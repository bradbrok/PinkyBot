"""Retirement tolerates small metadata skew without weakening real bind clocks."""

from __future__ import annotations

import json
import os
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from pinky_daemon import tmux_session, tmux_transcript
from pinky_daemon.codex_tmux_transcript import CodexTmuxTranscriptTailer
from pinky_daemon.tmux_session import _PLACEHOLDER_TRANSCRIPT_PATH, TmuxSession, _QueuedTurn
from pinky_daemon.tmux_transcript import TmuxTranscriptTailer
from tests import test_tmux_session as session_tests
from tests.test_tmux_fresh_binding import append_turn, cancel_recovery
from tests.test_tmux_fresh_transcript_usage import relaunch as relaunch
from tests.test_tmux_retire_floor import queued_responses, read_usage
from tests.test_transcript_recovery import _retained_session, _write_transcript

pytestmark = pytest.mark.usefixtures("reference_static_context")


@pytest.fixture
def wall_clock(monkeypatch):
    clock = SimpleNamespace(now=1_000.75)
    proxy = SimpleNamespace(**{**vars(time), "time": lambda: clock.now})
    monkeypatch.setattr(tmux_session, "time", proxy)
    monkeypatch.setattr(tmux_transcript, "time", proxy)
    return clock


def queue_current(session):
    session._response_callback = AsyncMock()
    session._finish_turn_delivery(_QueuedTurn(
        prompt="Current request", platform="fixture", chat_id="fixture", message_id="current",
    ))


@pytest.mark.parametrize("via", ["self-heal", "recovery", "construction"])
@pytest.mark.parametrize(
    "delta,admitted",
    [(-0.75, True), (-0.25, True), (-2.0, True), (-2.001, False), (-3.0, False),
     (0.0, True), (1.0, True)],
    ids=["coarse-one-second", "clock-behind-250ms", "exact-allowance", "below-allowance",
         "old", "equal", "newer"],
)
async def test_fresh_discovery_allows_small_clock_skew(
    relaunch, wall_clock, via, delta, admitted,
):
    session, previous, current = await relaunch(fresh=False)
    await cancel_recovery(session)
    await session._stop_tailer()
    if via == "construction":
        session._tailer = None
    original_spawn = session._tmux.new_session.side_effect

    async def spawn(**kwargs):
        assert previous in session._prelaunch_transcripts
        assert current not in session._prelaunch_transcripts
        append_turn(current, "Current complete response", 9_000)
        os.utime(current, (wall_clock.now + delta,) * 2)
        return await original_spawn(**kwargs)

    session._tmux.new_session = AsyncMock(side_effect=spawn)
    session._config.force_fresh_context_once = True
    await session._spawn_tmux_repl()
    await cancel_recovery(session)
    queue_current(session)

    def discover():
        if via == "recovery":
            session._attempt_first_bind_recovery()
        else:
            session._tailer._try_self_heal_repoint()

    discover()
    await read_usage(session)
    if not admitted:
        assert session._tailer.transcript_path == _PLACEHOLDER_TRANSCRIPT_PATH
        assert session.usage.last_usage == {}
        assert queued_responses(session) == []
        assert len(session._inflight_metas) == 1
        session._enqueue_internal_prompt.assert_not_awaited()
        os.utime(current, (wall_clock.now + 1,) * 2)
        discover()
        await read_usage(session)
    assert queued_responses(session) == [("current", "Current complete response")]
    assert session.usage.last_usage == {"input_tokens": 9_000}
    assert not session._inflight_metas
    assert session._tailer.offset == current.stat().st_size
    assert session.set_transcript_path(current, session_id=current.stem)
    await read_usage(session)
    assert queued_responses(session) == [("current", "Current complete response")]


