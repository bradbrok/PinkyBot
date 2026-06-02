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

_log = logging.getLogger("pinky.watchdog").info
_warn = logging.getLogger("pinky.watchdog").warning

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
# checkable-via-history window (#663 / R2). An agent with heartbeat_interval==0
# (no scheduler-driven cadence) is still "checkable" if it emitted an
# AGENT-ORIGIN heartbeat within this window — i.e. it is an actively-heartbeating
# agent, so a sustained current-epoch unbound is a real wedge signal. Generous
# enough (>> the unbound deadline) that a freshly-orphaned low-cadence agent
# stays checkable through detection, but bounded so a long-quiet agent stops
# being a recovery target (and can't be falsely force-restarted).
DEFAULT_MCP_CHECKABLE_HEARTBEAT_MAX_AGE = 1800  # 30 min


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


class SessionWatchdog:
    """Background service that detects and recovers stuck streaming sessions."""

    def __init__(
        self,
        *,
        streaming_sessions_fn: Callable[[], dict[str, dict[str, Any]]],
        recover_fn: Callable[[str, str, str], Coroutine] | None = None,
        alert_fn: Callable[[str, str], Coroutine] | None = None,
        agent_config_fn: Callable[[str], WatchdogConfig] | None = None,
        mcp_bind_status_fn: Callable[[str], dict] | None = None,
        mcp_recover_fn: Callable[[str, str, str], Coroutine] | None = None,
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
                ``async (agent_name, message) -> None``.  Called to send
                a warning to the owner.
            agent_config_fn:
                ``(agent_name) -> WatchdogConfig``.  Returns merged
                per-agent config.  Falls back to global defaults if None.
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
        self._interval = check_interval

        self._states: dict[str, _AgentState] = {}
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

        for agent_name, sessions in list(streaming.items()):
            for label, ss in list(sessions.items()):
                seen_keys.add((agent_name, label))
                snap = self._take_snapshot(agent_name, label, ss)
                await self._evaluate(snap, now)

        # Clean up state for sessions no longer streaming
        stale = [k for k in self._states if k not in seen_keys]
        for k in stale:
            del self._states[k]

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
        # (Re)start the sampled timer on first observation of this transition
        # state, or when it changed since the last sweep. No age has accrued
        # yet, so don't warn/recover on the same sweep.
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
        sessions = {}
        for key, state in self._states.items():
            agent_name, label = key
            stale_s = time.time() - state.last_progress_at
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
                    round(time.time() - state.transition_since, 1)
                    if state.transition_since else 0.0
                ),
                "transition_warned": state.transition_warned,
            }
        return {
            "running": self._running,
            "check_interval": self._interval,
            "agents": sessions,  # kept as "agents" for API compat
        }
