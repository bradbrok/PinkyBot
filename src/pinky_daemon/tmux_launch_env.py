"""Private JSON staging, nonce cancellation and isolated environment loading.

This module is stdlib-only so the same source runs in the target namespace.
Secret values enter on stdin or through a private file, never process argv.
The private state directory must be on a local filesystem: same-process
threads require flock exclusion, which NFS lock emulation may not provide.
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
    validate_inherit,
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
_CLOCK_SKEW = 300.0


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


def _request_deadline(deadline: float) -> None:
    now = time.time()
    if (
        not isinstance(deadline, (int, float)) or isinstance(deadline, bool)
        or not math.isfinite(deadline) or deadline <= now - _CLOCK_SKEW
        or deadline > now + PUBLICATION_TIMEOUT + _CLOCK_SKEW
    ):
        raise ValueError("invalid or stale launch request deadline")


def _deadline(deadline: float | None) -> None:
    if deadline is not None and time.monotonic() >= deadline:
        raise ValueError("expired launch publication lease")


def _directory(
    parent_fd: int, name: str, *, private: bool, create: bool, deadline: float | None = None,
) -> int:
    _deadline(deadline)
    if create:
        try:
            os.mkdir(name, mode=0o700, dir_fd=parent_fd)
        except FileExistsError:
            pass
    _deadline(deadline)
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
def _state_directory(*, create: bool, deadline: float | None = None):
    home = Path.home()
    parts = [".local", "state", "pinkybot", "tmux-launch-env"]
    fds = []
    try:
        _deadline(deadline)
        fd = os.open(home, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        fds.append(fd)
        info = os.fstat(fd)
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o022:
            raise PermissionError("unsafe launch environment home")
        for index, part in enumerate(parts):
            fd = _directory(fd, part, private=index >= 3, create=create, deadline=deadline)
            fds.append(fd)
        yield home.joinpath(*parts), fd
    finally:
        for opened in reversed(fds):
            os.close(opened)


@contextmanager
def _scope_directory(scope: str, *, create: bool = True):
    with _state_directory(create=create) as (root, parent):
        fd = _directory(parent, scope, private=True, create=create)
        try:
            yield root / scope, fd
        finally:
            os.close(fd)


@contextmanager
def _nonce_lock(directory: int, nonce: str, *, deadline: float | None = None):
    if deadline is not None:
        _deadline(deadline)
    name = f"env-{nonce}.lock"
    while True:
        if deadline is not None:
            _deadline(deadline)
        fd = os.open(
            name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600,
            dir_fd=directory,
        )
        try:
            if not _private_regular(os.fstat(fd)):
                raise PermissionError("unsafe launch environment lock")
            fcntl.flock(fd, fcntl.LOCK_EX)
            # GC can remove an inode while a waiter retains its descriptor.
            if deadline is not None:
                _deadline(deadline)
            if not _lock_is_current(directory, name, fd):
                continue
            yield fd
            return
        finally:
            os.close(fd)


def _lock_is_current(directory: int, name: str, fd: int) -> bool:
    try:
        current = os.stat(name, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        return False
    opened = os.fstat(fd)
    return (current.st_dev, current.st_ino) == (opened.st_dev, opened.st_ino)


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


def _sweep(directory: int, *, deadline: float | None = None) -> None:
    """Bound crash leftovers without removing active publication locks."""
    now = time.time()
    for name in os.listdir(directory):
        _deadline(deadline)
        match = re.fullmatch(r"env-([0-9a-f]{32})\.(json|lock)", name)
        if match is None:
            continue
        try:
            info = os.stat(name, dir_fd=directory, follow_symlinks=False)
            ttl = _SECRET_TTL if match[2] == "json" else _METADATA_TTL
            if not _private_regular(info) or now - info.st_mtime <= ttl:
                continue
            lock_name = f"env-{match[1]}.lock"
            while True:
                _deadline(deadline)
                try:
                    lock_fd = os.open(
                        lock_name, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK,
                        dir_fd=directory,
                    )
                except FileNotFoundError:
                    if match[2] == "json":
                        _unlink_own(directory, name)
                    break
                try:
                    if not _private_regular(os.fstat(lock_fd)):
                        break
                    try:
                        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        break
                    _deadline(deadline)
                    if not _lock_is_current(directory, lock_name, lock_fd):
                        continue
                    current = os.stat(name, dir_fd=directory, follow_symlinks=False)
                    if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
                        break
                    if now - current.st_mtime > ttl:
                        _unlink_own(directory, name)
                    break
                finally:
                    os.close(lock_fd)
        except FileNotFoundError:
            continue


def _sweep_scopes(directory: int, *, deadline: float) -> None:
    """Collect abandoned launches even when their original scope never returns."""
    for scope in os.listdir(directory):
        _deadline(deadline)
        if _SCOPE.fullmatch(scope) is None:
            continue
        try:
            fd = _directory(directory, scope, private=True, create=False, deadline=deadline)
        except OSError:
            # Foreign, permissive, missing or symlinked siblings grant no access.
            continue
        try:
            _sweep(fd, deadline=deadline)
        finally:
            os.close(fd)


def stage_env(
    env: dict[str, str], scope: str, nonce: str, *, deadline: float,
    inherit: str = "all", granted: tuple[str, ...] = (),
) -> dict | None:
    lease = time.monotonic() + PUBLICATION_TIMEOUT
    _request_deadline(deadline)
    validate_env(env)
    validate_inherit(env, inherit)
    if (
        not isinstance(granted, (list, tuple))
        or any(not isinstance(k, str) or k not in env for k in granted)
        or (inherit != "none" and granted)
    ):
        raise ValueError("invalid launch grant names")
    _identity(scope, nonce)
    populated = dict(env) if inherit == "none" else {k: v for k, v in env.items() if v != ""}
    needs_payload = bool(populated) or inherit == "none"
    try:
        with _state_directory(create=needs_payload, deadline=lease) as (root, parent):
            _sweep_scopes(parent, deadline=lease)
            if not needs_payload:
                return None
            directory = _directory(parent, scope, private=True, create=True, deadline=lease)
            try:
                policy = {"inherit": "none", "granted": list(granted)} if inherit == "none" else {}
                return _publish(populated, root / scope, directory, nonce, lease, policy=policy)
            finally:
                os.close(directory)
    except FileNotFoundError:
        if not needs_payload:
            return None
        raise


def _publish(
    populated: dict[str, str], path: Path, directory: int, nonce: str, lease: float,
    *, policy: dict | None = None,
):
    with _nonce_lock(directory, nonce, deadline=lease) as lock_fd:
        if os.read(lock_fd, 1):
            raise RuntimeError("launch environment publication cancelled")
        _deadline(lease)
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
                json.dump({"nonce": nonce, "env": populated, **(policy or {})}, output)
            # A descheduled publisher can cross its lease during the write.
            # Keep the lock until completion or removal of its secret data.
            _deadline(lease)
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
                               deadline=request["deadline"], inherit=request.get("inherit", "all"),
                               granted=tuple(request.get("granted", ())))
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
