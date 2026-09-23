"""Independent synthetic-only review controls; desired invariants intentionally RED."""

import asyncio
import logging
import os
import queue
import sqlite3
import threading
import time
from contextlib import closing

import httpx
import pytest

import pinky_daemon.agent_registry as ar
import pinky_daemon.schedule_fire_trace as ft
from pinky_daemon.scheduler import ScheduleWakeReceipt


@pytest.fixture
def registry(tmp_path):
    r = ar.AgentRegistry(str(tmp_path / "synthetic.db"))
    r.register("review-worker", working_dir=str(tmp_path / "worker"))
    yield r
    r.close()


def fire(r):
    s = r.add_schedule("review-worker", "* * * * *", name="synthetic", prompt="review")
    p, _ = r.persist_schedule_wake(
        s.id,
        agent_name="review-worker",
        schedule_name=s.name,
        prompt="review",
        fired_at=time.time() - 1,
    )
    assert r._fire_trace.flush()
    return p, ScheduleWakeReceipt(r, s.id, p.fired_at)


def test_positive_uncontended_accept_and_sqlite_policy(registry):
    p, receipt = fire(registry)
    started = time.monotonic()
    assert receipt.accept()
    assert time.monotonic() - started < 5, "authoritative accept stalled"
    assert registry.get_schedule_wake_by_fire(p.schedule_id, p.fired_at).accepted_at > 0
    # The zero-timeout policy probe must not race the accept trace just queued.
    assert registry._fire_trace.flush()
    assert registry._db.execute("PRAGMA journal_mode").fetchone()[0] == "truncate"
    db = registry._fire_trace._connect()
    try:
        policies = {
            "registry_busy_ms": registry._db.execute("PRAGMA busy_timeout").fetchone()[0],
            "worker_busy_ms": db.execute("PRAGMA busy_timeout").fetchone()[0],
            "worker_journal": db.execute("PRAGMA journal_mode").fetchone()[0],
            "worker_mmap": db.execute("PRAGMA mmap_size").fetchone()[0],
        }
    finally:
        db.close()
    print("POLICIES", policies)
    assert policies["registry_busy_ms"] > 0
    assert policies["worker_busy_ms"] == 0
    assert policies["worker_journal"] == "truncate"
    assert policies["worker_mmap"] == 0
    assert registry._fire_trace.flush()
    assert registry.get_schedule_wake_by_fire(p.schedule_id, p.fired_at).accepted_at > 0


def slow_trace_accept(registry, monkeypatch, hold):
    pending, receipt = fire(registry)
    writer = registry._fire_trace
    original = writer._connect
    entered, release = threading.Event(), threading.Event()
    once = [False]

    class Cursor:
        def __init__(self, cursor, owner):
            self.cursor = cursor
            self.owner = owner

        def __getattr__(self, name):
            return getattr(self.cursor, name)

        def execute(self, sql, *args):
            self.cursor.execute(sql, *args)
            self.owner.pause(sql)
            return self

        def executemany(self, sql, *args):
            self.cursor.executemany(sql, *args)
            self.owner.pause(sql)
            return self

    class Connection:
        def __init__(self, db):
            self.db = db

        def __enter__(self):
            self.db.__enter__()
            return self

        def __exit__(self, *args):
            return self.db.__exit__(*args)

        def __getattr__(self, name):
            return getattr(self.db, name)

        def pause(self, sql):
            if (
                sql.lstrip().startswith(("INSERT", "UPDATE", "DELETE"))
                and "schedule_fire_trace" in sql
                and not once[0]
            ):
                once[0] = True
                assert self.db.in_transaction
                entered.set()
                assert release.wait(hold + 2)

        def execute(self, sql, *args):
            result = self.db.execute(sql, *args)
            self.pause(sql)
            return result

        def executemany(self, sql, *args):
            result = self.db.executemany(sql, *args)
            self.pause(sql)
            return result

        def cursor(self, *args, **kwargs):
            return Cursor(self.db.cursor(*args, **kwargs), self)

    monkeypatch.setattr(writer, "_connect", lambda **kw: Connection(original(**kw)))
    receipt.trace("replay")
    assert entered.wait(1)
    timer = threading.Timer(hold, release.set)
    timer.start()
    try:
        started = time.monotonic()
        assert receipt.accept()
        elapsed = time.monotonic() - started
        assert registry.get_schedule_wake_by_fire(pending.schedule_id, pending.fired_at).accepted_at
        assert elapsed < 0.5, "trace DML held an authoritative receipt lock"
    finally:
        release.set()
        timer.join()
        assert writer.flush()
    assert once[0], "the DML pause must fire after transaction lock acquisition"


