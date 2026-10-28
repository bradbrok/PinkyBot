"""Storage holds require active registry names and complete scope inspection."""

from __future__ import annotations

import hashlib
import inspect
import json
from dataclasses import asdict

import pytest

from pinky_daemon import api
from pinky_daemon import store_catalog as stores
from pinky_daemon.agent_registry import AgentRegistry

_RELOCATED_FULL_SHA256 = "228f33b9fba63edf16473cb712a2da3daeee852eddf7825b19cf7f1dad5d1b93"


def registered_kwargs(call, names):
    if "registered_agents" in inspect.signature(call).parameters:
        return {"registered_agents": frozenset(names)}
    return {}


def inspect_layout(root, names):
    return stores.inspect_filesystem_layout(
        root, {}, silence_allowlist={},
        **registered_kwargs(stores.inspect_filesystem_layout, names),
    )


def reconcile(catalog, names):
    return catalog.reconcile_filesystem(
        scoped_hold=True, **registered_kwargs(catalog.reconcile_filesystem, names),
    )


def outward_entry(tmp_path, layout):
    root = tmp_path / "root"
    (root / "agents").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    if layout == "loose-database":
        target = outside / "shared.db"
        target.touch()
        link = root / "agents" / "shared.db"
    else:
        target = outside
        link = root / "agents" / "orphan" / "data" / "escape"
        link.parent.mkdir(parents=True)
    link.symlink_to(target, target_is_directory=layout != "loose-database")
    return root, link


def claimed_finding(tmp_path, layout):
    root, link = outward_entry(tmp_path, layout)
    report = inspect_layout(root, {"first"})
    assert len(report.findings) == 1
    claim = "shared.db" if layout == "loose-database" else "orphan"
    return root, {**report.findings[0], "path": str(link), "scope": claim}


def startup_report(root, finding):
    return {"ok": False, "root": str(root), "checked": ["import", "layout"],
            "findings": [finding], "warnings_count": 0, "warnings": [],
            "registered_agents": [finding["scope"]]}


@pytest.mark.parametrize("layout", ["loose-database", "orphan-directory"])
def test_unregistered_filesystem_entries_cannot_assign_their_own_scope(tmp_path, layout):
    root, _ = outward_entry(tmp_path, layout)
    report = inspect_layout(root, {"first"})
    assert len(report.findings) == 1
    scope = report.findings[0].get("scope")
    assert scope is None


@pytest.mark.parametrize("layout", ["loose-database", "orphan-directory"])
def test_unregistered_filesystem_entries_remain_fatal_when_holds_are_enabled(tmp_path, layout):
    root, _ = outward_entry(tmp_path, layout)
    catalog = stores.StoreCatalog(expected_root=root, silence_allowlist={})
    try:
        with pytest.raises(stores.StoreCatalogError, match="symlink escapes"):
            reconcile(catalog, {"first"})
    finally:
        catalog.close()


@pytest.mark.parametrize("layout", ["loose-database", "orphan-directory"])
def test_reconcile_revalidates_registration_instead_of_trusting_scope_metadata(
    tmp_path, monkeypatch, layout,
):
    root, finding = claimed_finding(tmp_path, layout)
    catalog = stores.StoreCatalog(expected_root=root, silence_allowlist={})
    monkeypatch.setattr(stores, "inspect_filesystem_layout", lambda *a, **k: stores.LayoutInspection([finding], []))
    try:
        with pytest.raises(stores.StoreCatalogError, match="symlink escapes"):
            reconcile(catalog, {"first"})
    finally:
        catalog.close()


@pytest.mark.parametrize("layout", ["loose-database", "orphan-directory"])
def test_explicit_acceptance_rejects_unregistered_scope_claims(tmp_path, layout):
    root, finding = claimed_finding(tmp_path, layout)
    report = {"ok": False, "reason": "findings", "findings": [finding]}
    accepted = api._accept_scoped_findings(
        report, True, root, **registered_kwargs(api._accept_scoped_findings, {"first"}),
    )
    assert accepted is False
    assert "accepted" not in report


@pytest.mark.parametrize("layout", ["loose-database", "orphan-directory"])
def test_exit_three_uses_parent_names_even_when_target_claims_registration(tmp_path, layout):
    root, finding = claimed_finding(tmp_path, layout)
    report = startup_report(root, finding)
    with pytest.raises(ValueError, match="eligible scoped findings"):
        api._startup_target_report(
            json.dumps(report), 3,
            **registered_kwargs(api._startup_target_report, {"first"}),
        )


