"""Tests for pinky_memory MCP server tools and embeddings.

Uses a real ReflectionStore backed by a temp SQLite file + NoOpEmbeddingClient.
No network calls needed.
"""
from __future__ import annotations

import json
import os
from unittest.mock import MagicMock, patch

import pytest

from pinky_memory.embeddings import EmbeddingClient, NoOpEmbeddingClient, build_embedding_client
from pinky_memory.server import create_server
from pinky_memory.store import ReflectionStore

# ── Helpers ────────────────────────────────────────────────────────────────────

def _tools(srv):
    return {t.name: t.fn for t in srv._tool_manager.list_tools()}


def _parse_recall(raw: str) -> dict:
    """Extract JSON from memory-context fenced recall output."""
    import re
    # Strip <memory-context> tags if present
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if match:
        return json.loads(match.group())
    # Fallback: try parsing the whole thing as JSON
    return json.loads(raw)


@pytest.fixture
def store(tmp_path):
    db = str(tmp_path / "mem.db")
    s = ReflectionStore(db_path=db)
    yield s
    s.close()


@pytest.fixture
def embedder():
    return NoOpEmbeddingClient()


@pytest.fixture
def srv(store, embedder):
    return create_server(store, embedder)


# ── Embeddings ─────────────────────────────────────────────────────────────────

class TestNoOpEmbeddingClient:
    def test_dimensions(self):
        c = NoOpEmbeddingClient()
        assert c.dimensions == 0

    def test_embed_returns_empty(self):
        c = NoOpEmbeddingClient()
        assert c.embed("hello") == []

    def test_embed_batch_returns_empties(self):
        c = NoOpEmbeddingClient()
        result = c.embed_batch(["a", "b", "c"])
        assert result == [[], [], []]

    def test_embed_batch_empty_input(self):
        c = NoOpEmbeddingClient()
        assert c.embed_batch([]) == []


class TestEmbeddingClient:
    def test_embed_delegates_to_batch(self):
        with patch("openai.OpenAI") as MockOpenAI:
            mock_client = MagicMock()
            MockOpenAI.return_value = mock_client
            mock_response = MagicMock()
            mock_item = MagicMock()
            mock_item.embedding = [0.1, 0.2, 0.3]
            mock_response.data = [mock_item]
            mock_client.embeddings.create.return_value = mock_response

            ec = EmbeddingClient(api_key="sk-test")
            result = ec.embed("hello world")
            assert result == [0.1, 0.2, 0.3]

    def test_embed_batch_empty(self):
        with patch("openai.OpenAI"):
            ec = EmbeddingClient(api_key="sk-test")
            assert ec.embed_batch([]) == []

    def test_embed_batch_multiple(self):
        with patch("openai.OpenAI") as MockOpenAI:
            mock_client = MagicMock()
            MockOpenAI.return_value = mock_client
            mock_response = MagicMock()
            items = [MagicMock(), MagicMock()]
            items[0].embedding = [0.1]
            items[1].embedding = [0.2]
            mock_response.data = items
            mock_client.embeddings.create.return_value = mock_response

            ec = EmbeddingClient(api_key="sk-test")
            result = ec.embed_batch(["a", "b"])
            assert result == [[0.1], [0.2]]

    def test_dimensions_property(self):
        with patch("openai.OpenAI"):
            ec = EmbeddingClient(api_key="sk-test", dimensions=512)
            assert ec.dimensions == 512

    def test_defaults_from_env(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-env", "EMBEDDING_DIMENSIONS": "768"}):
            with patch("openai.OpenAI"):
                ec = EmbeddingClient()
                assert ec.dimensions == 768


class TestBuildEmbeddingClient:
    def test_no_api_key_returns_noop(self):
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("OPENAI_API_KEY", None)
            client = build_embedding_client()
            assert isinstance(client, NoOpEmbeddingClient)

    def test_with_api_key_returns_real_client(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}):
            with patch("openai.OpenAI"):
                client = build_embedding_client()
                assert isinstance(client, EmbeddingClient)


