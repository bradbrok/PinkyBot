"""Daemon tmux commands must address their session by EXACT name.

tmux resolves a bare ``-t NAME`` by exact name first, then by unique prefix,
then by fnmatch pattern. With only ``pinky-x-old`` running, a command aimed at
``pinky-x`` would otherwise land on ``pinky-x-old``: probing, capturing,
typing into, resizing, renaming or killing the wrong session.

Every test here runs against a PRIVATE tmux server (``-L test816-<random>``,
``TMUX`` removed from the environment) that is killed on teardown. The test
harness observes panes by their tmux ids (``%N``), never by name, so the
observations do not depend on the name-resolution behaviour under test.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import pytest

from pinky_daemon import tmux_dream_runner
from pinky_daemon.tmux_dream_runner import TmuxDreamConfig, TmuxDreamRunner
from pinky_daemon.tmux_session import _is_dead_runtime_stderr, _TmuxControl

TMUX = shutil.which("tmux")

pytestmark = pytest.mark.skipif(TMUX is None, reason="tmux binary not installed")


def _env_without_tmux() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k not in {"TMUX", "TMUX_PANE"}}


class PrivateTmux:
    """A throwaway tmux server on its own ``-L`` socket."""

    def __init__(self) -> None:
        self.socket_name = f"test816-{uuid.uuid4().hex[:12]}"

    def run(self, *args: str) -> subprocess.CompletedProcess[str]:
        assert self.socket_name.startswith("test816-")
        return subprocess.run(
            [TMUX, "-L", self.socket_name, *args],
            capture_output=True,
            text=True,
            env=_env_without_tmux(),
            timeout=10,
        )

    def new(self, name: str, command: str = "exec sleep 300") -> None:
        result = self.run("new-session", "-d", "-s", name, "-x", "120", "-y", "40", command)
        assert result.returncode == 0, result.stderr
        assert name in self.sessions()

    def sessions(self) -> list[str]:
        result = self.run("list-sessions", "-F", "#{session_name}")
        if result.returncode != 0:
            return []
        return sorted(result.stdout.splitlines())

    def pane_id(self, name: str) -> str:
        """The single pane id of session ``name``, found by exact comparison."""
        result = self.run("list-panes", "-a", "-F", "#{session_name}\t#{pane_id}")
        assert result.returncode == 0, result.stderr
        panes = [
            line.split("\t", 1)[1]
            for line in result.stdout.splitlines()
            if line.split("\t", 1)[0] == name
        ]
        assert len(panes) == 1, (name, result.stdout)
        return panes[0]

    def capture(self, name: str) -> str:
        result = self.run("capture-pane", "-p", "-t", self.pane_id(name), "-S", "-100")
        assert result.returncode == 0, result.stderr
        return result.stdout

    def pane_format(self, name: str, fmt: str) -> str:
        result = self.run("display-message", "-p", "-t", self.pane_id(name), fmt)
        assert result.returncode == 0, result.stderr
        return result.stdout.strip()

    def type_literal(self, name: str, text: str) -> None:
        result = self.run("send-keys", "-t", self.pane_id(name), "-l", "--", text)
        assert result.returncode == 0, result.stderr

    def wait_for_text(self, name: str, text: str, timeout: float = 10.0) -> str:
        deadline = time.monotonic() + timeout
        pane = ""
        while time.monotonic() < deadline:
            pane = self.capture(name)
            if text in pane:
                return pane
            time.sleep(0.05)
        raise AssertionError(f"{text!r} never appeared in {name}: {pane!r}")

    def control(self, session_name: str) -> _TmuxControl:
        return _TmuxControl(session_name, tmux_binary=TMUX, socket_name=self.socket_name)

    def close(self) -> None:
        self.run("kill-server")
        socket_dir = Path(os.environ.get("TMUX_TMPDIR") or "/tmp") / f"tmux-{os.getuid()}"
        (socket_dir / self.socket_name).unlink(missing_ok=True)


@pytest.fixture
def private_tmux(monkeypatch):
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.delenv("TMUX_PANE", raising=False)
    server = PrivateTmux()
    try:
        yield server
    finally:
        server.close()


# -- target-session commands ---------------------------------------------------


async def test_has_session_does_not_resolve_to_prefix_neighbour(private_tmux):
    private_tmux.new("pinky-x-old")
    control = private_tmux.control("pinky-x")

    assert await control.has_session() is False

    private_tmux.new("pinky-x")
    assert await control.has_session() is True


async def test_kill_session_never_kills_prefix_neighbour(private_tmux):
    private_tmux.new("pinky-x-old")
    control = private_tmux.control("pinky-x")

    # A missing owned session is the idempotent, verified-absent success it
    # has always been; the neighbour must survive it.
    result = await control.kill_session()
    assert result.ok
    assert private_tmux.sessions() == ["pinky-x-old"]

    private_tmux.new("pinky-x")
    result = await control.kill_session()
    assert result.ok
    assert private_tmux.sessions() == ["pinky-x-old"]


async def test_rename_session_never_renames_prefix_neighbour(private_tmux):
    private_tmux.new("pinky-x-old")
    control = private_tmux.control("pinky-x")

    result = await control.rename_session("login-hold-x")
    assert not result.ok
    assert "can't find session" in result.stderr
    assert private_tmux.sessions() == ["pinky-x-old"]

    private_tmux.new("pinky-x")
    result = await control.rename_session("login-hold-x")
    assert result.ok, result.stderr
    assert private_tmux.sessions() == ["login-hold-x", "pinky-x-old"]


# -- target-window / target-pane commands --------------------------------------


async def test_resize_window_never_resizes_prefix_neighbour(private_tmux):
    private_tmux.new("pinky-x-old")
    control = private_tmux.control("pinky-x")
    size = "#{window_width}x#{window_height}"

    result = await control.resize_window(cols=100, rows=30)
    assert not result.ok
    assert private_tmux.pane_format("pinky-x-old", size) == "120x40"

    private_tmux.new("pinky-x")
    result = await control.resize_window(cols=100, rows=30)
    assert result.ok, result.stderr
    assert private_tmux.pane_format("pinky-x", size) == "100x30"
    assert private_tmux.pane_format("pinky-x-old", size) == "120x40"


async def _deliver(control: _TmuxControl, method: str, text: str):
    if method == "send_keys":
        return await control.send_keys(text, enter=True)
    if method == "send_literal":
        return await control.send_literal(text)
    assert method == "paste_text"
    return await control.paste_text(text, enter=True, enter_delay_ms=0)


@pytest.mark.parametrize("method", ["send_keys", "send_literal", "paste_text"])
async def test_input_never_reaches_prefix_neighbour(private_tmux, method):
    private_tmux.new("pinky-x-old", "exec cat")
    control = private_tmux.control("pinky-x")

    result = await _deliver(control, method, "MARK-MISSING")
    assert not result.ok
    # The worker treats this exactly like a vanished pane: dead runtime.
    assert _is_dead_runtime_stderr(result.stderr), result.stderr
    await asyncio.sleep(0.3)
    assert "MARK-MISSING" not in private_tmux.capture("pinky-x-old")

    private_tmux.new("pinky-x", "exec cat")
    result = await _deliver(control, method, "MARK-EXACT")
    assert result.ok, result.stderr
    private_tmux.wait_for_text("pinky-x", "MARK-EXACT")
    assert "MARK-EXACT" not in private_tmux.capture("pinky-x-old")


async def test_missing_session_is_dead_runtime_without_any_neighbour(private_tmux):
    """The no-neighbour case keeps the same caller-visible classification."""
    private_tmux.new("unrelated")
    control = private_tmux.control("pinky-x")

    for method in ("send_keys", "send_literal", "paste_text"):
        result = await _deliver(control, method, "MARK")
        assert not result.ok
        assert _is_dead_runtime_stderr(result.stderr), (method, result.stderr)
    assert await control.has_session() is False
    assert (await control.kill_session()).ok
    assert private_tmux.sessions() == ["unrelated"]


async def test_capture_pane_never_reads_prefix_neighbour(private_tmux):
    private_tmux.new("pinky-x-old", "printf 'NEIGHBOUR-TEXT\\n'; exec cat")
    private_tmux.wait_for_text("pinky-x-old", "NEIGHBOUR-TEXT")
    control = private_tmux.control("pinky-x")

    result = await control.capture_pane(lines=50)
    assert not result.ok
    assert "NEIGHBOUR-TEXT" not in result.stdout

    private_tmux.new("pinky-x", "printf 'EXACT-TEXT\\n'; exec cat")
    private_tmux.wait_for_text("pinky-x", "EXACT-TEXT")
    result = await control.capture_pane(lines=50, escapes=True, join=True)
    assert result.ok, result.stderr
    assert "EXACT-TEXT" in result.stdout
    assert "NEIGHBOUR-TEXT" not in result.stdout


async def test_capture_pane_named_target_session_is_exact(private_tmux):
    private_tmux.new("login-hold-x-old", "printf 'NEIGHBOUR-TEXT\\n'; exec cat")
    private_tmux.wait_for_text("login-hold-x-old", "NEIGHBOUR-TEXT")
    control = private_tmux.control("pinky-x")

    result = await control.capture_pane(lines=50, join=True, target_session="login-hold-x")
    assert not result.ok
    assert "NEIGHBOUR-TEXT" not in result.stdout

    private_tmux.new("login-hold-x", "printf 'EXACT-TEXT\\n'; exec cat")
    private_tmux.wait_for_text("login-hold-x", "EXACT-TEXT")
    result = await control.capture_pane(lines=50, join=True, target_session="login-hold-x")
    assert result.ok, result.stderr
    assert "EXACT-TEXT" in result.stdout


# -- text arguments are typed, never parsed as tmux flags ------------------------


async def _type(control: _TmuxControl, method: str, text: str):
    if method == "send_literal":
        return await control.send_literal(text)
    assert method == "send_keys"
    return await control.send_keys(text, enter=False)


@pytest.mark.parametrize("text", ["-Rt=pinky-y:", "-hello", "-l", "--"])
@pytest.mark.parametrize("method", ["send_literal", "send_keys"])
async def test_text_starting_with_dash_is_typed_into_the_exact_pane(private_tmux, method, text):
    """Text that looks like tmux flags is typed as-is. Parsed as flags,
    ``-R`` would reset a terminal and a later ``-t`` would re-target the
    command at another session, overriding the exact target."""
    private_tmux.new("pinky-x", "exec cat")
    private_tmux.new("pinky-y", "exec cat")
    private_tmux.type_literal("pinky-y", "NEIGHBOUR")
    private_tmux.wait_for_text("pinky-y", "NEIGHBOUR")
    cursor = "#{cursor_x},#{cursor_y}"
    neighbour_cursor = private_tmux.pane_format("pinky-y", cursor)

    result = await _type(private_tmux.control("pinky-x"), method, text)

    assert result.ok, result.stderr
    pane = private_tmux.wait_for_text("pinky-x", text, timeout=3.0)
    assert pane.strip() == text
    await asyncio.sleep(0.2)
    assert private_tmux.pane_format("pinky-y", cursor) == neighbour_cursor
    assert private_tmux.capture("pinky-y").strip() == "NEIGHBOUR"


async def test_new_name_starting_with_dash_is_taken_as_the_name(private_tmux):
    private_tmux.new("pinky-x")
    control = private_tmux.control("pinky-x")

    result = await control.rename_session("-renamed")

    assert result.ok, result.stderr
    assert private_tmux.sessions() == ["-renamed"]


# -- dream runner ---------------------------------------------------------------


class _PrivateAsyncio:
    """``asyncio`` stand-in for the dream runner module: every tmux it spawns
    is pointed at the private server; anything else is refused."""

    def __init__(self, socket_name: str) -> None:
        self._socket_name = socket_name

    def __getattr__(self, name: str):
        return getattr(asyncio, name)

    async def create_subprocess_exec(self, program, *args, **kwargs):
        if program != "tmux":
            raise AssertionError(f"dream runner spawned unexpected program {program!r}")
        kwargs["env"] = _env_without_tmux()
        return await asyncio.create_subprocess_exec(TMUX, "-L", self._socket_name, *args, **kwargs)


@pytest.fixture
def dream_tmux(private_tmux, monkeypatch):
    monkeypatch.setattr(tmux_dream_runner, "asyncio", _PrivateAsyncio(private_tmux.socket_name))
    return private_tmux


def _dream_runner(work_dir: Path, **overrides) -> TmuxDreamRunner:
    config = TmuxDreamConfig(
        working_dir=str(work_dir),
        timeout_s=overrides.pop("timeout_s", 30.0),
        poll_interval_s=0.1,
        ready_timeout_s=overrides.pop("ready_timeout_s", 10.0),
        submit_check_delay_s=0.1,
        **overrides,
    )
    runner = TmuxDreamRunner(config, agent_name="x")
    runner._seed_trust = lambda project_dir: False  # never touch a real config
    return runner


async def test_dream_run_leaves_prefix_neighbour_untouched(dream_tmux, tmp_path):
    """End to end: pre-spawn kill, spawn, remain-on-exit, readiness capture,
    instruction, submit check, result poll and teardown all stay on the exact
    dream session while a prefix neighbour is running."""
    dream_tmux.new("pinky-dream-x-old", "exec cat")
    neighbour_pane = dream_tmux.pane_id("pinky-dream-x-old")

    fake_claude = tmp_path / "fake-claude"
    fake_claude.write_text(
        "#!/bin/sh\n"
        "printf 'Claude fake REPL\\n'\n"
        "IFS= read -r line || exit 1\n"
        "path=$(printf '%s\\n' \"$line\""
        " | sed -n 's/.* plain text to \\(.*\\) using the Write tool.*/\\1/p')\n"
        f"remain=$('{TMUX}' show-options -wv remain-on-exit)\n"
        'printf \'report remain-on-exit=%s\\n\' "$remain" > "$path"\n'
        "exec sleep 60\n"
    )
    fake_claude.chmod(0o755)
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    runner = _dream_runner(work_dir, claude_binary=str(fake_claude))

    result = await runner.run("consolidate")

    assert result.ok, result.error
    assert result.output == "report remain-on-exit=on"
    assert dream_tmux.sessions() == ["pinky-dream-x-old"]
    assert dream_tmux.pane_id("pinky-dream-x-old") == neighbour_pane
    assert "Read the file" not in dream_tmux.capture("pinky-dream-x-old")
    assert dream_tmux.pane_format("pinky-dream-x-old", "#{remain-on-exit}") != "on"


def _kill_pane_process_keeping_pane(server: PrivateTmux, name: str) -> None:
    pane = server.pane_id(name)
    result = server.run("set-option", "-w", "-t", pane, "remain-on-exit", "on")
    assert result.returncode == 0, result.stderr
    result = server.run("respawn-pane", "-k", "-t", pane, "true")
    assert result.returncode == 0, result.stderr
    deadline = time.monotonic() + 10
    while server.pane_format(name, "#{pane_dead}") != "1":
        assert time.monotonic() < deadline, f"{name} pane never died"
        time.sleep(0.05)


async def test_dream_liveness_probe_ignores_prefix_neighbour(dream_tmux, tmp_path):
    runner = _dream_runner(tmp_path)
    dream_tmux.new("unrelated")
    without_session = await runner._repl_alive()

    # A neighbour whose process has exited must not answer for the absent
    # dream session: the probe gives the same answer as with no neighbour.
    dream_tmux.new("pinky-dream-x-old")
    _kill_pane_process_keeping_pane(dream_tmux, "pinky-dream-x-old")
    assert await runner._repl_alive() is without_session

    dream_tmux.new("pinky-dream-x")
    assert await runner._repl_alive() is True
    _kill_pane_process_keeping_pane(dream_tmux, "pinky-dream-x")
    assert await runner._repl_alive() is False


async def test_dream_readiness_wait_ignores_prefix_neighbour(dream_tmux, tmp_path):
    dream_tmux.new("pinky-dream-x-old", "printf 'Claude neighbour\\n'; exec cat")
    dream_tmux.wait_for_text("pinky-dream-x-old", "Claude neighbour")
    runner = _dream_runner(tmp_path, ready_timeout_s=1.5)

    assert await runner._wait_ready() is False

    dream_tmux.new("pinky-dream-x", "printf 'Claude exact\\n'; exec cat")
    dream_tmux.wait_for_text("pinky-dream-x", "Claude exact")
    assert await runner._wait_ready() is True


async def test_dream_submit_check_never_sends_enter_to_prefix_neighbour(dream_tmux, tmp_path):
    instruction = "Read the file /nowhere/prompt.md and follow it"
    dream_tmux.new("pinky-dream-x-old", "exec cat")
    dream_tmux.type_literal("pinky-dream-x-old", instruction)
    dream_tmux.wait_for_text("pinky-dream-x-old", instruction)
    runner = _dream_runner(tmp_path)

    await runner._ensure_submitted(instruction)
    await asyncio.sleep(0.5)
    # No Enter reached the neighbour: cat never echoed the line back.
    assert dream_tmux.capture("pinky-dream-x-old").count(instruction) == 1

    dream_tmux.new("pinky-dream-x", "exec cat")
    dream_tmux.type_literal("pinky-dream-x", instruction)
    dream_tmux.wait_for_text("pinky-dream-x", instruction)
    await runner._ensure_submitted(instruction)
    pane = dream_tmux.wait_for_text("pinky-dream-x", instruction + "\n" + instruction)
    assert pane.count(instruction) == 2
    assert dream_tmux.capture("pinky-dream-x-old").count(instruction) == 1
