"""Real SDK recovery must retain clients whose cleanup is unconfirmed."""

import asyncio
from unittest.mock import MagicMock

import pytest

from pinky_daemon.streaming_session import StreamingSession, StreamingSessionConfig
from pinky_daemon.transport_state import SessionState
from tests.recovery_test_support import SDKPeer


@pytest.mark.parametrize("stage", ["initial", "between", "terminal"])
@pytest.mark.parametrize("failure", ["error", "timeout"])
@pytest.mark.parametrize("prior_strict", [False, True])
async def test_cleanup_uncertainty_blocks_this_and_later_recovery_cycles(
    tmp_path,
    monkeypatch,
    stage,
    failure,
    prior_strict,
):
    monkeypatch.setenv("PINKY_RESUME_FAILSAFE", "1")
    ss = StreamingSession(
        StreamingSessionConfig(
            agent_name="sample",
            working_dir=str(tmp_path),
            # Deliberately malformed persisted context fails wake assembly AFTER
            # the real SDK initialize succeeds, reaching between-attempt cleanup.
            wake_context=7 if stage != "initial" else "",
        )
    )
    ss._state_machine._state = SessionState.CONNECTED
    ss._replacement_cleanup_strict = prior_strict
    ss._RECONNECT_BACKOFF = (0, 0) if stage == "between" else (0,)
    error = RuntimeError("child still alive") if failure == "error" else TimeoutError("cleanup")
    peers = []

    def factory(options):
        peer = SDKPeer(options)
        peer.disconnect.side_effect = error
        peers.append(peer)
        return peer

    factory_spy = MagicMock(side_effect=factory)
    monkeypatch.setattr("claude_agent_sdk.ClaudeSDKClient", factory_spy)
    existing = SDKPeer()
    if stage == "initial":
        existing.disconnect.side_effect = error
    ss._client = existing
    try:
        await ss._reconnect_with_backoff()
        first_count = factory_spy.call_count
        retained = ss._client
        first_state = ss.state
        await ss._reconnect_with_backoff()
        expected = 0 if stage == "initial" else 1
        assert ss._stats["reconnects"] == expected, "Recovery retried after unconfirmed cleanup"
        assert first_count == expected, "Recovery spawned after uncertain cleanup"
        assert factory_spy.call_count == expected, "Outer recovery renewed a failed cleanup cycle"
        assert retained is (existing if stage == "initial" else peers[0])
        assert ss._client is retained
        assert first_state == ss.state == SessionState.DEAD
        assert ss._replacement_cleanup_strict is prior_strict
        assert all(peer.query.await_count == 0 for peer in peers)
    finally:
        for peer in [existing, *peers]:
            peer.disconnect.side_effect = peer.close
        await ss.disconnect()


@pytest.mark.parametrize("prior_strict", [False, True])
async def test_cancelled_cleanup_keeps_client_and_restores_strictness(
    tmp_path,
    monkeypatch,
    prior_strict,
):
    monkeypatch.setenv("PINKY_RESUME_FAILSAFE", "1")
    ss = StreamingSession(StreamingSessionConfig(agent_name="sample", working_dir=str(tmp_path)))
    peer = SDKPeer()
    peer.disconnect.side_effect = asyncio.CancelledError()
    ss._client = peer
    ss._replacement_cleanup_strict = prior_strict
    factory = MagicMock()
    monkeypatch.setattr("claude_agent_sdk.ClaudeSDKClient", factory)
    with pytest.raises(asyncio.CancelledError):
        await ss._reconnect_with_backoff()
    assert ss._client is peer
    assert ss.state == SessionState.DEAD
    assert ss._replacement_cleanup_strict is prior_strict
    factory.assert_not_called()


@pytest.mark.parametrize("flag", [None, "0"])
async def test_disabled_recovery_keeps_legacy_disconnect_trace(tmp_path, monkeypatch, flag):
    if flag is None:
        monkeypatch.delenv("PINKY_RESUME_FAILSAFE", raising=False)
    else:
        monkeypatch.setenv("PINKY_RESUME_FAILSAFE", flag)
    ss = StreamingSession(StreamingSessionConfig(agent_name="sample", working_dir=str(tmp_path)))
    ss._RECONNECT_BACKOFF = (0,)
    old = SDKPeer()
    old.disconnect.side_effect = RuntimeError("child still alive")
    ss._client = old
    new = SDKPeer()
    factory = MagicMock(return_value=new)
    monkeypatch.setattr("claude_agent_sdk.ClaudeSDKClient", factory)
    try:
        await ss._reconnect_with_backoff()
        factory.assert_called_once()
        assert ss._client is new
        assert ss.state == SessionState.CONNECTED
        old.disconnect.assert_awaited_once()
    finally:
        await ss.disconnect()
