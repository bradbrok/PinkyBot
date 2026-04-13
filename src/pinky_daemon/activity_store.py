"""Activity Store — unified event log across all agents.

Records significant events: task lifecycle, research, presentations,
agent status changes, and more. Feeds the /activity API endpoint
and the frontend activity feed.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path


class ActivityStore:
    """SQLite-backed activity log."""

    def __init__(self, db_path: str = "data/activity.db") -> None:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._init_tables()

    def _init_tables(self) -> None:
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS activity_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_name  TEXT NOT NULL DEFAULT '',
                event_type  TEXT NOT NULL,
                title       TEXT NOT NULL DEFAULT '',
                description TEXT NOT NULL DEFAULT '',
                metadata    TEXT NOT NULL DEFAULT '{}',
                created_at  REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_activity_created
                ON activity_log(created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_activity_agent
                ON activity_log(agent_name);
            CREATE INDEX IF NOT EXISTS idx_activity_event_type
                ON activity_log(event_type);
        """)
        self._db.commit()

    def log(
        self,
        agent_name: str,
        event_type: str,
        title: str,
        description: str = "",
        metadata: dict | None = None,
    ) -> dict:
        """Record an activity event. Returns the saved event as a dict."""
        now = time.time()
        meta_json = json.dumps(metadata or {})
        cursor = self._db.execute(
            """INSERT INTO activity_log
               (agent_name, event_type, title, description, metadata, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (agent_name, event_type, title, description, meta_json, now),
        )
        self._db.commit()
        return {
            "id": cursor.lastrowid,
            "agent_name": agent_name,
            "event_type": event_type,
            "title": title,
            "description": description,
            "metadata": metadata or {},
            "created_at": now,
        }

    def list(
        self,
        limit: int = 50,
        offset: int = 0,
        agent_name: str = "",
        event_type: str = "",
    ) -> list[dict]:
        """Return recent activity events, newest first."""
        sql = "SELECT id, agent_name, event_type, title, description, metadata, created_at FROM activity_log WHERE 1=1"
        params: list = []
        if agent_name:
            sql += " AND agent_name=?"
            params.append(agent_name)
        if event_type:
            sql += " AND event_type=?"
            params.append(event_type)
        sql += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
        params.append(limit)
        params.append(offset)
        rows = self._db.execute(sql, params).fetchall()
        return [
            {
                "id": r[0],
                "agent_name": r[1],
                "event_type": r[2],
                "title": r[3],
                "description": r[4],
                "metadata": json.loads(r[5]) if r[5] else {},
                "created_at": r[6],
            }
            for r in rows
        ]

    def count_by_type_and_agent(self, event_type: str) -> dict[str, int]:
        """Return {agent_name: count} for a specific event type."""
        rows = self._db.execute(
            "SELECT agent_name, COUNT(*) FROM activity_log "
            "WHERE event_type=? AND agent_name != '' "
            "GROUP BY agent_name",
            (event_type,),
        ).fetchall()
        return {r[0]: r[1] for r in rows}

    def get_stats(self) -> dict:
        """Return summary stats for the activity log."""
        total = self._db.execute("SELECT COUNT(*) FROM activity_log").fetchone()[0]
        by_type = self._db.execute(
            "SELECT event_type, COUNT(*) FROM activity_log GROUP BY event_type ORDER BY COUNT(*) DESC"
        ).fetchall()
        by_agent = self._db.execute(
            "SELECT agent_name, COUNT(*) FROM activity_log WHERE agent_name != '' GROUP BY agent_name ORDER BY COUNT(*) DESC"
        ).fetchall()
        restarts_by_agent = self.count_by_type_and_agent("context_restart")
        return {
            "total": total,
            "by_type": {r[0]: r[1] for r in by_type},
            "by_agent": {r[0]: r[1] for r in by_agent},
            "restarts_by_agent": restarts_by_agent,
        }

    def close(self) -> None:
        self._db.close()
