"""All read paths filter the same broker and recorded-label evidence."""

import logging

import pytest

from pinky_daemon.autonomy import AutonomyEngine
from tests.isolated_policy_support import closure
from tests.isolated_policy_support import daemon as daemon
from tests.test_session_owner_reads import (
    _collect,
    _last_message,
    _owner_post,
    _read,
    _refresh_calls,
    _register,
)
from tests.test_streaming_session_owner_reads import streaming_session

pytestmark = pytest.mark.real_auth
SITES = ["sessions", "collect", "history", "search", "refresh", "last"]


def observed_ids(instance, monkeypatch, caller, site, *, expected_last):
    path = "/agents/" + caller
    if site == "collect":
        return _collect(instance, caller)
    if site == "last":
        assert _last_message(instance, caller) == expected_last
        return None
    if site == "refresh":
        calls = _refresh_calls(instance, monkeypatch)
        response = _owner_post(instance, path + "/sessions/refresh")
        assert response.status_code == 200
        assert set(response.json()["refreshed_sessions"]) == set(calls)
        return set(calls)
    suffix = "/sessions" if site == "sessions" else "/chat-history"
    if site == "search":
        suffix += "?q=needle"
    response = _read(instance, path + suffix, name="normal")
    assert response.status_code == 200
    field = "sessions" if site == "sessions" else "messages"
    key = "id" if site == "sessions" else "session_id"
    return {row[key] for row in response.json()[field]}


@pytest.mark.parametrize(
    "short,long,retired",
    [
        ("broker", "", False),
        ("label", "", False),
        ("label", "label", False),
        ("broker", "label", False),
        ("label", "broker", False),
        ("", "broker", False),
        ("", "label", True),
        ("label", "label", True),
        ("legacy", "broker", False),
        ("label", "legacy", False),
    ],
    ids=["broker-short", "label-short", "label-collision", "broker-label-collision",
         "label-broker-collision", "broker-long", "retired-long", "retired-collision",
         "legacy-broker-collision", "label-legacy-collision"],
)
@pytest.mark.parametrize("caller", ["tenant", "tenant-peer"])
@pytest.mark.parametrize("site", SITES)
def test_prefix_family_evidence_reaches_every_read_path(
    daemon, monkeypatch, short, long, retired, caller, site
):
    instance = daemon()
    _register(instance, "tenant-peer")
    store, manager = instance.app.state.conversation_store, instance.app.state.manager
    sid = "tenant-peer-main"
    legacy_owner = ""
    for owner, label, evidence in [
        ("tenant", "peer-main", short), ("tenant-peer", "main", long)
    ]:
        if evidence == "broker":
            # Registration, including a disconnected session, is the evidence.
            streaming_session(instance, owner, label, connected=False)
        elif evidence == "label":
            instance.agents.set_streaming_session_id(owner, "opaque-" + owner, label=label)
        elif evidence == "legacy":
            legacy_owner = owner
    manager.create(session_id=sid, agent_name=legacy_owner, working_dir="fixture-work")
    store.append(sid, "user", "needle ambiguous fixture", timestamp=99)
    for owner, timestamp in [("tenant", 10), ("tenant-peer", 20)]:
        manager.create(session_id=owner + "-own", agent_name=owner, working_dir="fixture-work")
        store.append(owner + "-own", "user", "needle distinct " + owner, timestamp=timestamp)
    if retired:
        assert instance.agents.retire("tenant-peer")
    claimant = "tenant" if short and not long else "tenant-peer" if long and not short else ""
    owns = caller == claimant
    actual = observed_ids(
        instance, monkeypatch, caller, site,
        expected_last=99 if owns else 10 if caller == "tenant" else 20,
    )
    if actual is not None:
        assert caller + "-own" in actual
        assert (sid in actual) is owns
        assert ("tenant-peer-own" if caller == "tenant" else "tenant-own") not in actual


def test_registry_only_autonomy_uses_label_aliases_and_hides_collisions(daemon):
    instance = daemon()
    _register(instance, "tenant-peer")
    store = instance.app.state.conversation_store
    store.append("tenant-main", "user", "short own", timestamp=10)
    store.append("tenant-peer-own", "user", "long own", timestamp=20)
    store.append("tenant-peer-main", "user", "ambiguous", timestamp=99)
    instance.agents.set_streaming_session_id("tenant", "opaque-short", label="peer-main")
    engine = AutonomyEngine(instance.agents, None, store)
    assert engine._last_message_time("tenant") == 99
    assert engine._last_message_time("tenant-peer") == 20
    instance.agents.set_streaming_session_id("tenant-peer", "opaque-long", label="main")
    assert engine._last_message_time("tenant") == 10
    assert engine._last_message_time("tenant-peer") == 20


def test_collision_warning_is_once_per_id_across_snapshots(daemon, tmp_path, caplog):
    instance = daemon()
    _register(instance, "tenant-peer")
    label = "warning-" + tmp_path.name
    short = streaming_session(instance, "tenant", "peer-" + label, connected=False)
    streaming_session(instance, "tenant-peer", label, connected=False)
    caplog.set_level(logging.WARNING, logger="pinky_daemon.session_identity")
    snapshot = closure(instance.app, "_agent_session_ownership")
    for _ in range(3):
        for caller in ["tenant", "tenant-peer"]:
            assert not snapshot(caller).keeps(short.id)
    rows = [row for row in caplog.records if row.name == "pinky_daemon.session_identity"]
    assert len(rows) == 1
    text = rows[0].getMessage()
    assert short.id in text and "tenant" in text and "tenant-peer" in text
    assert "opaque-" not in text and "message" not in text


@pytest.mark.parametrize("caller", ["tenant", "peer"])
@pytest.mark.parametrize("site", ["sessions", "history", "search", "last"])
def test_unrelated_legacy_owner_keeps_precedence_over_a_broker_alias(
    daemon, monkeypatch, caplog, caller, site
):
    instance = daemon()
    manager, store = instance.app.state.manager, instance.app.state.conversation_store
    manager.create(session_id="tenant-main", agent_name="peer", working_dir="fixture-work")
    manager.create(session_id="tenant-own", agent_name="tenant", working_dir="fixture-work")
    streaming_session(instance, "tenant", "main", connected=False)
    store.append("tenant-main", "user", "needle peer-owned", timestamp=99)
    store.append("tenant-own", "user", "needle short-owned", timestamp=10)
    caplog.set_level(logging.WARNING, logger="pinky_daemon.session_identity")
    actual = observed_ids(
        instance, monkeypatch, caller, site,
        expected_last=10 if caller == "tenant" else None,
    )
    if actual is not None:
        assert actual == ({"tenant-own"} if caller == "tenant" else {"tenant-main"})
    assert not [row for row in caplog.records if row.name == "pinky_daemon.session_identity"]


def test_empty_label_row_is_evidence_without_an_opaque_handle(daemon):
    instance = daemon()
    _register(instance, "tenant-peer")
    instance.agents.set_streaming_session_id("tenant", "", label="peer-main")
    store = instance.app.state.conversation_store
    store.append("tenant-peer-main", "user", "needle empty-handle fixture", timestamp=99)
    assert _last_message(instance, "tenant") == 99
    assert _last_message(instance, "tenant-peer") is None