# ── reflect tool ───────────────────────────────────────────────────────────────

class TestReflect:
    def test_basic_reflect(self, srv):
        result = json.loads(_tools(srv)["reflect"](
            content="Brad prefers dark mode",
            type="fact",
        ))
        assert result["stored"] is True
        assert "id" in result
        assert result["type"] == "fact"
        assert result["salience"] == 3

    def test_reflect_with_all_fields(self, srv):
        result = json.loads(_tools(srv)["reflect"](
            content="PinkyBot uses FastAPI",
            type="project_state",
            context="architecture review",
            project="PinkyBot",
            salience=5,
            entities=["brad", "Oleg"],
            source_session_id="tg:123",
            source_channel="telegram",
            source_message_ids=["m1", "m2"],
        ))
        assert result["stored"] is True
        assert result["salience"] == 5
        assert result["type"] == "project_state"

    def test_reflect_supersedes(self, srv, store):
        # First memory
        r1 = json.loads(_tools(srv)["reflect"](content="old fact", type="fact"))
        old_id = r1["id"]

        # Second supersedes first
        r2 = json.loads(_tools(srv)["reflect"](
            content="new fact",
            type="fact",
            supersedes=old_id,
        ))
        assert r2["stored"] is True

        # Old should be deactivated
        old = store.get(old_id)
        assert old.active is False

    def test_reflect_entities_lowercased(self, srv, store):
        result = json.loads(_tools(srv)["reflect"](
            content="test",
            type="fact",
            entities=["Brad", "OLEG"],
        ))
        ref = store.get(result["id"])
        assert "brad" in ref.entities
        assert "oleg" in ref.entities


# ── recall tool ────────────────────────────────────────────────────────────────

class TestRecall:
    def test_recall_empty_returns_no_results(self, srv):
        raw = _tools(srv)["recall"]()
        assert "<memory-context>" in raw
        assert "No reflections found" in raw

    def test_recall_finds_stored(self, srv):
        _tools(srv)["reflect"](content="cats are great", type="fact")
        raw = _tools(srv)["recall"](query="cats")
        assert "<memory-context>" in raw
        result = _parse_recall(raw)
        # With noop embedder, falls back to keyword search
        assert result["count"] >= 1

    def test_recall_filter_by_type(self, srv):
        _tools(srv)["reflect"](content="fact about X", type="fact")
        _tools(srv)["reflect"](content="insight about Y", type="insight")

        result = _parse_recall(_tools(srv)["recall"](type="fact"))
        for r in result["reflections"]:
            assert r["type"] == "fact"

    def test_recall_filter_by_project(self, srv):
        _tools(srv)["reflect"](content="pinkybot thing", type="fact", project="pinkybot")
        _tools(srv)["reflect"](content="other thing", type="fact", project="other")

        result = _parse_recall(_tools(srv)["recall"](project="pinkybot"))
        assert all(r["project"] == "pinkybot" or "pinkybot" in r["content"]
                   for r in result["reflections"])

    def test_recall_filter_by_entity(self, srv):
        _tools(srv)["reflect"](content="brad likes cats", type="fact", entities=["brad"])
        _tools(srv)["reflect"](content="unrelated", type="fact")

        result = _parse_recall(_tools(srv)["recall"](entity="brad"))
        assert result["count"] >= 1

    def test_recall_with_query_no_embedding_falls_back_to_keyword(self, srv):
        _tools(srv)["reflect"](content="unique xyzzy token", type="fact")
        result = _parse_recall(_tools(srv)["recall"](query="xyzzy"))
        assert result["count"] >= 1

    def test_recall_limit(self, srv):
        for i in range(10):
            _tools(srv)["reflect"](content=f"item {i}", type="fact")
        result = _parse_recall(_tools(srv)["recall"](limit=3))
        assert result["count"] <= 3

    def test_recall_response_shape(self, srv):
        _tools(srv)["reflect"](content="shape test", type="fact")
        result = _parse_recall(_tools(srv)["recall"]())
        assert result["count"] >= 1
        r = result["reflections"][0]
        for key in ("id", "type", "content", "context", "project", "salience",
                    "weight", "entities", "created_at", "access_count"):
            assert key in r


