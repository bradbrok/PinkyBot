"""Owner policy replacement and cross-store invalidation fail closed."""

import json
import time
from unittest.mock import AsyncMock

import pytest

from tests.slack_mention_only_support import (
    AGENT,
    CHANNEL,
    CHILD,
    OTHER,
    PEER,
    ROOT,
    configure,
    message,
    owner,
    required,
    signed,
    tag,
    tagged,
)
from tests.slack_mention_only_support import gateway as gateway

pytestmark = pytest.mark.real_auth


def path(agent=AGENT):
    return f"/agents/{agent}/slack/mention-only"


def put(g, channels, headers=None):
    return g.client.put(path(), headers=owner() if headers is None else headers,
                        json={"channels": channels})


def rows(g):
    return g.agents._db.execute(
        "SELECT channel_id, created_at, created_by FROM agent_slack_mention_only "
        "WHERE agent_name=? ORDER BY channel_id", (AGENT,),
    ).fetchall()


def test_owner_replace_deduplicates_and_audits_only_count(gateway):
    g = gateway
    response = put(g, [CHANNEL, OTHER, CHANNEL])
    assert response.status_code == 200, response.text
    assert required(g.agents, "get_slack_mention_only")(AGENT) == {CHANNEL, OTHER}
    stored = rows(g)
    assert len(stored) == 2 and all(row[1] > 0 and row[2].startswith("ui:") for row in stored)
    read = g.client.get(path(), headers=owner())
    assert read.status_code == 200, read.text
    assert read.json() == {"channels": [{"channel_id": row[0], "created_at": row[1]} for row in stored]}
    audit = g.app.state.audit._db.execute(
        "SELECT tool_input_summary FROM audit_log WHERE tool_name='owner_control'"
    ).fetchall()
    assert len(audit) == 1
    detail = json.loads(audit[0][0])
    assert detail == {"agent": AGENT, "actor": stored[0][2], "count": 2}
    assert CHANNEL not in audit[0][0] and OTHER not in audit[0][0]
    assert put(g, [OTHER]).status_code == 200
    assert required(g.agents, "get_slack_mention_only")(AGENT) == {OTHER}
    assert put(g, []).status_code == 200
    assert required(g.agents, "get_slack_mention_only")(AGENT) == set()


@pytest.mark.parametrize("agent", [AGENT, PEER])
def test_signed_agent_cannot_change_owner_policy(gateway, agent):
    g = gateway
    configure(g)
    before = rows(g)
    response = put(g, [], signed(g, "PUT", path(), agent))
    assert response.status_code == 403, response.text
    assert response.json()["detail"] == "authenticated owner session required"
    assert rows(g) == before


@pytest.mark.parametrize("agent,status", [(AGENT, 200), (PEER, 403)])
def test_get_is_self_scoped(gateway, agent, status):
    configure(gateway)
    response = gateway.client.get(path(), headers=signed(gateway, "GET", path(), agent))
    assert response.status_code == status, response.text
    if status == 200:
        assert response.json()["channels"][0]["channel_id"] == CHANNEL


@pytest.mark.parametrize("bad", ["D" + "0" * 9, "U" + "0" * 9, "Cshort", "C" + "0" * 21,
                                 "C" + "0" * 8 + "\n", "name", None, 1, True, {}, []])
def test_invalid_entry_is_400_and_entire_list_unchanged(gateway, bad):
    g = gateway
    configure(g)
    before = rows(g)
    response = put(g, [OTHER, bad])
    assert response.status_code == 400, response.text
    assert rows(g) == before


def test_more_than_fifty_entries_is_atomic_refusal(gateway):
    g = gateway
    configure(g)
    before = rows(g)
    response = put(g, ["C" + str(n).zfill(9) for n in range(51)])
    assert response.status_code == 400, response.text
    assert rows(g) == before


@pytest.mark.asyncio
async def test_remove_readd_purges_only_changed_channels(gateway, monkeypatch):
    g = gateway
    configure(g, [CHANNEL, OTHER])
    configure(g, [CHANNEL], PEER)
    tag(g)
    tag(g, channel=OTHER)
    tag(g, agent=PEER)
    assert put(g, [OTHER]).status_code == 200
    assert tagged(g) is None
    assert tagged(g, channel=OTHER) is not None and tagged(g, agent=PEER) is not None
    tag(g)  # A stale/orphan row must also be purged on newly adding a channel.
    assert put(g, [CHANNEL, OTHER]).status_code == 200
    assert tagged(g) is None
    assert tagged(g, channel=OTHER) is not None
    routed = AsyncMock()
    monkeypatch.setattr(g.broker, "_route_streaming", routed)
    await g.broker.handle_inbound(message(message_id=CHILD, metadata={"thread_ts": ROOT}))
    routed.assert_not_awaited()


def test_purge_failure_keeps_config_and_returns_500(gateway, monkeypatch):
    g = gateway
    configure(g)
    before = rows(g)
    purge = required(g.store, "purge_slack_tagged_threads")
    observed = []

    def fail(agent, channels=None):
        observed.append((agent, channels))
        assert rows(g) == before, "purge must precede config commit"
        raise RuntimeError("synthetic purge failure")

    monkeypatch.setattr(g.store, "purge_slack_tagged_threads", fail)
    assert put(g, [OTHER]).status_code == 500
    assert observed and rows(g) == before
    monkeypatch.setattr(g.store, "purge_slack_tagged_threads", purge)


def test_hard_delete_purges_policy_and_tagged_rows(gateway):
    g = gateway
    configure(g)
    configure(g, agent=PEER)
    tag(g)
    tag(g, agent=PEER)
    assert g.agents.delete(AGENT)
    assert rows(g) == [] and tagged(g) is None
    assert tagged(g, agent=PEER) is not None


