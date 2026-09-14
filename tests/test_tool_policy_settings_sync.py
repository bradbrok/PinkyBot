"""Managed command upgrades and double opt-in at the process environment boundary."""

from __future__ import annotations

import copy
import json
import os
import shlex
import subprocess
from unittest.mock import MagicMock

import pytest

from pinky_daemon import agent_registry
from pinky_daemon.agent_registry import AgentRegistry
from pinky_daemon.streaming_session import StreamingSession, StreamingSessionConfig
from pinky_daemon.tmux_session import TmuxSession


def _command(path):
    return ('if [ "${PINKY_TOOL_POLICY:-off}" = "off" ]; then exit 0; fi; '
            f'python3 {shlex.quote(str(path))} || exit 2')


def _installed(tmp_path):
    directory = tmp_path / "sample"
    directory.mkdir(exist_ok=True)
    AgentRegistry._setup_hooks(directory, "sample")
    settings = json.loads((directory / ".claude/settings.json").read_text())
    matching = [hook for bucket in settings["hooks"]["PreToolUse"]
                for hook in bucket["hooks"] if "hook_tool_policy.py" in hook["command"]]
    assert len(matching) == 1, "managed policy hook must be installed exactly once"
    return directory, matching[0], settings


@pytest.mark.parametrize("old_timeout", [None, 30, 600, 660])
def test_timeout_insert_upgrade_and_idempotence_preserve_other_hooks(old_timeout):
    hook = {"type": "command", "command": "python3 /work/hook_tool_policy.py || true"}
    if old_timeout is not None:
        hook["timeout"] = old_timeout
    unrelated = {"type": "command", "command": "custom-hook", "timeout": 17}
    hooks = {"PreToolUse": [{"matcher": ".*", "hooks": [hook, copy.deepcopy(unrelated)]}]}
    kwargs = dict(needle="/work/hook_tool_policy.py", command=_command("/work/hook_tool_policy.py"), timeout=660)
    assert AgentRegistry._merge_hook_into_event(hooks, "PreToolUse", **kwargs)
    assert hooks["PreToolUse"][0]["hooks"] == [
        {"type": "command", "command": kwargs["command"], "timeout": 660}, unrelated,
    ]
    before = copy.deepcopy(hooks)
    assert not AgentRegistry._merge_hook_into_event(hooks, "PreToolUse", **kwargs)
    assert hooks == before
    inserted = {}
    assert AgentRegistry._merge_hook_into_event(inserted, "PreToolUse", **kwargs)
    assert inserted["PreToolUse"][0]["hooks"][0]["timeout"] == 660


@pytest.mark.parametrize("old_timeout", [None, 30, 600])
def test_timeout_is_patched_even_if_command_is_already_current(old_timeout):
    command = _command("/work/hook_tool_policy.py")
    hook = {"type": "command", "command": command}
    if old_timeout is not None:
        hook["timeout"] = old_timeout
    hooks = {"PreToolUse": [{"matcher": ".*", "hooks": [hook]}]}
    assert AgentRegistry._merge_hook_into_event(
        hooks, "PreToolUse", needle="/work/hook_tool_policy.py", command=command, timeout=660,
    )
    assert hook["timeout"] == 660


def test_managed_script_rewritten_and_settings_upgrade_is_idempotent(tmp_path):
    directory, entry, settings = _installed(tmp_path)
    script = directory / ".claude/hook_tool_policy.py"
    assert entry == {"type": "command", "command": _command(script),
                     "timeout": agent_registry.TOOL_POLICY_HOOK_TIMEOUT_SEC}
    assert entry["timeout"] == 660
    source = script.read_text()
    compile(source, str(script), "exec")
    settings["hooks"]["PreToolUse"].append({"matcher": "Read", "hooks": [
        {"type": "command", "command": "custom-hook", "timeout": 7},
    ]})
    settings_path = directory / ".claude/settings.json"
    settings_path.write_text(json.dumps(settings))
    script.write_text("outdated script")
    AgentRegistry._setup_hooks(directory, "sample")
    assert script.read_text() == source
    first = settings_path.read_bytes()
    AgentRegistry._setup_hooks(directory, "sample")
    assert settings_path.read_bytes() == first
    assert {"type": "command", "command": "custom-hook", "timeout": 7} in [
        h for b in json.loads(first)["hooks"]["PreToolUse"] for h in b["hooks"]
    ]


@pytest.mark.parametrize("mode", [None, "off"])
def test_shell_off_short_circuits_without_starting_python(tmp_path, mode):
    _, entry, _ = _installed(tmp_path)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    marker = tmp_path / "python-started"
    fake_python = bin_dir / "python3"
    fake_python.write_text(f'#!/bin/sh\nprintf started > {shlex.quote(str(marker))}\nexit 93\n')
    fake_python.chmod(0o700)
    env = {**os.environ, "PATH": str(bin_dir)}
    env.pop("PINKY_TOOL_POLICY", None)
    if mode is not None:
        env["PINKY_TOOL_POLICY"] = mode
    result = subprocess.run(["/bin/sh", "-c", entry["command"]], env=env,
                            capture_output=True, text=True, timeout=3)
    assert result.returncode == 0
    assert not marker.exists(), "off must not start even the interpreter"


@pytest.mark.parametrize("failure", ["missing-interpreter", "crash"])
def test_shell_turns_interpreter_failure_into_exit_two(tmp_path, failure):
    _, entry, _ = _installed(tmp_path)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    if failure == "crash":
        fake_python = bin_dir / "python3"
        fake_python.write_text("#!/bin/sh\nexit 93\n")
        fake_python.chmod(0o700)
    result = subprocess.run(["/bin/sh", "-c", entry["command"]],
                            env={**os.environ, "PATH": str(bin_dir), "PINKY_TOOL_POLICY": "enforce"},
                            capture_output=True, text=True, timeout=3)
    assert result.returncode == 2


