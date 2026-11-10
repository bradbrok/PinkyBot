"""Ownership snapshots read only the related names and preserve opaque evidence."""

import logging
from unittest.mock import Mock

import pytest

from pinky_daemon.transport import SessionState
from tests.isolated_policy_support import closure
from tests.isolated_policy_support import daemon as daemon
from tests.test_session_owner_reads import _collect, _read, _register
from tests.test_streaming_session_owner_reads import streaming_session

pytestmark = pytest.mark.real_auth


def test_no_family_adds_no_broker_or_label_lookup(daemon, monkeypatch):
    instance = daemon()

    class UnusedBrokerMap(dict):
        def get(self, *args):
            raise AssertionError("Unrelated names need no broker lookup")

    monkeypatch.setattr(instance.app.state.broker, "_streaming", UnusedBrokerMap())
    label_read = Mock(wraps=instance.agents.list_streaming_session_labels)
    recorded_read = Mock(wraps=instance.agents.list_streaming_session_ids)
    names_read = Mock(wraps=instance.agents.list)
    monkeypatch.setattr(instance.agents, "list_streaming_session_labels", label_read)
    monkeypatch.setattr(instance.agents, "list_streaming_session_ids", recorded_read)
    monkeypatch.setattr(instance.agents, "list", names_read)
    snapshot = closure(instance.app, "_agent_session_ownership")("tenant")
    assert snapshot.keeps("tenant-main")
    assert label_read.call_count == 0
    assert recorded_read.call_count == names_read.call_count == 1
    assert names_read.call_args.kwargs == {"include_retired": True}


def test_family_lookup_reads_both_directions_and_no_unrelated_bucket(daemon, monkeypatch):
    instance = daemon()
    for name in ["tenant-peer", "tenant-peer-child"]:
        _register(instance, name)
    assert instance.agents.retire("tenant-peer-child")
    calls = []
    family = {"tenant", "tenant-peer", "tenant-peer-child"}

    class FamilyOnlyMap(dict):
        def get(self, name, default=None):
            assert name in family
            calls.append(name)
            return super().get(name, default)

    monkeypatch.setattr(instance.app.state.broker, "_streaming", FamilyOnlyMap())
    labels = Mock(wraps=instance.agents.list_streaming_session_labels)
    monkeypatch.setattr(instance.agents, "list_streaming_session_labels", labels)
    closure(instance.app, "_agent_session_ownership")("tenant-peer")
    assert set(calls) == family and len(calls) == len(family)
    labels.assert_called_once_with(frozenset(family))


@pytest.mark.parametrize("state", list(SessionState))
def test_registered_broker_evidence_is_independent_of_session_state(daemon, state):
    instance = daemon()
    _register(instance, "tenant-peer")
    session = streaming_session(instance, "tenant", "peer-main", connected=False)
    session._state_machine._state = state
    snapshot = closure(instance.app, "_agent_session_ownership")
    assert snapshot("tenant").keeps(session.id)
    assert not snapshot("tenant-peer").keeps(session.id)


@pytest.mark.parametrize("sid", ["opaque-resume", "tenant-peer-main"])
def test_recorded_handles_alone_keep_the_previous_noncollision_behavior(daemon, caplog, sid):
    instance = daemon()
    _register(instance, "tenant-peer")
    for owner in ["tenant", "tenant-peer"]:
        instance.agents.set_streaming_session_id(owner, sid, label="notes")
    instance.app.state.conversation_store.append(sid, "user", "needle opaque fixture")
    caplog.set_level(logging.WARNING, logger="pinky_daemon.session_identity")
    for owner in ["tenant", "tenant-peer"]:
        assert sid in _collect(instance, owner)
        for query in ["", "?q=needle"]:
            response = _read(instance, "/agents/" + owner + "/chat-history" + query, name=owner)
            assert [row["session_id"] for row in response.json()["messages"]] == [sid]
    assert not [row for row in caplog.records if row.name == "pinky_daemon.session_identity"]


def test_label_evidence_query_is_a_plain_read_and_keeps_empty_handle_rows(daemon):
    instance = daemon()
    instance.agents.set_streaming_session_id("tenant", "", label="notes")
    instance.agents.set_streaming_session_id("peer", "opaque-other", label="foreign")
    database = instance.agents._db
    changes = database.total_changes
    statements = []
    database.set_trace_callback(statements.append)
    try:
        assert instance.agents.list_streaming_session_labels(["tenant"]) == [
            {"agent_name": "tenant", "label": "notes"}
        ]
        assert instance.agents.list_streaming_session_labels([]) == []
    finally:
        database.set_trace_callback(None)
    assert database.total_changes == changes
    assert len(statements) == 1 and statements[0].lstrip().upper().startswith("SELECT ")
