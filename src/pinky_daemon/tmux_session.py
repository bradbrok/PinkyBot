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
import os
import shlex
import time
from dataclasses import dataclass, field, replace
from pathlib import Path

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
    build_idle_sleep_prompt,
    build_wake_prompt,
)

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

# Transient-failure retry cadence for the worker loop. Fixed (not
# exponential) — mirrors pulse-v2's poll cadence and keeps the
# semantics simple: "park, sleep, retry the same turn". The worker
# does not move on to the next queue item until the inflight turn
# either succeeds or hits a permanent failure.
_TRANSIENT_RETRY_BACKOFF_SEC = 2.0


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
    ) -> None:
        self.session_name = session_name
        self.tmux_binary = tmux_binary
        # An explicit socket isolates Pinky's tmux sessions from the
        # operator's own. Empty = use tmux's default socket.
        self.socket_name = socket_name

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
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            raise
        return TmuxCommandResult(
            returncode=proc.returncode or 0,
            stdout=stdout.decode("utf-8", errors="replace"),
            stderr=stderr.decode("utf-8", errors="replace"),
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
        self, *, lines: int = 200, escapes: bool = False,
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
        """
        args = [
            "capture-pane",
            "-t", self.session_name,
            "-p",  # print to stdout instead of paste buffer
        ]
        if escapes:
            args.append("-e")  # include ANSI escape sequences
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
      do not write to ``_inflight_meta``. Optional ``completion_event`` is
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
    # response_callback, no ``_inflight_meta`` writes).
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

# Per-turn timeout: how long the worker waits for ``_turn_done`` between
# dispatching a prompt and the tailer firing ``_handle_turn_complete``.
# Generous (10 min) to cover tool-use loops + slow models + cold-model
# dispatch. Anything longer is "stuck" — caller / watchdog retries.
_TURN_DONE_TIMEOUT_SEC = 600.0


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
        self._tmux = tmux_control or _TmuxControl(self._session_name)

        # Worker queue + task.
        self._message_queue: asyncio.Queue[_QueuedTurn] = asyncio.Queue()
        self._worker_task: asyncio.Task | None = None
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
        self._activity_log: list[str] = []

        # Response capture pipeline (PR8b). Lazily constructed in
        # ``_spawn_tmux_repl`` after we know the transcript path. The
        # tailer reads Claude Code's JSONL transcript, accumulates each
        # turn's assistant content, and fires ``_handle_turn_complete``
        # on every ``stop_hook_summary`` entry — which routes to
        # ``_response_callback`` to deliver the response upstream.
        self._tailer: TmuxTranscriptTailer | None = None
        # Last user-message metadata (platform / chat_id / message_id),
        # captured at send() time. Forwarded to ``_response_callback``
        # so the broker can route the reply back to the right channel.
        # Worker awaits ``_turn_done`` between turns so exactly one turn
        # is in flight at any time — meta is set in ``_deliver_turn`` and
        # cleared in ``_handle_turn_complete``, with the turn-done gate
        # preventing the worker from overwriting it before the tailer
        # consumes it. Pushok's PR #496 round-1 critical finding (Case 1).
        self._inflight_meta: dict = {}
        # Set by ``_handle_turn_complete`` at the end of every turn;
        # awaited by ``_message_worker`` after ``_deliver_turn`` so the
        # next dispatch can't clobber ``_inflight_meta`` mid-flight.
        # Invariant: between dispatches, turn_done is CLEARED; it's set
        # only after a callback fires. The first ``_deliver_turn`` clears
        # it (no-op on a fresh Event) before send-keys; the worker only
        # awaits on subsequent iterations.
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

    def _build_session_name(self) -> str:
        """Tmux session name pattern: ``pinky-<agent_name>``.

        Prefix prevents collision with the operator's own tmux sessions.
        Plain ``agent_name`` if you wanted to attach without prefix; the
        prefix is the safer default.
        """
        return f"pinky-{self.agent_name}"

    # ── State ───────────────────────────────────────────────────────────

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
        return {
            **self._stats,
            "state": self.state.value,
            "pending_responses": self._processing,
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
        valid = {"low", "medium", "high", "xhigh", "max", "auto"}
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

        if not self._skip_wake_prompt_for_tests:
            _wake_prompt = build_wake_prompt(
                WakePromptInput(
                    reason=_wake_reason,
                    context_body=self._config.wake_context or "",
                    timezone=self._config.timezone or "America/Los_Angeles",
                )
            )
            try:
                await self._enqueue_internal_prompt(
                    _wake_prompt,
                    reason=f"wake_{_wake_reason.value}",
                    wait_for_completion=False,
                )
            except Exception as e:
                # Wake-prompt enqueue failure must not strand the session
                # in CONNECTED-but-orientationless. Log loudly; the
                # session remains usable for external turns but the agent
                # will lack saved-state context until the next restart.
                _log(
                    f"tmux[{self.agent_name}]: wake prompt enqueue failed: {e} "
                    f"(reason={_wake_reason.value}) — session remains CONNECTED"
                )

        _log(
            f"tmux[{self.agent_name}]: connected, session={self._session_name}, "
            f"worker started, wake_reason={_wake_reason.value}"
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
        if self.agent_name:
            env["PINKY_AGENT_NAME"] = self.agent_name
        effort = self.effective_effort
        if effort:
            env["PINKY_EXPECTED_EFFORT"] = effort
        if self._config.strict_effort_enforcement:
            env["PINKY_STRICT_EFFORT"] = "1"
        # PINKY_SESSION_SECRET — see docstring. Read from os.environ
        # rather than a config field because it's a daemon-wide secret
        # (the daemon's own SDK clients and FastAPI middleware read it
        # from the same env var). Empty/missing is tolerated: hooks
        # already handle that gracefully (silent no-op).
        secret = os.environ.get("PINKY_SESSION_SECRET", "").strip()
        if secret:
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
        self._processing = False

        # Clear in-flight routing metadata so a straggler stop_hook_summary
        # (e.g. read from a stale transcript on reconnect) can't route a
        # late response to a stale chat. Pushok's PR #496 round-1 Case 2.
        self._inflight_meta = {}

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
        await self._message_queue.put(
            _QueuedTurn(
                prompt=prompt,
                platform="",
                chat_id="",
                message_id="",
                internal=True,
                reason=reason,
                completion_event=completion,
            )
        )

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
        """
        if self._tailer is not None:
            self._tailer.set_transcript_path(Path(path))
            _log(
                f"tmux[{self.agent_name}]: transcript path updated to "
                f"{path}"
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

    # Claude Code reserves a buffer below the raw model cap so the
    # ``/compact`` autocompact step fires before the API rejects the
    # next turn for context exhaustion. Empirically 33K on the 200K
    # window (≈16.5%); per the GH discussion thread (anthropics/
    # claude-code#27189) and the SDK's ``ContextUsageResponse``
    # docstring this is a fixed constant — not a percentage — so 1M-
    # window models reserve the same 33K, not a proportionally larger
    # buffer.
    _AUTOCOMPACT_BUFFER_TOKENS = 33_000

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

        threshold = self._restart_threshold_pct()
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
        elif pct < threshold and self._restart_nudge_fired:
            # Re-arm the latch once context drops back below threshold
            # (e.g. /compact ran). Next crossing will fire a fresh nudge.
            self._restart_nudge_fired = False

    async def _handle_turn_complete(self, response: TurnResponse) -> None:
        """Tailer callback — fired once per ``stop_hook_summary`` entry.

        Mirrors StreamingSession's per-turn dispatch: feed the
        conversation store, fire response_callback, fire stream_event
        for analytics. cost_callback is a no-op for tmux (subscription
        billing, no per-turn cost) but we still fire stream_event so
        usage telemetry is visible.

        Internal-prompt branch (PR for #543): when ``_inflight_turn``
        reports the just-finished turn was an internal-prompt (wake
        orientation, pre-sleep save reminder, etc.), skip the
        conversation_store assistant append and the response_callback —
        no external recipient, no user-visible conversation. Still:
        emit ``turn_completed`` for analytics parity, account usage
        toward the context-budget watchdog, and signal ``_turn_done``
        so the worker progresses. If the internal turn carried a
        ``completion_event``, set it before ``_turn_done`` so a
        ``wait_for_completion=True`` caller unblocks deterministically
        with the worker advancing in lock-step.
        """
        is_internal = bool(
            self._inflight_turn is not None and self._inflight_turn.internal
        )
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
        # captured inbound metadata so the broker can route the reply.
        # Skip for internal turns: no chat target, and the metadata is
        # intentionally empty (see ``_deliver_turn`` regression guard).
        if (
            not is_internal
            and self._response_callback
            and (response.text or response.tool_uses)
        ):
            try:
                meta = dict(self._inflight_meta)
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

        # Clear in-flight metadata — next send() will populate it.
        self._inflight_meta = {}

        # Internal-prompt completion signal (PR for #543). MUST fire
        # BEFORE ``_turn_done`` so a ``wait_for_completion=True`` caller
        # can rely on the worker not having dispatched the next turn
        # yet — the worker awaits ``_turn_done`` between turns, and
        # advancing past that gate would change the in-flight bookkeeping
        # the caller might be observing.
        if (
            is_internal
            and self._inflight_turn is not None
            and self._inflight_turn.completion_event is not None
        ):
            self._inflight_turn.completion_event.set()

        # Signal turn-complete to the worker UNCONDITIONALLY. Must be
        # outside any ``if response.text`` gate (Pushok's PR #496 round-1
        # Case 1 follow-up): empty-text turns (e.g. pure tool-use that
        # hit max_tokens) still complete a turn, and the worker is
        # awaiting this event regardless. Gating on text would deadlock
        # the worker forever on a tool-use-only turn.
        self._turn_done.set()

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

    async def _start_tailer(self) -> None:
        """Construct + start the transcript tailer.

        Called from ``_spawn_tmux_repl`` after the REPL boots. Uses the
        best-effort path guess (newest .jsonl in the project dir);
        SessionStart hook later repoints us at the canonical path.

        Idempotent — calling twice is a no-op.
        """
        if self._tailer is not None:
            await self._tailer.start()  # idempotent
            return

        guessed = self._discover_transcript_path()
        # Even if guessed is None (cold start, no transcript yet) we still
        # construct the tailer so notify_tail() works as soon as the
        # SessionStart hook reports a path. Use a placeholder path that
        # .exists() returns False for — the tailer's read_once handles
        # that gracefully.
        path = guessed or Path("/dev/null/no-transcript-yet")
        self._tailer = TmuxTranscriptTailer(
            transcript_path=path,
            on_turn_complete=self._handle_turn_complete,
            agent_name=self.agent_name,
            # #515 self-heal: hand the tailer our discovery callback so
            # it can mtime-scan and rebind on its own if the SessionStart
            # hook never fires (e.g. when tmux strips PINKY_SESSION_SECRET
            # from the hook script's env — see ``_build_repl_env``). The
            # tailer becomes correct independently of the hook firing.
            path_discovery=self._discover_transcript_path,
        )
        await self._tailer.start()
        if guessed is None:
            _log(
                f"tmux[{self.agent_name}]: tailer started with placeholder "
                f"path — awaiting SessionStart hook to report actual transcript"
            )
        else:
            # Seek to EOF on the existing file so we don't replay historical
            # turns on a warm-wake / resume. The SessionStart hook can
            # set_offset(0) if a fresh backfill is wanted.
            try:
                self._tailer.set_offset(guessed.stat().st_size)
            except OSError:
                # File disappeared between exists() check and stat() — race
                # with Claude Code rotating/clearing the project dir. Fall
                # through with offset=0 (the SessionStart hook will reset
                # us to the canonical path shortly).
                pass
            _log(
                f"tmux[{self.agent_name}]: tailer started at {guessed} "
                f"(offset={self._tailer.offset})"
            )

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
        """
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

        ``encoded-cwd``: the absolute cwd with the leading ``/`` consumed
        and remaining ``/`` replaced with ``-`` (e.g.
        ``/Users/oleg/foo`` → ``-Users-oleg-foo``). Mirrors Claude Code's
        own encoding so the glob targets the right directory.
        """
        cwd = Path(self._config.working_dir or ".").resolve()
        encoded = "-" + str(cwd).replace("/", "-")
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
        """Drain the message queue sequentially, delivering each turn to
        the tmux pane and waiting for it to complete before the next.

        PR8b round-2 (Pushok's Case 1 fix): the worker gates dispatch
        on ``_turn_done``, which ``_handle_turn_complete`` sets at the
        end of every turn (including empty-text / tool-use-only turns).
        This ensures ``_inflight_meta`` is never overwritten while the
        tailer still has work to fire for the in-flight turn, AND bounds
        the prompts that get stacked into Claude Code's input queue
        (UX win — CC's queued-prompt indicator is non-obvious).

        Murzik #522 round-1 (data-loss fix): the worker now keeps the
        current turn IN-HAND across transient failures via
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
        try:
            while self.state == SessionState.CONNECTED:
                # Only pull a new turn when nothing is inflight. After
                # a transient failure or a force_restart, ``_inflight_turn``
                # carries the previous turn so it gets retried instead of
                # silently dropped (Murzik #522 round-1).
                if self._inflight_turn is None:
                    self._inflight_turn = await self._message_queue.get()
                turn = self._inflight_turn
                try:
                    self._processing = True
                    await self._deliver_turn(turn)
                    self._stats["turns"] += 1

                    # Wait for THIS turn's stop_hook_summary to fire
                    # _handle_turn_complete, which sets _turn_done.
                    # Bounded so a missed Stop hook (e.g. transcript path
                    # never reported, hook script removed) doesn't strand
                    # the worker forever — 10 minutes is generous enough
                    # for long tool-use loops + slow models, tight enough
                    # that a real wedge surfaces in operations.
                    try:
                        await asyncio.wait_for(
                            self._turn_done.wait(),
                            timeout=_TURN_DONE_TIMEOUT_SEC,
                        )
                        self._has_completed_turn = True
                        # Success — clear inflight so the next
                        # iteration pulls a fresh turn.
                        self._inflight_turn = None
                    except asyncio.TimeoutError:
                        # The REPL is stuck. Pushok's PR #496 round-2
                        # follow-up: just "continue" leaves the stuck
                        # turn's stop_hook_summary free to land later
                        # and route to the *next* dispatch's meta —
                        # exactly the original Case 1 bug, slow-motion.
                        # Solution: force_restart the tmux pane. The
                        # orphaned turn dies with the REPL; SessionStart
                        # hook repoints the tailer at the new transcript
                        # file, so any late stop_hook_summary from the
                        # dead session can't poison the new one. The
                        # cancelled worker exits; ``_spawn_tmux_repl``
                        # spawns a fresh worker that resumes the queue
                        # in a clean state.
                        _log(
                            f"tmux[{self.agent_name}]: turn_done timeout "
                            f"after {_TURN_DONE_TIMEOUT_SEC}s — REPL stuck; "
                            f"scheduling force_restart and exiting worker"
                        )
                        self._inflight_meta = {}
                        self._turn_done.set()
                        self._stats["errors"] += 1
                        self._stats["turn_timeouts"] = (
                            self._stats.get("turn_timeouts", 0) + 1
                        )
                        # Turn-done timeout means the prompt DID land in
                        # the REPL but the response never completed.
                        # Treat as permanent for this turn — clear
                        # inflight so a stale prompt doesn't get re-
                        # pasted into the fresh REPL after force_restart.
                        self._inflight_turn = None
                        # Schedule force_restart in the background so this
                        # worker can exit cleanly without awaiting its own
                        # cancellation (force_restart calls disconnect →
                        # worker_task.cancel + await, which would deadlock
                        # if invoked synchronously from inside the worker).
                        asyncio.create_task(self.force_restart())
                        return
                except _ContextLockDeferral as e:
                    # Transient: lock file present. Don't touch
                    # _inflight_turn or _turn_done — _deliver_turn raised
                    # BEFORE clearing turn_done or mutating meta, so the
                    # signal/meta from any prior turn is still consistent.
                    _log(
                        f"tmux[{self.agent_name}]: turn deferred "
                        f"(context lock); retrying in "
                        f"{_TRANSIENT_RETRY_BACKOFF_SEC}s ({e})"
                    )
                    await asyncio.sleep(_TRANSIENT_RETRY_BACKOFF_SEC)
                    continue
                except Exception as e:
                    # Permanent failure (paste-buffer/send-keys failed,
                    # dead-pane, tailer-state corruption, etc.). Drop
                    # the inflight turn so we don't redeliver into a
                    # broken pane on the next iteration.
                    self._stats["errors"] += 1
                    _log(f"tmux[{self.agent_name}]: turn delivery raised: {e}")
                    # _deliver_turn already re-armed turn_done on send-keys
                    # failure; defensively re-arm here in case some other
                    # path raised (e.g. tailer state corruption).
                    self._turn_done.set()
                    self._inflight_turn = None
                    # Task #90: dead-pane already scheduled disconnect from
                    # inside _deliver_turn. Exit the worker cleanly so we
                    # don't retry into the now-being-torn-down pane.
                    # Mirrors the turn_done timeout path's create_task +
                    # return pattern above (the finally block below still
                    # runs and resets _processing).
                    if "can't find pane" in str(e):
                        return
                finally:
                    self._processing = False
        except asyncio.CancelledError:
            _log(f"tmux[{self.agent_name}]: worker cancelled")
        except Exception as e:
            _log(f"tmux[{self.agent_name}]: worker error: {e}")

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
        handles the inbound half — push the prompt, capture the routing
        metadata for the tailer to use when it fires the response_callback,
        clear the turn-done gate so the worker waits for THIS turn's
        completion before dispatching the next prompt, and signal the
        tailer to switch to the tighter active-poll cadence.

        Pushok's PR #496 round-1 Case 1 fix: the turn_done gate is what
        prevents ``_inflight_meta`` from being clobbered between back-to-
        back ``send()`` calls. The previous design assumed worker
        sequentiality alone was enough — but the worker is a dispatch
        pump, not a request/response broker, so a fast second prompt
        would overwrite meta before the first turn's stop_hook_summary
        landed.

        Pulse-v2 port (task #92): one safety primitive gates the paste —
        the context-lock check — raising a **typed transient exception**
        the worker catches in its retry loop (Murzik #522 round-1).
        Because the worker pops the turn from the queue before calling
        ``_deliver_turn``, a bare exception would silently drop the
        message; the worker keeps the turn in ``_inflight_turn`` and
        re-pastes the same prompt on the next iteration. The deferral
        path does not schedule disconnect — this is transient state,
        not a dead-pane.

        **Context-lock check.** If the daemon-level context manager has
        touched ``data/transport-locks/<agent>.lock``, it's mid-rewrite
        of files this REPL depends on — paste would land on an
        inconsistent state. Raise ``_ContextLockDeferral`` so the worker
        preserves the inflight turn, sleeps, and retries when the lock
        is released.

        Splash-state paste handling lives in ``_TmuxControl.paste_text``
        (bracketed-paste + delayed-Enter, commit 0864f4e / issue #514).
        Claude Code's splash dismisses on input focus, so pasting into
        the splash works correctly; no readiness gate is needed. (Prior
        to #525, a pane-scraping idle-prompt gate from #522 / #524 sat
        here; it waited for a bare ``❯`` signal that the splash never
        produces, killing every cold start.)
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

        # Capture routing metadata BEFORE send-keys so the tailer's callback
        # has access to it even if the tailer fires unusually quickly.
        #
        # Internal-prompt regression guard (PR for #543): only EXTERNAL
        # turns own routing metadata. An internal turn that writes here
        # would clobber a back-to-back external turn's routing fields
        # (the #496 round-1 Case 1 defect surfacing through this path).
        # Leave ``_inflight_meta`` empty for internal turns; they have no
        # response delivery target and ``_handle_turn_complete`` skips
        # the response_callback altogether when ``turn.internal``.
        if turn.internal:
            self._inflight_meta = {}
        else:
            self._inflight_meta = {
                "platform": turn.platform,
                "chat_id": turn.chat_id,
                "message_id": turn.message_id,
            }

        # Clear the turn-done gate BEFORE send-keys. Two reasons for
        # ordering this here (vs. after mark_active, the other reasonable
        # spot):
        #   1. ANY stop_hook_summary that arrives after this clear must
        #      come from THIS turn (we haven't yet delivered the prompt,
        #      let alone produced a response). So waiting on the next set
        #      is unambiguous.
        #   2. If we cleared after mark_active, a degenerate-fast tailer
        #      (test fixture, or a claude refusal turn) could conceivably
        #      set turn_done between mark_active and clear, then we'd
        #      wipe the signal and the worker would block forever.
        # Cost is one extra .clear() call that wipes a stale signal from
        # the previous turn — already-consumed by the previous worker
        # iteration's await, so wiping it is a no-op.
        self._turn_done.clear()

        # Use paste_text (bracketed paste + delayed Enter) instead of raw
        # send-keys (issue #514, Misha/Pulse v2 pattern). The delayed
        # Enter gives claude's cold-start splash UI time to dismiss
        # before the submit Enter arrives, so the first prompt of a
        # fresh session doesn't get wedged in claude's input buffer.
        result = await self._tmux.paste_text(turn.prompt, enter=True)
        if not result.ok:
            # Send failed — no response will arrive. Re-arm turn_done so
            # the worker's next iteration doesn't deadlock; clear meta
            # so a stale value doesn't leak to a later (unrelated) turn.
            self._inflight_meta = {}
            self._turn_done.set()
            # Task #90: detect dead-pane (tmux session killed externally,
            # tmux server crashed, etc.). Without this, the worker would
            # loop forever pasting into a non-existent pane. Schedule
            # disconnect (NOT force_restart — that's gated by the
            # restart_guard from #517 and may block once we've had a
            # completed turn). The disconnect drives CONNECTED → DEAD
            # via the default-disconnect path; the next inbound
            # send_to_agent triggers the normal auto-wake cold-start
            # path (validated in production by #517/#518/#519).
            if "can't find pane" in (result.stderr or ""):
                _log(
                    f"tmux[{self.agent_name}]: pane vanished "
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

        # Hint to the tailer that a turn is in flight — switches to the
        # active-poll cadence (200ms vs 2s) for low-latency response
        # capture. Stop hook will short-circuit this further by wake()ing
        # the tailer the moment the turn completes.
        if self._tailer is not None:
            self._tailer.mark_active()

    async def force_restart(self) -> bool:
        """Tear down the tmux session and start a fresh one.

        Drives ``CONNECTED → RECONNECTING → CONNECTED|DEAD``. Returns True
        on success, False if blocked by the restart guard.
        """
        if self._has_completed_turn and self._config.restart_guard:
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
            if not self._worker_task or self._worker_task.done():
                self._worker_task = asyncio.create_task(self._message_worker())
            _log(f"tmux[{self.agent_name}]: force_restart complete")
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
                # Respawn the worker — disconnect() above cancelled it, so
                # the queue would otherwise have no drainer on success.
                if not self._worker_task or self._worker_task.done():
                    self._worker_task = asyncio.create_task(self._message_worker())
                _log(f"tmux[{self.agent_name}]: reconnected successfully")
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
