"""Telemetry setup must not take authoritative scheduling down with it."""

import logging
import sqlite3
import threading
import time

import pytest

from pinky_daemon.agent_registry import AgentRegistry
import pinky_daemon.schedule_fire_trace as ft


@pytest.fixture
def locked_store(tmp_path):
    path = str(tmp_path / "registry.db")
    registry = AgentRegistry(path)
    registry.close()
    lock = sqlite3.connect(ft.ScheduleFireTrace.path_for(path), check_same_thread=False)
    lock.execute("BEGIN EXCLUSIVE")
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
        registry.register("worker")
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
