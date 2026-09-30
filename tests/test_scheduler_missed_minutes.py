"""Recover missed cron minutes without replaying historical work."""

import asyncio
import copy
import sqlite3
import time
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from pinky_daemon import agent_registry as registry_module
from pinky_daemon import scheduler as module
from pinky_daemon.agent_registry import AgentRegistry


def stamp(value):
    return datetime.fromisoformat(value).timestamp()


class Clock:
    wall = stamp("2026-09-30T03:59:30+00:00")
    mono = 100.0

    def set(self, value):
        self.wall = stamp(value)

    def advance(self, seconds):
        self.wall += seconds
        self.mono += seconds


@pytest.fixture
def env(tmp_path, monkeypatch):
    clock = Clock()
    clock_module = SimpleNamespace(**{
        **vars(time), "time": lambda: clock.wall, "monotonic": lambda: clock.mono,
    })
    monkeypatch.setattr(module, "time", clock_module)
    monkeypatch.setattr(registry_module, "time", clock_module)
    registry = AgentRegistry(str(tmp_path / "registry.db"))
    registry.register("worker", working_dir=str(tmp_path / "worker"))
    fired = []

    async def wake(name, session_id, prompt):
        fired.append(prompt)
        return True

    schedulers = []

    def scheduler():
        obj = module.AgentScheduler(registry, wake_callback=wake, tick_interval=10)
        schedulers.append(obj)
        return obj

    yield SimpleNamespace(clock=clock, registry=registry, fired=fired, scheduler=scheduler)
    for obj in schedulers:
        for task in obj._schedule_delivery_tasks:
            task.cancel()
    registry.close()


def add(env, cron, name, **kwargs):
    return env.registry.add_schedule(
        "worker", cron, name=name, prompt=name, timezone=kwargs.pop("timezone", "UTC"),
        **kwargs,
    )


async def check(scheduler, clock):
    await scheduler._check_schedules(clock.wall)
    await asyncio.gather(*tuple(scheduler._schedule_delivery_tasks))


def trace_rows(env):
    assert env.registry._fire_trace.flush(timeout=3), "trace did not drain"
    with sqlite3.connect(env.registry._fire_trace.path) as db:
        db.row_factory = sqlite3.Row
        return [dict(row) for row in db.execute("SELECT * FROM schedule_fire_trace")]


async def test_missed_slots_coalesce_to_latest_and_record_lateness(env, capsys):
    hourly = add(env, "0 * * * *", "hourly")
    half = add(env, "30 4 * * *", "half")
    scheduler = env.scheduler()
    await check(scheduler, env.clock)
    env.clock.set("2026-09-30T05:01:00+00:00")
    await check(scheduler, env.clock)
    assert env.fired == ["hourly", "half"], "missed cron slots were dropped"
    rows = {row["schedule_id"]: row for row in trace_rows(env)}
    for schedule, minute, lateness in (
        (hourly, "2026-09-30T05:00:00+00:00", 60),
        (half, "2026-09-30T04:30:00+00:00", 1860),
    ):
        row = rows[schedule.id]
        assert row["scheduled_minute"] == minute, "latest missed slot was not traced"
        assert row["late_fire"] == 1 and row["lateness_s"] == lateness
        assert row["fired_at"] == env.clock.wall, "claim identity was backdated"
    logs = capsys.readouterr().err
    assert f"LATE_FIRE schedule={hourly.id} scheduled_minute=2026-09-30T05:00:00+00:00 lateness_s=60" in logs
    assert f"LATE_FIRE schedule={half.id} scheduled_minute=2026-09-30T04:30:00+00:00 lateness_s=1860" in logs
    await check(scheduler, env.clock)
    assert env.fired == ["hourly", "half"], "same-window catch-up fired twice"


async def test_restart_skips_history_but_recovers_future_missed_slot(env):
    row = add(env, "30 4 * * *", "daily")
    env.registry.update_schedule_last_run(row.id, env.clock.wall - 86400)
    env.clock.set("2026-09-30T05:01:00+00:00")
    scheduler = env.scheduler()
    await check(scheduler, env.clock)
    assert env.fired == [], "restart replayed a pre-start slot"
    env.clock.set("2026-10-01T04:29:30+00:00")
    await check(scheduler, env.clock)
    env.clock.set("2026-10-01T04:31:00+00:00")
    await check(scheduler, env.clock)
    assert env.fired == ["daily"], "live missed slot after restart was dropped"


async def test_missed_one_shot_fires_once_and_disables(env):
    row = add(env, "0 * * * *", "once", one_shot=True)
    scheduler = env.scheduler()
    env.clock.set("2026-09-30T05:01:00+00:00")
    await check(scheduler, env.clock)
    assert env.fired == ["once"], "missed one-shot was dropped"
    assert env.registry.get_schedule(row.id).enabled is False, "one-shot stayed enabled"
    env.clock.set("2026-09-30T06:01:00+00:00")
    await check(scheduler, env.clock)
    assert env.fired == ["once"], "one-shot fired more than once"


