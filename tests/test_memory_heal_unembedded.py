"""Tests for #630 heal-on-write: re-embedding stranded ('[]') reflections.

A reflection stored while the embedder is a NoOp (no key) or transiently
degraded persists with ``embedding == '[]'`` — findable via structured/keyword
query but silently invisible to semantic ``recall``. These tests cover the
store primitives (``get_unembedded`` / ``set_embedding``) and the server's
heal-on-write backfill wired into ``_embed_build_insert`` (exercised via the
``reflect`` tool), including the degraded, bounded, kill-switch, and
never-break-the-write paths.
"""
from __future__ import annotations

import json
from pathlib import Path

from pinky_memory.embeddings import NoOpEmbeddingClient
from pinky_memory.server import _HEAL_BATCH, create_server
from pinky_memory.store import ReflectionStore
from pinky_memory.types import Reflection, ReflectionType

# ── Helpers ──────────────────────────────────────────────────────────────────


def _store(tmp_path: Path) -> ReflectionStore:
    return ReflectionStore(str(tmp_path / "heal.db"))


def _stranded(store: ReflectionStore, content: str) -> str:
    """Insert an active reflection with no embedding ('[]'), as a degraded or
    NoOp embedder would have. Returns its id."""
    r = store.insert(Reflection(type=ReflectionType.fact, content=content))
    assert store.get(r.id).embedding == []  # stored as '[]'
    return r.id


class _RealEmbedder:
    """Non-NoOp embedder returning a fixed finite, non-zero-norm vector so
    ``_safe_embed`` accepts it. Content-insensitive — tests assert *that* a row
    got embedded, not similarity."""

    dimensions = 8

    def embed(self, text: str) -> list[float]:
        return [0.1] * self.dimensions


def _tools(srv):
    return {t.name: t.fn for t in srv._tool_manager.list_tools()}


def _unembedded_count(store: ReflectionStore) -> int:
    return len(store.get_unembedded(limit=10_000))


# ── Store: get_unembedded ────────────────────────────────────────────────────


class TestGetUnembedded:
    def test_returns_only_active_stranded(self, tmp_path):
        store = _store(tmp_path)
        a = _stranded(store, "stranded one")
        b = _stranded(store, "stranded two")
        # An embedded row must not be reported.
        store.insert(Reflection(type=ReflectionType.fact, content="has vec", embedding=[0.2] * 8))
        # An inactive stranded row must not be reported (recall returns active).
        inactive = store.insert(Reflection(type=ReflectionType.fact, content="old stranded"))
        store.deactivate_superseded(inactive.id)  # sets active = 0

        ids = [rid for rid, _ in store.get_unembedded(limit=50)]
        assert set(ids) == {a, b}

    def test_oldest_first_and_bounded(self, tmp_path):
        store = _store(tmp_path)
        ids = [_stranded(store, f"s{i}") for i in range(5)]
        got = store.get_unembedded(limit=3)
        assert [rid for rid, _ in got] == ids[:3]  # oldest three, in order

    def test_returns_id_and_content(self, tmp_path):
        store = _store(tmp_path)
        rid = _stranded(store, "remember the milk")
        assert store.get_unembedded(limit=1) == [(rid, "remember the milk")]

    def test_empty_when_all_embedded(self, tmp_path):
        store = _store(tmp_path)
        store.insert(Reflection(type=ReflectionType.fact, content="x", embedding=[0.1] * 8))
        assert store.get_unembedded() == []


# ── Store: set_embedding ─────────────────────────────────────────────────────


