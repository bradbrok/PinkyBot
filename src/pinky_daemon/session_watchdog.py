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


class SessionWatchdog:
    """Background service that detects and recovers stuck streaming sessions."""

    def __init__(
        self,
        *,
        streaming_sessions_fn: Callable[[], dict[str, dict[str, Any]]],
        recover_fn: Callable[[str, str, str], Coroutine] | None = None,
        alert_fn: Callable[[str, str], Coroutine] | None = None,
        agent_config_fn: Callable[[str], WatchdogConfig] | None = None,
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
        self._interval = check_interval

        self._states: dict[str, _AgentState] = {}
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
        if not cfg.enabled:
            return

        state_key = (snap.agent_name, snap.label)
        state = self._states.setdefault(state_key, _AgentState(
            last_progress_turns=snap.turns,
            last_progress_activity=snap.current_activity,
            last_progress_at=now,
        ))

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
