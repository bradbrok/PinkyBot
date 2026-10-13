from __future__ import annotations

import builtins
import errno
import os
from pathlib import Path

import pytest

from pinky_daemon import store_catalog as storage


def _catalog(root: Path, *stores: Path) -> storage.StoreCatalog:
    catalog = storage.StoreCatalog(expected_root=root, silence_allowlist={})
    for index, path in enumerate(stores):
        catalog.register(
            "tasks" if index == 0 else "audit",
            path,
            journal_mode="delete",
            owner="FixtureStore",
        )
    return catalog


def _identities(*paths: Path) -> dict[str, tuple[int, int]]:
    result = {}
    for path in paths:
        metadata = path.stat()
        result[str(path)] = (metadata.st_dev, metadata.st_ino)
    return result


def _inspect(root: Path, identities=None, *, allowlist=None):
    return storage.inspect_filesystem_layout(
        str(root), identities or {}, silence_allowlist={} if allowlist is None else allowlist
    )


def _escape_message(root: Path, path: Path, target: Path, *, database=False) -> str:
    subject = "database" if database else "directory"
    return (
        "Store catalog filesystem alias coverage failed:\n- "
        f"{subject} symlink escapes expected root {str(root)!r}: "
        f"path={str(path)!r} resolved_path={str(target)!r}"
    )


def _alias_detail(root: Path, path: Path, registered: Path) -> str:
    metadata = path.stat()
    return (
        "unregistered path aliases registered store inode: "
        f"path={path.relative_to(root).as_posix()!r} resolved_path={str(path)!r} "
        f"dev_ino={(metadata.st_dev, metadata.st_ino)!r} "
        f"registered_realpaths={[str(registered)]!r}"
    )


@pytest.mark.parametrize("outside", [True, False])
@pytest.mark.parametrize("database", [False, True])
def test_layout_reports_external_links(tmp_path: Path, outside: bool, database: bool) -> None:
    root = tmp_path / "root"
    root.mkdir()
    scope = root / "scopes" / "sample"
    scope.mkdir(parents=True)
    target = (tmp_path if outside else root) / ("target.db" if database else "target")
    target.touch() if database else target.mkdir()
    link = scope / ("linked.db" if database else "linked")
    link.symlink_to(target, target_is_directory=not database)
    report = _inspect(root)
    catalog = _catalog(root)
    if outside:
        message = _escape_message(root, link, target, database=database)
        assert report.findings == [{
            "kind": "database_outside_root" if database else "directory_outside_root",
            "path": str(link), "resolved_path": str(target), "detail": message,
        }]
        with pytest.raises(storage.StoreCatalogError) as raised:
            catalog.validate()
        assert str(raised.value) == message
    else:
        assert report.findings == []
        assert catalog.validate() == report.warnings


@pytest.mark.parametrize("second_outside", [True, False])
def test_layout_collects_all_findings_in_walk_order(tmp_path: Path, second_outside: bool) -> None:
    root = tmp_path / "root"
    root.mkdir()
    nested = root / "a-nested"
    nested.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    safe = root / "safe"
    safe.mkdir()
    first = root / "z-exit"
    first.symlink_to(outside, target_is_directory=True)
    second = nested / "exit"
    second.symlink_to(outside if second_outside else safe, target_is_directory=True)
    report = _inspect(root)
    assert [finding["path"] for finding in report.findings] == (
        [str(first), str(second)] if second_outside else [str(first)]
    )


