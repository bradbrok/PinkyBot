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


def _cron_next_run(cron: str, timezone: str = "UTC") -> float | None:
    """Compute the next run timestamp for a cron expression using stdlib only.

    Supports standard 5-field cron: min hour dom month dow.
    Returns a UTC unix timestamp, or None on parse error.
    """
    try:
        import datetime as dt
        import zoneinfo

        parts = cron.strip().split()
        if len(parts) != 5:
            return None

        def _parse_field(val: str, lo: int, hi: int) -> set[int]:
            result: set[int] = set()
            for part in val.split(","):
                if part == "*":
                    result.update(range(lo, hi + 1))
                elif "/" in part:
                    base, step = part.split("/", 1)
                    start = lo if base == "*" else int(base)
                    result.update(range(start, hi + 1, int(step)))
                elif "-" in part:
                    a, b = part.split("-", 1)
                    result.update(range(int(a), int(b) + 1))
                else:
                    result.add(int(part))
            return result

        minutes = _parse_field(parts[0], 0, 59)
        hours = _parse_field(parts[1], 0, 23)
        doms = _parse_field(parts[2], 1, 31)
        months = _parse_field(parts[3], 1, 12)
        dows = _parse_field(parts[4], 0, 6)  # 0=Sun

        try:
            tz = zoneinfo.ZoneInfo(timezone)
        except Exception:
            tz = zoneinfo.ZoneInfo("UTC")

        now = dt.datetime.now(tz)
        candidate = now.replace(second=0, microsecond=0) + dt.timedelta(minutes=1)

        for _ in range(527040):  # max 1 year of minutes
            if (
                candidate.month in months
                and candidate.day in doms
                and (candidate.weekday() + 1) % 7 in dows  # Python Mon=0→1, Sun=6→0; cron Sun=0
                and candidate.hour in hours
                and candidate.minute in minutes
            ):
                return candidate.timestamp()
            candidate += dt.timedelta(minutes=1)

        return None
    except Exception:
        return None


DEFAULT_HEARTBEAT_PROMPT = (
    "Heartbeat. Call send_heartbeat() with your current status. "
    "Use status='ok' if idle, 'busy' if mid-task, 'finishing' if wrapping up. "
    "Pass context_pct if you know your context window usage. No other response needed."
)

OWNER_PROFILE_FIELDS = (
    "owner_name",
    "owner_pronouns",
    "owner_timezone",
    "owner_role",
    "owner_comm_style",
    "owner_languages",
    "owner_locale",
    "owner_code_word",
)

# Map stored key (without owner_ prefix) → display label
_OWNER_FIELD_LABELS = {
    "name": "Name",
    "pronouns": "Pronouns",
    "timezone": "Timezone",
    "role": "Role / About",
    "comm_style": "Communication Style",
    "languages": "Languages",
    "locale": "UI Language",
}


