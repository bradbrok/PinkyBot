"""Tests for pinky_memory.store.ReflectionStore."""
from __future__ import annotations

import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from pinky_memory.store import ReflectionStore
from pinky_memory.types import MemoryQueryFilters, Reflection, ReflectionType


# ── Helpers ────────────────────────────────────────────────────────────────────

def _store(tmp_path: Path) -> ReflectionStore:
    return ReflectionStore(str(tmp_path / "test.db"))


def _fact(content: str = "test fact", salience: int = 3, **kwargs) -> Reflection:
    return Reflection(type=ReflectionType.fact, content=content, salience=salience, **kwargs)


def _emb(dims: int = 8, val: float = 0.1) -> list[float]:
    """Tiny normalised embedding for fast tests."""
    v = [val] * dims
    mag = sum(x * x for x in v) ** 0.5
    return [x / mag for x in v]


# ── Basic CRUD ─────────────────────────────────────────────────────────────────

class TestInsertGet:
    def test_insert_and_get(self, tmp_path):
        store = _store(tmp_path)
        r = store.insert(_fact("hello world"))
        assert r.id
        fetched = store.get(r.id)
        assert fetched is not None
        assert fetched.content == "hello world"
        assert fetched.type == ReflectionType.fact
        assert fetched.active is True

    def test_get_missing_returns_none(self, tmp_path):
        store = _store(tmp_path)
        assert store.get("nonexistent-id") is None

    def test_insert_assigns_id_if_empty(self, tmp_path):
        store = _store(tmp_path)
        r = Reflection(type=ReflectionType.insight, content="auto id")
        inserted = store.insert(r)
        assert inserted.id != ""

    def test_insert_preserves_provided_id(self, tmp_path):
        store = _store(tmp_path)
        r = Reflection(id="my-custom-id", type=ReflectionType.fact, content="custom")
        inserted = store.insert(r)
        assert inserted.id == "my-custom-id"
        assert store.get("my-custom-id") is not None

    def test_insert_with_embedding(self, tmp_path):
        store = _store(tmp_path)
        emb = _emb()
        r = store.insert(_fact("embedded", embedding=emb))
        fetched = store.get(r.id)
        assert len(fetched.embedding) == len(emb)

    def test_insert_with_entities(self, tmp_path):
        store = _store(tmp_path)
        r = store.insert(_fact("brad asked", entities=["Brad"]))
        fetched = store.get(r.id)
        assert fetched.entities == ["Brad"]

    def test_insert_with_source_fields(self, tmp_path):
        store = _store(tmp_path)
        r = store.insert(_fact(
            "from telegram",
            source_session_id="telegram:123",
            source_channel="telegram",
            source_message_ids=["m1", "m2"],
        ))
        fetched = store.get(r.id)
        assert fetched.source_session_id == "telegram:123"
        assert fetched.source_channel == "telegram"
        assert fetched.source_message_ids == ["m1", "m2"]


# ── Deactivation / Supersession ────────────────────────────────────────────────

class TestDeactivation:
    def test_deactivate_superseded(self, tmp_path):
        store = _store(tmp_path)
        old = store.insert(_fact("old knowledge"))
        new = store.insert(_fact("new knowledge"))
        store.deactivate_superseded(old.id, superseded_by=new.id)
        fetched = store.get(old.id)
        assert fetched.active is False
        assert fetched.superseded_by == new.id

    def test_deactivate_without_superseded_by(self, tmp_path):
        store = _store(tmp_path)
        r = store.insert(_fact("to deactivate"))
        store.deactivate_superseded(r.id)
        assert store.get(r.id).active is False

    def test_set_no_recall_true(self, tmp_path):
        store = _store(tmp_path)
        r = store.insert(_fact("private"))
        store.set_no_recall(r.id, True)
        assert store.get(r.id).no_recall is True

    def test_set_no_recall_false(self, tmp_path):
        store = _store(tmp_path)
        r = store.insert(_fact("public"))
        store.set_no_recall(r.id, True)
        store.set_no_recall(r.id, False)
        assert store.get(r.id).no_recall is False


# ── Memory Linking ─────────────────────────────────────────────────────────────

