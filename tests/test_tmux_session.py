"""Tests for TmuxSession.

PR8 of the #486 sequence. Focused on the lifecycle choreography +
state-machine integration. The response capture pipeline is PR8b — its
tests will land alongside that PR.

Test strategy:
- Mock ``_TmuxControl`` (the subprocess wrapper) end-to-end; never shell
  out to a real tmux binary.
- Pin the state-machine transitions on every lifecycle path (cold-start
  success/failure, warm-reconnect, idle-sleep, force-restart).
- Pin the concurrent-connect Cases A + B from PR6's framework: greenfield
  backend should get the race protection by construction.
"""

from __future__ import annotations

import asyncio
import json as _json
import time as _time
from unittest.mock import AsyncMock, MagicMock

import pytest

from pinky_daemon import tmux_session
from pinky_daemon.streaming_session import StreamingSessionConfig
from pinky_daemon.tmux_session import (
    TmuxCommandResult,
    TmuxSession,
    _InflightMeta,
    _QueuedTurn,
    _TmuxControl,
)
from pinky_daemon.tmux_transcript import TurnResponse
from pinky_daemon.transport_state import SessionState, TransitionResult, Trigger


def _seed_inflight(
    ss: TmuxSession,
    *,
    meta: dict | None = None,
    internal: bool = False,
    completion_event: asyncio.Event | None = None,
    prompt: str = "",
) -> _InflightMeta:
    """Append one ``_InflightMeta`` entry to ``ss._inflight_metas``.

    Pre-#560 tests injected state via ``ss._inflight_meta = {...}``; the
    back-compat setter still does that for ROUTING-only seeds. This
    helper is the modern path — needed when tests want to seed an
    ``internal=True`` or ``completion_event`` entry, or chain multiple
    entries to exercise FIFO + watchdog tail-requeue behavior. Also
    bumps ``_head_started_at`` if this is the new head.

    Returns the entry so the test can assert on it. ``prompt`` lets
    watchdog-requeue tests verify the right turn body is replayed.
    """
    m = meta or {}
    synthetic_turn = _QueuedTurn(
        prompt=prompt,
        platform=m.get("platform", ""),
        chat_id=m.get("chat_id", ""),
        message_id=m.get("message_id", ""),
        internal=internal,
        completion_event=completion_event,
    )
    entry = _InflightMeta(
        meta=m,
        completion_event=completion_event,
        internal=internal,
        dispatched_at=_time.time(),
        turn=synthetic_turn,
    )
    was_empty = not ss._inflight_metas
    ss._inflight_metas.append(entry)
    if was_empty:
        ss._head_started_at = _time.time()
    return entry


def _ok() -> TmuxCommandResult:
    """Successful tmux command result."""
    return TmuxCommandResult(returncode=0, stdout="", stderr="")


def _fail(msg: str = "boom") -> TmuxCommandResult:
    """Failed tmux command result."""
    return TmuxCommandResult(returncode=1, stdout="", stderr=msg)


def _make_mock_tmux(*, has_session_initial: bool = False) -> MagicMock:
    """Build a MagicMock of ``_TmuxControl`` with sensible async defaults.

    All methods return success unless overridden by the test.
    """
    tmux = MagicMock(spec=_TmuxControl)
    tmux.session_name = "pinky-test"
    tmux.has_session = AsyncMock(return_value=has_session_initial)
    tmux.new_session = AsyncMock(return_value=_ok())
    tmux.kill_session = AsyncMock(return_value=_ok())
    tmux.send_keys = AsyncMock(return_value=_ok())
    tmux.paste_text = AsyncMock(return_value=_ok())
    tmux.capture_pane = AsyncMock(return_value=_ok())
    tmux.resize_window = AsyncMock(return_value=_ok())
    return tmux


def _make_session(
    *,
    agent_name: str = "dymok",
    state: SessionState | None = None,
    restart_guard=None,
    tmux: MagicMock | None = None,
) -> tuple[TmuxSession, MagicMock]:
    """Build a TmuxSession with mocked tmux control.

    Returns (session, tmux_mock). Tests that need to start in a specific
    state pass ``state=...``; the state machine is direct-mutated to that
    state (same bypass pattern existing StreamingSession tests use).
    """
    cfg = StreamingSessionConfig(
        agent_name=agent_name,
        working_dir="/tmp/tmux-session-test",
        restart_guard=restart_guard,
    )
    tmux = tmux or _make_mock_tmux()
    ss = TmuxSession(cfg, tmux_control=tmux)
    # Skip wake-prompt enqueue by default in unit tests — without a
    # simulated transcript tailer the worker would block forever on the
    # never-completing wake turn. Wake-prompt behavior has its own
    # dedicated tests (see TestWakePromptEnqueue below) which set this
    # flag explicitly to False and provide the tailer simulation.
    ss._skip_wake_prompt_for_tests = True
    if state is not None:
        ss._state_machine._state = state
    return ss, tmux


# ──────────────────────────────────────────────────────────────────────────
# Construction + identity
# ──────────────────────────────────────────────────────────────────────────


def test_default_initial_state_is_uninitialized() -> None:
    ss, _ = _make_session()
    assert ss.state == SessionState.UNINITIALIZED


def test_completed_turn_tracking_starts_false() -> None:
    ss, _ = _make_session()
    assert ss._has_completed_turn is False


def test_id_format_matches_streaming_session() -> None:
    ss, _ = _make_session(agent_name="dymok")
    # Default label → "main"
    assert ss.id == "dymok-main"


def test_resume_handle_is_tmux_session_name() -> None:
    """For tmux, the tmux session name IS the resume handle. Pinning by
    name preserves cwd → claude --continue resumes via that cwd's most-
    recent transcript automatically."""
    ss, _ = _make_session(agent_name="dymok")
    assert ss.resume_handle == "pinky-dymok"


def test_session_name_prefix_isolates_pinky_from_operator_tmux() -> None:
    """Tmux session name has the ``pinky-`` prefix so Pinky-owned sessions
    can be distinguished from the operator's own tmux sessions on the host."""
    ss, _ = _make_session(agent_name="dymok")
    assert ss._session_name.startswith("pinky-")


# ──────────────────────────────────────────────────────────────────────────
# Cold-start lifecycle: UNINITIALIZED → BOOTING → CONNECTED / DEAD
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cold_start_drives_state_through_booting_to_connected() -> None:
    """Successful cold-start lands in CONNECTED via the BOOT/BOOT_COMPLETE
    Trigger pair. Mirrors StreamingSession's PR6 cold-start contract."""
    ss, tmux = _make_session()
    await ss.connect()
    assert ss.state == SessionState.CONNECTED
    # Exactly one tmux new-session call (the cold-start spawn).
    assert tmux.new_session.await_count == 1


@pytest.mark.asyncio
async def test_cold_start_failure_drives_to_dead_via_boot_failed() -> None:
    """If ``tmux new-session`` fails, cold-start lands BOOTING → DEAD via
    BOOT_FAILED (not silent disconnect)."""
    tmux = _make_mock_tmux()
    tmux.new_session = AsyncMock(return_value=_fail("rc=1"))
    ss, _ = _make_session(tmux=tmux)
    with pytest.raises(RuntimeError, match="tmux new-session failed"):
        await ss.connect()
    assert ss.state == SessionState.DEAD


@pytest.mark.asyncio
async def test_cold_start_reaps_stale_session_before_spawn() -> None:
    """If a stale tmux session is found at cold-start time (e.g. previous
    daemon crashed without graceful disconnect), reap it first."""
    tmux = _make_mock_tmux(has_session_initial=True)
    ss, _ = _make_session(tmux=tmux)
    await ss.connect()
    # has_session checked, then kill_session called for the stale reap,
    # then new_session for the fresh spawn.
    tmux.has_session.assert_awaited()
    tmux.kill_session.assert_awaited()
    tmux.new_session.assert_awaited_once()


@pytest.mark.asyncio
async def test_cold_start_uses_correct_claude_invocation(tmp_path, monkeypatch) -> None:
    """The in-pane command must be
    ``claude --continue --dangerously-skip-permissions`` when a prior
    transcript exists for cwd. Pinned because a typo in the invocation
    silently breaks billing semantics (would hit SDK credits instead of
    subscription).

    Post-#511: ``--continue`` is gated on transcript existence, so this
    test pre-seeds a fake transcript before asserting.
    """
    # Point HOME at tmp so we can seed a transcript at the encoded-cwd path.
    monkeypatch.setenv("HOME", str(tmp_path))
    ss, tmux = _make_session()
    project_dir = ss._project_dir()
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / "seed.jsonl").write_text("")
    await ss.connect()
    _, kwargs = tmux.new_session.call_args
    cmd = kwargs["command"]
    assert "claude" in cmd
    assert "--continue" in cmd
    assert "--dangerously-skip-permissions" in cmd


@pytest.mark.asyncio
async def test_cold_start_omits_continue_when_no_prior_transcript(
    tmp_path, monkeypatch
) -> None:
    """Issue #511 regression: a freshly-registered agent has no transcript
    at ``~/.claude/projects/<encoded-cwd>/``. ``claude --continue`` exits 1
    in that case, tmux auto-reaps the detached session, and the Python
    state machine ends up CONNECTED against a dead REPL.

    Fix (#512): cold-start cmd must fall through to ``claude`` (no
    ``--continue``) when no prior transcript exists. The Claude CLI
    then creates a fresh transcript on the first turn, and subsequent
    reconnects find it and resume normally.
    """
    # Point HOME at an empty tmp dir — no project_dir, no transcripts.
    monkeypatch.setenv("HOME", str(tmp_path))
    ss, tmux = _make_session()
    await ss.connect()
    _, kwargs = tmux.new_session.call_args
    cmd = kwargs["command"]
    assert "claude" in cmd
    assert "--dangerously-skip-permissions" in cmd
    # The critical assertion — no --continue when no prior transcript.
    assert "--continue" not in cmd


def test_has_prior_transcript_false_when_project_dir_missing(
    tmp_path, monkeypatch
) -> None:
    """``_has_prior_transcript`` returns False when the encoded-cwd
    project dir doesn't exist (cold-start case)."""
    monkeypatch.setenv("HOME", str(tmp_path))
    ss, _ = _make_session()
    assert ss._has_prior_transcript() is False


def test_has_prior_transcript_false_when_project_dir_empty(
    tmp_path, monkeypatch
) -> None:
    """``_has_prior_transcript`` returns False when the project dir
    exists but contains no ``*.jsonl`` transcripts.

    Defends the race where Claude Code has created the directory
    (e.g. via a SessionStart hook) but hasn't written a transcript yet.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    ss, _ = _make_session()
    project_dir = ss._project_dir()
    project_dir.mkdir(parents=True, exist_ok=True)
    # Drop an unrelated file in there to confirm the glob filters by suffix.
    (project_dir / "not-a-transcript.txt").write_text("")
    assert ss._has_prior_transcript() is False


def test_has_prior_transcript_true_when_jsonl_exists(tmp_path, monkeypatch) -> None:
    """``_has_prior_transcript`` returns True when at least one .jsonl
    transcript exists for the agent's cwd."""
    monkeypatch.setenv("HOME", str(tmp_path))
    ss, _ = _make_session()
    project_dir = ss._project_dir()
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / "abc123.jsonl").write_text("")
    assert ss._has_prior_transcript() is True


def test_build_claude_cmd_includes_dangerously_skip_when_no_transcript(
    tmp_path, monkeypatch
) -> None:
    """Even with no prior transcript, the cold-start cmd must still
    carry ``--dangerously-skip-permissions`` (the non-interactive
    bootstrap flag). Pinning so the #511 fix can't accidentally regress
    the unrelated permissions handling.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    ss, _ = _make_session()
    cmd = ss._build_claude_cmd()
    assert "claude" in cmd
    assert "--dangerously-skip-permissions" in cmd
    assert "--continue" not in cmd


# ──────────────────────────────────────────────────────────────────────────
# capture_pane signature: escapes flag for the read-only pane viewer
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_capture_pane_default_omits_escapes_flag() -> None:
    """Existing callers that want plain text shouldn't suddenly start
    getting ANSI escapes back. Default ``escapes=False`` keeps the
    response pipeline's fallback capture clean."""
    tmux = _TmuxControl("pinky-test")
    calls: list[tuple[str, ...]] = []

    async def fake_run(*args, timeout=5.0):
        calls.append(args)
        return _ok()

    tmux._run = fake_run
    await tmux.capture_pane(lines=50)
    assert "-e" not in calls[0]
    assert "-p" in calls[0]
    assert "-S" in calls[0]


@pytest.mark.asyncio
async def test_capture_pane_with_escapes_adds_e_flag() -> None:
    """The pane-viewer endpoint needs ANSI escapes preserved for xterm.js
    rendering. ``escapes=True`` adds ``-e`` to the tmux invocation."""
    tmux = _TmuxControl("pinky-test")
    calls: list[tuple[str, ...]] = []

    async def fake_run(*args, timeout=5.0):
        calls.append(args)
        return _ok()

    tmux._run = fake_run
    await tmux.capture_pane(lines=200, escapes=True)
    assert "-e" in calls[0]


# ──────────────────────────────────────────────────────────────────────────
# resize_window / resize_pane: viewer reshapes tmux pane to fit the modal
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_resize_window_invokes_tmux_with_xy_flags() -> None:
    """``_TmuxControl.resize_window`` must call ``tmux resize-window``
    targeting the session with ``-x cols -y rows`` — that's what
    propagates the new geometry to the single pane (Claude Code's TUI
    then redraws on SIGWINCH)."""
    tmux = _TmuxControl("pinky-test")
    calls: list[tuple[str, ...]] = []

    async def fake_run(*args, timeout=5.0):
        calls.append(args)
        return _ok()

    tmux._run = fake_run
    await tmux.resize_window(cols=180, rows=48)

    assert len(calls) == 1
    args = calls[0]
    assert args[0] == "resize-window"
    assert "-t" in args and "pinky-test" in args
    assert "-x" in args and "180" in args
    assert "-y" in args and "48" in args


@pytest.mark.asyncio
async def test_resize_window_clamps_dims_into_safe_range() -> None:
    """Defensive clamping: callers come from the network (query params)
    so we don't trust them. Below-floor values clamp up to a Claude-
    Code-renderable size; above-ceiling values clamp down to a tmux-
    accepting size."""
    tmux = _TmuxControl("pinky-test")
    calls: list[tuple[str, ...]] = []

    async def fake_run(*args, timeout=5.0):
        calls.append(args)
        return _ok()

    tmux._run = fake_run

    # Below floor: cols<20 → 20, rows<10 → 10.
    await tmux.resize_window(cols=5, rows=2)
    assert "20" in calls[-1] and "10" in calls[-1]

    # Above ceiling: cols>500 → 500, rows>200 → 200.
    await tmux.resize_window(cols=9999, rows=9999)
    assert "500" in calls[-1] and "200" in calls[-1]


@pytest.mark.asyncio
async def test_resize_pane_returns_true_on_success() -> None:
    """Happy path: tmux returns 0, ``resize_pane`` returns ``True`` so
    the caller can log success / proceed."""
    session, tmux = _make_session()
    tmux.resize_window = AsyncMock(return_value=_ok())

    ok = await session.resize_pane(cols=200, rows=60)

    assert ok is True
    tmux.resize_window.assert_awaited_once_with(cols=200, rows=60)


@pytest.mark.asyncio
async def test_resize_pane_returns_false_on_tmux_failure() -> None:
    """tmux non-zero exit shouldn't bubble — the snapshot stream is
    more valuable than the resize. ``resize_pane`` returns ``False``
    and the stream continues (slightly mis-sized snapshot is better
    than aborting the modal)."""
    session, tmux = _make_session()
    tmux.resize_window = AsyncMock(
        return_value=_fail("can't find window")
    )

    ok = await session.resize_pane(cols=200, rows=60)

    assert ok is False


@pytest.mark.asyncio
async def test_resize_pane_swallows_subprocess_exceptions() -> None:
    """Defensive: any unexpected raise from the subprocess layer is
    logged + returns ``False`` instead of crashing the SSE generator
    (which would close the viewer mid-stream)."""
    session, tmux = _make_session()
    tmux.resize_window = AsyncMock(side_effect=RuntimeError("kaboom"))

    ok = await session.resize_pane(cols=120, rows=40)

    assert ok is False


# ──────────────────────────────────────────────────────────────────────────
# #514 — paste-buffer + delayed Enter for prompt delivery
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_paste_text_sets_buffer_pastes_and_sends_enter() -> None:
    """``_TmuxControl.paste_text`` must invoke three tmux subcommands
    in order: ``set-buffer``, ``paste-buffer -p`` (bracketed paste),
    then ``send-keys Enter``. The bracketed-paste mode is what makes
    this reliable across the claude cold-start splash UI (#514).
    """
    tmux = _TmuxControl("pinky-test")

    calls: list[tuple[str, ...]] = []

    async def fake_run(*args, timeout=5.0):
        calls.append(args)
        return _ok()

    tmux._run = fake_run
    result = await tmux.paste_text("hello world", enter_delay_ms=0)

    # Expect three commands in order.
    assert len(calls) == 3
    assert calls[0][:3] == ("set-buffer", "-b", "pinky-pinky-test")
    assert calls[0][3] == "hello world"
    assert "paste-buffer" in calls[1][0]
    assert "-p" in calls[1]  # bracketed paste mode
    assert "-d" in calls[1]  # delete buffer after paste
    assert calls[2] == ("send-keys", "-t", "pinky-test", "Enter")
    assert result.ok


@pytest.mark.asyncio
async def test_paste_text_skips_enter_when_enter_false() -> None:
    """``enter=False`` leaves the pasted text in claude's input buffer
    unsubmitted. Used by callers who want to stage a prompt without
    triggering a turn (e.g. internal setup, debugging)."""
    tmux = _TmuxControl("pinky-test")

    calls: list[tuple[str, ...]] = []

    async def fake_run(*args, timeout=5.0):
        calls.append(args)
        return _ok()

    tmux._run = fake_run
    await tmux.paste_text("hello", enter=False, enter_delay_ms=0)

    assert len(calls) == 2  # set-buffer + paste-buffer only
    assert not any("send-keys" in c[0] for c in calls)


@pytest.mark.asyncio
async def test_paste_text_short_circuits_on_set_buffer_failure() -> None:
    """If ``set-buffer`` fails (tmux server down, bad session name),
    paste_text returns the failure immediately without trying paste
    or Enter."""
    tmux = _TmuxControl("pinky-test")

    calls: list[tuple[str, ...]] = []

    async def fake_run(*args, timeout=5.0):
        calls.append(args)
        return _fail("set-buffer broke")

    tmux._run = fake_run
    result = await tmux.paste_text("hello", enter_delay_ms=0)

    assert len(calls) == 1
    assert not result.ok


@pytest.mark.asyncio
async def test_paste_text_short_circuits_on_paste_failure() -> None:
    """If ``paste-buffer`` fails after a successful ``set-buffer``,
    paste_text returns the failure without trying Enter. Skipping the
    Enter avoids submitting stale buffer content from a previous turn.
    """
    tmux = _TmuxControl("pinky-test")

    calls: list[tuple[str, ...]] = []

    async def fake_run(*args, timeout=5.0):
        calls.append(args)
        if args[0] == "paste-buffer":
            return _fail("paste broke")
        return _ok()

    tmux._run = fake_run
    result = await tmux.paste_text("hello", enter_delay_ms=0)

    assert len(calls) == 2  # set-buffer + paste-buffer; no send-keys
    assert not result.ok


