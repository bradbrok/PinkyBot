"""Config directories survive explicit launch staging and isolated execution."""

from __future__ import annotations

import itertools
import shlex
import sys
from types import SimpleNamespace

import pytest

from pinky_daemon import provisioning, tmux_launch_env_loader
from pinky_daemon.command_runner import ContainerCommandRunner
from pinky_daemon.streaming_session import StreamingSessionConfig
from pinky_daemon.tmux_session import TmuxSession, _TmuxControl
from tests.tmux_env_support import LaunchRecorder, child_payload, run_pane

CASES = [
    pytest.param("container", mode, source, dedicated,
                 id=f"container-{mode}-{source}-dedicated-{dedicated}")
    for mode, source, dedicated in itertools.product(
        ("enforce", "off", "shadow"), ("session", "registry"), (False, True),
    )
] + [
    pytest.param(kind, mode, "session", kind == "dedicated", id=f"{kind}-{mode}")
    for kind, mode in itertools.product(("local", "dedicated", "gate-off"),
                                        ("enforce", "off", "shadow"))
]


@pytest.mark.parametrize("kind,mode,source,dedicated", CASES)
async def test_config_directory_reaches_exec_under_launch_policy(
    tmp_path, monkeypatch, kind, mode, source, dedicated,
):
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    session_dir = tmp_path / "session project"
    registry_dir = tmp_path / "registry project"
    session_dir.mkdir()
    registry_dir.mkdir()
    working_dir = str(session_dir) if source == "session" else ""
    resolved_dir = working_dir or str(registry_dir)
    container = kind == "container"
    agent = SimpleNamespace(
        name="unit", isolation_mode="container" if kind in ("container", "gate-off") else "local",
        isolated=True, working_dir=resolved_dir if container else str(registry_dir),
        dedicated_config_dir=dedicated,
        tool_policy_enabled=False,
    )
    registry = SimpleNamespace(
        get=lambda name: agent, get_signing_key=lambda name: "synthetic-scoped-key",
    )
    grants = tmp_path / "grants.json"
    grants.write_text("{}")
    grants.chmod(0o600)
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
    runner = ContainerCommandRunner("synthetic-container", inner=inner, workdir=resolved_dir) if container else inner
    control = _TmuxControl("config-test", command_runner=runner)
    session = TmuxSession(
        StreamingSessionConfig(agent_name="unit", working_dir=working_dir), tmux_control=control,
    )
    session._registry = registry
    policy = session._launch_env_policy()
    env = session._build_repl_env(launch_policy=policy, report_shadow=False)
    assert policy.clean is (mode == "enforce")
    assert "CLAUDE_CONFIG_DIR" not in tmux_launch_env_loader.BASE_ALLOWLIST
    ambient_config = str(home / "ambient-config")
    if container:
        expected = provisioning.container_config_dir(resolved_dir)
        inherited_config = ambient_config if policy.clean else expected
    elif kind == "dedicated":
        expected = provisioning.local_config_dir(working_dir)
        inherited_config = ambient_config
    else:
        expected = None if policy.clean else ambient_config
        inherited_config = ambient_config
        assert "CLAUDE_CONFIG_DIR" not in env
    code = (
        "import json,os; print(json.dumps({"
        "'config':os.environ.get('CLAUDE_CONFIG_DIR'),"
        "'home':os.environ.get('HOME'),"
        "'empty_token':os.environ.get('CLAUDE_CODE_OAUTH_TOKEN') == '',"
        "'names':sorted(os.environ)}))"
    )
    await control.new_session(
        cwd=resolved_dir, command=shlex.join([sys.executable, "-c", code]),
        env=env, **policy.spawn_options(env),
    )
    child = child_payload(run_pane(inner.tmux_calls[-1], home, inherited={
        "CLAUDE_CONFIG_DIR": inherited_config,
        "CLAUDE_CODE_OAUTH_TOKEN": "synthetic-inherited-token",
        "UNGRANTED_CONTAINER_VALUE": "synthetic",
    }))
    assert child["config"] == expected
    assert child["home"] == str(home)
    if container and policy.clean:
        assert env["CLAUDE_CONFIG_DIR"] == expected
    if kind == "dedicated":
        assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "" and child["empty_token"]
    elif container:
        assert not child["empty_token"]
    if policy.clean:
        assert "UNGRANTED_CONTAINER_VALUE" not in child["names"]
    assert not list(home.rglob("env-*.json"))
