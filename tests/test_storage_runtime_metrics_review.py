"""Independent adversarial probes for the Lane B exact-head review."""

from __future__ import annotations

import logging
import os
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from pinky_daemon import store_snapshot as snapshot_module
from pinky_daemon.storage_observability import StorageObservability
from pinky_daemon.store_catalog import (
    BoundSQLiteFile,
    StoreCatalog,
    StoreCatalogError,
    StoreConnectionPolicy,
    StoreIntegrityTarget,
)
from pinky_daemon.store_snapshot import StoreSnapshotService


def _target(
    logical_name: str,
    path: Path,
    *,
    timeout_ms: int = 25,
) -> StoreIntegrityTarget:
    return StoreIntegrityTarget(
        logical_name=logical_name,
        path=os.fspath(path),
        criticality="memory",
        journal_mode="delete",
        connection_policy=StoreConnectionPolicy(busy_timeout_ms=timeout_ms),
    )


def _primary_error_code(error: sqlite3.Error) -> int | None:
    error_code = getattr(error, "sqlite_errorcode", None)
    return None if error_code is None else int(error_code) & 0xFF


class _PairClock:
    def __init__(self, durations_ms: list[int]) -> None:
        self._durations_ns = [duration * 1_000_000 for duration in durations_ms]
        self._now_ns = 0
        self._start = True

    def __call__(self) -> int:
        if self._start:
            self._start = False
            return self._now_ns
        if not self._durations_ns:
            raise AssertionError("review probe clock was read too many times")
        self._now_ns += self._durations_ns.pop(0)
        self._start = True
        return self._now_ns


def test_review_busy_commit_and_context_exit_are_counted_exactly(tmp_path: Path) -> None:
    path = tmp_path / "commit-contention.db"
    manifest = {"tasks": _target("tasks", path)}
    observability = StorageObservability(manifest)
    catalog = StoreCatalog(tmp_path, manifest=manifest, observability=observability)
    managed = catalog.open_connection("tasks", path, owner="lane-b-review")
    managed.execute("CREATE TABLE item(value INTEGER)")
    managed.commit()
    reader = sqlite3.connect(path, timeout=0.025)
    observability.enable_runtime()

    try:
        for boundary in ("commit", "context-exit"):
            reader.execute("BEGIN")
            assert reader.execute("SELECT COUNT(*) FROM item").fetchone() == (0,)
            if boundary == "commit":
                managed.execute("INSERT INTO item VALUES (1)")
                with pytest.raises(sqlite3.OperationalError) as caught:
                    managed.commit()
            else:
                with pytest.raises(sqlite3.OperationalError) as caught:
                    with managed:
                        managed.execute("INSERT INTO item VALUES (1)")
            assert _primary_error_code(caught.value) == sqlite3.SQLITE_BUSY
            reader.rollback()
            if managed.in_transaction:
                managed.rollback()
    finally:
        if reader.in_transaction:
            reader.rollback()
        if managed.in_transaction:
            managed.rollback()
        reader.close()
        managed.close()

    metrics = observability.snapshot()["runtime"]["stores"]["tasks"]
    assert metrics["busy_results_total"] == 2
    assert metrics["transactions"]["rolled_back_total"] == 2