@pytest.mark.parametrize("existing", [False, True])
def test_registration_payload_cannot_enable_policy(tmp_path, existing):
    registry = AgentRegistry(str(tmp_path / "agents.db"))
    try:
        if existing:
            registry.register("sample", working_dir=str(tmp_path / "sample"))
        agent = registry.register("sample", working_dir=str(tmp_path / "sample"),
                                  tool_policy_enabled=True)
        assert agent.to_dict().get("tool_policy_enabled") is False
        registry.update("sample", tool_policy_enabled=True)
        assert registry.get("sample").tool_policy_enabled is True
        registry.register("sample", tool_policy_enabled=False)
        assert registry.get("sample").tool_policy_enabled is True
    finally:
        registry.close()


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("mode", ["off", "log", "enforce"])
def test_tmux_exports_effective_opt_in_mode(tmp_path, monkeypatch, enabled, mode):
    monkeypatch.setenv("PINKY_TOOL_POLICY", mode)
    registry = AgentRegistry(str(tmp_path / "agents.db"))
    try:
        registry.register("sample", working_dir=str(tmp_path / "sample"))
        registry.update("sample", tool_policy_enabled=enabled)
        session = TmuxSession(StreamingSessionConfig(agent_name="sample", working_dir=str(tmp_path)),
                              registry=registry, tmux_control=MagicMock())
        assert session._build_repl_env().get("PINKY_TOOL_POLICY") == (mode if enabled else "off")
    finally:
        registry.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("mode", ["off", "log", "enforce"])
async def test_sdk_exports_effective_opt_in_mode_without_connecting(tmp_path, monkeypatch, enabled, mode):
    monkeypatch.setenv("PINKY_TOOL_POLICY", mode)
    registry = AgentRegistry(str(tmp_path / "agents.db"))
    captured = []

    class StopBeforeConnectError(Exception):
        pass

    def capture(options):
        captured.append(options)
        raise StopBeforeConnectError

    monkeypatch.setattr("claude_agent_sdk.ClaudeSDKClient", capture)
    try:
        registry.register("sample", working_dir=str(tmp_path / "sample"))
        registry.update("sample", tool_policy_enabled=enabled)
        session = StreamingSession(StreamingSessionConfig(agent_name="sample", working_dir=str(tmp_path)),
                                   registry=registry)
        with pytest.raises(StopBeforeConnectError):
            await session.connect()
        assert captured[0].env.get("PINKY_TOOL_POLICY") == (mode if enabled else "off")
    finally:
        registry.close()


def test_hook_generator_is_available_for_workspace_sync():
    assert callable(getattr(agent_registry, "_tool_policy_hook_source", None))


def test_exported_deadline_and_settings_timeout_constants(tmp_path):
    deadline = getattr(agent_registry, "TOOL_POLICY_HOOK_DEADLINE_SEC", None)
    timeout = getattr(agent_registry, "TOOL_POLICY_HOOK_TIMEOUT_SEC", None)
    assert deadline == 600
    assert timeout == 660
    _, entry, _ = _installed(tmp_path)
    assert entry["timeout"] == timeout


def test_settings_sync_uses_exported_timeout_constant(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_registry, "TOOL_POLICY_HOOK_TIMEOUT_SEC", 661)
    directory, entry, settings = _installed(tmp_path)
    assert entry["timeout"] == 661
    for bucket in settings["hooks"]["PreToolUse"]:
        for hook in bucket["hooks"]:
            if "hook_tool_policy.py" in hook["command"]:
                hook["timeout"] = 17
    settings_path = directory / ".claude/settings.json"
    settings_path.write_text(json.dumps(settings))
    AgentRegistry._setup_hooks(directory, "sample")
    entries = [h for b in json.loads(settings_path.read_text())["hooks"]["PreToolUse"]
               for h in b["hooks"] if "hook_tool_policy.py" in h["command"]]
    assert len(entries) == 1
    assert entries[0]["timeout"] == 661


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["off", "log", "enforce"])
@pytest.mark.parametrize("transport", ["tmux", "sdk"])
async def test_registry_lookup_failure_defers_policy_to_server(
    tmp_path, monkeypatch, capsys, mode, transport,
):
    monkeypatch.setenv("PINKY_TOOL_POLICY", mode)
    registry = MagicMock()
    registry.get.side_effect = RuntimeError("test registry unavailable")
    registry.get_signing_key.return_value = "sample-agent-key"
    captured = []

    class StopBeforeConnectError(Exception):
        pass

    def capture(options):
        captured.append(options)
        raise StopBeforeConnectError

    config = StreamingSessionConfig(agent_name="sample", working_dir=str(tmp_path))
    if transport == "tmux":
        session = TmuxSession(config, registry=registry, tmux_control=MagicMock())
        env = session._build_repl_env()
    else:
        monkeypatch.setattr("claude_agent_sdk.ClaudeSDKClient", capture)
        session = StreamingSession(config, registry=registry)
        with pytest.raises(StopBeforeConnectError):
            await session.connect()
        env = captured[0].env
    assert env["PINKY_TOOL_POLICY"] == mode
    assert "PINKY_SESSION_SECRET" not in env
    warning = capsys.readouterr().err
    assert "WARNING" in warning
    assert "tool policy flag lookup failed" in warning
    assert "sample" in warning and "test registry unavailable" in warning
    assert "exporting armed, server-authoritative" in warning