def test_trace_write_lock_must_not_delay_authoritative_accept(registry, monkeypatch):
    slow_trace_accept(registry, monkeypatch, 0.65)


@pytest.mark.parametrize("endpoint", ["fire-trace", "status"])
async def test_trace_read_endpoint_must_not_block_event_loop(tmp_path, endpoint):
    from pinky_daemon.api import create_api
    from pinky_daemon.auth import SESSION_COOKIE_NAME, create_session_cookie

    app = create_api(db_path=str(tmp_path / "asgi.db"))
    registry = app.state.agents
    registry.register("review-worker", working_dir=str(tmp_path / "worker"))
    fire(registry)
    lock = sqlite3.connect(registry._fire_trace.path, timeout=0, check_same_thread=False)
    lock.execute("BEGIN EXCLUSIVE")
    timer = threading.Timer(0.30, lock.rollback)
    timer.start()

    async def heartbeat():
        started = time.monotonic()
        await asyncio.sleep(0.01)
        return time.monotonic() - started

    beat = asyncio.create_task(heartbeat())
    await asyncio.sleep(0)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            cookies={
                SESSION_COOKIE_NAME: create_session_cookie(os.environ["PINKY_SESSION_SECRET"]),
            },
        ) as client:
            response = await client.get("/scheduler/" + endpoint)
        lag = await beat
        assert response.status_code == 200
        result = response.json()
        assert (
            len(result["rows"]) == 1
            if endpoint == "fire-trace"
            else result["fire_trace_24h"]["pending"] == 1
        )
        assert lag < 0.1, "diagnostic reads blocked the event loop"
    finally:
        timer.join()
        lock.close()
        registry.close()


def test_failure_handoff_must_not_temporarily_hide_failure(registry, monkeypatch):
    _, receipt = fire(registry)
    w = registry._fire_trace
    event = {
        "edge": "observed",
        "at": time.time(),
        "schedule_id": receipt.schedule_id,
        "fired_at": receipt.fired_at,
    }
    w.failed(event, RuntimeError("synthetic"))
    original = w._connect
    once = [False]
    reader_thread = threading.get_ident()

    class Proxy:
        def __init__(self, db):
            self.db = db

        def execute(self, *a, **kw):
            return self.db.execute(*a, **kw)

        def close(self):
            self.db.close()
            # Deterministic worker commit/removal between DB snapshot and overlay.
            thread = threading.Thread(target=w._persist_failures)
            thread.start()
            thread.join(2)
            assert not thread.is_alive()

    def connect(**kw):
        db = original(**kw)
        if threading.get_ident() == reader_thread and not once[0]:
            once[0] = True
            return Proxy(db)
        return db

    monkeypatch.setattr(w, "_connect", connect)
    counts = w.failure_counts(since=0)
    db = original()
    durable = db.execute("SELECT COUNT(*) FROM schedule_fire_trace_failures").fetchone()[0]
    db.close()
    print(
        "FAILURE_HANDOFF",
        {"returned": counts["observed"], "durable": durable, "overlay": len(w._failures)},
    )
    assert once[0], "the persistence handoff must run during the reader snapshot"
    assert durable == 1
    assert counts["observed"] == 1, "commit/pop between snapshots made a known failure disappear"


