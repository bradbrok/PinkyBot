"""The real SDK transport blanks ferry values and preserves legacy hook identity."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport

from pinky_daemon.claude_runner import ClaudeRunner, ClaudeRunnerConfig
from pinky_daemon.sdk_runner import SDKRunner, SDKRunnerConfig
from pinky_daemon.streaming_session import StreamingSession, StreamingSessionConfig

# The invocation below is constrained to a fixture-owned stub executable.
_TRANSPORT_CONNECT = SubprocessCLITransport.connect


class _OptionsCapturedError(Exception):
    pass


async def test_sdk_blanks_ferry_secrets_but_inherits_legacy_session_secret(tmp_path, monkeypatch):
    shared = "synthetic-sdk-shared-credential-0123456789"
    peer = "synthetic-sdk-peer-credential-012345678901"
    session_secret = "synthetic-sdk-legacy-session-0123456789"
    monkeypatch.setenv("PINKYBOT_FERRY_SHARED_SECRET", shared)
    monkeypatch.setenv("PINKYBOT_FERRY_PEER_SECRETS", json.dumps({"peer": peer}))
    monkeypatch.setenv("PINKY_SESSION_SECRET", session_secret)
    monkeypatch.setenv("CLAUDE_AGENT_SDK_SKIP_VERSION_CHECK", "1")
    monkeypatch.setenv("PINKY_RESUME_FAILSAFE", "0")
    report = tmp_path / "sdk-child-environment.json"
    stub = tmp_path / "fixture-sdk-cli"
    stub.write_text(
        f"#!{sys.executable}\n"
        "import json,os,pathlib\n"
        f"pathlib.Path({str(report)!r}).write_text(json.dumps(dict(os.environ)))\n"
    )
    stub.chmod(0o700)
    options = []

    def capture(value):
        value.cli_path = str(stub)
        options.append(value)
        raise _OptionsCapturedError

    monkeypatch.setattr("claude_agent_sdk.ClaudeSDKClient", capture)
    registry = SimpleNamespace(get=lambda name: SimpleNamespace(tool_policy_enabled=False))
    config = StreamingSessionConfig(agent_name="sdk-fixture", working_dir=str(tmp_path))
    with pytest.raises(_OptionsCapturedError):
        await StreamingSession(config, registry=registry).connect()
    assert len(options) == 1
    assert Path(options[0].cli_path).resolve().is_relative_to(tmp_path.resolve())
    transport = SubprocessCLITransport(prompt="", options=options[0])

    def no_cli_discovery():
        pytest.fail("the SDK fixture must never discover a real CLI")

    monkeypatch.setattr(transport, "_find_cli", no_cli_discovery)
    try:
        await _TRANSPORT_CONNECT(transport)
        await asyncio.wait_for(transport._process.wait(), timeout=5)
        assert report.is_file(), "the fixture CLI did not publish its actual environment"
        actual = json.loads(report.read_text())
    finally:
        await transport.close()

    ferry_values_absent = all(secret not in value for secret in (shared, peer) for value in actual.values())
    assert ferry_values_absent, "a ferry credential reached the SDK child environment"
    assert actual.get("PINKYBOT_FERRY_SHARED_SECRET") == ""
    assert actual.get("PINKYBOT_FERRY_PEER_SECRETS") == ""
    legacy_inherited = actual.get("PINKY_SESSION_SECRET") == session_secret
    assert legacy_inherited, "the SDK legacy session-secret contract changed"


def _seed_runner_environment(monkeypatch):
    shared = "synthetic-runner-shared-credential-0123456789"
    peer = "synthetic-runner-peer-credential-012345678901"
    session_secret = "synthetic-runner-legacy-session-0123456789"
    monkeypatch.setenv("PINKYBOT_FERRY_SHARED_SECRET", shared)
    monkeypatch.setenv("PINKYBOT_FERRY_PEER_SECRETS", json.dumps({"peer": peer}))
    monkeypatch.setenv("PINKY_SESSION_SECRET", session_secret)
    return shared, peer, session_secret


async def test_cli_runner_drops_ferry_secrets_before_exec(tmp_path, monkeypatch):
    shared, peer, session_secret = _seed_runner_environment(monkeypatch)
    monkeypatch.setenv("ORDINARY_RUNNER_CONFIG", "preserved")
    captured = []

    async def communicate():
        return b"fixture response", b""

    async def exec_spy(*args, **kwargs):
        captured.append(kwargs["env"])
        return SimpleNamespace(returncode=0, communicate=communicate)

    monkeypatch.setattr("pinky_daemon.claude_runner.asyncio.create_subprocess_exec", exec_spy)
    config = ClaudeRunnerConfig(working_dir=str(tmp_path), claude_bin=str(tmp_path / "unused-cli"))
    result = await ClaudeRunner(config).run("fixture prompt")
    assert result.ok
    assert len(captured) == 1
    actual = captured[0]
    assert "PINKYBOT_FERRY_SHARED_SECRET" not in actual
    assert "PINKYBOT_FERRY_PEER_SECRETS" not in actual
    assert all(secret not in value for secret in (shared, peer) for value in actual.values())
    assert actual["PINKY_SESSION_SECRET"] == session_secret
    assert actual["ORDINARY_RUNNER_CONFIG"] == "preserved"


async def test_sdk_runner_blanks_ferry_secrets_in_real_transport(tmp_path, monkeypatch):
    shared, peer, session_secret = _seed_runner_environment(monkeypatch)
    monkeypatch.setenv("CLAUDE_AGENT_SDK_SKIP_VERSION_CHECK", "1")
    report = tmp_path / "runner-child-environment.json"
    stub = tmp_path / "fixture-runner-cli"
    stub.write_text(
        f"#!{sys.executable}\n"
        "import json,os,pathlib\n"
        f"pathlib.Path({str(report)!r}).write_text(json.dumps(dict(os.environ)))\n"
    )
    stub.chmod(0o700)
    captured = []

    async def capture_query(*, prompt, options):
        options.cli_path = str(stub)
        captured.append(options)
        if False:
            yield None

    monkeypatch.setattr("claude_agent_sdk.query", capture_query)
    await SDKRunner(SDKRunnerConfig(working_dir=str(tmp_path))).run("fixture prompt")
    assert len(captured) == 1
    assert Path(captured[0].cli_path).resolve().is_relative_to(tmp_path.resolve())
    transport = SubprocessCLITransport(prompt="", options=captured[0])

    def no_cli_discovery():
        pytest.fail("the SDK runner fixture must never discover a real CLI")

    monkeypatch.setattr(transport, "_find_cli", no_cli_discovery)
    try:
        await _TRANSPORT_CONNECT(transport)
        await asyncio.wait_for(transport._process.wait(), timeout=5)
        assert report.is_file(), "the fixture runner did not publish its actual environment"
        actual = json.loads(report.read_text())
    finally:
        await transport.close()

    assert all(secret not in value for secret in (shared, peer) for value in actual.values()), (
        "a ferry credential reached the one-shot SDK child environment"
    )
    assert actual.get("PINKYBOT_FERRY_SHARED_SECRET") == ""
    assert actual.get("PINKYBOT_FERRY_PEER_SECRETS") == ""
    assert actual.get("PINKY_SESSION_SECRET") == session_secret
