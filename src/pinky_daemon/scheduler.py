"""Agent Scheduler — cron-based wake system for agents.

Runs as an async background task. On each tick (every 30s), checks all
enabled schedules against the current time. When a schedule fires,
sends the wake prompt to the agent's main session.

Also handles heartbeat monitoring: if an agent's main session hasn't
sent a heartbeat within its configured interval, marks it as stale.

Cron parsing uses a minimal built-in parser (no external deps).
Supports standard 5-field cron: minute hour day month weekday.
"""

from __future__ import annotations

import asyncio
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from pinky_daemon.agent_registry import AgentRegistry, AgentSchedule


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# ── Cron Parser ──────────────────────────────────────────────

def cron_matches(cron_expr: str, dt: datetime) -> bool:
    """Check if a datetime matches a 5-field cron expression.

    Fields: minute hour day-of-month month day-of-week
    Supports: * (any), */N (step), N-M (range), N,M (list)
    Day-of-week: 0=Monday ... 6=Sunday (ISO)
    """
    fields = cron_expr.strip().split()
    if len(fields) != 5:
        return False

    values = [dt.minute, dt.hour, dt.day, dt.month, dt.isoweekday() % 7]
    limits = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 6)]

    for field, value, (lo, hi) in zip(fields, values, limits):
        if not _field_matches(field, value, lo, hi):
            return False
    return True


def _field_matches(field: str, value: int, lo: int, hi: int) -> bool:
    """Check if a single cron field matches a value."""
    for part in field.split(","):
        if _part_matches(part.strip(), value, lo, hi):
            return True
    return False


def _part_matches(part: str, value: int, lo: int, hi: int) -> bool:
    """Match a single part of a cron field (e.g., '*/5', '1-3', '7')."""
    if part == "*":
        return True

    if "/" in part:
        base, step_str = part.split("/", 1)
        step = int(step_str)
        if base == "*":
            return value % step == 0
        start = int(base)
        return value >= start and (value - start) % step == 0

    if "-" in part:
        start, end = part.split("-", 1)
        return int(start) <= value <= int(end)

    return value == int(part)


def next_cron_description(cron_expr: str) -> str:
    """Human-readable description of a cron expression."""
    fields = cron_expr.strip().split()
    if len(fields) != 5:
        return cron_expr

    minute, hour, dom, month, dow = fields

    parts = []
    if minute == "0" and hour != "*":
        parts.append(f"at {hour}:00")
    elif minute != "*" and hour != "*":
        parts.append(f"at {hour}:{minute.zfill(2)}")
    elif "*/" in minute:
        step = minute.split("/")[1]
        parts.append(f"every {step} minutes")
    elif "*/" in hour:
        step = hour.split("/")[1]
        parts.append(f"every {step} hours")

    if dow != "*":
        days = {
            "0": "Sun", "1": "Mon", "2": "Tue", "3": "Wed",
            "4": "Thu", "5": "Fri", "6": "Sat",
        }
        day_parts = [days.get(d.strip(), d) for d in dow.split(",")]
        parts.append(f"on {', '.join(day_parts)}")

    return " ".join(parts) if parts else cron_expr


# ── Scheduler ────────────────────────────────────────────────