def test_queue_overflow_logging_failure_must_not_escape_accept(registry, monkeypatch):
    p, receipt = fire(registry)
    w = registry._fire_trace
    monkeypatch.setattr(w, "_queue", queue.Queue(maxsize=1))
    entered, release = threading.Event(), threading.Event()
    original = w._write

    def delayed(event):
        entered.set()
        assert release.wait(2)
        return original(event)

    monkeypatch.setattr(w, "_write", delayed)
    receipt.trace("replay")
    assert entered.wait(1)
    receipt.trace("replay")

    class BrokenHandler(logging.Handler):
        def emit(self, record):
            raise OSError("synthetic log sink failure")

    handler = BrokenHandler()
    ft.logger.addHandler(handler)
    error = None
    try:
        try:
            receipt.accept()
        except Exception as exc:
            error = exc
    finally:
        ft.logger.removeHandler(handler)
        release.set()
        assert w.flush()
    assert registry.get_schedule_wake_by_fire(p.schedule_id, p.fired_at).accepted_at > 0
    print("CALLER_LOG_FAILURE", repr(error))
    assert error is None, "best-effort trace failed the receipt caller after durable acceptance"


def test_daily_prune_capacity_keeps_up_with_one_minute_schedule(registry):
    w = registry._fire_trace
    db = sqlite3.connect(w.path)
    now = time.time()
    old = now - 31 * 86400
    for day in range(2):
        db.executemany(
            "INSERT INTO schedule_fire_trace(schedule_id,fired_at,updated_at) VALUES (?,?,?)",
            [(999, old + day * 1440 + i, old) for i in range(1440)],
        )
        db.executemany(
            "INSERT INTO schedule_fire_trace_failures VALUES (?,?,?,?,?,?,?,?)",
            [
                (f"{day}-{i}", 999, old + i, None, "observed", old, 1, "synthetic")
                for i in range(1440)
            ],
        )
        db.commit()
        registry.prune_schedule_fire_trace(now=now + day * 86400)
        assert w.flush()
    count = db.execute("SELECT COUNT(*) FROM schedule_fire_trace").fetchone()[0]
    failures = db.execute("SELECT COUNT(*) FROM schedule_fire_trace_failures").fetchone()[0]
    plans = {
        "edge_failures": db.execute(
            "EXPLAIN QUERY PLAN SELECT edge FROM schedule_fire_trace_failures WHERE schedule_id=? AND fired_at=?",
            (999, old),
        ).fetchall(),
        "prune_trace": db.execute(
            "EXPLAIN QUERY PLAN SELECT rowid FROM schedule_fire_trace WHERE updated_at<? ORDER BY updated_at LIMIT 500",
            (now,),
        ).fetchall(),
        "prune_failures": db.execute(
            "EXPLAIN QUERY PLAN SELECT event_id FROM schedule_fire_trace_failures WHERE failed_at<? ORDER BY failed_at LIMIT 500",
            (now,),
        ).fetchall(),
    }
    db.close()
    assert count == failures == 0, "maintenance must catch up with recurring ingress"
    for plan in plans.values():
        details = " ".join(str(row[3]).upper() for row in plan)
        assert "SEARCH" in details and "TEMP B-TREE" not in details, details


def test_failed_last_edge_is_retried_after_lock_release(registry):
    p, receipt = fire(registry)
    receipt.trace("paste")
    assert registry._fire_trace.flush()
    lock = sqlite3.connect(registry._fire_trace.path, timeout=0)
    lock.execute("BEGIN IMMEDIATE")
    try:
        receipt.trace("observed")
        assert registry._fire_trace.flush(timeout=1)
        # Release storage only after the event worker has finished, so success
        # must come from the autonomous retry rather than its final flush.
        deadline = time.monotonic() + 1
        while registry._fire_trace._running and time.monotonic() < deadline:
            threading.Event().wait(0.001)
        assert not registry._fire_trace._running
    finally:
        lock.rollback()
        lock.close()
    w = registry._fire_trace
    deadline = time.monotonic() + 5
    while True:
        # This test reader competes with the autonomous retry's TRUNCATE commit.
        # Its busy wait must tolerate scheduling delays; the worker stays at zero.
        with closing(sqlite3.connect(w.path, timeout=5)) as db:
            persisted = db.execute("SELECT COUNT(*) FROM schedule_fire_trace_failures").fetchone()[
                0
            ]
        if persisted:
            break
        assert time.monotonic() < deadline, "failure was not persisted after lock release"
        threading.Event().wait(0.01)
    before = w.report()["rows"][0]["outcome"]
    # New reader models restart memory loss; do not gracefully close original first.
    reopened = ft.ScheduleFireTrace(registry._db_path, registry._db)
    try:
        after = reopened.report()["rows"][0]["outcome"]
    finally:
        reopened.close()
    print("LAST_BUSY_EDGE", {"before": before, "after_restart": after, "overlay": len(w._failures)})
    assert before == "trace_incomplete"
    assert after == "trace_incomplete", "idle worker never persisted failure after DB recovered"


