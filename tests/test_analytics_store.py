"""Tests for pinky_daemon.analytics_store.AnalyticsStore — Tier 1 observability.

Covers the stuck-session observability additions:
- status enum lifecycle (running -> ok / error)
- arg_keys captured in metadata_json (PII-safe: key names only, no values)
- sweep_orphan_tool_calls closing out stale 'running' rows
- prune_tool_calls retention
- get_recent_tool_calls investigative helper
- schema migration backfill on pre-existing DBs without status column

Uses tmp_path for isolated SQLite DBs.
"""
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from pinky_daemon.analytics_store import AnalyticsStore
from pinky_daemon.pricing import RATE_TABLE

# ── Helpers ────────────────────────────────────────────────────────────────────

def _store(tmp_path: Path) -> AnalyticsStore:
    return AnalyticsStore(str(tmp_path / "analytics.db"))


def _seed_session(store: AnalyticsStore, session_id: str = "sess1", agent: str = "barsik") -> None:
    store.ensure_session_fact(
        session_id=session_id,
        agent_name=agent,
        session_label="test",
        provider="anthropic",
        model="claude-sonnet-4",
    )


def _iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")


# ── status enum lifecycle ──────────────────────────────────────────────────────

class TestStatusEnum:
    def test_start_sets_status_running(self, tmp_path):
        store = _store(tmp_path)
        _seed_session(store)
        store.start_tool_call(
            session_id="sess1", agent_name="barsik", turn_seq=1,
            tool_call_key="k1", tool_name="Read",
        )
        rows = store.get_recent_tool_calls(agent_name="barsik")
        assert len(rows) == 1
        assert rows[0]["status"] == "running"
        assert rows[0]["ended_at"] is None

    def test_finish_success_sets_status_ok(self, tmp_path):
        store = _store(tmp_path)
        _seed_session(store)
        store.start_tool_call(
            session_id="sess1", agent_name="barsik", turn_seq=1,
            tool_call_key="k1", tool_name="Read",
        )
        store.finish_tool_call(
            session_id="sess1", agent_name="barsik",
            tool_call_key="k1", success=True,
        )
        rows = store.get_recent_tool_calls(agent_name="barsik")
        assert rows[0]["status"] == "ok"
        assert rows[0]["success"] == 1

    def test_finish_failure_sets_status_error(self, tmp_path):
        store = _store(tmp_path)
        _seed_session(store)
        store.start_tool_call(
            session_id="sess1", agent_name="barsik", turn_seq=1,
            tool_call_key="k1", tool_name="Bash",
        )
        store.finish_tool_call(
            session_id="sess1", agent_name="barsik",
            tool_call_key="k1", success=False, error_type="nonzero_exit",
        )
        rows = store.get_recent_tool_calls(agent_name="barsik")
        assert rows[0]["status"] == "error"
        assert rows[0]["error_type"] == "nonzero_exit"

    def test_finish_explicit_status_override(self, tmp_path):
        store = _store(tmp_path)
        _seed_session(store)
        store.start_tool_call(
            session_id="sess1", agent_name="barsik", turn_seq=1,
            tool_call_key="k1", tool_name="Edit",
        )
        store.finish_tool_call(
            session_id="sess1", agent_name="barsik",
            tool_call_key="k1", success=False, status="cancelled",
        )
        rows = store.get_recent_tool_calls(agent_name="barsik")
        assert rows[0]["status"] == "cancelled"


# ── arg_keys in metadata ───────────────────────────────────────────────────────

class TestArgKeysMetadata:
    def test_arg_keys_round_trip(self, tmp_path):
        store = _store(tmp_path)
        _seed_session(store)
        store.start_tool_call(
            session_id="sess1", agent_name="barsik", turn_seq=1,
            tool_call_key="k1", tool_name="Edit",
            metadata={"arg_keys": ["file_path", "new_string", "old_string"]},
        )
        store.finish_tool_call(
            session_id="sess1", agent_name="barsik",
            tool_call_key="k1", success=True,
        )
        rows = store.get_recent_tool_calls(agent_name="barsik")
        assert rows[0]["metadata"]["arg_keys"] == [
            "file_path", "new_string", "old_string",
        ]

    def test_finish_merges_metadata(self, tmp_path):
        store = _store(tmp_path)
        _seed_session(store)
        store.start_tool_call(
            session_id="sess1", agent_name="barsik", turn_seq=1,
            tool_call_key="k1", tool_name="Bash",
            metadata={"arg_keys": ["command"]},
        )
        store.finish_tool_call(
            session_id="sess1", agent_name="barsik",
            tool_call_key="k1", success=True,
            metadata={"exit_code": 0},
        )
        rows = store.get_recent_tool_calls(agent_name="barsik")
        meta = rows[0]["metadata"]
        assert meta["arg_keys"] == ["command"]
        assert meta["exit_code"] == 0


