"""Bounded, process-local observability for daemon SQLite storage operations."""

from __future__ import annotations

import bisect
import logging
import math
import os
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from time import monotonic_ns as _monotonic_ns
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from collections.abc import Iterable

    from pinky_daemon.store_catalog import StoreIntegrityTarget, StoreRecord
    from pinky_daemon.store_snapshot import SnapshotResult

logger = logging.getLogger("pinky.storage")

HISTOGRAM_BOUNDS_MS = (1, 5, 10, 25, 50, 100, 250, 500, 1_000, 5_000, 30_000)
BUSY_STREAK_THRESHOLD = 3
LOCK_WAIT_THRESHOLD_MS = 250


@dataclass(slots=True)
class _BoundedHistogram:
    bucket_counts: list[int] = field(default_factory=lambda: [0] * (len(HISTOGRAM_BOUNDS_MS) + 1))
    count: int = 0

    def record(self, upper_bound_ms: int) -> None:
        bucket = bisect.bisect_left(HISTOGRAM_BOUNDS_MS, upper_bound_ms)
        self.bucket_counts[bucket] += 1
        self.count += 1

    def snapshot(self) -> dict[str, object]:
        return {
            "count": self.count,
            "bucket_counts": list(self.bucket_counts),
            "overflow_count": self.bucket_counts[-1],
            "p95_upper_bound_ms": self._quantile_upper_bound(0.95),
            "p99_upper_bound_ms": self._quantile_upper_bound(0.99),
        }

    def _quantile_upper_bound(self, quantile: float) -> int | None:
        if self.count == 0:
            return None
        rank = math.ceil(quantile * self.count)
        observed = 0
        for index, count in enumerate(self.bucket_counts):
            observed += count
            if observed >= rank:
                if index == len(HISTOGRAM_BOUNDS_MS):
                    return None
                return HISTOGRAM_BOUNDS_MS[index]
        raise AssertionError("histogram count diverged from its fixed buckets")


@dataclass(slots=True)
class _WriterQueueDepth:
    cohort_id: str
    current: int = 0
    high_water: int = 0


@dataclass(slots=True)
class _RuntimeStoreMetrics:
    writer_queue_depth: _WriterQueueDepth
    busy_results_total: int = 0
    locked_results_total: int = 0
    busy_streak_current: int = 0
    busy_streak_max: int = 0
    busy_event_latched: bool = False
    lock_wait_event_latched: bool = False
    lock_wait: _BoundedHistogram = field(default_factory=_BoundedHistogram)
    transaction_duration: _BoundedHistogram = field(default_factory=_BoundedHistogram)
    committed_total: int = 0
    rolled_back_total: int = 0


@dataclass(frozen=True, slots=True)
class RuntimeOperation:
    """One enabled SQLite call whose timer and queue slot must finish exactly once."""

    logical_name: str
    started_ns: int
    lock_bearing: bool


def _elapsed_upper_bound_ms(started_ns: int, finished_ns: int) -> int:
    return max(0, math.ceil((finished_ns - started_ns) / 1_000_000))


def _timestamp() -> str:
    return datetime.now(UTC).isoformat()


def emit_storage_event(event: str, /, **fields: str | int) -> None:
    """Write one machine-readable event containing only caller-selected fields."""
    rendered = ["storage_event", f"event={event}"]
    rendered.extend(f"{key}={value}" for key, value in fields.items())
    logger.info(" ".join(rendered))