def test_retention_expires_tagged_threads_and_fresh_tag_reopens(gateway):
    g = gateway
    configure(g)
    now = time.time()
    tag(g, at=now - 30 * 86400 - 1)
    tag(g, root=CHILD, at=now - 30 * 86400 + 1)
    g.store.sweep_retention(now=now)
    assert tagged(g) is None
    assert tagged(g, root=CHILD) is not None
    assert required(g.store, "is_tagged")(AGENT, CHANNEL, ROOT) is False
    required(g.store, "mark_tagged")(AGENT, CHANNEL, ROOT, CHILD)
    assert required(g.store, "is_tagged")(AGENT, CHANNEL, ROOT) is True
    assert tagged(g)[0] == CHILD


def test_registry_and_tag_store_contracts(gateway):
    g = gateway
    getter = required(g.agents, "get_slack_mention_only")
    setter = required(g.agents, "set_slack_mention_only")
    marker = required(g.store, "mark_tagged")
    query = required(g.store, "is_tagged")
    assert getter(AGENT) == set()
    setter(AGENT, [CHANNEL], "ui:fixture")
    marker(AGENT, CHANNEL, ROOT, ROOT)
    before = tuple(tagged(g))
    marker(AGENT, CHANNEL, ROOT, CHILD)
    assert tuple(tagged(g)) == before and query(AGENT, CHANNEL, ROOT)
    assert not query(PEER, CHANNEL, ROOT) and not query(AGENT, OTHER, ROOT)


def test_hard_delete_purge_failure_preserves_agent_and_policy(gateway, monkeypatch):
    g = gateway
    configure(g)
    before = rows(g)
    required(g.store, "purge_slack_tagged_threads")

    def fail(*args):
        raise RuntimeError("synthetic purge failure")

    monkeypatch.setattr(g.store, "purge_slack_tagged_threads", fail)
    with pytest.raises(RuntimeError, match="synthetic purge failure"):
        g.agents.delete(AGENT)
    assert g.agents.get(AGENT) is not None and rows(g) == before


def test_retire_preserves_policy_and_tag_control(gateway):
    g = gateway
    configure(g)
    tag(g)
    before = rows(g), tuple(tagged(g))
    assert g.agents.retire(AGENT)
    assert (rows(g), tuple(tagged(g))) == before


def test_policy_and_tags_survive_store_reopen(gateway):
    from pinky_daemon.agent_registry import AgentRegistry
    from pinky_daemon.message_context_store import MessageContextStore

    g = gateway
    configure(g)
    tag(g)
    registry = AgentRegistry(g.agents._db_path)
    store = MessageContextStore(g.store.db_path)
    try:
        assert required(registry, "get_slack_mention_only")(AGENT) == {CHANNEL}
        assert required(store, "is_tagged")(AGENT, CHANNEL, ROOT)
    finally:
        store.close()
        registry.close()


def test_hard_delete_purges_marks_and_notifies_after_commit(gateway, monkeypatch):
    import sqlite3

    g = gateway
    configure(g, [CHANNEL, OTHER])
    tag(g)
    tag(g, channel=OTHER)
    before = rows(g)
    purge = required(g.store, "purge_slack_tagged_threads")
    events = []

    def record_purge(agent, channels=None):
        events.append(("purge", agent, channels, g.agents._db.in_transaction,
                       g.agents.get(AGENT) is not None, rows(g)))
        return purge(agent, channels)

    def listener(changes):
        # Capture observations here; assertions outside the callback cannot be
        # swallowed by the registry's listener-exception isolation.
        connection = sqlite3.connect(g.agents._db_path)
        try:
            agent_rows = connection.execute(
                "SELECT name FROM agents WHERE name=?", (AGENT,),
            ).fetchall()
            policy_rows = connection.execute(
                "SELECT channel_id FROM agent_slack_mention_only WHERE agent_name=?", (AGENT,),
            ).fetchall()
            events.append(("listener", changes, g.agents._db.in_transaction,
                           agent_rows, policy_rows, tagged(g), tagged(g, channel=OTHER)))
        finally:
            connection.close()

    monkeypatch.setattr(g.store, "purge_slack_tagged_threads", record_purge)
    required(g.agents, "add_token_change_listener")(listener)
    assert g.agents.delete(AGENT)
    assert events == [
        ("purge", AGENT, None, False, True, before),
        ("listener", [(AGENT, "*")], False, [], [], None, None),
    ]
    assert g.agents.get(AGENT) is None and rows(g) == []
    assert tagged(g) is None and tagged(g, channel=OTHER) is None


def test_hard_delete_purge_failure_does_not_notify_listener(gateway, monkeypatch):
    g = gateway
    configure(g, [CHANNEL, OTHER])
    tag(g)
    tag(g, channel=OTHER)
    before = rows(g), tuple(tagged(g)), tuple(tagged(g, channel=OTHER))
    required(g.store, "purge_slack_tagged_threads")
    purges = []
    notifications = []

    def fail(agent, channels=None):
        purges.append((agent, channels, g.agents._db.in_transaction))
        raise RuntimeError("synthetic purge failure")

    monkeypatch.setattr(g.store, "purge_slack_tagged_threads", fail)
    required(g.agents, "add_token_change_listener")(notifications.append)
    with pytest.raises(RuntimeError, match="synthetic purge failure"):
        g.agents.delete(AGENT)
    assert purges == [(AGENT, None, False)]
    assert notifications == []
    assert g.agents.get(AGENT) is not None
    assert (rows(g), tuple(tagged(g)), tuple(tagged(g, channel=OTHER))) == before
    assert not g.agents._db.in_transaction
