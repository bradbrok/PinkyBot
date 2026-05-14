"""Tests for StreamingSession context-check behavior.

Focuses on _check_context() — the warn/restart logic that was buggy pre-fix:
  - warn flag was set before the query attempt (silent failure, no retry)
  - if/elif structure skipped warn on single-turn overshoot
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from pinky_daemon.streaming_session import StreamingSession, StreamingSessionConfig
from pinky_daemon.transport_state import SessionState


def _make_session(
    *,
    warn_pct: int = 40,
    restart_pct: int = 80,
    state: SessionState = SessionState.CONNECTED,
) -> StreamingSession:
    cfg = StreamingSessionConfig(
        agent_name="test",
        context_warn_pct=warn_pct,
        context_restart_pct=restart_pct,
    )
    ss = StreamingSession(cfg)
    # PR3 (#486 sequence): is_connected derives from state machine state.
    # Tests that need a connected session bypass the lock + side effects and
    # set ``_state`` directly — production code routes through the state
    # machine, but unit tests of internal behavior don't need the
    # request_transition/transition_complete dance.
    ss._state_machine._state = state
    ss._client = MagicMock()
    ss._client.query = AsyncMock()
    # Stub force_restart so tests don't need a real connect loop
    ss.force_restart = AsyncMock(return_value=True)
    return ss


def _stub_ctx(ss: StreamingSession, pct: int, *, max_tokens: int = 200_000) -> None:
    total = int(max_tokens * pct / 100)
    ss._client.get_context_usage = AsyncMock(
        return_value={"totalTokens": total, "maxTokens": max_tokens}
    )


@pytest.mark.asyncio
async def test_warn_fires_once_at_warn_threshold() -> None:
    ss = _make_session(warn_pct=40, restart_pct=80)
    _stub_ctx(ss, pct=50)

    await ss._check_context()

    assert ss._client.query.await_count == 1
    assert ss._context_warned is True
    ss.force_restart.assert_not_awaited()

    # Second check at same pct: warn should NOT re-fire
    await ss._check_context()
    assert ss._client.query.await_count == 1


@pytest.mark.asyncio
async def test_warn_flag_only_set_on_successful_query() -> None:
    """If query() fails, the warn flag must stay False so next turn retries."""
    ss = _make_session(warn_pct=40, restart_pct=80)
    _stub_ctx(ss, pct=50)
    ss._client.query = AsyncMock(side_effect=RuntimeError("transport down"))

    await ss._check_context()

    assert ss._context_warned is False, "Flag must not be set when query fails"
    assert ss._client.query.await_count == 1

    # Transport recovers: next check should retry warn and succeed
    ss._client.query = AsyncMock()
    await ss._check_context()

    assert ss._context_warned is True
    assert ss._client.query.await_count == 1


@pytest.mark.asyncio
async def test_single_turn_overshoot_fires_both_warn_and_restart() -> None:
    """When pct jumps past restart threshold without ever being between warn and restart
    (e.g. a big tool result), the old if/elif skipped warn entirely. Fixed: both fire.
    """
    ss = _make_session(warn_pct=40, restart_pct=80)
    _stub_ctx(ss, pct=85)

    await ss._check_context()

    assert ss._client.query.await_count == 1, "Warn must still fire on overshoot"
    assert ss._context_warned is True
    ss.force_restart.assert_awaited_once()


@pytest.mark.asyncio
async def test_below_warn_threshold_no_action() -> None:
    ss = _make_session(warn_pct=40, restart_pct=80)
    _stub_ctx(ss, pct=30)

    await ss._check_context()

    ss._client.query.assert_not_awaited()
    ss.force_restart.assert_not_awaited()
    assert ss._context_warned is False


@pytest.mark.asyncio
async def test_restart_alone_when_already_warned() -> None:
    """If we already warned earlier in this session, crossing restart threshold
    should just restart — no redundant warn query."""
    ss = _make_session(warn_pct=40, restart_pct=80)
    ss._context_warned = True
    _stub_ctx(ss, pct=85)

    await ss._check_context()

    ss._client.query.assert_not_awaited()
    ss.force_restart.assert_awaited_once()


# -- idle_sleep / IDLE_SLEEPING state (#348) ----------------------------------
#
# The watchdog resurrection path (api._heartbeat_resurrect) used to fight
# idle_sleep() because there was no way to distinguish a deliberate sleep from
# an error disconnect. These tests pin down the contract via state:
#   - idle_sleep() drives state → IDLE_SLEEPING
#   - successful connect() drives state → CONNECTED (genuine wake)
#   - plain disconnect() does NOT land in IDLE_SLEEPING (only idle_sleep does)


@pytest.mark.asyncio
async def test_idle_sleep_lands_in_idle_sleeping() -> None:
    """After idle_sleep(), state must be IDLE_SLEEPING so the watchdog
    resurrection callback knows to leave the session alone."""
    ss = _make_session()
    # Replace force_restart stub — we need disconnect() to actually run, which
    # _make_session leaves intact. idle_sleep() doesn't call force_restart.
    assert ss.state != SessionState.IDLE_SLEEPING, "Default state must not be IDLE_SLEEPING"

    result = await ss.idle_sleep()

    assert result is True
    assert ss.state == SessionState.IDLE_SLEEPING
    assert ss.stats["idle_sleeping"] is True


@pytest.mark.asyncio
async def test_connect_drives_idle_sleeping_to_connected() -> None:
    """A successful connect() from IDLE_SLEEPING (genuine wake) must drive
    state to CONNECTED in a single state-machine settle."""
    ss = _make_session(state=SessionState.IDLE_SLEEPING)
    assert ss.state == SessionState.IDLE_SLEEPING

    # Patch SDK connect path: stub the line where connect() drives state to
    # CONNECTED. Tests don't run the real SDK.
    async def fake_connect() -> None:
        ss._state_machine._state = SessionState.CONNECTED

    ss.connect = fake_connect  # type: ignore[assignment]
    await ss.connect()

    assert ss.state == SessionState.CONNECTED


@pytest.mark.asyncio
async def test_disconnect_alone_does_not_land_in_idle_sleeping() -> None:
    """Plain disconnect() (e.g. error path, force_restart) must NOT land in
    IDLE_SLEEPING — only idle_sleep() drives that state. This is what
    keeps the watchdog resurrection working for genuine failures."""
    ss = _make_session()
    assert ss.state != SessionState.IDLE_SLEEPING

    await ss.disconnect()

    assert ss.state != SessionState.CONNECTED
    assert ss.state != SessionState.IDLE_SLEEPING, (
        "disconnect() must not land in IDLE_SLEEPING — only idle_sleep() does"
    )


@pytest.mark.asyncio
async def test_idle_sleep_returns_false_when_already_disconnected() -> None:
    """idle_sleep() bails early if already disconnected and must not drive
    state into IDLE_SLEEPING in that case (no transition occurred)."""
    ss = _make_session(state=SessionState.DEAD)
    ss._client = None

    result = await ss.idle_sleep()

    assert result is False
    assert ss.state == SessionState.DEAD


# -- Auth-failure dedupe across AssistantMessage + ResultMessage paths --------
#
# A single failed turn can surface auth errors on BOTH paths the reader_loop
# watches: an AssistantMessage with error="authentication_failed", followed
# by the terminal ResultMessage with api_error_status=401. Without dedupe,
# AuthFailureTracker.record_failure() would increment twice for one real
# failure — tripping the operator-alert threshold early and skewing the
# host-wide multi-agent baseline. Reader_loop must collapse the two-path
# emission into a single auth-alert-callback invocation per turn.
#
# Per Murzik's PR #404 review: "I verified locally with a synthetic reader
# stream: callback fired as [('agent', 'authentication_failed'), ('agent',
# 'api_error_status=401')]." This test pins down the fix.


def _make_assistant_message(*, error: str | None = None):
    """Build an AssistantMessage with the minimum fields the reader_loop reads."""
    from claude_agent_sdk.types import AssistantMessage

    return AssistantMessage(
        content=[],
        model="claude-opus-4-7",
        error=error,
        usage={"input_tokens": 0, "output_tokens": 0},
        stop_reason="error" if error else "end_turn",
    )


def _make_result_message(
    *,
    is_error: bool = False,
    api_error_status: int | None = None,
    errors: list[str] | None = None,
):
    """Build a ResultMessage with the minimum fields the reader_loop reads."""
    from claude_agent_sdk.types import ResultMessage

    return ResultMessage(
        subtype="success" if not is_error else "error",
        duration_ms=10,
        duration_api_ms=5,
        is_error=is_error,
        num_turns=1,
        session_id="test-session",
        stop_reason="end_turn" if not is_error else "error",
        api_error_status=api_error_status,
        errors=errors,
    )


async def _run_reader_against_stream(ss: StreamingSession, messages: list) -> None:
    """Wire up a fake client whose receive_messages() yields the given list,
    then run the reader_loop until the iterator drains."""

    async def _receive_messages():
        for msg in messages:
            yield msg

    ss._client.receive_messages = _receive_messages
    await ss._reader_loop()


@pytest.mark.asyncio
async def test_auth_callback_fires_once_when_both_paths_signal_for_same_turn() -> None:
    """Regression for PR #404 / Murzik's review.

    SDK emits AssistantMessage(error="authentication_failed") followed by
    ResultMessage(is_error=True, api_error_status=401) for a single failed
    turn. The auth-alert callback must fire exactly once — not once per path."""
    ss = _make_session()
    callback = AsyncMock()
    ss._auth_alert_callback = callback

    await _run_reader_against_stream(
        ss,
        [
            _make_assistant_message(error="authentication_failed"),
            _make_result_message(
                is_error=True,
                api_error_status=401,
                errors=["Invalid API key"],
            ),
        ],
    )

    assert callback.await_count == 1, (
        f"Expected one callback per failed turn (dedupe across "
        f"AssistantMessage + ResultMessage paths); got {callback.await_count}: "
        f"{callback.await_args_list}"
    )
    # The AssistantMessage path should win (it fires first in the stream).
    args, _ = callback.await_args
    assert args == (ss.agent_name, "authentication_failed")


@pytest.mark.asyncio
async def test_auth_callback_fires_on_result_path_when_assistant_path_silent() -> None:
    """If the failed turn surfaces only at the ResultMessage (no
    AssistantMessage error mid-turn), the result-path detection must still
    fire the alert. Dedupe must not break this case."""
    ss = _make_session()
    callback = AsyncMock()
    ss._auth_alert_callback = callback

    await _run_reader_against_stream(
        ss,
        [
            _make_result_message(
                is_error=True,
                api_error_status=403,
                errors=["Forbidden"],
            ),
        ],
    )

    assert callback.await_count == 1
    args, _ = callback.await_args
    assert args[0] == ss.agent_name
    assert "api_error_status=403" in args[1]
    # Enriched detail string should include msg.errors for triage context.
    assert "Forbidden" in args[1]


@pytest.mark.asyncio
async def test_auth_dedupe_resets_between_turns() -> None:
    """Two separate failed turns in one session must produce two callback
    invocations — dedupe is per-turn, not per-session."""
    ss = _make_session()
    callback = AsyncMock()
    ss._auth_alert_callback = callback

    await _run_reader_against_stream(
        ss,
        [
            # Turn 1: AssistantMessage auth + ResultMessage 401 — one alert
            _make_assistant_message(error="authentication_failed"),
            _make_result_message(is_error=True, api_error_status=401),
            # Turn 2: ResultMessage 401 only — one more alert
            _make_result_message(is_error=True, api_error_status=401),
        ],
    )

    assert callback.await_count == 2, (
        f"Expected two callbacks (one per failed turn); got {callback.await_count}"
    )


# -- Transport protocol adoption (#486 PR3+PR4) -------------------------------
#
# PR3 replaced the implicit (is_connected, is_idle_sleeping) two-bool inference
# with a SessionState enum routed through an embedded StateMachine. PR4 deleted
# the bool shims and migrated all external readers to consult ``state``
# directly. The tests below pin down:
#   - StreamingSession structurally satisfies the Transport Protocol
#   - state starts UNINITIALIZED
#   - lifecycle methods leave the state machine in the expected SessionState
#   - the stats dict exposes the explicit `state` value


def test_streaming_session_satisfies_transport_protocol() -> None:
    """StreamingSession structurally implements the Transport Protocol from
    src/pinky_daemon/transport.py. Runtime isinstance check is a smoke test;
    the real enforcement is type-check time via the Protocol declaration."""
    from pinky_daemon.transport import Transport

    cfg = StreamingSessionConfig(agent_name="test")
    ss = StreamingSession(cfg)
    assert isinstance(ss, Transport), (
        "StreamingSession must structurally satisfy the Transport Protocol — "
        "missing attributes or properties would surface here"
    )


def test_state_starts_uninitialized() -> None:
    """Fresh StreamingSession starts UNINITIALIZED — distinguishes 'never
    tried to connect' from 'tried and DEAD' per transport_state.py."""
    cfg = StreamingSessionConfig(agent_name="test")
    ss = StreamingSession(cfg)
    assert ss.state == SessionState.UNINITIALIZED


