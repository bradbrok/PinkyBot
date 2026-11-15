"""Catalog finalizers serialize a shared file without blocking other files."""

from __future__ import annotations

import sqlite3
import threading
import time
from types import SimpleNamespace

import pytest

from pinky_daemon import store_catalog, store_shutdown
from tests._gc_quiet import gc_quiet


def _catalog(tmp_path, *, same_path=True, alias=False):
    shared = tmp_path / "shared.db"
    paths = {
        "waiting": shared,
        "blocked": tmp_path / ("alias.db" if alias else "other.db"),
    }
    if same_path and not alias:
        paths["blocked"] = shared
    manifest = {
        name: store_catalog.StoreIntegrityTarget(
            logical_name=name, path=str(path), criticality="telemetry", journal_mode="wal",
        )
        for name, path in paths.items()
    }
    catalog = store_catalog.StoreCatalog(
        expected_root=tmp_path, silence_allowlist={}, manifest=manifest,
    )
    connections = {}
    for name, path in paths.items():
        if alias and name == "blocked":
            path.symlink_to(shared)
        connection = catalog.open_connection(name, path, owner="shared-owner")
        mode = connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        connection.execute("CREATE TABLE IF NOT EXISTS payload (value TEXT)")
        connection.execute("INSERT INTO payload VALUES ('saved')")
        connection.commit()
        catalog.register(name, path, journal_mode=mode, owner="shared-owner")
        connections[name] = connection
    return catalog, connections, shared


def _workers(monkeypatch):
    workers = {}
    started = {name: threading.Event() for name in ("blocked", "waiting")}

    class ObservedThread(threading.Thread):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.key = self.name.removeprefix("store-shutdown-")
            workers[self.key] = self

        def run(self):
            started[self.key].set()
            super().run()

    monkeypatch.setattr(store_shutdown, "threading", SimpleNamespace(Thread=ObservedThread))
    return workers, started


def _join_and_close(workers, connections):
    for worker in workers.values():
        worker.join(3)
    assert all(not worker.is_alive() for worker in workers.values())
    for connection in connections.values():
        if not connection._store_closed:
            connection.close()


@pytest.mark.parametrize("alias", [False, True])
def test_same_path_checkpoint_overlap_is_serialized(tmp_path, monkeypatch, alias):
    catalog, connections, path = _catalog(tmp_path, alias=alias)
    workers, _ = _workers(monkeypatch)
    reader = sqlite3.connect(path, check_same_thread=False)
    reader.execute("BEGIN")
    reader.execute("SELECT * FROM payload").fetchall()
    connections["blocked"].execute("INSERT INTO payload VALUES ('newer')")
    connections["blocked"].commit()
    checkpoint_started = threading.Event()
    contender_seen = threading.Event()
    waiting_on_path_lock = threading.Event()
    release_reader = threading.Event()
    reader_lock = threading.Lock()
    checkpoint_rows = {}
    execute = store_catalog._ManagedSQLiteConnection.execute

    def unblock_reader(*, timeout=-1):
        if not reader_lock.acquire(timeout=timeout):
            return False
        try:
            if not release_reader.is_set():
                reader.rollback()
                release_reader.set()
        finally:
            reader_lock.release()
        return True

    class ObservedPathLock:
        def __init__(self):
            self.lock = threading.Lock()

        def __enter__(self):
            if threading.current_thread() is workers.get("waiting"):
                assert checkpoint_started.wait(3)
            if not self.lock.acquire(blocking=False):
                waiting_on_path_lock.set()
                contender_seen.set()
                # A failed acquire proves the waiter cannot reach its checkpoint.
                unblock_reader()
                self.lock.acquire()

        def __exit__(self, *exc):
            self.lock.release()

    monkeypatch.setattr(
        catalog._connection_authority, "_finalization_locks",
        {str(path.resolve()): ObservedPathLock()}, raising=False,
    )

    class CheckpointCursor:
        def __init__(self, cursor, name):
            self.cursor, self.name = cursor, name

        def fetchone(self):
            row = self.cursor.fetchone()
            checkpoint_rows[self.name] = row
            if row[0]:
                # Without serialization, release only after a real SQLite collision.
                unblock_reader()
            return row

    def traced_execute(connection, sql, parameters=()):
        if "wal_checkpoint" not in sql.lower():
            return execute(connection, sql, parameters)
        name = "blocked" if connection is connections["blocked"] else "waiting"
        if name == "blocked":
            checkpoint_started.set()
            assert contender_seen.wait(3)
        elif not waiting_on_path_lock.is_set():
            assert checkpoint_started.wait(3)
            contender_seen.set()
        return CheckpointCursor(execute(connection, sql, parameters), name)

    monkeypatch.setattr(store_catalog._ManagedSQLiteConnection, "execute", traced_execute)
    try:
        with gc_quiet():
            report = catalog.shutdown(deadline_seconds=4)
    finally:
        reader_unblocked = unblock_reader(timeout=3)
        join_error = None
        try:
            _join_and_close(workers, connections)
        except AssertionError as exc:
            join_error = exc
        if not reader_unblocked:
            pytest.fail("READER_CLEANUP_LOCK_TIMEOUT: caller reader_lock acquire exceeded 3 seconds")
        reader.close()
        if join_error is not None:
            raise join_error
    assert waiting_on_path_lock.is_set()
    assert report.ok and report.finalized == ("blocked", "waiting")
    assert set(checkpoint_rows) == {"blocked", "waiting"}
    assert all(row[0] == 0 for row in checkpoint_rows.values())
    assert all(connection._store_closed for connection in connections.values())