@pytest.mark.parametrize("outside", [True, False])
def test_layout_never_enters_external_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outside: bool
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    target = (tmp_path if outside else root) / "target"
    target.mkdir()
    child = target / "child.db"
    child.touch()
    (root / "link").symlink_to(target, target_is_directory=True)
    real_stat, real_scandir = os.stat, os.scandir
    stated, scanned = [], []

    def recording_stat(path, *args, **kwargs):
        stated.append(os.fspath(path))
        return real_stat(path, *args, **kwargs)

    def recording_scandir(path):
        scanned.append(os.fspath(path))
        return real_scandir(path)

    monkeypatch.setattr(storage.os, "stat", recording_stat)
    monkeypatch.setattr(storage.os, "scandir", recording_scandir)
    report = _inspect(root)
    if outside:
        assert len(report.findings) == 1
        assert str(target) not in scanned
        assert not any(path.startswith(str(target) + os.sep) for path in stated)
    else:
        assert report.findings == []
        assert str(target) in scanned
        assert str(child) in stated


@pytest.mark.parametrize("outside_on_pop", [False, True])
def test_layout_rechecks_queued_directory(tmp_path, monkeypatch, outside_on_pop):
    root = tmp_path / "root"
    root.mkdir()
    inside = root / "z-target"
    inside.mkdir()
    (inside / "inside.db").touch()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "child.db").touch()
    link = root / "a-link"
    link.symlink_to(inside, target_is_directory=True)
    realpath, realstat, realscandir = os.path.realpath, os.stat, os.scandir
    resolutions, scanned, stated = [], [], []

    def resolve(path, *args, **kwargs):
        if os.fspath(path) == str(link):
            resolutions.append(True)
            if outside_on_pop and len(resolutions) == 2:
                link.unlink()
                link.symlink_to(outside, target_is_directory=True)
        return realpath(path, *args, **kwargs)

    def scan(path):
        scanned.append(os.fspath(path))
        return realscandir(path)

    def stat_path(path, *args, **kwargs):
        stated.append(os.fspath(path))
        return realstat(path, *args, **kwargs)

    monkeypatch.setattr(storage.os.path, "realpath", resolve)
    monkeypatch.setattr(storage.os, "scandir", scan)
    monkeypatch.setattr(storage.os, "stat", stat_path)
    report = _inspect(root)
    assert len(resolutions) >= 2
    if outside_on_pop:
        assert report.findings == [{
            "kind": "directory_outside_root", "path": str(link),
            "resolved_path": str(outside), "detail": _escape_message(root, link, outside),
        }]
        assert str(outside) not in scanned
        assert not any(path.startswith(str(outside) + os.sep) for path in stated)
    else:
        assert report.findings == []
        assert str(inside) in scanned and str(inside / "inside.db") in stated


@pytest.mark.parametrize("registered", [True, False])
def test_layout_reports_uninspectable_store_footprint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, registered: bool
) -> None:
    directory = tmp_path / "locked"
    directory.mkdir()
    store = directory / "tasks.db"
    store.touch()
    identities = _identities(store) if registered else {}
    catalog = _catalog(tmp_path, *([store] if registered else []))
    failure = PermissionError(errno.EACCES, "fixture denied", str(directory))
    real_scandir = os.scandir

    def denied_scandir(path):
        if os.fspath(path) == str(directory):
            raise failure
        return real_scandir(path)

    monkeypatch.setattr(storage.os, "scandir", denied_scandir)
    report = _inspect(tmp_path, identities)
    if registered:
        message = (
            "Store catalog filesystem alias coverage failed:\n- "
            f"could not inspect database directory: path={str(directory)!r} "
            f"error=PermissionError: {failure}"
        )
        assert report.findings == [{
            "kind": "uninspectable_in_store_footprint", "path": str(directory),
            "resolved_path": str(directory), "detail": message,
        }]
        with pytest.raises(storage.StoreCatalogError) as raised:
            catalog.validate()
        assert str(raised.value) == message
    else:
        assert report.findings == []
        assert len(report.warnings) == 1
        assert "outside registered store footprint" in report.warnings[0]
        assert catalog.validate() == report.warnings


