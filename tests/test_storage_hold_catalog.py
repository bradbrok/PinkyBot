"""Scope attribution and opt-in filesystem reconciliation."""

from __future__ import annotations

import inspect
import os
from pathlib import Path

import pytest

from pinky_daemon import store_catalog as stores


def escape(tmp_path: Path, kind: str = "directory", name: str = "first"):
    root = tmp_path / "root"
    parent = root / "agents" / name / "data"
    parent.mkdir(parents=True)
    target = tmp_path / "outside" / "agents" / "second"
    target.mkdir(parents=True)
    if kind == "database":
        target = target / "content"
        target.write_bytes(b"fixture data")
    link = parent / ("escape.db" if kind == "database" else "escape")
    link.symlink_to(target, target_is_directory=kind == "directory")
    return root, link, target


def reconcile(catalog, *, enabled=True):
    try:
        return catalog.reconcile_filesystem(scoped_hold=enabled)
    except TypeError as exc:
        pytest.fail(f"Scoped reconciliation must be an explicit opt-in: {exc}")


@pytest.mark.parametrize("kind", ["directory", "database"])
def test_outside_findings_carry_the_discovered_subject(tmp_path, kind):
    root, link, target = escape(tmp_path, kind)
    report = stores.inspect_filesystem_layout(root, {}, silence_allowlist={})
    assert len(report.findings) == 1
    finding = report.findings[0]
    assert finding["kind"] == kind + "_outside_root"
    assert finding["path"] == str(link)
    assert finding["resolved_path"] == str(target)
    assert finding.get("scope") == "first", "The target's subject is not the discoverer"


@pytest.mark.parametrize("kind", ["directory", "database"])
def test_scoped_reconciliation_returns_holds_and_preserves_legacy_refusal(tmp_path, kind):
    root, _, _ = escape(tmp_path, kind)
    catalog = stores.StoreCatalog(expected_root=root, silence_allowlist={})
    report = catalog.inspect_layout()
    with pytest.raises(stores.StoreCatalogError) as raised:
        catalog.reconcile_filesystem()
    assert str(raised.value) == report.findings[0]["detail"]
    result = reconcile(catalog)
    assert result.warnings == []
    assert result.holds == report.findings


def test_scope_includes_the_entry_itself(tmp_path):
    root = tmp_path / "root"
    (root / "agents").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    link = root / "agents" / "first"
    link.symlink_to(outside, target_is_directory=True)
    report = stores.inspect_filesystem_layout(root, {}, silence_allowlist={})
    assert len(report.findings) == 1
    assert report.findings[0]["path"] == str(link)
    assert report.findings[0].get("scope") == "first"


@pytest.mark.parametrize("relative", ["escape", "agents", "agents-copy/first/escape"])
def test_paths_outside_subject_entries_never_acquire_scope(tmp_path, relative):
    root = tmp_path / "root"
    link = root / relative
    link.parent.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    link.symlink_to(outside, target_is_directory=True)
    catalog = stores.StoreCatalog(expected_root=root, silence_allowlist={})
    report = catalog.inspect_layout()
    assert len(report.findings) == 1 and not report.findings[0].get("scope")
    with pytest.raises(stores.StoreCatalogError, match="directory symlink escapes"):
        reconcile(catalog)


@pytest.mark.parametrize("kind", ["alias", "footprint"])
def test_registered_store_findings_remain_fatal_inside_a_subject(tmp_path, monkeypatch, kind):
    root = tmp_path / "root"
    directory = root / "agents" / "first" / "data"
    directory.mkdir(parents=True)
    registered = (root if kind == "alias" else directory) / "tasks.db"
    registered.touch()
    catalog = stores.StoreCatalog(expected_root=root, silence_allowlist={})
    catalog.register("tasks", registered, journal_mode="delete", owner="fixture")
    if kind == "alias":
        os.link(registered, directory / "alias.db")
    else:
        original = os.scandir

        def inaccessible(path):
            if os.fspath(path) == str(directory):
                raise PermissionError("fixture directory cannot be listed")
            return original(path)

        monkeypatch.setattr(os, "scandir", inaccessible)
    report = catalog.inspect_layout()
    expected = "alias_of_registered_store" if kind == "alias" else "uninspectable_in_store_footprint"
    assert [row["kind"] for row in report.findings] == [expected]
    assert "scope" not in report.findings[0]
    with pytest.raises(stores.StoreCatalogError) as raised:
        reconcile(catalog)
    assert report.findings[0]["detail"] in str(raised.value)


