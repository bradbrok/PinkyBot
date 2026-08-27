"""Lane B contracts for bounded, honest SQLite runtime observability."""

from __future__ import annotations

import json
import logging
import math
import os
import sqlite3
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from pinky_daemon import api as api_module
from pinky_daemon import storage_observability as observability_module
from pinky_daemon import store_catalog as catalog_module
from pinky_daemon import store_snapshot as snapshot_module
from pinky_daemon.auth import build_internal_auth_headers
from pinky_daemon.storage_observability import StorageObservability
from pinky_daemon.store_catalog import (
    BoundSQLiteFile,
    StoreCatalog,
    StoreCatalogError,
    StoreConnectionPolicy,
    StoreIntegrityTarget,
)
from pinky_daemon.store_manifest import (
    derive_fleet_store_manifest,
    derive_standalone_tenant_store_manifest,
)
from pinky_daemon.store_snapshot import StoreSnapshotService

HISTOGRAM_BOUNDS_MS = [1, 5, 10, 25, 50, 100, 250, 500, 1_000, 5_000, 30_000]
HISTOGRAM_BUCKET_COUNT = len(HISTOGRAM_BOUNDS_MS) + 1


class _PairClock:
    """Return start/end pairs with deterministic elapsed milliseconds."""

    def __init__(self, durations_ms: list[int]) -> None:
        self._durations_ns = [duration * 1_000_000 for duration in durations_ms]
        self._now_ns = 0
        self._start = True

    def __call__(self) -> int:
        if self._start:
            self._start = False
            return self._now_ns
        if not self._durations_ns:
            raise AssertionError("runtime instrumentation read the clock too many times")
        self._now_ns += self._durations_ns.pop(0)
        self._start = True
        return self._now_ns

    def assert_exhausted(self) -> None:
        assert self._start
        assert self._durations_ns == []


class _TickClock:
    """Advance one millisecond per read for exact transaction buckets."""

    def __init__(self) -> None:
        self._now_ns = 0

    def __call__(self) -> int:
        value = self._now_ns
        self._now_ns += 1_000_000
        return value


def _target(
    logical_name: str,
    path: Path,
    *,
    timeout_ms: int = 5_000,
) -> StoreIntegrityTarget:
    return StoreIntegrityTarget(
        logical_name=logical_name,
        path=os.fspath(path),
        criticality="memory",
        connection_policy=StoreConnectionPolicy(busy_timeout_ms=timeout_ms),
    )


def _shared_manifest(path: Path, *, timeout_ms: int = 5_000) -> dict[str, StoreIntegrityTarget]:
    return {
        "sessions": _target("sessions", path, timeout_ms=timeout_ms),
        "session_events": _target("session_events", path, timeout_ms=timeout_ms),
    }


def _register_operator(app: Any, tmp_path: Path) -> str:
    app.state.agents.register(
        "runtime-metrics-operator",
        model="opus",
        role="operator",
        working_dir=os.fspath(tmp_path / "operator"),
    )
    return app.state.agents.get_signing_key("runtime-metrics-operator")


def _signed_headers(key: str, *, method: str, path: str) -> dict[str, str]:
    return build_internal_auth_headers(
        key,
        agent_name="runtime-metrics-operator",
        method=method,
        path=path,
    )


def _storage_status(client: TestClient, key: str) -> dict[str, Any]:
    response = client.get(
        "/admin/watchdog",
        headers=_signed_headers(key, method="GET", path="/admin/watchdog"),
    )
    assert response.status_code == 200
    return response.json()["storage"]


def _runtime(observability: StorageObservability) -> dict[str, Any]:
    snapshot = observability.snapshot()
    assert "runtime" in snapshot, "runtime SQLite metrics are not implemented"
    return snapshot["runtime"]


def _assert_histogram_shape(histogram: dict[str, Any], *, count: int) -> None:
    assert set(histogram) == {
        "count",
        "bucket_counts",
        "overflow_count",
        "p95_upper_bound_ms",
        "p99_upper_bound_ms",
    }
    assert histogram["count"] == count
    assert len(histogram["bucket_counts"]) == HISTOGRAM_BUCKET_COUNT
    assert sum(histogram["bucket_counts"]) == count
    assert histogram["overflow_count"] == histogram["bucket_counts"][-1]
    if count == 0:
        assert histogram["p95_upper_bound_ms"] is None
        assert histogram["p99_upper_bound_ms"] is None


