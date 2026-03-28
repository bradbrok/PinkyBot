"""Skill/plugin registry — SQLite-backed skill management.

Skills are named capabilities (MCP tools, custom tools, integrations)
that can be registered globally and enabled/disabled per session.

Storage: SQLite with two tables:
  - skills: global skill registry (name, description, type, config)
  - session_skills: per-session enable/disable overrides
"""

from __future__ import annotations

import json
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


class SkillType(str, Enum):
    mcp_tool = "mcp_tool"
    builtin = "builtin"
    custom = "custom"


@dataclass
class Skill:
    """A registered skill/plugin."""

    name: str
    description: str = ""
    skill_type: str = "custom"
    version: str = "0.1.0"
    enabled: bool = True  # Global default
    config: dict = field(default_factory=dict)
    created_at: float = 0.0
    updated_at: float = 0.0

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "skill_type": self.skill_type,
            "version": self.version,
            "enabled": self.enabled,
            "config": self.config,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class SkillStore:
    """SQLite-backed skill registry."""

    def __init__(self, db_path: str = "data/skills.db") -> None:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._init_tables()

    def _init_tables(self) -> None:
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS skills (
                name TEXT PRIMARY KEY,
                description TEXT NOT NULL DEFAULT '',
                skill_type TEXT NOT NULL DEFAULT 'custom',
                version TEXT NOT NULL DEFAULT '0.1.0',
                enabled INTEGER NOT NULL DEFAULT 1,
                config TEXT NOT NULL DEFAULT '{}',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS session_skills (
                session_id TEXT NOT NULL,
                skill_name TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                updated_at REAL NOT NULL,
                PRIMARY KEY (session_id, skill_name),
                FOREIGN KEY (skill_name) REFERENCES skills(name) ON DELETE CASCADE
            );
        """)
        self._db.commit()

    def register(
        self,
        name: str,
        *,
        description: str = "",
        skill_type: str = "custom",
        version: str = "0.1.0",
        enabled: bool = True,
        config: dict | None = None,
    ) -> Skill:
        """Register a new skill or update an existing one."""
        now = time.time()
        config = config or {}

        existing = self.get(name)
        if existing:
            self._db.execute(
                """UPDATE skills
                   SET description=?, skill_type=?, version=?, enabled=?, config=?, updated_at=?
                   WHERE name=?""",
                (description, skill_type, version, int(enabled), json.dumps(config), now, name),
            )
        else:
            self._db.execute(
                """INSERT INTO skills (name, description, skill_type, version, enabled, config, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (name, description, skill_type, version, int(enabled), json.dumps(config), now, now),
            )
        self._db.commit()

        _log(f"skills: {'updated' if existing else 'registered'} {name}")
        return self.get(name)  # type: ignore

    def get(self, name: str) -> Skill | None:
        """Get a skill by name."""
        row = self._db.execute(
            "SELECT name, description, skill_type, version, enabled, config, created_at, updated_at FROM skills WHERE name=?",
            (name,),
        ).fetchone()
        if not row:
            return None
        return Skill(
            name=row[0],
            description=row[1],
            skill_type=row[2],
            version=row[3],
            enabled=bool(row[4]),
            config=json.loads(row[5]),
            created_at=row[6],
            updated_at=row[7],
        )

    def list(self, *, skill_type: str = "", enabled_only: bool = False) -> list[Skill]:
        """List all registered skills."""
        sql = "SELECT name, description, skill_type, version, enabled, config, created_at, updated_at FROM skills WHERE 1=1"
        params: list = []

        if skill_type:
            sql += " AND skill_type=?"
            params.append(skill_type)
        if enabled_only:
            sql += " AND enabled=1"

        sql += " ORDER BY name"
        rows = self._db.execute(sql, params).fetchall()
        return [
            Skill(
                name=r[0], description=r[1], skill_type=r[2], version=r[3],
                enabled=bool(r[4]), config=json.loads(r[5]),
                created_at=r[6], updated_at=r[7],
            )
            for r in rows
        ]

    def delete(self, name: str) -> bool:
        """Unregister a skill."""
        cursor = self._db.execute("DELETE FROM skills WHERE name=?", (name,))
        self._db.commit()
        if cursor.rowcount > 0:
            _log(f"skills: deleted {name}")
            return True
        return False

    def enable(self, name: str) -> bool:
        """Enable a skill globally."""
        return self._set_enabled(name, True)

    def disable(self, name: str) -> bool:
        """Disable a skill globally."""
        return self._set_enabled(name, False)

    def _set_enabled(self, name: str, enabled: bool) -> bool:
        cursor = self._db.execute(
            "UPDATE skills SET enabled=?, updated_at=? WHERE name=?",
            (int(enabled), time.time(), name),
        )
        self._db.commit()
        return cursor.rowcount > 0

    # ── Per-session overrides ─────────────────────────────────

    def enable_for_session(self, session_id: str, skill_name: str) -> bool:
        """Enable a skill for a specific session."""
        return self._set_session_skill(session_id, skill_name, True)

    def disable_for_session(self, session_id: str, skill_name: str) -> bool:
        """Disable a skill for a specific session."""
        return self._set_session_skill(session_id, skill_name, False)

    def _set_session_skill(self, session_id: str, skill_name: str, enabled: bool) -> bool:
        if not self.get(skill_name):
            return False

        self._db.execute(
            """INSERT INTO session_skills (session_id, skill_name, enabled, updated_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT (session_id, skill_name)
               DO UPDATE SET enabled=excluded.enabled, updated_at=excluded.updated_at""",
            (session_id, skill_name, int(enabled), time.time()),
        )
        self._db.commit()
        return True

    def get_session_skills(self, session_id: str) -> list[dict]:
        """Get all skills with per-session override status.

        Returns skills with effective enabled state (session override > global default).
        """
        rows = self._db.execute(
            """SELECT s.name, s.description, s.skill_type, s.version, s.enabled, s.config,
                      ss.enabled as session_enabled
               FROM skills s
               LEFT JOIN session_skills ss ON s.name = ss.skill_name AND ss.session_id = ?
               ORDER BY s.name""",
            (session_id,),
        ).fetchall()

        results = []
        for r in rows:
            session_override = r[6]
            effective = bool(session_override) if session_override is not None else bool(r[4])
            results.append({
                "name": r[0],
                "description": r[1],
                "skill_type": r[2],
                "version": r[3],
                "global_enabled": bool(r[4]),
                "session_override": bool(session_override) if session_override is not None else None,
                "effective_enabled": effective,
                "config": json.loads(r[5]),
            })
        return results

    def clear_session_override(self, session_id: str, skill_name: str) -> bool:
        """Remove per-session override, reverting to global default."""
        cursor = self._db.execute(
            "DELETE FROM session_skills WHERE session_id=? AND skill_name=?",
            (session_id, skill_name),
        )
        self._db.commit()
        return cursor.rowcount > 0

    def close(self) -> None:
        self._db.close()