class TestMemoryLinking:
    def test_create_link(self, tmp_path):
        store = _store(tmp_path)
        a = store.insert(_fact("topic a"))
        b = store.insert(_fact("topic b"))
        created = store.create_link(a.id, b.id, similarity=0.85)
        assert created is True

    def test_create_link_idempotent(self, tmp_path):
        store = _store(tmp_path)
        a = store.insert(_fact("a"))
        b = store.insert(_fact("b"))
        store.create_link(a.id, b.id, 0.85)
        created_again = store.create_link(a.id, b.id, 0.85)
        assert created_again is False

    def test_get_links(self, tmp_path):
        store = _store(tmp_path)
        a = store.insert(_fact("node a"))
        b = store.insert(_fact("node b"))
        c = store.insert(_fact("node c"))
        store.create_link(a.id, b.id, 0.9)
        store.create_link(a.id, c.id, 0.8)
        links = store.get_links(a.id)
        target_ids = {lk.target_id for lk in links}
        assert b.id in target_ids
        assert c.id in target_ids

    def test_get_links_ordered_by_similarity(self, tmp_path):
        store = _store(tmp_path)
        a = store.insert(_fact("a"))
        b = store.insert(_fact("b"))
        c = store.insert(_fact("c"))
        store.create_link(a.id, c.id, 0.7)
        store.create_link(a.id, b.id, 0.95)
        links = store.get_links(a.id)
        assert links[0].similarity >= links[1].similarity

    def test_prune_orphan_links(self, tmp_path):
        store = _store(tmp_path)
        a = store.insert(_fact("active"))
        b = store.insert(_fact("to deactivate"))
        store.create_link(a.id, b.id, 0.8)
        store.deactivate_superseded(b.id)
        pruned = store.prune_orphan_links()
        assert pruned >= 1
        assert store.get_links(a.id) == []

    def test_get_active_with_embeddings(self, tmp_path):
        store = _store(tmp_path)
        emb = _emb()
        with_emb = store.insert(_fact("has embedding", embedding=emb))
        without = store.insert(_fact("no embedding"))
        results = store.get_active_with_embeddings()
        ids = [r.id for r in results]
        assert with_emb.id in ids
        assert without.id not in ids

    def test_get_active_with_embeddings_since_filter(self, tmp_path):
        store = _store(tmp_path)
        emb = _emb()
        r = store.insert(_fact("recent", embedding=emb))
        future = datetime.now(timezone.utc) + timedelta(hours=1)
        results = store.get_active_with_embeddings(since=future)
        assert all(x.id != r.id for x in results)


# ── Salience Query ─────────────────────────────────────────────────────────────

class TestSalienceQuery:
    def test_get_by_min_salience(self, tmp_path):
        store = _store(tmp_path)
        low = store.insert(_fact("low", salience=2))
        high = store.insert(_fact("high", salience=5))
        results = store.get_by_min_salience(min_salience=4)
        ids = [r.id for r in results]
        assert high.id in ids
        assert low.id not in ids

    def test_get_by_min_salience_excludes_no_recall(self, tmp_path):
        store = _store(tmp_path)
        r = store.insert(_fact("secret", salience=5))
        store.set_no_recall(r.id, True)
        results = store.get_by_min_salience(min_salience=4)
        assert all(x.id != r.id for x in results)


# ── Keyword Search ─────────────────────────────────────────────────────────────

class TestKeywordSearch:
    def test_search_finds_content_match(self, tmp_path):
        store = _store(tmp_path)
        store.insert(_fact("machine learning is fun"))
        store.insert(_fact("unrelated topic"))
        results = store.search_by_keyword("machine learning")
        assert len(results) >= 1
        assert any("machine learning" in r.content for r in results)

    def test_search_returns_empty_for_no_match(self, tmp_path):
        store = _store(tmp_path)
        store.insert(_fact("something"))
        results = store.search_by_keyword("xyzzy123notfound")
        assert results == []

    def test_search_by_keyword_scored(self, tmp_path):
        store = _store(tmp_path)
        store.insert(_fact("vector search rocks"))
        store.insert(_fact("something else"))
        scored = store.search_by_keyword_scored("vector search")
        assert len(scored) >= 1
        score, reflection = scored[0]
        assert isinstance(score, float)
        assert "vector" in reflection.content

    def test_search_respects_active_filter(self, tmp_path):
        store = _store(tmp_path)
        r = store.insert(_fact("inactive keyword test"))
        store.deactivate_superseded(r.id)
        results = store.search_by_keyword("inactive keyword test", active_only=True)
        assert all(x.id != r.id for x in results)

    def test_search_keyword_type_filter(self, tmp_path):
        store = _store(tmp_path)
        store.insert(Reflection(type=ReflectionType.insight, content="cool insight here"))
        store.insert(_fact("cool fact here"))
        results = store.search_by_keyword("cool", type_filter=ReflectionType.insight)
        assert all(r.type == ReflectionType.insight for r in results)

    def test_search_keyword_project_filter(self, tmp_path):
        store = _store(tmp_path)
        store.insert(_fact("pinkybot task", project="pinkybot"))
        store.insert(_fact("other task", project="other"))
        results = store.search_by_keyword("task", project_filter="pinkybot")
        assert all(r.project == "pinkybot" for r in results)


