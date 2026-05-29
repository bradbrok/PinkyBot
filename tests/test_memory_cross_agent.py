"""Tests for cross-agent memory (reflect_for / kg_add_for / recall_for) — #145.

These privileged tools let the Dreamer (role=dreamer) read and consolidate
each agent's history into THAT agent's own memory namespace (the read side,
recall_for, lets it merge/supersede rather than duplicate). Security model:
- registered ONLY when a cross_agent_authorizer is wired (shared mode);
- the authorizer (caller must be authorized) gates every cross-agent call;
- target must be a strict slug AND a known agent (store_factory raises).
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from pinky_memory.embeddings import NoOpEmbeddingClient
from pinky_memory.server import create_server
from pinky_memory.store import ReflectionStore

_KNOWN_AGENTS = {"dreamer", "barsik", "pushok"}


def _tools(srv) -> dict:
    return {t.name: t.fn for t in srv._tool_manager.list_tools()}


@pytest.fixture
def shared(tmp_path):
    """Shared-mode server: per-agent store factory + dreamer-only authorizer."""
    stores: dict[str, ReflectionStore] = {}

    def factory(name: str) -> ReflectionStore:
        # Mirror _resolve_memory_db: unknown agents raise ValueError.
        if name not in _KNOWN_AGENTS:
            raise ValueError(f"Unknown agent: {name}")
        if name not in stores:
            stores[name] = ReflectionStore(db_path=str(tmp_path / f"{name}.db"))
        return stores[name]

    def authorizer(caller: str) -> bool:
        return caller == "dreamer"

    srv = create_server(
        store_factory=factory,
        embedder=NoOpEmbeddingClient(),
        cross_agent_authorizer=authorizer,
    )
    yield srv, stores
    for s in stores.values():
        s.close()


def _count(store: ReflectionStore) -> int:
    return len(
        store.search_by_keyword(
            query="", limit=100, active_only=True,
            type_filter=None, project_filter="", min_weight=0.0, entity_filter="",
        )
    )


# ── Registration gating ──────────────────────────────────────────────────────


def test_cross_agent_tools_absent_without_authorizer(tmp_path):
    """No authorizer → the capability simply doesn't exist."""
    store = ReflectionStore(db_path=str(tmp_path / "m.db"))
    try:
        srv = create_server(store, NoOpEmbeddingClient())
        names = set(_tools(srv))
        assert "reflect_for" not in names
        assert "kg_add_for" not in names
        assert "recall_for" not in names
    finally:
        store.close()


def test_cross_agent_tools_present_with_authorizer(shared):
    srv, _ = shared
    names = set(_tools(srv))
    assert "reflect_for" in names
    assert "kg_add_for" in names
    assert "recall_for" in names


# ── reflect_for ───────────────────────────────────────────────────────────────


def test_reflect_for_writes_into_target_not_caller(shared):
    srv, stores = shared
    reflect_for = _tools(srv)["reflect_for"]

    with patch("pinky_daemon.shared_mcp.get_current_agent", return_value="dreamer"):
        out = json.loads(reflect_for(target_agent="barsik", content="Barsik prefers X"))

    assert out["stored"] is True
    assert out["target_agent"] == "barsik"
    assert out["written_by"] == "dreamer"
    # Landed in barsik's store, NOT the dreamer's.
    assert _count(stores["barsik"]) == 1
    assert "dreamer" not in stores or _count(stores["dreamer"]) == 0


def test_reflect_for_rejects_unauthorized_caller(shared):
    srv, stores = shared
    reflect_for = _tools(srv)["reflect_for"]

    with patch("pinky_daemon.shared_mcp.get_current_agent", return_value="pushok"):
        with pytest.raises(PermissionError):
            reflect_for(target_agent="barsik", content="should not land")

    # Authorization is checked BEFORE the target store is resolved, so the
    # rejected write never even opened barsik's store (lazy factory).
    assert "barsik" not in stores


def test_reflect_for_rejects_invalid_target_slug(shared):
    srv, _ = shared
    reflect_for = _tools(srv)["reflect_for"]

    with patch("pinky_daemon.shared_mcp.get_current_agent", return_value="dreamer"):
        for bad in ("../evil", "Barsik", "a/b", "x" * 65, ""):
            with pytest.raises(ValueError):
                reflect_for(target_agent=bad, content="x")


def test_reflect_for_rejects_unknown_agent(shared):
    srv, _ = shared
    reflect_for = _tools(srv)["reflect_for"]

    with patch("pinky_daemon.shared_mcp.get_current_agent", return_value="dreamer"):
        with pytest.raises(ValueError):
            reflect_for(target_agent="ghost", content="x")  # valid slug, not registered


# ── kg_add_for ──────────────────────────────────────────────────────────────────


def test_kg_add_for_authorized_writes_to_target(shared):
    srv, stores = shared
    kg_add_for = _tools(srv)["kg_add_for"]

    with patch("pinky_daemon.shared_mcp.get_current_agent", return_value="dreamer"):
        out = json.loads(
            kg_add_for(
                target_agent="barsik",
                subject="Barsik", predicate="uses", object="SQLite",
            )
        )
    assert out.get("target_agent") == "barsik"
    assert out.get("written_by") == "dreamer"


def test_kg_add_for_rejects_unauthorized_caller(shared):
    srv, _ = shared
    kg_add_for = _tools(srv)["kg_add_for"]

    with patch("pinky_daemon.shared_mcp.get_current_agent", return_value="barsik"):
        with pytest.raises(PermissionError):
            kg_add_for(
                target_agent="pushok",
                subject="x", predicate="y", object="z",
            )


# ── recall_for ────────────────────────────────────────────────────────────────


def test_recall_for_reads_target_not_caller(shared):
    srv, stores = shared
    tools = _tools(srv)
    reflect_for, recall_for = tools["reflect_for"], tools["recall_for"]

    with patch("pinky_daemon.shared_mcp.get_current_agent", return_value="dreamer"):
        reflect_for(target_agent="barsik", content="Barsik prefers dark mode")
        # Reads barsik's store — finds the memory just written there.
        hit = recall_for(target_agent="barsik", query="dark")
        # Reads the dreamer's OWN store (empty) — finds nothing in barsik's.
        miss = recall_for(target_agent="dreamer", query="dark")

    assert "dark mode" in hit
    assert "No reflections found" in miss


def test_recall_for_rejects_unauthorized_caller(shared):
    srv, stores = shared
    recall_for = _tools(srv)["recall_for"]

    with patch("pinky_daemon.shared_mcp.get_current_agent", return_value="pushok"):
        with pytest.raises(PermissionError):
            recall_for(target_agent="barsik", query="x")

    # Authorization is checked BEFORE the target store is resolved (lazy factory).
    assert "barsik" not in stores


def test_recall_for_rejects_invalid_or_unknown_target(shared):
    srv, _ = shared
    recall_for = _tools(srv)["recall_for"]

    with patch("pinky_daemon.shared_mcp.get_current_agent", return_value="dreamer"):
        for bad in ("../evil", "Barsik", "a/b", "x" * 65, ""):
            with pytest.raises(ValueError):
                recall_for(target_agent=bad, query="x")
        with pytest.raises(ValueError):
            recall_for(target_agent="ghost", query="x")  # valid slug, not registered