async def test_catchup_bound_truncates_old_slots_without_flood(env, capsys):
    env.clock.set("2026-09-30T00:00:30+00:00")
    add(env, "30 1 * * *", "too-old")
    add(env, "0 * * * *", "hourly")
    scheduler = env.scheduler()
    env.clock.set("2026-09-30T12:01:00+00:00")
    await check(scheduler, env.clock)
    assert "SCHEDULER_CATCHUP_TRUNCATED" in capsys.readouterr().err, "catch-up truncation was silent"
    assert env.fired == ["hourly"], "catch-up exceeded six hours or flooded occurrences"
    assert len(trace_rows(env)) == 1


async def test_competing_missed_slot_claim_has_one_winner(env, monkeypatch, capsys):
    add(env, "30 4 * * *", "race")
    first, second = env.scheduler(), env.scheduler()
    snapshots = env.registry.get_all_schedules(enabled_only=True)
    monkeypatch.setattr(env.registry, "get_all_schedules", lambda **kw: copy.deepcopy(snapshots))
    env.clock.set("2026-09-30T05:01:00+00:00")
    await asyncio.gather(check(first, env.clock), check(second, env.clock))
    assert env.fired == ["race"], "missed-slot claim did not have exactly one winner"
    assert "lost last_run claim race" in capsys.readouterr().err, "lost claim race was silent"
    assert len(trace_rows(env)) == 1, "claim race minted duplicate fire identities"


async def test_fall_back_coalesces_both_local_occurrences(env):
    env.clock.set("2026-11-01T07:59:30+00:00")
    add(env, "30 1 * * *", "fold", timezone="America/Los_Angeles")
    scheduler = env.scheduler()
    env.clock.set("2026-11-01T09:59:00+00:00")
    await check(scheduler, env.clock)
    assert env.fired == ["fold"], "schedule timezone fold was missed or fired twice"
    row, = trace_rows(env)
    assert row["scheduled_minute"] == "2026-11-01T01:30:00-08:00", "latest fold instant was not selected"


async def test_spring_forward_does_not_invent_nonexistent_local_slot(env):
    env.clock.set("2026-03-08T09:59:30+00:00")
    add(env, "30 2 * * *", "missing", timezone="America/Los_Angeles")
    add(env, "30 3 * * *", "valid", timezone="America/Los_Angeles")
    scheduler = env.scheduler()
    env.clock.set("2026-03-08T11:01:00+00:00")
    await check(scheduler, env.clock)
    assert "missing" not in env.fired, "nonexistent spring-forward slot was invented"
    assert env.fired == ["valid"], "valid post-transition catch-up slot was dropped"


PHASES = (
    "_run_outbox_reaper_if_due", "_warn_oversized_schedule_prompts", "_check_schedules",
    "_check_clock_aligned_wakes", "_check_pending_wake_liveness", "_check_heartbeats",
    "_check_auto_sleep", "_check_idle_sessions", "_cleanup_expired_messages",
    "_check_dreams", "_check_librarian", "_check_url_watchers",
)
SYNC_PHASES = {
    "_run_outbox_reaper_if_due", "_warn_oversized_schedule_prompts",
    "_check_pending_wake_liveness", "_cleanup_expired_messages",
}


@pytest.mark.parametrize("slow_phase", PHASES)
async def test_slow_phase_is_named_and_next_tick_reports_gap(env, monkeypatch, capsys, slow_phase):
    scheduler = env.scheduler()
    seen = []

    def phase(name):
        def run(*args):
            if name == slow_phase and name not in seen:
                env.clock.advance(45)
            seen.append(name)

        async def async_run(*args):
            run(*args)

        return run if name in SYNC_PHASES else async_run

    for name in PHASES:
        monkeypatch.setattr(scheduler, name, phase(name))
    sleeps = 0

    async def sleep(delay):
        nonlocal sleeps
        sleeps += 1
        env.clock.advance(delay)
        if sleeps == 2:
            scheduler._running = False

    monkeypatch.setattr(module, "asyncio", SimpleNamespace(**{**vars(asyncio), "sleep": sleep}))
    scheduler._running = True
    await scheduler._loop()
    assert seen == list(PHASES) * 2, "phase order or inline execution changed"
    logs = capsys.readouterr().err
    assert f"SLOW_TICK_PHASE phase={slow_phase} elapsed_s=45" in logs, "slow phase was not named"
    assert "TICK_GAP gap_s=55 last_phase=_check_url_watchers" in logs, "tick gap was silent"
    assert scheduler.last_tick_started_monotonic == 155
    assert scheduler.consecutive_tick_errors == 0


