"""A rename refuses when the old or new session id has another claimant."""

import inspect
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from pinky_daemon.auth import SESSION_COOKIE_NAME, create_session_cookie
from pinky_daemon.session_identity import session_id_for_agent
from tests.conftest import TEST_SESSION_SECRET
from tests.isolated_policy_support import closure, replace_cell, signed
from tests.isolated_policy_support import daemon as daemon
from tests.test_session_owner_reads import _read, _register
from tests.test_streaming_session_owner_reads import streaming_session

pytestmark = pytest.mark.real_auth


def _rename_function(instance):
    return inspect.unwrap(closure(instance.app, "rename_streaming_session"))


def _rename_cells(instance):
    return inspect.getclosurevars(_rename_function(instance)).nonlocals


async def _own_session(instance, label):
    session = streaming_session(instance, "tenant", label)
    instance.agents.set_streaming_session_id("tenant", session.resume_handle, label=label)
    cells = _rename_cells(instance)
    key = ("tenant", label)
    cells["_resume_owners"][key] = session
    cells["_resume_epochs"][key] = object()
    session._on_resume_handle = await cells["_make_streaming_resume_handle_callback"](
        "tenant", label, owner=session,
    )
    session._on_resume_handle_sync = cells["_make_streaming_resume_handle_sync_callback"](
        "tenant", label, owner=session,
    )
    return session


def _patch(instance, label, new_label, principal="tenant"):
    path = "/agents/tenant/streaming-sessions/" + label
    client = TestClient(instance.app)
    if principal == "owner":
        client.cookies.set(SESSION_COOKIE_NAME, create_session_cookie(TEST_SESSION_SECRET))
    try:
        return client.patch(
            path,
            json={"label": new_label},
            headers={} if principal == "owner" else signed(instance, "PATCH", path, principal),
        )
    finally:
        client.close()


def _rows(store, session_id):
    return [row.to_dict() for row in store.get_history(session_id, limit=100)]


def _state(instance, session, old_id, new_id):
    broker = instance.app.state.broker._streaming
    cells = _rename_cells(instance)
    store = instance.app.state.conversation_store
    fts_tables = (
        "messages_fts", "messages_fts_data", "messages_fts_idx",
        "messages_fts_docsize", "messages_fts_config",
    )
    return {
        "broker_root": broker,
        "broker_buckets": broker.copy(),
        "broker_entries": {name: bucket.copy() for name, bucket in broker.items()},
        "label": session._config.label,
        "id": session.id,
        "resume_owners": cells["_resume_owners"].copy(),
        "resume_epochs": cells["_resume_epochs"].copy(),
        "async_callback": session._on_resume_handle,
        "sync_callback": session._on_resume_handle_sync,
        "labels": [tuple(row) for row in instance.agents._db.execute(
            "SELECT agent_name, label, session_id, updated_at "
            "FROM streaming_session_labels ORDER BY agent_name, label"
        )],
        "main_ids": [tuple(row) for row in instance.agents._db.execute(
            "SELECT name, streaming_session_id FROM agents ORDER BY name"
        )],
        "old_rows": _rows(store, old_id),
        "new_rows": _rows(store, new_id),
        "fts": {table: [tuple(row) for row in store._conn.execute(
            "SELECT * FROM " + table + " ORDER BY 1"
        )] for table in fts_tables},
        "fts_search": [row.to_dict() for row in store.search("renameclaimneedle", limit=100)],
    }


def _assert_unchanged(instance, session, old_id, new_id, before):
    after = _state(instance, session, old_id, new_id)
    assert after["broker_root"] is before["broker_root"], "broker map replaced"
    assert after["broker_entries"] == before["broker_entries"], "broker entries changed"
    assert after["broker_buckets"].keys() == before["broker_buckets"].keys()
    for name, bucket in before["broker_buckets"].items():
        assert after["broker_buckets"][name] is bucket, "broker bucket replaced"
    assert after["label"] == before["label"], "config label changed"
    assert after["id"] == before["id"], "live session id changed"
    assert after["resume_owners"] == before["resume_owners"], "resume owners changed"
    assert after["resume_epochs"] == before["resume_epochs"], "resume epochs changed"
    assert after["async_callback"] is before["async_callback"], "async callback replaced"
    assert after["sync_callback"] is before["sync_callback"], "sync callback replaced"
    assert after["labels"] == before["labels"], "registry label rows changed"
    assert after["main_ids"] == before["main_ids"], "registry main ids changed"
    assert after["old_rows"] == before["old_rows"], "old conversation rows changed"
    assert after["new_rows"] == before["new_rows"], "new conversation rows changed"
    assert after["fts"] == before["fts"], "FTS tables changed"
    assert after["fts_search"] == before["fts_search"], "FTS results changed"


