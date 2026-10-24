"""Metadata-equivalence and traversal-cost checks for large data roots."""

from __future__ import annotations

import logging
import os
import stat

import pytest

from pinky_daemon import store_catalog as catalog
from pinky_daemon.store_catalog import LayoutInspection, StoreCatalog


class _ReferenceInspector(catalog._FilesystemLayoutInspector):
    def inspect(self) -> LayoutInspection:
        pending_directories = [self.root]
        # Cycles are guarded by inode; warning policy follows each file realpath.
        visited_directories: set[tuple[int, int]] = set()
        while pending_directories:
            directory = pending_directories.pop()
            try:
                resolved_directory = os.path.realpath(directory)
            except OSError as exc:
                self._inspection_error("directory", directory, None, exc)
                continue
            try:
                directory_stat = os.stat(resolved_directory)
            except OSError as exc:
                self._inspection_error("directory", directory, resolved_directory, exc)
                continue
            if not stat.S_ISDIR(directory_stat.st_mode):
                self._record_database(directory, resolved_directory, directory_stat)
                continue
            if not self._under_root(resolved_directory):
                self._outside_root("directory", directory, resolved_directory)
                continue
            directory_identity = (directory_stat.st_dev, directory_stat.st_ino)
            if directory_identity in visited_directories:
                continue
            visited_directories.add(directory_identity)
            try:
                with os.scandir(resolved_directory) as iterator:
                    entries = sorted(iterator, key=lambda entry: entry.name)
            except OSError as exc:
                self._inspection_error("directory", directory, resolved_directory, exc)
                continue

            child_directories: list[str] = []
            for entry in entries:
                discovered_path = entry.path
                try:
                    resolved_path = os.path.realpath(discovered_path)
                except OSError as exc:
                    self._inspection_error("file or directory", discovered_path, None, exc)
                    continue
                try:
                    entry_stat = os.stat(resolved_path)
                except OSError as exc:
                    self._inspection_error(
                        "file or directory", discovered_path, resolved_path, exc
                    )
                    continue
                if stat.S_ISDIR(entry_stat.st_mode):
                    if not self._under_root(resolved_path):
                        self._outside_root("directory", discovered_path, resolved_path)
                        continue
                    child_directories.append(discovered_path)
                    continue
                self._record_database(discovered_path, resolved_path, entry_stat)
            pending_directories.extend(reversed(child_directories))

        matched_allowlist_patterns: set[str] = set()
        for database in self.unclaimed_databases:
            matching_patterns = {
                pattern for pattern in self.allowlist
                if self._allowlist_matches(pattern, database.relative_path)
            }
            matched_allowlist_patterns.update(matching_patterns)
            if matching_patterns:
                continue
            self.warnings.append(
                "unclaimed database file: "
                f"path={database.relative_path!r} resolved_path={database.resolved_path!r} "
                f"dev_ino={database.dev_ino!r}"
            )
        for pattern, reason in self.allowlist.items():
            if pattern not in matched_allowlist_patterns:
                self.warnings.append(
                    f"dead silence-allowlist entry {pattern!r} matched no database files "
                    f"(reason: {reason})"
                )
        return LayoutInspection(self.findings, self.warnings)



def _write(root, relative, content=b"data"):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def _identities(*paths):
    return catalog.read_store_identities(str(path.resolve()) for path in paths)


def _reference(root, identities, allowlist=None):
    return _ReferenceInspector(root, identities, {} if allowlist is None else allowlist).inspect()


def _inspect(root, identities, allowlist=None):
    return catalog.inspect_filesystem_layout(
        root, identities, silence_allowlist={} if allowlist is None else allowlist
    )


def _semantics(report):
    return report.findings, report.warnings


