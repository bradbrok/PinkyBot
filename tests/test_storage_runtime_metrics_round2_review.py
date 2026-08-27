"""Round-2 adversarial probes for the Lane B remediation review."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from pinky_daemon.storage_observability import StorageObservability
from pinky_daemon.store_catalog import StoreCatalog, StoreIntegrityTarget


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
            raise AssertionError("round-2 review clock was read too many times")
        self._now_ns += self._durations_ns.pop(0)
        self._start = True
        return self._now_ns


def _target(path: Path) -> StoreIntegrityTarget:
    return StoreIntegrityTarget(
        logical_name="tasks",
        path=os.fspath(path),
        criticality="memory",
    )


def test_assignment_form_pragma_remains_lock_bearing_past_bounded_prefix(
    tmp_path: Path,
) -> None:
    path = tmp_path / "long-assignment-pragma.db"
    manifest = {"tasks": _target(path)}
    observability = StorageObservability(manifest, clock_ns=_PairClock([1]))
    catalog = StoreCatalog(tmp_path, manifest=manifest, observability=observability)
    connection = catalog.open_connection("tasks", path, owner="lane-b-round2-review")
    observability.enable_runtime()

    # SQLite accepts arbitrary whitespace before the assignment operator. The
    # statement is still assignment-form even when '=' falls beyond byte 64.
    statement = "PRAGMA user_version" + (" " * 80) + "=1"
    try:
        connection.execute(statement)
    finally:
        connection.close()

    verifier = sqlite3.connect(path)
    try:
        assert verifier.execute("PRAGMA user_version").fetchone() == (1,)
    finally:
        verifier.close()

    metrics = observability.snapshot()["runtime"]["stores"]["tasks"]
    assert metrics["lock_wait_upper_bound_ms"]["count"] == 1


class _BoundarySpy(StorageObservability):
    def __init__(self, manifest: dict[str, StoreIntegrityTarget]) -> None:
        super().__init__(manifest)
        self.lock_bearing_operations: list[bool] = []

    def begin_runtime_operation(
        self,
        logical_name: str,
        *,
        lock_bearing: bool,
    ):
        operation = super().begin_runtime_operation(
            logical_name,
            lock_bearing=lock_bearing,
        )
        if operation is not None:
            self.lock_bearing_operations.append(lock_bearing)
        return operation


def test_transaction_boundaries_honor_the_binding_semantic_split_exactly_once(
    tmp_path: Path,
) -> None:
    path = tmp_path / "boundary-split.db"
    manifest = {"tasks": _target(path)}
    observability = _BoundarySpy(manifest)
    catalog = StoreCatalog(tmp_path, manifest=manifest, observability=observability)
    connection = catalog.open_connection("tasks", path, owner="lane-b-round2-review")
    connection.execute("CREATE TABLE item(value INTEGER)")
    connection.commit()
    observability.enable_runtime()

    connection.execute("BEGIN")
    connection.execute("INSERT INTO item VALUES (1)")
    connection.commit()
    connection.execute("BEGIN")
    connection.execute("INSERT INTO item VALUES (2)")
    connection.rollback()
    with connection:
        connection.execute("INSERT INTO item VALUES (3)")
    try:
        with connection:
            connection.execute("INSERT INTO item VALUES (4)")
            raise RuntimeError("force context rollback")
    except RuntimeError:
        pass
    connection.close()

    assert observability.lock_bearing_operations == [
        True,
        True,
        True,  # BEGIN, INSERT, direct commit
        True,
        True,
        False,  # BEGIN, INSERT, direct rollback
        True,
        True,  # INSERT, context-manager commit
        True,
        False,  # INSERT, context-manager rollback
    ]
    metrics = observability.snapshot()["runtime"]["stores"]["tasks"]
    assert metrics["transactions"] == {
        "committed_total": 2,
        "rolled_back_total": 2,
    }
    assert metrics["transaction_duration_ms"]["count"] == 4
    assert metrics["writer_queue_depth"]["current"] == 0
