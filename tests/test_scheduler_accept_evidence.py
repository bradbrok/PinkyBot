"""Acceptance diagnostics retain the first exact evidence without changing receipts."""

import json
import sqlite3
import time

import pytest

from pinky_daemon.agent_registry import AgentRegistry
from pinky_daemon.schedule_fire_trace import ScheduleFireTrace
from pinky_daemon.tmux_session import _SchedulerDeliveryCancelled, _UnreceiptedSchedulerPaste
from tests.test_schedule_fire_trace import (
    fire,
    observe,
    paste,
    rows,
)
from tests.test_schedule_fire_trace import (
    pane as pane,
)
from tests.test_schedule_fire_trace import (
    registry as registry,
)
from tests.test_tmux_fold_consumption_acceptance import _folded_user_entry
from tests.test_tmux_fold_remove_consumption import (
    _emit_fold_chain,
    _enable_fast_idle_reconcile,
    _run_until_reconciled,
)


async def emit(pane, entry):
    with pane.path.open("a") as stream:
        stream.write(json.dumps(entry) + "\n")
    await pane.session._tailer.read_once()


@pytest.mark.parametrize("pane", ["tmux_claude"], indirect=True)
@pytest.mark.parametrize("kind", [
    "user_row", "queue_dequeue", "mid_turn_fold", "folded_user_row",
    "idle_phantom_probe", "pending_paste_probe", "redelivery_recheck",
])
async def test_transport_accept_trace_and_receipt_log_name_evidence(
    pane, monkeypatch, capsys, kind,
):
    pending, durable = fire(pane.registry)
    turn, receipt = await paste(pane, durable)
    assert turn.scheduler_accept == durable.accept
    if kind == "user_row":
        await observe(pane)
    elif kind == "queue_dequeue":
        await emit(pane, {
            "type": "queue-operation", "operation": "enqueue", "content": pending.prompt,
        })
        await emit(pane, {"type": "queue-operation", "operation": "dequeue"})
    elif kind == "mid_turn_fold":
        _emit_fold_chain(pane.session, pane.path, pending.prompt, attachment_first=False)
    elif kind == "folded_user_row":
        await emit(pane, _folded_user_entry(pending.prompt))
    else:
        with pane.path.open("a") as stream:
            stream.write(json.dumps({"type": "user", "message": {
                "role": "user", "content": pending.prompt,
            }}) + "\n")
        if kind == "idle_phantom_probe":
            _enable_fast_idle_reconcile(monkeypatch, pane.session)
            await _run_until_reconciled(pane.session)
        else:
            meta = pane.session._inflight_metas[0]
            held = _UnreceiptedSchedulerPaste(meta, time.time())
            if kind == "pending_paste_probe":
                pane.session._inflight_metas.remove(meta)
                pane.session._head_started_at = None
                pane.session._unreceipted_scheduler_pastes.append(held)
                pane.session._settle_unreceipted_scheduler_pastes()
            else:
                turn.scheduler_redelivery_paste = held
                monkeypatch.setattr(pane.session, "_scheduler_pane_busy", lambda *args: False)
                with pytest.raises(_SchedulerDeliveryCancelled):
                    await pane.session._deliver_turn(turn)
    assert receipt.done() and receipt.result() is True
    record, = rows(pane.registry)
    assert record.get("accept_evidence") == kind
    assert record["matched_by"] == "transcript_receipt"
    assert record["receipt_accept_result"] == 1
    logs = [line for line in capsys.readouterr().err.splitlines()
            if "SCHEDULE_WAKE_RECEIPTED" in line]
    assert len(logs) == 1 and logs[0].endswith(", evidence=" + kind)
    assert pane.registry.get_schedule_wake_by_fire(
        pending.schedule_id, pending.fired_at,
    ).accepted_at > 0
    assert pane.registry._fire_trace.report()["rows"][0]["accept_evidence"] == kind


async def test_user_row_evidence_preserves_other_transport_defaults(pane):
    _, durable = fire(pane.registry)
    _, receipt = await paste(pane, durable)
    await observe(pane)
    assert receipt.result() is True
    record, = rows(pane.registry)
    assert record.get("accept_evidence") == ("user_row" if pane.kind == "tmux_claude" else "")


def test_first_accept_evidence_survives_duplicate_accept(registry, capsys):
    _, durable = fire(registry)
    durable.trace("accept_source", matched_by="transcript_receipt", evidence="user_row")
    assert durable.accept()
    first, = rows(registry)
    durable.trace("accept_source", matched_by="transcript_receipt", evidence="mid_turn_fold")
    assert durable.accept()
    assert rows(registry) == [first]
    assert first.get("accept_evidence") == "user_row"
    logs = [line for line in capsys.readouterr().err.splitlines()
            if "SCHEDULE_WAKE_RECEIPTED" in line]
    assert len(logs) == 1 and logs[0].endswith(", evidence=user_row")


def test_no_argument_accept_keeps_empty_evidence(registry, capsys):
    _, durable = fire(registry)
    assert durable.accept()
    record, = rows(registry)
    assert record.get("accept_evidence") == ""
    assert record["matched_by"] == "on_accept"
    assert ", evidence=" not in capsys.readouterr().err


def test_old_trace_upgrade_retains_existing_rows(tmp_path):
    path = str(tmp_path / "upgrade.db")
    with sqlite3.connect(ScheduleFireTrace.path_for(path)) as db:
        db.execute("""CREATE TABLE schedule_fire_trace (
            schedule_id INTEGER NOT NULL, fired_at REAL NOT NULL,
            matched_by TEXT NOT NULL DEFAULT '', notes TEXT NOT NULL DEFAULT '',
            PRIMARY KEY(schedule_id, fired_at))""")
        db.execute("INSERT INTO schedule_fire_trace VALUES(7, 10, 'previous', 'unchanged')")
    registry = AgentRegistry(path)
    try:
        record, = rows(registry)
        assert record.get("accept_evidence") == ""
        assert record["schedule_id"] == 7 and record["fired_at"] == 10
        assert record["matched_by"] == "previous" and record["notes"] == "unchanged"
    finally:
        registry.close()
    reopened = AgentRegistry(path)
    try:
        assert rows(reopened) == [record]
    finally:
        reopened.close()
