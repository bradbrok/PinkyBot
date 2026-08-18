"""Single-writer authority and daemon-down descriptor checks for SQLite stores."""

from __future__ import annotations

import contextlib
import fcntl
import os
import shutil
import stat
import subprocess
from collections.abc import Iterator, Sequence
from pathlib import Path

_LOCK_FILENAME = ".pinky-store-authority.lock"
_LSOF_TIMEOUT_SECONDS = 5.0


class StoreAuthorityError(RuntimeError):
    """Raised when exclusive authority over the daemon store root is unavailable."""


def store_authority_lock_path(db_path: str | os.PathLike[str]) -> str:
    """Return the canonical, stable lock inode path for one daemon DB root."""
    root = Path(os.path.realpath(os.fspath(db_path))).parent
    return os.fspath(root / _LOCK_FILENAME)


@contextlib.contextmanager
def store_authority_lock(db_path: str | os.PathLike[str]) -> Iterator[None]:
    """Hold a nonblocking exclusive flock without ever unlinking its inode."""
    lock_path = Path(store_authority_lock_path(db_path))
    try:
        lock_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        raise StoreAuthorityError("daemon store authority lock could not be opened") from exc
    try:
        try:
            descriptor_stat = os.fstat(descriptor)
            path_stat = lock_path.lstat()
        except OSError as exc:
            raise StoreAuthorityError("daemon store authority lock could not be verified") from exc
        if (
            not stat.S_ISREG(descriptor_stat.st_mode)
            or stat.S_ISLNK(path_stat.st_mode)
            or (descriptor_stat.st_dev, descriptor_stat.st_ino)
            != (path_stat.st_dev, path_stat.st_ino)
        ):
            raise StoreAuthorityError("daemon store authority lock path is unsafe")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, PermissionError) as exc:
            raise StoreAuthorityError("daemon store authority is already held") from exc
        try:
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def _resolve_lsof_binary() -> str | None:
    binary = shutil.which("lsof")
    if binary:
        return binary
    for candidate in ("/usr/sbin/lsof", "/usr/bin/lsof"):
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def assert_no_open_store_descriptors(paths: Sequence[Path]) -> None:
    """Fail closed unless lsof proves none of the supplied inodes are open."""
    identities = {_regular_file_identity(path) for path in paths}
    lsof = _resolve_lsof_binary()
    if lsof is None:
        raise StoreAuthorityError("descriptor scan is unavailable")
    try:
        completed = subprocess.run(
            [lsof, "-nP", "-FpfDi", "--", *(os.fspath(path) for path in paths)],
            capture_output=True,
            text=True,
            timeout=_LSOF_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise StoreAuthorityError("descriptor scan did not complete") from exc
    if completed.returncode not in (0, 1) or completed.stderr.strip():
        raise StoreAuthorityError("descriptor scan returned an unusable result")
    observed = _parse_lsof_identities(completed.stdout)
    if identities & observed:
        raise StoreAuthorityError("a daemon store file descriptor is still open")


def _regular_file_identity(path: Path) -> tuple[int, int]:
    path_stat = path.lstat()
    if stat.S_ISLNK(path_stat.st_mode) or not stat.S_ISREG(path_stat.st_mode):
        raise StoreAuthorityError("descriptor target is not a regular file")
    return (path_stat.st_dev, path_stat.st_ino)


def _parse_lsof_identities(output: str) -> set[tuple[int, int]]:
    if not output:
        return set()

    identities: set[tuple[int, int]] = set()
    saw_process = False
    current_descriptor = False
    device: int | None = None
    inode: int | None = None

    def finish_descriptor() -> None:
        nonlocal current_descriptor, device, inode
        if not current_descriptor or device is None or inode is None:
            raise StoreAuthorityError("descriptor scan output is malformed")
        identities.add((device, inode))
        current_descriptor = False
        device = None
        inode = None

    for raw_line in output.splitlines():
        if not raw_line:
            continue
        field, value = raw_line[0], raw_line[1:]
        if field == "p":
            if current_descriptor:
                finish_descriptor()
            if not value.isdigit():
                raise StoreAuthorityError("descriptor scan output is malformed")
            saw_process = True
        elif field == "f":
            if not saw_process or not value.isdigit():
                raise StoreAuthorityError("descriptor scan output is malformed")
            if current_descriptor:
                finish_descriptor()
            current_descriptor = True
        elif field == "D":
            if not current_descriptor or device is not None:
                raise StoreAuthorityError("descriptor scan output is malformed")
            device = _parse_lsof_integer(value)
        elif field == "i":
            if not current_descriptor or inode is not None:
                raise StoreAuthorityError("descriptor scan output is malformed")
            inode = _parse_lsof_integer(value)
        else:
            raise StoreAuthorityError("descriptor scan output is malformed")

    if current_descriptor:
        finish_descriptor()
    if not saw_process:
        raise StoreAuthorityError("descriptor scan output is malformed")
    return identities


def _parse_lsof_integer(value: str) -> int:
    try:
        parsed = int(value, 0)
    except ValueError as exc:
        raise StoreAuthorityError("descriptor scan output is malformed") from exc
    if parsed < 0:
        raise StoreAuthorityError("descriptor scan output is malformed")
    return parsed
