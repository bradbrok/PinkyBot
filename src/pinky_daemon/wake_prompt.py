"""Transport-neutral wake-prompt assembly.

Wake prompts orient an agent at session start / resume / restart by
combining a header line, the current time, saved-state context body,
session-purpose instruction, and an explicit messaging/tool reminder.

The assembly contract is shared across all transports (SDK, tmux, Codex).
Each transport decides HOW to deliver the prompt (background
``client.query``, paste into a tmux pane, ``codex exec`` input) but the
WHAT is assembled here so the contract doesn't drift per backend.

**Two distinct contracts, easy to confuse:**

- ``WakeReason`` controls PROMPT TEXT only — header line + first-sentence
  instruction.
- Whether a transport launches with ``--continue`` vs fresh-context is a
  *separate* contract: ``force_fresh_context_once`` on the transport's
  config. The two move together in the common case (a context_restart
  sets both), but keeping them coupled was a known source of bugs
  (tmux ``_build_claude_cmd`` always added ``--continue`` regardless of
  ``restart_reason``, so a "context_restart" tmux launch would silently
  resume the old transcript).

The builder is pure: no I/O, no MCP, no broker. ``context_body`` is the
already-assembled context string from the upstream context collector
(e.g. ``api._build_streaming_wake_context``) which has the registry /
broker dependencies and is responsible for saved state, restart manifest,
freshness warnings, active-channel preamble, inbox/task previews, etc.
"""

from __future__ import annotations

import zoneinfo
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone as _utc_timezone
from enum import Enum

__all__ = [
    "WakeReason",
    "WakePromptInput",
    "build_wake_prompt",
    "wake_reason_from_runtime",
    "DEFAULT_TIMEZONE",
]


def wake_reason_from_runtime(
    *,
    resume_handle: str,
    restart_reason: str,
) -> "WakeReason":
    """Map the legacy ``(resume_handle, restart_reason)`` pair to a
    :class:`WakeReason`.

    Helper for transports migrating off the old inline assembly. Mirrors
    the SDK ``connect()`` decision tree:

    - ``resume_handle`` non-empty → RESUME
    - ``restart_reason == "context_restart"`` → CONTEXT_RESTART
    - ``restart_reason == "auto_restart"`` → AUTO_RESTART
    - otherwise → NEW_SESSION

    Note: ``WakeReason.IDLE_WAKE`` is NOT produced by this helper because
    the old string-typed ``restart_reason`` had no value for it. Callers
    that want IDLE_WAKE must build :class:`WakePromptInput` directly.
    """
    if resume_handle:
        return WakeReason.RESUME
    if restart_reason == "context_restart":
        return WakeReason.CONTEXT_RESTART
    if restart_reason == "auto_restart":
        return WakeReason.AUTO_RESTART
    return WakeReason.NEW_SESSION


DEFAULT_TIMEZONE = "America/Los_Angeles"


class WakeReason(str, Enum):
    """Why the agent is being oriented at session start.

    Maps to header + instruction copy in the wake prompt. Callers decide
    which value applies based on runtime facts (daemon resume vs context
    restart vs auto restart vs fresh new session vs wake-after-idle-sleep).

    Critical: this enum controls PROMPT TEXT only. Launch behavior
    (``--continue`` suppression) is a separate contract; see
    ``force_fresh_context_once`` on ``StreamingSessionConfig``.
    """

    NEW_SESSION = "new_session"
    RESUME = "resume"
    CONTEXT_RESTART = "context_restart"
    AUTO_RESTART = "auto_restart"
    IDLE_WAKE = "idle_wake"


@dataclass(frozen=True)
class WakePromptInput:
    """Inputs to the wake-prompt builder.

    ``context_body`` is the already-assembled context string from the
    upstream collector (see module docstring). Pass an empty string when
    there is no saved state to inject.

    ``now`` is injectable for tests. When ``None`` (the production path),
    the builder resolves ``datetime.now(zoneinfo.ZoneInfo(timezone))``
    with a UTC fallback if the timezone string is invalid.
    """

    reason: WakeReason
    context_body: str = ""
    timezone: str = DEFAULT_TIMEZONE
    now: datetime | None = None


