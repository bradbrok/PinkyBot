from __future__ import annotations

import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

from pinky_daemon.sqlite_journal import configure_rollback_journal

# Providers to include in analytics dashboards.
# Only Anthropic/Claude usage — excludes Codex CLI (OpenAI) and other non-Anthropic providers.
_ANTHROPIC_PROVIDERS = {"firstParty", "default", "anthropic", "bedrock", "vertex"}
# Providers whose token usage carries a real per-token cost we price in the
# overview/agent/category cost views. Extends the Anthropic set with the
# OpenAI/Codex family so Codex agents (e.g. Murzik on gpt-5.5) appear in the
# cost dashboards at parity with Claude agents (#206) — previously the cost
# views were hard-gated to Anthropic-only, leaving Codex usage invisible
# even though OpenAI pricing was already seeded.
# NOTE: opencode is intentionally NOT included yet — it has no seeded pricing
# and `_provider_alias` can't map it to one rate table (opencode runs either
# Anthropic or OpenAI models depending on config), so including it would surface
# opencode turns at a misleading $0. Add it back alongside pricing + a model-aware
# alias when an opencode agent ships (#786).
_COSTED_PROVIDERS = _ANTHROPIC_PROVIDERS | {"openai", "openai_api", "codex_cli"}


def _utcnow() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _range_bounds(range_name: str) -> tuple[str, str]:
    now = datetime.now(UTC).replace(microsecond=0)
    if range_name == "today":
        start = now.replace(hour=0, minute=0, second=0)
    elif range_name == "30d":
        start = now - timedelta(days=30)
    else:
        start = now - timedelta(days=7)
    return (
        start.isoformat().replace("+00:00", "Z"),
        now.isoformat().replace("+00:00", "Z"),
    )


def _prev_range_bounds(range_name: str) -> tuple[str, str]:
    """Compute the previous period's bounds for delta comparison."""
    now = datetime.now(UTC).replace(microsecond=0)
    if range_name == "today":
        end = now.replace(hour=0, minute=0, second=0)
        start = end - timedelta(days=1)
    elif range_name == "30d":
        end = now - timedelta(days=30)
        start = end - timedelta(days=30)
    else:
        end = now - timedelta(days=7)
        start = end - timedelta(days=7)
    return (
        start.isoformat().replace("+00:00", "Z"),
        end.isoformat().replace("+00:00", "Z"),
    )


