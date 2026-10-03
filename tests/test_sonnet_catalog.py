"""Sonnet catalog, context limits, pricing, and persisted-seed upgrades."""

import sqlite3
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest

from pinky_daemon import runtime_model_catalog
from pinky_daemon.agent_registry import AgentRegistry
from pinky_daemon.analytics_store import AnalyticsStore
from pinky_daemon.pricing import compute_turn_cost_usd, lookup_rate
from pinky_daemon.streaming_session import is_1m_model
from pinky_daemon.tmux_session import TmuxSession

_FIXTURES = Path(__file__).parent / "fixtures"
_SONNET_5_RATES = {
    "input": 2.0,
    "output": 10.0,
    "cache_read": 0.2,
    "cache_write_5m": 2.5,
    "cache_write_1h": 4.0,
}
_REGISTRY_PRICE_COLUMNS = (
    "input_price",
    "output_price",
    "cached_input_price",
    "cache_write_5m_price",
    "cache_write_1h_price",
)
_OLD_PRICES = (3.0, 15.0, 0.3, 3.75, 6.0)
_NEW_PRICES = (2.0, 10.0, 0.2, 2.5, 4.0)
_MODEL_IDS = ("claude-sonnet-5", "claude-sonnet-5-5")


def _pre_update_db(tmp_path, fixture_name):
    path = tmp_path / (fixture_name + ".db")
    with closing(sqlite3.connect(path)) as conn:
        conn.executescript((_FIXTURES / (fixture_name + ".sql")).read_text())
        conn.commit()
    return path


def _raw_window(model):
    session = object.__new__(TmuxSession)
    session._config = SimpleNamespace(model=model)
    return session._raw_max_tokens_for_model()


def _prices(row):
    assert row is not None
    return tuple(row[column] for column in _REGISTRY_PRICE_COLUMNS)


def _analytics_rows(store, model):
    with store._connect() as conn:
        return [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM analytics_model_pricing "
                "WHERE provider='anthropic' AND model=? ORDER BY id",
                (model,),
            )
        ]


@pytest.mark.parametrize("model", ["claude-sonnet-5-5", "claude-sonnet-5-5[1m]"])
def test_sonnet_55_tmux_raw_context_is_one_million(model):
    assert _raw_window(model) == 1_000_000


@pytest.mark.parametrize("model", ["claude-sonnet-5-5", "claude-sonnet-5-5[1m]"])
def test_sonnet_55_static_one_million_membership(model):
    assert is_1m_model(model) is True


@pytest.mark.parametrize("model", ["claude-sonnet-5-5", "claude-sonnet-5-5[1m]"])
def test_sonnet_55_static_rates(model):
    assert lookup_rate(model) == _SONNET_5_RATES


@pytest.mark.parametrize("model", ["claude-sonnet-5", "claude-sonnet-5[1m]"])
def test_sonnet_5_standard_rates(model):
    assert lookup_rate(model) == _SONNET_5_RATES


@pytest.mark.parametrize("model", _MODEL_IDS)
def test_sonnet_5_mixed_usage_cost(model):
    cost = compute_turn_cost_usd(
        model,
        input_tokens=1_000_000,
        output_tokens=100_000,
        cache_read_tokens=2_000_000,
        cache_creation_5m_tokens=40_000,
        cache_creation_1h_tokens=10_000,
    )
    assert cost == pytest.approx(3.54)


@pytest.mark.parametrize("model", ["claude-sonnet-4-6", "claude-sonnet-4-5", "claude-sonnet-4"])
def test_sonnet_4_rates_remain_unchanged(model):
    assert lookup_rate(model) == {
        "input": 3.0,
        "output": 15.0,
        "cache_read": 0.3,
        "cache_write_5m": 3.75,
        "cache_write_1h": 6.0,
    }


def test_fresh_catalog_has_current_sonnet_55(tmp_path):
    with closing(AgentRegistry(str(tmp_path / "registry.db"))) as registry:
        row = registry.get_model("claude-sonnet-5-5")
        assert row is not None
        assert row["display_name"] == "Claude Sonnet 5.5"
        assert row["tier"] == "sonnet"
        assert row["context_window"] == 1_000_000
        assert row["is_1m"] == 1
        assert row["supports_thinking"] == 1
        assert _prices(row) == _NEW_PRICES
        assert "Current Sonnet" in row["description"]
        assert "128K" in row["description"]
        assert "adaptive" in row["description"]
        assert "effort defaults to high" in row["description"]
        assert "intro" not in row["description"].lower()
        older = registry.get_model("claude-sonnet-5")
        assert row["sort_order"] < older["sort_order"]