def _assert_claim_refusal(response, label):
    assert response.status_code == 409, response.text
    assert response.json() == {
        "detail": f"Session label '{label}' cannot be renamed because another session claim exists."
    }


@pytest.mark.parametrize("query", ["", "?q=renameclaimneedle"], ids=["plain", "q"])
@pytest.mark.parametrize("mode", ["enforce", "shadow", "off"])
async def test_rename_preserves_rows_and_state_when_both_sessions_claim_the_old_id(
    daemon, monkeypatch, mode, query,
):
    instance = daemon(mode)
    _register(instance, "tenant-peer")
    monkeypatch.setenv("PINKY_ISOLATED_READ_POLICY_MODE", "enforce")
    session = await _own_session(instance, "peer-main")
    other = streaming_session(instance, "tenant-peer", "main")
    instance.agents.set_streaming_session_id("tenant-peer", other.resume_handle, label="main")
    for writer in (session, other):
        assert await writer.send("renameclaimneedle row " + writer.agent_name) is True
        writer._client.query.assert_awaited_once()
    assert instance.app.state.manager.list() == []
    old_id, new_id = session.id, "tenant-after-rename"
    assert instance.app.state.conversation_store.count(old_id) == 2
    assert instance.app.state.conversation_store.count(new_id) == 0
    for owner in ("tenant", "tenant-peer"):
        response = _read(instance, "/agents/" + owner + "/chat-history" + query, name=owner)
        assert response.status_code == 200
        assert response.json()["messages"] == []
    before = _state(instance, session, old_id, new_id)
    response = _patch(instance, "peer-main", "after-rename")
    _assert_unchanged(instance, session, old_id, new_id, before)
    if mode == "enforce":
        assert response.status_code == 403, response.text
    else:
        _assert_claim_refusal(response, "peer-main")
    for owner in ("tenant", "tenant-peer"):
        response = _read(instance, "/agents/" + owner + "/chat-history" + query, name=owner)
        assert response.status_code == 200
        assert response.json()["messages"] == []


async def test_rename_refuses_a_claim_on_the_new_id_and_preserves_both_counts(daemon):
    instance = daemon("off")
    _register(instance, "tenant-peer")
    session = await _own_session(instance, "notes")
    other = streaming_session(instance, "tenant-peer", "main")
    instance.agents.set_streaming_session_id("tenant-peer", other.resume_handle, label="main")
    for writer in (session, other):
        assert await writer.send("renameclaimneedle row " + writer.agent_name) is True
        writer._client.query.assert_awaited_once()
    old_id, new_id = session.id, other.id
    store = instance.app.state.conversation_store
    assert (store.count(old_id), store.count(new_id)) == (1, 1)
    before = _state(instance, session, old_id, new_id)
    response = _patch(instance, "notes", "peer-main")
    _assert_unchanged(instance, session, old_id, new_id, before)
    assert (store.count(old_id), store.count(new_id)) == (1, 1)
    _assert_claim_refusal(response, "notes")


@pytest.mark.parametrize("position", ["old", "new"])
@pytest.mark.parametrize(
    "kind", ["legacy-live", "broker", "label", "recorded", "retired-recorded", "legacy-recorded"],
)
async def test_each_explicit_claim_kind_refuses_on_either_id(daemon, position, kind):
    instance = daemon("off")
    session = await _own_session(instance, "peer-old")
    old_id, new_id = session.id, "tenant-peer-new"
    target = old_id if position == "old" else new_id
    other_label = position
    if kind == "legacy-live":
        instance.app.state.manager.create(
            session_id=target, agent_name="peer", working_dir=str(instance.root / "peer"),
        )
    else:
        _register(instance, "tenant-peer")
        if kind == "broker":
            streaming_session(instance, "tenant-peer", other_label, connected=False)
        elif kind == "label":
            instance.agents.set_streaming_session_id("tenant-peer", "", label=other_label)
        elif kind == "legacy-recorded":
            instance.agents._db.execute(
                "UPDATE agents SET streaming_session_id=? WHERE name=?", (target, "tenant-peer"),
            )
            instance.agents._db.commit()
        else:
            instance.agents.set_streaming_session_id("tenant-peer", target, label="recorded")
            if kind == "retired-recorded":
                assert instance.agents.retire("tenant-peer") is True
    store = instance.app.state.conversation_store
    store.append(old_id, "user", "renameclaimneedle old fixture", timestamp=11)
    store.append(new_id, "assistant", "renameclaimneedle new fixture", timestamp=12)
    before = _state(instance, session, old_id, new_id)
    response = _patch(instance, "peer-old", "peer-new")
    _assert_unchanged(instance, session, old_id, new_id, before)
    _assert_claim_refusal(response, "peer-old")


