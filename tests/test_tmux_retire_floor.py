"""Discovery after retirement admits current files and rejects older content."""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock

import pytest

from pinky_daemon import tmux_transcript
from pinky_daemon.tmux_session import _PLACEHOLDER_TRANSCRIPT_PATH, _QueuedTurn
from pinky_daemon.tmux_transcript import TmuxTranscriptTailer
from tests.test_tmux_fresh_binding import append_turn, cancel_recovery
from tests.test_tmux_fresh_transcript_usage import relaunch as relaunch

pytestmark = pytest.mark.usefixtures("reference_static_context")


async def read_usage(session):
    await session._tailer.read_once()
    if session._ctx_usage_emit_task is not None:
        await session._ctx_usage_emit_task


def queued_responses(session):
    return [(call.args[0].message_id, call.args[0].text)
            for call in session._response_callback.await_args_list]


@pytest.mark.parametrize("via", ["self-heal", "recovery"])
@pytest.mark.parametrize("route", ["live", "completion"])
async def test_late_old_file_cannot_supply_current_usage_or_reply(relaunch, via, route):
    session, previous, current = await relaunch(fresh=False)
    await cancel_recovery(session)
    delayed = previous.with_name("delayed-previous.jsonl")
    assert session.set_transcript_path(delayed, session_id=delayed.stem)
    prior_floor = session._tailer._path_bound_at
    staging = previous.with_name("previous.pending")
    append_turn(staging, "Previous response", 150_300, complete=route == "completion")
    os.utime(staging, (prior_floor - 120, prior_floor - 120))
    await session._stop_tailer()
    session._config.force_fresh_context_once = True
    await session._spawn_tmux_repl()
    await cancel_recovery(session)
    assert delayed not in session._prelaunch_transcripts
    os.replace(staging, delayed)
    session._response_callback = AsyncMock()
    if route == "completion":
        session._tailer._on_usage = None
    session._finish_turn_delivery(_QueuedTurn(
        prompt="Current request", platform="fixture", chat_id="fixture", message_id="current",
    ))

    def discover():
        if via == "self-heal":
            session._tailer._try_self_heal_repoint()
        else:
            session._attempt_first_bind_recovery()

    discover()
    await read_usage(session)
    assert session.usage.last_usage == {}
    assert session.get_context_info()["totalTokens"] == 0
    assert queued_responses(session) == []
    session._enqueue_internal_prompt.assert_not_awaited()
    assert len(session._inflight_metas) == 1
    append_turn(current, "Current real response", 9_000)
    discover()
    await read_usage(session)
    assert queued_responses(session) == [("current", "Current real response")]
    assert session.get_context_info()["totalTokens"] == 9_000
    assert not session._inflight_metas


@pytest.mark.parametrize("retained", [True, False], ids=["retained", "constructed"])
async def test_old_file_surfacing_in_spawn_waits_for_current_content(relaunch, retained):
    session, previous, current = await relaunch(fresh=False)
    await cancel_recovery(session)
    delayed = previous.with_name("delayed-previous.jsonl")
    assert session.set_transcript_path(delayed, session_id=delayed.stem)
    prior_floor = session._tailer._path_bound_at
    staging = previous.with_name("previous.pending")
    append_turn(staging, "Already completed previous response", 150_300)
    os.utime(previous, (prior_floor - 240, prior_floor - 240))
    os.utime(staging, (prior_floor - 120, prior_floor - 120))
    await session._stop_tailer()
    if not retained:
        session._tailer = None
    original_spawn = session._tmux.new_session.side_effect

    async def spawn(**kwargs):
        assert delayed not in session._prelaunch_transcripts
        os.replace(staging, delayed)
        return await original_spawn(**kwargs)

    session._tmux.new_session = AsyncMock(side_effect=spawn)
    session._config.force_fresh_context_once = True
    await session._spawn_tmux_repl()
    await cancel_recovery(session)
    session._response_callback = AsyncMock()
    session._finish_turn_delivery(_QueuedTurn(
        prompt="Current request", platform="fixture", chat_id="fixture", message_id="current",
    ))
    session._tailer._try_self_heal_repoint()
    await read_usage(session)
    assert queued_responses(session) == []
    assert session.usage.last_usage == {}
    session._enqueue_internal_prompt.assert_not_awaited()
    assert len(session._inflight_metas) == 1
    append_turn(current, "Current real response", 9_000)
    session._tailer._try_self_heal_repoint()
    await read_usage(session)
    assert queued_responses(session) == [("current", "Current real response")]
    assert session.get_context_info()["totalTokens"] == 9_000
    assert not session._inflight_metas


@pytest.mark.parametrize("origin", ["retired", "constructed"])
@pytest.mark.parametrize("delta", [-3, 0, 1], ids=["old", "equal", "new"])
async def test_unbound_reader_keeps_a_strict_discovery_floor(tmp_path, monkeypatch, origin, delta):
    monkeypatch.setattr(tmux_transcript.time, "time", lambda: 1_000.0)
    candidate = tmp_path / "candidate.jsonl"
    completed = AsyncMock()
    usage = []
    kwargs = {"discovery_floor": 1_000.0} if origin == "constructed" else {}
    tailer = TmuxTranscriptTailer(
        _PLACEHOLDER_TRANSCRIPT_PATH, completed,
        path_discovery=lambda: candidate, on_usage=usage.append, **kwargs,
    )
    if origin == "retired":
        tailer.retire_binding(_PLACEHOLDER_TRANSCRIPT_PATH)
    append_turn(candidate, "Candidate response", 9_000)
    os.utime(candidate, (1_000 + delta, 1_000 + delta))
    tailer._try_self_heal_repoint()
    await tailer.read_once()
    if delta < 0:
        completed.assert_not_awaited()
        assert usage == []
    else:
        completed.assert_awaited_once()
        assert completed.await_args.args[0].text == "Candidate response"
        assert usage == [{"input_tokens": 9_000}]


@pytest.mark.parametrize("origin", ["cold", "retired", "bound"])
def test_retired_placeholder_keeps_the_cold_wedge_behavior(tmp_path, monkeypatch, origin):
    now = [1_000.0]
    monkeypatch.setattr(tmux_transcript.time, "time", lambda: now[0])
    report = MagicMock()
    tailer = TmuxTranscriptTailer(
        _PLACEHOLDER_TRANSCRIPT_PATH, AsyncMock(), on_bound_path_wedge=report,
    )
    missing = tmp_path / "current.jsonl"
    if origin == "retired":
        tailer.retire_binding(_PLACEHOLDER_TRANSCRIPT_PATH)
    elif origin == "bound":
        tailer.set_transcript_path(missing)
    now[0] = 1_401.0
    tailer._report_bound_path_wedge_if_due()
    if origin == "bound":
        report.assert_called_once_with(missing, 401.0)
    else:
        report.assert_not_called()
