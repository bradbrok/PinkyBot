"""Resume evidence and recovery-budget checks at existing tmux lifecycle seams."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from pinky_daemon import tmux_session
from pinky_daemon.codex_tmux_session import CodexTmuxSession
from pinky_daemon.streaming_session import StreamingSessionConfig
from pinky_daemon.tmux_session import TmuxCommandResult, TmuxSession, _QueuedTurn
from pinky_daemon.transport_state import SessionState
from tests.test_tmux_session import _make_mock_tmux


@pytest.fixture
def make_session(tmp_path, monkeypatch):
    monkeypatch.setenv("PINKY_RESUME_FAILSAFE", "1")
    monkeypatch.setattr(tmux_session, "_POST_SPAWN_LIVENESS_DELAY_SEC", 0)
    monkeypatch.setattr(TmuxSession, "_spawn_cleanup_state_dir", lambda self: tmp_path)

    def make(cls=TmuxSession):
        tmux = _make_mock_tmux()
        ss = cls(StreamingSessionConfig(
            agent_name="sample", working_dir=str(tmp_path), resume_handle="saved-handle",
        ), tmux_control=tmux)
        ss._skip_wake_prompt_for_tests = True
        ss._start_tailer = AsyncMock()
        ss._prepare_tmux_spawn = lambda: None
        ss._has_prior_transcript = lambda: True
        ss._stream_event_callback = AsyncMock()
        if cls is CodexTmuxSession:
            ss._codex_dismiss_nux_and_ready = AsyncMock()
        return ss, tmux

    return make


@pytest.mark.parametrize("cls", [TmuxSession, CodexTmuxSession])
@pytest.mark.parametrize("diagnostic", ["", "authentication required", "rate limit", "invalid model"])
async def test_unknown_resumed_pane_death_does_not_retry(make_session, cls, diagnostic):
    ss, tmux = make_session(cls)
    tmux.has_session = AsyncMock(side_effect=[False, False])
    tmux.capture_pane = AsyncMock(return_value=TmuxCommandResult(0, diagnostic, ""))
    with pytest.raises(RuntimeError):
        await ss.connect()
    assert ss.state == SessionState.DEAD
    assert tmux.new_session.await_count == 1
    assert tmux.kill_session.await_count == 1


@pytest.mark.parametrize("cls", [TmuxSession, CodexTmuxSession])
async def test_cancelled_liveness_probe_cleans_without_retry(make_session, cls):
    ss, tmux = make_session(cls)
    tmux.has_session = AsyncMock(side_effect=[False, asyncio.CancelledError()])
    with pytest.raises(asyncio.CancelledError):
        await ss.connect()
    assert tmux.new_session.await_count == 1
    assert tmux.kill_session.await_count == 1
    assert ss.state == SessionState.DEAD


@pytest.mark.parametrize("cls", [TmuxSession, CodexTmuxSession])
async def test_force_fresh_overrides_existing_transcript(make_session, cls):
    ss, _ = make_session(cls)
    ss._config.force_fresh_context_once = True
    command = ss._build_claude_cmd()
    assert "--continue" not in command
    assert "resume --last" not in command
    assert ss._last_launch_used_continue is False


@pytest.mark.parametrize("cls", [TmuxSession, CodexTmuxSession])
async def test_unknown_wake_receipt_cannot_authorize_fresh_restart(make_session, monkeypatch, cls):
    ss, tmux = make_session(cls)
    ss._state_machine._state = SessionState.CONNECTED
    ss._session_ready_event.set()
    monkeypatch.setattr(tmux_session, "_WAKE_SUBMISSION_RECEIPT_TIMEOUT_SEC", 0.001)
    monkeypatch.setattr(tmux_session, "_WAKE_SUBMISSION_ENTER_RETRY_LIMIT", 0)
    monkeypatch.setattr(tmux_session, "_WAKE_SUBMISSION_RECEIPT_QUIESCENCE_SEC", 0.001)
    tmux.capture_pane = AsyncMock(return_value=TmuxCommandResult(0, "unknown composer", ""))
    ss._config.wake_submission_recovery_injector = AsyncMock(return_value=False)
    ss.force_restart = AsyncMock(return_value=True)
    receipt = asyncio.get_running_loop().create_future()
    turn = _QueuedTurn(
        prompt="Resume saved work", internal=True, reason="wake_resume", submission_receipt=receipt,
    )
    try:
        await ss._finish_submitted_turn(turn)
    except (RuntimeError, tmux_session._WakeSubmissionRecoveryScheduled):
        pass
    task = ss._wake_submission_recovery_task
    if task is not None:
        await asyncio.wait_for(task, 1)
    ss.force_restart.assert_not_awaited()
    assert await receipt is False


@pytest.mark.parametrize("cls", [TmuxSession, CodexTmuxSession])
async def test_existing_wake_recovery_budget_is_one_shot(make_session, cls):
    ss, _ = make_session(cls)
    ss.force_restart = AsyncMock(return_value=False)
    turn = _QueuedTurn(prompt="Wake", internal=True, reason="wake_resume")
    ss._wake_submission_transport_recovery_used = True
    scheduled = await ss._schedule_wake_submission_transport_recovery(turn)
    assert scheduled is False
    ss.force_restart.assert_not_awaited()


@pytest.mark.parametrize("cls", [TmuxSession, CodexTmuxSession])
async def test_replacement_nonzero_kill_blocks_target_spawn(make_session, cls):
    ss, tmux = make_session(cls)
    ss._state_machine._state = SessionState.CONNECTED
    tmux.kill_session = AsyncMock(return_value=TmuxCommandResult(1, "", "still live"))
    spawn = AsyncMock()
    with pytest.raises(RuntimeError, match="cleanup failed"):
        await ss.restart_transport(target_preflight=lambda: None, bring_up=spawn)
    spawn.assert_not_awaited()