@pytest.mark.asyncio
async def test_idle_sleep_drives_state_to_idle_sleeping() -> None:
    """idle_sleep() must leave the state machine in IDLE_SLEEPING (not DEAD).
    Critical for the #348 watchdog-resurrection-skip contract."""
    ss = _make_session(state=SessionState.CONNECTED)
    result = await ss.idle_sleep()
    assert result is True
    assert ss.state == SessionState.IDLE_SLEEPING


@pytest.mark.asyncio
async def test_disconnect_from_connected_drives_state_to_dead() -> None:
    """Standalone disconnect() with no caller-declared intent drives
    CONNECTED → DEAD as terminal shutdown (matches pre-state-machine
    behavior for callers that just call disconnect())."""
    ss = _make_session(state=SessionState.CONNECTED)
    await ss.disconnect()
    assert ss.state == SessionState.DEAD


@pytest.mark.asyncio
async def test_disconnect_preserves_idle_sleeping_intent() -> None:
    """When idle_sleep() has already set state to IDLE_SLEEPING, the
    inner disconnect() call must NOT override it back to DEAD."""
    ss = _make_session(state=SessionState.CONNECTED)
    # Simulate idle_sleep's pre-disconnect state mutation directly so we
    # can isolate disconnect's behavior. (test_idle_sleep_drives_state_to_idle_sleeping
    # covers the integrated flow.)
    ss._state_machine._state = SessionState.IDLE_SLEEPING
    await ss.disconnect()
    assert ss.state == SessionState.IDLE_SLEEPING, (
        "disconnect() must respect prior IDLE_SLEEPING intent — only fires "
        "the DEAD fallback when called from CONNECTED"
    )


