"""Completion fallback must retain one proven occurrence across idle awaits."""

import asyncio
from unittest.mock import MagicMock

import pytest

from pinky_daemon.codex_tmux_transcript import CodexTmuxTranscriptTailer
from pinky_daemon.tmux_session import _QueuedTurn
from pinky_daemon.transport_state import SessionState
from tests.test_codex_scheduler_idle_receipt import (
    IDLE,
    _append,
    _complete,
    _ok,
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


@pytest.mark.asyncio
@pytest.mark.parametrize("capture_number", [1, 2])
@pytest.mark.parametrize("change", [
    "file_replaced", "file_missing", "anchor_changed", "start_changed", "close_changed",
    "file_appended", "ticket_changed", "tailer_swapped", "path_swapped", "offset_reset",
    "buffer_drained", "active", "queued", "in_hand", "tool", "other_meta", "disconnected",
    "receipt_replaced", "receipt_cancelled", "receipt_rejected", "late_user_message",
    "candidate_removed",
])
async def test_idle_await_cannot_change_occurrence(harness, capture_number, change):
    # A nonempty pre-paste anchor makes same-inode prefix rewriting observable.
    harness.rollout.write_text('{"type":"session_meta","payload":{}}\n')
    accept = MagicMock(return_value=True)
    turn, receipt = await _paste(harness, on_accept=accept)
    await _start(harness)
    original_tailer = harness.session._tailer
    capture_calls = 0

    async def capture(**_kwargs):
        nonlocal capture_calls
        capture_calls += 1
        if capture_calls == capture_number:
            data = harness.rollout.read_bytes()
            if change == "file_replaced":
                replacement = harness.rollout.with_suffix(".replacement")
                replacement.write_bytes(data)
                replacement.replace(harness.rollout)
            elif change == "file_missing":
                harness.rollout.unlink()
            elif change in {"anchor_changed", "start_changed", "close_changed"}:
                old, new = {
                    "anchor_changed": (b"session_meta", b"session_meto"),
                    "start_changed": (b"task_started", b"task_starteX"),
                    "close_changed": (b"completed", b"completeX"),
                }[change]
                harness.rollout.write_bytes(data.replace(old, new, 1))
            elif change == "file_appended":
                _append(harness, {"type": "task_started", "turn_id": "next-turn"})
            elif change == "ticket_changed":
                turn.transcript_offset_at_paste += 1
            elif change == "tailer_swapped":
                harness.session._tailer = CodexTmuxTranscriptTailer(
                    harness.rollout, harness.session._handle_turn_complete,
                )
            elif change == "path_swapped":
                other = harness.rollout.with_suffix(".other")
                other.write_bytes(data)
                original_tailer.set_transcript_path(other)
            elif change == "offset_reset":
                original_tailer.set_offset(0)
            elif change == "buffer_drained":
                original_tailer.drain_buffer()
            elif change == "active":
                original_tailer.mark_active()
            elif change == "queued":
                harness.session._message_queue.put_nowait(_QueuedTurn(prompt="next work"))
            elif change == "in_hand":
                harness.session._inflight_turn = _QueuedTurn(prompt="next work")
            elif change == "tool":
                harness.session._inflight_tool_calls["new-tool"] = {"tool": "test"}
            elif change == "other_meta":
                other = _QueuedTurn(prompt="next work", pane_delivery_started=True)
                harness.session._finish_turn_delivery(other)
            elif change == "disconnected":
                harness.session._state_machine._state = SessionState.DISCONNECTED
            elif change == "receipt_replaced":
                turn.scheduler_delivery = asyncio.get_running_loop().create_future()
            elif change == "receipt_cancelled":
                receipt.cancel()
            elif change == "receipt_rejected":
                receipt.set_result(False)
            elif change == "late_user_message":
                harness.session._on_transcript_entry({
                    "type": "event_msg",
                    "payload": {"type": "user_message", "message": turn.prompt},
                })
            elif change == "candidate_removed":
                harness.session._scheduler_pending_turns.remove(turn)
        return _ok(IDLE)

    harness.tmux.capture_pane.side_effect = capture
    await _complete(harness)
    assert original_tailer.stats["callback_errors"] == 0
    if change == "late_user_message":
        assert receipt.done() and receipt.result() is True
        accept.assert_called_once_with()
    else:
        assert not turn.transport_accepted
        accept.assert_not_called()
        if change == "receipt_cancelled":
            assert receipt.cancelled()
        elif change == "receipt_rejected":
            assert receipt.result() is False
        else:
            assert not receipt.done()
        if change == "receipt_replaced":
            assert not turn.scheduler_delivery.done()


@pytest.mark.asyncio
async def test_byte_offsets_survive_unicode_crlf_and_blank_records(harness):
    harness.rollout.write_bytes('{"type":"metadata","text":"λ"}\r\n\r\n'.encode())
    turn, receipt = await _paste(harness)
    with harness.rollout.open("ab") as stream:
        stream.write(b'\r\n{"type":"event_msg","payload":{"type":"task_started",'
                     b'"turn_id":"current-turn"}}\r\n')
    await _read(harness)
    await _complete(harness)
    assert receipt.done() and receipt.result() is True
    assert turn.transport_accepted
    assert harness.tmux.capture_pane.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["", "completed"])
async def test_matching_start_and_close_in_one_read_accepts(harness, text):
    turn, receipt = await _paste(harness)
    _append(harness,
            {"type": "task_started", "turn_id": "current-turn"},
            {"type": "task_complete", "turn_id": "current-turn", "last_agent_message": text})
    await _read(harness)
    assert receipt.done() and receipt.result() is True
    assert turn.transport_accepted
    assert harness.tmux.capture_pane.await_count == 2


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
