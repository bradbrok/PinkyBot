"""Base-parity checks independent of completion-fallback ownership."""
import pytest

from tests.test_codex_scheduler_idle_receipt import _append, _paste, _read, _start
from tests.test_codex_scheduler_idle_receipt import harness as harness


@pytest.mark.asyncio
async def test_valid_receipt_and_close_keep_base_behavior(harness):
    turn, receipt = await _paste(harness)
    await _start(harness)
    _append(harness, {"type": "user_message", "message": turn.prompt},
            {"type": "task_complete", "turn_id": "current-turn",
             "last_agent_message": "done"})
    await _read(harness)
    assert receipt.done() and receipt.result() is True
    assert not harness.session._inflight_metas
    assert not harness.session._tailer._active


@pytest.mark.asyncio
async def test_corrupt_unrelated_field_does_not_drop_exact_user_receipt(harness):
    turn, receipt = await _paste(harness)
    with harness.rollout.open("ab") as stream:
        stream.write(b'{"timestamp":"bad\xff","type":"event_msg",'
                     b'"payload":{"type":"user_message","message":"scheduled work"}}\n')
    await _read(harness)
    assert receipt.done() and receipt.result() is True
    assert turn.transport_accepted


@pytest.mark.asyncio
async def test_corrupt_close_text_does_not_strand_already_receipted_turn(harness):
    turn, receipt = await _paste(harness)
    await _start(harness)
    _append(harness, {"type": "user_message", "message": turn.prompt})
    await _read(harness)
    assert receipt.done() and receipt.result() is True
    with harness.rollout.open("ab") as stream:
        stream.write(b'{"type":"event_msg","payload":{"type":"task_complete",'
                     b'"turn_id":"current-turn","last_agent_message":"done\xff"}}\n')
    await _read(harness)
    assert not harness.session._inflight_metas, "base retired the accepted turn"
    assert not harness.session._tailer._active, "base cleared active on this close"
