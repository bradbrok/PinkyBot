"""Acceptance coverage for folded mid-turn tmux prompt consumption (#1163).

Claude Code can fold one or more pane pastes into a framed ``user`` transcript
row while another turn is running.  The full prompt bytes are still positive
transport evidence, but the row is not byte-identical to any pasted prompt.

Transport matrix: ``TmuxSession`` owns the Claude transcript evidence fixed in
this file and writes ``delivered_turn`` rows after that evidence.  The separate
``CodexSession`` transport has no delivered-turn acceptance path.
``CodexTmuxSession`` inherits the ledger method from ``TmuxSession``, but Codex
rollouts use an ``event_msg/user_message`` evidence shape and do not enter the
Claude folded-row fallback; this change must not make that shape containment-
aware or otherwise change Codex's empirically rowless behavior.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from pinky_daemon import tmux_session
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
    control.session_name = "pinky-fold-test"
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
    config = StreamingSessionConfig(
        agent_name="dymok",
        working_dir="/tmp/tmux-fold-consumption-test",
    )
    session = session_type(
        config,
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
    transport_accepted: bool = False,
    completion_event: asyncio.Event | None = None,
    scheduler_delivery: asyncio.Future[bool] | None = None,
    submission_receipt: asyncio.Future[bool] | None = None,
    platform: str = "",
    chat_id: str = "",
    message_id: str = "",
) -> _InflightMeta:
    turn = _QueuedTurn(
        prompt=prompt,
        platform=platform,
        chat_id=chat_id,
        message_id=message_id,
        completion_event=completion_event,
        scheduler_delivery=scheduler_delivery,
        submission_receipt=submission_receipt,
        transport_accepted=transport_accepted,
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


def _bind_ticket(entry: _InflightMeta, transcript: Path) -> None:
    stat = transcript.stat()
    offset = stat.st_size
    anchor_start = max(0, offset - 4096)
    with transcript.open("rb") as handle:
        handle.seek(anchor_start)
        anchor = handle.read(offset - anchor_start)
    entry.transcript_path_at_paste = transcript
    entry.transcript_file_identity_at_paste = (stat.st_dev, stat.st_ino)
    entry.transcript_offset_at_paste = offset
    entry.transcript_anchor_start_at_paste = anchor_start
    entry.transcript_anchor_at_paste = anchor
    entry.transcript_ticket_captured_at_ns = time.time_ns()


def _append_entry(transcript: Path, entry: dict) -> None:
    with transcript.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _folded_user_entry(prompt: str) -> dict:
    return {
        "type": "user",
        "message": {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": "<system-reminder>queued alongside tool output\n",
                },
                {
                    "type": "tool_result",
                    "tool_use_id": "tool-1",
                    "content": "adjacent result",
                },
                {"type": "text", "text": f"{prompt}\n</system-reminder>"},
            ],
        },
    }


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


async def _run_idle_reconcile(session: TmuxSession) -> None:
    task = asyncio.create_task(session._inflight_watchdog())
    try:
        for _ in range(200):
            if not session._inflight_metas:
                break
            await asyncio.sleep(0.01)
        else:
            pytest.fail("watchdog did not reconcile the aged idle deque")
    finally:
        task.cancel()
        await task


@pytest.mark.asyncio
async def test_fold_live_acceptance_persists_receipts_and_drains_without_requeue(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A framed fold is evidence now, and verified-consumed at idle later."""
    decisions = MagicMock()
    monkeypatch.setattr(tmux_session, "log_watchdog_decision", decisions)
    registry = MagicMock()
    registry.mark_turn_delivered.return_value = True
    session = _make_session(registry=registry)
    transcript = tmp_path / "session.jsonl"
    transcript.write_text('{"type":"system"}\n', encoding="utf-8")
    session._tailer = MagicMock()
    session._tailer.transcript_path = transcript

    running = _seed_inflight(
        session,
        prompt="turn A is already running",
        transport_accepted=True,
    )
    _bind_ticket(running, transcript)
    completion = asyncio.Event()
    scheduler_receipt = asyncio.get_running_loop().create_future()
    submission_receipt = asyncio.get_running_loop().create_future()
    prompt = "[agent | sender | internal | 2026-08-27T06:00:00-07:00]\nrun B once"
    folded = _seed_inflight(
        session,
        prompt=prompt,
        completion_event=completion,
        scheduler_delivery=scheduler_receipt,
        submission_receipt=submission_receipt,
        platform="telegram",
        chat_id="chat-1",
        message_id="message-1",
    )
    _bind_ticket(folded, transcript)
    row = _folded_user_entry(prompt)
    _append_entry(transcript, row)

    session._on_transcript_entry(row)

    assert folded.turn.transport_accepted is True
    assert scheduler_receipt.result() is True
    assert submission_receipt.result() is True
    registry.mark_turn_delivered.assert_called_once_with(
        "dymok",
        "telegram",
        "chat-1",
        "message-1",
        source="telegram",
    )

    _enable_fast_idle_reconcile(monkeypatch, session)
    await _run_idle_reconcile(session)

    assert completion.is_set()
    assert session._message_queue.empty()
    reasons = [call.kwargs.get("reason") for call in decisions.call_args_list]
    assert "phantom_requeued_unconsumed" not in reasons
    assert "verdict=verified_consumed" in capsys.readouterr().err
    registry.mark_turn_delivered.assert_called_once()


