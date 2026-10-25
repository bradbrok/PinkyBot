"""A fresh launch retires a binding without blacklisting an authoritative path."""

from __future__ import annotations

import asyncio
import json
import os
from unittest.mock import AsyncMock, MagicMock

import pytest

from pinky_daemon.streaming_session import StreamingSessionConfig
from pinky_daemon.tmux_session import _PLACEHOLDER_TRANSCRIPT_PATH, TmuxSession, _QueuedTurn
from pinky_daemon.tmux_transcript import TmuxTranscriptTailer
from tests.test_tmux_fresh_transcript_usage import relaunch as relaunch
from tests.test_tmux_session import _make_mock_tmux, _recovery_clock

pytestmark = pytest.mark.usefixtures("reference_static_context")


def append_turn(path, text, tokens=8_000, *, complete=True):
    entries = [{"type": "assistant", "message": {
        "content": [{"type": "text", "text": text}],
        "usage": {"input_tokens": tokens}, "stop_reason": "end_turn",
    }}]
    if complete:
        entries.append({"type": "system", "subtype": "stop_hook_summary"})
    with path.open("a") as handle:
        for entry in entries:
            handle.write(json.dumps(entry) + "\n")


async def cancel_recovery(session):
    task = session._first_bind_recovery_task
    if task is not None:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    session._first_bind_recovery_task = None


@pytest.mark.parametrize("hook", [False, True], ids=["lost-hook", "accepted-hook"])
@pytest.mark.parametrize("replace_file", [False, True], ids=["same-inode", "new-inode"])
async def test_reused_path_needs_an_authoritative_current_bind(
    relaunch, monkeypatch, hook, replace_file,
):
    session, previous, _current = await relaunch()
    await cancel_recovery(session)
    if replace_file:
        replacement = previous.with_name("replacement.jsonl")
        replacement.touch()
        os.replace(replacement, previous)
    session._response_callback = AsyncMock()
    session._finish_turn_delivery(_QueuedTurn(
        prompt="Current request", platform="fixture", chat_id="fixture", message_id="current",
    ))
    append_turn(previous, "Current response")
    if hook:
        assert session.set_transcript_path(previous, session_id=previous.stem)
    _recovery_clock(monkeypatch)
    await session._delayed_first_bind_recovery()
    session._tailer._try_self_heal_repoint()
    await session._tailer.read_once()
    if session._ctx_usage_emit_task is not None:
        await session._ctx_usage_emit_task
    if hook:
        session._response_callback.assert_awaited_once()
        response = session._response_callback.await_args.args[0]
        assert (response.message_id, response.text) == ("current", "Current response")
        assert not session._inflight_metas
        assert session.get_context_info()["totalTokens"] == 8_000
    else:
        session._response_callback.assert_not_awaited()
        assert len(session._inflight_metas) == 1
        assert session.get_context_info()["totalTokens"] == 0
        assert session.usage.last_usage == {}
        session._enqueue_internal_prompt.assert_not_awaited()


async def test_callback_restart_and_resumed_bind_deliver_the_real_later_reply(relaunch):
    session, previous, current = await relaunch(fresh=False)
    await cancel_recovery(session)
    session._response_callback = AsyncMock()
    session._finish_turn_delivery(_QueuedTurn(
        prompt="Previous request", platform="fixture", chat_id="fixture", message_id="previous",
    ))
    restarted = False

    async def complete_and_restart(response):
        nonlocal restarted
        await session._handle_turn_complete(response)
        if not restarted:
            restarted = True
            await session._stop_tailer()
            session._config.force_fresh_context_once = True
            await session._spawn_tmux_repl()
            await cancel_recovery(session)
            current.touch()
            assert session.set_transcript_path(current, session_id=current.stem)

    session._tailer._on_turn_complete = complete_and_restart
    append_turn(previous, "Previous response")
    append_turn(previous, "Late previous activity", 12_000, complete=False)
    await session._tailer.read_once()
    await session._stop_tailer()
    session._fresh_context_respawn_grace_until = 0.0
    session._config.force_fresh_context_once = False
    await session._spawn_tmux_repl()
    await cancel_recovery(session)
    assert session._last_launch_used_continue
    assert session.set_transcript_path(current, session_id=current.stem)
    session._finish_turn_delivery(_QueuedTurn(
        prompt="Later request", platform="fixture", chat_id="fixture", message_id="later",
    ))
    await session._tailer.read_once()
    assert [(c.args[0].message_id, c.args[0].text)
            for c in session._response_callback.await_args_list] == [
                ("previous", "Previous response")]
    append_turn(current, "Current real response", 9_000)
    await session._tailer.read_once()
    assert [(c.args[0].message_id, c.args[0].text)
            for c in session._response_callback.await_args_list] == [
                ("previous", "Previous response"), ("later", "Current real response")]
    assert not session._inflight_metas


