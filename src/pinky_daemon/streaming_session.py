"""Streaming Session — persistent bidirectional Claude Code connection.

Uses ClaudeSDKClient for non-blocking message delivery. Messages go in
via send(), responses come back via a background reader loop that calls
the response callback.

This is the preferred session type for broker-connected agents where
messages arrive asynchronously from platform users.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from pinky_daemon.context_window import resolve_context_window
from pinky_daemon.effort import CLI_EFFORT_LEVELS, resolve_cli_effort
from pinky_daemon.sessions import SessionUsage
from pinky_daemon.transport_state import SessionState, StateMachine, Trigger
from pinky_daemon.turn_response import TurnResponse
from pinky_daemon.wake_prompt import (
    WakePromptInput,
    build_idle_sleep_prompt,
    build_wake_prompt,
    wake_reason_from_runtime,
)

# Models with native 1M context (SDK reports 200k incorrectly).
# INVARIANT: must contain every model the registry flags is_1m=1
# (agent_registry._MODEL_SEEDS) — pinned by
# test_1m_models_set_matches_registry_is_1m so this can't drift again (#839).
_1M_MODELS = {
    "claude-fable-5",
    "claude-mythos-5",
    "claude-sonnet-5",
    "claude-sonnet-4-6",
    "claude-opus-4-6",
    "claude-opus-4-7",
    "claude-opus-4-8",
    "claude-opus-5",
}


def is_1m_model(model_id: str, model_set: "set[str] | None" = None) -> bool:
    """True if ``model_id`` is a 1M-context model, tolerant of a trailing
    ``[tier]`` suffix (e.g. ``gpt-5.6-sol[1m]`` → ``gpt-5.6-sol``).

    The ``[tier]`` suffix is a legacy way to request a 1M window. An exact
    membership test against ``_1M_MODELS`` misses the suffixed form and
    silently caps a genuine 1M model at 200k. Strip the suffix first (reusing
    pricing's canonical ``strip_tier``), then require the base model to be in
    the reviewed 1M set. A suffix does not fabricate a 1M window; in particular,
    #356 established that ``gpt-5.6-sol[1m]`` is 200k-class with a real
    subscription-backend limit near 167k.
    Defaults to the static ``_1M_MODELS`` set; callers on the api path pass
    their DB-refreshed set.
    """
    from pinky_daemon.pricing import strip_tier
    base = strip_tier(model_id or "")
    return base in (_1M_MODELS if model_set is None else model_set)


DEFAULT_STREAMING_ALLOWED_TOOLS = [
    "Read",
    "Glob",
    "Grep",
    "Agent",  # subagent spawning
    "mcp__memory__*",
    "mcp__pinky-memory__*",
    "mcp__pinky-self__*",
    "mcp__pinky-messaging__*",
]


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _notify_turn_idle(config: "StreamingSessionConfig", agent_name: str) -> None:
    """Report a real turn boundary without letting callback failures escape."""
    callback = config.on_turn_idle
    if callback is None:
        return
    try:
        callback(agent_name)
    except Exception as exc:
        _log(
            f"streaming[{agent_name}]: on_turn_idle callback failed: "
            f"{type(exc).__name__}: {exc}"
        )


@dataclass
class StreamingSessionConfig:
    """Configuration for a streaming session."""
    agent_name: str = ""
    label: str = "main"
    model: str = ""
    working_dir: str = "."
    allowed_tools: list[str] = field(default_factory=list)
    disallowed_tools: list[str] = field(default_factory=list)
    mcp_servers: dict = field(default_factory=dict)
    permission_mode: str = "bypassPermissions"
    max_turns: int = 0
    system_prompt: str = ""
    resume_handle: str = ""  # SDK resume token (opaque session-continuation handle) from previous run
    wake_context: str = ""  # Saved continuation context to inject on wake
    wake_context_builder: object = None  # Callable(agent_name) -> str; refreshes wake_context on restart
    # Fires AFTER successful wake-prompt delivery (paste/query landed).
    # Callers (api.py) wire it to log ``agent_wake`` so the previous-wake
    # timestamp the #591 cycle-bound gate reads is advanced on EVERY
    # delivered wake — not just cold-start + scheduler (Murzik P1#2).
    # Failure-tolerant: delivery failures must NOT fire the callback or
    # the boundary advances against a wake that never reached the model
    # → the directive would be eaten by a wedged paste.
    on_wake_delivered: object = None  # Callable(agent_name, WakeReason) -> None
    # Fires when a completed turn leaves no queued transport work. Scheduler
    # delivery uses this edge to drain durable wakes without timer polling.
    on_turn_idle: object = None  # Callable(agent_name) -> None
    # Tmux-only #984 recovery seam.  A verified-failed context-restart wake
    # uses this to route a CONTEXT-RELOAD instruction through the broker's
    # agent-message path.  The callback returns a positive handoff bool; the
    # session escalates to transport recovery when it is absent or false.
    wake_submission_recovery_injector: object = None
    restart_guard: object = None  # Callable(session) -> dict; blocks restart if persistence is stale
    live_status_fn: object = None  # Callable() -> dict|None; agent's live REPL status {"status","last_updated"} from Claude Code working/idle hooks. Tmux inflight watchdog uses it to avoid force-restarting an idle (not wedged) REPL (#118).
    watchdog_enabled_fn: object = None  # Callable() -> bool; whether this agent's watchdog_config.enabled is set. The tmux inflight watchdog reads it per tick so watchdog_config.enabled=false is an operator kill-switch for BOTH the daemon SessionWatchdog and per-session inflight recovery (#846). None → treated as enabled (default True).
    context_warn_pct: int = 40  # Warn agent to save state at this %
    context_restart_pct: int = 80  # Force restart at this %
    restart_guard_cooldown_sec: int = 60  # Minimum gap between restart-block warnings
    idle_timeout: int = 0  # Auto-sleep after this many seconds idle (0 = disabled); set from agent.auto_sleep_hours
    timezone: str = "America/Los_Angeles"  # IANA timezone for wake timestamp
    subagents: dict = field(default_factory=dict)  # name -> AgentDefinition
    provider_url: str = ""   # ANTHROPIC_BASE_URL override (e.g. "http://localhost:11434" for Ollama)
    provider_key: str = ""   # ANTHROPIC_API_KEY override (empty = use env var)
    thinking_effort: str = "medium"  # low, medium, high, xhigh, max, ultracode — default thinking depth
    # When True, the verify_effort CLI hook blocks tool calls if the runtime
    # effort drifts from thinking_effort. Default False (warn-only). See #429.
    strict_effort_enforcement: bool = False
    restart_reason: str = ""  # "context_restart", "auto_restart", etc. — cleared after wake prompt
    # When True, the NEXT transport launch starts with a fresh context
    # (e.g. tmux suppresses ``claude --continue`` even if a prior transcript
    # exists). The flag is one-shot — the transport clears it after
    # honoring it. This is a SEPARATE contract from ``restart_reason``:
    # the latter controls wake-prompt copy; this controls launch behavior.
    # Coupling them caused #543 (tmux ``context_restart`` silently
    # resumed the prior transcript because ``_build_claude_cmd`` only
    # looked at transcript existence, not ``restart_reason``).
    force_fresh_context_once: bool = False


# Backward-compatible import name for older callers/tests. New code should use
# TurnResponse directly.
StreamingTurnResult = TurnResponse


_OUTREACH_TOOL_NAMES = {
    "thread",
    "reply",  # deprecated alias for thread
    "send",
    "react",
    "send_gif",
    "send_voice",
    "send_photo",
    "send_document",
    "send_video",
    "broadcast",
    "send_message",
    "add_reaction",
    "send_voice_note",
}


def _tool_basename(tool_name: str) -> str:
    if "__" in tool_name:
        return tool_name.rsplit("__", 1)[-1]
    return tool_name


def _is_outreach_tool(tool_name: str) -> bool:
    return _tool_basename(tool_name) in _OUTREACH_TOOL_NAMES


# Auth-failure detection — uses native SDK types (claude-agent-sdk >= 0.1.76).
#
# Two paths surface credential failures, and we check both:
#
# 1. ``AssistantMessage.error`` is an ``AssistantMessageError`` Literal whose
#    only credential-failure value is "authentication_failed". The other
#    Literal members (billing_error, rate_limit, invalid_request,
#    server_error, unknown) are NOT auth issues and must NOT trip the
#    operator alert — billing errors and rate limits in particular would
#    spam the operator on perfectly authenticated sessions.
#
# 2. ``ResultMessage.api_error_status`` is the raw HTTP status of a failing
#    API call (added in SDK 0.1.76, emitted by CLI >= 2.1.110). 401 and 403
#    are credential failures; 429 and 5xx are transient/operational and
#    handled separately.
#
# This replaces an older substring-tuple match against ``msg.error`` that
# pre-dated the Literal type and over-matched (e.g. "permission_error" is
# never a value the SDK can produce).
_AUTH_ASSISTANT_ERROR = "authentication_failed"
_AUTH_HTTP_STATUSES = frozenset({401, 403})


def _is_auth_error_assistant(msg) -> bool:
    """True if an ``AssistantMessage`` indicates a credential failure."""
    return getattr(msg, "error", None) == _AUTH_ASSISTANT_ERROR


def _is_auth_error_result(result) -> bool:
    """True if a ``ResultMessage``'s HTTP status indicates a credential failure."""
    status = getattr(result, "api_error_status", None)
    return status in _AUTH_HTTP_STATUSES


def _describe_tool_use(tool_name: str, tool_input: dict) -> str:
    """Build a human-readable description of a tool invocation."""
    name = _tool_basename(tool_name)
    inp = tool_input or {}
    detail = ""

    if name == "Bash":
        desc = inp.get("description", "")
        cmd = inp.get("command", "")
        detail = desc or (cmd[:60] if cmd else "")
    elif name == "Read":
        path = inp.get("file_path", "")
        detail = path.rsplit("/", 1)[-1] if path else ""
    elif name == "Write":
        path = inp.get("file_path", "")
        detail = path.rsplit("/", 1)[-1] if path else ""
    elif name == "Edit":
        path = inp.get("file_path", "")
        detail = path.rsplit("/", 1)[-1] if path else ""
    elif name == "Grep":
        pattern = inp.get("pattern", "")
        detail = f'"{pattern[:40]}"' if pattern else ""
    elif name == "Glob":
        pattern = inp.get("pattern", "")
        detail = pattern[:40] if pattern else ""
    elif name in ("WebSearch", "web_search"):
        query = inp.get("query", "")
        detail = query[:50] if query else ""
    elif name in ("WebFetch", "web_fetch"):
        url = inp.get("url", "")
        detail = url[:60] if url else ""

    # MCP tools: show server__tool as "server: tool"
    if "__" in tool_name:
        parts = tool_name.split("__", 2)
        if len(parts) >= 3:
            name = f"{parts[1]}: {parts[2]}"

    if detail:
        return f"{name} — {detail}"
    return name


class StreamingSession:
    """Persistent bidirectional Claude Code session via SDK client.

    Unlike Session which blocks on each send(), StreamingSession:
    - Connects once and stays connected
    - send() writes to transport and returns immediately
    - A background reader loop processes responses
    - Response callback fires when agent finishes a turn
    """

    # An in-process transport: ``send`` calls ``client.query`` which enqueues
    # straight into the live turn stream, and returns a PER-CALL handoff bool
    # (False when the query raised — swallowed + reconnect, #853 P1). The
    # broker confirms an inject only when this capability AND that per-call
    # handoff are both true. Contrast TmuxSession (external pane) which sets
    # this False. See MessageBroker.inject_agent_message / InjectResult.
    injection_confirms_consumption: bool = True

    def __init__(
        self,
        config: StreamingSessionConfig,
        *,
        response_callback=None,  # async fn(TurnResponse)
        conversation_store=None,  # ConversationStore for history logging
        cost_callback=None,  # fn(agent_name, cost_usd, input_tokens, output_tokens, resume_handle)
        analytics_store=None,
        registry=None,  # AgentRegistry — for server-side presence stamping
        auth_alert_callback=None,  # async fn(agent_name, error_str) — fires on auth_failed
        auth_success_callback=None,  # fn(agent_name) — fires on a successful turn (clears auth fail state)
    ) -> None:
        self._config = config
        self._response_callback = response_callback
        self._cost_callback = cost_callback  # Sync callback to persist costs
        self._conversation_store = conversation_store
        self._analytics_store = analytics_store
        self._registry = registry
        # Auth-failure detection plumbing — called from the reader loop when
        # the SDK reports an authentication error so the daemon can alert
        # the operator. See pinky_daemon/auth_alerts.py for the tracker.
        self._auth_alert_callback = auth_alert_callback
        self._auth_success_callback = auth_success_callback
        self._client = None
        self._reader_task: asyncio.Task | None = None
        # Strong refs to background tasks (e.g. the post-handshake wake
        # prompt detached from connect()). Python's asyncio docs warn:
        # tasks created via asyncio.create_task() must have a strong
        # reference, otherwise the GC can collect them mid-flight. We
        # keep them in a set and have each task auto-discard itself via
        # add_done_callback when it finishes.
        self._background_tasks: set[asyncio.Task] = set()
        # PR3 (#486 sequence): formal adoption of the Transport protocol.
        # The pre-PR3 ``_connected`` + ``_idle_sleeping`` two-bool inference
        # was replaced by an explicit state machine. PR4 deleted the legacy
        # ``is_connected`` / ``is_idle_sleeping`` shim properties and
        # migrated all external readers (broker, api, scheduler, watchdog)
        # to consult ``state`` directly. PR5 renamed the in-memory SDK
        # resume token from ``session_id`` to ``resume_handle``. PR6 wired
        # the cold-start UNINITIALIZED → BOOTING → CONNECTED lifecycle
        # through the matrix via the BOOT / BOOT_COMPLETE / BOOT_FAILED
        # Trigger triplet (see ``connect()`` for the BOOT lifecycle).
        #
        # Warm-reconnect state writes (force_restart, idle_sleep, etc.)
        # still mutate ``_state`` directly at the same code points as the
        # pre-state-machine code. Adding RECONNECT_BEGIN / RECONNECT_COMPLETE /
        # RECONNECT_FAILED Trigger symmetry to the warm path is the
        # post-PR6 follow-up.
        #
        # Watchdog resurrection (api._heartbeat_resurrect) inspects
        # ``state == IDLE_SLEEPING`` to avoid fighting the idle-sleep
        # state — see issue #348.
        self._state_machine = StateMachine(
            owner_label=f"{config.agent_name}-{config.label or 'main'}",
            initial_state=SessionState.UNINITIALIZED,
        )
        self._last_response = ""
        self._pending_chats: list[tuple[str, str, str]] = []  # Queue of (platform, chat_id, message_id)
        # Set by the reader loop at every turn boundary (ResultMessage).
        # idle_sleep() awaits it so the save-state turn can finish before
        # the transport is torn down.
        self._turn_done = asyncio.Event()
        # In-flight warm reconnect, if any. attempt_reconnect() coalesces
        # concurrent callers onto this single task so two reconnects never
        # interleave disconnect/connect (which would orphan an SDK client).
        self._reconnect_task: asyncio.Task | None = None

        self.agent_name = config.agent_name
        self.resume_handle = config.resume_handle  # SDK resume token (persisted across restarts)
        self.created_at = time.time()
        self.last_active = self.created_at
        self.usage = SessionUsage()
        self._stats = {"turns": 0, "messages_sent": 0, "errors": 0, "reconnects": 0, "auto_restarts": 0}
        self._current_activity = ""  # Current tool being used (for UI streaming)
        self._activity_log: list[str] = []  # All tool activities this turn
        self._current_thinking = ""  # Latest thinking block (for UI streaming)
        self.account_info: dict = {}  # Populated from SDK init: email, subscriptionType, apiProvider
        self._on_resume_handle = None  # async fn(agent_name, resume_handle) — called when SDK resume token is captured
        self._context_warned = False  # Track if we've already warned this session
        self._last_restart_block_notice_at = 0.0
        self._effort_override: str | None = None  # Session-level thinking effort override
        # Runtime effort last reported by hooks ($CLAUDE_EFFORT via the
        # effort-drift endpoint) — the CLI's actual level, "" until a hook
        # reports. Read side of the effort knob (model/effort selector).
        self.last_reported_effort: str = ""
        self._turn_seq = 0  # Monotonic turn counter for analytics
        self._last_user_message = ""  # For analytics keyword classification

    async def connect(self) -> None:
        """Connect to Claude Code. Starts the reader loop.

        PR6 (Pushok): cold-start now drives the matrix-correct
        UNINITIALIZED → BOOTING → CONNECTED (or → DEAD on failure) lifecycle
        explicitly via the BOOT / BOOT_COMPLETE / BOOT_FAILED Trigger triplet.
        Warm-reconnect (RECONNECTING → CONNECTED) still direct-mutates pending
        the broader warm-reconnect-Trigger-symmetry follow-up (PR6.5/PR7).
        """
        from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

        # PR6: cold-start wire-up. If we entered connect() in UNINITIALIZED
        # OR a BOOT is already in flight (state == BOOTING with our state
        # machine mid-handshake from a concurrent caller), request BOOT
        # ownership through the state machine. The widened guard is the
        # load-bearing fix for the concurrent-connect race Murzik flagged
        # on PR #494: request_transition mutates state at grant time, so a
        # narrow ``state == UNINITIALIZED`` guard lets caller B enter
        # connect() during caller A's handshake, see state == BOOTING, skip
        # the ownership/subscriber path entirely, and run a second SDK
        # handshake (then direct-mutate CONNECTED via the warm-reconnect
        # else branch). With BOOTING in the guard, the state machine routes
        # caller B to the same-target in-flight branch — caller B subscribes
        # via InFlightHandle and inherits caller A's CONNECTED-or-DEAD
        # outcome, guaranteeing exactly one SDK construction per cold start.
        #
        # The matrix invariant pins the only legal exits from BOOTING as
        # CONNECTED (BOOT_COMPLETE) or DEAD (BOOT_FAILED). The token returned
        # here keeps us responsible for completing the in-flight transition on
        # every exit path — success, failure, or exception.
        cold_start_token = None
        if self.state in (SessionState.UNINITIALIZED, SessionState.BOOTING):
            boot_result = await self._state_machine.request_transition(
                SessionState.BOOTING,
                Trigger.BOOT,
                reason="cold_start_handshake",
            )
            if boot_result.owner_token is None:
                # Either a same-target BOOT is already in flight (we
                # subscribe and inherit the owner's outcome) or a different-
                # target transition is in flight (matrix rejected). The
                # subscriber path is the hot path for the concurrent-connect
                # race; the rejection path is rare in practice but handled
                # defensively rather than crashing.
                if boot_result.in_flight_handle is not None:
                    final = await boot_result.in_flight_handle.wait()
                    if final == SessionState.CONNECTED:
                        # Owner completed the handshake; we're done.
                        return
                    # Owner landed DEAD (cold-start failed) or some other
                    # non-CONNECTED state. Surface the failure — don't let
                    # the subscriber silently return as if connected, which
                    # would leave the caller proceeding against a session
                    # that has no client. The DEAD → RECONNECTING
                    # resurrection path remains available to upstream
                    # callers via the existing warm-reconnect machinery.
                    raise RuntimeError(
                        f"streaming[{self.agent_name}]: cold-start BOOT "
                        f"in-flight resolved to {final.value} (owner failed); "
                        f"refusing to return as connected"
                    )
                # No in-flight handle: rejection (matrix said no), or an
                # observational identity read that snuck through under a
                # post-completion race window. Case D from Pushok's #494
                # review: D enters with state == BOOTING but, by the time
                # request_transition acquires the lock, A has already
                # completed — state has moved to CONNECTED (happy) or DEAD
                # (failed). For CONNECTED, returning silently is fine —
                # the caller will see is_connected. For DEAD, returning
                # silently would let the caller think cold-start succeeded
                # against a dead transport; surface the failure instead.
                _log(
                    f"streaming[{self.agent_name}]: BOOT rejected "
                    f"({boot_result.rejection_reason!r}) — refusing cold-start"
                )
                if self.state == SessionState.DEAD:
                    raise RuntimeError(
                        f"streaming[{self.agent_name}]: cold-start BOOT "
                        f"rejected post-DEAD (owner failed before we "
                        f"subscribed); refusing to return as connected"
                    )
                return
            cold_start_token = boot_result.owner_token

        # PR6.5 follow-up (Pushok's #494 review, Case C): post-completion
        # straggler. A caller entering connect() with state already CONNECTED
        # skips the guard above, falls through to the SDK construction below,
        # and runs a redundant handshake — then direct-mutates CONNECTED via
        # the warm-reconnect else branch. This is the same double-connect
        # class as the BOOTING race but driven from CONNECTED, and predates
        # PR6 (the warm-reconnect path has always done this). Out of scope
        # for the BOOT lifecycle; tracked alongside RECONNECT_COMPLETE /
        # RECONNECT_FAILED Trigger symmetry as PR6.5.

        # Load MCP servers from .mcp.json
        mcp_servers = self._config.mcp_servers
        if not mcp_servers:
            mcp_json_path = Path(self._config.working_dir) / ".mcp.json"
            if mcp_json_path.exists():
                try:
                    mcp_data = json.loads(mcp_json_path.read_text())
                    mcp_servers = mcp_data.get("mcpServers", {})
                    _log(f"streaming[{self.agent_name}]: loaded {len(mcp_servers)} MCP servers")
                except Exception as e:
                    _log(f"streaming[{self.agent_name}]: failed to read .mcp.json: {e}")

        options = ClaudeAgentOptions(
            cwd=self._config.working_dir,
            allowed_tools=self._config.allowed_tools or DEFAULT_STREAMING_ALLOWED_TOOLS,
            permission_mode=self._config.permission_mode,
            mcp_servers=mcp_servers or None,
        )

        if self._config.disallowed_tools:
            options.disallowed_tools = self._config.disallowed_tools

        if self._config.model:
            options.model = self._config.model

        if self._config.max_turns:
            options.max_turns = self._config.max_turns

        if self._config.system_prompt:
            options.system_prompt = self._config.system_prompt

        if self._config.subagents:
            options.agents = self._config.subagents

        # Apply thinking effort. ultracode resolves to xhigh (#151) — the SDK
        # forwards options.effort to the CLI's --effort flag, which rejects
        # the literal "ultracode". Medium is passed EXPLICITLY: the CLI
        # persists the last effort per project dir, so omitting the option
        # for medium-configured agents boots them at whatever the previous
        # session ran at instead of medium.
        effort = resolve_cli_effort(self.effective_effort)
        if effort and effort != "auto":
            options.effort = effort

        # Build provider env overrides (Ollama / custom compatible endpoints)
        provider_env: dict[str, str] = {}
        if self._config.provider_url:
            provider_env["ANTHROPIC_BASE_URL"] = self._config.provider_url
        if self._config.provider_key:
            provider_env["ANTHROPIC_API_KEY"] = self._config.provider_key
            provider_env["ANTHROPIC_AUTH_TOKEN"] = self._config.provider_key

        # #429: surface configured effort + agent identity to CLI hooks so
        # hook_verify_effort.py can detect drift from PINKY_EXPECTED_EFFORT
        # vs $CLAUDE_EFFORT at PreToolUse time. The hook no-ops on "auto" /
        # empty (intentionally adaptive).
        if self.agent_name:
            provider_env["PINKY_AGENT_NAME"] = self.agent_name
        if effort:
            provider_env["PINKY_EXPECTED_EFFORT"] = effort
        if self._config.strict_effort_enforcement:
            provider_env["PINKY_STRICT_EFFORT"] = "1"

        if provider_env:
            # Generous timeout for slow local/third-party models (30 min)
            provider_env.setdefault("API_TIMEOUT_MS", "1800000")
            options.env = provider_env

        # Resume previous session if we have a resume handle
        if self.resume_handle:
            options.resume = self.resume_handle
            _log(f"streaming[{self.agent_name}]: resuming via handle {self.resume_handle[:12]}...")

        try:
            self._client = ClaudeSDKClient(options)
            await self._client.connect()
        except BaseException:
            # On cold-start failure, drive the BOOTING → DEAD transition via
            # BOOT_FAILED so the lifecycle is auditable in logs. Warm-reconnect
            # callers (force_restart etc.) don't enter this branch — their
            # state was RECONNECTING at entry, not UNINITIALIZED.
            if cold_start_token is not None:
                try:
                    await self._state_machine.transition_complete(
                        cold_start_token, SessionState.DEAD,
                        trigger=Trigger.BOOT_FAILED,
                    )
                except Exception as ce:
                    _log(
                        f"streaming[{self.agent_name}]: BOOT_FAILED completion "
                        f"raised after cold-start error: {ce}"
                    )
            raise

        # Land in CONNECTED. Cold-start goes through the matrix
        # (BOOTING → CONNECTED via BOOT_COMPLETE), keeping the cold-start
        # lifecycle a closed BOOT / BOOT_COMPLETE pair in audit logs.
        # Warm-reconnect (force_restart, idle-wake, etc.) still direct-mutates;
        # adding RECONNECT_COMPLETE / RECONNECT_FAILED Trigger symmetry is the
        # PR6.5 follow-up (warm-path Trigger-symmetry; out of scope for PR6).
        if cold_start_token is not None:
            await self._state_machine.transition_complete(
                cold_start_token, SessionState.CONNECTED,
                trigger=Trigger.BOOT_COMPLETE,
            )
        else:
            self._state_machine._state = SessionState.CONNECTED

        # Capture account info from SDK init result
        try:
            server_info = await self._client.get_server_info()
            if server_info and "account" in server_info:
                self.account_info = server_info["account"]
                _log(f"streaming[{self.agent_name}]: account — {self.account_info.get('subscriptionType', 'unknown')} ({self.account_info.get('apiProvider', 'unknown')})")
        except Exception as e:
            _log(f"streaming[{self.agent_name}]: failed to get account info: {e}")

        # Record session start in analytics
        self._analytics_session_started()

        # Start background reader
        self._reader_task = asyncio.create_task(self._reader_loop())

        _log(f"streaming[{self.agent_name}]: connected, reader loop started")

        # Auto-send wake prompt with saved context injected.
        #
        # Assembly moved to ``pinky_daemon.wake_prompt`` (PR for #543) so
        # tmux and Codex can share the same contract. The pure builder is
        # transport-neutral; this transport just delivers the result via
        # ``client.query``.
        #
        # Visible copy change: ``WakeReason.RESUME`` now says "Session
        # resumed." (neutral) instead of the prior "Session resumed after
        # daemon restart." The old wording wasn't safe for tmux warm
        # reconnect or idle-wake. Callers that explicitly know a daemon
        # restart happened should encode it in the context body (e.g. via
        # the restart manifest).
        wake_reason = wake_reason_from_runtime(
            resume_handle=self.resume_handle,
            restart_reason=self._config.restart_reason,
        )
        # #591 — rebuild wake-context body with the freshly-computed
        # wake_reason so the builder can gate the saved-state manifest
        # against the actual wake type (RESUME drops the bulk manifest;
        # CONTEXT_RESTART/AUTO_RESTART/NEW_SESSION emit it). The static
        # ``self._config.wake_context`` set at config-create time
        # predates this signal — kept as a fallback for tests / paths
        # without a builder. Trailing positional kwarg keeps 1-arg
        # callers of older builders working.
        wake_context_body = self._config.wake_context or ""
        if self._config.wake_context_builder:
            try:
                wake_context_body = self._config.wake_context_builder(
                    self.agent_name, wake_reason
                )
            except TypeError:
                # Legacy 1-arg builder — fall back to the pre-built body.
                pass
            except Exception as e:
                _log(
                    f"streaming[{self.agent_name}]: wake context rebuild failed: {e} "
                    "— using stored body"
                )
        wake_prompt = build_wake_prompt(
            WakePromptInput(
                reason=wake_reason,
                context_body=wake_context_body,
                timezone=self._config.timezone or "America/Los_Angeles",
            )
        )
        self._config.restart_reason = ""  # Clear after use.

        # Instrumentation: a single structured log line per wake prompt
        # gives validation tooling a grep-able marker. Tmux emits the
        # same fields via ``_emit_stream_event`` because it has that
        # surface; SDK lacks a stream-event callback today (deferred
        # follow-up — would unify observability across transports).
        _ctx_chars = len(wake_context_body or "")
        _prompt_hash = hashlib.sha256(wake_prompt.encode("utf-8")).hexdigest()[:12]
        _log(
            f"streaming[{self.agent_name}]: wake_prompt_sent "
            f"reason={wake_reason.value} "
            f"context_chars={_ctx_chars} "
            f"context_present={bool(wake_context_body)} "
            f"prompt_hash={_prompt_hash}"
        )

        async def _send_wake_prompt() -> None:
            try:
                await self._client.query(wake_prompt)
                # Keep ResultMessage pops 1:1 with queries: the wake turn
                # has no routing target, so enqueue an unrouted sentinel.
                self._pending_chats.append(("", "", ""))
                _log(
                    f"streaming[{self.agent_name}]: sent wake prompt "
                    f"(reason={wake_reason.value})"
                )
            except Exception as e:
                _log(f"streaming[{self.agent_name}]: wake prompt failed: {e}")
                return  # delivery failed → DO NOT fire on_wake_delivered (#591 P1#2)
            # Wake prompt delivered. Fire the post-delivery callback so
            # the agent_wake activity event is logged (advances the
            # #591 cycle-gate boundary on every successful warm wake,
            # not just cold-start + scheduler — Murzik P1#2).
            if self._config.on_wake_delivered:
                try:
                    self._config.on_wake_delivered(self.agent_name, wake_reason)
                except Exception as _cb_e:
                    _log(
                        f"streaming[{self.agent_name}]: on_wake_delivered "
                        f"callback failed: {_cb_e}"
                    )

        # Do not block daemon startup on the agent's first turn. Wake prompts
        # may immediately use MCP tools that depend on the API listener.
        # Retain a strong reference via ``self._background_tasks`` per the
        # asyncio docs: tasks created here can otherwise be GC'd mid-flight,
        # which would silently drop the wake prompt.
        wake_task = asyncio.create_task(_send_wake_prompt())
        self._background_tasks.add(wake_task)
        wake_task.add_done_callback(self._background_tasks.discard)

    async def send(
        self,
        prompt: str,
        platform: str = "",
        chat_id: str = "",
        message_id: str = "",
        agent_hint: str = "",
    ) -> bool:
        """Send a message to the agent. Non-blocking — returns immediately.

        Args:
            prompt: The formatted message to send.
            platform: The platform the message came from (e.g. 'telegram').
            chat_id: The chat_id to route the response back to.
            message_id: The source message_id to route reactions back to.
            agent_hint: Extra context appended to the query but NOT stored in
                conversation history (e.g. reply-platform hints).

        Returns:
            Per-call handoff bool (#853 P1): ``True`` when ``client.query()``
            accepted THIS message; ``False`` when it was dropped (not
            connected) or the query raised. The exception path is unchanged —
            swallowed, reconnect attempted, no raise — it just reports the
            failed handoff so the broker never treats this exact message as
            consumed (the capability attr alone must not confirm).
        """
        if self.state != SessionState.CONNECTED or not self._client:
            _log(f"streaming[{self.agent_name}]: not connected, dropping message")
            return False

        self.last_active = time.time()
        self._stats["messages_sent"] += 1
        # Extract raw user text for analytics classification (strip broker headers)
        self._last_user_message = self._strip_prompt_headers(prompt)

        # Log to conversation store with platform metadata (clean prompt, no hints)
        if self._conversation_store:
            try:
                self._conversation_store.append(
                    self.id, "user", prompt,
                    platform=platform, chat_id=chat_id,
                )
            except Exception as e:
                _log(f"streaming[{self.agent_name}]: conversation store append failed: {e}")

        try:
            self._analytics_log_activity(
                "prompt_submitted",
                metadata={"platform": platform, "chat_id": chat_id},
            )
            await self._client.query(prompt + agent_hint)
            # Always enqueue one routing entry per query -- even with no
            # chat_id -- so ResultMessage pops stay 1:1 with queries and a
            # system/internal turn can't consume a user turn's routing.
            self._pending_chats.append((platform, chat_id, message_id))
            _log(f"streaming[{self.agent_name}]: sent message (chat={chat_id})")
            return True
        except Exception as e:
            self._stats["errors"] += 1
            _log(f"streaming[{self.agent_name}]: send error: {e}")
            # Try to reconnect
            await self.attempt_reconnect()
            return False

    async def _reader_loop(self) -> None:
        """Background loop that reads responses and fires callbacks."""
        from claude_agent_sdk.types import (
            AssistantMessage,
            AssistantMessageError,
            ResultMessage,
            TextBlock,
            ThinkingBlock,
            ToolResultBlock,
            ToolUseBlock,
        )

        # Defensive invariant: ``_is_auth_error_assistant`` does an exact
        # match against ``AssistantMessageError`` for "authentication_failed".
        # If a future SDK release renames that Literal value, exact-match
        # silently stops detecting credential failures — re-creating the
        # exact regression mode #400 was built to catch. Fail loud at session
        # start instead. (CI also asserts this in test_auth_alerts.py, so
        # bumps caught in PR; this guard catches local-dev SDK upgrades.)
        assert _AUTH_ASSISTANT_ERROR in AssistantMessageError.__args__, (
            f"claude-agent-sdk renamed AssistantMessageError Literal — "
            f"_AUTH_ASSISTANT_ERROR={_AUTH_ASSISTANT_ERROR!r} no longer in "
            f"{AssistantMessageError.__args__}. Update the constant and tests."
        )

        _log(f"streaming[{self.agent_name}]: reader loop running")
        turn_tool_uses = []  # Track tool uses per turn
        turn_thinking: list[str] = []  # Track thinking blocks per turn
        # Per-turn dedupe for auth-failure callbacks. A single failed turn can
        # surface auth errors on BOTH paths: an AssistantMessage with
        # error="authentication_failed", followed by the terminal ResultMessage
        # with api_error_status=401. Without dedupe, AuthFailureTracker would
        # increment twice for one real failure — tripping the operator-alert
        # threshold early and skewing the multi-agent baseline. Reset at the
        # end of ResultMessage handling (turn boundary).
        auth_reported_this_turn = False

        try:
            async for msg in self._client.receive_messages():
                if isinstance(msg, AssistantMessage):
                    # Increment turn counter at the start of each assistant message
                    # so tool calls within this turn get the correct turn_seq.
                    self._turn_seq += 1

                    # If the SDK signals an API-level error (e.g. content filtering,
                    # rate limit, invalid request), skip the content blocks entirely —
                    # the text in them may be raw API error JSON that must never reach
                    # the user's chat.
                    if msg.error:
                        _log(
                            f"streaming[{self.agent_name}]: assistant error={msg.error!r}"
                            f" stop_reason={msg.stop_reason!r} — suppressing content"
                        )
                        # Detect auth failures and notify the operator. The SDK
                        # types ``msg.error`` as the AssistantMessageError
                        # Literal; only "authentication_failed" is a credential
                        # issue. Other Literal values (billing_error, rate_limit,
                        # invalid_request, server_error, unknown) are NOT auth
                        # failures and must not trip the operator alert.
                        if _is_auth_error_assistant(msg) and self._auth_alert_callback:
                            # Set the dedupe flag BEFORE invoking the callback
                            # so a callback exception can't cause the
                            # ResultMessage path to double-fire for the same
                            # turn.
                            auth_reported_this_turn = True
                            try:
                                await self._auth_alert_callback(
                                    self.agent_name, str(msg.error)
                                )
                            except Exception as exc:
                                _log(
                                    f"streaming[{self.agent_name}]: "
                                    f"auth_alert_callback raised: {exc}"
                                )
                        # Don't touch _last_response; fall through to usage/resume_handle capture.
                    else:
                        # Extract text and tool uses from content blocks
                        text_parts = []
                        block_types = [type(b).__name__ for b in msg.content]
                        if any(t != "TextBlock" for t in block_types):
                            _log(f"streaming[{self.agent_name}]: content blocks: {block_types}")
                        for block in msg.content:
                            if isinstance(block, TextBlock):
                                text_parts.append(block.text)
                            elif isinstance(block, ThinkingBlock):
                                if block.thinking:
                                    turn_thinking.append(block.thinking)
                                    self._current_thinking = block.thinking
                            elif isinstance(block, ToolUseBlock):
                                desc = _describe_tool_use(
                                    block.name,
                                    block.input if isinstance(block.input, dict) else {},
                                )
                                self._current_activity = desc
                                self._activity_log.append(desc)
                                tool_call_key = getattr(block, "id", "") or f"{block.name}_{len(turn_tool_uses)}"
                                turn_tool_uses.append({
                                    "tool": block.name,
                                    "input": block.input if isinstance(block.input, dict) else str(block.input)[:200],
                                    "_call_key": tool_call_key,
                                })
                                # Analytics: track tool start
                                tool_ns = ""
                                if "__" in block.name:
                                    parts = block.name.split("__", 2)
                                    if len(parts) >= 3:
                                        tool_ns = parts[1]
                                # Capture arg key names only (no values) — PII-safe
                                arg_keys: list[str] = []
                                if isinstance(block.input, dict):
                                    arg_keys = sorted(block.input.keys())
                                self._analytics_start_tool_call(
                                    tool_call_key=tool_call_key,
                                    tool_name=block.name,
                                    tool_namespace=tool_ns,
                                    metadata={"arg_keys": arg_keys} if arg_keys else None,
                                )
                            elif isinstance(block, ToolResultBlock):
                                # Attach result to the last matching tool use
                                content_str = str(block.content)[:300] if block.content else ""
                                if turn_tool_uses:
                                    turn_tool_uses[-1]["error"] = block.is_error
                                    if content_str:
                                        turn_tool_uses[-1]["result_preview"] = content_str[:200]
                                    # Analytics: track tool finish
                                    call_key = turn_tool_uses[-1].get("_call_key", "")
                                    if call_key:
                                        self._analytics_finish_tool_call(
                                            tool_call_key=call_key,
                                            success=not block.is_error,
                                            error_type="tool_error" if block.is_error else "",
                                        )
                        text = "\n".join(text_parts)
                        if text:
                            self._last_response = text

                    # Track usage
                    if msg.usage:
                        self.usage.input_tokens += msg.usage.get("input_tokens", 0)
                        self.usage.output_tokens += msg.usage.get("output_tokens", 0)

                    # Capture SDK resume handle for persistence
                    if msg.session_id and msg.session_id != self.resume_handle:
                        self.resume_handle = msg.session_id
                        _log(f"streaming[{self.agent_name}]: captured resume_handle {self.resume_handle[:12]}")
                        if self._on_resume_handle:
                            try:
                                await self._on_resume_handle(self.agent_name, self.resume_handle)
                            except Exception:
                                pass

                elif isinstance(msg, ResultMessage):
                    # Debug: log result message details
                    if msg.num_turns and msg.num_turns > 0:
                        _log(f"streaming[{self.agent_name}]: result — turns={msg.num_turns}, cost=${msg.total_cost_usd or 0:.4f}, model_usage={msg.model_usage}")

                    # Get the routing info for this response
                    if self._pending_chats:
                        resp_platform, resp_chat_id, resp_message_id = self._pending_chats.pop(0)
                    else:
                        resp_platform, resp_chat_id, resp_message_id = ("", "", "")

                    # If the SDK reports an error result, discard _last_response — it may
                    # contain raw API error JSON (e.g. content filter, rate limit) that must
                    # never be forwarded to the user's chat.
                    if msg.is_error:
                        _log(
                            f"streaming[{self.agent_name}]: error result"
                            f" stop_reason={msg.stop_reason!r}"
                            f" api_error_status={getattr(msg, 'api_error_status', None)!r}"
                            f" errors={msg.errors!r} — suppressing forwarded response"
                        )
                        # Detect credential failures on the result path too. The
                        # AssistantMessage path catches errors the SDK surfaces
                        # mid-turn; this path catches errors that only land at
                        # turn completion (api_error_status added in 0.1.76:
                        # 401/403 = bad creds; 429/5xx = transient, handled
                        # below as a generic error result).
                        #
                        # Skip if the AssistantMessage path already reported
                        # auth for this turn — both paths firing for one real
                        # failure double-counts in AuthFailureTracker.
                        if (
                            _is_auth_error_result(msg)
                            and self._auth_alert_callback
                            and not auth_reported_this_turn
                        ):
                            auth_reported_this_turn = True
                            # Surface msg.errors into the callback string when
                            # present — operators get richer triage context
                            # than the raw status code alone (the SDK started
                            # returning actionable messages in 0.1.77).
                            err_detail = f"api_error_status={msg.api_error_status}"
                            if msg.errors:
                                err_detail += f" errors={msg.errors!r}"
                            try:
                                await self._auth_alert_callback(
                                    self.agent_name,
                                    err_detail,
                                )
                            except Exception as exc:
                                _log(
                                    f"streaming[{self.agent_name}]: "
                                    f"auth_alert_callback (result) raised: {exc}"
                                )
                        # Analytics: still record errored turns
                        _u = msg.usage or {}
                        self._analytics_log_turn_usage(
                            input_tokens=(
                                _u.get("input_tokens", 0)
                                or _u.get("inputTokens", 0)
                            ),
                            output_tokens=(
                                _u.get("output_tokens", 0)
                                or _u.get("outputTokens", 0)
                            ),
                            cached_input_tokens=(
                                _u.get("cache_read_input_tokens", 0)
                                or _u.get("cached_input_tokens", 0)
                                or _u.get("cacheReadInputTokens", 0)
                            ),
                            error=True,
                        )
                        self._analytics_log_activity(
                            "turn_error",
                            metadata={
                                "stop_reason": msg.stop_reason or "",
                                "errors": str(msg.errors or "")[:200],
                            },
                        )
                        self._stamp_last_seen()
                        # Fire the response callback with EMPTY text for routed
                        # turns so downstream bookkeeping (typing indicator
                        # stop in broker.route_response) still runs. The
                        # suppressed content is never forwarded -- route_response
                        # no-ops on empty text.
                        if self._response_callback and resp_chat_id:
                            try:
                                await self._response_callback(TurnResponse(
                                    agent_name=self.agent_name,
                                    session_id=self.id,
                                    platform=resp_platform,
                                    chat_id=resp_chat_id,
                                    message_id=resp_message_id,
                                    text="",
                                    tool_uses=list(turn_tool_uses),
                                    used_outreach_tools=any(
                                        _is_outreach_tool(tool_use.get("tool", ""))
                                        for tool_use in turn_tool_uses
                                    ),
                                    usage=msg.usage or {},
                                    num_turns=msg.num_turns or 0,
                                ))
                            except Exception as e:
                                _log(f"streaming[{self.agent_name}]: callback error: {e}")
                        self._last_response = ""
                        self._current_activity = ""
                        self._activity_log = []
                        self._current_thinking = ""
                        turn_tool_uses = []
                        turn_thinking = []
                        self._stats["turns"] += 1
                        self.last_active = time.time()
                        self._turn_done.set()
                        # Reset per-turn auth dedupe — turn boundary
                        auth_reported_this_turn = False
                        if (
                            self.state == SessionState.CONNECTED
                            and not self._pending_chats
                        ):
                            _notify_turn_idle(self._config, self.agent_name)
                        continue

                    # Turn complete — fire response callback
                    turn_result = TurnResponse(
                        agent_name=self.agent_name,
                        session_id=self.id,
                        platform=resp_platform,
                        chat_id=resp_chat_id,
                        message_id=resp_message_id,
                        text=self._last_response,
                        tool_uses=list(turn_tool_uses),
                        used_outreach_tools=any(
                            _is_outreach_tool(tool_use.get("tool", ""))
                            for tool_use in turn_tool_uses
                        ),
                        usage=msg.usage or {},
                        total_cost_usd=msg.total_cost_usd or 0.0,
                        num_turns=msg.num_turns or 0,
                        model_usage=msg.model_usage or {},
                    )

                    # Fire whenever there's content OR a routing target: a
                    # routed turn with no text/tools must still reach
                    # broker.route_response so the typing indicator stops.
                    if self._response_callback and (
                        turn_result.response_text or turn_result.tool_uses or resp_chat_id
                    ):
                        try:
                            await self._response_callback(turn_result)
                        except Exception as e:
                            _log(f"streaming[{self.agent_name}]: callback error: {e}")

                    # A successful (non-errored) turn proves Claude auth is
                    # working again — clear any auth-fail tracking for this
                    # agent so the next outage emits a fresh alert.
                    if self._auth_success_callback:
                        try:
                            self._auth_success_callback(self.agent_name)
                        except Exception:
                            pass

                    # Track usage from result
                    if msg.total_cost_usd:
                        self.usage.total_cost_usd += msg.total_cost_usd
                        # Persist cost to DB for lifetime tracking
                        if self._cost_callback:
                            try:
                                self._cost_callback(
                                    self.agent_name, msg.total_cost_usd,
                                    msg.usage.get("input_tokens", 0) if msg.usage else 0,
                                    msg.usage.get("output_tokens", 0) if msg.usage else 0,
                                    self.resume_handle or "",
                                )
                            except Exception as e:
                                _log(f"streaming[{self.agent_name}]: cost callback error: {e}")
                    if msg.usage:
                        self.usage.last_usage = msg.usage

                    # Analytics: log aggregated turn usage
                    # Claude Agent SDK returns camelCase keys in model_usage
                    # (inputTokens, outputTokens, cacheReadInputTokens) while
                    # the Anthropic API uses snake_case — handle both.
                    agg_input = 0
                    agg_output = 0
                    agg_cached = 0
                    if msg.model_usage:
                        for _model_name, mu in msg.model_usage.items():
                            agg_input += (
                                mu.get("input_tokens", 0)
                                or mu.get("inputTokens", 0)
                            )
                            agg_output += (
                                mu.get("output_tokens", 0)
                                or mu.get("outputTokens", 0)
                            )
                            agg_cached += (
                                mu.get("cache_read_input_tokens", 0)
                                or mu.get("cacheReadInputTokens", 0)
                            )
                    elif msg.usage:
                        agg_input = (
                            msg.usage.get("input_tokens", 0)
                            or msg.usage.get("inputTokens", 0)
                        )
                        agg_output = (
                            msg.usage.get("output_tokens", 0)
                            or msg.usage.get("outputTokens", 0)
                        )
                        agg_cached = (
                            msg.usage.get("cache_read_input_tokens", 0)
                            or msg.usage.get("cached_input_tokens", 0)
                            or msg.usage.get("cacheReadInputTokens", 0)
                        )
                    if agg_input or agg_output or agg_cached:
                        self._analytics_log_turn_usage(
                            input_tokens=agg_input,
                            output_tokens=agg_output,
                            cached_input_tokens=agg_cached,
                            error=bool(msg.is_error),
                        )

                    # Analytics: turn lifecycle activity
                    if msg.is_error:
                        self._analytics_log_activity(
                            "turn_error",
                            metadata={
                                "stop_reason": msg.stop_reason or "",
                                "errors": str(msg.errors or "")[:200],
                            },
                        )
                    else:
                        self._analytics_log_activity(
                            "turn_completed",
                            metadata={
                                "num_turns": msg.num_turns or 0,
                                "cost_usd": msg.total_cost_usd or 0,
                                "tool_count": len(turn_tool_uses),
                                "input_tokens": agg_input,
                                "output_tokens": agg_output,
                                "cached_input_tokens": agg_cached,
                            },
                        )
                    # Stamp on any turn end (complete or error) — proves pipe is live
                    self._stamp_last_seen()

                    # Log assistant response to conversation store with metadata
                    if self._last_response and self._conversation_store:
                        try:
                            metadata = {}
                            if turn_tool_uses:
                                metadata["tool_uses"] = turn_tool_uses
                            if turn_thinking:
                                metadata["thinking"] = turn_thinking
                            if msg.model_usage:
                                metadata["model_usage"] = msg.model_usage
                            if msg.total_cost_usd:
                                metadata["cost_usd"] = msg.total_cost_usd
                            if msg.num_turns:
                                metadata["num_turns"] = msg.num_turns
                            if metadata:
                                _log(f"streaming[{self.agent_name}]: saving metadata: {list(metadata.keys())}, tools={len(turn_tool_uses)}")
                            self._conversation_store.append(
                                self.id, "assistant", self._last_response,
                                platform=resp_platform, chat_id=resp_chat_id,
                                metadata=metadata if metadata else None,
                            )
                        except Exception as e:
                            _log(f"streaming[{self.agent_name}]: failed to save to conversation store: {e}")

                    self._last_response = ""
                    self._current_activity = ""
                    self._activity_log = []
                    self._current_thinking = ""
                    turn_tool_uses = []  # Reset for next turn
                    turn_thinking = []  # Reset for next turn
                    self._stats["turns"] += 1
                    self.last_active = time.time()
                    self._turn_done.set()
                    # Reset per-turn auth dedupe — turn boundary. Successful
                    # turns rarely set this flag (no auth error fired), but
                    # reset unconditionally to keep the invariant simple.
                    auth_reported_this_turn = False

                    _log(f"streaming[{self.agent_name}]: turn complete (total: {self._stats['turns']})")

                    # Check context usage for auto-restart
                    await self._check_context()
                    if (
                        self.state == SessionState.CONNECTED
                        and not self._pending_chats
                    ):
                        _notify_turn_idle(self._config, self.agent_name)

        except Exception as e:
            _log(f"streaming[{self.agent_name}]: reader loop error: {e}")
            # Recoverable transport loss — drive to RECONNECTING so observers
            # see the intent immediately (matches the broker's wait-for-reconnect
            # pattern from PR #484). attempt_reconnect drives the retry loop and
            # settles to CONNECTED or DEAD.
            self._state_machine._state = SessionState.RECONNECTING
            await self.attempt_reconnect()

    async def _check_context(self) -> None:
        """Check context usage after each turn. Warn or force restart."""
        if not self._client or self.state != SessionState.CONNECTED:
            return

        try:
            ctx = await self._client.get_context_usage()
            total = ctx.get("totalTokens", 0)
            reported_max = ctx.get("maxTokens", 0)

            # Single source of truth for the window: trust the harness-reported
            # cap (reported_max), falling back to the configurable per-model map.
            max_t = resolve_context_window(
                self._config.model or "", reported_max=reported_max
            )

            pct = round(total / max_t * 100) if max_t > 0 else 0

            # Warn branch — independent of restart branch so a single-turn overshoot
            # (e.g. 35% → 85% from a big tool result) still gets a heads-up before the
            # forced restart fires. The warn flag is set only after a successful query
            # so a transient failure retries on the next turn instead of being lost.
            if pct >= self._config.context_warn_pct and not self._context_warned:
                remaining = max_t - total
                warn_msg = (
                    f"[SYSTEM] Context at {pct}% ({total:,}/{max_t:,} tokens). "
                    f"~{remaining:,} tokens remaining. "
                    f"Save your state with save_my_context before hitting {self._config.context_restart_pct}%, "
                    f"or call context_restart when ready."
                )
                try:
                    await self._client.query(warn_msg)
                    # Unrouted system turn -- keep ResultMessage pops 1:1
                    self._pending_chats.append(("", "", ""))
                    self._context_warned = True
                    _log(f"streaming[{self.agent_name}]: warned agent at {pct}% context")
                except Exception as e:
                    _log(
                        f"streaming[{self.agent_name}]: warn query failed at {pct}%: {e} "
                        f"— will retry next turn"
                    )

            if pct >= self._config.context_restart_pct:
                # Force restart
                _log(f"streaming[{self.agent_name}]: context at {pct}% — force restarting")
                restarted = await self.force_restart()
                if restarted:
                    self._stats["auto_restarts"] += 1

        except Exception as e:
            _log(f"streaming[{self.agent_name}]: context check failed: {e}")

    async def _notify_restart_blocked(self, guard: dict) -> None:
        """Tell the agent why restart is blocked, with basic rate limiting."""
        if not self._client:
            return

        now = time.time()
        cooldown = max(int(getattr(self._config, "restart_guard_cooldown_sec", 60) or 60), 1)
        if (now - self._last_restart_block_notice_at) < cooldown:
            return

        self._last_restart_block_notice_at = now
        self._stats["restart_blocks"] = self._stats.get("restart_blocks", 0) + 1

        detail = guard.get("message") or (
            "Context restart is blocked until you call save_my_context() from this session."
        )
        warn_msg = (
            "[SYSTEM] Context restart blocked. "
            f"{detail} Use save_my_context() now, then retry once you've saved your latest work."
        )
        try:
            await self._client.query(warn_msg)
            # Unrouted system turn -- keep ResultMessage pops 1:1
            self._pending_chats.append(("", "", ""))
        except Exception:
            pass

    async def force_restart(self) -> bool:
        """Force a context restart — disconnect, clear session, reconnect fresh."""
        if self._config.restart_guard:
            try:
                guard = self._config.restart_guard(self)
            except Exception as e:
                _log(f"streaming[{self.agent_name}]: restart guard failed: {e}")
                guard = {}
            if guard and not guard.get("restart_safe", False):
                _log(
                    f"streaming[{self.agent_name}]: restart blocked: "
                    f"{guard.get('reason', 'missing save')}"
                )
                await self._notify_restart_blocked(guard)
                return False

        _log(f"streaming[{self.agent_name}]: force restarting session")

        # Settle macro state in RECONNECTING for the full restart window —
        # disconnect → wake-context refresh → connect. Without this,
        # ``disconnect()``'s no-prior-intent fallback would drive
        # CONNECTED → DEAD and observers (broker auto-wake, watchdog
        # resurrection) would see DEAD mid-restart and race the in-flight
        # force_restart. Per @murzik on PR #491 review.
        self._state_machine._state = SessionState.RECONNECTING

        # Notify the persistence callback to clear the resume handle
        if self._on_resume_handle:
            try:
                await self._on_resume_handle(self.agent_name, "")
            except Exception:
                pass

        # Disconnect
        await self.disconnect()
        # Re-assert RECONNECTING after the teardown. ``disconnect()``'s
        # fallback only fires from CONNECTED, so it shouldn't trip here —
        # but defensive: if a future change adds another path that flips
        # state inside disconnect, we still observe RECONNECTING during
        # wake-context refresh and at connect() entry.
        self._state_machine._state = SessionState.RECONNECTING

        # #591 P1#1 (Murzik round-2): the prior eager refresh here ran
        # the builder 1-arg (commit=True), consuming restart-manifest BEFORE
        # connect() ran its own reason-aware committed rebuild.
        # Removed entirely: connect() is now the single source-of-truth
        # for both the wake_context body and side-effect consumption.

        # Reconnect fresh with wake context
        self._config.resume_handle = ""
        if not self._config.restart_reason:
            self._config.restart_reason = "auto_restart"
        self.resume_handle = ""
        self._context_warned = False

        try:
            await self.connect()
            _log(f"streaming[{self.agent_name}]: force restart complete — fresh session")
            return True
        except Exception as e:
            _log(f"streaming[{self.agent_name}]: force restart failed: {e}")
            # Connect raised mid-restart — terminal failure. DEAD is the
            # universal emergency-exit sink per transport_state.py docstring.
            # The watchdog's resurrection path (api._heartbeat_resurrect) can
            # drive DEAD → RECONNECTING via BROKER/SCHEDULER on the next
            # inbound message.
            self._state_machine._state = SessionState.DEAD
            return False

    async def idle_sleep(self) -> bool:
        """Put the session to sleep due to inactivity.

        Asks the agent to save memories, then disconnects. Session ID is
        preserved so the next wake can resume.
        Returns True if successfully slept.
        """
        if self.state != SessionState.CONNECTED or not self._client:
            return False

        _log(f"streaming[{self.agent_name}]: idle sleep triggered ({self._config.idle_timeout}s idle)")

        # Ask agent to save state before sleeping. Text moved to
        # ``pinky_daemon.wake_prompt.build_idle_sleep_prompt`` (PR for
        # #543 / idle-sleep parity) so tmux can use the same instruction
        # via its internal-prompt mechanism with explicit
        # wait_for_completion semantics.
        sends_before = self._stats["messages_sent"]
        try:
            self._turn_done.clear()
            await self._client.query(build_idle_sleep_prompt())
            # Unrouted system turn -- keep ResultMessage pops 1:1
            self._pending_chats.append(("", "", ""))
            _log(f"streaming[{self.agent_name}]: memory save prompt sent before idle sleep")
            # query() returns as soon as the prompt hits the transport; it
            # does NOT wait for the turn. Give the save turn a bounded
            # window to complete before tearing the session down --
            # mirrors tmux_session's wait_for_completion semantics.
            if self._reader_task and not self._reader_task.done():
                try:
                    await asyncio.wait_for(
                        self._turn_done.wait(),
                        timeout=self._IDLE_SLEEP_SAVE_TIMEOUT_SEC,
                    )
                    _log(f"streaming[{self.agent_name}]: memory save turn completed")
                except asyncio.TimeoutError:
                    _log(
                        f"streaming[{self.agent_name}]: memory save turn did not "
                        f"complete within {self._IDLE_SLEEP_SAVE_TIMEOUT_SEC:g}s -- "
                        f"sleeping anyway"
                    )
        except Exception as e:
            _log(f"streaming[{self.agent_name}]: memory save failed before idle sleep: {e}")

        # A message accepted during the save window would have its query
        # killed by the disconnect below. Abort the sleep instead and let
        # the new traffic run. The send counter is the signal -- last_active
        # also advances when the save turn itself completes, so it can't
        # distinguish new inbound messages from the save finishing.
        if self._stats["messages_sent"] != sends_before:
            _log(
                f"streaming[{self.agent_name}]: new message arrived during "
                f"pre-sleep save window -- aborting idle sleep"
            )
            return False

        # Set IDLE_SLEEPING state BEFORE the disconnect side effect, so
        # ``disconnect()``'s "from CONNECTED → DEAD" fallback (for callers
        # that didn't declare intent) doesn't override the idle-sleep intent.
        # This matches the state machine's grant-time-mutation invariant
        # (transport_state.py §6): observers see "we're idle-sleeping" as
        # soon as the intent is declared, not after disconnect completes.
        # The watchdog's #348 resurrection-skip check reads
        # ``state == IDLE_SLEEPING`` directly off this mutation.
        self._state_machine._state = SessionState.IDLE_SLEEPING
        # Disconnect but preserve session ID for resume
        await self.disconnect()
        self._stats["auto_restarts"] += 1
        _log(f"streaming[{self.agent_name}]: idle sleep complete — session preserved for resume")
        return True

    # Reconnect backoff schedule (seconds). Each entry is the wait *before* an attempt.
    # First attempt waits 2s (preserves prior behavior), then escalates to 8s and 30s.
    _RECONNECT_BACKOFF = (2, 8, 30)

    # Bounded wait for the memory-save turn to complete before idle_sleep()
    # tears the transport down. See idle_sleep().
    _IDLE_SLEEP_SAVE_TIMEOUT_SEC = 60.0

    async def attempt_reconnect(self) -> None:
        """Attempt to reconnect after a failure with bounded retries.

        Tries up to len(_RECONNECT_BACKOFF) times with escalating delays. If
        all attempts fail the session settles in DEAD (the universal emergency
        exit per transport_state.py docstring) and the scheduler's heartbeat
        watchdog is responsible for any further resurrection — see
        scheduler._check_heartbeats and the heartbeat_callback wiring in
        api.py (BROKER / SCHEDULER triggers drive DEAD → RECONNECTING).
        Public method: callable from inside the reader loop (transient
        transport failure) and from the watchdog callback.

        Concurrent callers (a transport drop typically surfaces in send()
        AND the reader loop, plus the watchdog can fire during the backoff
        window) are coalesced onto a single in-flight reconnect task. Two
        interleaved disconnect/connect cycles would each construct an SDK
        client and reader task, with the later assignment orphaning the
        first live subprocess. Running the cycle in its own task also keeps
        it alive when the initiating caller (e.g. the old reader task,
        cancelled by disconnect()) dies mid-reconnect.
        """
        task = self._reconnect_task
        if task is not None and not task.done():
            _log(
                f"streaming[{self.agent_name}]: reconnect already in flight -- "
                f"awaiting the existing attempt"
            )
        else:
            task = asyncio.create_task(self._reconnect_with_backoff())
            self._reconnect_task = task
            task.add_done_callback(self._clear_reconnect_task)
        await asyncio.wait_for(task, timeout=None)

    def _clear_reconnect_task(self, task: asyncio.Task) -> None:
        if self._reconnect_task is task:
            self._reconnect_task = None

    async def _reconnect_with_backoff(self) -> None:
        """Single warm-reconnect cycle: disconnect, then bounded retries."""
        # Settle the macro state: RECONNECTING for the duration of all retry
        # attempts (transport_state.py §5 — no flicker DEAD ↔ RECONNECTING
        # between attempts). The reader-loop exception path already drove us
        # here; we re-assert idempotently for callers that bypassed that path
        # (e.g. session_watchdog calling attempt_reconnect directly).
        self._state_machine._state = SessionState.RECONNECTING

        # Disconnect once up front so we start each attempt from a clean state.
        try:
            await self.disconnect()
        except Exception as e:
            _log(f"streaming[{self.agent_name}]: pre-reconnect disconnect raised: {e}")
        # disconnect()'s no-prior-intent fallback would normally drive
        # CONNECTED → DEAD; re-assert RECONNECTING after teardown so the
        # state reflects the in-flight retry, not a terminal failure.
        self._state_machine._state = SessionState.RECONNECTING

        last_error: Exception | None = None
        for attempt_idx, delay in enumerate(self._RECONNECT_BACKOFF, start=1):
            self._stats["reconnects"] += 1
            _log(
                f"streaming[{self.agent_name}]: reconnect attempt {attempt_idx}/"
                f"{len(self._RECONNECT_BACKOFF)} (#{self._stats['reconnects']} total) "
                f"after {delay}s backoff"
            )
            await asyncio.sleep(delay)
            try:
                await self.connect()
                _log(f"streaming[{self.agent_name}]: reconnected successfully")
                return
            except Exception as e:
                last_error = e
                _log(
                    f"streaming[{self.agent_name}]: reconnect attempt {attempt_idx} "
                    f"failed: {e}"
                )
                # Make sure we tear down any partial state before the next try.
                try:
                    await self.disconnect()
                except Exception:
                    pass
                # Re-assert RECONNECTING after the inner disconnect. ``connect()``
                # flips state to CONNECTED before its post-connect setup
                # (analytics session-started, reader-loop spawn); a raise during
                # setup leaves us briefly in CONNECTED, then the inner
                # ``disconnect()`` above fires the standalone-from-CONNECTED →
                # DEAD fallback. Without this re-assert the macro-state flickers
                # to DEAD between retries — contradicts the "no flicker
                # DEAD↔RECONNECTING" invariant from transport_state.py §5.
                # Per @pushok on PR #491 review (Bug 2).
                self._state_machine._state = SessionState.RECONNECTING

        # All retries exhausted — settle in DEAD. Watchdog resurrection on
        # the next inbound message can drive DEAD → RECONNECTING via BROKER
        # (broker auto-wake) or SCHEDULER (heartbeat resurrect).
        self._state_machine._state = SessionState.DEAD
        _log(
            f"streaming[{self.agent_name}]: all {len(self._RECONNECT_BACKOFF)} reconnect "
            f"attempts failed (last error: {last_error}); session settled in DEAD — "
            f"awaiting watchdog resurrection"
        )

    async def disconnect(self) -> None:
        """Tear down the SDK client. Side-effect runner per Transport docstring.

        Does NOT touch the state machine UNLESS the caller has not declared
        a higher-level intent (no in-flight transition AND state is CONNECTED).
        In that case ``disconnect()`` drives CONNECTED → DEAD as the default
        terminal shutdown — matches the pre-state-machine semantics for
        external callers that just call ``disconnect()`` without setting up
        a lifecycle intent.

        Callers with intent (``idle_sleep`` driving → IDLE_SLEEPING,
        ``force_restart`` / ``attempt_reconnect`` driving → RECONNECTING) set
        the target state BEFORE invoking ``disconnect()``, so this
        no-prior-intent fallback doesn't fire.
        """
        if (
            self._state_machine.state == SessionState.CONNECTED
            and self._state_machine._in_flight is None
        ):
            # Standalone disconnect with no caller-declared intent → terminal.
            self._state_machine._state = SessionState.DEAD
        self._analytics_session_ended()
        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass
        if self._client:
            try:
                await self._client.disconnect()
            except Exception:
                pass
            self._client = None
        # Routing entries for turns that will never complete are stale; a
        # reconnected session must not deliver its first responses (e.g. the
        # wake-prompt turn) to a leftover chat_id from before the teardown.
        # Fire the response callback with empty text for each routed entry
        # first: broker.route_response is the only place the typing
        # indicator stops, and a turn that dies in this teardown would
        # otherwise leave the typing task spinning against the platform API.
        stale_routes = [entry for entry in self._pending_chats if entry[1]]
        self._pending_chats.clear()
        if self._response_callback:
            for platform, chat_id, message_id in stale_routes:
                try:
                    await self._response_callback(TurnResponse(
                        agent_name=self.agent_name,
                        session_id=self.id,
                        platform=platform,
                        chat_id=chat_id,
                        message_id=message_id,
                        text="",
                    ))
                except Exception as e:
                    _log(f"streaming[{self.agent_name}]: stale-route callback error: {e}")
        _log(f"streaming[{self.agent_name}]: disconnected")

    # ── Analytics helpers ─────────────────────────────────

    def _analytics_session_started(self) -> None:
        if not self._analytics_store:
            return
        try:
            self._analytics_store.ensure_session_fact(
                session_id=self.id,
                agent_name=self.agent_name,
                session_label=self._config.label or "main",
                provider=self.account_info.get("apiProvider", "anthropic"),
                model=self._config.model or "",
            )
            self._analytics_log_activity("session_start")
        except Exception as e:
            _log(f"streaming[{self.agent_name}]: analytics session start failed: {e}")

    @staticmethod
    def _strip_prompt_headers(prompt: str) -> str:
        """Extract raw user text from a broker-formatted prompt.

        Strips the [platform | dm | sender | ...] header line and attachment
        metadata, returning just the user's message content.
        """
        lines = prompt.split("\n")
        body_lines = []
        for line in lines:
            # Skip broker header lines like [telegram | dm | ...]
            if line.startswith("[") and "|" in line and line.rstrip().endswith("]"):
                continue
            # Skip attachment lines
            if line.startswith("📎 Attachments:"):
                continue
            # Skip reply hints
            if line.startswith("💬 Reply on"):
                continue
            body_lines.append(line)
        return "\n".join(body_lines).strip()

    def _analytics_session_ended(self) -> None:
        if not self._analytics_store:
            return
        try:
            self._analytics_store.mark_session_ended(self.id)
            self._analytics_log_activity("session_end")
        except Exception as e:
            _log(f"streaming[{self.agent_name}]: analytics session end failed: {e}")

    def _analytics_log_activity(
        self, event_type: str, *, metadata: dict | None = None
    ) -> None:
        if not self._analytics_store:
            return
        try:
            self._analytics_store.log_activity(
                session_id=self.id,
                agent_name=self.agent_name,
                event_type=event_type,
                turn_seq=self._turn_seq or None,
                metadata=metadata,
            )
        except Exception as e:
            _log(f"streaming[{self.agent_name}]: analytics activity failed: {e}")

    def _stamp_last_seen(self) -> None:
        """Server-side presence: stamp agent last_seen_at (agent-agnostic)."""
        if not self._registry:
            return
        try:
            self._registry.stamp_last_seen(self.agent_name)
        except Exception as e:
            _log(f"streaming[{self.agent_name}]: stamp_last_seen failed: {e}")

    def _analytics_log_turn_usage(
        self,
        *,
        input_tokens: int,
        output_tokens: int,
        cached_input_tokens: int,
        error: bool,
    ) -> None:
        if not self._analytics_store or not self._turn_seq:
            return
        try:
            self._analytics_store.log_turn_usage(
                session_id=self.id,
                agent_name=self.agent_name,
                turn_seq=self._turn_seq,
                provider=self.account_info.get("apiProvider", "anthropic"),
                model=self._config.model or "",
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cached_input_tokens=cached_input_tokens,
                error=error,
                user_message_snippet=self._last_user_message,
            )
        except Exception as e:
            _log(f"streaming[{self.agent_name}]: analytics usage failed: {e}")

    def _analytics_start_tool_call(
        self,
        *,
        tool_call_key: str,
        tool_name: str,
        tool_namespace: str = "",
        metadata: dict | None = None,
    ) -> None:
        if not self._analytics_store or not tool_name:
            return
        try:
            self._analytics_store.start_tool_call(
                session_id=self.id,
                agent_name=self.agent_name,
                turn_seq=self._turn_seq or None,
                tool_call_key=tool_call_key,
                tool_name=tool_name,
                tool_namespace=tool_namespace,
                metadata=metadata,
            )
            self._analytics_log_activity(
                "tool_started",
                metadata={
                    "tool_name": tool_name,
                    "tool_namespace": tool_namespace,
                    **(metadata or {}),
                },
            )
        except Exception as e:
            _log(f"streaming[{self.agent_name}]: analytics tool start failed: {e}")

    def _analytics_finish_tool_call(
        self,
        *,
        tool_call_key: str,
        success: bool,
        error_type: str = "",
        metadata: dict | None = None,
    ) -> None:
        if not self._analytics_store or not tool_call_key:
            return
        try:
            self._analytics_store.finish_tool_call(
                session_id=self.id,
                agent_name=self.agent_name,
                tool_call_key=tool_call_key,
                success=success,
                error_type=error_type,
                metadata=metadata,
            )
            self._analytics_log_activity(
                "tool_finished",
                metadata={
                    "tool_call_key": tool_call_key,
                    "success": success,
                    **(metadata or {}),
                },
            )
        except Exception as e:
            _log(f"streaming[{self.agent_name}]: analytics tool finish failed: {e}")

    @property
    def effective_effort(self) -> str:
        """Current thinking effort: session override > config default."""
        return self._effort_override or self._config.thinking_effort or "medium"

    def set_effort(self, level: str) -> None:
        """Set session-level thinking effort override."""
        if level not in (*CLI_EFFORT_LEVELS, "ultracode"):
            raise ValueError(f"Invalid effort level: {level}")
        self._effort_override = level
        _log(f"streaming[{self.agent_name}]: effort set to {level}")

    def clear_effort_override(self) -> None:
        """Clear session override, revert to agent default."""
        self._effort_override = None
        _log(f"streaming[{self.agent_name}]: effort override cleared")

    @property
    def state(self) -> SessionState:
        """Current lifecycle state. Single source of truth for the four
        external readers (broker, api, scheduler, watchdog) post-PR4. The
        legacy ``is_connected`` / ``is_idle_sleeping`` shim properties were
        deleted in PR4 of #486 — readers branch on this directly now.
        """
        return self._state_machine.state

    @property
    def stats(self) -> dict:
        state = self._state_machine.state
        return {
            **self._stats,
            "connected": state == SessionState.CONNECTED,
            "idle_sleeping": state == SessionState.IDLE_SLEEPING,
            # PR3 exposes the state-machine value alongside the legacy bools
            # so dashboards / debug tools can read the explicit five-state
            # enum without waiting for PR4's reader migration. Stringified
            # for JSON compatibility.
            "state": state.value,
            # Wall-clock epoch the current state was entered (grant time). Lets
            # the watchdog age stuck transitions precisely instead of sampling
            # (#206).
            "state_entered_at": self._state_machine.state_entered_at,
            "pending_responses": len(self._pending_chats),
            "current_activity": self._current_activity,
            "current_thinking": self._current_thinking,
            "activity_log": list(self._activity_log),
            "cost_usd": round(self.usage.total_cost_usd, 6),
            "account": self.account_info,
            "thinking_effort": self.effective_effort,
        }

    @property
    def id(self) -> str:
        return f"{self.agent_name}-{self._config.label or 'main'}"
