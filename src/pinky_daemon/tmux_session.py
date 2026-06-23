"""Tmux Session — interactive ``claude`` REPL inside a tmux session.

PR8 of the #486 sequence. New transport backend for the Dymok test agent
and (eventually) Misha. Bills against the Claude Code subscription's
interactive limits instead of the capped SDK credit pool that
``StreamingSession`` consumes.

## Architecture

Each TmuxSession owns a single detached tmux session named after the
agent (``pinky-<agent_name>``). Inside that tmux session, an
interactive ``claude --continue --dangerously-skip-permissions`` REPL
runs. Inbound messages are delivered via ``tmux send-keys``; outbound
responses are captured by transcript-file tailing and delivered through
the shared ``response_callback`` contract.

## State machine integration

TmuxSession adopts the full StateMachine matrix from PR1/#487 — same
choreography as ``StreamingSession`` after PR3-PR6:

- Cold-start: ``UNINITIALIZED → BOOTING`` via ``Trigger.BOOT``; on success
  ``BOOTING → CONNECTED`` via ``BOOT_COMPLETE``; on failure
  ``BOOTING → DEAD`` via ``BOOT_FAILED``.
- Cold-start guard widened to ``state in {UNINITIALIZED, BOOTING}`` to
  defend the concurrent-connect race fixed in PR6 (Murzik's catch on
  PR #494).
- Warm-reconnect: ``CONNECTED → RECONNECTING → CONNECTED|DEAD`` via the
  standard triggers (USER_AGENT for ``force_restart``, WATCHDOG for
  watchdog-driven restarts, INTERNAL for the completion edge).
- Idle-sleep: ``CONNECTED → IDLE_SLEEPING`` via USER_AGENT.

CodexSession's coarse 3-state derivation is intentionally NOT mirrored
here. Greenfield backend, full matrix from day one — exactly the design
Brad green-lit in the side-by-side framing.

## Resume handle

TmuxSession's resume handle is the **tmux session name** itself.
``claude --continue`` resolves by ``cwd``'s most-recent transcript, and
the tmux session pins ``cwd``, so the session name uniquely identifies a
resumable conversation. Survives daemon restart as long as the tmux
session stays alive.

## Out of scope for PR8

- Context-budget watchdog (``_check_context``). StreamingSession's
  context warn/restart logic is SDK-specific (uses ``get_context_usage``);
  the equivalent for tmux requires reading the transcript file's token
  totals. Deferred until response pipeline lands.
- ``cost_usd`` reporting. Documented as a known gap on the Transport
  protocol (``stats`` shape varies per backend; tmux can't report cost
  the way SDK does because billing is against the subscription, not the
  metered credit pool).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shlex
import threading
import time
from collections import deque
from dataclasses import dataclass, field, replace
from pathlib import Path

from pinky_daemon.auth_relay import coordinator as _auth_relay
from pinky_daemon.auth_relay import extract_oauth_url, looks_like_login_wall
from pinky_daemon.command_runner import (
    CommandRunner,
    ContainerCommandRunner,
    LocalCommandRunner,
)
from pinky_daemon.effort import EFFORT_LEVELS, is_ultracode, resolve_cli_effort
from pinky_daemon.pricing import compute_cost_from_usage
from pinky_daemon.sessions import SessionUsage
from pinky_daemon.streaming_session import (
    StreamingSessionConfig,
    _is_outreach_tool,
    _log,
)
from pinky_daemon.tmux_transcript import (
    TmuxTranscriptTailer,
    TurnResponse,
)
from pinky_daemon.transport_state import (
    SessionState,
    StateMachine,
    Trigger,
)
from pinky_daemon.wake_prompt import (
    WakePromptInput,
    WakeReason,
    build_context_nudge_prompt,
    build_idle_sleep_prompt,
    build_wake_prompt,
)

# Soft context-watermark default (#614) — used when an agent's
# ``context_nudge_threshold_pct`` is unset (0). Sits well below the
# hard ``restart_threshold_pct`` (default 80) so the agent gets an
# early, graceful heads-up to checkpoint before the safety net trips.
DEFAULT_CONTEXT_NUDGE_THRESHOLD_PCT = 35.0

# ──────────────────────────────────────────────────────────────────────────
# Tmux subprocess control
# ──────────────────────────────────────────────────────────────────────────


# Context-lock check (pulse-v2 port, queue-drain.ts:252-263). When the
# daemon-level context manager is mid-rewrite of an agent's CLAUDE.md /
# transcript files, it touches ``data/transport-locks/<agent>.lock`` to
# tell the worker to skip pasting for now. Directory is the repo-root
# ``data/transport-locks/`` — consistent with other runtime-state dirs
# under ``data/`` (``data/agents/``, ``data/transfers/``, ``data/kb/``)
# and not per-agent because the lock signals daemon-wide intent, not
# agent-internal state.
_TRANSPORT_LOCK_DIR = Path("data/transport-locks")


# ──────────────────────────────────────────────────────────────────────────
# Claude Code first-run trust pre-seed (#112)
# ──────────────────────────────────────────────────────────────────────────
#
# A fresh ``claude`` REPL on a box that has never run Claude Code in this
# agent-home wedges at three interactive first-run gates:
#   1. the login / onboarding wizard ("Welcome to Claude Code")
#   2. "Do you trust the files in this folder?"
#   3. "Bypass Permissions mode" acceptance
# ``claude --dangerously-skip-permissions`` does NOT auto-accept either —
# the pane parks at the prompt, no transcript is ever written, and the
# session sits CONNECTED-but-mute with ``pending_responses=true`` forever
# (the symptom that wedged Angel on a fresh box). Claude Code persists the
# "already accepted" state in its global ``.claude.json``; pre-seeding the
# relevant flags before launch makes every new tmux agent boot clean on
# any box without an operator manually clearing the prompts.

# Serializes read-modify-write of the shared ``.claude.json`` across the
# daemon's concurrent agent launches so two simultaneous seeds can't drop
# each other's ``projects[...]`` entry (last-write-wins clobber).
#
# NOTE (cross-process race, accepted): this lock only serializes seeds
# WITHIN the daemon process. On a box where many agents' ``claude``
# processes share one ``.claude.json``, an already-running claude could
# write its own per-session keys (``numStartups``, ``lastCost``, ...)
# between our read and our ``os.replace`` — silently dropping that write.
# Window is tiny and severity low (those keys are non-load-bearing
# telemetry), so we accept it for now. A file lock (``fcntl.flock``)
# around the read-modify-write is the proper fix if this ever matters.
_CLAUDE_JSON_SEED_LOCK = threading.Lock()

_CLAUDE_AUTH_MODE_ENV = "PINKY_CLAUDE_AUTH_MODE"
_CLAUDE_AUTH_MODE_SHARED_REFRESH = "shared_refresh_file"
_CLAUDE_AUTH_MODE_PER_AGENT_OAUTH = "per_agent_oauth"
_CLAUDE_AUTH_MODES = {
    _CLAUDE_AUTH_MODE_SHARED_REFRESH,
    _CLAUDE_AUTH_MODE_PER_AGENT_OAUTH,
}


def _claude_auth_mode_env_for_agent(agent_name: str | None) -> str | None:
    if not agent_name:
        return None
    suffix = re.sub(r"[^A-Za-z0-9]", "_", agent_name).upper()
    return f"{_CLAUDE_AUTH_MODE_ENV}_{suffix}"


def _resolve_claude_auth_mode(raw: str, *, source: str) -> str | None:
    mode = raw.strip().lower() or _CLAUDE_AUTH_MODE_SHARED_REFRESH
    if mode in _CLAUDE_AUTH_MODES:
        return mode
    _log(
        f"tmux: unsupported {source}={raw!r}; "
        f"falling back to {_CLAUDE_AUTH_MODE_ENV}"
    )
    return None


def _claude_auth_mode(agent_name: str | None = None) -> str:
    """Auth mode for Claude Code tmux sessions.

    The default preserves the historical bootstrap path: copy the daemon user's
    Claude subscription OAuth credentials into container agents. A per-agent
    ``PINKY_CLAUDE_AUTH_MODE_<AGENT>`` override wins over the fleet-wide
    ``PINKY_CLAUDE_AUTH_MODE`` so one container can be canaried without putting
    the whole daemon into ``per_agent_oauth``. ``per_agent_oauth`` is the durable
    interactive-container mode: each agent owns its own Claude login in its
    container home volume, and the daemon must never import shared host
    credentials on normal restart/update.
    """
    agent_env = _claude_auth_mode_env_for_agent(agent_name)
    if agent_env:
        raw_agent = os.environ.get(agent_env)
        if raw_agent is not None:
            mode = _resolve_claude_auth_mode(raw_agent, source=agent_env)
            if mode is not None:
                return mode
    raw = os.environ.get(_CLAUDE_AUTH_MODE_ENV, _CLAUDE_AUTH_MODE_SHARED_REFRESH)
    mode = _resolve_claude_auth_mode(raw, source=_CLAUDE_AUTH_MODE_ENV)
    if mode is not None:
        return mode
    return _CLAUDE_AUTH_MODE_SHARED_REFRESH


def _credential_fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]


def _claude_creds_state(path: Path) -> str:
    """Return non-secret telemetry for a Claude Code credentials file."""
    if not path.exists():
        return "home_creds_present=false"
    try:
        data = json.loads(path.read_text())
    except Exception as e:
        return (
            "home_creds_present=true home_creds_parse_error="
            f"{type(e).__name__}"
        )
    if not isinstance(data, dict):
        return "home_creds_present=true home_creds_parse_error=not_object"
    oauth = data.get("claudeAiOauth")
    if not isinstance(oauth, dict):
        return "home_creds_present=true home_creds_has_refresh=false"
    refresh = oauth.get("refreshToken")
    access = oauth.get("accessToken")
    refresh_s = refresh if isinstance(refresh, str) else ""
    access_s = access if isinstance(access, str) else ""
    fp_token = refresh_s or access_s
    parts = [
        "home_creds_present=true",
        f"home_creds_has_refresh={str(bool(refresh_s)).lower()}",
    ]
    expires_at = oauth.get("expiresAt")
    if isinstance(expires_at, int):
        parts.append(f"home_creds_expires_at={expires_at}")
    if fp_token:
        parts.append(f"creds_fingerprint={_credential_fingerprint(fp_token)}")
    return " ".join(parts)


_CONTAINER_CREDS_STATE_PY = (
    "import hashlib,json,os,pathlib\n"
    "p=pathlib.Path(os.environ.get('HOME') or '/')/'.claude'/'.credentials.json'\n"
    "def fp(s): return hashlib.sha256(s.encode('utf-8')).hexdigest()[:12]\n"
    "if not p.exists():\n"
    "    print('home_creds_present=false')\n"
    "    raise SystemExit(0)\n"
    "try:\n"
    "    d=json.loads(p.read_text())\n"
    "except Exception as e:\n"
    "    print('home_creds_present=true home_creds_parse_error='+type(e).__name__)\n"
    "    raise SystemExit(0)\n"
    "if not isinstance(d,dict):\n"
    "    print('home_creds_present=true home_creds_parse_error=not_object')\n"
    "    raise SystemExit(0)\n"
    "o=d.get('claudeAiOauth')\n"
    "if not isinstance(o,dict):\n"
    "    print('home_creds_present=true home_creds_has_refresh=false')\n"
    "    raise SystemExit(0)\n"
    "r=o.get('refreshToken') if isinstance(o.get('refreshToken'),str) else ''\n"
    "a=o.get('accessToken') if isinstance(o.get('accessToken'),str) else ''\n"
    "parts=['home_creds_present=true','home_creds_has_refresh='+str(bool(r)).lower()]\n"
    "if isinstance(o.get('expiresAt'),int): parts.append('home_creds_expires_at='+str(o['expiresAt']))\n"
    "tok=r or a\n"
    "if tok: parts.append('creds_fingerprint='+fp(tok))\n"
    "print(' '.join(parts))\n"
)


def _resolve_claude_config_path(env: dict[str, str] | None = None) -> Path:
    """Resolve the path to Claude Code's global ``.claude.json``.

    Mirrors the CLI's resolution: ``$CLAUDE_CONFIG_DIR/.claude.json`` when
    ``CLAUDE_CONFIG_DIR`` is set, else ``$HOME/.claude.json``. ``env``
    defaults to the daemon process environment, which the tmux REPL
    inherits (``_build_repl_env`` only adds ``-e`` overrides on top, so
    the effective HOME/CLAUDE_CONFIG_DIR the launched ``claude`` sees is
    the daemon's unless explicitly overridden). Injectable for tests.
    """
    e = env if env is not None else os.environ
    cfg_dir = (e.get("CLAUDE_CONFIG_DIR") or "").strip()
    base = Path(cfg_dir) if cfg_dir else Path(e.get("HOME") or Path.home())
    return base / ".claude.json"


# Sentinel distinguishing "caller passed no agent" from "caller passed None
# (= local)" in the container-aware helpers below.
_UNSET = object()


def _container_start_timeout_sec() -> float:
    """Budget for provision+start of a container at spawn (#638). Separate from
    (and much larger than) the 60s cold-start umbrella because it can include a
    legitimate multi-minute ``podman pull`` on slow links. Env-overridable."""
    raw = os.environ.get("PINKY_CONTAINER_START_TIMEOUT_SEC", "").strip()
    try:
        val = float(raw) if raw else 600.0
    except (TypeError, ValueError):
        val = 600.0
    return max(val, 1.0)


def _is_dead_runtime_stderr(stderr: str) -> bool:
    """True when a tmux command's stderr says the execution substrate is gone —
    either the tmux pane itself, or (for container agents, #638) the container
    that ``podman exec`` needs. Both mean the same thing for the session state
    machine: no future paste can succeed, so the worker must schedule disconnect
    instead of silently eating every subsequent message against a zombie."""
    low = (stderr or "").lower()
    if any(
        needle in low
        for needle in (
            "can't find pane",
            # podman exec into a stopped container
            "can only create exec sessions on running containers",
            # podman/docker: container was removed entirely
            "no such container",
        )
    ):
        return True
    # docker exec into a stopped container: "Error response from daemon:
    # container <id> is not running" — the id sits between the words, so a
    # contiguous-substring needle can never match. Require both fragments.
    return "container" in low and "is not running" in low


def _seed_claude_trust_file(config_path: Path, project_dir: str) -> bool:
    """Idempotently pre-seed first-run trust/bypass flags in
    ``config_path`` (Claude Code's ``.claude.json``) for ``project_dir``.

    Sets top-level ``bypassPermissionsModeAccepted`` +
    ``hasCompletedOnboarding`` and, under ``projects[<resolved
    project_dir>]``, ``hasTrustDialogAccepted`` +
    ``hasCompletedProjectOnboarding`` — all to ``True``. Preserves every
    other key (the file also holds oauth creds + per-project history).

    Returns ``True`` if the file was modified, ``False`` if every flag was
    already set (no write). Raises on a corrupt/non-object file rather than
    clobbering it — callers treat seeding as best-effort and swallow.

    Atomic: writes a sibling temp file and ``os.replace``s it in, so a
    concurrent reader never sees a half-written config. Serialized
    process-wide via ``_CLAUDE_JSON_SEED_LOCK``.
    """
    proj_key = str(Path(project_dir).resolve())
    with _CLAUDE_JSON_SEED_LOCK:
        data: dict = {}
        if config_path.exists():
            with config_path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError(
                    f"{config_path} root is not a JSON object "
                    f"(got {type(data).__name__}) — refusing to overwrite"
                )

        changed = False
        # Top-level first-run gates, re-asserted on every launch.
        # ``hasCompletedOnboarding`` skips the initial login/onboarding wizard
        # ("Welcome to Claude Code"); ``bypassPermissionsModeAccepted`` skips
        # the "Bypass Permissions mode" consent. Both persist globally in
        # ``.claude.json`` — but when a shared-home fleet corrupts that file and
        # the CLI recreates it BLANK, both vanish and every agent re-wedges at
        # the wizard. Seeding them here makes the corruption self-heal: the next
        # launch repairs the config instead of parking at an interactive prompt.
        for flag in ("bypassPermissionsModeAccepted", "hasCompletedOnboarding"):
            if data.get(flag) is not True:
                data[flag] = True
                changed = True

        projects = data.setdefault("projects", {})
        if not isinstance(projects, dict):
            raise ValueError(f"{config_path} 'projects' is not an object")
        proj = projects.setdefault(proj_key, {})
        if not isinstance(proj, dict):
            raise ValueError(f"{config_path} projects[{proj_key!r}] is not an object")
        for flag in ("hasTrustDialogAccepted", "hasCompletedProjectOnboarding"):
            if proj.get(flag) is not True:
                proj[flag] = True
                changed = True

        if changed:
            config_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = config_path.parent / f".claude.json.pinky-seed.{os.getpid()}.tmp"
            with tmp.open("w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp, config_path)
        return changed

# Transient-failure retry cadence for the worker loop. Fixed (not
# exponential) — mirrors pulse-v2's poll cadence and keeps the
# semantics simple: "park, sleep, retry the same turn". The worker
# does not move on to the next queue item until the inflight turn
# either succeeds or hits a permanent failure.
_TRANSIENT_RETRY_BACKOFF_SEC = 2.0

# Bounded retry budget for per-turn delivery attempts that died on the
# tmux command timeout (``_TmuxControl._run``'s 5s subprocess ceiling).
# A momentarily busy tmux server / loaded host is transient; treating it
# as permanent silently dropped the user's message. Kept small because a
# retry after a timeout that landed AFTER the paste could double-paste
# the prompt into the input area.
_DELIVERY_TIMEOUT_RETRY_LIMIT = 3

# Capture-pane double-submit guard (see ``_timed_out_turn_landed``).
# ``_PANE_MARKER_CHARS`` is how much of the prompt's first line we look
# for in the pane -- short enough to survive an 80-col pane without
# wrapping, long enough to be distinctive. Markers shorter than
# ``_PANE_MARKER_MIN_CHARS`` are too ambiguous to trust (a false match
# would silently drop the message), so the guard declines and the worker
# falls back to a plain retry.
_PANE_MARKER_CHARS = 40
_PANE_MARKER_MIN_CHARS = 12

# Sentinel path used by ``_start_tailer`` when the transcript JSONL
# doesn't exist yet (cold-start). The tailer's ``read_once`` treats
# the non-existent file as "no data" and waits; once the SessionStart
# hook reports the real path, ``set_transcript_path`` swaps to it.
#
# Defined as a module-level constant (issue #563) so the placeholder→real
# transition can be detected reliably in ``TmuxSession.set_transcript_path``:
# the seek-to-byte-0 behavior only applies on that first transition, not
# on subsequent real→real swaps (compact-resume protected by #496).
_PLACEHOLDER_TRANSCRIPT_PATH = Path("/dev/null/no-transcript-yet")

# Issue #565 — delayed first-bind recovery delay. After ``_start_tailer``
# schedules a recovery task; if no explicit ``set_transcript_path`` bind
# has consumed ``_tailer_first_bind_pending`` by this deadline AND the
# launch is fresh, we re-run ``_discover_transcript_path()`` and rebind
# even if the currently watched path exists. Covers the bind-never-arrives
# case for fresh-launch-with-prior-history (the existing #515 self-heal
# only fires when the current watched path is missing; a stale real path
# blocks it forever). 5 seconds is generous slack vs. typical
# SessionStart hook latency (sub-second to ~200ms).
_FIRST_BIND_RECOVERY_DELAY_SEC = 5.0


class _ContextLockDeferral(Exception):  # noqa: N818
    """Transient: context-lock file present at paste time.

    Murzik #522 round-1: ``_deliver_turn`` previously raised a bare
    ``RuntimeError`` here, which the worker's catch-all dropped (turn
    was consumed from the queue with ``get()`` BEFORE ``_deliver_turn``
    ran, so an exception lost the message). The fix: raise a typed
    exception that the worker recognises as "transient, keep the
    inflight turn, sleep + retry without re-fetching from the queue".
    """


@dataclass
class TmuxCommandResult:
    """Outcome of one ``tmux ...`` invocation."""

    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


class _TmuxControl:
    """Thin async wrapper over the ``tmux`` CLI.

    All subprocess calls live here so they can be mocked in tests without
    touching the host's tmux. One instance per TmuxSession.

    Why a separate class instead of free functions: the session name +
    socket path are configuration state, not arguments callers should
    keep repeating. Encapsulating them here also gives tests a single
    monkeypatch target (``ts._tmux = MockTmuxControl()``).
    """

    def __init__(
        self,
        session_name: str,
        *,
        tmux_binary: str = "tmux",
        socket_name: str = "",
        command_runner: CommandRunner | None = None,
    ) -> None:
        self.session_name = session_name
        self.tmux_binary = tmux_binary
        # An explicit socket isolates Pinky's tmux sessions from the
        # operator's own. Empty = use tmux's default socket.
        self.socket_name = socket_name
        # #149 phase-3 execution seam: who runs the tmux subprocess. Default
        # LocalCommandRunner reproduces the prior inline create_subprocess_exec
        # verbatim (daemon's own user). An isolation_mode='unix_user' tenant is
        # wired with a RunuserCommandRunner so its tmux server + REPL run under
        # the agent's own pinky-<agent> uid. See command_runner.py.
        self._runner: CommandRunner = command_runner or LocalCommandRunner()

    def set_command_runner(self, runner: CommandRunner) -> None:
        """Swap the execution seam. #638: the runner must be RE-SELECTED at
        every spawn (TmuxSession._spawn_tmux_repl), not fixed at construction —
        session objects survive isolation_mode changes (PUT /agents flips the
        registry row with no session teardown, and reconnect/restart reuse the
        SAME object), and a stale runner is a silent isolation bypass: a
        flipped-to-container agent would keep launching claude on the HOST
        through a construction-time LocalCommandRunner while every other
        container decision (provision, seeds, tailer path, hook env) reads the
        live row and pretends isolation is in force."""
        self._runner = runner

    def _base_cmd(self) -> list[str]:
        cmd = [self.tmux_binary]
        if self.socket_name:
            cmd.extend(["-L", self.socket_name])
        return cmd

    async def _run(self, *args: str, timeout: float = 5.0) -> TmuxCommandResult:
        """Run ``tmux <args>`` and return its result.

        ``timeout`` defends against a hung tmux server. A timeout raises
        ``asyncio.TimeoutError``; the caller decides how to respond
        (typically: surface as a connect failure).
        """
        # Timeout layering note: this ``timeout`` is the per-tmux-command
        # ceiling (default 5s — generous for ``has-session`` / ``send-keys``
        # / ``kill-session`` which are local IPC and should return in <100ms).
        # The cold-start umbrella timeout (``_COLD_START_TIMEOUT_SEC`` = 60s)
        # bounds the whole ``_spawn_tmux_repl`` flow, which composes multiple
        # _run calls plus the new-session command (which spawns the REPL).
        # 5s here defends a hung tmux server; 60s up there defends a hung
        # REPL bootstrap (auth flow, CLAUDE.md load, etc.).
        cmd = self._base_cmd() + list(args)
        # Delegate the actual exec to the injected CommandRunner. For local
        # agents this is LocalCommandRunner — identical to the prior inline
        # create_subprocess_exec. For unix_user tenants the runner wraps the
        # argv in ``runuser -u pinky-<agent> --`` so tmux runs under the
        # agent's uid. Timeout/kill semantics live in the runner; a timeout
        # still raises asyncio.TimeoutError for the caller to handle.
        result = await self._runner.run(cmd, timeout=timeout)
        return TmuxCommandResult(
            returncode=result.returncode,
            stdout=result.stdout.decode("utf-8", errors="replace"),
            stderr=result.stderr.decode("utf-8", errors="replace"),
        )

    async def has_session(self) -> bool:
        """Return True if a tmux session with our name exists."""
        result = await self._run("has-session", "-t", self.session_name)
        return result.ok

    async def new_session(
        self,
        *,
        cwd: str,
        command: str,
        env: dict[str, str] | None = None,
    ) -> TmuxCommandResult:
        """Spawn a fresh detached tmux session running ``command``.

        ``cwd`` becomes the session's working directory — critical for
        ``claude --continue`` to find the right transcript.

        ``env`` is added as ``-e KEY=VAL`` flags (tmux 3.2+).
        """
        args = ["new-session", "-d", "-s", self.session_name, "-c", cwd]
        if env:
            for key, value in env.items():
                args.extend(["-e", f"{key}={value}"])
        # The command is passed as a single string arg; tmux invokes
        # it via the user's shell, so we shell-escape for safety.
        args.append(command)
        return await self._run(*args)

    async def kill_session(self) -> TmuxCommandResult:
        """Kill the tmux session. Idempotent — succeeds whether or not the
        session exists (callers shouldn't pre-check)."""
        result = await self._run("kill-session", "-t", self.session_name)
        # tmux returns 1 if the session didn't exist; treat that as ok
        # so callers can use this idempotently.
        if not result.ok and "can't find session" in result.stderr:
            return TmuxCommandResult(returncode=0, stdout="", stderr=result.stderr)
        return result

    async def resize_window(
        self, *, cols: int, rows: int,
    ) -> TmuxCommandResult:
        """Resize the session's window to ``cols`` × ``rows`` characters.

        Used by the read-only pane viewer so the agent's tmux pane
        reflows to match the modal's xterm grid dimensions — without
        this the pane stays at tmux's detached default (80×24) and the
        captured snapshot looks like a postage stamp inside a larger
        modal.

        Dims are clamped defensively to ``[20, 500]`` cols and
        ``[10, 200]`` rows: tmux itself caps around 500×200, and
        anything below 20×10 is too small for Claude Code's TUI to
        render coherently. The session's pane (active by default in
        single-pane layouts) follows the window size automatically.
        """
        cols = max(20, min(500, int(cols)))
        rows = max(10, min(200, int(rows)))
        return await self._run(
            "resize-window",
            "-t", self.session_name,
            "-x", str(cols),
            "-y", str(rows),
        )

    async def send_keys(self, text: str, *, enter: bool = True) -> TmuxCommandResult:
        """Send ``text`` to the active pane of the session.

        ``enter=True`` (default) appends a literal carriage return after
        the text, equivalent to ``tmux send-keys ... Enter``. The REPL
        receives the keystrokes and (for claude) processes them as a
        prompt.

        ``text`` is passed as a single tmux argument; tmux interprets
        no further shell metacharacters.

        Use ``paste_text`` instead for prompts that need to survive the
        claude cold-start splash UI (issue #514) — bracketed-paste plus
        a short delay is more reliable than raw keystrokes during the
        splash-to-chat transition.
        """
        args = ["send-keys", "-t", self.session_name, text]
        if enter:
            args.append("Enter")
        return await self._run(*args)

    async def send_literal(self, text: str) -> TmuxCommandResult:
        """Send ``text`` as LITERAL characters (``send-keys -l``).

        Unlike ``send_keys``, tmux performs no keyname interpretation —
        "Enter" types the five letters, "C-c" types three characters.
        Used by the typeable pane view, where the operator's typed text
        must never be accidentally promoted to a control key.
        """
        return await self._run("send-keys", "-t", self.session_name, "-l", text)

    async def paste_text(
        self,
        text: str,
        *,
        enter: bool = True,
        enter_delay_ms: int = 300,
    ) -> TmuxCommandResult:
        """Deliver ``text`` to the pane via tmux paste-buffer with
        bracketed paste mode, then (optionally) send Enter after a
        short delay.

        Adopted from Pulse v2's session manager (issue #514). Bracketed
        paste sequences are buffered atomically by the terminal —
        claude receives the full payload as a single block rather than
        as a stream of keystrokes. The ``enter_delay_ms`` window gives
        claude's post-login splash UI time to dismiss itself (which it
        does on input focus) before the submit Enter arrives.

        Compared to raw ``send-keys text Enter``: the keystroke-based
        path delivers text and Enter back-to-back, and claude's splash
        absorbs the Enter during its rendering transition. The result
        is text buffered in claude's input area with no submission —
        a permanently wedged session.

        Args:
            text: Prompt payload. Passed as a single tmux arg via
                ``set-buffer``, so no shell-metachar interpretation.
            enter: If True (default), send a submit Enter after
                ``enter_delay_ms`` ms. If False, leaves the pasted text
                in the input buffer unsubmitted.
            enter_delay_ms: Sleep between paste and Enter. Defaults to
                300 — the value Pulse v2's session manager uses for the
                claude backend (it uses 4000 for codex, which renders
                its prompt more slowly).

        Returns the last tmux command's result (either the Enter send
        or the paste, depending on ``enter``). On any intermediate
        failure, returns that failure result immediately.
        """
        # Per-session buffer name so concurrent paste_text on different
        # sessions don't race on a shared buffer.
        buf_name = f"pinky-{self.session_name}"

        set_result = await self._run("set-buffer", "-b", buf_name, text)
        if not set_result.ok:
            return set_result

        # ``-p`` enables bracketed paste mode (atomic, single block).
        # ``-d`` deletes the buffer after paste (saves memory on long
        # prompts; the buffer name is reusable for the next call).
        paste_result = await self._run(
            "paste-buffer",
            "-b",
            buf_name,
            "-d",
            "-t",
            self.session_name,
            "-p",
        )
        if not paste_result.ok or not enter:
            return paste_result

        if enter_delay_ms > 0:
            await asyncio.sleep(enter_delay_ms / 1000.0)

        return await self._run("send-keys", "-t", self.session_name, "Enter")

    async def capture_pane(
        self, *, lines: int = 200, escapes: bool = False, join: bool = False,
    ) -> TmuxCommandResult:
        """Capture the last ``lines`` lines of the pane's visible content.

        Used by the response pipeline as a fallback when transcript-file
        tailing isn't available, and by the read-only pane-view SSE
        endpoint (with ``escapes=True``) to stream the live pane to
        xterm.js in the chat UI.

        ``escapes=True`` adds ``-e`` so tmux includes the ANSI colour
        and cursor escapes it stripped by default — needed for xterm
        to render the pane faithfully. Default ``False`` preserves the
        plain-text shape callers expect.

        ``join=True`` adds ``-J`` so tmux joins wrapped lines and preserves
        trailing spaces — needed by the auth-relay watcher (#205) to read a
        long OAuth URL back as one contiguous string rather than column-wrapped
        fragments. Default ``False`` keeps the per-line shape.
        """
        args = [
            "capture-pane",
            "-t", self.session_name,
            "-p",  # print to stdout instead of paste buffer
        ]
        if escapes:
            args.append("-e")  # include ANSI escape sequences
        if join:
            args.append("-J")  # join wrapped lines (de-wrap long URLs)
        args.extend(["-S", str(-abs(lines))])
        return await self._run(*args)


# ──────────────────────────────────────────────────────────────────────────
# Worker queue payload
# ──────────────────────────────────────────────────────────────────────────


@dataclass
class _QueuedTurn:
    """Inbound message awaiting delivery to the claude REPL.

    Two flavors share this dataclass:

    - **External** (default): inbound user / broker messages. Counted as
      ``messages_sent``, logged to conversation_store, routed back via
      ``_response_callback`` with platform/chat_id/message_id.
    - **Internal** (``internal=True``): daemon-side prompts for lifecycle
      orientation — e.g. wake prompts at ``connect()``, pre-sleep save
      reminders at ``idle_sleep()``. Skip conversation_store appends and
      external-stats increments, do not route through response_callback,
      do not write to ``_inflight_metas``. Optional ``completion_event`` is
      set when the turn completes so callers can ``wait_for_completion``.
    """

    prompt: str
    platform: str = ""
    chat_id: str = ""
    message_id: str = ""
    queued_at: float = field(default_factory=time.time)
    # Internal-prompt flag set by ``_enqueue_internal_prompt``. See
    # ``_deliver_turn`` and ``_handle_turn_complete`` for the
    # conditional bypasses (no conversation_store append, no
    # response_callback, no ``_inflight_metas`` writes).
    internal: bool = False
    # Human-readable label for the internal-turn audit log
    # (``wake_prompt_sent``, ``idle_sleep_presave``, etc.). Ignored when
    # ``internal=False``.
    reason: str = ""
    # Optional event set by ``_handle_turn_complete`` when this turn
    # finishes — lets internal-prompt callers ``wait_for_completion`` so
    # they don't progress (e.g. disconnect) before the agent honors the
    # prompt. Ignored when ``None``.
    completion_event: asyncio.Event | None = None
    # #591 P1#2 (Murzik round-2): for wake prompts, the
    # ``on_wake_delivered`` callback must fire ONLY after the actual
    # paste lands — enqueue-time firing advances the cycle-gate
    # boundary even when the paste later fails (context-lock deferral
    # or REPL not-ready), eating the directive on the next RESUME.
    # ``_deliver_turn`` invokes this after ``result.ok`` so the boundary
    # tracks confirmed delivery, not just intent. ``None`` for
    # non-wake turns and for any internal turn whose enqueuer doesn't
    # care about post-delivery hooks.
    on_delivered: object = None  # Callable() -> None — fires on paste-success


@dataclass
class _InflightMeta:
    """One in-flight turn's routing metadata + completion signal.

    Issue #560 / PR for concurrent dispatch. Appended to
    ``_inflight_metas`` by ``_deliver_turn`` after a successful paste;
    popped FIFO by ``_handle_turn_complete`` on each ``stop_hook_summary``.
    Multiple entries co-exist when steering messages are pasted back-to-
    back into a busy REPL — Claude Code's native queued-prompt feature
    handles the in-pane queue; this deque tracks OUR routing/completion
    state per pending turn.

    Replaces PR #496 round-2's single ``_inflight_meta`` dict, which was
    the chokepoint forcing strictly serial dispatch and made mid-turn
    steering impossible (the worker awaited ``_turn_done`` between
    dispatches to protect the dict from being clobbered).

    **Ordering** is preserved end-to-end: Claude Code processes pasted
    prompts sequentially (its native input queue is FIFO); the transcript
    tailer reads the JSONL file in line order; FIFO pop matches FIFO
    append. The single-meta-clobber bug (#496 Case 1) is defended by
    each turn carrying its OWN routing dict that lives in the deque
    entry — no shared mutable cell.
    """

    # Routing metadata: {"platform", "chat_id", "message_id"}. Empty
    # dict for internal turns (wake prompts, pre-sleep save reminders) —
    # they have no external recipient. Used by ``_handle_turn_complete``
    # to populate the ``TurnResponse`` it passes to ``_response_callback``.
    meta: dict
    # Per-turn completion event. Set by ``_handle_turn_complete`` when
    # THIS entry is popleft'd from the deque. Used by callers with
    # ``wait_for_completion=True`` (e.g. pre-sleep save) to block until
    # their specific turn finishes — NOT some later turn. Also set on
    # the watchdog timeout path for the HEAD only (tail entries get
    # requeued instead; their event fires when they're actually rerun).
    # None for fire-and-forget.
    completion_event: asyncio.Event | None
    # True for daemon-internal turns. ``_handle_turn_complete`` skips the
    # ``conversation_store.append`` + ``_response_callback`` calls when
    # this flag is set. The turn's response still flows through the
    # transcript JSONL (audit), just not into the chat-side surfaces.
    internal: bool
    # When the paste+Enter succeeded. Informational only — the watchdog
    # ages turns by deque-head transitions (``_head_started_at``), NOT
    # by ``dispatched_at``, so a queued turn gets its OWN fair timeout
    # window once it becomes the head (Murzik review on PR for #560).
    dispatched_at: float
    # Original ``_QueuedTurn`` carried so the watchdog can REQUEUE the
    # tail entries for replay after a stuck-head force_restart, instead
    # of silently dropping them. Murzik review on PR #561 found that
    # the initial deque shape only stored routing metadata; when A
    # wedged and the watchdog force-restarted, B/C (already dispatched
    # into CC's native queue but not yet run) were killed with the old
    # REPL and could not be replayed. The replay path uses ``turn`` to
    # push the original prompt + completion_event back to the front of
    # ``_message_queue`` so the new worker re-dispatches them after
    # the restart settles.
    turn: _QueuedTurn
    # Paste-time baselines for the watchdog's secondary stall verdict (#592).
    # The verdict compares the CURRENT transcript mtime against
    # ``max(transcript_mtime_at_paste, paste_succeeded_at) + _TRANSCRIPT_PASTE_SLACK``:
    # growth past that floor means the REPL was active on this turn, so a stale
    # live_status.last_updated (Stop hook missed advancing it) can be ignored and
    # the meta drained as phantom.
    #
    # ``paste_succeeded_at`` is a daemon-clock stamp taken right after paste
    # success; it is the authoritative floor. ``transcript_mtime_at_paste`` is the
    # file mtime sampled at the same moment, but the file write can LAG the tmux
    # paste (paste_text only waits on paste-buffer + 300 ms + Enter, not on Claude
    # writing the JSONL), so on its own it can be a stale PREVIOUS-turn mtime far in
    # the past — which would let a real hang-on-paste's echo clear the slack and
    # false-drain as idle (Murzik, #595 review). Taking the max anchors the floor to
    # this turn's paste time regardless of write lag. Both None ⇒ fall back to wedged.
    transcript_mtime_at_paste: float | None = None
    paste_succeeded_at: float | None = None


# ──────────────────────────────────────────────────────────────────────────
# TmuxSession
# ──────────────────────────────────────────────────────────────────────────


# Reconnect backoff schedule (seconds). Kept in step with StreamingSession's
# ``_RECONNECT_BACKOFF`` so api._heartbeat_resurrect can treat runtimes
# uniformly.
_RECONNECT_BACKOFF = (2, 8, 30)

# Cold-start timeout: how long we wait for the tmux ``new-session`` +
# ``claude`` REPL boot to complete before declaring the cold-start failed.
# Generous (60s) because tmux startup is cheap but the claude REPL may need
# to authenticate / fetch first turn / load CLAUDE.md.
_COLD_START_TIMEOUT_SEC = 60.0

# Per-turn timeout: how long ANY single in-flight turn can be at the
# HEAD of ``_inflight_metas`` without its ``stop_hook_summary`` landing
# before the watchdog considers it stuck and triggers ``force_restart``.
# Generous (10 min) to cover tool-use loops + slow models + cold-model
# dispatch. Anything longer is "stuck".
#
# Note (#560): pre-PR this was the worker's per-iteration ``_turn_done``
# wait timeout. With concurrent dispatch, the worker no longer awaits
# between turns — the watchdog ages turns by deque-HEAD transitions
# (``_head_started_at``) so each queued turn gets its own fair timeout
# window once it becomes the head (Murzik review).
_TURN_DONE_TIMEOUT_SEC = 600.0

# Watchdog poll cadence. 15s strikes a balance: tight enough that a
# stuck REPL gets force_restarted inside one cycle past
# ``_TURN_DONE_TIMEOUT_SEC``; loose enough that the loop is invisible
# in CPU profiles even with many active tmux sessions.
_WATCHDOG_TICK_SEC = 15.0

# #118 — idle-signal freshness floor. When trusting Claude Code's "idle"
# hook signal to reconcile a phantom inflight head, the idle must be
# at-or-after when the CURRENT head was pasted (``min(_head_started_at,
# head.dispatched_at)``). No fixed slack window: the Stop-hook idle stamp
# and the dispatch stamp share the daemon clock (no skew), so a stale idle
# left over from the previous turn is rejected outright — a genuine
# hang-on-paste is classified ``wedged``, not phantom-drained. (Replaces the
# unsafe ``_head_started_at - 5s`` window flagged in Murzik's round-2 review.)

# #592 — transcript-activity slack for the secondary stall-verdict check.
# After the idle-freshness floor check fails (Stop hook didn't advance
# live_status.last_updated for this turn), we fall back to transcript mtime:
# if the transcript grew more than this many seconds after the paste, the
# REPL was active on the turn and the meta is phantom. The slack prevents the
# paste echo itself (~0–1 s in the transcript) from triggering the check —
# we want evidence of a *response*, not just the pasted text landing.
_TRANSCRIPT_PASTE_SLACK = 5.0

# #692 — background-task activity window for the stall verdict. A turn parked
# on a long-running background task (a Dynamic Workflow, or an ``Agent`` /
# background tool call) emits nothing to the MAIN transcript — its subagents
# stream to their own transcripts under ``<session>/subagents`` and
# ``<session>/workflows``. ``_transcript_recently_grew`` only watches the main
# transcript, so such a turn looks "quiet" and the watchdog would force_restart
# it (killing the in-flight work) ~``_TURN_DONE_TIMEOUT_SEC`` in. We treat a
# subagent/workflow transcript written within this window as positive "still
# making progress" evidence → ``growing``, not ``wedged``. Tighter than the
# main-transcript window: a workflow making background progress writes a
# subagent transcript far more often than this, while a workflow that has been
# silent this long AND whose main REPL is quiet is genuinely stuck.
_BACKGROUND_TASK_ACTIVE_WINDOW_SEC = 180.0

# #731 — absolute ceiling for crediting an in-flight FOREGROUND tool call as
# liveness. A single long blocking foreground tool call (e.g. a deliberate
# ``gh run watch`` up to ~10 min, or a slow build) writes nothing to the main
# transcript and — unlike a Workflow/Agent — spawns no subagent dir, so it
# looks identical to a wedge to the stall verdict. The PreToolUse/PostToolUse
# hooks (task #93) tell us a tool is genuinely in flight, and we extend the
# wedge window while one is. The ceiling bounds that trust: a tool "in flight"
# longer than this is treated as a lost finish-POST or a genuinely hung child
# and is NOT credited (and is pruned), so a real stuck REPL still recovers —
# just later. 30 min is generous headroom over the ~10 min worst-case legit
# foreground wait while keeping the worst-case false-negative (delayed wedge
# recovery) bounded.
_FOREGROUND_TOOL_ACTIVE_CEILING_SEC = 1800.0

# Issue #570 — wake-prompt readiness-gate timeout. ``_deliver_turn`` awaits
# ``_session_ready_event`` for turns with ``internal=True and
# reason.startswith("wake_")`` so the wake prompt's paste doesn't land while
# Claude Code is still in its splash/MCP-bootstrap phase (where bracketed-paste
# + 300ms-Enter is consumed by transition state instead of submitting the
# turn). The event opens when ``set_transcript_path`` is called by the
# SessionStart hook. 30s is generous — the worst observed claude boot on the
# prod Mac Mini takes ~5-15s loading shared-MCP + per-agent MCP servers; the
# timeout exists as a safety fallback (not a target). On timeout we proceed
# with the paste anyway (legacy behavior), so a regressed hook degrades to
# the pre-#570 race rather than hanging the session. Gate lives at delivery
# time (not enqueue time) so the wake turn stays at the queue HEAD and
# external sends arriving during the wait queue BEHIND it — preserves FIFO
# across the bootstrap window (Murzik #571 review catch).
_SESSION_READY_GATE_TIMEOUT_SEC = 30.0

# Auth-relay (#205): after spawn, watch the pane for the claude OAuth login
# wall for this long, polling at this interval. The wall (if any) appears
# within seconds of launch; if it never shows the session authed normally and
# the watcher exits. Read-only capture_pane — no turn is pasted, so the
# inflight watchdog never ages the session out from under the watcher.
_AUTH_WALL_DETECT_WINDOW_SEC = 90.0
_AUTH_WALL_POLL_SEC = 2.5
# Pause after injecting the code before re-reading the pane, to let claude
# complete the login handshake and clear the wall.
_AUTH_LOGIN_SETTLE_SEC = 2.5

# Issue #151 — native ultracode activation settle. After typing the interactive
# ``/effort ultracode`` into a freshly-ready REPL (see ``_deliver_turn``), pause
# briefly so the CLI processes the slash command before the wake prompt's
# bracketed-paste lands. The command is client-side + instant (no model turn),
# so a short settle is sufficient; it is NOT a correctness gate, just ordering
# slack between two send paths into the same pane.
_NATIVE_ULTRACODE_SETTLE_SEC = 0.4


class TmuxSession:
    """Agent session backed by an interactive ``claude`` REPL in tmux.

    Implements the ``Transport`` protocol (see ``transport.py``). Drop-in
    replacement for ``StreamingSession`` and ``CodexSession`` from the
    broker / api / scheduler's perspective.

    See module docstring for architecture overview and out-of-scope items
    (the response capture pipeline is the principal remaining gap).
    """

    def __init__(
        self,
        config: StreamingSessionConfig,
        *,
        response_callback=None,
        conversation_store=None,
        cost_callback=None,
        stream_event_callback=None,
        analytics_store=None,
        registry=None,
        tmux_control: _TmuxControl | None = None,
    ) -> None:
        self._config = config
        self._response_callback = response_callback
        self._cost_callback = cost_callback
        self._conversation_store = conversation_store
        self._stream_event_callback = stream_event_callback
        self._analytics_store = analytics_store
        self._registry = registry

        self.agent_name = config.agent_name

        # State machine — full matrix, mirrors StreamingSession post-PR6.
        self._state_machine = StateMachine(f"{self.agent_name}-tmux")

        # Resume handle for tmux is the session name itself. Pinning by
        # name preserves cwd → ``claude --continue`` resumes via that cwd's
        # most-recent transcript automatically.
        self._session_name = self._build_session_name()
        self.resume_handle = self._session_name

        # Tmux subprocess control. Injectable for tests (mock the whole
        # ``_TmuxControl`` rather than monkeypatching subprocess primitives).
        # For an isolation_mode="container" agent (runtime gate ON), the tmux
        # server + REPL run INSIDE its container via a ContainerCommandRunner;
        # otherwise the default LocalCommandRunner reproduces today's behavior.
        self._tmux = tmux_control or _TmuxControl(
            self._session_name, command_runner=self._select_command_runner()
        )

        # Worker queue + task.
        self._message_queue: asyncio.Queue[_QueuedTurn] = asyncio.Queue()
        self._worker_task: asyncio.Task | None = None
        # Background watchdog that ages the deque head against
        # ``_TURN_DONE_TIMEOUT_SEC`` and triggers ``force_restart`` when
        # a stop hook fails to land. Issue #560 replaces the per-iter
        # worker timeout with a separate task so concurrent dispatch
        # isn't blocked behind a per-turn wait. Started/cancelled
        # alongside ``_worker_task``.
        self._watchdog_task: asyncio.Task | None = None
        # Auth-relay watcher (#205): flag-gated background task that detects the
        # claude OAuth login wall and relays it to the owner. Started at the end
        # of ``_spawn_tmux_repl``, cancelled in ``disconnect``.
        self._auth_watcher_task: asyncio.Task | None = None
        self._processing = False

        # Operational stats. Shape matches StreamingSession.stats for the
        # keys callers actually read (broker, api, watchdog); cost_usd is
        # absent because subscription billing isn't per-turn metered.
        self._stats = {
            "turns": 0,
            "messages_sent": 0,
            "errors": 0,
            "reconnects": 0,
            "auto_restarts": 0,
        }
        self.usage = SessionUsage()

        # Context-budget watchdog state (task #95). The nudge latch
        # prevents firing ``restart_nudge`` SSE events on every turn
        # once we're above the agent's ``restart_threshold_pct`` — it
        # re-arms only after context drops below the threshold (e.g.
        # post-/compact). Per-turn token accumulation lives in
        # ``self.usage`` (a SessionUsage dataclass).
        self._restart_nudge_fired = False

        # Soft context-watermark latch (#614). Distinct from
        # ``_restart_nudge_fired`` (which gates the SSE-to-UI restart_nudge
        # at the hard threshold): this gates the one-shot in-REPL nudge
        # injected when usage first crosses the agent's *soft* threshold.
        # Re-arms when usage drops back below the soft line (e.g. after a
        # context_restart), so it can fire once per window.
        self._soft_nudge_fired = False

        # Effort knob. tmux's claude REPL doesn't currently honor a
        # per-session effort override (CLAUDE_EFFORT env is set at
        # spawn time and we'd have to relaunch to change it). We accept
        # the call to keep the Transport contract consistent and log a
        # warning when it's used.
        self._effort_override: str | None = None

        # Resume-handle update callback (e.g. AgentRegistry persistence).
        # For tmux the resume_handle is stable from construction (= session
        # name), so this is fired exactly once on connect for symmetry with
        # the SDK backend's persistence hook.
        self._on_resume_handle = None

        self.created_at = time.time()
        self.last_active = self.created_at
        self.account_info: dict = {"apiProvider": "tmux_claude_repl"}
        self._current_activity = ""
        self._current_thinking = ""
        # #731: tool_use_id → start-time for tool calls that have started
        # (PreToolUse hook) but not finished (PostToolUse hook). The inflight
        # watchdog reads this as positive liveness so a long foreground tool
        # call isn't mistaken for a wedged REPL. Bounded/pruned by the verdict.
        self._inflight_tool_calls: dict[str, float] = {}
        self._activity_log: list[str] = []

        # Response capture pipeline (PR8b). Lazily constructed in
        # ``_spawn_tmux_repl`` after we know the transcript path. The
        # tailer reads Claude Code's JSONL transcript, accumulates each
        # turn's assistant content, and fires ``_handle_turn_complete``
        # on every ``stop_hook_summary`` entry — which routes to
        # ``_response_callback`` to deliver the response upstream.
        self._tailer: TmuxTranscriptTailer | None = None
        # FIFO of in-flight turn routing metadata. Issue #560 replaces
        # PR #496 round-2's single ``_inflight_meta`` dict (which forced
        # strictly serial dispatch via a worker gate, breaking mid-turn
        # steering). Each successful ``paste_text(..., enter=True)`` in
        # ``_deliver_turn`` appends one ``_InflightMeta``; each
        # ``stop_hook_summary`` in ``_handle_turn_complete`` pops the
        # oldest. Multiple entries co-exist while Claude Code's native
        # queued-prompt feature drains the in-pane queue.
        #
        # Defense of #496 Case 1 (response routed to wrong chat_id):
        # each entry carries its OWN routing dict; there is no shared
        # mutable cell to clobber. Ordering is FIFO end-to-end because
        # CC processes pasted prompts sequentially, the tailer reads
        # transcript JSONL in line order, and ``popleft`` matches
        # ``append``.
        self._inflight_metas: deque[_InflightMeta] = deque()
        # Timestamp (``time.time()``) of when the CURRENT deque HEAD
        # became the head — either via empty→nonempty append, or via
        # popleft when entries remain behind it. Reset to ``None`` when
        # the deque drains. The ``_inflight_watchdog`` ages turns
        # against this, NOT against ``dispatched_at``, so a queued turn
        # gets its own ``_TURN_DONE_TIMEOUT_SEC`` window once it becomes
        # the head (Murzik review on PR for #560).
        self._head_started_at: float | None = None
        # Back-compat advisory signal. Pre-#560 this was the worker's
        # per-iteration gate (the bottleneck that broke steering).
        # Post-#560 the worker no longer awaits it between dispatches;
        # ``_handle_turn_complete`` still ``.set()``s it on every turn
        # so external observers (tests, ``_enqueue_internal_prompt`` with
        # ``wait_for_completion=True`` callers via the per-turn
        # ``completion_event``, ``connect``-time clears, etc.) keep
        # working unchanged. Treat as "ANY turn completed since last
        # clear", not "exactly one turn was inflight".
        self._turn_done: asyncio.Event = asyncio.Event()
        # Becomes true only after the worker observes a successful turn_done.
        # Before that, restart cannot discard completed agent work, so
        # watchdog recovery may bypass the persistence guard.
        self._has_completed_turn = False

        # Murzik #522 round-1: the worker keeps the current turn IN-HAND
        # across transient failures instead of ``get()``-ing a new one
        # every iteration. ``_inflight_turn`` is None when the worker is
        # idle / between turns; populated by the worker as soon as it
        # pulls from the queue, and cleared only on success or permanent
        # failure. Survives ``force_restart`` (instance state on self
        # outlives worker-task cancellation + re-spawn), which is what
        # lets the new REPL pick the same turn back up after a stuck-
        # REPL escalation.
        self._inflight_turn: _QueuedTurn | None = None

        # Launch-mode snapshot written by ``_build_claude_cmd`` and read
        # by ``connect()`` to derive wake-prompt orientation. None until
        # the first launch. Cleared/overwritten on each launch.
        self._last_launch_used_continue: bool = False
        self._last_launch_forced_fresh: bool = False
        self._last_launch_had_prior_transcript: bool = False

        # Issue #563 — "first transcript bind" tracking. Set to True in
        # ``_start_tailer`` after the tailer is constructed; consumed
        # on the first ``set_transcript_path`` call. Combined with
        # ``not _last_launch_used_continue``, drives the seek-to-byte-0
        # behavior for fresh launches whose SessionStart hook arrives
        # AFTER CC has already written the first turn's
        # ``stop_hook_summary``. Continue launches preserve the
        # seek-to-EOF default (#496 round-1 Case 3 reply-spam defense).
        self._tailer_first_bind_pending: bool = False

        # Issue #565 — handle to the delayed first-bind recovery task
        # scheduled from ``_start_tailer``. Cancelled in ``_stop_tailer``
        # so a torn-down session doesn't have a stray task firing
        # ``set_transcript_path`` against a stopped tailer.
        self._first_bind_recovery_task: asyncio.Task[None] | None = None

        # Issue #570 — wake-prompt readiness gate. Set when
        # ``set_transcript_path`` is called by the SessionStart hook
        # (signalling "claude is past splash/MCP-boot, input area is
        # live"). ``_deliver_turn`` awaits this for turns with
        # ``internal=True and reason.startswith("wake_")`` so the wake
        # prompt's paste lands AFTER claude is ready to receive a
        # submit Enter. Without the gate, on ``force_fresh_context_once``
        # respawn the bracketed-paste + 300ms-Enter sequence completes
        # during MCP bootstrap and the Enter is consumed by transition
        # state instead of submitting the turn (CR-01 failure mode
        # from #543 validation matrix). Gate lives at delivery time
        # (not enqueue time) so the wake turn stays at the queue HEAD
        # and external sends arriving during the wait queue BEHIND it
        # — preserves FIFO across the bootstrap window (Murzik #571
        # review catch). Reset to a fresh ``Event()`` on every spawn
        # in ``_start_tailer`` — must NOT survive across respawns or
        # a stale "open" state from the previous session would let
        # wake prompts paste into a still-booting fresh REPL.
        self._session_ready_event: asyncio.Event = asyncio.Event()

        # Issue #151 — native ultracode activation. Armed by
        # ``_build_claude_cmd`` on a FRESH cold-start launch whose effective
        # effort is ultracode; consumed exactly once in ``_deliver_turn``,
        # which types the interactive ``/effort ultracode`` into the
        # now-ready REPL before the first prompt pastes (upgrading from
        # "xhigh + ULTRACODE_DIRECTIVE" to the CLI's real ultracode tier —
        # its own standing dynamic-workflow system-reminder). Default False
        # so non-ultracode agents — and unit tests that call ``_deliver_turn``
        # directly without building the launch command — never type the
        # slash command. Re-armed per launch (see ``_build_claude_cmd``).
        self._native_ultracode_pending: bool = False

        # Test seam: when True, ``connect()`` skips wake-prompt assembly
        # + enqueue. Production callers must NOT flip this; it exists so
        # unit tests that mock at the paste/queue layer can exercise
        # ``connect()`` without stranding the worker on a never-
        # completing wake-prompt turn (the worker awaits
        # ``_turn_done`` between turns; without a simulated transcript
        # tailer firing ``_handle_turn_complete``, the worker would
        # block forever on the first dispatched turn — wake or otherwise).
        # Dedicated wake-prompt tests leave this False and provide the
        # tailer simulation explicitly.
        self._skip_wake_prompt_for_tests: bool = False

    # ── Identity ────────────────────────────────────────────────────────

    @property
    def id(self) -> str:
        """Stable identifier matching StreamingSession's format."""
        label = getattr(self._config, "label", "") or "main"
        return f"{self.agent_name}-{label}"

    def _container_agent(self, strict: bool = False):
        """Return this session's Agent iff it should run inside a container —
        the runtime gate is ON *and* isolation_mode=="container". Returns None
        (→ default local behavior) otherwise.

        ``strict`` (#638, used by the SPAWN path): a registry lookup FAILURE
        raises instead of returning None. The default fail-safe is right for
        read-side consumers (a hiccup must not break a local session's env or
        tailer), but at spawn time silently falling back to a
        LocalCommandRunner would launch a container-labeled agent UNISOLATED
        on the host — fail closed there."""
        from pinky_daemon.provisioning import container_runtime_enabled

        if not container_runtime_enabled() or not self._registry:
            return None
        try:
            agent = self._registry.get(self.agent_name)
        except Exception as e:
            if strict:
                raise RuntimeError(
                    f"registry lookup failed while resolving isolation for "
                    f"{self.agent_name!r} — refusing to spawn (a fallback to "
                    f"local execution would silently bypass container "
                    f"isolation): {e}"
                ) from e
            return None
        if not agent or getattr(agent, "isolation_mode", "") != "container":
            return None
        return agent

    def _select_command_runner(self, agent=_UNSET) -> CommandRunner:
        """LocalCommandRunner by default; a ContainerCommandRunner bound to the
        agent's container for a gated container agent, so every tmux command
        execs into the container. ``agent`` lets the spawn path pass its own
        registry snapshot so the runner and the rest of the spawn agree."""
        if agent is _UNSET:
            agent = self._container_agent()
        if agent is None:
            return LocalCommandRunner()
        from pinky_daemon.provisioning import ContainerNames, container_runtime_binary

        names = ContainerNames.for_agent(agent.name)
        # The agent's host working_dir is bind-mounted into the container at the
        # SAME absolute path (ContainerProvisioner._create_argv), so it's a valid
        # in-container cwd. Use the SESSION's (api-resolved) working_dir so the
        # `podman exec -w`, `tmux new-session -c`, trust seed, and tailer slug
        # all agree on one path (the registry row may hold a symlinked variant).
        workdir = (self._config.working_dir or "").strip() or (
            (getattr(agent, "working_dir", "") or "").strip()
        )
        return ContainerCommandRunner(
            names.container,
            container_binary=container_runtime_binary(),
            workdir=workdir or None,
        )

    async def _ensure_container_started(self, agent=_UNSET) -> None:
        """For a gated container agent, idempotently provision + start its
        container BEFORE the first ``podman exec`` (tmux new-session). No-op for
        local/non-container agents and when the gate is off. Run off-loop since
        the podman calls are blocking subprocesses.

        #638: runs OUTSIDE the 60s cold-start umbrella with its own (much
        larger) budget — ensure_started can legitimately include a multi-minute
        ``podman pull`` (image evicted, container_image changed), and a
        wait_for cancellation can't stop a to_thread anyway (it would leak a
        zombie provisioning thread that races the retry's provision)."""
        if agent is _UNSET:
            agent = self._container_agent()
        if agent is None:
            return
        from pinky_daemon.provisioning import get_provisioner

        provisioner = get_provisioner(
            "container",
            signing_key_provider=self._registry.get_or_create_signing_key,
        )
        timeout = _container_start_timeout_sec()
        try:
            await asyncio.wait_for(
                asyncio.to_thread(provisioner.ensure_started, agent),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            raise RuntimeError(
                f"container start for {self.agent_name!r} exceeded "
                f"{timeout:.0f}s (PINKY_CONTAINER_START_TIMEOUT_SEC) — likely a "
                f"slow/wedged image pull; NOTE the underlying provisioning "
                f"thread cannot be cancelled and may still complete in the "
                f"background, in which case the next start attempt is fast"
            ) from None
        await self._check_container_image_contract()

    async def _check_container_image_contract(self) -> None:
        """Fail fast (clear message → BOOT_FAILED) when the operator's
        bring-your-own image is missing a binary the daemon's runtime depends
        on: ``tmux`` (every session command is ``podman exec … tmux``),
        ``claude`` (the REPL itself), ``python3`` (in-container trust seed +
        hook scripts). Without this, a bad image surfaces as an opaque
        tmux-spawn stderr minutes later. Probe failures other than a clean
        "missing" verdict are tolerated (the spawn will surface them)."""
        runner = self._select_command_runner()
        if not isinstance(runner, ContainerCommandRunner):
            return
        probe = "for c in tmux claude python3; do command -v $c >/dev/null || echo $c; done"
        try:
            res = await runner.run(["sh", "-c", probe], timeout=15)
        except Exception as e:
            _log(
                f"tmux[{self.agent_name}]: image-contract probe errored "
                f"(non-fatal, spawn will surface real failures): {e}"
            )
            return
        missing = res.stdout.decode("utf-8", "replace").split() if res.ok else []
        if missing:
            raise RuntimeError(
                f"container image for {self.agent_name!r} is missing required "
                f"binaries: {', '.join(missing)} — the bring-your-own image must "
                f"provide tmux, claude (Claude Code CLI), and python3"
            )

    def _seed_container_claude_creds(self) -> None:
        """One-time host-side seed of the daemon user's Claude OAuth credentials
        into a container agent's (host-visible) CLAUDE_CONFIG_DIR, so the
        in-container ``claude`` starts authenticated instead of sitting at a
        login prompt (#638 creds story).

        The durable design: CLAUDE_CONFIG_DIR lives inside the same-path-mounted
        working_dir, so a subsequent in-container ``claude login`` (or a token
        refresh) persists across container restarts AND recreates. This seed is
        only the bootstrap — skipped when creds already exist there. First-party
        trusted agents sharing the operator's Claude identity is the accepted
        model on both fleets today; set PINKY_CONTAINER_SEED_CREDS=0 to disable
        and log each tenant in manually (podman exec -it pinky-<agent> claude
        login). Best-effort: failure must never block the spawn."""
        if self._container_agent() is None:
            return
        if os.environ.get("PINKY_CONTAINER_SEED_CREDS", "1").strip().lower() in (
            "0", "false", "no",
        ):
            return
        mode = _claude_auth_mode(self.agent_name)
        if mode == _CLAUDE_AUTH_MODE_PER_AGENT_OAUTH:
            _log(
                f"tmux[{self.agent_name}]: claude_auth_mode={mode} — "
                f"skipping shared host credentials seed; existing per-agent "
                f"container-home creds must be preserved"
            )
            return
        # #780: when static-token forwarding is enabled, claude authenticates
        # via CLAUDE_CODE_OAUTH_TOKEN (no refresh) — never seed the refresh-prone
        # .credentials.json. Keyed on the FLAG, not token presence: fail CLOSED
        # so a rollout misconfig (flag on, token missing) surfaces as a loud
        # login wall instead of silently falling back to the shared refresh-
        # token file (Murzik #781 P2).
        if self._forward_oauth_enabled():
            _log(
                f"tmux[{self.agent_name}]: static OAuth token forwarding enabled — "
                f"skipping container creds seed (#780; fail-closed if token absent)"
            )
            return
        wd = (self._config.working_dir or "").strip()
        if not wd or not Path(wd).is_absolute():
            return
        from pinky_daemon.provisioning import container_config_dir

        dst_dir = Path(container_config_dir(wd))
        dst = dst_dir / ".credentials.json"
        if dst.exists():
            return
        host_cfg = (os.environ.get("CLAUDE_CONFIG_DIR") or "").strip()
        src = (Path(host_cfg) if host_cfg else Path.home() / ".claude") / ".credentials.json"
        try:
            if not src.exists():
                _log(
                    f"tmux[{self.agent_name}]: no host claude credentials at "
                    f"{src} to seed — in-container claude will need a manual "
                    f"login (non-fatal)"
                )
                return
            _log(
                f"tmux[{self.agent_name}]: claude_auth_mode={mode} "
                f"host_seed_source_state={_claude_creds_state(src)}"
            )
            dst_dir.mkdir(parents=True, exist_ok=True)
            # Create 0600 from the first byte (no write→chmod gap in a
            # bind-mounted dir): open with mode via os.open, then write.
            fd = os.open(str(dst), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                with os.fdopen(fd, "wb") as fh:
                    fh.write(src.read_bytes())
            except Exception:
                dst.unlink(missing_ok=True)
                raise
            _log(
                f"tmux[{self.agent_name}]: seeded claude credentials into "
                f"container config dir {dst_dir}"
            )
        except Exception as e:
            _log(
                f"tmux[{self.agent_name}]: container creds seed failed "
                f"(non-fatal): {e}"
            )

    async def _seed_container_home_creds(self) -> None:
        """Copy the seeded Claude credentials from the (bind-mounted, host-
        seeded) CLAUDE_CONFIG_DIR into the home VOLUME's ``~/.claude/`` —
        INSIDE the container, after it is running.

        Live-validated on the Pi (#638 rollout): claude reads OAuth
        credentials from ``$HOME/.claude/.credentials.json``, NOT from
        CLAUDE_CONFIG_DIR — with creds only in the config dir it sits at the
        OAuth login screen forever (trust flags in CLAUDE_CONFIG_DIR *are*
        honored; credentials are not). Idempotent: skips when the volume
        already has credentials, so an agent's own later ``claude login`` (or
        a token refresh) is never clobbered. Best-effort: a failure must not
        block the spawn (worst case is the login prompt, not a regression)."""
        runner = self._select_command_runner()
        if not isinstance(runner, ContainerCommandRunner):
            return
        mode = _claude_auth_mode(self.agent_name)
        if mode == _CLAUDE_AUTH_MODE_PER_AGENT_OAUTH:
            try:
                res = await runner.run(
                    ["python3", "-c", _CONTAINER_CREDS_STATE_PY], timeout=15
                )
                if res.ok:
                    state = res.stdout.decode("utf-8", "replace").strip()
                    _log(
                        f"tmux[{self.agent_name}]: claude_auth_mode={mode} "
                        f"{state or 'home_creds_state=empty'} — "
                        f"not copying shared credentials"
                    )
                else:
                    _log(
                        f"tmux[{self.agent_name}]: claude_auth_mode={mode} "
                        f"home creds probe rc={res.returncode} "
                        f"stderr={res.stderr.decode('utf-8', 'replace').strip()[:200]!r} "
                        f"(non-fatal; not copying shared credentials)"
                    )
            except Exception as e:
                _log(
                    f"tmux[{self.agent_name}]: claude_auth_mode={mode} "
                    f"home creds probe failed (non-fatal; not copying shared "
                    f"credentials): {e}"
                )
            return
        seed_sh = (
            'test -f "$HOME/.claude/.credentials.json" || { '
            'test -f "$CLAUDE_CONFIG_DIR/.credentials.json" && '
            'mkdir -p "$HOME/.claude" && '
            'cp "$CLAUDE_CONFIG_DIR/.credentials.json" '
            '"$HOME/.claude/.credentials.json" && '
            'chmod 600 "$HOME/.claude/.credentials.json"; }'
        )
        try:
            res = await runner.run(["sh", "-c", seed_sh], timeout=15)
            if res.ok:
                _log(
                    f"tmux[{self.agent_name}]: ensured claude credentials in "
                    f"container home volume"
                )
            else:
                _log(
                    f"tmux[{self.agent_name}]: in-container creds seed "
                    f"rc={res.returncode} "
                    f"stderr={res.stderr.decode('utf-8', 'replace').strip()[:200]!r} "
                    f"(non-fatal)"
                )
        except Exception as e:
            _log(
                f"tmux[{self.agent_name}]: in-container creds seed failed "
                f"(non-fatal): {e}"
            )

    async def _seed_container_trust(self, project_dir: str) -> None:
        """Seed Claude Code's first-run trust/bypass flags INSIDE a container
        agent's home volume — its ``.claude.json`` lives there, not on a host
        path the daemon can resolve, so we ``podman exec`` the seed now that the
        container is running. No-op for local/non-container agents. Best-effort:
        a failure must never block the spawn (worst case is the pre-existing
        trust-gate wedge, not a regression). Mirrors ``_seed_claude_trust_file``
        but runs in-container and reads CLAUDE_CONFIG_DIR from the container env."""
        runner = self._select_command_runner()
        if not isinstance(runner, ContainerCommandRunner):
            return
        seed_py = (
            "import json,os,sys,pathlib\n"
            "cfg=(os.environ.get('CLAUDE_CONFIG_DIR') or '').strip()\n"
            "base=pathlib.Path(cfg) if cfg else pathlib.Path(os.environ.get('HOME') or '/')\n"
            "p=base/'.claude.json'\n"
            "proj=os.path.realpath(sys.argv[1])\n"
            "d={}\n"
            "if p.exists():\n"
            "    try: d=json.loads(p.read_text())\n"
            "    except Exception: d={}\n"
            "if not isinstance(d,dict): d={}\n"
            "d['bypassPermissionsModeAccepted']=True\n"
            # Live-validated on the Pi (#638): without the GLOBAL onboarding
            # flag, a fresh container config dir triggers claude's first-run
            # wizard, whose first step is the OAuth sign-in screen — even with
            # VALID credentials in place (claude had already written
            # oauthAccount/userID from them). Local agents inherit the
            # operator's onboarded ~/.claude.json so they never hit this.
            "d['hasCompletedOnboarding']=True\n"
            "pr=d.setdefault('projects',{})\n"
            "pr.setdefault(proj,{})\n"
            "pr[proj]['hasTrustDialogAccepted']=True\n"
            "pr[proj]['hasCompletedProjectOnboarding']=True\n"
            # Live-validated on the Pi (lera rollout, #735): a fresh container
            # also wedges on Claude Code's "N new MCP servers found" approval
            # prompt at first spawn — and the queued-message paste then sends
            # Enter onto whatever is highlighted. Pre-approve the project's own
            # .mcp.json servers (the daemon wrote that file; they're trusted).
            "pr[proj]['enableAllProjectMcpServers']=True\n"
            "p.parent.mkdir(parents=True,exist_ok=True)\n"
            "p.write_text(json.dumps(d,indent=2))\n"
            # Same rollout, second wedge: `--dangerously-skip-permissions`
            # shows the Bypass Permissions accept dialog whose DEFAULT is
            # "No, exit" — the message paste's Enter kills the REPL. The
            # .claude.json flag above is NOT sufficient on CC 2.1.x; the
            # actual switch is skipDangerousModePermissionPrompt in
            # CLAUDE_CONFIG_DIR/settings.json. Merge it in, never clobber.
            "sp=(base if cfg else base/'.claude')/'settings.json'\n"
            "sp.parent.mkdir(parents=True,exist_ok=True)\n"
            "s={}\n"
            "if sp.exists():\n"
            "    try: s=json.loads(sp.read_text())\n"
            "    except Exception: s={}\n"
            "if not isinstance(s,dict): s={}\n"
            "if not s.get('skipDangerousModePermissionPrompt'):\n"
            "    s['skipDangerousModePermissionPrompt']=True\n"
            "    sp.write_text(json.dumps(s,indent=2))\n"
        )
        try:
            res = await runner.run(
                ["python3", "-c", seed_py, project_dir], timeout=20
            )
            if res.ok:
                _log(
                    f"tmux[{self.agent_name}]: seeded in-container claude trust "
                    f"for project {project_dir}"
                )
            else:
                _log(
                    f"tmux[{self.agent_name}]: in-container trust seed "
                    f"rc={res.returncode} "
                    f"stderr={res.stderr.decode('utf-8', 'replace').strip()[:200]!r} "
                    f"(non-fatal)"
                )
        except Exception as e:
            _log(
                f"tmux[{self.agent_name}]: in-container trust seed failed "
                f"(non-fatal): {e}"
            )

    def _build_session_name(self) -> str:
        """Tmux session name pattern: ``pinky-<agent_name>``.

        Prefix prevents collision with the operator's own tmux sessions.
        Plain ``agent_name`` if you wanted to attach without prefix; the
        prefix is the safer default.
        """
        return f"pinky-{self.agent_name}"

    # ── State ───────────────────────────────────────────────────────────

    @property
    def _inflight_meta(self) -> dict:
        """Back-compat view: routing metadata of the OLDEST in-flight turn.

        Pre-#560 this was a single mutable dict cell — the chokepoint the
        worker serialized dispatch around. Post-#560 the source of truth
        is ``_inflight_metas`` (FIFO deque); this property returns the
        OLDEST entry's meta (or ``{}`` when no turn is in flight) so
        pre-#560 tests + any external observers keep working without
        mass-rewrites. Returns a copy so callers can't mutate the deque
        through it.

        Production code (``_deliver_turn``, ``_handle_turn_complete``)
        operates on ``_inflight_metas`` directly. Do NOT introduce new
        readers of ``_inflight_meta`` — read the deque or its head.
        """
        if self._inflight_metas:
            return dict(self._inflight_metas[0].meta)
        return {}

    @_inflight_meta.setter
    def _inflight_meta(self, value: dict) -> None:
        """Back-compat setter for pre-#560 test fixtures.

        Old idiom:
            ``ss._inflight_meta = {"platform": ..., "chat_id": ..., "message_id": ...}``

        New equivalent: clear the deque, append one entry carrying
        ``value`` as its routing meta. ``ss._inflight_meta = {}`` clears
        the deque entirely.

        Production code does NOT use this setter — it goes through
        ``_inflight_metas.append`` directly in ``_deliver_turn``. The
        setter exists only so pre-#560 test fixtures don't need a
        sed-rewrite. New tests should populate the deque explicitly.
        """
        self._inflight_metas.clear()
        self._head_started_at = None
        if value:
            # Synthesize a minimal _QueuedTurn for the entry's ``turn``
            # field — tests using this setter don't care about replay
            # semantics, only routing-meta reads.
            synthetic = _QueuedTurn(
                prompt="",
                platform=value.get("platform", ""),
                chat_id=value.get("chat_id", ""),
                message_id=value.get("message_id", ""),
            )
            self._inflight_metas.append(_InflightMeta(
                meta=dict(value),
                completion_event=None,
                internal=False,
                dispatched_at=time.time(),
                turn=synthetic,
            ))
            self._head_started_at = time.time()

    @property
    def state(self) -> SessionState:
        """Single source of truth — read from the embedded StateMachine.

        Same contract as StreamingSession post-PR3: lifecycle queries go
        through the state machine, no derived bool inference.
        """
        return self._state_machine.state

    @property
    def stats(self) -> dict:
        """Operational snapshot. Keeps the keys callers actually read."""
        # ``pending_responses`` counts ONLY undelivered queue backlog --
        # it is the key session_watchdog's require_backlog gate reads, and
        # an in-flight turn must not arm that outer watchdog: it has none
        # of ``_inflight_watchdog``'s liveness carve-outs (transcript
        # growth, recent background tasks, live_status floor), so counting
        # a running turn there would warn/auto-recover mid-turn on any
        # long turn. ``inflight_turns`` exposes the pasted-awaiting-stop
        # span separately for busy-state consumers (UI badge).
        return {
            **self._stats,
            "state": self.state.value,
            # Wall-clock epoch the current state was entered (grant time) — lets
            # the watchdog age stuck transitions precisely instead of sampling
            # (#206).
            "state_entered_at": self._state_machine.state_entered_at,
            "pending_responses": self._message_queue.qsize(),
            "inflight_turns": len(self._inflight_metas),
            "current_activity": self._current_activity,
            "current_thinking": self._current_thinking,
            "activity_log": list(self._activity_log[-20:]),
            "account": self.account_info,
            "thinking_effort": self.effective_effort,
            # cost_usd intentionally absent — see module docstring.
        }

    @property
    def effective_effort(self) -> str:
        """Resolved thinking effort. ``auto`` is never returned (matched
        to ``Transport.effective_effort`` contract)."""
        level = self._effort_override or self._config.thinking_effort or "medium"
        if level == "auto":
            return "medium"
        return level

    async def _emit_stream_event(self, event: dict) -> None:
        if not self._stream_event_callback:
            return
        try:
            result = self._stream_event_callback(event)
            if asyncio.iscoroutine(result):
                await result
        except Exception as e:
            _log(f"tmux[{self.agent_name}]: stream_event_callback raised: {e}")

    async def record_tool_use_start(
        self,
        *,
        tool_use_id: str,
        tool_name: str,
        tool_input: dict,
    ) -> None:
        """Record a tool-call start (task #93).

        Called by the PreToolUse hook via
        ``POST /agents/{name}/transport/tool-use``. Mirrors what
        ``StreamingSession`` does in-band for SDK agents:

        - Update ``_current_activity`` so live status surfaces show
          which tool the agent is running right now.
        - Append a human-readable line to ``_activity_log``.
        - Open an analytics row via ``start_tool_call`` (PII-safe —
          only arg KEYS are recorded, not values).
        - Emit a ``tool_use_start`` stream event for SSE consumers.

        ``tool_use_id`` is Claude Code's per-call identifier — used
        as the analytics key so the later ``record_tool_use_finish``
        can close it out.

        Fire-and-forget semantics: failures are logged but never
        propagate to the caller (the hook is wrapped in ``|| true``
        anyway, but we'd rather have telemetry than no telemetry).
        """
        if not tool_name:
            return

        # #731: mark this tool call in-flight so the inflight watchdog doesn't
        # mistake a long foreground tool call (e.g. a blocking `gh run watch`)
        # for a wedged REPL. Cleared by record_tool_use_finish; bounded by
        # _FOREGROUND_TOOL_ACTIVE_CEILING_SEC in the verdict so a lost
        # finish-POST can't extend the window forever.
        if tool_use_id:
            self._inflight_tool_calls[tool_use_id] = time.time()

        # Human-readable activity line — mirror SDK by importing the
        # shared describer if available, falling back to a basic format.
        try:
            from pinky_daemon.streaming_session import _describe_tool_use
            desc = _describe_tool_use(tool_name, tool_input or {})
        except Exception:
            # Defensive fallback — keeps record_tool_use_start working
            # if streaming_session ever moves or renames the helper.
            desc = tool_name.rsplit("__", 1)[-1] if "__" in tool_name else tool_name
        self._current_activity = desc
        try:
            self._activity_log.append(desc)
        except Exception:
            # Activity log is best-effort UI plumbing — never let a
            # logging quirk break tool-use tracking. Real errors below
            # surface via _log calls in the analytics block.
            pass

        # Analytics: open a row keyed by tool_use_id (or a synthetic
        # key if the hook didn't see one — Claude Code's payload
        # always includes it for normal tool calls, but defending
        # against schema drift).
        call_key = tool_use_id or f"{tool_name}_{int(time.time() * 1000)}"
        tool_ns = ""
        if "__" in tool_name:
            parts = tool_name.split("__", 2)
            if len(parts) >= 3:
                tool_ns = parts[1]
        arg_keys: list[str] = []
        if isinstance(tool_input, dict):
            arg_keys = sorted(tool_input.keys())

        # Persist description alongside arg_keys so the chat UI can
        # rebuild the chip strip after a page refresh (otherwise these
        # only live in the transient tool_use_start SSE payload).
        start_meta: dict = {}
        if arg_keys:
            start_meta["arg_keys"] = arg_keys
        if desc:
            start_meta["description"] = desc

        if self._analytics_store:
            try:
                self._analytics_store.start_tool_call(
                    session_id=self.id,
                    agent_name=self.agent_name,
                    turn_seq=None,
                    tool_call_key=call_key,
                    tool_name=tool_name,
                    tool_namespace=tool_ns,
                    metadata=start_meta or None,
                )
            except Exception as e:
                _log(
                    f"tmux[{self.agent_name}]: analytics tool start "
                    f"failed: {e}"
                )

        await self._emit_stream_event(
            {
                "type": "tool_use_start",
                "agent_name": self.agent_name,
                "tool_use_id": call_key,
                "tool_name": tool_name,
                "tool_namespace": tool_ns,
                "arg_keys": arg_keys,
                "description": desc,
            }
        )

    async def record_tool_use_finish(
        self,
        *,
        tool_use_id: str,
        tool_name: str = "",
        is_error: bool = False,
        tool_response: object = None,
    ) -> None:
        """Record a tool-call result (task #93).

        Called by the PostToolUse hook via
        ``POST /agents/{name}/transport/tool-result``. Closes the
        analytics row opened by ``record_tool_use_start`` and emits
        a ``tool_use_finish`` stream event with a short result snippet
        (capped — same 200-char cap SDK uses).

        Tolerates a missing ``tool_use_id`` (some Claude Code event
        flows omit it for synthetic tool calls); the analytics close
        is skipped in that case but the stream event still fires so
        UI consumers see the finish signal.
        """
        if not tool_name and not tool_use_id:
            return

        # #731: this tool call is done — drop it from the in-flight set so the
        # watchdog stops extending the wedge window on its behalf.
        if tool_use_id:
            self._inflight_tool_calls.pop(tool_use_id, None)

        # Short result snippet for the stream event — same cap SDK
        # uses for parity. Tool responses can be huge (file contents,
        # search results); never emit the full payload.
        result_preview = ""
        if tool_response is not None:
            try:
                if isinstance(tool_response, str):
                    result_preview = tool_response[:200]
                else:
                    import json as _json
                    result_preview = _json.dumps(
                        tool_response, default=str
                    )[:200]
            except Exception:
                result_preview = str(tool_response)[:200]

        # Persist result_preview so the chat UI's chip strip can show
        # the truncated tool output after a page refresh. The same
        # 200-char snippet that the live tool_use_finish SSE event
        # carries — no new PII surface.
        finish_meta: dict = {}
        if result_preview:
            finish_meta["result_preview"] = result_preview

        if tool_use_id and self._analytics_store:
            try:
                self._analytics_store.finish_tool_call(
                    session_id=self.id,
                    agent_name=self.agent_name,
                    tool_call_key=tool_use_id,
                    success=not is_error,
                    error_type="tool_error" if is_error else "",
                    metadata=finish_meta or None,
                )
            except Exception as e:
                _log(
                    f"tmux[{self.agent_name}]: analytics tool finish "
                    f"failed: {e}"
                )

        await self._emit_stream_event(
            {
                "type": "tool_use_finish",
                "agent_name": self.agent_name,
                "tool_use_id": tool_use_id,
                "tool_name": tool_name,
                "is_error": is_error,
                "result_preview": result_preview,
            }
        )

    def set_effort(self, level: str) -> None:
        """Accept the call for protocol parity. tmux's claude REPL doesn't
        honor mid-session effort changes — log a warning and stash the
        value. A force_restart picks it up on the relaunched REPL."""
        valid = set(EFFORT_LEVELS)
        if level not in valid:
            raise ValueError(
                f"invalid effort {level!r}; expected one of {sorted(valid)}"
            )
        self._effort_override = None if level == "auto" else level
        _log(
            f"tmux[{self.agent_name}]: set_effort({level!r}) stashed — "
            f"takes effect on next force_restart (REPL relaunch)"
        )

    def clear_effort_override(self) -> None:
        self._effort_override = None

    # ── Lifecycle methods ───────────────────────────────────────────────

    async def connect(self, *, trigger: Trigger = Trigger.BROKER) -> None:
        """Bring the tmux session up via the appropriate state-machine path.

        Handles three entry states explicitly — each drives a different
        matrix edge:

        1. **Cold-start** (state ∈ {UNINITIALIZED, BOOTING}):
           ``UNINITIALIZED → BOOTING → CONNECTED|DEAD`` via the
           ``BOOT / BOOT_COMPLETE / BOOT_FAILED`` Trigger triplet.
           The ``trigger`` argument is ignored — BOOT is mandatory by
           matrix (the only legal trigger out of UNINITIALIZED).
        2. **Warm-wake** (state ∈ {IDLE_SLEEPING, DEAD}):
           ``IDLE_SLEEPING|DEAD → RECONNECTING → CONNECTED|DEAD`` via
           the caller-supplied ``trigger`` (BROKER for auto-wake on
           inbound, WATCHDOG for watchdog-driven wake, SCHEDULER for
           cron-driven wake, API_ADMIN for explicit operator wake).
           ``Trigger.INTERNAL`` is NOT legal for this edge — the matrix
           pins it to external actors (Murzik's PR #495 round-1
           finding 1 + 2).
        3. **No-op** (state == CONNECTED): silently return. This is the
           post-completion-straggler case (Pushok's Case C from PR6);
           pre-existing across StreamingSession + CodexSession + here.
           Tracked alongside the warm-reconnect Trigger symmetry
           follow-up.

        Cold-start + warm-wake both use the same in-flight subscriber
        protection — concurrent ``connect()`` calls on a fresh or sleeping
        session result in exactly one tmux spawn; concurrent callers
        subscribe and inherit the owner's outcome (CONNECTED clean return,
        DEAD raise).

        Args:
            trigger: Actor identity for the IDLE_SLEEPING|DEAD →
                RECONNECTING edge. Ignored for cold-start (BOOT is the
                only legal trigger). Default ``BROKER`` — the most
                common caller (auto-wake on inbound message).
        """
        cold_start_token = None
        warm_wake_token = None

        if self.state in (SessionState.UNINITIALIZED, SessionState.BOOTING):
            # ── Cold-start path ───────────────────────────────────────
            boot_result = await self._state_machine.request_transition(
                SessionState.BOOTING,
                Trigger.BOOT,
                reason="cold_start_handshake",
            )
            if boot_result.owner_token is None:
                # Same-target BOOT in flight: subscribe + inherit outcome.
                # Surface DEAD as raise per PR6's failure-propagation
                # contract.
                if boot_result.in_flight_handle is not None:
                    final = await boot_result.in_flight_handle.wait()
                    if final == SessionState.CONNECTED:
                        return
                    raise RuntimeError(
                        f"tmux[{self.agent_name}]: cold-start BOOT in-flight "
                        f"resolved to {final.value} (owner failed); refusing "
                        f"to return as connected"
                    )
                # Post-DEAD rejection (Pushok's Case D): surface failure.
                _log(
                    f"tmux[{self.agent_name}]: BOOT rejected "
                    f"({boot_result.rejection_reason!r}) — refusing cold-start"
                )
                if self.state == SessionState.DEAD:
                    raise RuntimeError(
                        f"tmux[{self.agent_name}]: cold-start BOOT rejected "
                        f"post-DEAD (owner failed before we subscribed); "
                        f"refusing to return as connected"
                    )
                return
            cold_start_token = boot_result.owner_token

        elif self.state in (SessionState.IDLE_SLEEPING, SessionState.DEAD):
            # ── Warm-wake path (Murzik's #495 round-1 fix) ────────────
            # The matrix requires an external trigger (BROKER, WATCHDOG,
            # SCHEDULER, API_ADMIN) for IDLE_SLEEPING|DEAD → RECONNECTING.
            # INTERNAL is rejected here — that was the pre-fix bug:
            # connect() direct-mutated CONNECTED, bypassing the
            # RECONNECTING macro state and skipping subscriber protection
            # for concurrent wakes.
            wake_result = await self._state_machine.request_transition(
                SessionState.RECONNECTING,
                trigger,
                reason=f"warm_wake_from_{self.state.value}",
            )
            if wake_result.owner_token is None:
                # Same-target RECONNECTING in flight: subscribe.
                if wake_result.in_flight_handle is not None:
                    final = await wake_result.in_flight_handle.wait()
                    if final == SessionState.CONNECTED:
                        return
                    raise RuntimeError(
                        f"tmux[{self.agent_name}]: warm-wake RECONNECTING "
                        f"in-flight resolved to {final.value} (owner failed); "
                        f"refusing to return as connected"
                    )
                # Rejection (matrix said no, or post-completion race).
                _log(
                    f"tmux[{self.agent_name}]: warm-wake rejected "
                    f"({wake_result.rejection_reason!r}) — state={self.state.value}"
                )
                if self.state == SessionState.DEAD:
                    raise RuntimeError(
                        f"tmux[{self.agent_name}]: warm-wake rejected post-DEAD; "
                        f"refusing to return as connected"
                    )
                return
            warm_wake_token = wake_result.owner_token

        elif self.state == SessionState.CONNECTED:
            # ── No-op (post-completion straggler) ─────────────────────
            # Pre-existing class shared with StreamingSession + CodexSession.
            # Logged for visibility; no double-spawn.
            _log(
                f"tmux[{self.agent_name}]: connect() called while already "
                f"CONNECTED — no-op (post-completion straggler)"
            )
            return

        else:
            # state == RECONNECTING: another path (force_restart /
            # attempt_reconnect) owns this transition. connect() should
            # not be the entry point for that lifecycle.
            _log(
                f"tmux[{self.agent_name}]: connect() called with state="
                f"{self.state.value} — refusing (another path owns this "
                f"transition)"
            )
            return

        try:
            await self._spawn_tmux_repl()
        except BaseException:
            # Cold-start or warm-wake failed. Drive the in-flight transition
            # to DEAD with the correct completion trigger (BOOT_FAILED for
            # cold-start, INTERNAL for warm-wake — DEAD is always legal as
            # emergency exit, so trigger choice is for audit visibility).
            if cold_start_token is not None:
                try:
                    await self._state_machine.transition_complete(
                        cold_start_token,
                        SessionState.DEAD,
                        trigger=Trigger.BOOT_FAILED,
                    )
                except Exception as ce:
                    _log(
                        f"tmux[{self.agent_name}]: BOOT_FAILED completion "
                        f"raised after cold-start error: {ce}"
                    )
            elif warm_wake_token is not None:
                try:
                    await self._state_machine.transition_complete(
                        warm_wake_token,
                        SessionState.DEAD,
                        trigger=Trigger.INTERNAL,
                    )
                except Exception as ce:
                    _log(
                        f"tmux[{self.agent_name}]: warm-wake DEAD completion "
                        f"raised after spawn error: {ce}"
                    )
            raise

        # Wake-prompt orientation snapshot (PR for #543). Read the
        # launch-mode signals that ``_build_claude_cmd`` recorded on the
        # session during ``_spawn_tmux_repl``. We snapshot now (pre-
        # state-machine completion) for a stable read; the enqueue
        # happens after CONNECTED + worker startup below.
        _was_force_fresh_launch = self._last_launch_forced_fresh
        _had_prior_transcript_pre_spawn = self._last_launch_had_prior_transcript
        _restart_reason_snapshot = self._config.restart_reason

        # Spawn succeeded. Complete the appropriate in-flight transition.
        if cold_start_token is not None:
            # Cold-start: BOOTING → CONNECTED via BOOT_COMPLETE.
            await self._state_machine.transition_complete(
                cold_start_token,
                SessionState.CONNECTED,
                trigger=Trigger.BOOT_COMPLETE,
            )
        elif warm_wake_token is not None:
            # Warm-wake: RECONNECTING → CONNECTED via INTERNAL (the matrix
            # cell for the completion edge).
            await self._state_machine.transition_complete(
                warm_wake_token,
                SessionState.CONNECTED,
                trigger=Trigger.INTERNAL,
            )

        # NOTE: tailer startup moved into ``_spawn_tmux_repl`` (Pushok's
        # PR #496 round-2 Case 1' fix) so ``force_restart`` and
        # ``attempt_reconnect`` get the same composition. The REPL + tailer
        # come up as a unit; do not start the tailer here.

        # Ensure turn_done invariant: between dispatches, the event is
        # cleared. After a force_restart, the previous worker may have
        # set it just before dying; reset to the invariant baseline so
        # the first new dispatch's await blocks on THIS session's turns,
        # not a stale signal from the killed session.
        self._turn_done.clear()

        # Start the worker.
        if not self._worker_task or self._worker_task.done():
            self._worker_task = asyncio.create_task(self._message_worker())
        # Start the inflight watchdog (#560). Independent of the worker
        # so concurrent dispatch isn't bottlenecked behind a per-turn
        # ``_turn_done`` wait. Idle when ``_inflight_metas`` is empty.
        if not self._watchdog_task or self._watchdog_task.done():
            self._watchdog_task = asyncio.create_task(self._inflight_watchdog())

        # Fire resume-handle persistence callback (one-shot for tmux —
        # session name is stable from construction but the persistence
        # hook expects a "connected" signal).
        if self._on_resume_handle:
            try:
                await self._on_resume_handle(self.agent_name, self.resume_handle)
            except Exception as e:
                _log(f"tmux[{self.agent_name}]: resume_handle callback raised: {e}")

        # Wake-prompt assembly + enqueue (PR for #543, parent defect:
        # tmux had no wake-prompt path, so Saved State / current time /
        # active channels / ToolSearch reminder all silently dropped on
        # connect). Uses the shared ``build_wake_prompt`` builder so the
        # contract matches SDK exactly.
        #
        # Reason mapping (tmux-specific because tmux ``resume_handle``
        # is stable from construction and doesn't usefully discriminate
        # fresh-vs-resume, per Murzik's pointer):
        #   - ``force_fresh_context_once`` was honored      → CONTEXT_RESTART
        #   - ``restart_reason == "auto_restart"``          → AUTO_RESTART
        #   - prior transcript existed (warm reconnect)     → RESUME
        #   - else                                          → NEW_SESSION
        #
        # Delivery: ``_enqueue_internal_prompt`` with
        # ``wait_for_completion=False`` — the wake turn flows behind any
        # external work in queue order. The internal-prompt path skips
        # ``_inflight_meta`` and ``_response_callback`` (regression guard
        # against PR #496 round-1 Case 1 surfacing through this path).
        if _was_force_fresh_launch or _restart_reason_snapshot == "context_restart":
            _wake_reason = WakeReason.CONTEXT_RESTART
        elif _restart_reason_snapshot == "auto_restart":
            _wake_reason = WakeReason.AUTO_RESTART
        elif _had_prior_transcript_pre_spawn:
            _wake_reason = WakeReason.RESUME
        else:
            _wake_reason = WakeReason.NEW_SESSION

        # Clear restart_reason after consumption — matches SDK semantics.
        self._config.restart_reason = ""

        await self._enqueue_wake_prompt(_wake_reason)

        _log(
            f"tmux[{self.agent_name}]: connected, session={self._session_name}, "
            f"worker started, wake_reason={_wake_reason.value}"
        )

    async def _enqueue_wake_prompt(self, reason: WakeReason, *, front: bool = False) -> None:
        """Build + enqueue the orientation wake prompt for ``reason``
        (``wait_for_completion=False`` so it flows behind any queued
        external work, in queue order).

        Shared by ``connect()`` and ``force_restart()``. Before this was
        extracted, ``force_restart`` respawned the REPL but — unlike
        ``connect`` — never enqueued a wake prompt, so a watchdog-driven
        restart dropped the agent onto a blank session with no
        saved-state context (the "comes back idle / no anything"
        symptom Brad reported). Routing both paths through here keeps the
        re-prime behavior identical.

        ``front=True`` prepends the wake prompt at the queue HEAD ahead
        of any existing contents. ``force_restart`` uses this because the
        inflight watchdog requeues replay/backlog at the front of the
        queue before scheduling the restart; a trailing wake prompt would
        let the resumed REPL process user turns before orientation
        (Murzik #589 review). ``connect()`` uses the default tail enqueue
        — its bootstrap queue is empty so head == tail.

        The ``_skip_wake_prompt_for_tests`` seam short-circuits here so
        unit tests without a transcript-tailer simulation don't hang the
        worker on a never-completing wake turn.

        Enqueue failure is logged, never raised — a wake-prompt hiccup
        must not strand the session in CONNECTED-but-orientationless. It
        remains usable for external turns; the agent just lacks
        saved-state context until the next restart.
        """
        if self._skip_wake_prompt_for_tests:
            return
        # #591 — rebuild wake-context body with the freshly-computed
        # ``reason`` so the builder can gate the saved-state manifest
        # against the actual wake type (RESUME drops the bulk manifest
        # since ``claude --continue`` already loaded the conversation;
        # CONTEXT_RESTART/AUTO_RESTART/NEW_SESSION emit it). The static
        # ``self._config.wake_context`` was set at config-create time
        # (BEFORE the warm-vs-fresh decision is made) so reading it here
        # without rebuilding would re-emit a stale manifest on RESUME —
        # the exact symptom #591 was filed for. Falls back to the stored
        # body when no builder is wired (tests). Trailing positional
        # kwarg keeps legacy 1-arg builders working.
        wake_context_body = self._config.wake_context or ""
        if self._config.wake_context_builder:
            try:
                wake_context_body = self._config.wake_context_builder(
                    self.agent_name, reason
                )
            except TypeError:
                pass
            except Exception as e:
                _log(
                    f"tmux[{self.agent_name}]: wake context rebuild failed: {e} "
                    "— using stored body"
                )
        wake_prompt = build_wake_prompt(
            WakePromptInput(
                reason=reason,
                context_body=wake_context_body,
                timezone=self._config.timezone or "America/Los_Angeles",
            )
        )
        # #591 P1#2 (Murzik round-2): defer the on_wake_delivered fire
        # to AFTER the actual paste lands, not at enqueue success. The
        # paste happens later in ``_deliver_turn``; if it fails (context
        # lock deferral, paste_text returning not-ok) the prompt is
        # never shown to the agent, and firing the callback at enqueue
        # would advance the #591 cycle-gate boundary against a wake
        # that never reached the model — eating the directive on the
        # next RESUME. The closure carries the agent name + reason so
        # ``_deliver_turn`` can fire on paste-success without re-reading
        # them. ``None`` when no callback is wired (tests).
        _wake_delivered_cb: object = None
        if self._config.on_wake_delivered:
            _config_cb = self._config.on_wake_delivered
            _agent_name = self.agent_name
            _reason = reason

            def _wake_delivered_cb() -> None:  # type: ignore[no-redef]
                _config_cb(_agent_name, _reason)

        try:
            await self._enqueue_internal_prompt(
                wake_prompt,
                reason=f"wake_{reason.value}",
                wait_for_completion=False,
                front=front,
                on_delivered=_wake_delivered_cb,
            )
        except Exception as e:
            _log(
                f"tmux[{self.agent_name}]: wake prompt enqueue failed: {e} "
                f"(reason={reason.value}) — session remains CONNECTED"
            )

    async def _spawn_tmux_repl(self) -> None:
        """Spawn the tmux session and the in-pane claude REPL, then start
        the response tailer.

        Wrapped in cold-start timeout so a hung spawn fails to DEAD
        rather than parking the state machine indefinitely.

        Invariant (Pushok's PR #496 round-2 Case 1'): REPL + tailer come
        up as a unit — single source of truth for all callers (``connect``,
        ``force_restart``, ``attempt_reconnect``). Previously the tailer
        was started only by ``connect``, which left ``force_restart`` and
        ``attempt_reconnect`` with a dead tailer task → ``turn_done`` could
        never fire → worker timed out → another ``force_restart`` →
        death loop. Bundling here makes the contract structural rather
        than docstring-only.
        """
        cwd = self._config.working_dir or "."
        # Ensure cwd exists — claude --continue needs it.
        Path(cwd).mkdir(parents=True, exist_ok=True)

        # #638 (review-confirmed critical): take ONE strict registry snapshot
        # and RE-SELECT the execution seam from it on EVERY spawn. Session
        # objects survive isolation_mode flips (PUT /agents tears nothing
        # down; reconnect/restart/auto-wake reuse this object), so a runner
        # fixed at construction silently launches a flipped-to-container
        # agent UNISOLATED on the host (or podman-wraps a flipped-to-local
        # one into a stopped container). strict=True: a registry failure
        # raises → BOOT_FAILED, never a quiet local fallback.
        container_agent = self._container_agent(strict=True)
        self._tmux.set_command_runner(self._select_command_runner(container_agent))
        _log(
            f"tmux[{self.agent_name}]: claude_auth_mode={_claude_auth_mode(self.agent_name)} "
            f"container_agent={str(container_agent is not None).lower()}"
        )

        # Container agents: provision + start the container BEFORE any
        # `podman exec tmux …`. Deliberately OUTSIDE the 60s cold-start
        # umbrella below — this can include a multi-minute image pull and
        # runs under its own budget (see _ensure_container_started).
        await self._ensure_container_started(container_agent)

        # Pre-seed Claude Code's first-run trust/bypass flags (#112) so a
        # FRESH REPL doesn't wedge on the "trust this folder?" / "Bypass
        # Permissions mode" gates that --dangerously-skip-permissions does
        # NOT auto-accept. Idempotent + best-effort: a failure here must
        # never block the spawn (worst case is the pre-existing wedge, not
        # a regression). Resolve the config path against the effective env
        # the launched claude inherits (daemon env + our -e overrides).
        # Local agents seed the host's ~/.claude.json here. A container
        # agent's trust file is seeded in-container via `podman exec` inside
        # ``_spawn()`` below (the container is running by now).
        if container_agent is None:
            try:
                effective_env = {**os.environ, **self._build_repl_env()}
                cfg_path = _resolve_claude_config_path(effective_env)
                if _seed_claude_trust_file(cfg_path, cwd):
                    _log(
                        f"tmux[{self.agent_name}]: pre-seeded claude trust flags "
                        f"in {cfg_path} for project {cwd}"
                    )
            except Exception as e:
                _log(
                    f"tmux[{self.agent_name}]: claude trust pre-seed failed "
                    f"(non-fatal): {e}"
                )
        else:
            # Container agent: bootstrap Claude credentials into its
            # host-visible CLAUDE_CONFIG_DIR (one-time, best-effort) so the
            # in-container REPL starts authenticated. Host-side file copy —
            # no container required, so it runs before ensure_started.
            self._seed_container_claude_creds()

        # Pulse-v2 idle-prompt gate (task #92) re-arms on every fresh
        # spawn. The new REPL hasn't responded to anything yet, so the
        # next ``_deliver_turn`` must wait for its idle prompt before
        # pasting — even if a prior REPL on this session object had
        # ``_has_completed_turn = True``. force_restart / attempt_reconnect
        # both flow through here, so this is the structural reset point.
        self._has_completed_turn = False

        # If a stale session is left over from a previous daemon run (e.g.
        # crash without graceful disconnect), reap it. We're the cold-start
        # owner; reclaiming the name is safe.
        if await self._tmux.has_session():
            _log(
                f"tmux[{self.agent_name}]: stale session {self._session_name} "
                f"found, reaping before fresh spawn"
            )
            await self._tmux.kill_session()

        # Build the in-pane command. ``claude --continue`` resumes the
        # most-recent transcript for ``cwd``; falls back to fresh session
        # if none exists.
        claude_cmd = self._build_claude_cmd()
        env = self._build_repl_env()

        async def _spawn():
            # Container is up (started above, outside this umbrella): seed its
            # trust file and home-volume credentials (via `podman exec`)
            # before the REPL launches. No-ops for local agents.
            await self._seed_container_trust(cwd)
            await self._seed_container_home_creds()
            result = await self._tmux.new_session(
                cwd=cwd,
                command=claude_cmd,
                env=env,
            )
            if not result.ok:
                raise RuntimeError(
                    f"tmux new-session failed: rc={result.returncode} "
                    f"stderr={result.stderr.strip()!r}"
                )

        try:
            await asyncio.wait_for(_spawn(), timeout=_COLD_START_TIMEOUT_SEC)
        except asyncio.TimeoutError:
            # Reap whatever partial state we may have left.
            try:
                await self._tmux.kill_session()
            except Exception:
                # Best-effort cleanup; ignore failure since we're already
                # raising the cold-start timeout to the caller.
                pass
            raise RuntimeError(
                f"tmux[{self.agent_name}]: cold-start timed out after "
                f"{_COLD_START_TIMEOUT_SEC}s"
            ) from None

        # NOTE: ``force_fresh_context_once`` consumption is deferred to
        # the end of this method (after tailer startup also succeeds),
        # NOT here — see the load-bearing comment at the consume site
        # below for the full rationale (Murzik #545 follow-up round 2).

        # REPL is up — bring up the response capture pipeline (PR8b).
        # Kept OUTSIDE the cold-start timeout so tailer construction
        # (which stats the project dir to guess the transcript path)
        # can't get killed mid-flight and leave a partial state. On
        # tailer-start failure we roll back the spawn — the REPL is
        # unusable without response capture, and callers expect the
        # symmetric "spawn raised → caller transitions DEAD" semantics.
        try:
            await self._start_tailer()
        except Exception:
            # Murzik's PR #496 round-3 cleanup-hole fix: if _start_tailer
            # raises AFTER constructing self._tailer but before/during
            # the await on start(), we'd otherwise transition DEAD with
            # a live orphan tailer instance. Stop the partial tailer +
            # null the slot before re-raising, so the caller sees a
            # clean state. Symmetric with the tmux kill below.
            try:
                await self._stop_tailer()
            except Exception:
                pass
            self._tailer = None
            try:
                await self._tmux.kill_session()
            except Exception:
                # Best-effort cleanup; ignore failure since we're already
                # re-raising the tailer-start error to the caller.
                pass
            raise

        # REPL + tailer are both up as a unit — NOW it's safe to
        # consume the one-shot ``force_fresh_context_once`` flag
        # (Murzik #545 follow-up round 2). Clearing earlier (right
        # after ``_spawn()`` returned) would still lose the fresh-
        # context guarantee on retry if tailer startup then failed
        # and rolled back the whole launch. The invariant pinned here:
        # the flag remains set until launch (REPL + tailer) succeeds
        # as a complete unit.
        if self._last_launch_forced_fresh:
            self._config.force_fresh_context_once = False

        # Auth-relay (#205): if enabled + configured, start a flag-gated
        # background watcher that detects the claude OAuth login wall and relays
        # it to the owner. The normal path is byte-identical when off. Cancel a
        # watcher left over from a prior spawn on this reused session object.
        if _auth_relay.enabled() and _auth_relay.configured:
            prev = self._auth_watcher_task
            if prev is not None and not prev.done():
                prev.cancel()
            self._auth_watcher_task = asyncio.create_task(
                self._watch_for_oauth_url()
            )

    async def _watch_for_oauth_url(self) -> None:
        """Watch the pane for the claude OAuth login wall and relay it (#205).

        Flag-gated; started at the end of ``_spawn_tmux_repl`` only when the
        auth relay is enabled + configured. Pure read-only observation
        (``capture_pane``) — it never pastes a turn, so the inflight watchdog
        never sees an aging head and the session stays CONNECTED for as long as
        the owner needs to reply. Bounded to a short window after spawn: the
        wall (if any) appears within seconds; if it never shows, the session
        authenticated normally and the watcher exits.
        """
        deadline = time.monotonic() + _AUTH_WALL_DETECT_WINDOW_SEC
        try:
            while time.monotonic() < deadline:
                if self.state != SessionState.CONNECTED:
                    return
                res = await self._tmux.capture_pane(lines=40, join=True)
                text = res.stdout if res.ok and res.stdout else ""
                url = extract_oauth_url(text)
                if url:
                    await self._relay_login_and_inject(url)
                    return
                await asyncio.sleep(_AUTH_WALL_POLL_SEC)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            _log(
                f"tmux[{self.agent_name}]: auth-relay watcher error "
                f"(non-fatal): {e}"
            )

    async def _relay_login_and_inject(self, url: str) -> None:
        """Relay the OAuth URL to the owner, await the code, inject it (#205).

        Never logs the code. On success notifies the owner the agent is signed
        in; on a rejected code or relay timeout, tells them to restart.
        """
        _log(
            f"tmux[{self.agent_name}]: claude OAuth login wall detected — "
            f"relaying sign-in link to owner"
        )
        try:
            code = await _auth_relay.open(self.agent_name, url)
        except asyncio.TimeoutError:
            _log(f"tmux[{self.agent_name}]: auth relay expired before a code arrived")
            return
        except Exception as e:
            _log(f"tmux[{self.agent_name}]: auth relay failed (non-fatal): {e}")
            return

        # Inject the owner-supplied code via bracketed paste (never logged).
        await self._tmux.paste_text(code, enter=True)
        _log(f"tmux[{self.agent_name}]: injected owner-supplied auth code")

        # Give claude a moment to complete login, then confirm the wall cleared.
        await asyncio.sleep(_AUTH_LOGIN_SETTLE_SEC)
        try:
            res = await self._tmux.capture_pane(lines=40, join=True)
            text = res.stdout if res.ok and res.stdout else ""
        except Exception:
            text = ""
        if looks_like_login_wall(text):
            _log(
                f"tmux[{self.agent_name}]: login wall still present after code "
                f"— likely rejected"
            )
            await _auth_relay.notify_owner(
                self.agent_name,
                f'That code did not complete the sign-in for "{self.agent_name}". '
                f"Restart the session to try again.",
            )
        else:
            _log(f"tmux[{self.agent_name}]: claude sign-in completed")
            await _auth_relay.notify_owner(
                self.agent_name,
                f'Agent "{self.agent_name}" is signed in to Claude.',
            )

    def _build_claude_cmd(self) -> str:
        """Build the in-pane ``claude`` invocation as a single shell string.

        Returned as a string (not a list) because tmux invokes it via the
        user's shell. Components are individually quoted with
        ``shlex.quote`` to defend against agent-name / config injection.

        ``--continue`` is gated on a prior transcript existing for this
        agent's cwd (issue #511). Otherwise the Claude CLI exits 1
        ("no conversation found to continue"), the detached tmux session
        is auto-reaped on command exit, and the Python state machine ends
        up CONNECTED against a dead REPL. Cold-starting a fresh agent
        must fall through to ``claude`` (no ``--continue``) so a new
        transcript is created on the first turn; subsequent reconnects
        will find that transcript and resume normally.

        **Fresh-context suppression** (PR for #543): callers that need
        to force a fresh conversation (e.g. ``/streaming/restart``,
        ``context_restart`` MCP tool) set
        ``config.force_fresh_context_once = True``. This launch will
        skip ``--continue`` even when a prior transcript exists,
        producing a fresh Claude Code session. The flag is one-shot —
        consumed here and reset to False so the next spawn behaves
        normally. This is a separate contract from ``restart_reason``,
        which controls the wake-prompt TEXT; coupling them was the
        root cause of #543 (tmux context_restart silently resumed the
        old transcript because we only checked transcript existence).
        """
        # Resolve launch mode. The flag is one-shot ("next launch only,"
        # not "every launch from now on") but consumption happens in
        # ``_spawn_tmux_repl`` AFTER ``_spawn()`` returns successfully —
        # NOT here. Why: Murzik's #545 review caught that consuming the
        # flag during command-build means a failed spawn + retry would
        # silently lose the fresh-context guarantee and resume with
        # ``--continue`` while still emitting context_restart wake copy.
        # By deferring the consume, a retry sees the flag still set
        # and honors it again. See ``_spawn_tmux_repl`` for the clear.
        force_fresh = bool(getattr(self._config, "force_fresh_context_once", False))
        has_prior = self._has_prior_transcript()
        use_continue = has_prior and not force_fresh

        # Record launch mode on the session so ``connect()`` can derive
        # the wake reason post-spawn (force_fresh / restart_reason both
        # influence orientation copy). Read-only afterward.
        self._last_launch_used_continue = use_continue
        self._last_launch_forced_fresh = force_fresh
        self._last_launch_had_prior_transcript = has_prior

        parts = ["claude"]
        if use_continue:
            parts.append("--continue")
        parts.append("--dangerously-skip-permissions")
        # Optional model override.
        if self._config.model:
            parts.extend(["--model", self._config.model])
        # Thinking effort (#151). tmux historically never passed --effort, so a
        # configured effort was only hook-detected, never actually applied.
        # Mirror the SDK contract — set --effort for any explicit non-medium
        # level. ultracode resolves to xhigh because the CLI flag rejects the
        # literal "ultracode" (it's only reachable via interactive /effort);
        # the workflow-orchestration half is carried by ULTRACODE_DIRECTIVE in
        # the system prompt.
        cli_effort = resolve_cli_effort(self.effective_effort)
        if cli_effort and cli_effort not in ("medium", "auto"):
            parts.extend(["--effort", cli_effort])

        # #151 native ultracode activation. ultracode boots at --effort xhigh
        # (above) because the CLI flag rejects the literal "ultracode". The
        # real tier — xhigh + the CLI's own standing dynamic-workflow
        # system-reminder — is reachable ONLY via the interactive
        # ``/effort ultracode``. Arm a one-shot so ``_deliver_turn`` types it
        # into the ready REPL BEFORE the first prompt pastes (Brad's ordering:
        # spawn → change effort → inject wake context). FRESH launches only:
        # a ``--continue`` reconnect already carries conversation context,
        # where ``/effort`` trips the mid-session "Change effort level?"
        # confirmation (the prompt-cache full re-read). On a fresh spawn the
        # input area is empty, so the CLI sets effort silently. Re-armed every
        # build so a failed-spawn retry doesn't lose the activation;
        # ULTRACODE_DIRECTIVE remains the fallback if the keystroke send fails
        # or on a CLI predating native ultracode.
        self._native_ultracode_pending = (not use_continue) and is_ultracode(
            self.effective_effort
        )

        cmd = " ".join(shlex.quote(p) for p in parts)

        # Instrumentation: typed launch-mode log so validation tooling
        # can grep for `claude_cmd_mode=fresh` after a context_restart
        # to confirm the suppress-continue contract held.
        _log(
            f"tmux[{self.agent_name}]: claude_cmd_built "
            f"mode={'continue' if use_continue else 'fresh'} "
            f"force_fresh={force_fresh} "
            f"prior_transcript={has_prior}"
        )
        return cmd

    def _forward_oauth_enabled(self) -> bool:
        """Whether static OAuth-token forwarding is enabled (#780).

        Flag-gated (``PINKY_FORWARD_OAUTH_TOKEN``, default OFF) for staged
        rollout/soak. This is the operator's *intent* signal: when ON, the
        fleet is meant to authenticate via a long-lived static token, so the
        refresh-prone ``.credentials.json`` container seed is suppressed
        REGARDLESS of whether the token is currently set — a misconfig (flag
        on, token missing) must fail CLOSED (a loud login wall) rather than
        silently fall back to the shared refresh-token file (Murzik #781 P2).
        """
        return os.environ.get("PINKY_FORWARD_OAUTH_TOKEN", "0").strip().lower() in (
            "1", "true", "yes", "on",
        )

    def _static_oauth_token(self) -> str:
        """The long-lived ``CLAUDE_CODE_OAUTH_TOKEN`` to inject into this
        session's env, or ``""`` when forwarding is inactive/withheld (#780).

        A ``claude setup-token`` token (``sk-ant-oat01-…``, ~1yr, NEVER
        refreshed) authenticates without ever touching the single-use OAuth
        refresh token in ``.credentials.json`` — eliminating the shared-creds
        refresh race that de-auths a fleet on concurrent cold-start. The #777
        cold-start serialization only narrows that window; the in-REPL refresh
        still races, so a static token is the durable fix.

        Withheld for custom-provider agents — keyed on ``provider_url`` OR
        ``provider_key`` (Murzik #781 P1): provider resolution can yield
        ``(url, "", model)`` (a non-default base URL with an EMPTY key), and a
        first-party Claude subscription token must NEVER be presented to a
        gateway / custom base URL, even when no key is set.
        """
        if not self._forward_oauth_enabled():
            return ""
        if (self._config.provider_url or "").strip() or self._config.provider_key:
            return ""
        return os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "").strip()

    def _build_repl_env(self) -> dict[str, str]:
        """Env vars injected into the tmux session.

        Mirrors StreamingSession's ``provider_env`` shape so hook scripts
        (e.g. ``hook_verify_effort.py``) see the same signals on both
        backends.

        **#515 follow-up: PINKY_SESSION_SECRET propagation.** Tmux
        ``new-session`` only propagates env vars listed via ``-e
        KEY=VAL``; parent-process env is dropped except for the small
        ``update-environment`` allowlist (DISPLAY, SSH_*, etc.). Without
        explicit propagation, every PinkyBot-managed hook
        (``hook_idle.py``, ``hook_working.py``, ``hook_verify_effort.py``,
        ``hook_tmux_wake.py``, ``hook_tmux_session_start.py``) hits the
        guard ``if not secret: sys.exit(0)`` and silently no-ops. That
        broke #515 (tailer never repoints from placeholder), and also
        breaks tmux-agent presence updates, effort-drift logging, and
        Stop-hook wakeups across the whole hook fleet. SDK agents are
        unaffected because claude inherits daemon env via subprocess.

        Propagating the secret here re-enables the entire hook fleet
        for tmux agents without touching any individual hook script.
        """
        env: dict[str, str] = {}
        if self._config.provider_url:
            env["ANTHROPIC_BASE_URL"] = self._config.provider_url
        if self._config.provider_key:
            env["ANTHROPIC_API_KEY"] = self._config.provider_key
            env["ANTHROPIC_AUTH_TOKEN"] = self._config.provider_key
        # Static OAuth token forwarding (#780): inject a long-lived, never-
        # refreshed CLAUDE_CODE_OAUTH_TOKEN so claude authenticates with it
        # instead of the single-use refresh token in .credentials.json (no
        # refresh ⇒ no shared-creds de-auth race). ESSENTIAL for container
        # agents — their isolated env does NOT inherit the daemon env, so
        # without this -e the token never reaches them; local tmux agents get
        # it via tmux-server inheritance, but forwarding makes it explicit and
        # uniform. Flag-gated + provider-guarded inside _static_oauth_token.
        oauth_token = self._static_oauth_token()
        if oauth_token:
            env["CLAUDE_CODE_OAUTH_TOKEN"] = oauth_token
        if self.agent_name:
            env["PINKY_AGENT_NAME"] = self.agent_name
        # Surface the RESOLVED effort (#151): the drift hook compares this to
        # the runtime $CLAUDE_EFFORT, which reports xhigh under ultracode — so
        # expect xhigh, not the literal "ultracode", to avoid false drift.
        effort = resolve_cli_effort(self.effective_effort)
        if effort:
            env["PINKY_EXPECTED_EFFORT"] = effort
        if self._config.strict_effort_enforcement:
            env["PINKY_STRICT_EFFORT"] = "1"
        # PINKY_AGENT_KEY (#623 increment 2) — this agent's per-agent signing
        # key. Provisioned so hook scripts running in this tmux session sign
        # internal requests with a non-forgeable identity. Lookup guarded like
        # _restart_threshold_pct — a registry hiccup must not break session env.
        agent_key = ""
        if self._registry and self.agent_name:
            try:
                agent_key = (self._registry.get_signing_key(self.agent_name) or "").strip()
            except Exception:
                agent_key = ""
        if agent_key:
            env["PINKY_AGENT_KEY"] = agent_key

        # Container agents (#638): every PinkyBot hook script POSTs to the
        # daemon at PINKY_DAEMON_URL (default http://localhost:8888) — but
        # inside the container netns, localhost is the CONTAINER, so without
        # this the whole hook fleet (Stop-hook wakes, SessionStart transcript
        # reporting, live status, tool telemetry) silently no-ops and the
        # response pipeline never fires. host.containers.internal is wired
        # via --add-host at container create (ContainerProvisioner).
        if self._container_agent() is not None:
            env["PINKY_DAEMON_URL"] = os.environ.get(
                "PINKY_CONTAINER_DAEMON_URL", "http://host.containers.internal:8888"
            )

        # PINKY_SESSION_SECRET — the daemon-wide secret. Read from os.environ
        # rather than a config field because the daemon's own SDK clients and
        # FastAPI middleware read it from the same env var. Empty/missing is
        # tolerated: hooks already handle that gracefully (silent no-op).
        #
        # #149 phase-3 security gate (fail CLOSED — Murzik #639 review): the
        # global secret is the fleet-wide signing key; the daemon dual-accepts
        # it for EVERY agent name, so any child that holds it can sign internal
        # requests AS ANY OTHER AGENT. Inject it ONLY when the agent is *proven*
        # non-isolated. Withhold it whenever:
        #   - the agent is isolated — with a per-agent key it signs as itself;
        #     WITHOUT one it is a provisioning failure, so omit BOTH and let
        #     hooks/MCP no-op rather than hand a sandbox the forgeable secret
        #     (fail closed, not degraded-available); or
        #   - isolation can't be proven (registry unwired/errored) AND a
        #     per-agent key is present — the key already gives a working
        #     identity, and registry uncertainty must not cause secret exposure
        #     (same fail-open class as #635).
        # The only paths that still receive the global secret are proven
        # non-isolated agents and the legacy/dev "unknown + no key" case (an
        # agent with no key genuinely needs the shared secret to sign at all).
        secret = os.environ.get("PINKY_SESSION_SECRET", "").strip()
        status = self._isolation_status()
        if status == "isolated":
            if agent_key:
                _log(
                    f"tmux[{self.agent_name}]: isolated — per-agent key only, "
                    f"global secret withheld"
                )
            else:
                _log(
                    f"tmux[{self.agent_name}]: ERROR isolated agent has no per-agent "
                    f"signing key — withholding global secret too (hooks/MCP will "
                    f"no-op); provision a key to restore signing"
                )
        elif status == "unknown" and agent_key and secret:
            _log(
                f"tmux[{self.agent_name}]: isolation status unknown but per-agent "
                f"key present — withholding global secret (fail closed)"
            )
        elif secret:
            env["PINKY_SESSION_SECRET"] = secret
        return env

    async def disconnect(self) -> None:
        """Tear down the worker and kill the tmux session. Idempotent.

        Per the Transport contract: ``disconnect`` is the side-effect
        runner, NOT the intent declarer. Callers establish lifecycle
        intent (idle_sleep / force_restart / explicit DEAD) by driving
        the state machine BEFORE calling disconnect.
        """
        # Cancel worker.
        if self._worker_task and not self._worker_task.done():
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
        self._worker_task = None
        # Cancel watchdog (#560). Mirrors the worker shutdown — must be
        # before the deque drain so it doesn't race a force_restart it
        # may have just scheduled.
        if self._watchdog_task and not self._watchdog_task.done():
            self._watchdog_task.cancel()
            try:
                await self._watchdog_task
            except asyncio.CancelledError:
                pass
        self._watchdog_task = None
        # Cancel the auth-relay watcher (#205) alongside the worker/watchdog.
        if self._auth_watcher_task and not self._auth_watcher_task.done():
            self._auth_watcher_task.cancel()
            try:
                await self._auth_watcher_task
            except asyncio.CancelledError:
                pass
        self._auth_watcher_task = None
        self._processing = False

        # Drain the in-flight metadata deque (#560 replaces PR #496
        # round-2's single-dict clear). Critical safety: unblock every
        # pending ``completion_event`` BEFORE clearing the deque, so a
        # ``wait_for_completion=True`` caller (e.g. pre-sleep save)
        # doesn't hang forever when its turn is abandoned by a
        # disconnect / force_restart cycle. Murzik review point #2.
        #
        # Also defends #496 round-1 Case 2: a straggler stop_hook_summary
        # read from a stale transcript on reconnect can't route a late
        # response — the deque it would popleft from is empty.
        drained = list(self._inflight_metas)
        self._inflight_metas.clear()
        self._head_started_at = None
        # #731: session is being torn down — drop in-flight tool state so a
        # stale entry can't leak across the disconnect/reconnect boundary.
        self._inflight_tool_calls.clear()
        for entry in drained:
            if entry.completion_event is not None and not entry.completion_event.is_set():
                entry.completion_event.set()

        # Issue #547: also unblock ``_inflight_turn`` — the turn the
        # worker pulled from the queue but had NOT yet pasted (e.g.
        # mid context-lock retry, or worker cancelled before
        # _deliver_turn ran). Its meta isn't in the deque yet, so the
        # drain loop above missed it. Without this, an unbounded
        # ``wait_for_completion=True`` caller hangs forever when its
        # internal turn is interrupted pre-paste.
        if self._inflight_turn is not None:
            evt = self._inflight_turn.completion_event
            if evt is not None and not evt.is_set():
                evt.set()
            self._inflight_turn = None

        # Stop the response tailer (PR8b). Tailer instance is retained
        # so stats/path persist; only the background task is cancelled.
        await self._stop_tailer()

        # Kill tmux session. ``kill_session`` is idempotent (treats
        # "can't find session" as ok).
        try:
            await self._tmux.kill_session()
        except Exception as e:
            _log(f"tmux[{self.agent_name}]: kill_session raised: {e}")

        # Default disconnect (no prior intent set) → DEAD. The state
        # machine's existing matrix already handles the CONNECTED → DEAD
        # cell under INTERNAL. If a prior intent already mutated state
        # (IDLE_SLEEPING, RECONNECTING), this is a no-op — we don't drive
        # the transition again.
        if self.state == SessionState.CONNECTED:
            try:
                result = await self._state_machine.request_transition(
                    SessionState.DEAD,
                    Trigger.INTERNAL,
                    reason="disconnect_default",
                )
                if result.owner_token is not None:
                    await self._state_machine.transition_complete(
                        result.owner_token,
                        SessionState.DEAD,
                        trigger=Trigger.INTERNAL,
                    )
            except Exception as e:
                _log(f"tmux[{self.agent_name}]: disconnect→DEAD raised: {e}")

        _log(f"tmux[{self.agent_name}]: disconnected")

    async def send(
        self,
        prompt: str,
        *,
        platform: str = "",
        chat_id: str = "",
        message_id: str = "",
        agent_hint: str = "",
    ) -> None:
        """Queue a turn for delivery to the in-pane claude REPL.

        Non-blocking. Callers must ensure ``state == CONNECTED`` before
        calling (per Transport contract). Behavior when called while
        non-CONNECTED: drop with a log line (matches StreamingSession's
        legacy behavior).
        """
        if self.state != SessionState.CONNECTED:
            _log(
                f"tmux[{self.agent_name}]: not connected (state={self.state.value}), "
                f"dropping message"
            )
            return

        self.last_active = time.time()
        self._stats["messages_sent"] += 1

        # Log to conversation store BEFORE appending agent_hint so chat
        # history doesn't contain the routing hint.
        if self._conversation_store:
            try:
                self._conversation_store.append(
                    self.id, "user", prompt,
                    platform=platform, chat_id=chat_id,
                )
            except Exception:
                pass

        queued_prompt = prompt + agent_hint if agent_hint else prompt
        await self._message_queue.put(_QueuedTurn(
            prompt=queued_prompt,
            platform=platform,
            chat_id=chat_id,
            message_id=message_id,
        ))
        _log(f"tmux[{self.agent_name}]: queued message (chat={chat_id})")

    async def _enqueue_internal_prompt(
        self,
        prompt: str,
        *,
        reason: str,
        wait_for_completion: bool = False,
        timeout_sec: float | None = None,
        front: bool = False,
        on_delivered: object = None,
    ) -> None:
        """Queue a daemon-internal prompt with no external-side-effects.

        Differences vs ``send()``:

        - **No conversation_store append** — the prompt is daemon-internal
          (wake orientation, pre-sleep save reminder, etc.), not a user
          message.
        - **No ``messages_sent`` increment** — external-message stats stay
          accurate for analytics / dashboards.
        - **No ``_inflight_meta`` writes** — wake prompts have no chat
          routing, and writing here would clobber a back-to-back external
          turn's routing metadata (regression guard for PR #496 round-1
          Case 1 surfacing through this path).
        - **No ``_response_callback`` invocation** — there's no chat to
          deliver the response back to. The agent's response is captured
          in the transcript JSONL and counted toward ``stats["turns"]``
          like any other turn.

        ``wait_for_completion=False`` (default): fire-and-forget. Returns
        immediately. Used by wake prompts at ``connect()`` time — the
        session is starting and external work can flow behind the wake
        turn in queue order.

        ``wait_for_completion=True``: await the queued turn's completion
        before returning. Used by pre-sleep save prompts where the caller
        must not progress (e.g. disconnect) until the agent has honored
        the instruction. Bounded by ``timeout_sec`` if provided — raises
        ``asyncio.TimeoutError`` on timeout.

        Always returns ``None``. (Earlier drafts suggested returning the
        completion event for lazy observation in fire-and-forget mode,
        but the lazy-observe pattern isn't used by any current caller
        and adds a footgun — the event would only be set when the
        worker reaches that turn, which may be after several other
        turns drain. Callers needing post-hoc completion signal must
        opt into ``wait_for_completion=True`` and accept the inline
        block. Murzik #545 follow-up.)

        Connection state: behaves like ``send()`` — drops with a log line
        if the session is not CONNECTED. Cold-start callers (``connect``)
        invoke this immediately after the state machine reports
        CONNECTED, so the gate passes.

        **Wake-prompt readiness gate (#570) lives at delivery time**, not
        here. ``_deliver_turn`` awaits ``_session_ready_event`` for
        ``turn.internal and turn.reason.startswith("wake_")`` before
        calling ``paste_text``, so the wake ``_QueuedTurn`` is enqueued
        IMMEDIATELY by this method and sits at the queue HEAD while the
        worker blocks. Any external ``send()`` calls during the gate
        wait enqueue BEHIND the wake turn, preserving FIFO across the
        bootstrap window. Gating here at enqueue time would let
        concurrent external messages jump ahead while the wake sits in
        the SessionStart wait (Murzik #571 review catch).

        ``front=True``: prepend the turn at the HEAD of ``_message_queue``
        ahead of any existing contents, instead of the default tail
        ``put()``. Used by ``force_restart``'s wake-prompt re-prime: the
        inflight watchdog requeues replay/backlog at the front of the
        queue *before* scheduling the restart, so a tail-enqueued wake
        prompt would sit behind that backlog and the resumed REPL would
        process user turns before ever seeing orientation (Murzik #589
        review). ``asyncio.Queue`` has no put-front, so we use the same
        drain+repush pattern the watchdog uses; it is synchronous (no
        ``await`` between drain and repush) so it's atomic w.r.t. other
        tasks. Caller is responsible for invoking this BEFORE the worker
        starts draining when strict head placement is required.
        """
        if self.state != SessionState.CONNECTED:
            _log(
                f"tmux[{self.agent_name}]: not connected (state={self.state.value}), "
                f"dropping internal prompt (reason={reason})"
            )
            return None

        self.last_active = time.time()
        # Audit log — the diagnostic marker validation tooling greps for.
        # Hash gives a stable identity per prompt body without leaking the
        # text into operator log streams.
        import hashlib as _hashlib

        _prompt_hash = _hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:12]
        _log(
            f"tmux[{self.agent_name}]: wake_prompt_sent "
            f"reason={reason} "
            f"prompt_chars={len(prompt)} "
            f"prompt_hash={_prompt_hash} "
            f"wait={wait_for_completion}"
        )
        await self._emit_stream_event(
            {
                "type": "wake_prompt_sent",
                "agent_name": self.agent_name,
                "reason": reason,
                "prompt_chars": len(prompt),
                "prompt_hash": _prompt_hash,
                "wait_for_completion": wait_for_completion,
            }
        )

        completion = asyncio.Event() if wait_for_completion else None
        turn = _QueuedTurn(
            prompt=prompt,
            platform="",
            chat_id="",
            message_id="",
            internal=True,
            reason=reason,
            completion_event=completion,
            on_delivered=on_delivered,
        )
        if front:
            # Prepend ahead of existing queue contents. Synchronous
            # drain+repush (no await between) so it's atomic w.r.t. the
            # worker and any concurrent enqueues. Mirrors the watchdog's
            # replay-requeue pattern (asyncio.Queue has no put-front).
            backlog: list[_QueuedTurn] = []
            while not self._message_queue.empty():
                try:
                    backlog.append(self._message_queue.get_nowait())
                except asyncio.QueueEmpty:
                    break
            self._message_queue.put_nowait(turn)
            for t in backlog:
                self._message_queue.put_nowait(t)
        else:
            await self._message_queue.put(turn)

        if wait_for_completion and completion is not None:
            if timeout_sec is not None:
                await asyncio.wait_for(completion.wait(), timeout=timeout_sec)
            else:
                await completion.wait()
        return None

    # ── Response capture pipeline (PR8b) ────────────────────────────────

    def notify_tail(self) -> None:
        """Wake the transcript tailer — called from the Stop hook handler.

        Idempotent + no-op if the tailer hasn't started yet (e.g. wake
        arrives during cold-start before the spawn completes). The
        tailer's own ``wake()`` is safe before ``start()``.
        """
        if self._tailer is not None:
            self._tailer.wake()

    def set_transcript_path(self, path: Path | str) -> None:
        """Update the watched transcript path — called when SessionStart
        hook reports the actual path Claude Code is writing to.

        Cleaner than guessing the path via mtime glob: the SessionStart
        hook fires before the first model call, so the tailer is
        repointed at the right file before any response data arrives.

        **First bind for a fresh launch reads from byte 0** (issue
        #563, with Murzik review on PR #564 commit 1 extending the
        invariant beyond the cold-start placeholder case).

        The hook's "fires before the first model call" claim is
        empirically false: the wake-action turn can complete in <1s
        (final text + ``stop_hook_summary`` written to the JSONL)
        while the hook arrival is 50-200ms after. If we let the
        tailer seek to current-EOF on the first real path bind (the
        default behavior designed for compact-resume to defend
        against #496 round-1 Case 3 reply-spam), we skip past the
        first turn's ``stop_hook_summary`` forever — the deque head
        meta stays unresolved, subsequent turns pile up behind it
        as tail entries, and the watchdog fires at 600s. Observed
        4 times on Dymok across the log history.

        Two flavors of this race:
          1. **Cold-start placeholder→real:** ``_start_tailer`` found
             no prior transcript and used the placeholder; SessionStart
             hook reports the fresh JSONL after CC's first turn lands.
          2. **Forced-fresh old-real→new-real:** ``force_fresh_context_once``
             made this launch fresh despite prior history;
             ``_start_tailer`` discovered the OLD JSONL via mtime scan;
             SessionStart hook reports the NEW JSONL that CC just
             created — same late-hook race against CC's first turn.

        Both share the invariant: **the first ``set_transcript_path``
        call after ``_start_tailer`` for a fresh launch should seek
        to byte 0**. The ``_tailer_first_bind_pending`` flag (set in
        ``_start_tailer``, consumed here) tracks "first call since
        spawn"; ``not self._last_launch_used_continue`` qualifies
        "fresh launch."

        For continue launches, the seek-to-EOF default is preserved
        — the JSONL has prior history and we must not replay it
        (#496 reply-spam defense unchanged for the live-session case).
        Even if a continue launch races and ends up on a placeholder
        in ``_start_tailer`` (unlikely but possible if
        ``_has_prior_transcript`` and ``_discover_transcript_path``
        disagree under a project-dir mutation race), the predicate
        evaluates ``True AND not True = False`` → seek to EOF, safe.

        The flag is consumed regardless of whether the path actually
        changed (the tailer's own equality guard handles no-ops). This
        prevents repeated SessionStart posts later in the session from
        accidentally being treated as a "first bind" again.

        **Issue #570 — wake-prompt readiness signal.** This method also
        opens ``_session_ready_event`` on first call after spawn (the
        SessionStart hook is our most reliable "claude is past splash
        + MCP bootstrap, input area is live" signal). ``_deliver_turn``
        awaits this event for any in-flight turn with ``internal=True
        and reason.startswith("wake_")`` before calling ``paste_text``,
        so the wake-action paste doesn't land while CC is still in a
        transition state that would consume its Enter instead of
        submitting the turn. See ``_deliver_turn`` for the gate logic;
        reset semantics live in ``_start_tailer``. Gate lives at
        delivery (not enqueue) so the wake turn stays at queue head
        and external sends queue behind — FIFO preserved (Murzik #571
        review).
        """
        if self._tailer is None:
            return
        seek_to_start = (
            self._tailer_first_bind_pending
            and not self._last_launch_used_continue
        )
        # Consume the first-bind flag now — even if the tailer's
        # internal equality guard short-circuits the actual swap.
        self._tailer_first_bind_pending = False
        self._tailer.set_transcript_path(
            Path(path), seek_to_start=seek_to_start,
        )
        _log(
            f"tmux[{self.agent_name}]: transcript path updated to {path}"
            + (" (first-bind — seek_to_start)" if seek_to_start else "")
        )

        # Issue #570: SessionStart hook firing is our "claude is past
        # splash + MCP boot, input area is live" signal — open the
        # readiness gate so any pending wake prompt's paste can land.
        # Idempotent under .set() so a hook that re-fires later in the
        # session is a harmless no-op (existing tests confirm hook can
        # fire on every CC SessionStart event, not just first launch).
        if not self._session_ready_event.is_set():
            self._session_ready_event.set()
            _log(
                f"tmux[{self.agent_name}]: session-ready gate opened "
                f"(SessionStart hook)"
            )

    async def get_pane_snapshot(self, *, lines: int = 200) -> str:
        """Return the last ``lines`` lines of the tmux pane, with ANSI
        escape sequences preserved.

        Used by the read-only pane-view SSE endpoint to stream live
        terminal output to the chat UI's xterm.js modal. ANSI escapes
        carry color + cursor positioning so xterm renders the pane the
        way a human sees it.

        Returns an empty string if the tmux subprocess fails — caller
        decides whether to retry or surface to the UI. Mirrors the
        defensive posture of ``_handle_turn_complete``: a transient
        tmux blip never raises out of this layer.
        """
        try:
            result = await self._tmux.capture_pane(
                lines=lines, escapes=True,
            )
        except Exception as e:
            _log(
                f"tmux[{self.agent_name}]: get_pane_snapshot raised: {e}"
            )
            return ""
        if not result.ok:
            return ""
        return result.stdout

    async def resize_pane(self, *, cols: int, rows: int) -> bool:
        """Resize the tmux window (and therefore its single pane) to
        ``cols`` × ``rows`` characters.

        Called by the read-only pane-view endpoint so the agent's
        terminal reflows to match the viewer's xterm grid — without
        this, a detached session stays at tmux's 80×24 default and the
        captured snapshot looks tiny inside a larger modal.

        Returns ``True`` on success. Failures are swallowed (logged
        only): the viewer would rather display a slightly-misfit
        snapshot than abort the whole stream over a transient tmux
        error. Dim clamping happens in ``TmuxRunner.resize_window``.
        """
        try:
            result = await self._tmux.resize_window(cols=cols, rows=rows)
        except Exception as e:
            _log(
                f"tmux[{self.agent_name}]: resize_pane raised: {e}"
            )
            return False
        if not result.ok:
            _log(
                f"tmux[{self.agent_name}]: resize_pane failed "
                f"(rc={result.returncode}): {result.stderr.strip()}"
            )
            return False
        return True

    # Named keys the typeable pane view may send. Bounded on purpose:
    # enough to drive Claude Code's dialogs/menus and edit a prompt line,
    # without exposing tmux's full keyname surface. C-c is included —
    # interrupting a runaway turn is half the point of operator input.
    PANE_KEY_WHITELIST = frozenset({
        "Enter", "Escape", "Tab", "BTab", "Space", "BSpace", "DC",
        "Up", "Down", "Left", "Right", "Home", "End", "PPage", "NPage",
        "C-c", "C-u",
    })

    async def send_pane_keys(self, *, text: str = "", key: str = "") -> bool:
        """Operator keystrokes from the pane-view modal (typeable terminal).

        Exactly one of ``text`` / ``key`` per call:

        - ``text`` — literal characters, sent with ``send-keys -l`` so tmux
          performs NO keyname interpretation ("Enter" types five letters).
          C0/DEL control characters are rejected — a literal "\\x04" would
          be C-d in the pane, bypassing the named-key whitelist.
        - ``key`` — one named key from ``PANE_KEY_WHITELIST`` (tmux keyname
          semantics: Enter submits, Up/Down navigate dialogs, C-c interrupts).

        This is the interactive counterpart of ``get_pane_snapshot`` — same
        pane, same defensive posture (log + False, never raise). It exists so
        an operator can resolve first-run dialogs / wedged prompts from the
        web UI without SSH + ``tmux attach``.
        """
        if bool(text) == bool(key):
            return False  # exactly one input mode per call
        if key and key not in self.PANE_KEY_WHITELIST:
            _log(
                f"tmux[{self.agent_name}]: send_pane_keys rejected "
                f"non-whitelisted key {key!r}"
            )
            return False
        if text and any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in text):
            # Control bytes in the literal channel would bypass the key
            # whitelist ("\x04" is C-d regardless of which door it came
            # through) — control sequences are only reachable as named keys.
            _log(
                f"tmux[{self.agent_name}]: send_pane_keys rejected "
                f"control characters in literal text"
            )
            return False
        try:
            if text:
                result = await self._tmux.send_literal(text)
            else:
                result = await self._tmux.send_keys(key, enter=False)
        except Exception as e:
            _log(f"tmux[{self.agent_name}]: send_pane_keys raised: {e}")
            return False
        if not result.ok:
            _log(
                f"tmux[{self.agent_name}]: send_pane_keys failed "
                f"(rc={result.returncode}): {result.stderr.strip()}"
            )
            return False
        return True

    # Claude Code reserves a buffer below the raw model cap so the
    # ``/compact`` autocompact step fires before the API rejects the
    # next turn for context exhaustion. Empirically 33K on the 200K
    # window (≈16.5%); per the GH discussion thread (anthropics/
    # claude-code#27189) and the SDK's ``ContextUsageResponse``
    # docstring this is a fixed constant — not a percentage — so 1M-
    # window models reserve the same 33K, not a proportionally larger
    # buffer.
    _AUTOCOMPACT_BUFFER_TOKENS = 33_000

    # Absolute token ceiling for the restart-for-sanity nudge on
    # 1M-context models. Brad's preference (2026-05-29): on a 1M window,
    # restart around 400k tokens for a clean slate rather than riding the
    # context up toward the autocompact buffer. Expressed as an absolute
    # token count (not a %) because "restart around 400k" is how the
    # budget is reasoned about, and 40-ish % means very different real
    # headroom on a 1M vs a 200k window. Only bites when it is *below*
    # the percentage-based threshold (always true on 1M, never on 200k
    # since 400k exceeds that window entirely).
    _RESTART_TOKENS_CAP_1M = 400_000

    def _raw_max_tokens_for_model(self) -> int:
        """Return the model's **raw** context-window cap (no buffer).

        Mirrors ``api._streaming_context_info``'s 1M-model logic: models
        listed in ``_1M_MODELS`` cap at 1M tokens; everything else 200k.
        Lazy import dodges the streaming_session ↔ tmux_session circle.

        Use this for parity with the SDK's ``rawMaxTokens`` field;
        callers measuring real headroom should use
        ``_max_tokens_for_model`` (the effective cap with the
        autocompact buffer subtracted).
        """
        try:
            from pinky_daemon.streaming_session import _1M_MODELS
            big_models = _1M_MODELS  # noqa: N806 - alias for readability
        except Exception:
            big_models = set()
        model = (self._config.model or "").strip()
        return 1_000_000 if model in big_models else 200_000

    def _max_tokens_for_model(self) -> int:
        """Return the model's **effective** context-window cap.

        Effective = raw cap minus Claude Code's autocompact buffer.
        Without this subtraction our percentage gauge under-reports by
        ~16 points on the 200K window (gauge shows 50% at 100K real
        tokens; ``/context`` shows ~60%), and the restart-nudge fires
        ~16% later than it should.

        Honours ``CLAUDE_AUTOCOMPACT_PCT_OVERRIDE`` (Claude Code's own
        env var) as the **effective-cap percentage of raw** — e.g.
        ``85`` means autocompact triggers at 85% so effective = 85% of
        raw. Setting it to ``100`` disables the buffer entirely
        (effective == raw); malformed values fall back to the default.
        """
        raw = self._raw_max_tokens_for_model()
        override = os.environ.get("CLAUDE_AUTOCOMPACT_PCT_OVERRIDE", "").strip()
        if override:
            try:
                pct = float(override)
                if pct > 0:
                    return max(1, int(raw * pct / 100.0))
            except (TypeError, ValueError):
                # Bad env value — log and fall through to default.
                _log(
                    f"tmux[{self.agent_name}]: ignoring malformed "
                    f"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE={override!r}"
                )
        return max(1, raw - self._AUTOCOMPACT_BUFFER_TOKENS)

    def _isolation_status(self) -> str:
        """Tri-state isolation lookup for the env secret gate (#149 phase-3).

        Returns ``"isolated"``, ``"not_isolated"``, or ``"unknown"`` (registry
        unwired, agent not found, or lookup raised). A bare bool would conflate
        "proven non-isolated" (safe to inject the global secret) with "can't
        tell" — and Murzik's #639 review caught that conflation as a fail-OPEN:
        if ``get_signing_key`` returns a key but ``registry.get`` raises, a bool
        helper falls to False and the env builder would inject BOTH the per-agent
        key AND the forgeable global secret (the same fail-open class fixed in
        #635). The caller withholds the global secret whenever isolation can't
        be *proven* false and a per-agent key already provides a working
        identity, so registry uncertainty never causes global-secret exposure.
        """
        if not self._registry or not self.agent_name:
            return "unknown"
        try:
            agent = self._registry.get(self.agent_name)
        except Exception:
            return "unknown"
        if agent is None:
            return "unknown"
        # A non-local isolation_mode IS isolation, regardless of the `isolated`
        # bool: a container/unix_user tenant holding the fleet-wide forgeable
        # PINKY_SESSION_SECRET would defeat the entire OS boundary (#638 gap —
        # the register/update models coerce isolated=True for non-local modes,
        # but legacy rows / direct DB writes must not bypass the secret gate).
        if getattr(agent, "isolation_mode", "local") not in ("", "local"):
            return "isolated"
        return "isolated" if getattr(agent, "isolated", False) else "not_isolated"

    def _restart_threshold_pct(self) -> float:
        """Pull the agent's restart threshold from the registry.

        Defaults to 80% if the registry isn't wired or doesn't carry
        a value — matches AgentRegistry's default and StreamingSession's
        behavior.
        """
        if not self._registry:
            return 80.0
        try:
            agent = self._registry.get(self.agent_name)
            if agent and getattr(agent, "restart_threshold_pct", None):
                return float(agent.restart_threshold_pct)
        except Exception:
            pass
        return 80.0

    def _effective_restart_threshold_pct(self) -> float:
        """Restart threshold as a percentage, with the 1M absolute cap applied.

        Combines the per-agent percentage threshold
        (``_restart_threshold_pct``) with the absolute
        ``_RESTART_TOKENS_CAP_1M`` ceiling, returning whichever fires
        *earlier* (the lower percentage). The cap is expressed against
        the **effective** max tokens so it lines up with the percentage
        the gauge reports — i.e. crossing the returned percentage means
        the real token total has reached ``min(pct·max, 400k)``.

        On a 200k window the 400k cap exceeds the whole window, so the
        ``min`` is always the configured percentage and behaviour is
        unchanged. On a 1M window 400k ≈ 41% of the ~967k effective cap,
        so the threshold drops from the default 80% to ~41% — Brad's
        restart-around-400k-for-sanity preference.
        """
        pct_threshold = self._restart_threshold_pct()
        max_tokens = self._max_tokens_for_model()
        if max_tokens <= 0:
            return pct_threshold
        cap_pct = self._RESTART_TOKENS_CAP_1M / max_tokens * 100.0
        return min(pct_threshold, cap_pct)

    def _soft_nudge_threshold_pct(self) -> float:
        """Pull the agent's soft context-watermark from the registry (#614).

        Returns the per-agent ``context_nudge_threshold_pct`` when set to a
        positive value; otherwise falls back to the global
        ``DEFAULT_CONTEXT_NUDGE_THRESHOLD_PCT`` (35%). A value of 0 means
        "unset → use global default", matching AgentRegistry's column default.
        """
        if not self._registry:
            return DEFAULT_CONTEXT_NUDGE_THRESHOLD_PCT
        try:
            agent = self._registry.get(self.agent_name)
            raw = getattr(agent, "context_nudge_threshold_pct", 0.0) if agent else 0.0
            if raw and float(raw) > 0:
                return float(raw)
        except Exception:
            pass
        return DEFAULT_CONTEXT_NUDGE_THRESHOLD_PCT

    def _record_turn_usage(self, response: TurnResponse) -> None:
        """Fold a turn's usage block into ``self.usage`` (SessionUsage).

        ``SessionUsage.record`` expects a RunResult-shape object with
        cost / duration / model_usage fields the tmux path doesn't
        produce — so we accumulate the token fields directly here.
        Defensive: a malformed usage dict (schema drift) is treated as
        zero contributions rather than crashing the tailer.
        """
        u = response.usage if isinstance(response.usage, dict) else {}
        try:
            self.usage.input_tokens += int(u.get("input_tokens", 0) or 0)
            self.usage.output_tokens += int(u.get("output_tokens", 0) or 0)
            # Claude transcripts use ``cache_creation_input_tokens`` /
            # ``cache_read_input_tokens``; SDK uses ``cache_write_tokens`` /
            # ``cache_read_tokens``. Accept either.
            self.usage.cache_read_tokens += int(
                u.get("cache_read_input_tokens", 0)
                or u.get("cache_read_tokens", 0)
                or 0
            )
            self.usage.cache_write_tokens += int(
                u.get("cache_creation_input_tokens", 0)
                or u.get("cache_write_tokens", 0)
                or 0
            )
        except (TypeError, ValueError) as e:
            _log(
                f"tmux[{self.agent_name}]: usage parse drifted, "
                f"skipping turn ({type(e).__name__}: {e})"
            )

        self.usage.total_turns += 1
        self.usage.total_duration_ms += max(0, int(response.duration_ms or 0))
        self.usage.last_stop_reason = response.stop_reason or ""
        if u:
            self.usage.last_usage = dict(u)

    def _log_turn_cost_and_analytics(self, response: TurnResponse) -> None:
        """Forward a completed turn's usage to analytics + cost tracking.

        The SDK path (``StreamingSession``) gets ``total_cost_usd`` on
        every ``ResultMessage`` and fires ``cost_callback`` +
        ``analytics_store.log_turn_usage`` per turn — that's what powers
        the live Analytics page and lifetime-cost rollups. The tmux path
        runs Claude Code under a subscription, so the transcript carries
        only token *counts*, never a dollar figure. Without this, tmux
        agents are dark on live Analytics and lifetime cost; only the
        post-hoc ``burn_snapshot`` scrape catches them (#648).

        We close that gap here: compute the per-turn cost from the token
        counts via the in-tree rate table (``pricing.py``, the live twin
        of ``burn_cost_report``'s rate file) and fire both callbacks with
        the SDK's signatures.

        Must run AFTER ``_record_turn_usage`` so ``self.usage.total_turns``
        is the current turn's 1-based sequence — the tmux analog of the
        SDK's ``self._turn_seq``. Both reset to 0 per session and share a
        stable ``self.id``, so the ``log_turn_usage`` upsert
        (``ON CONFLICT(session_id, turn_seq)``) behaves identically across
        the two transports.

        Defensive throughout: pricing/analytics are side telemetry, never
        a correctness dependency of the turn pipeline. A failure here must
        not crash the tailer or break reply delivery.
        """
        if not self._analytics_store and not self._cost_callback:
            return
        turn_seq = self.usage.total_turns
        if turn_seq <= 0:
            return

        u = response.usage if isinstance(response.usage, dict) else {}
        # Prefer the transcript's own model field (authoritative for the
        # turn that actually ran); fall back to the configured model.
        model = (response.model or self._config.model or "").strip()

        try:
            input_tokens = int(u.get("input_tokens", 0) or 0)
            output_tokens = int(u.get("output_tokens", 0) or 0)
            # Analytics ``cached_input_tokens`` is cache-READ only (matches
            # the SDK path + the column's meaning).
            cached_input_tokens = int(
                u.get("cache_read_input_tokens", 0)
                or u.get("cache_read_tokens", 0)
                or 0
            )
        except (TypeError, ValueError):
            input_tokens = output_tokens = cached_input_tokens = 0

        cost_usd = 0.0
        try:
            cost_usd = compute_cost_from_usage(model, u)
        except Exception as e:  # pragma: no cover - defensive
            _log(f"tmux[{self.agent_name}]: turn cost compute failed: {e}")
        if model and cost_usd == 0.0 and (input_tokens or output_tokens):
            # Non-empty turn but zero cost ⇒ no rate row for this model.
            # Surface once so a new model id gets added to the table.
            _log(
                f"tmux[{self.agent_name}]: no pricing rate for model "
                f"{model!r}; turn cost recorded as $0"
            )

        if cost_usd:
            self.usage.total_cost_usd += cost_usd
        if self._cost_callback:
            try:
                self._cost_callback(
                    self.agent_name,
                    cost_usd,
                    input_tokens,
                    output_tokens,
                    self.resume_handle or "",
                )
            except Exception as e:
                _log(f"tmux[{self.agent_name}]: cost callback error: {e}")

        if self._analytics_store and (
            input_tokens or output_tokens or cached_input_tokens
        ):
            try:
                self._analytics_store.log_turn_usage(
                    session_id=self.id,
                    agent_name=self.agent_name,
                    turn_seq=turn_seq,
                    provider="anthropic",
                    model=model,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cached_input_tokens=cached_input_tokens,
                    error=False,
                )
            except Exception as e:
                _log(f"tmux[{self.agent_name}]: analytics usage failed: {e}")

    def _current_total_tokens(self) -> int:
        """Token count for the current *context window* (not cumulative).

        Subtle: ``SessionUsage`` accumulates across turns for cost +
        lifetime-usage tracking, but context-window size is a
        per-API-call number — each Claude Code turn re-sends the full
        prior conversation, so the LAST turn's ``input_tokens`` already
        captures everything currently in context. Summing across turns
        would multi-count.

        Mirrors how the SDK reports context: its
        ``client.get_context_usage()`` returns the live window state,
        not a lifetime sum. We approximate that from ``last_usage`` —
        the most recent assistant entry's usage block (captured by
        ``_TurnBuffer._last_usage`` in the tailer, then folded into
        ``SessionUsage.last_usage`` by ``_record_turn_usage``).

        Formula: ``input_tokens + output_tokens + cache_read +
        cache_write`` — Anthropic's prompt-cached tokens count toward
        the window separately from the inline ``input_tokens``, so all
        four kinds must be summed for parity with the SDK's reported
        total.
        """
        last = self.usage.last_usage if isinstance(self.usage.last_usage, dict) else {}
        try:
            return (
                int(last.get("input_tokens", 0) or 0)
                + int(last.get("output_tokens", 0) or 0)
                + int(
                    last.get("cache_read_input_tokens", 0)
                    or last.get("cache_read_tokens", 0)
                    or 0
                )
                + int(
                    last.get("cache_creation_input_tokens", 0)
                    or last.get("cache_write_tokens", 0)
                    or 0
                )
            )
        except (TypeError, ValueError):
            return 0

    @property
    def context_used_pct(self) -> float:
        """Context-window usage as a percentage (#745).

        ``StreamingSession`` and ``CodexSession`` both expose this
        property, and callers that don't know the transport — the
        scheduler's heartbeat reconciler in particular — read it via
        ``getattr(session, "context_used_pct", 0.0)``. Without it every
        reconciled heartbeat for a tmux agent recorded 0.0% while the
        real number sat one call away in ``get_context_info()``.
        """
        max_tokens = self._max_tokens_for_model()
        if max_tokens <= 0:
            return 0.0
        return round(self._current_total_tokens() / max_tokens * 100.0, 1)

    def get_context_info(self) -> dict:
        """Return SDK-compatible context-window snapshot.

        Consumed by ``api._streaming_context_info`` (which checks for
        this method when ``ss._client`` is absent — the tmux case). The
        return shape matches what the SDK's ``get_context_usage`` would
        emit, so the existing ``/agents/{name}/streaming/status``
        endpoint serves tmux sessions with zero downstream changes.
        Frontend Chat.svelte already renders ``streamingStats.totalTokens``
        / ``maxTokens`` / ``categories`` from that endpoint.

        Categories are coarse-grained for tmux — we don't have the SDK's
        per-tool / per-mcp breakdown, just the cumulative token rollups.
        """
        total = self._current_total_tokens()
        max_tokens = self._max_tokens_for_model()
        raw_max_tokens = self._raw_max_tokens_for_model()
        pct = (total / max_tokens * 100.0) if max_tokens > 0 else 0.0

        # Categories breakdown reflects the *current* context window
        # (same source as ``total``: ``last_usage``), so the chat UI's
        # stacked bar adds up to ``total``. Pulling from cumulative
        # SessionUsage counters would make the bar show lifetime
        # totals and disagree with the percentage gauge.
        last = self.usage.last_usage if isinstance(self.usage.last_usage, dict) else {}

        def _int(d: dict, *keys: str) -> int:
            for k in keys:
                v = d.get(k)
                if v:
                    try:
                        return int(v)
                    except (TypeError, ValueError):
                        return 0
            return 0

        categories = [
            {"name": "Input", "tokens": _int(last, "input_tokens")},
            {"name": "Output", "tokens": _int(last, "output_tokens")},
            {"name": "Cache read",
             "tokens": _int(last, "cache_read_input_tokens", "cache_read_tokens")},
            {"name": "Cache write",
             "tokens": _int(last, "cache_creation_input_tokens", "cache_write_tokens")},
        ]
        return {
            "totalTokens": total,
            "maxTokens": max_tokens,
            "rawMaxTokens": raw_max_tokens,
            # Snake-case alias for ``_streaming_context_info`` which
            # also reads these (different code paths normalize via
            # camelCase or snake_case depending on the caller).
            "total_tokens": total,
            "max_tokens": max_tokens,
            "raw_max_tokens": raw_max_tokens,
            "percentage": round(pct, 1),
            "categories": categories,
            "mcpTools": [],
            "mcp_tools": [],
        }

    async def _emit_context_usage_event(self) -> None:
        """Emit a ``context_usage`` SSE event and a ``restart_nudge``
        when the cumulative token total crosses the agent's
        ``restart_threshold_pct``.

        The nudge is one-shot per crossing: once we've fired above the
        threshold, we don't fire again until the total drops below it
        (e.g. after a /compact). This protects against a cascade of
        nudges every turn at high context.
        """
        info = self.get_context_info()
        await self._emit_stream_event(
            {
                "type": "context_usage",
                "agent_name": self.agent_name,
                **info,
            }
        )

        threshold = self._effective_restart_threshold_pct()
        pct = info.get("percentage", 0.0) or 0.0
        if pct >= threshold and not self._restart_nudge_fired:
            self._restart_nudge_fired = True
            await self._emit_stream_event(
                {
                    "type": "restart_nudge",
                    "agent_name": self.agent_name,
                    "percentage": pct,
                    "threshold_pct": threshold,
                    "total_tokens": info["totalTokens"],
                    "max_tokens": info["maxTokens"],
                }
            )
            # Drive the action, not just the notification. The SSE event
            # above informs the UI / observers; this delivers the restart
            # directive INTO the agent's own next turn so something
            # actually happens. Gated by PINKY_CONTEXT_AUTORESTART_NUDGE
            # (default on; set "0" to fall back to notify-only for soak /
            # kill-switch). Latch above guarantees one nudge per crossing.
            if os.environ.get("PINKY_CONTEXT_AUTORESTART_NUDGE", "1") != "0":
                await self._enqueue_autorestart_nudge(
                    total=info["totalTokens"],
                    max_tokens=info["maxTokens"],
                    pct=pct,
                )
        elif pct < threshold and self._restart_nudge_fired:
            # Re-arm the latch once context drops back below threshold
            # (e.g. /compact ran). Next crossing will fire a fresh nudge.
            self._restart_nudge_fired = False

        # Soft context-watermark nudge (#614). Unlike the restart_nudge
        # above (SSE-to-UI only), this injects a one-time reminder INTO the
        # agent's REPL telling it to checkpoint + context_restart at a
        # natural break. It sits strictly below the hard threshold: if usage
        # is already at/above the hard line, that path owns the response and
        # we don't double-act (issue #614 "hard wins"). Fires once per
        # crossing; re-arms when usage drops back below the soft line.
        #
        # ``threshold`` here is the EFFECTIVE hard threshold
        # (``_effective_restart_threshold_pct``, post-#618), not the raw 80%.
        # That matters on 1M-context models where the hard line drops to
        # ~41% (the 400k cap): the soft band must follow it down to
        # [soft, ~41%) so the nudge never fires ABOVE the real restart point
        # (which would invert the escalation — soft after hard). Gating on
        # the effective threshold keeps "soft strictly below hard" true on
        # both 200k and 1M windows. (Dymok #614/#618 integration.)
        soft_threshold = self._soft_nudge_threshold_pct()
        if 0 < soft_threshold < threshold:
            if soft_threshold <= pct < threshold and not self._soft_nudge_fired:
                self._soft_nudge_fired = True
                await self._emit_stream_event(
                    {
                        "type": "context_nudge_soft",
                        "agent_name": self.agent_name,
                        "percentage": pct,
                        "threshold_pct": soft_threshold,
                        "total_tokens": info["totalTokens"],
                        "max_tokens": info["maxTokens"],
                    }
                )
                await self._enqueue_internal_prompt(
                    build_context_nudge_prompt(pct, soft_threshold),
                    reason="context_nudge_soft",
                    wait_for_completion=False,
                )
            elif pct < soft_threshold and self._soft_nudge_fired:
                self._soft_nudge_fired = False

    async def _enqueue_autorestart_nudge(
        self, *, total: int, max_tokens: int, pct: float
    ) -> None:
        """Deliver the restart-for-sanity directive into the agent's own turn.

        The companion ``restart_nudge`` SSE event tells the UI / observers;
        this tells the *agent*. Routed through ``_enqueue_internal_prompt``
        so it rides the normal turn queue without polluting the
        user-visible conversation (no conversation_store append, no chat
        routing). The agent is asked to author its own continuation via
        ``save_my_context`` *before* ``context_restart`` — the daemon
        can't write a meaningful wake_action, which is exactly why a clean
        restart (fresh slate + agent-authored handoff) beats an in-place
        ``/compact`` at this depth.

        Tail-enqueued (not ``front``): any user turns already queued are
        answered first, then the restart. The alternative — jumping the
        restart ahead of pending user work — trades responsiveness for a
        slightly tighter context bound; left as a follow-up call for
        review. One nudge per crossing (caller latched).
        """
        prompt = (
            f"⚠️ Context budget check — you're at {total:,} / {max_tokens:,} tokens "
            f"({pct:.0f}%), past your restart-for-sanity threshold. Finish the "
            f"thought you're on, then: (1) call save_my_context with a concrete "
            f"wake_action capturing exactly what to resume, and (2) call "
            f"context_restart to continue in a fresh session. Do this now — don't "
            f"pick up new work first. A clean restart keeps your reasoning sharp."
        )
        # MUST stay fire-and-forget (wait_for_completion=False). This runs
        # inside the tailer's _handle_turn_complete callback — the very code
        # that SETS turn completion events. Waiting here for THIS nudge's
        # completion would block the single tailer task on an event only a
        # future stop-hook (drained by that same task) can set: a self-
        # deadlock, bounded only by timeout_sec. Do not "improve" this to
        # wait_for_completion=True. (Dymok #618 review.)
        await self._enqueue_internal_prompt(prompt, reason="context_autorestart_nudge")

    async def _handle_turn_complete(self, response: TurnResponse) -> None:
        """Tailer callback — fired once per ``stop_hook_summary`` entry.

        Mirrors StreamingSession's per-turn dispatch: feed the
        conversation store, fire response_callback, fire stream_event
        for analytics. cost_callback is a no-op for tmux (subscription
        billing, no per-turn cost) but we still fire stream_event so
        usage telemetry is visible.

        **#560 — concurrent dispatch.** Each stop hook pops the OLDEST
        in-flight meta from ``_inflight_metas`` (FIFO). Internal-vs-
        external + per-turn completion event come from the popped
        entry's own fields — NOT from ``_inflight_turn`` (which under
        concurrent dispatch may already point at a later turn that's
        also pasted). This is the deque equivalent of PR #543's
        internal-prompt branch.

        Critical-section discipline (Murzik review point #6): the
        synchronous block at the top — popleft, set ``completion_event``,
        advance ``_head_started_at``, set back-compat ``_turn_done`` —
        runs without ``await`` so concurrent stop hooks (in practice
        serialized by the tailer's single-task read loop, but defended
        here too) can't interleave with deque mutation. The async
        callback chain (``conversation_store.append`` is sync, but
        ``_emit_stream_event`` / ``_response_callback`` / context-budget
        emission ARE awaited) runs AFTER, against local copies of the
        popped state. By the time we await anything, the deque is
        consistent.

        Empty-on-pop defense (Murzik review point #7): if a stop hook
        arrives with an empty deque (race, stale tailer, double-fire,
        force_restart in-flight), log and bail. Do NOT synthesize routing
        metadata — that would resurrect the #496 Case 1 defect with a
        twist (route to wrong chat from an empty/zero state).
        """
        # ── Critical section: synchronous deque mutation + signals ────
        # #731: a Stop hook means the model yielded — no foreground tool is
        # executing, so any remaining in-flight tool entries are leaked (a lost
        # PostToolUse finish-POST). Clear them here so the next turn's wedge
        # verdict can't be spuriously extended by a stale entry.
        self._inflight_tool_calls.clear()
        if not self._inflight_metas:
            # No meta to pop. Stop hook arrived without a dispatch
            # behind it — most commonly an AUTONOMOUS turn (background-
            # task notification, harness re-invocation) that never had a
            # daemon dispatch; also race/stale tailer. Bail on the
            # callback chain; routing must NOT be synthesized. But the
            # turn DID end: clear per-turn live-activity state and tell
            # the UI, or Chat.svelte shows stale thinking dots + frozen
            # activity log until the next dispatched turn completes.
            _log(
                f"tmux[{self.agent_name}]: stop hook with empty inflight_metas "
                f"(autonomous turn / race) — skipping callback chain"
            )
            self._current_activity = ""
            self._current_thinking = ""
            self._activity_log = []
            await self._emit_stream_event(
                {
                    "type": "turn_completed",
                    "agent_name": self.agent_name,
                    "stop_reason": response.stop_reason,
                    "usage": response.usage,
                    "duration_ms": response.duration_ms,
                    "assistant_entry_count": response.assistant_entry_count,
                    "tool_use_count": len(response.tool_uses),
                    "autonomous": True,
                }
            )
            return

        entry = self._inflight_metas.popleft()
        # Unblock any wait_for_completion caller for THIS entry's turn
        # before the awaitable callbacks run — keeps the caller's wakeup
        # tight (no waiting on conversation_store / response_callback /
        # stream_event latency). Idempotent: ``.set()`` on a set Event
        # is a no-op.
        if entry.completion_event is not None and not entry.completion_event.is_set():
            entry.completion_event.set()
        # Advance the head-age watchdog (Murzik review point #1). If
        # entries remain, the NEW head's clock starts NOW so it gets
        # its own ``_TURN_DONE_TIMEOUT_SEC`` window. If the deque is
        # empty, the watchdog has nothing to age.
        if self._inflight_metas:
            self._head_started_at = time.time()
        else:
            self._head_started_at = None
        # Back-compat advisory signal. Worker no longer gates on this
        # (#560), but tests + external observers still listen.
        self._turn_done.set()
        # ``_has_completed_turn`` gates the restart_guard: once ANY
        # turn has completed in this session's lifetime, force_restart
        # asks the guard whether unsaved state should block teardown.
        # Pre-#560 the worker set this after observing ``_turn_done``;
        # under concurrent dispatch the worker no longer waits between
        # turns, so the canonical "first completion" signal moves here.
        self._has_completed_turn = True
        # ── End critical section ──────────────────────────────────────

        is_internal = entry.internal
        thinking_text = (response.thinking or "").strip()
        thinking_blocks = [thinking_text] if thinking_text else []
        thinking_chars = len(thinking_text)

        # Surface the latest completed thinking block briefly during turn
        # finalization. Live streaming of tmux thinking requires tailer-side
        # incremental events; this mirrors SDK state shape without leaving stale
        # thinking in status after the turn completes.
        if thinking_text:
            self._current_thinking = thinking_text

        # Log to conversation store. role=assistant. Skip for internal
        # turns so wake-prompt responses don't pollute the user-visible
        # conversation history (the response is still in the JSONL
        # transcript for audit).
        if not is_internal and self._conversation_store and response.text:
            try:
                if thinking_blocks:
                    self._conversation_store.append(
                        self.id,
                        "assistant",
                        response.text,
                        metadata={"thinking": thinking_blocks},
                    )
                else:
                    self._conversation_store.append(
                        self.id, "assistant", response.text,
                    )
            except Exception as e:
                _log(
                    f"tmux[{self.agent_name}]: conversation_store.append "
                    f"raised: {e}"
                )

        # Context-budget watchdog (task #95): accumulate per-turn usage
        # into ``self.usage`` so ``stats`` + ``get_context_info`` surface
        # cumulative + last-turn numbers. Tmux agents have been blind to
        # their own context window forever — without this they can't
        # make their own /compact / restart / sleep calls. The transcript
        # tailer already pulled the usage dict out of each assistant
        # entry's ``usage`` block; we just need to fold it into the
        # session-level dataclass and emit it.
        self._record_turn_usage(response)
        # #648 — forward per-turn usage to analytics + cost tracking so
        # tmux agents reach live Analytics / lifetime-cost parity with the
        # SDK path. Must follow ``_record_turn_usage`` (it bumps
        # ``total_turns``, used as the analytics turn_seq).
        self._log_turn_cost_and_analytics(response)
        await self._emit_context_usage_event()

        # Stream event for analytics (usage / duration). Named
        # ``turn_completed`` to match StreamingSession + CodexSession
        # (see ``streaming_session.py:942`` and ``codex_session.py:753``)
        # — Chat.svelte's SSE handler listens for ``turn_completed`` so
        # the UI clears pending-assistant-stream state at turn end.
        await self._emit_stream_event(
            {
                "type": "turn_completed",
                "agent_name": self.agent_name,
                "stop_reason": response.stop_reason,
                "usage": response.usage,
                "duration_ms": response.duration_ms,
                "assistant_entry_count": response.assistant_entry_count,
                "tool_use_count": len(response.tool_uses),
                "thinking_chars": thinking_chars,
                "thinking_block_count": len(thinking_blocks),
            }
        )

        # Response callback — the broker-routing payload. Includes the
        # captured inbound metadata (from the popped deque entry, NOT
        # the legacy single-dict cell) so the broker can route the reply.
        # Skip for internal turns: no chat target, and the metadata is
        # intentionally empty (see ``_deliver_turn``).
        if (
            not is_internal
            and self._response_callback
            and (response.text or response.tool_uses)
        ):
            try:
                meta = entry.meta
                turn_result = replace(
                    response,
                    agent_name=self.agent_name,
                    session_id=self.id,
                    platform=meta.get("platform", ""),
                    chat_id=meta.get("chat_id", ""),
                    message_id=meta.get("message_id", ""),
                    used_outreach_tools=any(
                        _is_outreach_tool(
                            tool_use.get("tool", "") or tool_use.get("name", "")
                        )
                        for tool_use in response.tool_uses
                    ),
                )
                result = self._response_callback(turn_result)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as e:
                _log(
                    f"tmux[{self.agent_name}]: response_callback raised: {e}"
                )

        # NOTE: deque pop + completion_event + ``_turn_done`` + head-age
        # advance all happened in the critical section at the top.
        # Don't re-emit them here — that would (a) double-fire events
        # on a now-stale ``entry``, and (b) defeat the "fire before
        # awaits so waiters wake promptly" discipline. This block used
        # to clear ``_inflight_meta = {}`` and set ``_turn_done`` /
        # ``completion_event``; under #560 the deque carries the state.

        # Reset per-turn live-activity state so the next turn starts
        # clean. Without this the polling endpoint ``/streaming/status``
        # keeps returning the previous turn's accumulated activity log
        # and Chat.svelte's thinking-bubble shows stale tool calls
        # blending across turns. ``_current_activity`` clears the
        # "Bash — ..." chip in the UI; ``_current_thinking`` clears the
        # reasoning preview; ``_activity_log`` clears the scrollback. The
        # chip-strip from PR #528 has its own per-turn lifetime on the client
        # and is unaffected.
        self._current_activity = ""
        self._current_thinking = ""
        self._activity_log = []

    async def handle_stop_failure(
        self,
        error_type: str,
        message: str = "",
        session_id: str = "",
    ) -> bool:
        """Resolve the in-flight turn when Claude Code reports a StopFailure.

        Issue #108 — close the turn-end-detection gap. The transcript
        tailer detects turn-end ONLY via ``system/stop_hook_summary``
        entries, which terminal API-error / StopFailure turns don't
        reliably emit. Without this, a failed turn wedges at the HEAD of
        ``_inflight_metas`` until the 10-minute ``_inflight_watchdog``
        force-restarts the session — the caller's ``completion_event``
        never fires, the chat gets no reply, and the deque ages for the
        full timeout.

        The ``StopFailure`` hook (#584) already POSTs a typed, explicitly
        terminal signal; this makes that POST the authoritative turn-end
        marker for failed turns (avoids the ``type==user``/``tool_result``
        ambiguity a transcript-scan heuristic would hit). Called by the
        ``/transport/stop-failure`` endpoint AFTER its existing logging +
        auth-alert routing, so #584's behavior is fully preserved.

        Behavior:

        - **Empty ``_inflight_metas``** → idempotent no-op. The turn
          already resolved (a real ``stop_hook_summary`` landed first, or
          a prior StopFailure POST cleared it). Log + return ``False``.
          Deliberately does NOT drain or synthesize: there is no in-flight
          turn to fail, and draining here could discard a legitimately
          accumulating *next* turn's partial buffer.
        - **Non-empty** → synthesize a ``TurnResponse`` carrying
          ``stop_reason="stop_failure:<error_type>"`` and feed it through
          ``_handle_turn_complete``. That reuses the full FIFO machinery:
          popleft the oldest meta, fire its ``completion_event``, advance
          ``_head_started_at`` so the next entry (FIFO advance: A fails →
          B becomes head) gets its own fresh timeout window, set the
          back-compat ``_turn_done``, and — for external turns — fire
          ``response_callback`` so the waiting caller learns the turn
          ended. Internal-turn suppression (no conversation_store append,
          no response_callback) is honored by ``_handle_turn_complete``
          unchanged. The tailer's in-progress buffer is drained FIRST — in
          the same no-await span as the synchronous pop — so (a) partial
          failed-turn text can't bleed into the next real
          ``stop_hook_summary``, and (b) a late ``stop_hook_summary``
          arriving while a queued turn (B) is the new head is absorbed
          silently (empty buffer → the tailer's ``is_empty`` branch never
          fires the callback) instead of falsely completing B. On the
          single-inflight path a late stop hook likewise finds an empty
          buffer / empty deque and is a harmless no-op (no double callback).
          See the drain-ordering note at the call site for why
          drain-after-await reopens the FIFO window.

        ``session_id`` is **log context only** — never a routing/match
        gate. A mismatch or empty value must NOT block unwedging the only
        live in-flight turn: the hook's ``session_id`` and the tailer's
        notion of the current turn can legitimately differ across a
        ``--continue`` resume.

        Returns ``True`` if an in-flight turn was resolved, ``False`` on
        the idempotent empty-deque path.
        """
        error_type = (error_type or "unknown").strip() or "unknown"
        sid_ctx = f" [session_id={session_id}]" if session_id else ""

        if not self._inflight_metas:
            _log(
                f"tmux[{self.agent_name}]: StopFailure ({error_type}) with no "
                f"in-flight turn — idempotent no-op{sid_ctx}"
            )
            return False

        _log(
            f"tmux[{self.agent_name}]: StopFailure ({error_type}) resolving "
            f"in-flight turn (deque depth={len(self._inflight_metas)}){sid_ctx}"
        )

        # Synthesize a terminal turn payload to route through the normal
        # completion path. ``_handle_turn_complete`` reads ``response.text``
        # (not the tailer buffer), so the synthesized text is what reaches
        # the caller — a human-legible failure note.
        synthesized = TurnResponse(
            text=message or f"Claude Code turn failed: {error_type}",
            stop_reason=f"stop_failure:{error_type}",
            usage={},
        )

        # Drain the tailer's in-progress turn buffer BEFORE resolving — in
        # the same no-await span as the synchronous deque pop at the top of
        # ``_handle_turn_complete`` (no event-loop yield occurs between this
        # drain and that pop). This ordering is load-bearing for the FIFO
        # case (Murzik review, PR #585): when A fails while B is queued
        # behind it, ``_handle_turn_complete`` pops A synchronously but then
        # awaits its stream/context/response_callback chain while B is the
        # NEW head. If the failed turn's partial assistant text were still
        # buffered, a late ``stop_hook_summary`` read by the tailer DURING
        # those awaits would fire ``_handle_turn_complete`` again and falsely
        # pop/complete B — the tailer fires its callback only when the buffer
        # is non-empty (``_read_and_dispatch``: ``closes_turn and not
        # is_empty``); an empty buffer takes the silent ``is_empty`` drain
        # branch and never fires. Draining first guarantees that late stop
        # hook finds an empty buffer and is absorbed silently, so B stays in
        # flight. Draining AFTER the await reopens exactly this window.
        # Guarded + best-effort: a drain hiccup must not block the resolve.
        # Mirrors the drain discipline in ``_stop_tailer`` /
        # ``set_transcript_path``.
        if self._tailer is not None:
            try:
                self._tailer.drain_buffer()
            except Exception as e:
                _log(
                    f"tmux[{self.agent_name}]: StopFailure drain_buffer "
                    f"raised: {e}"
                )

        await self._handle_turn_complete(synthesized)
        return True

    async def _start_tailer(self) -> None:
        """Construct (if needed) + start the transcript tailer, then arm
        the per-spawn first-bind state.

        Called from ``_spawn_tmux_repl`` after every REPL boot —
        including ``force_restart`` / ``attempt_reconnect`` paths where
        ``_stop_tailer`` previously ran but the tailer **instance** is
        intentionally retained so stats and last-known path survive.

        Two responsibilities, split clearly:

        1. **Construction (first call only):** discover an initial
           transcript path, build the ``TmuxTranscriptTailer``, seek
           to EOF on an existing file (or accept the placeholder for
           cold start). Subsequent calls re-use the instance.
        2. **Per-spawn arming (every call):** arm
           ``_tailer_first_bind_pending = True`` and (re)schedule the
           ``#565`` delayed recovery task. This MUST run on every
           ``_start_tailer`` invocation, not just the first one —
           Murzik's PR #566 round-1 review pointed out that the
           retained-instance respawn path skipped both pieces of
           setup, silently breaking ``#564``'s first-bind seek and
           ``#565``'s recovery for any second-and-later spawn.

        See ``set_transcript_path`` for what consumes the flag and
        ``_attempt_first_bind_recovery`` for what the scheduled task
        does on the deadline.
        """
        if self._tailer is None:
            # ── Construction (first call only) ──────────────────────
            guessed = self._discover_transcript_path()
            # Even if guessed is None (cold start, no transcript yet)
            # we still construct the tailer so ``notify_tail()`` works
            # as soon as the SessionStart hook reports a path. Use a
            # placeholder path that ``.exists()`` returns False for —
            # the tailer's read_once handles that gracefully.
            path = guessed or _PLACEHOLDER_TRANSCRIPT_PATH
            self._tailer = TmuxTranscriptTailer(
                transcript_path=path,
                on_turn_complete=self._handle_turn_complete,
                agent_name=self.agent_name,
                # #515 self-heal: hand the tailer our discovery
                # callback so it can mtime-scan and rebind on its own
                # if the SessionStart hook never fires. Closes the
                # placeholder-flavor gap; the stale-real-path flavor
                # is covered by ``_attempt_first_bind_recovery``
                # (issue #565).
                path_discovery=self._discover_transcript_path,
            )
            await self._tailer.start()
            if guessed is None:
                _log(
                    f"tmux[{self.agent_name}]: tailer started with placeholder "
                    f"path — awaiting SessionStart hook to report actual transcript"
                )
            else:
                # Seek to EOF on the existing file so we don't replay
                # historical turns on a warm-wake / resume. The
                # SessionStart hook (or the #565 delayed recovery)
                # can ``set_offset(0)`` if a fresh backfill is wanted.
                try:
                    self._tailer.set_offset(guessed.stat().st_size)
                except OSError:
                    # File disappeared between exists() check and
                    # stat() — race with Claude Code rotating /
                    # clearing the project dir. Fall through with
                    # offset=0; the hook will reset us shortly.
                    pass
                _log(
                    f"tmux[{self.agent_name}]: tailer started at {guessed} "
                    f"(offset={self._tailer.offset})"
                )
        else:
            # ── Re-spawn (force_restart, attempt_reconnect) ─────────
            # Tailer instance retained across ``_stop_tailer``;
            # restart its background task. Path + offset are
            # intentionally preserved so a same-path resume sees its
            # own EOF (Murzik's PR #496 round-3 Case 2'' relies on
            # the path-equality guard in ``set_transcript_path``).
            # The new REPL's path may differ (force_fresh_context_once
            # creates a new JSONL); the per-spawn arming below lets
            # the upcoming ``set_transcript_path`` or the delayed
            # recovery handle that rebind correctly.
            await self._tailer.start()

        # ── Per-spawn arming (every call) ───────────────────────────
        # Issue #563/#564: arm the first-bind flag so the next
        # ``set_transcript_path`` call (or the #565 delayed recovery)
        # can seek to byte 0 on fresh launches. Pre-PR-#566-round-2
        # this lived inside the construction branch — Murzik's review
        # caught that ``force_restart()`` → ``_stop_tailer`` →
        # ``_start_tailer`` (retained instance) skipped the arming.
        # Result was that any second-or-later fresh-launch spawn
        # silently lost the #564 first-bind seek AND the #565
        # delayed recovery for the rest of its lifetime.
        self._tailer_first_bind_pending = True

        # Issue #570: reset the wake-prompt readiness gate to a fresh
        # unset Event on every spawn. The previous spawn's event may
        # have been set (SessionStart hook fired) or pending (hook
        # never arrived) — either way it's stale for the new REPL.
        # Reassigning the binding is safe under asyncio's
        # single-threaded model: no awaiter can hold a reference to
        # the old Event between this line and the next paste because
        # the worker that would await it is started AFTER
        # ``_start_tailer`` returns (see ``_spawn_tmux_repl`` order).
        # Plan/Murzik review note: don't try to ``.clear()`` the
        # existing event — a stale waiter from the old spawn could
        # race-reset it back to unset while the new spawn's hook is
        # firing. Fresh binding is unambiguous.
        self._session_ready_event = asyncio.Event()

        # Issue #565: schedule a fresh delayed recovery for this
        # spawn. Cancel any leftover task from a previous spawn
        # defensively — ``_stop_tailer`` also cancels, but a future
        # caller might skip ``_stop_tailer``, and double-scheduling
        # would race two recoveries against one spawn.
        if (
            self._first_bind_recovery_task is not None
            and not self._first_bind_recovery_task.done()
        ):
            self._first_bind_recovery_task.cancel()
        self._first_bind_recovery_task = asyncio.create_task(
            self._delayed_first_bind_recovery()
        )

    async def _delayed_first_bind_recovery(self) -> None:
        """Issue #565 — wait, then attempt first-bind recovery.

        Sleeps for ``_FIRST_BIND_RECOVERY_DELAY_SEC`` and then calls
        ``_attempt_first_bind_recovery()``. Split from the sync
        recovery method so tests can exercise the recovery logic
        without dealing with timer-based scheduling.

        Cancellation during the sleep is the expected unwind on
        ``_stop_tailer``: ``asyncio.CancelledError`` propagates so the
        task is marked cancelled (don't swallow it — that would mask
        the intent and confuse anything inspecting the task state).
        Any non-cancel exception from ``_attempt_first_bind_recovery``
        is caught and logged; the task must not crash unhandled.
        """
        await asyncio.sleep(_FIRST_BIND_RECOVERY_DELAY_SEC)
        try:
            self._attempt_first_bind_recovery()
        except Exception as e:  # defensive — must never crash a task
            _log(
                f"tmux[{self.agent_name}]: #565 first-bind recovery raised "
                f"({type(e).__name__}: {e})"
            )

    def _attempt_first_bind_recovery(self) -> None:
        """Issue #565 — recover from the bind-never-arrives case on a
        fresh launch with prior history.

        The pre-#565 self-heal in ``TmuxTranscriptTailer`` only fires
        when the current watched path is **missing**. That covers the
        cold-start placeholder flavor (the placeholder path doesn't
        exist on disk). It does **not** cover the fresh-launch-with-
        prior-history flavor: ``_start_tailer`` discovers an OLD real
        JSONL via mtime scan, seeks the tailer to its EOF, and waits
        for the SessionStart hook. If the hook never arrives, the
        tailer remains bound to the stale path forever — the existing
        self-heal's ``self._path.exists()`` early-return blocks it.

        Recovery decision needs ``_tailer_first_bind_pending`` and
        ``_last_launch_used_continue``, which the tailer doesn't know
        about — keep it here at ``TmuxSession``. Route the rebind
        through ``set_transcript_path`` so the existing first-bind
        seek-to-start path (PR #564) handles the seek + flag-consume,
        and so the #496 continue-launch reply-spam defense remains
        intact for the predicate-evaluates-False branch.

        No-op when:
          - Continue launch (predicate-False, EOF defense preserved).
          - First-bind flag already consumed by the explicit hook.
          - Tailer has been torn down (``_stop_tailer`` ran).
          - Discovery returns None (no real transcript on disk yet).
          - Discovery returns the same path we're already on.
        """
        # Guard: only fresh launches need recovery — continue launches
        # already seek EOF for #496 reply-spam defense.
        if self._last_launch_used_continue:
            return
        # Guard: explicit hook bind already arrived → flag consumed →
        # nothing to recover.
        if not self._tailer_first_bind_pending:
            return
        # Guard: tailer was torn down before the deadline (e.g.
        # ``_stop_tailer`` was called between sleep completion and the
        # recovery firing). Cancellation usually catches this, but
        # the gap between sleep return and ``_attempt`` is non-zero.
        if self._tailer is None:
            return
        try:
            discovered = self._discover_transcript_path()
        except Exception as e:
            _log(
                f"tmux[{self.agent_name}]: #565 recovery discovery raised "
                f"({type(e).__name__}: {e})"
            )
            return
        if discovered is None:
            return
        # No-change → no work. The tailer's own equality guard would
        # handle this, but checking here keeps the log noise honest.
        if Path(discovered) == Path(self._tailer.transcript_path):
            return
        _log(
            f"tmux[{self.agent_name}]: #565 first-bind recovery — no "
            f"explicit bind in {_FIRST_BIND_RECOVERY_DELAY_SEC}s, "
            f"rebinding {self._tailer.transcript_path} → {discovered}"
        )
        # Routes through the standard first-bind path → seeks to byte 0
        # and consumes the ``_tailer_first_bind_pending`` flag (PR #564).
        self.set_transcript_path(discovered)

    async def _stop_tailer(self) -> None:
        """Stop the tailer if running. Idempotent.

        Murzik's PR #496 round-3 Case 2'' fix: ALSO drain the tailer's
        in-progress turn buffer. The round-2 drain inside
        ``set_transcript_path`` only fires when the path actually
        changes — but ``claude --continue`` after ``force_restart``
        resumes the same JSONL path, so the path-equality guard skips
        the drain and partial assistant text from the killed session
        would survive into the next session's first turn.

        ``_stop_tailer`` is the single semantic "session ended"
        boundary that covers both the new-path and same-path cases —
        drain here unconditionally and the path-equality guard in
        ``set_transcript_path`` becomes belt-and-suspenders rather
        than the sole defense.

        Issue #565: cancel any pending first-bind recovery task before
        the tailer goes away — otherwise the task can wake after
        ``_stop_tailer`` and call ``set_transcript_path`` against a
        stopped tailer. The ``_attempt_first_bind_recovery`` method
        also re-checks ``self._tailer is None`` defensively.
        """
        if (
            self._first_bind_recovery_task is not None
            and not self._first_bind_recovery_task.done()
        ):
            self._first_bind_recovery_task.cancel()
        self._first_bind_recovery_task = None
        if self._tailer is not None:
            try:
                await self._tailer.stop()
            except Exception as e:
                _log(f"tmux[{self.agent_name}]: tailer.stop raised: {e}")
            # Discard any partial turn state. Doing this AFTER stop()
            # means we don't race the tail loop's _read_and_dispatch
            # (the loop is cancelled by stop()).
            try:
                self._tailer.drain_buffer()
            except Exception as e:
                _log(
                    f"tmux[{self.agent_name}]: tailer.drain_buffer raised: {e}"
                )
            # Keep the instance — notify_tail() before next spawn is a no-op
            # but reusing the instance preserves stats across reconnects.

    def _project_dir(self) -> Path:
        """Return Claude Code's ``~/.claude/projects/<encoded-cwd>`` path
        for this agent's working_dir. The directory may not exist yet —
        callers must handle that case.

        ``encoded-cwd``: Claude Code slugs the absolute cwd by replacing
        every non-alphanumeric character with ``-`` (the JS encoder is
        ``cwd.replace(/[^a-zA-Z0-9]/g, '-')``). For an absolute path the
        leading ``/`` therefore becomes the leading ``-`` — e.g.
        ``/Users/oleg/foo`` → ``-Users-oleg-foo`` and
        ``/Users/oleg/.pulse-v2/x`` → ``-Users-oleg--pulse-v2-x`` (the
        dot collapses to a dash too). Mirroring that exactly is what
        lets the glob target the real directory.

        **History (this is a real bug fix, not cosmetics):** the prior
        implementation was ``"-" + str(cwd).replace("/", "-")``. Because
        ``str(cwd)`` already starts with ``/`` (which the replace turns
        into a leading ``-``), prepending another ``-`` produced a
        *double-dash* path (``--Users-oleg-...``) that never exists on
        disk. ``_has_prior_transcript()`` then always returned False, so
        ``_build_claude_cmd`` never passed ``--continue`` — every tmux
        restart silently cold-started a fresh conversation, dropping all
        prior context. It also dropped dot-containing paths
        (``.pulse-v2``). Using Claude Code's actual slug algorithm fixes
        both. See ``test_project_dir_matches_claude_code_encoding``.
        """
        cwd = Path(self._config.working_dir or ".").resolve()
        # Match Claude Code's encoder exactly: every non-alphanumeric char
        # → '-'. For an absolute path the leading '/' yields the leading
        # '-' on its own; do NOT prepend an extra dash (that was the bug).
        encoded = re.sub(r"[^a-zA-Z0-9]", "-", str(cwd))
        # Container agents (#638): claude runs with CLAUDE_CONFIG_DIR set to
        # <working_dir>/.claude-container INSIDE the container — and because
        # the working_dir is bind-mounted at the SAME absolute path, that
        # config dir (and the transcripts under its projects/) is visible to
        # this host-side daemon at the identical path. Without this branch
        # the tailer looks in the daemon user's ~/.claude, finds nothing,
        # and the whole response pipeline is dead for container agents.
        # NOTE: the slug still encodes the agent's cwd — identical in- and
        # out-of-container because of the same-path mount.
        if self._container_agent() is not None:
            wd = (self._config.working_dir or "").strip()
            if wd and Path(wd).is_absolute():
                from pinky_daemon.provisioning import container_config_dir

                return Path(container_config_dir(wd)) / "projects" / encoded
        return Path.home() / ".claude" / "projects" / encoded

    def _has_prior_transcript(self) -> bool:
        """True iff at least one ``*.jsonl`` transcript exists for this
        agent's cwd. Used by ``_build_claude_cmd`` to decide whether
        ``claude --continue`` is safe (issue #511).

        ``claude --continue`` exits with code 1 when no prior transcript
        exists for cwd. On detached tmux that exit silently reaps the
        session (the command ran and exited, no remain-on-exit) while
        ``tmux new-session`` itself returned 0 — leaving the Python
        state machine in CONNECTED against a dead REPL. Gate
        ``--continue`` on this check to avoid that wedge.
        """
        project_dir = self._project_dir()
        if not project_dir.exists():
            return False
        try:
            return any(project_dir.glob("*.jsonl"))
        except OSError:
            return False

    def _discover_transcript_path(self) -> Path | None:
        """Best-effort guess at the transcript path before SessionStart
        hook reports it.

        Claude Code stores transcripts at
        ``~/.claude/projects/<encoded-cwd>/<session-id>.jsonl``. We glob
        the project dir (see ``_project_dir``) and return the newest
        .jsonl. If none exist yet (cold start before claude writes
        anything) returns None; the SessionStart hook will repoint us
        once it fires.

        Assumption: each PinkyBot agent has a unique working_dir
        (``data/agents/<name>/`` by convention). If two agents ever
        share a cwd, this mtime-glob would cross-talk and the wrong
        agent's tailer might be repointed at another's transcript. The
        SessionStart hook's path-update is the authoritative correction
        either way; this is a startup race window only.
        """
        project_dir = self._project_dir()
        if not project_dir.exists():
            return None
        try:
            jsonls = sorted(
                project_dir.glob("*.jsonl"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
        except OSError:
            return None
        return jsonls[0] if jsonls else None

    async def _message_worker(self) -> None:
        """Drain ``_message_queue``, pasting each turn into the tmux pane.

        **#560 — concurrent dispatch.** Pre-#560 the worker dispatched
        each turn and then awaited ``_turn_done`` before pulling the
        next one. That serialization protected the single
        ``_inflight_meta`` cell from being clobbered (Pushok's PR #496
        round-2 fix), at the cost of making mid-turn steering impossible
        — a second ``send()`` while a turn ran sat invisibly in the
        queue until the first turn's stop_hook_summary landed.

        Under the deque-based design the worker no longer awaits
        between dispatches. ``_deliver_turn`` appends each successful
        paste's meta to ``_inflight_metas``; ``_handle_turn_complete``
        pops them FIFO. The watchdog (``_inflight_watchdog``) handles
        the "stop hook never fires" failure mode by aging the deque
        head and force_restarting if it exceeds ``_TURN_DONE_TIMEOUT_SEC``
        — replacing the pre-#560 per-iter timeout.

        Murzik #522 round-1 (data-loss fix), preserved: the worker keeps
        the current turn IN-HAND across transient failures via
        ``self._inflight_turn``. The previous shape — ``get()`` a turn,
        run ``_deliver_turn``, let any exception fall through the
        catch-all — silently dropped messages when the context-lock
        gate raised: the queue had already coughed up the message,
        and the except handler logged-but-didn't-requeue. The new
        shape:

        - Only ``get()`` from the queue when ``_inflight_turn is None``.
        - ``_ContextLockDeferral`` is TRANSIENT — sleep
          ``_TRANSIENT_RETRY_BACKOFF_SEC`` and loop without touching
          ``_inflight_turn``, so the next iteration retries the SAME
          turn against the SAME REPL.
        - Any other exception (paste-fail, dead-pane, etc.) is treated
          as PERMANENT — clear ``_inflight_turn`` and follow the
          existing handler semantics (disconnect on dead-pane).

        Note: prior to #525, there was a pre-paste idle-prompt readiness
        gate (#522) and a rate-limit-wait band-aid (#524). Both were
        removed: the gate waited for a pane signal (bare ``❯``) that
        Claude Code's splash never produces, so it killed every cold-
        start. ``paste_text`` is designed to handle splash-state paste
        (splash dismisses on input focus); we trust that path.
        """
        _log(f"tmux[{self.agent_name}]: message worker started")
        delivery_timeouts = 0
        try:
            while self.state == SessionState.CONNECTED:
                # Only pull a new turn when nothing is inflight. After
                # a transient failure or a force_restart, ``_inflight_turn``
                # carries the previous turn so it gets retried instead of
                # silently dropped (Murzik #522 round-1).
                if self._inflight_turn is None:
                    self._inflight_turn = await self._message_queue.get()
                    delivery_timeouts = 0
                turn = self._inflight_turn
                try:
                    self._processing = True
                    await self._deliver_turn(turn)
                    self._stats["turns"] += 1
                    # Success — paste landed, meta appended to the
                    # deque. ``_has_completed_turn`` advances when the
                    # first stop_hook_summary pops anything (see
                    # ``_handle_turn_complete``). Worker clears its
                    # in-hand turn and immediately iterates to the
                    # next queued message — no _turn_done wait under
                    # #560. CC's native queued-prompt feature absorbs
                    # the second/third/Nth pasted turn while the first
                    # is still running.
                    self._inflight_turn = None
                except _ContextLockDeferral as e:
                    # Transient: lock file present. Don't touch
                    # _inflight_turn or any deque state — _deliver_turn
                    # raised BEFORE pasting, so no meta was appended.
                    _log(
                        f"tmux[{self.agent_name}]: turn deferred "
                        f"(context lock); retrying in "
                        f"{_TRANSIENT_RETRY_BACKOFF_SEC}s ({e})"
                    )
                    await asyncio.sleep(_TRANSIENT_RETRY_BACKOFF_SEC)
                    continue
                except Exception as e:
                    # A tmux command timeout (``_run``'s 5s subprocess
                    # ceiling) is transient - a busy tmux server must not
                    # cost the user their message. Keep the turn in hand
                    # and retry with a bounded budget; ``_deliver_turn``
                    # raised before appending any meta, so the retry is
                    # state-clean.
                    if (
                        isinstance(e, TimeoutError)
                        and delivery_timeouts + 1 < _DELIVERY_TIMEOUT_RETRY_LIMIT
                    ):
                        delivery_timeouts += 1
                        # DUPLICATE-SUBMIT WINDOW: a timeout on the final
                        # send-keys Enter can expire after tmux already
                        # processed the paste+submit; re-pasting would
                        # then run a side-effecting turn twice. Check the
                        # pane for the pasted prompt first -- if it is
                        # there, finish bookkeeping instead of re-pasting
                        # (an extra Enter submits a parked prompt and is
                        # a no-op on an empty input box).
                        if await self._timed_out_turn_landed(turn):
                            _log(
                                f"tmux[{self.agent_name}]: delivery timed "
                                f"out but the prompt reached the pane; "
                                f"recording delivery instead of re-pasting"
                            )
                            try:
                                await self._tmux.send_keys("", enter=True)
                            except Exception as enter_e:
                                _log(
                                    f"tmux[{self.agent_name}]: post-timeout "
                                    f"submit Enter failed: {enter_e}"
                                )
                            self._finish_turn_delivery(turn)
                            self._stats["turns"] += 1
                            self._inflight_turn = None
                            continue
                        _log(
                            f"tmux[{self.agent_name}]: turn delivery timed "
                            f"out (attempt {delivery_timeouts}/"
                            f"{_DELIVERY_TIMEOUT_RETRY_LIMIT}); retrying in "
                            f"{_TRANSIENT_RETRY_BACKOFF_SEC}s"
                        )
                        await asyncio.sleep(_TRANSIENT_RETRY_BACKOFF_SEC)
                        continue
                    # Permanent failure (paste-buffer/send-keys failed,
                    # dead-pane, tailer-state corruption, etc.). Drop
                    # the inflight turn so we don't redeliver into a
                    # broken pane on the next iteration.
                    self._stats["errors"] += 1
                    _log(f"tmux[{self.agent_name}]: turn delivery raised: {e}")
                    # _deliver_turn already re-armed _turn_done and
                    # fired the per-turn completion_event on the explicit
                    # !ok branch (Murzik review point #2); defensively
                    # re-arm _turn_done here in case some other path
                    # raised (e.g. tailer state corruption, paste_text
                    # itself raising before the !ok handler ran).
                    self._turn_done.set()
                    # Issue #547: a wait_for_completion=True caller for
                    # THIS turn must unblock even when delivery raised
                    # before _deliver_turn's own completion_event branch.
                    # Idempotent — .set() on an already-set Event is a
                    # no-op.
                    if (
                        turn.completion_event is not None
                        and not turn.completion_event.is_set()
                    ):
                        turn.completion_event.set()
                    # The message is being dropped; tell the chat that
                    # sent it instead of leaving the user with dead
                    # silence (daemon-log-only failures are invisible
                    # from Telegram/Discord).
                    await self._notify_delivery_failure(turn)
                    self._inflight_turn = None
                    # Task #90: dead-pane/dead-container already scheduled
                    # disconnect from inside _deliver_turn. Exit the worker
                    # cleanly so we don't retry into the now-being-torn-down
                    # pane. The watchdog also exits when CONNECTED → DEAD.
                    if _is_dead_runtime_stderr(str(e)):
                        return
                finally:
                    self._processing = False
        except asyncio.CancelledError:
            _log(f"tmux[{self.agent_name}]: worker cancelled")
        except Exception as e:
            _log(f"tmux[{self.agent_name}]: worker error: {e}")

    async def _notify_delivery_failure(self, turn: _QueuedTurn) -> None:
        """Route a delivery-failure notice back to the chat that sent
        ``turn``.

        Called when the worker gives up on an external turn (permanent
        paste failure or exhausted timeout retries). The message was
        already popped from ``_message_queue`` and will not be
        redelivered; without this the sender gets no signal at all.
        Internal turns have no chat target, so they are skipped.
        Failure-tolerant: a broken callback must not take the worker
        down with it.
        """
        if turn.internal or not self._response_callback:
            return
        notice = TurnResponse(
            agent_name=self.agent_name,
            session_id=self.id,
            platform=turn.platform,
            chat_id=turn.chat_id,
            message_id=turn.message_id,
            text=(
                "[delivery error] Your message could not be delivered to "
                "the agent's session and was dropped. Please resend it."
            ),
            stop_reason="delivery_error",
        )
        try:
            result = self._response_callback(notice)
            if asyncio.iscoroutine(result):
                await result
        except Exception as e:
            _log(
                f"tmux[{self.agent_name}]: delivery-failure notice "
                f"callback raised: {e}"
            )

    async def _timed_out_turn_landed(self, turn: _QueuedTurn) -> bool:
        """Capture-pane check: did a timed-out delivery actually land?

        A tmux command timeout can expire AFTER tmux processed the
        command -- notably ``paste_text``'s final send-keys Enter -- so
        blindly re-pasting would submit the turn a second time and
        side-effecting instructions would run twice. Look for the head
        of the prompt's first line in the pane: if it is visible, the
        paste reached the pane (parked in the input area or already
        submitted into the scrollback) and the worker must NOT re-paste.

        Returns False when the probe fails or the marker is too short
        to be unambiguous -- the worker then falls back to a plain
        retry, accepting the narrow duplicate window over the certainty
        of a dropped message. Best-effort by design: a capture-pane
        that itself times out yields False, never an exception.
        """
        marker = ""
        for line in turn.prompt.splitlines():
            line = line.strip()
            if line:
                marker = line[:_PANE_MARKER_CHARS]
                break
        if len(marker) < _PANE_MARKER_MIN_CHARS:
            return False
        try:
            result = await self._tmux.capture_pane()
        except Exception:
            return False
        return result.ok and marker in (result.stdout or "")

    def _transcript_recently_grew(self, now: float, window: float) -> bool:
        """True if the transcript file was written within ``window`` seconds.

        A growing transcript means the REPL is actively emitting output (a
        long or streaming turn), so it is NOT wedged. Returns False when the
        path is the cold-start placeholder, missing, or unstattable —
        absence of evidence is treated as "not growing" so the caller falls
        through to the idle/wedged checks rather than masking a real stall.
        """
        tailer = self._tailer
        path = getattr(tailer, "transcript_path", None) if tailer else None
        if not path:
            return False
        try:
            mtime = Path(path).stat().st_mtime
        except OSError:
            return False
        return (now - mtime) < window

    def _background_tasks_recently_active(self, now: float, window: float) -> bool:
        """True if a background task wrote a transcript within ``window`` seconds.

        A blocking turn can be legitimately busy with NO main-transcript output:
        the REPL is parked on a long-running background task (a Dynamic
        Workflow, or an ``Agent`` / background tool call) whose subagents stream
        to their OWN transcript files, not the main one.
        ``_transcript_recently_grew`` only watches the main transcript, so such
        a turn looks "quiet" and the watchdog would force_restart it — killing
        the in-flight background work — ~``_TURN_DONE_TIMEOUT_SEC`` in (#692).
        This extends the "still producing output" evidence to background-task
        transcripts.

        Layout: Claude Code writes the main transcript at ``<session>.jsonl``
        and puts subagent/workflow transcripts under the sibling ``<session>/``
        directory (``subagents/`` and ``workflows/``). We derive that directory
        from the tailer's transcript path and look for any entry modified within
        the window, short-circuiting on the first hit. Absence of evidence →
        False (same convention as ``_transcript_recently_grew``: fall through to
        the idle/wedged checks rather than masking a real stall).
        """
        tailer = self._tailer
        path = getattr(tailer, "transcript_path", None) if tailer else None
        if not path:
            return False
        try:
            path = Path(path)
        except (TypeError, ValueError):
            return False
        name = path.name
        if not name.endswith(".jsonl"):
            return False
        # ``<session>.jsonl`` → sibling ``<session>/`` dir holding background work.
        session_dir = path.with_name(name[: -len(".jsonl")])
        cutoff = now - window
        for sub in ("subagents", "workflows"):
            root = session_dir / sub
            try:
                if not root.is_dir():
                    continue
                for entry in root.rglob("*"):
                    try:
                        if entry.stat().st_mtime >= cutoff:
                            return True
                    except OSError:
                        continue
            except OSError:
                continue
        return False

    def _foreground_tool_in_flight(self, now: float) -> bool:
        """True if a FOREGROUND tool call is still running (#731).

        A single long blocking foreground tool call (e.g. a deliberate
        ``gh run watch`` up to ~10 min, or a slow build) writes nothing to the
        main transcript until it returns and — unlike a Workflow/Agent — spawns
        no subagent transcript, so both ``_transcript_recently_grew`` and
        ``_background_tasks_recently_active`` read it as "quiet". With the REPL
        legitimately ``working`` that is indistinguishable from a wedge, and the
        watchdog force_restarts a healthy turn, SIGKILLing the tool child (#731).

        The PreToolUse/PostToolUse hooks (task #93) already POST tool-start and
        tool-finish to the daemon, so ``_inflight_tool_calls`` holds the
        ``tool_use_id``s that have started but not finished — an authoritative
        "a tool is genuinely running" signal. We credit that as liveness, the
        same carve-out background tasks get.

        Bounded by ``_FOREGROUND_TOOL_ACTIVE_CEILING_SEC``: an entry older than
        the ceiling is a lost finish-POST or a genuinely hung child, so it is
        NOT credited and is pruned here (keeping the set bounded). A real stuck
        REPL therefore still recovers — just one ceiling-window later.
        """
        if not self._inflight_tool_calls:
            return False
        alive = False
        for tool_use_id, started_at in list(self._inflight_tool_calls.items()):
            if (now - started_at) >= _FOREGROUND_TOOL_ACTIVE_CEILING_SEC:
                # Suspected lost finish / hung child — stop crediting + prune.
                del self._inflight_tool_calls[tool_use_id]
                continue
            alive = True
        return alive

    def _inflight_stall_verdict(self, now: float) -> str:
        """Classify a possibly-stalled inflight head for the watchdog (#118).

        Returns one of:
          - ``"ok"``      — head not (yet) aged past ``_TURN_DONE_TIMEOUT_SEC``.
          - ``"growing"`` — aged out BUT the main transcript is still being
                            written, OR a background task (a Workflow / Agent
                            tool call) is still writing its own subagent
                            transcript (#692), OR a foreground tool call is
                            still in flight (#731) → a long/streaming,
                            background-busy, or foreground-tool-busy turn, not
                            wedged.
          - ``"idle"``    — aged out, transcript quiet, and Claude Code last
                            reported *idle* (Stop hook) at-or-after this head
                            started → the REPL finished and is waiting for
                            input, so the lingering meta(s) are phantom (a
                            paste with no matching stop_hook). Reconcile, don't
                            restart.
          - ``"wedged"``  — aged out, transcript quiet, REPL not idle →
                            genuinely stuck; force_restart.

        Brad's directive (#118): never tear a session down unless it is
        *actually* wedged. ``growing`` and ``idle`` are the two "positive
        evidence it's fine" carve-outs that stop the watchdog from
        force-restarting a healthy session just because the deque count
        drifted (paste-vs-stop_hook) or a turn ran long. When the liveness
        signals are unavailable (e.g. no ``live_status_fn`` wired in tests),
        the verdict falls through to ``"wedged"`` — preserving the original
        stuck-REPL recovery behavior.
        """
        if not self._inflight_metas or self._head_started_at is None:
            return "ok"
        if (now - self._head_started_at) <= _TURN_DONE_TIMEOUT_SEC:
            return "ok"
        # (a) Still producing output? Long/streaming turn — not wedged.
        if self._transcript_recently_grew(now, _TURN_DONE_TIMEOUT_SEC):
            return "growing"
        # (a2) Parked on a long-running BACKGROUND task (Workflow / Agent tool)?
        # Its subagents stream to their own transcripts, leaving the MAIN one
        # quiet, but the REPL is legitimately busy — not wedged (#692). Checked
        # BEFORE the idle reconcile so an actively-working background turn is
        # never drained as a phantom.
        if self._background_tasks_recently_active(
            now, _BACKGROUND_TASK_ACTIVE_WINDOW_SEC
        ):
            return "growing"
        # (a3) Parked on a long-running FOREGROUND tool call (#731)? The
        # PreToolUse/PostToolUse hooks (task #93) track in-flight tool_use_ids;
        # a tool that has started but not finished (within the ceiling) is
        # genuine liveness — extend, don't restart. Checked before the idle
        # reconcile for the same reason as (a2): an actively-working foreground
        # turn must never be drained as a phantom.
        if self._foreground_tool_in_flight(now):
            return "growing"
        # (b) REPL reported idle? Consult Claude Code's working/idle hook
        # signal (Stop hook → "idle"; PreToolUse/etc → "working"). An idle
        # REPL has nothing in flight. Require the idle to be at-least-as-recent
        # as when the CURRENT head was pasted — otherwise a turn was pasted
        # that the REPL never came alive for (hang-on-paste), which IS a wedge.
        live = None
        fn = getattr(self._config, "live_status_fn", None)
        if fn is not None:
            try:
                live = fn()
            except Exception:
                live = None
        if live and live.get("status") == "idle":
            last_updated = live.get("last_updated") or 0.0
            # Floor the idle-freshness check at when the current head was
            # actually pasted (#118 / Murzik round-2). The earlier of:
            #   - ``head.dispatched_at`` — this turn's paste+Enter time, and
            #   - ``_head_started_at``    — the deque-head transition clock.
            # For a queued turn that inherited the head spot, dispatched_at
            # (paste time, while the prior head was still running) predates
            # the head re-base, so ``min`` picks it and still tolerates
            # tailer/status ordering jitter for queued turns. For a fresh
            # first turn into an empty deque the two are equal, so a STALE
            # idle left over from the PREVIOUS turn (reported BEFORE this turn
            # was pasted) is correctly rejected → wedged. No fixed slack
            # window: both timestamps come from the same daemon clock (no
            # skew), and a turn's own idle always postdates its own paste, so
            # an idle that predates the paste cannot belong to this turn.
            head = self._inflight_metas[0]
            idle_floor = min(self._head_started_at, head.dispatched_at)
            if last_updated >= idle_floor:
                return "idle"
            # (#592) Secondary: the Stop hook may have fired for this turn
            # but failed to advance live_status.last_updated (concurrent-
            # dispatch phantom — e.g. two turns complete close together and
            # the second hook's write is lost). Transcript evidence is more
            # reliable: if the transcript grew meaningfully AFTER this head's
            # paste, the REPL was active on this turn and has since gone idle,
            # so the lingering meta is phantom. _TRANSCRIPT_PASTE_SLACK guards
            # against the paste echo itself (~0–1 s) triggering the check —
            # a hang-on-paste (REPL never processed the turn) stays at the
            # paste-echo level and is still classified ``"wedged"``.
            # Baseline = max(file mtime at paste, daemon-clock paste time). The
            # daemon stamp anchors the floor to THIS turn even when the JSONL
            # write lags the tmux paste; without it a stale previous-turn mtime
            # could let a real hang-on-paste's echo clear the slack (#595 review).
            baseline = head.paste_succeeded_at
            mtime_at = head.transcript_mtime_at_paste
            if mtime_at is not None and (baseline is None or mtime_at > baseline):
                baseline = mtime_at
            if baseline is not None:
                _t = self._tailer
                _tp = getattr(_t, "transcript_path", None) if _t else None
                if _tp:
                    try:
                        if Path(_tp).stat().st_mtime > baseline + _TRANSCRIPT_PASTE_SLACK:
                            return "idle"
                    except OSError:
                        pass
        self._log_wedged_inputs(now, live)
        return "wedged"

    def _log_wedged_inputs(self, now: float, live: dict | None) -> None:
        """Dump verdict inputs at the wedged decision point (#592).

        Why: distinguishes (A) stale-idle from (B) stuck-working false-positives
        in production logs without changing classifier behavior. Read alongside
        the existing "REPL stuck; scheduling force_restart" line to confirm
        which case fired.
        """
        head_dispatched_at: float | None = None
        if self._inflight_metas:
            head_dispatched_at = getattr(
                self._inflight_metas[0], "dispatched_at", None
            )
        live_status = live.get("status") if live else None
        live_last_updated = live.get("last_updated") if live else None
        idle_floor: float | None = None
        if self._head_started_at is not None and head_dispatched_at is not None:
            idle_floor = min(self._head_started_at, head_dispatched_at)
        transcript_mtime: float | None = None
        tailer = self._tailer
        transcript_path = (
            getattr(tailer, "transcript_path", None) if tailer else None
        )
        if transcript_path:
            try:
                transcript_mtime = Path(transcript_path).stat().st_mtime
            except OSError:
                pass
        age = (
            (now - self._head_started_at)
            if self._head_started_at is not None
            else None
        )
        age_str = f"{age:.1f}" if age is not None else "None"
        _log(
            f"tmux[{self.agent_name}]: verdict_wedged_inputs "
            f"live_status={live_status!r} "
            f"live_last_updated={live_last_updated} "
            f"idle_floor={idle_floor} "
            f"head_dispatched_at={head_dispatched_at} "
            f"head_started_at={self._head_started_at} "
            f"transcript_mtime={transcript_mtime} "
            f"age_s={age_str} "
            f"depth={len(self._inflight_metas)} "
            f"inflight_tools={len(self._inflight_tool_calls)}"
        )

    async def _inflight_watchdog(self) -> None:
        """Age the ``_inflight_metas`` head; force_restart if it sticks.

        Issue #560 — replaces the per-iter ``_turn_done`` timeout the
        worker used to enforce. With concurrent dispatch the worker no
        longer waits between turns, so the "stop hook never fires"
        failure mode needs a separate watcher.

        **Head-age, not paste-age** (Murzik review point #1). When a
        turn becomes the deque head (either by being the first append
        into an empty deque, or by inheriting the head spot after the
        previous head was popped), its ``_head_started_at`` clock
        starts. Each turn gets its own ``_TURN_DONE_TIMEOUT_SEC``
        window once it's the head — a queued turn doesn't get
        force_restarted for ageing while ANOTHER turn was running.

        **Tail requeue on timeout** (Murzik review on PR #561).
        When the head wedges, ONLY the head is abandoned — its
        ``completion_event`` fires (signal of "definitively failed"),
        but its prompt is NOT replayed. Tail entries (B, C, ... that
        were already dispatched into Claude Code's native queue but
        never got to run because A wedged) carry intact prompts +
        completion_events; we requeue them at the FRONT of
        ``_message_queue`` in FIFO order so the new worker (spawned
        by force_restart's disconnect→connect cycle) re-dispatches
        them after the restart. Their ``completion_event`` stays
        UNSET so a ``wait_for_completion=True`` caller still waits
        for the actual rerun, not for a phantom "completion" the
        watchdog falsely signaled.

        Also covers the worker's in-hand-but-not-pasted turn (e.g.
        mid context-lock retry) — that turn's meta isn't in the deque
        yet, but it must replay too. Requeued AFTER the tail entries:
        the worker is single-threaded so the in-hand turn was pulled
        from the queue AFTER the tail entries were pasted, so in
        original send-order it comes LAST. (Murzik review on commit 2:
        commit 2 had this backwards — fixed in commit 3.)

        **Atomic handoff with worker shutdown** (Murzik review on
        commit 2). The live worker is cancelled SYNCHRONOUSLY before
        the requeue is made visible to ``_message_queue``. Without
        this, the worker (parked in ``_message_queue.get()``) would
        race the post-watchdog ``force_restart()``: ``put_nowait``
        resolves the pending getter future synchronously, the worker
        wakes up and pastes B/C back into the still-wedged REPL,
        ``disconnect()``'s drain fires their completion_events on
        abandoned deque entries → loss/false-completion bug returns.
        Cancelling the worker first transitions its getter future
        to CANCELLED; ``asyncio.Queue._wakeup_next`` skips done
        waiters, so the subsequent ``put_nowait`` cannot wake it.

        On the watchdog timeout path, ``_inflight_turn`` is also
        cleared so the post-restart worker doesn't try to redeliver
        a stale reference (the requeued copy is the canonical replay).

        ``force_restart`` cancels this task as part of its
        ``disconnect`` shutdown; the new connect's ``_spawn_tmux_repl``
        respawns a fresh watchdog.
        """
        _log(f"tmux[{self.agent_name}]: inflight watchdog started")
        try:
            while self.state == SessionState.CONNECTED:
                await asyncio.sleep(_WATCHDOG_TICK_SEC)
                now = time.time()
                verdict = self._inflight_stall_verdict(now)
                if verdict == "ok":
                    continue
                age = now - (self._head_started_at or now)
                depth = len(self._inflight_metas)
                if verdict == "growing":
                    # #118: head aged out BUT the transcript is still being
                    # written — a long/streaming turn, NOT a wedge. Extend
                    # the window instead of tearing the session down.
                    self._head_started_at = now
                    _log(
                        f"tmux[{self.agent_name}]: inflight head aged {age:.1f}s "
                        f"but transcript or background task still active — not "
                        f"wedged, extending window (deque depth={depth})"
                    )
                    continue
                if verdict == "idle":
                    # #118: head aged out, transcript quiet, and the REPL last
                    # reported idle — nothing is actually in flight, so the
                    # lingering meta(s) are phantom (a paste with no matching
                    # stop_hook). Reconcile by draining + firing their
                    # completion events; do NOT restart an idle REPL. This is
                    # the core fix for "torn down ~10min after activity even
                    # though nothing was wedged."
                    drained = list(self._inflight_metas)
                    self._inflight_metas.clear()
                    self._head_started_at = None
                    for m in drained:
                        ev = m.completion_event
                        if ev is not None and not ev.is_set():
                            ev.set()
                    _log(
                        f"tmux[{self.agent_name}]: inflight head aged {age:.1f}s "
                        f"but REPL is idle — reconciled {len(drained)} phantom "
                        f"meta(s), NOT restarting (#118)"
                    )
                    continue
                # verdict == "wedged": no output + REPL not idle → genuinely
                # stuck. Fall through to the force_restart recovery path.
                _log(
                    f"tmux[{self.agent_name}]: inflight head aged {age:.1f}s "
                    f"> {_TURN_DONE_TIMEOUT_SEC}s, transcript quiet + REPL not "
                    f"idle — REPL stuck; scheduling force_restart "
                    f"(deque depth={depth})"
                )
                # Snapshot deque state before mutation so this critical
                # section is atomic from the outside (no awaits between
                # snapshot and mutation).
                head = self._inflight_metas.popleft()
                tail_entries = list(self._inflight_metas)
                self._inflight_metas.clear()
                self._head_started_at = None
                # Also capture any in-hand-but-not-pasted turn (e.g.
                # worker mid context-lock retry). Cleared so the
                # post-restart worker doesn't redeliver from the stale
                # reference.
                in_hand = self._inflight_turn
                self._inflight_turn = None

                # **CRITICAL — kill the live worker BEFORE making the
                # replay queue visible** (Murzik review on commit 2 of
                # PR #561). The worker is almost certainly parked in
                # ``_message_queue.get()``; ``put_nowait`` resolves
                # that pending getter future synchronously. If we
                # requeued first, the still-live worker would wake up,
                # pull B/C, and ``_deliver_turn`` them back into the
                # STILL-WEDGED REPL before ``force_restart()``'s
                # disconnect could cancel it. Then disconnect's drain
                # would fire B/C's completion_events on the freshly-
                # appended (and abandoned) deque entries — recreating
                # the loss/false-completion bug we're trying to fix,
                # just with a race window.
                #
                # ``Task.cancel()`` synchronously transitions the
                # task's awaited future (the queue getter) to CANCELLED.
                # ``asyncio.Queue._wakeup_next`` skips done waiters, so
                # the subsequent ``put_nowait`` cannot wake the cancelled
                # worker. The new worker spawned by ``force_restart()``'s
                # post-disconnect branch is the only consumer of the
                # replay. ``disconnect()``'s own worker cancel is
                # idempotent (no-op on an already-cancelled task).
                if self._worker_task is not None and not self._worker_task.done():
                    self._worker_task.cancel()

                # Replay list: tail entries FIRST (FIFO from deque),
                # then in_hand LAST. The worker is single-threaded —
                # it pulls one turn from the queue, pastes it (appends
                # to ``_inflight_metas``), then pulls the next. So
                # tail entries B were pasted EARLIER than whatever
                # the worker is currently holding in ``_inflight_turn``
                # (in_hand C). Original send-order: A (head) → B
                # (tail) → C (in_hand). On A timeout, replay must be
                # B then C. (Pre-paste-retry edge: deque empty, in_hand
                # is the sole entry — ``tail_entries`` is empty, so
                # in_hand becomes the lone replay entry, correct.)
                replay: list[_QueuedTurn] = []
                replay.extend(entry.turn for entry in tail_entries)
                if in_hand is not None:
                    replay.append(in_hand)
                if replay:
                    # Prepend ``replay`` to ``_message_queue``: drain
                    # current queue contents, push replay first, then
                    # the original backlog. Preserves FIFO across the
                    # boundary. ``asyncio.Queue`` has no put-front, so
                    # the drain+repush is the canonical pattern.
                    backlog: list[_QueuedTurn] = []
                    while not self._message_queue.empty():
                        try:
                            backlog.append(self._message_queue.get_nowait())
                        except asyncio.QueueEmpty:
                            break
                    for t in replay:
                        self._message_queue.put_nowait(t)
                    for t in backlog:
                        self._message_queue.put_nowait(t)
                    _log(
                        f"tmux[{self.agent_name}]: requeued "
                        f"{len(replay)} turn(s) for replay after "
                        f"force_restart (tail={len(tail_entries)}, "
                        f"in_hand={'yes' if in_hand else 'no'})"
                    )

                # HEAD ONLY: fire its completion_event. Head was
                # definitively abandoned (its prompt is NOT replayed —
                # the wedge invalidated whatever progress it made).
                # Tail entries' events stay UNSET so wait_for_completion
                # callers wait for the actual rerun, not the watchdog
                # itself. Critical contract — Murzik review on PR #561.
                if (
                    head.completion_event is not None
                    and not head.completion_event.is_set()
                ):
                    head.completion_event.set()
                self._turn_done.set()
                self._stats["errors"] += 1
                self._stats["turn_timeouts"] = (
                    self._stats.get("turn_timeouts", 0) + 1
                )
                # Schedule force_restart in the background — must NOT
                # await it here because force_restart→disconnect cancels
                # this watchdog task and awaits its completion, which
                # would deadlock.
                #
                # ``bypass_guard=True``: Murzik review on commit 3.
                # The watchdog has already (a) abandoned the head's
                # completion_event, (b) moved tail/in_hand replay into
                # ``_message_queue``, (c) cancelled the only worker.
                # If ``force_restart`` honored the persistence guard
                # and returned False, the session would stay CONNECTED
                # with no worker and no watchdog to consume the replay
                # queue or recover — silently inert. The guard exists
                # to preserve completed-but-unsaved state mid-
                # conversation; once the head has wedged for
                # ``_TURN_DONE_TIMEOUT_SEC``, that conversation state
                # is already corrupted, so the guard's premise no
                # longer holds. See ``force_restart`` docstring.
                asyncio.create_task(self.force_restart(bypass_guard=True))
                return
        except asyncio.CancelledError:
            _log(f"tmux[{self.agent_name}]: inflight watchdog cancelled")
        except Exception as e:
            _log(f"tmux[{self.agent_name}]: inflight watchdog error: {e}")

    def _context_lock_path(self) -> Path:
        """Path of this agent's daemon-level context lock file.

        See ``_TRANSPORT_LOCK_DIR`` for the directory rationale. Returns
        a path without creating it — the file's existence (not contents)
        is the signal, and creation/removal is the context manager's job.
        """
        return _TRANSPORT_LOCK_DIR / f"{self.agent_name}.lock"

    async def _deliver_turn(self, turn: _QueuedTurn) -> None:
        """Push one turn through to the tmux pane.

        PR8b: the response side is handled asynchronously by the
        transcript tailer (set up in ``_spawn_tmux_repl``). This method
        handles the inbound half — push the prompt and, on successful
        paste, **append** the routing metadata to ``_inflight_metas``
        so the tailer's ``_handle_turn_complete`` can pop it (FIFO)
        when this turn's ``stop_hook_summary`` lands.

        **#560 — concurrent dispatch.** The pre-#560 design wrote a
        single ``_inflight_meta`` dict here and waited (in the worker)
        for ``_turn_done`` to fire before dispatching the next turn.
        That gate is what made mid-turn steering impossible — a second
        send() while a turn was running sat invisibly in
        ``_message_queue`` until the first turn fully resolved. The
        deque replaces the single-cell shared mutable; the worker no
        longer waits between dispatches; each entry carries its own
        routing dict so #496 Case 1 (clobber → wrong-chat routing) is
        impossible by construction.

        Pulse-v2 port (task #92): the context-lock check still raises a
        typed transient exception the worker catches in its retry loop
        (Murzik #522 round-1). Because the worker pops the turn from
        ``_message_queue`` BEFORE calling ``_deliver_turn``, a bare
        exception would silently drop the message; the worker keeps
        the turn in ``_inflight_turn`` and re-pastes on the next
        iteration. The deferral path does NOT append to
        ``_inflight_metas`` (no paste happened, no stop hook will
        fire).

        **Context-lock check.** If the daemon-level context manager has
        touched ``data/transport-locks/<agent>.lock``, it's mid-rewrite
        of files this REPL depends on — paste would land on an
        inconsistent state. Raise ``_ContextLockDeferral`` so the worker
        preserves the inflight turn, sleeps, and retries when the lock
        is released.

        Splash-state paste handling lives in ``_TmuxControl.paste_text``
        (bracketed-paste + delayed-Enter, commit 0864f4e / issue #514).
        For non-wake turns Claude Code's splash dismisses on input
        focus, so pasting into the splash works correctly. The
        wake-prompt case is more fragile because the bracketed-paste
        + 300ms-Enter sequence can complete while CC is still in
        MCP-bootstrap (the Enter is consumed by transition state and
        the typed prompt sits in the input area unsubmitted). Issue
        #570 added a per-turn readiness gate scoped to wake_* internal
        prompts; see the ``_session_ready_event`` await below.

        **Paste-failure unblock (Murzik review point #2):** if
        ``paste_text`` reports !ok we do NOT append to
        ``_inflight_metas`` (no stop hook will fire), and we DO set
        the turn's ``completion_event`` so a ``wait_for_completion=True``
        caller (e.g. pre-sleep save) doesn't hang forever.

        **#570 wake-prompt readiness gate (Murzik #571 review).** The
        gate lives HERE at delivery time, not at enqueue time, to
        preserve queue-order FIFO across the bootstrap window. While
        the worker blocks on the gate the wake turn sits at the
        ``_message_queue`` HEAD; any external ``send()`` calls during
        the wait enqueue BEHIND the wake turn and run AFTER it.
        Gating at enqueue time would let those external messages jump
        the wake prompt (broker calls ``send`` the moment ``state ==
        CONNECTED``, which fires before this wait would have ended).
        """
        # Pulse-v2 safety primitive: context-lock check. Cheap fs stat;
        # do this first so we bail before mutating any state. The lock
        # being held is transient — Murzik #522 round-1: raise a typed
        # ``_ContextLockDeferral`` so the worker knows to PRESERVE the
        # inflight turn, sleep, and retry on the next iteration. Bare
        # ``RuntimeError`` here was being eaten by the worker's catch-
        # all + ``get()``-before-deliver pattern, silently dropping the
        # message.
        if self._context_lock_path().exists():
            raise _ContextLockDeferral(
                f"context lock present at {self._context_lock_path()} — "
                f"deferring paste; worker will retry on next iteration"
            )

        # Issue #570 — wake-prompt readiness gate. Wake_* internal
        # prompts must not paste until SessionStart confirms claude
        # is past splash + MCP-bootstrap and the input area is live
        # (otherwise the bracketed-paste + 300ms-Enter race loses the
        # Enter to transition state and the wake turn never fires).
        # Scope: only internal turns whose reason starts with "wake_"
        # — external turns and non-wake internal turns (e.g.
        # ``idle_sleep_presave``) are sent into already-live sessions
        # and skip the gate. Fallback on timeout: proceed with the
        # paste anyway (degrades to pre-#570 race rather than hanging
        # the session). The worker is single-threaded; blocking here
        # blocks the worker, which keeps the wake turn at the queue
        # head and preserves FIFO for any external messages enqueued
        # during the gate wait (Murzik #571 review).
        #
        # Observability: every wake_* turn emits one ``wake_gate``
        # activity event with subtype ``instant`` | ``opened`` |
        # ``timeout`` and metadata ``{reason, latency_ms}``. This is
        # the source for the gate-latency histogram + timeout counter
        # we need to validate the #570 fix in production and decide
        # whether the substrate stays tmux long-term. Sub-100ms log
        # suppression is preserved (operator noise) but analytics
        # records the full distribution.
        is_wake_turn = turn.internal and turn.reason.startswith("wake_")
        if is_wake_turn:
            if self._session_ready_event.is_set():
                gate_subtype = "instant"
                gate_latency_ms = 0
            else:
                _gate_start = time.monotonic()
                try:
                    await asyncio.wait_for(
                        self._session_ready_event.wait(),
                        timeout=_SESSION_READY_GATE_TIMEOUT_SEC,
                    )
                    gate_latency_ms = int((time.monotonic() - _gate_start) * 1000)
                    gate_subtype = "opened"
                    # Only log when the wait was noticeable — sub-100ms
                    # waits are uninteresting (covers the case where the
                    # hook arrived before the worker popped the turn).
                    # Above that, the duration is the diagnostic that the
                    # fix is doing work.
                    if gate_latency_ms > 100:
                        _log(
                            f"tmux[{self.agent_name}]: wake-prompt readiness "
                            f"gate opened after {gate_latency_ms}ms "
                            f"(reason={turn.reason})"
                        )
                except asyncio.TimeoutError:
                    gate_latency_ms = int(_SESSION_READY_GATE_TIMEOUT_SEC * 1000)
                    gate_subtype = "timeout"
                    _log(
                        f"tmux[{self.agent_name}]: wake-prompt readiness "
                        f"gate TIMEOUT after {_SESSION_READY_GATE_TIMEOUT_SEC}s "
                        f"— proceeding with paste anyway "
                        f"(reason={turn.reason}). Hook may have failed to "
                        f"fire; check the agent's "
                        f".claude/hook_tmux_session_start.py."
                    )

            if self._analytics_store:
                try:
                    self._analytics_store.log_activity(
                        session_id=self.id,
                        agent_name=self.agent_name,
                        event_type="wake_gate",
                        subtype=gate_subtype,
                        metadata={
                            "reason": turn.reason,
                            "latency_ms": gate_latency_ms,
                        },
                    )
                except Exception as e:  # pragma: no cover — defensive
                    _log(
                        f"tmux[{self.agent_name}]: analytics wake_gate "
                        f"emit failed ({gate_subtype}, {gate_latency_ms}ms): {e}"
                    )

        # #151 native ultracode activation. On the FIRST turn after a fresh
        # cold-start with ultracode effort, type the interactive
        # ``/effort ultracode`` into the now-ready REPL BEFORE pasting the
        # prompt — Brad's ordering: spawn → change effort → inject wake
        # context. The input area is empty at this point (no turn has pasted
        # yet), so the CLI sets effort silently; the "Change effort level?"
        # confirmation only fires mid-conversation (the prompt-cache re-read).
        # One-shot + best-effort: the flag is cleared regardless of outcome,
        # and a send failure / readiness timeout degrades to the
        # ULTRACODE_DIRECTIVE fallback rather than blocking delivery.
        if self._native_ultracode_pending:
            self._native_ultracode_pending = False
            # Raw keystrokes typed during the splash/MCP-boot phase get eaten,
            # so ensure the input area is live first. Wake turns already
            # awaited this gate above (no-op here); a non-wake first turn
            # waits here. Timeout → attempt the send anyway (context is still
            # empty; worst case the directive fallback carries the tier).
            if not self._session_ready_event.is_set():
                try:
                    await asyncio.wait_for(
                        self._session_ready_event.wait(),
                        timeout=_SESSION_READY_GATE_TIMEOUT_SEC,
                    )
                except asyncio.TimeoutError:
                    _log(
                        f"tmux[{self.agent_name}]: native /effort ultracode — "
                        f"readiness gate timeout; sending anyway"
                    )
            try:
                eff_res = await self._tmux.send_keys(
                    "/effort ultracode", enter=True
                )
                if eff_res.ok:
                    # Settle so the slash command is processed before the
                    # prompt's bracketed-paste lands in the same pane.
                    await asyncio.sleep(_NATIVE_ULTRACODE_SETTLE_SEC)
                    _log(
                        f"tmux[{self.agent_name}]: native /effort ultracode "
                        f"activated (pre-prompt)"
                    )
                else:
                    _log(
                        f"tmux[{self.agent_name}]: native /effort ultracode "
                        f"send failed (rc={eff_res.returncode}, "
                        f"stderr={(eff_res.stderr or '').strip()!r}); "
                        f"ULTRACODE_DIRECTIVE fallback remains in effect"
                    )
            except Exception as e:  # pragma: no cover — defensive
                _log(
                    f"tmux[{self.agent_name}]: native /effort ultracode raised "
                    f"({e}); continuing with prompt + directive fallback"
                )

        # Clear the back-compat ``_turn_done`` event before pasting.
        # Under #560 the worker no longer awaits this between dispatches
        # — the clear is purely for legacy observers and for tests that
        # pin the "cleared on dispatch, set on completion" pattern.
        # ``_handle_turn_complete`` re-sets it on every successful pop.
        self._turn_done.clear()

        # Use paste_text (bracketed paste + delayed Enter) instead of raw
        # send-keys (issue #514, Misha/Pulse v2 pattern). The delayed
        # Enter gives claude's cold-start splash UI time to dismiss
        # before the submit Enter arrives, so the first prompt of a
        # fresh session doesn't get wedged in claude's input buffer.
        # Wake-prompt timing is additionally protected by the readiness
        # gate above (#570 / Murzik #571 review).
        result = await self._tmux.paste_text(turn.prompt, enter=True)
        if not result.ok:
            # Send failed — no response will arrive. Re-arm turn_done
            # (back-compat) and unblock any wait_for_completion caller
            # for THIS turn so they don't hang forever — Murzik review
            # point #2.
            self._turn_done.set()
            if turn.completion_event is not None and not turn.completion_event.is_set():
                turn.completion_event.set()
            # Task #90: detect dead-pane (tmux session killed externally,
            # tmux server crashed, etc.). Without this, the worker would
            # loop forever pasting into a non-existent pane. Schedule
            # disconnect (NOT force_restart — that's gated by the
            # restart_guard from #517 and may block once we've had a
            # completed turn). The disconnect drives CONNECTED → DEAD
            # via the default-disconnect path; the next inbound
            # send_to_agent triggers the normal auto-wake cold-start
            # path (validated in production by #517/#518/#519).
            if _is_dead_runtime_stderr(result.stderr or ""):
                _log(
                    f"tmux[{self.agent_name}]: pane/container vanished "
                    f"(stderr={result.stderr.strip()!r}); scheduling disconnect"
                )
                # create_task — must not await disconnect from inside
                # the worker; disconnect cancels the worker task and
                # awaits its completion, which would deadlock here.
                asyncio.create_task(self.disconnect())
            raise RuntimeError(
                f"tmux paste-buffer / send-keys failed: rc={result.returncode} "
                f"stderr={result.stderr.strip()!r}"
            )

        self._finish_turn_delivery(turn)

    def _finish_turn_delivery(self, turn: _QueuedTurn) -> None:
        """Post-paste bookkeeping for a turn that reached the pane.

        Factored out of ``_deliver_turn`` so the worker's timeout-retry
        path can mark a turn delivered when the capture-pane guard
        (``_timed_out_turn_landed``) shows a timed-out paste actually
        landed -- without re-pasting it.
        """
        # #591 P1#2 (Murzik round-2): paste landed. Fire the optional
        # post-delivery callback (set on wake turns by
        # ``_enqueue_wake_prompt`` so ``agent_wake`` is logged AFTER the
        # prompt actually reached the REPL — not at enqueue time). This
        # guarantees the cycle-gate boundary advances only on confirmed
        # delivery: paste-failure (``_deliver_turn``'s ``not result.ok``
        # branch) raises BEFORE this point, so a wedged paste leaves the
        # boundary intact and the next attempt re-emits the directive.
        # Failure-tolerant: a misbehaving callback must not strand the
        # delivery, so wrap in try/except.
        if turn.on_delivered is not None:
            try:
                turn.on_delivered()
            except Exception as _cb_e:
                _log(
                    f"tmux[{self.agent_name}]: on_delivered callback "
                    f"failed (reason={turn.reason}): {_cb_e}"
                )

        # Paste succeeded — append routing metadata to the deque so
        # ``_handle_turn_complete`` can popleft it when this turn's
        # stop_hook_summary lands. Internal turns get an empty meta
        # dict (no external recipient).
        if turn.internal:
            meta_dict: dict = {}
        else:
            meta_dict = {
                "platform": turn.platform,
                "chat_id": turn.chat_id,
                "message_id": turn.message_id,
            }
        # #592: capture paste-time baselines so the watchdog can detect post-paste
        # REPL activity even when the Stop hook's live_status update is stale.
        # ``_paste_succeeded_at`` (daemon clock) is the authoritative floor; the
        # transcript mtime is sampled too but can lag the paste (see _InflightMeta),
        # so the verdict uses max(...). Errors are swallowed — the daemon-clock
        # stamp alone is a safe baseline.
        _paste_succeeded_at = time.time()
        _tailer_ref = self._tailer
        _tpath = getattr(_tailer_ref, "transcript_path", None) if _tailer_ref else None
        _tmtime_at_paste: float | None = None
        if _tpath:
            try:
                _tmtime_at_paste = Path(_tpath).stat().st_mtime
            except OSError:
                pass
        was_empty = not self._inflight_metas
        self._inflight_metas.append(_InflightMeta(
            meta=meta_dict,
            completion_event=turn.completion_event,
            internal=turn.internal,
            dispatched_at=time.time(),
            turn=turn,
            transcript_mtime_at_paste=_tmtime_at_paste,
            paste_succeeded_at=_paste_succeeded_at,
        ))
        # Watchdog head-clock. If this entry just became the head (deque
        # was empty before append), start its timeout window NOW. If
        # other entries are ahead, the head's clock was set when IT
        # became the head — leave it alone (Murzik review point #1).
        if was_empty:
            self._head_started_at = time.time()

        # Hint to the tailer that a turn is in flight — switches to the
        # active-poll cadence (200ms vs 2s) for low-latency response
        # capture. Stop hook will short-circuit this further by wake()ing
        # the tailer the moment the turn completes.
        if self._tailer is not None:
            self._tailer.mark_active()

    async def force_restart(self, *, bypass_guard: bool = False) -> bool:
        """Tear down the tmux session and start a fresh one.

        Drives ``CONNECTED → RECONNECTING → CONNECTED|DEAD``. Returns True
        on success, False if blocked by the restart guard.

        **``bypass_guard``** (Murzik review on commit 3 of PR #561).
        The persistence guard exists to prevent restarts that would
        drop completed-but-unsaved agent state mid-conversation. The
        inflight watchdog calls this with ``bypass_guard=True``
        because by the time the watchdog fires, the REPL is already
        wedged — its head turn timed out, the conversation state is
        already corrupted from the agent's POV. Leaving the session
        "intact" doesn't preserve anything useful; it only strands
        the replay queue with no worker to consume it (the watchdog
        had to cancel the old worker to prevent the race window
        Murzik flagged on commit 2).
        """
        if (
            not bypass_guard
            and self._has_completed_turn
            and self._config.restart_guard
        ):
            try:
                guard = self._config.restart_guard(self)
            except Exception:
                guard = {}
            if guard and not guard.get("restart_safe", False):
                _log(f"tmux[{self.agent_name}]: restart blocked")
                return False

        _log(f"tmux[{self.agent_name}]: force_restart")

        # Pre-assert RECONNECTING so observers (broker, watchdog) see the
        # intent before disconnect's CONNECTED → DEAD fallback fires.
        # Matches the StreamingSession.force_restart choreography from
        # PR3 / Murzik's #491 review.
        result = await self._state_machine.request_transition(
            SessionState.RECONNECTING,
            Trigger.USER_AGENT,
            reason="force_restart",
        )
        token = result.owner_token
        if token is None:
            # Couldn't grab ownership; another transition is in flight.
            # Best-effort: log and return False.
            _log(
                f"tmux[{self.agent_name}]: force_restart couldn't grab "
                f"RECONNECTING ownership ({result.rejection_reason!r})"
            )
            return False

        await self.disconnect()

        # disconnect's default → DEAD path triggers ONLY from CONNECTED;
        # we pre-set RECONNECTING above so it stays put. Now spawn fresh.
        try:
            await self._spawn_tmux_repl()
            await self._state_machine.transition_complete(
                token,
                SessionState.CONNECTED,
                trigger=Trigger.INTERNAL,
            )
            # Re-prime the agent with an orientation wake prompt BEFORE the
            # worker can start draining. Without this, force_restart
            # respawned the REPL but — unlike connect() — left the agent on
            # a blank session with no saved-state context ("comes back idle
            # / no anything").
            #
            # Ordering is load-bearing (Murzik #589 review): the inflight
            # watchdog requeues replay/backlog at the FRONT of
            # _message_queue *before* scheduling this restart, so the wake
            # prompt must (a) be front-enqueued ahead of that backlog and
            # (b) land before the worker starts — otherwise the resumed
            # REPL could process a user turn before ever seeing
            # orientation. We enqueue at the head here, then start the
            # worker, guaranteeing wake leads.
            #
            # Reason derives from the launch signals _build_claude_cmd just
            # recorded: a normal watchdog restart has a prior transcript
            # (now that _project_dir is fixed) → RESUME ("pick up where you
            # left off"); a forced-fresh respawn → CONTEXT_RESTART; a
            # genuinely transcript-less respawn → NEW_SESSION.
            if self._last_launch_forced_fresh:
                wake_reason = WakeReason.CONTEXT_RESTART
            elif self._last_launch_had_prior_transcript:
                wake_reason = WakeReason.RESUME
            else:
                wake_reason = WakeReason.NEW_SESSION
            await self._enqueue_wake_prompt(wake_reason, front=True)

            if not self._worker_task or self._worker_task.done():
                self._worker_task = asyncio.create_task(self._message_worker())
            # Respawn the watchdog too (#560). disconnect() above
            # cancelled it; without this, force_restart-then-stuck-turn
            # wouldn't be caught.
            if not self._watchdog_task or self._watchdog_task.done():
                self._watchdog_task = asyncio.create_task(self._inflight_watchdog())
            _log(
                f"tmux[{self.agent_name}]: force_restart complete "
                f"(wake_reason={wake_reason.value})"
            )
            return True
        except Exception as e:
            _log(f"tmux[{self.agent_name}]: force_restart spawn failed: {e}")
            try:
                await self._state_machine.transition_complete(
                    token,
                    SessionState.DEAD,
                    trigger=Trigger.INTERNAL,
                )
            except Exception:
                pass
            return False

    async def idle_sleep(self) -> bool:
        """Disconnect but keep the tmux session name pinned for cheap
        warm-wake on next inbound message.

        Drives ``CONNECTED → IDLE_SLEEPING`` via USER_AGENT.

        **Pre-sleep save instruction** (PR for #543 idle-sleep parity).
        Before the state transition + disconnect, send the agent the
        same save-state instruction SDK sends in its
        ``idle_sleep()``: "use reflect() / note your task so you can
        resume." Delivery is via ``_enqueue_internal_prompt`` with
        ``wait_for_completion=True`` — caller must NOT proceed to
        disconnect until the agent has had a chance to honor the
        instruction. Without that wait flag, tmux would paste the
        instruction and kill the pane before the agent could call
        reflect/save_my_context (the footgun Murzik flagged when
        reviewing the internal-prompt API).

        ``timeout_sec=120`` matches the conservative ceiling for a
        single save turn — long enough for a typical reflect()/
        save_my_context() roundtrip, tight enough that a wedged REPL
        doesn't strand the session indefinitely in CONNECTED while
        the operator/scheduler is trying to drive it to sleep.

        Exceptions from the pre-sleep enqueue are logged + swallowed
        (mirrors SDK's behavior) — a misbehaving REPL must not block
        idle-sleep semantics. The session still transitions to
        IDLE_SLEEPING + disconnects in that path; only the save-state
        side-effect is lost.
        """
        if self.state != SessionState.CONNECTED:
            return False

        _log(f"tmux[{self.agent_name}]: idle_sleep")

        # Pre-sleep save instruction. MUST run while still CONNECTED
        # (``_enqueue_internal_prompt`` gates on state) and BEFORE the
        # state-machine transition + disconnect below. The
        # ``wait_for_completion=True`` semantic blocks here until the
        # agent's turn ends so we don't disconnect mid-reflect.
        try:
            await self._enqueue_internal_prompt(
                build_idle_sleep_prompt(),
                reason="idle_sleep_presave",
                wait_for_completion=True,
                timeout_sec=120.0,
            )
            _log(f"tmux[{self.agent_name}]: idle_sleep_presave completed")
        except asyncio.TimeoutError:
            _log(
                f"tmux[{self.agent_name}]: idle_sleep_presave timed out after "
                f"120s — proceeding to disconnect anyway"
            )
        except Exception as e:
            _log(
                f"tmux[{self.agent_name}]: idle_sleep_presave failed: {e} — "
                f"proceeding to disconnect anyway"
            )

        # Pre-set IDLE_SLEEPING so disconnect's CONNECTED → DEAD fallback
        # doesn't fire (matches StreamingSession's choreography).
        result = await self._state_machine.request_transition(
            SessionState.IDLE_SLEEPING,
            Trigger.USER_AGENT,
            reason="idle_sleep",
        )
        token = result.owner_token
        if token is None:
            _log(
                f"tmux[{self.agent_name}]: idle_sleep couldn't grab IDLE_SLEEPING "
                f"ownership ({result.rejection_reason!r})"
            )
            return False

        await self.disconnect()

        await self._state_machine.transition_complete(
            token,
            SessionState.IDLE_SLEEPING,
            trigger=Trigger.USER_AGENT,
        )
        self._stats["auto_restarts"] += 1
        _log(f"tmux[{self.agent_name}]: idle_sleep complete")
        return True

    async def attempt_reconnect(self, *, trigger: Trigger = Trigger.BROKER) -> None:
        """Best-effort reconnect after a transient transport failure.

        Drives the warm-reconnect loop with bounded backoff. Matches the
        StreamingSession contract so api._heartbeat_resurrect treats both
        runtimes uniformly.

        Murzik's PR #495 round-1 finding 2: the matrix requires different
        triggers per source state for the ``→ RECONNECTING`` edge —

        - CONNECTED → RECONNECTING: USER_AGENT / WATCHDOG / API_ADMIN / INTERNAL
        - IDLE_SLEEPING → RECONNECTING: BROKER / WATCHDOG / SCHEDULER / API_ADMIN
        - DEAD → RECONNECTING: BROKER / WATCHDOG / SCHEDULER / API_ADMIN

        The pre-fix unconditional ``INTERNAL`` would silently reject when
        called from DEAD or IDLE_SLEEPING — exactly the resurrection paths
        that need to work for ``api._heartbeat_resurrect`` to revive a
        watchdog-killed agent. The trigger parameter lets the caller declare
        their identity; default ``BROKER`` matches the most common caller
        (broker auto-wake on inbound).

        Args:
            trigger: Actor identity for the ``→ RECONNECTING`` edge. Pick
                the one that matches the matrix cell for the current source
                state. Default ``BROKER`` covers auto-wake on inbound;
                pass ``WATCHDOG`` from the watchdog resurrection callback,
                ``SCHEDULER`` from cron-driven resurrect, ``API_ADMIN`` from
                explicit operator action.
        """
        # Drive into RECONNECTING. If we're already there (e.g. force_restart
        # is mid-flight), let that owner finish.
        if self.state == SessionState.RECONNECTING:
            _log(
                f"tmux[{self.agent_name}]: attempt_reconnect entered while "
                f"already RECONNECTING — bailing (another path owns this transition)"
            )
            return

        # Pick a matrix-legal trigger for the current source state. INTERNAL
        # only works from CONNECTED; warm sources (IDLE_SLEEPING/DEAD) need
        # an external actor identity.
        result = await self._state_machine.request_transition(
            SessionState.RECONNECTING,
            trigger,
            reason=f"attempt_reconnect_from_{self.state.value}",
        )
        token = result.owner_token
        if token is None:
            # Could be a concurrent transition or matrix rejection. Subscribe
            # if there's a handle; surface DEAD if we landed there.
            if result.in_flight_handle is not None:
                final = await result.in_flight_handle.wait()
                if final == SessionState.CONNECTED:
                    return
                _log(
                    f"tmux[{self.agent_name}]: attempt_reconnect in-flight "
                    f"resolved to {final.value}"
                )
                return
            _log(
                f"tmux[{self.agent_name}]: attempt_reconnect rejected "
                f"({result.rejection_reason!r})"
            )
            return

        try:
            await self.disconnect()
        except Exception as e:
            _log(f"tmux[{self.agent_name}]: pre-reconnect disconnect raised: {e}")

        last_error: Exception | None = None
        for attempt_idx, delay in enumerate(_RECONNECT_BACKOFF, start=1):
            self._stats["reconnects"] += 1
            _log(
                f"tmux[{self.agent_name}]: reconnect attempt {attempt_idx}/"
                f"{len(_RECONNECT_BACKOFF)} after {delay}s backoff"
            )
            await asyncio.sleep(delay)
            try:
                await self._spawn_tmux_repl()
                await self._state_machine.transition_complete(
                    token,
                    SessionState.CONNECTED,
                    trigger=Trigger.INTERNAL,
                )
                # Re-prime with an orientation wake prompt BEFORE the
                # worker starts draining, mirroring force_restart (#589).
                # Without this a heartbeat-resurrected agent comes back
                # on a session with no saved-state / current-time /
                # channel orientation. Reason derivation matches
                # force_restart's launch-signal mapping.
                if self._last_launch_forced_fresh:
                    wake_reason = WakeReason.CONTEXT_RESTART
                elif self._last_launch_had_prior_transcript:
                    wake_reason = WakeReason.RESUME
                else:
                    wake_reason = WakeReason.NEW_SESSION
                await self._enqueue_wake_prompt(wake_reason, front=True)
                # Respawn the worker — disconnect() above cancelled it, so
                # the queue would otherwise have no drainer on success.
                if not self._worker_task or self._worker_task.done():
                    self._worker_task = asyncio.create_task(self._message_worker())
                # Respawn the watchdog too (#560).
                if not self._watchdog_task or self._watchdog_task.done():
                    self._watchdog_task = asyncio.create_task(self._inflight_watchdog())
                _log(
                    f"tmux[{self.agent_name}]: reconnected successfully "
                    f"(wake_reason={wake_reason.value})"
                )
                return
            except Exception as e:
                last_error = e
                _log(
                    f"tmux[{self.agent_name}]: reconnect attempt {attempt_idx} "
                    f"failed: {e}"
                )

        # Exhausted retry budget → DEAD.
        try:
            await self._state_machine.transition_complete(
                token,
                SessionState.DEAD,
                trigger=Trigger.INTERNAL,
            )
        except Exception:
            pass
        _log(
            f"tmux[{self.agent_name}]: all {len(_RECONNECT_BACKOFF)} reconnect "
            f"attempts failed (last error: {last_error}); landed DEAD"
        )
