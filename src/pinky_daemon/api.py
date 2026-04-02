"""Pinky API — stateful HTTP server for Claude Code sessions.

FastAPI server that manages Claude Code sessions via REST endpoints.
Each session maintains its own conversation context, MCP connections,
and tool permissions.

Usage:
    python -m pinky_daemon --mode api --port 8888
    curl -X POST http://localhost:8888/sessions -d '{"model": "sonnet"}'
    curl -X POST http://localhost:8888/sessions/{id}/message -d '{"content": "Hello"}'
"""

from __future__ import annotations

import asyncio
import hmac
import json
import os
import sys
import tempfile
import time
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Literal

from fastapi import FastAPI, HTTPException, Request, Response, UploadFile
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from pinky_daemon.agent_comms import AgentComms
from pinky_daemon.agent_registry import AgentRegistry
from pinky_daemon.auth import (
    INTERNAL_AGENT_HEADER,
    INTERNAL_SIGNATURE_HEADER,
    INTERNAL_TIMESTAMP_HEADER,
    SESSION_COOKIE_NAME,
    create_session_cookie,
    hash_password,
    is_browser_json_request,
    password_source,
    verify_internal_request,
    verify_password,
    verify_session_cookie,
)
from pinky_daemon.autonomy import AgentEvent, AutonomyEngine, EventType
from pinky_daemon.broker import BrokerMessage, MessageBroker
from pinky_daemon.conversation_store import ConversationStore
from pinky_daemon.dream_runner import DreamRunner
from pinky_daemon.hooks import (
    AuditStore,
    HookEvent,
    HookManager,
    create_context_save_hook,
    create_cost_tracker_hook,
    create_heartbeat_hook,
    create_typing_indicator_hook,
)
from pinky_daemon.outreach_config import OutreachConfigStore
from pinky_daemon.plugin_manager import PluginManager
from pinky_daemon.research_export import (
    export_brief_html,
    export_brief_markdown,
    export_brief_pdf,
    get_export_content_markdown,
)
from pinky_daemon.research_store import ResearchStore
from pinky_daemon.scheduler import AgentScheduler
from pinky_daemon.session_store import SessionStore
from pinky_daemon.sessions import SessionManager, SessionState
from pinky_daemon.skill_loader import discover_all_skills, register_discovered_skills
from pinky_daemon.skill_store import SkillStore
from pinky_daemon.task_store import TaskStore

try:
    from pinky_memory.store import ReflectionStore
    from pinky_memory.types import MemoryQueryFilters
except ImportError:
    ReflectionStore = None  # type: ignore[assignment, misc]
    MemoryQueryFilters = None  # type: ignore[assignment, misc]


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# ── Request/Response Models ──────────────────────────────────


VALID_PERMISSION_MODES = ("", "default", "acceptEdits", "bypassPermissions", "dontAsk", "plan", "auto")
MIN_UI_PASSWORD_LENGTH = 8


class CreateSessionRequest(BaseModel):
    """Create a new Claude Code session."""

    model: str = ""
    soul: str = ""  # Inline soul text, or path to CLAUDE.md
    working_dir: str = "."
    allowed_tools: list[str] = Field(default_factory=lambda: [
        "mcp__memory__*",
        "mcp__pinky-memory__*",
        "mcp__pinky-self__*",
        "Read",
        "Glob",
        "Grep",
    ])
    max_turns: int = 25
    timeout: float = 300.0
    system_prompt: str = ""
    session_id: str = ""
    restart_threshold_pct: float = 80.0
    auto_restart: bool = True
    permission_mode: str = ""  # default, acceptEdits, bypassPermissions, dontAsk, plan, auto


class SendMessageRequest(BaseModel):
    """Send a message to a session."""

    content: str


class AuthSetupRequest(BaseModel):
    """Create the initial UI password."""

    password: str
    next: str = "/"


class AuthLoginRequest(BaseModel):
    """Log into the Pinky UI."""

    password: str
    next: str = "/"


class UpdatePasswordRequest(BaseModel):
    """Change the stored UI password."""

    password: str


class SessionResponse(BaseModel):
    """Session info returned by API."""

    id: str
    state: str
    model: str
    soul: str
    created_at: float
    last_active: float
    message_count: int
    mcp_servers: list[str]
    allowed_tools: list[str]
    context_used_pct: float = 0.0
    permission_mode: str = ""
    session_type: str = "chat"
    agent_name: str = ""
    usage: dict = Field(default_factory=dict)


class ContextResponse(BaseModel):
    """Context window status."""

    session_id: str
    estimated_tokens: int
    max_tokens: int
    context_used_pct: float
    message_count: int
    needs_restart: bool
    restart_threshold_pct: float
    checkpoints: int
    last_checkpoint_at: float | None


class RestartResponse(BaseModel):
    """Result of a session restart."""

    session_id: str
    checkpoint_summary: str
    messages_checkpointed: int
    tokens_at_checkpoint: int
    restart_number: int


class MessageResponse(BaseModel):
    """Response from sending a message."""

    role: str
    content: str
    timestamp: float
    duration_ms: int
    error: str


class HistoryResponse(BaseModel):
    """Conversation history."""

    session_id: str
    messages: list[dict]
    count: int


class SearchResponse(BaseModel):
    """Search results."""

    query: str
    results: list[dict]
    count: int


class ConversationListResponse(BaseModel):
    """List of conversations."""

    conversations: list[dict]
    count: int


# ── Agent Comms Models ───────────────────────────────────────


CONTENT_TYPES = Literal["text", "task_request", "task_response", "status", "file_transfer"]
PRIORITY_LEVELS = Literal[0, 1, 2]


class SendAgentMessageRequest(BaseModel):
    """Send a message to another agent/session."""

    to: str  # session_id, group name, or "*" for broadcast
    content: str
    metadata: dict = Field(default_factory=dict)
    content_type: CONTENT_TYPES = "text"
    parent_message_id: int | None = None
    priority: PRIORITY_LEVELS = 0


class CreateGroupRequest(BaseModel):
    """Create a named agent group."""

    name: str
    members: list[str]


class JoinGroupRequest(BaseModel):
    """Join a group."""

    session_id: str


# ── Skill Models ────────────────────────────────────────────


class RegisterSkillRequest(BaseModel):
    """Register a new skill/plugin package."""

    name: str
    description: str = ""
    skill_type: str = "custom"
    version: str = "0.1.0"
    enabled: bool = True
    config: dict = Field(default_factory=dict)
    mcp_server_config: dict = Field(default_factory=dict)
    tool_patterns: list[str] = Field(default_factory=list)
    directive: str = ""
    requires: list[str] = Field(default_factory=list)
    self_assignable: bool = False
    category: str = "general"
    shared: bool = False
    file_templates: dict = Field(default_factory=dict)
    default_config: dict = Field(default_factory=dict)


class UpdateSkillRequest(BaseModel):
    """Update an existing skill."""

    description: str | None = None
    skill_type: str | None = None
    version: str | None = None
    enabled: bool | None = None
    config: dict | None = None
    mcp_server_config: dict | None = None
    tool_patterns: list[str] | None = None
    directive: str | None = None
    requires: list[str] | None = None
    self_assignable: bool | None = None
    category: str | None = None
    shared: bool | None = None
    file_templates: dict | None = None
    default_config: dict | None = None


class SessionSkillRequest(BaseModel):
    """Enable/disable a skill for a session."""

    enabled: bool


class AssignSkillRequest(BaseModel):
    """Assign a skill to an agent."""

    assigned_by: str = "user"
    config_overrides: dict = Field(default_factory=dict)


class CreateSkillFromMdRequest(BaseModel):
    """Create a skill from SKILL.md content."""

    content: str
    agent_name: str = ""


class InstallSkillFromGitRequest(BaseModel):
    """Install a skill from a git repository."""

    url: str
    agent_name: str = ""


# ── Outreach Config Models ──────────────────────────────────


class ConfigurePlatformRequest(BaseModel):
    """Configure a messaging platform."""

    token: str = ""
    enabled: bool | None = None
    settings: dict = Field(default_factory=dict)


class UpdateHeartbeatPromptRequest(BaseModel):
    """Update the global heartbeat wake prompt."""

    prompt: str


class OwnerProfileRequest(BaseModel):
    """Update owner profile fields. All fields optional — only set fields are updated."""

    name: str = ""
    pronouns: str = ""
    timezone: str = ""
    role: str = ""
    comm_style: str = ""
    languages: str = ""
    code_word: str = ""


# ── Agent Models ─────────────────────────────────────────────


class RegisterAgentRequest(BaseModel):
    """Register a named agent."""

    name: str
    display_name: str = ""
    model: str = "opus"
    soul: str = ""
    users: str = ""
    boundaries: str = ""
    system_prompt: str = ""
    working_dir: str = ""  # Empty = auto-creates data/agents/{name}/
    permission_mode: str = "auto"
    allowed_tools: list[str] = Field(default_factory=list)
    max_turns: int = 25
    timeout: float = 300.0
    max_sessions: int = 5
    plain_text_fallback: bool = False
    groups: list[str] = Field(default_factory=list)


class UpdateAgentRequest(BaseModel):
    """Update an agent's config."""

    display_name: str | None = None
    model: str | None = None
    soul: str | None = None
    users: str | None = None
    boundaries: str | None = None
    system_prompt: str | None = None
    working_dir: str | None = None
    permission_mode: str | None = None
    allowed_tools: list[str] | None = None
    max_turns: int | None = None
    timeout: float | None = None
    max_sessions: int | None = None
    groups: list[str] | None = None
    enabled: bool | None = None
    plain_text_fallback: bool | None = None
    restart_threshold_pct: float | None = None
    wake_interval: int | None = None  # Seconds (0=disabled, 1800=30m, 3600=1h)
    clock_aligned: bool | None = None  # Align to wall clock boundaries
    auto_sleep_hours: int | None = None  # Auto-sleep after N hours idle (0=disabled)
    voice_config: dict | None = None  # Per-agent voice settings
    dream_enabled: bool | None = None  # Enable nightly memory consolidation
    dream_schedule: str | None = None  # Cron for dream runs (default "0 3 * * *")
    dream_timezone: str | None = None  # IANA timezone for dream schedule
    dream_model: str | None = None  # Model override for dream runs (empty = agent's model)
    dream_notify: bool | None = None  # Inject dream summary into morning wake context


class AddDirectiveRequest(BaseModel):
    """Add a directive to an agent."""

    directive: str
    priority: int = 0


class SetModelRequest(BaseModel):
    """Change model on a streaming session."""

    model: str


class AgentMessageRequest(BaseModel):
    """Send a message from one agent to another."""

    from_agent: str
    message: str
    content_type: CONTENT_TYPES = "text"
    parent_message_id: int | None = None
    priority: PRIORITY_LEVELS = 0
    metadata: dict = Field(default_factory=dict)


# Models that support 1M context windows
_1M_MODELS = {"claude-sonnet-4-6", "claude-opus-4-6"}


class SetAgentTokenRequest(BaseModel):
    """Set a bot token for an agent."""

    token: str
    enabled: bool = True
    settings: dict = Field(default_factory=dict)


class AddMcpServerRequest(BaseModel):
    """Add a custom MCP server to an agent."""

    name: str
    server_type: str = "stdio"
    command: str = ""
    args: list[str] = Field(default_factory=list)
    url: str = ""
    env: dict = Field(default_factory=dict)


class UpdateMcpServerRequest(BaseModel):
    """Update a custom MCP server."""

    server_type: str | None = None
    command: str | None = None
    args: list[str] | None = None
    url: str | None = None
    env: dict | None = None


class ApproveUserRequest(BaseModel):
    """Approve a Telegram user for an agent."""

    chat_id: str
    display_name: str = ""
    approved_by: str = ""


class SpawnSessionRequest(BaseModel):
    """Spawn a new session from an agent's config."""

    session_id: str = ""  # Auto-generated if empty
    session_type: str = "chat"  # main, worker, chat


class CreateTaskRequest(BaseModel):
    title: str
    project_id: int = 0
    description: str = ""
    status: str = "pending"
    priority: str = "normal"
    assigned_agent: str = ""
    created_by: str = ""
    tags: list[str] = Field(default_factory=list)
    due_date: str = ""
    parent_id: int = 0
    blocked_by: list[int] = Field(default_factory=list)


class UpdateTaskRequest(BaseModel):
    title: str | None = None
    project_id: int | None = None
    description: str | None = None
    status: str | None = None
    priority: str | None = None
    assigned_agent: str | None = None
    tags: list[str] | None = None
    due_date: str | None = None
    parent_id: int | None = None
    blocked_by: list[int] | None = None


class CreateProjectRequest(BaseModel):
    name: str
    description: str = ""


class AddCommentRequest(BaseModel):
    author: str = ""
    content: str


class AddScheduleRequest(BaseModel):
    name: str = ""
    cron: str
    prompt: str = ""
    timezone: str = "America/Los_Angeles"
    direct_send: bool = False
    target_channel: str = ""


class RecordHeartbeatRequest(BaseModel):
    session_id: str = ""
    status: str = "alive"
    context_pct: float = 0.0
    message_count: int = 0
    metadata: dict = Field(default_factory=dict)


class SetContextRequest(BaseModel):
    task: str = ""
    context: str = ""
    notes: str = ""
    blockers: list[str] = Field(default_factory=list)
    priority_items: list[str] = Field(default_factory=list)
    metadata: dict = Field(default_factory=dict)


class PushEventRequest(BaseModel):
    type: str = "manual_wake"
    data: dict = Field(default_factory=dict)
    priority: int = 0


# ── Research Pipeline Models ──────────────────────────────────


class CreateResearchRequest(BaseModel):
    title: str
    description: str = ""
    submitted_by: str = "admin"
    priority: str = "normal"
    tags: list[str] = Field(default_factory=list)
    scope: str = ""


class UpdateResearchRequest(BaseModel):
    title: str | None = None
    description: str | None = None
    status: str | None = None
    priority: str | None = None
    tags: list[str] | None = None
    scope: str | None = None


class AssignResearchRequest(BaseModel):
    agent_name: str


class SubmitBriefRequest(BaseModel):
    author_agent: str
    content: str
    summary: str = ""
    sources: list[str] = Field(default_factory=list)
    key_findings: list[str] = Field(default_factory=list)


class SubmitReviewRequest(BaseModel):
    brief_id: int
    reviewer_agent: str
    verdict: str = "approve"
    comments: str = ""
    confidence: int = 3
    suggested_additions: list[str] = Field(default_factory=list)
    corrections: list[str] = Field(default_factory=list)


# ── Core Skill Seeding ──────────────────────────────────────


def _seed_core_skills(skill_store) -> None:
    """Pre-register core and optional skills on startup.

    Core skills (shared=True) auto-apply to all agents.
    Optional skills can be assigned manually or by agents themselves.
    Uses register() which is idempotent — safe to call every startup.
    """
    pinky_src = str(Path(__file__).resolve().parent.parent)
    _core = [
        {
            "name": "pinky-memory",
            "description": "Long-term memory with vector search — reflect, recall, introspect",
            "skill_type": "mcp_tool",
            "category": "core",
            "shared": True,
            "self_assignable": False,
            "tool_patterns": ["mcp__pinky-memory__*", "mcp__memory__*"],
            "mcp_server_config": {
                "command": sys.executable,
                "args": ["-m", "pinky_memory", "--db", "data/memory.db"],
                "cwd": pinky_src,
            },
        },
        {
            "name": "pinky-self",
            "description": "Self-management — schedules, context, tasks, health, inter-agent comms, skills",
            "skill_type": "mcp_tool",
            "category": "core",
            "shared": True,
            "self_assignable": False,
            "tool_patterns": ["mcp__pinky-self__*"],
            "mcp_server_config": {
                "command": sys.executable,
                "args": ["-m", "pinky_self", "--agent", "{agent_name}", "--api-url", "http://localhost:8888"],
                "cwd": pinky_src,
            },
        },
        {
            "name": "pinky-messaging",
            "description": "Outbound messaging — send, thread, react, broadcast across platforms",
            "skill_type": "mcp_tool",
            "category": "core",
            "shared": True,
            "self_assignable": False,
            "tool_patterns": ["mcp__pinky-messaging__*"],
            "mcp_server_config": {
                "command": sys.executable,
                "args": ["-m", "pinky_messaging", "--agent", "{agent_name}", "--api-url", "http://localhost:8888"],
                "cwd": pinky_src,
            },
        },
        {
            "name": "file-access",
            "description": "Read files, search by name (Glob), and search content (Grep)",
            "skill_type": "builtin",
            "category": "core",
            "shared": True,
            "self_assignable": False,
            "tool_patterns": ["Read", "Glob", "Grep"],
        },
    ]

    for skill_def in _core:
        # Only seed if skill doesn't exist yet (preserve user edits)
        if not skill_store.get(skill_def["name"]):
            skill_store.register(**skill_def)


# ── MCP Config ──────────────────────────────────────────────


def _write_mcp_json(
    work_dir: Path,
    agent_name: str,
    agent_registry=None,
    skill_store=None,
) -> None:
    """Write .mcp.json with default MCP servers + skill-provided servers for an agent.

    Every agent gets:
    - pinky-memory: SQLite long-term memory with vector search
    - pinky-self: heartbeat, schedules, self-management
    - pinky-messaging: outbound messaging through the broker
    Plus any MCP servers from assigned skills.
    """
    pinky_src = str(Path(__file__).resolve().parent.parent)
    mcp_config: dict = {"mcpServers": {}}

    # Memory: per-agent SQLite long-term memory with vector search
    data_dir = work_dir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    db_path = str(data_dir / "memory.db")
    mcp_config["mcpServers"]["pinky-memory"] = {
        "command": sys.executable,
        "args": ["-m", "pinky_memory", "--db", db_path],
        "cwd": pinky_src,
    }

    # Pinky-self: heartbeat_ack, schedules, self-management
    mcp_config["mcpServers"]["pinky-self"] = {
        "command": sys.executable,
        "args": ["-m", "pinky_self", "--agent", agent_name, "--api-url", "http://localhost:8888"],
        "cwd": pinky_src,
    }

    # Pinky-messaging: outbound messaging through the broker
    mcp_config["mcpServers"]["pinky-messaging"] = {
        "command": sys.executable,
        "args": ["-m", "pinky_messaging", "--agent", agent_name, "--api-url", "http://localhost:8888"],
        "cwd": pinky_src,
    }

    # Merge MCP servers from assigned skills
    if skill_store:
        materialized = skill_store.materialize_for_agent(agent_name)
        for server_name, server_cfg in materialized.get("mcp_servers", {}).items():
            # Don't overwrite core servers
            if server_name not in mcp_config["mcpServers"]:
                mcp_config["mcpServers"][server_name] = server_cfg

    # Merge custom MCP servers from DB
    if agent_registry:
        for srv in agent_registry.list_mcp_servers(agent_name):
            if not srv["enabled"]:
                continue
            sname = srv["server_name"]
            if sname in mcp_config["mcpServers"]:
                continue  # don't overwrite core/skill servers
            if srv["server_type"] == "stdio":
                entry = {"command": srv["command"], "args": json.loads(srv["args"])}
            else:
                entry = {"url": srv["url"]}
            env = json.loads(srv["env"])
            if env:
                entry["env"] = env
            mcp_config["mcpServers"][sname] = entry

    # Merge with existing .mcp.json if present
    mcp_json = work_dir / ".mcp.json"
    if mcp_json.exists():
        try:
            existing = json.loads(mcp_json.read_text())
            # Remove legacy pinky-outreach (replaced by pinky-messaging)
            existing.setdefault("mcpServers", {}).pop("pinky-outreach", None)
            existing["mcpServers"].update(mcp_config["mcpServers"])
            mcp_config = existing
        except Exception:
            pass
    mcp_json.write_text(json.dumps(mcp_config, indent=2))


# ── API Server ───────────────────────────────────────────────


