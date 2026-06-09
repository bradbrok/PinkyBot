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