@pytest.mark.asyncio
async def test_fold_acceptance_releases_scheduler_gate_before_idle_reconcile() -> None:
    """Fold evidence settles the receipt that makes the scheduler gate busy."""
    session = _make_session()
    session._config.live_status_fn = lambda: {
        "status": "idle",
        "last_updated": time.time() + 1,
    }
    receipt = asyncio.get_running_loop().create_future()
    prompt = "folded wake holding the scheduler gate"
    entry = _seed_inflight(
        session,
        prompt=prompt,
        submission_receipt=receipt,
    )

    assert session._scheduler_pane_busy() is True
    session._on_transcript_entry(_folded_user_entry(prompt))

    assert entry.turn.transport_accepted is True
    assert receipt.result() is True
    assert len(session._inflight_metas) == 1, "no idle reconcile has run"
    assert session._scheduler_pane_busy() is False


@pytest.mark.asyncio
async def test_ledgered_unconsumed_phantom_is_suppressed_with_negative_receipts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A durable delivery row makes replay a known duplicate, so drop loudly."""
    decisions = MagicMock()
    monkeypatch.setattr(tmux_session, "log_watchdog_decision", decisions)
    registry = MagicMock()
    registry.is_turn_delivered.return_value = True
    session = _make_session(registry=registry)
    transcript = tmp_path / "session.jsonl"
    transcript.write_text('{"type":"system"}\n', encoding="utf-8")
    session._tailer = MagicMock()
    session._tailer.transcript_path = transcript
    completion = asyncio.Event()
    scheduler_receipt = asyncio.get_running_loop().create_future()
    submission_receipt = asyncio.get_running_loop().create_future()
    entry = _seed_inflight(
        session,
        prompt="ledgered prompt absent from this transcript window",
        completion_event=completion,
        scheduler_delivery=scheduler_receipt,
        submission_receipt=submission_receipt,
        platform="telegram",
        chat_id="chat-1",
        message_id="message-1",
    )
    _bind_ticket(entry, transcript)
    _enable_fast_idle_reconcile(monkeypatch, session)

    await _run_idle_reconcile(session)

    assert completion.is_set()
    assert scheduler_receipt.result() is False
    assert submission_receipt.result() is False
    assert entry.turn.replay_count == 0
    assert session._message_queue.empty()
    registry.is_turn_delivered.assert_called_once_with(
        "dymok", "telegram", "chat-1", "message-1"
    )
    reasons = [call.kwargs.get("reason") for call in decisions.call_args_list]
    assert "phantom_suppressed_ledgered" in reasons
    assert "phantom_requeued_unconsumed" not in reasons
    logs = capsys.readouterr().err
    assert "PHANTOM_SUPPRESSED_LEDGERED" in logs
    assert "message-1" in logs
    assert "scheduler_receipt=pending" in logs
    assert "submission_receipt=pending" in logs
    assert "PHANTOM_LEDGER_SUPPRESSION_SUMMARY suppressed=1" in logs


@pytest.mark.asyncio
async def test_ledger_read_error_fails_open_to_existing_requeue(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A broken backstop read never converts uncertainty into a silent drop."""
    decisions = MagicMock()
    monkeypatch.setattr(tmux_session, "log_watchdog_decision", decisions)
    registry = MagicMock()
    registry.is_turn_delivered.side_effect = RuntimeError("registry unavailable")
    session = _make_session(registry=registry)
    transcript = tmp_path / "session.jsonl"
    transcript.write_text('{"type":"system"}\n', encoding="utf-8")
    session._tailer = MagicMock()
    session._tailer.transcript_path = transcript
    completion = asyncio.Event()
    entry = _seed_inflight(
        session,
        prompt="uncertain ledger prompt",
        completion_event=completion,
        platform="telegram",
        chat_id="chat-2",
        message_id="message-2",
    )
    _bind_ticket(entry, transcript)
    _enable_fast_idle_reconcile(monkeypatch, session)

    await _run_idle_reconcile(session)

    assert not completion.is_set()
    assert entry.turn.replay_count == 1
    assert session._message_queue.get_nowait() is entry.turn
    assert session._message_queue.empty()
    reasons = [call.kwargs.get("reason") for call in decisions.call_args_list]
    assert reasons.count("phantom_requeued_unconsumed") == 1
    assert "phantom_suppressed_ledgered" not in reasons


