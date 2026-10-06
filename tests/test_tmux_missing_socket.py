"""Missing-socket absence through wrapped runners and real tmux output."""

import os
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path

import pytest

from pinky_daemon.command_runner import (
    CommandResult,
    ContainerCommandRunner,
    RunuserCommandRunner,
)
from pinky_daemon.tmux_session import (
    TmuxCommandResult,
    _TmuxControl,
    production_tmux_control,
)
from tests import tmux_socket_support


@pytest.mark.parametrize("wrapper", ["container", "runuser"])
@pytest.mark.asyncio
async def test_wrapped_tmux_missing_socket_proves_absence(wrapper, tmp_path):
    """Canonical ENOENT proves absence without statting another namespace."""
    calls = []

    class RecordingInner:
        async def run(self, argv, *, timeout=None, stdin_data=None):
            calls.append(list(argv))
            return CommandResult(
                1, b"", b"error connecting to /tmp/tmux-501/pinkybot (No such file or directory)\n"
            )

    if wrapper == "container":
        runner = ContainerCommandRunner("test-container", workdir=str(tmp_path), inner=RecordingInner())
        prefix = ["podman", "exec", "-w", str(tmp_path), "--", "test-container"]
    else:
        runner = RunuserCommandRunner("test-user", inner=RecordingInner())
        prefix = ["runuser", "-u", "test-user", "--"]
    control = production_tmux_control("test-session", command_runner=runner)

    assert control._local_socket_path() is None
    assert await control.has_session() is False
    assert calls == [prefix + [
        "tmux", "-L", "pinkybot", "-f", "/dev/null", "has-session", "-t", "=test-session",
    ]]


@pytest.mark.parametrize("overlong", [False, True], ids=["missing-socket", "overlong-socket"])
def test_real_tmux_socket_error_classification(overlong, monkeypatch, record_property):
    """A private real client distinguishes ENOENT from ENAMETOOLONG."""
    binary = shutil.which("tmux")
    if binary is None:
        pytest.skip("tmux is not installed")
    directory = Path(tempfile.mkdtemp(prefix="tx", dir="/tmp"))
    try:
        assert directory.stat().st_mode & 0o777 == 0o700
        assert directory.stat().st_uid == os.getuid()
        home = directory / "home"
        home.mkdir(mode=0o700)
        label = "absence-" + uuid.uuid4().hex[:8]
        socket_dir = directory
        if overlong:
            socket_dir = directory / ("l" * 90)
            socket_dir.mkdir(mode=0o700)
        socket = socket_dir / f"tmux-{os.getuid()}" / label
        assert (len(os.fsencode(socket.resolve())) >= 100) is overlong
        # Register only this owned route, including the deliberate overlong probe.
        monkeypatch.setattr(
            tmux_socket_support, "_LABELS",
            tmux_socket_support._LABELS | {(str(socket_dir), label)},
        )
        env = {"HOME": str(home), "PATH": "/usr/bin:/bin:/opt/homebrew/bin",
               "TMUX_TMPDIR": str(socket_dir)}
        result = subprocess.run(
            [binary, "-L", label, "-f", "/dev/null", "has-session", "-t", "=nope"],
            env=env, capture_output=True, text=True, timeout=5,
        )
        record_property("socket_path_length", len(os.fsencode(socket.resolve())))
        record_property("tmux_binary", binary)
        record_property("tmux_stderr", result.stderr)
        assert result.returncode == 1
        assert result.stdout == ""
        assert ("File name too long" if overlong else "No such file or directory") in result.stderr
        decoded = TmuxCommandResult(result.returncode, result.stdout, result.stderr)
        assert _TmuxControl._server_absence_is_reported(decoded) is not overlong
    finally:
        shutil.rmtree(directory)