def test_fresh_catalog_sonnet_5_has_standard_rates_and_description(tmp_path):
    with closing(AgentRegistry(str(tmp_path / "registry.db"))) as registry:
        row = registry.get_model("claude-sonnet-5")
        assert _prices(row) == _NEW_PRICES
        assert "Current Sonnet" not in row["description"]
        assert "intro" not in row["description"].lower()
        assert "$2/$10" in row["description"]


def test_pre_update_registry_gains_sonnet_55_on_open(tmp_path):
    path = _pre_update_db(tmp_path, "sonnet_registry_pre_update")
    with closing(AgentRegistry(str(path))) as registry:
        row = registry.get_model("claude-sonnet-5-5")
        assert row is not None
        assert row["is_1m"] == 1
        assert row["context_window"] == 1_000_000
        assert _prices(row) == _NEW_PRICES


def test_pre_update_registry_corrects_all_sonnet_5_prices_on_open(tmp_path):
    path = _pre_update_db(tmp_path, "sonnet_registry_pre_update")
    with closing(AgentRegistry(str(path))) as registry:
        assert _prices(registry.get_model("claude-sonnet-5")) == _NEW_PRICES
        assert _prices(registry.get_model("claude-sonnet-4-6")) == _OLD_PRICES


def test_pre_update_registry_corrects_sonnet_5_description_on_open(tmp_path):
    path = _pre_update_db(tmp_path, "sonnet_registry_pre_update")
    with closing(AgentRegistry(str(path))) as registry:
        row = registry.get_model("claude-sonnet-5")
        assert "Current Sonnet" not in row["description"]
        assert "intro" not in row["description"].lower()
        assert "$2/$10" in row["description"]


def test_pre_update_registry_without_write_columns_gets_all_correct_rates(tmp_path):
    path = _pre_update_db(tmp_path, "sonnet_registry_pre_update")
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("ALTER TABLE models DROP COLUMN cache_write_5m_price")
        conn.execute("ALTER TABLE models DROP COLUMN cache_write_1h_price")
        conn.commit()
    with closing(AgentRegistry(str(path))) as registry:
        assert _prices(registry.get_model("claude-sonnet-5")) == _NEW_PRICES


@pytest.mark.parametrize("column", _REGISTRY_PRICE_COLUMNS)
def test_pre_update_registry_preserves_custom_price_rows(tmp_path, column):
    path = _pre_update_db(tmp_path, "sonnet_registry_pre_update")
    expected = list(_OLD_PRICES)
    expected[_REGISTRY_PRICE_COLUMNS.index(column)] = 9.0
    with closing(sqlite3.connect(path)) as conn:
        conn.execute(f"UPDATE models SET {column}=? WHERE model_id='claude-sonnet-5'", (9.0,))
        conn.commit()
    with closing(AgentRegistry(str(path))) as registry:
        assert _prices(registry.get_model("claude-sonnet-5")) == tuple(expected)


def test_pre_update_registry_preserves_custom_description(tmp_path):
    path = _pre_update_db(tmp_path, "sonnet_registry_pre_update")
    with closing(sqlite3.connect(path)) as conn:
        conn.execute(
            "UPDATE models SET description='Custom catalog note' WHERE model_id='claude-sonnet-5'"
        )
        conn.commit()
    with closing(AgentRegistry(str(path))) as registry:
        assert registry.get_model("claude-sonnet-5")["description"] == "Custom catalog note"
        assert _prices(registry.get_model("claude-sonnet-5")) == _NEW_PRICES


def test_pre_update_registry_second_startup_leaves_all_model_rows_unchanged(tmp_path):
    path = _pre_update_db(tmp_path, "sonnet_registry_pre_update")
    with closing(AgentRegistry(str(path))) as registry:
        before = registry.list_models(active_only=False)
    with closing(AgentRegistry(str(path))) as registry:
        assert registry.list_models(active_only=False) == before


