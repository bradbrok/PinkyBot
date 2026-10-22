"""Red tests: a Codex tmux launch that honored ``force_fresh_context_once``
must consume the one-shot flag, so a later relaunch resumes the new
conversation instead of starting yet another fresh one."""
import asyncio
import shlex
import time
from unittest.mock import AsyncMock

import pytest

from pinky_daemon import codex_tmux_session, tmux_session
from pinky_daemon.codex_tmux_session import CodexTmuxSession
from pinky_daemon.streaming_session import StreamingSessionConfig
from tests.test_tmux_session import _make_mock_tmux, _turn_response


def _codex_session(tmp_path, monkeypatch) -> CodexTmuxSession:
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    working_dir = tmp_path / "agent"
    working_dir.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    cfg = StreamingSessionConfig(
        agent_name="codexagent",
        working_dir=str(working_dir),
        model="gpt-5.5",
        thinking_effort="medium",
    )
    ss = CodexTmuxSession(cfg, tmux_control=_make_mock_tmux())
    ss._skip_wake_prompt_for_tests = True
    ss._codex_dismiss_nux_and_ready = AsyncMock()
    ss._has_prior_transcript = lambda: True
    return ss


@pytest.mark.asyncio
async def test_successful_fresh_launch_consumes_force_fresh_once(tmp_path, monkeypatch):
    ss = _codex_session(tmp_path, monkeypatch)
    ss._config.force_fresh_context_once = True

    await ss.connect()
    try:
        assert ss._last_launch_forced_fresh is True
        assert ss._config.force_fresh_context_once is False
    finally:
        await ss.disconnect()


@pytest.mark.asyncio
async def test_relaunch_after_fresh_launch_resumes(tmp_path, monkeypatch):
    ss = _codex_session(tmp_path, monkeypatch)
    ss._config.force_fresh_context_once = True

    await ss.connect()
    try:
        # Past any post-fresh grace window: the next launch is an ordinary
        # relaunch of the conversation the fresh launch started.
        ss._fresh_context_respawn_grace_until = 0.0
        cmd = ss._build_claude_cmd()
        assert "resume --last" in cmd
        assert ss._last_launch_forced_fresh is False
    finally:
        await ss.disconnect()


class _LaunchClock:
    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now

    def __getattr__(self, name):
        return getattr(time, name)


def _launch_session(tmp_path, monkeypatch):
    clock = _LaunchClock()
    monkeypatch.setattr(codex_tmux_session, "time", clock)
    monkeypatch.setattr(tmux_session, "time", clock)
    monkeypatch.setattr(tmux_session, "_POST_SPAWN_LIVENESS_DELAY_SEC", 0)
    ss = _codex_session(tmp_path, monkeypatch)
    alive = False
    spawn_result = ss._tmux.new_session.return_value
    kill_result = ss._tmux.kill_session.return_value

    async def spawn(*_args, **_kwargs):
        nonlocal alive
        alive = True
        return spawn_result

    async def kill(*_args, **_kwargs):
        nonlocal alive
        alive = False
        return kill_result

    ss._tmux.has_session = AsyncMock(side_effect=lambda: alive)
    ss._tmux.new_session.side_effect = spawn
    ss._tmux.kill_session.side_effect = kill
    ss._enqueue_wake_prompt = AsyncMock()
    return ss, clock


def _assert_fresh(ss):
    argv = shlex.split(ss._build_claude_cmd())
    assert "resume" not in argv
    assert "-C" in argv
    assert ss._last_launch_forced_fresh is True


def _assert_resume(ss):
    argv = shlex.split(ss._build_claude_cmd())
    assert argv[:3] == ["codex", "resume", "--last"]
    assert "-C" not in argv
    assert ss._last_launch_forced_fresh is False


@pytest.mark.asyncio
async def test_fresh_launch_consumes_once_and_arms_grace_epoch(tmp_path, monkeypatch):
    ss, clock = _launch_session(tmp_path, monkeypatch)
    ss._config.force_fresh_context_once = True
    try:
        await ss.connect()
        assert ss._config.force_fresh_context_once is False
        assert ss._last_launch_force_fresh_once is True
        assert ss._last_launch_in_fresh_grace is False
        assert ss._fresh_context_respawn_epoch == 1
        assert ss._fresh_context_respawn_epoch_seq == 1
        assert ss._fresh_context_respawn_grace_until == (
            clock.now + tmux_session.FRESH_CONTEXT_RESPAWN_GRACE_SEC
        )
        ss._enqueue_wake_prompt.assert_awaited_once_with(tmux_session.WakeReason.CONTEXT_RESTART)
    finally:
        await ss.disconnect()


@pytest.mark.asyncio
@pytest.mark.parametrize("past_deadline", [0, 1], ids=["at-expiry", "after-expiry"])
async def test_relaunch_at_or_after_grace_resumes_with_resume_wake(
    tmp_path, monkeypatch, past_deadline,
):
    ss, clock = _launch_session(tmp_path, monkeypatch)
    ss._config.force_fresh_context_once = True
    try:
        await ss.connect()
        assert ss._fresh_context_respawn_grace_until > clock.now
        deadline = ss._fresh_context_respawn_grace_until
        await ss.disconnect()
        clock.now = deadline + past_deadline
        await ss.connect()
        ss._enqueue_wake_prompt.assert_awaited_with(tmux_session.WakeReason.RESUME)
        assert ss._last_launch_force_fresh_once is False
        assert ss._last_launch_in_fresh_grace is False
        _assert_resume(ss)
    finally:
        await ss.disconnect()