@pytest.mark.asyncio
async def test_paste_text_waits_enter_delay_between_paste_and_enter() -> None:
    """The Enter delay between paste and Enter is the mechanism that
    lets claude's cold-start splash UI dismiss itself before the
    submit Enter arrives. Pinning so the sleep can't be accidentally
    removed during refactor.
    """
    tmux = _TmuxControl("pinky-test")
    sleep_durations: list[float] = []

    original_sleep = asyncio.sleep

    async def tracked_sleep(seconds):
        sleep_durations.append(seconds)
        # No-op the actual sleep so the test runs fast.
        await original_sleep(0)

    async def fake_run(*args, timeout=5.0):
        return _ok()

    tmux._run = fake_run
    # Patch asyncio.sleep IN the module under test, not globally.
    original = tmux_session.asyncio.sleep
    tmux_session.asyncio.sleep = tracked_sleep
    try:
        await tmux.paste_text("hello", enter_delay_ms=250)
    finally:
        tmux_session.asyncio.sleep = original

    assert 0.25 in sleep_durations


@pytest.mark.asyncio
async def test_deliver_turn_uses_paste_text_not_send_keys() -> None:
    """The worker's per-turn delivery must go through paste_text (not
    raw send-keys) so cold-start splash absorption (#514) is avoided.
    Pinning so a future refactor can't silently revert the delivery
    path."""
    tmux = _make_mock_tmux()
    tmux.paste_text = AsyncMock(return_value=_ok())
    ss, _ = _make_session(tmux=tmux)
    ss._state_machine._state = SessionState.CONNECTED

    turn = _QueuedTurn(
        prompt="hello dymok",
        platform="telegram",
        chat_id="123",
        message_id="m1",
    )
    await ss._deliver_turn(turn)

    tmux.paste_text.assert_awaited_once()
    args, kwargs = tmux.paste_text.call_args
    assert args[0] == "hello dymok" or kwargs.get("text") == "hello dymok"
    # And raw send_keys must NOT have been used for dispatch.
    tmux.send_keys.assert_not_awaited()


# ──────────────────────────────────────────────────────────────────────────
# REPL env propagation — #515 follow-up.
#
# Tmux ``new-session`` only propagates env via explicit ``-e KEY=VAL``;
# parent env is dropped (except the small ``update-environment``
# allowlist). Without explicit propagation, every PinkyBot-managed hook
# silently exits at ``if not secret: sys.exit(0)`` and the SessionStart
# tailer-repoint, Stop wake, presence updates, and effort-drift logs
# all stop working for tmux agents.
# ──────────────────────────────────────────────────────────────────────────


def test_build_repl_env_propagates_pinky_session_secret_when_set(
    monkeypatch,
) -> None:
    """When the daemon env has ``PINKY_SESSION_SECRET``, it must be
    included in the tmux env so the HMAC-signing hook scripts inside
    the tmux session can authenticate to the daemon."""
    monkeypatch.setenv("PINKY_SESSION_SECRET", "test-secret-32-bytes-min-xyz")
    ss, _ = _make_session()
    env = ss._build_repl_env()
    assert env.get("PINKY_SESSION_SECRET") == "test-secret-32-bytes-min-xyz"


def test_build_repl_env_omits_pinky_session_secret_when_unset(
    monkeypatch,
) -> None:
    """When the daemon env has no ``PINKY_SESSION_SECRET`` (dev-mode,
    misconfigured deploy), the env must NOT include an empty
    ``PINKY_SESSION_SECRET=``. Hooks already handle missing-secret
    gracefully (silent no-op); polluting tmux with an empty value
    risks future bugs where empty-string is treated as "present"."""
    monkeypatch.delenv("PINKY_SESSION_SECRET", raising=False)
    ss, _ = _make_session()
    env = ss._build_repl_env()
    assert "PINKY_SESSION_SECRET" not in env


def test_build_repl_env_strips_whitespace_in_pinky_session_secret(
    monkeypatch,
) -> None:
    """Whitespace-only env value is treated as unset. Defends against
    ``PINKY_SESSION_SECRET=" "`` accidentally passing the truthy guard
    while still failing HMAC verification on the daemon side."""
    monkeypatch.setenv("PINKY_SESSION_SECRET", "   ")
    ss, _ = _make_session()
    env = ss._build_repl_env()
    assert "PINKY_SESSION_SECRET" not in env


# ──────────────────────────────────────────────────────────────────────────
# Concurrent cold-start race (PR6 framework: Case A + Case B)
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_concurrent_cold_start_runs_one_tmux_spawn() -> None:
    """PR6's canonical concurrent-connect race regression, applied to the
    greenfield tmux backend. Two concurrent connect() calls must result
    in exactly one tmux new-session.

    By the time the second caller enters connect(), state is BOOTING
    (the first caller flipped it at grant time). The widened guard
    (``state in {UNINITIALIZED, BOOTING}``) routes the second caller to
    the same-target in-flight branch — subscribes via InFlightHandle,
    inherits the owner's CONNECTED outcome.
    """
    tmux = _make_mock_tmux()
    release_spawn = asyncio.Event()
    spawn_started = asyncio.Event()
    spawn_count = 0

    async def blocking_new_session(*, cwd, command, env=None):
        nonlocal spawn_count
        spawn_count += 1
        spawn_started.set()
        await release_spawn.wait()
        return _ok()

    tmux.new_session = AsyncMock(side_effect=blocking_new_session)
    ss, _ = _make_session(tmux=tmux)

    t1 = asyncio.create_task(ss.connect())
    await spawn_started.wait()
    assert ss.state == SessionState.BOOTING, (
        "First caller must hold BOOTING ownership while spawn is in flight"
    )

    t2 = asyncio.create_task(ss.connect())
    # Yield to let t2 subscribe.
    for _ in range(10):
        await asyncio.sleep(0)

    assert spawn_count == 1, (
        f"Greenfield TmuxSession must inherit PR6's one-spawn invariant; "
        f"got {spawn_count} concurrent tmux new-session calls"
    )

    release_spawn.set()
    await asyncio.gather(t1, t2)

    assert spawn_count == 1
    assert ss.state == SessionState.CONNECTED


@pytest.mark.asyncio
async def test_concurrent_cold_start_subscriber_raises_on_owner_dead() -> None:
    """Case B — owner's spawn raises, subscriber inherits DEAD and raises.

    Subscriber must NOT silently return as if connected (which would
    leave the broker thinking tmux is up when it isn't).
    """
    tmux = _make_mock_tmux()
    release_spawn = asyncio.Event()
    spawn_started = asyncio.Event()

    async def failing_new_session(*, cwd, command, env=None):
        spawn_started.set()
        await release_spawn.wait()
        return _fail("rc=1")

    tmux.new_session = AsyncMock(side_effect=failing_new_session)
    ss, _ = _make_session(tmux=tmux)

    t1 = asyncio.create_task(ss.connect())
    await spawn_started.wait()
    assert ss.state == SessionState.BOOTING

    t2 = asyncio.create_task(ss.connect())
    for _ in range(10):
        await asyncio.sleep(0)

    release_spawn.set()
    # Owner re-raises the original tmux failure; subscriber raises with
    # the "resolved to dead" marker.
    with pytest.raises(RuntimeError, match="tmux new-session failed"):
        await t1
    with pytest.raises(RuntimeError, match="resolved to dead"):
        await t2
    assert ss.state == SessionState.DEAD


# ──────────────────────────────────────────────────────────────────────────
# Cold-start Case D (post-DEAD rejection) — Pushok PR #495 round-1 nit 2
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cold_start_rejection_post_dead_raises() -> None:
    """Pushok's Case D from PR #494, applied to TmuxSession. A caller
    enters connect() observing state == BOOTING, but by the time
    request_transition acquires the lock the owner has already completed
    to DEAD. The matrix rejection branch (in_flight_handle is None) must
    surface the failure, not silently return as if connected.

    Surrogate test — pre-set state to BOOTING and patch request_transition
    to return rejection + DEAD state, matching the race outcome.
    """
    ss, _ = _make_session(state=SessionState.BOOTING)

    async def fake_request_transition(target, trigger, *, reason=None):
        # Simulate the race outcome: owner just completed DEAD; rejection.
        ss._state_machine._state = SessionState.DEAD
        return TransitionResult(
            changed=False,
            from_state=SessionState.DEAD,
            to_state=SessionState.DEAD,
            rejection_reason="phantom: owner completed DEAD before subscribe",
        )

    ss._state_machine.request_transition = fake_request_transition  # type: ignore[assignment]

    with pytest.raises(RuntimeError, match="post-DEAD"):
        await ss.connect()


# ──────────────────────────────────────────────────────────────────────────
# Warm-wake from IDLE_SLEEPING / DEAD — Murzik PR #495 round-1 finding 1
# ──────────────────────────────────────────────────────────────────────────
#
# Pre-fix: connect() only handled UNINITIALIZED/BOOTING. IDLE_SLEEPING and
# DEAD entries fell through to direct-mutating CONNECTED via the warm-
# reconnect else branch — skipping the matrix IDLE_SLEEPING|DEAD →
# RECONNECTING edge entirely, and giving concurrent wakes no subscriber
# protection.
#
# Post-fix: connect() takes a ``trigger`` parameter; IDLE_SLEEPING/DEAD
# entries drive ``→ RECONNECTING`` via the caller-supplied trigger
# (default BROKER — the most common caller: broker auto-wake on inbound).
# Same in-flight subscriber protection as cold-start.


@pytest.mark.asyncio
async def test_warm_wake_from_idle_sleeping_drives_through_reconnecting() -> None:
    """Auto-wake on inbound from IDLE_SLEEPING must drive
    IDLE_SLEEPING → RECONNECTING → CONNECTED, NOT direct-mutate CONNECTED.

    The matrix audit log captures every transition; pre-fix the
    IDLE_SLEEPING → RECONNECTING edge was invisible because the code
    skipped it entirely.
    """
    ss, tmux = _make_session(state=SessionState.IDLE_SLEEPING)
    await ss.connect()  # default trigger=BROKER

    assert ss.state == SessionState.CONNECTED
    # The new-session call confirms we ran the warm-wake spawn.
    tmux.new_session.assert_awaited_once()


@pytest.mark.asyncio
async def test_warm_wake_from_dead_drives_through_reconnecting() -> None:
    """Same path from DEAD — the resurrection-on-inbound case.
    api._heartbeat_resurrect relies on this working; pre-fix it would
    silently bail because INTERNAL isn't legal for DEAD → RECONNECTING."""
    ss, _ = _make_session(state=SessionState.DEAD)
    await ss.connect(trigger=Trigger.BROKER)
    assert ss.state == SessionState.CONNECTED


@pytest.mark.asyncio
async def test_warm_wake_failure_drives_to_dead() -> None:
    """If spawn fails during warm-wake, the in-flight transition completes
    DEAD via the emergency-exit path. State must NOT be left parked in
    RECONNECTING — that would strand subscribers + leak the in-flight
    record (driver-abandonment failure mode)."""
    tmux = _make_mock_tmux()
    tmux.new_session = AsyncMock(return_value=_fail("simulated wake failure"))
    ss, _ = _make_session(state=SessionState.IDLE_SLEEPING, tmux=tmux)

    with pytest.raises(RuntimeError, match="tmux new-session failed"):
        await ss.connect()
    assert ss.state == SessionState.DEAD


@pytest.mark.asyncio
async def test_concurrent_warm_wake_runs_one_spawn() -> None:
    """Concurrent connect() on an IDLE_SLEEPING session must result in
    exactly one tmux spawn. Same shape as the cold-start Case A
    regression — caller A wins RECONNECTING ownership, caller B
    subscribes via the in-flight handle.

    Pre-fix: both callers fell through to ``_spawn_tmux_repl`` and
    direct-mutated CONNECTED — double-spawn, no subscriber protection.
    Post-fix: matrix subscriber path applies to warm-wake too.
    """
    tmux = _make_mock_tmux()
    release_spawn = asyncio.Event()
    spawn_started = asyncio.Event()
    spawn_count = 0

    async def blocking_new_session(*, cwd, command, env=None):
        nonlocal spawn_count
        spawn_count += 1
        spawn_started.set()
        await release_spawn.wait()
        return _ok()

    tmux.new_session = AsyncMock(side_effect=blocking_new_session)
    ss, _ = _make_session(state=SessionState.IDLE_SLEEPING, tmux=tmux)

    t1 = asyncio.create_task(ss.connect())
    await spawn_started.wait()
    assert ss.state == SessionState.RECONNECTING, (
        "First caller must hold RECONNECTING ownership while spawn is "
        "in flight (warm-wake path)"
    )

    t2 = asyncio.create_task(ss.connect())
    for _ in range(10):
        await asyncio.sleep(0)

    assert spawn_count == 1, (
        f"Warm-wake concurrent-connect must run exactly one tmux spawn; "
        f"got {spawn_count}"
    )

    release_spawn.set()
    await asyncio.gather(t1, t2)
    assert spawn_count == 1
    assert ss.state == SessionState.CONNECTED


@pytest.mark.asyncio
async def test_warm_wake_uses_caller_supplied_trigger() -> None:
    """Trigger threads through to the matrix audit. WATCHDOG is the
    canonical resurrect-from-watchdog trigger; verify it's accepted.
    Matrix-legality is enforced by the state machine — this test pins
    that the call doesn't crash and lands CONNECTED."""
    ss, _ = _make_session(state=SessionState.DEAD)
    await ss.connect(trigger=Trigger.WATCHDOG)
    assert ss.state == SessionState.CONNECTED


@pytest.mark.asyncio
async def test_connect_from_connected_is_no_op() -> None:
    """Post-completion straggler — connect() called while already
    CONNECTED returns silently. Pushok's Case C from PR #494, applied to
    TmuxSession. No double-spawn, no state mutation."""
    ss, tmux = _make_session(state=SessionState.CONNECTED)
    await ss.connect()
    assert ss.state == SessionState.CONNECTED
    tmux.new_session.assert_not_awaited()


# ──────────────────────────────────────────────────────────────────────────
# attempt_reconnect with trigger awareness — Murzik PR #495 round-1 finding 2
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_attempt_reconnect_from_dead_lands_connected_with_broker_trigger() -> None:
    """Murzik's finding 2: pre-fix attempt_reconnect used Trigger.INTERNAL
    unconditionally. INTERNAL is matrix-rejected from DEAD/IDLE_SLEEPING
    (only BROKER/WATCHDOG/SCHEDULER/API_ADMIN are legal for those edges),
    so a DEAD agent's reconnect would silently bail without ever retrying.

    Post-fix: caller-supplied trigger threads through. With BROKER (the
    default), DEAD → RECONNECTING → CONNECTED works."""
    ss, _ = _make_session(state=SessionState.DEAD)
    ss._RECONNECT_BACKOFF = (0,)  # speed up the test
    # Override module constant locally so the test doesn't sleep.
    import pinky_daemon.tmux_session as ts_mod
    original_backoff = ts_mod._RECONNECT_BACKOFF
    ts_mod._RECONNECT_BACKOFF = (0,)
    try:
        await ss.attempt_reconnect()  # default trigger=BROKER
        assert ss.state == SessionState.CONNECTED
    finally:
        ts_mod._RECONNECT_BACKOFF = original_backoff


@pytest.mark.asyncio
async def test_attempt_reconnect_from_idle_sleeping_lands_connected_with_watchdog_trigger() -> None:
    """Watchdog-driven warm-wake from IDLE_SLEEPING via attempt_reconnect.
    Pins the IDLE_SLEEPING → RECONNECTING edge with WATCHDOG trigger."""
    ss, _ = _make_session(state=SessionState.IDLE_SLEEPING)
    import pinky_daemon.tmux_session as ts_mod
    original_backoff = ts_mod._RECONNECT_BACKOFF
    ts_mod._RECONNECT_BACKOFF = (0,)
    try:
        await ss.attempt_reconnect(trigger=Trigger.WATCHDOG)
        assert ss.state == SessionState.CONNECTED
    finally:
        ts_mod._RECONNECT_BACKOFF = original_backoff


@pytest.mark.asyncio
async def test_attempt_reconnect_exhausted_budget_lands_dead() -> None:
    """If all retries fail, the in-flight transition completes DEAD."""
    tmux = _make_mock_tmux()
    tmux.new_session = AsyncMock(return_value=_fail("persistent failure"))
    ss, _ = _make_session(state=SessionState.DEAD, tmux=tmux)
    import pinky_daemon.tmux_session as ts_mod
    original_backoff = ts_mod._RECONNECT_BACKOFF
    ts_mod._RECONNECT_BACKOFF = (0, 0)  # 2 failed attempts, no sleep
    try:
        await ss.attempt_reconnect(trigger=Trigger.BROKER)
        assert ss.state == SessionState.DEAD
    finally:
        ts_mod._RECONNECT_BACKOFF = original_backoff


# ──────────────────────────────────────────────────────────────────────────
# disconnect + idle_sleep + force_restart choreography
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_disconnect_from_connected_lands_in_dead() -> None:
    """Default disconnect (no prior intent set) lands CONNECTED → DEAD.
    Matches StreamingSession.disconnect's contract."""
    ss, tmux = _make_session(state=SessionState.CONNECTED)
    await ss.disconnect()
    assert ss.state == SessionState.DEAD
    tmux.kill_session.assert_awaited()


@pytest.mark.asyncio
async def test_idle_sleep_drives_to_idle_sleeping_not_dead() -> None:
    """idle_sleep must pre-set IDLE_SLEEPING so disconnect's default
    CONNECTED → DEAD fallback doesn't fire. Otherwise the watchdog's
    resurrection callback would race the idle-sleep intent.

    Same flicker-class bug as Pushok's PR #492 Nit 2 on CodexSession.
    """
    ss, tmux = _make_session(state=SessionState.CONNECTED)

    # idle_sleep now enqueues a pre-sleep prompt via
    # ``_enqueue_internal_prompt(wait_for_completion=True, timeout_sec=120)``
    # before the state transition. This unit test doesn't run a real
    # message queue/worker/tailer, so the wait would block on the
    # 120s timeout. Stub the helper to a no-op (same pattern used by
    # ``TestIdleSleepPresavePrompt`` below) so we exercise only the
    # CONNECTED → IDLE_SLEEPING transition this test is pinning.
    async def _noop(prompt, *, reason, wait_for_completion=False, timeout_sec=None):
        return None

    ss._enqueue_internal_prompt = _noop

    result = await ss.idle_sleep()
    assert result is True
    assert ss.state == SessionState.IDLE_SLEEPING
    tmux.kill_session.assert_awaited()


@pytest.mark.asyncio
async def test_idle_sleep_returns_false_when_not_connected() -> None:
    ss, _ = _make_session(state=SessionState.DEAD)
    result = await ss.idle_sleep()
    assert result is False
    assert ss.state == SessionState.DEAD


@pytest.mark.asyncio
async def test_force_restart_holds_reconnecting_across_disconnect_and_spawn() -> None:
    """Mirror of test_force_restart_holds_reconnecting_across_disconnect_and_connect
    from test_streaming_session.py. The macro state must stay RECONNECTING
    throughout the restart — no flicker through DEAD.
    """
    ss, tmux = _make_session(state=SessionState.CONNECTED)

    observed_states: list[SessionState] = []
    original_kill = tmux.kill_session

    async def kill_with_observation(*args, **kwargs):
        observed_states.append(ss.state)
        return await original_kill(*args, **kwargs)

    tmux.kill_session = AsyncMock(side_effect=kill_with_observation)

    result = await ss.force_restart()
    assert result is True
    # State at every observation point during the restart must be
    # RECONNECTING (or CONNECTED at the very end), never DEAD.
    for s in observed_states:
        assert s == SessionState.RECONNECTING, (
            f"force_restart must hold RECONNECTING across teardown — "
            f"observed {s}"
        )
    assert ss.state == SessionState.CONNECTED


