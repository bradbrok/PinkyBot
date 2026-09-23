"""Isolated launch policy, grants and authentication contracts."""

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

from pinky_daemon import isolated_launch_env, tmux_launch_env_loader, tmux_session
from pinky_daemon.command_runner import RunuserCommandRunner
from pinky_daemon.tmux_session import _TmuxControl
from tests.test_isolated_launch_env_shadow import builder
from tests.tmux_env_r3_support import NONCE
from tests.tmux_env_support import LaunchRecorder
from tests.tmux_isolated_env_support import DAEMON_NAMES, Registry, launch_probe


@pytest.fixture
def grants(tmp_path, monkeypatch):
    path = tmp_path / "grants.json"
    path.write_text("{}")
    path.chmod(0o600)
    monkeypatch.setenv("PINKY_ISOLATED_ENV", "enforce")
    monkeypatch.setenv("PINKY_ISOLATED_ENV_GRANTS_FILE", str(path))
    return path


@pytest.mark.parametrize("kind", ["claude", "codex", "app_server"])
@pytest.mark.parametrize("mode", ["off", "shadow"])
async def test_observation_modes_preserve_real_inheritance(tmp_path, monkeypatch, kind, mode):
    async with launch_probe(tmp_path, monkeypatch, mode=mode) as probe:
        monkeypatch.setenv("PINKY_ISOLATED_ENV_GRANTS_FILE", "/does/not/exist")
        names = await probe.launch(kind)
        assert DAEMON_NAMES <= names


@pytest.mark.parametrize("kind", ["claude", "codex", "app_server"])
async def test_granted_name_present_and_ungranted_name_removed(tmp_path, monkeypatch, kind):
    async with launch_probe(tmp_path, monkeypatch, mode="enforce") as probe:
        Path(os.environ["PINKY_ISOLATED_ENV_GRANTS_FILE"]).write_text(
            json.dumps({"test-tenant": ["HRPOS_PASSWORD"]}),
        )
        names = await probe.launch(kind)
        assert "HRPOS_PASSWORD" in names and "PINKY_AGENT_KEY" in names
        assert not {"PINKY_SESSION_SECRET", "PINKYBOT_FERRY_SHARED_SECRET"} & names


@pytest.mark.parametrize("kind", ["claude", "codex", "app_server"])
async def test_grants_reload_at_next_real_spawn(tmp_path, monkeypatch, kind):
    async with launch_probe(tmp_path, monkeypatch, mode="enforce") as probe:
        path = Path(os.environ["PINKY_ISOLATED_ENV_GRANTS_FILE"])
        assert "HRPOS_PASSWORD" not in await probe.launch(kind)
        path.write_text('{"test-tenant":["HRPOS_PASSWORD"]}')
        assert "HRPOS_PASSWORD" in await probe.launch(kind)
        path.write_text("{}")
        assert "HRPOS_PASSWORD" not in await probe.launch(kind)


@pytest.mark.parametrize("kind", ["claude", "codex", "app_server"])
async def test_one_policy_snapshot_per_real_spawn(tmp_path, monkeypatch, kind):
    async with launch_probe(tmp_path, monkeypatch, mode="enforce") as probe:
        capture = Mock(wraps=isolated_launch_env.capture_policy)
        monkeypatch.setattr(isolated_launch_env, "capture_policy", capture)
        real_spawn = probe.control.new_session

        async def change_mode_after_policy(**kwargs):
            monkeypatch.setenv("PINKY_ISOLATED_ENV", "off")
            Path(os.environ["PINKY_ISOLATED_ENV_GRANTS_FILE"]).write_text("{")
            return await real_spawn(**kwargs)

        monkeypatch.setattr(probe.control, "new_session", change_mode_after_policy)
        assert not DAEMON_NAMES & await probe.launch(kind)
        assert capture.call_count == 1


