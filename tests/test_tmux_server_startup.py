"""Startup absence classification and subprocess output bounds."""

import asyncio
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from pinky_daemon import tmux_session
from pinky_daemon.command_runner import CommandResult, LocalCommandRunner
from pinky_daemon.isolated_launch_env import LaunchEnvError
from tests.test_tmux_legacy_socket_reap import Registry, launch_probe
from tests.tmux_server_env_support import seed


@pytest.mark.legacy_tmux_reap
@pytest.mark.parametrize("state", ["no_server", "no_session", "permission_error"])
async def test_legacy_absence_completes_but_real_errors_block(tmp_path, monkeypatch, state):
    seed(monkeypatch, tmp_path)
    monkeypatch.setenv("TMUX_TMPDIR", str(tmp_path))
    registry = Registry(tmp_path / "settings.sqlite")
    calls = []

    async def run(self, argv, **kwargs):
        calls.append(argv)
        assert "-L" in argv and argv[argv.index("-L") + 1] == "default"
        assert kwargs["env"]["TMUX_TMPDIR"] == str(tmp_path)
        if state == "no_server":
            return CommandResult(1, b"", b"no server running on /synthetic/socket\n")
        if state == "no_session":
            return (CommandResult(0, b"unrelated\n", b"") if "list-sessions" in argv else
                    CommandResult(1, b"", b"can't find session: synthetic\n"))
        return CommandResult(1, b"", b"permission denied\n")

    monkeypatch.setattr(LocalCommandRunner, "run", run)
    monkeypatch.setattr(tmux_session._TmuxControl, "_server_socket_is_missing", lambda _: False)
    try:
        blocked = await tmux_session.reap_legacy_tmux_sessions(registry, log=lambda _: None)
        assert bool(blocked) is (state == "permission_error")
        assert bool(registry.writes) is (state != "permission_error")
        assert not any("kill-session" in argv for argv in calls)
        for agent in registry.agents:
            launch = launch_probe("app_server", tmp_path, monkeypatch, registry, agent=agent.name)
            await launch(allowed=state != "permission_error")
    finally:
        registry.db.close()


async def test_unrelated_tests_record_reap_without_socket_access(_isolate_legacy_tmux_reap):
    assert await tmux_session.reap_legacy_tmux_sessions(object()) == set()
    _isolate_legacy_tmux_reap.assert_awaited_once()


async def test_cleanup_keeps_base_path_and_recorded_socket_despite_new_config(tmp_path, monkeypatch):
    seed(monkeypatch, tmp_path)
    monkeypatch.setenv("TMUX_TMPDIR", str(tmp_path))
    monkeypatch.setenv("PATH", "/synthetic/tools:/bin")
    monkeypatch.setenv("PINKY_TMUX_SOCKET", "bad/config")
    monkeypatch.setenv("PINKY_TMUX_PANE_PATH", "bad/path")
    seen = {}

    async def run(self, argv, **kwargs):
        seen.update(kwargs)
        return CommandResult(0, b"", b"")

    monkeypatch.setattr(LocalCommandRunner, "run", run)
    recorded = tmp_path / "recorded.sock"
    ctrl = tmux_session.production_tmux_control("pinky-test-agent", socket_path=str(recorded), cleanup=True)
    await ctrl._run("has-session")
    assert ctrl._local_socket_path() == recorded
    assert seen["env"]["PATH"].startswith("/synthetic/tools:/bin:")
    assert seen["env"]["TMUX_TMPDIR"] == str(tmp_path)


async def test_recorded_cleanup_control_cannot_start_old_server(tmp_path, monkeypatch):
    seed(monkeypatch, tmp_path)
    calls = AsyncMock(side_effect=AssertionError("cleanup-only control executed a launch"))
    monkeypatch.setattr(LocalCommandRunner, "run", calls)
    ctrl = tmux_session.production_tmux_control("pinky-test-agent", socket_path=str(tmp_path / "recorded.sock"), cleanup=True)
    with pytest.raises(LaunchEnvError, match="cleanup-only"):
        await ctrl.new_session(cwd=str(tmp_path), command="true", env={})
    calls.assert_not_awaited()


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
async def test_bounded_client_read_refuses_oversize(stream):
    with pytest.raises(ExceptionGroup):
        await LocalCommandRunner().run([sys.executable, "-I", "-c",
            f"import sys;sys.{stream}.write('x'*1000000)"], max_output_bytes=1024, timeout=5)


async def test_bounded_client_read_has_deadline():
    with pytest.raises(asyncio.TimeoutError):
        await LocalCommandRunner().run([sys.executable, "-I", "-c", "import time;time.sleep(60)"],
                                      max_output_bytes=1024, timeout=0.05)


async def test_client_cleanup_is_bounded_when_pipe_stays_open(monkeypatch):
    async def never_eof(*args):
        await asyncio.Event().wait()

    pipe = SimpleNamespace(read=never_eof)
    transport = SimpleNamespace(close=Mock())
    proc = SimpleNamespace(stdout=pipe, stderr=pipe, kill=Mock(), wait=AsyncMock(return_value=0), _transport=transport)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=proc))
    with pytest.raises(asyncio.TimeoutError):
        async with asyncio.timeout(2):
            await LocalCommandRunner().run(["synthetic-client"], max_output_bytes=1024, timeout=0.01)
    proc.kill.assert_called_once()
    transport.close.assert_called_once()
