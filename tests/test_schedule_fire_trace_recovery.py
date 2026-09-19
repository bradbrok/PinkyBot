"""Lifecycle evidence and reader isolation under independent failure sequences."""

import sqlite3
import threading
import time
from collections import deque

import pytest
from fastapi.testclient import TestClient

import pinky_daemon.schedule_fire_trace as ft
from pinky_daemon.agent_registry import AgentRegistry
from pinky_daemon.scheduler import ScheduleWakeReceipt


def idle(writer, timeout=3):
    deadline = time.monotonic() + timeout
    while writer._running or writer._queue.unfinished_tasks:
        assert time.monotonic() < deadline, "owned trace worker did not finish"
        threading.Event().wait(0.002)


@pytest.fixture
def registry(tmp_path):
    registry = AgentRegistry(str(tmp_path / "ledger.db"))
    registry.register("worker", working_dir=str(tmp_path / "worker"))
    yield registry
    registry.close()


def fire(registry, *, age=4):
    schedule = registry.add_schedule("worker", "*/7 * * * *", name="recurring", prompt="work")
    pending, _ = registry.persist_schedule_wake(
        schedule.id, agent_name="worker", schedule_name=schedule.name,
        prompt=schedule.prompt, fired_at=time.time() - age,
    )
    idle(registry._fire_trace)
    return pending, ScheduleWakeReceipt(registry, schedule.id, pending.fired_at)


def outcome(registry):
    idle(registry._fire_trace)
    return registry._fire_trace.report(since=0)["rows"][0]["outcome"]


def test_worker_connection_requests_zero_timeout_explicitly(registry, monkeypatch):
    calls = []
    original = ft.open_store_connection

    def opened(*args, **kwargs):
        calls.append((args[1], kwargs["timeout"]))
        return original(*args, **kwargs)

    monkeypatch.setattr(ft, "open_store_connection", opened)
    connection = registry._fire_trace._connect()
    connection.close()
    assert calls == [("schedule_fire_trace", 0)]


def test_report_classification_releases_truncate_ledger_before_accept(registry, monkeypatch):
    _, receipt = fire(registry)
    assert registry._db.execute("PRAGMA journal_mode").fetchone()[0] == "truncate"
    entered, release, accepted = threading.Event(), threading.Event(), threading.Event()
    original = ft.derive_outcome
    results, errors, timings = [], [], []

    def classify(*args, **kwargs):
        if threading.current_thread() is reader and not entered.is_set():
            entered.set()
            assert release.wait(3)
        return original(*args, **kwargs)

    def read():
        try:
            results.append(registry._fire_trace.report())
        except BaseException as exc:
            errors.append(exc)

    def accept():
        try:
            start = time.perf_counter()
            results.append(receipt.accept())
            timings.append(time.perf_counter() - start)
        except BaseException as exc:
            errors.append(exc)
        finally:
            accepted.set()

    monkeypatch.setattr(ft, "derive_outcome", classify)
    reader = threading.Thread(target=read)
    accepter = threading.Thread(target=accept)
    reader.start()
    try:
        assert entered.wait(3), "classification pause was not reached"
        accepter.start()
        completed_before_release = accepted.wait(0.2)
    finally:
        release.set()
        reader.join(3)
        if accepter.ident is not None:
            accepter.join(3)
    assert not reader.is_alive() and not accepter.is_alive()
    assert not errors
    assert True in results
    print("ACCEPT_SECONDS_DURING_REPORT", timings)
    assert completed_before_release, "report classification retained a ledger read lock"
    assert timings[0] < 0.05


def test_failure_recovery_continues_beyond_a_full_retry_cycle(registry, monkeypatch):
    pending, _ = fire(registry)
    writer = registry._fire_trace
    scheduled = deque()
    delays = []

    class Timer:
        def __init__(self, delay, callback):
            self.callback = callback
            self.cancelled = False
            delays.append(delay)

        def start(self):
            scheduled.append(self)

        def cancel(self):
            self.cancelled = True

    monkeypatch.setattr(ft.threading, "Timer", Timer)
    lock = sqlite3.connect(writer.path, timeout=0)
    lock.execute("BEGIN IMMEDIATE")
    try:
        writer.failed({"edge": "observed", "at": time.time(), "fire_id": pending.id},
                      RuntimeError("unavailable trace storage"))
        # Advance actual callback/worker cycles deterministically while SQLite stays locked.
        for _ in range(13):
            assert scheduled, "recovery permanently stopped after its retry budget"
            timer = scheduled.popleft()
            assert not timer.cancelled
            timer.callback()
            idle(writer)
        assert len(writer._failures) == 1
        assert scheduled
        assert max(delays) <= 5
        lock.rollback()
        scheduled.popleft().callback()
        idle(writer)
        with sqlite3.connect(writer.path) as db:
            assert db.execute("SELECT SUM(failures) FROM schedule_fire_trace_failures").fetchone()[0] == 1
        assert not writer._failures
    finally:
        lock.rollback()
        lock.close()