@pytest.mark.parametrize("kind", ["claude", "codex", "app_server"])
@pytest.mark.parametrize("bad", [
    "missing", "malformed", "wildcard", "daemon_only", "not_mapping", "not_list",
    "duplicate_agent", "shell_name", "reserved_name", "symlink", "public", "fifo",
    "wildcard_agent", "duplicate_name", "ferry_key", "deep", "oversized", "unset",
])
def test_bad_grants_refuse_isolated_payload_loudly(grants, tmp_path, monkeypatch, kind, bad):
    data = {
        "malformed": "{", "wildcard": '{"test-tenant":["HRPOS_*"]}',
        "daemon_only": '{"test-tenant":["PINKY_SESSION_SECRET"]}',
        "not_mapping": "[]", "not_list": '{"test-tenant":"HRPOS_PASSWORD"}',
        "duplicate_agent": '{"test-tenant":[],"test-tenant":[]}',
        "shell_name": '{"test-tenant":["BASH_ENV"]}',
        "reserved_name": '{"test-tenant":["__PINKY_LAUNCH_X"]}',
        "wildcard_agent": '{"*":["HRPOS_PASSWORD"]}',
        "duplicate_name": '{"test-tenant":["HRPOS_PASSWORD","HRPOS_PASSWORD"]}',
        "ferry_key": '{"test-tenant":["PINKYBOT_FERRY_SHARED_SECRET"]}',
        "deep": "[" * 2000 + "0" + "]" * 2000,
        "oversized": " " * 65537,
    }
    if bad in data:
        grants.write_text(data[bad])
    elif bad == "missing":
        grants.unlink()
    elif bad == "unset":
        monkeypatch.delenv("PINKY_ISOLATED_ENV_GRANTS_FILE")
    elif bad == "public":
        grants.chmod(0o644)
    elif bad == "symlink":
        target = tmp_path / "target.json"
        grants.rename(target)
        grants.symlink_to(target)
    elif bad == "fifo":
        grants.unlink()
        os.mkfifo(grants, 0o600)
    build, logs = builder(kind, Registry(), tmp_path, monkeypatch)
    with pytest.raises((ValueError, PermissionError), match="grant"):
        build()
    assert sum("ERROR" in message and "grant" in message for message in logs) == 1


@pytest.mark.parametrize("kind", ["claude", "codex", "app_server"])
def test_grants_file_edit_takes_effect_next_payload(grants, tmp_path, monkeypatch, kind):
    monkeypatch.setenv("EXACT_GRANT", "synthetic-value")
    build, _ = builder(kind, Registry(), tmp_path, monkeypatch)
    assert "EXACT_GRANT" not in build()
    grants.write_text('{"test-tenant":["EXACT_GRANT"]}')
    assert build()["EXACT_GRANT"] == "synthetic-value"
    grants.write_text("{}")
    assert "EXACT_GRANT" not in build()


@pytest.mark.parametrize("kind", ["claude", "codex", "app_server"])
def test_grants_do_not_replace_explicit_identity(grants, tmp_path, monkeypatch, kind):
    monkeypatch.setenv("PINKY_AGENT_KEY", "synthetic-wrong-agent-key")
    monkeypatch.setenv("PINKY_AGENT_NAME", "another-tenant")
    monkeypatch.setenv("EMPTY_GRANT", "")
    monkeypatch.setenv("ZOHO_API_SECRET", "synthetic-zoho")
    grants.write_text('{"test-tenant":["PINKY_AGENT_KEY","PINKY_AGENT_NAME","EMPTY_GRANT","ZOHO_API_SECRET"]}')
    build, _ = builder(kind, Registry(), tmp_path, monkeypatch)
    env = build()
    assert env["PINKY_AGENT_KEY"] == "synthetic-agent-key"
    assert env["PINKY_AGENT_NAME"] == "test-tenant"
    assert env["EMPTY_GRANT"] == ""
    assert env["ZOHO_API_SECRET"] == "synthetic-zoho"


@pytest.mark.parametrize("kind", ["claude", "codex", "app_server"])
def test_unknown_grant_agent_ignored_with_log(grants, tmp_path, monkeypatch, kind):
    registry = Registry()
    real_get = registry.get
    monkeypatch.setattr(registry, "get", lambda name: real_get(name) if name == "test-tenant" else None)
    grants.write_text('{"not-registered":["HRPOS_PASSWORD"]}')
    build, logs = builder(kind, registry, tmp_path, monkeypatch)
    assert "HRPOS_PASSWORD" not in build()
    assert any("not-registered" in message and "ignor" in message.lower() for message in logs)