@pytest.mark.parametrize("scenario", ["no-family", "family-unclaimed", "own-evidence"])
async def test_unclaimed_rename_preserves_the_existing_success_contract(daemon, scenario):
    instance = daemon("off")
    if scenario != "no-family":
        _register(instance, "tenant-peer")
    old_label, new_label = ("peer-old", "peer-new") if scenario == "own-evidence" else ("old", "new")
    session = await _own_session(instance, old_label)
    if scenario == "own-evidence":
        instance.agents.set_streaming_session_id("tenant", "", label=new_label)
    assert await session.send("renameclaimneedle own row") is True
    session._client.query.assert_awaited_once()
    store = instance.app.state.conversation_store
    before = _rows(store, session.id)
    old_id = session.id
    new_id = session_id_for_agent("tenant", new_label)
    response = _patch(instance, old_label, new_label)
    assert response.status_code == 200, response.text
    assert response.json() == {
        "renamed": True, "agent": "tenant", "old_label": old_label, "new_label": new_label,
    }
    assert store.count(old_id) == 0
    assert _rows(store, new_id) == [dict(row, session_id=new_id) for row in before]
    assert session.id == new_id and session._config.label == new_label
    assert instance.app.state.broker.get_streaming_session("tenant", old_label) is None
    assert instance.app.state.broker.get_streaming_session("tenant", new_label) is session
    assert instance.agents.get_streaming_session_id("tenant", old_label) == ""
    assert instance.agents.get_streaming_session_id("tenant", new_label) == session.resume_handle
    assert [row.session_id for row in store.search("renameclaimneedle")] == [new_id]


@pytest.mark.parametrize("mode", ["enforce", "shadow", "off"])
@pytest.mark.parametrize("principal", ["tenant", "normal", "owner"])
async def test_claim_refusal_is_independent_of_the_allowed_principal(daemon, mode, principal):
    instance = daemon(mode)
    _register(instance, "tenant-peer")
    session = await _own_session(instance, "peer-old")
    instance.agents.set_streaming_session_id("tenant-peer", "", label="old")
    old_id, new_id = session.id, "tenant-after-rename"
    instance.app.state.conversation_store.append(old_id, "user", "renameclaimneedle matrix row")
    before = _state(instance, session, old_id, new_id)
    response = _patch(instance, "peer-old", "after-rename", principal=principal)
    _assert_unchanged(instance, session, old_id, new_id, before)
    if principal == "tenant" and mode == "enforce":
        assert response.status_code == 403, response.text
    else:
        _assert_claim_refusal(response, "peer-old")


@pytest.mark.parametrize("mode", ["enforce", "shadow", "off"])
@pytest.mark.parametrize("principal", ["tenant", "normal", "owner"])
@pytest.mark.parametrize("target", ["tenant", "tenant-peer"])
def test_label_create_and_rename_principal_matrix(daemon, monkeypatch, mode, principal, target):
    instance = daemon(mode)
    work = instance.root / "tenant-peer"
    work.mkdir()
    instance.agents.register("tenant-peer", isolated=False, working_dir=str(work))
    instance.agents.set_main_agent("tenant-peer")
    calls = []

    async def fixture_start(name, *, label="main", resume_id=""):
        calls.append((name, label))
        return streaming_session(instance, name, label, connected=False)

    replace_cell(
        monkeypatch, inspect.unwrap(closure(instance.app, "create_streaming_session")),
        "_start_streaming_session", fixture_start,
    )
    base = "/agents/" + target + "/streaming-sessions"
    client = TestClient(instance.app)
    if principal == "owner":
        client.cookies.set(SESSION_COOKIE_NAME, create_session_cookie(TEST_SESSION_SECRET))

    def request(method, path, **kwargs):
        headers = {} if principal == "owner" else signed(
            instance, method, path.split("?", 1)[0], principal,
        )
        return client.request(method, path, headers=headers, **kwargs)

    allowed = principal != "tenant" or (target == "tenant" and mode != "enforce")
    try:
        created = request("POST", base + "?label=part-old")
        assert created.status_code == (200 if allowed else 403), created.text
        if not allowed:
            assert not calls
            streaming_session(instance, target, "part-old", connected=False)
        renamed = request("PATCH", base + "/part-old", json={"label": "part-new"})
        assert renamed.status_code == (200 if allowed else 403), renamed.text
        assert (instance.app.state.broker.get_streaming_session(target, "part-new") is not None) is allowed
        assert instance.agents.get_main_agent() == "tenant-peer"
    finally:
        client.close()