def test_review_failed_short_wait_does_not_rearm_threshold_event(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    path = tmp_path / "wait-latch.db"
    manifest = {"tasks": _target("tasks", path)}
    clock = _PairClock([300, 1, 400, 1, 400])
    observability = StorageObservability(manifest, clock_ns=clock)
    observability.enable_runtime()
    caplog.set_level(logging.INFO, logger="pinky.storage")

    for succeeded, error_code in (
        (False, sqlite3.SQLITE_BUSY),
        (False, sqlite3.SQLITE_BUSY),
        (False, sqlite3.SQLITE_BUSY),
        (True, None),
        (False, sqlite3.SQLITE_BUSY),
    ):
        operation = observability.begin_runtime_operation("tasks", lock_bearing=True)
        assert operation is not None
        observability.finish_runtime_operation(
            operation,
            succeeded=succeeded,
            sqlite_primary_error_code=error_code,
        )

    wait_events = [
        record.getMessage()
        for record in caplog.records
        if "kind=lock_wait_upper_bound" in record.getMessage()
    ]
    assert len(wait_events) == 2


def test_review_close_records_implicit_transaction_rollback(tmp_path: Path) -> None:
    path = tmp_path / "close-rollback.db"
    manifest = {"tasks": _target("tasks", path)}
    observability = StorageObservability(manifest)
    catalog = StoreCatalog(tmp_path, manifest=manifest, observability=observability)
    connection = catalog.open_connection("tasks", path, owner="lane-b-review")
    connection.execute("CREATE TABLE item(value INTEGER)")
    connection.commit()
    observability.enable_runtime()

    connection.execute("INSERT INTO item VALUES (1)")
    assert connection.in_transaction is True
    connection.close()

    metrics = observability.snapshot()["runtime"]["stores"]["tasks"]
    assert metrics["transactions"]["rolled_back_total"] == 1
    assert metrics["transaction_duration_ms"]["count"] == 1
    assert connection._store_transaction_started_ns is None
    verifier = sqlite3.connect(path)
    try:
        assert verifier.execute("SELECT COUNT(*) FROM item").fetchone() == (0,)
    finally:
        verifier.close()


def test_review_quick_check_error_survives_reconciliation_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "combined-preflight-failure.db"
    seed = sqlite3.connect(path)
    seed.execute("CREATE TABLE item(value INTEGER)")
    seed.commit()
    seed.close()
    manifest = {"tasks": _target("tasks", path)}
    observability = StorageObservability(manifest)
    catalog = StoreCatalog(tmp_path, manifest=manifest, observability=observability)

    class FailingQuickCheck:
        def __init__(self) -> None:
            self.statement = ""

        def execute(self, statement: str) -> FailingQuickCheck:
            self.statement = statement
            return self

        def fetchone(self) -> tuple[str]:
            assert self.statement == "PRAGMA journal_mode"
            return ("delete",)

        def fetchall(self) -> list[tuple[str]]:
            assert self.statement == "PRAGMA quick_check"
            raise sqlite3.DatabaseError("review quick-check failure")

        def close(self) -> None:
            return None

    monkeypatch.setattr(
        BoundSQLiteFile,
        "connect_read_only",
        lambda _bound_file: FailingQuickCheck(),
    )

    def fail_reconciliation(_bound_file: BoundSQLiteFile) -> str:
        raise OSError("review reconciliation failure")

    monkeypatch.setattr(
        BoundSQLiteFile,
        "path_state",
        fail_reconciliation,
    )

    with pytest.raises(StoreCatalogError):
        catalog.preflight_integrity(manifest.values())

    corruption = observability.snapshot()["corruption"]
    assert corruption["preflight_refusals_total"] == 1
    assert corruption["quick_check_failures_total"] == 1
    assert corruption["stores"]["tasks"] == {
        "preflight_refusals": 1,
        "quick_check_failures": 1,
    }


def test_review_mutating_pragma_is_classified_lock_bearing(tmp_path: Path) -> None:
    path = tmp_path / "mutating-pragma.db"
    manifest = {"tasks": _target("tasks", path)}
    clock = _PairClock([1])
    observability = StorageObservability(manifest, clock_ns=clock)
    catalog = StoreCatalog(tmp_path, manifest=manifest, observability=observability)
    connection = catalog.open_connection("tasks", path, owner="lane-b-review")
    observability.enable_runtime()
    try:
        connection.execute("PRAGMA user_version=1")
    finally:
        connection.close()

    metrics = observability.snapshot()["runtime"]["stores"]["tasks"]
    assert metrics["lock_wait_upper_bound_ms"]["count"] == 1


def test_review_snapshot_quick_check_exception_is_counted_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "snapshot-source.db"
    seed = sqlite3.connect(path)
    seed.execute("CREATE TABLE item(value INTEGER)")
    seed.execute("INSERT INTO item VALUES (1)")
    seed.commit()
    seed.close()
    manifest = {
        "sessions": _target("sessions", path),
        "session_events": _target("session_events", path),
    }
    observability = StorageObservability(manifest)
    catalog = StoreCatalog(tmp_path, manifest=manifest, observability=observability)
    for logical_name in manifest:
        catalog.register(
            logical_name,
            path,
            journal_mode="delete",
            owner="lane-b-review",
        )

    real_connect = sqlite3.connect

    class BadQuickCheckConnection(sqlite3.Connection):
        def execute(
            self,
            sql: str,
            parameters: Any = (),
            /,
        ) -> sqlite3.Cursor:
            if sql == "PRAGMA quick_check":
                raise sqlite3.DatabaseError("review snapshot quick-check failure")
            return super().execute(sql, parameters)

    def connect_with_bad_destination(database: Any, *args: Any, **kwargs: Any) -> Any:
        if os.fspath(database).endswith(".tmp"):
            kwargs["factory"] = BadQuickCheckConnection
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(snapshot_module.sqlite3, "connect", connect_with_bad_destination)
    [result] = StoreSnapshotService(catalog, observability=observability).create_snapshots(
        "sessions"
    )

    assert result.status == "failed"
    corruption = observability.snapshot()["corruption"]
    assert corruption["quick_check_failures_total"] == 1
    for logical_name in manifest:
        assert corruption["stores"][logical_name]["quick_check_failures"] == 1
