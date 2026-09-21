"""Current rollout user rows retain exact scheduler receipt authority."""

import asyncio
import copy
import json
from pathlib import Path

import pytest

from pinky_daemon.scheduler import AgentScheduler
from pinky_daemon.tmux_session import _PLACEHOLDER_TRANSCRIPT_PATH
from tests.test_codex_scheduler_idle_receipt import (
    NOW,
    _drain_delivery,
    _paste,
    _read,
    _schedule_fire,
)
from tests.test_codex_scheduler_idle_receipt import harness as harness
from tests.test_schedule_fire_trace import rows

FIXTURES = Path(__file__).parent / "fixtures" / "codex_user_receipts"


def fixture(version="0.155.1"):
    return [json.loads(line) for line in (FIXTURES / f"{version}.jsonl").read_text().splitlines()]


def user_row(prompt="scheduled work"):
    row = next(row for row in fixture() if row["type"] == "response_item")
    row["payload"]["content"][0]["text"] = prompt
    return row


def append(harness, *entries):
    with harness.rollout.open("a") as stream:
        for entry in entries:
            stream.write(json.dumps(entry) + "\n")


@pytest.mark.parametrize("version", ["0.155.1", "0.144.0"])
async def test_witnessed_user_row_accepts_before_completion(harness, version):
    schedule, pending, durable = _schedule_fire(harness)
    turn, receipt = await _paste(harness, on_accept=durable.accept)
    for entry in fixture(version):
        if entry["payload"].get("type") == "task_complete":
            break
        append(harness, entry)
    await _read(harness)
    assert receipt.done() and receipt.result() is True, "user row must resolve the receipt"
    assert not harness.session.scheduler_wake_inflight(turn.prompt)
    record, = rows(harness.registry)
    assert record["user_message_observed_at"] > 0
    assert record["matched_at"] > 0
    assert harness.registry.get_schedule_wake_by_fire(schedule.id, pending.fired_at).accepted_at > 0
    harness.tmux.capture_pane.assert_not_awaited()


async def test_current_user_row_releases_first_boundary_replay(harness):
    schedule, older, durable = _schedule_fire(harness, age=3601)
    turn, receipt = await _paste(harness, on_accept=durable.accept)
    submitted = []

    async def deliver(agent_name, session_id, prompt, *, schedule_receipt):
        submitted.append(prompt)
        next_receipt = await harness.session.send_scheduler_prompt(
            prompt, on_accept=schedule_receipt.accept,
        )
        await _drain_delivery(harness)
        append(harness, user_row(prompt))
        await _read(harness)
        return next_receipt

    scheduler = AgentScheduler(
        harness.registry, wake_callback=deliver,
        delivery_inflight_fn=lambda _name, prompt: harness.session.scheduler_wake_inflight(prompt),
    )
    harness.schedulers.append(scheduler)
    await scheduler._replay_pending_locked("worker")
    assert harness.registry.get_schedule_wake_by_fire(schedule.id, older.fired_at).abandoned_at > 0
    newer, _ = harness.registry.persist_schedule_wake(
        schedule.id, agent_name="worker", schedule_name=schedule.name,
        prompt="newer distinct work", fired_at=NOW - 1,
    )
    await scheduler._replay_pending_locked("worker")
    assert submitted == [] and not receipt.done()
    notifications = []

    def idle(name):
        notifications.append(name)
        scheduler.notify_agent_idle(name)

    harness.session._config.on_turn_idle = idle
    append(harness, *fixture())
    await _read(harness)
    assert notifications == ["worker"]
    tasks = list(scheduler._pending_replay_tasks.values())
    assert tasks
    await asyncio.wait_for(asyncio.gather(*tasks), timeout=5)
    assert submitted == ["newer distinct work"], "first idle boundary must release the newer fire"
    assert not harness.session.scheduler_wake_inflight(turn.prompt)
    assert harness.registry.get_schedule_wake_by_fire(schedule.id, newer.fired_at).accepted_at > 0
    assert [call.args[0] for call in harness.tmux.paste_text.await_args_list] == [
        "scheduled work", "newer distinct work",
    ]
    assert notifications == ["worker"]


@pytest.mark.parametrize("shape", [
    "pre_ticket", "other_source", "missing", "cold_start", "placeholder", "unbound", "no_pointer",
])
async def test_current_user_row_requires_paste_provenance(harness, shape, capsys):
    if shape == "pre_ticket":
        append(harness, user_row(), user_row())
    turn, receipt = await _paste(harness)
    if shape == "other_source":
        replacement = harness.rollout.with_suffix(".new")
        replacement.write_text("")
        replacement.replace(harness.rollout)
    elif shape in {"missing", "cold_start", "placeholder", "unbound"}:
        turn.transcript_file_identity_at_paste = None
        turn.transcript_offset_at_paste = 0 if shape in {"cold_start", "placeholder"} else None
    if shape == "placeholder":
        turn.transcript_path_at_paste = _PLACEHOLDER_TRANSCRIPT_PATH
    if shape == "unbound":
        turn.transcript_path_at_paste = None
    if shape == "no_pointer":
        harness.session._tailer.entry_pointer = None
        harness.session._on_transcript_entry(user_row())
        harness.session._on_transcript_entry(user_row())
    else:
        if shape != "pre_ticket":
            append(harness, user_row(), user_row())
        await _read(harness)
    assert not receipt.done(), "a user row without matching paste provenance must be rejected"
    assert not turn.transport_accepted
    assert harness.session.scheduler_wake_inflight(turn.prompt)
    warnings = [line for line in capsys.readouterr().err.splitlines()
                if "WARNING codex user-row ticket" in line]
    assert len(warnings) == 1, "repeated rejected rows must warn once per turn and shape"
    expected = {"missing": "inaccessible", "cold_start": "cold-start",
                "placeholder": "cold-start", "unbound": "unbound",
                "no_pointer": "no_pointer"}.get(shape, "mismatch")
    assert f"turn_id={id(turn)}" in warnings[0]
    assert f"shape={expected}" in warnings[0]
    reason = {"cold-start": "cold_start_ticket_unverified", "unbound": "no paste ticket",
              "inaccessible": "ticket identity/offset missing"}.get(expected, "paste ticket mismatch")
    assert f"reason={reason!r}" in warnings[0]
    if expected == "cold-start":
        detail = "placeholder" if shape == "placeholder" else "file_missing"
        assert f"cold_start_reason={detail!r}" in warnings[0]
    for field in ("entry_offset=", "source_identity=", "ticket_offset=", "ticket_identity="):
        assert field in warnings[0]