@pytest.mark.parametrize("flag", ["PINKY_SESSION_CLASS_REBUILD", "PINKY_RESUME_FAILSAFE"])
async def test_claim_refusal_precedes_startup_settle(daemon, monkeypatch, flag):
    instance = daemon("off")
    _register(instance, "tenant-peer")
    monkeypatch.setenv(flag, "1")
    session = await _own_session(instance, "peer-old")
    instance.agents.set_streaming_session_id("tenant-peer", "", label="old")
    cells = _rename_cells(instance)
    cells["_resume_owners"][("tenant", "after-rename")] = object()
    cells["_resume_epochs"][("tenant", "after-rename")] = object()
    settle = AsyncMock(wraps=cells["_settle_startup_debt"])
    replace_cell(monkeypatch, _rename_function(instance), "_settle_startup_debt", settle)
    old_id, new_id = session.id, "tenant-after-rename"
    instance.app.state.conversation_store.append(old_id, "user", "renameclaimneedle fenced row")
    before = _state(instance, session, old_id, new_id)
    response = _patch(instance, "peer-old", "after-rename")
    settle.assert_not_awaited()
    _assert_unchanged(instance, session, old_id, new_id, before)
    _assert_claim_refusal(response, "peer-old")


async def test_new_id_text_without_an_explicit_claim_keeps_the_existing_rename(daemon):
    instance = daemon("off")
    _register(instance, "tenant-peer")
    session = await _own_session(instance, "notes")
    assert await session.send("renameclaimneedle text control") is True
    store = instance.app.state.conversation_store
    before = _rows(store, session.id)
    response = _patch(instance, "notes", "peer-unused")
    assert response.status_code == 200, response.text
    assert response.json() == {
        "renamed": True, "agent": "tenant", "old_label": "notes", "new_label": "peer-unused",
    }
    assert store.count("tenant-notes") == 0
    assert _rows(store, "tenant-peer-unused") == [
        dict(row, session_id="tenant-peer-unused") for row in before
    ]


@pytest.mark.parametrize(
    "label,new_label,status,detail",
    [
        ("peer-old", "", 400, "New label is required"),
        ("main", "fresh", 400, "Cannot rename the main session"),
        ("missing", "fresh", 404, "Streaming session 'missing' not found for tenant"),
        ("peer-old", "peer-old", 409, "Session 'peer-old' already exists for tenant"),
        ("peer-old", "occupied", 409, "Session 'occupied' already exists for tenant"),
    ],
)
async def test_existing_validation_precedes_claim_checks(
    daemon, monkeypatch, label, new_label, status, detail,
):
    instance = daemon("off")
    _register(instance, "tenant-peer")
    session = await _own_session(instance, "peer-old")
    streaming_session(instance, "tenant", "occupied", connected=False)
    instance.agents.set_streaming_session_id("tenant-peer", "", label="old")
    ownership = closure(instance.app, "_agent_session_ownership")
    calls = []

    def observe(*args, **kwargs):
        calls.append((args, kwargs))
        return ownership(*args, **kwargs)

    # The base has no ownership dependency in this handler yet.
    if "_agent_session_ownership" in _rename_cells(instance):
        replace_cell(monkeypatch, _rename_function(instance), "_agent_session_ownership", observe)
    before = _state(instance, session, session.id, "tenant-fresh")
    response = _patch(instance, label, new_label)
    assert response.status_code == status, response.text
    assert response.json() == {"detail": detail}
    assert calls == []
    _assert_unchanged(instance, session, session.id, "tenant-fresh", before)
