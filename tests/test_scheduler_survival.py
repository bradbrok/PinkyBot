"""Scheduler recovery and liveness during transient storage failures."""
from __future__ import annotations

import asyncio
import io
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from pinky_daemon import scheduler as module
from pinky_daemon.agent_registry import AgentRegistry
from pinky_daemon.scheduler import AgentScheduler
from pinky_daemon.session_watchdog import SessionWatchdog


class LoopExit(BaseException):
    pass


class FailingWriter(io.StringIO):
    def __init__(self, first=1, last=4, *, flush_error=None):
        super().__init__()
        self.calls = 0
        self.first = first
        self.last = last
        self.flush_error = flush_error

    def write(self, text):
        self.calls += 1
        if self.first <= self.calls <= self.last:
            raise OSError(28, "disk full")
        return super().write(text)

    def flush(self):
        if self.flush_error:
            raise self.flush_error
        return super().flush()


@pytest.fixture
def registry(tmp_path):
    reg = AgentRegistry(db_path=str(tmp_path / "agents.db"))
    reg.register("worker", runtime="claude_sdk", transport="tmux")
    yield reg
    reg.close()


async def until(predicate, timeout=3):
    async def poll():
        while not predicate():
            await asyncio.sleep(0.005)
    await asyncio.wait_for(poll(), timeout)


async def cleanup(scheduler):
    # Preserve the failure assertion even when the baseline task is dead.
    try:
        await scheduler.stop()
    except BaseException:
        pass


def cron_scheduler(registry):
    registry.add_schedule("worker", "* * * * *", name="due", prompt="run", timezone="UTC")
    fired = asyncio.Event()

    async def wake(*args):
        fired.set()
        return True

    return AgentScheduler(registry, wake_callback=wake, tick_interval=0.01), fired


@pytest.mark.asyncio
async def test_r1_disk_full_window_keeps_ticking_and_fires_cron(registry, monkeypatch):
    scheduler, fired = cron_scheduler(registry)
    ticks = 0

    async def tick():
        nonlocal ticks
        ticks += 1
        if ticks <= 4:
            raise RuntimeError("tick failed")
        await scheduler._check_schedules(1_800_000_000)

    scheduler._tick = tick
    writer = FailingWriter(first=3, last=6)
    try:
        with monkeypatch.context() as patch:
            patch.setattr(module.sys, "stderr", writer)
            await scheduler.start()
            await asyncio.wait_for(fired.wait(), 0.5)
        assert ticks >= 5
        assert scheduler.health_snapshot()["log_write_failures"] >= 4
        assert scheduler.loop_restarts == 0
    finally:
        await cleanup(scheduler)


@pytest.mark.asyncio
async def test_r2_unexpected_loop_exit_restarts_and_fires_cron(registry, monkeypatch):
    scheduler, fired = cron_scheduler(registry)
    ticks = 0
    sleeps = 0
    real_sleep = asyncio.sleep

    async def sleep(delay):
        nonlocal sleeps
        sleeps += 1
        if sleeps == 1:
            raise LoopExit("private details")
        await real_sleep(delay)

    async def tick():
        nonlocal ticks
        ticks += 1
        if ticks > 1:
            await scheduler._check_schedules(1_800_000_000)

    monkeypatch.setattr(module, "asyncio", SimpleNamespace(**{
        **vars(asyncio), "sleep": sleep,
    }))
    scheduler._tick = tick
    replay = AsyncMock()
    monkeypatch.setattr(scheduler, "_replay_pending_for_agent", replay)
    try:
        await scheduler.start()
        await asyncio.wait_for(fired.wait(), 2)
        assert scheduler.loop_restarts == 1
        assert scheduler.last_loop_exit == "LoopExit"
        replay.assert_not_awaited()
    finally:
        await cleanup(scheduler)