class AgentScheduler:
    """Background scheduler for agent wake schedules and heartbeats."""

    def __init__(
        self,
        registry: AgentRegistry,
        *,
        wake_callback=None,
        heartbeat_callback=None,
        direct_send_callback=None,
        tick_interval: int = 30,
    ) -> None:
        self._registry = registry
        self._wake_callback = wake_callback  # async fn(agent_name, session_id, prompt)
        self._heartbeat_callback = heartbeat_callback  # async fn(agent_name, session_id)
        self._direct_send_callback = direct_send_callback  # async fn(agent_name, platform, chat_id, message)
        self._tick_interval = tick_interval
        self._running = False
        self._task: asyncio.Task | None = None
        self._last_check_minute: int = -1  # Prevent double-firing within same minute

    async def start(self) -> None:
        """Start the scheduler background loop."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())
        _log(f"scheduler: started (tick every {self._tick_interval}s)")

    async def stop(self) -> None:
        """Stop the scheduler."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        _log("scheduler: stopped")

    async def _loop(self) -> None:
        """Main scheduler loop."""
        while self._running:
            try:
                await self._tick()
            except Exception as e:
                _log(f"scheduler: error in tick: {e}")
            await asyncio.sleep(self._tick_interval)

    async def _tick(self) -> None:
        """Single scheduler tick — check schedules and heartbeats."""
        now = time.time()

        # Check cron schedules
        await self._check_schedules(now)

        # Check heartbeat health
        await self._check_heartbeats(now)

    async def _check_schedules(self, now: float) -> None:
        """Check all enabled schedules and fire any that match current time."""
        schedules = self._registry.get_all_schedules(enabled_only=True)
        if not schedules:
            return

        for schedule in schedules:
            try:
                tz = ZoneInfo(schedule.timezone)
            except (KeyError, ValueError):
                tz = ZoneInfo("America/Los_Angeles")

            dt = datetime.fromtimestamp(now, tz=tz)
            current_minute = dt.hour * 60 + dt.minute

            # Skip if we already checked this minute for this schedule
            if schedule.last_run > 0:
                last_dt = datetime.fromtimestamp(schedule.last_run, tz=tz)
                last_minute = last_dt.hour * 60 + last_dt.minute
                last_day = last_dt.date()
                if last_minute == current_minute and last_day == dt.date():
                    continue

            if cron_matches(schedule.cron, dt):
                _log(f"scheduler: firing schedule '{schedule.name}' for agent '{schedule.agent_name}' (direct_send={schedule.direct_send})")
                self._registry.update_schedule_last_run(schedule.id, now)

                if schedule.direct_send and schedule.target_channel and self._direct_send_callback:
                    # Direct send mode: route message through broker, not as agent input
                    try:
                        await self._direct_send_callback(
                            schedule.agent_name,
                            "telegram",  # Default platform
                            schedule.target_channel,
                            schedule.prompt,
                        )
                        _log(f"scheduler: direct-sent to {schedule.target_channel} for {schedule.agent_name}")
                    except Exception as e:
                        _log(f"scheduler: direct send failed for {schedule.agent_name}: {e}")
                elif self._wake_callback:
                    try:
                        main_session_id = f"{schedule.agent_name}-main"
                        await self._wake_callback(
                            schedule.agent_name,
                            main_session_id,
                            schedule.prompt or f"Scheduled wake: {schedule.name}",
                        )
                    except Exception as e:
                        _log(f"scheduler: wake callback failed for {schedule.agent_name}: {e}")

    async def _check_heartbeats(self, now: float) -> None:
        """Check heartbeat health for all agents with heartbeat_interval > 0."""
        agents = self._registry.list(enabled_only=True)

        for agent in agents:
            if agent.heartbeat_interval <= 0:
                continue

            hb = self._registry.get_latest_heartbeat(agent.name)
            if not hb:
                # No heartbeat ever recorded — mark stale
                self._registry.record_heartbeat(
                    agent.name, status="stale",
                    metadata={"reason": "no heartbeat recorded"},
                )
                continue

            age = now - hb.timestamp
            if age > agent.heartbeat_interval * 2:
                # Missed 2+ intervals — dead
                if hb.status != "dead":
                    self._registry.record_heartbeat(
                        agent.name, session_id=hb.session_id,
                        status="dead", context_pct=hb.context_pct,
                        message_count=hb.message_count,
                        metadata={"reason": f"no heartbeat for {int(age)}s"},
                    )
                    _log(f"scheduler: agent '{agent.name}' marked dead (no heartbeat for {int(age)}s)")
            elif age > agent.heartbeat_interval:
                # Missed 1 interval — stale
                if hb.status == "alive":
                    self._registry.record_heartbeat(
                        agent.name, session_id=hb.session_id,
                        status="stale", context_pct=hb.context_pct,
                        message_count=hb.message_count,
                        metadata={"reason": f"heartbeat overdue by {int(age - agent.heartbeat_interval)}s"},
                    )

    async def fire_now(self, agent_name: str, prompt: str = "") -> bool:
        """Manually trigger a wake for an agent."""
        if not self._wake_callback:
            return False

        main_session_id = f"{agent_name}-main"
        try:
            await self._wake_callback(agent_name, main_session_id, prompt or "Manual wake trigger")
            return True
        except Exception as e:
            _log(f"scheduler: manual wake failed for {agent_name}: {e}")
            return False

    @property
    def running(self) -> bool:
        return self._running
