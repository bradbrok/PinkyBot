"""Queue-operation remove consumption coverage for mid-turn folds (#1171).

Claude Code normally records a folded queued prompt as three transcript rows:
``queue-operation/enqueue``, ``queue-operation/remove``, and a
``queued_command`` attachment.  A remove alone is not positive consumption
evidence.  Certification requires one exact, paste-bound occurrence of all
three rows, while any matched remove must still retire its native queue slot.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from pinky_daemon import tmux_session, watchdog_log
from pinky_daemon.codex_session import CodexSession
from pinky_daemon.codex_tmux_session import CodexTmuxSession
from pinky_daemon.streaming_session import StreamingSessionConfig
from pinky_daemon.tmux_session import (
    TmuxCommandResult,
    TmuxSession,
    _InflightMeta,
    _QueuedTurn,
    _TmuxControl,
)
from pinky_daemon.transport_state import SessionState


def _ok() -> TmuxCommandResult:
    return TmuxCommandResult(returncode=0, stdout="", stderr="")


def _mock_tmux() -> MagicMock:
    control = MagicMock(spec=_TmuxControl)
    control.session_name = "pinky-fold-remove"
    control.tmux_binary = "tmux"
    control.socket_name = ""
    control.socket_path = None
    control.has_session = AsyncMock(return_value=False)
    control.new_session = AsyncMock(return_value=_ok())
    control.kill_session = AsyncMock(return_value=_ok())
    control.rename_session = AsyncMock(return_value=_ok())
    control.send_keys = AsyncMock(return_value=_ok())
    control.paste_text = AsyncMock(return_value=_ok())
    control.capture_pane = AsyncMock(return_value=_ok())
    control.resize_window = AsyncMock(return_value=_ok())
    return control


def _mock_registry() -> MagicMock:
    """Return fail-closed registry gates for every node in this file."""
    registry = MagicMock()
    # A bare MagicMock predicate is truthy. Positive ledger evidence must
    # always be deliberate, never an unconfigured fixture artifact.
    registry.is_turn_delivered.return_value = False
    registry.get_signing_key.return_value = None
    registry.get_or_create_signing_key.return_value = b""
    registry.mark_turn_delivered.return_value = True
    return registry


def _make_session(
    *,
    registry: object | None = None,
    session_type: type[TmuxSession] = TmuxSession,
) -> TmuxSession:
    if registry is None:
        registry = _mock_registry()
    session = session_type(
        StreamingSessionConfig(
            agent_name="dymok",
            working_dir="/tmp/tmux-fold-remove",
        ),
        registry=registry,
        tmux_control=_mock_tmux(),
    )
    session._skip_wake_prompt_for_tests = True
    session._state_machine._state = SessionState.CONNECTED
    return session


def _seed_inflight(
    session: TmuxSession,
    *,
    prompt: str,
    scheduler_delivery: asyncio.Future[bool] | None = None,
    submission_receipt: asyncio.Future[bool] | None = None,
    scheduler_accept: object = None,
    completion_event: asyncio.Event | None = None,
    platform: str = "telegram",
    chat_id: str = "chat",
    message_id: str = "message",
) -> _InflightMeta:
    turn = _QueuedTurn(
        prompt=prompt,
        scheduler_delivery=scheduler_delivery,
        submission_receipt=submission_receipt,
        scheduler_accept=scheduler_accept,
        completion_event=completion_event,
        platform=platform,
        chat_id=chat_id,
        message_id=message_id,
    )
    turn.pane_delivery_started = True
    entry = _InflightMeta(
        meta={
            "platform": platform,
            "chat_id": chat_id,
            "message_id": message_id,
        },
        completion_event=completion_event,
        internal=False,
        dispatched_at=time.time(),
        turn=turn,
    )
    was_empty = not session._inflight_metas
    session._inflight_metas.append(entry)
    if was_empty:
        session._head_started_at = time.time()
    return entry


def _ticket_parts(transcript: Path) -> tuple[tuple[int, int], int, int, bytes]:
    stat = transcript.stat()
    offset = stat.st_size
    anchor_start = max(0, offset - 4096)
    anchor = transcript.read_bytes()[anchor_start:offset]
    return (stat.st_dev, stat.st_ino), offset, anchor_start, anchor


def _bind_ticket(entry: _InflightMeta, transcript: Path) -> None:
    _bind_turn_ticket(entry.turn, transcript)
    identity, offset, anchor_start, anchor = _ticket_parts(transcript)
    entry.transcript_path_at_paste = transcript
    entry.transcript_file_identity_at_paste = identity
    entry.transcript_offset_at_paste = offset
    entry.transcript_anchor_start_at_paste = anchor_start
    entry.transcript_anchor_at_paste = anchor
    entry.transcript_ticket_captured_at_ns = time.time_ns()


def _bind_turn_ticket(turn: _QueuedTurn, transcript: Path) -> None:
    identity, offset, anchor_start, anchor = _ticket_parts(transcript)
    turn.transcript_path_at_paste = transcript
    turn.transcript_file_identity_at_paste = identity
    turn.transcript_offset_at_paste = offset
    turn.transcript_anchor_start_at_paste = anchor_start
    turn.transcript_anchor_at_paste = anchor
    turn.transcript_ticket_captured_at_ns = time.time_ns()


def _append_entry(transcript: Path, entry: dict) -> int:
    offset = transcript.stat().st_size if transcript.exists() else 0
    with transcript.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return offset


def _queue_entry(operation: str, prompt: str | None = None) -> dict:
    entry = {"type": "queue-operation", "operation": operation}
    if prompt is not None:
        entry["content"] = prompt
    return entry


def _attachment_entry(prompt: str, *, attachment_type: str = "queued_command") -> dict:
    return {
        "type": "attachment",
        "attachment": {
            "type": attachment_type,
            "prompt": prompt,
            "origin": {"kind": "composer"},
        },
    }


def _emit_live(
    session: TmuxSession,
    transcript: Path,
    entry: dict,
    *,
    entry_offset: int | None | object = Ellipsis,
    source_identity: tuple[int, int] | None | object = Ellipsis,
) -> int:
    written_offset = _append_entry(transcript, entry)
    if entry_offset is Ellipsis:
        entry_offset = written_offset
    if source_identity is Ellipsis:
        stat = transcript.stat()
        source_identity = (stat.st_dev, stat.st_ino)
    session._on_transcript_entry(
        entry,
        entry_offset=entry_offset,
        source_identity=source_identity,
    )
    return written_offset


def _emit_fold_chain(
    session: TmuxSession,
    transcript: Path,
    prompt: str,
    *,
    attachment_first: bool,
) -> None:
    _emit_live(session, transcript, _queue_entry("enqueue", prompt))
    if attachment_first:
        _emit_live(session, transcript, _attachment_entry(prompt))
    _emit_live(session, transcript, _queue_entry("remove", prompt))
    if not attachment_first:
        _emit_live(session, transcript, _attachment_entry(prompt))


def _append_fold_chain(
    transcript: Path,
    prompt: str,
    *,
    attachment_first: bool,
) -> None:
    _append_entry(transcript, _queue_entry("enqueue", prompt))
    if attachment_first:
        _append_entry(transcript, _attachment_entry(prompt))
    _append_entry(transcript, _queue_entry("remove", prompt))
    if not attachment_first:
        _append_entry(transcript, _attachment_entry(prompt))


def _assert_fold_pair_byte_counters(session: TmuxSession) -> None:
    assert session._pane_fold_attachment_bytes == sum(
        session._fold_pair_prompt_bytes(evidence.prompt)
        for evidence in session._pane_fold_attachments
    )
    assert session._pane_fold_remove_bytes == sum(
        session._fold_pair_prompt_bytes(evidence.queued.content or "")
        for evidence in session._pane_fold_removes
    )


def _enable_fast_idle_reconcile(
    monkeypatch: pytest.MonkeyPatch,
    session: TmuxSession,
) -> None:
    monkeypatch.setattr(tmux_session, "_TURN_DONE_TIMEOUT_SEC", 0.01)
    monkeypatch.setattr(tmux_session, "_WATCHDOG_TICK_SEC", 0.01)
    session._config.live_status_fn = lambda: {
        "status": "idle",
        "last_updated": time.time() + 1,
    }
    session._transcript_recently_grew = lambda *_args: False
    session._head_started_at = time.time() - 1


async def _run_until_requeued(session: TmuxSession) -> None:
    task = asyncio.create_task(session._inflight_watchdog())
    try:
        for _ in range(200):
            if not session._inflight_metas and not session._message_queue.empty():
                break
            await asyncio.sleep(0.01)
        else:
            pytest.fail("watchdog did not requeue the unconsumed idle turn")
    finally:
        task.cancel()
        await task


async def _run_until_reconciled(session: TmuxSession) -> None:
    task = asyncio.create_task(session._inflight_watchdog())
    try:
        for _ in range(200):
            if not session._inflight_metas:
                break
            await asyncio.sleep(0.01)
        else:
            pytest.fail("watchdog did not reconcile the consumed idle turn")
    finally:
        task.cancel()
        await task


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "attachment_first",
    [
        pytest.param(True, id="attachment-before-remove"),
        pytest.param(False, id="attachment-after-remove"),
    ],
)
async def test_f1_f2_live_full_chain_certifies_receipts_and_ledger(
    attachment_first: bool,
    tmp_path: Path,
) -> None:
    registry = _mock_registry()
    session = _make_session(registry=registry)
    scheduler_receipt = asyncio.get_running_loop().create_future()
    submission_receipt = asyncio.get_running_loop().create_future()
    prompt = f"live remove fold attachment_first={attachment_first}"
    transcript = tmp_path / "live.jsonl"
    transcript.write_text('{"type":"system"}\n', encoding="utf-8")
    seeded = _seed_inflight(
        session,
        prompt=prompt,
        scheduler_delivery=scheduler_receipt,
        submission_receipt=submission_receipt,
        message_id=f"message-{attachment_first}",
    )
    _bind_ticket(seeded, transcript)

    _emit_fold_chain(
        session,
        transcript,
        prompt,
        attachment_first=attachment_first,
    )

    assert seeded.turn.transport_accepted is True
    assert scheduler_receipt.result() is True
    assert submission_receipt.result() is True
    registry.mark_turn_delivered.assert_called_once_with(
        "dymok",
        "telegram",
        "chat",
        f"message-{attachment_first}",
        source="telegram",
    )
    assert not session._pane_queue_operations


@pytest.mark.parametrize(
    "attachment_first",
    [
        pytest.param(True, id="attachment-before-remove"),
        pytest.param(False, id="attachment-after-remove"),
    ],
)
def test_f3_f4_phantom_probe_recognizes_full_remove_chain(
    attachment_first: bool,
    tmp_path: Path,
) -> None:
    session = _make_session()
    transcript = tmp_path / "phantom.jsonl"
    transcript.write_text('{"type":"system"}\n', encoding="utf-8")
    prompt = f"phantom remove fold attachment_first={attachment_first}"
    seeded = _seed_inflight(session, prompt=prompt)
    _bind_ticket(seeded, transcript)
    _append_fold_chain(
        transcript,
        prompt,
        attachment_first=attachment_first,
    )

    assert session._phantom_consumption_verdicts([seeded]) == [True]


def test_f5_cancel_remove_retires_evidence_without_certification(tmp_path: Path) -> None:
    session = _make_session()
    transcript = tmp_path / "cancel-retire.jsonl"
    transcript.write_text('{"type":"system"}\n', encoding="utf-8")
    seeded = _seed_inflight(session, prompt="cancelled folded prompt")
    _bind_ticket(seeded, transcript)

    _emit_live(session, transcript, _queue_entry("enqueue", seeded.turn.prompt))
    _emit_live(session, transcript, _queue_entry("remove", seeded.turn.prompt))

    assert not session._pane_queue_operations
    assert seeded.turn.transport_accepted is False


def test_f6_remove_retires_nonhead_and_cap_eviction_is_observable(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    session = _make_session()
    transcript = tmp_path / "nonhead.jsonl"
    transcript.write_text('{"type":"system"}\n', encoding="utf-8")
    first = _seed_inflight(session, prompt="queue head")
    second = _seed_inflight(session, prompt="queue middle")
    _bind_ticket(first, transcript)
    _bind_ticket(second, transcript)
    _emit_live(session, transcript, _queue_entry("enqueue", first.turn.prompt))
    _emit_live(session, transcript, _queue_entry("enqueue", second.turn.prompt))
    _emit_live(session, transcript, _queue_entry("remove", second.turn.prompt))
    remaining = [evidence.content for evidence in session._pane_queue_operations]

    bounded = _make_session()
    for index in range(1025):
        bounded._on_transcript_entry(
            _attachment_entry(f"occurrence-bound-{index}"),
            entry_offset=index,
            source_identity=(7, 11),
        )
    bounded._on_transcript_entry(
        _attachment_entry("byte-bound header\n" + "x" * (4 * 1024 * 1024)),
        entry_offset=2048,
        source_identity=(7, 11),
    )
    logs = capsys.readouterr().err.splitlines()

    def has_eviction(bound: str, header: str) -> bool:
        return any(
            "evict" in line.lower()
            and "attachment" in line.lower()
            and bound in line.lower()
            and f"prompt_header={header!r}" in line
            for line in logs
        )

    problems: list[str] = []
    if remaining != [first.turn.prompt]:
        problems.append(f"non-head remove left {remaining!r}")
    if not has_eviction("occurrence", "occurrence-bound-0"):
        problems.append("occurrence-cap eviction lacked operation/header/bound log")
    if not has_eviction("byte", "byte-bound header"):
        problems.append("byte-cap eviction lacked operation/header/bound log")
    assert not problems, "; ".join(problems)


@pytest.mark.asyncio
async def test_f7_remove_repairs_fifo_before_later_dequeue(tmp_path: Path) -> None:
    session = _make_session()
    transcript = tmp_path / "fifo.jsonl"
    transcript.write_text('{"type":"system"}\n', encoding="utf-8")
    first_receipt = asyncio.get_running_loop().create_future()
    second_receipt = asyncio.get_running_loop().create_future()
    first = _seed_inflight(
        session,
        prompt="fold A",
        submission_receipt=first_receipt,
        message_id="a",
    )
    second = _seed_inflight(
        session,
        prompt="dequeue B",
        submission_receipt=second_receipt,
        message_id="b",
    )
    _bind_ticket(first, transcript)
    _bind_ticket(second, transcript)

    _emit_live(session, transcript, _queue_entry("enqueue", first.turn.prompt))
    _emit_live(session, transcript, _queue_entry("remove", first.turn.prompt))
    _emit_live(session, transcript, _queue_entry("enqueue", second.turn.prompt))
    _emit_live(session, transcript, _queue_entry("dequeue"))

    assert first.turn.transport_accepted is False
    assert not first_receipt.done()
    assert second.turn.transport_accepted is True
    assert second_receipt.result() is True


def test_f8_one_attachment_certifies_only_oldest_identical_remove(
    tmp_path: Path,
) -> None:
    session = _make_session()
    transcript = tmp_path / "one-attachment.jsonl"
    transcript.write_text('{"type":"system"}\n', encoding="utf-8")
    prompt = "same folded prompt"
    first = _seed_inflight(session, prompt=prompt, message_id="first")
    _bind_ticket(first, transcript)
    _emit_live(session, transcript, _queue_entry("enqueue", prompt))
    _emit_live(session, transcript, _queue_entry("remove", prompt))

    second = _seed_inflight(session, prompt=prompt, message_id="second")
    _bind_ticket(second, transcript)
    _emit_live(session, transcript, _queue_entry("enqueue", prompt))
    _emit_live(session, transcript, _queue_entry("remove", prompt))
    _emit_live(session, transcript, _attachment_entry(prompt))

    assert first.turn.transport_accepted is True
    assert second.turn.transport_accepted is False


def test_f9_distinct_attachments_certify_identical_removes_oldest_first(
    tmp_path: Path,
) -> None:
    session = _make_session()
    transcript = tmp_path / "two-attachments.jsonl"
    transcript.write_text('{"type":"system"}\n', encoding="utf-8")
    prompt = "same folded prompt twice"
    accepted: list[int] = []
    first = _seed_inflight(
        session,
        prompt=prompt,
        scheduler_accept=lambda: accepted.append(0) or True,
        message_id="first",
    )
    _bind_ticket(first, transcript)
    _emit_live(session, transcript, _queue_entry("enqueue", prompt))

    second = _seed_inflight(
        session,
        prompt=prompt,
        scheduler_accept=lambda: accepted.append(1) or True,
        message_id="second",
    )
    _bind_ticket(second, transcript)
    _emit_live(session, transcript, _queue_entry("enqueue", prompt))
    _emit_live(session, transcript, _queue_entry("remove", prompt))
    _emit_live(session, transcript, _queue_entry("remove", prompt))
    _emit_live(session, transcript, _attachment_entry(prompt))
    _emit_live(session, transcript, _attachment_entry(prompt))

    assert accepted == [0, 1]
    assert first.turn.transport_accepted is True
    assert second.turn.transport_accepted is True


@pytest.mark.asyncio
async def test_f10_busy_receiver_refold_binds_only_redelivery_ticket(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    registry = _mock_registry()
    session = _make_session(registry=registry)
    transcript = tmp_path / "redelivery.jsonl"
    transcript.write_text('{"type":"system"}\n', encoding="utf-8")
    session._tailer = MagicMock()
    session._tailer.transcript_path = transcript
    prompt = "busy receiver refolds this redelivery"

    original = _seed_inflight(session, prompt=prompt, message_id="redelivery")
    _bind_ticket(original, transcript)
    original_ticket_offset = original.transcript_offset_at_paste
    original_enqueue_offset = _emit_live(
        session,
        transcript,
        _queue_entry("enqueue", prompt),
    )
    original_remove_offset = _emit_live(
        session,
        transcript,
        _queue_entry("remove", prompt),
    )

    _enable_fast_idle_reconcile(monkeypatch, session)
    await _run_until_requeued(session)

    turn = session._message_queue.get_nowait()
    assert turn is original.turn
    assert turn.replay_count == 1

    # The delayed attachment completes occurrence 1 only after its bounded
    # live remove evidence was purged. It lands before the redelivery ticket,
    # so the complete stale chain cannot certify occurrence 2.
    original_attachment_offset = _emit_live(
        session,
        transcript,
        _attachment_entry(prompt),
    )
    _bind_turn_ticket(turn, transcript)
    turn.pane_delivery_started = True
    session._finish_turn_delivery(turn)
    redelivery = session._inflight_metas[0]
    assert redelivery.turn is turn
    assert turn.pane_delivery_recorded is True
    assert turn.pane_delivery_started is True
    assert original_ticket_offset is not None
    assert redelivery.transcript_offset_at_paste is not None
    assert redelivery.transcript_offset_at_paste > original_ticket_offset
    assert (
        max(
            original_enqueue_offset,
            original_remove_offset,
            original_attachment_offset,
        )
        < redelivery.transcript_offset_at_paste
    )
    assert session._phantom_consumption_verdicts([redelivery]) == [False]
    assert turn.transport_accepted is False
    registry.mark_turn_delivered.assert_not_called()

    # Occurrence 2 carries its own attachment. The bounded live path remains
    # fenced after purge and must refuse it; byte-zero FIFO reconstruction sees
    # two attachments, binds each to its own remove, and proves only chain 2 is
    # fresh-ticket-bound. Reconciliation therefore settles at replay cycle 1
    # instead of advancing toward the replay cap.
    _emit_fold_chain(
        session,
        transcript,
        prompt,
        attachment_first=True,
    )

    assert turn.transport_accepted is False
    assert session._phantom_consumption_verdicts([redelivery]) == [True]
    session._head_started_at = time.time() - 1
    await _run_until_reconciled(session)

    assert turn.transport_accepted is True
    assert turn.replay_count == 1
    assert session._message_queue.empty()
    registry.mark_turn_delivered.assert_called_once_with(
        "dymok",
        "telegram",
        "chat",
        "redelivery",
        source="telegram",
    )


@pytest.mark.parametrize(
    "delayed_attachment_before_enqueue",
    [
        pytest.param(True, id="delayed-before-new-enqueue"),
        pytest.param(False, id="delayed-after-new-enqueue"),
    ],
)
def test_evicted_remove_cannot_donate_delayed_attachment_to_new_cancel(
    delayed_attachment_before_enqueue: bool,
    tmp_path: Path,
) -> None:
    session = _make_session()
    transcript = tmp_path / "evicted-remove.jsonl"
    transcript.write_text('{"type":"system"}\n', encoding="utf-8")
    prompt = "equal prompt with delayed attachment"

    _emit_live(session, transcript, _queue_entry("enqueue", prompt))
    _emit_live(session, transcript, _queue_entry("remove", prompt))
    for index in range(1024):
        filler = f"remove-cache filler {index}"
        _emit_live(session, transcript, _queue_entry("enqueue", filler))
        _emit_live(session, transcript, _queue_entry("remove", filler))

    assert all(evidence.queued.content != prompt for evidence in session._pane_fold_removes)
    _assert_fold_pair_byte_counters(session)

    seeded = _seed_inflight(session, prompt=prompt)
    _bind_ticket(seeded, transcript)
    if delayed_attachment_before_enqueue:
        _emit_live(session, transcript, _attachment_entry(prompt))
        _emit_live(session, transcript, _queue_entry("enqueue", prompt))
    else:
        _emit_live(session, transcript, _queue_entry("enqueue", prompt))
        _emit_live(session, transcript, _attachment_entry(prompt))
    _emit_live(session, transcript, _queue_entry("remove", prompt))

    assert seeded.turn.transport_accepted is False
    _assert_fold_pair_byte_counters(session)


@pytest.mark.parametrize(
    "delayed_attachment_before_enqueue",
    [
        pytest.param(True, id="delayed-before-new-enqueue"),
        pytest.param(False, id="delayed-after-new-enqueue"),
    ],
)
def test_probe_old_remove_cannot_donate_delayed_attachment_to_new_cancel(
    delayed_attachment_before_enqueue: bool,
    tmp_path: Path,
) -> None:
    session = _make_session()
    transcript = tmp_path / "probe-delayed-attachment.jsonl"
    transcript.write_text('{"type":"system"}\n', encoding="utf-8")
    prompt = "probe equal prompt with delayed attachment"

    _append_entry(transcript, _queue_entry("enqueue", prompt))
    _append_entry(transcript, _queue_entry("remove", prompt))
    seeded = _seed_inflight(session, prompt=prompt)
    _bind_ticket(seeded, transcript)

    if delayed_attachment_before_enqueue:
        _append_entry(transcript, _attachment_entry(prompt))
        _append_entry(transcript, _queue_entry("enqueue", prompt))
    else:
        _append_entry(transcript, _queue_entry("enqueue", prompt))
        _append_entry(transcript, _attachment_entry(prompt))
    _append_entry(transcript, _queue_entry("remove", prompt))

    assert session._phantom_consumption_verdicts([seeded]) == [False]


def test_replay_purge_fences_delayed_attachment_from_new_cancel(
    tmp_path: Path,
) -> None:
    session = _make_session()
    transcript = tmp_path / "replay-purge.jsonl"
    transcript.write_text('{"type":"system"}\n', encoding="utf-8")
    prompt = "replayed equal prompt with delayed attachment"
    original = _seed_inflight(session, prompt=prompt, message_id="original")
    survivor = _seed_inflight(
        session,
        prompt="unrelated pending remove",
        message_id="survivor",
    )
    _bind_ticket(original, transcript)
    _bind_ticket(survivor, transcript)
    for seeded in (original, survivor):
        _emit_live(
            session,
            transcript,
            _queue_entry("enqueue", seeded.turn.prompt),
        )
        _emit_live(
            session,
            transcript,
            _queue_entry("remove", seeded.turn.prompt),
        )

    session._purge_fold_removes_for_turn(original.turn)

    assert [evidence.queued.turn for evidence in session._pane_fold_removes] == [survivor.turn]
    _assert_fold_pair_byte_counters(session)

    _bind_ticket(original, transcript)
    original.turn.pane_delivery_started = True
    original.turn.pane_queue_enqueued = False
    _emit_live(session, transcript, _queue_entry("enqueue", prompt))
    _emit_live(session, transcript, _attachment_entry(prompt))
    _emit_live(session, transcript, _queue_entry("remove", prompt))

    assert original.turn.transport_accepted is False
    _assert_fold_pair_byte_counters(session)


def test_nonhead_retirement_preserves_occurrence_and_frozen_ticket_fields(
    tmp_path: Path,
) -> None:
    session = _make_session()
    transcript = tmp_path / "retire-in-place.jsonl"
    transcript.write_text('{"type":"system"}\n', encoding="utf-8")
    first = _seed_inflight(session, prompt="first")
    second = _seed_inflight(session, prompt="second")
    _bind_ticket(first, transcript)
    _bind_ticket(second, transcript)
    _emit_live(session, transcript, _queue_entry("enqueue", first.turn.prompt))
    _emit_live(session, transcript, _queue_entry("enqueue", second.turn.prompt))
    before = list(session._pane_queue_operations)

    session._retire_acceptance_evidence(second.turn)
    after = list(session._pane_queue_operations)

    assert [item.content for item in after] == ["first", "second"]
    assert after[0] is before[0]
    assert after[1].retired is True
    assert after[1].occurrence_id == before[1].occurrence_id
    assert after[1].entry_offset == before[1].entry_offset
    assert after[1].source_identity == before[1].source_identity
    assert after[1].ticket_offset == before[1].ticket_offset
    assert after[1].ticket_identity == before[1].ticket_identity


@pytest.mark.asyncio
async def test_disconnect_resets_fold_pair_occurrence_epoch_as_one_unit(
    tmp_path: Path,
) -> None:
    session = _make_session()
    transcript = tmp_path / "before-disconnect.jsonl"
    transcript.write_text('{"type":"system"}\n', encoding="utf-8")
    prompt = "same prompt after disconnect"
    original = _seed_inflight(session, prompt=prompt)
    _bind_ticket(original, transcript)
    _emit_live(session, transcript, _queue_entry("enqueue", prompt))
    first_occurrence_id = session._pane_queue_operations[-1].occurrence_id
    _emit_live(session, transcript, _queue_entry("remove", prompt))
    session._purge_fold_removes_for_turn(original.turn)

    await session.disconnect()

    assert not session._pane_queue_operations
    assert not session._pane_fold_attachments
    assert not session._pane_fold_removes
    assert session._pane_fold_attachment_bytes == 0
    assert session._pane_fold_remove_bytes == 0

    replacement = tmp_path / "after-disconnect.jsonl"
    replacement.write_text('{"type":"system"}\n', encoding="utf-8")
    replay = _seed_inflight(session, prompt=prompt)
    _bind_ticket(replay, replacement)
    _emit_live(session, replacement, _queue_entry("enqueue", prompt))
    reset_occurrence_id = session._pane_queue_operations[-1].occurrence_id
    _emit_live(session, replacement, _queue_entry("remove", prompt))
    _emit_live(session, replacement, _attachment_entry(prompt))

    assert reset_occurrence_id == first_occurrence_id
    assert replay.turn.transport_accepted is True


@pytest.mark.asyncio
async def test_requeue_decision_logs_repr_quoted_first_line_prompt_header(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    session = _make_session()
    transcript = tmp_path / "requeue-log.jsonl"
    transcript.write_text('{"type":"system"}\n', encoding="utf-8")
    session._tailer = MagicMock()
    session._tailer.transcript_path = transcript
    prompt = "visible first line\nhidden later line"
    seeded = _seed_inflight(session, prompt=prompt)
    _bind_ticket(seeded, transcript)
    logs: list[str] = []
    monkeypatch.setattr(watchdog_log, "_log", logs.append)

    _enable_fast_idle_reconcile(monkeypatch, session)
    await _run_until_requeued(session)

    rendered = next(line for line in logs if "reason=phantom_requeued_unconsumed" in line)
    assert rendered.endswith("prompt_header='visible first line'")
    assert "hidden later line" not in rendered


def test_replay_purge_covers_attachment_first_partial_cancel_and_refold(
    tmp_path: Path,
) -> None:
    observed: dict[str, tuple[bool, list[bool | None]]] = {}
    for fresh_attachment in (False, True):
        flavor = "refold" if fresh_attachment else "cancel"
        session = _make_session()
        transcript = tmp_path / f"partial-{flavor}.jsonl"
        transcript.write_text('{"type":"system"}\n', encoding="utf-8")
        prompt = f"attachment-first partial before {flavor}"
        seeded = _seed_inflight(session, prompt=prompt)
        _bind_ticket(seeded, transcript)

        _emit_live(session, transcript, _queue_entry("enqueue", prompt))
        _emit_live(session, transcript, _attachment_entry(prompt))
        assert len(session._pane_queue_operations) == 1
        assert len(session._pane_fold_attachments) == 1
        assert not session._pane_fold_removes

        session._purge_fold_removes_for_turn(seeded.turn)
        _bind_ticket(seeded, transcript)
        seeded.turn.replay_count = 1
        seeded.turn.pane_delivery_started = True
        seeded.turn.pane_queue_enqueued = False

        _emit_live(session, transcript, _queue_entry("enqueue", prompt))
        if fresh_attachment:
            _emit_live(session, transcript, _attachment_entry(prompt))
        _emit_live(session, transcript, _queue_entry("remove", prompt))

        live_accepted = seeded.turn.transport_accepted
        seeded.turn.transport_accepted = False
        probe_verdict = session._phantom_consumption_verdicts([seeded])
        observed[flavor] = (live_accepted, probe_verdict)
        _assert_fold_pair_byte_counters(session)

    assert observed == {
        "cancel": (False, [False]),
        "refold": (False, [True]),
    }


def test_incomplete_full_history_for_fold_candidate_requires_replay(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(tmux_session, "_PHANTOM_TRANSCRIPT_SCAN_BYTES", 256)
    session = _make_session()
    transcript = tmp_path / "over-budget-fold-history.jsonl"
    transcript.write_text('{"type":"system"}\n', encoding="utf-8")
    seeded = _seed_inflight(session, prompt="fold candidate beyond scan budget")
    _bind_ticket(seeded, transcript)
    for index in range(8):
        _append_entry(
            transcript,
            {
                "type": "progress",
                "index": index,
                "payload": "x" * 48,
            },
        )

    assert transcript.stat().st_size > 256
    assert session._phantom_consumption_verdicts([seeded]) == [False]


def test_sibling_source_budget_starvation_requires_replay(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    scan_budget = 256
    monkeypatch.setattr(
        tmux_session,
        "_PHANTOM_TRANSCRIPT_SCAN_BYTES",
        scan_budget,
    )
    session = _make_session()
    spender_transcript = tmp_path / "budget-spender.jsonl"
    starved_transcript = tmp_path / "budget-starved.jsonl"
    for transcript in (spender_transcript, starved_transcript):
        transcript.write_text('{"type":"system"}\n', encoding="utf-8")

    spender = _seed_inflight(
        session,
        prompt="fold candidate whose source spends the shared budget",
        message_id="spender",
    )
    _bind_ticket(spender, spender_transcript)
    starved = _seed_inflight(
        session,
        prompt="fold candidate on a later available source",
        message_id="starved",
    )
    _bind_ticket(starved, starved_transcript)
    for index in range(8):
        _append_entry(
            spender_transcript,
            {
                "type": "progress",
                "index": index,
                "payload": "x" * 48,
            },
        )

    assert spender_transcript.stat().st_size > scan_budget
    assert session._phantom_consumption_verdicts([spender, starved]) == [
        False,
        False,
    ]


@pytest.mark.parametrize(
    ("partial_kind", "expected"),
    [
        pytest.param("budget-clipped", False, id="budget-clipped"),
        pytest.param("exact-eof-at-budget", None, id="exact-eof-at-budget"),
        pytest.param("budget-minus-one", None, id="budget-minus-one"),
        pytest.param("genuine-partial", None, id="genuine-partial"),
    ],
)
def test_first_row_partial_distinguishes_budget_starvation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    partial_kind: str,
    expected: bool | None,
) -> None:
    scan_budget = 128
    monkeypatch.setattr(
        tmux_session,
        "_PHANTOM_TRANSCRIPT_SCAN_BYTES",
        scan_budget,
    )
    session = _make_session()
    transcript = tmp_path / f"first-row-{partial_kind}.jsonl"
    transcript.write_text('{"type":"system"}\n', encoding="utf-8")
    seeded = _seed_inflight(session, prompt="fold candidate with partial first row")
    _bind_ticket(seeded, transcript)
    post_anchor_budget = scan_budget - transcript.stat().st_size
    partial_size = {
        "budget-clipped": post_anchor_budget + 20,
        "exact-eof-at-budget": post_anchor_budget,
        "budget-minus-one": post_anchor_budget - 1,
        "genuine-partial": 20,
    }[partial_kind]
    assert partial_size > 0
    with transcript.open("ab") as handle:
        handle.write(b"x" * partial_size)
    assert (transcript.stat().st_size > scan_budget) is (partial_kind == "budget-clipped")

    assert session._phantom_consumption_verdicts([seeded]) == [expected]


def test_genuinely_unavailable_fold_source_preserves_legacy_drain(
    tmp_path: Path,
) -> None:
    session = _make_session()
    transcript = tmp_path / "unavailable-fold-source.jsonl"
    transcript.write_text('{"type":"system"}\n', encoding="utf-8")
    seeded = _seed_inflight(session, prompt="fold candidate with missing source")
    _bind_ticket(seeded, transcript)
    transcript.unlink()

    assert session._phantom_consumption_verdicts([seeded]) == [None]


def test_non_fold_budget_exhaustion_preserves_legacy_drain(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    scan_budget = 128
    monkeypatch.setattr(
        tmux_session,
        "_PHANTOM_TRANSCRIPT_SCAN_BYTES",
        scan_budget,
    )
    session = _make_session()
    transcript = tmp_path / "non-fold-over-budget.jsonl"
    transcript.write_text('{"type":"system"}\n', encoding="utf-8")
    seeded = _seed_inflight(session, prompt="")
    _bind_ticket(seeded, transcript)
    for index in range(4):
        _append_entry(
            transcript,
            {
                "type": "progress",
                "index": index,
                "payload": "x" * 48,
            },
        )

    assert transcript.stat().st_size > scan_budget
    assert session._phantom_consumption_verdicts([seeded]) == [None]


def test_probe_refold_fresh_chain_survives_prior_equal_prompt_attempts(
    tmp_path: Path,
) -> None:
    session = _make_session()
    transcript = tmp_path / "fresh-chain-after-prior-attempts.jsonl"
    transcript.write_text('{"type":"system"}\n', encoding="utf-8")
    prompt = "fresh refold after prior equal prompt attempts"

    _append_fold_chain(transcript, prompt, attachment_first=False)
    _append_fold_chain(transcript, prompt, attachment_first=True)
    seeded = _seed_inflight(session, prompt=prompt)
    seeded.turn.replay_count = 1
    _bind_ticket(seeded, transcript)
    _append_fold_chain(transcript, prompt, attachment_first=False)

    assert session._phantom_consumption_verdicts([seeded]) == [True]


@pytest.mark.asyncio
async def test_p1_cancel_remove_without_attachment_never_certifies(
    tmp_path: Path,
) -> None:
    session = _make_session()
    transcript = tmp_path / "cancel-negative.jsonl"
    transcript.write_text('{"type":"system"}\n', encoding="utf-8")
    receipt = asyncio.get_running_loop().create_future()
    seeded = _seed_inflight(
        session,
        prompt="cancel without attachment",
        submission_receipt=receipt,
    )
    _bind_ticket(seeded, transcript)

    _emit_live(session, transcript, _queue_entry("enqueue", seeded.turn.prompt))
    _emit_live(session, transcript, _queue_entry("remove", seeded.turn.prompt))

    assert seeded.turn.transport_accepted is False
    assert not receipt.done()


@pytest.mark.asyncio
async def test_p2_cancel_remove_stays_negative_and_requeues(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    session = _make_session()
    transcript = tmp_path / "cancel-phantom.jsonl"
    transcript.write_text('{"type":"system"}\n', encoding="utf-8")
    session._tailer = MagicMock()
    session._tailer.transcript_path = transcript
    seeded = _seed_inflight(session, prompt="cancel phantom")
    _bind_ticket(seeded, transcript)
    _append_entry(transcript, _queue_entry("enqueue", seeded.turn.prompt))
    _append_entry(transcript, _queue_entry("remove", seeded.turn.prompt))

    assert session._phantom_consumption_verdicts([seeded]) == [False]
    _enable_fast_idle_reconcile(monkeypatch, session)
    await _run_until_requeued(session)

    assert session._message_queue.get_nowait() is seeded.turn
    assert seeded.turn.replay_count == 1
    assert seeded.turn.transport_accepted is False


@pytest.mark.asyncio
async def test_p3_task_notification_remove_is_noop_and_does_not_desync(
    tmp_path: Path,
) -> None:
    session = _make_session()
    transcript = tmp_path / "task-notification.jsonl"
    transcript.write_text('{"type":"system"}\n', encoding="utf-8")
    receipt = asyncio.get_running_loop().create_future()
    seeded = _seed_inflight(
        session,
        prompt="real queued prompt",
        submission_receipt=receipt,
    )
    _bind_ticket(seeded, transcript)

    _emit_live(session, transcript, _queue_entry("enqueue", seeded.turn.prompt))
    _emit_live(
        session,
        transcript,
        _queue_entry("remove", "<task-notification>internal</task-notification>"),
    )
    _emit_live(session, transcript, _queue_entry("dequeue"))

    assert seeded.turn.transport_accepted is True
    assert receipt.result() is True


def test_p4_tool_result_echo_does_not_complete_remove_chain(tmp_path: Path) -> None:
    session = _make_session()
    transcript = tmp_path / "tool-result.jsonl"
    transcript.write_text('{"type":"system"}\n', encoding="utf-8")
    prompt = "prompt echoed only by a tool result"
    seeded = _seed_inflight(session, prompt=prompt)
    _bind_ticket(seeded, transcript)
    _emit_live(session, transcript, _queue_entry("enqueue", prompt))
    _emit_live(session, transcript, _queue_entry("remove", prompt))
    _emit_live(
        session,
        transcript,
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tool-echo",
                        "content": prompt,
                    }
                ],
            },
        },
    )

    assert seeded.turn.transport_accepted is False


def test_p5_nonqueued_attachment_does_not_complete_remove_chain(
    tmp_path: Path,
) -> None:
    session = _make_session()
    transcript = tmp_path / "other-attachment.jsonl"
    transcript.write_text('{"type":"system"}\n', encoding="utf-8")
    prompt = "prompt inside an unrelated attachment"
    seeded = _seed_inflight(session, prompt=prompt)
    _bind_ticket(seeded, transcript)
    _emit_live(session, transcript, _queue_entry("enqueue", prompt))
    _emit_live(session, transcript, _queue_entry("remove", prompt))
    _emit_live(
        session,
        transcript,
        _attachment_entry(prompt, attachment_type="image"),
    )

    assert seeded.turn.transport_accepted is False


def test_p6_queued_attachment_from_another_inode_never_certifies(
    tmp_path: Path,
) -> None:
    session = _make_session()
    transcript = tmp_path / "attachment-inode.jsonl"
    transcript.write_text('{"type":"system"}\n', encoding="utf-8")
    prompt = "attachment from replacement transcript"
    seeded = _seed_inflight(session, prompt=prompt)
    _bind_ticket(seeded, transcript)
    _emit_live(session, transcript, _queue_entry("enqueue", prompt))
    _emit_live(session, transcript, _queue_entry("remove", prompt))
    identity = seeded.transcript_file_identity_at_paste
    assert identity is not None
    _emit_live(
        session,
        transcript,
        _attachment_entry(prompt),
        source_identity=(identity[0], identity[1] + 1),
    )

    assert seeded.turn.transport_accepted is False


def test_p7_queued_attachment_before_paste_offset_never_certifies(
    tmp_path: Path,
) -> None:
    session = _make_session()
    transcript = tmp_path / "attachment-offset.jsonl"
    transcript.write_text('{"type":"system"}\n', encoding="utf-8")
    prompt = "attachment predates paste"
    seeded = _seed_inflight(session, prompt=prompt)
    _bind_ticket(seeded, transcript)
    _emit_live(session, transcript, _queue_entry("enqueue", prompt))
    _emit_live(session, transcript, _queue_entry("remove", prompt))
    assert seeded.transcript_offset_at_paste is not None
    _emit_live(
        session,
        transcript,
        _attachment_entry(prompt),
        entry_offset=seeded.transcript_offset_at_paste - 1,
    )

    assert seeded.turn.transport_accepted is False


def test_p8_remove_chain_without_ticket_fails_closed(tmp_path: Path) -> None:
    session = _make_session()
    transcript = tmp_path / "missing-ticket.jsonl"
    transcript.write_text('{"type":"system"}\n', encoding="utf-8")
    seeded = _seed_inflight(session, prompt="ticketless remove chain")

    _emit_fold_chain(
        session,
        transcript,
        seeded.turn.prompt,
        attachment_first=False,
    )

    assert seeded.turn.transport_accepted is False


def test_p9_remove_chain_without_entry_offset_fails_closed(tmp_path: Path) -> None:
    session = _make_session()
    transcript = tmp_path / "missing-offset.jsonl"
    transcript.write_text('{"type":"system"}\n', encoding="utf-8")
    prompt = "remove row without offset"
    seeded = _seed_inflight(session, prompt=prompt)
    _bind_ticket(seeded, transcript)
    _emit_live(session, transcript, _queue_entry("enqueue", prompt))
    _emit_live(
        session,
        transcript,
        _queue_entry("remove", prompt),
        entry_offset=None,
    )
    _emit_live(session, transcript, _attachment_entry(prompt))

    assert seeded.turn.transport_accepted is False


def test_p10_remove_chain_with_file_identity_mismatch_fails_closed(
    tmp_path: Path,
) -> None:
    session = _make_session()
    transcript = tmp_path / "remove-inode.jsonl"
    transcript.write_text('{"type":"system"}\n', encoding="utf-8")
    prompt = "remove from replacement transcript"
    seeded = _seed_inflight(session, prompt=prompt)
    _bind_ticket(seeded, transcript)
    identity = seeded.transcript_file_identity_at_paste
    assert identity is not None
    _emit_live(session, transcript, _queue_entry("enqueue", prompt))
    _emit_live(
        session,
        transcript,
        _queue_entry("remove", prompt),
        source_identity=(identity[0], identity[1] + 1),
    )
    _emit_live(session, transcript, _attachment_entry(prompt))

    assert seeded.turn.transport_accepted is False


def test_p11_codex_transports_remain_outside_claude_remove_chain(
    tmp_path: Path,
) -> None:
    assert not issubclass(CodexSession, TmuxSession)
    assert not hasattr(CodexSession, "_mark_transport_accepted")
    session = _make_session(session_type=CodexTmuxSession)
    transcript = tmp_path / "codex.jsonl"
    transcript.write_text('{"type":"system"}\n', encoding="utf-8")
    prompt = "Claude remove rows presented to Codex"
    seeded = _seed_inflight(session, prompt=prompt)
    _bind_ticket(seeded, transcript)

    session._on_transcript_entry(_queue_entry("enqueue", prompt))
    session._on_transcript_entry(_queue_entry("remove", prompt))
    session._on_transcript_entry(_attachment_entry(prompt))

    assert seeded.turn.transport_accepted is False


def test_p12_turn_opener_and_dequeue_acceptance_are_preserved(
    tmp_path: Path,
) -> None:
    session = _make_session()
    transcript = tmp_path / "preservation.jsonl"
    transcript.write_text('{"type":"system"}\n', encoding="utf-8")
    opener = _seed_inflight(session, prompt="ordinary turn opener", message_id="opener")
    dequeued = _seed_inflight(session, prompt="ordinary dequeue", message_id="dequeue")
    _bind_ticket(opener, transcript)
    _bind_ticket(dequeued, transcript)

    _emit_live(
        session,
        transcript,
        {
            "type": "user",
            "message": {"role": "user", "content": opener.turn.prompt},
        },
    )
    _emit_live(session, transcript, _queue_entry("enqueue", dequeued.turn.prompt))
    _emit_live(session, transcript, _queue_entry("dequeue"))

    assert opener.turn.transport_accepted is True
    assert dequeued.turn.transport_accepted is True


# #1281 fixture provenance: Claude Code 2.1.278, structure-only production
# witness, 2026-09-19. The 4-hex id is stable across the session, not a message
# identity. A mid-tool-call paste has NO user text row: all three queue-chain
# legs contain the same envelope, including the id on the closing tag.
def _cc_21278_envelope(raw: str) -> str:
    return f'<pasted_content id="077e">\n{raw}\n</pasted_content id="077e">'


def _cc_21278_chain(content: str, attachment_first: bool) -> list[dict]:
    enqueue = _queue_entry("enqueue", content)
    remove = {**_queue_entry("remove", content), "reason": "absorbed_mid_turn"}
    attachment = _attachment_entry(content)
    attachment["attachment"]["origin"] = {"kind": "human"}
    return [enqueue, attachment, remove] if attachment_first else [enqueue, remove, attachment]


@pytest.mark.parametrize("wrapped", [False, True], ids=["legacy", "cc-2.1.278"])
@pytest.mark.parametrize("attachment_first", [False, True])
@pytest.mark.asyncio
async def test_1281_idle_chain_drains_without_replay(
    wrapped: bool, attachment_first: bool, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    session = _make_session()
    transcript = tmp_path / "idle-envelope.jsonl"
    transcript.write_text('{"type":"system"}\n', encoding="utf-8")
    # Preserve raw boundary whitespace; only the two framing newlines go away.
    raw = " \n[ferry | dm | sender | msg_id:1281-a]\nSynthetic prompt.\n\n "
    completion = asyncio.Event()
    receipt = asyncio.get_running_loop().create_future()
    entry = _seed_inflight(session, prompt=raw, completion_event=completion,
                           submission_receipt=receipt)
    _bind_ticket(entry, transcript)
    content = _cc_21278_envelope(raw) if wrapped else raw
    for row in _cc_21278_chain(content, attachment_first):
        _append_entry(transcript, row)
    assert session._phantom_consumption_verdicts([entry]) == [True]
    _enable_fast_idle_reconcile(monkeypatch, session)
    await _run_until_reconciled(session)
    assert session._message_queue.empty()
    assert entry.turn.replay_count == 0
    assert entry.turn.transport_accepted is True
    assert completion.is_set()
    assert receipt.result() is True
    assert "verdict=verified_consumed" in capsys.readouterr().err


@pytest.mark.parametrize("wrapped", [False, True], ids=["legacy", "cc-2.1.278"])
@pytest.mark.parametrize("attachment_first", [False, True])
@pytest.mark.asyncio
async def test_1281_live_envelope_chain_certifies_receipts(
    wrapped: bool, attachment_first: bool, tmp_path: Path,
) -> None:
    session = _make_session()
    transcript = tmp_path / "live-envelope.jsonl"
    transcript.write_text('{"type":"system"}\n', encoding="utf-8")
    raw = "[ferry | dm | sender | msg_id:1281-live]\nSynthetic live prompt."
    delivery = asyncio.get_running_loop().create_future()
    receipt = asyncio.get_running_loop().create_future()
    entry = _seed_inflight(session, prompt=raw, scheduler_delivery=delivery,
                           submission_receipt=receipt)
    _bind_ticket(entry, transcript)
    content = _cc_21278_envelope(raw) if wrapped else raw
    rows = _cc_21278_chain(content, attachment_first)
    for row in rows[:2]:
        _emit_live(session, transcript, row)
        assert entry.turn.transport_accepted is False
    _emit_live(session, transcript, rows[2])
    assert entry.turn.transport_accepted is True
    assert delivery.result() is True
    assert receipt.result() is True
    session._registry.mark_turn_delivered.assert_called_once()
    assert not session._pane_queue_operations
    _assert_fold_pair_byte_counters(session)


@pytest.mark.asyncio
async def test_1281_standalone_envelope_user_row_control(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _make_session()
    transcript = tmp_path / "standalone-envelope.jsonl"
    transcript.write_text('{"type":"system"}\n', encoding="utf-8")
    entry = _seed_inflight(session, prompt="Standalone synthetic control")
    _bind_ticket(entry, transcript)
    _append_entry(transcript, {"type": "user", "message": {
        "role": "user", "content": "\n\n" + _cc_21278_envelope(entry.turn.prompt) + "\n",
    }})
    assert session._phantom_consumption_verdicts([entry]) == [True]
    _enable_fast_idle_reconcile(monkeypatch, session)
    await _run_until_reconciled(session)
    assert session._message_queue.empty()
    assert entry.turn.replay_count == 0
    assert entry.turn.transport_accepted is True


@pytest.mark.parametrize("unconsumed", [False, True])
@pytest.mark.asyncio
async def test_1281_production_mix_two_rows_three_chains(
    unconsumed: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    session = _make_session()
    transcript = tmp_path / "production-mix.jsonl"
    transcript.write_text('{"type":"system"}\n', encoding="utf-8")
    entries = []
    for index in range(5):
        entry = _seed_inflight(session, prompt=f"Synthetic prompt {index}",
                               message_id=f"1281-{index}", completion_event=asyncio.Event())
        _bind_ticket(entry, transcript)
        entries.append(entry)
        content = _cc_21278_envelope(entry.turn.prompt)
        if index in (1, 4):
            _append_entry(transcript, {"type": "user", "message": {
                "role": "user", "content": "\n\n" + content + "\n",
            }})
        else:
            for row in _cc_21278_chain(content, attachment_first=index == 0):
                _append_entry(transcript, row)
    absent = None
    if unconsumed:
        absent = _seed_inflight(session, prompt="Genuinely absent prompt",
                                 message_id="1281-absent", completion_event=asyncio.Event())
        _bind_ticket(absent, transcript)
    _enable_fast_idle_reconcile(monkeypatch, session)
    await _run_until_reconciled(session)
    assert all(entry.turn.transport_accepted for entry in entries)
    assert all(entry.turn.replay_count == 0 for entry in entries)
    assert all(entry.completion_event.is_set() for entry in entries)
    if absent is not None:
        assert session._message_queue.get_nowait() is absent.turn
        assert absent.turn.replay_count == 1
        assert absent.turn.transport_accepted is False
        assert not absent.completion_event.is_set()
    assert session._message_queue.empty()
    output = capsys.readouterr().err
    assert output.count("verdict=verified_consumed") == 5


@pytest.mark.parametrize("variant", [
    "mismatched-id", "nested", "idless-close", "wrong-close", "nonhex-id",
    "long-id", "missing-open-newline", "missing-close-newline", "inner-byte",
    "two-envelopes", "prefix", "suffix",
])
def test_1281_malformed_or_unequal_chain_is_not_consumption(
    variant: str, tmp_path: Path,
) -> None:
    raw = "Synthetic exact prompt"
    content = _cc_21278_envelope(raw)
    if variant == "mismatched-id":
        content = content.replace('</pasted_content id="077e">', '</pasted_content id="077f">')
    elif variant == "nested":
        content = _cc_21278_envelope(content)
    elif variant == "idless-close":
        content = content.replace('</pasted_content id="077e">', '</pasted_content>')
    elif variant == "wrong-close":
        content = content.replace("</pasted_content", "</other_content")
    elif variant == "nonhex-id":
        content = content.replace("077e", "zzzz")
    elif variant == "long-id":
        content = content.replace("077e", "0077e")
    elif variant == "missing-open-newline":
        content = content.replace('>\n', '>', 1)
    elif variant == "missing-close-newline":
        content = content.replace('\n</', '</', 1)
    elif variant == "inner-byte":
        content = _cc_21278_envelope(raw + ".")
    elif variant == "two-envelopes":
        content += "\n" + content
    elif variant == "prefix":
        content = "prefix " + content
    elif variant == "suffix":
        content += " suffix"
    session = _make_session()
    transcript = tmp_path / "invalid-envelope.jsonl"
    transcript.write_text('{"type":"system"}\n', encoding="utf-8")
    entry = _seed_inflight(session, prompt=raw)
    _bind_ticket(entry, transcript)
    for row in _cc_21278_chain(content, attachment_first=True):
        _emit_live(session, transcript, row)
    assert entry.turn.transport_accepted is False
    assert session._phantom_consumption_verdicts([entry]) == [False]
    session._registry.mark_turn_delivered.assert_not_called()


@pytest.mark.parametrize("live", [False, True])
def test_1281_one_envelope_chain_cannot_certify_equal_prompt_twin(
    live: bool, tmp_path: Path,
) -> None:
    session = _make_session()
    transcript = tmp_path / "twins.jsonl"
    transcript.write_text('{"type":"system"}\n', encoding="utf-8")
    first = _seed_inflight(session, prompt="Equal prompt")
    second = _seed_inflight(session, prompt="Equal prompt")
    for entry in (first, second):
        _bind_ticket(entry, transcript)
    for row in _cc_21278_chain(_cc_21278_envelope(first.turn.prompt), True):
        if live:
            _emit_live(session, transcript, row)
        else:
            _append_entry(transcript, row)
    if live:
        assert first.turn.transport_accepted is True
        assert second.turn.transport_accepted is False
    assert session._phantom_consumption_verdicts([first, second]) == [True, False]


@pytest.mark.parametrize("live", [False, True])
def test_1281_literal_envelope_prompt_keeps_legacy_equality(
    live: bool, tmp_path: Path,
) -> None:
    session = _make_session()
    transcript = tmp_path / "literal-envelope.jsonl"
    transcript.write_text('{"type":"system"}\n', encoding="utf-8")
    # These are the actual queued bytes, not a wrapper around the queued bytes.
    raw = _cc_21278_envelope("Literal markup supplied by caller")
    entry = _seed_inflight(session, prompt=raw)
    _bind_ticket(entry, transcript)
    for row in _cc_21278_chain(raw, False):
        if live:
            _emit_live(session, transcript, row)
        else:
            _append_entry(transcript, row)
    if live:
        assert entry.turn.transport_accepted is True
    assert session._phantom_consumption_verdicts([entry]) == [True]


def test_1281_pre_ticket_envelope_chain_cannot_certify_later_paste(tmp_path: Path) -> None:
    session = _make_session()
    transcript = tmp_path / "old-envelope.jsonl"
    transcript.write_text('{"type":"system"}\n', encoding="utf-8")
    raw = "Same text, later paste"
    for row in _cc_21278_chain(_cc_21278_envelope(raw), True):
        _append_entry(transcript, row)
    entry = _seed_inflight(session, prompt=raw)
    _bind_ticket(entry, transcript)
    assert session._phantom_consumption_verdicts([entry]) == [False]


@pytest.mark.parametrize("valid", [False, True])
def test_1281_only_valid_envelope_gets_exact_user_row_priority(
    valid: bool, tmp_path: Path,
) -> None:
    session = _make_session()
    transcript = tmp_path / "exact-priority.jsonl"
    transcript.write_text('{"type":"system"}\n', encoding="utf-8")
    short = _seed_inflight(session, prompt="alpha")
    long = _seed_inflight(session, prompt="alpha beta")
    for entry in (short, long):
        _bind_ticket(entry, transcript)
    content = "\n\n" + _cc_21278_envelope(long.turn.prompt) + "\n"
    if not valid:
        content = content.replace('</pasted_content id="077e">', '</pasted_content>')
    _append_entry(transcript, {"type": "user", "message": {"content": content}})
    # Valid envelope is exact evidence for long before short can take its span.
    # Malformed envelope remains ordinary containment: oldest span wins as before.
    assert session._phantom_consumption_verdicts([short, long]) == (
        [False, True] if valid else [True, False]
    )


@pytest.mark.parametrize("valid", [False, True])
@pytest.mark.parametrize("dequeued", [False, True])
def test_1281_retirement_matches_only_valid_envelope_identity(
    valid: bool, dequeued: bool, tmp_path: Path,
) -> None:
    session = _make_session()
    transcript = tmp_path / "retirement.jsonl"
    transcript.write_text('{"type":"system"}\n', encoding="utf-8")
    raw = "Retired original occurrence"
    content = _cc_21278_envelope(raw)
    if not valid:
        content = content.replace('</pasted_content id="077e">', '</pasted_content id="abcd">')
    # Transcript arrives before there is an owner: test the fallback tombstone.
    _emit_live(session, transcript, _queue_entry("enqueue", content))
    if dequeued:
        _emit_live(session, transcript, _queue_entry("dequeue"))
    entry = _seed_inflight(session, prompt=raw)
    _bind_ticket(entry, transcript)
    session._retire_acceptance_evidence(entry.turn)
    evidence = (session._pane_dequeued_turns if dequeued else session._pane_queue_operations)[0]
    assert evidence.retired is valid
    assert evidence.content == content
    assert entry.turn.transport_accepted is False


@pytest.mark.parametrize("valid", [False, True])
def test_1281_late_wake_guard_requires_valid_envelope_identity(
    valid: bool, tmp_path: Path,
) -> None:
    session = _make_session()
    transcript = tmp_path / "late-wake.jsonl"
    transcript.write_text('{"type":"system"}\n', encoding="utf-8")
    entry = _seed_inflight(session, prompt="Original orientation wake")
    _bind_ticket(entry, transcript)
    guard = tmux_session._WakeContextReloadGuard(entry.turn, "Recovery instruction")
    session._wake_context_reload_guard = guard
    content = _cc_21278_envelope(entry.turn.prompt)
    if not valid:
        content = _cc_21278_envelope(content)
    _emit_live(session, transcript, {"type": "user", "message": {"content": content}})
    assert guard.original_seen is valid


@pytest.mark.parametrize("valid", [False, True])
def test_1281_enqueue_ownership_requires_valid_envelope_identity(
    valid: bool, tmp_path: Path,
) -> None:
    session = _make_session()
    transcript = tmp_path / "enqueue-owner.jsonl"
    transcript.write_text('{"type":"system"}\n', encoding="utf-8")
    entry = _seed_inflight(session, prompt="Owned enqueue")
    _bind_ticket(entry, transcript)
    content = _cc_21278_envelope(entry.turn.prompt)
    if not valid:
        content = content.replace('</pasted_content id="077e">', '</pasted_content>')
    _emit_live(session, transcript, _queue_entry("enqueue", content))
    evidence = session._pane_queue_operations[0]
    assert evidence.turn is (entry.turn if valid else None)
    assert entry.turn.pane_queue_enqueued is valid
    assert entry.turn.transport_accepted is False  # Enqueue alone is not consumption.
    assert evidence.content == content  # Raw leg identity never gets normalized.


def test_1281_different_envelope_legs_cannot_form_a_chain(tmp_path: Path) -> None:
    session = _make_session()
    transcript = tmp_path / "unequal-legs.jsonl"
    transcript.write_text('{"type":"system"}\n', encoding="utf-8")
    entry = _seed_inflight(session, prompt="Equal inner text is not equal leg identity")
    _bind_ticket(entry, transcript)
    envelope = _cc_21278_envelope(entry.turn.prompt)
    for row in (
        _queue_entry("enqueue", envelope),
        _attachment_entry(envelope.replace("077e", "abcd")),
        _queue_entry("remove", envelope),
    ):
        _emit_live(session, transcript, row)
    assert entry.turn.transport_accepted is False
    assert session._phantom_consumption_verdicts([entry]) == [False]


@pytest.mark.parametrize("boundary", ["post-ticket", "pre-ticket", "missing-ticket", "wrong-source"])
def test_1281_live_envelope_user_row_preserves_paste_boundary(
    boundary: str, tmp_path: Path,
) -> None:
    session = _make_session()
    transcript = tmp_path / "live-row-boundary.jsonl"
    transcript.write_text('{"type":"system"}\n', encoding="utf-8")
    raw = "Same prompt in an old or new user row"
    row = {"type": "user", "message": {
        "content": "\n\n" + _cc_21278_envelope(raw) + "\n",
    }}
    if boundary == "pre-ticket":
        row_offset = _append_entry(transcript, row)
    entry = _seed_inflight(session, prompt=raw)
    if boundary != "missing-ticket":
        _bind_ticket(entry, transcript)
    if boundary != "pre-ticket":
        row_offset = _append_entry(transcript, row)
    stat = transcript.stat()
    identity = (stat.st_dev, stat.st_ino + (boundary == "wrong-source"))
    session._on_transcript_entry(row, entry_offset=row_offset, source_identity=identity)
    assert entry.turn.transport_accepted is (boundary == "post-ticket")
    assert session._registry.mark_turn_delivered.call_count == (boundary == "post-ticket")


@pytest.mark.parametrize("before_meta", [False, True], ids=["recorded", "pasting"])
@pytest.mark.parametrize("boundary", ["pre-ticket", "wrong-source", "post-ticket"])
def test_1290_bare_user_row_requires_turn_ticket(
    boundary: str, before_meta: bool, tmp_path: Path,
) -> None:
    session = _make_session()
    transcript = tmp_path / "bare-boundary.jsonl"
    transcript.write_text('{"type":"system"}\n', encoding="utf-8")
    entry = _seed_inflight(session, prompt="Repeated bare prompt")
    row = {"type": "user", "message": {"content": entry.turn.prompt}}
    if boundary == "pre-ticket":
        row_offset = _append_entry(transcript, row)
    _bind_ticket(entry, transcript)
    if boundary != "pre-ticket":
        row_offset = _append_entry(transcript, row)
    if before_meta:
        session._inflight_metas.clear()
        session._inflight_turn = entry.turn
        assert session._inflight_meta_for_turn(entry.turn) is None
    stat = transcript.stat()
    identity = (stat.st_dev, stat.st_ino + (boundary == "wrong-source"))
    session._on_transcript_entry(row, entry_offset=row_offset, source_identity=identity)
    assert entry.turn.transport_accepted is (boundary == "post-ticket")
    assert session._registry.mark_turn_delivered.call_count == (boundary == "post-ticket")


@pytest.mark.parametrize("shape", ["unbound", "inaccessible"])
def test_1290_bare_user_row_without_ticket_rejects_and_warns_once(
    shape: str, tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    session = _make_session()
    transcript = tmp_path / "missing-ticket.jsonl"
    entry = _seed_inflight(session, prompt="Prompt without provenance")
    if shape == "inaccessible":
        entry.turn.transcript_path_at_paste = transcript
    row = {"type": "user", "message": {"content": entry.turn.prompt}}
    row_offset = _emit_live(session, transcript, row)
    _emit_live(session, transcript, row)
    assert entry.turn.transport_accepted is False
    session._registry.mark_turn_delivered.assert_not_called()
    assert list(session._inflight_metas) == [entry]
    warnings = [line for line in capsys.readouterr().err.splitlines()
                if "reason='no paste ticket'" in line]
    assert len(warnings) == 1
    assert "WARNING" in warnings[0]
    assert f"turn_id={id(entry.turn)}" in warnings[0]
    assert f"entry_offset={row_offset}" in warnings[0]
    assert f"shape={shape}" in warnings[0]


@pytest.mark.asyncio
async def test_1290_equal_prompt_twins_consume_only_ticketed_occurrence(tmp_path: Path) -> None:
    session = _make_session()
    transcript = tmp_path / "bare-twins.jsonl"
    transcript.write_text('{"type":"system"}\n', encoding="utf-8")
    first_receipt = asyncio.get_running_loop().create_future()
    second_receipt = asyncio.get_running_loop().create_future()
    first = _seed_inflight(session, prompt="Equal queued prompt", message_id="first",
                           scheduler_delivery=first_receipt, completion_event=asyncio.Event())
    _bind_ticket(first, transcript)
    row = {"type": "user", "message": {"content": first.turn.prompt}}
    row_offset = _append_entry(transcript, row)
    second = _seed_inflight(session, prompt=first.turn.prompt, message_id="second",
                            scheduler_delivery=second_receipt, completion_event=asyncio.Event())
    _bind_ticket(second, transcript)
    identity = first.turn.transcript_file_identity_at_paste
    for _ in range(2):
        session._on_transcript_entry(row, entry_offset=row_offset, source_identity=identity)
    assert first.turn.transport_accepted is True
    assert second.turn.transport_accepted is False
    assert first_receipt.result() is True
    assert not second_receipt.done()
    assert not second.completion_event.is_set()
    assert any(meta.turn is second.turn for meta in session._inflight_metas)
    session._registry.mark_turn_delivered.assert_called_once_with(
        session.agent_name, "telegram", "chat", "first", source="telegram",
    )


def test_1290_cold_start_bare_user_row_accepts_with_warning(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    session = _make_session()
    transcript = tmp_path / "cold-start.jsonl"
    entry = _seed_inflight(session, prompt="First prompt after spawn")
    session._tailer = MagicMock(transcript_path=transcript)
    ticket = session._capture_transcript_occurrence_ticket()
    (entry.turn.transcript_path_at_paste,
     entry.turn.transcript_file_identity_at_paste,
     entry.turn.transcript_offset_at_paste) = ticket
    assert tuple(ticket) == (transcript, None, 0)
    row = {"type": "user", "message": {"content": entry.turn.prompt}}
    row_offset = _emit_live(session, transcript, row)
    _emit_live(session, transcript, row)
    assert entry.turn.transport_accepted is True
    session._registry.mark_turn_delivered.assert_called_once()
    warnings = [line for line in capsys.readouterr().err.splitlines()
                if "reason='cold_start_ticket_unverified'" in line]
    assert len(warnings) == 1
    assert "WARNING" in warnings[0]
    assert f"turn_id={id(entry.turn)}" in warnings[0]
    assert f"entry_offset={row_offset}" in warnings[0]
    assert "shape=cold-start" in warnings[0]
    assert "cold_start_reason='file_missing'" in warnings[0]


@pytest.mark.parametrize("before_meta", [False, True], ids=["recorded", "pasting"])
@pytest.mark.parametrize("boundary", [
    "pre-ticket", "wrong-source", "post-ticket", "unbound", "inaccessible", "cold-start",
])
def test_1290_enveloped_user_row_uses_same_turn_ticket(
    boundary: str, before_meta: bool, tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    session = _make_session()
    transcript = tmp_path / "envelope-ticket.jsonl"
    transcript.write_text('{"type":"system"}\n', encoding="utf-8")
    entry = _seed_inflight(session, prompt="Enveloped prompt during paste")
    row = {"type": "user", "message": {
        "content": _cc_21278_envelope(entry.turn.prompt),
    }}
    if boundary == "pre-ticket":
        row_offset = _append_entry(transcript, row)
    if boundary in {"pre-ticket", "wrong-source", "post-ticket"}:
        _bind_ticket(entry, transcript)
    elif boundary != "unbound":
        entry.turn.transcript_path_at_paste = transcript
        if boundary == "cold-start":
            entry.turn.transcript_offset_at_paste = 0
    if boundary != "pre-ticket":
        row_offset = _append_entry(transcript, row)
    if before_meta:
        session._inflight_metas.clear()
        session._inflight_turn = entry.turn
    stat = transcript.stat()
    identity = (stat.st_dev, stat.st_ino + (boundary == "wrong-source"))
    for _ in range(2):
        session._on_transcript_entry(row, entry_offset=row_offset, source_identity=identity)
    accepted = boundary in {"post-ticket", "cold-start"}
    assert entry.turn.transport_accepted is accepted
    assert session._registry.mark_turn_delivered.call_count == accepted
    warnings = [line for line in capsys.readouterr().err.splitlines() if "WARNING" in line]
    if boundary in {"unbound", "inaccessible", "cold-start"}:
        assert len(warnings) == 1
        assert f"shape={boundary}" in warnings[0]
        reason = "cold_start_ticket_unverified" if boundary == "cold-start" else "no paste ticket"
        assert f"reason={reason!r}" in warnings[0]
    else:
        assert warnings == []


def test_1290_placeholder_capture_is_a_cold_start_ticket() -> None:
    session = _make_session()
    session._tailer = MagicMock(transcript_path=tmux_session._PLACEHOLDER_TRANSCRIPT_PATH)
    ticket = session._capture_transcript_occurrence_ticket()
    assert tuple(ticket) == (tmux_session._PLACEHOLDER_TRANSCRIPT_PATH, None, 0)
    assert ticket.anchor_start == 0
    assert ticket.anchor == b""
    assert ticket.captured_at_ns is not None


@pytest.mark.asyncio
async def test_1290_placeholder_early_paste_accepts_with_warning(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    session = _make_session()
    session._tailer = MagicMock(transcript_path=tmux_session._PLACEHOLDER_TRANSCRIPT_PATH)
    turn = _QueuedTurn(prompt="Ordinary prompt before first bind", message_id="early")
    session._inflight_turn = turn
    await session._deliver_turn(turn)
    assert not session._session_ready_event.is_set()
    transcript = tmp_path / "first-materialized.jsonl"
    row_offset = _emit_live(session, transcript, {
        "type": "user", "message": {"content": turn.prompt},
    })
    assert turn.transport_accepted is True
    session._registry.mark_turn_delivered.assert_called_once()
    warnings = [line for line in capsys.readouterr().err.splitlines()
                if "reason='cold_start_ticket_unverified'" in line]
    assert len(warnings) == 1
    assert "WARNING" in warnings[0]
    assert f"turn_id={id(turn)}" in warnings[0]
    assert f"entry_offset={row_offset}" in warnings[0]
    assert "cold_start_reason='placeholder'" in warnings[0]
