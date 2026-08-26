"""Typed SQLite owner for the daemon hook audit trail."""

from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from pinky_daemon.store_catalog import (
    StoreCatalog,
    apply_store_connection_policy,
    open_store_connection,
    store_connection_policy,
)


@dataclass
class AuditEntry:
    """A single audit log entry."""

    id: int = 0
    agent_name: str = ""
    session_id: str = ""
    event: str = ""
    tool_name: str = ""
    tool_input_summary: str = ""
    cost_usd: float = 0.0
    duration_ms: int = 0
    success: bool = True
    timestamp: float = 0.0

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "agent_name": self.agent_name,
            "session_id": self.session_id,
            "event": self.event,
            "tool_name": self.tool_name,
            "tool_input_summary": self.tool_input_summary,
            "cost_usd": self.cost_usd,
            "duration_ms": self.duration_ms,
            "success": self.success,
            "timestamp": self.timestamp,
        }


class AuditStore:
    """SQLite-backed audit trail for hook events."""

    def __init__(
        self,
        db_path: str = "data/audit.db",
        *,
        catalog: StoreCatalog | None = None,
    ) -> None:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db_path = db_path
        self._catalog = catalog
        self._thread_local = threading.local()
        self._init_tables()

    @property
    def _db(self) -> sqlite3.Connection:
        """Return the calling thread's connection, creating it on first use."""
        connection = getattr(self._thread_local, "connection", None)
        if connection is None:
            connection = open_store_connection(
                self._catalog,
                "audit",
                self._db_path,
                owner=type(self).__name__,
            )
            journal_mode = str(connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]).lower()
            apply_store_connection_policy(
                connection,
                store_connection_policy(self._catalog, "audit"),
            )
            if self._catalog is not None:
                self._catalog.register(
                    "audit",
                    self._db_path,
                    journal_mode=journal_mode,
                    owner=type(self).__name__,
                )
            self._thread_local.connection = connection
        return connection

    def _init_tables(self) -> None:
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_name TEXT NOT NULL DEFAULT '',
                session_id TEXT NOT NULL DEFAULT '',
                event TEXT NOT NULL,
                tool_name TEXT NOT NULL DEFAULT '',
                tool_input_summary TEXT NOT NULL DEFAULT '',
                cost_usd REAL NOT NULL DEFAULT 0,
                duration_ms INTEGER NOT NULL DEFAULT 0,
                success INTEGER NOT NULL DEFAULT 1,
                timestamp REAL NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_audit_agent ON audit_log(agent_name, timestamp DESC);
            CREATE INDEX IF NOT EXISTS idx_audit_session ON audit_log(session_id);
            CREATE INDEX IF NOT EXISTS idx_audit_event ON audit_log(event);
        """)
        self._db.commit()

    def log(
        self,
        event: str,
        *,
        agent_name: str = "",
        session_id: str = "",
        tool_name: str = "",
        tool_input_summary: str = "",
        cost_usd: float = 0.0,
        duration_ms: int = 0,
        success: bool = True,
    ) -> AuditEntry:
        """Log an audit event."""
        now = time.time()
        cursor = self._db.execute(
            """INSERT INTO audit_log
               (agent_name, session_id, event, tool_name, tool_input_summary,
                cost_usd, duration_ms, success, timestamp)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                agent_name,
                session_id,
                event,
                tool_name,
                tool_input_summary[:500],
                cost_usd,
                duration_ms,
                int(success),
                now,
            ),
        )
        self._db.commit()
        return AuditEntry(
            id=cursor.lastrowid,
            agent_name=agent_name,
            session_id=session_id,
            event=event,
            tool_name=tool_name,
            tool_input_summary=tool_input_summary[:500],
            cost_usd=cost_usd,
            duration_ms=duration_ms,
            success=success,
            timestamp=now,
        )

    def get_log(
        self,
        *,
        agent_name: str = "",
        session_id: str = "",
        event: str = "",
        limit: int = 50,
    ) -> list[AuditEntry]:
        """Query audit log."""
        conditions = []
        params: list = []
        if agent_name:
            conditions.append("agent_name=?")
            params.append(agent_name)
        if session_id:
            conditions.append("session_id=?")
            params.append(session_id)
        if event:
            conditions.append("event=?")
            params.append(event)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        params.append(limit)
        rows = self._db.execute(
            f"SELECT * FROM audit_log {where} ORDER BY timestamp DESC LIMIT ?",
            params,
        ).fetchall()
        return [
            AuditEntry(
                id=row[0],
                agent_name=row[1],
                session_id=row[2],
                event=row[3],
                tool_name=row[4],
                tool_input_summary=row[5],
                cost_usd=row[6],
                duration_ms=row[7],
                success=bool(row[8]),
                timestamp=row[9],
            )
            for row in rows
        ]

    def get_costs(self, *, agent_name: str = "", session_id: str = "") -> dict:
        """Get cost summary."""
        conditions = ["cost_usd > 0"]
        params: list = []
        if agent_name:
            conditions.append("agent_name=?")
            params.append(agent_name)
        if session_id:
            conditions.append("session_id=?")
            params.append(session_id)
        where = f"WHERE {' AND '.join(conditions)}"
        row = self._db.execute(
            f"SELECT SUM(cost_usd), COUNT(*) FROM audit_log {where}",
            params,
        ).fetchone()
        return {
            "total_cost_usd": round(row[0] or 0, 4),
            "query_count": row[1] or 0,
        }

    def prune(self, *, max_age_days: int = 30) -> int:
        """Remove audit entries older than max_age_days."""
        cutoff = time.time() - (max_age_days * 86400)
        cursor = self._db.execute(
            "DELETE FROM audit_log WHERE timestamp < ?",
            (cutoff,),
        )
        self._db.commit()
        return cursor.rowcount

    def close(self) -> None:
        """Close the calling thread's connection, if it has opened one."""
        connection = getattr(self._thread_local, "connection", None)
        if connection is not None:
            connection.close()
            del self._thread_local.connection
