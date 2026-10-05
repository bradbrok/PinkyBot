"""Production constructor and exec-boundary contracts for dedicated tmux."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from pinky_daemon import tmux_session
from pinky_daemon.command_runner import (
    CommandResult,
    ContainerCommandRunner,
    LocalCommandRunner,
    RunuserCommandRunner,
)
from pinky_daemon.isolated_launch_env import LaunchConfigError
from tests.tmux_server_env_support import FAMILIES, allowed, control, no_values, owner, seed


@pytest.fixture
def daemon(tmp_path, monkeypatch):
    seed(monkeypatch, tmp_path)
    return tmp_path


@pytest.mark.parametrize("kind", FAMILIES)
@pytest.mark.parametrize("label", [None, "test-fleet", "", "-fleet", "x" * 64])
def test_production_constructors_pin_route_and_ignore_config(daemon, monkeypatch, kind, label):
    if label is not None:
        monkeypatch.setenv("PINKY_TMUX_SOCKET", label)
    cmd = control(owner(kind, daemon))._base_cmd()
    expected = "pinkybot" if label is None else label or "default"
    assert "-L" in cmd, "production control has no explicitly pinned label"
    assert cmd[cmd.index("-L") + 1] == expected
    assert "-f" in cmd and cmd[cmd.index("-f") + 1] == "/dev/null"


@pytest.mark.parametrize("kind", FAMILIES)
@pytest.mark.parametrize("label", [".", "..", "default", "a/b", "bad label", "x" * 65, "bad\nlabel", "a;true"])
def test_invalid_socket_configuration_refuses_without_fallback(daemon, monkeypatch, kind, label):
    monkeypatch.setenv("PINKY_TMUX_SOCKET", label)
    with pytest.raises(LaunchConfigError):
        control(owner(kind, daemon))._base_cmd()


@pytest.mark.parametrize("kind", FAMILIES)
async def test_local_tmux_client_gets_constructed_env_only(daemon, monkeypatch, kind):
    monkeypatch.setenv("TMUX_TMPDIR", str(daemon))
    seen = []

    async def record(self, argv, **kwargs):
        seen.append((argv, kwargs))
        return CommandResult(0, b"", b"")

    monkeypatch.setattr(LocalCommandRunner, "run", record)
    await control(owner(kind, daemon))._run("has-session", "-t", "=test-agent")
    argv, kwargs = seen[-1]
    env = kwargs.get("env")
    assert isinstance(env, dict), "local tmux client inherits ambient daemon environment"
    assert allowed(set(env)), "client contains forbidden names"
    assert env["TMUX_TMPDIR"] == str(daemon)
    assert env["HOME"] == str(daemon / "home")
    assert "PWD" not in env and "TMUX" not in env
    no_values(argv)


async def test_local_runner_forwards_explicit_env_and_stdin(monkeypatch):
    seen = {}
    proc = SimpleNamespace(returncode=0, communicate=AsyncMock(return_value=(b"out", b"err")))

    async def create(*args, **kwargs):
        seen.update(kwargs)
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    env = {"PATH": "/usr/bin:/bin"}
    result = await LocalCommandRunner().run(["tmux", "-V"], env=env, stdin_data=b"synthetic", timeout=1)
    assert seen["env"] == env
    proc.communicate.assert_awaited_once_with(input=b"synthetic")
    assert result.stdout == b"out" and result.stderr == b"err"


@pytest.mark.parametrize("kind", ["runuser", "container", "local_subclass"])
async def test_wrapped_runner_never_receives_host_env(daemon, monkeypatch, kind):
    seen = []

    class Recorder:
        async def run(self, argv, **kwargs):
            seen.append((argv, kwargs))
            return CommandResult(0, b"", b"")

    class LocalSubclass(LocalCommandRunner):
        async def run(self, argv, **kwargs):
            return await Recorder().run(argv, **kwargs)

    runner = (RunuserCommandRunner("test-user", inner=Recorder()) if kind == "runuser" else
              ContainerCommandRunner("test-container", inner=Recorder()) if kind == "container" else LocalSubclass())
    ctrl = control(owner("claude", daemon))
    ctrl.set_command_runner(runner)
    await ctrl._run("has-session", stdin_data=b"synthetic")
    assert "env" not in seen[-1][1]
    assert seen[-1][1]["stdin_data"] == b"synthetic"


async def test_dream_raw_exec_keeps_combined_output_and_adds_clean_env(daemon, monkeypatch):
    seen = {}
    proc = SimpleNamespace(returncode=7, communicate=AsyncMock(return_value=(b"out-err-out", None)))

    async def create(*args, **kwargs):
        seen.update(kwargs)
        seen["argv"] = args
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    dream = owner("dream", daemon)
    monkeypatch.setattr(dream._control, "_run", AsyncMock(side_effect=AssertionError("delegated raw dream call")))
    assert await dream._tmux("capture-pane", "-p") == (7, "out-err-out")
    assert seen["stderr"] == asyncio.subprocess.STDOUT
    assert isinstance(seen.get("env"), dict), "raw dream subprocess inherits daemon environment"
    assert allowed(set(seen["env"]))
    no_values(seen["argv"])


async def test_dream_timeout_still_kills_waits_and_returns_124(daemon, monkeypatch):
    proc = SimpleNamespace(communicate=AsyncMock(side_effect=asyncio.TimeoutError), kill=lambda: None, wait=AsyncMock())
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=proc))
    result = await owner("dream", daemon)._tmux("capture-pane", timeout=0.01)
    assert result[0] == 124
    proc.wait.assert_awaited_once()


def test_explicit_injected_control_keeps_recorded_socket(daemon, monkeypatch):
    from pinky_daemon.codex_tmux_session import CodexTmuxSession
    from pinky_daemon.streaming_session import StreamingSessionConfig
    monkeypatch.setenv("PINKY_TMUX_SOCKET", "new-fleet")
    explicit = tmux_session._TmuxControl("test-agent", socket_path="/synthetic/prior.sock")
    session = CodexTmuxSession(StreamingSessionConfig(agent_name="test-agent", working_dir=str(daemon)), tmux_control=explicit)
    assert session._tmux is explicit
    assert explicit._base_cmd()[1:3] == ["-S", "/synthetic/prior.sock"]


@pytest.mark.parametrize("kind", FAMILIES)
def test_control_pins_socket_root_across_ambient_changes(daemon, monkeypatch, kind):
    first = daemon / "first"
    monkeypatch.setenv("TMUX_TMPDIR", str(first))
    monkeypatch.setenv("PINKY_TMUX_SOCKET", "test-first")
    ctrl = control(owner(kind, daemon))
    monkeypatch.setenv("TMUX_TMPDIR", str(daemon / "second"))
    monkeypatch.setenv("PINKY_TMUX_SOCKET", "test-second")
    monkeypatch.setenv("TMUX", "/synthetic/wrong,1,1")
    path = ctrl._local_socket_path()
    assert path is not None and path.parent.parent == first and path.name == "test-first"


@pytest.mark.parametrize("configured", ["test-current", "bad/config"])
async def test_recorded_cleanup_route_remains_authoritative(daemon, monkeypatch, configured):
    monkeypatch.setenv("PINKY_TMUX_SOCKET", configured)
    monkeypatch.setenv("TMUX_TMPDIR", str(daemon / "new-root"))
    original = str(daemon / "recorded.sock")
    debt = tmux_session._TmuxSpawnCleanupDebt(agent_name="test-agent", session_name="pinky-test-agent",
        socket_name="test-prior", socket_path=original, tmux_binary="tmux", runner={"kind": "local"},
        site="synthetic-test", created_at=1)
    path = tmux_session._persist_tmux_spawn_cleanup_debt(daemon, debt)
    seen = []

    async def strict(ctrl, **kwargs):
        seen.append(ctrl._base_cmd())
        return None

    monkeypatch.setattr(tmux_session, "_strict_owned_tmux_cleanup", strict)
    assert await tmux_session.reconcile_tmux_spawn_cleanup_debts(daemon) == (1, 0)
    assert seen[0][1:3] == ["-S", original]
    assert not path.exists()