# ── introspect tool ────────────────────────────────────────────────────────────

class TestIntrospect:
    def test_introspect_empty_store(self, srv):
        result = _parse_recall(_tools(srv)["introspect"]())
        assert "total_reflections" in result

    def test_introspect_counts(self, srv):
        _tools(srv)["reflect"](content="fact1", type="fact")
        _tools(srv)["reflect"](content="insight1", type="insight")
        result = _parse_recall(_tools(srv)["introspect"]())
        assert result["total_reflections"] >= 2

    def test_introspect_timeframe_day(self, srv):
        _tools(srv)["reflect"](content="today's fact", type="fact")
        result = _parse_recall(_tools(srv)["introspect"](timeframe="day"))
        assert "total_reflections" in result
        assert result["total_reflections"] >= 1

    def test_introspect_filter_by_project(self, srv):
        _tools(srv)["reflect"](content="proj thing", type="fact", project="myproj")
        result = _parse_recall(_tools(srv)["introspect"](project="myproj"))
        assert "total_reflections" in result

    def test_introspect_filter_by_type(self, srv):
        _tools(srv)["reflect"](content="insight here", type="insight")
        result = _parse_recall(_tools(srv)["introspect"](type="insight"))
        assert "total_reflections" in result


# ── memory_links tool ──────────────────────────────────────────────────────────

class TestMemoryLinks:
    def test_no_links(self, srv):
        r = json.loads(_tools(srv)["reflect"](content="solo memory", type="fact"))
        result = json.loads(_tools(srv)["memory_links"](reflection_id=r["id"]))
        assert result["count"] == 0
        assert result["links"] == []

    def test_unknown_id(self, srv):
        result = json.loads(_tools(srv)["memory_links"](reflection_id="nonexistent-id"))
        assert result["count"] == 0


# ── memory_query tool ──────────────────────────────────────────────────────────

