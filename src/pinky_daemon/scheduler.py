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
import math
import os
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from pinky_daemon.agent_registry import (
    AgentRegistry,
    PendingScheduleWake,
    RecurringScheduleStaleDrop,
)
from pinky_daemon.cron_utils import _field_matches
from pinky_daemon.transport_state import SessionState
from pinky_daemon.watchdog_log import log_watchdog_decision


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


_PROVEN_LIVE_HEARTBEAT_STATUSES = frozenset(
    {"alive", "ok", "busy", "finishing"}
)

# How recent an agent-origin heartbeat must be to suppress the owner alert on
# an unconfirmed schedule delivery. Deliberately short: it must cover "the
# agent was demonstrably running while the receipt window elapsed", not "the
# agent was up at some point today".
_UNDELIVERED_ALERT_LIVENESS_WINDOW = 120.0

# so a daemon restart can never deliver hours-old frozen prompts. Constructor
# injection below keeps fleet policy configurable and tests deterministic.
_PERSISTED_WAKE_MAX_AGE_SEC = 60 * 60

# Throttle warnings about oversized schedule prompts to avoid log spam.
# These warnings happen when a schedule's prompt exceeds the gRPC message
# size limit; throttling prevents re-warning on every delivery attempt.
_SCHEDULE_PROMPT_WARN_INTERVAL_SEC = 15 * 60

# Prompt size threshold (bytes) above which warnings are logged.
_SCHEDULE_PROMPT_WARN_THRESHOLD = 8000

_RECEIPT_EXTENSION_MAX_AGE_ENV = "PINKY_SCHEDULE_RECEIPT_EXTENSION_MAX_AGE_SEC"
_RECEIPT_EXTENSION_MAX_AGE_SEC = 60 * 60
_RECEIPT_EXTENSION_ATTEMPT_CAP_ENV = (
    "PINKY_SCHEDULE_RECEIPT_EXTENSION_ATTEMPT_CAP"
)
_RECEIPT_EXTENSION_ATTEMPT_CAP = 3
_ABANDONED_RECEIPT_OBSERVER_INTERVAL_SEC = 1.0
_PENDING_WAKE_LIVENESS_DRAIN_INTERVAL_SEC = 60
_OUTBOX_DRAIN_EXTENSION_ATTEMPT_CAP_ENV = (
    "PINKY_OUTBOX_DRAIN_EXTENSION_ATTEMPT_CAP"
)
_OUTBOX_DRAIN_EXTENSION_ATTEMPT_CAP = 30
_OUTBOX_DRAIN_EXTENSION_MAX_AGE_ENV = (
    "PINKY_OUTBOX_DRAIN_EXTENSION_MAX_AGE_SEC"
)
_OUTBOX_DRAIN_EXTENSION_MAX_AGE_SEC = 30 * 60
_OUTBOX_DRAIN_PROBE_TIMEOUT_ENV = "PINKY_OUTBOX_DRAIN_PROBE_TIMEOUT_SEC"
_OUTBOX_DRAIN_PROBE_TIMEOUT_SEC = 5.0
_OUTBOX_REAPER_RETAIN_ACCEPTED_ENV = (
    "PINKY_OUTBOX_REAPER_RETAIN_ACCEPTED_SEC"
)
_OUTBOX_REAPER_RETAIN_ABANDONED_ENV = (
    "PINKY_OUTBOX_REAPER_RETAIN_ABANDONED_SEC"
)
_OUTBOX_REAPER_RETAIN_PARKED_ENV = "PINKY_OUTBOX_REAPER_RETAIN_PARKED_SEC"
_OUTBOX_REAPER_PAYLOAD_TRIM_ENV = (
    "PINKY_OUTBOX_REAPER_PAYLOAD_TRIM_AFTER_SEC"
)
_OUTBOX_REAPER_FAILURE_BACKOFF_ENV = (
    "PINKY_OUTBOX_REAPER_FAILURE_BACKOFF_SEC"
)
_OUTBOX_REAPER_RETAIN_ACCEPTED_SEC = 7 * 24 * 60 * 60
_OUTBOX_REAPER_RETAIN_ABANDONED_SEC = 14 * 24 * 60 * 60
_OUTBOX_REAPER_RETAIN_PARKED_SEC = 30 * 24 * 60 * 60
_OUTBOX_REAPER_PAYLOAD_TRIM_AFTER_SEC = 48 * 60 * 60
_OUTBOX_REAPER_FAILURE_BACKOFF_SEC = 60 * 60


# ── Rate Limit Gating ───────────────────────────────────────

_RATE_LIMIT_FILE = "/tmp/claude-rate-limits.json"
_RATE_LIMIT_THRESHOLD = 80  # percent — skip heartbeats above this

# Il limite di retry sui wake persistiti vive ora in
# AgentScheduler.PERSISTED_WAKE_ATTEMPT_CAP (parcheggio, non discard).


class _ReceiptAbandonedError(RuntimeError):
    """An unconfirmed wake exhausted its receipt-extension budget."""


@dataclass
class _OutboxDrainExtensionState:
    """One oldest pending fire's consecutive busy-drain evidence."""

    oldest_fired_at: float
    attempts: int = 0
    alerted: bool = False


def _positive_env_seconds(name: str, default: float) -> float:
    """Read one finite positive duration, falling back loudly."""
    raw_value = os.environ.get(name, str(default))
    try:
        value = float(raw_value)
        if not math.isfinite(value) or value <= 0:
            raise ValueError("duration must be finite and positive")
    except (TypeError, ValueError):
        _log(
            f"scheduler: invalid {name} duration {raw_value!r}; "
            f"using {default:g}s"
        )
        return default
    return value


def _positive_env_int(name: str, default: int) -> int:
    """Read one positive integer, falling back loudly."""
    raw_value = os.environ.get(name, str(default))
    try:
        value = int(raw_value)
        if str(value) != raw_value.strip() or value <= 0:
            raise ValueError("value must be a positive integer")
    except (TypeError, ValueError):
        _log(
            f"scheduler: invalid {name} value {raw_value!r}; using {default}"
        )
        return default
    return value


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


