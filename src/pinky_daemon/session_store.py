"""Session persistence — SQLite-backed session storage.

Persists session configurations so they survive server restarts.
The session's Claude Code subprocess state is ephemeral (managed by CC),
but the config (model, soul, tools, permissions) and metadata
(created_at, last_active, restart_count) are durable.

On startup, SessionManager loads all non-closed sessions from the store
and re-initializes their runners. The CC session ID is preserved so
--continue can resume where it left off.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import time
from dataclasses import dataclass
from pathlib import Path


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


@dataclass
class SessionRecord:
    """A persisted session configuration."""

    id: str
    model: str
    soul: str
    working_dir: str
    allowed_tools: list[str]
    max_turns: int
    timeout: float
    system_prompt: str
    restart_threshold_pct: float
    auto_restart: bool
    permission_mode: str
    state: str
    created_at: float
    last_active: float
    restart_count: int
    sdk_session_id: str
    session_type: str = "chat"
    agent_name: str = ""


class SessionStore:
    """SQLite-backed session persistence."""

    def __init__(self, db_path: str = "data/sessions.db") -> None:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._init_tables()

    def _init_tables(self) -> None:
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                model TEXT NOT NULL DEFAULT '',
                soul TEXT NOT NULL DEFAULT '',
                working_dir TEXT NOT NULL DEFAULT '.',
                allowed_tools TEXT NOT NULL DEFAULT '[]',
                max_turns INTEGER NOT NULL DEFAULT 0,
                timeout REAL NOT NULL DEFAULT 300.0,
                system_prompt TEXT NOT NULL DEFAULT '',
                restart_threshold_pct REAL NOT NULL DEFAULT 80.0,
                auto_restart INTEGER NOT NULL DEFAULT 1,
                permission_mode TEXT NOT NULL DEFAULT '',
                state TEXT NOT NULL DEFAULT 'idle',
                created_at REAL NOT NULL,
                last_active REAL NOT NULL,
                restart_count INTEGER NOT NULL DEFAULT 0,
                sdk_session_id TEXT NOT NULL DEFAULT '',
                session_type TEXT NOT NULL DEFAULT 'chat',
                agent_name TEXT NOT NULL DEFAULT ''
            );

        """)
        self._db.commit()
        self._migrate()
        # Create indexes after migration ensures columns exist
        self._db.executescript("""
            CREATE INDEX IF NOT EXISTS idx_sessions_agent ON sessions(agent_name);
            CREATE INDEX IF NOT EXISTS idx_sessions_type ON sessions(session_type);
        """)
        self._db.commit()

    _COLUMNS = (
        "id, model, soul, working_dir, allowed_tools, max_turns, timeout, "
        "system_prompt, restart_threshold_pct, auto_restart, permission_mode, "
        "state, created_at, last_active, restart_count, sdk_session_id, "
        "session_type, agent_name"
    )

    def _migrate(self) -> None:
        """Add new columns to existing databases."""
        existing = {
            row[1] for row in self._db.execute("PRAGMA table_info(sessions)").fetchall()
        }
        migrations = [
            ("session_type", "TEXT NOT NULL DEFAULT 'chat'"),
            ("agent_name", "TEXT NOT NULL DEFAULT ''"),
        ]
        for col, typedef in migrations:
            if col not in existing:
                self._db.execute(f"ALTER TABLE sessions ADD COLUMN {col} {typedef}")
                _log(f"session_store: migrated — added column {col}")
        self._db.commit()

    def save(self, record: SessionRecord) -> None:
        """Save or update a session record."""
        self._db.execute(
            f"""INSERT INTO sessions ({self._COLUMNS})
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT (id) DO UPDATE SET
                model=excluded.model, soul=excluded.soul, working_dir=excluded.working_dir,
                allowed_tools=excluded.allowed_tools, max_turns=excluded.max_turns,
                timeout=excluded.timeout, system_prompt=excluded.system_prompt,
                restart_threshold_pct=excluded.restart_threshold_pct,
                auto_restart=excluded.auto_restart, permission_mode=excluded.permission_mode,
                state=excluded.state, last_active=excluded.last_active,
                restart_count=excluded.restart_count, sdk_session_id=excluded.sdk_session_id,
                session_type=excluded.session_type, agent_name=excluded.agent_name""",
            (
                record.id, record.model, record.soul, record.working_dir,
                json.dumps(record.allowed_tools), record.max_turns, record.timeout,
                record.system_prompt, record.restart_threshold_pct,
                int(record.auto_restart), record.permission_mode,
                record.state, record.created_at, record.last_active,
                record.restart_count, record.sdk_session_id,
                record.session_type, record.agent_name,
            ),
        )
        self._db.commit()

    def get(self, session_id: str) -> SessionRecord | None:
        """Get a session record by ID."""
        row = self._db.execute(
            f"SELECT {self._COLUMNS} FROM sessions WHERE id=?",
            (session_id,),
        ).fetchone()
        if not row:
            return None
        return self._row_to_record(row)

    def list_active(self) -> list[SessionRecord]:
        """List all non-closed sessions."""
        rows = self._db.execute(
            f"SELECT {self._COLUMNS} FROM sessions WHERE state != 'closed' ORDER BY last_active DESC",
        ).fetchall()
        return [self._row_to_record(r) for r in rows]

    def list_all(self) -> list[SessionRecord]:
        """List all sessions including closed."""
        rows = self._db.execute(
            f"SELECT {self._COLUMNS} FROM sessions ORDER BY last_active DESC",
        ).fetchall()
        return [self._row_to_record(r) for r in rows]

    def list_by_agent(self, agent_name: str, *, active_only: bool = True) -> list[SessionRecord]:
        """List sessions for a specific agent."""
        if active_only:
            rows = self._db.execute(
                f"SELECT {self._COLUMNS} FROM sessions WHERE agent_name=? AND state != 'closed' ORDER BY last_active DESC",
                (agent_name,),
            ).fetchall()
        else:
            rows = self._db.execute(
                f"SELECT {self._COLUMNS} FROM sessions WHERE agent_name=? ORDER BY last_active DESC",
                (agent_name,),
            ).fetchall()
        return [self._row_to_record(r) for r in rows]

    def get_main_session(self, agent_name: str) -> SessionRecord | None:
        """Get the main session for an agent (if any)."""
        row = self._db.execute(
            f"SELECT {self._COLUMNS} FROM sessions WHERE agent_name=? AND session_type='main' AND state != 'closed' LIMIT 1",
            (agent_name,),
        ).fetchone()
        if not row:
            return None
        return self._row_to_record(row)

    def update_state(self, session_id: str, state: str) -> None:
        """Update session state."""
        self._db.execute(
            "UPDATE sessions SET state=?, last_active=? WHERE id=?",
            (state, time.time(), session_id),
        )
        self._db.commit()

    def update_activity(self, session_id: str) -> None:
        """Update last_active timestamp."""
        self._db.execute(
            "UPDATE sessions SET last_active=? WHERE id=?",
            (time.time(), session_id),
        )
        self._db.commit()

    def update_sdk_session_id(self, session_id: str, sdk_session_id: str) -> None:
        """Update the real SDK session ID for resume."""
        self._db.execute(
            "UPDATE sessions SET sdk_session_id=? WHERE id=?",
            (sdk_session_id, session_id),
        )
        self._db.commit()

    def update_restart_count(self, session_id: str, count: int) -> None:
        """Update restart count."""
        self._db.execute(
            "UPDATE sessions SET restart_count=? WHERE id=?",
            (count, session_id),
        )
        self._db.commit()

    def delete(self, session_id: str) -> bool:
        """Mark a session as closed (soft delete)."""
        cursor = self._db.execute(
            "UPDATE sessions SET state='closed', last_active=? WHERE id=?",
            (time.time(), session_id),
        )
        self._db.commit()
        return cursor.rowcount > 0

    def hard_delete(self, session_id: str) -> bool:
        """Permanently remove a session record."""
        cursor = self._db.execute("DELETE FROM sessions WHERE id=?", (session_id,))
        self._db.commit()
        return cursor.rowcount > 0

    def _row_to_record(self, row: tuple) -> SessionRecord:
        return SessionRecord(
            id=row[0],
            model=row[1],
            soul=row[2],
            working_dir=row[3],
            allowed_tools=json.loads(row[4]),
            max_turns=row[5],
            timeout=row[6],
            system_prompt=row[7],
            restart_threshold_pct=row[8],
            auto_restart=bool(row[9]),
            permission_mode=row[10],
            state=row[11],
            created_at=row[12],
            last_active=row[13],
            restart_count=row[14],
            sdk_session_id=row[15],
            session_type=row[16] if len(row) > 16 else "chat",
            agent_name=row[17] if len(row) > 17 else "",
        )

    def close(self) -> None:
        self._db.close()


