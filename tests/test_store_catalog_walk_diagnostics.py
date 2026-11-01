"""Whole-row walk diagnostics preserve scope and reconciliation decisions."""

import copy

import pytest

from pinky_daemon import store_catalog as stores


def _row(root, name="alice"):
    return {
        "kind": "directory_outside_root",
        "path": str(root / "agents" / name / "data/escape"),
        "resolved_path": str(root.parent / "outside"),
        "detail": "same diagnostic text",
    }


def _walk_rows(root, rows):
    inspector = stores._FilesystemLayoutInspector(root, {}, {}, {"alice", "bob"})
    for row in rows:
        inspector._record_finding(copy.deepcopy(row))
    return inspector


def test_equal_text_findings_with_different_scopes_both_survive(tmp_path):
    root = tmp_path / "root"
    alice, bob = _row(root), _row(root, "bob")

    report = _walk_rows(root, [alice, bob, alice, bob])

    assert report.findings == [{**alice, "scope": "alice"}, {**bob, "scope": "bob"}]
    assert len({row["detail"] for row in report.findings}) == 1
    assert {row["scope"] for row in report.findings} == {"alice", "bob"}
    assert len({row["path"] for row in report.findings}) == 2


@pytest.mark.parametrize("field", ["kind", "path", "resolved_path", "detail"])
def test_only_a_whole_equal_finding_is_suppressed(tmp_path, field):
    root = tmp_path / "root"
    original = _row(root)
    changed = {
        **original,
        field: {
            "kind": "database_outside_root",
            "path": str(root / "agents/alice/data/other"),
            "resolved_path": str(tmp_path / "different-outside"),
            "detail": "different diagnostic text",
        }[field],
    }

    report = _walk_rows(root, [original, original, changed, changed])

    assert report.findings == [{**original, "scope": "alice"}, {**changed, "scope": "alice"}]


def test_walk_warning_text_and_repetition_are_unchanged(tmp_path):
    inspector = stores._FilesystemLayoutInspector(tmp_path, {}, {}, {"alice", "bob"})
    error = PermissionError("fixture denied")
    for _ in range(2):
        inspector._inspection_error("directory", str(tmp_path / "unknown"), None, error)

    assert inspector.findings == []
    assert len(inspector.warnings) == 2
    assert inspector.warnings[0] == inspector.warnings[1]
    assert "fixture denied" in inspector.warnings[0]


@pytest.mark.parametrize("include_unscoped", [False, True])
def test_whole_row_suppression_preserves_hold_and_refusal_decisions(
    tmp_path,
    monkeypatch,
    include_unscoped,
):
    root = tmp_path / "root"
    root.mkdir()
    alice, bob = {**_row(root), "scope": "alice"}, {**_row(root, "bob"), "scope": "bob"}
    raw = [alice, alice, bob]
    if include_unscoped:
        unscoped = {**_row(root), "path": str(root / "unscoped")}
        raw.extend([unscoped, unscoped])
    unique = _walk_rows(root, raw).findings
    assert {tuple(sorted(row.items())) for row in raw} == {
        tuple(sorted(row.items())) for row in unique
    }
    catalog = stores.StoreCatalog(expected_root=root, silence_allowlist={})
    outcomes = []
    try:
        for rows in (raw, unique):
            report = stores.LayoutInspection(copy.deepcopy(rows), [])
            monkeypatch.setattr(stores, "inspect_filesystem_layout", lambda *a, _r=report, **kw: _r)
            if include_unscoped:
                with pytest.raises(stores.StoreCatalogError, match="same diagnostic text") as error:
                    catalog.reconcile_filesystem(
                        scoped_hold=True, registered_agents={"alice", "bob"}
                    )
                outcomes.append(("refused", str(error.value)))
            else:
                result = catalog.reconcile_filesystem(
                    scoped_hold=True, registered_agents={"alice", "bob"}
                )
                outcomes.append(
                    (
                        "held",
                        {row["scope"] for row in result.holds},
                        {tuple(sorted(row.items())) for row in result.holds},
                    )
                )
                assert {row["scope"] for row in result.holds} == {"alice", "bob"}
            with pytest.raises(stores.StoreCatalogError, match="same diagnostic text"):
                catalog.reconcile_filesystem(scoped_hold=False, registered_agents={"alice", "bob"})
        assert outcomes[0] == outcomes[1]
    finally:
        catalog.close()
