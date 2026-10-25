"""Accepted binds hand retirement protection to a strict real-bind clock."""

from __future__ import annotations

import os
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from pinky_daemon import tmux_transcript
from pinky_daemon.tmux_session import _PLACEHOLDER_TRANSCRIPT_PATH, _QueuedTurn
from pinky_daemon.tmux_transcript import TmuxTranscriptTailer
from tests.test_tmux_fresh_binding import append_turn, cancel_recovery
from tests.test_tmux_fresh_transcript_usage import relaunch as relaunch
from tests.test_tmux_retire_clocks import wall_clock as wall_clock
from tests.test_tmux_retire_floor import queued_responses, read_usage

pytestmark = pytest.mark.usefixtures("reference_static_context")


def queue(session, message_id):
    session._finish_turn_delivery(_QueuedTurn(
        prompt="Current request", platform="fixture", chat_id="fixture", message_id=message_id,
    ))


@pytest.mark.parametrize("bind", ["hook", "recovery-ack"])
@pytest.mark.parametrize("old_age", [120.0, 0.25], ids=["old", "inside-retire-allowance"])
async def test_constructed_bind_keeps_a_strict_history_guard(relaunch, wall_clock, bind, old_age):
    session, previous, current = await relaunch(retained=False, create_during_spawn=True)
    await cancel_recovery(session)
    tailer = session._tailer
    append_turn(current, "First current response", 9_000)
    os.utime(current, (wall_clock.now,) * 2)
    assert tailer.transcript_path == current
    assert tailer._path_bound_at == 0.0
    assert tailer.discovery_floor == wall_clock.now
    before = (tailer.offset, tailer._swap_generation)
    if bind == "hook":
        assert session.set_transcript_path(current, session_id=current.stem)
    else:
        session._attempt_first_bind_recovery()
    assert (tailer.offset, tailer._swap_generation) == before
    assert not session._tailer_first_bind_pending
    session._response_callback = AsyncMock()
    queue(session, "first")
    await read_usage(session)
    assert queued_responses(session) == [("first", "First current response")]

    current.rename(current.with_suffix(".pending"))
    delayed = previous.with_name("delayed-previous.jsonl")
    assert delayed not in session._prelaunch_transcripts
    append_turn(delayed, "Previous completed response", 150_300)
    os.utime(delayed, (wall_clock.now - old_age,) * 2)
    queue(session, "second")
    tailer._try_self_heal_repoint()
    old_read = await tailer.read_once()
    if session._ctx_usage_emit_task is not None:
        await session._ctx_usage_emit_task
    after_old = (dict(session.usage.last_usage), queued_responses(session),
                 len(session._inflight_metas))
    current.with_suffix(".pending").rename(current)
    append_turn(current, "Second current response", 9_000)
    os.utime(current, (wall_clock.now + 1.0,) * 2)
    tailer._try_self_heal_repoint()
    await read_usage(session)
    await read_usage(session)
    assert old_read == 0
    assert after_old == ({"input_tokens": 9_000}, [("first", "First current response")], 1)
    session._enqueue_internal_prompt.assert_not_awaited()
    assert queued_responses(session) == [
        ("first", "First current response"), ("second", "Second current response"),
    ]
    assert not session._inflight_metas
    assert tailer._path_bound_at == wall_clock.now
    assert tailer.discovery_floor == 0.0


@pytest.mark.parametrize("origin", ["cold-placeholder", "retired-placeholder", "constructed-real"])
async def test_direct_placeholder_bind_keeps_the_retirement_guard(tmp_path, wall_clock, origin):
    current = tmp_path / "current.jsonl"
    path = current if origin == "constructed-real" else _PLACEHOLDER_TRANSCRIPT_PATH
    if path == current:
        append_turn(current, "Current response", 9_000)
    delayed = tmp_path / "delayed-old.jsonl"
    append_turn(delayed, "Old response", 150_300)
    os.utime(delayed, (wall_clock.now - 120.0,) * 2)
    completed = AsyncMock()
    tailer = TmuxTranscriptTailer(
        path, completed, discovery_floor=wall_clock.now, path_discovery=lambda: delayed,
    )
    if origin == "retired-placeholder":
        tailer.retire_binding(_PLACEHOLDER_TRANSCRIPT_PATH)
    tailer.set_offset(7)
    tailer._wake_event.clear()
    before = (tailer.transcript_path, tailer.offset, tailer._swap_generation,
              tailer._path_bound_at, tailer.discovery_floor, tailer._wake_event.is_set())
    assert tailer.set_transcript_path(_PLACEHOLDER_TRANSCRIPT_PATH) is None
    assert (tailer.transcript_path, tailer.offset, tailer._swap_generation,
            tailer._path_bound_at, tailer.discovery_floor, tailer._wake_event.is_set()) == before
    if current.exists():
        current.rename(current.with_suffix(".pending"))
    tailer._try_self_heal_repoint()
    assert await tailer.read_once() == 0
    completed.assert_not_awaited()


