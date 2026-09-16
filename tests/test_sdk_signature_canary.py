"""Installed SDK contracts must drift visibly without widening attribution."""

import importlib.metadata
import json
from unittest.mock import MagicMock

import pytest
from claude_agent_sdk._errors import ProcessError

from pinky_daemon.resume_recovery import sdk_rejection
from pinky_daemon.streaming_session import StreamingSession, StreamingSessionConfig
from tests.recovery_test_support import SDKPeer

REQUESTED = "11111111-1111-4111-8111-111111111111"
OTHER = "22222222-2222-4222-8222-222222222222"


def rejection(requested=REQUESTED, stderr=None):
    return ProcessError(
        f"Claude Code returned an error result: No conversation found with session ID: {requested}",
        exit_code=1,
        stderr=stderr,
    )


def test_installed_process_error_rendering_contract():
    assert importlib.metadata.version("claude-agent-sdk") == "0.2.138", (
        "SDK changed: re-characterize the initialize rejection before updating this canary"
    )
    plain = rejection()
    assert sdk_rejection(plain, REQUESTED, 0) is not None
    decorated = rejection(stderr="synthetic-diagnostic")
    assert str(decorated) == str(plain) + "\nError output: synthetic-diagnostic"
    assert sdk_rejection(decorated, REQUESTED, 0) is None
    assert sdk_rejection(rejection(OTHER), REQUESTED, 0) is None


@pytest.mark.parametrize(
    "case,version,reason",
    [
        ("version", "0.2.999", "unsupported_sdk_version"),
        ("stderr", "0.2.138", "stderr_shape_changed"),
        ("unknown", "0.2.138", "unclassified_initialize"),
    ],
)
async def test_signature_drift_is_visible_once_without_fresh_retry(
    tmp_path,
    monkeypatch,
    case,
    version,
    reason,
):
    monkeypatch.setenv("PINKY_RESUME_FAILSAFE", "1")
    real_version = importlib.metadata.version
    reads = []

    def metadata(name):
        if name == "claude-agent-sdk":
            reads.append(name)
            return version
        return real_version(name)

    monkeypatch.setattr(importlib.metadata, "version", metadata)
    error = (
        rejection(stderr="synthetic-secret /private/diagnostic")
        if case == "stderr"
        else ProcessError("unknown initialize format", exit_code=1)
        if case == "unknown"
        else rejection()
    )
    clients = []

    def factory(options):
        client = SDKPeer(options)
        client.connect.side_effect = error
        clients.append(client)
        return client

    monkeypatch.setattr("claude_agent_sdk.ClaudeSDKClient", factory)
    logs = []
    monkeypatch.setattr("pinky_daemon.streaming_session._log", logs.append)
    for _ in range(2):
        ss = StreamingSession(
            StreamingSessionConfig(
                agent_name="sample",
                working_dir=str(tmp_path),
                resume_handle=REQUESTED,
            )
        )
        with pytest.raises(ProcessError) as caught:
            await ss.connect()
        assert caught.value is error
    assert reads, "Enabled resume never checked the installed SDK contract"
    assert len(clients) == 2, "Contract drift authorized a fresh retry"
    assert all(client.query.await_count == 0 for client in clients)
    events = [json.loads(line) for line in logs if line.startswith("{")]
    drift = [event for event in events if event.get("type") == "resume_signature_contract_drift"]
    assert len(drift) == 1, "Contract drift must emit one rate-limited metadata event"
    assert drift[0]["sdk_version"] == version
    assert drift[0]["reason"] == reason
    rendered = json.dumps(drift)
    assert len(rendered) <= 512
    assert all(value not in rendered for value in [REQUESTED, "synthetic-secret", "/private/"])
    assert not any(event.get("type") == "resume_fallback_attempted" for event in events)


@pytest.mark.parametrize("flag", [None, "0"])
async def test_disabled_canary_keeps_original_failure_and_silence(tmp_path, monkeypatch, flag):
    if flag is None:
        monkeypatch.delenv("PINKY_RESUME_FAILSAFE", raising=False)
    else:
        monkeypatch.setenv("PINKY_RESUME_FAILSAFE", flag)
    peer = SDKPeer()
    error = rejection(stderr="synthetic-diagnostic")
    peer.connect.side_effect = error
    factory = MagicMock(return_value=peer)
    monkeypatch.setattr("claude_agent_sdk.ClaudeSDKClient", factory)
    logs = []
    monkeypatch.setattr("pinky_daemon.streaming_session._log", logs.append)
    ss = StreamingSession(
        StreamingSessionConfig(
            agent_name="sample",
            working_dir=str(tmp_path),
            resume_handle=REQUESTED,
        )
    )
    with pytest.raises(ProcessError) as caught:
        await ss.connect()
    assert caught.value is error
    factory.assert_called_once()
    peer.disconnect.assert_not_awaited()
    peer.query.assert_not_awaited()
    assert not any("resume_signature_contract_drift" in line for line in logs)


@pytest.mark.parametrize("installed", ["1" * 1024 + ".2.3", "0.2.999+/private/synthetic"])
async def test_malformed_version_metadata_is_bounded_and_excluded(tmp_path, monkeypatch, installed):
    monkeypatch.setenv("PINKY_RESUME_FAILSAFE", "1")
    real_version = importlib.metadata.version
    monkeypatch.setattr(importlib.metadata, "version", lambda name: installed
                        if name == "claude-agent-sdk" else real_version(name))
    peer = SDKPeer()
    error = rejection()
    peer.connect.side_effect = error
    factory = MagicMock(return_value=peer)
    monkeypatch.setattr("claude_agent_sdk.ClaudeSDKClient", factory)
    logs = []
    monkeypatch.setattr("pinky_daemon.streaming_session._log", logs.append)
    ss = StreamingSession(StreamingSessionConfig(
        agent_name="sample", working_dir=str(tmp_path), resume_handle=REQUESTED,
    ))
    with pytest.raises(ProcessError) as caught:
        await ss.connect()
    assert caught.value is error
    factory.assert_called_once()
    peer.query.assert_not_awaited()
    events = [json.loads(line) for line in logs if line.startswith("{")]
    assert events == [{"type": "resume_signature_contract_drift", "sdk_version": "unknown",
                       "reason": "unsupported_sdk_version"}]
    assert len(json.dumps(events)) <= 512