@pytest.mark.parametrize("kind", ["claude", "codex", "app_server"])
def test_nonisolated_unaffected_by_bad_mode_or_grants(grants, tmp_path, monkeypatch, kind):
    grants.write_text("{")
    monkeypatch.setenv("PINKY_ISOLATED_ENV", "invalid-mode")
    monkeypatch.setenv("PINKY_SESSION_SECRET", "synthetic-global")
    build, _ = builder(kind, Registry("not_isolated"), tmp_path, monkeypatch)
    assert build()["PINKY_SESSION_SECRET"] == "synthetic-global"


@pytest.mark.parametrize("kind", ["claude", "codex", "app_server"])
async def test_unknown_mode_refuses_once_per_spawn(tmp_path, monkeypatch, kind):
    async with launch_probe(tmp_path, monkeypatch, mode="invalid-mode") as probe:
        with pytest.raises(ValueError, match="mode"):
            await probe.launch(kind)
        assert not probe.names_path.exists()
        assert sum("ERROR" in line and "mode" in line for line in probe.logs) == 1
        assert "invalid-mode" not in "\n".join(probe.logs)


async def test_claude_shared_auth_token_explicit_with_forwarding_off(tmp_path, monkeypatch):
    async with launch_probe(tmp_path, monkeypatch, mode="enforce") as probe:
        monkeypatch.setenv("PINKY_FORWARD_OAUTH_TOKEN", "0")
        names = await probe.launch(provider_key="", empty_token=False)
        assert "CLAUDE_CODE_OAUTH_TOKEN" in names and "PINKY_AGENT_KEY" in names
        assert "CLAUDE_CODE_OAUTH_TOKEN" not in json.loads(probe.empty_names_path.read_text())
        assert not DAEMON_NAMES & names


async def test_custom_provider_does_not_receive_shared_oauth(tmp_path, monkeypatch):
    async with launch_probe(tmp_path, monkeypatch, mode="enforce") as probe:
        names = await probe.launch(provider_url="https://provider.invalid", empty_token=False)
        assert "CLAUDE_CODE_OAUTH_TOKEN" not in names
        assert "ANTHROPIC_API_KEY" in names


async def test_dedicated_auth_keeps_empty_override(tmp_path, monkeypatch):
    async with launch_probe(tmp_path, monkeypatch, mode="enforce") as probe:
        registry = Registry()
        get = registry.get

        def dedicated(name):
            agent = get(name)
            agent.dedicated_config_dir = True
            return agent

        monkeypatch.setattr(registry, "get", dedicated)
        names = await probe.launch(registry=registry, provider_key="", empty_token=False)
        assert "CLAUDE_CONFIG_DIR" in names
        assert "CLAUDE_CODE_OAUTH_TOKEN" in json.loads(probe.empty_names_path.read_text())


@pytest.mark.parametrize("kind", ["claude", "codex", "app_server"])
def test_isolated_payload_is_scoped_not_ambient(grants, tmp_path, monkeypatch, kind):
    monkeypatch.setenv("UNGRANTED_KNOB", "synthetic")
    monkeypatch.setenv("HRPOS_PASSWORD", "synthetic")
    build, _ = builder(kind, Registry(), tmp_path, monkeypatch)
    env = build()
    assert "PINKY_AGENT_KEY" in env
    assert not (DAEMON_NAMES | {"UNGRANTED_KNOB"}) & env.keys()


@pytest.mark.parametrize("remote", [False, True])
@pytest.mark.parametrize("env", [{}, {"INTENTIONALLY_EMPTY": ""}])
async def test_clean_policy_stages_even_without_populated_values(tmp_path, monkeypatch, remote, env):
    monkeypatch.setenv("HOME", str(tmp_path))
    recorder = LaunchRecorder(tmp_path)
    runner = RunuserCommandRunner("test", inner=recorder) if remote else recorder
    control = _TmuxControl("policy-test", command_runner=runner)
    result = await control.new_session(cwd=str(tmp_path), command="true", env=env, inherit="none")
    assert result.launch_env is not None
    paths = list(tmp_path.rglob("env-*.json"))
    assert len(paths) == 1
    data = json.loads(paths[0].read_text())
    assert data["inherit"] == "none" and data["env"] == env
    assert str(paths[0]) in recorder.tmux_calls[-1][-1]