# ── orphan sweep ───────────────────────────────────────────────────────────────

class TestOrphanSweep:
    def test_sweep_closes_stale_running_rows(self, tmp_path):
        store = _store(tmp_path)
        _seed_session(store)
        # Start a tool call "2 hours ago"
        old_ts = _iso(datetime.now(UTC) - timedelta(hours=2))
        store.start_tool_call(
            session_id="sess1", agent_name="barsik", turn_seq=1,
            tool_call_key="k_old", tool_name="WebFetch", ts=old_ts,
        )
        # Fresh running row — must not be touched
        store.start_tool_call(
            session_id="sess1", agent_name="barsik", turn_seq=2,
            tool_call_key="k_new", tool_name="Read",
        )
        count = store.sweep_orphan_tool_calls(older_than_seconds=3600)
        assert count == 1

        rows = store.get_recent_tool_calls(agent_name="barsik", limit=10)
        by_key = {r["tool_call_key"]: r for r in rows}
        assert by_key["k_old"]["status"] == "orphan"
        assert by_key["k_old"]["error_type"] == "orphan"
        assert by_key["k_old"]["success"] == 0
        assert by_key["k_old"]["ended_at"] is not None
        assert by_key["k_old"]["duration_ms"] is not None
        assert by_key["k_new"]["status"] == "running"

    def test_sweep_skips_already_finished(self, tmp_path):
        store = _store(tmp_path)
        _seed_session(store)
        old_ts = _iso(datetime.now(UTC) - timedelta(hours=2))
        store.start_tool_call(
            session_id="sess1", agent_name="barsik", turn_seq=1,
            tool_call_key="k1", tool_name="Read", ts=old_ts,
        )
        store.finish_tool_call(
            session_id="sess1", agent_name="barsik",
            tool_call_key="k1", success=True,
        )
        count = store.sweep_orphan_tool_calls(older_than_seconds=3600)
        assert count == 0


# ── retention prune ────────────────────────────────────────────────────────────

class TestPruneToolCalls:
    def test_prune_deletes_old_rows(self, tmp_path):
        store = _store(tmp_path)
        _seed_session(store)
        old_ts = _iso(datetime.now(UTC) - timedelta(days=45))
        new_ts = _iso(datetime.now(UTC) - timedelta(days=5))
        store.start_tool_call(
            session_id="sess1", agent_name="barsik", turn_seq=1,
            tool_call_key="k_old", tool_name="Read", ts=old_ts,
        )
        store.start_tool_call(
            session_id="sess1", agent_name="barsik", turn_seq=2,
            tool_call_key="k_new", tool_name="Read", ts=new_ts,
        )
        deleted = store.prune_tool_calls(retention_days=30)
        assert deleted == 1
        rows = store.get_recent_tool_calls(agent_name="barsik")
        assert len(rows) == 1
        assert rows[0]["tool_call_key"] == "k_new"

    def test_prune_keeps_all_within_window(self, tmp_path):
        store = _store(tmp_path)
        _seed_session(store)
        store.start_tool_call(
            session_id="sess1", agent_name="barsik", turn_seq=1,
            tool_call_key="k1", tool_name="Read",
        )
        deleted = store.prune_tool_calls(retention_days=30)
        assert deleted == 0


# ── get_recent_tool_calls ──────────────────────────────────────────────────────