class AnalyticsStore:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._pricing_cache: dict[tuple[str, str], list[dict]] | None = None
        self._init_db()

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._connect() as conn:
            # Runs once per store construction, before any other connection is
            # handed out. TRUNCATE is persisted in the database header, so the
            # short-lived connections from _connect() inherit it.
            configure_rollback_journal(conn, db_label="analytics.db")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS analytics_session_facts (
                  session_id TEXT PRIMARY KEY,
                  agent_name TEXT NOT NULL,
                  session_label TEXT,
                  provider TEXT NOT NULL,
                  model TEXT NOT NULL,
                  project_key TEXT,
                  project_source TEXT,
                  started_at TEXT NOT NULL,
                  ended_at TEXT,
                  daemon_run_id TEXT,
                  source TEXT NOT NULL DEFAULT 'live',
                  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE INDEX IF NOT EXISTS idx_asf_agent_started
                  ON analytics_session_facts(agent_name, started_at DESC);

                CREATE INDEX IF NOT EXISTS idx_asf_project_started
                  ON analytics_session_facts(project_key, started_at DESC);

                CREATE TABLE IF NOT EXISTS analytics_turn_usage (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  session_id TEXT NOT NULL,
                  agent_name TEXT NOT NULL,
                  turn_seq INTEGER NOT NULL,
                  ts TEXT NOT NULL,
                  provider TEXT NOT NULL,
                  model TEXT NOT NULL,
                  input_tokens INTEGER NOT NULL DEFAULT 0,
                  output_tokens INTEGER NOT NULL DEFAULT 0,
                  cached_input_tokens INTEGER NOT NULL DEFAULT 0,
                  error INTEGER NOT NULL DEFAULT 0,
                  source_event_id TEXT,
                  UNIQUE(session_id, turn_seq)
                );

                CREATE INDEX IF NOT EXISTS idx_atu_agent_ts
                  ON analytics_turn_usage(agent_name, ts DESC);

                CREATE INDEX IF NOT EXISTS idx_atu_model_ts
                  ON analytics_turn_usage(model, ts DESC);

                CREATE TABLE IF NOT EXISTS analytics_tool_calls (
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
                  status TEXT NOT NULL DEFAULT 'running',
                  metadata_json TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_atc_agent_started
                  ON analytics_tool_calls(agent_name, started_at DESC);

                CREATE INDEX IF NOT EXISTS idx_atc_tool_started
                  ON analytics_tool_calls(tool_name, started_at DESC);

                CREATE INDEX IF NOT EXISTS idx_atc_call_key
                  ON analytics_tool_calls(session_id, tool_call_key);

                CREATE TABLE IF NOT EXISTS analytics_activity_events (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  session_id TEXT NOT NULL,
                  agent_name TEXT NOT NULL,
                  ts TEXT NOT NULL,
                  event_type TEXT NOT NULL,
                  subtype TEXT,
                  turn_seq INTEGER,
                  metadata_json TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_aae_agent_ts
                  ON analytics_activity_events(agent_name, ts DESC);

                CREATE INDEX IF NOT EXISTS idx_aae_type_ts
                  ON analytics_activity_events(event_type, ts DESC);

                CREATE TABLE IF NOT EXISTS analytics_task_classifications (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  session_id TEXT NOT NULL,
                  agent_name TEXT NOT NULL,
                  classification_version TEXT NOT NULL,
                  classified_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                  bucket_start TEXT NOT NULL,
                  bucket_end TEXT NOT NULL,
                  category TEXT NOT NULL,
                  confidence REAL,
                  evidence_json TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_atclass_agent_bucket
                  ON analytics_task_classifications(agent_name, bucket_start DESC);

                CREATE TABLE IF NOT EXISTS analytics_model_pricing (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  provider TEXT NOT NULL,
                  model TEXT NOT NULL,
                  effective_from TEXT NOT NULL,
                  effective_to TEXT,
                  input_usd_per_mtok REAL NOT NULL,
                  output_usd_per_mtok REAL NOT NULL,
                  cached_input_usd_per_mtok REAL NOT NULL DEFAULT 0,
                  notes TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_amp_lookup
                  ON analytics_model_pricing(provider, model, effective_from DESC);

                CREATE TABLE IF NOT EXISTS analytics_active_spans (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  day TEXT NOT NULL,
                  session_id TEXT NOT NULL,
                  agent_name TEXT NOT NULL,
                  span_start TEXT NOT NULL,
                  span_end TEXT NOT NULL,
                  active_seconds INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS analytics_rollup_dirty (
                  day TEXT NOT NULL,
                  agent_name TEXT NOT NULL,
                  reason TEXT,
                  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                  PRIMARY KEY(day, agent_name)
                );
                """
            )
            self._seed_default_pricing(conn)
            self._migrate_opus_haiku_seed_pricing(conn)
            self._migrate_legacy_dotted_model_ids(conn)
            self._ensure_pricing_rows(conn)
            self._migrate_codex_provider_attribution(conn)
            # Schema migration: add user_message_snippet to turn_usage
            cols = {
                r[1] for r in conn.execute("PRAGMA table_info(analytics_turn_usage)").fetchall()
            }
            if "user_message_snippet" not in cols:
                conn.execute(
                    "ALTER TABLE analytics_turn_usage ADD COLUMN user_message_snippet TEXT"
                )
            # Schema migration: add status enum to tool_calls
            tc_cols = {
                r[1] for r in conn.execute("PRAGMA table_info(analytics_tool_calls)").fetchall()
            }
            if "status" not in tc_cols:
                conn.execute(
                    "ALTER TABLE analytics_tool_calls "
                    "ADD COLUMN status TEXT NOT NULL DEFAULT 'running'"
                )
                # Backfill: closed rows (ended_at set) get ok/error from success;
                # open rows (ended_at null) remain 'running'.
                conn.execute(
                    "UPDATE analytics_tool_calls "
                    "SET status = CASE WHEN success=1 THEN 'ok' ELSE 'error' END "
                    "WHERE ended_at IS NOT NULL"
                )
            # Index on status must be created *after* the migration adds the column.
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_atc_status_started "
                "ON analytics_tool_calls(status, started_at)"
            )

    # Pricing rows added AFTER the original seed batch. _seed_default_pricing
    # only fires on an empty table, so these reach already-seeded production
    # DBs via the idempotent _ensure_pricing_rows below (insert-if-absent,
    # never clobbering an operator-set price). Verified official 2026 rates.
    _LATEST_PRICING_ADDITIONS = [
        ("openai", "gpt-5.5", "2020-01-01T00:00:00Z", None, 5.00, 30.00, 0.50, "seed"),
        ("openai", "gpt-5.3-codex", "2020-01-01T00:00:00Z", None, 1.75, 14.00, 0.175, "seed"),
        # gpt-5.6-sol (#860): codex fleet model since 2026-07-09. Official
        # rates identical to gpt-5.5 (developers.openai.com, verified
        # 2026-07-10). Mirrors pricing.RATE_TABLE (_GPT_FRONTIER), parity-pinned
        # by test_seed_pricing_matches_rate_table.
        ("openai", "gpt-5.6-sol", "2020-01-01T00:00:00Z", None, 5.00, 30.00, 0.50, "seed"),
        # Sonnet 5 (2026-06): the current Sonnet tier. Standard $3/$15 (cached
        # $0.30) — mirrors pinky_daemon.pricing.RATE_TABLE (_SONNET), pinned by
        # test_seed_pricing_matches_rate_table. Introductory $2/$10 runs through
        # 2026-08-31, then reverts; we seed the durable standard rate (see the
        # RATE_TABLE comment for the rationale).
        ("anthropic", "claude-sonnet-5", "2020-01-01T00:00:00Z", None, 3.00, 15.00, 0.30, "seed"),
        # Opus 5 (2026-07-24): same durable $5/$25 standard tier as Opus 4.8.
        ("anthropic", "claude-opus-5", "2020-01-01T00:00:00Z", None, 5.00, 25.00, 0.50, "seed"),
    ]

    def _seed_default_pricing(self, conn) -> None:
        row = conn.execute("SELECT COUNT(*) AS count FROM analytics_model_pricing").fetchone()
        if row and row["count"]:
            return
        seed_rows = [
            # OpenAI / Codex-family defaults seeded with current official rates.
            ("openai", "gpt-5", "2020-01-01T00:00:00Z", None, 1.25, 10.00, 0.125, "seed"),
            ("openai", "gpt-5-chat-latest", "2020-01-01T00:00:00Z", None, 1.25, 10.00, 0.125, "seed"),
            ("openai", "gpt-5-codex", "2020-01-01T00:00:00Z", None, 1.25, 10.00, 0.125, "seed"),
            ("openai", "gpt-5-mini", "2020-01-01T00:00:00Z", None, 0.25, 2.00, 0.025, "seed"),
            ("openai", "gpt-5-nano", "2020-01-01T00:00:00Z", None, 0.05, 0.40, 0.005, "seed"),
            ("openai", "gpt-5.1", "2020-01-01T00:00:00Z", None, 1.25, 10.00, 0.125, "seed"),
            ("openai", "gpt-5.1-chat-latest", "2020-01-01T00:00:00Z", None, 1.25, 10.00, 0.125, "seed"),
            ("openai", "gpt-5.1-codex", "2020-01-01T00:00:00Z", None, 1.25, 10.00, 0.125, "seed"),
            ("openai", "gpt-5.1-codex-max", "2020-01-01T00:00:00Z", None, 1.25, 10.00, 0.125, "seed"),
            ("openai", "gpt-5.2", "2020-01-01T00:00:00Z", None, 1.75, 14.00, 0.175, "seed"),
            ("openai", "gpt-5.2-chat-latest", "2020-01-01T00:00:00Z", None, 1.75, 14.00, 0.175, "seed"),
            ("openai", "gpt-5.2-codex", "2020-01-01T00:00:00Z", None, 1.75, 14.00, 0.175, "seed"),
            # Anthropic defaults seeded for future provider expansion.
            # NOTE: these rates must mirror pinky_daemon.pricing.RATE_TABLE — the
            # live per-turn cost path reads that table, this Analytics path reads
            # this seed, and the two must agree on the same turn's dollar figure.
            # test_seed_pricing_matches_rate_table pins them together (#669).
            ("anthropic", "claude-fable-5", "2020-01-01T00:00:00Z", None, 10.00, 50.00, 1.00, "seed"),
            ("anthropic", "claude-mythos-5", "2020-01-01T00:00:00Z", None, 10.00, 50.00, 1.00, "seed"),
            # Opus 4.5+ is the standard $5/$25 tier (flat across 4.5→4.8), not the
            # pre-4.5 legacy $15/$75 tier. Mispriced as legacy until #669.
            ("anthropic", "claude-opus-4-8", "2020-01-01T00:00:00Z", None, 5.00, 25.00, 0.50, "seed"),
            ("anthropic", "claude-opus-4-7", "2020-01-01T00:00:00Z", None, 5.00, 25.00, 0.50, "seed"),
            ("anthropic", "claude-opus-4-6", "2020-01-01T00:00:00Z", None, 5.00, 25.00, 0.50, "seed"),
            ("anthropic", "claude-opus-4-5", "2020-01-01T00:00:00Z", None, 5.00, 25.00, 0.50, "seed"),
            # Pre-4.5 Opus stays on the legacy $15/$75 tier.
            ("anthropic", "claude-opus-4-1", "2020-01-01T00:00:00Z", None, 15.00, 75.00, 1.50, "seed"),
            ("anthropic", "claude-opus-4", "2020-01-01T00:00:00Z", None, 15.00, 75.00, 1.50, "seed"),
            ("anthropic", "claude-sonnet-4-6", "2020-01-01T00:00:00Z", None, 3.00, 15.00, 0.30, "seed"),
            ("anthropic", "claude-sonnet-4-5", "2020-01-01T00:00:00Z", None, 3.00, 15.00, 0.30, "seed"),
            ("anthropic", "claude-sonnet-4", "2020-01-01T00:00:00Z", None, 3.00, 15.00, 0.30, "seed"),
            ("anthropic", "claude-sonnet-3-7", "2020-01-01T00:00:00Z", None, 3.00, 15.00, 0.30, "seed"),
            ("anthropic", "claude-sonnet-3-5", "2020-01-01T00:00:00Z", None, 3.00, 15.00, 0.30, "seed"),
            # Haiku 4.5 is the $1/$5 tier; 0.80/4.00/0.08 was the old Haiku 3.5
            # price. Corrected in #669.
            ("anthropic", "claude-haiku-4-5", "2020-01-01T00:00:00Z", None, 1.00, 5.00, 0.10, "seed"),
            ("anthropic", "claude-haiku-3-5", "2020-01-01T00:00:00Z", None, 0.80, 4.00, 0.08, "seed"),
            ("anthropic", "claude-haiku-3", "2020-01-01T00:00:00Z", None, 0.25, 1.25, 0.03, "seed"),
        ]
        conn.executemany(
            """
            INSERT INTO analytics_model_pricing (
              provider, model, effective_from, effective_to,
              input_usd_per_mtok, output_usd_per_mtok, cached_input_usd_per_mtok, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            seed_rows + self._LATEST_PRICING_ADDITIONS,
        )

    def _migrate_opus_haiku_seed_pricing(self, conn) -> None:
        """Realign mispriced Anthropic seed rows on already-seeded DBs (#669).

        ``_seed_default_pricing`` only runs on an empty table, so deployed
        Analytics DBs keep their original (wrong) rates after a seed fix
        lands. This one-shot, idempotent migration corrects them to match
        ``pinky_daemon.pricing.RATE_TABLE``:

        * Opus 4.6/4.7/4.8 were seeded at the legacy $15/$75 tier — a ~3x
          overstatement. Corrected to the standard $5/$25 tier.
        * ``claude-opus-4-5`` was missing — inserted at $5/$25.
        * Haiku 4.5 was seeded at the old Haiku-3.5 $0.80/$4.00 price —
          corrected to $1.00/$5.00.
        * ``claude-sonnet-4-5`` was missing — inserted at $3/$15.

        Safe/idempotent: only ``notes='seed'`` rows are touched (operator
        overrides with any other ``notes`` are left alone); the UPDATEs match
        only the known-stale rate, so a second run is a no-op; the INSERTs are
        skipped when the model already has a row.
        """
        # Opus 4.5+ standard tier: fix legacy-priced seed rows in place.
        conn.execute(
            """
            UPDATE analytics_model_pricing
            SET input_usd_per_mtok = 5.00,
                output_usd_per_mtok = 25.00,
                cached_input_usd_per_mtok = 0.50
            WHERE provider = 'anthropic'
              AND model IN ('claude-opus-4-8', 'claude-opus-4-7', 'claude-opus-4-6')
              AND notes = 'seed'
              AND input_usd_per_mtok = 15.00
            """
        )
        # Haiku 4.5: replace the old Haiku-3.5 price.
        conn.execute(
            """
            UPDATE analytics_model_pricing
            SET input_usd_per_mtok = 1.00,
                output_usd_per_mtok = 5.00,
                cached_input_usd_per_mtok = 0.10
            WHERE provider = 'anthropic'
              AND model = 'claude-haiku-4-5'
              AND notes = 'seed'
              AND input_usd_per_mtok = 0.80
            """
        )
        # Models absent from older seeds — insert at the correct tier.
        for model, inp, out, cached in (
            ("claude-opus-4-5", 5.00, 25.00, 0.50),
            ("claude-sonnet-4-5", 3.00, 15.00, 0.30),
        ):
            exists = conn.execute(
                "SELECT 1 FROM analytics_model_pricing "
                "WHERE provider = 'anthropic' AND model = ? LIMIT 1",
                (model,),
            ).fetchone()
            if not exists:
                conn.execute(
                    """
                    INSERT INTO analytics_model_pricing (
                      provider, model, effective_from, effective_to,
                      input_usd_per_mtok, output_usd_per_mtok,
                      cached_input_usd_per_mtok, notes
                    ) VALUES ('anthropic', ?, '2020-01-01T00:00:00Z', NULL, ?, ?, ?, 'seed')
                    """,
                    (model, inp, out, cached),
                )
        # Drop the lazily-built cache so corrected rows are read next lookup.
        self._pricing_cache = None

    # Legacy seed rows that used a dotted minor-version id, mapped to the
    # canonical hyphenated form pinky_daemon.pricing.RATE_TABLE / the Anthropic
    # API use. _seed_default_pricing now emits the canonical form directly; this
    # corrects DBs seeded before #759.
    _LEGACY_DOTTED_MODEL_IDS = {
        "claude-opus-4.1": "claude-opus-4-1",
        "claude-sonnet-3.7": "claude-sonnet-3-7",
        "claude-sonnet-3.5": "claude-sonnet-3-5",
        "claude-haiku-3.5": "claude-haiku-3-5",
    }

    def _migrate_legacy_dotted_model_ids(self, conn) -> None:
        """Rename dotted legacy seed ids to the canonical hyphenated form (#759).

        The seed historically registered some legacy models with a dotted minor
        version (``claude-opus-4.1``, ``claude-sonnet-3.7/3.5``,
        ``claude-haiku-3.5``) that never matched ``pricing.RATE_TABLE`` or the
        Anthropic API ids — so the rows were dead to canonical lookups and sat
        outside the #669 parity drift-guard. ``_seed_default_pricing`` now emits
        the canonical form, but ``_seed_default_pricing`` only runs on an empty
        table, so deployed DBs keep the dotted rows; this corrects them on init.

        Safe/idempotent: only ``notes='seed'`` rows are touched (operator
        overrides are left alone); if a canonical seed row already exists the
        dotted dupe is dropped instead of renamed (no UNIQUE constraint on the
        table, so a blind rename could otherwise duplicate); after the first run
        no dotted rows remain, so a second run is a no-op.
        """
        touched = False
        for dotted, canonical in self._LEGACY_DOTTED_MODEL_IDS.items():
            canonical_exists = conn.execute(
                "SELECT 1 FROM analytics_model_pricing "
                "WHERE provider='anthropic' AND model=? AND notes='seed' LIMIT 1",
                (canonical,),
            ).fetchone()
            if canonical_exists:
                cur = conn.execute(
                    "DELETE FROM analytics_model_pricing "
                    "WHERE provider='anthropic' AND model=? AND notes='seed'",
                    (dotted,),
                )
            else:
                cur = conn.execute(
                    "UPDATE analytics_model_pricing SET model=? "
                    "WHERE provider='anthropic' AND model=? AND notes='seed'",
                    (canonical, dotted),
                )
            if cur.rowcount:
                touched = True
        if touched:
            # Drop the lazily-built cache so renamed rows are read next lookup.
            self._pricing_cache = None

    def _ensure_pricing_rows(self, conn) -> None:
        """Idempotently backfill pricing rows added after the initial seed.

        ``_seed_default_pricing`` only populates an empty table, so models
        introduced in later releases (e.g. gpt-5.5) never reach an
        already-seeded production DB through it. Insert each addition only
        when no row exists for that (provider, model) — never overrides an
        operator-set or already-present price.
        """
        for r in self._LATEST_PRICING_ADDITIONS:
            conn.execute(
                """
                INSERT INTO analytics_model_pricing (
                  provider, model, effective_from, effective_to,
                  input_usd_per_mtok, output_usd_per_mtok,
                  cached_input_usd_per_mtok, notes
                )
                SELECT ?, ?, ?, ?, ?, ?, ?, ?
                WHERE NOT EXISTS (
                  SELECT 1 FROM analytics_model_pricing
                  WHERE provider = ? AND model = ?
                )
                """,
                (*r, r[0], r[1]),
            )

    def _migrate_codex_provider_attribution(self, conn) -> None:
        """Reattribute + reprice codex turns mislogged as ``anthropic`` (#860).

        ``TmuxSession._log_turn_cost_and_analytics`` hardcoded
        ``provider="anthropic"``, so every CodexTmuxSession turn landed as
        ``anthropic/gpt-*`` — a (provider, model) pair the pricing lookup can
        never match (``_provider_alias`` never crosses providers), pricing all
        codex history at $0. Heal deployed rows to ``codex_cli`` — the value
        new codex turns record and the SDK path's default — which aliases onto
        the openai rate rows at read time.

        The provider flip alone would over-bill history: pre-#860 rows also
        predate ``_normalize_turn_usage``, so their ``input_tokens`` is
        codex's INCLUSIVE count with the cached span invisible
        (``cached_input_tokens=0`` — the old extraction read only the
        Anthropic cache keys). Repricing that at the full input rate
        overstates ~7x (measured across the fleet's 2026-07 rollouts: 97.5%
        of codex input is cached reads). The per-turn split is unrecoverable
        from the DB, so rows carrying that signature move the whole count to
        the cached column: error bounded at roughly -20% (the true-uncached
        share priced at 10x the cached rate), failing toward the visible
        undercount — the same direction ``_normalize_turn_usage`` chooses.

        Follows the ``_migrate_opus_haiku_seed_pricing`` precedent: idempotent
        (both predicates match only misattributed ``anthropic`` pairs, gone
        after the first run) and narrow (a ``gpt-*`` model under ``anthropic``
        cannot be legitimate — Anthropic serves no gpt model — so no honest
        row can match). Touches turn usage AND session facts so per-session
        rollups group consistently.
        """
        conn.execute(
            """
            UPDATE analytics_turn_usage
            SET provider = 'codex_cli',
                cached_input_tokens = input_tokens,
                input_tokens = 0
            WHERE provider = 'anthropic' AND model LIKE 'gpt-%'
              AND cached_input_tokens = 0
            """
        )
        for table in ("analytics_turn_usage", "analytics_session_facts"):
            conn.execute(
                f"""
                UPDATE {table}
                SET provider = 'codex_cli'
                WHERE provider = 'anthropic' AND model LIKE 'gpt-%'
                """
            )

    def ensure_session_fact(
        self,
        *,
        session_id: str,
        agent_name: str,
        session_label: str,
        provider: str,
        model: str,
        source: str = "live",
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO analytics_session_facts (
                  session_id, agent_name, session_label, provider, model, started_at, source
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                  agent_name=excluded.agent_name,
                  session_label=excluded.session_label,
                  provider=excluded.provider,
                  model=excluded.model
                """,
                (session_id, agent_name, session_label, provider, model, _utcnow(), source),
            )

    def mark_session_ended(self, session_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE analytics_session_facts SET ended_at=? WHERE session_id=?",
                (_utcnow(), session_id),
            )

    def log_activity(
        self,
        *,
        session_id: str,
        agent_name: str,
        event_type: str,
        subtype: str = "",
        turn_seq: int | None = None,
        metadata: dict | None = None,
        ts: str | None = None,
    ) -> None:
        when = ts or _utcnow()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO analytics_activity_events (
                  session_id, agent_name, ts, event_type, subtype, turn_seq, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    agent_name,
                    when,
                    event_type,
                    subtype or None,
                    turn_seq,
                    json.dumps(metadata) if metadata else None,
                ),
            )
            self._mark_dirty(conn, agent_name=agent_name, ts=when, reason=event_type)

    def log_turn_usage(
        self,
        *,
        session_id: str,
        agent_name: str,
        turn_seq: int,
        provider: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cached_input_tokens: int,
        error: bool = False,
        source_event_id: str = "",
        ts: str | None = None,
        user_message_snippet: str = "",
    ) -> None:
        when = ts or _utcnow()
        # Truncate snippet to 500 chars to keep DB lean
        snippet = (user_message_snippet or "")[:500]
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO analytics_turn_usage (
                  session_id, agent_name, turn_seq, ts, provider, model,
                  input_tokens, output_tokens, cached_input_tokens, error,
                  source_event_id, user_message_snippet
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id, turn_seq) DO UPDATE SET
                  ts=excluded.ts,
                  provider=excluded.provider,
                  model=excluded.model,
                  input_tokens=excluded.input_tokens,
                  output_tokens=excluded.output_tokens,
                  cached_input_tokens=excluded.cached_input_tokens,
                  error=excluded.error,
                  source_event_id=excluded.source_event_id,
                  user_message_snippet=excluded.user_message_snippet
                """,
                (
                    session_id,
                    agent_name,
                    turn_seq,
                    when,
                    provider,
                    model,
                    input_tokens,
                    output_tokens,
                    cached_input_tokens,
                    1 if error else 0,
                    source_event_id or None,
                    snippet or None,
                ),
            )
            self._mark_dirty(conn, agent_name=agent_name, ts=when, reason="turn_usage")

    def start_tool_call(
        self,
        *,
        session_id: str,
        agent_name: str,
        turn_seq: int | None,
        tool_call_key: str,
        tool_name: str,
        tool_namespace: str = "",
        metadata: dict | None = None,
        ts: str | None = None,
    ) -> None:
        when = ts or _utcnow()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO analytics_tool_calls (
                  session_id, agent_name, turn_seq, tool_call_key, tool_name,
                  tool_namespace, started_at, status, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'running', ?)
                """,
                (
                    session_id,
                    agent_name,
                    turn_seq,
                    tool_call_key or None,
                    tool_name,
                    tool_namespace or None,
                    when,
                    json.dumps(metadata) if metadata else None,
                ),
            )
            self._mark_dirty(conn, agent_name=agent_name, ts=when, reason="tool_start")

    def finish_tool_call(
        self,
        *,
        session_id: str,
        agent_name: str,
        tool_call_key: str,
        success: bool,
        error_type: str = "",
        metadata: dict | None = None,
        ts: str | None = None,
        status: str = "",
    ) -> None:
        """Finalize a tool call row.

        status override takes precedence; otherwise derived from success
        (ok when success else error). Valid values: ok | error | cancelled | timeout.
        """
        when = ts or _utcnow()
        final_status = status or ("ok" if success else "error")
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT id, started_at, metadata_json
                FROM analytics_tool_calls
                WHERE session_id=? AND tool_call_key=?
                ORDER BY id DESC
                LIMIT 1
                """,
                (session_id, tool_call_key),
            ).fetchone()
            if row:
                started_at = datetime.fromisoformat(row["started_at"].replace("Z", "+00:00"))
                finished_at = datetime.fromisoformat(when.replace("Z", "+00:00"))
                duration_ms = max(0, int((finished_at - started_at).total_seconds() * 1000))
                merged = json.loads(row["metadata_json"]) if row["metadata_json"] else {}
                if metadata:
                    merged.update(metadata)
                conn.execute(
                    """
                    UPDATE analytics_tool_calls
                    SET ended_at=?, duration_ms=?, success=?, error_type=?,
                        status=?, metadata_json=?
                    WHERE id=?
                    """,
                    (
                        when,
                        duration_ms,
                        1 if success else 0,
                        error_type or None,
                        final_status,
                        json.dumps(merged) if merged else None,
                        row["id"],
                    ),
                )
            self._mark_dirty(conn, agent_name=agent_name, ts=when, reason="tool_finish")

    def sweep_orphan_tool_calls(
        self,
        *,
        older_than_seconds: int = 3600,
        now_ts: str | None = None,
    ) -> int:
        """Mark tool calls still in 'running' older than threshold as 'orphan'.

        Runs on a schedule to close out rows where finish_tool_call never fired
        (crash, process kill, exceptions). Sets ended_at=now, duration_ms computed,
        success=0, error_type='orphan', status='orphan'. Returns count updated.
        """
        when = now_ts or _utcnow()
        cutoff = datetime.fromisoformat(when.replace("Z", "+00:00")) - timedelta(
            seconds=max(1, older_than_seconds)
        )
        cutoff_str = cutoff.isoformat().replace("+00:00", "Z")
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, started_at FROM analytics_tool_calls
                WHERE status='running' AND started_at < ?
                """,
                (cutoff_str,),
            ).fetchall()
            count = 0
            finished_at = datetime.fromisoformat(when.replace("Z", "+00:00"))
            for row in rows:
                started_at = datetime.fromisoformat(row["started_at"].replace("Z", "+00:00"))
                duration_ms = max(0, int((finished_at - started_at).total_seconds() * 1000))
                conn.execute(
                    """
                    UPDATE analytics_tool_calls
                    SET ended_at=?, duration_ms=?, success=0,
                        error_type='orphan', status='orphan'
                    WHERE id=?
                    """,
                    (when, duration_ms, row["id"]),
                )
                count += 1
            return count

    def prune_tool_calls(self, *, retention_days: int = 30, now_ts: str | None = None) -> int:
        """Delete tool_calls rows older than retention_days. Returns deleted count."""
        when = now_ts or _utcnow()
        cutoff = datetime.fromisoformat(when.replace("Z", "+00:00")) - timedelta(
            days=max(1, retention_days)
        )
        cutoff_str = cutoff.isoformat().replace("+00:00", "Z")
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM analytics_tool_calls WHERE started_at < ?",
                (cutoff_str,),
            )
            return cur.rowcount or 0

    def get_recent_tool_calls(
        self,
        *,
        agent_name: str = "",
        session_id: str = "",
        limit: int = 20,
    ) -> list[dict]:
        """Return most-recent tool_calls filtered by agent and/or session.

        Primary investigative helper for 'what happened before this agent got stuck'.
        Returns newest-first, deserialized metadata.
        """
        clauses = []
        params: list = []
        if agent_name:
            clauses.append("agent_name = ?")
            params.append(agent_name)
        if session_id:
            clauses.append("session_id = ?")
            params.append(session_id)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(max(1, min(limit, 500)))
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT id, session_id, agent_name, turn_seq, tool_call_key,
                       tool_name, tool_namespace, started_at, ended_at,
                       duration_ms, success, error_type, status, metadata_json
                FROM analytics_tool_calls
                {where}
                ORDER BY started_at DESC, id DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        out: list[dict] = []
        for row in rows:
            item = dict(row)
            raw = item.pop("metadata_json", None)
            try:
                item["metadata"] = json.loads(raw) if raw else {}
            except Exception:
                item["metadata"] = {}
            out.append(item)
        return out

    def get_overview(self, range_name: str = "7d") -> dict:
        start_ts, end_ts = _range_bounds(range_name)
        usage_rows = self._fetch_usage_rows(start_ts=start_ts, end_ts=end_ts)
        totals_map = {
            "input_tokens": 0,
            "output_tokens": 0,
            "cached_input_tokens": 0,
            "cost_usd": 0.0,
        }
        trend_map: dict[str, dict] = {}
        agent_map: dict[str, dict] = {}
        session_ids: set[str] = set()
        for row in usage_rows:
            cost_usd = self._compute_usage_cost(row)
            totals_map["input_tokens"] += int(row["input_tokens"])
            totals_map["output_tokens"] += int(row["output_tokens"])
            totals_map["cached_input_tokens"] += int(row["cached_input_tokens"])
            totals_map["cost_usd"] += cost_usd
            session_ids.add(row["session_id"])
            bucket = row["ts"][:10]
            bucket_row = trend_map.setdefault(
                bucket,
                {
                    "bucket": bucket, "cost_usd": 0.0,
                    "input_tokens": 0, "output_tokens": 0, "cached_input_tokens": 0,
                    "sessions": set(),
                },
            )
            bucket_row["cost_usd"] += cost_usd
            bucket_row["input_tokens"] += int(row["input_tokens"])
            bucket_row["output_tokens"] += int(row["output_tokens"])
            bucket_row["cached_input_tokens"] += int(row["cached_input_tokens"])
            bucket_row["sessions"].add(row["session_id"])
            agent_row = agent_map.setdefault(row["agent_name"], {"agent_name": row["agent_name"], "cost_usd": 0.0, "total_tokens": 0})
            agent_row["cost_usd"] += cost_usd
            agent_row["total_tokens"] += int(row["input_tokens"]) + int(row["output_tokens"]) + int(row["cached_input_tokens"])
        active_seconds = self._compute_active_seconds(range_name=range_name)

        # Compute previous-period totals for delta comparison
        prev_start, prev_end = _prev_range_bounds(range_name)
        prev_rows = self._fetch_usage_rows(start_ts=prev_start, end_ts=prev_end)
        prev_totals = {
            "input_tokens": 0, "output_tokens": 0, "cached_input_tokens": 0,
            "cost_usd": 0.0, "sessions": set(), "agents": set(),
        }
        for row in prev_rows:
            prev_totals["cost_usd"] += self._compute_usage_cost(row)
            prev_totals["input_tokens"] += int(row["input_tokens"])
            prev_totals["output_tokens"] += int(row["output_tokens"])
            prev_totals["cached_input_tokens"] += int(row["cached_input_tokens"])
            prev_totals["sessions"].add(row["session_id"])
            prev_totals["agents"].add(row["agent_name"])
        prev_active = self._compute_active_seconds(start_ts=prev_start, end_ts=prev_end)

        def _delta_pct(current: float, previous: float) -> float | None:
            if previous == 0:
                return None
            return round((current - previous) / previous * 100, 1)

        totals = {
            "cost_usd": round(totals_map["cost_usd"], 4),
            "active_hours": round(active_seconds / 3600, 2),
            "input_tokens": totals_map["input_tokens"],
            "output_tokens": totals_map["output_tokens"],
            "cached_input_tokens": totals_map["cached_input_tokens"],
            "agent_count": len(agent_map),
            "sessions_count": len(session_ids),
        }

        return {
            "range": range_name,
            "totals": totals,
            "deltas": {
                "cost_usd": _delta_pct(totals_map["cost_usd"], prev_totals["cost_usd"]),
                "active_hours": _delta_pct(active_seconds / 3600, prev_active / 3600),
                "input_tokens": _delta_pct(totals_map["input_tokens"], prev_totals["input_tokens"]),
                "output_tokens": _delta_pct(totals_map["output_tokens"], prev_totals["output_tokens"]),
                "sessions_count": _delta_pct(len(session_ids), len(prev_totals["sessions"])),
                "total_tokens": _delta_pct(
                    totals_map["input_tokens"] + totals_map["output_tokens"] + totals_map["cached_input_tokens"],
                    prev_totals["input_tokens"] + prev_totals["output_tokens"] + prev_totals["cached_input_tokens"],
                ),
            },
            "trend": [
                {
                    "bucket": bucket_row["bucket"],
                    "cost_usd": round(bucket_row["cost_usd"], 4),
                    "input_tokens": bucket_row["input_tokens"],
                    "output_tokens": bucket_row["output_tokens"],
                    "cached_input_tokens": bucket_row["cached_input_tokens"],
                    "sessions_count": len(bucket_row["sessions"]),
                }
                for bucket_row in sorted(trend_map.values(), key=lambda row: row["bucket"])
            ],
            "top_agents": sorted(agent_map.values(), key=lambda row: (-row["cost_usd"], -row["total_tokens"], row["agent_name"]))[:10],
        }

    def list_agents(self, range_name: str = "7d") -> dict:
        start_ts, end_ts = _range_bounds(range_name)
        usage_rows = self._fetch_usage_rows(start_ts=start_ts, end_ts=end_ts)
        agent_map: dict[str, dict] = {}
        for row in usage_rows:
            entry = agent_map.setdefault(
                row["agent_name"],
                {
                    "agent_name": row["agent_name"],
                    "cost_usd": 0.0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cached_input_tokens": 0,
                    "session_ids": set(),
                    "turns_count": 0,
                },
            )
            entry["cost_usd"] += self._compute_usage_cost(row)
            entry["input_tokens"] += int(row["input_tokens"])
            entry["output_tokens"] += int(row["output_tokens"])
            entry["cached_input_tokens"] += int(row["cached_input_tokens"])
            entry["session_ids"].add(row["session_id"])
            entry["turns_count"] += 1
        agents = []
        for entry in agent_map.values():
            active_seconds = self._compute_active_seconds(range_name=range_name, agent_name=entry["agent_name"])
            agents.append(
                {
                    "agent_name": entry["agent_name"],
                    "cost_usd": round(entry["cost_usd"], 4),
                    "active_hours": round(active_seconds / 3600, 2),
                    "input_tokens": entry["input_tokens"],
                    "output_tokens": entry["output_tokens"],
                    "cached_input_tokens": entry["cached_input_tokens"],
                    "sessions_count": len(entry["session_ids"]),
                    "turns_count": entry["turns_count"],
                }
            )
        agents.sort(key=lambda row: (-row["cost_usd"], -(row["input_tokens"] + row["output_tokens"] + row["cached_input_tokens"]), row["agent_name"]))
        return {"range": range_name, "agents": agents, "count": len(agents)}

    def get_agent_detail(self, agent_name: str, range_name: str = "7d") -> dict:
        start_ts, end_ts = _range_bounds(range_name)
        usage_rows = self._fetch_usage_rows(start_ts=start_ts, end_ts=end_ts, agent_name=agent_name)
        totals_map = {
            "cost_usd": 0.0,
            "input_tokens": 0,
            "output_tokens": 0,
            "cached_input_tokens": 0,
            "session_ids": set(),
            "turns_count": 0,
        }
        trend_map: dict[str, dict] = {}
        for row in usage_rows:
            cost_usd = self._compute_usage_cost(row)
            totals_map["cost_usd"] += cost_usd
            totals_map["input_tokens"] += int(row["input_tokens"])
            totals_map["output_tokens"] += int(row["output_tokens"])
            totals_map["cached_input_tokens"] += int(row["cached_input_tokens"])
            totals_map["session_ids"].add(row["session_id"])
            totals_map["turns_count"] += 1
            bucket = row["ts"][:10]
            bucket_row = trend_map.setdefault(
                bucket,
                {"bucket": bucket, "cost_usd": 0.0, "input_tokens": 0, "output_tokens": 0, "cached_input_tokens": 0},
            )
            bucket_row["cost_usd"] += cost_usd
            bucket_row["input_tokens"] += int(row["input_tokens"])
            bucket_row["output_tokens"] += int(row["output_tokens"])
            bucket_row["cached_input_tokens"] += int(row["cached_input_tokens"])
        with self._connect() as conn:
            sessions = conn.execute(
                """
                SELECT session_id, model, started_at, ended_at
                FROM analytics_session_facts
                WHERE agent_name=? AND started_at <= ? AND COALESCE(ended_at, ?) >= ?
                ORDER BY started_at DESC
                LIMIT 50
                """,
                (agent_name, end_ts, end_ts, start_ts),
            ).fetchall()
            tools = conn.execute(
                """
                SELECT tool_name,
                       COUNT(*) AS calls,
                       COALESCE(SUM(duration_ms), 0) AS total_duration_ms
                FROM analytics_tool_calls
                WHERE agent_name=? AND started_at >= ? AND started_at <= ?
                GROUP BY tool_name
                ORDER BY calls DESC, tool_name
                LIMIT 20
                """,
                (agent_name, start_ts, end_ts),
            ).fetchall()
        return {
            "agent_name": agent_name,
            "range": range_name,
            "totals": {
                "cost_usd": round(totals_map["cost_usd"], 4),
                "active_hours": round(self._compute_active_seconds(range_name=range_name, agent_name=agent_name) / 3600, 2),
                "input_tokens": totals_map["input_tokens"],
                "output_tokens": totals_map["output_tokens"],
                "cached_input_tokens": totals_map["cached_input_tokens"],
                "sessions_count": len(totals_map["session_ids"]),
                "turns_count": totals_map["turns_count"],
            },
            "trend": [
                {
                    "bucket": row["bucket"],
                    "cost_usd": round(row["cost_usd"], 4),
                    "input_tokens": row["input_tokens"],
                    "output_tokens": row["output_tokens"],
                    "cached_input_tokens": row["cached_input_tokens"],
                }
                for row in sorted(trend_map.values(), key=lambda item: item["bucket"])
            ],
            "tools": [
                {
                    "tool_name": row["tool_name"],
                    "calls": int(row["calls"]),
                    "total_duration_ms": int(row["total_duration_ms"]),
                }
                for row in tools
            ],
            "sessions": [dict(row) for row in sessions],
        }

    # ── Turn classification ────────────────────────────────

    CLASSIFICATION_VERSION = "heuristic-v2"

    # Tool sets for category classification
    _EDIT_TOOLS = {"Edit", "Write", "NotebookEdit"}
    _READ_TOOLS = {
        "Read", "Grep", "Glob", "WebSearch", "WebFetch",
        "web_search", "web_fetch", "search", "scrape", "crawl", "extract",
        "recall", "introspect", "memory_query", "kg_query",
        "kb_search", "kb_get_wiki", "ToolSearch",
    }
    _MESSAGING_TOOLS = {
        "send", "thread", "react", "broadcast",
        "send_gif", "send_voice", "send_photo", "send_document", "send_video",
        "send_message", "add_reaction", "send_voice_note",
    }
    _DELEGATION_TOOLS = {"Agent", "send_to_agent"}
    _PLAN_TOOLS = {"EnterPlanMode", "TodoWrite", "create_task", "bulk_create_tasks"}
    _SKILL_TOOLS = {"Skill", "load_skill", "install_skill"}

    # Bash command patterns (compiled regexes)
    _TEST_PATTERNS = re.compile(
        r"(?:^|\s|&&|\|)"
        r"(?:pytest|python[3]?\s+-m\s+pytest|unittest|npm\s+(?:run\s+)?test"
        r"|cargo\s+test|go\s+test|bun\s+test|vitest|jest)"
        r"(?:\s|$|;|\|)",
        re.IGNORECASE,
    )
    _GIT_PATTERNS = re.compile(
        r"\bgit\s+(?:push|pull|commit|merge|rebase|checkout|branch|stash"
        r"|log|diff|status|add|reset|cherry-pick|tag)\b",
        re.IGNORECASE,
    )
    _BUILD_PATTERNS = re.compile(
        r"\b(?:npm\s+run\s+build|npm\s+publish|pip\s+install|docker"
        r"|deploy|make\s+build|npm\s+run\s+dev|npm\s+start|pm2"
        r"|systemctl|cargo\s+build|sync\.sh)\b",
        re.IGNORECASE,
    )

    # User message keyword patterns for refinement
    _DEBUG_KEYWORDS = re.compile(
        r"\b(?:fix|bug|error|broken|failing|crash|issue|debug"
        r"|traceback|exception|not\s+working|wrong|unexpected)\b",
        re.IGNORECASE,
    )
    _FEATURE_KEYWORDS = re.compile(
        r"\b(?:add|create|implement|new|build|feature|introduce"
        r"|set\s*up|scaffold|generate)\b",
        re.IGNORECASE,
    )
    _REFACTOR_KEYWORDS = re.compile(
        r"\b(?:refactor|clean\s*up|rename|reorganize|simplify"
        r"|extract|restructure|move|migrate|split)\b",
        re.IGNORECASE,
    )
    _BRAINSTORM_KEYWORDS = re.compile(
        r"\b(?:brainstorm|idea|what\s+if|explore|think\s+about"
        r"|approach|strategy|design|consider|how\s+should"
        r"|what\s+would|opinion|suggest|recommend)\b",
        re.IGNORECASE,
    )
    _RESEARCH_KEYWORDS = re.compile(
        r"\b(?:research|investigate|look\s+into|find\s+out|check"
        r"|search|analyze|review|understand|explain|how\s+does"
        r"|what\s+is|show\s+me|compare)\b",
        re.IGNORECASE,
    )

    @staticmethod
    def _tool_basename(name: str) -> str:
        """Extract the base tool name from namespaced formats.

        Handles MCP style (mcp__server__tool) and dotted style (functions.exec_command).
        """
        if "__" in name:
            return name.rsplit("__", 1)[-1]
        if "." in name:
            return name.rsplit(".", 1)[-1]
        return name

    def _classify_turn(
        self,
        tool_names: list[str],
        bash_commands: list[str],
        *,
        output_tokens: int = 0,
        user_message: str = "",
    ) -> str:
        """Classify a turn into one of 13 categories.

        Two-layer heuristic:
        1. Tool-based classification (what tools were used)
        2. Keyword refinement (what the user asked for)

        Categories: coding, debugging, feature, refactoring, testing,
        exploration, planning, delegation, git, build_deploy, messaging,
        conversation, general
        """
        basenames = {self._tool_basename(t) for t in tool_names}

        # ── Layer 1: tool-based classification ──

        # Plan mode / task tools
        if basenames & self._PLAN_TOOLS:
            return "planning"

        # Agent delegation
        if basenames & self._DELEGATION_TOOLS:
            return "delegation"

        # Messaging
        if basenames & self._MESSAGING_TOOLS:
            return "messaging"

        has_edits = bool(basenames & self._EDIT_TOOLS)
        has_reads = bool(basenames & self._READ_TOOLS)
        has_bash = "Bash" in basenames
        has_skill = bool(basenames & self._SKILL_TOOLS)

        # Bash command sub-classification
        if has_bash and bash_commands:
            n_cmds = len(bash_commands)
            n_test = sum(1 for c in bash_commands if self._TEST_PATTERNS.search(c))
            n_git = sum(1 for c in bash_commands if self._GIT_PATTERNS.search(c))
            n_build = sum(1 for c in bash_commands if self._BUILD_PATTERNS.search(c))

            # Testing wins if any test command is present (high signal)
            if n_test > 0:
                return "testing"
            # Git/build only win if they're the majority action in the turn
            if not has_edits and n_git > 0 and n_git >= n_cmds * 0.5:
                return "git"
            if not has_edits and n_build > 0 and n_build >= n_cmds * 0.5:
                return "build_deploy"

        # Coding (edits or bash with commands)
        if has_edits:
            return self._refine_coding(user_message)

        if has_bash and bash_commands:
            return self._refine_coding(user_message)

        # Exploration (reads, search, web)
        if has_reads:
            return self._refine_exploration(user_message)

        # Skill tool
        if has_skill:
            return "general"

        # ── Layer 2: no-tool turns ──

        if not tool_names:
            if output_tokens > 0 and user_message:
                return self._classify_conversation(user_message)
            if output_tokens > 0:
                return "conversation"
            return "general"

        return "general"

    def _refine_coding(self, user_message: str) -> str:
        """Refine a coding turn using user message keywords."""
        if not user_message:
            return "coding"
        if self._DEBUG_KEYWORDS.search(user_message):
            return "debugging"
        if self._REFACTOR_KEYWORDS.search(user_message):
            return "refactoring"
        if self._FEATURE_KEYWORDS.search(user_message):
            return "feature"
        return "coding"

    def _refine_exploration(self, user_message: str) -> str:
        """Refine an exploration turn using user message keywords."""
        if not user_message:
            return "exploration"
        if self._DEBUG_KEYWORDS.search(user_message):
            return "debugging"
        if self._RESEARCH_KEYWORDS.search(user_message):
            return "exploration"
        return "exploration"

    def _classify_conversation(self, user_message: str) -> str:
        """Classify a no-tool turn by user message keywords."""
        if self._BRAINSTORM_KEYWORDS.search(user_message):
            return "brainstorming"
        if self._RESEARCH_KEYWORDS.search(user_message):
            return "exploration"
        if self._DEBUG_KEYWORDS.search(user_message):
            return "debugging"
        if self._FEATURE_KEYWORDS.search(user_message):
            return "feature"
        return "conversation"

    def get_categories(self, range_name: str = "7d", agent_name: str = "") -> dict:
        """Get token usage breakdown by task category for the given range."""
        start_ts, end_ts = _range_bounds(range_name)

        # Fetch all turns with their tool calls and user message snippets.
        # Costed providers (Anthropic + OpenAI/Codex) so Codex agents show up in
        # the per-category cost breakdown at parity with Claude agents (#206).
        provider_placeholders = ",".join("?" for _ in _COSTED_PROVIDERS)
        usage_query = f"""
            SELECT u.session_id, u.agent_name, u.turn_seq, u.ts,
                   u.input_tokens, u.output_tokens, u.cached_input_tokens,
                   u.provider, u.model, u.user_message_snippet
            FROM analytics_turn_usage u
            WHERE u.ts >= ? AND u.ts <= ?
              AND u.provider IN ({provider_placeholders})
        """
        params: list = [start_ts, end_ts, *_COSTED_PROVIDERS]
        if agent_name:
            usage_query += " AND u.agent_name=?"
            params.append(agent_name)
        usage_query += " ORDER BY u.ts"

        with self._connect() as conn:
            turns = conn.execute(usage_query, params).fetchall()

            # Build tool lookup: (session_id, turn_seq) -> [tool_names]
            tool_query = """
                SELECT session_id, turn_seq, tool_name
                FROM analytics_tool_calls
                WHERE started_at >= ? AND started_at <= ?
            """
            tool_params: list = [start_ts, end_ts]
            if agent_name:
                tool_query += " AND agent_name=?"
                tool_params.append(agent_name)
            tool_rows = conn.execute(tool_query, tool_params).fetchall()

            # Also get bash command metadata for test detection
            bash_query = """
                SELECT session_id, turn_seq, metadata_json
                FROM analytics_tool_calls
                WHERE started_at >= ? AND started_at <= ?
                  AND (tool_name = 'Bash' OR tool_name LIKE '%__Bash')
            """
            if agent_name:
                bash_query += " AND agent_name=?"
            bash_rows = conn.execute(bash_query, tool_params).fetchall()

        # Index tools and bash commands by (session_id, turn_seq)
        tool_map: dict[tuple, list[str]] = {}
        for row in tool_rows:
            key = (row["session_id"], row["turn_seq"])
            tool_map.setdefault(key, []).append(row["tool_name"])

        bash_map: dict[tuple, list[str]] = {}
        for row in bash_rows:
            key = (row["session_id"], row["turn_seq"])
            meta = row["metadata_json"]
            if meta:
                try:
                    import json as _json
                    parsed = _json.loads(meta)
                    cmd = parsed.get("command", "") if isinstance(parsed, dict) else ""
                    if cmd:
                        bash_map.setdefault(key, []).append(cmd)
                except Exception:
                    pass

        # Classify each turn and aggregate
        categories: dict[str, dict] = {}
        for turn in turns:
            key = (turn["session_id"], turn["turn_seq"])
            tools = tool_map.get(key, [])
            bash_cmds = bash_map.get(key, [])
            user_msg = turn["user_message_snippet"] or ""
            category = self._classify_turn(
                tools, bash_cmds,
                output_tokens=int(turn["output_tokens"]),
                user_message=user_msg,
            )

            entry = categories.setdefault(category, {
                "category": category,
                "input_tokens": 0,
                "output_tokens": 0,
                "cached_input_tokens": 0,
                "cost_usd": 0.0,
                "turns": 0,
            })
            entry["input_tokens"] += int(turn["input_tokens"])
            entry["output_tokens"] += int(turn["output_tokens"])
            entry["cached_input_tokens"] += int(turn["cached_input_tokens"])
            entry["cost_usd"] += self._compute_usage_cost(turn)
            entry["turns"] += 1

        result = sorted(
            categories.values(),
            key=lambda c: -(c["input_tokens"] + c["output_tokens"] + c["cached_input_tokens"]),
        )
        for r in result:
            r["cost_usd"] = round(r["cost_usd"], 4)

        return {
            "range": range_name,
            "classification_version": self.CLASSIFICATION_VERSION,
            "categories": result,
        }

    def get_hourly(
        self,
        range_name: str = "7d",
        agent_name: str = "",
        timezone: str = "America/Los_Angeles",
    ) -> dict:
        """Get token usage by hour-of-day with historical averages."""
        import zoneinfo

        try:
            tz = zoneinfo.ZoneInfo(timezone)
        except Exception:
            tz = zoneinfo.ZoneInfo("America/Los_Angeles")

        start_ts, end_ts = _range_bounds(range_name)

        # Fetch current period turns (costed providers incl. Codex/OpenAI, #206)
        # so the hourly token chart reconciles with the overview totals.
        provider_placeholders = ",".join("?" for _ in _COSTED_PROVIDERS)
        query = f"""
            SELECT ts, input_tokens, output_tokens, cached_input_tokens
            FROM analytics_turn_usage
            WHERE ts >= ? AND ts <= ?
              AND provider IN ({provider_placeholders})
        """
        params: list = [start_ts, end_ts, *_COSTED_PROVIDERS]
        if agent_name:
            query += " AND agent_name=?"
            params.append(agent_name)

        # Fetch historical turns for average (capped to 90 days before current range)
        hist_start = (
            datetime.fromisoformat(start_ts.replace("Z", "+00:00"))
            - timedelta(days=90)
        ).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        hist_query = f"""
            SELECT ts, input_tokens, output_tokens, cached_input_tokens
            FROM analytics_turn_usage
            WHERE ts >= ? AND ts < ?
              AND provider IN ({provider_placeholders})
        """
        hist_params: list = [hist_start, start_ts, *_COSTED_PROVIDERS]
        if agent_name:
            hist_query += " AND agent_name=?"
            hist_params.append(agent_name)

        with self._connect() as conn:
            current_rows = conn.execute(query, params).fetchall()
            hist_rows = conn.execute(hist_query, hist_params).fetchall()

        def _to_local_hour(ts_str: str) -> int:
            dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            return dt.astimezone(tz).hour

        def _to_local_date(ts_str: str) -> str:
            dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            return dt.astimezone(tz).strftime("%Y-%m-%d")

        # Current period: aggregate by hour
        hours: dict[int, dict] = {h: {
            "hour": h, "input_tokens": 0, "output_tokens": 0,
            "cached_input_tokens": 0, "turns": 0,
        } for h in range(24)}

        for row in current_rows:
            h = _to_local_hour(row["ts"])
            hours[h]["input_tokens"] += int(row["input_tokens"])
            hours[h]["output_tokens"] += int(row["output_tokens"])
            hours[h]["cached_input_tokens"] += int(row["cached_input_tokens"])
            hours[h]["turns"] += 1

        # Historical average: aggregate by (day, hour), then average per hour
        # Uses fresh tokens (input + output, no cached) for meaningful comparison
        hist_day_hour: dict[tuple[str, int], int] = {}  # (day, hour) -> fresh_tokens
        hist_days: set[str] = set()
        for row in hist_rows:
            h = _to_local_hour(row["ts"])
            day = _to_local_date(row["ts"])
            hist_days.add(day)
            key = (day, h)
            fresh = int(row["input_tokens"]) + int(row["output_tokens"])
            hist_day_hour[key] = hist_day_hour.get(key, 0) + fresh

        num_hist_days = max(1, len(hist_days))
        hist_avg: dict[int, float] = {}
        for h in range(24):
            hour_total = sum(v for (d, hr), v in hist_day_hour.items() if hr == h)
            hist_avg[h] = round(hour_total / num_hist_days)

        return {
            "range": range_name,
            "timezone": timezone,
            "hours": [
                {
                    **hours[h],
                    "total_tokens": (
                        hours[h]["input_tokens"]
                        + hours[h]["output_tokens"]
                        + hours[h]["cached_input_tokens"]
                    ),
                    "fresh_tokens": (
                        hours[h]["input_tokens"]
                        + hours[h]["output_tokens"]
                    ),
                    "historical_avg": hist_avg.get(h, 0),
                }
                for h in range(24)
            ],
            "historical_days": num_hist_days,
            "historical_window_days": 90,
            "historical_avg_type": "active_day",
        }

    def _fetch_usage_rows(
        self,
        *,
        start_ts: str,
        end_ts: str,
        agent_name: str = "",
        costed_only: bool = True,
    ) -> list[sqlite3.Row]:
        query = """
            SELECT session_id, agent_name, ts, provider, model, input_tokens, output_tokens, cached_input_tokens
            FROM analytics_turn_usage
            WHERE ts >= ? AND ts <= ?
        """
        params: list = [start_ts, end_ts]
        if agent_name:
            query += " AND agent_name=?"
            params.append(agent_name)
        if costed_only:
            placeholders = ",".join("?" for _ in _COSTED_PROVIDERS)
            query += f" AND provider IN ({placeholders})"
            params.extend(_COSTED_PROVIDERS)
        query += " ORDER BY ts"
        with self._connect() as conn:
            return conn.execute(query, params).fetchall()

    def _compute_usage_cost(self, row: sqlite3.Row) -> float:
        pricing = self._lookup_pricing(
            provider=row["provider"],
            model=row["model"],
            ts=row["ts"],
        )
        if not pricing:
            return 0.0
        input_cost = (int(row["input_tokens"]) / 1_000_000) * float(pricing["input_usd_per_mtok"])
        output_cost = (int(row["output_tokens"]) / 1_000_000) * float(pricing["output_usd_per_mtok"])
        cached_cost = (int(row["cached_input_tokens"]) / 1_000_000) * float(pricing["cached_input_usd_per_mtok"])
        return input_cost + output_cost + cached_cost

    def _pricing_table(self) -> dict[tuple[str, str], list[dict]]:
        """Pricing rows keyed by (provider, model), newest effective_from first."""
        if self._pricing_cache is None:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT * FROM analytics_model_pricing ORDER BY effective_from DESC"
                ).fetchall()
            table: dict[tuple[str, str], list[dict]] = {}
            for row in rows:
                table.setdefault((row["provider"], row["model"]), []).append(dict(row))
            self._pricing_cache = table
        return self._pricing_cache

    def _lookup_pricing(self, *, provider: str, model: str, ts: str) -> dict | None:
        table = self._pricing_table()
        provider_aliases = [provider or "", self._provider_alias(provider)]
        model_aliases = self._model_aliases(model)
        for provider_name in provider_aliases:
            for model_name in model_aliases:
                for row in table.get((provider_name, model_name), []):
                    if row["effective_from"] <= ts and (
                        row["effective_to"] is None or row["effective_to"] > ts
                    ):
                        return row
        return None

    def _provider_alias(self, provider: str) -> str:
        raw = (provider or "").strip().lower()
        if raw in {"codex_cli", "openai", "openai_api"}:
            return "openai"
        if raw in {"anthropic", "claude", "claude_code"}:
            return "anthropic"
        return raw

    def _model_aliases(self, model: str) -> list[str]:
        raw = (model or "").strip()
        if not raw:
            return [raw]
        aliases = [raw, raw.lower()]
        lowered = raw.lower()
        if lowered.endswith("-latest"):
            aliases.append(lowered.removesuffix("-latest"))
        aliases.append(lowered.replace("_", "-"))
        return list(dict.fromkeys([alias for alias in aliases if alias]))

    def _compute_active_seconds(
        self,
        *,
        range_name: str = "",
        agent_name: str = "",
        idle_gap_seconds: int = 300,
        start_ts: str = "",
        end_ts: str = "",
    ) -> int:
        if not start_ts or not end_ts:
            start_ts, end_ts = _range_bounds(range_name)
        query = """
            SELECT session_id, agent_name, ts
            FROM analytics_activity_events
            WHERE ts >= ? AND ts <= ?
        """
        params: list = [start_ts, end_ts]
        if agent_name:
            query += " AND agent_name=?"
            params.append(agent_name)
        query += " ORDER BY agent_name, session_id, ts"
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        total = 0
        current_key = None
        span_start = None
        prev_ts = None
        for row in rows:
            key = (row["agent_name"], row["session_id"])
            ts = datetime.fromisoformat(row["ts"].replace("Z", "+00:00"))
            if key != current_key or prev_ts is None or (ts - prev_ts).total_seconds() > idle_gap_seconds:
                if span_start and prev_ts:
                    total += max(15, int((prev_ts - span_start).total_seconds()))
                current_key = key
                span_start = ts
            prev_ts = ts
        if span_start and prev_ts:
            total += max(15, int((prev_ts - span_start).total_seconds()))
        return total

    def _mark_dirty(self, conn, *, agent_name: str, ts: str, reason: str) -> None:
        day = ts[:10]
        conn.execute(
            """
            INSERT INTO analytics_rollup_dirty(day, agent_name, reason, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(day, agent_name) DO UPDATE SET
              reason=excluded.reason,
              updated_at=excluded.updated_at
            """,
            (day, agent_name, reason, _utcnow()),
        )