class StorageObservability:
    """Current-boot metrics exposed through the existing watchdog endpoint."""

    def __init__(
        self,
        manifest: dict[str, StoreIntegrityTarget],
        *,
        clock_ns: Callable[[], int] | None = None,
    ) -> None:
        self._lock = threading.RLock()
        self._manifest = dict(manifest)
        self._clock_ns = clock_ns
        self._runtime_armed = False
        self._runtime_enabled = False
        self._preflight = {logical_name: "pending" for logical_name in self._manifest}
        self._boot_gate: dict[str, object] = {
            "outcome": "pending",
            "warning_count": 0,
            "timestamp": None,
        }
        self._inventory: dict[str, object] = {
            "logical_count": 0,
            "physical_count": 0,
            "stores": {},
        }
        self._reconcile_warning_count = 0
        self._snapshots = {
            logical_name: {
                "count": 0,
                "last_timestamp": None,
                "last_outcome": None,
            }
            for logical_name in self._manifest
        }
        queue_depths: dict[str, _WriterQueueDepth] = {}
        self._runtime: dict[str, _RuntimeStoreMetrics] = {}
        for logical_name, target in self._manifest.items():
            physical_path = os.path.realpath(os.fspath(target.path))
            depth = queue_depths.get(physical_path)
            if depth is None:
                depth = _WriterQueueDepth(cohort_id=f"store-{len(queue_depths) + 1:02d}")
                queue_depths[physical_path] = depth
            self._runtime[logical_name] = _RuntimeStoreMetrics(writer_queue_depth=depth)
        self._corruption_preflight_refusals_total = 0
        self._corruption_quick_check_failures_total = 0
        self._corruption = {
            logical_name: {
                "preflight_refusals": 0,
                "quick_check_failures": 0,
            }
            for logical_name in self._manifest
        }

    def enable_runtime(self) -> None:
        """Enable synchronous runtime recording after boot-time construction."""
        with self._lock:
            self._runtime_enabled = True

    def arm_runtime(self) -> None:
        """Mark boot complete so the next catalog/ASGI service boundary can enable."""
        with self._lock:
            self._runtime_armed = True

    def enable_runtime_if_armed(self) -> None:
        """Activate recording only after the owning API completed its boot gate."""
        with self._lock:
            if self._runtime_armed:
                self._runtime_enabled = True

    def begin_runtime_operation(
        self,
        logical_name: str,
        *,
        lock_bearing: bool,
    ) -> RuntimeOperation | None:
        """Start one operation without reading the clock while recording is disabled."""
        with self._lock:
            if not self._runtime_enabled or logical_name not in self._runtime:
                return None
        started_ns = self._read_clock_ns()
        if lock_bearing:
            with self._lock:
                depth = self._runtime[logical_name].writer_queue_depth
                depth.current += 1
                depth.high_water = max(depth.high_water, depth.current)
        return RuntimeOperation(
            logical_name=logical_name,
            started_ns=started_ns,
            lock_bearing=lock_bearing,
        )

    def finish_runtime_operation(
        self,
        operation: RuntimeOperation,
        *,
        succeeded: bool,
        sqlite_primary_error_code: int | None = None,
    ) -> int:
        """Finish one timed call and always release its physical writer queue slot."""
        try:
            finished_ns = self._read_clock_ns()
        finally:
            if operation.lock_bearing:
                with self._lock:
                    depth = self._runtime[operation.logical_name].writer_queue_depth
                    depth.current -= 1
                    if depth.current < 0:
                        raise AssertionError("writer queue depth became negative")

        upper_bound_ms = _elapsed_upper_bound_ms(operation.started_ns, finished_ns)
        busy_event = False
        wait_event = False
        wait_observed_ms = 0
        with self._lock:
            metrics = self._runtime[operation.logical_name]
            if operation.lock_bearing:
                metrics.lock_wait.record(upper_bound_ms)
                if upper_bound_ms > LOCK_WAIT_THRESHOLD_MS:
                    if not metrics.lock_wait_event_latched:
                        metrics.lock_wait_event_latched = True
                        wait_event = True
                        wait_observed_ms = upper_bound_ms
                else:
                    metrics.lock_wait_event_latched = False

            if sqlite_primary_error_code == 5:
                metrics.busy_results_total += 1
                metrics.busy_streak_current += 1
                metrics.busy_streak_max = max(
                    metrics.busy_streak_max,
                    metrics.busy_streak_current,
                )
                if (
                    metrics.busy_streak_current >= BUSY_STREAK_THRESHOLD
                    and not metrics.busy_event_latched
                ):
                    metrics.busy_event_latched = True
                    busy_event = True
            elif sqlite_primary_error_code == 6:
                metrics.locked_results_total += 1
            elif succeeded and operation.lock_bearing:
                metrics.busy_streak_current = 0
                metrics.busy_event_latched = False

        if busy_event:
            emit_storage_event(
                "contention",
                logical_name=operation.logical_name,
                kind="busy_streak",
                observed=BUSY_STREAK_THRESHOLD,
                threshold=BUSY_STREAK_THRESHOLD,
            )
        if wait_event:
            emit_storage_event(
                "contention",
                logical_name=operation.logical_name,
                kind="lock_wait_upper_bound",
                observed_ms=wait_observed_ms,
                threshold_ms=LOCK_WAIT_THRESHOLD_MS,
            )
        return finished_ns

    def record_transaction_duration(
        self,
        logical_name: str,
        started_ns: int,
        finished_ns: int,
        *,
        outcome: str | None = None,
    ) -> None:
        """Record one completed transaction using an already-read end timestamp."""
        upper_bound_ms = _elapsed_upper_bound_ms(started_ns, finished_ns)
        with self._lock:
            metrics = self._runtime[logical_name]
            metrics.transaction_duration.record(upper_bound_ms)
            if outcome == "committed":
                metrics.committed_total += 1
            elif outcome == "rolled_back":
                metrics.rolled_back_total += 1

    def finish_transaction(
        self,
        logical_name: str,
        started_ns: int,
        *,
        outcome: str,
    ) -> None:
        """Read one load-bearing end time for commit/rollback method calls."""
        self.record_transaction_duration(
            logical_name,
            started_ns,
            self._read_clock_ns(),
            outcome=outcome,
        )

    def start_transaction(self) -> int | None:
        """Start timing a transaction that became active before runtime enablement."""
        with self._lock:
            if not self._runtime_enabled:
                return None
        return self._read_clock_ns()

    def record_preflight_refusal(
        self,
        logical_names: Iterable[str],
        *,
        quick_check_failed: bool,
    ) -> None:
        """Count one rejected physical store and fan it out to its logical aliases."""
        names = tuple(dict.fromkeys(logical_names))
        with self._lock:
            self._corruption_preflight_refusals_total += 1
            if quick_check_failed:
                self._corruption_quick_check_failures_total += 1
            for logical_name in names:
                metrics = self._corruption.get(logical_name)
                if metrics is None:
                    continue
                metrics["preflight_refusals"] += 1
                if quick_check_failed:
                    metrics["quick_check_failures"] += 1
        emit_storage_event(
            "corruption",
            source="preflight",
            logical_name=names[0] if names else "unknown",
            logical_count=len(names),
        )

    def record_snapshot_quick_check_failure(self, logical_names: Iterable[str]) -> None:
        """Count one failed destination copy and fan it out to its logical aliases."""
        names = tuple(dict.fromkeys(logical_names))
        with self._lock:
            self._corruption_quick_check_failures_total += 1
            for logical_name in names:
                metrics = self._corruption.get(logical_name)
                if metrics is not None:
                    metrics["quick_check_failures"] += 1
        emit_storage_event(
            "corruption",
            source="snapshot",
            logical_name=names[0] if names else "unknown",
            logical_count=len(names),
        )

    def _read_clock_ns(self) -> int:
        clock_ns = self._clock_ns
        if clock_ns is not None:
            return clock_ns()
        return _monotonic_ns()

    def record_preflight(self, logical_name: str, outcome: str) -> None:
        timestamp = _timestamp()
        with self._lock:
            self._preflight[logical_name] = outcome
        emit_storage_event(
            "boot",
            phase="preflight",
            logical_name=logical_name,
            outcome=outcome,
            timestamp=timestamp,
        )

    def record_boot_failure(self) -> None:
        self._record_boot_gate("fail", 0)

    def record_boot_success(
        self,
        records: Iterable[StoreRecord],
        warnings: list[str],
    ) -> None:
        records = list(records)
        stores: dict[str, dict[str, str]] = {}
        physical_paths: set[str] = set()
        for record in records:
            stores[record.logical_name] = {
                "criticality": record.criticality,
                "journal_mode": record.journal_mode,
            }
            if not record.is_memory:
                physical_paths.add(record.resolved_path)
        with self._lock:
            self._inventory = {
                "logical_count": len(stores),
                "physical_count": len(physical_paths),
                "stores": stores,
            }
            self._reconcile_warning_count = len(warnings)
        self._record_boot_gate("warn" if warnings else "pass", len(warnings))

    def _record_boot_gate(self, outcome: str, warning_count: int) -> None:
        timestamp = _timestamp()
        with self._lock:
            self._boot_gate = {
                "outcome": outcome,
                "warning_count": warning_count,
                "timestamp": timestamp,
            }
        emit_storage_event(
            "boot",
            phase="boot_gate",
            outcome=outcome,
            warning_count=warning_count,
            timestamp=timestamp,
        )

    def record_snapshot(self, result: SnapshotResult) -> None:
        timestamp = _timestamp()
        for logical_name in result.logical_names:
            with self._lock:
                if logical_name not in self._snapshots:
                    continue
                metrics = self._snapshots[logical_name]
                metrics["count"] = int(metrics["count"]) + 1
                metrics["last_timestamp"] = timestamp
                metrics["last_outcome"] = result.status
            emit_storage_event(
                "snapshot",
                logical_name=logical_name,
                outcome=result.status,
                timestamp=timestamp,
            )

    def snapshot(self) -> dict[str, object]:
        """Return a path-free copy suitable for the admin watchdog response."""
        with self._lock:
            return {
                "boot_gate": dict(self._boot_gate),
                "preflight": dict(self._preflight),
                "inventory": {
                    "logical_count": self._inventory["logical_count"],
                    "physical_count": self._inventory["physical_count"],
                    "stores": {
                        name: dict(details)
                        for name, details in dict(self._inventory["stores"]).items()
                    },
                },
                "reconcile_warning_count": self._reconcile_warning_count,
                "snapshots": {name: dict(metrics) for name, metrics in self._snapshots.items()},
                "runtime": {
                    "enabled": self._runtime_enabled,
                    "histogram_bounds_ms": list(HISTOGRAM_BOUNDS_MS),
                    "thresholds": {
                        "busy_streak": BUSY_STREAK_THRESHOLD,
                        "lock_wait_upper_bound_ms": LOCK_WAIT_THRESHOLD_MS,
                    },
                    "stores": {
                        name: self._runtime_store_snapshot(metrics)
                        for name, metrics in self._runtime.items()
                    },
                },
                "corruption": {
                    "preflight_refusals_total": self._corruption_preflight_refusals_total,
                    "quick_check_failures_total": self._corruption_quick_check_failures_total,
                    "stores": {name: dict(metrics) for name, metrics in self._corruption.items()},
                },
            }

    @staticmethod
    def _runtime_store_snapshot(metrics: _RuntimeStoreMetrics) -> dict[str, object]:
        depth = metrics.writer_queue_depth
        return {
            "busy_results_total": metrics.busy_results_total,
            "locked_results_total": metrics.locked_results_total,
            "busy_streak": {
                "current": metrics.busy_streak_current,
                "max": metrics.busy_streak_max,
            },
            "lock_wait_upper_bound_ms": metrics.lock_wait.snapshot(),
            "transaction_duration_ms": metrics.transaction_duration.snapshot(),
            "transactions": {
                "committed_total": metrics.committed_total,
                "rolled_back_total": metrics.rolled_back_total,
            },
            "writer_queue_depth": {
                "cohort_id": depth.cohort_id,
                "current": depth.current,
                "high_water": depth.high_water,
            },
        }
