"""Tests for the Transport session state machine.

PR1 of the #486 sequence: pure data-structure tests. No streaming-session
involvement. If these pass, the state machine is correct as a standalone
primitive; PR2 (Transport protocol) and PR3 (StreamingSession adopts) build
on top.
"""

from __future__ import annotations

import asyncio

import pytest

from pinky_daemon.transport_state import (
    ILLEGAL_PAIR_REASONS,
    LEGAL_TRANSITIONS,
    InFlightHandle,
    SessionState,
    StateMachine,
    TransitionError,
    Trigger,
)


def _sm_in(label: str, state: SessionState) -> StateMachine:
    """Construct a StateMachine and place it in ``state`` directly.

    PR6 (Pushok) added a production guard requiring ``initial_state`` to be
    one of ``{UNINITIALIZED, DEAD}`` — the two states a fresh state machine
    can legitimately be constructed in. Tests need to start in arbitrary
    states without driving through a real handshake; this helper bypasses
    the guard by direct ``_state =`` mutation (same pattern existing tests
    already use for inline state changes elsewhere).

    Use this in tests that need a specific starting state for matrix-cell
    coverage, ownership semantics, identity reads, etc. Production code
    must NOT do this.
    """
    sm = StateMachine(label)
    sm._state = state
    return sm

# ──────────────────────────────────────────────────────────────────────────
# Construction & basic state reads
# ──────────────────────────────────────────────────────────────────────────


class TestConstruction:
    def test_default_initial_state_is_uninitialized(self):
        sm = StateMachine("agent")
        assert sm.state == SessionState.UNINITIALIZED

    def test_explicit_initial_state_honored(self):
        # PR6: production guard restricts initial_state to UNINITIALIZED
        # or DEAD; both are honored. Other states must raise.
        sm = _sm_in("agent", SessionState.DEAD)
        assert sm.state == SessionState.DEAD

    def test_initial_state_guard_rejects_non_birth_states(self):
        # PR6 invariant: ``StateMachine.__init__`` only accepts
        # UNINITIALIZED (cold-start) or DEAD (rehydrated from persisted
        # "session died" snapshot). Any other state would let callers
        # bypass the cold-start lifecycle invariants (UNINITIALIZED-has-
        # zero-incoming, BOOT-only-from-UNINITIALIZED). Hard guard rail.
        for invalid in (
            SessionState.BOOTING,
            SessionState.RECONNECTING,
            SessionState.CONNECTED,
            SessionState.IDLE_SLEEPING,
        ):
            with pytest.raises(ValueError, match="UNINITIALIZED.*DEAD"):
                StateMachine("agent", initial_state=invalid)


# ──────────────────────────────────────────────────────────────────────────
# Identity / observational reads
# ──────────────────────────────────────────────────────────────────────────


class TestIdentity:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("state", list(SessionState))
    async def test_same_state_is_observational(self, state):
        """``request_transition(current_state)`` returns ``changed=False`` and
        confers no ownership, regardless of trigger."""
        sm = _sm_in("agent", state)
        # Trigger is irrelevant for identity reads — pick BROKER as a placeholder.
        result = await sm.request_transition(state, Trigger.BROKER)
        assert result.changed is False
        assert result.from_state == state
        assert result.to_state == state
        assert result.owner_token is None
        assert result.in_flight_handle is None
        assert result.rejection_reason is None
        # State unchanged.
        assert sm.state == state


# ──────────────────────────────────────────────────────────────────────────
# Matrix coverage: every legal cell admits exactly its declared triggers
# ──────────────────────────────────────────────────────────────────────────


class TestMatrixLegalCells:
    @pytest.mark.asyncio
    async def test_every_legal_cell_accepts_each_declared_trigger(self):
        """For each (from, to) in LEGAL_TRANSITIONS, every declared trigger
        must produce ``changed=True`` and grant an OwnerToken."""
        for (from_state, to_state), triggers in LEGAL_TRANSITIONS.items():
            for trigger in triggers:
                sm = _sm_in("agent", from_state)
                result = await sm.request_transition(to_state, trigger)
                assert result.changed is True, (
                    f"legal cell ({from_state.value} → {to_state.value}, "
                    f"trigger={trigger.value}) did not change"
                )
                assert result.owner_token is not None, (
                    f"legal cell ({from_state.value} → {to_state.value}, "
                    f"trigger={trigger.value}) did not grant ownership"
                )
                assert result.from_state == from_state
                assert result.to_state == to_state
                assert sm.state == to_state

    @pytest.mark.asyncio
    async def test_legal_cell_rejects_unauthorized_triggers(self):
        """Triggers NOT in the legal set for a cell must be rejected with a
        reason. Tests the trigger enforcement column of the matrix."""
        for (from_state, to_state), triggers in LEGAL_TRANSITIONS.items():
            unauthorized = set(Trigger) - triggers
            for trigger in unauthorized:
                sm = _sm_in("agent", from_state)
                result = await sm.request_transition(to_state, trigger)
                assert result.changed is False, (
                    f"unauthorized trigger {trigger.value} drove "
                    f"{from_state.value} → {to_state.value}"
                )
                assert result.rejection_reason is not None
                assert "not authorized" in result.rejection_reason
                # State unchanged.
                assert sm.state == from_state