def test_same_path_lock_wait_preserves_the_aggregate_deadline(tmp_path, monkeypatch):
    catalog, connections, _ = _catalog(tmp_path)
    workers, started = _workers(monkeypatch)
    holding, release, waiting_entered = threading.Event(), threading.Event(), threading.Event()
    commit = store_catalog._ManagedSQLiteConnection.commit

    def blocked_commit(connection):
        if connection is connections["blocked"]:
            holding.set()
            release.wait()
        else:
            waiting_entered.set()
        return commit(connection)

    monkeypatch.setattr(store_catalog._ManagedSQLiteConnection, "commit", blocked_commit)
    try:
        with gc_quiet():
            begin = time.monotonic()
            with pytest.raises(store_shutdown.StoreShutdownError) as caught:
                catalog.shutdown(deadline_seconds=0.9)
            elapsed = time.monotonic() - begin
            assert holding.is_set() and started["waiting"].is_set()
            assert elapsed < 1.5
            assert not waiting_entered.is_set()
            assert not connections["waiting"]._store_closed
            assert caught.value.report.attempted == ("blocked", "waiting")
            assert [(failure.logical_name, failure.reason) for failure in caught.value.report.failures] == [
                (name, "aggregate deadline share exceeded; store never finalized")
                for name in ("blocked", "waiting")
            ]
    finally:
        release.set()
        _join_and_close(workers, connections)
    assert all(connection._store_closed for connection in connections.values())


def test_different_paths_finalize_while_another_path_is_blocked(tmp_path, monkeypatch):
    catalog, connections, _ = _catalog(tmp_path, same_path=False)
    workers, _ = _workers(monkeypatch)
    holding, release, overlap = threading.Event(), threading.Event(), threading.Event()
    commit = store_catalog._ManagedSQLiteConnection.commit

    def blocked_commit(connection):
        if connection is connections["blocked"]:
            holding.set()
            release.wait()
        else:
            if holding.is_set() and not release.is_set():
                overlap.set()
            release.set()
        return commit(connection)

    monkeypatch.setattr(store_catalog._ManagedSQLiteConnection, "commit", blocked_commit)
    try:
        with gc_quiet():
            report = catalog.shutdown(deadline_seconds=1.8)
        assert overlap.is_set()
        assert report.ok and report.finalized == ("blocked", "waiting")
    finally:
        release.set()
        _join_and_close(workers, connections)
    assert all(connection._store_closed for connection in connections.values())
