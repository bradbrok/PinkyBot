"""Context measurements must follow the conversation selected by a fresh launch."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from pinky_daemon import tmux_session
from pinky_daemon.streaming_session import StreamingSessionConfig
from pinky_daemon.tmux_session import TmuxCommandResult, TmuxSession, _QueuedTurn
from pinky_daemon.tmux_transcript import TmuxTranscriptTailer
from pinky_daemon.transport_state import SessionState
from tests.test_tmux_session import _make_mock_tmux

pytestmark = pytest.mark.usefixtures("reference_static_context")


def _append_usage(path, total, *, complete=False):
    entries = [{
        "type": "assistant",
        "message": {
            "content": [{"type": "text", "text": "Response."}],
            "usage": {"input_tokens": total},
            "stop_reason": "end_turn",
        },
    }]
    if complete:
        entries.append({"type": "system", "subtype": "stop_hook_summary"})
    with path.open("a") as handle:
        for entry in entries:
            handle.write(json.dumps(entry) + "\n")


async def _read_usage(session):
    await session._tailer.read_once()
    if session._ctx_usage_emit_task is not None:
        await session._ctx_usage_emit_task


@pytest.fixture
async def relaunch(tmp_path, monkeypatch):
    monkeypatch.setattr(tmux_session, "_POST_SPAWN_LIVENESS_DELAY_SEC", 0)
    monkeypatch.setattr(TmuxSession, "_spawn_cleanup_state_dir", lambda self: tmp_path)
    # Drive the real reader explicitly, without a background polling task.
    monkeypatch.setattr(TmuxTranscriptTailer, "start", AsyncMock())
    sessions = []

    async def make(*, fresh=True, retained=True, create_during_spawn=False):
        project = tmp_path / str(len(sessions)) / "transcripts"
        project.mkdir(parents=True)
        previous = project / "previous.jsonl"
        previous.touch()
        current = project / "current.jsonl"
        tmux = _make_mock_tmux()
        alive = False
        launches = 0

        async def spawn(**_kwargs):
            nonlocal alive, launches
            alive = True
            launches += 1
            if launches == 2 and create_during_spawn:
                current.touch()
            return TmuxCommandResult(0, "", "")

        async def kill():
            nonlocal alive
            alive = False
            return TmuxCommandResult(0, "", "")

        tmux.has_session = AsyncMock(side_effect=lambda: alive)
        tmux.new_session = AsyncMock(side_effect=spawn)
        tmux.kill_session = AsyncMock(side_effect=kill)
        session = TmuxSession(StreamingSessionConfig(
            agent_name="context-test",
            working_dir=str(project.parent),
            model="claude-haiku-4-5",
        ), tmux_control=tmux)
        sessions.append(session)
        session._project_dir = lambda: project
        session._prepare_tmux_spawn = MagicMock()
        session._emit_stream_event = AsyncMock()
        session._enqueue_internal_prompt = AsyncMock(wraps=session._enqueue_internal_prompt)
        await session._spawn_tmux_repl()
        assert session._last_launch_used_continue
        original_tailer = session._tailer
        await session._stop_tailer()
        if not retained:
            session._tailer = None
        if create_during_spawn:
            session._discover_transcript_path = lambda: current
        session.usage.last_usage = {"input_tokens": 150_300}
        session.usage.total_turns = 3
        session.usage.input_tokens = 123
        session._restart_nudge_fired = session._soft_nudge_fired = True
        session._config.force_fresh_context_once = fresh
        await session._spawn_tmux_repl()
        session._state_machine._state = SessionState.CONNECTED
        assert session._last_launch_forced_fresh is fresh
        assert (session._tailer is original_tailer) is retained
        assert session._tailer.transcript_path == (
            current if create_during_spawn and not retained else previous
        )
        assert session.usage.total_turns == 3
        assert session.usage.input_tokens == 123
        assert session.usage.last_usage == ({} if fresh else {"input_tokens": 150_300})
        assert session._restart_nudge_fired is (not fresh)
        assert session._soft_nudge_fired is (not fresh)
        return session, previous, current

    yield make
    for session in sessions:
        await session._stop_tailer()


@pytest.mark.parametrize("retained", [True, False], ids=["retained", "guessed"])
@pytest.mark.parametrize("total", [66_800, 150_300], ids=["soft", "hard"])
@pytest.mark.parametrize("route", ["live", "completion"])
async def test_previous_transcript_cannot_restore_usage_after_fresh_launch(
    relaunch, retained, total, route,
):
    session, previous, _current = await relaunch(retained=retained)
    if route == "completion":
        # Isolate Stop-hook accounting from the live usage callback.
        session._tailer._on_usage = None
        session._finish_turn_delivery(_QueuedTurn(prompt="Continue.", internal=True))
    _append_usage(previous, total, complete=route == "completion")
    await _read_usage(session)

    queued = list(session._message_queue._queue)
    assert {
        "tokens": session.get_context_info()["totalTokens"],
        "nudges": [turn.reason for turn in queued],
        "delivery_drops": [session._drop_stale_context_nudge(turn) for turn in queued],
    } == {"tokens": 0, "nudges": [], "delivery_drops": []}
    session._enqueue_internal_prompt.assert_not_awaited()
    assert session.usage.last_usage == {}
    assert session.get_context_info()["totalTokens"] == 0
    assert not session._restart_nudge_fired
    assert not session._soft_nudge_fired
    assert session.usage.total_turns == 3
    assert session.usage.input_tokens == 123
    if route == "completion":
        assert len(session._inflight_metas) == 1


@pytest.mark.parametrize("source", ["hook", "spawn"])
async def test_new_transcript_keeps_both_crossings_and_rearms_after_usage_drops(relaunch, source):
    session, _previous, current = await relaunch(
        retained=source == "hook", create_during_spawn=source == "spawn",
    )
    if source == "hook":
        current.touch()
        assert session.set_transcript_path(current, session_id="current")
    assert current not in session._prelaunch_transcripts

    for total in [10_000, 66_800, 66_800, 150_300, 150_300]:
        _append_usage(current, total)
        await _read_usage(session)
        assert session.get_context_info()["totalTokens"] == total
    assert [call.kwargs["reason"] for call in session._enqueue_internal_prompt.await_args_list] == [
        "context_nudge_soft", "context_autorestart_nudge",
    ]
    for total in [10_000, 66_800, 150_300]:
        _append_usage(current, total)
        await _read_usage(session)
    assert [call.kwargs["reason"] for call in session._enqueue_internal_prompt.await_args_list] == [
        "context_nudge_soft", "context_autorestart_nudge",
        "context_nudge_soft", "context_autorestart_nudge",
    ]


@pytest.mark.parametrize("retained", [True, False], ids=["retained", "guessed"])
async def test_resume_keeps_snapshot_latches_and_reads_the_existing_transcript(relaunch, retained):
    session, previous, _current = await relaunch(fresh=False, retained=retained)
    _append_usage(previous, 150_300)
    await _read_usage(session)
    assert session.get_context_info()["totalTokens"] == 150_300
    assert session._restart_nudge_fired
    assert session._soft_nudge_fired
    session._enqueue_internal_prompt.assert_not_awaited()


async def test_new_transcript_completion_records_usage(relaunch):
    session, _previous, current = await relaunch()
    current.touch()
    assert session.set_transcript_path(current, session_id="current")
    session._tailer._on_usage = None
    session._finish_turn_delivery(_QueuedTurn(prompt="Continue.", internal=True))
    _append_usage(current, 150_300, complete=True)
    await _read_usage(session)
    assert session.get_context_info()["totalTokens"] == 150_300
    assert session.usage.total_turns == 4
    assert session.usage.input_tokens == 150_423
    assert not session._inflight_metas
    session._enqueue_internal_prompt.assert_awaited_once()
    reason = session._enqueue_internal_prompt.await_args.kwargs["reason"]
    assert reason == "context_autorestart_nudge"


async def test_fresh_launch_during_completion_rejects_the_rest_of_the_old_chunk(relaunch):
    session, previous, _current = await relaunch(fresh=False)
    session._finish_turn_delivery(_QueuedTurn(prompt="Continue.", internal=True))

    async def restart_after_completion(response):
        await session._handle_turn_complete(response)
        await session._stop_tailer()
        session._config.force_fresh_context_once = True
        await session._spawn_tmux_repl()

    session._tailer._on_turn_complete = restart_after_completion
    _append_usage(previous, 10_000, complete=True)
    _append_usage(previous, 150_300)
    await _read_usage(session)
    assert session.get_context_info()["totalTokens"] == 0
    assert session.usage.last_usage == {}
    session._enqueue_internal_prompt.assert_not_awaited()