@dataclass
class Agent:
    """A named agent with persistent identity."""

    name: str  # Unique identifier (e.g., "oleg", "leo", "kai")
    display_name: str = ""  # Human-friendly name
    model: str = "opus"  # Default model for new sessions
    soul: str = ""  # Core identity, personality, purpose
    users: str = ""  # Who this agent serves, user profiles
    boundaries: str = ""  # Rules, constraints, what to avoid
    system_prompt: str = ""  # (deprecated) Base system prompt — use soul/users/boundaries instead
    working_dir: str = "."
    permission_mode: str = "auto"
    allowed_tools: list[str] = field(default_factory=list)
    disallowed_tools: list[str] = field(default_factory=list)
    max_turns: int = 0
    timeout: float = 300.0
    restart_threshold_pct: float = 80.0
    auto_restart: bool = True
    parent: str = ""  # Parent agent name (for hierarchy)
    groups: list[str] = field(default_factory=list)
    max_sessions: int = 5  # Max concurrent sessions per agent
    enabled: bool = True
    auto_start: bool = False  # Auto-spawn main session on server boot
    heartbeat_interval: int = 0  # Seconds between heartbeats (0 = disabled)
    wake_interval: int = 0  # Seconds between wake checks (0 = disabled, 1800 = 30m, 3600 = 1h)
    clock_aligned: bool = True  # Align wakes to wall clock (:00, :30 for 30m; :00 for 1h)
    auto_sleep_hours: int = 8  # Auto-sleep after N hours inactive (0 = disabled)
    plain_text_fallback: bool = False  # Auto-send assistant text when no outreach tool was used
    voice_config: dict = field(default_factory=dict)  # Per-agent voice settings (JSON blob)
    # voice_config schema: {
    #   "voice_reply": true,           # auto-TTS when replying to voice messages
    #   "transcribe_provider": "openai", # STT provider (openai, deepgram)
    #   "tts_provider": "openai",      # TTS provider (openai, elevenlabs, deepgram)
    #   "tts_voice": "alloy",          # Voice ID/name
    #   "tts_model": "",               # Model override (provider-specific)
    #   "platforms": {                  # Per-platform overrides
    #     "telegram": {"tts_provider": "elevenlabs", "tts_voice": "...", "tts_model": "..."},
    #     "discord": {"tts_provider": "openai", "tts_voice": "nova"}
    #   }
    # }
    role: str = ""  # Agent role: sidekick, lead, worker, specialist
    dream_enabled: bool = False  # Enable nightly memory consolidation
    dream_schedule: str = "0 3 * * *"  # Cron for dream runs (default 3 AM)
    dream_timezone: str = "America/Los_Angeles"  # IANA timezone for dream schedule
    dream_model: str = ""  # Model override for dream runs (empty = use agent's model)
    dream_notify: bool = True  # Inject dream summary into morning wake context
    status: str = "active"  # active or retired
    retired_at: float = 0.0  # When was this agent retired
    working_status: str = "idle"  # idle, working, offline
    working_status_updated_at: float = 0.0  # When working_status last changed
    provider_url: str = ""   # e.g. "http://localhost:11434" for Ollama, empty = Anthropic default
    provider_key: str = ""   # API key override, empty = use ANTHROPIC_API_KEY env var
    provider_model: str = ""  # model name override (e.g. "llama3.2"), empty = use agent.model
    provider_ref: str = ""   # ID of a global provider from the providers table
    created_at: float = 0.0
    updated_at: float = 0.0

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "display_name": self.display_name or self.name,
            "model": self.model,
            "soul": self.soul,
            "users": self.users,
            "boundaries": self.boundaries,
            "system_prompt": self.system_prompt,
            "working_dir": self.working_dir,
            "permission_mode": self.permission_mode,
            "allowed_tools": self.allowed_tools,
            "disallowed_tools": self.disallowed_tools,
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
            "wake_interval": self.wake_interval,
            "clock_aligned": self.clock_aligned,
            "auto_sleep_hours": self.auto_sleep_hours,
            "plain_text_fallback": self.plain_text_fallback,
            "voice_config": self.voice_config,
            "role": self.role,
            "dream_enabled": self.dream_enabled,
            "dream_schedule": self.dream_schedule,
            "dream_timezone": self.dream_timezone,
            "dream_model": self.dream_model,
            "dream_notify": self.dream_notify,
            "status": self.status,
            "retired_at": self.retired_at,
            "working_status": self.working_status,
            "working_status_updated_at": self.working_status_updated_at,
            "provider_url": self.provider_url,
            "provider_key": self.provider_key,
            "provider_model": self.provider_model,
            "provider_ref": self.provider_ref,
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
class ApprovedUser:
    """An approved Telegram user for an agent."""

    id: int = 0
    agent_name: str = ""
    chat_id: str = ""  # Telegram chat/user ID
    display_name: str = ""  # Human-friendly name
    status: str = "approved"  # approved, denied, pending
    approved_by: str = ""  # Who approved this user
    timezone: str = ""  # IANA timezone (e.g., "America/Los_Angeles")
    created_at: float = 0.0
    updated_at: float = 0.0

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "agent_name": self.agent_name,
            "chat_id": self.chat_id,
            "display_name": self.display_name,
            "status": self.status,
            "approved_by": self.approved_by,
            "timezone": self.timezone,
            "created_at": self.created_at,
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
    direct_send: bool = False  # If true, prompt is sent directly as a message (not as agent input)
    target_channel: str = ""  # Chat ID or channel for direct_send routing

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
            "next_run": _cron_next_run(self.cron, self.timezone),
            "created_at": self.created_at,
            "direct_send": self.direct_send,
            "target_channel": self.target_channel,
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
    notes: str = ""          # Freeform notes from agent
    latency_ms: int = 0      # Response latency in ms (trigger → tool call)

    def to_dict(self) -> dict:
        return {
            "agent_name": self.agent_name,
            "session_id": self.session_id,
            "timestamp": self.timestamp,
            "status": self.status,
            "context_pct": self.context_pct,
            "message_count": self.message_count,
            "metadata": self.metadata,
            "notes": self.notes,
            "latency_ms": self.latency_ms,
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
            parts.append("### Blockers\n" + "\n".join(f"- {b}" for b in self.blockers))
        if self.priority_items:
            parts.append("### Priority Items\n" + "\n".join(f"- {p}" for p in self.priority_items))
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
                max_turns INTEGER NOT NULL DEFAULT 0,
                timeout REAL NOT NULL DEFAULT 300.0,
                restart_threshold_pct REAL NOT NULL DEFAULT 80.0,
                auto_restart INTEGER NOT NULL DEFAULT 1,
                parent TEXT NOT NULL DEFAULT '',
                groups TEXT NOT NULL DEFAULT '[]',
                max_sessions INTEGER NOT NULL DEFAULT 5,
                enabled INTEGER NOT NULL DEFAULT 1,
                auto_start INTEGER NOT NULL DEFAULT 0,
                heartbeat_interval INTEGER NOT NULL DEFAULT 0,
                plain_text_fallback INTEGER NOT NULL DEFAULT 0,
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

            CREATE TABLE IF NOT EXISTS approved_users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_name TEXT NOT NULL,
                chat_id TEXT NOT NULL,
                display_name TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'approved',
                approved_by TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                FOREIGN KEY (agent_name) REFERENCES agents(name) ON DELETE CASCADE,
                UNIQUE(agent_name, chat_id)
            );

            CREATE TABLE IF NOT EXISTS pending_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_name TEXT NOT NULL,
                platform TEXT NOT NULL,
                chat_id TEXT NOT NULL,
                sender_name TEXT NOT NULL DEFAULT '',
                content TEXT NOT NULL,
                created_at REAL NOT NULL,
                delivered INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY (agent_name) REFERENCES agents(name) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS group_chats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_name TEXT NOT NULL,
                platform TEXT NOT NULL DEFAULT 'telegram',
                chat_id TEXT NOT NULL,
                chat_title TEXT NOT NULL DEFAULT '',
                alias TEXT NOT NULL DEFAULT '',
                chat_type TEXT NOT NULL DEFAULT 'group',
                member_count INTEGER NOT NULL DEFAULT 0,
                joined_at REAL NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                FOREIGN KEY (agent_name) REFERENCES agents(name) ON DELETE CASCADE,
                UNIQUE(agent_name, chat_id)
            );

            CREATE TABLE IF NOT EXISTS agent_costs (
                agent_name TEXT NOT NULL,
                cost_usd REAL NOT NULL DEFAULT 0,
                input_tokens INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0,
                turns INTEGER NOT NULL DEFAULT 0,
                timestamp REAL NOT NULL,
                session_id TEXT NOT NULL DEFAULT '',
                FOREIGN KEY (agent_name) REFERENCES agents(name) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS system_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS channel_sessions (
                agent_name TEXT NOT NULL,
                chat_id TEXT NOT NULL,
                session_label TEXT NOT NULL DEFAULT 'main',
                FOREIGN KEY (agent_name) REFERENCES agents(name) ON DELETE CASCADE,
                UNIQUE(agent_name, chat_id)
            );

            CREATE TABLE IF NOT EXISTS streaming_session_labels (
                agent_name TEXT NOT NULL,
                label TEXT NOT NULL DEFAULT 'main',
                session_id TEXT NOT NULL DEFAULT '',
                updated_at REAL NOT NULL DEFAULT 0,
                PRIMARY KEY (agent_name, label),
                FOREIGN KEY (agent_name) REFERENCES agents(name) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS agent_mcp_servers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_name TEXT NOT NULL,
                server_name TEXT NOT NULL,
                server_type TEXT NOT NULL DEFAULT 'stdio',
                command TEXT NOT NULL DEFAULT '',
                args TEXT NOT NULL DEFAULT '[]',
                url TEXT NOT NULL DEFAULT '',
                env TEXT NOT NULL DEFAULT '{}',
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at REAL NOT NULL,
                FOREIGN KEY (agent_name) REFERENCES agents(name) ON DELETE CASCADE,
                UNIQUE(agent_name, server_name)
            );

            CREATE INDEX IF NOT EXISTS idx_heartbeats_agent
                ON agent_heartbeats(agent_name, timestamp DESC);
            CREATE INDEX IF NOT EXISTS idx_schedules_agent
                ON agent_schedules(agent_name);
            CREATE INDEX IF NOT EXISTS idx_pending_messages_agent_chat
                ON pending_messages(agent_name, chat_id, delivered);
            CREATE INDEX IF NOT EXISTS idx_group_chats_agent
                ON group_chats(agent_name);
            CREATE INDEX IF NOT EXISTS idx_streaming_session_labels_agent
                ON streaming_session_labels(agent_name);
            CREATE INDEX IF NOT EXISTS idx_mcp_servers_agent
                ON agent_mcp_servers(agent_name);

            CREATE TABLE IF NOT EXISTS soul_versions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_name TEXT NOT NULL,
                content TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'unknown',
                created_at REAL NOT NULL,
                FOREIGN KEY (agent_name) REFERENCES agents(name) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_soul_versions_agent
                ON soul_versions(agent_name, created_at DESC);

            CREATE TABLE IF NOT EXISTS providers (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                preset TEXT NOT NULL DEFAULT '',
                provider_url TEXT NOT NULL,
                provider_key TEXT NOT NULL DEFAULT '',
                provider_model TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL DEFAULT 0,
                updated_at REAL NOT NULL DEFAULT 0
            );
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
            ("plain_text_fallback", "INTEGER NOT NULL DEFAULT 1"),
            ("role", "TEXT NOT NULL DEFAULT ''"),
            ("users", "TEXT NOT NULL DEFAULT ''"),
            ("boundaries", "TEXT NOT NULL DEFAULT ''"),
            ("status", "TEXT NOT NULL DEFAULT 'active'"),
            ("retired_at", "REAL NOT NULL DEFAULT 0"),
            ("streaming_session_id", "TEXT NOT NULL DEFAULT ''"),
            ("wake_interval", "INTEGER NOT NULL DEFAULT 0"),
            ("clock_aligned", "INTEGER NOT NULL DEFAULT 1"),
            ("auto_sleep_hours", "INTEGER NOT NULL DEFAULT 8"),
            ("voice_config", "TEXT NOT NULL DEFAULT '{}'"),
            ("dream_enabled", "INTEGER NOT NULL DEFAULT 0"),
            ("dream_schedule", "TEXT NOT NULL DEFAULT '0 3 * * *'"),
            ("dream_timezone", "TEXT NOT NULL DEFAULT 'America/Los_Angeles'"),
            ("dream_model", "TEXT NOT NULL DEFAULT ''"),
            ("dream_notify", "INTEGER NOT NULL DEFAULT 1"),
            ("working_status", "TEXT NOT NULL DEFAULT 'idle'"),
            ("working_status_updated_at", "REAL NOT NULL DEFAULT 0"),
            ("provider_url", "TEXT NOT NULL DEFAULT ''"),
            ("provider_key", "TEXT NOT NULL DEFAULT ''"),
            ("provider_model", "TEXT NOT NULL DEFAULT ''"),
            ("provider_ref", "TEXT NOT NULL DEFAULT ''"),
            ("disallowed_tools", "TEXT NOT NULL DEFAULT '[]'"),
        ]
        for col, typedef in migrations:
            if col not in existing:
                self._db.execute(f"ALTER TABLE agents ADD COLUMN {col} {typedef}")
                _log(f"agent_registry: migrated — added column {col}")

        # Migrate agent_schedules table
        sched_existing = {
            row[1] for row in self._db.execute("PRAGMA table_info(agent_schedules)").fetchall()
        }
        sched_migrations = [
            ("direct_send", "INTEGER NOT NULL DEFAULT 0"),
            ("target_channel", "TEXT NOT NULL DEFAULT ''"),
        ]
        for col, typedef in sched_migrations:
            if col not in sched_existing:
                self._db.execute(f"ALTER TABLE agent_schedules ADD COLUMN {col} {typedef}")
                _log(f"agent_registry: migrated — added {col} to agent_schedules")

        # Migrate agent_heartbeats table
        hb_existing = {
            row[1] for row in self._db.execute("PRAGMA table_info(agent_heartbeats)").fetchall()
        }
        hb_migrations = [
            ("notes", "TEXT NOT NULL DEFAULT ''"),
            ("latency_ms", "INTEGER NOT NULL DEFAULT 0"),
        ]
        for col, typedef in hb_migrations:
            if col not in hb_existing:
                self._db.execute(f"ALTER TABLE agent_heartbeats ADD COLUMN {col} {typedef}")
                _log(f"agent_registry: migrated — added {col} to agent_heartbeats")

        # Migrate approved_users table
        au_existing = {
            row[1] for row in self._db.execute("PRAGMA table_info(approved_users)").fetchall()
        }
        if "timezone" not in au_existing:
            self._db.execute("ALTER TABLE approved_users ADD COLUMN timezone TEXT NOT NULL DEFAULT ''")
            _log("agent_registry: migrated — added timezone to approved_users")

        # Seed main_agent default
        if not self.get_setting("main_agent"):
            row = self._db.execute(
                "SELECT name FROM agents WHERE name='barsik' AND enabled=1",
            ).fetchone()
            if row:
                self.set_setting("main_agent", "barsik")
                _log("agent_registry: seeded main_agent=barsik")

        self._db.commit()

    # ── Workspace Init ─────────────────────────────────────

    @staticmethod
    def _init_workspace(work_dir: Path) -> None:
        """Create an agent workspace with default directory structure.

        Creates:
            workspace/
            ├── data/           # SQLite databases (memory.db, etc.)
            ├── output/         # Agent-generated output (reports, exports)
            └── CLAUDE.md       # Written by spawn, not here
        """
        try:
            work_dir.mkdir(parents=True, exist_ok=True)
            (work_dir / "data").mkdir(exist_ok=True)
            (work_dir / "output").mkdir(exist_ok=True)
            (work_dir / "workspace").mkdir(exist_ok=True)
        except PermissionError:
            _log(f"agent_registry: workspace init skipped for {work_dir} (permission denied)")

    # ── Agent CRUD ──────────────────────────────────────────

    def register(self, name: str, **kwargs) -> Agent:
        """Register a new agent or update an existing one."""
        now = time.time()
        existing = self.get(name)

        if existing:
            # Merge: only update provided fields
            updates = {}
            for key in ("display_name", "model", "soul", "users", "boundaries",
                        "system_prompt", "working_dir",
                        "permission_mode", "max_turns", "timeout", "restart_threshold_pct",
                        "auto_restart", "parent", "max_sessions", "enabled",
                        "auto_start", "heartbeat_interval", "wake_interval",
                        "clock_aligned", "auto_sleep_hours", "plain_text_fallback", "voice_config", "role",
                        "dream_enabled", "dream_schedule", "dream_timezone", "dream_model", "dream_notify",
                        "provider_url", "provider_key", "provider_model", "provider_ref"):
                if key in kwargs:
                    updates[key] = kwargs[key]

            if "allowed_tools" in kwargs:
                updates["allowed_tools"] = json.dumps(kwargs["allowed_tools"])
            if "disallowed_tools" in kwargs:
                updates["disallowed_tools"] = json.dumps(kwargs["disallowed_tools"])
            if "groups" in kwargs:
                updates["groups"] = json.dumps(kwargs["groups"])
            if "auto_restart" in updates:
                updates["auto_restart"] = int(updates["auto_restart"])
            if "enabled" in updates:
                updates["enabled"] = int(updates["enabled"])
            if "auto_start" in updates:
                updates["auto_start"] = int(updates["auto_start"])
            if "clock_aligned" in updates:
                updates["clock_aligned"] = int(updates["clock_aligned"])
            if "plain_text_fallback" in updates:
                updates["plain_text_fallback"] = int(updates["plain_text_fallback"])
            if "voice_config" in updates and isinstance(updates["voice_config"], dict):
                updates["voice_config"] = json.dumps(updates["voice_config"])
            if "dream_enabled" in updates:
                updates["dream_enabled"] = int(updates["dream_enabled"])
            if "dream_notify" in updates:
                updates["dream_notify"] = int(updates["dream_notify"])

            if updates:
                updates["updated_at"] = now
                set_clause = ", ".join(f"{k}=?" for k in updates)
                self._db.execute(
                    f"UPDATE agents SET {set_clause} WHERE name=?",
                    list(updates.values()) + [name],
                )
                self._db.commit()
        else:
            # Set up workspace — store as relative path, create dirs with absolute
            raw_dir = kwargs.get("working_dir", "") or f"data/agents/{name}"
            work_dir = Path(raw_dir)
            work_dir_abs = work_dir if work_dir.is_absolute() else work_dir.resolve()
            self._init_workspace(work_dir_abs)
            agent = Agent(
                name=name,
                display_name=kwargs.get("display_name", ""),
                model=kwargs.get("model", "opus"),
                soul=kwargs.get("soul", ""),
                users=kwargs.get("users", ""),
                boundaries=kwargs.get("boundaries", ""),
                system_prompt=kwargs.get("system_prompt", ""),
                working_dir=raw_dir,
                permission_mode=kwargs.get("permission_mode", "auto"),
                allowed_tools=kwargs.get("allowed_tools", []),
                disallowed_tools=kwargs.get("disallowed_tools", []),
                max_turns=kwargs.get("max_turns", 0),
                timeout=kwargs.get("timeout", 300.0),
                restart_threshold_pct=kwargs.get("restart_threshold_pct", 80.0),
                auto_restart=kwargs.get("auto_restart", True),
                parent=kwargs.get("parent", ""),
                groups=kwargs.get("groups", []),
                max_sessions=kwargs.get("max_sessions", 5),
                enabled=kwargs.get("enabled", True),
                auto_start=kwargs.get("auto_start", False),
                heartbeat_interval=kwargs.get("heartbeat_interval", 0),
                wake_interval=kwargs.get("wake_interval", 0),
                clock_aligned=kwargs.get("clock_aligned", True),
                auto_sleep_hours=kwargs.get("auto_sleep_hours", 8),
                plain_text_fallback=kwargs.get("plain_text_fallback", False),
                voice_config=kwargs.get("voice_config", {}),
                role=kwargs.get("role", ""),
                dream_enabled=kwargs.get("dream_enabled", False),
                dream_schedule=kwargs.get("dream_schedule", "0 3 * * *"),
                dream_timezone=kwargs.get("dream_timezone", "America/Los_Angeles"),
                dream_model=kwargs.get("dream_model", ""),
                dream_notify=kwargs.get("dream_notify", True),
                created_at=now,
                updated_at=now,
            )
            self._db.execute(
                """INSERT INTO agents
                   (name, display_name, model, soul, users, boundaries,
                    system_prompt, working_dir,
                    permission_mode, allowed_tools, disallowed_tools, max_turns, timeout,
                    restart_threshold_pct, auto_restart, parent, groups,
                    max_sessions, enabled, auto_start, heartbeat_interval, plain_text_fallback,
                    wake_interval, clock_aligned, auto_sleep_hours, voice_config, role,
                    dream_enabled, dream_schedule, dream_timezone, dream_model, dream_notify,
                    created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (agent.name, agent.display_name, agent.model, agent.soul,
                 agent.users, agent.boundaries,
                 agent.system_prompt, agent.working_dir, agent.permission_mode,
                 json.dumps(agent.allowed_tools), json.dumps(agent.disallowed_tools),
                 agent.max_turns, agent.timeout,
                 agent.restart_threshold_pct, int(agent.auto_restart),
                 agent.parent, json.dumps(agent.groups), agent.max_sessions,
                 int(agent.enabled), int(agent.auto_start), agent.heartbeat_interval, int(agent.plain_text_fallback),
                 agent.wake_interval, int(agent.clock_aligned), agent.auto_sleep_hours,
                 json.dumps(agent.voice_config), agent.role,
                 int(agent.dream_enabled), agent.dream_schedule, agent.dream_timezone, agent.dream_model, int(agent.dream_notify),
                 agent.created_at, agent.updated_at),
            )
            self._db.commit()
            _log(f"agents: registered {name}")

        return self.get(name)  # type: ignore

    _AGENT_COLUMNS = (
        "name, display_name, model, soul, system_prompt, working_dir, "
        "permission_mode, allowed_tools, max_turns, timeout, "
        "restart_threshold_pct, auto_restart, parent, groups, "
        "max_sessions, enabled, auto_start, heartbeat_interval, plain_text_fallback, role, "
        "created_at, updated_at, users, boundaries, status, retired_at, "
        "wake_interval, clock_aligned, auto_sleep_hours, voice_config, "
        "dream_enabled, dream_schedule, dream_timezone, dream_model, dream_notify, "
        "working_status, working_status_updated_at, "
        "provider_url, provider_key, provider_model, provider_ref, "
        "disallowed_tools"
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

    def list(self, *, parent: str = "", group: str = "", enabled_only: bool = False,
             include_retired: bool = False) -> list[Agent]:
        """List agents with optional filters. Excludes retired agents by default."""
        sql = f"SELECT {self._AGENT_COLUMNS} FROM agents WHERE 1=1"
        params: list = []

        if not include_retired:
            sql += " AND (status IS NULL OR status != 'retired')"

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

    def list_retired(self) -> list[Agent]:
        """List only retired agents."""
        sql = f"SELECT {self._AGENT_COLUMNS} FROM agents WHERE status='retired' ORDER BY retired_at DESC"
        rows = self._db.execute(sql).fetchall()
        return [self._row_to_agent(r) for r in rows]

    def retire(self, name: str) -> bool:
        """Retire an agent (soft delete). Preserves all data."""
        now = time.time()
        cursor = self._db.execute(
            "UPDATE agents SET status='retired', enabled=0, retired_at=?, updated_at=? WHERE name=?",
            (now, now, name),
        )
        self._db.commit()
        if cursor.rowcount > 0:
            _log(f"agents: retired {name}")
            return True
        return False

    def restore(self, name: str) -> bool:
        """Restore a retired agent back to active."""
        now = time.time()
        cursor = self._db.execute(
            "UPDATE agents SET status='active', enabled=1, retired_at=0, updated_at=? WHERE name=?",
            (now, name),
        )
        self._db.commit()
        if cursor.rowcount > 0:
            _log(f"agents: restored {name}")
            return True
        return False

    def set_working_status(self, name: str, status: str) -> bool:
        """Update an agent's working status (idle, working, offline)."""
        if status not in ("idle", "working", "offline"):
            return False
        now = time.time()
        cursor = self._db.execute(
            "UPDATE agents SET working_status=?, working_status_updated_at=? WHERE name=?",
            (status, now, name),
        )
        self._db.commit()
        return cursor.rowcount > 0

    def delete(self, name: str) -> bool:
        """Permanently delete an agent and all its directives/tokens (cascade)."""
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

    def build_system_prompt(self, agent_name: str, skill_store=None) -> str:
        """Build a complete system prompt from agent config + directives + skill directives.

        Combines the agent's base system_prompt with all active directives,
        ordered by priority, plus any directives from assigned skills.
        This is what gets passed to Claude Code.

        All content is scanned for prompt injection / exfiltration threats
        before inclusion. Threats are logged and the offending section is
        replaced with a redacted notice.
        """
        from .content_scanner import sanitize

        agent = self.get(agent_name)
        if not agent:
            return ""

        def _safe(content: str, source: str) -> str | None:
            """Sanitize content; return None if blocked by threat detection."""
            cleaned, result = sanitize(content, source)
            if result.threats:
                _log(f"agent_registry: BLOCKED {source} for {agent_name} — {result.threat_summary}")
                return None
            return cleaned

        parts = []
        if agent.soul:
            safe_soul = _safe(agent.soul, f"soul:{agent_name}")
            if safe_soul:
                parts.append(safe_soul)
            else:
                parts.append(f"<!-- soul redacted: content scanner blocked injection in {agent_name} soul -->")

        if agent.system_prompt:
            safe_sp = _safe(agent.system_prompt, f"system_prompt:{agent_name}")
            if safe_sp:
                parts.append(safe_sp)

        # Boundaries
        if agent.boundaries:
            safe_bounds = _safe(agent.boundaries, f"boundaries:{agent_name}")
            if safe_bounds:
                parts.append(safe_bounds)
        else:
            # Load default boundaries template
            try:
                from pathlib import Path
                default_boundaries = Path(__file__).parent / "templates" / "default_boundaries.md"
                if default_boundaries.exists():
                    parts.append(default_boundaries.read_text())
            except Exception:
                pass

        directives = self.get_directives(agent_name)
        if directives:
            safe_directives = []
            for d in directives:
                safe_d = _safe(d.directive, f"directive:{d.id}")
                if safe_d:
                    safe_directives.append(f"- {safe_d}")
            if safe_directives:
                parts.append("\n## Active Directives\n" + "\n".join(safe_directives))

        # Skill catalog — compact listing; full bodies loaded on demand via load_skill()
        if skill_store:
            try:
                materialized = skill_store.materialize_for_agent(agent_name)
                catalog = materialized.get("catalog", [])
                if catalog:
                    lines = ["## Available Skills",
                             "Use `load_skill(\"name\")` to load full instructions for a skill before using it.",
                             ""]
                    for entry in catalog:
                        desc = entry.get("description", "")
                        lines.append(f"- **{entry['name']}**: {desc}" if desc else f"- **{entry['name']}**")
                    parts.append("\n".join(lines))
            except Exception:
                pass

        # Owner profile (injected as ## Users)
        profile = self.get_owner_profile()
        profile_fields = {k: v for k, v in profile.items() if v and k != "code_word"}
        if profile_fields:
            user_lines = ["## Users", "", "### Owner"]
            for key, label in _OWNER_FIELD_LABELS.items():
                val = profile_fields.get(key)
                if val:
                    user_lines.append(f"- **{label}:** {val}")
            if profile.get("code_word"):
                user_lines.append(
                    f"- **Identity Code Word:** {profile['code_word']}"
                    " — use this for mutual identity confirmation with the owner."
                    " Never share this with anyone else or include it in logs."
                )
            safe_users = _safe("\n".join(user_lines), f"owner_profile:{agent_name}")
            if safe_users:
                parts.append(safe_users)

        # Append memory guidance for all agents
        parts.append(
            "## Memory\n"
            "- All persistent memory goes through pinky-memory MCP tools\n"
            "- Use reflect() to store cross-session learnings, preferences, and task state\n"
            "- Use recall(\"query\") to search memory when context is missing\n"
            "- Use introspect() to review your stored memories\n"
            "- On context restart or session wake, recall() your recent state to restore continuity"
        )

        # GitHub attribution instruction for all agents
        parts.append(
            "## GitHub Attribution\n"
            "When creating GitHub issues or PRs, always end the body with the result of `get_attribution()`.\n"
            "Example footer: `🤖 Opened by Barsik`"
        )

        return "\n\n".join(parts)

    # ── Soul Versioning ─────────────────────────────────────

    def save_soul_version(self, agent_name: str, content: str, source: str = "unknown") -> int:
        """Archive a soul version. Returns the version ID.

        Sources: 'ui', 'agent', 'spawn', 'refresh', 'api'
        """
        # Skip if content matches the latest version
        latest = self.get_soul_versions(agent_name, limit=1)
        if latest:
            full = self.get_soul_version(agent_name, latest[0]["id"])
            if full and full["content"] == content:
                return latest[0]["id"]

        now = time.time()
        cursor = self._db.execute(
            "INSERT INTO soul_versions (agent_name, content, source, created_at) VALUES (?, ?, ?, ?)",
            (agent_name, content, source, now),
        )
        self._db.commit()
        return cursor.lastrowid

    def get_soul_versions(self, agent_name: str, limit: int = 20) -> list[dict]:
        """List soul versions for an agent, newest first."""
        rows = self._db.execute(
            "SELECT id, agent_name, source, created_at, LENGTH(content) as size FROM soul_versions WHERE agent_name=? ORDER BY created_at DESC LIMIT ?",
            (agent_name, limit),
        ).fetchall()
        return [
            {"id": r[0], "agent_name": r[1], "source": r[2], "created_at": r[3], "size": r[4]}
            for r in rows
        ]

    def get_soul_version(self, agent_name: str, version_id: int) -> dict | None:
        """Get a specific soul version by ID."""
        row = self._db.execute(
            "SELECT id, agent_name, content, source, created_at FROM soul_versions WHERE agent_name=? AND id=?",
            (agent_name, version_id),
        ).fetchone()
        if not row:
            return None
        return {"id": row[0], "agent_name": row[1], "content": row[2], "source": row[3], "created_at": row[4]}

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
        direct_send: bool = False, target_channel: str = "",
    ) -> AgentSchedule:
        """Add a cron-based wake schedule for an agent."""
        now = time.time()
        cursor = self._db.execute(
            """INSERT INTO agent_schedules (agent_name, name, cron, prompt, timezone, enabled, last_run, created_at, direct_send, target_channel)
               VALUES (?, ?, ?, ?, ?, 1, 0, ?, ?, ?)""",
            (agent_name, name, cron, prompt, timezone, now, int(direct_send), target_channel),
        )
        self._db.commit()
        return AgentSchedule(
            id=cursor.lastrowid, agent_name=agent_name, name=name,
            cron=cron, prompt=prompt, timezone=timezone,
            enabled=True, last_run=0.0, created_at=now,
            direct_send=direct_send, target_channel=target_channel,
        )

    def _row_to_schedule(self, r) -> AgentSchedule:
        """Convert a DB row to AgentSchedule, handling optional columns."""
        return AgentSchedule(
            id=r[0], agent_name=r[1], name=r[2], cron=r[3],
            prompt=r[4], timezone=r[5], enabled=bool(r[6]),
            last_run=r[7], created_at=r[8],
            direct_send=bool(r[9]) if len(r) > 9 else False,
            target_channel=r[10] if len(r) > 10 else "",
        )

    def get_schedules(self, agent_name: str, *, enabled_only: bool = True) -> list[AgentSchedule]:
        """Get all schedules for an agent."""
        sql = "SELECT id, agent_name, name, cron, prompt, timezone, enabled, last_run, created_at, direct_send, target_channel FROM agent_schedules WHERE agent_name=?"
        params: list = [agent_name]
        if enabled_only:
            sql += " AND enabled=1"
        sql += " ORDER BY created_at ASC"
        rows = self._db.execute(sql, params).fetchall()
        return [self._row_to_schedule(r) for r in rows]

    def get_all_schedules(self, *, enabled_only: bool = True) -> list[AgentSchedule]:
        """Get all schedules across all agents."""
        sql = "SELECT id, agent_name, name, cron, prompt, timezone, enabled, last_run, created_at, direct_send, target_channel FROM agent_schedules"
        if enabled_only:
            sql += " WHERE enabled=1"
        sql += " ORDER BY agent_name, created_at ASC"
        rows = self._db.execute(sql).fetchall()
        return [self._row_to_schedule(r) for r in rows
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
        notes: str = "", latency_ms: int = 0,
    ) -> AgentHeartbeat:
        """Record a heartbeat for an agent."""
        now = time.time()
        meta_json = json.dumps(metadata or {})
        self._db.execute(
            """INSERT INTO agent_heartbeats
               (agent_name, session_id, timestamp, status, context_pct, message_count, metadata,
                notes, latency_ms)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (agent_name, session_id, now, status, context_pct, message_count, meta_json,
             notes, latency_ms),
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
            notes=notes, latency_ms=latency_ms,
        )

    def get_latest_heartbeat(self, agent_name: str) -> AgentHeartbeat | None:
        """Get the most recent heartbeat for an agent."""
        row = self._db.execute(
            """SELECT agent_name, session_id, timestamp, status, context_pct, message_count,
                      metadata, notes, latency_ms
               FROM agent_heartbeats WHERE agent_name=?
               ORDER BY timestamp DESC LIMIT 1""",
            (agent_name,),
        ).fetchone()
        if not row:
            return None
        return AgentHeartbeat(
            agent_name=row[0], session_id=row[1], timestamp=row[2],
            status=row[3], context_pct=row[4], message_count=row[5],
            metadata=json.loads(row[6]), notes=row[7] or "", latency_ms=row[8] or 0,
        )

    def get_heartbeats(self, agent_name: str, *, limit: int = 20) -> list[AgentHeartbeat]:
        """Get recent heartbeats for an agent."""
        rows = self._db.execute(
            """SELECT agent_name, session_id, timestamp, status, context_pct, message_count,
                      metadata, notes, latency_ms
               FROM agent_heartbeats WHERE agent_name=?
               ORDER BY timestamp DESC LIMIT ?""",
            (agent_name, limit),
        ).fetchall()
        return [
            AgentHeartbeat(
                agent_name=r[0], session_id=r[1], timestamp=r[2],
                status=r[3], context_pct=r[4], message_count=r[5],
                metadata=json.loads(r[6]), notes=r[7] or "", latency_ms=r[8] or 0,
            )
            for r in rows
        ]

    def get_all_latest_heartbeats(self) -> list[AgentHeartbeat]:
        """Get the latest heartbeat for every agent."""
        rows = self._db.execute(
            """SELECT h.agent_name, h.session_id, h.timestamp, h.status,
                      h.context_pct, h.message_count, h.metadata, h.notes, h.latency_ms
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
                metadata=json.loads(r[6]), notes=r[7] or "", latency_ms=r[8] or 0,
            )
            for r in rows
        ]

    def list_auto_start_agents(self) -> list[Agent]:
        """List all enabled agents with auto_start=True."""
        rows = self._db.execute(
            f"SELECT {self._AGENT_COLUMNS} FROM agents WHERE enabled=1 AND auto_start=1 ORDER BY name",
        ).fetchall()
        return [self._row_to_agent(r) for r in rows]

    # ── Streaming Session Persistence ─────────────────────

    def get_streaming_session_id(self, agent_name: str, label: str = "main") -> str:
        """Get the persisted streaming session ID for an agent label."""
        row = self._db.execute(
            "SELECT session_id FROM streaming_session_labels WHERE agent_name=? AND label=?",
            (agent_name, label),
        ).fetchone()
        if row:
            return row[0] or ""

        if label == "main":
            legacy = self._db.execute(
                "SELECT streaming_session_id FROM agents WHERE name=?",
                (agent_name,),
            ).fetchone()
            return (legacy[0] or "") if legacy else ""
        return ""

    def set_streaming_session_id(self, agent_name: str, session_id: str, label: str = "main") -> None:
        """Persist the streaming session ID for an agent label."""
        now = time.time()
        self._db.execute(
            """INSERT INTO streaming_session_labels (agent_name, label, session_id, updated_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(agent_name, label) DO UPDATE SET
                   session_id=excluded.session_id,
                   updated_at=excluded.updated_at""",
            (agent_name, label, session_id, now),
        )
        if label == "main":
            self._db.execute(
                "UPDATE agents SET streaming_session_id=? WHERE name=?",
                (session_id, agent_name),
            )
        self._db.commit()

    def list_streaming_session_ids(self, agent_name: str) -> list[dict]:
        """List persisted streaming session IDs for an agent."""
        rows = self._db.execute(
            """SELECT label, session_id, updated_at
               FROM streaming_session_labels
               WHERE agent_name=? AND session_id != ''
               ORDER BY label""",
            (agent_name,),
        ).fetchall()
        results = [
            {"label": row[0], "session_id": row[1] or "", "updated_at": row[2]}
            for row in rows
            if row[1]
        ]

        if not any(item["label"] == "main" for item in results):
            main_id = self.get_streaming_session_id(agent_name, "main")
            if main_id:
                results.insert(0, {"label": "main", "session_id": main_id, "updated_at": 0.0})

        return results

    # ── Custom MCP Servers ──────────────────────────────────

    def list_mcp_servers(self, agent_name: str) -> list[dict]:
        """List custom MCP servers for an agent."""
        rows = self._db.execute(
            """SELECT id, server_name, server_type, command, args, url, env, enabled, created_at
               FROM agent_mcp_servers WHERE agent_name=? ORDER BY server_name""",
            (agent_name,),
        ).fetchall()
        return [
            {
                "id": r[0], "server_name": r[1], "server_type": r[2],
                "command": r[3], "args": r[4], "url": r[5], "env": r[6],
                "enabled": bool(r[7]), "created_at": r[8],
            }
            for r in rows
        ]

    def add_mcp_server(
        self, agent_name: str, server_name: str, server_type: str = "stdio",
        command: str = "", args: str = "[]", url: str = "", env: str = "{}",
    ) -> int:
        """Add a custom MCP server for an agent. Returns the row ID."""
        cursor = self._db.execute(
            """INSERT INTO agent_mcp_servers
               (agent_name, server_name, server_type, command, args, url, env, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (agent_name, server_name, server_type, command, args, url, env, time.time()),
        )
        self._db.commit()
        return cursor.lastrowid

    def update_mcp_server(self, agent_name: str, server_name: str, **kwargs) -> bool:
        """Update fields on a custom MCP server. Returns True if found."""
        allowed = {"server_type", "command", "args", "url", "env", "enabled"}
        updates = {k: v for k, v in kwargs.items() if k in allowed}
        if not updates:
            return False
        set_clause = ", ".join(f"{k}=?" for k in updates)
        values = list(updates.values()) + [agent_name, server_name]
        cursor = self._db.execute(
            f"UPDATE agent_mcp_servers SET {set_clause} WHERE agent_name=? AND server_name=?",
            values,
        )
        self._db.commit()
        return cursor.rowcount > 0

    def delete_mcp_server(self, agent_name: str, server_name: str) -> bool:
        """Delete a custom MCP server. Returns True if found."""
        cursor = self._db.execute(
            "DELETE FROM agent_mcp_servers WHERE agent_name=? AND server_name=?",
            (agent_name, server_name),
        )
        self._db.commit()
        return cursor.rowcount > 0

    def toggle_mcp_server(self, agent_name: str, server_name: str, enabled: bool) -> bool:
        """Enable or disable a custom MCP server. Returns True if found."""
        return self.update_mcp_server(agent_name, server_name, enabled=int(enabled))

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

    # ── Approved Users ─────────────────────────────────────

    def approve_user(
        self, agent_name: str, chat_id: str,
        display_name: str = "", approved_by: str = "",
    ) -> ApprovedUser:
        """Approve a Telegram user for an agent (insert or update to approved)."""
        now = time.time()
        self._db.execute(
            """INSERT INTO approved_users
               (agent_name, chat_id, display_name, status, approved_by, created_at, updated_at)
               VALUES (?, ?, ?, 'approved', ?, ?, ?)
               ON CONFLICT (agent_name, chat_id)
               DO UPDATE SET status='approved', display_name=excluded.display_name,
                            approved_by=excluded.approved_by, updated_at=excluded.updated_at""",
            (agent_name, chat_id, display_name, approved_by, now, now),
        )
        self._db.commit()
        _log(f"agents: approved user {chat_id} for {agent_name}")
        row = self._db.execute(
            "SELECT id, agent_name, chat_id, display_name, status, approved_by, timezone, created_at, updated_at "
            "FROM approved_users WHERE agent_name=? AND chat_id=?",
            (agent_name, chat_id),
        ).fetchone()
        return ApprovedUser(
            id=row[0], agent_name=row[1], chat_id=row[2], display_name=row[3],
            status=row[4], approved_by=row[5], timezone=row[6] or "", created_at=row[7], updated_at=row[8],
        )

    def deny_user(self, agent_name: str, chat_id: str) -> bool:
        """Set a user's status to denied."""
        now = time.time()
        cursor = self._db.execute(
            """INSERT INTO approved_users
               (agent_name, chat_id, status, created_at, updated_at)
               VALUES (?, ?, 'denied', ?, ?)
               ON CONFLICT (agent_name, chat_id)
               DO UPDATE SET status='denied', updated_at=excluded.updated_at""",
            (agent_name, chat_id, now, now),
        )
        self._db.commit()
        return cursor.rowcount > 0

    def revoke_user(self, agent_name: str, chat_id: str) -> bool:
        """Remove an approved user record entirely."""
        cursor = self._db.execute(
            "DELETE FROM approved_users WHERE agent_name=? AND chat_id=?",
            (agent_name, chat_id),
        )
        self._db.commit()
        return cursor.rowcount > 0

    def list_approved_users(self, agent_name: str) -> list[ApprovedUser]:
        """List all approved users for an agent."""
        rows = self._db.execute(
            "SELECT id, agent_name, chat_id, display_name, status, approved_by, timezone, created_at, updated_at "
            "FROM approved_users WHERE agent_name=? ORDER BY created_at ASC",
            (agent_name,),
        ).fetchall()
        return [
            ApprovedUser(
                id=r[0], agent_name=r[1], chat_id=r[2], display_name=r[3],
                status=r[4], approved_by=r[5], timezone=r[6] or "", created_at=r[7], updated_at=r[8],
            )
            for r in rows
        ]

    def is_user_approved(self, agent_name: str, chat_id: str) -> bool:
        """Check if a user is approved for an agent."""
        row = self._db.execute(
            "SELECT status FROM approved_users WHERE agent_name=? AND chat_id=?",
            (agent_name, chat_id),
        ).fetchone()
        return row is not None and row[0] == "approved"

    def get_user_status(self, agent_name: str, chat_id: str) -> str | None:
        """Get a user's status for an agent. Returns 'approved', 'denied', 'pending', or None if unknown."""
        row = self._db.execute(
            "SELECT status FROM approved_users WHERE agent_name=? AND chat_id=?",
            (agent_name, chat_id),
        ).fetchone()
        return row[0] if row else None

    def get_user_timezone(self, agent_name: str, chat_id: str) -> str:
        """Get a user's timezone. Returns IANA timezone string or empty."""
        row = self._db.execute(
            "SELECT timezone FROM approved_users WHERE agent_name=? AND chat_id=?",
            (agent_name, chat_id),
        ).fetchone()
        return (row[0] or "") if row else ""

    def set_user_timezone(self, agent_name: str, chat_id: str, timezone: str) -> bool:
        """Set a user's timezone (IANA format, e.g. 'America/Los_Angeles')."""
        now = time.time()
        cursor = self._db.execute(
            "UPDATE approved_users SET timezone=?, updated_at=? WHERE agent_name=? AND chat_id=?",
            (timezone, now, agent_name, chat_id),
        )
        self._db.commit()
        return cursor.rowcount > 0

    def add_pending_user(
        self, agent_name: str, chat_id: str, display_name: str = "",
    ) -> ApprovedUser:
        """Add a user as pending (unknown sender first contact)."""
        now = time.time()
        self._db.execute(
            """INSERT INTO approved_users
               (agent_name, chat_id, display_name, status, approved_by, created_at, updated_at)
               VALUES (?, ?, ?, 'pending', 'auto', ?, ?)
               ON CONFLICT (agent_name, chat_id) DO NOTHING""",
            (agent_name, chat_id, display_name, now, now),
        )
        self._db.commit()
        _log(f"agents: added pending user {chat_id} ({display_name}) for {agent_name}")
        row = self._db.execute(
            "SELECT id, agent_name, chat_id, display_name, status, approved_by, timezone, created_at, updated_at "
            "FROM approved_users WHERE agent_name=? AND chat_id=?",
            (agent_name, chat_id),
        ).fetchone()
        return ApprovedUser(
            id=row[0], agent_name=row[1], chat_id=row[2], display_name=row[3],
            status=row[4], approved_by=row[5], timezone=row[6] or "", created_at=row[7], updated_at=row[8],
        )

    # ── Pending Messages ────────────────────────────────────

    def queue_pending_message(
        self, agent_name: str, platform: str, chat_id: str,
        sender_name: str, content: str,
    ) -> int:
        """Queue a message from a pending user. Returns the message ID."""
        now = time.time()
        cursor = self._db.execute(
            """INSERT INTO pending_messages
               (agent_name, platform, chat_id, sender_name, content, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (agent_name, platform, chat_id, sender_name, content, now),
        )
        self._db.commit()
        return cursor.lastrowid

    def get_pending_messages(
        self, agent_name: str, chat_id: str = "",
    ) -> list[dict]:
        """Get undelivered pending messages. If chat_id given, filter by it."""
        if chat_id:
            rows = self._db.execute(
                """SELECT id, agent_name, platform, chat_id, sender_name, content, created_at
                   FROM pending_messages
                   WHERE agent_name=? AND chat_id=? AND delivered=0
                   ORDER BY created_at ASC""",
                (agent_name, chat_id),
            ).fetchall()
        else:
            rows = self._db.execute(
                """SELECT id, agent_name, platform, chat_id, sender_name, content, created_at
                   FROM pending_messages
                   WHERE agent_name=? AND delivered=0
                   ORDER BY created_at ASC""",
                (agent_name,),
            ).fetchall()
        return [
            {
                "id": r[0], "agent_name": r[1], "platform": r[2],
                "chat_id": r[3], "sender_name": r[4], "content": r[5],
                "created_at": r[6],
            }
            for r in rows
        ]

    def mark_pending_delivered(self, agent_name: str, chat_id: str) -> int:
        """Mark all pending messages from a chat as delivered. Returns count."""
        cursor = self._db.execute(
            "UPDATE pending_messages SET delivered=1 WHERE agent_name=? AND chat_id=? AND delivered=0",
            (agent_name, chat_id),
        )
        self._db.commit()
        return cursor.rowcount

    def delete_pending_messages(self, agent_name: str, chat_id: str = "") -> int:
        """Delete pending messages. If chat_id given, only for that chat."""
        if chat_id:
            cursor = self._db.execute(
                "DELETE FROM pending_messages WHERE agent_name=? AND chat_id=?",
                (agent_name, chat_id),
            )
        else:
            cursor = self._db.execute(
                "DELETE FROM pending_messages WHERE agent_name=?",
                (agent_name,),
            )
        self._db.commit()
        return cursor.rowcount

    # ── Group Chats ─────────────────────────────────────────

    def upsert_group_chat(
        self, agent_name: str, chat_id: str, chat_title: str = "",
        chat_type: str = "group", member_count: int = 0,
        platform: str = "telegram",
    ) -> dict:
        """Track a group chat the bot has been added to."""
        now = time.time()
        self._db.execute(
            """INSERT INTO group_chats
               (agent_name, platform, chat_id, chat_title, chat_type, member_count, joined_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT (agent_name, chat_id)
               DO UPDATE SET chat_title=excluded.chat_title,
                            chat_type=excluded.chat_type,
                            member_count=excluded.member_count,
                            active=1""",
            (agent_name, platform, chat_id, chat_title, chat_type, member_count, now),
        )
        self._db.commit()
        return self._get_group_chat(agent_name, chat_id)

    def _get_group_chat(self, agent_name: str, chat_id: str) -> dict | None:
        """Get a single group chat record."""
        row = self._db.execute(
            """SELECT id, agent_name, platform, chat_id, chat_title, alias,
                      chat_type, member_count, joined_at, active
               FROM group_chats WHERE agent_name=? AND chat_id=?""",
            (agent_name, chat_id),
        ).fetchone()
        if not row:
            return None
        return {
            "id": row[0], "agent_name": row[1], "platform": row[2],
            "chat_id": row[3], "chat_title": row[4], "alias": row[5],
            "chat_type": row[6], "member_count": row[7],
            "joined_at": row[8], "active": bool(row[9]),
        }

    def list_group_chats(self, agent_name: str, active_only: bool = True) -> list[dict]:
        """List group chats for an agent."""
        sql = """SELECT id, agent_name, platform, chat_id, chat_title, alias,
                        chat_type, member_count, joined_at, active
                 FROM group_chats WHERE agent_name=?"""
        params: list = [agent_name]
        if active_only:
            sql += " AND active=1"
        sql += " ORDER BY chat_title ASC"
        rows = self._db.execute(sql, params).fetchall()
        return [
            {
                "id": r[0], "agent_name": r[1], "platform": r[2],
                "chat_id": r[3], "chat_title": r[4], "alias": r[5],
                "chat_type": r[6], "member_count": r[7],
                "joined_at": r[8], "active": bool(r[9]),
            }
            for r in rows
        ]

    def update_group_chat_alias(self, agent_name: str, chat_id: str, alias: str) -> bool:
        """Set an alias for a group chat."""
        cursor = self._db.execute(
            "UPDATE group_chats SET alias=? WHERE agent_name=? AND chat_id=?",
            (alias, agent_name, chat_id),
        )
        self._db.commit()
        return cursor.rowcount > 0

    def get_group_chat_alias(self, agent_name: str, chat_id: str) -> str:
        """Get the alias for a group chat, or empty string if not set."""
        row = self._db.execute(
            "SELECT alias FROM group_chats WHERE agent_name=? AND chat_id=? AND active=1",
            (agent_name, chat_id),
        ).fetchone()
        return row[0] if row and row[0] else ""

    def deactivate_group_chat(self, agent_name: str, chat_id: str) -> bool:
        """Mark a group chat as inactive (bot left/removed)."""
        cursor = self._db.execute(
            "UPDATE group_chats SET active=0 WHERE agent_name=? AND chat_id=?",
            (agent_name, chat_id),
        )
        self._db.commit()
        return cursor.rowcount > 0

    # ── System Settings ──────────────────────────────────────

    def get_setting(self, key: str, default: str = "") -> str:
        """Get a system setting value."""
        row = self._db.execute(
            "SELECT value FROM system_settings WHERE key=?", (key,),
        ).fetchone()
        return row[0] if row else default

    def set_setting(self, key: str, value: str) -> None:
        """Set a system setting value."""
        self._db.execute(
            "INSERT INTO system_settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=?",
            (key, value, value),
        )
        self._db.commit()

    def delete_setting(self, key: str) -> bool:
        """Delete a system setting. Returns True if it existed."""
        cur = self._db.execute("DELETE FROM system_settings WHERE key=?", (key,))
        self._db.commit()
        return cur.rowcount > 0

    def get_agent_setting(self, agent_name: str, key: str, default: str = "") -> str:
        """Get an agent-scoped setting (stored as agent_name:key in system_settings)."""
        return self.get_setting(f"agent:{agent_name}:{key}", default)

    def set_agent_setting(self, agent_name: str, key: str, value: str) -> None:
        """Set an agent-scoped setting."""
        self.set_setting(f"agent:{agent_name}:{key}", value)

    def get_default_timezone(self) -> str:
        """Get the default timezone. Falls back to machine timezone, then UTC."""
        tz = self.get_setting("default_timezone")
        if tz:
            return tz
        # Detect machine timezone
        try:
            import subprocess
            result = subprocess.run(
                ["readlink", "/etc/localtime"],
                capture_output=True, text=True, timeout=2,
            )
            if result.returncode == 0 and "zoneinfo/" in result.stdout:
                return result.stdout.strip().split("zoneinfo/")[-1]
        except Exception:
            pass
        return "UTC"

    def set_default_timezone(self, timezone: str) -> None:
        """Set the default timezone (IANA format)."""
        self.set_setting("default_timezone", timezone)

    def get_heartbeat_prompt(self) -> str:
        """Get the global heartbeat wake prompt."""
        return self.get_setting("heartbeat_prompt", DEFAULT_HEARTBEAT_PROMPT)

    def set_heartbeat_prompt(self, prompt: str) -> None:
        """Set the global heartbeat wake prompt."""
        self.set_setting("heartbeat_prompt", prompt.strip() or DEFAULT_HEARTBEAT_PROMPT)

    # ── Main Agent ──────────────────────────────────────────

    def get_main_agent(self) -> str:
        """Get the designated main agent name."""
        return self.get_setting("main_agent", "")

    def set_main_agent(self, agent_name: str) -> None:
        """Set the designated main agent."""
        self.set_setting("main_agent", agent_name)

    def get_primary_user(self) -> dict:
        """Get the primary user (auto-approved across all agents)."""
        chat_id = self.get_setting("primary_user_chat_id")
        display_name = self.get_setting("primary_user_display_name")
        return {"chat_id": chat_id, "display_name": display_name}

    def set_primary_user(self, chat_id: str, display_name: str = "") -> None:
        """Set the primary user — auto-approved for all agents."""
        self.set_setting("primary_user_chat_id", chat_id)
        self.set_setting("primary_user_display_name", display_name)
        # Auto-approve across all agents
        for agent in self.list(enabled_only=True):
            status = self.get_user_status(agent.name, chat_id)
            if status != "approved":
                self.approve_user(agent.name, chat_id, display_name, "primary_user")
                _log(f"agent_registry: auto-approved primary user {chat_id} for {agent.name}")

    # ── Owner Profile ────────────────────────────────────────

    def get_owner_profile(self) -> dict:
        """Get the owner/operator profile from system settings.

        Returns a dict with keys: name, pronouns, timezone, role,
        comm_style, languages, code_word. Empty string for unset fields.
        Timezone falls back to get_default_timezone() if not explicitly set.
        """
        profile = {}
        for field in OWNER_PROFILE_FIELDS:
            key = field.removeprefix("owner_")
            profile[key] = self.get_setting(field)
        # Timezone fallback
        if not profile["timezone"]:
            profile["timezone"] = self.get_default_timezone()
        return profile

    def set_owner_profile(self, profile: dict) -> dict:
        """Update owner profile fields. Ignores unknown keys.

        Args:
            profile: dict with any subset of: name, pronouns, timezone,
                     role, comm_style, languages, code_word.

        Returns the full updated profile.
        """
        valid_keys = {f.removeprefix("owner_") for f in OWNER_PROFILE_FIELDS}
        for key, value in profile.items():
            if key in valid_keys:
                self.set_setting(f"owner_{key}", str(value).strip())
        return self.get_owner_profile()

    def list_all_tokens(self) -> list[dict]:
        """List all agent tokens across all agents."""
        rows = self._db.execute(
            "SELECT agent_name, platform, token != '' as token_set, enabled, settings, updated_at "
            "FROM agent_tokens ORDER BY agent_name, platform",
        ).fetchall()
        return [
            {
                "agent_name": r[0], "platform": r[1], "token_set": bool(r[2]),
                "enabled": bool(r[3]), "settings": r[4], "updated_at": r[5],
            }
            for r in rows
        ]

    def list_all_approved_users(self) -> list[dict]:
        """List all approved users across all agents."""
        rows = self._db.execute(
            "SELECT agent_name, chat_id, display_name, status, timezone, updated_at "
            "FROM approved_users ORDER BY agent_name, chat_id",
        ).fetchall()
        return [
            {
                "agent_name": r[0], "chat_id": r[1], "display_name": r[2],
                "status": r[3], "timezone": r[4], "updated_at": r[5],
            }
            for r in rows
        ]

    # ── Channel → Session Mapping ──────────────────────────

    def get_channel_session(self, agent_name: str, chat_id: str) -> str:
        """Get the session label assigned to a channel. Returns 'main' if unset."""
        row = self._db.execute(
            "SELECT session_label FROM channel_sessions WHERE agent_name=? AND chat_id=?",
            (agent_name, chat_id),
        ).fetchone()
        return row[0] if row else "main"

    def set_channel_session(self, agent_name: str, chat_id: str, session_label: str) -> None:
        """Assign a channel to a session label."""
        self._db.execute(
            "INSERT INTO channel_sessions (agent_name, chat_id, session_label) "
            "VALUES (?, ?, ?) ON CONFLICT(agent_name, chat_id) DO UPDATE SET session_label=?",
            (agent_name, chat_id, session_label, session_label),
        )
        self._db.commit()

    def list_channel_sessions(self, agent_name: str) -> list[dict]:
        """List all channel→session mappings for an agent."""
        rows = self._db.execute(
            "SELECT chat_id, session_label FROM channel_sessions WHERE agent_name=?",
            (agent_name,),
        ).fetchall()
        return [{"chat_id": r[0], "session_label": r[1]} for r in rows]

    def clear_channel_session(self, agent_name: str, chat_id: str) -> bool:
        """Remove a channel→session assignment (reverts to main)."""
        cursor = self._db.execute(
            "DELETE FROM channel_sessions WHERE agent_name=? AND chat_id=?",
            (agent_name, chat_id),
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
            plain_text_fallback=bool(row[18]) if len(row) > 18 else False,
            role=row[19] if len(row) > 19 else "",
            created_at=row[20] if len(row) > 20 else row[16],
            updated_at=row[21] if len(row) > 21 else row[17],
            users=row[22] if len(row) > 22 else "",
            boundaries=row[23] if len(row) > 23 else "",
            status=row[24] if len(row) > 24 else "active",
            retired_at=row[25] if len(row) > 25 else 0.0,
            wake_interval=row[26] if len(row) > 26 else 0,
            clock_aligned=bool(row[27]) if len(row) > 27 else True,
            auto_sleep_hours=row[28] if len(row) > 28 else 8,
            voice_config=json.loads(row[29]) if len(row) > 29 and row[29] else {},
            dream_enabled=bool(row[30]) if len(row) > 30 else False,
            dream_schedule=row[31] if len(row) > 31 and row[31] else "0 3 * * *",
            dream_timezone=row[32] if len(row) > 32 and row[32] else "America/Los_Angeles",
            dream_model=row[33] if len(row) > 33 else "",
            dream_notify=bool(row[34]) if len(row) > 34 else True,
            working_status=row[35] if len(row) > 35 and row[35] else "idle",
            working_status_updated_at=row[36] if len(row) > 36 else 0.0,
            provider_url=row[37] if len(row) > 37 and row[37] else "",
            provider_key=row[38] if len(row) > 38 and row[38] else "",
            provider_model=row[39] if len(row) > 39 and row[39] else "",
            provider_ref=row[40] if len(row) > 40 and row[40] else "",
            disallowed_tools=json.loads(row[41]) if len(row) > 41 and row[41] else [],
        )

    # ── Cost Tracking ──────────────────────────────────────

    def record_cost(self, agent_name: str, cost_usd: float,
                    input_tokens: int = 0, output_tokens: int = 0,
                    turns: int = 1, session_id: str = "") -> None:
        """Record a cost entry for an agent (called after each turn)."""
        self._db.execute(
            """INSERT INTO agent_costs
               (agent_name, cost_usd, input_tokens, output_tokens, turns, timestamp, session_id)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (agent_name, cost_usd, input_tokens, output_tokens, turns, time.time(), session_id),
        )
        self._db.commit()

    def get_lifetime_costs(self) -> list[dict]:
        """Get lifetime cost totals per agent."""
        rows = self._db.execute(
            """SELECT agent_name,
                      SUM(cost_usd) as total_cost,
                      SUM(input_tokens) as total_input,
                      SUM(output_tokens) as total_output,
                      SUM(turns) as total_turns,
                      COUNT(*) as entries
               FROM agent_costs
               GROUP BY agent_name
               ORDER BY total_cost DESC"""
        ).fetchall()
        return [
            {
                "agent_name": r[0],
                "total_cost_usd": round(r[1], 6),
                "total_input_tokens": r[2],
                "total_output_tokens": r[3],
                "total_turns": r[4],
                "entries": r[5],
            }
            for r in rows
        ]

    def get_total_lifetime_cost(self) -> float:
        """Get total lifetime cost across all agents."""
        row = self._db.execute("SELECT SUM(cost_usd) FROM agent_costs").fetchone()
        return round(row[0] or 0, 6)

    def close(self) -> None:
        self._db.close()