class TestMatrixIllegalCells:
    @pytest.mark.asyncio
    async def test_every_illegal_pair_rejects_with_reason(self):
        """For each illegal (from, to) pair, every trigger must reject with
        the matrix's documented reason."""
        for (from_state, to_state), expected_reason in ILLEGAL_PAIR_REASONS.items():
            # Sanity: illegal pairs must not appear in LEGAL_TRANSITIONS.
            assert (from_state, to_state) not in LEGAL_TRANSITIONS, (
                f"({from_state.value}, {to_state.value}) is in both legal "
                f"and illegal maps"
            )
            for trigger in Trigger:
                sm = _sm_in("agent", from_state)
                result = await sm.request_transition(to_state, trigger)
                assert result.changed is False
                assert result.rejection_reason == expected_reason, (
                    f"illegal cell ({from_state.value} → {to_state.value}, "
                    f"trigger={trigger.value}) rejected with wrong reason"
                )
                assert sm.state == from_state

    def test_matrix_is_exhaustive(self):
        """Every off-diagonal (from, to) pair must be either in LEGAL_TRANSITIONS
        or in ILLEGAL_PAIR_REASONS — no cell silently missing."""
        for from_state in SessionState:
            for to_state in SessionState:
                if from_state == to_state:
                    continue  # diagonal = identity, handled in request_transition
                in_legal = (from_state, to_state) in LEGAL_TRANSITIONS
                in_illegal = (from_state, to_state) in ILLEGAL_PAIR_REASONS
                assert in_legal or in_illegal, (
                    f"cell ({from_state.value} → {to_state.value}) is missing "
                    f"from both LEGAL_TRANSITIONS and ILLEGAL_PAIR_REASONS — "
                    f"every cell must be defended"
                )
                assert not (in_legal and in_illegal), (
                    f"cell ({from_state.value} → {to_state.value}) appears in "
                    f"both legal and illegal maps"
                )


# ──────────────────────────────────────────────────────────────────────────
# Ownership semantics: the bug-killer
# ──────────────────────────────────────────────────────────────────────────