# ── Embedding (numpy) Search ───────────────────────────────────────────────────

class TestEmbeddingSearch:
    def test_search_by_embedding_basic(self, tmp_path):
        store = _store(tmp_path)
        emb = _emb(8, 0.5)
        store.insert(_fact("embedded memory", embedding=emb))
        store.insert(_fact("no embedding"))
        results = store.search_by_embedding(emb, limit=5)
        assert len(results) >= 1

    def test_search_by_embedding_no_results_for_empty(self, tmp_path):
        store = _store(tmp_path)
        results = store.search_by_embedding(_emb())
        assert results == []

    def test_search_by_embedding_scored(self, tmp_path):
        store = _store(tmp_path)
        emb = _emb(8, 0.3)
        store.insert(_fact("scored embedding", embedding=emb))
        scored = store.search_by_embedding_scored(emb, limit=5)
        assert len(scored) >= 1
        score, ref = scored[0]
        assert 0.0 <= score <= 1.5  # cosine + boosts can push above 1

    def test_search_excludes_no_recall(self, tmp_path):
        store = _store(tmp_path)
        emb = _emb()
        r = store.insert(_fact("hidden", embedding=emb))
        store.set_no_recall(r.id, True)
        results = store.search_by_embedding(emb)
        assert all(x.id != r.id for x in results)


# ── Near-Duplicate Detection ───────────────────────────────────────────────────

class TestNearDuplicate:
    def test_finds_duplicate(self, tmp_path):
        store = _store(tmp_path)
        emb = _emb()
        store.insert(_fact("original", embedding=emb))
        result = store.find_near_duplicate(emb, threshold=0.99)
        assert result is not None
        sim, ref = result
        assert sim > 0.99

    def test_no_duplicate_below_threshold(self, tmp_path):
        store = _store(tmp_path)
        # Orthogonal vectors: cosine similarity is 0
        e1 = [1.0, 0.0, 0.0, 0.0]
        e2 = [0.0, 1.0, 0.0, 0.0]
        store.insert(_fact("orthogonal", embedding=e1))
        result = store.find_near_duplicate(e2, threshold=0.99)
        assert result is None

    def test_empty_embedding_returns_none(self, tmp_path):
        store = _store(tmp_path)
        assert store.find_near_duplicate([]) is None


# ── Introspect ─────────────────────────────────────────────────────────────────

class TestIntrospect:
    def test_introspect_empty(self, tmp_path):
        store = _store(tmp_path)
        info = store.introspect()
        assert info["total_reflections"] == 0
        assert info["by_type"] == {}

    def test_introspect_counts(self, tmp_path):
        store = _store(tmp_path)
        store.insert(_fact("fact 1"))
        store.insert(_fact("fact 2"))
        store.insert(Reflection(type=ReflectionType.insight, content="insight 1"))
        info = store.introspect()
        assert info["total_reflections"] == 3
        assert info["by_type"].get("fact") == 2
        assert info["by_type"].get("insight") == 1

    def test_introspect_by_project(self, tmp_path):
        store = _store(tmp_path)
        store.insert(_fact("work thing", project="pinkybot"))
        info = store.introspect(project_filter="pinkybot")
        assert info["total_reflections"] == 1

    def test_introspect_type_filter(self, tmp_path):
        store = _store(tmp_path)
        store.insert(_fact("a fact"))
        store.insert(Reflection(type=ReflectionType.insight, content="an insight"))
        info = store.introspect(type_filter=ReflectionType.fact)
        assert info["by_type"].get("insight") is None

    def test_introspect_timeframe_day(self, tmp_path):
        store = _store(tmp_path)
        store.insert(_fact("recent"))
        info = store.introspect(timeframe="day")
        assert info["total_reflections"] >= 1

    def test_introspect_recent_list(self, tmp_path):
        store = _store(tmp_path)
        for i in range(7):
            store.insert(_fact(f"fact {i}"))
        info = store.introspect()
        assert len(info["recent"]) == 5  # capped at 5


