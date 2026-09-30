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


@pytest.mark.parametrize("fault", ["write", "flush", "closed"])
def test_log_writer_failures_are_counted(monkeypatch, fault):
    error = ValueError("closed stream") if fault == "closed" else OSError(28, "disk full")
    writer = FailingWriter(
        first=1 if fault == "write" else 100,
        flush_error=None if fault == "write" else error,
    )
    before = module.log_write_failures
    with monkeypatch.context() as patch:
        patch.setattr(module.sys, "stderr", writer)
        module._log("diagnostic")
    assert module.log_write_failures == before + 1


@pytest.mark.asyncio
async def test_tick_base_exception_and_broken_error_formatting_are_contained(registry):
    class BrokenError(BaseException):
        def __str__(self):
            raise LoopExit()

    scheduler = AgentScheduler(registry, tick_interval=0.001)
    calls = 0

    async def tick():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise LoopExit()
        if calls == 2:
            raise BrokenError()

    scheduler._tick = tick
    try:
        await scheduler.start()
        await until(lambda: calls >= 3)
        assert scheduler.loop_restarts == 0
    finally:
        await cleanup(scheduler)


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [asyncio.CancelledError, KeyboardInterrupt, SystemExit])
async def test_process_control_exceptions_escape_tick(registry, error):
    scheduler = AgentScheduler(registry)
    scheduler._running = True
    scheduler._tick = AsyncMock(side_effect=error)
    with pytest.raises(error):
        await scheduler._loop()
    scheduler._running = False


@pytest.mark.asyncio
async def test_supervisor_backoff_caps_and_resets_after_ten_clean_ticks(registry, monkeypatch):
    scheduler = AgentScheduler(registry, tick_interval=0.001)
    event_loop = asyncio.get_running_loop()
    real_call_later = event_loop.call_later
    delays = []

    def call_later(delay, callback, *args, **kwargs):
        if callback == scheduler._restart_loop:
            delays.append(delay)
            delay = 0
        return real_call_later(delay, callback, *args, **kwargs)

    monkeypatch.setattr(event_loop, "call_later", call_later)
    entered = 0
    ticks = 0
    real_loop = scheduler._loop
    idle = asyncio.Event()

    async def loop():
        nonlocal entered
        entered += 1
        if entered <= 5:
            raise LoopExit()
        if entered == 6:
            await real_loop()
        else:
            await idle.wait()

    async def tick():
        nonlocal ticks
        ticks += 1

    async def sleep(delay):
        if ticks == 10:
            raise LoopExit()
        await asyncio.sleep(delay)

    monkeypatch.setattr(module, "asyncio", SimpleNamespace(**{
        **vars(asyncio), "sleep": sleep,
    }))
    scheduler._loop = loop
    scheduler._tick = tick
    try:
        await scheduler.start()
        await until(lambda: entered == 7)
        assert delays == [1, 5, 30, 60, 60, 1]
        assert scheduler.loop_restarts == 6
        assert ticks == 10
    finally:
        await cleanup(scheduler)


@pytest.mark.asyncio
async def test_owner_notify_failure_logging_cannot_escape(registry, monkeypatch):
    scheduler = AgentScheduler(registry, owner_notify_callback=AsyncMock(side_effect=OSError(28, "full")))
    try:
        with monkeypatch.context() as patch:
            patch.setattr(module.sys, "stderr", FailingWriter())
            scheduler._queue_owner_alert("worker", "delivery failed")
            tasks = list(scheduler._owner_alert_tasks)
            assert len(tasks) == 1
            await asyncio.gather(*tasks)
            assert tasks[0].exception() is None
    finally:
        await cleanup(scheduler)


