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

# ──────────────────────────────────────────────────────────────────────────
# Construction & basic state reads
# ──────────────────────────────────────────────────────────────────────────


class TestConstruction:
    def test_default_initial_state_is_uninitialized(self):
        sm = StateMachine("agent")
        assert sm.state == SessionState.UNINITIALIZED

    def test_explicit_initial_state_honored(self):
        sm = StateMachine("agent", initial_state=SessionState.CONNECTED)
        assert sm.state == SessionState.CONNECTED


# ──────────────────────────────────────────────────────────────────────────
# Identity / observational reads
# ──────────────────────────────────────────────────────────────────────────


class TestIdentity:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("state", list(SessionState))
    async def test_same_state_is_observational(self, state):
        """``request_transition(current_state)`` returns ``changed=False`` and
        confers no ownership, regardless of trigger."""
        sm = StateMachine("agent", initial_state=state)
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
                sm = StateMachine("agent", initial_state=from_state)
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
                sm = StateMachine("agent", initial_state=from_state)
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
                sm = StateMachine("agent", initial_state=from_state)
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
        sm = StateMachine("agent", initial_state=SessionState.IDLE_SLEEPING)

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
        sm = StateMachine("agent", initial_state=SessionState.IDLE_SLEEPING)

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
        sm = StateMachine("agent", initial_state=SessionState.IDLE_SLEEPING)

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
        sm = StateMachine("agent", initial_state=SessionState.IDLE_SLEEPING)
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
        sm = StateMachine("agent", initial_state=SessionState.IDLE_SLEEPING)

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
        sm = StateMachine("agent", initial_state=SessionState.IDLE_SLEEPING)

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
    async def test_in_flight_clears_after_completion_so_new_transition_can_start(self):
        """After an in-flight transition completes, the state machine must
        accept a new transition request — the singleton guard releases."""
        sm = StateMachine("agent", initial_state=SessionState.IDLE_SLEEPING)

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
        sm = StateMachine("agent", initial_state=SessionState.IDLE_SLEEPING)
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
            sm = StateMachine("agent", initial_state=SessionState.RECONNECTING)
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
            sm = StateMachine("agent", initial_state=state)
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
        sm = StateMachine("agent", initial_state=SessionState.RECONNECTING)
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
        sm = StateMachine("agent", initial_state=SessionState.IDLE_SLEEPING)
        for trigger in Trigger:
            result = await sm.request_transition(
                SessionState.CONNECTED, trigger
            )
            assert result.changed is False
            assert result.rejection_reason is not None

    @pytest.mark.asyncio
    async def test_dead_to_idle_sleeping_is_illegal(self):
        """Sleeping a dead session has no meaning."""
        sm = StateMachine("agent", initial_state=SessionState.DEAD)
        for trigger in Trigger:
            result = await sm.request_transition(
                SessionState.IDLE_SLEEPING, trigger
            )
            assert result.changed is False
            assert result.rejection_reason is not None


# ──────────────────────────────────────────────────────────────────────────
# Audit log emission
# ──────────────────────────────────────────────────────────────────────────


class TestAuditLog:
    @pytest.mark.asyncio
    async def test_every_request_emits_one_audit_line(self, capsys):
        """One audit line per request_transition call, regardless of outcome
        (changed / observational / subscribed / rejected)."""
        sm = StateMachine("test", initial_state=SessionState.IDLE_SLEEPING)

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
        sm = StateMachine("test", initial_state=SessionState.DEAD)
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
            sm = StateMachine("agent", initial_state=SessionState.IDLE_SLEEPING)
            result = await sm.request_transition(
                SessionState.RECONNECTING, Trigger.BROKER
            )
            assert result.owner_token is not None
            tokens.add(result.owner_token.token)
        assert len(tokens) == 20, "OwnerToken hash collision or non-uniqueness"