@pytest.mark.asyncio
async def test_force_restart_failure_lands_in_dead() -> None:
    """If the re-spawn fails after disconnect, force_restart returns False
    and the state machine lands DEAD."""
    ss, tmux = _make_session(state=SessionState.CONNECTED)
    # First new_session call (post-restart spawn) fails.
    tmux.new_session = AsyncMock(return_value=_fail("re-spawn failed"))
    result = await ss.force_restart()
    assert result is False
    assert ss.state == SessionState.DEAD


@pytest.mark.asyncio
async def test_force_restart_skips_restart_guard_before_first_completed_turn() -> None:
    """A pre-first-turn tmux restart cannot lose completed work, so the
    persistence guard must not block watchdog recovery from a cold-start wedge.
    """
    guard = MagicMock(return_value={"restart_safe": False, "reason": "no save"})
    ss, tmux = _make_session(state=SessionState.CONNECTED, restart_guard=guard)

    result = await ss.force_restart()

    assert result is True
    assert ss.state == SessionState.CONNECTED
    guard.assert_not_called()
    tmux.kill_session.assert_awaited()


@pytest.mark.asyncio
async def test_force_restart_honors_restart_guard_after_completed_turn() -> None:
    """Once any turn has completed, force_restart keeps the existing
    persistence guard behavior to avoid dropping unsaved agent state.

    #518 retargeting note: assertion was ``send_keys.assert_awaited()``
    before #518 moved per-turn dispatch to ``paste_text`` (bracketed
    paste + delayed Enter for cold-start splash survival). Updated to
    pin the new contract; this test slipped through #518's PR-level
    CI as a rebase artifact and broke main, picked up here in #519.
    """
    guard = MagicMock(return_value={"restart_safe": False, "reason": "stale"})
    ss, tmux = _make_session(restart_guard=guard)
    await ss.connect()

    await ss.send(prompt="done", platform="t", chat_id="c", message_id="m")
    for _ in range(20):
        await asyncio.sleep(0)
        if tmux.paste_text.await_count >= 1:
            break
    tmux.paste_text.assert_awaited()

    await ss._handle_turn_complete(TurnResponse(text="ok", stop_reason="end_turn"))
    for _ in range(20):
        await asyncio.sleep(0)
        if ss._has_completed_turn:
            break
    assert ss._has_completed_turn is True

    result = await ss.force_restart()

    assert result is False
    assert ss.state == SessionState.CONNECTED
    guard.assert_called_once_with(ss)
    tmux.kill_session.assert_not_awaited()
    await ss.disconnect()


# ──────────────────────────────────────────────────────────────────────────
# send + worker
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_send_drops_when_not_connected() -> None:
    """Per Transport contract, send() drops when not CONNECTED. Matches
    StreamingSession's legacy drop-silent behavior."""
    ss, tmux = _make_session(state=SessionState.DEAD)
    await ss.send("hello", platform="telegram", chat_id="123")
    tmux.paste_text.assert_not_awaited()
    assert ss._stats["messages_sent"] == 0


@pytest.mark.asyncio
async def test_send_queues_and_worker_delivers_via_paste_text() -> None:
    """Happy path: cold-start, send a message, worker dequeues and
    pushes via tmux paste_text (bracketed paste + delayed Enter, the
    #514 fix). Pins the paste_text invocation shape (enter=True
    submits the prompt after the cold-start splash dismisses)."""
    ss, tmux = _make_session()
    await ss.connect()
    await ss.send("hello world", platform="telegram", chat_id="123")

    # Let the worker drain one item.
    for _ in range(20):
        await asyncio.sleep(0)
        if tmux.paste_text.await_count >= 1:
            break

    tmux.paste_text.assert_awaited()
    # paste_text is called with the prompt + enter=True (default).
    args, kwargs = tmux.paste_text.call_args
    assert args[0] == "hello world"
    assert kwargs.get("enter", True) is True

    # Clean up — cancel worker so the test doesn't leak the task.
    await ss.disconnect()


@pytest.mark.asyncio
async def test_send_increments_turn_and_message_counters() -> None:
    ss, tmux = _make_session()
    await ss.connect()
    await ss.send("first", platform="telegram", chat_id="123")
    for _ in range(20):
        await asyncio.sleep(0)
        if ss._stats["turns"] >= 1:
            break
    assert ss._stats["messages_sent"] == 1
    assert ss._stats["turns"] == 1
    await ss.disconnect()


# ──────────────────────────────────────────────────────────────────────────
# Effort knob (protocol parity; no in-session effect on tmux backend)
# ──────────────────────────────────────────────────────────────────────────


def test_set_effort_accepts_valid_levels() -> None:
    ss, _ = _make_session()
    for level in ("low", "medium", "high", "xhigh", "max", "auto"):
        ss.set_effort(level)


def test_set_effort_rejects_invalid_level() -> None:
    ss, _ = _make_session()
    with pytest.raises(ValueError, match="invalid effort"):
        ss.set_effort("nuclear")


def test_set_effort_auto_clears_override() -> None:
    ss, _ = _make_session()
    ss.set_effort("max")
    assert ss.effective_effort == "max"
    ss.set_effort("auto")
    # auto resolves to medium (default) per the contract.
    assert ss.effective_effort == "medium"


def test_clear_effort_override_resets_to_config_default() -> None:
    ss, _ = _make_session()
    ss.set_effort("max")
    ss.clear_effort_override()
    # Falls back to config's thinking_effort, defaulting to "medium".
    assert ss.effective_effort == "medium"


# ──────────────────────────────────────────────────────────────────────────
# stats shape
# ──────────────────────────────────────────────────────────────────────────


def test_stats_shape_matches_broker_consumer_keys() -> None:
    """Stats dict must include the keys broker/api/watchdog read.

    Intentionally absent: ``cost_usd`` — tmux billing is against the
    subscription, not per-turn metered. Documented gap.
    """
    ss, _ = _make_session(state=SessionState.CONNECTED)
    stats = ss.stats
    for key in ("turns", "messages_sent", "errors", "reconnects",
                "auto_restarts", "state", "thinking_effort",
                "current_thinking"):
        assert key in stats, f"stats missing required key: {key}"
    # state stringified for JSON-friendly transport over the API.
    assert stats["state"] == "connected"
    # cost_usd not reported.
    assert "cost_usd" not in stats


# ──────────────────────────────────────────────────────────────────────────
# PR8b — Response capture pipeline integration
# ──────────────────────────────────────────────────────────────────────────


class _AsyncCollector:
    """Drop-in async callback that records TurnResponse calls."""

    def __init__(self) -> None:
        self.calls: list[TurnResponse] = []

    async def __call__(self, response: TurnResponse):
        self.calls.append(response)


def _make_session_with_response_cb(
    *, response_cb=None, conv_store=None, stream_evt=None,
) -> tuple[TmuxSession, MagicMock]:
    """TmuxSession built with the response-side callbacks wired up."""
    cfg = StreamingSessionConfig(
        agent_name="dymok",
        working_dir="/tmp/tmux-session-test",
    )
    tmux = _make_mock_tmux()
    ss = TmuxSession(
        cfg,
        tmux_control=tmux,
        response_callback=response_cb,
        conversation_store=conv_store,
        stream_event_callback=stream_evt,
    )
    # Skip wake-prompt enqueue — see _make_session docstring. Tests that
    # specifically exercise wake-prompt behavior flip this back to False.
    ss._skip_wake_prompt_for_tests = True
    return ss, tmux


@pytest.mark.asyncio
async def test_connect_starts_tailer() -> None:
    """After cold-start, the tailer is constructed and running."""
    ss, _ = _make_session()
    await ss.connect()
    assert ss._tailer is not None
    assert ss._tailer.stats["running"] is True
    await ss.disconnect()


@pytest.mark.asyncio
async def test_disconnect_stops_tailer() -> None:
    """disconnect() cancels the tailer's background task."""
    ss, _ = _make_session()
    await ss.connect()
    tailer = ss._tailer
    assert tailer is not None
    await ss.disconnect()
    # Tailer instance preserved (so stats survive); but task is stopped.
    assert ss._tailer is tailer
    assert ss._tailer.stats["running"] is False


@pytest.mark.asyncio
async def test_deliver_turn_captures_inflight_meta() -> None:
    """_deliver_turn stashes routing metadata for the tailer's callback
    to forward through response_callback."""
    ss, _ = _make_session(state=SessionState.CONNECTED)
    turn = _QueuedTurn(
        prompt="hello",
        platform="telegram",
        chat_id="12345",
        message_id="m1",
    )
    await ss._deliver_turn(turn)
    assert ss._inflight_meta == {
        "platform": "telegram",
        "chat_id": "12345",
        "message_id": "m1",
    }


@pytest.mark.asyncio
async def test_deliver_turn_clears_meta_on_paste_text_failure() -> None:
    """If tmux paste_text fails, in-flight meta is cleared so a stale
    tail doesn't fire response_callback with bogus routing data."""
    tmux = _make_mock_tmux()
    tmux.paste_text = AsyncMock(return_value=_fail("rc=1"))
    ss, _ = _make_session(state=SessionState.CONNECTED, tmux=tmux)
    turn = _QueuedTurn(prompt="hi", platform="t", chat_id="c", message_id="m")
    with pytest.raises(RuntimeError, match="tmux paste-buffer"):
        await ss._deliver_turn(turn)
    assert ss._inflight_meta == {}


@pytest.mark.asyncio
async def test_handle_turn_complete_fires_response_callback() -> None:
    """End-to-end: synthetic TurnResponse → response_callback called with
    correct unified routing payload."""
    cb = _AsyncCollector()
    ss, _ = _make_session_with_response_cb(response_cb=cb)
    ss._state_machine._state = SessionState.CONNECTED
    ss._inflight_meta = {
        "platform": "telegram",
        "chat_id": "12345",
        "message_id": "m1",
    }
    response = TurnResponse(
        text="hello back",
        stop_reason="end_turn",
        usage={"input_tokens": 10, "output_tokens": 5},
    )
    await ss._handle_turn_complete(response)

    assert len(cb.calls) == 1
    result = cb.calls[0]
    assert result.agent_name == "dymok"
    assert result.session_id == ss.id
    assert result.response_text == "hello back"
    assert result.platform == "telegram"
    assert result.chat_id == "12345"
    assert result.message_id == "m1"
    assert result.usage == {"input_tokens": 10, "output_tokens": 5}
    # Meta cleared after firing — next turn starts clean.
    assert ss._inflight_meta == {}


@pytest.mark.asyncio
async def test_handle_turn_complete_skips_callback_for_empty_text() -> None:
    """Empty response with no tool activity doesn't fire the response_callback."""
    cb = _AsyncCollector()
    ss, _ = _make_session_with_response_cb(response_cb=cb)
    response = TurnResponse(text="", stop_reason="tool_use")
    await ss._handle_turn_complete(response)
    assert cb.calls == []


@pytest.mark.asyncio
async def test_handle_turn_complete_fires_callback_for_tool_only_turn() -> None:
    """Tool-only turns still notify the broker so it can stop typing and
    suppress plain-text fallback when an outreach tool handled delivery.
    """
    cb = _AsyncCollector()
    ss, _ = _make_session_with_response_cb(response_cb=cb)
    ss._inflight_meta = {
        "platform": "telegram",
        "chat_id": "12345",
        "message_id": "m1",
    }
    response = TurnResponse(
        text="",
        stop_reason="tool_use",
        tool_uses=[
            {
                "name": "mcp__pinky-messaging__send",
                "input": {"chat_id": "12345", "text": "sent via tool"},
                "id": "toolu_1",
            }
        ],
    )
    await ss._handle_turn_complete(response)

    assert len(cb.calls) == 1
    result = cb.calls[0]
    assert result.response_text == ""
    assert result.chat_id == "12345"
    assert result.used_outreach_tools is True


@pytest.mark.asyncio
async def test_handle_turn_complete_writes_to_conversation_store() -> None:
    """assistant response is appended to the conversation store."""
    conv = MagicMock()
    ss, _ = _make_session_with_response_cb(conv_store=conv)
    # #560: handler now requires an in-flight meta entry; seed one with
    # empty routing (this test only exercises the conversation_store side
    # effect, not the broker callback).
    _seed_inflight(ss)
    response = TurnResponse(text="response text", stop_reason="end_turn")
    await ss._handle_turn_complete(response)
    conv.append.assert_called_once_with(ss.id, "assistant", "response text")


@pytest.mark.asyncio
async def test_handle_turn_complete_stores_thinking_metadata() -> None:
    """Tmux thinking blocks persist in conversation metadata like SDK turns."""
    conv = MagicMock()
    ss, _ = _make_session_with_response_cb(conv_store=conv)
    _seed_inflight(ss)  # #560
    response = TurnResponse(
        text="response text",
        thinking="checked the transcript and tool state",
        stop_reason="end_turn",
    )

    await ss._handle_turn_complete(response)

    conv.append.assert_called_once_with(
        ss.id,
        "assistant",
        "response text",
        metadata={"thinking": ["checked the transcript and tool state"]},
    )
    assert ss.stats["current_thinking"] == ""


@pytest.mark.asyncio
async def test_handle_turn_complete_fires_stream_event() -> None:
    """stream_event_callback gets a turn_completed event with usage + duration."""
    events: list[dict] = []

    async def stream_cb(evt):
        events.append(evt)

    ss, _ = _make_session_with_response_cb(stream_evt=stream_cb)
    _seed_inflight(ss)  # #560
    response = TurnResponse(
        text="x", stop_reason="end_turn",
        usage={"input_tokens": 100, "output_tokens": 50},
        duration_ms=1500,
        assistant_entry_count=2,
        thinking="reasoned about the pane state",
        tool_uses=[{"name": "Bash", "input": {}, "id": "t1"}],
    )
    await ss._handle_turn_complete(response)
    # Two events fire per turn since task #95: a ``context_usage``
    # event with the cumulative token snapshot, then the existing
    # ``turn_completed`` event. The watchdog snapshot precedes the
    # turn-end so the chat UI can update its session-info card
    # *before* the thinking bubble clears.
    types = [e["type"] for e in events]
    assert types == ["context_usage", "turn_completed"]

    turn_evt = events[1]
    assert turn_evt["agent_name"] == "dymok"
    # Renamed from "turn_complete" to "turn_completed" for parity with
    # StreamingSession + CodexSession — Chat.svelte listens for the
    # -d suffix so the thinking-bubble clears at turn end.
    assert turn_evt["type"] == "turn_completed"
    assert turn_evt["stop_reason"] == "end_turn"
    assert turn_evt["duration_ms"] == 1500
    assert turn_evt["assistant_entry_count"] == 2
    assert turn_evt["tool_use_count"] == 1
    assert turn_evt["thinking_chars"] == len("reasoned about the pane state")
    assert turn_evt["thinking_block_count"] == 1


@pytest.mark.asyncio
async def test_handle_turn_complete_resets_live_activity_state() -> None:
    """Activity log + current activity must clear at turn end.

    Without this reset, the polling endpoint ``/streaming/status``
    keeps returning the previous turn's accumulated tool-call lines
    and Chat.svelte's thinking-bubble shows stale activity blending
    across turns (Brad's msg #7950 with screenshot, 2026-05-17)."""
    ss, _ = _make_session_with_response_cb()
    # Simulate mid-turn state: chips pushed by PreToolUse hook before
    # the model finished responding.
    ss._current_activity = "Bash — echo hi"
    ss._current_thinking = "working through the command output"
    ss._activity_log = ["ToolSearch", "Bash — echo test", "Bash — echo hi"]
    _seed_inflight(ss)  # #560
    response = TurnResponse(text="done", stop_reason="end_turn")

    await ss._handle_turn_complete(response)

    assert ss._current_activity == ""
    assert ss._current_thinking == ""
    assert ss._activity_log == []


@pytest.mark.asyncio
async def test_handle_turn_complete_swallows_callback_exceptions() -> None:
    """A misbehaving response_callback must not strand the session.

    Critical because the tailer awaits this method; an unhandled raise
    would leak out and (in production) blow up the tail loop's exception
    handler, dropping subsequent turns."""
    async def bad_cb(*args, **kwargs):
        raise RuntimeError("downstream broke")

    bad_conv = MagicMock()
    bad_conv.append = MagicMock(side_effect=RuntimeError("store broke"))

    async def bad_stream(*args, **kwargs):
        raise RuntimeError("stream broke")

    ss, _ = _make_session_with_response_cb(
        response_cb=bad_cb,
        conv_store=bad_conv,
        stream_evt=bad_stream,
    )
    response = TurnResponse(text="x", stop_reason="end_turn")
    # All three callbacks raise; method must not.
    await ss._handle_turn_complete(response)
    # Meta still cleared (PR8b contract: clear at end regardless).
    assert ss._inflight_meta == {}


@pytest.mark.asyncio
async def test_notify_tail_wakes_tailer() -> None:
    """notify_tail() forwards to the tailer's wake() method."""
    ss, _ = _make_session()
    await ss.connect()
    assert ss._tailer is not None
    # Clear any latched wake from start().
    ss._tailer._wake_event.clear()
    ss.notify_tail()
    assert ss._tailer._wake_event.is_set()
    await ss.disconnect()


@pytest.mark.asyncio
async def test_notify_tail_safe_before_connect() -> None:
    """notify_tail() before tailer is constructed is a silent no-op."""
    ss, _ = _make_session()
    assert ss._tailer is None
    ss.notify_tail()  # must not raise


@pytest.mark.asyncio
async def test_set_transcript_path_forwards_to_tailer(tmp_path) -> None:
    """SessionStart hook reports a new path → tailer is repointed."""
    ss, _ = _make_session()
    await ss.connect()
    new_path = tmp_path / "new-session.jsonl"
    new_path.touch()
    ss.set_transcript_path(new_path)
    assert ss._tailer.transcript_path == new_path
    # Offset reset on rotation (per tailer contract).
    assert ss._tailer.offset == 0
    await ss.disconnect()


@pytest.mark.asyncio
async def test_set_transcript_path_safe_before_connect(tmp_path) -> None:
    """set_transcript_path before tailer exists is a silent no-op."""
    ss, _ = _make_session()
    # Don't connect — tailer is None.
    ss.set_transcript_path(tmp_path / "x.jsonl")  # must not raise
    assert ss._tailer is None


@pytest.mark.asyncio
async def test_end_to_end_tailer_to_response_callback(tmp_path) -> None:
    """Full integration: synthetic transcript file → tailer reads → turn
    complete → response_callback fires with full routing metadata.

    This exercises the real tailer (no mocks) but feeds it a synthetic
    transcript instead of a live claude REPL. Pins the entire PR8b
    contract end-to-end."""
    cb = _AsyncCollector()
    ss, _ = _make_session_with_response_cb(response_cb=cb)
    await ss.connect()

    # Replace the tailer's path with our synthetic transcript.
    transcript = tmp_path / "synthetic.jsonl"
    transcript.write_text("")
    ss.set_transcript_path(transcript)

    # Simulate _deliver_turn capturing routing meta.
    ss._inflight_meta = {
        "platform": "telegram",
        "chat_id": "777",
        "message_id": "m42",
    }

    # Write a synthetic turn to the transcript file.
    entries = [
        {
            "type": "user",
            "timestamp": "2026-05-14T05:00:00.000Z",
            "message": {"role": "user", "content": "hi"},
        },
        {
            "type": "assistant",
            "timestamp": "2026-05-14T05:00:00.100Z",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "hello there"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 5, "output_tokens": 3},
            },
        },
        {
            "type": "system",
            "subtype": "stop_hook_summary",
            "timestamp": "2026-05-14T05:00:00.500Z",
        },
    ]
    transcript.write_text("\n".join(_json.dumps(e) for e in entries) + "\n")

    # Drive the tailer to read.
    await ss._tailer.read_once()

    # response_callback fired with the right unified payload.
    assert len(cb.calls) == 1
    result = cb.calls[0]
    assert result.agent_name == "dymok"
    assert result.response_text == "hello there"
    assert result.platform == "telegram"
    assert result.chat_id == "777"
    assert result.message_id == "m42"

    await ss.disconnect()


