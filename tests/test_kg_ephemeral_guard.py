"""Tests for the write-time KG ephemeral-entity guard (#153 step-2).

Covers the shared detector (pure), the store choke-point, the MCP tool path,
the extractor rejection-routing, the flag kill-switch, and rejection logging.
"""
from __future__ import annotations

import json

import pytest

from pinky_memory.embeddings import NoOpEmbeddingClient
from pinky_memory.ephemeral_guard import (
    guard_enabled,
    is_ephemeral,
    is_ephemeral_entity,
)
from pinky_memory.kg_extractor import KGExtractor
from pinky_memory.server import create_server
from pinky_memory.store import ReflectionStore


def _tools(srv):
    return {t.name: t.fn for t in srv._tool_manager.list_tools()}


def _entity_exists(store, name) -> bool:
    row = store._conn.execute(
        "SELECT 1 FROM kg_entities WHERE name = ?", (name,)
    ).fetchone()
    return row is not None


@pytest.fixture(autouse=True)
def _clean_guard_env(monkeypatch):
    """Start every test with the guard at its default (ON) and the default log
    path (next to the temp db), so ambient env can't leak between tests."""
    monkeypatch.delenv("PINKY_KG_EPHEMERAL_GUARD", raising=False)
    monkeypatch.delenv("PINKY_KG_EPHEMERAL_GUARD_LOG", raising=False)


@pytest.fixture
def store(tmp_path):
    s = ReflectionStore(db_path=str(tmp_path / "mem.db"))
    yield s
    s.close()


@pytest.fixture
def srv(store):
    return create_server(store, NoOpEmbeddingClient())


# Pure-ID / version-stamp ephemerals — MUST be caught.
EPHEMERAL_POSITIVES = [
    "PR #635", "issue #501", "task #70", "release 26.05.094", "26.05.070",
    "bradbrok/PinkyBot#336", "tod-dashboard Issue #66", "PinkyBot #472-479",
    "PinkyBot 26.05.093", "Release 26.05.089",
]
# Legit named/descriptive entities — MUST pass (intentional misses).
LEGIT_NEGATIVES = [
    "PR #124 period foundation", "Codex CLI 0.125.0", "Claude Code", "PinkyBot",
    "tod-dashboard Issue #66 follow-up", "Brad", "SQLite", "Python 3.12",
    "Postgres 13.0",
]


class TestIsEphemeralEntity:
    @pytest.mark.parametrize("name", EPHEMERAL_POSITIVES)
    def test_positives(self, name):
        assert is_ephemeral_entity(name) is True

    @pytest.mark.parametrize("name", LEGIT_NEGATIVES)
    def test_false_positive_guards(self, name):
        assert is_ephemeral_entity(name) is False

    @pytest.mark.parametrize("name", ["  PR #635  ", "pr #635", "PR#635", "Release 26.05.089"])
    def test_whitespace_and_case(self, name):
        assert is_ephemeral_entity(name) is True

    def test_empty_and_none(self):
        assert is_ephemeral_entity("") is False
        assert is_ephemeral_entity(None) is False

    def test_sweep_import_parity(self):
        # scripts/kg_ephemeral_sweep.py imports `is_ephemeral`; it MUST be the
        # exact same object so the guard and sweep can never drift.
        assert is_ephemeral is is_ephemeral_entity


class TestGuardEnabled:
    @pytest.mark.parametrize("val", ["0", "false", "off", "no", "FALSE", "Off"])
    def test_disabled(self, val, monkeypatch):
        monkeypatch.setenv("PINKY_KG_EPHEMERAL_GUARD", val)
        assert guard_enabled() is False

    @pytest.mark.parametrize("val", ["1", "true", "on", "yes"])
    def test_enabled(self, val, monkeypatch):
        monkeypatch.setenv("PINKY_KG_EPHEMERAL_GUARD", val)
        assert guard_enabled() is True

    def test_default_enabled(self):
        assert guard_enabled() is True


