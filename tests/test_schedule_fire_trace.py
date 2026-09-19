"""Durable observations must diagnose fires without influencing delivery."""

import asyncio
import hashlib
import json
import os
import sqlite3
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from pinky_daemon.agent_registry import AgentRegistry
from pinky_daemon.codex_tmux_session import CodexTmuxSession
from pinky_daemon.codex_tmux_transcript import CodexTmuxTranscriptTailer
from pinky_daemon.scheduler import AgentScheduler, ScheduleWakeReceipt
from pinky_daemon.streaming_session import StreamingSessionConfig
from pinky_daemon.tmux_session import TmuxCommandResult, TmuxSession, _TmuxControl
from pinky_daemon.tmux_transcript import TmuxTranscriptTailer
from pinky_daemon.transport_state import SessionState


def flush(registry):
    if hasattr(registry, "_fire_trace"):
        assert registry._fire_trace.flush(timeout=5)


def rows(registry):
    flush(registry)
    cursor = registry._db.execute("SELECT * FROM schedule_fire_trace ORDER BY fired_at")
    return [dict(zip([c[0] for c in cursor.description], row)) for row in cursor.fetchall()]


@pytest.fixture
def registry(tmp_path):
    registry = AgentRegistry(str(tmp_path / "registry.db"))
    registry.register("worker", working_dir=str(tmp_path / "worker"))
    yield registry
    registry.close()


def fire(registry, *, age=10, prompt="scheduled work", claim=False, name="recurring"):
    schedule = registry.add_schedule("worker", "*/5 * * * *", name=name, prompt=prompt)
    fired_at = time.time() - age
    if claim:
        claimed, pending = registry.claim_schedule_fire(
            schedule.id, timestamp=fired_at, expected_last_run=schedule.last_run,
            agent_name="worker", schedule_name=schedule.name, prompt=prompt,
        )
        assert claimed
    else:
        pending, _ = registry.persist_schedule_wake(
            schedule.id, agent_name="worker", schedule_name=schedule.name,
            prompt=prompt, fired_at=fired_at,
        )
    flush(registry)
    return pending, ScheduleWakeReceipt(registry, schedule.id, fired_at)


@pytest.fixture(params=["tmux_claude", "tmux_codex"])
async def pane(request, registry, tmp_path, monkeypatch):
    kind = request.param
    registry.update("worker", transport="tmux", runtime=(
        "codex_cli" if kind == "tmux_codex" else "claude_sdk"
    ))
    tmux = MagicMock(spec=_TmuxControl)
    tmux.session_name = "test-trace-pane"
    ok = TmuxCommandResult(returncode=0, stdout="", stderr="")
    tmux.has_session = AsyncMock(return_value=False)
    tmux.kill_session = AsyncMock(return_value=ok)
    tmux.paste_text = AsyncMock(return_value=ok)
    tmux.capture_pane = AsyncMock(return_value=TmuxCommandResult(
        returncode=0, stdout="Ready\nmodel · /tmp/worker\n", stderr="",
    ))
    cls = CodexTmuxSession if kind == "tmux_codex" else TmuxSession
    session = cls(StreamingSessionConfig(agent_name="worker", working_dir=str(tmp_path)),
                  tmux_control=tmux)
    session._state_machine._state = SessionState.CONNECTED
    session._config.live_status_fn = lambda: {"status": "idle", "last_updated": time.time()}
    monkeypatch.setattr(session, "_context_lock_path", lambda: tmp_path / "absent.lock")
    path = tmp_path / "transcript.jsonl"
    path.touch()
    tailer = CodexTmuxTranscriptTailer if kind == "tmux_codex" else TmuxTranscriptTailer
    session._tailer = tailer(path, session._handle_turn_complete,
                             on_entry=session._on_transcript_entry)
    yield SimpleNamespace(session=session, path=path, registry=registry, kind=kind, tmux=tmux)
    await session.disconnect()


async def paste(pane, durable):
    receipt = await pane.session.send_scheduler_prompt("scheduled work", on_accept=durable.accept)
    turn = pane.session._scheduler_pending_turns[-1]
    tasks = list(pane.session._scheduler_delivery_tasks)
    await asyncio.wait_for(asyncio.gather(*tasks), timeout=2)
    assert turn.pane_delivery_recorded
    assert not receipt.done()
    return turn, receipt


