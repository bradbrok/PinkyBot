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

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "from": self.from_session,
            "to": self.to_session,
            "content": self.content,
            "timestamp": self.timestamp,
            "type": self.message_type,
            "group": self.group,
            "read": self.read,
        }


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

    def _init_schema(self) -> None:
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

    # ── Send ─────────────────────────────────────────────────

    def send(
        self,
        from_session: str,
        to_session: str,
        content: str,
        *,
        metadata: dict | None = None,
    ) -> AgentMessage:
        """Send a direct message to another session."""
        return self._send(
            from_session=from_session,
            to_session=to_session,
            content=content,
            message_type="direct",
            recipients=[to_session],
            metadata=metadata,
        )

    def send_group(
        self,
        from_session: str,
        group: str,
        content: str,
        *,
        metadata: dict | None = None,
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
        )

    def broadcast(
        self,
        from_session: str,
        content: str,
        *,
        active_sessions: list[str] | None = None,
        metadata: dict | None = None,
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

        return self._send(
            from_session=from_session,
            to_session="*",
            content=content,
            message_type="broadcast",
            recipients=recipients,
            metadata=metadata,
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
    ) -> AgentMessage:
        """Internal: store message and deliver to recipients' inboxes."""
        ts = time.time()
        meta_json = json.dumps(metadata or {})

        cursor = self._conn.execute(
            """INSERT INTO messages (from_session, to_session, content, timestamp,
               message_type, group_name, metadata)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (from_session, to_session, content, ts, message_type, group, meta_json),
        )
        msg_id = cursor.lastrowid

        # Deliver to each recipient's inbox
        for recipient in recipients:
            self._conn.execute(
                "INSERT INTO inbox (session_id, message_id) VALUES (?, ?)",
                (recipient, msg_id),
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

        rows = self._conn.execute(
            f"""SELECT m.*, i.read, i.id as inbox_id
                FROM inbox i
                JOIN messages m ON m.id = i.message_id
                WHERE i.session_id = ?
                {read_filter}
                ORDER BY m.timestamp DESC
                LIMIT ?""",
            (session_id, limit),
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

    def _row_to_message(self, row: sqlite3.Row) -> AgentMessage:
        meta = {}
        try:
            meta = json.loads(row["metadata"])
        except (json.JSONDecodeError, KeyError):
            pass

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
        )


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)
