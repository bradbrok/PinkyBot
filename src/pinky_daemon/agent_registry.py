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
import math
import os
import re
import secrets
import shlex
import sqlite3
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID

from pinky_daemon.cron_utils import _field_matches
from pinky_daemon.effort import is_ultracode
from pinky_daemon.store_catalog import StoreCatalog

# Agent names appear in filesystem paths (data/agents/{name}/, hook scripts
# under .claude/, settings.json, .mcp.json) and database queries. Restrict
# to a safe character class to prevent path-traversal and arbitrary-write
# taint. The same regex lives on ``RegisterAgentRequest.name`` in
# api_models.py — duplicated here intentionally as defense-in-depth so any
# in-process caller of ``register()`` (tests, future routes, scripts) gets
# the same guarantee the API layer enforces, and so CodeQL's taint analysis
# sees an explicit sanitizer at the source-of-path-construction.
_AGENT_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
_BUZZ_HEX_64_RE = re.compile(r"^[0-9a-f]{64}$")
_BARSIK_BUZZ_RELAY_SIGNING_PUBKEY = (
    "12f6870117eff1a6318bd38c82a65d51dd19879b7489f57247114d0ee8a96de3"
)
BUZZ_OWNER_SILENCE_DAYS = 14
BUZZ_INBOUND_CLAIM_LEASE_SECONDS = 5 * 60
OUTBOX_REAPER_BATCH_SIZE = 10_000
OUTBOX_REAPER_PAYLOAD_TRIMMED = "[payload trimmed by outbox reaper]"


def _validate_buzz_pubkey(value: str, *, field_name: str = "pubkey") -> str:
    pubkey = str(value or "")
    if not _BUZZ_HEX_64_RE.fullmatch(pubkey):
        raise ValueError(f"Buzz {field_name} must be exactly 64 lowercase hex characters")
    return pubkey


def _validate_buzz_channel_id(value: str) -> str:
    try:
        canonical = str(UUID(str(value)))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError("Buzz channel_id must be a UUID") from exc
    if canonical != str(value):
        raise ValueError("Buzz channel_id must be a canonical lowercase UUID")
    return canonical


def _validate_buzz_annotation(value: str, *, field_name: str, limit: int) -> str:
    annotation = str(value or "").strip()
    if len(annotation) > limit or any(
        ord(ch) < 32 or ord(ch) == 127 for ch in annotation
    ):
        raise ValueError(
            f"Buzz {field_name} must be at most {limit} printable characters"
        )
    return annotation


def _validate_agent_name(name: str) -> str:
    """Validate ``name`` against the safe-char allowlist; return it unchanged.

    Raises ``ValueError`` on any name that could escape ``data/agents/``
    via path traversal or contain shell-unsafe characters that would
    end up in hook command strings.
    """
    if not isinstance(name, str) or not _AGENT_NAME_RE.fullmatch(name):
        raise ValueError(
            f"invalid agent name {name!r}: must match "
            f"^[a-z0-9][a-z0-9_-]{{0,62}}$ "
            f"(lowercase alphanumeric, underscore, hyphen; "
            f"starts with letter or digit; up to 63 chars)"
        )
    return name


class AgentPathContainmentError(ValueError):
    """A requested agent path resolved outside its owning workspace."""


def resolve_agent_path(
    agent_name: str,
    agent_dir: str | Path,
    *parts: str | Path,
) -> Path:
    """Resolve an agent-owned path and refuse aliases outside its workspace.

    ``agent_dir`` is the persisted owning workspace.  Resolution happens
    before any caller reads or writes the result, so ``..`` components,
    absolute children, symlinks, and case-normalized aliases cannot escape
    the workspace.  The agent name is validated here as well so every path
    boundary shares the registry's existing strict allowlist.
    """
    _validate_agent_name(agent_name)
    if not agent_dir:
        raise AgentPathContainmentError("agent workspace is not configured")
    workspace_path = Path(agent_dir)
    if not workspace_path.is_absolute():
        raise AgentPathContainmentError("agent workspace is not an absolute path")
    workspace = workspace_path.resolve()
    candidate = workspace.joinpath(*parts) if parts else workspace
    resolved = candidate.resolve()
    if resolved != workspace and not resolved.is_relative_to(workspace):
        raise AgentPathContainmentError("agent path is outside its workspace")
    return resolved


def replace_agent_text(
    agent_name: str,
    agent_dir: str | Path,
    path: str | Path,
    content: str,
) -> Path:
    """Atomically replace one contained agent file without following links."""
    target = resolve_agent_path(agent_name, agent_dir, path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, target)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise
    return target


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _verify_effort_hook_source() -> str:
    """Return the source for ``.claude/hook_verify_effort.py``.

    The hook compares the runtime ``$CLAUDE_EFFORT`` (Claude Code v2.1.133+)
    against ``$PINKY_EXPECTED_EFFORT`` (injected by the daemon at session
    start). On drift, it POSTs to ``/agents/{name}/effort-drift`` and, in
    strict mode (``$PINKY_STRICT_EFFORT=1``), emits a block decision so
    Claude Code refuses the tool call.

    No-ops silently when expected is empty/auto, when actual is unset (older
    CLI), or when ``$PINKY_AGENT_NAME`` is missing.
    """
    return '''\
#!/usr/bin/env python3
"""PinkyBot effort-drift verification hook (#429).

Compares the runtime thinking effort surfaced by Claude Code (v2.1.133+) to
the expected value injected by the daemon. On mismatch, reports drift and
optionally blocks the tool call.

Hooks must never crash the tool call — failures are swallowed.
"""
from __future__ import annotations

import json
import os
import sys

# Tools the hook MUST NOT block even in strict mode — these are how the
# agent self-remediates drift. Blocking set_thinking_effort would trap
# the agent: the only fix becomes unavailable. Match is substring against
# the tool name, so it covers raw `set_thinking_effort` and any MCP-qualified
# variant (e.g. `mcp__pinky-self__set_thinking_effort`).
REMEDIATION_TOOLS = (
    "set_thinking_effort",
)

# Levels set_thinking_effort MCP tool accepts. Kept in sync with the tool's
# validator (pinky_self/server.py). If a future registry adds a level
# outside this set, suggesting `set_thinking_effort(expected)` would fail
# at the MCP layer — surface a clear "not self-remediable" reason instead
# of an unreachable suggestion.
SET_EFFORT_ACCEPTED = ("low", "medium", "high", "xhigh", "max", "auto")


def _post_drift(agent: str, expected: str, actual: str, tool_name: str,
                strict: bool, daemon_url: str) -> None:
    import base64
    import hashlib
    import hmac
    import time
    import urllib.request

    path = f"/agents/{agent}/effort-drift"
    body = json.dumps({
        "expected": expected,
        "actual": actual,
        "tool_name": tool_name,
        "strict": bool(strict),
    }).encode()
    req = urllib.request.Request(
        f"{daemon_url}{path}", data=body, method="POST",
    )
    req.add_header("Content-Type", "application/json")
    req.add_header("x-pinky-agent", agent)
    # HMAC-sign exactly like the daemon's verify_internal_request expects:
    # SHA256 over the newline-joined agent / METHOD / path / ts, base64url,
    # padding stripped. Prefer the per-agent key — an isolated agent has the
    # global secret both withheld from its env (#639) and rejected by the
    # daemon (#640), so an unsigned (or global-secret-signed) effort-drift
    # POST 401s. With no secret at all there is nothing the daemon would
    # accept, so send unsigned and let the endpoint decide (legacy/dev).
    # (Ported from the Pi-only hotfix ec82055, which never landed on main.)
    secret = (
        os.environ.get("PINKY_AGENT_KEY", "").strip()
        or os.environ.get("PINKY_SESSION_SECRET", "").strip()
    )
    if secret:
        ts = int(time.time())
        sig_payload = f"{agent}\\nPOST\\n{path}\\n{ts}".encode()
        sig = base64.urlsafe_b64encode(
            hmac.new(secret.encode(), sig_payload, hashlib.sha256).digest()
        ).decode().rstrip("=")
        req.add_header("x-pinky-timestamp", str(ts))
        req.add_header("x-pinky-signature", sig)
    try:
        urllib.request.urlopen(req, timeout=2)
    except Exception:
        pass


def _is_remediation_tool(tool_name: str) -> bool:
    """True if tool_name is on the strict-mode allowlist."""
    if not tool_name:
        return False
    needle = tool_name.lower()
    return any(t in needle for t in REMEDIATION_TOOLS)


def _remediation_suggestion(expected: str) -> str:
    """Return a remediation call the agent can actually make.

    When expected is in the MCP tool's accepted set (the common case),
    suggest the direct call. Otherwise be honest: tell the agent the
    expected level isn't reachable from inside the session, so it knows
    to escalate to the owner rather than spinning on a suggestion that
    won't resolve the drift.
    """
    if expected in SET_EFFORT_ACCEPTED:
        return f"set_thinking_effort({expected!r})"
    return (
        f"<no self-remediation path: expected={expected!r} is not in the "
        "set_thinking_effort tool's accepted levels; ask your owner to "
        "either widen the tool's allow-list or relax strict_effort_enforcement>"
    )


def main() -> None:
    actual = os.environ.get("CLAUDE_EFFORT", "").strip().lower()
    expected = os.environ.get("PINKY_EXPECTED_EFFORT", "").strip().lower()
    agent = os.environ.get("PINKY_AGENT_NAME", "").strip()
    strict = os.environ.get("PINKY_STRICT_EFFORT", "").strip() == "1"
    daemon_url = os.environ.get(
        "PINKY_DAEMON_URL", "http://localhost:8888"
    ).rstrip("/")

    # No-op cases — these are not drift events:
    #   - no expected configured
    #   - expected is "auto" (effort is intentionally adaptive)
    #   - actual is unset (older Claude Code without v2.1.133 effort.level)
    #   - no agent name (hook misconfigured)
    if not expected or expected == "auto" or not actual or not agent:
        return

    if actual == expected:
        return  # match — no action

    # Try to read the tool name from the JSON event piped to stdin, if any.
    tool_name = ""
    try:
        stdin_data = sys.stdin.read() if not sys.stdin.isatty() else ""
        if stdin_data:
            payload = json.loads(stdin_data)
            tool_name = (
                payload.get("tool_name")
                or (payload.get("tool") or {}).get("name")
                or ""
            )
    except Exception:
        pass

    # Always record the drift — even when we're about to let it through.
    _post_drift(agent, expected, actual, tool_name, strict, daemon_url)

    # Strict path: emit a block decision EXCEPT when the tool being called
    # is the very thing that can fix the drift. Blocking the remediation
    # tool would trap the agent in an unbreakable strict-mode loop.
    if strict and not _is_remediation_tool(tool_name):
        print(json.dumps({
            "decision": "block",
            "reason": (
                f"Effort drift detected: expected={expected} actual={actual}. "
                f"Call {_remediation_suggestion(expected)} and retry the tool, "
                "or contact your owner to relax strict_effort_enforcement."
            ),
        }))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Hooks must never crash the tool call.
        pass
'''


def _cron_next_run(cron: str, timezone: str = "UTC") -> float | None:
    """Compute the next run timestamp for a cron expression using stdlib only.

    Supports standard 5-field cron: min hour dom month dow.
    Matching delegates to ``scheduler._field_matches`` so the displayed
    next_run agrees with when ``scheduler.cron_matches`` actually fires.
    Returns a UTC unix timestamp, or None on parse error / no match within
    a year.
    """
    try:
        import datetime as dt
        import zoneinfo

        parts = cron.strip().split()
        if len(parts) != 5:
            return None

        limits = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 6))
        sets: list[set[int]] = []
        for raw, (lo, hi) in zip(parts, limits):
            matched = {v for v in range(lo, hi + 1) if _field_matches(raw.strip(), v, lo, hi)}
            if not matched:
                return None  # unsatisfiable field, never fires
            sets.append(matched)
        minutes, hours, doms, months, dows = sets

        try:
            tz = zoneinfo.ZoneInfo(timezone)
        except Exception:
            tz = zoneinfo.ZoneInfo("UTC")

        now = dt.datetime.now(tz)
        candidate = now.replace(second=0, microsecond=0) + dt.timedelta(minutes=1)
        horizon = candidate + dt.timedelta(days=366)

        while candidate <= horizon:
            if (
                candidate.month in months
                and candidate.day in doms
                and candidate.isoweekday() % 7 in dows  # same mapping as cron_matches; Sun=0
            ):
                if candidate.hour in hours and candidate.minute in minutes:
                    return candidate.timestamp()
                candidate += dt.timedelta(minutes=1)
            else:
                # Date fields miss: skip the rest of the day in one step
                # instead of walking it minute by minute.
                candidate = (candidate + dt.timedelta(days=1)).replace(hour=0, minute=0)

        return None
    except Exception:
        return None


def _validate_schedule_cron(cron: str) -> None:
    """Reject cron expressions that the scheduler cannot match."""
    parts = cron.strip().split()
    if len(parts) != 5:
        raise ValueError("Cron expression must contain exactly five fields")

    limits = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 6))
    try:
        for part, (lo, hi) in zip(parts, limits):
            if not any(_field_matches(part, value, lo, hi) for value in range(lo, hi + 1)):
                raise ValueError
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid cron expression: {cron!r}") from exc


# Injected into the system prompt when an agent's effective effort is the
# ``ultracode`` tier (#151). Replicates Claude Code's native ultracode
# semantics — "xhigh effort plus standing dynamic-workflow orchestration" —
# as an explicit operating directive so the behavior holds on every CLI
# version, including ones predating native ultracode support. The effort knob
# itself is set to xhigh by the transports (see ``effort.resolve_cli_effort``);
# this section carries the workflow-by-default half.
ULTRACODE_DIRECTIVE = """## ⚡ Ultracode Mode (active)

This session runs in **ultracode**: maximum reasoning effort plus standing \
dynamic-workflow orchestration. Operate accordingly:

- **Author and run a Workflow for every substantive task by default.** \
Decompose the work, fan out parallel subagents, adversarially verify findings \
before committing, then synthesize. For multi-phase work (understand → design \
→ implement → review), run several workflows in sequence and stay in the loop \
between them.
- **Token cost is not the constraint — correctness and thoroughness are.** \
Favor exhaustive coverage and independent verification over a single fast pass.
- **Reserve solo, inline execution for trivial or conversational turns** (a \
quick answer, a one-line edit, acking a message). Everything non-trivial gets a \
workflow.
- If the Workflow tool is unavailable this session, fall back to spawning \
parallel subagents and adversarially verifying their output.

This mode is deliberate and owner-enabled. It stays on until the effort level \
is changed off ultracode."""

DEFAULT_HEARTBEAT_PROMPT = (
    "Heartbeat — your autonomy loop. This is your chance to act, not just report.\n\n"
    "1. Call send_heartbeat(status, context_pct, notes) first "
    "(status: ok/busy/finishing).\n"
    "2. Then be proactive:\n"
    "   - get_next_task() for pending work\n"
    "   - Follow up on anything you're tracking\n"
    "   - Reach out to the owner if you have updates, ideas, or finished something\n"
    "   - Do background maintenance (memory, context, cleanup)\n\n"
    "Don't just ping and go silent. If there's nothing to do, that's fine — "
    "but look first."
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
    # Soft context-watermark (#614): when usage first crosses this %, the
    # agent gets a one-time in-REPL nudge to checkpoint + context_restart
    # at a natural break. 0.0 = use the global default. Must sit below
    # restart_threshold_pct (the hard safety net).
    context_nudge_threshold_pct: float = 0.0
    auto_restart: bool = True
    parent: str = ""  # Parent agent name (for hierarchy)
    groups: list[str] = field(default_factory=list)
    max_sessions: int = 5  # Max concurrent sessions per agent
    enabled: bool = True
    auto_start: bool = False  # Auto-spawn main session on server boot
    heartbeat_interval: int = 0  # Seconds between heartbeats (0 = disabled)
    wake_interval: int = 0  # Seconds between wake checks (0 = disabled, 1800 = 30m, 3600 = 1h)
    clock_aligned: bool = True  # Align wakes to wall clock (:00, :30 for 30m; :00 for 1h)
    auto_sleep_hours: int = 0  # Auto-sleep after N hours inactive (0 = disabled)
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
    # #149 tenant isolation: True = hard-isolated tenant (Counterpart), scoped to
    # ITSELF only. Daemon denies it cross-agent actions + admin/register_agent.
    # Default False = full-trust inner-fleet agent (no behavior change).
    isolated: bool = False
    # #149 phase-3 OS isolation: HOW an isolated tenant is run at the OS level.
    #   "local"     = in-process, shares the daemon's OS user (default; current
    #                 behavior, no isolation beyond the daemon-authz `isolated` flag).
    #   "unix_user" = provisioned its own `pinky-<agent>` OS user + private
    #                 home/workdir/keys, run under that uid (inc3b). EXEC path is
    #                 Linux/systemd-only; macOS builds it but cannot run it.
    # Orthogonal to `isolated`: `isolated` is the daemon-authz boundary; this is
    # the runtime sandbox. Only meaningful when `isolated` is True.
    isolation_mode: str = "local"
    # Operator-supplied container image for isolation_mode="container" (bring-
    # your-own; Pinky pulls it, never builds it, and bakes in no CLIs). Empty
    # for every other mode. Consumed by ContainerProvisioner via its default
    # image_provider; the runtime cutover that uses it is host-gated.
    container_image: str = ""
    dream_enabled: bool = False  # Enable nightly memory consolidation
    dream_schedule: str = "0 3 * * *"  # Cron for dream runs (default 3 AM)
    dream_timezone: str = "America/Los_Angeles"  # IANA timezone for dream schedule
    dream_model: str = ""  # Model override for dream runs (empty = use agent's model)
    dream_notify: bool = True  # Inject dream summary into morning wake context
    librarian_enabled: bool = False  # Enable daily KB wiki curation
    librarian_schedule: str = "0 4 * * *"  # Cron for librarian (default 4 AM, after dreams)
    status: str = "active"  # active or retired
    retired_at: float = 0.0  # When was this agent retired
    working_status: str = "idle"  # idle, working, offline
    working_status_updated_at: float = 0.0  # When working_status last changed
    last_seen_at: float = 0.0  # Server-side presence: updated on delivery/turn completion
    runtime: str = "claude_sdk"  # Agent runtime: claude_sdk, codex_cli, opencode
    transport: str = "sdk"  # Claude runtime transport: sdk, tmux
    provider_url: str = ""   # e.g. "http://localhost:11434" for Ollama, empty = Anthropic default
    provider_key: str = ""   # API key override, empty = use ANTHROPIC_API_KEY env var
    provider_model: str = ""  # model name override (e.g. "llama3.2"), empty = use agent.model
    provider_ref: str = ""   # ID of a global provider from the providers table
    codex_home: str = ""  # Explicit per-agent CODEX_HOME override (flag-gated)
    thinking_effort: str = "medium"  # low, medium, high, xhigh, max, ultracode — default thinking depth
    # ``ultracode`` (#151): xhigh reasoning + standing workflow orchestration.
    # Resolves to xhigh for the actual effort knob (the CLI flag rejects the
    # literal "ultracode"); the workflow-by-default behavior is injected via
    # ULTRACODE_DIRECTIVE in build_system_prompt.
    # When True, the verify_effort CLI hook blocks tool calls if the runtime
    # effort drifts from thinking_effort. Default False (warn-only): drift is
    # surfaced to /agents/{name}/effort-drift + heartbeat but does not block.
    strict_effort_enforcement: bool = False
    # When True AND the agent is LOCAL (isolation_mode local / non-container),
    # the tmux session runs with its own CLAUDE_CONFIG_DIR
    # (<working_dir>/.claude-local) and the shared CLAUDE_CODE_OAUTH_TOKEN is
    # withheld — so the agent holds its OWN Claude subscription account
    # (populated later by a manual `claude /login`) instead of sharing the
    # daemon user's ~/.claude. No-op for container agents (they already get
    # their own config dir). Default False = shared ~/.claude (current behavior).
    dedicated_config_dir: bool = False
    watchdog_config: dict = field(default_factory=dict)  # Per-agent watchdog overrides (JSON blob)
    # watchdog_config schema: {
    #   "enabled": true,              # Enable/disable watchdog for this agent
    #   "mode": "recover",            # "alert" (warn only) or "recover" (auto-restart)
    #   "warn_after_seconds": 600,    # Seconds before warning
    #   "recover_after_seconds": 900, # Seconds before auto-recovery
    #   "require_backlog": true,      # Only act if pending messages exist
    #   "min_pending": 1              # Minimum pending messages to trigger
    # }
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
            "context_nudge_threshold_pct": self.context_nudge_threshold_pct,
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
            "isolated": self.isolated,
            "isolation_mode": self.isolation_mode,
            "container_image": self.container_image,
            "dream_enabled": self.dream_enabled,
            "dream_schedule": self.dream_schedule,
            "dream_timezone": self.dream_timezone,
            "dream_model": self.dream_model,
            "dream_notify": self.dream_notify,
            "librarian_enabled": self.librarian_enabled,
            "librarian_schedule": self.librarian_schedule,
            "status": self.status,
            "retired_at": self.retired_at,
            "working_status": self.working_status,
            "working_status_updated_at": self.working_status_updated_at,
            "last_seen_at": self.last_seen_at,
            "runtime": self.runtime,
            "transport": self.transport,
            "provider_url": self.provider_url,
            "provider_key_set": bool(self.provider_key),
            "provider_model": self.provider_model,
            "provider_ref": self.provider_ref,
            "codex_home": self.codex_home,
            "thinking_effort": self.thinking_effort,
            "strict_effort_enforcement": self.strict_effort_enforcement,
            "dedicated_config_dir": self.dedicated_config_dir,
            "watchdog_config": self.watchdog_config,
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
    token_ref: str = ""  # reference to global bot_tokens.id

    def to_dict(self) -> dict:
        return {
            "agent_name": self.agent_name,
            "platform": self.platform,
            "token_set": self.token_set,
            "enabled": self.enabled,
            "settings": self.settings,
            "updated_at": self.updated_at,
            "token_ref": self.token_ref,
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
    last_run: float = 0.0  # Scheduler decided to fire.
    last_delivered: float = 0.0  # Session confirmed prompt acceptance.
    created_at: float = 0.0
    direct_send: bool = False  # If true, prompt is sent directly as a message (not as agent input)
    target_channel: str = ""  # Chat ID or channel for direct_send routing
    one_shot: bool = False  # If true, auto-disable after first firing

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
            "last_delivered": self.last_delivered,
            "next_run": _cron_next_run(self.cron, self.timezone),
            "created_at": self.created_at,
            "direct_send": self.direct_send,
            "target_channel": self.target_channel,
            "one_shot": self.one_shot,
        }


@dataclass
class PendingScheduleWake:
    """One durable exact-fire scheduler ledger record.

    The historical class name is retained because active rows are also the
    replay outbox.  ``accepted_at``, ``parked_at``, and ``abandoned_at`` make
    terminal states explicit without deleting the forensic receipt.
    """

    id: int = 0
    schedule_id: int = 0
    agent_name: str = ""
    schedule_name: str = ""
    prompt: str = ""
    fired_at: float = 0.0
    created_at: float = 0.0
    attempts: int = 0
    parked_at: float = 0.0
    accepted_at: float = 0.0
    failed_at: float = 0.0
    last_error: str = ""
    abandoned_at: float = 0.0

    @property
    def name(self) -> str:
        """Match the ``AgentSchedule`` interface used by receipt waiting."""
        return self.schedule_name

    @property
    def ledger_state(self) -> str:
        """Return the exact operational outcome used by fleet health."""
        if self.accepted_at > 0:
            return "receipted-ran-once"
        if self.abandoned_at > 0:
            return "abandoned"
        if self.parked_at > 0:
            return "quarantined"
        return "pending"

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "schedule_id": self.schedule_id,
            "agent_name": self.agent_name,
            "schedule_name": self.schedule_name,
            "prompt": self.prompt,
            "fired_at": self.fired_at,
            "created_at": self.created_at,
            "attempts": self.attempts,
            "parked_at": self.parked_at,
            "accepted_at": self.accepted_at,
            "failed_at": self.failed_at,
            "last_error": self.last_error,
            "abandoned_at": self.abandoned_at,
            "state": self.ledger_state,
        }


@dataclass(frozen=True)
class RecurringScheduleStaleDrop:
    """Bounded aggregate of recurring fires dropped before delivery."""

    schedule_id: int = 0
    agent_name: str = ""
    schedule_name: str = ""
    drop_count: int = 0
    first_dropped_at: float = 0.0
    last_dropped_at: float = 0.0
    max_row_age_s: float = 0.0
    generation: int = 0


class ScheduleNameConflictError(ValueError):
    """An enabled schedule already uses the requested agent/name pair."""


class AgentAlreadyExistsError(ValueError):
    """A create-only registration collided with an existing agent name."""


class AgentRegistrationIncompleteError(RuntimeError):
    """A create-only registration won its DB name but failed before completion."""

    def __init__(self, name: str, *, row_committed: bool):
        super().__init__(f"registration incomplete for {name}")
        self.row_committed = row_committed


class AgentWorkspacePathError(ValueError):
    """An agent workspace could not be resolved to a usable absolute path."""


class AgentWorkspaceOverlapError(ValueError):
    """An agent workspace overlaps another registered agent's owner root."""


@dataclass(frozen=True)
class SoulMutationSummary:
    """Content-free evidence explaining why a soul replacement is risky."""

    old_length: int
    new_length: int
    shrink_percent: float
    missing_anchors: tuple[str, ...]

    def to_dict(self) -> dict:
        return {
            "old_length": self.old_length,
            "new_length": self.new_length,
            "shrink_percent": self.shrink_percent,
            "missing_anchors": list(self.missing_anchors),
        }


class SoulMutationRejectedError(ValueError):
    """A soul replacement needs an explicit force flag before it may run."""

    def __init__(self, summary: SoulMutationSummary) -> None:
        super().__init__("soul mutation rejected by shrink/identity guard")
        self.summary = summary


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
    wake_action: str = ""  # Required first action on wake-up
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
            "wake_action": self.wake_action,
            "metadata": self.metadata,
            "updated_at": self.updated_at,
            "updated_by": self.updated_by,
        }

    def to_prompt(self, resume_mode: bool = False) -> str:
        """Format as a system prompt section for injection on restart.

        ``resume_mode=True`` is for warm-resume wakes (``claude --continue``
        succeeded, prior conversation already in context). In that mode
        only the wake_action directive is rendered — the rest of the
        manifest (task/context/notes/blockers/priority) is redundant
        with what the resumed conversation already carries, and replaying
        it caused #591. The wake_action is preserved because it is a
        directive ("do this FIRST"), not history; dropping it would
        silently lose intent set by the prior session.
        """
        parts = []
        if self.wake_action:
            parts.append(f"## ⚡ Wake Action (do this FIRST)\n{self.wake_action}")
        if resume_mode:
            return "\n\n".join(parts) if parts else ""
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


def _tmux_wake_hook_source(agent_name: str) -> str:
    """Return the source for ``.claude/hook_tmux_wake.py``.

    Fires on ``Stop`` (and ``PostCompact``, if wired). POSTs to
    ``/agents/{name}/transport/wake`` so the TmuxSession's transcript
    tailer wakes immediately rather than waiting for the next poll tick.
    HMAC-signed identically to ``hook_idle.py``. No-op for non-tmux
    agents — the daemon endpoint returns 200 with session: None.

    Idempotent + fire-and-forget. Failures are swallowed so a daemon
    outage doesn't block the model turn.
    """
    return f'''\
#!/usr/bin/env python3
"""PinkyBot transport wake hook (PR8b).

Notifies the daemon that the model turn has ended so the response
tailer can read up-to-EOF on the transcript file without waiting for
the fallback poll. No-op for non-tmux runtimes (daemon returns 200
session: None).
"""
import hashlib, hmac, base64, time, urllib.request, json, os, sys

secret = os.environ.get("PINKY_AGENT_KEY", "").strip() or os.environ.get("PINKY_SESSION_SECRET", "").strip()
if not secret:
    sys.exit(0)

try:
    raw = sys.stdin.read()
    payload_in = json.loads(raw) if raw else {{}}
except Exception:
    payload_in = {{}}

agent = "{agent_name}"
path = "/agents/{agent_name}/transport/wake"
ts = int(time.time())
payload = f"{{agent}}\\nPOST\\n{{path}}\\n{{ts}}".encode()
digest = hmac.new(secret.encode(), payload, hashlib.sha256).digest()
sig = base64.urlsafe_b64encode(digest).decode().rstrip("=")

req = urllib.request.Request(
    os.environ.get("PINKY_DAEMON_URL", "http://localhost:8888").rstrip("/") + path,
    data=json.dumps({{
        "event": "stop_hook_summary",
        "session_id": payload_in.get("session_id", ""),
        "agent_id": payload_in.get("agent_id", ""),
        "agent_type": payload_in.get("agent_type", ""),
    }}).encode(),
    method="POST",
)
req.add_header("Content-Type", "application/json")
req.add_header("x-pinky-agent", agent)
req.add_header("x-pinky-timestamp", str(ts))
req.add_header("x-pinky-signature", sig)
try:
    urllib.request.urlopen(req, timeout=2)
except Exception:
    pass
'''


def _tmux_pre_tool_hook_source(agent_name: str) -> str:
    """Return the source for ``.claude/hook_tmux_pre_tool.py``.

    Fires on Claude Code's ``PreToolUse``. Reads the hook payload from
    stdin (``session_id``, ``tool_name``, ``tool_input``, ``tool_use_id``)
    and POSTs to ``/agents/{name}/transport/tool-use`` so the daemon
    can record the tool call to analytics and emit a live stream event.

    Task #93: tmux parity with SDK's tool tracking (see
    ``streaming_session.py``'s ``_analytics_start_tool_call``).

    No-op for non-tmux runtimes (daemon returns 200 session: None).
    Idempotent + fire-and-forget — failures are swallowed so a daemon
    outage doesn't block the model turn.
    """
    return f'''\
#!/usr/bin/env python3
"""PinkyBot PreToolUse hook (task #93).

POSTs the tool-call start to the daemon so tmux-transport agents
record analytics + emit live SSE events matching SDK parity.
"""
import hashlib, hmac, base64, time, urllib.request, json, os, sys

secret = os.environ.get("PINKY_AGENT_KEY", "").strip() or os.environ.get("PINKY_SESSION_SECRET", "").strip()
if not secret:
    sys.exit(0)

try:
    raw = sys.stdin.read()
    payload_in = json.loads(raw) if raw else {{}}
except Exception:
    sys.exit(0)

tool_name = payload_in.get("tool_name", "")
if not tool_name:
    sys.exit(0)

agent = "{agent_name}"
path = "/agents/{agent_name}/transport/tool-use"
ts = int(time.time())
payload_sig = f"{{agent}}\\nPOST\\n{{path}}\\n{{ts}}".encode()
digest = hmac.new(secret.encode(), payload_sig, hashlib.sha256).digest()
sig = base64.urlsafe_b64encode(digest).decode().rstrip("=")

body = {{
    "session_id": payload_in.get("session_id", ""),
    "tool_use_id": payload_in.get("tool_use_id", ""),
    "tool_name": tool_name,
    "tool_input": payload_in.get("tool_input") or {{}},
    # Piggyback the runtime thinking effort (Claude Code v2.1.133+) so the
    # daemon can surface what the REPL is ACTUALLY running at — the read
    # side of the model/effort selector. Empty on older CLIs.
    "effort": os.environ.get("CLAUDE_EFFORT", ""),
}}

req = urllib.request.Request(
    os.environ.get("PINKY_DAEMON_URL", "http://localhost:8888").rstrip("/") + path,
    data=json.dumps(body).encode(),
    method="POST",
)
req.add_header("Content-Type", "application/json")
req.add_header("x-pinky-agent", agent)
req.add_header("x-pinky-timestamp", str(ts))
req.add_header("x-pinky-signature", sig)
try:
    urllib.request.urlopen(req, timeout=2)
except Exception:
    pass
'''