@pytest.mark.asyncio
async def test_discover_transcript_path_returns_none_for_empty_project_dir(tmp_path, monkeypatch) -> None:
    """When the encoded-cwd project dir doesn't exist, return None.

    This is the cold-start case — agent has never been run, so Claude
    Code hasn't created the project dir yet. Tailer starts with a
    placeholder path and SessionStart hook later reports the canonical one."""
    # Point HOME at a tmp dir so the glob has nowhere to find anything.
    monkeypatch.setenv("HOME", str(tmp_path))
    ss, _ = _make_session()
    # working_dir is /tmp/tmux-session-test from the fixture; project dir
    # path under our fake HOME does not exist.
    assert ss._discover_transcript_path() is None


# ──────────────────────────────────────────────────────────────────────────
# PR8b round 2 — Pushok's review fixes
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_turn_done_set_unconditionally_for_empty_text_turn() -> None:
    """Pushok's PR #496 round-1 Case 1 follow-up: a turn that produces
    zero assistant text (pure tool-use that hit max_tokens, or refusal)
    must still set ``_turn_done`` so the worker can dispatch the next
    prompt. If turn_done were gated on ``response.text``, the worker
    would deadlock forever on tool-use-only turns.
    """
    cb = _AsyncCollector()
    ss, _ = _make_session_with_response_cb(response_cb=cb)
    # Pre-clear turn_done to mimic what _deliver_turn does.
    ss._turn_done.clear()
    assert not ss._turn_done.is_set()
    # #560: handler now requires an in-flight meta entry. The contract
    # this test pins (empty-text turn still sets turn_done) is preserved
    # because the critical-section block at the top of the handler sets
    # turn_done before any text/tool gating.
    _seed_inflight(ss)

    # Empty-text turn — pure tool-use refusal, max_tokens, etc.
    empty_response = TurnResponse(text="", stop_reason="max_tokens")
    await ss._handle_turn_complete(empty_response)

    # response_callback NOT fired (empty text), but turn_done IS set.
    assert cb.calls == []
    assert ss._turn_done.is_set(), (
        "turn_done must be set unconditionally so the worker can proceed"
    )


@pytest.mark.asyncio
async def test_deliver_turn_clears_turn_done_before_paste_text() -> None:
    """The clear must happen BEFORE paste_text so that any subsequent
    stop_hook_summary unambiguously belongs to THIS turn (not a stale
    pre-arm from a prior callback). Pinned via call-order observation.
    """
    tmux = _make_mock_tmux()
    cleared_at: list[bool] = []

    async def observing_paste(*args, **kwargs):
        # Snapshot turn_done state at the moment paste_text is called.
        cleared_at.append(not ss._turn_done.is_set())
        return _ok()

    tmux.paste_text = AsyncMock(side_effect=observing_paste)
    ss, _ = _make_session(state=SessionState.CONNECTED, tmux=tmux)
    # Pre-arm turn_done to a SET state to prove the clear() actually fires.
    ss._turn_done.set()

    turn = _QueuedTurn(prompt="hi", platform="t", chat_id="c", message_id="m")
    await ss._deliver_turn(turn)

    assert cleared_at == [True], (
        "turn_done must be CLEARED at the moment paste_text is invoked"
    )


@pytest.mark.asyncio
async def test_deliver_turn_paste_text_failure_re_arms_turn_done() -> None:
    """If paste_text fails, the worker would otherwise block forever on
    turn_done.wait() because no callback will ever fire for the failed
    dispatch. _deliver_turn must re-arm turn_done as part of its failure
    cleanup so the worker's next iteration starts in a clean state.
    """
    tmux = _make_mock_tmux()
    tmux.paste_text = AsyncMock(return_value=_fail("rc=1"))
    ss, _ = _make_session(state=SessionState.CONNECTED, tmux=tmux)
    ss._turn_done.clear()

    turn = _QueuedTurn(prompt="hi", platform="t", chat_id="c", message_id="m")
    with pytest.raises(RuntimeError):
        await ss._deliver_turn(turn)

    # Meta cleared AND turn_done re-armed.
    assert ss._inflight_meta == {}
    assert ss._turn_done.is_set()


@pytest.mark.asyncio
async def test_deliver_turn_dead_pane_schedules_disconnect_and_worker_exits() -> None:
    """Task #90: when paste_text fails because the tmux pane is gone
    (external kill, tmux server crash), _deliver_turn must schedule a
    disconnect so the session transitions CONNECTED → DEAD. The worker
    must exit cleanly on the resulting RuntimeError (rather than
    looping forever pasting into the missing pane). After this, a
    follow-up send() must be dropped per the not-CONNECTED contract
    — the next inbound message will cold-start a fresh pane via the
    auto-wake path validated in #517/#518/#519.
    """
    tmux = _make_mock_tmux()
    # Mimic tmux's exact stderr shape when the target session/pane is gone.
    tmux.paste_text = AsyncMock(
        return_value=_fail("can't find pane: pinky-dymok")
    )
    ss, _ = _make_session(tmux=tmux)
    await ss.connect()
    assert ss.state == SessionState.CONNECTED
    worker_task = ss._worker_task
    assert worker_task is not None

    # Queue one turn — worker will pick it up, paste_text fails with
    # dead-pane stderr, disconnect gets scheduled, worker exits.
    await ss.send("hi", platform="telegram", chat_id="123", message_id="m1")

    # Wait for state to transition to DEAD (disconnect runs as a
    # background task scheduled via create_task).
    for _ in range(100):
        await asyncio.sleep(0.01)
        if ss.state == SessionState.DEAD and worker_task.done():
            break

    assert ss.state == SessionState.DEAD, (
        f"expected DEAD after dead-pane detect, got {ss.state.value}"
    )
    assert worker_task.done(), "worker must exit cleanly on dead-pane"

    # Follow-up send is dropped because state != CONNECTED (matches the
    # existing "drop with log line" behavior at the top of send()).
    paste_count_before = tmux.paste_text.await_count
    await ss.send("again", platform="telegram", chat_id="123", message_id="m2")
    assert tmux.paste_text.await_count == paste_count_before, (
        "send() must drop while not CONNECTED — no additional paste_text "
        "calls into the dead pane"
    )


# ──────────────────────────────────────────────────────────────────────────
# Paste-time safety: context-lock check (#522 + #525)
# ──────────────────────────────────────────────────────────────────────────
# Issue #525 removed the pane-scraping idle-prompt readiness gate (#522 +
# #524). It waited for a pane signal (bare ``❯``) that Claude Code's splash
# never produces and killed every cold start. Splash-state paste is
# handled by ``_TmuxControl.paste_text`` (bracketed-paste + delayed Enter,
# commit 0864f4e / issue #514): the splash dismisses on input focus.
# Context-lock check is the only remaining paste-time gate.


@pytest.mark.asyncio
async def test_deliver_turn_pastes_immediately_on_cold_repl() -> None:
    """Issue #525: cold-start paste must NOT block on any pane-state
    readiness check. The original gate (#522) waited for a bare ``❯``
    that Claude Code's splash never produces, deadlocking cold starts.
    First turn after spawn pastes directly — splash-state paste is
    handled by ``paste_text`` (splash dismisses on input focus).
    """
    tmux = _make_mock_tmux()
    ss, _ = _make_session(state=SessionState.CONNECTED, tmux=tmux)
    assert ss._has_completed_turn is False

    turn = _QueuedTurn(prompt="hi", platform="t", chat_id="c", message_id="m1")
    await ss._deliver_turn(turn)

    # Paste must fire immediately, no readiness polling involved.
    tmux.paste_text.assert_awaited_once()
    assert not hasattr(tmux, "wait_for_idle_prompt") or \
        not tmux.wait_for_idle_prompt.await_count


@pytest.mark.asyncio
async def test_deliver_turn_skips_paste_when_context_locked(
    monkeypatch, tmp_path
) -> None:
    """Pulse-v2 port: if the daemon-level context manager has touched
    the agent's transport-lock file, ``_deliver_turn`` must raise a
    typed ``_ContextLockDeferral`` BEFORE paste_text so the worker
    preserves the inflight turn and re-pastes the SAME prompt on the
    next iteration once the lock is released (worker-level retry
    behavior pinned separately in
    ``test_context_lock_preserves_turn_until_released``).
    """
    # Point the lock dir at a tmp path so the test can't escape the
    # sandbox or collide with a real lock.
    monkeypatch.setattr(tmux_session, "_TRANSPORT_LOCK_DIR", tmp_path)
    tmux = _make_mock_tmux()
    ss, _ = _make_session(state=SessionState.CONNECTED, tmux=tmux)

    # Touch the lock for this agent.
    lock_path = tmp_path / f"{ss.agent_name}.lock"
    lock_path.write_text("")

    turn = _QueuedTurn(prompt="hi", platform="t", chat_id="c", message_id="m1")
    # Murzik #522 round-1: typed transient exception (was bare
    # RuntimeError) — the worker recognises it as "preserve inflight,
    # sleep + retry the same turn".
    with pytest.raises(
        tmux_session._ContextLockDeferral, match="context lock present"
    ):
        await ss._deliver_turn(turn)

    # Paste must not have been called — lock check is the first thing
    # _deliver_turn does.
    tmux.paste_text.assert_not_awaited()

    # Once the lock is released, the next dispatch proceeds normally.
    lock_path.unlink()
    await ss._deliver_turn(turn)
    tmux.paste_text.assert_awaited_once()


@pytest.mark.asyncio
async def test_spawn_tmux_repl_resets_completed_turn_flag() -> None:
    """The idle-prompt gate's discriminator is ``_has_completed_turn``.
    Every fresh spawn must reset it to False so the gate fires for the
    first paste against the new REPL — even after a prior REPL on this
    session object had completed turns.
    """
    ss, _ = _make_session()
    # Simulate a prior REPL having completed turns.
    ss._has_completed_turn = True
    await ss._spawn_tmux_repl()
    assert ss._has_completed_turn is False
    await ss.disconnect()


@pytest.mark.asyncio
async def test_disconnect_clears_inflight_meta() -> None:
    """Pushok's PR #496 round-1 Case 2: a stale ``_inflight_meta`` from
    a turn that was in-flight at disconnect time must not survive the
    disconnect — otherwise a straggler stop_hook_summary read after
    reconnect could route a response to a stale chat."""
    ss, _ = _make_session()
    await ss.connect()
    # Simulate an in-flight turn.
    ss._inflight_meta = {"platform": "telegram", "chat_id": "999", "message_id": "m"}
    await ss.disconnect()
    assert ss._inflight_meta == {}


@pytest.mark.asyncio
async def test_multi_prompt_routing_no_cross_user_leak(tmp_path) -> None:
    """#560 / Pushok PR #496 round-1 Case 1 — preserved with concurrent
    dispatch: two ``send()`` calls in quick succession must route response
    A to chat A and response B to chat B, regardless of how quickly the
    worker dispatched them.

    Pre-#560 contract: worker awaited ``_turn_done`` between dispatches,
    so turn B sat in ``_message_queue`` until turn A finished. The
    in-flight meta cell was always exactly turn A's.

    Post-#560 contract: worker dispatches both back-to-back (paste-with-
    Enter), Claude Code's native queued-prompt feature absorbs turn B
    while turn A runs, ``_inflight_metas`` holds BOTH metas in FIFO
    order. The router still routes A→A, B→B because the deque pops
    oldest-first and each entry carries its own routing dict (no
    shared mutable cell to clobber).
    """
    cb = _AsyncCollector()
    ss, _ = _make_session_with_response_cb(response_cb=cb)
    await ss.connect()

    # Repoint tailer at our synthetic transcript.
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("")
    ss.set_transcript_path(transcript)
    # Use tight cadences so the test runs fast.
    ss._tailer._fallback_poll_sec = 0.02
    ss._tailer._active_poll_sec = 0.01

    # Queue two prompts back-to-back.
    await ss.send(prompt="from A", platform="telegram", chat_id="A", message_id="mA")
    await ss.send(prompt="from B", platform="telegram", chat_id="B", message_id="mB")

    # Under #560 the worker dispatches both turns; both metas land in
    # ``_inflight_metas`` in order. Spin until both are appended.
    for _ in range(50):
        await asyncio.sleep(0.01)
        if len(ss._inflight_metas) >= 2:
            break
    assert len(ss._inflight_metas) == 2, (
        "worker should dispatch BOTH turns back-to-back under concurrent "
        f"dispatch (#560); got {len(ss._inflight_metas)} in-flight metas"
    )
    # FIFO check: oldest entry (deque head) is A, next is B.
    assert ss._inflight_metas[0].meta == {
        "platform": "telegram", "chat_id": "A", "message_id": "mA",
    }, "deque head must be turn A's meta"
    assert ss._inflight_metas[1].meta == {
        "platform": "telegram", "chat_id": "B", "message_id": "mB",
    }, "deque second must be turn B's meta"

    # Write turn A's response + stop_hook_summary to the transcript.
    # (``_json`` is imported at file scope; no local re-import needed.)
    turn_a_entries = [
        {"type": "assistant", "timestamp": "2026-05-14T05:00:00.000Z",
         "message": {"role": "assistant",
                     "content": [{"type": "text", "text": "response A"}],
                     "stop_reason": "end_turn",
                     "usage": {}}},
        {"type": "system", "subtype": "stop_hook_summary",
         "timestamp": "2026-05-14T05:00:00.500Z"},
    ]
    transcript.write_text(
        "\n".join(_json.dumps(e) for e in turn_a_entries) + "\n"
    )
    ss._tailer.wake()

    # Wait for response A to fire its callback. After popleft, the deque
    # head should advance to B.
    for _ in range(50):
        await asyncio.sleep(0.02)
        if cb.calls and len(ss._inflight_metas) == 1:
            break

    # Critical: response A was routed to chat A, NOT chat B (the original
    # Case 1 leak). With the deque each entry carries its own routing
    # dict — there is no shared mutable cell to clobber.
    assert len(cb.calls) == 1
    result = cb.calls[0]
    assert result.response_text == "response A"
    assert result.chat_id == "A", (
        f"response A leaked to wrong chat: {result} — Case 1 regression"
    )
    # Deque head is now B.
    assert ss._inflight_metas[0].meta["chat_id"] == "B"

    # Append turn B's response + stop_hook_summary.
    turn_b_entries = [
        {"type": "assistant", "timestamp": "2026-05-14T05:00:01.000Z",
         "message": {"role": "assistant",
                     "content": [{"type": "text", "text": "response B"}],
                     "stop_reason": "end_turn",
                     "usage": {}}},
        {"type": "system", "subtype": "stop_hook_summary",
         "timestamp": "2026-05-14T05:00:01.500Z"},
    ]
    with transcript.open("a", encoding="utf-8") as fh:
        fh.write("\n".join(_json.dumps(e) for e in turn_b_entries) + "\n")
    ss._tailer.wake()

    for _ in range(50):
        await asyncio.sleep(0.02)
        if len(cb.calls) == 2:
            break

    # Critical: response B was routed to chat B.
    assert len(cb.calls) == 2
    result = cb.calls[1]
    assert result.response_text == "response B"
    assert result.chat_id == "B"
    # Deque is now empty.
    assert len(ss._inflight_metas) == 0

    await ss.disconnect()


@pytest.mark.asyncio
async def test_worker_force_restarts_on_turn_done_timeout(monkeypatch) -> None:
    """#560 retargeting note: pre-#560 this contract was enforced by
    the worker's per-iter ``_turn_done`` timeout. Under concurrent
    dispatch the worker no longer waits between turns, so the "stop
    hook never fires → force_restart" failure mode moves to the
    ``_inflight_watchdog`` background task — which ages the deque
    HEAD (not paste age) so each queued turn gets its own fair
    window once it becomes the head (Murzik review point #1).

    Test name preserved for `git blame` continuity; behavior pinned
    on the new mechanism. Original Case 1 protection (no cross-routing
    leak) is now structural via the per-entry routing dicts in
    ``_inflight_metas``.
    """
    from pinky_daemon import tmux_session
    # Shorten both timers so the test doesn't take 10 minutes. Watchdog
    # ticks at _WATCHDOG_TICK_SEC; head age must exceed _TURN_DONE_TIMEOUT_SEC
    # before force_restart fires.
    monkeypatch.setattr(tmux_session, "_TURN_DONE_TIMEOUT_SEC", 0.05)
    monkeypatch.setattr(tmux_session, "_WATCHDOG_TICK_SEC", 0.02)

    guard = MagicMock(return_value={"restart_safe": False, "reason": "no save"})
    ss, _ = _make_session(restart_guard=guard)
    await ss.connect()

    # Track force_restart calls — replace with a stub that signals.
    force_restart_called = asyncio.Event()
    force_restart_done = asyncio.Event()
    force_restart_results: list[bool] = []
    original_force_restart = ss.force_restart

    async def stub_force_restart():
        force_restart_called.set()
        # Call original to drive state machine through reconnect.
        try:
            result = await original_force_restart()
            force_restart_results.append(result)
            return result
        finally:
            force_restart_done.set()

    ss.force_restart = stub_force_restart

    # Send one prompt — worker dispatches; deque grows by one; no stop
    # hook ever fires, so the watchdog detects the stuck head and
    # schedules force_restart.
    await ss.send(prompt="stuck", platform="t", chat_id="c", message_id="m")

    # Wait for the watchdog to detect + fire force_restart.
    try:
        await asyncio.wait_for(force_restart_called.wait(), timeout=2.0)
    except asyncio.TimeoutError:
        pytest.fail(
            "force_restart should have been called after inflight watchdog "
            "head-age exceeded _TURN_DONE_TIMEOUT_SEC"
        )

    try:
        await asyncio.wait_for(force_restart_done.wait(), timeout=2.0)
    except asyncio.TimeoutError:
        pytest.fail("pre-first-turn force_restart should not be blocked by guard")

    # Turn-timeout counter incremented (now in the watchdog).
    assert ss._stats.get("turn_timeouts", 0) == 1
    assert ss._has_completed_turn is False
    assert force_restart_results == [True]
    guard.assert_not_called()
    # Deque was drained by the watchdog's force_restart path.
    assert len(ss._inflight_metas) == 0

    await ss.disconnect()


@pytest.mark.asyncio
async def test_spawn_clears_turn_done_after_reconnect() -> None:
    """The turn_done invariant ("cleared between dispatches") must be
    re-established after force_restart so the first dispatch on the
    new tmux pane doesn't see a stale set() from the killed session's
    last callback.
    """
    ss, _ = _make_session()
    # Pre-set turn_done to simulate the state at the moment a
    # force_restart happens (last turn completed → callback set it).
    ss._turn_done.set()
    assert ss._turn_done.is_set()

    await ss.connect()
    # After connect (which calls _spawn_tmux_repl), the invariant is
    # restored: turn_done is cleared.
    assert not ss._turn_done.is_set(), (
        "turn_done invariant violated post-spawn — should be cleared"
    )
    await ss.disconnect()


