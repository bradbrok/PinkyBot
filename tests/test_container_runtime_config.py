"""Launch payloads and provisioning select one canonical container config directory."""

from __future__ import annotations

import hashlib
import itertools
import json
import shlex
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from pinky_daemon import provisioning
from pinky_daemon.command_runner import ContainerCommandRunner
from pinky_daemon.streaming_session import StreamingSessionConfig
from pinky_daemon.tmux_session import TmuxSession, _TmuxControl
from tests.tmux_env_support import LaunchRecorder, child_payload, run_pane

BASE_DIGESTS = json.loads(
    (Path(__file__).parent / "fixtures" / "local_launch_env_digests.json").read_text()
)
CONTROL_CASES = list(itertools.product(
    ("enforce", "off", "shadow"), ("local", "dedicated", "gate-off", "unknown"),
    (False, True),
))


def _launch(tmp_path, monkeypatch, *, session_dir, registry_dir, mode="enforce",
            kind="container", custom=False):
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    agent = SimpleNamespace(
        name="unit", isolation_mode="container" if kind in ("container", "gate-off") else "local",
        isolated=True, working_dir=registry_dir, dedicated_config_dir=kind == "dedicated",
        tool_policy_enabled=False,
    )

    def get_agent(name):
        if kind == "unknown":
            raise RuntimeError("synthetic registry unavailable")
        return agent

    registry = SimpleNamespace(get=get_agent, get_signing_key=lambda name: "synthetic-scoped-key")
    grants = tmp_path / "grants.json"
    grants.write_text("{}")
    grants.chmod(0o600)
    for name in ("PINKY_SESSION_SECRET", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
                 "ANTHROPIC_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PINKY_ISOLATED_ENV", mode)
    monkeypatch.setenv("PINKY_ISOLATED_ENV_GRANTS_FILE", str(grants))
    monkeypatch.setenv("PINKY_FORWARD_OAUTH_TOKEN", "1")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "synthetic-shared-token")
    monkeypatch.setenv("PINKY_TOOL_POLICY", "off")
    if kind == "gate-off":
        monkeypatch.delenv("PINKY_CONTAINER_RUNTIME", raising=False)
    else:
        monkeypatch.setenv("PINKY_CONTAINER_RUNTIME", "podman")
    inner = LaunchRecorder(home)
    runner = ContainerCommandRunner("synthetic-container", inner=inner) if kind == "container" else inner
    control = _TmuxControl("config-path-test", command_runner=runner)
    session = TmuxSession(
        StreamingSessionConfig(agent_name="unit", working_dir=session_dir, thinking_effort="medium"),
        tmux_control=control,
    )
    session._registry = registry
    if custom:
        session._config.provider_url = "https://provider.example.invalid"
        session._config.provider_key = "synthetic-provider-key"
    return SimpleNamespace(session=session, agent=agent, inner=inner, control=control, home=home)


def _build(fixture):
    return fixture.session._build_repl_env(report_shadow=False)


def _serialized_env(env, tmp_path):
    normalized = {key: value.replace(str(tmp_path), "<test-root>") for key, value in env.items()}
    return json.dumps(normalized, separators=(",", ":"), ensure_ascii=False).encode()


def _assert_fallback_log(capsys):
    output = capsys.readouterr()
    logs = output.out + output.err
    reason = "working_dir not absolute; using container home config dir"
    assert logs.count(reason) == 1
    line = next(line for line in logs.splitlines() if reason in line)
    assert "unit" in line and "WARNING" in line and "/home/agent/.claude" in line
    assert not any(value in logs for value in (
        "synthetic-shared-token", "synthetic-scoped-key", "synthetic-provider-key",
    )), "Launch diagnostics exposed a synthetic credential value"


async def _execute(fixture, env, inherited_config):
    policy = fixture.session._launch_env_policy()
    code = "import json,os; print(json.dumps({'config':os.environ.get('CLAUDE_CONFIG_DIR')}))"
    await fixture.control.new_session(
        cwd=str(fixture.home), command=shlex.join([sys.executable, "-c", code]),
        env=env, **policy.spawn_options(env),
    )
    raw = run_pane(fixture.inner.tmux_calls[-1], fixture.home, inherited={
        "CLAUDE_CONFIG_DIR": inherited_config,
    })
    assert not any(value.encode() in raw.stdout + raw.stderr for value in (
        "synthetic-shared-token", "synthetic-scoped-key", "synthetic-provider-key",
    )), "Executed probe exposed a synthetic credential value"
    assert not list(fixture.home.rglob("env-*.json"))
    return child_payload(raw)["config"]


@pytest.mark.parametrize("spelling", ["empty", "none", "whitespace", "missing-attribute"])
def test_empty_container_workdir_sets_home_config_and_one_warning(
    tmp_path, monkeypatch, capsys, spelling,
):
    working_dir = {"empty": "", "none": None, "whitespace": " \t", "missing-attribute": ""}[spelling]
    f = _launch(tmp_path, monkeypatch, session_dir=working_dir, registry_dir=working_dir)
    if spelling == "missing-attribute":
        del f.agent.working_dir
    env = _build(f)
    assert env.get("CLAUDE_CONFIG_DIR") == "/home/agent/.claude"
    _assert_fallback_log(capsys)


