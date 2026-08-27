"""Paste-boundary provenance pins for live folded acceptance (#1163)."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from pinky_daemon.codex_tmux_session import CodexTmuxSession
from pinky_daemon.streaming_session import StreamingSessionConfig
from pinky_daemon.tmux_session import (
    TmuxCommandResult,
    TmuxSession,
    _InflightMeta,
    _QueuedTurn,
    _TmuxControl,
)
from pinky_daemon.tmux_transcript import TmuxTranscriptTailer
from pinky_daemon.transport_state import SessionState


def _ok() -> TmuxCommandResult:
    return TmuxCommandResult(returncode=0, stdout="", stderr="")


def _mock_tmux() -> MagicMock:
    control = MagicMock(spec=_TmuxControl)
    control.session_name = "pinky-fold-provenance"
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


def _make_session(
    *,
    registry: object | None = None,
    session_type: type[TmuxSession] = TmuxSession,
) -> TmuxSession:
    session = session_type(
        StreamingSessionConfig(
            agent_name="dymok",
            working_dir="/tmp/tmux-fold-provenance",
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
    platform: str = "telegram",
    chat_id: str = "chat",
    message_id: str = "message",
) -> _InflightMeta:
    turn = _QueuedTurn(
        prompt=prompt,
        scheduler_delivery=scheduler_delivery,
        submission_receipt=submission_receipt,
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
        completion_event=None,
        internal=False,
        dispatched_at=time.time(),
        turn=turn,
    )
    session._inflight_metas.append(entry)
    return entry


def _bind_ticket(entry: _InflightMeta, transcript: Path) -> None:
    stat = transcript.stat()
    offset = stat.st_size
    anchor_start = max(0, offset - 4096)
    anchor = transcript.read_bytes()[anchor_start:offset]
    entry.transcript_path_at_paste = transcript
    entry.transcript_file_identity_at_paste = (stat.st_dev, stat.st_ino)
    entry.transcript_offset_at_paste = offset
    entry.transcript_anchor_start_at_paste = anchor_start
    entry.transcript_anchor_at_paste = anchor
    entry.transcript_ticket_captured_at_ns = time.time_ns()


def _folded_user_entry(prompt: str) -> dict:
    return {
        "type": "user",
        "message": {"role": "user", "content": f"<fold>{prompt}</fold>"},
    }


def _append_entry(transcript: Path, entry: dict) -> None:
    with transcript.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry) + "\n")


async def _drive_tailer(
    session: TmuxSession,
    transcript: Path,
) -> None:
    tailer = TmuxTranscriptTailer(
        transcript,
        lambda _response: None,
        on_entry=session._on_transcript_entry,
    )
    session._tailer = tailer
    await tailer.read_once()


@pytest.mark.asyncio
async def test_live_fold_without_paste_ticket_fails_closed(
    tmp_path: Path,
) -> None:
    """A live row cannot certify a candidate with no readable paste ticket."""
    registry = MagicMock()
    session = _make_session(registry=registry)
    scheduler_receipt = asyncio.get_running_loop().create_future()
    submission_receipt = asyncio.get_running_loop().create_future()
    prompt = "ticketless folded prompt"
    seeded = _seed_inflight(
        session,
        prompt=prompt,
        scheduler_delivery=scheduler_receipt,
        submission_receipt=submission_receipt,
    )
    transcript = tmp_path / "ticketless.jsonl"
    _append_entry(transcript, _folded_user_entry(prompt))

    await _drive_tailer(session, transcript)

    assert seeded.turn.transport_accepted is False
    assert scheduler_receipt.done() is False
    assert submission_receipt.done() is False
    registry.mark_turn_delivered.assert_not_called()


@pytest.mark.asyncio
async def test_live_fold_from_mismatched_file_identity_fails_closed(
    tmp_path: Path,
) -> None:
    """A replacement inode cannot spend a ticket captured on the old file."""
    registry = MagicMock()
    session = _make_session(registry=registry)
    transcript = tmp_path / "replaced.jsonl"
    transcript.write_text('{"type":"system"}\n', encoding="utf-8")
    prompt = "folded prompt from replacement inode"
    seeded = _seed_inflight(session, prompt=prompt)
    _bind_ticket(seeded, transcript)
    ticket_identity = seeded.transcript_file_identity_at_paste
    replacement = tmp_path / "replacement.jsonl"
    replacement.write_text('{"type":"system"}\n', encoding="utf-8")
    replacement.replace(transcript)
    assert (transcript.stat().st_dev, transcript.stat().st_ino) != ticket_identity
    _append_entry(transcript, _folded_user_entry(prompt))

    await _drive_tailer(session, transcript)

    assert seeded.turn.transport_accepted is False
    registry.mark_turn_delivered.assert_not_called()


@pytest.mark.asyncio
async def test_live_fold_before_paste_offset_fails_closed(
    tmp_path: Path,
) -> None:
    """A complete row whose start predates the ticket cannot certify it."""
    registry = MagicMock()
    session = _make_session(registry=registry)
    prompt = "folded prompt already present before paste"
    transcript = tmp_path / "pre-ticket.jsonl"
    _append_entry(transcript, _folded_user_entry(prompt))
    seeded = _seed_inflight(session, prompt=prompt)
    _bind_ticket(seeded, transcript)
    assert seeded.transcript_offset_at_paste == transcript.stat().st_size

    await _drive_tailer(session, transcript)

    assert seeded.turn.transport_accepted is False
    registry.mark_turn_delivered.assert_not_called()


@pytest.mark.asyncio
async def test_same_read_straddle_rejects_old_fold_then_accepts_new_fold(
    tmp_path: Path,
) -> None:
    """Per-row offsets preserve a valid fold later in a pre-ticket chunk."""
    registry = MagicMock()
    session = _make_session(registry=registry)
    prompt = "folded prompt straddling one transcript read"
    transcript = tmp_path / "straddle.jsonl"
    _append_entry(transcript, _folded_user_entry(prompt))
    seeded = _seed_inflight(session, prompt=prompt)
    _bind_ticket(seeded, transcript)
    _append_entry(transcript, _folded_user_entry(prompt))
    observed: list[bool] = []

    def observe_entry(
        entry: dict,
        entry_offset: int | None = None,
        source_identity: tuple[int, int] | None = None,
    ) -> None:
        session._on_transcript_entry(
            entry,
            entry_offset=entry_offset,
            source_identity=source_identity,
        )
        observed.append(seeded.turn.transport_accepted)

    tailer = TmuxTranscriptTailer(
        transcript,
        lambda _response: None,
        on_entry=observe_entry,
    )
    session._tailer = tailer

    await tailer.read_once()

    assert observed == [False, True]
    registry.mark_turn_delivered.assert_called_once()


def test_codex_super_call_cannot_supply_claude_fold_provenance(
    tmp_path: Path,
) -> None:
    """Codex's entry-only super call leaves Claude fold matching inert."""
    registry = MagicMock()
    session = _make_session(
        registry=registry,
        session_type=CodexTmuxSession,
    )
    prompt = "folded Claude-shaped row presented through Codex"
    transcript = tmp_path / "codex.jsonl"
    transcript.write_text('{"type":"system"}\n', encoding="utf-8")
    seeded = _seed_inflight(session, prompt=prompt)
    _bind_ticket(seeded, transcript)

    session._on_transcript_entry(_folded_user_entry(prompt))

    assert seeded.turn.transport_accepted is False
    registry.mark_turn_delivered.assert_not_called()
