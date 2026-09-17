"""Independent strict-decoding recovery and late-receipt checks."""

import json

import pytest

from tests.test_codex_scheduler_idle_receipt import _append, _paste, _read
from tests.test_codex_scheduler_idle_receipt import harness as harness


@pytest.mark.asyncio
@pytest.mark.parametrize("corrupt_position", ["start", "middle", "close"])
async def test_corrupt_occurrence_cannot_certify_but_next_valid_one_recovers(
    harness, corrupt_position,
):
    turn, receipt = await _paste(harness)
    tailer = harness.session._tailer
    original = tailer._on_turn_complete
    certificates = []

    async def capture(response):
        certificates.append(tailer.completion)
        await original(response)

    tailer._on_turn_complete = capture
    for kind in ("task_started", "agent_message", "task_complete"):
        record = json.dumps({"type": "event_msg", "payload": {
            "type": kind, "turn_id": "bad", "message": "answer",
            "last_agent_message": "answer",
        }}).encode() + b"\n"
        if kind == {
            "start": "task_started", "middle": "agent_message",
            "close": "task_complete",
        }[corrupt_position]:
            record = record.replace(b"bad", b"bad\xff")
        with harness.rollout.open("ab") as stream:
            stream.write(record)
        await _read(harness)
    assert all(certificate is None for certificate in certificates)
    assert not receipt.done()
    assert not turn.transport_accepted
    assert tailer.stats["parse_errors"] == 1

    # Ordinary close handling must drain even a tainted proof occurrence.
    certificates.clear()
    _append(harness, {"type": "task_started", "turn_id": "valid-\ufffd"})
    await _read(harness)
    _append(harness, {"type": "task_complete", "turn_id": "valid-\ufffd",
                      "last_agent_message": "done"})
    await _read(harness)
    assert len(certificates) == 1
    assert certificates[0].turn_id == "valid-\ufffd"
    assert tailer.completion is None
    assert tailer.completion_response is None
    assert not receipt.done(), "valid certificate still does not prove wake ownership"
    _append(harness, {"type": "user_message", "message": turn.prompt})
    await _read(harness)
    assert receipt.done() and receipt.result() is True


@pytest.mark.asyncio
async def test_partial_multibyte_user_receipt_waits_for_complete_record(harness):
    turn, receipt = await _paste(harness, prompt="wake \u03bb\ufffd\u2028work")
    record = json.dumps({"type": "event_msg", "payload": {
        "type": "user_message", "message": turn.prompt,
    }}, ensure_ascii=False).encode() + b"\n"
    split = record.index("\u03bb".encode()) + 1
    with harness.rollout.open("ab") as stream:
        stream.write(record[:split])
    offset = harness.session._tailer._offset
    assert await harness.session._tailer.read_once() == 0
    assert harness.session._tailer._offset == offset
    assert not receipt.done()
    with harness.rollout.open("ab") as stream:
        stream.write(record[split:])
    await _read(harness)
    assert receipt.done() and receipt.result() is True
    assert harness.session._tailer._offset == len(record)
    assert harness.session._tailer.stats["parse_errors"] == 0