def test_repeated_fold_reserves_accepted_occurrence_without_second_mark() -> None:
    """A replayed accepted candidate owns its fold; it cannot donate the row."""
    registry = MagicMock()
    session = _make_session(registry=registry)
    prompt = "identical replayed prompt"
    accepted = _seed_inflight(
        session,
        prompt=prompt,
        transport_accepted=True,
        platform="telegram",
        chat_id="chat-3",
        message_id="message-3",
    )
    later = _seed_inflight(session, prompt=prompt)

    session._on_transcript_entry(_folded_user_entry(prompt))

    assert accepted.turn.transport_accepted is True
    assert later.turn.transport_accepted is False
    registry.mark_turn_delivered.assert_not_called()


@pytest.mark.parametrize(
    ("occurrences", "expected"),
    [
        (1, [True, False]),
        (2, [True, True]),
    ],
)
def test_idle_fold_allocates_identical_prompts_by_nonoverlapping_occurrence(
    tmp_path: Path,
    occurrences: int,
    expected: list[bool],
) -> None:
    """One folded occurrence proves one oldest candidate; two prove both."""
    session = _make_session()
    transcript = tmp_path / "session.jsonl"
    transcript.write_text('{"type":"system"}\n', encoding="utf-8")
    session._tailer = MagicMock()
    session._tailer.transcript_path = transcript
    prompt = "same complete folded prompt"
    first = _seed_inflight(session, prompt=prompt)
    second = _seed_inflight(session, prompt=prompt)
    _bind_ticket(first, transcript)
    _bind_ticket(second, transcript)
    content = "<fold>" + " | separator | ".join([prompt] * occurrences) + "</fold>"
    _append_entry(
        transcript,
        {"type": "user", "message": {"role": "user", "content": content}},
    )

    assert session._phantom_consumption_verdicts([first, second]) == expected


def test_idle_fold_allocates_exact_rows_before_containment(tmp_path: Path) -> None:
    """A nested exact candidate takes its exact row before folded allocation."""
    session = _make_session()
    transcript = tmp_path / "session.jsonl"
    transcript.write_text('{"type":"system"}\n', encoding="utf-8")
    session._tailer = MagicMock()
    session._tailer.transcript_path = transcript
    short = _seed_inflight(session, prompt="alpha")
    long = _seed_inflight(session, prompt="alpha beta")
    _bind_ticket(short, transcript)
    _bind_ticket(long, transcript)
    _append_entry(
        transcript,
        {
            "type": "user",
            "message": {"role": "user", "content": "<fold>alpha beta</fold>"},
        },
    )
    _append_entry(
        transcript,
        {"type": "user", "message": {"role": "user", "content": "alpha"}},
    )

    assert session._phantom_consumption_verdicts([short, long]) == [True, True]


