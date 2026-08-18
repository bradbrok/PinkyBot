"""Bounded, process-local observability for daemon SQLite storage operations."""

from __future__ import annotations

import logging
import threading
from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

    from pinky_daemon.store_catalog import StoreIntegrityTarget, StoreRecord
    from pinky_daemon.store_snapshot import SnapshotResult

logger = logging.getLogger("pinky.storage")


def _timestamp() -> str:
    return datetime.now(UTC).isoformat()


def emit_storage_event(event: str, /, **fields: str | int) -> None:
    """Write one machine-readable event containing only caller-selected fields."""
    rendered = ["storage_event", f"event={event}"]
    rendered.extend(f"{key}={value}" for key, value in fields.items())
    logger.info(" ".join(rendered))


class StorageObservability:
    """Current-boot metrics exposed through the existing watchdog endpoint."""

    def __init__(self, manifest: dict[str, StoreIntegrityTarget]) -> None:
        self._lock = threading.RLock()
        self._manifest = dict(manifest)
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
            }
