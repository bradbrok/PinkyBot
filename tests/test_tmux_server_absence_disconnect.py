"""Verified server absence ends a failed session; uncertain probes do not."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from pinky_daemon import tmux_session
from pinky_daemon.command_runner import CommandResult, ContainerCommandRunner
from pinky_daemon.streaming_session import StreamingSessionConfig
from pinky_daemon.tmux_session import TmuxCommandResult, TmuxSession, _QueuedTurn, _TmuxControl
from pinky_daemon.transport_state import SessionState

NO_SERVER = "no server running on /tmp/tmux-1000/test-socket"
NO_SOCKET = "error connecting to /tmp/tmux-1000/test-socket (No such file or directory)"


def _result(stderr="", *, rc=1, stdout=""):
    return TmuxCommandResult(returncode=rc, stdout=stdout, stderr=stderr)


def _setup(tmp_path, monkeypatch, failed, listing=None, *, wrapped=False, legacy=False):
    listing = listing if listing is not None else _result(rc=0, stdout="owned\n")
    calls, logs = [], []

    async def run(argv, **kwargs):
        calls.append(tuple(argv))
        if "list-sessions" in argv:
            if isinstance(listing, Exception):
                raise listing
            return listing
        if "load-buffer" in argv:
            return failed
        raise AssertionError(f"unexpected tmux command: {argv}")

    class Inner:
        async def run(self, argv, **kwargs):
            result = await run(argv, **kwargs)
            return CommandResult(result.returncode, result.stdout.encode(), result.stderr.encode())

    socket = tmp_path / "socket"
    socket.touch()
    runner = ContainerCommandRunner("test-container", inner=Inner()) if wrapped else None
    control = _TmuxControl("owned", socket_path=str(socket), command_runner=runner)
    if not wrapped:
        async def local_run(*args, **kwargs):
            return await run(args, **kwargs)

        monkeypatch.setattr(control, "_run", local_run)
    control.kill_session = AsyncMock(return_value=_result(rc=0))
    probe = AsyncMock(wraps=control._session_absence_is_verified)
    monkeypatch.setattr(control, "_session_absence_is_verified", probe)
    if legacy:
        control = SimpleNamespace(**control.__dict__, paste_text=control.paste_text)
        del control._session_absence_is_verified
    session = TmuxSession(
        StreamingSessionConfig(agent_name="test-agent", working_dir=str(tmp_path)),
        tmux_control=control,
    )
    session._state_machine._state = SessionState.CONNECTED
    session._skip_wake_prompt_for_tests = True
    monkeypatch.setattr(tmux_session, "_log", logs.append)
    return session, control, probe, calls, logs


def _turn():
    return _QueuedTurn(
        prompt="first", platform="test", chat_id="chat", message_id="message",
        completion_event=asyncio.Event(),
        scheduler_delivery=asyncio.get_running_loop().create_future(),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("wrapped", [False, True], ids=["local", "container"])
@pytest.mark.parametrize("stderr", [NO_SERVER, NO_SOCKET, "server exited unexpectedly"],
                         ids=["no-server", "no-socket", "listed-absent"])
async def test_verified_absence_exits_worker_and_next_turn_spawns(
    tmp_path, monkeypatch, wrapped, stderr,
):
    session, control, probe, calls, logs = _setup(
        tmp_path, monkeypatch, _result(stderr), _result(rc=0, stdout="other\n"), wrapped=wrapped,
    )
    notice = asyncio.Event()
    notices = []

    async def notify(response):
        notices.append(response)
        notice.set()

    session._response_callback = notify
    release, disconnected = asyncio.Event(), asyncio.Event()
    real_disconnect = session.disconnect
    tasks = []

    async def disconnect():
        tasks.append(asyncio.current_task())
        await release.wait()
        await real_disconnect()
        disconnected.set()

    disconnect_spy = AsyncMock(side_effect=disconnect)
    monkeypatch.setattr(session, "disconnect", disconnect_spy)
    turn = _turn()
    session._message_queue.put_nowait(turn)
    worker = session._worker_task = asyncio.create_task(session._message_worker())
    try:
        await asyncio.wait_for(notice.wait(), 2)
        await asyncio.sleep(0)
        assert worker.done(), "worker must exit before disconnect is allowed to cancel it"
        assert not worker.cancelled()
        assert worker.result() is None
        disconnect_spy.assert_awaited_once()
        probe.assert_awaited_once()
        assert turn.completion_event.is_set()
        assert turn.scheduler_delivery.result() is False
        assert session._inflight_turn is None
        assert len(notices) == 1 and notices[0].stop_reason == "delivery_error"
        assert any("server/session verified absent" in line for line in logs)
        if stderr == "server exited unexpectedly":
            assert sum("list-sessions" in call for call in calls) == 1
        else:
            assert not any("list-sessions" in call for call in calls)
        if wrapped:
            assert control._local_socket_path() is None
            assert all(call[:2] == ("podman", "exec") for call in calls)
            assert all("test-container" in call for call in calls)
        release.set()
        await asyncio.wait_for(disconnected.wait(), 2)
        assert session.state == SessionState.DEAD
        assert await session.send("before connect") is False
        spawned = []

        async def spawn():
            spawned.append(session.state)

        monkeypatch.setattr(session, "_spawn_tmux_repl", spawn)
        pasted = asyncio.Event()

        async def paste(text, **kwargs):
            assert text == "next"
            pasted.set()
            return _result(rc=0)

        monkeypatch.setattr(control, "paste_text", paste)
        await session.connect()
        assert spawned == [SessionState.RECONNECTING]
        assert session.state == SessionState.CONNECTED
        assert await session.send("next") is True
        await asyncio.wait_for(pasted.wait(), 2)
    finally:
        release.set()
        await real_disconnect()
        if tasks:
            await asyncio.gather(*tasks)


@pytest.mark.asyncio
@pytest.mark.parametrize("failed,listing,raises", [
    (_result("server exited unexpectedly"), None, False),
    (_result("permission denied"), None, False),
    (_result(NO_SERVER, stdout="unexpected"), None, False),
    (_result(NO_SERVER, rc=2), None, False),
    (_result("server exited unexpectedly"), _result("listing failed"), False),
    (_result("server exited unexpectedly"), RuntimeError("probe failed"), True),
], ids=["unexpected-exit", "permission", "stdout", "status", "listing-failed", "probe-raised"])
async def test_uncertain_absence_drops_turn_without_disconnect(
    tmp_path, monkeypatch, failed, listing, raises,
):
    session, control, probe, calls, logs = _setup(tmp_path, monkeypatch, failed, listing)
    real_disconnect = session.disconnect
    disconnect = AsyncMock()
    monkeypatch.setattr(session, "disconnect", disconnect)
    notice = asyncio.Event()

    async def notify(response):
        notice.set()

    session._response_callback = notify
    turn = _turn()
    session._message_queue.put_nowait(turn)
    worker = session._worker_task = asyncio.create_task(session._message_worker())
    try:
        await asyncio.wait_for(notice.wait(), 2)
        await asyncio.sleep(0)
        disconnect.assert_not_called()
        probe.assert_awaited_once()
        assert session.state == SessionState.CONNECTED
        assert not worker.done()
        assert session._inflight_turn is None
        assert turn.completion_event.is_set()
        assert turn.scheduler_delivery.result() is False
        assert sum("list-sessions" in call for call in calls) == 1
        if raises:
            assert any("absence probe failed" in line and "probe failed" in line for line in logs)
    finally:
        await real_disconnect()


@pytest.mark.asyncio
@pytest.mark.parametrize("stderr", [
    "can't find pane: owned", "can't find session: owned",
    "can only create exec sessions on running containers", "no such container: test-container",
    "container test-container is not running",
])
async def test_existing_dead_runtime_is_typed_and_skips_probe(tmp_path, monkeypatch, stderr):
    session, control, probe, calls, logs = _setup(tmp_path, monkeypatch, _result(stderr))
    disconnect = AsyncMock()
    monkeypatch.setattr(session, "disconnect", disconnect)
    turn = _turn()
    with pytest.raises(RuntimeError) as raised:
        await session._deliver_turn(turn)
    await asyncio.sleep(0)
    assert type(raised.value) is not RuntimeError
    assert str(raised.value) == f"tmux paste-buffer / send-keys failed: rc=1 stderr={stderr!r}"
    disconnect.assert_awaited_once()
    probe.assert_not_called()
    assert turn.completion_event.is_set()
    assert turn.scheduler_delivery.result() is False


@pytest.mark.asyncio
async def test_control_without_absence_probe_preserves_failure(tmp_path, monkeypatch):
    session, control, probe, calls, logs = _setup(
        tmp_path, monkeypatch, _result(NO_SERVER), legacy=True,
    )
    disconnect = AsyncMock()
    monkeypatch.setattr(session, "disconnect", disconnect)
    with pytest.raises(RuntimeError) as raised:
        await session._deliver_turn(_turn())
    await asyncio.sleep(0)
    assert type(raised.value) is RuntimeError
    assert session.state == SessionState.CONNECTED
    disconnect.assert_not_called()
    probe.assert_not_called()
