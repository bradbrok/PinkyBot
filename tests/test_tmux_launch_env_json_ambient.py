"""Ambient shell state and undecodable values do not prevent child startup."""

import asyncio
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from pinky_daemon.codex_app_server_tmux import CodexAppServerSupervisor
from pinky_daemon.codex_tmux_session import CodexTmuxSession
from pinky_daemon.streaming_session import StreamingSessionConfig
from pinky_daemon.tmux_session import _TmuxControl
from tests.tmux_env_r3_support import SECRET, probe_command
from tests.tmux_env_support import LaunchRecorder, secret_files

SHELL_NAMES = (
    "PWD", "OLDPWD", "SHLVL", "_", "BASHOPTS", "BASH_VERSINFO", "EUID", "PPID",
    "SHELLOPTS", "UID", "IFS", "ENV", "BASH_ENV", "PS4",
)
TMUX_BINARY = shutil.which("tmux")


@pytest.fixture
def home(tmp_path):
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    with patch.dict(os.environ, {"HOME": str(home), "PATH": os.defpath}, clear=True):
        yield home


def ambient(home, transport):
    if transport == "repl":
        session = CodexTmuxSession(StreamingSessionConfig(agent_name="test", working_dir=str(home)),
                                   tmux_control=MagicMock())
        return session._build_repl_env()
    with patch.object(CodexAppServerSupervisor, "_resolve_sock_dir", return_value=(str(home), False)):
        supervisor = CodexAppServerSupervisor("test", working_dir=str(home))
    return supervisor._build_env()


@pytest.mark.parametrize("name", SHELL_NAMES)
async def test_strict_boundary_refuses_shell_owned_and_control_names(home, name):
    recorder = LaunchRecorder(home)
    control = _TmuxControl("strict-shell-policy", command_runner=recorder)
    with pytest.raises(ValueError) as error:
        await control.new_session(cwd=str(home), command="true", env={name: SECRET})
    assert SECRET not in str(error.value)
    assert not recorder.calls
    assert not secret_files(home, SECRET)


@pytest.mark.parametrize("transport", ["repl", "app_server"])
@pytest.mark.parametrize("shell", ["sh", "bash"])
async def test_shell_owned_ambient_is_dropped_and_child_runs(home, monkeypatch, transport, shell):
    for name in SHELL_NAMES:
        monkeypatch.setenv(name, "17" if name in {"UID", "EUID", "PPID"} else "synthetic-shell-state")
    monkeypatch.setenv("SECRET", SECRET)
    env = ambient(home, transport)
    assert not set(SHELL_NAMES) & set(env)
    assert env["SECRET"] == SECRET
    recorder = LaunchRecorder(home)
    control = _TmuxControl("ambient-shell-state", command_runner=recorder)
    await control.new_session(cwd=str(home), command=probe_command(), env=env)
    binary = "/bin/sh" if shell == "sh" else shutil.which("bash")
    assert binary
    argv = [binary, *(["--posix"] if shell == "bash" else []), "-c", recorder.tmux_calls[-1][-1]]
    result = subprocess.run(argv, env={"HOME": str(home), "PATH": os.defpath},
                            capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["SECRET"] == SECRET
    assert not secret_files(home, SECRET)


@pytest.mark.parametrize("transport", ["repl", "app_server"])
@pytest.mark.parametrize("name", ["INVALID_UTF8", "LONG_NAME_" * 12])
async def test_non_utf8_ambient_value_is_warned_by_key_only_and_launches(
    home, monkeypatch, capsys, transport, name,
):
    assert os.supports_bytes_environ
    monkeypatch.setitem(os.environb, name.encode(), b"synthetic-undecodable-value-\xff")
    monkeypatch.setenv("SECRET", SECRET)
    env = ambient(home, transport)
    assert name not in env
    out = capsys.readouterr()
    log = out.out + out.err
    warning = next(line for line in log.splitlines() if "WARNING" in line)
    assert repr(name)[:64] in warning
    if len(repr(name)) > 64:
        assert name not in warning
    assert "synthetic-undecodable-value" not in log and SECRET not in log
    recorder = LaunchRecorder(home)
    control = _TmuxControl("utf8-ambient", command_runner=recorder)
    await control.new_session(cwd=str(home), command=probe_command(), env=env)
    result = subprocess.run(["/bin/sh", "-c", recorder.tmux_calls[-1][-1]],
                            env={"HOME": str(home), "PATH": os.defpath},
                            capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["SECRET"] == SECRET
    assert not secret_files(home, SECRET)


@pytest.mark.parametrize("transport", ["repl", "app_server"])
async def test_real_tmux_child_runs_with_shell_owned_daemon_environment(home, monkeypatch, transport):
    binary = TMUX_BINARY
    if binary is None:
        pytest.skip("real tmux is unavailable")
    socket_root = Path(tempfile.mkdtemp(prefix="json-tmux-", dir="/tmp"))
    socket = socket_root / "test.sock"
    output = home / "child.json"
    control = _TmuxControl("real-ambient", tmux_binary=binary, socket_path=str(socket))
    for name in SHELL_NAMES:
        monkeypatch.setenv(name, "17" if name in {"UID", "EUID", "PPID"} else "synthetic-shell-state")
    monkeypatch.setenv("SECRET", SECRET)
    env = ambient(home, transport)
    # Do not let inherited daemon state explain delivery or bypass the builder.
    with patch.dict(os.environ, {"HOME": str(home), "PATH": os.defpath}, clear=True):
        code = "import os,json,pathlib;pathlib.Path(" + repr(str(output)) + ").write_text(json.dumps({'SECRET':os.environ.get('SECRET')}))"
        command = shlex.join([sys.executable, "-I", "-c", code])
        try:
            result = await control.new_session(cwd=str(home), command=command, env=env)
            assert result.ok
            for _ in range(200):
                if output.exists():
                    break
                await asyncio.sleep(0.01)
            assert output.exists(), "tmux returned success but the child never ran"
            assert json.loads(output.read_text()) == {"SECRET": SECRET}
            output.unlink()
            assert not secret_files(home, SECRET)
        finally:
            subprocess.run([binary, "-S", str(socket), "kill-server"], capture_output=True, timeout=5,
                           env={"HOME": str(home), "PATH": os.defpath})
            shutil.rmtree(socket_root)
