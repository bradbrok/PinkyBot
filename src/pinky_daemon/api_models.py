"""Pydantic request/response models for the Pinky API.

Extracted from api.py to keep that module focused on app construction and
route registration. All HTTP payload schemas live here.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field, field_validator

# Agent names appear in filesystem paths (data/agents/{name}/, hook scripts,
# settings.json, .mcp.json) and database queries. Restrict to a safe character
# class to prevent path-traversal and arbitrary-write taint — see CodeQL
# alerts on PR #510 that surfaced agent_registry.py's path construction.
# Lowercase + digits + underscore + hyphen, starts with [a-z0-9], 1-63 chars.
_AGENT_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")

# ── Session Models ───────────────────────────────────────────


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
    max_turns: int = 0
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

    token: str | None = None
    enabled: bool | None = None
    settings: dict | None = None


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
    locale: str = ""  # BCP-47 locale tag for UI language (e.g. "en", "ru", "ja")
    code_word: str = ""


class SetMainAgentRequest(BaseModel):
    """Set the main (primary) system agent."""

    agent: str


class SetDefaultProviderRequest(BaseModel):
    """Set the default global provider used by agents without explicit provider config."""

    provider_id: str = ""


# ── Agent Models ─────────────────────────────────────────────


class RegisterAgentRequest(BaseModel):
    """Register a named agent."""

    name: str
    display_name: str = ""

    @field_validator("name")
    @classmethod
    def _name_safe(cls, v: str) -> str:
        """Reject agent names that don't match the safe-char allowlist.

        The name becomes part of filesystem paths (working_dir, hook scripts,
        settings.json) downstream. Blocking unsafe characters at request
        parse time short-circuits any path-construction code in
        agent_registry from ever seeing tainted input. See _AGENT_NAME_RE
        for the allowed shape.
        """
        if not _AGENT_NAME_RE.fullmatch(v):
            raise ValueError(
                "agent name must match ^[a-z0-9][a-z0-9_-]{0,62}$ "
                "(lowercase alphanumeric, underscore, hyphen; "
                "starts with letter or digit; up to 63 chars)"
            )
        return v

    model: str = "opus"
    soul: str = ""
    users: str = ""
    boundaries: str = ""
    system_prompt: str = ""
    working_dir: str = ""  # Empty = auto-creates data/agents/{name}/
    permission_mode: str = "auto"
    allowed_tools: list[str] = Field(default_factory=list)
    disallowed_tools: list[str] = Field(default_factory=list)
    max_turns: int = 0
    timeout: float = 300.0
    max_sessions: int = 5
    plain_text_fallback: bool = False
    groups: list[str] = Field(default_factory=list)
    auto_start: bool = False
    role: str = ""
    heartbeat_interval: int = 0
    runtime: str = "claude_sdk"
    transport: str = "sdk"  # Claude runtime transport: sdk, tmux
    provider_url: str = ""  # ANTHROPIC_BASE_URL override (e.g. Ollama endpoint)
    provider_key: str = ""  # ANTHROPIC_API_KEY override
    provider_model: str = ""  # Model name override for this provider
    provider_ref: str = ""  # ID of a global provider from the providers table
    thinking_effort: str = "medium"  # low/medium/high/xhigh/max
    strict_effort_enforcement: bool = False  # PR #429 — block tool calls when effort drifts
    watchdog_config: dict | None = None  # Per-agent watchdog overrides


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
    disallowed_tools: list[str] | None = None
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
    runtime: str | None = None  # Runtime selector: claude_sdk, codex_cli, opencode
    transport: str | None = None  # Claude runtime transport: sdk, tmux
    provider_url: str | None = None  # ANTHROPIC_BASE_URL override (e.g. Ollama endpoint)
    provider_key: str | None = None  # ANTHROPIC_API_KEY override
    provider_model: str | None = None  # Model name override for this provider
    provider_ref: str | None = None  # ID of a global provider from the providers table
    thinking_effort: str | None = None  # low/medium/high/xhigh/max
    strict_effort_enforcement: bool | None = None  # PR #429 — block tool calls when effort drifts
    watchdog_config: dict | None = None  # Per-agent watchdog overrides


class AddDirectiveRequest(BaseModel):
    """Add a directive to an agent."""

    directive: str
    priority: int = 0


class SetModelRequest(BaseModel):
    """Change model on a streaming session."""

    model: str


class ForceRestartAgentRequest(BaseModel):
    """Force-restart a wedged agent (task #103).

    Escape hatch for the case where an agent's reader loop is stalled
    on the LLM and it can no longer call ``save_my_context``, so the
    normal /agents/{name}/streaming/restart gate fails. Bumps the
    saved-context ``updated_at`` to satisfy ``within_buffer`` and
    triggers the restart. Requires the agent's last **agent-origin**
    heartbeat (excludes scheduler ``server_presence`` rows — Murzik
    review of #573) to be at least ``min_heartbeat_age_sec`` old as
    anti-abuse — don't force-restart a healthy agent that's just busy.
    """

    reason: str = ""
    # 10 minutes default — see memory a29089c4e81a. ``Field(ge=0)``
    # prevents a typo from silently disabling the gate by passing a
    # negative value (which would always satisfy the freshness check).
    min_heartbeat_age_sec: int = Field(default=600, ge=0)


class AgentMessageRequest(BaseModel):
    """Send a message from one agent to another."""

    from_agent: str
    message: str
    content_type: CONTENT_TYPES = "text"
    parent_message_id: int | None = None
    priority: PRIORITY_LEVELS = 0
    metadata: dict = Field(default_factory=dict)


class KBIngestRequest(BaseModel):
    """File a new raw source into the knowledge base."""

    title: str
    content: str
    source_url: str | None = None
    source_type: str = "note"
    filed_by: str = "unknown"
    tags: list[str] = Field(default_factory=list)
    owner_notes: str = ""


class UpsertProfileEntry(BaseModel):
    """Add or update a profile entry."""

    category: str
    key: str
    value: str
    confidence: float = 0.8
    source: str = "manual"


class UpdateProfileEntry(BaseModel):
    """Update a specific profile entry."""

    value: str | None = None
    confidence: float | None = None


class SetVisibilityRequest(BaseModel):
    """Set profile visibility for an agent."""

    visible: bool = True


class AddRelationshipRequest(BaseModel):
    """Add a relationship between users."""

    to_chat_id: str = ""
    to_display_name: str
    relation: str
    context: str = ""
    confidence: float = 0.8


class SetAgentTokenRequest(BaseModel):
    """Set a bot token for an agent."""

    token: str = ""
    token_ref: str = ""
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


class ForkSessionRequest(BaseModel):
    """Fork an existing session into a new branch."""

    title: str = ""  # Custom title for the fork; auto-derived if empty
    up_to_message_id: str = ""  # Fork up to this message UUID (inclusive); full history if empty


class CloneWorkerRequest(BaseModel):
    """Fork the agent's main session and spin up a worker clone with a task."""

    task: str  # The prompt/task to send as the worker's initial message
    title: str = ""  # Optional fork title


# ── Project / Task Models ─────────────────────────────────────


class CreateTaskRequest(BaseModel):
    title: str
    project_id: int = 0
    milestone_id: int = 0
    sprint_id: int = 0
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
    milestone_id: int | None = None
    sprint_id: int | None = None
    description: str | None = None
    status: str | None = None
    priority: str | None = None
    assigned_agent: str | None = None
    tags: list[str] | None = None
    due_date: str | None = None
    parent_id: int | None = None
    blocked_by: list[int] | None = None


class CreateMilestoneRequest(BaseModel):
    name: str
    description: str = ""
    due_date: str = ""


class UpdateMilestoneRequest(BaseModel):
    name: str | None = None
    description: str | None = None
    due_date: str | None = None
    status: str | None = None


class CreateSprintRequest(BaseModel):
    name: str
    goal: str = ""
    start_date: str = ""
    end_date: str = ""


class UpdateSprintRequest(BaseModel):
    name: str | None = None
    goal: str | None = None
    start_date: str | None = None
    end_date: str | None = None
    status: str | None = None


class CreateProjectRequest(BaseModel):
    name: str
    description: str = ""
    repo_url: str = ""
    team_members: list | None = None
    linked_assets: list | None = None


class UpdateProjectRequest(BaseModel):
    name: str = ""
    description: str = ""
    status: str = ""
    due_date: str = ""
    repo_url: str | None = None
    team_members: list | None = None
    linked_assets: list | None = None


class AddTeamMemberRequest(BaseModel):
    name: str
    role: str = ""
    contact: str = ""


class AddLinkedAssetRequest(BaseModel):
    type: str  # "research", "presentation", "url", etc.
    title: str
    url: str = ""
    description: str = ""
    id: int | None = None  # optional reference ID (e.g. research_topic_id)


class AddCommentRequest(BaseModel):
    author: str = ""
    content: str


# ── Schedule / Trigger Models ─────────────────────────────────


class AddScheduleRequest(BaseModel):
    name: str = ""
    cron: str
    prompt: str = ""
    timezone: str = "America/Los_Angeles"
    direct_send: bool = False
    target_channel: str = ""
    one_shot: bool = False


class CreateTriggerRequest(BaseModel):
    name: str = ""
    trigger_type: str  # 'webhook' | 'url' | 'file'
    url: str = ""
    method: str = "GET"
    condition: str = "status_changed"
    condition_value: str = ""
    file_path: str = ""
    interval_seconds: int = 300
    prompt_template: str = ""
    enabled: bool = True


class UpdateTriggerRequest(BaseModel):
    name: str | None = None
    url: str | None = None
    method: str | None = None
    condition: str | None = None
    condition_value: str | None = None
    file_path: str | None = None
    interval_seconds: int | None = None
    prompt_template: str | None = None
    enabled: bool | None = None


class RecordHeartbeatRequest(BaseModel):
    session_id: str = ""
    status: str = "alive"
    context_pct: float = 0.0
    message_count: int = 0
    metadata: dict = Field(default_factory=dict)
    notes: str = ""
    latency_ms: int = 0


class SetContextRequest(BaseModel):
    task: str = ""
    context: str = ""
    notes: str = ""
    blockers: list[str] = Field(default_factory=list)
    priority_items: list[str] = Field(default_factory=list)
    wake_action: str = ""
    metadata: dict = Field(default_factory=dict)


class PushEventRequest(BaseModel):
    type: str = "manual_wake"
    data: dict = Field(default_factory=dict)
    priority: int = 0


class AgentStatusRequest(BaseModel):
    """Agent working status update — called by Claude Code hooks."""

    status: Literal["working", "idle", "thinking", "tool_use", "offline"]
    tool_name: str = ""
    detail: str = ""


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


# ── Presentation Models ───────────────────────────────────────


class CreatePresentationRequest(BaseModel):
    title: str
    html_content: str
    description: str = ""
    created_by: str = "admin"
    tags: list[str] = Field(default_factory=list)
    research_topic_id: int | None = None


class UpdatePresentationRequest(BaseModel):
    html_content: str
    description: str = ""
    created_by: str = "admin"
    title: str | None = None
    tags: list[str] | None = None


class RestoreVersionRequest(BaseModel):
    version: int


class GeneratePresentationRequest(BaseModel):
    agent_name: str
    topic_id: int
    instructions: str = ""


class CreateTemplateRequest(BaseModel):
    name: str
    html_content: str
    description: str = ""
    tags: list[str] = Field(default_factory=list)
    thumbnail_css: str = ""


class SetPresentationPasswordRequest(BaseModel):
    password: str = ""  # empty string = remove password protection


# ── App Models ────────────────────────────────────────────────


class CreateAppRequest(BaseModel):
    name: str
    description: str = ""
    app_type: str = "other"
    created_by: str = ""
    tags: list[str] = Field(default_factory=list)
    html_content: str = ""


class UpdateAppRequest(BaseModel):
    name: str | None = None
    description: str | None = None
    app_type: str | None = None
    tags: list[str] | None = None
    status: str | None = None


class DeployAppRequest(BaseModel):
    html_content: str


class SetAppPasswordRequest(BaseModel):
    password: str = ""  # empty string = remove password protection


# ── KB Models ─────────────────────────────────────────────────


class WikiSaveRequest(BaseModel):
    title: str
    content: str
    sources: list[str] = []
    related: list[str] = []


# ── Beta-only additions (merged from beta 2026-05-11) ──────


class EffortDriftRequest(BaseModel):
    """Effort-drift event — POSTed by hook_verify_effort.py (#429).

    Emitted when the runtime ``$CLAUDE_EFFORT`` (Claude Code v2.1.133+)
    diverges from the daemon-injected ``PINKY_EXPECTED_EFFORT``.
    """

    expected: str
    actual: str
    tool_name: str = ""
    strict: bool = False
    session_id: str = ""


class TransportWakeRequest(BaseModel):
    """Backend-agnostic wake signal — POSTed by Stop / PostCompact hooks (PR8b).

    The ``event`` tag tells the Transport why it was woken; backends
    interpret it. For ``TmuxSession`` the tailer's ``wake()`` is
    idempotent and the event is informational — ``stop_hook_summary``
    expects new data in the transcript; ``compact`` should force a
    re-stat in case the file rotated.
    """

    event: Literal["stop_hook_summary", "compact", "manual"] = "stop_hook_summary"
    label: str = "main"


class TransportTranscriptPathRequest(BaseModel):
    """Report the canonical transcript path — POSTed by the SessionStart
    hook (PR8b).

    Lets the daemon repoint the tailer at the actual file Claude Code
    is writing to, instead of relying on the mtime-glob guess. Path is
    absolute; the daemon validates it's under the configured Claude
    projects dir and that the file exists before swapping.
    """

    transcript_path: str
    session_id: str = ""
    label: str = "main"


class TransportToolUseRequest(BaseModel):
    """Per-tool-call start — POSTed by the PreToolUse hook (task #93).

    Mirrors what ``StreamingSession._analytics_start_tool_call`` records
    for SDK agents so tmux-transport agents reach analytics + the SSE
    stream with the same shape.

    ``tool_use_id`` is Claude Code's per-call identifier; the daemon
    uses it as the key for matching the later ``tool-result`` POST.
    ``tool_input`` is the raw input dict from Claude Code — passed
    through verbatim; the daemon extracts arg keys (not values) for
    PII-safe analytics metadata.
    """

    tool_use_id: str = ""
    tool_name: str
    tool_input: dict = {}
    session_id: str = ""
    label: str = "main"


class TransportToolResultRequest(BaseModel):
    """Per-tool-call result — POSTed by the PostToolUse hook (task #93).

    ``tool_use_id`` matches an earlier ``tool-use`` POST so the daemon
    can close out the analytics row and emit the finish stream event.
    ``is_error`` is the consolidated success/error flag; ``tool_response``
    is the raw response payload, useful for the result_preview snippet.
    """

    tool_use_id: str = ""
    tool_name: str = ""
    is_error: bool = False
    tool_response: dict | list | str | None = None
    session_id: str = ""
    label: str = "main"


class TransportStopFailureRequest(BaseModel):
    """Typed turn-failure signal — POSTed by the ``StopFailure`` hook.

    Claude Code fires ``StopFailure`` when a turn ends due to an API
    error. CC delivers the typed failure in its ``error`` field
    (``authentication_failed``, ``rate_limit``, ``billing_error``,
    ``server_error``, …); the hook maps that onto this ``error_type``.
    Forwarding it lets the daemon alert the owner on auth-class failures
    proactively — instead of an agent going silently dark — and feed the
    rest (rate_limit / billing) to logs/observability.

    Fire-and-forget like the other transport hooks: the hook wraps the
    POST in ``|| true`` so a daemon hiccup never fails the model turn.
    """

    error_type: str = "unknown"
    message: str = ""
    session_id: str = ""
    label: str = "main"


class FederationPeerUpsertRequest(BaseModel):
    """Insert or update a federation peer (admin / config-seeded path).

    Composite key: (fleet, agent). ``seed_source='config'`` is the stronger
    statement of intent and never gets downgraded by an observed update.
    """

    fleet: str
    agent: str
    display_name: str = ""
    seed_source: str = "config"  # "config" | "observed"


class MeshAllowlistEntryRequest(BaseModel):
    """Add or remove one pattern in an agent's mesh outbound allowlist."""

    pattern: str  # e.g. "pulse@pulse", "*@pulse", "pulse@*"


class MeshAllowlistSetRequest(BaseModel):
    """Replace the full mesh outbound allowlist for an agent."""

    patterns: list[str] = []


class MeshSendRequest(BaseModel):
    """Publish a ferry envelope to a remote agent via the daemon's mesh sender.

    The agent making this request must have ``target`` matching at least one
    pattern in its ``mesh_outbound_allowlist``; default-deny otherwise.

    ``body`` is a free-form payload — caller decides shape; ``kind`` is
    inserted into ``body.kind`` per ferry PROTOCOL.md v0.1 §1.
    """

    target: str  # "agent_slug@fleet" or "ferry://fleet/agent_slug"
    body: str | dict = ""
    kind: str = "msg"
    correlation_id: str = ""
    reply_to: str = ""
    priority: str = "normal"


class PeerFleetAclEntryRequest(BaseModel):
    """Add or remove one peer-fleet ACL selector for an agent.

    Selectors are shaped after `pinky_daemon.ferry.types.AgentCardSelector` —
    at-least-one of fleet/agent_id/pinky_type must be non-empty. Wildcards via
    agent_id="*" or fleet="*".
    """

    fleet: str = ""
    agent_id: str = ""
    pinky_type: str = ""


class PeerFleetAclSetRequest(BaseModel):
    """Replace the full peer-fleet ACL for an agent (full replacement, not merge)."""

    selectors: list[PeerFleetAclEntryRequest] = []
