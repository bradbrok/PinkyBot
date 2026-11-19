"""Process-local inode guard for non-SQLite file operations.

Closing any ordinary descriptor for an inode releases all of this process's
POSIX locks on it, even when SQLite owns other open descriptors for that file.
Registration and inspection use stat only; they never open a database.
"""

from __future__ import annotations

import operator
import os
import sqlite3
import threading
import weakref
from contextlib import ExitStack
from functools import wraps
from itertools import islice
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


def live_sqlite_identities() -> set[tuple[int, int]]:
    """Snapshot registered database and sidecar identities without opening them."""
    with _lock:
        return _refresh().copy()


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


def serialized_sqlite_call(method):
    """Serialize operations only on connections that permit cross-thread use."""

    @wraps(method)
    def locked(self, *args, **kwargs):
        if self._sqlite_lock is None:
            return method(self, *args, **kwargs)
        with self._sqlite_lock:
            return method(self, *args, **kwargs)

    return locked


_USE_ARRAYSIZE = object()


class MaterializedSQLiteCursor:
    """DB-API results with no open native cursor or statement after execution."""

    def __init__(self, connection, factory=sqlite3.Cursor):
        self.connection = connection
        self._factory = sqlite3.Cursor if factory is LiveSQLiteCursor else factory
        self.row_factory = connection.row_factory
        self.arraysize = 1
        self.description = None
        self.lastrowid = None
        self.rowcount = -1
        self._rows = iter(())

    def _execute(self, operation, *args):
        with self.connection._sqlite_lock:
            self._rows = iter(())
            self.description = None
            self.rowcount = -1
            cursor = sqlite3.Connection.cursor(self.connection, self._factory)
            try:
                cursor.row_factory = self.row_factory
                getattr(cursor, operation)(*args)
                rows = cursor.fetchall()
                self.description = cursor.description
                self.lastrowid = cursor.lastrowid
                self.rowcount = cursor.rowcount
                self._rows = iter(rows)
            finally:
                cursor.close()
        return self

    def execute(self, sql, parameters=(), /):
        return self._execute("execute", sql, parameters)

    def executemany(self, sql, parameters, /):
        return self._execute("executemany", sql, parameters)

    def executescript(self, script, /):
        return self._execute("executescript", script)

    def fetchone(self):
        return next(self._rows, None)

    def fetchmany(self, size=_USE_ARRAYSIZE):
        size = operator.index(self.arraysize if size is _USE_ARRAYSIZE else size)
        return list(self._rows) if size <= 0 else list(islice(self._rows, size))

    def fetchall(self):
        return list(self._rows)

    def __iter__(self):
        return self

    def __next__(self):
        return next(self._rows)

    def close(self):
        # Execution already closed the native cursor under connection ownership.
        pass


class LiveSQLiteConnection(sqlite3.Connection):
    """Track live inodes; materialize cursors when cross-thread use is enabled."""

    _live_sqlite_path: str | None = None

    def __init__(self, *args, **kwargs):
        check_same_thread = kwargs.get("check_same_thread", args[4] if len(args) > 4 else True)
        self._sqlite_lock = None if check_same_thread else threading.RLock()
        super().__init__(*args, **kwargs)

    @serialized_sqlite_call
    def cursor(self, factory=LiveSQLiteCursor):
        if self._sqlite_lock is not None:
            return MaterializedSQLiteCursor(self, factory)
        cursor = super().cursor(factory)
        if self._live_sqlite_path is not None:
            with _lock:
                paths = _owners.get(self)
                if paths is not None:
                    _owners[cursor] = paths
        return cursor

    @serialized_sqlite_call
    def execute(self, sql, parameters=(), /):
        return self.cursor().execute(sql, parameters)

    @serialized_sqlite_call
    def executemany(self, sql, parameters, /):
        return self.cursor().executemany(sql, parameters)

    @serialized_sqlite_call
    def executescript(self, script, /):
        return self.cursor().executescript(script)

    commit = serialized_sqlite_call(sqlite3.Connection.commit)
    rollback = serialized_sqlite_call(sqlite3.Connection.rollback)
    __enter__ = serialized_sqlite_call(sqlite3.Connection.__enter__)
    __exit__ = serialized_sqlite_call(sqlite3.Connection.__exit__)
    create_function = serialized_sqlite_call(sqlite3.Connection.create_function)
    create_aggregate = serialized_sqlite_call(sqlite3.Connection.create_aggregate)
    create_collation = serialized_sqlite_call(sqlite3.Connection.create_collation)
    create_window_function = serialized_sqlite_call(sqlite3.Connection.create_window_function)
    set_authorizer = serialized_sqlite_call(sqlite3.Connection.set_authorizer)
    set_progress_handler = serialized_sqlite_call(sqlite3.Connection.set_progress_handler)
    set_trace_callback = serialized_sqlite_call(sqlite3.Connection.set_trace_callback)
    getlimit = serialized_sqlite_call(sqlite3.Connection.getlimit)
    setlimit = serialized_sqlite_call(sqlite3.Connection.setlimit)
    if hasattr(sqlite3.Connection, "serialize"):
        serialize = serialized_sqlite_call(sqlite3.Connection.serialize)
        deserialize = serialized_sqlite_call(sqlite3.Connection.deserialize)
    if hasattr(sqlite3.Connection, "enable_load_extension"):
        enable_load_extension = serialized_sqlite_call(sqlite3.Connection.enable_load_extension)
        load_extension = serialized_sqlite_call(sqlite3.Connection.load_extension)
    if hasattr(sqlite3.Connection, "setconfig"):
        getconfig = serialized_sqlite_call(sqlite3.Connection.getconfig)
        setconfig = serialized_sqlite_call(sqlite3.Connection.setconfig)

    def backup(self, target, **kwargs):
        # Both handles participate; stable ordering prevents opposite backups
        # from each taking their source lock and waiting for the other's target.
        locks = {lock for db in (self, target) if (lock := getattr(db, "_sqlite_lock", None))}
        with ExitStack() as stack:
            for lock in sorted(locks, key=id):
                stack.enter_context(lock)
            return super().backup(target, **kwargs)

    @serialized_sqlite_call
    def iterdump(self, *args, **kwargs):
        dump = super().iterdump(*args, **kwargs)
        return iter(tuple(dump)) if self._sqlite_lock is not None else dump

    @serialized_sqlite_call
    def blobopen(self, *args, **kwargs):
        if self._sqlite_lock is not None:
            raise sqlite3.NotSupportedError("incremental blobs require a same-thread connection")
        return super().blobopen(*args, **kwargs)

    # interrupt deliberately bypasses ownership: another thread must be able
    # to cancel a long-running operation while that operation holds the lock.

    @serialized_sqlite_call
    def close(self) -> None:
        super().close()
        # Only same-thread cursors can still own an unfinished native statement.
        unregister_live_sqlite(self)