@pytest.mark.parametrize("entry", ["directory", "nested-directory", "nested-database"])
def test_active_registered_name_scopes_its_entry_and_children(tmp_path, entry):
    root = tmp_path / "root"
    (root / "agents").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    link = root / "agents" / "first"
    if entry != "directory":
        link = link / "data" / ("escape.db" if entry == "nested-database" else "escape")
        link.parent.mkdir(parents=True)
    target = outside / "content.db" if entry == "nested-database" else outside
    if entry == "nested-database":
        target.touch()
    link.symlink_to(target, target_is_directory=entry != "nested-database")
    report = inspect_layout(root, {"first"})
    assert len(report.findings) == 1
    scope = report.findings[0].get("scope")
    assert scope == "first"
    catalog = stores.StoreCatalog(expected_root=root, silence_allowlist={})
    try:
        result = reconcile(catalog, {"first"})
        assert result.holds == report.findings
        assert result.warnings == []
    finally:
        catalog.close()


def test_absent_registered_name_set_fails_closed(tmp_path):
    root, _ = outward_entry(tmp_path, "orphan-directory")
    catalog = stores.StoreCatalog(expected_root=root, silence_allowlist={})
    try:
        with pytest.raises(stores.StoreCatalogError, match="symlink escapes"):
            catalog.reconcile_filesystem(scoped_hold=True)
    finally:
        catalog.close()


def test_retired_registered_name_does_not_make_its_outward_link_eligible(tmp_path):
    root = tmp_path / "root"
    root.mkdir(mode=0o700)
    catalog = stores.StoreCatalog(expected_root=root, silence_allowlist={})
    registry = AgentRegistry(str(root / "agents.db"), catalog=catalog)
    try:
        registry.register("first", working_dir=str(root / "first-workspace"))
        assert registry.retire("first")
        names = {agent.name for agent in registry.list()}
        assert "first" not in names
        directory = root / "agents" / "first" / "data"
        directory.mkdir(parents=True)
        outside = tmp_path / "outside"
        outside.mkdir()
        (directory / "escape").symlink_to(outside, target_is_directory=True)
        with pytest.raises(stores.StoreCatalogError, match="symlink escapes"):
            reconcile(catalog, names)
    finally:
        registry.close()
        catalog.close()


def test_active_scopes_remain_independent_during_full_and_scoped_inspection(tmp_path):
    root = tmp_path / "root"
    for name in ("first", "second"):
        directory = root / "agents" / name / "data"
        directory.mkdir(parents=True)
        outside = tmp_path / (name + "-outside")
        outside.mkdir()
        (directory / "escape").symlink_to(outside, target_is_directory=True)
    catalog = stores.StoreCatalog(expected_root=root, silence_allowlist={})
    names = {"first", "second"}
    try:
        result = reconcile(catalog, names)
        assert {row["scope"] for row in result.holds} == names
        for name in sorted(names):
            report = catalog.inspect_layout(
                scope=name, **registered_kwargs(catalog.inspect_layout, names),
            )
            assert len(report.findings) == 1 and report.warnings == []
            assert report.findings[0]["scope"] == name
            assert report.findings[0]["path"] == str(root / "agents" / name / "data/escape")
    finally:
        catalog.close()


def test_registered_hold_cannot_hide_an_unscoped_fatal_finding(tmp_path):
    root = tmp_path / "root"
    directory = root / "agents" / "first" / "data"
    directory.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (directory / "escape").symlink_to(outside, target_is_directory=True)
    (root / "unscoped").symlink_to(outside, target_is_directory=True)
    catalog = stores.StoreCatalog(expected_root=root, silence_allowlist={})
    try:
        with pytest.raises(stores.StoreCatalogError, match="symlink escapes"):
            reconcile(catalog, {"first"})
    finally:
        catalog.close()


def relocated_layout(tmp_path):
    root = tmp_path / "root"
    directory = root / "agents" / "first" / "data"
    directory.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (directory / "escape").symlink_to(outside, target_is_directory=True)
    moved = root / "relocated-data"
    directory.rename(moved)
    directory.symlink_to(moved, target_is_directory=True)
    return root


def normalized_report(report, tmp_path):
    payload = json.dumps(asdict(report), sort_keys=True, separators=(",", ":"))
    return payload.replace(str(tmp_path), "<test-root>").encode()


def test_full_inspection_of_relocated_data_matches_the_established_report(tmp_path):
    root = relocated_layout(tmp_path)
    report = inspect_layout(root, {"first"})
    assert report.findings and report.warnings == []
    actual_digest = hashlib.sha256(normalized_report(report, tmp_path)).hexdigest()
    assert actual_digest == _RELOCATED_FULL_SHA256