def test_long_trace_transaction_must_not_stall_accept(registry, monkeypatch):
    slow_trace_accept(registry, monkeypatch, 5.3)


def test_failure_overlay_is_bounded_when_queue_is_full(registry, monkeypatch):
    _, receipt = fire(registry)
    w = registry._fire_trace
    entered, release = threading.Event(), threading.Event()
    original = w._write
    original_failed = w.failed
    first_replay, failures = [], []
    first_written = threading.Event()

    def delayed(event):
        if event["edge"] == "replay" and not first_replay:
            first_replay.append(event)
            entered.set()
            assert release.wait(10)
            result = original(event)
            first_written.set()
            return result
        return original(event)

    def failed(event, error):
        failures.append((event, error))
        return original_failed(event, error)

    monkeypatch.setattr(w, "_write", delayed)
    monkeypatch.setattr(w, "failed", failed)
    # Silence the synthetic overflow log volume; logging fault tested separately.
    monkeypatch.setattr(ft.logger, "warning", lambda *a, **kw: None)
    receipt.trace("replay")
    try:
        assert entered.wait(5), "writer did not take the first replay"
        assert w._queue.qsize() == 0, "first replay must be outside the queue"
        for _ in range(w._queue.maxsize + 2048):
            receipt.trace("replay")
        queue_size = w._queue.qsize()
        overlay_size = len(w._failures)
    finally:
        release.set()
        assert w.flush(timeout=15)
    full = [(event, error) for event, error in failures if isinstance(error, queue.Full)]
    slow = [(event, error) for event, error in failures if isinstance(error, TimeoutError)]
    assert len(full) == 2048
    assert all(event["edge"] == "replay" for event, _ in full)
    assert queue_size == w._queue.maxsize
    assert overlay_size <= 1024, "bounded queue diverts unlimited overflow into unbounded dict"
    # A successfully written, deliberately paused replay may also be slow.
    # Account for that diagnostic explicitly rather than calling it queue overflow.
    assert first_written.is_set()
    assert len(slow) in (0, 1)
    assert all(event is first_replay[0] and str(error) == "slow trace write"
               for event, error in slow)
    assert len(failures) == len(full) + len(slow), "unexplained trace failure"
    assert w.report()["rows"][0]["replay_count"] == w._queue.maxsize + 1
    assert w.failure_counts(since=0)["replay"] == 2048 + len(slow)
    print("OVERFLOW_BOUND", {"queue": queue_size, "failures": overlay_size,
                             "queue_full": len(full), "slow_first_replay": len(slow)})


def test_positive_single_executor_other_registry_waits_without_deadlock(tmp_path):
    a = ar.AgentRegistry(str(tmp_path / "a.db"))
    b = ar.AgentRegistry(str(tmp_path / "b.db"))
    entered, release = threading.Event(), threading.Event()
    original = a._fire_trace._write

    def delayed(event):
        entered.set()
        assert release.wait(2)
        return original(event)

    a._fire_trace._write = delayed
    try:
        a.prune_schedule_fire_trace(now=time.time())
        assert entered.wait(1)
        b.prune_schedule_fire_trace(now=time.time())
        # The bounded-slice control below tests fairness; this control pins recovery.
    finally:
        release.set()
        assert a._fire_trace.flush()
        assert b._fire_trace.flush()
        a.close()
        b.close()


def test_positive_failure_uuid_deduplicates_db_overlay_overlap(registry):
    _, receipt = fire(registry)
    w = registry._fire_trace
    w.failed(
        {
            "edge": "observed",
            "at": time.time(),
            "schedule_id": receipt.schedule_id,
            "fired_at": receipt.fired_at,
        },
        RuntimeError("synthetic"),
    )
    saved = dict(w._failures)
    w._persist_failures()
    assert not w._failures
    w._failures.update(saved)  # model the commit-before-pop overlap
    assert w.failure_counts(since=0)["observed"] == 1
    w._persist_failures()
    assert w.failure_counts(since=0)["observed"] == 1