# Static tool / outreach hint block. Identical to the previous inline
# assembly in ``StreamingSession.connect()`` so the SDK refactor is a
# string-stable extraction.
_TOOLS_HINT = (
    "You have explicit pinky-messaging outreach tools: "
    "send, thread, react, send_gif, send_voice, send_photo, send_document, broadcast."
    "\n\nIMPORTANT: If your tools are deferred (require ToolSearch before use), "
    "immediately call ToolSearch with TWO queries to pre-load your core tools "
    "(max 10 per call):\n"
    '1. "select:mcp__pinky-messaging__send,mcp__pinky-messaging__thread,'
    "mcp__pinky-messaging__react,mcp__pinky-self__send_heartbeat,"
    "mcp__pinky-self__send_to_agent,mcp__pinky-self__check_inbox,"
    "mcp__pinky-self__create_task,mcp__pinky-self__get_next_task,"
    'mcp__pinky-self__complete_task,mcp__pinky-self__context_status"\n'
    '2. "select:mcp__pinky-memory__reflect,mcp__pinky-memory__recall,'
    "mcp__pinky-self__who_am_i,mcp__pinky-self__load_my_context,"
    "mcp__pinky-self__save_my_context,mcp__pinky-self__check_my_health,"
    'mcp__pinky-self__agent_status,mcp__pinky-self__claim_task"\n'
    "Do this silently before any other action — do not mention it to the user. "
    "Call both ToolSearch queries in parallel in a single response."
)


def _resolve_now(timezone_name: str, now: datetime | None) -> datetime:
    """Return a tz-aware ``now`` for header formatting.

    If ``now`` is provided, use it as-is (tests inject this). Otherwise
    resolve ``datetime.now(ZoneInfo(timezone_name))`` with a UTC fallback
    if the timezone string is invalid — matching the prior inline
    behavior in ``StreamingSession.connect()``.
    """
    if now is not None:
        return now
    try:
        tz = zoneinfo.ZoneInfo(timezone_name or DEFAULT_TIMEZONE)
        return datetime.now(tz)
    except Exception:
        # Fallback labelled "UTC" — issue #294 lineage: a previous
        # version implicitly used local-tz datetime.now() and falsely
        # labelled it UTC. Be explicit here.
        return datetime.now(_utc_timezone.utc)


def _format_time(now: datetime) -> str:
    """Format a datetime for the wake-prompt header.

    The ``%-d`` / ``%-I`` format directives are platform-specific
    (Linux/macOS) and match the original inline SDK assembly. The
    transport layer is expected to run on these platforms; if portability
    to Windows ever matters, switch to ``str(dt.day)`` / ``str(dt.hour)``
    in this helper.
    """
    tzname = now.tzname() or "UTC"
    return now.strftime(f"%A, %B %-d, %Y at %-I:%M %p {tzname}")


def _header_and_lead(reason: WakeReason, time_str: str) -> tuple[str, str]:
    """Return ``(header, instruction_lead)`` for a wake reason.

    ``header`` announces the wake event + current time. ``instruction_lead``
    is the agent-facing first sentence after the saved-state block; it
    leads into the static messaging/tool reminder.

    The ``RESUME`` copy is intentionally neutral ("Session resumed.") —
    the prior SDK copy claimed "Session resumed after daemon restart"
    which is wrong for tmux warm reconnect and idle-wake. Callers that
    explicitly know it was a daemon restart should encode that fact in
    the ``context_body`` (e.g. via the restart manifest).
    """
    if reason == WakeReason.RESUME:
        return (
            f"Session resumed. Current time: {time_str}.",
            "Pick up where you left off.",
        )
    if reason == WakeReason.CONTEXT_RESTART:
        return (
            f"Context restarted. Current time: {time_str}.",
            "Fresh context — pick up from saved state.",
        )
    if reason == WakeReason.AUTO_RESTART:
        return (
            f"Auto-restarted (context limit). Current time: {time_str}.",
            "Context was getting full — pick up from saved state.",
        )
    if reason == WakeReason.IDLE_WAKE:
        return (
            f"Waking from idle. Current time: {time_str}.",
            "Pick up from saved state.",
        )
    # NEW_SESSION (default).
    return (
        f"New session started. Current time: {time_str}.",
        "You're connected via Pinky's message broker.",
    )


def build_wake_prompt(inp: WakePromptInput) -> str:
    """Assemble a wake prompt string. Pure: no I/O.

    Structure (matches the prior inline SDK assembly):

        {header}
        ── Saved State ──    # only when context_body is non-empty
        {context_body}
        ──────────────────

        {instruction_lead} {messaging_tool_reminder} {tools_hint} {fallback_note}
    """
    now = _resolve_now(inp.timezone, inp.now)
    time_str = _format_time(now)
    header, lead = _header_and_lead(inp.reason, time_str)

    ctx_block = ""
    if inp.context_body:
        ctx_block = f"\n\n── Saved State ──\n{inp.context_body}\n──────────────────"

    return (
        f"{header}{ctx_block}\n\n"
        f"{lead} Users will message you through Telegram. "
        "Use send(chat_id, platform, text) for normal responses. "
        "Use thread(message_id, text) only when you want to quote/thread a specific message. "
        f"{_TOOLS_HINT} If you do not call an outreach tool, Pinky may fall back to plain-text delivery based on agent settings."
    )
