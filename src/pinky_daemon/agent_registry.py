"""Agent Registry — first-class named agents with persistent identity.

An Agent is the identity layer. Sessions are instances of an agent.
One agent can have many concurrent sessions, all sharing the same
soul, directives, tools, bot tokens, and personality.

Architecture:
    Agent (identity, config, soul)
      └── Session 1 (active context, running Claude Code)
      └── Session 2 (another parallel context)
      └── Session N (infinite scale)

Storage: SQLite with three tables:
  - agents: core agent identity and config
  - agent_directives: per-agent persistent instructions
  - agent_tokens: per-agent platform bot tokens

Hierarchy:
  - Agents can have a parent_id (lead -> worker relationship)
  - Groups organize agents into teams
"""

from __future__ import annotations

import json
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


@dataclass
class Agent:
    """A named agent with persistent identity."""

    name: str  # Unique identifier (e.g., "oleg", "leo", "kai")
    display_name: str = ""  # Human-friendly name
    model: str = "opus"  # Default model for new sessions
    soul: str = ""  # CLAUDE.md content or path
    system_prompt: str = ""  # Base system prompt
    working_dir: str = "."
    permission_mode: str = "auto"
    allowed_tools: list[str] = field(default_factory=list)
    max_turns: int = 25
    timeout: float = 300.0
    restart_threshold_pct: float = 80.0
    auto_restart: bool = True
    parent: str = ""  # Parent agent name (for hierarchy)
    groups: list[str] = field(default_factory=list)
    max_sessions: int = 5  # Max concurrent sessions per agent
    enabled: bool = True
    auto_start: bool = False  # Auto-spawn main session on server boot
    heartbeat_interval: int = 0  # Seconds between heartbeats (0 = disabled)
    role: str = ""  # Agent role: sidekick, lead, worker, specialist
    created_at: float = 0.0
    updated_at: float = 0.0

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "display_name": self.display_name or self.name,
            "model": self.model,
            "soul": self.soul,
            "system_prompt": self.system_prompt,
            "working_dir": self.working_dir,
            "permission_mode": self.permission_mode,
            "allowed_tools": self.allowed_tools,
            "max_turns": self.max_turns,
            "timeout": self.timeout,
            "restart_threshold_pct": self.restart_threshold_pct,
            "auto_restart": self.auto_restart,
            "parent": self.parent,
            "groups": self.groups,
            "max_sessions": self.max_sessions,
            "enabled": self.enabled,
            "auto_start": self.auto_start,
            "heartbeat_interval": self.heartbeat_interval,
            "role": self.role,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass
class AgentDirective:
    """A persistent instruction for an agent."""

    id: int = 0
    agent_name: str = ""
    directive: str = ""  # The instruction text
    priority: int = 0  # Higher = more important
    active: bool = True
    created_at: float = 0.0

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "agent_name": self.agent_name,
            "directive": self.directive,
            "priority": self.priority,
            "active": self.active,
            "created_at": self.created_at,
        }


@dataclass
class AgentToken:
    """A platform bot token for an agent."""

    agent_name: str = ""
    platform: str = ""  # telegram, discord, slack
    token_set: bool = False  # Never expose actual token
    enabled: bool = True
    settings: dict = field(default_factory=dict)
    updated_at: float = 0.0

    def to_dict(self) -> dict:
        return {
            "agent_name": self.agent_name,
            "platform": self.platform,
            "token_set": self.token_set,
            "enabled": self.enabled,
            "settings": self.settings,
            "updated_at": self.updated_at,
        }


@dataclass
class AgentSchedule:
    """A cron-based wake schedule for an agent."""

    id: int = 0
    agent_name: str = ""
    name: str = ""  # Human-friendly name (e.g., "morning_check")
    cron: str = ""  # Cron expression (e.g., "0 8 * * *")
    prompt: str = ""  # Message to send to main session on wake
    timezone: str = "America/Los_Angeles"
    enabled: bool = True
    last_run: float = 0.0
    created_at: float = 0.0

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "agent_name": self.agent_name,
            "name": self.name,
            "cron": self.cron,
            "prompt": self.prompt,
            "timezone": self.timezone,
            "enabled": self.enabled,
            "last_run": self.last_run,
            "created_at": self.created_at,
        }


