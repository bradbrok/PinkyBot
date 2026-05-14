"""Transport session state machine.

Replaces the implicit ``is_connected + session_id`` two-bool state inference in
``streaming_session.py`` + ``broker.py`` with an explicit five-state enum and a
lock-guarded transition primitive that **grants ownership** of any side effect
bound to a transition.

The two-bool inference had four observable states (CONNECTED, RECONNECTING,
IDLE_SLEEPING, DEAD) encoded across two independently-mutable booleans, plus
an implicit fifth state ("never tried") that nothing distinguished from DEAD.
During ``force_restart``'s ``disconnect()`` → ``connect()`` window the bools
disagreed with each other, the broker raced the restart, and inbound messages
got silently dropped (see PR #484 + issue #486). The fix was a bounded poll;
the underlying race is still there.

This module is the state portion of the upcoming ``Transport`` protocol.
Backends (``StreamingSession`` for the SDK, ``TmuxSession`` for the tmux
transport) compose this state machine; callers (broker, watchdog, API) read
state through it instead of reverse-engineering it from per-backend bools.

Design contract (matrix v2 on issue #486):

1. **State transitions are ownership grants, not just mutations.** Concurrent
   callers requesting the same target transition race for ownership exactly
   once. The winner gets an ``OwnerToken`` and runs the side effect (e.g.
   ``connect()``). Losers get an ``InFlightHandle`` and subscribe via
   ``await_transition_complete``. Same-state requests are observational —
   ``changed=False``, no token granted, no side effect implied.

2. **Every transition declares a trigger.** ``request_transition(target,
   trigger)`` carries the actor identity (BOOT / BROKER / WATCHDOG /
   INTERNAL / USER_AGENT / API_ADMIN). The matrix defends cells per
   ``(from, to, trigger)`` triple — most notably ``RECONNECTING → CONNECTED``
   is INTERNAL-only, so no external caller can flip a session into CONNECTED.

3. **Resume handles (``session_id`` for SDK, tmux session name + transcript
   path for tmux) are Transport implementation detail.** The state machine
   doesn't see them. ``IDLE_SLEEPING`` means "deliberately disconnected with
   private wake semantics," not "has a non-empty SDK session_id."

4. **Audit log per transition.** Every ``request_transition`` call writes a
   log line — including identity / observational reads and rejected
   transitions with reason. Free-for-future telemetry.

5. **RECONNECTING is the macro state across all backoff attempts.** Internal
   retry sub-states stay private to the Transport; the state machine doesn't
   flicker DEAD ↔ RECONNECTING between attempts.

6. **State mutates at grant time, not at completion.** When
   ``request_transition`` grants ownership, ``self._state`` flips to the
   target **before** the owner's side effect runs. This is intentional and
   has direct consequences for observers:

   - Broker, watchdog, and dashboard see "we are RECONNECTING" as soon as
     a reconnect is intended, not after it completes. This is what makes
     "RECONNECTING-as-intent" observable and what lets the broker hold
     inbound messages instead of dropping them during a force_restart
     (see PR #484).
   - The in-flight registration is the source of truth for "a transition
     is in progress" — readers should consult both ``state`` and
     ``in_flight`` if they need to distinguish "settled in RECONNECTING"
     from "transitioning to RECONNECTING right now."
   - A "state-only-changes-on-commit" alternative would defer the observable
     flip to ``transition_complete``. That's a valid design but it requires
     a separate "intended state" field to make in-flight observable, which
     is just the singleton in-flight by another name. We picked the
     simpler shape: state mutates eagerly, completion releases subscribers.

   The original double-connect race (issue #486) was downstream of this
   choice: pre-state-machine, the two bools ``is_connected`` and ``session_id``
   were mutated independently around the side effect, so observers saw
   intermediate combinations the code didn't expect. With the singleton
   in-flight and grant-time mutation, observers see a single consistent
   transition state.
"""

from __future__ import annotations

import asyncio
import secrets
import sys
from dataclasses import dataclass, field
from enum import Enum


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


