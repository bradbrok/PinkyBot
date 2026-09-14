"""Catalog-owned SQLite policy overrides, approval CAS, and decision audit."""

from __future__ import annotations

import secrets
import sqlite3
import threading
import time
from pathlib import Path

from pinky_daemon.store_catalog import (
    StoreCatalog,
    apply_store_connection_policy,
    open_store_connection,
    store_connection_policy,
)


class ToolPolicyStore:
    def __init__(self, db_path: str, *, catalog: StoreCatalog | None = None):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db_path = db_path
        self._catalog = catalog
        self._thread_local = threading.local()
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS tool_policy_overrides (
                id INTEGER PRIMARY KEY, agent_name TEXT NOT NULL, pattern TEXT NOT NULL,
                rule_id TEXT, decision TEXT NOT NULL CHECK(decision IN ('allow','deny','pause')),
                note TEXT NOT NULL, created_by TEXT NOT NULL, created_at REAL NOT NULL,
                valid_until REAL);
            CREATE INDEX IF NOT EXISTS idx_tool_policy_overrides_agent
                ON tool_policy_overrides(agent_name);
            CREATE TABLE IF NOT EXISTS tool_policy_pending (
                pending_id TEXT PRIMARY KEY, agent_name TEXT NOT NULL, session_id TEXT NOT NULL,
                tool_use_id TEXT NOT NULL, tool_name TEXT NOT NULL, input_sha256 TEXT NOT NULL,
                summary TEXT NOT NULL, created_at REAL NOT NULL, deadline_ts REAL NOT NULL,
                state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','resolved')),
                result TEXT, resolved_by TEXT, reason TEXT, resolved_at REAL,
                eval_type TEXT, reason_code TEXT, principal_class TEXT, rule_id TEXT,
                UNIQUE(agent_name, tool_use_id));
            CREATE INDEX IF NOT EXISTS idx_tool_policy_pending_deadline
                ON tool_policy_pending(state, deadline_ts);
            CREATE TABLE IF NOT EXISTS tool_policy_decisions (
                id INTEGER PRIMARY KEY, agent_name TEXT NOT NULL, session_id TEXT NOT NULL,
                tool_use_id TEXT NOT NULL, tool_name TEXT NOT NULL, evaluated_permission TEXT NOT NULL,
                eval_type TEXT NOT NULL, rule_id TEXT, reason_code TEXT NOT NULL,
                principal_class TEXT NOT NULL, input_sha256 TEXT NOT NULL, result TEXT,
                resolved_by TEXT, latency_ms REAL NOT NULL, created_at REAL NOT NULL,
                hook_sha256 TEXT, settings_sha256 TEXT);
            CREATE INDEX IF NOT EXISTS idx_tool_policy_decisions_agent_time
                ON tool_policy_decisions(agent_name, created_at);
            CREATE INDEX IF NOT EXISTS idx_tool_policy_decisions_agent_tool_use
                ON tool_policy_decisions(agent_name, tool_use_id);
            CREATE TABLE IF NOT EXISTS tool_policy_allow_counts (
                agent_name TEXT NOT NULL, rule_id TEXT NOT NULL, day TEXT NOT NULL,
                n INTEGER NOT NULL, PRIMARY KEY(agent_name, rule_id, day));
        """)
        self._ensure_columns()
        self._db.commit()

    @property
    def _db(self):
        connection = getattr(self._thread_local, "connection", None)
        if connection is None:
            connection = open_store_connection(self._catalog, "tool_policy", self._db_path,
                                               owner=type(self).__name__)
            journal = connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]
            apply_store_connection_policy(connection, store_connection_policy(self._catalog, "tool_policy"))
            connection.row_factory = sqlite3.Row
            if self._catalog is not None:
                self._catalog.register("tool_policy", self._db_path, journal_mode=journal,
                                       owner=type(self).__name__)
            self._thread_local.connection = connection
        return connection

    def _ensure_columns(self):
        for table, columns in (
            ("tool_policy_decisions", ("hook_sha256", "settings_sha256")),
            ("tool_policy_pending", ("eval_type", "reason_code", "principal_class", "rule_id")),
        ):
            existing = {row[1] for row in self._db.execute(f"PRAGMA table_info({table})")}
            for column in columns:
                if column not in existing:
                    self._db.execute(f"ALTER TABLE {table} ADD COLUMN {column} TEXT")

    def close(self):
        connection = getattr(self._thread_local, "connection", None)
        if connection is not None:
            connection.close()
            self._thread_local.connection = None

    def list_overrides(self, agent: str, now: float) -> list[dict]:
        return [dict(row) for row in self._db.execute(
            "SELECT * FROM tool_policy_overrides WHERE agent_name=? "
            "AND (valid_until IS NULL OR valid_until>?) ORDER BY id", (agent, now),
        )]

    def put_override(self, *, agent, pattern, decision, note, created_by, rule_id=None, valid_until=None):
        with self._db:
            cursor = self._db.execute(
                "INSERT INTO tool_policy_overrides "
                "(agent_name,pattern,rule_id,decision,note,created_by,created_at,valid_until) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (agent, pattern, rule_id, decision, note, created_by, time.time(), valid_until),
            )
        return cursor.lastrowid

    def delete_override(self, *, agent, override_id) -> bool:
        with self._db:
            cursor = self._db.execute("DELETE FROM tool_policy_overrides WHERE id=? AND agent_name=?",
                                      (override_id, agent))
        return cursor.rowcount == 1

    def create_pending(self, *, agent_name, session_id, tool_use_id, tool_name, input_sha256,
                       summary, created_at, deadline_ts, evaluation=None) -> str:
        pending_id = "tp_" + secrets.token_hex(8)
        detail = (evaluation or {}).get("evaluation", {})
        with self._db:
            self._db.execute(
                "INSERT INTO tool_policy_pending "
                "(pending_id,agent_name,session_id,tool_use_id,tool_name,input_sha256,summary,created_at,deadline_ts, "
                "eval_type,reason_code,principal_class,rule_id) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(agent_name,tool_use_id) DO NOTHING",
                (pending_id, agent_name, session_id, tool_use_id, tool_name, input_sha256,
                 summary, created_at, deadline_ts, detail.get("type"), detail.get("reason_code"),
                 detail.get("principal_class"), detail.get("rule_id")),
            )
            row = self._db.execute(
                "SELECT * FROM tool_policy_pending WHERE agent_name=? AND tool_use_id=?",
                (agent_name, tool_use_id),
            ).fetchone()
            if any(row[key] != value for key, value in (
                ("session_id", session_id), ("tool_name", tool_name), ("input_sha256", input_sha256),
            )):
                raise ValueError("pending binding mismatch")
        return row["pending_id"]

    def get_pending(self, pending_id) -> dict | None:
        row = self._db.execute("SELECT * FROM tool_policy_pending WHERE pending_id=?", (pending_id,)).fetchone()
        return dict(row) if row else None

    def resolve_pending(self, pending_id, *, agent, tool_use_id, input_sha256, result, resolved_by, reason):
        """CAS at write-lock acquisition time; a deadline crossed while waiting refuses approval.

        Expired rows remain pending for the route's expiry sweep, which owns the
        timeout audit and session event. No state is read before the conditional UPDATE.
        """
        if result not in {"allow", "deny"}:
            raise ValueError("invalid resolution")
        with self._db:
            self._db.execute("BEGIN IMMEDIATE")
            now = time.time()
            cursor = self._db.execute(
                "UPDATE tool_policy_pending SET state='resolved',result=?,resolved_by=?,reason=?,resolved_at=? "
                "WHERE pending_id=? AND agent_name=? AND tool_use_id=? AND input_sha256=? "
                "AND state='pending' AND deadline_ts>?",
                (result, resolved_by, reason, now, pending_id, agent, tool_use_id, input_sha256, now),
            )
        if cursor.rowcount == 1:
            return "resolved"
        row = self.get_pending(pending_id)
        if not row:
            return "not_found"
        if (row["agent_name"], row["tool_use_id"], row["input_sha256"]) != (agent, tool_use_id, input_sha256):
            return "binding_mismatch"
        if row["state"] == "pending" and row["deadline_ts"] <= now:
            return "expired"
        return "already_resolved"

    def expire_due(self, now: float) -> list[str]:
        with self._db:
            rows = self._db.execute(
                "UPDATE tool_policy_pending SET state='resolved',result='deny',resolved_by='timeout', "
                "reason='no owner decision within the approval window',resolved_at=? "
                "WHERE state='pending' AND deadline_ts<=? RETURNING pending_id", (now, now),
            ).fetchall()
        return sorted(row[0] for row in rows)

    def record_decision(self, *, agent_name, session_id, tool_use_id, tool_name, evaluation,
                        result=None, resolved_by=None, latency_ms=0, hook_sha256=None, settings_sha256=None):
        detail = evaluation["evaluation"]
        with self._db:
            cursor = self._db.execute(
                "INSERT INTO tool_policy_decisions (agent_name,session_id,tool_use_id,tool_name, "
                "evaluated_permission,eval_type,rule_id,reason_code,principal_class,input_sha256,result, "
                "resolved_by,latency_ms,created_at,hook_sha256,settings_sha256) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (agent_name, session_id, tool_use_id, tool_name, evaluation["evaluated_permission"],
                 detail["type"], detail.get("rule_id"), detail["reason_code"], detail["principal_class"],
                 detail["input_sha256"], result or evaluation["evaluated_permission"], resolved_by,
                 max(0, latency_ms), time.time(), hook_sha256, settings_sha256),
            )
        return cursor.lastrowid

    def bump_allow_count(self, *, agent, rule_id, day):
        with self._db:
            self._db.execute(
                "INSERT INTO tool_policy_allow_counts VALUES(?,?,?,1) "
                "ON CONFLICT(agent_name,rule_id,day) DO UPDATE SET n=n+1", (agent, rule_id, day),
            )

    def get_pause_decision(self, agent_name: str, tool_use_id: str) -> dict | None:
        """Read the original pause provenance through the agent/tool-use index."""
        row = self._db.execute(
            "SELECT * FROM tool_policy_decisions WHERE agent_name=? AND tool_use_id=? "
            "AND evaluated_permission='pause' ORDER BY id LIMIT 1", (agent_name, tool_use_id),
        ).fetchone()
        return dict(row) if row else None

    def list_decisions(self, agent, since, limit) -> list[dict]:
        return [dict(row) for row in self._db.execute(
            "SELECT * FROM tool_policy_decisions WHERE agent_name=? AND created_at>=? ORDER BY id DESC LIMIT ?",
            (agent, since, max(0, min(limit, 1000))),
        )]

    def count_pending(self) -> int:
        return self._db.execute("SELECT count(*) FROM tool_policy_pending WHERE state='pending'").fetchone()[0]

    def tamper_count_since(self, ts) -> int:
        return self._db.execute(
            "SELECT count(*) FROM tool_policy_decisions WHERE eval_type='tamper' AND created_at>=?", (ts,),
        ).fetchone()[0]
