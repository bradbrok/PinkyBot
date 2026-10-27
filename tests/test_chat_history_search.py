"""Scoped history searches and blocking-store concurrency controls."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import threading
import time
from datetime import datetime, timezone
from types import SimpleNamespace
from urllib.parse import quote

import httpx
import pytest

from pinky_daemon.conversation_store import ConversationStore
from pinky_daemon.routes import memory


@pytest.fixture
def history(tmp_path, monkeypatch):
    from fastapi import FastAPI

    store = ConversationStore(str(tmp_path / "conversations.db"))
    state = SimpleNamespace(name="sample", ids={"opaque-own"}, store=store)
    monkeypatch.setattr(
        memory, "_agents",
        SimpleNamespace(get=lambda name: state if name == state.name else None),
    )
    monkeypatch.setattr(memory, "_store", store)
    monkeypatch.setattr(memory, "_collect_agent_session_ids", lambda name: set(state.ids))

    def resolve(name, **filters):
        assert name == state.name
        return [m.to_dict() for m in store.get_messages_for_sessions(state.ids, **filters)]

    monkeypatch.setattr(memory, "_resolve_agent_history", resolve)
    app = FastAPI()
    app.include_router(memory.router)

    @app.get("/cheap")
    async def cheap():
        return {"ok": True}

    state.app = app
    yield state
    store.close()


async def request(history, **params):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=history.app), base_url="http://test",
    ) as client:
        return await client.get(
            f"/agents/{quote(history.name, safe='')}/chat-history", params=params,
        )


async def concurrent_probe(app, path, entered):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
    ) as client:
        started = time.perf_counter()

        async def cheap():
            # Measure from dispatch, including time the loop could not schedule us.
            while not entered.is_set():
                await asyncio.sleep(0.005)
            response = await client.get("/cheap")
            return time.perf_counter() - started, response

        slow, (elapsed, fast) = await asyncio.gather(client.get(path), cheap())
    assert fast.status_code == 200
    assert elapsed < 0.5, f"cheap request waited {elapsed:.3f}s behind store I/O"
    return slow


@pytest.mark.parametrize("query", ["needle", ""], ids=["search", "history"])
async def test_slow_history_store_does_not_block_other_requests(history, monkeypatch, query):
    entered = threading.Event()

    def slow_search(
        query, *, limit=50, session_ids=None, session_prefix="",
        after_ts=0.0, before_ts=0.0, role="",
    ):
        entered.set()
        time.sleep(1.0)
        return []

    def slow_history(name, **filters):
        entered.set()
        time.sleep(1.0)
        return []

    monkeypatch.setattr(history.store, "search", slow_search)
    monkeypatch.setattr(memory, "_resolve_agent_history", slow_history)
    response = await concurrent_probe(
        history.app, f"/agents/sample/chat-history?q={query}", entered,
    )
    assert response.status_code == 200
    assert response.json()["messages"] == []


async def test_own_match_is_not_crowded_out_by_newer_foreign_matches(history):
    own = history.store.append("opaque-own", "user", "needle own", timestamp=1)
    for n in range(10):  # More than 3 * limit foreign results, all newer.
        history.store.append("other-main", "user", "needle foreign", timestamp=10 + n)
    response = await request(history, q="needle", limit=3)
    assert response.status_code == 200
    assert [m["id"] for m in response.json()["messages"]] == [own.id]


@pytest.mark.parametrize(
    "filters,target_offset,newer_offsets,other_role",
    [
        ({"role": "user"}, 0, range(1, 6), "assistant"),
        ({"before": "2026-01-02"}, 86400, range(86401, 86406), "user"),
        ({"after": "2026-01-02", "role": "user"}, 0, range(1, 6), "assistant"),
        ({"after": "2026-01-02"}, 0, range(-5, 0), "user"),
    ],
    ids=["role", "before-inclusive", "after-and-role", "after-inclusive"],
)
async def test_search_filters_apply_before_result_limit(
    history, filters, target_offset, newer_offsets, other_role,
):
    start = datetime(2026, 1, 2, tzinfo=timezone.utc).timestamp()
    target = history.store.append(
        "opaque-own", "user", "needle selected", timestamp=start + target_offset,
    )
    for offset in newer_offsets:
        history.store.append(
            "opaque-own", other_role, "needle outside filter", timestamp=start + offset,
        )
    response = await request(history, q="needle", limit=1, **filters)
    assert response.status_code == 200
    assert [m["id"] for m in response.json()["messages"]] == [target.id]


def test_search_without_new_filters_retains_global_results(history):
    history.store.append("opaque-own", "user", "needle own", timestamp=1)
    newest = history.store.append("foreign", "assistant", "needle foreign", timestamp=2)
    assert [m.id for m in history.store.search("needle", limit=1)] == [newest.id]


def test_fts_retry_keeps_date_role_and_session_filters_before_limit(history):
    first = history.store.append("opaque-own", "user", "don't forget", timestamp=10)
    last = history.store.append("opaque-own", "user", "don't forget", timestamp=40)
    history.store.append("opaque-own", "user", "don't forget", timestamp=5)
    history.store.append("opaque-own", "user", "don't forget", timestamp=60)
    history.store.append("opaque-own", "assistant", "don't forget", timestamp=30)
    history.store.append("foreign", "user", "don't forget", timestamp=40)
    results = history.store.search(
        "don't", session_ids=history.ids, after_ts=10, before_ts=40, role="user", limit=2,
    )
    assert [m.id for m in results] == [last.id, first.id]


@pytest.mark.parametrize(
    "name,foreign", [("unit_1", "unitX1"), ("unit%1", "unitX1"), ("unit\\1", "unit1")],
    ids=["underscore", "percent", "backslash"],
)
async def test_prefix_is_literal_with_empty_known_ids(history, name, foreign):
    history.name = name
    history.ids.clear()
    own = history.store.append(f"{name}-main", "user", "needle own", timestamp=1)
    for n in range(5):
        history.store.append(f"{foreign}-main", "user", "needle foreign", timestamp=10 + n)
    data = (await request(history, q="needle", limit=1)).json()
    assert [m["id"] for m in data["messages"]] == [own.id]
    assert data["sessions_searched"] == 0


def test_large_scope_stays_below_sqlite_variable_limit(history):
    ids = [f"session-{n}" for n in range(1500)]
    own = history.store.append(ids[-1], "user", "needle own", timestamp=1)
    history.store.append("foreign", "user", "needle foreign", timestamp=2)
    old_limit = history.store._conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 64)
    try:
        results = history.store.search("needle", session_ids=ids, limit=1)
    finally:
        history.store._conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, old_limit)
    assert [m.id for m in results] == [own.id]


def test_empty_scope_is_distinct_from_unfiltered_search(history):
    message = history.store.append("foreign", "user", "needle")
    assert history.store.search("needle", session_ids=[]) == []
    assert [m.id for m in history.store.search("needle")] == [message.id]


def test_fts_phrase_retry_keeps_scope_and_existing_filters(history):
    own = history.store.append("opaque-own", "user", "don't forget", platform="web", chat_id="1")
    history.store.append("foreign", "user", "don't forget", platform="web", chat_id="1")
    history.store.append("opaque-own", "user", "don't forget", platform="other", chat_id="2")
    results = history.store.search(
        "don't", session_ids={"opaque-own"}, session_id="opaque-own",
        platform="web", chat_id="1",
    )
    assert [m.id for m in results] == [own.id]


@pytest.mark.parametrize("query", ["needle", ""], ids=["search", "history"])
@pytest.mark.parametrize(
    "filters,expected",
    [
        ({"after": "2026-01-02"}, ["late", "boundary", "inside", "assistant"]),
        ({"before": "2026-01-02"}, ["boundary", "inside", "assistant", "early"]),
        ({"role": "assistant"}, ["assistant"]),
        ({"after": "2026-01-02", "before": "2026-01-02", "role": "user"},
         ["boundary", "inside"]),
    ],
    ids=["after", "before-inclusive", "role", "combined"],
)
async def test_date_role_filters_and_response_shape(history, query, filters, expected):
    start = datetime(2026, 1, 2, tzinfo=timezone.utc).timestamp()
    rows = [
        ("early", start - 1, "user"), ("assistant", start, "assistant"),
        ("inside", start + 1, "user"), ("boundary", start + 86400, "user"),
        ("late", start + 86401, "user"),
    ]
    names = {}
    for name, timestamp, role in rows:
        message = history.store.append(
            "opaque-own", role, f"needle {name} " + "x" * 600,
            timestamp=timestamp, platform="web",
        )
        names[message.id] = name
    response = await request(history, q=query, **filters)
    assert response.status_code == 200
    data = response.json()
    assert set(data) == {"messages", "query", "count", "sessions_searched"}
    assert data["query"] == query
    assert data["count"] == len(expected)
    assert data["sessions_searched"] == 1
    assert [names[m["id"]] for m in data["messages"]] == expected
    for message in data["messages"]:
        assert set(message) == {
            "id", "session_id", "role", "content", "timestamp", "platform", "duration_ms",
        }
        assert len(message["content"]) == 500
        assert message["platform"] == "web"
        assert message["duration_ms"] == 0


@pytest.mark.parametrize("field", ["after", "before"])
async def test_invalid_date_still_returns_bad_request(history, field):
    response = await request(history, **{field: "bad-date"})
    assert response.status_code == 400
    assert response.json()["detail"] == f"Invalid '{field}' date format. Use YYYY-MM-DD."


@pytest.mark.parametrize("total,logged", [(1.2, True), (0.9, False), (1.0, False)])
async def test_phase_log_contains_only_metadata(history, monkeypatch, caplog, total, logged):
    query = "privatequery"
    content = "privatequery confidential message"
    history.store.append("opaque-own", "user", content)
    times = iter([10.0, 10.2, 10.7, 10.0 + total])
    monkeypatch.setattr(memory, "perf_counter", lambda: next(times), raising=False)
    with caplog.at_level(logging.INFO, logger=memory.__name__):
        response = await request(history, q=query)
    assert response.status_code == 200
    records = [r for r in caplog.records if r.name == memory.__name__]
    assert len(records) == int(logged)
    if logged:
        line = records[0].getMessage()
        for key in ["collection_ms=", "store_read_ms=", "response_build_ms=", "total_ms=",
                    "agent=sample", "has_query=True", "row_count=1"]:
            assert key in line
        assert "collection_ms=200.0" in line
        assert "store_read_ms=500.0" in line
        assert "response_build_ms=500.0" in line
        assert "total_ms=1200.0" in line
        assert query not in line
        assert content not in line


@pytest.mark.parametrize(
    "path,method,result,status",
    [
        ("memories", "query", ([], 0), 200),
        ("memories/search?q=needle", "search_by_keyword", [], 200),
        ("memories/stats", "introspect", {}, 200),
        ("memories/missing", "get", None, 404),
        ("memories/example/links", "get_links", [], 200),
        ("memory/kg-graph", "kg_entities_list", [], 200),
        ("memory/kg-stats", "kg_stats", {}, 200),
    ],
    ids=["list", "search", "stats", "get", "links", "graph", "graph-stats"],
)
async def test_other_memory_store_routes_do_not_block_loop(
    history, monkeypatch, path, method, result, status,
):
    entered = threading.Event()

    def slow_read(*args, **kwargs):
        entered.set()
        time.sleep(1.0)
        return result

    store = SimpleNamespace(close=lambda: None, kg_query=lambda **kwargs: [])
    setattr(store, method, slow_read)
    monkeypatch.setattr(memory, "_get_memory_store", lambda name: store)
    response = await concurrent_probe(history.app, f"/agents/sample/{path}", entered)
    assert response.status_code == status