class SessionState(Enum):
    """Lifecycle state of a Transport session.

    See the matrix on issue #486 for the legal/illegal transition table and the
    rationale per cell. Summary:

    - ``UNINITIALIZED`` — birth state. Constructed but never tried to connect.
      Distinguishes "never tried" from "tried and failed."
    - ``RECONNECTING`` — macro state for "currently trying to be CONNECTED."
      Internal retry attempts stay inside this state; no flicker.
    - ``CONNECTED`` — happy path. Can send/receive.
    - ``IDLE_SLEEPING`` — deliberately disconnected with wake semantics.
      Distinct from DEAD because the Transport retains private resume info.
    - ``DEAD`` — unrecoverable without explicit resurrection. Auth failure,
      retry budget exhausted, watchdog give-up, explicit shutdown.
    """

    UNINITIALIZED = "uninitialized"
    RECONNECTING = "reconnecting"
    CONNECTED = "connected"
    IDLE_SLEEPING = "idle_sleeping"
    DEAD = "dead"


class Trigger(Enum):
    """Actor that initiated a transition.

    Carried on every ``request_transition`` call so the matrix can defend
    cells per ``(from, to, trigger)``. ``RECONNECTING → CONNECTED`` is
    INTERNAL-only: only the Transport's own connect coroutine can drive the
    final flip into CONNECTED after a successful handshake.
    """

    BOOT = "boot"  # daemon startup / boot policy
    BROKER = "broker"  # _route_streaming auto-wake / resurrection on inbound
    WATCHDOG = "watchdog"  # session_watchdog lifecycle decisions (idle, resurrect)
    SCHEDULER = "scheduler"  # scheduler.py cron-driven wakes + heartbeat resurrect
    INTERNAL = "internal"  # Transport's own connect/disconnect machinery
    USER_AGENT = "user_agent"  # agent's own MCP tools (context_restart, idle_sleep)
    API_ADMIN = "api_admin"  # explicit operator action via HTTP


@dataclass(frozen=True)
class OwnerToken:
    """Granted to the caller that drove a state change.

    Possession of an ``OwnerToken`` confers the right (and the **obligation**)
    to run the side effect bound to the transition. After completing, the
    owner calls ``StateMachine.transition_complete(token, final_state)`` to
    release waiting subscribers. Tokens are single-use; passing the same
    token twice is a programming error.

    **Driver abandonment contract.** Owners MUST call ``transition_complete``
    on every code path that exits ownership — success, failure, exception, or
    ``asyncio.CancelledError``. If the owner task dies without completing, the
    ``_in_flight`` registration is never cleared and ``InFlightHandle.wait()``
    subscribers block forever. The canonical pattern is::

        result = await sm.request_transition(target, trigger)
        if result.owner_token is not None:
            try:
                await side_effect()
                await sm.transition_complete(result.owner_token, final_state)
            except BaseException:
                # Universal emergency exit — DEAD is always legal here.
                await sm.transition_complete(result.owner_token, SessionState.DEAD)
                raise

    ``transition_complete(token, SessionState.DEAD)`` is allowed from any
    state regardless of matrix rules (see ``StateMachine.transition_complete``).
    This is the catch-fire path for owners whose side effect failed
    catastrophically; the state machine accepts DEAD as the terminal sink
    so the driver-abandonment failure mode is avoidable in practice.

    Auto-cleanup on owner-task cancellation is a future enhancement; for now
    the contract is enforced by convention + the emergency-exit escape hatch.
    """

    token: str
    target: SessionState

    @classmethod
    def _new(cls, target: SessionState) -> "OwnerToken":
        return cls(token=secrets.token_hex(8), target=target)


@dataclass
class InFlightHandle:
    """Subscriber handle for an in-flight transition.

    Returned to ``request_transition`` callers that found an existing
    transition to the same target already in flight. ``await_transition_complete``
    blocks on the underlying event until the owning caller calls
    ``transition_complete``; subscribers then read the final state.

    Why not a busy poll: subscribers may be in the broker hot path, and a busy
    poll would burn the event loop while the owner does I/O. ``asyncio.Event``
    is the natural primitive — wait is parked, owner signals once.
    """

    target: SessionState
    _event: asyncio.Event = field(default_factory=asyncio.Event)
    _final_state: SessionState | None = None

    async def wait(self) -> SessionState:
        """Block until the owner releases the transition. Returns the final state."""
        await self._event.wait()
        assert self._final_state is not None, "owner released without setting final state"
        return self._final_state

    def _resolve(self, final_state: SessionState) -> None:
        self._final_state = final_state
        self._event.set()


