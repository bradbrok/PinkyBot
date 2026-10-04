"""Committed roster writes are visible to existing runtime consumers."""

from __future__ import annotations

import copy

import pytest

from pinky_daemon import runtime_model_catalog
from pinky_daemon.agent_registry import AgentRegistry
from pinky_daemon.analytics_store import AnalyticsStore
from pinky_daemon.pricing import RATE_TABLE, compute_turn_cost_usd, lookup_rate
from pinky_daemon.streaming_session import _1M_MODELS, is_1m_model
from tests._model_roster_local import SONNET, add, apply, document, model_row, new_model, release


@pytest.fixture
def registry(tmp_path):
    instance = AgentRegistry(db_path=str(tmp_path / "agents.db"))
    runtime_model_catalog.bind_registry(instance)
    try:
        yield instance
    finally:
        runtime_model_catalog.reset_for_tests()
        instance.close()


def test_all_five_rates_change_after_a_primed_lookup(registry):
    bare = SONNET.split("/", 1)[1]
    assert lookup_rate(bare) == RATE_TABLE[bare]
    static = copy.deepcopy(RATE_TABLE)
    value = document()
    model_row(value)["pricing"].update(
        input=7.0, output=31.0, cached_input=0.7, cache_write_5m=8.0, cache_write_1h=12.0
    )
    apply(registry, value)
    assert lookup_rate(bare + "[1m]") == {
        "input": 7.0,
        "output": 31.0,
        "cache_read": 0.7,
        "cache_write_5m": 8.0,
        "cache_write_1h": 12.0,
    }
    assert compute_turn_cost_usd(
        bare,
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        cache_read_tokens=1_000_000,
        cache_creation_5m_tokens=1_000_000,
        cache_creation_1h_tokens=1_000_000,
    ) == pytest.approx(58.7)
    assert RATE_TABLE == static


def test_cached_missing_model_is_visible_after_insert(registry):
    assert lookup_rate("roster-added-model") is None
    value = document()
    value["models"].append(new_model())
    apply(registry, value)
    assert lookup_rate("roster-added-model")["input"] == model_row(value)["pricing"]["input"]


def test_one_million_decision_changes_without_recreating_consumers(registry):
    bare = SONNET.split("/", 1)[1]
    assert is_1m_model(bare + "[1m]")
    assert bare in runtime_model_catalog.get_1m_models()
    static = set(_1M_MODELS)
    value = document()
    model_row(value).update(context_window=800_000, is_1m=False)
    apply(registry, value)
    assert not is_1m_model(bare + "[1m]")
    assert bare not in registry.get_1m_models()
    assert bare not in runtime_model_catalog.get_1m_models()
    assert _1M_MODELS == static


def test_dry_run_does_not_invalidate_primed_runtime_cache(registry, monkeypatch):
    bare = SONNET.split("/", 1)[1]
    lookup_rate(bare)
    is_1m_model(bare)
    calls = []
    monkeypatch.setattr(runtime_model_catalog, "invalidate", lambda: calls.append(True))
    value = document()
    model_row(value)["pricing"]["input"] = 7.0
    apply(registry, value, dry_run=True)
    assert calls == []
    assert lookup_rate(bare)["input"] == RATE_TABLE[bare]["input"]


def test_release_refreshes_price_and_context_cache(registry):
    bare = SONNET.split("/", 1)[1]
    row = model_row(document())
    row["pricing"]["input"] = 7.0
    row.update(context_window=800_000, is_1m=False)
    add(registry, row)
    assert lookup_rate(bare)["input"] == 7.0 and not is_1m_model(bare)
    apply(registry, document())
    release(registry, SONNET, ["input_price", "context_window"])
    assert lookup_rate(bare)["input"] == model_row(document())["pricing"]["input"]
    assert is_1m_model(bare)


def test_analytics_reprices_same_store_and_lifetime_ledger_is_untouched(registry, tmp_path):
    bare = SONNET.split("/", 1)[1]
    registry.register(
        "test-agent",
        model=bare,
        working_dir=str(tmp_path / "workspace"),
        enabled=False,
        auto_start=False,
    )
    analytics = AnalyticsStore(str(tmp_path / "analytics.db"))
    analytics.ensure_session_fact(
        session_id="roster-session",
        agent_name="test-agent",
        session_label="main",
        provider="anthropic",
        model=bare,
    )
    analytics.log_turn_usage(
        session_id="roster-session",
        agent_name="test-agent",
        turn_seq=1,
        provider="anthropic",
        model=bare,
        input_tokens=1_000_000,
        output_tokens=0,
        cached_input_tokens=0,
    )
    expected = model_row(document())["pricing"]["input"]
    assert analytics.get_overview(range_name="7d")["totals"]["cost_usd"] == expected
    analytics._pricing_table()
    registry.record_cost("test-agent", expected, input_tokens=1_000_000)
    ledger = registry._db.execute("SELECT * FROM agent_costs ORDER BY rowid").fetchall()
    value = document()
    model_row(value)["pricing"]["input"] = 7.0
    apply(registry, value)
    assert analytics.get_overview(range_name="7d")["totals"]["cost_usd"] == 7.0
    assert registry._db.execute("SELECT * FROM agent_costs ORDER BY rowid").fetchall() == ledger
    assert registry.get_total_lifetime_cost() == expected
