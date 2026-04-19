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

from pinky_daemon.analytics_store import AnalyticsStore

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