@dataclass(frozen=True)
class TransitionResult:
    """Outcome of a ``request_transition`` call.

    Three possible shapes:

    1. **Caller drove the change.** ``changed=True``, ``owner_token`` set,
       ``in_flight_handle=None``. Caller MUST call ``transition_complete``
       once the side effect resolves.
    2. **Same target already in flight.** ``changed=False``,
       ``owner_token=None``, ``in_flight_handle`` set. Caller subscribes via
       ``in_flight_handle.wait()`` to learn the final state.
    3. **Same-state observational read.** ``changed=False``, both tokens
       ``None``. ``from_state == to_state``; no side effect implied.
    4. **Rejected.** ``changed=False``, both tokens ``None``,
       ``rejection_reason`` set. The matrix rejected this triple.
    """

    changed: bool
    from_state: SessionState
    to_state: SessionState
    owner_token: OwnerToken | None = None
    in_flight_handle: InFlightHandle | None = None
    rejection_reason: str | None = None


# ──────────────────────────────────────────────────────────────────────────
# Transition matrix
# ──────────────────────────────────────────────────────────────────────────
#
# Cell key: (from_state, to_state, trigger) → legal? + reason.
# Identity transitions (from == to) are observational reads — always allowed,
# but ``changed=False`` and no token granted. They are not stored here; the
# state machine handles them in ``request_transition`` before matrix lookup.
#
# Each illegal entry carries a reason string. "Illegal" means "intentionally
# disallowed" — if a future case demands a new transition, add it explicitly
# with a defense, not by accident.

# Cells where the (from, to) pair is legal, listing the set of triggers that
# may drive it. Any (from, to, trigger) outside this map is REJECTED.
LEGAL_TRANSITIONS: dict[tuple[SessionState, SessionState], frozenset[Trigger]] = {
    # FROM UNINITIALIZED
    (SessionState.UNINITIALIZED, SessionState.RECONNECTING): frozenset({Trigger.BOOT}),
    # Boot policy decline OR shutdown-before-start.
    (SessionState.UNINITIALIZED, SessionState.DEAD): frozenset(
        {Trigger.BOOT, Trigger.API_ADMIN}
    ),

    # FROM RECONNECTING
    # Only the Transport's own connect handshake can drive CONNECTED.
    (SessionState.RECONNECTING, SessionState.CONNECTED): frozenset({Trigger.INTERNAL}),
    # Retry budget exhausted / connect() raised; also admin force-kill mid-reconnect.
    (SessionState.RECONNECTING, SessionState.DEAD): frozenset(
        {Trigger.INTERNAL, Trigger.API_ADMIN}
    ),

    # FROM CONNECTED
    # context_restart (USER_AGENT) / forced reconnect (WATCHDOG) / admin restart
    # (API_ADMIN) / recoverable transport loss (INTERNAL: EOF, reader-loop crash).
    (SessionState.CONNECTED, SessionState.RECONNECTING): frozenset(
        {Trigger.USER_AGENT, Trigger.WATCHDOG, Trigger.API_ADMIN, Trigger.INTERNAL}
    ),
    # idle_sleep MCP / watchdog idle-threshold.
    (SessionState.CONNECTED, SessionState.IDLE_SLEEPING): frozenset(
        {Trigger.USER_AGENT, Trigger.WATCHDOG}
    ),
    # Terminal: auth_failed (INTERNAL) or admin shutdown (API_ADMIN). Recoverable
    # failures go via RECONNECTING in the cell above.
    (SessionState.CONNECTED, SessionState.DEAD): frozenset(
        {Trigger.INTERNAL, Trigger.API_ADMIN}
    ),

    # FROM IDLE_SLEEPING
    # Auto-wake on inbound / scheduled wake / admin wake.
    (SessionState.IDLE_SLEEPING, SessionState.RECONNECTING): frozenset(
        {Trigger.BROKER, Trigger.WATCHDOG, Trigger.SCHEDULER, Trigger.API_ADMIN}
    ),
    # Watchdog give-up / admin shutdown. Direct (no fake RECONNECTING when
    # nothing is trying to wake).
    (SessionState.IDLE_SLEEPING, SessionState.DEAD): frozenset(
        {Trigger.WATCHDOG, Trigger.API_ADMIN}
    ),

    # FROM DEAD
    # Resurrection on inbound / admin resurrect / watchdog post-backoff /
    # scheduler heartbeat resurrect.
    (SessionState.DEAD, SessionState.RECONNECTING): frozenset(
        {Trigger.BROKER, Trigger.API_ADMIN, Trigger.WATCHDOG, Trigger.SCHEDULER}
    ),
}