def _tmux_post_tool_hook_source(agent_name: str) -> str:
    """Return the source for ``.claude/hook_tmux_post_tool.py``.

    Fires on Claude Code's ``PostToolUse``. Reads the hook payload
    from stdin (includes ``tool_response`` with success / error info)
    and POSTs to ``/agents/{name}/transport/tool-result`` so the
    daemon can mark the analytics row finished and emit the result
    stream event.

    Task #93. No-op for non-tmux runtimes. Fire-and-forget.
    """
    return f'''\
#!/usr/bin/env python3
"""PinkyBot PostToolUse hook (task #93).

POSTs the tool-call result to the daemon so tmux-transport agents
record analytics + emit live SSE events matching SDK parity.
"""
import hashlib, hmac, base64, time, urllib.request, json, os, sys

secret = os.environ.get("PINKY_AGENT_KEY", "").strip() or os.environ.get("PINKY_SESSION_SECRET", "").strip()
if not secret:
    sys.exit(0)

try:
    raw = sys.stdin.read()
    payload_in = json.loads(raw) if raw else {{}}
except Exception:
    sys.exit(0)

tool_name = payload_in.get("tool_name", "")
if not tool_name:
    sys.exit(0)

# Claude Code's PostToolUse payload carries `tool_response` (the
# tool's actual return value/error). Shape varies by tool, so we
# pass it through verbatim and let the daemon decide what to extract.
tool_response = payload_in.get("tool_response")
is_error = False
if isinstance(tool_response, dict):
    # Common shapes: {{"is_error": true, ...}}, {{"error": "..."}},
    # or {{"content": [{{"type": "text", "text": "..."}}], "is_error": bool}}
    is_error = bool(
        tool_response.get("is_error")
        or tool_response.get("error")
    )

agent = "{agent_name}"
path = "/agents/{agent_name}/transport/tool-result"
ts = int(time.time())
payload_sig = f"{{agent}}\\nPOST\\n{{path}}\\n{{ts}}".encode()
digest = hmac.new(secret.encode(), payload_sig, hashlib.sha256).digest()
sig = base64.urlsafe_b64encode(digest).decode().rstrip("=")

body = {{
    "session_id": payload_in.get("session_id", ""),
    "tool_use_id": payload_in.get("tool_use_id", ""),
    "tool_name": tool_name,
    "is_error": is_error,
    "tool_response": tool_response if isinstance(tool_response, (dict, list, str)) else None,
}}

req = urllib.request.Request(
    os.environ.get("PINKY_DAEMON_URL", "http://localhost:8888").rstrip("/") + path,
    data=json.dumps(body, default=str).encode(),
    method="POST",
)
req.add_header("Content-Type", "application/json")
req.add_header("x-pinky-agent", agent)
req.add_header("x-pinky-timestamp", str(ts))
req.add_header("x-pinky-signature", sig)
try:
    urllib.request.urlopen(req, timeout=2)
except Exception:
    pass
'''


def _tmux_stop_failure_hook_source(agent_name: str) -> str:
    """Return the source for ``.claude/hook_tmux_stop_failure.py``.

    Fires on Claude Code's ``StopFailure`` — a turn that ended due to an
    API error. Reads the hook payload from stdin (CC's ``error`` field
    carries the typed failure: ``authentication_failed`` / ``rate_limit``
    / ``billing_error`` / ``server_error`` / …) and POSTs it to
    ``/agents/{name}/transport/stop-failure`` so the daemon can alert the
    owner on auth-class failures — instead of the agent going silently
    dark — and log the rest (rate_limit / billing) for observability.

    StopFailure hook output/exit code is ignored by Claude Code
    (observability-only), so this can never affect the turn. Fire-and-
    forget; no-op when PINKY_SESSION_SECRET is unset.
    """
    return f'''\
#!/usr/bin/env python3
"""PinkyBot StopFailure hook.

Reads CC's StopFailure payload (the typed failure is in the ``error``
field) and POSTs it to the daemon so it can alert on auth-class
failures proactively rather than the agent going silently dark.
"""
import hashlib, hmac, base64, time, urllib.request, json, os, sys

secret = os.environ.get("PINKY_AGENT_KEY", "").strip() or os.environ.get("PINKY_SESSION_SECRET", "").strip()
if not secret:
    sys.exit(0)

try:
    raw = sys.stdin.read()
    payload_in = json.loads(raw) if raw else {{}}
except Exception:
    payload_in = {{}}

# Claude Code delivers the typed failure in ``error`` (NOT
# ``error_type``); ``error_type`` is kept as a defensive alias for
# our own internal posts. See StopFailure input schema:
# https://code.claude.com/docs/en/hooks#stopfailure-input
error_type = payload_in.get("error") or payload_in.get("error_type") or "unknown"
# ``error`` is the type, not a message — for human-readable detail CC
# gives ``last_assistant_message`` (the rendered API-error string) and
# ``error_details``; fall back to our internal message keys.
message = ""
for _k in ("last_assistant_message", "error_details", "message", "error_message"):
    _v = payload_in.get(_k)
    if isinstance(_v, str) and _v:
        message = _v
        break

agent = "{agent_name}"
path = "/agents/{agent_name}/transport/stop-failure"
ts = int(time.time())
payload_sig = f"{{agent}}\\nPOST\\n{{path}}\\n{{ts}}".encode()
digest = hmac.new(secret.encode(), payload_sig, hashlib.sha256).digest()
sig = base64.urlsafe_b64encode(digest).decode().rstrip("=")

body = {{
    "error_type": error_type,
    "message": message,
    "session_id": payload_in.get("session_id", ""),
    "agent_id": payload_in.get("agent_id", ""),
    "agent_type": payload_in.get("agent_type", ""),
}}

req = urllib.request.Request(
    os.environ.get("PINKY_DAEMON_URL", "http://localhost:8888").rstrip("/") + path,
    data=json.dumps(body).encode(),
    method="POST",
)
req.add_header("Content-Type", "application/json")
req.add_header("x-pinky-agent", agent)
req.add_header("x-pinky-timestamp", str(ts))
req.add_header("x-pinky-signature", sig)
try:
    urllib.request.urlopen(req, timeout=2)
except Exception:
    pass
'''


def _tmux_session_start_hook_source(agent_name: str) -> str:
    """Return the source for ``.claude/hook_tmux_session_start.py``.

    Fires on ``SessionStart``. Reads the JSON payload from stdin
    (``session_id``, ``transcript_path``, ``cwd``) and POSTs the
    transcript path to ``/agents/{name}/transport/transcript-path``
    so the tailer can repoint at the canonical file instead of relying
    on the daemon's mtime-glob guess.

    No-op for non-tmux runtimes (daemon returns 200 session: None).
    """
    return f'''\
#!/usr/bin/env python3
"""PinkyBot transport SessionStart hook (PR8b).

Reports the actual transcript path Claude Code is writing to so the
TmuxSession tailer doesn't have to guess via mtime-glob. Reads the
SessionStart hook payload from stdin (per Claude Code hook spec) and
forwards transcript_path to the daemon.
"""
import hashlib, hmac, base64, time, urllib.request, json, os, sys

secret = os.environ.get("PINKY_AGENT_KEY", "").strip() or os.environ.get("PINKY_SESSION_SECRET", "").strip()
if not secret:
    sys.exit(0)

# SessionStart payload arrives on stdin per Claude Code hook spec.
try:
    raw = sys.stdin.read()
    payload_in = json.loads(raw) if raw else {{}}
except Exception:
    sys.exit(0)

transcript_path = payload_in.get("transcript_path", "")
if not transcript_path:
    sys.exit(0)
session_id = payload_in.get("session_id", "")

agent = "{agent_name}"
path = "/agents/{agent_name}/transport/transcript-path"
ts = int(time.time())
payload_sig = f"{{agent}}\\nPOST\\n{{path}}\\n{{ts}}".encode()
digest = hmac.new(secret.encode(), payload_sig, hashlib.sha256).digest()
sig = base64.urlsafe_b64encode(digest).decode().rstrip("=")

req = urllib.request.Request(
    os.environ.get("PINKY_DAEMON_URL", "http://localhost:8888").rstrip("/") + path,
    data=json.dumps({{
        "transcript_path": transcript_path,
        "session_id": session_id,
    }}).encode(),
    method="POST",
)
req.add_header("Content-Type", "application/json")
req.add_header("x-pinky-agent", agent)
req.add_header("x-pinky-timestamp", str(ts))
req.add_header("x-pinky-signature", sig)
try:
    urllib.request.urlopen(req, timeout=2)
except Exception:
    pass
'''


class AgentDbConfigError(RuntimeError):
    """Raised when conversations_agents.db cannot be confirmed in the required
    rollback-journal (TRUNCATE) mode. We refuse to run the agents DB on WAL —
    the WAL ``-shm`` mmap is the SIGBUS fault surface (#797/#220)."""


def _configure_agents_db_connection(
    conn: sqlite3.Connection, *, retries: int = 6, busy_ms: int = 5000
) -> str:
    """Put the agents-DB connection into rollback (TRUNCATE) journal mode.

    Why not WAL (#797/#220): the WAL wal-index ``-shm`` is always memory-mapped
    in WAL mode. Under the daemon's long-lived registry connection plus the
    per-request read-only signing-key resolver churn, that mapped ``-shm`` page
    went stale and a SQLite pager read SIGBUS'd the daemon (``si_addr`` confirmed
    inside ``conversations_agents.db-shm``; ``mmap_size=0`` was already set, so
    the main-db is not mapped — the ``-shm`` is the inherent fault surface).
    Rollback journal mode has no ``-shm`` at all, so the daemon never maps it.

    Must run BEFORE table init and before any local MCP / agent-session resume
    can spawn stdio children that hold the DB.

    Fails LOUD: if the connection cannot be confirmed in ``truncate`` mode after
    bounded retries, raises :class:`AgentDbConfigError` rather than silently
    running on WAL. Returns the effective journal mode (``"truncate"``).
    """
    conn.execute(f"PRAGMA busy_timeout={int(busy_ms)}")
    last: str | None = None
    for attempt in range(retries):
        # If still on WAL, drain it first so no hot WAL content is stranded
        # before the wal-index is dropped. Busy here is non-fatal — the mode
        # switch below retries.
        try:
            cur = conn.execute("PRAGMA journal_mode").fetchone()
            if cur and str(cur[0]).lower() == "wal":
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.OperationalError:
            pass
        try:
            row = conn.execute("PRAGMA journal_mode=TRUNCATE").fetchone()
            last = str(row[0]).lower() if row else None
            if last == "truncate":
                return last
        except sqlite3.OperationalError as exc:
            last = f"error:{exc}"
        time.sleep(0.2 * (attempt + 1))
    raise AgentDbConfigError(
        f"conversations_agents.db refused to leave WAL: journal_mode={last!r} "
        f"after {retries} attempts — refusing to run on the WAL -shm SIGBUS "
        f"surface (#797/#220)."
    )