def test_layout_matches_frozen_walk_on_mixed_metadata(tmp_path, monkeypatch):
    root = tmp_path / "data"
    registered = _write(root, "registered.db")
    custom = _write(root, "custom/store.data")
    _write(root, "deep/" * 12 + "notes.txt")
    _write(root, "deep/" * 12 + "unclaimed.db")
    for suffix in ("-wal", "-shm", "-journal"):
        _write(root, "unclaimed.db" + suffix)
    for name in catalog.DEFAULT_FILESYSTEM_SILENCE_ALLOWLIST:
        _write(root, name)
    for directory in ("tmp", "snapshots", "old-snapshot-copy", "temporary"):
        _write(root, directory + "/copy.db")
    os.link(registered, root / "alias.db")
    os.link(registered, root / "snapshots/alias.db")
    (root / "inside.db").symlink_to(registered)
    outside = _write(tmp_path, "outside/source.db")
    (root / "outside.db").symlink_to(outside)
    (root / "outside-directory").symlink_to(outside.parent, target_is_directory=True)
    (root / "deep/back").symlink_to(root, target_is_directory=True)
    denied = root / "locked"
    denied.mkdir()
    footprint = root / "custom/locked"
    footprint.mkdir()
    missing_store = footprint / "store.data"
    identities = _identities(registered, custom, missing_store)
    original = os.scandir

    def scandir(path):
        if not isinstance(path, int) and os.fspath(path) in {str(denied), str(footprint)}:
            raise PermissionError("directory cannot be listed")
        return original(path)

    monkeypatch.setattr(os, "scandir", scandir)
    expected = _reference(root, identities, catalog.DEFAULT_FILESYSTEM_SILENCE_ALLOWLIST)
    observed = _inspect(root, identities, catalog.DEFAULT_FILESYSTEM_SILENCE_ALLOWLIST)
    assert _semantics(observed) == _semantics(expected)
    assert {finding["kind"] for finding in observed.findings} == {
        "alias_of_registered_store", "database_outside_root", "directory_outside_root",
        "uninspectable_in_store_footprint",
    }
    assert len([finding for finding in observed.findings
                if finding["kind"] == "alias_of_registered_store"]) == 2
    assert any("locked" in warning for warning in observed.warnings)
    assert any("unclaimed.db" in warning for warning in observed.warnings)
    assert not any("dead silence" in warning for warning in observed.warnings)
    assert not any("copy.db" in warning for warning in observed.warnings)


def test_ordinary_entries_need_no_realpath_or_stat(tmp_path, monkeypatch):
    root = tmp_path / "data"
    root.mkdir()
    root = root.resolve()
    leaf = root
    directories = [str(root)]
    for level in range(12):
        leaf /= f"level-{level}"
        leaf.mkdir()
        directories.append(str(leaf))
    for index in range(20):
        _write(leaf, f"ordinary-{index}.txt")
    database = _write(leaf, "orphan.db")
    custom = _write(leaf, "registered.data")
    for suffix in ("-wal", "-shm", "-journal"):
        _write(leaf, "orphan.db" + suffix)
    identities = _identities(custom)
    realpath, metadata = os.path.realpath, os.stat
    resolved, inspected = [], []

    def resolve(path, *args, **kwargs):
        resolved.append(os.fspath(path))
        return realpath(path, *args, **kwargs)

    def inspect(path, *args, **kwargs):
        if not isinstance(path, int):
            inspected.append(os.fspath(path))
        return metadata(path, *args, **kwargs)

    monkeypatch.setattr(os.path, "realpath", resolve)
    monkeypatch.setattr(os, "stat", inspect)
    report = _inspect(root, identities)
    assert resolved == [str(root), *directories]  # Initial root and every directory pop.
    assert len(inspected) == 15  # Root, twelve directories, a DB and a registered path.
    assert inspected.count(str(database)) == inspected.count(str(custom)) == 1
    assert not any("ordinary-" in path or path.endswith(("-wal", "-shm", "-journal"))
                   for path in inspected)
    assert len(report.warnings) == 1


