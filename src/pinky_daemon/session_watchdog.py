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

_log = logging.getLogger("pinky.watchdog").info
_warn = logging.getLogger("pinky.watchdog").warning

# ── Defaults ────────────────────────────────────────────────────────

DEFAULT_CHECK_INTERVAL = 60  # seconds between watchdog sweeps
DEFAULT_WARN_AFTER = 600  # 10 min — notify owner
DEFAULT_RECOVER_AFTER = 900  # 15 min — auto-recover
DEFAULT_MODE = "recover"  # "alert" = warn only, "recover" = warn + auto-fix


@dataclass
class WatchdogConfig:
    """Per-agent watchdog settings (merges onto global defaults)."""

    enabled: bool = True
    mode: str = DEFAULT_MODE  # "alert" | "recover"
    warn_after_seconds: int = DEFAULT_WARN_AFTER
    recover_after_seconds: int = DEFAULT_RECOVER_AFTER
    require_backlog: bool = True  # only flag if pending queue > 0
    min_pending: int = 1


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


@dataclass
class _AgentState:
    """Tracked state for one agent across watchdog sweeps."""

    last_progress_turns: int = 0
    last_progress_activity: str = ""
    last_progress_at: float = field(default_factory=time.time)
    warned: bool = False


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
        seen_agents: set[str] = set()

        for agent_name, sessions in streaming.items():
            for label, ss in sessions.items():
                seen_agents.add(agent_name)
                snap = self._take_snapshot(agent_name, label, ss)
                await self._evaluate(snap, now)

        # Clean up state for agents no longer streaming
        stale = [k for k in self._states if k not in seen_agents]
        for k in stale:
            del self._states[k]

    def _take_snapshot(
        self, agent_name: str, label: str, ss: Any
    ) -> _SessionSnapshot:
        stats = ss.stats if hasattr(ss, "stats") else {}
        return _SessionSnapshot(
            agent_name=agent_name,
            label=label,
            connected=stats.get("connected", False),
            turns=stats.get("turns", 0),
            pending=stats.get("pending_responses", 0),
            current_activity=stats.get("current_activity", ""),
            sample_time=time.time(),
        )

    async def _evaluate(self, snap: _SessionSnapshot, now: float) -> None:
        cfg = self._config_fn(snap.agent_name)
        if not cfg.enabled:
            return

        state = self._states.setdefault(snap.agent_name, _AgentState(
            last_progress_turns=snap.turns,
            last_progress_activity=snap.current_activity,
            last_progress_at=now,
        ))

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
            return

        # No progress — how long?
        stale_seconds = now - state.last_progress_at

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
                    # Reset state after recovery
                    state.last_progress_at = time.time()
                    state.warned = False
                    state.last_progress_turns = 0
                    state.last_progress_activity = ""
                except Exception as exc:
                    _warn(
                        "watchdog recovery failed for %s: %s",
                        snap.agent_name, exc,
                    )

    # ── Status ───────────────────────────────────────────────

    def status(self) -> dict:
        """Return current watchdog state for diagnostics."""
        agents = {}
        for name, state in self._states.items():
            stale_s = time.time() - state.last_progress_at
            agents[name] = {
                "last_progress_turns": state.last_progress_turns,
                "last_progress_activity": state.last_progress_activity,
                "stale_seconds": round(stale_s, 1),
                "warned": state.warned,
            }
        return {
            "running": self._running,
            "check_interval": self._interval,
            "agents": agents,
        }
