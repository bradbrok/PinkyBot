"""Directory aliases retain each lexical ownership scope without new link policy."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from pinky_daemon import store_catalog as stores


def _alias_tree(tmp_path: Path, alias_owner: str, unmanaged_target: bool = False):
    root = tmp_path / "root"
    target_owner = "bob" if alias_owner == "alice" else "alice"
    target_home = root / "agents" / target_owner
    target = target_home / "custom" if unmanaged_target else target_home / "data" / "shared"
    target.mkdir(parents=True)
    alias = root / "agents" / alias_owner / "data" / "shared"
    alias.parent.mkdir(parents=True)
    alias.symlink_to(target, target_is_directory=True)
    return root, alias, target


def _inspect(root: Path, registered_agents=("alice", "bob")):
    return stores.inspect_filesystem_layout(
        root, {}, silence_allowlist={}, registered_agents=registered_agents,
    )


@pytest.mark.parametrize("alias_owner", ["alice", "bob"])
@pytest.mark.parametrize("unmanaged_target", [False, True])
def test_regression_records_each_alias_scope(tmp_path, alias_owner, unmanaged_target):
    root, alias, target = _alias_tree(tmp_path, alias_owner, unmanaged_target)
    outside = tmp_path / "outside"
    outside.mkdir()
    (target / "escape").symlink_to(outside, target_is_directory=True)

    report = _inspect(root)

    assert {(row["path"], row.get("scope")) for row in report.findings} == {
        (str(alias / "escape"), alias_owner),
        (str(target / "escape"), "bob" if alias_owner == "alice" else "alice"),
    }
    assert {row["kind"] for row in report.findings} == {"directory_outside_root"}


@pytest.mark.parametrize("unmanaged_target", [False, True])
def test_regression_unregistered_alias_keeps_daemon_wide_refusal(tmp_path, unmanaged_target):
    root, alias, target = _alias_tree(tmp_path, "bob", unmanaged_target)
    outside = tmp_path / "outside"
    outside.mkdir()
    (target / "escape").symlink_to(outside, target_is_directory=True)
    catalog = stores.StoreCatalog(expected_root=root, silence_allowlist={})

    with pytest.raises(stores.StoreCatalogError, match="directory symlink escapes"):
        catalog.reconcile_filesystem(scoped_hold=True, registered_agents={"alice"})
    report = _inspect(root, {"alice"})
    assert {(row["path"], row.get("scope")) for row in report.findings} == {
        (str(target / "escape"), "alice"), (str(alias / "escape"), None),
    }


def test_regression_dangling_outward_database_entry_is_a_finding(tmp_path):
    root = tmp_path / "root"
    directory = root / "agents" / "alice" / "data"
    directory.mkdir(parents=True)
    link = directory / "escape.db"
    missing = tmp_path / "outside" / "missing.db"
    link.symlink_to(missing)

    report = _inspect(root)

    assert len(report.findings) == 1
    assert report.findings[0]["kind"] == "database_outside_root"
    assert report.findings[0]["path"] == str(link)
    assert report.findings[0]["resolved_path"] == str(missing)
    assert report.findings[0].get("scope") == "alice"


def test_regression_queued_database_link_missing_target_is_a_finding(tmp_path, monkeypatch):
    root = tmp_path / "root"
    directory = root / "agents" / "alice" / "data"
    trigger, queued = directory / "000-trigger", directory / "queued.db"
    trigger.mkdir(parents=True)
    queued.mkdir()
    missing = tmp_path / "outside" / "missing.db"
    original_scandir = os.scandir
    replaced = []

    def scandir(path):
        if os.fspath(path) == str(trigger) and not replaced:
            queued.rmdir()
            queued.symlink_to(missing)
            replaced.append(str(queued))
        return original_scandir(path)

    monkeypatch.setattr(os, "scandir", scandir)
    report = _inspect(root)

    assert replaced == [str(queued)]
    assert len(report.findings) == 1
    assert report.findings[0]["kind"] == "database_outside_root"
    assert report.findings[0]["path"] == str(queued)
    assert report.findings[0]["resolved_path"] == str(missing)
    assert report.findings[0].get("scope") == "alice"


@pytest.mark.parametrize("alias_owner", ["alice", "bob"])
def test_control_clean_cross_owner_alias_allows_reconciliation(tmp_path, alias_owner):
    root, _, target = _alias_tree(tmp_path, alias_owner)
    registered = target / "tasks.db"
    registered.touch()
    catalog = stores.StoreCatalog(expected_root=root, silence_allowlist={})
    catalog.register("tasks", registered, journal_mode="delete", owner="fixture")

    result = catalog.reconcile_filesystem(
        scoped_hold=True, registered_agents={"alice", "bob"},
    )

    assert result.holds == []
    assert result.warnings == []


@pytest.mark.parametrize("file_alias", [False, True])
def test_control_alias_walk_keeps_existing_unclaimed_warning_count(tmp_path, file_alias):
    root, _, target = _alias_tree(tmp_path, "alice")
    database = target / "orphan.db"
    database.touch()
    if file_alias:
        (target / "copy.db").symlink_to(database)

    report = _inspect(root)

    assert report.findings == []
    assert len(report.warnings) == (2 if file_alias else 1)
    assert all("unclaimed database file" in warning for warning in report.warnings)


@pytest.mark.parametrize("name", ["config.toml", "missing", "copy.db-wal", "copy.db-shm", "copy.db-journal"])
def test_control_dangling_outward_non_database_link_stays_silent(tmp_path, name):
    root = tmp_path / "root"
    directory = root / "agents" / "alice" / "data"
    directory.mkdir(parents=True)
    (directory / name).symlink_to(tmp_path / "outside" / "missing")

    report = _inspect(root)

    assert report.findings == []
    assert report.warnings == []


def test_control_dangling_inside_root_database_link_stays_silent(tmp_path):
    root = tmp_path / "root"
    directory = root / "agents" / "alice" / "data"
    directory.mkdir(parents=True)
    (directory / "missing.db").symlink_to(root / "missing.db")

    report = _inspect(root)

    assert report.findings == []
    assert report.warnings == []


def test_control_dangling_outward_directory_walk_stays_silent(tmp_path):
    root = tmp_path / "root"
    (root / "agents").mkdir(parents=True)
    (root / "agents" / "alice").symlink_to(tmp_path / "missing", target_is_directory=True)

    report = _inspect(root)

    assert report.findings == []
    assert report.warnings == []


def test_control_same_scope_aliases_and_cycles_are_scanned_once(tmp_path, monkeypatch):
    root = tmp_path / "root"
    target = root / "agents" / "alice" / "data" / "shared"
    target.mkdir(parents=True)
    (target.parent / "copy").symlink_to(target, target_is_directory=True)
    (target / "loop").symlink_to(target, target_is_directory=True)
    original_scandir = os.scandir
    scanned = []

    def scandir(path):
        if os.path.samefile(path, target):
            scanned.append(os.fspath(path))
        return original_scandir(path)

    monkeypatch.setattr(os, "scandir", scandir)
    report = _inspect(root)

    assert report.findings == []
    assert report.warnings == []
    assert len(scanned) == 1
