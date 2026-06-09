"""Fable 5 lands in the seeded model catalog, and the seed is idempotent.

Guards the additive-seed behavior: a new model added to ``_MODEL_SEEDS`` must
reach an already-populated DB (the early-return-on-non-empty bug this fixes
stranded new models on every deployed instance), and re-seeding must never
duplicate rows.
"""

from __future__ import annotations

import tempfile

from pinky_daemon.agent_registry import AgentRegistry


def _by_model_id(reg) -> dict:
    return {m["model_id"]: m for m in reg.list_models()}


def test_fable_5_seeded_with_1m_context_and_price():
    with tempfile.TemporaryDirectory() as d:
        reg = AgentRegistry(db_path=f"{d}/agents.db")
        models = _by_model_id(reg)
        assert "claude-fable-5" in models
        fable = models["claude-fable-5"]
        assert fable["is_1m"] == 1
        assert fable["context_window"] == 1_000_000
        assert fable["input_price"] == 10.0
        assert fable["output_price"] == 50.0


def test_seed_is_idempotent_and_additive():
    with tempfile.TemporaryDirectory() as d:
        reg = AgentRegistry(db_path=f"{d}/agents.db")
        before = len(reg.list_models())
        assert before > 0
        # Re-running the seed must not duplicate any rows (INSERT OR IGNORE).
        reg._seed_models()
        assert len(reg.list_models()) == before


def test_seed_propagates_new_model_to_populated_db():
    # The actual regression this PR fixes: a model added to _MODEL_SEEDS must
    # reach an ALREADY-seeded DB. Simulate a pre-Fable DB by deleting the row
    # from a populated table, then re-seed — Fable must reappear with no other
    # rows duplicated. Reverting to early-return-on-non-empty fails this test.
    with tempfile.TemporaryDirectory() as d:
        reg = AgentRegistry(db_path=f"{d}/agents.db")
        reg._db.execute("DELETE FROM models WHERE model_id = 'claude-fable-5'")
        reg._db.commit()
        assert "claude-fable-5" not in _by_model_id(reg)
        before = len(reg.list_models())
        reg._seed_models()
        after = _by_model_id(reg)
        assert "claude-fable-5" in after
        assert len(after) == before + 1


def test_fable_5_in_1m_models_set():
    # 1M-context routing (SDK under-reports as 200k) must include Fable.
    from pinky_daemon.streaming_session import _1M_MODELS

    assert "claude-fable-5" in _1M_MODELS