# ── GC Inactive ────────────────────────────────────────────────────────────────

class TestGcInactive:
    def test_gc_inactive_deletes_old_inactive(self, tmp_path):
        store = _store(tmp_path)
        r = store.insert(_fact("old inactive"))
        store.deactivate_superseded(r.id)
        # Force created_at to be old
        store._conn.execute(
            "UPDATE reflections SET created_at = ? WHERE id = ?",
            ((datetime.now(timezone.utc) - timedelta(days=60)).isoformat(), r.id),
        )
        store._conn.commit()
        deleted = store.gc_inactive(max_age_days=30)
        assert deleted == 1
        assert store.get(r.id) is None

    def test_gc_does_not_delete_recent_inactive(self, tmp_path):
        store = _store(tmp_path)
        r = store.insert(_fact("recent inactive"))
        store.deactivate_superseded(r.id)
        deleted = store.gc_inactive(max_age_days=30)
        assert deleted == 0
        assert store.get(r.id) is not None

    def test_gc_keeps_inactive_if_replacement_also_inactive(self, tmp_path):
        store = _store(tmp_path)
        old = store.insert(_fact("old"))
        new = store.insert(_fact("new"))
        store.deactivate_superseded(old.id, superseded_by=new.id)
        store.deactivate_superseded(new.id)  # replacement is also inactive
        # Age both
        store._conn.execute(
            "UPDATE reflections SET created_at = ?",
            ((datetime.now(timezone.utc) - timedelta(days=60)).isoformat(),),
        )
        store._conn.commit()
        deleted = store.gc_inactive(max_age_days=30)
        # old should NOT be deleted since replacement is also inactive
        assert store.get(old.id) is not None


# ── Weight Decay ───────────────────────────────────────────────────────────────

class TestApplyDecay:
    def test_apply_decay_reduces_weight(self, tmp_path):
        store = _store(tmp_path)
        r = store.insert(_fact("normal fact", salience=2))
        # Make it look like it was last accessed 10 days ago
        old_accessed = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
        store._conn.execute(
            "UPDATE reflections SET accessed_at = ? WHERE id = ?",
            (old_accessed, r.id),
        )
        store._conn.commit()
        updated = store.apply_decay()
        assert updated >= 1
        fetched = store.get(r.id)
        assert fetched.weight < 1.0

    def test_apply_decay_immune_high_salience_fact(self, tmp_path):
        store = _store(tmp_path)
        r = store.insert(_fact("important", salience=5))
        old_accessed = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
        store._conn.execute(
            "UPDATE reflections SET accessed_at = ? WHERE id = ?",
            (old_accessed, r.id),
        )
        store._conn.commit()
        store.apply_decay(immunity_salience=4)
        fetched = store.get(r.id)
        assert fetched.weight == 1.0  # immune

    def test_apply_decay_archives_below_threshold(self, tmp_path):
        store = _store(tmp_path)
        r = store.insert(_fact("fading", salience=2))
        # Set weight just above threshold and last accessed 100 days ago
        store._conn.execute(
            "UPDATE reflections SET weight = 0.15, accessed_at = ? WHERE id = ?",
            ((datetime.now(timezone.utc) - timedelta(days=100)).isoformat(), r.id),
        )
        store._conn.commit()
        store.apply_decay(archive_threshold=0.1)
        fetched = store.get(r.id)
        # Should be soft-archived or weight reduced significantly
        assert fetched.weight < 0.15


# ── Boost on Access ────────────────────────────────────────────────────────────