@pytest.mark.asyncio
@pytest.mark.parametrize("exit_first", [False, True])
async def test_r3_stop_cancels_supervision_without_restart(registry, exit_first):
    scheduler = AgentScheduler(registry, tick_interval=0.01)
    entered = 0
    real_loop = scheduler._loop

    async def loop():
        nonlocal entered
        entered += 1
        if exit_first:
            raise LoopExit()
        await real_loop()

    scheduler._loop = loop
    scheduler._tick = AsyncMock()
    try:
        await scheduler.start()
        await until(lambda: entered == 1)
        if exit_first:
            await until(lambda: getattr(scheduler, "loop_restarts", 0) == 1, timeout=0.2)
        await scheduler.stop()
        await asyncio.sleep(1.05)
        assert entered == 1
        assert scheduler._task is None
        assert scheduler.loop_restarts == int(exit_first)
        assert scheduler.health_snapshot()["running"] is False
    finally:
        await cleanup(scheduler)


@pytest.mark.asyncio
async def test_r4_health_age_uses_monotonic_clock_and_degrades(tmp_path, monkeypatch):
    from pinky_daemon import api

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(api, "SHARED_MCP_ENABLED", False)
    app = api.create_api(db_path=str(tmp_path / "conversations.db"))
    scheduler = app.state.scheduler
    clock = [100.0, 1_800_000_000.0]
    monkeypatch.setattr(module, "time", SimpleNamespace(
        monotonic=lambda: clock[0], time=lambda: clock[1],
    ))
    scheduler._tick = AsyncMock()
    client = TestClient(app)
    try:
        await scheduler.start()
        await until(lambda: scheduler._tick.await_count > 0)
        initial = client.get("/system/health").json()["scheduler"]
        assert initial["running"] is True
        assert initial["last_tick_age_s"] == 0
        assert initial["status"] == "ok"
        assert initial["loop_restarts"] == 0
        assert isinstance(initial["log_write_failures"], int)
        assert scheduler.last_tick_started_at == clock[1]
        assert scheduler.last_tick_completed_at == clock[1]
        clock[0] += 120
        clock[1] -= 86400
        assert client.get("/system/health").json()["scheduler"]["status"] == "ok"
        clock[0] += 1
        stale = client.get("/system/health").json()["scheduler"]
        assert stale["last_tick_age_s"] == 121
        assert stale["status"] == "degraded"
    finally:
        await cleanup(scheduler)
        client.close()
        app.state.store_catalog.shutdown(deadline_seconds=5)


@pytest.mark.asyncio
async def test_r5_watchdog_alerts_once_per_stale_episode(registry, monkeypatch):
    scheduler = AgentScheduler(registry)
    clock = [100.0]
    monkeypatch.setattr(module, "time", SimpleNamespace(
        monotonic=lambda: clock[0], time=lambda: 1_800_000_000.0,
    ))
    scheduler._tick = AsyncMock()
    notify = AsyncMock(return_value=True)
    scheduler._queue_owner_alert = lambda *args: pytest.fail("alert queued on stale scheduler")
    watchdog = SessionWatchdog(
        streaming_sessions_fn=lambda: {},
        scheduler_health_fn=scheduler.health_snapshot,
        scheduler_notify_fn=notify,
    )
    try:
        await scheduler.start()
        await until(lambda: scheduler._tick.await_count > 0)
        await watchdog._sweep()
        notify.assert_not_awaited()
        clock[0] += 300
        await watchdog._sweep()
        notify.assert_not_awaited()
        clock[0] += 1
        await watchdog._sweep()
        await watchdog._sweep()
        notify.assert_awaited_once_with("scheduler has not ticked for 5 min; restarts=0")
        # Resume a real loop tick, then start a second stale episode.
        scheduler._task.cancel()
        await until(lambda: scheduler.loop_restarts == 1)
        await until(lambda: scheduler._tick.await_count > 1)
        await watchdog._sweep()
        clock[0] += 301
        await watchdog._sweep()
        assert notify.await_count == 2
        assert notify.await_args.args == ("scheduler has not ticked for 5 min; restarts=1",)
    finally:
        await cleanup(scheduler)