async def observe(pane):
    entry = ({"type": "event_msg", "payload": {
        "type": "user_message", "message": "scheduled work",
    }} if pane.kind == "tmux_codex" else {
        "type": "user", "message": {"role": "user", "content": "scheduled work"},
    })
    with pane.path.open("a") as stream:
        stream.write(json.dumps(entry) + "\n")
    await pane.session._tailer.read_once()


@pytest.mark.parametrize("claim", [False, True])
def test_t1_enqueue_identity_and_hash(registry, claim):
    pending, _ = fire(registry, claim=claim)
    record, = rows(registry)
    assert record["fire_id"] == pending.id
    assert record["enqueued_at"] == pending.created_at
    assert record["prompt_hash"] == hashlib.sha256(pending.prompt.encode()).hexdigest()[:12]
    assert record["transport_kind"] == "sdk"
    assert record["outcome"] == "pending"
    registry.persist_schedule_wake(pending.schedule_id, agent_name="worker",
                                   schedule_name="recurring", prompt=pending.prompt,
                                   fired_at=pending.fired_at)
    assert rows(registry) == [record]


async def test_t1_paste_exactly_once(pane):
    _, durable = fire(pane.registry)
    turn, _ = await paste(pane, durable)
    pane.session._finish_turn_delivery(turn)
    record, = rows(pane.registry)
    assert record["paste_at"] > 0
    assert record["paste_attempts"] == 1
    assert record["transport_kind"] == pane.kind
    pointer = json.loads(record["paste_pointer"])
    assert pointer["path"] == str(pane.path)
    assert pointer["offset"] == 0
    assert record["outcome"] == "producer_no_user_turn"


async def test_t1_observer_is_independent_of_matcher(pane, monkeypatch):
    _, durable = fire(pane.registry)
    _, receipt = await paste(pane, durable)
    monkeypatch.setattr(pane.session, "_match_acceptance_content", lambda *a, **k: None)
    monkeypatch.setattr(pane.session, "_folded_acceptance_turns", lambda *a, **k: [])
    await observe(pane)
    record, = rows(pane.registry)
    assert record["user_message_observed_at"] > 0
    pointer = json.loads(record["user_message_pointer"])
    assert pointer["path"] == str(pane.path) and pointer["offset"] == 0
    assert not receipt.done()
    assert record["matched_at"] == 0
    assert record["outcome"] == "observer_unmatched"


async def test_t1_accept_from_real_observer(pane):
    _, durable = fire(pane.registry)
    _, receipt = await paste(pane, durable)
    await observe(pane)
    assert receipt.result() is True
    first, = rows(pane.registry)
    assert first["matched_at"] > 0 and first["receipt_accept_result"] == 1
    assert first["matched_by"] == "transcript_receipt"
    assert first["outcome"] == "delivered"
    assert durable.accept()
    assert rows(pane.registry) == [first]


async def test_t1_watchdog_replay(pane, monkeypatch):
    _, durable = fire(pane.registry)
    turn, receipt = await paste(pane, durable)
    session = pane.session
    monkeypatch.setattr("pinky_daemon.tmux_session._WATCHDOG_TICK_SEC", 0)
    monkeypatch.setattr("pinky_daemon.tmux_session._TURN_DONE_TIMEOUT_SEC", 0)
    monkeypatch.setattr(session, "_inflight_stall_verdict", lambda *a: "wedged")
    session._head_started_at = 0
    session._config.live_status_fn = lambda: {"status": "working", "last_updated": time.time()}
    session._transcript_recently_grew = lambda *a: False
    session._background_tasks_recently_active = lambda *a: False
    session._foreground_tool_in_flight = lambda *a: False
    session._pane_is_animating = AsyncMock(return_value=False)
    restarted = asyncio.Event()

    async def restart(**kwargs):
        restarted.set()
        session._state_machine._state = SessionState.DEAD

    monkeypatch.setattr(session, "force_restart", restart)
    task = asyncio.create_task(session._inflight_watchdog())
    try:
        await asyncio.wait_for(restarted.wait(), timeout=2)
        assert turn.replay_count == 1 and not receipt.done()
        record, = rows(pane.registry)
        assert record["replay_count"] == 1
        assert record["outcome"] == "producer_no_user_turn"
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_t1_sdk_api_delivery(tmp_path):
    from pinky_daemon.api import create_api

    app = create_api(db_path=str(tmp_path / "sdk-api.db"))
    registry = app.state.agents
    registry.register("worker", working_dir=str(tmp_path / "worker"))
    pending, durable = fire(registry)
    session = SimpleNamespace(state=SessionState.CONNECTED, send=AsyncMock(return_value=True),
                              injection_confirms_consumption=True, resume_handle="sdk-session")
    app.state.broker._streaming["worker"] = {"main": session}
    try:
        result = await app.state.scheduler._wake_callback(
            "worker", "worker-main", pending.prompt, schedule_receipt=durable,
        )
        assert result is True
        record, = rows(registry)
        assert record["paste_at"] > 0 and record["paste_attempts"] == 1
        assert record["matched_by"] == "on_accept" and record["receipt_accept_result"] == 1
        assert record["transport_kind"] == "sdk" and record["outcome"] == "delivered"
    finally:
        registry.close()