class ScheduleWakeReceipt:
    """Capability that durably accepts one exact scheduler fire.

    The transport calls ``accept`` at its real acceptance edge and only then
    resolves the process-local Future.  This ordering is the load-bearing
    crash-gap guarantee for issue #991.
    """

    def __init__(
        self,
        registry: AgentRegistry,
        schedule_id: int,
        fired_at: float,
    ) -> None:
        self.schedule_id = schedule_id
        self.fired_at = fired_at
        self._registry = registry

    def accept(self) -> bool:
        """Commit the positive receipt synchronously and idempotently."""
        return self._registry.confirm_pending_schedule_wake_by_fire(
            self.schedule_id,
            self.fired_at,
            delivered_at=time.time(),
        )

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
        delivery_drain_busy_fn=None,
        delivery_inflight_fn=None,
        delivery_queued_fn=None,
        delivery_cancel_queued_fn=None,
        owner_notify_callback=None,
        trigger_store=None,
        activity=None,
        tick_interval: int = 30,
        schedule_delivery_timeout: float = 600.0,
        pending_wake_max_age_sec: float = _PERSISTED_WAKE_MAX_AGE_SEC,
        receipt_extension_attempt_cap: int | None = None,
        receipt_extension_max_age_sec: float | None = None,
        outbox_drain_extension_attempt_cap: int | None = None,
        outbox_drain_extension_max_age_sec: float | None = None,
        outbox_drain_probe_timeout_sec: float | None = None,
    ) -> None:
        self._registry = registry
        # async fn(agent_name, session_id, prompt, *, schedule_receipt=None)
        # -> bool | Awaitable[bool]. Production API callbacks accept the
        # optional durable receipt capability; legacy/test callbacks retain
        # the original three-argument contract.
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
        # A pasted-unresolved prompt cannot be safely cancelled and replayed,
        # but it also cannot hold the per-agent lock forever. Receipt waiting
        # is therefore capped against the wake's durable original fired_at.
        # At the ceiling the waiter detaches, the exact row is explicitly
        # abandoned, and a later positive receipt can still win.
        self._delivery_inflight_fn = delivery_inflight_fn
        # fn(agent_name) -> bool. A fresh, drain-specific pane-state probe
        # evaluated under the scheduler's per-agent replay lock. This is
        # deliberately narrower than the wide watchdog signal above: a stale
        # inflight meta plus periodic monitor output may keep the watchdog
        # positive after the REPL has reported a newer idle boundary (#1098).
        # Missing implementations fall back to delivery_busy_fn.
        self._delivery_drain_busy_fn = delivery_drain_busy_fn
        # fn(agent_name, prompt) -> bool. True means THIS wake remains queued
        # and recallable before its first pane paste on a live transport. This
        # is positive in-progress evidence just like ``delivery_inflight_fn``:
        # timing it out would persist a replay row while the original remains
        # deliverable. The companion cancel callback owns the ceiling edge.
        self._delivery_queued_fn = delivery_queued_fn
        # fn(agent_name, prompt) -> bool | Awaitable[bool]. True proves the
        # exact queued-unpasted turn was recalled. False means the scheduler
        # must re-read transport state: the turn may have raced to paste, in
        # which case the unrecallable inflight abandonment path owns it.
        self._delivery_cancel_queued_fn = delivery_cancel_queued_fn
        # async fn(agent_name, text) -> bool. Delivers operator-facing owner
        # alerts for terminal dead-letter events (bounded receipt/drain expiry,
        # PERSISTED_WAKE_PARKED, stale one-shot drop) through an out-of-band
        # transport. Routine FIRED-BUT-UNDELIVERED receipt failures are logged
        # only, NOT owner-notified (#1043).
        self._owner_notify_callback = owner_notify_callback
        self._trigger_store = trigger_store  # TriggerStore | None
        self._activity = activity  # ActivityStore | None
        self._tick_interval = tick_interval
        self._schedule_delivery_timeout = schedule_delivery_timeout
        if pending_wake_max_age_sec <= 0:
            raise ValueError("pending_wake_max_age_sec must be positive")
        self._pending_wake_max_age_sec = float(pending_wake_max_age_sec)
        if receipt_extension_attempt_cap is None:
            receipt_extension_attempt_cap = _positive_env_int(
                _RECEIPT_EXTENSION_ATTEMPT_CAP_ENV,
                _RECEIPT_EXTENSION_ATTEMPT_CAP,
            )
        if (
            isinstance(receipt_extension_attempt_cap, bool)
            or not isinstance(receipt_extension_attempt_cap, int)
            or receipt_extension_attempt_cap <= 0
        ):
            raise ValueError(
                "receipt_extension_attempt_cap must be a positive integer"
            )
        self._receipt_extension_attempt_cap = receipt_extension_attempt_cap
        if receipt_extension_max_age_sec is None:
            raw_extension_max_age = os.environ.get(
                _RECEIPT_EXTENSION_MAX_AGE_ENV,
                str(_RECEIPT_EXTENSION_MAX_AGE_SEC),
            )
            try:
                receipt_extension_max_age_sec = float(raw_extension_max_age)
                if (
                    not math.isfinite(receipt_extension_max_age_sec)
                    or receipt_extension_max_age_sec <= 0
                ):
                    raise ValueError("ceiling must be finite and positive")
            except (TypeError, ValueError):
                receipt_extension_max_age_sec = _RECEIPT_EXTENSION_MAX_AGE_SEC
                _log(
                    "scheduler: invalid receipt-extension ceiling "
                    f"{raw_extension_max_age!r}; using "
                    f"{_RECEIPT_EXTENSION_MAX_AGE_SEC:g}s"
                )
        if (
            not math.isfinite(receipt_extension_max_age_sec)
            or receipt_extension_max_age_sec <= 0
        ):
            raise ValueError(
                "receipt_extension_max_age_sec must be finite and positive"
            )
        self._receipt_extension_max_age_sec = float(
            receipt_extension_max_age_sec
        )
        if outbox_drain_extension_attempt_cap is None:
            outbox_drain_extension_attempt_cap = _positive_env_int(
                _OUTBOX_DRAIN_EXTENSION_ATTEMPT_CAP_ENV,
                _OUTBOX_DRAIN_EXTENSION_ATTEMPT_CAP,
            )
        if (
            isinstance(outbox_drain_extension_attempt_cap, bool)
            or not isinstance(outbox_drain_extension_attempt_cap, int)
            or outbox_drain_extension_attempt_cap <= 0
        ):
            raise ValueError(
                "outbox_drain_extension_attempt_cap must be a positive integer"
            )
        self._outbox_drain_extension_attempt_cap = (
            outbox_drain_extension_attempt_cap
        )
        if outbox_drain_extension_max_age_sec is None:
            outbox_drain_extension_max_age_sec = _positive_env_seconds(
                _OUTBOX_DRAIN_EXTENSION_MAX_AGE_ENV,
                _OUTBOX_DRAIN_EXTENSION_MAX_AGE_SEC,
            )
        if (
            not math.isfinite(outbox_drain_extension_max_age_sec)
            or outbox_drain_extension_max_age_sec <= 0
        ):
            raise ValueError(
                "outbox_drain_extension_max_age_sec must be finite and positive"
            )
        self._outbox_drain_extension_max_age_sec = float(
            outbox_drain_extension_max_age_sec
        )
        if outbox_drain_probe_timeout_sec is None:
            outbox_drain_probe_timeout_sec = _positive_env_seconds(
                _OUTBOX_DRAIN_PROBE_TIMEOUT_ENV,
                _OUTBOX_DRAIN_PROBE_TIMEOUT_SEC,
            )
        if (
            not math.isfinite(outbox_drain_probe_timeout_sec)
            or outbox_drain_probe_timeout_sec <= 0
        ):
            raise ValueError(
                "outbox_drain_probe_timeout_sec must be finite and positive"
            )
        self._outbox_drain_probe_timeout_sec = float(
            outbox_drain_probe_timeout_sec
        )
        self._outbox_reaper_retain_accepted_sec = _positive_env_seconds(
            _OUTBOX_REAPER_RETAIN_ACCEPTED_ENV,
            _OUTBOX_REAPER_RETAIN_ACCEPTED_SEC,
        )
        self._outbox_reaper_retain_abandoned_sec = _positive_env_seconds(
            _OUTBOX_REAPER_RETAIN_ABANDONED_ENV,
            _OUTBOX_REAPER_RETAIN_ABANDONED_SEC,
        )
        self._outbox_reaper_retain_parked_sec = _positive_env_seconds(
            _OUTBOX_REAPER_RETAIN_PARKED_ENV,
            _OUTBOX_REAPER_RETAIN_PARKED_SEC,
        )
        self._outbox_reaper_payload_trim_after_sec = _positive_env_seconds(
            _OUTBOX_REAPER_PAYLOAD_TRIM_ENV,
            _OUTBOX_REAPER_PAYLOAD_TRIM_AFTER_SEC,
        )
        self._outbox_reaper_failure_backoff_sec = _positive_env_seconds(
            _OUTBOX_REAPER_FAILURE_BACKOFF_ENV,
            _OUTBOX_REAPER_FAILURE_BACKOFF_SEC,
        )
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
        self._detached_receipt_tasks: set[asyncio.Task] = set()
        self._pending_replay_tasks: dict[str, asyncio.Task] = {}
        self._pending_replay_again: set[str] = set()
        self._pending_replay_drain_recheck: set[str] = set()
        self._outbox_drain_extensions: dict[
            str, _OutboxDrainExtensionState
        ] = {}
        self._receipt_extension_alerted: set[tuple[int, float]] = set()
        self._owner_alert_tasks: set[asyncio.Task] = set()
        self._last_schedule_prompt_warn_at: float | None = None
        self._last_pending_wake_liveness_drain_at: float | None = None
        self._last_outbox_reaper_day: date | None = None
        self._last_outbox_reaper_failed_at: float | None = None
        # Resurrection rate-limit: agent_name -> list[timestamp] of recent attempts.
        # Used by _check_heartbeats to cap how often we ping the heartbeat_callback
        # for a stuck agent (avoid thrashing on a persistently-broken session).
        self._resurrection_attempts: dict[str, list[float]] = {}
        # In-process dedup for replayed wakes during this startup session.
        # Prevents duplicate delivery when daemon restarts before receipt
        # persists to DB: (schedule_id, fired_at) tuples already delivered.
        self._replayed_wakes_this_startup: set[tuple[int, float]] = set()
        # Throttle per pending wake to prevent replay cycling: pending_wake_id -> last_replay_attempt_time.
        # When _drain_outbox_if_pending() is called repeatedly (e.g. on each heartbeat),
        # this prevents creating new replay tasks for the same pending wake within 10 minutes.
        # Addresses bug: undelivered wakes cycling 4x in 4h due to repeated replay attempts.
        self._pending_wake_replay_throttle: dict[int, float] = {}

    async def start(self) -> None:
        """Start the scheduler background loop."""
        if self._running:
            return
        self._running = True
        now = time.time()
        self._run_outbox_reaper_if_due(now)
        self._warn_oversized_schedule_prompts(now, force=True)
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
        detached_receipts = list(self._detached_receipt_tasks)
        for task in detached_receipts:
            if not task.done():
                task.cancel()
        if detached_receipts:
            await asyncio.gather(*detached_receipts, return_exceptions=True)
        self._detached_receipt_tasks.clear()
        self._pending_replay_again.clear()
        self._pending_replay_drain_recheck.clear()
        replay_tasks = list(self._pending_replay_tasks.values())
        for task in replay_tasks:
            if not task.done():
                task.cancel()
        if replay_tasks:
            await asyncio.gather(*replay_tasks, return_exceptions=True)
        self._pending_replay_tasks.clear()
        self._outbox_drain_extensions.clear()
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

        self._run_outbox_reaper_if_due(now)
        self._warn_oversized_schedule_prompts(now)

        # Check cron schedules
        await self._check_schedules(now)

        # Check clock-aligned wakes
        await self._check_clock_aligned_wakes(now)

        # Idle notifications are the primary low-latency outbox drain.  This
        # bounded fallback prevents a lost edge from stranding a busy-deferred
        # fire forever, including agents that keep the default heartbeat=0.
        self._check_pending_wake_liveness(now)

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

    def _run_outbox_reaper_if_due(self, now: float) -> bool:
        """Run host-owned outbox maintenance once per local calendar day."""
        current_day = date.fromtimestamp(now)
        if self._last_outbox_reaper_day == current_day:
            return False
        if (
            self._last_outbox_reaper_failed_at is not None
            and now - self._last_outbox_reaper_failed_at
            < self._outbox_reaper_failure_backoff_sec
        ):
            return False
        try:
            self._registry.reap_pending_schedule_wakes(
                now=now,
                abandon_after=self._receipt_extension_max_age_sec,
                retain_accepted=self._outbox_reaper_retain_accepted_sec,
                retain_abandoned=self._outbox_reaper_retain_abandoned_sec,
                retain_parked=self._outbox_reaper_retain_parked_sec,
                payload_trim_after=(
                    self._outbox_reaper_payload_trim_after_sec
                ),
            )
        except Exception as exc:
            self._last_outbox_reaper_failed_at = now
            _log(
                "scheduler: outbox reaper failed: "
                f"{type(exc).__name__}: {exc}"
            )
            return False
        self._last_outbox_reaper_day = current_day
        self._last_outbox_reaper_failed_at = None
        return True

    def _warn_oversized_schedule_prompts(
        self,
        now: float,
        *,
        force: bool = False,
    ) -> None:
        """Warn at startup and periodically for large enabled prompts."""
        last_warn = self._last_schedule_prompt_warn_at
        if (
            not force
            and last_warn is not None
            and now - last_warn < _SCHEDULE_PROMPT_WARN_INTERVAL_SEC
        ):
            return
        self._last_schedule_prompt_warn_at = now

        try:
            schedules = self._registry.get_oversized_enabled_schedule_prompts(
                _SCHEDULE_PROMPT_WARN_THRESHOLD
            )
        except Exception as exc:
            # A health guard must never prevent scheduler startup or a tick.
            _log(f"scheduler: large-prompt health check failed: {exc}")
            return

        for schedule_id, agent_name, name, prompt_length in schedules:
            _log(
                "scheduler: warning — large enabled schedule prompt "
                f"id={schedule_id} agent={agent_name[:64]!r} "
                f"name={name[:80]!r} prompt_chars={prompt_length} "
                f"threshold={_SCHEDULE_PROMPT_WARN_THRESHOLD}; "
                "tmux delivery requires stdin-backed load-buffer"
            )

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
                if schedule.direct_send:
                    claimed = self._registry.update_schedule_last_run(
                        schedule.id,
                        now,
                        expected_last_run=schedule.last_run,
                    )
                else:
                    claimed, _ledger_row = (
                        self._registry.claim_schedule_fire(
                            schedule.id,
                            timestamp=now,
                            expected_last_run=schedule.last_run,
                            agent_name=schedule.agent_name,
                            schedule_name=schedule.name,
                            prompt=(
                                schedule.prompt
                                or f"Scheduled wake: {schedule.name}"
                            ),
                        )
                    )
                if not claimed:
                    _log(
                        f"scheduler: lost last_run claim race for "
                        f"#{schedule.id} — skipping fire"
                    )
                    continue
                _log(f"scheduler: firing schedule '{schedule.name}' for agent '{schedule.agent_name}' (direct_send={schedule.direct_send}, one_shot={schedule.one_shot})")
                if self._activity:
                    try:
                        self._activity.log(schedule.agent_name, "schedule_fired", f"Schedule '{schedule.name}' fired")
                    except Exception:
                        pass
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
            if self._agent_busy_not_wedged(agent_name) or lock.locked():
                direct_sends = [
                    schedule for schedule in schedules if schedule.direct_send
                ]
                deferred = [
                    schedule for schedule in schedules if not schedule.direct_send
                ]
                for index, schedule in enumerate(direct_sends):
                    await self._deliver_schedule(
                        schedule,
                        attempt_started=(
                            attempt_started if index == 0 else None
                        ),
                    )
                if deferred:
                    if attempt_started is not None:
                        attempt_started.set()
                    _log(
                        f"scheduler: deferring {len(deferred)} fired "
                        f"schedule(s) for agent '{agent_name}' until the next "
                        "turn-idle boundary"
                    )
                return
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

    def notify_agent_idle(self, agent_name: str) -> None:
        """Drain durable scheduler work after a transport turn goes idle."""
        _log(
            f"scheduler: turn-idle delivery trigger for agent '{agent_name}'"
        )
        self.replay_pending_for_agent(agent_name)

    def _alert_stale_one_shot_drop(
        self,
        *,
        agent_name: str,
        schedule_id: int,
        schedule_name: str,
        pending_id: int,
        row_age: float,
        replay_max_age: float,
    ) -> None:
        """Owner-alert a stale-dropped one-shot fire.

        A one-shot has no next occurrence (``_check_schedules`` auto-disables
        it once fired), so a fire dropped for staleness is owed work lost unless
        re-armed. Fired from both the replay pass and the live cohort path so a
        queued-then-stale one-shot is never silently lost.
        """
        self._queue_owner_alert(
            agent_name,
            (
                "🚨 STALE ONE-SHOT WAKE DROPPED: outbox row "
                f"#{pending_id} for schedule '{schedule_name}' "
                f"(#{schedule_id}) on agent '{agent_name}' aged past its "
                f"replay window ({row_age:.1f}s > {replay_max_age:.1f}s). "
                "The stale wake was discarded and this one-shot has no next "
                "occurrence; verify the owed work and re-arm it if needed."
            ),
        )

    def _record_recurring_stale_drop(
        self,
        *,
        agent_name: str,
        schedule_id: int,
        schedule_name: str,
        dropped_at: float,
        row_age: float,
    ) -> None:
        """Persist one recurring drop without creating owner-facing delivery."""
        try:
            notice = self._registry.record_recurring_schedule_stale_drop(
                schedule_id,
                agent_name=agent_name,
                schedule_name=schedule_name,
                dropped_at=dropped_at,
                row_age_s=row_age,
            )
        except Exception as exc:
            # Surfacing bookkeeping must never turn a terminal stale drop back
            # into a replay candidate or fail the scheduler cohort.
            _log(
                "scheduler: recurring stale-drop surfacing record failed for "
                f"schedule '{schedule_name}' (#{schedule_id}) on agent "
                f"'{agent_name}': {type(exc).__name__}: {exc}"
            )
            return
        _log(
            "scheduler: recurring stale-drop notice aggregated for schedule "
            f"'{schedule_name}' (#{schedule_id}) on agent '{agent_name}': "
            f"count={notice.drop_count}"
        )

    async def _deliver_schedule(
        self,
        schedule,
        *,
        attempt_started: asyncio.Event | None = None,
    ) -> None:
        """Attempt one fired schedule and record confirmed delivery separately."""
        confirmed = False
        failure_reason = "no delivery callback configured"

        # Direct unit/in-process callers can bypass ``_check_schedules``. Keep
        # their exact fire represented too; production fires already have this
        # row from the atomic claim transaction above.
        if not schedule.direct_send and schedule.last_run > 0:
            row, _created = self._registry.persist_schedule_wake(
                schedule.id,
                agent_name=schedule.agent_name,
                schedule_name=schedule.name,
                prompt=self._wake_prompt(schedule),
                fired_at=schedule.last_run,
            )
            # A terminal or already-accepted fire must not be delivered:
            # an operator may have discarded this fire while its cohort task
            # waited behind the per-agent delivery lock. Delivering would
            # resurrect the tombstone (and the delivery-confirm would clear it).
            # Set the cohort-start event first so ``_check_schedules`` does not
            # wait out its timeout on a discarded first cohort member.
            if row is not None and (
                row.parked_at > 0
                or row.abandoned_at > 0
                or row.accepted_at > 0
            ):
                if attempt_started is not None:
                    attempt_started.set()
                return

            # A fire that aged past its replay window while its cohort task
            # waited behind the per-agent delivery lock must not be pasted: it
            # would deliver an hours-old prompt, and a busy session queues
            # several such fires that then drain as duplicate stale wakes. Drop
            # it, matching the replay path's staleness ceiling. Set the
            # cohort-start event first so ``_check_schedules`` does not wait out
            # its timeout on a stale first cohort member.
            replay_max_age = self._pending_wake_replay_max_age(
                schedule, schedule.last_run
            )
            drop_checked_at = time.time()
            age = max(0.0, drop_checked_at - schedule.last_run)
            if age > replay_max_age:
                dropped = (
                    self._registry.delete_pending_schedule_wake(row.id)
                    if row is not None
                    else False
                )
                _log(
                    f"scheduler: COHORT_STALE_DROPPED pending "
                    f"#{row.id if row is not None else '?'}, schedule "
                    f"'{schedule.name}' (#{schedule.id}) for agent "
                    f"'{schedule.agent_name}': age_s={age:.1f} "
                    f"max_age_s={replay_max_age:.1f} dropped={dropped}"
                )
                if dropped and schedule.one_shot:
                    # A one-shot was auto-disabled on fire, so a stale drop has
                    # no next occurrence — mirror the replay path and alert.
                    self._alert_stale_one_shot_drop(
                        agent_name=schedule.agent_name,
                        schedule_id=schedule.id,
                        schedule_name=schedule.name,
                        pending_id=row.id if row is not None else 0,
                        row_age=age,
                        replay_max_age=replay_max_age,
                    )
                elif dropped:
                    self._record_recurring_stale_drop(
                        agent_name=schedule.agent_name,
                        schedule_id=schedule.id,
                        schedule_name=schedule.name,
                        dropped_at=drop_checked_at,
                        row_age=age,
                    )
                if attempt_started is not None:
                    attempt_started.set()
                return

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
            except _ReceiptAbandonedError:
                return
            except asyncio.TimeoutError as exc:
                failure_reason = str(exc) or (
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
            receipted_pending = (
                self._registry.confirm_pending_schedule_wake_by_fire(
                    schedule.id,
                    schedule.last_run,
                    delivered_at=delivered_at,
                )
            )
            if not receipted_pending:
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
                f"'{schedule.agent_name}' (fired_at={schedule.last_run})"
            )
            return

        self._record_schedule_undelivered(schedule, failure_reason)

    def _record_schedule_undelivered(
        self, schedule, failure_reason: str
    ) -> None:
        """Persist and loudly log one unconfirmed fired schedule.

        Owner-alert policy (#420, 082c6d0a): the FIRST unconfirmed delivery
        alerts the owner as an early warning, the intermediate retry attempts
        1..CAP-1 are suppressed (they are frequently false positives — the wake
        persists and replays on its own; #1043), and the terminal attempt at
        CAP alerts again. So: alert at the first failure and at the final park,
        silence in between.
        """
        persisted = False
        alert_this_failure = True
        if not schedule.direct_send:
            try:
                row, _created = self._registry.persist_schedule_wake(
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
                row, alert_this_failure = (
                    self._registry.record_schedule_wake_failure(
                        schedule.id,
                        schedule.last_run,
                        failure_reason,
                    )
                )
                if row is not None and row.accepted_at > 0:
                    _log(
                        f"scheduler: durable receipt won teardown race for "
                        f"schedule '{schedule.name}' (#{schedule.id}) for "
                        f"agent '{schedule.agent_name}' "
                        f"(fired_at={schedule.last_run}); suppressing stale "
                        "negative delivery verdict"
                    )
                    return
                if row is not None and row.parked_at > 0:
                    _log(
                        f"scheduler: schedule '{schedule.name}' "
                        f"(#{schedule.id}) for agent "
                        f"'{schedule.agent_name}' is already quarantined "
                        f"(fired_at={schedule.last_run}); suppressing "
                        "duplicate undelivered verdict"
                    )
                    return
                if row is not None and row.abandoned_at > 0:
                    _log(
                        f"scheduler: schedule '{schedule.name}' "
                        f"(#{schedule.id}) for agent "
                        f"'{schedule.agent_name}' is already abandoned "
                        f"(fired_at={schedule.last_run}); suppressing "
                        "duplicate undelivered verdict"
                    )
                    return
                if (
                    persisted
                    and row is not None
                    and 0 < row.attempts < self.PERSISTED_WAKE_ATTEMPT_CAP
                ):
                    _log(
                        f"scheduler: schedule '{schedule.name}' "
                        f"(#{schedule.id}) for agent '{schedule.agent_name}' "
                        f"is on retry attempt {row.attempts} (of "
                        f"{self.PERSISTED_WAKE_ATTEMPT_CAP}); suppressing owner "
                        "alert — wake will be retried automatically"
                    )
                    return
                # Alert on attempts >= CAP since we've exhausted retries
                if (
                    persisted
                    and row is not None
                    and row.attempts >= self.PERSISTED_WAKE_ATTEMPT_CAP
                ):
                    self._queue_owner_alert(
                        schedule.agent_name,
                        (
                            f"🚨 FIRED BUT UNDELIVERED: schedule "
                            f"'{schedule.name}' (#{schedule.id}) on agent "
                            f"'{schedule.agent_name}' reached {row.attempts} "
                            f"unconfirmed delivery attempts. {failure_reason}"
                        ),
                    )
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
        if alert_this_failure and self._agent_recently_proved_live(
            schedule.agent_name
        ):
            _log(
                f"scheduler: agent '{schedule.agent_name}' posted an "
                f"agent-origin heartbeat within "
                f"{_UNDELIVERED_ALERT_LIVENESS_WINDOW:g}s of schedule "
                f"'{schedule.name}' (#{schedule.id}) going unconfirmed; "
                "suppressing owner alert (the persisted wake replays on its "
                "own)"
            )
            alert_this_failure = False
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
        fresh_receipt_budget: bool = False,
    ) -> bool:
        """Wait for one exact receipt within its applicable delivery budget."""
        delivery_prompt, stale_drop_notices = (
            self._wake_prompt_with_recurring_stale_drops(schedule)
        )
        delivery = asyncio.create_task(
            self._wake_and_confirm(
                schedule,
                prompt=delivery_prompt,
                attempt_started=attempt_started,
            )
        )
        receipt_wait_attempts = 0
        try:
            while True:
                try:
                    remaining = max(
                        0.0,
                        self._receipt_extension_max_age_sec
                        - self._receipt_age(schedule),
                    )
                    if fresh_receipt_budget:
                        # Replay already proved this old prompt was never
                        # pasted. Give the owed first transport attempt one
                        # real acceptance window instead of cancelling its
                        # task at timeout=0 from the historical fired_at.
                        remaining = max(
                            remaining, self._schedule_delivery_timeout
                        )
                        fresh_receipt_budget = False
                    receipt_wait_attempts += 1
                    confirmed = await asyncio.wait_for(
                        asyncio.shield(delivery),
                        timeout=min(
                            self._schedule_delivery_timeout, remaining
                        ),
                    )
                    if confirmed and stale_drop_notices:
                        self._acknowledge_recurring_stale_drops(
                            schedule.agent_name, stale_drop_notices
                        )
                    return confirmed
                except asyncio.TimeoutError:
                    age = self._receipt_age(schedule)
                    wall_clock_expired = (
                        age >= self._receipt_extension_max_age_sec
                    )
                    attempts_expired = (
                        receipt_wait_attempts
                        >= self._receipt_extension_attempt_cap
                    )
                    if wall_clock_expired or attempts_expired:
                        bound_reason = (
                            "wall-clock cap"
                            if wall_clock_expired
                            else "attempt cap"
                        )
                        queued = self._wake_prompt_queued(
                            schedule, prompt=delivery_prompt
                        )
                        if queued:
                            cancelled = await self._cancel_queued_wake(
                                schedule, prompt=delivery_prompt
                            )
                            if cancelled:
                                # Transport recall is the load-bearing first
                                # transition. Retire the process-local waiter
                                # only after the pane turn is no longer
                                # deliverable, then abandon its durable row.
                                delivery.cancel()
                                await asyncio.gather(
                                    delivery, return_exceptions=True
                                )
                                _log(
                                    "scheduler: cancelled-queued schedule "
                                    f"'{schedule.name}' "
                                    f"(#{self._schedule_id(schedule)}) for "
                                    f"agent '{schedule.agent_name}' before "
                                    "receipt abandonment"
                                )
                                self._mark_abandoned_receipt(
                                    schedule,
                                    age=age,
                                    bound_reason=bound_reason,
                                    wait_attempts=receipt_wait_attempts,
                                )
                                raise _ReceiptAbandonedError

                            # Acceptance can land while the transport recall
                            # waits for its pane lock. Positive consumption is
                            # terminal and outranks both queued and pasted
                            # timeout handling.
                            await asyncio.sleep(0)
                            settled, confirmed = (
                                self._delivery_result_if_done(delivery)
                            )
                            if settled:
                                if confirmed and stale_drop_notices:
                                    self._acknowledge_recurring_stale_drops(
                                        schedule.agent_name,
                                        stale_drop_notices,
                                    )
                                return confirmed

                            # The queued probe and recall are separate edges.
                            # If pane delivery won between them, cancellation
                            # is no longer safe; retain late-receipt authority
                            # exactly like every other pasted turn.
                            if self._wake_prompt_inflight(
                                schedule, prompt=delivery_prompt
                            ):
                                self._abandon_receipt_wait(
                                    schedule,
                                    delivery,
                                    age=age,
                                    bound_reason=bound_reason,
                                    wait_attempts=receipt_wait_attempts,
                                    stale_drop_notices=stale_drop_notices,
                                )
                                raise _ReceiptAbandonedError

                            # A transport without an explicit recall hook (or
                            # one that failed while the exact turn remains
                            # queued) still gets the established receipt-
                            # cancellation fence. There is no await between
                            # the positive recheck and Task.cancel(), so the
                            # pane path cannot advance in this event-loop turn;
                            # its in-lock cancelled-receipt guard completes
                            # the recall before any later paste.
                            if self._wake_prompt_queued(
                                schedule, prompt=delivery_prompt
                            ):
                                task_cancelled = delivery.cancel()
                                await asyncio.gather(
                                    delivery, return_exceptions=True
                                )
                                if task_cancelled:
                                    _log(
                                        "scheduler: cancelled-queued schedule "
                                        f"'{schedule.name}' "
                                        f"(#{self._schedule_id(schedule)}) "
                                        f"for agent '{schedule.agent_name}' "
                                        "before receipt abandonment "
                                        "(receipt-fence fallback)"
                                    )
                                    self._mark_abandoned_receipt(
                                        schedule,
                                        age=age,
                                        bound_reason=bound_reason,
                                        wait_attempts=receipt_wait_attempts,
                                    )
                                    raise _ReceiptAbandonedError

                        inflight = self._wake_prompt_inflight(
                            schedule, prompt=delivery_prompt
                        )
                        if inflight:
                            self._abandon_receipt_wait(
                                schedule,
                                delivery,
                                age=age,
                                bound_reason=bound_reason,
                                wait_attempts=receipt_wait_attempts,
                                stale_drop_notices=stale_drop_notices,
                            )
                            raise _ReceiptAbandonedError

                        # A transcript acceptance can resolve the transport
                        # Future in the same event-loop turn as this timeout.
                        # Give the delivery task one scheduling edge before a
                        # stale negative cancel, then re-read exact state.
                        await asyncio.sleep(0)
                        settled, confirmed = self._delivery_result_if_done(
                            delivery
                        )
                        if settled:
                            if confirmed and stale_drop_notices:
                                self._acknowledge_recurring_stale_drops(
                                    schedule.agent_name,
                                    stale_drop_notices,
                                )
                            return confirmed
                        if self._wake_prompt_queued(
                            schedule, prompt=delivery_prompt
                        ):
                            # Positive recallability never owns a late-receipt
                            # observer. Cancel the delivery task so the
                            # transport's in-lock receipt fence prevents a
                            # later paste, then terminalize the exact fire.
                            delivery.cancel()
                            await asyncio.gather(
                                delivery, return_exceptions=True
                            )
                            self._mark_abandoned_receipt(
                                schedule,
                                age=age,
                                bound_reason=bound_reason,
                                wait_attempts=receipt_wait_attempts,
                            )
                            raise _ReceiptAbandonedError
                        if self._wake_prompt_inflight(
                            schedule, prompt=delivery_prompt
                        ):
                            self._abandon_receipt_wait(
                                schedule,
                                delivery,
                                age=age,
                                bound_reason=bound_reason,
                                wait_attempts=receipt_wait_attempts,
                                stale_drop_notices=stale_drop_notices,
                            )
                            raise _ReceiptAbandonedError
                        delivery.cancel()
                        await asyncio.gather(
                            delivery, return_exceptions=True
                        )
                        self._mark_abandoned_receipt(
                            schedule,
                            age=age,
                            bound_reason=bound_reason,
                            wait_attempts=receipt_wait_attempts,
                        )
                        raise _ReceiptAbandonedError
                    if self._agent_busy_not_wedged(schedule.agent_name):
                        _log(
                            f"scheduler: receipt still pending for schedule "
                            f"'{schedule.name}' "
                            f"(#{self._schedule_id(schedule)}) for agent "
                            f"'{schedule.agent_name}', but inflight watchdog "
                            "reports busy-not-wedged; extending delivery timeout "
                            f"(wait {receipt_wait_attempts}/"
                            f"{self._receipt_extension_attempt_cap})"
                        )
                        continue
                    if self._wake_prompt_inflight(
                        schedule, prompt=delivery_prompt
                    ):
                        _log(
                            f"scheduler: receipt still pending for schedule "
                            f"'{schedule.name}' "
                            f"(#{self._schedule_id(schedule)}) for agent "
                            f"'{schedule.agent_name}', but its prompt is "
                            "already pasted to the transport — a cancel "
                            "cannot recall it, and declaring undelivered "
                            "would re-persist a wake that is about to "
                            "execute (duplicate execution); extending "
                            f"(wait {receipt_wait_attempts}/"
                            f"{self._receipt_extension_attempt_cap})"
                        )
                        continue
                    if self._wake_prompt_queued(
                        schedule, prompt=delivery_prompt
                    ):
                        _log(
                            f"scheduler: receipt still pending for schedule "
                            f"'{schedule.name}' "
                            f"(#{self._schedule_id(schedule)}) for agent "
                            f"'{schedule.agent_name}', but its prompt is "
                            "queued-unpasted on a connected transport; "
                            "extending delivery timeout "
                            f"(wait {receipt_wait_attempts}/"
                            f"{self._receipt_extension_attempt_cap})"
                        )
                        continue
                    await asyncio.sleep(0)
                    settled, confirmed = self._delivery_result_if_done(
                        delivery
                    )
                    if settled:
                        if confirmed and stale_drop_notices:
                            self._acknowledge_recurring_stale_drops(
                                schedule.agent_name, stale_drop_notices
                            )
                        return confirmed
                    if self._wake_prompt_inflight(
                        schedule, prompt=delivery_prompt
                    ) or self._wake_prompt_queued(
                        schedule, prompt=delivery_prompt
                    ):
                        continue
                    delivery.cancel()
                    await asyncio.gather(delivery, return_exceptions=True)
                    raise
        except asyncio.CancelledError:
            delivery.cancel()
            await asyncio.gather(delivery, return_exceptions=True)
            raise

    def _agent_recently_proved_live(self, agent_name: str) -> bool:
        """Return True if the agent itself said it was alive very recently.

        Reads ``get_latest_agent_heartbeat`` — NOT ``get_latest_heartbeat`` —
        because the latter includes the synthetic ``server_presence`` rows this
        scheduler writes when it sees a CONNECTED transport. The dominant cause
        of an unconfirmed wake is exactly "transport CONNECTED but reader loop
        wedged on an LLM call", so using ``get_latest_heartbeat`` would accept
        the daemon's own writing as proof of life and hide a real crash.

        Fails closed: any error, missing row, or non-agent-authored status
        leaves the alert in place. This never widens the delivery timeout — a
        genuinely dead agent still pages the owner.
        """
        try:
            hb = self._registry.get_latest_agent_heartbeat(agent_name)
        except Exception as exc:
            _log(
                f"scheduler: liveness lookup failed for '{agent_name}': "
                f"{type(exc).__name__}: {exc}"
            )
            return False
        if hb is None or hb.status not in _PROVEN_LIVE_HEARTBEAT_STATUSES:
            return False
        return (time.time() - hb.timestamp) <= _UNDELIVERED_ALERT_LIVENESS_WINDOW

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

    async def _agent_busy_for_drain(self, agent_name: str) -> bool:
        """Re-read pane state without letting a hung probe hold the lane lock."""
        probe = self._delivery_drain_busy_fn or self._delivery_busy_fn
        if probe is None:
            return False
        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(probe, agent_name),
                timeout=self._outbox_drain_probe_timeout_sec,
            )
            return result is True
        except asyncio.TimeoutError:
            _log(
                f"scheduler: drain-time transport recheck timed out for "
                f"'{agent_name}' after "
                f"{self._outbox_drain_probe_timeout_sec:.1f}s; "
                "counting as busy and releasing the replay lock"
            )
            return True
        except Exception as exc:
            _log(
                f"scheduler: drain-time transport recheck failed for "
                f"'{agent_name}': {type(exc).__name__}: {exc}; deferring safely"
            )
            return True

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

    def replay_pending_for_agent(
        self, agent_name: str, *, drain_recheck: bool = False
    ) -> None:
        """Coalesce triggers while guaranteeing one replay after the active pass."""
        if drain_recheck:
            self._pending_replay_drain_recheck.add(agent_name)
        existing = self._pending_replay_tasks.get(agent_name)
        if existing is not None and not existing.done():
            self._pending_replay_again.add(agent_name)
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
            rerun = agent_name in self._pending_replay_again
            self._pending_replay_again.discard(agent_name)
            if rerun and not done.cancelled() and done.exception() is None:
                self.replay_pending_for_agent(agent_name)

        task.add_done_callback(_done)

    async def _replay_pending_for_agent(self, agent_name: str) -> None:
        """Deliver one agent's current owed wakes with exact receipts."""
        lock = self._schedule_delivery_locks.setdefault(agent_name, asyncio.Lock())
        async with lock:
            drain_recheck = agent_name in self._pending_replay_drain_recheck
            self._pending_replay_drain_recheck.discard(agent_name)
            if drain_recheck:
                await self._replay_pending_locked(
                    agent_name, drain_recheck=True
                )
            else:
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
                "is stopped to prevent a storm; the exact fire remains in "
                "the ledger as a queryable quarantine."
            ),
        )
        return True

    def _record_outbox_drain_extension(
        self, agent_name: str, *, summary: dict, now: float
    ) -> bool:
        """Bound consecutive drain deferrals by count and oldest-fire age."""
        oldest_fired_at = float(summary["oldest_fired_at"])
        state = self._outbox_drain_extensions.get(agent_name)
        if state is None or state.oldest_fired_at != oldest_fired_at:
            state = _OutboxDrainExtensionState(oldest_fired_at)
            self._outbox_drain_extensions[agent_name] = state
        state.attempts += 1
        oldest_age = max(0.0, now - oldest_fired_at)
        wall_clock_expired = (
            oldest_age >= self._outbox_drain_extension_max_age_sec
        )
        attempts_expired = (
            state.attempts >= self._outbox_drain_extension_attempt_cap
        )
        if not (wall_clock_expired or attempts_expired):
            _log(
                f"scheduler: periodic outbox drain deferred for "
                f"'{agent_name}': fresh transport recheck remains busy "
                f"(attempt {state.attempts}/"
                f"{self._outbox_drain_extension_attempt_cap}, "
                f"oldest_age_s={oldest_age:.1f}/"
                f"{self._outbox_drain_extension_max_age_sec:.1f})"
            )
            return False

        bound_reason = (
            "wall-clock cap" if wall_clock_expired else "attempt cap"
        )
        targeted, abandoned = self._abandon_outbox_drain_rows(
            agent_name,
            now=now,
            bound_reason=bound_reason,
            state=state,
            oldest_age=oldest_age,
        )
        if state.alerted:
            _log(
                "scheduler: OUTBOX_DRAIN_EXTENSION_EXPIRED owner already "
                f"alerted for '{agent_name}' oldest_fired_at={oldest_fired_at} "
                f"bound={bound_reason!r}; transport recheck remains busy, "
                f"terminalized={abandoned}/{targeted}"
            )
            return True
        state.alerted = True
        _log(
            f"scheduler: OUTBOX_DRAIN_EXTENSION_EXPIRED agent='{agent_name}' "
            f"oldest_fired_at={oldest_fired_at} oldest_age_s={oldest_age:.1f} "
            f"attempts={state.attempts} bound={bound_reason!r} "
            f"terminalized={abandoned}/{targeted}; owner alert queued"
        )
        self._queue_owner_alert(
            agent_name,
            (
                "🚨 OUTBOX DRAIN EXTENSION EXPIRED: agent "
                f"'{agent_name}' still reports busy at the fresh drain-time "
                f"transport check after hitting its {bound_reason} "
                f"({state.attempts} checks, oldest wake age "
                f"{oldest_age:.1f}s). {abandoned}/{targeted} active wake row(s) "
                "were moved to explicit OUTBOX_DRAIN_ABANDONED terminal state; "
                "a late positive receipt can still supersede abandonment. "
                "Inspect the transport and wake ledger before intervening."
            ),
        )
        return True

    def _abandon_outbox_drain_rows(
        self,
        agent_name: str,
        *,
        now: float,
        bound_reason: str,
        state: _OutboxDrainExtensionState,
        oldest_age: float,
    ) -> tuple[int, int]:
        """Move every active row behind an expired drain to honest terminal state."""
        try:
            pending_wakes = self._registry.list_pending_schedule_wakes(
                agent_name
            )
        except Exception as exc:
            _log(
                "scheduler: OUTBOX_DRAIN_ABANDON_FAILURE listing active rows "
                f"for '{agent_name}': {type(exc).__name__}: {exc}"
            )
            return 0, 0

        reason = (
            "OUTBOX_DRAIN_ABANDONED: drain-extension budget expired "
            f"by {bound_reason} (oldest_fired_at={state.oldest_fired_at}, "
            f"oldest_age={oldest_age:.1f}s, checks={state.attempts}, "
            f"attempt_cap={self._outbox_drain_extension_attempt_cap}, "
            f"max_age={self._outbox_drain_extension_max_age_sec:.1f}s)"
        )
        abandoned = 0
        for pending in pending_wakes:
            try:
                if self._registry.abandon_pending_schedule_wake(
                    pending.id,
                    abandoned_at=now,
                    reason=reason,
                ):
                    abandoned += 1
            except Exception as exc:
                _log(
                    "scheduler: OUTBOX_DRAIN_ABANDON_FAILURE pending "
                    f"#{pending.id} for '{agent_name}': "
                    f"{type(exc).__name__}: {exc}"
                )
        return len(pending_wakes), abandoned

    async def _replay_pending_locked(
        self, agent_name: str, *, drain_recheck: bool = False
    ) -> None:
        """Reap zombies, collapse recurrences, then replay under the agent lock."""
        replay_now = time.time()
        health = self._registry.get_pending_schedule_wake_health(
            agent_name, now=replay_now
        )
        if health and health[0]["count"] > 0:
            summary = health[0]
            _log(
                f"scheduler: PERSISTED_WAKE_OUTBOX_HEALTH agent="
                f"'{agent_name}' count={summary['count']} "
                f"abandoned_count={summary['abandoned_count']} "
                f"oldest_age_s={summary['oldest_age_seconds']:.1f} "
                f"oldest_fired_at={summary['oldest_fired_at']}"
            )
            busy = (
                await self._agent_busy_for_drain(agent_name)
                if drain_recheck
                else self._agent_busy_not_wedged(agent_name)
            )
            if busy:
                drain_expired = False
                if drain_recheck:
                    drain_expired = self._record_outbox_drain_extension(
                        agent_name, summary=summary, now=replay_now
                    )
                if drain_expired:
                    _log(
                        "scheduler: persisted wake replay stopped for "
                        f"'{agent_name}': drain-extension budget terminalized "
                        "the active rows"
                    )
                else:
                    _log(
                        f"scheduler: persisted wake replay deferred for "
                        f"'{agent_name}' until the next turn-idle boundary"
                    )
                return
            if drain_recheck:
                self._outbox_drain_extensions.pop(agent_name, None)
        all_pending_wakes = self._registry.list_pending_schedule_wakes(
            agent_name, include_parked=True
        )
        abandoned_schedule_ids = set()
        for pending in all_pending_wakes:
            if (
                pending.abandoned_at > 0
                and pending.last_error.startswith("RECEIPT_ABANDONED")
                and self._wake_prompt_inflight(
                    pending,
                    prompt=self._wake_prompt_with_recurring_stale_drops(
                        pending
                    )[0],
                )
            ):
                # A persisted abandonment blocks only while the old physical
                # prompt can still execute. After a restart or failed receipt,
                # a no-longer-inflight tombstone must not starve newer work.
                abandoned_schedule_ids.add(pending.schedule_id)
        pending_wakes = []
        fresh_receipt_budget_ids: set[int] = set()
        for pending in all_pending_wakes:
            current_schedule = self._registry.get_schedule(pending.schedule_id)
            zombie_reason = ""

            # Discard wakes older than 24 hours to prevent stale phantom fires
            age_seconds = time.time() - pending.fired_at
            if age_seconds > (24 * 3600):
                zombie_reason = f"persisted wake stale ({age_seconds / 3600:.1f}h old)"
            elif current_schedule is None:
                zombie_reason = "schedule deleted"
            elif current_schedule.agent_name != pending.agent_name:
                zombie_reason = "schedule reassigned to another agent"
            elif not current_schedule.enabled and not current_schedule.one_shot:
                zombie_reason = "schedule disabled"
            if zombie_reason:
                if pending.parked_at != 0 or pending.abandoned_at != 0:
                    continue
                quarantined = self._registry.park_pending_schedule_wake(
                    pending.id,
                    reason=f"terminal replay policy: {zombie_reason}",
                )
                if quarantined:
                    log_tag = (
                        "PERSISTED_WAKE_STALE_DROPPED"
                        if "stale" in zombie_reason
                        else "PERSISTED_WAKE_ZOMBIE_QUARANTINED"
                    )
                    _log(
                        f"scheduler: {log_tag} pending "
                        f"#{pending.id}, schedule #{pending.schedule_id} for "
                        f"agent '{pending.agent_name}': {zombie_reason}; "
                        "quarantined=True"
                    )
                else:
                    # Ripristinato da b28c7367 (#1020), scartato in tre merge
                    # successivi: un park che non cambia stato deve avere un
                    # marcatore proprio, altrimenti è indistinguibile da una
                    # quarantena vera nei log e negli alert per stringa.
                    _log(
                        f"scheduler: PERSISTED_WAKE_ZOMBIE_PARK_NOOP pending "
                        f"#{pending.id}, schedule #{pending.schedule_id} for "
                        f"agent '{pending.agent_name}': {zombie_reason}; "
                        "park returned no state change"
                    )
                continue
            if pending.parked_at != 0:
                # `include_parked=True` fa entrare anche le righe terminali.
                # Una riga parcheggiata il cui schedule è ancora vivo non
                # produce `zombie_reason`, quindi senza questa guardia arriva
                # in `pending_wakes`, diventa `newest_recurring` e collassa la
                # riga viva più vecchia — che poi non viene consegnata perché
                # l'UPDATE degli attempts filtra su parked_at=0. Il turno
                # sparisce del tutto.
                continue
            if pending.schedule_id in abandoned_schedule_ids:
                _log(
                    f"scheduler: persisted wake #{pending.id}, schedule "
                    f"#{pending.schedule_id} for agent '{pending.agent_name}' "
                    "remains pending behind an older abandoned pasted fire"
                )
                continue
            replay_max_age = self._pending_wake_replay_max_age(
                current_schedule, pending.fired_at
            )
            row_age = max(0.0, replay_now - pending.fired_at)
            delivery_prompt, stale_drop_notices = (
                self._wake_prompt_with_recurring_stale_drops(pending)
            )
            # Invariant: abandonment precedes both stale deletion and
            # recurrence collapse. Once a prompt is physically pasted it
            # cannot be recalled, so replay must abandon that exact fire
            # in place and retain its existing durable acceptance authority.
            # Calling _wait_for_wake_confirmation here would paste a duplicate.
            past_receipt_ceiling = (
                row_age >= self._receipt_extension_max_age_sec
            )
            inflight_past_receipt_ceiling = (
                past_receipt_ceiling
                and self._wake_prompt_inflight(pending, prompt=delivery_prompt)
            )
            if inflight_past_receipt_ceiling:
                retained = self._abandon_inflight_replay(
                    pending,
                    prompt=delivery_prompt,
                    age=row_age,
                    stale_drop_notices=stale_drop_notices,
                )
                if retained:
                    abandoned_schedule_ids.add(pending.schedule_id)
                _log(
                    f"scheduler: pasted pending wake #{pending.id} crossed "
                    "its receipt ceiling; receipt abandonment takes "
                    "precedence over stale deletion and recurrence collapse"
                )
                continue
            if row_age > replay_max_age:
                discarded = self._registry.delete_pending_schedule_wake(
                    pending.id
                )
                _log(
                    f"scheduler: PERSISTED_WAKE_STALE_DROPPED pending "
                    f"#{pending.id}, schedule '{pending.schedule_name}' "
                    f"(#{pending.schedule_id}) for agent "
                    f"'{pending.agent_name}': age_s={row_age:.1f} "
                    f"max_age_s={replay_max_age:.1f} discarded={discarded}"
                )
                if discarded and current_schedule.one_shot:
                    self._alert_stale_one_shot_drop(
                        agent_name=pending.agent_name,
                        schedule_id=pending.schedule_id,
                        schedule_name=pending.schedule_name,
                        pending_id=pending.id,
                        row_age=row_age,
                        replay_max_age=replay_max_age,
                    )
                elif discarded:
                    self._record_recurring_stale_drop(
                        agent_name=pending.agent_name,
                        schedule_id=pending.schedule_id,
                        schedule_name=pending.schedule_name,
                        dropped_at=replay_now,
                        row_age=row_age,
                    )
                continue
            if past_receipt_ceiling:
                # The inflight probe above proved this old row was never
                # pasted. It remains owed work inside its replay window.
                fresh_receipt_budget_ids.add(pending.id)
            pending_wakes.append(pending)

        # A recurring schedule describes current work, not a FIFO event log.
        # When several occurrences accumulated during one busy turn, keep the
        # newest relevant fire and quarantine older recurrences as explicit
        # ledger evidence. One-shots remain independent owed work.
        newest_recurring: dict[int, PendingScheduleWake] = {}
        for pending in pending_wakes:
            current_schedule = self._registry.get_schedule(
                pending.schedule_id
            )
            if current_schedule is not None and not current_schedule.one_shot:
                newest_recurring[pending.schedule_id] = pending
        collapsed_ids: set[int] = set()
        for pending in pending_wakes:
            newest = newest_recurring.get(pending.schedule_id)
            if newest is None or newest.id == pending.id:
                continue
            if self._registry.collapse_pending_schedule_wake(
                pending.id,
                superseded_by_fired_at=newest.fired_at,
                collapsed_at=replay_now,
            ):
                collapsed_ids.add(pending.id)
                _log(
                    f"scheduler: RECURRENCE_COLLAPSED pending #{pending.id}, "
                    f"schedule '{pending.schedule_name}' "
                    f"(#{pending.schedule_id}) for agent "
                    f"'{pending.agent_name}': fired_at={pending.fired_at} "
                    f"superseded_by_fired_at={newest.fired_at}"
                )
                if self._activity:
                    try:
                        self._activity.log(
                            pending.agent_name,
                            "schedule_recurrence_collapsed",
                            f"Schedule '{pending.schedule_name}' recurrence "
                            "collapsed into its newest pending fire",
                        )
                    except Exception:
                        pass
        if collapsed_ids:
            pending_wakes = [
                pending
                for pending in pending_wakes
                if pending.id not in collapsed_ids
            ]

        for pending in pending_wakes:
            if pending.attempts >= self.PERSISTED_WAKE_ATTEMPT_CAP:
                self._park_pending_wake_if_capped(pending, pending.attempts)
                break
            # Skip if this wake was already delivered earlier in this startup session.
            # Prevents duplicate delivery when daemon restarts before receipt persists.
            dedup_key = (pending.schedule_id, pending.fired_at)
            if dedup_key in self._replayed_wakes_this_startup:
                _log(
                    f"scheduler: skipping already-delivered persisted wake "
                    f"#{pending.id} for schedule #{pending.schedule_id} on "
                    f"agent '{pending.agent_name}' (dedup key {dedup_key})"
                )
                continue
            attempts = self._registry.increment_pending_schedule_wake_attempts(
                pending.id
            )
            if attempts is None:
                continue
            try:
                confirmed = await self._wait_for_wake_confirmation(
                    pending,
                    # Any over-ceiling row that reached this point is not
                    # currently pasted and survived the replay-age policy.
                    # Its first legitimate transport acceptance is still owed.
                    fresh_receipt_budget=(
                        pending.id in fresh_receipt_budget_ids
                    ),
                )
            except asyncio.CancelledError:
                raise
            except _ReceiptAbandonedError:
                break
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
                    f"'{agent_name}' was confirmed but receipt persistence "
                    "did not match a row"
                )
                break
            # Mark as replayed to prevent duplicate delivery if daemon restarts
            # before the next replay attempt reads the confirmed row.
            self._replayed_wakes_this_startup.add(
                (pending.schedule_id, pending.fired_at)
            )
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

    def _pending_wake_replay_max_age(self, schedule, fired_at: float) -> float:
        """Return ``min(next fire interval, configured replay ceiling)``.

        We only search through the configured ceiling. If a sparse schedule
        has no next fire inside that window, the ceiling wins. This bounds the
        work to at most 60 minute checks under the production one-hour policy.
        """
        ceiling = self._pending_wake_max_age_sec
        try:
            tz = ZoneInfo(schedule.timezone)
        except (KeyError, ValueError):
            tz = ZoneInfo("America/Los_Angeles")

        fired_minute = datetime.fromtimestamp(fired_at, tz=tz).replace(
            second=0, microsecond=0
        )
        candidate = fired_minute + timedelta(minutes=1)
        horizon = fired_minute + timedelta(seconds=ceiling)
        while candidate <= horizon:
            if cron_matches(schedule.cron, candidate):
                return min(
                    ceiling,
                    max(60.0, candidate.timestamp() - fired_minute.timestamp()),
                )
            candidate += timedelta(minutes=1)
        return ceiling

    @staticmethod
    def _wake_prompt(schedule) -> str:
        """The exact prompt text a schedule's wake delivers to the transport."""
        return schedule.prompt or f"Scheduled wake: {schedule.name}"

    @staticmethod
    def _stale_drop_timestamp(timestamp: float) -> str:
        """Render an unambiguous compact UTC timestamp for the agent note."""
        return datetime.fromtimestamp(timestamp, tz=ZoneInfo("UTC")).strftime(
            "%Y-%m-%d %H:%M:%SZ"
        )

    def _wake_prompt_with_recurring_stale_drops(
        self, schedule
    ) -> tuple[str, list[RecurringScheduleStaleDrop]]:
        """Prepend bounded unsurfaced drop aggregates to one exact wake prompt."""
        prompt = self._wake_prompt(schedule)
        try:
            notices = self._registry.list_recurring_schedule_stale_drops(
                schedule.agent_name
            )
        except Exception as exc:
            # Operational-awareness storage must not block real scheduled work.
            _log(
                "scheduler: recurring stale-drop surfacing read failed for "
                f"agent '{schedule.agent_name}': {type(exc).__name__}: {exc}"
            )
            return prompt, []
        if not notices:
            return prompt, []

        lines = []
        for notice in notices:
            schedule_name = " ".join(notice.schedule_name.split()) or "unnamed"
            if len(schedule_name) > 120:
                schedule_name = f"{schedule_name[:117]}..."
            fire_word = "fire" if notice.drop_count == 1 else "fires"
            drop_verb = "was" if notice.drop_count == 1 else "were"
            work_subject = "that fire" if notice.drop_count == 1 else "those fires"
            lines.append(
                f"Note: {notice.drop_count} {fire_word} of recurring schedule "
                f"'{schedule_name}' (#{notice.schedule_id}) {drop_verb} dropped as stale "
                f"between {self._stale_drop_timestamp(notice.first_dropped_at)} "
                f"and {self._stale_drop_timestamp(notice.last_dropped_at)} "
                "(session busy past the replay window). The work "
                f"{work_subject} would have done was NOT performed."
            )
        notes_text = "\n".join(lines)
        return f"{notes_text}\n\n{prompt}", notices

    def _acknowledge_recurring_stale_drops(
        self, agent_name: str, notices: list[RecurringScheduleStaleDrop]
    ) -> None:
        """Best-effort clear only snapshots included in a confirmed wake."""
        try:
            cleared = self._registry.acknowledge_recurring_schedule_stale_drops(
                agent_name, notices
            )
        except Exception as exc:
            # The wake already has a positive receipt. Never convert it to an
            # undelivered/replayable wake because notice cleanup failed.
            _log(
                "scheduler: recurring stale-drop surfacing acknowledge failed "
                f"for agent '{agent_name}': {type(exc).__name__}: {exc}"
            )
            return
        _log(
            "scheduler: recurring stale-drop notice surfaced for agent "
            f"'{agent_name}': snapshots={len(notices)} cleared={cleared}"
        )

    def _wake_prompt_inflight(self, schedule, *, prompt: str | None = None) -> bool:
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
                    schedule.agent_name,
                    self._wake_prompt(schedule) if prompt is None else prompt,
                )
                is True
            )
        except Exception as exc:
            _log(
                f"scheduler: wake-inflight check failed for "
                f"'{schedule.agent_name}': {type(exc).__name__}: {exc}"
            )
            return False

    def _wake_prompt_queued(self, schedule, *, prompt: str | None = None) -> bool:
        """True when this wake is queued-unpasted and safely recallable."""
        if self._delivery_queued_fn is None:
            return False
        try:
            return (
                self._delivery_queued_fn(
                    schedule.agent_name,
                    self._wake_prompt(schedule) if prompt is None else prompt,
                )
                is True
            )
        except Exception as exc:
            _log(
                f"scheduler: wake-queued check failed for "
                f"'{schedule.agent_name}': {type(exc).__name__}: {exc}"
            )
            return False

    async def _cancel_queued_wake(
        self, schedule, *, prompt: str | None = None
    ) -> bool:
        """Recall one exact queued wake, returning only positive evidence."""
        if self._delivery_cancel_queued_fn is None:
            return False
        try:
            cancelled = self._delivery_cancel_queued_fn(
                schedule.agent_name,
                self._wake_prompt(schedule) if prompt is None else prompt,
            )
            if inspect.isawaitable(cancelled):
                cancelled = await cancelled
            return cancelled is True
        except Exception as exc:
            _log(
                f"scheduler: queued-wake cancel failed for "
                f"'{schedule.agent_name}': {type(exc).__name__}: {exc}"
            )
            return False

    @staticmethod
    def _delivery_result_if_done(
        delivery: asyncio.Task,
    ) -> tuple[bool, bool]:
        """Return ``(settled, confirmed)`` without consuming cancellation."""
        if not delivery.done() or delivery.cancelled():
            return False, False
        return True, delivery.result()

    def _abandon_receipt_wait(
        self,
        schedule,
        delivery: asyncio.Task,
        *,
        age: float,
        bound_reason: str,
        wait_attempts: int,
        stale_drop_notices: list[RecurringScheduleStaleDrop],
    ) -> None:
        """Release the delivery lock while retaining late-receipt authority."""
        schedule_id, fired_at, _ = self._mark_abandoned_receipt(
            schedule,
            age=age,
            bound_reason=bound_reason,
            wait_attempts=wait_attempts,
        )

        async def _observe_late_receipt() -> None:
            try:
                confirmed = await delivery
                if not confirmed:
                    return
                self._registry.confirm_pending_schedule_wake_by_fire(
                    schedule_id,
                    fired_at,
                    delivered_at=time.time(),
                )
                if stale_drop_notices:
                    self._acknowledge_recurring_stale_drops(
                        schedule.agent_name, stale_drop_notices
                    )
                self._log_late_abandoned_receipt(
                    schedule, schedule_id=schedule_id, fired_at=fired_at
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                _log(
                    f"scheduler: abandoned receipt observer failed for "
                    f"schedule '{schedule.name}' (#{schedule_id}) for agent "
                    f"'{schedule.agent_name}': {type(exc).__name__}: {exc}"
                )

        self._track_detached_receipt_task(_observe_late_receipt())

    def _mark_abandoned_receipt(
        self,
        schedule,
        *,
        age: float,
        bound_reason: str = "wall-clock cap",
        wait_attempts: int | None = None,
    ) -> tuple[int, float, bool]:
        """Move one budget-expired unconfirmed fire to explicit abandonment."""
        attempt_detail = (
            f", wait_attempts={wait_attempts}"
            if wait_attempts is not None
            else ""
        )
        reason = (
            "RECEIPT_ABANDONED: receipt-extension budget expired "
            f"by {bound_reason} (age={age:.1f}s, "
            f"max_age={self._receipt_extension_max_age_sec:.1f}s"
            f"{attempt_detail})"
        )
        schedule_id = self._schedule_id(schedule)
        fired_at = self._schedule_fired_at(schedule)
        row = self._registry.get_schedule_wake_by_fire(
            schedule_id, fired_at
        )
        abandoned = bool(
            row is not None
            and self._registry.abandon_pending_schedule_wake(
                row.id,
                abandoned_at=time.time(),
                reason=reason,
            )
        )
        _log(
            f"scheduler: RECEIPT_ABANDONED schedule '{schedule.name}' "
            f"(#{schedule_id}) for agent '{schedule.agent_name}' "
            f"fired_at={fired_at} age_s={age:.1f} "
            f"ceiling_s={self._receipt_extension_max_age_sec:.1f} "
            f"bound={bound_reason!r} wait_attempts={wait_attempts} "
            f"abandoned={abandoned}"
        )
        self._alert_receipt_extension_expired(
            schedule,
            age=age,
            bound_reason=bound_reason,
            wait_attempts=wait_attempts,
        )
        if self._activity:
            try:
                self._activity.log(
                    schedule.agent_name,
                    "schedule_receipt_abandoned",
                    f"Schedule '{schedule.name}' receipt wait exceeded its "
                    f"{bound_reason}",
                )
            except Exception:
                pass
        return schedule_id, fired_at, abandoned

    def _alert_receipt_extension_expired(
        self,
        schedule,
        *,
        age: float,
        bound_reason: str,
        wait_attempts: int | None,
    ) -> None:
        """Emit one durable-fire alert after its generous wait budget expires."""
        schedule_id = self._schedule_id(schedule)
        fired_at = self._schedule_fired_at(schedule)
        alert_key = (schedule_id, fired_at)
        if alert_key in self._receipt_extension_alerted:
            _log(
                "scheduler: RECEIPT_EXTENSION_EXPIRED owner already alerted "
                f"for schedule '{schedule.name}' (#{schedule_id}) on agent "
                f"'{schedule.agent_name}' fired_at={fired_at}"
            )
            return
        self._receipt_extension_alerted.add(alert_key)
        attempts = (
            str(wait_attempts) if wait_attempts is not None else "persisted replay"
        )
        _log(
            f"scheduler: RECEIPT_EXTENSION_EXPIRED schedule "
            f"'{schedule.name}' (#{schedule_id}) for agent "
            f"'{schedule.agent_name}' fired_at={fired_at} age_s={age:.1f} "
            f"bound={bound_reason!r} wait_attempts={attempts}; owner alert queued"
        )
        self._queue_owner_alert(
            schedule.agent_name,
            (
                "🚨 RECEIPT EXTENSION EXPIRED: schedule "
                f"'{schedule.name}' (#{schedule_id}) on agent "
                f"'{schedule.agent_name}' exhausted its {bound_reason} after "
                f"{age:.1f}s and {attempts} confirmation wait(s). The exact "
                "fire was moved to explicit abandonment to prevent replay; "
                "an already-pasted delivery retains one late-receipt authority. "
                "Inspect the transport and wake ledger before intervening."
            ),
        )

    def _abandon_inflight_replay(
        self,
        schedule: PendingScheduleWake,
        *,
        prompt: str,
        age: float,
        stale_drop_notices: list[RecurringScheduleStaleDrop],
    ) -> bool:
        """Abandon a pasted replay in place, never invoking the wake callback.

        The physical turn already owns the durable ``ScheduleWakeReceipt``
        callback.  Polling only observes that existing authority; it never
        creates another transport turn.
        """
        schedule_id, fired_at, abandoned = self._mark_abandoned_receipt(
            schedule, age=age
        )
        row = self._registry.get_schedule_wake_by_fire(schedule_id, fired_at)
        retained = bool(
            abandoned
            or (
                row is not None
                and row.accepted_at == 0
                and row.abandoned_at > 0
                and row.last_error.startswith("RECEIPT_ABANDONED")
            )
        )
        if not retained:
            return False

        async def _observe_existing_receipt() -> None:
            try:
                while True:
                    row = self._registry.get_schedule_wake_by_fire(
                        schedule_id, fired_at
                    )
                    if row is None:
                        return
                    if row.accepted_at > 0:
                        if stale_drop_notices:
                            self._acknowledge_recurring_stale_drops(
                                schedule.agent_name, stale_drop_notices
                            )
                        self._log_late_abandoned_receipt(
                            schedule,
                            schedule_id=schedule_id,
                            fired_at=fired_at,
                        )
                        return
                    if not self._wake_prompt_inflight(
                        schedule, prompt=prompt
                    ):
                        # Durable acceptance precedes the transport receipt
                        # resolving. Re-read once across that ordering edge.
                        row = self._registry.get_schedule_wake_by_fire(
                            schedule_id, fired_at
                        )
                        if row is not None and row.accepted_at > 0:
                            if stale_drop_notices:
                                self._acknowledge_recurring_stale_drops(
                                    schedule.agent_name, stale_drop_notices
                                )
                            self._log_late_abandoned_receipt(
                                schedule,
                                schedule_id=schedule_id,
                                fired_at=fired_at,
                            )
                        return
                    await asyncio.sleep(
                        _ABANDONED_RECEIPT_OBSERVER_INTERVAL_SEC
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                _log(
                    f"scheduler: abandoned receipt observer failed for "
                    f"schedule '{schedule.name}' (#{schedule_id}) for agent "
                    f"'{schedule.agent_name}': {type(exc).__name__}: {exc}"
                )

        self._track_detached_receipt_task(_observe_existing_receipt())
        return True

    def _track_detached_receipt_task(self, observer) -> None:
        """Run one late-receipt observer outside the per-agent delivery lock."""
        task = asyncio.create_task(observer)
        self._detached_receipt_tasks.add(task)
        task.add_done_callback(self._detached_receipt_tasks.discard)

    @staticmethod
    def _log_late_abandoned_receipt(
        schedule, *, schedule_id: int, fired_at: float
    ) -> None:
        _log(
            f"scheduler: late positive receipt retired abandoned "
            f"schedule '{schedule.name}' (#{schedule_id}) for agent "
            f"'{schedule.agent_name}' fired_at={fired_at}"
        )

    @staticmethod
    def _schedule_id(schedule) -> int:
        """Return the schedule identity, never a replay outbox-row identity."""
        if hasattr(schedule, "schedule_id"):
            return int(schedule.schedule_id)
        return int(schedule.id)

    @staticmethod
    def _schedule_fired_at(schedule) -> float:
        """Return the immutable exact-fire timestamp for either row shape."""
        if hasattr(schedule, "fired_at"):
            return float(schedule.fired_at)
        return float(schedule.last_run)

    def _receipt_age(self, schedule) -> float:
        """Age one durable fire; direct legacy callers with no stamp start now."""
        fired_at = self._schedule_fired_at(schedule)
        if fired_at <= 0:
            return 0.0
        return max(0.0, time.time() - fired_at)

    async def _wake_and_confirm(
        self,
        schedule,
        *,
        prompt: str | None = None,
        attempt_started: asyncio.Event | None = None,
    ) -> bool:
        """Invoke the wake callback and await its exact per-prompt receipt."""
        if attempt_started is not None:
            attempt_started.set()
        main_session_id = f"{schedule.agent_name}-main"
        fired_at = (
            schedule.fired_at
            if hasattr(schedule, "fired_at")
            else schedule.last_run
        )
        receipt = ScheduleWakeReceipt(
            self._registry, self._schedule_id(schedule), fired_at
        )
        callback_kwargs = {}
        try:
            signature = inspect.signature(self._wake_callback)
            if "schedule_receipt" in signature.parameters:
                callback_kwargs["schedule_receipt"] = receipt
        except (TypeError, ValueError):
            # Opaque callables keep the historical three-argument contract.
            pass
        result = await self._wake_callback(
            schedule.agent_name,
            main_session_id,
            self._wake_prompt(schedule) if prompt is None else prompt,
            **callback_kwargs,
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

        Throttled per pending wake to prevent cycling: if a wake was replayed
        within the last 10 minutes, skip replay to avoid creating multiple retry
        cycles on each heartbeat (see #escalation: schedule #3 repeating 4x in 4h).
        """
        existing = self._pending_replay_tasks.get(agent_name)
        if existing is not None and not existing.done():
            return
        try:
            pendings = self._registry.list_pending_schedule_wakes(agent_name)
            if not pendings:
                return
        except Exception as exc:
            _log(
                f"scheduler: outbox drain check failed for '{agent_name}': "
                f"{type(exc).__name__}: {exc}"
            )
            return

        # Throttle check: prevent replaying the same pending wake within 10 minutes.
        # If the oldest pending wake was recently replayed, skip this drain cycle.
        _PENDING_WAKE_REPLAY_THROTTLE_SECONDS = 600  # 10 minutes
        now = time.time()
        oldest_wake = pendings[0]
        last_attempt = self._pending_wake_replay_throttle.get(oldest_wake.id)
        if last_attempt is not None and (now - last_attempt) < _PENDING_WAKE_REPLAY_THROTTLE_SECONDS:
            return

        # Update throttle for this wake and all others being replayed
        for wake in pendings:
            self._pending_wake_replay_throttle[wake.id] = now

        _log(
            f"scheduler: proven-live agent '{agent_name}' has a stranded wake "
            "outbox; triggering durable replay"
        )
        self.replay_pending_for_agent(agent_name)

    def _check_pending_wake_liveness(self, now: float) -> None:
        """Periodically drain live-daemon outboxes without heartbeat opt-in.

        Turn-idle remains the primary path.  This once-per-minute scan is the
        bounded fallback for a lost idle callback. Each replay rechecks the
        transport's current pane state under the per-agent delivery lock; it
        does not trust the watchdog-wide liveness verdict sampled by a receipt
        waiter. Busy rechecks have their own attempt and wall-clock budget.
        """
        last_drain = self._last_pending_wake_liveness_drain_at
        if last_drain is None:
            self._last_pending_wake_liveness_drain_at = now
            return
        if now - last_drain < _PENDING_WAKE_LIVENESS_DRAIN_INTERVAL_SEC:
            return
        self._last_pending_wake_liveness_drain_at = now
        try:
            agent_names = {
                pending.agent_name
                for pending in self._registry.list_pending_schedule_wakes()
            }
        except Exception as exc:
            _log(
                "scheduler: periodic outbox drain scan failed: "
                f"{type(exc).__name__}: {exc}"
            )
            return
        for settled_agent in self._outbox_drain_extensions.keys() - agent_names:
            self._outbox_drain_extensions.pop(settled_agent, None)
        for agent_name in sorted(agent_names):
            _log(
                f"scheduler: periodic liveness drain found pending wakes "
                f"for '{agent_name}'; scheduling fresh transport recheck"
            )
            self.replay_pending_for_agent(agent_name, drain_recheck=True)

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