def test_idle_fold_uses_global_nonoverlapping_spans_oldest_first(tmp_path: Path) -> None:
    """Overlapping distinct prompts under-accept safely instead of double-use."""
    session = _make_session()
    transcript = tmp_path / "session.jsonl"
    transcript.write_text('{"type":"system"}\n', encoding="utf-8")
    session._tailer = MagicMock()
    session._tailer.transcript_path = transcript
    short = _seed_inflight(session, prompt="alpha")
    long = _seed_inflight(session, prompt="alpha beta")
    _bind_ticket(short, transcript)
    _bind_ticket(long, transcript)
    _append_entry(
        transcript,
        {
            "type": "user",
            "message": {"role": "user", "content": "<fold>alpha beta</fold>"},
        },
    )

    assert session._phantom_consumption_verdicts([short, long]) == [True, False]


def test_live_fold_fallback_rejects_prefix_assistant_and_empty_prompt() -> None:
    """Containment never promotes partial, assistant, or empty evidence."""
    session = _make_session()
    prefix = _seed_inflight(session, prompt="complete prompt bytes")
    assistant = _seed_inflight(session, prompt="assistant-only complete prompt")
    empty = _seed_inflight(session, prompt="")

    session._on_transcript_entry(
        {"type": "user", "message": {"content": "complete prompt"}}
    )
    session._on_transcript_entry(
        {
            "type": "assistant",
            "message": {"content": "assistant-only complete prompt"},
        }
    )
    session._on_transcript_entry(
        {"type": "user", "message": {"content": "unrelated framed user row"}}
    )

    assert prefix.turn.transport_accepted is False
    assert assistant.turn.transport_accepted is False
    assert empty.turn.transport_accepted is False


def test_idle_fold_preserves_ticket_window_empty_and_unavailable_verdicts(
    tmp_path: Path,
) -> None:
    """Containment keeps the existing False/None provenance boundaries."""
    session = _make_session()
    transcript = tmp_path / "session.jsonl"
    prompt = "full prompt must be post-ticket"
    transcript.write_text(
        json.dumps({"type": "user", "message": {"content": prompt}}) + "\n",
        encoding="utf-8",
    )
    session._tailer = MagicMock()
    session._tailer.transcript_path = transcript
    bounded = _seed_inflight(session, prompt=prompt)
    empty = _seed_inflight(session, prompt="")
    _bind_ticket(bounded, transcript)
    _bind_ticket(empty, transcript)
    _append_entry(
        transcript,
        {"type": "user", "message": {"content": "full prompt must be post"}},
    )
    _append_entry(
        transcript,
        {"type": "assistant", "message": {"content": prompt}},
    )

    assert session._phantom_consumption_verdicts([bounded, empty]) == [False, False]

    unavailable_session = _make_session()
    unavailable = _seed_inflight(unavailable_session, prompt="unavailable prompt")
    unavailable_session._tailer = None
    assert unavailable_session._phantom_consumption_verdicts([unavailable]) == [None]


def test_codex_transport_matrix_does_not_gain_claude_fold_evidence() -> None:
    """Both Codex classes remain outside the Claude folded-row write path."""
    assert not issubclass(CodexSession, TmuxSession)
    assert not hasattr(CodexSession, "_mark_transport_accepted")
    assert CodexTmuxSession._mark_transport_accepted is TmuxSession._mark_transport_accepted
    assert CodexTmuxSession._on_transcript_entry is not TmuxSession._on_transcript_entry

    registry = MagicMock()
    session = _make_session(registry=registry, session_type=CodexTmuxSession)
    prompt = "codex rollout prompt"
    entry = _seed_inflight(
        session,
        prompt=prompt,
        platform="telegram",
        chat_id="chat-codex",
        message_id="message-codex",
    )

    session._on_transcript_entry(
        {
            "type": "event_msg",
            "payload": {
                "type": "user_message",
                "message": f"<fold>{prompt}</fold>",
            },
        }
    )

    assert entry.turn.transport_accepted is False
    registry.mark_turn_delivered.assert_not_called()
