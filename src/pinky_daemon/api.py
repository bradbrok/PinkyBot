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
import hashlib
import hmac
import json
import os
import re
import sys
import tempfile
import time
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Literal

from fastapi import (
    FastAPI,
    HTTPException,
    Request,
    Response,
    UploadFile,
    WebSocket,
)
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles

from pinky_daemon.activity_store import ActivityStore
from pinky_daemon.agent_comms import AgentComms
from pinky_daemon.agent_registry import AgentRegistry
from pinky_daemon.analytics_store import AnalyticsStore
from pinky_daemon.api_models import (
    AddDirectiveRequest,
    AddMcpServerRequest,
    AddScheduleRequest,
    AgentMessageRequest,
    AgentStatusRequest,
    ApproveUserRequest,
    AssignSkillRequest,
    AuthLoginRequest,
    AuthSetupRequest,
    CloneWorkerRequest,
    ContextResponse,
    ConversationListResponse,
    CreateGroupRequest,
    CreateSessionRequest,
    EffortDriftRequest,
    FederationPeerUpsertRequest,
    ForceRestartAgentRequest,
    ForkSessionRequest,
    HistoryResponse,
    JoinGroupRequest,
    MeshAllowlistEntryRequest,
    MeshAllowlistSetRequest,
    MeshSendRequest,
    MessageResponse,
    OwnerProfileRequest,
    PeerFleetAclEntryRequest,
    PeerFleetAclSetRequest,
    PushEventRequest,
    RecordHeartbeatRequest,
    RegisterAgentRequest,
    RestartResponse,
    SearchResponse,
    SendAgentMessageRequest,
    SendMessageRequest,
    SessionResponse,
    SetAgentTokenRequest,
    SetContextRequest,
    SetDefaultProviderRequest,
    SetMainAgentRequest,
    SetModelRequest,
    SpawnSessionRequest,
    TransportStopFailureRequest,
    TransportToolResultRequest,
    TransportToolUseRequest,
    TransportTranscriptPathRequest,
    TransportWakeRequest,
    UpdateAgentRequest,
    UpdateHeartbeatPromptRequest,
    UpdateMcpServerRequest,
    UpdatePasswordRequest,
)
from pinky_daemon.app_store import AppStore
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
from pinky_daemon.effort import CLI_EFFORT_LEVELS, EFFORT_LEVELS
from pinky_daemon.hooks import (
    AuditStore,
    HookEvent,
    HookManager,
    create_context_save_hook,
    create_cost_tracker_hook,
    create_heartbeat_hook,
    create_typing_indicator_hook,
)
from pinky_daemon.kb_store import KBStore
from pinky_daemon.librarian_runner import LibrarianRunner
from pinky_daemon.mesh_store import MeshStore
from pinky_daemon.outreach_config import OutreachConfigStore
from pinky_daemon.plugin_manager import PluginManager
from pinky_daemon.presentation_store import PresentationStore
from pinky_daemon.research_store import ResearchStore
from pinky_daemon.scheduler import AgentScheduler
from pinky_daemon.session_store import SessionEventStore, SessionStore
from pinky_daemon.session_watchdog import (
    SessionWatchdog,
    WatchdogConfig,
    compute_mcp_checkable,
)
from pinky_daemon.sessions import (
    SessionManager,
    SessionState,
    is_purgeable_legacy_session,
)
from pinky_daemon.shared_mcp import (
    SHARED_MCP_HOST,
    SHARED_MCP_PORT,
    SharedMcpManager,
    get_gateway_epoch,
    get_probe_request,
    get_probe_status,
    record_probe_request,
)
from pinky_daemon.skill_loader import discover_all_skills, register_discovered_skills
from pinky_daemon.skill_store import SkillStore
from pinky_daemon.task_store import TaskStore

# Alias: pinky_daemon.sessions.SessionState (imported above) is the
# harness/agent-SDK lifecycle enum (running/closed); the import below is the
# transport-layer lifecycle (CONNECTED/RECONNECTING/IDLE_SLEEPING/DEAD/
# UNINITIALIZED) from transport_state.py. The alias avoids the name collision
# while keeping both enums accessible.
from pinky_daemon.transport_state import SessionState as TransportSessionState
from pinky_daemon.trigger_store import TriggerStore
from pinky_daemon.wake_prompt import WakeReason

# Feature flag: shared MCP mode uses a single HTTP/SSE server instead of per-agent stdio
SHARED_MCP_ENABLED = os.environ.get("PINKY_SHARED_MCP", "0") == "1"

try:
    from pinky_memory.store import ReflectionStore
    from pinky_memory.types import MemoryQueryFilters
except ImportError:
    ReflectionStore = None  # type: ignore[assignment, misc]
    MemoryQueryFilters = None  # type: ignore[assignment, misc]


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _agent_is_dreamer(agent) -> bool:
    """True iff ``agent`` holds the privileged ``dreamer`` role (#145).

    Role-only by design: the Dreamer (Mora) is a single privileged identity,
    not a class. Group membership does NOT grant cross-agent memory access —
    treating "any agent in a group named 'dreamer'" as authorized would let a
    group assignment silently confer read-all + write-all over every agent's
    memory. (Both #624 reviewers flagged the looser predicate.) See the TRUST
    STATEMENT in ``pinky_memory.server`` for why that reach is concentrated.
    Default-deny: ``None`` (unknown/unregistered agent) → False.
    """
    if agent is None:
        return False
    return getattr(agent, "role", "") == "dreamer"


# Cold-start connect umbrella (#110). `_start_streaming_session` registers a
# session in `broker._streaming` only AFTER `ss.connect()` returns, and the
# SessionWatchdog samples only `broker._streaming` — so a cold start that
# wedges *inside* connect() (BOOTING, before registration) is invisible to the
# watchdog and would hang the start path indefinitely. This bound caps that
# wedge at the start site. It sits above tmux's 60s spawn bound
# (`tmux_session._COLD_START_TIMEOUT_SEC`) and well below the 4/6-min
# transition watchdog, so a legitimately slow SDK handshake isn't force-killed
# mid-flight. Scope is the *cold start* in `_start_streaming_session` only —
# `_ensure_streaming_session`'s reconnect path acts on an already-registered
# session, which the transition watchdog can already observe.
COLD_START_CONNECT_TIMEOUT_SEC = float(
    os.environ.get("PINKY_COLD_START_CONNECT_TIMEOUT_SEC", "120")
)


async def _bounded_cold_start_connect(
    ss,
    *,
    agent_name: str,
    label: str,
    timeout: float = COLD_START_CONNECT_TIMEOUT_SEC,
) -> None:
    """Run ``ss.connect()`` under a cold-start umbrella timeout (#110).

    On timeout, ``asyncio.wait_for`` cancels the in-flight ``connect()``. For a
    wedge still in BOOTING, ``connect()``'s own ``BaseException`` handler drives
    BOOT_FAILED → DEAD. But a cancel can also land *after* BOOT_COMPLETE (state
    already CONNECTED) yet *before* the caller registers the session —
    bypassing that handler and leaving an unregistered-but-connected client. So
    we always best-effort ``disconnect()`` before re-raising; the caller must
    treat any raise as "do NOT register this session".
    """
    try:
        await asyncio.wait_for(ss.connect(), timeout=timeout)
    except asyncio.TimeoutError:
        _log(
            f"streaming-start: cold start timed out for {agent_name}/{label} "
            f"after {timeout:.0f}s — discarding unregistered session"
        )
        try:
            await ss.disconnect()
        except Exception as de:  # best-effort cleanup; never mask the timeout
            _log(
                f"streaming-start: post-timeout disconnect failed for "
                f"{agent_name}/{label}: {de}"
            )
        raise


# ── Request/Response Models ──────────────────────────────────


VALID_PERMISSION_MODES = ("", "default", "acceptEdits", "bypassPermissions", "dontAsk", "plan", "auto")
MIN_UI_PASSWORD_LENGTH = 8
CONTEXT_SAVE_SOURCE_SELF_TOOL = "save_my_context"
CONTEXT_ACTIVITY_SAVE_BUFFER_SECONDS = 5 * 60
CONTEXT_STALE_WARNING_SECONDS = 12 * 60 * 60
FRONTEND_BUILD_MANIFEST = "build-manifest.json"

# StopFailure hook (CC typed turn-failure signal). Auth-class failures are
# routed into the shared AuthFailureTracker so tmux/CLI sessions get the same
# proactive operator alert (threshold + cooldown + operator resolution) the
# SDK reader loop already triggers; other classes (rate_limit, billing_error,
# server_error, …) are logged for observability. oauth_org_not_allowed is
# auth-adjacent — same re-auth remedy — so it rides the auth path too.
_STOP_FAILURE_AUTH_TYPES = frozenset(
    {"authentication_failed", "oauth_org_not_allowed"}
)

# Outreach attempt outcome buckets (task #81 / issue #395 follow-up).
# Splits the previous free-form "ok"/"error" outcome into 4 typed buckets so
# dashboards can distinguish caller-side validation failures from real
# upstream platform errors from genuinely unexpected internal exceptions.
#   - success         — adapter call returned a message_id (the send worked)
#   - rejected        — caller-side validation failed (HTTPException raised
#                       by our code due to bad input: missing field, bad
#                       platform, missing API key, no Giphy results, etc.)
#   - error_upstream  — Telegram / OpenAI / Giphy returned a real error
#                       (TelegramError, urlopen network failure, timeout,
#                       upstream HTTP 4xx/5xx)
#   - error_internal  — genuinely unexpected exception we should have caught
#                       (bare `except Exception` paths with no upstream context)
OutreachOutcome = Literal["success", "rejected", "error_upstream", "error_internal"]


def resolve_provider_config(
    *,
    agent_provider_url: str,
    agent_provider_key: str,
    agent_provider_model: str,
    agent_provider_ref: str,
    default_provider_ref: str,
    db,
) -> tuple[str, str, str]:
    """Resolve effective provider tuple (url, key, model) with deterministic precedence."""
    url = (agent_provider_url or "").strip()
    key = (agent_provider_key or "").strip()
    model = (agent_provider_model or "").strip()
    provider_ref = (agent_provider_ref or "").strip()

    # Agent with no explicit provider fields inherits system default provider.
    if not provider_ref and not url and not key and not model:
        provider_ref = (default_provider_ref or "").strip()

    # Agent explicit URL always wins; refs are only used when URL is empty.
    if provider_ref and not url:
        row = db.execute(
            "SELECT provider_url, provider_key, provider_model FROM providers WHERE id=?",
            (provider_ref,),
        ).fetchone()
        if row:
            url = row[0] or ""
            key = row[1] or ""
            model = row[2] or ""
    return url, key, model


# ── Agent Comms Models ───────────────────────────────────────


CONTENT_TYPES = Literal["text", "task_request", "task_response", "status", "file_transfer"]
PRIORITY_LEVELS = Literal[0, 1, 2]


# ── Skill Models ────────────────────────────────────────────


# ── Outreach Config Models ──────────────────────────────────


# ── Agent Models ─────────────────────────────────────────────


def _refresh_1m_models(registry) -> None:
    """Refresh the 1M model set from the registry."""
    global _1M_MODELS
    try:
        db_set = registry.get_1m_models()
        if db_set:
            _1M_MODELS = db_set
    except Exception:
        pass  # Keep fallback


# ── Research Pipeline Models ──────────────────────────────────


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


# Maps agent skills to the pinky-self tool gates they require.
# An agent only gets the gates needed by their assigned skills.
# Agents with no skills get core tools only (no gates).
# To add a new mapping: add the skill name as key, gate names as values.
#
# "pinky-self" covers general agent management gates.
# Specialized features (research, presentations) need their own skills.
SKILL_TO_GATES: dict[str, list[str]] = {
    "pinky-self": ["schedule", "admin", "skill-admin", "triggers", "extras", "tasks-admin", "voice", "apps"],
    "pinky-memory": ["kb"],
    "research": ["research"],
    "presentations": ["presentations"],
}

# All valid gate names for reference
ALL_TOOL_GATES = [
    "extras", "kb", "research", "presentations", "triggers",
    "schedule", "skill-admin", "admin", "tasks-admin", "voice", "apps",
]

# Gate → pinky-self tool names registered under that gate.
# Used to compute disallowed_tools for SDK-side gating in shared MCP mode
# (where the shared server runs ALL gates and filtering happens client-side).
GATE_TOOL_NAMES: dict[str, list[str]] = {
    "extras": [
        "get_attribution", "render_pdf", "spawn_clone", "get_agent_card",
    ],
    "schedule": [
        "set_wake_schedule", "list_my_schedules", "remove_wake_schedule",
    ],
    "tasks-admin": [
        "decompose_project", "bulk_create_tasks",
    ],
    "presentations": [
        "get_presentation_template", "create_presentation",
        "update_presentation", "list_presentations",
    ],
    "research": [
        "submit_research_brief", "submit_research_review",
        "get_my_research_assignments", "claim_research_topic",
        "create_research_topic", "publish_research",
        "list_research_topics", "get_research_detail",
        "export_research_pdf",
    ],
    "skill-admin": [
        "list_available_skills", "add_skill", "remove_skill",
        "discover_skills", "install_skill", "create_skill", "propose_skill",
    ],
    "admin": [
        "check_for_updates", "update_and_restart", "restart_daemon",
        "register_agent",
    ],
    "triggers": [
        "create_trigger", "list_triggers", "delete_trigger", "test_trigger",
    ],
    "kb": [
        "kb_ingest", "kb_search", "kb_get_wiki", "kb_stats",
        "kb_run_librarian", "kb_save_wiki", "kb_delete_wiki",
        "kb_delete_raw", "kb_update_raw",
    ],
    "voice": [
        "propose_call", "list_voice_calls", "list_call_requests",
    ],
    "apps": [
        "create_app", "deploy_app", "update_app", "get_app_source",
        "list_apps", "delete_app", "app_url",
    ],
}

# Pre-compute the full set of all gated tool names (MCP-prefixed)
ALL_GATED_TOOL_NAMES: set[str] = set()
for _gate_tools in GATE_TOOL_NAMES.values():
    for _tool in _gate_tools:
        ALL_GATED_TOOL_NAMES.add(f"mcp__pinky-self__{_tool}")


def _get_shared_mode_disallowed_tools(agent_name: str, skill_store=None) -> list[str]:
    """Compute disallowed pinky-self tools for SDK-side gating in shared MCP mode.

    In shared mode the server runs ALL gates. We filter client-side by telling
    the SDK which tools the agent should NOT see. Returns MCP-prefixed tool names
    like 'mcp__pinky-self__kb_ingest'.
    """
    agent_gates = set(_get_agent_tool_gates(agent_name, skill_store))
    disallowed: list[str] = []
    for gate, tools in GATE_TOOL_NAMES.items():
        if gate not in agent_gates:
            for tool in tools:
                disallowed.append(f"mcp__pinky-self__{tool}")
    return sorted(disallowed)

def _get_agent_tool_gates(agent_name: str, skill_store=None) -> list[str]:
    """Determine which pinky-self tool gates should be active for an agent.

    Returns a list of gate names that will be passed as --tool-gates to pinky-self.
    Maps agent skills → gates via SKILL_TO_GATES. Agents with no skills get
    core tools only (no gates). They can always load_skill() to get more.
    """
    if not skill_store:
        # No skill store available — fall back to all gates for safety
        return list(ALL_TOOL_GATES)

    try:
        agent_skills = skill_store.get_agent_skills(agent_name, enabled_only=True)
    except Exception:
        return list(ALL_TOOL_GATES)

    if not agent_skills:
        return []  # No skills → core tools only

    # Collect gates from all assigned skills
    gates: set[str] = set()
    for skill in agent_skills:
        skill_name = skill.get("name", "")
        if skill_name in SKILL_TO_GATES:
            gates.update(SKILL_TO_GATES[skill_name])

    return sorted(gates)


# #623: env var names whose VALUES must never be returned over the API. The
# /agents/{name}/mcp-servers endpoint echoes the .mcp.json / DB `env` block,
# which now carries PINKY_AGENT_KEY (the per-agent signing credential) for
# stdio agents. The substring set also redacts custom-server secrets as
# defense-in-depth. Keys stay visible (so the UI shows which vars are set);
# only values are masked.
_SENSITIVE_ENV_SUBSTRINGS = (
    "KEY", "SECRET", "TOKEN", "PASSWORD", "PASSWD", "CREDENTIAL", "AUTH",
)


# #149: path surfaces that embed a TARGET agent name in the URL. The isolation
# guard extracts that target and compares it to the authenticated caller so an
# isolated agent can only act on its OWN resources.
#   /agents/{name}, /agents/{name}/...   — agent record + all sub-resources
#   /autonomy/{name}/...                 — start/stop/event (NOT /autonomy/status,
#                                          which is target-less and so excluded by
#                                          the required trailing sub-segment)
_ISOLATION_PATH_RES = (
    re.compile(r"^/agents/([^/]+)(?:/.*)?$"),
    re.compile(r"^/autonomy/([^/]+)/.+$"),
)

# #149: body-actor surfaces (/broker/*) are NOT matched here. Their acting
# agent comes from the request BODY (field ``agent_name``), not the path, so
# path matching can't catch impersonation (e.g. an isolated agent POSTing to
# /broker/send with a forged agent_name — Pushok's review catch). Those are
# guarded in the handlers via _deny_isolated_cross_actor(), where the body is
# already parsed (reading the body in BaseHTTPMiddleware is fragile).


def _isolation_path_target(path: str) -> str | None:
    """Return the target agent name embedded in an isolation-relevant path, or
    None if the path doesn't name a specific agent."""
    for rx in _ISOLATION_PATH_RES:
        m = rx.match(path)
        if m:
            return m.group(1)
    return None


