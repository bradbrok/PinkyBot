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
from unittest.mock import AsyncMock, MagicMock

import pytest

from pinky_daemon.streaming_session import StreamingSessionConfig
from pinky_daemon.tmux_session import (
    TmuxCommandResult,
    TmuxSession,
    _QueuedTurn,
    _TmuxControl,
)
from pinky_daemon.tmux_transcript import TurnResponse
from pinky_daemon.transport_state import SessionState, TransitionResult, Trigger


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
    tmux.capture_pane = AsyncMock(return_value=_ok())
    return tmux


def _make_session(
    *,
    agent_name: str = "dymok",
    state: SessionState | None = None,
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
    )
    tmux = tmux or _make_mock_tmux()
    ss = TmuxSession(cfg, tmux_control=tmux)
    if state is not None:
        ss._state_machine._state = state
    return ss, tmux


# ──────────────────────────────────────────────────────────────────────────
# Construction + identity
# ──────────────────────────────────────────────────────────────────────────


def test_default_initial_state_is_uninitialized() -> None:
    ss, _ = _make_session()
    assert ss.state == SessionState.UNINITIALIZED


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

    Fix: cold-start cmd must fall through to ``claude`` (no
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


# ──────────────────────────────────────────────────────────────────────────
# send + worker
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_send_drops_when_not_connected() -> None:
    """Per Transport contract, send() drops when not CONNECTED. Matches
    StreamingSession's legacy drop-silent behavior."""
    ss, tmux = _make_session(state=SessionState.DEAD)
    await ss.send("hello", platform="telegram", chat_id="123")
    tmux.send_keys.assert_not_awaited()
    assert ss._stats["messages_sent"] == 0


@pytest.mark.asyncio
async def test_send_queues_and_worker_delivers_via_send_keys() -> None:
    """Happy path: cold-start, send a message, worker dequeues and pushes
    via tmux send-keys. Pins the send_keys invocation shape (enter=True
    appends an Enter keypress, completing the prompt in claude's REPL)."""
    ss, tmux = _make_session()
    await ss.connect()
    await ss.send("hello world", platform="telegram", chat_id="123")

    # Let the worker drain one item.
    for _ in range(20):
        await asyncio.sleep(0)
        if tmux.send_keys.await_count >= 1:
            break

    tmux.send_keys.assert_awaited()
    # send_keys is called with the prompt + enter=True (default).
    args, kwargs = tmux.send_keys.call_args
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
                "auto_restarts", "state", "thinking_effort"):
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
async def test_deliver_turn_clears_meta_on_send_keys_failure() -> None:
    """If tmux send-keys fails, in-flight meta is cleared so a stale tail
    doesn't fire response_callback with bogus routing data."""
    tmux = _make_mock_tmux()
    tmux.send_keys = AsyncMock(return_value=_fail("rc=1"))
    ss, _ = _make_session(state=SessionState.CONNECTED, tmux=tmux)
    turn = _QueuedTurn(prompt="hi", platform="t", chat_id="c", message_id="m")
    with pytest.raises(RuntimeError, match="tmux send-keys failed"):
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
    response = TurnResponse(text="response text", stop_reason="end_turn")
    await ss._handle_turn_complete(response)
    conv.append.assert_called_once_with(ss.id, "assistant", "response text")


@pytest.mark.asyncio
async def test_handle_turn_complete_fires_stream_event() -> None:
    """stream_event_callback gets a turn_complete event with usage + duration."""
    events: list[dict] = []

    async def stream_cb(evt):
        events.append(evt)

    ss, _ = _make_session_with_response_cb(stream_evt=stream_cb)
    response = TurnResponse(
        text="x", stop_reason="end_turn",
        usage={"input_tokens": 100, "output_tokens": 50},
        duration_ms=1500,
        assistant_entry_count=2,
        tool_uses=[{"name": "Bash", "input": {}, "id": "t1"}],
    )
    await ss._handle_turn_complete(response)
    assert len(events) == 1
    evt = events[0]
    assert evt["agent_name"] == "dymok"
    assert evt["type"] == "turn_complete"
    assert evt["stop_reason"] == "end_turn"
    assert evt["duration_ms"] == 1500
    assert evt["assistant_entry_count"] == 2
    assert evt["tool_use_count"] == 1


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

    # Empty-text turn — pure tool-use refusal, max_tokens, etc.
    empty_response = TurnResponse(text="", stop_reason="max_tokens")
    await ss._handle_turn_complete(empty_response)

    # response_callback NOT fired (empty text), but turn_done IS set.
    assert cb.calls == []
    assert ss._turn_done.is_set(), (
        "turn_done must be set unconditionally so the worker can proceed"
    )