def _contention_events(caplog: pytest.LogCaptureFixture, kind: str) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("storage_event event=contention ")
        and f"kind={kind}" in record.getMessage()
    ]


def _primary_error_code(error: sqlite3.Error) -> int | None:
    error_code = getattr(error, "sqlite_errorcode", None)
    return None if error_code is None else int(error_code) & 0xFF


def _wait_for_depth(
    observability: StorageObservability,
    logical_name: str,
    expected: int,
    *,
    timeout: float = 1.0,
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        depth = _runtime(observability)["stores"][logical_name]["writer_queue_depth"]
        if depth["current"] == expected:
            return
        time.sleep(0.005)
    depth = _runtime(observability)["stores"][logical_name]["writer_queue_depth"]
    assert depth["current"] == expected


# A + B + H: real SQLite contention, exact outcome counters, event latch, and endpoint.
def test_real_busy_results_are_exact_and_busy_event_rearms_only_after_lock_success(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("PINKY_SESSION_SECRET", "runtime-metrics-secret")
    original_manifest = api_module._derive_api_store_manifest

    def short_busy_manifest(db_path: str | os.PathLike[str]) -> dict[str, StoreIntegrityTarget]:
        manifest = original_manifest(db_path)
        task = manifest["tasks"]
        manifest["tasks"] = replace(
            task,
            connection_policy=replace(task.connection_policy, busy_timeout_ms=25),
        )
        return manifest

    monkeypatch.setattr(api_module, "_derive_api_store_manifest", short_busy_manifest)
    caplog.set_level(logging.INFO, logger="pinky.storage")
    app = api_module.create_api(db_path=os.fspath(tmp_path / "conversations.db"))
    key = _register_operator(app, tmp_path)
    client = TestClient(app)
    task_path = next(
        record.resolved_path
        for record in app.state.store_catalog.snapshot()
        if record.logical_name == "tasks"
    )
    managed = app.state.store_catalog.open_connection(
        "tasks",
        task_path,
        owner="runtime-metrics-test",
    )
    blocker = sqlite3.connect(task_path, timeout=0.025)

    def observe_busy() -> None:
        with pytest.raises(sqlite3.OperationalError) as caught:
            managed.execute("BEGIN IMMEDIATE")
        assert _primary_error_code(caught.value) == sqlite3.SQLITE_BUSY

    try:
        blocker.execute("BEGIN EXCLUSIVE")
        for _ in range(4):
            observe_busy()
        assert len(_contention_events(caplog, "busy_streak")) == 1

        blocker.rollback()
        assert managed.execute("SELECT 1").fetchone() == (1,)
        blocker.execute("BEGIN EXCLUSIVE")
        for _ in range(2):
            observe_busy()
        assert len(_contention_events(caplog, "busy_streak")) == 1

        blocker.rollback()
        managed.execute("BEGIN IMMEDIATE")
        managed.rollback()
        blocker.execute("BEGIN EXCLUSIVE")
        for _ in range(3):
            observe_busy()
        blocker.rollback()
        managed.execute("BEGIN IMMEDIATE")
        managed.rollback()

        storage = _storage_status(client, key)
    finally:
        if blocker.in_transaction:
            blocker.rollback()
        blocker.close()
        managed.close()
        client.close()

    task_metrics = storage["runtime"]["stores"]["tasks"]
    assert task_metrics["busy_results_total"] == 9
    assert task_metrics["locked_results_total"] == 0
    assert task_metrics["busy_streak"] == {"current": 0, "max": 6}
    wait_histogram = task_metrics["lock_wait_upper_bound_ms"]
    assert wait_histogram["count"] >= task_metrics["busy_results_total"]
    assert sum(wait_histogram["bucket_counts"]) == wait_histogram["count"]

    events = _contention_events(caplog, "busy_streak")
    assert len(events) == 2
    assert all("observed=3" in event and "threshold=3" in event for event in events)
    assert all(task_path not in event for event in events)
    assert all("BEGIN" not in event and "database is locked" not in event for event in events)


# B: deterministic lock-wait threshold, one event per episode, and successful rearm.
def test_lock_wait_upper_bound_event_latches_and_rearms_on_short_lock_operation(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    path = tmp_path / "tasks.db"
    manifest = {"tasks": _target("tasks", path)}
    clock = _PairClock([300, 301, 1, 100, 400])
    observability = StorageObservability(manifest, clock_ns=clock)
    catalog = StoreCatalog(
        tmp_path,
        manifest=manifest,
        observability=observability,
    )
    connection = catalog.open_connection("tasks", path, owner="runtime-metrics-test")
    connection.execute("CREATE TABLE seed(value INTEGER)")
    caplog.set_level(logging.INFO, logger="pinky.storage")
    observability.enable_runtime()

    try:
        connection.execute("CREATE TABLE first(value INTEGER)")
        connection.execute("CREATE TABLE second(value INTEGER)")
        assert connection.execute("SELECT 1").fetchone() == (1,)
        connection.execute("CREATE TABLE short(value INTEGER)")
        connection.execute("CREATE TABLE fourth(value INTEGER)")
    finally:
        connection.close()

    clock.assert_exhausted()
    wait_histogram = _runtime(observability)["stores"]["tasks"]["lock_wait_upper_bound_ms"]
    _assert_histogram_shape(wait_histogram, count=4)
    assert wait_histogram["bucket_counts"][5] == 1  # 100ms
    assert wait_histogram["bucket_counts"][7] == 3  # 300/301/400ms
    events = _contention_events(caplog, "lock_wait_upper_bound")
    assert len(events) == 2
    assert all("threshold_ms=250" in event for event in events)
    assert all("observed_ms=" in event for event in events)
    assert all(os.fspath(path) not in event for event in events)


# C: fixed non-cumulative histogram memory under deterministic churn.
def test_histograms_remain_fixed_cardinality_under_ten_thousand_samples(
    tmp_path: Path,
) -> None:
    path = tmp_path / "churn.db"
    manifest = {"tasks": _target("tasks", path)}
    durations = [1] * 9_500 + [250] * 399 + [30_000] * 100 + [30_001]
    clock = _PairClock(durations.copy())
    observability = StorageObservability(manifest, clock_ns=clock)
    catalog = StoreCatalog(
        tmp_path,
        manifest=manifest,
        observability=observability,
    )
    connection = catalog.open_connection("tasks", path, owner="runtime-metrics-test")
    observability.enable_runtime()

    try:
        for _ in durations:
            connection.execute("CREATE TABLE IF NOT EXISTS churn(value INTEGER)")
    finally:
        connection.close()

    clock.assert_exhausted()
    store_metrics = _runtime(observability)["stores"]["tasks"]
    expected_buckets = [0] * HISTOGRAM_BUCKET_COUNT
    expected_buckets[0] = 9_500
    expected_buckets[6] = 399
    expected_buckets[10] = 100
    expected_buckets[11] = 1
    for metric_name in ("lock_wait_upper_bound_ms", "transaction_duration_ms"):
        histogram = store_metrics[metric_name]
        _assert_histogram_shape(histogram, count=10_000)
        assert histogram["bucket_counts"] == expected_buckets
        assert histogram["overflow_count"] == 1
        assert histogram["p95_upper_bound_ms"] == 1
        assert histogram["p99_upper_bound_ms"] == 30_000

    encoded = json.dumps(observability.snapshot(), sort_keys=True)
    assert "samples" not in encoded
    assert "30001" not in encoded


# D: a disabled catalog has no timer, task, sampler, clock read, or runtime event.
def test_disabled_recorder_path_has_no_clock_or_background_worker_cost(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def forbidden_clock() -> int:
        raise AssertionError("disabled storage recorder read the monotonic clock")

    monkeypatch.setattr(observability_module, "_monotonic_ns", forbidden_clock, raising=False)
    threads_before = {thread.ident for thread in threading.enumerate()}
    caplog.set_level(logging.INFO, logger="pinky.storage")
    catalog = StoreCatalog(tmp_path)
    connection = catalog.open_connection(
        "tasks",
        tmp_path / "disabled.db",
        owner="disabled-runtime-test",
    )
    try:
        connection.execute("CREATE TABLE item(value INTEGER)")
        connection.commit()
    finally:
        connection.close()

    assert {thread.ident for thread in threading.enumerate()} == threads_before
    assert _contention_events(caplog, "busy_streak") == []
    assert _contention_events(caplog, "lock_wait_upper_bound") == []


def test_serving_endpoint_adds_exact_runtime_and_corruption_keys_but_starts_idle(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("PINKY_SESSION_SECRET", "idle-runtime-secret")
    base = tmp_path / "conversations.db"
    app = api_module.create_api(db_path=os.fspath(base))
    key = _register_operator(app, tmp_path)
    client = TestClient(app)
    try:
        storage = _storage_status(client, key)
    finally:
        client.close()

    assert set(storage) == {
        "boot_gate",
        "preflight",
        "inventory",
        "reconcile_warning_count",
        "snapshots",
        "runtime",
        "corruption",
    }
    runtime = storage["runtime"]
    assert set(runtime) == {"enabled", "histogram_bounds_ms", "thresholds", "stores"}
    assert runtime["enabled"] is True
    assert runtime["histogram_bounds_ms"] == HISTOGRAM_BOUNDS_MS
    assert runtime["thresholds"] == {
        "busy_streak": 3,
        "lock_wait_upper_bound_ms": 250,
    }
    assert len(runtime["stores"]) == 24
    assert set(runtime["stores"]) == set(api_module._derive_api_store_manifest(base))

    cohort_ids = set()
    for metrics in runtime["stores"].values():
        assert set(metrics) == {
            "busy_results_total",
            "locked_results_total",
            "busy_streak",
            "lock_wait_upper_bound_ms",
            "transaction_duration_ms",
            "transactions",
            "writer_queue_depth",
        }
        assert metrics["busy_results_total"] == 0
        assert metrics["locked_results_total"] == 0
        assert metrics["busy_streak"] == {"current": 0, "max": 0}
        _assert_histogram_shape(metrics["lock_wait_upper_bound_ms"], count=0)
        _assert_histogram_shape(metrics["transaction_duration_ms"], count=0)
        assert metrics["transactions"] == {"committed_total": 0, "rolled_back_total": 0}
        depth = metrics["writer_queue_depth"]
        assert set(depth) == {"cohort_id", "current", "high_water"}
        assert depth["current"] == 0
        assert depth["high_water"] == 0
        assert isinstance(depth["cohort_id"], str) and depth["cohort_id"]
        cohort_ids.add(depth["cohort_id"])

    assert len(cohort_ids) == 22
    assert (
        runtime["stores"]["sessions"]["writer_queue_depth"]
        == runtime["stores"]["session_events"]["writer_queue_depth"]
    )
    assert (
        runtime["stores"]["agents"]["writer_queue_depth"]
        == runtime["stores"]["agent_signing_keys"]["writer_queue_depth"]
    )
    corruption = storage["corruption"]
    assert set(corruption) == {
        "preflight_refusals_total",
        "quick_check_failures_total",
        "stores",
    }
    assert corruption["preflight_refusals_total"] == 0
    assert corruption["quick_check_failures_total"] == 0
    assert set(corruption["stores"]) == set(runtime["stores"])
    assert all(
        metrics == {"preflight_refusals": 0, "quick_check_failures": 0}
        for metrics in corruption["stores"].values()
    )
    encoded = json.dumps(storage, sort_keys=True)
    assert os.path.realpath(base) not in encoded
    assert "idle-runtime-secret" not in encoded


# Requirement 2: bounded first-token classification, never a full SQL regex/scan.
def test_sql_classification_reads_only_a_bounded_prefix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class BoundedStatement(str):
        def __new__(cls, value: str):
            instance = super().__new__(cls, value)
            instance.slices = []
            return instance

        def __getitem__(self, key: object) -> str:
            assert isinstance(key, slice), "classification indexed the full statement"
            assert key.stop is not None and key.stop <= 64
            self.slices.append(key)
            return super().__getitem__(key)

        def lstrip(self, *args: object, **kwargs: object) -> str:
            raise AssertionError("classification stripped the full statement")

        def split(self, *args: object, **kwargs: object) -> list[str]:
            raise AssertionError("classification split the full statement")

        def upper(self) -> str:
            raise AssertionError("classification uppercased the full statement")

    class NoRegex:
        def __getattr__(self, name: str) -> Any:
            raise AssertionError(f"classification used regex operation {name}")

    path = tmp_path / "classification.db"
    manifest = {"tasks": _target("tasks", path)}
    observability = StorageObservability(manifest)
    catalog = StoreCatalog(
        tmp_path,
        manifest=manifest,
        observability=observability,
    )
    connection = catalog.open_connection("tasks", path, owner="classification-test")
    observability.enable_runtime()
    monkeypatch.setattr(catalog_module, "re", NoRegex())
    statement = BoundedStatement("SELECT 1 /*" + ("x" * 100_000) + "*/")
    try:
        assert connection.execute(statement).fetchone() == (1,)
    finally:
        connection.close()

    assert statement.slices, "managed execution never classified the SQL statement"
    wait_histogram = _runtime(observability)["stores"]["tasks"]["lock_wait_upper_bound_ms"]
    assert wait_histogram["count"] == 0


# E + requirement 3: transaction durations and context-manager exact-once accounting.
def test_transaction_timing_and_context_manager_counts_are_exact(
    tmp_path: Path,
) -> None:
    path = tmp_path / "transactions.db"
    manifest = {"tasks": _target("tasks", path)}
    observability = StorageObservability(manifest, clock_ns=_TickClock())
    catalog = StoreCatalog(
        tmp_path,
        manifest=manifest,
        observability=observability,
    )
    connection = catalog.open_connection("tasks", path, owner="transaction-test")
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
    with pytest.raises(RuntimeError, match="roll back this context"):
        with connection:
            connection.execute("INSERT INTO item VALUES (4)")
            raise RuntimeError("roll back this context")

    autocommit = catalog.open_connection(
        "tasks",
        path,
        owner="autocommit-transaction-test",
        isolation_level=None,
    )
    try:
        autocommit.execute("INSERT INTO item VALUES (5)")
        autocommit.executescript("INSERT INTO item VALUES (6); INSERT INTO item VALUES (7);")
    finally:
        autocommit.close()
        connection.close()

    metrics = _runtime(observability)["stores"]["tasks"]
    assert metrics["transactions"] == {"committed_total": 2, "rolled_back_total": 2}
    histogram = metrics["transaction_duration_ms"]
    _assert_histogram_shape(histogram, count=6)
    assert histogram["bucket_counts"][0] == 2  # autocommit + executescript, 1ms each
    assert histogram["bucket_counts"][1] == 4  # explicit/context transactions, <=5ms
    assert connection.in_transaction is False


# F + requirements 1/5: shared physical lane depth without recorder serialization.
def test_shared_writer_lane_depth_is_non_summable_and_returns_to_zero(
    tmp_path: Path,
) -> None:
    path = tmp_path / "sessions.db"
    manifest = _shared_manifest(path, timeout_ms=1_000)
    observability = StorageObservability(manifest)
    catalog = StoreCatalog(
        tmp_path,
        manifest=manifest,
        observability=observability,
    )
    holder = catalog.open_connection("sessions", path, owner="shared-session-owner")
    waiter = catalog.open_connection("session_events", path, owner="shared-session-owner")
    holder.execute("CREATE TABLE event(value INTEGER)")
    holder.commit()
    observability.enable_runtime()
    acquired = threading.Event()
    release_waiter = threading.Event()
    worker_errors: list[BaseException] = []

    def wait_for_writer() -> None:
        try:
            waiter.execute("BEGIN IMMEDIATE")
            acquired.set()
            assert release_waiter.wait(1.0)
            waiter.rollback()
        except BaseException as exc:
            worker_errors.append(exc)
            acquired.set()

    holder.execute("BEGIN IMMEDIATE")
    worker = threading.Thread(target=wait_for_writer, name="storage-writer-depth-test")
    worker.start()
    try:
        _wait_for_depth(observability, "sessions", 1)
        holder.rollback()
        assert acquired.wait(1.0)
        _wait_for_depth(observability, "sessions", 0)
        runtime = _runtime(observability)
        sessions_depth = runtime["stores"]["sessions"]["writer_queue_depth"]
        events_depth = runtime["stores"]["session_events"]["writer_queue_depth"]
        assert sessions_depth == events_depth
        assert sessions_depth["high_water"] == 1
        assert set(sessions_depth) == {"cohort_id", "current", "high_water"}
        assert os.fspath(path) not in json.dumps(sessions_depth)
    finally:
        release_waiter.set()
        worker.join(2.0)
        if holder.in_transaction:
            holder.rollback()
        holder.close()
        waiter.close()

    assert not worker.is_alive()
    assert worker_errors == []


def test_writer_lane_depth_decrements_in_finally_after_raising_call(tmp_path: Path) -> None:
    path = tmp_path / "sessions-raising.db"
    manifest = _shared_manifest(path, timeout_ms=25)
    observability = StorageObservability(manifest)
    catalog = StoreCatalog(
        tmp_path,
        manifest=manifest,
        observability=observability,
    )
    holder = catalog.open_connection("sessions", path, owner="shared-session-owner")
    waiter = catalog.open_connection("session_events", path, owner="shared-session-owner")
    holder.execute("CREATE TABLE event(value INTEGER)")
    holder.commit()
    observability.enable_runtime()
    errors: list[sqlite3.OperationalError] = []

    def fail_for_writer() -> None:
        try:
            waiter.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            errors.append(exc)

    holder.execute("BEGIN IMMEDIATE")
    worker = threading.Thread(target=fail_for_writer, name="storage-writer-error-test")
    worker.start()
    worker.join(2.0)
    try:
        assert not worker.is_alive()
        assert len(errors) == 1
        assert _primary_error_code(errors[0]) == sqlite3.SQLITE_BUSY
        runtime = _runtime(observability)
        depth = runtime["stores"]["session_events"]["writer_queue_depth"]
        assert depth["current"] == 0
        assert depth["high_water"] == 1
    finally:
        holder.rollback()
        holder.close()
        waiter.close()


def test_extended_sqlite_locked_is_counted_separately_from_busy(tmp_path: Path) -> None:
    path = tmp_path / "shared-cache.db"
    uri = f"file:{path}?cache=shared"
    manifest = {"tasks": _target("tasks", path, timeout_ms=25)}
    observability = StorageObservability(manifest)
    catalog = StoreCatalog(
        tmp_path,
        manifest=manifest,
        observability=observability,
    )
    holder = catalog.open_connection("tasks", uri, owner="locked-test", uri=True)
    waiter = catalog.open_connection("tasks", uri, owner="locked-test", uri=True)
    holder.execute("CREATE TABLE item(value INTEGER)")
    holder.execute("INSERT INTO item VALUES (1)")
    holder.commit()
    observability.enable_runtime()

    holder.execute("BEGIN IMMEDIATE")
    holder.execute("UPDATE item SET value = 2")
    try:
        with pytest.raises(sqlite3.OperationalError) as caught:
            waiter.execute("UPDATE item SET value = 3")
        assert _primary_error_code(caught.value) == sqlite3.SQLITE_LOCKED
    finally:
        holder.rollback()
        holder.close()
        waiter.close()

    metrics = _runtime(observability)["stores"]["tasks"]
    assert metrics["busy_results_total"] == 0
    assert metrics["locked_results_total"] == 1


# G: one physical preflight refusal, per-logical fanout, exact quick_check subset.
@pytest.mark.parametrize("quick_check", [sqlite3.DatabaseError("private-detail"), [("bad",)]])
def test_preflight_corruption_counts_physical_once_and_shared_aliases_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    quick_check: sqlite3.DatabaseError | list[tuple[str]],
) -> None:
    path = tmp_path / "shared-corrupt.db"
    seed = sqlite3.connect(path)
    seed.execute("CREATE TABLE item(value INTEGER)")
    seed.commit()
    seed.close()
    manifest = _shared_manifest(path)
    observability = StorageObservability(manifest)
    catalog = StoreCatalog(
        tmp_path,
        manifest=manifest,
        observability=observability,
    )

    class FakeQuickCheckConnection:
        def execute(self, statement: str) -> FakeQuickCheckConnection:
            assert statement == "PRAGMA quick_check"
            if isinstance(quick_check, sqlite3.DatabaseError):
                raise quick_check
            return self

        def fetchall(self) -> list[tuple[str]]:
            assert isinstance(quick_check, list)
            return quick_check

        def close(self) -> None:
            return None

    monkeypatch.setattr(
        BoundSQLiteFile,
        "connect_read_only",
        lambda _bound_file: FakeQuickCheckConnection(),
    )
    caplog.set_level(logging.INFO, logger="pinky.storage")

    with pytest.raises(StoreCatalogError):
        catalog.preflight_integrity(manifest.values())

    corruption = observability.snapshot()["corruption"]
    assert corruption["preflight_refusals_total"] == 1
    assert corruption["quick_check_failures_total"] == 1
    for logical_name in ("sessions", "session_events"):
        assert corruption["stores"][logical_name] == {
            "preflight_refusals": 1,
            "quick_check_failures": 1,
        }
    events = [
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("storage_event event=corruption ")
    ]
    assert any("source=preflight" in event for event in events)
    assert all(os.fspath(path) not in event for event in events)
    assert all("private-detail" not in event and "bad" not in event for event in events)


def test_snapshot_destination_quick_check_failure_is_counted_live_without_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "sessions.db"
    seed = sqlite3.connect(path)
    seed.execute("CREATE TABLE item(value INTEGER)")
    seed.execute("INSERT INTO item VALUES (1)")
    seed.commit()
    seed.close()
    manifest = _shared_manifest(path)
    observability = StorageObservability(manifest)
    catalog = StoreCatalog(
        tmp_path,
        manifest=manifest,
        observability=observability,
    )
    for logical_name in manifest:
        catalog.register(
            logical_name,
            path,
            journal_mode="delete",
            owner="shared-session-owner",
        )

    real_connect = sqlite3.connect

    class FailedRows:
        def fetchall(self) -> list[tuple[str]]:
            return [("snapshot-private-corruption",)]

    class BadQuickCheckConnection(sqlite3.Connection):
        def execute(
            self,
            sql: str,
            parameters: Any = (),
            /,
        ) -> sqlite3.Cursor | FailedRows:
            if sql == "PRAGMA quick_check":
                return FailedRows()
            return super().execute(sql, parameters)

    def connect_with_bad_destination(database: Any, *args: Any, **kwargs: Any) -> Any:
        if os.fspath(database).endswith(".tmp"):
            kwargs["factory"] = BadQuickCheckConnection
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(snapshot_module.sqlite3, "connect", connect_with_bad_destination)
    service = StoreSnapshotService(catalog, observability=observability)
    [result] = service.create_snapshots("sessions")

    assert result.status == "failed"
    corruption = observability.snapshot()["corruption"]
    assert corruption["preflight_refusals_total"] == 0
    assert corruption["quick_check_failures_total"] == 1
    for logical_name in ("sessions", "session_events"):
        assert corruption["stores"][logical_name] == {
            "preflight_refusals": 0,
            "quick_check_failures": 1,
        }
    encoded = json.dumps(corruption, sort_keys=True)
    assert os.fspath(path) not in encoded
    assert "snapshot-private-corruption" not in encoded
    assert list(tmp_path.rglob("*.json")) == []


# H: Lane A policy/retry behavior and the 24-name manifest remain exact.
def test_manifest_cardinality_alias_origin_and_connection_policy_are_unchanged(
    tmp_path: Path,
) -> None:
    fleet = derive_fleet_store_manifest(tmp_path / "conversations.db")
    tenant = derive_standalone_tenant_store_manifest(tmp_path / "tenant-keys.db")

    assert len(fleet) == 24
    assert set(fleet).intersection(tenant) == {"agent_signing_keys"}
    assert fleet["agents"].path == fleet["agent_signing_keys"].path
    assert fleet["sessions"].path == fleet["session_events"].path
    timeout_counts = {
        timeout_ms: sum(
            target.connection_policy.busy_timeout_ms == timeout_ms for target in fleet.values()
        )
        for timeout_ms in (5_000, 30_000)
    }
    assert timeout_counts == {5_000: 9, 30_000: 15}
    assert {target.connection_policy.rollback_retries for target in fleet.values()} == {6}
    assert {target.connection_policy.rollback_retry_delay_seconds for target in fleet.values()} == {
        0.2
    }


def test_runtime_wrapper_does_not_add_retry_sleep_or_journal_mode_writes() -> None:
    source = Path(catalog_module.__file__).read_text(encoding="utf-8")
    managed_start = source.index("class _ManagedSQLiteConnection")
    managed_end = source.index("\n\n@dataclass", managed_start)
    managed_source = source[managed_start:managed_end]

    assert "sleep(" not in managed_source
    assert "rollback_retries" not in managed_source
    assert "journal_mode=" not in managed_source.lower()
    assert "set_busy_handler" not in source


def test_histogram_quantile_rank_contract_is_nearest_rank() -> None:
    assert math.ceil(0.95 * 10_000) == 9_500
    assert math.ceil(0.99 * 10_000) == 9_900
