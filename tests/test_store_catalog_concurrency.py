"""Shared connections preserve statement parameters and cursor results."""

from __future__ import annotations

import gc
import json
import sqlite3
import threading
import time
import weakref
from concurrent.futures import ThreadPoolExecutor

import pytest

from pinky_daemon.agent_registry import AgentRegistry
from pinky_daemon.store_catalog import StoreCatalog, open_store_connection


@pytest.mark.parametrize("managed", [False, True], ids=["standalone", "catalog"])
@pytest.mark.parametrize("registry_reads", [False, True], ids=["connection", "registry"])
def test_shared_connection_preserves_cached_read_results(tmp_path, managed, registry_reads):
    catalog = StoreCatalog(expected_root=tmp_path) if managed else None
    registry = (
        AgentRegistry(str(tmp_path / "shared.db"), catalog=catalog) if registry_reads else None
    )
    connection = (
        registry._db
        if registry
        else open_store_connection(
            catalog,
            "agents",
            tmp_path / "shared.db",
            owner="test",
            check_same_thread=False,
        )
    )
    try:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS system_settings (key TEXT PRIMARY KEY, value TEXT)"
        )
        connection.executemany(
            "INSERT OR REPLACE INTO system_settings VALUES (?, ?)",
            [(f"read-{index}", f"value-{index}") for index in range(32)],
        )
        connection.commit()
        workers = 16
        barrier = threading.Barrier(workers)
        stop = threading.Event()
        started = time.monotonic()

        def read(worker):
            errors, mismatches, completed = [], [], 0
            barrier.wait(timeout=10)
            for iteration in range(500):
                if stop.is_set():
                    break
                index = (worker + iteration) % 32
                key, expected = f"read-{index}", f"value-{index}"
                phase = "execute"
                try:
                    if registry_reads and worker % 2 == 0:
                        phase = "registry.execute/fetchone"
                        result = registry.get_setting(key)
                    else:
                        cursor = connection.execute(
                            "SELECT value FROM system_settings WHERE key=?",
                            (key,),
                        )
                        phase = "fetchone" if worker % 2 == 0 else "fetchall"
                        if worker % 2 == 0:
                            row = cursor.fetchone()
                            result = row[0] if row is not None else None
                        else:
                            rows = cursor.fetchall()
                            result = rows[0][0] if len(rows) == 1 else rows
                        del cursor
                    if result != expected and len(mismatches) < 8:
                        mismatches.append((worker, index, repr(result)))
                except sqlite3.Error as error:
                    errors.append(
                        (
                            worker,
                            phase,
                            type(error).__name__,
                            str(error),
                            getattr(error, "sqlite_errorcode", None),
                        )
                    )
                    stop.set()
                completed += 1
            return completed, errors, mismatches

        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(read, range(workers)))
        errors = [error for _count, failures, _mismatches in results for error in failures]
        mismatches = [row for _count, _errors, rows in results for row in rows]
        print(
            json.dumps(
                {
                    "connection": type(connection).__name__,
                    "workers": workers,
                    "completed": sum(count for count, _errors, _rows in results),
                    "seconds": time.monotonic() - started,
                    "sqlite": sqlite3.sqlite_version,
                    "sqlite_error_count": len(errors),
                    "errors": errors,
                    "row_mismatch_count": len(mismatches),
                    "mismatches": mismatches,
                }
            )
        )
        assert not errors, f"shared connection SQLite failures: {errors}"
        assert not mismatches, f"shared connection returned incorrect rows: {mismatches}"
    finally:
        if registry is not None:
            registry.close()
        else:
            connection.close()


@pytest.mark.parametrize("managed", [False, True], ids=["standalone", "catalog"])
@pytest.mark.parametrize("closed", [False, True], ids=["open", "closed"])
@pytest.mark.parametrize("cycle", [False, True], ids=["last-reference", "cyclic-gc"])
@pytest.mark.parametrize("bound_method", [False, True], ids=["cursor", "bound-method"])
def test_shared_results_drop_without_native_sql_on_releasing_thread(
    tmp_path, managed, closed, cycle, bound_method
):
    from pinky_identity.live_sqlite import is_live_sqlite_file

    path = tmp_path / "gc.db"
    connection = open_store_connection(
        StoreCatalog(expected_root=tmp_path) if managed else None,
        "agents",
        path,
        owner="test",
        check_same_thread=False,
    )
    connection.executescript(
        "CREATE TABLE entries(value); INSERT INTO entries VALUES (1), (2), (3);"
    )
    cursor = connection.execute("SELECT value FROM entries")
    assert cursor.fetchone() == (1,)
    if cycle:
        cursor.cycle = cursor
    reference = weakref.ref(cursor)
    holder = [cursor.fetchone if bound_method else cursor]
    del cursor
    if closed:
        connection.close()
    observer = sqlite3.connect(path, timeout=0)
    finished = threading.Event()
    release_errors = []

    def release():
        try:
            holder.clear()
            if cycle:
                gc.collect()
        except BaseException as error:
            release_errors.append(error)
        finally:
            finished.set()

    worker = threading.Thread(target=release)
    try:
        # Even before Python results are released, no native statement remains
        # to hold a read lock or outlive the connection.
        observer.execute("BEGIN EXCLUSIVE")
        observer.rollback()
        with connection._sqlite_lock:
            worker.start()
            assert finished.wait(5), "dropping materialized results must not need SQL ownership"
            assert reference() is None
            observer.execute("BEGIN EXCLUSIVE")
            observer.rollback()
            if managed:
                assert is_live_sqlite_file(path) is not closed
        worker.join(5)
        assert not worker.is_alive()
        assert not release_errors
    finally:
        worker.join(5)
        observer.close()
        connection.close()


