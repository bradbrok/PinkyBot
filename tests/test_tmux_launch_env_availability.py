"""Ambient names and namespace startup latency must not prevent valid launches."""

import os
from unittest.mock import MagicMock, patch

import pytest

from pinky_daemon.codex_app_server_tmux import CodexAppServerSupervisor
from pinky_daemon.codex_tmux_session import CodexTmuxSession
from pinky_daemon.command_runner import ContainerCommandRunner, RunuserCommandRunner
from pinky_daemon.streaming_session import StreamingSessionConfig
from pinky_daemon.tmux_session import _TmuxControl
from tests.tmux_env_support import (
    LaunchRecorder,
    child_payload,
    probe_command,
    run_pane,
    secret_files,
)


@pytest.fixture
def home(tmp_path):
    directory = tmp_path / "home"
    directory.mkdir(mode=0o700)
    with patch.dict(os.environ, {"HOME": str(directory), "PATH": os.defpath}, clear=True):
        yield directory


@pytest.mark.parametrize("transport", ["repl", "app_server"])
@pytest.mark.parametrize("name", ["BASH_FUNC_x%%", "9INVALID", "NON_ASCII_é", "LONG" * 25 + "%"])
async def test_invalid_ambient_name_is_warned_and_valid_launch_proceeds(
    home, monkeypatch, capsys, transport, name
):
    monkeypatch.setenv(name, "synthetic-invalid-name-value")
    monkeypatch.setenv("UNKNOWN_CREDENTIAL", "synthetic-valid-name-value")
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
    assert name not in env
    captured = capsys.readouterr()
    logs = captured.out + captured.err
    warning = next(line for line in logs.splitlines() if "WARNING" in line)
    assert repr(name)[:64] in warning
    if len(repr(name)) > 64:
        assert name not in warning
    assert "synthetic-invalid-name-value" not in logs
    assert "synthetic-valid-name-value" not in logs
    recorder = LaunchRecorder(home)
    control = _TmuxControl("ambient-name-test", command_runner=recorder)
    await control.new_session(cwd=str(home), command=probe_command(home), env=env)
    staged_path = secret_files(home, "synthetic-valid-name-value")[0]
    child = child_payload(run_pane(recorder.tmux_calls[-1], home))
    assert child["env"]["UNKNOWN_CREDENTIAL"] == "synthetic-valid-name-value"
    assert name not in child["env"]
    assert str(staged_path) not in child["files_at_exec"]
    assert not staged_path.exists()
    assert not secret_files(home, "synthetic-valid-name-value")
    assert all(path.endswith(".lock") for path in child["files_at_exec"])


@pytest.mark.parametrize("namespace", ["container", "runuser"])
async def test_namespace_staging_uses_seed_startup_budget(home, namespace):
    class SlowStartRecorder(LaunchRecorder):
        staging_timeout = None

        async def run(self, argv, *, timeout=None, stdin_data=None):
            if stdin_data is not None:
                self.staging_timeout = timeout
                if timeout is not None and timeout < 10:
                    raise TimeoutError("synthetic namespace startup exceeds timeout")
            return await super().run(argv, timeout=timeout, stdin_data=stdin_data)

    recorder = SlowStartRecorder(home)
    if namespace == "container":
        runner = ContainerCommandRunner("test-container", inner=recorder)
    else:
        runner = RunuserCommandRunner("test-user", inner=recorder)
    control = _TmuxControl("startup-budget-test", command_runner=runner)
    await control.new_session(
        cwd=str(home), command=probe_command(home), env={"SECRET": "synthetic-budget-value"},
    )
    assert recorder.staging_timeout == 15
    paths = secret_files(home, "synthetic-budget-value")
    assert len(paths) == 1
    staged_path = paths[0]
    child = child_payload(run_pane(recorder.tmux_calls[-1], home))
    assert child["env"]["SECRET"] == "synthetic-budget-value"
    assert str(staged_path) not in child["files_at_exec"]
    assert not staged_path.exists()
    assert not secret_files(home, "synthetic-budget-value")
