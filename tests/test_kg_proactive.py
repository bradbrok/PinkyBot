"""Tests for proactive KG surfacing (#682, PR2).

Two layers:
  * ``pinky_memory.kg_reason`` pure functions — materiality policy,
    fingerprints, selection (filter/dedup/cap), and digest formatting.
  * ``DreamRunner._surface_kg_insights`` + ``get_kg_insights`` — the
    flag-gated, read-only, deduped, once-per-cycle delivery wiring.

Invariants (designed with Murzik):
  * Flag OFF (default) ⇒ surfacing is completely inert.
  * Contradictions on high-value predicate domains are always material;
    softer predicates need a high-value entity.
  * Changes: ended/superseded are material on high-value domain/entity;
    additions only when high-value AND confidence >= 0.7.
  * Fingerprints suppress repeats across nights; top-5 cap bounds volume.
  * Nothing is ever written to the KG; the owner is never auto-messaged.
"""

from __future__ import annotations

import os
import tempfile
from types import SimpleNamespace

import pytest

from pinky_daemon.dream_runner import DreamRunner
from pinky_memory import kg_reason
from pinky_memory.store import ReflectionStore

# ── Builders ────────────────────────────────────────────────────────────────

def _contradiction(subject, predicate, values, winner=None):
    return {
        "subject": subject,
        "predicate": predicate,
        "active_values": list(values),
        "conflicting": [
            {"id": i, "object": v, "valid_from": "", "confidence": 1.0,
             "source_reflection_id": ""}
            for i, v in enumerate(values)
        ],
        "suggested_winner": {"object": winner or values[-1], "reason": "advisory"},
    }


def _change(_id, subject, predicate, obj, *, confidence=1.0, status="active",
            valid_to=None):
    return {
        "id": _id, "subject": subject, "predicate": predicate, "object": obj,
        "valid_from": "2026-06-01", "valid_to": valid_to,
        "confidence": confidence, "status": status, "source_reflection_id": "",
    }


# ── Fingerprints ────────────────────────────────────────────────────────────

class TestFingerprints:
    def test_contradiction_fp_order_independent(self):
        a = _contradiction("Brad", "timezone", ["PST", "EST"])
        b = _contradiction("Brad", "timezone", ["EST", "PST"])
        assert kg_reason.contradiction_fingerprint(a) == \
            kg_reason.contradiction_fingerprint(b)

    def test_contradiction_fp_casefolded(self):
        a = _contradiction("brad", "TimeZone", ["pst", "est"])
        b = _contradiction("Brad", "timezone", ["PST", "EST"])
        assert kg_reason.contradiction_fingerprint(a) == \
            kg_reason.contradiction_fingerprint(b)

    def test_contradiction_fp_changes_with_values(self):
        a = _contradiction("Brad", "timezone", ["PST", "EST"])
        b = _contradiction("Brad", "timezone", ["PST", "EST", "CST"])
        assert kg_reason.contradiction_fingerprint(a) != \
            kg_reason.contradiction_fingerprint(b)

    def test_change_fp_distinct_by_bucket(self):
        ch = _change(7, "Brad", "lives_in", "Denver")
        assert kg_reason.change_fingerprint(ch, "added") != \
            kg_reason.change_fingerprint(ch, "ended")

    def test_change_fp_stable_by_id(self):
        ch = _change(7, "Brad", "lives_in", "Denver")
        assert kg_reason.change_fingerprint(ch, "added") == "change:added:7"


# ── Materiality ─────────────────────────────────────────────────────────────