@dataclass
class AgentHeartbeat:
    """A heartbeat record for an agent."""

    agent_name: str = ""
    session_id: str = ""
    timestamp: float = 0.0
    status: str = "alive"  # alive, stale, dead
    context_pct: float = 0.0
    message_count: int = 0
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "agent_name": self.agent_name,
            "session_id": self.session_id,
            "timestamp": self.timestamp,
            "status": self.status,
            "context_pct": self.context_pct,
            "message_count": self.message_count,
            "metadata": self.metadata,
        }


@dataclass
class AgentContext:
    """Persistent continuation context for an agent.

    Agents set this before a context restart so the next session
    picks up where they left off. Like a save-state for the brain.
    """

    agent_name: str = ""
    task: str = ""  # What was I working on?
    context: str = ""  # Key context/state to preserve
    notes: str = ""  # Freeform notes
    blockers: list[str] = field(default_factory=list)
    priority_items: list[str] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)
    updated_at: float = 0.0
    updated_by: str = ""  # session ID that saved this

    def to_dict(self) -> dict:
        return {
            "agent_name": self.agent_name,
            "task": self.task,
            "context": self.context,
            "notes": self.notes,
            "blockers": self.blockers,
            "priority_items": self.priority_items,
            "metadata": self.metadata,
            "updated_at": self.updated_at,
            "updated_by": self.updated_by,
        }

    def to_prompt(self) -> str:
        """Format as a system prompt section for injection on restart."""
        parts = []
        if self.task:
            parts.append(f"## Continuation\nYou were working on: {self.task}")
        if self.context:
            parts.append(f"### Context\n{self.context}")
        if self.notes:
            parts.append(f"### Notes\n{self.notes}")
        if self.blockers:
            parts.append(f"### Blockers\n" + "\n".join(f"- {b}" for b in self.blockers))
        if self.priority_items:
            parts.append(f"### Priority Items\n" + "\n".join(f"- {p}" for p in self.priority_items))
        return "\n\n".join(parts) if parts else ""