@pytest.mark.asyncio
async def test_force_restart_resumes_tailer(tmp_path) -> None:
    """Pushok's PR #496 round-2 Case 1': ``force_restart`` must leave
    the tailer running so the new session can complete a turn.

    Bug shape pre-fix: ``_start_tailer`` was only called from
    ``connect``. ``force_restart`` invoked ``_spawn_tmux_repl`` directly
    (bypassing ``connect``), so after a restart the tailer instance
    survived but its background task was dead. Result:

    1. New worker dispatches turn → ``mark_active`` wakes a dead task.
    2. No ``stop_hook_summary`` ever fires → ``turn_done`` never set.
    3. Worker times out after 600s → another ``force_restart``.
    4. Death loop. Agent silently never delivers responses.

    The round-2 turn_done event gate (Case 1) made this loud — without
    it, the failure was silently-dropped responses on a "live" agent.

    Pre-fix this test fails with:
      ``assert ss._tailer.stats["running"] is True`` → ``False``
    and the end-to-end response_callback never fires.
    """
    cb = _AsyncCollector()
    ss, _ = _make_session_with_response_cb(response_cb=cb)
    await ss.connect()
    assert ss._tailer.stats["running"] is True, (
        "tailer should be running after cold-start connect"
    )

    # Trigger a force_restart. This drives:
    #   CONNECTED → RECONNECTING (via disconnect+_stop_tailer)
    #            → CONNECTED (via _spawn_tmux_repl)
    # The fix moves _start_tailer into _spawn_tmux_repl so the post-
    # restart session has a live tailer task.
    restart_ok = await ss.force_restart()
    assert restart_ok, "force_restart should succeed"

    # Critical invariant — pre-fix this is False (task killed by
    # disconnect's _stop_tailer, never restarted).
    assert ss._tailer is not None
    assert ss._tailer.stats["running"] is True, (
        "tailer task must be running after force_restart — Pushok Case 1' "
        "regression: if this fires, force_restart skipped _start_tailer"
    )

    # End-to-end pin: drive a complete turn through the post-restart
    # tailer and assert response_callback fires. This is the real test
    # — even if the tailer instance is "running" by accident, can it
    # actually deliver a response?
    transcript = tmp_path / "post_restart.jsonl"
    transcript.write_text("")
    ss.set_transcript_path(transcript)

    ss._inflight_meta = {
        "platform": "telegram",
        "chat_id": "999",
        "message_id": "m_post_restart",
    }

    entries = [
        {
            "type": "assistant",
            "timestamp": "2026-05-14T06:00:00.100Z",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "alive after restart"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        },
        {
            "type": "system",
            "subtype": "stop_hook_summary",
            "timestamp": "2026-05-14T06:00:00.500Z",
        },
    ]
    transcript.write_text("\n".join(_json.dumps(e) for e in entries) + "\n")

    await ss._tailer.read_once()

    # Pre-fix: this assertion fails because the tailer task is dead and
    # ``read_once`` is the only thing keeping it limping — but the
    # background loop that would actually drive turn completion in
    # production never runs. We explicitly call ``read_once`` here so
    # the assertion is robust against scheduling jitter; the real
    # production-shape assertion is the ``stats["running"]`` one above.
    assert len(cb.calls) == 1, (
        "post-restart turn should have completed end-to-end"
    )
    result = cb.calls[0]
    assert result.agent_name == "dymok"
    assert result.response_text == "alive after restart"
    assert result.chat_id == "999"

    await ss.disconnect()


@pytest.mark.asyncio
async def test_stop_tailer_drains_buffer_for_same_path_resume(tmp_path) -> None:
    """Murzik's PR #496 round-3 Case 2'' regression: ``_stop_tailer``
    must drain the in-progress turn buffer so a same-path lifecycle
    restart (``claude --continue`` resume) doesn't leak dead-session
    partial text into the next session's first turn.

    The round-2 fix in ``set_transcript_path`` only drained on path
    *change*. If ``force_restart`` is followed by Claude Code resuming
    the same JSONL path, ``set_transcript_path`` either isn't called or
    skips the drain due to path equality. The killed session's partial
    assistant text would then survive into the next session, and the
    first complete turn's callback would fire with
    ``old_partial + new_text``.

    Pre-fix this test fails with:
      ``assert ss._tailer._buffer.is_empty`` → ``False`` after
      ``_stop_tailer``, because round-2's drain was scoped to path
      swaps and round-3's ``_start_tailer`` fix did not address the
      buffer-retention angle.
    """
    cb = _AsyncCollector()
    ss, _ = _make_session_with_response_cb(response_cb=cb)
    await ss.connect()

    # Replace the tailer's path with a controlled synthetic transcript.
    transcript = tmp_path / "session_x.jsonl"
    transcript.write_text("")
    ss.set_transcript_path(transcript)

    # Feed a partial turn — assistant entry without stop_hook_summary.
    # This simulates the in-flight state when a session is killed mid-turn.
    partial_entries = [
        {
            "type": "assistant",
            "timestamp": "2026-05-14T06:00:00.100Z",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "partial from X"}],
                "stop_reason": "",
                "usage": {},
            },
        },
    ]
    transcript.write_text("\n".join(_json.dumps(e) for e in partial_entries) + "\n")
    await ss._tailer.read_once()

    # Buffer should hold "partial from X"; no callback yet (no
    # stop_hook_summary).
    assert len(cb.calls) == 0
    assert not ss._tailer._buffer.is_empty, (
        "buffer should have accumulated partial assistant text"
    )

    # Stop the tailer — the fix: this should also drain the buffer.
    await ss._stop_tailer()
    assert ss._tailer._buffer.is_empty, (
        "_stop_tailer must drain the buffer (Murzik Case 2'') — "
        "without this, a same-path resume leaks dead-session text "
        "into the next session's first reply"
    )

    # Restart the tailer with the SAME path — simulates claude --continue
    # resuming. The round-2 set_transcript_path drain wouldn't fire here
    # because the path didn't change; we rely on _stop_tailer's drain.
    await ss._start_tailer()

    # Session Y produces a complete turn, appended to the same transcript.
    with open(transcript, "a") as fh:
        new_entries = [
            {
                "type": "assistant",
                "timestamp": "2026-05-14T06:05:00.100Z",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "response from Y"}],
                    "stop_reason": "end_turn",
                    "usage": {"input_tokens": 5, "output_tokens": 3},
                },
            },
            {
                "type": "system",
                "subtype": "stop_hook_summary",
                "timestamp": "2026-05-14T06:05:00.500Z",
            },
        ]
        fh.write("\n".join(_json.dumps(e) for e in new_entries) + "\n")

    ss._inflight_meta = {"platform": "telegram", "chat_id": "Y", "message_id": "mY"}
    await ss._tailer.read_once()

    # Callback fires with ONLY Y's text — no "partial from X" prefix.
    assert len(cb.calls) == 1, "Y's turn should have fired exactly one callback"
    result = cb.calls[0]
    assert result.response_text == "response from Y", (
        f"expected clean Y response, got {result.response_text!r} — "
        f"if this contains 'partial from X', the same-path-resume "
        f"buffer-leak regression has reopened"
    )

    await ss.disconnect()


@pytest.mark.asyncio
async def test_spawn_tmux_repl_rollback_clears_partial_tailer() -> None:
    """Murzik's PR #496 round-3 cleanup-hole regression: if
    ``_start_tailer`` raises AFTER constructing ``self._tailer``, the
    ``_spawn_tmux_repl`` rollback path must stop+null the partial
    tailer instance before re-raising. Otherwise the caller transitions
    DEAD with a live orphan tailer instance.

    Pre-fix this test fails with:
      ``assert ss._tailer is None`` → ``False``, because the round-3
      rollback only killed tmux and left ``self._tailer`` populated.
    """
    ss, tmux = _make_session()

    # Monkeypatch TmuxTranscriptTailer.start so it raises AFTER the
    # tailer instance has been constructed by _start_tailer's
    # ``self._tailer = TmuxTranscriptTailer(...)`` assignment.
    from pinky_daemon import tmux_transcript

    async def boom(self):
        raise RuntimeError("synthetic tailer start failure")

    original_start = tmux_transcript.TmuxTranscriptTailer.start
    tmux_transcript.TmuxTranscriptTailer.start = boom
    try:
        with pytest.raises(RuntimeError, match="synthetic tailer start failure"):
            await ss._spawn_tmux_repl()
    finally:
        tmux_transcript.TmuxTranscriptTailer.start = original_start

    # Rollback assertions: tailer slot is cleared and tmux was killed.
    assert ss._tailer is None, (
        "rollback in _spawn_tmux_repl must reset self._tailer — "
        "otherwise the caller transitions DEAD with a live orphan "
        "tailer instance (Murzik round-3 cleanup-hole regression)"
    )
    # The tmux.kill_session call from the rollback block should have
    # fired at least once (it may also have been called by the pre-spawn
    # stale-session reaper; either way, count >= 1).
    assert tmux.kill_session.await_count >= 1, (
        "rollback must call tmux.kill_session"
    )


# ──────────────────────────────────────────────────────────────────────────
# Murzik #522 round-1 — worker-level inflight-preservation for transient
# failures (context-lock + idle-prompt timeout). The PR-1 shape ``get()``-d
# the turn from the queue BEFORE _deliver_turn, then let any exception fall
# through the catch-all, silently dropping the message. These tests pin the
# fix at the worker level (not just _deliver_turn unit) — Murzik
# specifically called this out as required.
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_context_lock_preserves_turn_until_released(
    monkeypatch, tmp_path
) -> None:
    """Murzik #522 round-1 (the actual bug): the worker must keep the
    inflight turn in-hand while the context lock is held and re-paste
    after the lock is released, not silently drop the message.

    Pre-fix shape: ``_message_queue.get()`` happened BEFORE
    ``_deliver_turn``; the gate's ``RuntimeError`` fell through the
    worker's catch-all log-only handler. qsize went to 0 and paste_text
    was never called — for that turn or any successor.
    """
    # Speed up the worker's transient-failure backoff so the test
    # doesn't sit on a 2s sleep per retry.
    monkeypatch.setattr(tmux_session, "_TRANSIENT_RETRY_BACKOFF_SEC", 0.01)
    # Sandbox the lock dir to tmp_path.
    monkeypatch.setattr(tmux_session, "_TRANSPORT_LOCK_DIR", tmp_path)

    ss, tmux = _make_session()
    # Touch lock BEFORE connect/send so the very first delivery attempt
    # hits the deferral.
    lock_path = tmp_path / f"{ss.agent_name}.lock"
    lock_path.write_text("")

    await ss.connect()
    await ss.send("hi", platform="t", chat_id="c", message_id="m1")

    # Give the worker several scheduler ticks to (a) get() the turn,
    # (b) hit the deferral, (c) loop a few times still seeing the lock.
    for _ in range(20):
        await asyncio.sleep(0.005)
    # Pre-unlock invariants: paste must NOT have fired, queue is empty
    # (turn is held in _inflight_turn, not in the queue), and the
    # inflight slot holds the turn.
    assert tmux.paste_text.await_count == 0, (
        "Murzik #522 round-1: paste must not fire while context lock "
        "is held — pre-fix this was the silent-drop window"
    )
    assert ss._message_queue.qsize() == 0
    assert ss._inflight_turn is not None
    assert ss._inflight_turn.prompt == "hi"

    # Release the lock. Within a handful of backoff cycles the worker
    # should re-attempt _deliver_turn and paste the SAME turn.
    lock_path.unlink()
    for _ in range(50):
        await asyncio.sleep(0.005)
        if tmux.paste_text.await_count >= 1:
            break

    assert tmux.paste_text.await_count == 1, (
        "Murzik #522 round-1 fix: same turn must re-paste once the lock "
        "is released (pre-fix paste_count stayed at 0 forever)"
    )
    args, _ = tmux.paste_text.call_args
    assert args[0] == "hi"

    # Drive the turn to completion so the worker clears _inflight_turn.
    await ss._handle_turn_complete(TurnResponse(text="ok", stop_reason="end_turn"))
    for _ in range(20):
        await asyncio.sleep(0)
        if ss._inflight_turn is None:
            break
    assert ss._inflight_turn is None

    await ss.disconnect()


# Issue #525 removed the idle-prompt readiness gate (#522 + #524) along
# with its worker-loop escalation paths. The deleted tests covered:
# - test_idle_prompt_timeout_retries_then_force_restarts
# - test_rate_limit_wait_preserves_turn_without_force_restart
# - test_rate_limit_wait_budget_trips_dead_with_turn_preserved
# - test_idle_prompt_preserves_turn_across_force_restart
# - test_pre_first_turn_restart_circuit_breaker_marks_dead
# Splash-state paste is handled by `paste_text` (splash dismisses on
# input focus); no readiness gate is needed.


# ──────────────────────────────────────────────────────────────────────────
# Task #93: tool-use tracking via PreToolUse / PostToolUse hooks
# ──────────────────────────────────────────────────────────────────────────


def _make_session_with_analytics(
    *, agent_name: str = "dymok"
) -> tuple[TmuxSession, MagicMock, MagicMock]:
    """Build a TmuxSession with a mocked analytics_store + stream callback.

    Returns (session, analytics_mock, events_list). The session is in
    CONNECTED state so the methods under test (which don't touch state)
    are reachable without going through cold-start.
    """
    cfg = StreamingSessionConfig(
        agent_name=agent_name,
        working_dir="/tmp/tmux-session-test",
    )
    tmux = _make_mock_tmux()
    analytics = MagicMock()
    analytics.start_tool_call = MagicMock()
    analytics.finish_tool_call = MagicMock()
    events: list[dict] = []

    async def stream_cb(event: dict) -> None:
        events.append(event)

    ss = TmuxSession(
        cfg,
        tmux_control=tmux,
        analytics_store=analytics,
        stream_event_callback=stream_cb,
    )
    return ss, analytics, events


@pytest.mark.asyncio
async def test_record_tool_use_start_emits_event_and_opens_analytics() -> None:
    """``record_tool_use_start`` must update activity, open an analytics
    row keyed by tool_use_id, and emit a ``tool_use_start`` stream event
    matching SDK parity (task #93).
    """
    ss, analytics, events = _make_session_with_analytics()

    await ss.record_tool_use_start(
        tool_use_id="toolu_abc123",
        tool_name="mcp__pinky-self__send_to_agent",
        tool_input={"to": "dymok", "message": "hi"},
    )

    # Activity surfaced for live status consumers.
    assert ss._current_activity  # non-empty human-readable description

    # Analytics row opened with the correct key + PII-safe arg_keys.
    # ``description`` is now also persisted so the chip strip can be
    # rebuilt after a chat-page refresh — it's the same human-readable
    # label already in the live SSE event.
    analytics.start_tool_call.assert_called_once()
    kwargs = analytics.start_tool_call.call_args.kwargs
    assert kwargs["agent_name"] == "dymok"
    assert kwargs["tool_call_key"] == "toolu_abc123"
    assert kwargs["tool_name"] == "mcp__pinky-self__send_to_agent"
    assert kwargs["tool_namespace"] == "pinky-self"
    meta = kwargs["metadata"]
    assert meta["arg_keys"] == ["message", "to"]
    assert meta["description"]  # non-empty human-readable label

    # Stream event emitted with the expected shape.
    assert len(events) == 1
    evt = events[0]
    assert evt["type"] == "tool_use_start"
    assert evt["agent_name"] == "dymok"
    assert evt["tool_use_id"] == "toolu_abc123"
    assert evt["tool_name"] == "mcp__pinky-self__send_to_agent"
    assert evt["tool_namespace"] == "pinky-self"
    assert evt["arg_keys"] == ["message", "to"]
    assert evt["description"]  # non-empty


@pytest.mark.asyncio
async def test_record_tool_use_start_noops_on_empty_tool_name() -> None:
    """Defensive: an empty tool_name should not open analytics or emit."""
    ss, analytics, events = _make_session_with_analytics()
    await ss.record_tool_use_start(
        tool_use_id="x", tool_name="", tool_input={}
    )
    analytics.start_tool_call.assert_not_called()
    assert events == []


@pytest.mark.asyncio
async def test_record_tool_use_start_synthesizes_key_when_missing() -> None:
    """Some Claude Code event flows omit tool_use_id. We must still open
    an analytics row using a synthetic key so the call shows up in the
    feed instead of being silently dropped.
    """
    ss, analytics, events = _make_session_with_analytics()
    await ss.record_tool_use_start(
        tool_use_id="", tool_name="Bash", tool_input={"command": "ls"}
    )
    analytics.start_tool_call.assert_called_once()
    key = analytics.start_tool_call.call_args.kwargs["tool_call_key"]
    assert key.startswith("Bash_")
    assert len(events) == 1


@pytest.mark.asyncio
async def test_record_tool_use_finish_emits_finish_and_closes_analytics() -> None:
    """``record_tool_use_finish`` must close the analytics row keyed by
    tool_use_id with success=True (when not is_error) and emit a
    ``tool_use_finish`` stream event with a capped result_preview.
    """
    ss, analytics, events = _make_session_with_analytics()
    await ss.record_tool_use_finish(
        tool_use_id="toolu_abc123",
        tool_name="Bash",
        is_error=False,
        tool_response={"content": [{"type": "text", "text": "ok"}]},
    )

    analytics.finish_tool_call.assert_called_once()
    kwargs = analytics.finish_tool_call.call_args.kwargs
    assert kwargs["tool_call_key"] == "toolu_abc123"
    assert kwargs["success"] is True
    assert kwargs["error_type"] == ""
    # ``result_preview`` now lands in metadata so the chip strip can
    # surface the truncated tool output after a page refresh. Same
    # 200-char cap as the live SSE event.
    fmeta = kwargs["metadata"] or {}
    assert "result_preview" in fmeta
    assert fmeta["result_preview"]
    assert len(fmeta["result_preview"]) <= 200

    assert len(events) == 1
    evt = events[0]
    assert evt["type"] == "tool_use_finish"
    assert evt["tool_use_id"] == "toolu_abc123"
    assert evt["is_error"] is False
    assert evt["result_preview"]  # non-empty snippet
    assert len(evt["result_preview"]) <= 200


@pytest.mark.asyncio
async def test_record_tool_use_finish_marks_error_when_is_error() -> None:
    """When ``is_error=True``, analytics gets success=False + error_type
    and the stream event carries the flag through for UI consumers.
    """
    ss, analytics, events = _make_session_with_analytics()
    await ss.record_tool_use_finish(
        tool_use_id="toolu_err",
        tool_name="Bash",
        is_error=True,
        tool_response="command not found: blorp",
    )

    analytics.finish_tool_call.assert_called_once()
    kwargs = analytics.finish_tool_call.call_args.kwargs
    assert kwargs["success"] is False
    assert kwargs["error_type"] == "tool_error"

    assert events[0]["is_error"] is True


@pytest.mark.asyncio
async def test_record_tool_use_finish_skips_analytics_without_id() -> None:
    """No tool_use_id → skip analytics close (no row to close), but
    still emit the stream event so consumers see the finish signal.
    """
    ss, analytics, events = _make_session_with_analytics()
    await ss.record_tool_use_finish(
        tool_use_id="",
        tool_name="Bash",
        is_error=False,
        tool_response="ok",
    )
    analytics.finish_tool_call.assert_not_called()
    assert len(events) == 1
    assert events[0]["type"] == "tool_use_finish"


@pytest.mark.asyncio
async def test_record_tool_use_finish_caps_huge_response_at_200() -> None:
    """``result_preview`` must be capped to match SDK's 200-char cap;
    tool responses can be enormous (file contents, search results).
    """
    ss, analytics, events = _make_session_with_analytics()
    huge = "x" * 5000
    await ss.record_tool_use_finish(
        tool_use_id="toolu_huge",
        tool_name="Read",
        is_error=False,
        tool_response=huge,
    )
    assert len(events[0]["result_preview"]) == 200