@pytest.mark.parametrize("model", _MODEL_IDS)
def test_upgraded_registry_drives_runtime_rates_and_context(tmp_path, model):
    path = _pre_update_db(tmp_path, "sonnet_registry_pre_update")
    with closing(AgentRegistry(str(path))) as registry:
        runtime_model_catalog.bind_registry(registry)
        assert lookup_rate(model) == _SONNET_5_RATES
        assert is_1m_model(model) is True
        assert _raw_window(model) == 1_000_000


@pytest.mark.parametrize("model", _MODEL_IDS)
def test_fresh_analytics_sonnet_5_rates(tmp_path, model):
    store = AnalyticsStore(str(tmp_path / "analytics.db"))
    rows = _analytics_rows(store, model)
    assert len(rows) == 1
    assert tuple(
        rows[0][key]
        for key in ("input_usd_per_mtok", "output_usd_per_mtok", "cached_input_usd_per_mtok")
    ) == (2.0, 10.0, 0.2)


def test_pre_update_analytics_adds_sonnet_55(tmp_path):
    path = _pre_update_db(tmp_path, "sonnet_analytics_pre_update")
    store = AnalyticsStore(str(path))
    rows = _analytics_rows(store, "claude-sonnet-5-5")
    assert len(rows) == 1
    assert rows[0]["input_usd_per_mtok"] == 2.0
    assert rows[0]["output_usd_per_mtok"] == 10.0
    assert rows[0]["cached_input_usd_per_mtok"] == 0.2


def test_pre_update_analytics_corrects_sonnet_5_seed(tmp_path):
    path = _pre_update_db(tmp_path, "sonnet_analytics_pre_update")
    store = AnalyticsStore(str(path))
    rows = _analytics_rows(store, "claude-sonnet-5")
    assert len(rows) == 1
    assert tuple(
        rows[0][key]
        for key in ("input_usd_per_mtok", "output_usd_per_mtok", "cached_input_usd_per_mtok")
    ) == (2.0, 10.0, 0.2)


@pytest.mark.parametrize(
    "column", ["input_usd_per_mtok", "output_usd_per_mtok", "cached_input_usd_per_mtok"]
)
def test_pre_update_analytics_preserves_custom_seed_prices(tmp_path, column):
    path = _pre_update_db(tmp_path, "sonnet_analytics_pre_update")
    with closing(sqlite3.connect(path)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute(
            f"UPDATE analytics_model_pricing SET {column}=9.0 WHERE model='claude-sonnet-5'"
        )
        expected = dict(
            conn.execute(
                "SELECT * FROM analytics_model_pricing WHERE model='claude-sonnet-5'"
            ).fetchone()
        )
        conn.commit()
    store = AnalyticsStore(str(path))
    before = _analytics_rows(store, "claude-sonnet-5")
    assert before == [expected]
    AnalyticsStore(str(path))
    assert _analytics_rows(store, "claude-sonnet-5") == before


@pytest.mark.parametrize(
    "change",
    [
        "notes='operator override'",
        "effective_from='2026-06-09T00:00:00Z'",
        "effective_to='2026-08-31T23:59:59Z'",
    ],
)
def test_pre_update_analytics_preserves_operator_and_historical_rows(tmp_path, change):
    path = _pre_update_db(tmp_path, "sonnet_analytics_pre_update")
    with closing(sqlite3.connect(path)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute(f"UPDATE analytics_model_pricing SET {change} WHERE model='claude-sonnet-5'")
        before = dict(
            conn.execute(
                "SELECT * FROM analytics_model_pricing WHERE model='claude-sonnet-5'"
            ).fetchone()
        )
        conn.commit()
    store = AnalyticsStore(str(path))
    assert _analytics_rows(store, "claude-sonnet-5") == [before]


def test_pre_update_analytics_second_startup_leaves_all_pricing_rows_unchanged(tmp_path):
    path = _pre_update_db(tmp_path, "sonnet_analytics_pre_update")
    store = AnalyticsStore(str(path))
    with store._connect() as conn:
        before = [
            tuple(row) for row in conn.execute("SELECT * FROM analytics_model_pricing ORDER BY id")
        ]
    AnalyticsStore(str(path))
    with store._connect() as conn:
        after = [
            tuple(row) for row in conn.execute("SELECT * FROM analytics_model_pricing ORDER BY id")
        ]
    assert after == before
