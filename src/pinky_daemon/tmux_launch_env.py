"""Private JSON staging, nonce cancellation and isolated environment loading.

This module is stdlib-only so the same source runs in the target namespace.
Secret values enter on stdin or through a private file, never process argv.
"""
from __future__ import annotations

import fcntl
import json
import math
import os
import re
import stat
import sys
import time
from contextlib import contextmanager
from pathlib import Path

from pinky_daemon.tmux_launch_env_loader import (
    _NONCE,
    _private_regular,
    key_policy,
    validate_env,
)
from pinky_daemon.tmux_launch_env_loader import (
    is_valid_key_name as is_valid_key_name,
)
from pinky_daemon.tmux_launch_env_loader import (
    load_env as load_env,
)

_SCOPE = re.compile(r"[0-9a-f]{64}", re.ASCII)
PUBLICATION_TIMEOUT = 60.0
_METADATA_TTL = 24 * 60 * 60
_SECRET_TTL = 10 * 60


def ambient_env(items, warn) -> dict[str, str]:
    """Filter inherited shell state; diagnostics contain only bounded key names."""
    result = {}
    for key, value in items:
        if key in {"TMUX", "TMUX_PANE"}:
            continue
        policy = key_policy(key)
        if policy in {"invalid", "shell"}:
            warn(f"WARNING dropping {policy} env name {repr(key)[:64]}")
            continue
        try:
            value.encode("utf-8")
        except UnicodeError:
            warn(f"WARNING dropping non-UTF-8 env value for {repr(key)[:64]}")
            continue
        if "\n" in value or "\r" in value:
            warn(f"dropping multiline env {repr(key)[:64]} (excluded from launch environment)")
            continue
        # Reserved names stay present for a loud strict-boundary refusal.
        result[key] = value
    return result


def _identity(scope: str, nonce: str) -> None:
    if not isinstance(scope, str) or _SCOPE.fullmatch(scope) is None:
        raise ValueError("invalid launch environment scope")
    if not isinstance(nonce, str) or _NONCE.fullmatch(nonce) is None:
        raise ValueError("invalid launch environment nonce")


def _deadline(deadline: float, *, initial: bool = False) -> None:
    now = time.time()
    if (
        not isinstance(deadline, (int, float)) or isinstance(deadline, bool)
        or not math.isfinite(deadline) or deadline <= now
        or (initial and deadline > now + PUBLICATION_TIMEOUT)
    ):
        raise ValueError("invalid or expired launch publication deadline")


def _directory(parent_fd: int, name: str, *, private: bool, create: bool) -> int:
    if create:
        try:
            os.mkdir(name, mode=0o700, dir_fd=parent_fd)
        except FileExistsError:
            pass
    fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
    try:
        info = os.fstat(fd)
        denied = 0o077 if private else 0o022
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & denied:
            raise PermissionError("unsafe launch environment directory")
    except BaseException:
        os.close(fd)
        raise
    return fd