@pytest.mark.asyncio
async def test_disconnect_preserves_reconnecting_intent() -> None:
    """Symmetric to test_disconnect_preserves_idle_sleeping_intent. When
    force_restart() / attempt_reconnect() has already set state to
    RECONNECTING, the inner disconnect() call must NOT override it back
    to DEAD — otherwise observers see the macro-state flicker mid-restart
    (Bug 1 + Bug 2 from @pushok on PR #491 review).

    This pins the contract that would catch a regression of either bug
    even if force_restart() or attempt_reconnect() forgot to pre-assert
    RECONNECTING.
    """
    ss = _make_session(state=SessionState.CONNECTED)
    ss._state_machine._state = SessionState.RECONNECTING
    await ss.disconnect()
    assert ss.state == SessionState.RECONNECTING, (
        "disconnect() must respect prior RECONNECTING intent — only fires "
        "the DEAD fallback when called from CONNECTED"
    )


@pytest.mark.asyncio
async def test_attempt_reconnect_holds_reconnecting_through_partial_success_retry() -> None:
    """Regression for @pushok's PR #491 round-1 finding (Bug 2).

    Before the fix, ``attempt_reconnect()``'s retry-on-failure branch flickered
    state to DEAD between attempts. The scenario: ``connect()`` sets
    state=CONNECTED *before* its post-connect setup (analytics, reader-loop
    spawn); a raise during that setup leaves state=CONNECTED at the moment
    the retry except-block calls the inner ``disconnect()``, which fires the
    standalone-from-CONNECTED → DEAD fallback. After the inner disconnect
    runs the macro-state is DEAD instead of RECONNECTING — contradicts the
    "no flicker DEAD↔RECONNECTING" invariant (transport_state.py §5).

    The fix re-asserts RECONNECTING after the inner disconnect. This test
    pins it by observing state between the failed connect and the next
    backoff sleep — must see RECONNECTING.
    """
    cfg = StreamingSessionConfig(
        agent_name="test",
        context_warn_pct=40,
        context_restart_pct=80,
    )
    ss = StreamingSession(cfg)
    ss._state_machine._state = SessionState.CONNECTED
    # Skip the backoff sleeps entirely — we're testing state choreography.
    ss._RECONNECT_BACKOFF = (0, 0, 0)  # type: ignore[assignment]

    # CRITICAL: do NOT stub disconnect(). The real disconnect()'s
    # standalone-from-CONNECTED → DEAD fallback IS the bug path. We need
    # it to fire on each retry's inner-disconnect call so the test
    # actually exercises whether attempt_reconnect's reassert restores
    # RECONNECTING — without using the real fallback, the test would
    # green even with the reassert removed (per @murzik PR #491 round-2).
    # Real disconnect() is safe on an unconfigured session: _client,
    # _reader_task, _analytics_store all None → no-op side effects.
    connect_call_entry_states: list[SessionState] = []
    connect_calls = 0

    async def fake_connect() -> None:
        # Capture state AT ENTRY — this is the load-bearing observation.
        # On retry-after-failure iterations, if attempt_reconnect's
        # except-block reassert is missing, entry state will be DEAD
        # (left over from the inner disconnect's CONNECTED → DEAD
        # fallback). With the reassert, entry state stays RECONNECTING.
        nonlocal connect_calls
        connect_calls += 1
        connect_call_entry_states.append(ss.state)
        # Simulate the Bug 2 trajectory: flip CONNECTED first, then raise.
        # In production this is connect()'s post-handshake setup raising
        # (analytics open, reader_loop spawn, account-info fetch, etc.).
        ss._state_machine._state = SessionState.CONNECTED
        if connect_calls <= 2:
            raise RuntimeError(f"simulated post-handshake setup failure #{connect_calls}")

    ss.connect = fake_connect  # type: ignore[assignment]

    # Pre-condition: state CONNECTED (so the initial disconnect-fallback could
    # fire if attempt_reconnect didn't pre-assert RECONNECTING).
    await ss.attempt_reconnect()

    # 3 connect() calls: 2 raise (retries 1+2), 3rd succeeds.
    assert connect_calls == 3
    # Final state CONNECTED.
    assert ss.state == SessionState.CONNECTED
    # The load-bearing assertion: each connect() entry must see
    # RECONNECTING, never DEAD. If the reassert in attempt_reconnect's
    # except-block were removed, calls 2 and 3 would enter from DEAD
    # (after the real disconnect()'s CONNECTED → DEAD fallback fires
    # on the partial-success teardown). This pins the "no flicker
    # DEAD ↔ RECONNECTING between retries" invariant structurally.
    assert connect_call_entry_states == [
        SessionState.RECONNECTING,
        SessionState.RECONNECTING,
        SessionState.RECONNECTING,
    ], (
        f"connect() entry states must all be RECONNECTING, got "
        f"{connect_call_entry_states}. If DEAD appears in calls 2+, the "
        f"attempt_reconnect except-block reassert regressed."
    )