class AgentRegistry:
    """SQLite-backed agent registry."""

    def __init__(self, db_path: str = "data/agents.db") -> None:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._init_tables()

    def _init_tables(self) -> None:
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS agents (
                name TEXT PRIMARY KEY,
                display_name TEXT NOT NULL DEFAULT '',
                model TEXT NOT NULL DEFAULT 'opus',
                soul TEXT NOT NULL DEFAULT '',
                system_prompt TEXT NOT NULL DEFAULT '',
                working_dir TEXT NOT NULL DEFAULT '.',
                permission_mode TEXT NOT NULL DEFAULT 'auto',
                allowed_tools TEXT NOT NULL DEFAULT '[]',
                max_turns INTEGER NOT NULL DEFAULT 25,
                timeout REAL NOT NULL DEFAULT 300.0,
                restart_threshold_pct REAL NOT NULL DEFAULT 80.0,
                auto_restart INTEGER NOT NULL DEFAULT 1,
                parent TEXT NOT NULL DEFAULT '',
                groups TEXT NOT NULL DEFAULT '[]',
                max_sessions INTEGER NOT NULL DEFAULT 5,
                enabled INTEGER NOT NULL DEFAULT 1,
                auto_start INTEGER NOT NULL DEFAULT 0,
                heartbeat_interval INTEGER NOT NULL DEFAULT 0,
                role TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS agent_directives (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_name TEXT NOT NULL,
                directive TEXT NOT NULL,
                priority INTEGER NOT NULL DEFAULT 0,
                active INTEGER NOT NULL DEFAULT 1,
                created_at REAL NOT NULL,
                FOREIGN KEY (agent_name) REFERENCES agents(name) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS agent_tokens (
                agent_name TEXT NOT NULL,
                platform TEXT NOT NULL,
                token TEXT NOT NULL DEFAULT '',
                enabled INTEGER NOT NULL DEFAULT 1,
                settings TEXT NOT NULL DEFAULT '{}',
                updated_at REAL NOT NULL,
                PRIMARY KEY (agent_name, platform),
                FOREIGN KEY (agent_name) REFERENCES agents(name) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS agent_schedules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_name TEXT NOT NULL,
                name TEXT NOT NULL DEFAULT '',
                cron TEXT NOT NULL,
                prompt TEXT NOT NULL DEFAULT '',
                timezone TEXT NOT NULL DEFAULT 'America/Los_Angeles',
                enabled INTEGER NOT NULL DEFAULT 1,
                last_run REAL NOT NULL DEFAULT 0,
                created_at REAL NOT NULL,
                FOREIGN KEY (agent_name) REFERENCES agents(name) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS agent_heartbeats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_name TEXT NOT NULL,
                session_id TEXT NOT NULL DEFAULT '',
                timestamp REAL NOT NULL,
                status TEXT NOT NULL DEFAULT 'alive',
                context_pct REAL NOT NULL DEFAULT 0,
                message_count INTEGER NOT NULL DEFAULT 0,
                metadata TEXT NOT NULL DEFAULT '{}',
                FOREIGN KEY (agent_name) REFERENCES agents(name) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS agent_contexts (
                agent_name TEXT PRIMARY KEY,
                task TEXT NOT NULL DEFAULT '',
                context TEXT NOT NULL DEFAULT '',
                notes TEXT NOT NULL DEFAULT '',
                blockers TEXT NOT NULL DEFAULT '[]',
                priority_items TEXT NOT NULL DEFAULT '[]',
                metadata TEXT NOT NULL DEFAULT '{}',
                updated_at REAL NOT NULL DEFAULT 0,
                updated_by TEXT NOT NULL DEFAULT '',
                FOREIGN KEY (agent_name) REFERENCES agents(name) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_heartbeats_agent
                ON agent_heartbeats(agent_name, timestamp DESC);
            CREATE INDEX IF NOT EXISTS idx_schedules_agent
                ON agent_schedules(agent_name);
        """)
        self._db.commit()
        self._migrate()

    def _migrate(self) -> None:
        """Add new columns to existing databases."""
        existing = {
            row[1] for row in self._db.execute("PRAGMA table_info(agents)").fetchall()
        }
        migrations = [
            ("auto_start", "INTEGER NOT NULL DEFAULT 0"),
            ("heartbeat_interval", "INTEGER NOT NULL DEFAULT 0"),
            ("role", "TEXT NOT NULL DEFAULT ''"),
        ]
        for col, typedef in migrations:
            if col not in existing:
                self._db.execute(f"ALTER TABLE agents ADD COLUMN {col} {typedef}")
                _log(f"agent_registry: migrated — added column {col}")
        self._db.commit()

    # ── Agent CRUD ──────────────────────────────────────────

    def register(self, name: str, **kwargs) -> Agent:
        """Register a new agent or update an existing one."""
        now = time.time()
        existing = self.get(name)

        if existing:
            # Merge: only update provided fields
            updates = {}
            for key in ("display_name", "model", "soul", "system_prompt", "working_dir",
                        "permission_mode", "max_turns", "timeout", "restart_threshold_pct",
                        "auto_restart", "parent", "max_sessions", "enabled",
                        "auto_start", "heartbeat_interval", "role"):
                if key in kwargs:
                    updates[key] = kwargs[key]

            if "allowed_tools" in kwargs:
                updates["allowed_tools"] = json.dumps(kwargs["allowed_tools"])
            if "groups" in kwargs:
                updates["groups"] = json.dumps(kwargs["groups"])
            if "auto_restart" in updates:
                updates["auto_restart"] = int(updates["auto_restart"])
            if "enabled" in updates:
                updates["enabled"] = int(updates["enabled"])
            if "auto_start" in updates:
                updates["auto_start"] = int(updates["auto_start"])

            if updates:
                updates["updated_at"] = now
                set_clause = ", ".join(f"{k}=?" for k in updates)
                self._db.execute(
                    f"UPDATE agents SET {set_clause} WHERE name=?",
                    list(updates.values()) + [name],
                )
                self._db.commit()
        else:
            agent = Agent(
                name=name,
                display_name=kwargs.get("display_name", ""),
                model=kwargs.get("model", "opus"),
                soul=kwargs.get("soul", ""),
                system_prompt=kwargs.get("system_prompt", ""),
                working_dir=kwargs.get("working_dir", "") or f"data/agents/{name}",
                permission_mode=kwargs.get("permission_mode", "auto"),
                allowed_tools=kwargs.get("allowed_tools", []),
                max_turns=kwargs.get("max_turns", 25),
                timeout=kwargs.get("timeout", 300.0),
                restart_threshold_pct=kwargs.get("restart_threshold_pct", 80.0),
                auto_restart=kwargs.get("auto_restart", True),
                parent=kwargs.get("parent", ""),
                groups=kwargs.get("groups", []),
                max_sessions=kwargs.get("max_sessions", 5),
                enabled=kwargs.get("enabled", True),
                auto_start=kwargs.get("auto_start", False),
                heartbeat_interval=kwargs.get("heartbeat_interval", 0),
                role=kwargs.get("role", ""),
                created_at=now,
                updated_at=now,
            )
            self._db.execute(
                """INSERT INTO agents
                   (name, display_name, model, soul, system_prompt, working_dir,
                    permission_mode, allowed_tools, max_turns, timeout,
                    restart_threshold_pct, auto_restart, parent, groups,
                    max_sessions, enabled, auto_start, heartbeat_interval, role,
                    created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (agent.name, agent.display_name, agent.model, agent.soul,
                 agent.system_prompt, agent.working_dir, agent.permission_mode,
                 json.dumps(agent.allowed_tools), agent.max_turns, agent.timeout,
                 agent.restart_threshold_pct, int(agent.auto_restart),
                 agent.parent, json.dumps(agent.groups), agent.max_sessions,
                 int(agent.enabled), int(agent.auto_start), agent.heartbeat_interval,
                 agent.role, agent.created_at, agent.updated_at),
            )
            self._db.commit()
            _log(f"agents: registered {name}")

        return self.get(name)  # type: ignore

    _AGENT_COLUMNS = (
        "name, display_name, model, soul, system_prompt, working_dir, "
        "permission_mode, allowed_tools, max_turns, timeout, "
        "restart_threshold_pct, auto_restart, parent, groups, "
        "max_sessions, enabled, auto_start, heartbeat_interval, role, "
        "created_at, updated_at"
    )

    def get(self, name: str) -> Agent | None:
        """Get an agent by name."""
        row = self._db.execute(
            f"SELECT {self._AGENT_COLUMNS} FROM agents WHERE name=?",
            (name,),
        ).fetchone()
        if not row:
            return None
        return self._row_to_agent(row)

    def list(self, *, parent: str = "", group: str = "", enabled_only: bool = False) -> list[Agent]:
        """List agents with optional filters."""
        sql = f"SELECT {self._AGENT_COLUMNS} FROM agents WHERE 1=1"
        params: list = []

        if parent:
            sql += " AND parent=?"
            params.append(parent)
        if enabled_only:
            sql += " AND enabled=1"

        sql += " ORDER BY name"
        rows = self._db.execute(sql, params).fetchall()
        agents = [self._row_to_agent(r) for r in rows]

        if group:
            agents = [a for a in agents if group in a.groups]

        return agents

    def delete(self, name: str) -> bool:
        """Delete an agent and all its directives/tokens (cascade)."""
        cursor = self._db.execute("DELETE FROM agents WHERE name=?", (name,))
        self._db.commit()
        if cursor.rowcount > 0:
            _log(f"agents: deleted {name}")
            return True
        return False

    def get_children(self, parent_name: str) -> list[Agent]:
        """Get all child agents of a parent."""
        return self.list(parent=parent_name)

    def get_hierarchy(self, name: str) -> dict:
        """Get an agent and its full hierarchy tree."""
        agent = self.get(name)
        if not agent:
            return {}
        children = self.get_children(name)
        return {
            "agent": agent.to_dict(),
            "children": [self.get_hierarchy(c.name) for c in children],
        }

    # ── Directives ──────────────────────────────────────────

    def add_directive(self, agent_name: str, directive: str, *, priority: int = 0) -> AgentDirective:
        """Add a directive to an agent."""
        now = time.time()
        cursor = self._db.execute(
            "INSERT INTO agent_directives (agent_name, directive, priority, active, created_at) VALUES (?, ?, ?, 1, ?)",
            (agent_name, directive, priority, now),
        )
        self._db.commit()
        return AgentDirective(
            id=cursor.lastrowid,
            agent_name=agent_name,
            directive=directive,
            priority=priority,
            active=True,
            created_at=now,
        )

    def get_directives(self, agent_name: str, *, active_only: bool = True) -> list[AgentDirective]:
        """Get all directives for an agent, ordered by priority desc."""
        sql = "SELECT id, agent_name, directive, priority, active, created_at FROM agent_directives WHERE agent_name=?"
        params: list = [agent_name]
        if active_only:
            sql += " AND active=1"
        sql += " ORDER BY priority DESC, created_at ASC"
        rows = self._db.execute(sql, params).fetchall()
        return [
            AgentDirective(id=r[0], agent_name=r[1], directive=r[2], priority=r[3], active=bool(r[4]), created_at=r[5])
            for r in rows
        ]

    def remove_directive(self, directive_id: int) -> bool:
        """Remove a directive."""
        cursor = self._db.execute("DELETE FROM agent_directives WHERE id=?", (directive_id,))
        self._db.commit()
        return cursor.rowcount > 0

    def toggle_directive(self, directive_id: int, active: bool) -> bool:
        """Enable/disable a directive."""
        cursor = self._db.execute(
            "UPDATE agent_directives SET active=? WHERE id=?",
            (int(active), directive_id),
        )
        self._db.commit()
        return cursor.rowcount > 0

    def build_system_prompt(self, agent_name: str) -> str:
        """Build a complete system prompt from agent config + directives.

        Combines the agent's base system_prompt with all active directives,
        ordered by priority. This is what gets passed to Claude Code.
        """
        agent = self.get(agent_name)
        if not agent:
            return ""

        parts = []
        if agent.soul:
            parts.append(agent.soul)
        if agent.system_prompt:
            parts.append(agent.system_prompt)

        directives = self.get_directives(agent_name)
        if directives:
            directive_text = "\n".join(f"- {d.directive}" for d in directives)
            parts.append(f"\n## Active Directives\n{directive_text}")

        # Append memory tier guidance for all agents
        parts.append(
            "## Memory\n"
            "- Working memory: Use native MEMORY.md and memory/*.md files for active project state\n"
            "- Long-term memory: Use reflect() to store important cross-session learnings\n"
            "- Recall: Use recall(\"query\") to search long-term memory when context is missing\n"
            "- Don't duplicate — if it's in MEMORY.md, don't also reflect() it unless it needs semantic search"
        )

        return "\n\n".join(parts)

    # ── Tokens ──────────────────────────────────────────────

    def set_token(self, agent_name: str, platform: str, token: str, **kwargs) -> AgentToken:
        """Set a platform bot token for an agent."""
        now = time.time()
        enabled = kwargs.get("enabled", True)
        settings = kwargs.get("settings", {})

        self._db.execute(
            """INSERT INTO agent_tokens (agent_name, platform, token, enabled, settings, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT (agent_name, platform)
               DO UPDATE SET token=excluded.token, enabled=excluded.enabled,
                            settings=excluded.settings, updated_at=excluded.updated_at""",
            (agent_name, platform, token, int(enabled), json.dumps(settings), now),
        )
        self._db.commit()
        return self.get_token(agent_name, platform)  # type: ignore

    def get_token(self, agent_name: str, platform: str) -> AgentToken | None:
        """Get token config for an agent+platform (token value never exposed)."""
        row = self._db.execute(
            "SELECT agent_name, platform, token, enabled, settings, updated_at FROM agent_tokens WHERE agent_name=? AND platform=?",
            (agent_name, platform),
        ).fetchone()
        if not row:
            return None
        return AgentToken(
            agent_name=row[0], platform=row[1], token_set=bool(row[2]),
            enabled=bool(row[3]), settings=json.loads(row[4]), updated_at=row[5],
        )

    def get_raw_token(self, agent_name: str, platform: str) -> str:
        """Get the actual token value (internal use only)."""
        row = self._db.execute(
            "SELECT token FROM agent_tokens WHERE agent_name=? AND platform=?",
            (agent_name, platform),
        ).fetchone()
        return row[0] if row else ""

    def list_tokens(self, agent_name: str) -> list[AgentToken]:
        """List all tokens for an agent."""
        rows = self._db.execute(
            "SELECT agent_name, platform, token, enabled, settings, updated_at FROM agent_tokens WHERE agent_name=? ORDER BY platform",
            (agent_name,),
        ).fetchall()
        return [
            AgentToken(agent_name=r[0], platform=r[1], token_set=bool(r[2]),
                       enabled=bool(r[3]), settings=json.loads(r[4]), updated_at=r[5])
            for r in rows
        ]

    def remove_token(self, agent_name: str, platform: str) -> bool:
        """Remove a token."""
        cursor = self._db.execute(
            "DELETE FROM agent_tokens WHERE agent_name=? AND platform=?",
            (agent_name, platform),
        )
        self._db.commit()
        return cursor.rowcount > 0

    # ── Schedules ───────────────────────────────────────────

    def add_schedule(
        self, agent_name: str, cron: str, *,
        name: str = "", prompt: str = "", timezone: str = "America/Los_Angeles",
    ) -> AgentSchedule:
        """Add a cron-based wake schedule for an agent."""
        now = time.time()
        cursor = self._db.execute(
            """INSERT INTO agent_schedules (agent_name, name, cron, prompt, timezone, enabled, last_run, created_at)
               VALUES (?, ?, ?, ?, ?, 1, 0, ?)""",
            (agent_name, name, cron, prompt, timezone, now),
        )
        self._db.commit()
        return AgentSchedule(
            id=cursor.lastrowid, agent_name=agent_name, name=name,
            cron=cron, prompt=prompt, timezone=timezone,
            enabled=True, last_run=0.0, created_at=now,
        )

    def get_schedules(self, agent_name: str, *, enabled_only: bool = True) -> list[AgentSchedule]:
        """Get all schedules for an agent."""
        sql = "SELECT id, agent_name, name, cron, prompt, timezone, enabled, last_run, created_at FROM agent_schedules WHERE agent_name=?"
        params: list = [agent_name]
        if enabled_only:
            sql += " AND enabled=1"
        sql += " ORDER BY created_at ASC"
        rows = self._db.execute(sql, params).fetchall()
        return [
            AgentSchedule(id=r[0], agent_name=r[1], name=r[2], cron=r[3],
                         prompt=r[4], timezone=r[5], enabled=bool(r[6]),
                         last_run=r[7], created_at=r[8])
            for r in rows
        ]

    def get_all_schedules(self, *, enabled_only: bool = True) -> list[AgentSchedule]:
        """Get all schedules across all agents."""
        sql = "SELECT id, agent_name, name, cron, prompt, timezone, enabled, last_run, created_at FROM agent_schedules"
        if enabled_only:
            sql += " WHERE enabled=1"
        sql += " ORDER BY agent_name, created_at ASC"
        rows = self._db.execute(sql).fetchall()
        return [
            AgentSchedule(id=r[0], agent_name=r[1], name=r[2], cron=r[3],
                         prompt=r[4], timezone=r[5], enabled=bool(r[6]),
                         last_run=r[7], created_at=r[8])
            for r in rows
        ]

    def remove_schedule(self, schedule_id: int) -> bool:
        """Remove a schedule."""
        cursor = self._db.execute("DELETE FROM agent_schedules WHERE id=?", (schedule_id,))
        self._db.commit()
        return cursor.rowcount > 0

    def toggle_schedule(self, schedule_id: int, enabled: bool) -> bool:
        """Enable/disable a schedule."""
        cursor = self._db.execute(
            "UPDATE agent_schedules SET enabled=? WHERE id=?",
            (int(enabled), schedule_id),
        )
        self._db.commit()
        return cursor.rowcount > 0

    def update_schedule_last_run(self, schedule_id: int, timestamp: float = 0.0) -> None:
        """Record when a schedule last ran."""
        ts = timestamp or time.time()
        self._db.execute(
            "UPDATE agent_schedules SET last_run=? WHERE id=?",
            (ts, schedule_id),
        )
        self._db.commit()

    # ── Heartbeats ─────────────────────────────────────────

    def record_heartbeat(
        self, agent_name: str, *,
        session_id: str = "", status: str = "alive",
        context_pct: float = 0.0, message_count: int = 0,
        metadata: dict | None = None,
    ) -> AgentHeartbeat:
        """Record a heartbeat for an agent."""
        now = time.time()
        meta_json = json.dumps(metadata or {})
        self._db.execute(
            """INSERT INTO agent_heartbeats
               (agent_name, session_id, timestamp, status, context_pct, message_count, metadata)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (agent_name, session_id, now, status, context_pct, message_count, meta_json),
        )
        self._db.commit()

        # Prune old heartbeats (keep last 100 per agent)
        self._db.execute(
            """DELETE FROM agent_heartbeats WHERE agent_name=? AND id NOT IN
               (SELECT id FROM agent_heartbeats WHERE agent_name=?
                ORDER BY timestamp DESC LIMIT 100)""",
            (agent_name, agent_name),
        )
        self._db.commit()

        return AgentHeartbeat(
            agent_name=agent_name, session_id=session_id,
            timestamp=now, status=status, context_pct=context_pct,
            message_count=message_count, metadata=metadata or {},
        )

    def get_latest_heartbeat(self, agent_name: str) -> AgentHeartbeat | None:
        """Get the most recent heartbeat for an agent."""
        row = self._db.execute(
            """SELECT agent_name, session_id, timestamp, status, context_pct, message_count, metadata
               FROM agent_heartbeats WHERE agent_name=?
               ORDER BY timestamp DESC LIMIT 1""",
            (agent_name,),
        ).fetchone()
        if not row:
            return None
        return AgentHeartbeat(
            agent_name=row[0], session_id=row[1], timestamp=row[2],
            status=row[3], context_pct=row[4], message_count=row[5],
            metadata=json.loads(row[6]),
        )

    def get_heartbeats(self, agent_name: str, *, limit: int = 20) -> list[AgentHeartbeat]:
        """Get recent heartbeats for an agent."""
        rows = self._db.execute(
            """SELECT agent_name, session_id, timestamp, status, context_pct, message_count, metadata
               FROM agent_heartbeats WHERE agent_name=?
               ORDER BY timestamp DESC LIMIT ?""",
            (agent_name, limit),
        ).fetchall()
        return [
            AgentHeartbeat(
                agent_name=r[0], session_id=r[1], timestamp=r[2],
                status=r[3], context_pct=r[4], message_count=r[5],
                metadata=json.loads(r[6]),
            )
            for r in rows
        ]

    def get_all_latest_heartbeats(self) -> list[AgentHeartbeat]:
        """Get the latest heartbeat for every agent."""
        rows = self._db.execute(
            """SELECT h.agent_name, h.session_id, h.timestamp, h.status,
                      h.context_pct, h.message_count, h.metadata
               FROM agent_heartbeats h
               INNER JOIN (
                   SELECT agent_name, MAX(timestamp) as max_ts
                   FROM agent_heartbeats GROUP BY agent_name
               ) latest ON h.agent_name = latest.agent_name AND h.timestamp = latest.max_ts
               ORDER BY h.agent_name""",
        ).fetchall()
        return [
            AgentHeartbeat(
                agent_name=r[0], session_id=r[1], timestamp=r[2],
                status=r[3], context_pct=r[4], message_count=r[5],
                metadata=json.loads(r[6]),
            )
            for r in rows
        ]

    def list_auto_start_agents(self) -> list[Agent]:
        """List all enabled agents with auto_start=True."""
        rows = self._db.execute(
            f"SELECT {self._AGENT_COLUMNS} FROM agents WHERE enabled=1 AND auto_start=1 ORDER BY name",
        ).fetchall()
        return [self._row_to_agent(r) for r in rows]

    # ── Wake Context ───────────────────────────────────────

    def set_context(
        self, agent_name: str, *,
        task: str = "", context: str = "", notes: str = "",
        blockers: list[str] | None = None,
        priority_items: list[str] | None = None,
        metadata: dict | None = None,
        updated_by: str = "",
    ) -> AgentContext:
        """Save continuation context for an agent.

        Called by the agent before a context restart so the next
        session can pick up where it left off.
        """
        now = time.time()
        self._db.execute(
            """INSERT INTO agent_contexts
               (agent_name, task, context, notes, blockers, priority_items, metadata, updated_at, updated_by)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT (agent_name) DO UPDATE SET
                task=excluded.task, context=excluded.context, notes=excluded.notes,
                blockers=excluded.blockers, priority_items=excluded.priority_items,
                metadata=excluded.metadata, updated_at=excluded.updated_at,
                updated_by=excluded.updated_by""",
            (agent_name, task, context, notes,
             json.dumps(blockers or []), json.dumps(priority_items or []),
             json.dumps(metadata or {}), now, updated_by),
        )
        self._db.commit()
        _log(f"agents: saved context for {agent_name}")
        return self.get_context(agent_name)  # type: ignore

    def get_context(self, agent_name: str) -> AgentContext | None:
        """Get the saved continuation context for an agent."""
        row = self._db.execute(
            """SELECT agent_name, task, context, notes, blockers, priority_items,
                      metadata, updated_at, updated_by
               FROM agent_contexts WHERE agent_name=?""",
            (agent_name,),
        ).fetchone()
        if not row:
            return None
        return AgentContext(
            agent_name=row[0], task=row[1], context=row[2], notes=row[3],
            blockers=json.loads(row[4]), priority_items=json.loads(row[5]),
            metadata=json.loads(row[6]), updated_at=row[7], updated_by=row[8],
        )

    def clear_context(self, agent_name: str) -> bool:
        """Clear the continuation context after it's been consumed."""
        cursor = self._db.execute(
            "DELETE FROM agent_contexts WHERE agent_name=?", (agent_name,),
        )
        self._db.commit()
        return cursor.rowcount > 0

    # ── Helpers ──────────────────────────────────────────────

    def _row_to_agent(self, row: tuple) -> Agent:
        return Agent(
            name=row[0], display_name=row[1], model=row[2], soul=row[3],
            system_prompt=row[4], working_dir=row[5], permission_mode=row[6],
            allowed_tools=json.loads(row[7]), max_turns=row[8], timeout=row[9],
            restart_threshold_pct=row[10], auto_restart=bool(row[11]),
            parent=row[12], groups=json.loads(row[13]), max_sessions=row[14],
            enabled=bool(row[15]),
            auto_start=bool(row[16]) if len(row) > 16 else False,
            heartbeat_interval=row[17] if len(row) > 17 else 0,
            role=row[18] if len(row) > 18 else "",
            created_at=row[19] if len(row) > 19 else row[16],
            updated_at=row[20] if len(row) > 20 else row[17],
        )

    def close(self) -> None:
        self._db.close()