class TestStoreGuard:
    def test_rejects_ephemeral_subject(self, store):
        result = store.kg_add(subject="PR #635", predicate="relates_to", obj="feature")
        assert result["rejected"] is True
        assert result["id"] is None
        # No triple and no orphan entity for either endpoint.
        assert store.kg_query(entity="PR #635") == []
        assert store.kg_query(entity="feature") == []
        assert not _entity_exists(store, "PR #635")
        assert not _entity_exists(store, "feature")

    def test_rejects_ephemeral_object(self, store):
        result = store.kg_add(subject="Brad", predicate="filed", obj="issue #501")
        assert result["rejected"] is True
        # Guard returns before _ensure_entity, so the legit subject is also
        # never minted — the whole triple is rejected atomically.
        assert not _entity_exists(store, "issue #501")
        assert not _entity_exists(store, "Brad")

    def test_allows_legit(self, store):
        result = store.kg_add(subject="Brad", predicate="uses", obj="SQLite")
        assert result.get("rejected") is not True
        assert result["id"] is not None
        assert len(store.kg_query(entity="Brad")) == 1

    def test_allows_descriptive(self, store):
        # Anchored fullmatch: any descriptive payload defeats the pattern.
        result = store.kg_add(
            subject="PR #124 period foundation", predicate="status", obj="merged"
        )
        assert result.get("rejected") is not True
        assert len(store.kg_query(entity="PR #124 period foundation")) == 1

    def test_rejection_dict_shape(self, store):
        result = store.kg_add(subject="PR #635", predicate="x", obj="y")
        for key in ("id", "subject", "predicate", "object", "valid_from", "extraction_method"):
            assert key in result, f"missing success-shape key: {key}"
        assert result["rejected"] is True
        assert result["reason"] == "ephemeral_entity"
        assert result["extraction_method"] == "rejected"

    def test_flag_off_allows(self, store, monkeypatch):
        monkeypatch.setenv("PINKY_KG_EPHEMERAL_GUARD", "0")
        result = store.kg_add(subject="PR #635", predicate="relates_to", obj="feature")
        assert result.get("rejected") is not True
        assert result["id"] is not None
        assert len(store.kg_query(entity="PR #635")) == 1


class TestMcpKgAddGuard:
    def test_mcp_kg_add_rejects(self, srv):
        result = json.loads(_tools(srv)["kg_add"](subject="PR #635", predicate="x", object="y"))
        assert result["rejected"] is True

    def test_mcp_kg_add_allows(self, srv):
        result = json.loads(
            _tools(srv)["kg_add"](subject="Brad", predicate="uses", object="SQLite")
        )
        assert result.get("rejected") is not True
        assert result["id"] is not None


class TestExtractorGuard:
    def test_ephemeral_routed_to_skipped(self, store):
        llm_response = json.dumps([{
            "subject": "PR #700", "predicate": "relates_to", "object": "feature x",
            "confidence": 0.9, "evidence_span": "PR #700 relates to feature x",
        }])
        extractor = KGExtractor(store=store, llm_caller=lambda p: llm_response)
        result = extractor.extract_from_reflection("ref1", "PR #700 relates to feature x")
        assert result.total_added == 0
        assert any("ephemeral" in s.get("reason", "") for s in result.triples_skipped)
        # Nothing reached the real store.
        assert store.kg_query(entity="PR #700") == []
        assert not _entity_exists(store, "PR #700")


class TestRejectionLogging:
    def test_rejection_logged(self, store, tmp_path, monkeypatch):
        log_path = tmp_path / "guard.jsonl"
        monkeypatch.setenv("PINKY_KG_EPHEMERAL_GUARD_LOG", str(log_path))
        store.kg_add(subject="PR #635", predicate="relates_to", obj="feature")
        lines = log_path.read_text().strip().splitlines()
        assert len(lines) == 1
        rec = json.loads(lines[0])
        assert rec["event"] == "write_rejected"
        assert rec["role"] == "subject"
        assert rec["name"] == "PR #635"
        assert rec["source"] == "kg_add"
        assert rec["reason"] == "ephemeral_entity"

    def test_log_failure_does_not_break_write(self, store, tmp_path, monkeypatch):
        # Parent of the log dir is a regular file -> makedirs/open raise; the
        # rejection must still apply and kg_add must not propagate the error.
        a_file = tmp_path / "afile"
        a_file.write_text("x")
        monkeypatch.setenv("PINKY_KG_EPHEMERAL_GUARD_LOG", str(a_file / "sub" / "g.jsonl"))
        result = store.kg_add(subject="PR #635", predicate="x", obj="y")
        assert result["rejected"] is True
