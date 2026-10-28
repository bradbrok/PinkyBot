"""History session discovery stays scoped, indexed and shared within a request."""

from __future__ import annotations

import inspect
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from pinky_daemon.api import create_api
from pinky_daemon.conversation_store import ConversationStore
from pinky_daemon.routes import memory


@pytest.fixture
def history_app(tmp_path, monkeypatch):
    # Restore router bindings after each app so other files retain their own app.
    for name in ("_agents", "_store", "_collect_agent_session_ids", "_resolve_agent_history"):
        monkeypatch.setattr(memory, name, getattr(memory, name))
    app = create_api(default_working_dir=str(tmp_path), db_path=str(tmp_path / "api.db"))
    for name in ("unit", "unit-x", "unitary"):
        app.state.agents.register(name, working_dir=str(tmp_path / name))
    app.state.manager.create(
        session_id="opaque-active", agent_name="unit", working_dir=str(tmp_path),
    )
    app.state.manager.create(
        session_id="unit-active", agent_name="unitary", working_dir=str(tmp_path),
    )
    app.state.manager.create(
        session_id="unitary-active", agent_name="unitary", working_dir=str(tmp_path),
    )
    app.state.agents.set_streaming_session_id("unit", "opaque-stream", label="notes")
    app.state.agents.set_streaming_session_id("unit-x", "opaque-child-stream", label="work")
    for index, session_id in enumerate((
        "unit-main", "unit-main", "unit-notes", "unit-x-main", "unitary-main",
        "unit", "unit.sibling", "opaque-stream", "opaque-active",
    )):
        app.state.conversation_store.append(
            session_id, "user", f"needle {session_id}", timestamp=100 + index,
        )
    yield app
    app.state.dream_runner.close()
    app.state.conversation_store.close()
    app.state.agents.close()
    app.state.store_catalog.close()


def legacy_session_ids(app, name):
    """Reference the previous union on a small, complete synthetic fixture."""
    result = {
        session.id for session in app.state.manager.list()
        if session.agent_name == name or session.id.startswith(name + "-")
    }
    for entry in app.state.agents.list_streaming_session_ids(name):
        if entry.get("session_id"):
            result.add(entry["session_id"])
        if entry.get("label"):
            result.add(name + "-" + entry["label"])
    result.update(
        summary.session_id
        for summary in app.state.conversation_store.list_conversations(limit=5000)
        if summary.session_id.startswith(name + "-")
    )
    return result


@pytest.mark.parametrize("prefix", [
    "unit-", "unit_x-", "unit%-", "unit'-", "café-", "absent-",
])
def test_session_prefix_uses_a_covering_index_range(tmp_path, prefix):
    store = ConversationStore(str(tmp_path / "messages.db"))
    try:
        sessions = [
            "unit-main", "unit-main", "unit-tag", "unit-x-main", "unitary-main",
            "unit", "unit.sibling", "UNIT-main", "unit_x-main", "unitXx-main",
            "unit%-main", "unit'-main", "café-main", "cafés-main",
        ]
        for session_id in sessions:
            store.append(session_id, "user", "fixture message")
        method = getattr(store, "list_session_ids_with_prefix", None)
        assert callable(method), "Session discovery needs a dedicated prefix query"
        statements = []
        store._conn.set_trace_callback(statements.append)
        result = method(prefix)
        store._conn.set_trace_callback(None)
        assert set(result) == {value for value in sessions if value.startswith(prefix)}
        assert len(result) == len(set(result))
        queries = [sql for sql in statements if sql.lstrip().upper().startswith("SELECT")]
        assert len(queries) == 1
        query = " ".join(queries[0].split())
        assert query.upper().startswith("SELECT DISTINCT SESSION_ID FROM MESSAGES WHERE")
        assert "session_id >=" in query and "session_id <" in query
        assert "GROUP BY" not in query.upper() and "COUNT(" not in query.upper()
        plan = " ".join(row[3] for row in store._conn.execute("EXPLAIN QUERY PLAN " + query))
        assert "SEARCH messages USING COVERING INDEX idx_messages_session" in plan
        assert "session_id>? AND session_id<?" in plan
        assert "TEMP B-TREE" not in plan
    finally:
        store.close()