@pytest.mark.parametrize("origin", ["retired", "constructed"])
async def test_real_bind_clears_retained_floor_after_clock_step(tmp_path, wall_clock, origin):
    current = tmp_path / "current.jsonl"
    completed = AsyncMock()
    kwargs = {"discovery_floor": wall_clock.now} if origin == "constructed" else {}
    tailer = TmuxTranscriptTailer(
        _PLACEHOLDER_TRANSCRIPT_PATH, completed, path_discovery=lambda: current, **kwargs,
    )
    if origin == "retired":
        tailer.retire_binding(_PLACEHOLDER_TRANSCRIPT_PATH)
    wall_clock.now = 990.0
    tailer.set_transcript_path(tmp_path / "bound-but-missing.jsonl")
    assert tailer._path_bound_at == 990.0
    append_turn(current, "Current response", 9_000)
    os.utime(current, (991.0, 991.0))
    tailer._try_self_heal_repoint()
    await tailer.read_once()
    completed.assert_awaited_once()
    assert completed.await_args.args[0].text == "Current response"
    assert tailer._discovery_floor == 0.0


@pytest.mark.parametrize(
    "via", ["hook", "recovery", "self-heal", "same-path-hook", "same-path-ack"],
)
async def test_accepted_bind_discards_the_retire_floor(relaunch, wall_clock, via):
    if via.startswith("same-path"):
        session, _, current = await relaunch(retained=False, create_during_spawn=True)
    else:
        session, _, current = await relaunch()
    await cancel_recovery(session)
    assert session._tailer._discovery_floor == wall_clock.now
    append_turn(current, "Current response", 9_000)
    if via in {"hook", "same-path-hook"}:
        session._tailer.set_offset(7)
        assert session.set_transcript_path(current, session_id=current.stem)
        assert session._tailer.offset == (7 if via == "same-path-hook" else 0)
    elif via == "recovery":
        session._attempt_first_bind_recovery()
    elif via == "self-heal":
        session._tailer._try_self_heal_repoint()
    else:
        # A fresh constructor may already have selected this file. Recovery
        # acknowledges it without rewinding bytes read before the hook.
        session._tailer.set_offset(7)
        session._attempt_first_bind_recovery()
        assert session._tailer.offset == 7
    assert session._tailer._discovery_floor == 0.0


async def test_self_heal_clear_also_releases_shared_discovery(relaunch, wall_clock):
    session, _, current = await relaunch()
    await cancel_recovery(session)
    wall_clock.now = 990.0
    append_turn(current, "First current response", 9_000)
    os.utime(current, (999.0, 999.0))
    queue_current(session)
    session._tailer._try_self_heal_repoint()
    await read_usage(session)
    assert queued_responses(session) == [("current", "First current response")]
    assert session._tailer._path_bound_at == 990.0
    current.rename(current.with_suffix(".pending"))
    discovered = current.with_name("discovered-current.jsonl")
    append_turn(discovered, "Next current response", 9_000)
    os.utime(discovered, (991.0, 991.0))
    queue_current(session)
    assert session._discover_post_launch_transcript_path() == discovered
    session._tailer._try_self_heal_repoint()
    await read_usage(session)
    assert queued_responses(session) == [("current", "Next current response")]


@pytest.mark.parametrize("continued", [True, False], ids=["continue", "cold-fresh"])
async def test_nonfresh_launch_clears_retained_floor_before_process(
    relaunch, wall_clock, continued,
):
    session, previous, current = await relaunch()
    await cancel_recovery(session)
    await session._stop_tailer()
    if continued:
        os.utime(previous, (980.0, 980.0))
    else:
        previous.unlink()
    original_spawn = session._tmux.new_session.side_effect
    retained = session._tailer
    generation, offset = retained._swap_generation, retained.offset

    async def spawn(**kwargs):
        assert session._transcript_discovery_floor == 0.0
        assert retained._discovery_floor == 0.0
        assert retained._swap_generation == generation
        assert retained.offset == offset
        return await original_spawn(**kwargs)

    session._tmux.new_session = AsyncMock(side_effect=spawn)
    session._fresh_context_respawn_grace_until = 0.0
    session._config.force_fresh_context_once = False
    wall_clock.now = 990.0
    await session._spawn_tmux_repl()
    await cancel_recovery(session)
    assert session._last_launch_used_continue is continued
    assert not session._last_launch_forced_fresh
    assert session._tailer is retained
    append_turn(current, "Current response", 9_000)
    os.utime(current, (991.0, 991.0))
    queue_current(session)
    session._tailer._try_self_heal_repoint()
    await read_usage(session)
    assert queued_responses(session) == [("current", "Current response")]