# Illegal (from, to) pairs with the rationale. Used to produce useful rejection
# messages when a caller requests a structurally-disallowed transition.
ILLEGAL_PAIR_REASONS: dict[tuple[SessionState, SessionState], str] = {
    (SessionState.UNINITIALIZED, SessionState.CONNECTED):
        "no shortcut to CONNECTED — must go through RECONNECTING",
    (SessionState.UNINITIALIZED, SessionState.IDLE_SLEEPING):
        "can't sleep something that never connected",
    (SessionState.RECONNECTING, SessionState.UNINITIALIZED):
        "UNINITIALIZED is birth state, no return path",
    (SessionState.RECONNECTING, SessionState.IDLE_SLEEPING):
        "can't sleep mid-connect; finish reconnecting or fail to DEAD first",
    (SessionState.CONNECTED, SessionState.UNINITIALIZED):
        "UNINITIALIZED is birth state, no return path",
    (SessionState.IDLE_SLEEPING, SessionState.UNINITIALIZED):
        "UNINITIALIZED is birth state, no return path",
    (SessionState.IDLE_SLEEPING, SessionState.CONNECTED):
        "wake goes through RECONNECTING for observability — no direct CONNECTED",
    (SessionState.DEAD, SessionState.UNINITIALIZED):
        "UNINITIALIZED is birth state, no return path",
    (SessionState.DEAD, SessionState.CONNECTED):
        "resurrection goes through RECONNECTING — no direct CONNECTED",
    (SessionState.DEAD, SessionState.IDLE_SLEEPING):
        "sleeping a dead session has no meaning; resurrect to RECONNECTING first",
}


# ──────────────────────────────────────────────────────────────────────────
# State machine
# ──────────────────────────────────────────────────────────────────────────


class TransitionError(RuntimeError):
    """Raised when a transition primitive is misused (not when a transition
    is structurally rejected — that returns a ``TransitionResult`` with
    ``rejection_reason`` set)."""


