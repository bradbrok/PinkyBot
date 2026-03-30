"""Conversation store — persistent, searchable message log.

Stores raw user/agent exchanges in SQLite with FTS5 full-text search.
Separate from the memory system (curated knowledge) — this is the
raw transcript of every conversation.

Schema:
    messages(id, session_id, role, content, timestamp, platform, chat_id, metadata)
    messages_fts(content)  -- FTS5 virtual table for search
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class StoredMessage:
    """A message persisted in the conversation store."""

    id: int
    session_id: str
    role: str  # "user" or "assistant"
    content: str
    timestamp: float
    platform: str = ""
    chat_id: str = ""
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = {
            "id": self.id,
            "session_id": self.session_id,
            "role": self.role,
            "content": self.content,
            "timestamp": self.timestamp,
            "platform": self.platform,
            "chat_id": self.chat_id,
        }
        if self.metadata:
            d["metadata"] = self.metadata
        return d


@dataclass
class ConversationSummary:
    """Summary of a conversation (grouped by session)."""

    session_id: str
    message_count: int
    first_message_at: float
    last_message_at: float
    platform: str = ""
    chat_id: str = ""

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "message_count": self.message_count,
            "first_message_at": self.first_message_at,
            "last_message_at": self.last_message_at,
            "platform": self.platform,
            "chat_id": self.chat_id,
        }


class ConversationStore:
    """SQLite-backed conversation store with full-text search.

    Stores every user/agent message exchange for persistence and search.
    Uses FTS5 for fast keyword search across all conversations.
    """

    def __init__(self, db_path: str = "data/conversations.db") -> None:
        self._db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._init_schema()

    def _init_schema(self) -> None:
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                timestamp REAL NOT NULL,
                platform TEXT DEFAULT '',
                chat_id TEXT DEFAULT '',
                metadata TEXT DEFAULT '{}'
            );

            CREATE INDEX IF NOT EXISTS idx_messages_session
                ON messages(session_id);
            CREATE INDEX IF NOT EXISTS idx_messages_timestamp
                ON messages(timestamp);
            CREATE INDEX IF NOT EXISTS idx_messages_platform_chat
                ON messages(platform, chat_id);

            CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts
                USING fts5(content, content=messages, content_rowid=id);

            -- Triggers to keep FTS in sync
            CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
                INSERT INTO messages_fts(rowid, content)
                VALUES (new.id, new.content);
            END;

            CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
                INSERT INTO messages_fts(messages_fts, rowid, content)
                VALUES ('delete', old.id, old.content);
            END;

            CREATE TRIGGER IF NOT EXISTS messages_au AFTER UPDATE ON messages BEGIN
                INSERT INTO messages_fts(messages_fts, rowid, content)
                VALUES ('delete', old.id, old.content);
                INSERT INTO messages_fts(rowid, content)
                VALUES (new.id, new.content);
            END;
        """)

    def append(
        self,
        session_id: str,
        role: str,
        content: str,
        *,
        timestamp: float = 0.0,
        platform: str = "",
        chat_id: str = "",
        metadata: dict | None = None,
    ) -> StoredMessage:
        """Append a message to the conversation log."""
        ts = timestamp or time.time()
        meta_json = json.dumps(metadata or {})

        cursor = self._conn.execute(
            """INSERT INTO messages (session_id, role, content, timestamp, platform, chat_id, metadata)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (session_id, role, content, ts, platform, chat_id, meta_json),
        )
        self._conn.commit()

        return StoredMessage(
            id=cursor.lastrowid,
            session_id=session_id,
            role=role,
            content=content,
            timestamp=ts,
            platform=platform,
            chat_id=chat_id,
            metadata=metadata or {},
        )

    def get_history(
        self,
        session_id: str,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> list[StoredMessage]:
        """Get messages for a session, newest first."""
        rows = self._conn.execute(
            """SELECT * FROM messages
               WHERE session_id = ?
               ORDER BY timestamp DESC
               LIMIT ? OFFSET ?""",
            (session_id, limit, offset),
        ).fetchall()
        return [self._row_to_message(r) for r in reversed(rows)]

    def search(
        self,
        query: str,
        *,
        session_id: str = "",
        platform: str = "",
        chat_id: str = "",
        limit: int = 50,
    ) -> list[StoredMessage]:
        """Full-text search across all conversations.

        Args:
            query: Search query (FTS5 syntax supported).
            session_id: Filter to specific session.
            platform: Filter by platform.
            chat_id: Filter by chat.
            limit: Max results.
        """
        # Build query with optional filters
        conditions = ["messages_fts MATCH ?"]
        params: list = [query]

        if session_id:
            conditions.append("m.session_id = ?")
            params.append(session_id)
        if platform:
            conditions.append("m.platform = ?")
            params.append(platform)
        if chat_id:
            conditions.append("m.chat_id = ?")
            params.append(chat_id)

        where = " AND ".join(conditions)
        params.append(limit)

        rows = self._conn.execute(
            f"""SELECT m.* FROM messages m
                JOIN messages_fts ON m.id = messages_fts.rowid
                WHERE {where}
                ORDER BY m.timestamp DESC
                LIMIT ?""",
            params,
        ).fetchall()

        return [self._row_to_message(r) for r in rows]

    def list_conversations(
        self,
        *,
        platform: str = "",
        limit: int = 50,
    ) -> list[ConversationSummary]:
        """List all conversations grouped by session."""
        conditions = []
        params: list = []

        if platform:
            conditions.append("platform = ?")
            params.append(platform)

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        params.append(limit)

        rows = self._conn.execute(
            f"""SELECT
                    session_id,
                    COUNT(*) as message_count,
                    MIN(timestamp) as first_message_at,
                    MAX(timestamp) as last_message_at,
                    platform,
                    chat_id
                FROM messages
                {where}
                GROUP BY session_id
                ORDER BY last_message_at DESC
                LIMIT ?""",
            params,
        ).fetchall()

        return [
            ConversationSummary(
                session_id=r["session_id"],
                message_count=r["message_count"],
                first_message_at=r["first_message_at"],
                last_message_at=r["last_message_at"],
                platform=r["platform"] or "",
                chat_id=r["chat_id"] or "",
            )
            for r in rows
        ]

    def count(self, session_id: str = "") -> int:
        """Count messages, optionally filtered by session."""
        if session_id:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM messages WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        else:
            row = self._conn.execute("SELECT COUNT(*) FROM messages").fetchone()
        return row[0]

    def close(self) -> None:
        self._conn.close()

    def _row_to_message(self, row: sqlite3.Row) -> StoredMessage:
        meta = {}
        try:
            meta = json.loads(row["metadata"])
        except (json.JSONDecodeError, KeyError):
            pass

        return StoredMessage(
            id=row["id"],
            session_id=row["session_id"],
            role=row["role"],
            content=row["content"],
            timestamp=row["timestamp"],
            platform=row["platform"] or "",
            chat_id=row["chat_id"] or "",
            metadata=meta,
        )
