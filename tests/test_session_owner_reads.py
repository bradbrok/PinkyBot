"""Agent reads and lifecycle attribution follow the available owner evidence."""

import asyncio

import pytest
from fastapi.testclient import TestClient

from tests.isolated_policy_support import closure, signed
from tests.isolated_policy_support import daemon as daemon
from tests.test_isolated_read_enforce import _read

pytestmark = pytest.mark.real_auth


@pytest.mark.parametrize("suffix", ["", "?q=ownershipneedle"])
def test_chat_history_does_not_include_a_longer_agents_rows(daemon, monkeypatch, suffix):
    instance = daemon()
    work = instance.root / "tenant-peer"
    work.mkdir()
    instance.agents.register("tenant-peer", isolated=True, working_dir=str(work), groups=["other"])
    store = instance.app.state.conversation_store
    store.append("tenant-main", "assistant", "Own history fixture")
    store.append("tenant-peer-main", "assistant", "ownershipneedle foreign fixture")
    monkeypatch.setenv("PINKY_ISOLATED_READ_POLICY_MODE", "enforce")
    response = _read(instance, "/agents/tenant/chat-history" + suffix)
    assert response.status_code == 200
    assert not any("ownershipneedle" in row["content"] for row in response.json()["messages"])


def test_sessions_do_not_include_a_longer_agents_live_session(daemon, monkeypatch):
    instance = daemon()
    work = instance.root / "tenant-peer"
    work.mkdir()
    instance.agents.register("tenant-peer", isolated=True, working_dir=str(work), groups=["other"])
    instance.app.state.manager.create(
        session_id="tenant-peer-main",
        agent_name="tenant-peer",
        soul="foreign-session-body-fixture",
        working_dir=str(work),
    )
    monkeypatch.setenv("PINKY_ISOLATED_READ_POLICY_MODE", "enforce")
    response = _read(instance, "/agents/tenant/sessions")
    assert response.status_code == 200
    assert "foreign-session-body-fixture" not in response.text


@pytest.mark.asyncio
async def test_incremental_stream_separates_agent_and_label(daemon, monkeypatch):
    from tests.test_isolated_read_shadow import _request, _shadow_check

    instance = daemon()
    work = instance.root / "tenant-peer"
    work.mkdir()
    instance.agents.register("tenant-peer", isolated=True, working_dir=str(work), groups=["other"])
    monkeypatch.setenv("PINKY_ISOLATED_READ_POLICY_MODE", "enforce")
    assert not _shadow_check(instance)(
        _request(instance, "/agents/tenant/streaming/events"), "tenant"
    )
    response = await closure(instance.app, "stream_agent_events")("tenant", label="peer-main")
    foreign = await closure(instance.app, "_make_streaming_event_callback")("tenant-peer", "main")
    own = await closure(instance.app, "_make_streaming_event_callback")("tenant", "peer-main")
    await foreign({"type": "delta", "text": "foreign-stream-body-fixture"})
    await own({"type": "delta", "text": "own-stream-body-fixture"})
    try:
        event = await asyncio.wait_for(anext(response.body_iterator), 0.5)
    finally:
        await response.body_iterator.aclose()
    assert "foreign-stream-body-fixture" not in event
    assert "own-stream-body-fixture" in event


@pytest.mark.parametrize(
    "session_id,registered_owner,expected",
    [
        ("worker-name-main", "worker-name", "worker-name"),
        ("unlisted-main", "", ""),
        ("tenant-main", "", "tenant"),
    ],
    ids=["hyphenated-owner", "unowned", "unchanged-owner"],
)
def test_event_attribution_uses_the_registered_owner(
    daemon,
    session_id,
    registered_owner,
    expected,
):
    instance = daemon()
    if registered_owner:
        work = instance.root / registered_owner
        work.mkdir()
        instance.agents.register(registered_owner, working_dir=str(work))
    path = "/sessions/" + session_id + "/events"
    client = TestClient(instance.app)
    try:
        response = client.post(
            path,
            json={"event_type": "session_start"},
            headers=signed(instance, "POST", path, "normal"),
        )
    finally:
        client.close()
    assert response.status_code == 200
    assert set(response.json()) == {"ok", "id", "session_id", "event_type"}
    assert response.json()["ok"] is True
    events = _read(instance, path, name="normal")
    assert events.status_code == 200
    assert events.json()["count"] == 1
    assert events.json()["events"][0]["agent_name"] == expected
