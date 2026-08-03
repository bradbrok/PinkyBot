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
import inspect
import json
import sys
import time
from datetime import date, datetime
from zoneinfo import ZoneInfo

from pinky_daemon.agent_registry import AgentRegistry
from pinky_daemon.cron_utils import _field_matches
from pinky_daemon.transport_state import SessionState
from pinky_daemon.watchdog_log import log_watchdog_decision


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


_PROVEN_LIVE_HEARTBEAT_STATUSES = frozenset(
    {"alive", "ok", "busy", "finishing"}
)


# ── Rate Limit Gating ───────────────────────────────────────

_RATE_LIMIT_FILE = "/tmp/claude-rate-limits.json"
_RATE_LIMIT_THRESHOLD = 80  # percent — skip heartbeats above this


def _is_claude_code_agent(agent, registry: AgentRegistry) -> bool:
    """Return True if this agent runs on Claude Code (not Codex or other provider)."""
    runtime = (getattr(agent, "runtime", "") or "").strip()
    if runtime:
        return runtime == "claude_sdk"
    try:
        from pinky_daemon.api import resolve_provider_config
        url, _, _ = resolve_provider_config(
            agent_provider_url=agent.provider_url or "",
            agent_provider_key=agent.provider_key or "",
            agent_provider_model=agent.provider_model or "",
            agent_provider_ref=agent.provider_ref or "",
            default_provider_ref=registry.get_setting("default_provider_ref", "") or "",
            db=registry._db,
        )
        return url != "codex_cli"
    except Exception:
        return True  # fail-safe: assume CC


def _rate_limits_ok() -> bool:
    """Return True if CC rate limits are below threshold (or unavailable).

    Reads the shared rate limit file written by the statusline script.
    If the file is missing, stale (>5min), or unreadable, returns True
    (fail-open — don't skip heartbeats when we can't check).
    """
    try:
        with open(_RATE_LIMIT_FILE) as f:
            data = json.loads(f.read())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return True  # fail-open

    # Stale data (>5min) — don't gate on outdated info
    if time.time() - data.get("updated_at", 0) > 300:
        return True

    five_pct = data.get("five_hour", {}).get("used_percentage", 0)
    seven_pct = data.get("seven_day", {}).get("used_percentage", 0)

    if five_pct >= _RATE_LIMIT_THRESHOLD or seven_pct >= _RATE_LIMIT_THRESHOLD:
        return False
    return True


# ── Cron Parser ──────────────────────────────────────────────

