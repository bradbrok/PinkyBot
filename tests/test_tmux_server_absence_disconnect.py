"""Verified server absence ends a failed session; uncertain probes do not."""
import asyncio
import os
import shutil
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from pinky_daemon import tmux_session
from pinky_daemon.command_runner import CommandResult, ContainerCommandRunner
from pinky_daemon.streaming_session import StreamingSessionConfig
from pinky_daemon.tmux_server_env import ServerConfig
from pinky_daemon.tmux_session import TmuxCommandResult, TmuxSession, _QueuedTurn, _TmuxControl
from pinky_daemon.transport_state import SessionState
from tests.tmux_socket_support import private_socket

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
    control.has_session = AsyncMock(return_value=False)
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

    async def disconnect(**kwargs):
        tasks.append(asyncio.current_task())
        await release.wait()
        await real_disconnect(**kwargs)
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
        control.kill_session.assert_not_called()
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


def _real_control(socket):
    binary = shutil.which("tmux")
    assert binary is not None
    return _TmuxControl(
        "owned", tmux_binary=binary, socket_path=socket,
        server_config=ServerConfig("test", dict(os.environ)),
    )


async def _create_runtime(control, name, tmp_path, pids):
    result = await control._run("new-session", "-d", "-s", name, "-c", str(tmp_path),
                                "/bin/sleep 60")
    assert result.ok, result.stderr
    identity = await control._run("display-message", "-p", "-t", f"={name}:0.0",
                                  "#{pid} #{pane_pid}")
    assert identity.ok, identity.stderr
    values = list(map(int, identity.stdout.split()))
    assert len(values) == 2
    pids.update(values)


async def _finish_runtime(control, pids):
    await control._run("kill-server")
    for _ in range(200):
        active = []
        for pid in pids:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                continue
            active.append(pid)
        if not active:
            break
        await asyncio.sleep(0.01)
    assert not active, "private server and pane must stop before socket cleanup"


def _session_for(control, tmp_path):
    session = TmuxSession(
        StreamingSessionConfig(agent_name="test-agent", working_dir=str(tmp_path)),
        tmux_control=control,
    )
    session._state_machine._state = SessionState.CONNECTED
    session._skip_wake_prompt_for_tests = True
    return session


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["canonical", "listing"])
async def test_fresh_runtime_survives_prior_absence_result(tmp_path, monkeypatch, kind):
    with private_socket() as socket:
        control = _real_control(socket)
        pids, tasks = set(), []
        real_run = control._run
        try:
            if kind == "canonical":
                failed = await control.paste_text("first", enter=False)
                assert not failed.ok and control._server_absence_is_reported(failed)
                prior_listing = None
            else:
                await _create_runtime(control, "other", tmp_path, pids)
                failed = await control._run("load-buffer", "-Z")
                assert not failed.ok and not control._server_absence_is_reported(failed)
                assert not tmux_session._is_dead_runtime_stderr(failed.stderr)
                prior_listing = await control._run("list-sessions", "-F", "#{session_name}")
                assert prior_listing.ok and prior_listing.stdout == "other\n"
            await _create_runtime(control, "owned", tmp_path, pids)
            assert await control.has_session() is True

            async def observed_run(*args, **kwargs):
                if kind == "listing" and args[0] == "list-sessions":
                    return prior_listing
                return await real_run(*args, **kwargs)

            monkeypatch.setattr(control, "_run", observed_run)
            monkeypatch.setattr(control, "paste_text", AsyncMock(return_value=failed))
            kill = AsyncMock(wraps=control.kill_session)
            monkeypatch.setattr(control, "kill_session", kill)
            session = _session_for(control, tmp_path)
            real_disconnect = session.disconnect

            async def disconnect(**kwargs):
                tasks.append(asyncio.current_task())
                await real_disconnect(**kwargs)

            monkeypatch.setattr(session, "disconnect", disconnect)
            turn = _turn()
            with pytest.raises(RuntimeError) as raised:
                await session._deliver_turn(turn)
            await asyncio.sleep(0)
            if tasks:
                await asyncio.wait_for(asyncio.gather(*tasks), 3)
            assert await control.has_session() is True, "a recreated owned runtime must survive"
            assert not tasks and session.state == SessionState.CONNECTED
            assert type(raised.value) is RuntimeError
            kill.assert_not_called()
            assert turn.completion_event.is_set() and turn.scheduler_delivery.result() is False
        finally:
            await _finish_runtime(control, pids)