# ──────────────────────────────────────────────────────────────────────────
# Pane snapshot (read-only viewer endpoint)
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_pane_snapshot_returns_stdout_with_escapes() -> None:
    """``get_pane_snapshot`` calls capture_pane with escapes=True and
    returns the raw stdout (ANSI escapes preserved for xterm.js)."""
    ss, tmux = _make_session()
    tmux.capture_pane = AsyncMock(return_value=TmuxCommandResult(
        returncode=0,
        stdout="\x1b[1;32mhello\x1b[0m",
        stderr="",
    ))
    out = await ss.get_pane_snapshot(lines=150)
    assert out == "\x1b[1;32mhello\x1b[0m"
    # Verify escapes flag was passed through.
    tmux.capture_pane.assert_awaited_once()
    kwargs = tmux.capture_pane.await_args.kwargs
    assert kwargs.get("escapes") is True
    assert kwargs.get("lines") == 150


@pytest.mark.asyncio
async def test_get_pane_snapshot_returns_empty_on_tmux_failure() -> None:
    """A non-zero return from tmux capture-pane (transient server blip,
    session went away during a frame) must NOT raise — the SSE generator
    just emits an empty frame and the next one will recover."""
    ss, tmux = _make_session()
    tmux.capture_pane = AsyncMock(return_value=TmuxCommandResult(
        returncode=1, stdout="", stderr="lost server",
    ))
    out = await ss.get_pane_snapshot()
    assert out == ""


@pytest.mark.asyncio
async def test_get_pane_snapshot_swallows_subprocess_exception() -> None:
    """If capture_pane itself raises (e.g. the tmux binary disappeared
    mid-flight), the snapshot helper must return "" rather than letting
    the exception escape into the SSE generator and kill the stream."""
    ss, tmux = _make_session()
    tmux.capture_pane = AsyncMock(side_effect=RuntimeError("tmux gone"))
    out = await ss.get_pane_snapshot()
    assert out == ""


# ──────────────────────────────────────────────────────────────────────────
# Context-budget watchdog (task #95)
#
# Per-turn usage accumulation + ``context_usage`` SSE emission +
# ``restart_nudge`` when crossing the agent's restart_threshold_pct.
# Tmux agents need this for parity with SDK agents — without it they're
# blind to their own context window and can't make their own /compact /
# restart / sleep decisions.
# ──────────────────────────────────────────────────────────────────────────


def _turn_response(*, input_tokens: int, output_tokens: int = 0,
                   cache_read: int = 0, cache_write: int = 0,
                   text: str = "ok") -> TurnResponse:
    """Build a TurnResponse with a realistic usage block.

    Mirrors the Claude Code transcript schema: cache fields use the
    ``cache_creation_input_tokens`` / ``cache_read_input_tokens`` names
    rather than the SDK's shortened forms. The watchdog accepts either.
    """
    return TurnResponse(
        text=text,
        stop_reason="end_turn",
        usage={
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_creation_input_tokens": cache_write,
            "cache_read_input_tokens": cache_read,
        },
        duration_ms=100,
        assistant_entry_count=1,
        tool_uses=[],
    )


@pytest.mark.asyncio
async def test_record_turn_usage_accumulates_across_turns() -> None:
    """Cumulative token counts on ``self.usage`` grow turn over turn,
    rather than getting overwritten. Without this, the chat UI's
    context-percentage gauge sees only the latest turn's deltas and
    can't show "approaching compaction"."""
    ss, _ = _make_session_with_response_cb()
    _seed_inflight(ss)  # #560
    await ss._handle_turn_complete(
        _turn_response(input_tokens=1000, output_tokens=100, cache_read=500)
    )
    _seed_inflight(ss)  # #560 — second turn
    await ss._handle_turn_complete(
        _turn_response(input_tokens=2000, output_tokens=200, cache_write=300)
    )
    assert ss.usage.input_tokens == 3000
    assert ss.usage.output_tokens == 300
    assert ss.usage.cache_read_tokens == 500
    assert ss.usage.cache_write_tokens == 300
    assert ss.usage.total_turns == 2


@pytest.mark.asyncio
async def test_record_turn_usage_tolerates_schema_drift() -> None:
    """A usage block with unexpected shapes (None values, strings,
    missing keys) must not crash the tailer. Token visibility is
    best-effort; the tailer's invariant is to keep running."""
    ss, _ = _make_session_with_response_cb()
    _seed_inflight(ss)  # #560
    drift = TurnResponse(
        text="ok",
        stop_reason="end_turn",
        usage={"input_tokens": None, "output_tokens": "definitely a number"},
        duration_ms=50,
        assistant_entry_count=1,
        tool_uses=[],
    )
    # Must not raise.
    await ss._handle_turn_complete(drift)
    # Should still bump turn count + record stop_reason even if tokens drift.
    assert ss.usage.total_turns == 1
    assert ss.usage.last_stop_reason == "end_turn"


@pytest.mark.asyncio
async def test_emits_context_usage_event_with_sdk_shape() -> None:
    """``context_usage`` SSE event must carry the SDK-compatible fields
    so Chat.svelte's session-info card renders for tmux agents the same
    way it does for SDK ones (totalTokens / maxTokens / categories)."""
    events: list[dict] = []

    async def stream_cb(evt):
        events.append(evt)

    ss, _ = _make_session_with_response_cb(stream_evt=stream_cb)
    _seed_inflight(ss)  # #560
    await ss._handle_turn_complete(
        _turn_response(input_tokens=10_000, output_tokens=500, cache_read=2_000)
    )

    ctx_events = [e for e in events if e["type"] == "context_usage"]
    assert len(ctx_events) == 1
    evt = ctx_events[0]
    assert evt["agent_name"] == "dymok"
    assert evt["totalTokens"] == 12_500
    # Effective max for a 200K-raw model = 200K - 33K autocompact
    # buffer (Claude Code reserves room for /compact to fire before
    # the API rejects for context exhaustion). ``rawMaxTokens`` keeps
    # the raw cap visible for any UI that wants both.
    assert evt["maxTokens"] == 200_000 - 33_000
    assert evt["rawMaxTokens"] == 200_000
    assert evt["percentage"] == round(12_500 / (200_000 - 33_000) * 100, 1)
    # Category breakdown — coarse for tmux, but matches the SDK's shape.
    names = {c["name"] for c in evt["categories"]}
    assert names == {"Input", "Output", "Cache read", "Cache write"}


@pytest.mark.asyncio
async def test_get_context_info_returns_sdk_shape() -> None:
    """``get_context_info()`` is the synchronous fallback that
    ``api._streaming_context_info`` calls when no ``_client`` is
    present (the tmux path). Shape must match what the existing
    ``/streaming/status`` consumer expects."""
    ss, _ = _make_session_with_response_cb()
    _seed_inflight(ss)  # #560
    await ss._handle_turn_complete(
        _turn_response(input_tokens=5_000, output_tokens=200)
    )
    info = ss.get_context_info()
    # camelCase + snake_case both populated — different call sites read
    # different conventions.
    assert info["totalTokens"] == 5_200
    assert info["total_tokens"] == 5_200
    assert info["maxTokens"] == info["max_tokens"]
    assert info["percentage"] >= 0.0
    assert isinstance(info["categories"], list)
    assert info["mcpTools"] == info["mcp_tools"] == []


@pytest.mark.asyncio
async def test_raw_max_tokens_for_1m_vs_200k_model() -> None:
    """``_raw_max_tokens_for_model`` returns the raw cap unaltered —
    1M for ``_1M_MODELS`` members, 200k otherwise. Without this split,
    tmux agents on Opus 4.7 would peg their context gauge at 20% of
    real capacity."""
    from pinky_daemon import streaming_session as _ss_mod
    # Pin a known 1M model so the test doesn't drift if the DB-backed
    # set changes underneath us.
    original = set(_ss_mod._1M_MODELS)
    try:
        _ss_mod._1M_MODELS = {"claude-opus-4-7"}
        cfg = StreamingSessionConfig(
            agent_name="dymok",
            working_dir="/tmp/tmux-session-test",
            model="claude-opus-4-7",
        )
        tmux = _make_mock_tmux()
        ss = TmuxSession(cfg, tmux_control=tmux)
        assert ss._raw_max_tokens_for_model() == 1_000_000

        cfg2 = StreamingSessionConfig(
            agent_name="dymok",
            working_dir="/tmp/tmux-session-test",
            model="claude-haiku-4-5",
        )
        ss2 = TmuxSession(cfg2, tmux_control=tmux)
        assert ss2._raw_max_tokens_for_model() == 200_000
    finally:
        _ss_mod._1M_MODELS = original


@pytest.mark.asyncio
async def test_max_tokens_subtracts_autocompact_buffer(monkeypatch) -> None:
    """Effective cap = raw - 33K. Critical: Claude Code's ``/compact``
    fires before the API rejects for context exhaustion, so the gauge
    must measure against effective max not raw — otherwise we
    under-report by ~16 points on the 200K window and the
    restart-nudge fires too late."""
    monkeypatch.delenv("CLAUDE_AUTOCOMPACT_PCT_OVERRIDE", raising=False)

    from pinky_daemon import streaming_session as _ss_mod
    original = set(_ss_mod._1M_MODELS)
    try:
        _ss_mod._1M_MODELS = {"claude-opus-4-7"}

        cfg = StreamingSessionConfig(
            agent_name="dymok",
            working_dir="/tmp/tmux-session-test",
            model="claude-haiku-4-5",
        )
        ss = TmuxSession(cfg, tmux_control=_make_mock_tmux())
        # 200K raw → 167K effective.
        assert ss._max_tokens_for_model() == 200_000 - 33_000

        cfg_big = StreamingSessionConfig(
            agent_name="dymok",
            working_dir="/tmp/tmux-session-test",
            model="claude-opus-4-7",
        )
        ss_big = TmuxSession(cfg_big, tmux_control=_make_mock_tmux())
        # 1M raw → 967K effective. Per SDK source the autocompact
        # buffer is a fixed 33K, not a proportional fraction.
        assert ss_big._max_tokens_for_model() == 1_000_000 - 33_000
    finally:
        _ss_mod._1M_MODELS = original


@pytest.mark.asyncio
async def test_autocompact_override_env_sets_effective_cap(monkeypatch) -> None:
    """``CLAUDE_AUTOCOMPACT_PCT_OVERRIDE`` is Claude Code's own env
    var — the percentage at which autocompact triggers. We interpret
    it as effective-cap percentage of raw, matching the docs and the
    SDK behavior."""
    cfg = StreamingSessionConfig(
        agent_name="dymok",
        working_dir="/tmp/tmux-session-test",
        model="claude-haiku-4-5",
    )
    tmux = _make_mock_tmux()
    ss = TmuxSession(cfg, tmux_control=tmux)

    # 85% → effective = 0.85 * 200K = 170K.
    monkeypatch.setenv("CLAUDE_AUTOCOMPACT_PCT_OVERRIDE", "85")
    assert ss._max_tokens_for_model() == 170_000

    # 100 → autocompact disabled, effective == raw.
    monkeypatch.setenv("CLAUDE_AUTOCOMPACT_PCT_OVERRIDE", "100")
    assert ss._max_tokens_for_model() == 200_000

    # Malformed value falls back to the default 33K buffer.
    monkeypatch.setenv("CLAUDE_AUTOCOMPACT_PCT_OVERRIDE", "not-a-number")
    assert ss._max_tokens_for_model() == 200_000 - 33_000

    # Empty / zero values also fall back (zero would imply effective=0,
    # which would be a divide-by-zero waiting to happen).
    monkeypatch.setenv("CLAUDE_AUTOCOMPACT_PCT_OVERRIDE", "0")
    assert ss._max_tokens_for_model() == 200_000 - 33_000


@pytest.mark.asyncio
async def test_get_context_info_exposes_raw_and_effective_caps(monkeypatch) -> None:
    """``get_context_info`` returns both ``maxTokens`` (effective) and
    ``rawMaxTokens`` (raw cap) for SDK parity — matches the shape of
    ``ContextUsageResponse`` so the same frontend can render tmux and
    SDK sessions interchangeably. Snake-case aliases mirror the
    existing camelCase fields."""
    monkeypatch.delenv("CLAUDE_AUTOCOMPACT_PCT_OVERRIDE", raising=False)

    cfg = StreamingSessionConfig(
        agent_name="dymok",
        working_dir="/tmp/tmux-session-test",
        model="claude-haiku-4-5",
    )
    ss = TmuxSession(cfg, tmux_control=_make_mock_tmux())

    info = ss.get_context_info()

    assert info["rawMaxTokens"] == 200_000
    assert info["maxTokens"] == 200_000 - 33_000
    # Snake-case aliases populated identically.
    assert info["raw_max_tokens"] == info["rawMaxTokens"]
    assert info["max_tokens"] == info["maxTokens"]


@pytest.mark.asyncio
async def test_restart_nudge_fires_when_crossing_threshold() -> None:
    """When per-turn context window crosses ``restart_threshold_pct``,
    emit a ``restart_nudge`` SSE event so the chat UI can light up the
    "you should /compact" indicator. SDK agents have had this for
    months via the SDK's context callbacks; tmux now matches.

    Each Claude Code turn re-sends the full prior conversation, so the
    LAST turn's ``input_tokens`` already reflects the current window —
    no summing needed. Threshold check rides off ``last_usage``.
    """
    events: list[dict] = []

    async def stream_cb(evt):
        events.append(evt)

    ss, _ = _make_session_with_response_cb(stream_evt=stream_cb)

    # Stub the threshold so we can hit it deterministically with small
    # token counts (default is 80%, which is 160k tokens of 200k — too
    # big to push through in a unit test usage block).
    ss._restart_threshold_pct = lambda: 50.0

    # Below threshold: no nudge. (50k / 200k = 25%.)
    _seed_inflight(ss)  # #560
    await ss._handle_turn_complete(_turn_response(input_tokens=50_000))
    assert not [e for e in events if e["type"] == "restart_nudge"]

    # Cross threshold: nudge fires. (110k / 200k = 55%.)
    _seed_inflight(ss)  # #560
    await ss._handle_turn_complete(_turn_response(input_tokens=110_000))
    nudges = [e for e in events if e["type"] == "restart_nudge"]
    assert len(nudges) == 1
    assert nudges[0]["percentage"] >= 50.0
    assert nudges[0]["threshold_pct"] == 50.0


@pytest.mark.asyncio
async def test_restart_nudge_does_not_refire_while_above_threshold() -> None:
    """Once above threshold, subsequent turns must not fire the nudge
    every time. The chat UI gets one signal per crossing, not a
    cascade of identical events while context stays above the bar."""
    events: list[dict] = []

    async def stream_cb(evt):
        events.append(evt)

    ss, _ = _make_session_with_response_cb(stream_evt=stream_cb)
    ss._restart_threshold_pct = lambda: 50.0

    # Three turns, all above threshold — only one nudge total.
    _seed_inflight(ss)  # #560
    await ss._handle_turn_complete(_turn_response(input_tokens=110_000))
    _seed_inflight(ss)
    await ss._handle_turn_complete(_turn_response(input_tokens=130_000))
    _seed_inflight(ss)
    await ss._handle_turn_complete(_turn_response(input_tokens=140_000))

    nudges = [e for e in events if e["type"] == "restart_nudge"]
    assert len(nudges) == 1


@pytest.mark.asyncio
async def test_restart_nudge_rearms_after_drop_below_threshold() -> None:
    """After a /compact, the per-turn input_tokens drops sharply (the
    conversation history was compressed). The next sub-threshold turn
    re-arms the latch; a subsequent above-threshold turn fires a fresh
    nudge."""
    events: list[dict] = []

    async def stream_cb(evt):
        events.append(evt)

    ss, _ = _make_session_with_response_cb(stream_evt=stream_cb)
    ss._restart_threshold_pct = lambda: 50.0

    # Cross threshold (above 50% of 200k).
    _seed_inflight(ss)  # #560
    await ss._handle_turn_complete(_turn_response(input_tokens=110_000))
    assert ss._restart_nudge_fired is True

    # Simulate post-compact: a turn with low input_tokens. The latch
    # resets because the window measurement is now below threshold.
    _seed_inflight(ss)
    await ss._handle_turn_complete(_turn_response(input_tokens=5_000))
    assert ss._restart_nudge_fired is False

    # Cross again — fresh nudge.
    _seed_inflight(ss)
    await ss._handle_turn_complete(_turn_response(input_tokens=110_000))
    nudges = [e for e in events if e["type"] == "restart_nudge"]
    assert len(nudges) == 2


# ──────────────────────────────────────────────────────────────────────────
# Wake-prompt / internal-prompt path (PR for #543)
# ──────────────────────────────────────────────────────────────────────────


def _turn_response(
    *,
    text: str = "",
    input_tokens: int = 0,
    cache_read: int = 0,
    cache_write: int = 0,
    output_tokens: int = 0,
    thinking: str = "",
    tool_uses: list | None = None,
    stop_reason: str = "end_turn",
) -> TurnResponse:
    return TurnResponse(
        text=text,
        thinking=thinking,
        tool_uses=tool_uses or [],
        stop_reason=stop_reason,
        usage={
            "input_tokens": input_tokens,
            "cache_read_input_tokens": cache_read,
            "cache_creation_input_tokens": cache_write,
            "output_tokens": output_tokens,
        },
        duration_ms=0,
        assistant_entry_count=1 if text else 0,
    )


class TestInternalPromptBypasses:
    """``_enqueue_internal_prompt`` must NOT mutate the external-stat
    surfaces: no conversation_store user-message append, no
    ``messages_sent`` increment, no ``_inflight_meta`` write.

    These are the contractual guarantees Murzik called out — without
    them, an internal turn would poison external-turn state (Case 1
    regression from PR #496 round-1 surfacing through a new path).
    """

    @pytest.mark.asyncio
    async def test_internal_prompt_does_not_append_to_conversation_store(self):
        conv_store = MagicMock()
        conv_store.append = MagicMock()
        ss, _ = _make_session_with_response_cb(conv_store=conv_store)
        # Force CONNECTED so the enqueue gate passes without running
        # the full connect path.
        ss._state_machine._state = SessionState.CONNECTED

        await ss._enqueue_internal_prompt(
            "wake prompt body",
            reason="wake_new_session",
            wait_for_completion=False,
        )

        conv_store.append.assert_not_called()

    @pytest.mark.asyncio
    async def test_internal_prompt_does_not_increment_messages_sent(self):
        ss, _ = _make_session()
        ss._state_machine._state = SessionState.CONNECTED
        before = ss._stats["messages_sent"]

        await ss._enqueue_internal_prompt(
            "wake prompt body",
            reason="wake_new_session",
            wait_for_completion=False,
        )

        assert ss._stats["messages_sent"] == before, (
            "internal prompts are daemon-side; external-message stats "
            "must stay at the pre-enqueue value"
        )

    @pytest.mark.asyncio
    async def test_internal_prompt_does_not_write_inflight_meta(self):
        """Murzik's regression guard against #496 round-1 Case 1 surfacing
        via the internal-prompt path. An internal turn that wrote routing
        metadata would clobber a back-to-back external turn's routing
        fields.

        #560 retargeting: ``_deliver_turn`` now APPENDS to
        ``_inflight_metas`` (FIFO) instead of overwriting a single dict.
        Internal turns still append with empty meta + ``internal=True``,
        so an external turn dispatched after carries its OWN routing
        in its OWN deque entry — no shared mutable cell to clobber.
        """
        ss, tmux = _make_session()
        ss._state_machine._state = SessionState.CONNECTED

        internal_turn = _QueuedTurn(
            prompt="wake prompt body",
            platform="",
            chat_id="",
            message_id="",
            internal=True,
            reason="wake_new_session",
        )

        await ss._deliver_turn(internal_turn)

        # Internal turn appended an entry with EMPTY meta + internal=True.
        # The back-compat ``_inflight_meta`` property reads the oldest
        # entry's meta — which is empty for this internal turn.
        assert ss._inflight_meta == {}, (
            "internal turn wrote routing metadata — would have clobbered "
            "back-to-back external routing pre-#560; under #560 each "
            "entry carries its own dict so this is structurally impossible"
        )
        assert len(ss._inflight_metas) == 1
        assert ss._inflight_metas[0].internal is True
        assert ss._inflight_metas[0].meta == {}

    @pytest.mark.asyncio
    async def test_internal_wake_prompt_does_not_clobber_external_routing(self):
        """The full Case 1 regression guard, post-#560: enqueue an
        internal wake prompt, then an external turn, dispatch both —
        each entry in ``_inflight_metas`` must carry its OWN routing.
        Pre-#560 the single ``_inflight_meta`` cell would have been
        overwritten by whichever turn dispatched last; post-#560 the
        deque holds both side-by-side."""
        ss, tmux = _make_session()
        ss._state_machine._state = SessionState.CONNECTED

        # Internal turn first — appends an empty-meta + internal=True entry.
        internal_turn = _QueuedTurn(
            prompt="wake",
            internal=True,
            reason="wake_new_session",
        )
        await ss._deliver_turn(internal_turn)
        assert len(ss._inflight_metas) == 1
        assert ss._inflight_metas[0].internal is True
        assert ss._inflight_metas[0].meta == {}

        # External turn next — appends its own routing dict + internal=False.
        # The internal turn's entry is unaffected.
        external_turn = _QueuedTurn(
            prompt="hello",
            platform="telegram",
            chat_id="42",
            message_id="m1",
        )
        await ss._deliver_turn(external_turn)
        assert len(ss._inflight_metas) == 2
        # FIFO: internal first, external second.
        assert ss._inflight_metas[0].internal is True
        assert ss._inflight_metas[0].meta == {}
        assert ss._inflight_metas[1].internal is False
        assert ss._inflight_metas[1].meta == {
            "platform": "telegram",
            "chat_id": "42",
            "message_id": "m1",
        }