def test_only_symlink_children_are_resolved_again(tmp_path, monkeypatch):
    root = tmp_path / "data"
    registered = _write(root, "registered.db")
    alias = root / "link.db"
    alias.symlink_to(registered)
    identities = _identities(registered)
    original, calls = os.path.realpath, []

    def realpath(path, *args, **kwargs):
        calls.append(os.fspath(path))
        return original(path, *args, **kwargs)

    monkeypatch.setattr(os.path, "realpath", realpath)
    report = _inspect(root, identities)
    assert calls == [str(root), str(root), str(alias)]
    assert str(registered) not in calls
    assert report.findings == [] and report.warnings == []


def test_unreadable_ordinary_file_is_no_longer_inspected(tmp_path, monkeypatch):
    ordinary = _write(tmp_path, "ordinary.txt")
    original = os.stat

    def inspect(path, *args, **kwargs):
        if not isinstance(path, int) and os.fspath(path) == str(ordinary):
            raise PermissionError("ordinary file cannot be inspected")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", inspect)
    old = _reference(tmp_path, {})
    new = _inspect(tmp_path, {})
    assert not old.findings and len(old.warnings) == 1
    assert not new.findings and new.warnings == []


@pytest.mark.parametrize("in_footprint", [False, True])
def test_failed_directory_stat_keeps_footprint_and_names_directory(
    tmp_path, monkeypatch, in_footprint
):
    directory = tmp_path / "blocked"
    directory.mkdir()
    identities = _identities(directory / "nested.data") if in_footprint else {}
    original = os.stat

    def inspect(path, *args, **kwargs):
        if not isinstance(path, int) and os.fspath(path) == str(directory):
            raise PermissionError("directory metadata unavailable")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", inspect)
    old = _reference(tmp_path, identities)
    new = _inspect(tmp_path, identities)
    if in_footprint:
        assert len(old.findings) == len(new.findings) == 1
        assert new.findings[0]["kind"] == old.findings[0]["kind"]
        assert new.findings[0]["detail"] == old.findings[0]["detail"].replace(
            "file or directory", "directory"
        )
        assert not new.warnings
    else:
        assert not old.findings and not new.findings
        assert len(old.warnings) == len(new.warnings) == 1
        assert new.warnings[0] == old.warnings[0].replace("file or directory", "directory")
    assert "file or directory" not in str(_semantics(new))


def test_registered_non_database_name_keeps_its_stat_error(tmp_path, monkeypatch):
    registered = _write(tmp_path, "store.data")
    identities = _identities(registered)
    original = os.stat

    def inspect(path, *args, **kwargs):
        if not isinstance(path, int) and os.fspath(path) == str(registered):
            raise PermissionError("registered metadata unavailable")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", inspect)
    report = _inspect(tmp_path, identities)
    assert _semantics(report) == _semantics(_reference(tmp_path, identities))
    assert len(report.findings) == 1
    assert report.findings[0]["kind"] == "uninspectable_in_store_footprint"


def test_unmanaged_workspace_is_pruned_before_its_entries(tmp_path, monkeypatch):
    root = tmp_path / "data"
    registered = _write(root, "main.db")
    workspace = root / "agents/worker/workspace"
    _write(workspace, "build/copy.db")
    os.link(registered, workspace / "alias.db")
    outside = tmp_path / "outside"
    outside.mkdir()
    (workspace / "escape").symlink_to(outside, target_is_directory=True)
    original = os.scandir
    visited = []

    def scandir(path):
        visited.append(os.fspath(path))
        return original(path)

    monkeypatch.setattr(os, "scandir", scandir)
    report = _inspect(root, _identities(registered))
    assert report.findings == [] and report.warnings == []
    assert getattr(report, "skipped_unmanaged_subtrees", 0) == 1
    assert str(workspace) not in visited
    assert not any(path.startswith(str(workspace) + os.sep) for path in visited)