@pytest.mark.parametrize("edge", ["abandon", "drain_park", "release", "reaper", "idle_replay"])
def test_t1_ledger_and_idle_edges(registry, monkeypatch, edge):
    pending, _ = fire(registry, age=100)
    if edge == "abandon":
        registry.abandon_pending_schedule_wake(pending.id, reason="RECEIPT_ABANDONED: wall-clock")
    elif edge in {"drain_park", "release"}:
        registry.drain_park_pending_schedule_wake(pending.id)
        if edge == "release":
            flush(registry)
            assert registry.release_drain_parked_schedule_wakes("worker") == 1
    elif edge == "reaper":
        reap(registry, time.time(), abandon_after=50)
    else:
        scheduler = AgentScheduler(registry)
        monkeypatch.setattr(scheduler, "replay_pending_for_agent", lambda name: None)
        scheduler.notify_agent_idle("worker")
    record, = rows(registry)
    if edge == "idle_replay":
        assert record["replay_count"] == 1
        assert record["outcome"] == "pending"
    else:
        assert record["abandoned_at"] > 0
        assert record["abandon_reason"] == {
            "abandon": "RECEIPT_ABANDONED: wall-clock", "drain_park": "drain_parked",
            "release": "released", "reaper": "reaper",
        }[edge]
        assert record["outcome"] == ("drain_parked" if edge == "drain_park" else "never_pasted")


async def test_t1_textless_codex_completion_remains_unaccepted(pane, monkeypatch):
    monkeypatch.setattr("pinky_daemon.codex_tmux_session._CODEX_IDLE_CONFIRM_SEC", 0)
    _, durable = fire(pane.registry)
    _, receipt = await paste(pane, durable)
    with pane.path.open("a") as stream:
        for payload in [{"type": "task_started", "turn_id": "task"},
                        {"type": "task_complete", "turn_id": "task", "last_agent_message": "done"}]:
            stream.write(json.dumps({"type": "event_msg", "payload": payload}) + "\n")
    await pane.session._tailer.read_once()
    assert not receipt.done()
    record, = rows(pane.registry)
    assert record["outcome"] == "producer_no_user_turn"
    assert record["user_message_observed_at"] == record["matched_at"] == 0


def test_t2_late_accept_keeps_abandonment(registry):
    pending, durable = fire(registry, age=1800)
    abandoned_at = time.time() - 1
    registry.abandon_pending_schedule_wake(pending.id, abandoned_at=abandoned_at)
    flush(registry)
    assert durable.accept()
    flush(registry)
    assert durable.accept()
    record, = rows(registry)
    assert record["abandoned_at"] == abandoned_at
    assert record["late_accept_after_abandon"] == 1
    assert record["outcome"] == "late_delivered"


def reap(registry, now, *, abandon_after=10000000):
    return registry.reap_pending_schedule_wakes(
        now=now, abandon_after=abandon_after, retain_accepted=10,
        retain_abandoned=10, retain_parked=10, payload_trim_after=10,
    )


def test_t3_independent_retention_and_restart(registry):
    pending, durable = fire(registry, age=32 * 86400)
    assert durable.accept()
    flush(registry)
    reap(registry, time.time() + 20)
    assert registry.get_schedule_wake_by_fire(pending.schedule_id, pending.fired_at) is None
    assert len(rows(registry)) == 1
    registry.prune_schedule_fire_trace(now=time.time(), retention_days=1)
    flush(registry)
    assert len(rows(registry)) == 1, "retain evidence for 30 days after its latest event"
    registry.prune_schedule_fire_trace(now=time.time() + 31 * 86400, retention_days=1)
    flush(registry)
    assert rows(registry) == []