@pytest.mark.parametrize("resolution_fails", [True, False])
def test_unresolved_footprint_finding_has_null_resolved_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, resolution_fails: bool
) -> None:
    store = tmp_path / "tasks.db"
    store.touch()
    identities = _identities(store)
    real_realpath = os.path.realpath

    def resolve(path, *args, **kwargs):
        if resolution_fails and os.fspath(path) == str(store):
            raise PermissionError(errno.EACCES, "fixture denied", str(store))
        return real_realpath(path, *args, **kwargs)

    monkeypatch.setattr(storage.os.path, "realpath", resolve)
    report = _inspect(tmp_path, identities)
    if resolution_fails:
        assert len(report.findings) == 1
        assert report.findings[0]["kind"] == "uninspectable_in_store_footprint"
        assert report.findings[0]["path"] == str(store)
        assert report.findings[0]["resolved_path"] is None
    else:
        assert report.findings == []


@pytest.mark.parametrize("hard_link", [True, False])
@pytest.mark.parametrize("suppression", ["ordinary", "allowlisted", "ignored"])
def test_layout_reports_registered_inode_aliases(
    tmp_path: Path, hard_link: bool, suppression: str
) -> None:
    store = tmp_path / "tasks.db"
    store.touch()
    directory = tmp_path / "snapshots" if suppression == "ignored" else tmp_path
    directory.mkdir(exist_ok=True)
    alias = directory / "copy.db"
    os.link(store, alias) if hard_link else alias.touch()
    allowlist = {"copy.db": "fixture suppression"} if suppression == "allowlisted" else {}
    report = _inspect(tmp_path, _identities(store), allowlist=allowlist)
    if hard_link:
        detail = _alias_detail(tmp_path, alias, store)
        assert report.findings == [{
            "kind": "alias_of_registered_store", "path": str(alias),
            "resolved_path": str(alias), "detail": detail,
        }]
    else:
        assert report.findings == []


@pytest.mark.parametrize("external_link", [True, False])
def test_legacy_reconciliation_preserves_mixed_finding_precedence(
    tmp_path: Path, external_link: bool
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    store = root / "tasks.db"
    store.touch()
    aliases = [root / "a-alias.db", root / "b-alias.db"]
    for alias in aliases:
        os.link(store, alias)
    target = tmp_path / "outside" if external_link else root / "safe"
    target.mkdir()
    link = root / "z-link"
    link.symlink_to(target, target_is_directory=True)
    report = _inspect(root, _identities(store))
    assert [finding["kind"] for finding in report.findings] == (
        ["alias_of_registered_store", "alias_of_registered_store", "directory_outside_root"]
        if external_link else ["alias_of_registered_store", "alias_of_registered_store"]
    )
    with pytest.raises(storage.StoreCatalogError) as raised:
        _catalog(root, store).validate()
    expected = (
        _escape_message(root, link, target)
        if external_link else "Store catalog filesystem reconciliation failed:\n- "
        + "\n- ".join(_alias_detail(root, alias, store) for alias in aliases)
    )
    assert str(raised.value) == expected


def test_alias_finding_uses_absolute_discovered_path(tmp_path: Path) -> None:
    store = tmp_path / "tasks.db"
    store.touch()
    alias = tmp_path / "b-alias.db"
    os.link(store, alias)
    link = tmp_path / "a-link.db"
    link.symlink_to(alias)
    report = _inspect(tmp_path, _identities(store))
    assert [finding["path"] for finding in report.findings] == [str(link), str(alias)]
    assert all(finding["resolved_path"] == str(alias) for finding in report.findings)
    assert all(finding["detail"] == _alias_detail(tmp_path, alias, store)
               for finding in report.findings)


def test_inspection_preserves_warning_order_without_logging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    orphan = tmp_path / "a-orphan.db"
    orphan.touch()
    locked = tmp_path / "z-locked"
    locked.mkdir()
    real_scandir = os.scandir

    def denied_scandir(path):
        if os.fspath(path) == str(locked):
            raise PermissionError(errno.EACCES, "fixture denied", str(locked))
        return real_scandir(path)

    monkeypatch.setattr(storage.os, "scandir", denied_scandir)
    report = _inspect(tmp_path, allowlist={"absent.db": "fixture obsolete"})
    assert report.findings == []
    assert len(report.warnings) == 3
    assert "could not inspect" in report.warnings[0]
    assert "unclaimed database file" in report.warnings[1]
    assert "dead silence-allowlist" in report.warnings[2]
    assert "STORE CATALOG WARNING" not in caplog.text
    catalog = storage.StoreCatalog(
        expected_root=tmp_path, silence_allowlist={"absent.db": "fixture obsolete"}
    )
    assert catalog.reconcile_filesystem() == report.warnings
    assert all(warning in caplog.text for warning in report.warnings)


@pytest.mark.parametrize("exists", [True, False])
def test_identity_reader_retains_missing_paths(tmp_path: Path, exists: bool) -> None:
    store = tmp_path / "tasks.db"
    if exists:
        store.touch()
    expected = _identities(store)[str(store)] if exists else None
    assert storage.read_store_identities([str(store)]) == {str(store): expected}


@pytest.mark.parametrize("denied", [True, False])
def test_identity_reader_propagates_nonmissing_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, denied: bool
) -> None:
    store = tmp_path / "tasks.db"
    store.touch()
    expected = _identities(store)
    failure = PermissionError(errno.EACCES, "fixture denied", str(store))
    real_stat = os.stat

    def stat_path(path, *args, **kwargs):
        if denied and os.fspath(path) == str(store):
            raise failure
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(storage.os, "stat", stat_path)
    if denied:
        with pytest.raises(PermissionError) as raised:
            storage.read_store_identities([str(store)])
        assert raised.value is failure
        assert raised.value.filename == str(store)
    else:
        assert storage.read_store_identities([str(store)]) == expected