class TestOwnership:
    @pytest.mark.asyncio
    async def test_concurrent_same_target_yields_exactly_one_owner(self):
        """Murzik's gating test: two coroutines call
        ``request_transition(RECONNECTING)`` simultaneously from IDLE_SLEEPING.
        Exactly one gets an OwnerToken; the other gets an InFlightHandle.

        IDLE_SLEEPING → RECONNECTING accepts both BROKER (auto-wake on inbound)
        and WATCHDOG (scheduled wake) triggers — the canonical production race.
        """
        sm = _sm_in("agent", SessionState.IDLE_SLEEPING)

        # Both callers race for ownership of IDLE_SLEEPING → RECONNECTING.
        results = await asyncio.gather(
            sm.request_transition(SessionState.RECONNECTING, Trigger.BROKER),
            sm.request_transition(SessionState.RECONNECTING, Trigger.WATCHDOG),
        )

        owners = [r for r in results if r.owner_token is not None]
        subscribers = [r for r in results if r.in_flight_handle is not None]

        assert len(owners) == 1, (
            f"expected exactly one owner; got {len(owners)}: {results}"
        )
        assert len(subscribers) == 1, (
            f"expected exactly one subscriber; got {len(subscribers)}: {results}"
        )
        # The owner observed the change; the subscriber did not.
        assert owners[0].changed is True
        assert subscribers[0].changed is False
        assert subscribers[0].in_flight_handle.target == SessionState.RECONNECTING

    @pytest.mark.asyncio
    async def test_subscriber_receives_final_state_when_owner_completes(self):
        """Subscribers ``await`` their in-flight handle and read the final
        state set by the owner."""
        sm = _sm_in("agent", SessionState.IDLE_SLEEPING)

        owner_result = await sm.request_transition(
            SessionState.RECONNECTING, Trigger.BROKER
        )
        subscriber_result = await sm.request_transition(
            SessionState.RECONNECTING, Trigger.WATCHDOG
        )
        assert owner_result.owner_token is not None
        assert subscriber_result.in_flight_handle is not None

        # Owner does its side effect, then completes with the final state.
        async def owner_workflow():
            await asyncio.sleep(0.01)  # simulate connect() I/O
            await sm.transition_complete(
                owner_result.owner_token, SessionState.CONNECTED
            )

        async def subscriber_workflow():
            return await subscriber_result.in_flight_handle.wait()

        owner_task = asyncio.create_task(owner_workflow())
        subscriber_task = asyncio.create_task(subscriber_workflow())

        final_state = await subscriber_task
        await owner_task

        assert final_state == SessionState.CONNECTED
        assert sm.state == SessionState.CONNECTED

    @pytest.mark.asyncio
    async def test_subscriber_receives_dead_when_owner_fails(self):
        """If the owner's side effect fails (retry budget exhausted), the
        subscriber learns the final state is DEAD, not CONNECTED."""
        sm = _sm_in("agent", SessionState.IDLE_SLEEPING)

        owner_result = await sm.request_transition(
            SessionState.RECONNECTING, Trigger.BROKER
        )
        subscriber_result = await sm.request_transition(
            SessionState.RECONNECTING, Trigger.WATCHDOG
        )

        async def owner_fails():
            await asyncio.sleep(0.01)
            # Connect failed; retry budget exhausted; state machine flips to DEAD.
            await sm.transition_complete(
                owner_result.owner_token, SessionState.DEAD
            )

        await asyncio.gather(
            owner_fails(),
            asyncio.create_task(
                _assert_subscriber_sees(subscriber_result.in_flight_handle, SessionState.DEAD)
            ),
        )
        assert sm.state == SessionState.DEAD

    @pytest.mark.asyncio
    async def test_owner_token_cannot_be_reused(self):
        """Completing the same token twice is a programming error."""
        sm = _sm_in("agent", SessionState.IDLE_SLEEPING)
        result = await sm.request_transition(
            SessionState.RECONNECTING, Trigger.BROKER
        )
        assert result.owner_token is not None

        await sm.transition_complete(result.owner_token, SessionState.CONNECTED)

        with pytest.raises(TransitionError):
            await sm.transition_complete(result.owner_token, SessionState.CONNECTED)

    @pytest.mark.asyncio
    async def test_cross_target_rejected_while_in_flight(self):
        """Regression for Murzik's #487 finding: while a transition is in
        flight, a different-target request from any caller must be rejected.

        Repro sequence (the bug, pre-fix):
        1. IDLE_SLEEPING → RECONNECTING via BROKER grants owner A.
        2. Another caller subscribes to RECONNECTING and waits on its handle.
        3. Before A completes, API_ADMIN requests DEAD. Pre-fix, this minted
           a second owner B because ``_in_flight.get(DEAD)`` returned None.
           State flipped to DEAD; subscriber stranded.
        4. Owner A later tried to complete CONNECTED from DEAD → TransitionError.

        Post-fix: API_ADMIN's DEAD request is rejected with a clear reason.
        Owner A can still complete the original transition. Subscriber is
        released with whatever final state A reports.
        """
        sm = _sm_in("agent", SessionState.IDLE_SLEEPING)

        # Step 1: owner A starts IDLE_SLEEPING → RECONNECTING.
        owner_a = await sm.request_transition(
            SessionState.RECONNECTING, Trigger.BROKER
        )
        assert owner_a.owner_token is not None
        assert sm.state == SessionState.RECONNECTING

        # Step 2: subscriber B joins.
        subscriber_b = await sm.request_transition(
            SessionState.RECONNECTING, Trigger.WATCHDOG
        )
        assert subscriber_b.in_flight_handle is not None

        # Step 3: API_ADMIN requests DEAD while A is still in flight.
        # Must be rejected, not granted a second owner.
        cross_target = await sm.request_transition(
            SessionState.DEAD, Trigger.API_ADMIN
        )
        assert cross_target.changed is False, (
            "cross-target request granted ownership while another transition "
            "was in flight — strands subscribers and creates competing owners"
        )
        assert cross_target.owner_token is None
        assert cross_target.in_flight_handle is None
        assert cross_target.rejection_reason is not None
        assert "already in flight" in cross_target.rejection_reason
        # State must NOT have moved.
        assert sm.state == SessionState.RECONNECTING

        # Step 4: owner A completes successfully — subscriber B is released
        # with the final state, no errors thrown.
        async def owner_workflow():
            await asyncio.sleep(0.01)
            await sm.transition_complete(
                owner_a.owner_token, SessionState.CONNECTED
            )

        owner_task = asyncio.create_task(owner_workflow())
        final = await subscriber_b.in_flight_handle.wait()
        await owner_task

        assert final == SessionState.CONNECTED
        assert sm.state == SessionState.CONNECTED

    @pytest.mark.asyncio
    async def test_cross_target_rejection_does_not_strand_subscriber(self):
        """Tighter variant of the regression: after a cross-target request
        is rejected, the original subscriber's ``wait()`` must still resolve
        when the original owner completes — proving the rejection didn't
        corrupt the in-flight registration."""
        sm = _sm_in("agent", SessionState.IDLE_SLEEPING)

        owner = await sm.request_transition(
            SessionState.RECONNECTING, Trigger.BROKER
        )
        subscriber = await sm.request_transition(
            SessionState.RECONNECTING, Trigger.WATCHDOG
        )

        # Multiple cross-target rejections.
        for trigger in (Trigger.API_ADMIN, Trigger.WATCHDOG):
            rejected = await sm.request_transition(SessionState.DEAD, trigger)
            assert rejected.rejection_reason is not None

        # Subscriber is still in the in-flight registration and resolves
        # cleanly when the owner completes.
        async def owner_completes():
            await sm.transition_complete(owner.owner_token, SessionState.CONNECTED)

        results = await asyncio.gather(
            owner_completes(),
            subscriber.in_flight_handle.wait(),
        )
        assert results[1] == SessionState.CONNECTED

    @pytest.mark.asyncio
    async def test_dead_is_universal_emergency_exit_from_any_in_flight(self):
        """Regression for Pushok's #487 finding: an owner whose side effect
        fails catastrophically must always be able to complete to DEAD,
        regardless of whether the matrix has INTERNAL on the
        ``current_state → DEAD`` cell. DEAD is the terminal sink; falling to
        it is never illegal at completion time.

        Pre-fix repro: ``CONNECTED → IDLE_SLEEPING`` owner whose
        ``idle_sleep`` crashes mid-disconnect tries to mark DEAD. The matrix
        gives ``(IDLE_SLEEPING, DEAD)`` legal triggers ``{WATCHDOG, API_ADMIN}``
        — no INTERNAL — so ``transition_complete`` raises and the in-flight
        registration is never cleared. Subscribers stranded.
        """
        # CONNECTED → IDLE_SLEEPING owner crashes mid-disconnect, falls to DEAD.
        sm = _sm_in("agent", SessionState.CONNECTED)
        sleeper = await sm.request_transition(
            SessionState.IDLE_SLEEPING, Trigger.USER_AGENT
        )
        assert sleeper.owner_token is not None
        # Side effect crashes; owner reports DEAD as the emergency exit.
        # Pre-fix: TransitionError because (IDLE_SLEEPING, DEAD) doesn't have
        # INTERNAL in its legal set. Post-fix: accepted, state moves to DEAD.
        await sm.transition_complete(sleeper.owner_token, SessionState.DEAD)
        assert sm.state == SessionState.DEAD

    @pytest.mark.asyncio
    async def test_dead_emergency_exit_releases_subscribers_with_dead(self):
        """Subscribers waiting on an in-flight transition learn the final
        state is DEAD when the owner takes the emergency exit, not the
        original target."""
        sm = _sm_in("agent", SessionState.CONNECTED)
        sleeper = await sm.request_transition(
            SessionState.IDLE_SLEEPING, Trigger.USER_AGENT
        )
        subscriber = await sm.request_transition(
            SessionState.IDLE_SLEEPING, Trigger.WATCHDOG
        )
        assert subscriber.in_flight_handle is not None

        async def owner_fails_to_dead():
            await asyncio.sleep(0.01)
            await sm.transition_complete(sleeper.owner_token, SessionState.DEAD)

        owner_task = asyncio.create_task(owner_fails_to_dead())
        final = await subscriber.in_flight_handle.wait()
        await owner_task

        assert final == SessionState.DEAD
        assert sm.state == SessionState.DEAD

    @pytest.mark.asyncio
    async def test_non_dead_completion_still_respects_matrix(self):
        """The DEAD emergency exit is specifically DEAD-only. Non-DEAD
        completion still goes through the INTERNAL-legality gate so callers
        can't quietly land in arbitrary states by claiming "complete."
        """
        sm = _sm_in("agent", SessionState.CONNECTED)
        sleeper = await sm.request_transition(
            SessionState.IDLE_SLEEPING, Trigger.USER_AGENT
        )

        # RECONNECTING is not INTERNAL-legal from IDLE_SLEEPING (legal triggers:
        # {BROKER, WATCHDOG, SCHEDULER, API_ADMIN}). Owner can't unilaterally
        # land in RECONNECTING via completion.
        with pytest.raises(TransitionError) as exc:
            await sm.transition_complete(
                sleeper.owner_token, SessionState.RECONNECTING
            )
        assert "DEAD is always legal" in str(exc.value)

    @pytest.mark.asyncio
    async def test_owner_can_emergency_exit_to_dead_on_cancellation_via_finally(self):
        """Demonstrates the documented try/finally contract: owners that
        catch ``asyncio.CancelledError`` and use the DEAD emergency exit
        do not strand subscribers."""
        sm = _sm_in("agent", SessionState.IDLE_SLEEPING)
        owner = await sm.request_transition(
            SessionState.RECONNECTING, Trigger.BROKER
        )
        subscriber = await sm.request_transition(
            SessionState.RECONNECTING, Trigger.WATCHDOG
        )

        # Simulate the owner's side-effect task getting cancelled. The owner
        # follows the documented contract: catch the cancellation, emergency-
        # exit to DEAD via transition_complete, re-raise.
        async def owner_workflow():
            try:
                await asyncio.sleep(10)  # would block forever
            except asyncio.CancelledError:
                await sm.transition_complete(owner.owner_token, SessionState.DEAD)
                raise

        task = asyncio.create_task(owner_workflow())
        # Yield so the owner starts the sleep.
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        # Subscriber resolves with DEAD, not stranded.
        final = await subscriber.in_flight_handle.wait()
        assert final == SessionState.DEAD
        assert sm.state == SessionState.DEAD

    @pytest.mark.asyncio
    async def test_in_flight_clears_after_completion_so_new_transition_can_start(self):
        """After an in-flight transition completes, the state machine must
        accept a new transition request — the singleton guard releases."""
        sm = _sm_in("agent", SessionState.IDLE_SLEEPING)

        owner_1 = await sm.request_transition(
            SessionState.RECONNECTING, Trigger.BROKER
        )
        await sm.transition_complete(owner_1.owner_token, SessionState.CONNECTED)

        # Now request a fresh transition — must succeed (CONNECTED → IDLE_SLEEPING).
        owner_2 = await sm.request_transition(
            SessionState.IDLE_SLEEPING, Trigger.WATCHDOG
        )
        assert owner_2.changed is True
        assert owner_2.owner_token is not None

    @pytest.mark.asyncio
    async def test_completion_rejects_illegal_final_state(self):
        """If an owner reports a final state that isn't an INTERNAL-legal
        transition from the current state, completion raises rather than
        silently parking the state machine in an invalid state."""
        sm = _sm_in("agent", SessionState.IDLE_SLEEPING)
        result = await sm.request_transition(
            SessionState.RECONNECTING, Trigger.BROKER
        )
        assert result.owner_token is not None

        # RECONNECTING → UNINITIALIZED is not INTERNAL-legal (UNINITIALIZED
        # has no return path).
        with pytest.raises(TransitionError):
            await sm.transition_complete(
                result.owner_token, SessionState.UNINITIALIZED
            )