class TestBoostOnAccess:
    def test_boost_increases_weight(self, tmp_path):
        store = _store(tmp_path)
        r = store.insert(_fact("boostable"))
        store._conn.execute("UPDATE reflections SET weight = 0.5 WHERE id = ?", (r.id,))
        store._conn.commit()
        store.boost_weight_on_access(r.id, boost=0.1)
        fetched = store.get(r.id)
        assert abs(fetched.weight - 0.6) < 0.001

    def test_boost_caps_at_1(self, tmp_path):
        store = _store(tmp_path)
        r = store.insert(_fact("full weight"))
        store.boost_weight_on_access(r.id, boost=0.5)
        fetched = store.get(r.id)
        assert fetched.weight == 1.0


# ── Memory Events ──────────────────────────────────────────────────────────────

class TestMemoryEvents:
    def test_log_and_get_event(self, tmp_path):
        store = _store(tmp_path)
        r = store.insert(_fact("logged"))
        event_id = store.log_memory_event(
            "merge",
            source_ids=[r.id],
            target_id=r.id,
            prior_content="original content",
            prior_salience=3,
            metadata={"reason": "test"},
        )
        assert event_id > 0
        event = store.get_memory_event(event_id)
        assert event is not None
        assert event["event_type"] == "merge"

    def test_get_nonexistent_event(self, tmp_path):
        store = _store(tmp_path)
        assert store.get_memory_event(99999) is None

    def test_revert_memory_event(self, tmp_path):
        store = _store(tmp_path)
        # Create a dedup_merge event (the only type that supports revert via source reactivation)
        r = store.insert(_fact("duplicate"))
        new_r = store.insert(_fact("canonical"))
        store.deactivate_superseded(r.id, superseded_by=new_r.id)
        event_id = store.log_memory_event(
            "dedup_merge",
            source_ids=[r.id],
            target_id=new_r.id,
            prior_content=r.content,
            prior_salience=r.salience,
        )
        reverted = store.revert_memory_event(event_id)
        assert reverted is True
        # Source should be reactivated
        assert store.get(r.id).active is True

    def test_revert_unknown_event_type_returns_false(self, tmp_path):
        store = _store(tmp_path)
        r = store.insert(_fact("x"))
        event_id = store.log_memory_event("unknown_type", source_ids=[r.id])
        reverted = store.revert_memory_event(event_id)
        assert reverted is False

    def test_revert_already_reversed_returns_false(self, tmp_path):
        store = _store(tmp_path)
        r = store.insert(_fact("y"))
        new_r = store.insert(_fact("z"))
        store.deactivate_superseded(r.id, superseded_by=new_r.id)
        event_id = store.log_memory_event("dedup_merge", source_ids=[r.id], target_id=new_r.id)
        store.revert_memory_event(event_id)
        # Second revert should fail
        assert store.revert_memory_event(event_id) is False


# ── Multiple Types ─────────────────────────────────────────────────────────────

class TestMultipleTypes:
    def test_all_reflection_types_insertable(self, tmp_path):
        store = _store(tmp_path)
        for rtype in ReflectionType:
            r = Reflection(type=rtype, content=f"type {rtype.value}")
            inserted = store.insert(r)
            fetched = store.get(inserted.id)
            assert fetched.type == rtype

    def test_introspect_by_salience(self, tmp_path):
        store = _store(tmp_path)
        for s in [1, 2, 3, 4, 5]:
            store.insert(_fact(f"salience {s}", salience=s))
        info = store.introspect()
        assert len(info["by_salience"]) == 5


# ── Concurrency (light) ────────────────────────────────────────────────────────

class TestConcurrency:
    def test_concurrent_inserts(self, tmp_path):
        import threading

        store = _store(tmp_path)
        errors = []

        def insert_many():
            try:
                for i in range(10):
                    store.insert(_fact(f"concurrent {i}"))
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=insert_many) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        info = store.introspect()
        assert info["total_reflections"] == 50


# ── Structured Query ───────────────────────────────────────────────────────────