@pytest.mark.parametrize("mode", ["enforce", "off", "shadow"])
async def test_dot_session_fallback_exec_preserves_provisioned_home_config(
    tmp_path, monkeypatch, capsys, mode,
):
    f = _launch(tmp_path, monkeypatch, session_dir=".", registry_dir="", mode=mode)
    expected = provisioning.ContainerProvisioner().runtime_env(f.agent)["CLAUDE_CONFIG_DIR"]
    env = _build(f)
    assert await _execute(f, env, expected) == expected
    _assert_fallback_log(capsys)


async def test_registry_symlink_exec_uses_the_provisioned_resolved_target(tmp_path, monkeypatch):
    target = tmp_path / "canonical project"
    target.mkdir()
    alias = tmp_path / "registry alias"
    alias.symlink_to(target, target_is_directory=True)
    f = _launch(tmp_path, monkeypatch, session_dir="", registry_dir=str(alias))
    expected = str(target.resolve() / ".claude-container")
    assert await _execute(f, _build(f), expected) == expected


async def test_relative_registry_uses_home_volume_instead_of_session_path(
    tmp_path, monkeypatch, capsys,
):
    f = _launch(tmp_path, monkeypatch, session_dir=str(tmp_path), registry_dir="relative project")
    assert await _execute(f, _build(f), "/home/agent/.claude") == "/home/agent/.claude"
    _assert_fallback_log(capsys)


@pytest.mark.parametrize("case", [
    "empty", "none", "whitespace", "missing-attribute", "dot", "relative",
    "symlink", "absolute", "different-session",
])
def test_launch_create_and_runtime_config_directory_parity(tmp_path, monkeypatch, case):
    target = tmp_path / "canonical project"
    target.mkdir()
    alias = tmp_path / "registry alias"
    alias.symlink_to(target, target_is_directory=True)
    working_dir = {
        "empty": "", "none": None, "whitespace": " \t", "missing-attribute": "",
        "dot": ".", "relative": "relative project", "symlink": str(alias),
        "absolute": str(target), "different-session": str(target),
    }[case]
    session_dir = str(tmp_path / "unprovisioned session") if case == "different-session" else working_dir
    f = _launch(tmp_path, monkeypatch, session_dir=session_dir, registry_dir=working_dir)
    if case == "missing-attribute":
        del f.agent.working_dir
    expected = str(target.resolve() / ".claude-container") if case in (
        "symlink", "absolute", "different-session",
    ) else "/home/agent/.claude"
    env = _build(f)
    assert env.get("CLAUDE_CONFIG_DIR") == expected
    provisioner = provisioning.ContainerProvisioner()
    names = provisioner.names(f.agent)
    assert env["CLAUDE_CONFIG_DIR"] == provisioner._config_dir_for(f.agent, names)
    assert env["CLAUDE_CONFIG_DIR"] == provisioner.runtime_env(f.agent)["CLAUDE_CONFIG_DIR"]
    argv = provisioner._create_argv(f.agent, names, "synthetic-image")
    assert f"CLAUDE_CONFIG_DIR={expected}" in argv
    if case in ("symlink", "absolute", "different-session"):
        assert f"{target.resolve()}:{target.resolve()}" in argv
    else:
        assert not any(":relative project" in value for value in argv)


def test_launch_and_provisioning_call_one_helper_with_registry_inputs(tmp_path, monkeypatch):
    shared = getattr(provisioning, "container_runtime_config_dir", None)
    assert callable(shared), "A shared container config-directory resolver is required"
    f = _launch(tmp_path, monkeypatch, session_dir=str(tmp_path / "other"), registry_dir=str(tmp_path))
    provisioner = provisioning.ContainerProvisioner()
    names = provisioner.names(f.agent)
    calls = []

    def observe(working_dir, names_config_dir):
        calls.append((working_dir, names_config_dir))
        return shared(working_dir, names_config_dir)

    monkeypatch.setattr(provisioning, "container_runtime_config_dir", observe)
    provisioner.runtime_env(f.agent)
    _build(f)
    assert calls == [(f.agent.working_dir, names.config_dir)] * 2


@pytest.mark.parametrize("mode,kind,custom", CONTROL_CASES)
def test_noncontainer_serialized_environment_matches_established_contract(
    tmp_path, monkeypatch, mode, kind, custom,
):
    f = _launch(tmp_path, monkeypatch, session_dir=str(tmp_path), registry_dir=str(tmp_path),
                mode=mode, kind=kind, custom=custom)
    actual = _serialized_env(_build(f), tmp_path)
    expected = BASE_DIGESTS[f"{mode}/{kind}/{custom}"]
    assert hashlib.sha256(actual).hexdigest() == expected["sha256"]
    assert len(actual) == expected["bytes"]
