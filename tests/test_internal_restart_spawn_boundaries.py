"""Real spawn orchestration with disposable external process boundaries."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from pinky_daemon.codex_tmux_session import CodexTmuxSession
from pinky_daemon.tmux_session import TmuxSession, _QueuedTurn
from pinky_daemon.transport_state import SessionState
from tests.recovery_test_support import lifecycle_harness as lifecycle_harness
from tests.recovery_test_support import set_flags

_TMUX_SPAWN = TmuxSession._spawn_tmux_repl
_CODEX_TMUX_SPAWN = CodexTmuxSession._spawn_tmux_repl


@pytest.mark.parametrize("source", ["claude_sdk", "codex_cli"])
@pytest.mark.parametrize("phase", ["container", "probe", "trust", "credentials", "new_session"])
async def test_real_spawn_stops_after_revoked_external_await(
    lifecycle_harness, monkeypatch, source, phase,
):
    h = lifecycle_harness
    ss = h.seed((source, "tmux"))
    monkeypatch.setattr(TmuxSession, "_spawn_tmux_repl", _TMUX_SPAWN)
    monkeypatch.setattr(CodexTmuxSession, "_spawn_tmux_repl", _CODEX_TMUX_SPAWN)
    if source == "codex_cli":
        ss._codex_dismiss_nux_and_ready = AsyncMock()
    ss._ensure_container_started = AsyncMock()
    ss._reap_retained_spawn_cleanup_debt = AsyncMock()
    ss._seed_container_trust = AsyncMock()
    ss._seed_container_home_creds = AsyncMock()
    ss._start_tailer = AsyncMock()
    ss._build_repl_env = lambda: {"HOME": ss._config.working_dir}
    ss._build_claude_cmd = lambda: "disposable-command"
    ss._prepare_tmux_spawn = lambda: None
    ss._tmux.has_session = AsyncMock(return_value=False)
    ss._tmux.new_session = AsyncMock(return_value=SimpleNamespace(ok=True))
    entered, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def boundary(*args, **kwargs):
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled.set()
            await release.wait()
        return SimpleNamespace(ok=True) if phase == "new_session" else False

    target = {
        "container": ss._ensure_container_started,
        "probe": ss._tmux.has_session,
        "trust": ss._seed_container_trust,
        "credentials": ss._seed_container_home_creds,
        "new_session": ss._tmux.new_session,
    }[phase]
    target.side_effect = boundary
    caller = asyncio.create_task(ss.force_restart())
    stop = waiter = None
    try:
        await asyncio.wait_for(entered.wait(), 2)
        stop = asyncio.create_task(h.client.post("/agents/sample/stop"))
        waiter = asyncio.create_task(cancelled.wait())
        await asyncio.wait({stop, waiter}, timeout=2, return_when=asyncio.FIRST_COMPLETED)
        assert cancelled.is_set(), "Terminal path did not reach the actual spawn owner"
        assert not stop.done()
        release.set()
        assert (await stop).status_code == 200
        assert await caller is False
        assert ss._tmux.new_session.await_count == int(phase == "new_session")
        ss._start_tailer.assert_not_awaited()
        if source == "codex_cli":
            ss._codex_dismiss_nux_and_ready.assert_not_awaited()
        assert ss.state == SessionState.DEAD
        assert ss._state_machine._in_flight is None
    finally:
        release.set()
        if waiter:
            waiter.cancel()
        await asyncio.gather(*(t for t in (caller, stop, waiter) if t), return_exceptions=True)


async def test_owned_restart_preserves_wake_initiator_fresh_latch(lifecycle_harness, monkeypatch):
    h = lifecycle_harness
    set_flags(monkeypatch, "a")
    ss = h.seed(("claude_sdk", "tmux"))
    observed = []

    async def spawn(owner):
        if owner is ss:
            observed.append(ss._config.force_fresh_context_once)

    h.control.start_hook = spawn
    turn = _QueuedTurn(prompt="saved wake", internal=True, reason="wake_context_restart")
    ss._wake_submission_recovery_task = asyncio.create_task(
        ss._run_wake_submission_transport_recovery(turn),
    )
    await ss._wake_submission_recovery_task
    assert observed == [True]
    assert not ss._wake_submission_recovery_task.cancelled()
    assert ss.state == SessionState.CONNECTED
    assert ss._state_machine._in_flight is None


@pytest.mark.parametrize("mode", ["a", "b", "both", "off"])
async def test_retained_codex_startup_keeps_task_scoped_connect_permit(monkeypatch, tmp_path, mode):
    from tests.test_codex_app_server_phase1 import _session

    set_flags(monkeypatch, mode)
    ss = _session(monkeypatch, tmp_path, mode="happy", init_timeout=2)
    try:
        await ss.connect()
        first = ss._app_proc
        await ss.restart_transport()
        assert ss.state == SessionState.CONNECTED
        assert ss._use_app_server
        assert ss._app_client is not None
        assert ss._app_proc is not first
        assert first.returncode is not None
        assert getattr(ss, "_replacement_connect_owner", None) is None
    finally:
        await ss.disconnect()


@pytest.mark.parametrize("mode", ["a", "b", "both"])
async def test_real_codex_initialize_is_joined_before_retirement(monkeypatch, tmp_path, mode):
    from tests.test_codex_app_server_phase1 import _session

    set_flags(monkeypatch, mode)
    ss = _session(monkeypatch, tmp_path, mode="hang-init", init_timeout=30)
    ss._state_machine._state = SessionState.CONNECTED
    task = asyncio.create_task(ss.force_restart())
    try:
        async with asyncio.timeout(2):
            while ss._app_client is None:
                await asyncio.sleep(0.01)
        proc = ss._app_proc
        await ss.retire_transport()
        assert await task is False
        assert not task.cancelled()
        assert proc.returncode is not None
        assert ss._app_client is None
        assert ss._app_proc is None
        assert ss.state == SessionState.DEAD
        assert ss._state_machine._in_flight is None
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await ss.disconnect()


@pytest.mark.parametrize("mode", ["a", "b", "both"])
async def test_startup_refusal_after_initialize_settles_cold_token(lifecycle_harness, monkeypatch, mode):
    from pinky_daemon.streaming_session import StreamingSession

    h = lifecycle_harness
    set_flags(monkeypatch, mode)
    old = h.seed()
    assert (await h.client.post("/agents/sample/stop")).status_code == 200
    ss = StreamingSession(old._config)
    entered, release = asyncio.Event(), asyncio.Event()

    async def spawn(owner):
        if owner is ss:
            entered.set()
            await release.wait()

    h.control.start_hook = spawn
    connect = asyncio.create_task(ss.connect())
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert ss.state == SessionState.BOOTING
        await ss.retire_transport()
        release.set()
        with pytest.raises(RuntimeError, match="retired or quiescing"):
            await connect
        assert ss.state == SessionState.DEAD
        assert ss._state_machine._in_flight is None
    finally:
        release.set()
        await asyncio.gather(connect, return_exceptions=True)