async def test_gap_uses_existing_monotonic_start_even_when_wall_clock_rewinds(env, monkeypatch, capsys):
    scheduler = env.scheduler()
    scheduler.last_tick_started_monotonic = 50
    env.clock.mono = 150
    env.clock.wall -= 86400

    async def tick():
        scheduler._running = False

    scheduler._tick = tick
    monkeypatch.setattr(module, "asyncio", SimpleNamespace(**{**vars(asyncio), "sleep": AsyncMock()}))
    scheduler._running = True
    await scheduler._loop()
    assert "TICK_GAP gap_s=100 last_phase=" in capsys.readouterr().err, "existing monotonic progress was not used"


async def test_current_minute_startup_and_next_minute_keep_existing_behavior(env, capsys):
    add(env, "* * * * *", "current")
    scheduler = env.scheduler()
    await check(scheduler, env.clock)
    await check(scheduler, env.clock)
    assert env.fired == ["current"]
    assert "LATE_FIRE" not in capsys.readouterr().err
    env.clock.advance(30)
    await check(scheduler, env.clock)
    assert env.fired == ["current", "current"]
    assert all(row["late_fire"] == 0 for row in trace_rows(env))


async def test_empty_pass_and_new_schedule_do_not_replay_old_slots(env):
    scheduler = env.scheduler()
    env.clock.set("2026-09-30T04:31:00+00:00")
    await check(scheduler, env.clock)
    add(env, "30 4 * * *", "new")
    env.clock.set("2026-09-30T05:01:00+00:00")
    await check(scheduler, env.clock)
    assert env.fired == [], "new schedule replayed a pre-creation slot"
    assert scheduler._last_cron_evaluated_at == env.clock.wall


async def test_failed_evaluation_retries_unclaimed_missed_slots(env, monkeypatch):
    add(env, "30 4 * * *", "retry")
    scheduler = env.scheduler()
    env.clock.set("2026-09-30T05:01:00+00:00")
    with monkeypatch.context() as patch:
        patch.setattr(env.registry, "get_all_schedules", lambda **kw: (_ for _ in ()).throw(OSError("unavailable")))
        with pytest.raises(OSError, match="unavailable"):
            await check(scheduler, env.clock)
    env.clock.advance(60)
    await check(scheduler, env.clock)
    assert env.fired == ["retry"], "failed scan consumed its catch-up window"


async def test_direct_send_catchup_keeps_actual_claim_time(env):
    row = add(env, "30 4 * * *", "direct", direct_send=True, target_channel="test")
    scheduler = env.scheduler()
    sent = AsyncMock(return_value=True)
    scheduler._direct_send_callback = sent
    env.clock.set("2026-09-30T05:01:00+00:00")
    await check(scheduler, env.clock)
    assert sent.await_count == 1, "direct-send missed slot was dropped"
    assert env.registry.get_schedule(row.id).last_run == env.clock.wall
    traced, = trace_rows(env)
    assert traced["late_fire"] == 1
    assert traced["scheduled_minute"] == "2026-09-30T04:30:00+00:00"


@pytest.mark.parametrize("duration,logged", [(30, False), (30.01, True)])
async def test_phase_timing_threshold_and_exception_propagation(env, monkeypatch, capsys, duration, logged):
    scheduler = env.scheduler()
    for name in PHASES:
        monkeypatch.setattr(scheduler, name, lambda *args: None)

    async def failure(now):
        env.clock.advance(duration)
        raise RuntimeError("phase failed")

    monkeypatch.setattr(scheduler, "_check_heartbeats", failure)
    with pytest.raises(RuntimeError, match="phase failed"):
        await scheduler._tick()
    assert ("SLOW_TICK_PHASE phase=_check_heartbeats" in capsys.readouterr().err) is logged
    assert scheduler._last_tick_phase == "_check_heartbeats"


async def test_trace_upgrade_preserves_existing_rows_and_adds_late_fields(env):
    add(env, "* * * * *", "existing")
    scheduler = env.scheduler()
    await check(scheduler, env.clock)
    original, = trace_rows(env)
    # Simulate an older trace schema before reopening the same observational store.
    writer = env.registry._fire_trace
    writer.close()
    with sqlite3.connect(writer.path) as db:
        for column in ("scheduled_minute", "late_fire", "lateness_s"):
            db.execute(f"ALTER TABLE schedule_fire_trace DROP COLUMN {column}")
    from pinky_daemon.schedule_fire_trace import ScheduleFireTrace

    env.registry._fire_trace = ScheduleFireTrace(env.registry._db_path)
    upgraded, = trace_rows(env)
    assert upgraded["fired_at"] == original["fired_at"]
    assert upgraded["scheduled_minute"] == "" and upgraded["late_fire"] == 0
