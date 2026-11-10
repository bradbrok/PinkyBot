"""Explicit ownership retains eligible search rows without widening its scope."""

import pytest

from tests.isolated_policy_support import daemon as daemon
from tests.test_session_owner_identity import messages as messages
from tests.test_session_owner_reads import _collect, _read, _register
from tests.test_streaming_session_owner_reads import streaming_session

pytestmark = pytest.mark.real_auth


@pytest.mark.asyncio
@pytest.mark.parametrize("caller", ["tenant", "tenant-peer"])
async def test_failed_index_search_retains_only_the_broker_owner(daemon, monkeypatch, caller):
    instance = daemon()
    _register(instance, "tenant-peer")
    monkeypatch.setenv("PINKY_ISOLATED_READ_POLICY_MODE", "enforce")
    session = streaming_session(instance, "tenant", "peer-main")
    assert await session.send("retainedneedle owned fixture") is True
    session._client.query.assert_awaited_once()
    assert instance.app.state.manager.list() == []
    assert instance.agents.list_streaming_session_ids("tenant") == []

    def unavailable(prefix):
        raise OSError("fixture indexed discovery unavailable")

    monkeypatch.setattr(instance.app.state.conversation_store,
                        "list_session_ids_with_prefix", unavailable)
    assert _collect(instance, caller) == set()
    response = _read(instance, "/agents/" + caller + "/chat-history?q=retainedneedle", name=caller)
    assert response.status_code == 200
    assert [row["session_id"] for row in response.json()["messages"]] == (
        [session.id] if caller == "tenant" else []
    )


def test_retains_cannot_widen_candidate_scope(messages):
    own = messages.append("tenant-main", "user", "needle own", timestamp=10)
    messages.append("outside-main", "user", "needle outside", timestamp=99)
    arguments = dict(session_ids=set(), session_prefix="tenant-", exclude_prefixes=("tenant-",))
    assert messages.search("needle", retain_session_ids=("outside-main",), **arguments) == []
    assert [row.id for row in messages.search(
        "needle", retain_session_ids=(own.session_id,), **arguments
    )] == [own.id]


def test_explicit_id_exclusion_overrides_retains(messages):
    own = messages.append("tenant-own", "user", "needle own", timestamp=10)
    collision = messages.append("tenant-peer-main", "user", "needle collision", timestamp=99)
    assert [row.id for row in messages.search(
        "needle", session_ids=set(), session_prefix="tenant-",
        exclude_prefixes=("tenant-peer-",), exclude_session_ids=(collision.session_id,),
        retain_session_ids=(collision.session_id,),
    )] == [own.id]


def test_search_defaults_keep_the_existing_scope_and_prefix_union(messages):
    own = messages.append("tenant-main", "user", "needle own", timestamp=10)
    other = messages.append("tenant-peer-main", "user", "needle other", timestamp=20)
    opaque = messages.append("opaque-own", "user", "needle recorded", timestamp=30)
    assert [row.id for row in messages.search("needle")] == [opaque.id, other.id, own.id]
    arguments = dict(session_ids={opaque.session_id}, session_prefix="tenant-")
    assert [row.id for row in messages.search("needle", **arguments)] == [opaque.id, other.id, own.id]
    assert [row.id for row in messages.search(
        "needle", exclude_prefixes=("tenant-peer-",), **arguments
    )] == [opaque.id, own.id]


@pytest.mark.asyncio
async def test_plain_history_failed_discovery_keeps_the_empty_base_candidate_set(
    daemon, monkeypatch
):
    instance = daemon()
    _register(instance, "tenant-peer")
    monkeypatch.setenv("PINKY_ISOLATED_READ_POLICY_MODE", "enforce")
    session = streaming_session(instance, "tenant", "peer-main")
    assert await session.send("retainedneedle owned fixture") is True
    assert instance.app.state.manager.list() == []
    assert instance.agents.list_streaming_session_ids("tenant") == []

    def unavailable(prefix):
        raise OSError("fixture indexed discovery unavailable")

    monkeypatch.setattr(instance.app.state.conversation_store,
                        "list_session_ids_with_prefix", unavailable)
    for owner in ["tenant", "tenant-peer"]:
        assert _collect(instance, owner) == set()
        response = _read(instance, "/agents/" + owner + "/chat-history", name=owner)
        assert response.status_code == 200
        assert response.json()["messages"] == []