async def test_fresh_binding_is_retired_before_process_creation(relaunch):
    session, _previous, _current = await relaunch(fresh=False)
    await cancel_recovery(session)
    original_spawn = session._tmux.new_session.side_effect
    session._tailer.set_offset(17)
    session._tailer._buffer.feed({"type": "assistant", "message": {
        "content": [{"type": "text", "text": "Partial previous response"}],
    }})
    session._tailer_first_bind_pending = False
    session._session_ready_event.set()
    session._tailer._wake_event.clear()

    async def spawn(**kwargs):
        assert session._tailer.transcript_path == _PLACEHOLDER_TRANSCRIPT_PATH
        assert session._tailer.offset == 0
        assert session._tailer.stats["buffer_empty"]
        assert session._tailer._wake_event.is_set()
        assert not session._tailer_first_bind_pending
        assert session._session_ready_event.is_set()
        return await original_spawn(**kwargs)

    session._tmux.new_session = AsyncMock(side_effect=spawn)
    session._config.force_fresh_context_once = True
    await session._spawn_tmux_repl()


@pytest.mark.parametrize("current_exists", [False, True], ids=["awaiting-file", "early-reply"])
async def test_constructed_fresh_reader_never_tails_a_prelaunch_guess(
    tmp_path, monkeypatch, current_exists,
):
    project = tmp_path / "transcripts"
    project.mkdir()
    previous = project / "previous.jsonl"
    previous.touch()
    current = project / "current.jsonl"
    if current_exists:
        append_turn(current, "Early current response")
    session = TmuxSession(StreamingSessionConfig(
        agent_name="context-test", working_dir=str(tmp_path), model="claude-haiku-4-5",
    ), tmux_control=_make_mock_tmux())
    session._project_dir = lambda: project
    session._discover_transcript_path = lambda: previous
    session._last_launch_forced_fresh = True
    session._prelaunch_transcripts = frozenset({previous})
    session._emit_stream_event = AsyncMock()
    session._enqueue_internal_prompt = AsyncMock()
    session._response_callback = AsyncMock()
    session._finish_turn_delivery(_QueuedTurn(
        prompt="Current request", platform="fixture", chat_id="fixture", message_id="current",
    ))
    monkeypatch.setattr(TmuxTranscriptTailer, "start", AsyncMock())
    try:
        await session._start_tailer()
        await cancel_recovery(session)
        append_turn(previous, "Late previous response", 150_300)
        await session._tailer.read_once()
        if session._ctx_usage_emit_task is not None:
            await session._ctx_usage_emit_task
        assert {
            "old_binding": session._tailer.transcript_path == previous,
            "tokens": session.get_context_info()["totalTokens"],
            "nudges": session._enqueue_internal_prompt.await_count,
            "replies": session._response_callback.await_count,
        } == {"old_binding": False, "tokens": 8_000 if current_exists else 0,
              "nudges": 0, "replies": int(current_exists)}
        if current_exists:
            assert session._response_callback.await_args.args[0].text == "Early current response"
    finally:
        await session._stop_tailer()


@pytest.mark.parametrize("via", ["self-heal", "recovery"])
async def test_lost_hook_reaches_a_distinct_new_transcript(relaunch, via):
    session, previous, current = await relaunch()
    await cancel_recovery(session)
    session._response_callback = AsyncMock()
    session._finish_turn_delivery(_QueuedTurn(
        prompt="Current request", platform="fixture", chat_id="fixture", message_id="current",
    ))
    append_turn(previous, "Late previous response", 150_300)
    append_turn(current, "Recovered current response")
    if via == "self-heal":
        session._tailer._try_self_heal_repoint()
    else:
        session._attempt_first_bind_recovery()
    await session._tailer.read_once()
    session._response_callback.assert_awaited_once()
    assert session._response_callback.await_args.args[0].text == "Recovered current response"
    assert session.get_context_info()["totalTokens"] == 8_000
    assert not session._inflight_metas
    session._enqueue_internal_prompt.assert_not_awaited()


async def test_retire_in_callback_preserves_the_new_bindings_offset(tmp_path):
    previous, current = tmp_path / "previous.jsonl", tmp_path / "current.jsonl"
    append_turn(previous, "Previous response")
    append_turn(previous, "Late previous response")
    current.write_text('{"type":"user"}\n')
    responses = []

    async def completed(response):
        responses.append(response.text)
        if len(responses) == 1:
            tailer.retire_binding(_PLACEHOLDER_TRANSCRIPT_PATH)
            tailer.set_transcript_path(current)

    tailer = TmuxTranscriptTailer(previous, completed)
    await tailer.read_once()
    assert tailer.offset == current.stat().st_size
    assert tailer.transcript_path == current
    append_turn(current, "Current response")
    await tailer.read_once()
    assert responses == ["Previous response", "Current response"]


async def test_fresh_launch_leaves_other_transcript_readers_unchanged(relaunch):
    from pinky_daemon.codex_tmux_transcript import CodexTmuxTranscriptTailer

    session, previous, _current = await relaunch(fresh=False)
    await cancel_recovery(session)
    tailer = MagicMock(spec=CodexTmuxTranscriptTailer)
    tailer.transcript_path = previous
    tailer.start = AsyncMock()
    tailer.stop = AsyncMock()
    session._tailer = tailer
    session._config.force_fresh_context_once = True
    await session._spawn_tmux_repl()
    assert tailer.transcript_path == previous
