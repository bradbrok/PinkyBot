"""Exact recorded claims and the rename lookup budget."""

import sqlite3
from unittest.mock import Mock, call

import pytest

from pinky_daemon.agent_registry import AgentRegistry
from tests.isolated_policy_support import closure, replace_cell
from tests.isolated_policy_support import daemon as daemon
from tests.test_label_rename_claims import _claim_decision, _own_session, _patch, _rows
from tests.test_session_owner_reads import _register

pytestmark = pytest.mark.real_auth


@pytest.fixture
def registry(tmp_path):
    instance = AgentRegistry(db_path=str(tmp_path / "agents.db"))
    for name in ("unit", "unit-peer", "other"):
        work = tmp_path / name
        work.mkdir()
        instance.register(name, working_dir=str(work))
    try:
        yield instance
    finally:
        instance.close()


@pytest.mark.parametrize("legacy", [False, True], ids=["label-value", "legacy-value"])
@pytest.mark.parametrize("retired", [False, True], ids=["active", "retired"])
def test_recorded_claims_use_exact_names_and_id_values(registry, legacy, retired):
    if legacy:
        registry._db.execute(
            "UPDATE agents SET streaming_session_id=? WHERE name=?", ("unit-main", "unit-peer"),
        )
        registry._db.commit()
    else:
        registry.set_streaming_session_id("unit-peer", "unit-main", label="recorded")
    if retired:
        assert registry.retire("unit-peer") is True
    assert registry.has_streaming_session_id_claim({"unit-peer"}, {"unit-main"}) is True
    assert registry.has_streaming_session_id_claim({"unit-peer"}, {"unit-main-extra"}) is False
    assert registry.has_streaming_session_id_claim({"unit-peer"}, {"UNIT-main"}) is False
    assert registry.has_streaming_session_id_claim({"unit"}, {"unit-main"}) is False
    assert registry.has_streaming_session_id_claim({"other"}, {"unit-main"}) is False
    assert registry.has_streaming_session_id_claim({"unit-peer"}, {"unit-peer-recorded"}) is False


def test_recorded_claim_read_is_one_select_with_no_schema_or_row_changes(registry):
    registry.set_streaming_session_id("unit-peer", "unit-old", label="recorded")
    registry.set_streaming_session_id("other", "unit-new", label="recorded")
    db = registry._db
    schema = [tuple(row) for row in db.execute("SELECT * FROM sqlite_master ORDER BY name")]
    changes = db.total_changes
    statements = []
    db.set_trace_callback(statements.append)
    try:
        assert registry.has_streaming_session_id_claim({"unit-peer"}, {"unit-old", "unit-new"})
        assert not registry.has_streaming_session_id_claim(set(), {"unit-old"})
        assert not registry.has_streaming_session_id_claim({"unit-peer"}, set())
    finally:
        db.set_trace_callback(None)
    assert len(statements) == 1 and statements[0].lstrip().upper().startswith("SELECT ")
    assert db.total_changes == changes
    assert [tuple(row) for row in db.execute("SELECT * FROM sqlite_master ORDER BY name")] == schema


def test_empty_recorded_values_are_not_claims(registry):
    registry.set_streaming_session_id("unit-peer", "", label="main")
    assert registry.has_streaming_session_id_claim({"unit-peer"}, {""}) is False


def test_recorded_claim_collections_have_a_bounded_parameter_count(registry):
    registry.set_streaming_session_id("unit-peer", "unit-old", label="recorded")
    names = ["unit-peer", *("unit-" + str(i) for i in range(1500))]
    ids = ["unit-old", *("id-" + str(i) for i in range(1500))]
    db = registry._db
    previous = db.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 64)
    try:
        assert registry.has_streaming_session_id_claim(names, ids) is True
        assert registry.has_streaming_session_id_claim(names, ids[1:]) is False
    finally:
        db.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, previous)


@pytest.mark.parametrize("family", [False, True], ids=["no-family", "family"])
@pytest.mark.parametrize("fencing", [False, True], ids=["fencing-off", "fencing-on"])
async def test_rename_takes_one_snapshot_per_check_and_only_a_family_needs_a_claim_read(
    daemon, monkeypatch, family, fencing,
):
    instance = daemon("off")
    monkeypatch.setenv("PINKY_RESUME_FAILSAFE", "1" if fencing else "0")
    monkeypatch.setenv("PINKY_SESSION_CLASS_REBUILD", "0")
    if family:
        _register(instance, "tenant-peer")
    await _own_session(instance, "notes")
    snapshot = Mock(wraps=closure(instance.app, "_agent_session_ownership"))
    claim_read = Mock(wraps=instance.agents.has_streaming_session_id_claim)
    replace_cell(monkeypatch, _claim_decision(instance), "_agent_session_ownership", snapshot)
    monkeypatch.setattr(instance.agents, "has_streaming_session_id_claim", claim_read)
    response = _patch(instance, "notes", "renamed")
    assert response.status_code == 200, response.text
    checks = 2 if fencing else 1
    assert snapshot.call_args_list == [call("tenant")] * checks
    if family:
        assert claim_read.call_args_list == [
            call(frozenset({"tenant-peer"}), ("tenant-notes", "tenant-renamed")),
        ] * checks
    else:
        claim_read.assert_not_called()


@pytest.mark.parametrize("position", ["old", "new"])
async def test_recorded_value_outside_the_family_does_not_refuse_a_rename(daemon, position):
    instance = daemon("off")
    _register(instance, "tenant-peer")
    session = await _own_session(instance, "notes")
    old_id, new_id = session.id, "tenant-renamed"
    instance.agents.set_streaming_session_id(
        "peer", old_id if position == "old" else new_id, label="recorded",
    )
    store = instance.app.state.conversation_store
    store.append(old_id, "user", "renameclaimneedle outside-family control", timestamp=15)
    before = _rows(store, old_id)
    response = _patch(instance, "notes", "renamed")
    assert response.status_code == 200, response.text
    assert response.json() == {
        "renamed": True, "agent": "tenant", "old_label": "notes", "new_label": "renamed",
    }
    assert store.count(old_id) == 0
    assert _rows(store, new_id) == [dict(row, session_id=new_id) for row in before]


@pytest.mark.parametrize("position", ["old", "new"])
async def test_own_legacy_claim_allows_a_rename(daemon, position):
    instance = daemon("off")
    session = await _own_session(instance, "notes")
    instance.app.state.manager.create(
        session_id=session.id if position == "old" else "tenant-renamed",
        agent_name="tenant", working_dir=str(instance.root / "tenant"),
    )
    store = instance.app.state.conversation_store
    store.append(session.id, "user", "renameclaimneedle own legacy control", timestamp=16)
    response = _patch(instance, "notes", "renamed")
    assert response.status_code == 200, response.text
    assert store.count("tenant-notes") == 0
    assert store.count("tenant-renamed") == 1
