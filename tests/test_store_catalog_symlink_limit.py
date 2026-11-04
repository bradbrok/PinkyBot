"""Filesystem diagnostics survive directory aliases beyond the kernel hop limit."""

from __future__ import annotations

import errno
import os
from contextlib import nullcontext
from pathlib import Path

import pytest

from pinky_daemon import store_catalog as stores


def _long_alias_tree(tmp_path: Path):
    root = tmp_path / "root"
    stash = root / "agents" / "bob" / "stash"
    target = stash / "d48"
    target.mkdir(parents=True)
    for index in reversed(range(48)):
        (stash / f"d{index}").symlink_to(f"d{index + 1}", target_is_directory=True)
    alias = root / "agents" / "alice" / "data" / "d0"
    alias.parent.mkdir(parents=True)
    alias.symlink_to(stash / "d0", target_is_directory=True)
    return root, alias, target


def _inspect(root):
    return stores.inspect_filesystem_layout(
        root, {}, silence_allowlist={}, registered_agents={"alice"},
    )


def _assert_outward(report, path, target):
    assert len(report.findings) == 1
    finding = report.findings[0]
    assert finding["kind"] == "database_outside_root"
    assert finding["path"] == str(path)
    assert finding["resolved_path"] == str(target)
    assert finding["scope"] == "alice"


@pytest.mark.parametrize("target_present", [False, True])
def test_long_alias_database_entry_keeps_outward_finding(tmp_path, target_present):
    root, alias, directory = _long_alias_tree(tmp_path)
    target = tmp_path / "outside.db"
    if target_present:
        target.touch()
    (directory / "escape.db").symlink_to(target)
    with pytest.raises(OSError) as error:
        os.lstat(alias / "escape.db")
    assert error.value.errno == errno.ELOOP

    _assert_outward(_inspect(root), alias / "escape.db", target)


@pytest.mark.parametrize("check_errno", [None, errno.EACCES, errno.ELOOP])
def test_long_alias_queued_database_link_is_checked(tmp_path, monkeypatch, check_errno):
    root, alias, directory = _long_alias_tree(tmp_path)
    trigger, queued = directory / "000-trigger", directory / "queued.db"
    trigger.mkdir()
    queued.mkdir()
    target = tmp_path / "missing.db"
    original_scandir = os.scandir
    original_check = stores._FilesystemLayoutInspector._record_outward_database_link
    original_lstat = os.lstat
    replaced = []

    def scandir(path):
        if os.fspath(path) == str(trigger) and not replaced:
            queued.rmdir()
            queued.symlink_to(target)
            replaced.append(str(queued))
        return original_scandir(path)

    def denied_lstat(path, *args, **kwargs):
        if os.path.basename(path) == "queued.db":
            raise OSError(check_errno, os.strerror(check_errno), os.fspath(path))
        return original_lstat(path, *args, **kwargs)

    def check(inspector, path, resolved_path, *args, **kwargs):
        if check_errno is not None and path == str(alias / "queued.db"):
            with monkeypatch.context() as patch:
                patch.setattr(os, "lstat", denied_lstat)
                return original_check(inspector, path, resolved_path, *args, **kwargs)
        return original_check(inspector, path, resolved_path, *args, **kwargs)

    monkeypatch.setattr(os, "scandir", scandir)
    monkeypatch.setattr(stores._FilesystemLayoutInspector, "_record_outward_database_link", check)
    report = _inspect(root)

    assert replaced == [str(queued)]
    if check_errno is None:
        _assert_outward(report, alias / "queued.db", target)
    else:
        assert report.findings == []
        assert len(report.warnings) == 1
        assert report.warnings[0].startswith("could not inspect database directory")
        assert str(alias / "queued.db") in report.warnings[0]


@pytest.mark.parametrize("different_target", [False, True])
def test_unclaimed_entry_dedup_includes_target_identity(tmp_path, monkeypatch, different_target):
    root = tmp_path / "root"
    root.mkdir()
    first, second = root / "first.db", root / "second.db"
    first.touch()
    second.touch()
    metadata = root.stat()
    entry_identity = ((metadata.st_dev, metadata.st_ino), "x.db")
    inspector = stores._FilesystemLayoutInspector(root, {}, {}, {"alice"})
    targets = (first, second if different_target else first)
    for target in targets:
        inspector._record_database(
            str(root / "x.db"), str(target), target.stat(), entry_identity=entry_identity,
        )
    monkeypatch.setattr(os, "scandir", lambda path: nullcontext([]))

    report = inspector.inspect()

    assert report.findings == []
    assert len(report.warnings) == len(set(targets))
    for target in set(targets):
        assert sum(f"resolved_path={str(target)!r}" in warning for warning in report.warnings) == 1
    assert all(warning.startswith("unclaimed database file:") for warning in report.warnings)
