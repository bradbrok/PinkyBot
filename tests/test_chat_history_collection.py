"""History session discovery stays scoped, indexed and shared within a request."""

from __future__ import annotations

import inspect
import sqlite3
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


@pytest.mark.parametrize("filters", [
    {},
    {"after": "2025-01-01", "before": "2026-01-01", "role": "user"},
], ids=["plain", "date-and-role"])
def test_uncapped_history_remains_readable_with_a_finite_parameter_budget(
    history_app, tmp_path, filters,
):
    history_app.state.agents.register("scope", working_dir=str(tmp_path / "scope"))
    store = history_app.state.conversation_store
    store._conn.executemany(
        "INSERT INTO messages(session_id, role, content, timestamp) "
        "VALUES (?, 'user', 'fixture message', ?)",
        ((f"scope-session-{index:06d}", 1760000000 + index) for index in range(6000)),
    )
    store._conn.commit()
    previous_limit = store._conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 5004)
    try:
        assert len(memory._collect_agent_session_ids("scope")) == 6000
        error = None
        result = None
        try:
            result = memory.search_agent_chat_history("scope", limit=10, **filters)
        except sqlite3.OperationalError as exc:
            error = str(exc)
        assert error is None, f"History could not read the complete session scope: {error}"
        assert result["count"] == 10
        assert [message["session_id"] for message in result["messages"]] == [
            f"scope-session-{index:06d}" for index in range(5999, 5989, -1)
        ]
    finally:
        store._conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, previous_limit)


def in_list_history(store, session_ids, **filters):
    """Reference the previous reader on a small, complete scope."""
    ordered_ids = [sid for sid in dict.fromkeys(session_ids) if sid]
    if not ordered_ids:
        return []
    conditions = ["session_id IN (" + ",".join("?" for _ in ordered_ids) + ")"]
    params = list(ordered_ids)
    for name, condition in (
        ("after_ts", "timestamp >= ?"),
        ("before_ts", "timestamp <= ?"),
        ("role", "role = ?"),
    ):
        if filters.get(name):
            conditions.append(condition)
            params.append(filters[name])
    params.append(filters.get("limit", 50))
    rows = store._conn.execute(
        "SELECT * FROM messages WHERE " + " AND ".join(conditions)
        + " ORDER BY timestamp DESC LIMIT ?",
        params,
    ).fetchall()
    return [store._row_to_message(row) for row in rows]


@pytest.mark.parametrize("filters", [
    {},
    {"limit": 3},
    {"after_ts": 102, "limit": 3},
    {"before_ts": 105, "limit": 3},
    {"role": "assistant", "limit": 2},
    {"after_ts": 101, "before_ts": 106, "role": "user", "limit": 3},
    {"limit": 0},
    {"after_ts": 1000, "limit": 3},
], ids=["plain", "limit", "after", "before", "role", "all-filters", "zero", "no-match"])
def test_history_scope_matches_in_list_order_filters_and_global_limit(history_app, filters):
    store = history_app.state.conversation_store
    store.append("unit-main", "assistant", "assistant fixture", timestamp=103.5)
    store.append("unit-quote'", "assistant", "quoted fixture", timestamp=105.5)
    store.append("unit-unicode-é", "user", "Unicode fixture", timestamp=106.5)
    store.append("foreign-main", "user", "excluded newer fixture", timestamp=999)
    session_ids = sorted(memory._collect_agent_session_ids("unit")) + ["", "unit-main"]
    before = list(session_ids)
    expected = in_list_history(store, session_ids, **filters)
    actual = store.get_messages_for_sessions(session_ids, **filters)
    assert actual == expected
    assert [message.timestamp for message in actual] == sorted(
        (message.timestamp for message in actual), reverse=True,
    )
    assert len(actual) <= filters.get("limit", 50)
    assert session_ids == before


def test_history_scope_read_uses_the_session_index(history_app):
    store = history_app.state.conversation_store
    session_ids = memory._collect_agent_session_ids("unit")
    statements = []
    store._conn.set_trace_callback(statements.append)
    try:
        assert store.get_messages_for_sessions(
            session_ids, after_ts=100, before_ts=108, role="user", limit=3,
        )
    finally:
        store._conn.set_trace_callback(None)
    queries = [sql for sql in statements if sql.lstrip().upper().startswith("SELECT")]
    assert len(queries) == 1
    plan = " ".join(
        row[3] for row in store._conn.execute("EXPLAIN QUERY PLAN " + queries[0])
    )
    assert "SEARCH messages USING INDEX idx_messages_session" in plan