def test_unscoped_findings_keep_the_original_failure_order(tmp_path, monkeypatch):
    root, _, _ = escape(tmp_path)
    catalog = stores.StoreCatalog(expected_root=root, silence_allowlist={})
    scoped = catalog.inspect_layout().findings[0]
    scoped = {**scoped, "scope": "first"}
    alias = dict(kind="alias_of_registered_store", path=str(root / "alias.db"),
                 resolved_path=str(root / "tasks.db"), detail="fixture alias failure")
    fatal = dict(kind="uninspectable_in_store_footprint", path=str(root / "owned"),
                 resolved_path=None, detail="fixture first fatal failure")
    report = stores.LayoutInspection([scoped, alias, fatal], [])
    monkeypatch.setattr(stores, "inspect_filesystem_layout", lambda *args, **kwargs: report)
    with pytest.raises(stores.StoreCatalogError) as raised:
        reconcile(catalog)
    assert str(raised.value) == fatal["detail"]


@pytest.mark.parametrize("method", ["validate", "reconcile_filesystem"])
def test_storage_holds_are_disabled_by_default_for_other_catalogs(tmp_path, method):
    root, _, _ = escape(tmp_path)
    catalog = stores.StoreCatalog(expected_root=root, silence_allowlist={})
    parameters = inspect.signature(getattr(catalog, method)).parameters
    assert "scoped_hold" in parameters
    assert parameters["scoped_hold"].default is False
    with pytest.raises(stores.StoreCatalogError, match="directory symlink escapes"):
        getattr(catalog, method)()


def test_recheck_walks_one_scope_with_the_main_containment_boundary(tmp_path, monkeypatch):
    root = tmp_path / "root"
    first = root / "agents" / "first"
    second = root / "agents" / "second"
    (first / "data").mkdir(parents=True)
    (second / "data").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (second / "data" / "escape").symlink_to(outside, target_is_directory=True)
    (first / "data" / "peer").symlink_to(second, target_is_directory=True)
    catalog = stores.StoreCatalog(expected_root=root, silence_allowlist={})
    assert catalog.inspect_layout().findings
    scanned = []
    original = os.scandir

    def scandir(path):
        scanned.append(os.fspath(path))
        assert not os.fspath(path).startswith(str(second)), "Recheck walked another scope"
        return original(path)

    monkeypatch.setattr(os, "scandir", scandir)
    try:
        report = catalog.inspect_layout(scope="first")
    except TypeError as exc:
        pytest.fail(f"A scope-only recheck must be available: {exc}")
    assert report.findings == []
    assert report.warnings == []
    assert scanned and str(root) not in scanned


def test_tenant_shaped_catalog_still_raises_for_scoped_findings(tmp_path):
    root, _, _ = escape(tmp_path)
    tenant = stores.StoreCatalog(expected_root=root, silence_allowlist={})
    with pytest.raises(stores.StoreCatalogError, match="directory symlink escapes"):
        tenant.validate()
    result = reconcile(tenant)
    assert len(result.holds) == 1
    assert result.holds[0].get("scope") == "first"


def test_unknown_kind_is_fatal_even_with_scope_metadata(tmp_path, monkeypatch):
    root, _, _ = escape(tmp_path)
    catalog = stores.StoreCatalog(expected_root=root, silence_allowlist={})
    finding = dict(kind="future_kind", path=str(root / "agents/first/data"), scope="first",
                   resolved_path=None, detail="fixture future fatal")
    monkeypatch.setattr(stores, "inspect_filesystem_layout", lambda *args, **kwargs: stores.LayoutInspection([finding], []))
    with pytest.raises(stores.StoreCatalogError, match="fixture future fatal"):
        reconcile(catalog)