class AgentRegistry:
    """SQLite-backed agent registry."""

    def __init__(
        self,
        db_path: str = "data/agents.db",
        *,
        buzz_device_key_path: str | None = None,
        catalog: StoreCatalog | None = None,
    ) -> None:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        # Absolute path retained so callers (e.g. _write_mcp_json) can hand
        # stdio MCP subprocesses an explicit DB location for request-time
        # signing-key lookup (#641) rather than relying on their cwd.
        self._db_path = str(Path(db_path).resolve())
        self._buzz_device_key_path = str(
            Path(buzz_device_key_path).resolve()
            if buzz_device_key_path
            else Path(self._db_path).parent / "identity" / ".device_key"
        )
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        # #797/#220: the agents DB runs in ROLLBACK (TRUNCATE) journal mode, NOT
        # WAL. The WAL wal-index (-shm) is always mmap'd; under the long-lived
        # registry connection + per-request RO signing-key resolver churn, that
        # mapped -shm went stale and a SQLite pager read SIGBUS'd the daemon
        # (si_addr confirmed inside conversations_agents.db-shm). Rollback mode
        # has no -shm, so the daemon never maps it. Runs before _init_tables and
        # before any MCP/session resume spawns stdio children. Agents DB only.
        journal_mode = _configure_agents_db_connection(self._db)
        self._db.execute("PRAGMA foreign_keys=ON")
        if catalog is not None:
            catalog.register(
                "agents",
                self._db_path,
                journal_mode=journal_mode,
                owner=type(self).__name__,
            )
        # Guard read-modify-write sequences (e.g. peer_fleet_acl mutation)
        # from concurrent admin-API requests. SQLite connection is shared
        # across threads (check_same_thread=False) and Python's default
        # isolation_level uses deferred BEGIN — without this lock, two
        # admin requests can both read the same baseline ACL, append
        # different entries, and one write loses.
        self._rmw_lock = threading.RLock()
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
                context_nudge_threshold_pct REAL NOT NULL DEFAULT 0.0,
                auto_restart INTEGER NOT NULL DEFAULT 1,
                parent TEXT NOT NULL DEFAULT '',
                groups TEXT NOT NULL DEFAULT '[]',
                max_sessions INTEGER NOT NULL DEFAULT 5,
                enabled INTEGER NOT NULL DEFAULT 1,
                registration_finalized INTEGER NOT NULL DEFAULT 1,
                auto_start INTEGER NOT NULL DEFAULT 0,
                heartbeat_interval INTEGER NOT NULL DEFAULT 0,
                plain_text_fallback INTEGER NOT NULL DEFAULT 0,
                role TEXT NOT NULL DEFAULT '',
                runtime TEXT NOT NULL DEFAULT 'claude_sdk',
                transport TEXT NOT NULL DEFAULT 'sdk',
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
                last_delivered REAL NOT NULL DEFAULT 0,
                created_at REAL NOT NULL,
                FOREIGN KEY (agent_name) REFERENCES agents(name) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS pending_schedule_wakes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                schedule_id INTEGER NOT NULL,
                agent_name TEXT NOT NULL,
                schedule_name TEXT NOT NULL DEFAULT '',
                prompt TEXT NOT NULL DEFAULT '',
                fired_at REAL NOT NULL,
                created_at REAL NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                parked_at REAL NOT NULL DEFAULT 0,
                accepted_at REAL NOT NULL DEFAULT 0,
                failed_at REAL NOT NULL DEFAULT 0,
                last_error TEXT NOT NULL DEFAULT '',
                abandoned_at REAL NOT NULL DEFAULT 0,
                FOREIGN KEY (agent_name) REFERENCES agents(name) ON DELETE CASCADE,
                UNIQUE(schedule_id, fired_at)
            );

            CREATE TABLE IF NOT EXISTS recurring_schedule_stale_drops (
                schedule_id INTEGER PRIMARY KEY,
                agent_name TEXT NOT NULL,
                schedule_name TEXT NOT NULL DEFAULT '',
                drop_count INTEGER NOT NULL DEFAULT 1,
                first_dropped_at REAL NOT NULL,
                last_dropped_at REAL NOT NULL,
                max_row_age_s REAL NOT NULL DEFAULT 0,
                generation INTEGER NOT NULL DEFAULT 1,
                FOREIGN KEY (schedule_id) REFERENCES agent_schedules(id)
                    ON DELETE CASCADE,
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
                reply_chat_id TEXT NOT NULL DEFAULT '',
                is_group INTEGER NOT NULL DEFAULT 0,
                sender_id TEXT NOT NULL DEFAULT '',
                sender_name TEXT NOT NULL DEFAULT '',
                content TEXT NOT NULL,
                created_at REAL NOT NULL,
                delivered INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY (agent_name) REFERENCES agents(name) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS approval_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_name TEXT NOT NULL,
                chat_id TEXT NOT NULL,
                target_name TEXT NOT NULL DEFAULT '',
                is_channel INTEGER NOT NULL DEFAULT 0,
                gate_state TEXT NOT NULL DEFAULT 'pending',
                held_count INTEGER NOT NULL DEFAULT 0,
                oldest_held_at REAL NOT NULL DEFAULT 0,
                notification_state TEXT NOT NULL DEFAULT 'retrying',
                notification_attempts INTEGER NOT NULL DEFAULT 0,
                notified_held_count INTEGER NOT NULL DEFAULT 0,
                last_notified_at REAL NOT NULL DEFAULT 0,
                next_retry_at REAL NOT NULL DEFAULT 0,
                last_error TEXT NOT NULL DEFAULT '',
                notification_destination TEXT NOT NULL DEFAULT '{}',
                fallback_path TEXT NOT NULL DEFAULT '[]',
                aging_reprompt_count INTEGER NOT NULL DEFAULT 0,
                high_signal_alerted_at REAL NOT NULL DEFAULT 0,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                FOREIGN KEY (agent_name) REFERENCES agents(name) ON DELETE CASCADE,
                UNIQUE(agent_name, chat_id)
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

            CREATE TABLE IF NOT EXISTS verified_contacts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_name TEXT NOT NULL,
                platform TEXT NOT NULL,
                principal TEXT NOT NULL,
                name TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT '',
                added_at REAL NOT NULL,
                UNIQUE(agent_name, platform, principal)
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
            CREATE INDEX IF NOT EXISTS idx_pending_schedule_wakes_agent
                ON pending_schedule_wakes(agent_name, fired_at, id);
            CREATE INDEX IF NOT EXISTS idx_recurring_stale_drops_agent
                ON recurring_schedule_stale_drops(agent_name, schedule_id);
            CREATE INDEX IF NOT EXISTS idx_pending_messages_agent_chat
                ON pending_messages(agent_name, chat_id, delivered);
            CREATE INDEX IF NOT EXISTS idx_approval_requests_retry
                ON approval_requests(gate_state, notification_state, next_retry_at);
            CREATE INDEX IF NOT EXISTS idx_group_chats_agent
                ON group_chats(agent_name);
            CREATE INDEX IF NOT EXISTS idx_verified_contacts_agent
                ON verified_contacts(agent_name);
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

            CREATE TABLE IF NOT EXISTS bot_tokens (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                platform TEXT NOT NULL DEFAULT 'telegram',
                token TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL DEFAULT 0,
                updated_at REAL NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS effort_drift_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_name TEXT NOT NULL,
                session_id TEXT NOT NULL DEFAULT '',
                expected TEXT NOT NULL,
                actual TEXT NOT NULL,
                tool_name TEXT NOT NULL DEFAULT '',
                strict INTEGER NOT NULL DEFAULT 0,
                timestamp REAL NOT NULL,
                FOREIGN KEY (agent_name) REFERENCES agents(name) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_effort_drift_agent ON effort_drift_events(agent_name, timestamp DESC);

            CREATE TABLE IF NOT EXISTS models (
                id TEXT PRIMARY KEY,
                provider TEXT NOT NULL DEFAULT 'anthropic',
                model_id TEXT NOT NULL,
                display_name TEXT NOT NULL DEFAULT '',
                description TEXT NOT NULL DEFAULT '',
                tier TEXT NOT NULL DEFAULT '',
                context_window INTEGER NOT NULL DEFAULT 200000,
                is_1m INTEGER NOT NULL DEFAULT 0,
                input_price REAL NOT NULL DEFAULT 0,
                output_price REAL NOT NULL DEFAULT 0,
                cached_input_price REAL NOT NULL DEFAULT 0,
                supports_thinking INTEGER NOT NULL DEFAULT 1,
                active INTEGER NOT NULL DEFAULT 1,
                sort_order INTEGER NOT NULL DEFAULT 100,
                created_at REAL NOT NULL DEFAULT 0,
                updated_at REAL NOT NULL DEFAULT 0,
                UNIQUE(provider, model_id)
            );

            CREATE TABLE IF NOT EXISTS agent_signing_keys (
                agent_name TEXT PRIMARY KEY,
                signing_key TEXT NOT NULL,
                created_at REAL NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS buzz_identities (
                agent TEXT PRIMARY KEY NOT NULL,
                pubkey TEXT NOT NULL
                    CHECK(length(pubkey)=64 AND pubkey=lower(pubkey)
                          AND pubkey NOT GLOB '*[^0-9a-f]*'),
                wrap_version INTEGER NOT NULL,
                nonce BLOB NOT NULL,
                ciphertext BLOB NOT NULL,
                relay_url TEXT NOT NULL,
                community_id TEXT NOT NULL,
                relay_signing_pubkey TEXT NOT NULL DEFAULT ''
                    CHECK(
                        relay_signing_pubkey='' OR (
                            length(relay_signing_pubkey)=64
                            AND relay_signing_pubkey=lower(relay_signing_pubkey)
                            AND relay_signing_pubkey NOT GLOB '*[^0-9a-f]*'
                        )
                    ),
                enabled INTEGER NOT NULL DEFAULT 0 CHECK(enabled IN (0, 1)),
                status TEXT NOT NULL DEFAULT 'disabled',
                last_error TEXT NOT NULL DEFAULT '',
                tos_receipt TEXT NOT NULL,
                tos_approved_by TEXT NOT NULL,
                tos_approved_at REAL NOT NULL,
                tos_approval_ref TEXT NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                FOREIGN KEY (agent) REFERENCES agents(name) ON DELETE CASCADE,
                CHECK(
                    enabled=0 OR (
                        tos_receipt != '' AND tos_approved_by != ''
                        AND tos_approved_at > 0 AND tos_approval_ref != ''
                    )
                )
            );

            CREATE TABLE IF NOT EXISTS buzz_inbound_policies (
                agent TEXT PRIMARY KEY NOT NULL,
                community_id TEXT NOT NULL,
                relay_url TEXT NOT NULL,
                owner_pubkey TEXT NOT NULL
                    CHECK(length(owner_pubkey)=64 AND owner_pubkey=lower(owner_pubkey)
                          AND owner_pubkey NOT GLOB '*[^0-9a-f]*'),
                owner_configured_at REAL NOT NULL,
                owner_last_seen_at REAL NOT NULL DEFAULT 0,
                owner_silence_notified_at REAL NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'configured',
                last_connect_at REAL NOT NULL DEFAULT 0,
                last_liveness_at REAL NOT NULL DEFAULT 0,
                last_event_at REAL NOT NULL DEFAULT 0,
                last_error TEXT NOT NULL DEFAULT '',
                updated_by TEXT NOT NULL,
                updated_at REAL NOT NULL,
                FOREIGN KEY (agent) REFERENCES buzz_identities(agent) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS buzz_inbound_channels (
                agent TEXT NOT NULL,
                community_id TEXT NOT NULL,
                relay_url TEXT NOT NULL,
                channel_id TEXT NOT NULL,
                label TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL,
                FOREIGN KEY (agent) REFERENCES buzz_inbound_policies(agent) ON DELETE CASCADE,
                PRIMARY KEY (agent, community_id, channel_id)
            );

            CREATE TABLE IF NOT EXISTS buzz_inbound_principals (
                agent TEXT NOT NULL,
                community_id TEXT NOT NULL,
                pubkey TEXT NOT NULL
                    CHECK(length(pubkey)=64 AND pubkey=lower(pubkey)
                          AND pubkey NOT GLOB '*[^0-9a-f]*'),
                role TEXT NOT NULL CHECK(role IN ('owner', 'approved')),
                display_name TEXT NOT NULL DEFAULT '',
                approved_by TEXT NOT NULL,
                approved_at REAL NOT NULL,
                last_seen_at REAL NOT NULL DEFAULT 0,
                FOREIGN KEY (agent) REFERENCES buzz_inbound_policies(agent) ON DELETE CASCADE,
                PRIMARY KEY (agent, community_id, pubkey)
            );

            CREATE TABLE IF NOT EXISTS buzz_inbound_events (
                agent TEXT NOT NULL,
                event_id TEXT NOT NULL
                    CHECK(length(event_id)=64 AND event_id=lower(event_id)
                          AND event_id NOT GLOB '*[^0-9a-f]*'),
                community_id TEXT NOT NULL,
                channel_id TEXT NOT NULL,
                author_pubkey TEXT NOT NULL,
                kind INTEGER NOT NULL CHECK(kind=9),
                event_created_at REAL NOT NULL,
                event_json TEXT NOT NULL,
                delivery_status TEXT NOT NULL DEFAULT 'pending'
                    CHECK(delivery_status IN ('pending', 'delivered')),
                claimed_at REAL NOT NULL DEFAULT 0,
                delivered_at REAL NOT NULL DEFAULT 0,
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT NOT NULL DEFAULT '',
                FOREIGN KEY (agent) REFERENCES buzz_inbound_policies(agent) ON DELETE CASCADE,
                PRIMARY KEY (agent, event_id)
            );

            CREATE INDEX IF NOT EXISTS idx_buzz_inbound_events_pending
                ON buzz_inbound_events(agent, delivery_status, event_created_at);
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
            # HTTP create-only registration commits its ownership claim before
            # fallible provisioning/MCP publication. Pre-upgrade rows are all
            # completed registrations; new claim rows override this default to
            # 0 until finalize_registration() publishes bootstrap state.
            ("registration_finalized", "INTEGER NOT NULL DEFAULT 1"),
            ("heartbeat_interval", "INTEGER NOT NULL DEFAULT 0"),
            # Off by default — must match the dataclass + CREATE TABLE default (0).
            # A DEFAULT 1 here silently backfilled every pre-existing agent with
            # fallback ON, which leaked their internal reasoning into chats (the
            # broker also hard-gates fallback on group/public channels).
            ("plain_text_fallback", "INTEGER NOT NULL DEFAULT 0"),
            ("role", "TEXT NOT NULL DEFAULT ''"),
            ("users", "TEXT NOT NULL DEFAULT ''"),
            ("boundaries", "TEXT NOT NULL DEFAULT ''"),
            ("status", "TEXT NOT NULL DEFAULT 'active'"),
            ("retired_at", "REAL NOT NULL DEFAULT 0"),
            ("streaming_session_id", "TEXT NOT NULL DEFAULT ''"),
            ("wake_interval", "INTEGER NOT NULL DEFAULT 0"),
            ("clock_aligned", "INTEGER NOT NULL DEFAULT 1"),
            ("auto_sleep_hours", "INTEGER NOT NULL DEFAULT 0"),
            ("voice_config", "TEXT NOT NULL DEFAULT '{}'"),
            ("dream_enabled", "INTEGER NOT NULL DEFAULT 0"),
            ("dream_schedule", "TEXT NOT NULL DEFAULT '0 3 * * *'"),
            ("dream_timezone", "TEXT NOT NULL DEFAULT 'America/Los_Angeles'"),
            ("dream_model", "TEXT NOT NULL DEFAULT ''"),
            ("dream_notify", "INTEGER NOT NULL DEFAULT 1"),
            ("librarian_enabled", "INTEGER NOT NULL DEFAULT 0"),
            ("librarian_schedule", "TEXT NOT NULL DEFAULT '0 4 * * *'"),
            ("working_status", "TEXT NOT NULL DEFAULT 'idle'"),
            ("working_status_updated_at", "REAL NOT NULL DEFAULT 0"),
            ("provider_url", "TEXT NOT NULL DEFAULT ''"),
            ("provider_key", "TEXT NOT NULL DEFAULT ''"),
            ("provider_model", "TEXT NOT NULL DEFAULT ''"),
            ("provider_ref", "TEXT NOT NULL DEFAULT ''"),
            ("disallowed_tools", "TEXT NOT NULL DEFAULT '[]'"),
            ("thinking_effort", "TEXT NOT NULL DEFAULT 'medium'"),
            # When 1, verify_effort CLI hook blocks tool calls on effort drift
            # (vs. warn-only default of 0). See #429.
            ("strict_effort_enforcement", "INTEGER NOT NULL DEFAULT 0"),
            # Opt-in per-agent dedicated CLAUDE_CONFIG_DIR (own Claude account
            # for a LOCAL agent). Default 0 = shared ~/.claude (unchanged).
            ("dedicated_config_dir", "INTEGER NOT NULL DEFAULT 0"),
            ("watchdog_config", "TEXT NOT NULL DEFAULT '{}'"),
            ("last_seen_at", "REAL NOT NULL DEFAULT 0"),
            ("runtime", "TEXT NOT NULL DEFAULT 'claude_sdk'"),
            ("transport", "TEXT NOT NULL DEFAULT 'sdk'"),
            # Ferry peer-fleet ACL — list of AgentCardSelector dicts
            # (separate identity primitive from approved_users; default deny-all)
            ("peer_fleet_acl", "TEXT NOT NULL DEFAULT '[]'"),
            # Ferry outbound mesh allowlist — list of "agent@fleet" patterns
            # gating which targets this agent may publish to via
            # mesh_remote_send. Default-deny (empty list = no outbound).
            ("mesh_outbound_allowlist", "TEXT NOT NULL DEFAULT '[]'"),
            # Soft context-watermark nudge (#614); 0 = use global default.
            ("context_nudge_threshold_pct", "REAL NOT NULL DEFAULT 0.0"),
            # #149 tenant isolation: when 1, this agent is a hard-isolated
            # tenant (Counterpart) — scoped to ITSELF only. The daemon denies
            # it cross-agent actions (acting on a different agent's resources)
            # and admin/register_agent. Default 0 = full-trust inner-fleet agent
            # (no behavior change). Enforcement keys off the #623 per-agent-key
            # authenticated identity.
            ("isolated", "INTEGER NOT NULL DEFAULT 0"),
            # #149 phase-3: OS-level runtime sandbox for an isolated tenant.
            # 'local' = in-process under the daemon's user (default, current
            # behavior); 'unix_user' = own pinky-<agent> OS user (inc3b, Linux
            # exec only). Orthogonal to `isolated`; only meaningful when isolated.
            ("isolation_mode", "TEXT NOT NULL DEFAULT 'local'"),
            # Operator-supplied container image for isolation_mode="container".
            # Empty for other modes; bring-your-own (Pinky never builds it).
            ("container_image", "TEXT NOT NULL DEFAULT ''"),
            # Explicit per-agent CODEX_HOME. Inert unless the isolation flag is on.
            ("codex_home", "TEXT NOT NULL DEFAULT ''"),
        ]
        for col, typedef in migrations:
            if col not in existing:
                self._db.execute(f"ALTER TABLE agents ADD COLUMN {col} {typedef}")
                _log(f"agent_registry: migrated — added column {col}")
        # Structural belt for the exact persisted-root case. Legacy placeholder
        # values are excluded; resolved aliases and nested/enclosing roots still
        # require the BEGIN IMMEDIATE overlap check in register().
        self._db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_agents_working_dir_owner_exact "
            "ON agents(working_dir) WHERE working_dir NOT IN ('', '.')"
        )
        self._db.commit()
        self._backfill_runtime_from_provider_url()
        self._warn_codex_runtime_mismatches()
        self._backfill_signing_keys()

        # Migrate agent_schedules table
        sched_existing = {
            row[1] for row in self._db.execute("PRAGMA table_info(agent_schedules)").fetchall()
        }
        sched_migrations = [
            ("direct_send", "INTEGER NOT NULL DEFAULT 0"),
            ("target_channel", "TEXT NOT NULL DEFAULT ''"),
            ("one_shot", "INTEGER NOT NULL DEFAULT 0"),
            ("last_delivered", "REAL NOT NULL DEFAULT 0"),
        ]
        for col, typedef in sched_migrations:
            if col not in sched_existing:
                self._db.execute(f"ALTER TABLE agent_schedules ADD COLUMN {col} {typedef}")
                _log(f"agent_registry: migrated — added {col} to agent_schedules")

        # Migrate pending_schedule_wakes table
        wake_existing = {
            row[1]
            for row in self._db.execute(
                "PRAGMA table_info(pending_schedule_wakes)"
            ).fetchall()
        }
        wake_migrations = [
            ("attempts", "INTEGER NOT NULL DEFAULT 0"),
            ("parked_at", "REAL NOT NULL DEFAULT 0"),
            ("accepted_at", "REAL NOT NULL DEFAULT 0"),
            ("failed_at", "REAL NOT NULL DEFAULT 0"),
            ("last_error", "TEXT NOT NULL DEFAULT ''"),
            ("abandoned_at", "REAL NOT NULL DEFAULT 0"),
        ]
        for col, typedef in wake_migrations:
            if col not in wake_existing:
                self._db.execute(
                    f"ALTER TABLE pending_schedule_wakes ADD COLUMN {col} {typedef}"
                )
                _log(
                    "agent_registry: migrated — added "
                    f"{col} to pending_schedule_wakes"
                )
        self._db.execute(
            """CREATE INDEX IF NOT EXISTS idx_schedule_wake_ledger_state
               ON pending_schedule_wakes(
                   agent_name, accepted_at, parked_at, fired_at
               )"""
        )
        self._db.execute(
            """CREATE INDEX IF NOT EXISTS idx_schedule_wake_reaper_state
               ON pending_schedule_wakes(
                   accepted_at, parked_at, abandoned_at, fired_at
               )"""
        )
        self._db.commit()

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

        # Migrate agent_tokens table
        at_existing = {
            row[1] for row in self._db.execute("PRAGMA table_info(agent_tokens)").fetchall()
        }
        if "token_ref" not in at_existing:
            self._db.execute(
                "ALTER TABLE agent_tokens ADD COLUMN token_ref TEXT NOT NULL DEFAULT ''"
            )
            _log("agent_registry: migrated — added token_ref to agent_tokens")

        # Migrate approved_users table
        au_existing = {
            row[1] for row in self._db.execute("PRAGMA table_info(approved_users)").fetchall()
        }
        if "timezone" not in au_existing:
            self._db.execute("ALTER TABLE approved_users ADD COLUMN timezone TEXT NOT NULL DEFAULT ''")
            _log("agent_registry: migrated — added timezone to approved_users")

        # Migrate agent_contexts table
        ctx_existing = {
            row[1] for row in self._db.execute("PRAGMA table_info(agent_contexts)").fetchall()
        }
        if "wake_action" not in ctx_existing:
            self._db.execute(
                "ALTER TABLE agent_contexts ADD COLUMN wake_action TEXT NOT NULL DEFAULT ''"
            )
            _log("agent_registry: migrated — added wake_action to agent_contexts")

        # Migrate pending_messages table — reply_chat_id preserves the true reply
        # destination (e.g. the Slack/Telegram channel a group message arrived in).
        # The chat_id column is the per-user approval key (the sender's id); in a
        # group/channel that differs from the destination, so without a separate
        # field a held message would be re-delivered to the sender's DM instead of
        # the channel. Backfill to chat_id so pre-existing rows keep their prior
        # (DM-correct) behavior.
        pm_existing = {
            row[1] for row in self._db.execute("PRAGMA table_info(pending_messages)").fetchall()
        }
        if "reply_chat_id" not in pm_existing:
            self._db.execute(
                "ALTER TABLE pending_messages ADD COLUMN reply_chat_id TEXT NOT NULL DEFAULT ''"
            )
            self._db.execute(
                "UPDATE pending_messages SET reply_chat_id=chat_id WHERE reply_chat_id=''"
            )
            _log("agent_registry: migrated — added reply_chat_id to pending_messages")
        # is_group lets a held group/channel message re-deliver with the correct
        # group context on approval (so the prompt header + fallback gate treat
        # it as a channel, not a DM). Defaults 0 = DM, matching prior rows.
        if "is_group" not in pm_existing:
            self._db.execute(
                "ALTER TABLE pending_messages ADD COLUMN is_group INTEGER NOT NULL DEFAULT 0"
            )
            _log("agent_registry: migrated — added is_group to pending_messages")
        # sender_id is the original human sender, stored separately from the
        # approval-key chat_id (which is the channel for group messages). Backfill
        # to chat_id for existing rows — pre-#241 the approval key WAS the sender.
        if "sender_id" not in pm_existing:
            self._db.execute(
                "ALTER TABLE pending_messages ADD COLUMN sender_id TEXT NOT NULL DEFAULT ''"
            )
            self._db.execute(
                "UPDATE pending_messages SET sender_id=chat_id WHERE sender_id=''"
            )
            _log("agent_registry: migrated — added sender_id to pending_messages")

        # Approval maintenance metadata (#998). Aging re-prompts are bounded
        # independently from transport retries/new-message notifications, and
        # high-signal alerts are durable so one approved principal does not
        # page the owner on every message while its channel awaits approval.
        ar_existing = {
            row[1] for row in self._db.execute("PRAGMA table_info(approval_requests)").fetchall()
        }
        if "aging_reprompt_count" not in ar_existing:
            self._db.execute(
                "ALTER TABLE approval_requests "
                "ADD COLUMN aging_reprompt_count INTEGER NOT NULL DEFAULT 0"
            )
            _log("agent_registry: migrated — added aging_reprompt_count to approval_requests")
        if "high_signal_alerted_at" not in ar_existing:
            self._db.execute(
                "ALTER TABLE approval_requests "
                "ADD COLUMN high_signal_alerted_at REAL NOT NULL DEFAULT 0"
            )
            _log("agent_registry: migrated — added high_signal_alerted_at to approval_requests")

        # Buzz identity storage is forward-migrated column-by-column rather
        # than relying only on CREATE TABLE. This keeps fleet-local DBs safe if
        # an increment adds lifecycle metadata after the first rollout.
        buzz_existing = {
            row[1] for row in self._db.execute("PRAGMA table_info(buzz_identities)").fetchall()
        }
        relay_signing_pubkey_added = "relay_signing_pubkey" not in buzz_existing
        buzz_migrations = [
            ("pubkey", "TEXT NOT NULL DEFAULT ''"),
            ("wrap_version", "INTEGER NOT NULL DEFAULT 1"),
            ("nonce", "BLOB NOT NULL DEFAULT X''"),
            ("ciphertext", "BLOB NOT NULL DEFAULT X''"),
            ("relay_url", "TEXT NOT NULL DEFAULT ''"),
            ("community_id", "TEXT NOT NULL DEFAULT ''"),
            ("relay_signing_pubkey", "TEXT NOT NULL DEFAULT ''"),
            ("enabled", "INTEGER NOT NULL DEFAULT 0"),
            ("status", "TEXT NOT NULL DEFAULT 'disabled'"),
            ("last_error", "TEXT NOT NULL DEFAULT ''"),
            ("tos_receipt", "TEXT NOT NULL DEFAULT ''"),
            ("tos_approved_by", "TEXT NOT NULL DEFAULT ''"),
            ("tos_approved_at", "REAL NOT NULL DEFAULT 0"),
            ("tos_approval_ref", "TEXT NOT NULL DEFAULT ''"),
            ("created_at", "REAL NOT NULL DEFAULT 0"),
            ("updated_at", "REAL NOT NULL DEFAULT 0"),
        ]
        for col, typedef in buzz_migrations:
            if col not in buzz_existing:
                self._db.execute(f"ALTER TABLE buzz_identities ADD COLUMN {col} {typedef}")
                _log(f"agent_registry: migrated — added {col} to buzz_identities")
        if relay_signing_pubkey_added:
            # One-time deployment migration for the operator-verified production
            # authority. New/future identities receive their own explicit pin at
            # provisioning; migrations never fetch NIP-11 or trust network data.
            cursor = self._db.execute(
                "UPDATE buzz_identities SET relay_signing_pubkey=? "
                "WHERE agent='barsik' AND relay_signing_pubkey=''",
                (_BARSIK_BUZZ_RELAY_SIGNING_PUBKEY,),
            )
            if cursor.rowcount:
                _log("agent_registry: seeded barsik Buzz relay signing authority")
        self._db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_buzz_identities_pubkey "
            "ON buzz_identities(pubkey) WHERE pubkey != ''"
        )
        self._db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_buzz_identities_approval_ref "
            "ON buzz_identities(tos_approval_ref) WHERE tos_approval_ref != ''"
        )
        self._db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_buzz_identities_tos_receipt "
            "ON buzz_identities(tos_receipt) WHERE tos_receipt != ''"
        )

        # Deployment seed for the explicitly verified owner principal in #545.
        # The registry and all runtime lookups remain agent-keyed; this is only
        # the caller-specified bootstrap row and never auto-learns from traffic.
        self._seed_verified_contacts()

        # Seed main_agent default: if unset, adopt the oldest enabled agent.
        # New installs get their main agent auto-assigned at create time (see
        # ``register``); this migration covers pre-existing installs whose
        # main_agent was never set (e.g. agents created via the API before
        # auto-assignment landed). Name-agnostic — no hardcoded agent name.
        if not self.get_setting("main_agent"):
            row = self._db.execute(
                "SELECT name FROM agents WHERE enabled=1 "
                "ORDER BY created_at ASC, name ASC LIMIT 1",
            ).fetchone()
            if row:
                self.set_setting("main_agent", row[0])
                _log(f"agent_registry: seeded main_agent={row[0]}")

        self._db.commit()

        # Seed default models
        self._seed_models()

    def _seed_verified_contacts(self, *, _commit: bool = True) -> None:
        """Install caller-specified contacts for a finalized registration."""
        marker = "migration:verified_contacts_brad_owner_seed_v1"
        if self.get_setting(marker) == "1":
            return
        if self._db.execute(
            "SELECT 1 FROM agents "
            "WHERE name='barsik' AND registration_finalized=1"
        ).fetchone() is None:
            return
        try:
            cursor = self._db.execute(
                """INSERT INTO verified_contacts
                   (agent_name, platform, principal, name, role, added_at)
                   VALUES ('barsik', 'buzz', ?, 'Brad', 'owner', ?)
                   ON CONFLICT(agent_name, platform, principal) DO UPDATE SET
                     name='Brad', role='owner'""",
                (
                    "buzz:posspecialists:"
                    "90425c785cf23b60e57300658a7f4855938b3c2f661b3ef33acdb54831fcb44b",
                    time.time(),
                ),
            )
            self._db.execute(
                """INSERT INTO system_settings (key, value) VALUES (?, '1')
                   ON CONFLICT(key) DO UPDATE SET value='1'""",
                (marker,),
            )
            if _commit:
                self._db.commit()
        except Exception:
            if _commit:
                self._db.rollback()
            raise
        if cursor.rowcount:
            _log("agent_registry: seeded barsik Buzz owner verified contact")

    def finalize_registration(self, name: str) -> None:
        """Atomically finalize registration and publish its bootstrap state."""
        name = _validate_agent_name(name)
        with self._rmw_lock:
            try:
                self._db.execute("BEGIN IMMEDIATE")
                cursor = self._db.execute(
                    "UPDATE agents SET registration_finalized=1 WHERE name=?",
                    (name,),
                )
                if cursor.rowcount == 0:
                    raise KeyError(f"Agent '{name}' not found")
                self._seed_verified_contacts(_commit=False)
                self._db.commit()
            except Exception:
                self._db.rollback()
                raise

    def _backfill_runtime_from_provider_url(self) -> None:
        """One-shot migration from legacy provider_url runtime selection."""
        marker = "migration:agents_runtime_codex_cli_backfill"
        if self.get_setting(marker) == "1":
            return

        cursor = self._db.execute(
            "UPDATE agents SET runtime='codex_cli', transport='sdk' "
            "WHERE provider_url='codex_cli' AND runtime='claude_sdk'"
        )
        self._db.execute(
            "INSERT INTO system_settings (key, value) VALUES (?, '1') "
            "ON CONFLICT(key) DO UPDATE SET value='1'",
            (marker,),
        )
        self._db.commit()
        if cursor.rowcount:
            _log(f"agent_registry: backfilled runtime=codex_cli for {cursor.rowcount} agent(s)")

    def _warn_codex_runtime_mismatches(self) -> None:
        """Warn when Codex provider rows still have the Claude SDK runtime.

        The one-shot migration above covers rows that existed when the runtime
        column landed. Rows created after that marker was set can still drift if
        a caller persists provider_url='codex_cli' without runtime='codex_cli'.
        Warn only: startup should not mutate post-migration rows unexpectedly.
        """
        rows = self._db.execute(
            "SELECT name FROM agents "
            "WHERE provider_url='codex_cli' AND runtime='claude_sdk' "
            "ORDER BY name"
        ).fetchall()
        if not rows:
            return

        sample = ", ".join(row[0] for row in rows[:5])
        suffix = "" if len(rows) <= 5 else f", +{len(rows) - 5} more"
        _log(
            "agent_registry: warning: "
            f"{len(rows)} Codex CLI agent(s) have runtime=claude_sdk "
            f"({sample}{suffix}); run scripts/backfill_codex_runtime.py"
        )

    # ── Workspace Init ─────────────────────────────────────

    def ensure_workspace_hooks(self, agent_name: str) -> None:
        """Re-run hook setup for an existing agent's workspace.

        Idempotent. Use to install new hooks (e.g. ``hook_verify_effort.py``
        from #429) on agents whose workspace pre-dates them, without nuking
        any user customizations to existing scripts.
        """
        agent_name = _validate_agent_name(agent_name)
        agent = self.get(agent_name)
        if not agent or not agent.working_dir:
            return
        work_dir = resolve_agent_path(agent_name, agent.working_dir)
        if not work_dir.exists():
            return
        try:
            self._setup_hooks(work_dir, agent_name)
        except Exception as e:  # pragma: no cover — defensive
            _log(f"agent_registry: ensure_workspace_hooks({agent_name}) failed: {e}")

    @staticmethod
    def _init_workspace(work_dir: Path, agent_name: str = "") -> None:
        """Create an agent workspace with default directory structure.

        Creates:
            workspace/
            ├── data/           # SQLite databases (memory.db, etc.)
            ├── output/         # Agent-generated output (reports, exports)
            ├── .claude/        # Claude Code hooks + settings
            └── CLAUDE.md       # Written by spawn, not here
        """
        if agent_name:
            work_dir = resolve_agent_path(agent_name, work_dir)
            data_dir = resolve_agent_path(agent_name, work_dir, "data")
            output_dir = resolve_agent_path(agent_name, work_dir, "output")
            workspace_dir = resolve_agent_path(agent_name, work_dir, "workspace")
        else:
            work_dir = Path(work_dir).resolve()
            data_dir = work_dir / "data"
            output_dir = work_dir / "output"
            workspace_dir = work_dir / "workspace"
        try:
            work_dir.mkdir(parents=True, exist_ok=True)
            data_dir.mkdir(exist_ok=True)
            output_dir.mkdir(exist_ok=True)
            workspace_dir.mkdir(exist_ok=True)
        except PermissionError:
            _log(f"agent_registry: workspace init skipped for {work_dir} (permission denied)")
            return

        # Set up Claude Code hooks for working/idle status
        if agent_name:
            AgentRegistry._setup_hooks(work_dir, agent_name)

    @staticmethod
    def _write_hook_if_changed(
        *,
        agent_dir: Path,
        hook_path: Path,
        new_source: str,
        hook_filename: str,
        agent_name: str,
    ) -> None:
        """Rewrite a pinky-managed hook file when its content changed.

        No-op when on-disk content already matches ``new_source``. The
        read/compare/write sequence is identical for every hook script
        managed under ``.claude/`` (verify_effort, tmux_wake,
        tmux_session_start, tmux_pre_tool, tmux_post_tool); the helper
        consolidates it in one place.

        ``agent_name`` is re-validated as a sanitizer signal for static
        analysis — every path passing through this helper is rooted in
        an agent_name that matches the safe-char allowlist (see
        ``_validate_agent_name``). The validation is cheap and raises
        ``ValueError`` on bad input rather than corrupting disk layout.
        """
        hook_path = resolve_agent_path(agent_name, agent_dir, hook_path)
        existing = hook_path.read_text() if hook_path.exists() else ""
        if existing == new_source:
            return
        replace_agent_text(agent_name, agent_dir, hook_path, new_source)
        verb = "updated" if existing else "created"
        _log(f"agent_registry: {verb} {hook_filename} for {agent_name}")

    @staticmethod
    def _setup_hooks(work_dir: Path, agent_name: str) -> None:
        """Generate Claude Code hooks for agent working/idle status reporting
        and (since #429) effort-drift verification.

        Creates ``.claude/`` directory with hook scripts and settings.json.
        Existing scripts are not overwritten; settings.json is idempotently
        merged so the verify_effort hook can be added to agents whose
        settings predate #429 without nuking their existing hooks.
        """
        # Re-validate even though ``register()`` already did. ``_setup_hooks``
        # writes hook scripts whose paths depend on ``agent_name``; explicit
        # sanitization here means CodeQL's taint analysis sees the sanitizer
        # at the source of every path-construction site below, not just at
        # the public entry point. Cheap re-check; raises ``ValueError`` on
        # bad input so the daemon crashes loudly rather than corrupting
        # filesystem layout.
        work_dir = resolve_agent_path(agent_name, work_dir)
        claude_dir = resolve_agent_path(agent_name, work_dir, ".claude")
        claude_dir.mkdir(exist_ok=True)

        hook_template = '''\
#!/usr/bin/env python3
import hashlib, hmac, base64, time, urllib.request, json, os, subprocess, sys, tempfile

agent = "{agent_name}"
status = "{status}"

def emit_failure(literal, detail):
    message = "%s agent=%s status=%s error=%s" % (literal, agent, status, detail)
    # The hook runs inside a tmux pane, whose stderr is not guaranteed to
    # reach the daemon's systemd journal. Send failures to the host logger as
    # the durable operator receipt, with stderr as a portable fallback.
    try:
        subprocess.run(
            ["logger", "-t", "pinkybot-status-hook", message],
            timeout=1,
            check=False,
        )
    except Exception:
        pass
    print(message, file=sys.stderr, flush=True)

secret = os.environ.get("PINKY_AGENT_KEY", "").strip() or os.environ.get("PINKY_SESSION_SECRET", "").strip()
if not secret:
    # One durable receipt per tmux/Claude session. Hook commands are separate
    # subprocesses, so an O_EXCL marker keyed by their inherited session ID is
    # the cheap cross-invocation once guard.
    marker = os.path.join(
        tempfile.gettempdir(),
        "pinkybot-status-hook-%s-%s.missing-secret" % (agent, os.getsid(0)),
    )
    try:
        fd = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        pass
    except Exception as exc:
        emit_failure("STATUS_HOOK_SECRET_MISSING", "marker: %s" % exc)
    else:
        os.close(fd)
        emit_failure("STATUS_HOOK_SECRET_MISSING", "no signing key in hook env")
    sys.exit(0)

path = "/agents/{agent_name}/status"
ts = int(time.time())
payload = f"{{agent}}\\nPOST\\n{{path}}\\n{{ts}}".encode()
digest = hmac.new(secret.encode(), payload, hashlib.sha256).digest()
sig = base64.urlsafe_b64encode(digest).decode().rstrip("=")

req = urllib.request.Request(
    os.environ.get("PINKY_DAEMON_URL", "http://localhost:8888").rstrip("/") + path,
    data=json.dumps({{"status": "{status}"}}).encode(),
    method="POST",
)
req.add_header("Content-Type", "application/json")
req.add_header("x-pinky-agent", agent)
req.add_header("x-pinky-timestamp", str(ts))
req.add_header("x-pinky-signature", sig)
try:
    urllib.request.urlopen(req, timeout=2)
except Exception as exc:
    emit_failure(
        "STATUS_HOOK_POST_FAILURE",
        "%s: %s" % (type(exc).__name__, exc),
    )
'''
        working_path = resolve_agent_path(agent_name, work_dir, ".claude", "hook_working.py")
        idle_path = resolve_agent_path(agent_name, work_dir, ".claude", "hook_idle.py")
        verify_effort_path = resolve_agent_path(
            agent_name, work_dir, ".claude", "hook_verify_effort.py"
        )
        tmux_wake_path = resolve_agent_path(
            agent_name, work_dir, ".claude", "hook_tmux_wake.py"
        )
        tmux_session_start_path = resolve_agent_path(
            agent_name, work_dir, ".claude", "hook_tmux_session_start.py"
        )
        tmux_pre_tool_path = resolve_agent_path(
            agent_name, work_dir, ".claude", "hook_tmux_pre_tool.py"
        )
        tmux_post_tool_path = resolve_agent_path(
            agent_name, work_dir, ".claude", "hook_tmux_post_tool.py"
        )
        tmux_stop_failure_path = resolve_agent_path(
            agent_name, work_dir, ".claude", "hook_tmux_stop_failure.py"
        )

        # #638: these two were historically written once and left alone, which
        # stranded fleet agents on stale sources (e.g. the hardcoded
        # http://localhost:8888 that is dead inside a container netns). They
        # are fully PinkyBot-managed, so keep them current like the five
        # always-rewritten hooks below.
        AgentRegistry._write_hook_if_changed(
            agent_dir=work_dir,
            hook_path=working_path,
            new_source=hook_template.format(agent_name=agent_name, status="working"),
            hook_filename="hook_working.py",
            agent_name=agent_name,
        )
        AgentRegistry._write_hook_if_changed(
            agent_dir=work_dir,
            hook_path=idle_path,
            new_source=hook_template.format(agent_name=agent_name, status="idle"),
            hook_filename="hook_idle.py",
            agent_name=agent_name,
        )

        # #429: verify_effort hook — compares $CLAUDE_EFFORT (v2.1.133+) to
        # PINKY_EXPECTED_EFFORT (set by daemon at session start). On drift,
        # posts to /agents/{name}/effort-drift; under strict mode also emits
        # a block decision so Claude Code refuses the tool call.
        #
        # PR8b: TmuxSession response capture pipeline adds two more hooks:
        #   - hook_tmux_wake.py: fires on Stop, POSTs /transport/wake
        #   - hook_tmux_session_start.py: SessionStart, POSTs transcript_path
        #
        # Task #93: PreToolUse + PostToolUse hooks for tmux tool-use tracking.
        #
        # All five are ALWAYS rewritten (unlike hook_working / hook_idle which
        # are left alone if present) — they're fully PinkyBot-managed and
        # getting the latest semantics on disk matters across releases. The
        # tmux hooks are installed unconditionally; the daemon endpoint
        # returns ``ok: True, session: None`` for non-tmux runtimes, so each
        # is a cheap no-op for SDK / codex agents (one extra POST per turn).
        AgentRegistry._write_hook_if_changed(
            agent_dir=work_dir,
            hook_path=verify_effort_path,
            new_source=_verify_effort_hook_source(),
            hook_filename="hook_verify_effort.py",
            agent_name=agent_name,
        )
        AgentRegistry._write_hook_if_changed(
            agent_dir=work_dir,
            hook_path=tmux_wake_path,
            new_source=_tmux_wake_hook_source(agent_name),
            hook_filename="hook_tmux_wake.py",
            agent_name=agent_name,
        )
        AgentRegistry._write_hook_if_changed(
            agent_dir=work_dir,
            hook_path=tmux_session_start_path,
            new_source=_tmux_session_start_hook_source(agent_name),
            hook_filename="hook_tmux_session_start.py",
            agent_name=agent_name,
        )
        AgentRegistry._write_hook_if_changed(
            agent_dir=work_dir,
            hook_path=tmux_pre_tool_path,
            new_source=_tmux_pre_tool_hook_source(agent_name),
            hook_filename="hook_tmux_pre_tool.py",
            agent_name=agent_name,
        )
        AgentRegistry._write_hook_if_changed(
            agent_dir=work_dir,
            hook_path=tmux_post_tool_path,
            new_source=_tmux_post_tool_hook_source(agent_name),
            hook_filename="hook_tmux_post_tool.py",
            agent_name=agent_name,
        )
        AgentRegistry._write_hook_if_changed(
            agent_dir=work_dir,
            hook_path=tmux_stop_failure_path,
            new_source=_tmux_stop_failure_hook_source(agent_name),
            hook_filename="hook_tmux_stop_failure.py",
            agent_name=agent_name,
        )

        AgentRegistry._sync_hooks_settings(
            resolve_agent_path(agent_name, work_dir, ".claude", "settings.json"),
            agent_dir=work_dir,
            working_path=working_path.resolve(),
            idle_path=idle_path.resolve(),
            verify_effort_path=verify_effort_path.resolve(),
            tmux_wake_path=tmux_wake_path.resolve(),
            tmux_session_start_path=tmux_session_start_path.resolve(),
            tmux_pre_tool_path=tmux_pre_tool_path.resolve(),
            tmux_post_tool_path=tmux_post_tool_path.resolve(),
            tmux_stop_failure_path=tmux_stop_failure_path.resolve(),
            agent_name=agent_name,
        )

    @staticmethod
    def _sync_hooks_settings(
        settings_path: Path,
        *,
        agent_dir: Path,
        working_path: Path,
        idle_path: Path,
        verify_effort_path: Path,
        tmux_wake_path: Path,
        tmux_session_start_path: Path,
        tmux_pre_tool_path: Path,
        tmux_post_tool_path: Path,
        tmux_stop_failure_path: Path,
        agent_name: str,
    ) -> None:
        """Idempotently ensure settings.json has all PinkyBot hooks wired up.

        - Creates settings.json with the full hook set if missing.
        - If present, adds any missing PinkyBot-managed hook entries to
          PreToolUse / PostToolUse / Stop / SessionStart, preserving
          user-added entries.
        - Identifies PinkyBot-managed entries by the absolute script path
          appearing in the command string.
        """
        import json as _json

        settings_path = resolve_agent_path(agent_name, agent_dir, settings_path)
        working_path = resolve_agent_path(agent_name, agent_dir, working_path)
        idle_path = resolve_agent_path(agent_name, agent_dir, idle_path)
        verify_effort_path = resolve_agent_path(
            agent_name,
            agent_dir,
            verify_effort_path,
        )
        tmux_wake_path = resolve_agent_path(agent_name, agent_dir, tmux_wake_path)
        tmux_session_start_path = resolve_agent_path(
            agent_name,
            agent_dir,
            tmux_session_start_path,
        )
        tmux_pre_tool_path = resolve_agent_path(
            agent_name,
            agent_dir,
            tmux_pre_tool_path,
        )
        tmux_post_tool_path = resolve_agent_path(
            agent_name,
            agent_dir,
            tmux_post_tool_path,
        )
        tmux_stop_failure_path = resolve_agent_path(
            agent_name,
            agent_dir,
            tmux_stop_failure_path,
        )

        # Quote script paths: a working_dir containing spaces would otherwise
        # make python3 open a nonexistent file, and the trailing
        # "|| true" would swallow the failure silently.
        verify_cmd = (
            f"python3 {shlex.quote(str(verify_effort_path))}"
            f' "$CLAUDE_PROJECT_DIR" 2>/dev/null || true'
        )
        # Do not suppress stderr: STATUS_HOOK_POST_FAILURE is a release
        # receipt when the direct POST (and possibly host logger) fails.
        working_cmd = f"python3 {shlex.quote(str(working_path))} || true"
        idle_cmd = f"python3 {shlex.quote(str(idle_path))} || true"
        tmux_wake_cmd = f"python3 {shlex.quote(str(tmux_wake_path))} 2>/dev/null || true"
        tmux_session_start_cmd = (
            f"python3 {shlex.quote(str(tmux_session_start_path))} 2>/dev/null || true"
        )
        tmux_pre_tool_cmd = f"python3 {shlex.quote(str(tmux_pre_tool_path))} 2>/dev/null || true"
        tmux_post_tool_cmd = f"python3 {shlex.quote(str(tmux_post_tool_path))} 2>/dev/null || true"
        tmux_stop_failure_cmd = (
            f"python3 {shlex.quote(str(tmux_stop_failure_path))} 2>/dev/null || true"
        )

        if not settings_path.exists():
            settings = {
                "hooks": {
                    "PreToolUse": [
                        {
                            "matcher": ".*",
                            "hooks": [
                                {"type": "command", "command": working_cmd},
                                {"type": "command", "command": verify_cmd},
                                # Task #93: tool-use start (no-op for non-tmux).
                                {"type": "command", "command": tmux_pre_tool_cmd},
                            ],
                        }
                    ],
                    "PostToolUse": [
                        {
                            "matcher": ".*",
                            "hooks": [
                                # Task #93: tool-use finish (no-op for non-tmux).
                                {"type": "command", "command": tmux_post_tool_cmd},
                            ],
                        }
                    ],
                    "Stop": [
                        {
                            "matcher": ".*",
                            "hooks": [
                                {"type": "command", "command": idle_cmd},
                                # PR8b: wake the tmux response tailer on Stop.
                                # No-op for non-tmux agents (daemon returns
                                # 200 session: None).
                                {"type": "command", "command": tmux_wake_cmd},
                            ],
                        }
                    ],
                    "SessionStart": [
                        {
                            # SessionStart fires on startup/resume/clear/compact.
                            # Match all so the tailer is repointed any time the
                            # transcript file might have changed.
                            "matcher": ".*",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": tmux_session_start_cmd,
                                },
                            ],
                        }
                    ],
                    "StopFailure": [
                        {
                            # StopFailure fires when a turn ends on an API error.
                            # matcher ".*" catches every error class; the hook
                            # forwards the typed failure (CC's ``error`` field)
                            # so the daemon can alert on auth-class failures and
                            # log the rest (rate_limit / billing).
                            "matcher": ".*",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": tmux_stop_failure_cmd,
                                },
                            ],
                        }
                    ],
                }
            }
            replace_agent_text(
                agent_name,
                agent_dir,
                settings_path,
                _json.dumps(settings, indent=2) + "\n",
            )
            _log(f"agent_registry: created settings.json for {agent_name}")
            return

        # Merge path — add any missing PinkyBot-managed entries. We use
        # absolute script paths as needles so user-renamed copies aren't
        # mistaken for already-installed hooks.
        try:
            data = _json.loads(settings_path.read_text())
        except Exception as e:
            _log(
                f"agent_registry: settings.json parse failed for {agent_name}: {e}; "
                "skipping merge"
            )
            return

        changed = False
        hooks = data.setdefault("hooks", {})

        # Working/idle status hooks predate the managed merge path.  Existing
        # settings.json files could therefore retain SessionStart/tmux hooks
        # while silently missing the only writers of live_status.  Repair both
        # on every workspace sync, just like the newer managed hooks.
        changed |= AgentRegistry._merge_hook_into_event(
            hooks, "PreToolUse",
            needle=str(working_path),
            command=working_cmd,
        )
        changed |= AgentRegistry._merge_hook_into_event(
            hooks, "Stop",
            needle=str(idle_path),
            command=idle_cmd,
        )

        # verify_effort hook → PreToolUse bucket
        changed |= AgentRegistry._merge_hook_into_event(
            hooks, "PreToolUse",
            needle=str(verify_effort_path),
            command=verify_cmd,
        )

        # tmux_wake hook → Stop bucket
        changed |= AgentRegistry._merge_hook_into_event(
            hooks, "Stop",
            needle=str(tmux_wake_path),
            command=tmux_wake_cmd,
        )

        # tmux_session_start hook → SessionStart bucket
        changed |= AgentRegistry._merge_hook_into_event(
            hooks, "SessionStart",
            needle=str(tmux_session_start_path),
            command=tmux_session_start_cmd,
        )

        # Task #93: tmux_pre_tool → PreToolUse bucket
        changed |= AgentRegistry._merge_hook_into_event(
            hooks, "PreToolUse",
            needle=str(tmux_pre_tool_path),
            command=tmux_pre_tool_cmd,
        )

        # Task #93: tmux_post_tool → PostToolUse bucket (new bucket if needed)
        changed |= AgentRegistry._merge_hook_into_event(
            hooks, "PostToolUse",
            needle=str(tmux_post_tool_path),
            command=tmux_post_tool_cmd,
        )

        # tmux_stop_failure → StopFailure bucket (new bucket if needed).
        # Backfills agents whose settings.json predates the hook.
        changed |= AgentRegistry._merge_hook_into_event(
            hooks, "StopFailure",
            needle=str(tmux_stop_failure_path),
            command=tmux_stop_failure_cmd,
        )

        if changed:
            replace_agent_text(
                agent_name,
                agent_dir,
                settings_path,
                _json.dumps(data, indent=2) + "\n",
            )
            _log(
                f"agent_registry: merged PinkyBot hooks into settings.json "
                f"for {agent_name}"
            )

    @staticmethod
    def _merge_hook_into_event(
        hooks: dict, event: str, *, needle: str, command: str,
    ) -> bool:
        """Insert ``command`` into ``hooks[event]`` if no entry containing
        ``needle`` exists. Returns True iff the structure was modified.

        Targets (or creates) the matcher=``.*`` bucket within the event.
        Idempotent — re-running this with the same args does nothing.
        """
        event_list = hooks.setdefault(event, [])
        for entry in event_list:
            for h in entry.get("hooks", []):
                if needle in (h.get("command") or ""):
                    if h.get("type") == "command" and h.get("command") == command:
                        return False
                    # PinkyBot-managed hook paths are the identity.  Upgrade
                    # stale command wrappers in place (for example, remove the
                    # historical ``2>/dev/null`` status-hook sink) instead of
                    # treating any path match as permanently current.
                    h["type"] = "command"
                    h["command"] = command
                    return True

        target_bucket = None
        for entry in event_list:
            if entry.get("matcher") == ".*":
                target_bucket = entry
                break
        if target_bucket is None:
            target_bucket = {"matcher": ".*", "hooks": []}
            event_list.append(target_bucket)

        target_bucket.setdefault("hooks", []).append(
            {"type": "command", "command": command}
        )
        return True

    # ── Agent CRUD ──────────────────────────────────────────

    @staticmethod
    def _markdown_heading_text(line: str, level: int) -> str | None:
        """Parse one ATX heading in linear time, without regex backtracking."""
        prefix = "#" * level
        if not line.startswith(prefix):
            return None
        if len(line) == level or not line[level].isspace():
            return None
        body = line[level:].lstrip()
        if not body:
            return None
        closing_start = len(body)
        while closing_start and body[closing_start - 1] == "#":
            closing_start -= 1
        if closing_start < len(body) and body[closing_start - 1].isspace():
            body = body[:closing_start].rstrip()
        return body or None

    @staticmethod
    def _has_identity_label(line: str, label: str) -> bool:
        """Recognize the legacy name/role label prefix in linear time."""
        starts = [0]
        if line[:1] in {"-", "*"}:
            after_bullet = 1
            while after_bullet < len(line) and line[after_bullet].isspace():
                after_bullet += 1
            starts.append(after_bullet)
        for start in starts:
            cursor = start
            if line[cursor : cursor + 2] == "**":
                cursor += 2
            end = cursor + len(label)
            if line[cursor:end].casefold() != label:
                continue
            cursor = end
            while cursor < len(line) and line[cursor].isspace():
                cursor += 1
            if cursor < len(line) and line[cursor] == ":":
                return True
        return False

    @staticmethod
    def _soul_identity_anchors(
        content: str,
        *,
        agent_name: str,
        display_name: str = "",
    ) -> set[str]:
        """Return recognized identity structure without retaining its text."""
        anchors: set[str] = set()
        identities = {
            value.strip().casefold()
            for value in (agent_name, display_name)
            if value and value.strip()
        }
        for raw_line in content.splitlines():
            line = raw_line.strip()
            heading = AgentRegistry._markdown_heading_text(line, 1)
            if heading and heading.casefold() in identities:
                anchors.add("agent_heading")
            identity_heading = AgentRegistry._markdown_heading_text(line, 2)
            if identity_heading and identity_heading.casefold() == "identity":
                anchors.add("identity_heading")
            if AgentRegistry._has_identity_label(line, "name"):
                anchors.add("name_label")
            if AgentRegistry._has_identity_label(line, "role"):
                anchors.add("role_label")
        return anchors

    def assess_soul_mutation(
        self,
        name: str,
        old_content: str,
        new_content: str,
        *,
        display_name: str = "",
    ) -> SoulMutationSummary:
        """Describe a proposed soul replacement without exposing soul text."""
        name = _validate_agent_name(name)
        if not isinstance(old_content, str) or not isinstance(new_content, str):
            raise TypeError("soul content must be text")
        old_length = len(old_content)
        new_length = len(new_content)
        shrink_percent = (
            round(max(0.0, (old_length - new_length) * 100.0 / old_length), 2)
            if old_length
            else 0.0
        )
        old_anchors = self._soul_identity_anchors(
            old_content,
            agent_name=name,
            display_name=display_name,
        )
        new_anchors = self._soul_identity_anchors(
            new_content,
            agent_name=name,
            display_name=display_name,
        )
        return SoulMutationSummary(
            old_length=old_length,
            new_length=new_length,
            shrink_percent=shrink_percent,
            missing_anchors=tuple(sorted(old_anchors - new_anchors)),
        )

    def guard_soul_mutation(
        self,
        name: str,
        old_content: str,
        new_content: str,
        *,
        display_name: str = "",
        force_soul: bool = False,
    ) -> SoulMutationSummary:
        """Reject destructive soul replacement unless explicitly forced."""
        summary = self.assess_soul_mutation(
            name,
            old_content,
            new_content,
            display_name=display_name,
        )
        shrinks_more_than_half = summary.new_length * 2 < summary.old_length
        if not force_soul and (shrinks_more_than_half or summary.missing_anchors):
            raise SoulMutationRejectedError(summary)
        return summary

    def update(self, name: str, *, _commit: bool = True, **kwargs) -> Agent:
        """Partially update an existing agent without creating one.

        Only explicitly supplied, allowlisted fields are changed.  This is
        intentionally distinct from :meth:`register`: callers that mean to
        mutate durable defaults must not accidentally create a half-populated
        agent or reset fields omitted from a PATCH-like request.
        """
        name = _validate_agent_name(name)
        force_soul = bool(kwargs.pop("force_soul", False))
        soul_source = str(kwargs.pop("soul_source", "registry-update") or "registry-update")
        with self._rmw_lock:
            existing = self.get(name)
            if not existing:
                raise KeyError(f"Agent '{name}' not found")
            if "working_dir" in kwargs:
                raise ValueError(
                    "working_dir is not supported by partial update; use register()"
                )

            updates = {}
            for key in ("display_name", "model", "soul", "users", "boundaries",
                        "system_prompt",
                        "permission_mode", "max_turns", "timeout", "restart_threshold_pct",
                        "context_nudge_threshold_pct",
                        "auto_restart", "parent", "max_sessions", "enabled",
                        "auto_start", "heartbeat_interval", "wake_interval",
                        "clock_aligned", "auto_sleep_hours", "plain_text_fallback", "voice_config", "role",
                        "dream_enabled", "dream_schedule", "dream_timezone", "dream_model", "dream_notify",
                        "librarian_enabled", "librarian_schedule",
                        "runtime", "transport", "provider_url", "provider_model", "provider_ref",
                        "codex_home",
                        "thinking_effort", "strict_effort_enforcement",
                        "dedicated_config_dir", "isolated",
                        "isolation_mode", "container_image"):
                if key in kwargs:
                    updates[key] = kwargs[key]

            # Secret: empty/absent means "unchanged" so callers round-tripping
            # the redacted to_dict() (provider_key_set) can't wipe the key.
            # Wiping requires the explicit clear_provider_key flag.
            if kwargs.get("provider_key"):
                updates["provider_key"] = kwargs["provider_key"]
            elif kwargs.get("clear_provider_key"):
                updates["provider_key"] = ""

            for key in ("watchdog_config", "allowed_tools", "disallowed_tools", "groups"):
                if key in kwargs:
                    updates[key] = json.dumps(kwargs[key])

            for key in ("auto_restart", "enabled", "auto_start", "clock_aligned",
                        "plain_text_fallback", "dream_enabled", "dream_notify",
                        "librarian_enabled", "strict_effort_enforcement",
                        "dedicated_config_dir", "isolated"):
                if key in updates:
                    updates[key] = int(updates[key])
            if "voice_config" in updates and isinstance(updates["voice_config"], dict):
                updates["voice_config"] = json.dumps(updates["voice_config"])

            soul_changed = "soul" in updates and updates["soul"] != existing.soul
            if "soul" in updates and not soul_changed:
                updates.pop("soul")
            if soul_changed:
                self.guard_soul_mutation(
                    name,
                    existing.soul,
                    updates["soul"],
                    display_name=existing.display_name,
                    force_soul=force_soul,
                )

            if updates:
                updates["updated_at"] = time.time()
                set_clause = ", ".join(f"{key}=?" for key in updates)
                try:
                    if soul_changed:
                        self._insert_soul_version_uncommitted(
                            name,
                            existing.soul,
                            source=f"{soul_source}:before",
                        )
                    self._db.execute(
                        f"UPDATE agents SET {set_clause} WHERE name=?",
                        list(updates.values()) + [name],
                    )
                    if _commit:
                        self._db.commit()
                except Exception:
                    if _commit:
                        self._db.rollback()
                    raise

        updated = self.get(name)
        if not updated:  # Defensive against a concurrent delete.
            raise KeyError(f"Agent '{name}' not found")
        return updated

    def _insert_agent_row(
        self,
        sql: str,
        params: tuple,
        *,
        name: str,
        create_only: bool,
        work_dir: Path,
    ) -> None:
        """Atomically claim an owner root, insert, and initialize the winner."""
        with self._rmw_lock:
            insert_won = False
            try:
                # The advisory preflight in register() is intentionally repeated
                # under SQLite's cross-connection writer lock. Different names do
                # not contend on the agents.name PRIMARY KEY, so BEGIN IMMEDIATE is
                # the authority that serializes overlap-check + INSERT across
                # daemon processes. The second writer observes the first commit
                # before it can evaluate owner-root equality or nesting.
                self._db.execute("BEGIN IMMEDIATE")
                self._refuse_workspace_overlap(name, work_dir)
                self._db.execute(sql, params)
                insert_won = True
                # A losing create-only request must not reinitialize files in
                # either the winner's workspace or an attacker-chosen path.
                # Keep the insert transaction open so only the PRIMARY KEY
                # winner reaches filesystem setup; rollback the row if setup
                # itself fails.
                self._init_workspace(work_dir, agent_name=name)
                self._db.commit()
            except sqlite3.IntegrityError as exc:
                self._db.rollback()
                if create_only:
                    # The exact-root belt may be evaluated before the name
                    # PRIMARY KEY when a same-name loser also reuses the
                    # winner's root. Classify from committed DB state after
                    # rollback instead of depending on SQLite's index order.
                    if self._db.execute(
                        "SELECT 1 FROM agents WHERE name=?",
                        (name,),
                    ).fetchone():
                        raise AgentAlreadyExistsError(
                            f"Agent '{name}' already exists"
                        ) from exc
                    if "agents.working_dir" in str(exc):
                        raise AgentWorkspaceOverlapError(
                            "agent workspace overlaps another registered agent"
                        ) from exc
                raise
            except Exception:
                self._db.rollback()
                if create_only and insert_won:
                    raise AgentRegistrationIncompleteError(
                        name,
                        row_committed=False,
                    )
                raise

    def _update_existing_registration(
        self,
        name: str,
        *,
        working_dir: str | Path,
        update_kwargs: dict,
    ) -> Agent:
        """Serialize a legacy register-update workspace claim across connections."""
        root = self._resolve_workspace_root(name, working_dir)
        # Preserve the early, side-effect-free refusal while making the repeated
        # check inside BEGIN IMMEDIATE authoritative for concurrent writers.
        self._refuse_workspace_overlap(name, root)
        try:
            self._db.execute("BEGIN IMMEDIATE")
            existing = self.get(name)
            if not existing:
                raise KeyError(f"Agent '{name}' not found")
            self._refuse_workspace_overlap(name, root)
            if str(root) != existing.working_dir:
                self._init_workspace(root, agent_name=name)

            # update() owns soul-guard and snapshot semantics. Suppress its
            # commit so those field mutations and the owner-root claim land in
            # the same transaction, or all roll back on overlap/guard failure.
            self.update(name, _commit=False, **update_kwargs)
            self._db.execute(
                "UPDATE agents SET working_dir=?, updated_at=? WHERE name=?",
                (str(root), time.time(), name),
            )
            self._db.commit()
        except Exception:
            self._db.rollback()
            raise

        refreshed = self.get(name)
        if not refreshed:  # Defensive against a concurrent delete.
            raise KeyError(f"Agent '{name}' not found")
        return refreshed

    @staticmethod
    def _resolve_workspace_root(name: str, working_dir: str | Path) -> Path:
        """Return a stable absolute owner root suitable for persistence."""
        _validate_agent_name(name)
        try:
            root = Path(working_dir).resolve()
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            raise AgentWorkspacePathError("agent workspace path is invalid") from exc
        if not root.is_absolute() or (root.exists() and not root.is_dir()):
            raise AgentWorkspacePathError("agent workspace path is invalid")
        return root

    def _refuse_workspace_overlap(self, name: str, root: Path) -> None:
        """Fail closed when ``root`` intersects another agent's owner root."""
        _validate_agent_name(name)
        rows = self._db.execute(
            "SELECT name, working_dir FROM agents WHERE name<>?",
            (name,),
        ).fetchall()
        for other_name, other_working_dir in rows:
            try:
                other_root = self._resolve_workspace_root(
                    other_name,
                    other_working_dir,
                )
            except (AgentWorkspacePathError, ValueError) as exc:
                raise AgentWorkspacePathError(
                    "registered agent workspace path is invalid"
                ) from exc
            if (
                root == other_root
                or root.is_relative_to(other_root)
                or other_root.is_relative_to(root)
            ):
                raise AgentWorkspaceOverlapError(
                    "agent workspace overlaps another registered agent"
                )

    def resolve_registration_workspace(
        self,
        name: str,
        working_dir: str | Path,
    ) -> Path:
        """Resolve and preflight a proposed owner root without mutating state."""
        name = _validate_agent_name(name)
        with self._rmw_lock:
            root = self._resolve_workspace_root(name, working_dir)
            self._refuse_workspace_overlap(name, root)
            return root

    def register(self, name: str, *, create_only: bool = False, **kwargs) -> Agent:
        """Register atomically, including cross-agent workspace ownership."""
        name = _validate_agent_name(name)
        with self._rmw_lock:
            return self._register_locked(name, create_only=create_only, **kwargs)

    def _register_locked(
        self,
        name: str,
        *,
        create_only: bool = False,
        **kwargs,
    ) -> Agent:
        """Register while ``_rmw_lock`` protects root checks and mutation."""
        # Sanitize before any path is constructed downstream. Same regex as
        # the API model — duplicated here so in-process callers (tests,
        # scripts, future routes) can't bypass it. ``_validate_agent_name``
        # is the sanitizer CodeQL's taint analysis recognizes at the
        # source-of-path-construction.
        name = _validate_agent_name(name)
        now = time.time()
        # A create-only caller must reach INSERT OR ABORT without a read-side
        # availability decision. The agents.name PRIMARY KEY is authoritative,
        # including when multiple daemon processes race on the same DB.
        existing = None if create_only else self.get(name)

        if existing:
            # Keep the legacy workspace mutation on register(), whose admin
            # callers and path-handling contract predate the partial update
            # API. AgentRegistry.update() is intentionally path-free.
            update_kwargs = dict(kwargs)
            update_kwargs.pop("working_dir", None)
            if kwargs.get("working_dir"):
                updated = self._update_existing_registration(
                    name,
                    working_dir=kwargs["working_dir"],
                    update_kwargs=update_kwargs,
                )
            else:
                updated = self.update(name, **update_kwargs)
            # Preserve register()'s historical signing-key backfill contract
            # for legacy rows even though the field mutation is delegated.
            self.get_or_create_signing_key(name)
            self._seed_verified_contacts()
            return updated
        else:
            # Set up workspace — always store absolute path for portability.
            # Relative paths break when daemon CWD differs from install dir.
            raw_dir = kwargs.get("working_dir", "") or f"data/agents/{name}"
            work_dir_abs = self._resolve_workspace_root(name, raw_dir)
            self._refuse_workspace_overlap(name, work_dir_abs)
            agent = Agent(
                name=name,
                display_name=kwargs.get("display_name", ""),
                model=kwargs.get("model", "opus"),
                soul=kwargs.get("soul", ""),
                users=kwargs.get("users", ""),
                boundaries=kwargs.get("boundaries", ""),
                system_prompt=kwargs.get("system_prompt", ""),
                working_dir=str(work_dir_abs),
                permission_mode=kwargs.get("permission_mode", "auto"),
                allowed_tools=kwargs.get("allowed_tools", []),
                disallowed_tools=kwargs.get("disallowed_tools", []),
                max_turns=kwargs.get("max_turns", 0),
                timeout=kwargs.get("timeout", 300.0),
                restart_threshold_pct=kwargs.get("restart_threshold_pct", 80.0),
                context_nudge_threshold_pct=kwargs.get("context_nudge_threshold_pct", 0.0),
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
                isolated=kwargs.get("isolated", False),
                isolation_mode=kwargs.get("isolation_mode", "local"),
                container_image=kwargs.get("container_image", ""),
                dream_enabled=kwargs.get("dream_enabled", False),
                dream_schedule=kwargs.get("dream_schedule", "0 3 * * *"),
                dream_timezone=kwargs.get("dream_timezone", "America/Los_Angeles"),
                dream_model=kwargs.get("dream_model", ""),
                dream_notify=kwargs.get("dream_notify", True),
                librarian_enabled=kwargs.get("librarian_enabled", False),
                librarian_schedule=kwargs.get("librarian_schedule", "0 4 * * *"),
                runtime=kwargs.get("runtime", "claude_sdk"),
                transport=kwargs.get("transport", "sdk"),
                provider_url=kwargs.get("provider_url", ""),
                provider_key=kwargs.get("provider_key", ""),
                provider_model=kwargs.get("provider_model", ""),
                provider_ref=kwargs.get("provider_ref", ""),
                codex_home=kwargs.get("codex_home", ""),
                thinking_effort=kwargs.get("thinking_effort", "medium"),
                strict_effort_enforcement=kwargs.get("strict_effort_enforcement", False),
                dedicated_config_dir=kwargs.get("dedicated_config_dir", False),
                watchdog_config=kwargs.get("watchdog_config", {}),
                created_at=now,
                updated_at=now,
            )
            insert_complete = False
            try:
                self._insert_agent_row(
                    """INSERT OR ABORT INTO agents
                   (name, display_name, model, soul, users, boundaries,
                    system_prompt, working_dir,
                    permission_mode, allowed_tools, disallowed_tools, max_turns, timeout,
                    restart_threshold_pct, context_nudge_threshold_pct, auto_restart, parent, groups,
                    max_sessions, enabled, registration_finalized, auto_start,
                    heartbeat_interval, plain_text_fallback,
                    wake_interval, clock_aligned, auto_sleep_hours, voice_config, role, isolated,
                    isolation_mode, container_image,
                    dream_enabled, dream_schedule, dream_timezone, dream_model, dream_notify,
                    librarian_enabled, librarian_schedule,
                    runtime, transport, provider_url, provider_key, provider_model, provider_ref,
                    codex_home,
                    thinking_effort, strict_effort_enforcement, dedicated_config_dir,
                    watchdog_config,
                    created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (agent.name, agent.display_name, agent.model, agent.soul,
                 agent.users, agent.boundaries,
                 agent.system_prompt, agent.working_dir, agent.permission_mode,
                 json.dumps(agent.allowed_tools), json.dumps(agent.disallowed_tools),
                 agent.max_turns, agent.timeout,
                 agent.restart_threshold_pct, agent.context_nudge_threshold_pct,
                 int(agent.auto_restart),
                 agent.parent, json.dumps(agent.groups), agent.max_sessions,
                 int(agent.enabled), int(not create_only), int(agent.auto_start),
                 agent.heartbeat_interval, int(agent.plain_text_fallback),
                 agent.wake_interval, int(agent.clock_aligned), agent.auto_sleep_hours,
                 json.dumps(agent.voice_config), agent.role, int(agent.isolated),
                 agent.isolation_mode, agent.container_image,
                 int(agent.dream_enabled), agent.dream_schedule, agent.dream_timezone, agent.dream_model, int(agent.dream_notify),
                 int(agent.librarian_enabled), agent.librarian_schedule,
                 agent.runtime, agent.transport, agent.provider_url, agent.provider_key,
                 agent.provider_model, agent.provider_ref,
                 agent.codex_home,
                 agent.thinking_effort, int(agent.strict_effort_enforcement),
                 int(agent.dedicated_config_dir),
                 json.dumps(agent.watchdog_config),
                     agent.created_at, agent.updated_at),
                    name=name,
                    create_only=create_only,
                    work_dir=work_dir_abs,
                )
                insert_complete = True
                _log(f"agents: registered {name}")

                # First-run convenience: if no main agent is designated yet, adopt
                # this newly created agent. Without a main agent the daemon starts
                # no autonomy loop and the agent never auto-wakes — a silent
                # dead-end for fresh installs. Only fires on creation of an enabled
                # agent when main is unset, so it never overrides an existing choice.
                if agent.enabled and not self.get_setting("main_agent"):
                    self.set_setting("main_agent", name)
                    _log(f"agents: auto-assigned main_agent={name} (first agent)")

                # Ensure the agent has a per-agent signing key (#623). Idempotent —
                # returns the existing key on re-registration / update.
                self.get_or_create_signing_key(name)
                # The HTTP create-only path has fallible provisioning and MCP
                # publication stages after this registry commit. Defer its
                # verified-contact bootstrap to finalize_registration() so a
                # failed POST cannot leave the contact or migration marker.
                # Direct legacy registrations have no later external stages and
                # retain their historical bootstrap behavior.
                if not create_only:
                    self._seed_verified_contacts()
            except (AgentAlreadyExistsError, AgentRegistrationIncompleteError):
                raise
            except Exception as exc:
                if create_only and insert_complete:
                    raise AgentRegistrationIncompleteError(
                        name,
                        row_committed=True,
                    ) from exc
                raise

            return self.get(name)  # type: ignore

    _AGENT_COLUMNS = (
        "name, display_name, model, soul, system_prompt, working_dir, "
        "permission_mode, allowed_tools, max_turns, timeout, "
        "restart_threshold_pct, auto_restart, parent, groups, "
        "max_sessions, enabled, auto_start, heartbeat_interval, plain_text_fallback, role, "
        "created_at, updated_at, users, boundaries, status, retired_at, "
        "wake_interval, clock_aligned, auto_sleep_hours, voice_config, "
        "dream_enabled, dream_schedule, dream_timezone, dream_model, dream_notify, "
        "librarian_enabled, librarian_schedule, "
        "working_status, working_status_updated_at, "
        "runtime, transport, provider_url, provider_key, provider_model, provider_ref, "
        "disallowed_tools, thinking_effort, watchdog_config, last_seen_at, "
        "strict_effort_enforcement, context_nudge_threshold_pct, isolated, "
        "isolation_mode, container_image, dedicated_config_dir, codex_home"
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

    # ── Per-agent signing keys (#623) ──────────────────────────────────
    # Each agent gets its own 256-bit key for signing internal (MCP->daemon)
    # requests, replacing the shared global PINKY_SESSION_SECRET. This gives
    # each agent a NON-FORGEABLE identity — the prerequisite for the
    # containerized-Counterpart tenant boundary, where one team member's agent
    # must be unable to impersonate another. Stored in a dedicated table, never
    # in the agents row / to_dict(), so the secret is never serialized into API
    # responses. Migration is dual-accept (see auth.verify_internal_request):
    # the global secret stays valid until the cutover PR provisions per-agent
    # keys into agent env and drops global-secret acceptance.

    def get_or_create_signing_key(self, agent_name: str) -> str:
        """Return the agent's per-agent signing key, generating one on first use."""
        name = _validate_agent_name(agent_name)
        row = self._db.execute(
            "SELECT signing_key FROM agent_signing_keys WHERE agent_name=?", (name,)
        ).fetchone()
        if row and row[0]:
            return row[0]
        key = secrets.token_urlsafe(32)
        self._db.execute(
            "INSERT OR REPLACE INTO agent_signing_keys "
            "(agent_name, signing_key, created_at) VALUES (?, ?, ?)",
            (name, key, time.time()),
        )
        self._db.commit()
        return key

    def get_signing_key(self, agent_name: str) -> str | None:
        """Return the agent's signing key if one exists (no generation)."""
        row = self._db.execute(
            "SELECT signing_key FROM agent_signing_keys WHERE agent_name=?",
            (agent_name,),
        ).fetchone()
        return row[0] if row and row[0] else None

    def _backfill_signing_keys(self) -> None:
        """Generate a signing key for any existing agent that lacks one (#623)."""
        try:
            rows = self._db.execute("SELECT name FROM agents").fetchall()
        except Exception:
            return
        generated = 0
        for (name,) in rows:
            try:
                if not self.get_signing_key(name):
                    self.get_or_create_signing_key(name)
                    generated += 1
            except Exception as e:
                # A single non-conforming legacy name must not brick boot
                # (get_or_create -> _validate_agent_name raises). Log + skip.
                _log(f"agent_registry: skipped signing-key backfill for {name!r}: {e}")
        if generated:
            _log(f"agent_registry: backfilled signing keys for {generated} agent(s)")

    # ── Buzz identities (#541 inc1) ────────────────────────────────────

    @staticmethod
    def _buzz_identity_dict(row) -> dict:
        """Public identity DTO. Secret envelope and receipt stay daemon-only."""
        return {
            "agent": row[0],
            "pubkey": row[1],
            "wrap_version": row[2],
            "relay_url": row[3],
            "community_id": row[4],
            "relay_signing_pubkey": row[5],
            "enabled": bool(row[6]),
            "status": row[7],
            "last_error": row[8],
            "tos_approved": bool(row[9] and row[10] and row[11]),
            "tos_approved_by": row[9],
            "tos_approved_at": row[10],
            "tos_approval_ref": row[11],
            "created_at": row[12],
            "updated_at": row[13],
        }

    def get_buzz_identity(self, agent_name: str) -> dict | None:
        """Return public Buzz identity state without receipt or key envelope."""
        row = self._db.execute(
            "SELECT agent, pubkey, wrap_version, relay_url, community_id, "
            "relay_signing_pubkey, enabled, status, last_error, "
            "tos_approved_by, tos_approved_at, "
            "tos_approval_ref, created_at, updated_at "
            "FROM buzz_identities WHERE agent=?",
            (agent_name,),
        ).fetchone()
        return self._buzz_identity_dict(row) if row else None

    def list_buzz_identities(self, *, enabled_only: bool = False) -> list[dict]:
        """List public Buzz identity state, never encrypted or raw secret fields."""
        sql = (
            "SELECT agent, pubkey, wrap_version, relay_url, community_id, "
            "relay_signing_pubkey, enabled, status, last_error, "
            "tos_approved_by, tos_approved_at, "
            "tos_approval_ref, created_at, updated_at FROM buzz_identities"
        )
        if enabled_only:
            sql += " WHERE enabled=1"
        sql += " ORDER BY agent"
        return [self._buzz_identity_dict(row) for row in self._db.execute(sql).fetchall()]

    def bind_buzz_identity_owner_control(
        self,
        agent_name: str,
        *,
        private_key: str,
        relay_url: str,
        community_id: str,
        relay_signing_pubkey: str,
        enabled: bool,
        owner_actor: str,
        _commit: bool = True,
    ) -> dict:
        """Bind one identity from the authenticated owner-control route only.

        The HTTP composition root derives only the authenticated owner actor.
        Receipt, timestamp, and reference are generated here and bound to the
        exact identity/policy/scope; callers cannot supply authority bytes.
        """
        from urllib.parse import urlsplit

        from pinky_daemon.buzz_identity import (
            issue_buzz_owner_approval,
            validate_buzz_owner_actor,
            validate_buzz_owner_approval,
            wrap_buzz_private_key,
        )
        from pinky_identity.keystore import DeviceKey

        agent = _validate_agent_name(agent_name)
        if not self.get(agent):
            raise KeyError(f"Agent '{agent}' not found")
        relay = str(relay_url or "").strip()
        parsed = urlsplit(relay)
        if (
            parsed.scheme not in {"ws", "wss"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Buzz relay_url must be a plain ws:// or wss:// URL")
        community = str(community_id or "").strip().lower()
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,62}", community):
            raise ValueError("Buzz community_id is invalid")
        relay_signer = _validate_buzz_pubkey(
            relay_signing_pubkey,
            field_name="relay_signing_pubkey",
        )
        approver = validate_buzz_owner_actor(owner_actor)

        # Refuse accidental secret duplication into any durable text column.
        secret_text = str(private_key or "").strip().lower()
        if secret_text and any(
            secret_text in value.lower()
            for value in (relay, community, relay_signer, approver)
        ):
            raise ValueError("Buzz private key must not appear in identity metadata")

        device_key = DeviceKey.load_or_create(self._buzz_device_key_path)
        envelope = wrap_buzz_private_key(
            private_key,
            agent=agent,
            device_key=device_key,
        )
        now = time.time()
        status = "active" if enabled else "disabled"
        with self._rmw_lock:
            existing = self._db.execute(
                "SELECT pubkey, relay_url, community_id, relay_signing_pubkey, tos_receipt, "
                "tos_approved_by, tos_approved_at, tos_approval_ref "
                "FROM buzz_identities WHERE agent=?",
                (agent,),
            ).fetchone()
            if existing and (
                not secrets.compare_digest(existing[0], envelope.pubkey)
                or not secrets.compare_digest(existing[1], relay)
                or not secrets.compare_digest(existing[2], community)
            ):
                raise ValueError(
                    "Buzz identity or approval scope rotation requires an explicit rotation operation"
                )
            if existing and existing[3] and not secrets.compare_digest(
                existing[3], relay_signer
            ):
                raise ValueError(
                    "Buzz relay signing authority rotation requires an explicit rotation operation"
                )
            try:
                if existing:
                    validate_buzz_owner_approval(
                        agent=agent,
                        pubkey=existing[0],
                        relay_url=existing[1],
                        community_id=existing[2],
                        receipt=existing[4],
                        approved_by=existing[5],
                        approved_at=existing[6],
                        approval_ref=existing[7],
                    )
                    # Same identity + same immutable receipt: safe idempotent
                    # re-bind. Preserve the original authority actor/timestamp/ref.
                    self._db.execute(
                        """UPDATE buzz_identities
                           SET wrap_version=?, nonce=?, ciphertext=?, relay_url=?,
                               community_id=?, relay_signing_pubkey=?, enabled=?, status=?, last_error='',
                               updated_at=? WHERE agent=?""",
                        (
                            envelope.wrap_version,
                            envelope.nonce,
                            envelope.ciphertext,
                            relay,
                            community,
                            relay_signer,
                            int(enabled),
                            status,
                            now,
                            agent,
                        ),
                    )
                else:
                    approval = issue_buzz_owner_approval(
                        owner_actor=approver,
                        agent=agent,
                        pubkey=envelope.pubkey,
                        relay_url=relay,
                        community_id=community,
                    )
                    self._db.execute(
                        """INSERT INTO buzz_identities (
                               agent, pubkey, wrap_version, nonce, ciphertext,
                               relay_url, community_id, relay_signing_pubkey,
                               enabled, status, last_error,
                               tos_receipt, tos_approved_by, tos_approved_at,
                               tos_approval_ref, created_at, updated_at
                           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', ?, ?, ?, ?, ?, ?)""",
                        (
                            agent,
                            envelope.pubkey,
                            envelope.wrap_version,
                            envelope.nonce,
                            envelope.ciphertext,
                            relay,
                            community,
                            relay_signer,
                            int(enabled),
                            status,
                            approval.receipt,
                            approval.approved_by,
                            approval.approved_at,
                            approval.approval_ref,
                            now,
                            now,
                        ),
                    )
                if _commit:
                    self._db.commit()
            except sqlite3.IntegrityError as exc:
                self._db.rollback()
                raise ValueError("Buzz identity conflicts with an existing binding") from exc
        result = self.get_buzz_identity(agent)
        if result is None:  # pragma: no cover - defensive after successful write
            raise RuntimeError("Buzz identity write did not persist")
        return result

    def get_buzz_signing_material(self, agent_name: str):
        """Unwrap one active identity or disable it on any integrity failure."""
        from pinky_daemon.buzz_identity import (
            BuzzDependencyError,
            BuzzIdentityUnhealthyError,
            BuzzKeyEnvelope,
            BuzzSigningMaterial,
            unwrap_buzz_private_key,
            validate_buzz_owner_approval,
        )
        from pinky_identity.keystore import DeviceKey

        row = self._db.execute(
            """SELECT agent, pubkey, wrap_version, nonce, ciphertext, relay_url,
                      community_id, relay_signing_pubkey, enabled, status,
                      tos_receipt, tos_approved_by, tos_approved_at, tos_approval_ref
               FROM buzz_identities WHERE agent=?""",
            (agent_name,),
        ).fetchone()
        if not row or not row[8] or row[9] != "active":
            return None
        try:
            validate_buzz_owner_approval(
                agent=row[0],
                pubkey=row[1],
                relay_url=row[5],
                community_id=row[6],
                receipt=row[10],
                approved_by=row[11],
                approved_at=row[12],
                approval_ref=row[13],
            )
            envelope = BuzzKeyEnvelope(
                agent=row[0],
                pubkey=row[1],
                wrap_version=row[2],
                nonce=bytes(row[3]),
                ciphertext=bytes(row[4]),
            )
            device_key = DeviceKey.load_or_create(self._buzz_device_key_path)
            private_key = unwrap_buzz_private_key(envelope, device_key=device_key)
        except BuzzDependencyError:
            self.mark_buzz_dependency_refused(agent_name, "missing_runtime_dependency")
            raise
        except Exception as exc:
            self.mark_buzz_identity_unhealthy(agent_name, type(exc).__name__)
            raise BuzzIdentityUnhealthyError("Buzz identity is unhealthy and was disabled") from exc
        return BuzzSigningMaterial(
            agent=row[0],
            pubkey=row[1],
            private_key=private_key,
            relay_url=row[5],
            community_id=row[6],
            relay_signing_pubkey=row[7],
        )

    def mark_buzz_identity_unhealthy(self, agent_name: str, reason: str) -> None:
        """Disable an identity after AEAD/KEK/public-key integrity failure."""
        self._db.execute(
            "UPDATE buzz_identities SET enabled=0, status='unhealthy', "
            "last_error=?, updated_at=? WHERE agent=?",
            (str(reason or "integrity_failure")[:160], time.time(), agent_name),
        )
        self._db.commit()

    def mark_buzz_dependency_refused(self, agent_name: str, reason: str) -> None:
        """Refuse registration without destroying an otherwise valid identity."""
        self._db.execute(
            "UPDATE buzz_identities SET status='dependency_refused', "
            "last_error=?, updated_at=? WHERE agent=? AND enabled=1",
            (str(reason or "missing_runtime_dependency")[:160], time.time(), agent_name),
        )
        self._db.commit()

    def mark_buzz_dependency_ready(self, agent_name: str) -> None:
        """Restore an enabled dependency-refused identity after venv healing."""
        self._db.execute(
            "UPDATE buzz_identities SET status='active', last_error='', updated_at=? "
            "WHERE agent=? AND enabled=1 AND status='dependency_refused'",
            (time.time(), agent_name),
        )
        self._db.commit()

    def disable_buzz_identity(self, agent_name: str) -> bool:
        """Owner-control lifecycle operation; encrypted material remains recoverable."""
        cursor = self._db.execute(
            "UPDATE buzz_identities SET enabled=0, status='disabled', "
            "last_error='', updated_at=? WHERE agent=?",
            (time.time(), agent_name),
        )
        self._db.commit()
        return cursor.rowcount > 0

    # ── Buzz inbound authorization + durable delivery (#541 inc2) ─────

    def configure_buzz_inbound_owner_control(
        self,
        agent_name: str,
        *,
        owner_pubkey: str,
        channels: list[dict],
        approved_users: list[dict],
        owner_actor: str,
        _commit: bool = True,
    ) -> dict:
        """Atomically replace one identity-scoped, default-deny inbound policy."""
        from pinky_daemon.buzz_identity import validate_buzz_owner_actor

        actor = validate_buzz_owner_actor(owner_actor)
        owner = _validate_buzz_pubkey(owner_pubkey, field_name="owner_pubkey")
        identity = self._db.execute(
            "SELECT pubkey, relay_url, community_id FROM buzz_identities WHERE agent=?",
            (agent_name,),
        ).fetchone()
        if not identity:
            raise KeyError(f"Buzz identity not found for agent: {agent_name}")
        if owner == identity[0]:
            raise ValueError("Buzz owner_pubkey must not equal the agent identity pubkey")
        if not channels:
            raise ValueError("Buzz inbound channel allowlist must not be empty")
        if len(channels) > 128 or len(approved_users) > 256:
            raise ValueError("Buzz inbound policy exceeds configured bounds")

        clean_channels: list[tuple[str, str]] = []
        channel_ids: set[str] = set()
        for item in channels:
            channel = _validate_buzz_channel_id(item.get("channel_id", ""))
            if channel in channel_ids:
                raise ValueError("Buzz channel allowlist contains a duplicate channel_id")
            channel_ids.add(channel)
            label = _validate_buzz_annotation(
                item.get("label", ""), field_name="channel label", limit=80
            )
            clean_channels.append((channel, label))

        clean_users: list[tuple[str, str]] = []
        user_pubkeys: set[str] = {owner}
        for item in approved_users:
            pubkey = _validate_buzz_pubkey(item.get("pubkey", ""))
            if pubkey in user_pubkeys:
                raise ValueError("Buzz approved principals contain a duplicate pubkey")
            if pubkey == identity[0]:
                raise ValueError("Buzz approved principal must not equal the agent identity")
            user_pubkeys.add(pubkey)
            display = _validate_buzz_annotation(
                item.get("display_name", ""), field_name="display_name", limit=120
            )
            clean_users.append((pubkey, display))

        relay_url = identity[1]
        community_id = identity[2]
        now = time.time()
        with self._rmw_lock:
            prior_policy = self._db.execute(
                "SELECT owner_pubkey, owner_configured_at, owner_last_seen_at, "
                "owner_silence_notified_at FROM buzz_inbound_policies WHERE agent=?",
                (agent_name,),
            ).fetchone()
            prior_seen = {
                row[0]: float(row[1])
                for row in self._db.execute(
                    "SELECT pubkey, last_seen_at FROM buzz_inbound_principals WHERE agent=?",
                    (agent_name,),
                ).fetchall()
            }
            same_owner = bool(prior_policy and prior_policy[0] == owner)
            configured_at = float(prior_policy[1]) if same_owner else now
            owner_seen = float(prior_policy[2]) if same_owner else 0.0
            silence_notified = float(prior_policy[3]) if same_owner else 0.0
            try:
                self._db.execute(
                    """INSERT INTO buzz_inbound_policies
                       (agent, community_id, relay_url, owner_pubkey,
                        owner_configured_at, owner_last_seen_at,
                        owner_silence_notified_at, status, last_connect_at,
                        last_liveness_at, last_event_at, last_error,
                        updated_by, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, 'configured', 0, 0, 0, '', ?, ?)
                       ON CONFLICT(agent) DO UPDATE SET
                         community_id=excluded.community_id,
                         relay_url=excluded.relay_url,
                         owner_pubkey=excluded.owner_pubkey,
                         owner_configured_at=excluded.owner_configured_at,
                         owner_last_seen_at=excluded.owner_last_seen_at,
                         owner_silence_notified_at=excluded.owner_silence_notified_at,
                         status='configured', last_error='',
                         updated_by=excluded.updated_by, updated_at=excluded.updated_at""",
                    (
                        agent_name,
                        community_id,
                        relay_url,
                        owner,
                        configured_at,
                        owner_seen,
                        silence_notified,
                        actor,
                        now,
                    ),
                )
                self._db.execute("DELETE FROM buzz_inbound_channels WHERE agent=?", (agent_name,))
                self._db.execute("DELETE FROM buzz_inbound_principals WHERE agent=?", (agent_name,))
                self._db.executemany(
                    """INSERT INTO buzz_inbound_channels
                       (agent, community_id, relay_url, channel_id, label, created_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    [
                        (agent_name, community_id, relay_url, channel, label, now)
                        for channel, label in clean_channels
                    ],
                )
                principals = [(owner, "owner", "")] + [
                    (pubkey, "approved", display) for pubkey, display in clean_users
                ]
                self._db.executemany(
                    """INSERT INTO buzz_inbound_principals
                       (agent, community_id, pubkey, role, display_name,
                        approved_by, approved_at, last_seen_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    [
                        (
                            agent_name,
                            community_id,
                            pubkey,
                            role,
                            display,
                            actor,
                            now,
                            prior_seen.get(pubkey, 0.0),
                        )
                        for pubkey, role, display in principals
                    ],
                )
                # A policy replacement is an authorization-boundary change.
                # Pending events whose channel or author was revoked must not
                # become deliverable if that authority is added back later.
                revoked_pending = [
                    (agent_name, row[0])
                    for row in self._db.execute(
                        """SELECT event_id, channel_id, author_pubkey
                           FROM buzz_inbound_events
                           WHERE agent=? AND delivery_status='pending'""",
                        (agent_name,),
                    ).fetchall()
                    if row[1] not in channel_ids or row[2] not in user_pubkeys
                ]
                self._db.executemany(
                    "DELETE FROM buzz_inbound_events WHERE agent=? AND event_id=?",
                    revoked_pending,
                )
                if _commit:
                    self._db.commit()
            except Exception:
                self._db.rollback()
                raise
        policy = self.get_buzz_inbound_policy(agent_name)
        if policy is None:  # pragma: no cover - defensive after successful transaction
            raise RuntimeError("Buzz inbound policy write did not persist")
        return policy

    def bind_buzz_identity_with_inbound_owner_control(
        self,
        agent_name: str,
        *,
        private_key: str,
        relay_url: str,
        community_id: str,
        relay_signing_pubkey: str,
        enabled: bool,
        owner_pubkey: str,
        channels: list[dict],
        approved_users: list[dict],
        owner_actor: str,
    ) -> tuple[dict, dict]:
        """Atomically bind an identity and its complete inbound policy."""
        with self._rmw_lock:
            try:
                self._db.execute("BEGIN IMMEDIATE")
                identity = self.bind_buzz_identity_owner_control(
                    agent_name,
                    private_key=private_key,
                    relay_url=relay_url,
                    community_id=community_id,
                    relay_signing_pubkey=relay_signing_pubkey,
                    enabled=enabled,
                    owner_actor=owner_actor,
                    _commit=False,
                )
                policy = self.configure_buzz_inbound_owner_control(
                    agent_name,
                    owner_pubkey=owner_pubkey,
                    channels=channels,
                    approved_users=approved_users,
                    owner_actor=owner_actor,
                    _commit=False,
                )
                self._db.commit()
            except Exception:
                self._db.rollback()
                raise
        return identity, policy

    def get_buzz_inbound_policy(self, agent_name: str) -> dict | None:
        row = self._db.execute(
            """SELECT agent, community_id, relay_url, owner_pubkey,
                      owner_configured_at, owner_last_seen_at,
                      owner_silence_notified_at, status, last_connect_at,
                      last_liveness_at, last_event_at, last_error,
                      updated_by, updated_at
               FROM buzz_inbound_policies WHERE agent=?""",
            (agent_name,),
        ).fetchone()
        if not row:
            return None
        channels = [
            {"channel_id": item[0], "label": item[1]}
            for item in self._db.execute(
                "SELECT channel_id, label FROM buzz_inbound_channels "
                "WHERE agent=? AND community_id=? AND relay_url=? ORDER BY channel_id",
                (agent_name, row[1], row[2]),
            ).fetchall()
        ]
        principal_rows = self._db.execute(
            """SELECT pubkey, role, display_name, approved_by, approved_at, last_seen_at
               FROM buzz_inbound_principals
               WHERE agent=? AND community_id=? ORDER BY role DESC, pubkey""",
            (agent_name, row[1]),
        ).fetchall()
        principals = [
            {
                "principal": f"buzz:{row[1]}:{item[0]}",
                "pubkey": item[0],
                "role": item[1],
                "display_name": item[2],
                "approved_by": item[3],
                "approved_at": item[4],
                "last_seen_at": item[5],
            }
            for item in principal_rows
        ]
        return {
            "agent": row[0],
            "community_id": row[1],
            "relay_url": row[2],
            "owner_pubkey": row[3],
            "owner_principal": f"buzz:{row[1]}:{row[3]}",
            "owner_configured_at": row[4],
            "owner_last_seen_at": row[5],
            "owner_silence_notified_at": row[6],
            "owner_silence_days": BUZZ_OWNER_SILENCE_DAYS,
            "channels": channels,
            "approved_users": [item for item in principals if item["role"] == "approved"],
            "principals": principals,
            "status": row[7],
            "last_connect_at": row[8],
            "last_liveness_at": row[9],
            "last_event_at": row[10],
            "last_error": row[11],
            "updated_by": row[12],
            "updated_at": row[13],
        }

    def get_buzz_inbound_channel(
        self, agent_name: str, community_id: str, relay_url: str, channel_id: str
    ) -> dict | None:
        row = self._db.execute(
            """SELECT channel_id, label FROM buzz_inbound_channels
               WHERE agent=? AND community_id=? AND relay_url=? AND channel_id=?""",
            (agent_name, community_id, relay_url, channel_id),
        ).fetchone()
        return {"channel_id": row[0], "label": row[1]} if row else None

    def upsert_buzz_inbound_channel_from_membership(
        self,
        agent_name: str,
        community_id: str,
        relay_url: str,
        channel_id: str,
        *,
        label: str = "",
    ) -> dict:
        """Admit one relay-notified membership into the scoped inbound gate."""
        channel = _validate_buzz_channel_id(channel_id)
        clean_label = _validate_buzz_annotation(label, field_name="channel label", limit=80)
        with self._rmw_lock:
            policy = self._db.execute(
                """SELECT 1 FROM buzz_inbound_policies
                   WHERE agent=? AND community_id=? AND relay_url=?""",
                (agent_name, community_id, relay_url),
            ).fetchone()
            if policy is None:
                raise ValueError("Buzz membership notification is outside the inbound policy scope")
            self._db.execute(
                """INSERT INTO buzz_inbound_channels
                   (agent, community_id, relay_url, channel_id, label, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(agent, community_id, channel_id) DO UPDATE SET
                     relay_url=excluded.relay_url,
                     label=CASE WHEN excluded.label != '' THEN excluded.label
                                ELSE buzz_inbound_channels.label END""",
                (agent_name, community_id, relay_url, channel, clean_label, time.time()),
            )
            self._db.commit()
        result = self.get_buzz_inbound_channel(
            agent_name, community_id, relay_url, channel
        )
        if result is None:  # pragma: no cover - defensive after successful write
            raise RuntimeError("Buzz membership channel write did not persist")
        return result

    def remove_buzz_inbound_channel_from_membership(
        self,
        agent_name: str,
        community_id: str,
        relay_url: str,
        channel_id: str,
    ) -> bool:
        """Revoke a relay-notified membership and any undelivered channel rows."""
        channel = _validate_buzz_channel_id(channel_id)
        with self._rmw_lock:
            cursor = self._db.execute(
                """DELETE FROM buzz_inbound_channels
                   WHERE agent=? AND community_id=? AND relay_url=? AND channel_id=?""",
                (agent_name, community_id, relay_url, channel),
            )
            self._db.execute(
                """DELETE FROM buzz_inbound_events
                   WHERE agent=? AND community_id=? AND channel_id=?
                     AND delivery_status='pending'""",
                (agent_name, community_id, channel),
            )
            self._db.commit()
        return cursor.rowcount > 0

    def get_buzz_inbound_principal(
        self, agent_name: str, community_id: str, pubkey: str
    ) -> dict | None:
        row = self._db.execute(
            """SELECT pubkey, role, display_name, approved_by, approved_at, last_seen_at
               FROM buzz_inbound_principals
               WHERE agent=? AND community_id=? AND pubkey=?""",
            (agent_name, community_id, pubkey),
        ).fetchone()
        if not row:
            return None
        return {
            "principal": f"buzz:{community_id}:{row[0]}",
            "pubkey": row[0],
            "role": row[1],
            "display_name": row[2],
            "approved_by": row[3],
            "approved_at": row[4],
            "last_seen_at": row[5],
        }

    def note_buzz_inbound_principal_seen(
        self, agent_name: str, community_id: str, pubkey: str, seen_at: float
    ) -> None:
        with self._rmw_lock:
            self._db.execute(
                """UPDATE buzz_inbound_principals
                   SET last_seen_at=MAX(last_seen_at, ?)
                   WHERE agent=? AND community_id=? AND pubkey=?""",
                (seen_at, agent_name, community_id, pubkey),
            )
            self._db.execute(
                """UPDATE buzz_inbound_policies
                   SET owner_last_seen_at=MAX(owner_last_seen_at, ?),
                       owner_silence_notified_at=0
                   WHERE agent=? AND community_id=? AND owner_pubkey=?""",
                (seen_at, agent_name, community_id, pubkey),
            )
            self._db.commit()

    def buzz_owner_silence_alert_due(
        self, agent_name: str, *, now: float | None = None
    ) -> dict | None:
        row = self._db.execute(
            """SELECT community_id, owner_pubkey, owner_configured_at,
                      owner_last_seen_at, owner_silence_notified_at
               FROM buzz_inbound_policies WHERE agent=?""",
            (agent_name,),
        ).fetchone()
        if not row:
            return None
        current = time.time() if now is None else float(now)
        basis = float(row[3] or row[2])
        if basis <= 0 or current - basis < BUZZ_OWNER_SILENCE_DAYS * 86400:
            return None
        if float(row[4]) >= basis:
            return None
        return {
            "agent": agent_name,
            "community_id": row[0],
            "owner_principal": f"buzz:{row[0]}:{row[1]}",
            "last_seen_at": float(row[3]),
            "configured_at": float(row[2]),
            "days": BUZZ_OWNER_SILENCE_DAYS,
        }

    def mark_buzz_owner_silence_notified(
        self, agent_name: str, *, notified_at: float | None = None
    ) -> None:
        self._db.execute(
            "UPDATE buzz_inbound_policies SET owner_silence_notified_at=? WHERE agent=?",
            (time.time() if notified_at is None else float(notified_at), agent_name),
        )
        self._db.commit()

    def begin_buzz_inbound_event_delivery(
        self,
        agent_name: str,
        event: dict,
        *,
        community_id: str,
        channel_id: str,
        replay: bool = False,
    ) -> bool:
        """Claim one verified durable kind-9; ephemeral events never enter this table."""
        if event.get("kind") != 9:
            raise ValueError("only durable Buzz kind-9 events may be claimed")
        event_id = _validate_buzz_pubkey(event.get("id", ""), field_name="event id")
        author = _validate_buzz_pubkey(event.get("pubkey", ""))
        payload = json.dumps(event, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        now = time.time()
        with self._rmw_lock:
            row = self._db.execute(
                "SELECT delivery_status, claimed_at FROM buzz_inbound_events "
                "WHERE agent=? AND event_id=?",
                (agent_name, event_id),
            ).fetchone()
            if row:
                if row[0] == "delivered" or not replay:
                    return False
                cursor = self._db.execute(
                    """UPDATE buzz_inbound_events
                       SET claimed_at=?, attempts=attempts+1, last_error=''
                       WHERE agent=? AND event_id=? AND delivery_status='pending'
                         AND (claimed_at=0 OR claimed_at<=?)""",
                    (
                        now,
                        agent_name,
                        event_id,
                        now - BUZZ_INBOUND_CLAIM_LEASE_SECONDS,
                    ),
                )
                self._db.commit()
                return cursor.rowcount > 0
            else:
                self._db.execute(
                    """INSERT INTO buzz_inbound_events
                       (agent, event_id, community_id, channel_id, author_pubkey,
                        kind, event_created_at, event_json, delivery_status,
                        claimed_at, delivered_at, attempts, last_error)
                       VALUES (?, ?, ?, ?, ?, 9, ?, ?, 'pending', ?, 0, 1, '')""",
                    (
                        agent_name,
                        event_id,
                        community_id,
                        channel_id,
                        author,
                        float(event["created_at"]),
                        payload,
                        now,
                    ),
                )
            self._db.commit()
        return True

    def list_pending_buzz_inbound_events(self, agent_name: str) -> list[dict]:
        stale_before = time.time() - BUZZ_INBOUND_CLAIM_LEASE_SECONDS
        rows = self._db.execute(
            """SELECT event_json FROM buzz_inbound_events
               WHERE agent=? AND delivery_status='pending'
                 AND (claimed_at=0 OR claimed_at<=?)
               ORDER BY event_created_at, event_id""",
            (agent_name, stale_before),
        ).fetchall()
        result: list[dict] = []
        for row in rows:
            try:
                event = json.loads(row[0])
            except (TypeError, ValueError):
                continue
            if isinstance(event, dict):
                result.append(event)
        return result

    def reset_buzz_inbound_event_claims_after_restart(self) -> int:
        """Release claims whose owning daemon process can no longer exist."""
        cursor = self._db.execute(
            """UPDATE buzz_inbound_events SET claimed_at=0
               WHERE delivery_status='pending' AND claimed_at!=0"""
        )
        self._db.commit()
        return cursor.rowcount

    def mark_buzz_inbound_event_delivered(self, agent_name: str, event_id: str) -> None:
        now = time.time()
        self._db.execute(
            """UPDATE buzz_inbound_events
               SET delivery_status='delivered', delivered_at=?, last_error='', event_json=''
               WHERE agent=? AND event_id=? AND delivery_status='pending'""",
            (now, agent_name, event_id),
        )
        self._db.commit()

    def mark_buzz_inbound_event_retry(self, agent_name: str, event_id: str, reason: str) -> None:
        self._db.execute(
            """UPDATE buzz_inbound_events SET claimed_at=0, last_error=?
               WHERE agent=? AND event_id=? AND delivery_status='pending'""",
            (str(reason or "delivery_failed")[:160], agent_name, event_id),
        )
        self._db.commit()

    def get_buzz_subscription_since(self, agent_name: str, *, now: float | None = None) -> int:
        row = self._db.execute(
            "SELECT MAX(event_created_at) FROM buzz_inbound_events WHERE agent=?",
            (agent_name,),
        ).fetchone()
        if row and row[0]:
            return max(0, int(float(row[0])) - 2)
        current = time.time() if now is None else float(now)
        return max(0, int(current) - 60)

    def update_buzz_inbound_health(
        self,
        agent_name: str,
        *,
        status: str | None = None,
        last_error: str = "",
        connected_at: float | None = None,
        liveness_at: float | None = None,
        event_at: float | None = None,
    ) -> None:
        connected = float(connected_at) if connected_at is not None else None
        liveness = float(liveness_at) if liveness_at is not None else None
        event = float(event_at) if event_at is not None else None
        status_value = str(status or "unknown")[:40] if status is not None else None
        self._db.execute(
            """UPDATE buzz_inbound_policies
               SET status=COALESCE(?, status), last_error=?,
                   last_connect_at=COALESCE(MAX(last_connect_at, ?), last_connect_at),
                   last_liveness_at=COALESCE(MAX(last_liveness_at, ?), last_liveness_at),
                   last_event_at=COALESCE(MAX(last_event_at, ?), last_event_at)
               WHERE agent=?""",
            (
                status_value,
                str(last_error or "")[:160],
                connected,
                liveness,
                event,
                agent_name,
            ),
        )
        self._db.commit()

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

    def stamp_last_seen(self, name: str, ts: float | None = None) -> None:
        """Server-side presence: stamp agents.last_seen_at. Agent-agnostic."""
        ts = ts if ts is not None else time.time()
        self._db.execute("UPDATE agents SET last_seen_at = ? WHERE name = ?", (ts, name))
        self._db.commit()

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
        # #623: purge the per-agent signing key too. Without this, a hard-
        # deleted name keeps its key in agent_signing_keys, so re-registering
        # that name inherits a STALE signing credential. Harmless under dual-
        # accept, but once the per-agent key is the sole credential (increment
        # 4) a recreated agent must mint a fresh identity, not resurrect the
        # old one. No FK/cascade on the table, so delete explicitly.
        self._db.execute("DELETE FROM agent_signing_keys WHERE agent_name=?", (name,))
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

    def build_system_prompt(
        self, agent_name: str, skill_store=None, effort: str | None = None
    ) -> str:
        """Build a complete system prompt from agent config + directives + skill directives.

        Combines the agent's base system_prompt with all active directives,
        ordered by priority, plus any directives from assigned skills.
        This is what gets passed to Claude Code.

        ``effort`` is the effective thinking-effort level for the session
        being built (session override if any, else the agent default). When
        it is the ``ultracode`` tier (#151), the ULTRACODE_DIRECTIVE section
        is injected so workflow-by-default orchestration holds regardless of
        CLI version. Defaults to the agent's persistent ``thinking_effort``.

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

        # Ultracode operating mode (#151). Injected high (right after identity)
        # so it reads as a standing directive. Keyed off the effective effort:
        # the session override if one was passed, else the agent default.
        effective_effort = effort if effort is not None else agent.thinking_effort
        if is_ultracode(effective_effort):
            parts.append(ULTRACODE_DIRECTIVE)

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

        # Skill hint — minimal pointer; full catalog available on demand
        if skill_store:
            try:
                materialized = skill_store.materialize_for_agent(agent_name)
                catalog = materialized.get("catalog", [])
                if catalog:
                    names = [e["name"] for e in catalog]
                    parts.append(
                        f"## Skills\n"
                        f"You have {len(catalog)} skills equipped. "
                        f"Call `list_my_skills()` for descriptions, "
                        f"`load_skill(\"name\")` for full instructions.\n\n"
                        f"Equipped: {', '.join(names)}"
                    )
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

        # Inject learned user profiles (from dream consolidation)
        try:
            from pinky_daemon.user_profile_store import UserProfileStore
            profile_store = UserProfileStore()
            known_users = profile_store.get_all_users()
            profile_sections = []
            for uid in known_users:
                # Look up display name from approved_users
                display = ""
                for au in self.list_approved_users(agent_name):
                    if au.chat_id == uid:
                        display = au.display_name
                        break
                section = profile_store.format_profile_for_prompt(
                    agent_name, uid, display_name=display,
                )
                if section:
                    profile_sections.append(section)
            if profile_sections:
                # Learned profiles are externally influenced (dream consolidation
                # over third-party interactions) — run them through the same
                # content scanner + fencing as the owner profile, not raw.
                safe_profiles = _safe(
                    "\n\n".join(profile_sections), f"learned_profiles:{agent_name}"
                )
                if safe_profiles:
                    parts.append(safe_profiles)
        except Exception:
            pass  # Don't break prompt build if profile store unavailable

        # Append memory guidance for all agents
        parts.append(
            "## Memory\n"
            "- All persistent memory goes through pinky-memory MCP tools\n"
            "- Use reflect() to store cross-session learnings, preferences, and task state\n"
            "- Use recall(\"query\") to search memory when context is missing\n"
            "- Use introspect() to review your stored memories\n"
            "- On context restart or session wake, recall() your recent state to restore continuity"
        )

        # Auto-skill learning hint for all agents
        parts.append(
            "## Auto-skill Learning\n"
            "After completing a complex multi-step task, call `propose_skill()` to capture the approach "
            "as a reusable skill. This makes you progressively smarter at recurring task types — "
            "each successful workflow becomes a repeatable template.\n"
            "Use `propose_skill(task_description=..., steps_taken=..., outcome=..., skill_name=...)` "
            "with `auto_install=False` (default) to draft for review, or `auto_install=True` to register immediately."
        )

        # GitHub attribution instruction for all agents
        parts.append(
            "## GitHub Attribution\n"
            "When creating GitHub issues or PRs, always end the body with the result of `get_attribution()`.\n"
            "Example footer: `🤖 Opened by Barsik`"
        )

        return "\n\n".join(parts)

    # ── Soul Versioning ─────────────────────────────────────

    def _insert_soul_version_uncommitted(
        self,
        agent_name: str,
        content: str,
        *,
        source: str,
    ) -> int:
        """Insert one version row; the caller owns commit/rollback."""
        agent_name = _validate_agent_name(agent_name)
        cursor = self._db.execute(
            "INSERT INTO soul_versions (agent_name, content, source, created_at) "
            "VALUES (?, ?, ?, ?)",
            (agent_name, content, source, time.time()),
        )
        return int(cursor.lastrowid)

    def snapshot_soul_before_mutation(
        self,
        agent_name: str,
        content: str,
        *,
        source: str,
    ) -> int:
        """Durably snapshot replaced content before a non-DB mutation."""
        agent_name = _validate_agent_name(agent_name)
        with self._rmw_lock:
            try:
                version_id = self._insert_soul_version_uncommitted(
                    agent_name,
                    content,
                    source=source,
                )
                self._db.commit()
            except Exception:
                self._db.rollback()
                raise
        return version_id

    def save_soul_version(self, agent_name: str, content: str, source: str = "unknown") -> int:
        """Archive a soul version. Returns the version ID.

        Sources: 'ui', 'agent', 'spawn', 'refresh', 'api'
        """
        agent_name = _validate_agent_name(agent_name)
        with self._rmw_lock:
            try:
                # Spawn publication records the installed content after writing.
                # Keep that historical deduplication contract; mutation snapshots
                # use _insert_soul_version_uncommitted so every actual replacement
                # gets an audit row even when the same content appeared previously.
                latest = self.get_soul_versions(agent_name, limit=1)
                if latest:
                    full = self.get_soul_version(agent_name, latest[0]["id"])
                    if full and full["content"] == content:
                        return latest[0]["id"]

                version_id = self._insert_soul_version_uncommitted(
                    agent_name,
                    content,
                    source=source,
                )
                self._db.commit()
                return version_id
            except Exception:
                self._db.rollback()
                raise

    def get_soul_versions(self, agent_name: str, limit: int = 20) -> list[dict]:
        """List soul versions for an agent, newest first."""
        agent_name = _validate_agent_name(agent_name)
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
        agent_name = _validate_agent_name(agent_name)
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
        token_ref = kwargs.get("token_ref", "")

        self._db.execute(
            """INSERT INTO agent_tokens (agent_name, platform, token, enabled, settings, token_ref, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT (agent_name, platform)
               DO UPDATE SET token=excluded.token, enabled=excluded.enabled,
                            settings=excluded.settings, token_ref=excluded.token_ref,
                            updated_at=excluded.updated_at""",
            (agent_name, platform, token, int(enabled), json.dumps(settings), token_ref, now),
        )
        self._db.commit()
        return self.get_token(agent_name, platform)  # type: ignore

    def get_token(self, agent_name: str, platform: str) -> AgentToken | None:
        """Get token config for an agent+platform (token value never exposed)."""
        row = self._db.execute(
            "SELECT agent_name, platform, token, enabled, settings, updated_at, token_ref"
            " FROM agent_tokens WHERE agent_name=? AND platform=?",
            (agent_name, platform),
        ).fetchone()
        if not row:
            return None
        token_ref = row[6] if len(row) > 6 else ""
        return AgentToken(
            agent_name=row[0], platform=row[1], token_set=bool(row[2]) or bool(token_ref),
            enabled=bool(row[3]), settings=json.loads(row[4]), updated_at=row[5],
            token_ref=token_ref,
        )

    def get_raw_token(self, agent_name: str, platform: str) -> str:
        """Get the actual token value (internal use only).

        Resolution order: inline token → token_ref → empty string.
        """
        row = self._db.execute(
            "SELECT token, token_ref FROM agent_tokens WHERE agent_name=? AND platform=?",
            (agent_name, platform),
        ).fetchone()
        if not row:
            return ""
        inline_token = row[0] or ""
        token_ref = row[1] if len(row) > 1 else ""
        if inline_token:
            return inline_token
        if token_ref:
            ref_row = self._db.execute(
                "SELECT token FROM bot_tokens WHERE id=?", (token_ref,)
            ).fetchone()
            return ref_row[0] if ref_row else ""
        return ""

    def get_token_account_id(self, agent_name: str, platform: str) -> str:
        """Return the explicit provider account/team/workspace binding."""
        token = self.get_token(agent_name, platform)
        if not token or not token.enabled or not token.token_set:
            return ""
        return str(
            token.settings.get("account_id")
            or token.settings.get("team_id")
            or token.settings.get("workspace_id")
            or ""
        ).strip()

    def get_raw_token_for_account(
        self, agent_name: str, platform: str, account_id: str,
    ) -> str:
        """Resolve a token only when its explicit account binding is exact."""
        requested = str(account_id or "").strip()
        if not requested or self.get_token_account_id(agent_name, platform) != requested:
            return ""
        return self.get_raw_token(agent_name, platform)

    def list_tokens(self, agent_name: str) -> list[AgentToken]:
        """List all tokens for an agent."""
        rows = self._db.execute(
            "SELECT agent_name, platform, token, enabled, settings, updated_at, token_ref"
            " FROM agent_tokens WHERE agent_name=? ORDER BY platform",
            (agent_name,),
        ).fetchall()
        return [
            AgentToken(agent_name=r[0], platform=r[1], token_set=bool(r[2]) or bool(r[6] if len(r) > 6 else ""),
                       enabled=bool(r[3]), settings=json.loads(r[4]), updated_at=r[5],
                       token_ref=r[6] if len(r) > 6 else "")
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

    def _ensure_schedule_name_available(
        self,
        agent_name: str,
        name: str,
        *,
        exclude_schedule_id: int | None = None,
    ) -> None:
        """Enforce enabled agent/name uniqueness within this writer process.

        Callers hold ``_rmw_lock`` across this guard and the mutation, which
        serializes threads sharing this connection. This does not coordinate
        with a second writer process; deployment requires one writer process
        per agents DB file.
        """
        sql = """SELECT id FROM agent_schedules
                 WHERE agent_name=? AND name=? AND enabled=1"""
        params: list = [agent_name, name]
        if exclude_schedule_id is not None:
            sql += " AND id<>?"
            params.append(exclude_schedule_id)
        sql += " ORDER BY id ASC LIMIT 1"
        existing = self._db.execute(sql, params).fetchone()
        if existing:
            raise ScheduleNameConflictError(
                f"Enabled schedule {name!r} already exists for agent {agent_name!r} "
                f"as ID {existing[0]}; choose a distinct name to create another "
                f"schedule, or use update_wake_schedule with ID {existing[0]} "
                "to edit the existing one"
            )

    def add_schedule(
        self, agent_name: str, cron: str, *,
        name: str = "", prompt: str = "", timezone: str = "America/Los_Angeles",
        direct_send: bool = False, target_channel: str = "",
        one_shot: bool = False,
    ) -> AgentSchedule:
        """Add a cron-based wake schedule for an agent."""
        _validate_schedule_cron(cron)
        now = time.time()
        with self._rmw_lock:
            self._ensure_schedule_name_available(agent_name, name)
            cursor = self._db.execute(
                """INSERT INTO agent_schedules (
                       agent_name, name, cron, prompt, timezone, enabled, last_run,
                       created_at, direct_send, target_channel, one_shot
                   ) VALUES (?, ?, ?, ?, ?, 1, 0, ?, ?, ?, ?)""",
                (
                    agent_name, name, cron, prompt, timezone, now,
                    int(direct_send), target_channel, int(one_shot),
                ),
            )
            self._db.commit()
            return AgentSchedule(
                id=cursor.lastrowid, agent_name=agent_name, name=name,
                cron=cron, prompt=prompt, timezone=timezone,
                enabled=True, last_run=0.0, last_delivered=0.0, created_at=now,
                direct_send=direct_send, target_channel=target_channel,
                one_shot=one_shot,
            )

    def _row_to_schedule(self, r) -> AgentSchedule:
        """Convert a DB row to AgentSchedule, handling optional columns."""
        return AgentSchedule(
            id=r[0], agent_name=r[1], name=r[2], cron=r[3],
            prompt=r[4], timezone=r[5], enabled=bool(r[6]),
            last_run=r[7], last_delivered=r[8], created_at=r[9],
            direct_send=bool(r[10]) if len(r) > 10 else False,
            target_channel=r[11] if len(r) > 11 else "",
            one_shot=bool(r[12]) if len(r) > 12 else False,
        )

    def get_schedules(self, agent_name: str, *, enabled_only: bool = True) -> list[AgentSchedule]:
        """Get all schedules for an agent."""
        sql = "SELECT id, agent_name, name, cron, prompt, timezone, enabled, last_run, last_delivered, created_at, direct_send, target_channel, one_shot FROM agent_schedules WHERE agent_name=?"
        params: list = [agent_name]
        if enabled_only:
            sql += " AND enabled=1"
        sql += " ORDER BY created_at ASC"
        rows = self._db.execute(sql, params).fetchall()
        return [self._row_to_schedule(r) for r in rows]

    def get_all_schedules(self, *, enabled_only: bool = True) -> list[AgentSchedule]:
        """Get all schedules across all agents."""
        sql = "SELECT id, agent_name, name, cron, prompt, timezone, enabled, last_run, last_delivered, created_at, direct_send, target_channel, one_shot FROM agent_schedules"
        if enabled_only:
            sql += " WHERE enabled=1"
        sql += " ORDER BY agent_name, created_at ASC"
        rows = self._db.execute(sql).fetchall()
        return [self._row_to_schedule(r) for r in rows
        ]

    def get_oversized_enabled_schedule_prompts(
        self,
        min_length: int,
    ) -> list[tuple[int, str, str, int]]:
        """Return enabled schedules whose prompts exceed ``min_length``.

        Only prompt metadata is selected; the prompt content itself never
        leaves SQLite or reaches the warning log.
        """
        if min_length < 0:
            raise ValueError("min_length must be non-negative")
        rows = self._db.execute(
            """SELECT id, agent_name, name, LENGTH(prompt)
               FROM agent_schedules
               WHERE enabled=1 AND LENGTH(prompt) > ?
               ORDER BY LENGTH(prompt) DESC""",
            (min_length,),
        ).fetchall()
        return [
            (int(row[0]), str(row[1]), str(row[2]), int(row[3]))
            for row in rows
        ]

    def get_schedule(self, schedule_id: int) -> AgentSchedule | None:
        """Return one schedule regardless of enabled state."""
        row = self._db.execute(
            """SELECT id, agent_name, name, cron, prompt, timezone, enabled,
                      last_run, last_delivered, created_at, direct_send,
                      target_channel, one_shot
               FROM agent_schedules WHERE id=?""",
            (schedule_id,),
        ).fetchone()
        return self._row_to_schedule(row) if row else None

    def update_schedule(
        self,
        schedule_id: int,
        *,
        cron: str | None = None,
        prompt: str | None = None,
        timezone: str | None = None,
        name: str | None = None,
        direct_send: bool | None = None,
        target_channel: str | None = None,
        one_shot: bool | None = None,
    ) -> AgentSchedule | None:
        """Partially update a schedule without changing its ID."""
        values = {
            "name": name,
            "cron": cron,
            "prompt": prompt,
            "timezone": timezone,
            "direct_send": direct_send,
            "target_channel": target_channel,
            "one_shot": one_shot,
        }
        updates = {field: value for field, value in values.items() if value is not None}
        if not updates:
            raise ValueError("update_schedule requires at least one field")
        if cron is not None:
            _validate_schedule_cron(cron)
        for column in ("direct_send", "one_shot"):
            if column in updates:
                updates[column] = int(updates[column])

        with self._rmw_lock:
            current = self._db.execute(
                "SELECT agent_name, enabled FROM agent_schedules WHERE id=?",
                (schedule_id,),
            ).fetchone()
            if current is None:
                return None
            if name is not None and bool(current[1]):
                self._ensure_schedule_name_available(
                    current[0],
                    name,
                    exclude_schedule_id=schedule_id,
                )

            set_clause = ", ".join(f"{field}=?" for field in updates)
            cursor = self._db.execute(
                f"UPDATE agent_schedules SET {set_clause} WHERE id=?",
                list(updates.values()) + [schedule_id],
            )
            self._db.commit()
            if cursor.rowcount == 0:
                return None
            row = self._db.execute(
                """SELECT id, agent_name, name, cron, prompt, timezone, enabled, last_run,
                          last_delivered, created_at, direct_send, target_channel, one_shot
                   FROM agent_schedules WHERE id=?""",
                (schedule_id,),
            ).fetchone()
            return self._row_to_schedule(row) if row else None

    def remove_schedule(self, schedule_id: int) -> bool:
        """Remove a schedule."""
        cursor = self._db.execute("DELETE FROM agent_schedules WHERE id=?", (schedule_id,))
        self._db.commit()
        return cursor.rowcount > 0

    def toggle_schedule(self, schedule_id: int, enabled: bool) -> bool:
        """Enable/disable a schedule."""
        if not enabled:
            cursor = self._db.execute(
                "UPDATE agent_schedules SET enabled=0 WHERE id=?",
                (schedule_id,),
            )
            self._db.commit()
            return cursor.rowcount > 0
        with self._rmw_lock:
            current = self._db.execute(
                "SELECT agent_name, name FROM agent_schedules WHERE id=?",
                (schedule_id,),
            ).fetchone()
            if current is None:
                return False
            self._ensure_schedule_name_available(
                current[0],
                current[1],
                exclude_schedule_id=schedule_id,
            )
            cursor = self._db.execute(
                "UPDATE agent_schedules SET enabled=1 WHERE id=?",
                (schedule_id,),
            )
            self._db.commit()
            return cursor.rowcount > 0

    def update_schedule_last_run(
        self,
        schedule_id: int,
        timestamp: float = 0.0,
        *,
        expected_last_run: float | None = None,
    ) -> bool:
        """Record a fire, optionally only if ``last_run`` is unchanged."""
        ts = timestamp or time.time()
        if expected_last_run is None:
            cursor = self._db.execute(
                "UPDATE agent_schedules SET last_run=? WHERE id=?",
                (ts, schedule_id),
            )
        else:
            cursor = self._db.execute(
                "UPDATE agent_schedules SET last_run=? WHERE id=? AND last_run=?",
                (ts, schedule_id, expected_last_run),
            )
        self._db.commit()
        return cursor.rowcount > 0

    def update_schedule_last_delivered(
        self, schedule_id: int, timestamp: float = 0.0
    ) -> None:
        """Record when the session confirmed acceptance of a fired prompt."""
        ts = timestamp or time.time()
        self._db.execute(
            "UPDATE agent_schedules SET last_delivered=? WHERE id=?",
            (ts, schedule_id),
        )
        self._db.commit()

    def record_recurring_schedule_stale_drop(
        self,
        schedule_id: int,
        *,
        agent_name: str,
        schedule_name: str,
        dropped_at: float,
        row_age_s: float,
    ) -> RecurringScheduleStaleDrop:
        """Aggregate one stale recurring fire into one bounded schedule row.

        The table has exactly one row per live schedule and cascades on
        schedule deletion. ``generation`` is a non-reusable revision: an
        acknowledgement retains a zero-count tombstone, so a later drop can
        never recycle the revision held by an older receipt observer.
        """
        if schedule_id <= 0:
            raise ValueError("schedule_id must be positive")
        if not agent_name:
            raise ValueError("agent_name must not be empty")
        if dropped_at <= 0:
            raise ValueError("dropped_at must be positive")
        bounded_name = str(schedule_name or "")[:256]
        bounded_age = max(0.0, float(row_age_s))
        with self._rmw_lock:
            self._db.execute(
                """INSERT INTO recurring_schedule_stale_drops (
                       schedule_id, agent_name, schedule_name, drop_count,
                       first_dropped_at, last_dropped_at, max_row_age_s,
                       generation
                   ) VALUES (?, ?, ?, 1, ?, ?, ?, 1)
                   ON CONFLICT(schedule_id) DO UPDATE SET
                       agent_name=excluded.agent_name,
                       schedule_name=excluded.schedule_name,
                       drop_count=CASE
                           WHEN recurring_schedule_stale_drops.drop_count = 0
                           THEN 1
                           ELSE recurring_schedule_stale_drops.drop_count + 1
                       END,
                       first_dropped_at=CASE
                           WHEN recurring_schedule_stale_drops.drop_count = 0
                           THEN excluded.first_dropped_at
                           ELSE MIN(
                               recurring_schedule_stale_drops.first_dropped_at,
                               excluded.first_dropped_at
                           )
                       END,
                       last_dropped_at=CASE
                           WHEN recurring_schedule_stale_drops.drop_count = 0
                           THEN excluded.last_dropped_at
                           ELSE MAX(
                               recurring_schedule_stale_drops.last_dropped_at,
                               excluded.last_dropped_at
                           )
                       END,
                       max_row_age_s=CASE
                           WHEN recurring_schedule_stale_drops.drop_count = 0
                           THEN excluded.max_row_age_s
                           ELSE MAX(
                               recurring_schedule_stale_drops.max_row_age_s,
                               excluded.max_row_age_s
                           )
                       END,
                       generation=recurring_schedule_stale_drops.generation + 1""",
                (
                    schedule_id,
                    agent_name,
                    bounded_name,
                    dropped_at,
                    dropped_at,
                    bounded_age,
                ),
            )
            row = self._db.execute(
                """SELECT schedule_id, agent_name, schedule_name, drop_count,
                          first_dropped_at, last_dropped_at, max_row_age_s,
                          generation
                   FROM recurring_schedule_stale_drops
                   WHERE schedule_id=?""",
                (schedule_id,),
            ).fetchone()
            self._db.commit()
        return RecurringScheduleStaleDrop(*row)

    def list_recurring_schedule_stale_drops(
        self, agent_name: str
    ) -> list[RecurringScheduleStaleDrop]:
        """Return unsurfaced recurring-drop aggregates in stable order."""
        rows = self._db.execute(
            """SELECT schedule_id, agent_name, schedule_name, drop_count,
                      first_dropped_at, last_dropped_at, max_row_age_s,
                      generation
               FROM recurring_schedule_stale_drops
               WHERE agent_name=? AND drop_count > 0
               ORDER BY schedule_id ASC""",
            (agent_name,),
        ).fetchall()
        return [RecurringScheduleStaleDrop(*row) for row in rows]

    def acknowledge_recurring_schedule_stale_drops(
        self,
        agent_name: str,
        notices: list[RecurringScheduleStaleDrop],
    ) -> int:
        """Clear only versioned aggregates included in a confirmed delivery.

        Clearing retains a zero-count revision tombstone.  Deleting the row
        would let a later INSERT restart at generation 1, allowing a delayed
        acknowledgement for an older generation-1 snapshot to erase the new
        unsurfaced casualty (an ABA race).
        """
        if not notices:
            return 0
        cleared = 0
        with self._rmw_lock:
            for notice in notices:
                if notice.agent_name != agent_name:
                    continue
                cursor = self._db.execute(
                    """UPDATE recurring_schedule_stale_drops
                       SET drop_count=0
                       WHERE agent_name=? AND schedule_id=? AND generation=?
                         AND drop_count > 0""",
                    (agent_name, notice.schedule_id, notice.generation),
                )
                cleared += cursor.rowcount
            self._db.commit()
        return cleared

    def claim_schedule_fire(
        self,
        schedule_id: int,
        *,
        timestamp: float,
        expected_last_run: float,
        agent_name: str,
        schedule_name: str,
        prompt: str,
    ) -> tuple[bool, PendingScheduleWake | None]:
        """Atomically claim one fire and create its durable ledger/outbox row.

        A crash must never leave ``last_run`` advanced without an exact-fire
        row that startup can classify.  This transaction closes that earlier
        fire-claim/outbox gap while preserving the compare-and-swap race guard.
        """
        if timestamp <= 0:
            raise ValueError("timestamp must be a positive exact-fire timestamp")
        created_at = time.time()
        with self._rmw_lock:
            cursor = self._db.execute(
                """UPDATE agent_schedules SET last_run=?
                   WHERE id=? AND last_run=?""",
                (timestamp, schedule_id, expected_last_run),
            )
            if cursor.rowcount == 0:
                self._db.commit()
                return False, None
            self._db.execute(
                """INSERT OR IGNORE INTO pending_schedule_wakes (
                       schedule_id, agent_name, schedule_name, prompt,
                       fired_at, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    schedule_id,
                    agent_name,
                    schedule_name,
                    prompt,
                    timestamp,
                    created_at,
                ),
            )
            row = self._select_schedule_wake_by_fire(
                schedule_id, timestamp
            )
            self._db.commit()
        return True, PendingScheduleWake(*row)

    def _select_schedule_wake_by_fire(
        self, schedule_id: int, fired_at: float
    ):
        return self._db.execute(
            """SELECT id, schedule_id, agent_name, schedule_name, prompt,
                      fired_at, created_at, attempts, parked_at, accepted_at,
                      failed_at, last_error, abandoned_at
               FROM pending_schedule_wakes
               WHERE schedule_id=? AND fired_at=?""",
            (schedule_id, fired_at),
        ).fetchone()

    def persist_schedule_wake(
        self,
        schedule_id: int,
        *,
        agent_name: str,
        schedule_name: str,
        prompt: str,
        fired_at: float,
    ) -> tuple[PendingScheduleWake, bool]:
        """Durably retain one fired wake until exact delivery is confirmed.

        ``(schedule_id, fired_at)`` identifies one cron fire and makes repeated
        accounting of the same failed receipt idempotent. Callers must carry
        that immutable timestamp with the fired cohort; rereading mutable
        ``last_run`` here can collapse overlapping fires into one row.
        """
        if fired_at <= 0:
            raise ValueError("fired_at must be a positive exact-fire timestamp")
        created_at = time.time()
        with self._rmw_lock:
            cursor = self._db.execute(
                """INSERT OR IGNORE INTO pending_schedule_wakes (
                       schedule_id, agent_name, schedule_name, prompt,
                       fired_at, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    schedule_id,
                    agent_name,
                    schedule_name,
                    prompt,
                    fired_at,
                    created_at,
                ),
            )
            created = cursor.rowcount > 0
            row = self._db.execute(
                """SELECT id, schedule_id, agent_name, schedule_name, prompt,
                          fired_at, created_at, attempts, parked_at, accepted_at,
                          failed_at, last_error, abandoned_at
                   FROM pending_schedule_wakes
                   WHERE schedule_id=? AND fired_at=?""",
                (schedule_id, fired_at),
            ).fetchone()
            self._db.commit()
        return PendingScheduleWake(*row), created

    def list_pending_schedule_wakes(
        self,
        agent_name: str | None = None,
        *,
        include_parked: bool = False,
    ) -> list[PendingScheduleWake]:
        """Return pending scheduler wakes oldest-first.

        Accepted receipts are always excluded. Quarantined and abandoned rows
        are excluded by default; pass ``include_parked=True`` to inspect those
        terminal records alongside the active replay outbox.
        """
        sql = """SELECT id, schedule_id, agent_name, schedule_name, prompt,
                        fired_at, created_at, attempts, parked_at, accepted_at,
                        failed_at, last_error, abandoned_at
                 FROM pending_schedule_wakes"""
        conditions: list[str] = []
        params: list = []
        if agent_name is not None:
            conditions.append("agent_name=?")
            params.append(agent_name)
        conditions.append("accepted_at=0")
        if not include_parked:
            conditions.extend(("parked_at=0", "abandoned_at=0"))
        if conditions:
            sql += " WHERE " + " AND ".join(conditions)
        sql += " ORDER BY fired_at ASC, id ASC"
        rows = self._db.execute(sql, params).fetchall()
        return [PendingScheduleWake(*row) for row in rows]

    def get_pending_schedule_wake_health(
        self,
        agent_name: str | None = None,
        *,
        now: float | None = None,
    ) -> list[dict]:
        """Return active outbox debt and abandoned history per agent.

        Accepted receipts and quarantined rows are terminal ledger history and
        remain excluded. Explicitly abandoned rows surface as their own count,
        never as active replay debt.
        """
        sql = """SELECT agent_name,
                        SUM(CASE WHEN abandoned_at=0 THEN 1 ELSE 0 END),
                        MIN(CASE WHEN abandoned_at=0 THEN fired_at END),
                        MAX(CASE WHEN abandoned_at=0 THEN fired_at END),
                        SUM(CASE WHEN abandoned_at>0 THEN 1 ELSE 0 END)
                 FROM pending_schedule_wakes
                 WHERE accepted_at=0 AND parked_at=0"""
        params: list = []
        if agent_name is not None:
            sql += " AND agent_name=?"
            params.append(agent_name)
        sql += " GROUP BY agent_name ORDER BY agent_name"
        rows = self._db.execute(sql, params).fetchall()
        observed_at = time.time() if now is None else float(now)
        result = []
        for row in rows:
            oldest_fired_at = float(row[2]) if row[2] is not None else 0.0
            newest_fired_at = float(row[3]) if row[3] is not None else 0.0
            result.append(
                {
                    "agent_name": str(row[0]),
                    "count": int(row[1]),
                    "abandoned_count": int(row[4]),
                    "oldest_fired_at": oldest_fired_at,
                    "newest_fired_at": newest_fired_at,
                    "oldest_age_seconds": (
                        max(0.0, observed_at - oldest_fired_at)
                        if oldest_fired_at > 0
                        else 0.0
                    ),
                }
            )
        return result

    def list_schedule_wake_ledger(
        self,
        agent_name: str | None = None,
        *,
        state: str | None = None,
        fired_after: float = 0.0,
        limit: int = 200,
    ) -> list[PendingScheduleWake]:
        """Return queryable exact-fire outcomes newest-first for fleet health."""
        if state not in {
            None,
            "pending",
            "receipted-ran-once",
            "quarantined",
            "abandoned",
        }:
            raise ValueError(f"invalid scheduler wake ledger state: {state}")
        sql = """SELECT id, schedule_id, agent_name, schedule_name, prompt,
                        fired_at, created_at, attempts, parked_at, accepted_at,
                        failed_at, last_error, abandoned_at
                 FROM pending_schedule_wakes"""
        conditions: list[str] = []
        params: list = []
        if agent_name is not None:
            conditions.append("agent_name=?")
            params.append(agent_name)
        if fired_after > 0:
            conditions.append("fired_at>=?")
            params.append(fired_after)
        if state == "pending":
            conditions.extend(
                ("accepted_at=0", "parked_at=0", "abandoned_at=0")
            )
        elif state == "receipted-ran-once":
            conditions.append("accepted_at>0")
        elif state == "quarantined":
            conditions.extend(
                ("accepted_at=0", "parked_at>0", "abandoned_at=0")
            )
        elif state == "abandoned":
            conditions.extend(
                ("accepted_at=0", "parked_at=0", "abandoned_at>0")
            )
        if conditions:
            sql += " WHERE " + " AND ".join(conditions)
        sql += " ORDER BY fired_at DESC, id DESC LIMIT ?"
        params.append(max(1, min(int(limit), 1000)))
        rows = self._db.execute(sql, params).fetchall()
        return [PendingScheduleWake(*row) for row in rows]

    def get_schedule_wake_by_fire(
        self, schedule_id: int, fired_at: float
    ) -> PendingScheduleWake | None:
        """Return one exact-fire ledger row regardless of terminal state."""
        row = self._select_schedule_wake_by_fire(schedule_id, fired_at)
        return PendingScheduleWake(*row) if row is not None else None

    def record_schedule_wake_failure(
        self, schedule_id: int, fired_at: float, reason: str
    ) -> tuple[PendingScheduleWake | None, bool]:
        """Record a negative hint unless durable terminal evidence exists.

        Returns the current row and whether this was its first negative hint.
        A retained accepted receipt always wins over a cancelled/stale Future.
        """
        timestamp = time.time()
        with self._rmw_lock:
            row = self._select_schedule_wake_by_fire(schedule_id, fired_at)
            if row is None:
                return None, False
            current = PendingScheduleWake(*row)
            if (
                current.accepted_at > 0
                or current.parked_at > 0
                or current.abandoned_at > 0
            ):
                return current, False
            first_failure = current.failed_at == 0
            self._db.execute(
                """UPDATE pending_schedule_wakes
                   SET failed_at=CASE WHEN failed_at=0 THEN ? ELSE failed_at END,
                       last_error=?
                   WHERE id=? AND accepted_at=0 AND parked_at=0
                     AND abandoned_at=0""",
                (timestamp, reason, current.id),
            )
            row = self._select_schedule_wake_by_fire(schedule_id, fired_at)
            self._db.commit()
        return PendingScheduleWake(*row), first_failure

    def increment_pending_schedule_wake_attempts(
        self, pending_id: int
    ) -> int | None:
        """Persist one delivery attempt and return its new count."""
        with self._rmw_lock:
            cursor = self._db.execute(
                """UPDATE pending_schedule_wakes
                   SET attempts=attempts + 1
                   WHERE id=? AND accepted_at=0 AND parked_at=0
                     AND abandoned_at=0""",
                (pending_id,),
            )
            if cursor.rowcount == 0:
                self._db.commit()
                return None
            row = self._db.execute(
                "SELECT attempts FROM pending_schedule_wakes WHERE id=?",
                (pending_id,),
            ).fetchone()
            self._db.commit()
        return int(row[0])

    def park_pending_schedule_wake(
        self,
        pending_id: int,
        *,
        parked_at: float = 0.0,
        reason: str = "delivery attempts exhausted",
    ) -> bool:
        """Atomically move an active wake to terminal quarantine once."""
        timestamp = parked_at or time.time()
        with self._rmw_lock:
            cursor = self._db.execute(
                """UPDATE pending_schedule_wakes
                   SET parked_at=?,
                       failed_at=CASE WHEN failed_at=0 THEN ? ELSE failed_at END,
                       last_error=?
                   WHERE id=? AND parked_at=0 AND accepted_at=0
                     AND abandoned_at=0""",
                (timestamp, timestamp, reason, pending_id),
            )
            self._db.commit()
        return cursor.rowcount > 0

    def abandon_pending_schedule_wake(
        self,
        pending_id: int,
        *,
        abandoned_at: float = 0.0,
        reason: str = "receipt confirmation ceiling exceeded",
    ) -> bool:
        """Atomically move an active wake to explicit abandonment once.

        Abandonment is distinct from delivery quarantine: it records the
        ambiguous receipt-gap outcome without replaying or discarding the row.
        A later positive receipt remains authoritative and clears this marker.
        """
        timestamp = abandoned_at or time.time()
        with self._rmw_lock:
            cursor = self._db.execute(
                """UPDATE pending_schedule_wakes
                   SET abandoned_at=?,
                       failed_at=CASE WHEN failed_at=0 THEN ? ELSE failed_at END,
                       last_error=?
                   WHERE id=? AND parked_at=0 AND accepted_at=0
                     AND abandoned_at=0""",
                (timestamp, timestamp, reason, pending_id),
            )
            self._db.commit()
        return cursor.rowcount > 0

    def collapse_pending_schedule_wake(
        self,
        pending_id: int,
        *,
        superseded_by_fired_at: float,
        collapsed_at: float = 0.0,
    ) -> bool:
        """Quarantine one recurrence superseded by a newer pending fire."""
        timestamp = collapsed_at or time.time()
        reason = (
            "recurrence collapsed into newer pending fire "
            f"fired_at={superseded_by_fired_at}"
        )
        with self._rmw_lock:
            cursor = self._db.execute(
                """UPDATE pending_schedule_wakes
                   SET parked_at=?,
                       failed_at=CASE WHEN failed_at=0 THEN ? ELSE failed_at END,
                       last_error=?
                   WHERE id=? AND parked_at=0 AND accepted_at=0
                     AND abandoned_at=0""",
                (timestamp, timestamp, reason, pending_id),
            )
            self._db.commit()
        return cursor.rowcount > 0

    def confirm_pending_schedule_wake(
        self, pending_id: int, *, delivered_at: float = 0.0
    ) -> bool:
        """Atomically retain a positive receipt and retire it from replay."""
        timestamp = delivered_at or time.time()
        with self._rmw_lock:
            row = self._db.execute(
                """SELECT schedule_id, accepted_at
                   FROM pending_schedule_wakes WHERE id=?""",
                (pending_id,),
            ).fetchone()
            if row is None:
                return False
            self._db.execute(
                """UPDATE agent_schedules
                   SET last_delivered=MAX(last_delivered, ?)
                   WHERE id=?""",
                (timestamp, row[0]),
            )
            self._db.execute(
                """UPDATE pending_schedule_wakes
                   SET accepted_at=CASE
                           WHEN accepted_at=0 THEN ? ELSE accepted_at END,
                       parked_at=0,
                       abandoned_at=0,
                       last_error=''
                   WHERE id=?""",
                (timestamp, pending_id),
            )
            self._db.commit()
        return True

    def confirm_pending_schedule_wake_by_fire(
        self,
        schedule_id: int,
        fired_at: float,
        *,
        delivered_at: float = 0.0,
    ) -> bool:
        """Atomically persist acceptance for one exact fire before replay."""
        timestamp = delivered_at or time.time()
        with self._rmw_lock:
            row = self._db.execute(
                """SELECT id, accepted_at FROM pending_schedule_wakes
                   WHERE schedule_id=? AND fired_at=?""",
                (schedule_id, fired_at),
            ).fetchone()
            if row is None:
                return False
            self._db.execute(
                """UPDATE agent_schedules
                   SET last_delivered=MAX(last_delivered, ?)
                   WHERE id=?""",
                (timestamp, schedule_id),
            )
            self._db.execute(
                """UPDATE pending_schedule_wakes
                   SET accepted_at=CASE
                           WHEN accepted_at=0 THEN ? ELSE accepted_at END,
                       parked_at=0,
                       abandoned_at=0,
                       last_error=''
                   WHERE schedule_id=? AND fired_at=?""",
                (timestamp, schedule_id, fired_at),
            )
            self._db.commit()
        newly_receipted = float(row[1]) == 0
        if newly_receipted:
            _log(
                "agent_registry: SCHEDULE_WAKE_RECEIPTED "
                f"pending #{row[0]}, schedule #{schedule_id}, "
                f"fired_at={fired_at}"
            )
        return True

    def discard_pending_schedule_wake(
        self,
        pending_id: int,
        *,
        agent_name: str | None = None,
    ) -> bool:
        """Retire an active pending wake by TOMBSTONING it (not deleting).

        Sets ``parked_at`` so the row survives as a terminal marker instead of
        being erased. This is load-bearing: a bare ``DELETE`` left no durable
        "retired" record, so a later reconciliation — finding the schedule's
        ``last_run`` advanced but no outbox row and no accepted receipt — would
        re-create and re-fire an already-handled fire. Keeping the row preserves
        the ``(schedule_id, fired_at)`` key (a re-persist's ``INSERT OR IGNORE``
        then no-ops), and the parked row is excluded from the active replay
        outbox (``list_pending_schedule_wakes`` default), so the fire is neither
        re-created nor re-delivered.

        ``agent_name`` scopes self-service callers to their own outbox. Rows that
        are already accepted, parked, or abandoned are terminal evidence and
        are left as-is.

        Parked tombstones accumulate until a terminal-row reaper prunes them
        (tracked separately; accepted rows already accumulate the same way).
        """
        sql = """UPDATE pending_schedule_wakes
                 SET parked_at=?, last_error=?
                 WHERE id=? AND accepted_at=0 AND parked_at=0
                   AND abandoned_at=0"""
        params: list = [time.time(), "retired via discard", pending_id]
        if agent_name is not None:
            sql += " AND agent_name=?"
            params.append(agent_name)
        with self._rmw_lock:
            cursor = self._db.execute(sql, params)
            self._db.commit()
            return cursor.rowcount > 0

    def delete_pending_schedule_wake(self, pending_id: int) -> bool:
        """Hard-delete an active pending wake (no tombstone).

        Used by the internal replay stale-drop pass, which fires at the cadence
        of frequently-recurring schedules — tombstoning there would accumulate
        rows unboundedly without a reaper. Deliberate/manual retirement uses
        ``discard_pending_schedule_wake`` (which tombstones) so a retired fire
        cannot be re-created by a later reconciliation. Accepted, parked, or
        abandoned rows are terminal and are left as-is.
        """
        with self._rmw_lock:
            cursor = self._db.execute(
                """DELETE FROM pending_schedule_wakes
                   WHERE id=? AND accepted_at=0 AND parked_at=0
                     AND abandoned_at=0""",
                (pending_id,),
            )
            self._db.commit()
            return cursor.rowcount > 0

    def reap_pending_schedule_wakes(
        self,
        *,
        now: float,
        abandon_after: float,
        retain_accepted: float,
        retain_abandoned: float,
        retain_parked: float,
        payload_trim_after: float,
        batch_size: int = OUTBOX_REAPER_BATCH_SIZE,
    ) -> list[dict[str, int | str]]:
        """Transition stranded wakes and prune bounded terminal history.

        This maintenance pass never delivers or re-queues work. Active rows
        cross into explicit abandonment only after the strict fired-at
        ceiling. Legacy ``RECEIPT_ABANDONED`` quarantine markers are reclassified
        into the same explicit state. Retention is measured from each transition,
        so every newly backfilled abandonment survives its full forensic window.
        Large populations are committed in bounded chunks while the registry's
        mutation lock prevents in-process replay races.
        """
        observed_at = float(now)
        windows = {
            "abandon_after": float(abandon_after),
            "retain_accepted": float(retain_accepted),
            "retain_abandoned": float(retain_abandoned),
            "retain_parked": float(retain_parked),
            "payload_trim_after": float(payload_trim_after),
        }
        if not math.isfinite(observed_at) or observed_at <= 0:
            raise ValueError("outbox reaper now must be positive")
        if any(
            not math.isfinite(value) or value <= 0
            for value in windows.values()
        ):
            raise ValueError("outbox reaper windows must be positive")
        if batch_size <= 0:
            raise ValueError("outbox reaper batch_size must be positive")

        metric_fields = (
            "abandoned",
            "accepted_reaped",
            "abandoned_reaped",
            "parked_reaped",
            "payloads_trimmed",
            "retained_active",
        )
        metrics: dict[str, dict[str, int | str]] = {}

        def _agent_metrics(agent_name: str) -> dict[str, int | str]:
            if agent_name not in metrics:
                metrics[agent_name] = {
                    "agent_name": agent_name,
                    **{field: 0 for field in metric_fields},
                }
            return metrics[agent_name]

        def _apply_batched(
            sql: str,
            params: tuple,
            metric_field: str,
        ) -> None:
            while True:
                rows = self._db.execute(
                    sql,
                    (*params, int(batch_size)),
                ).fetchall()
                self._db.commit()
                for row in rows:
                    agent_metrics = _agent_metrics(str(row[0]))
                    agent_metrics[metric_field] = (
                        int(agent_metrics[metric_field]) + 1
                    )
                if len(rows) < batch_size:
                    return

        def _refresh_retained_active() -> None:
            for agent_metrics in metrics.values():
                agent_metrics["retained_active"] = 0
            active_rows = self._db.execute(
                """SELECT agent_name, COUNT(*)
                   FROM pending_schedule_wakes
                   WHERE accepted_at=0 AND parked_at=0 AND abandoned_at=0
                   GROUP BY agent_name"""
            ).fetchall()
            for agent_name, count in active_rows:
                _agent_metrics(str(agent_name))["retained_active"] = int(count)

        def _emit_log(
            message: str,
            *,
            emission_errors: list[Exception] | None = None,
        ) -> None:
            if emission_errors is None:
                _log(message)
                return
            try:
                _log(message)
            except Exception as exc:
                emission_errors.append(exc)

        def _emit_metrics(
            *,
            failed_phase: str | None = None,
            emission_errors: list[Exception] | None = None,
        ) -> list[dict[str, int | str]]:
            result = [metrics[name] for name in sorted(metrics)]
            for row in result:
                _emit_log(
                    f"outbox-reaper: agent={row['agent_name']} "
                    f"abandoned=+{row['abandoned']} "
                    f"accepted_reaped={row['accepted_reaped']} "
                    f"abandoned_reaped={row['abandoned_reaped']} "
                    f"parked_reaped={row['parked_reaped']} "
                    f"payloads_trimmed={row['payloads_trimmed']} "
                    f"retained_active={row['retained_active']}",
                    emission_errors=emission_errors,
                )
            totals = {
                field: sum(int(row[field]) for row in result)
                for field in metric_fields
            }
            partial = (
                f" PARTIAL failed_phase={failed_phase}"
                if failed_phase is not None
                else ""
            )
            _emit_log(
                f"outbox-reaper: summary{partial} agents={len(result)} "
                f"abandoned=+{totals['abandoned']} "
                f"accepted_reaped={totals['accepted_reaped']} "
                f"abandoned_reaped={totals['abandoned_reaped']} "
                f"parked_reaped={totals['parked_reaped']} "
                f"payloads_trimmed={totals['payloads_trimmed']} "
                f"retained_active={totals['retained_active']}",
                emission_errors=emission_errors,
            )
            return result

        discard_reason = "retired via discard"
        abandon_cutoff = observed_at - windows["abandon_after"]
        accepted_cutoff = observed_at - windows["retain_accepted"]
        abandoned_cutoff = observed_at - windows["retain_abandoned"]
        parked_cutoff = observed_at - windows["retain_parked"]
        trim_cutoff = observed_at - windows["payload_trim_after"]

        failed_phase = "agent_scan"
        with self._rmw_lock:
            try:
                for row in self._db.execute(
                    "SELECT DISTINCT agent_name FROM pending_schedule_wakes"
                ).fetchall():
                    _agent_metrics(str(row[0]))

                failed_phase = "abandon"
                _apply_batched(
                    """UPDATE pending_schedule_wakes
                       SET abandoned_at=?, parked_at=0
                       WHERE id IN (
                           SELECT id FROM pending_schedule_wakes
                           WHERE accepted_at=0 AND abandoned_at=0
                             AND (
                                 (parked_at=0 AND fired_at < ?)
                                 OR (
                                     parked_at>0
                                     AND last_error LIKE 'RECEIPT_ABANDONED:%'
                                 )
                             )
                           ORDER BY id ASC LIMIT ?
                       )
                       RETURNING agent_name""",
                    (observed_at, abandon_cutoff),
                    "abandoned",
                )
                failed_phase = "accepted_reap"
                _apply_batched(
                    """DELETE FROM pending_schedule_wakes
                       WHERE id IN (
                           SELECT id FROM pending_schedule_wakes
                           WHERE accepted_at>0 AND accepted_at < ?
                           ORDER BY id ASC LIMIT ?
                       )
                       RETURNING agent_name""",
                    (accepted_cutoff,),
                    "accepted_reaped",
                )
                failed_phase = "abandoned_reap"
                _apply_batched(
                    """DELETE FROM pending_schedule_wakes
                       WHERE id IN (
                           SELECT id FROM pending_schedule_wakes
                           WHERE (
                               accepted_at=0 AND parked_at=0
                               AND abandoned_at>0 AND abandoned_at < ?
                           ) OR (
                               accepted_at=0 AND abandoned_at=0
                               AND parked_at>0 AND parked_at < ?
                               AND last_error=?
                           )
                           ORDER BY id ASC LIMIT ?
                       )
                       RETURNING agent_name""",
                    (abandoned_cutoff, abandoned_cutoff, discard_reason),
                    "abandoned_reaped",
                )
                failed_phase = "parked_reap"
                _apply_batched(
                    """DELETE FROM pending_schedule_wakes AS parked
                       WHERE parked.id IN (
                           SELECT candidate.id
                           FROM pending_schedule_wakes AS candidate
                           WHERE candidate.accepted_at=0
                             AND candidate.abandoned_at=0
                             AND candidate.parked_at>0
                             AND candidate.parked_at < ?
                             AND candidate.last_error<>?
                             AND EXISTS (
                                 SELECT 1
                                 FROM pending_schedule_wakes AS newer
                                 WHERE newer.schedule_id=candidate.schedule_id
                                   AND newer.accepted_at=0
                                   AND newer.abandoned_at=0
                                   AND newer.parked_at>0
                                   AND newer.last_error<>?
                                   AND (
                                       newer.parked_at > candidate.parked_at
                                       OR (
                                           newer.parked_at=candidate.parked_at
                                           AND newer.id > candidate.id
                                       )
                                   )
                             )
                           ORDER BY candidate.id ASC LIMIT ?
                       )
                       RETURNING agent_name""",
                    (parked_cutoff, discard_reason, discard_reason),
                    "parked_reaped",
                )
                failed_phase = "payload_trim"
                _apply_batched(
                    """UPDATE pending_schedule_wakes AS terminal
                       SET prompt=?
                       WHERE terminal.id IN (
                           SELECT candidate.id
                           FROM pending_schedule_wakes AS candidate
                           WHERE candidate.prompt<>?
                             AND (
                                 (
                                     candidate.accepted_at>0
                                     AND candidate.accepted_at < ?
                                 ) OR (
                                     candidate.accepted_at=0
                                     AND candidate.parked_at=0
                                     AND candidate.abandoned_at>0
                                     AND candidate.abandoned_at < ?
                                 ) OR (
                                     candidate.accepted_at=0
                                     AND candidate.abandoned_at=0
                                     AND candidate.parked_at>0
                                     AND candidate.parked_at < ?
                                     AND candidate.last_error=?
                                 ) OR (
                                     candidate.accepted_at=0
                                     AND candidate.abandoned_at=0
                                     AND candidate.parked_at>0
                                     AND candidate.parked_at < ?
                                     AND candidate.last_error<>?
                                     AND EXISTS (
                                         SELECT 1
                                         FROM pending_schedule_wakes AS newer
                                         WHERE newer.schedule_id=candidate.schedule_id
                                           AND newer.accepted_at=0
                                           AND newer.abandoned_at=0
                                           AND newer.parked_at>0
                                           AND newer.last_error<>?
                                           AND (
                                               newer.parked_at > candidate.parked_at
                                               OR (
                                                   newer.parked_at=candidate.parked_at
                                                   AND newer.id > candidate.id
                                               )
                                           )
                                     )
                                 )
                             )
                           ORDER BY candidate.id ASC LIMIT ?
                       )
                       RETURNING agent_name""",
                    (
                        OUTBOX_REAPER_PAYLOAD_TRIMMED,
                        OUTBOX_REAPER_PAYLOAD_TRIMMED,
                        trim_cutoff,
                        trim_cutoff,
                        trim_cutoff,
                        discard_reason,
                        trim_cutoff,
                        discard_reason,
                        discard_reason,
                    ),
                    "payloads_trimmed",
                )

                failed_phase = "retained_active"
                _refresh_retained_active()
            except Exception:
                self._db.rollback()
                try:
                    _refresh_retained_active()
                except Exception:
                    # Preserve and report the maintenance failure even if the
                    # diagnostic read is unavailable on a broken connection.
                    pass
                emission_errors: list[Exception] = []
                try:
                    _emit_metrics(
                        failed_phase=failed_phase,
                        emission_errors=emission_errors,
                    )
                except Exception as exc:
                    emission_errors.append(exc)
                _emit_log(
                    "outbox-reaper: ERROR maintenance pass failed",
                    emission_errors=emission_errors,
                )
                for emission_error in tuple(emission_errors):
                    try:
                        _log(
                            "outbox-reaper: emission ERROR while reporting "
                            "maintenance failure: "
                            f"{type(emission_error).__name__}: "
                            f"{emission_error}"
                        )
                    except Exception:
                        pass
                raise

        return _emit_metrics()

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

    # ── Effort Drift Events (#429) ───────────────────────

    def record_effort_drift(
        self,
        agent_name: str,
        *,
        expected: str,
        actual: str,
        session_id: str = "",
        tool_name: str = "",
        strict: bool = False,
    ) -> int:
        """Record a thinking-effort drift event from the verify_effort CLI hook.

        Emitted when ``$CLAUDE_EFFORT`` (runtime) diverges from the agent's
        configured ``thinking_effort``. See #429 / verify_effort hook.

        Also writes a structured note to the heartbeat stream so drift is
        visible alongside normal liveness telemetry without a separate
        query.

        Returns the inserted event row id.
        """
        now = time.time()
        cursor = self._db.execute(
            """INSERT INTO effort_drift_events
               (agent_name, session_id, expected, actual, tool_name, strict, timestamp)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (agent_name, session_id, expected, actual, tool_name,
             int(bool(strict)), now),
        )
        self._db.commit()

        # Mirror to heartbeat notes so it shows up in normal observability.
        # Don't let heartbeat failure break the drift recording.
        try:
            label = "blocked" if strict else "warn"
            note = (
                f"[effort drift / {label}] expected={expected} actual={actual}"
            )
            if tool_name:
                note += f" tool={tool_name}"
            self.record_heartbeat(
                agent_name,
                session_id=session_id,
                status="alive",
                notes=note,
            )
        except Exception as e:  # pragma: no cover — defensive
            _log(f"agent_registry: effort-drift heartbeat note failed: {e}")

        return int(cursor.lastrowid or 0)

    def get_effort_drift_events(
        self,
        agent_name: str = "",
        *,
        limit: int = 50,
        since: float = 0.0,
    ) -> list[dict]:
        """Query recent effort-drift events.

        Pass ``agent_name=""`` to get fleet-wide events. ``since`` is a unix
        timestamp; only events after it are returned.
        """
        conditions: list[str] = []
        params: list = []
        if agent_name:
            conditions.append("agent_name=?")
            params.append(agent_name)
        if since:
            conditions.append("timestamp>?")
            params.append(since)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        params.append(limit)
        rows = self._db.execute(
            f"""SELECT id, agent_name, session_id, expected, actual,
                       tool_name, strict, timestamp
                FROM effort_drift_events {where}
                ORDER BY timestamp DESC LIMIT ?""",
            params,
        ).fetchall()
        return [
            {
                "id": r[0],
                "agent_name": r[1],
                "session_id": r[2],
                "expected": r[3],
                "actual": r[4],
                "tool_name": r[5],
                "strict": bool(r[6]),
                "timestamp": r[7],
            }
            for r in rows
        ]

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

    def get_latest_agent_heartbeat(
        self, agent_name: str
    ) -> AgentHeartbeat | None:
        """Get the most recent **agent-origin** heartbeat — excludes
        synthetic ``server_presence`` rows the scheduler writes when
        the streaming session is merely ``CONNECTED``.

        Use this when "is the agent actually responsive?" is the
        question, not "has the daemon seen the session lately?". The
        force-restart endpoint (#103) uses this distinction because
        its target failure mode is exactly "transport CONNECTED but
        reader loop wedged on an LLM call" — the scheduler keeps
        writing fresh ``server_presence`` rows in that case, so
        ``get_latest_heartbeat`` would look healthy while the agent
        is actually dead in the water (Murzik review of #573).

        Filter (two cuts, both required):

        1. ``metadata.source`` is NULL or != 'server_presence' — drops
           scheduler reconciliation rows written when the daemon sees
           a CONNECTED transport. Those carry ``status='alive'`` and
           would mask the wedge if used naively.

        2. ``status NOT IN ('stale', 'dead')`` — drops scheduler
           stale-out / dead-out rows written when an agent misses
           heartbeat windows. Those carry no ``source`` field but
           ``status='stale'`` or ``status='dead'`` — without this cut
           a fresh ``dead`` row from the scheduler would still pass
           the source filter and produce the wrong 'agent-origin
           heartbeat is fresh; not wedged' conclusion (Murzik
           round-2 review of #573).

        Agent-origin heartbeats land with ``status`` in {ok, busy,
        finishing, alive} (the pinky-self MCP ``send_heartbeat()`` uses
        ok/busy/finishing; the tool-use hook + effort-drift recorder
        write ``alive``). All have empty metadata. Both cuts together
        give exactly the "agent actively said it's alive" set.
        """
        row = self._db.execute(
            """SELECT agent_name, session_id, timestamp, status, context_pct, message_count,
                      metadata, notes, latency_ms
               FROM agent_heartbeats
               WHERE agent_name=?
                 AND (json_extract(metadata, '$.source') IS NULL
                      OR json_extract(metadata, '$.source') != 'server_presence')
                 AND status NOT IN ('stale', 'dead')
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
        wake_action: str = "",
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
               (agent_name, task, context, notes, blockers, priority_items,
                wake_action, metadata, updated_at, updated_by)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT (agent_name) DO UPDATE SET
                task=excluded.task, context=excluded.context, notes=excluded.notes,
                blockers=excluded.blockers, priority_items=excluded.priority_items,
                wake_action=excluded.wake_action,
                metadata=excluded.metadata, updated_at=excluded.updated_at,
                updated_by=excluded.updated_by""",
            (agent_name, task, context, notes,
             json.dumps(blockers or []), json.dumps(priority_items or []),
             wake_action, json.dumps(metadata or {}), now, updated_by),
        )
        self._db.commit()
        _log(f"agents: saved context for {agent_name}")
        return self.get_context(agent_name)  # type: ignore

    def get_context(self, agent_name: str) -> AgentContext | None:
        """Get the saved continuation context for an agent."""
        row = self._db.execute(
            """SELECT agent_name, task, context, notes, blockers, priority_items,
                      wake_action, metadata, updated_at, updated_by
               FROM agent_contexts WHERE agent_name=?""",
            (agent_name,),
        ).fetchone()
        if not row:
            return None
        return AgentContext(
            agent_name=row[0], task=row[1], context=row[2], notes=row[3],
            blockers=json.loads(row[4]), priority_items=json.loads(row[5]),
            wake_action=row[6], metadata=json.loads(row[7]),
            updated_at=row[8], updated_by=row[9],
        )

    def clear_context(self, agent_name: str) -> bool:
        """Clear the continuation context after it's been consumed."""
        cursor = self._db.execute(
            "DELETE FROM agent_contexts WHERE agent_name=?", (agent_name,),
        )
        self._db.commit()
        return cursor.rowcount > 0

    def bump_context_updated_at(
        self, agent_name: str, *, ts: float | None = None
    ) -> bool:
        """Bump the ``updated_at`` timestamp on the saved context to ``ts``
        (default: now). Used by the force-restart-agent escape hatch (task
        #103) to satisfy the restart_safe gate's ``within_buffer`` check
        when the agent is wedged and cannot call ``save_my_context``
        itself.

        Returns ``True`` if a row was updated, ``False`` if no saved
        context exists for the agent.

        Why a dedicated helper: the SQL touches a single column and the
        force-restart code path needs to write it from api.py, which
        should not reach into ``agents._db`` directly. Keeping the
        UPDATE here keeps the persistence layer ownership clean.
        """
        when = ts if ts is not None else time.time()
        cursor = self._db.execute(
            "UPDATE agent_contexts SET updated_at=? WHERE agent_name=?",
            (when, agent_name),
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
               DO UPDATE SET status='approved',
                            display_name=COALESCE(
                                NULLIF(excluded.display_name, ''),
                                approved_users.display_name),
                            approved_by=excluded.approved_by, updated_at=excluded.updated_at""",
            (agent_name, chat_id, display_name, approved_by, now, now),
        )
        # Approval state and its durable notification aggregate must transition
        # together regardless of caller (API, owner command, or migration).
        self._db.execute(
            """UPDATE approval_requests
               SET gate_state='approved', next_retry_at=0, updated_at=?
               WHERE agent_name=? AND chat_id=?""",
            (now, agent_name, chat_id),
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
        self._db.execute(
            """UPDATE approval_requests
               SET gate_state='denied', next_retry_at=0, updated_at=?
               WHERE agent_name=? AND chat_id=?""",
            (now, agent_name, chat_id),
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

    def grandfather_approved_users(
        self, agent_name: str, candidates: list[tuple[str, str]],
    ) -> list[dict]:
        """Approve chats with durable evidence of prior agent participation.

        Existing approved and denied decisions are authoritative and are never
        changed. A qualifying pending row is healed as well: its approval
        request is settled here, while the broker-dependent held-message flush
        is resumed by the API startup migration handler.

        Each candidate is isolated so a malformed legacy row cannot brick
        daemon startup. The caller owns cross-store candidate discovery and the
        one-shot migration marker.
        """
        seeded: list[dict] = []
        seen: set[str] = set()
        for raw_chat_id, raw_display_name in candidates:
            chat_id = str(raw_chat_id or "").strip()
            if not chat_id or chat_id in seen:
                continue
            seen.add(chat_id)
            display_name = str(raw_display_name or "").strip()
            try:
                previous_status = self.get_user_status(agent_name, chat_id)
                if previous_status not in (None, "pending"):
                    continue
                approved = self.approve_user(
                    agent_name,
                    chat_id,
                    display_name,
                    approved_by="grandfather-migration",
                )
                was_pending = previous_status == "pending"
                if was_pending:
                    self.settle_approval_request(agent_name, chat_id, "approved")
                seeded.append({
                    "chat_id": chat_id,
                    "display_name": approved.display_name,
                    "pending_to_approved": was_pending,
                })
            except Exception as exc:
                _log(
                    "ERROR agent_registry: grandfather migration skipped "
                    f"{agent_name}/{chat_id}: {exc}"
                )
        return seeded

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

    def get_user_display_name(self, agent_name: str, chat_id: str) -> str:
        """Get a user's display name. Returns empty string if not found."""
        row = self._db.execute(
            "SELECT display_name FROM approved_users WHERE agent_name=? AND chat_id=?",
            (agent_name, chat_id),
        ).fetchone()
        return (row[0] or "") if row else ""

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
        sender_name: str, content: str, reply_chat_id: str = "",
        is_group: bool = False, sender_id: str = "",
    ) -> int:
        """Queue a message from a pending user. Returns the message ID.

        ``chat_id`` is the approval key — the sender's id for a DM, or the
        channel id for a group/channel — and pending messages are looked up and
        approved by it. ``reply_chat_id`` is where the eventual reply should be
        delivered; for a group/channel message that's the channel id, which
        differs from the sender. Defaults to ``chat_id`` (correct for 1:1 DMs
        where sender == destination). ``is_group`` records whether the held
        message arrived in a group/channel so it re-delivers with the right
        context on approval.
        """
        cursor = self._queue_pending_message_uncommitted(
            agent_name=agent_name, platform=platform, chat_id=chat_id,
            sender_name=sender_name, content=content, reply_chat_id=reply_chat_id,
            is_group=is_group, sender_id=sender_id,
        )
        self._db.commit()
        return cursor

    def _queue_pending_message_uncommitted(
        self, *, agent_name: str, platform: str, chat_id: str,
        sender_name: str, content: str, reply_chat_id: str = "",
        is_group: bool = False, sender_id: str = "",
        db: sqlite3.Connection | None = None,
    ) -> int:
        """Insert one held row without committing (transaction helper)."""
        connection = db or self._db
        now = time.time()
        dest = reply_chat_id or chat_id
        sid = sender_id or chat_id
        cursor = connection.execute(
            """INSERT INTO pending_messages
               (agent_name, platform, chat_id, reply_chat_id, is_group, sender_id, sender_name, content, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (agent_name, platform, chat_id, dest, int(is_group), sid, sender_name, content, now),
        )
        return cursor.lastrowid

    def get_pending_messages(
        self, agent_name: str, chat_id: str = "",
    ) -> list[dict]:
        """Get undelivered pending messages. If chat_id given, filter by it."""
        if chat_id:
            rows = self._db.execute(
                """SELECT id, agent_name, platform, chat_id, reply_chat_id,
                          is_group, sender_id, sender_name, content, created_at
                   FROM pending_messages
                   WHERE agent_name=? AND chat_id=? AND delivered=0
                   ORDER BY created_at ASC""",
                (agent_name, chat_id),
            ).fetchall()
        else:
            rows = self._db.execute(
                """SELECT id, agent_name, platform, chat_id, reply_chat_id,
                          is_group, sender_id, sender_name, content, created_at
                   FROM pending_messages
                   WHERE agent_name=? AND delivered=0
                   ORDER BY created_at ASC""",
                (agent_name,),
            ).fetchall()
        return [
            {
                "id": r[0], "agent_name": r[1], "platform": r[2],
                "chat_id": r[3], "reply_chat_id": r[4] or r[3],
                "is_group": bool(r[5]), "sender_id": r[6] or r[3],
                "sender_name": r[7], "content": r[8], "created_at": r[9],
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

    def mark_pending_message_delivered(self, message_id: int) -> bool:
        """Mark one held message delivered immediately after its route succeeds."""
        cursor = self._db.execute(
            "UPDATE pending_messages SET delivered=1 WHERE id=? AND delivered=0",
            (message_id,),
        )
        self._db.commit()
        return cursor.rowcount > 0

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

    def list_approval_backlogs(
        self,
        agent_name: str = "",
        chat_id: str = "",
        *,
        now: float | None = None,
    ) -> list[dict]:
        """Return every undelivered approval backlog, grouped by agent/chat.

        This deliberately mirrors the fleet-wide incident diagnostic: group
        undelivered ``pending_messages`` by ``(agent_name, chat_id)`` and join
        both the gate status and the original sender's approval status. The
        latter identifies the high-signal case where an approved principal is
        being silently held behind a still-pending group/channel gate.
        """
        where = ["pm.delivered=0"]
        params: list[object] = []
        if agent_name:
            where.append("pm.agent_name=?")
            params.append(agent_name)
        if chat_id:
            where.append("pm.chat_id=?")
            params.append(chat_id)
        rows = self._db.execute(
            f"""SELECT pm.agent_name, pm.chat_id,
                       COALESCE(gate.status, 'missing'),
                       COALESCE(gate.display_name, ''),
                       COUNT(*), MIN(pm.created_at), MAX(pm.created_at),
                       COALESCE(ar.id, 0),
                       COALESCE(ar.notification_state, 'missing'),
                       COALESCE(ar.last_notified_at, 0),
                       COUNT(DISTINCT CASE WHEN principal.status='approved'
                                           THEN pm.sender_id END),
                       GROUP_CONCAT(DISTINCT CASE WHEN principal.status='approved'
                                                  THEN pm.sender_id END)
                FROM pending_messages AS pm
                LEFT JOIN approved_users AS gate
                  ON gate.agent_name=pm.agent_name AND gate.chat_id=pm.chat_id
                LEFT JOIN approved_users AS principal
                  ON principal.agent_name=pm.agent_name
                 AND principal.chat_id=pm.sender_id
                LEFT JOIN approval_requests AS ar
                  ON ar.agent_name=pm.agent_name AND ar.chat_id=pm.chat_id
                WHERE {' AND '.join(where)}
                GROUP BY pm.agent_name, pm.chat_id, gate.status,
                         gate.display_name, ar.id, ar.notification_state,
                         ar.last_notified_at
                ORDER BY MIN(pm.created_at), pm.agent_name, pm.chat_id""",
            params,
        ).fetchall()
        observed_at = time.time() if now is None else now
        return [
            {
                "agent_name": row[0],
                "chat_id": row[1],
                "gate_status": row[2],
                "display_name": row[3],
                "undelivered_count": row[4],
                "oldest_held_at": row[5],
                "newest_held_at": row[6],
                "oldest_age_seconds": max(0, int(observed_at - row[5])),
                "request_id": row[7],
                "notification_state": row[8],
                "last_notified_at": row[9],
                "approved_principal_count": row[10],
                "approved_principal_ids": row[11].split(",") if row[11] else [],
                "high_signal": row[2] != "approved" and row[10] > 0,
            }
            for row in rows
        ]

    def get_approval_backlog_health(self, agent_name: str = "") -> dict:
        """Owner/operator-facing summary of every undelivered approval row."""
        backlogs = self.list_approval_backlogs(agent_name)
        pending = [row for row in backlogs if row["gate_status"] in ("pending", "missing")]
        approved = [row for row in backlogs if row["gate_status"] == "approved"]
        denied = [row for row in backlogs if row["gate_status"] == "denied"]
        high_signal = [row for row in backlogs if row["high_signal"]]
        return {
            "healthy": not backlogs,
            "pending_chats": len(pending),
            "approved_stranded_chats": len(approved),
            "denied_stranded_chats": len(denied),
            "high_signal_chats": len(high_signal),
            "undelivered_messages": sum(row["undelivered_count"] for row in backlogs),
            "backlogs": backlogs,
        }

    # ── Approval Requests (#863 emergency lane) ─────────────

    @staticmethod
    def _approval_request_dict(row) -> dict:
        return {
            "id": row[0], "agent_name": row[1], "chat_id": row[2],
            "target_name": row[3], "is_channel": bool(row[4]),
            "gate_state": row[5], "held_count": row[6],
            "oldest_held_at": row[7], "notification_state": row[8],
            "notification_attempts": row[9], "notified_held_count": row[10],
            "last_notified_at": row[11], "next_retry_at": row[12],
            "last_error": row[13],
            "notification_destination": json.loads(row[14] or "{}"),
            "fallback_path": json.loads(row[15] or "[]"),
            "created_at": row[16], "updated_at": row[17],
            "aging_reprompt_count": row[18],
            "high_signal_alerted_at": row[19],
        }

    def _enrich_approval_request(self, request: dict) -> dict:
        backlogs = self.list_approval_backlogs(
            request["agent_name"], request["chat_id"],
        )
        if backlogs:
            request.update({
                "undelivered_count": backlogs[0]["undelivered_count"],
                "approved_principal_count": backlogs[0]["approved_principal_count"],
                "approved_principal_ids": backlogs[0]["approved_principal_ids"],
                "high_signal": backlogs[0]["high_signal"],
            })
        else:
            request.update({
                "undelivered_count": 0,
                "approved_principal_count": 0,
                "approved_principal_ids": [],
                "high_signal": False,
            })
        return request

    def get_approval_request(self, agent_name: str, chat_id: str) -> dict | None:
        """Return the stable request for a legacy ``(agent, chat_id)`` gate.

        Inc 0 deliberately does not accept platform/team/conversation here.
        Those composite approval keys belong to Inc 1 after row-key migration.
        """
        row = self._db.execute(
            """SELECT id, agent_name, chat_id, target_name, is_channel,
                      gate_state, held_count, oldest_held_at, notification_state,
                      notification_attempts, notified_held_count, last_notified_at,
                      next_retry_at, last_error, notification_destination,
                      fallback_path, created_at, updated_at,
                      aging_reprompt_count, high_signal_alerted_at
               FROM approval_requests WHERE agent_name=? AND chat_id=?""",
            (agent_name, chat_id),
        ).fetchone()
        return self._enrich_approval_request(self._approval_request_dict(row)) if row else None

    def record_approval_hold(
        self, agent_name: str, chat_id: str, *, target_name: str = "",
        is_channel: bool = False, held_at: float | None = None,
    ) -> dict:
        """Create or aggregate one approval request on the legacy gate key."""
        self._record_approval_hold_uncommitted(
            agent_name, chat_id, target_name=target_name,
            is_channel=is_channel, held_at=held_at,
        )
        self._db.commit()
        return self.get_approval_request(agent_name, chat_id)  # type: ignore[return-value]

    def _record_approval_hold_uncommitted(
        self, agent_name: str, chat_id: str, *, target_name: str = "",
        is_channel: bool = False, held_at: float | None = None,
        db: sqlite3.Connection | None = None,
    ) -> None:
        """Upsert the legacy aggregate without committing (transaction helper)."""
        connection = db or self._db
        now = held_at if held_at is not None else time.time()
        connection.execute(
            """INSERT INTO approval_requests
               (agent_name, chat_id, target_name, is_channel, gate_state,
                held_count, oldest_held_at, notification_state, created_at, updated_at)
               VALUES (?, ?, ?, ?, 'pending', 1, ?, 'retrying', ?, ?)
               ON CONFLICT(agent_name, chat_id) DO UPDATE SET
                   target_name=CASE WHEN excluded.target_name != ''
                                    THEN excluded.target_name ELSE target_name END,
                   is_channel=excluded.is_channel,
                   gate_state='pending',
                   held_count=held_count + 1,
                   oldest_held_at=CASE WHEN oldest_held_at=0
                                      THEN excluded.oldest_held_at ELSE oldest_held_at END,
                   updated_at=excluded.updated_at""",
            (agent_name, chat_id, target_name, int(is_channel), now, now, now),
        )

    def queue_pending_message_with_approval_request(
        self, *, agent_name: str, platform: str, chat_id: str,
        sender_name: str, content: str, reply_chat_id: str = "",
        is_group: bool = False, sender_id: str = "", target_name: str = "",
        held_at: float | None = None,
    ) -> tuple[int, dict]:
        """Atomically persist a held row and its legacy approval aggregate.

        The explicit transaction closes the #863 crash boundary: after commit,
        a durable held row always has the request discovered by the restart
        retry loop; before commit, neither side survives.
        """
        transaction_db = sqlite3.connect(
            self._db_path, timeout=5.0, check_same_thread=False,
        )
        transaction_db.execute("PRAGMA busy_timeout=5000")
        transaction_db.execute("PRAGMA foreign_keys=ON")
        try:
            transaction_db.execute("BEGIN IMMEDIATE")
            message_id = self._queue_pending_message_uncommitted(
                agent_name=agent_name, platform=platform, chat_id=chat_id,
                sender_name=sender_name, content=content,
                reply_chat_id=reply_chat_id, is_group=is_group,
                sender_id=sender_id, db=transaction_db,
            )
            self._record_approval_hold_uncommitted(
                agent_name, chat_id, target_name=target_name,
                is_channel=is_group, held_at=held_at, db=transaction_db,
            )
            transaction_db.commit()
        except BaseException:
            transaction_db.rollback()
            raise
        finally:
            transaction_db.close()
        request = self.get_approval_request(agent_name, chat_id)
        if request is None:  # defensive invariant check after successful commit
            raise RuntimeError("approval request missing after atomic hold commit")
        return message_id, request

    def begin_approval_notification(
        self,
        request_id: int,
        *,
        reset_attempts: bool,
        aging_reprompt: bool = False,
    ) -> None:
        """Mark a notification cycle active, optionally resetting re-notify attempts."""
        self._db.execute(
            """UPDATE approval_requests
               SET notification_state='retrying', last_error='', updated_at=?,
                   notification_attempts=CASE WHEN ? THEN 0 ELSE notification_attempts END,
                   aging_reprompt_count=aging_reprompt_count + ?
               WHERE id=? AND gate_state='pending'""",
            (time.time(), int(reset_attempts), int(aging_reprompt), request_id),
        )
        self._db.commit()

    def record_approval_notification_failure(
        self, request_id: int, *, error: str, next_retry_at: float,
        failed: bool, fallback_path: list[dict],
    ) -> None:
        state = "failed" if failed else "retrying"
        self._db.execute(
            """UPDATE approval_requests
               SET notification_state=?, notification_attempts=notification_attempts+1,
                   next_retry_at=?, last_error=?, fallback_path=?, updated_at=?
               WHERE id=? AND gate_state='pending'""",
            (
                state, next_retry_at, error[:1000],
                json.dumps(fallback_path, separators=(",", ":")), time.time(), request_id,
            ),
        )
        self._db.commit()

    def record_approval_notification_delivered(
        self,
        request_id: int,
        *,
        destination: dict,
        fallback_path: list[dict],
        high_signal: bool = False,
    ) -> None:
        now = time.time()
        self._db.execute(
            """UPDATE approval_requests
               SET notification_state='delivered', notification_attempts=0,
                   notified_held_count=held_count, last_notified_at=?, next_retry_at=0,
                   last_error='', notification_destination=?, fallback_path=?, updated_at=?,
                   high_signal_alerted_at=CASE WHEN ? THEN ? ELSE high_signal_alerted_at END
               WHERE id=? AND gate_state='pending'""",
            (
                now, json.dumps(destination, separators=(",", ":")),
                json.dumps(fallback_path, separators=(",", ":")), now,
                int(high_signal), now, request_id,
            ),
        )
        self._db.commit()

    def settle_approval_request(self, agent_name: str, chat_id: str, state: str) -> None:
        if state not in ("approved", "denied"):
            raise ValueError(f"invalid approval gate state: {state}")
        self._db.execute(
            """UPDATE approval_requests
               SET gate_state=?, next_retry_at=0, updated_at=?
               WHERE agent_name=? AND chat_id=?""",
            (state, time.time(), agent_name, chat_id),
        )
        self._db.commit()

    def list_due_approval_notifications(self, now: float | None = None) -> list[dict]:
        """Return retry-due rows plus delivered rows eligible for maintenance.

        The broker applies the aging/new-hold/high-signal policy to delivered
        candidates. Including them here lets the daemon re-prompt without a new
        inbound message waking the approval path.
        """
        due_at = time.time() if now is None else now
        rows = self._db.execute(
            """SELECT id, agent_name, chat_id, target_name, is_channel,
                      gate_state, held_count, oldest_held_at, notification_state,
                      notification_attempts, notified_held_count, last_notified_at,
                      next_retry_at, last_error, notification_destination,
                      fallback_path, created_at, updated_at,
                      aging_reprompt_count, high_signal_alerted_at
               FROM approval_requests
               WHERE gate_state='pending'
                 AND ((notification_state='retrying' AND next_retry_at <= ?)
                      OR notification_state IN ('delivered', 'failed'))
               ORDER BY next_retry_at, id""",
            (due_at,),
        ).fetchall()
        return [
            self._enrich_approval_request(self._approval_request_dict(row))
            for row in rows
        ]

    def get_approval_notification_health(self, agent_name: str) -> dict:
        rows = self._db.execute(
            """SELECT id, chat_id, held_count, oldest_held_at, notification_state,
                      notification_attempts, next_retry_at, last_error
               FROM approval_requests
               WHERE agent_name=? AND gate_state='pending'
                 AND notification_state IN ('retrying', 'failed')
               ORDER BY created_at""",
            (agent_name,),
        ).fetchall()
        requests = [
            {
                "request_id": r[0], "chat_id": r[1], "held_count": r[2],
                "oldest_age_seconds": max(0, int(time.time() - r[3])) if r[3] else 0,
                "notification_state": r[4], "attempts": r[5],
                "next_retry_at": r[6], "last_error": r[7],
            }
            for r in rows
        ]
        return {
            "healthy": not requests,
            "retrying": sum(r[4] == "retrying" for r in rows),
            "failed": sum(r[4] == "failed" for r in rows),
            "requests": requests,
        }

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
               DO UPDATE SET platform=CASE WHEN excluded.platform='buzz'
                                            THEN excluded.platform
                                            ELSE group_chats.platform END,
                            chat_title=CASE WHEN excluded.platform='buzz'
                                                 AND excluded.chat_title=''
                                            THEN group_chats.chat_title
                                            ELSE excluded.chat_title END,
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

    # ── Verified Contacts ────────────────────────────────────

    @staticmethod
    def _verified_contact_dict(row) -> dict:
        return {
            "id": row[0],
            "agent_name": row[1],
            "platform": row[2],
            "principal": row[3],
            "name": row[4],
            "role": row[5],
            "added_at": row[6],
        }

    def get_verified_contact(
        self, agent_name: str, platform: str, principal: str
    ) -> dict | None:
        """Return one explicitly registered contact, never traffic-derived."""
        row = self._db.execute(
            """SELECT id, agent_name, platform, principal, name, role, added_at
               FROM verified_contacts
               WHERE agent_name=? AND platform=? AND principal=?""",
            (agent_name, platform, principal),
        ).fetchone()
        return self._verified_contact_dict(row) if row else None

    def upsert_verified_contact(
        self,
        agent_name: str,
        platform: str,
        principal: str,
        name: str,
        role: str = "",
    ) -> dict:
        """Create or replace an explicit principal-to-name trust decision."""
        agent = _validate_agent_name(agent_name)
        clean_platform = str(platform or "").strip().lower()
        clean_principal = str(principal or "").strip()
        clean_name = str(name or "").strip()
        clean_role = str(role or "").strip().lower()
        if not clean_platform or len(clean_platform) > 64:
            raise ValueError("verified contact platform must be 1-64 characters")
        if not clean_principal or len(clean_principal) > 512:
            raise ValueError("verified contact principal must be 1-512 characters")
        if not clean_name or len(clean_name) > 120 or any(
            ord(ch) < 32 or ord(ch) == 127 for ch in clean_name
        ):
            raise ValueError("verified contact name must be 1-120 printable characters")
        if clean_role not in {"", "owner", "agent"}:
            raise ValueError("verified contact role must be owner, agent, or empty")
        now = time.time()
        self._db.execute(
            """INSERT INTO verified_contacts
               (agent_name, platform, principal, name, role, added_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(agent_name, platform, principal) DO UPDATE SET
                 name=excluded.name, role=excluded.role, added_at=excluded.added_at""",
            (agent, clean_platform, clean_principal, clean_name, clean_role, now),
        )
        self._db.commit()
        result = self.get_verified_contact(agent, clean_platform, clean_principal)
        if result is None:  # pragma: no cover - defensive after successful write
            raise RuntimeError("verified contact write did not persist")
        return result

    def list_verified_contacts(self, agent_name: str) -> list[dict]:
        """List explicitly registered contacts for one agent."""
        rows = self._db.execute(
            """SELECT id, agent_name, platform, principal, name, role, added_at
               FROM verified_contacts WHERE agent_name=?
               ORDER BY platform, name COLLATE NOCASE, principal""",
            (agent_name,),
        ).fetchall()
        return [self._verified_contact_dict(row) for row in rows]

    def delete_verified_contact(
        self, agent_name: str, platform: str, principal: str
    ) -> bool:
        """Delete one explicit verified-contact trust decision."""
        cursor = self._db.execute(
            """DELETE FROM verified_contacts
               WHERE agent_name=? AND platform=? AND principal=?""",
            (agent_name, str(platform or "").strip().lower(), str(principal or "").strip()),
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

    # ── Owner notification destinations (#863) ─────────────

    @staticmethod
    def _normalize_owner_destination(destination: dict) -> dict:
        if not isinstance(destination, dict):
            raise ValueError("owner notification destination must be an object")
        normalized = {
            "platform": str(destination.get("platform") or "").strip().lower(),
            "account_id": str(
                destination.get("account_id")
                or destination.get("team_id")
                or destination.get("workspace_id")
                or ""
            ).strip(),
            "conversation_id": str(destination.get("conversation_id") or "").strip(),
            "principal_id": str(destination.get("principal_id") or "").strip(),
        }
        if not all(normalized.values()):
            raise ValueError(
                "owner notification destination requires platform, "
                "account_id/team_id/workspace_id, conversation_id, and principal_id"
            )
        return normalized

    def get_owner_notification_destinations(self) -> list[dict]:
        """Return the primary destination followed by ordered fallbacks."""
        raw = self.get_setting("owner_notification_destinations", "")
        if not raw:
            return []
        try:
            parsed = json.loads(raw)
            if not isinstance(parsed, list):
                raise ValueError("must be a list")
            return [self._normalize_owner_destination(item) for item in parsed]
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            _log(f"agent_registry: invalid owner_notification_destinations: {exc}")
            return []

    def set_owner_notification_destinations(self, destinations: list[dict]) -> list[dict]:
        """Store a canonical primary destination and ordered fallback list."""
        if not isinstance(destinations, list) or not destinations:
            raise ValueError("at least one owner notification destination is required")
        normalized = [self._normalize_owner_destination(item) for item in destinations]
        self.set_setting(
            "owner_notification_destinations",
            json.dumps(normalized, separators=(",", ":")),
        )
        return normalized

    def migrate_primary_user_notification_destination(
        self, *, platform: str, account_id: str,
    ) -> list[dict]:
        """Seed the destination tuple from the legacy primary-user chat id.

        ``platform`` and ``account_id`` are mandatory migration inputs. They
        are never guessed from inbound traffic or the shape of the legacy id.
        Existing canonical configuration is preserved unchanged.
        """
        existing = self.get_owner_notification_destinations()
        if existing:
            return existing
        primary_chat_id = self.get_setting("primary_user_chat_id", "").strip()
        if not primary_chat_id:
            raise ValueError("primary_user_chat_id is not configured")
        return self.set_owner_notification_destinations([
            {
                "platform": platform,
                "account_id": account_id,
                "conversation_id": primary_chat_id,
                "principal_id": primary_chat_id,
            }
        ])

    # ── Purchase Approvers (financial boundary, #249) ─────────
    #
    # Slack user ids permitted to approve a purchase (a money action). This is
    # the AUTHORITATIVE gate: the daemon validates the VERIFIED clicker id from a
    # Slack block_actions payload (set/signed by Slack, not LLM-provided) against
    # this list before any approval propagates. Fail-closed — an empty/unset
    # list denies all (no one can approve until an approver is configured).

    def get_purchase_approvers(self) -> list[str]:
        """Return the Slack user ids allowed to approve purchases (may be empty)."""
        raw = self.get_setting("purchase_approver_slack_ids", "")
        if not raw:
            return []
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return [str(x).strip() for x in parsed if str(x).strip()]
        except Exception:
            pass
        return [p.strip() for p in raw.split(",") if p.strip()]

    def set_purchase_approvers(self, slack_user_ids: list[str]) -> None:
        """Set the purchase-approver allowlist (Slack user ids)."""
        cleaned = [str(x).strip() for x in (slack_user_ids or []) if str(x).strip()]
        self.set_setting("purchase_approver_slack_ids", json.dumps(cleaned))

    def is_purchase_approver(self, slack_user_id: str) -> bool:
        """Whether a VERIFIED Slack user id may approve a purchase (fail-closed).

        Returns False for a blank id or when no allowlist is configured — a
        money action requires an explicitly configured, matching approver.
        """
        if not slack_user_id:
            return False
        return slack_user_id in self.get_purchase_approvers()

    def get_purchase_approval_secret(self) -> str:
        """Shared secret for minting daemon-signed approval tokens (may be empty).

        Must equal the pos-spec-purchasing MCP's POS_PURCHASING_APPROVAL_SECRET
        for tokens to verify. Empty => the daemon cannot mint, so approvals are
        refused (fail-closed). Set at deploy alongside the MCP env.
        """
        return self.get_setting("purchase_approval_secret", "")

    def set_purchase_approval_secret(self, secret: str) -> None:
        """Set the shared approval-token secret (must match the MCP env value)."""
        self.set_setting("purchase_approval_secret", (secret or "").strip())

    # ── Owner Profile ────────────────────────────────────────

    def get_owner_profile(self) -> dict:
        """Get the owner/operator profile from system settings.

        Returns a dict with keys: name, pronouns, timezone, role,
        comm_style, languages, code_word. Empty string for unset fields.
        Timezone falls back to get_default_timezone() if not explicitly set.
        """
        profile = {}
        for fname in OWNER_PROFILE_FIELDS:
            key = fname.removeprefix("owner_")
            profile[key] = self.get_setting(fname)
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
            "SELECT agent_name, platform, "
            "(token != '' OR COALESCE(token_ref, '') != '') as token_set, "
            "enabled, settings, updated_at "
            "FROM agent_tokens ORDER BY agent_name, platform",
        ).fetchall()
        return [
            {
                "agent_name": r[0], "platform": r[1], "token_set": bool(r[2]),
                "enabled": bool(r[3]), "settings": r[4], "updated_at": r[5],
            }
            for r in rows
        ]

    # ── Global Bot Tokens ─────────────────────────────────────

    def list_bot_tokens(self) -> list[dict]:
        """List all global bot tokens (token value redacted), with agent assignments."""
        rows = self._db.execute(
            "SELECT id, name, platform, token, created_at, updated_at"
            " FROM bot_tokens ORDER BY name"
        ).fetchall()
        # Build a map of token_id → list of agent names that reference it
        ref_rows = self._db.execute(
            "SELECT token_ref, agent_name FROM agent_tokens WHERE token_ref != '' AND token_ref IS NOT NULL"
        ).fetchall()
        ref_map: dict[str, list[str]] = {}
        for ref_id, agent_name in ref_rows:
            ref_map.setdefault(ref_id, []).append(agent_name)
        return [
            {
                "id": r[0], "name": r[1], "platform": r[2],
                "token_set": bool(r[3]), "created_at": r[4], "updated_at": r[5],
                "assigned_agents": sorted(ref_map.get(r[0], [])),
            }
            for r in rows
        ]

    def create_bot_token(self, name: str, platform: str, token: str) -> dict:
        """Create a new global bot token."""
        import uuid
        token_id = str(uuid.uuid4())[:8]
        now = time.time()
        self._db.execute(
            "INSERT INTO bot_tokens (id, name, platform, token, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (token_id, name, platform, token, now, now),
        )
        self._db.commit()
        return {"id": token_id, "name": name, "platform": platform, "token_set": bool(token)}

    def get_bot_token(self, token_id: str) -> dict | None:
        """Get a global bot token by ID (redacted)."""
        row = self._db.execute(
            "SELECT id, name, platform, token, created_at, updated_at"
            " FROM bot_tokens WHERE id=?",
            (token_id,),
        ).fetchone()
        if not row:
            return None
        return {
            "id": row[0], "name": row[1], "platform": row[2],
            "token_set": bool(row[3]), "created_at": row[4], "updated_at": row[5],
        }

    def get_raw_bot_token(self, token_id: str) -> str:
        """Get the actual bot token value (internal use only)."""
        row = self._db.execute(
            "SELECT token FROM bot_tokens WHERE id=?", (token_id,)
        ).fetchone()
        return row[0] if row else ""

    def update_bot_token(self, token_id: str, **kwargs) -> dict | None:
        """Update a global bot token."""
        updates = []
        params = []
        for col in ("name", "platform", "token"):
            if col in kwargs:
                updates.append(f"{col}=?")
                params.append(kwargs[col])
        if not updates:
            return self.get_bot_token(token_id)
        updates.append("updated_at=?")
        params.append(time.time())
        params.append(token_id)
        self._db.execute(
            f"UPDATE bot_tokens SET {', '.join(updates)} WHERE id=?", params
        )
        self._db.commit()
        return self.get_bot_token(token_id)

    def delete_bot_token(self, token_id: str) -> bool:
        """Delete a global bot token. Clears refs in agent_tokens."""
        self._db.execute(
            "UPDATE agent_tokens SET token_ref='' WHERE token_ref=?", (token_id,)
        )
        cursor = self._db.execute("DELETE FROM bot_tokens WHERE id=?", (token_id,))
        self._db.commit()
        return cursor.rowcount > 0

    # ── Model Registry ──────────────────────────────────────

    _MODEL_SEEDS = [
        # Anthropic
        ("anthropic", "claude-fable-5", "Claude Fable 5", "Anthropic's most capable widely-released model (2026-06-09). Demanding reasoning + long-horizon agentic work. 1M context; adaptive thinking always on (use effort to control depth).", "fable", 1_000_000, 1, 10.0, 50.0, 1.0, 1, 1),
        ("anthropic", "claude-mythos-5", "Claude Mythos 5", "Claude Fable 5 capabilities without the safety classifiers. Limited availability via Project Glasswing (approved customers only).", "fable", 1_000_000, 1, 10.0, 50.0, 1.0, 1, 2),
        ("anthropic", "claude-opus-5", "Claude Opus 5", "For complex agentic coding + enterprise work. 1M context; effort defaults high; adaptive thinking. Knowledge cutoff May 2026.", "opus", 1_000_000, 1, 5.0, 25.0, 0.5, 1, 2),
        ("anthropic", "claude-opus-4-8", "Claude Opus 4.8", "Newest Opus (2026-05-28). Sharper judgement, more honest progress reporting, longer independent runs. Effort defaults to high; adaptive thinking triggers only when needed.", "opus", 1_000_000, 1, 5.0, 25.0, 0.5, 1, 3),
        ("anthropic", "claude-opus-4-7", "Claude Opus 4.7", "Stricter instruction-following, xhigh effort, larger vision.", "opus", 1_000_000, 1, 5.0, 25.0, 0.5, 1, 5),
        ("anthropic", "claude-opus-4-6", "Claude Opus 4.6", "Maximum intelligence. Deep reasoning.", "opus", 1_000_000, 1, 5.0, 25.0, 0.5, 1, 10),
        ("anthropic", "claude-sonnet-5", "Claude Sonnet 5", "Current Sonnet (2026-06). Best speed+intelligence balance — daily driver. 1M context; adaptive thinking, effort defaults to high. Intro pricing $2/$10 through Aug 2026.", "sonnet", 1_000_000, 1, 3.0, 15.0, 0.3, 1, 15),
        ("anthropic", "claude-sonnet-4-6", "Claude Sonnet 4.6", "Fast + smart. Daily driver.", "sonnet", 1_000_000, 1, 3.0, 15.0, 0.3, 1, 20),
        ("anthropic", "claude-haiku-4-5", "Claude Haiku 4.5", "Lightning fast. Simple tasks.", "haiku", 200_000, 0, 1.0, 5.0, 0.1, 1, 30),
        ("anthropic", "claude-opus-4-5", "Claude Opus 4.5", "Previous-gen Opus.", "opus", 200_000, 0, 5.0, 25.0, 0.5, 1, 40),
        ("anthropic", "claude-sonnet-4-5", "Claude Sonnet 4.5", "Previous-gen Sonnet.", "sonnet", 200_000, 0, 3.0, 15.0, 0.3, 1, 50),
        # OpenAI / Codex CLI
        ("openai", "gpt-5.6-sol", "GPT-5.6 Sol", "Current codex fleet model (2026-07). Frontier coding + reasoning. 200k-class context; ChatGPT-sub proxy compacts at 150k below its observed ~167k backend limit. Codex sign-in auth only (API pending).", "flagship", 200_000, 0, 5.0, 30.0, 0.5, 0, 54),
        ("openai", "gpt-5.5", "GPT-5.5", "Previous frontier. Coding + reasoning. Codex sign-in auth only (API pending).", "flagship", 200_000, 0, 5.0, 30.0, 0.5, 0, 55),
        ("openai", "gpt-5.4", "GPT-5.4", "Flagship. Complex reasoning & coding.", "flagship", 200_000, 0, 1.75, 14.0, 0.175, 0, 60),
        ("openai", "gpt-5.4-mini", "GPT-5.4 Mini", "Fast + capable. Daily driver.", "mid", 200_000, 0, 0.25, 2.0, 0.025, 0, 70),
        ("openai", "gpt-5.4-nano", "GPT-5.4 Nano", "Cheapest. High-volume tasks.", "low", 200_000, 0, 0.05, 0.4, 0.005, 0, 80),
    ]

    # One-time data corrections for rows already seeded with wrong values.
    # ``_seed_models`` is INSERT OR IGNORE, so fixing ``_MODEL_SEEDS`` alone
    # never reaches an existing install. Each entry rewrites the prices of one
    # model id ONLY while the row still carries the exact stale numbers —
    # operator-customized prices are left untouched. (#741: Opus 4.5+ seeded
    # at the pre-4.5 $15/$75 tier, Haiku 4.5 at the 3.5 tier's $0.80/$4;
    # pricing.py — the actual cost engine — was always correct.)
    _PRICE_CORRECTIONS = [
        # (id, (stale in, out, cached), (correct in, out, cached))
        ("anthropic/claude-opus-4-8", (15.0, 75.0, 1.5), (5.0, 25.0, 0.5)),
        ("anthropic/claude-opus-4-7", (15.0, 75.0, 1.5), (5.0, 25.0, 0.5)),
        ("anthropic/claude-opus-4-6", (15.0, 75.0, 1.5), (5.0, 25.0, 0.5)),
        ("anthropic/claude-opus-4-5", (15.0, 75.0, 1.5), (5.0, 25.0, 0.5)),
        ("anthropic/claude-haiku-4-5", (0.8, 4.0, 0.08), (1.0, 5.0, 0.1)),
        # #860: gpt-5.5 was seeded at the gpt-5.2-tier $1.75/$14 while the
        # official rate is $5/$30 (cached $0.50) — analytics_store and
        # pricing.RATE_TABLE always had it right; this realigns the catalog.
        ("openai/gpt-5.5", (1.75, 14.0, 0.175), (5.0, 30.0, 0.5)),
    ]

    # One-time context-window / 1M-flag corrections for rows already seeded
    # with the wrong window. Same INSERT OR IGNORE gap as _PRICE_CORRECTIONS:
    # fixing _MODEL_SEEDS alone never migrates an existing install. Each entry
    # rewrites one model id ONLY while the row still carries the exact stale
    # ``(context_window, is_1m)`` pair — an operator-customized window is left
    # untouched.
    _CONTEXT_CORRECTIONS = [
        # (id, (stale_ctx, stale_is_1m), (correct_ctx, correct_is_1m))
        # #356 supersedes #873's inferred 1M designation: live ChatGPT-sub
        # backend evidence puts gpt-5.6-sol near 167k. Treat it as 200k-class so
        # the 400k-only restart logic never applies; tmux independently compacts
        # the subscription proxy at 150k. Correct only the exact stale 1M pair.
        ("openai/gpt-5.6-sol", (1_000_000, 1), (200_000, 0)),
    ]

    def _seed_models(self) -> None:
        """Ensure default models exist (idempotent).

        Per-row ``INSERT OR IGNORE`` adds any missing model and never
        overwrites an existing row, so new entries in ``_MODEL_SEEDS``
        propagate to existing installs on the next startup — not only to a
        fresh DB. (Previously this early-returned when the table was
        non-empty, so a newly-added model never reached running deployments.)
        """
        now = time.time()
        added = 0
        for (provider, model_id, display, desc, tier, ctx, is_1m,
             inp, out, cached, thinking, sort) in self._MODEL_SEEDS:
            mid = f"{provider}/{model_id}"
            cur = self._db.execute(
                """INSERT OR IGNORE INTO models
                   (id, provider, model_id, display_name, description, tier,
                    context_window, is_1m, input_price, output_price,
                    cached_input_price, supports_thinking, sort_order,
                    created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (mid, provider, model_id, display, desc, tier, ctx, is_1m,
                 inp, out, cached, thinking, sort, now, now),
            )
            added += cur.rowcount
        corrected = 0
        for mid, (old_in, old_out, old_cached), (new_in, new_out, new_cached) \
                in self._PRICE_CORRECTIONS:
            cur = self._db.execute(
                """UPDATE models
                   SET input_price=?, output_price=?, cached_input_price=?,
                       updated_at=?
                   WHERE id=? AND input_price=? AND output_price=?
                     AND cached_input_price=?""",
                (new_in, new_out, new_cached, now,
                 mid, old_in, old_out, old_cached),
            )
            corrected += cur.rowcount
        ctx_corrected = 0
        for mid, (old_ctx, old_1m), (new_ctx, new_1m) in self._CONTEXT_CORRECTIONS:
            cur = self._db.execute(
                """UPDATE models
                   SET context_window=?, is_1m=?, updated_at=?
                   WHERE id=? AND context_window=? AND is_1m=?""",
                (new_ctx, new_1m, now, mid, old_ctx, old_1m),
            )
            ctx_corrected += cur.rowcount
        self._db.commit()
        if added:
            _log(f"agent_registry: seeded {added} model(s)")
        if corrected:
            _log(f"agent_registry: corrected stale prices on {corrected} model(s)")
        if ctx_corrected:
            _log(
                "agent_registry: corrected stale context windows on "
                f"{ctx_corrected} model(s)"
            )

    def list_models(self, *, provider: str = "", active_only: bool = True) -> list[dict]:
        """List available models, optionally filtered by provider."""
        sql = "SELECT * FROM models"
        conditions = []
        params: list = []
        if active_only:
            conditions.append("active=1")
        if provider:
            conditions.append("provider=?")
            params.append(provider)
        if conditions:
            sql += " WHERE " + " AND ".join(conditions)
        sql += " ORDER BY sort_order, provider, model_id"
        cursor = self._db.execute(sql, params)
        rows = cursor.fetchall()
        if not rows:
            return []
        cols = [d[0] for d in cursor.description]
        return [dict(zip(cols, row)) for row in rows]

    def get_model(self, model_id: str) -> dict | None:
        """Get a model by its full ID (provider/model_id) or just model_id."""
        row = self._db.execute(
            "SELECT * FROM models WHERE id=? OR model_id=?",
            (model_id, model_id),
        ).fetchone()
        if not row:
            return None
        cols = [r[1] for r in self._db.execute("PRAGMA table_info(models)").fetchall()]
        return dict(zip(cols, row))

    def add_model(
        self,
        *,
        provider: str,
        model_id: str,
        display_name: str = "",
        description: str = "",
        tier: str = "",
        context_window: int = 200_000,
        is_1m: bool = False,
        input_price: float = 0,
        output_price: float = 0,
        cached_input_price: float = 0,
        supports_thinking: bool = True,
        sort_order: int = 100,
    ) -> dict:
        """Add or update a model in the registry."""
        full_id = f"{provider}/{model_id}"
        now = time.time()
        self._db.execute(
            """INSERT INTO models
               (id, provider, model_id, display_name, description, tier,
                context_window, is_1m, input_price, output_price,
                cached_input_price, supports_thinking, sort_order,
                created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                display_name=excluded.display_name,
                description=excluded.description,
                tier=excluded.tier,
                context_window=excluded.context_window,
                is_1m=excluded.is_1m,
                input_price=excluded.input_price,
                output_price=excluded.output_price,
                cached_input_price=excluded.cached_input_price,
                supports_thinking=excluded.supports_thinking,
                sort_order=excluded.sort_order,
                active=1,
                updated_at=excluded.updated_at""",
            (full_id, provider, model_id,
             display_name or model_id, description, tier,
             context_window, int(is_1m), input_price, output_price,
             cached_input_price, int(supports_thinking), sort_order,
             now, now),
        )
        self._db.commit()
        _log(f"agent_registry: added/updated model {full_id}")
        return self.get_model(full_id) or {}

    def delete_model(self, model_id: str) -> bool:
        """Soft-delete a model (set active=0)."""
        cursor = self._db.execute(
            "UPDATE models SET active=0, updated_at=? WHERE id=? OR model_id=?",
            (time.time(), model_id, model_id),
        )
        self._db.commit()
        return cursor.rowcount > 0

    def get_1m_models(self) -> set[str]:
        """Return set of model_ids that have 1M context windows."""
        rows = self._db.execute(
            "SELECT model_id FROM models WHERE is_1m=1 AND active=1"
        ).fetchall()
        return {r[0] for r in rows}

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
            librarian_enabled=bool(row[35]) if len(row) > 35 else False,
            librarian_schedule=row[36] if len(row) > 36 and row[36] else "0 4 * * *",
            working_status=row[37] if len(row) > 37 and row[37] else "idle",
            working_status_updated_at=row[38] if len(row) > 38 else 0.0,
            runtime=row[39] if len(row) > 39 and row[39] else "claude_sdk",
            transport=row[40] if len(row) > 40 and row[40] else "sdk",
            provider_url=row[41] if len(row) > 41 and row[41] else "",
            provider_key=row[42] if len(row) > 42 and row[42] else "",
            provider_model=row[43] if len(row) > 43 and row[43] else "",
            provider_ref=row[44] if len(row) > 44 and row[44] else "",
            disallowed_tools=json.loads(row[45]) if len(row) > 45 and row[45] else [],
            thinking_effort=row[46] if len(row) > 46 and row[46] else "medium",
            watchdog_config=json.loads(row[47]) if len(row) > 47 and row[47] else {},
            last_seen_at=row[48] if len(row) > 48 else 0.0,
            strict_effort_enforcement=bool(row[49]) if len(row) > 49 else False,
            context_nudge_threshold_pct=row[50] if len(row) > 50 else 0.0,
            isolated=bool(row[51]) if len(row) > 51 else False,
            isolation_mode=row[52] if len(row) > 52 and row[52] else "local",
            container_image=row[53] if len(row) > 53 and row[53] else "",
            dedicated_config_dir=bool(row[54]) if len(row) > 54 else False,
            codex_home=row[55] if len(row) > 55 and row[55] else "",
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

    # ── Ferry peer-fleet ACL ───────────────────────────────
    #
    # Separate identity primitive from approved_users (humans on Telegram /
    # Discord / etc.). Ferry inbound is *agents* addressing an agent —
    # different identity primitive, separate list. Default-deny.
    #
    # Stored as JSON array of AgentCardSelector dicts on the `agents` row's
    # `peer_fleet_acl` column. See `pinky_daemon.ferry.types.AgentCardSelector`
    # for the selector shape.

    def has_agent(self, agent_name: str) -> bool:
        """Return True if an agent with this name is registered (any status)."""
        row = self._db.execute(
            "SELECT 1 FROM agents WHERE name = ? LIMIT 1", (agent_name,)
        ).fetchone()
        return row is not None

    def get_peer_fleet_acl(self, agent_name: str) -> list[dict]:
        """Return the list of peer-fleet ACL selector dicts for an agent.

        Each dict has shape `{fleet, agent_id, pinky_type}` (any combination,
        with at least one non-null field per AgentCardSelector contract).
        Returns [] for unknown agents or empty/missing ACL.
        """
        row = self._db.execute(
            "SELECT peer_fleet_acl FROM agents WHERE name = ?", (agent_name,)
        ).fetchone()
        if not row or not row[0]:
            return []
        try:
            data = json.loads(row[0])
        except (TypeError, ValueError):
            return []
        if not isinstance(data, list):
            return []
        return [d for d in data if isinstance(d, dict)]

    def set_peer_fleet_acl(
        self,
        agent_name: str,
        selectors: list[dict],
    ) -> None:
        """Replace the peer-fleet ACL for an agent (full replacement, not merge).

        Each selector must have at least one of fleet/agent_id/pinky_type
        non-empty. Selectors that don't validate are silently dropped with
        a log line. Empty list = deny all peer-fleet inbound.
        """
        clean: list[dict] = []
        for raw in selectors or []:
            if not isinstance(raw, dict):
                _log(f"peer_fleet_acl: skipping non-dict selector for {agent_name}: {raw!r}")
                continue
            fleet = (raw.get("fleet") or "").strip() or None
            agent_id = (raw.get("agent_id") or "").strip() or None
            pinky_type = (raw.get("pinky_type") or "").strip() or None
            if not (fleet or agent_id or pinky_type):
                _log(f"peer_fleet_acl: skipping empty selector for {agent_name}")
                continue
            clean.append({
                "fleet": fleet,
                "agent_id": agent_id,
                "pinky_type": pinky_type,
            })
        self._db.execute(
            "UPDATE agents SET peer_fleet_acl = ? WHERE name = ?",
            (json.dumps(clean), agent_name),
        )
        self._db.commit()

    def add_peer_fleet_acl(
        self,
        agent_name: str,
        *,
        fleet: str | None = None,
        agent_id: str | None = None,
        pinky_type: str | None = None,
    ) -> bool:
        """Append one selector to an agent's peer_fleet_acl.

        Returns True if added, False if the selector was empty (dropped).
        Idempotent: a selector matching an existing entry is skipped.

        Thread-safe: read-modify-write is guarded by ``_rmw_lock`` so
        concurrent admin-API requests can't lose updates.
        """
        entry = {
            "fleet": (fleet or "").strip() or None,
            "agent_id": (agent_id or "").strip() or None,
            "pinky_type": (pinky_type or "").strip() or None,
        }
        if not (entry["fleet"] or entry["agent_id"] or entry["pinky_type"]):
            return False
        with self._rmw_lock:
            existing = self.get_peer_fleet_acl(agent_name)
            if entry in existing:
                return True
            existing.append(entry)
            self.set_peer_fleet_acl(agent_name, existing)
            return True

    def remove_peer_fleet_acl(
        self,
        agent_name: str,
        *,
        fleet: str | None = None,
        agent_id: str | None = None,
        pinky_type: str | None = None,
    ) -> int:
        """Remove all selectors matching the given criteria. Returns count removed.

        Matching is **exact** on every field. A stored selector matches
        only when all three of ``fleet`` / ``agent_id`` / ``pinky_type``
        equal the corresponding argument (``None`` and empty string are
        normalized to the same value, so omitting an argument is the
        same as passing ``""``).

        Wildcard caveat: a stored selector with ``agent_id="*"`` is
        removed only by passing ``agent_id="*"`` — calling
        ``remove_peer_fleet_acl(agent_name, fleet="sigil")`` will **not**
        remove a stored ``{fleet:"sigil", agent_id:"*"}`` and silently
        returns 0. Pass the exact stored selector you want to delete.

        Thread-safe: read-modify-write is guarded by ``_rmw_lock`` so
        concurrent admin-API requests can't lose updates.
        """
        target = {
            "fleet": (fleet or "").strip() or None,
            "agent_id": (agent_id or "").strip() or None,
            "pinky_type": (pinky_type or "").strip() or None,
        }
        with self._rmw_lock:
            existing = self.get_peer_fleet_acl(agent_name)
            kept = [s for s in existing if s != target]
            removed = len(existing) - len(kept)
            if removed:
                self.set_peer_fleet_acl(agent_name, kept)
            return removed

    # ── Ferry outbound mesh allowlist ──────────────────────
    #
    # Per-agent allowlist gating which (fleet, agent) targets this agent
    # may publish to via the mesh_remote_send tool. Stored as JSON list
    # of "agent_slug@fleet" patterns on the `agents` row's
    # `mesh_outbound_allowlist` column. Default-deny (empty list = no
    # outbound). Patterns support wildcards per
    # `pinky_daemon.ferry.outbound.allowlist_matches`.

    def get_mesh_outbound_allowlist(self, agent_name: str) -> list[str]:
        """Return the agent's mesh outbound allowlist patterns.

        Returns [] for unknown agents or empty/missing allowlist.
        """
        row = self._db.execute(
            "SELECT mesh_outbound_allowlist FROM agents WHERE name = ?",
            (agent_name,),
        ).fetchone()
        if not row or not row[0]:
            return []
        try:
            data = json.loads(row[0])
        except (TypeError, ValueError):
            return []
        if not isinstance(data, list):
            return []
        return [s for s in data if isinstance(s, str) and s.strip()]

    def set_mesh_outbound_allowlist(
        self,
        agent_name: str,
        patterns: list[str],
    ) -> None:
        """Replace the mesh outbound allowlist for an agent (full replace).

        Empty/whitespace patterns are silently dropped. Empty list = deny
        all outbound.
        """
        clean = [p.strip() for p in (patterns or []) if isinstance(p, str) and p.strip()]
        self._db.execute(
            "UPDATE agents SET mesh_outbound_allowlist = ? WHERE name = ?",
            (json.dumps(clean), agent_name),
        )
        self._db.commit()

    def add_mesh_outbound_allowlist(self, agent_name: str, pattern: str) -> bool:
        """Append one pattern to an agent's mesh outbound allowlist.

        Returns True if added (or already present), False if pattern is empty.
        Idempotent. Thread-safe via ``_rmw_lock``.
        """
        pat = (pattern or "").strip()
        if not pat:
            return False
        with self._rmw_lock:
            existing = self.get_mesh_outbound_allowlist(agent_name)
            if pat in existing:
                return True
            existing.append(pat)
            self.set_mesh_outbound_allowlist(agent_name, existing)
            return True

    def remove_mesh_outbound_allowlist(self, agent_name: str, pattern: str) -> int:
        """Remove all matching patterns. Returns count removed.

        Exact string match only; pass the stored pattern verbatim.
        """
        pat = (pattern or "").strip()
        if not pat:
            return 0
        with self._rmw_lock:
            existing = self.get_mesh_outbound_allowlist(agent_name)
            kept = [s for s in existing if s != pat]
            removed = len(existing) - len(kept)
            if removed:
                self.set_mesh_outbound_allowlist(agent_name, kept)
            return removed

    def close(self) -> None:
        self._db.close()
