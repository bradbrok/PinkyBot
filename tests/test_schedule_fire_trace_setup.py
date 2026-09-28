"""Telemetry setup must not take authoritative scheduling down with it."""

import logging
import os
import sqlite3
import threading
import time
from pathlib import Path

import httpx
import pytest

import pinky_daemon.schedule_fire_trace as ft
from pinky_daemon.agent_registry import AgentRegistry


@pytest.fixture(params=["EXCLUSIVE", "IMMEDIATE"])
def locked_store(tmp_path, request):
    path = str(tmp_path / "registry.db")
    registry = AgentRegistry(path)
    registry.close()
    lock = sqlite3.connect(ft.ScheduleFireTrace.path_for(path), check_same_thread=False)
    lock.execute("BEGIN " + request.param)
    try:
        yield path, lock
    finally:
        lock.rollback()
        lock.close()


def configure(monkeypatch, *, budget=0.04, retry=0.02):
    # raising=False keeps the pre-fix RED at the actual locked constructor.
    monkeypatch.setattr(ft, "SETUP_TIMEOUT_SECONDS", budget, raising=False)
    monkeypatch.setattr(ft, "SETUP_RETRY_SECONDS", retry, raising=False)
    monkeypatch.setattr(ft, "SETUP_RETRY_MAX_SECONDS", retry * 4, raising=False)


def wait_until(predicate):
    deadline = time.monotonic() + 5
    while not predicate():
        assert time.monotonic() < deadline, "background recovery stalled"
        threading.Event().wait(0.01)


def test_setup_waits_for_brief_lock(locked_store, monkeypatch):
    path, lock = locked_store
    configure(monkeypatch, budget=0.4)
    timer = threading.Timer(0.04, lock.rollback)
    timer.start()
    registry = None
    try:
        registry = AgentRegistry(path)
        assert registry._fire_trace.status()["state"] == "healthy"
        with registry._fire_trace._connect() as db:
            assert db.execute("PRAGMA busy_timeout").fetchone()[0] == 0
    finally:
        timer.join()
        if registry is not None:
            registry.close()


def test_exhausted_setup_keeps_authoritative_accept_and_counts_drops(
    locked_store, monkeypatch, caplog
):
    path, _ = locked_store
    configure(monkeypatch, retry=60)
    caplog.set_level(logging.INFO, logger=ft.__name__)
    started = time.monotonic()
    registry = AgentRegistry(path)
    try:
        assert time.monotonic() - started < 1, "ignored injected setup budget"
        registry.register("worker", working_dir=str(Path(path).parent / "worker"))
        schedule = registry.add_schedule("worker", "* * * * *", name="wake", prompt="wake")
        pending, _ = registry.persist_schedule_wake(
            schedule.id, agent_name="worker", schedule_name="wake", prompt="wake", fired_at=100
        )
        assert registry.confirm_pending_schedule_wake(pending.id)
        assert registry.get_schedule_wake_by_fire(schedule.id, 100).accepted_at > 0
        status = registry._fire_trace.status()
        assert status["state"] == "degraded"
        assert status["dropped_events"] == 2
        assert registry._fire_trace._queue.empty()
        errors = [r for r in caplog.records if r.name == ft.__name__ and r.levelno == logging.ERROR]
        assert len(errors) == 1
        assert "degraded" in errors[0].message
        report = registry._fire_trace.report()
        assert report["status"] == status
        assert report["counts_scope"] == "unavailable"
    finally:
        registry.close()


def test_setup_recovers_without_restart_and_logs_transitions_once(
    locked_store, monkeypatch, caplog
):
    path, lock = locked_store
    configure(monkeypatch)
    caplog.set_level(logging.INFO, logger=ft.__name__)
    registry = AgentRegistry(path)
    writer = registry._fire_trace
    try:
        writer.submit(dict(edge="replay", at=100, schedule_id=1, fired_at=100))
        wait_until(lambda: writer.status()["setup_attempts"] >= 3)
        assert writer.status()["state"] == "degraded"
        assert len([r for r in caplog.records if r.levelno >= logging.ERROR]) == 1
        lock.rollback()
        wait_until(lambda: writer.status()["state"] == "recovered")
        assert writer.status()["dropped_events"] == 1
        writer.submit(dict(edge="replay", at=101, schedule_id=2, fired_at=101))
        assert writer.flush()
        rows = writer.report()["rows"]
        assert len(rows) == 1
        assert rows[0]["schedule_id"] == 2
        assert rows[0]["replay_count"] == 1
        recovered = [r.message for r in caplog.records if "recovered" in r.message]
        assert len(recovered) == 1
        assert "dropped_events=1" in recovered[0]
        assert writer.report()["status"]["state"] == "recovered"
    finally:
        registry.close()


