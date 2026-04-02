"""Skill/plugin registry — SQLite-backed skill management.

Skills are named capability packages that can include:
- MCP server definitions (command, args, cwd, env)
- Tool allowlist patterns (glob patterns for allowed_tools)
- System prompt directives (behavioral instructions)
- File templates (files to create in agent workspace)
- Dependencies on other skills

Skills can be:
- Shared: auto-applied to all agents when globally enabled
- Agent-specific: manually assigned to individual agents
- Self-assignable: agents can add them to themselves via pinky-self tools

Storage: SQLite with three tables:
  - skills: global skill catalog
  - agent_skills: per-agent skill assignments
  - session_skills: (deprecated) per-session overrides
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
    """A registered skill/plugin package."""

    name: str
    description: str = ""
    skill_type: str = "custom"
    version: str = "0.1.0"
    enabled: bool = True  # Global default
    config: dict = field(default_factory=dict)
    mcp_server_config: dict = field(default_factory=dict)
    tool_patterns: list[str] = field(default_factory=list)
    directive: str = ""
    requires: list[str] = field(default_factory=list)
    self_assignable: bool = False
    category: str = "general"
    shared: bool = False
    file_templates: dict = field(default_factory=dict)
    default_config: dict = field(default_factory=dict)
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
            "mcp_server_config": self.mcp_server_config,
            "tool_patterns": self.tool_patterns,
            "directive": self.directive,
            "requires": self.requires,
            "self_assignable": self.self_assignable,
            "category": self.category,
            "shared": self.shared,
            "file_templates": self.file_templates,
            "default_config": self.default_config,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass
class AgentSkill:
    """A skill assigned to a specific agent."""

    agent_name: str
    skill_name: str
    enabled: bool = True
    assigned_by: str = "user"  # user, self, system, shared
    config_overrides: dict = field(default_factory=dict)
    assigned_at: float = 0.0

    def to_dict(self) -> dict:
        return {
            "agent_name": self.agent_name,
            "skill_name": self.skill_name,
            "enabled": self.enabled,
            "assigned_by": self.assigned_by,
            "config_overrides": self.config_overrides,
            "assigned_at": self.assigned_at,
        }


# Column order for SELECT queries
_SKILL_COLS = (
    "name, description, skill_type, version, enabled, config, "
    "mcp_server_config, tool_patterns, directive, requires, "
    "self_assignable, category, shared, file_templates, default_config, "
    "created_at, updated_at"
)


def _row_to_skill(row: tuple) -> Skill:
    """Convert a database row to a Skill instance."""
    return Skill(
        name=row[0],
        description=row[1],
        skill_type=row[2],
        version=row[3],
        enabled=bool(row[4]),
        config=json.loads(row[5]),
        mcp_server_config=json.loads(row[6]),
        tool_patterns=json.loads(row[7]),
        directive=row[8],
        requires=json.loads(row[9]),
        self_assignable=bool(row[10]),
        category=row[11],
        shared=bool(row[12]),
        file_templates=json.loads(row[13]),
        default_config=json.loads(row[14]),
        created_at=row[15],
        updated_at=row[16],
    )


class SkillStore:
    """SQLite-backed skill registry with per-agent assignment."""

    def __init__(self, db_path: str = "data/skills.db") -> None:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA foreign_keys=ON")
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

            CREATE TABLE IF NOT EXISTS agent_skills (
                agent_name TEXT NOT NULL,
                skill_name TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                assigned_by TEXT NOT NULL DEFAULT 'user',
                config_overrides TEXT NOT NULL DEFAULT '{}',
                assigned_at REAL NOT NULL,
                PRIMARY KEY (agent_name, skill_name),
                FOREIGN KEY (skill_name) REFERENCES skills(name) ON DELETE CASCADE
            );
        """)
        self._db.commit()
        self._migrate()

    def _migrate(self) -> None:
        """Add new columns to existing skills table."""
        existing = {
            row[1] for row in self._db.execute("PRAGMA table_info(skills)").fetchall()
        }
        migrations = [
            ("mcp_server_config", "TEXT NOT NULL DEFAULT '{}'"),
            ("tool_patterns", "TEXT NOT NULL DEFAULT '[]'"),
            ("directive", "TEXT NOT NULL DEFAULT ''"),
            ("requires", "TEXT NOT NULL DEFAULT '[]'"),
            ("self_assignable", "INTEGER NOT NULL DEFAULT 0"),
            ("category", "TEXT NOT NULL DEFAULT 'general'"),
            ("shared", "INTEGER NOT NULL DEFAULT 0"),
            ("file_templates", "TEXT NOT NULL DEFAULT '{}'"),
            ("default_config", "TEXT NOT NULL DEFAULT '{}'"),
        ]
        for col, typedef in migrations:
            if col not in existing:
                self._db.execute(f"ALTER TABLE skills ADD COLUMN {col} {typedef}")
                _log(f"skill_store: migrated — added column {col}")
        self._db.commit()

    # ── Skill Catalog (CRUD) ──────────────────────────────────

    def register(
        self,
        name: str,
        *,
        description: str = "",
        skill_type: str = "custom",
        version: str = "0.1.0",
        enabled: bool = True,
        config: dict | None = None,
        mcp_server_config: dict | None = None,
        tool_patterns: list[str] | None = None,
        directive: str = "",
        requires: list[str] | None = None,
        self_assignable: bool = False,
        category: str = "general",
        shared: bool = False,
        file_templates: dict | None = None,
        default_config: dict | None = None,
    ) -> Skill:
        """Register a new skill or update an existing one."""
        now = time.time()
        config = config or {}
        mcp_server_config = mcp_server_config or {}
        tool_patterns = tool_patterns or []
        requires = requires or []
        file_templates = file_templates or {}
        default_config = default_config or {}

        existing = self.get(name)
        if existing:
            self._db.execute(
                """UPDATE skills
                   SET description=?, skill_type=?, version=?, enabled=?, config=?,
                       mcp_server_config=?, tool_patterns=?, directive=?, requires=?,
                       self_assignable=?, category=?, shared=?, file_templates=?,
                       default_config=?, updated_at=?
                   WHERE name=?""",
                (
                    description, skill_type, version, int(enabled), json.dumps(config),
                    json.dumps(mcp_server_config), json.dumps(tool_patterns), directive,
                    json.dumps(requires), int(self_assignable), category, int(shared),
                    json.dumps(file_templates), json.dumps(default_config), now, name,
                ),
            )
        else:
            self._db.execute(
                f"""INSERT INTO skills ({_SKILL_COLS})
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    name, description, skill_type, version, int(enabled), json.dumps(config),
                    json.dumps(mcp_server_config), json.dumps(tool_patterns), directive,
                    json.dumps(requires), int(self_assignable), category, int(shared),
                    json.dumps(file_templates), json.dumps(default_config), now, now,
                ),
            )
        self._db.commit()

        _log(f"skill_store: {'updated' if existing else 'registered'} {name}")
        return self.get(name)  # type: ignore

    def get(self, name: str) -> Skill | None:
        """Get a skill by name."""
        row = self._db.execute(
            f"SELECT {_SKILL_COLS} FROM skills WHERE name=?", (name,),
        ).fetchone()
        if not row:
            return None
        return _row_to_skill(row)

    def list(
        self,
        *,
        skill_type: str = "",
        enabled_only: bool = False,
        category: str = "",
        shared_only: bool = False,
        self_assignable_only: bool = False,
    ) -> list[Skill]:
        """List all registered skills with optional filters."""
        sql = f"SELECT {_SKILL_COLS} FROM skills WHERE 1=1"
        params: list = []

        if skill_type:
            sql += " AND skill_type=?"
            params.append(skill_type)
        if enabled_only:
            sql += " AND enabled=1"
        if category:
            sql += " AND category=?"
            params.append(category)
        if shared_only:
            sql += " AND shared=1"
        if self_assignable_only:
            sql += " AND self_assignable=1"

        sql += " ORDER BY category, name"
        rows = self._db.execute(sql, params).fetchall()
        return [_row_to_skill(r) for r in rows]

    def delete(self, name: str) -> bool:
        """Unregister a skill (also removes all agent assignments)."""
        cursor = self._db.execute("DELETE FROM skills WHERE name=?", (name,))
        self._db.commit()
        if cursor.rowcount > 0:
            _log(f"skill_store: deleted {name}")
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

    def get_categories(self) -> list[str]:
        """Return distinct skill categories."""
        rows = self._db.execute(
            "SELECT DISTINCT category FROM skills ORDER BY category"
        ).fetchall()
        return [r[0] for r in rows]

    # ── Agent Skill Assignment ────────────────────────────────

    def assign_to_agent(
        self,
        agent_name: str,
        skill_name: str,
        *,
        assigned_by: str = "user",
        config_overrides: dict | None = None,
    ) -> bool:
        """Assign a skill to an agent. Returns False if skill doesn't exist."""
        skill = self.get(skill_name)
        if not skill:
            return False

        # Check self-assignable constraint
        if assigned_by == "self" and not skill.self_assignable:
            return False

        now = time.time()
        self._db.execute(
            """INSERT INTO agent_skills (agent_name, skill_name, enabled, assigned_by, config_overrides, assigned_at)
               VALUES (?, ?, 1, ?, ?, ?)
               ON CONFLICT (agent_name, skill_name)
               DO UPDATE SET enabled=1, assigned_by=excluded.assigned_by,
                             config_overrides=excluded.config_overrides, assigned_at=excluded.assigned_at""",
            (agent_name, skill_name, assigned_by, json.dumps(config_overrides or {}), now),
        )
        self._db.commit()
        _log(f"skill_store: assigned {skill_name} to {agent_name} (by {assigned_by})")
        return True

    def remove_from_agent(self, agent_name: str, skill_name: str) -> bool:
        """Remove a skill assignment from an agent."""
        cursor = self._db.execute(
            "DELETE FROM agent_skills WHERE agent_name=? AND skill_name=?",
            (agent_name, skill_name),
        )
        self._db.commit()
        if cursor.rowcount > 0:
            _log(f"skill_store: removed {skill_name} from {agent_name}")
            return True
        return False

    def is_assigned(self, agent_name: str, skill_name: str) -> bool:
        """Check if a skill is assigned to an agent (directly or via shared)."""
        # Direct assignment
        row = self._db.execute(
            "SELECT 1 FROM agent_skills WHERE agent_name=? AND skill_name=? AND enabled=1",
            (agent_name, skill_name),
        ).fetchone()
        if row:
            return True
        # Shared auto-apply
        row = self._db.execute(
            "SELECT 1 FROM skills WHERE name=? AND enabled=1 AND shared=1",
            (skill_name,),
        ).fetchone()
        return row is not None

    def set_agent_skill_enabled(self, agent_name: str, skill_name: str, enabled: bool) -> bool:
        """Enable or disable a skill for a specific agent."""
        cursor = self._db.execute(
            "UPDATE agent_skills SET enabled=? WHERE agent_name=? AND skill_name=?",
            (int(enabled), agent_name, skill_name),
        )
        self._db.commit()
        return cursor.rowcount > 0

    def get_agent_skills(self, agent_name: str, *, enabled_only: bool = True) -> list[dict]:
        """Get all skills for an agent: direct assignments + shared globals.

        Returns dicts with skill data + assignment metadata.
        """
        results: dict[str, dict] = {}

        # 1. Shared globally-enabled skills (auto-apply)
        shared = self.list(shared_only=True, enabled_only=True)
        for skill in shared:
            results[skill.name] = {
                **skill.to_dict(),
                "assigned_by": "shared",
                "config_overrides": {},
                "effective_enabled": True,
                "agent_enabled": None,  # No agent-level override
            }

        # 2. Direct assignments (override shared if both exist)
        where = "WHERE a.agent_name=?"
        params: list = [agent_name]
        if enabled_only:
            where += " AND a.enabled=1"

        # Prefix skill columns with s. to avoid ambiguity in JOIN
        s_cols = ", ".join(f"s.{c.strip()}" for c in _SKILL_COLS.split(","))
        rows = self._db.execute(
            f"""SELECT a.skill_name, a.enabled, a.assigned_by, a.config_overrides, a.assigned_at,
                       {s_cols}
                FROM agent_skills a
                JOIN skills s ON a.skill_name = s.name
                {where}
                ORDER BY s.category, s.name""",
            params,
        ).fetchall()

        for r in rows:
            skill = _row_to_skill(r[5:])
            agent_enabled = bool(r[1])
            # Agent-level assignment overrides shared
            results[skill.name] = {
                **skill.to_dict(),
                "assigned_by": r[2],
                "config_overrides": json.loads(r[3]),
                "assigned_at": r[4],
                "effective_enabled": agent_enabled and skill.enabled,
                "agent_enabled": agent_enabled,
            }

        if enabled_only:
            results = {k: v for k, v in results.items() if v["effective_enabled"]}

        return list(results.values())

    def get_available_skills(
        self,
        agent_name: str,
        *,
        self_assignable_only: bool = False,
        category: str = "",
    ) -> list[Skill]:
        """Get skills from the catalog that are NOT assigned to this agent."""
        # Get assigned skill names
        assigned = {
            r[0] for r in self._db.execute(
                "SELECT skill_name FROM agent_skills WHERE agent_name=?", (agent_name,),
            ).fetchall()
        }
        # Also exclude shared skills (they're already effectively assigned)
        shared = {
            r[0] for r in self._db.execute(
                "SELECT name FROM skills WHERE shared=1 AND enabled=1",
            ).fetchall()
        }
        excluded = assigned | shared

        all_skills = self.list(
            enabled_only=True,
            category=category,
            self_assignable_only=self_assignable_only,
        )
        return [s for s in all_skills if s.name not in excluded]

    def check_dependencies(self, skill_name: str, agent_name: str) -> list[str]:
        """Check if an agent has all prerequisite skills. Returns missing skill names."""
        skill = self.get(skill_name)
        if not skill or not skill.requires:
            return []

        missing = []
        for req in skill.requires:
            if not self.is_assigned(agent_name, req):
                missing.append(req)
        return missing

    # ── Materialization ───────────────────────────────────────

    def materialize_for_agent(self, agent_name: str) -> dict:
        """Materialize all active skills into concrete MCP servers, tool patterns,
        directives, and file templates for an agent.

        Returns:
            {
                "mcp_servers": { "server-name": {command, args, cwd, env}, ... },
                "tool_patterns": ["mcp__server-name__*", ...],
                "directives": ["directive text", ...],
                "file_templates": { "relative/path": "content", ... },
            }
        """
        agent_skills = self.get_agent_skills(agent_name, enabled_only=True)

        mcp_servers: dict = {}
        tool_patterns: list[str] = []
        directives: list[str] = []
        file_templates: dict = {}

        for skill_data in agent_skills:
            # MCP server config — substitute {agent_name} placeholder
            mcp_cfg = skill_data.get("mcp_server_config", {})
            if mcp_cfg:
                resolved = _substitute_placeholders(mcp_cfg, agent_name)
                # Use skill name as the MCP server key
                server_name = skill_data["name"]
                mcp_servers[server_name] = resolved

            # Tool patterns
            patterns = skill_data.get("tool_patterns", [])
            for p in patterns:
                if p not in tool_patterns:
                    tool_patterns.append(p)

            # Directives
            directive = skill_data.get("directive", "")
            if directive:
                directives.append(directive)

            # File templates
            templates = skill_data.get("file_templates", {})
            file_templates.update(templates)

        # Skill catalog metadata (name + description for compact prompt listing)
        catalog = []
        for skill_data in agent_skills:
            catalog.append({
                "name": skill_data["name"],
                "description": skill_data.get("description", ""),
            })

        return {
            "mcp_servers": mcp_servers,
            "tool_patterns": tool_patterns,
            "directives": directives,
            "file_templates": file_templates,
            "catalog": catalog,
        }

    def get_catalog_with_counts(self) -> list[dict]:
        """Get all skills with agent assignment counts."""
        skills = self.list()
        result = []
        for skill in skills:
            count = self._db.execute(
                "SELECT COUNT(*) FROM agent_skills WHERE skill_name=? AND enabled=1",
                (skill.name,),
            ).fetchone()[0]
            d = skill.to_dict()
            d["agent_count"] = count
            result.append(d)
        return result

    # ── Per-session overrides (deprecated) ────────────────────

    def enable_for_session(self, session_id: str, skill_name: str) -> bool:
        """Enable a skill for a specific session. (Deprecated — use agent_skills.)"""
        return self._set_session_skill(session_id, skill_name, True)

    def disable_for_session(self, session_id: str, skill_name: str) -> bool:
        """Disable a skill for a specific session. (Deprecated — use agent_skills.)"""
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
        """Get all skills with per-session override status. (Deprecated.)"""
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
        """Remove per-session override. (Deprecated.)"""
        cursor = self._db.execute(
            "DELETE FROM session_skills WHERE session_id=? AND skill_name=?",
            (session_id, skill_name),
        )
        self._db.commit()
        return cursor.rowcount > 0

    def close(self) -> None:
        self._db.close()


def _substitute_placeholders(config: dict, agent_name: str) -> dict:
    """Recursively substitute {agent_name} in MCP server config values."""
    result = {}
    for k, v in config.items():
        if isinstance(v, str):
            result[k] = v.replace("{agent_name}", agent_name)
        elif isinstance(v, list):
            result[k] = [
                item.replace("{agent_name}", agent_name) if isinstance(item, str) else item
                for item in v
            ]
        elif isinstance(v, dict):
            result[k] = _substitute_placeholders(v, agent_name)
        else:
            result[k] = v
    return result