def test_flush_includes_the_final_failure_persistence_pass(registry, monkeypatch):
    _, receipt = fire(registry)
    writer = registry._fire_trace
    original = writer._persist_failures
    entered, release = threading.Event(), threading.Event()
    calls = []

    def persist():
        calls.append(1)
        if len(calls) == 2:
            entered.set()
            assert release.wait(3)
        return original()

    monkeypatch.setattr(writer, "_persist_failures", persist)
    receipt.trace("replay")
    try:
        assert entered.wait(3)
        assert writer._queue.unfinished_tasks == 0
        assert not writer.flush(timeout=0.02), "flush claimed completion before final persistence"
    finally:
        release.set()
        idle(writer)
    assert writer.flush(timeout=0.1)


@pytest.mark.parametrize("transition", ["release_then_park", "terminal_then_accept"])
def test_counted_transition_loss_survives_later_authoritative_changes(
    registry, monkeypatch, transition,
):
    pending, receipt = fire(registry)
    assert registry.drain_park_pending_schedule_wake(pending.id)
    assert outcome(registry) == "drain_parked"
    writer = registry._fire_trace
    original = writer._write

    def fail(event):
        if event["edge"] == "abandon":
            raise RuntimeError("lost lifecycle observation")
        return original(event)

    monkeypatch.setattr(writer, "_write", fail)
    if transition == "release_then_park":
        assert registry.release_drain_parked_schedule_wakes("worker") == 1
    else:
        assert registry.abandon_pending_schedule_wake(pending.id)
    assert outcome(registry) == "trace_incomplete"
    assert writer.failure_counts(since=0)["abandon"] == 1
    monkeypatch.setattr(writer, "_write", original)
    if transition == "release_then_park":
        assert registry.drain_park_pending_schedule_wake(pending.id)
    else:
        assert receipt.accept()
    assert outcome(registry) == "trace_incomplete"
    writer.close()
    registry._fire_trace = ft.ScheduleFireTrace(registry._db_path)
    assert outcome(registry) == "trace_incomplete"


@pytest.fixture
def api(tmp_path):
    from pinky_daemon.api import create_api

    app = create_api(db_path=str(tmp_path / "api.db"))
    client = TestClient(app, raise_server_exceptions=False)
    yield app, client
    client.close()
    app.state.agents.close()


@pytest.mark.parametrize("parameter,value", [
    ("offset", 2**63), ("offset", 10**30),
    ("schedule_id", 2**63), ("schedule_id", -(2**63) - 1),
    ("since", "nan"), ("since", "inf"), ("since", "-inf"),
])
def test_query_numbers_are_rejected_before_storage_binding(api, parameter, value):
    _, client = api
    assert client.cookies, "the positive request must carry the admin cookie"
    response = client.get("/scheduler/fire-trace", params={parameter: value})
    assert response.status_code == 422, response.text


def test_query_integer_boundary_and_finite_window_remain_valid(api):
    _, client = api
    for parameters in ({"offset": 2**63 - 1}, {"schedule_id": -(2**63)}, {"since": 1.25}):
        response = client.get("/scheduler/fire-trace", params=parameters)
        assert response.status_code == 200
        assert response.json()["rows"] == []


