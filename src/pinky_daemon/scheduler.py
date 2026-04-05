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
import json
import sys
import time
from datetime import date, datetime
from zoneinfo import ZoneInfo

from pinky_daemon.agent_registry import AgentRegistry


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
    """Background scheduler for agent wake schedules and heartbeats.

    Supports clock-aligned wakes: agents wake at wall-clock boundaries
    (e.g., :00/:30 for 30m interval, :00 for 1h) instead of arbitrary
    intervals from last activity.

    Also supports auto-sleep: agents are put to sleep after a configurable
    number of hours with no activity.
    """

    def __init__(
        self,
        registry: AgentRegistry,
        *,
        wake_callback=None,
        heartbeat_callback=None,
        direct_send_callback=None,
        auto_sleep_callback=None,
        dream_callback=None,
        streaming_sessions_fn=None,
        comms_cleanup_fn=None,
        trigger_store=None,
        activity=None,
        tick_interval: int = 30,
    ) -> None:
        self._registry = registry
        self._wake_callback = wake_callback  # async fn(agent_name, session_id, prompt)
        self._heartbeat_callback = heartbeat_callback  # async fn(agent_name, session_id)
        self._direct_send_callback = direct_send_callback  # async fn(agent_name, platform, chat_id, message)
        self._auto_sleep_callback = auto_sleep_callback  # async fn(agent_name, reason)
        self._dream_callback = dream_callback  # async fn(agent_name, agent_config)
        self._streaming_sessions_fn = streaming_sessions_fn  # fn() -> dict[name, StreamingSession]
        self._comms_cleanup_fn = comms_cleanup_fn  # fn() -> int (expired inbox cleanup)
        self._trigger_store = trigger_store  # TriggerStore | None
        self._activity = activity  # ActivityStore | None
        self._tick_interval = tick_interval
        self._running = False
        self._task: asyncio.Task | None = None
        self._last_clock_slot: dict[str, int] = {}  # agent_name -> last fired clock slot (minutes since midnight)
        self._last_dream_check: dict[str, tuple] = {}  # agent_name -> (date_str, cron-minute) dedup key

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
        """Single scheduler tick — check schedules, heartbeats, clock-aligned wakes, auto-sleep, idle sessions, expired messages, dreams, and url watchers."""
        now = time.time()

        # Check cron schedules
        await self._check_schedules(now)

        # Check clock-aligned wakes
        await self._check_clock_aligned_wakes(now)

        # Check heartbeat health
        await self._check_heartbeats(now)

        # Check auto-sleep (idle too long)
        await self._check_auto_sleep(now)

        # Check for idle streaming sessions
        await self._check_idle_sessions(now)

        # Cleanup expired inbox messages
        self._cleanup_expired_messages()

        # Check dream schedules
        await self._check_dreams(now)

        # Check URL watcher triggers
        await self._check_url_watchers(now)

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
                if self._activity:
                    try:
                        self._activity.log(schedule.agent_name, "schedule_fired", f"Schedule '{schedule.name}' fired")
                    except Exception:
                        pass
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

    async def _check_idle_sessions(self, now: float) -> None:
        """Put idle streaming sessions to sleep to save resources."""
        if not self._streaming_sessions_fn:
            return

        try:
            sessions = self._streaming_sessions_fn()
        except Exception:
            return

        for name, session_dict in sessions.items():
            for label, ss in session_dict.items():
                if not ss.is_connected:
                    continue

                idle_timeout = ss._config.idle_timeout
                if idle_timeout <= 0:
                    continue

                idle_seconds = now - ss.last_active
                if idle_seconds >= idle_timeout:
                    _log(f"scheduler: {name}/{label} idle for {int(idle_seconds)}s (threshold: {idle_timeout}s) — auto-sleeping")
                    if self._activity:
                        try:
                            self._activity.log(name, "agent_sleep", f"{name} auto-slept (idle timeout)")
                        except Exception:
                            pass
                    try:
                        await ss.idle_sleep()
                    except Exception as e:
                        _log(f"scheduler: idle sleep failed for {name}/{label}: {e}")

    def _cleanup_expired_messages(self) -> None:
        """Remove expired inbox entries via the comms cleanup callback."""
        if not self._comms_cleanup_fn:
            return
        try:
            count = self._comms_cleanup_fn()
            if count > 0:
                _log(f"scheduler: cleaned up {count} expired inbox messages")
        except Exception as e:
            _log(f"scheduler: expired message cleanup failed: {e}")

    async def _check_clock_aligned_wakes(self, now: float) -> None:
        """Check agents with clock-aligned wake intervals and fire if a new slot is due.

        For a 30m interval, wakes at :00 and :30 each hour.
        For a 60m interval, wakes at :00 each hour.
        For a 15m interval, wakes at :00, :15, :30, :45.
        """
        agents = self._registry.list(enabled_only=True)

        for agent in agents:
            if agent.wake_interval <= 0:
                continue

            interval_minutes = agent.wake_interval // 60
            if interval_minutes <= 0:
                continue

            # Get current time in a reasonable timezone
            try:
                tz = ZoneInfo("America/Los_Angeles")
            except (KeyError, ValueError):
                tz = ZoneInfo("UTC")

            dt = datetime.fromtimestamp(now, tz=tz)
            current_minutes = dt.hour * 60 + dt.minute

            if agent.clock_aligned:
                # Clock-aligned: fire at wall-clock boundaries
                current_slot = (current_minutes // interval_minutes) * interval_minutes
                last_slot = self._last_clock_slot.get(agent.name, -1)

                if current_slot == last_slot:
                    continue  # Already fired this slot

                self._last_clock_slot[agent.name] = current_slot
                _log(f"scheduler: clock-aligned wake for '{agent.name}' at :{dt.minute:02d} (slot {current_slot}, interval {interval_minutes}m)")
            else:
                # Legacy: interval-based from last activity
                hb = self._registry.get_latest_heartbeat(agent.name)
                last_active = hb.timestamp if hb else 0
                if last_active > 0 and (now - last_active) < agent.wake_interval:
                    continue

            if self._wake_callback:
                try:
                    session_id = f"{agent.name}-main"
                    prompt = self._registry.get_heartbeat_prompt()
                    tz_name = agent.dream_timezone or self._registry.get_default_timezone() or "UTC"
                    tz = ZoneInfo(tz_name)
                    ts = datetime.now(tz).strftime("%Y-%m-%d %H:%M %Z")
                    prompt = f"[{ts}] {prompt}"
                    await self._wake_callback(
                        agent.name, session_id,
                        prompt,
                    )
                except Exception as e:
                    _log(f"scheduler: clock-aligned wake failed for {agent.name}: {e}")

    async def _check_auto_sleep(self, now: float) -> None:
        """Auto-sleep agents that have been idle beyond their auto_sleep_hours threshold."""
        if not self._streaming_sessions_fn:
            return

        agents = self._registry.list(enabled_only=True)

        for agent in agents:
            if agent.auto_sleep_hours <= 0:
                continue

            threshold_seconds = agent.auto_sleep_hours * 3600

            # Check streaming sessions for this agent
            try:
                sessions = self._streaming_sessions_fn()
            except Exception:
                continue

            agent_sessions = sessions.get(agent.name, {})
            if not agent_sessions:
                continue

            for label, ss in agent_sessions.items():
                if not ss.is_connected:
                    continue

                idle_seconds = now - ss.last_active
                if idle_seconds >= threshold_seconds:
                    _log(f"scheduler: auto-sleep for '{agent.name}/{label}' — idle {idle_seconds / 3600:.1f}h (threshold: {agent.auto_sleep_hours}h)")
                    if self._auto_sleep_callback:
                        try:
                            await self._auto_sleep_callback(
                                agent.name,
                                f"Auto-sleep: idle for {idle_seconds / 3600:.1f}h (threshold: {agent.auto_sleep_hours}h)",
                            )
                        except Exception as e:
                            _log(f"scheduler: auto-sleep callback failed for {agent.name}: {e}")
                    else:
                        # Fallback: use idle_sleep on the session directly
                        try:
                            await ss.idle_sleep()
                        except Exception as e:
                            _log(f"scheduler: auto-sleep idle_sleep failed for {agent.name}/{label}: {e}")

    async def _check_dreams(self, now: float) -> None:
        """Check dream schedules for all dream-enabled agents and fire if due."""
        if not self._dream_callback:
            return

        agents = self._registry.list(enabled_only=True)

        for agent in agents:
            if not getattr(agent, "dream_enabled", False):
                continue

            cron_expr = getattr(agent, "dream_schedule", "0 3 * * *") or "0 3 * * *"
            tz_name = getattr(agent, "dream_timezone", "") or "America/Los_Angeles"

            try:
                tz = ZoneInfo(tz_name)
            except (KeyError, ValueError):
                tz = ZoneInfo("UTC")

            dt = datetime.fromtimestamp(now, tz=tz)
            current_minute = dt.hour * 60 + dt.minute

            # Skip if we already fired for this (date, minute) combination
            dedup_key = (date.today().isoformat(), current_minute)
            if self._last_dream_check.get(agent.name) == dedup_key:
                continue

            if cron_matches(cron_expr, dt):
                self._last_dream_check[agent.name] = dedup_key
                _log(f"scheduler: dream schedule fired for '{agent.name}' (cron={cron_expr})")
                try:
                    await self._dream_callback(agent.name, agent)
                except Exception as e:
                    _log(f"scheduler: dream callback failed for '{agent.name}': {e}")

    async def _check_url_watchers(self, now: float) -> None:
        """Poll enabled url-type triggers whose interval has elapsed."""
        if not self._trigger_store or not self._wake_callback:
            return

        try:
            due = self._trigger_store.list_due_url_watchers(now)
        except Exception as e:
            _log(f"scheduler: url watcher list failed: {e}")
            return

        if not due:
            return

        _log(f"scheduler: checking {len(due)} url watcher(s)")

        for trigger in due:
            await self._poll_url_trigger(trigger, now)

    async def _poll_url_trigger(self, trigger, now: float) -> None:
        """Poll a single url trigger and fire if its condition is met."""
        import urllib.error
        import urllib.request

        try:
            req = urllib.request.Request(
                trigger.url, method=trigger.method or "GET",
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                status_code = resp.status
                body_bytes = resp.read(65536)  # cap at 64KB
                body_text = body_bytes.decode(errors="replace")
        except urllib.error.HTTPError as e:
            status_code = e.code
            body_text = ""
        except Exception as e:
            _log(f"scheduler: url watcher '{trigger.name}' fetch error: {e}")
            self._trigger_store.record_check(trigger.id, trigger.last_value)
            return

        fired = self._evaluate_url_condition(trigger, status_code, body_text)

        if fired:
            prompt = self._render_url_trigger_prompt(trigger, status_code, body_text)
            try:
                await self._wake_callback(
                    trigger.agent_name, f"{trigger.agent_name}-main", prompt
                )
                _log(
                    f"scheduler: url watcher '{trigger.name}' fired for '{trigger.agent_name}'"
                )
            except Exception as e:
                _log(f"scheduler: url watcher wake failed for '{trigger.agent_name}': {e}")
            self._trigger_store.record_fire(trigger.id)

        # Determine new last_value to store
        condition = trigger.condition
        if condition in ("status_changed", "status_is"):
            new_value = str(status_code)
        elif condition in ("json_field_equals", "json_field_changed"):
            new_value = self._extract_json_field(body_text, trigger.condition_value)
        else:
            # body_contains / default: store raw body (capped)
            new_value = body_text[:1024]

        self._trigger_store.record_check(trigger.id, new_value)

    def _evaluate_url_condition(self, trigger, status_code: int, body_text: str) -> bool:
        """Return True if the trigger condition is satisfied."""
        condition = trigger.condition
        last_value = trigger.last_value
        condition_value = trigger.condition_value

        if condition == "status_changed":
            return last_value != str(status_code)

        if condition == "status_is":
            try:
                expected = int(condition_value)
            except (ValueError, TypeError):
                return False
            return status_code == expected

        if condition == "body_contains":
            return condition_value.lower() in body_text.lower()

        if condition == "json_field_equals":
            # condition_value format: "path=some.field,value=expected"
            # or JSON: {"path": "a.b", "value": "x"}
            try:
                params = json.loads(condition_value)
                path = params.get("path", "")
                expected = str(params.get("value", ""))
            except Exception:
                return False
            current = self._extract_json_field(body_text, path)
            return current == expected

        if condition == "json_field_changed":
            try:
                params = json.loads(condition_value) if condition_value else {}
                path = params.get("path", condition_value)
            except Exception:
                path = condition_value
            current = self._extract_json_field(body_text, path)
            return current != last_value

        # Unknown condition — don't fire
        return False

    def _extract_json_field(self, body_text: str, path: str) -> str:
        """Extract a dot-path value from a JSON body string."""
        try:
            obj = json.loads(body_text)
        except Exception:
            return ""
        parts = (path or "").split(".")
        current = obj
        for part in parts:
            if isinstance(current, dict):
                current = current.get(part)
            elif isinstance(current, list):
                try:
                    current = current[int(part)]
                except (ValueError, IndexError):
                    return ""
            else:
                return ""
        return str(current) if current is not None else ""

    def _render_url_trigger_prompt(self, trigger, status_code: int, body_text: str) -> str:
        """Build a prompt for a url watcher trigger fire."""
        if trigger.prompt_template:
            import re
            from datetime import datetime
            from datetime import timezone as _tz

            timestamp = datetime.now(_tz.utc).isoformat()
            try:
                body_json = json.loads(body_text)
            except Exception:
                body_json = None

            condition = trigger.condition
            if condition == "json_field_changed":
                try:
                    params = json.loads(trigger.condition_value) if trigger.condition_value else {}
                    path = params.get("path", trigger.condition_value)
                except Exception:
                    path = trigger.condition_value
                field_value = self._extract_json_field(body_text, path)
            else:
                field_value = ""

            ctx = {
                "trigger_name": trigger.name,
                "url": trigger.url,
                "status": str(status_code),
                "body_raw": body_text[:2000],
                "field_value": field_value,
                "field_previous": trigger.last_value,
                "timestamp": timestamp,
                "body": body_json or {},
            }

            def _extract_path(obj, path_str: str) -> str:
                parts = path_str.split(".")
                current = obj
                for p in parts:
                    if isinstance(current, dict):
                        current = current.get(p)
                    elif isinstance(current, list):
                        try:
                            current = current[int(p)]
                        except (ValueError, IndexError):
                            return ""
                    else:
                        return ""
                return str(current) if current is not None else ""

            def replacer(match) -> str:
                expr = match.group(1).strip()
                if expr in ctx and expr != "body":
                    return str(ctx[expr])
                if expr.startswith("body.") and body_json:
                    return _extract_path(body_json, expr[5:])
                return ""

            return re.sub(r"\{\{([^}]+)\}\}", replacer, trigger.prompt_template)

        return (
            f"URL watcher '{trigger.name}' fired.\n"
            f"URL: {trigger.url}\n"
            f"Status: {status_code}\n"
            f"Body (first 500 chars):\n{body_text[:500]}"
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
