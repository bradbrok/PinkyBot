"""Tests for KG auto-extraction pipeline (Phase 2)."""

from __future__ import annotations

import json

from pinky_memory.kg_extractor import (
    EVENT_PREDICATES,
    FUNCTIONAL_PREDICATES,
    MULTI_VALUED_PREDICATES,
    ExtractedTriple,
    ExtractionResult,
    KGExtractor,
    get_predicate_type,
    normalize_entity_name,
    normalize_predicate,
    parse_llm_response,
    validate_triple,
)

# ── Normalization ─────────────────────────────────────────────

class TestNormalizeEntityName:
    def test_strips_whitespace(self):
        assert normalize_entity_name("  Brad  ") == "Brad"

    def test_collapses_spaces(self):
        assert normalize_entity_name("Mac  Mini") == "Mac Mini"


class TestNormalizePredicate:
    def test_snake_case(self):
        assert normalize_predicate("works at") == "works_at"

    def test_hyphen_to_underscore(self):
        assert normalize_predicate("lives-in") == "lives_in"

    def test_lowercase(self):
        assert normalize_predicate("USES") == "uses"

    def test_alias_works_for(self):
        assert normalize_predicate("works_for") == "employed_by"

    def test_alias_resides_in(self):
        assert normalize_predicate("resides_in") == "lives_in"

    def test_alias_built_with(self):
        assert normalize_predicate("built_with") == "uses"

    def test_unknown_predicate_passthrough(self):
        assert normalize_predicate("invented") == "invented"


class TestGetPredicateType:
    def test_functional(self):
        assert get_predicate_type("lives_in") == "functional"
        assert get_predicate_type("works_at") == "functional"

    def test_multi_valued(self):
        assert get_predicate_type("uses") == "multi_valued"
        assert get_predicate_type("knows") == "multi_valued"

    def test_event(self):
        assert get_predicate_type("created") == "event"
        assert get_predicate_type("shipped") == "event"

    def test_unknown_defaults_to_multi(self):
        assert get_predicate_type("invented_by") == "multi_valued"


# ── Validation ────────────────────────────────────────────────

class TestValidateTriple:
    def test_valid_triple(self):
        t = ExtractedTriple(subject="Brad", predicate="uses", object="Python")
        assert validate_triple(t) == []

    def test_empty_subject(self):
        t = ExtractedTriple(subject="", predicate="uses", object="Python")
        assert "empty subject" in validate_triple(t)

    def test_empty_predicate(self):
        t = ExtractedTriple(subject="Brad", predicate="", object="Python")
        assert "empty predicate" in validate_triple(t)

    def test_subject_equals_object(self):
        t = ExtractedTriple(subject="Brad", predicate="knows", object="Brad")
        assert "subject equals object" in validate_triple(t)

    def test_low_confidence(self):
        t = ExtractedTriple(
            subject="Brad", predicate="uses", object="Python", confidence=0.1
        )
        issues = validate_triple(t)
        assert any("confidence" in i for i in issues)

    def test_long_entity_name(self):
        t = ExtractedTriple(
            subject="x" * 201, predicate="uses", object="Python"
        )
        issues = validate_triple(t)
        assert any("too long" in i for i in issues)


# ── LLM Response Parsing ─────────────────────────────────────

class TestParseLlmResponse:
    def test_valid_json(self):
        response = json.dumps([{
            "subject": "Brad",
            "predicate": "uses",
            "object": "Python",
            "confidence": 0.9,
        }])
        triples = parse_llm_response(response)
        assert len(triples) == 1
        assert triples[0].subject == "Brad"
        assert triples[0].predicate == "uses"
        assert triples[0].object == "Python"

    def test_markdown_fences_stripped(self):
        response = "```json\n" + json.dumps([{
            "subject": "Brad",
            "predicate": "lives_in",
            "object": "Denver",
        }]) + "\n```"
        triples = parse_llm_response(response)
        assert len(triples) == 1

    def test_empty_array(self):
        assert parse_llm_response("[]") == []

    def test_invalid_json(self):
        assert parse_llm_response("not json at all") == []

    def test_normalizes_predicate(self):
        response = json.dumps([{
            "subject": "Brad",
            "predicate": "works for",
            "object": "Acme",
        }])
        triples = parse_llm_response(response)
        assert triples[0].predicate == "employed_by"

    def test_evidence_span_truncated(self):
        response = json.dumps([{
            "subject": "Brad",
            "predicate": "uses",
            "object": "SQLite",
            "evidence_span": "x" * 300,
        }])
        triples = parse_llm_response(response)
        assert len(triples[0].evidence_span) <= 200

    def test_negation_flag(self):
        response = json.dumps([{
            "subject": "Brad",
            "predicate": "uses",
            "object": "Postgres",
            "is_negation": True,
        }])
        triples = parse_llm_response(response)
        assert triples[0].is_negation is True

    def test_skips_non_dict_items(self):
        response = json.dumps(["not a dict", {"subject": "A", "predicate": "b", "object": "C"}])
        triples = parse_llm_response(response)
        assert len(triples) == 1

    def test_not_a_list(self):
        assert parse_llm_response('{"key": "value"}') == []