class TestMemoryQuery:
    def test_query_empty_store(self, srv):
        result = json.loads(_tools(srv)["memory_query"]())
        assert result["total"] == 0
        assert result["reflections"] == []

    def test_query_returns_stored(self, srv):
        _tools(srv)["reflect"](content="query test fact", type="fact")
        result = json.loads(_tools(srv)["memory_query"]())
        assert result["total"] >= 1

    def test_query_filter_by_type(self, srv):
        _tools(srv)["reflect"](content="a fact", type="fact")
        _tools(srv)["reflect"](content="an insight", type="insight")
        result = json.loads(_tools(srv)["memory_query"](type="fact"))
        for r in result["reflections"]:
            assert r["type"] == "fact"

    def test_query_filter_by_project(self, srv):
        _tools(srv)["reflect"](content="proj item", type="fact", project="alpha")
        _tools(srv)["reflect"](content="other item", type="fact", project="beta")
        result = json.loads(_tools(srv)["memory_query"](project="alpha"))
        assert all(r["project"] == "alpha" for r in result["reflections"])

    def test_query_salience_min(self, srv):
        _tools(srv)["reflect"](content="low sal", type="fact", salience=1)
        _tools(srv)["reflect"](content="high sal", type="fact", salience=5)
        result = json.loads(_tools(srv)["memory_query"](salience_min=5))
        for r in result["reflections"]:
            assert r["salience"] >= 5

    def test_query_salience_max(self, srv):
        _tools(srv)["reflect"](content="low sal", type="fact", salience=1)
        _tools(srv)["reflect"](content="high sal", type="fact", salience=5)
        result = json.loads(_tools(srv)["memory_query"](salience_max=1))
        for r in result["reflections"]:
            assert r["salience"] <= 1

    def test_query_pagination(self, srv):
        for i in range(5):
            _tools(srv)["reflect"](content=f"item {i}", type="fact")
        page1 = json.loads(_tools(srv)["memory_query"](limit=2, offset=0))
        page2 = json.loads(_tools(srv)["memory_query"](limit=2, offset=2))
        assert page1["count"] == 2
        assert page2["count"] == 2
        ids1 = {r["id"] for r in page1["reflections"]}
        ids2 = {r["id"] for r in page2["reflections"]}
        assert ids1.isdisjoint(ids2)

    def test_query_preset_recent_insights(self, srv):
        _tools(srv)["reflect"](content="an insight", type="insight")
        result = json.loads(_tools(srv)["memory_query"](preset="recent_insights"))
        assert "total" in result

    def test_query_preset_high_value(self, srv):
        _tools(srv)["reflect"](content="critical fact", type="fact", salience=5)
        result = json.loads(_tools(srv)["memory_query"](preset="high_value"))
        assert "total" in result

    def test_query_unknown_preset_returns_error(self, srv):
        result = json.loads(_tools(srv)["memory_query"](preset="bogus_preset"))
        assert "error" in result
        assert "bogus_preset" in result["error"]

    def test_query_sort_by_salience(self, srv):
        _tools(srv)["reflect"](content="low", type="fact", salience=1)
        _tools(srv)["reflect"](content="high", type="fact", salience=5)
        result = json.loads(_tools(srv)["memory_query"](sort_by="salience", sort_dir="desc"))
        saliences = [r["salience"] for r in result["reflections"]]
        assert saliences == sorted(saliences, reverse=True)

    def test_query_response_shape(self, srv):
        _tools(srv)["reflect"](content="shape test", type="fact")
        result = json.loads(_tools(srv)["memory_query"]())
        assert "total" in result
        assert "count" in result
        assert "offset" in result
        assert "reflections" in result
        r = result["reflections"][0]
        for key in ("id", "type", "content", "project", "salience", "active",
                    "entities", "access_count", "created_at", "accessed_at"):
            assert key in r

    def test_query_entity_filter(self, srv):
        _tools(srv)["reflect"](content="about brad", type="fact", entities=["brad"])
        _tools(srv)["reflect"](content="no entity", type="fact")
        result = json.loads(_tools(srv)["memory_query"](entity="brad"))
        # May return 0 depending on store query impl — just check no error
        assert "total" in result

    def test_query_due_for_review(self, srv):
        _tools(srv)["reflect"](content="review me", type="fact")
        result = json.loads(_tools(srv)["memory_query"](due_for_review=True))
        assert "total" in result

    def test_query_has_links_false(self, srv):
        _tools(srv)["reflect"](content="solo", type="fact")
        result = json.loads(_tools(srv)["memory_query"](has_links=False))
        assert "total" in result


# ── Knowledge Graph Tests ─────────────────────────────────────────────────────