@pytest.mark.parametrize("same_path", [True, False], ids=["same-path", "new-path"])
def test_retirement_guard_survives_until_clock_stamp(tmp_path, monkeypatch, wall_clock, same_path):
    current = tmp_path / "current.jsonl"
    tailer = TmuxTranscriptTailer(
        current if same_path else _PLACEHOLDER_TRANSCRIPT_PATH, AsyncMock(),
        discovery_floor=wall_clock.now,
    )
    observations = []

    def stamp():
        observations.append((tailer._path_bound_at, tailer.discovery_floor))
        assert tailer._path_bound_at > 0.0 or tailer.discovery_floor > 0.0
        return wall_clock.now

    monkeypatch.setattr(tmux_transcript, "time", SimpleNamespace(**{**vars(time), "time": stamp}))
    tailer.set_transcript_path(current)
    assert observations == [(0.0, wall_clock.now)]
    assert tailer._path_bound_at == wall_clock.now
    assert tailer.discovery_floor == 0.0


@pytest.mark.parametrize("same_path", [True, False], ids=["same-path", "new-path"])
def test_zero_bind_clock_cannot_discard_the_retirement_guard(tmp_path, wall_clock, same_path):
    current = tmp_path / "current.jsonl"
    tailer = TmuxTranscriptTailer(
        current if same_path else _PLACEHOLDER_TRANSCRIPT_PATH, AsyncMock(),
        discovery_floor=wall_clock.now,
    )
    retired_at = wall_clock.now
    wall_clock.now = 0.0
    tailer.set_transcript_path(current)
    assert tailer._path_bound_at == 0.0
    assert tailer.discovery_floor == retired_at
    wall_clock.now = retired_at + 1.0
    tailer.set_transcript_path(current)
    assert tailer._path_bound_at == wall_clock.now
    assert tailer.discovery_floor == 0.0


@pytest.mark.parametrize("same_path", [True, False], ids=["same-path", "new-path"])
def test_repeated_real_bind_preserves_or_restamps_the_clock(
    tmp_path, wall_clock, same_path,
):
    current = tmp_path / "current.jsonl"
    tailer = TmuxTranscriptTailer(
        _PLACEHOLDER_TRANSCRIPT_PATH, AsyncMock(), discovery_floor=wall_clock.now,
    )
    tailer.set_transcript_path(current)
    bound_at = tailer._path_bound_at
    tailer.set_offset(7)
    generation = tailer._swap_generation
    wall_clock.now += 50.0
    tailer.set_transcript_path(
        current if same_path else tmp_path / "next.jsonl", seek_to_start=True,
    )
    assert tailer._path_bound_at == (bound_at if same_path else wall_clock.now)
    assert tailer.offset == (7 if same_path else 0)
    assert tailer._swap_generation == generation + int(not same_path)
    assert tailer.discovery_floor == 0.0


@pytest.mark.parametrize("origin", ["retired-placeholder", "bound-missing", "bound-existing"])
def test_same_path_bind_keeps_real_and_placeholder_wedge_behavior(tmp_path, wall_clock, origin):
    current = tmp_path / "current.jsonl"
    if origin == "bound-existing":
        current.touch()
    path = _PLACEHOLDER_TRANSCRIPT_PATH if origin == "retired-placeholder" else current
    report = MagicMock()
    tailer = TmuxTranscriptTailer(
        path, AsyncMock(), discovery_floor=wall_clock.now, on_bound_path_wedge=report,
    )
    if origin == "retired-placeholder":
        tailer.retire_binding(_PLACEHOLDER_TRANSCRIPT_PATH)
    tailer.set_transcript_path(path)
    wall_clock.now += 401.0
    tailer._report_bound_path_wedge_if_due()
    tailer._report_bound_path_wedge_if_due()
    if origin == "bound-missing":
        report.assert_called_once_with(current, 401.0)
    else:
        report.assert_not_called()