@pytest.mark.asyncio
@pytest.mark.parametrize("wrapped", [False, True], ids=["local", "container"])
async def test_verified_absence_disconnect_does_not_kill(tmp_path, monkeypatch, wrapped):
    session, control, probe, calls, logs = _setup(
        tmp_path, monkeypatch, _result(NO_SERVER), wrapped=wrapped,
    )
    real_disconnect = session.disconnect
    finished = asyncio.Event()

    async def disconnect(**kwargs):
        await real_disconnect(**kwargs)
        finished.set()

    spy = AsyncMock(side_effect=disconnect)
    monkeypatch.setattr(session, "disconnect", spy)
    turn = _turn()
    with pytest.raises(RuntimeError) as raised:
        await session._deliver_turn(turn)
    await asyncio.wait_for(finished.wait(), 2)
    assert type(raised.value).__name__ == "_DeadRuntimeError"
    assert session.state == SessionState.DEAD
    control.kill_session.assert_not_called()
    spy.assert_awaited_once_with(kill_tmux=False)
    control.has_session.assert_awaited_once()
    probe.assert_awaited_once()
    assert turn.completion_event.is_set() and turn.scheduler_delivery.result() is False


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["canonical", "listing"])
async def test_runtime_created_after_reprobe_survives_disconnect(tmp_path, monkeypatch, kind):
    with private_socket() as socket:
        control = _real_control(socket)
        pids = set()
        try:
            if kind == "canonical":
                failed = await control.paste_text("first", enter=False)
                assert not failed.ok and control._server_absence_is_reported(failed)
            else:
                await _create_runtime(control, "other", tmp_path, pids)
                failed = await control._run("load-buffer", "-Z")
                assert not failed.ok and not tmux_session._is_dead_runtime_stderr(failed.stderr)
            kill = AsyncMock(wraps=control.kill_session)
            monkeypatch.setattr(control, "kill_session", kill)
            fresh_check = AsyncMock(wraps=control.has_session)
            monkeypatch.setattr(control, "has_session", fresh_check)
            monkeypatch.setattr(control, "paste_text", AsyncMock(return_value=failed))
            session = _session_for(control, tmp_path)
            real_disconnect = session.disconnect
            finished = asyncio.Event()

            async def disconnect(**kwargs):
                await _create_runtime(control, "owned", tmp_path, pids)
                await real_disconnect(**kwargs)
                finished.set()

            monkeypatch.setattr(session, "disconnect", disconnect)
            turn = _turn()
            with pytest.raises(RuntimeError) as raised:
                await session._deliver_turn(turn)
            await asyncio.wait_for(finished.wait(), 3)
            assert await control.has_session() is True, "teardown must not kill a later generation"
            assert session.state == SessionState.DEAD
            kill.assert_not_called()
            assert fresh_check.await_count == 2
            assert type(raised.value).__name__ == "_DeadRuntimeError"
            assert turn.completion_event.is_set() and turn.scheduler_delivery.result() is False
        finally:
            await _finish_runtime(control, pids)


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [TimeoutError("fresh check timeout"),
                                  PermissionError("fresh check denied"),
                                  RuntimeError("fresh check uncertain")])
async def test_reprobe_exception_preserves_connected(tmp_path, monkeypatch, error):
    session, control, probe, calls, logs = _setup(tmp_path, monkeypatch, _result(NO_SERVER))
    control.has_session.side_effect = error
    disconnect = AsyncMock()
    monkeypatch.setattr(session, "disconnect", disconnect)
    turn = _turn()
    with pytest.raises(RuntimeError) as raised:
        await session._deliver_turn(turn)
    await asyncio.sleep(0)
    assert type(raised.value) is RuntimeError
    assert session.state == SessionState.CONNECTED
    disconnect.assert_not_called()
    control.has_session.assert_awaited_once()
    assert any("absence re-probe failed" in line and str(error) in line for line in logs)
    assert turn.completion_event.is_set() and turn.scheduler_delivery.result() is False


@pytest.mark.asyncio
async def test_default_disconnect_still_kills_once(tmp_path, monkeypatch):
    session, control, probe, calls, logs = _setup(tmp_path, monkeypatch, _result("permission denied"))
    await session.disconnect()
    control.kill_session.assert_awaited_once_with()
    assert session.state == SessionState.DEAD
