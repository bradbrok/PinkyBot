"""Startup absence classification and subprocess output bounds."""

import asyncio
import sys

import pytest

from pinky_daemon import tmux_session
from pinky_daemon.command_runner import CommandResult, LocalCommandRunner
from tests.test_tmux_legacy_socket_reap import Registry
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
    finally:
        registry.db.close()


async def test_unrelated_tests_record_reap_without_socket_access(_isolate_legacy_tmux_reap):
    assert await tmux_session.reap_legacy_tmux_sessions(object()) == set()
    _isolate_legacy_tmux_reap.assert_awaited_once()


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
async def test_bounded_client_read_refuses_oversize(stream):
    with pytest.raises(ExceptionGroup):
        await LocalCommandRunner().run([sys.executable, "-I", "-c",
            f"import sys;sys.{stream}.write('x'*1000000)"], max_output_bytes=1024, timeout=5)


async def test_bounded_client_read_has_deadline():
    with pytest.raises(asyncio.TimeoutError):
        await LocalCommandRunner().run([sys.executable, "-I", "-c", "import time;time.sleep(60)"],
                                      max_output_bytes=1024, timeout=0.05)