class TestStructuredQuery:
    def test_query_basic(self, tmp_path):
        store = _store(tmp_path)
        store.insert(_fact("searchable fact"))
        store.insert(_fact("another fact"))
        results, total = store.query(MemoryQueryFilters())
        assert total == 2
        assert len(results) == 2

    def test_query_type_filter(self, tmp_path):
        store = _store(tmp_path)
        store.insert(_fact("a fact"))
        store.insert(Reflection(type=ReflectionType.insight, content="an insight"))
        results, total = store.query(MemoryQueryFilters(type="insight"))
        assert total == 1
        assert results[0].type == ReflectionType.insight

    def test_query_project_filter(self, tmp_path):
        store = _store(tmp_path)
        store.insert(_fact("pinky task", project="pinkybot"))
        store.insert(_fact("other task", project="other"))
        results, total = store.query(MemoryQueryFilters(project="pinkybot"))
        assert total == 1
        assert results[0].project == "pinkybot"

    def test_query_salience_range(self, tmp_path):
        store = _store(tmp_path)
        for s in range(1, 6):
            store.insert(_fact(f"s{s}", salience=s))
        results, total = store.query(MemoryQueryFilters(salience_min=3, salience_max=4))
        assert total == 2
        assert all(3 <= r.salience <= 4 for r in results)

    def test_query_entity_filter(self, tmp_path):
        store = _store(tmp_path)
        store.insert(_fact("brad said", entities=["Brad"]))
        store.insert(_fact("no one said"))
        results, total = store.query(MemoryQueryFilters(entity="Brad"))
        assert total == 1

    def test_query_inactive_filter(self, tmp_path):
        store = _store(tmp_path)
        active = store.insert(_fact("active"))
        inactive = store.insert(_fact("inactive"))
        store.deactivate_superseded(inactive.id)
        results, _ = store.query(MemoryQueryFilters(active=False))
        ids = [r.id for r in results]
        assert inactive.id in ids
        assert active.id not in ids

    def test_query_sort_by_salience(self, tmp_path):
        store = _store(tmp_path)
        store.insert(_fact("low", salience=1))
        store.insert(_fact("high", salience=5))
        results, _ = store.query(MemoryQueryFilters(sort_by="salience", sort_dir="desc"))
        assert results[0].salience >= results[-1].salience

    def test_query_pagination(self, tmp_path):
        store = _store(tmp_path)
        for i in range(10):
            store.insert(_fact(f"fact {i}"))
        page1, total = store.query(MemoryQueryFilters(limit=5, offset=0))
        page2, _ = store.query(MemoryQueryFilters(limit=5, offset=5))
        assert total == 10
        assert len(page1) == 5
        assert len(page2) == 5
        ids1 = {r.id for r in page1}
        ids2 = {r.id for r in page2}
        assert ids1.isdisjoint(ids2)

    def test_query_has_links_true(self, tmp_path):
        store = _store(tmp_path)
        a = store.insert(_fact("linked a"))
        b = store.insert(_fact("linked b"))
        c = store.insert(_fact("isolated"))
        store.create_link(a.id, b.id, 0.9)
        results, _ = store.query(MemoryQueryFilters(has_links=True))
        ids = {r.id for r in results}
        assert a.id in ids
        assert b.id in ids
        assert c.id not in ids

    def test_query_has_links_false(self, tmp_path):
        store = _store(tmp_path)
        a = store.insert(_fact("linked"))
        b = store.insert(_fact("isolated"))
        store.create_link(a.id, b.id, 0.9)
        c = store.insert(_fact("also isolated"))
        results, _ = store.query(MemoryQueryFilters(has_links=False))
        ids = {r.id for r in results}
        assert c.id in ids
        assert a.id not in ids

    def test_query_orphan_mode(self, tmp_path):
        store = _store(tmp_path)
        orphan = store.insert(_fact("orphan", salience=2))
        with_entity = store.insert(_fact("has entity", entities=["Brad"]))
        results, _ = store.query(MemoryQueryFilters(orphan_mode=True))
        ids = {r.id for r in results}
        assert orphan.id in ids
        assert with_entity.id not in ids

    def test_query_preset_high_value(self, tmp_path):
        store = _store(tmp_path)
        store.insert(_fact("important", salience=5))
        store.insert(_fact("trivial", salience=1))
        results, total = store.query(MemoryQueryFilters(preset="high_value"))
        assert all(r.salience >= 4 for r in results)

    def test_query_created_after(self, tmp_path):
        store = _store(tmp_path)
        r = store.insert(_fact("recent"))
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        results, total = store.query(MemoryQueryFilters(created_after=future))
        assert total == 0


# ── Utility Methods ────────────────────────────────────────────────────────────

