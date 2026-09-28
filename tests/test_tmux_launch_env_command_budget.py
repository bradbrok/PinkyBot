"""The loader leaves room for ordinary commands in the tmux message budget."""

import asyncio
import importlib
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from pinky_daemon import tmux_launch_env, tmux_session
from pinky_daemon.command_runner import RunuserCommandRunner
from pinky_daemon.tmux_session import _TmuxControl
from tests.tmux_env_r3_support import SECRET
from tests.tmux_env_support import LaunchRecorder, secret_files

TMUX_BINARY = shutil.which("tmux")


@pytest.fixture
def home(tmp_path):
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    with patch.dict(os.environ, {"HOME": str(home), "PATH": os.defpath}, clear=True):
        yield home


def long_launch(home):
    cwd = home / ("a" * 200) / ("b" * 200) / ("c" * 200)
    cwd.mkdir(parents=True, mode=0o700)
    code = "import pathlib;pathlib.Path('child-ran').write_text('ok')"
    command = shlex.join([
        sys.executable, "-I", "-c", code,
        "--model", "synthetic", "--config", "note=" + "p" * 4200,
    ])
    assert len(command.encode()) >= 4096
    env = {f"EMPTY_FORWARD_VALUE_{i:02}": "" for i in range(20)}
    env["SECRET"] = SECRET
    return cwd, command, env


def test_loader_source_has_explicit_four_kib_budget():
    source = getattr(tmux_session, "_LAUNCH_ENV_LOADER_SOURCE", None)
    assert isinstance(source, str), "tmux must receive a separate minimal loader"
    assert len(source.encode("utf-8")) <= 4096
    assert "def stage_env(" not in source and "def _sweep(" not in source


def test_staging_and_loader_share_validation_objects():
    loader = importlib.import_module("pinky_daemon.tmux_launch_env_loader")
    for name in ("is_valid_key_name", "key_policy", "validate_env", "_private_regular", "_NONCE"):
        assert getattr(tmux_launch_env, name) is getattr(loader, name), name


@pytest.mark.parametrize("remote", [False, True])
async def test_real_new_session_argv_has_headroom_for_long_commands(home, remote, record_property):
    cwd, command, env = long_launch(home)
    recorder = LaunchRecorder(home)
    runner = RunuserCommandRunner("test", inner=recorder) if remote else recorder
    control = _TmuxControl("budget-" + "s" * 50, command_runner=runner)
    await control.new_session(cwd=str(cwd), command=command, env=env)
    argv = recorder.tmux_calls[-1]
    # Include terminators and conservative per-argument length framing.
    packed = sum(len(arg.encode("utf-8")) + 5 for arg in argv)
    record_property("packed_argv_bytes", packed)
    record_property("command_bytes", len(command.encode("utf-8")))
    assert packed <= 12 * 1024, "launch is too close to tmux's command-size limit"
    assert SECRET not in " ".join(argv)


async def test_real_tmux_executes_long_command_with_empty_environment_overrides(home):
    if TMUX_BINARY is None:
        pytest.skip("real tmux is unavailable")
    cwd, command, env = long_launch(home)
    socket_root = Path(tempfile.mkdtemp(prefix="tmux-budget-", dir="/tmp"))
    socket = socket_root / "server.sock"
    control = _TmuxControl("budget-real", tmux_binary=TMUX_BINARY, socket_path=str(socket))
    try:
        result = await control.new_session(cwd=str(cwd), command=command, env=env)
        assert result.ok, result.stderr
        for _ in range(200):
            if (cwd / "child-ran").exists():
                break
            await asyncio.sleep(0.01)
        assert (cwd / "child-ran").read_text() == "ok"
        assert not secret_files(home, SECRET)
    finally:
        subprocess.run([TMUX_BINARY, "-S", str(socket), "kill-server"],
                       env={"HOME": str(home), "PATH": os.defpath}, capture_output=True, timeout=5)
        shutil.rmtree(socket_root)
