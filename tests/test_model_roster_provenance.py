"""Field ownership migration and supported local model writes."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing

import pytest

from pinky_daemon.agent_registry import AgentRegistry
from tests._model_roster_local import (
    CONTEXT_FIELDS,
    FIELDS,
    MARKER,
    SONNET,
    add,
    apply,
    document,
    flat,
    legacy_db,
    model_row,
    new_model,
    owned,
    release,
    snapshot,
)


@pytest.fixture
def registry(tmp_path):
    instance = AgentRegistry(db_path=str(tmp_path / "agents.db"))
    try:
        yield instance
    finally:
        instance.close()


def test_columns_are_added_even_when_write_rate_columns_already_exist(tmp_path):
    path = legacy_db(tmp_path / "legacy.db")
    instance = AgentRegistry(db_path=str(path))
    try:
        columns = {row[1]: row for row in instance._db.execute("PRAGMA table_info(models)")}
        assert "operator_fields" in columns and "roster_revision" in columns
        assert columns["operator_fields"][3:5] == (1, "'[]'")
        assert columns["roster_revision"][3] == 0
        assert instance.get_setting(MARKER) == "1"
        assert all(not owned(instance, row["id"]) for row in instance.list_models())
    finally:
        instance.close()


def test_missing_write_rates_backfill_before_classification(tmp_path):
    instance = AgentRegistry(db_path=str(legacy_db(tmp_path / "legacy.db", write_columns=False)))
    try:
        expected = flat(model_row(document()))
        actual = instance.get_model(SONNET)
        assert actual["cache_write_5m_price"] == expected["cache_write_5m_price"]
        assert actual["cache_write_1h_price"] == expected["cache_write_1h_price"]
        assert owned(instance) == set()
    finally:
        instance.close()


@pytest.mark.parametrize(
    "field,value",
    [
        ("display_name", "Custom display"),
        ("description", "Custom description"),
        ("tier", "custom"),
        ("input_price", 7.0),
        ("output_price", 29.0),
        ("cached_input_price", 0.7),
        ("cache_write_5m_price", 8.0),
        ("cache_write_1h_price", 12.0),
        ("supports_thinking", 0),
        ("active", 0),
        ("sort_order", 999),
        ("context_window", 800_000),
        ("is_1m", 0),
    ],
)
def test_classification_preserves_each_existing_difference(tmp_path, field, value):
    instance = AgentRegistry(
        db_path=str(legacy_db(tmp_path / "legacy.db", overrides={SONNET: {field: value}}))
    )
    try:
        expected_owned = CONTEXT_FIELDS if field in CONTEXT_FIELDS else {field}
        assert owned(instance) == expected_owned
        assert instance.get_model(SONNET)[field] == value
    finally:
        instance.close()


def test_nonbaseline_row_is_fully_owned(tmp_path):
    custom = new_model()
    instance = AgentRegistry(db_path=str(legacy_db(tmp_path / "legacy.db", extra_rows=[custom])))
    try:
        assert owned(instance, "openai/roster-added-model") == FIELDS
        assert instance.get_model("openai/roster-added-model")["roster_revision"] is None
    finally:
        instance.close()


def test_legacy_price_and_context_corrections_precede_classification(tmp_path):
    overrides = {
        "anthropic/claude-opus-4-8": {
            "input_price": 15.0,
            "output_price": 75.0,
            "cached_input_price": 1.5,
        },
        "openai/gpt-5.6-sol": {"context_window": 1_000_000, "is_1m": 1},
    }
    instance = AgentRegistry(db_path=str(legacy_db(tmp_path / "legacy.db", overrides=overrides)))
    try:
        for full_id in overrides:
            assert owned(instance, full_id) == set()
            expected = flat(model_row(document(), full_id))
            assert all(
                instance.get_model(full_id)[field] == expected[field]
                for field in overrides[full_id]
            )
    finally:
        instance.close()


def test_second_constructor_keeps_classification_and_state_unchanged(tmp_path):
    path = tmp_path / "agents.db"
    instance = AgentRegistry(db_path=str(path))
    try:
        assert instance.get_setting(MARKER) == "1"
        original = snapshot(instance)
    finally:
        instance.close()
    reopened = AgentRegistry(db_path=str(path))
    try:
        assert snapshot(reopened) == original
    finally:
        reopened.close()


def test_classification_marker_failure_rolls_back_ownership(tmp_path, monkeypatch):
    path = legacy_db(tmp_path / "legacy.db", overrides={SONNET: {"input_price": 7.0}})
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(f"""CREATE TRIGGER fail_classification BEFORE INSERT ON system_settings
            WHEN NEW.key='{MARKER}' BEGIN SELECT classification_attempt();
            SELECT RAISE(ABORT, 'classification marker blocked'); END""")
    attempts = []
    real_connect = sqlite3.connect

    def connect(database, *args, **kwargs):
        connection = real_connect(database, *args, **kwargs)
        connection.create_function("classification_attempt", 0, lambda: attempts.append(True) or 1)
        return connection

    monkeypatch.setattr(sqlite3, "connect", connect)
    instance = AgentRegistry.__new__(AgentRegistry)
    try:
        try:
            instance.__init__(db_path=str(path))
        except sqlite3.IntegrityError as exc:
            assert "classification marker blocked" in str(exc)
        assert attempts == [True], "classification must attempt its marker write"
        assert instance.get_setting(MARKER) == ""
        assert owned(instance) == set()
        assert instance.get_model(SONNET)["input_price"] == 7.0
    finally:
        # Classification can fail before the constructor creates ScheduleFireTrace.
        trace = getattr(instance, "_fire_trace", None)
        if trace is not None:
            trace.close()
        instance._db.close()


def test_default_operator_insert_owns_all_fields(registry):
    add(registry, new_model())
    assert owned(registry, "openai/roster-added-model") == FIELDS


def test_identical_operator_update_acquires_no_fields(registry):
    add(registry, model_row(document()))
    assert owned(registry) == set()


def test_operator_price_edit_owns_only_changed_fields(registry):
    row = model_row(document())
    row["pricing"]["input"] = 7.0
    add(registry, row)
    assert owned(registry) == {"input_price"}


@pytest.mark.parametrize("changed", ["context_window", "is_1m"])
def test_operator_context_edit_owns_the_whole_pair(registry, changed):
    row = model_row(document())
    row[changed] = 800_000 if changed == "context_window" else False
    add(registry, row)
    assert owned(registry) == CONTEXT_FIELDS


def test_delete_and_operator_reactivation_track_active(registry):
    assert registry.delete_model(SONNET)
    assert owned(registry) == {"active"}
    add(registry, model_row(document()))
    assert registry.get_model(SONNET)["active"] == 1
    assert owned(registry) == {"active"}


async def test_management_add_route_explicitly_marks_operator_edit(registry, monkeypatch):
    from pinky_daemon.routes import providers

    calls = []
    original = registry.add_model

    def add_spy(**kwargs):
        calls.append(kwargs)
        return original(**kwargs)

    monkeypatch.setattr(providers, "_agents", registry)
    monkeypatch.setattr(registry, "add_model", add_spy)
    row = model_row(document())
    row["pricing"]["input"] = 7.0
    request = flat(row)
    request.pop("active")
    request.update(provider=row["provider"], model_id=row["model_id"])
    await providers.add_model(providers.AddModelRequest(**request))
    assert len(calls) == 1 and calls[0].get("operator") is True
    assert owned(registry) == {"input_price"}


async def test_management_delete_route_explicitly_marks_operator_action(registry, monkeypatch):
    from pinky_daemon.routes import providers

    calls = []
    original = registry.delete_model

    def delete_spy(model_id, **kwargs):
        calls.append(kwargs)
        return original(model_id, **kwargs)

    monkeypatch.setattr(providers, "_agents", registry)
    monkeypatch.setattr(registry, "delete_model", delete_spy)
    result = await providers.delete_model(SONNET)
    assert result["deleted"] and len(calls) == 1 and calls[0].get("operator") is True
    assert owned(registry) == {"active"}


async def test_discovery_route_is_insert_only_and_marks_automation(registry, monkeypatch):
    import httpx

    from pinky_daemon.routes import providers

    calls = []
    original = registry.add_model
    before = registry.get_model(SONNET)

    def add_spy(**kwargs):
        calls.append(kwargs)
        return original(**kwargs)

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {
                "data": [
                    {"id": SONNET.split("/", 1)[1], "display_name": "Different display"},
                    {"id": "claude-opus-4", "display_name": "Discovered model"},
                    {"id": "claude-unpriced-model"},
                ]
            }

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

        async def get(self, url, **kwargs):
            assert url == "https://api.anthropic.com/v1/models"
            return Response()

    monkeypatch.setattr(providers, "_agents", registry)
    monkeypatch.setattr(registry, "add_model", add_spy)
    monkeypatch.setattr(httpx, "AsyncClient", Client)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-discovery-key")
    result = await providers.sync_models()
    assert result["new_models"] == ["claude-opus-4"]
    assert result["requires_pricing"] == ["claude-unpriced-model"]
    assert registry.get_model(SONNET) == before
    assert len(calls) == 1 and calls[0].get("operator") is False
    assert owned(registry, "anthropic/claude-opus-4") == set()


def test_automation_insert_remains_roster_managed(registry):
    add(registry, new_model(), operator=False)
    assert owned(registry, "openai/roster-added-model") == set()


def test_automation_update_skips_owned_fields_and_context_pair(registry):
    operator_row = model_row(document())
    operator_row["pricing"]["input"] = 7.0
    operator_row.update(context_window=800_000, is_1m=False)
    add(registry, operator_row)
    automated = model_row(document())
    automated["pricing"]["input"] = 9.0
    automated["description"] = "Automated description"
    add(registry, automated, operator=False)
    assert owned(registry) == {"input_price", *CONTEXT_FIELDS}
    actual = registry.get_model(SONNET)
    assert actual["input_price"] == 7.0
    assert (actual["context_window"], actual["is_1m"]) == (800_000, 0)
    assert actual["description"] == "Automated description"


def test_higher_revision_rollback_survives_next_constructor(tmp_path):
    path = tmp_path / "agents.db"
    instance = AgentRegistry(db_path=str(path))
    value = document(10)
    row = model_row(value)
    row["pricing"].update(
        input=3.0, output=15.0, cached_input=0.3, cache_write_5m=3.75, cache_write_1h=6.0
    )
    row["description"] = (
        "Current Sonnet (2026-06). Best speed+intelligence balance — daily driver. 1M context; adaptive thinking, effort defaults to high. Intro pricing $2/$10 through Aug 2026."
    )
    model_row(value, "openai/gpt-5.6-sol").update(context_window=1_000_000, is_1m=True)
    model_row(value, "anthropic/claude-opus-4-8")["pricing"].update(
        input=15.0, output=75.0, cached_input=1.5
    )
    try:
        apply(instance, value)
        before = snapshot(instance)
    finally:
        instance.close()
    reopened = AgentRegistry(db_path=str(path))
    try:
        assert snapshot(reopened) == before
        assert reopened.get_model(SONNET)["input_price"] == 3.0
        assert reopened.get_model("openai/gpt-5.6-sol")["is_1m"] == 1
        assert reopened.get_model("anthropic/claude-opus-4-8")["input_price"] == 15.0
    finally:
        reopened.close()


@pytest.mark.parametrize("full_id", [SONNET, "anthropic/claude-opus-4-8", "openai/gpt-5.6-sol"])
def test_frozen_corrections_skip_operator_owned_values_on_next_boot(tmp_path, full_id):
    path = tmp_path / "agents.db"
    instance = AgentRegistry(db_path=str(path))
    row = model_row(document(), full_id)
    if full_id == SONNET:
        row["pricing"].update(
            input=3.0, output=15.0, cached_input=0.3, cache_write_5m=3.75, cache_write_1h=6.0
        )
    elif full_id.startswith("anthropic/"):
        row["pricing"].update(input=15.0, output=75.0, cached_input=1.5)
    else:
        row.update(context_window=1_000_000, is_1m=True)
    try:
        add(instance, row)
        assert owned(instance, full_id)
        before = snapshot(instance)
    finally:
        instance.close()
    reopened = AgentRegistry(db_path=str(path))
    try:
        assert snapshot(reopened) == before
    finally:
        reopened.close()


def test_release_either_context_field_clears_both(registry):
    row = model_row(document())
    row.update(context_window=800_000, is_1m=False)
    add(registry, row)
    release(registry, SONNET, ["is_1m"])
    assert owned(registry) == set()
    actual = registry.get_model(SONNET)
    assert (actual["context_window"], actual["is_1m"]) == (1_000_000, 1)
    assert json.loads(registry.get_setting(MARKER)) == 1
