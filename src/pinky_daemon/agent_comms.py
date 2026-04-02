"""Agent communications — inter-session messaging.

Enables sessions (agents) to send messages to each other:
- Direct: one agent to another
- Group: named groups of agents
- Broadcast: to all active agents

Messages are stored in SQLite for durability and searchability.
Each session has an inbox that accumulates messages until read.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class AgentMessage:
    """A message between agents."""

    id: int
    from_session: str
    to_session: str  # session_id, group name, or "*" for broadcast
    content: str
    timestamp: float
    message_type: str = "direct"  # direct, group, broadcast
    group: str = ""
    metadata: dict = field(default_factory=dict)
    read: bool = False
    content_type: str = "text"  # text, task_request, task_response, status, file_transfer
    parent_message_id: int | None = None  # for reply threading
    priority: int = 0  # 0=normal, 1=high, 2=urgent

    def to_dict(self) -> dict:
        d = {
            "id": self.id,
            "from": self.from_session,
            "to": self.to_session,
            "content": self.content,
            "timestamp": self.timestamp,
            "type": self.message_type,
            "group": self.group,
            "read": self.read,
            "content_type": self.content_type,
            "priority": self.priority,
        }
        if self.parent_message_id is not None:
            d["parent_message_id"] = self.parent_message_id
        return d


class AgentComms:
    """Inter-agent communication system.

    Backed by SQLite. Supports direct messages, named groups,
    and broadcast to all sessions.
    """

    def __init__(self, db_path: str = "data/agent_comms.db") -> None:
        self._db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._init_schema()
        self._migrate()

    def _init_schema(self) -> None:
        # executescript issues an implicit COMMIT before running, so
        # _migrate() (which follows) starts outside any transaction.
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                from_session TEXT NOT NULL,
                to_session TEXT NOT NULL,
                content TEXT NOT NULL,
                timestamp REAL NOT NULL,
                message_type TEXT NOT NULL DEFAULT 'direct',
                group_name TEXT DEFAULT '',
                metadata TEXT DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS inbox (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                message_id INTEGER NOT NULL,
                read INTEGER DEFAULT 0,
                FOREIGN KEY (message_id) REFERENCES messages(id)
            );

            CREATE TABLE IF NOT EXISTS groups (
                name TEXT NOT NULL,
                session_id TEXT NOT NULL,
                joined_at REAL NOT NULL,
                PRIMARY KEY (name, session_id)
            );

            CREATE INDEX IF NOT EXISTS idx_inbox_session
                ON inbox(session_id, read);
            CREATE INDEX IF NOT EXISTS idx_messages_timestamp
                ON messages(timestamp);
            CREATE INDEX IF NOT EXISTS idx_groups_session
                ON groups(session_id);
        """)

    def _migrate(self) -> None:
        """Add new columns to existing databases (atomic)."""
        self._conn.execute("BEGIN")
        try:
            msg_existing = {
                row[1] for row in self._conn.execute("PRAGMA table_info(messages)").fetchall()
            }
            msg_migrations = [
                ("content_type", "TEXT NOT NULL DEFAULT 'text'"),
                ("parent_message_id", "INTEGER DEFAULT NULL"),
                ("priority", "INTEGER NOT NULL DEFAULT 0"),
            ]
            for col, typedef in msg_migrations:
                if col not in msg_existing:
                    self._conn.execute(f"ALTER TABLE messages ADD COLUMN {col} {typedef}")
                    _log(f"agent_comms: migrated — added {col} to messages")

            inbox_existing = {
                row[1] for row in self._conn.execute("PRAGMA table_info(inbox)").fetchall()
            }
            inbox_migrations = [
                ("expires_at", "REAL DEFAULT NULL"),
            ]
            for col, typedef in inbox_migrations:
                if col not in inbox_existing:
                    self._conn.execute(f"ALTER TABLE inbox ADD COLUMN {col} {typedef}")
                    _log(f"agent_comms: migrated — added {col} to inbox")

            # Indexes for new columns
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_messages_parent ON messages(parent_message_id)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_inbox_expires ON inbox(expires_at)"
            )
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

    # ── Send ─────────────────────────────────────────────────

    def send(
        self,
        from_session: str,
        to_session: str,
        content: str,
        *,
        metadata: dict | None = None,
        content_type: str = "text",
        parent_message_id: int | None = None,
        priority: int = 0,
        ttl_seconds: int | None = None,
    ) -> AgentMessage:
        """Send a direct message to another session."""
        return self._send(
            from_session=from_session,
            to_session=to_session,
            content=content,
            message_type="direct",
            recipients=[to_session],
            metadata=metadata,
            content_type=content_type,
            parent_message_id=parent_message_id,
            priority=priority,
            ttl_seconds=ttl_seconds,
        )

    def send_group(
        self,
        from_session: str,
        group: str,
        content: str,
        *,
        metadata: dict | None = None,
        content_type: str = "text",
        parent_message_id: int | None = None,
        priority: int = 0,
        ttl_seconds: int | None = None,
    ) -> AgentMessage:
        """Send a message to all members of a named group."""
        members = self._get_group_members(group)
        # Don't deliver to sender
        recipients = [m for m in members if m != from_session]

        return self._send(
            from_session=from_session,
            to_session=group,
            content=content,
            message_type="group",
            group=group,
            recipients=recipients,
            metadata=metadata,
            content_type=content_type,
            parent_message_id=parent_message_id,
            priority=priority,
            ttl_seconds=ttl_seconds,
        )

    def broadcast(
        self,
        from_session: str,
        content: str,
        *,
        active_sessions: list[str] | None = None,
        metadata: dict | None = None,
        content_type: str = "text",
        parent_message_id: int | None = None,
        priority: int = 0,
        ttl_seconds: int | None = None,
    ) -> AgentMessage:
        """Broadcast a message to all active sessions.

        Args:
            from_session: Sender session ID.
            content: Message content.
            active_sessions: List of active session IDs. If None, delivers to
                            all sessions that have ever received a message.
            metadata: Optional metadata dict.
        """
        if active_sessions:
            recipients = [s for s in active_sessions if s != from_session]
        else:
            # Deliver to all known sessions
            rows = self._conn.execute(
                "SELECT DISTINCT session_id FROM inbox"
            ).fetchall()
            recipients = [r["session_id"] for r in rows if r["session_id"] != from_session]

        # Broadcasts default to 7-day TTL if not specified
        if ttl_seconds is None:
            ttl_seconds = 7 * 24 * 3600  # 7 days

        return self._send(
            from_session=from_session,
            to_session="*",
            content=content,
            message_type="broadcast",
            recipients=recipients,
            metadata=metadata,
            content_type=content_type,
            parent_message_id=parent_message_id,
            priority=priority,
            ttl_seconds=ttl_seconds,
        )

    def _send(
        self,
        *,
        from_session: str,
        to_session: str,
        content: str,
        message_type: str,
        recipients: list[str],
        group: str = "",
        metadata: dict | None = None,
        content_type: str = "text",
        parent_message_id: int | None = None,
        priority: int = 0,
        ttl_seconds: int | None = None,
    ) -> AgentMessage:
        """Internal: store message and deliver to recipients' inboxes."""
        ts = time.time()
        meta_json = json.dumps(metadata or {})

        cursor = self._conn.execute(
            """INSERT INTO messages (from_session, to_session, content, timestamp,
               message_type, group_name, metadata, content_type, parent_message_id, priority)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (from_session, to_session, content, ts, message_type, group, meta_json,
             content_type, parent_message_id, priority),
        )
        msg_id = cursor.lastrowid

        # Compute expiry for inbox entries
        expires_at = (ts + ttl_seconds) if ttl_seconds else None

        # Deliver to each recipient's inbox
        for recipient in recipients:
            self._conn.execute(
                "INSERT INTO inbox (session_id, message_id, expires_at) VALUES (?, ?, ?)",
                (recipient, msg_id, expires_at),
            )

        self._conn.commit()

        _log(f"comms: {from_session} -> {to_session} ({message_type}, {len(recipients)} recipients)")

        return AgentMessage(
            id=msg_id,
            from_session=from_session,
            to_session=to_session,
            content=content,
            timestamp=ts,
            message_type=message_type,
            group=group,
            metadata=metadata or {},
            content_type=content_type,
            parent_message_id=parent_message_id,
            priority=priority,
        )

    # ── File Transfer ───────────────────────────────────────

    def send_file(
        self,
        from_session: str,
        to_session: str,
        file_path: str,
        *,
        description: str = "",
    ) -> AgentMessage:
        """Send a file to another agent.

        Copies the file to a shared transfer directory and sends a message
        with the file path in metadata.

        Args:
            from_session: Sender session ID.
            to_session: Recipient session ID (or group name, or "*").
            file_path: Absolute path to the file to send.
            description: Optional description of the file.
        """
        if not os.path.isfile(file_path):
            raise FileNotFoundError(f"Source file does not exist: {file_path}")

        filename = os.path.basename(file_path)
        dest_dir = os.path.join("data", "transfers", to_session)
        os.makedirs(dest_dir, exist_ok=True)

        dest_path = os.path.join(dest_dir, filename)
        if os.path.exists(dest_path):
            # Prefix with timestamp to avoid collisions
            ts_prefix = str(int(time.time()))
            filename = f"{ts_prefix}_{filename}"
            dest_path = os.path.join(dest_dir, filename)

        shutil.copy2(file_path, dest_path)
        abs_dest_path = os.path.abspath(dest_path)
        file_size = os.path.getsize(abs_dest_path)

        metadata = {
            "type": "file_transfer",
            "file_name": filename,
            "file_path": abs_dest_path,
            "file_size": file_size,
            "original_path": file_path,
        }

        content = f"📎 File from {from_session}: {filename}"
        if description:
            content += f"\n{description}"
        content += f"\nSaved to: {abs_dest_path}"

        return self.send(
            from_session=from_session,
            to_session=to_session,
            content=content,
            metadata=metadata,
        )

    # ── Receive ──────────────────────────────────────────────

    def get_inbox(
        self,
        session_id: str,
        *,
        unread_only: bool = True,
        limit: int = 50,
    ) -> list[AgentMessage]:
        """Get messages from a session's inbox."""
        read_filter = "AND i.read = 0" if unread_only else ""
        now = time.time()

        rows = self._conn.execute(
            f"""SELECT m.*, i.read, i.id as inbox_id
                FROM inbox i
                JOIN messages m ON m.id = i.message_id
                WHERE i.session_id = ?
                {read_filter}
                AND (i.expires_at IS NULL OR i.expires_at > ?)
                ORDER BY m.priority DESC, m.timestamp DESC
                LIMIT ?""",
            (session_id, now, limit),
        ).fetchall()

        return [self._row_to_message(r) for r in rows]

    def mark_read(self, session_id: str, message_ids: list[int] | None = None) -> int:
        """Mark messages as read in a session's inbox.

        Args:
            session_id: Session whose inbox to update.
            message_ids: Specific message IDs to mark. None = mark all.

        Returns:
            Number of messages marked.
        """
        if message_ids:
            placeholders = ",".join("?" * len(message_ids))
            cursor = self._conn.execute(
                f"""UPDATE inbox SET read = 1
                    WHERE session_id = ? AND message_id IN ({placeholders}) AND read = 0""",
                [session_id, *message_ids],
            )
        else:
            cursor = self._conn.execute(
                "UPDATE inbox SET read = 1 WHERE session_id = ? AND read = 0",
                (session_id,),
            )

        self._conn.commit()
        return cursor.rowcount

    def unread_count(self, session_id: str) -> int:
        """Count unread messages for a session."""
        row = self._conn.execute(
            "SELECT COUNT(*) FROM inbox WHERE session_id = ? AND read = 0",
            (session_id,),
        ).fetchone()
        return row[0]

    # ── Groups ───────────────────────────────────────────────

    def create_group(self, name: str, session_ids: list[str]) -> dict:
        """Create a named group with initial members."""
        ts = time.time()
        for sid in session_ids:
            self._conn.execute(
                "INSERT OR IGNORE INTO groups (name, session_id, joined_at) VALUES (?, ?, ?)",
                (name, sid, ts),
            )
        self._conn.commit()
        _log(f"comms: created group '{name}' with {len(session_ids)} members")
        return {"name": name, "members": session_ids}

    def join_group(self, name: str, session_id: str) -> bool:
        """Add a session to a group."""
        self._conn.execute(
            "INSERT OR IGNORE INTO groups (name, session_id, joined_at) VALUES (?, ?, ?)",
            (name, session_id, time.time()),
        )
        self._conn.commit()
        return True

    def leave_group(self, name: str, session_id: str) -> bool:
        """Remove a session from a group."""
        cursor = self._conn.execute(
            "DELETE FROM groups WHERE name = ? AND session_id = ?",
            (name, session_id),
        )
        self._conn.commit()
        return cursor.rowcount > 0

    def list_groups(self) -> list[dict]:
        """List all groups with member counts."""
        rows = self._conn.execute(
            """SELECT name, COUNT(*) as member_count
               FROM groups GROUP BY name ORDER BY name""",
        ).fetchall()
        return [{"name": r["name"], "members": r["member_count"]} for r in rows]

    def get_group_members(self, name: str) -> list[str]:
        """Get all session IDs in a group."""
        return self._get_group_members(name)

    def _get_group_members(self, name: str) -> list[str]:
        rows = self._conn.execute(
            "SELECT session_id FROM groups WHERE name = ? ORDER BY joined_at",
            (name,),
        ).fetchall()
        return [r["session_id"] for r in rows]

    # ── Helpers ──────────────────────────────────────────────

    def close(self) -> None:
        self._conn.close()

    def get_thread(self, message_id: int, *, session_id: str = "") -> list[AgentMessage]:
        """Get all messages in a thread (root + all descendants at any depth).

        Args:
            message_id: Any message ID in the thread.
            session_id: If provided, only return messages that the session is
                        authorized to see. Authorization means the full thread
                        is fetched first, then filtered to messages where
                        session_id is sender or recipient. Note: a descendant
                        may appear without its immediate parent if that parent
                        involves other sessions.
        """
        # Find the root: walk up parent chain
        root_id = message_id
        for _ in range(50):  # guard against cycles
            row = self._conn.execute(
                "SELECT parent_message_id FROM messages WHERE id = ?", (root_id,)
            ).fetchone()
            if not row or not row["parent_message_id"]:
                break
            root_id = row["parent_message_id"]

        # Recursive CTE to get all descendants at any depth.
        # Left-join inbox to get the real read state for this session
        # (falls back to False when the session has no inbox entry).
        if session_id:
            query = """
                WITH RECURSIVE thread AS (
                    SELECT m.* FROM messages m WHERE m.id = ?
                    UNION ALL
                    SELECT m.* FROM messages m
                    JOIN thread t ON m.parent_message_id = t.id
                )
                SELECT t.*, COALESCE(i.read, 0) as read
                FROM thread t
                LEFT JOIN inbox i ON i.message_id = t.id AND i.session_id = ?
                WHERE t.from_session = ? OR t.to_session = ?
                ORDER BY t.timestamp ASC
            """
            params: list = [root_id, session_id, session_id, session_id]
        else:
            query = """
                WITH RECURSIVE thread AS (
                    SELECT m.* FROM messages m WHERE m.id = ?
                    UNION ALL
                    SELECT m.* FROM messages m
                    JOIN thread t ON m.parent_message_id = t.id
                )
                SELECT t.*, 0 as read FROM thread t
                ORDER BY t.timestamp ASC
            """
            params = [root_id]

        rows = self._conn.execute(query, params).fetchall()
        return [self._row_to_message(r) for r in rows]

    def get_all_messages(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[AgentMessage], int]:
        """Return all messages ordered by timestamp desc (for audit).

        Returns (messages, total_count).
        """
        total = self._conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        rows = self._conn.execute(
            """SELECT m.*, COALESCE(i.read, 0) as read
               FROM messages m
               LEFT JOIN inbox i ON m.id = i.message_id
               ORDER BY m.timestamp DESC
               LIMIT ? OFFSET ?""",
            (limit, offset),
        ).fetchall()
        return [self._row_to_message(r) for r in rows], total

    def get_all_inbox_summaries(self) -> list[dict]:
        """Get unread message count per agent for all agents with inbox entries."""
        rows = self._conn.execute(
            "SELECT session_id, COUNT(*) as unread "
            "FROM inbox WHERE read = 0 GROUP BY session_id",
        ).fetchall()
        return [{"agent": r[0], "unread": r[1]} for r in rows]

    def cleanup_expired(self) -> int:
        """Delete expired inbox entries. Returns count deleted."""
        now = time.time()
        cursor = self._conn.execute(
            "DELETE FROM inbox WHERE expires_at IS NOT NULL AND expires_at < ?",
            (now,),
        )
        self._conn.commit()
        return cursor.rowcount

    def _row_to_message(self, row: sqlite3.Row) -> AgentMessage:
        meta = {}
        try:
            meta = json.loads(row["metadata"])
        except (json.JSONDecodeError, KeyError):
            pass

        # Safely read new columns (may not exist in old DBs before migration)
        keys = row.keys()
        content_type = row["content_type"] if "content_type" in keys else "text"
        parent_message_id = row["parent_message_id"] if "parent_message_id" in keys else None
        priority = row["priority"] if "priority" in keys else 0

        return AgentMessage(
            id=row["id"],
            from_session=row["from_session"],
            to_session=row["to_session"],
            content=row["content"],
            timestamp=row["timestamp"],
            message_type=row["message_type"],
            group=row["group_name"],
            metadata=meta,
            read=bool(row["read"]),
            content_type=content_type,
            parent_message_id=parent_message_id,
            priority=priority,
        )


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)