async def _assert_subscriber_sees(handle: InFlightHandle, expected: SessionState):
    """Helper: subscriber waits and asserts the final state matches."""
    final = await handle.wait()
    assert final == expected


# ──────────────────────────────────────────────────────────────────────────
# Specific matrix invariants worth their own tests
# ──────────────────────────────────────────────────────────────────────────


class TestMatrixInvariants:
    @pytest.mark.asyncio
    async def test_reconnecting_to_connected_is_internal_only(self):
        """Invariant 2: only the Transport's own connect coroutine can flip
        a session into CONNECTED. External triggers must be rejected."""
        for trigger in Trigger:
            if trigger == Trigger.INTERNAL:
                continue  # The legal one — tested in TestMatrixLegalCells.
            sm = _sm_in("agent", SessionState.RECONNECTING)
            # Inject an in-flight transition via completion, since we can't
            # request the same transition from outside INTERNAL: just check
            # that an external trigger requesting CONNECTED is rejected.
            result = await sm.request_transition(
                SessionState.CONNECTED, trigger
            )
            assert result.changed is False, (
                f"external trigger {trigger.value} flipped session into CONNECTED"
            )
            assert result.rejection_reason is not None
            assert "not authorized" in result.rejection_reason

    @pytest.mark.asyncio
    async def test_uninitialized_is_one_way(self):
        """Invariant 3: once a Transport leaves UNINITIALIZED, no transition
        returns to it."""
        for state in SessionState:
            if state == SessionState.UNINITIALIZED:
                continue
            sm = _sm_in("agent", state)
            for trigger in Trigger:
                result = await sm.request_transition(
                    SessionState.UNINITIALIZED, trigger
                )
                assert result.changed is False, (
                    f"{state.value} → UNINITIALIZED accepted via {trigger.value}"
                )

    @pytest.mark.asyncio
    async def test_reconnecting_to_idle_sleeping_is_illegal(self):
        """Can't sleep mid-connect."""
        sm = _sm_in("agent", SessionState.RECONNECTING)
        for trigger in Trigger:
            result = await sm.request_transition(
                SessionState.IDLE_SLEEPING, trigger
            )
            assert result.changed is False
            assert result.rejection_reason is not None

    @pytest.mark.asyncio
    async def test_idle_sleeping_to_connected_must_go_through_reconnecting(self):
        """Invariant 1: every successful connect is observable as RECONNECTING
        first. No direct IDLE_SLEEPING → CONNECTED."""
        sm = _sm_in("agent", SessionState.IDLE_SLEEPING)
        for trigger in Trigger:
            result = await sm.request_transition(
                SessionState.CONNECTED, trigger
            )
            assert result.changed is False
            assert result.rejection_reason is not None

    @pytest.mark.asyncio
    async def test_dead_to_idle_sleeping_is_illegal(self):
        """Sleeping a dead session has no meaning."""
        sm = _sm_in("agent", SessionState.DEAD)
        for trigger in Trigger:
            result = await sm.request_transition(
                SessionState.IDLE_SLEEPING, trigger
            )
            assert result.changed is False
            assert result.rejection_reason is not None

    # ── PR6 invariants (Pushok): cold-start lifecycle ───────────────────────

    def test_uninitialized_has_zero_incoming_edges(self):
        """PR6 (Pushok dual-frame invariant): UNINITIALIZED is the birth state
        and has zero incoming edges in the matrix. Combined with the test
        ``test_uninitialized_is_one_way`` (which proves no trigger lands a
        non-UNINITIALIZED session there) this is the strongest version of
        "never reachable" — a future maintainer adding a row whose to_state
        is UNINITIALIZED will fail this structural check immediately,
        independent of any trigger.

        This is the dual of DEAD's pattern: DEAD has many incoming edges
        (universal sink) and limited outgoing edges (resurrection only);
        UNINITIALIZED has zero incoming edges and many legal outgoing edges.
        """
        for (from_state, to_state), _triggers in LEGAL_TRANSITIONS.items():
            assert to_state != SessionState.UNINITIALIZED, (
                f"matrix violates UNINITIALIZED-zero-incoming invariant: "
                f"({from_state.value} → {to_state.value}) lands in UNINITIALIZED"
            )

    def test_boot_only_drives_from_uninitialized(self):
        """PR6 (Pushok): the BOOT trigger only fires when from_state is
        UNINITIALIZED. Pairs with ``test_uninitialized_has_zero_incoming_edges``
        as the structural bypass for the singleton-in-flight guard:
        UNINITIALIZED is provably never reachable while another transition
        is in flight (because nothing transitions TO UNINITIALIZED), and
        BOOT only fires from UNINITIALIZED, so a BOOT request cannot race
        an in-flight transition by construction.
        """
        for (from_state, to_state), triggers in LEGAL_TRANSITIONS.items():
            if Trigger.BOOT not in triggers:
                continue
            assert from_state == SessionState.UNINITIALIZED, (
                f"BOOT trigger drives non-cold-start cell "
                f"({from_state.value} → {to_state.value}); BOOT must only "
                f"fire from UNINITIALIZED"
            )

    def test_boot_complete_only_drives_from_booting(self):
        """PR6 (Pushok): BOOT_COMPLETE closes the cold-start lifecycle. It
        must only ever drive BOOTING → CONNECTED — any other cell would
        decouple the trigger from the cold-start lifecycle it's named for.
        """
        for (from_state, to_state), triggers in LEGAL_TRANSITIONS.items():
            if Trigger.BOOT_COMPLETE not in triggers:
                continue
            assert (from_state, to_state) == (
                SessionState.BOOTING, SessionState.CONNECTED
            ), (
                f"BOOT_COMPLETE drives unexpected cell "
                f"({from_state.value} → {to_state.value}); must only close "
                f"BOOTING → CONNECTED"
            )

    def test_boot_failed_only_drives_from_booting(self):
        """PR6 (Pushok): BOOT_FAILED closes the cold-start lifecycle on
        failure. It must only ever drive BOOTING → DEAD.
        """
        for (from_state, to_state), triggers in LEGAL_TRANSITIONS.items():
            if Trigger.BOOT_FAILED not in triggers:
                continue
            assert (from_state, to_state) == (
                SessionState.BOOTING, SessionState.DEAD
            ), (
                f"BOOT_FAILED drives unexpected cell "
                f"({from_state.value} → {to_state.value}); must only close "
                f"BOOTING → DEAD"
            )

    def test_booting_is_one_shot(self):
        """PR6 (Pushok): cold-start is one-shot. BOOTING has no self-edge
        and no exit to RECONNECTING — failed cold-starts go to DEAD and
        reduce to warm-reconnect through the DEAD → RECONNECTING
        resurrection path.
        """
        # No self-edge.
        assert (SessionState.BOOTING, SessionState.BOOTING) not in LEGAL_TRANSITIONS
        # No direct hop to RECONNECTING; that conflation is exactly what
        # introducing BOOTING avoids.
        assert (SessionState.BOOTING, SessionState.RECONNECTING) in ILLEGAL_PAIR_REASONS
        # The legal exits from BOOTING are exactly {CONNECTED, DEAD}.
        booting_exits = {
            to_state
            for (from_state, to_state) in LEGAL_TRANSITIONS
            if from_state == SessionState.BOOTING
        }
        assert booting_exits == {SessionState.CONNECTED, SessionState.DEAD}, (
            f"BOOTING legal exits drifted: got {booting_exits}, "
            f"expected {{CONNECTED, DEAD}}"
        )

    @pytest.mark.asyncio
    async def test_boot_completion_uses_boot_complete_trigger(self):
        """End-to-end: cold-start lifecycle goes UNINITIALIZED → BOOTING via
        BOOT, then BOOTING → CONNECTED via BOOT_COMPLETE (not INTERNAL).
        Audit logs gain a distinct cold-start completion signal vs the warm
        RECONNECTING → CONNECTED INTERNAL completion.
        """
        sm = StateMachine("agent")
        boot = await sm.request_transition(SessionState.BOOTING, Trigger.BOOT)
        assert boot.owner_token is not None
        assert sm.state == SessionState.BOOTING
        # Complete with BOOT_COMPLETE explicitly.
        await sm.transition_complete(
            boot.owner_token, SessionState.CONNECTED,
            trigger=Trigger.BOOT_COMPLETE,
        )
        assert sm.state == SessionState.CONNECTED

    @pytest.mark.asyncio
    async def test_boot_completion_with_internal_trigger_rejected(self):
        """The matrix authorizes BOOTING → CONNECTED ONLY via BOOT_COMPLETE.
        A completion that tries to use INTERNAL — the warm-reconnect
        completion trigger — must be rejected. Without this guard, callers
        could silently complete cold-start through the warm-path code path
        and lose the audit-log distinction.
        """
        sm = StateMachine("agent")
        boot = await sm.request_transition(SessionState.BOOTING, Trigger.BOOT)
        assert boot.owner_token is not None
        with pytest.raises(TransitionError, match="not legal via internal"):
            await sm.transition_complete(
                boot.owner_token, SessionState.CONNECTED,
                trigger=Trigger.INTERNAL,
            )

    @pytest.mark.asyncio
    async def test_boot_failure_uses_boot_failed_trigger(self):
        """Cold-start failure: BOOTING → DEAD via BOOT_FAILED."""
        sm = StateMachine("agent")
        boot = await sm.request_transition(SessionState.BOOTING, Trigger.BOOT)
        assert boot.owner_token is not None
        await sm.transition_complete(
            boot.owner_token, SessionState.DEAD,
            trigger=Trigger.BOOT_FAILED,
        )
        assert sm.state == SessionState.DEAD

    @pytest.mark.asyncio
    async def test_cold_start_failure_reduces_to_warm_reconnect(self):
        """PR6 (Pushok one-shot semantic): after BOOTING → DEAD, the only
        recovery path is the warm-reconnect resurrection DEAD → RECONNECTING.
        There is no BOOTING → BOOTING self-edge and no BOOTING → RECONNECTING
        hop; retries reduce to warm-reconnect through the existing
        resurrection machinery.
        """
        sm = StateMachine("agent")
        boot = await sm.request_transition(SessionState.BOOTING, Trigger.BOOT)
        await sm.transition_complete(
            boot.owner_token, SessionState.DEAD, trigger=Trigger.BOOT_FAILED,
        )
        # No retry within BOOTING — a BOOT trigger from DEAD must reject
        # (UNINITIALIZED is never reachable, so BOOT has nothing to drive).
        reject = await sm.request_transition(SessionState.BOOTING, Trigger.BOOT)
        assert reject.changed is False
        # Warm-reconnect resurrection IS legal — that's the retry path.
        reconnect = await sm.request_transition(
            SessionState.RECONNECTING, Trigger.WATCHDOG,
        )
        assert reconnect.changed is True
        assert sm.state == SessionState.RECONNECTING


