"""Revision, transaction, retirement, and release contracts for local roster apply."""

from __future__ import annotations

import hashlib
import json
import sqlite3

import pytest

from tests._model_roster_local import (
    CONTEXT_FIELDS,
    DOCUMENT_KEY,
    SONNET,
    SOURCE,
    add,
    apply,
    assert_skip,
    bundled_revision,
    encode,
    fixture_document,
    last_good,
    model_row,
    new_model,
    owned,
    reference_registry,
    release,
    require_method,
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


def test_higher_revision_changes_only_differing_rows_and_persists_exact_bytes(registry):
    before = registry.get_model(SONNET)
    untouched = registry.get_model("anthropic/claude-opus-4-8")
    value = fixture_document(bundled_revision() + 3)
    model_row(value)["pricing"]["input"] = 7.0
    blob = b" \n" + encode(value) + b"\n\t"
    report = apply(registry, blob)
    assert report["revision_gate"] == "accepted"
    actual = registry.get_model(SONNET)
    assert actual["input_price"] == 7.0
    assert actual["created_at"] == before["created_at"]
    assert actual["updated_at"] != before["updated_at"]
    assert actual["roster_revision"] == value["revision"]
    assert registry.get_model("anthropic/claude-opus-4-8") == untouched
    assert registry.get_setting(DOCUMENT_KEY).encode("utf-8") == blob
    recorded = status(registry)
    assert recorded["last_applied_revision"] == value["revision"]
    assert recorded["sha256"] == hashlib.sha256(blob).hexdigest()
    assert recorded["source"] == SOURCE
    assert recorded["applied_at"] and recorded["last_attempt_at"]
    assert not recorded["last_error"]
    assert recorded["counts"] == report["counts"]
    assert report["counts"]["updated"] == 1


@pytest.mark.parametrize("offset", [-1, 0], ids=["lower", "equal"])
def test_equal_or_lower_document_preserves_last_good(registry, offset):
    value = fixture_document(bundled_revision() + 1)
    model_row(value)["pricing"]["input"] = 7.0
    apply(registry, value)
    before_rows = snapshot(registry)["models"]
    before_good = last_good(registry)
    stale = fixture_document(value["revision"] + offset)
    model_row(stale)["pricing"]["input"] = 9.0
    apply(registry, stale)
    assert snapshot(registry)["models"] == before_rows
    assert last_good(registry) == before_good


def test_accepted_unchanged_revision_advances_global_not_row_metadata(registry):
    original = registry.get_model(SONNET)
    apply(registry, fixture_document(bundled_revision() + 7))
    assert registry.get_model(SONNET) == original
    assert status(registry)["last_applied_revision"] == bundled_revision() + 7


def test_accepted_all_owned_row_advances_global_without_changing_row_metadata(registry):
    row = new_model()
    add(registry, row)
    original = registry.get_model("openai/roster-added-model")
    value = fixture_document(bundled_revision() + 7)
    row["pricing"]["input"] = 7.0
    value["models"].append(row)
    report = apply(registry, value)
    assert registry.get_model(original["id"]) == original
    assert status(registry)["last_applied_revision"] == bundled_revision() + 7
    assert report["counts"]["skipped_operator"] == 1


def test_insert_is_managed_and_absent_rows_are_retained(registry):
    value = fixture_document(bundled_revision() + 1)
    value["models"].append(new_model())
    apply(registry, value)
    inserted = registry.get_model("openai/roster-added-model")
    assert inserted and inserted["active"] == 1 and inserted["roster_revision"] == value["revision"]
    assert owned(registry, inserted["id"]) == set()
    apply(registry, fixture_document(bundled_revision() + 2))
    assert registry.get_model(inserted["id"]) == inserted


def test_existing_active_row_is_never_retired_by_the_roster(registry):
    value = fixture_document()
    model_row(value).update(active=False, description="Updated description")
    report = apply(registry, value)
    actual = registry.get_model(SONNET)
    assert actual["active"] == 1
    assert actual["description"] == "Updated description"
    assert owned(registry) == set()
    assert_skip(report, SONNET, "deactivation_not_automatic")


def test_new_inactive_row_is_not_inserted(registry):
    value = fixture_document()
    value["models"].append(new_model(active=False))
    report = apply(registry, value)
    assert registry.get_model("openai/roster-added-model") is None
    assert_skip(report, "openai/roster-added-model", "inactive_row_not_inserted")
    assert status(registry)["last_applied_revision"] == value["revision"]


def test_operator_deactivation_is_not_undone(registry):
    assert registry.delete_model(SONNET)
    apply(registry, fixture_document())
    assert registry.get_model(SONNET)["active"] == 0
    assert owned(registry) == {"active"}


def test_unowned_inactive_row_can_be_activated(registry):
    assert registry.delete_model(SONNET)
    inactive = fixture_document(bundled_revision() + 1)
    model_row(inactive)["active"] = False
    apply(registry, inactive)
    release(registry, SONNET, ["active"])
    assert registry.get_model(SONNET)["active"] == 0 and owned(registry) == set()
    apply(registry, fixture_document(bundled_revision() + 2))
    assert registry.get_model(SONNET)["active"] == 1


def test_operator_price_survives_while_other_fields_apply(registry):
    row = model_row(fixture_document())
    row["pricing"]["input"] = 7.0
    add(registry, row)
    value = fixture_document()
    model_row(value)["pricing"].update(input=9.0, output=31.0)
    apply(registry, value)
    actual = registry.get_model(SONNET)
    assert actual["input_price"] == 7.0
    assert actual["output_price"] == 31.0
    assert owned(registry) == {"input_price"}


def test_owned_context_pair_is_never_partially_applied(registry):
    row = model_row(fixture_document())
    row["context_window"] = 800_000
    add(registry, row)
    assert owned(registry) == CONTEXT_FIELDS
    value = fixture_document()
    model_row(value).update(context_window=2_000_000, is_1m=True, description="Other field")
    apply(registry, value)
    actual = registry.get_model(SONNET)
    assert (actual["context_window"], actual["is_1m"]) == (800_000, 1)
    assert actual["description"] == "Other field"


def test_unowned_context_pair_updates_together(registry):
    value = fixture_document()
    model_row(value).update(context_window=800_000, is_1m=False)
    apply(registry, value)
    actual = registry.get_model(SONNET)
    assert (actual["context_window"], actual["is_1m"]) == (800_000, 0)


@pytest.mark.parametrize("offset,gate", [(-1, "lower"), (0, "equal"), (1, "accepted")])
def test_dry_run_skips_revision_gate_and_writes_nothing(registry, offset, gate):
    apply(registry, fixture_document(bundled_revision() + 1))
    before = snapshot(registry)
    value = fixture_document(bundled_revision() + 1 + offset)
    model_row(value)["pricing"]["input"] = 7.0
    value["models"].append(new_model())
    report = apply(registry, value, dry_run=True)
    assert snapshot(registry) == before
    assert report["revision_gate"] == gate
    assert report["counts"]["updated"] == 1
    assert report["counts"]["inserted"] == 1


def test_dry_run_reports_operator_and_retirement_skips(registry):
    row = model_row(fixture_document())
    row["pricing"]["input"] = 7.0
    add(registry, row)
    value = fixture_document()
    model_row(value)["pricing"]["input"] = 9.0
    model_row(value)["active"] = False
    before = snapshot(registry)
    report = apply(registry, value, dry_run=True)
    assert snapshot(registry) == before
    assert report["counts"]["skipped_operator"] == 1
    assert_skip(report, SONNET, "deactivation_not_automatic")
    assert "input_price" in json.dumps(report)


INVALID_DOCUMENTS = [
    b"not-json",
    b"\xff",
    b"[]",
    b"[" * 100_000,
    b'{"schema":"pinky-model-roster/1","revision":2,"revision":3}',
    b" " * (256 * 1024 + 1),
]


@pytest.mark.parametrize(
    "blob", INVALID_DOCUMENTS, ids=["json", "utf8", "shape", "nesting", "duplicate", "size"]
)
@pytest.mark.parametrize("dry_run", [False, True])
def test_rejection_keeps_last_good_and_dry_run_is_completely_read_only(registry, blob, dry_run):
    method = require_method(registry, "apply_model_roster")
    before = snapshot(registry)
    good = last_good(registry)
    with pytest.raises(ValueError):
        method(blob, source=SOURCE, dry_run=dry_run)
    assert snapshot(registry)["models"] == before["models"]
    assert last_good(registry) == good
    if dry_run:
        assert snapshot(registry) == before
    else:
        assert registry.get_setting("model_roster.last_error")


def test_empty_document_is_rejected_through_apply(registry):
    value = fixture_document()
    value["models"] = []
    method = require_method(registry, "apply_model_roster")
    with pytest.raises(ValueError, match="models"):
        method(encode(value), source=SOURCE)


@pytest.mark.parametrize("dry_run", [False, True])
def test_cross_provider_existing_collision_rejects_whole_document(registry, dry_run):
    conflicting = new_model()
    conflicting.update(provider="anthropic", model_id="roster-collision")
    add(registry, conflicting)
    before = snapshot(registry)
    good = last_good(registry)
    value = fixture_document()
    model_row(value)["pricing"]["input"] = 7.0
    incoming = new_model("roster-collision")
    value["models"].append(incoming)
    method = require_method(registry, "apply_model_roster")
    with pytest.raises(ValueError, match="roster-collision"):
        method(encode(value), source=SOURCE, dry_run=dry_run)
    assert snapshot(registry)["models"] == before["models"]
    assert last_good(registry) == good
    if dry_run:
        assert snapshot(registry) == before
    else:
        assert "roster-collision" in registry.get_setting("model_roster.last_error")


def test_state_write_failure_rolls_back_all_catalog_changes(registry, monkeypatch):
    from pinky_daemon import runtime_model_catalog

    method = require_method(registry, "apply_model_roster")
    invalidations = []
    monkeypatch.setattr(runtime_model_catalog, "invalidate", lambda: invalidations.append(True))
    before = snapshot(registry)["models"]
    good = last_good(registry)
    for operation in ("INSERT", "UPDATE"):
        registry._db.execute(f"""CREATE TRIGGER fail_roster_{operation.lower()}
            BEFORE {operation} ON system_settings
            WHEN NEW.key='model_roster.last_applied_revision' AND NEW.value='{bundled_revision() + 1}'
            BEGIN SELECT RAISE(ABORT, 'roster state blocked'); END""")
    registry._db.commit()
    value = fixture_document()
    model_row(value)["pricing"]["input"] = 7.0
    value["models"].append(new_model())
    with pytest.raises(sqlite3.IntegrityError, match="roster state blocked"):
        method(encode(value), source=SOURCE)
    assert snapshot(registry)["models"] == before
    assert last_good(registry) == good
    assert invalidations == []


def test_release_immediately_uses_last_applied_document_without_revision_bump(registry):
    edited = model_row(fixture_document())
    edited["pricing"]["input"] = 7.0
    add(registry, edited)
    value = fixture_document(bundled_revision() + 7)
    model_row(value)["pricing"]["input"] = 9.0
    apply(registry, value)
    good = last_good(registry)
    release(registry, SONNET, ["input_price"])
    assert registry.get_model(SONNET)["input_price"] == 9.0
    assert owned(registry) == set()
    assert last_good(registry) == good


@pytest.mark.parametrize("field", ["context_window", "is_1m"])
def test_release_of_either_context_field_reapplies_whole_pair(registry, field):
    row = model_row(fixture_document())
    row.update(context_window=800_000, is_1m=False)
    add(registry, row)
    value = fixture_document(bundled_revision() + 2)
    model_row(value).update(context_window=2_000_000, is_1m=True)
    apply(registry, value)
    release(registry, SONNET, [field])
    assert owned(registry) == set()
    actual = registry.get_model(SONNET)
    assert (actual["context_window"], actual["is_1m"]) == (2_000_000, 1)


def test_release_never_deactivates(registry):
    add(registry, model_row(fixture_document()))
    registry.delete_model(SONNET)
    add(registry, model_row(fixture_document()))
    assert owned(registry) == {"active"}
    value = fixture_document()
    model_row(value)["active"] = False
    apply(registry, value)
    report = release(registry, SONNET, ["active"])
    assert registry.get_model(SONNET)["active"] == 1
    assert owned(registry) == set()
    assert_skip(report, SONNET, "deactivation_not_automatic")


def test_release_without_any_applied_document_only_clears_ownership(registry):
    row = model_row(fixture_document())
    row["pricing"]["input"] = 7.0
    add(registry, row)
    for (key,) in registry._db.execute(
        "SELECT key FROM system_settings WHERE key LIKE 'model_roster.%'"
    ).fetchall():
        registry.delete_setting(key)
    release(registry, SONNET, ["input_price"])
    assert owned(registry) == set()
    assert registry.get_model(SONNET)["input_price"] == 7.0


def test_release_of_row_absent_from_saved_document_keeps_values(registry):
    row = new_model()
    add(registry, row)
    apply(registry, fixture_document())
    before = registry.get_model("openai/roster-added-model")
    report = release(registry, before["id"], "all")
    after = registry.get_model(before["id"])
    assert owned(registry, before["id"]) == set()
    for key in ("input_price", "output_price", "active", "created_at"):
        assert after[key] == before[key]
    assert after["updated_at"] != before["updated_at"]
    assert before["id"] in json.dumps(report)


def test_corrupt_saved_document_cannot_partially_clear_ownership(registry):
    row = model_row(fixture_document())
    row["pricing"]["input"] = 7.0
    add(registry, row)
    registry.set_setting(DOCUMENT_KEY, "not-json")
    before = snapshot(registry)
    method = require_method(registry, "release_model_roster_fields")
    with pytest.raises(ValueError):
        method(SONNET, ["input_price"])
    assert snapshot(registry)["models"] == before["models"]
    assert owned(registry) == {"input_price"}


def test_valid_saved_document_with_wrong_digest_cannot_clear_ownership(registry):
    row = model_row(fixture_document())
    row["pricing"]["input"] = 7.0
    add(registry, row)
    registry.set_setting("model_roster.sha256", "0" * 64)
    before = snapshot(registry)["models"]
    method = require_method(registry, "release_model_roster_fields")
    with pytest.raises(ValueError):
        method(SONNET, ["input_price"])
    assert snapshot(registry)["models"] == before
    assert owned(registry) == {"input_price"}


@pytest.mark.parametrize("fields", [["provider"], ["created_at"], ["misspelled_field"]])
def test_invalid_release_fields_change_nothing(registry, fields):
    method = require_method(registry, "release_model_roster_fields")
    before = snapshot(registry)
    with pytest.raises(ValueError):
        method(SONNET, fields)
    assert snapshot(registry) == before