@pytest.mark.parametrize("evidence,ledger,expected", [
    ({}, {}, "pending"),
    ({"abandoned_at": 105}, {}, "never_pasted"),
    ({"drain_parked_at": 105}, {}, "drain_parked"),
    ({"drain_parked_at": 105, "released_at": 110}, {}, "never_pasted"),
    ({"paste_at": 105}, {}, "producer_no_user_turn"),
    ({"paste_at": 105, "user_message_observed_at": 106}, {}, "observer_unmatched"),
    ({"matched_at": 110, "receipt_accept_result": 1}, {}, "delivered"),
    ({"matched_at": 1001, "receipt_accept_result": 1}, {}, "late_delivered"),
    ({"matched_at": 161, "cadence_seconds": 60, "receipt_accept_result": 1}, {}, "late_delivered"),
    ({"matched_at": 160, "cadence_seconds": 60, "receipt_accept_result": 1}, {}, "delivered"),
    ({"matched_at": 110, "abandoned_at": 105, "receipt_accept_result": 1}, {}, "late_delivered"),
    ({}, {"attempts": 1}, "trace_incomplete"),
    ({"paste_at": 105}, {"accepted_at": 110}, "trace_incomplete"),
    ({"user_message_observed_at": 106}, {"accepted_at": 110}, "trace_incomplete"),
])
@pytest.mark.parametrize("replay_count", [0, 3])
def test_t4_outcome_truth_table(evidence, ledger, expected, replay_count):
    from pinky_daemon.schedule_fire_trace import derive_outcome

    assert derive_outcome({"fired_at": 100, "replay_count": replay_count, **evidence}, ledger) == expected


@pytest.mark.parametrize("fault", ["raise", "slow", "busy", "busy_transient"])
@pytest.mark.parametrize("edge", ["paste", "accept"])
async def test_t5_trace_writer_cannot_block_receipt(pane, monkeypatch, caplog, fault, edge):
    pending, durable = fire(pane.registry)
    flush(pane.registry)
    writer = pane.registry._fire_trace
    connection = writer._connect()
    try:
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 0
    finally:
        connection.close()
    original = writer._write
    entered = threading.Event()
    release = threading.Event()
    lock = None

    def faulty(event):
        if event["edge"] == edge:
            entered.set()
            if fault == "raise":
                raise RuntimeError("injected trace error")
            if fault == "slow":
                time.sleep(2)
            if fault in {"busy", "busy_transient"}:
                assert release.wait(3)
        return original(event)

    monkeypatch.setattr(writer, "_write", faulty)
    if edge == "paste":
        start = time.perf_counter()
        _, receipt = await paste(pane, durable)
        elapsed = time.perf_counter() - start
    else:
        _, receipt = await paste(pane, durable)
        flush(pane.registry)
        start = time.perf_counter()
        await observe(pane)
        elapsed = time.perf_counter() - start
        assert receipt.result() is True
    assert elapsed < 0.5, "trace IO added wake-path latency"
    assert await asyncio.to_thread(entered.wait, 1)
    timer = None
    if fault in {"busy", "busy_transient"}:
        lock = sqlite3.connect(pane.registry._db_path, timeout=0, check_same_thread=False)
        lock.execute("BEGIN IMMEDIATE")
        if fault == "busy_transient":
            timer = threading.Timer(0.1, lock.rollback)
            timer.start()
        release.set()
    try:
        busy_start = time.perf_counter()
        if fault in {"busy", "busy_transient"}:
            assert writer.flush(timeout=0.8), "trace writer waited for the registry lock"
            assert time.perf_counter() - busy_start < 1
        else:
            flush(pane.registry)
    finally:
        if timer:
            timer.join()
        if lock:
            lock.rollback()
            lock.close()
    failures = writer.failure_counts(since=time.time() - 86400)
    assert failures[edge] == (0 if fault == "busy_transient" else 1)
    errors = [r for r in caplog.records if "schedule fire trace" in r.message.lower()]
    assert len(errors) == (0 if fault == "busy_transient" else 1)
    if fault == "busy_transient":
        record, = rows(pane.registry)
        assert record["paste_at" if edge == "paste" else "matched_at"] > 0
    if edge == "accept":
        assert pane.registry.get_schedule_wake_by_fire(
            pending.schedule_id, pending.fired_at,
        ).accepted_at > 0