def _redact_env_secrets(env: dict) -> dict:
    """Mask values of sensitive env vars before returning an env dict via API."""
    if not isinstance(env, dict):
        return env
    out = {}
    for k, v in env.items():
        if isinstance(k, str) and any(s in k.upper() for s in _SENSITIVE_ENV_SUBSTRINGS):
            out[k] = "***redacted***"
        else:
            out[k] = v
    return out


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

    When PINKY_SHARED_MCP=1, all three core servers use SSE transport pointing
    at the shared MCP server. Memory uses a per-agent store pool for DB isolation.
    """
    pinky_src = str(Path(__file__).resolve().parent.parent)
    mcp_config: dict = {"mcpServers": {}}

    if SHARED_MCP_ENABLED:
        # Shared SSE mode: point at the shared HTTP server with agent identity header
        shared_base = f"http://{SHARED_MCP_HOST}:{SHARED_MCP_PORT}"
        agent_headers = {"X-Agent-Name": agent_name}

        # Memory: per-agent SQLite via shared SSE server (store pool)
        mcp_config["mcpServers"]["pinky-memory"] = {
            "type": "sse",
            "url": f"{shared_base}/mcp/memory/sse",
            "headers": agent_headers,
            "alwaysLoad": True,
        }
        mcp_config["mcpServers"]["pinky-self"] = {
            "type": "sse",
            "url": f"{shared_base}/mcp/self/sse",
            "headers": agent_headers,
            "alwaysLoad": True,
        }
        mcp_config["mcpServers"]["pinky-messaging"] = {
            "type": "sse",
            "url": f"{shared_base}/mcp/messaging/sse",
            "headers": agent_headers,
            "alwaysLoad": True,
        }
    else:
        # Memory: per-agent SQLite long-term memory with vector search (stdio)
        data_dir = work_dir / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        db_path = str(data_dir / "memory.db")
        mcp_config["mcpServers"]["pinky-memory"] = {
            "command": sys.executable,
            "args": ["-m", "pinky_memory", "--db", db_path],
            "cwd": pinky_src,
            "alwaysLoad": True,
        }

        # Stdio mode: spawn per-agent processes (original behavior)
        # #623 increment 2: provision this agent's per-agent signing key into
        # the per-agent MCP subprocesses so they sign internal requests with a
        # non-forgeable identity (resolve_signing_secret prefers PINKY_AGENT_KEY
        # over the inherited global PINKY_SESSION_SECRET). The `env` field is
        # additive — the subprocess still inherits PINKY_SESSION_SECRET, and the
        # daemon dual-accepts either, so a missing key degrades gracefully.
        stdio_env: dict[str, str] = {}
        if agent_registry:
            try:
                agent_key = agent_registry.get_signing_key(agent_name)
                if agent_key:
                    stdio_env["PINKY_AGENT_KEY"] = agent_key
            except Exception:
                pass
            # #641: hand self/messaging the explicit agents-DB path so they can
            # look up the CURRENT signing key at request time (via a resolver in
            # their entrypoints) instead of trusting the baked PINKY_AGENT_KEY
            # above, which goes stale when the daemon restarts but the long-lived
            # stdio servers (children of the persistent tmux REPL) don't. Explicit
            # path — these run with cwd=pinky_src, so a relative DB path would
            # resolve wrong. Regenerated on every daemon startup (see _start), so
            # existing agents pick this up on the next REPL relaunch.
            db_path = getattr(agent_registry, "_db_path", "")
            if isinstance(db_path, str) and db_path:
                stdio_env["PINKY_AGENTS_DB"] = db_path

        # Pinky-self: heartbeat_ack, schedules, self-management
        tool_gates = _get_agent_tool_gates(agent_name, skill_store)
        self_args = [
            "-m", "pinky_self", "--agent", agent_name,
            "--api-url", "http://localhost:8888",
        ]
        if tool_gates:
            self_args.extend(["--tool-gates", ",".join(tool_gates)])
        mcp_config["mcpServers"]["pinky-self"] = {
            "command": sys.executable,
            "args": self_args,
            "cwd": pinky_src,
            "alwaysLoad": True,
        }
        if stdio_env:
            mcp_config["mcpServers"]["pinky-self"]["env"] = dict(stdio_env)

        # Pinky-messaging: outbound messaging through the broker
        mcp_config["mcpServers"]["pinky-messaging"] = {
            "command": sys.executable,
            "args": [
                "-m", "pinky_messaging", "--agent", agent_name,
                "--api-url", "http://localhost:8888",
            ],
            "cwd": pinky_src,
            "alwaysLoad": True,
        }
        if stdio_env:
            mcp_config["mcpServers"]["pinky-messaging"]["env"] = dict(stdio_env)

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
    # Contains the agent's PINKY_AGENT_KEY — restrict to owner-only (0600).
    try:
        os.chmod(mcp_json, 0o600)
    except OSError:
        # Best-effort hardening — a failed chmod must not block agent setup.
        pass


def _check_installed_deps_drift(repo_dir: str) -> list[dict]:
    """Compare installed package versions to pyproject.toml pins.

    Returns a list of drift entries — one per dependency whose installed
    version doesn't satisfy the pin (including missing packages). Each entry
    is ``{"package": str, "specifier": str, "installed": str | None}``.

    Used by ``admin_update`` to decide whether ``pip install`` is needed even
    when ``pyproject.toml`` didn't change in the current pull. Catches the
    common failure mode where an earlier pull bumped a pin but its routine
    restart skipped the reinstall step, leaving installed packages stale.

    Failures (missing pyproject, unparseable deps) are surfaced via raised
    exceptions and treated as non-fatal by callers — drift can't be assessed
    so reinstall stays gated on the other triggers.
    """
    import tomllib
    from importlib.metadata import PackageNotFoundError, version

    from packaging.requirements import Requirement

    pyproject_path = Path(repo_dir) / "pyproject.toml"
    with open(pyproject_path, "rb") as f:
        meta = tomllib.load(f)

    project = meta.get("project", {}) or {}
    deps: list[str] = list(project.get("dependencies", []) or [])
    optional = project.get("optional-dependencies", {}) or {}
    for group_deps in optional.values():
        if group_deps:
            deps.extend(group_deps)

    drifts: list[dict] = []
    for dep_str in deps:
        try:
            req = Requirement(dep_str)
        except Exception:
            # Unparseable entry — skip rather than fail the whole check.
            continue
        # Skip markers that don't apply to the current env (e.g. python_version<"3.10")
        if req.marker is not None and not req.marker.evaluate():
            continue
        try:
            installed = version(req.name)
        except PackageNotFoundError:
            drifts.append({
                "package": req.name,
                "specifier": str(req.specifier),
                "installed": None,
            })
            continue
        if req.specifier and not req.specifier.contains(installed, prereleases=True):
            drifts.append({
                "package": req.name,
                "specifier": str(req.specifier),
                "installed": installed,
            })
    return drifts


def _sha256_file(path: Path) -> str | None:
    """Return the SHA-256 hex digest for ``path``, or ``None`` if absent."""
    if not path.exists() or not path.is_file():
        return None
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _current_git_hash(repo_dir: str) -> str:
    """Best-effort short HEAD hash for frontend build status."""
    import subprocess as sp

    try:
        return sp.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=repo_dir, stderr=sp.DEVNULL, timeout=10,
        ).decode().strip()
    except Exception:
        return "unknown"


def _frontend_index_assets(index_path: Path) -> list[str]:
    """Extract /assets references from a built frontend index.html."""
    if not index_path.exists():
        return []
    html = index_path.read_text(errors="replace")
    assets = re.findall(r"""(?:src|href)=["']/assets/([^"']+)["']""", html)
    # Stable order, no duplicates.
    return list(dict.fromkeys(assets))


def _write_frontend_build_manifest(
    repo_dir: str,
    *,
    git_hash: str | None = None,
    built_at: float | None = None,
) -> dict:
    """Write frontend-dist/build-manifest.json after a successful build.

    The manifest is intentionally daemon-side rather than Vite-coupled:
    it records the git hash, package-lock hash, and asset references
    observed in the freshly-built index.html.
    """
    repo = Path(repo_dir)
    dist = repo / "frontend-dist"
    index_path = dist / "index.html"
    if not index_path.exists():
        raise FileNotFoundError(f"frontend index not found: {index_path}")

    manifest = {
        "version": 1,
        "built_at": built_at if built_at is not None else time.time(),
        "git_hash": git_hash or _current_git_hash(repo_dir),
        "package_lock_sha256": _sha256_file(
            repo / "frontend-svelte" / "package-lock.json"
        ),
        "assets": _frontend_index_assets(index_path),
    }
    manifest_path = dist / FRONTEND_BUILD_MANIFEST
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return manifest


def _frontend_build_status(
    repo_dir: str,
    *,
    current_git_hash: str | None = None,
) -> dict:
    """Return frontend build freshness diagnostics.

    Status values:
    - missing: frontend-dist or index.html is absent
    - broken: index.html references missing assets or manifest is unreadable
    - unverified: dist exists but no daemon-written manifest is present
    - stale: manifest exists but git/package-lock hash differs
    - ok: manifest matches current git/package-lock hash and assets exist
    """
    repo = Path(repo_dir)
    dist = repo / "frontend-dist"
    index_path = dist / "index.html"
    manifest_path = dist / FRONTEND_BUILD_MANIFEST
    current_hash = current_git_hash or _current_git_hash(repo_dir)
    current_lock_hash = _sha256_file(repo / "frontend-svelte" / "package-lock.json")

    base = {
        "status": "unknown",
        "message": "",
        "dist_exists": dist.exists(),
        "index_exists": index_path.exists(),
        "manifest_exists": manifest_path.exists(),
        "current_git_hash": current_hash,
        "current_package_lock_sha256": current_lock_hash,
        "built_git_hash": None,
        "built_package_lock_sha256": None,
        "assets": [],
        "built_assets": [],
        "missing_assets": [],
        "stale_reasons": [],
    }

    if not dist.exists():
        base.update({
            "status": "missing",
            "message": (
                "Frontend build missing: frontend-dist does not exist. "
                "Run /admin/update with npm available or build the frontend."
            ),
        })
        return base

    if not index_path.exists():
        base.update({
            "status": "missing",
            "message": (
                "Frontend build missing: frontend-dist/index.html does not exist. "
                "Run /admin/update with npm available or build the frontend."
            ),
        })
        return base

    assets = _frontend_index_assets(index_path)
    missing_assets = [
        asset for asset in assets
        if not (dist / "assets" / asset).exists()
    ]
    base["assets"] = assets
    base["missing_assets"] = missing_assets
    if missing_assets:
        base.update({
            "status": "broken",
            "message": (
                "Frontend build is broken: index.html references missing "
                f"asset(s): {', '.join(missing_assets)}"
            ),
        })
        return base

    if not manifest_path.exists():
        base.update({
            "status": "unverified",
            "message": (
                "Frontend build present but unverified: build-manifest.json "
                "is absent (expected for Docker images built before manifest "
                "support, or manual builds)."
            ),
        })
        return base

    try:
        manifest = json.loads(manifest_path.read_text())
    except Exception as e:
        base.update({
            "status": "broken",
            "message": f"Frontend build manifest is unreadable: {e}",
        })
        return base

    built_hash = str(manifest.get("git_hash") or "")
    built_lock_hash = manifest.get("package_lock_sha256")
    raw_built_assets = manifest.get("assets")
    built_assets = list(raw_built_assets or []) if isinstance(raw_built_assets, list) else []
    base["built_git_hash"] = built_hash or None
    base["built_package_lock_sha256"] = built_lock_hash
    base["built_assets"] = built_assets

    if not built_hash or not built_lock_hash or not isinstance(raw_built_assets, list):
        base.update({
            "status": "unverified",
            "message": "Frontend build manifest is present but incomplete.",
        })
        return base

    stale_reasons: list[str] = []
    if (
        built_hash
        and current_hash
        and built_hash != "unknown"
        and current_hash != "unknown"
        and built_hash != current_hash
    ):
        stale_reasons.append("git_hash")
    if built_lock_hash and current_lock_hash and built_lock_hash != current_lock_hash:
        stale_reasons.append("package_lock")
    if built_assets != assets:
        stale_reasons.append("assets")

    base["stale_reasons"] = stale_reasons
    if stale_reasons:
        base.update({
            "status": "stale",
            "message": (
                "Frontend build is stale: "
                f"{', '.join(stale_reasons)} mismatch "
                f"(built={built_hash or 'unknown'}, current={current_hash})."
            ),
        })
        return base

    base.update({
        "status": "ok",
        "message": "Frontend build manifest matches current source.",
    })
    return base


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

    # ── CORS ──────────────────────────────────────────────
    # Allow origins from env (comma-separated) or default to same-origin only.
    _cors_origins = os.environ.get("PINKY_CORS_ORIGINS", "").strip()
    if _cors_origins:
        from fastapi.middleware.cors import CORSMiddleware
        app.add_middleware(
            CORSMiddleware,
            allow_origins=[o.strip() for o in _cors_origins.split(",") if o.strip()],
            allow_credentials=True,
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
            allow_headers=["Content-Type", "Authorization", "X-Internal-Auth",
                           "X-Internal-Timestamp", "X-Internal-Signature"],
            max_age=3600,
        )

    # ── Security Headers ──────────────────────────────────
    @app.middleware("http")
    async def security_headers_middleware(request: Request, call_next):
        if request.headers.get("upgrade", "").lower() == "websocket":
            return await call_next(request)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        # HSTS only when behind TLS (reverse proxy sets X-Forwarded-Proto)
        proto = request.headers.get("x-forwarded-proto", "")
        if proto == "https" or request.url.scheme == "https":
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return response

    session_store = SessionStore(db_path=db_path.replace(".db", "_sessions.db"))
    session_event_store = SessionEventStore(db_path=db_path.replace(".db", "_sessions.db"))
    store = ConversationStore(db_path=db_path)
    analytics = AnalyticsStore(db_path=db_path.replace(".db", "_analytics.db"))
    agents = AgentRegistry(db_path=db_path.replace(".db", "_agents.db"))
    _refresh_1m_models(agents)
    audit = AuditStore(db_path=db_path.replace(".db", "_audit.db"))
    hooks = HookManager(audit_store=audit)

    # In-memory live status for agents (updated by POST /agents/{name}/status).
    # Keys are agent names; values are dicts with status/tool_name/detail/last_updated.
    _agent_live_status: dict[str, dict] = {}

    # Per-agent cost milestone tracking — set of already-fired thresholds (USD).
    _agent_cost_milestones: dict[str, set[float]] = {}

    # Register built-in hooks
    hooks.register(HookEvent.post_tool_use, create_heartbeat_hook(agents))
    hooks.register(HookEvent.session_end, create_context_save_hook(agents))
    hooks.register(HookEvent.session_end, create_cost_tracker_hook())
    hooks.register(HookEvent.session_start, create_typing_indicator_hook(agents))

    manager = SessionManager(
        max_sessions=max_sessions, store=session_store,
        session_event_store=session_event_store,
        conversation_store=store, agent_registry=agents,
        hook_manager=hooks,
    )
    _comms_db = db_path.replace(".db", "_agent_comms.db")
    comms = AgentComms(db_path=_comms_db)

    # Auth-failure tracker — counts SDK auth errors across all streaming
    # sessions on this host and decides when to alert the operator. The
    # callback wiring is created in _build_auth_alert_callbacks() below
    # once _broker_send is available; the tracker itself is host-global.
    from pinky_daemon.auth_alerts import (
        TRANSPORT_FAILURE_POLICIES,
        AuthFailureTracker,
        format_alert_message,
        resolve_operator_chat,
    )
    auth_tracker = AuthFailureTracker()
    # #104: per-class trackers for non-auth StopFailure classes (billing_error,
    # rate_limit). Each is an independent sliding-window/cooldown tracker so a
    # billing failure never counts toward the rate_limit threshold (and vice
    # versa). Auth stays on its own dedicated `auth_tracker` above.
    transport_failure_trackers = {
        et: AuthFailureTracker(
            fail_window_seconds=pol.fail_window_seconds,
            fail_threshold=pol.fail_threshold,
            alert_cooldown_seconds=pol.alert_cooldown_seconds,
        )
        for et, pol in TRANSPORT_FAILURE_POLICIES.items()
    }

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

    def _get_imessage_adapter(agent_name: str = ""):
        """Get or create the iMessage adapter for an agent."""
        key = (agent_name or "__global__", "imessage")
        if key in _platform_adapters:
            return _platform_adapters[key]
        # Check if any agent has iMessage enabled in DB
        check_name = agent_name or agents.get_main_agent() or ""
        if check_name and agents.get_raw_token(check_name, "imessage"):
            from pinky_outreach.imessage import iMessageAdapter
            adapter = iMessageAdapter()
            _platform_adapters[key] = adapter
            return adapter
        return None

    def _get_tg_adapter(agent_name: str):
        return _get_platform_adapter(agent_name, "telegram")

    from pinky_daemon.telegram_markdown import md_to_tg_mdv2 as _md_to_tg_mdv2

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

        if platform == "imessage":
            im = _get_imessage_adapter(agent_name)
            if not im:
                raise HTTPException(503, "iMessage not enabled for this agent")
            result = im.send_message(chat_id, content)
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
        loop = asyncio.get_running_loop()

        async def _deliver() -> dict:
            msg = await loop.run_in_executor(
                None,
                lambda: _send_text_message(
                    agent_name,
                    platform,
                    chat_id,
                    content,
                    reply_to=reply_to,
                    parse_mode=parse_mode,
                    silent=silent,
                ),
            )
            # Once an outbound message lands, the typing indicator becomes noise —
            # Telegram has no "stop typing" API, but cancelling our 4s
            # sendChatAction loop prevents the indicator from popping back up
            # after the message arrives.
            broker._stop_typing(agent_name, chat_id)
            return {
                "sent": True,
                "agent": agent_name,
                "platform": platform,
                "chat_id": chat_id,
                "message_id": msg.message_id,
            }

        # Dedupe guard (issue #113): an identical send already in flight or just
        # delivered is suppressed and the original result returned, so a tool
        # that retried after its own timeout doesn't deliver twice. Cancellation
        # vs failure handling lives in deliver_deduped.
        return await broker.deliver_deduped(
            agent_name, platform, chat_id, content, _deliver, reply_to=reply_to
        )

    async def _broker_react(
        agent_name: str,
        platform: str,
        chat_id: str,
        message_id: str,
        emoji: str,
    ):
        """Add a reaction on behalf of an agent."""
        loop = asyncio.get_running_loop()
        if platform != "telegram":
            adapter = _get_platform_adapter(agent_name, platform)
            if not adapter:
                return
            try:
                if platform == "discord":
                    await loop.run_in_executor(None, lambda: adapter.add_reaction(chat_id, message_id, emoji))
                elif platform == "slack":
                    await loop.run_in_executor(None, lambda: adapter.add_reaction(chat_id, message_id, emoji))
                return
            except Exception as e:
                _log(f"broker-react: failed for {agent_name} -> {chat_id}:{message_id} {emoji}: {e}")
                return

        adapter = _get_tg_adapter(agent_name)
        if not adapter:
            return

        try:
            await loop.run_in_executor(None, lambda: adapter.set_reaction(chat_id, int(message_id), emoji))
        except Exception as e:
            _log(f"broker-react: failed for {agent_name} -> {chat_id}:{message_id} {emoji}: {e}")

    async def _broker_typing(agent_name: str, platform: str, chat_id: str):
        """Show typing indicator on the platform."""
        adapter = _get_platform_adapter(agent_name, platform)
        if not adapter:
            return
        try:
            loop = asyncio.get_running_loop()
            if platform == "telegram":
                await loop.run_in_executor(None, lambda: adapter.send_chat_action(chat_id, "typing"))
            elif platform == "discord":
                await loop.run_in_executor(None, lambda: adapter.send_typing(chat_id))
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

        def _generate_and_send():
            """Run all blocking TTS + send I/O in a thread."""
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
            finally:
                if audio_path:
                    try:
                        os.unlink(audio_path)
                    except Exception:
                        pass

        # Issue #395 follow-up: wrap in try/except so a failure surfaces as a
        # structured 502 (not 500), and move _stop_typing into `finally` so a
        # failed send doesn't leave the chat showing "typing…" forever.
        try:
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(None, _generate_and_send)
        except HTTPException:
            # All HTTPExceptions reachable here come from caller-side config
            # validation inside _generate_and_send (e.g. missing API key,
            # unknown TTS provider) — bucket as `rejected`.
            _outreach_attempt_log(
                agent_name=agent_name, platform=platform, method="send_voice",
                chat_id=chat_id, file_path="", caption_len=len(text),
                outcome="rejected", error="HTTPException",
            )
            raise
        except Exception as e:
            # Bare catch-all wraps urlopen (TTS provider) + adapter.send_voice
            # (Telegram) — both are upstream calls, so bucket as `error_upstream`.
            error_repr = f"{type(e).__name__}: {e}"
            _outreach_attempt_log(
                agent_name=agent_name, platform=platform, method="send_voice",
                chat_id=chat_id, file_path="", caption_len=len(text),
                outcome="error_upstream", error=error_repr,
            )
            raise HTTPException(502, f"Failed to send_voice: {error_repr}") from e
        finally:
            broker._stop_typing(agent_name, chat_id)

        _outreach_attempt_log(
            agent_name=agent_name, platform=platform, method="send_voice",
            chat_id=chat_id, file_path="", caption_len=len(text),
            outcome="success",
        )
        return result

    activity = ActivityStore(db_path=db_path.replace(".db", "_activity.db"))
    app.state.activity = activity
    # Exposed for unit-test reach-in (#591 P1#1 — verifying commit=False
    # preserves inbox state). Not part of the public API.
    app.state.comms = comms

    async def _broker_stop_agent(agent_name: str) -> dict:
        """Stop callback for broker command interception."""
        streaming_closed = await _disconnect_streaming_sessions(agent_name)
        closed = 0
        for s in list(manager.list()):
            if s.agent_name == agent_name:
                manager.delete(s.id)
                closed += 1
        total = streaming_closed + closed
        _log(f"api: agent {agent_name} force-stopped via /stop command, "
             f"closed {total} session(s)")
        activity.log(agent_name, "agent_stop", f"{agent_name} force-stopped via /stop")
        return {"agent": agent_name, "status": "stopped", "sessions_closed": total}

    async def _broker_stop_all() -> dict:
        """Stop-all callback for broker command interception."""
        results = {}
        for a in agents.list():
            sc = await _disconnect_streaming_sessions(a.name)
            closed = 0
            for s in list(manager.list()):
                if s.agent_name == a.name:
                    manager.delete(s.id)
                    closed += 1
            total = sc + closed
            if total > 0:
                results[a.name] = total
                _log(f"api: agent {a.name} force-stopped via /stop all")
                activity.log(a.name, "agent_stop",
                             f"{a.name} force-stopped (stop-all via /stop)")
        return {"stopped": results, "total_agents": len(results)}

    broker = MessageBroker(
        agents,
        manager,
        send_callback=_broker_send,
        reaction_callback=_broker_react,
        typing_callback=_broker_typing,
        stop_callback=_broker_stop_agent,
        stop_all_callback=_broker_stop_all,
        activity_store=activity,
    )
    _broker_pollers: list = []  # Track active broker pollers

    app.state.manager = manager
    app.state.broker = broker
    app.state.agents = agents
    app.state.auth_tracker = auth_tracker
    app.state.transport_failure_trackers = transport_failure_trackers
    app.state.conversation_store = store
    app.state.session_store = session_store
    app.state.session_event_store = session_event_store

    _COST_MILESTONES = (1.0, 5.0, 10.0, 25.0, 50.0, 100.0)  # noqa: N806

    def _make_cost_callback(registry):
        """Create a sync callback to persist per-turn cost data and fire cost milestones."""
        def _record_cost(agent_name, cost_usd, input_tokens, output_tokens, session_id):
            registry.record_cost(
                agent_name,
                cost_usd,
                input_tokens,
                output_tokens,
                session_id=session_id,
            )
            # Check cost milestones for this session
            sessions_map = broker._streaming.get(agent_name, {})
            total_session_cost = sum(ss.usage.total_cost_usd for ss in sessions_map.values())
            fired = _agent_cost_milestones.setdefault(agent_name, set())
            for milestone in _COST_MILESTONES:
                if total_session_cost >= milestone and milestone not in fired:
                    fired.add(milestone)
                    activity.log(
                        agent_name=agent_name,
                        event_type="cost_milestone",
                        title=f"{agent_name} reached ${milestone:.0f} session spend",
                        metadata={
                            "milestone_usd": milestone,
                            "total_session_cost_usd": round(total_session_cost, 4),
                        },
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

    def _humanize_seconds(seconds: float) -> str:
        """Render a compact human-readable duration."""
        total = max(int(seconds), 0)
        if total < 60:
            return f"{total}s"
        minutes, sec = divmod(total, 60)
        if minutes < 60:
            return f"{minutes}m" if sec == 0 else f"{minutes}m {sec}s"
        hours, minutes = divmod(minutes, 60)
        if hours < 24:
            return f"{hours}h" if minutes == 0 else f"{hours}h {minutes}m"
        days, hours = divmod(hours, 24)
        return f"{days}d" if hours == 0 else f"{days}d {hours}h"

    def _get_saved_context_freshness(agent_name: str) -> dict:
        """Return age/source information for the saved continuation context."""
        ctx = agents.get_context(agent_name)
        if not ctx:
            return {
                "exists": False,
                "explicit_save": False,
                "stale_warning": False,
                "age_seconds": None,
                "age_human": "",
                "source": "",
                "updated_at": None,
                "updated_by": "",
            }

        age_seconds = max(time.time() - ctx.updated_at, 0.0)
        source = ""
        if isinstance(ctx.metadata, dict):
            source = str(ctx.metadata.get("source", "") or "")

        return {
            "exists": True,
            "explicit_save": source == CONTEXT_SAVE_SOURCE_SELF_TOOL,
            "stale_warning": age_seconds >= CONTEXT_STALE_WARNING_SECONDS,
            "age_seconds": int(age_seconds),
            "age_human": _humanize_seconds(age_seconds),
            "source": source,
            "updated_at": ctx.updated_at,
            "updated_by": ctx.updated_by,
        }

    def _build_restart_guard(
        agent_name: str,
        *,
        current_session_id: str = "",
        activity_ts: float = 0.0,
    ) -> dict:
        """Decide whether restart/sleep is safe based on explicit saved context."""
        freshness = _get_saved_context_freshness(agent_name)
        if not freshness["exists"]:
            return {
                **freshness,
                "restart_safe": False,
                "current_session_match": False,
                "activity_gap_seconds": None,
                "activity_gap_human": "",
                "within_buffer": False,
                "reason": "missing_context",
            }

        current_session_match = bool(current_session_id) and (
            freshness["updated_by"] == current_session_id
        )
        activity_reference = activity_ts or time.time()
        activity_gap_seconds = max(activity_reference - float(freshness["updated_at"] or 0.0), 0.0)
        within_buffer = activity_gap_seconds <= CONTEXT_ACTIVITY_SAVE_BUFFER_SECONDS
        # current_session_match is intentionally excluded: a fresh wake-from-save legitimately
        # has the latest explicit save owned by the prior session. The 5-minute within_buffer
        # window is sufficient to detect stale saves without false-positives on restart.
        restart_safe = bool(freshness["explicit_save"]) and within_buffer

        reason = "ok"
        if not freshness["explicit_save"]:
            reason = "missing_explicit_save"
        elif not within_buffer:
            reason = "save_too_old"

        return {
            **freshness,
            "restart_safe": restart_safe,
            "current_session_match": current_session_match,
            "activity_gap_seconds": int(activity_gap_seconds),
            "activity_gap_human": _humanize_seconds(activity_gap_seconds),
            "within_buffer": within_buffer,
            "reason": reason,
        }

    def _guard_message(action: str, guard: dict) -> str:
        """Explain why a restart-like action is blocked."""
        if guard["reason"] == "missing_context":
            return (
                f"{action.capitalize()} blocked: no saved context exists. "
                f"Call save_my_context() from this session before {action}."
            )
        if guard["reason"] == "missing_explicit_save":
            return (
                f"{action.capitalize()} blocked: the last saved context was not written via save_my_context(). "
                f"Call save_my_context() from this session before {action}."
            )
        if guard["reason"] == "different_session":
            age = guard.get("age_human") or "unknown age"
            return (
                f"{action.capitalize()} blocked: the latest explicit save belongs to a different session "
                f"({age} old). Call save_my_context() again in this session before {action}."
            )
        if guard["reason"] == "save_too_old":
            gap = guard.get("activity_gap_human") or "too long"
            return (
                f"{action.capitalize()} blocked: your last explicit save trails session activity by {gap}. "
                f"Save again before {action}; the allowed buffer is 5 minutes."
            )
        return ""

    def _resolve_agent_provider(agent) -> tuple[str, str, str]:
        """Resolve (provider_url, provider_key, provider_model) for an agent.

        If the agent has a provider_ref set and no explicit provider_url, looks up
        the global provider from the providers table. If provider_ref is empty and
        no explicit provider fields are set, falls back to the system default
        provider (`default_provider_ref`) from settings. Agent-level fields win.
        Returns (url, key, model) — any may be empty string.
        """
        try:
            return resolve_provider_config(
                agent_provider_url=agent.provider_url or "",
                agent_provider_key=agent.provider_key or "",
                agent_provider_model=agent.provider_model or "",
                agent_provider_ref=agent.provider_ref or "",
                default_provider_ref=agents.get_setting("default_provider_ref", "") or "",
                db=agents._db,
            )
        except Exception as e:
            _log(f"api: could not resolve provider config for {agent.name}: {e}")
            return agent.provider_url or "", agent.provider_key or "", agent.provider_model or ""

    def _get_streaming_restart_guard(agent_name: str, ss) -> dict:
        """Build a restart guard for a live streaming session."""
        guard = _build_restart_guard(
            agent_name,
            current_session_id=ss.resume_handle or "",
            activity_ts=getattr(ss, "last_active", 0.0) or time.time(),
        )
        guard["message"] = _guard_message("restart", guard)
        return guard

    def _get_previous_wake_timestamp(agent_name: str) -> float | None:
        """Return the timestamp of the previous ``agent_wake`` event for the
        agent, or ``None`` if no prior wake exists.

        Used by :func:`_build_streaming_wake_context` to decide whether a
        saved-context ``wake_action`` is fresh-for-this-cycle on a
        ``RESUME`` wake — the directive is "fresh" iff it was written
        AFTER the previous wake (i.e. set during the current awake
        period, not replayed from a prior cycle). #591 fix.

        Safe to call from ``_build_streaming_wake_context`` because the
        CURRENT wake's ``agent_wake`` event is logged AFTER this function
        returns (see api.py:2449 and api.py:7557 — wake events are
        logged post-connect, while wake-context is built pre-connect).
        So ``list(..., limit=1)`` returns the PREVIOUS wake's timestamp.
        """
        try:
            events = activity.list(
                agent_name=agent_name, event_type="agent_wake", limit=1
            )
        except Exception:
            return None
        if not events:
            return None
        ts = events[0].get("created_at")
        try:
            return float(ts) if ts is not None else None
        except (TypeError, ValueError):
            return None

    def _build_streaming_wake_context(
        agent_name: str,
        reason: WakeReason = WakeReason.NEW_SESSION,
        *,
        commit: bool = True,
    ) -> str:
        """Build wake context for a streaming session.

        ``reason`` gates how the saved-context manifest is rendered (#591).
        On ``WakeReason.RESUME`` the agent is warm-resuming via
        ``claude --continue`` and the prior conversation is already in
        context, so the bulk manifest (task/context/notes/blockers/
        priority_items) is redundant and replaying it caused the
        stale-continuation bug. Only a *fresh-this-cycle* ``wake_action``
        is preserved on RESUME (directive, not history).

        For ``CONTEXT_RESTART`` / ``AUTO_RESTART`` / ``NEW_SESSION`` /
        ``IDLE_WAKE``, the agent is launching fresh (or near-fresh) with
        no prior conversation, so the full manifest is rendered as
        before. The default value preserves pre-#591 behavior for the
        ``wake_context_builder(name)`` 1-arg callers.

        ``commit`` gates the side-effecting consumption steps embedded in
        the build (inbox ``mark_read`` and restart-manifest deletion).
        When ``False``, the function renders the *current* state without
        consuming — used by the eager pre-build at session-config creation
        time, which would otherwise consume inbox + manifest BEFORE the
        delivered connect-time rebuild runs (Murzik P1#1: production saw
        unread agent messages and restart-manifest content silently
        vanish from real wake prompts). ``True`` (the default) preserves
        pre-#591 behavior for legacy callers and is what the connect-time
        rebuild uses.
        """
        wake_ctx = ""
        emitted_manifest = False  # Whether saved-context contributed to wake_ctx
        saved = agents.get_context(agent_name)
        if saved:
            if reason == WakeReason.RESUME:
                # Warm --continue resume: drop the bulk manifest. Emit
                # wake_action only if it was set during the current
                # awake period (manifest written after the previous
                # wake). This blocks replay of stale directives from
                # prior sleep cycles (the original #591 symptom).
                #
                # Deliberately do NOT set ``emitted_manifest = True``
                # here. The stale-warning that gates on that flag is
                # about the BULK manifest's absolute age (>12h); this
                # branch already validated freshness via the tighter
                # cycle-bound check, and we never emitted the bulk to
                # begin with. Firing the warning on a wake_action that
                # was JUST certified fresh-this-cycle would be a
                # contradiction (Barsik branch review).
                if saved.wake_action:
                    prev_wake_ts = _get_previous_wake_timestamp(agent_name)
                    fresh_this_cycle = (
                        prev_wake_ts is None or saved.updated_at > prev_wake_ts
                    )
                    if fresh_this_cycle:
                        directive = saved.to_prompt(resume_mode=True)
                        if directive:
                            wake_ctx = directive
            else:
                ctx_prompt = saved.to_prompt()
                if ctx_prompt:
                    wake_ctx = ctx_prompt
                    emitted_manifest = True

        # Freshness warning only meaningful when we emitted the manifest —
        # for RESUME with wake_action_only the cycle-bound gate already
        # validated freshness, and for empty wake_ctx the warning is
        # misleading (warns about state we're not actually showing).
        if emitted_manifest:
            freshness = _get_saved_context_freshness(agent_name)
            if freshness.get("stale_warning"):
                warning = (
                    f"WARNING: Saved continuation context is {freshness.get('age_human', 'stale')} old. "
                    "Verify it against recent work before relying on it."
                )
                wake_ctx = f"{warning}\n\n{wake_ctx}" if wake_ctx else warning

        channel_ctx = broker.build_channel_context(agent_name)
        if channel_ctx:
            wake_ctx = f"{wake_ctx}\n\n{channel_ctx}" if wake_ctx else channel_ctx

        # Inject dream summary if the agent dreamed last night and has dream_notify enabled
        agent = agents.get(agent_name)
        if agent and getattr(agent, "dream_notify", True):
            morning_summary = dream_runner.get_morning_summary(agent_name)
            if morning_summary:
                dream_ctx = f"Dream summary from last night:\n{morning_summary}"
                wake_ctx = f"{wake_ctx}\n\n{dream_ctx}" if wake_ctx else dream_ctx

        # Inject unread inbox messages from other agents
        inbox_messages = comms.get_inbox(agent_name, unread_only=True, limit=10)
        if inbox_messages:
            lines = []
            for m in inbox_messages:
                lines.append(f"  From {m.from_session}: {m.content}")
            inbox_ctx = (
                f"Unread agent messages ({len(inbox_messages)}):\n"
                + "\n".join(lines)
                + "\nReply via send_to_agent, not by messaging the owner."
            )
            wake_ctx = f"{wake_ctx}\n\n{inbox_ctx}" if wake_ctx else inbox_ctx
            if commit:
                comms.mark_read(agent_name, [m.id for m in inbox_messages])

        # Inject open task summary
        try:
            open_tasks = tasks.list(assigned_agent=agent_name)
            if open_tasks:
                in_progress = [t for t in open_tasks if t.status == "in_progress"]
                pending = [t for t in open_tasks if t.status == "pending"]
                blocked = [t for t in open_tasks if t.status == "blocked"]
                task_parts = []
                if in_progress:
                    task_parts.append(f"{len(in_progress)} in progress")
                if pending:
                    task_parts.append(f"{len(pending)} pending")
                if blocked:
                    task_parts.append(f"{len(blocked)} blocked")
                task_summary = f"Open tasks ({len(open_tasks)}): {', '.join(task_parts)}."
                top_tasks = (in_progress + pending + blocked)[:5]
                for t in top_tasks:
                    task_summary += f"\n  [{t.status}] #{t.id}: {t.title}"
                wake_ctx = f"{wake_ctx}\n\n{task_summary}" if wake_ctx else task_summary
        except Exception:
            pass  # Task store may not be initialized yet

        # Inject restart manifest entry if present — written by on_shutdown() before restart.
        # After reading, remove this agent's entry so it doesn't repeat on subsequent wakes.
        import json as _json
        from datetime import datetime, timedelta, timezone
        manifest_path = Path(db_path).parent / "restart_manifest.json"
        if manifest_path.exists():
            try:
                manifest = _json.loads(manifest_path.read_text())
                agent_entry = manifest.get("agents", {}).get(agent_name)
                if agent_entry:
                    in_progress = agent_entry.get("in_progress", "")
                    activity_log = agent_entry.get("activity_log", [])
                    pending = agent_entry.get("pending_responses", 0)
                    restart_time = manifest.get("restart_time", "")

                    # Only inject if manifest is fresh (< 2 hours old) to avoid stale data
                    try:
                        rt = datetime.fromisoformat(restart_time)
                        if rt.tzinfo is None:
                            rt = rt.replace(tzinfo=timezone.utc)
                        age = datetime.now(timezone.utc) - rt
                        fresh = age < timedelta(hours=2)
                    except Exception:
                        fresh = True

                    if fresh:
                        restart_lines = [f"System restarted at {restart_time}."]
                        if in_progress:
                            restart_lines.append(f"You were mid-task: {in_progress}")
                        if activity_log:
                            recent = activity_log[-3:]
                            restart_lines.append(f"Last recorded actions: {', '.join(str(a) for a in recent)}")
                        if pending > 0:
                            restart_lines.append(
                                f"You had {pending} pending response(s) in flight — may not have been delivered."
                            )
                        restart_ctx = "── Restart Manifest ──\n" + "\n".join(restart_lines) + "\n──────────────────"
                        wake_ctx = f"{restart_ctx}\n\n{wake_ctx}" if wake_ctx else restart_ctx

                    # Consume this agent's entry so it doesn't repeat on next wake.
                    # Gated by ``commit`` — preview builds (eager pre-build at
                    # config-create time) must NOT delete or the delivered
                    # rebuild loses the manifest content.
                    if commit:
                        del manifest["agents"][agent_name]
                        if manifest["agents"]:
                            manifest_path.write_text(_json.dumps(manifest, indent=2))
                        else:
                            manifest_path.unlink(missing_ok=True)
            except Exception as e:
                _log(f"wake_context: failed to read restart manifest for {agent_name}: {e}")

        # ── MCP bind-probe directive (#663 phase-2) ──────────────────
        # For agents enrolled in MCP-orphan auto-recovery (per-agent
        # mcp_recover flag), inject a top-of-prompt directive eliciting an
        # mcp_probe round-trip, so a healthy agent POSITIVELY proves its MCP
        # client re-bound to the current gateway generation instead of us
        # inferring health from prolonged silence. Gated on commit=True: the
        # connect-time rebuild is the delivered build whose text actually
        # reaches the model, so its fresh nonce matches the registered request;
        # the eager preview (commit=False) gets no directive and logs no request
        # (a throwaway-nonce directive there could never be answered). Record
        # the request BEFORE mutating wake_ctx so a record failure leaves the
        # normal wake text intact. Fully fail-safe: any error falls back to the
        # normal wake text with no request, so a probe bug can never block a
        # launch. Placed last so it sits at the VERY TOP of wake_ctx (not buried
        # under saved-state/channel/task text).
        try:
            if (
                commit
                and agent
                and WatchdogConfig.from_raw(
                    getattr(agent, "watchdog_config", None)
                ).mcp_recover
            ):
                launch_id = uuid.uuid4().hex[:12]
                nonce = uuid.uuid4().hex[:12]
                record_probe_request(agent_name, launch_id, nonce)
                probe_directive = (
                    "⚠️ MCP BIND CHECK — FIRST, silently, before anything else: "
                    f"call mcp_probe(nonce='{nonce}', launch_id='{launch_id}') to "
                    "confirm your MCP transport re-bound to the current gateway, "
                    "then continue normally. Do not mention this unless it fails."
                )
                wake_ctx = (
                    f"{probe_directive}\n\n{wake_ctx}" if wake_ctx else probe_directive
                )
        except Exception as e:
            _log(
                f"wake_context: mcp-probe directive injection failed for "
                f"{agent_name}: {e}"
            )

        return wake_ctx

    # Exposed for unit-test reach-in (#591 reason-gating verification).
    # Not part of the public API; harness should not depend on it.
    app.state._build_streaming_wake_context = _build_streaming_wake_context

    def _log_agent_wake_event(agent_name: str, reason: WakeReason) -> None:
        """Fires from SDK / tmux / codex sessions after a wake prompt is
        successfully delivered.

        Centralizes the ``agent_wake`` activity event so the #591
        cycle-gate boundary advances on EVERY delivered warm wake — not
        just cold-start + scheduler (the prior coverage that missed the
        ``_ensure_streaming_session`` reconnect and broker auto-wake
        paths, leaving directives to replay forever on warm wakes —
        Murzik P1#2).

        Failure-tolerant: callbacks raise, so wrap in try/except. Don't
        let a logging hiccup wedge the wake delivery.
        """
        try:
            activity.log(
                agent_name=agent_name,
                event_type="agent_wake",
                title=f"{agent_name} woke up",
                metadata={"reason": reason.value},
            )
        except Exception as e:
            _log(
                f"api: agent_wake log failed for {agent_name} "
                f"(reason={reason.value}): {e}"
            )

    # Exposed for unit-test reach-in (verifying centralized wake logging
    # advances the cycle-gate boundary). Not part of the public API.
    app.state._log_agent_wake_event = _log_agent_wake_event

    _stream_event_subscribers: dict[str, set[asyncio.Queue]] = {}

    async def _publish_stream_event(session_stream_id: str, event: dict) -> None:
        """Broadcast an incremental stream event to active SSE subscribers."""
        subscribers = list(_stream_event_subscribers.get(session_stream_id, set()))
        if not subscribers:
            return
        for queue in subscribers:
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # Drop stale updates for slow clients; final turn still persists.
                pass

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

    async def _make_streaming_event_callback(agent_name: str, label: str):
        """Create a callback for incremental codex event streaming to the web UI."""
        session_stream_id = f"{agent_name}-{label}"

        async def _on_stream_event(event: dict):
            await _publish_stream_event(session_stream_id, {
                "session_id": session_stream_id,
                **(event or {}),
            })

        return _on_stream_event

    async def _on_auth_failure(agent_name: str, error: str) -> None:
        """Streaming-session hook: record an auth failure and alert the operator.

        Called from StreamingSession's reader loop whenever the SDK reports
        an authentication-class error — either AssistantMessage.error ==
        "authentication_failed" (see streaming_session._is_auth_error_assistant)
        or ResultMessage.api_error_status in {401, 403} (see
        streaming_session._is_auth_error_result). The AuthFailureTracker
        decides whether this failure crosses the alert threshold and whether
        the cooldown has elapsed; if both, we DM the operator via
        _broker_send using the affected agent's own bot.
        """
        try:
            decision = auth_tracker.record_failure(agent_name, error)
        except Exception as e:
            _log(f"auth_alerts: tracker.record_failure raised: {e}")
            return

        if not decision.get("should_alert"):
            return

        try:
            chat_id, platform = resolve_operator_chat(
                get_setting=agents.get_setting,
                list_all_approved_users=agents.list_all_approved_users,
            )
        except Exception as e:
            _log(f"auth_alerts: resolve_operator_chat raised: {e}")
            return

        if not chat_id:
            _log(
                "auth_alerts: auth failure detected but no operator_chat_id "
                "configured and no approved_users to fall back on — "
                "alert suppressed (set system_settings.operator_chat_id)"
            )
            return

        try:
            host_label = agents.get_setting("host_label", "") or ""
        except Exception:
            host_label = ""

        body = format_alert_message(
            agent_name=agent_name,
            decision=decision,
            error=error,
            host_label=host_label,
        )

        try:
            await _broker_send(agent_name, platform, chat_id, body)
        except Exception as e:
            # Delivery failed — do NOT commit the alert. The next failure
            # crossing threshold will retry instead of being silenced for
            # the cooldown window.
            _log(
                f"auth_alerts: failed to send operator alert via "
                f"{agent_name} bot to {platform}:{chat_id}: {e} "
                f"(cooldown not advanced — will retry on next failure)"
            )
            return

        try:
            auth_tracker.commit_alert()
        except Exception as e:
            _log(f"auth_alerts: tracker.commit_alert raised: {e}")
        _log(
            f"auth_alerts: operator alert sent to {platform}:{chat_id} "
            f"for {agent_name} (reason={decision.get('reason')}, "
            f"agents_failing={decision.get('agents_failing')})"
        )

    def _on_auth_success(agent_name: str) -> None:
        """Clear an agent's auth-failure record after a successful turn."""
        try:
            auth_tracker.record_success(agent_name)
        except Exception as e:
            _log(f"auth_alerts: tracker.record_success raised: {e}")

    async def _on_transport_failure_alert(agent_name: str, error_type: str) -> None:
        """Record a non-auth transport failure (#104) and alert the operator.

        Mirrors ``_on_auth_failure`` but routes to the per-class tracker and
        uses class-specific remedy text. Classes without a policy never reach
        here — the StopFailure endpoint gates on ``TRANSPORT_FAILURE_POLICIES``.
        Two-phase like the auth path: ``commit_alert`` only after a successful
        send, so a broker failure preserves the cooldown and retries.

        Per-class failures age out by window eviction (no success-clear hook):
        a transient rate_limit naturally drops below threshold once the agent
        recovers, which is exactly the "only alert if sustained" behaviour.
        """
        policy = TRANSPORT_FAILURE_POLICIES.get(error_type)
        tracker = transport_failure_trackers.get(error_type)
        if policy is None or tracker is None:
            return

        try:
            decision = tracker.record_failure(agent_name, error_type)
        except Exception as e:
            _log(f"transport_alerts: tracker.record_failure raised: {e}")
            return

        if not decision.get("should_alert"):
            return

        try:
            chat_id, platform = resolve_operator_chat(
                get_setting=agents.get_setting,
                list_all_approved_users=agents.list_all_approved_users,
            )
        except Exception as e:
            _log(f"transport_alerts: resolve_operator_chat raised: {e}")
            return

        if not chat_id:
            _log(
                f"transport_alerts: {error_type} alert for {agent_name} "
                "suppressed — no operator_chat_id and no approved_users fallback"
            )
            return

        try:
            host_label = agents.get_setting("host_label", "") or ""
        except Exception:
            host_label = ""

        body = format_alert_message(
            agent_name=agent_name,
            decision=decision,
            error=error_type,
            host_label=host_label,
            problem=policy.problem,
            remedy=policy.remedy,
        )

        try:
            await _broker_send(agent_name, platform, chat_id, body)
        except Exception as e:
            # Delivery failed — do NOT commit; next failure crossing threshold
            # retries instead of silencing the alert for the cooldown window.
            _log(
                f"transport_alerts: failed to send {error_type} alert via "
                f"{agent_name} bot to {platform}:{chat_id}: {e} "
                f"(cooldown not advanced — will retry on next failure)"
            )
            return

        try:
            tracker.commit_alert()
        except Exception as e:
            _log(f"transport_alerts: commit_alert raised: {e}")
        _log(
            f"transport_alerts: {error_type} operator alert sent to "
            f"{platform}:{chat_id} for {agent_name} "
            f"(reason={decision.get('reason')})"
        )

    async def _make_streaming_resume_handle_callback(agent_name: str, label: str):
        """Persist a streaming session's SDK resume handle when captured.

        Also logs auto context-restart events: an empty handle means
        force_restart() cleared the resume handle internally.

        Persistence layer (``agents.set_streaming_session_id`` / DB column
        ``streaming_session_id``) keeps its name for now; PR5 renamed only
        the in-memory surface. A DB-column / AgentRegistry rename is a
        deliberate follow-up to avoid bundling a migration into this PR.
        """
        async def _on_resume_handle(_agent_name: str, resume_handle: str):
            agents.set_streaming_session_id(agent_name, resume_handle, label=label)
            short_id = resume_handle[:12] if resume_handle else ""
            _log(f"streaming[{agent_name}/{label}]: persisted resume_handle {short_id}")
            if not resume_handle:
                # Empty = auto context restart triggered internally by the session
                try:
                    session_event_store.log(
                        session_id=f"{agent_name}-{label}",
                        agent_name=agent_name,
                        event_type="context_restart",
                        metadata={"label": label, "source": "auto"},
                    )
                    activity.log(
                        agent_name=agent_name,
                        event_type="context_restart",
                        title=f"{agent_name} auto context restart",
                        metadata={"label": label, "source": "auto"},
                    )
                except Exception:
                    pass
        return _on_resume_handle

    # Cache context usage per session to avoid blocking health checks
    _context_cache: dict[str, tuple[float, dict]] = {}  # session_id -> (timestamp, info)
    _CONTEXT_CACHE_TTL = 30.0  # noqa: N806 — seconds
    _context_fetch_in_progress: set[str] = set()

    async def _streaming_context_info(ss, *, force: bool = False) -> dict:
        """Best-effort context usage details for a streaming session.

        Uses a cache to avoid blocking callers when the SDK is busy.
        Returns cached data if a fresh fetch times out.
        """
        if not ss or ss.state != TransportSessionState.CONNECTED:
            return {}

        sid = ss.resume_handle or getattr(ss, "id", "")
        now = time.time()

        get_context_info = getattr(ss, "get_context_info", None)
        if not getattr(ss, "_client", None):
            if callable(get_context_info):
                try:
                    info = get_context_info() or {}
                except Exception:
                    info = {}
                if info:
                    _context_cache[sid] = (now, info)
                return info
            return {}

        # Return cache if fresh enough and not forced
        if not force and sid in _context_cache:
            cached_at, cached_info = _context_cache[sid]
            if now - cached_at < _CONTEXT_CACHE_TTL:
                return cached_info

        # Don't pile up concurrent fetches for the same session
        if sid in _context_fetch_in_progress:
            return _context_cache.get(sid, (0, {}))[1] if sid in _context_cache else {}

        _context_fetch_in_progress.add(sid)
        try:
            ctx = await asyncio.wait_for(ss._client.get_context_usage(), timeout=3.0)
            total = ctx.get("totalTokens", 0)
            reported_max = ctx.get("maxTokens", 0)
            actual_max = reported_max
            if (ss._config.model or "") in _1M_MODELS and reported_max <= 200_000:
                actual_max = 1_000_000
            # rawMaxTokens (SDK ≥ 0.1.x) is the raw model cap; maxTokens
            # is effective (autocompact buffer subtracted). Pass both
            # through so the frontend can show either; default to
            # actual_max if the SDK doesn't populate it (older SDK).
            raw_max = ctx.get("rawMaxTokens", 0) or actual_max
            pct = round(total / actual_max * 100, 1) if actual_max > 0 else 0.0
            info = {
                "total_tokens": total,
                "max_tokens": actual_max,
                "raw_max_tokens": raw_max,
                "percentage": pct,
                "categories": ctx.get("categories", []),
                "mcp_tools": ctx.get("mcpTools", []),
            }
            _context_cache[sid] = (now, info)
            return info
        except Exception:
            # Return stale cache on timeout rather than empty
            if sid in _context_cache:
                return _context_cache[sid][1]
            return {}
        finally:
            _context_fetch_in_progress.discard(sid)

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
            "state": "connected" if ss.state == TransportSessionState.CONNECTED else "idle",
            "context_used_pct": pct,
            "message_count": ss._stats.get("messages_sent", 0) + ss._stats.get("turns", 0),
            "needs_restart": bool(ctx) and pct >= ss._config.context_restart_pct,
            "streaming": True,
            "label": label,
            "connected": ss.state == TransportSessionState.CONNECTED,
            "sdk_session_id": ss.resume_handle[:12] if ss.resume_handle else "",
        }

    def _isolation_block_reason(agent_name: str) -> tuple[int, str] | None:
        """#149 phase-3 respawn guard (single source of truth).

        If ``agent_name``'s ``isolation_mode`` has no runnable provisioner,
        return ``(http_status, detail)``; else ``None``. Non-raising so HTTP
        callers and the broker (which has no HTTP context) share one check.

        The field is accepted + persisted at registration (forward-compatible),
        but ``get_provisioner`` fails closed on unimplemented modes. Consulting
        this before EVERY transport (re)spawn — cold start, reconnect, restart,
        idle auto-wake — makes that guarantee operational: a unix_user-labeled
        agent (incl. one updated from local) can never relaunch under the
        daemon uid with none of the requested OS isolation. inc3c+ lifts it for
        unix_user once the provisioner + lifecycle wiring land. (Murzik #642 P1.)
        """
        agent = agents.get(agent_name)
        if not agent:
            return None
        mode = (getattr(agent, "isolation_mode", "") or "local").strip() or "local"
        try:
            from pinky_daemon.provisioning import get_provisioner
            get_provisioner(mode)
            return None
        except NotImplementedError as e:
            return (501, f"isolation_mode '{mode}' for agent '{agent_name}' is not runnable yet: {e}")
        except ValueError as e:
            return (400, f"invalid isolation_mode for agent '{agent_name}': {e}")

    def _enforce_isolation_runnable(agent_name: str) -> None:
        """Raise HTTPException if ``agent_name`` can't be (re)spawned under its
        isolation_mode. The HTTP-context wrapper around _isolation_block_reason."""
        blocked = _isolation_block_reason(agent_name)
        if blocked:
            status, detail = blocked
            _log(f"api: refusing to start {agent_name}: {detail}")
            raise HTTPException(status, detail)

    async def _start_streaming_session(
        agent_name: str,
        *,
        label: str = "main",
        resume_id: str = "",
    ):
        """Create, connect, and register a streaming session for an agent label."""
        from pinky_daemon.codex_session import CodexSession
        from pinky_daemon.streaming_session import (
            DEFAULT_STREAMING_ALLOWED_TOOLS,
            StreamingSession,
            StreamingSessionConfig,
        )
        from pinky_daemon.tmux_session import TmuxSession

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

        # Build AgentDefinition objects for peer agents (subagent spawning)
        subagents = {}
        try:
            from claude_agent_sdk import AgentDefinition
            for peer in agents.list():
                if peer.name == agent_name or not peer.enabled:
                    continue
                subagents[peer.name] = AgentDefinition(
                    description=(
                        f"{peer.display_name or peer.name} — {peer.role or 'AI agent'}. "
                        f"Use to delegate tasks or send messages to {peer.name}."
                    ),
                    prompt=peer.soul or f"You are {peer.display_name or peer.name}, an AI agent.",
                    model=peer.model or "inherit",
                )
        except Exception as e:
            _log(f"api: could not build subagent definitions: {e}")

        # In shared MCP mode, compute SDK-side disallowed tools for this agent
        # (shared server runs ALL gates; filtering is client-side)
        effective_disallowed = list(agent.disallowed_tools or [])
        if SHARED_MCP_ENABLED:
            shared_disallowed = _get_shared_mode_disallowed_tools(agent_name, skills)
            effective_disallowed.extend(shared_disallowed)
            # Deduplicate
            effective_disallowed = sorted(set(effective_disallowed))

        def runtime_from_legacy_provider(agent_config) -> str:
            """Deprecated temporary shim for the runtime rollout.

            Runtime selection now belongs to agents.runtime. This fallback exists
            only for legacy rows that have not passed the one-shot codex_cli
            provider_url backfill yet, and should be removed after rollout.
            """
            runtime = (getattr(agent_config, "runtime", "") or "").strip()
            if runtime:
                return runtime
            if (getattr(agent_config, "provider_url", "") or "").strip() == "codex_cli":
                return "codex_cli"
            return "claude_sdk"

        runtime = runtime_from_legacy_provider(agent)
        transport = (getattr(agent, "transport", "") or "sdk").strip() or "sdk"
        if runtime == "opencode":
            if os.environ.get("PINKY_ENABLE_OPENCODE", "0") == "1":
                msg = "opencode runtime is enabled but OpencodeSession is not implemented yet"
                status = 501
            else:
                msg = "opencode runtime is disabled; set PINKY_ENABLE_OPENCODE=1 after implementation lands"
                status = 503
            _log(f"api: refusing to start {agent_name}: {msg}")
            raise HTTPException(status, msg)
        if runtime not in {"claude_sdk", "codex_cli"}:
            msg = f"unknown runtime '{runtime}' for agent '{agent_name}'"
            _log(f"api: {msg}")
            raise HTTPException(400, msg)
        if transport not in {"sdk", "tmux"}:
            msg = f"unknown transport '{transport}' for agent '{agent_name}'"
            _log(f"api: {msg}")
            raise HTTPException(400, msg)
        if runtime != "claude_sdk" and transport != "sdk":
            msg = (
                f"transport '{transport}' is only valid for claude_sdk runtime "
                f"(agent '{agent_name}' has runtime '{runtime}')"
            )
            _log(f"api: {msg}")
            raise HTTPException(400, msg)

        # #149 phase-3 cold-start guard (see _enforce_isolation_runnable).
        _enforce_isolation_runnable(agent_name)

        is_codex = runtime == "codex_cli"
        is_tmux = runtime == "claude_sdk" and transport == "tmux"
        resolved_provider_url, resolved_provider_key, resolved_provider_model = _resolve_agent_provider(agent)
        if is_codex:
            resolved_provider_url = "codex_cli"
        effective_model = resolved_provider_model or agent.model

        # Build MCP server config for Codex agents (injected via -c flags)
        codex_mcp_servers = {}
        if is_codex and SHARED_MCP_ENABLED:
            shared_base = f"http://{SHARED_MCP_HOST}:{SHARED_MCP_PORT}"
            agent_headers = {"X-Agent-Name": agent_name}
            for srv_name in ("self", "memory", "messaging"):
                codex_mcp_servers[f"pinky-{srv_name}"] = {
                    "url": f"{shared_base}/mcp/{srv_name}/http/mcp",
                    "headers": agent_headers,
                }

        # #429: ensure verify_effort hook is installed in the agent's
        # .claude/settings.json before we spin up the session. No-op for
        # agents whose workspace already has it.
        try:
            agents.ensure_workspace_hooks(agent_name)
        except Exception as e:
            _log(f"streaming-start: ensure_workspace_hooks({agent_name}) — {e}")

        config = StreamingSessionConfig(
            agent_name=agent_name,
            label=label,
            model=effective_model,
            working_dir=work_dir,
            allowed_tools=effective_tools,
            disallowed_tools=effective_disallowed,
            mcp_servers=codex_mcp_servers,
            permission_mode=agent.permission_mode or "bypassPermissions",
            max_turns=agent.max_turns,
            system_prompt=agents.build_system_prompt(agent_name, skill_store=skills),
            resume_handle=resume_id,
            # commit=False here: the connect-time rebuild via
            # ``wake_context_builder`` is the delivered (committed) build
            # that consumes inbox + restart_manifest. An eager committed
            # build here would consume both before the delivered one
            # runs, leaving the actual wake prompt without unread inbox
            # or restart-manifest content (Murzik P1#1).
            wake_context=_build_streaming_wake_context(agent_name, commit=False),
            wake_context_builder=_build_streaming_wake_context,
            on_wake_delivered=_log_agent_wake_event,
            restart_guard=lambda session, _agent_name=agent_name: _get_streaming_restart_guard(_agent_name, session),
            live_status_fn=lambda _agent_name=agent_name: _agent_live_status.get(_agent_name),
            context_warn_pct=warn_pct,
            context_restart_pct=restart_pct,
            timezone=agents.get_owner_profile().get("timezone", "America/Los_Angeles"),
            subagents=subagents,
            provider_url=resolved_provider_url,
            provider_key=resolved_provider_key,
            thinking_effort=agent.thinking_effort or "medium",
            strict_effort_enforcement=bool(
                getattr(agent, "strict_effort_enforcement", False)
            ),
            idle_timeout=max(0, agent.auto_sleep_hours) * 3600,
        )

        callback = await _make_streaming_response_callback()
        sid_callback = await _make_streaming_resume_handle_callback(agent_name, label)

        # Select session class based on persisted runtime + Claude transport.
        if is_tmux:
            SessionClass = TmuxSession  # noqa: N806
        elif is_codex:
            SessionClass = CodexSession  # noqa: N806
        else:
            SessionClass = StreamingSession  # noqa: N806

        init_kwargs = {
            "response_callback": callback,
            "conversation_store": store,
            "cost_callback": _make_cost_callback(agents),
            "analytics_store": analytics,
            "registry": agents,
        }
        # Auth-failure detection is currently only wired for the SDK-based
        # StreamingSession. Codex sessions don't surface an equivalent
        # "authentication_failed" signal, and TmuxSession relies on the
        # interactive Claude REPL + hook/tailer path, so skip the hooks there.
        if not is_codex and not is_tmux:
            init_kwargs["auth_alert_callback"] = _on_auth_failure
            init_kwargs["auth_success_callback"] = _on_auth_success
        if is_codex or is_tmux:
            init_kwargs["stream_event_callback"] = await _make_streaming_event_callback(agent_name, label)

        ss = SessionClass(config, **init_kwargs)
        ss._on_resume_handle = sid_callback
        try:
            await _bounded_cold_start_connect(
                ss,
                agent_name=agent_name,
                label=label,
                timeout=COLD_START_CONNECT_TIMEOUT_SEC,
            )
        except asyncio.TimeoutError:
            # Cold-start wedged past the umbrella (#110). The session was NOT
            # registered and was best-effort disconnected by the helper. Alert
            # the owner + audit, then propagate so callers (boot loop /
            # _ensure_streaming_session) surface the failure normally.
            try:
                activity.log(
                    agent_name=agent_name,
                    event_type="cold_start_timeout",
                    title=(
                        f"Cold-start connect timed out after "
                        f"{COLD_START_CONNECT_TIMEOUT_SEC:.0f}s ({label})"
                    ),
                    metadata={"label": label, "resume_id": resume_id or ""},
                )
                owner = agents.get_owner_profile()
                owner_chat = owner.get("chat_id", "") if owner else ""
                if owner_chat:
                    await broker.send_callback(
                        agent_name=agent_name,
                        platform="telegram",
                        chat_id=str(owner_chat),
                        text=(
                            f"⚠️ {agent_name} cold-start connect timed out after "
                            f"{COLD_START_CONNECT_TIMEOUT_SEC:.0f}s ({label}) — "
                            f"session not started."
                        ),
                    )
            except Exception as ae:
                _log(
                    f"streaming-start: cold-start timeout alert failed for "
                    f"{agent_name}: {ae}"
                )
            raise
        broker.register_streaming(agent_name, ss, label=label)

        # Log session lifecycle event
        event_type = "session_resume" if resume_id else "session_start"
        try:
            analytics.ensure_session_fact(
                session_id=ss.id,
                agent_name=agent_name,
                session_label=label,
                provider="tmux" if is_tmux else (runtime if is_codex else (resolved_provider_url or "default")),
                model=effective_model or "",
            )
            analytics.log_activity(
                session_id=ss.id,
                agent_name=agent_name,
                event_type=event_type,
                metadata={"label": label, "resume_id": resume_id or ""},
            )
            session_event_store.log(
                session_id=ss.id,
                agent_name=agent_name,
                event_type=event_type,
                metadata={"label": label, "resume_id": resume_id or ""},
            )
            # ``agent_wake`` is logged centrally via ``on_wake_delivered``
            # (api._log_agent_wake_event) which fires from the session's
            # connect-time wake-prompt delivery. Logging it here would
            # double-count and undercut the #591 cycle-gate (Murzik P1#2):
            # the boundary would advance BEFORE the actual wake prompt
            # lands, so a delivery-failure could eat the directive while
            # the gate thinks the wake "happened."
        except Exception as _e:
            _log(f"api: failed to log session start event for {agent_name}: {_e}")

        return ss

    async def _ensure_streaming_session(agent_name: str, *, label: str = "main"):
        """Return a connected streaming session for an agent label.

        State-aware so RECONNECTING doesn't race an in-flight reconnect
        with a second connect() (per @murzik PR #492 review). Branches:
          - CONNECTED       → return as-is
          - IDLE_SLEEPING   → connect() to wake
          - RECONNECTING    → wait bounded for the in-flight reconnect to
                              settle to CONNECTED; if it doesn't, return
                              the session anyway and let the caller's own
                              error handling kick in (matches the existing
                              "not running" fallback shape downstream)
          - DEAD / UNINITIALIZED → connect() (this is the auto-start /
                              resurrection path; explicit and intentional)
        """
        sessions = broker._streaming.get(agent_name, {})
        ss = sessions.get(label)
        if ss:
            state = ss.state
            if state == TransportSessionState.CONNECTED:
                return ss
            if state == TransportSessionState.RECONNECTING:
                # Wait for the in-flight reconnect to land. Use the same
                # bounded poll the broker uses on the inbound path so the
                # waits stay consistent.
                from pinky_daemon.broker import (
                    _INBOUND_RECONNECT_POLL_SEC,
                    _INBOUND_RECONNECT_WAIT_SEC,
                )
                deadline = time.monotonic() + _INBOUND_RECONNECT_WAIT_SEC
                while time.monotonic() < deadline:
                    await asyncio.sleep(_INBOUND_RECONNECT_POLL_SEC)
                    if ss.state == TransportSessionState.CONNECTED:
                        break
                return ss
            # IDLE_SLEEPING / DEAD / UNINITIALIZED → explicit connect().
            # #149 P1: this relaunches the transport for an existing session
            # object — guard it too, else a local→unix_user update could wake
            # the old local session under the daemon uid (Murzik #642 review).
            _enforce_isolation_runnable(agent_name)
            await ss.connect()
            return ss

        resume_id = agents.get_streaming_session_id(agent_name, label=label)
        return await _start_streaming_session(agent_name, label=label, resume_id=resume_id)

    # Wire the broker's cold-wake path so inbound platform messages
    # (Telegram, Discord, etc.) can start a fresh streaming session for a
    # sibling agent that was never auto-started at boot. Without this,
    # `MessageBroker._route_streaming` would only handle the "session
    # exists but disconnected" case and fall through to "not running" for
    # any sibling that hasn't been touched via the web admin chat path.
    broker.set_ensure_session_callback(_ensure_streaming_session)
    # #149 P1: guard the broker's idle auto-wake respawn path too (it calls
    # connect() directly, bypassing _ensure_streaming_session). Shares the same
    # block-reason check; the broker logs+skips rather than raising HTTP.
    broker.set_isolation_guard(_isolation_block_reason)

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

            # Log session disconnect event
            try:
                session_event_store.log(
                    session_id=ss.id,
                    agent_name=agent_name,
                    event_type="session_disconnect",
                    metadata={"label": label},
                )
                activity.log(
                    agent_name=agent_name,
                    event_type="agent_sleep",
                    title=f"{agent_name} session disconnected",
                    metadata={"label": label, "session_id": ss.id},
                )
            except Exception as _e:
                _log(f"api: failed to log session disconnect for {agent_name}: {_e}")

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
    presentations = PresentationStore(db_path=db_path.replace(".db", "_presentations.db"))
    app_store = AppStore(db_path=db_path.replace(".db", "_apps.db"))
    trigger_store = TriggerStore(db_path=db_path.replace(".db", "_triggers.db"))
    mesh_store = MeshStore(db_path=db_path.replace(".db", "_mesh.db"))

    # Knowledge Base — project-level, all agents share
    _data_dir = Path(db_path).parent
    kb = KBStore(data_dir=_data_dir)

    # KB Librarian runner — curates wiki pages from raw sources
    librarian_runner = LibrarianRunner(
        kb_store=kb,
        db_path=db_path.replace(".db", "_librarian_state.db"),
    )
    app.state.librarian_runner = librarian_runner

    # Librarian debounce + running lock (global — KB is shared)
    # Mutable container so routes/kb.py can read/write the same state.
    _librarian_state = SimpleNamespace(
        timer=None,           # asyncio.TimerHandle | None
        running=False,
        pending=False,
        auto_run=True,        # Global toggle
    )

    def _schedule_librarian() -> None:
        """Schedule a librarian run after the debounce window."""
        from pinky_daemon.librarian_runner import LIBRARIAN_DEBOUNCE_S

        if not _librarian_state.auto_run:
            return

        # Cancel existing timer (resets the debounce)
        if _librarian_state.timer is not None:
            _librarian_state.timer.cancel()

        loop = asyncio.get_event_loop()
        _librarian_state.timer = loop.call_later(
            LIBRARIAN_DEBOUNCE_S,
            lambda: asyncio.ensure_future(_trigger_librarian()),
        )
        _log(f"librarian: debounce timer set ({LIBRARIAN_DEBOUNCE_S}s)")

    # ── KB Auto-Ingest ────────────────────────────────────────
    # Automatically ingest substantive data into KB raw sources.
    # Only high-signal material: published research, people profiles, project state.

    def _kb_auto_ingest(
        title: str,
        content: str,
        source_type: str = "note",
        filed_by: str = "system",
        tags: list[str] | None = None,
        source_url: str | None = None,
    ) -> bool:
        """Ingest content into KB raw sources and trigger librarian.

        For snapshot sources (with source_url like internal://...),
        replaces the previous version by overwriting the file in place.
        For one-off sources, skips exact content duplicates.

        Returns True if ingested/updated, False if duplicate or empty.
        """
        if not content or not content.strip():
            return False

        # Check for existing source by URL (snapshot replace) or content (dedup)
        existing = kb.check_duplicate(source_url=source_url, content=content)
        if existing:
            if source_url and existing.source_url == source_url:
                # Snapshot replace: overwrite the file with updated content
                try:
                    file_path = existing.file_path
                    if file_path and Path(file_path).exists():
                        from datetime import datetime
                        from datetime import timezone as tz
                        now_iso = datetime.now(tz.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                        frontmatter = (
                            f"---\nid: {existing.id}\ntitle: {title}\n"
                            f"source_type: {source_type}\nfiled_at: {now_iso}\n"
                            f"filed_by: {filed_by}\ntags: {tags or []}\n"
                            f"source_url: {source_url}\n---\n\n"
                        )
                        Path(file_path).write_text(frontmatter + f"# {title}\n\n{content}")
                        _log(f"kb-auto-ingest: updated snapshot '{title}' ({existing.id})")
                        _schedule_librarian()
                        return True
                except Exception as e:
                    _log(f"kb-auto-ingest: snapshot update failed for '{title}': {e}")
                    return False
            else:
                # Content duplicate — skip
                _log(f"kb-auto-ingest: skipped duplicate '{title}' (existing: {existing.id})")
                return False

        try:
            source = kb.ingest(
                title=title,
                content=content,
                source_url=source_url,
                source_type=source_type,
                filed_by=filed_by,
                tags=tags or [],
            )
            _log(f"kb-auto-ingest: filed '{title}' as {source.id}")
            _schedule_librarian()
            return True
        except Exception as e:
            _log(f"kb-auto-ingest: failed to file '{title}': {e}")
            return False

    def _kb_ingest_research(topic_id: int) -> None:
        """Auto-ingest a published research brief into KB."""
        brief = research.get_latest_brief(topic_id)
        topic = research.get_topic(topic_id)
        if not brief or not topic:
            return

        tags = list(topic.tags) if topic.tags else []
        tags.append("research")

        _kb_auto_ingest(
            title=f"Research: {topic.title}",
            content=brief.content,
            source_type="article",
            filed_by=brief.author_agent or "research",
            tags=tags,
        )

    def _kb_ingest_people_profiles() -> None:
        """Snapshot all known people profiles into KB as a single source."""
        all_users = user_profiles.get_all_users()
        if not all_users:
            return

        sections = []
        for chat_id in all_users:
            entries = user_profiles.get_user_profile(chat_id, min_confidence=0.3)
            if not entries:
                continue

            # Get a display name from identity entries
            name = chat_id
            for e in entries:
                if e.category == "identity" and e.key in ("name", "display_name", "first_name"):
                    name = e.value
                    break

            lines = [f"## {name} (chat_id: {chat_id})"]
            by_cat: dict[str, list] = {}
            for e in entries:
                by_cat.setdefault(e.category, []).append(e)

            for cat, cat_entries in sorted(by_cat.items()):
                lines.append(f"**{cat.title()}:**")
                for e in cat_entries:
                    lines.append(f"- {e.key}: {e.value}")

            # Add relationships
            rels = user_profiles.get_relationships(chat_id)
            if rels:
                lines.append("**Relationships:**")
                for r in rels:
                    lines.append(f"- {r.to_display_name}: {r.relation}")

            sections.append("\n".join(lines))

        if not sections:
            return

        content = "# People Profiles\n\nKnown information about people in the owner's circle.\n\n" + "\n\n---\n\n".join(sections)

        _kb_auto_ingest(
            title="People Profiles Snapshot",
            content=content,
            source_type="note",
            source_url="internal://people-profiles-snapshot",
            filed_by="system",
            tags=["people", "profiles"],
        )

    def _kb_ingest_project_state() -> None:
        """Snapshot active projects and recent completed tasks into KB."""
        all_projects = tasks.list_projects()
        completed = tasks.list(status="completed", limit=20)

        sections = []

        if all_projects:
            proj_lines = ["## Active Projects"]
            for p in all_projects:
                if p.status == "archived":
                    continue
                proj_lines.append(f"### {p.name}")
                if p.description:
                    proj_lines.append(p.description)
                proj_lines.append(f"- Status: {p.status}")
                if p.repo_url:
                    proj_lines.append(f"- Repo: {p.repo_url}")
                # Get project tasks
                proj_tasks = tasks.list(project_id=p.id, limit=50)
                if proj_tasks:
                    by_status: dict[str, list] = {}
                    for t in proj_tasks:
                        by_status.setdefault(t.status, []).append(t)
                    for status, status_tasks in sorted(by_status.items()):
                        proj_lines.append(f"- {status}: {len(status_tasks)} tasks")
                proj_lines.append("")
            sections.append("\n".join(proj_lines))

        if completed:
            comp_lines = ["## Recently Completed Tasks"]
            for t in completed:
                comp_lines.append(f"- **{t.title}** (by {t.assigned_agent or 'unassigned'})")
                # Get completion comment
                comments = tasks.get_comments(t.id)
                if comments:
                    last = comments[-1]
                    comp_lines.append(f"  Summary: {last.content[:200]}")
            sections.append("\n".join(comp_lines))

        if not sections:
            return

        content = "# Project & Task State\n\nCurrent project status and recent task completions.\n\n" + "\n\n".join(sections)

        _kb_auto_ingest(
            title="Project State Snapshot",
            content=content,
            source_type="note",
            source_url="internal://project-state-snapshot",
            filed_by="system",
            tags=["projects", "tasks"],
        )

    async def _trigger_librarian() -> None:
        """Run the librarian, with running lock and pending re-run support."""
        _librarian_state.timer = None

        if _librarian_state.running:
            _librarian_state.pending = True
            _log("librarian: already running — will re-run after current finishes")
            return

        if not librarian_runner.should_run():
            _log("librarian: no new sources — skipping")
            return

        # Resolve an agent to use for working_dir (use main agent)
        main_name = agents.get_main_agent()
        if not main_name:
            _log("librarian: no main agent configured — skipping")
            return
        agent = agents.get(main_name)
        if not agent:
            return

        _librarian_state.running = True
        try:
            _log("librarian: starting run (triggered by ingest debounce)")
            stats = await librarian_runner.run(main_name, agent)
            _log(f"librarian: done — sources={stats.get('sources_processed', 0)}")
        except Exception as e:
            _log(f"librarian: run failed: {e}")
        finally:
            _librarian_state.running = False
            if _librarian_state.pending:
                _librarian_state.pending = False
                _schedule_librarian()  # New data arrived during run — go again

    # ── Migration router ──────────────────────────────────────────────────────
    try:
        from pinky_daemon.migration.routes import router as migration_router
        from pinky_daemon.migration.routes import set_dependencies as _migration_set_deps

        # Wire shared agent_registry; memory_store is per-agent so we leave it None
        # here — the importer opens the correct DB path when applying memories.
        _migration_set_deps(agent_registry=agents, memory_store=None)
        app.include_router(migration_router)
        _log("migration: OpenClaw migration module loaded")
    except ImportError as _e:
        _log(f"migration: module not available — {_e}")

    # ── Voice router ─────────────────────────────────────────────────────────
    try:
        from pinky_daemon.voice_routes import router as voice_router
        from pinky_daemon.voice_routes import set_dependencies as _voice_set_deps
        from pinky_daemon.voice_store import VoiceStore

        _voice_store = VoiceStore(db_path="data/voice_calls.db")
        _voice_base_url = (
            agents.get_setting("PINKY_BASE_URL")
            or os.environ.get("PINKY_BASE_URL", "")
        )
        _voice_set_deps(
            voice_store=_voice_store,
            agents=agents,
            broker_send=_broker_send,
            base_url=_voice_base_url,
        )
        app.include_router(voice_router)
        _log("voice: Twilio voice module loaded")

        # WS endpoint registered at end of create_api() at /ws/voice/{id}
    except ImportError as _e:
        _log(f"voice: module not available — {_e}")

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
    import datetime as _dt
    # UTC so the version string is deterministic regardless of where the
    # daemon runs (was previously implicit local-tz). #294
    _now = _dt.datetime.now(_dt.timezone.utc)
    _pinky_version = f"{_now.strftime('%y')}.{int(_now.strftime('%m')):02d}.{_git_hash}"

    app.state.frontend_build_status = _frontend_build_status(
        str(_pkg_root), current_git_hash=_git_hash,
    )
    _frontend_status = app.state.frontend_build_status.get("status")
    if _frontend_status in {"missing", "broken", "stale"}:
        _log(
            "frontend: WARNING "
            f"{app.state.frontend_build_status.get('message', _frontend_status)}"
        )
    elif _frontend_status == "unverified":
        _log(
            "frontend: "
            f"{app.state.frontend_build_status.get('message', _frontend_status)}"
        )

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
        "/landing",
        "/auth/login",
        "/auth/logout",
        "/auth/status",
        "/auth/setup",
        "/favicon.svg",
        "/icons.svg",
    }
    _public_prefixes = (
        "/assets/", "/static/", "/p/", "/hooks/",
        # Public app viewer — a shared app is opened by share token at
        # /a/{share_token} with no session (the link is the credential). The
        # management surface (/apps) stays protected; only the /a/ viewer is
        # public. Listed here so deny-by-default never 401s a valid share link.
        "/a/",
        # Twilio voice callbacks — authenticated via X-Twilio-Signature, not session
        "/api/voice/twiml/", "/api/voice/status/", "/api/voice/amd/",
        # ConversationRelay WebSocket — auth via session-ID-in-path (opaque UUID)
        "/ws/voice/",
        # Google OAuth callback — Google's redirect arrives cross-site WITHOUT our
        # SameSite=strict cookie; the route self-authenticates via a one-time state
        # nonce. Must stay public even though the rest of /calendar is now protected.
        # (Checked here in gate step 1, BEFORE the _protected_api_prefixes deny.)
        "/calendar/google/callback",
    )
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
        "/presentations",
        "/presentation-templates",
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
        "/kb",
        "/migration",
        "/auth/password",
        "/api/voice",
        # Admin-control, credential, and PII surfaces — require a session
        # cookie or signed internal auth like the rest of the list. Legitimate
        # callers already authenticate: the dashboard via the session cookie
        # (api.js credentials:'same-origin'), agents/MCP via signed internal
        # headers (build_internal_auth_headers). Public carve-outs that must
        # stay reachable live in _public_prefixes above
        # (/calendar/google/callback) or under a different prefix (the public
        # app viewer is /a/{share_token}, NOT /apps).
        "/admin",          # restart / update / channel / watchdog (owner + agent)
        "/providers",      # LLM provider API keys
        "/bot-tokens",     # platform bot tokens
        "/user-profiles",  # owner/contact PII (also assembled into the system prompt)
        "/calendar",       # CalDAV / Google credentials + config
        "/apps",           # app management (deploy/share/upload); public viewer is /a/*
        "/models",         # model registry management
        # #506 deny-by-default coverage: these surfaces previously fell through
        # the auth gate (reachable unauthenticated). All are dashboard/agent
        # management or read surfaces — the dashboard reaches them with the
        # session cookie, agents via signed internal headers. None is a public
        # or external-callback route (cross-fleet *delivery* uses the ferry/NATS
        # transport, not these HTTP routes; /federation + /mesh here are local
        # peer CRUD + operator diagnostics).
        "/analytics",      # agent usage/cost analytics (dashboard)
        "/api/migrate",    # OpenClaw migration (admin)
        "/comms",          # inter-agent comms inboxes/messages
        "/dreams",         # dream-runner data
        "/federation",     # federation peer CRUD (local management)
        "/mesh",           # mesh operator diagnostics (local)
        "/milestones",     # project milestones
        "/plugins",        # plugin registry management
        "/render",         # server-side PDF/markdown rendering
        "/schedules",      # cron/wake schedules
        "/soul-templates", # soul template registry
        "/sprints",        # sprint management
        "/triggers",       # webhook/url trigger management
    )

    # Single source of truth for the auth gate's route classification. Exposed
    # on app.state so the coverage audit (scripts/audit_route_auth_coverage.py)
    # and its regression test classify against the EXACT sets the middleware
    # uses — no drifting duplicate lists (#506).
    app.state.auth_route_sets = {
        "public_exact": frozenset(_public_exact_paths),
        "public_prefixes": tuple(_public_prefixes),
        "protected_html": frozenset(_protected_html_paths),
        "protected_api_prefixes": tuple(_protected_api_prefixes),
    }

    # Deny-by-default migration (#506). Modes:
    #   off     — legacy fall-through: an unauthenticated request to a path that
    #             is neither public nor under a protected prefix reaches the
    #             route (the historical gap).
    #   shadow  — same as off, but log every would-deny so we can soak prod and
    #             see which unclassified surfaces actually get unauth traffic
    #             before enforcing.
    #   enforce — 401 the gap closed.
    # Default shadow: behaviour-identical to off (still falls through) but emits
    # the soak signal. Flip to enforce via env once the soak is clean.
    _auth_deny_mode = os.environ.get("PINKY_AUTH_DENY_DEFAULT", "shadow").strip().lower()
    if _auth_deny_mode not in ("off", "shadow", "enforce"):
        _log(f"auth: unknown PINKY_AUTH_DENY_DEFAULT={_auth_deny_mode!r} — defaulting to 'shadow'")
        _auth_deny_mode = "shadow"

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
        agent_name = request.headers.get(INTERNAL_AGENT_HEADER, "")
        timestamp = request.headers.get(INTERNAL_TIMESTAMP_HEADER, "")
        signature = request.headers.get(INTERNAL_SIGNATURE_HEADER, "")
        # Per-agent signing key (#623): dual-accept alongside the global secret.
        # The agent's own key (if provisioned) is checked first; the global
        # secret remains valid through the migration window.
        agent_key = None
        if agent_name:
            try:
                agent_key = agents.get_signing_key(agent_name)
            except Exception:
                agent_key = None
        # #149 phase-3 inc2: the global secret is a universal bearer credential
        # (accepted for every name), so it may authenticate ONLY a proven
        # existing non-isolated agent. Isolated callers must use their own
        # per-agent key; unknown names and registry errors are denied the global
        # secret entirely (fail closed) — otherwise a leaked secret authenticates
        # as any made-up identity (Murzik #640). Per-agent keys are provisioned
        # fleet-wide (#623) so legitimate callers are unaffected.
        allow_global_secret = _global_secret_allowed_for(agent_name)
        usable_secret = secret if allow_global_secret else ""
        if not usable_secret and not agent_key:
            return False
        return verify_internal_request(
            secret,
            agent_name=agent_name,
            method=request.method,
            path=request.url.path,
            timestamp=timestamp,
            signature=signature,
            agent_key=agent_key,
            allow_global_secret=allow_global_secret,
        )

    def _internal_isolation_denied(request: Request, caller_name: str) -> bool:
        """#149 tenant isolation (PATH-target surfaces): an authenticated
        *isolated* agent may only act on its OWN resources. Returns True (deny)
        when the caller is isolated and the request path targets a DIFFERENT
        agent (``/agents/{other}/*`` or ``/autonomy/{other}/*``).

        Only reached after _has_valid_internal_auth succeeds, so ``caller_name``
        is the verified signed identity (the signature binds the name). The
        cheap path checks (no agent-target match, or target == caller) short-
        circuit BEFORE any registry lookup, so the hot path (self-calls, non-
        targeted endpoints) pays no extra DB read — only a genuine cross-agent
        target triggers the isolated-flag lookup.

        Body-actor surfaces (/broker/*, where the sender is named in the request
        body, not the path) are NOT covered here — they're enforced in the
        handlers via _deny_isolated_cross_actor(). Reading the request body in
        BaseHTTPMiddleware is fragile, so the body check lives where the body is
        already parsed.

        Defense-in-depth on top of tool-gating: isolated agents are provisioned
        without admin/register gates, but this enforces the tenant boundary at
        the daemon regardless of what tools the agent manages to invoke.
        """
        if not caller_name:
            return False
        target = _isolation_path_target(request.url.path)
        if target is None:
            return False  # path does not name a specific agent
        if target == caller_name:
            return False  # acting on self — always allowed, no lookup
        # Genuine cross-agent target. Resolve the caller's isolation flag.
        # Fail CLOSED: a registry error means we cannot prove the caller is
        # non-isolated, so deny this (already cross-agent shaped) request rather
        # than risk a tenant escape.
        try:
            caller = agents.get(caller_name)
        except Exception as e:
            _log(
                f"isolation-check: registry lookup failed for '{caller_name}' "
                f"on {request.url.path}: {e} — failing closed (deny)"
            )
            return True
        if caller and getattr(caller, "isolated", False):
            _log(
                f"isolation: denied isolated agent '{caller_name}' cross-agent "
                f"access to {request.url.path}"
            )
            return True
        return False

    def _is_isolated_agent(name: str) -> bool:
        """True iff ``name`` resolves to an agent with the #149 isolated flag.
        Fails CLOSED (returns True) on registry error so a body-actor guard
        denies rather than risks a tenant escape when isolation can't be
        verified."""
        if not name:
            return False
        try:
            agent = agents.get(name)
        except Exception as e:
            _log(f"isolation-check: registry lookup failed for '{name}': {e} — failing closed")
            return True
        return bool(agent and getattr(agent, "isolated", False))

    def _global_secret_allowed_for(name: str) -> bool:
        """#149 phase-3 inc2: may the shared global secret authenticate a request
        claiming ``name``? Only for a PROVEN existing, non-isolated agent.

        The global secret is a universal bearer credential — it is accepted for
        every name, not bound to an identity. So it must NOT authenticate:
          * an isolated agent (it must use its own per-agent key), nor
          * an UNKNOWN / unregistered name (else a leaked secret authenticates as
            any made-up identity — Murzik's #640 ``ghost`` finding), nor
          * anything we can't resolve (registry error → fail CLOSED).
        Distinct from ``_is_isolated_agent`` (which fails closed to *isolated*):
        here every uncertain case must DENY the global secret, so unknown agents
        and registry errors both return False. Per-agent keys are provisioned
        fleet-wide (#623), so legitimate callers are unaffected — every internal
        signer (hooks, pinky-self/messaging MCP) claims a registered agent name.

        SCOPE (residual, tracked in #638): this still lets a leaked global secret
        impersonate an *existing non-isolated* agent. Fully closing that needs the
        fleetwide global-secret drop (#623 inc4) and/or the per-user OS isolation
        (#638 inc3+) that prevents an isolated tenant from reading the secret.
        """
        if not name:
            return False
        try:
            agent = agents.get(name)
        except Exception as e:
            _log(f"auth: global-secret check registry lookup failed for '{name}': {e} — denying global")
            return False
        if agent is None:
            return False
        return not bool(getattr(agent, "isolated", False))

    def _deny_isolated_cross_actor(request: Request, body_agent: str) -> None:
        """#149 body-actor guard for /broker/* (and any handler whose acting
        agent comes from the request body). Raises 403 when an *isolated*
        caller's body names a DIFFERENT agent — i.e. an attempt to send/act AS
        another agent. No-op for non-isolated callers (full-trust inner-fleet
        agents may act broadly) and for non-internal callers (operator/browser
        sessions don't carry a verified internal-agent header).

        The caller identity is the signature-verified internal header, set only
        after _has_valid_internal_auth succeeds in the middleware; a session/
        browser request has no such header and is left untouched here.
        """
        caller = request.headers.get(INTERNAL_AGENT_HEADER, "")
        if not caller or not body_agent or body_agent == caller:
            return
        if _is_isolated_agent(caller):
            _log(
                f"isolation: denied isolated agent '{caller}' from acting as "
                f"'{body_agent}' on {request.url.path}"
            )
            raise HTTPException(
                status_code=403,
                detail="isolated agent may only act as itself",
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
        # Skip auth for WebSocket upgrades — WS handlers do their own auth
        if request.headers.get("upgrade", "").lower() == "websocket":
            return await call_next(request)

        # NOTE on the unconfigured-secret case (PINKY_SESSION_SECRET unset):
        # we do NOT short-circuit to call_next here. Doing so would make
        # every protected surface (/settings, /dashboard, /agents, /tasks,
        # ...) reachable without auth in any bootstrap-state deployment,
        # which is strictly broader than pre-#497 behavior and a real
        # security regression. Instead, the existing per-path logic below
        # naturally fails closed when there's no secret:
        #   - Public paths (login/setup/landing/assets/hooks/twilio) → through.
        #   - _has_valid_internal_auth() returns False (no secret to verify).
        #   - _has_valid_session() returns False (no secret to verify).
        #   - Protected HTML → 307 redirect to /setup (where the daemon
        #     surfaces the missing-secret state via the setup flow).
        #   - Protected API → 401 from _needs_browser_api_auth (browser
        #     shape, body advertises session_secret_configured: false so
        #     the SPA routes the user to setup) or from the scoped
        #     default-deny below (curl/non-browser shape on a
        #     _protected_api_prefixes path).
        path = request.url.path

        # 1. Public paths (login/setup/landing, /assets, /hooks, Twilio webhook
        #    callbacks, ConversationRelay WS — these are authenticated by
        #    other means or are intentionally open).
        if _is_public_path(path):
            return await call_next(request)

        # 2. HMAC-signed internal request (agent-to-daemon, hook scripts).
        if _has_valid_internal_auth(request):
            # #149: an isolated agent is scoped to its OWN resources. Deny any
            # cross-agent /agents/{other}/* access even with a valid signature.
            caller = request.headers.get(INTERNAL_AGENT_HEADER, "")
            if _internal_isolation_denied(request, caller):
                return JSONResponse(
                    status_code=403,
                    content={
                        "error": "isolated agent may only access its own resources",
                        "agent": caller,
                    },
                )
            return await call_next(request)

        # 3. Valid session cookie → through. Pulled up from the per-path
        #    branches below so a logged-in browser session passes the same
        #    way regardless of which protected surface is being hit.
        if _has_valid_session(request):
            return await call_next(request)

        # 4. Protected HTML pages: unauth → 307 redirect to /login (or
        #    /setup before first password is set). Different shape from
        #    the JSON 401 because this is hit by the browser navigating.
        if path in _protected_html_paths:
            next_target = _sanitize_next(str(request.url.path) + (f"?{request.url.query}" if request.url.query else ""))
            destination = "/setup" if _setup_required() else "/login"
            return RedirectResponse(url=f"{destination}?next={urllib.parse.quote(next_target, safe='/%#?=&')}", status_code=307)

        # 5. Browser-shaped API request (cookie present or browser-JSON
        #    headers) hitting a protected API surface: rich 401 with
        #    auth-status info for frontend UX. This is what the SPA reads
        #    to decide whether to show /login vs /setup.
        if _needs_browser_api_auth(request):
            return JSONResponse(
                status_code=401,
                content={
                    "authenticated": False,
                    "setup_required": _setup_required(),
                    "password_source": _active_password_source(),
                    "session_secret_configured": bool(_session_secret()),
                },
            )

        # 6. Scoped default-deny for protected API surfaces. Closes #497:
        #    previously the middleware fell through to `call_next` here,
        #    letting non-browser unauth requests reach every /agents/*,
        #    /tasks/*, /system/*, etc. endpoint. We now flat-401 any path
        #    inside ``_protected_api_prefixes`` that hasn't been granted
        #    access by one of the gates above.
        #
        #    Why scoped (not global) — per Murzik's PR #504 round-2
        #    review: a global default-deny here blocked routes that
        #    intentionally aren't behind a session cookie, notably the
        #    Google OAuth callback ``/calendar/google/callback``. Real
        #    redirects from Google arrive cross-site without our
        #    SameSite=strict cookie; the callback authenticates itself
        #    via a one-time state nonce that the route validates. Such
        #    public-but-state-protected routes (and any future ones that
        #    follow the same pattern) must fall through to ``call_next``
        #    so the route can run its own validation. Adding them to
        #    ``_protected_api_prefixes`` would be wrong — they're not
        #    session-protected — and adding every single new route to
        #    the prefix list is brittle.
        #
        #    Unmapped paths (typos like /random/url) fall through to
        #    FastAPI's 404. Acceptable: there's no protected surface to
        #    leak, and 401-ing every unmapped path was a UX regression
        #    in v2 of this PR.
        if path.startswith(_protected_api_prefixes):
            return JSONResponse(status_code=401, content={"detail": "Unauthorized"})

        # 7. Deny-by-default (#506). Anything still here is unauthenticated, not
        #    public, not a protected-HTML page, and not under a protected API
        #    prefix — the historical fall-through gap where unmapped/unclassified
        #    surfaces (e.g. /comms, /federation, /memory, /dreams) were reachable
        #    without auth. Genuinely-public routes are added to _public_* (e.g.
        #    the /a/ app viewer) so they pass at step 1 and never reach here.
        if _auth_deny_mode == "enforce":
            return JSONResponse(status_code=401, content={"detail": "Unauthorized"})
        if _auth_deny_mode == "shadow":
            _log(f"auth: would-deny (shadow) {request.method} {path}")
        return await call_next(request)

    # ── Request Timing Middleware ──────────────────────────────
    # Logs slow requests and adds Server-Timing header for frontend diagnostics.
    # Registered after auth middleware so it wraps the full request lifecycle.

    _SLOW_REQUEST_MS = 500  # noqa: N806 — log requests slower than this

    @app.middleware("http")
    async def timing_middleware(request: Request, call_next):
        if request.headers.get("upgrade", "").lower() == "websocket":
            return await call_next(request)
        start = time.time()
        response = await call_next(request)
        elapsed_ms = (time.time() - start) * 1000

        # Add Server-Timing header (visible in browser DevTools → Network → Timing)
        response.headers["Server-Timing"] = f"total;dur={elapsed_ms:.1f}"

        # Log slow requests (skip streaming/websocket-like paths)
        path = request.url.path
        if elapsed_ms >= _SLOW_REQUEST_MS and "/ws" not in path:
            _log(f"timing: {request.method} {path} — {elapsed_ms:.0f}ms")

        return response

    # ── Rate Limit Status ──────────────────────────────────
    _RATE_LIMIT_FILE = "/tmp/claude-rate-limits.json"  # noqa: N806 — module-like constant inside factory

    def _read_rate_limits() -> dict:
        """Read CC rate limits from shared file (written by statusline script)."""
        try:
            with open(_RATE_LIMIT_FILE) as f:
                data = json.loads(f.read())
            if time.time() - data.get("updated_at", 0) < 300:
                return data
            return {**data, "stale": True}
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    @app.get("/api")
    async def api_info():
        """Health check and server info (JSON)."""
        channel = os.environ.get("PINKYBOT_CHANNEL", "stable")
        rate_limits = _read_rate_limits()
        info = {
            "name": "pinky",
            "version": _pinky_version,
            "claude_version": _claude_version,
            "git_hash": _git_hash,
            "git_branch": _git_branch,
            "channel": channel,
            "sessions": manager.count,
            "started_at": _server_started_at,
        }
        # Include rate limit summary if available
        five_pct = rate_limits.get("five_hour", {}).get("used_percentage")
        if five_pct is not None:
            info["rate_limits"] = {
                "five_hour_pct": five_pct,
                "seven_day_pct": rate_limits.get("seven_day", {}).get(
                    "used_percentage"
                ),
            }
        return info

    # ── SPA helper ──
    def _serve_spa_or_html(filename: str):
        """Serve built SPA index.html if available, else fall back to vanilla HTML."""
        if frontend_dist.exists():
            spa_index = frontend_dist / "index.html"
            if spa_index.exists():
                return FileResponse(
                    str(spa_index),
                    headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
                )
        html_path = frontend_dir / filename if frontend_dir.exists() else None
        if html_path and html_path.exists():
            return FileResponse(str(html_path))
        # No built SPA and no vanilla HTML. New installs that skipped the
        # frontend build (npm unavailable at install time, manual install, or a
        # self-update that pulled source without rebuilding) land here. Surface
        # the exact build command instead of a bare "not found".
        repo_root = frontend_dist.parent
        return HTMLResponse(
            f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>PinkyBot — frontend not built</title>
    <style>
      body {{ font-family: system-ui, sans-serif; background: #111318; color: #f3f5f7; margin: 0; }}
      main {{ max-width: 40rem; margin: 10vh auto; padding: 2rem; background: #1a1f28; border: 1px solid #2a3140; border-radius: 0.5rem; }}
      h1 {{ margin: 0 0 1rem 0; font-size: 1.4rem; }}
      p {{ color: #c6ced8; line-height: 1.5; }}
      pre {{ background: #0c1016; padding: 1rem; border-radius: 0.4rem; overflow-x: auto; color: #9ad; }}
    </style>
  </head>
  <body>
    <main>
      <h1>The PinkyBot web UI hasn't been built yet</h1>
      <p>The daemon is running, but the frontend assets are missing. Build them once, then reload this page:</p>
      <pre>cd {repo_root}/frontend-svelte
npm install
npm run build</pre>
      <p>This needs Node.js 18+ and npm. The installer (<code>install.sh</code>) normally does this automatically; you only see this page if that step was skipped.</p>
    </main>
  </body>
</html>""",
            status_code=503,
        )

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

    # ── Auth Rate Limiting ─────────────────────────────────
    # In-memory per-IP rate limiter for auth endpoints. No external deps.
    _auth_attempts: dict[str, list[float]] = {}  # IP -> list of attempt timestamps
    _AUTH_MAX_ATTEMPTS = 5  # noqa: N806 — max attempts per window
    _AUTH_WINDOW_SECONDS = 300  # noqa: N806 — 5-minute window

    def _check_auth_rate_limit(request: Request) -> None:
        """Raise 429 if IP has exceeded auth attempt limit."""
        ip = request.headers.get("x-forwarded-for", "").split(",")[0].strip() or (request.client.host if request.client else "unknown")
        now = time.time()
        cutoff = now - _AUTH_WINDOW_SECONDS

        # Clean old entries
        attempts = [t for t in _auth_attempts.get(ip, []) if t > cutoff]
        _auth_attempts[ip] = attempts

        if len(attempts) >= _AUTH_MAX_ATTEMPTS:
            _log(f"auth: rate limited {ip} ({len(attempts)} attempts in {_AUTH_WINDOW_SECONDS}s)")
            raise HTTPException(429, "Too many authentication attempts. Try again later.")

        # Record this attempt
        attempts.append(now)
        _auth_attempts[ip] = attempts

    @app.get("/auth/status")
    async def get_ui_auth_status(request: Request):
        """Get browser auth state for the UI."""
        return _auth_status(request)

    @app.post("/auth/setup")
    async def setup_ui_password(request: Request, req: AuthSetupRequest):
        """Create the initial UI password and start a session."""
        _check_auth_rate_limit(request)
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
        _check_auth_rate_limit(request)
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
        _check_auth_rate_limit(request)
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

    @app.get("/landing", response_class=HTMLResponse)
    async def landing_page():
        """Public landing page — no auth required."""
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
        """Delete a session and free resources (including SDK transcript file)."""
        session = manager.get(session_id)
        sdk_session_id = session._sdk_session_id if session else ""
        working_dir = session.working_dir if session else ""

        deleted = manager.delete(session_id)
        if not deleted:
            raise HTTPException(404, f"Session '{session_id}' not found")

        # Also delete the underlying SDK transcript file if we have one
        if sdk_session_id:
            try:
                from claude_agent_sdk import delete_session as sdk_delete
                sdk_delete(sdk_session_id, directory=working_dir or None)
                _log(f"api: deleted SDK transcript {sdk_session_id}")
            except FileNotFoundError:
                pass  # Already gone
            except Exception as e:
                _log(f"api: could not delete SDK transcript {sdk_session_id}: {e}")

        _log(f"api: deleted session {session_id}")
        return {"deleted": True, "session_id": session_id}

    @app.post("/sessions/{session_id}/fork")
    async def fork_session(session_id: str, req: ForkSessionRequest):
        """Fork a session's transcript into a new branch.

        Creates an independent copy of the session's conversation history
        (up to an optional message cutoff). The forked session exists as a
        new SDK transcript file and can be resumed by spawning a new session
        with resume=<forked_session_id>.
        """
        session = manager.get(session_id)
        if not session:
            raise HTTPException(404, f"Session '{session_id}' not found")
        sdk_id = session._sdk_session_id
        if not sdk_id:
            raise HTTPException(400, f"Session '{session_id}' has no active SDK transcript to fork")

        try:
            from claude_agent_sdk import fork_session as sdk_fork
            result = sdk_fork(
                sdk_id,
                directory=session.working_dir or None,
                up_to_message_id=req.up_to_message_id or None,
                title=req.title or None,
            )
        except FileNotFoundError as e:
            raise HTTPException(404, str(e))
        except ValueError as e:
            raise HTTPException(400, str(e))

        _log(f"api: forked session {session_id} (sdk={sdk_id}) → {result.session_id}")
        return {
            "source_session_id": session_id,
            "source_sdk_session_id": sdk_id,
            "forked_sdk_session_id": result.session_id,
        }

    @app.post("/agents/{name}/sessions/{session_id}/message", response_model=MessageResponse)
    async def _noop_agent_session_message(name: str, session_id: str, req: SendMessageRequest):
        """Alias: route agent-scoped session messages to the global sessions handler."""
        return await send_message(session_id, req)

    @app.post("/agents/{name}/clone-worker")
    async def clone_worker(name: str, req: CloneWorkerRequest):
        """Fork the agent's main session and spawn a worker clone with a task.

        1. Finds the agent's active main session and its SDK transcript ID.
        2. Forks the transcript (clone inherits full conversation context).
        3. Spawns a new worker session that resumes from the fork.
        4. Sends the task as the worker's first message.

        The worker runs autonomously and reports back through the normal
        message broker. Multiple clones can be spawned in parallel.
        """
        agent = agents.get(name)
        if not agent:
            raise HTTPException(404, f"Agent '{name}' not found")

        # Find the agent's main session
        main_sessions = [
            s for s in manager.list()
            if s.agent_name == name and s.session_type.value == "main"
        ]
        if not main_sessions:
            raise HTTPException(400, f"Agent '{name}' has no active main session to fork from")
        main_session = main_sessions[0]

        sdk_id = main_session._sdk_session_id
        if not sdk_id:
            raise HTTPException(400, f"Agent '{name}' main session has no SDK transcript yet")

        # Fork the main session transcript
        try:
            from claude_agent_sdk import fork_session as sdk_fork
            fork_result = sdk_fork(
                sdk_id,
                directory=main_session.working_dir or None,
                title=req.title or f"Worker clone for: {req.task[:60]}",
            )
        except Exception as e:
            raise HTTPException(500, f"Fork failed: {e}")

        # Spawn a new worker session resuming from the fork
        worker_id = f"{name}-clone-{uuid.uuid4().hex[:8]}"
        _clone_provider_url, _clone_provider_key, _clone_provider_model = _resolve_agent_provider(agent)
        worker = manager.create(
            session_id=worker_id,
            model=_clone_provider_model or agent.model,
            soul=agent.soul,
            working_dir=main_session.working_dir,
            allowed_tools=agent.allowed_tools or None,
            disallowed_tools=agent.disallowed_tools or None,
            max_turns=agent.max_turns,
            timeout=agent.timeout,
            system_prompt=main_session._system_prompt,
            restart_threshold_pct=agent.restart_threshold_pct,
            auto_restart=False,  # Workers don't auto-restart
            permission_mode=agent.permission_mode,
            session_type="worker",
            agent_name=name,
            provider_url=_clone_provider_url,
            provider_key=_clone_provider_key,
        )
        # Pre-set the SDK session ID so the worker resumes from the fork
        worker._sdk_session_id = fork_result.session_id

        # Send the task as the first message (non-blocking)
        _send_task = asyncio.create_task(worker.send(req.task))
        _send_task.add_done_callback(
            lambda t: _log(f"api: clone worker send failed: {t.exception()}", level="error")
            if not t.cancelled() and t.exception()
            else None
        )

        _log(f"api: spawned clone worker {worker_id} for {name} (fork={fork_result.session_id})")
        return {
            "worker_session_id": worker_id,
            "forked_sdk_session_id": fork_result.session_id,
            "agent": name,
            "task": req.task,
        }

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
    async def get_conversation_history(
        session_id: str, limit: int = 100, offset: int = 0,
    ):
        """Get conversation history from persistent store.

        Supports pagination: offset counts backwards from newest messages.
        limit=100&offset=0 → 100 most recent; limit=100&offset=100 → previous 100.
        """
        messages = store.get_history(session_id, limit=limit, offset=offset)
        total = store.count(session_id)
        return {
            "session_id": session_id,
            "messages": [
                {
                    "role": m.role, "content": m.content,
                    "timestamp": m.timestamp, "metadata": m.metadata,
                }
                for m in messages
            ],
            "count": len(messages),
            "total": total,
            "has_more": offset + len(messages) < total,
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

    @app.get("/analytics/overview")
    async def get_analytics_overview(range: str = "7d"):
        """Return high-level analytics totals and trends."""
        return analytics.get_overview(range_name=range)

    @app.get("/analytics/agents")
    async def get_analytics_agents(range: str = "7d"):
        """Return analytics summaries grouped by agent."""
        return analytics.list_agents(range_name=range)

    @app.get("/analytics/agents/{agent_name}")
    async def get_analytics_agent_detail(agent_name: str, range: str = "7d"):
        """Return analytics details for a single agent."""
        agent = agents.get(agent_name)
        if not agent:
            raise HTTPException(404, f"Agent '{agent_name}' not found")
        return analytics.get_agent_detail(agent_name=agent_name, range_name=range)

    @app.get("/analytics/categories")
    async def get_analytics_categories(range: str = "7d", agent: str = ""):
        """Return token usage breakdown by task category."""
        return analytics.get_categories(range_name=range, agent_name=agent)

    @app.get("/analytics/hourly")
    async def get_analytics_hourly(
        range: str = "7d",
        agent: str = "",
        tz: str = "America/Los_Angeles",
    ):
        """Return token usage by hour-of-day with historical averages."""
        return analytics.get_hourly(range_name=range, agent_name=agent, timezone=tz)

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

    @app.post("/sessions/{session_id}/events")
    async def log_session_event(session_id: str, req: dict):
        """Log a session lifecycle event (internal use).

        event_type: 'context_restart' | 'session_resume' | 'session_start' | 'session_end'
        metadata: arbitrary JSON dict (fork_id, turns, context_pct, etc.)
        """
        event_type = req.get("event_type", "")
        if not event_type:
            raise HTTPException(400, "event_type is required")
        agent_name = req.get("agent_name", "")
        if not agent_name:
            # Derive from session_id if possible
            session = manager.get(session_id)
            agent_name = session.agent_name if session else session_id.split("-")[0]
        metadata = req.get("metadata", {})
        analytics.log_activity(
            session_id=session_id,
            agent_name=agent_name,
            event_type=event_type,
            metadata=metadata if isinstance(metadata, dict) else {},
        )
        if event_type == "session_end":
            analytics.mark_session_ended(session_id)
        row_id = session_event_store.log(session_id, agent_name, event_type, metadata)
        return {"ok": True, "id": row_id, "session_id": session_id, "event_type": event_type}

    @app.get("/agents/{name}/session-events")
    async def get_agent_session_events(name: str, limit: int = 50):
        """Fetch recent session lifecycle events for an agent."""
        agent = agents.get(name)
        if not agent:
            raise HTTPException(404, f"Agent '{name}' not found")
        events = session_event_store.get_for_agent(name, limit=limit)
        return {"agent": name, "events": events, "count": len(events)}

    @app.get("/sessions/{session_id}/events")
    async def get_session_events(session_id: str, limit: int = 100):
        """Get session lifecycle events for a specific session."""
        events = session_event_store.get_for_session(session_id, limit=limit)
        return {"session_id": session_id, "events": events, "count": len(events)}

    # Also add refresh to the agent sessions endpoint
    @app.post("/agents/{name}/sessions/refresh")
    async def refresh_agent_sessions(name: str):
        """Refresh all sessions for an agent — reload MCP config while keeping conversations.

        Rewrites .mcp.json and CLAUDE.md, then refreshes each session's runner.
        """
        agent = agents.get(name)
        if not agent:
            raise HTTPException(404, f"Agent '{name}' not found")

        # Rewrite MCP config only — CLAUDE.md is agent-owned, don't stomp it
        work_dir = Path(agent.working_dir or default_working_dir).resolve()
        work_dir.mkdir(parents=True, exist_ok=True)
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

    @app.get("/comms/messages")
    async def get_all_comms(limit: int = 100, offset: int = 0):
        """Return all inter-agent messages for audit (newest first)."""
        messages, total = comms.get_all_messages(limit=limit, offset=offset)
        return {
            "messages": [m.to_dict() for m in messages],
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    @app.get("/comms/inboxes")
    async def get_inbox_summaries():
        """Get unread message counts per agent."""
        return {"inboxes": comms.get_all_inbox_summaries()}

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

    # ── Skill / Plugin Endpoints ──────────────────────────
    from pinky_daemon.routes.skills import router as _skills_router
    from pinky_daemon.routes.skills import set_dependencies as _skills_set_deps

    _skills_set_deps(
        skills=skills,
        plugins=plugins,
        agents=agents,
        manager=manager,
        pinky_root=_pinky_root,
        log=_log,
    )
    app.include_router(_skills_router)

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

        This writes the updated .mcp.json, then restarts the current
        streaming session only if the agent has a recent explicit
        save_my_context() checkpoint from this session.
        """
        agent = agents.get(name)
        if not agent:
            raise HTTPException(404, f"Agent '{name}' not found")

        work_dir = Path(agent.working_dir or default_working_dir).resolve()

        # 1. Materialize skills
        materialized = skills.materialize_for_agent(name)

        # 2. Write file templates (don't overwrite existing files)
        for rel_path, content in materialized.get("file_templates", {}).items():
            file_path = (work_dir / rel_path).resolve()
            # Security: prevent path traversal — file must stay within work_dir
            if not file_path.is_relative_to(work_dir.resolve()):
                _log(f"api: BLOCKED path traversal in skill template: {rel_path}")
                continue
            if not file_path.exists():
                file_path.parent.mkdir(parents=True, exist_ok=True)
                file_path.write_text(content)
                _log(f"api: wrote skill template {rel_path} to {work_dir}")

        # 3. Rewrite .mcp.json with skill servers
        _write_mcp_json(work_dir, name, agent_registry=agents, skill_store=skills)

        # 4. CLAUDE.md is agent-owned — don't overwrite on skill changes
        # Dynamic parts (directives, skills, users) are injected at session start via system_prompt

        # 5. Restart streaming session if one exists
        restarted = False
        streaming = broker._get_streaming_session(name)
        if streaming:
            guard = _get_streaming_restart_guard(name, streaming)
            if not guard["restart_safe"]:
                raise HTTPException(409, _guard_message("restart", guard))

            # Close and restart
            await _disconnect_streaming_sessions(name)
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
        for key_name in (
            "ELEVENLABS_API_KEY", "OPENAI_API_KEY", "DEEPGRAM_API_KEY", "GIPHY_API_KEY",
            "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_PHONE_NUMBER", "PINKY_BASE_URL",
        ):
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
        allowed = {
            "ANTHROPIC_API_KEY",
            "ELEVENLABS_API_KEY", "OPENAI_API_KEY", "DEEPGRAM_API_KEY", "GIPHY_API_KEY",
            "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_PHONE_NUMBER", "PINKY_BASE_URL",
        }
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

    # ── Outreach Platform Configuration Routes ──────────
    from pinky_daemon.routes.outreach import router as _outreach_router
    from pinky_daemon.routes.outreach import set_dependencies as _outreach_set_deps

    _outreach_set_deps(outreach_config=outreach_config)
    app.include_router(_outreach_router)

    # ── Calendar Endpoints ───────────────────────────────────
    from pinky_daemon.routes.calendar import router as _calendar_router
    from pinky_daemon.routes.calendar import set_dependencies as _calendar_set_deps

    _calendar_set_deps(agents=agents)
    app.include_router(_calendar_router)

    # ── Agent Registry Endpoints ────────────────────────────

    @app.post("/agents")
    async def register_agent(req: RegisterAgentRequest, request: Request):
        """Register a new agent or update an existing one."""
        # #149: registering/upserting agents is an ADMIN capability. This is the
        # agent-mint/upsert collection route — it has no target agent in the
        # PATH, so the middleware's path-based isolation guard can't see it
        # (target is None → flows through). An isolated tenant reaching here with
        # a valid signature could mint a new full-trust agent OR upsert an
        # existing record (including dropping its OWN isolated flag — a direct
        # escape). Deny the whole route for isolated callers; agents are minted
        # by an operator/admin, never self-served by a tenant. (Murzik #635
        # re-review catch.) Operator/browser sessions carry no internal-agent
        # header → caller "" → not isolated → unaffected.
        caller = request.headers.get(INTERNAL_AGENT_HEADER, "")
        if _is_isolated_agent(caller):
            _log(
                f"isolation: denied isolated agent '{caller}' from POST /agents "
                f"(admin mint/upsert, body name='{req.name}')"
            )
            raise HTTPException(403, "isolated agent may not register or modify agents")
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
            auto_start=req.auto_start,
            role=req.role,
            heartbeat_interval=req.heartbeat_interval,
            runtime=req.runtime,
            transport=req.transport,
            provider_url=req.provider_url,
            provider_key=req.provider_key,
            provider_model=req.provider_model,
            provider_ref=req.provider_ref,
            thinking_effort=req.thinking_effort,
            strict_effort_enforcement=req.strict_effort_enforcement,
            watchdog_config=req.watchdog_config or {},
            isolated=req.isolated,
            isolation_mode=req.isolation_mode,
        )
        # Write .mcp.json so the agent gets default MCP servers (memory, self, messaging)
        work_dir = Path(agent.working_dir) if agent.working_dir else None
        if work_dir:
            _write_mcp_json(work_dir, req.name, agent_registry=agents, skill_store=skills)
        return agent.to_dict()

    @app.get("/agents")
    async def list_agents(group: str = "", enabled_only: bool = False):
        """List all registered agents, including live working status."""
        result = agents.list(group=group, enabled_only=enabled_only)
        live_agents = broker.get_live_agents()
        agents_out = []
        for a in result:
            d = a.to_dict()
            live = _agent_live_status.get(a.name)
            d["live_status"] = live["status"] if live else a.working_status or "idle"
            d["live_tool_name"] = live["tool_name"] if live else ""
            d["live_detail"] = live["detail"] if live else ""
            d["live_status_updated_at"] = live["last_updated"] if live else a.working_status_updated_at
            d["streaming"] = a.name in live_agents
            agents_out.append(d)
        return {
            "agents": agents_out,
            "count": len(agents_out),
            "main_agent": agents.get_main_agent(),
        }

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
        agent_obj = agents.get(agent_name)
        hb_ts = hb.timestamp if hb else 0
        server_ts = agent_obj.last_seen_at if agent_obj else 0
        last_seen = max(server_ts, hb_ts)
        now = time.time()
        if streaming:
            status = "online"
        elif hb and hb_ts >= server_ts:
            # Heartbeat is the freshest liveness signal — honor its status field.
            age = now - hb.timestamp
            if hb.status == "alive" and age < 300:
                status = "online"
            elif hb.status == "alive" and age < 1800:
                status = "idle"
            else:
                status = "offline"
        elif server_ts > 0:
            # Server-stamped activity (turn completion, delivered inter-agent msg, etc.)
            # is authoritative liveness evidence for agents that don't emit heartbeats.
            age = now - server_ts
            if age < 300:
                status = "online"
            elif age < 1800:
                status = "idle"
            else:
                status = "offline"
        else:
            status = "unknown"
        return {"status": status, "streaming": streaming, "last_seen": last_seen}

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

    @app.post("/agents/{name}/status")
    async def set_agent_working_status(name: str, req: AgentStatusRequest):
        """Update an agent's working status.

        Called by Claude Code hooks (PreToolUse -> working/tool_use/thinking, Stop -> idle).
        Accepted statuses: working, idle, thinking, tool_use, offline.
        """
        agent = agents.get(name)
        if not agent:
            raise HTTPException(404, f"Agent '{name}' not found")
        new_status = req.status

        # Persist coarse-grained status to DB (only the DB-supported values)
        db_status = new_status if new_status in ("idle", "working", "offline") else "working"
        agents.set_working_status(name, db_status)

        # Update live in-memory status (fine-grained: thinking, tool_use, etc.)
        _agent_live_status[name] = {
            "status": new_status,
            "tool_name": req.tool_name,
            "detail": req.detail,
            "last_updated": time.time(),
        }

        # Only log meaningful state transitions to activity (not every tool tick)
        if new_status in ("idle", "working", "offline"):
            activity.log(
                agent_name=name,
                event_type="agent_" + new_status,
                title=f"{agent.display_name or name} is {new_status}",
                metadata={"agent": name, "status": new_status},
            )

        return {
            "agent": name,
            "status": new_status,
            "working_status": db_status,
            "ok": True,
        }

    @app.post("/agents/{name}/effort-drift")
    async def report_effort_drift(name: str, req: EffortDriftRequest):
        """Record a thinking-effort drift event from hook_verify_effort.py (#429).

        Called when ``$CLAUDE_EFFORT`` (runtime) diverges from
        ``$PINKY_EXPECTED_EFFORT`` (agent's configured ``thinking_effort``).
        The hook is fire-and-forget — we return quickly and never fail the
        tool call.
        """
        agent = agents.get(name)
        if not agent:
            raise HTTPException(404, f"Agent '{name}' not found")
        event_id = agents.record_effort_drift(
            name,
            expected=req.expected,
            actual=req.actual,
            session_id=req.session_id,
            tool_name=req.tool_name,
            strict=req.strict,
        )
        activity.log(
            agent_name=name,
            event_type="effort_drift",
            title=(
                f"effort drift — expected={req.expected} actual={req.actual}"
                + (" (blocked)" if req.strict else "")
            ),
            metadata={
                "agent": name,
                "expected": req.expected,
                "actual": req.actual,
                "tool_name": req.tool_name,
                "strict": req.strict,
            },
        )
        return {
            "ok": True,
            "agent": name,
            "event_id": event_id,
            "expected": req.expected,
            "actual": req.actual,
            "strict": req.strict,
        }

    @app.post("/agents/{name}/transport/wake")
    async def transport_wake(name: str, req: TransportWakeRequest):
        """Wake the Transport's response pipeline — called by Stop /
        PostCompact hooks (PR8b).

        Generic across runtimes: ``TmuxSession`` uses this to wake its
        transcript tailer; future backends (e.g. a wire-protocol REPL)
        could reuse the same endpoint. ``StreamingSession`` ignores the
        wake — its response is in-band via the SDK callback.

        Fire-and-forget: returns 200 immediately. Hook scripts wrap the
        request in ``|| true`` so a 404/500 here doesn't fail the model
        turn.
        """
        agent = agents.get(name)
        if not agent:
            raise HTTPException(404, f"Agent '{name}' not found")

        session = broker.get_streaming_session(name, label=req.label)
        if session is None:
            # Hook fired before the daemon registered the session, or
            # after a graceful disconnect. Not an error — the next
            # tailer start picks up where we left off.
            return {"ok": True, "agent": name, "session": None}

        # Duck-typed dispatch: only tmux currently exposes notify_tail.
        notify = getattr(session, "notify_tail", None)
        if callable(notify):
            try:
                notify()
            except Exception as e:
                _log(f"api: transport_wake notify_tail raised for {name}: {e}")
        return {"ok": True, "agent": name, "event": req.event}

    @app.post("/agents/{name}/transport/stop-failure")
    async def transport_stop_failure(name: str, req: TransportStopFailureRequest):
        """Typed turn-failure signal — POSTed by the ``StopFailure`` hook.

        Claude Code fires ``StopFailure`` when a turn ends due to an API
        error, carrying a typed ``error_type``. Every failure is logged for
        observability. Auth-class failures (``authentication_failed`` /
        ``oauth_org_not_allowed``) are routed into the shared
        ``AuthFailureTracker`` via ``_on_auth_failure`` — the same
        threshold + cooldown + operator-resolution path the SDK reader loop
        uses. tmux/CLI agents have no SDK reader loop, so this hook is the
        only thing that surfaces a dead token before the agent goes
        silently dark.

        Fire-and-forget: returns 200 even with no live session so the
        hook's ``|| true`` never fails the model turn. Runtime-agnostic —
        the alert matters regardless of transport.
        """
        agent = agents.get(name)
        if not agent:
            raise HTTPException(404, f"Agent '{name}' not found")

        error_type = (req.error_type or "unknown").strip() or "unknown"

        # Observability: every failure class is recorded.
        try:
            activity.log(
                agent_name=name,
                event_type="cc_stop_failure",
                title=f"StopFailure: {error_type}"[:200],
            )
        except Exception:
            pass
        _log(
            f"api: StopFailure for {name} — error_type={error_type}"
            + (f" — {req.message[:200]}" if req.message else "")
        )

        # Auth-class failures → shared auth-alert path (tracker decides
        # whether the threshold + cooldown warrant an operator DM).
        auth_failure = error_type in _STOP_FAILURE_AUTH_TYPES
        if auth_failure:
            try:
                await _on_auth_failure(name, error_type)
            except Exception as e:
                _log(f"api: StopFailure auth routing failed for {name}: {e}")

        # #104: non-auth classes with an alert policy (billing_error,
        # rate_limit) route to their own per-class tracker. Everything else
        # stays log-only. ``alert_routed`` reflects whether the failure was
        # sent to any alert tracker (auth or class) — not whether a DM fired
        # (that's the tracker's threshold/cooldown decision downstream).
        alert_routed = auth_failure
        if not auth_failure and error_type in TRANSPORT_FAILURE_POLICIES:
            alert_routed = True
            try:
                await _on_transport_failure_alert(name, error_type)
            except Exception as e:
                _log(
                    f"api: StopFailure transport-alert routing failed "
                    f"for {name}: {e}"
                )

        # #108 — make the StopFailure POST the authoritative turn-end
        # signal for tmux turns. A terminal API-error turn doesn't
        # reliably emit a ``stop_hook_summary``, so without this the failed
        # turn wedges at the head of the session's in-flight deque until
        # the 10-min inflight watchdog force-restarts it. Look up the live
        # session and duck-call ``handle_stop_failure`` AFTER the auth
        # routing above (preserves #584). Only ``TmuxSession`` exposes it;
        # SDK / Codex sessions detect turn-end in-band and don't. Fire-and-
        # forget like the rest of this hook — never fail the model turn.
        turn_resolved = False
        session = broker.get_streaming_session(name, label=req.label)
        if session is not None:
            resolver = getattr(session, "handle_stop_failure", None)
            if callable(resolver):
                try:
                    turn_resolved = bool(
                        await resolver(error_type, req.message, req.session_id)
                    )
                except Exception as e:
                    _log(
                        f"api: StopFailure turn-resolve raised for {name}: {e}"
                    )

        return {
            "ok": True,
            "agent": name,
            "error_type": error_type,
            "auth_failure": auth_failure,
            "alert_routed": alert_routed,
            "turn_resolved": turn_resolved,
        }

    @app.post("/agents/{name}/transport/transcript-path")
    async def transport_transcript_path(
        name: str, req: TransportTranscriptPathRequest,
    ):
        """Update the Transport's watched transcript path — called by
        the SessionStart hook (PR8b).

        The SessionStart hook reports the file Claude Code is actually
        writing to, replacing the daemon's mtime-glob guess.

        Validation (Pushok's PR #496 round-1 Case 4a):
          - Must be absolute.
          - Must be under ``~/.claude/projects/`` — defends against a
            caller with a valid HMAC pointing the tailer at an arbitrary
            file (e.g. ``/var/log/system.log``) and forcing the daemon
            to read large chunks of it. The size cap inside the tailer
            (``_MAX_READ_CHUNK_BYTES``) is the defense-in-depth; this
            prefix check is the perimeter.

        File may not exist yet on a fresh session (Claude Code creates
        it on first append); the tailer's ``read_once`` handles the
        missing-file path gracefully, so we don't insist on existence.
        """
        agent = agents.get(name)
        if not agent:
            raise HTTPException(404, f"Agent '{name}' not found")

        path = Path(req.transcript_path)
        if not path.is_absolute():
            raise HTTPException(400, "transcript_path must be absolute")
        # Restrict to ``~/.claude/projects/``. Resolve symlinks before
        # the prefix check so a symlinked attack path is normalised.
        projects_root = (Path.home() / ".claude" / "projects").resolve()
        try:
            normalised = path.resolve(strict=False)
        except (OSError, RuntimeError) as e:
            raise HTTPException(400, f"transcript_path could not be resolved: {e}")
        # ``is_relative_to`` (Py 3.9+) is the idiomatic sanitizer here and
        # is recognized by CodeQL's path-traversal taint analysis — the
        # equivalent ``parents``-membership check tripped a false-positive
        # CodeQL alert in round-2 even though it had the same semantics.
        if not normalised.is_relative_to(projects_root):
            raise HTTPException(
                403,
                f"transcript_path must be under {projects_root}",
            )

        session = broker.get_streaming_session(name, label=req.label)
        if session is None:
            return {"ok": True, "agent": name, "session": None}

        update = getattr(session, "set_transcript_path", None)
        if callable(update):
            try:
                update(normalised)
            except Exception as e:
                _log(
                    f"api: transport_transcript_path set_transcript_path "
                    f"raised for {name}: {e}"
                )
                raise HTTPException(500, str(e))
        return {"ok": True, "agent": name, "transcript_path": str(normalised)}

    @app.post("/agents/{name}/transport/tool-use")
    async def transport_tool_use(name: str, req: TransportToolUseRequest):
        """Record a tool-call start — POSTed by the PreToolUse hook (task #93).

        Generic across runtimes: ``TmuxSession`` records the call to
        analytics + emits a stream event for SSE consumers, matching
        what ``StreamingSession`` already does for SDK agents.
        ``StreamingSession`` ignores this — it captures tool calls in-
        band via the SDK callback.

        Fire-and-forget: returns 200 immediately. Hook scripts wrap the
        request in ``|| true`` so a 404/500 doesn't fail the model turn.
        """
        agent = agents.get(name)
        if not agent:
            raise HTTPException(404, f"Agent '{name}' not found")

        session = broker.get_streaming_session(name, label=req.label)
        if session is None:
            return {"ok": True, "agent": name, "session": None}

        record = getattr(session, "record_tool_use_start", None)
        if callable(record):
            try:
                await record(
                    tool_use_id=req.tool_use_id,
                    tool_name=req.tool_name,
                    tool_input=req.tool_input or {},
                )
            except Exception as e:
                _log(
                    f"api: transport_tool_use record_tool_use_start "
                    f"raised for {name}: {e}"
                )
        return {"ok": True, "agent": name, "tool_use_id": req.tool_use_id}

    @app.post("/agents/{name}/transport/tool-result")
    async def transport_tool_result(name: str, req: TransportToolResultRequest):
        """Record a tool-call result — POSTed by the PostToolUse hook (task #93).

        Companion to ``/transport/tool-use``. Closes out the analytics
        row and emits the finish stream event.
        """
        agent = agents.get(name)
        if not agent:
            raise HTTPException(404, f"Agent '{name}' not found")

        session = broker.get_streaming_session(name, label=req.label)
        if session is None:
            return {"ok": True, "agent": name, "session": None}

        record = getattr(session, "record_tool_use_finish", None)
        if callable(record):
            try:
                await record(
                    tool_use_id=req.tool_use_id,
                    tool_name=req.tool_name,
                    is_error=req.is_error,
                    tool_response=req.tool_response,
                )
            except Exception as e:
                _log(
                    f"api: transport_tool_result record_tool_use_finish "
                    f"raised for {name}: {e}"
                )
        return {"ok": True, "agent": name, "tool_use_id": req.tool_use_id}

    @app.get("/agents/{name}/effort-drift")
    async def list_effort_drift_events(
        name: str, limit: int = 50, since: float = 0.0,
    ):
        """List recent effort-drift events for an agent (#429)."""
        agent = agents.get(name)
        if not agent:
            raise HTTPException(404, f"Agent '{name}' not found")
        events = agents.get_effort_drift_events(
            agent_name=name, limit=limit, since=since,
        )
        return {"agent": name, "events": events, "count": len(events)}

    @app.get("/agents/{name}/status")
    async def get_agent_working_status(name: str):
        """Get an agent's current working status.

        Returns the fine-grained live status (from memory) plus DB-persisted coarse status.
        """
        agent = agents.get(name)
        if not agent:
            raise HTTPException(404, f"Agent '{name}' not found")
        live = _agent_live_status.get(name)
        return {
            "agent": name,
            "status": live["status"] if live else agent.working_status or "idle",
            "tool_name": live["tool_name"] if live else "",
            "detail": live["detail"] if live else "",
            "last_updated": live["last_updated"] if live else agent.working_status_updated_at,
            "db_status": agent.working_status or "idle",
        }

    @app.get("/agents/{name}/mcp-bind-status")
    async def get_agent_mcp_bind_status(name: str):
        """MCP transport bind status for an agent (issue #663).

        Reports whether the agent has a successful MCP round-trip recorded for
        the CURRENT gateway generation (``bound``), plus the last probe nonce /
        launch_id / observed_at and the current vs bound gateway epoch. Pure
        read of the in-memory bind ledger — does NOT touch the agent's MCP
        transport, so it stays usable precisely when that transport is dead.
        """
        agent = agents.get(name)
        if not agent:
            raise HTTPException(404, f"Agent '{name}' not found")
        return get_probe_status(name)

    # ── Thinking Effort ──────────────────────────────────

    @app.get("/agents/{name}/effort")
    async def get_agent_effort(name: str, label: str = "main"):
        """Get an agent's thinking effort (default + session override)."""
        agent = agents.get(name)
        if not agent:
            raise HTTPException(404, f"Agent '{name}' not found")
        # Label-aware session lookup
        session_override = None
        sessions = broker._streaming.get(name, {})
        ss = sessions.get(label) or sessions.get("main") if sessions else None
        if ss and hasattr(ss, "_effort_override") and ss._effort_override:
            session_override = ss._effort_override
        default = agent.thinking_effort or "medium"
        effective = session_override or default
        return {
            "agent": name,
            "label": label,
            "default": default,
            "session_override": session_override,
            "effective": effective,
        }

    @app.put("/agents/{name}/effort")
    async def set_agent_effort(name: str, req: dict):
        """Set an agent's default thinking effort level."""
        level = req.get("effort", "medium")
        # Persistent default accepts the CLI levels plus the ultracode tier
        # (#151); "auto" is a session-only override, not a stored default.
        if level not in (*CLI_EFFORT_LEVELS, "ultracode"):
            raise HTTPException(400, f"Invalid effort level: {level}")
        agent = agents.get(name)
        if not agent:
            raise HTTPException(404, f"Agent '{name}' not found")
        agents.update(name, thinking_effort=level)
        return {"agent": name, "default": level}

    @app.post("/agents/{name}/sessions/{session_label}/effort")
    async def set_session_effort(name: str, session_label: str, req: dict):
        """Set session-level thinking effort override (label-aware)."""
        level = req.get("effort", "medium")
        if level not in EFFORT_LEVELS:
            raise HTTPException(400, f"Invalid effort level: {level}")
        agent = agents.get(name)
        if not agent:
            raise HTTPException(404, f"Agent '{name}' not found")
        # Label-aware session lookup
        sessions = broker._streaming.get(name, {})
        ss = (sessions.get(session_label) or sessions.get("main")) if sessions else None
        if not ss or not hasattr(ss, "set_effort"):
            raise HTTPException(404, f"No active session '{session_label}' for '{name}'")
        if level == "auto":
            ss.clear_effort_override()
        else:
            ss.set_effort(level)
        default = agent.thinking_effort or "medium"
        effective = level if level != "auto" else default
        return {"agent": name, "label": session_label,
                "default": default,
                "session_override": None if level == "auto" else level,
                "effective": effective}

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

        # CLAUDE.md is agent-owned after spawn — don't overwrite on soul field updates.
        # The file is written once at spawn; agents edit it directly after that.

        return agent.to_dict()

    @app.put("/agents/{name}/provider")
    async def set_agent_provider(name: str, req: dict):
        """Set agent model provider (Ollama, custom Anthropic-compatible endpoint, etc.)"""
        agent = agents.get(name)
        if not agent:
            raise HTTPException(404, f"Agent '{name}' not found")
        updates = {}
        if "provider_url" in req:
            updates["provider_url"] = req["provider_url"].strip()
        if "provider_key" in req:
            updates["provider_key"] = req["provider_key"].strip()
        if "provider_model" in req:
            updates["provider_model"] = req["provider_model"].strip()
        if "provider_ref" in req:
            updates["provider_ref"] = req["provider_ref"].strip()
        if updates:
            agents.register(name, **updates)
        return {"saved": True, **updates}

    # ── Global Providers / Models / Bot Tokens ──────────────
    # _provider_public_row_to_dict is used by /settings/default-provider too,
    # so it stays here rather than moving into routes/providers.py.
    def _provider_public_row_to_dict(row) -> dict:
        """Provider info safe for lightweight settings dropdowns."""
        return {
            "id": row[0],
            "name": row[1],
            "preset": row[2],
            "provider_model": row[5],
        }

    from pinky_daemon.routes.providers import router as _providers_router
    from pinky_daemon.routes.providers import set_dependencies as _providers_set_deps

    _providers_set_deps(agents=agents)
    app.include_router(_providers_router)

    @app.post("/agents/{name}/claude-md/rebuild")
    async def rebuild_claude_md(name: str):
        """Deprecated — CLAUDE.md is now agent-owned. Returns current file content."""
        agent = agents.get(name)
        if not agent:
            raise HTTPException(404, f"Agent '{name}' not found")

        work_dir = Path(agent.working_dir or default_working_dir).resolve()
        claude_md = work_dir / "CLAUDE.md"
        content = claude_md.read_text() if claude_md.exists() else agent.soul or ""
        return {"content": content, "size": len(content), "deprecated": True}

    # ── Soul Templates ────────────────────────────────────────────────────

    @app.get("/soul-templates")
    async def get_soul_templates():
        """List available soul templates with metadata."""
        from pinky_daemon.soul_templates import list_templates
        return {"templates": list_templates()}

    @app.post("/soul-templates/render")
    async def render_soul_template(req: dict):
        """Render a soul template with the given parameters.

        Body: {type, name, model?, mode?, pronouns?, platforms?, heartbeat_interval?, custom_soul?}
        """
        from pinky_daemon.soul_templates import build_soul
        heart_type = req.get("type", "sidekick")
        name = req.get("name", "Agent")
        soul = build_soul(
            heart_type=heart_type,
            name=name,
            model=req.get("model", "sonnet"),
            mode=req.get("mode", "default"),
            pronouns=req.get("pronouns", ""),
            platforms=req.get("platforms"),
            heartbeat_interval=req.get("heartbeat_interval", 0),
            custom_soul=req.get("custom_soul", ""),
        )
        return {"type": heart_type, "name": name, "soul": soul}

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
        token = agents.set_token(
            name, platform, req.token,
            enabled=req.enabled, settings=req.settings, token_ref=req.token_ref,
        )

        # Dynamically start/restart a broker poller for Telegram tokens
        raw_token = agents.get_raw_token(name, platform)
        if platform == "telegram" and raw_token:
            try:
                from pinky_daemon.pollers import BrokerTelegramPoller
                from pinky_outreach.telegram import TelegramAdapter

                # Stop any existing poller for this agent
                for p in list(_broker_pollers):
                    if (
                        isinstance(p, BrokerTelegramPoller)
                        and p._agent_name == name
                    ):
                        p.stop()
                        _broker_pollers.remove(p)
                        _log(f"api: stopped old telegram poller for {name}")
                        break

                adapter = TelegramAdapter(raw_token, timeout=45.0)  # > poll_timeout (30s) to avoid racing
                poller = BrokerTelegramPoller(
                    adapter, name, broker, registry=agents,
                )
                _broker_pollers.append(poller)
                asyncio.create_task(poller.start())
                _log(f"api: started telegram poller for {name}")
            except Exception as e:
                _log(f"api: failed to start telegram poller for {name}: {e}")

        # Dynamically start/restart a broker poller for Discord tokens
        if platform == "discord" and raw_token:
            try:
                from pinky_daemon.pollers import BrokerDiscordPoller
                from pinky_outreach.discord import DiscordAdapter

                # Stop any existing poller for this agent
                for p in list(_broker_pollers):
                    if (
                        isinstance(p, BrokerDiscordPoller)
                        and p._agent_name == name
                    ):
                        p.stop()
                        _broker_pollers.remove(p)
                        _log(f"api: stopped old discord poller for {name}")
                        break

                # Optional per-token overrides come through token settings.
                settings = req.settings or {}
                poll_interval = float(settings.get("poll_interval_sec", 1.0))
                watched = settings.get("watched_channels") or None

                adapter = DiscordAdapter(raw_token)
                poller = BrokerDiscordPoller(
                    adapter, name, broker, registry=agents,
                    poll_interval=poll_interval,
                    watched_channels=watched,
                )
                _broker_pollers.append(poller)
                asyncio.create_task(poller.start())
                _log(
                    f"api: started discord poller for {name} "
                    f"(poll_interval={poll_interval}s, "
                    f"watched={'auto' if not watched else len(watched)})"
                )
            except Exception as e:
                _log(f"api: failed to start discord poller for {name}: {e}")

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

        # Stop broker poller if removing a Telegram token
        if platform == "telegram":
            from pinky_daemon.pollers import BrokerTelegramPoller
            for p in list(_broker_pollers):
                if (
                    isinstance(p, BrokerTelegramPoller)
                    and p._agent_name == name
                ):
                    p.stop()
                    _broker_pollers.remove(p)
                    _log(f"api: stopped telegram poller for {name}")
                    break

        # Stop broker poller if removing a Discord token
        if platform == "discord":
            from pinky_daemon.pollers import BrokerDiscordPoller
            for p in list(_broker_pollers):
                if (
                    isinstance(p, BrokerDiscordPoller)
                    and p._agent_name == name
                ):
                    p.stop()
                    _broker_pollers.remove(p)
                    _log(f"api: stopped discord poller for {name}")
                    break

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
                entry["env"] = _redact_env_secrets(cfg["env"])
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
                "env": _redact_env_secrets(json.loads(srv["env"])),
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

    # ── Ferry Peer-Fleet ACL ───────────────────────────────
    #
    # Separate identity primitive from approved_users. Ferry inbound is
    # *agents* addressing an agent; approved_users is *humans*. Default-deny.
    # See `pinky_daemon.ferry.types.AgentCardSelector` for selector shape.

    @app.get("/agents/{name}/peer-fleet-acl")
    async def get_peer_fleet_acl(name: str):
        """List the peer-fleet ACL selectors for an agent."""
        if not agents.has_agent(name):
            raise HTTPException(404, f"Agent '{name}' not found")
        return {
            "agent": name,
            "selectors": agents.get_peer_fleet_acl(name),
        }

    @app.post("/agents/{name}/peer-fleet-acl")
    async def add_peer_fleet_acl(name: str, req: PeerFleetAclEntryRequest):
        """Add one selector to an agent's peer-fleet ACL.

        Empty selectors (no fleet/agent_id/pinky_type set) are rejected with 400.
        Adds are idempotent — already-present selectors return success without
        creating duplicates.
        """
        if not agents.has_agent(name):
            raise HTTPException(404, f"Agent '{name}' not found")
        added = agents.add_peer_fleet_acl(
            name,
            fleet=req.fleet or None,
            agent_id=req.agent_id or None,
            pinky_type=req.pinky_type or None,
        )
        if not added:
            raise HTTPException(
                400,
                "selector requires at least one of fleet, agent_id, or pinky_type",
            )
        return {
            "agent": name,
            "added": True,
            "selectors": agents.get_peer_fleet_acl(name),
        }

    @app.put("/agents/{name}/peer-fleet-acl")
    async def set_peer_fleet_acl(name: str, req: PeerFleetAclSetRequest):
        """Replace the full peer-fleet ACL for an agent (full replacement)."""
        if not agents.has_agent(name):
            raise HTTPException(404, f"Agent '{name}' not found")
        selector_dicts = [
            {
                "fleet": s.fleet or None,
                "agent_id": s.agent_id or None,
                "pinky_type": s.pinky_type or None,
            }
            for s in req.selectors
        ]
        agents.set_peer_fleet_acl(name, selector_dicts)
        return {
            "agent": name,
            "selectors": agents.get_peer_fleet_acl(name),
        }

    @app.delete("/agents/{name}/peer-fleet-acl")
    async def remove_peer_fleet_acl(
        name: str,
        fleet: str = "",
        agent_id: str = "",
        pinky_type: str = "",
    ):
        """Remove all selectors matching the given query.

        Match is exact across all three fields (None == empty == "don't filter").
        Returns the count removed.
        """
        if not agents.has_agent(name):
            raise HTTPException(404, f"Agent '{name}' not found")
        removed = agents.remove_peer_fleet_acl(
            name,
            fleet=fleet or None,
            agent_id=agent_id or None,
            pinky_type=pinky_type or None,
        )
        return {
            "agent": name,
            "removed": removed,
            "selectors": agents.get_peer_fleet_acl(name),
        }

    # ── Ferry Mesh Outbound (mesh_remote_send) ─────────────
    #
    # Agent-driven outbound: build a ferry envelope and publish to the
    # mesh transport (NATS). Default-deny via per-agent allowlist; cred
    # never leaves the daemon process. See pinky_daemon.ferry.outbound.

    @app.get("/agents/{name}/mesh/allowlist")
    async def get_mesh_allowlist(name: str):
        """List the outbound allowlist patterns for an agent."""
        if not agents.has_agent(name):
            raise HTTPException(404, f"Agent '{name}' not found")
        return {
            "agent": name,
            "patterns": agents.get_mesh_outbound_allowlist(name),
        }

    @app.post("/agents/{name}/mesh/allowlist")
    async def add_mesh_allowlist(name: str, req: MeshAllowlistEntryRequest):
        """Add one pattern to an agent's outbound allowlist (idempotent)."""
        if not agents.has_agent(name):
            raise HTTPException(404, f"Agent '{name}' not found")
        added = agents.add_mesh_outbound_allowlist(name, req.pattern)
        if not added:
            raise HTTPException(400, "pattern must be non-empty")
        return {
            "agent": name,
            "added": True,
            "patterns": agents.get_mesh_outbound_allowlist(name),
        }

    @app.put("/agents/{name}/mesh/allowlist")
    async def set_mesh_allowlist(name: str, req: MeshAllowlistSetRequest):
        """Replace the full outbound allowlist for an agent."""
        if not agents.has_agent(name):
            raise HTTPException(404, f"Agent '{name}' not found")
        agents.set_mesh_outbound_allowlist(name, req.patterns)
        return {
            "agent": name,
            "patterns": agents.get_mesh_outbound_allowlist(name),
        }

    @app.delete("/agents/{name}/mesh/allowlist")
    async def remove_mesh_allowlist(name: str, pattern: str = ""):
        """Remove one pattern from an agent's outbound allowlist."""
        if not agents.has_agent(name):
            raise HTTPException(404, f"Agent '{name}' not found")
        removed = agents.remove_mesh_outbound_allowlist(name, pattern)
        return {
            "agent": name,
            "removed": removed,
            "patterns": agents.get_mesh_outbound_allowlist(name),
        }

    @app.get("/mesh/diagnostics")
    async def mesh_diagnostics():
        """Operator-facing health snapshot for the daemon's mesh sender.

        Reports whether creds + URL + nats CLI are wired without leaking
        the password.
        """
        from pinky_daemon.ferry.outbound import MeshSender
        return MeshSender().diagnostics()

    @app.post("/agents/{name}/mesh/send")
    async def mesh_send(name: str, req: MeshSendRequest):
        """Publish a ferry envelope to ``req.target`` on behalf of ``name``.

        Permission model: ``req.target`` must match at least one pattern
        in the agent's ``mesh_outbound_allowlist``. Default-deny.

        Returns ``{sent, correlation_id, subject, ts}`` on success;
        on failure the error is surfaced in ``error``.
        """
        from pinky_daemon.ferry.outbound import (
            MeshSender,
            allowlist_matches,
            build_envelope,
            parse_address,
        )

        if not agents.has_agent(name):
            raise HTTPException(404, f"Agent '{name}' not found")

        target = (req.target or "").strip()
        if not target:
            raise HTTPException(400, "target is required")
        if parse_address(target) is None:
            raise HTTPException(400, f"unparseable target address: {target!r}")

        allowlist = agents.get_mesh_outbound_allowlist(name)
        if not allowlist_matches(target, allowlist):
            raise HTTPException(
                403,
                f"target {target!r} not in agent's mesh_outbound_allowlist",
            )

        # Body shape: callers may pass a string (wrapped as {"text": str})
        # or a dict. ``kind`` is always set from the request.
        if isinstance(req.body, dict):
            body_dict = dict(req.body)
        else:
            body_dict = {"text": str(req.body or "")}
        body_dict["kind"] = req.kind or "msg"

        envelope = build_envelope(
            from_=f"ferry://pinkybot/{name}",
            to=target,
            body=body_dict,
            correlation_id=req.correlation_id or None,
            reply_to=req.reply_to or None,
            priority=req.priority or "normal",
        )

        sender = MeshSender()
        result = sender.send(envelope)

        # Audit log: every send attempt, success or failure. Persistence
        # failure must not propagate into the API response (defensive try).
        try:
            target_fleet, target_agent = parse_address(target) or ("", "")
            mesh_store.log_message(
                direction="outbound",
                local_agent=name,
                remote_fleet=target_fleet,
                remote_agent=target_agent,
                correlation_id=envelope.correlation_id or "",
                kind=envelope.body.get("kind", "msg"),
                body=envelope.body,
                envelope_ts=envelope.ts,
                reply_to=envelope.reply_to,
                error=result.error,
            )
        except Exception as exc:  # noqa: BLE001
            _log(f"mesh outbound log failed (non-fatal): {exc}")

        return {
            "sent": result.sent,
            "correlation_id": result.correlation_id,
            "subject": result.subject,
            "ts": result.ts,
            "error": result.error,
        }

    @app.get("/agents/{name}/mesh/inbox")
    async def get_mesh_inbox(
        name: str,
        limit: int = 50,
        offset: int = 0,
        kind: str = "",
        sender: str = "",
        direction: str = "",
    ):
        """List mesh messages for an agent, newest first.

        Filters: ``kind`` (exact), ``sender`` (``"agent@fleet"``),
        ``direction`` (``"inbound"`` | ``"outbound"`` | ``""``).
        """
        if not agents.has_agent(name):
            raise HTTPException(404, f"Agent '{name}' not found")
        messages = mesh_store.get_inbox(
            name,
            limit=limit,
            offset=offset,
            kind=kind,
            sender=sender,
            direction=direction,
        )
        return {
            "agent": name,
            "count": len(messages),
            "messages": [m.to_dict() for m in messages],
        }

    @app.get("/federation/peers")
    async def list_federation_peers(
        fleet: str = "",
        seed_source: str = "",
        limit: int = 200,
    ):
        """List known federation peers, newest-seen first.

        Filters: ``fleet`` (exact), ``seed_source`` (``"config"`` | ``"observed"``).
        """
        peers = mesh_store.list_peers(
            fleet=fleet, seed_source=seed_source, limit=limit,
        )
        return {
            "count": len(peers),
            "peers": [p.to_dict() for p in peers],
        }

    @app.post("/federation/peers")
    async def upsert_federation_peer(req: FederationPeerUpsertRequest):
        """Insert or update a federation peer (admin / config-seeded path).

        ``seed_source='config'`` is the stronger statement of intent — once
        set, an observed-update doesn't downgrade it.
        """
        if not (req.fleet and req.agent):
            raise HTTPException(400, "fleet and agent are required")
        seed = req.seed_source if req.seed_source in ("config", "observed") else "config"
        mesh_store.upsert_peer(
            fleet=req.fleet,
            agent=req.agent,
            display_name=req.display_name,
            seed_source=seed,
        )
        peer = mesh_store.get_peer(req.fleet, req.agent)
        return {"upserted": True, "peer": peer.to_dict() if peer else None}

    @app.delete("/federation/peers/{fleet}/{agent}")
    async def delete_federation_peer(fleet: str, agent: str):
        """Hard-delete a federation peer."""
        removed = mesh_store.remove_peer(fleet, agent)
        return {"removed": removed, "fleet": fleet, "agent": agent}

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

    @app.patch("/agents/{name}/streaming-sessions/{label}")
    async def rename_streaming_session(name: str, label: str, req: dict):
        """Rename a streaming session label."""
        new_label = (req.get("label") or "").strip()
        if not new_label:
            raise HTTPException(400, "New label is required")
        if label == "main":
            raise HTTPException(400, "Cannot rename the main session")
        sessions = broker._streaming.get(name, {})
        ss = sessions.get(label)
        if not ss:
            raise HTTPException(404, f"Streaming session '{label}' not found for {name}")
        if new_label in sessions:
            raise HTTPException(409, f"Session '{new_label}' already exists for {name}")
        # Move in broker registry
        sessions[new_label] = sessions.pop(label)
        # Update stored session ID mapping
        old_sid = agents.get_streaming_session_id(name, label=label)
        if old_sid:
            agents.set_streaming_session_id(name, old_sid, label=new_label)
            agents.set_streaming_session_id(name, "", label=label)
        # Rename conversation history so it follows the new session ID
        old_conv_id = f"{name}-{label}"
        new_conv_id = f"{name}-{new_label}"
        store.rename_session(old_conv_id, new_conv_id)
        return {"renamed": True, "agent": name, "old_label": label, "new_label": new_label}

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
    async def broker_send_message(req: dict, request: Request):
        """Send an outbound message through the broker on behalf of an agent."""
        agent_name = req.get("agent_name", "")
        platform = req.get("platform", "telegram")
        chat_id = req.get("chat_id", "")
        content = req.get("content", "")
        reply_to = req.get("reply_to", "")
        parse_mode = req.get("parse_mode", "")
        if not agent_name or not chat_id or not content:
            raise HTTPException(400, "agent_name, chat_id, and content are required")
        _deny_isolated_cross_actor(request, agent_name)
        try:
            result = await _broker_send(
                agent_name,
                platform,
                chat_id,
                content,
                reply_to=reply_to,
                parse_mode=parse_mode,
            )
        except HTTPException:
            raise
        except Exception as e:
            _log(f"broker-send: failed for {agent_name} -> {platform}:{chat_id}: {e}")
            raise HTTPException(502, f"Failed to send message: {e}")
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
    async def broker_thread(req: dict, request: Request):
        """Send a threaded/quoted reply to an inbound message using stored broker context."""
        agent_name = req.get("agent_name", "")
        source_message_id = req.get("message_id", "")
        content = req.get("content", "").strip()
        parse_mode = req.get("parse_mode", "")
        if not agent_name or not source_message_id or not content:
            raise HTTPException(400, "agent_name, message_id, and content are required")
        _deny_isolated_cross_actor(request, agent_name)

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
    async def broker_broadcast(req: dict, request: Request):
        """Broadcast a message to all active channels for an agent."""
        agent_name = req.get("agent_name", "")
        content = req.get("content", "").strip()
        if not agent_name or not content:
            raise HTTPException(400, "agent_name and content are required")
        _deny_isolated_cross_actor(request, agent_name)

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

    def _outreach_attempt_log(
        *,
        agent_name: str,
        platform: str,
        method: str,
        chat_id: str,
        file_path: str,
        caption_len: int,
        outcome: OutreachOutcome,
        error: str = "",
    ) -> None:
        """Structured outreach-attempt log line (issue #395).

        Logs payload-shape only — no payload contents, no raw file path values
        beyond extension — so log files stay correlatable without leaking
        user content.

        Deliberately does NOT call os.path.getsize/exists on the user-supplied
        file_path: even though the adapter ultimately opens the file anyway,
        adding new filesystem reads in the route handler trips CodeQL's
        path-injection detector. file_ext is pure string manipulation and is
        sufficient to discriminate the usual 4xx culprits (wrong mime, 0-byte,
        unsupported format) when correlated with the upstream error message.
        """
        suffix = Path(file_path).suffix.lower() if file_path else ""
        parts = [
            f"agent={agent_name}",
            f"platform={platform}",
            f"method={method}",
            f"chat_id={chat_id}",
            f"file_ext={suffix}",
            f"caption_len={caption_len}",
            f"outcome={outcome}",
        ]
        if error:
            parts.append(f"error={error}")
        _log("outreach-attempt: " + " ".join(parts))

    async def _broker_send_file_route(
        req: dict,
        request: Request,
        *,
        kind: str,  # "photo" or "document"
        method: str,  # "send_photo" or "send_document"
    ) -> dict:
        """Shared implementation for /broker/send-photo and /broker/send-document.

        Wraps the upstream platform call (which can raise TelegramError, FileNotFoundError,
        httpx errors, etc.) in try/except so failures surface as a structured 502 instead
        of an unhandled ASGI exception (issue #395). Always stops the typing indicator
        in `finally` so a failed send doesn't leave the chat showing "typing…" forever.
        """
        agent_name = req.get("agent_name", "")
        _deny_isolated_cross_actor(request, agent_name)  # #149 body-actor guard
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

        loop = asyncio.get_running_loop()
        try:
            msg = await loop.run_in_executor(
                None,
                lambda: _send_file_message(
                    agent_name, platform, chat_id, file_path,
                    caption=caption, reply_to=reply_to, kind=kind,
                ),
            )
        except HTTPException:
            # Already structured — let FastAPI render it. Still stop typing.
            # HTTPExceptions reachable here come from _send_file_message's
            # config-side validation (HTTPException(503, "No {platform} adapter")
            # / HTTPException(400, "Unsupported platform")) → caller-side
            # `rejected`.
            _outreach_attempt_log(
                agent_name=agent_name, platform=platform, method=method,
                chat_id=chat_id, file_path=file_path, caption_len=len(caption),
                outcome="rejected", error="HTTPException",
            )
            raise
        except FileNotFoundError as e:
            # Carve-out before the generic catch-all: FileNotFoundError on the
            # caller-supplied `file_path` happens when the adapter does
            # `open(file_path, "rb")` and the path doesn't exist or isn't
            # readable. Critically, NO Telegram/OpenAI/upstream call has been
            # made yet — this is purely a caller-side validation failure
            # (they handed us a path we can't read), so it belongs in the
            # `rejected` bucket, not `error_upstream`. Status 400 reflects the
            # bad-input semantics (was 502 before #408 follow-up).
            error_repr = f"{type(e).__name__}: {e}"
            _outreach_attempt_log(
                agent_name=agent_name, platform=platform, method=method,
                chat_id=chat_id, file_path=file_path, caption_len=len(caption),
                outcome="rejected", error=error_repr,
            )
            raise HTTPException(400, f"Failed to {method}: {error_repr}") from e
        except Exception as e:
            # Bare catch-all wraps adapter.send_{photo,document,animation},
            # which raise TelegramError, httpx errors, etc. All are produced
            # downstream of our handler against a real network/SDK call, so
            # bucket as `error_upstream`. (FileNotFoundError is split out
            # above as a caller-side `rejected`.)
            error_repr = f"{type(e).__name__}: {e}"
            _outreach_attempt_log(
                agent_name=agent_name, platform=platform, method=method,
                chat_id=chat_id, file_path=file_path, caption_len=len(caption),
                outcome="error_upstream", error=error_repr,
            )
            raise HTTPException(502, f"Failed to {method}: {error_repr}") from e
        finally:
            # Typing indicator must stop regardless of success/failure so a
            # failed send doesn't leave "typing…" stuck.
            broker._stop_typing(agent_name, chat_id)

        result = {"sent": True, "message_id": msg.message_id, "platform": platform, "chat_id": chat_id}
        _outreach_attempt_log(
            agent_name=agent_name, platform=platform, method=method,
            chat_id=chat_id, file_path=file_path, caption_len=len(caption),
            outcome="success",
        )
        if kind == "photo":
            content_default = "[photo]"
        elif kind == "animation":
            content_default = f"[animation] {Path(file_path).name}"
        else:
            content_default = f"[document] {Path(file_path).name}"
        _record_outbound_message(
            agent_name,
            platform=platform,
            chat_id=chat_id,
            content=caption or content_default,
            # PII-safe: record argument key names only, not the raw file_path.
            # Matches the arg_keys pattern used in codex_session.py / streaming_session.py.
            metadata={"tool": method, "source_message_id": source_message_id, "arg_keys": ["file_path"], "delivery": result},
        )
        return result

    @app.post("/broker/send-photo")
    async def broker_send_photo(req: dict, request: Request):
        """Send a photo through the broker on behalf of an agent."""
        return await _broker_send_file_route(req, request, kind="photo", method="send_photo")

    @app.post("/broker/send-document")
    async def broker_send_document(req: dict, request: Request):
        """Send a document through the broker on behalf of an agent."""
        return await _broker_send_file_route(req, request, kind="document", method="send_document")

    @app.post("/broker/send-gif")
    async def broker_send_gif(req: dict, request: Request):
        """Search Giphy and send the result as an animation through the broker.

        API key is resolved from system settings, then env var, then a public
        fallback key (rate-limited — set GIPHY_API_KEY in Settings for prod).
        """
        import random
        import tempfile
        import urllib.parse

        giphy_public_key = "dc6zaTOxFJmzC"

        agent_name_req = req.get("agent_name", "")
        _deny_isolated_cross_actor(request, agent_name_req)  # #149 body-actor guard
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
        api_key = agents.get_setting("GIPHY_API_KEY") or os.environ.get("GIPHY_API_KEY", giphy_public_key)

        # Search Giphy
        params = urllib.parse.urlencode({
            "api_key": api_key,
            "q": query,
            "limit": 10,
            "rating": "g",
            "lang": "en",
        })
        search_url = f"https://api.giphy.com/v1/gifs/search?{params}"

        loop = asyncio.get_running_loop()

        def _search_giphy():
            with urllib.request.urlopen(search_url, timeout=10) as resp:
                return json.loads(resp.read())

        # Issue #397 follow-up (Murzik P1): widen the try/finally to cover the
        # Giphy search step AND the no-results 404 path, not just download+send.
        # Previously, a search timeout or empty-results response would raise
        # before reaching the cleanup block, leaving the typing indicator stuck
        # — same class of bug as the original incident #395.
        try:
            try:
                data = await loop.run_in_executor(None, _search_giphy)
            except Exception as e:
                # Giphy search failure (urlopen timeout / network / HTTP error)
                # — purely upstream.
                error_repr = f"Giphy search failed: {e}"
                _outreach_attempt_log(
                    agent_name=agent_name_req, platform=platform, method="send_gif",
                    chat_id=chat_id, file_path="", caption_len=len(caption),
                    outcome="error_upstream", error=error_repr,
                )
                raise HTTPException(502, error_repr) from e

            results = data.get("data", [])
            if not results:
                # Giphy returned 200 with an empty result set: not an upstream
                # failure (the API answered correctly), but the caller's query
                # didn't match anything. Treat as caller-side `rejected` —
                # nothing for us to send back.
                _outreach_attempt_log(
                    agent_name=agent_name_req, platform=platform, method="send_gif",
                    chat_id=chat_id, file_path="", caption_len=len(caption),
                    outcome="rejected", error="no_results",
                )
                raise HTTPException(404, f"No GIFs found for query: {query!r}")

            # Pick randomly from top 5 for variety
            pick = random.choice(results[:min(5, len(results))])
            gif_url = pick["images"]["original"]["url"].split("?")[0]

            # Download GIF to a temp file and send via adapter (all blocking I/O)
            def _download_and_send():
                with tempfile.NamedTemporaryFile(suffix=".gif", delete=False) as tmp:
                    tmp_path = tmp.name
                try:
                    with urllib.request.urlopen(gif_url, timeout=30) as resp:
                        with open(tmp_path, "wb") as f:
                            f.write(resp.read())
                    return adapter.send_animation(chat_id, tmp_path, caption=caption, reply_to_message_id=int(reply_to) if reply_to else None)
                finally:
                    try:
                        os.unlink(tmp_path)
                    except Exception:
                        pass

            # Issue #395 follow-up: wrap the download+send in try/except so a
            # failure surfaces as a structured 502 (not 500/unhandled). The
            # outer finally below ensures _stop_typing fires on every exit path.
            try:
                msg = await loop.run_in_executor(None, _download_and_send)
            except HTTPException:
                # _download_and_send wraps urlopen + adapter.send_animation;
                # neither raises HTTPException directly today, so this branch
                # is defensive. If an HTTPException ever does surface here it
                # came from a config-side check, so bucket as `rejected`.
                _outreach_attempt_log(
                    agent_name=agent_name_req, platform=platform, method="send_gif",
                    chat_id=chat_id, file_path="", caption_len=len(caption),
                    outcome="rejected", error="HTTPException",
                )
                raise
            except Exception as e:
                # urlopen (download) + adapter.send_animation (Telegram) —
                # both upstream.
                error_repr = f"{type(e).__name__}: {e}"
                _outreach_attempt_log(
                    agent_name=agent_name_req, platform=platform, method="send_gif",
                    chat_id=chat_id, file_path="", caption_len=len(caption),
                    outcome="error_upstream", error=error_repr,
                )
                raise HTTPException(502, f"Failed to send_gif: {error_repr}") from e
        finally:
            broker._stop_typing(agent_name_req, chat_id)

        result = {"sent": True, "message_id": msg.message_id, "query": query, "platform": platform, "chat_id": chat_id}
        _outreach_attempt_log(
            agent_name=agent_name_req, platform=platform, method="send_gif",
            chat_id=chat_id, file_path="", caption_len=len(caption),
            outcome="success",
        )
        _record_outbound_message(
            agent_name_req,
            platform=platform,
            chat_id=chat_id,
            content=caption or f"[gif] {query}",
            metadata={"tool": "send_gif", "source_message_id": source_message_id, "query": query, "delivery": result},
        )
        return result

    @app.post("/broker/send-animation")
    async def broker_send_animation(req: dict, request: Request):
        """Send an animation (GIF) through the broker on behalf of an agent.

        Issue #395 follow-up: routed through the shared _broker_send_file_route
        helper so failures surface as structured 502 and the typing indicator
        is always torn down in `finally`.
        """
        return await _broker_send_file_route(req, request, kind="animation", method="send_animation")

    @app.post("/broker/send-voice")
    async def broker_send_voice(req: dict, request: Request):
        """Generate TTS audio and send as a voice message through the broker.

        Supports ElevenLabs, OpenAI TTS, and Deepgram Aura. API keys are
        read from system settings (settings panel) or env vars.
        """
        agent_name_req = req.get("agent_name", "")
        _deny_isolated_cross_actor(request, agent_name_req)  # #149 body-actor guard
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
    async def broker_react(req: dict, request: Request):
        """Add a reaction through the broker on behalf of an agent."""
        agent_name = req.get("agent_name", "")
        _deny_isolated_cross_actor(request, agent_name)  # #149 body-actor guard
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
        # Log reaction in conversation history so the UI can display it
        session_id = _session_id_for_agent(agent_name)
        store.append(
            session_id,
            "system",
            f"reacted {emoji} to message",
            platform=platform,
            chat_id=chat_id,
            metadata={"reaction": True, "emoji": emoji, "target_message_id": message_id},
        )
        return {"reacted": True}

    @app.post("/agents/{name}/streaming/restart")
    async def restart_streaming_session(name: str):
        """Restart an agent's streaming session — fresh context, new CC session."""
        ss = broker._get_streaming_session(name)
        if not ss:
            raise HTTPException(404, f"No streaming session for '{name}'")

        # #149 P1: restart relaunches the transport — guard before we disconnect,
        # so a blocked (e.g. relabeled unix_user) agent isn't torn down then left
        # down. An unrunnable isolation_mode means "do not (re)spawn".
        _enforce_isolation_runnable(name)

        guard = _get_streaming_restart_guard(name, ss)
        if not guard["restart_safe"]:
            raise HTTPException(409, guard["message"])

        old_resume_handle = ss.resume_handle
        old_turns = ss._stats["turns"]

        # #667 diag: capture pre-restart live-session state so an orphan that's
        # recovered by this (often EXTERNAL) restart isn't a black box. The
        # connected/state fields distinguish a connected-but-MCP-wedged orphan
        # from a disconnected-dead one — the open question from the 2026-06-02
        # power-loss incident. Fail-safe: never block a restart on diagnostics.
        try:
            _st = ss.stats if hasattr(ss, "stats") else {}
            _log(
                f"#667 diag: pre-restart state for {name}: "
                f"transport={type(ss).__name__} state={_st.get('state')!r} "
                f"connected={_st.get('connected')} turns={_st.get('turns')} "
                f"resume_handle={'set' if old_resume_handle else 'empty'}"
            )
        except Exception:
            pass

        # Disconnect and clear persisted resume handle
        await ss.disconnect()
        agents.set_streaming_session_id(name, "", label="main")

        # Refresh wake context and reconnect fresh
        ss._config.wake_context = _build_streaming_wake_context(name, commit=False)
        ss._config.resume_handle = ""
        ss._config.restart_reason = "context_restart"
        # PR for #543: explicit launch-behavior contract — the next
        # transport spawn must NOT resume the prior conversation. For
        # SDK this is implicit (resume_handle="" already accomplishes
        # it), but tmux ``_build_claude_cmd`` checks transcript existence
        # only and would silently re-apply ``--continue`` unless this
        # flag is set. One-shot: consumed by the transport on the next
        # launch and reset to False.
        ss._config.force_fresh_context_once = True
        ss.resume_handle = ""
        # Codex sessions track the thread_id separately on the session object;
        # clear it too or `codex exec resume <stale-id>` keeps firing next turn.
        if hasattr(ss, "codex_session_id"):
            if ss.codex_session_id:
                _log(f"api: clearing stale codex thread {ss.codex_session_id[:12]} for {name}")
            ss.codex_session_id = ""
        try:
            await ss.connect()
            _log(f"api: streaming session restarted for {name}")
            activity.log(name, "context_restart", f"{name} context restarted")
            session_event_store.log(
                session_id=ss.id,
                agent_name=name,
                event_type="context_restart",
                metadata={"label": "main", "old_session_id": old_resume_handle[:12] if old_resume_handle else ""},
            )
        except Exception as e:
            broker.unregister_streaming(name)
            raise HTTPException(500, f"Failed to restart: {e}")

        return {
            "restarted": True,
            "agent": name,
            "old_session_id": old_resume_handle[:12] if old_resume_handle else "",
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
        if ss.state != TransportSessionState.CONNECTED or not ss._client:
            raise HTTPException(409, f"Streaming session for '{name}' not connected")

        # Check if context window would change
        old_max = 0
        try:
            ctx = await asyncio.wait_for(ss._client.get_context_usage(), timeout=5.0)
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
            guard = _get_streaming_restart_guard(name, ss)
            if not guard["restart_safe"]:
                raise HTTPException(409, _guard_message("restart", guard))

            # Ask agent to save state
            try:
                await ss._client.query(
                    "Model is being changed and your session will restart for a new context window. "
                    "Quickly save your current state to wake context or memory files."
                )
            except Exception:
                pass

            # Restart streaming session
            old_resume_handle = ss.resume_handle
            old_turns = ss._stats["turns"]
            await ss.disconnect()
            agents.set_streaming_session_id(name, "", label="main")
            ss._config.resume_handle = ""
            ss.resume_handle = ""
            if hasattr(ss, "codex_session_id"):
                if ss.codex_session_id:
                    _log(f"api: clearing stale codex thread {ss.codex_session_id[:12]} for {name}")
                ss.codex_session_id = ""
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
                "old_session_id": old_resume_handle[:12] if old_resume_handle else "",
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
        if ss.state != TransportSessionState.CONNECTED:
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
        if ss.state != TransportSessionState.CONNECTED:
            raise HTTPException(409, f"Streaming session for '{name}' not connected")

        guard = _get_streaming_restart_guard(name, ss)
        if not guard["restart_safe"]:
            raise HTTPException(409, _guard_message("restart", guard))

        # Step 1: Ask agent to save memories before archiving
        try:
            await ss._client.query(
                "Your session is about to be archived. Before it ends:\n\n"
                "1. Use reflect() or save_my_context to persist key learnings and state\n"
                "2. Summarize what you were working on so your next session can pick up\n\n"
                "Do this now — your session will be reset after you confirm."
            )
            _log(f"api: archive memory save requested for {name}")
        except Exception as e:
            _log(f"api: archive memory save failed for {name}: {e}")

        # Step 2: Restart with fresh context
        old_resume_handle = ss.resume_handle
        old_turns = ss._stats["turns"]

        await ss.disconnect()
        agents.set_streaming_session_id(name, "", label="main")

        ss._config.wake_context = _build_streaming_wake_context(name, commit=False)
        ss._config.resume_handle = ""
        ss.resume_handle = ""
        if hasattr(ss, "codex_session_id"):
            if ss.codex_session_id:
                _log(f"api: clearing stale codex thread {ss.codex_session_id[:12]} for {name}")
            ss.codex_session_id = ""
        try:
            await ss.connect()
            _log(f"api: archived and restarted session for {name}")
        except Exception as e:
            broker.unregister_streaming(name)
            raise HTTPException(500, f"Failed to restart after archive: {e}")

        return {
            "archived": True,
            "agent": name,
            "old_session_id": old_resume_handle[:12] if old_resume_handle else "",
            "old_turns": old_turns,
        }

    @app.get("/agents/{name}/streaming/status")
    async def streaming_session_status(name: str, label: str = "main"):
        """Get streaming session status for an agent (optionally by sub-session label)."""
        sessions = broker._streaming.get(name, {})
        ss = sessions.get(label) if sessions else None
        if not ss:
            # Fall back to main if requested label doesn't exist
            ss = sessions.get("main") if sessions else None
        if not ss:
            raise HTTPException(404, f"No streaming session for '{name}'")
        guard = _get_streaming_restart_guard(name, ss)

        # Use cached context info to avoid blocking (refreshed in background)
        ctx = await _streaming_context_info(ss)
        context_info = {}
        if ctx:
            context_info = {
                "total_tokens": ctx.get("total_tokens", 0),
                "max_tokens": ctx.get("max_tokens", 0),
                "percentage": ctx.get("percentage", 0),
                "categories": ctx.get("categories", []),
                "mcp_tools": ctx.get("mcp_tools", []),
            }

        return {
            "agent": name,
            "session_id": ss.resume_handle[:12] if ss.resume_handle else "",
            "connected": ss.state == TransportSessionState.CONNECTED,
            "stats": ss.stats,
            "context": context_info,
            "saved_context": guard,
        }

    @app.get("/agents/{name}/session-meta")
    async def get_agent_session_meta(name: str, label: str = "main"):
        """Get a concise metadata summary for an agent's live streaming session.

        Returns session_id, context percentage, model, cost, turns, uptime, and provider.
        Useful for chat UI headers and dashboards.
        """
        agent = agents.get(name)
        if not agent:
            raise HTTPException(404, f"Agent '{name}' not found")

        sessions = broker._streaming.get(name, {})
        ss = sessions.get(label) or sessions.get("main")
        if not ss:
            return {
                "agent": name,
                "session_id": agents.get_streaming_session_id(name, label=label) or "",
                "context_pct": 0,
                "resume_id": "",
                "model": agent.model or "",
                "cost_usd": 0.0,
                "turns": 0,
                "uptime_seconds": 0,
                "provider": "",
                "connected": False,
            }

        # Best-effort context percentage (cached to avoid blocking)
        ctx_info = await _streaming_context_info(ss)
        context_pct = round(ctx_info.get("percentage", 0))

        provider = (ss.account_info.get("apiProvider") or "").lower()
        if not provider and getattr(ss._config, "provider_url", ""):
            provider = "custom"
        elif not provider:
            provider = "anthropic"

        # Effort info
        default_effort = agent.thinking_effort or "medium"
        session_effort = getattr(ss, "_effort_override", None)
        effective_effort = session_effort or default_effort

        return {
            "agent": name,
            "session_id": ss.resume_handle or "",
            "context_pct": context_pct,
            "resume_id": ss.resume_handle or "",
            "model": ss._config.model or agent.model or "",
            "cost_usd": round(ss.usage.total_cost_usd, 4),
            "turns": ss._stats.get("turns", 0),
            "uptime_seconds": round(time.time() - ss.created_at),
            "provider": provider,
            "connected": ss.state == TransportSessionState.CONNECTED,
            "default_effort": default_effort,
            "session_effort": session_effort,
            "effective_effort": effective_effort,
        }

    @app.post("/agents/{name}/message")
    async def send_agent_message_direct(name: str, req: AgentMessageRequest):
        """Send a message from one agent directly into another's streaming context.

        Always stores in comms DB for audit trail. If the target agent is
        online, also injects into their streaming context immediately.
        """
        if not agents.get(name):
            raise HTTPException(404, f"Agent '{name}' not found")
        # Always store in comms DB for audit/visibility
        msg = comms.send(
            req.from_agent, name, req.message,
            metadata=req.metadata,
            content_type=req.content_type,
            parent_message_id=req.parent_message_id,
            priority=req.priority,
        )
        delivered = await broker.inject_agent_message(
            req.from_agent, name, req.message,
        )
        # Auto-wake sleeping agents on inter-agent messages
        if not delivered:
            _log(f"api: agent message {req.from_agent} -> {name} — target offline, auto-waking")
            try:
                streaming = await _ensure_streaming_session(name, label="main")
                if streaming and streaming.state == TransportSessionState.CONNECTED:
                    delivered = await broker.inject_agent_message(
                        req.from_agent, name, req.message,
                    )
                    if delivered:
                        _log(f"api: agent message delivered after auto-wake {name}")
            except Exception as e:
                _log(f"api: auto-wake failed for {name}: {e}")
        if delivered:
            # Agent saw it live — mark as read so it doesn't repeat on wake
            comms.mark_read(name, [msg.id])
        return {
            "delivered": delivered,
            "queued": not delivered,
            "message_id": msg.id,
            "from": req.from_agent,
            "to": name,
        }

    @app.post("/agents/{name}/chat")
    async def chat_with_agent(name: str, req: dict, session: str = "main"):
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

        label = session or "main"

        # Get streaming session by label — auto-wake if not connected
        sessions = broker._streaming.get(name, {})
        streaming = sessions.get(label)
        if not streaming or streaming.state != TransportSessionState.CONNECTED:
            _log(f"api: chat to '{name}' session '{label}' — not connected, auto-waking")
            streaming = await _ensure_streaming_session(name, label=label)
            if not streaming:
                raise HTTPException(503, f"Agent '{name}' session '{label}' could not be started")

        # Format with metadata like broker messages
        from datetime import datetime, timezone
        from zoneinfo import ZoneInfo
        tz_str = agents.get_default_timezone()
        try:
            ts = datetime.now(ZoneInfo(tz_str)).strftime(f"%Y-%m-%d %H:%M:%S {tz_str}")
        except Exception:
            # datetime.utcnow() is deprecated in Python 3.12. #294
            ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

        prompt = f"[web | dm | Admin | web | {ts}]\n{content}"
        await streaming.send(prompt, platform="web", chat_id="web")
        return {"sent": True, "agent": name}

    @app.get("/agents/{agent_name}/streaming/events")
    async def stream_agent_events(agent_name: str, label: str = "main"):
        """SSE stream for incremental agent events (Codex CLI partials/tool calls)."""
        session_stream_id = f"{agent_name}-{label}"
        queue: asyncio.Queue = asyncio.Queue(maxsize=200)
        _stream_event_subscribers.setdefault(session_stream_id, set()).add(queue)

        async def event_generator():
            try:
                while True:
                    try:
                        payload = await asyncio.wait_for(queue.get(), timeout=10.0)
                        yield f"data: {json.dumps(payload)}\n\n"
                    except asyncio.TimeoutError:
                        yield f"data: {json.dumps({'type': 'ping', 'session_id': session_stream_id, 'timestamp': time.time()})}\n\n"
            finally:
                subs = _stream_event_subscribers.get(session_stream_id)
                if subs:
                    subs.discard(queue)
                    if not subs:
                        _stream_event_subscribers.pop(session_stream_id, None)

        return StreamingResponse(event_generator(), media_type="text/event-stream")

    @app.get("/agents/{agent_name}/tmux/pane/stream")
    async def stream_tmux_pane(
        agent_name: str,
        label: str = "main",
        interval_ms: int = 400,
        lines: int = 200,
        cols: int = 0,
        rows: int = 0,
    ):
        """Stream the live tmux pane contents as SSE snapshots.

        Read-only viewer for the agent's terminal — the chat UI's
        xterm.js modal subscribes to this and renders each frame.
        ANSI escape sequences are preserved (``capture-pane -e``) so
        colors + cursor positioning come through.

        Each SSE event is a JSON object:
            {"type": "snapshot", "data": "...", "ts": 1234.5}
        ``data`` is the raw ``capture-pane`` output. The client clears
        + redraws on each frame (cheap because the captured content is
        a static screen state, not an event log).

        404 if the agent doesn't exist. If the session isn't a tmux
        runtime (or hasn't booted yet) the stream emits a single
        ``{"type": "not_tmux"}`` event and closes — the modal renders
        a helpful message instead of an empty terminal.

        ``interval_ms`` is clamped to [100, 2000] to bound the rate.
        ``lines`` is clamped to [10, 1000] — generous range covers
        short panes (tmux REPL header) up to long scrollback dumps.

        ``cols`` / ``rows`` (optional) reshape the agent's tmux pane
        to those dimensions before streaming begins. Used by the modal
        to make the captured snapshot fill the viewer instead of
        sitting at tmux's 80×24 detached default. The pane is shared
        across viewers — last writer wins. Reconnect with new dims to
        resize again (modal does this on container resize). ``0``
        (default) means "don't touch the pane size."
        """
        agent = agents.get(agent_name)
        if not agent:
            raise HTTPException(404, f"Agent '{agent_name}' not found")

        # Clamp to defend against accidental tight loops or huge captures.
        interval_s = max(0.1, min(2.0, interval_ms / 1000.0))
        line_count = max(10, min(1000, lines))
        # Honour caller dims only when both are positive; tmux's own
        # clamping (in TmuxRunner.resize_window) handles upper bounds.
        want_resize = cols > 0 and rows > 0

        async def event_generator():
            session = broker.get_streaming_session(agent_name, label=label)
            snap = getattr(session, "get_pane_snapshot", None)
            if session is None or not callable(snap):
                # SDK / codex / cold session — no tmux pane to show.
                yield (
                    'data: '
                    + json.dumps({
                        "type": "not_tmux",
                        "agent": agent_name,
                        "label": label,
                    })
                    + '\n\n'
                )
                return

            if want_resize:
                resize = getattr(session, "resize_pane", None)
                if callable(resize):
                    try:
                        await resize(cols=cols, rows=rows)
                    except Exception as e:  # pragma: no cover — defensive
                        _log(
                            f"api: stream_tmux_pane resize raised for "
                            f"{agent_name}: {e}"
                        )
                    # Brief settle so the first capture reflects the new
                    # geometry (Claude Code's TUI redraws on SIGWINCH).
                    await asyncio.sleep(0.15)

            try:
                while True:
                    try:
                        snapshot = await snap(lines=line_count)
                    except Exception as e:  # pragma: no cover — defensive
                        _log(
                            f"api: stream_tmux_pane snapshot raised for "
                            f"{agent_name}: {e}"
                        )
                        snapshot = ""
                    payload = {
                        "type": "snapshot",
                        "data": snapshot,
                        "ts": time.time(),
                    }
                    yield f"data: {json.dumps(payload)}\n\n"
                    await asyncio.sleep(interval_s)
            except asyncio.CancelledError:
                # Client disconnected — exit cleanly.
                raise

        return StreamingResponse(
            event_generator(), media_type="text/event-stream",
        )

    @app.get("/agents/{agent_name}/sessions/{label}/tool-calls")
    async def list_session_tool_calls(
        agent_name: str,
        label: str = "main",
        limit: int = 500,
    ):
        """Return persisted tool calls for an agent session, oldest first.

        Backs the chat UI's chip-strip rebuild on refresh: ``liveToolCalls``
        is transient SSE state and evaporates on reload, so on chat load
        the frontend pulls the persisted analytics rows (tmux hooks write
        them in PR #527) and interleaves them with messages by timestamp.

        404 if the agent doesn't exist. Returns an empty list (not 404)
        when the agent exists but has no analytics rows yet — keeps the
        frontend's fetch + merge path branch-free.

        ``limit`` is clamped by ``get_recent_tool_calls`` to [1, 500];
        we accept a wider range and let the store cap it.
        """
        agent = agents.get(agent_name)
        if not agent:
            raise HTTPException(404, f"Agent '{agent_name}' not found")

        session_id = f"{agent_name}-{label or 'main'}"
        try:
            rows = analytics.get_recent_tool_calls(
                agent_name=agent_name,
                session_id=session_id,
                limit=limit,
            )
        except Exception as e:
            _log(
                f"api: list_session_tool_calls fetch raised for "
                f"{agent_name}/{label}: {e}"
            )
            rows = []

        # get_recent_tool_calls returns newest-first; flip for chronological
        # consumption (chat UI walks the array in display order).
        rows.reverse()

        out: list[dict] = []
        for row in rows:
            meta = row.get("metadata") or {}
            err = row.get("error_type") or ""
            out.append({
                "tool_use_id": row.get("tool_call_key"),
                "tool_name": row.get("tool_name") or "",
                "tool_namespace": row.get("tool_namespace") or "",
                "description": meta.get("description") or "",
                "arg_keys": meta.get("arg_keys") or [],
                "result_preview": meta.get("result_preview") or "",
                "started_at": row.get("started_at"),
                "ended_at": row.get("ended_at"),
                "duration_ms": row.get("duration_ms"),
                "success": (
                    bool(row.get("success"))
                    if row.get("success") is not None
                    else None
                ),
                "error_type": err,
                "status": row.get("status") or "",
                "is_error": bool(err),
                "finished": row.get("status") not in ("running", None, ""),
            })
        return {
            "ok": True,
            "agent": agent_name,
            "session_id": session_id,
            "tool_calls": out,
        }

    @app.post("/agents/{name}/upload")
    async def upload_file_to_agent(name: str, file: UploadFile):
        """Upload a file to an agent via the web UI."""
        agent = agents.get(name)
        if not agent:
            raise HTTPException(404, f"Agent '{name}' not found")

        # Save file to data/uploads/{agent_name}/
        upload_dir = f"data/uploads/{name}"
        os.makedirs(upload_dir, exist_ok=True)
        # Strip directory components, then validate against a strict allowlist.
        # basename neutralizes "../" and absolute paths; the anchored regex is
        # the path-injection barrier — no path separator can survive it. A name
        # that doesn't match gets a safe generated name rather than a rejection,
        # so odd-but-harmless filenames still upload.
        filename = os.path.basename(file.filename or "")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._() -]{0,127}", filename):
            filename = f"upload_{int(time.time())}"
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
        from datetime import datetime, timezone
        from zoneinfo import ZoneInfo
        tz_str = agents.get_default_timezone()
        try:
            ts = datetime.now(ZoneInfo(tz_str)).strftime(f"%Y-%m-%d %H:%M:%S {tz_str}")
        except Exception:
            # datetime.utcnow() is deprecated in Python 3.12. #294
            ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

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

        # Resolve provider_ref for legacy session spawn
        _spawn_provider_url, _spawn_provider_key, _spawn_provider_model = _resolve_agent_provider(agent)

        session = manager.create(
            session_id=session_id,
            model=_spawn_provider_model or agent.model,
            soul=agent.soul,
            working_dir=str(work_dir),
            allowed_tools=agent.allowed_tools or None,
            disallowed_tools=agent.disallowed_tools or None,
            max_turns=agent.max_turns,
            timeout=agent.timeout,
            system_prompt=system_prompt,
            restart_threshold_pct=agent.restart_threshold_pct,
            auto_restart=agent.auto_restart,
            permission_mode=agent.permission_mode,
            session_type=session_type,
            agent_name=name,
            provider_url=_spawn_provider_url,
            provider_key=_spawn_provider_key,
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
            one_shot=req.one_shot,
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
            metadata=req.metadata, notes=req.notes, latency_ms=req.latency_ms,
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
        if streaming_main and streaming_main.resume_handle:
            session_id = streaming_main.resume_handle
        for s in manager.list():
            if not session_id and s.agent_name == agent_name and s.session_type == "main":
                session_id = s.id
                break

        ctx = agents.set_context(
            agent_name,
            task=req.task, context=req.context, notes=req.notes,
            blockers=req.blockers, priority_items=req.priority_items,
            wake_action=req.wake_action,
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
            return {
                "agent_name": agent_name,
                "context": None,
                "freshness": _get_saved_context_freshness(agent_name),
            }
        return {
            **ctx.to_dict(),
            "freshness": _get_saved_context_freshness(agent_name),
        }

    @app.delete("/agents/{agent_name}/context")
    async def clear_agent_context(agent_name: str):
        """Clear the continuation context after consumption."""
        agents.clear_context(agent_name)
        return {"cleared": True}

    # Note: `POST /agents/{name}/sleep` (deep_sleep_agent) was removed
    # in #552. It was a Pulse v2 carry-over that fully closed sessions
    # — bypassing the IDLE_SLEEPING state — so broker auto-wake on
    # inbound platform messages had no resume handle to grab onto
    # (Telegram, Discord, etc. hit the "not running" path; web chat
    # masked the bug by cold-starting via its own /chat endpoint).
    # The watchdog's idle-sleep at the agent's idle threshold is now
    # the single sleep path; it transitions to IDLE_SLEEPING with the
    # resume handle preserved, so the broker can warm-wake from any
    # platform's inbound message via `_route_streaming`.

    @app.post("/agents/{agent_name}/stop")
    async def stop_agent(agent_name: str):
        """Force-stop an agent — immediately disconnect all sessions.

        Unlike sleep, this does NOT check for a recent save_my_context
        checkpoint. Use this as an emergency stop.
        """
        agent = agents.get(agent_name)
        if not agent:
            raise HTTPException(404, f"Agent '{agent_name}' not found")

        streaming_closed = await _disconnect_streaming_sessions(agent_name)

        closed = 0
        for s in list(manager.list()):
            if s.agent_name == agent_name:
                manager.delete(s.id)
                closed += 1

        total_closed = streaming_closed + closed
        _log(f"api: agent {agent_name} force-stopped, closed {total_closed} session(s)")
        activity.log(agent_name, "agent_stop", f"{agent_name} force-stopped")
        return {
            "agent": agent_name,
            "status": "stopped",
            "sessions_closed": total_closed,
        }

    @app.post("/agents/stop-all")
    async def stop_all_agents():
        """Force-stop ALL agents — emergency kill switch."""
        results = {}
        for a in agents.list():
            agent_name = a.name
            streaming_closed = await _disconnect_streaming_sessions(agent_name)
            closed = 0
            for s in list(manager.list()):
                if s.agent_name == agent_name:
                    manager.delete(s.id)
                    closed += 1
            total = streaming_closed + closed
            if total > 0:
                results[agent_name] = total
                _log(f"api: agent {agent_name} force-stopped, closed {total} session(s)")
                activity.log(agent_name, "agent_stop", f"{agent_name} force-stopped (stop-all)")
        return {"stopped": results, "total_agents": len(results)}

    # ── Wake Trigger ───────────────────────────────────────

    @app.post("/agents/{agent_name}/forward")
    async def forward_to_agent(agent_name: str, req: dict):
        """Forward a message to an agent's streaming session."""
        agent = agents.get(agent_name)
        if not agent:
            raise HTTPException(404, f"Agent '{agent_name}' not found")
        content = req.get("content", "")
        if not content:
            raise HTTPException(400, "content is required")

        ss = await _ensure_streaming_session(agent_name, label="main")
        if not ss:
            raise HTTPException(503, f"No streaming session for '{agent_name}'")
        await ss.send(content)
        activity.log(agent_name, "message_forwarded",
                     f"Message forwarded to {agent_name}")
        return {"sent": True, "agent": agent_name}

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
            "connected": ss.state == TransportSessionState.CONNECTED,
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
        # ``agent_wake`` is logged centrally via ``on_wake_delivered``
        # when the session's connect-time wake prompt lands. Logging
        # here would double-count for scheduled wakes that triggered
        # a cold-start (the connect already fired the callback) and
        # would mismark scheduled re-prompts (which aren't really new
        # wake events — the session was already awake). See Murzik P1#2.

    async def _dream_callback(agent_name: str, agent_config) -> None:
        """Callback for the scheduler to run nightly dream consolidation."""
        _log(f"scheduler: triggering dream for '{agent_name}'")
        try:
            await dream_runner.run_dream(agent_name, agent_config)

            # Post-dream: snapshot people profiles into KB (dreams extract new profile data)
            try:
                _kb_ingest_people_profiles()
            except Exception as pe:
                _log(f"scheduler: post-dream KB people ingest failed: {pe}")

        except Exception as e:
            _log(f"scheduler: dream run failed for '{agent_name}': {e}")
            # Notify main agent of dream failure
            main_name = agents.get_main_agent()
            if main_name and main_name != agent_name:
                main_ss = broker._streaming.get(main_name, {}).get("main")
                if main_ss and main_ss.state == TransportSessionState.CONNECTED:
                    try:
                        await main_ss.send(
                            f"[SYSTEM ALERT] Dream run failed for agent '{agent_name}': {e}"
                        )
                    except Exception:
                        pass  # Don't let notification failure mask the original error

    async def _librarian_callback(agent_name: str, agent_config) -> None:
        """Callback for the scheduler to run KB librarian curation."""
        if not librarian_runner.should_run(agent_name):
            _log(f"scheduler: librarian skipped for '{agent_name}' — no new sources or cooldown")
            return
        _log(f"scheduler: triggering librarian for '{agent_name}'")
        try:
            stats = await librarian_runner.run(agent_name, agent_config)
            _log(f"scheduler: librarian done for '{agent_name}' — "
                 f"sources={stats.get('sources_processed', 0)}")
        except Exception as e:
            _log(f"scheduler: librarian run failed for '{agent_name}': {e}")

    async def _heartbeat_resurrect(agent_name: str, _session_id: str) -> None:
        """Watchdog resurrection: reconnect a dead streaming session.

        Invoked by AgentScheduler._maybe_resurrect when an agent's heartbeat is
        currently marked dead. The save_my_context guard used by the public
        /streaming/restart endpoint is bypassed here — but only safely because
        of the `state == CONNECTED` check below: if the underlying transport is still
        live the callback bails out immediately and the public guarded path
        remains the only way to restart. We only ever drive a reconnect when the
        client is already disconnected, at which point there is no live session
        whose work could be lost.

        Known limitation: this does NOT cover the "transport-alive-but-agent-
        silent" case (reader loop wedged on a stalled LLM call) — see the
        follow-up issue noted on #338.
        """
        ss = broker._get_streaming_session(agent_name)
        if not ss:
            _log(
                f"api: resurrection skipped for {agent_name} — no streaming "
                f"session registered"
            )
            return
        if getattr(ss, "state", None) == TransportSessionState.CONNECTED:
            # Race: the in-process retry recovered between heartbeat tick and
            # the callback running. Also the load-bearing guard for bypassing
            # the save_my_context restart guard — see docstring above.
            return
        if getattr(ss, "state", None) == TransportSessionState.IDLE_SLEEPING:
            # Session was deliberately disconnected by idle_sleep(). Resurrecting
            # it here would fight the idle-sleep state and cause an immediate
            # reconnect/sleep churn cycle. The next genuine wake (scheduler or
            # incoming message) will reconnect via connect(). See #348.
            _log(
                f"api: resurrection skipped for {agent_name} — session is "
                f"idle-sleeping (will wake on next scheduled/incoming event)"
            )
            return
        _log(f"api: watchdog resurrection — reconnecting {agent_name}")
        try:
            # TODO(#338-followup): wrap in asyncio.create_task so the scheduler
            # tick isn't blocked for up to ~40s during the internal backoff.
            attempt_reconnect = getattr(ss, "attempt_reconnect", None)
            if callable(attempt_reconnect):
                await attempt_reconnect()
            else:
                # Runtime-tolerant fallback for future StreamingSession-compatible
                # implementations that have connect/disconnect but not the richer
                # watchdog reconnect helper yet.
                try:
                    disconnect = getattr(ss, "disconnect", None)
                    if callable(disconnect):
                        await disconnect()
                except Exception as e:
                    _log(f"api: resurrection pre-disconnect failed for {agent_name}: {e}")
                connect = getattr(ss, "connect", None)
                if not callable(connect):
                    raise AttributeError(
                        f"{ss.__class__.__name__} has no attempt_reconnect or connect"
                    )
                await connect()
            if getattr(ss, "state", None) == TransportSessionState.CONNECTED:
                activity.log(
                    agent_name, "watchdog_resurrect",
                    f"{agent_name} restored by heartbeat watchdog",
                )
        except Exception as e:
            _log(f"api: resurrection failed for {agent_name}: {e}")

    def _is_resurrectable(agent_name: str) -> bool:
        """Scheduler precondition: True iff resurrection should be attempted.

        Returns False for agents that don't currently want resurrection — most
        commonly idle-sleeping sessions (which the API callback would refuse
        anyway, but at the cost of a budget slot and a noisy log line every
        scheduler tick). See #348/#349 for the original API-layer skip.
        """
        ss = broker._get_streaming_session(agent_name)
        if not ss:
            return False  # nothing to resurrect
        if getattr(ss, "state", None) == TransportSessionState.IDLE_SLEEPING:
            return False  # deliberately disconnected — leave it alone
        return True

    scheduler = AgentScheduler(
        agents,
        wake_callback=_wake_callback,
        heartbeat_callback=_heartbeat_resurrect,
        is_resurrectable_fn=_is_resurrectable,
        direct_send_callback=broker.send_callback,
        dream_callback=_dream_callback,
        librarian_callback=_librarian_callback,
        streaming_sessions_fn=lambda: broker._streaming,
        comms_cleanup_fn=comms.cleanup_expired,
        trigger_store=trigger_store,
        activity=activity,
    )

    # Autonomy engine — self-directed work loops
    autonomy = AutonomyEngine(
        agents, tasks, store,
        session_sender=_wake_callback,
    )

    # ── Session Watchdog ───────────────────────────────────────
    def _get_watchdog_config(agent_name: str) -> WatchdogConfig:
        """Merge per-agent watchdog_config onto global defaults."""
        agent = agents.get(agent_name)
        if not agent:
            return WatchdogConfig()
        # Single-source merge (incl. the #109 transition-* thresholds) so new
        # fields can't be silently dropped here.
        return WatchdogConfig.from_raw(agent.watchdog_config)

    async def _watchdog_recover(agent_name: str, label: str, reason: str) -> None:
        """Recovery callback: stop + reconnect a single stuck streaming session."""
        _log(f"watchdog: recovering {agent_name}/{label} — {reason}")
        try:
            activity.log(
                agent_name=agent_name,
                event_type="watchdog_recover",
                title=f"Watchdog auto-recovery ({label}): {reason}",
            )
            # Disconnect only the stuck session, not siblings
            sessions = broker._streaming.get(agent_name, {})
            ss = sessions.get(label)
            if ss:
                try:
                    await ss.disconnect()
                except Exception:
                    pass
                agents.set_streaming_session_id(agent_name, "", label=label)
                broker.unregister_streaming(agent_name, label=label)
            await asyncio.sleep(2)
            await _ensure_streaming_session(agent_name, label=label)
            _log(f"watchdog: {agent_name}/{label} recovered successfully")
        except Exception as exc:
            _log(f"watchdog: recovery failed for {agent_name}/{label}: {exc}")

    async def _watchdog_alert(agent_name: str, message: str) -> None:
        """Alert callback: notify the owner via the broker."""
        _log(f"watchdog: alert for {agent_name} — {message}")
        try:
            activity.log(
                agent_name=agent_name,
                event_type="watchdog_warn",
                title=message[:200],
            )
            # Send to owner's primary chat if available
            agent = agents.get(agent_name)
            if agent:
                owner = agents.get_owner_profile()
                owner_chat = owner.get("chat_id", "") if owner else ""
                if owner_chat:
                    await broker.send_callback(
                        agent_name=agent_name,
                        platform="telegram",
                        chat_id=str(owner_chat),
                        text=message,
                    )
        except Exception as exc:
            _log(f"watchdog: alert delivery failed for {agent_name}: {exc}")

    def _watchdog_mcp_bind_status(agent_name: str) -> dict:
        """Bind-status for the watchdog's #663 MCP-recover check.

        ``checkable`` means a missing current-epoch MCP success is a real wedge
        signal (not just a quiet / heartbeat-disabled agent) AND a gateway
        generation is established. It is true when the agent heartbeats on a
        cadence (``heartbeat_interval > 0``) OR has emitted a recent AGENT-ORIGIN
        heartbeat — the latter covers wake-driven sidekicks (e.g. barsik) that
        carry ``heartbeat_interval == 0`` but heartbeat in practice (#663 Murzik
        Finding #2 / R2). ``bound`` is true when a successful MCP round-trip has
        been recorded for the CURRENT gateway epoch (the heartbeat bind signal).
        ``probe_request`` carries the #663 phase-2 active-probe state (whether the
        agent answered the launch-time mcp_probe directive) — used by the watchdog
        only to CORROBORATE/enrich an already-decided passive recovery, never to
        trigger one on its own.
        """
        agent = agents.get(agent_name)
        hb = int(getattr(agent, "heartbeat_interval", 0) or 0) if agent else 0
        epoch = get_gateway_epoch()
        st = get_probe_status(agent_name)
        # Agent-origin heartbeat recency — NOT get_latest_heartbeat, so synthetic
        # server_presence rows can't make a wedged agent look checkable-and-bound.
        latest_hb_ts: float | None = None
        if agent:
            latest_hb = agents.get_latest_agent_heartbeat(agent_name)
            if latest_hb and latest_hb.timestamp:
                latest_hb_ts = float(latest_hb.timestamp)
        return {
            "checkable": compute_mcp_checkable(
                agent_exists=bool(agent),
                epoch=epoch,
                heartbeat_interval=hb,
                latest_agent_heartbeat_ts=latest_hb_ts,
                now=time.time(),
            ),
            "bound": bool(st.get("bound")),
            "heartbeat_interval": hb,
            "probe_request": get_probe_request(agent_name),
        }

    async def _watchdog_mcp_recover(agent_name: str, label: str, reason: str) -> None:
        """Force-fresh recover an MCP-unbound session (#663).

        Mirrors the validated force-restart core (``admin_force_restart_agent``):
        a plain reconnect would re-``--continue`` the cwd transcript and inherit
        the dead MCP transport, so we MUST set ``force_fresh_context_once`` to
        relaunch on a clean transcript where CC cold-binds its MCP client.
        No anti-abuse heartbeat gate here — a wedged-MCP agent's heartbeat is
        stale *by construction* (that IS the signal), and the watchdog has
        already applied its own deadline + rate limits.
        """
        sessions = broker._streaming.get(agent_name, {})
        ss = sessions.get(label)
        if not ss:
            _log(f"watchdog: no session for {agent_name}/{label} to MCP-recover")
            return
        audit_meta = {
            "label": label,
            "reason": reason,
            "source": "watchdog_mcp_recover",
        }
        activity.log(
            agent_name=agent_name,
            event_type="mcp_epoch_unbound",
            title=f"Watchdog MCP-recover ({label}): {reason}",
            metadata=audit_meta,
        )
        try:
            await ss.disconnect()
        except Exception:
            pass
        agents.set_streaming_session_id(agent_name, "", label=label)
        # The agent couldn't refresh save_my_context (its MCP was dead) — bump
        # so any context-staleness gate is satisfied; preserve saved state.
        try:
            agents.bump_context_updated_at(agent_name)
        except Exception:
            pass
        ss._config.wake_context = _build_streaming_wake_context(agent_name, commit=False)
        ss._config.resume_handle = ""
        ss._config.restart_reason = "mcp_epoch_unbound"
        ss._config.force_fresh_context_once = True
        ss.resume_handle = ""
        if hasattr(ss, "codex_session_id"):
            ss.codex_session_id = ""
        try:
            await ss.connect()
            session_event_store.log(
                session_id=ss.id,
                agent_name=agent_name,
                event_type="force_restart",
                metadata=audit_meta,
            )
            _log(f"watchdog: MCP-recovered {agent_name}/{label} (force-fresh)")
        except Exception as exc:
            broker.unregister_streaming(agent_name, label=label)
            _log(f"watchdog: MCP recovery connect failed for {agent_name}/{label}: {exc}")
            raise

    watchdog = SessionWatchdog(
        streaming_sessions_fn=lambda: broker._streaming,
        recover_fn=_watchdog_recover,
        alert_fn=_watchdog_alert,
        agent_config_fn=_get_watchdog_config,
        mcp_bind_status_fn=_watchdog_mcp_bind_status,
        mcp_recover_fn=_watchdog_mcp_recover,
    )

    # Shared MCP server (started in on_startup if enabled)
    shared_mcp_manager: SharedMcpManager | None = None

    @app.on_event("startup")
    async def on_startup():
        """Start broker pollers, streaming sessions, scheduler, and autonomy."""
        nonlocal shared_mcp_manager

        # Lock down SQLite file permissions to owner-only. Runs here (not in
        # create_api) so every store's __init__ has already created its DB
        # file; the sweep is idempotent and best-effort.
        try:
            from pinky_daemon.db_security import sweep_db_permissions
            sweep_db_permissions(Path(db_path).resolve().parent)
        except Exception as exc:  # never let hardening abort startup
            _log(f"startup: db permission sweep skipped ({exc})")

        # Rotate the daemon console log (logs/api.log): daily + size-capped,
        # gzipped 0600 archives with Telegram tokens redacted, pruned past the
        # retention window. copytruncate keeps launchd's open fd valid (it does
        # not reopen StandardOutPath after a move). Best-effort background task,
        # behind a kill-switch (PINKY_LOG_ROTATION=off + restart to disable).
        if os.environ.get("PINKY_LOG_ROTATION", "on").strip().lower() not in ("off", "0", "false"):
            try:
                from pinky_daemon.log_rotation import run_rotation_loop
                _api_log = Path("logs") / "api.log"
                if _api_log.exists():
                    # Keep a strong ref on app.state so the task isn't GC'd
                    # mid-flight (asyncio.create_task only holds a weak ref).
                    app.state.log_rotation_task = asyncio.create_task(
                        run_rotation_loop(_api_log)
                    )
            except Exception as exc:  # never let log rotation abort startup
                _log(f"startup: log rotation not started ({exc})")

        # Start shared MCP server BEFORE agent sessions so SSE URLs are ready
        if SHARED_MCP_ENABLED:
            def _resolve_memory_db(agent_name: str) -> str:
                """Resolve agent_name -> memory DB path for the shared store pool."""
                agent = agents.get(agent_name)
                if not agent:
                    raise ValueError(f"Unknown agent: {agent_name}")
                db_path = str(Path(agent.working_dir) / "data" / "memory.db")
                # Ensure data dir exists (agent may not have used memory yet)
                Path(db_path).parent.mkdir(parents=True, exist_ok=True)
                return db_path

            def _is_cross_agent_memory_authorized(caller_name: str) -> bool:
                """May ``caller_name`` access other agents' memory? (#145)

                Authorized iff the caller is a registered agent holding the
                ``dreamer`` role — the Dreamer (Mora) that consolidates each
                agent's history into that agent's own memory. Role-only (see
                ``_agent_is_dreamer``). Default-deny: unknown agents and any
                lookup failure → False.
                """
                try:
                    return _agent_is_dreamer(agents.get(caller_name))
                except Exception:
                    return False

            def _resolve_signing_key(agent_name: str) -> str | None:
                """Resolve agent_name -> per-agent signing key (#623).

                Lets the shared self/messaging MCP servers sign each request
                with the resolved agent's own key instead of the global secret.
                Guarded: unknown agent / lookup failure -> None -> the signer
                falls back to the global secret (dual-accept still works).
                """
                try:
                    return agents.get_signing_key(agent_name)
                except Exception:
                    return None

            # Resolve the OpenAI key for the shared pinky-memory embedder.
            # It lives in system_settings (where TTS/broker read it via
            # get_setting), NOT the daemon process env — so the embedder, which
            # only consulted os.environ, silently ran NoOp and disabled semantic
            # recall fleet-wide until 2026-06-05. Prefer settings, fall back to
            # env. Empty -> NoOp embedder (logged at WARNING).
            try:
                _memory_openai_key = agents.get_setting("OPENAI_API_KEY") or os.environ.get(
                    "OPENAI_API_KEY", ""
                )
            except Exception:
                _memory_openai_key = os.environ.get("OPENAI_API_KEY", "")

            shared_mcp_manager = SharedMcpManager(
                api_url="http://localhost:8888",
                memory_db_resolver=_resolve_memory_db,
                cross_agent_authorizer=_is_cross_agent_memory_authorized,
                signing_key_resolver=_resolve_signing_key,
                openai_api_key=_memory_openai_key,
            )
            await shared_mcp_manager.start()
            _log(f"startup: shared MCP server started on {shared_mcp_manager.url}")

        # Boot policy (2026-05-11): only the **main agent** auto-resumes at boot.
        # Sibling agents stay dormant until something triggers them — an inbound
        # message, an agent-to-agent message, or a scheduled wake firing. Their
        # autonomy loop + streaming session are created lazily on first dispatch
        # (see `_ensure_streaming_session` and `autonomy.push_event`).
        #
        # `auto_start_agents` is no longer used to gate boot startup. The
        # `agent.auto_start` flag is preserved for compat (`/admin/auto-start`
        # endpoint still reports it) but does not control which agents come up.
        main_name_for_boot = agents.get_main_agent()

        # Start broker pollers and streaming sessions for all enabled agents.
        from pinky_daemon.pollers import (
            BrokerDiscordPoller,
            BrokeriMessagePoller,
            BrokerTelegramPoller,
        )
        from pinky_outreach.discord import DiscordAdapter
        from pinky_outreach.telegram import TelegramAdapter
        all_agents = agents.list(enabled_only=True)
        streaming_count = 0
        for agent in all_agents:
            # Regenerate .mcp.json on every startup so paths match the current machine.
            # Critical for migration scenarios (e.g. Mac Mini → RPi) where old paths linger.
            work_dir = Path(agent.working_dir).resolve() if agent.working_dir else None
            if work_dir and work_dir.is_dir():
                try:
                    _write_mcp_json(work_dir, agent.name, agent_registry=agents, skill_store=skills)
                except Exception as e:
                    _log(f"startup: failed to regenerate .mcp.json for {agent.name}: {e}")

            token = agents.get_raw_token(agent.name, "telegram")
            if token:
                # Skip if a poller already exists for this agent
                existing = any(
                    isinstance(p, BrokerTelegramPoller) and p._agent_name == agent.name
                    for p in _broker_pollers
                )
                if existing:
                    _log(f"startup: telegram poller already exists for {agent.name}, skipping")
                    continue
                adapter = TelegramAdapter(token, timeout=45.0)  # > poll_timeout (30s) to avoid racing
                poller = BrokerTelegramPoller(
                    adapter, agent.name, broker, registry=agents,
                )
                _broker_pollers.append(poller)
                asyncio.create_task(poller.start())
                _log(f"startup: broker poller started for {agent.name}")

            # Discord poller — REST polling (Gateway/WebSocket is a future v0.2)
            discord_token = agents.get_raw_token(agent.name, "discord")
            if discord_token:
                existing = any(
                    isinstance(p, BrokerDiscordPoller) and p._agent_name == agent.name
                    for p in _broker_pollers
                )
                if existing:
                    _log(f"startup: discord poller already exists for {agent.name}, skipping")
                else:
                    try:
                        # Read per-token settings (poll_interval_sec, watched_channels)
                        settings = {}
                        for tok in agents.list_tokens(agent.name):
                            if tok.platform == "discord":
                                settings = tok.settings or {}
                                break
                        poll_interval = float(settings.get("poll_interval_sec", 1.0))
                        watched = settings.get("watched_channels") or None

                        d_adapter = DiscordAdapter(discord_token)
                        d_poller = BrokerDiscordPoller(
                            d_adapter, agent.name, broker, registry=agents,
                            poll_interval=poll_interval,
                            watched_channels=watched,
                        )
                        _broker_pollers.append(d_poller)
                        asyncio.create_task(d_poller.start())
                        _log(
                            f"startup: discord poller started for {agent.name} "
                            f"(interval={poll_interval}s, "
                            f"channels={'auto' if not watched else len(watched)})"
                        )
                    except Exception as e:
                        _log(f"startup: discord poller failed for {agent.name}: {e}")

            # iMessage poller — check if agent has imessage enabled in DB
            if agents.get_raw_token(agent.name, "imessage"):
                try:
                    from pinky_outreach.imessage import iMessageAdapter
                    im_adapter = iMessageAdapter()
                    im_poller = BrokeriMessagePoller(
                        im_adapter, agent.name, broker,
                    )
                    _broker_pollers.append(im_poller)
                    asyncio.create_task(im_poller.start())
                    _log(f"startup: iMessage poller started for {agent.name}")
                except Exception as e:
                    _log(f"startup: iMessage poller failed for {agent.name}: {e}")

            # Boot policy: only the main agent auto-resumes its streaming session.
            # Sibling sessions are created on-demand via `_ensure_streaming_session`
            # when an inbound message / agent-to-agent message / scheduled wake
            # routes through the autonomy event queue. Persisted session IDs are
            # kept in the DB so resume-by-id still works when siblings later wake.
            if agent.name != main_name_for_boot:
                _log(f"startup: skipping streaming session for {agent.name} (on-demand only)")
                continue

            # Validate working_dir exists — stale paths from a different machine
            # (e.g. migrating from Mac Mini to RPi) would cause Fatal errors.
            if work_dir and not work_dir.is_dir():
                _log(f"startup: working_dir missing for {agent.name}: {work_dir} — skipping session")
                # Clear stale session IDs so we don't retry on next boot
                for entry in agents.list_streaming_session_ids(agent.name):
                    if entry["session_id"]:
                        agents.set_streaming_session_id(agent.name, "", label=entry["label"])
                continue

            main_resume = agents.get_streaming_session_id(agent.name, label="main")
            try:
                await _start_streaming_session(agent.name, label="main", resume_id=main_resume)
                streaming_count += 1
                if main_resume:
                    _log(f"startup: streaming session resumed for {agent.name}/main (session {main_resume[:12]})")
                else:
                    _log(f"startup: streaming session connected for {agent.name}/main (new)")
            except Exception as e:
                _log(f"startup: streaming session failed for {agent.name}/main: {e}")
                # If resume failed, clear the stale session ID and try fresh on next boot
                if main_resume:
                    _log(f"startup: clearing stale session ID for {agent.name}/main")
                    agents.set_streaming_session_id(agent.name, "", label="main")

        # Clean up restored legacy SDK sessions: those superseded by a live
        # streaming session, plus unowned ghosts (blank agent_name) that would
        # otherwise reload every boot and pollute orphan diagnostics with a
        # stale id (#667). See ``is_purgeable_legacy_session``.
        streaming_agents = set(broker._streaming.keys())
        legacy_purged = 0
        ghost_purged = 0
        for s in manager.list():
            if is_purgeable_legacy_session(s.agent_name, streaming_agents):
                if not s.agent_name:
                    ghost_purged += 1
                manager.delete(s.id)
                legacy_purged += 1
        if legacy_purged:
            _log(
                f"startup: purged {legacy_purged} legacy session(s) "
                f"({ghost_purged} unowned ghost(s)) (#667)"
            )

        await scheduler.start()
        await autonomy.start()
        await watchdog.start()

        # Boot policy: only the main agent's autonomy loop starts at boot.
        # Sibling loops start lazily via `autonomy.push_event` when an event
        # arrives for them (inbound message, agent-to-agent message, scheduled
        # wake). See `autonomy.push_event` — it ungates the wake on `enabled`
        # rather than `auto_start` so any enabled agent can be woken on demand.
        main_started = False
        main_name = agents.get_main_agent()
        if main_name:
            main_agent = agents.get(main_name)
            if main_agent and main_agent.enabled:
                await autonomy.start_agent_loop(main_name)
                main_started = True
                _log(f"startup: main agent '{main_name}' autonomy loop started")
            else:
                _log(f"startup: warn — main agent '{main_name}' missing or disabled")
        else:
            _log("startup: warn — no main agent configured; no autonomy loop started")

        _log(
            f"startup: scheduler + autonomy + watchdog running, "
            f"main={'on' if main_started else 'off'}, "
            f"{len(_broker_pollers)} broker poller(s), {streaming_count} streaming"
        )

    @app.on_event("shutdown")
    async def on_shutdown():
        """Stop scheduler, autonomy, broker pollers, and streaming sessions on shutdown."""
        import json as _json
        from datetime import datetime, timezone

        # Write restart manifest before disconnecting — captures each agent's in-progress state
        # so it can be injected into the wake prompt on next startup.
        manifest_path = Path(db_path).parent / "restart_manifest.json"
        manifest: dict = {
            "restart_time": datetime.now(timezone.utc).isoformat(),
            "planned": True,
            "agents": {},
        }
        for name in list(broker._streaming.keys()):
            sessions = broker._streaming.get(name, {})
            for label, ss in list(sessions.items()):
                try:
                    s = ss.stats
                    manifest["agents"][name] = {
                        "label": label,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "in_progress": s.get("current_activity", ""),
                        "activity_log": s.get("activity_log", []),
                        "pending_responses": s.get("pending_responses", 0),
                    }
                except Exception:
                    pass
        try:
            manifest_path.write_text(_json.dumps(manifest, indent=2))
            _log(f"shutdown: restart manifest written for {len(manifest['agents'])} agent(s)")
        except Exception as e:
            _log(f"shutdown: failed to write restart manifest: {e}")

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
        await watchdog.stop()
        # Stop shared MCP server
        if shared_mcp_manager and shared_mcp_manager.is_running:
            await shared_mcp_manager.stop()
            _log("shutdown: shared MCP server stopped")

    # ── Admin: Session Watchdog ─────────────────────────

    @app.get("/admin/watchdog")
    async def get_watchdog_status():
        """Return session watchdog status, tracked agents, and Claude auth health.

        Three top-level keys for external monitors:
            running / check_interval / agents — session-stuck watchdog state.
            auth_status — host-wide Claude auth health (see auth_alerts).

        ``auth_status.status`` is one of ``ok | degraded | broken``; tying a
        Prometheus alert to ``broken`` catches credential outages quickly.
        """
        out = watchdog.status()
        try:
            out["auth_status"] = auth_tracker.status()
        except Exception as e:
            _log(f"admin/watchdog: auth_tracker.status raised: {e}")
            out["auth_status"] = {"status": "unknown", "error": str(e)}
        # #104: per-class non-auth alert trackers (billing_error, rate_limit).
        transport_status: dict[str, dict] = {}
        for et, tracker in transport_failure_trackers.items():
            try:
                transport_status[et] = tracker.status()
            except Exception as e:
                _log(f"admin/watchdog: {et} tracker.status raised: {e}")
                transport_status[et] = {"status": "unknown", "error": str(e)}
        out["transport_alert_status"] = transport_status
        return out

    # ── Admin: Shared MCP Status ─────────────────────────

    @app.get("/admin/shared-mcp")
    async def get_shared_mcp_status():
        """Return shared MCP server status."""
        return {
            "enabled": SHARED_MCP_ENABLED,
            "running": shared_mcp_manager.is_running if shared_mcp_manager else False,
            "url": shared_mcp_manager.url if shared_mcp_manager else None,
            "servers": ["self", "messaging"] if shared_mcp_manager else [],
        }

    # ── Admin: Release Channel ────────────────────────────
    #
    # Trunk-based since 26.05.070 (#450). The only channel is "stable" —
    # production hosts pull the latest GitHub Release tag from main.
    # PINKYBOT_CHANNEL is retained as an env var for back-compat, but the
    # only accepted value is "stable"; anything else is logged and coerced.

    @app.get("/admin/channel")
    async def get_release_channel():
        """Return the current release channel.

        Always reports "stable" / "main" — there is no other channel.
        Legacy PINKYBOT_CHANNEL=beta is silently coerced to stable.
        """
        raw = os.environ.get("PINKYBOT_CHANNEL", "stable")
        if raw != "stable":
            _log(
                f"admin: PINKYBOT_CHANNEL={raw!r} is deprecated; "
                "trunk-based since #450 — treating as 'stable'"
            )
        return {"channel": "stable", "branch": "main"}

    @app.post("/admin/channel")
    async def set_release_channel(channel: str = "stable"):
        """Set the release channel — only "stable" is accepted.

        Trunk-based since #450: the beta channel was removed. This endpoint
        is preserved so legacy clients/UIs don't 404, but anything other
        than "stable" returns 400.

        Persists to .env file and updates the running env var.
        Does NOT restart — call /admin/update after to refresh.
        """
        if channel != "stable":
            raise HTTPException(
                400,
                "Only 'stable' is supported. The beta channel was removed in "
                "the trunk-based migration (#450).",
            )

        os.environ["PINKYBOT_CHANNEL"] = channel

        # Persist to .env file
        env_path = Path(__file__).resolve().parent.parent.parent / ".env"
        lines: list[str] = []
        found = False
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                if line.strip().startswith("PINKYBOT_CHANNEL="):
                    lines.append(f"PINKYBOT_CHANNEL={channel}")
                    found = True
                else:
                    lines.append(line)
        if not found:
            lines.append(f"PINKYBOT_CHANNEL={channel}")
        env_path.write_text("\n".join(lines) + "\n")

        _log(f"admin: release channel set to {channel} (branch=main)")
        return {"channel": "stable", "branch": "main"}

    # ── Admin: Update & Restart ───────────────────────────

    @app.get("/admin/update/status")
    async def admin_update_status():
        """Return update-adjacent status, including frontend build freshness."""
        frontend_status = _frontend_build_status(str(_pkg_root), current_git_hash=_git_hash)
        app.state.frontend_build_status = frontend_status
        return {
            "git_hash": _git_hash,
            "git_branch": _git_branch,
            "frontend": frontend_status,
        }

    @app.post("/admin/update")
    async def admin_update(
        branch: str = "",
        target: str = "",
        dry_run: bool = False,
        force: bool = False,
        force_deps: bool = False,
    ):
        """Update to a verified release (or a pinned ref) and restart.

        Deploy target (``target`` arg):
        - empty → the latest *published* GitHub Release tag (default).
        - a release tag (e.g. "26.06.109") → that release.
        - a commit SHA → an explicit operator-pinned deploy.

        A release-tag target is verified against the GitHub Releases API
        over TLS before checkout: it must be a published (non-draft,
        non-prerelease) Release whose commit matches the local tag. On
        failure the daemon refuses and stays on the current version. The
        daemon then checks out that exact ref (detached HEAD) rather than
        pulling branch HEAD, so production runs exactly the verified code.

        The process manager (launchctl/systemd) must be installed for
        auto-restart. Without it, the daemon will stop and stay stopped.

        Trunk-based since #450; only branch="main" (or empty) is accepted.
        branch="beta" returns 400 — the beta channel is gone.

        force=True: (a) bypass release verification — the operator override
        for e.g. an unreachable GitHub API; and (b) discard local mods to
        TRACKED files before checkout (untracked files are preserved). A
        commit-SHA target is always an operator pin (existence-checked only).

        force_deps=True: reinstall dependencies even when HEAD didn't change.
        """
        import shutil
        import subprocess as sp
        import sys

        if branch and branch != "main":
            raise HTTPException(
                400,
                f"Only branch='main' is supported (got {branch!r}). The beta "
                "channel was removed in the trunk-based migration (#450).",
            )

        # Legacy env back-compat: PINKYBOT_CHANNEL=beta → log + treat as stable.
        raw_channel = os.environ.get("PINKYBOT_CHANNEL", "stable")
        if raw_channel != "stable":
            _log(
                f"admin: PINKYBOT_CHANNEL={raw_channel!r} is deprecated; "
                "trunk-based since #450 — treating as 'stable'"
            )

        branch = "main"
        repo_dir = str(Path(__file__).resolve().parent.parent.parent)

        # Current state
        try:
            before_hash = sp.check_output(
                ["git", "rev-parse", "--short", "HEAD"],
                cwd=repo_dir, stderr=sp.DEVNULL, timeout=10,
            ).decode().strip()
        except Exception:
            before_hash = "unknown"

        # Current tag (if any)
        try:
            current_tag = sp.check_output(
                ["git", "describe", "--tags", "--exact-match", "HEAD"],
                cwd=repo_dir, stderr=sp.DEVNULL, timeout=10,
            ).decode().strip()
        except Exception:
            current_tag = None

        # Fetch tags + branch
        try:
            sp.check_output(
                ["git", "fetch", "origin", "--tags", branch],
                cwd=repo_dir, stderr=sp.STDOUT, timeout=30,
            )
        except sp.CalledProcessError as e:
            return {"error": f"git fetch failed: {e.output.decode()[:500]}"}

        # Unshallow if needed (install.sh uses --depth 1) so tag/commit
        # resolution and verification see full history.
        try:
            is_shallow = sp.check_output(
                ["git", "rev-parse", "--is-shallow-repository"],
                cwd=repo_dir, stderr=sp.DEVNULL, timeout=10,
            ).decode().strip()
            if is_shallow == "true":
                sp.check_output(
                    ["git", "fetch", "--unshallow", "origin"],
                    cwd=repo_dir, stderr=sp.STDOUT, timeout=60,
                )
        except Exception:
            pass  # Non-fatal — shallow log may still miss commits

        # Resolve + verify the deploy target. Empty target = latest published
        # release; a tag = that release (verified against the GitHub Releases
        # API over TLS unless force); a commit SHA = an explicit operator pin.
        from pinky_daemon import self_update
        decision = self_update.resolve_and_verify(
            repo_dir, target, force=force, log=_log,
        )
        target_tag = self_update.latest_release_tag(repo_dir)

        # Preview mode — report the planned deploy without mutating anything.
        if dry_run:
            try:
                pending = sp.check_output(
                    ["git", "log", "--oneline", f"HEAD..origin/{branch}"],
                    cwd=repo_dir, stderr=sp.DEVNULL, timeout=10,
                ).decode().strip()
            except Exception:
                pending = ""
            commits = [line for line in pending.splitlines() if line.strip()] if pending else []
            result = {
                "dry_run": True,
                "current_hash": before_hash,
                "current_release": current_tag,
                "branch": branch,
                "pending_commits": len(commits),
                "commits": commits,
                "up_to_date": len(commits) == 0,
                "deploy_ref": decision.ref or None,
                "deploy_kind": decision.kind or None,
                "verified": decision.verified,
            }
            if target_tag:
                result["latest_release"] = target_tag
            if decision.error:
                result["verify_error"] = decision.error
            return result

        # Refuse before touching the working tree if verification failed.
        if decision.error:
            return {
                "error": decision.error,
                "current_release": current_tag,
                "staying_on_version": before_hash,
            }

        # Force mode: discard local mods to TRACKED files before checkout.
        # Untracked files (e.g. .env, local notes) are NOT touched.
        forced_reset = False
        forced_files: list[str] = []
        if force:
            try:
                dirty = sp.check_output(
                    ["git", "diff", "--name-only", "HEAD"],
                    cwd=repo_dir, stderr=sp.DEVNULL, timeout=10,
                ).decode().strip()
                if dirty:
                    forced_files = [f for f in dirty.splitlines() if f.strip()]
                    sp.check_output(
                        ["git", "checkout", "--", "."],
                        cwd=repo_dir, stderr=sp.STDOUT, timeout=30,
                    )
                    forced_reset = True
                    _log(f"admin: force=True reset {len(forced_files)} tracked file(s): {forced_files[:10]}")
            except sp.CalledProcessError as e:
                return {"error": f"force reset failed: {e.output.decode()[:500]}"}
            except Exception as e:
                _log(f"admin: force reset warning: {e}")

        # Check out the resolved + verified ref (detached HEAD at the tag, or
        # the pinned commit). Replaces `git pull origin main` so production
        # runs exactly the verified ref, not whatever is at branch HEAD.
        try:
            sp.check_output(
                ["git", "checkout", decision.ref],
                cwd=repo_dir, stderr=sp.STDOUT, timeout=60,
            )
            _log(f"admin: checked out {decision.kind} {decision.ref} (verified={decision.verified})")
        except sp.CalledProcessError as e:
            return {"error": f"git checkout {decision.ref} failed: {e.output.decode()[:500]}"}

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

        # Detect dependency changes — rebuild whenever pyproject.toml or uv.lock
        # changed in the pull, when force_deps=True is passed, or when the
        # installed package versions have drifted from the pyproject pins
        # (e.g., pyproject was bumped on an earlier pull that skipped reinstall).
        deps_rebuilt = False
        deps_error = ""
        deps_drift: list[dict] = []
        try:
            if before_hash != after_hash:
                changed = sp.check_output(
                    ["git", "diff", "--name-only", before_hash, after_hash, "--",
                     "pyproject.toml", "uv.lock"],
                    cwd=repo_dir, stderr=sp.DEVNULL, timeout=10,
                ).decode().strip()
            else:
                changed = ""

            # Auto-deps drift check: compare installed versions to pyproject pins
            # independently of git diff. Catches the "pyproject bumped earlier,
            # routine restart skipped reinstall" case that force_deps used to
            # paper over manually.
            try:
                deps_drift = _check_installed_deps_drift(repo_dir)
            except Exception as drift_exc:
                _log(f"admin: deps drift check failed (non-fatal): {drift_exc}")
                deps_drift = []

            if changed or force_deps or deps_drift:
                # Prefer project venv pip if present, else use the running daemon's
                # interpreter (sys.executable). This works for both venv and
                # system-python deployments — the prior `.venv/bin/pip`-only path
                # silently skipped rebuilds on system-python hosts.
                venv_pip = Path(repo_dir) / ".venv" / "bin" / "pip"
                if venv_pip.exists():
                    pip_cmd = [str(venv_pip), "install", "-e", ".[all]", "--quiet"]
                else:
                    # PEP 668: system pythons (Homebrew, Debian) mark themselves
                    # externally-managed. --break-system-packages lets us install
                    # into the same env the daemon imports from.
                    pip_cmd = [
                        sys.executable, "-m", "pip", "install",
                        "-e", ".[all]", "--quiet", "--break-system-packages",
                    ]
                sp.check_output(pip_cmd, cwd=repo_dir, stderr=sp.STDOUT, timeout=180)
                deps_rebuilt = True
        except sp.CalledProcessError as e:
            deps_error = f"pip install failed: {e.output.decode()[:500] if e.output else e}"
            _log(f"admin: {deps_error}")
        except Exception as e:
            deps_error = f"deps rebuild failed: {e}"
            _log(f"admin: {deps_error}")

        # Always rebuild frontend on update to keep compiled assets fresh
        frontend_rebuilt = False
        frontend_error = ""
        frontend_manifest: dict | None = None
        try:
            fe_dir = str(Path(repo_dir) / "frontend-svelte")
            npm_path = shutil.which("npm")
            if npm_path and Path(fe_dir).exists():
                _log("admin: rebuilding frontend...")
                sp.check_output(
                    [npm_path, "install", "--silent"], cwd=fe_dir, stderr=sp.STDOUT, timeout=120
                )
                sp.check_output(
                    [npm_path, "run", "build"], cwd=fe_dir, stderr=sp.STDOUT, timeout=120
                )
                frontend_rebuilt = True
            elif not npm_path:
                frontend_error = "npm not found — install Node.js 18+ to enable auto frontend builds"
                _log(f"admin: {frontend_error}")
        except Exception as e:
            frontend_error = f"Frontend build failed: {e}"
            _log(f"admin: {frontend_error}")

        if frontend_rebuilt:
            try:
                frontend_manifest = _write_frontend_build_manifest(
                    repo_dir, git_hash=after_hash,
                )
            except Exception as e:
                frontend_error = f"Frontend build manifest failed: {e}"
                _log(f"admin: {frontend_error}")

        frontend_status = _frontend_build_status(repo_dir, current_git_hash=after_hash)
        app.state.frontend_build_status = frontend_status

        result = {
            "updated": True,
            "before_hash": before_hash,
            "after_hash": after_hash,
            # The release actually deployed (the resolved ref when it's a tag);
            # None for an operator-pinned commit. latest_release reports the
            # newest tag regardless, for context.
            "release": decision.ref if decision.kind == "release" else None,
            "latest_release": target_tag,
            "deploy_ref": decision.ref,
            "deploy_kind": decision.kind,
            "verified": decision.verified,
            "commits": summary.splitlines() if summary else [],
            "deps_rebuilt": deps_rebuilt,
            "deps_error": deps_error or None,
            "deps_drift": deps_drift,
            "frontend_rebuilt": frontend_rebuilt,
            "frontend_error": frontend_error or None,
            "frontend_manifest": frontend_manifest,
            "frontend_status": frontend_status,
            "forced_reset": forced_reset,
            "forced_files": forced_files,
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

    @app.post("/admin/force-restart-agent/{name}")
    async def admin_force_restart_agent(
        name: str, req: ForceRestartAgentRequest | None = None
    ):
        """Force a fresh streaming session for a WEDGED agent (task #103).

        Recovery escape hatch for the case where an agent's reader loop
        is stalled on the LLM and it can no longer call
        ``save_my_context``, so the normal ``/agents/{name}/streaming/
        restart`` gate's ``within_buffer`` check fails permanently.

        Bypass mechanism: bump ``agent_contexts.updated_at`` to NOW,
        which satisfies the gate without requiring an agent-side save.
        The saved context itself is preserved (we still wake into it);
        only the timestamp is fudged. If no saved context exists at all
        we refuse — there's nothing to wake into and the fresh session
        would have no orientation.

        Anti-abuse: requires the agent's last heartbeat to be at least
        ``req.min_heartbeat_age_sec`` old (default 600s / 10 min). A
        recently-alive agent is "busy," not "wedged." Agents that have
        never recorded a heartbeat are allowed (genuinely fresh).

        Audit: emits ``activity.log`` + ``session_event_store.log``
        entries with event_type=``force_restart`` and the caller's
        reason in metadata.

        Validation reference: procedure validated 2026-05-06 on Sasha
        (wedged 12h) + Ivan (wedged 9h); both recovered cleanly.
        Reference memory: a29089c4e81a.
        """
        request = req or ForceRestartAgentRequest()
        ss = broker._get_streaming_session(name)
        if not ss:
            raise HTTPException(404, f"No streaming session for '{name}'")

        # Anti-abuse: refuse if agent is actively heartbeating. Crucially
        # uses ``get_latest_agent_heartbeat`` (NOT ``get_latest_heartbeat``)
        # so synthetic ``server_presence`` rows the scheduler writes
        # when the transport is merely CONNECTED don't mask a wedged
        # reader loop — that's exactly the failure mode this endpoint
        # targets (Murzik review of #573).
        latest_hb = agents.get_latest_agent_heartbeat(name)
        heartbeat_age_sec: int | None = None
        if latest_hb and latest_hb.timestamp:
            heartbeat_age_sec = int(time.time() - float(latest_hb.timestamp))
            if heartbeat_age_sec < request.min_heartbeat_age_sec:
                raise HTTPException(
                    409,
                    f"Agent '{name}' agent-origin heartbeat is "
                    f"{heartbeat_age_sec}s old "
                    f"(< {request.min_heartbeat_age_sec}s threshold); "
                    f"not wedged. Use /agents/{name}/streaming/restart "
                    f"for a normal restart, or lower "
                    f"min_heartbeat_age_sec if you have a genuine "
                    f"reason to override.",
                )

        # Refuse if there's no saved context — fresh session would have
        # no orientation and the wake context would be empty. Better to
        # surface this as a 412 than to silently restart into a void.
        prior_context = agents.get_context(name)
        if prior_context is None:
            raise HTTPException(
                412,
                f"No saved context exists for '{name}'; cannot force-restart "
                f"without state to wake into. Call save_my_context once "
                f"from any session (or use /agents/{name}/wake to start "
                f"fresh).",
            )

        # Capture pre-bump context age for the audit trail BEFORE the
        # timestamp bump erases the diagnostic (Murzik review of #573).
        # Operators reviewing 'why did we force-restart this agent'
        # benefit from knowing the saved context was, e.g., 12h stale
        # at force-restart time — that's the headline 'wedged' signal.
        prior_context_updated_at = float(prior_context.updated_at or 0.0)
        prior_context_age_sec: int | None = (
            int(time.time() - prior_context_updated_at)
            if prior_context_updated_at > 0
            else None
        )

        # The actual bypass: bump updated_at so the gate's within_buffer
        # check passes. Done BEFORE the restart so the existing
        # restart_streaming_session logic (which gates internally on
        # /streaming/restart) would also pass — but we don't call that
        # endpoint, we inline the restart below to capture the audit
        # metadata and avoid a second HTTP hop.
        bumped = agents.bump_context_updated_at(name)
        if not bumped:  # pragma: no cover — get_context() check above prevents this
            raise HTTPException(412, f"No saved context for '{name}'")

        # Inline the restart_streaming_session() body. We mirror it
        # rather than calling the endpoint so we own the audit metadata
        # (force_restart vs context_restart) and skip the guard recheck
        # the public endpoint does — we just satisfied it.
        old_resume_handle = ss.resume_handle
        old_turns = ss._stats["turns"]

        await ss.disconnect()
        agents.set_streaming_session_id(name, "", label="main")

        ss._config.wake_context = _build_streaming_wake_context(name, commit=False)
        ss._config.resume_handle = ""
        ss._config.restart_reason = "force_restart"
        ss._config.force_fresh_context_once = True
        ss.resume_handle = ""
        if hasattr(ss, "codex_session_id"):
            if ss.codex_session_id:
                _log(
                    f"api: clearing stale codex thread {ss.codex_session_id[:12]} "
                    f"for {name} (force-restart)"
                )
            ss.codex_session_id = ""

        audit_meta = {
            "label": "main",
            "old_session_id": old_resume_handle[:12] if old_resume_handle else "",
            "reason": request.reason or "",
            "heartbeat_age_sec": heartbeat_age_sec,
            "prior_context_updated_at": prior_context_updated_at,
            "prior_context_age_sec": prior_context_age_sec,
            "source": "force_restart_endpoint",
        }
        try:
            await ss.connect()
            _log(
                f"api: FORCE-restarted streaming session for {name} "
                f"(heartbeat_age={heartbeat_age_sec}s, "
                f"reason={request.reason!r})"
            )
            activity.log(
                agent_name=name,
                event_type="force_restart",
                title=f"{name} force-restarted via /admin/force-restart-agent",
                metadata=audit_meta,
            )
            session_event_store.log(
                session_id=ss.id,
                agent_name=name,
                event_type="force_restart",
                metadata=audit_meta,
            )
        except Exception as e:
            broker.unregister_streaming(name)
            raise HTTPException(500, f"Failed to force-restart: {e}")

        return {
            "restarted": True,
            "forced": True,
            "agent": name,
            "old_session_id": old_resume_handle[:12] if old_resume_handle else "",
            "old_turns": old_turns,
            "heartbeat_age_sec": heartbeat_age_sec,
            "prior_context_updated_at": prior_context_updated_at,
            "prior_context_age_sec": prior_context_age_sec,
            "reason": request.reason or "",
        }

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

    # ── User Profiles (learned from dreams) ─────────────────

    from pinky_daemon.user_profile_store import UserProfileStore

    user_profiles = UserProfileStore()

    from pinky_daemon.routes.user_profiles import router as _user_profiles_router
    from pinky_daemon.routes.user_profiles import set_dependencies as _user_profiles_set_deps

    _user_profiles_set_deps(user_profiles=user_profiles, agents=agents)
    app.include_router(_user_profiles_router)


    @app.get("/settings/main-agent")
    async def get_main_agent_setting():
        """Get the designated main agent."""
        return {"agent": agents.get_main_agent()}

    @app.put("/settings/main-agent")
    async def set_main_agent_setting(req: SetMainAgentRequest):
        """Set the designated main agent."""
        agent = agents.get(req.agent)
        if not agent:
            raise HTTPException(404, f"Agent '{req.agent}' not found")
        agents.set_main_agent(req.agent)
        return {"agent": req.agent}

    @app.get("/settings/default-provider")
    async def get_default_provider_setting():
        """Get the default global provider id used by unconfigured agents."""
        provider_id = (agents.get_setting("default_provider_ref", "") or "").strip()
        provider = None
        if provider_id:
            row = agents._db.execute(
                "SELECT id, name, preset, provider_url, provider_key, provider_model, created_at, updated_at "
                "FROM providers WHERE id=?",
                (provider_id,),
            ).fetchone()
            if row:
                provider = _provider_public_row_to_dict(row)
            else:
                provider_id = ""
        return {"provider_id": provider_id, "provider": provider}

    @app.put("/settings/default-provider")
    async def set_default_provider_setting(req: SetDefaultProviderRequest):
        """Set/clear the default global provider used by unconfigured agents."""
        provider_id = (req.provider_id or "").strip()
        if provider_id:
            row = agents._db.execute("SELECT id FROM providers WHERE id=?", (provider_id,)).fetchone()
            if not row:
                raise HTTPException(404, f"Provider '{provider_id}' not found")
        agents.set_setting("default_provider_ref", provider_id)
        return {"provider_id": provider_id}

    @app.get("/system/auth")
    async def get_auth_status():
        """Check Claude Code and Codex CLI auth status."""
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
            "codex_installed": bool(shutil.which("codex")),
            "codex_logged_in": False,
            "codex_auth_method": None,
            "has_openai_api_key": bool(os.environ.get("OPENAI_API_KEY")),
        }

        # ── Claude Code auth ──
        if not result["claude_installed"]:
            result["setup_message"] = "Claude Code CLI not found. Install it first: npm install -g @anthropic-ai/claude-code"
        else:
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
            except Exception as e:
                result["setup_message"] = f"Could not check Claude auth status: {e}"

        # ── Codex CLI auth ──
        if result["codex_installed"]:
            try:
                proc = subprocess.run(
                    ["codex", "login", "status"],
                    capture_output=True, text=True, timeout=10,
                )
                status_text = (proc.stdout.strip() or proc.stderr.strip()).lower()
                if proc.returncode == 0 and status_text and "not logged in" not in status_text:
                    result["codex_logged_in"] = True
                    # Parse auth method from output like "Logged in using ChatGPT"
                    if "using" in status_text:
                        method = status_text.split("using", 1)[1].strip()
                        result["codex_auth_method"] = method
                    else:
                        result["codex_auth_method"] = status_text
            except Exception:
                pass  # Codex auth check is best-effort

        result["setup_required"] = (
            not result["logged_in"]
            and not result["has_api_key"]
        )
        if result["setup_required"] and "setup_message" not in result:
            result["setup_message"] = (
                "Not logged in. Run 'claude login' in your terminal "
                "to authenticate with your Anthropic account."
            )

        return result

    # ── Onboarding ──────────────────────────────────────────────

    @app.get("/system/onboarding-status")
    async def get_onboarding_status():
        """Composite check for onboarding wizard state."""
        auth = await get_auth_status()
        agent_list = agents.list()
        platform_list = outreach_config.list()
        tz = agents.get_default_timezone()
        owner = agents.get_owner_profile()

        return {
            "onboarding_completed": agents.get_setting("onboarding_completed") == "true",
            "has_agents": len(agent_list) > 0,
            "has_api_key": auth.get("has_api_key", False),
            "claude_logged_in": auth.get("logged_in", False),
            "has_platform": any(p.get("enabled") for p in platform_list),
            "has_timezone": tz not in ("UTC", ""),
            "has_owner_name": bool(owner.get("name", "")),
            "agent_count": len(agent_list),
            "platform_count": len(platform_list),
        }

    @app.post("/system/onboarding-complete")
    async def mark_onboarding_complete():
        """Mark onboarding wizard as completed."""
        agents.set_setting("onboarding_completed", "true")
        return {"completed": True}

    @app.post("/system/onboarding-reset")
    async def reset_onboarding():
        """Reset onboarding flag so wizard can be re-run."""
        agents.set_setting("onboarding_completed", "")
        return {"reset": True}

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

    # ── Activity / Audit / Hook-Feed Routes ──────────────────
    from pinky_daemon.routes.activity import router as _activity_router
    from pinky_daemon.routes.activity import set_dependencies as _activity_set_deps

    _activity_set_deps(audit=audit, hooks=hooks, activity=activity)
    app.include_router(_activity_router)

    # ── Task/Project Management ──────────────────────────

    @app.get("/tasks-ui", response_class=HTMLResponse)
    async def tasks_ui():
        return _serve_spa_or_html("tasks.html")

    # ── Project / Task / Sprint / Milestone Routes ──────────────
    from pinky_daemon.routes.projects_tasks import router as _pt_router
    from pinky_daemon.routes.projects_tasks import set_dependencies as _pt_set_deps

    _pt_set_deps(
        tasks=tasks,
        research=research,
        presentations=presentations,
        activity=activity,
        autonomy=autonomy,
        kb_auto_ingest=_kb_auto_ingest,
    )
    app.include_router(_pt_router)

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

    # ── Memory + Chat-History + KG Routes ──────────────────────
    from pinky_daemon.routes.memory import router as _memory_router
    from pinky_daemon.routes.memory import set_dependencies as _memory_set_deps

    _memory_set_deps(
        agents=agents,
        store=store,
        collect_agent_session_ids=_collect_agent_session_ids,
        resolve_agent_history=_resolve_agent_history,
    )
    app.include_router(_memory_router)

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

    # ── Research Pipeline Endpoints ──────────────────────────
    from pinky_daemon.routes.research import router as _research_router
    from pinky_daemon.routes.research import set_dependencies as _research_set_deps

    _research_set_deps(
        research=research,
        activity=activity,
        kb_ingest_research=_kb_ingest_research,
    )
    app.include_router(_research_router)

    # ── Presentations ─────────────────────────────────────
    from pinky_daemon.routes.presentations import router as _pres_router
    from pinky_daemon.routes.presentations import set_dependencies as _pres_set_deps

    _pres_set_deps(
        presentations=presentations,
        research=research,
        agents=agents,
        broker=broker,
        activity=activity,
    )
    app.include_router(_pres_router)

    # ── Trigger Endpoints + Public Webhook Receiver ──────────
    from pinky_daemon.routes.triggers import router as _triggers_router
    from pinky_daemon.routes.triggers import set_dependencies as _triggers_set_deps

    _triggers_set_deps(
        trigger_store=trigger_store,
        agents=agents,
        log=_log,
        wake_callback=_wake_callback,
    )
    app.include_router(_triggers_router)

    # ── Knowledge Base ────────────────────────────────────────────────────────
    from pinky_daemon.routes.kb import router as _kb_router
    from pinky_daemon.routes.kb import set_dependencies as _kb_set_deps

    _kb_set_deps(
        kb=kb,
        agents=agents,
        librarian_runner=librarian_runner,
        librarian_state=_librarian_state,
        schedule_librarian=_schedule_librarian,
        trigger_librarian=_trigger_librarian,
        kb_ingest_people_profiles=_kb_ingest_people_profiles,
        kb_ingest_project_state=_kb_ingest_project_state,
    )
    app.include_router(_kb_router)

    # ── Apps ──────────────────────────────────────────────────
    from pinky_daemon.routes.apps import router as _apps_router
    from pinky_daemon.routes.apps import set_dependencies as _apps_set_deps

    _apps_set_deps(app_store=app_store)
    app.include_router(_apps_router)

    # Voice ConversationRelay WebSocket
    try:
        from pinky_daemon.voice_routes import conversationrelay_ws as _cr_ws

        @app.websocket("/ws/voice/{call_session_id}")
        async def voice_ws(ws: WebSocket, call_session_id: str):
            await _cr_ws(ws, call_session_id)

        _log("voice: WS endpoint registered at /ws/voice/{id}")
    except ImportError:
        pass

    return app