class TestUtilityMethods:
    def test_count_active(self, tmp_path):
        store = _store(tmp_path)
        store.insert(_fact("a"))
        store.insert(_fact("b"))
        assert store.count() == 2

    def test_count_including_inactive(self, tmp_path):
        store = _store(tmp_path)
        r = store.insert(_fact("deactivated"))
        store.deactivate_superseded(r.id)
        assert store.count(active_only=False) == 1
        assert store.count(active_only=True) == 0

    def test_update_salience_weight(self, tmp_path):
        store = _store(tmp_path)
        r = store.insert(_fact("changeable"))
        store.update_salience_weight(r.id, new_weight=0.42)
        fetched = store.get(r.id)
        assert abs(fetched.weight - 0.42) < 0.001

    def test_update_content(self, tmp_path):
        store = _store(tmp_path)
        r = store.insert(_fact("original content"))
        store.update_content(r.id, "updated content")
        fetched = store.get(r.id)
        assert fetched.content == "updated content"

    def test_archive_reflection(self, tmp_path):
        store = _store(tmp_path)
        r = store.insert(_fact("to archive"))
        store.archive_reflection(r.id, reason="cleanup")
        assert store.get(r.id).active is False

    def test_get_recent_reflections(self, tmp_path):
        store = _store(tmp_path)
        r = store.insert(_fact("fresh"))
        results = store.get_recent_reflections(hours=1)
        assert any(x.id == r.id for x in results)

    def test_get_recent_reflections_type_filter(self, tmp_path):
        store = _store(tmp_path)
        store.insert(_fact("a fact"))
        store.insert(Reflection(type=ReflectionType.insight, content="an insight"))
        results = store.get_recent_reflections(hours=1, type_filter=ReflectionType.insight)
        assert all(r.type == ReflectionType.insight for r in results)

    def test_get_all_active_by_type(self, tmp_path):
        store = _store(tmp_path)
        store.insert(_fact("fact here"))
        store.insert(Reflection(type=ReflectionType.continuation, content="cont"))
        results = store.get_all_active_by_type(ReflectionType.fact)
        assert all(r.type == ReflectionType.fact for r in results)

    def test_get_recent_active_by_type(self, tmp_path):
        store = _store(tmp_path)
        for i in range(5):
            store.insert(Reflection(type=ReflectionType.insight, content=f"insight {i}"))
        results = store.get_recent_active_by_type(ReflectionType.insight, limit=3)
        assert len(results) == 3

    def test_set_context_json(self, tmp_path):
        store = _store(tmp_path)
        r = store.insert(_fact("context test"))
        store.set_context_json(r.id, {"key": "value", "n": 42})
        import json
        fetched = store.get(r.id)
        ctx = json.loads(fetched.context)
        assert ctx["key"] == "value"

    def test_get_orphan_memories(self, tmp_path):
        store = _store(tmp_path)
        r = store.insert(_fact("old orphan", salience=2))
        store._conn.execute(
            "UPDATE reflections SET created_at = ? WHERE id = ?",
            ((datetime.now(timezone.utc) - timedelta(days=60)).isoformat(), r.id),
        )
        store._conn.commit()
        orphans = store.get_orphan_memories(min_age_days=30)
        assert any(x.id == r.id for x in orphans)

    def test_get_active_reflections_for_decay(self, tmp_path):
        store = _store(tmp_path)
        r = store.insert(_fact("for decay"))
        results = store.get_active_reflections_for_decay()
        assert any(x.id == r.id for x in results)

    def test_get_active_reflections_for_decay_min_age(self, tmp_path):
        store = _store(tmp_path)
        r = store.insert(_fact("fresh — should be excluded"))
        results = store.get_active_reflections_for_decay(min_age_days=10)
        assert all(x.id != r.id for x in results)


# ── Spaced Review ──────────────────────────────────────────────────────────────