@pytest.mark.asyncio
async def test_watchdog_retry_requires_positive_receipt_and_ignores_stopped_scheduler(monkeypatch):
    from pinky_daemon import session_watchdog

    clock = [100.0]
    monkeypatch.setattr(session_watchdog, "time", SimpleNamespace(
        monotonic=lambda: clock[0], time=lambda: clock[0],
    ))
    health = {
        "enabled": True, "running": False, "last_tick_age_s": 601,
        "tick_interval_s": 60, "loop_restarts": 2,
    }
    notify = AsyncMock(side_effect=[OSError(28, "full"), False, True, True])
    watchdog = SessionWatchdog(
        streaming_sessions_fn=lambda: {},
        scheduler_health_fn=lambda: health,
        scheduler_notify_fn=notify,
    )
    for _ in range(4):
        await watchdog._sweep()
        clock[0] += 301
    assert notify.await_count == 3
    health["enabled"] = False
    await watchdog._sweep()
    assert notify.await_count == 3
    health["enabled"] = True
    await watchdog._sweep()
    assert notify.await_count == 4


@pytest.mark.asyncio
async def test_api_watchdog_notifies_owner_directly_with_broken_stderr(tmp_path, monkeypatch):
    from pinky_daemon import api

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(api, "SHARED_MCP_ENABLED", False)
    app = api.create_api(db_path=str(tmp_path / "conversations.db"))
    scheduler = app.state.scheduler
    reg = scheduler._registry
    reg.register("worker", runtime="claude_sdk")
    reg.set_token("worker", "telegram", "test-token", settings={"account_id": "test"})
    reg.set_owner_notification_destinations([
        {"platform": "telegram", "account_id": "unavailable", "conversation_id": "123", "principal_id": "123"},
        {"platform": "telegram", "account_id": "test", "conversation_id": "123", "principal_id": "123"},
    ])
    scheduler._running = True
    scheduler._started_monotonic = module.time.monotonic() - 601
    scheduler._queue_owner_alert = lambda *args: pytest.fail("scheduler queue used")
    send = AsyncMock(return_value={"sent": True})
    monkeypatch.setattr(app.state.broker, "_send_callback", send)
    try:
        with monkeypatch.context() as patch:
            patch.setattr(module.sys, "stderr", FailingWriter(last=100))
            await app.state.watchdog._sweep()
            await app.state.watchdog._sweep()
        send.assert_awaited_once()
        assert send.await_args.args[3] == "scheduler has not ticked for 10 min; restarts=0"
    finally:
        await cleanup(scheduler)
        app.state.store_catalog.shutdown(deadline_seconds=5)


@pytest.mark.asyncio
@pytest.mark.parametrize("result", [False, None, OSError(28, "disk full")])
async def test_watchdog_unconfirmed_alerts_back_off_and_cap_per_episode(monkeypatch, result):
    from pinky_daemon import session_watchdog

    clock = [100.0]
    monkeypatch.setattr(session_watchdog, "time", SimpleNamespace(
        monotonic=lambda: clock[0], time=lambda: clock[0],
    ))
    health = {"enabled": True, "running": False, "last_tick_age_s": 601,
              "tick_interval_s": 30, "loop_restarts": 3}
    notify = AsyncMock(side_effect=result) if isinstance(result, Exception) else AsyncMock(return_value=result)
    watchdog = SessionWatchdog(streaming_sessions_fn=lambda: {},
                               scheduler_health_fn=lambda: health, scheduler_notify_fn=notify)
    await watchdog._sweep()
    assert notify.await_count == 1
    clock[0] += 59
    await watchdog._sweep()
    assert notify.await_count == 1
    clock[0] += 1
    await watchdog._sweep()
    assert notify.await_count == 2
    clock[0] += 299
    await watchdog._sweep()
    assert notify.await_count == 2
    clock[0] += 1
    await watchdog._sweep()
    assert notify.await_count == 3
    assert watchdog.stale_alert_undelivered == 1
    for _ in range(10):
        clock[0] += 3600
        await watchdog._sweep()
    assert notify.await_count == 3
    assert watchdog.stale_alert_undelivered == 1
    health["last_tick_age_s"] = 0
    await watchdog._sweep()
    health["last_tick_age_s"] = 601
    await watchdog._sweep()
    assert notify.await_count == 4
    assert watchdog.status()["scheduler_alert"]["stale_alert_undelivered"] == 1