# ── ExtractionResult ──────────────────────────────────────────

class TestExtractionResult:
    def test_defaults(self):
        r = ExtractionResult(reflection_id="abc")
        assert r.total_added == 0
        assert r.total_superseded == 0

    def test_counts(self):
        r = ExtractionResult(
            reflection_id="abc",
            triples_added=[{"id": "1"}, {"id": "2"}],
            triples_superseded=[{"id": "3"}],
        )
        assert r.total_added == 2
        assert r.total_superseded == 1


# ── KGExtractor Integration ──────────────────────────────────

class TestKGExtractorNoLLM:
    def test_no_llm_returns_error(self):
        extractor = KGExtractor(store=object(), llm_caller=None)
        result = extractor.extract_from_reflection("abc", "Brad uses Python for all projects")
        assert "no LLM caller" in result.errors[0]

    def test_short_content_skipped(self):
        extractor = KGExtractor(store=object(), llm_caller=lambda p: "[]")
        result = extractor.extract_from_reflection("abc", "short")
        assert "too short" in result.errors[0]


class TestKGExtractorWithMockLLM:
    """Test the full pipeline with a mock LLM and mock store."""

    def _make_mock_store(self):
        """Create a mock store that records calls."""
        class MockStore:
            def __init__(self):
                self.added = []
                self.invalidated = []
                self.queries = []

            def kg_add(self, **kwargs):
                result = {"id": f"t{len(self.added)}", **kwargs}
                self.added.append(result)
                return result

            def kg_invalidate(self, subject, predicate, obj, valid_to=""):
                self.invalidated.append({
                    "subject": subject, "predicate": predicate,
                    "object": obj, "valid_to": valid_to,
                })
                return 1

            def kg_query(self, entity="", predicate="", include_expired=False, limit=10):
                return self.queries  # Pre-populated for conflict tests

        return MockStore()

    def test_basic_extraction(self):
        llm_response = json.dumps([{
            "subject": "Brad",
            "predicate": "uses",
            "object": "Python",
            "confidence": 0.9,
            "evidence_span": "Brad uses Python for everything",
        }])
        store = self._make_mock_store()
        extractor = KGExtractor(store=store, llm_caller=lambda p: llm_response)

        result = extractor.extract_from_reflection("ref1", "Brad uses Python for everything")
        assert result.total_added == 1
        assert store.added[0]["subject"] == "Brad"
        assert store.added[0]["extraction_method"] == "auto_llm"

    def test_negation_invalidates(self):
        llm_response = json.dumps([{
            "subject": "Brad",
            "predicate": "uses",
            "object": "Postgres",
            "is_negation": True,
            "confidence": 0.9,
        }])
        store = self._make_mock_store()
        extractor = KGExtractor(store=store, llm_caller=lambda p: llm_response)

        result = extractor.extract_from_reflection("ref1", "Brad stopped using Postgres")
        assert result.total_added == 0
        assert result.total_superseded == 1
        assert store.invalidated[0]["object"] == "Postgres"

    def test_functional_conflict_supersedes(self):
        llm_response = json.dumps([{
            "subject": "Brad",
            "predicate": "lives_in",
            "object": "Denver",
            "confidence": 0.9,
            "valid_from": "2026-01",
        }])
        store = self._make_mock_store()
        # Pre-populate existing conflicting triple
        store.queries = [{
            "subject": "Brad",
            "predicate": "lives_in",
            "object": "San Francisco",
            "valid_from": "2020-01",
        }]
        extractor = KGExtractor(store=store, llm_caller=lambda p: llm_response)

        result = extractor.extract_from_reflection("ref1", "Brad moved to Denver in Jan 2026")
        assert result.total_added == 1
        assert result.total_superseded == 1
        assert store.invalidated[0]["object"] == "San Francisco"
        assert store.added[0]["obj"] == "Denver"

    def test_functional_conflict_temporal_ordering(self):
        """Older facts should NOT supersede newer ones on reprocessing."""
        llm_response = json.dumps([{
            "subject": "Brad",
            "predicate": "lives_in",
            "object": "San Francisco",
            "confidence": 0.9,
            "valid_from": "2020-01",
        }])
        store = self._make_mock_store()
        # Existing newer fact
        store.queries = [{
            "subject": "Brad",
            "predicate": "lives_in",
            "object": "Denver",
            "valid_from": "2026-01",
        }]
        extractor = KGExtractor(store=store, llm_caller=lambda p: llm_response)

        result = extractor.extract_from_reflection("ref1", "Old memory: Brad lived in SF back in 2020")
        # Should NOT supersede Denver — SF is older
        assert result.total_added == 0
        assert result.total_superseded == 0
        assert len(result.triples_skipped) == 1
        assert "older than existing" in result.triples_skipped[0]["reason"]
        assert len(store.invalidated) == 0

    def test_dedupe_skips_existing_active(self):
        """Duplicate active triples should be skipped, not inserted twice."""
        llm_response = json.dumps([{
            "subject": "Brad",
            "predicate": "uses",
            "object": "Python",
            "confidence": 0.9,
        }])
        store = self._make_mock_store()
        # Pre-populate — same triple already active
        store.queries = [{
            "subject": "Brad",
            "predicate": "uses",
            "object": "Python",
        }]
        extractor = KGExtractor(store=store, llm_caller=lambda p: llm_response)

        result = extractor.extract_from_reflection("ref1", "Brad uses Python for all his projects")
        assert result.total_added == 0
        assert len(result.triples_skipped) == 1
        assert "duplicate" in result.triples_skipped[0]["reason"]

    def test_multi_valued_no_conflict(self):
        llm_response = json.dumps([{
            "subject": "Brad",
            "predicate": "uses",
            "object": "Rust",
            "confidence": 0.9,
        }])
        store = self._make_mock_store()
        # Even with existing "uses" triples, multi-valued shouldn't invalidate
        store.queries = [{
            "subject": "Brad",
            "predicate": "uses",
            "object": "Python",
        }]
        extractor = KGExtractor(store=store, llm_caller=lambda p: llm_response)

        result = extractor.extract_from_reflection("ref1", "Brad also uses Rust now")
        assert result.total_added == 1
        assert result.total_superseded == 0
        assert len(store.invalidated) == 0

    def test_low_confidence_skipped(self):
        llm_response = json.dumps([{
            "subject": "Brad",
            "predicate": "uses",
            "object": "Java",
            "confidence": 0.2,
        }])
        store = self._make_mock_store()
        extractor = KGExtractor(store=store, llm_caller=lambda p: llm_response)

        result = extractor.extract_from_reflection("ref1", "Brad might use Java?")
        assert result.total_added == 0
        assert len(result.triples_skipped) == 1

    def test_empty_extraction(self):
        store = self._make_mock_store()
        extractor = KGExtractor(store=store, llm_caller=lambda p: "[]")

        result = extractor.extract_from_reflection("ref1", "Just a normal day, nothing special.")
        assert result.total_added == 0
        assert len(result.errors) == 0

    def test_batch_extraction(self):
        responses = iter([
            json.dumps([{"subject": "A", "predicate": "knows", "object": "B", "confidence": 0.9}]),
            json.dumps([]),
            json.dumps([{"subject": "C", "predicate": "uses", "object": "D", "confidence": 0.8}]),
        ])
        store = self._make_mock_store()
        extractor = KGExtractor(store=store, llm_caller=lambda p: next(responses))

        reflections = [
            {"id": "r1", "content": "A knows B from work on the PinkyBot project"},
            {"id": "r2", "content": "The weather is nice today, nothing special happening"},
            {"id": "r3", "content": "C uses D for testing across all environments"},
        ]
        results = extractor.extract_batch(reflections)
        assert len(results) == 3
        assert results[0].total_added == 1
        assert results[1].total_added == 0
        assert results[2].total_added == 1

    def test_llm_failure_captured(self):
        def failing_llm(prompt):
            raise RuntimeError("API timeout")

        store = self._make_mock_store()
        extractor = KGExtractor(store=store, llm_caller=failing_llm)

        result = extractor.extract_from_reflection("ref1", "Some memory content here")
        assert len(result.errors) == 1
        assert "API timeout" in result.errors[0]


# ── Predicate Sets Sanity ─────────────────────────────────────

class TestPredicateSets:
    def test_no_overlap_functional_multi(self):
        assert FUNCTIONAL_PREDICATES.isdisjoint(MULTI_VALUED_PREDICATES)

    def test_no_overlap_functional_event(self):
        assert FUNCTIONAL_PREDICATES.isdisjoint(EVENT_PREDICATES)

    def test_no_overlap_multi_event(self):
        assert MULTI_VALUED_PREDICATES.isdisjoint(EVENT_PREDICATES)