@pytest.mark.asyncio
async def test_force_restart_holds_reconnecting_across_disconnect_and_connect() -> None:
    """Regression for @murzik's PR #491 round-1 finding.

    Before the fix, ``force_restart()`` called ``disconnect()`` while state was
    still CONNECTED. ``disconnect()``'s no-prior-intent fallback then drove
    state to DEAD, and observers (broker auto-wake, watchdog resurrect) would
    see DEAD during the wake-context-refresh window and at ``connect()``
    entry — racing the in-flight force_restart.

    The fix sets RECONNECTING before disconnect and re-asserts after. This
    test pins the contract by observing state at three points: after
    ``disconnect()`` returns, inside the fake ``connect()`` (mid-restart),
    and after ``connect()`` settles.
    """
    cfg = StreamingSessionConfig(
        agent_name="test",
        context_warn_pct=40,
        context_restart_pct=80,
    )
    ss = StreamingSession(cfg)
    ss._state_machine._state = SessionState.CONNECTED
    ss._client = MagicMock()

    observed_states: list[SessionState] = []

    async def fake_disconnect() -> None:
        # disconnect must NOT settle in DEAD when force_restart pre-set
        # RECONNECTING. Capture state at the boundary.
        observed_states.append(("after_intent_before_disconnect", ss.state))
        # No-op teardown — we're testing the state choreography, not SDK.

    async def fake_connect() -> None:
        # connect must see RECONNECTING at entry, not DEAD.
        observed_states.append(("connect_entry", ss.state))
        ss._state_machine._state = SessionState.CONNECTED
        observed_states.append(("connect_exit", ss.state))

    ss.disconnect = fake_disconnect  # type: ignore[assignment]
    ss.connect = fake_connect  # type: ignore[assignment]

    restarted = await ss.force_restart()
    assert restarted is True

    # State must be RECONNECTING at every observation point between the
    # intent declaration and connect() settling.
    state_at_disconnect = dict(observed_states)["after_intent_before_disconnect"]
    state_at_connect_entry = dict(observed_states)["connect_entry"]
    assert state_at_disconnect == SessionState.RECONNECTING, (
        f"state at disconnect boundary must be RECONNECTING, got {state_at_disconnect}"
    )
    assert state_at_connect_entry == SessionState.RECONNECTING, (
        f"state at connect() entry must be RECONNECTING (not DEAD), "
        f"got {state_at_connect_entry}"
    )
    assert ss.state == SessionState.CONNECTED