def test_missing_store_retains_footprint_coverage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "locked"
    directory.mkdir()
    store = directory / "missing.db"
    identities = storage.read_store_identities([str(store)])
    real_scandir = os.scandir

    def denied_scandir(path):
        if os.fspath(path) == str(directory):
            raise PermissionError(errno.EACCES, "fixture denied", str(directory))
        return real_scandir(path)

    monkeypatch.setattr(storage.os, "scandir", denied_scandir)
    assert _inspect(tmp_path, identities).findings[0]["kind"] == "uninspectable_in_store_footprint"
    assert _inspect(tmp_path).findings == []


def test_inspection_is_read_only_and_does_not_mutate_catalog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    store = tmp_path / "tasks.db"
    store.touch()
    catalog = _catalog(tmp_path, store)
    original_record = catalog._entries[0].record
    original_observations = list(catalog._observations)
    store.rename(tmp_path / "retired.txt")
    store.touch()
    alias = tmp_path / "alias.db"
    os.link(store, alias)
    stat_calls = []
    real_stat = os.stat

    def forbidden(*args, **kwargs):
        raise AssertionError("inspection must not open content, snapshot, or refresh catalog state")

    def recording_stat(path, *args, **kwargs):
        stat_calls.append(os.fspath(path))
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(catalog, "snapshot", forbidden)
    monkeypatch.setattr(catalog, "_refresh_identities", forbidden)
    monkeypatch.setattr(builtins, "open", forbidden)
    monkeypatch.setattr(storage.os, "open", forbidden)
    monkeypatch.setattr(storage.os, "stat", recording_stat)
    report = catalog.inspect_layout()
    assert [finding["path"] for finding in report.findings] == [str(alias)]
    assert str(store) in stat_calls
    assert catalog._entries[0].record is original_record
    assert catalog._observations == original_observations
    assert "STORE CATALOG WARNING" not in caplog.text


def test_legacy_external_directory_message_control(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    link = root / "link"
    link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(storage.StoreCatalogError) as raised:
        _catalog(root).validate()
    assert str(raised.value) == _escape_message(root, link, outside)


def test_legacy_clean_root_control(tmp_path: Path) -> None:
    assert _catalog(tmp_path).validate() == []