class StateMachine:
    """Lock-guarded state machine for a single Transport session.

    One instance per Transport. Not safe to share across sessions.

    The state machine owns:
    - Current ``SessionState``.
    - A single ``asyncio.Lock`` serializing state mutations.
    - At most one in-flight transition per target state (the ``OwnerToken``
      holder, plus any subscribed ``InFlightHandle``s).
    - An audit log emitter.

    The state machine does NOT own:
    - Resume handles (SDK session_id, tmux session name, transcript paths).
      Each Transport implementation holds these privately.
    - Retry policy. Backoff lives inside the Transport's RECONNECTING side
      effect; the state machine stays in RECONNECTING for the duration.
    """

    def __init__(
        self,
        owner_label: str,
        *,
        initial_state: SessionState = SessionState.UNINITIALIZED,
    ) -> None:
        self._label = owner_label
        self._state = initial_state
        self._lock = asyncio.Lock()
        # At most one in-flight transition per state machine (singleton, not
        # keyed by target). Same-target requests subscribe; different-target
        # requests reject until the owner completes. Without this, two
        # competing in-flight transitions can coexist for different targets
        # and the first transition's subscribers get stranded. Repro: see
        # ``test_cross_target_rejected_while_in_flight`` and Murzik's review
        # of PR #487 (https://github.com/bradbrok/PinkyBot/issues/486).
        self._in_flight: _InFlight | None = None

    @property
    def state(self) -> SessionState:
        """Current state. Safe to read without the lock (atomic in CPython for
        enum reads); callers wanting consistency with a transition should use
        the ``TransitionResult`` returned from ``request_transition``."""
        return self._state

    async def request_transition(
        self,
        target: SessionState,
        trigger: Trigger,
        *,
        reason: str | None = None,
    ) -> TransitionResult:
        """Request a transition to ``target`` on behalf of ``trigger``.

        Returns a ``TransitionResult`` describing the outcome — see the
        ``TransitionResult`` docstring for the four possible shapes.

        ``reason`` is an optional free-form string carried in the audit log
        (e.g. ``"boot_declined"`` for ``UNINITIALIZED → DEAD`` driven by the
        boot policy). The state machine doesn't interpret it.
        """
        async with self._lock:
            from_state = self._state
            in_flight = self._in_flight

            # Case 1: a transition is in flight.
            if in_flight is not None:
                # Same target → subscribe. Caller gets the final state when
                # the owner completes; no duplicate side effect runs.
                if in_flight.target == target:
                    handle = InFlightHandle(target=target)
                    in_flight.subscribers.append(handle)
                    self._audit(
                        from_state, target, trigger, "subscribed", reason=reason
                    )
                    return TransitionResult(
                        changed=False,
                        from_state=from_state,
                        to_state=target,
                        in_flight_handle=handle,
                    )

                # Different target → reject. The state machine is committed to
                # an in-flight transition; cross-target requests must wait for
                # it to complete (or, in a future PR, use an explicit cancel/
                # override path that resolves the old subscribers and
                # invalidates the old token). Without this rejection, two
                # owners can coexist for different targets and the first
                # transition's subscribers get stranded — see the regression
                # test for the exact sequence (Murzik's PR #487 review).
                rej = (
                    f"transition already in flight for "
                    f"{in_flight.target.value} (started via "
                    f"{in_flight.trigger.value}); cannot request "
                    f"{target.value} until the owner completes"
                )
                self._audit(
                    from_state, target, trigger, "rejected", reason=rej
                )
                return TransitionResult(
                    changed=False,
                    from_state=from_state,
                    to_state=from_state,
                    rejection_reason=rej,
                )

            # Case 2: identity / observational read (no in-flight transition).
            if from_state == target:
                self._audit(
                    from_state, target, trigger, "observational", reason=reason
                )
                return TransitionResult(
                    changed=False,
                    from_state=from_state,
                    to_state=target,
                )

            # Case 3: matrix rejection.
            legal_triggers = LEGAL_TRANSITIONS.get((from_state, target))
            if legal_triggers is None:
                rej = ILLEGAL_PAIR_REASONS.get(
                    (from_state, target),
                    "transition not in matrix",
                )
                self._audit(
                    from_state, target, trigger, "rejected", reason=rej
                )
                return TransitionResult(
                    changed=False,
                    from_state=from_state,
                    to_state=from_state,
                    rejection_reason=rej,
                )
            if trigger not in legal_triggers:
                rej = (
                    f"trigger {trigger.value} not authorized for "
                    f"{from_state.value} → {target.value} "
                    f"(legal: {sorted(t.value for t in legal_triggers)})"
                )
                self._audit(
                    from_state, target, trigger, "rejected", reason=rej
                )
                return TransitionResult(
                    changed=False,
                    from_state=from_state,
                    to_state=from_state,
                    rejection_reason=rej,
                )

            # Case 4: caller drives the change. Mint owner token, mutate state,
            # register the in-flight transition.
            token = OwnerToken._new(target)
            self._state = target
            self._in_flight = _InFlight(
                from_state=from_state,
                target=target,
                trigger=trigger,
                owner_token=token,
                subscribers=[],
            )
            self._audit(
                from_state, target, trigger, "owned", reason=reason
            )
            return TransitionResult(
                changed=True,
                from_state=from_state,
                to_state=target,
                owner_token=token,
            )

    async def transition_complete(
        self,
        token: OwnerToken,
        final_state: SessionState,
    ) -> None:
        """Owner reports the transition's side effect has resolved.

        ``final_state`` is what the owner's side effect ended at — for a
        successful ``connect()`` from RECONNECTING, ``final_state=CONNECTED``;
        for an exhausted retry budget, ``final_state=DEAD``. The state machine
        applies ``final_state`` as a new transition (subject to matrix rules,
        but driven by an implicit INTERNAL trigger since this is the
        Transport's own machinery completing).

        Subscribers waiting on the in-flight handle are released with the
        final state.

        Raises ``TransitionError`` if the token is unknown (already completed
        or never issued).
        """
        async with self._lock:
            in_flight = self._in_flight
            if (
                in_flight is None
                or in_flight.target != token.target
                or in_flight.owner_token.token != token.token
            ):
                raise TransitionError(
                    f"unknown or already-completed owner token for target "
                    f"{token.target.value}"
                )

            # Apply final_state if different from the current in-flight target.
            # E.g. RECONNECTING in-flight with final_state=CONNECTED is the
            # normal handshake-success path; final_state=DEAD is the retry-
            # exhausted path. Both are legal INTERNAL transitions per the matrix.
            if final_state != self._state:
                # ── DEAD as universal emergency exit ─────────────────────────
                # DEAD is the terminal sink; it's never illegal to fall to it.
                # This is the catch-fire path for owners whose side effect
                # failed catastrophically (e.g. CONNECTED → IDLE_SLEEPING
                # whose disconnect crashed mid-way — the matrix doesn't give
                # INTERNAL on IDLE_SLEEPING → DEAD, but we still need an
                # escape so the driver-abandonment failure mode is avoidable.
                # Bypasses the INTERNAL-legality gate; everything else still
                # respects the matrix.
                if final_state == SessionState.DEAD:
                    from_state = self._state
                    self._state = SessionState.DEAD
                    self._audit(
                        from_state, SessionState.DEAD, Trigger.INTERNAL,
                        "emergency_completed",
                        reason=f"in-flight {in_flight.from_state.value} → "
                               f"{in_flight.target.value} failed to DEAD",
                    )
                else:
                    # Internal completion is implicitly trigger=INTERNAL for
                    # non-DEAD targets.
                    legal = LEGAL_TRANSITIONS.get((self._state, final_state))
                    if legal is None or Trigger.INTERNAL not in legal:
                        raise TransitionError(
                            f"illegal completion: {self._state.value} → "
                            f"{final_state.value} not INTERNAL-legal "
                            f"(DEAD is always legal as emergency exit)"
                        )
                    from_state = self._state
                    self._state = final_state
                    self._audit(
                        from_state, final_state, Trigger.INTERNAL, "completed",
                        reason=f"in-flight {in_flight.from_state.value} → "
                               f"{in_flight.target.value} resolved",
                    )
            else:
                # final_state == current; owner ended exactly where the matrix
                # parked us. Treat as completion of the in-flight transition.
                self._audit(
                    self._state, self._state, Trigger.INTERNAL, "completed",
                    reason=f"in-flight {in_flight.from_state.value} → "
                           f"{in_flight.target.value} resolved at target",
                )

            # Release subscribers.
            for handle in in_flight.subscribers:
                handle._resolve(final_state)
            # Clear in-flight entry.
            self._in_flight = None

    def _audit(
        self,
        from_state: SessionState,
        to_state: SessionState,
        trigger: Trigger,
        result: str,
        *,
        reason: str | None = None,
    ) -> None:
        """Emit a single audit line per transition request.

        Format: ``transport_state[<label>]: <from> → <to> via <trigger> [result=<r>]``
        plus optional ``reason=<r>`` suffix. Easy to grep for state lifecycle
        debugging; structured enough to feed into analytics later if we want
        state-distribution metrics.
        """
        line = (
            f"transport_state[{self._label}]: {from_state.value} → "
            f"{to_state.value} via {trigger.value} [result={result}]"
        )
        if reason:
            line += f" reason={reason!r}"
        _log(line)


@dataclass
class _InFlight:
    """Internal record of an in-flight transition. Not exposed to callers."""

    from_state: SessionState
    target: SessionState
    trigger: Trigger
    owner_token: OwnerToken
    subscribers: list[InFlightHandle]