class TestMateriality:
    def test_contradiction_material_predicate_always(self):
        c = _contradiction("SomeRandomPerson", "timezone", ["PST", "EST"])
        assert kg_reason.is_material_contradiction(c, set()) is True

    def test_contradiction_soft_predicate_needs_high_value(self):
        c = _contradiction("Stranger", "primary_editor", ["vim", "emacs"])
        assert kg_reason.is_material_contradiction(c, set()) is False
        assert kg_reason.is_material_contradiction(c, {"stranger"}) is True

    def test_change_ended_material_on_material_predicate(self):
        ch = _change(1, "TOD", "deployed_to", "Contabo", valid_to="2026-06-05",
                     status="ended")
        assert kg_reason.is_material_change(ch, "ended", set()) is True

    def test_change_added_low_conf_ignored(self):
        ch = _change(2, "Brad", "lives_in", "Denver", confidence=0.5)
        assert kg_reason.is_material_change(ch, "added", {"brad"}) is False

    def test_change_added_high_conf_high_value(self):
        ch = _change(3, "Brad", "lives_in", "Denver", confidence=0.9)
        assert kg_reason.is_material_change(ch, "added", {"brad"}) is True

    def test_change_added_high_conf_but_not_high_value(self):
        ch = _change(4, "Nobody", "lives_in", "Denver", confidence=0.9)
        assert kg_reason.is_material_change(ch, "added", {"brad"}) is False


# ── Selection ───────────────────────────────────────────────────────────────

class TestSelectInsights:
    def test_dedup_against_seen(self):
        c = _contradiction("Brad", "timezone", ["PST", "EST"])
        fp = kg_reason.contradiction_fingerprint(c)
        sel = kg_reason.select_insights([c], {}, {fp}, set())
        assert sel["contradictions"] == []
        assert sel["fingerprints"] == []

    def test_contradictions_first_then_cap(self):
        contras = [
            _contradiction(f"Person{i}", "timezone", ["PST", "EST"])
            for i in range(7)
        ]
        sel = kg_reason.select_insights(contras, {}, set(), set(), cap=5)
        assert len(sel["contradictions"]) == 5
        assert len(sel["changes"]) == 0
        assert len(sel["fingerprints"]) == 5

    def test_changes_fill_remaining_capacity(self):
        contras = [_contradiction("Brad", "timezone", ["PST", "EST"])]
        changes = {
            "ended": [
                _change(i, "TOD", "deployed_to", f"Host{i}", valid_to="2026-06-05",
                        status="ended")
                for i in range(10)
            ],
            "added": [],
        }
        sel = kg_reason.select_insights(contras, changes, set(), set(), cap=5)
        assert len(sel["contradictions"]) == 1
        assert len(sel["changes"]) == 4  # filled to the cap of 5
        assert all(ch["_bucket"] == "ended" for ch in sel["changes"])

    def test_immaterial_filtered_out(self):
        contras = [_contradiction("Stranger", "primary_editor", ["vim", "emacs"])]
        sel = kg_reason.select_insights(contras, {}, set(), set())
        assert sel["contradictions"] == []

    def test_within_run_dedup(self):
        c = _contradiction("Brad", "timezone", ["PST", "EST"])
        sel = kg_reason.select_insights([c, c], {}, set(), set())
        assert len(sel["contradictions"]) == 1


# ── Formatting ──────────────────────────────────────────────────────────────

class TestFormatDigest:
    def test_empty_when_nothing(self):
        assert kg_reason.format_digest(
            {"contradictions": [], "changes": []}) == ""

    def test_min_items_threshold(self):
        sel = {"contradictions": [_contradiction("Brad", "timezone", ["PST", "EST"])],
               "changes": []}
        assert kg_reason.format_digest(sel, min_items=2) == ""
        assert kg_reason.format_digest(sel, min_items=1) != ""

    def test_renders_contradiction_and_change(self):
        sel = {
            "contradictions": [
                _contradiction("Brad", "timezone", ["PST", "EST"], winner="PST")],
            "changes": [
                {**_change(9, "TOD", "deployed_to", "Contabo",
                           valid_to="2026-06-05", status="ended"),
                 "_bucket": "ended"}],
        }
        out = kg_reason.format_digest(sel)
        assert "no auto-fix applied" in out.lower()
        assert "Brad" in out and "timezone" in out
        assert "PST vs EST" in out or "EST vs PST" in out
        assert "advisory winner: PST" in out
        assert "[ended] TOD deployed_to Contabo" in out