class TestSetEmbedding:
    def test_persists_and_makes_searchable(self, tmp_path):
        store = _store(tmp_path)
        rid = _stranded(store, "find me by vector")
        vec = [0.1] * 8

        assert store.set_embedding(rid, vec) is True
        assert store.get(rid).embedding == vec
        assert rid not in [r for r, _ in store.get_unembedded()]
        # End-to-end: now reachable by vector search (proves vec-table sync).
        hits = store.search_by_embedding(query_embedding=vec, limit=5)
        assert rid in [h.id for h in hits]

    def test_missing_id_returns_false(self, tmp_path):
        store = _store(tmp_path)
        assert store.set_embedding("does-not-exist", [0.1] * 8) is False

    def test_empty_vector_is_noop(self, tmp_path):
        store = _store(tmp_path)
        rid = _stranded(store, "stays stranded")
        assert store.set_embedding(rid, []) is False
        assert store.get(rid).embedding == []

    def test_idempotent_reheal(self, tmp_path):
        store = _store(tmp_path)
        rid = _stranded(store, "double heal")
        vec = [0.1] * 8
        assert store.set_embedding(rid, vec) is True
        # Second call must not raise or duplicate the vec row.
        assert store.set_embedding(rid, vec) is True
        hits = store.search_by_embedding(query_embedding=vec, limit=10)
        assert [h.id for h in hits].count(rid) == 1


# ── Server: heal-on-write ────────────────────────────────────────────────────


class TestHealOnWrite:
    def test_reflect_reembeds_stranded(self, tmp_path):
        store = _store(tmp_path)
        s1 = _stranded(store, "stranded A")
        s2 = _stranded(store, "stranded B")
        srv = create_server(store, _RealEmbedder())

        result = json.loads(_tools(srv)["reflect"](content="fresh fact", type="fact"))
        assert result["embedded"] is True
        # Both previously-stranded rows are now embedded.
        assert store.get(s1).embedding == [0.1] * 8
        assert store.get(s2).embedding == [0.1] * 8
        assert _unembedded_count(store) == 0

    def test_skipped_when_embedder_degraded(self, tmp_path):
        store = _store(tmp_path)
        sid = _stranded(store, "still stranded")
        srv = create_server(store, NoOpEmbeddingClient())

        result = json.loads(_tools(srv)["reflect"](content="no vec either", type="fact"))
        assert result["embedded"] is False
        # Nothing healed; the new row is also un-embedded. No crash.
        assert store.get(sid).embedding == []
        assert _unembedded_count(store) == 2

    def test_recovers_after_embedder_restored(self, tmp_path):
        store = _store(tmp_path)
        # Write under a degraded/NoOp embedder → stranded.
        noop_srv = create_server(store, NoOpEmbeddingClient())
        r = json.loads(_tools(noop_srv)["reflect"](content="written while degraded", type="fact"))
        assert r["embedded"] is False
        assert _unembedded_count(store) == 1
        # Embedder recovers → next write heals the backlog.
        live_srv = create_server(store, _RealEmbedder())
        json.loads(_tools(live_srv)["reflect"](content="now healthy", type="fact"))
        assert _unembedded_count(store) == 0

    def test_heal_is_batch_bounded(self, tmp_path):
        store = _store(tmp_path)
        for i in range(_HEAL_BATCH + 2):
            _stranded(store, f"backlog {i}")
        srv = create_server(store, _RealEmbedder())

        json.loads(_tools(srv)["reflect"](content="trigger 1", type="fact"))
        # One write heals at most _HEAL_BATCH; the remaining 2 persist.
        assert _unembedded_count(store) == 2
        json.loads(_tools(srv)["reflect"](content="trigger 2", type="fact"))
        assert _unembedded_count(store) == 0

    def test_kill_switch_disables_heal(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PINKY_MEMORY_DISABLE_HEAL", "1")
        store = _store(tmp_path)
        sid = _stranded(store, "left alone")
        srv = create_server(store, _RealEmbedder())

        result = json.loads(_tools(srv)["reflect"](content="healthy write", type="fact"))
        assert result["embedded"] is True  # the new row still embeds
        assert store.get(sid).embedding == []  # but heal did not run

    def test_heal_never_breaks_the_write(self, tmp_path, monkeypatch):
        store = _store(tmp_path)
        _stranded(store, "boom row")
        srv = create_server(store, _RealEmbedder())

        def _explode(*a, **k):
            raise RuntimeError("backfill query blew up")

        monkeypatch.setattr(store, "get_unembedded", _explode)
        # The reflect must still succeed and store/embed its own row.
        result = json.loads(_tools(srv)["reflect"](content="must survive", type="fact"))
        assert result["stored"] is True
        assert result["embedded"] is True
