"""Default-off traces also executable on the pre-feature base."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from pinky_daemon.streaming_session import StreamingSession, StreamingSessionConfig
from pinky_daemon.transport_state import SessionState


@pytest.mark.parametrize("flag", [None, "0"])
async def test_disabled_failsafe_preserves_original_failed_sdk_connect(tmp_path, monkeypatch, flag):
    if flag is None:
        monkeypatch.delenv("PINKY_RESUME_FAILSAFE", raising=False)
    else:
        monkeypatch.setenv("PINKY_RESUME_FAILSAFE", flag)
    error = RuntimeError("startup failed")
    client = SimpleNamespace(connect=AsyncMock(side_effect=error), disconnect=AsyncMock())
    factory = MagicMock(return_value=client)
    monkeypatch.setattr("claude_agent_sdk.ClaudeSDKClient", factory)
    session = StreamingSession(StreamingSessionConfig(
        agent_name="sample", working_dir=str(tmp_path), resume_handle="saved",
    ))
    with pytest.raises(RuntimeError) as caught:
        await session.connect()
    assert caught.value is error
    assert factory.call_count == 1
    assert session._client is client
    client.disconnect.assert_not_awaited()
    assert session.resume_handle == "saved"
    assert session.state == SessionState.DEAD


@pytest.mark.parametrize("flag", [None, "0"])
async def test_disabled_failsafe_preserves_original_reconnect_backoff(tmp_path, monkeypatch, flag):
    if flag is None:
        monkeypatch.delenv("PINKY_RESUME_FAILSAFE", raising=False)
    else:
        monkeypatch.setenv("PINKY_RESUME_FAILSAFE", flag)
    session = StreamingSession(StreamingSessionConfig(agent_name="sample", working_dir=str(tmp_path)))
    session._RECONNECT_BACKOFF = (0, 0, 0)
    session.disconnect = AsyncMock()
    session.connect = AsyncMock(side_effect=RuntimeError("unavailable"))
    await session._reconnect_with_backoff()
    assert session.connect.await_count == 3
    assert session.disconnect.await_count == 4
    assert session.state == SessionState.DEAD