def test_stats_exposes_state_alongside_legacy_bools() -> None:
    """The stats dict must include the explicit 5-state value AND the legacy
    bool shims, so dashboards / debug tools can read either while the four
    external readers migrate in PR4."""
    ss = _make_session(state=SessionState.CONNECTED)
    stats = ss.stats
    assert stats["state"] == "connected"
    assert stats["connected"] is True
    assert stats["idle_sleeping"] is False

    ss._state_machine._state = SessionState.IDLE_SLEEPING
    stats = ss.stats
    assert stats["state"] == "idle_sleeping"
    assert stats["connected"] is False
    assert stats["idle_sleeping"] is True


@pytest.mark.asyncio
async def test_billing_error_assistant_does_not_block_subsequent_auth_alert() -> None:
    """Edge case: AssistantMessage with error="billing_error" (NOT auth) on
    one turn must not set the dedupe flag, so a real auth failure on the
    NEXT turn still alerts."""
    ss = _make_session()
    callback = AsyncMock()
    ss._auth_alert_callback = callback

    await _run_reader_against_stream(
        ss,
        [
            # Turn 1: billing error — must not fire auth alert, must not
            # set the dedupe flag (it's not an auth error).
            _make_assistant_message(error="billing_error"),
            _make_result_message(is_error=True, api_error_status=402),
            # Turn 2: real auth failure — must alert.
            _make_assistant_message(error="authentication_failed"),
            _make_result_message(is_error=True, api_error_status=401),
        ],
    )

    assert callback.await_count == 1
    args, _ = callback.await_args
    assert args == (ss.agent_name, "authentication_failed")
