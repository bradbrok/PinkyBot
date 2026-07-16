"""Durable routing context for message-based outreach tools."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

DEFAULT_RETENTION_DAYS = 30
DEFAULT_MAX_PER_AGENT = 1000


class MessageContextStore:
    """SQLite-backed message routing contexts with bounded retention."""

    def __init__(
        self,
        db_path: str,
        *,
        retention_days: int = DEFAULT_RETENTION_DAYS,
        max_per_agent: int = DEFAULT_MAX_PER_AGENT,
    ) -> None:
        self.db_path = db_path
        self.retention_days = max(1, retention_days)
        self.max_per_agent = max(1, max_per_agent)
        self._lock = threading.RLock()
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA busy_timeout=5000")
        self._create_schema()
        self._ensure_columns()
        self._ensure_identity_key()
        self._create_indexes()
        self.sweep_retention()

    @property
    def retention_seconds(self) -> float:
        return float(self.retention_days * 86400)

    def _create_schema(self) -> None:
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS message_contexts (
                agent_name       TEXT NOT NULL,
                message_id       TEXT NOT NULL,
                platform         TEXT NOT NULL,
                chat_id          TEXT NOT NULL,
                message_ts       REAL NOT NULL,
                reply_to         TEXT NOT NULL DEFAULT '',
                is_group         INTEGER NOT NULL DEFAULT 0,
                source_was_voice INTEGER NOT NULL DEFAULT 0,
                attachments_json TEXT NOT NULL DEFAULT '[]',
                metadata_json    TEXT NOT NULL DEFAULT '{}',
                stored_at        REAL NOT NULL,
                PRIMARY KEY (agent_name, platform, chat_id, message_id)
            );
            """
        )
        self._db.commit()

    def _ensure_identity_key(self) -> None:
        """Migrate legacy agent/message keys to chat-scoped message identity."""
        info = self._db.execute("PRAGMA table_info(message_contexts)").fetchall()
        primary_key = tuple(
            row["name"]
            for row in sorted((row for row in info if row["pk"]), key=lambda row: row["pk"])
        )
        expected = ("agent_name", "platform", "chat_id", "message_id")
        if primary_key == expected:
            return

        with self._lock:
            try:
                self._db.execute("BEGIN IMMEDIATE")
                self._db.execute("DROP TABLE IF EXISTS message_contexts_new")
                self._db.execute(
                    """
                    CREATE TABLE message_contexts_new (
                        agent_name       TEXT NOT NULL,
                        message_id       TEXT NOT NULL,
                        platform         TEXT NOT NULL,
                        chat_id          TEXT NOT NULL,
                        message_ts       REAL NOT NULL,
                        reply_to         TEXT NOT NULL DEFAULT '',
                        is_group         INTEGER NOT NULL DEFAULT 0,
                        source_was_voice INTEGER NOT NULL DEFAULT 0,
                        attachments_json TEXT NOT NULL DEFAULT '[]',
                        metadata_json    TEXT NOT NULL DEFAULT '{}',
                        stored_at        REAL NOT NULL,
                        PRIMARY KEY (agent_name, platform, chat_id, message_id)
                    )
                    """
                )
                self._db.execute(
                    """
                    INSERT INTO message_contexts_new (
                        agent_name, message_id, platform, chat_id, message_ts,
                        reply_to, is_group, source_was_voice, attachments_json,
                        metadata_json, stored_at
                    )
                    SELECT agent_name, message_id, platform, chat_id, message_ts,
                           reply_to, is_group, source_was_voice, attachments_json,
                           metadata_json, stored_at
                    FROM message_contexts
                    """
                )
                self._db.execute("DROP TABLE message_contexts")
                self._db.execute("ALTER TABLE message_contexts_new RENAME TO message_contexts")
                self._db.commit()
            except Exception:
                self._db.rollback()
                raise

    def _ensure_columns(self) -> None:
        """Add forward-compatible columns following the daemon store pattern."""
        migrations = [
            ("reply_to", "TEXT NOT NULL DEFAULT ''"),
            ("is_group", "INTEGER NOT NULL DEFAULT 0"),
            ("source_was_voice", "INTEGER NOT NULL DEFAULT 0"),
            ("attachments_json", "TEXT NOT NULL DEFAULT '[]'"),
            ("metadata_json", "TEXT NOT NULL DEFAULT '{}'"),
            ("stored_at", "REAL NOT NULL DEFAULT 0"),
        ]
        existing = {
            row["name"]
            for row in self._db.execute("PRAGMA table_info(message_contexts)").fetchall()
        }
        for column, column_type in migrations:
            if column not in existing:
                self._db.execute(
                    f"ALTER TABLE message_contexts ADD COLUMN {column} {column_type}"
                )
        if "stored_at" not in existing:
            self._db.execute(
                "UPDATE message_contexts SET stored_at=? WHERE stored_at=0",
                (time.time(),),
            )
        self._db.commit()

    def _create_indexes(self) -> None:
        self._db.executescript(
            """
            CREATE INDEX IF NOT EXISTS idx_message_contexts_agent_stored
                ON message_contexts(agent_name, stored_at DESC);
            CREATE INDEX IF NOT EXISTS idx_message_contexts_stored
                ON message_contexts(stored_at);
            """
        )
        self._db.commit()

    @staticmethod
    def _json_list(raw: str) -> list[dict]:
        try:
            value = json.loads(raw or "[]")
        except (TypeError, ValueError):
            return []
        if not isinstance(value, list):
            return []
        return [item for item in value if isinstance(item, dict)]

    @staticmethod
    def _json_dict(raw: str) -> dict:
        try:
            value = json.loads(raw or "{}")
        except (TypeError, ValueError):
            return {}
        return value if isinstance(value, dict) else {}

    def put(self, context: dict[str, Any], *, stored_at: float | None = None) -> None:
        """Insert or replace one agent-scoped message context."""
        agent_name = str(context.get("agent_name") or "")
        message_id = str(context.get("message_id") or "")
        platform = str(context.get("platform") or "")
        chat_id = str(context.get("chat_id") or "")
        if not agent_name or not message_id or not platform or not chat_id:
            return
        attachments = context.get("attachments")
        metadata = context.get("metadata")
        now = time.time() if stored_at is None else float(stored_at)
        with self._lock:
            try:
                self._db.execute(
                    """
                    INSERT INTO message_contexts (
                        agent_name, message_id, platform, chat_id, message_ts,
                        reply_to, is_group, source_was_voice, attachments_json,
                        metadata_json, stored_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(agent_name, platform, chat_id, message_id) DO UPDATE SET
                        message_ts=excluded.message_ts,
                        reply_to=excluded.reply_to,
                        is_group=excluded.is_group,
                        source_was_voice=excluded.source_was_voice,
                        attachments_json=excluded.attachments_json,
                        metadata_json=excluded.metadata_json,
                        stored_at=excluded.stored_at
                    """,
                    (
                        agent_name,
                        message_id,
                        platform,
                        chat_id,
                        float(context.get("timestamp") or 0),
                        str(context.get("reply_to") or ""),
                        1 if context.get("is_group") else 0,
                        1 if context.get("source_was_voice") else 0,
                        json.dumps(
                            attachments if isinstance(attachments, list) else [],
                            default=str,
                        ),
                        json.dumps(
                            metadata if isinstance(metadata, dict) else {},
                            default=str,
                        ),
                        now,
                    ),
                )
                self._sweep_retention_locked(now)
                self._db.commit()
            except Exception:
                self._db.rollback()
                raise

    def get(
        self,
        agent_name: str,
        message_id: str,
        *,
        platform: str | None = None,
        chat_id: str | None = None,
        include_stored_at: bool = False,
    ) -> dict[str, Any] | None:
        """Return one unambiguous, unexpired context for the owning agent."""
        cutoff = time.time() - self.retention_seconds
        identity_filter = ""
        params: list[Any] = [agent_name, message_id, cutoff]
        if platform is not None and chat_id is not None:
            identity_filter = " AND platform=? AND chat_id=?"
            params.extend((platform, chat_id))
        with self._lock:
            rows = self._db.execute(
                f"""
                SELECT agent_name, message_id, platform, chat_id, message_ts,
                       reply_to, is_group, source_was_voice, attachments_json,
                       metadata_json, stored_at
                FROM message_contexts
                WHERE agent_name=? AND message_id=? AND stored_at>=?{identity_filter}
                ORDER BY stored_at DESC, rowid DESC
                LIMIT 2
                """,
                params,
            ).fetchall()
        if len(rows) != 1:
            return None
        row = rows[0]
        context = {
            "agent_name": row["agent_name"],
            "message_id": row["message_id"],
            "platform": row["platform"],
            "chat_id": row["chat_id"],
            "timestamp": row["message_ts"],
            "reply_to": row["reply_to"],
            "is_group": bool(row["is_group"]),
            "source_was_voice": bool(row["source_was_voice"]),
            "attachments": self._json_list(row["attachments_json"]),
            "metadata": self._json_dict(row["metadata_json"]),
        }
        if include_stored_at:
            context["_stored_at"] = float(row["stored_at"])
        return context

    def _sweep_retention_locked(self, now: float) -> int:
        """Prune rows while the caller owns ``_lock`` and the transaction."""
        cutoff = now - self.retention_seconds
        before = self._db.total_changes
        self._db.execute(
            "DELETE FROM message_contexts WHERE stored_at < ?",
            (cutoff,),
        )
        agent_rows = self._db.execute(
            "SELECT DISTINCT agent_name FROM message_contexts"
        ).fetchall()
        for row in agent_rows:
            self._db.execute(
                """
                DELETE FROM message_contexts
                WHERE rowid IN (
                    SELECT rowid FROM message_contexts
                    WHERE agent_name=?
                    ORDER BY stored_at DESC, rowid DESC
                    LIMIT -1 OFFSET ?
                )
                """,
                (row["agent_name"], self.max_per_agent),
            )
        return self._db.total_changes - before

    def sweep_retention(self, *, now: float | None = None) -> int:
        """Prune contexts older than 30 days and cap each agent to 1,000 rows."""
        current = time.time() if now is None else float(now)
        with self._lock:
            try:
                deleted = self._sweep_retention_locked(current)
                self._db.commit()
                return deleted
            except Exception:
                self._db.rollback()
                raise

    def close(self) -> None:
        with self._lock:
            self._db.close()