class TestGetRecentToolCalls:
    def test_returns_newest_first(self, tmp_path):
        store = _store(tmp_path)
        _seed_session(store)
        for i in range(3):
            ts = _iso(datetime.now(UTC) - timedelta(minutes=10 - i))
            store.start_tool_call(
                session_id="sess1", agent_name="barsik", turn_seq=i,
                tool_call_key=f"k{i}", tool_name="Read", ts=ts,
            )
        rows = store.get_recent_tool_calls(agent_name="barsik")
        assert [r["tool_call_key"] for r in rows] == ["k2", "k1", "k0"]

    def test_filters_by_agent(self, tmp_path):
        store = _store(tmp_path)
        _seed_session(store, agent="barsik")
        _seed_session(store, session_id="sess2", agent="murzik")
        store.start_tool_call(
            session_id="sess1", agent_name="barsik", turn_seq=1,
            tool_call_key="b1", tool_name="Read",
        )
        store.start_tool_call(
            session_id="sess2", agent_name="murzik", turn_seq=1,
            tool_call_key="m1", tool_name="Bash",
        )
        barsik_rows = store.get_recent_tool_calls(agent_name="barsik")
        assert len(barsik_rows) == 1
        assert barsik_rows[0]["tool_call_key"] == "b1"

    def test_filters_by_session(self, tmp_path):
        store = _store(tmp_path)
        _seed_session(store, session_id="sA")
        _seed_session(store, session_id="sB")
        store.start_tool_call(
            session_id="sA", agent_name="barsik", turn_seq=1,
            tool_call_key="a1", tool_name="Read",
        )
        store.start_tool_call(
            session_id="sB", agent_name="barsik", turn_seq=1,
            tool_call_key="b1", tool_name="Bash",
        )
        rows = store.get_recent_tool_calls(session_id="sA")
        assert len(rows) == 1
        assert rows[0]["tool_call_key"] == "a1"

    def test_respects_limit(self, tmp_path):
        store = _store(tmp_path)
        _seed_session(store)
        for i in range(10):
            store.start_tool_call(
                session_id="sess1", agent_name="barsik", turn_seq=i,
                tool_call_key=f"k{i}", tool_name="Read",
            )
        rows = store.get_recent_tool_calls(agent_name="barsik", limit=3)
        assert len(rows) == 3


# ── schema migration ──────────────────────────────────────────────────────────

class TestSchemaMigration:
    def test_adds_status_column_to_preexisting_db(self, tmp_path):
        """Simulate a DB created before the status column existed and verify
        AnalyticsStore.__init__ migrates it and backfills status values."""
        db_path = tmp_path / "legacy.db"
        # Manually build pre-migration schema (no status column)
        with sqlite3.connect(str(db_path)) as conn:
            conn.executescript(
                """
                CREATE TABLE analytics_tool_calls (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  session_id TEXT NOT NULL,
                  agent_name TEXT NOT NULL,
                  turn_seq INTEGER,
                  tool_call_key TEXT,
                  tool_name TEXT NOT NULL,
                  tool_namespace TEXT,
                  started_at TEXT NOT NULL,
                  ended_at TEXT,
                  duration_ms INTEGER,
                  success INTEGER,
                  error_type TEXT,
                  metadata_json TEXT
                );
                """
            )
            # Closed OK row — should backfill to 'ok'
            conn.execute(
                "INSERT INTO analytics_tool_calls "
                "(session_id, agent_name, tool_name, started_at, ended_at, success) "
                "VALUES ('s1','barsik','Read','2026-04-01T00:00:00Z',"
                "'2026-04-01T00:00:01Z',1)"
            )
            # Closed failed row — backfill to 'error'
            conn.execute(
                "INSERT INTO analytics_tool_calls "
                "(session_id, agent_name, tool_name, started_at, ended_at, success) "
                "VALUES ('s1','barsik','Bash','2026-04-01T00:00:02Z',"
                "'2026-04-01T00:00:03Z',0)"
            )
            # Still-open row — should stay 'running'
            conn.execute(
                "INSERT INTO analytics_tool_calls "
                "(session_id, agent_name, tool_name, started_at) "
                "VALUES ('s1','barsik','Edit','2026-04-01T00:00:04Z')"
            )

        # Open through AnalyticsStore — should trigger migration
        store = AnalyticsStore(str(db_path))
        rows = store.get_recent_tool_calls(agent_name="barsik", limit=10)
        statuses = {r["tool_name"]: r["status"] for r in rows}
        assert statuses == {"Read": "ok", "Bash": "error", "Edit": "running"}