def test_t6_api_filters_counts_and_auth(tmp_path):
    from pinky_daemon.api import create_api

    app = create_api(db_path=str(tmp_path / "api.db"))
    registry = app.state.agents
    registry.register("worker", working_dir=str(tmp_path / "worker"))
    pending, durable = fire(registry)
    durable.accept()
    flush(registry)
    from pinky_daemon.auth import SESSION_COOKIE_NAME, create_session_cookie

    client = TestClient(app)
    cookie = create_session_cookie(os.environ["PINKY_SESSION_SECRET"], user="admin")
    client.cookies.set(SESSION_COOKIE_NAME, cookie)
    assert client.cookies.get(SESSION_COOKIE_NAME) == cookie
    try:
        response = client.get("/scheduler/fire-trace", params={
            "since": time.time() - 86400, "agent": "worker", "schedule_id": pending.schedule_id,
            "outcome": "delivered",
        })
        assert response.status_code == 200
        body = response.json()
        assert [r["fire_id"] for r in body["rows"]] == [pending.id]
        assert body["per_agent"]["worker"]["delivered"] == 1
        assert body["per_schedule"][str(pending.schedule_id)]["delivered"] == 1
        for params in [{"agent": "missing"}, {"schedule_id": -1},
                       {"outcome": "never_pasted"}, {"since": time.time() + 1}]:
            assert client.get("/scheduler/fire-trace", params=params).json()["rows"] == []
        waiting, _ = fire(registry, name="waiting recurrence")
        status = client.get("/scheduler/status").json()
        assert status["fire_trace_24h"]["pending"] == 1
        assert status["fire_trace_24h"]["never_pasted"] == 0
        pending_rows = client.get("/scheduler/fire-trace", params={"outcome": "pending"}).json()
        assert [r["fire_id"] for r in pending_rows["rows"]] == [waiting.id]
        assert client.get("/scheduler/fire-trace", params={"outcome": "never_pasted"}).json()["rows"] == []
        assert status["fire_trace_24h"]["delivered"] == 1
        assert status["fire_trace_24h"]["trace_incomplete"] == 0
        assert sum(status["trace_write_failures_24h"].values()) == 0
        client.cookies.clear()
        assert SESSION_COOKIE_NAME not in client.cookies
        assert client.get("/scheduler/fire-trace").status_code == 401
    finally:
        client.close()
        registry.close()


async def test_t5_queue_full_preserves_authoritative_acceptance(registry, monkeypatch, caplog):
    import queue

    pending, durable = fire(registry)
    writer = registry._fire_trace
    monkeypatch.setattr(writer, "_queue", queue.Queue(maxsize=1))
    entered, release = threading.Event(), threading.Event()
    original = writer._write

    def blocked(event):
        entered.set()
        assert release.wait(2)
        return original(event)

    monkeypatch.setattr(writer, "_write", blocked)
    durable.trace("replay")
    assert await asyncio.to_thread(entered.wait, 1)
    try:
        durable.trace("replay")
        started = time.perf_counter()
        assert durable.accept()
        assert time.perf_counter() - started < 0.5
        assert registry.get_schedule_wake_by_fire(
            pending.schedule_id, pending.fired_at,
        ).accepted_at > 0
    finally:
        release.set()
        flush(registry)
    assert writer.failure_counts(since=0)["accept"] == 1
    errors = [r for r in caplog.records if "schedule fire trace" in r.message.lower()]
    assert len(errors) == 1 and "Full" in errors[0].message
    assert writer.report()["rows"][0]["outcome"] == "trace_incomplete"
    assert rows(registry)[0]["replay_count"] == 2