@pytest.mark.parametrize("store_at_directory", [False, True])
def test_registered_workspace_footprint_is_walked(tmp_path, store_at_directory):
    workspace = tmp_path / "agents/worker/workspace"
    registered = _write(workspace, "store.data")
    _write(workspace, "unclaimed.db")
    target = workspace if store_at_directory else registered
    identities = _identities(target)
    report = _inspect(tmp_path, identities)
    assert _semantics(report) == _semantics(_reference(tmp_path, identities))
    assert getattr(report, "skipped_unmanaged_subtrees", 0) == 0
    assert len(report.warnings) == 1 and "unclaimed.db" in report.warnings[0]


def test_sibling_store_does_not_claim_an_unmanaged_workspace(tmp_path):
    workspace = tmp_path / "agents/worker/workspace"
    _write(workspace, "hidden.db")
    registered = _write(tmp_path, "agents/worker/workspace-copy/main.db")
    report = _inspect(tmp_path, _identities(registered))
    assert report.findings == [] and report.warnings == []
    assert getattr(report, "skipped_unmanaged_subtrees", 0) == 1


def test_symlink_into_workspace_uses_the_resolved_skip_rule(tmp_path):
    workspace = tmp_path / "agents/worker/workspace"
    _write(workspace, "copy.db")
    (tmp_path / "000-link").symlink_to(workspace, target_is_directory=True)
    report = _inspect(tmp_path, {})
    assert report.findings == [] and report.warnings == []
    assert getattr(report, "skipped_unmanaged_subtrees", 0) == 1


def test_workspace_symlink_to_managed_directory_is_walked(tmp_path):
    managed = tmp_path / "managed"
    _write(managed, "orphan.db")
    workspace = tmp_path / "agents/worker/workspace"
    workspace.parent.mkdir(parents=True)
    workspace.symlink_to(managed, target_is_directory=True)
    report = _inspect(tmp_path, {})
    assert _semantics(report) == _semantics(_reference(tmp_path, {}))
    assert len(report.warnings) == 1 and "orphan.db" in report.warnings[0]
    assert getattr(report, "skipped_unmanaged_subtrees", 0) == 0


def test_reconcile_reports_skipped_workspace_count(tmp_path, caplog):
    _write(tmp_path, "agents/worker/workspace/copy.db")
    store = StoreCatalog(expected_root=tmp_path, silence_allowlist={})
    try:
        with caplog.at_level(logging.INFO, logger="pinky.store_catalog"):
            warnings = store.reconcile_filesystem()
        assert warnings == []
        messages = [record.getMessage() for record in caplog.records
                    if record.name == "pinky.store_catalog" and record.levelno == logging.INFO]
        assert len(messages) == 1
        assert "skipped" in messages[0] and "1" in messages[0]
    finally:
        store.close()


@pytest.mark.parametrize("failed_probe", ["symlink", "directory"])
def test_entry_type_failure_keeps_the_carried_resolved_path(tmp_path, monkeypatch, failed_probe):
    root = tmp_path / "data"
    registered = _write(root, "protected/store.data")
    directory = registered.parent
    identities = _identities(registered)
    scan, metadata = os.scandir, os.stat

    def denied():
        raise PermissionError("entry metadata unavailable")

    class Entry:
        def __init__(self, entry):
            self.entry, self.path, self.name = entry, entry.path, entry.name

        def is_symlink(self):
            return denied() if failed_probe == "symlink" else self.entry.is_symlink()

        def is_dir(self, *, follow_symlinks):
            return denied()

    class Listing:
        def __init__(self, path):
            self.listing = scan(path)

        def __enter__(self):
            return (Entry(entry) if entry.path == str(directory) else entry
                    for entry in self.listing.__enter__())

        def __exit__(self, *args):
            return self.listing.__exit__(*args)

    def inspect(path, *args, **kwargs):
        if not isinstance(path, int) and os.fspath(path) == str(directory):
            denied()
        return metadata(path, *args, **kwargs)

    monkeypatch.setattr(os, "scandir", Listing)
    monkeypatch.setattr(os, "stat", inspect)
    old = _reference(root, identities)
    new = _inspect(root, identities)
    assert _semantics(new) == _semantics(old)
    assert len(new.findings) == 1
    assert new.findings[0]["resolved_path"] == str(directory)