class TestTurnClassification:
    def test_send_video_classifies_as_messaging(self, tmp_path):
        store = _store(tmp_path)
        # Bare and MCP-namespaced forms both classify as messaging, on par
        # with send_document/send_photo (regression guard for the send_video
        # tool added to _MESSAGING_TOOLS).
        assert store._classify_turn(["send_video"], []) == "messaging"
        assert (
            store._classify_turn(["mcp__pinky-messaging__send_video"], [])
            == "messaging"
        )


class TestGetCategoriesAgentFilter:
    def test_agent_filter_does_not_crash_and_scopes_turns(self, tmp_path):
        store = _store(tmp_path)
        _seed_session(store, session_id="s1", agent="barsik")
        _seed_session(store, session_id="s2", agent="murka")
        now = _iso(datetime.now(UTC))
        store.log_turn_usage(
            session_id="s1", agent_name="barsik", turn_seq=1,
            provider="anthropic", model="claude-sonnet-4",
            input_tokens=100, output_tokens=50, cached_input_tokens=0,
            ts=now,
        )
        store.start_tool_call(
            session_id="s1", agent_name="barsik", turn_seq=1,
            tool_call_key="k1", tool_name="Bash",
            metadata={"command": "pytest tests/"}, ts=now,
        )
        store.log_turn_usage(
            session_id="s2", agent_name="murka", turn_seq=1,
            provider="anthropic", model="claude-sonnet-4",
            input_tokens=10, output_tokens=5, cached_input_tokens=0,
            ts=now,
        )
        result = store.get_categories(range_name="7d", agent_name="barsik")
        assert sum(c["turns"] for c in result["categories"]) == 1
        assert sum(c["input_tokens"] for c in result["categories"]) == 100


class TestPricingLookup:
    def test_overview_costs_use_seeded_pricing(self, tmp_path):
        store = _store(tmp_path)
        _seed_session(store)
        store.log_turn_usage(
            session_id="sess1", agent_name="barsik", turn_seq=1,
            provider="anthropic", model="claude-sonnet-4",
            input_tokens=1_000_000, output_tokens=0, cached_input_tokens=0,
        )
        overview = store.get_overview(range_name="7d")
        # claude-sonnet-4 seeded at 3.00 USD per MTok input
        assert overview["totals"]["cost_usd"] == pytest.approx(3.0)

    def test_lookup_pricing_resolves_aliases(self, tmp_path):
        store = _store(tmp_path)
        ts = _iso(datetime.now(UTC))
        row = store._lookup_pricing(provider="claude_code", model="Claude-Sonnet-4", ts=ts)
        assert row is not None
        assert row["input_usd_per_mtok"] == 3.00
        assert store._lookup_pricing(provider="anthropic", model="no-such-model", ts=ts) is None


class TestSeedRateTableParity:
    """#669: the Analytics seed and pinky_daemon.pricing.RATE_TABLE are two
    hand-maintained rate tables that feed two cost paths (dashboard vs live
    per-turn). They must agree for the same model, or the same usage reports
    different dollar figures. This pins them together as a drift guard."""

    def test_seed_pricing_matches_rate_table(self, tmp_path):
        store = _store(tmp_path)
        with store._connect() as conn:
            rows = conn.execute(
                "SELECT model, input_usd_per_mtok, output_usd_per_mtok, "
                "cached_input_usd_per_mtok FROM analytics_model_pricing "
                "WHERE provider = 'anthropic'"
            ).fetchall()
        # Normalize dotted legacy ids (claude-opus-4.1) to the hyphenated form
        # RATE_TABLE uses (claude-opus-4-1) so the two key spaces line up.
        seeded = {r["model"].replace(".", "-"): r for r in rows}

        missing = [m for m in RATE_TABLE if m not in seeded]
        assert not missing, f"models in RATE_TABLE but absent from Analytics seed: {missing}"

        mismatches = []
        for model, rates in RATE_TABLE.items():
            row = seeded[model]
            if (
                row["input_usd_per_mtok"] != rates["input"]
                or row["output_usd_per_mtok"] != rates["output"]
                or row["cached_input_usd_per_mtok"] != rates["cache_read"]
            ):
                mismatches.append(
                    f"{model}: seed "
                    f"{row['input_usd_per_mtok']}/{row['output_usd_per_mtok']}/"
                    f"{row['cached_input_usd_per_mtok']} != RATE_TABLE "
                    f"{rates['input']}/{rates['output']}/{rates['cache_read']}"
                )
        assert not mismatches, "Analytics seed disagrees with pricing.RATE_TABLE:\n" + "\n".join(
            mismatches
        )