# ──────────────────────────────────────────────────────────────────────────
# Audit log emission
# ──────────────────────────────────────────────────────────────────────────


class TestAuditLog:
    @pytest.mark.asyncio
    async def test_every_request_emits_one_audit_line(self, capsys):
        """One audit line per request_transition call, regardless of outcome
        (changed / observational / subscribed / rejected)."""
        sm = _sm_in("test", SessionState.IDLE_SLEEPING)

        # Identity (observational) — IDLE_SLEEPING → IDLE_SLEEPING is an
        # observational read regardless of trigger (no in-flight transition).
        await sm.request_transition(SessionState.IDLE_SLEEPING, Trigger.BROKER)
        # Legal transition (owned) — IDLE_SLEEPING → RECONNECTING accepts BROKER.
        await sm.request_transition(SessionState.RECONNECTING, Trigger.BROKER)
        # Subscribed (in-flight) — a second caller targeting RECONNECTING
        # while the first is still in flight subscribes instead of racing.
        await sm.request_transition(SessionState.RECONNECTING, Trigger.WATCHDOG)

        captured = capsys.readouterr().err
        lines = [ln for ln in captured.splitlines() if "transport_state[test]" in ln]
        assert len(lines) == 3, f"expected 3 audit lines, got {len(lines)}: {lines}"
        assert "result=observational" in lines[0]
        assert "result=owned" in lines[1]
        assert "result=subscribed" in lines[2]

    @pytest.mark.asyncio
    async def test_rejection_emits_audit_with_reason(self, capsys):
        sm = _sm_in("test", SessionState.DEAD)
        await sm.request_transition(SessionState.CONNECTED, Trigger.BROKER)
        err = capsys.readouterr().err
        assert "transport_state[test]" in err
        assert "result=rejected" in err
        assert "reason=" in err

    @pytest.mark.asyncio
    async def test_caller_supplied_reason_in_audit(self, capsys):
        """Caller-supplied ``reason`` (e.g. ``boot_declined``) shows up in
        the audit line."""
        sm = StateMachine("test")
        await sm.request_transition(
            SessionState.DEAD, Trigger.BOOT, reason="boot_declined"
        )
        err = capsys.readouterr().err
        assert "reason='boot_declined'" in err


# ──────────────────────────────────────────────────────────────────────────
# OwnerToken uniqueness
# ──────────────────────────────────────────────────────────────────────────


class TestOwnerToken:
    @pytest.mark.asyncio
    async def test_each_ownership_grant_yields_unique_token(self):
        """OwnerTokens are minted per transition; reusing a closed transition's
        token in transition_complete is rejected (see test_owner_token_cannot_be_reused)."""
        tokens = set()
        for _ in range(20):
            sm = _sm_in("agent", SessionState.IDLE_SLEEPING)
            result = await sm.request_transition(
                SessionState.RECONNECTING, Trigger.BROKER
            )
            assert result.owner_token is not None
            tokens.add(result.owner_token.token)
        assert len(tokens) == 20, "OwnerToken hash collision or non-uniqueness"