class SessionEventStore:
    """SQLite-backed store for session lifecycle events."""

    def __init__(self, db_path: str = "data/sessions.db") -> None:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._init_tables()

    def _init_tables(self) -> None:
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS session_events (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id  TEXT NOT NULL,
                agent_name  TEXT NOT NULL,
                event_type  TEXT NOT NULL,
                metadata    TEXT NOT NULL DEFAULT '{}',
                created_at  REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_session_events_agent ON session_events(agent_name);
            CREATE INDEX IF NOT EXISTS idx_session_events_session ON session_events(session_id);
            CREATE INDEX IF NOT EXISTS idx_session_events_type ON session_events(event_type);
        """)
        self._db.commit()

    def log(
        self,
        session_id: str,
        agent_name: str,
        event_type: str,
        metadata: dict | None = None,
    ) -> int:
        """Log a session event. Returns the new row id."""
        cursor = self._db.execute(
            "INSERT INTO session_events (session_id, agent_name, event_type, metadata, created_at) VALUES (?, ?, ?, ?, ?)",
            (session_id, agent_name, event_type, json.dumps(metadata or {}), time.time()),
        )
        self._db.commit()
        return cursor.lastrowid  # type: ignore[return-value]

    def get_for_agent(self, agent_name: str, limit: int = 50) -> list[dict]:
        """Fetch recent events for all sessions of an agent."""
        rows = self._db.execute(
            "SELECT id, session_id, agent_name, event_type, metadata, created_at "
            "FROM session_events WHERE agent_name=? ORDER BY created_at DESC LIMIT ?",
            (agent_name, limit),
        ).fetchall()
        return [
            {
                "id": r[0],
                "session_id": r[1],
                "agent_name": r[2],
                "event_type": r[3],
                "metadata": json.loads(r[4]),
                "created_at": r[5],
            }
            for r in rows
        ]

    def get_for_session(self, session_id: str, limit: int = 100) -> list[dict]:
        """Fetch events for a specific session."""
        rows = self._db.execute(
            "SELECT id, session_id, agent_name, event_type, metadata, created_at "
            "FROM session_events WHERE session_id=? ORDER BY created_at DESC LIMIT ?",
            (session_id, limit),
        ).fetchall()
        return [
            {
                "id": r[0],
                "session_id": r[1],
                "agent_name": r[2],
                "event_type": r[3],
                "metadata": json.loads(r[4]),
                "created_at": r[5],
            }
            for r in rows
        ]

    def close(self) -> None:
        self._db.close()
