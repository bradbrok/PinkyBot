"""Tests for the #149 phase-3 CommandRunner execution seam.

LocalCommandRunner is exercised against real short-lived subprocesses (it IS
the subprocess primitive, so mocking it would test nothing). RunuserCommandRunner
is tested via its argv-wrapping contract + a recording inner runner — no Linux,
root, or real ``runuser`` needed to assert the command shape.
"""

from __future__ import annotations

import asyncio
import sys

import pytest

from pinky_daemon.command_runner import (
    CommandResult,
    LocalCommandRunner,
    RunuserCommandRunner,
)


class TestCommandResult:
    def test_ok_true_on_zero(self):
        assert CommandResult(0, b"", b"").ok is True

    def test_ok_false_on_nonzero(self):
        assert CommandResult(1, b"", b"").ok is False


@pytest.mark.asyncio
class TestLocalCommandRunner:
    async def test_captures_stdout_and_returncode(self):
        r = await LocalCommandRunner().run(
            [sys.executable, "-c", "import sys; sys.stdout.write('hi'); sys.exit(3)"]
        )
        assert r.returncode == 3
        assert r.stdout == b"hi"
        assert r.ok is False

    async def test_captures_stderr(self):
        r = await LocalCommandRunner().run(
            [sys.executable, "-c", "import sys; sys.stderr.write('boom')"]
        )
        assert r.returncode == 0
        assert b"boom" in r.stderr

    async def test_feeds_stdin(self):
        r = await LocalCommandRunner().run(
            [sys.executable, "-c", "import sys; sys.stdout.write(sys.stdin.read())"],
            stdin=b"piped",
        )
        assert r.stdout == b"piped"

    async def test_timeout_raises_and_kills(self):
        # A 30s sleep with a 0.2s ceiling must raise rather than hang.
        with pytest.raises(asyncio.TimeoutError):
            await LocalCommandRunner().run(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                timeout=0.2,
            )


class _RecordingRunner(LocalCommandRunner):
    """Inner runner double that records argv instead of executing."""

    def __init__(self):
        self.calls: list[tuple[list[str], float | None, bytes | None]] = []

    async def run(self, argv, *, timeout=None, stdin=None):
        self.calls.append((argv, timeout, stdin))
        return CommandResult(0, b"", b"")


@pytest.mark.asyncio
class TestRunuserCommandRunner:
    async def test_wrap_shape(self):
        r = RunuserCommandRunner("pinky-mora")
        assert r.wrap(["tmux", "has-session", "-t", "x"]) == [
            "runuser", "-u", "pinky-mora", "--", "tmux", "has-session", "-t", "x",
        ]

    async def test_custom_runuser_binary(self):
        r = RunuserCommandRunner("pinky-mora", runuser_binary="/usr/sbin/runuser")
        assert r.wrap(["tmux"])[0] == "/usr/sbin/runuser"

    async def test_run_delegates_wrapped_argv_to_inner(self):
        inner = _RecordingRunner()
        r = RunuserCommandRunner("pinky-mora", inner=inner)
        await r.run(["tmux", "kill-session"], timeout=5.0, stdin=b"x")
        assert len(inner.calls) == 1
        argv, timeout, stdin = inner.calls[0]
        assert argv == ["runuser", "-u", "pinky-mora", "--", "tmux", "kill-session"]
        assert timeout == 5.0  # timeout/stdin pass through unchanged
        assert stdin == b"x"

    async def test_terminator_isolates_wrapped_options(self):
        # A wrapped arg that looks like a runuser option must sit AFTER ``--``.
        r = RunuserCommandRunner("pinky-mora")
        wrapped = r.wrap(["-l"])
        assert wrapped.index("--") < wrapped.index("-l")

    async def test_empty_username_rejected(self):
        with pytest.raises(ValueError):
            RunuserCommandRunner("")