# ── DreamRunner integration ─────────────────────────────────────────────────

@pytest.fixture
def dream_env(tmp_path, monkeypatch):
    """A DreamRunner with a temp dream_state.db and an agent KG seeded with a
    live functional contradiction (Brad timezone PST vs EST)."""
    dream_db = str(tmp_path / "dream_state.db")
    work_dir = tmp_path / "agent"
    (work_dir / "data").mkdir(parents=True)
    mem_db = str(work_dir / "data" / "memory.db")

    store = ReflectionStore(db_path=mem_db)
    store.kg_add("Brad", "timezone", "PST", valid_from="2026-01-01")
    store.kg_add("Brad", "timezone", "EST", valid_from="2026-06-01")

    runner = DreamRunner(
        db_path=dream_db,
        owner_provider=lambda: {"name": "Bradley Brockman"},
    )
    agent_config = SimpleNamespace(working_dir=str(work_dir))
    # A dream row must exist for the digest UPDATE to land + the window check.
    runner._save_state("barsik", "test dream summary")
    return runner, agent_config


class TestSurfaceKGInsights:
    def test_flag_off_is_inert(self, dream_env, monkeypatch):
        runner, cfg = dream_env
        monkeypatch.delenv("PINKY_KG_PROACTIVE", raising=False)
        assert runner._surface_kg_insights("barsik", cfg) == ""
        assert runner.get_kg_insights("barsik") is None

    def test_flag_on_surfaces_contradiction(self, dream_env, monkeypatch):
        runner, cfg = dream_env
        monkeypatch.setenv("PINKY_KG_PROACTIVE", "1")
        digest = runner._surface_kg_insights("barsik", cfg)
        assert digest
        assert "timezone" in digest
        assert "PST" in digest and "EST" in digest

    def test_repeat_suppressed_second_run(self, dream_env, monkeypatch):
        runner, cfg = dream_env
        monkeypatch.setenv("PINKY_KG_PROACTIVE", "1")
        first = runner._surface_kg_insights("barsik", cfg)
        assert first
        second = runner._surface_kg_insights("barsik", cfg)
        assert second == ""  # same contradiction fingerprint already surfaced

    def test_get_kg_insights_once_only(self, dream_env, monkeypatch):
        runner, cfg = dream_env
        monkeypatch.setenv("PINKY_KG_PROACTIVE", "1")
        runner._surface_kg_insights("barsik", cfg)
        first = runner.get_kg_insights("barsik")
        assert first and "timezone" in first
        # Second read in the same cycle returns nothing (already delivered).
        assert runner.get_kg_insights("barsik") is None

    def test_surfacing_never_writes_kg(self, dream_env, monkeypatch):
        runner, cfg = dream_env
        monkeypatch.setenv("PINKY_KG_PROACTIVE", "1")
        mem_db = str(cfg.working_dir + "/data/memory.db")
        before = ReflectionStore(db_path=mem_db).kg_find_contradictions()
        runner._surface_kg_insights("barsik", cfg)
        after = ReflectionStore(db_path=mem_db).kg_find_contradictions()
        assert len(before) == len(after) == 1  # unchanged — read-only

    def test_missing_memory_db_is_safe(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PINKY_KG_PROACTIVE", "1")
        runner = DreamRunner(db_path=str(tmp_path / "ds.db"))
        cfg = SimpleNamespace(working_dir=str(tmp_path / "no_such_agent"))
        runner._save_state("ghost", "summary")
        assert runner._surface_kg_insights("ghost", cfg) == ""


def test_high_value_entities_from_owner(tmp_path):
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        runner = DreamRunner(
            db_path=path,
            owner_provider=lambda: {"name": "Bradley Brockman"},
        )
        hv = runner._high_value_entities()
        assert "bradley brockman" in hv
        assert "bradley" in hv and "brockman" in hv
    finally:
        os.unlink(path)


def test_high_value_entities_empty_without_provider(tmp_path):
    runner = DreamRunner(db_path=str(tmp_path / "ds.db"))
    assert runner._high_value_entities() == set()
