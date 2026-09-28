"""Reserved shell bookkeeping names must fail closed before staging."""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

from pinky_daemon.codex_app_server_tmux import CodexAppServerSupervisor
from pinky_daemon.codex_tmux_session import CodexTmuxSession
from pinky_daemon.streaming_session import StreamingSessionConfig
from pinky_daemon.tmux_session import _TmuxControl
from tests.tmux_env_support import LaunchRecorder

SECRET = "synthetic-reserved-collision-secret"
RESERVED = "__PINKY_LAUNCH_ANYTHING"


@pytest.fixture
def home(tmp_path):
    directory = tmp_path / "home"
    directory.mkdir(mode=0o700)
    with patch.dict(os.environ, {"HOME": str(directory), "PATH": os.defpath}, clear=True):
        yield directory


async def assert_refused(home, env):
    recorder = LaunchRecorder(home)
    tmux = _TmuxControl("reserved-name-test", command_runner=recorder)
    with pytest.raises((ValueError, RuntimeError)) as error:
        await tmux.new_session(cwd=str(home), command="true", env=env)
    assert SECRET not in str(error.value)
    assert not recorder.calls, "reserved-name validation precedes staging and launch"
    assert not list(home.iterdir()), "reserved input must leave no partial filesystem state"


async def test_reserved_prefix_refused_before_side_effect(home):
    await assert_refused(home, {"UNKNOWN_CREDENTIAL": SECRET, RESERVED: "x"})


@pytest.mark.parametrize("transport", ["repl", "app_server"])
async def test_codex_builder_keeps_collision_for_loud_boundary_refusal(home, monkeypatch, transport):
    monkeypatch.setenv("UNKNOWN_CREDENTIAL", SECRET)
    monkeypatch.setenv(RESERVED, "x")
    if transport == "repl":
        session = CodexTmuxSession(
            StreamingSessionConfig(agent_name="test-agent", working_dir=str(home)),
            tmux_control=MagicMock(),
        )
        env = session._build_repl_env()
    else:
        with patch.object(CodexAppServerSupervisor, "_resolve_sock_dir", return_value=(str(home), False)):
            supervisor = CodexAppServerSupervisor("test-agent", working_dir=str(home))
        env = supervisor._build_env()
    assert env[RESERVED] == "x", "the builder must not silently discard a collision"
    assert env["UNKNOWN_CREDENTIAL"] == SECRET
    await assert_refused(home, env)