def test_idle_trace_scan_must_not_delay_replay_scheduling(registry, monkeypatch):
    from pinky_daemon.scheduler import AgentScheduler

    fire(registry)
    scheduler = AgentScheduler(registry)
    replay_times = []
    monkeypatch.setattr(
        scheduler, "replay_pending_for_agent", lambda name: replay_times.append(time.monotonic())
    )
    start = time.monotonic()
    scheduler.notify_agent_idle("review-worker")
    assert replay_times[-1] - start < 0.1
    assert registry._fire_trace.flush()
    lock = sqlite3.connect(registry._db_path, timeout=0, check_same_thread=False)
    lock.execute("BEGIN EXCLUSIVE")
    timer = threading.Timer(0.65, lock.rollback)
    timer.start()
    start = time.monotonic()
    try:
        scheduler.notify_agent_idle("review-worker")
    finally:
        timer.join()
        lock.close()
    delay = replay_times[-1] - start
    print("IDLE_TRACE_SCAN", {"replay_scheduling_seconds": delay})
    assert delay < 0.5, "trace-only pending-row scan delayed scheduling the real replay"


def test_trace_storage_has_own_file_and_read_only_ledger(registry):
    writer = registry._fire_trace
    assert writer.path == ft.ScheduleFireTrace.path_for(registry._db_path)
    assert writer.path != registry._db_path
    db = writer._connect()
    try:
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "truncate"
        assert db.execute("PRAGMA busy_timeout").fetchone()[0] == 0
        assert db.execute("PRAGMA mmap_size").fetchone()[0] == 0
        attached = {row[1]: row[2] for row in db.execute("PRAGMA database_list")}
        assert attached["ledger"] == registry._db_path
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            db.execute("UPDATE ledger.pending_schedule_wakes SET attempts=99")
    finally:
        db.close()
    assert not registry._db.execute(
        "SELECT name FROM sqlite_master WHERE name='schedule_fire_trace'"
    ).fetchall()


def test_trace_prune_and_failure_queries_use_indices(registry):
    db = sqlite3.connect(registry._fire_trace.path)
    try:
        queries = [
            (
                "SELECT edge FROM schedule_fire_trace_failures WHERE schedule_id=? AND fired_at=?",
                (1, 1),
            ),
            (
                "SELECT rowid FROM schedule_fire_trace WHERE updated_at<? ORDER BY updated_at LIMIT 500",
                (1,),
            ),
            (
                "SELECT event_id FROM schedule_fire_trace_failures WHERE failed_at<? ORDER BY failed_at LIMIT 500",
                (1,),
            ),
        ]
        for sql, parameters in queries:
            plan = " ".join(
                row[3].upper() for row in db.execute("EXPLAIN QUERY PLAN " + sql, parameters)
            )
            assert "SEARCH" in plan and "TEMP B-TREE" not in plan, plan
    finally:
        db.close()


def test_executor_slices_allow_another_registry_to_progress(registry, tmp_path, monkeypatch):
    _, receipt = fire(registry)
    other = ar.AgentRegistry(str(tmp_path / "other.db"))
    writer = registry._fire_trace
    keep_running = threading.Event()
    keep_running.set()
    entered = threading.Event()
    original = writer._write

    def continuous(event):
        original(event)
        if keep_running.is_set():
            writer.submit(event)
        entered.set()

    monkeypatch.setattr(writer, "_write", continuous)
    receipt.trace("replay")
    assert entered.wait(1)
    try:
        other.prune_schedule_fire_trace(now=time.time())
        assert other._fire_trace.flush(timeout=0.5), "one writer monopolized the shared executor"
    finally:
        keep_running.clear()
        assert writer.flush()
        assert other._fire_trace.flush()
        other.close()


def test_trace_event_contains_failure_reporter_fault(registry, monkeypatch):
    _, receipt = fire(registry)
    writer = registry._fire_trace

    def raising(*args, **kwargs):
        raise OSError("injected instrumentation fault")

    monkeypatch.setattr(writer, "submit", raising)
    monkeypatch.setattr(writer, "failed", raising)
    assert receipt.accept()
    assert registry.get_schedule_wake_by_fire(receipt.schedule_id, receipt.fired_at).accepted_at > 0


