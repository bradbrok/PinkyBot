"""Saved roster state and exact row identities protect model writes."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing

import pytest

from pinky_daemon import runtime_model_catalog
from tests._model_roster_local import (
    DOCUMENT_KEY,
    FIELDS,
    SONNET,
    add,
    bundled_revision,
    fixture_document,
    flat,
    legacy_db,
    model_row,
    new_model,
    owned,
    reference_registry,
    release,
    snapshot,
    status,
)


@pytest.fixture
def registry(tmp_path):
    instance = reference_registry(tmp_path / "agents.db")
    try:
        yield instance
    finally:
        instance.close()


@pytest.mark.parametrize("saved", ["", None, " \n\t"], ids=["empty", "missing", "whitespace"])
@pytest.mark.parametrize("evidence", ["revision", "digest", "both"])
def test_applied_document_evidence_refuses_missing_or_empty_release(
    registry, monkeypatch, saved, evidence
):
    edited = model_row(fixture_document())
    edited["pricing"]["input"] = 7.0
    add(registry, edited)
    registry.set_setting("model_roster.last_applied_revision", str(bundled_revision()) if evidence != "digest" else "0")
    if evidence == "revision":
        registry.delete_setting("model_roster.sha256")
    if saved is None:
        registry.delete_setting(DOCUMENT_KEY)
    else:
        registry.set_setting(DOCUMENT_KEY, saved)
    before = snapshot(registry)
    changes = registry._db.total_changes
    invalidations = []
    monkeypatch.setattr(runtime_model_catalog, "invalidate", lambda: invalidations.append(True))
    refusal = None
    try:
        release(registry, SONNET, ["input_price"])
    except ValueError as exc:
        refusal = exc
    assert snapshot(registry) == before
    assert registry._db.total_changes == changes
    assert owned(registry) == {"input_price"}
    assert invalidations == []
    assert refusal is not None, "an applied roster with no valid saved document must refuse release"


@pytest.mark.parametrize("saved", ["", None], ids=["empty", "missing"])
def test_never_applied_release_clears_ownership_without_repricing(registry, saved):
    edited = model_row(fixture_document())
    edited["pricing"]["input"] = 7.0
    add(registry, edited)
    for key, _ in snapshot(registry)["settings"]:
        if key.startswith("model_roster."):
            registry.delete_setting(key)
    registry.set_setting("model_roster.last_applied_revision", "0")
    if saved is not None:
        registry.set_setting(DOCUMENT_KEY, saved)
    assert status(registry)["last_applied_revision"] == 0
    assert not status(registry)["sha256"]
    before = registry.get_model(SONNET)
    settings = snapshot(registry)["settings"]
    report = release(registry, SONNET, ["input_price"])
    after = registry.get_model(SONNET)
    assert owned(registry) == set()
    assert after["input_price"] == 7.0
    assert after["created_at"] == before["created_at"]
    assert after["updated_at"] != before["updated_at"]
    assert after["roster_revision"] == before["roster_revision"]
    assert snapshot(registry)["settings"] == settings
    assert report["rows"][0]["reason"] == "no_applied_document"
    assert report["rows"][0]["fields_released"] == ["input_price"]


async def test_management_add_inserts_exact_id_despite_other_rows_slash_id(registry, monkeypatch):
    from pinky_daemon.routes import providers

    monkeypatch.setattr(providers, "_agents", registry)
    await providers.add_model(
        providers.AddModelRequest(
            provider="openai", model_id="anthropic/upgrade-child", input_price=7.0,
            output_price=0.0, cached_input_price=0.0, cache_write_5m_price=0.0,
            cache_write_1h_price=0.0,
        )
    )
    other_id = "openai/anthropic/upgrade-child"
    before = registry._db.execute("SELECT * FROM models WHERE id=?", (other_id,)).fetchone()
    assert before is not None
    result = await providers.add_model(
        providers.AddModelRequest(
            provider="anthropic", model_id="upgrade-child", input_price=9.0,
            output_price=0.0, cached_input_price=0.0, cache_write_5m_price=0.0,
            cache_write_1h_price=0.0,
        )
    )
    target_id = "anthropic/upgrade-child"
    exact = registry._db.execute(
        "SELECT id, provider, model_id, input_price FROM models WHERE id=?", (target_id,)
    ).fetchone()
    assert exact == (target_id, "anthropic", "upgrade-child", 9.0)
    assert registry._db.execute("SELECT * FROM models WHERE id=?", (other_id,)).fetchone() == before
    assert result["id"] == target_id


def test_frozen_correction_only_targets_exact_baseline_row(registry):
    custom = new_model(SONNET)
    custom["pricing"]["input"] = 3.0
    add(registry, custom)
    custom_id = f"openai/{SONNET}"
    registry._db.execute(
        "UPDATE models SET operator_fields='[]', roster_revision=NULL WHERE id=?", (custom_id,)
    )
    cursor = registry._db.execute("SELECT * FROM models WHERE id=?", (SONNET,))
    columns = [entry[0] for entry in cursor.description]
    baseline = dict(zip(columns, cursor.fetchone()))
    registry._db.execute("DELETE FROM models WHERE id=?", (SONNET,))
    registry._db.commit()
    before = snapshot(registry)
    assert registry._correct_model_fields(
        SONNET, {"input_price": 3.0}, {"input_price": baseline["input_price"]}, 123.0
    ) == 0, "a bare-id alias must not stand in for an absent correction target"
    assert snapshot(registry) == before
    stale = {**baseline, "input_price": 3.0, "operator_fields": "[]", "roster_revision": None}
    registry._db.execute(
        f"INSERT INTO models ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
        tuple(stale[column] for column in columns),
    )
    registry._db.commit()
    before = snapshot(registry)["models"]
    assert registry._correct_model_fields(
        SONNET, {"input_price": 3.0}, {"input_price": baseline["input_price"]}, 124.0
    ) == 1
    registry._db.commit()
    corrected = dict(zip(columns, registry._db.execute(
        "SELECT * FROM models WHERE id=?", (SONNET,)
    ).fetchone()))
    assert corrected["input_price"] == baseline["input_price"]
    assert corrected["updated_at"] == 124.0
    id_index = columns.index("id")
    assert [row for row in snapshot(registry)["models"] if row[id_index] != SONNET] == [
        row for row in before if row[id_index] != SONNET
    ]


def test_constructor_corrects_only_baseline_after_lower_rowid_custom_alias(tmp_path):
    path = legacy_db(tmp_path / "agents.db")
    baseline = model_row(fixture_document())
    stale_prices = {
        "input_price": 3.0,
        "output_price": 15.0,
        "cached_input_price": 0.3,
        "cache_write_5m_price": 3.75,
        "cache_write_1h_price": 6.0,
    }
    custom = new_model(SONNET)
    custom_id = f"openai/{SONNET}"
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("ALTER TABLE models ADD COLUMN operator_fields TEXT NOT NULL DEFAULT '[]'")
        connection.execute("ALTER TABLE models ADD COLUMN roster_revision INTEGER")
        connection.execute("DELETE FROM models")
        # Insert the custom alias first, as on a store predating the baseline model.
        for row, fields in ((custom, FIELDS), (baseline, set())):
            values = {
                **flat(row), **stale_prices,
                "id": f"{row['provider']}/{row['model_id']}",
                "provider": row["provider"], "model_id": row["model_id"],
                "created_at": 11.0, "updated_at": 12.0,
                "operator_fields": json.dumps(sorted(fields)), "roster_revision": None,
            }
            columns = list(values)
            connection.execute(
                f"INSERT INTO models ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
                tuple(values[column] for column in columns),
            )
        rowids = dict(connection.execute("SELECT id, rowid FROM models"))
        assert rowids[custom_id] < rowids[SONNET]
        before = connection.execute("SELECT * FROM models WHERE id=?", (custom_id,)).fetchone()
    instance = reference_registry(path)
    try:
        assert instance._db.execute(
            "SELECT * FROM models WHERE id=?", (custom_id,)
        ).fetchone() == before
        cursor = instance._db.execute("SELECT * FROM models WHERE id=?", (SONNET,))
        actual = dict(zip((entry[0] for entry in cursor.description), cursor.fetchone()))
        expected = flat(baseline)
        assert {field: actual[field] for field in stale_prices} == {
            field: expected[field] for field in stale_prices
        }
    finally:
        instance.close()