class TestInternalPromptCompletion:
    """The ``wait_for_completion`` semantic. Without it, PR B (idle_sleep
    parity) would paste a "please save state" instruction and kill the
    pane before the agent could honor it. This contract is the lifecycle
    primitive Murzik flagged as critical."""

    @pytest.mark.asyncio
    async def test_wait_for_completion_false_returns_immediately(self):
        ss, _ = _make_session()
        ss._state_machine._state = SessionState.CONNECTED
        result = await ss._enqueue_internal_prompt(
            "wake",
            reason="wake_new_session",
            wait_for_completion=False,
        )
        # When wait=False, the helper returns the completion_event (or
        # None if the caller doesn't need to observe). Per implementation
        # it returns None because we don't construct the event in
        # fire-and-forget mode. This pins that contract.
        assert result is None or isinstance(result, asyncio.Event)
        # The internal turn is in the queue.
        assert ss._message_queue.qsize() == 1

    @pytest.mark.asyncio
    async def test_wait_for_completion_true_blocks_until_event_fires(self):
        """The caller awaits the completion event before returning. The
        worker sets it from ``_handle_turn_complete``. We simulate that
        sequence here without running the full worker loop."""
        ss, _ = _make_session()
        ss._state_machine._state = SessionState.CONNECTED

        async def _enqueue():
            await ss._enqueue_internal_prompt(
                "presave",
                reason="idle_sleep_presave",
                wait_for_completion=True,
                timeout_sec=2.0,
            )

        enqueue_task = asyncio.create_task(_enqueue())
        # Yield so the enqueue task can put the turn on the queue + start
        # awaiting the completion event.
        for _ in range(5):
            await asyncio.sleep(0)
            if ss._message_queue.qsize() >= 1:
                break

        # Pull the queued turn, simulate the worker's "inflight" state,
        # and fire the completion event the way ``_handle_turn_complete``
        # would. The caller awaiting ``_enqueue`` should now return.
        turn = await ss._message_queue.get()
        ss._inflight_turn = turn
        assert turn.completion_event is not None
        turn.completion_event.set()

        await asyncio.wait_for(enqueue_task, timeout=1.0)

    @pytest.mark.asyncio
    async def test_wait_for_completion_timeout_raises(self):
        ss, _ = _make_session()
        ss._state_machine._state = SessionState.CONNECTED
        with pytest.raises(asyncio.TimeoutError):
            await ss._enqueue_internal_prompt(
                "presave",
                reason="idle_sleep_presave",
                wait_for_completion=True,
                timeout_sec=0.05,
            )

    @pytest.mark.asyncio
    async def test_completion_event_fires_before_turn_done(self):
        """Critical ordering: ``completion_event.set()`` MUST fire before
        ``_turn_done.set()`` in ``_handle_turn_complete``. Otherwise a
        ``wait_for_completion=True`` caller might observe ``_turn_done``
        without its own event being set — a race that could break PR B's
        "save state before disconnect" contract.

        #560 retargeting: pre-#560 the completion_event lived on
        ``self._inflight_turn`` (the worker's current dispatch). Under
        concurrent dispatch each entry in ``_inflight_metas`` carries
        its OWN completion_event; the handler popleft's the entry and
        fires its event. Still in the synchronous critical section,
        still before ``_turn_done``.
        """
        ss, _ = _make_session()
        ss._state_machine._state = SessionState.CONNECTED

        # Seed an internal entry with a completion_event into the deque.
        completion = asyncio.Event()
        _seed_inflight(ss, internal=True, completion_event=completion)
        # _turn_done starts cleared.
        ss._turn_done.clear()

        await ss._handle_turn_complete(_turn_response(text="ack"))

        assert completion.is_set(), (
            "completion_event must be set by _handle_turn_complete"
        )
        assert ss._turn_done.is_set(), (
            "_turn_done must also be set so the worker progresses"
        )


class TestInternalTurnSkipsResponseCallback:
    """Internal turns have no chat target — the response_callback must
    not fire (no broker routing, no Telegram delivery)."""

    @pytest.mark.asyncio
    async def test_handle_turn_complete_skips_response_callback_for_internal(self):
        cb = _AsyncCollector()
        ss, _ = _make_session_with_response_cb(response_cb=cb)
        ss._state_machine._state = SessionState.CONNECTED
        ss._inflight_turn = _QueuedTurn(
            prompt="wake",
            internal=True,
            reason="wake_new_session",
        )

        await ss._handle_turn_complete(_turn_response(text="agent reply"))

        assert cb.calls == [], (
            "internal turn must not invoke response_callback — no chat "
            "to route the reply back to"
        )

    @pytest.mark.asyncio
    async def test_handle_turn_complete_skips_conversation_store_for_internal(self):
        conv_store = MagicMock()
        conv_store.append = MagicMock()
        ss, _ = _make_session_with_response_cb(conv_store=conv_store)
        ss._state_machine._state = SessionState.CONNECTED
        ss._inflight_turn = _QueuedTurn(
            prompt="wake",
            internal=True,
            reason="wake_new_session",
        )

        await ss._handle_turn_complete(_turn_response(text="agent reply"))

        # No user-message append (handled in _enqueue_internal_prompt
        # by not calling conv_store at all), no assistant-message append
        # (this method's branch).
        conv_store.append.assert_not_called()


class TestForceFreshContextOnce:
    """``--continue`` suppression flag — independent contract from
    wake-prompt copy (per Murzik's separation principle)."""

    @pytest.mark.asyncio
    async def test_build_claude_cmd_suppresses_continue_when_flag_set(self, tmp_path):
        ss, _ = _make_session()
        # Force a prior-transcript condition so ``_build_claude_cmd``
        # would normally add ``--continue``.
        ss._has_prior_transcript = lambda: True

        ss._config.force_fresh_context_once = True
        cmd = ss._build_claude_cmd()

        assert "--continue" not in cmd, (
            "force_fresh_context_once must suppress --continue even when "
            "a prior transcript exists"
        )

    def test_build_claude_cmd_records_launch_mode_on_session(self):
        ss, _ = _make_session()
        ss._has_prior_transcript = lambda: True
        ss._config.force_fresh_context_once = True

        ss._build_claude_cmd()

        assert ss._last_launch_forced_fresh is True
        assert ss._last_launch_used_continue is False
        assert ss._last_launch_had_prior_transcript is True

    def test_build_claude_cmd_default_uses_continue_with_prior_transcript(self):
        ss, _ = _make_session()
        ss._has_prior_transcript = lambda: True
        # force_fresh defaults to False.
        cmd = ss._build_claude_cmd()
        assert "--continue" in cmd


class TestWakePromptEnqueueOnConnect:
    """Wake-prompt assembly + enqueue is the parent defect from #543.
    These tests pin that ``connect()`` actually injects the wake prompt,
    with the right WakeReason for each runtime signal."""

    @pytest.mark.asyncio
    async def test_connect_enqueues_wake_prompt_by_default(self):
        ss, _ = _make_session()
        # Override the test-default skip so the real path runs.
        ss._skip_wake_prompt_for_tests = False
        await ss.connect()

        assert ss._message_queue.qsize() >= 1, (
            "connect() must enqueue a wake prompt"
        )
        turn = ss._message_queue._queue[0]  # type: ignore[attr-defined]
        assert turn.internal is True
        assert turn.reason.startswith("wake_")
        await ss.disconnect()

    @pytest.mark.asyncio
    async def test_connect_skips_wake_prompt_when_flag_set(self):
        """Test seam — verifies the unit-test path doesn't queue the
        wake prompt (otherwise the worker would hang waiting for the
        transcript tailer to fire ``_handle_turn_complete``)."""
        ss, _ = _make_session()
        # Default: skip flag is True.
        await ss.connect()
        assert ss._message_queue.qsize() == 0
        await ss.disconnect()

    @pytest.mark.asyncio
    async def test_connect_wake_reason_is_new_session_on_cold_start(self):
        ss, _ = _make_session()
        ss._skip_wake_prompt_for_tests = False
        # No prior transcript, no restart_reason → NEW_SESSION.
        await ss.connect()
        turn = ss._message_queue._queue[0]  # type: ignore[attr-defined]
        assert "new_session" in turn.reason
        await ss.disconnect()

    @pytest.mark.asyncio
    async def test_connect_wake_reason_is_context_restart_when_flag_set(self):
        ss, _ = _make_session()
        ss._skip_wake_prompt_for_tests = False
        ss._config.force_fresh_context_once = True

        await ss.connect()

        turn = ss._message_queue._queue[0]  # type: ignore[attr-defined]
        assert "context_restart" in turn.reason
        await ss.disconnect()

    @pytest.mark.asyncio
    async def test_connect_wake_reason_is_auto_restart_when_set(self):
        ss, _ = _make_session()
        ss._skip_wake_prompt_for_tests = False
        ss._config.restart_reason = "auto_restart"

        await ss.connect()

        turn = ss._message_queue._queue[0]  # type: ignore[attr-defined]
        assert "auto_restart" in turn.reason
        await ss.disconnect()

    @pytest.mark.asyncio
    async def test_connect_wake_prompt_body_includes_saved_state_when_present(self):
        ss, _ = _make_session()
        ss._skip_wake_prompt_for_tests = False
        ss._config.wake_context = "## Continuation\nWorking on X"

        await ss.connect()

        turn = ss._message_queue._queue[0]  # type: ignore[attr-defined]
        assert "── Saved State ──" in turn.prompt
        assert "## Continuation" in turn.prompt
        assert "Working on X" in turn.prompt
        await ss.disconnect()


# ──────────────────────────────────────────────────────────────────────────
# Idle-sleep parity (PR B for #543) + #545 follow-ups (Murzik review)
# ──────────────────────────────────────────────────────────────────────────


class TestIdleSleepPresavePrompt:
    """Pre-sleep save instruction parity with SDK.

    Tmux ``idle_sleep()`` must enqueue the "use reflect()/save state"
    prompt the SDK sends, with ``wait_for_completion=True`` so the
    disconnect doesn't kill the pane before the agent honors the
    instruction (the footgun Murzik flagged when reviewing the
    internal-prompt API)."""

    @pytest.mark.asyncio
    async def test_idle_sleep_enqueues_presave_prompt_before_disconnect(self):
        """The pre-sleep prompt must be enqueued via
        ``_enqueue_internal_prompt`` BEFORE the state transition to
        IDLE_SLEEPING — otherwise the enqueue gate (which requires
        CONNECTED) would drop the prompt."""
        ss, tmux = _make_session(state=SessionState.CONNECTED)
        observed: list[tuple[str, dict]] = []

        async def _spy(prompt, *, reason, wait_for_completion=False, timeout_sec=None):
            observed.append(
                (prompt, {"reason": reason, "wait": wait_for_completion, "timeout": timeout_sec})
            )
            # Don't actually call original — we don't want to involve the
            # message queue + worker in this unit test (no transcript
            # tailer to fire ``_handle_turn_complete``). The contract
            # we're pinning is "idle_sleep called _enqueue_internal_prompt
            # with the right args BEFORE state transition + disconnect."
            return None

        ss._enqueue_internal_prompt = _spy

        result = await ss.idle_sleep()
        assert result is True
        assert ss.state == SessionState.IDLE_SLEEPING

        # The pre-sleep prompt was sent.
        assert len(observed) == 1, "exactly one pre-sleep prompt expected"
        prompt, kwargs = observed[0]
        assert "Auto-sleep is activating" in prompt
        assert "reflect()" in prompt
        assert kwargs["reason"] == "idle_sleep_presave"
        assert kwargs["wait"] is True, (
            "wait_for_completion MUST be True — otherwise disconnect "
            "would kill the pane before the agent could honor the save "
            "instruction (footgun from Murzik's API review)"
        )
        assert kwargs["timeout"] == 120.0

    @pytest.mark.asyncio
    async def test_idle_sleep_proceeds_on_presave_timeout(self):
        """A wedged REPL must not block idle-sleep semantics. If the
        pre-sleep enqueue times out, log + continue to disconnect."""
        ss, _ = _make_session(state=SessionState.CONNECTED)

        async def _timeout(*args, **kwargs):
            raise asyncio.TimeoutError("simulated")

        ss._enqueue_internal_prompt = _timeout

        result = await ss.idle_sleep()
        assert result is True, (
            "idle_sleep must complete even when pre-sleep enqueue raises"
        )
        assert ss.state == SessionState.IDLE_SLEEPING

    @pytest.mark.asyncio
    async def test_idle_sleep_proceeds_on_presave_arbitrary_failure(self):
        ss, _ = _make_session(state=SessionState.CONNECTED)

        async def _raise(*args, **kwargs):
            raise RuntimeError("simulated REPL failure")

        ss._enqueue_internal_prompt = _raise

        result = await ss.idle_sleep()
        assert result is True
        assert ss.state == SessionState.IDLE_SLEEPING


class TestForceFreshContextOnceDeferredConsume:
    """Murzik's #545 follow-up: the flag MUST stay set across a failed
    spawn so a retry honors the fresh-context guarantee. Two regressions:

    1. ``new_session`` fails first time → retry sees flag still set.
    2. ``_start_tailer`` fails first time → retry sees flag still set
       (this is the round-2 case that catches a post-``_spawn()``
       clear instead of post-``_start_tailer()`` clear).
    """

    @pytest.mark.asyncio
    async def test_build_claude_cmd_does_not_consume_flag(self):
        """The flag must survive ``_build_claude_cmd`` so a failed
        ``_spawn_tmux_repl`` leaves the flag observable for retry."""
        ss, _ = _make_session()
        ss._has_prior_transcript = lambda: True
        ss._config.force_fresh_context_once = True

        ss._build_claude_cmd()

        assert ss._config.force_fresh_context_once is True, (
            "_build_claude_cmd must NOT consume the flag — that's the "
            "spawn-success path's job. Premature consumption is the bug "
            "Murzik caught on PR #545 review."
        )

    @pytest.mark.asyncio
    async def test_force_fresh_survives_failed_new_session_retry(self):
        """Failure mode 1: ``new_session`` fails on first attempt.
        Flag stays set; retry's ``_build_claude_cmd`` re-applies the
        suppression."""
        tmux = _make_mock_tmux()
        # First call fails, second call succeeds.
        tmux.new_session = AsyncMock(side_effect=[_fail("rc=1"), _ok()])
        ss, _ = _make_session(tmux=tmux)

        # Seed flag + prior transcript so suppression is meaningful.
        ss._config.force_fresh_context_once = True
        ss._has_prior_transcript = lambda: True

        # First connect — should fail.
        with pytest.raises(RuntimeError, match="new-session failed"):
            await ss.connect()

        # Flag must still be set so the retry honors it.
        assert ss._config.force_fresh_context_once is True, (
            "force_fresh_context_once was consumed despite spawn failure — "
            "retry would silently lose the fresh-context guarantee"
        )

        # Reset state machine + recreate tmux (kept original behavior:
        # second new_session call returns _ok()). Force CONNECTED-eligible
        # state for retry.
        ss._state_machine._state = SessionState.UNINITIALIZED
        await ss.connect()
        assert ss.state == SessionState.CONNECTED
        # After successful spawn + tailer, flag is now consumed.
        assert ss._config.force_fresh_context_once is False, (
            "flag must consume on successful launch — otherwise it stays "
            "set forever and every subsequent launch is fresh"
        )

    @pytest.mark.asyncio
    async def test_force_fresh_survives_failed_tailer_start_retry(self):
        """Failure mode 2 (the round-2 case): ``_start_tailer`` fails
        after ``new_session`` succeeded. Flag stays set; retry honors it.

        This is the test that catches a post-``_spawn()`` clear
        (the round-1 fix that Murzik flagged as still buggy)."""
        ss, _ = _make_session()
        ss._config.force_fresh_context_once = True
        ss._has_prior_transcript = lambda: True

        # Make _start_tailer fail on first call, succeed on second.
        call_count = {"n": 0}
        real_start_tailer = ss._start_tailer

        async def _flaky_start_tailer():
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise RuntimeError("simulated tailer-start failure")
            await real_start_tailer()

        ss._start_tailer = _flaky_start_tailer

        # First connect — tailer-start raises, _spawn_tmux_repl rolls
        # back the tmux session, exception propagates.
        with pytest.raises(RuntimeError, match="tailer-start failure"):
            await ss.connect()

        # Round-2 case: even though ``_spawn()`` succeeded above, the
        # tailer-start failure rolled back the whole launch — flag
        # MUST still be set.
        assert ss._config.force_fresh_context_once is True, (
            "force_fresh_context_once was consumed after _spawn() but "
            "before _start_tailer() succeeded — retry would lose the "
            "fresh-context guarantee. This is the round-2 case from "
            "Murzik's #545 review."
        )

        # Reset for retry.
        ss._state_machine._state = SessionState.UNINITIALIZED
        await ss.connect()
        # After REPL + tailer both up, NOW the flag clears.
        assert ss._config.force_fresh_context_once is False


