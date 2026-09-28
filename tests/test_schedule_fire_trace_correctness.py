"""Independent review regressions; expected invariants deliberately fail on review head."""

import json
import sqlite3
import threading
import time

import pytest
from fastapi.testclient import TestClient

import pinky_daemon.agent_registry as ar
import pinky_daemon.schedule_fire_trace as ft
import pinky_daemon.scheduler as sched
from tests.test_schedule_fire_trace import pane as pane  # existing isolated transport doubles only
from tests.test_schedule_fire_trace import paste


@pytest.fixture
def registry(tmp_path):
    r = ar.AgentRegistry(str(tmp_path / "review.db"))
    r.register("worker", working_dir=str(tmp_path / "worker"))
    yield r
    r.close()


def fire(r):
    s = r.add_schedule("worker", "*/5 * * * *", name="synthetic", prompt="scheduled work")
    p, _ = r.persist_schedule_wake(
        s.id, agent_name="worker", schedule_name=s.name, prompt=s.prompt, fired_at=time.time() - 10
    )
    assert r._fire_trace.flush()
    return p, sched.ScheduleWakeReceipt(r, s.id, p.fired_at)


def record(r):
    assert r._fire_trace.flush()
    (result,) = r._fire_trace.report()["rows"]
    return result


@pytest.fixture
def api(tmp_path):
    import pinky_daemon.api as api_module

    app = api_module.create_api(db_path=str(tmp_path / "api" / "conversations.db"))
    app.state.agents.register("worker", working_dir=str(tmp_path / "worker"))
    client = TestClient(app)
    yield app, client
    client.close()
    app.state.agents.close()


def test_fire_trace_response_is_bounded(api):
    app, client = api
    r = app.state.agents
    # Synthetic retained history; no transport work or production stores.
    with sqlite3.connect(r._fire_trace.path) as db:
        db.executemany(
            "INSERT INTO schedule_fire_trace(schedule_id,fired_at,agent_name) VALUES(?,?,?)",
            [(i + 1, time.time() + i, "worker") for i in range(2001)],
        )
    response = client.get("/scheduler/fire-trace", params={"since": 0, "limit": 100})
    assert response.status_code == 200
    print("RESPONSE_BOUND", len(response.json()["rows"]), len(response.content))
    body = response.json()
    assert len(body["rows"]) == 100
    assert body["counts"]["pending"] == 2001
    second = client.get(
        "/scheduler/fire-trace", params={"since": 0, "limit": 100, "offset": 100}
    ).json()
    assert not {row["schedule_id"] for row in body["rows"]} & {
        row["schedule_id"] for row in second["rows"]
    }
    assert len(client.get("/scheduler/fire-trace").json()["rows"]) == 200
    assert client.get("/scheduler/fire-trace", params={"limit": 1001}).status_code == 422
    assert client.get("/scheduler/fire-trace", params={"offset": -1}).status_code == 422


def test_positive_filters_are_data_and_reject_invalid_typed_id(api):
    app, client = api
    p, _ = fire(app.state.agents)
    assert (
        client.get("/scheduler/fire-trace", params={"agent": "worker' OR 1=1 --"}).json()["rows"]
        == []
    )
    assert (
        client.get(
            "/scheduler/fire-trace",
            params={"outcome": "pending'; DROP TABLE schedule_fire_trace;--"},
        ).json()["rows"]
        == []
    )
    assert (
        client.get("/scheduler/fire-trace", params={"schedule_id": "1 OR 1=1"}).status_code == 422
    )
    assert len(client.get("/scheduler/fire-trace").json()["rows"]) == 1


@pytest.mark.parametrize("transition", ["release", "terminal_abandon"])
def test_failed_transition_after_drain_park_is_incomplete(registry, monkeypatch, transition):
    p, _ = fire(registry)
    assert registry.drain_park_pending_schedule_wake(p.id)
    assert record(registry)["outcome"] == "drain_parked"
    writer = registry._fire_trace
    original = writer._write

    def fail(event):
        if event["edge"] == "abandon":
            raise RuntimeError("synthetic failed transition")
        return original(event)

    monkeypatch.setattr(writer, "_write", fail)
    if transition == "release":
        assert registry.release_drain_parked_schedule_wakes("worker") == 1
    else:
        assert registry.abandon_pending_schedule_wake(p.id)
    result = record(registry)
    failures = writer.failures()
    print("FAILED_TRANSITION", transition, result["outcome"], failures)
    assert writer.failure_counts(since=0)["abandon"] == 1
    assert result["outcome"] == "trace_incomplete"


def test_release_retains_exact_identity_when_ledger_changes_before_worker(registry, monkeypatch):
    p, _ = fire(registry)
    assert registry.drain_park_pending_schedule_wake(p.id)
    record(registry)
    writer = registry._fire_trace
    original = writer._write
    entered, proceed = threading.Event(), threading.Event()

    def paused(event):
        if event.get("release_agent"):
            entered.set()
            assert proceed.wait(5)
        return original(event)

    monkeypatch.setattr(writer, "_write", paused)
    try:
        assert registry.release_drain_parked_schedule_wakes("worker") == 1
        assert entered.wait(2)
        release_at = registry.get_schedule_wake_by_fire(p.schedule_id, p.fired_at).released_at
        # Normal re-park resets released_at, before the asynchronous release query.
        assert registry.drain_park_pending_schedule_wake(p.id)
        later_schedule = registry.add_schedule("worker", "*/5 * * * *", name="later recurrence")
        later, _ = registry.persist_schedule_wake(
            later_schedule.id,
            agent_name="worker",
            schedule_name=later_schedule.name,
            prompt="later work",
            fired_at=time.time(),
        )
        assert registry.drain_park_pending_schedule_wake(later.id)
    finally:
        proceed.set()
    assert writer.flush()
    records = {row["fire_id"]: row for row in writer.report()["rows"]}
    result = records[p.id]
    assert records[later.id]["released_at"] == 0
    print("DELAYED_RELEASE", release_at, result["released_at"], result["drain_parked_at"])
    assert result["released_at"] == release_at


