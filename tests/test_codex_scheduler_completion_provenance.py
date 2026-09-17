"""Completion provenance is evidence of a task, not ownership of a submission."""

import pytest

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
@pytest.mark.parametrize("shape", [
    "no_start", "different_id", "missing_id", "aborted", "duplicate_start",
    "malformed", "historical_start", "trailing_start", "partial_trailing_start",
])
async def test_unproven_completion_retains_receipt(harness, shape):
    certificates = _capture_certificates(harness)
    if shape == "historical_start":
        _append(harness, {"type": "task_started", "turn_id": "current-turn"})
    turn, receipt = await _paste(harness)
    if shape == "historical_start":
        await _read(harness)
    elif shape != "no_start":
        await _start(harness)
    if shape == "duplicate_start":
        await _start(harness, "current-turn")
    if shape == "malformed":
        with harness.rollout.open("a") as stream:
            stream.write("invalid record\n")
    payload = {
        "type": "turn_aborted" if shape == "aborted" else "task_complete",
        "turn_id": "different-turn" if shape == "different_id" else "current-turn",
        "last_agent_message": "completed",
    }
    if shape == "missing_id":
        payload.pop("turn_id")
    _append(harness, payload)
    if shape == "trailing_start":
        _append(harness, {"type": "task_started", "turn_id": "next-turn"})
    elif shape == "partial_trailing_start":
        with harness.rollout.open("a") as stream:
            stream.write('{"type":"event_msg"')
    await _read(harness)
    assert not receipt.done()
    assert harness.session.scheduler_wake_inflight(turn.prompt)
    assert harness.tmux.capture_pane.await_count == 0
    assert certificates
    if shape in {"no_start", "different_id", "missing_id", "aborted", "duplicate_start",
                 "malformed"}:
        assert all(certificate is None for certificate in certificates)
    else:
        # Physical task provenance can be valid without proving wake ownership.
        assert certificates[0] is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("field", [
    "transcript_path_at_paste", "transcript_file_identity_at_paste",
    "transcript_offset_at_paste", "transcript_anchor_start_at_paste",
    "transcript_anchor_at_paste", "transcript_ticket_captured_at_ns",
])
async def test_missing_ticket_is_not_reconstructed(harness, field):
    turn, receipt = await _paste(harness)
    setattr(turn, field, None)
    await _start(harness)
    await _complete(harness)
    assert not receipt.done()
    assert harness.tmux.capture_pane.await_count == 0


def _capture_certificates(harness):
    tailer = harness.session._tailer
    original = tailer._on_turn_complete
    certificates = []

    async def capture(response):
        certificates.append(tailer.completion)
        await original(response)

    tailer._on_turn_complete = capture
    return certificates


@pytest.mark.asyncio
@pytest.mark.parametrize("reset", ["drain", "offset", "path"])
async def test_reset_between_start_and_close_invalidates_certificate(harness, reset):
    turn, receipt = await _paste(harness)
    certificates = _capture_certificates(harness)
    await _start(harness)
    tailer = harness.session._tailer
    if reset == "drain":
        tailer.drain_buffer()
    elif reset == "offset":
        tailer.set_offset(tailer._offset)
    else:
        other = harness.rollout.with_suffix(".other")
        other.touch()
        tailer.set_transcript_path(other)
        harness.rollout = other
    await _complete(harness)
    assert certificates and all(cert is None for cert in certificates)
    assert not receipt.done()
    assert not turn.transport_accepted


@pytest.mark.asyncio
async def test_callback_certificate_is_scoped_to_exact_response(harness):
    _, receipt = await _paste(harness)
    tailer = harness.session._tailer
    original = tailer._on_turn_complete
    observed = []

    async def capture(response):
        assert tailer.completion_response is response
        observed.append(tailer.completion)
        await original(response)

    tailer._on_turn_complete = capture
    await _start(harness)
    await _complete(harness)
    assert len(observed) == 1 and observed[0] is not None
    assert tailer.completion is None and tailer.completion_response is None
    assert not receipt.done()


@pytest.mark.asyncio
async def test_byte_offsets_survive_unicode_crlf_and_blank_records(harness):
    harness.rollout.write_bytes('{"type":"metadata","text":"λ"}\r\n\r\n'.encode())
    turn, receipt = await _paste(harness)
    certificates = _capture_certificates(harness)
    with harness.rollout.open("ab") as stream:
        stream.write(b'\r\n{"type":"event_msg","payload":{"type":"task_started",'
                     b'"turn_id":"current-turn"}}\r\n')
    await _read(harness)
    await _complete(harness)
    assert not receipt.done() and not turn.transport_accepted
    assert len(certificates) == 1 and certificates[0] is not None
    certificate = certificates[0]
    data = harness.rollout.read_bytes()
    assert data[certificate.start_offset:].startswith(certificate.start_record)
    assert data[certificate.end_offset - len(certificate.close_record):
                certificate.end_offset] == certificate.close_record
    harness.tmux.capture_pane.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["", "completed"])
async def test_matching_start_and_close_certifies_task_without_accepting_wake(harness, text):
    turn, receipt = await _paste(harness)
    certificates = _capture_certificates(harness)
    _append(harness,
            {"type": "task_started", "turn_id": "current-turn"},
            {"type": "task_complete", "turn_id": "current-turn", "last_agent_message": text})
    await _read(harness)
    assert not receipt.done() and not turn.transport_accepted
    assert len(certificates) == 1 and certificates[0] is not None
    certificate = certificates[0]
    data = harness.rollout.read_bytes()
    assert data[certificate.start_offset:].startswith(certificate.start_record)
    assert data[certificate.end_offset - len(certificate.close_record):
                certificate.end_offset] == certificate.close_record
    harness.tmux.capture_pane.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_rollout_at_paste_does_not_gain_identity_later(harness):
    harness.rollout.unlink()
    turn, receipt = await _paste(harness)
    assert turn.transcript_file_identity_at_paste is None
    await _start(harness)
    await _complete(harness)
    assert not receipt.done()
    assert harness.session.scheduler_wake_inflight(turn.prompt)
    assert harness.tmux.capture_pane.await_count == 0
