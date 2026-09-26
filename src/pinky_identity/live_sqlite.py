"""Process-local inode guard for non-SQLite file operations.

Closing any ordinary descriptor for an inode releases all of this process's
POSIX locks on it, even when SQLite owns other open descriptors for that file.
Registration and inspection use stat only; they never open a database.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import weakref
from pathlib import Path

_SUFFIXES = ("", "-wal", "-shm", "-journal")
_lock = threading.RLock()
_owners: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


class LiveSQLiteFileError(RuntimeError):
    """A raw file operation would endanger a live SQLite connection's locks."""


def _identity(path: str | Path) -> tuple[int, int] | None:
    try:
        # Metadata-only guard: never reads contents of caller-selected files.
        info = os.stat(path)
    except FileNotFoundError:
        return None
    return info.st_dev, info.st_ino


def register_live_sqlite(owner: object, path: str | Path) -> None:
    """Track a connection owner until explicit close or garbage collection."""
    path = os.path.realpath(path)
    identity = _identity(path)
    if identity is not None:
        with _lock:
            _owners.setdefault(owner, {})[path] = {identity}


def unregister_live_sqlite(owner: object) -> None:
    with _lock:
        _owners.pop(owner, None)


def _refresh() -> set[tuple[int, int]]:
    identities = set()
    seen = set()
    for paths in list(_owners.values()):
        if id(paths) in seen:
            continue
        seen.add(id(paths))
        for path, known in paths.items():
            # Sidecars may be created lazily after the connection is registered.
            # Retain old identities too: an unlinked file can still be mapped.
            if _identity(path) in known:
                for suffix in _SUFFIXES[1:]:
                    identity = _identity(path + suffix)
                    if identity is not None:
                        known.add(identity)
            identities.update(known)
    return identities


def is_live_sqlite_file(path: str | Path) -> bool:
    with _lock:
        identity = _identity(path)
        return identity is not None and identity in _refresh()


def refuse_live_sqlite_file(path: str | Path) -> None:
    if is_live_sqlite_file(path):
        raise LiveSQLiteFileError(f"Refusing raw file access to a live SQLite store: {path}")


def refuse_sqlite_attachment(path: str | Path) -> None:
    """Reject database names and live aliases without opening the attachment."""
    resolved = os.path.realpath(path)
    if resolved.lower().endswith(tuple(".db" + suffix for suffix in _SUFFIXES)):
        raise LiveSQLiteFileError("SQLite databases and sidecars cannot be sent as attachments")
    refuse_live_sqlite_file(resolved)


def track_sqlite_connection(connection: LiveSQLiteConnection, path: str | Path) -> None:
    connection._live_sqlite_path = os.fspath(path)
    register_live_sqlite(connection, path)


class LiveSQLiteCursor(sqlite3.Cursor):
    """Keep inode protection while a statement can outlive connection.close()."""

    def close(self) -> None:
        super().close()
        unregister_live_sqlite(self)


class LiveSQLiteConnection(sqlite3.Connection):
    """Track connection and cursor lifetimes, including SQLite zombie handles."""

    _live_sqlite_path: str | None = None

    def cursor(self, factory=LiveSQLiteCursor):
        cursor = super().cursor(factory)
        if self._live_sqlite_path is not None:
            with _lock:
                paths = _owners.get(self)
                if paths is not None:
                    _owners[cursor] = paths
        return cursor

    def execute(self, sql, parameters=(), /):
        return self.cursor().execute(sql, parameters)

    def executemany(self, sql, parameters, /):
        return self.cursor().executemany(sql, parameters)

    def executescript(self, script, /):
        return self.cursor().executescript(script)

    def close(self) -> None:
        super().close()
        # Any unfinished cursor remains registered until close or collection.
        unregister_live_sqlite(self)