@pytest.mark.asyncio
async def test_deliver_turn_clears_turn_done_before_send_keys() -> None:
    """The clear must happen BEFORE send-keys so that any subsequent
    stop_hook_summary unambiguously belongs to THIS turn (not a stale
    pre-arm from a prior callback). Pinned via call-order observation.
    """
    tmux = _make_mock_tmux()
    cleared_at: list[bool] = []

    async def observing_send_keys(*args, **kwargs):
        # Snapshot turn_done state at the moment send_keys is called.
        cleared_at.append(not ss._turn_done.is_set())
        return _ok()

    tmux.send_keys = AsyncMock(side_effect=observing_send_keys)
    ss, _ = _make_session(state=SessionState.CONNECTED, tmux=tmux)
    # Pre-arm turn_done to a SET state to prove the clear() actually fires.
    ss._turn_done.set()

    turn = _QueuedTurn(prompt="hi", platform="t", chat_id="c", message_id="m")
    await ss._deliver_turn(turn)

    assert cleared_at == [True], (
        "turn_done must be CLEARED at the moment send_keys is invoked"
    )


@pytest.mark.asyncio
async def test_deliver_turn_send_keys_failure_re_arms_turn_done() -> None:
    """If send-keys fails, the worker would otherwise block forever on
    turn_done.wait() because no callback will ever fire for the failed
    dispatch. _deliver_turn must re-arm turn_done as part of its failure
    cleanup so the worker's next iteration starts in a clean state.
    """
    tmux = _make_mock_tmux()
    tmux.send_keys = AsyncMock(return_value=_fail("rc=1"))
    ss, _ = _make_session(state=SessionState.CONNECTED, tmux=tmux)
    ss._turn_done.clear()

    turn = _QueuedTurn(prompt="hi", platform="t", chat_id="c", message_id="m")
    with pytest.raises(RuntimeError):
        await ss._deliver_turn(turn)

    # Meta cleared AND turn_done re-armed.
    assert ss._inflight_meta == {}
    assert ss._turn_done.is_set()


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
    """Pushok's PR #496 round-1 critical Case 1 repro: two send() calls
    in quick succession must route response 1 to chat A and response 2
    to chat B. The worker's turn_done gate enforces this by blocking
    dispatch of turn 2 until turn 1's stop_hook_summary lands.

    This is the canonical regression test for the bug: pre-fix, the
    second send() would clobber _inflight_meta before turn 1's
    response_callback fired, routing response 1 to chat B."""
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

    # Worker should have dispatched turn 1 but be blocked on turn_done
    # for turn 1's completion. Verify by checking the queue still has
    # turn 2 (give the worker a tick to drain turn 1).
    for _ in range(20):
        await asyncio.sleep(0.01)
        if ss._inflight_meta.get("chat_id") == "A":
            break
    assert ss._inflight_meta == {
        "platform": "telegram", "chat_id": "A", "message_id": "mA",
    }, "worker should be holding turn A's meta while awaiting turn_done"
    assert ss._message_queue.qsize() == 1, (
        "turn B must still be queued — worker should not dispatch it "
        "until turn A's stop_hook_summary lands"
    )

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

    # Wait for response A to be delivered AND for the worker to dispatch
    # turn B.
    for _ in range(50):
        await asyncio.sleep(0.02)
        if cb.calls and ss._inflight_meta.get("chat_id") == "B":
            break

    # Critical: response A was routed to chat A, NOT chat B (the original bug).
    assert len(cb.calls) == 1
    result = cb.calls[0]
    assert result.response_text == "response A"
    assert result.chat_id == "A", (
        f"response A leaked to wrong chat: {result} — original Case 1 bug regression"
    )

    # Worker has now dispatched turn B (meta swapped).
    assert ss._inflight_meta["chat_id"] == "B"

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

    await ss.disconnect()


@pytest.mark.asyncio
async def test_worker_force_restarts_on_turn_done_timeout(monkeypatch) -> None:
    """Pushok's PR #496 round-1 Case 1 follow-up: when the model gets
    stuck and turn_done never fires, the worker times out and triggers
    force_restart instead of just clearing meta and continuing. The
    latter would re-introduce the original Case 1 cross-routing bug —
    a late-arriving stop_hook_summary for the stuck turn would route
    to the *next* turn's meta.
    """
    from pinky_daemon import tmux_session
    # Shorten the timeout so the test doesn't take 10 minutes.
    monkeypatch.setattr(tmux_session, "_TURN_DONE_TIMEOUT_SEC", 0.1)

    ss, tmux = _make_session()
    await ss.connect()

    # Track force_restart calls — replace with a stub that signals.
    force_restart_called = asyncio.Event()
    original_force_restart = ss.force_restart

    async def stub_force_restart():
        force_restart_called.set()
        # Call original to drive state machine through reconnect.
        return await original_force_restart()

    ss.force_restart = stub_force_restart

    # Send one prompt — worker dispatches and waits for turn_done.
    # No stop_hook_summary will ever be written, so timeout fires.
    await ss.send(prompt="stuck", platform="t", chat_id="c", message_id="m")

    # Wait for the timeout path to fire force_restart.
    try:
        await asyncio.wait_for(force_restart_called.wait(), timeout=2.0)
    except asyncio.TimeoutError:
        pytest.fail("force_restart should have been called after turn_done timeout")

    # Turn-timeout counter incremented.
    assert ss._stats.get("turn_timeouts", 0) == 1

    # Final cleanup — force_restart may still be running, give it a moment.
    await asyncio.sleep(0.05)
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
