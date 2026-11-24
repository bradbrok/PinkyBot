"""Relay bearer and private identity paths stay inside the daemon boundary."""

import json
from types import SimpleNamespace

import pytest

from pinky_daemon.claude_runner import ClaudeRunner, ClaudeRunnerConfig
from pinky_daemon.command_runner import ContainerCommandRunner, RunuserCommandRunner
from pinky_daemon.tmux_session import _TmuxControl
from tests import test_ferry_sdk_env as sdk_support
from tests.test_tmux_launch_env import claude_session
from tests.test_tmux_launch_env import private_home as private_home
from tests.tmux_env_support import LaunchRecorder, child_payload, probe_command, run_pane

NAMES = ("PINKYBOT_FERRY_RELAY_TOKEN", "PINKYBOT_FERRY_RELAY_KEY_FILE")
VALUES = ("synthetic-relay-private-token-0123456789", "/synthetic/private/identity-path-0123456789")


def seed(monkeypatch):
    for name, value in zip(NAMES, VALUES, strict=True):
        monkeypatch.setenv(name, value)


@pytest.mark.parametrize("kind", ["streaming", "runner"])
async def test_real_sdk_main_child_blanks_both_relay_values(tmp_path, monkeypatch, kind):
    seed(monkeypatch)
    if kind == "streaming":
        await sdk_support.test_sdk_blanks_ferry_secrets_but_inherits_legacy_session_secret(tmp_path, monkeypatch)
        report = tmp_path / "sdk-child-environment.json"
    else:
        await sdk_support.test_sdk_runner_blanks_ferry_secrets_in_real_transport(tmp_path, monkeypatch)
        report = tmp_path / "runner-child-environment.json"
    actual = json.loads(report.read_text())
    assert all(actual.get(name) == "" for name in NAMES)
    assert all(secret not in value for secret in VALUES for value in actual.values())


async def test_direct_cli_runner_drops_both_relay_values_before_exec(tmp_path, monkeypatch):
    seed(monkeypatch)
    captured = []

    async def communicate():
        return b"fixture response", b""

    async def exec_spy(*args, **kwargs):
        captured.append(kwargs["env"])
        return SimpleNamespace(returncode=0, communicate=communicate)

    monkeypatch.setattr("pinky_daemon.claude_runner.asyncio.create_subprocess_exec", exec_spy)
    result = await ClaudeRunner(ClaudeRunnerConfig(working_dir=str(tmp_path), claude_bin="unused-fixture")).run("fixture prompt")
    assert result.ok and len(captured) == 1
    assert not set(NAMES).intersection(captured[0])
    assert all(secret not in value for secret in VALUES for value in captured[0].values())


@pytest.mark.parametrize("name", NAMES)
def test_standalone_compacted_loader_refuses_explicit_relay_grant(tmp_path, name):
    from tests.tmux_env_r3_support import payload, run_loader

    path = payload(tmp_path, env={name: VALUES[NAMES.index(name)]})
    data = json.loads(path.read_text())
    data.update(inherit="none", granted=[name])
    path.write_text(json.dumps(data))
    result = run_loader(path)
    assert result.returncode == 1
    assert result.stdout == b"" and result.stderr.strip() == b"launch environment load failed"
    assert all(secret.encode() not in result.stdout + result.stderr for secret in VALUES)
    assert not path.exists()


@pytest.mark.parametrize("kind", ["container", "runuser"])
@pytest.mark.parametrize("isolated", [False, True])
async def test_wrapped_launch_payload_and_child_exclude_relay_values(private_home, monkeypatch, kind, isolated):
    seed(monkeypatch)
    session = claude_session(private_home, monkeypatch, isolated=isolated)
    recorder = LaunchRecorder(private_home)
    runner = ContainerCommandRunner("test-container", inner=recorder) if kind == "container" else RunuserCommandRunner("test-user", inner=recorder)
    if kind == "container":
        monkeypatch.setenv("PINKY_CONTAINER_RUNTIME", "podman")
        session._registry.get.return_value.isolation_mode = "container"
    env = session._build_repl_env()
    assert not set(NAMES).intersection(env)
    tmux = _TmuxControl("relay-wrapped-fixture", command_runner=runner)
    await tmux.new_session(cwd=str(private_home), command=probe_command(private_home), env=env)
    child = child_payload(run_pane(recorder.tmux_calls[-1], private_home))
    assert not set(NAMES).intersection(child["env"])
    assert all(secret not in json.dumps(child) for secret in VALUES)
    assert all(secret not in "\n".join(argv) for argv, _ in recorder.calls for secret in VALUES)
