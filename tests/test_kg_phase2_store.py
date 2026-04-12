"""Tests for KG Phase 2 store enhancements — new columns, extraction log."""

from __future__ import annotations

import os
import tempfile

import pytest

from pinky_memory.store import ReflectionStore


@pytest.fixture
def store(tmp_path):
    """Create a fresh ReflectionStore for testing."""
    db_path = str(tmp_path / "test_memory.db")
    return ReflectionStore(db_path=db_path)


class TestKGPhase2Schema:
    """Verify the new columns exist on kg_triples."""

    def test_kg_add_with_metadata(self, store):
        result = store.kg_add(
            subject="Brad",
            predicate="uses",
            obj="Python",
            confidence=0.9,
            extraction_method="auto_llm",
            status="active",
            temporal_granularity="inferred",
            evidence_span="Brad uses Python daily",
        )
        assert result["id"]
        assert result["extraction_method"] == "auto_llm"

    def test_kg_add_defaults(self, store):
        """Manual triples should have default Phase 2 values."""
        result = store.kg_add(
            subject="Brad",
            predicate="knows",
            obj="Alice",
        )
        # Query it back to verify defaults
        triples = store.kg_query(entity="Brad")
        assert len(triples) == 1

    def test_extraction_method_persists(self, store):
        store.kg_add(
            subject="Brad",
            predicate="lives_in",
            obj="Denver",
            extraction_method="auto_dream",
            evidence_span="moved to Denver",
        )
        # Query raw to check the column
        row = store._conn.execute(
            "SELECT extraction_method, evidence_span FROM kg_triples WHERE subject = 'Brad'"
        ).fetchone()
        assert row["extraction_method"] == "auto_dream"
        assert row["evidence_span"] == "moved to Denver"


class TestKGExtractionLog:
    """Test the kg_extraction_log table operations."""

    def test_log_extraction(self, store):
        store.kg_log_extraction(
            reflection_id="abc123",
            extractor_version="1.0",
            triples_extracted=3,
            triples_superseded=1,
        )
        row = store._conn.execute(
            "SELECT * FROM kg_extraction_log WHERE reflection_id = 'abc123'"
        ).fetchone()
        assert row is not None
        assert row["extractor_version"] == "1.0"
        assert row["triples_extracted"] == 3
        assert row["triples_superseded"] == 1

    def test_log_idempotent_upsert(self, store):
        """Reprocessing same reflection overwrites the log entry."""
        store.kg_log_extraction("abc", "1.0", triples_extracted=2)
        store.kg_log_extraction("abc", "1.1", triples_extracted=5)

        rows = store._conn.execute(
            "SELECT * FROM kg_extraction_log WHERE reflection_id = 'abc'"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["extractor_version"] == "1.1"
        assert rows[0]["triples_extracted"] == 5

    def test_log_with_errors(self, store):
        store.kg_log_extraction(
            reflection_id="err1",
            extractor_version="1.0",
            errors="LLM call failed: timeout",
        )
        row = store._conn.execute(
            "SELECT errors FROM kg_extraction_log WHERE reflection_id = 'err1'"
        ).fetchone()
        assert "timeout" in row["errors"]


class TestKGUnprocessedReflections:
    """Test fetching reflections that need KG extraction."""

    def _insert_reflection(self, store, ref_id, content="test content"):
        """Insert a minimal reflection for testing."""
        import time
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        store._conn.execute(
            """INSERT INTO reflections
               (id, type, content, context, project, salience, active,
                embedding, created_at, accessed_at, access_count, weight)
               VALUES (?, 'fact', ?, '', '', 3, 1, '[]', ?, ?, 0, 1.0)""",
            (ref_id, content, now, now),
        )
        store._conn.commit()

    def test_all_unprocessed(self, store):
        self._insert_reflection(store, "r1", "Brad uses Python")
        self._insert_reflection(store, "r2", "Brad lives in Denver")

        unprocessed = store.kg_get_unprocessed_reflections()
        assert len(unprocessed) == 2

    def test_processed_excluded(self, store):
        self._insert_reflection(store, "r1", "Brad uses Python")
        self._insert_reflection(store, "r2", "Brad lives in Denver")
        store.kg_log_extraction("r1", "1.0", triples_extracted=1)

        unprocessed = store.kg_get_unprocessed_reflections()
        assert len(unprocessed) == 1
        assert unprocessed[0]["id"] == "r2"

    def test_version_mismatch_reprocessed(self, store):
        self._insert_reflection(store, "r1", "Brad uses Python")
        store.kg_log_extraction("r1", "0.9", triples_extracted=1)

        # Without version filter — already processed
        unprocessed = store.kg_get_unprocessed_reflections()
        assert len(unprocessed) == 0

        # With newer version — needs reprocessing
        unprocessed = store.kg_get_unprocessed_reflections(extractor_version="1.0")
        assert len(unprocessed) == 1

    def test_inactive_reflections_excluded(self, store):
        self._insert_reflection(store, "r1", "Brad uses Python")
        store._conn.execute("UPDATE reflections SET active = 0 WHERE id = 'r1'")
        store._conn.commit()

        unprocessed = store.kg_get_unprocessed_reflections()
        assert len(unprocessed) == 0

    def test_limit_respected(self, store):
        for i in range(10):
            self._insert_reflection(store, f"r{i}", f"fact {i}")

        unprocessed = store.kg_get_unprocessed_reflections(limit=3)
        assert len(unprocessed) == 3


class TestKGExtractionStats:
    def _insert_reflection(self, store, ref_id, content="test"):
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        store._conn.execute(
            """INSERT INTO reflections
               (id, type, content, context, project, salience, active,
                embedding, created_at, accessed_at, access_count, weight)
               VALUES (?, 'fact', ?, '', '', 3, 1, '[]', ?, ?, 0, 1.0)""",
            (ref_id, content, now, now),
        )
        store._conn.commit()

    def test_stats(self, store):
        self._insert_reflection(store, "r1")
        self._insert_reflection(store, "r2")
        self._insert_reflection(store, "r3")
        store.kg_log_extraction("r1", "1.0", triples_extracted=3)
        store.kg_log_extraction("r2", "1.0", triples_extracted=0, errors="failed")

        stats = store.kg_extraction_stats()
        assert stats["total_reflections"] == 3
        assert stats["processed"] == 2
        assert stats["unprocessed"] == 1
        assert stats["total_triples_extracted"] == 3
        assert stats["extraction_errors"] == 1