@contextmanager
def _scope_directory(scope: str, *, create: bool = True):
    home = Path.home()
    parts = [".local", "state", "pinkybot", "tmux-launch-env", scope]
    fds = []
    try:
        fd = os.open(home, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        fds.append(fd)
        info = os.fstat(fd)
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o022:
            raise PermissionError("unsafe launch environment home")
        for index, part in enumerate(parts):
            fd = _directory(fd, part, private=index >= 3, create=create)
            fds.append(fd)
        yield home.joinpath(*parts), fd
    finally:
        for opened in reversed(fds):
            os.close(opened)


@contextmanager
def _nonce_lock(directory: int, nonce: str, *, deadline: float | None = None):
    if deadline is not None:
        _deadline(deadline)
    name = f"env-{nonce}.lock"
    fd = os.open(
        name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=directory,
    )
    try:
        if not _private_regular(os.fstat(fd)):
            raise PermissionError("unsafe launch environment lock")
        fcntl.flock(fd, fcntl.LOCK_EX)
        # GC may have removed the inode while a waiter retained an open fd.
        # Neither an expired lease nor an unlinked inode authorizes publication.
        if deadline is not None:
            _deadline(deadline)
        current = os.stat(name, dir_fd=directory, follow_symlinks=False)
        opened = os.fstat(fd)
        if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
            raise RuntimeError("launch environment lock was replaced")
        yield fd
    finally:
        os.close(fd)


def _unlink_own(directory: int, name: str) -> None:
    try:
        info = os.stat(name, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        return
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
        raise PermissionError("unsafe launch environment file")
    try:
        os.unlink(name, dir_fd=directory)
    except FileNotFoundError:
        pass


def _sweep(directory: int) -> None:
    """Bound crash leftovers without removing active publication locks."""
    now = time.time()
    for name in os.listdir(directory):
        match = re.fullmatch(r"env-([0-9a-f]{32})\.(json|lock)", name)
        if match is None:
            continue
        try:
            info = os.stat(name, dir_fd=directory, follow_symlinks=False)
            ttl = _SECRET_TTL if match[2] == "json" else _METADATA_TTL
            if not _private_regular(info) or now - info.st_mtime <= ttl:
                continue
            lock_name = f"env-{match[1]}.lock"
            try:
                lock_fd = os.open(
                    lock_name, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory,
                )
            except FileNotFoundError:
                if match[2] == "json":
                    _unlink_own(directory, name)
                continue
            try:
                if not _private_regular(os.fstat(lock_fd)):
                    continue
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    continue
                current = os.stat(name, dir_fd=directory, follow_symlinks=False)
                if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
                    continue
                if now - current.st_mtime > ttl:
                    _unlink_own(directory, name)
            finally:
                os.close(lock_fd)
        except FileNotFoundError:
            continue


def stage_env(env: dict[str, str], scope: str, nonce: str, *, deadline: float) -> dict | None:
    validate_env(env)
    _identity(scope, nonce)
    _deadline(deadline, initial=True)
    populated = {key: value for key, value in env.items() if value != ""}
    if not populated:
        try:
            with _scope_directory(scope, create=False) as (_, directory):
                _sweep(directory)
        except FileNotFoundError:
            pass
        return None
    with _scope_directory(scope) as (path, directory):
        _sweep(directory)
        with _nonce_lock(directory, nonce, deadline=deadline) as lock_fd:
            if os.read(lock_fd, 1):
                raise RuntimeError("launch environment publication cancelled")
            _deadline(deadline)
            name = f"env-{nonce}.json"
            created = False
            try:
                output_fd = os.open(
                    name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600, dir_fd=directory,
                )
                created = True
                try:
                    output = os.fdopen(output_fd, "w", encoding="utf-8", newline="\n")
                except BaseException:
                    os.close(output_fd)
                    raise
                with output:
                    json.dump({"nonce": nonce, "env": populated}, output)
                # A descheduled publisher can cross its lease during the write.
                # Keep the lock until completion or removal of its secret data.
                _deadline(deadline)
                return {"path": str(path / name)}
            except BaseException:
                if created:
                    _unlink_own(directory, name)
                raise


def cancel_env(scope: str, nonce: str) -> None:
    _identity(scope, nonce)
    with _scope_directory(scope) as (_, directory):
        with _nonce_lock(directory, nonce) as lock_fd:
            os.lseek(lock_fd, 0, os.SEEK_SET)
            os.write(lock_fd, b"cancelled\n")
            os.ftruncate(lock_fd, 10)
            _unlink_own(directory, f"env-{nonce}.json")


def main() -> None:
    try:
        request = json.load(sys.stdin)
        action = request.get("action", "stage")
        if action == "stage":
            result = stage_env(request["env"], request["scope"], request["nonce"],
                               deadline=request["deadline"])
        elif action == "cancel":
            cancel_env(request["scope"], request["nonce"])
            result = None
        else:
            raise ValueError("invalid launch operation")
    except Exception:
        print("launch environment staging failed", file=sys.stderr)
        raise SystemExit(1) from None
    print(json.dumps(result))


if __name__ == "__main__":
    main()