def create_api(
    *,
    max_sessions: int = 50,
    default_working_dir: str = ".",
    db_path: str = "data/conversations.db",
) -> FastAPI:
    """Create the FastAPI application."""

    app = FastAPI(
        title="Pinky",
        description="Stateful Claude Code session API",
        version="0.1.0",
    )

    session_store = SessionStore(db_path=db_path.replace(".db", "_sessions.db"))
    store = ConversationStore(db_path=db_path)
    agents = AgentRegistry(db_path=db_path.replace(".db", "_agents.db"))
    audit = AuditStore(db_path=db_path.replace(".db", "_audit.db"))
    hooks = HookManager(audit_store=audit)

    # Register built-in hooks
    hooks.register(HookEvent.post_tool_use, create_heartbeat_hook(agents))
    hooks.register(HookEvent.session_end, create_context_save_hook(agents))
    hooks.register(HookEvent.session_end, create_cost_tracker_hook())
    hooks.register(HookEvent.session_start, create_typing_indicator_hook(agents))

    manager = SessionManager(
        max_sessions=max_sessions, store=session_store,
        conversation_store=store, agent_registry=agents,
        hook_manager=hooks,
    )
    comms = AgentComms(db_path=db_path.replace(".db", "_comms.db"))

    # Message broker — routes platform messages through approval checks to agent sessions
    _platform_adapters: dict[tuple[str, str], object] = {}

    def _get_platform_adapter(agent_name: str, platform: str):
        """Get or create a platform adapter for an agent."""
        key = (agent_name, platform)
        if key in _platform_adapters:
            return _platform_adapters[key]

        token = agents.get_raw_token(agent_name, platform)
        if not token:
            return None

        adapter = None
        if platform == "telegram":
            from pinky_outreach.telegram import TelegramAdapter
            adapter = TelegramAdapter(token)
        elif platform == "discord":
            from pinky_outreach.discord import DiscordAdapter
            adapter = DiscordAdapter(token)
        elif platform == "slack":
            from pinky_outreach.slack import SlackAdapter
            adapter = SlackAdapter(token)

        if adapter:
            _platform_adapters[key] = adapter
        return adapter

    def _get_tg_adapter(agent_name: str):
        return _get_platform_adapter(agent_name, "telegram")

    import re as _re

    def _md_to_tg_mdv2(text: str) -> str:
        """Convert standard Markdown to Telegram MarkdownV2.

        Handles: bold, italic, code, code blocks, links.
        Escapes all other special characters.
        """
        # Characters that must be escaped in MarkdownV2 (outside entities)
        SPECIAL = set(r'_*[]()~`>#+-=|{}.!')

        # Extract code blocks and inline code first to protect them
        parts = []
        pos = 0

        # Code blocks: ```lang\ncode\n```
        code_block_re = _re.compile(r'```(\w*)\n(.*?)```', _re.DOTALL)
        # Inline code: `code`
        inline_code_re = _re.compile(r'`([^`]+)`')
        # Headings: # text → bold (TG has no heading support)
        heading_re = _re.compile(r'^#{1,6}\s+(.+)$', _re.MULTILINE)
        # Bold: **text**
        bold_re = _re.compile(r'\*\*(.+?)\*\*')
        # Italic: _text_ or *text* (single asterisks)
        italic_underscore_re = _re.compile(r'(?<!\w)_(.+?)_(?!\w)')
        italic_star_re = _re.compile(r'(?<!\*)\*([^*]+?)\*(?!\*)')

        def escape(s: str) -> str:
            """Escape special chars for MarkdownV2."""
            result = []
            for ch in s:
                if ch in SPECIAL:
                    result.append('\\')
                result.append(ch)
            return ''.join(result)

        # Step 1: Replace code blocks with placeholders
        placeholders = []
        def save_code_block(m):
            lang = m.group(1)
            code = m.group(2)
            idx = len(placeholders)
            placeholders.append(f"```{lang}\n{code}```")
            return f"\x00CB{idx}\x00"
        text = code_block_re.sub(save_code_block, text)

        # Step 2: Replace inline code with placeholders
        def save_inline_code(m):
            idx = len(placeholders)
            placeholders.append(f"`{m.group(1)}`")
            return f"\x00IC{idx}\x00"
        text = inline_code_re.sub(save_inline_code, text)

        # Step 3: Replace headings with bold (TG has no heading support)
        def save_heading(m):
            idx = len(placeholders)
            placeholders.append(f"*{escape(m.group(1))}*")
            return f"\x00HD{idx}\x00"
        text = heading_re.sub(save_heading, text)

        # Step 4: Replace bold **text** with placeholders
        def save_bold(m):
            idx = len(placeholders)
            placeholders.append(f"*{escape(m.group(1))}*")
            return f"\x00BD{idx}\x00"
        text = bold_re.sub(save_bold, text)

        # Step 5: Replace strikethrough ~~text~~ with ~text~ (TG format)
        strike_re = _re.compile(r'~~(.+?)~~')
        def save_strike(m):
            idx = len(placeholders)
            placeholders.append(f"~{escape(m.group(1))}~")
            return f"\x00ST{idx}\x00"
        text = strike_re.sub(save_strike, text)

        # Step 6: Replace italic _text_ with placeholders
        def save_italic(m):
            idx = len(placeholders)
            placeholders.append(f"_{escape(m.group(1))}_")
            return f"\x00IT{idx}\x00"
        text = italic_underscore_re.sub(save_italic, text)

        # Step 6b: Replace italic *text* (single asterisk) with placeholders
        def save_italic_star(m):
            idx = len(placeholders)
            placeholders.append(f"_{escape(m.group(1))}_")
            return f"\x00IS{idx}\x00"
        text = italic_star_re.sub(save_italic_star, text)

        # Step 7: Replace [text](url) links with placeholders
        link_re = _re.compile(r'\[([^\]]+)\]\(([^)]+)\)')
        def save_link(m):
            idx = len(placeholders)
            placeholders.append(f"[{escape(m.group(1))}]({m.group(2)})")
            return f"\x00LK{idx}\x00"
        text = link_re.sub(save_link, text)

        # Step 7b: Replace blockquote lines with placeholders (TG supports > natively)
        bq_re = _re.compile(r'^(>{1,})\s?(.*)$', _re.MULTILINE)
        def save_blockquote(m):
            idx = len(placeholders)
            placeholders.append(f"{m.group(1)} {escape(m.group(2))}")
            return f"\x00BQ{idx}\x00"
        text = bq_re.sub(save_blockquote, text)

        # Step 8: Escape remaining text
        text = escape(text)

        # Step 10: Restore placeholders
        placeholder_re = _re.compile(r'\x00(CB|IC|HD|BD|ST|IT|IS|LK|BQ)(\d+)\x00')
        def restore(m):
            return placeholders[int(m.group(2))]
        text = placeholder_re.sub(restore, text)

        return text

    def _session_id_for_agent(agent_name: str) -> str:
        return f"{agent_name}-main"

    def _record_outbound_message(
        agent_name: str,
        *,
        platform: str,
        chat_id: str,
        content: str,
        metadata: dict | None = None,
    ) -> None:
        store.append(
            _session_id_for_agent(agent_name),
            "assistant",
            content,
            platform=platform,
            chat_id=chat_id,
            metadata=metadata or None,
        )

    def _resolve_message_context(agent_name: str, message_id: str):
        ctx = broker.get_message_context(agent_name, message_id)
        if not ctx:
            raise HTTPException(404, f"Message context '{message_id}' not found for {agent_name}")
        return ctx

    def _extract_message_id(result) -> str:
        """Extract a message ID from a platform adapter response."""
        if hasattr(result, "message_id"):
            return str(result.message_id)
        if isinstance(result, dict):
            return str(result.get("message_id") or result.get("ts") or result.get("id") or "")
        return str(result) if result else ""

    def _send_text_message(
        agent_name: str,
        platform: str,
        chat_id: str,
        content: str,
        *,
        reply_to: str = "",
        parse_mode: str = "",
        silent: bool = False,
    ) -> SimpleNamespace:
        """Send a text message and return SimpleNamespace(message_id=...)."""
        adapter = _get_platform_adapter(agent_name, platform)
        if not adapter:
            raise HTTPException(503, f"No {platform} adapter for {agent_name}")

        if platform == "telegram":
            reply_to_id = int(reply_to) if reply_to else None
            if parse_mode:
                result = adapter.send_message(
                    chat_id,
                    content,
                    reply_to_message_id=reply_to_id,
                    parse_mode=parse_mode,
                    disable_notification=silent,
                )
                return SimpleNamespace(message_id=_extract_message_id(result))
            try:
                mdv2 = _md_to_tg_mdv2(content)
                result = adapter.send_message(
                    chat_id,
                    mdv2,
                    reply_to_message_id=reply_to_id,
                    parse_mode="MarkdownV2",
                    disable_notification=silent,
                )
                return SimpleNamespace(message_id=_extract_message_id(result))
            except Exception as e:
                _log(f"broker-send: MarkdownV2 failed ({e}), trying plain")
                result = adapter.send_message(
                    chat_id,
                    content,
                    reply_to_message_id=reply_to_id,
                    disable_notification=silent,
                )
                return SimpleNamespace(message_id=_extract_message_id(result))

        if platform == "discord":
            result = adapter.send_message(chat_id, content, reply_to=reply_to or None)
            return SimpleNamespace(message_id=_extract_message_id(result))

        if platform == "slack":
            result = adapter.send_message(chat_id, content, thread_ts=reply_to or None)
            return SimpleNamespace(message_id=_extract_message_id(result))

        raise HTTPException(400, f"Unsupported platform: {platform}")

    def _send_file_message(
        agent_name: str,
        platform: str,
        chat_id: str,
        file_path: str,
        *,
        caption: str = "",
        reply_to: str = "",
        kind: str = "document",
    ) -> SimpleNamespace:
        """Send a file/media message and return SimpleNamespace(message_id=...)."""
        adapter = _get_platform_adapter(agent_name, platform)
        if not adapter:
            raise HTTPException(503, f"No {platform} adapter for {agent_name}")

        if platform == "telegram":
            reply_to_id = int(reply_to) if reply_to else None
            if kind == "photo":
                result = adapter.send_photo(chat_id, file_path, caption=caption, reply_to_message_id=reply_to_id)
            elif kind == "animation":
                result = adapter.send_animation(chat_id, file_path, caption=caption, reply_to_message_id=reply_to_id)
            else:
                result = adapter.send_document(chat_id, file_path, caption=caption, reply_to_message_id=reply_to_id)
            return SimpleNamespace(message_id=_extract_message_id(result))

        if platform == "discord":
            result = adapter.send_file(chat_id, file_path, content=caption)
            return SimpleNamespace(message_id=_extract_message_id(result))

        if platform == "slack":
            result = adapter.upload_file(chat_id, file_path, initial_comment=caption)
            return SimpleNamespace(message_id=_extract_message_id(result))

        raise HTTPException(400, f"Unsupported platform: {platform}")

    async def _broker_send(
        agent_name: str,
        platform: str,
        chat_id: str,
        content: str,
        *,
        reply_to: str = "",
        parse_mode: str = "",
        silent: bool = False,
    ) -> dict:
        """Send a message back to the platform on behalf of an agent."""
        msg = _send_text_message(
            agent_name,
            platform,
            chat_id,
            content,
            reply_to=reply_to,
            parse_mode=parse_mode,
            silent=silent,
        )
        return {
            "sent": True,
            "agent": agent_name,
            "platform": platform,
            "chat_id": chat_id,
            "message_id": msg.message_id,
        }

    async def _broker_react(
        agent_name: str,
        platform: str,
        chat_id: str,
        message_id: str,
        emoji: str,
    ):
        """Add a reaction on behalf of an agent."""
        if platform != "telegram":
            adapter = _get_platform_adapter(agent_name, platform)
            if not adapter:
                return
            try:
                if platform == "discord":
                    adapter.add_reaction(chat_id, message_id, emoji)
                elif platform == "slack":
                    adapter.add_reaction(chat_id, message_id, emoji)
                return
            except Exception as e:
                _log(f"broker-react: failed for {agent_name} -> {chat_id}:{message_id} {emoji}: {e}")
                return

        adapter = _get_tg_adapter(agent_name)
        if not adapter:
            return

        try:
            adapter.set_reaction(chat_id, int(message_id), emoji)
        except Exception as e:
            _log(f"broker-react: failed for {agent_name} -> {chat_id}:{message_id} {emoji}: {e}")

    async def _broker_typing(agent_name: str, platform: str, chat_id: str):
        """Show typing indicator on the platform."""
        adapter = _get_platform_adapter(agent_name, platform)
        if not adapter:
            return
        try:
            if platform == "telegram":
                adapter.send_chat_action(chat_id, "typing")
            elif platform == "discord":
                adapter.send_typing(chat_id)
        except Exception:
            pass

    def _get_voice_reply_settings(agent_name: str, platform: str) -> dict | None:
        agent = agents.get(agent_name)
        if not agent:
            return None
        voice_cfg = agent.voice_config or {}
        if not voice_cfg.get("voice_reply", False):
            return None
        platform_cfg = voice_cfg.get("platforms", {}).get(platform, {})
        return {
            "provider": platform_cfg.get("tts_provider") or voice_cfg.get("tts_provider", "openai"),
            "voice": platform_cfg.get("tts_voice") or voice_cfg.get("tts_voice", ""),
            "model": platform_cfg.get("tts_model") or voice_cfg.get("tts_model", ""),
        }

    async def _broker_send_voice_message(
        agent_name: str,
        platform: str,
        chat_id: str,
        text: str,
        *,
        provider: str = "openai",
        voice: str = "",
        model: str = "",
        reply_to: str = "",
        include_text_copy: bool = False,
    ) -> dict:
        """Generate TTS audio and send it as a voice message."""
        if platform != "telegram":
            raise HTTPException(503, f"No {platform} voice adapter for {agent_name}")

        adapter = _get_tg_adapter(agent_name)
        if not adapter:
            raise HTTPException(503, f"No {platform} adapter for {agent_name}")

        def _get_key(name: str) -> str:
            return agents.get_setting(name) or os.environ.get(name, "")

        audio_path = ""
        try:
            if provider == "elevenlabs":
                api_key = _get_key("ELEVENLABS_API_KEY")
                if not api_key:
                    raise HTTPException(400, "ELEVENLABS_API_KEY not configured")
                voice_id = voice or "21m00Tcm4TlvDq8ikWAM"
                tts_model = model or "eleven_turbo_v2_5"
                tts_url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
                tts_data = json.dumps({
                    "text": text,
                    "model_id": tts_model,
                    "output_format": "mp3_44100_128",
                }).encode()
                tts_req = urllib.request.Request(
                    tts_url,
                    data=tts_data,
                    method="POST",
                    headers={
                        "Content-Type": "application/json",
                        "xi-api-key": api_key,
                    },
                )
                with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
                    audio_path = tmp.name
                with urllib.request.urlopen(tts_req, timeout=30) as resp:
                    with open(audio_path, "wb") as f:
                        f.write(resp.read())
            elif provider == "openai":
                api_key = _get_key("OPENAI_API_KEY")
                if not api_key:
                    raise HTTPException(400, "OPENAI_API_KEY not configured")
                tts_voice = voice or "alloy"
                tts_model = model or "tts-1"
                tts_url = "https://api.openai.com/v1/audio/speech"
                tts_data = json.dumps({
                    "model": tts_model,
                    "input": text,
                    "voice": tts_voice,
                    "response_format": "opus",
                }).encode()
                tts_req = urllib.request.Request(
                    tts_url,
                    data=tts_data,
                    method="POST",
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {api_key}",
                    },
                )
                with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
                    audio_path = tmp.name
                with urllib.request.urlopen(tts_req, timeout=30) as resp:
                    with open(audio_path, "wb") as f:
                        f.write(resp.read())
            elif provider == "deepgram":
                api_key = _get_key("DEEPGRAM_API_KEY")
                if not api_key:
                    raise HTTPException(400, "DEEPGRAM_API_KEY not configured")
                dg_model = model or voice or "aura-asteria-en"
                tts_url = f"https://api.deepgram.com/v1/speak?model={dg_model}"
                tts_data = json.dumps({"text": text}).encode()
                tts_req = urllib.request.Request(
                    tts_url,
                    data=tts_data,
                    method="POST",
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": f"Token {api_key}",
                    },
                )
                with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
                    audio_path = tmp.name
                with urllib.request.urlopen(tts_req, timeout=30) as resp:
                    with open(audio_path, "wb") as f:
                        f.write(resp.read())
            else:
                raise HTTPException(400, f"Unknown TTS provider: {provider}")

            msg = adapter.send_voice(
                chat_id,
                audio_path,
                caption="",
                reply_to_message_id=int(reply_to) if reply_to else None,
            )
            result = {
                "sent": True,
                "message_id": msg.message_id,
                "provider": provider,
                "platform": platform,
                "chat_id": chat_id,
            }
            if include_text_copy:
                text_msg = _send_text_message(agent_name, platform, chat_id, text, reply_to=reply_to)
                result["text_message_id"] = text_msg.message_id
            return result
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(500, f"Voice note failed: {e}")
        finally:
            if audio_path:
                try:
                    os.unlink(audio_path)
                except Exception:
                    pass

    broker = MessageBroker(
        agents,
        manager,
        send_callback=_broker_send,
        reaction_callback=_broker_react,
        typing_callback=_broker_typing,
    )
    _broker_pollers: list = []  # Track active broker pollers

    app.state.manager = manager
    app.state.broker = broker
    app.state.agents = agents
    app.state.conversation_store = store
    app.state.session_store = session_store

    def _make_cost_callback(registry):
        """Create a sync callback to persist per-turn cost data."""
        def _record_cost(agent_name, cost_usd, input_tokens, output_tokens, session_id):
            registry.record_cost(
                agent_name,
                cost_usd,
                input_tokens,
                output_tokens,
                session_id=session_id,
            )
        return _record_cost

    def _collect_agent_session_ids(agent_name: str) -> set[str]:
        """Return all known session IDs and aliases attributable to an agent."""
        session_ids: set[str] = set()

        for session in manager.list():
            if getattr(session, "agent_name", "") == agent_name or session.id.startswith(f"{agent_name}-"):
                session_ids.add(session.id)

        for entry in agents.list_streaming_session_ids(agent_name):
            session_id = entry.get("session_id", "") or ""
            label = entry.get("label", "") or ""
            if session_id:
                session_ids.add(session_id)
            if label:
                session_ids.add(f"{agent_name}-{label}")

        try:
            for conv in store.list_conversations(limit=5000):
                if conv.session_id.startswith(f"{agent_name}-"):
                    session_ids.add(conv.session_id)
        except Exception:
            pass

        return session_ids

    def _resolve_agent_history(
        agent_name: str,
        *,
        after_ts: float = 0.0,
        before_ts: float = 0.0,
        limit: int = 50,
        role: str = "",
    ) -> list[dict]:
        """Return persisted messages for an agent across all known sessions."""
        if not agents.get(agent_name):
            return []

        session_ids = _collect_agent_session_ids(agent_name)
        if not session_ids:
            return []

        messages = store.get_messages_for_sessions(
            session_ids,
            after_ts=after_ts,
            before_ts=before_ts,
            role=role,
            limit=limit,
        )
        return [m.to_dict() for m in messages]

    dream_runner = DreamRunner(
        db_path=db_path.replace(".db", "_dream_state.db"),
        history_provider=lambda agent_name, after_ts, limit, role: _resolve_agent_history(
            agent_name,
            after_ts=after_ts,
            limit=limit,
            role=role,
        ),
    )
    app.state.dream_runner = dream_runner
    app.state.agent_history_resolver = _resolve_agent_history

    def _build_streaming_wake_context(agent_name: str) -> str:
        """Build wake context for a streaming session."""
        wake_ctx = ""
        saved = agents.get_context(agent_name)
        if saved:
            ctx_prompt = saved.to_prompt()
            if ctx_prompt:
                wake_ctx = ctx_prompt

        channel_ctx = broker.build_channel_context(agent_name)
        if channel_ctx:
            wake_ctx = f"{wake_ctx}\n\n{channel_ctx}" if wake_ctx else channel_ctx

        # Inject dream summary if the agent dreamed last night and has dream_notify enabled
        agent = agents.get(agent_name)
        if agent and getattr(agent, "dream_notify", True):
            morning_summary = dream_runner.get_morning_summary(agent_name)
            if morning_summary:
                dream_ctx = f"🌙 Dream summary from last night:\n{morning_summary}"
                wake_ctx = f"{wake_ctx}\n\n{dream_ctx}" if wake_ctx else dream_ctx

        return wake_ctx

    async def _make_streaming_response_callback():
        """Create a response callback that routes through the broker."""
        async def _on_response(turn_result):
            if not turn_result.chat_id:
                return
            agent = agents.get(turn_result.agent_name)
            if not agent:
                return
            if turn_result.chat_id:
                await broker.route_response(
                    turn_result.agent_name,
                    turn_result.platform,
                    turn_result.chat_id,
                    turn_result.response_text,
                    message_id=turn_result.message_id,
                    used_outreach=turn_result.used_outreach_tools,
                    fallback_enabled=agent.plain_text_fallback,
                )
        return _on_response

    async def _make_streaming_session_id_callback(agent_name: str, label: str):
        """Persist a streaming session ID when captured from the SDK."""
        async def _on_session_id(_agent_name: str, session_id: str):
            agents.set_streaming_session_id(agent_name, session_id, label=label)
            short_id = session_id[:12] if session_id else ""
            _log(f"streaming[{agent_name}/{label}]: persisted session_id {short_id}")
        return _on_session_id

    async def _streaming_context_info(ss) -> dict:
        """Best-effort context usage details for a streaming session."""
        if not ss or not ss.is_connected or not ss._client:
            return {}

        try:
            ctx = await ss._client.get_context_usage()
            total = ctx.get("totalTokens", 0)
            reported_max = ctx.get("maxTokens", 0)
            actual_max = reported_max
            if (ss._config.model or "") in _1M_MODELS and reported_max <= 200_000:
                actual_max = 1_000_000
            pct = round(total / actual_max * 100, 1) if actual_max > 0 else 0.0
            return {
                "total_tokens": total,
                "max_tokens": actual_max,
                "percentage": pct,
            }
        except Exception:
            return {}

    async def _streaming_health_info(agent_name: str, label: str = "main") -> dict | None:
        """Return health-oriented session info for a streaming session."""
        sessions = broker._streaming.get(agent_name, {})
        ss = sessions.get(label)
        if not ss:
            return None

        ctx = await _streaming_context_info(ss)
        pct = ctx.get("percentage", 0.0)
        return {
            "id": f"{agent_name}-{label}",
            "state": "connected" if ss.is_connected else "idle",
            "context_used_pct": pct,
            "message_count": ss._stats.get("messages_sent", 0) + ss._stats.get("turns", 0),
            "needs_restart": bool(ctx) and pct >= ss._config.context_restart_pct,
            "streaming": True,
            "label": label,
            "connected": ss.is_connected,
            "sdk_session_id": ss.session_id[:12] if ss.session_id else "",
        }

    async def _start_streaming_session(
        agent_name: str,
        *,
        label: str = "main",
        resume_id: str = "",
    ):
        """Create, connect, and register a streaming session for an agent label."""
        from pinky_daemon.streaming_session import (
            DEFAULT_STREAMING_ALLOWED_TOOLS,
            StreamingSession,
            StreamingSessionConfig,
        )

        agent = agents.get(agent_name)
        if not agent or not agent.enabled:
            return None

        work_dir = str(Path(agent.working_dir).resolve()) if agent.working_dir else "."
        restart_pct = int(agent.restart_threshold_pct) if agent.restart_threshold_pct else 80
        warn_pct = max(restart_pct // 2, 20)

        # Merge allowed_tools: defaults + agent config + skill-provided patterns
        effective_tools = list(DEFAULT_STREAMING_ALLOWED_TOOLS)
        if agent.allowed_tools:
            effective_tools.extend(agent.allowed_tools)
        materialized = skills.materialize_for_agent(agent_name)
        effective_tools.extend(materialized.get("tool_patterns", []))
        # Deduplicate preserving order
        _seen: set[str] = set()
        effective_tools = [t for t in effective_tools if not (t in _seen or _seen.add(t))]  # type: ignore[func-returns-value]

        config = StreamingSessionConfig(
            agent_name=agent_name,
            model=agent.model,
            working_dir=work_dir,
            allowed_tools=effective_tools,
            permission_mode=agent.permission_mode or "bypassPermissions",
            max_turns=agent.max_turns,
            system_prompt=agents.build_system_prompt(agent_name, skill_store=skills),
            resume_session_id=resume_id,
            wake_context=_build_streaming_wake_context(agent_name),
            wake_context_builder=_build_streaming_wake_context,
            context_warn_pct=warn_pct,
            context_restart_pct=restart_pct,
        )

        callback = await _make_streaming_response_callback()
        sid_callback = await _make_streaming_session_id_callback(agent_name, label)
        ss = StreamingSession(
            config,
            response_callback=callback,
            conversation_store=store,
            cost_callback=_make_cost_callback(agents),
        )
        ss._on_session_id = sid_callback
        await ss.connect()
        broker.register_streaming(agent_name, ss, label=label)
        return ss

    async def _ensure_streaming_session(agent_name: str, *, label: str = "main"):
        """Return a connected streaming session for an agent label."""
        sessions = broker._streaming.get(agent_name, {})
        ss = sessions.get(label)
        if ss:
            if not ss.is_connected:
                await ss.connect()
            return ss

        resume_id = agents.get_streaming_session_id(agent_name, label=label)
        return await _start_streaming_session(agent_name, label=label, resume_id=resume_id)

    async def _disconnect_streaming_sessions(agent_name: str) -> int:
        """Disconnect and unregister all streaming sessions for an agent."""
        persisted = agents.list_streaming_session_ids(agent_name)
        sessions = dict(broker._streaming.get(agent_name, {}))
        closed = 0

        for label, ss in sessions.items():
            try:
                await ss.disconnect()
            except Exception:
                pass
            closed += 1
            agents.set_streaming_session_id(agent_name, "", label=label)

        broker.unregister_streaming(agent_name)

        for entry in persisted:
            if entry["label"] not in sessions:
                agents.set_streaming_session_id(agent_name, "", label=entry["label"])

        return closed

    skills = SkillStore(db_path=db_path.replace(".db", "_skills.db"))
    _seed_core_skills(skills)

    # Discover SKILL.md files from filesystem and register them
    _pinky_root = Path(__file__).resolve().parent.parent.parent
    _discovered_skills = discover_all_skills(project_root=str(_pinky_root))
    if _discovered_skills:
        register_discovered_skills(skills, _discovered_skills)

    # Initialize plugin manager and discover Python plugins
    plugins = PluginManager(
        db_path=db_path.replace(".db", "_plugins.db"),
        api_url="http://localhost:8888",
        working_dir=default_working_dir,
    )
    plugins.discover_all(project_root=str(_pinky_root))
    # Re-enable previously enabled plugins
    for pname in plugins.get_previously_enabled():
        info = plugins.get(pname)
        if info:
            plugins.enable(pname)
            plugins.register_in_skill_store(skills, pname)

    outreach_config = OutreachConfigStore(db_path=db_path.replace(".db", "_outreach.db"))
    tasks = TaskStore(db_path=db_path.replace(".db", "_tasks.db"))
    research = ResearchStore(db_path=db_path.replace(".db", "_research.db"))

    # Serve frontend (prefer built Svelte app, fall back to vanilla HTML)
    _pkg_root = Path(__file__).resolve().parent.parent.parent
    frontend_dist = _pkg_root / "frontend-dist"
    frontend_dir = frontend_dist if frontend_dist.exists() else _pkg_root / "frontend"
    if frontend_dir.exists():
        app.mount("/static", StaticFiles(directory=str(frontend_dir)), name="static")
        # Serve Svelte assets if using built frontend
        if frontend_dist.exists():
            app.mount("/assets", StaticFiles(directory=str(frontend_dist / "assets")), name="assets")

    _server_started_at = time.time()

    # Detect versions at startup
    import subprocess as _sp
    try:
        _claude_version = _sp.check_output(["claude", "--version"], stderr=_sp.DEVNULL, timeout=5).decode().strip()
    except Exception:
        _claude_version = "unknown"
    try:
        _git_hash = _sp.check_output(["git", "rev-parse", "--short", "HEAD"], stderr=_sp.DEVNULL, timeout=5).decode().strip()
    except Exception:
        _git_hash = "unknown"
    try:
        _git_branch = _sp.check_output(["git", "rev-parse", "--abbrev-ref", "HEAD"], stderr=_sp.DEVNULL, timeout=5).decode().strip()
    except Exception:
        _git_branch = "unknown"

    if not os.environ.get("PINKY_SESSION_SECRET", "").strip():
        _log("auth: WARNING PINKY_SESSION_SECRET is not set; UI login/setup cannot issue signed cookies")
    if password_source(
        os.environ.get("PINKY_UI_PASSWORD", "").strip(),
        agents.get_setting("ui_password_hash", "").strip(),
    ) == "unset":
        _log("auth: WARNING no UI password configured; first browser visit will require setup")

    _public_exact_paths = {
        "/api",
        "/login",
        "/setup",
        "/auth/login",
        "/auth/logout",
        "/auth/status",
        "/auth/setup",
        "/favicon.svg",
        "/icons.svg",
    }
    _public_prefixes = ("/assets/", "/static/")
    _protected_html_paths = {
        "/",
        "/dashboard",
        "/chat",
        "/fleet",
        "/agents-ui",
        "/settings",
        "/memories",
        "/research-ui",
        "/tasks-ui",
    }
    _protected_api_prefixes = (
        "/agents",
        "/sessions",
        "/tasks",
        "/projects",
        "/research",
        "/skills",
        "/outreach",
        "/system",
        "/settings",
        "/scheduler",
        "/activity",
        "/audit",
        "/conversations",
        "/heartbeats",
        "/groups",
        "/hooks",
        "/autonomy",
        "/broker",
        "/auth/password",
    )

    def _session_secret() -> str:
        return os.environ.get("PINKY_SESSION_SECRET", "").strip()

    def _active_password_source() -> str:
        return password_source(
            os.environ.get("PINKY_UI_PASSWORD", "").strip(),
            agents.get_setting("ui_password_hash", "").strip(),
        )

    def _password_matches(password: str) -> bool:
        env_password = os.environ.get("PINKY_UI_PASSWORD", "").strip()
        if env_password:
            return hmac.compare_digest(password, env_password)
        stored_hash = agents.get_setting("ui_password_hash", "").strip()
        return verify_password(password, stored_hash)

    def _setup_required() -> bool:
        return _active_password_source() == "unset"

    def _session_secure(request: Request) -> bool:
        proto = request.headers.get("x-forwarded-proto", request.url.scheme or "")
        return proto.lower() == "https"

    def _set_session_cookie(response: Response, request: Request) -> None:
        secret = _session_secret()
        if not secret:
            raise HTTPException(503, "PINKY_SESSION_SECRET is not configured")
        response.set_cookie(
            key=SESSION_COOKIE_NAME,
            value=create_session_cookie(secret),
            max_age=7 * 24 * 3600,
            httponly=True,
            samesite="strict",
            secure=_session_secure(request),
            path="/",
        )

    def _clear_session_cookie(response: Response, request: Request) -> None:
        response.delete_cookie(
            key=SESSION_COOKIE_NAME,
            httponly=True,
            samesite="strict",
            secure=_session_secure(request),
            path="/",
        )

    def _sanitize_next(target: str) -> str:
        cleaned = (target or "").strip()
        if not cleaned.startswith("/") or cleaned.startswith("//"):
            return "/"
        return cleaned

    def _auth_status(request: Request) -> dict:
        secret = _session_secret()
        source = _active_password_source()
        session = verify_session_cookie(secret, request.cookies.get(SESSION_COOKIE_NAME, "")) if secret else None
        return {
            "authenticated": bool(session),
            "password_source": source,
            "setup_required": source == "unset",
            "env_override": source == "env",
            "can_manage_stored_password": source != "env",
            "session_secret_configured": bool(secret),
        }

    def _is_public_path(path: str) -> bool:
        if path in _public_exact_paths:
            return True
        return path.startswith(_public_prefixes)

    def _has_valid_internal_auth(request: Request) -> bool:
        secret = _session_secret()
        if not secret:
            return False
        agent_name = request.headers.get(INTERNAL_AGENT_HEADER, "")
        timestamp = request.headers.get(INTERNAL_TIMESTAMP_HEADER, "")
        signature = request.headers.get(INTERNAL_SIGNATURE_HEADER, "")
        return verify_internal_request(
            secret,
            agent_name=agent_name,
            method=request.method,
            path=request.url.path,
            timestamp=timestamp,
            signature=signature,
        )

    def _has_valid_session(request: Request) -> bool:
        secret = _session_secret()
        if not secret:
            return False
        return bool(verify_session_cookie(secret, request.cookies.get(SESSION_COOKIE_NAME, "")))

    def _needs_browser_api_auth(request: Request) -> bool:
        path = request.url.path
        if path == "/auth/password":
            return True
        if not path.startswith(_protected_api_prefixes):
            return False
        return SESSION_COOKIE_NAME in request.cookies or is_browser_json_request(request.headers)

    @app.middleware("http")
    async def auth_middleware(request: Request, call_next):
        path = request.url.path
        if _is_public_path(path):
            return await call_next(request)

        if _has_valid_internal_auth(request):
            return await call_next(request)

        if path in _protected_html_paths:
            if _has_valid_session(request):
                return await call_next(request)
            next_target = _sanitize_next(str(request.url.path) + (f"?{request.url.query}" if request.url.query else ""))
            destination = "/setup" if _setup_required() else "/login"
            return RedirectResponse(url=f"{destination}?next={urllib.parse.quote(next_target, safe='/%#?=&')}", status_code=307)

        if _needs_browser_api_auth(request):
            if _has_valid_session(request):
                return await call_next(request)
            return JSONResponse(
                status_code=401,
                content={
                    "authenticated": False,
                    "setup_required": _setup_required(),
                    "password_source": _active_password_source(),
                    "session_secret_configured": bool(_session_secret()),
                },
            )

        return await call_next(request)

    @app.get("/api")
    async def api_info():
        """Health check and server info (JSON)."""
        return {
            "name": "pinky",
            "version": "0.1.0",
            "claude_version": _claude_version,
            "git_hash": _git_hash,
            "git_branch": _git_branch,
            "sessions": manager.count,
            "started_at": _server_started_at,
        }

    # ── SPA helper ──
    def _serve_spa_or_html(filename: str):
        """Serve built SPA index.html if available, else fall back to vanilla HTML."""
        if frontend_dist.exists():
            spa_index = frontend_dist / "index.html"
            if spa_index.exists():
                return FileResponse(str(spa_index))
        html_path = frontend_dir / filename if frontend_dir.exists() else None
        if html_path and html_path.exists():
            return FileResponse(str(html_path))
        return HTMLResponse("<h1>Frontend not found</h1>", status_code=404)

    def _auth_page(title: str, message: str, detail: str) -> HTMLResponse:
        return HTMLResponse(
            f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>{title}</title>
    <style>
      body {{ font-family: system-ui, sans-serif; background: #111318; color: #f3f5f7; margin: 0; }}
      main {{ max-width: 32rem; margin: 12vh auto; padding: 2rem; background: #1a1f28; border: 1px solid #2a3140; }}
      h1 {{ margin: 0 0 1rem 0; font-size: 1.5rem; }}
      p {{ color: #c6ced8; line-height: 1.5; }}
      code {{ background: #0c1016; padding: 0.15rem 0.35rem; border-radius: 0.25rem; }}
    </style>
  </head>
  <body>
    <main>
      <h1>{title}</h1>
      <p>{message}</p>
      <p>{detail}</p>
    </main>
  </body>
</html>""",
            status_code=503,
        )

    @app.get("/favicon.svg")
    async def favicon():
        for candidate in (frontend_dist / "favicon.svg", frontend_dir / "favicon.svg"):
            if candidate.exists():
                return FileResponse(str(candidate))
        raise HTTPException(404, "favicon not found")

    @app.get("/icons.svg")
    async def icons():
        for candidate in (frontend_dist / "icons.svg", frontend_dir / "icons.svg"):
            if candidate.exists():
                return FileResponse(str(candidate))
        raise HTTPException(404, "icons not found")

    @app.get("/auth/status")
    async def get_ui_auth_status(request: Request):
        """Get browser auth state for the UI."""
        return _auth_status(request)

    @app.post("/auth/setup")
    async def setup_ui_password(request: Request, req: AuthSetupRequest):
        """Create the initial UI password and start a session."""
        if not _session_secret():
            raise HTTPException(
                503,
                "PINKY_SESSION_SECRET is required before the UI can complete setup",
            )
        if _active_password_source() == "env":
            raise HTTPException(409, "PINKY_UI_PASSWORD is set; setup is disabled while env override is active")
        if not _setup_required():
            raise HTTPException(409, "UI password is already configured")
        password = req.password.strip()
        if not password:
            raise HTTPException(400, "password is required")
        if len(password) < MIN_UI_PASSWORD_LENGTH:
            raise HTTPException(400, f"password must be at least {MIN_UI_PASSWORD_LENGTH} characters")
        agents.set_setting("ui_password_hash", hash_password(password))
        response = JSONResponse({"configured": True, "next": _sanitize_next(req.next)})
        _set_session_cookie(response, request)
        return response

    @app.post("/auth/login")
    async def login_ui(request: Request, req: AuthLoginRequest):
        """Validate the UI password and issue a session cookie."""
        if not _session_secret():
            raise HTTPException(
                503,
                "PINKY_SESSION_SECRET is required before the UI can log in",
            )
        if _setup_required():
            raise HTTPException(409, "UI password has not been configured yet")
        if not _password_matches(req.password):
            raise HTTPException(401, "Invalid password")
        response = JSONResponse({"authenticated": True, "next": _sanitize_next(req.next)})
        _set_session_cookie(response, request)
        return response

    @app.post("/auth/logout")
    async def logout_ui(request: Request):
        """Clear the UI session cookie."""
        response = JSONResponse({"logged_out": True})
        _clear_session_cookie(response, request)
        return response

    @app.put("/auth/password")
    async def update_ui_password(request: Request, req: UpdatePasswordRequest):
        """Set or rotate the stored UI password."""
        if not _has_valid_session(request):
            raise HTTPException(401, "Authentication required")
        if _active_password_source() == "env":
            raise HTTPException(409, "Cannot change the UI password while PINKY_UI_PASSWORD is set")
        password = req.password.strip()
        if not password:
            raise HTTPException(400, "password is required")
        if len(password) < MIN_UI_PASSWORD_LENGTH:
            raise HTTPException(400, f"password must be at least {MIN_UI_PASSWORD_LENGTH} characters")
        agents.set_setting("ui_password_hash", hash_password(password))
        return {"updated": True}

    @app.get("/")
    async def root():
        """Dashboard — serves the SPA or falls back to JSON health check."""
        if frontend_dir.exists():
            return _serve_spa_or_html("dashboard.html")
        return {"name": "pinky", "version": "0.1.0", "sessions": manager.count}

    @app.get("/login", response_class=HTMLResponse)
    async def login_page():
        if not _session_secret():
            return _auth_page(
                "PINKY_SESSION_SECRET required",
                "The UI cannot create authenticated sessions without a signing secret.",
                "Set the PINKY_SESSION_SECRET environment variable and restart PinkyBot.",
            )
        if _setup_required():
            return RedirectResponse(url="/setup", status_code=307)
        return _serve_spa_or_html("index.html")

    @app.get("/setup", response_class=HTMLResponse)
    async def setup_page():
        if not _session_secret():
            return _auth_page(
                "PINKY_SESSION_SECRET required",
                "The UI cannot finish first-run setup without a signing secret.",
                "Set the PINKY_SESSION_SECRET environment variable and restart PinkyBot.",
            )
        return _serve_spa_or_html("index.html")

    @app.get("/dashboard", response_class=HTMLResponse)
    async def dashboard_ui():
        return _serve_spa_or_html("dashboard.html")

    @app.get("/chat", response_class=HTMLResponse)
    async def chat_ui():
        return _serve_spa_or_html("chat.html")

    @app.get("/fleet", response_class=HTMLResponse)
    async def fleet_ui():
        return _serve_spa_or_html("fleet.html")

    @app.get("/agents-ui", response_class=HTMLResponse)
    async def agents_ui():
        return _serve_spa_or_html("agents.html")

    @app.get("/settings", response_class=HTMLResponse)
    async def settings_ui():
        return _serve_spa_or_html("settings.html")

    @app.get("/memories", response_class=HTMLResponse)
    async def memories_ui():
        return _serve_spa_or_html("memories.html")

    @app.get("/research-ui", response_class=HTMLResponse)
    async def research_ui():
        return _serve_spa_or_html("research.html")

    @app.post("/sessions", response_model=SessionResponse)
    async def create_session(req: CreateSessionRequest):
        """Create a new Claude Code session.

        Returns session info including the session ID for subsequent calls.
        """
        # Resolve soul: if it's a file path, read it
        soul_text = req.soul
        system_prompt = req.system_prompt

        if req.soul and not req.soul.startswith("#"):
            # Looks like a file path
            soul_path = Path(req.soul)
            if soul_path.exists():
                soul_text = soul_path.read_text()
                if not system_prompt:
                    system_prompt = soul_text

        if req.permission_mode and req.permission_mode not in VALID_PERMISSION_MODES:
            raise HTTPException(400, f"Invalid permission_mode '{req.permission_mode}'. Valid: {', '.join(m for m in VALID_PERMISSION_MODES if m)}")

        working_dir = req.working_dir or default_working_dir

        session = manager.create(
            session_id=req.session_id,
            model=req.model,
            soul=soul_text,
            working_dir=working_dir,
            allowed_tools=req.allowed_tools,
            max_turns=req.max_turns,
            timeout=req.timeout,
            system_prompt=system_prompt,
            restart_threshold_pct=req.restart_threshold_pct,
            auto_restart=req.auto_restart,
            permission_mode=req.permission_mode,
        )

        _log(f"api: created session {session.id}")
        info = session.info
        return SessionResponse(**info.to_dict())

    @app.get("/sessions")
    async def list_sessions():
        """List all active manager-backed ad-hoc sessions."""
        return [SessionResponse(**s.to_dict()).model_dump() for s in manager.list()]

    @app.get("/sessions/{session_id}", response_model=SessionResponse)
    async def get_session(session_id: str):
        """Get session info."""
        session = manager.get(session_id)
        if not session:
            raise HTTPException(404, f"Session '{session_id}' not found")
        return SessionResponse(**session.info.to_dict())

    @app.delete("/sessions/{session_id}")
    async def delete_session(session_id: str):
        """Delete a session and free resources."""
        deleted = manager.delete(session_id)
        if not deleted:
            raise HTTPException(404, f"Session '{session_id}' not found")
        _log(f"api: deleted session {session_id}")
        return {"deleted": True, "session_id": session_id}

    @app.post("/sessions/{session_id}/message", response_model=MessageResponse)
    async def send_message(session_id: str, req: SendMessageRequest):
        """Send a message to a session and get the response.

        The session maintains conversation context across calls.
        Each call resumes the previous Claude Code session.
        """
        session = manager.get(session_id)
        if not session:
            raise HTTPException(404, f"Session '{session_id}' not found")

        if session.state == SessionState.closed:
            raise HTTPException(410, f"Session '{session_id}' is closed")

        if session.state == SessionState.running:
            raise HTTPException(409, f"Session '{session_id}' is busy")

        _log(f"api: message to {session_id}: {req.content[:80]}...")

        # Log user message to conversation store
        store.append(session_id, "user", req.content)

        msg = await session.send(req.content)

        # Log assistant response to conversation store
        if msg.content:
            store.append(session_id, "assistant", msg.content)

        return MessageResponse(
            role=msg.role,
            content=msg.content,
            timestamp=msg.timestamp,
            duration_ms=msg.duration_ms,
            error=msg.error,
        )

    @app.get("/sessions/{session_id}/history", response_model=HistoryResponse)
    async def get_history(session_id: str, limit: int = 50):
        """Get conversation history for a session.

        Returns in-memory history if available, otherwise falls back
        to the persistent conversation store (survives server restarts).
        """
        session = manager.get(session_id)
        if not session:
            raise HTTPException(404, f"Session '{session_id}' not found")

        history = session.get_history(limit=limit)

        # Fall back to persistent conversation store if in-memory is empty
        if not history:
            messages = store.get_history(session_id, limit=limit)
            history = [m.to_dict() for m in messages]

        return HistoryResponse(
            session_id=session_id,
            messages=history,
            count=len(history),
        )

    @app.get("/conversations/{session_id}/history")
    async def get_conversation_history(session_id: str, limit: int = 100):
        """Get conversation history from persistent store (works for any session ID)."""
        messages = store.get_history(session_id, limit=limit)
        return {
            "session_id": session_id,
            "messages": [{"role": m.role, "content": m.content, "timestamp": m.timestamp, "metadata": m.metadata} for m in messages],
            "count": len(messages),
        }

    @app.post("/conversations/{session_id}/checkpoint")
    async def log_checkpoint(session_id: str, req: dict):
        """Log a checkpoint event (context restart, compact, archive) to conversation history."""
        checkpoint_type = req.get("type", "checkpoint")
        detail = req.get("detail", "")
        store.append(
            session_id, "system", detail or f"Checkpoint: {checkpoint_type}",
            metadata={"checkpoint": checkpoint_type},
        )
        return {"logged": True, "type": checkpoint_type}

    @app.get("/sessions/{session_id}/history/search")
    async def search_history(session_id: str, q: str = "", context: int = 3):
        """Search conversation history with surrounding context messages."""
        if not q:
            raise HTTPException(400, "Query parameter 'q' required")

        # Search for matching messages
        results = store.search(q, session_id=session_id, limit=20)
        if not results:
            return {"matches": [], "query": q}

        # For each match, load surrounding messages
        all_messages = store.get_history(session_id, limit=500)
        matches = []
        for result in results:
            # Find position of match in full history
            match_idx = None
            for i, msg in enumerate(all_messages):
                if msg.id == result.id:
                    match_idx = i
                    break
            if match_idx is None:
                continue

            # Get surrounding context
            start = max(0, match_idx - context)
            end = min(len(all_messages), match_idx + context + 1)
            context_msgs = []
            for i in range(start, end):
                m = all_messages[i]
                context_msgs.append({
                    "role": m.role,
                    "content": m.content,
                    "timestamp": m.timestamp,
                    "is_match": m.id == result.id,
                })
            matches.append({"messages": context_msgs})

        return {"matches": matches, "query": q, "total": len(results)}

    @app.get("/sessions/{session_id}/context", response_model=ContextResponse)
    async def get_context(session_id: str):
        """Get context window status for a session.

        Shows estimated token usage, whether a restart is needed,
        and checkpoint history.
        """
        session = manager.get(session_id)
        if not session:
            raise HTTPException(404, f"Session '{session_id}' not found")

        status = session.get_context_status()
        return ContextResponse(**status.to_dict())

    @app.get("/sessions/{session_id}/usage")
    async def get_session_usage(session_id: str):
        """Get detailed token usage and cost breakdown for a session."""
        session = manager.get(session_id)
        if not session:
            raise HTTPException(404, f"Session '{session_id}' not found")
        return {
            "session_id": session_id,
            "agent_name": session.agent_name,
            "model": session.model,
            **session.usage.to_dict(),
        }

    # ── Conversation Store Endpoints ──────────────────────────

    @app.get("/conversations", response_model=ConversationListResponse)
    async def list_conversations(platform: str = "", limit: int = 50):
        """List all conversations grouped by session.

        Returns session IDs with message counts and timestamps.
        """
        convos = store.list_conversations(platform=platform, limit=limit)
        return ConversationListResponse(
            conversations=[c.to_dict() for c in convos],
            count=len(convos),
        )

    @app.get("/conversations/search", response_model=SearchResponse)
    async def search_conversations(
        q: str,
        session_id: str = "",
        platform: str = "",
        chat_id: str = "",
        limit: int = 50,
    ):
        """Full-text search across all conversations.

        Searches message content using FTS5. Supports boolean operators
        (AND, OR, NOT) and phrase matching ("exact phrase").
        """
        if not q:
            raise HTTPException(400, "Query parameter 'q' is required")

        results = store.search(
            q,
            session_id=session_id,
            platform=platform,
            chat_id=chat_id,
            limit=limit,
        )
        return SearchResponse(
            query=q,
            results=[r.to_dict() for r in results],
            count=len(results),
        )

    @app.get("/conversations/{session_id}", response_model=HistoryResponse)
    async def get_conversation(session_id: str, limit: int = 50, offset: int = 0):
        """Get persisted conversation history for a session.

        Unlike /sessions/{id}/history (in-memory), this reads from
        the persistent SQLite store and survives server restarts.
        """
        messages = store.get_history(session_id, limit=limit, offset=offset)
        return HistoryResponse(
            session_id=session_id,
            messages=[m.to_dict() for m in messages],
            count=len(messages),
        )

    # ── Session Management (continued) ───────────────────────

    @app.post("/sessions/{session_id}/restart", response_model=RestartResponse)
    async def restart_session(session_id: str):
        """Force a context restart with checkpoint.

        Saves a summary of the current conversation, resets the
        Claude Code session, and prepares for fresh messages.
        The session ID stays the same — callers don't notice.
        """
        session = manager.get(session_id)
        if not session:
            raise HTTPException(404, f"Session '{session_id}' not found")

        if session.state == SessionState.closed:
            raise HTTPException(410, f"Session '{session_id}' is closed")

        if session.state == SessionState.running:
            raise HTTPException(409, f"Session '{session_id}' is busy")

        _log(f"api: manual restart for {session_id}")
        checkpoint = await session.restart()

        return RestartResponse(
            session_id=session_id,
            checkpoint_summary=checkpoint.summary[:500],
            messages_checkpointed=checkpoint.message_count,
            tokens_at_checkpoint=checkpoint.estimated_tokens_at_checkpoint,
            restart_number=session._restart_count,
        )

    @app.post("/sessions/{session_id}/refresh")
    async def refresh_session(session_id: str):
        """Refresh a session — reload MCP servers while keeping conversation.

        Use this instead of delete + recreate when you need to pick up
        new MCP tools or config changes. The agent keeps its conversation
        context and resumes where it left off.
        """
        session = manager.refresh(session_id)
        if not session:
            raise HTTPException(404, f"Session '{session_id}' not found")
        _log(f"api: refreshed session {session_id}, mcp_servers={session.mcp_servers}")
        info = session.info
        return {
            "refreshed": True,
            "session": SessionResponse(**info.to_dict()).model_dump(),
        }

    # Also add refresh to the agent sessions endpoint
    @app.post("/agents/{name}/sessions/refresh")
    async def refresh_agent_sessions(name: str):
        """Refresh all sessions for an agent — reload MCP config while keeping conversations.

        Rewrites .mcp.json and CLAUDE.md, then refreshes each session's runner.
        """
        agent = agents.get(name)
        if not agent:
            raise HTTPException(404, f"Agent '{name}' not found")

        # Rewrite MCP config and CLAUDE.md
        work_dir = Path(agent.working_dir or default_working_dir).resolve()
        work_dir.mkdir(parents=True, exist_ok=True)
        claude_md = work_dir / "CLAUDE.md"
        # Snapshot on-disk content before overwriting (catches agent self-edits)
        if claude_md.exists():
            agents.save_soul_version(name, claude_md.read_text(), source="agent")
        system_prompt = agents.build_system_prompt(name, skill_store=skills)
        claude_md.write_text(system_prompt)
        agents.save_soul_version(name, system_prompt, source="refresh")
        _write_mcp_json(work_dir, name, agent_registry=agents, skill_store=skills)

        # Refresh all sessions for this agent
        all_sessions = manager.list()
        refreshed = []
        for s in all_sessions:
            if s.agent_name == name or s.id.startswith(f"{name}-"):
                session = manager.refresh(s.id)
                if session:
                    refreshed.append(s.id)

        _log(f"api: refreshed {len(refreshed)} session(s) for agent {name}")
        return {
            "agent": name,
            "refreshed_sessions": refreshed,
            "count": len(refreshed),
        }

    # ── Agent Communication Endpoints ────────────────────────

    @app.post("/sessions/{session_id}/send")
    async def send_agent_message(session_id: str, req: SendAgentMessageRequest):
        """Send a message from one session to another.

        Supports direct (to=session_id), group (to=group_name),
        and broadcast (to="*") messaging.
        """
        session = manager.get(session_id)
        if not session:
            raise HTTPException(404, f"Session '{session_id}' not found")

        extra = dict(
            metadata=req.metadata,
            content_type=req.content_type,
            parent_message_id=req.parent_message_id,
            priority=req.priority,
        )
        if req.to == "*":
            # Broadcast
            active = [s.id for s in manager.list()]
            msg = comms.broadcast(session_id, req.content, active_sessions=active, **extra)
        elif comms.get_group_members(req.to):
            # Group message
            msg = comms.send_group(session_id, req.to, req.content, **extra)
        else:
            # Direct message
            msg = comms.send(session_id, req.to, req.content, **extra)

        return msg.to_dict()

    @app.get("/sessions/{session_id}/inbox")
    async def get_inbox(session_id: str, unread_only: bool = True, limit: int = 50):
        """Get a session's inbox — messages from other agents."""
        messages = comms.get_inbox(session_id, unread_only=unread_only, limit=limit)
        return {
            "session_id": session_id,
            "messages": [m.to_dict() for m in messages],
            "count": len(messages),
            "unread": comms.unread_count(session_id),
        }

    @app.post("/sessions/{session_id}/inbox/read")
    async def mark_inbox_read(session_id: str, message_ids: list[int] | None = None):
        """Mark messages as read. Pass message_ids or omit to mark all."""
        count = comms.mark_read(session_id, message_ids)
        return {"marked": count}

    @app.get("/sessions/{session_id}/inbox/{message_id}/thread")
    async def get_message_thread(session_id: str, message_id: int):
        """Get all messages in a thread where session_id is a participant."""
        thread = comms.get_thread(message_id, session_id=session_id)
        return {
            "session_id": session_id,
            "root_message_id": thread[0].id if thread else message_id,
            "messages": [m.to_dict() for m in thread],
            "count": len(thread),
        }

    @app.post("/groups")
    async def create_group(req: CreateGroupRequest):
        """Create a named group of agents."""
        result = comms.create_group(req.name, req.members)
        return result

    @app.get("/groups")
    async def list_groups():
        """List all agent groups."""
        return {"groups": comms.list_groups()}

    @app.get("/groups/{name}")
    async def get_group(name: str):
        """Get group members."""
        members = comms.get_group_members(name)
        if not members:
            raise HTTPException(404, f"Group '{name}' not found")
        return {"name": name, "members": members}

    @app.post("/groups/{name}/join")
    async def join_group(name: str, req: JoinGroupRequest):
        """Add a session to a group."""
        comms.join_group(name, req.session_id)
        return {"joined": True, "group": name, "session_id": req.session_id}

    @app.post("/groups/{name}/leave")
    async def leave_group(name: str, req: JoinGroupRequest):
        """Remove a session from a group."""
        left = comms.leave_group(name, req.session_id)
        if not left:
            raise HTTPException(404, "Not a member")
        return {"left": True, "group": name, "session_id": req.session_id}

    # ── Skill Management Endpoints ──────────────────────────

    @app.post("/skills")
    async def register_skill(req: RegisterSkillRequest):
        """Register a new skill or update an existing one."""
        skill = skills.register(
            req.name,
            description=req.description,
            skill_type=req.skill_type,
            version=req.version,
            enabled=req.enabled,
            config=req.config,
            mcp_server_config=req.mcp_server_config,
            tool_patterns=req.tool_patterns,
            directive=req.directive,
            requires=req.requires,
            self_assignable=req.self_assignable,
            category=req.category,
            shared=req.shared,
            file_templates=req.file_templates,
            default_config=req.default_config,
        )
        return skill.to_dict()

    @app.get("/skills")
    async def list_skills(
        skill_type: str = "",
        enabled_only: bool = False,
        category: str = "",
        shared_only: bool = False,
        self_assignable_only: bool = False,
    ):
        """List all registered skills."""
        result = skills.list(
            skill_type=skill_type,
            enabled_only=enabled_only,
            category=category,
            shared_only=shared_only,
            self_assignable_only=self_assignable_only,
        )
        return {"skills": [s.to_dict() for s in result], "count": len(result)}

    # ── Skill specific-path routes (must be before /skills/{name}) ──

    @app.get("/skills/catalog")
    async def get_skill_catalog():
        """Get all skills with agent assignment counts."""
        return {"skills": skills.get_catalog_with_counts()}

    @app.get("/skills/categories")
    async def get_skill_categories():
        """Get distinct skill categories."""
        return {"categories": skills.get_categories()}

    @app.get("/skills/{name}")
    async def get_skill(name: str):
        """Get a skill by name."""
        skill = skills.get(name)
        if not skill:
            raise HTTPException(404, f"Skill '{name}' not found")
        return skill.to_dict()

    @app.put("/skills/{name}")
    async def update_skill(name: str, req: UpdateSkillRequest):
        """Update an existing skill's properties."""
        existing = skills.get(name)
        if not existing:
            raise HTTPException(404, f"Skill '{name}' not found")

        skill = skills.register(
            name,
            description=req.description if req.description is not None else existing.description,
            skill_type=req.skill_type if req.skill_type is not None else existing.skill_type,
            version=req.version if req.version is not None else existing.version,
            enabled=req.enabled if req.enabled is not None else existing.enabled,
            config=req.config if req.config is not None else existing.config,
            mcp_server_config=req.mcp_server_config if req.mcp_server_config is not None else existing.mcp_server_config,
            tool_patterns=req.tool_patterns if req.tool_patterns is not None else existing.tool_patterns,
            directive=req.directive if req.directive is not None else existing.directive,
            requires=req.requires if req.requires is not None else existing.requires,
            self_assignable=req.self_assignable if req.self_assignable is not None else existing.self_assignable,
            category=req.category if req.category is not None else existing.category,
            shared=req.shared if req.shared is not None else existing.shared,
            file_templates=req.file_templates if req.file_templates is not None else existing.file_templates,
            default_config=req.default_config if req.default_config is not None else existing.default_config,
        )
        return skill.to_dict()

    @app.delete("/skills/{name}")
    async def delete_skill(name: str):
        """Unregister a skill."""
        deleted = skills.delete(name)
        if not deleted:
            raise HTTPException(404, f"Skill '{name}' not found")
        return {"deleted": True, "name": name}

    @app.post("/skills/{name}/enable")
    async def enable_skill(name: str):
        """Enable a skill globally."""
        if not skills.enable(name):
            raise HTTPException(404, f"Skill '{name}' not found")
        return {"enabled": True, "name": name}

    @app.post("/skills/{name}/disable")
    async def disable_skill(name: str):
        """Disable a skill globally."""
        if not skills.disable(name):
            raise HTTPException(404, f"Skill '{name}' not found")
        return {"disabled": True, "name": name}

    @app.get("/sessions/{session_id}/skills")
    async def get_session_skills(session_id: str):
        """Get skills for a session with effective enabled state."""
        session = manager.get(session_id)
        if not session:
            raise HTTPException(404, f"Session '{session_id}' not found")
        result = skills.get_session_skills(session_id)
        return {"session_id": session_id, "skills": result, "count": len(result)}

    @app.put("/sessions/{session_id}/skills/{skill_name}")
    async def set_session_skill(session_id: str, skill_name: str, req: SessionSkillRequest):
        """Enable or disable a skill for a specific session."""
        session = manager.get(session_id)
        if not session:
            raise HTTPException(404, f"Session '{session_id}' not found")

        if req.enabled:
            ok = skills.enable_for_session(session_id, skill_name)
        else:
            ok = skills.disable_for_session(session_id, skill_name)

        if not ok:
            raise HTTPException(404, f"Skill '{skill_name}' not found")
        return {"session_id": session_id, "skill": skill_name, "enabled": req.enabled}

    @app.delete("/sessions/{session_id}/skills/{skill_name}")
    async def clear_session_skill_override(session_id: str, skill_name: str):
        """Remove per-session override, reverting to global default."""
        session = manager.get(session_id)
        if not session:
            raise HTTPException(404, f"Session '{session_id}' not found")
        skills.clear_session_override(session_id, skill_name)
        return {"session_id": session_id, "skill": skill_name, "override_cleared": True}

    # ── Skill Discovery & Plugin Endpoints ───────────────

    @app.post("/skills/from-md")
    async def create_skill_from_md(req: CreateSkillFromMdRequest):
        """Create a skill by parsing SKILL.md content inline.

        Parses the frontmatter + body, registers as a skill, and optionally
        assigns it to an agent.
        """
        import tempfile

        from pinky_daemon.skill_loader import parse_skill_md

        if not req.content.strip():
            raise HTTPException(400, "Empty SKILL.md content")

        # Write to a temp file so the parser can work with it
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", prefix="skill_", delete=False) as f:
            f.write(req.content)
            tmp_path = f.name

        try:
            parsed = parse_skill_md(tmp_path)
        finally:
            os.unlink(tmp_path)

        if not parsed:
            raise HTTPException(400, "Failed to parse SKILL.md — check frontmatter (name and description required)")

        # Also write to skills/ directory for persistence
        skill_dir = _pinky_root / "skills" / parsed.name
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text(req.content)

        # Register in SkillStore
        config = {
            "location": str(skill_dir / "SKILL.md"),
            "base_dir": str(skill_dir),
            "source": "ui",
        }
        if parsed.metadata:
            config["metadata"] = parsed.metadata

        skill = skills.register(
            parsed.name,
            description=parsed.description,
            skill_type="skill",
            version=parsed.metadata.get("version", "1.0.0") if parsed.metadata else "1.0.0",
            enabled=True,
            config=config,
            tool_patterns=parsed.allowed_tools,
            directive=parsed.body,
            self_assignable=True,
            category="skill",
            shared=False,
        )

        result = skill.to_dict()

        # Auto-assign to agent if specified
        if req.agent_name:
            agent = agents.get(req.agent_name)
            if agent:
                skills.assign_to_agent(req.agent_name, parsed.name, assigned_by="user")
                result["assigned_to"] = req.agent_name

        return result

    @app.post("/skills/from-git")
    async def install_skill_from_git(req: InstallSkillFromGitRequest):
        """Clone a git repo into skills/ and register any SKILL.md files found.

        Supports:
        - Full repo: https://github.com/org/skill-name
        - Repo with .git suffix: https://github.com/org/skill-name.git
        - Subdirectory hint: https://github.com/org/skills-collection/tree/main/my-skill
        """
        import re as _re
        import subprocess as sp

        url = req.url.strip()
        if not url:
            raise HTTPException(400, "URL is required")

        # Parse GitHub tree/blob URLs:
        #   github.com/org/repo/tree/branch/path
        #   github.com/org/repo/blob/branch/path/to/SKILL.md
        subdir = ""
        gh_match = _re.match(
            r"https?://github\.com/([^/]+/[^/]+)/(?:tree|blob)/[^/]+/(.*)", url,
        )
        if gh_match:
            repo_slug = gh_match.group(1)
            path = gh_match.group(2).rstrip("/")
            # If pointing at a file, use its parent directory
            if path.endswith(".md") or "." in path.rsplit("/", 1)[-1]:
                path = path.rsplit("/", 1)[0] if "/" in path else ""
            subdir = path
            url = f"https://github.com/{repo_slug}.git"
        elif _re.match(r"https?://github\.com/[^/]+/[^/]+$", url):
            # Plain repo URL: github.com/org/repo (no tree/blob)
            url = url.rstrip("/") + ".git" if not url.endswith(".git") else url

        # Derive a directory name from the URL
        repo_name = url.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")
        target_dir = _pinky_root / "skills" / repo_name

        try:
            if target_dir.exists():
                # Pull latest
                sp.run(
                    ["git", "-C", str(target_dir), "pull", "--ff-only"],
                    capture_output=True, timeout=60, check=True,
                )
                _log(f"api: updated skill repo {repo_name}")
            else:
                # Clone
                sp.run(
                    ["git", "clone", "--depth", "1", url, str(target_dir)],
                    capture_output=True, timeout=120, check=True,
                )
                _log(f"api: cloned skill repo {repo_name} to {target_dir}")
        except sp.CalledProcessError as e:
            stderr = e.stderr.decode() if e.stderr else str(e)
            raise HTTPException(400, f"Git clone failed: {stderr.strip()}")
        except sp.TimeoutExpired:
            raise HTTPException(504, "Git clone timed out")

        # Scan the cloned directory (or subdirectory) for SKILL.md files
        scan_root = target_dir / subdir if subdir else target_dir
        if not scan_root.is_dir():
            raise HTTPException(400, f"Subdirectory '{subdir}' not found in cloned repo")

        from pinky_daemon.skill_loader import register_discovered_skills as _register
        from pinky_daemon.skill_loader import scan_skills_directory

        # Check if scan_root itself has a SKILL.md (repo IS a skill)
        found = scan_skills_directory(scan_root)

        # If nothing found in subdirs, check root-level SKILL.md
        if not found and (scan_root / "SKILL.md").is_file():
            from pinky_daemon.skill_loader import parse_skill_md
            parsed = parse_skill_md(scan_root / "SKILL.md")
            if parsed:
                found = [parsed]

        if not found:
            raise HTTPException(400, f"No SKILL.md files found in {repo_name}" + (f"/{subdir}" if subdir else ""))

        result = _register(skills, found, overwrite=True)

        # Auto-assign to agent if specified
        assigned = []
        if req.agent_name:
            agent = agents.get(req.agent_name)
            if agent:
                for name in result["registered"] + result["updated"]:
                    skills.assign_to_agent(req.agent_name, name, assigned_by="user")
                    assigned.append(name)

        return {
            "repo": repo_name,
            "skills_found": len(found),
            "registered": result["registered"],
            "updated": result["updated"],
            "skipped": result["skipped"],
            "assigned_to": req.agent_name if assigned else "",
            "assigned_skills": assigned,
        }

    @app.post("/skills/discover")
    async def discover_skills_endpoint():
        """Re-scan filesystem for SKILL.md files and register new skills."""
        found = discover_all_skills(project_root=str(_pinky_root))
        result = register_discovered_skills(skills, found, overwrite=False)
        return {
            "discovered": len(found),
            **result,
        }

    @app.get("/plugins")
    async def list_plugins_endpoint():
        """List all discovered plugins with their state."""
        plugin_list = plugins.list_plugins()
        return {"plugins": plugin_list, "count": len(plugin_list)}

    @app.post("/plugins/discover")
    async def discover_plugins_endpoint():
        """Re-scan filesystem for Python plugins."""
        found = plugins.discover_all(project_root=str(_pinky_root))
        return {"discovered": [m.name for m in found], "count": len(found)}

    @app.post("/plugins/{name}/enable")
    async def enable_plugin(name: str):
        """Enable a discovered plugin."""
        info = plugins.get(name)
        if not info:
            raise HTTPException(404, f"Plugin '{name}' not found")
        ok = plugins.enable(name)
        if not ok:
            raise HTTPException(500, f"Failed to enable plugin: {info.error}")
        plugins.register_in_skill_store(skills, name)
        return {"enabled": True, "name": name}

    @app.post("/plugins/{name}/disable")
    async def disable_plugin(name: str):
        """Disable an active plugin."""
        info = plugins.get(name)
        if not info:
            raise HTTPException(404, f"Plugin '{name}' not found")
        plugins.disable(name)
        return {"disabled": True, "name": name}

    @app.get("/plugins/{name}")
    async def get_plugin(name: str):
        """Get plugin details."""
        info = plugins.get(name)
        if not info:
            raise HTTPException(404, f"Plugin '{name}' not found")
        m = info.manifest
        return {
            "name": m.name,
            "description": m.description,
            "version": m.version,
            "author": m.author,
            "state": info.state.value,
            "error": info.error,
            "permissions": m.permissions,
            "tools": m.tools,
            "hooks": m.hooks,
            "directory": m.directory,
        }

    # ── Agent Skill Endpoints ──────────────────────────────

    @app.get("/agents/{name}/skills")
    async def get_agent_skills(name: str, enabled_only: bool = True):
        """List skills for an agent (direct assignments + shared globals)."""
        agent = agents.get(name)
        if not agent:
            raise HTTPException(404, f"Agent '{name}' not found")
        result = skills.get_agent_skills(name, enabled_only=enabled_only)
        return {"agent": name, "skills": result, "count": len(result)}

    @app.get("/agents/{name}/skills/available")
    async def get_available_agent_skills(
        name: str,
        self_assignable: bool = False,
        category: str = "",
    ):
        """List skills from the catalog not yet assigned to this agent."""
        agent = agents.get(name)
        if not agent:
            raise HTTPException(404, f"Agent '{name}' not found")
        result = skills.get_available_skills(
            name, self_assignable_only=self_assignable, category=category,
        )
        return {"agent": name, "skills": [s.to_dict() for s in result], "count": len(result)}

    @app.post("/agents/{name}/skills/{skill_name}")
    async def assign_agent_skill(name: str, skill_name: str, req: AssignSkillRequest):
        """Assign a skill to an agent."""
        agent = agents.get(name)
        if not agent:
            raise HTTPException(404, f"Agent '{name}' not found")
        skill = skills.get(skill_name)
        if not skill:
            raise HTTPException(404, f"Skill '{skill_name}' not found")

        # Check self-assignable constraint
        if req.assigned_by == "self" and not skill.self_assignable:
            raise HTTPException(403, f"Skill '{skill_name}' is not self-assignable")

        # Check dependencies
        missing = skills.check_dependencies(skill_name, name)
        if missing:
            raise HTTPException(
                400,
                f"Missing prerequisite skills: {', '.join(missing)}. "
                f"Assign them first or choose a skill without dependencies.",
            )

        ok = skills.assign_to_agent(
            name, skill_name,
            assigned_by=req.assigned_by,
            config_overrides=req.config_overrides,
        )
        if not ok:
            raise HTTPException(500, "Failed to assign skill")
        return {"assigned": True, "agent": name, "skill": skill_name}

    @app.delete("/agents/{name}/skills/{skill_name}")
    async def remove_agent_skill(name: str, skill_name: str):
        """Remove a skill from an agent."""
        agent = agents.get(name)
        if not agent:
            raise HTTPException(404, f"Agent '{name}' not found")
        # Prevent removing core skills
        skill = skills.get(skill_name)
        if skill and skill.category == "core":
            raise HTTPException(400, f"Cannot remove core skill '{skill_name}'")
        removed = skills.remove_from_agent(name, skill_name)
        if not removed:
            raise HTTPException(404, f"Skill '{skill_name}' not assigned to '{name}'")
        return {"removed": True, "agent": name, "skill": skill_name}

    @app.post("/agents/{name}/skills/{skill_name}/enable")
    async def enable_agent_skill(name: str, skill_name: str):
        """Enable an assigned skill for an agent."""
        if not skills.set_agent_skill_enabled(name, skill_name, True):
            raise HTTPException(404, f"Skill '{skill_name}' not assigned to '{name}'")
        return {"enabled": True, "agent": name, "skill": skill_name}

    @app.post("/agents/{name}/skills/{skill_name}/disable")
    async def disable_agent_skill(name: str, skill_name: str):
        """Disable an assigned skill for an agent (opt-out)."""
        if not skills.set_agent_skill_enabled(name, skill_name, False):
            # For shared skills, create a disabled assignment to opt out
            skill = skills.get(skill_name)
            if skill and skill.shared:
                skills.assign_to_agent(name, skill_name, assigned_by="user")
                skills.set_agent_skill_enabled(name, skill_name, False)
                return {"disabled": True, "agent": name, "skill": skill_name, "opted_out": True}
            raise HTTPException(404, f"Skill '{skill_name}' not assigned to '{name}'")
        return {"disabled": True, "agent": name, "skill": skill_name}

    @app.post("/agents/{name}/skills/apply")
    async def apply_agent_skills(name: str):
        """Re-materialize skills and restart the agent's streaming session.

        This writes the updated .mcp.json, saves agent context,
        disconnects the current session, and starts a fresh one
        with the new skill configuration.
        """
        agent = agents.get(name)
        if not agent:
            raise HTTPException(404, f"Agent '{name}' not found")

        work_dir = Path(agent.working_dir or default_working_dir).resolve()

        # 1. Materialize skills
        materialized = skills.materialize_for_agent(name)

        # 2. Write file templates (don't overwrite existing files)
        for rel_path, content in materialized.get("file_templates", {}).items():
            file_path = work_dir / rel_path
            if not file_path.exists():
                file_path.parent.mkdir(parents=True, exist_ok=True)
                file_path.write_text(content)
                _log(f"api: wrote skill template {rel_path} to {work_dir}")

        # 3. Rewrite .mcp.json with skill servers
        _write_mcp_json(work_dir, name, agent_registry=agents, skill_store=skills)

        # 4. Rewrite CLAUDE.md with skill directives
        system_prompt = agents.build_system_prompt(name, skill_store=skills)
        claude_md = work_dir / "CLAUDE.md"
        claude_md.write_text(system_prompt)

        # 5. Restart streaming session if one exists
        restarted = False
        streaming = broker.get_streaming(name)
        if streaming:
            # Save context before restart
            try:
                import urllib.request
                save_req = urllib.request.Request(
                    f"http://localhost:8888/agents/{name}/context",
                    data=json.dumps({"task": "skill_change", "notes": "Auto-saved before skill apply"}).encode(),
                    method="PUT",
                    headers={"Content-Type": "application/json"},
                )
                urllib.request.urlopen(save_req, timeout=5)
            except Exception:
                pass

            # Close and restart
            await _close_streaming_sessions(name)
            await _start_streaming_session(name)
            restarted = True

        return {
            "applied": True,
            "agent": name,
            "mcp_servers": list(materialized.get("mcp_servers", {}).keys()),
            "tool_patterns": materialized.get("tool_patterns", []),
            "directives_count": len(materialized.get("directives", [])),
            "session_restarted": restarted,
        }

    # ── System Settings ────────────────────────────────────

    @app.get("/system/timezone")
    async def get_default_timezone():
        """Get the default timezone."""
        return {"timezone": agents.get_default_timezone()}

    @app.put("/system/timezone")
    async def set_default_timezone(timezone: str):
        """Set the default timezone (IANA format, e.g. 'America/Los_Angeles')."""
        try:
            from zoneinfo import ZoneInfo
            ZoneInfo(timezone)
        except Exception:
            raise HTTPException(400, f"Invalid timezone: {timezone}")
        agents.set_default_timezone(timezone)
        return {"updated": True, "timezone": timezone}

    @app.get("/system/primary-user")
    async def get_primary_user():
        """Get the primary user (auto-approved across all agents)."""
        return agents.get_primary_user()

    @app.put("/system/primary-user")
    async def set_primary_user(chat_id: str, display_name: str = ""):
        """Set the primary user — auto-approved for all agents."""
        if not chat_id.strip():
            raise HTTPException(400, "chat_id is required")
        agents.set_primary_user(chat_id.strip(), display_name.strip())
        return {"updated": True, **agents.get_primary_user()}

    @app.get("/system/api-keys")
    async def get_api_keys():
        """Get configured API keys (masked for display)."""
        keys = {}
        for key_name in ("ELEVENLABS_API_KEY", "OPENAI_API_KEY", "DEEPGRAM_API_KEY", "GIPHY_API_KEY"):
            val = agents.get_setting(key_name) or os.environ.get(key_name, "")
            keys[key_name] = {
                "configured": bool(val),
                "source": "settings" if agents.get_setting(key_name) else ("env" if os.environ.get(key_name) else "none"),
                "preview": val[:8] + "..." if len(val) > 8 else ("***" if val else ""),
            }
        return {"keys": keys}

    @app.put("/system/api-keys/{key_name}")
    async def set_api_key(key_name: str, req: dict):
        """Set an API key in system settings."""
        allowed = {"ELEVENLABS_API_KEY", "OPENAI_API_KEY", "DEEPGRAM_API_KEY", "GIPHY_API_KEY"}
        if key_name not in allowed:
            raise HTTPException(400, f"Unknown key: {key_name}. Allowed: {', '.join(sorted(allowed))}")
        value = req.get("value", "").strip()
        if not value:
            raise HTTPException(400, "value is required")
        agents.set_setting(key_name, value)
        return {"saved": True, "key": key_name}

    @app.delete("/system/api-keys/{key_name}")
    async def delete_api_key(key_name: str):
        """Remove an API key from system settings."""
        agents.set_setting(key_name, "")
        return {"deleted": True, "key": key_name}

    @app.get("/system/all-tokens")
    async def list_all_tokens():
        """List all agent bot tokens across all agents."""
        return {"tokens": agents.list_all_tokens()}

    @app.get("/system/all-approved-users")
    async def list_all_approved_users():
        """List all approved users across all agents."""
        return {"users": agents.list_all_approved_users()}

    # ── Outreach Configuration Endpoints ────────────────────

    @app.get("/outreach/platforms")
    async def list_platforms():
        """List all configured outreach platforms."""
        result = outreach_config.list()
        return {"platforms": [p.to_dict() for p in result], "count": len(result)}

    @app.get("/outreach/platforms/{platform}")
    async def get_platform(platform: str):
        """Get platform configuration (token is never exposed)."""
        config = outreach_config.get(platform)
        if not config:
            raise HTTPException(404, f"Platform '{platform}' not configured")
        return config.to_dict()

    @app.put("/outreach/platforms/{platform}")
    async def configure_platform(platform: str, req: ConfigurePlatformRequest):
        """Configure or update a messaging platform.

        Set token, enabled state, and platform-specific settings.
        Token is stored securely and never returned in API responses.
        """
        try:
            config = outreach_config.configure(
                platform,
                token=req.token,
                enabled=req.enabled,
                settings=req.settings if req.settings else None,
            )
            return config.to_dict()
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.post("/outreach/platforms/{platform}/enable")
    async def enable_platform(platform: str):
        """Enable an outreach platform."""
        if not outreach_config.enable(platform):
            raise HTTPException(404, f"Platform '{platform}' not configured")
        return {"enabled": True, "platform": platform}

    @app.post("/outreach/platforms/{platform}/disable")
    async def disable_platform(platform: str):
        """Disable an outreach platform."""
        if not outreach_config.disable(platform):
            raise HTTPException(404, f"Platform '{platform}' not configured")
        return {"disabled": True, "platform": platform}

    @app.post("/outreach/platforms/{platform}/test")
    async def test_platform(platform: str):
        """Test connectivity to a platform.

        Attempts to call the platform's API with the stored token
        and returns success/error info.
        """
        result = outreach_config.test_connection(platform)
        return result

    @app.delete("/outreach/platforms/{platform}")
    async def delete_platform(platform: str):
        """Remove a platform configuration entirely."""
        deleted = outreach_config.delete(platform)
        if not deleted:
            raise HTTPException(404, f"Platform '{platform}' not configured")
        return {"deleted": True, "platform": platform}

    # ── Agent Registry Endpoints ────────────────────────────

    @app.post("/agents")
    async def register_agent(req: RegisterAgentRequest):
        """Register a new agent or update an existing one."""
        agent = agents.register(
            req.name,
            display_name=req.display_name,
            model=req.model,
            soul=req.soul,
            users=req.users,
            boundaries=req.boundaries,
            system_prompt=req.system_prompt,
            working_dir=req.working_dir,
            permission_mode=req.permission_mode,
            allowed_tools=req.allowed_tools,
            max_turns=req.max_turns,
            timeout=req.timeout,
            max_sessions=req.max_sessions,
            plain_text_fallback=req.plain_text_fallback,
            groups=req.groups,
        )
        return agent.to_dict()

    @app.get("/agents")
    async def list_agents(group: str = "", enabled_only: bool = False):
        """List all registered agents."""
        result = agents.list(group=group, enabled_only=enabled_only)
        return {"agents": [a.to_dict() for a in result], "count": len(result)}

    @app.get("/agents/retired")
    async def list_retired_agents():
        """List all retired agents."""
        result = agents.list_retired()
        return {"agents": [a.to_dict() for a in result], "count": len(result)}

    def _agent_presence(agent_name: str) -> dict:
        """Compute presence for an agent (shared by /presence and /card)."""
        live = broker.get_live_agents()
        streaming = agent_name in live
        hb = agents.get_latest_heartbeat(agent_name)
        if streaming:
            status = "online"
        elif hb:
            age = time.time() - hb.timestamp
            if hb.status == "alive" and age < 300:
                status = "online"
            elif hb.status == "alive" and age < 1800:
                status = "idle"
            else:
                status = "offline"
        else:
            status = "unknown"
        return {"status": status, "streaming": streaming, "last_seen": hb.timestamp if hb else 0}

    @app.get("/agents/presence")
    async def get_all_agent_presence():
        """Get presence status for all enabled agents."""
        all_agents = agents.list(enabled_only=True)
        result = []
        for agent in all_agents:
            p = _agent_presence(agent.name)
            result.append({
                "agent": agent.name,
                "display_name": agent.display_name or agent.name,
                **p,
            })
        return {"agents": result}

    @app.get("/agents/{name}")
    async def get_agent(name: str):
        """Get an agent by name."""
        agent = agents.get(name)
        if not agent:
            raise HTTPException(404, f"Agent '{name}' not found")
        return agent.to_dict()

    @app.get("/agents/{name}/presence")
    async def get_agent_presence(name: str):
        """Get presence status for a specific agent."""
        agent = agents.get(name)
        if not agent:
            raise HTTPException(404, f"Agent '{name}' not found")
        p = _agent_presence(name)
        return {"agent": name, "display_name": agent.display_name or agent.name, **p}

    @app.get("/agents/{name}/card")
    async def get_agent_card(name: str):
        """Get an agent's capability card for inter-agent discovery."""
        agent = agents.get(name)
        if not agent:
            raise HTTPException(404, f"Agent '{name}' not found")
        p = _agent_presence(name)
        directives = agents.get_directives(name)
        return {
            "name": agent.name,
            "display_name": agent.display_name or agent.name,
            "role": agent.role,
            "model": agent.model,
            "status": p["status"],
            "last_seen": p["last_seen"],
            "capabilities": [d.directive for d in directives],
            "groups": agent.groups,
        }

    @app.put("/agents/{name}")
    async def update_agent(name: str, req: UpdateAgentRequest):
        """Update an agent's configuration."""
        existing = agents.get(name)
        if not existing:
            raise HTTPException(404, f"Agent '{name}' not found")

        kwargs = {k: v for k, v in req.model_dump().items() if v is not None}
        agent = agents.register(name, **kwargs)

        # If soul-related fields changed, sync CLAUDE.md and archive version
        soul_fields = {"soul", "users", "boundaries", "system_prompt"}
        if soul_fields & kwargs.keys():
            work_dir = Path(agent.working_dir or default_working_dir).resolve()
            work_dir.mkdir(parents=True, exist_ok=True)
            system_prompt = agents.build_system_prompt(name, skill_store=skills)
            (work_dir / "CLAUDE.md").write_text(system_prompt)
            agents.save_soul_version(name, system_prompt, source="ui")
            _log(f"api: synced CLAUDE.md for {name}")

        return agent.to_dict()

    @app.post("/agents/{name}/claude-md/rebuild")
    async def rebuild_claude_md(name: str):
        """Rebuild CLAUDE.md from DB fields (soul + boundaries + directives + skills + owner profile).

        Overwrites the on-disk CLAUDE.md with a fresh build from source components.
        """
        agent = agents.get(name)
        if not agent:
            raise HTTPException(404, f"Agent '{name}' not found")

        work_dir = Path(agent.working_dir or default_working_dir).resolve()
        work_dir.mkdir(parents=True, exist_ok=True)
        claude_md = work_dir / "CLAUDE.md"
        # Snapshot on-disk content before overwriting (catches agent self-edits)
        if claude_md.exists():
            agents.save_soul_version(name, claude_md.read_text(), source="agent")
        system_prompt = agents.build_system_prompt(name, skill_store=skills)
        claude_md.write_text(system_prompt)
        agents.save_soul_version(name, system_prompt, source="rebuild")
        _log(f"api: rebuilt CLAUDE.md for {name} ({len(system_prompt)} chars)")
        return {"content": system_prompt, "size": len(system_prompt)}

    @app.get("/agents/{name}/soul/versions")
    async def list_soul_versions(name: str, limit: int = 20):
        """List archived soul versions for an agent."""
        agent = agents.get(name)
        if not agent:
            raise HTTPException(404, f"Agent '{name}' not found")
        versions = agents.get_soul_versions(name, limit=limit)
        return {"agent": name, "versions": versions, "count": len(versions)}

    @app.get("/agents/{name}/soul/versions/{version_id}")
    async def get_soul_version(name: str, version_id: int):
        """Get a specific soul version content."""
        version = agents.get_soul_version(name, version_id)
        if not version:
            raise HTTPException(404, f"Soul version {version_id} not found for '{name}'")
        return version

    @app.post("/agents/{name}/soul/versions/{version_id}/restore")
    async def restore_soul_version(name: str, version_id: int):
        """Restore an agent's soul from an archived version."""
        version = agents.get_soul_version(name, version_id)
        if not version:
            raise HTTPException(404, f"Soul version {version_id} not found for '{name}'")
        agent = agents.register(name, soul=version["content"])
        work_dir = Path(agent.working_dir or default_working_dir).resolve()
        work_dir.mkdir(parents=True, exist_ok=True)
        (work_dir / "CLAUDE.md").write_text(version["content"])
        agents.save_soul_version(name, version["content"], source=f"restore-v{version_id}")
        _log(f"api: restored soul version {version_id} for {name}")
        return {"restored": True, "agent": name, "version_id": version_id}

    @app.delete("/agents/{name}")
    async def retire_agent(name: str):
        """Retire an agent (soft delete). Preserves all data for restoration."""
        retired = agents.retire(name)
        if not retired:
            raise HTTPException(404, f"Agent '{name}' not found")
        return {"retired": True, "name": name}

    @app.post("/agents/{name}/restore")
    async def restore_agent(name: str):
        """Restore a retired agent back to active."""
        restored = agents.restore(name)
        if not restored:
            raise HTTPException(404, f"Agent '{name}' not found")
        return {"restored": True, "name": name}

    # ── Agent Directives ────────────────────────────────────

    @app.post("/agents/{name}/directives")
    async def add_directive(name: str, req: AddDirectiveRequest):
        """Add a persistent directive to an agent."""
        if not agents.get(name):
            raise HTTPException(404, f"Agent '{name}' not found")
        directive = agents.add_directive(name, req.directive, priority=req.priority)
        return directive.to_dict()

    @app.get("/agents/{name}/directives")
    async def get_directives(name: str, active_only: bool = True):
        """Get all directives for an agent."""
        if not agents.get(name):
            raise HTTPException(404, f"Agent '{name}' not found")
        result = agents.get_directives(name, active_only=active_only)
        return {"agent": name, "directives": [d.to_dict() for d in result], "count": len(result)}

    @app.delete("/agents/{name}/directives/{directive_id}")
    async def remove_directive(name: str, directive_id: int):
        """Remove a directive."""
        if not agents.remove_directive(directive_id):
            raise HTTPException(404, "Directive not found")
        return {"deleted": True, "id": directive_id}

    @app.post("/agents/{name}/directives/{directive_id}/toggle")
    async def toggle_directive(name: str, directive_id: int, active: bool = True):
        """Enable or disable a directive."""
        if not agents.toggle_directive(directive_id, active):
            raise HTTPException(404, "Directive not found")
        return {"id": directive_id, "active": active}

    # ── Agent Tokens ────────────────────────────────────────

    @app.put("/agents/{name}/tokens/{platform}")
    async def set_agent_token(name: str, platform: str, req: SetAgentTokenRequest):
        """Set a bot token for an agent on a platform."""
        if not agents.get(name):
            raise HTTPException(404, f"Agent '{name}' not found")
        token = agents.set_token(name, platform, req.token, enabled=req.enabled, settings=req.settings)
        return token.to_dict()

    @app.get("/agents/{name}/tokens")
    async def list_agent_tokens(name: str):
        """List all tokens for an agent."""
        if not agents.get(name):
            raise HTTPException(404, f"Agent '{name}' not found")
        result = agents.list_tokens(name)
        return {"agent": name, "tokens": [t.to_dict() for t in result], "count": len(result)}

    @app.delete("/agents/{name}/tokens/{platform}")
    async def remove_agent_token(name: str, platform: str):
        """Remove a bot token for an agent."""
        if not agents.remove_token(name, platform):
            raise HTTPException(404, "Token not found")
        return {"deleted": True, "agent": name, "platform": platform}

    # ── MCP Servers ────────────────────────────────────────

    @app.get("/agents/{name}/mcp-servers")
    async def list_mcp_servers(name: str):
        """List all MCP servers for an agent (core + skill + custom)."""
        agent = agents.get(name)
        if not agent:
            raise HTTPException(404, f"Agent '{name}' not found")
        work_dir = Path(agent.working_dir).resolve() if agent.working_dir else None

        # Read current .mcp.json for core + skill servers
        core_and_skill: dict[str, dict] = {}
        if work_dir:
            mcp_json = work_dir / ".mcp.json"
            if mcp_json.exists():
                try:
                    data = json.loads(mcp_json.read_text())
                    core_and_skill = data.get("mcpServers", {})
                except Exception:
                    pass

        # Identify core server names
        core_names = {"pinky-memory", "pinky-self", "pinky-messaging"}

        # Get skill server names
        skill_names: set[str] = set()
        if skills:
            materialized = skills.materialize_for_agent(name)
            skill_names = set(materialized.get("mcp_servers", {}).keys())

        # Custom servers from DB
        custom_servers = agents.list_mcp_servers(name)
        custom_names = {s["server_name"] for s in custom_servers}

        servers = []
        # Emit core + skill servers from .mcp.json
        for sname, cfg in core_and_skill.items():
            if sname in custom_names:
                continue  # will be listed from DB below
            source = "core" if sname in core_names else "skill" if sname in skill_names else "config"
            entry = {"name": sname, "source": source, "enabled": True}
            if "command" in cfg:
                entry["server_type"] = "stdio"
                entry["command"] = cfg.get("command", "")
                entry["args"] = cfg.get("args", [])
            elif "url" in cfg:
                entry["server_type"] = "http"
                entry["url"] = cfg.get("url", "")
            if "env" in cfg:
                entry["env"] = cfg["env"]
            servers.append(entry)

        # Emit custom DB servers
        for srv in custom_servers:
            servers.append({
                "name": srv["server_name"],
                "source": "custom",
                "server_type": srv["server_type"],
                "command": srv["command"],
                "args": json.loads(srv["args"]),
                "url": srv["url"],
                "env": json.loads(srv["env"]),
                "enabled": srv["enabled"],
            })

        return {"servers": servers, "count": len(servers)}

    @app.post("/agents/{name}/mcp-servers")
    async def add_mcp_server(name: str, req: AddMcpServerRequest):
        """Add a custom MCP server for an agent."""
        agent = agents.get(name)
        if not agent:
            raise HTTPException(404, f"Agent '{name}' not found")
        if not req.name.strip():
            raise HTTPException(400, "Server name is required")
        try:
            row_id = agents.add_mcp_server(
                name, req.name.strip(), req.server_type,
                command=req.command, args=json.dumps(req.args),
                url=req.url, env=json.dumps(req.env),
            )
        except Exception as e:
            if "UNIQUE" in str(e):
                raise HTTPException(409, f"MCP server '{req.name}' already exists for {name}")
            raise
        work_dir = Path(agent.working_dir).resolve() if agent.working_dir else Path(".")
        _write_mcp_json(work_dir, name, agent_registry=agents, skill_store=skills)
        return {"id": row_id, "server_name": req.name, "agent": name}

    @app.put("/agents/{name}/mcp-servers/{server_name}")
    async def update_mcp_server(name: str, server_name: str, req: UpdateMcpServerRequest):
        """Update a custom MCP server."""
        agent = agents.get(name)
        if not agent:
            raise HTTPException(404, f"Agent '{name}' not found")
        kwargs = {}
        if req.server_type is not None:
            kwargs["server_type"] = req.server_type
        if req.command is not None:
            kwargs["command"] = req.command
        if req.args is not None:
            kwargs["args"] = json.dumps(req.args)
        if req.url is not None:
            kwargs["url"] = req.url
        if req.env is not None:
            kwargs["env"] = json.dumps(req.env)
        if not agents.update_mcp_server(name, server_name, **kwargs):
            raise HTTPException(404, f"MCP server '{server_name}' not found")
        work_dir = Path(agent.working_dir).resolve() if agent.working_dir else Path(".")
        _write_mcp_json(work_dir, name, agent_registry=agents, skill_store=skills)
        return {"updated": True, "server_name": server_name}

    @app.delete("/agents/{name}/mcp-servers/{server_name}")
    async def delete_mcp_server(name: str, server_name: str):
        """Remove a custom MCP server."""
        agent = agents.get(name)
        if not agent:
            raise HTTPException(404, f"Agent '{name}' not found")
        if not agents.delete_mcp_server(name, server_name):
            raise HTTPException(404, f"MCP server '{server_name}' not found")
        work_dir = Path(agent.working_dir).resolve() if agent.working_dir else Path(".")
        _write_mcp_json(work_dir, name, agent_registry=agents, skill_store=skills)
        return {"deleted": True, "server_name": server_name}

    @app.post("/agents/{name}/mcp-servers/{server_name}/toggle")
    async def toggle_mcp_server(name: str, server_name: str, enabled: bool = True):
        """Enable or disable a custom MCP server."""
        agent = agents.get(name)
        if not agent:
            raise HTTPException(404, f"Agent '{name}' not found")
        if not agents.toggle_mcp_server(name, server_name, enabled):
            raise HTTPException(404, f"MCP server '{server_name}' not found")
        work_dir = Path(agent.working_dir).resolve() if agent.working_dir else Path(".")
        _write_mcp_json(work_dir, name, agent_registry=agents, skill_store=skills)
        return {"toggled": True, "server_name": server_name, "enabled": enabled}

    # ── Approved Users ──────────────────────────────────────

    @app.get("/agents/{name}/approved-users")
    async def list_approved_users(name: str):
        """List approved users for an agent."""
        if not agents.get(name):
            raise HTTPException(404, f"Agent '{name}' not found")
        users = agents.list_approved_users(name)
        return {"users": [u.to_dict() for u in users], "count": len(users)}

    @app.post("/agents/{name}/approved-users")
    async def approve_user(name: str, req: ApproveUserRequest):
        """Approve a user for this agent. Delivers any pending messages."""
        if not agents.get(name):
            raise HTTPException(404, f"Agent '{name}' not found")
        user = agents.approve_user(name, req.chat_id, req.display_name or "", req.approved_by or "admin")
        # Deliver pending messages in background
        delivered = 0
        try:
            delivered = await broker.handle_approval(name, req.chat_id)
        except Exception as e:
            _log(f"api: failed to deliver pending messages for {req.chat_id}: {e}")
        result = user.to_dict()
        result["pending_delivered"] = delivered
        return result

    @app.put("/agents/{name}/approved-users/{chat_id}/deny")
    async def deny_user(name: str, chat_id: str):
        """Deny a user."""
        agents.deny_user(name, chat_id)
        return {"denied": True, "chat_id": chat_id}

    @app.delete("/agents/{name}/approved-users/{chat_id}")
    async def revoke_user(name: str, chat_id: str):
        """Revoke an approved user."""
        agents.revoke_user(name, chat_id)
        return {"revoked": True, "chat_id": chat_id}

    @app.put("/agents/{name}/approved-users/{chat_id}/timezone")
    async def set_user_timezone(name: str, chat_id: str, timezone: str = ""):
        """Set a user's timezone (IANA format, e.g. America/Los_Angeles)."""
        if not timezone:
            raise HTTPException(400, "timezone parameter required")
        # Validate timezone
        try:
            from zoneinfo import ZoneInfo
            ZoneInfo(timezone)
        except Exception:
            raise HTTPException(400, f"Invalid timezone: {timezone}")
        if not agents.set_user_timezone(name, chat_id, timezone):
            raise HTTPException(404, "User not found")
        return {"updated": True, "chat_id": chat_id, "timezone": timezone}

    # ── Pending Messages (Broker) ──────────────────────────

    @app.get("/agents/{name}/pending-messages")
    async def list_pending_messages(name: str):
        """List pending messages from unapproved users."""
        if not agents.get(name):
            raise HTTPException(404, f"Agent '{name}' not found")
        messages = agents.get_pending_messages(name)
        # Group by chat_id for UI convenience
        by_chat: dict[str, list] = {}
        for msg in messages:
            by_chat.setdefault(msg["chat_id"], []).append(msg)
        return {
            "agent": name,
            "pending_users": len(by_chat),
            "total_messages": len(messages),
            "by_sender": by_chat,
        }

    @app.delete("/agents/{name}/pending-messages/{chat_id}")
    async def delete_pending_messages(name: str, chat_id: str):
        """Delete pending messages for a specific chat."""
        count = agents.delete_pending_messages(name, chat_id)
        return {"deleted": count, "chat_id": chat_id}

    # ── Group Chats (Broker) ───────────────────────────────

    @app.get("/agents/{name}/group-chats")
    async def list_group_chats(name: str):
        """List TG group chats the agent's bot is in."""
        if not agents.get(name):
            raise HTTPException(404, f"Agent '{name}' not found")
        chats = agents.list_group_chats(name)
        return {"agent": name, "group_chats": chats, "count": len(chats)}

    @app.put("/agents/{name}/group-chats/{chat_id}")
    async def update_group_chat(name: str, chat_id: str, alias: str = ""):
        """Set alias for a group chat."""
        if not agents.update_group_chat_alias(name, chat_id, alias):
            raise HTTPException(404, "Group chat not found")
        return {"updated": True, "chat_id": chat_id, "alias": alias}

    @app.delete("/agents/{name}/group-chats/{chat_id}")
    async def deactivate_group_chat(name: str, chat_id: str):
        """Mark a group chat as inactive."""
        if not agents.deactivate_group_chat(name, chat_id):
            raise HTTPException(404, "Group chat not found")
        return {"deactivated": True, "chat_id": chat_id}

    # ── Channel → Session Assignment ──────────────────────

    @app.get("/agents/{name}/channel-sessions")
    async def list_channel_sessions(name: str):
        """List channel→session mappings for an agent."""
        if not agents.get(name):
            raise HTTPException(404, f"Agent '{name}' not found")
        return {
            "agent": name,
            "mappings": agents.list_channel_sessions(name),
        }

    @app.put("/agents/{name}/channel-sessions/{chat_id}")
    async def set_channel_session(name: str, chat_id: str, session_label: str = "main"):
        """Assign a channel to a streaming session label."""
        if not agents.get(name):
            raise HTTPException(404, f"Agent '{name}' not found")
        agents.set_channel_session(name, chat_id, session_label)
        return {"updated": True, "chat_id": chat_id, "session_label": session_label}

    @app.delete("/agents/{name}/channel-sessions/{chat_id}")
    async def clear_channel_session(name: str, chat_id: str):
        """Remove a channel→session assignment (reverts to main)."""
        if not agents.clear_channel_session(name, chat_id):
            raise HTTPException(404, "Channel session mapping not found")
        return {"cleared": True, "chat_id": chat_id}

    # ── Streaming Sessions ──────────────────────────────────

    @app.get("/agents/{name}/streaming-sessions")
    async def list_streaming_sessions(name: str):
        """List active streaming sessions for an agent."""
        if not agents.get(name):
            raise HTTPException(404, f"Agent '{name}' not found")
        return {
            "agent": name,
            "sessions": broker.list_streaming_sessions(name),
        }

    @app.post("/agents/{name}/streaming-sessions")
    async def create_streaming_session(name: str, label: str = "main"):
        """Create a new streaming session for an agent."""
        agent = agents.get(name)
        if not agent:
            raise HTTPException(404, f"Agent '{name}' not found")

        # Check if label already exists
        existing = broker.list_streaming_sessions(name)
        for s in existing:
            if s["label"] == label:
                raise HTTPException(409, f"Streaming session '{label}' already exists for {name}")

        try:
            await _start_streaming_session(
                name,
                label=label,
                resume_id=agents.get_streaming_session_id(name, label=label),
            )
            _log(f"api: created streaming session {name}/{label}")
            return {"created": True, "agent": name, "label": label}
        except Exception as e:
            raise HTTPException(500, f"Failed to create streaming session: {e}")

    @app.delete("/agents/{name}/streaming-sessions/{label}")
    async def delete_streaming_session(name: str, label: str):
        """Stop and remove a streaming session."""
        if label == "main":
            raise HTTPException(400, "Cannot delete the main streaming session")
        sessions = broker._streaming.get(name, {})
        ss = sessions.get(label)
        if not ss:
            raise HTTPException(404, f"Streaming session '{label}' not found for {name}")
        try:
            await ss.disconnect()
        except Exception:
            pass
        agents.set_streaming_session_id(name, "", label=label)
        broker.unregister_streaming(name, label=label)
        return {"deleted": True, "agent": name, "label": label}

    # ── Broker Status ──────────────────────────────────────

    @app.get("/broker/status")
    async def broker_status():
        """Get message broker status."""
        return {
            "stats": broker.stats,
            "active_pollers": [
                {"agent": p.agent_name, "polls": p.poll_count, "running": p.is_running}
                for p in _broker_pollers
            ],
        }

    @app.post("/broker/send")
    async def broker_send_message(req: dict):
        """Send an outbound message through the broker on behalf of an agent."""
        agent_name = req.get("agent_name", "")
        platform = req.get("platform", "telegram")
        chat_id = req.get("chat_id", "")
        content = req.get("content", "")
        reply_to = req.get("reply_to", "")
        parse_mode = req.get("parse_mode", "")
        if not agent_name or not chat_id or not content:
            raise HTTPException(400, "agent_name, chat_id, and content are required")
        result = await _broker_send(
            agent_name,
            platform,
            chat_id,
            content,
            reply_to=reply_to,
            parse_mode=parse_mode,
        )
        _record_outbound_message(
            agent_name,
            platform=platform,
            chat_id=chat_id,
            content=content,
            metadata={
                "tool": "send",
                "reply_to": reply_to,
                "delivery": result,
            },
        )
        return result

    @app.post("/broker/thread")
    async def broker_thread(req: dict):
        """Send a threaded/quoted reply to an inbound message using stored broker context."""
        agent_name = req.get("agent_name", "")
        source_message_id = req.get("message_id", "")
        content = req.get("content", "").strip()
        parse_mode = req.get("parse_mode", "")
        if not agent_name or not source_message_id or not content:
            raise HTTPException(400, "agent_name, message_id, and content are required")

        ctx = _resolve_message_context(agent_name, source_message_id)
        voice_settings = _get_voice_reply_settings(agent_name, ctx.platform)

        if ctx.source_was_voice and voice_settings:
            result = await _broker_send_voice_message(
                agent_name,
                ctx.platform,
                ctx.chat_id,
                content,
                provider=voice_settings["provider"],
                voice=voice_settings["voice"],
                model=voice_settings["model"],
                reply_to=ctx.message_id,
                include_text_copy=True,
            )
            _record_outbound_message(
                agent_name,
                platform=ctx.platform,
                chat_id=ctx.chat_id,
                content=content,
                metadata={
                    "tool": "thread",
                    "source_message_id": ctx.message_id,
                    "delivery_mode": "voice_auto_reply",
                    "delivery": result,
                },
            )
            return result

        result = await _broker_send(
            agent_name,
            ctx.platform,
            ctx.chat_id,
            content,
            reply_to=ctx.message_id,
            parse_mode=parse_mode,
        )
        _record_outbound_message(
            agent_name,
            platform=ctx.platform,
            chat_id=ctx.chat_id,
            content=content,
            metadata={
                "tool": "thread",
                "source_message_id": ctx.message_id,
                "delivery": result,
            },
        )
        return result

    @app.post("/broker/broadcast")
    async def broker_broadcast(req: dict):
        """Broadcast a message to all active channels for an agent."""
        agent_name = req.get("agent_name", "")
        content = req.get("content", "").strip()
        if not agent_name or not content:
            raise HTTPException(400, "agent_name and content are required")

        deliveries: list[dict] = []
        errors: list[str] = []
        for user in agents.list_approved_users(agent_name):
            if user.status != "approved":
                continue
            try:
                result = await _broker_send(agent_name, "telegram", user.chat_id, content)
                deliveries.append(result)
                _record_outbound_message(
                    agent_name,
                    platform="telegram",
                    chat_id=user.chat_id,
                    content=content,
                    metadata={"tool": "broadcast", "delivery": result},
                )
            except Exception as e:
                errors.append(f"telegram:{user.chat_id}: {e}")
                _log(f"broker-broadcast: failed for {agent_name} -> telegram:{user.chat_id}: {e}")

        for group in agents.list_group_chats(agent_name):
            try:
                result = await _broker_send(agent_name, group["platform"], group["chat_id"], content)
                deliveries.append(result)
                _record_outbound_message(
                    agent_name,
                    platform=group["platform"],
                    chat_id=group["chat_id"],
                    content=content,
                    metadata={"tool": "broadcast", "delivery": result},
                )
            except Exception as e:
                errors.append(f"{group['platform']}:{group['chat_id']}: {e}")
                _log(f"broker-broadcast: failed for {agent_name} -> {group['platform']}:{group['chat_id']}: {e}")

        return {"sent": True, "count": len(deliveries), "errors": errors, "deliveries": deliveries}

    @app.post("/broker/send-photo")
    async def broker_send_photo(req: dict):
        """Send a photo through the broker on behalf of an agent."""
        agent_name = req.get("agent_name", "")
        source_message_id = req.get("message_id", "")
        platform = req.get("platform", "telegram")
        chat_id = req.get("chat_id", "")
        file_path = req.get("file_path", "")
        caption = req.get("caption", "")
        reply_to = ""
        if source_message_id and not chat_id:
            ctx = _resolve_message_context(agent_name, source_message_id)
            platform = ctx.platform
            chat_id = ctx.chat_id
            reply_to = ctx.message_id
        if not agent_name or not chat_id or not file_path:
            raise HTTPException(400, "agent_name, chat_id, and file_path are required")
        msg = _send_file_message(agent_name, platform, chat_id, file_path, caption=caption, reply_to=reply_to, kind="photo")
        result = {"sent": True, "message_id": msg.message_id, "platform": platform, "chat_id": chat_id}
        _record_outbound_message(
            agent_name,
            platform=platform,
            chat_id=chat_id,
            content=caption or "[photo]",
            metadata={"tool": "send_photo", "source_message_id": source_message_id, "file_path": file_path, "delivery": result},
        )
        return result

    @app.post("/broker/send-document")
    async def broker_send_document(req: dict):
        """Send a document through the broker on behalf of an agent."""
        agent_name = req.get("agent_name", "")
        source_message_id = req.get("message_id", "")
        platform = req.get("platform", "telegram")
        chat_id = req.get("chat_id", "")
        file_path = req.get("file_path", "")
        caption = req.get("caption", "")
        reply_to = ""
        if source_message_id and not chat_id:
            ctx = _resolve_message_context(agent_name, source_message_id)
            platform = ctx.platform
            chat_id = ctx.chat_id
            reply_to = ctx.message_id
        if not agent_name or not chat_id or not file_path:
            raise HTTPException(400, "agent_name, chat_id, and file_path are required")
        msg = _send_file_message(agent_name, platform, chat_id, file_path, caption=caption, reply_to=reply_to, kind="document")
        result = {"sent": True, "message_id": msg.message_id, "platform": platform, "chat_id": chat_id}
        _record_outbound_message(
            agent_name,
            platform=platform,
            chat_id=chat_id,
            content=caption or f"[document] {Path(file_path).name}",
            metadata={"tool": "send_document", "source_message_id": source_message_id, "file_path": file_path, "delivery": result},
        )
        return result

    @app.post("/broker/send-gif")
    async def broker_send_gif(req: dict):
        """Search Giphy and send the result as an animation through the broker.

        API key is resolved from system settings, then env var, then a public
        fallback key (rate-limited — set GIPHY_API_KEY in Settings for prod).
        """
        import random
        import tempfile
        import urllib.parse

        _GIPHY_PUBLIC_KEY = "dc6zaTOxFJmzC"

        agent_name_req = req.get("agent_name", "")
        source_message_id = req.get("message_id", "")
        platform = req.get("platform", "telegram")
        chat_id = req.get("chat_id", "")
        query = req.get("query", "").strip()
        caption = req.get("caption", "")
        reply_to = ""

        if source_message_id and not chat_id:
            ctx = _resolve_message_context(agent_name_req, source_message_id)
            platform = ctx.platform
            chat_id = ctx.chat_id
            reply_to = ctx.message_id

        if not agent_name_req or not chat_id or not query:
            raise HTTPException(400, "agent_name, chat_id, and query are required")

        if platform != "telegram":
            raise HTTPException(503, f"No {platform} GIF adapter for {agent_name_req}")

        adapter = _get_tg_adapter(agent_name_req)
        if not adapter:
            raise HTTPException(503, f"No {platform} adapter for {agent_name_req}")

        # Resolve Giphy API key: settings > env > public fallback
        api_key = agents.get_setting("GIPHY_API_KEY") or os.environ.get("GIPHY_API_KEY", _GIPHY_PUBLIC_KEY)

        # Search Giphy
        params = urllib.parse.urlencode({
            "api_key": api_key,
            "q": query,
            "limit": 10,
            "rating": "g",
            "lang": "en",
        })
        search_url = f"https://api.giphy.com/v1/gifs/search?{params}"
        try:
            with urllib.request.urlopen(search_url, timeout=10) as resp:
                data = json.loads(resp.read())
        except Exception as e:
            raise HTTPException(502, f"Giphy search failed: {e}")

        results = data.get("data", [])
        if not results:
            raise HTTPException(404, f"No GIFs found for query: {query!r}")

        # Pick randomly from top 5 for variety
        pick = random.choice(results[:min(5, len(results))])
        gif_url = pick["images"]["original"]["url"].split("?")[0]

        # Download GIF to a temp file
        try:
            with tempfile.NamedTemporaryFile(suffix=".gif", delete=False) as tmp:
                tmp_path = tmp.name
            with urllib.request.urlopen(gif_url, timeout=30) as resp:
                with open(tmp_path, "wb") as f:
                    f.write(resp.read())
        except Exception as e:
            raise HTTPException(502, f"GIF download failed: {e}")

        try:
            msg = adapter.send_animation(chat_id, tmp_path, caption=caption, reply_to_message_id=int(reply_to) if reply_to else None)
            result = {"sent": True, "message_id": msg.message_id, "query": query, "platform": platform, "chat_id": chat_id}
            _record_outbound_message(
                agent_name_req,
                platform=platform,
                chat_id=chat_id,
                content=caption or f"[gif] {query}",
                metadata={"tool": "send_gif", "source_message_id": source_message_id, "query": query, "delivery": result},
            )
            return result
        except Exception as e:
            raise HTTPException(500, str(e))

    @app.post("/broker/send-animation")
    async def broker_send_animation(req: dict):
        """Send an animation (GIF) through the broker on behalf of an agent."""
        agent_name = req.get("agent_name", "")
        source_message_id = req.get("message_id", "")
        platform = req.get("platform", "telegram")
        chat_id = req.get("chat_id", "")
        file_path = req.get("file_path", "")
        caption = req.get("caption", "")
        reply_to = ""
        if source_message_id and not chat_id:
            ctx = _resolve_message_context(agent_name, source_message_id)
            platform = ctx.platform
            chat_id = ctx.chat_id
            reply_to = ctx.message_id
        if not agent_name or not chat_id or not file_path:
            raise HTTPException(400, "agent_name, chat_id, and file_path are required")
        msg = _send_file_message(agent_name, platform, chat_id, file_path, caption=caption, reply_to=reply_to, kind="animation")
        result = {"sent": True, "message_id": msg.message_id, "platform": platform, "chat_id": chat_id}
        _record_outbound_message(
            agent_name,
            platform=platform,
            chat_id=chat_id,
            content=caption or f"[animation] {Path(file_path).name}",
            metadata={"tool": "send_animation", "source_message_id": source_message_id, "file_path": file_path, "delivery": result},
        )
        return result

    @app.post("/broker/send-voice")
    async def broker_send_voice(req: dict):
        """Generate TTS audio and send as a voice message through the broker.

        Supports ElevenLabs, OpenAI TTS, and Deepgram Aura. API keys are
        read from system settings (settings panel) or env vars.
        """
        agent_name_req = req.get("agent_name", "")
        source_message_id = req.get("message_id", "")
        platform = req.get("platform", "telegram")
        chat_id = req.get("chat_id", "")
        text = req.get("text", "").strip()
        provider = req.get("provider", "openai")
        voice = req.get("voice", "")
        model = req.get("model", "")
        reply_to = ""

        if source_message_id and not chat_id:
            ctx = _resolve_message_context(agent_name_req, source_message_id)
            platform = ctx.platform
            chat_id = ctx.chat_id
            reply_to = ctx.message_id

        if not agent_name_req or not chat_id or not text:
            raise HTTPException(400, "agent_name, chat_id, and text are required")

        result = await _broker_send_voice_message(
            agent_name_req,
            platform,
            chat_id,
            text,
            provider=provider,
            voice=voice,
            model=model,
            reply_to=reply_to,
        )
        _record_outbound_message(
            agent_name_req,
            platform=platform,
            chat_id=chat_id,
            content=text,
            metadata={"tool": "send_voice", "source_message_id": source_message_id, "delivery": result},
        )
        return result

    @app.post("/broker/react")
    async def broker_react(req: dict):
        """Add a reaction through the broker on behalf of an agent."""
        agent_name = req.get("agent_name", "")
        platform = req.get("platform", "telegram")
        chat_id = req.get("chat_id", "")
        message_id = req.get("message_id", "")
        emoji = req.get("emoji", "")
        if message_id and not chat_id:
            ctx = _resolve_message_context(agent_name, message_id)
            platform = ctx.platform
            chat_id = ctx.chat_id
        if not agent_name or not chat_id or not message_id or not emoji:
            raise HTTPException(400, "agent_name, message_id, and emoji are required")
        await _broker_react(agent_name, platform, chat_id, message_id, emoji)
        return {"reacted": True}

    @app.post("/agents/{name}/streaming/restart")
    async def restart_streaming_session(name: str):
        """Restart an agent's streaming session — fresh context, new CC session."""
        ss = broker._get_streaming_session(name)
        if not ss:
            raise HTTPException(404, f"No streaming session for '{name}'")

        old_session_id = ss.session_id
        old_turns = ss._stats["turns"]

        # Disconnect and clear persisted session ID
        await ss.disconnect()
        agents.set_streaming_session_id(name, "", label="main")

        # Refresh wake context and reconnect fresh
        ss._config.wake_context = _build_streaming_wake_context(name)
        ss._config.resume_session_id = ""
        ss.session_id = ""
        try:
            await ss.connect()
            _log(f"api: streaming session restarted for {name}")
        except Exception as e:
            broker.unregister_streaming(name)
            raise HTTPException(500, f"Failed to restart: {e}")

        return {
            "restarted": True,
            "agent": name,
            "old_session_id": old_session_id[:12] if old_session_id else "",
            "old_turns": old_turns,
        }

    @app.post("/agents/{name}/streaming/model")
    async def set_streaming_model(name: str, req: SetModelRequest):
        """Change the model on a running streaming session.

        If the context window size changes (e.g. 200k → 1M), automatically
        saves state and restarts the session. Otherwise switches mid-session.
        """
        ss = broker._get_streaming_session(name)
        if not ss:
            raise HTTPException(404, f"No streaming session for '{name}'")
        if not ss.is_connected or not ss._client:
            raise HTTPException(409, f"Streaming session for '{name}' not connected")

        # Check if context window would change
        old_max = 0
        try:
            ctx = await ss._client.get_context_usage()
            old_max = ctx.get("maxTokens", 0)
        except Exception:
            pass

        new_is_1m = req.model in _1M_MODELS
        old_is_1m = old_max > 500_000  # Current window is 1M-class

        needs_restart = new_is_1m != old_is_1m

        # Update agent config first
        agents.register(name, model=req.model)

        if needs_restart:
            # Context window changes — need full restart
            _log(f"api: model change {req.model} for {name} requires restart (window change)")

            # Ask agent to save state
            try:
                await ss._client.query(
                    "Model is being changed and your session will restart for a new context window. "
                    "Quickly save your current state to wake context or memory files."
                )
            except Exception:
                pass

            # Restart streaming session
            old_session_id = ss.session_id
            old_turns = ss._stats["turns"]
            await ss.disconnect()
            agents.set_streaming_session_id(name, "", label="main")
            ss._config.resume_session_id = ""
            ss.session_id = ""
            ss._config.model = req.model
            try:
                await ss.connect()
                _log(f"api: restarted {name} with model {req.model}")
            except Exception as e:
                broker.unregister_streaming(name)
                raise HTTPException(500, f"Failed to restart: {e}")

            return {
                "updated": True,
                "agent": name,
                "model": req.model,
                "restarted": True,
                "old_session_id": old_session_id[:12] if old_session_id else "",
                "old_turns": old_turns,
            }
        else:
            # Same window class — hot swap
            try:
                await ss._client.set_model(req.model)
                _log(f"api: model hot-swapped to {req.model} for {name}")
            except Exception as e:
                raise HTTPException(500, f"Failed to set model: {e}")

            return {"updated": True, "agent": name, "model": req.model, "restarted": False}

    @app.post("/agents/{name}/streaming/compact")
    async def compact_streaming_session(name: str):
        """Send /compact to an agent's streaming session to compress context."""
        ss = broker._get_streaming_session(name)
        if not ss:
            raise HTTPException(404, f"No streaming session for '{name}'")
        if not ss.is_connected:
            raise HTTPException(409, f"Streaming session for '{name}' not connected")

        try:
            await ss._client.query(
                "Run /compact now to compress your conversation context. "
                "Summarize key state before compacting."
            )
            _log(f"api: compact requested for {name}")
        except Exception as e:
            raise HTTPException(500, f"Compact failed: {e}")

        return {"compacted": True, "agent": name}

    @app.post("/agents/{name}/streaming/archive")
    async def archive_streaming_session(name: str):
        """Archive session: nudge agent to save memory, then start fresh."""
        ss = broker._get_streaming_session(name)
        if not ss:
            raise HTTPException(404, f"No streaming session for '{name}'")
        if not ss.is_connected:
            raise HTTPException(409, f"Streaming session for '{name}' not connected")

        # Step 1: Ask agent to save memories before archiving
        try:
            await ss._client.query(
                "Your session is about to be archived. Before it ends:\n\n"
                "1. Save everything important to your memory files (MEMORY.md and memory/*.md)\n"
                "2. Use reflect() or save_my_context to persist key learnings and state\n"
                "3. Summarize what you were working on so your next session can pick up\n\n"
                "Do this now — your session will be reset after you confirm."
            )
            _log(f"api: archive memory save requested for {name}")
        except Exception as e:
            _log(f"api: archive memory save failed for {name}: {e}")

        # Step 2: Restart with fresh context
        old_session_id = ss.session_id
        old_turns = ss._stats["turns"]

        await ss.disconnect()
        agents.set_streaming_session_id(name, "", label="main")

        ss._config.wake_context = _build_streaming_wake_context(name)
        ss._config.resume_session_id = ""
        ss.session_id = ""
        try:
            await ss.connect()
            _log(f"api: archived and restarted session for {name}")
        except Exception as e:
            broker.unregister_streaming(name)
            raise HTTPException(500, f"Failed to restart after archive: {e}")

        return {
            "archived": True,
            "agent": name,
            "old_session_id": old_session_id[:12] if old_session_id else "",
            "old_turns": old_turns,
        }

    @app.get("/agents/{name}/streaming/status")
    async def streaming_session_status(name: str):
        """Get streaming session status for an agent."""
        ss = broker._get_streaming_session(name)
        if not ss:
            raise HTTPException(404, f"No streaming session for '{name}'")

        # Try to get context usage from SDK
        context_info = {}
        if ss.is_connected and ss._client:
            try:
                ctx = await ss._client.get_context_usage()
                total = ctx.get("totalTokens", 0)
                reported_max = ctx.get("maxTokens", 0)

                # Fix: SDK reports 200k for 1M models — use actual window
                model = ss._config.model or ""
                actual_max = reported_max
                if model in _1M_MODELS and reported_max <= 200_000:
                    actual_max = 1_000_000

                pct = round(total / actual_max * 100) if actual_max > 0 else 0
                context_info = {
                    "total_tokens": total,
                    "max_tokens": actual_max,
                    "percentage": pct,
                }
            except Exception:
                pass

        return {
            "agent": name,
            "session_id": ss.session_id[:12] if ss.session_id else "",
            "connected": ss.is_connected,
            "stats": ss.stats,
            "context": context_info,
        }

    @app.post("/agents/{name}/message")
    async def send_agent_message(name: str, req: AgentMessageRequest):
        """Send a message from one agent directly into another's streaming context.

        If the target agent is offline, falls back to inbox delivery so the
        message is persisted and available when the agent wakes up.
        """
        if not agents.get(name):
            raise HTTPException(404, f"Agent '{name}' not found")
        delivered = await broker.inject_agent_message(req.from_agent, name, req.message)
        if not delivered:
            # Fallback: store in inbox so agent sees it when they wake
            msg = comms.send(
                req.from_agent, name, req.message,
                metadata=req.metadata,
                content_type=req.content_type,
                parent_message_id=req.parent_message_id,
                priority=req.priority,
            )
            return {
                "delivered": False, "queued": True,
                "message_id": msg.id, "from": req.from_agent, "to": name,
            }
        return {"delivered": True, "queued": False, "from": req.from_agent, "to": name}

    @app.post("/agents/{name}/chat")
    async def chat_with_agent(name: str, req: dict):
        """Send a message from the web UI to an agent's streaming session.

        The message gets formatted with [web | dm | ...] metadata and routed
        through the streaming session. Response comes back via the callback.
        """
        agent = agents.get(name)
        if not agent:
            raise HTTPException(404, f"Agent '{name}' not found")

        content = req.get("content", "").strip()
        if not content:
            raise HTTPException(400, "content is required")

        # Get streaming session
        streaming = broker._get_streaming_session(name)
        if not streaming or not streaming.is_connected:
            raise HTTPException(503, f"Agent '{name}' streaming session not connected")

        # Format with metadata like broker messages
        from datetime import datetime
        from zoneinfo import ZoneInfo
        tz_str = agents.get_default_timezone()
        try:
            ts = datetime.now(ZoneInfo(tz_str)).strftime(f"%Y-%m-%d %H:%M:%S {tz_str}")
        except Exception:
            ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")

        prompt = f"[web | dm | Admin | web | {ts}]\n{content}"
        await streaming.send(prompt, platform="web", chat_id="web")
        return {"sent": True, "agent": name}

    @app.post("/agents/{name}/upload")
    async def upload_file_to_agent(name: str, file: UploadFile):
        """Upload a file to an agent via the web UI."""
        agent = agents.get(name)
        if not agent:
            raise HTTPException(404, f"Agent '{name}' not found")

        # Save file to data/uploads/{agent_name}/
        upload_dir = f"data/uploads/{name}"
        os.makedirs(upload_dir, exist_ok=True)
        filename = file.filename or "upload"
        dest = os.path.join(upload_dir, filename)
        if os.path.exists(dest):
            base, ext = os.path.splitext(filename)
            dest = os.path.join(upload_dir, f"{base}_{int(time.time())}{ext}")

        content_bytes = await file.read()
        with open(dest, "wb") as f:
            f.write(content_bytes)

        abs_path = os.path.abspath(dest)
        size = len(content_bytes)

        # Route to agent as a message with file attachment
        from datetime import datetime
        from zoneinfo import ZoneInfo
        tz_str = agents.get_default_timezone()
        try:
            ts = datetime.now(ZoneInfo(tz_str)).strftime(f"%Y-%m-%d %H:%M:%S {tz_str}")
        except Exception:
            ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")

        msg = BrokerMessage(
            platform="web",
            chat_id="web",
            sender_name="admin",
            sender_id="web",
            content=f"[web | dm | Admin | web | {ts}]\nFile uploaded: {filename} ({size} bytes)\nSaved to: {abs_path}",
            agent_name=name,
            attachments=[{
                "type": "file",
                "file_name": filename,
                "file_size": size,
                "path": abs_path,
            }],
        )
        await broker.handle_inbound(msg)

        return {"uploaded": True, "filename": filename, "path": abs_path, "size": size}

    # ── Spawn Session from Agent ────────────────────────────

    # ── Agent Heart Files ─────────────────────────────────

    @app.get("/agents/{name}/files")
    async def list_agent_files(name: str):
        """List heart files in the agent's working directory."""
        agent = agents.get(name)
        if not agent:
            raise HTTPException(404, f"Agent '{name}' not found")
        work_dir = Path(agent.working_dir).resolve()
        if not work_dir.exists():
            return {"agent": name, "working_dir": str(work_dir), "files": [], "exists": False}
        # Heart file extensions — config, soul, and docs
        heart_exts = {".md", ".yaml", ".yml", ".toml", ".json", ".txt", ".cfg", ".ini"}
        files = []
        for f in sorted(work_dir.iterdir()):
            if f.is_file() and not f.name.startswith('.') and (f.suffix in heart_exts or f.name == "CLAUDE.md"):
                files.append({
                    "name": f.name,
                    "size": f.stat().st_size,
                    "modified": f.stat().st_mtime,
                    "is_claude_md": f.name == "CLAUDE.md",
                })
        return {"agent": name, "working_dir": str(work_dir), "files": files, "exists": True}

    @app.get("/agents/{name}/files/{filename}")
    async def read_agent_file(name: str, filename: str):
        """Read a heart file from the agent's working directory."""
        agent = agents.get(name)
        if not agent:
            raise HTTPException(404, f"Agent '{name}' not found")
        file_path = Path(agent.working_dir).resolve() / filename
        if not file_path.exists() or not file_path.is_file():
            raise HTTPException(404, f"File '{filename}' not found")
        # Safety: don't serve files outside the working dir
        if not str(file_path).startswith(str(Path(agent.working_dir).resolve())):
            raise HTTPException(403, "Access denied")
        return {"name": filename, "content": file_path.read_text(errors="replace")}

    @app.put("/agents/{name}/files/{filename}")
    async def write_agent_file(name: str, filename: str, req: SendMessageRequest):
        """Write a heart file to the agent's working directory.

        Uses content field for file contents.
        """
        agent = agents.get(name)
        if not agent:
            raise HTTPException(404, f"Agent '{name}' not found")
        work_dir = Path(agent.working_dir).resolve()
        work_dir.mkdir(parents=True, exist_ok=True)
        file_path = work_dir / filename
        # Safety check
        if not str(file_path.resolve()).startswith(str(work_dir)):
            raise HTTPException(403, "Access denied")
        file_path.write_text(req.content)
        if filename == "CLAUDE.md":
            agents.save_soul_version(name, req.content, source="api")
        _log(f"api: wrote {filename} to {work_dir} for agent {name}")
        return {"written": True, "name": filename, "size": len(req.content)}

    # ── Spawn Session from Agent ────────────────────────────

    @app.post("/agents/{name}/sessions")
    async def spawn_agent_session(name: str, req: SpawnSessionRequest):
        """Spawn a new session from an agent's config.

        Creates a session pre-configured with the agent's model, soul,
        tools, permissions, and active directives. Writes CLAUDE.md to
        the agent's working directory before spawning.
        """
        agent = agents.get(name)
        if not agent:
            raise HTTPException(404, f"Agent '{name}' not found")
        if not agent.enabled:
            raise HTTPException(400, f"Agent '{name}' is disabled")

        # Build system prompt from agent config + directives
        system_prompt = agents.build_system_prompt(name, skill_store=skills)

        # Ensure working directory exists and write CLAUDE.md
        work_dir = Path(agent.working_dir or default_working_dir).resolve()
        work_dir.mkdir(parents=True, exist_ok=True)
        claude_md = work_dir / "CLAUDE.md"
        # Snapshot on-disk content before overwriting (catches agent self-edits)
        if claude_md.exists():
            agents.save_soul_version(name, claude_md.read_text(), source="agent")
        claude_md.write_text(system_prompt)
        agents.save_soul_version(name, system_prompt, source="spawn")
        _log(f"api: wrote CLAUDE.md ({len(system_prompt)} chars) to {work_dir}")

        # Write .mcp.json with default MCP servers + skill-provided servers
        _write_mcp_json(work_dir, name, agent_registry=agents, skill_store=skills)
        _log(f"api: wrote .mcp.json to {work_dir}")

        session_type = req.session_type or "chat"
        if session_type == "main":
            session_id = req.session_id or f"{name}-main"
        else:
            session_id = req.session_id or f"{name}-{uuid.uuid4().hex[:8]}"

        session = manager.create(
            session_id=session_id,
            model=agent.model,
            soul=agent.soul,
            working_dir=str(work_dir),
            allowed_tools=agent.allowed_tools or None,
            max_turns=agent.max_turns,
            timeout=agent.timeout,
            system_prompt=system_prompt,
            restart_threshold_pct=agent.restart_threshold_pct,
            auto_restart=agent.auto_restart,
            permission_mode=agent.permission_mode,
            session_type=session_type,
            agent_name=name,
        )

        _log(f"api: spawned {session_type} session {session.id} from agent {name}")
        info = session.info
        return {
            "agent": name,
            "session": SessionResponse(**info.to_dict()).model_dump(),
        }

    @app.get("/agents/{name}/sessions")
    async def list_agent_sessions(name: str):
        """List all active sessions spawned from an agent."""
        if not agents.get(name):
            raise HTTPException(404, f"Agent '{name}' not found")
        all_sessions = manager.list()
        agent_sessions = [s for s in all_sessions if s.agent_name == name or s.id.startswith(f"{name}-")]
        return {
            "agent": name,
            "sessions": [SessionResponse(**s.to_dict()).model_dump() for s in agent_sessions],
            "count": len(agent_sessions),
        }

    # ── Schedule Endpoints ─────────────────────────────────

    @app.post("/agents/{agent_name}/schedules")
    async def add_schedule(agent_name: str, req: AddScheduleRequest):
        """Add a cron-based wake schedule for an agent."""
        if not agents.get(agent_name):
            raise HTTPException(404, f"Agent '{agent_name}' not found")
        schedule = agents.add_schedule(
            agent_name, req.cron,
            name=req.name, prompt=req.prompt, timezone=req.timezone,
            direct_send=req.direct_send, target_channel=req.target_channel,
        )
        return schedule.to_dict()

    @app.get("/agents/{agent_name}/schedules")
    async def list_schedules(agent_name: str, enabled_only: bool = True):
        """List all schedules for an agent."""
        if not agents.get(agent_name):
            raise HTTPException(404, f"Agent '{agent_name}' not found")
        schedules = agents.get_schedules(agent_name, enabled_only=enabled_only)
        return {
            "agent": agent_name,
            "schedules": [s.to_dict() for s in schedules],
            "count": len(schedules),
        }

    @app.delete("/agents/{agent_name}/schedules/{schedule_id}")
    async def remove_schedule(agent_name: str, schedule_id: int):
        """Remove a schedule."""
        if not agents.remove_schedule(schedule_id):
            raise HTTPException(404, f"Schedule {schedule_id} not found")
        return {"deleted": True}

    @app.post("/agents/{agent_name}/schedules/{schedule_id}/toggle")
    async def toggle_schedule(agent_name: str, schedule_id: int, enabled: bool = True):
        """Enable/disable a schedule."""
        if not agents.toggle_schedule(schedule_id, enabled):
            raise HTTPException(404, f"Schedule {schedule_id} not found")
        return {"toggled": True, "enabled": enabled}

    @app.get("/schedules")
    async def list_all_schedules(enabled_only: bool = True):
        """List all schedules across all agents."""
        schedules = agents.get_all_schedules(enabled_only=enabled_only)
        return {
            "schedules": [s.to_dict() for s in schedules],
            "count": len(schedules),
        }

    # ── Heartbeat Endpoints ────────────────────────────────

    @app.post("/agents/{agent_name}/heartbeat")
    async def record_heartbeat(agent_name: str, req: RecordHeartbeatRequest):
        """Record a heartbeat for an agent."""
        if not agents.get(agent_name):
            raise HTTPException(404, f"Agent '{agent_name}' not found")
        hb = agents.record_heartbeat(
            agent_name,
            session_id=req.session_id, status=req.status,
            context_pct=req.context_pct, message_count=req.message_count,
            metadata=req.metadata,
        )
        return hb.to_dict()

    @app.get("/agents/{agent_name}/heartbeats")
    async def get_heartbeats(agent_name: str, limit: int = 20):
        """Get recent heartbeats for an agent."""
        if not agents.get(agent_name):
            raise HTTPException(404, f"Agent '{agent_name}' not found")
        heartbeats = agents.get_heartbeats(agent_name, limit=limit)
        return {
            "agent": agent_name,
            "heartbeats": [h.to_dict() for h in heartbeats],
            "count": len(heartbeats),
        }

    @app.get("/agents/{agent_name}/heartbeat")
    async def get_latest_heartbeat(agent_name: str):
        """Get the most recent heartbeat for an agent."""
        if not agents.get(agent_name):
            raise HTTPException(404, f"Agent '{agent_name}' not found")
        hb = agents.get_latest_heartbeat(agent_name)
        if not hb:
            return {"agent": agent_name, "heartbeat": None}
        return {"agent": agent_name, "heartbeat": hb.to_dict()}

    @app.get("/heartbeats")
    async def get_all_heartbeats():
        """Get latest heartbeat for every agent."""
        heartbeats = agents.get_all_latest_heartbeats()
        return {
            "heartbeats": [h.to_dict() for h in heartbeats],
            "count": len(heartbeats),
        }

    # ── Dream Endpoints ────────────────────────────────────────

    @app.post("/agents/{agent_name}/dream")
    async def trigger_dream(agent_name: str):
        """Manually trigger a dream run for an agent.

        Spawns a dedicated memory consolidation session that processes
        recent conversation history and stores durable memory nodes.
        The summary is stored and will be injected into the next wake context
        if dream_notify is enabled.
        """
        agent = agents.get(agent_name)
        if not agent:
            raise HTTPException(404, f"Agent '{agent_name}' not found")

        _log(f"api: manual dream trigger for '{agent_name}'")
        summary = await dream_runner.run_dream(agent_name, agent)
        state = dream_runner.get_state(agent_name)
        return {
            "agent": agent_name,
            "summary": summary,
            "dream_state": state,
        }

    @app.get("/agents/{agent_name}/dream/history")
    async def get_dream_history(agent_name: str):
        """Get the dream state (last run, summary, stats) for an agent."""
        if not agents.get(agent_name):
            raise HTTPException(404, f"Agent '{agent_name}' not found")
        state = dream_runner.get_state(agent_name)
        return {"agent": agent_name, "dream_state": state}

    @app.get("/dreams")
    async def list_all_dream_states():
        """List dream state for all agents that have ever dreamed."""
        states = dream_runner.list_states()
        return {"dream_states": states, "count": len(states)}

    # ── Agent Context (continuation state) ──────────────────

    @app.put("/agents/{agent_name}/context")
    async def set_agent_context(agent_name: str, req: SetContextRequest):
        """Save continuation context for an agent.

        Agents call this before a context restart to preserve their state.
        The context is injected into the system prompt on next session start.
        """
        if not agents.get(agent_name):
            raise HTTPException(404, f"Agent '{agent_name}' not found")

        # Find which session is saving (for updated_by)
        session_id = ""
        streaming_main = broker._streaming.get(agent_name, {}).get("main")
        if streaming_main and streaming_main.session_id:
            session_id = streaming_main.session_id
        for s in manager.list():
            if not session_id and s.agent_name == agent_name and s.session_type == "main":
                session_id = s.id
                break

        ctx = agents.set_context(
            agent_name,
            task=req.task, context=req.context, notes=req.notes,
            blockers=req.blockers, priority_items=req.priority_items,
            metadata=req.metadata, updated_by=session_id,
        )
        return ctx.to_dict()

    @app.get("/agents/{agent_name}/context")
    async def get_agent_context(agent_name: str):
        """Get the saved continuation context for an agent."""
        if not agents.get(agent_name):
            raise HTTPException(404, f"Agent '{agent_name}' not found")
        ctx = agents.get_context(agent_name)
        if not ctx:
            return {"agent_name": agent_name, "context": None}
        return ctx.to_dict()

    @app.delete("/agents/{agent_name}/context")
    async def clear_agent_context(agent_name: str):
        """Clear the continuation context after consumption."""
        agents.clear_context(agent_name)
        return {"cleared": True}

    @app.post("/agents/{agent_name}/sleep")
    async def deep_sleep_agent(agent_name: str):
        """Put an agent into deep sleep — save context and close all sessions.

        The agent saves its current state and all sessions are closed.
        On next wake (manual or scheduled), context is restored.
        """
        agent = agents.get(agent_name)
        if not agent:
            raise HTTPException(404, f"Agent '{agent_name}' not found")

        streaming_closed = await _disconnect_streaming_sessions(agent_name)

        # Close legacy manager-backed sessions for this agent
        closed = 0
        for s in list(manager.list()):
            if s.agent_name == agent_name:
                manager.delete(s.id)
                closed += 1

        total_closed = streaming_closed + closed
        _log(f"api: agent {agent_name} entered deep sleep, closed {total_closed} session(s)")
        return {
            "agent": agent_name,
            "status": "sleeping",
            "sessions_closed": total_closed,
            "context_saved": agents.get_context(agent_name) is not None,
        }

    # ── Wake Trigger ───────────────────────────────────────

    @app.post("/agents/{agent_name}/wake")
    async def wake_agent(agent_name: str, prompt: str = ""):
        """Manually trigger a wake for an agent's streaming main session."""
        agent = agents.get(agent_name)
        if not agent:
            raise HTTPException(404, f"Agent '{agent_name}' not found")

        wake_prompt = prompt or "Manual wake trigger"
        _log(f"api: waking agent {agent_name} with prompt: {wake_prompt[:80]}...")

        ss = await _ensure_streaming_session(agent_name, label="main")
        if not ss:
            raise HTTPException(503, f"Failed to start streaming main session for '{agent_name}'")
        await ss.send(wake_prompt)

        return {
            "agent": agent_name,
            "session_id": ss.id,
            "sent": True,
            "connected": ss.is_connected,
        }

    async def _wake_callback(agent_name: str, session_id: str, prompt: str) -> None:
        """Callback for the scheduler/autonomy to wake an agent."""
        del session_id  # Streaming is now the canonical main runtime.
        ss = await _ensure_streaming_session(agent_name, label="main")
        if not ss:
            _log(f"scheduler: no streaming main session for {agent_name}, skipping wake")
            return
        await ss.send(prompt)
        _log(f"scheduler: woke {agent_name} via streaming main")

    async def _dream_callback(agent_name: str, agent_config) -> None:
        """Callback for the scheduler to run nightly dream consolidation."""
        _log(f"scheduler: triggering dream for '{agent_name}'")
        try:
            await dream_runner.run_dream(agent_name, agent_config)
        except Exception as e:
            _log(f"scheduler: dream run failed for '{agent_name}': {e}")

    scheduler = AgentScheduler(
        agents,
        wake_callback=_wake_callback,
        direct_send_callback=broker.send_callback,
        dream_callback=_dream_callback,
        streaming_sessions_fn=lambda: broker._streaming,
        comms_cleanup_fn=comms.cleanup_expired,
    )

    # Autonomy engine — self-directed work loops
    autonomy = AutonomyEngine(
        agents, tasks, store,
        session_sender=_wake_callback,
    )

    @app.on_event("startup")
    async def on_startup():
        """Start broker pollers, streaming sessions, scheduler, and autonomy."""
        auto_start_agents = agents.list_auto_start_agents()

        # Start broker pollers and streaming sessions for all enabled agents.
        from pinky_daemon.pollers import BrokerTelegramPoller
        from pinky_outreach.telegram import TelegramAdapter
        all_agents = agents.list(enabled_only=True)
        streaming_count = 0
        for agent in all_agents:
            token = agents.get_raw_token(agent.name, "telegram")
            if token:
                adapter = TelegramAdapter(token)
                poller = BrokerTelegramPoller(
                    adapter, agent.name, broker, registry=agents,
                )
                _broker_pollers.append(poller)
                asyncio.create_task(poller.start())
                _log(f"startup: broker poller started for {agent.name}")

            persisted = agents.list_streaming_session_ids(agent.name)
            labels_to_start = {entry["label"]: entry["session_id"] for entry in persisted if entry["session_id"]}
            labels_to_start.setdefault("main", agents.get_streaming_session_id(agent.name, label="main"))

            for label, resume_id in labels_to_start.items():
                try:
                    await _start_streaming_session(agent.name, label=label, resume_id=resume_id)
                    streaming_count += 1
                    if resume_id:
                        _log(f"startup: streaming session resumed for {agent.name}/{label} (session {resume_id[:12]})")
                    else:
                        _log(f"startup: streaming session connected for {agent.name}/{label} (new)")
                except Exception as e:
                    _log(f"startup: streaming session failed for {agent.name}/{label}: {e}")

        # Clean up legacy sessions for agents that now have streaming sessions.
        # These ghost sessions were restored by SessionManager._restore_sessions()
        # but are superseded by the streaming sessions created above.
        streaming_agents = set(broker._streaming.keys())
        legacy_purged = 0
        for s in manager.list():
            if s.agent_name and s.agent_name in streaming_agents:
                manager.delete(s.id)
                legacy_purged += 1
        if legacy_purged:
            _log(f"startup: purged {legacy_purged} legacy session(s) superseded by streaming")

        await scheduler.start()
        await autonomy.start()

        for agent in auto_start_agents:
            await autonomy.start_agent_loop(agent.name)

        _log(f"startup: scheduler + autonomy running, {len(auto_start_agents)} agent(s) auto-started, {len(_broker_pollers)} broker poller(s), {streaming_count} streaming")

    @app.on_event("shutdown")
    async def on_shutdown():
        """Stop scheduler, autonomy, broker pollers, and streaming sessions on shutdown."""
        # Disconnect streaming sessions
        for name in list(broker._streaming.keys()):
            sessions = broker._streaming.get(name, {})
            for label, ss in list(sessions.items()):
                try:
                    await ss.disconnect()
                except Exception:
                    pass
            broker.unregister_streaming(name)
        for poller in _broker_pollers:
            poller.stop()
        await autonomy.stop()
        await scheduler.stop()

    # ── Admin: Update & Restart ───────────────────────────

    @app.post("/admin/update")
    async def admin_update(branch: str = "main", dry_run: bool = False):
        """Pull latest code, rebuild if needed, and restart the daemon.

        The process manager (launchctl/systemd) must be installed for
        auto-restart. Without it, the daemon will stop and stay stopped.
        """
        import subprocess as sp

        repo_dir = str(Path(__file__).resolve().parent.parent.parent)

        # Current state
        try:
            before_hash = sp.check_output(
                ["git", "rev-parse", "--short", "HEAD"],
                cwd=repo_dir, stderr=sp.DEVNULL, timeout=10,
            ).decode().strip()
        except Exception:
            before_hash = "unknown"

        # Fetch
        try:
            sp.check_output(
                ["git", "fetch", "origin", branch],
                cwd=repo_dir, stderr=sp.STDOUT, timeout=30,
            )
        except sp.CalledProcessError as e:
            return {"error": f"git fetch failed: {e.output.decode()[:500]}"}

        # Preview mode — show pending commits
        if dry_run:
            try:
                pending = sp.check_output(
                    ["git", "log", "--oneline", f"HEAD..origin/{branch}"],
                    cwd=repo_dir, stderr=sp.DEVNULL, timeout=10,
                ).decode().strip()
            except Exception:
                pending = ""
            commits = [line for line in pending.splitlines() if line.strip()] if pending else []
            return {
                "dry_run": True,
                "current_hash": before_hash,
                "branch": branch,
                "pending_commits": len(commits),
                "commits": commits,
                "up_to_date": len(commits) == 0,
            }

        # Pull
        try:
            sp.check_output(
                ["git", "pull", "origin", branch],
                cwd=repo_dir, stderr=sp.STDOUT, timeout=60,
            )
        except sp.CalledProcessError as e:
            return {"error": f"git pull failed: {e.output.decode()[:500]}"}

        # After hash + commit summary
        try:
            after_hash = sp.check_output(
                ["git", "rev-parse", "--short", "HEAD"],
                cwd=repo_dir, stderr=sp.DEVNULL, timeout=10,
            ).decode().strip()
        except Exception:
            after_hash = "unknown"

        try:
            summary = sp.check_output(
                ["git", "log", "--oneline", f"{before_hash}..{after_hash}"],
                cwd=repo_dir, stderr=sp.DEVNULL, timeout=10,
            ).decode().strip()
        except Exception:
            summary = ""

        # Detect dependency changes
        deps_rebuilt = False
        try:
            changed = sp.check_output(
                ["git", "diff", "--name-only", before_hash, after_hash, "--", "pyproject.toml"],
                cwd=repo_dir, stderr=sp.DEVNULL, timeout=10,
            ).decode().strip()
            if changed:
                venv_pip = str(Path(repo_dir) / ".venv" / "bin" / "pip")
                if Path(venv_pip).exists():
                    sp.check_output(
                        [venv_pip, "install", "-e", ".[all]", "--quiet"],
                        cwd=repo_dir, stderr=sp.STDOUT, timeout=120,
                    )
                    deps_rebuilt = True
        except Exception:
            pass

        # Detect frontend changes
        frontend_rebuilt = False
        try:
            changed = sp.check_output(
                ["git", "diff", "--name-only", before_hash, after_hash, "--", "frontend-svelte/"],
                cwd=repo_dir, stderr=sp.DEVNULL, timeout=10,
            ).decode().strip()
            if changed:
                fe_dir = str(Path(repo_dir) / "frontend-svelte")
                if Path(fe_dir).exists():
                    sp.check_output(["npm", "install", "--silent"], cwd=fe_dir, stderr=sp.STDOUT, timeout=60)
                    sp.check_output(["npm", "run", "build"], cwd=fe_dir, stderr=sp.STDOUT, timeout=60)
                    frontend_rebuilt = True
        except Exception:
            pass

        result = {
            "updated": True,
            "before_hash": before_hash,
            "after_hash": after_hash,
            "commits": summary.splitlines() if summary else [],
            "deps_rebuilt": deps_rebuilt,
            "frontend_rebuilt": frontend_rebuilt,
            "restarting": before_hash != after_hash or deps_rebuilt,
        }

        # Schedule graceful restart if anything changed
        if result["restarting"]:
            import signal

            async def _delayed_exit():
                await asyncio.sleep(1)  # let response flush
                _log("admin: SIGTERM self for restart after update")
                os.kill(os.getpid(), signal.SIGTERM)

            asyncio.create_task(_delayed_exit())

        return result

    @app.post("/admin/restart")
    async def admin_restart():
        """Graceful daemon restart. Requires process manager for auto-restart."""
        import signal

        async def _delayed_exit():
            await asyncio.sleep(1)
            _log("admin: SIGTERM self for restart")
            os.kill(os.getpid(), signal.SIGTERM)

        asyncio.create_task(_delayed_exit())
        return {"restarting": True, "git_hash": _git_hash}

    # ── Scheduler Control ──────────────────────────────────

    @app.get("/scheduler/status")
    async def scheduler_status():
        """Get scheduler status."""
        all_schedules = agents.get_all_schedules(enabled_only=False)
        auto_start = agents.list_auto_start_agents()
        return {
            "running": scheduler.running,
            "total_schedules": len(all_schedules),
            "enabled_schedules": sum(1 for s in all_schedules if s.enabled),
            "auto_start_agents": [a.name for a in auto_start],
        }

    @app.get("/settings/heartbeat")
    async def get_heartbeat_settings():
        """Get heartbeat/wake/sleep settings for all agents."""
        all_agents = agents.list(include_retired=False)
        heartbeats = agents.get_all_latest_heartbeats()
        hb_map = {h.agent_name: h.to_dict() for h in heartbeats}
        all_schedules = agents.get_all_schedules(enabled_only=False)

        result = []
        for a in all_agents:
            agent_schedules = [s.to_dict() for s in all_schedules if s.agent_name == a.name]
            result.append({
                "name": a.name,
                "display_name": a.display_name or a.name,
                "enabled": a.enabled,
                "status": a.status,
                "heartbeat_interval": a.heartbeat_interval,
                "wake_interval": a.wake_interval,
                "clock_aligned": a.clock_aligned,
                "auto_sleep_hours": a.auto_sleep_hours,
                "latest_heartbeat": hb_map.get(a.name),
                "schedules": agent_schedules,
            })
        return {
            "agents": result,
            "scheduler_running": scheduler.running,
            "heartbeat_prompt": agents.get_heartbeat_prompt(),
        }

    @app.put("/settings/heartbeat/prompt")
    async def set_heartbeat_prompt(req: UpdateHeartbeatPromptRequest):
        """Update the global heartbeat wake prompt."""
        prompt = req.prompt.strip()
        if not prompt:
            raise HTTPException(400, "prompt is required")
        agents.set_heartbeat_prompt(prompt)
        return {"updated": True, "heartbeat_prompt": agents.get_heartbeat_prompt()}

    @app.get("/settings/owner-profile")
    async def get_owner_profile():
        """Get the owner/operator profile."""
        return agents.get_owner_profile()

    @app.put("/settings/owner-profile")
    async def set_owner_profile(req: OwnerProfileRequest):
        """Update owner profile. Only non-empty fields are written."""
        updates = {k: v for k, v in req.model_dump().items() if v}
        if not updates:
            return agents.get_owner_profile()
        return agents.set_owner_profile(updates)

    @app.get("/system/auth")
    async def get_auth_status():
        """Check Claude Code auth status. Detects Max/Pro login or API key."""
        import shutil
        import subprocess

        result = {
            "logged_in": False,
            "auth_method": None,
            "api_provider": None,
            "email": None,
            "subscription_type": None,
            "has_api_key": bool(os.environ.get("ANTHROPIC_API_KEY")),
            "claude_installed": bool(shutil.which("claude")),
            "setup_required": True,
        }

        if not result["claude_installed"]:
            result["setup_message"] = "Claude Code CLI not found. Install it first: npm install -g @anthropic-ai/claude-code"
            return result

        try:
            proc = subprocess.run(
                ["claude", "auth", "status"],
                capture_output=True, text=True, timeout=10,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                auth_data = json.loads(proc.stdout.strip())
                result["logged_in"] = auth_data.get("loggedIn", False)
                result["auth_method"] = auth_data.get("authMethod")
                result["api_provider"] = auth_data.get("apiProvider")
                result["email"] = auth_data.get("email")
                result["subscription_type"] = auth_data.get("subscriptionType")
                result["setup_required"] = not result["logged_in"] and not result["has_api_key"]
                if result["setup_required"]:
                    result["setup_message"] = "Not logged in. Run 'claude login' in your terminal to authenticate with your Anthropic account."
        except Exception as e:
            result["setup_message"] = f"Could not check auth status: {e}"

        return result

    @app.get("/settings/account")
    async def get_account_info():
        """Get account info and cumulative costs across all sessions (lifetime + current run)."""
        run_cost = 0.0
        run_costs = []
        account = {}

        for agent_name, sessions in broker._streaming.items():
            agent_cost = 0.0
            for label, ss in sessions.items():
                agent_cost += ss.usage.total_cost_usd
                if not account and ss.account_info:
                    account = ss.account_info
            run_costs.append({
                "name": agent_name,
                "cost_usd": round(agent_cost, 6),
                "sessions": len(sessions),
            })
            run_cost += agent_cost

        # Lifetime costs from DB
        lifetime_costs = agents.get_lifetime_costs()
        lifetime_total = agents.get_total_lifetime_cost()

        return {
            "account": account,
            "run_cost_usd": round(run_cost, 6),
            "lifetime_cost_usd": lifetime_total,
            "run_agents": run_costs,
            "lifetime_agents": lifetime_costs,
        }

    # ── Autonomy Engine ────────────────────────────────────

    @app.get("/autonomy/status")
    async def autonomy_status():
        """Get autonomy engine status — active loops, event queues."""
        return autonomy.get_status()

    @app.post("/autonomy/{agent_name}/start")
    async def start_agent_loop(agent_name: str):
        """Start the autonomous work loop for an agent."""
        agent = agents.get(agent_name)
        if not agent:
            raise HTTPException(404, f"Agent '{agent_name}' not found")
        await autonomy.start_agent_loop(agent_name)
        return {"agent": agent_name, "loop": "started"}

    @app.post("/autonomy/{agent_name}/stop")
    async def stop_agent_loop(agent_name: str):
        """Stop an agent's work loop."""
        await autonomy.stop_agent_loop(agent_name)
        return {"agent": agent_name, "loop": "stopped"}

    @app.post("/autonomy/{agent_name}/event")
    async def push_agent_event(agent_name: str, req: PushEventRequest):
        """Push an event to an agent's queue — triggers autonomous action.

        Event types: message_received, task_assigned, task_updated,
        schedule_wake, manual_wake, worker_report, alert
        """
        agent = agents.get(agent_name)
        if not agent:
            raise HTTPException(404, f"Agent '{agent_name}' not found")

        try:
            event_type = EventType(req.type)
        except ValueError:
            raise HTTPException(400, f"Invalid event type: {req.type}")

        event = AgentEvent(
            type=event_type,
            agent_name=agent_name,
            data=req.data,
            priority=req.priority,
        )
        await autonomy.push_event(event)
        return {
            "agent": agent_name,
            "event": req.type,
            "queued": True,
            "pending": autonomy.event_queue.pending_count(agent_name),
        }

    # ── Audit & Hooks ──────────────────────────────────────

    @app.get("/audit")
    async def get_audit_log(
        agent_name: str = "", session_id: str = "",
        event: str = "", limit: int = 50,
    ):
        """Query the audit trail."""
        entries = audit.get_log(
            agent_name=agent_name, session_id=session_id,
            event=event, limit=limit,
        )
        return {"entries": [e.to_dict() for e in entries], "count": len(entries)}

    @app.get("/audit/costs")
    async def get_audit_costs(agent_name: str = "", session_id: str = ""):
        """Get cost summary from audit trail."""
        return audit.get_costs(agent_name=agent_name, session_id=session_id)

    @app.get("/hooks")
    async def list_hooks():
        """List all registered hooks."""
        return hooks.list_hooks()

    @app.get("/activity")
    async def get_activity_feed(limit: int = 50, since: float = 0.0):
        """Get live activity feed from hooks.

        Poll this endpoint for real-time agent activity.
        Use 'since' param with the last timestamp to get only new events.
        """
        feed = hooks.get_activity_feed(limit=limit, since=since)
        return {"events": feed, "count": len(feed)}

    @app.get("/activity/active")
    async def get_active_agents():
        """Get currently active agents and what they're doing."""
        active = hooks.get_active_agents()
        return {"agents": active, "count": len(active)}

    # ── Task/Project Management ──────────────────────────

    @app.get("/tasks-ui", response_class=HTMLResponse)
    async def tasks_ui():
        return _serve_spa_or_html("tasks.html")

    # Projects

    @app.post("/projects")
    async def create_project(req: CreateProjectRequest):
        project = tasks.create_project(req.name, description=req.description)
        return project.to_dict()

    @app.get("/projects")
    async def list_projects(include_archived: bool = False):
        projects = tasks.list_projects(include_archived=include_archived)
        return {"projects": [p.to_dict() for p in projects], "count": len(projects)}

    @app.get("/projects/{project_id}")
    async def get_project(project_id: int):
        project = tasks.get_project(project_id)
        if not project:
            raise HTTPException(404, "Project not found")
        project_tasks = tasks.list(project_id=project_id, include_completed=True)
        return {
            "project": project.to_dict(),
            "tasks": [t.to_dict() for t in project_tasks],
            "task_count": len(project_tasks),
        }

    @app.put("/projects/{project_id}")
    async def update_project(project_id: int, req: CreateProjectRequest):
        project = tasks.update_project(project_id, name=req.name, description=req.description)
        if not project:
            raise HTTPException(404, "Project not found")
        return project.to_dict()

    @app.delete("/projects/{project_id}")
    async def delete_project(project_id: int):
        if not tasks.delete_project(project_id):
            raise HTTPException(404, "Project not found")
        return {"deleted": True}

    # Tasks

    @app.post("/tasks")
    async def create_task(req: CreateTaskRequest):
        task = tasks.create(
            req.title,
            project_id=req.project_id,
            description=req.description,
            status=req.status,
            priority=req.priority,
            assigned_agent=req.assigned_agent,
            created_by=req.created_by,
            tags=req.tags,
            due_date=req.due_date,
            parent_id=req.parent_id,
            blocked_by=req.blocked_by,
        )

        # Push event to assigned agent's autonomy loop
        if task.assigned_agent:
            priority = 2 if task.priority == "urgent" else 1 if task.priority == "high" else 0
            await autonomy.push_event(AgentEvent(
                type=EventType.task_assigned,
                agent_name=task.assigned_agent,
                data={"task_id": task.id, "title": task.title, "priority": task.priority},
                priority=priority,
            ))

        return task.to_dict()

    @app.get("/tasks")
    async def list_tasks(
        status: str = "",
        assigned_agent: str = "",
        priority: str = "",
        tag: str = "",
        project_id: int | None = None,
        include_completed: bool = False,
        limit: int = 100,
    ):
        task_list = tasks.list(
            status=status, assigned_agent=assigned_agent,
            priority=priority, tag=tag, project_id=project_id,
            include_completed=include_completed, limit=limit,
        )
        return {"tasks": [t.to_dict() for t in task_list], "count": len(task_list)}

    @app.get("/tasks/stats")
    async def task_stats():
        by_status = tasks.count_by_status()
        by_agent = tasks.count_by_agent()
        return {"by_status": by_status, "by_agent": by_agent}

    # Task self-service — must be before /tasks/{task_id} to avoid route conflict

    @app.get("/tasks/next")
    async def next_task(agent_name: str = "", priority: str = ""):
        """Get the next available task for an agent to work on."""
        if agent_name:
            my_tasks = tasks.list(assigned_agent=agent_name, status="pending")
            if my_tasks:
                return {"task": my_tasks[0].to_dict(), "source": "assigned"}
            my_tasks = tasks.list(assigned_agent=agent_name, status="in_progress")
            if my_tasks:
                return {"task": my_tasks[0].to_dict(), "source": "in_progress"}
        unassigned = tasks.list(assigned_agent="")
        unassigned = [t for t in unassigned if not t.assigned_agent]
        if unassigned:
            return {"task": unassigned[0].to_dict(), "source": "unassigned"}
        return {"task": None, "source": "none"}

    @app.get("/tasks/{task_id}")
    async def get_task(task_id: int):
        task = tasks.get(task_id)
        if not task:
            raise HTTPException(404, "Task not found")
        subtasks = tasks.get_subtasks(task_id)
        comments = tasks.get_comments(task_id)
        return {
            "task": task.to_dict(),
            "subtasks": [s.to_dict() for s in subtasks],
            "comments": [c.to_dict() for c in comments],
        }

    @app.put("/tasks/{task_id}")
    async def update_task(task_id: int, req: UpdateTaskRequest):
        kwargs = {k: v for k, v in req.model_dump().items() if v is not None}
        task = tasks.update(task_id, **kwargs)
        if not task:
            raise HTTPException(404, "Task not found")
        return task.to_dict()

    @app.delete("/tasks/{task_id}")
    async def delete_task(task_id: int):
        if not tasks.delete(task_id):
            raise HTTPException(404, "Task not found")
        return {"deleted": True}

    @app.post("/tasks/claim/{task_id}")
    async def claim_task(task_id: int, agent_name: str = ""):
        """Agent claims an unassigned task. Sets status to in_progress."""
        task = tasks.get(task_id)
        if not task:
            raise HTTPException(404, "Task not found")
        if task.assigned_agent and task.assigned_agent != agent_name:
            raise HTTPException(409, f"Task already assigned to {task.assigned_agent}")

        updated = tasks.update(task_id, assigned_agent=agent_name, status="in_progress")
        tasks.add_comment(task_id, agent_name, "Claimed and started work")
        return updated.to_dict()

    @app.post("/tasks/complete/{task_id}")
    async def complete_task(task_id: int, agent_name: str = "", summary: str = ""):
        """Agent marks a task as completed with an optional summary."""
        task = tasks.get(task_id)
        if not task:
            raise HTTPException(404, "Task not found")

        updated = tasks.update(task_id, status="completed")
        comment = summary or "Task completed"
        tasks.add_comment(task_id, agent_name or task.assigned_agent, comment)

        # Push worker_report event to parent agent if task has a creator
        if task.created_by and task.created_by != agent_name:
            await autonomy.push_event(AgentEvent(
                type=EventType.worker_report,
                agent_name=task.created_by,
                data={
                    "task_id": task.id,
                    "title": task.title,
                    "worker": agent_name or task.assigned_agent,
                    "result": summary,
                },
                priority=1,
            ))

        return updated.to_dict()

    @app.post("/tasks/block/{task_id}")
    async def block_task(task_id: int, agent_name: str = "", reason: str = ""):
        """Agent marks a task as blocked with a reason."""
        task = tasks.get(task_id)
        if not task:
            raise HTTPException(404, "Task not found")

        updated = tasks.update(task_id, status="blocked")
        tasks.add_comment(task_id, agent_name or task.assigned_agent, f"Blocked: {reason}")
        return updated.to_dict()

    # Self-monitoring endpoints

    @app.get("/agents/{agent_name}/health")
    async def agent_health(agent_name: str):
        """Comprehensive health check for an agent.

        Returns: session status, context usage, heartbeat health,
        task workload, autonomy loop status, error rate.
        """
        agent = agents.get(agent_name)
        if not agent:
            raise HTTPException(404, f"Agent '{agent_name}' not found")

        session_info = await _streaming_health_info(agent_name, label="main")
        legacy_session = None
        if not session_info:
            main_session = manager.get(f"{agent_name}-main")
            if main_session:
                session_info = {
                    "id": main_session.id,
                    "state": main_session.state.value,
                    "context_used_pct": main_session.context_used_pct,
                    "message_count": main_session.message_count,
                    "needs_restart": main_session.needs_restart,
                    "streaming": False,
                }
        else:
            main_session = manager.get(f"{agent_name}-main")
            if main_session:
                legacy_session = {
                    "id": main_session.id,
                    "state": main_session.state.value,
                    "context_used_pct": main_session.context_used_pct,
                    "message_count": main_session.message_count,
                    "needs_restart": main_session.needs_restart,
                    "streaming": False,
                }

        # Heartbeat
        hb = agents.get_latest_heartbeat(agent_name)
        heartbeat_info = None
        if hb:
            age = time.time() - hb.timestamp
            heartbeat_info = {
                "status": hb.status,
                "age_seconds": int(age),
                "healthy": age < (agent.heartbeat_interval * 2) if agent.heartbeat_interval else True,
            }

        # Task workload
        agent_tasks = tasks.list(assigned_agent=agent_name)
        task_info = {
            "total_active": len(agent_tasks),
            "pending": sum(1 for t in agent_tasks if t.status == "pending"),
            "in_progress": sum(1 for t in agent_tasks if t.status == "in_progress"),
            "blocked": sum(1 for t in agent_tasks if t.status == "blocked"),
        }

        # Autonomy status
        autonomy_info = autonomy.get_status()
        loop_active = agent_name in autonomy_info.get("active_loops", [])
        pending_events = autonomy.event_queue.pending_count(agent_name)

        # Recent errors from audit
        errors = audit.get_log(agent_name=agent_name, event="error", limit=5)

        # Cost
        cost_info = audit.get_costs(agent_name=agent_name)

        return {
            "agent": agent_name,
            "enabled": agent.enabled,
            "role": agent.role,
            "session": session_info,
            "legacy_session": legacy_session,
            "heartbeat": heartbeat_info,
            "tasks": task_info,
            "autonomy": {
                "loop_active": loop_active,
                "pending_events": pending_events,
            },
            "costs": cost_info,
            "recent_errors": [e.to_dict() for e in errors],
            "recommendation": _health_recommendation(session_info, heartbeat_info, task_info, errors),
        }

    def _health_recommendation(session, heartbeat, tasks, errors) -> str:
        """Generate a health recommendation based on metrics."""
        issues = []
        if session and session.get("needs_restart"):
            issues.append("context_full")
        if session and session.get("state") == "error":
            issues.append("session_error")
        if heartbeat and not heartbeat.get("healthy"):
            issues.append("heartbeat_stale")
        if tasks.get("blocked", 0) > 0:
            issues.append("tasks_blocked")
        if len(errors) >= 3:
            issues.append("high_error_rate")

        if not issues:
            return "healthy"
        if "context_full" in issues:
            return "needs_restart"
        if "session_error" in issues:
            return "needs_attention"
        if "high_error_rate" in issues:
            return "unstable"
        return "degraded"

    # time is imported at module level

    # Task comments

    @app.post("/tasks/{task_id}/comments")
    async def add_comment(task_id: int, req: AddCommentRequest):
        if not tasks.get(task_id):
            raise HTTPException(404, "Task not found")
        comment = tasks.add_comment(task_id, req.author, req.content)
        return comment.to_dict()

    @app.get("/tasks/{task_id}/comments")
    async def get_comments(task_id: int, limit: int = 50):
        comments = tasks.get_comments(task_id, limit=limit)
        return {"comments": [c.to_dict() for c in comments], "count": len(comments)}

    @app.delete("/tasks/{task_id}/comments/{comment_id}")
    async def delete_comment(task_id: int, comment_id: int):
        if not tasks.delete_comment(comment_id):
            raise HTTPException(404, "Comment not found")
        return {"deleted": True}

    # ── Memory Browsing Endpoints ──────────────────────────

    def _get_memory_store(agent_name: str) -> "ReflectionStore":
        """Get the memory store for an agent. Opens the DB at {working_dir}/data/memory.db."""
        if ReflectionStore is None:
            raise HTTPException(501, "pinky_memory is not installed")
        from pinky_memory.store import ReflectionStore as _RS
        agent = agents.get(agent_name)
        if not agent:
            raise HTTPException(404, f"Agent '{agent_name}' not found")
        db_path = str(Path(agent.working_dir) / "data" / "memory.db")
        if not Path(db_path).exists():
            raise HTTPException(404, f"No memory database for agent '{agent_name}'")
        return _RS(db_path=db_path)

    def _reflection_to_dict(r) -> dict:
        """Serialize a Reflection to a JSON-safe dict (omit embedding)."""
        return {
            "id": r.id,
            "type": r.type.value,
            "content": r.content,
            "context": r.context,
            "project": r.project,
            "salience": r.salience,
            "active": r.active,
            "no_recall": r.no_recall,
            "supersedes": r.supersedes,
            "superseded_by": r.superseded_by,
            "event_date": r.event_date,
            "entities": r.entities,
            "source_session_id": r.source_session_id,
            "source_channel": r.source_channel,
            "source_message_ids": r.source_message_ids,
            "created_at": r.created_at.isoformat(),
            "accessed_at": r.accessed_at.isoformat(),
            "access_count": r.access_count,
            "weight": round(r.weight, 4),
            "next_review_date": r.next_review_date,
            "review_interval_days": r.review_interval_days,
        }

    @app.get("/agents/{agent_name}/memories")
    async def list_memories(
        agent_name: str,
        type: str = "",
        project: str = "",
        entity: str = "",
        salience_min: int | None = None,
        salience_max: int | None = None,
        active: bool = True,
        sort_by: str = "created_at",
        sort_dir: str = "desc",
        limit: int = 50,
        offset: int = 0,
    ):
        """List/filter memories for an agent."""
        store = _get_memory_store(agent_name)
        filters = MemoryQueryFilters(
            type=type or None,
            project=project or None,
            entity=entity or None,
            salience_min=salience_min,
            salience_max=salience_max,
            active=active,
            sort_by=sort_by,
            sort_dir=sort_dir,
            limit=min(limit, 100),
            offset=offset,
        )
        results, total = store.query(filters)
        store.close()
        return {
            "memories": [_reflection_to_dict(r) for r in results],
            "total": total,
            "limit": filters.limit,
            "offset": filters.offset,
        }

    @app.get("/agents/{agent_name}/memories/search")
    async def search_memories(agent_name: str, q: str = "", limit: int = 20):
        """Keyword search across an agent's memories."""
        if not q:
            raise HTTPException(400, "Query parameter 'q' is required")
        store = _get_memory_store(agent_name)
        results = store.search_by_keyword(q, limit=min(limit, 50))
        store.close()
        return {
            "memories": [_reflection_to_dict(r) for r in results],
            "query": q,
            "count": len(results),
        }

    @app.get("/agents/{agent_name}/chat-history")
    async def search_agent_chat_history(
        agent_name: str, q: str = "", limit: int = 50,
        after: str = "", before: str = "", role: str = "",
    ):
        """Search an agent's chat history across all their sessions.

        Args:
            q: Full-text search query (optional).
            limit: Max results.
            after: ISO date (YYYY-MM-DD) — only messages after this date.
            before: ISO date (YYYY-MM-DD) — only messages before this date.
            role: Filter by role (user, assistant).
        """
        from datetime import datetime, timezone
        agent = agents.get(agent_name)
        if not agent:
            raise HTTPException(404, f"Agent '{agent_name}' not found")

        # Parse date filters to timestamps
        after_ts = 0.0
        before_ts = 0.0
        if after:
            try:
                after_ts = datetime.strptime(after, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()
            except ValueError:
                raise HTTPException(400, "Invalid 'after' date format. Use YYYY-MM-DD.")
        if before:
            try:
                # End of day
                before_ts = (datetime.strptime(before, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()) + 86400
            except ValueError:
                raise HTTPException(400, "Invalid 'before' date format. Use YYYY-MM-DD.")

        # Build set of session IDs belonging to this agent from both
        # active sessions AND the persistent conversation store
        agent_session_ids = _collect_agent_session_ids(agent_name)

        results = []
        if q:
            # Search globally then filter to agent's sessions
            try:
                all_results = store.search(q, limit=limit * 3)
                results = [m for m in all_results if m.session_id in agent_session_ids or m.session_id.startswith(f"{agent_name}-")]
            except Exception:
                pass
        else:
            results = [
                SimpleNamespace(**m)
                for m in _resolve_agent_history(
                    agent_name,
                    after_ts=after_ts,
                    before_ts=before_ts,
                    limit=limit,
                    role=role,
                )
            ]

        if q:
            # Apply date filters after search results
            if after_ts:
                results = [m for m in results if m.timestamp >= after_ts]
            if before_ts:
                results = [m for m in results if m.timestamp <= before_ts]
            if role:
                results = [m for m in results if m.role == role]
            results.sort(key=lambda m: m.timestamp, reverse=True)
            results = results[:limit]

        return {
            "messages": [
                {
                    "id": m.id,
                    "session_id": m.session_id,
                    "role": m.role,
                    "content": m.content[:500],
                    "timestamp": m.timestamp,
                    "platform": m.platform,
                    "duration_ms": getattr(m, "duration_ms", 0),
                }
                for m in results
            ],
            "query": q,
            "count": len(results),
            "sessions_searched": len(agent_session_ids),
        }

    @app.get("/agents/{agent_name}/memories/stats")
    async def memory_stats(agent_name: str, timeframe: str = "all"):
        """Get memory statistics for an agent."""
        store = _get_memory_store(agent_name)
        stats = store.introspect(timeframe=timeframe)
        store.close()
        return stats

    @app.get("/agents/{agent_name}/memories/{memory_id}")
    async def get_memory(agent_name: str, memory_id: str):
        """Get a single memory by ID."""
        store = _get_memory_store(agent_name)
        reflection = store.get(memory_id)
        store.close()
        if not reflection:
            raise HTTPException(404, f"Memory '{memory_id}' not found")
        return _reflection_to_dict(reflection)

    @app.get("/agents/{agent_name}/memories/{memory_id}/links")
    async def get_memory_links(agent_name: str, memory_id: str):
        """Get linked memories for a reflection."""
        store = _get_memory_store(agent_name)
        links = store.get_links(memory_id)
        # Also fetch the linked reflections themselves
        linked_memories = []
        for link in links:
            target = store.get(link.target_id)
            if target:
                linked_memories.append({
                    "similarity": round(link.similarity, 3),
                    "memory": _reflection_to_dict(target),
                })
        store.close()
        return {"links": linked_memories, "count": len(linked_memories)}

    # ── SSE Streaming Endpoints ───────────────────────────

    @app.get("/sessions/{session_id}/stream")
    async def stream_session(session_id: str):
        """SSE endpoint for streaming session output.

        Keeps connection alive with heartbeats. Full streaming
        will be wired when SDK runner supports it.
        """
        session = manager.get(session_id)
        if not session:
            raise HTTPException(404, f"Session '{session_id}' not found")

        async def event_generator():
            while True:
                yield f"data: {json.dumps({'type': 'heartbeat', 'session_id': session_id, 'state': session.state.value, 'context_used_pct': round(session.context_used_pct, 1)})}\n\n"
                await asyncio.sleep(5)

        return StreamingResponse(event_generator(), media_type="text/event-stream")

    @app.get("/activity/stream")
    async def stream_activity():
        """SSE endpoint for real-time activity feed.

        Polls the hook manager's activity store for new events.
        Bridge pattern — replace with proper event bus later.
        """
        async def event_generator():
            last_check = time.time()
            while True:
                await asyncio.sleep(2)
                now = time.time()
                # Get new activity since last check
                feed = hooks.get_activity_feed(limit=20, since=last_check)
                if feed:
                    for event in feed:
                        yield f"data: {json.dumps(event)}\n\n"
                else:
                    yield f"data: {json.dumps({'type': 'ping', 'timestamp': now})}\n\n"
                last_check = now

        return StreamingResponse(event_generator(), media_type="text/event-stream")

    # ── Research Pipeline Endpoints ──────────────────────────

    @app.post("/research")
    async def create_research_topic(req: CreateResearchRequest):
        topic = research.create_topic(
            title=req.title, description=req.description,
            submitted_by=req.submitted_by, priority=req.priority,
            tags=req.tags, scope=req.scope,
        )
        return topic.to_dict()

    @app.get("/research")
    async def list_research_topics(status: str = "", limit: int = 50, offset: int = 0):
        topics = research.list_topics(status=status or None, limit=limit, offset=offset)
        return {"topics": [t.to_dict() for t in topics], "count": len(topics)}

    @app.get("/research/stats")
    async def research_stats():
        return research.get_stats()

    @app.get("/research/{topic_id}")
    async def get_research_topic(topic_id: int):
        detail = research.get_topic_detail(topic_id)
        if not detail:
            raise HTTPException(404, "Topic not found")
        return detail

    @app.put("/research/{topic_id}")
    async def update_research_topic(topic_id: int, req: UpdateResearchRequest):
        updates = {k: v for k, v in req.model_dump().items() if v is not None}
        topic = research.update_topic(topic_id, **updates)
        if not topic:
            raise HTTPException(404, "Topic not found")
        return topic.to_dict()

    @app.post("/research/{topic_id}/assign")
    async def assign_research(topic_id: int, req: AssignResearchRequest):
        topic = research.assign_topic(topic_id, req.agent_name)
        if not topic:
            raise HTTPException(404, "Topic not found")
        return topic.to_dict()

    @app.post("/research/{topic_id}/brief")
    async def submit_research_brief(topic_id: int, req: SubmitBriefRequest):
        brief = research.submit_brief(
            topic_id=topic_id, author_agent=req.author_agent,
            content=req.content, summary=req.summary,
            sources=req.sources, key_findings=req.key_findings,
        )
        return brief.to_dict()

    @app.get("/research/{topic_id}/briefs")
    async def list_research_briefs(topic_id: int):
        briefs = research.get_briefs(topic_id)
        return {"briefs": [b.to_dict() for b in briefs], "count": len(briefs)}

    @app.post("/research/{topic_id}/reviews")
    async def submit_research_review(topic_id: int, req: SubmitReviewRequest):
        review = research.submit_review(
            brief_id=req.brief_id, topic_id=topic_id,
            reviewer_agent=req.reviewer_agent, verdict=req.verdict,
            comments=req.comments, confidence=req.confidence,
            suggested_additions=req.suggested_additions,
            corrections=req.corrections,
        )
        return review.to_dict()

    @app.get("/research/{topic_id}/reviews")
    async def list_research_reviews(topic_id: int):
        reviews = research.get_reviews(topic_id=topic_id)
        return {"reviews": [r.to_dict() for r in reviews], "count": len(reviews)}

    @app.post("/research/{topic_id}/publish")
    async def publish_research(topic_id: int):
        topic = research.publish_topic(topic_id)
        if not topic:
            raise HTTPException(404, "Topic not found")
        return topic.to_dict()

    @app.get("/research/{topic_id}/export")
    async def export_research(topic_id: int, format: str = "md"):
        """Export a research brief as MD or HTML file download."""
        detail = research.get_topic_detail(topic_id)
        if not detail:
            raise HTTPException(404, "Topic not found")
        briefs = detail.get("briefs", [])
        if not briefs:
            raise HTTPException(404, "No briefs found for this topic")
        brief = briefs[-1]  # Latest version
        reviews = detail.get("reviews", [])
        topic_data = detail["topic"]

        if format == "pdf":
            path = export_brief_pdf(topic_data, brief, reviews)
            return FileResponse(
                path,
                media_type="application/pdf",
                filename=os.path.basename(path),
            )
        elif format == "html":
            path = export_brief_html(topic_data, brief, reviews)
            return FileResponse(
                path,
                media_type="text/html",
                filename=os.path.basename(path),
            )
        else:
            path = export_brief_markdown(topic_data, brief, reviews)
            return FileResponse(
                path,
                media_type="text/markdown",
                filename=os.path.basename(path),
            )

    @app.get("/research/{topic_id}/export/content")
    async def export_research_content(topic_id: int, format: str = "md"):
        """Get export content inline (not as file download)."""
        detail = research.get_topic_detail(topic_id)
        if not detail:
            raise HTTPException(404, "Topic not found")
        briefs = detail.get("briefs", [])
        if not briefs:
            raise HTTPException(404, "No briefs found for this topic")
        brief = briefs[-1]
        reviews = detail.get("reviews", [])
        topic_data = detail["topic"]

        content = get_export_content_markdown(topic_data, brief, reviews)
        return {"content": content, "format": format, "topic_id": topic_id}

    # ── General PDF Rendering ─────────────────────────────

    @app.post("/render/pdf")
    async def render_pdf(req: dict):
        """Render markdown content as a PDF file.

        Returns the file path for use with pinky-messaging's send_document.
        """
        content = req.get("content", "").strip()
        filename = req.get("filename", "document.pdf")
        title = req.get("title", "Document")
        if not content:
            raise HTTPException(400, "content is required")

        from pinky_daemon.research_export import _HTML_TEMPLATE, EXPORT_DIR, _markdown_to_html

        html_body = _markdown_to_html(content)
        html = _HTML_TEMPLATE.format(title=title, body=html_body)

        os.makedirs(EXPORT_DIR, exist_ok=True)
        if not filename.endswith(".pdf"):
            filename += ".pdf"
        path = os.path.join(EXPORT_DIR, filename)

        try:
            from weasyprint import HTML as WP_HTML
            WP_HTML(string=html).write_pdf(path)
        except ImportError:
            raise HTTPException(503, "WeasyPrint not installed — cannot render PDFs")
        except Exception as e:
            raise HTTPException(500, f"PDF rendering failed: {e}")

        return {"success": True, "path": os.path.abspath(path), "filename": filename}

    return app