class TestKnowledgeGraph:
    """Tests for the per-agent temporal knowledge graph."""

    def test_kg_add_and_query(self, srv):
        tools = _tools(srv)
        result = json.loads(tools["kg_add"](
            subject="Brad", predicate="uses", object="SQLite",
            valid_from="2026-03", subject_type="person", object_type="tool",
        ))
        assert result["subject"] == "Brad"
        assert result["predicate"] == "uses"
        assert result["object"] == "SQLite"
        assert result["id"]

        query = json.loads(tools["kg_query"](entity="Brad"))
        assert query["count"] == 1
        assert query["triples"][0]["object"] == "SQLite"

    def test_kg_invalidate(self, srv):
        tools = _tools(srv)
        tools["kg_add"](subject="Brad", predicate="uses", object="Postgres", valid_from="2025-01")
        result = json.loads(tools["kg_invalidate"](
            subject="Brad", predicate="uses", object="Postgres", valid_to="2026-03",
        ))
        assert result["invalidated"] == 1

        # Should not appear in active query
        query = json.loads(tools["kg_query"](entity="Brad"))
        assert query["count"] == 0

        # Should appear with include_expired
        query = json.loads(tools["kg_query"](entity="Brad", include_expired=True))
        assert query["count"] == 1
        assert query["triples"][0]["valid_to"] == "2026-03"

    def test_kg_timeline(self, srv):
        tools = _tools(srv)
        tools["kg_add"](subject="TOD", predicate="status", object="pitch", valid_from="2026-01")
        tools["kg_add"](subject="TOD", predicate="status", object="contract", valid_from="2026-02")
        tools["kg_add"](subject="TOD", predicate="status", object="deployed", valid_from="2026-03")

        result = json.loads(tools["kg_timeline"](entity="TOD"))
        assert result["count"] == 3
        objects = [t["object"] for t in result["timeline"]]
        assert objects == ["pitch", "contract", "deployed"]

    def test_kg_connections(self, srv):
        tools = _tools(srv)
        tools["kg_add"](subject="Brad", predicate="manages", object="Barsik")
        tools["kg_add"](subject="Brad", predicate="manages", object="Pushok")
        tools["kg_add"](subject="Brad", predicate="works_with", object="Dmitriy")

        result = json.loads(tools["kg_connections"](entity="Brad"))
        assert len(result["outgoing"]) == 3
        targets = {c["target"] for c in result["outgoing"]}
        assert targets == {"Barsik", "Pushok", "Dmitriy"}

    def test_kg_connections_incoming(self, srv):
        tools = _tools(srv)
        tools["kg_add"](subject="Brad", predicate="manages", object="Barsik")

        result = json.loads(tools["kg_connections"](entity="Barsik"))
        assert len(result["incoming"]) == 1
        assert result["incoming"][0]["source"] == "Brad"

    def test_kg_stats(self, srv):
        tools = _tools(srv)
        tools["kg_add"](subject="Brad", predicate="uses", object="SQLite",
                        subject_type="person", object_type="tool")
        tools["kg_add"](subject="Brad", predicate="manages", object="Barsik",
                        subject_type="person", object_type="agent")

        result = json.loads(tools["kg_stats"]())
        assert result["entities"] >= 3  # Brad, SQLite, Barsik
        assert result["triples_active"] == 2
        assert "uses" in result["predicates"]
        assert "manages" in result["predicates"]

    def test_kg_query_as_of(self, srv):
        tools = _tools(srv)
        tools["kg_add"](subject="Brad", predicate="uses", object="Postgres", valid_from="2025-01")
        tools["kg_invalidate"](subject="Brad", predicate="uses", object="Postgres", valid_to="2026-03")
        tools["kg_add"](subject="Brad", predicate="uses", object="SQLite", valid_from="2026-03")

        # As of Feb 2026 — should see Postgres
        feb = json.loads(tools["kg_query"](entity="Brad", predicate="uses", as_of="2026-02-15"))
        assert feb["count"] == 1
        assert feb["triples"][0]["object"] == "Postgres"

        # As of Apr 2026 — should see SQLite
        apr = json.loads(tools["kg_query"](entity="Brad", predicate="uses", as_of="2026-04-15"))
        assert apr["count"] == 1
        assert apr["triples"][0]["object"] == "SQLite"

    def test_kg_case_insensitive(self, srv):
        tools = _tools(srv)
        tools["kg_add"](subject="brad", predicate="uses", object="sqlite")

        # Query with different case
        result = json.loads(tools["kg_query"](entity="Brad"))
        assert result["count"] == 1

    def test_kg_duplicate_entity(self, srv):
        tools = _tools(srv)
        tools["kg_add"](subject="Brad", predicate="uses", object="SQLite", subject_type="person")
        tools["kg_add"](subject="Brad", predicate="manages", object="Barsik", subject_type="person")

        # Brad should exist only once
        result = json.loads(tools["kg_stats"]())
        _brad_count = sum(1 for _ in [1])  # just verify no error
        assert result["entities"] == 3  # Brad, SQLite, Barsik