def test_materialized_cursor_interface_and_native_row_factory(tmp_path):
    connection = open_store_connection(
        StoreCatalog(expected_root=tmp_path),
        "agents",
        tmp_path / "rows.db",
        owner="test",
    )
    try:
        connection.executescript("CREATE TABLE entries (id INTEGER PRIMARY KEY, value TEXT);")
        cursor = connection.cursor()
        assert (
            cursor.executemany("INSERT INTO entries(value) VALUES (?)", [("a",), ("b",)]) is cursor
        )
        assert cursor.rowcount == 2
        assert cursor.execute("INSERT INTO entries(value) VALUES (?)", ("c",)) is cursor
        assert cursor.lastrowid == 3
        connection.commit()
        cursor.arraysize = 2
        assert cursor.connection is connection
        assert cursor.execute("SELECT value FROM entries ORDER BY id") is cursor
        assert cursor.description[0][0] == "value"
        assert cursor.fetchmany() == [("a",), ("b",)]
        assert list(cursor) == [("c",)]
        connection.row_factory = sqlite3.Row
        row = connection.execute("SELECT value FROM entries WHERE id=1").fetchone()
        assert row["value"] == "a"

        def factory(public, row):
            return public, row

        cursor.row_factory = factory
        assert cursor.row_factory is factory
        native, values = cursor.execute("SELECT 7").fetchone()
        assert isinstance(native, sqlite3.Cursor)
        assert values == (7,)
        with pytest.raises(sqlite3.ProgrammingError, match="closed cursor"):
            native.fetchone()
        cursor.row_factory = None
        assert cursor.execute("SELECT 8").fetchall() == [(8,)]
        cursor.close()
        assert cursor.execute("SELECT 9").fetchone() == (9,)
    finally:
        connection.close()


@pytest.mark.parametrize("seam", [False, True], ids=["factory", "store-seam"])
def test_shared_factory_alone_does_not_register_live_paths(tmp_path, seam):
    from pinky_identity.live_sqlite import LiveSQLiteConnection, live_sqlite_identities

    before = live_sqlite_identities()
    path = tmp_path / "untracked.db"
    connection = (
        open_store_connection(None, "agents", path, owner="test", check_same_thread=False)
        if seam
        else sqlite3.connect(path, check_same_thread=False, factory=LiveSQLiteConnection)
    )
    try:
        cursor = connection.execute("SELECT 1")
        assert live_sqlite_identities() == before
        assert cursor.fetchone() == (1,)
    finally:
        connection.close()
    assert live_sqlite_identities() == before


def test_same_thread_connections_keep_native_cursor_and_inode_lifetime(tmp_path):
    from pinky_identity.fs_security import harden_secret_file
    from pinky_identity.live_sqlite import (
        LiveSQLiteConnection,
        LiveSQLiteFileError,
        is_live_sqlite_file,
        track_sqlite_connection,
    )

    ordinary = open_store_connection(None, "agents", tmp_path / "ordinary.db", owner="test")
    try:
        assert type(ordinary) is sqlite3.Connection
    finally:
        ordinary.close()
    path = tmp_path / "tracked.db"
    connection = sqlite3.connect(path, factory=LiveSQLiteConnection)
    track_sqlite_connection(connection, path)
    connection.executescript("CREATE TABLE entries(value); INSERT INTO entries VALUES (1), (2);")
    cursor = connection.execute("SELECT value FROM entries")
    assert isinstance(cursor, sqlite3.Cursor)
    assert connection._sqlite_lock is None
    assert cursor.fetchone() == (1,)
    connection.close()
    assert is_live_sqlite_file(path)
    with pytest.raises(LiveSQLiteFileError):
        harden_secret_file(path)
    del cursor
    gc.collect()
    assert not is_live_sqlite_file(path)
    harden_secret_file(path)


def test_shared_backup_locks_both_handles_and_auxiliary_operations(tmp_path):
    from pinky_identity.live_sqlite import LiveSQLiteConnection

    source = sqlite3.connect(
        tmp_path / "source.db", factory=LiveSQLiteConnection, check_same_thread=False
    )
    target = sqlite3.connect(
        tmp_path / "target.db", factory=LiveSQLiteConnection, check_same_thread=False
    )
    try:
        source.create_function("twice", 1, lambda value: value * 2)
        source.executescript("CREATE TABLE entries(value); INSERT INTO entries VALUES (4);")
        assert source.execute("SELECT twice(value) FROM entries").fetchone() == (8,)
        progress = []

        def observe(*status):
            assert source._sqlite_lock._is_owned()
            assert target._sqlite_lock._is_owned()
            progress.append(status)

        source.backup(target, pages=1, progress=observe)
        assert progress
        assert target.execute("SELECT value FROM entries").fetchall() == [(4,)]
        assert any("CREATE TABLE entries" in line for line in source.iterdump())
        with pytest.raises(sqlite3.NotSupportedError, match="same-thread"):
            source.blobopen("entries", "value", 1)
    finally:
        target.close()
        source.close()