def test_late_accept_flag_survives_cross_thread_event_reordering(registry, monkeypatch):
    p, receipt = fire(registry)
    original = ar.trace_event
    entered, proceed = threading.Event(), threading.Event()
    errors = []

    def reorder(r, edge, **fields):
        if edge == "abandon":
            entered.set()
            assert proceed.wait(5)
        return original(r, edge, **fields)

    monkeypatch.setattr(ar, "trace_event", reorder)

    def abandon():
        try:
            assert registry.abandon_pending_schedule_wake(p.id)
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=abandon)
    thread.start()
    try:
        assert entered.wait(2)
        assert receipt.accept()
        assert registry._fire_trace.flush()
    finally:
        proceed.set()
        thread.join(3)
    assert not errors
    result = record(registry)
    print(
        "LATE_FLAG",
        result["matched_at"],
        result["abandoned_at"],
        result["outcome"],
        result["late_accept_after_abandon"],
    )
    assert result["outcome"] == "late_delivered"
    assert result["late_accept_after_abandon"] == 1


@pytest.mark.parametrize("pane", ["tmux_claude"], indirect=True)
@pytest.mark.parametrize("source", ["old_offset", "wrong_identity", "current"])
async def test_folded_observation_respects_paste_ticket(pane, source):
    _, receipt = fire(pane.registry)
    # A genuine existing folded row, not a fabricated negative byte offset.
    entry = {"type": "user", "message": {"role": "user", "content": "prefix scheduled work suffix"}}
    pane.path.write_text(json.dumps(entry) + "\n")
    turn, delivery = await paste(pane, receipt)
    identity = turn.transcript_file_identity_at_paste
    offset = turn.transcript_offset_at_paste
    assert identity is not None and offset is not None
    assert offset > 0
    pane.session._on_transcript_entry(
        entry,
        entry_offset=0 if source == "old_offset" else offset,
        source_identity=(identity[0], identity[1] + 1) if source == "wrong_identity" else identity,
    )
    result = record(pane.registry)
    print("FOLDED_TICKET", source, result["outcome"], result["user_message_pointer"])
    if source == "current":
        assert delivery.result() is True
        assert result["outcome"] == "delivered"
    else:
        assert not delivery.done()
        assert result["user_message_observed_at"] == 0
        assert result["outcome"] == "producer_no_user_turn"


def test_positive_null_identity_binding_pending_and_late_accept(registry):
    s = registry.add_schedule("worker", "* * * * *", name="fallback", prompt="scheduled work")
    fired_at = time.time() - 10
    ft.trace_event(registry, "enqueue", schedule_id=s.id, fired_at=fired_at, prompt=s.prompt)
    initial = record(registry)
    assert initial["fire_id"] is None and initial["outcome"] == "pending"
    p, _ = registry.persist_schedule_wake(
        s.id, agent_name="worker", schedule_name=s.name, prompt=s.prompt, fired_at=fired_at
    )
    assert record(registry)["fire_id"] == p.id
    assert registry.abandon_pending_schedule_wake(p.id)
    abandoned = record(registry)["abandoned_at"]
    assert registry.confirm_pending_schedule_wake(p.id)
    final = record(registry)
    assert final["abandoned_at"] == abandoned
    assert final["late_accept_after_abandon"] == 1
    assert final["outcome"] == "late_delivered"


@pytest.mark.parametrize("pane", ["tmux_codex"], indirect=True)
@pytest.mark.parametrize("source", ["old_offset", "wrong_identity", "current"])
async def test_rollout_observation_respects_paste_ticket(pane, source, monkeypatch):
    _, receipt = fire(pane.registry)
    entry = {
        "type": "event_msg",
        "payload": {
            "type": "user_message",
            "message": "scheduled work",
        },
    }
    pane.path.write_text(json.dumps(entry) + "\n")
    turn, delivery = await paste(pane, receipt)
    identity = turn.transcript_file_identity_at_paste
    offset = turn.transcript_offset_at_paste
    assert identity is not None and offset > 0
    monkeypatch.setattr(pane.session, "_match_acceptance_turn", lambda *args: None)
    pane.session._tailer.entry_pointer = {
        "path": str(pane.path),
        "offset": 0 if source == "old_offset" else offset,
        "identity": (identity[0], identity[1] + 1) if source == "wrong_identity" else identity,
    }
    pane.session._on_transcript_entry(entry)
    result = record(pane.registry)
    assert not delivery.done()
    assert bool(result["user_message_observed_at"]) == (source == "current")
    assert result["outcome"] == (
        "observer_unmatched" if source == "current" else "producer_no_user_turn"
    )