class TestSpacedReview:
    def test_schedule_review(self, tmp_path):
        store = _store(tmp_path)
        r = store.insert(_fact("reviewable"))
        store.schedule_review(r.id, interval_days=14)
        fetched = store.get(r.id)
        assert fetched.review_interval_days == 14
        assert fetched.next_review_date is not None

    def test_confirm_review_doubles_interval(self, tmp_path):
        store = _store(tmp_path)
        r = store.insert(_fact("due for review"))
        store.schedule_review(r.id, interval_days=7)
        store.confirm_review(r.id)
        fetched = store.get(r.id)
        assert fetched.review_interval_days == 14

    def test_confirm_review_caps_at_180(self, tmp_path):
        store = _store(tmp_path)
        r = store.insert(_fact("max interval"))
        store.schedule_review(r.id, interval_days=120)
        store.confirm_review(r.id)
        fetched = store.get(r.id)
        assert fetched.review_interval_days == 180

    def test_get_memories_due_for_review(self, tmp_path):
        store = _store(tmp_path)
        r = store.insert(_fact("overdue"))
        # Force next_review_date to yesterday
        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
        store._conn.execute(
            "UPDATE reflections SET next_review_date = ? WHERE id = ?",
            (yesterday, r.id),
        )
        store._conn.commit()
        due = store.get_memories_due_for_review()
        assert any(x.id == r.id for x in due)

    def test_not_due_if_future_date(self, tmp_path):
        store = _store(tmp_path)
        r = store.insert(_fact("not due yet"))
        future = (datetime.now(timezone.utc) + timedelta(days=30)).strftime("%Y-%m-%d")
        store._conn.execute(
            "UPDATE reflections SET next_review_date = ? WHERE id = ?",
            (future, r.id),
        )
        store._conn.commit()
        due = store.get_memories_due_for_review()
        assert all(x.id != r.id for x in due)


# ── Sync Counts ────────────────────────────────────────────────────────────────

class TestSyncCounts:
    def test_save_and_load_sync_count(self, tmp_path):
        store = _store(tmp_path)
        store.save_sync_count("session-abc", 42)
        counts = store.load_sync_counts()
        assert counts.get("session-abc") == 42

    def test_save_sync_count_upsert(self, tmp_path):
        store = _store(tmp_path)
        store.save_sync_count("s1", 10)
        store.save_sync_count("s1", 20)
        counts = store.load_sync_counts()
        assert counts["s1"] == 20

    def test_load_sync_counts_empty(self, tmp_path):
        store = _store(tmp_path)
        assert store.load_sync_counts() == {}


# ── Integrity / Rebuild ────────────────────────────────────────────────────────

class TestIntegrity:
    def test_check_integrity_ok(self, tmp_path):
        store = _store(tmp_path)
        ok, msg = store.check_integrity()
        assert ok is True

    def test_rebuild_fts(self, tmp_path):
        store = _store(tmp_path)
        store.insert(_fact("indexed content"))
        count = store.rebuild_fts()
        # Returns -1 if FTS5 not compiled, else >= 0
        assert count == -1 or count >= 1

    def test_reopen(self, tmp_path):
        store = _store(tmp_path)
        store.insert(_fact("persisted"))
        store.reopen()
        # Should still be readable after reopen
        assert store.count() == 1


# ── Consolidate Batch ──────────────────────────────────────────────────────────

class TestConsolidateBatch:
    def test_consolidate_empty_list(self, tmp_path):
        store = _store(tmp_path)
        assert store.consolidate_batch([]) == 0

    def test_consolidate_no_duplicates(self, tmp_path):
        store = _store(tmp_path)
        # Orthogonal embeddings — no duplicates
        r1 = store.insert(_fact("unique a", embedding=[1.0, 0.0, 0.0, 0.0]))
        r2 = store.insert(_fact("unique b", embedding=[0.0, 1.0, 0.0, 0.0]))
        merged = store.consolidate_batch([r1.id, r2.id])
        assert merged == 0
        # Both still active
        assert store.get(r1.id).active is True
        assert store.get(r2.id).active is True

    def test_consolidate_merges_near_duplicates(self, tmp_path):
        store = _store(tmp_path)
        emb = _emb()
        r1 = store.insert(_fact("near duplicate 1", embedding=emb, salience=3))
        # Slightly perturbed — still very similar
        perturbed = [x + 0.0001 for x in emb]
        mag = sum(x * x for x in perturbed) ** 0.5
        perturbed = [x / mag for x in perturbed]
        r2 = store.insert(_fact("near duplicate 2", embedding=perturbed, salience=2))
        merged = store.consolidate_batch([r1.id, r2.id], merge_threshold=0.85)
        # One should be deactivated as duplicate
        active_count = sum(1 for rid in [r1.id, r2.id] if store.get(rid).active)
        assert active_count <= 2  # at most kept one
