"""Malformed field ownership must never permit automated model overwrites."""

from __future__ import annotations

import pytest

from pinky_daemon import runtime_model_catalog
from tests._model_roster_local import (
    SONNET,
    add,
    apply,
    fixture_document,
    last_good,
    model_row,
    new_model,
    reference_registry,
    release,
    snapshot,
    status,
)


@pytest.fixture(
    params=["not json", "{}", '"active"', "[1]", '["no_such_field"]'],
    ids=["invalid-json", "object", "string", "non-string-field", "unknown-field"],
)
def corrupt_registry(request, tmp_path, monkeypatch):
    instance = reference_registry(tmp_path / "agents.db")
    try:
        edited = model_row(fixture_document())
        edited["pricing"]["input"] = 7.0
        add(instance, edited)
        instance._db.execute(
            "UPDATE models SET operator_fields=? WHERE id=?", (request.param, SONNET)
        )
        instance._db.commit()
        invalidations = []
        monkeypatch.setattr(runtime_model_catalog, "invalidate", lambda: invalidations.append(True))
        yield instance, invalidations
    finally:
        instance.close()


@pytest.mark.parametrize("dry_run", [False, True], ids=["apply", "dry-run"])
def test_corrupt_ownership_rejects_whole_roster(corrupt_registry, dry_run):
    registry, invalidations = corrupt_registry
    value = fixture_document()
    damaged = model_row(value)
    damaged["pricing"]["input"] = 9.0
    model_row(value, "openai/gpt-5.6-sol")["pricing"]["input"] = 17.0
    # Plan a healthy insert and edit before encountering the corrupted row.
    value["models"].remove(damaged)
    value["models"].insert(0, new_model())
    value["models"].append(damaged)
    before = snapshot(registry)
    good = last_good(registry)
    revision = status(registry)["last_applied_revision"]
    changes = registry._db.total_changes
    refusal = None
    try:
        apply(registry, value, dry_run=dry_run)
    except ValueError as exc:
        refusal = exc
    assert snapshot(registry)["models"] == before["models"]
    assert last_good(registry) == good
    assert status(registry)["last_applied_revision"] == revision
    assert invalidations == []
    assert refusal is not None, "malformed operator_fields must reject the whole roster"
    assert "operator_fields" in str(refusal)
    if dry_run:
        assert snapshot(registry) == before
        assert registry._db.total_changes == changes
    else:
        assert "operator_fields" in status(registry)["last_error"]


def test_corrupt_ownership_refuses_release_without_changes(corrupt_registry):
    registry, invalidations = corrupt_registry
    before = snapshot(registry)
    changes = registry._db.total_changes
    refusal = None
    try:
        release(registry, SONNET, ["input_price"])
    except ValueError as exc:
        refusal = exc
    assert snapshot(registry) == before
    assert registry._db.total_changes == changes
    assert invalidations == []
    assert refusal is not None, "malformed operator_fields must refuse ownership release"
    assert "operator_fields" in str(refusal)


def test_corrupt_ownership_refuses_automated_add_without_changes(corrupt_registry):
    registry, invalidations = corrupt_registry
    automated = model_row(fixture_document())
    automated["pricing"]["input"] = 9.0
    before = snapshot(registry)
    changes = registry._db.total_changes
    refusal = None
    try:
        add(registry, automated, operator=False)
    except ValueError as exc:
        refusal = exc
    assert snapshot(registry) == before
    assert registry._db.total_changes == changes
    assert invalidations == []
    assert refusal is not None, "malformed operator_fields must refuse automated updates"
    assert "operator_fields" in str(refusal)
