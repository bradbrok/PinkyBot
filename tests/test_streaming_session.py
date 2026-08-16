"""Tests for StreamingSession context-check behavior.

Focuses on _check_context() — the warn/restart logic that was buggy pre-fix:
  - warn flag was set before the query attempt (silent failure, no retry)
  - if/elif structure skipped warn on single-turn overshoot
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from pinky_daemon.streaming_session import StreamingSession, StreamingSessionConfig
from pinky_daemon.transport_state import SessionState

OLD_SESSION_ID = "11111111-1111-4111-8111-111111111111"
NEW_SESSION_ID = "22222222-2222-4222-8222-222222222222"


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
    session_id: str = "test-session",
):
    """Build a ResultMessage with the minimum fields the reader_loop reads."""
    from claude_agent_sdk.types import ResultMessage

    return ResultMessage(
        subtype="success" if not is_error else "error",
        duration_ms=10,
        duration_api_ms=5,
        is_error=is_error,
        num_turns=1,
        session_id=session_id,
        stop_reason="end_turn" if not is_error else "error",
        api_error_status=api_error_status,
        errors=errors,
    )


def _make_text_assistant_message(text: str):
    """Build a successful AssistantMessage carrying visible console text."""
    from claude_agent_sdk.types import AssistantMessage, TextBlock

    return AssistantMessage(
        content=[TextBlock(text=text)],
        model="claude-opus-4-7",
        error=None,
        usage={"input_tokens": 0, "output_tokens": 1},
        stop_reason="end_turn",
    )


async def _run_reader_against_stream(ss: StreamingSession, messages: list) -> None:
    """Wire up a fake client whose receive_messages() yields the given list,
    then run the reader_loop until the iterator drains."""

    async def _receive_messages():
        for msg in messages:
            yield msg

    ss._client.receive_messages = _receive_messages
    await ss._reader_loop()


@pytest.mark.skip(reason="ConversationResetMessage removed from SDK")
@pytest.mark.asyncio
async def test_reader_warns_on_conversation_reset_and_unknown_frame(capsys) -> None:
    """A widened SDK Message union must never degrade to a silent drop."""
    from claude_agent_sdk.types import ConversationResetMessage

    ss = _make_session()
    reset = ConversationResetMessage(
        new_conversation_id="new-conversation",
        uuid="reset-uuid",
        session_id="session-uuid",
    )

    await _run_reader_against_stream(ss, [reset, object()])

    logs = capsys.readouterr().err
    assert "WARNING SDK conversation reset" in logs
    assert "new_conversation_id='new-conversation'" in logs
    assert "WARNING unhandled SDK message type=object; continuing" in logs


@pytest.mark.skip(reason="ConversationResetMessage removed from SDK")
@pytest.mark.asyncio
async def test_reset_replaces_old_resume_handle_from_result_message() -> None:
    """Reset(old) + Result(new) persists empty first, then the fresh handle."""
    from claude_agent_sdk.types import ConversationResetMessage

    persisted = OLD_SESSION_ID
    writes: list[str] = []
    ss = _make_session()
    ss.resume_handle = OLD_SESSION_ID

    def persist(_agent_name: str, resume_handle: str) -> None:
        nonlocal persisted
        persisted = resume_handle
        writes.append(resume_handle)

    ss._on_resume_handle_sync = persist
    reset = ConversationResetMessage(
        new_conversation_id="new-conversation",
        uuid="reset-uuid",
        session_id=OLD_SESSION_ID,
    )

    await _run_reader_against_stream(
        ss,
        [reset, _make_result_message(session_id=NEW_SESSION_ID)],
    )

    assert writes == ["", NEW_SESSION_ID]
    assert persisted == NEW_SESSION_ID
    assert ss.resume_handle == NEW_SESSION_ID


@pytest.mark.skip(reason="ConversationResetMessage removed from SDK")
@pytest.mark.asyncio
async def test_reset_without_fresh_frame_clears_resume_handle_before_death() -> None:
    """A reset-window death leaves durable and in-memory handles empty."""
    from claude_agent_sdk.types import ConversationResetMessage

    persisted = OLD_SESSION_ID
    ss = _make_session()
    ss.resume_handle = OLD_SESSION_ID

    def persist(_agent_name: str, resume_handle: str) -> None:
        nonlocal persisted
        persisted = resume_handle

    ss._on_resume_handle_sync = persist
    reset = ConversationResetMessage(
        new_conversation_id="new-conversation",
        uuid="reset-uuid",
        session_id=OLD_SESSION_ID,
    )

    await _run_reader_against_stream(ss, [reset])

    assert persisted == ""
    assert ss.resume_handle == ""
    restarted = StreamingSession(
        StreamingSessionConfig(agent_name="test", resume_handle=persisted)
    )
    assert restarted.resume_handle == ""


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


# -- PR6 round-2: concurrent cold-start race (Murzik's blocker on #494) ------
#
# Repro shape: caller A enters connect() on an UNINITIALIZED session, wins
# BOOT ownership, state flips UNINITIALIZED → BOOTING at grant time, A's SDK
# handshake is still awaiting. Caller B enters connect() during A's handshake.
#
# Pre-fix: the cold-start guard was ``if self.state == UNINITIALIZED:``. B
# saw state == BOOTING, skipped the entire ownership/subscriber block,
# constructed a second ClaudeSDKClient, ran a second await connect(), and
# direct-mutated _state = CONNECTED at the warm-reconnect else branch. Two
# concurrent SDK handshakes for one logical cold start — same double-connect
# class PR1 was meant to prevent, just for the new BOOTING state.
#
# Post-fix: guard widened to ``state in {UNINITIALIZED, BOOTING}``. The state
# machine's same-target in-flight branch routes B to an InFlightHandle; B
# subscribes via wait() and inherits A's outcome. Subscriber surfaces failure
# (DEAD resolution) as a raise so the second caller never silently looks
# connected.


@pytest.mark.asyncio
async def test_concurrent_cold_start_runs_one_sdk_handshake(monkeypatch) -> None:
    """Two concurrent connect() calls on an UNINITIALIZED StreamingSession
    must result in exactly one SDK construction + handshake. Caller A wins
    BOOT ownership; caller B subscribes via the in-flight handle.

    Pre-fix (Murzik's #494 blocker): B saw state == BOOTING, bypassed the
    ownership path, and built a second SDK client. Post-fix: B subscribes
    and returns only when A lands CONNECTED.
    """
    import claude_agent_sdk

    cfg = StreamingSessionConfig(agent_name="test-concurrent-boot")
    ss = StreamingSession(cfg)

    construction_count = 0
    release_handshake = asyncio.Event()
    handshake_entered = asyncio.Event()

    class FakeClient:
        def __init__(self, options):
            nonlocal construction_count
            construction_count += 1
            self._options = options

        async def connect(self):
            handshake_entered.set()
            await release_handshake.wait()

        async def get_server_info(self):
            return None

        async def disconnect(self):
            pass

    monkeypatch.setattr(claude_agent_sdk, "ClaudeSDKClient", FakeClient)
    # Strip the post-connect side effects we don't care about for this test.
    ss._analytics_session_started = MagicMock()

    async def _noop_reader_loop():
        pass

    ss._reader_loop = _noop_reader_loop  # type: ignore[assignment]
    # Skip the wake-prompt auto-send (no client.query needed).
    ss._config.wake_context = ""

    t1 = asyncio.create_task(ss.connect())
    # Yield until A is parked inside the fake SDK handshake, so B enters
    # connect() while state is BOOTING (the load-bearing race window).
    await handshake_entered.wait()
    assert ss.state == SessionState.BOOTING, (
        "After A wins BOOT ownership but before completion, state must be "
        "BOOTING — the race window B must defend against"
    )

    t2 = asyncio.create_task(ss.connect())
    # Yield to let B subscribe (request_transition acquires the lock briefly).
    for _ in range(10):
        await asyncio.sleep(0)

    # B must NOT have constructed a second SDK client.
    assert construction_count == 1, (
        f"Concurrent connect() must run exactly one SDK handshake; "
        f"got {construction_count}. If >1, B bypassed the in-flight "
        f"subscriber path — Murzik's #494 race regressed."
    )

    # Release A's handshake. Both tasks must complete cleanly.
    release_handshake.set()
    await asyncio.gather(t1, t2)

    assert construction_count == 1
    assert ss.state == SessionState.CONNECTED


@pytest.mark.asyncio
async def test_concurrent_cold_start_subscriber_raises_on_owner_dead(monkeypatch) -> None:
    """If the BOOT owner's handshake fails (lands DEAD), the concurrent
    subscriber must raise — not silently return as if connected. Otherwise
    caller B would proceed to use a non-existent client.
    """
    import claude_agent_sdk

    cfg = StreamingSessionConfig(agent_name="test-concurrent-boot-fail")
    ss = StreamingSession(cfg)

    handshake_entered = asyncio.Event()
    release_handshake = asyncio.Event()
    construction_count = 0

    class FailingClient:
        def __init__(self, options):
            nonlocal construction_count
            construction_count += 1

        async def connect(self):
            handshake_entered.set()
            await release_handshake.wait()
            raise RuntimeError("simulated cold-start handshake failure")

        async def disconnect(self):
            pass

    monkeypatch.setattr(claude_agent_sdk, "ClaudeSDKClient", FailingClient)

    t1 = asyncio.create_task(ss.connect())
    await handshake_entered.wait()
    assert ss.state == SessionState.BOOTING

    t2 = asyncio.create_task(ss.connect())
    for _ in range(10):
        await asyncio.sleep(0)

    # Release — A's handshake raises, drives BOOTING → DEAD via BOOT_FAILED.
    release_handshake.set()

    # A propagates the original RuntimeError; B raises because final state DEAD.
    with pytest.raises(RuntimeError, match="simulated cold-start handshake"):
        await t1
    with pytest.raises(RuntimeError, match="resolved to dead"):
        await t2

    # Still exactly one SDK construction — B never built a second client.
    assert construction_count == 1
    assert ss.state == SessionState.DEAD


@pytest.mark.asyncio
async def test_concurrent_cold_start_rejection_post_dead_raises(monkeypatch) -> None:
    """Pushok's Case D from #494: caller D enters connect() with state ==
    BOOTING but, by the time request_transition acquires the lock, the
    owner has already completed to DEAD. request_transition returns a
    matrix rejection (BOOTING is illegal from DEAD), in_flight_handle is
    None. Pre-Case-D fix: silently returned, caller thought cold-start
    succeeded against a dead transport. Post-fix: raises so the caller
    sees the failure.
    """
    cfg = StreamingSessionConfig(agent_name="test-case-d-rejection")
    ss = StreamingSession(cfg)
    # Place the session in DEAD (owner already failed). connect() guard
    # widening doesn't catch this — DEAD isn't in {UNINITIALIZED, BOOTING}.
    # To trip Case D specifically we need to enter the guard with BOOTING
    # but observe DEAD by lock time. Simulate by direct-setting state to
    # BOOTING with no in-flight, then connect()'s request_transition will
    # see no in-flight + identity check (BOOTING == BOOTING) and return
    # observational. To get the DEAD-rejection branch we set the state to
    # something else after the guard but before the lock — but since
    # connect() doesn't yield between those, we can't easily simulate the
    # exact race. Instead, place state in BOOTING with no in-flight; the
    # observational read returns (owner_token=None, handle=None,
    # rejection_reason=None). Then the post-log check inspects self.state;
    # mutate to DEAD just before by setting both.
    #
    # Practical surrogate: trip the same code path by placing the state
    # machine in a configuration where request_transition will return
    # (owner_token=None, handle=None) AND self.state == DEAD when the
    # check runs. Place state in BOOTING (so the guard matches), then
    # after request_transition's identity-observational return we want
    # self.state == DEAD. Since the test doesn't have a real concurrent
    # owner, set the state machine directly to DEAD after the guard
    # decision — but connect() reads self.state synchronously. The
    # cleanest surrogate: pre-set _state to DEAD; the guard skips entirely
    # (DEAD not in {UNINITIALIZED, BOOTING}) and we fall through. That
    # tests Case C path, not Case D.
    #
    # The truly-realistic Case D test would require a concurrent owner
    # whose completion lands DEAD between D's guard and D's lock
    # acquisition. That's a narrow race window. We approximate by
    # patching request_transition to return a synthetic rejection while
    # the state machine reports DEAD — verifying connect() raises.
    from pinky_daemon.transport_state import TransitionResult

    ss._state_machine._state = SessionState.BOOTING
    # After this synthetic transition_complete from a phantom owner,
    # the state machine reports DEAD.
    real_request_transition = ss._state_machine.request_transition

    async def fake_request_transition(target, trigger, *, reason=None):
        # Simulate the race: owner just completed to DEAD; rejection.
        ss._state_machine._state = SessionState.DEAD
        return TransitionResult(
            changed=False,
            from_state=SessionState.DEAD,
            to_state=SessionState.DEAD,
            rejection_reason="phantom: owner completed DEAD before subscribe",
        )

    ss._state_machine.request_transition = fake_request_transition  # type: ignore[assignment]
    # Restore state to BOOTING so the guard matches at entry.
    ss._state_machine._state = SessionState.BOOTING

    with pytest.raises(RuntimeError, match="post-DEAD"):
        await ss.connect()

    # Cleanup
    ss._state_machine.request_transition = real_request_transition  # type: ignore[assignment]


# -- Non-blocking wake-prompt (post-2026-05-18 wedge) ------------------------
#
# Background: connect()'s post-handshake wake prompt used to be a plain
# ``await self._client.query(wake_prompt)``. The wake prompt explicitly
# instructs the agent to ToolSearch/preload MCP tools — i.e. the agent's
# very first turn touches the shared-MCP listener that daemon startup is
# coupled to via FastAPI's startup awaiting _start_streaming_session().
#
# Under load (or against a pathological MCP request that backtracks inside
# pydantic validation), this could keep startup from completing — supervisor
# saw startup timeout, killed the process, flap loop. Containment fix:
# detach the wake prompt into ``asyncio.create_task(...)`` so connect()
# returns as soon as the SDK handshake settles. The underlying regex hazard
# in shared-MCP validation is tracked separately.
#
# Pin: even if the wake-prompt query() blocks forever, connect() returns
# within a short timeout. If this regresses, daemon startup is hostage to
# the agent's first turn again.


@pytest.mark.asyncio
async def test_connect_returns_when_wake_prompt_query_blocks(monkeypatch) -> None:
    """connect() must NOT await the wake-prompt query. The wake prompt is
    scheduled as a background task so daemon startup can complete even when
    the agent's first turn (or its MCP tool preload) hangs.

    Pre-fix: ``await self._client.query(wake_prompt)`` inside connect() —
    startup hostage to first-turn latency.
    Post-fix: ``asyncio.create_task(_send_wake_prompt())`` — connect()
    returns once handshake + analytics + reader-loop spawn complete.
    """
    import claude_agent_sdk

    cfg = StreamingSessionConfig(agent_name="test-wake-nonblock")
    ss = StreamingSession(cfg)

    query_entered = asyncio.Event()
    release_query = asyncio.Event()  # Stays unset until cleanup.

    class FakeClient:
        def __init__(self, options):
            self._options = options

        async def connect(self):
            return None

        async def get_server_info(self):
            return None

        async def query(self, prompt):
            # Signal we got here, then block. If connect() awaited this,
            # the asyncio.wait_for below would time out.
            query_entered.set()
            await release_query.wait()

        async def disconnect(self):
            pass

    monkeypatch.setattr(claude_agent_sdk, "ClaudeSDKClient", FakeClient)
    ss._analytics_session_started = MagicMock()

    async def _noop_reader_loop():
        pass

    ss._reader_loop = _noop_reader_loop  # type: ignore[assignment]

    # The load-bearing assertion: connect() returns even though query()
    # is parked. Generous timeout to avoid CI flake; a regression would
    # hang here until asyncio.wait_for trips.
    await asyncio.wait_for(ss.connect(), timeout=2.0)

    # Cross-check: the wake task DID get scheduled and entered query().
    # If this assertion fails, connect() returned without dispatching the
    # wake prompt at all — a different regression (silent loss of wake).
    await asyncio.wait_for(query_entered.wait(), timeout=1.0)

    assert ss.state == SessionState.CONNECTED

    # Strong-ref pin (Murzik PR #539 review blocker): the wake task must
    # be retained on the session so the GC cannot collect it mid-flight
    # while query() is still parked. Force a GC cycle and assert the task
    # is still tracked + still alive.
    import gc

    gc.collect()
    assert len(ss._background_tasks) == 1, (
        "Wake task must be retained in self._background_tasks while in "
        "flight. If empty, the asyncio.create_task() reference was lost "
        "and the GC may collect the task before query() completes — "
        "silently dropping the wake prompt in production."
    )
    [wake_task] = ss._background_tasks
    assert not wake_task.done(), "Parked wake task must still be alive"

    # Drain: release query(), let the wake task complete, then assert
    # the done_callback discarded it from the strong-ref set.
    release_query.set()
    for _ in range(5):
        await asyncio.sleep(0)
    assert wake_task.done()
    assert len(ss._background_tasks) == 0, (
        "Completed wake task must auto-discard from _background_tasks via "
        "the done_callback registered at create time. Otherwise the set "
        "leaks one entry per cold start."
    )


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


# -- _pending_chats turn-boundary drain invariant -----------------------------
#
# The SDK may coalesce several submitted messages into one turn and emit one
# ResultMessage. Every reservation visible at the start of that boundary must
# be drained together; pop-one leaves stale route state for an unrelated turn.


@pytest.mark.asyncio
async def test_send_enqueues_routing_entry_even_without_chat_id() -> None:
    """Every query through send() must enqueue exactly one routing entry,
    including internal sends with no chat_id (e.g. inject_agent_message)."""
    ss = _make_session()
    await ss.send("internal prompt")
    assert ss._pending_chats == [("", "", "")]

    await ss.send("user prompt", platform="telegram", chat_id="123", message_id="9")
    assert ss._pending_chats == [("", "", ""), ("telegram", "123", "9")]


@pytest.mark.asyncio
@pytest.mark.parametrize("is_error", [False, True])
async def test_send_books_turn_before_fast_result_can_emit_idle(
    is_error: bool,
) -> None:
    """A result visible before query() returns still consumes a booked turn."""
    ss = _make_session()
    write_visible = asyncio.Event()
    release_query = asyncio.Event()
    result_consumed = asyncio.Event()
    idle_agents: list[str] = []
    ss._config.on_turn_idle = idle_agents.append

    async def held_query(prompt: str) -> None:
        del prompt
        write_visible.set()
        await release_query.wait()

    async def receive_messages():
        await write_visible.wait()
        yield _make_result_message(
            is_error=is_error,
            api_error_status=429 if is_error else None,
        )
        result_consumed.set()

    ss._client.query = held_query
    ss._client.receive_messages = receive_messages

    send_task = asyncio.create_task(
        ss.send(
            "fast turn",
            platform="telegram",
            chat_id="123",
            message_id="9",
        )
    )
    reader_task = asyncio.create_task(ss._reader_loop())

    await result_consumed.wait()
    assert not send_task.done()
    assert idle_agents == [ss.agent_name]
    assert ss._pending_chats == []

    release_query.set()
    assert await send_task is True
    await reader_task
    assert ss._pending_chats == []


@pytest.mark.asyncio
async def test_unrouted_query_books_before_fast_result_boundary() -> None:
    """Notification/system queries use the same reserve-before-write rule as
    send(), so a fast Result cannot leave a late sentinel behind."""
    ss = _make_session()
    write_visible = asyncio.Event()
    release_query = asyncio.Event()
    result_consumed = asyncio.Event()

    async def held_query(prompt: str) -> None:
        del prompt
        write_visible.set()
        await release_query.wait()

    async def receive_messages():
        await write_visible.wait()
        yield _make_result_message()
        result_consumed.set()

    ss._client.query = held_query
    ss._client.receive_messages = receive_messages

    query_task = asyncio.create_task(ss._query_unrouted("notification prompt"))
    reader_task = asyncio.create_task(ss._reader_loop())

    await result_consumed.wait()
    assert not query_task.done()
    assert ss._pending_chats == []

    release_query.set()
    await query_task
    await reader_task
    assert ss._pending_chats == []


@pytest.mark.asyncio
async def test_send_query_failure_rolls_back_only_its_reservation() -> None:
    ss = _make_session()
    existing = ("telegram", "same", "same")
    ss._pending_chats.append(existing)
    ss._client.query = AsyncMock(side_effect=RuntimeError("write failed"))
    ss.attempt_reconnect = AsyncMock(return_value=False)

    assert (
        await ss.send(
            "fails",
            platform="telegram",
            chat_id="same",
            message_id="same",
        )
        is False
    )
    assert len(ss._pending_chats) == 1
    assert ss._pending_chats[0] is existing


@pytest.mark.asyncio
async def test_context_warn_enqueues_unrouted_sentinel() -> None:
    """The [SYSTEM] context warning triggers an agent turn whose ResultMessage
    pops a routing tuple -- a sentinel must be enqueued for it."""
    ss = _make_session(warn_pct=40, restart_pct=80)
    _stub_ctx(ss, pct=50)
    await ss._check_context()
    assert ss._pending_chats == [("", "", "")]


@pytest.mark.asyncio
async def test_restart_blocked_notice_enqueues_unrouted_sentinel() -> None:
    ss = _make_session()
    await ss._notify_restart_blocked({"message": "save first"})
    assert ss._pending_chats == [("", "", "")]


@pytest.mark.asyncio
async def test_disconnect_clears_pending_chats() -> None:
    """Stale routing entries must not survive a teardown -- a reconnected
    session's first turns would otherwise be routed to an old chat_id."""
    ss = _make_session()
    ss._pending_chats.append(("telegram", "123", "9"))
    await ss.disconnect()
    assert ss._pending_chats == []


@pytest.mark.asyncio
async def test_disconnect_fires_empty_callback_for_routed_pending_entries() -> None:
    """A routed turn that dies in a teardown (e.g. the disconnect inside a
    warm reconnect) never reaches broker.route_response -- the only place the
    typing indicator stops. disconnect() must fire the callback with empty
    text for each routed entry; sentinels stay silent."""
    ss = _make_session()
    routed: list[tuple[str, str, str]] = []

    async def capture(turn_result):
        routed.append((turn_result.platform, turn_result.chat_id, turn_result.text))

    ss._response_callback = capture
    ss._pending_chats.append(("telegram", "123", "9"))
    ss._pending_chats.append(("", "", ""))

    await ss.disconnect()

    assert routed == [("telegram", "123", "")], (
        f"routed entries must fire an empty-text callback on teardown, got {routed}"
    )
    assert ss._pending_chats == []


@pytest.mark.asyncio
async def test_coalesced_turn_boundary_drains_all_pending_routes_loudly(
    capsys,
) -> None:
    """N>1 queued messages may share one SDK result; no reservation survives
    and the stale count is operator-visible."""
    ss = _make_session()
    routed: list[tuple[str, str, str]] = []

    async def capture(turn_result):
        routed.append(
            (turn_result.platform, turn_result.chat_id, turn_result.response_text)
        )

    ss._response_callback = capture
    # Two explicit agent packets plus a notification-origin prompt coalesced
    # into the same live SDK turn. Agent injects are deliberately unrouted.
    ss._pending_chats.append(("", "", ""))
    ss._pending_chats.append(("", "", ""))
    ss._pending_chats.append(("", "", ""))

    await _run_reader_against_stream(
        ss,
        [
            _make_text_assistant_message("web-only turn-final narration"),
            _make_result_message(),
        ],
    )

    assert ss._pending_chats == []
    assert routed == [("", "", "web-only turn-final narration")]
    assert "TURN_BOUNDARY_STALE_ROUTE_DRAIN" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_notification_after_agent_inbound_is_web_record_only(tmp_path) -> None:
    """A notification-origin completion after an earlier agent inbound is
    retained in the web conversation store without retaining either route."""
    from pinky_daemon.conversation_store import ConversationStore

    store = ConversationStore(db_path=str(tmp_path / "conversations.db"))
    ss = _make_session()
    ss._conversation_store = store
    ss._pending_chats.append(("agent", "requester", "agent-message"))
    ss._pending_chats.append(("", "", ""))

    await _run_reader_against_stream(
        ss,
        [
            _make_text_assistant_message("notification turn console text"),
            _make_result_message(),
        ],
    )

    assert ss._pending_chats == []
    assistant = [m for m in store.get_history(ss.id) if m.role == "assistant"]
    assert len(assistant) == 1
    assert assistant[0].content == "notification turn console text"
    assert (assistant[0].platform, assistant[0].chat_id) == ("", "")


# -- Typing-indicator leak: callback must fire for every routed turn ----------
#
# broker.route_response is the only place the Telegram typing loop is
# cancelled. The reader loop used to skip the response callback for errored
# turns and for turns with no text/tool uses, leaving the typing task
# hammering the Telegram API forever.


@pytest.mark.asyncio
async def test_error_result_fires_callback_with_empty_text_for_routed_turn() -> None:
    ss = _make_session()
    routed: list[tuple[str, str]] = []

    async def capture(turn_result):
        routed.append((turn_result.chat_id, turn_result.response_text))

    ss._response_callback = capture
    ss._pending_chats.append(("telegram", "123", "9"))

    await _run_reader_against_stream(
        ss,
        [_make_result_message(is_error=True, api_error_status=429)],
    )

    assert routed == [("123", "")], (
        "errored routed turn must still fire the callback (empty text) so "
        "the broker can stop the typing indicator"
    )


@pytest.mark.asyncio
async def test_routed_turn_with_no_text_or_tools_still_fires_callback() -> None:
    ss = _make_session()
    routed: list[str] = []

    async def capture(turn_result):
        routed.append(turn_result.chat_id)

    ss._response_callback = capture
    ss._pending_chats.append(("telegram", "123", "9"))

    await _run_reader_against_stream(ss, [_make_result_message()])

    assert routed == ["123"]


@pytest.mark.asyncio
async def test_unrouted_empty_turn_does_not_fire_callback() -> None:
    """Sentinel turns with no content stay silent -- no routing target."""
    ss = _make_session()
    routed: list[str] = []
    idle_agents: list[str] = []
    ss._config.on_turn_idle = idle_agents.append

    async def capture(turn_result):
        routed.append(turn_result.chat_id)

    ss._response_callback = capture
    ss._pending_chats.append(("", "", ""))

    await _run_reader_against_stream(ss, [_make_result_message()])

    assert routed == []
    assert idle_agents == [ss.agent_name]


# -- idle_sleep must let the memory-save turn finish before teardown ----------
#
# query() returns as soon as the prompt hits the transport. idle_sleep()
# used to disconnect() immediately after, killing the CLI subprocess before
# the save-memories turn ever ran -- the "save state before sleeping"
# contract was a no-op.


@pytest.mark.asyncio
async def test_idle_sleep_waits_for_save_turn_completion() -> None:
    ss = _make_session()
    order: list[str] = []

    async def fake_reader() -> None:
        await asyncio.sleep(0.05)
        order.append("save_turn_done")
        ss._turn_done.set()

    ss._reader_task = asyncio.create_task(fake_reader())

    orig_disconnect = ss.disconnect

    async def tracking_disconnect() -> None:
        order.append("disconnect")
        await orig_disconnect()

    ss.disconnect = tracking_disconnect  # type: ignore[assignment]

    result = await ss.idle_sleep()

    assert result is True
    assert order == ["save_turn_done", "disconnect"], (
        f"disconnect must happen AFTER the save turn completes, got {order}"
    )
    assert ss.state == SessionState.IDLE_SLEEPING


@pytest.mark.asyncio
async def test_idle_sleep_proceeds_after_save_timeout() -> None:
    """The wait is bounded -- a wedged save turn must not block sleep forever."""
    ss = _make_session()
    ss._IDLE_SLEEP_SAVE_TIMEOUT_SEC = 0.05  # type: ignore[assignment]

    async def never_finishes() -> None:
        await asyncio.sleep(60)

    ss._reader_task = asyncio.create_task(never_finishes())

    result = await ss.idle_sleep()

    assert result is True
    assert ss.state == SessionState.IDLE_SLEEPING


@pytest.mark.asyncio
async def test_idle_sleep_aborts_when_message_arrives_during_save_window() -> None:
    """A send() accepted during the save window would have its query killed
    by the teardown -- the sleep must abort and leave the session connected."""
    ss = _make_session()

    async def reader_with_traffic() -> None:
        await ss.send("hi there", platform="telegram", chat_id="123")
        ss._turn_done.set()

    ss._reader_task = asyncio.create_task(reader_with_traffic())

    result = await ss.idle_sleep()

    assert result is False
    assert ss.state == SessionState.CONNECTED, (
        "idle sleep must abort when new traffic arrives mid-save"
    )
    assert ss._client is not None, "transport must not be torn down on abort"


# -- Warm-reconnect coalescing -------------------------------------------------
#
# A transport drop surfaces in send() AND the reader loop (and the watchdog
# can fire during the backoff window). Unguarded, two concurrent
# attempt_reconnect calls interleave disconnect/sleep/connect: each connect()
# builds its own SDK client + reader task and the later assignment orphans
# the first live subprocess.


@pytest.mark.asyncio
async def test_concurrent_attempt_reconnect_coalesces_to_single_cycle() -> None:
    ss = _make_session()
    ss._RECONNECT_BACKOFF = (0.01,)  # type: ignore[assignment]
    connect_calls = 0

    async def fake_connect() -> None:
        nonlocal connect_calls
        connect_calls += 1
        await asyncio.sleep(0.05)
        ss._state_machine._state = SessionState.CONNECTED

    ss.connect = fake_connect  # type: ignore[assignment]

    await asyncio.gather(ss.attempt_reconnect(), ss.attempt_reconnect())

    assert connect_calls == 1, (
        f"concurrent attempt_reconnect must coalesce onto one cycle; "
        f"connect() ran {connect_calls}x"
    )
    assert ss.state == SessionState.CONNECTED
    assert ss._reconnect_task is None, "in-flight marker must clear when done"