class TestInternalPromptReturnContract:
    """Murzik #545 follow-up: ``_enqueue_internal_prompt`` always
    returns None. Earlier drafts suggested lazy event observation on
    wait=False but the pattern wasn't used and added a footgun."""

    @pytest.mark.asyncio
    async def test_wait_false_returns_none(self):
        ss, _ = _make_session()
        ss._state_machine._state = SessionState.CONNECTED
        result = await ss._enqueue_internal_prompt(
            "wake",
            reason="wake_new_session",
            wait_for_completion=False,
        )
        assert result is None, (
            "wait_for_completion=False must return None — lazy event "
            "observation is intentionally not part of the contract "
            "(Murzik #545 follow-up; doc/impl mismatch fixed)"
        )

    @pytest.mark.asyncio
    async def test_wait_true_returns_none(self):
        """For consistency: wait_for_completion=True also returns None
        (the call already waited inline)."""
        ss, _ = _make_session()
        ss._state_machine._state = SessionState.CONNECTED

        # Same simulation as test_wait_for_completion_true_blocks_until_event_fires.
        async def _enqueue():
            return await ss._enqueue_internal_prompt(
                "presave",
                reason="idle_sleep_presave",
                wait_for_completion=True,
                timeout_sec=2.0,
            )

        task = asyncio.create_task(_enqueue())
        for _ in range(5):
            await asyncio.sleep(0)
            if ss._message_queue.qsize() >= 1:
                break
        turn = await ss._message_queue.get()
        ss._inflight_turn = turn
        assert turn.completion_event is not None
        turn.completion_event.set()
        result = await asyncio.wait_for(task, timeout=1.0)
        assert result is None

    @pytest.mark.asyncio
    async def test_dropped_when_not_connected_returns_none(self):
        ss, _ = _make_session()
        ss._state_machine._state = SessionState.DEAD
        result = await ss._enqueue_internal_prompt(
            "wake",
            reason="wake_new_session",
            wait_for_completion=False,
        )
        assert result is None


# ──────────────────────────────────────────────────────────────────────────
# #560 — concurrent dispatch / inflight deque
# ──────────────────────────────────────────────────────────────────────────


class TestInflightDequeConcurrentDispatch:
    """The contract block #560 introduces.

    Murzik's review points pinned as named tests:
    1. FIFO response routing across three back-to-back chat_ids
    2. Internal-prompt completion_event fires only on its own popleft
    3. Paste failure → no deque append + immediate completion_event set
    4. Force_restart / disconnect drains the deque and unblocks every
       pending completion_event so wait_for_completion callers can't
       hang on an abandoned turn
    5. Watchdog ages the HEAD, not paste_time — a queued turn gets its
       own full timeout window once it becomes the head
    """

    @pytest.mark.asyncio
    async def test_fifo_routing_a_b_c_no_cross_leak(self):
        """Three turns with distinct chat_ids → three stop hooks → each
        response routes to its own chat in FIFO order. Pinned per
        Murzik review point #3."""
        cb = _AsyncCollector()
        ss, _ = _make_session_with_response_cb(response_cb=cb)
        ss._state_machine._state = SessionState.CONNECTED

        # Seed three external entries directly (bypass paste — this
        # test pins the deque + routing contract, not the paste path).
        _seed_inflight(ss, meta={"platform": "telegram", "chat_id": "A", "message_id": "mA"})
        _seed_inflight(ss, meta={"platform": "telegram", "chat_id": "B", "message_id": "mB"})
        _seed_inflight(ss, meta={"platform": "telegram", "chat_id": "C", "message_id": "mC"})
        assert len(ss._inflight_metas) == 3

        # Fire stop hooks in order.
        await ss._handle_turn_complete(TurnResponse(text="reply A", stop_reason="end_turn"))
        await ss._handle_turn_complete(TurnResponse(text="reply B", stop_reason="end_turn"))
        await ss._handle_turn_complete(TurnResponse(text="reply C", stop_reason="end_turn"))

        assert len(cb.calls) == 3
        assert (cb.calls[0].response_text, cb.calls[0].chat_id) == ("reply A", "A")
        assert (cb.calls[1].response_text, cb.calls[1].chat_id) == ("reply B", "B")
        assert (cb.calls[2].response_text, cb.calls[2].chat_id) == ("reply C", "C")
        assert len(ss._inflight_metas) == 0

    @pytest.mark.asyncio
    async def test_internal_in_middle_completion_event_fires_only_on_own_pop(self):
        """Queue: external, internal-with-event, external. The
        completion_event for the middle internal entry must fire ONLY
        when that entry pops — not on the first external's stop hook,
        not on the last external's. Murzik review point #4."""
        cb = _AsyncCollector()
        ss, _ = _make_session_with_response_cb(response_cb=cb)
        ss._state_machine._state = SessionState.CONNECTED

        completion = asyncio.Event()
        _seed_inflight(ss, meta={"platform": "telegram", "chat_id": "A", "message_id": "mA"})
        _seed_inflight(ss, internal=True, completion_event=completion)
        _seed_inflight(ss, meta={"platform": "telegram", "chat_id": "C", "message_id": "mC"})

        # First stop hook: pops external A. completion_event NOT set yet.
        await ss._handle_turn_complete(TurnResponse(text="reply A", stop_reason="end_turn"))
        assert not completion.is_set(), (
            "completion_event must NOT fire when an unrelated entry pops"
        )
        assert len(cb.calls) == 1
        assert cb.calls[0].chat_id == "A"

        # Second stop hook: pops the internal middle entry. NOW completion fires.
        await ss._handle_turn_complete(TurnResponse(text="ack", stop_reason="end_turn"))
        assert completion.is_set(), (
            "completion_event MUST fire when its own internal entry pops"
        )
        # Internal entry skips response_callback — callback count unchanged.
        assert len(cb.calls) == 1

        # Third stop hook: pops external C.
        await ss._handle_turn_complete(TurnResponse(text="reply C", stop_reason="end_turn"))
        assert len(cb.calls) == 2
        assert cb.calls[1].chat_id == "C"

    @pytest.mark.asyncio
    async def test_paste_failure_does_not_append_to_deque(self):
        """``_deliver_turn`` must not append an entry to
        ``_inflight_metas`` when ``paste_text`` reports !ok. Otherwise a
        future stop hook would pop a meta with no corresponding CC
        turn, routing a later response to the wrong chat."""
        tmux = _make_mock_tmux()
        tmux.paste_text = AsyncMock(return_value=_fail("rc=1"))
        ss, _ = _make_session(state=SessionState.CONNECTED, tmux=tmux)
        turn = _QueuedTurn(prompt="x", platform="t", chat_id="c", message_id="m")
        with pytest.raises(RuntimeError, match="tmux paste-buffer"):
            await ss._deliver_turn(turn)
        assert len(ss._inflight_metas) == 0, (
            "paste failure must NOT leave a phantom meta entry"
        )

    @pytest.mark.asyncio
    async def test_paste_failure_unblocks_pending_completion_event(self):
        """Murzik review point #2 — explicit: a turn with
        ``completion_event`` whose paste fails must NOT hang the caller.
        ``_deliver_turn`` fires the event on the failure path."""
        tmux = _make_mock_tmux()
        tmux.paste_text = AsyncMock(return_value=_fail("rc=1"))
        ss, _ = _make_session(state=SessionState.CONNECTED, tmux=tmux)
        completion = asyncio.Event()
        turn = _QueuedTurn(
            prompt="x", internal=True, reason="presave",
            completion_event=completion,
        )
        with pytest.raises(RuntimeError):
            await ss._deliver_turn(turn)
        assert completion.is_set(), (
            "paste failure must unblock the caller waiting on completion_event"
        )

    @pytest.mark.asyncio
    async def test_disconnect_unblocks_pending_completion_events(self):
        """Disconnect drains ``_inflight_metas`` and sets every pending
        ``completion_event`` so a ``wait_for_completion=True`` caller
        doesn't hang on an abandoned turn (e.g. force_restart cycling
        the pane mid-save). Murzik review point #2 — explicit."""
        ss, _ = _make_session(state=SessionState.CONNECTED)
        await ss.connect()

        event1 = asyncio.Event()
        event2 = asyncio.Event()
        _seed_inflight(ss, internal=True, completion_event=event1)
        _seed_inflight(ss, meta={"platform": "t", "chat_id": "c", "message_id": "m"})
        # No event on the second; the entry is external — disconnect
        # still drains it, just doesn't have anything to signal.
        _seed_inflight(ss, internal=True, completion_event=event2)
        assert len(ss._inflight_metas) == 3

        await ss.disconnect()

        assert len(ss._inflight_metas) == 0
        assert event1.is_set(), "disconnect must unblock event1"
        assert event2.is_set(), "disconnect must unblock event2"

    @pytest.mark.asyncio
    async def test_handle_turn_complete_with_empty_deque_logs_and_bails(self):
        """Empty-deque defense (Murzik review point #7): a stop hook
        with no pending meta must not synthesize routing. It logs and
        skips the callback chain — better silent than wrong-routed.
        """
        cb = _AsyncCollector()
        ss, _ = _make_session_with_response_cb(response_cb=cb)
        ss._state_machine._state = SessionState.CONNECTED
        # Deque is empty.
        assert len(ss._inflight_metas) == 0

        # Handler should bail cleanly without raising or calling cb.
        await ss._handle_turn_complete(TurnResponse(text="orphan", stop_reason="end_turn"))

        assert cb.calls == [], (
            "empty-deque path must NOT synthesize routing — would "
            "re-introduce #496 Case 1 from zero state"
        )

    @pytest.mark.asyncio
    async def test_head_clock_resets_on_popleft_with_remaining_entries(self):
        """Murzik review point #1: when the head pops and entries remain,
        the NEW head's clock starts fresh so it gets its own full
        ``_TURN_DONE_TIMEOUT_SEC`` window (rather than inheriting the
        previous head's age)."""
        ss, _ = _make_session_with_response_cb()
        ss._state_machine._state = SessionState.CONNECTED

        _seed_inflight(ss, meta={"platform": "t", "chat_id": "A", "message_id": "mA"})
        _seed_inflight(ss, meta={"platform": "t", "chat_id": "B", "message_id": "mB"})
        first_head_at = ss._head_started_at
        assert first_head_at is not None

        # Sleep a tiny bit so the new head_started_at differs.
        await asyncio.sleep(0.01)
        await ss._handle_turn_complete(TurnResponse(text="A", stop_reason="end_turn"))

        # Deque still has one entry; head clock should have advanced.
        assert len(ss._inflight_metas) == 1
        assert ss._head_started_at is not None
        assert ss._head_started_at > first_head_at, (
            "post-popleft head clock must reset so the new head gets "
            "its own full timeout window (Murzik #560 review point #1)"
        )

        # Drain — clock becomes None.
        await ss._handle_turn_complete(TurnResponse(text="B", stop_reason="end_turn"))
        assert ss._head_started_at is None
        assert len(ss._inflight_metas) == 0

    @pytest.mark.asyncio
    async def test_worker_permanent_failure_unblocks_inflight_turn_completion_event(self):
        """Issue #547: a turn the worker had in-hand whose delivery
        raised — for any reason other than the explicit !ok branch in
        ``_deliver_turn`` that already fires the event — must still
        unblock its ``completion_event``. Otherwise an unbounded
        ``wait_for_completion=True`` caller (timeout_sec=None) deadlocks
        forever.

        Repro by stubbing ``_deliver_turn`` to raise directly,
        bypassing its own completion_event handling — the catch-all in
        the worker's except branch is what saves the caller.
        """
        ss, _ = _make_session()
        await ss.connect()
        # Stub _deliver_turn to raise unconditionally.
        async def _raise(turn):
            raise RuntimeError("tailer state corruption simulated")
        ss._deliver_turn = _raise

        completion = asyncio.Event()
        internal_turn = _QueuedTurn(
            prompt="presave",
            internal=True,
            reason="idle_sleep_presave",
            completion_event=completion,
        )
        await ss._message_queue.put(internal_turn)

        # Worker should hit the permanent-failure branch and fire the
        # completion event. Tight timeout — a regression manifests as
        # test hang→fail, not a silent deadlock.
        try:
            await asyncio.wait_for(completion.wait(), timeout=2.0)
        except asyncio.TimeoutError:
            pytest.fail(
                "completion_event must fire on worker permanent-failure "
                "path (#547) so unbounded wait_for_completion can't deadlock"
            )

        await ss.disconnect()

    @pytest.mark.asyncio
    async def test_disconnect_unblocks_pre_paste_inflight_turn(self):
        """Issue #547 (extended): the worker may hold ``_inflight_turn``
        for a turn it pulled from the queue but hadn't yet pasted
        (e.g. mid context-lock retry, or worker cancelled before
        ``_deliver_turn`` ran). The meta isn't in ``_inflight_metas``
        yet, so the disconnect drain loop misses it. ``disconnect``
        must explicitly unblock the in-hand turn's ``completion_event``.
        """
        ss, _ = _make_session(state=SessionState.CONNECTED)
        await ss.connect()
        # Inject a pre-paste in-hand turn directly (the worker would
        # normally set this during a context-lock retry).
        completion = asyncio.Event()
        ss._inflight_turn = _QueuedTurn(
            prompt="presave",
            internal=True,
            reason="idle_sleep_presave",
            completion_event=completion,
        )
        # No entry in the deque yet — the paste never happened.
        assert len(ss._inflight_metas) == 0

        await ss.disconnect()

        assert completion.is_set(), (
            "disconnect must unblock the worker's pre-paste in-hand "
            "turn (#547) — otherwise unbounded wait_for_completion hangs"
        )
        assert ss._inflight_turn is None, (
            "disconnect must also clear the in-hand reference so the "
            "next connect doesn't redeliver a stale turn"
        )

    @pytest.mark.asyncio
    async def test_watchdog_requeues_tail_on_stuck_head(self, monkeypatch):
        """Murzik review on PR #561 — critical data-loss fix.

        Before this fix the watchdog drained the WHOLE deque, fired
        all completion_events, and force_restarted — silently dropping
        every queued turn behind the stuck head. With CC's native
        queue absorbing back-to-back pastes, that's a regression vs
        the pre-#560 serial dispatch (where B/C would still be in
        _message_queue when A timed out).

        New contract:
        - HEAD ONLY is abandoned (its completion_event fires)
        - TAIL entries are requeued at the front of _message_queue in
          FIFO order, with their original ``_QueuedTurn`` (prompt +
          completion_event) intact for replay after force_restart
        - Tail completion_events stay UNSET — they fire only when the
          rerun actually completes
        """
        from pinky_daemon import tmux_session as _ts
        monkeypatch.setattr(_ts, "_TURN_DONE_TIMEOUT_SEC", 0.05)
        monkeypatch.setattr(_ts, "_WATCHDOG_TICK_SEC", 0.02)

        ss, _ = _make_session()
        await ss.connect()
        # Cancel the worker so the test isolates the watchdog's
        # drain+requeue behavior. In production, force_restart's
        # disconnect cancels the worker BEFORE it can re-pull the
        # requeued turns from _message_queue; without cancelling here
        # the worker would immediately re-dispatch B/C back into the
        # deque, masking the requeue we want to assert.
        ss._worker_task.cancel()
        try:
            await ss._worker_task
        except asyncio.CancelledError:
            pass

        # Stub force_restart so we don't actually drive the full
        # disconnect→reconnect cycle.
        force_called = asyncio.Event()
        async def _stub_force_restart():
            force_called.set()
            return True
        ss.force_restart = _stub_force_restart

        # Seed three in-flight entries (A=internal-with-event,
        # B=external, C=external-with-event). Distinct prompts so we
        # can assert replay order. ``_seed_inflight`` synthesizes the
        # _QueuedTurn so the requeue has something to push.
        completion_a = asyncio.Event()
        completion_c = asyncio.Event()
        _seed_inflight(ss, internal=True, completion_event=completion_a, prompt="A")
        _seed_inflight(
            ss,
            meta={"platform": "telegram", "chat_id": "B", "message_id": "mB"},
            prompt="B",
        )
        _seed_inflight(
            ss,
            meta={"platform": "telegram", "chat_id": "C", "message_id": "mC"},
            completion_event=completion_c,
            prompt="C",
        )
        # Force the head clock to look ancient so the watchdog trips
        # immediately on its next tick.
        ss._head_started_at = 0.0
        assert len(ss._inflight_metas) == 3
        # Sanity: queue is empty pre-timeout.
        assert ss._message_queue.qsize() == 0

        try:
            await asyncio.wait_for(force_called.wait(), timeout=2.0)
        except asyncio.TimeoutError:
            pytest.fail("watchdog must trigger force_restart when head ages out")

        # HEAD only fired its completion_event. Tail events stayed unset.
        assert completion_a.is_set(), "head's completion_event must fire (abandoned)"
        assert not completion_c.is_set(), (
            "tail entry's completion_event must NOT fire — it'll fire when "
            "the rerun actually completes after force_restart"
        )

        # Deque drained.
        assert len(ss._inflight_metas) == 0
        assert ss._head_started_at is None

        # B and C requeued at the front of _message_queue in FIFO order.
        assert ss._message_queue.qsize() == 2, (
            "tail entries B and C must be requeued for replay"
        )
        b_replay = ss._message_queue.get_nowait()
        c_replay = ss._message_queue.get_nowait()
        assert b_replay.prompt == "B", "B must be at the front (FIFO)"
        assert c_replay.prompt == "C", "C must follow B"
        # Replay carries the original completion_event so the eventual
        # rerun unblocks the right caller.
        assert c_replay.completion_event is completion_c

        # Stats updated.
        assert ss._stats.get("turn_timeouts", 0) == 1

        await ss.disconnect()

    @pytest.mark.asyncio
    async def test_watchdog_requeues_in_hand_turn_with_tail(self, monkeypatch):
        """Tail-requeue must also handle the worker's
        ``_inflight_turn`` (a turn pulled from the queue but not yet
        pasted — e.g. mid context-lock retry). It was earlier in
        original send-order than the deque tail; replay before the tail.
        """
        from pinky_daemon import tmux_session as _ts
        monkeypatch.setattr(_ts, "_TURN_DONE_TIMEOUT_SEC", 0.05)
        monkeypatch.setattr(_ts, "_WATCHDOG_TICK_SEC", 0.02)

        ss, _ = _make_session()
        await ss.connect()
        # Same worker-cancel guard as the sibling test above.
        ss._worker_task.cancel()
        try:
            await ss._worker_task
        except asyncio.CancelledError:
            pass
        force_called = asyncio.Event()
        async def _stub():
            force_called.set()
            return True
        ss.force_restart = _stub

        # Deque: [A (stuck head), B]
        _seed_inflight(ss, prompt="A", meta={"chat_id": "A"})
        _seed_inflight(ss, prompt="B", meta={"chat_id": "B"})
        # In-hand: a turn the worker had pulled but not yet pasted
        # (context-lock retry). Original send-order: came AFTER B.
        in_hand_turn = _QueuedTurn(prompt="F", platform="t", chat_id="F", message_id="mF")
        ss._inflight_turn = in_hand_turn
        ss._head_started_at = 0.0

        try:
            await asyncio.wait_for(force_called.wait(), timeout=2.0)
        except asyncio.TimeoutError:
            pytest.fail("watchdog must trigger force_restart")

        assert ss._inflight_turn is None, "in-hand turn cleared after requeue"
        # Replay order: in_hand FIRST (was earliest pending), then tail B.
        assert ss._message_queue.qsize() == 2
        first = ss._message_queue.get_nowait()
        second = ss._message_queue.get_nowait()
        assert first.prompt == "F", (
            "in-hand turn was earlier in send-order than tail — replay first"
        )
        assert second.prompt == "B"

        await ss.disconnect()

    @pytest.mark.asyncio
    async def test_head_clock_does_not_advance_on_subsequent_append(self):
        """Appending another entry behind an existing head must NOT
        reset the head clock — that would game the watchdog. The clock
        only resets on empty→nonempty append (new head) or on popleft
        with remaining entries (new head)."""
        ss, _ = _make_session_with_response_cb()
        ss._state_machine._state = SessionState.CONNECTED

        _seed_inflight(ss, meta={"chat_id": "A"})
        first_head_at = ss._head_started_at
        assert first_head_at is not None
        await asyncio.sleep(0.01)
        # Append second entry — head clock must NOT advance.
        _seed_inflight(ss, meta={"chat_id": "B"})
        assert ss._head_started_at == first_head_at, (
            "appending behind an existing head must not reset the head "
            "clock — the original head is what the watchdog ages"
        )