class TestSeedPricingMigration:
    """#669: deployed Analytics DBs are already seeded, so _seed_default_pricing
    (empty-table guard) won't re-run. _migrate_opus_haiku_seed_pricing corrects
    the stale rows on every init; these cover a stale DB and operator safety."""

    def _stomp_stale(self, db_path: str) -> None:
        """Rewrite a seeded DB to its pre-#669 (wrong) state."""
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "UPDATE analytics_model_pricing SET input_usd_per_mtok=15.00, "
                "output_usd_per_mtok=75.00, cached_input_usd_per_mtok=1.50 "
                "WHERE notes='seed' AND model IN "
                "('claude-opus-4-8','claude-opus-4-7','claude-opus-4-6')"
            )
            conn.execute(
                "UPDATE analytics_model_pricing SET input_usd_per_mtok=0.80, "
                "output_usd_per_mtok=4.00, cached_input_usd_per_mtok=0.08 "
                "WHERE notes='seed' AND model='claude-haiku-4-5'"
            )
            conn.execute(
                "DELETE FROM analytics_model_pricing "
                "WHERE model IN ('claude-opus-4-5','claude-sonnet-4-5')"
            )

    def test_migration_corrects_stale_seed_on_reopen(self, tmp_path):
        db_path = str(tmp_path / "stale.db")
        AnalyticsStore(db_path)  # seeds correct rows
        self._stomp_stale(db_path)  # simulate a pre-#669 deployed DB
        store = AnalyticsStore(db_path)  # __init__ runs the migration

        ts = _iso(datetime.now(UTC))
        for model in (
            "claude-opus-4-8",
            "claude-opus-4-7",
            "claude-opus-4-6",
            "claude-opus-4-5",
        ):
            row = store._lookup_pricing(provider="anthropic", model=model, ts=ts)
            assert row is not None, f"{model} missing after migration"
            assert row["input_usd_per_mtok"] == 5.00, model
            assert row["output_usd_per_mtok"] == 25.00, model
            assert row["cached_input_usd_per_mtok"] == 0.50, model

        haiku = store._lookup_pricing(provider="anthropic", model="claude-haiku-4-5", ts=ts)
        assert haiku["input_usd_per_mtok"] == 1.00
        assert haiku["output_usd_per_mtok"] == 5.00
        assert haiku["cached_input_usd_per_mtok"] == 0.10

        sonnet45 = store._lookup_pricing(provider="anthropic", model="claude-sonnet-4-5", ts=ts)
        assert sonnet45 is not None and sonnet45["input_usd_per_mtok"] == 3.00

    def test_migration_is_idempotent_and_inserts_once(self, tmp_path):
        db_path = str(tmp_path / "idem.db")
        AnalyticsStore(db_path)
        self._stomp_stale(db_path)
        AnalyticsStore(db_path)  # first migration
        AnalyticsStore(db_path)  # second init — must not double-insert or re-touch
        with sqlite3.connect(db_path) as conn:
            for model in ("claude-opus-4-5", "claude-sonnet-4-5"):
                count = conn.execute(
                    "SELECT COUNT(*) FROM analytics_model_pricing WHERE model=?",
                    (model,),
                ).fetchone()[0]
                assert count == 1, f"{model} duplicated: {count} rows"

    def test_migration_preserves_operator_overrides(self, tmp_path):
        db_path = str(tmp_path / "override.db")
        AnalyticsStore(db_path)
        # Operator-priced row (non-'seed' notes) at the legacy tier must survive.
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "INSERT INTO analytics_model_pricing (provider, model, effective_from, "
                "effective_to, input_usd_per_mtok, output_usd_per_mtok, "
                "cached_input_usd_per_mtok, notes) VALUES "
                "('anthropic','claude-opus-4-8','2026-06-01T00:00:00Z',NULL,15.00,75.00,1.50,'operator')"
            )
        AnalyticsStore(db_path)  # migration must not touch the operator row
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT input_usd_per_mtok FROM analytics_model_pricing "
                "WHERE model='claude-opus-4-8' AND notes='operator'"
            ).fetchone()
        assert row is not None and row[0] == 15.00