def test_close_cancels_setup_recovery(locked_store, monkeypatch):
    path, lock = locked_store
    configure(monkeypatch, retry=60)
    registry = AgentRegistry(path)
    writer = registry._fire_trace
    attempts = writer.status()["setup_attempts"]
    registry.close()
    lock.rollback()
    writer._kick()
    assert writer._retry_timer is None
    assert writer.status()["setup_attempts"] == attempts


def test_setup_backoff_is_capped_and_submits_do_not_schedule_retries(
    locked_store, monkeypatch, caplog
):
    from types import SimpleNamespace

    path, _ = locked_store
    configure(monkeypatch)
    timers = []

    class Timer:
        def __init__(self, delay, callback):
            self.delay, self.callback = delay, callback
            self.cancelled = False
            timers.append(self)

        def start(self):
            pass

        def cancel(self):
            self.cancelled = True

    # Only this module's Timer changes; queue/flush synchronization stays real.
    monkeypatch.setattr(ft, "threading", SimpleNamespace(
        Timer=Timer, RLock=threading.RLock, Event=threading.Event,
    ))
    registry = AgentRegistry(path)
    writer = registry._fire_trace
    try:
        for n in range(100):
            writer.submit(dict(edge="replay", at=n))
        assert len(timers) == 1
        assert writer.status()["setup_attempts"] == 1
        assert writer.status()["dropped_events"] == 100
        for n in range(4):
            timers[n].callback()
            assert writer.flush()
        assert [timer.delay for timer in timers] == [0.02, 0.04, 0.08, 0.08, 0.08]
        assert writer.status()["setup_attempts"] == 5
        assert len([r for r in caplog.records if r.levelno >= logging.ERROR]) == 1
    finally:
        registry.close()
    assert timers[-1].cancelled


@pytest.mark.parametrize("release_early", [True, False], ids=["retried", "accounted"])
def test_worker_connect_contention_is_retried_or_accounted(tmp_path, monkeypatch, release_early):
    registry = AgentRegistry(str(tmp_path / "registry.db"))
    writer = registry._fire_trace
    lock = sqlite3.connect(writer.path)
    lock.execute("BEGIN EXCLUSIVE")
    original_connect, original_failed = writer._connect, writer.failed
    attempted, failed, released = threading.Event(), threading.Event(), threading.Event()
    errors = []

    def connect(**kwargs):
        try:
            return original_connect(**kwargs)
        except sqlite3.OperationalError:
            attempted.set()
            if release_early:
                # Observe a real failed open, then release before allowing the
                # product's retry budget to run. Host scheduling is not the test.
                assert released.wait(5)
            raise

    def account(event, error):
        errors.append(error)
        original_failed(event, error)
        failed.set()

    monkeypatch.setattr(writer, "_connect", connect)
    monkeypatch.setattr(writer, "failed", account)
    try:
        writer.submit(dict(edge="replay", schedule_id=1, fired_at=100, at=101))
        assert attempted.wait(5)
        if not release_early:
            assert failed.wait(5)
            assert len(errors) == 1
            assert isinstance(errors[0], sqlite3.OperationalError)
        lock.rollback()
        released.set()
        assert writer.flush()
        if release_early:
            assert writer.report()["rows"][0]["replay_count"] == 1
        else:
            def persisted():
                with sqlite3.connect(writer.path) as db:
                    return db.execute("SELECT COUNT(*) FROM schedule_fire_trace_failures").fetchone()[0]

            wait_until(lambda: persisted() == 1)
            assert writer.failures()[0]["reason"] == "OperationalError"
            assert writer.report()["rows"] == []
    finally:
        lock.rollback()
        released.set()
        lock.close()
        registry.close()


@pytest.mark.parametrize("endpoint", ["/admin/watchdog", "/scheduler/status", "/scheduler/fire-trace"])
async def test_degraded_status_is_visible_without_database_reads(tmp_path, monkeypatch, endpoint):
    from pinky_daemon.api import create_api
    from pinky_daemon.auth import SESSION_COOKIE_NAME, create_session_cookie

    app = create_api(db_path=str(tmp_path / "api.db"))
    registry = app.state.agents
    registry._fire_trace.close()
    lock = sqlite3.connect(registry._fire_trace.path)
    lock.execute("BEGIN EXCLUSIVE")
    configure(monkeypatch, retry=60)
    registry._fire_trace = ft.ScheduleFireTrace(registry._db_path)
    try:
        registry._fire_trace.submit(dict(edge="replay", at=100))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test",
            cookies={SESSION_COOKIE_NAME: create_session_cookie(os.environ["PINKY_SESSION_SECRET"])},
        ) as client:
            response = await client.get(endpoint)
        assert response.status_code == 200
        key = {"/admin/watchdog": "fire_trace", "/scheduler/status": "fire_trace_status",
               "/scheduler/fire-trace": "status"}[endpoint]
        status = response.json()[key]
        assert status["state"] == "degraded"
        assert status["dropped_events"] == 1
    finally:
        lock.rollback()
        lock.close()
        registry.close()