@pytest.mark.asyncio
async def test_relaunch_inside_grace_stays_fresh_with_context_restart_wake(tmp_path, monkeypatch):
    ss, clock = _launch_session(tmp_path, monkeypatch)
    ss._config.force_fresh_context_once = True
    try:
        await ss.connect()
        await ss.disconnect()
        clock.now += 10
        await ss.connect()
        ss._enqueue_wake_prompt.assert_awaited_with(tmux_session.WakeReason.CONTEXT_RESTART)
        assert ss._config.force_fresh_context_once is False
        assert ss._last_launch_force_fresh_once is False
        assert ss._last_launch_in_fresh_grace is True
        _assert_fresh(ss)
    finally:
        await ss.disconnect()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["spawn", "tailer"])
async def test_failed_launch_keeps_flag_until_successful_fresh_retry(
    tmp_path, monkeypatch, failure,
):
    ss, clock = _launch_session(tmp_path, monkeypatch)
    ss._config.force_fresh_context_once = True
    original_spawn = ss._tmux.new_session
    original_tailer = ss._start_tailer
    if failure == "spawn":
        ss._tmux.new_session = AsyncMock(side_effect=RuntimeError("synthetic spawn failure"))
    else:
        ss._start_tailer = AsyncMock(side_effect=RuntimeError("synthetic tailer failure"))
    try:
        with pytest.raises(RuntimeError, match=f"synthetic {failure} failure"):
            await ss.connect()
        assert ss._config.force_fresh_context_once is True
        assert ss._fresh_context_respawn_epoch == 0
        assert ss._fresh_context_respawn_epoch_seq == 0
        assert ss._fresh_context_respawn_grace_until == 0.0
        if failure == "tailer":
            ss._tmux.kill_session.assert_awaited()
            assert ss._tailer is None
        ss._tmux.new_session = original_spawn
        ss._start_tailer = original_tailer
        await ss.connect()
        assert ss._config.force_fresh_context_once is False
        assert ss._last_launch_forced_fresh is True
        assert ss._fresh_context_respawn_epoch == 1
        assert ss._fresh_context_respawn_grace_until > clock.now
        ss._enqueue_wake_prompt.assert_awaited_once_with(tmux_session.WakeReason.CONTEXT_RESTART)
    finally:
        await ss.disconnect()


@pytest.mark.asyncio
@pytest.mark.parametrize("retirement", ["completion", "phantom"])
async def test_correlated_completion_ends_grace_but_phantom_retirement_waits_for_expiry(
    tmp_path, monkeypatch, retirement,
):
    ss, clock = _launch_session(tmp_path, monkeypatch)
    ss._config.force_fresh_context_once = True
    try:
        await ss.connect()
        deadline = ss._fresh_context_respawn_grace_until
        epoch = ss._fresh_context_respawn_epoch
        assert epoch > 0 and deadline > clock.now
        done = asyncio.Event()
        turn = tmux_session._QueuedTurn(
            prompt="replacement turn", internal=True, completion_event=done,
        )
        ss._finish_turn_delivery(turn)
        meta = ss._inflight_metas[0]
        assert meta.fresh_context_epoch == epoch
        if retirement == "completion":
            await ss._handle_turn_complete(_turn_response(text="replacement complete"))
            assert done.is_set()
            assert not ss._inflight_metas
            assert ss._fresh_context_respawn_epoch == 0
            assert ss._fresh_context_respawn_grace_until == 0.0
            await ss.disconnect()
            await ss.connect()
            ss._enqueue_wake_prompt.assert_awaited_with(tmux_session.WakeReason.RESUME)
            _assert_resume(ss)
        else:
            assert ss._reconcile_codex_phantom_metas([meta], reason="explicit_idle_pane") == 1
            assert done.is_set()
            assert not ss._inflight_metas
            assert ss._fresh_context_respawn_epoch == epoch
            assert ss._fresh_context_respawn_grace_until == deadline
            clock.now = deadline - 1
            _assert_fresh(ss)
            clock.now = deadline
            _assert_resume(ss)
            assert ss._fresh_context_respawn_epoch == epoch
            assert ss._fresh_context_respawn_grace_until == deadline
    finally:
        await ss.disconnect()


@pytest.mark.asyncio
async def test_in_grace_relaunch_does_not_rearm_epoch_or_extend_deadline(tmp_path, monkeypatch):
    ss, clock = _launch_session(tmp_path, monkeypatch)
    ss._config.force_fresh_context_once = True
    try:
        await ss.connect()
        original = (ss._fresh_context_respawn_epoch, ss._fresh_context_respawn_epoch_seq,
                    ss._fresh_context_respawn_grace_until)
        assert original[0] > 0 and original[2] > clock.now
        await ss.disconnect()
        clock.now += 20
        await ss.connect()
        assert ss._last_launch_forced_fresh is True
        assert (ss._fresh_context_respawn_epoch, ss._fresh_context_respawn_epoch_seq,
                ss._fresh_context_respawn_grace_until) == original
        assert ss._last_launch_force_fresh_once is False
        assert ss._last_launch_in_fresh_grace is True
    finally:
        await ss.disconnect()
