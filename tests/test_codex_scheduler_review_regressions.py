"""Independent exact-wake ownership and lossless completion-proof probes."""

from unittest.mock import MagicMock

import pytest

from tests.test_codex_scheduler_completion_provenance import _capture_certificates
from tests.test_codex_scheduler_idle_receipt import (
    _append,
    _complete,
    _paste,
    _read,
    _start,
)
from tests.test_codex_scheduler_idle_receipt import (
    harness as harness,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("unrelated_user_row", [False, True])
async def test_unowned_post_paste_task_cannot_accept_wake(harness, unrelated_user_row):
    accepted = MagicMock(return_value=True)
    turn, receipt = await _paste(harness, prompt="wake A", on_accept=accepted)
    # A successful paste is not evidence that Codex consumed A. A separate
    # autonomous/human task B can be the next observed task in this rollout.
    await _start(harness, "autonomous-B")
    if unrelated_user_row:
        _append(harness, {"type": "user_message", "message": "unrelated B"})
        await _read(harness)
        assert not receipt.done()
    await _complete(harness, "autonomous-B")
    harness.tmux.capture_pane.assert_not_awaited()
    assert not receipt.done(), "B's completion must not settle A's exact receipt"
    assert not turn.transport_accepted
    accepted.assert_not_called()


def _raw_event(event_type, raw_id):
    return (b'{"type":"event_msg","payload":{"type":"' + event_type
            + b'","turn_id":"' + raw_id + b'"}}\n')


@pytest.mark.asyncio
@pytest.mark.parametrize("start_id,close_id", [
    (b"x\xff", b"x\xfe"),
    (b"x\xff", "x\ufffd".encode()),
    (b"x\xff", b"x\\ufffd"),
])
async def test_distinct_raw_ids_cannot_collapse_into_completion(harness, start_id, close_id):
    turn, receipt = await _paste(harness)
    certificates = _capture_certificates(harness)
    with harness.rollout.open("ab") as stream:
        stream.write(_raw_event(b"task_started", start_id))
        stream.write(_raw_event(b"task_complete", close_id))
    await _read(harness)
    assert certificates and all(certificate is None for certificate in certificates)
    assert not receipt.done(), "lossy decoding forged equal task IDs"
    assert not turn.transport_accepted


@pytest.mark.asyncio
async def test_corrupt_intervening_record_invalidates_completion_proof(harness):
    turn, receipt = await _paste(harness)
    certificates = _capture_certificates(harness)
    await _start(harness)
    with harness.rollout.open("ab") as stream:
        stream.write(b'{"type":"event_msg","payload":{"type":"agent_message","message":"\xff"}}\n')
    await _complete(harness)
    assert certificates and all(certificate is None for certificate in certificates)
    assert not receipt.done(), "undecodable occurrence must fail closed"
    assert not turn.transport_accepted


@pytest.mark.asyncio
@pytest.mark.parametrize("turn_id", ["valid-\u03bb", "literal-\ufffd"])
async def test_valid_unicode_ids_remain_supported(harness, turn_id):
    _, receipt = await _paste(harness)
    certificates = _capture_certificates(harness)
    await _start(harness, turn_id)
    await _complete(harness, turn_id)
    assert len(certificates) == 1 and certificates[0].turn_id == turn_id
    assert not receipt.done()


@pytest.mark.asyncio
async def test_user_message_retains_authority_with_unproven_completion(harness):
    accepted = MagicMock(return_value=True)
    turn, receipt = await _paste(harness, on_accept=accepted)
    _append(harness, {"type": "user_message", "message": turn.prompt})
    await _read(harness)
    assert receipt.done() and receipt.result() is True
    await _complete(harness, "no-matching-start")
    accepted.assert_called_once_with()


@pytest.mark.asyncio
async def test_next_task_appended_during_real_callback_does_not_accept(harness):
    _, receipt = await _paste(harness)
    await _start(harness)
    appended = []

    async def on_event(event):
        if event.get("type") == "turn_completed":
            _append(harness,
                    {"type": "task_started", "turn_id": "B"},
                    {"type": "task_complete", "turn_id": "B"})
            appended.append(True)

    harness.session._stream_event_callback = on_event
    await _complete(harness)
    assert appended == [True]
    assert not receipt.done()
    harness.tmux.capture_pane.assert_not_awaited()


@pytest.mark.asyncio
async def test_json_escape_and_literal_same_id_are_equivalent(harness):
    _, receipt = await _paste(harness)
    certificates = _capture_certificates(harness)
    with harness.rollout.open("ab") as stream:
        stream.write(_raw_event(b"task_started", b"x\\u03bb"))
        stream.write(_raw_event(b"task_complete", "x\u03bb".encode()))
    await _read(harness)
    assert len(certificates) == 1 and certificates[0].turn_id == "xλ"
    assert not receipt.done()


@pytest.mark.asyncio
async def test_real_callback_repaste_preserves_late_receipt_authority(harness):
    turn, receipt = await _paste(harness)
    await _start(harness)
    old_ticket = harness.session._codex_paste_ticket(turn)
    repasted = []

    async def repaste():
        if repasted:
            return
        repasted.append(True)
        await harness.session._deliver_turn(turn)
        assert harness.session._codex_paste_ticket(turn) != old_ticket

    async def event_callback(event):
        if event.get("type") == "turn_completed":
            await repaste()

    harness.session._stream_event_callback = event_callback
    await _complete(harness)
    assert repasted == [True]
    assert harness.tmux.paste_text.await_count == 2
    assert not receipt.done()
    assert not turn.transport_accepted
    # A real exact receipt still has authority after fallback rejection.
    _append(harness, {"type": "user_message", "message": turn.prompt})
    await _read(harness)
    assert receipt.done() and receipt.result() is True
