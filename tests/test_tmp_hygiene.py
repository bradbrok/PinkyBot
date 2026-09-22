"""Temporary trees remain inspectable after tests deliberately freeze modes."""

from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest


def test_restore_frozen_tree_top_down_and_idempotently(tmp_path):
    from tests._tmp_hygiene import restore_tree_readability

    root = tmp_path / "root"
    root.mkdir()
    frozen_file = root / "file"
    frozen_file.write_text("file")
    frozen_dir = root / "directory"
    frozen_dir.mkdir()
    nested_file = frozen_dir / "nested"
    nested_file.write_text("nested")
    for path in (frozen_file, nested_file, frozen_dir, root):
        path.chmod(0)

    try:
        assert restore_tree_readability(root) == 4
        assert frozen_file.read_text() == "file"
        assert nested_file.read_text() == "nested"
        for path in (root, frozen_dir):
            assert stat.S_IMODE(path.stat().st_mode) == 0o700
        for path in (frozen_file, nested_file):
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert restore_tree_readability(root) == 0
    finally:
        for path in (root, frozen_dir, frozen_file, nested_file):
            path.chmod(0o700 if path in (root, frozen_dir) else 0o600)


@pytest.mark.parametrize("mode", [0o100, 0o400, 0o050])
def test_restore_directory_missing_owner_read_or_execute(tmp_path, mode):
    from tests._tmp_hygiene import restore_tree_readability

    directory = tmp_path / "directory"
    directory.mkdir()
    directory.chmod(mode)
    try:
        assert restore_tree_readability(tmp_path) == 1
        assert stat.S_IMODE(directory.stat().st_mode) == mode | 0o700
        assert restore_tree_readability(tmp_path) == 0
    finally:
        directory.chmod(0o700)


def test_preserve_already_readable_modes(tmp_path):
    from tests._tmp_hygiene import restore_tree_readability

    readable = tmp_path / "readable"
    readable.write_text("keep read-only")
    readable.chmod(0o400)
    unreadable = tmp_path / "unreadable"
    unreadable.write_text("keep group and other bits")
    unreadable.chmod(0o051)

    assert restore_tree_readability(tmp_path) == 1
    assert stat.S_IMODE(readable.stat().st_mode) == 0o400
    assert stat.S_IMODE(unreadable.stat().st_mode) == 0o651
    assert restore_tree_readability(tmp_path) == 0


@pytest.mark.parametrize("target_kind", ["file", "directory", "missing"])
def test_restore_symlink_without_following_it(tmp_path, target_kind):
    from tests._tmp_hygiene import restore_tree_readability

    root = tmp_path / "root"
    root.mkdir()
    target = tmp_path / "target"
    if target_kind == "directory":
        target.mkdir()
    elif target_kind == "file":
        target.write_text("untouched")
    link = root / "link"
    link.symlink_to(target, target_is_directory=target_kind == "directory")
    if target_kind != "missing":
        target.chmod(0)

    try:
        # Linux symlinks have immutable 0777 modes; macOS supports mode 000.
        if hasattr(os, "lchmod"):
            os.lchmod(link, 0)
            expected_changes = 1
        else:
            expected_changes = 0
        assert restore_tree_readability(root) == expected_changes
        assert link.is_symlink()
        assert link.lstat().st_mode & stat.S_IRUSR
        assert link.readlink() == target
        if target_kind == "missing":
            assert not target.exists()
        else:
            assert stat.S_IMODE(target.stat().st_mode) == 0
        assert restore_tree_readability(root) == 0
    finally:
        if target_kind != "missing":
            target.chmod(0o700 if target_kind == "directory" else 0o600)
        if hasattr(os, "lchmod"):
            os.lchmod(link, 0o600)


def test_umask_test_leaves_readable_basetemp(tmp_path):
    basetemp = tmp_path / "basetemp"
    child_tmpdir = tmp_path / "tmpdir"
    child_tmpdir.mkdir()
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "tests/test_codex_home.py::"
            "test_prepare_publishes_soul_mode_0600_under_restrictive_umask",
            f"--basetemp={basetemp}",
        ],
        cwd=Path(__file__).resolve().parents[1],
        env={**os.environ, "TMPDIR": str(child_tmpdir)},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    probe = subprocess.run(
        [
            "find",
            str(basetemp),
            "(",
            "-type",
            "l",
            "!",
            "-perm",
            "-u+r",
            ")",
            "-o",
            "(",
            "-type",
            "f",
            "!",
            "-perm",
            "-u+r",
            ")",
            "-o",
            "(",
            "-type",
            "d",
            "!",
            "-perm",
            "-u+rx",
            ")",
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert probe.returncode == 0, probe.stderr
    assert probe.stdout == "", probe.stdout