@pytest.mark.parametrize("key", ["PINKY_SESSION_SECRET", "PINKYBOT_FERRY_SHARED_SECRET"])
async def test_direct_boundary_daemon_only_refusal(tmp_path, monkeypatch, key):
    recorder = LaunchRecorder(tmp_path)
    logs = []
    monkeypatch.setattr(tmux_session, "_log", logs.append)
    control = _TmuxControl("policy-test", command_runner=recorder)
    with pytest.raises(PermissionError, match="daemon-only"):
        await control.new_session(cwd=str(tmp_path), command="true", env={key: "synthetic"}, inherit="none")
    assert not recorder.calls
    assert any("ERROR" in line and key in line for line in logs)
    assert "synthetic" not in "\n".join(logs)


def loader_probe(tmp_path, data):
    path = tmp_path / f"env-{NONCE}.json"
    path.write_text(json.dumps({"nonce": NONCE, **data}))
    path.chmod(0o600)
    source = Path(tmux_launch_env_loader.__file__).read_text()
    source = (
        "import os,json,sys;print('inherited-names='+json.dumps(sorted(os.environ)),file=sys.stderr)\n"
        + source
    )
    command = shlex.join([sys.executable, "-I", "-c", "import os,json;print(json.dumps(sorted(os.environ)))"])
    result = subprocess.run(
        [sys.executable, "-I", "-c", source, str(path), NONCE, command],
        env={"HOME": str(tmp_path), "PATH": os.defpath, "HRPOS_PASSWORD": "synthetic-secret",
             "PINKY_SESSION_SECRET": "synthetic-global", "LC_ALL": "C", "XDG_CONFIG_HOME": str(tmp_path),
             "HTTPS_PROXY": "https://proxy.invalid", "SSL_CERT_FILE": "/synthetic-ca"},
        capture_output=True, timeout=5,
    )
    assert not path.exists()
    assert b"synthetic-secret" not in result.stderr and b"synthetic-global" not in result.stderr
    return result


def test_loader_clean_exec_and_actual_names_only_log(tmp_path):
    result = loader_probe(tmp_path, {"env": {"GRANTED_NAME": "synthetic"}, "inherit": "none", "granted": ["GRANTED_NAME"]})
    assert result.returncode == 0, result.stderr
    names = set(json.loads(result.stdout))
    assert not DAEMON_NAMES & names
    assert {"HOME", "PATH", "LC_ALL", "XDG_CONFIG_HOME", "HTTPS_PROXY", "SSL_CERT_FILE", "GRANTED_NAME"} <= names
    assert b"inherited environment scrubbed" in result.stderr
    before = json.loads(result.stderr.splitlines()[0].removeprefix(b"inherited-names="))
    expected_base = {"HOME", "PATH", "LC_ALL", "XDG_CONFIG_HOME", "HTTPS_PROXY", "SSL_CERT_FILE"}
    count = len(set(before) - expected_base)
    assert f"{count} names dropped".encode() in result.stderr and b"GRANTED_NAME" in result.stderr


@pytest.mark.parametrize("policy", [None, True, "all", "other", {}, []])
def test_loader_malformed_policy_refuses_without_exec(tmp_path, policy):
    result = loader_probe(tmp_path, {"env": {"ALLOWED": "synthetic"}, "inherit": policy})
    assert result.returncode != 0 and not result.stdout


@pytest.mark.parametrize("key", ["PINKY_SESSION_SECRET", "PINKYBOT_FERRY_SHARED_SECRET"])
def test_loader_independently_refuses_daemon_only(tmp_path, key):
    result = loader_probe(tmp_path, {"env": {key: "synthetic"}, "inherit": "none"})
    assert result.returncode != 0 and not result.stdout


@pytest.mark.parametrize("auth_name", ["ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL"])
def test_claude_daemon_auth_inputs_explicit_when_not_overridden(grants, tmp_path, monkeypatch, auth_name):
    from pinky_daemon.streaming_session import StreamingSessionConfig
    from pinky_daemon.tmux_session import TmuxSession

    monkeypatch.setenv(auth_name, "synthetic-auth-input")
    session = TmuxSession(StreamingSessionConfig(agent_name="test-tenant", working_dir=str(tmp_path)), registry=Registry())
    assert session._build_repl_env()[auth_name] == "synthetic-auth-input"


def test_configured_provider_auth_overrides_grants(grants, tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-old-provider")
    grants.write_text('{"test-tenant":["ANTHROPIC_API_KEY"]}')
    build, _ = builder("claude", Registry(), tmp_path, monkeypatch)
    assert build()["ANTHROPIC_API_KEY"] == "synthetic-provider"
