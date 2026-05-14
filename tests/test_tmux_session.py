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
from unittest.mock import AsyncMock, MagicMock

import pytest

from pinky_daemon.streaming_session import StreamingSessionConfig
from pinky_daemon.tmux_session import (
    TmuxCommandResult,
    TmuxSession,
    _TmuxControl,
)
from pinky_daemon.transport_state import SessionState


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
async def test_cold_start_uses_correct_claude_invocation() -> None:
    """The in-pane command must be ``claude --continue
    --dangerously-skip-permissions`` per the design. Pinned because a
    typo in the invocation silently breaks billing semantics (would hit
    SDK credits instead of subscription)."""
    ss, tmux = _make_session()
    await ss.connect()
    _, kwargs = tmux.new_session.call_args
    cmd = kwargs["command"]
    assert "claude" in cmd
    assert "--continue" in cmd
    assert "--dangerously-skip-permissions" in cmd


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