@pytest.mark.parametrize("change", ["prompt", "assistant", "developer", "no_user_row"])
async def test_nonmatching_current_rows_cannot_accept(harness, change, capsys):
    turn, receipt = await _paste(harness)
    entries = fixture()
    entry = next(row for row in entries if row["type"] == "response_item")
    if change == "prompt":
        entry["payload"]["content"][0]["text"] = "other work"
    elif change == "no_user_row":
        entries.remove(entry)
    else:
        entry["payload"]["role"] = change
    append(harness, *entries)
    await _read(harness)
    assert not receipt.done()
    assert not turn.transport_accepted
    assert "WARNING codex user-row ticket" not in capsys.readouterr().err


async def test_multiple_input_text_items_preserve_order(harness):
    _, receipt = await _paste(harness)
    entry = user_row()
    entry["payload"]["content"] = [
        {"type": "input_text", "text": "scheduled "},
        {"type": "input_image", "image_url": "fixture-image"},
        {"type": "input_text", "text": "work"},
    ]
    append(harness, entry)
    await _read(harness)
    assert receipt.done() and receipt.result() is True


@pytest.mark.parametrize("content", [None, "scheduled work", [None], [{"type": "input_text", "text": 1}]])
async def test_malformed_content_rejects_and_warns_once(harness, content, capsys):
    _, receipt = await _paste(harness)
    entry = user_row()
    entry["payload"]["content"] = content
    append(harness, entry, copy.deepcopy(entry))
    await _read(harness)
    assert not receipt.done()
    assert capsys.readouterr().err.count("malformed Codex user-row content") == 1


async def test_observation_remains_independent_of_acceptance_match(harness, monkeypatch):
    _, _, durable = _schedule_fire(harness)
    _, receipt = await _paste(harness, on_accept=durable.accept)
    monkeypatch.setattr(harness.session, "_match_acceptance_turn", lambda _: None)
    append(harness, user_row())
    await _read(harness)
    record, = rows(harness.registry)
    assert record["user_message_observed_at"] > 0
    assert not receipt.done() and record["matched_at"] == 0


@pytest.mark.parametrize("content", [[], [{"type": "input_image"}], [{"type": "input_text", "text": ""}]])
async def test_content_without_text_does_not_accept(harness, content):
    _, receipt = await _paste(harness)
    entry = user_row()
    entry["payload"]["content"] = content
    append(harness, entry)
    await _read(harness)
    assert not receipt.done()


async def test_user_row_and_close_during_paste_reserve_metadata_once(harness):
    _, _, durable = _schedule_fire(harness)
    result = harness.tmux.paste_text.return_value
    receipts = []

    async def paste_in_progress(*args, **kwargs):
        turn = harness.session._scheduler_pending_turns[-1]
        assert not turn.pane_delivery_recorded
        append(harness, *fixture())
        await _read(harness)
        receipts.append(turn.scheduler_delivery.done())
        assert not harness.session._inflight_metas
        return result

    harness.tmux.paste_text.side_effect = paste_in_progress
    receipt = await harness.session.send_scheduler_prompt("scheduled work", on_accept=durable.accept)
    await _drain_delivery(harness)
    assert receipts == [True]
    assert receipt.done() and receipt.result() is True
    assert not harness.session._inflight_metas


async def test_unknown_text_item_warns_once_without_accepting(harness, capsys):
    _, receipt = await _paste(harness)
    entry = user_row()
    entry["payload"]["content"] = [{"type": "text", "text": "scheduled work"}]
    append(harness, entry, copy.deepcopy(entry))
    await _read(harness)
    assert not receipt.done()
    warning = "WARNING Codex user-row content yielded no text; item types=['text']"
    assert capsys.readouterr().err.count(warning) == 1


@pytest.mark.parametrize("ticket", ["missing", "pre_ticket"])
async def test_legacy_receipt_remains_ticket_free(harness, ticket):
    entry = fixture("0.144.0")[-1]
    if ticket == "pre_ticket":
        append(harness, entry)
    turn, receipt = await _paste(harness)
    if ticket == "missing":
        turn.transcript_file_identity_at_paste = None
        turn.transcript_offset_at_paste = None
        append(harness, entry)
    await _read(harness)
    assert receipt.done() and receipt.result() is True
    assert turn.transport_accepted


@pytest.mark.parametrize("payload", [None, "str"])
async def test_non_dict_response_payload_is_ignored(harness, payload):
    _, receipt = await _paste(harness)
    harness.session._on_transcript_entry({"type": "response_item", "payload": payload})
    assert not receipt.done()