@pytest.mark.parametrize("aligned", [False, True])
def test_status_overflow_window_exposes_bounds_before_and_after_reopen(api, monkeypatch, aligned):
    app, client = api
    registry = app.state.agents
    writer = registry._fire_trace
    idle(writer)
    writer.DETAIL_LIMIT = 0
    hour = (int(time.time()) // 3600 - 26) * 3600
    cutoff = hour + (3600 if aligned else 1800)
    monkeypatch.setattr(time, "time", lambda: cutoff + 86400)
    # One event is definitely outside; the second is in a fully contained hour.
    for timestamp in (hour + 900, hour + 3700):
        writer.failed({"edge": "observed", "at": timestamp}, RuntimeError("overflow"))
    writer._kick()
    idle(writer)
    for reopened in (False, True):
        if reopened:
            writer.close()
            writer = registry._fire_trace = ft.ScheduleFireTrace(registry._db_path)
        response = client.get("/scheduler/status")
        assert response.status_code == 200
        status = response.json()
        assert "trace_write_failure_bounds_24h" in status
        bounds = status["trace_write_failure_bounds_24h"]["observed"]
        assert bounds["count"] == bounds["upper"] == (1 if aligned else 2)
        assert bounds["lower"] == 1
        assert bounds["exact"] is aligned
        assert status["trace_write_failures_24h"]["observed"] == bounds["upper"]
        assert writer.failure_counts(since=cutoff)["observed"] == bounds["upper"]
        assert bounds["display"] == ("1" if aligned else "≤2 (approx.)")
        if not aligned:
            assert bounds["bucket_start"] == hour
            assert bounds["bucket_end"] == hour + 3600


@pytest.mark.parametrize("transition", ["terminal", "later_park"])
def test_new_lifecycle_timestamp_extends_retention_of_old_history(registry, monkeypatch, transition):
    current = time.time()
    old = current - 43 * 86400
    with monkeypatch.context() as clock:
        clock.setattr(time, "time", lambda: old)
        pending, _ = fire(registry)
        assert registry.drain_park_pending_schedule_wake(pending.id)
        idle(registry._fire_trace)
        if transition == "later_park":
            assert registry.release_drain_parked_schedule_wakes("worker") == 1
            idle(registry._fire_trace)
    if transition == "terminal":
        assert registry.abandon_pending_schedule_wake(pending.id, abandoned_at=current)
    else:
        assert registry.drain_park_pending_schedule_wake(pending.id, drain_parked_at=current)
    writer = registry._fire_trace
    idle(writer)
    writer.submit({"edge": "prune", "at": current + 2, "retention_days": 30})
    idle(writer)
    rows = writer.report(since=0)["rows"]
    assert len(rows) == 1, "fresh lifecycle evidence was immediately pruned"
    assert rows[0]["updated_at"] >= current
    assert rows[0]["abandoned_at"] == old


def test_replayed_fire_retains_failure_uncertainty_after_detail_pruning(registry, monkeypatch):
    current = time.time()
    old = current - 41 * 86400
    with monkeypatch.context() as clock:
        clock.setattr(time, "time", lambda: old)
        pending, receipt = fire(registry)
        receipt.trace("paste", pointer="{}")
        idle(registry._fire_trace)
        registry._fire_trace.failed(
            {"edge": "observed", "at": old, "fire_id": pending.id}, RuntimeError("lost row")
        )
        registry._fire_trace._kick()
        idle(registry._fire_trace)
    assert outcome(registry) == "trace_incomplete"
    receipt.trace("replay")
    idle(registry._fire_trace)
    registry._fire_trace.submit({"edge": "prune", "at": current + 2, "retention_days": 30})
    assert outcome(registry) == "trace_incomplete"
    registry._fire_trace.close()
    registry._fire_trace = ft.ScheduleFireTrace(registry._db_path)
    assert outcome(registry) == "trace_incomplete"


def test_report_page_and_window_counts_share_a_snapshot_during_prune(registry, monkeypatch):
    writer = registry._fire_trace
    old = time.time() - 39 * 86400
    with sqlite3.connect(writer.path) as db:
        db.executemany(
            "INSERT INTO schedule_fire_trace(schedule_id,fired_at,updated_at) VALUES(?,?,?)",
            [(1000 + index, old + index, old) for index in range(23)],
        )
    original = writer._connect
    entered = []
    reader_thread = threading.get_ident()

    class Connection:
        def __init__(self, db):
            self.db = db

        def __getattr__(self, name):
            return getattr(self.db, name)

        def execute(self, sql, *args):
            if "SELECT derived_outcome,COUNT(*)" in sql and not entered:
                entered.append(True)
                try:
                    writer._write({"edge": "prune", "at": time.time(), "retention_days": 30})
                except sqlite3.OperationalError as exc:
                    assert exc.sqlite_errorcode == sqlite3.SQLITE_BUSY
            return self.db.execute(sql, *args)

    def connect(**kwargs):
        db = original(**kwargs)
        return Connection(db) if kwargs.get("timeout") and threading.get_ident() == reader_thread else db

    monkeypatch.setattr(writer, "_connect", connect)
    report = writer.report(since=0, limit=11)
    assert entered, "concurrent maintenance injection must run between page and count reads"
    assert len(report["rows"]) == 11
    assert report["total"] == report["counts"]["pending"] == 23
    assert report["next_offset"] == 11
    writer._write({"edge": "prune", "at": time.time(), "retention_days": 30})
    assert writer.report(since=0)["rows"] == []