def test_idle_notification_without_replay_work_emits_no_edge(registry, monkeypatch):
    from pinky_daemon.scheduler import AgentScheduler

    fire(registry)
    scheduler = AgentScheduler(registry)
    monkeypatch.setattr(scheduler, "replay_pending_for_agent", lambda name: None)
    scheduler.notify_agent_idle("review-worker")
    assert registry._fire_trace.flush()
    assert registry._fire_trace.report()["rows"][0]["replay_count"] == 0


def test_writer_and_reader_open_through_catalog_authority(tmp_path, monkeypatch):
    from pinky_daemon.store_catalog import StoreCatalog

    catalog = StoreCatalog(expected_root=tmp_path)
    calls = []
    original = ft.open_store_connection

    def opened(owner_catalog, name, database, **kwargs):
        calls.append((owner_catalog, name, str(database), kwargs["owner"]))
        return original(owner_catalog, name, database, **kwargs)

    monkeypatch.setattr(ft, "open_store_connection", opened)
    registry = ar.AgentRegistry(str(tmp_path / "catalog.db"), catalog=catalog)
    try:
        writer = registry._fire_trace
        connection = writer._connect()
        connection.close()
        assert writer.report()["rows"] == []
        assert {name for _, name, _, _ in calls} == {
            "schedule_fire_trace",
            "schedule_fire_trace_read",
        }
        assert all(
            c is catalog and path == writer.path and owner == "schedule_fire_trace"
            for c, _, path, owner in calls
        )
        assert catalog.connection_policy("schedule_fire_trace").busy_timeout_ms == 0
        assert catalog.connection_policy("schedule_fire_trace_read").busy_timeout_ms == 1000
    finally:
        registry.close()
        assert catalog.shutdown(deadline_seconds=1).ok


def test_prune_transactions_delete_at_most_500_rows(registry, monkeypatch):
    writer = registry._fire_trace
    old = time.time() - 31 * 86400
    with sqlite3.connect(writer.path) as db:
        db.executemany(
            "INSERT INTO schedule_fire_trace(schedule_id,fired_at,updated_at) VALUES(?,?,?)",
            [(999, old + i, old) for i in range(1501)],
        )
        db.executemany(
            """INSERT INTO schedule_fire_trace_failures
            (event_id,schedule_id,fired_at,fire_id,edge,failed_at,failures,reason)
            VALUES(?,?,?,?,?,?,?,?)""",
            [
                (f"bounded-{i}", 999, old + i, None, "observed", old, 1, "synthetic")
                for i in range(1501)
            ],
        )
    commits = []
    original = writer._connect

    def connect(**kwargs):
        db = original(**kwargs)
        previous = [db.total_changes]

        def trace(sql):
            if sql.strip().upper() == "COMMIT":
                commits.append(db.total_changes - previous[0])
                previous[0] = db.total_changes

        db.set_trace_callback(trace)
        return db

    monkeypatch.setattr(writer, "_connect", connect)
    registry.prune_schedule_fire_trace(now=time.time())
    assert writer.flush()
    assert commits and sum(commits) == 3002
    assert max(commits) <= 500, commits


def test_worker_reporter_failure_cannot_strand_bookkeeping(registry, monkeypatch):
    _, receipt = fire(registry)
    writer = registry._fire_trace
    original_write = writer._write
    original_failed = writer.failed
    raised = threading.Event()

    def broken_write(event):
        raise RuntimeError("injected worker write failure")

    def broken_reporter(event, error):
        raised.set()
        raise RuntimeError("injected diagnostic reporter failure")

    monkeypatch.setattr(writer, "_write", broken_write)
    monkeypatch.setattr(writer, "failed", broken_reporter)
    receipt.trace("replay")
    assert raised.wait(1)
    assert writer.flush(timeout=1), "failed diagnostics stranded queue accounting"
    monkeypatch.setattr(writer, "_write", original_write)
    monkeypatch.setattr(writer, "failed", original_failed)
    receipt.trace("replay")
    assert writer.flush(timeout=1)
    assert writer.report()["rows"][0]["replay_count"] == 1
    deadline = time.monotonic() + 1
    while writer._running and time.monotonic() < deadline:
        threading.Event().wait(0.001)
    assert not writer._running