def test_empty_session_prefix_does_not_scan_the_store(tmp_path):
    store = ConversationStore(str(tmp_path / "messages.db"))
    try:
        method = getattr(store, "list_session_ids_with_prefix", None)
        assert callable(method), "Session discovery needs a dedicated prefix query"
        statements = []
        store._conn.set_trace_callback(statements.append)
        assert method("") == []
        assert not any(sql.lstrip().upper().startswith("SELECT") for sql in statements)
    finally:
        store.close()


@pytest.mark.parametrize("name", ["unit", "unit-x", "unitary"])
def test_session_collection_preserves_active_streaming_and_prefix_aliases(history_app, name):
    expected = legacy_session_ids(history_app, name)
    actual = memory._collect_agent_session_ids(name)
    assert actual == expected
    if name == "unit":
        assert {"opaque-active", "opaque-stream", "unit-notes", "unit-x-main"} <= actual
        assert "unitary-main" not in actual


def test_session_collection_does_not_list_all_conversations(history_app, monkeypatch):
    store = history_app.state.conversation_store
    expected = legacy_session_ids(history_app, "unit")
    aggregate = Mock(wraps=store.list_conversations)
    monkeypatch.setattr(store, "list_conversations", aggregate)
    assert memory._collect_agent_session_ids("unit") == expected
    assert aggregate.call_count == 0, "Collecting one agent must not aggregate the whole table"


def test_failed_prefix_read_keeps_active_and_streaming_aliases(history_app, monkeypatch):
    store = history_app.state.conversation_store
    failing = Mock(side_effect=OSError("synthetic read failure"))
    monkeypatch.setattr(store, "list_session_ids_with_prefix", failing, raising=False)
    result = memory._collect_agent_session_ids("unit")
    assert result == {"opaque-active", "unit-active", "opaque-stream", "unit-notes"}
    assert failing.call_count == 1


@pytest.mark.parametrize("query", ["", "needle"], ids=["history", "search"])
def test_chat_history_collects_session_ids_once(history_app, monkeypatch, query):
    manager = history_app.state.manager
    agents = history_app.state.agents
    listed = Mock(wraps=manager.list)
    streamed = Mock(wraps=agents.list_streaming_session_ids)
    monkeypatch.setattr(manager, "list", listed)
    monkeypatch.setattr(agents, "list_streaming_session_ids", streamed)
    client = TestClient(history_app)
    try:
        response = client.get("/agents/unit/chat-history", params={"q": query})
    finally:
        client.close()
    assert response.status_code == 200, response.text
    assert response.json()["count"] > 0
    assert listed.call_count == 1, "The route and resolver must share their collection"
    assert streamed.call_count == 1


@pytest.mark.parametrize("session_ids", [set(), {"opaque-stream"}], ids=["empty", "provided"])
def test_history_resolver_uses_the_provided_session_ids(history_app, monkeypatch, session_ids):
    resolver = history_app.state.agent_history_resolver
    assert "session_ids" in inspect.signature(resolver).parameters
    listed = Mock(side_effect=AssertionError("A provided set must not trigger collection"))
    streamed = Mock(side_effect=AssertionError("A provided set must not trigger collection"))
    monkeypatch.setattr(history_app.state.manager, "list", listed)
    monkeypatch.setattr(history_app.state.agents, "list_streaming_session_ids", streamed)
    before = set(session_ids)
    result = resolver("unit", session_ids=session_ids)
    assert {message["session_id"] for message in result} == session_ids
    assert session_ids == before
    assert listed.call_count == streamed.call_count == 0


def test_history_resolver_still_collects_when_no_set_is_supplied(history_app, monkeypatch):
    manager = history_app.state.manager
    listed = Mock(wraps=manager.list)
    monkeypatch.setattr(manager, "list", listed)
    result = history_app.state.agent_history_resolver("unit")
    assert result and listed.call_count == 1


def test_unknown_agent_history_does_not_collect(history_app, monkeypatch):
    listed = Mock(side_effect=AssertionError("An unknown agent has no history to collect"))
    monkeypatch.setattr(history_app.state.manager, "list", listed)
    assert history_app.state.agent_history_resolver("unknown") == []
    assert listed.call_count == 0
