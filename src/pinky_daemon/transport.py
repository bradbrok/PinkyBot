"""Transport protocol — the contract that every agent transport backend honors.

PR2 of the #486 sequence. Typing-only PR: this module defines a
``runtime_checkable`` ``typing.Protocol`` that codifies the public surface
``streaming_session.py``'s ``StreamingSession`` already implements, with the
``SessionState`` / ``Trigger`` enums from PR1 substituted for the implicit
``is_connected + session_id`` bool inference.

This PR doesn't enforce that ``StreamingSession`` satisfies the protocol —
PR3 makes it formally adopt. Duck-typed Protocol means landing this PR
alone has no behavior change: the Protocol exists as documentation +
type-check target for new code (e.g. the upcoming ``TmuxSession`` for
Brad's Dymok test agent).

## Why a Protocol instead of an abstract base class

- Side-by-side architecture (Brad's directive on the tmux work): both
  ``StreamingSession`` (SDK) and ``TmuxSession`` (tmux REPL) live in tree
  indefinitely. A Protocol covers both backends without forcing inheritance
  from a shared base, which would couple the two implementations.

- StreamingSession exists today with a stable consumer surface (broker.py,
  api.py, watchdog, scheduler). Migrating it to inherit from an ABC requires
  changing its method signatures; Protocol just documents the surface
  consumers already rely on.

- Future backends (a Codex transport, a remote agent transport via the
  ferry, etc.) compose against this Protocol without circular import
  weirdness or shared-base lifecycle issues.

## Migration plan

- PR2 (this) — ``Transport`` Protocol + the state-machine-aware
  ``state`` property via the embedded ``StateMachine``. Shim helpers
  ``is_connected`` / ``is_idle_sleeping`` were deleted in PR4.
- PR3 — ``StreamingSession`` adopts (formally implements) ``Transport``
  by routing ``is_connected`` through its embedded ``StateMachine``.
  ``broker.py`` / ``api.py`` / ``scheduler.py`` / ``calendar.py`` type their
  StreamingSession parameters as ``Transport``.
- TmuxSession PR sequence — drafted in parallel against this Protocol.
  Dymok agent boots with ``PINKYBOT_TRANSPORT=tmux``; everything routes
  through the same Transport surface.
- PR4 — cleanup (LANDED): deleted ``is_connected`` / ``is_idle_sleeping`` shims and
  consume state directly via ``state == SessionState.X`` at the four caller
  files identified during pre-PR scoping.
- PR5 — rename (LANDED): renamed in-memory ``session_id`` to ``resume_handle``
  (per @murzik on PR #488 review; reaffirmed by @pushok in the same round).
  Scoped to the in-memory surface (Transport property, StreamingSession /
  CodexSession instance attrs, ``StreamingSessionConfig`` field, callback
  hooks). The persistence layer (``AgentRegistry.{get,set,list}_streaming_session_id``
  + DB column ``streaming_session_id``) deliberately keeps its old name to
  avoid bundling a migration into the rename PR. Re-typing as a
  backend-specific opaque object is still open as a separate concern.

## What's intentionally NOT in this Protocol

- Constructor / config. Each backend has its own config dataclass
  (``StreamingSessionConfig`` for SDK; ``TmuxSessionConfig`` for tmux).
  Construction signature varies; runtime surface doesn't.

- Internal helpers (``_check_context``, ``_reader_loop``, ``_analytics_*``,
  ``_strip_prompt_headers``, etc.). Implementation detail.

- Resume handle data. ``session_id`` (SDK) and tmux session name +
  transcript path (tmux) are kept private to each backend. Consumers don't
  need them today (the state machine carries the lifecycle signal); when
  the broker eventually needs to query "what's the resume handle for this
  session," that's a separate method that each backend implements
  differently. Out of scope for PR2.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from pinky_daemon.transport_state import SessionState


@runtime_checkable
class Transport(Protocol):
    """Public surface of an agent transport backend.

    Two backends are planned:

    1. ``StreamingSession`` — persistent Claude Agent SDK ``ClaudeSDKClient``
       per agent. The hot path for the existing SDK-backed fleet (barsik,
       pushok, ryzhik, persik, murzik, etc.).
    2. ``TmuxSession`` — interactive ``claude`` REPL inside a tmux session.
       The new backend for the Dymok test agent. Drives billing against the
       subscription's interactive limits instead of the capped SDK credit
       pool.

    Both implement this Protocol; consumers (broker, api, scheduler,
    watchdog) interact with whichever backend an agent is configured for
    without caring which one it is.

    ``runtime_checkable`` allows ``isinstance(obj, Transport)`` checks
    (structural; checks attribute names, not types). Useful for diagnostic
    code; the Protocol is otherwise enforced at type-check time only.
    """

    # ── Identity ─────────────────────────────────────────────────────────

    # Style note: ``agent_name`` is a plain attribute set at construction
    # time and not derived; ``id`` is a property because it combines
    # ``agent_name`` with the per-channel label. The mixed style is
    # intentional (data vs derived) — not an oversight.

    agent_name: str
    """Agent this transport is bound to. One transport per agent."""

    @property
    def id(self) -> str:
        """Stable identifier for this transport instance.

        For ``StreamingSession``: ``f"{agent_name}-{label or 'main'}"``.
        For ``TmuxSession``: same shape, label captures the tmux session
        name suffix when an agent runs multiple channels.

        Used by analytics, the conversation store, and admin dashboards.
        """
        ...

    # ── Lifecycle state ─────────────────────────────────────────────────

    @property
    def state(self) -> SessionState:
        """Current lifecycle state. Single source of truth for connection
        liveness, idle-sleep skip, and reconnect-in-flight signalling. The
        legacy ``is_connected`` / ``is_idle_sleeping`` shim properties
        were removed in PR4 of #486 — all readers branch on ``state``
        directly.

        Backends implement this in one of two ways:

        - ``StreamingSession`` — the full ``StateMachine`` matrix from
          ``transport_state.py``. Tracks UNINITIALIZED, RECONNECTING,
          CONNECTED, IDLE_SLEEPING, DEAD with explicit transitions and
          the no-flicker invariant.
        - ``CodexSession`` — a coarser derivation from internal
          ``_connected`` / ``_idle_sleeping`` bools. UNINITIALIZED and
          RECONNECTING are not modeled; CONNECTED / IDLE_SLEEPING / DEAD
          are sufficient for the polymorphic reader contract.
        """
        ...

    @property
    def resume_handle(self) -> str:
        """Resume handle, opaque shape per backend.

        For ``StreamingSession``: the Claude Code SDK session ID, persisted
        in the agent DB and used to drive ``--resume`` on cold-start.

        For ``TmuxSession``: TBD. Likely the tmux session name (since
        ``claude --continue`` resolves by cwd's most-recent transcript and
        the tmux session pins the cwd).

        **Opacity contract.** Consumers must treat this as opaque
        compatibility data — never derive lifecycle state from it. The
        state machine does NOT consult ``resume_handle`` for lifecycle
        decisions per issue #486 invariant 7 (resume_handle is data, not
        state). The pre-state-machine bug (#484, #486) was downstream of
        callers inferring "is_connected ∨ resume_handle ≠ ''" as state; PR3
        cut that inference at the source. PR5 renamed the property from
        ``session_id`` to ``resume_handle`` to clarify the intent (an opaque
        continuation token) and disambiguate from PinkyBot's own session UUID.

        Re-typing as a backend-specific opaque object (per @murzik on PR
        #488 review) is still open as a follow-up.
        """
        ...

    @property
    def stats(self) -> dict[str, Any]:
        """Snapshot of operational metrics: turn count, message count,
        error count, auto-restart count, current cost, current thinking
        effort, etc. Consumed by the admin dashboard and ``/agents`` API.

        Shape varies per backend (e.g. TmuxSession can't report
        ``cost_usd`` per the migration plan's accepted-loss list); callers
        should treat unknown keys defensively.
        """
        ...

    # ── Lifecycle methods ───────────────────────────────────────────────

    async def connect(self) -> None:
        """Bring the transport from a non-CONNECTED state into CONNECTED.

        For ``StreamingSession``: instantiate ``ClaudeSDKClient``, start
        the reader loop, register MCP servers, refresh wake context.

        For ``TmuxSession``: ``tmux new-session`` (or attach to existing
        on resume), launch ``claude --continue --dangerously-skip-
        permissions``, register hook event subscriptions.

        Drives ``RECONNECTING → CONNECTED`` via the state machine's
        INTERNAL trigger. Raises on irrecoverable connect failure (auth,
        config error); the StateMachine completes to DEAD via the
        emergency-exit path.
        """
        ...

    async def disconnect(self) -> None:
        """Bring the transport down. Idempotent.

        ``disconnect`` takes no intent parameter — lifecycle intent must be
        established by the caller through a higher-level method before
        invoking the raw disconnect:

        - ``idle_sleep`` drives ``CONNECTED → IDLE_SLEEPING`` via the state
          machine and then performs the disconnect.
        - Default (direct caller, no preceding intent set): drives
          ``→ DEAD``. Used for terminal shutdown paths.

        PR3 enforces this by routing the state-machine transitions through
        ``force_restart`` / ``idle_sleep`` / explicit shutdown paths;
        ``disconnect`` itself becomes the side-effect runner, not the
        intent declarer.
        """
        ...

    async def send(
        self,
        prompt: str,
        *,
        platform: str = "",
        chat_id: str = "",
        message_id: str = "",
        agent_hint: str = "",
    ) -> bool:
        """Send a message to the agent. Non-blocking.

        Args:
            prompt: The formatted message to send.
            platform: Platform the message came from (telegram, discord,
                slack, web, etc.). Used for routing the agent's response.
            chat_id: Chat / DM identifier on the platform.
            message_id: Source message_id for reaction routing.
            agent_hint: Reply-platform context appended to the query but
                NOT stored in conversation history. Lets the agent know
                where to reply without polluting the persistent record.

        Returns:
            Per-call handoff bool (#853): ``True`` when the transport
            accepted THIS message (SDK ``client.query`` succeeded / turn
            enqueued for a pane worker), ``False`` when it was dropped or
            the handoff failed. Handoff is NOT consumption — whether a
            truthy handoff also confirms consumption is declared by the
            transport's ``injection_confirms_consumption`` class attr
            (``True`` only for in-process SDK streams; ``False`` for
            external-pane tmux/codex transports). The broker combines
            both into ``InjectResult.confirmed``; only a confirmed inject
            may retire the durable comms inbox copy.

        **Caller contract.** Callers must ensure the transport is in
        ``SessionState.CONNECTED`` before invoking ``send``. ``send`` itself
        does **not** wait for in-flight transitions or trigger a reconnect
        — its only job is to push a turn at the underlying client when one
        is ready.

        The canonical safe-call pattern is in ``broker._route_streaming``
        (the wait-for-reconnect loop from PR #484): observe state +
        in-flight handle, hold the inbound message until the transport
        is either CONNECTED or terminally DEAD, then call ``send``.

        Behavior when called while NOT CONNECTED is **unspecified by this
        Protocol** beyond the return value:

        - Current ``StreamingSession`` drops silently and returns ``False``
          (see ``streaming_session.py:send``).
        - Future backends MAY raise instead; if they return, a dropped
          message MUST be reported as ``False``, never ``True``.

        Callers must not depend on the drop-vs-raise behavior; if you need
        delivery guarantees during non-CONNECTED windows, drive the
        wait/queue logic at the caller (broker's pattern).
        """
        ...

    async def force_restart(self) -> bool:
        """Tear down the current session and start a fresh one.

        Used by the agent's ``context_restart`` MCP tool when the agent
        decides to clear its working context, and by the watchdog for
        unrecoverable session failures.

        Returns ``True`` if the restart completed successfully, ``False``
        if blocked (e.g. restart guard rejecting because save_my_context
        wasn't called from the current session).

        Drives ``CONNECTED → RECONNECTING → CONNECTED|DEAD``.
        """
        ...

    async def idle_sleep(self) -> bool:
        """Disconnect the transport but preserve resume info so it can be
        cheaply re-woken on the next inbound message.

        Called by the watchdog when an agent exceeds its idle threshold.
        (Agent-initiated ``request_sleep`` was removed in #552 — it
        bypassed the IDLE_SLEEPING state and broke broker auto-wake.)

        Returns ``True`` if the sleep completed successfully.

        Drives ``CONNECTED → IDLE_SLEEPING``.
        """
        ...

    async def attempt_reconnect(self) -> None:
        """Best-effort reconnect after a transient transport failure.

        Called from ``send()``'s exception handler and from the watchdog's
        resurrection path. Internally drives ``CONNECTED → RECONNECTING``
        followed by either ``→ CONNECTED`` or ``→ DEAD`` after the retry
        budget (lives inside the Transport's RECONNECTING macro state per
        #486 invariant 5).

        **Migration-public, PR4 cleanup candidate.** Kept on the Protocol
        for now because ``send()``'s exception handler + ``scheduler.py``
        currently call it as a public method. Once PR3 / PR4 land,
        consumers should drive reconnects through ``request_transition``
        on the state machine (or the equivalent dedicated transition
        method) instead of calling ``attempt_reconnect`` directly. At that
        point this becomes a transport-internal helper and drops off the
        Protocol. Per @murzik on PR #488 review.
        """
        ...

    # ── Effort overrides (thinking-effort knob) ──────────────────────────

    @property
    def effective_effort(self) -> str:
        """Currently-applied thinking effort: ``"low"`` / ``"medium"`` /
        ``"high"`` / ``"xhigh"`` / ``"max"``.

        Reflects the *applied* effort post-resolution; the ``"auto"``
        sentinel accepted by ``set_effort`` (and the ``set_thinking_effort``
        MCP tool) is never returned here — it's resolved to one of the
        five concrete levels before the next turn runs.
        """
        ...

    def set_effort(self, level: str) -> None:
        """Override the thinking effort for this session.

        Backends that don't honor effort (e.g. TmuxSession may not, TBD)
        accept the call but log a warning and ignore. Consumers shouldn't
        rely on backend-specific honoring.
        """
        ...

    def clear_effort_override(self) -> None:
        """Drop any session-level effort override; revert to the agent's
        configured default on the next turn."""
        ...
