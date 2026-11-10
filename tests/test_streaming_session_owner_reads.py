"""Real streaming sends supply ownership evidence outside the legacy manager."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from pinky_daemon.streaming_session import StreamingSession, StreamingSessionConfig
from pinky_daemon.transport import SessionState
from tests.isolated_policy_support import daemon as daemon
from tests.test_session_owner_reads import _read, _register

pytestmark = pytest.mark.real_auth


def streaming_session(instance, owner, label, *, connected=True):
    session = StreamingSession(
        StreamingSessionConfig(
            agent_name=owner,
            label=label,
            working_dir=str(instance.root / owner),
            resume_handle="opaque-" + owner + "-" + label,
        ),
        conversation_store=instance.app.state.conversation_store,
    )
    if connected:
        session._state_machine._state = SessionState.CONNECTED
    session._client = SimpleNamespace(query=AsyncMock())
    instance.app.state.broker.register_streaming(owner, session, label=label)
    return session


@pytest.mark.asyncio
@pytest.mark.parametrize("query", ["", "?q=streamownerneedle"])
@pytest.mark.parametrize("reader", ["tenant", "tenant-peer"])
async def test_real_streaming_send_keeps_its_rows_with_the_registered_owner(
    daemon, monkeypatch, query, reader
):
    instance = daemon()
    _register(instance, "tenant-peer")
    monkeypatch.setenv("PINKY_ISOLATED_READ_POLICY_MODE", "enforce")
    session = streaming_session(instance, "tenant", "peer-main")
    instance.agents.set_streaming_session_id(
        "tenant", session.resume_handle, label="peer-main"
    )
    assert instance.app.state.manager.list() == []
    assert session.agent_name == "tenant" and session.id == "tenant-peer-main"
    assert await session.send("streamownerneedle written by the short owner") is True
    session._client.query.assert_awaited_once()
    assert instance.app.state.conversation_store.count(session.id) == 1
    response = _read(instance, "/agents/" + reader + "/chat-history" + query, name=reader)
    assert response.status_code == 200
    present = any("streamownerneedle" in row["content"] for row in response.json()["messages"])
    assert present is (reader == "tenant")


@pytest.mark.asyncio
@pytest.mark.parametrize("both_live", [False, True])
@pytest.mark.parametrize("query", ["", "?q=twoliveownerneedle"])
async def test_real_streaming_sends_hide_a_collided_id_and_keep_distinct_ids(
    daemon, monkeypatch, both_live, query
):
    instance = daemon()
    _register(instance, "tenant-peer")
    monkeypatch.setenv("PINKY_ISOLATED_READ_POLICY_MODE", "enforce")
    configs = [("tenant", "peer-main")]
    if both_live:
        configs.append(("tenant-peer", "main"))
    collided = []
    for owner, label in configs:
        session = streaming_session(instance, owner, label)
        instance.agents.set_streaming_session_id(owner, session.resume_handle, label=label)
        assert await session.send("twoliveownerneedle shared-id " + owner) is True
        session._client.query.assert_awaited_once()
        collided.append(session)
    assert {session.id for session in collided} == {"tenant-peer-main"}
    for owner, label in [("tenant", "own"), ("tenant-peer", "own")]:
        session = streaming_session(instance, owner, label)
        assert await session.send("twoliveownerneedle distinct " + owner) is True
    assert instance.app.state.manager.list() == []
    observed = {}
    for owner in ["tenant", "tenant-peer"]:
        response = _read(instance, "/agents/" + owner + "/chat-history" + query, name=owner)
        assert response.status_code == 200
        observed[owner] = {row["content"] for row in response.json()["messages"]}
    expected = {
        "tenant": {"twoliveownerneedle distinct tenant"},
        "tenant-peer": {"twoliveownerneedle distinct tenant-peer"},
    }
    if not both_live:
        expected["tenant"].add("twoliveownerneedle shared-id tenant")
    assert observed == expected