@pytest.mark.parametrize("delta", [-2.0, -0.25, 0.0, 0.25])
async def test_real_bind_discovery_floor_keeps_strict_boundary(tmp_path, wall_clock, delta):
    current = tmp_path / "current.jsonl"
    completed = AsyncMock()
    tailer = TmuxTranscriptTailer(
        _PLACEHOLDER_TRANSCRIPT_PATH, completed, path_discovery=lambda: current,
        discovery_floor=wall_clock.now,
    )
    tailer.set_transcript_path(tmp_path / "bound-but-missing.jsonl")
    append_turn(current, "Current response", 9_000)
    os.utime(current, (wall_clock.now + delta,) * 2)
    tailer._try_self_heal_repoint()
    await tailer.read_once()
    assert completed.await_count == int(delta >= 0)
    assert tailer.stats["self_heal_stale_skips"] == int(delta < 0)


def append_codex_turn(path, text):
    with path.open("a") as handle:
        handle.write(json.dumps({"type": "event_msg", "payload": {
            "type": "task_complete", "last_agent_message": text,
        }}) + "\n")


@pytest.mark.parametrize("via", ["construction", "retained-launch", "self-heal", "recovery"])
async def test_codex_routes_keep_base_discovery_without_retire_floor(tmp_path, monkeypatch, via):
    session, previous, root = _retained_session(tmp_path, monkeypatch, "codex", cold=True)
    monkeypatch.setattr(CodexTmuxTranscriptTailer, "start", AsyncMock())
    monkeypatch.setattr(CodexTmuxTranscriptTailer, "stop", AsyncMock())
    monkeypatch.setattr(TmuxSession, "_spawn_cleanup_state_dir", lambda self: tmp_path)
    monkeypatch.setattr(tmux_session, "_POST_SPAWN_LIVENESS_DELAY_SEC", 0)
    current = root / "2026/09/25/rollout-current.jsonl"
    if via == "construction":
        session._tailer = None
    elif via == "retained-launch":
        _write_transcript(previous, session._config.working_dir)
        append_codex_turn(previous, "Previous response")
        os.utime(previous, (9.0, 9.0))
        session._tailer.set_transcript_path(previous)
    retained = session._tailer
    if retained is not None:
        retained._on_turn_complete = session._handle_turn_complete
    before = (retained._swap_generation, retained.offset) if retained is not None else None

    def create():
        _write_transcript(current, session._config.working_dir)
        append_codex_turn(current, "Current response")
        os.utime(current, (10.0, 10.0))

    async def spawn(**kwargs):
        if via in {"construction", "retained-launch"}:
            create()
        return session_tests._ok()

    monkeypatch.setattr(session._tmux, "new_session", AsyncMock(side_effect=spawn))
    try:
        await TmuxSession._spawn_tmux_repl(session)
        await cancel_recovery(session)
        assert getattr(session, "_transcript_discovery_floor", 0.0) == 0.0
        assert not hasattr(session._tailer, "_discovery_floor")
        assert not hasattr(session._tailer, "_path_bound_at")
        if via == "construction":
            assert session._tailer.transcript_path == current
            assert session._tailer.offset == current.stat().st_size
            append_codex_turn(current, "New response after construction")
        elif via == "retained-launch":
            assert session._tailer is retained
            assert session._tailer.transcript_path == previous
            assert (retained._swap_generation, retained.offset) == before
        else:
            create()
        assert session._discover_post_launch_transcript_path() == current
        queue_current(session)
        if via == "recovery":
            session._attempt_first_bind_recovery()
        elif via != "construction":
            session._tailer._try_self_heal_repoint()
        await read_usage(session)
        expected = (
            "New response after construction" if via == "construction" else "Current response"
        )
        assert queued_responses(session) == [("current", expected)]
        await read_usage(session)
        assert queued_responses(session) == [("current", expected)]
    finally:
        await session._stop_tailer()