@pytest.mark.parametrize("replacement", ["outside", "parent-outside", "inside", "file"])
def test_queued_directory_changes_match_frozen_walk(tmp_path, monkeypatch, replacement):
    root = tmp_path / "data"
    root.mkdir()
    parent = root / "parent" if replacement == "parent-outside" else root
    trigger = parent / "000-trigger"
    queued = parent / "queued"
    trigger.mkdir(parents=True)
    queued.mkdir()
    retired = root / "retired"
    outside = tmp_path / "outside"
    inside = root / "zz-target"
    if replacement == "parent-outside":
        _write(queued, "escaped.db")
    elif replacement == "outside":
        _write(outside, "escaped.db")
    elif replacement == "inside":
        _write(inside, "unclaimed.db")
    original_scandir = os.scandir

    def run(inspector):
        swaps = []

        def scandir(path):
            if os.fspath(path) == str(trigger) and not swaps:
                if replacement == "parent-outside":
                    parent.rename(outside)
                    parent.symlink_to(outside, target_is_directory=True)
                else:
                    queued.rename(retired)
                    if replacement == "file":
                        queued.write_bytes(b"ordinary replacement")
                    else:
                        queued.symlink_to(
                            outside if replacement == "outside" else inside,
                            target_is_directory=True,
                        )
                swaps.append(path)
            return original_scandir(path)

        try:
            with monkeypatch.context() as patch:
                patch.setattr(os, "scandir", scandir)
                report = inspector(root, {})
            assert swaps == [str(trigger)], "The replacement must happen after enqueue"
            return report
        finally:
            if swaps:
                if replacement == "parent-outside":
                    parent.unlink()
                    outside.rename(parent)
                else:
                    queued.unlink()
                    retired.rename(queued)

    observed = run(_inspect)
    expected = run(_reference)
    assert _semantics(observed) == _semantics(expected)
    if replacement in {"outside", "parent-outside"}:
        assert [row["kind"] for row in observed.findings] == ["directory_outside_root"]
        assert observed.findings[0]["resolved_path"] == str(
            outside / "queued" if replacement == "parent-outside" else outside
        )
        assert observed.warnings == []
    elif replacement == "inside":
        assert observed.findings == []
        assert len(observed.warnings) == 1 and "zz-target/unclaimed.db" in observed.warnings[0]
    else:
        assert observed.findings == [] and observed.warnings == []


@pytest.mark.parametrize("relative", [
    "agents/a/workspace", "agents/a/b/workspace",
    "agents/a/build/cache/workspace", "agents/workspace",
    "agents/a/data/workspace", "agents/a/.claude/cache/workspace",
])
def test_unmanaged_pattern_matches_exact_path_segments(tmp_path, relative):
    root = tmp_path / "data"
    registered = _write(root, "registered.db")
    workspace = root / relative
    workspace.mkdir(parents=True)
    os.link(registered, workspace / "alias.db")
    outside = _write(tmp_path, "outside/escaped.db").parent
    (workspace / "escape").symlink_to(outside, target_is_directory=True)
    identities = _identities(registered)
    report = _inspect(root, identities)
    # Unknown first-level agent directories are pruned before their nested
    # workspace names matter. Owned ancestors retain the exact workspace rule.
    if relative in {
        "agents/a/workspace", "agents/a/b/workspace", "agents/a/build/cache/workspace",
    }:
        assert report.findings == [] and report.warnings == []
        assert report.skipped_unmanaged_subtrees == 1
    else:
        assert _semantics(report) == _semantics(_reference(root, identities))
        assert {row["kind"] for row in report.findings} == {
            "alias_of_registered_store", "directory_outside_root",
        }
        assert report.warnings == []
        assert report.skipped_unmanaged_subtrees == 0