def cron_matches(cron_expr: str, dt: datetime) -> bool:
    """Check if a datetime matches a 5-field cron expression.

    Fields: minute hour day-of-month month day-of-week
    Supports: * (any), */N (step), N-M (range), N-M/S (range step),
    N,M (list), and 3-letter day/month names (mon, jan, ...)
    Day-of-week: 0=Sunday ... 6=Saturday
    """
    fields = cron_expr.strip().split()
    if len(fields) != 5:
        return False

    values = [dt.minute, dt.hour, dt.day, dt.month, dt.isoweekday() % 7]
    limits = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 6)]

    try:
        for field, value, (lo, hi) in zip(fields, values, limits):
            if not _field_matches(field, value, lo, hi):
                return False
    except ValueError:
        # Schedules are stored unvalidated; a single malformed expression
        # must never abort the scheduler tick for everyone else.
        _log(f"scheduler: invalid cron expression {cron_expr!r}; treating as no match")
        return False
    return True


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

    PERSISTED_WAKE_ATTEMPT_CAP = 5

    def __init__(
        self,
        registry: AgentRegistry,
        *,
        wake_callback=None,
        heartbeat_callback=None,
        direct_send_callback=None,
        auto_sleep_callback=None,
        dream_callback=None,
        librarian_callback=None,
        streaming_sessions_fn=None,
        is_resurrectable_fn=None,
        comms_cleanup_fn=None,
        delivery_busy_fn=None,
        delivery_inflight_fn=None,
        owner_notify_callback=None,
        trigger_store=None,
        activity=None,
        tick_interval: int = 30,
        schedule_delivery_timeout: float = 600.0,
    ) -> None:
        self._registry = registry
        # async fn(agent_name, session_id, prompt) -> bool | Awaitable[bool].
        # An awaitable return is a per-prompt delivery receipt; same-agent
        # schedules are not advanced until it resolves.
        self._wake_callback = wake_callback
        self._heartbeat_callback = heartbeat_callback  # async fn(agent_name, session_id)
        self._direct_send_callback = direct_send_callback  # async fn(agent_name, platform, chat_id, message)
        self._auto_sleep_callback = auto_sleep_callback  # async fn(agent_name, reason)
        self._dream_callback = dream_callback  # async fn(agent_name, agent_config)
        self._librarian_callback = librarian_callback  # async fn(agent_name, agent_config)
        self._last_librarian_check: dict[str, tuple] = {}  # dedup key
        self._streaming_sessions_fn = streaming_sessions_fn  # fn() -> dict[name, StreamingSession]
        # Precondition check for resurrection. If supplied, must return True iff
        # the named agent is currently in a state where resurrection is desired.
        # Used to skip eval for idle-sleeping agents (which the API callback
        # would refuse anyway, but at the cost of a budget slot and a log line).
        self._is_resurrectable_fn = is_resurrectable_fn  # fn(agent_name) -> bool
        self._comms_cleanup_fn = comms_cleanup_fn  # fn() -> int (expired comms bookkeeping cleanup)
        # fn(agent_name) -> bool. True means the inflight watchdog has
        # positive busy-not-wedged evidence, so a pending scheduler receipt
        # must keep waiting instead of expiring behind a healthy long turn.
        self._delivery_busy_fn = delivery_busy_fn
        # fn(agent_name, prompt) -> bool. True means THIS wake's prompt has
        # already been pasted to the transport with its receipt unresolved.
        # Past that point a cancel cannot recall the prompt — the pane will
        # execute it regardless — so declaring the wake undelivered and
        # re-persisting it would mint a phantom outbox row whose later
        # replay is a DUPLICATE EXECUTION. The receipt wait must extend
        # instead. Distinct from delivery_busy_fn: that reads watchdog
        # liveness (can blip false between turns at the timeout boundary);
        # this reads the turn's own transport execution state.
        #
        # The extension is DELIBERATELY unbounded while the probe keeps
        # reporting pasted-unresolved: in that state any timeout action
        # either drops the wake or duplicates it, so the scheduler holds.
        # The hold ends when the receipt resolves (acceptance observed),
        # the session leaves CONNECTED (receipt resolves False), or a
        # force_restart resets the turn's pasted flag (probe reads False
        # and the durable cancel+persist path resumes). Operational cost
        # while held: same-agent schedule cohorts queue behind the
        # per-agent delivery lock — surfaced by the "extending" log line
        # each timeout period (Murzik review, PR #983).
        self._delivery_inflight_fn = delivery_inflight_fn
        # async fn(agent_name, text) -> bool. FIRED BUT UNDELIVERED must leave
        # journald and reach the owner through an out-of-band transport.
        self._owner_notify_callback = owner_notify_callback
        self._trigger_store = trigger_store  # TriggerStore | None
        self._activity = activity  # ActivityStore | None
        self._tick_interval = tick_interval
        self._schedule_delivery_timeout = schedule_delivery_timeout
        self._running = False
        self._task: asyncio.Task | None = None
        self._last_clock_slot: dict[str, int] = {}  # agent_name -> last fired clock slot (minutes since midnight)
        self._last_dream_check: dict[str, tuple] = {}  # agent_name -> (date_str, cron-minute) dedup key
        # In-flight dream/librarian runs (#702). These run as background tasks
        # so a long dream (~1h with KG extraction) can't freeze the tick loop —
        # a blocked tick silently skips every cron minute in the window.
        self._dream_tasks: dict[str, asyncio.Task] = {}  # agent_name -> running dream
        # The librarian curates ONE shared project-level KBStore (api.py wires a
        # single LibrarianRunner guarded by a global _librarian_state.running
        # lock on the ingest-debounce path), so scheduled runs must not execute
        # concurrently across agents either: single global slot, not per-agent.
        self._librarian_tasks: dict[str, asyncio.Task] = {}  # LIBRARIAN_GLOBAL_KEY -> running librarian
        # In-flight idle/auto-sleep runs. idle_sleep() waits up to a minute
        # for the pre-sleep memory-save turn, so it must never be awaited
        # inline in the tick (same #702 class as dreams): one slot per
        # (agent_name, label) so a sleep spanning several ticks isn't refired.
        self._sleep_tasks: dict[tuple[str, str], asyncio.Task] = {}
        # Schedule prompts run outside the tick loop so waiting for a busy
        # agent cannot freeze every other cron/heartbeat check (#702 class).
        # A per-agent lock preserves fire order across same-tick and later-tick
        # cohorts while allowing unrelated agents to progress independently.
        self._schedule_delivery_tasks: set[asyncio.Task] = set()
        self._schedule_delivery_locks: dict[str, asyncio.Lock] = {}
        self._pending_replay_tasks: dict[str, asyncio.Task] = {}
        self._owner_alert_tasks: set[asyncio.Task] = set()
        # Resurrection rate-limit: agent_name -> list[timestamp] of recent attempts.
        # Used by _check_heartbeats to cap how often we ping the heartbeat_callback
        # for a stuck agent (avoid thrashing on a persistently-broken session).
        self._resurrection_attempts: dict[str, list[float]] = {}

    async def start(self) -> None:
        """Start the scheduler background loop."""
        if self._running:
            return
        self._running = True
        for pending in self._registry.list_pending_schedule_wakes():
            self.replay_pending_for_agent(pending.agent_name)
        # Queue startup catch-up before the first live tick can enqueue newer
        # cron fires. Per-agent delivery locks preserve that ordering.
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
        # Cancel in-flight dream/librarian/sleep runs. Before #702 these ran
        # inside the loop task, so stop() cancelled them implicitly — keep
        # that contract.
        for task_map in (self._dream_tasks, self._librarian_tasks, self._sleep_tasks):
            for task in task_map.values():
                if not task.done():
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
            task_map.clear()
        delivery_tasks = list(self._schedule_delivery_tasks)
        for task in delivery_tasks:
            if not task.done():
                task.cancel()
        if delivery_tasks:
            await asyncio.gather(*delivery_tasks, return_exceptions=True)
        self._schedule_delivery_tasks.clear()
        replay_tasks = list(self._pending_replay_tasks.values())
        for task in replay_tasks:
            if not task.done():
                task.cancel()
        if replay_tasks:
            await asyncio.gather(*replay_tasks, return_exceptions=True)
        self._pending_replay_tasks.clear()
        alert_tasks = list(self._owner_alert_tasks)
        if alert_tasks:
            await asyncio.gather(*alert_tasks, return_exceptions=True)
        self._owner_alert_tasks.clear()
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

        # Check librarian schedule
        await self._check_librarian(now)

        # Check URL watcher triggers
        await self._check_url_watchers(now)

    async def _check_schedules(self, now: float) -> None:
        """Stamp due schedules fired, then deliver each agent's cohort in order."""
        schedules = self._registry.get_all_schedules(enabled_only=True)
        if not schedules:
            return

        due_by_agent: dict[str, list] = {}
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
                _log(f"scheduler: firing schedule '{schedule.name}' for agent '{schedule.agent_name}' (direct_send={schedule.direct_send}, one_shot={schedule.one_shot})")
                if self._activity:
                    try:
                        self._activity.log(schedule.agent_name, "schedule_fired", f"Schedule '{schedule.name}' fired")
                    except Exception:
                        pass
                self._registry.update_schedule_last_run(schedule.id, now)
                # Carry the exact fire identity on this queued snapshot. A
                # later minute can advance the DB row while this cohort still
                # waits behind a long turn, so failure paths must never reread
                # mutable last_run from storage.
                schedule.last_run = now

                # Auto-disable one-shot schedules after firing
                if schedule.one_shot:
                    self._registry.toggle_schedule(schedule.id, False)
                    _log(f"scheduler: one-shot schedule '{schedule.name}' (#{schedule.id}) auto-disabled after firing")

                due_by_agent.setdefault(schedule.agent_name, []).append(schedule)

        cohort_started: list[asyncio.Event] = []
        for agent_name, due_schedules in due_by_agent.items():
            attempt_started = asyncio.Event()
            task = asyncio.create_task(
                self._deliver_schedule_group(
                    agent_name,
                    due_schedules,
                    attempt_started=attempt_started,
                )
            )
            self._schedule_delivery_tasks.add(task)
            task.add_done_callback(self._schedule_delivery_done)
            cohort_started.append(attempt_started)

        # Synchronize on the actual start condition rather than counting event
        # loop turns. The confirmation monitor adds task indirection on Python
        # 3.12/3.13, but a due callback must still begin before this check
        # returns (#702). This bound never waits for a receipt (or a dream), and
        # prevents an older per-agent delivery lock from blocking the tick.
        if cohort_started:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*(event.wait() for event in cohort_started)),
                    timeout=1.0,
                )
            except asyncio.TimeoutError:
                _log(
                    "scheduler: cohort-start synchronization timed out; "
                    "blocked delivery tasks remain queued"
                )

    async def _deliver_schedule_group(
        self,
        agent_name: str,
        schedules: list,
        *,
        attempt_started: asyncio.Event | None = None,
    ) -> None:
        """Deliver one agent's due prompts serially, including receipt waits."""
        lock = self._schedule_delivery_locks.setdefault(agent_name, asyncio.Lock())
        next_index = 0
        try:
            # Persisted wakes belong to the NEXT session, never another cron
            # tick on the same doomed transport. Only wait when boot/
            # orientation explicitly scheduled catch-up; otherwise leave the
            # outbox untouched until that lifecycle boundary occurs.
            replay_task = self._pending_replay_tasks.get(agent_name)
            if (
                replay_task is not None
                and replay_task is not asyncio.current_task()
                and not replay_task.done()
            ):
                try:
                    await asyncio.shield(replay_task)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    _log(
                        f"scheduler: persisted wake catch-up failed before "
                        f"live cohort for '{agent_name}': "
                        f"{type(exc).__name__}: {exc}"
                    )
            async with lock:
                for next_index, schedule in enumerate(schedules):
                    try:
                        await self._deliver_schedule(
                            schedule,
                            attempt_started=(
                                attempt_started if next_index == 0 else None
                            ),
                        )
                    except asyncio.CancelledError:
                        self._record_schedule_undelivered(
                            schedule, "delivery canceled before confirmation"
                        )
                        next_index += 1
                        raise
                    next_index += 1
        except asyncio.CancelledError:
            # All members of this cohort were stamped fired before this task
            # was created. Cancellation (including stop()) must not make the
            # current/tail schedules disappear from the audit trail. Keep this
            # synchronous so shutdown remains fast.
            for schedule in schedules[next_index:]:
                self._record_schedule_undelivered(
                    schedule, "delivery canceled before attempt"
                )
            raise

    async def _deliver_schedule(
        self,
        schedule,
        *,
        attempt_started: asyncio.Event | None = None,
    ) -> None:
        """Attempt one fired schedule and record confirmed delivery separately."""
        confirmed = False
        failure_reason = "no delivery callback configured"

        if schedule.direct_send:
            if attempt_started is not None:
                attempt_started.set()
            if not schedule.target_channel:
                failure_reason = "direct send has no target channel"
            elif self._direct_send_callback is None:
                failure_reason = "no direct send callback configured"
            else:
                try:
                    await self._direct_send_callback(
                        schedule.agent_name,
                        "telegram",  # Default platform
                        schedule.target_channel,
                        schedule.prompt,
                    )
                    confirmed = True
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    failure_reason = (
                        f"direct send raised {type(e).__name__}: {e}"
                    )
        elif self._wake_callback:
            try:
                confirmed = await self._wait_for_wake_confirmation(
                    schedule, attempt_started=attempt_started
                )
                if not confirmed:
                    failure_reason = "wake callback returned no positive receipt"
            except asyncio.TimeoutError:
                failure_reason = (
                    "delivery receipt timed out after "
                    f"{self._schedule_delivery_timeout:g}s"
                )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                failure_reason = f"wake callback raised {type(e).__name__}: {e}"
        elif attempt_started is not None:
            attempt_started.set()

        if confirmed:
            delivered_at = time.time()
            retired_pending = (
                self._registry.confirm_pending_schedule_wake_by_fire(
                    schedule.id,
                    schedule.last_run,
                    delivered_at=delivered_at,
                )
            )
            if not retired_pending:
                self._registry.update_schedule_last_delivered(
                    schedule.id, delivered_at
                )
            if self._activity:
                try:
                    self._activity.log(
                        schedule.agent_name,
                        "schedule_delivered",
                        f"Schedule '{schedule.name}' delivery confirmed",
                    )
                except Exception as exc:
                    _log(
                        f"scheduler: failed to record schedule delivery "
                        f"activity for '{schedule.agent_name}': "
                        f"{type(exc).__name__}: {exc}"
                    )
            _log(
                f"scheduler: delivery confirmed for schedule "
                f"'{schedule.name}' (#{schedule.id}) for agent "
                f"'{schedule.agent_name}'"
            )
            return

        self._record_schedule_undelivered(schedule, failure_reason)

    def _record_schedule_undelivered(
        self, schedule, failure_reason: str
    ) -> None:
        """Persist, alert, and loudly account one unconfirmed fired schedule."""
        persisted = False
        alert_this_failure = True
        if not schedule.direct_send:
            try:
                _, created = self._registry.persist_schedule_wake(
                    schedule.id,
                    agent_name=schedule.agent_name,
                    schedule_name=schedule.name,
                    prompt=(
                        schedule.prompt
                        or f"Scheduled wake: {schedule.name}"
                    ),
                    fired_at=schedule.last_run,
                )
                persisted = True
                alert_this_failure = created
            except Exception as exc:
                _log(
                    f"scheduler: SCHEDULER_WAKE_PERSIST_FAILURE schedule "
                    f"'{schedule.name}' (#{schedule.id}) for agent "
                    f"'{schedule.agent_name}': {type(exc).__name__}: {exc}"
                )
        if self._activity:
            try:
                self._activity.log(
                    schedule.agent_name,
                    "schedule_undelivered",
                    f"Schedule '{schedule.name}' fired but delivery was not confirmed",
                )
            except Exception:
                pass
        _log(
            f"scheduler: FIRED BUT UNDELIVERED schedule "
            f"'{schedule.name}' (#{schedule.id}) for agent "
            f"'{schedule.agent_name}': {failure_reason}"
        )
        if alert_this_failure:
            recovery = (
                " The wake was persisted for the agent's next session."
                if persisted
                else " WARNING: durable wake persistence did not succeed."
            )
            self._queue_owner_alert(
                schedule.agent_name,
                (
                    "🚨 FIRED BUT UNDELIVERED: schedule "
                    f"'{schedule.name}' (#{schedule.id}) for agent "
                    f"'{schedule.agent_name}' was not confirmed: "
                    f"{failure_reason}.{recovery}"
                ),
            )

    async def _wait_for_wake_confirmation(
        self,
        schedule,
        *,
        attempt_started: asyncio.Event | None = None,
    ) -> bool:
        """Wait for one exact receipt, extending while positive liveness holds."""
        delivery = asyncio.create_task(
            self._wake_and_confirm(
                schedule, attempt_started=attempt_started
            )
        )
        try:
            while True:
                try:
                    return await asyncio.wait_for(
                        asyncio.shield(delivery),
                        timeout=self._schedule_delivery_timeout,
                    )
                except asyncio.TimeoutError:
                    if self._agent_busy_not_wedged(schedule.agent_name):
                        _log(
                            f"scheduler: receipt still pending for schedule "
                            f"'{schedule.name}' (#{schedule.id}) for agent "
                            f"'{schedule.agent_name}', but inflight watchdog "
                            "reports busy-not-wedged; extending delivery timeout"
                        )
                        continue
                    if self._wake_prompt_inflight(schedule):
                        _log(
                            f"scheduler: receipt still pending for schedule "
                            f"'{schedule.name}' (#{schedule.id}) for agent "
                            f"'{schedule.agent_name}', but its prompt is "
                            "already pasted to the transport — a cancel "
                            "cannot recall it, and declaring undelivered "
                            "would re-persist a wake that is about to "
                            "execute (duplicate execution); extending"
                        )
                        continue
                    delivery.cancel()
                    await asyncio.gather(delivery, return_exceptions=True)
                    raise
        except asyncio.CancelledError:
            delivery.cancel()
            await asyncio.gather(delivery, return_exceptions=True)
            raise

    def _agent_busy_not_wedged(self, agent_name: str) -> bool:
        """Read the transport's live positive-liveness signal, failing closed."""
        if self._delivery_busy_fn is None:
            return False
        try:
            return self._delivery_busy_fn(agent_name) is True
        except Exception as exc:
            _log(
                f"scheduler: busy-not-wedged check failed for "
                f"'{agent_name}': {type(exc).__name__}: {exc}"
            )
            return False

    def _queue_owner_alert(self, agent_name: str, message: str) -> None:
        """Start one owner alert with a strong task reference and loud failure."""
        if self._owner_notify_callback is None:
            _log(
                f"scheduler: OWNER_NOTIFY_UNAVAILABLE for schedule-delivery "
                f"alert agent '{agent_name}'"
            )
            return

        async def _notify() -> None:
            try:
                result = self._owner_notify_callback(agent_name, message)
                if inspect.isawaitable(result):
                    result = await result
                if result is not True:
                    raise RuntimeError("owner notify returned no positive receipt")
            except Exception as exc:
                _log(
                    f"scheduler: OWNER_NOTIFY_FAILURE for schedule-delivery "
                    f"alert agent '{agent_name}': "
                    f"{type(exc).__name__}: {exc}"
                )

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            _log(
                f"scheduler: OWNER_NOTIFY_FAILURE for schedule-delivery "
                f"alert agent '{agent_name}': no running event loop"
            )
            return
        task = loop.create_task(_notify())
        self._owner_alert_tasks.add(task)
        task.add_done_callback(self._owner_alert_tasks.discard)

    def replay_pending_for_agent(self, agent_name: str) -> None:
        """Replay the durable wake outbox after the agent's next session boot."""
        existing = self._pending_replay_tasks.get(agent_name)
        if existing is not None and not existing.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            _log(
                f"scheduler: persisted wake replay trigger for '{agent_name}' "
                "has no running event loop; startup catch-up remains pending"
            )
            return
        task = loop.create_task(self._replay_pending_for_agent(agent_name))
        self._pending_replay_tasks[agent_name] = task

        def _done(done: asyncio.Task) -> None:
            if self._pending_replay_tasks.get(agent_name) is done:
                self._pending_replay_tasks.pop(agent_name, None)
            if not done.cancelled() and done.exception() is not None:
                error = done.exception()
                _log(
                    f"scheduler: PERSISTED_WAKE_REPLAY_FAILURE for "
                    f"'{agent_name}': {type(error).__name__}: {error}"
                )

        task.add_done_callback(_done)

    async def _replay_pending_for_agent(self, agent_name: str) -> None:
        """Deliver one agent's persisted wakes FIFO and retire exact successes."""
        lock = self._schedule_delivery_locks.setdefault(agent_name, asyncio.Lock())
        async with lock:
            await self._replay_pending_locked(agent_name)

    def _park_pending_wake_if_capped(self, pending, attempts: int) -> bool:
        """Park one capped wake once and emit its single owner alert."""
        if attempts < self.PERSISTED_WAKE_ATTEMPT_CAP:
            return False
        try:
            parked = self._registry.park_pending_schedule_wake(pending.id)
        except Exception as exc:
            _log(
                f"scheduler: PERSISTED_WAKE_PARK_FAILURE pending "
                f"#{pending.id}, schedule '{pending.schedule_name}' "
                f"(#{pending.schedule_id}) for agent '{pending.agent_name}': "
                f"{type(exc).__name__}: {exc}"
            )
            return False
        if not parked:
            return False
        _log(
            f"scheduler: PERSISTED_WAKE_PARKED pending #{pending.id}, "
            f"schedule '{pending.schedule_name}' (#{pending.schedule_id}), "
            f"fired_at={pending.fired_at}, attempts={attempts} for agent "
            f"'{pending.agent_name}'"
        )
        self._queue_owner_alert(
            pending.agent_name,
            (
                "🚨 PERSISTED WAKE PARKED: outbox row "
                f"#{pending.id} for schedule '{pending.schedule_name}' "
                f"(#{pending.schedule_id}) on agent '{pending.agent_name}' "
                f"reached {attempts} unconfirmed delivery attempts. Replay "
                "is stopped for this row to prevent a storm; delete the row "
                "manually to unpark it."
            ),
        )
        return True

    async def _replay_pending_locked(self, agent_name: str) -> None:
        """Reap all zombies, then replay active wakes FIFO under the agent lock."""
        all_pending_wakes = self._registry.list_pending_schedule_wakes(
            agent_name, include_parked=True
        )
        pending_wakes = []
        for pending in all_pending_wakes:
            current_schedule = self._registry.get_schedule(pending.schedule_id)
            zombie_reason = ""
            if current_schedule is None:
                zombie_reason = "schedule deleted"
            elif current_schedule.agent_name != pending.agent_name:
                zombie_reason = "schedule reassigned to another agent"
            elif not current_schedule.enabled and not current_schedule.one_shot:
                zombie_reason = "schedule disabled"
            if zombie_reason:
                retired = self._registry.discard_pending_schedule_wake(
                    pending.id
                )
                _log(
                    f"scheduler: PERSISTED_WAKE_ZOMBIE_DROPPED pending "
                    f"#{pending.id}, schedule #{pending.schedule_id} for "
                    f"agent '{pending.agent_name}': {zombie_reason}; "
                    f"outbox_retired={retired}"
                )
                continue
            if pending.parked_at == 0:
                pending_wakes.append(pending)

        for pending in pending_wakes:
            if pending.attempts >= self.PERSISTED_WAKE_ATTEMPT_CAP:
                self._park_pending_wake_if_capped(pending, pending.attempts)
                break
            attempts = self._registry.increment_pending_schedule_wake_attempts(
                pending.id
            )
            if attempts is None:
                continue
            try:
                confirmed = await self._wait_for_wake_confirmation(pending)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                _log(
                    f"scheduler: persisted wake #{pending.id} for "
                    f"'{agent_name}' remains pending after replay: "
                    f"{type(exc).__name__}: {exc}"
                )
                self._park_pending_wake_if_capped(pending, attempts)
                break
            if not confirmed:
                _log(
                    f"scheduler: persisted wake #{pending.id} for "
                    f"'{agent_name}' remains pending: no positive receipt"
                )
                self._park_pending_wake_if_capped(pending, attempts)
                break
            delivered_at = time.time()
            if not self._registry.confirm_pending_schedule_wake(
                pending.id, delivered_at=delivered_at
            ):
                _log(
                    f"scheduler: persisted wake #{pending.id} for "
                    f"'{agent_name}' was confirmed but outbox retirement "
                    "did not match a row"
                )
                break
            if self._activity:
                try:
                    self._activity.log(
                        agent_name,
                        "schedule_delivered",
                        f"Schedule '{pending.schedule_name}' persisted "
                        "wake delivery confirmed",
                    )
                except Exception as exc:
                    _log(
                        f"scheduler: failed to record persisted wake "
                        f"delivery activity for '{agent_name}': "
                        f"{type(exc).__name__}: {exc}"
                    )
            _log(
                f"scheduler: persisted wake delivery confirmed for "
                f"schedule '{pending.schedule_name}' "
                f"(#{pending.schedule_id}) for agent '{agent_name}'"
            )

    @staticmethod
    def _wake_prompt(schedule) -> str:
        """The exact prompt text a schedule's wake delivers to the transport."""
        return schedule.prompt or f"Scheduled wake: {schedule.name}"

    def _wake_prompt_inflight(self, schedule) -> bool:
        """True when this wake's prompt is pasted with its receipt unresolved.

        Reads the transport's per-turn execution state via
        ``delivery_inflight_fn``, failing closed (False) so a missing or
        broken probe degrades to the pre-probe behavior rather than
        extending forever.
        """
        if self._delivery_inflight_fn is None:
            return False
        try:
            return (
                self._delivery_inflight_fn(
                    schedule.agent_name, self._wake_prompt(schedule)
                )
                is True
            )
        except Exception as exc:
            _log(
                f"scheduler: wake-inflight check failed for "
                f"'{schedule.agent_name}': {type(exc).__name__}: {exc}"
            )
            return False

    async def _wake_and_confirm(
        self,
        schedule,
        *,
        attempt_started: asyncio.Event | None = None,
    ) -> bool:
        """Invoke the wake callback and await its exact per-prompt receipt."""
        if attempt_started is not None:
            attempt_started.set()
        main_session_id = f"{schedule.agent_name}-main"
        result = await self._wake_callback(
            schedule.agent_name,
            main_session_id,
            self._wake_prompt(schedule),
        )
        if inspect.isawaitable(result):
            result = await result
        return result is True

    def _schedule_delivery_done(self, task: asyncio.Task) -> None:
        """Retire a delivery task and surface any unexpected cohort crash."""
        self._schedule_delivery_tasks.discard(task)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            _log(
                f"scheduler: SCHEDULE DELIVERY TASK CRASHED with "
                f"{type(error).__name__}: {error}"
            )

    async def _check_heartbeats(self, now: float) -> None:
        """Check heartbeat health for all agents with heartbeat_interval > 0."""
        agents = self._registry.list(enabled_only=True)
        streaming_sessions = {}
        if self._streaming_sessions_fn:
            try:
                streaming_sessions = self._streaming_sessions_fn() or {}
            except Exception:
                streaming_sessions = {}

        for agent in agents:
            if agent.heartbeat_interval <= 0:
                continue

            hb = self._registry.get_latest_heartbeat(agent.name)
            if self._reconcile_server_liveness(agent, hb, now, streaming_sessions):
                # Server-side transport evidence proves this agent is live —
                # a safe boot boundary to drain any stranded wake outbox.
                self._drain_outbox_if_pending(agent.name)
                continue
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
                # Always evaluate resurrection on a dead heartbeat — even if we
                # already logged the death earlier — so a stuck session keeps
                # getting periodic restart attempts (rate-limited below).
                await self._maybe_resurrect(agent.name, hb.session_id, now)
            elif age > agent.heartbeat_interval:
                # Missed 1 interval — stale
                if hb.status == "alive":
                    self._registry.record_heartbeat(
                        agent.name, session_id=hb.session_id,
                        status="stale", context_pct=hb.context_pct,
                        message_count=hb.message_count,
                        metadata={"reason": f"heartbeat overdue by {int(age - agent.heartbeat_interval)}s"},
                    )
            elif hb.status in _PROVEN_LIVE_HEARTBEAT_STATUSES:
                # Fresh agent/hook activity is transport-observed execution
                # evidence: SDK hooks write "alive" and tmux-rails agents POST
                # "ok"/"busy"/"finishing" themselves while running. This
                # scheduler's own current-timestamp writes use only
                # "stale"/"dead", which remain excluded, so accepting the full
                # agent-authored vocabulary preserves #981's proven-live-only
                # policy and never drains on scheduler-authored or aged rows.
                self._drain_outbox_if_pending(agent.name)

    def _drain_outbox_if_pending(self, agent_name: str) -> None:
        """Replay a proven-live agent's stranded wake outbox.

        The durable outbox otherwise drains only at daemon ``start()`` or on a
        confirmed wake-prompt turn (``on_wake_delivered``). A session revived
        WITHOUT such a turn — e.g. a ``context_restart`` whose orientation wake
        never produced an exact receipt, then kept alive by heartbeats — stays
        alive yet never replays, stranding every cron it missed while dark
        (live incident 2026-08-01: 20 rows still pending after a mid-day heartbeat, all
        fired 06:00–10:15 into the gap). ``_check_heartbeats`` calls this once
        it has proof the session is live, closing that gap without touching the
        deliberate "a new cron fire never replays backlog into the old session"
        contract: replay still happens only at a proven-live boundary.

        Idempotent and self-limiting: skips when a replay is already in flight,
        only reads the (indexed) outbox for a genuinely-live agent, and excludes
        parked dead letters from its pending check. FIFO receipt failures remain
        pending only through the bounded attempt cap, preventing one
        never-confirming row from spawning replay tasks forever while preserving
        the proven-live-only drain policy.
        """
        existing = self._pending_replay_tasks.get(agent_name)
        if existing is not None and not existing.done():
            return
        try:
            if not self._registry.list_pending_schedule_wakes(agent_name):
                return
        except Exception as exc:
            _log(
                f"scheduler: outbox drain check failed for '{agent_name}': "
                f"{type(exc).__name__}: {exc}"
            )
            return
        _log(
            f"scheduler: proven-live agent '{agent_name}' has a stranded wake "
            "outbox; triggering durable replay"
        )
        self.replay_pending_for_agent(agent_name)

    # Resurrection cap: at most this many attempts per RESURRECTION_WINDOW_SECONDS
    # per agent. Prevents thrashing on a persistently-broken session while still
    # giving transient failures multiple chances to recover.
    RESURRECTION_MAX_ATTEMPTS = 5
    RESURRECTION_WINDOW_SECONDS = 3600  # 1 hour

    async def _maybe_resurrect(
        self, agent_name: str, session_id: str, now: float,
    ) -> None:
        """Trigger the heartbeat_callback for a dead agent, with rate limiting.

        Called from _check_heartbeats whenever an agent's heartbeat is currently
        marked dead. We rate-limit per agent so a permanently broken session
        doesn't generate restart calls every tick — but transient failures still
        get multiple recovery attempts.
        """
        if not self._heartbeat_callback:
            return

        # Precondition: skip agents that don't want resurrection at all
        # (e.g. idle-sleeping). This avoids consuming the rate-limit budget
        # and emitting "attempt N/N" log spam every tick for sleeping agents
        # the API callback would refuse anyway. See #348/#349 — that fix
        # landed at the API layer; this is the matching scheduler-level skip.
        if self._is_resurrectable_fn is not None:
            try:
                if not self._is_resurrectable_fn(agent_name):
                    return
            except Exception as e:
                # Fail-open: if the precondition check itself errors, fall
                # through to the existing path so we don't silently disable
                # resurrection for everyone.
                _log(f"scheduler: is_resurrectable_fn raised for {agent_name}: {e}")

        # Trim attempts outside the window
        window_start = now - self.RESURRECTION_WINDOW_SECONDS
        attempts = [t for t in self._resurrection_attempts.get(agent_name, []) if t >= window_start]
        if len(attempts) >= self.RESURRECTION_MAX_ATTEMPTS:
            # Capped — log once per cap-hit (cheap; tick is 30s so worst case
            # we log once per cap-hit window), then bail.
            return

        attempts.append(now)
        self._resurrection_attempts[agent_name] = attempts
        _log(
            f"scheduler: resurrection attempt {len(attempts)}/"
            f"{self.RESURRECTION_MAX_ATTEMPTS} for dead agent '{agent_name}'"
        )
        try:
            await self._heartbeat_callback(agent_name, session_id)
        except Exception as e:
            _log(f"scheduler: resurrection callback failed for {agent_name}: {e}")

    def _reconcile_server_liveness(
        self,
        agent,
        hb,
        now: float,
        streaming_sessions: dict,
    ) -> bool:
        """Return True when server-side liveness should suppress death handling.

        Heartbeat rows are agent-reported, but the daemon also has authoritative
        transport evidence: connected streaming sessions and server-stamped
        last_seen_at. Without reconciling those signals, a stale dead heartbeat
        can make the scheduler resurrect an already-live runtime forever.
        """
        sessions = streaming_sessions.get(agent.name, {}) or {}
        connected_label = ""
        connected_session = None
        main_session = sessions.get("main")
        if (
            main_session is not None
            and getattr(main_session, "state", None) == SessionState.CONNECTED
        ):
            connected_label = "main"
            connected_session = main_session
        else:
            for label, session in sessions.items():
                if getattr(session, "state", None) == SessionState.CONNECTED:
                    connected_label = label
                    connected_session = session
                    break

        hb_ts = hb.timestamp if hb else 0
        server_ts = getattr(agent, "last_seen_at", 0.0) or 0.0
        grace_seconds = agent.heartbeat_interval * 2
        fresh_server_seen = server_ts > hb_ts and (now - server_ts) <= grace_seconds

        if not connected_session and not fresh_server_seen:
            return False

        should_record = (
            not hb
            or hb.status != "alive"
            or (now - hb.timestamp) >= agent.heartbeat_interval
            or server_ts > hb_ts
        )
        if should_record:
            context_pct = 0.0
            message_count = 0
            session_id = f"{agent.name}-main"
            metadata = {"source": "server_presence"}
            if connected_session:
                session_id = getattr(connected_session, "id", session_id)
                metadata["reason"] = "connected_streaming_session"
                metadata["label"] = connected_label
                try:
                    context_pct = float(getattr(connected_session, "context_used_pct", 0.0) or 0.0)
                except Exception:
                    context_pct = 0.0
                stats = getattr(connected_session, "stats", {}) or {}
                if isinstance(stats, dict):
                    message_count = int(stats.get("messages_sent", 0) or 0) + int(
                        stats.get("turns", 0) or 0
                    )
            else:
                metadata["reason"] = "fresh_last_seen"
                metadata["last_seen_at"] = server_ts

            self._registry.record_heartbeat(
                agent.name,
                session_id=session_id,
                status="alive",
                context_pct=context_pct,
                message_count=message_count,
                metadata=metadata,
            )
            _log(
                f"scheduler: reconciled heartbeat for '{agent.name}' from "
                f"{metadata['reason']}"
            )

        self._resurrection_attempts.pop(agent.name, None)
        return True

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
                if ss.state != SessionState.CONNECTED:
                    continue

                idle_timeout = ss._config.idle_timeout
                if idle_timeout <= 0:
                    continue

                idle_seconds = now - ss.last_active
                if idle_seconds >= idle_timeout:
                    # #230 — never idle-sleep a session running a live Workflow /
                    # background turn. ``last_active`` is bumped only at turn
                    # delivery, NOT by subagent/workflow progress, so a long
                    # quiet-main-transcript Workflow looks "idle" and idle_sleep()
                    # would tear the REPL down mid-flight, killing the work. The
                    # transport's live ``inflight_active`` signal (reusing the
                    # #692/#731 liveness plumbing) flips False the moment liveness
                    # stops, so a FINISHED workflow still sleeps normally on a
                    # later tick. Absent on codex/legacy stats → falsy → sleeps
                    # exactly as before.
                    stats = getattr(ss, "stats", None) or {}
                    if stats.get("inflight_active"):
                        log_watchdog_decision(
                            watchdog="idle_sleep", agent=name, label=label,
                            decision="skip", reason="inflight_active",
                            state=getattr(ss.state, "value", str(ss.state)),
                            last_active_age_s=idle_seconds,
                            idle_timeout_s=idle_timeout,
                            inflight_turns=stats.get("inflight_turns"),
                            inflight_active=True,
                            inflight_liveness_reason=stats.get(
                                "inflight_liveness_reason"
                            ),
                            inflight_liveness_age_s=stats.get(
                                "inflight_liveness_age_s"
                            ),
                        )
                        continue
                    _log(f"scheduler: {name}/{label} idle for {int(idle_seconds)}s (threshold: {idle_timeout}s) — auto-sleeping")
                    log_watchdog_decision(
                        watchdog="idle_sleep", agent=name, label=label,
                        decision="sleep", reason="idle_timeout",
                        state=getattr(ss.state, "value", str(ss.state)),
                        last_active_age_s=idle_seconds,
                        idle_timeout_s=idle_timeout, inflight_active=False,
                    )
                    if self._activity:
                        try:
                            self._activity.log(name, "agent_sleep", f"{name} auto-slept (idle timeout)")
                        except Exception:
                            pass
                    self._spawn_idle_sleep(name, label, ss)

    def _spawn_idle_sleep(self, name: str, label: str, ss) -> None:
        """Fire idle_sleep as a background task — never inline in the tick.

        idle_sleep() gives the pre-sleep memory-save turn up to a minute to
        complete; N idle agents awaited serially would stall the tick loop
        N minutes, skipping every cron minute / heartbeat / wake in the
        window (#702 class). Mirrors ``_spawn_agent_callback``.
        """
        key = (name, label)
        existing = self._sleep_tasks.get(key)
        if existing and not existing.done():
            return

        async def _run() -> None:
            try:
                await ss.idle_sleep()
            except Exception as e:
                _log(f"scheduler: idle sleep failed for {name}/{label}: {e}")

        self._sleep_tasks[key] = asyncio.create_task(_run())

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

            # Gate heartbeats on CC rate limits — skip CC agents when usage ≥ 80%
            if _is_claude_code_agent(agent, self._registry) and not _rate_limits_ok():
                _log(
                    f"scheduler: skipping heartbeat for '{agent.name}'"
                    f" — CC rate limit ≥ {_RATE_LIMIT_THRESHOLD}%"
                )
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
                if ss.state != SessionState.CONNECTED:
                    continue

                idle_seconds = now - ss.last_active
                if idle_seconds >= threshold_seconds:
                    # #230 — never auto-sleep a session running a live Workflow /
                    # background turn. This path shares the stale-``last_active``
                    # threshold with _check_idle_sessions AND runs BEFORE it in
                    # _tick(), and ``auto_sleep_hours`` also feeds the tmux
                    # session ``idle_timeout`` — so without the SAME carve-out it
                    # would tear down an in-flight Workflow here first, before the
                    # _check_idle_sessions guard ever runs (Murzik #825 review).
                    # Releases the instant liveness stops (finished work sleeps).
                    stats = getattr(ss, "stats", None) or {}
                    state_val = getattr(ss.state, "value", str(ss.state))
                    if stats.get("inflight_active"):
                        log_watchdog_decision(
                            watchdog="auto_sleep", agent=agent.name, label=label,
                            decision="skip", reason="inflight_active",
                            state=state_val, last_active_age_s=idle_seconds,
                            idle_timeout_s=threshold_seconds,
                            inflight_turns=stats.get("inflight_turns"),
                            inflight_active=True,
                            inflight_liveness_reason=stats.get(
                                "inflight_liveness_reason"
                            ),
                            inflight_liveness_age_s=stats.get(
                                "inflight_liveness_age_s"
                            ),
                        )
                        continue
                    _log(f"scheduler: auto-sleep for '{agent.name}/{label}' — idle {idle_seconds / 3600:.1f}h (threshold: {agent.auto_sleep_hours}h)")
                    log_watchdog_decision(
                        watchdog="auto_sleep", agent=agent.name, label=label,
                        decision="sleep", reason="auto_sleep_idle",
                        state=state_val, last_active_age_s=idle_seconds,
                        idle_timeout_s=threshold_seconds, inflight_active=False,
                    )
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
                        self._spawn_idle_sleep(agent.name, label, ss)

    LIBRARIAN_GLOBAL_KEY = "__shared_kb__"

    def _spawn_agent_callback(
        self, task_map: dict, callback, agent, *, kind: str, fired_msg: str, key: str = ""
    ) -> None:
        """Fire a dream/librarian callback as a background task (#702).

        The tick loop must never await these inline: a dream can run for an
        hour (KG extraction over dozens of reflections), and a blocked tick
        skips every cron schedule, heartbeat check, and wake in the window —
        `_check_schedules` matches the current minute only, with no catch-up.

        ``key`` is the overlap-guard slot: agent name for dreams (independent
        per-agent state), LIBRARIAN_GLOBAL_KEY for librarian runs (shared KB —
        at most one run in flight fleet-wide).
        """
        key = key or agent.name
        existing = task_map.get(key)
        if existing and not existing.done():
            _log(f"scheduler: {kind} run already in flight — skipping fire for '{agent.name}'")
            return
        _log(fired_msg)

        async def _run() -> None:
            try:
                await callback(agent.name, agent)
            except Exception as e:
                _log(f"scheduler: {kind} callback failed for '{agent.name}': {e}")

        task_map[key] = asyncio.create_task(_run())

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
                self._spawn_agent_callback(
                    self._dream_tasks, self._dream_callback, agent, kind="dream",
                    fired_msg=f"scheduler: dream schedule fired for '{agent.name}' (cron={cron_expr})",
                )

    async def _check_librarian(self, now: float) -> None:
        """Check if the KB librarian should run.

        Fires for agents with librarian_enabled=True on their librarian_schedule.
        Only runs if new raw sources exist since the last run.
        """
        if not self._librarian_callback:
            return

        agents = self._registry.list(enabled_only=True)

        for agent in agents:
            if not getattr(agent, "librarian_enabled", False):
                continue

            cron_expr = getattr(agent, "librarian_schedule", "0 4 * * *") or "0 4 * * *"
            tz_name = getattr(agent, "dream_timezone", "") or "America/Los_Angeles"

            try:
                tz = ZoneInfo(tz_name)
            except (KeyError, ValueError):
                tz = ZoneInfo("UTC")

            dt = datetime.fromtimestamp(now, tz=tz)
            current_minute = dt.hour * 60 + dt.minute

            # Dedup — don't fire twice for the same (date, minute)
            dedup_key = (date.today().isoformat(), current_minute)
            if self._last_librarian_check.get(agent.name) == dedup_key:
                continue

            if cron_matches(cron_expr, dt):
                self._last_librarian_check[agent.name] = dedup_key
                self._spawn_agent_callback(
                    self._librarian_tasks, self._librarian_callback, agent, kind="librarian",
                    fired_msg=f"scheduler: librarian schedule fired for '{agent.name}' "
                              f"(cron={cron_expr})",
                    key=self.LIBRARIAN_GLOBAL_KEY,
                )

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

        def _fetch() -> tuple[int, str]:
            req = urllib.request.Request(
                trigger.url, method=trigger.method or "GET",
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                body_bytes = resp.read(65536)  # cap at 64KB
                return resp.status, body_bytes.decode(errors="replace")

        try:
            # Off-loop: a slow watched URL must not stall the shared event loop
            # (cf. the run_in_executor pattern in pollers.py).
            status_code, body_text = await asyncio.to_thread(_fetch)
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