def test_t3_additive_migration_reopen_and_identity_fallback(tmp_path):
    from pinky_daemon.schedule_fire_trace import trace_event

    path = str(tmp_path / "migration.db")
    with sqlite3.connect(path) as db:
        db.execute("""CREATE TABLE schedule_fire_trace (
            schedule_id INTEGER NOT NULL, fired_at REAL NOT NULL,
            PRIMARY KEY(schedule_id,fired_at))""")
    registry = AgentRegistry(path)
    try:
        assert registry._db.execute("PRAGMA journal_mode").fetchone()[0] == "truncate"
        registry.register("worker", working_dir=str(tmp_path / "worker"))
        schedule = registry.add_schedule("worker", "*/5 * * * *", name="recurring")
        fired_at = time.time()
        trace_event(registry, "enqueue", schedule_id=schedule.id, fired_at=fired_at,
                    agent_name="worker", schedule_name="recurring", prompt="work")
        before, = rows(registry)
        assert before["fire_id"] is None, "an unbound fire must not allocate an unrelated id"
        pending, _ = registry.persist_schedule_wake(
            schedule.id, fired_at=fired_at, agent_name="worker", schedule_name="recurring",
            prompt="work",
        )
        after, = rows(registry)
        assert after["fire_id"] == pending.id
        assert after["enqueued_at"] == before["enqueued_at"]
    finally:
        registry.close()
    reopened = AgentRegistry(path)
    try:
        assert rows(reopened) == [after]
    finally:
        reopened.close()


def test_t4_read_api_cross_checks_authoritative_ledger(tmp_path):
    from pinky_daemon.api import create_api

    app = create_api(db_path=str(tmp_path / "cross-check.db"))
    registry = app.state.agents
    registry.register("worker", working_dir=str(tmp_path / "worker"))
    attempted, _ = fire(registry)
    accepted, _ = fire(registry, name="accepted recurrence")
    registry.increment_pending_schedule_wake_attempts(attempted.id)
    # Simulate missing observations while retaining real authoritative evidence.
    registry._db.execute("UPDATE pending_schedule_wakes SET accepted_at=? WHERE id=?",
                         (time.time(), accepted.id))
    registry._db.commit()
    client = TestClient(app)
    try:
        response = client.get("/scheduler/fire-trace", params={"outcome": "trace_incomplete"})
        assert response.status_code == 200
        assert {r["fire_id"] for r in response.json()["rows"]} == {attempted.id, accepted.id}
        counts = client.get("/scheduler/status").json()["fire_trace_24h"]
        assert counts["trace_incomplete"] == 2
        assert counts["never_pasted"] == counts["observer_unmatched"] == counts["pending"] == 0
    finally:
        client.close()
        registry.close()


def test_t5_non_busy_error_has_no_retry(registry, monkeypatch, caplog):
    _, durable = fire(registry)
    writer = registry._fire_trace
    calls = []

    def invalid_sql(event):
        calls.append(event)
        raise sqlite3.OperationalError("no such table: injected_missing_table")

    monkeypatch.setattr(writer, "_write", invalid_sql)
    started = time.perf_counter()
    durable.trace("paste", pointer="{}")
    flush(registry)
    assert time.perf_counter() - started < 0.5
    assert len(calls) == 1
    assert writer.failure_counts(since=0)["paste"] == 1
    assert len([r for r in caplog.records if "schedule fire trace" in r.message.lower()]) == 1


async def test_t1_sdk_pointer_uses_session_submission_sequence(tmp_path):
    from pinky_daemon.api import create_api

    app = create_api(db_path=str(tmp_path / "sdk-pointers.db"))
    registry = app.state.agents
    registry.register("worker", working_dir=str(tmp_path / "worker"))
    session = SimpleNamespace(state=SessionState.CONNECTED, send=AsyncMock(return_value=True),
                              injection_confirms_consumption=True, resume_handle="sdk-session")
    app.state.broker._streaming["worker"] = {"main": session}
    pointers = []
    try:
        for name in ("first recurrence", "second recurrence"):
            pending, durable = fire(registry, name=name)
            assert await app.state.scheduler._wake_callback(
                "worker", "worker-main", pending.prompt, schedule_receipt=durable,
            )
            records = rows(registry)
            record = next(r for r in records if r["fire_id"] == pending.id)
            pointers.append(json.loads(record["paste_pointer"]))
        assert [p["resume_handle"] for p in pointers] == ["sdk-session", "sdk-session"]
        assert all(p["message_id"] is None for p in pointers)
        assert 0 < pointers[0]["submit_seq"] < pointers[1]["submit_seq"]
    finally:
        registry.close()
