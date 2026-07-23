"""Session watchdog — detects and recovers stuck streaming sessions.

Periodically samples each streaming session's state and flags sessions
that appear stuck (no progress for an extended period while messages
queue up).  Two escalation tiers:

  1. **Warn** — notify the owner that agent X appears stuck.
  2. **Recover** — stop the session and reconnect automatically.

Global defaults can be overridden per-agent via ``watchdog_config`` on
the agent record.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine

from pinky_daemon.transport_state import SessionState
from pinky_daemon.watchdog_log import log_watchdog_decision

_log = logging.getLogger("pinky.watchdog").info
_warn = logging.getLogger("pinky.watchdog").warning
_critical = logging.getLogger("pinky.watchdog").critical

# Lifecycle states a session passes THROUGH on its way to CONNECTED. If a
# session sits in one of these past a bound it's wedged (#109) — the normal
# progress logic can't see it (it bails on ``not connected``) and the
# per-session ``_inflight_watchdog`` is CONNECTED-only. CONNECTED is watched
# by the progress logic; IDLE_SLEEPING (intentional rest) and DEAD (its own
# resurrection path) are deliberately excluded.
#
# VISIBILITY CAVEAT: this watchdog only samples *registered* sessions
# (``broker._streaming``). Production registers a session only AFTER
# ``ss.connect()`` returns (``_start_streaming_session``), so a fresh
# cold-start wedged in BOOTING is not yet registered and is NOT observed
# here — that gap needs a separate start-task watchdog (follow-up #110).
# RECONNECTING (and any transition on an already-registered session) IS
# observable and is the failure class #109 actually recovers. BOOTING is
# kept in the set so the branch stays correct if/when such sessions become
# observable; the branch logic itself is state-agnostic.
_TRANSITION_STATES = frozenset(
    {SessionState.BOOTING.value, SessionState.RECONNECTING.value}
)

# ── Defaults ────────────────────────────────────────────────────────

DEFAULT_CHECK_INTERVAL = 60  # seconds between watchdog sweeps
DEFAULT_WARN_AFTER = 600  # 10 min — notify owner
DEFAULT_RECOVER_AFTER = 900  # 15 min — auto-recover
DEFAULT_MODE = "recover"  # "alert" = warn only, "recover" = warn + auto-fix
# Lifecycle transition-age bounds (#109) — for sessions stuck in
# BOOTING/RECONNECTING. Kept above the reconnect-retry worst case (~220s)
# so a legitimately slow transition isn't force-recovered mid-retry.
DEFAULT_TRANSITION_WARN_AFTER = 240  # 4 min stuck in a transition — notify
DEFAULT_TRANSITION_RECOVER_AFTER = 360  # 6 min — hard-recover
# MCP-bind recovery (#663). Recover an agent whose MCP transport is wedged for
# the current gateway generation — the CC #60949 failure class: a daemon restart
# tears down the :8890 gateway, the resumed CC client never re-inits its MCP
# transport (404 not handled), so every pinky tool dies until a force-fresh
# relaunch. Signal: a heartbeat-enabled agent that records NO successful MCP
# round-trip for the current gateway epoch within a generous deadline. Flag-gated
# per agent via WatchdogConfig.mcp_recover (default OFF — soak on one agent first).
DEFAULT_MCP_UNBOUND_FLOOR = 240  # min deadline regardless of heartbeat interval
DEFAULT_MCP_UNBOUND_HEARTBEAT_MULT = 3  # deadline >= this * heartbeat_interval
DEFAULT_MCP_RECOVER_MIN_INTERVAL = 120  # global min secs between any two MCP recoveries
# #663 phase-2: a launch-time mcp_probe directive unanswered for this long is a
# *corroborating* wedge signal. Generous (LLM may take a few turns to act on the
# first-action directive). Only enriches an already-decided passive recovery —
# never triggers one, and never shortens the sustained-unbound deadline.
DEFAULT_MCP_PROBE_DEADLINE = 120
# checkable-via-history window (#663 / R2). An agent with heartbeat_interval==0
# (no scheduler-driven cadence) is still "checkable" if it emitted an
# AGENT-ORIGIN heartbeat within this window — i.e. it is an actively-heartbeating
# agent, so a sustained current-epoch unbound is a real wedge signal. Generous
# enough (>> the unbound deadline) that a freshly-orphaned low-cadence agent
# stays checkable through detection, but bounded so a long-quiet agent stops
# being a recovery target (and can't be falsely force-restarted).
DEFAULT_MCP_CHECKABLE_HEARTBEAT_MAX_AGE = 1800  # 30 min
# Failed owner pages retry at most once per normal sweep. The mismatch decision
# and critical log remain latched separately, so a transient destination outage
# cannot suppress delivery forever or spam the log (#902 review).
DEFAULT_TMUX_LIVENESS_NOTIFY_RETRY_AFTER = 60
# #902: after a dead-registered tmux mismatch alerts, require this much
# continuously observed health before re-arming.  A pane that flickers in and
# out across adjacent 60s sweeps therefore produces one page, while a genuinely
# new outage after a stable recovery still alerts.
DEFAULT_TMUX_LIVENESS_REARM_AFTER = 300  # 5 min continuously healthy


def compute_mcp_checkable(
    *,
    agent_exists: bool,
    epoch: str,
    heartbeat_interval: int,
    latest_agent_heartbeat_ts: float | None,
    now: float,
    max_heartbeat_age: float = DEFAULT_MCP_CHECKABLE_HEARTBEAT_MAX_AGE,
) -> bool:
    """Decide whether an agent's MCP bind status is *checkable* (#663 / R2).

    "Checkable" means: this agent is one we expect to be making MCP round-trips,
    so a missing current-epoch bind is a genuine wedge signal — not just a quiet
    or heartbeat-disabled agent. True iff a gateway epoch is established AND
    EITHER:

      * ``heartbeat_interval > 0`` — the agent is configured to heartbeat on a
        cadence (the classic scheduler-monitored agent); or
      * the agent emitted an AGENT-ORIGIN heartbeat within ``max_heartbeat_age``
        — covers active agents that carry ``heartbeat_interval == 0`` (wake-driven
        sidekicks like barsik) yet heartbeat in practice. The timestamp MUST come
        from ``get_latest_agent_heartbeat`` (NOT ``get_latest_heartbeat``) so the
        scheduler's synthetic ``server_presence`` rows can't make a wedged agent
        look checkable-and-healthy — #663 Murzik Finding #2.

    A pure function (no I/O) so the policy is unit-testable in isolation; the API
    layer threads in the live ``agents``/ledger reads.
    """
    if not (agent_exists and epoch):
        return False
    if heartbeat_interval > 0:
        return True
    if (
        latest_agent_heartbeat_ts is not None
        and (now - latest_agent_heartbeat_ts) <= max_heartbeat_age
    ):
        return True
    return False


@dataclass
class WatchdogConfig:
    """Per-agent watchdog settings (merges onto global defaults)."""

    enabled: bool = True
    mode: str = DEFAULT_MODE  # "alert" | "recover"
    warn_after_seconds: int = DEFAULT_WARN_AFTER
    recover_after_seconds: int = DEFAULT_RECOVER_AFTER
    require_backlog: bool = True  # only flag if pending queue > 0
    min_pending: int = 1
    # Lifecycle transition-age thresholds (#109). A session stuck in
    # BOOTING/RECONNECTING is a different failure class from a connected-
    # but-stalled session, so it gets its own bounds. Defaults sit above the
    # designed reconnect-retry envelope (worst case ~220s across spawn
    # attempts) so the watchdog never races a legitimate-but-slow transition,
    # while still recovering far faster than the 10-15min progress bounds.
    transition_warn_after_seconds: int = DEFAULT_TRANSITION_WARN_AFTER
    transition_recover_after_seconds: int = DEFAULT_TRANSITION_RECOVER_AFTER
    # #663 — auto force-fresh recovery of an MCP-unbound session. Default OFF;
    # enable per agent (start with one) to soak before fleet-wide rollout.
    mcp_recover: bool = False

    @classmethod
    def from_raw(cls, raw: dict | None) -> "WatchdogConfig":
        """Merge a per-agent ``watchdog_config`` dict onto the defaults.

        Single source of truth for the merge so a caller can't silently drop
        newly-added fields by forgetting to thread them (this is exactly how
        the #109 transition thresholds were initially missed in the API
        layer). Unknown keys are ignored; missing keys fall back to defaults.
        """
        raw = raw or {}
        if not raw:
            return cls()
        return cls(
            enabled=raw.get("enabled", cls.enabled),
            mode=raw.get("mode", cls.mode),
            warn_after_seconds=raw.get("warn_after_seconds", cls.warn_after_seconds),
            recover_after_seconds=raw.get(
                "recover_after_seconds", cls.recover_after_seconds
            ),
            require_backlog=raw.get("require_backlog", cls.require_backlog),
            min_pending=raw.get("min_pending", cls.min_pending),
            transition_warn_after_seconds=raw.get(
                "transition_warn_after_seconds",
                cls.transition_warn_after_seconds,
            ),
            transition_recover_after_seconds=raw.get(
                "transition_recover_after_seconds",
                cls.transition_recover_after_seconds,
            ),
            mcp_recover=raw.get("mcp_recover", cls.mcp_recover),
        )


@dataclass
class _SessionSnapshot:
    """Point-in-time observation of a streaming session."""

    agent_name: str
    label: str
    connected: bool
    turns: int
    pending: int
    current_activity: str
    sample_time: float
    state: str = ""  # lifecycle state value (e.g. "booting"); "" if unknown
    # Wall-clock epoch the session entered ``state``, stamped at grant time by
    # the transport's StateMachine (#206). 0.0 when the transport doesn't
    # expose it — the watchdog then falls back to its own sampled timing.
    state_entered_at: float = 0.0
    # #230 — live in-flight carve-out signal (from the transport's
    # ``stats["inflight_active"]``). True iff the session has an in-flight turn
    # that is genuinely busy right now (foreground tool / main- or background-
    # transcript growth). The progress watchdog skips warn+recover while True so
    # a long Workflow isn't force-recovered mid-flight — WITHOUT resetting the
    # stale clock, so it acts immediately once liveness stops. False/empty for
    # transports that don't expose it (codex/streaming/legacy).
    inflight_turns: int = 0
    inflight_active: bool = False
    inflight_liveness_reason: str = ""
    inflight_liveness_age_s: float | None = None


@dataclass
class _AgentState:
    """Tracked state for one agent across watchdog sweeps."""

    last_progress_turns: int = 0
    last_progress_activity: str = ""
    last_progress_at: float = field(default_factory=time.time)
    warned: bool = False
    recovered_at: float = 0.0  # grace period after recovery
    # Lifecycle transition-age tracking (#109). Kept separate from the
    # progress fields above — different failure class, different reset rules.
    # ``transition_since`` is sampling-based: it starts when a sweep first
    # observes the current transition state and resets when that state
    # changes (e.g. BOOTING → RECONNECTING).
    transition_state: str = ""  # "" when not in a tracked transition
    transition_since: float = 0.0
    transition_warned: bool = False
    transition_recovered_at: float = 0.0  # grace period after transition recovery
    # MCP-bind tracking (#663). ``mcp_unbound_since`` starts when a sweep first
    # observes a connected, heartbeat-enabled agent with no current-epoch MCP
    # success; it resets the moment a bind success appears (or the agent stops
    # being checkable), so the deadline always measures a *sustained* outage.
    mcp_unbound_since: float = 0.0
    mcp_recovered_at: float = 0.0  # grace period after an MCP-bind recovery


@dataclass
class _TmuxLivenessState:
    """Debounce state for one registered-active tmux agent (#902)."""

    session_name: str = ""
    missing_since: float = 0.0
    healthy_since: float = 0.0
    mismatch_logged: bool = False
    notified: bool = False
    last_notify_attempt_at: float = 0.0
    notification_attempts: int = 0
    last_checked_at: float = 0.0


class SessionWatchdog:
    """Background service that detects and recovers stuck streaming sessions."""

    def __init__(
        self,
        *,
        streaming_sessions_fn: Callable[[], dict[str, dict[str, Any]]],
        recover_fn: Callable[[str, str, str], Coroutine] | None = None,
        alert_fn: Callable[[str, str], Coroutine[Any, Any, bool | None]] | None = None,
        agent_config_fn: Callable[[str], WatchdogConfig] | None = None,
        mcp_bind_status_fn: Callable[[str], dict] | None = None,
        mcp_recover_fn: Callable[[str, str, str], Coroutine] | None = None,
        tmux_liveness_fn: Callable[
            [str, str, Any],
            Coroutine[Any, Any, bool | tuple[bool, str] | None],
        ] | None = None,
        check_interval: int = DEFAULT_CHECK_INTERVAL,
    ) -> None:
        """
        Args:
            streaming_sessions_fn:
                Returns ``broker._streaming`` — mapping of
                ``{agent_name: {label: session_obj}}``.
            recover_fn:
                ``async (agent_name, label, reason) -> None``.  Called when
                a session is deemed stuck and mode == "recover".
            alert_fn:
                ``async (agent_name, message) -> bool | None``. Called to send
                a warning to the owner. The tmux-liveness branch requires
                ``True`` to confirm delivery and rate-limits retries otherwise;
                legacy watchdog branches ignore the return value.
            agent_config_fn:
                ``(agent_name) -> WatchdogConfig``.  Returns merged
                per-agent config.  Falls back to global defaults if None.
            tmux_liveness_fn:
                ``async (agent_name, label, session) -> (alive, session_name)
                | bool | None``. Returns whether the tmux session exists, plus
                its actual resource name, for an eligible enabled, active,
                tmux-transport agent. ``None`` skips non-tmux agents. Detection
                is alert-only; this callback is never used to respawn or
                otherwise mutate a session.
            check_interval:
                Seconds between sweeps.
        """
        self._streaming_fn = streaming_sessions_fn
        self._recover_fn = recover_fn
        self._alert_fn = alert_fn
        self._config_fn = agent_config_fn or (lambda _: WatchdogConfig())
        # #663: bind-status lookup -> {checkable, bound, heartbeat_interval};
        # mcp_recover_fn force-fresh restarts a wedged-MCP session.
        self._mcp_bind_status_fn = mcp_bind_status_fn
        self._mcp_recover_fn = mcp_recover_fn
        self._tmux_liveness_fn = tmux_liveness_fn
        self._interval = check_interval

        self._states: dict[str, _AgentState] = {}
        self._tmux_liveness_states: dict[str, _TmuxLivenessState] = {}
        self._last_mcp_recover_at: float = 0.0  # global MCP-recover rate-limit
        self._task: asyncio.Task | None = None
        self._running = False

    # ── Lifecycle ────────────────────────────────────────────

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="session-watchdog")
        _log("watchdog started (interval=%ds)", self._interval)

    async def stop(self) -> None:
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        _log("watchdog stopped")

    @property
    def is_running(self) -> bool:
        return self._running

    # ── Main loop ────────────────────────────────────────────

    async def _loop(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(self._interval)
                if not self._running:
                    break
                await self._sweep()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                _warn("watchdog sweep error: %s", exc)
                await asyncio.sleep(self._interval)

    async def _sweep(self) -> None:
        """Sample all streaming sessions and check for stuck ones."""
        streaming = self._streaming_fn()
        now = time.time()
        seen_keys: set[tuple[str, str]] = set()
        tmux_checked_agents: set[str] = set()

        for agent_name, sessions in list(streaming.items()):
            for label, ss in list(sessions.items()):
                seen_keys.add((agent_name, label))
                snap = self._take_snapshot(agent_name, label, ss)
                # #902: a tmux resource is per agent (``pinky-<name>``), not
                # per broker label. Check at most one connected registration
                # per agent on each sweep.
                if agent_name not in tmux_checked_agents:
                    checked = await self._reconcile_tmux_liveness(
                        snap, ss, now,
                    )
                    if checked:
                        tmux_checked_agents.add(agent_name)
                await self._evaluate(snap, now)

        # Clean up state for sessions no longer streaming
        stale = [k for k in self._states if k not in seen_keys]
        for k in stale:
            del self._states[k]

        # No longer registered-active is a healthy/non-actionable observation
        # for debounce purposes. It clears the current mismatch and eventually
        # re-arms after a stable interval, without treating an intentional
        # broker unregister as another outage.
        for agent_name in list(self._tmux_liveness_states):
            if agent_name not in tmux_checked_agents:
                await self._evaluate_tmux_liveness(
                    agent_name, alive=True, now=now,
                )

    async def _reconcile_tmux_liveness(
        self,
        snap: _SessionSnapshot,
        ss: Any,
        now: float,
    ) -> bool:
        """Check one registered-active session against its tmux resource.

        Returns True once this agent has been classified for the current sweep,
        including a non-tmux skip or a failed diagnostic check. False means the
        supplied broker registration was not active, so another label may still
        be eligible.
        """
        if self._tmux_liveness_fn is None:
            return False
        if not snap.connected:
            return False
        try:
            result = await self._tmux_liveness_fn(
                snap.agent_name, snap.label, ss,
            )
        except Exception as exc:
            # A diagnostic failure is not proof the session is absent. Surface
            # it, but never page (or restart) a potentially healthy agent.
            _warn(
                "watchdog tmux liveness check failed for %s/%s: %s",
                snap.agent_name, snap.label, exc,
            )
            return True
        if result is None:
            # The callback rejected this agent (disabled/retired/non-tmux).
            # Clear any old mismatch using the same stable-health debounce.
            await self._evaluate_tmux_liveness(
                snap.agent_name, alive=True, now=now,
            )
            return True
        if isinstance(result, tuple):
            alive, session_name = result
        else:
            # Compatibility for simple/test callbacks. Production returns the
            # actual control name so Codex-over-tmux reports pinky-codex-*.
            alive, session_name = result, f"pinky-{snap.agent_name}"
        await self._evaluate_tmux_liveness(
            snap.agent_name,
            alive=bool(alive),
            now=now,
            session_name=session_name,
        )
        return True

    async def _evaluate_tmux_liveness(
        self,
        agent_name: str,
        *,
        alive: bool,
        now: float,
        session_name: str = "",
    ) -> None:
        """Log once, retry owner delivery, and debounce tmux mismatch flaps."""
        state = self._tmux_liveness_states.get(agent_name)
        if alive:
            if state is None:
                return
            state.last_checked_at = now
            if session_name:
                state.session_name = session_name
            if state.missing_since:
                state.missing_since = 0.0
                state.healthy_since = now
            elif not state.healthy_since:
                state.healthy_since = now
            if (now - state.healthy_since) >= DEFAULT_TMUX_LIVENESS_REARM_AFTER:
                # A continuously healthy window makes a later mismatch a new
                # incident. Drop the state entirely so the next miss pages
                # immediately and starts a fresh duration clock.
                del self._tmux_liveness_states[agent_name]
            return

        if state is None:
            state = _TmuxLivenessState()
            self._tmux_liveness_states[agent_name] = state
        state.session_name = session_name or state.session_name or f"pinky-{agent_name}"
        state.last_checked_at = now
        state.healthy_since = 0.0
        if not state.missing_since:
            state.missing_since = now

        dead_registered_for = max(0, int(now - state.missing_since))
        message = (
            f"🚨 Session liveness mismatch: {agent_name} is registered active, "
            f"but tmux session '{state.session_name}' is missing "
            f"(dead-registered for at least {dead_registered_for}s). "
            "Detection only; no restart was attempted."
        )
        # The mismatch decision/log and the owner-delivery confirmation are
        # independent. Log exactly once, but do not let a transient outage in
        # every configured destination permanently suppress the page.
        if not state.mismatch_logged:
            state.mismatch_logged = True
            _critical(message)

        if state.notified or self._alert_fn is None:
            return
        if (
            state.last_notify_attempt_at
            and (now - state.last_notify_attempt_at)
            < DEFAULT_TMUX_LIVENESS_NOTIFY_RETRY_AFTER
        ):
            return

        state.last_notify_attempt_at = now
        state.notification_attempts += 1
        try:
            delivered = await self._alert_fn(agent_name, message)
        except Exception as exc:
            _warn(
                "watchdog tmux liveness alert failed for %s: %s",
                agent_name, exc,
            )
            return
        if delivered is True:
            state.notified = True
            return
        _warn(
            "watchdog tmux liveness alert unconfirmed for %s; retrying in %ds",
            agent_name, DEFAULT_TMUX_LIVENESS_NOTIFY_RETRY_AFTER,
        )

    def _take_snapshot(
        self, agent_name: str, label: str, ss: Any
    ) -> _SessionSnapshot:
        stats = ss.stats if hasattr(ss, "stats") else {}
        state = stats.get("state", "")
        # Derive ``connected`` when the session doesn't expose it directly.
        # tmux ``stats`` exposes ``state`` but not ``connected``, so without
        # this the progress watchdog treated every tmux session as
        # permanently disconnected (and never flagged stalls). Codex/streaming
        # expose ``connected`` directly and keep their own value.
        connected = stats.get("connected")
        if connected is None:
            connected = state == SessionState.CONNECTED.value
        return _SessionSnapshot(
            agent_name=agent_name,
            label=label,
            connected=bool(connected),
            turns=stats.get("turns", 0),
            pending=(stats.get("pending_responses", 0) or stats.get("pending_messages", 0)),
            current_activity=stats.get("current_activity", ""),
            sample_time=time.time(),
            state=state,
            state_entered_at=stats.get("state_entered_at", 0.0) or 0.0,
            inflight_turns=stats.get("inflight_turns", 0) or 0,
            inflight_active=bool(stats.get("inflight_active", False)),
            inflight_liveness_reason=stats.get("inflight_liveness_reason", "") or "",
            inflight_liveness_age_s=stats.get("inflight_liveness_age_s"),
        )

    async def _evaluate(self, snap: _SessionSnapshot, now: float) -> None:
        cfg = self._config_fn(snap.agent_name)
        # The MCP-bind recovery branch (#663) is gated on its OWN flag
        # (cfg.mcp_recover), independent of the master cfg.enabled switch: an
        # agent can run with the progress/transition watchdog disabled yet still
        # opt into MCP-orphan auto-recovery. So return early only when BOTH are
        # off; everything below the MCP-bind branch stays gated on cfg.enabled.
        if not cfg.enabled and not cfg.mcp_recover:
            return

        state_key = (snap.agent_name, snap.label)
        state = self._states.setdefault(state_key, _AgentState(
            last_progress_turns=snap.turns,
            last_progress_activity=snap.current_activity,
            last_progress_at=now,
        ))

        # ── MCP-bind recovery branch (#663) ──────────────────────────
        # Self-gated on cfg.mcp_recover (a no-op when off), and evaluated BEFORE
        # the cfg.enabled gate so it works for agents whose progress/transition
        # watchdog is disabled. Also independent of the progress logic below: a
        # wedged-MCP session can still "make progress" on non-MCP turns, so
        # progress must not mask a dead transport. Returns True (and we stop) on
        # recovery. During a BOOTING/RECONNECTING transition snap.connected is
        # False, so this is a cheap no-op (clears the unbound clock) and the
        # transition branch below still handles the wedge.
        if await self._evaluate_mcp_bind(snap, state, cfg, now):
            return

        # Everything below — the lifecycle-transition and progress/backlog
        # watchdog — is gated on the master enable flag.
        if not cfg.enabled:
            return

        # ── Lifecycle transition-age branch (#109) ──────────────────
        # Handled BEFORE the progress/backlog logic: a session wedged in
        # BOOTING/RECONNECTING reports connected=False and would be dropped
        # by the ``not snap.connected`` guard below, and the per-session
        # _inflight_watchdog is CONNECTED-only — so nothing else ages it.
        if snap.state in _TRANSITION_STATES:
            await self._evaluate_transition(snap, state, cfg, now)
            return
        # Exited any transition (now connected/idle/dead/uninitialized) —
        # clear lifecycle tracking before the normal progress logic runs.
        if state.transition_state:
            state.transition_state = ""
            state.transition_since = 0.0
            state.transition_warned = False
            state.transition_recovered_at = 0.0

        # Detect progress: turn count increased or activity changed
        made_progress = (
            snap.turns > state.last_progress_turns
            or snap.current_activity != state.last_progress_activity
        )

        if made_progress:
            state.last_progress_turns = snap.turns
            state.last_progress_activity = snap.current_activity
            state.last_progress_at = now
            state.warned = False
            state.recovered_at = 0.0  # clear grace period
            return

        # No progress — how long?
        stale_seconds = now - state.last_progress_at

        # Grace period after recovery — don't re-flag immediately
        if state.recovered_at and (now - state.recovered_at) < cfg.warn_after_seconds:
            return

        # Must be connected and have backlog (if required)
        if not snap.connected:
            return
        if cfg.require_backlog and snap.pending < cfg.min_pending:
            return

        # #230 — in-flight Workflow/background carve-out. A session running a
        # live Workflow/Agent emits nothing this progress logic sees (turns and
        # current_activity stay static), so without this it would warn+recover
        # mid-flight once a message queues behind the long turn. Skip warn+recover
        # while the transport reports a genuinely-busy in-flight turn — but do
        # NOT reset ``last_progress_at``: the stale clock keeps aging BEHIND the
        # exemption, so the moment liveness stops (turn still not progressing) we
        # act on the next sweep instead of granting a fresh full window. Parity
        # for the daemon-level warn/recover path of what #692/#731 gave the
        # per-session _inflight_watchdog's force_restart path.
        if snap.inflight_active:
            log_watchdog_decision(
                watchdog="session", agent=snap.agent_name, label=snap.label,
                decision="skip", reason="inflight_active", state=snap.state,
                pending=snap.pending, turns=snap.turns,
                current_activity=snap.current_activity,
                progress_stale_s=stale_seconds,
                warn_after_s=cfg.warn_after_seconds,
                recover_after_s=cfg.recover_after_seconds,
                inflight_turns=snap.inflight_turns, inflight_active=True,
                inflight_liveness_reason=snap.inflight_liveness_reason,
                inflight_liveness_age_s=snap.inflight_liveness_age_s,
            )
            return

        # Warn tier
        if (
            not state.warned
            and stale_seconds >= cfg.warn_after_seconds
        ):
            state.warned = True
            msg = (
                f"⚠️ {snap.agent_name} appears stuck — "
                f"no progress for {int(stale_seconds // 60)}min, "
                f"activity: \"{snap.current_activity or 'idle'}\", "
                f"{snap.pending} pending message(s)."
            )
            _warn(msg)
            log_watchdog_decision(
                watchdog="session", agent=snap.agent_name, label=snap.label,
                decision="warn", reason="stale_progress", state=snap.state,
                pending=snap.pending, turns=snap.turns,
                current_activity=snap.current_activity,
                progress_stale_s=stale_seconds, warn_after_s=cfg.warn_after_seconds,
                inflight_turns=snap.inflight_turns, inflight_active=False,
            )
            if self._alert_fn:
                try:
                    await self._alert_fn(snap.agent_name, msg)
                except Exception as exc:
                    _warn("watchdog alert failed for %s: %s", snap.agent_name, exc)

        # Recover tier
        if (
            cfg.mode == "recover"
            and stale_seconds >= cfg.recover_after_seconds
        ):
            reason = (
                f"Stuck for {int(stale_seconds // 60)}min on "
                f"\"{snap.current_activity or 'idle'}\" with "
                f"{snap.pending} pending message(s)"
            )
            _warn("watchdog recovering %s: %s", snap.agent_name, reason)
            log_watchdog_decision(
                watchdog="session", agent=snap.agent_name, label=snap.label,
                decision="recover", reason="stale_progress", state=snap.state,
                pending=snap.pending, turns=snap.turns,
                current_activity=snap.current_activity,
                progress_stale_s=stale_seconds,
                recover_after_s=cfg.recover_after_seconds,
                inflight_turns=snap.inflight_turns, inflight_active=False,
            )
            if self._recover_fn:
                try:
                    await self._recover_fn(snap.agent_name, snap.label, reason)
                    # Reset state after recovery with grace period
                    now_t = time.time()
                    state.last_progress_at = now_t
                    state.recovered_at = now_t
                    state.warned = False
                    state.last_progress_turns = 0
                    state.last_progress_activity = ""
                except Exception as exc:
                    _warn(
                        "watchdog recovery failed for %s: %s",
                        snap.agent_name, exc,
                    )

    async def _evaluate_transition(
        self, snap: _SessionSnapshot, state: _AgentState,
        cfg: WatchdogConfig, now: float,
    ) -> None:
        """Warn / hard-recover a session stuck in a BOOTING/RECONNECTING transition.

        Separate failure class from the progress watchdog: the session never
        completes its lifecycle transition (vs. a CONNECTED session making no
        progress). Recovery deliberately goes through the generic
        ``_recover_fn`` (disconnect → clear persisted id → unregister → fresh
        ``_ensure_streaming_session``), which abandons the wedged state-machine
        owner. It does NOT call ``force_restart`` — that primitive starts by
        requesting RECONNECTING, the wrong edge from BOOTING and a no-op/reject
        from RECONNECTING, i.e. it would try to drive another edge through the
        already-wedged owner.

        Age is sampling-based: it starts when a sweep first observes the
        current transition state and resets if that state changes
        (BOOTING → RECONNECTING counts as a fresh transition). Backlog is NOT
        required — a session stuck BOOTING can't receive anything regardless of
        queue depth.
        """
        # Anchor the age clock. Prefer the session-provided ``state_entered_at``
        # (stamped at GRANT time by the StateMachine — precise) over the sampled
        # timer (starts on the first sweep that observes the state — up to one
        # interval, ~60s, late). Fall back to sampling for transports that don't
        # expose it (#206; Murzik).
        authoritative = snap.state_entered_at if snap.state_entered_at > 0 else None
        if authoritative is not None:
            # (Re)anchor whenever the tracked state value changes OR the entry
            # time advanced (a same-value re-entry, e.g. a fresh RECONNECTING
            # grant). No early return: the real entry time may ALREADY exceed a
            # threshold, and the sampled path would have wrongly reset to ``now``.
            if state.transition_state != snap.state or state.transition_since != authoritative:
                state.transition_state = snap.state
                state.transition_since = authoritative
                state.transition_warned = False
        else:
            # Sampled fallback: start the timer on first observation / on change.
            # No age has accrued yet, so don't warn/recover on the same sweep.
            if state.transition_state != snap.state:
                state.transition_state = snap.state
                state.transition_since = now
                state.transition_warned = False
                return

        age = now - state.transition_since

        # Grace window after a recovery so we don't immediately re-fire on the
        # fresh session's own (legitimate) BOOTING.
        if (
            state.transition_recovered_at
            and (now - state.transition_recovered_at)
            < cfg.transition_warn_after_seconds
        ):
            return

        # Warn tier
        if (
            not state.transition_warned
            and age >= cfg.transition_warn_after_seconds
        ):
            state.transition_warned = True
            msg = (
                f"⚠️ {snap.agent_name} stuck in '{snap.state}' for "
                f"{int(age // 60)}min — lifecycle transition not completing."
            )
            _warn(msg)
            if self._alert_fn:
                try:
                    await self._alert_fn(snap.agent_name, msg)
                except Exception as exc:
                    _warn(
                        "watchdog transition alert failed for %s: %s",
                        snap.agent_name, exc,
                    )

        # Recover tier — hard recovery via the generic callback (abandon the
        # wedged owner + fresh start). Never force_restart.
        if (
            cfg.mode == "recover"
            and age >= cfg.transition_recover_after_seconds
        ):
            reason = (
                f"Stuck in '{snap.state}' lifecycle transition for "
                f"{int(age // 60)}min"
            )
            _warn(
                "watchdog transition-recovering %s: %s",
                snap.agent_name, reason,
            )
            if self._recover_fn:
                try:
                    await self._recover_fn(snap.agent_name, snap.label, reason)
                    # Reset with a grace period; the fresh session will pass
                    # through BOOTING legitimately and must not re-trip.
                    now_t = time.time()
                    state.transition_recovered_at = now_t
                    state.transition_since = now_t
                    state.transition_warned = False
                    state.transition_state = ""
                except Exception as exc:
                    _warn(
                        "watchdog transition recovery failed for %s: %s",
                        snap.agent_name, exc,
                    )

    async def _evaluate_mcp_bind(
        self, snap: _SessionSnapshot, state: _AgentState,
        cfg: WatchdogConfig, now: float,
    ) -> bool:
        """Force-fresh recover an agent whose MCP transport is wedged for the
        current gateway generation (#663). Returns True if a recovery fired.

        Flag-gated (``cfg.mcp_recover``). The bind signal is the agent's own
        heartbeat: ``send_heartbeat`` is an MCP tool call, so a heartbeat-enabled
        agent that records NO successful MCP round-trip for the CURRENT gateway
        epoch within a generous deadline (>> its heartbeat interval) has a dead
        MCP transport — and only a force-fresh relaunch re-binds it (CC #60949;
        a plain ``--continue`` re-resume inherits the dead transport).

        Safety rails: acts only on CONNECTED + heartbeat-enabled agents (a quiet
        or heartbeat-disabled agent never false-trips); the deadline measures a
        *sustained* unbound observation (so a freshly-rebuilt gateway gives every
        agent the full window to re-bind via its next heartbeat — no deploy-time
        storm); a per-agent post-recovery grace plus a global min-interval
        backstop bound the recovery rate; any status-lookup error is swallowed
        (never kill a healthy session on a diagnostic hiccup).
        """
        if (
            not cfg.mcp_recover
            or self._mcp_bind_status_fn is None
            or self._mcp_recover_fn is None
        ):
            return False
        if not snap.connected:
            if state.mcp_unbound_since:
                # We were tracking a connected-but-unbound session and it just
                # went disconnected — torn down (external restart / crash /
                # warm-wake teardown) before MCP-recover could fire. Surface it
                # so an orphan recovered by another path isn't invisible (#667).
                _log(
                    "mcp-bind: %s was unbound ~%ds then went disconnected before "
                    "MCP-recover deadline — recovered/torn-down elsewhere (#667)",
                    snap.agent_name, int(now - state.mcp_unbound_since),
                )
            state.mcp_unbound_since = 0.0
            return False

        try:
            status = self._mcp_bind_status_fn(snap.agent_name) or {}
        except Exception as exc:
            _warn("watchdog mcp-bind status failed for %s: %s", snap.agent_name, exc)
            return False

        # Not checkable this phase (heartbeat disabled / no gateway epoch yet) —
        # NOT unhealthy; clear any clock and move on.
        if not status.get("checkable"):
            state.mcp_unbound_since = 0.0
            return False
        # Healthy: a current-epoch MCP success exists.
        if status.get("bound"):
            state.mcp_unbound_since = 0.0
            return False

        # Unbound for the current epoch — track a *sustained* outage.
        if state.mcp_unbound_since == 0.0:
            state.mcp_unbound_since = now
            _log(
                "mcp-bind: %s unbound for current gateway epoch — starting "
                "MCP-recover clock (#667 diag)",
                snap.agent_name,
            )
            return False
        unbound_for = now - state.mcp_unbound_since

        hb = max(int(status.get("heartbeat_interval", 0) or 0), 0)
        deadline = max(DEFAULT_MCP_UNBOUND_FLOOR, DEFAULT_MCP_UNBOUND_HEARTBEAT_MULT * hb)

        # Per-agent grace after a recovery: the fresh session needs time to come
        # up and emit its first heartbeat before it could be flagged again.
        if state.mcp_recovered_at and (now - state.mcp_recovered_at) < deadline:
            return False
        if unbound_for < deadline:
            return False

        # Global backstop against a fleet restart storm (e.g. if a detection bug
        # ever flagged many agents at once): cap the MCP-recover rate fleet-wide.
        if (
            self._last_mcp_recover_at
            and (now - self._last_mcp_recover_at) < DEFAULT_MCP_RECOVER_MIN_INTERVAL
        ):
            _warn(
                "watchdog: %s MCP-unbound for %ds but global recover rate-limit "
                "active — deferring", snap.agent_name, int(unbound_for),
            )
            return False

        reason = (
            f"MCP transport unbound for current gateway epoch ~{int(unbound_for)}s "
            f"(no successful MCP round-trip; deadline {deadline}s) — force-fresh recover"
        )
        # #663 phase-2: CORROBORATE with the active-probe signal. The recovery is
        # already decided by the passive sustained-unbound machinery above; this
        # only enriches the reason/audit when the agent also ignored its
        # launch-time mcp_probe directive past the probe deadline. It never
        # triggers recovery on its own and never shortens the deadline.
        probe = status.get("probe_request") or {}
        if (
            probe.get("current")
            and not probe.get("fulfilled")
            and (probe.get("age_sec") or 0) >= DEFAULT_MCP_PROBE_DEADLINE
        ):
            reason += (
                f"; missed requested probe (launch_id={probe.get('launch_id')}, "
                f"nonce={probe.get('nonce')}, ~{int(probe.get('age_sec') or 0)}s "
                "unanswered) — corroborates wedge"
            )
        _warn("watchdog MCP-recovering %s: %s", snap.agent_name, reason)
        try:
            await self._mcp_recover_fn(snap.agent_name, snap.label, reason)
        except Exception as exc:
            _warn("watchdog MCP recovery failed for %s: %s", snap.agent_name, exc)
            return False

        # Recovery fired — record rate-limit + grace, reset progress tracking
        # (the fresh session restarts the progress clock).
        self._last_mcp_recover_at = now
        state.mcp_recovered_at = now
        state.mcp_unbound_since = 0.0
        state.last_progress_at = now
        state.warned = False
        state.last_progress_turns = 0
        state.last_progress_activity = ""
        if self._alert_fn:
            try:
                await self._alert_fn(
                    snap.agent_name,
                    f"🔧 Auto-recovered {snap.agent_name}: MCP transport was wedged "
                    f"(unbound from the gateway ~{int(unbound_for)}s) — force-fresh "
                    f"relaunched. (#663)",
                )
            except Exception as exc:
                _warn(
                    "watchdog mcp-recover alert failed for %s: %s",
                    snap.agent_name, exc,
                )
        return True

    # ── Status ───────────────────────────────────────────────

    def status(self) -> dict:
        """Return current watchdog state for diagnostics."""
        now = time.time()
        sessions = {}
        for key, state in self._states.items():
            agent_name, label = key
            stale_s = now - state.last_progress_at
            display_key = f"{agent_name}/{label}" if label else agent_name
            sessions[display_key] = {
                "agent_name": agent_name,
                "label": label,
                "last_progress_turns": state.last_progress_turns,
                "last_progress_activity": state.last_progress_activity,
                "stale_seconds": round(stale_s, 1),
                "warned": state.warned,
                "transition_state": state.transition_state,
                "transition_age": (
                    round(now - state.transition_since, 1)
                    if state.transition_since else 0.0
                ),
                "transition_warned": state.transition_warned,
            }
        tmux_liveness = {
            agent_name: {
                "session_name": state.session_name,
                "missing": bool(state.missing_since),
                "dead_registered_seconds": (
                    round(now - state.missing_since, 1)
                    if state.missing_since else 0.0
                ),
                "mismatch_logged": state.mismatch_logged,
                "notification_latched": state.notified,
                "notification_attempts": state.notification_attempts,
                "healthy_seconds": (
                    round(now - state.healthy_since, 1)
                    if state.healthy_since else 0.0
                ),
            }
            for agent_name, state in self._tmux_liveness_states.items()
        }
        return {
            "running": self._running,
            "check_interval": self._interval,
            "agents": sessions,  # kept as "agents" for API compat
            "tmux_liveness": tmux_liveness,
        }
