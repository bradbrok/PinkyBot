"""Adversarial reviewer probes for the #1163 folded-acceptance change."""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from pinky_daemon import tmux_session
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


def _make_session(*, registry: object | None = None) -> TmuxSession:
    control = MagicMock(spec=_TmuxControl)
    control.session_name = "pinky-review-probe"
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
    session = TmuxSession(
        StreamingSessionConfig(
            agent_name="dymok",
            working_dir="/tmp/tmux-fold-review-probe",
        ),
        registry=registry,
        tmux_control=control,
    )
    session._skip_wake_prompt_for_tests = True
    session._state_machine._state = SessionState.CONNECTED
    return session


def _seed_inflight(session: TmuxSession, *, prompt: str, **turn_kwargs) -> _InflightMeta:
    turn = _QueuedTurn(prompt=prompt, **turn_kwargs)
    turn.pane_delivery_started = True
    entry = _InflightMeta(
        meta={
            "platform": turn.platform,
            "chat_id": turn.chat_id,
            "message_id": turn.message_id,
        },
        completion_event=turn.completion_event,
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


def _append_entry(transcript: Path, entry: dict) -> None:
    with transcript.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry) + "\n")


def test_span_search_advances_through_chained_overlaps() -> None:
    """An overlapping first hit must not hide a later disjoint occurrence."""
    assert TmuxSession._first_unoccupied_prompt_span(
        "ababa", "ba", [(0, 3)]
    ) == (3, 5)
    assert TmuxSession._first_unoccupied_prompt_span(
        "aaaa", "aa", [(0, 2)]
    ) == (2, 4)


@pytest.mark.asyncio
async def test_live_fold_allocates_two_distinct_nonoverlapping_prompts(
    tmp_path: Path,
) -> None:
    """One live folded row can settle several disjoint pending occurrences."""
    transcript = tmp_path / "two-prompts.jsonl"
    transcript.write_text('{"type":"system"}\n', encoding="utf-8")
    session = _make_session()
    first = _seed_inflight(session, prompt="first folded prompt")
    second = _seed_inflight(session, prompt="second folded prompt")
    _bind_ticket(first, transcript)
    _bind_ticket(second, transcript)
    _append_entry(
        transcript,
        {
            "type": "user",
            "message": {
                "content": (
                    "<fold>first folded prompt</fold>"
                    "<fold>second folded prompt</fold>"
                )
            },
        },
    )
    tailer = TmuxTranscriptTailer(
        transcript,
        lambda _response: None,
        on_entry=session._on_transcript_entry,
    )

    await tailer.read_once()

    assert first.turn.transport_accepted is True
    assert second.turn.transport_accepted is True


def test_complete_fold_row_remains_positive_at_scan_budget_boundary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A fully read row stays usable even when the source scan is incomplete."""
    session = _make_session()
    transcript = tmp_path / "budget.jsonl"
    transcript.write_text('{"type":"system"}\n', encoding="utf-8")
    session._tailer = MagicMock()
    session._tailer.transcript_path = transcript
    prompt = "folded at the exact budget boundary"
    entry = _seed_inflight(session, prompt=prompt)
    _bind_ticket(entry, transcript)
    _append_entry(
        transcript,
        {"type": "user", "message": {"content": f"<fold>{prompt}</fold>"}},
    )
    post_ticket_bytes = transcript.stat().st_size - entry.transcript_offset_at_paste
    anchor_bytes = len(entry.transcript_anchor_at_paste or b"")
    monkeypatch.setattr(
        tmux_session,
        "_PHANTOM_TRANSCRIPT_SCAN_BYTES",
        anchor_bytes + post_ticket_bytes,
    )

    assert session._phantom_consumption_verdicts([entry]) == [True]


def test_partial_fold_row_at_scan_budget_boundary_stays_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A budget-clipped row means replay under end-cause discrimination."""
    session = _make_session()
    transcript = tmp_path / "partial-budget.jsonl"
    transcript.write_text('{"type":"system"}\n', encoding="utf-8")
    session._tailer = MagicMock()
    session._tailer.transcript_path = transcript
    prompt = "folded beyond the scan boundary"
    entry = _seed_inflight(session, prompt=prompt)
    _bind_ticket(entry, transcript)
    _append_entry(
        transcript,
        {"type": "user", "message": {"content": f"<fold>{prompt}</fold>"}},
    )
    anchor_bytes = len(entry.transcript_anchor_at_paste or b"")
    monkeypatch.setattr(
        tmux_session,
        "_PHANTOM_TRANSCRIPT_SCAN_BYTES",
        anchor_bytes + 8,
    )

    assert session._phantom_consumption_verdicts([entry]) == [False]


@pytest.mark.asyncio
async def test_live_fold_after_ticket_survives_a_chunk_that_starts_before_ticket(
    tmp_path: Path,
) -> None:
    """Use the row offset, not chunk start, when a read straddles the ticket."""
    prompt = "folded after ticket in a chunk with older backlog"
    transcript = tmp_path / "straddled.jsonl"
    transcript.write_text(
        json.dumps(
            {
                "type": "user",
                "message": {"content": "unrelated row before the paste ticket"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    session = _make_session()
    seeded = _seed_inflight(session, prompt=prompt)
    _bind_ticket(seeded, transcript)
    _append_entry(
        transcript,
        {"type": "user", "message": {"content": f"<fold>{prompt}</fold>"}},
    )
    tailer = TmuxTranscriptTailer(
        transcript,
        lambda _response: None,
        on_entry=session._on_transcript_entry,
    )
    assert tailer.offset == 0 < seeded.transcript_offset_at_paste

    await tailer.read_once()

    assert seeded.turn.transport_accepted is True


@pytest.mark.asyncio
async def test_delayed_live_callback_cannot_use_a_pre_ticket_fold_row(
    tmp_path: Path,
) -> None:
    """A row already on disk before paste cannot prove the new occurrence.

    The tailer reads a whole chunk before awaiting a turn callback.  A new paste
    can start during that await, after later rows in the chunk are already on
    disk but before their synchronous ``on_entry`` callbacks run.
    """
    prompt = "[telegram | dm | sender | chat | unique-message-id]\nrun once"
    transcript = tmp_path / "delayed.jsonl"
    rows = [
        {
            "type": "assistant",
            "message": {
                "content": [{"type": "text", "text": "prior response"}],
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        },
        {
            "type": "system",
            "subtype": "stop_hook_summary",
            "hookCount": 1,
            "hasOutput": False,
        },
        {
            "type": "user",
            "message": {
                "content": f"unrelated old row quoting <{prompt}> for diagnostics"
            },
        },
    ]
    transcript.write_text(
        "".join(json.dumps(row) + "\n" for row in rows),
        encoding="utf-8",
    )
    registry = MagicMock()
    session = _make_session(registry=registry)
    seeded = None

    async def seed_during_prior_turn_callback(_response: object) -> None:
        nonlocal seeded
        seeded = _seed_inflight(
            session,
            prompt=prompt,
            platform="telegram",
            chat_id="chat",
            message_id="unique-message-id",
        )
        _bind_ticket(seeded, transcript)

    tailer = TmuxTranscriptTailer(
        transcript,
        seed_during_prior_turn_callback,
        on_entry=session._on_transcript_entry,
    )
    await tailer.read_once()

    assert seeded is not None
    assert seeded.transcript_offset_at_paste == transcript.stat().st_size
    assert seeded.turn.transport_accepted is False
    registry.mark_turn_delivered.assert_not_called()
