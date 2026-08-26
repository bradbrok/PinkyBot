"""Daemon-owned, catalog-selected consistent snapshots of SQLite stores."""

from __future__ import annotations

import os
import re
import sqlite3
import stat
import tempfile
import threading
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable, Literal

from pinky_daemon.store_catalog import StoreCatalog, StoreRecord

BACKUP_PAGES = 64
BACKUP_SLEEP_SECONDS = 0.01
_SNAPSHOT_LOCK = threading.Lock()
_SAFE_FILENAME = re.compile(r"[^a-zA-Z0-9_.-]+")


class StoreSnapshotError(RuntimeError):
    """Base error for daemon-owned store snapshots."""


class StoreSnapshotSelectionError(StoreSnapshotError):
    """Raised when a request cannot be resolved safely through the catalog."""


class StoreSnapshotVerificationError(StoreSnapshotError):
    """Raised when a completed copy fails its integrity or inode checks."""


@dataclass(frozen=True, slots=True)
class SnapshotResult:
    """One physical store copy, which may represent several logical stores."""

    logical_names: tuple[str, ...]
    snapshot_path: str | None
    verification: str
    error: StoreSnapshotError | None = None

    @property
    def logical_name(self) -> str:
        return self.logical_names[0]

    @property
    def status(self) -> Literal["published", "failed"]:
        if self.snapshot_path is not None and self.error is None:
            return "published"
        return "failed"

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "logical_name": self.logical_name,
            "logical_names": list(self.logical_names),
            "status": self.status,
        }
        if self.status == "published":
            payload["path"] = self.snapshot_path
        else:
            payload["error"] = str(self.error or "snapshot failed")
        return payload


@dataclass(frozen=True, slots=True)
class _SelectedStore:
    resolved_path: str
    records: tuple[StoreRecord, ...]

    @property
    def logical_names(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(record.logical_name for record in self.records))

    @property
    def dev_ino(self) -> tuple[int, int] | None:
        identities = {record.dev_ino for record in self.records if record.dev_ino is not None}
        if not identities:
            return None
        return next(iter(identities))


class StoreSnapshotService:
    """Produce verified copies without exposing live database files to callers."""

    def __init__(self, catalog: StoreCatalog) -> None:
        self._catalog = catalog

    def create_snapshots(
        self,
        logical_name: str | None = None,
        *,
        on_outcome: Callable[[SnapshotResult], None] | None = None,
    ) -> list[SnapshotResult]:
        """Snapshot one named store or every snapshot-recovery filesystem store.

        The global lock serializes backup work across all service instances. A
        single catalog snapshot is selected and grouped before any source is
        opened, so shared physical stores produce one copy with all associated
        logical names.
        """
        with _SNAPSHOT_LOCK:
            selected = self._select(logical_name)
            results = []
            for store in selected:
                result = self._snapshot_outcome(store)
                if on_outcome is not None:
                    self._report_outcome(result, on_outcome)
                results.append(result)
            return results

    def _snapshot_outcome(self, store: _SelectedStore) -> SnapshotResult:
        try:
            return self._snapshot_selected(store)
        except (StoreSnapshotError, sqlite3.Error, OSError) as exc:
            error = self._as_snapshot_error(store, exc)
            return SnapshotResult(
                logical_names=store.logical_names,
                snapshot_path=None,
                verification="failed",
                error=error,
            )

    @staticmethod
    def _as_snapshot_error(
        store: _SelectedStore,
        exc: Exception,
    ) -> StoreSnapshotError:
        if isinstance(exc, StoreSnapshotError):
            return exc
        error = StoreSnapshotError(
            f"{type(exc).__name__} while snapshotting {store.logical_names[0]!r}: {exc}"
        )
        error.__cause__ = exc
        return error

    @staticmethod
    def _report_outcome(
        result: SnapshotResult,
        on_outcome: Callable[[SnapshotResult], None],
    ) -> None:
        try:
            on_outcome(result)
        except Exception as exc:
            # A published path is not allowed to outlive a failed audit write.
            # This cleanup is only for audit infrastructure failure; a sibling
            # store's corruption never removes an already-audited good copy.
            if result.status == "published" and result.snapshot_path is not None:
                try:
                    Path(result.snapshot_path).unlink()
                except OSError as cleanup_exc:
                    raise StoreSnapshotError(
                        "snapshot audit failed and the unaudited artifact "
                        f"could not be removed: {result.snapshot_path!r}"
                    ) from cleanup_exc
            raise StoreSnapshotError(
                f"snapshot outcome audit failed for {result.logical_name!r}"
            ) from exc

    def _select(self, logical_name: str | None) -> list[_SelectedStore]:
        records = self._catalog.snapshot()
        if logical_name is not None:
            requested = [record for record in records if record.logical_name == logical_name]
            if not requested:
                raise StoreSnapshotSelectionError(
                    f"unregistered store logical_name={logical_name!r}"
                )
            if any(record.is_memory for record in requested):
                raise StoreSnapshotSelectionError(
                    f"in-memory store cannot be snapshotted: {logical_name!r}"
                )
            selected_paths = {record.resolved_path for record in requested}
            if len(selected_paths) != 1:
                raise StoreSnapshotSelectionError(
                    f"logical_name has divergent registered paths: {logical_name!r}"
                )
        else:
            requested = [
                record
                for record in records
                if record.recovery == "snapshot" and not record.is_memory
            ]
            selected_paths = {record.resolved_path for record in requested}

        # Once a physical path is selected, report every logical store the
        # catalog associates with it. This preserves shared-owner identities
        # such as sessions + session_events while still making only one copy.
        matching = [
            record
            for record in records
            if not record.is_memory and record.resolved_path in selected_paths
        ]

        grouped: dict[str, list[StoreRecord]] = {}
        for record in matching:
            self._validate_record_path(record)
            grouped.setdefault(record.resolved_path, []).append(record)
        for path, group in grouped.items():
            identities = {record.dev_ino for record in group if record.dev_ino is not None}
            if len(identities) > 1:
                raise StoreSnapshotSelectionError(
                    f"catalog records disagree on source identity: path={path!r}"
                )
        return [
            _SelectedStore(resolved_path=path, records=tuple(group))
            for path, group in grouped.items()
        ]

    def _validate_record_path(self, record: StoreRecord) -> None:
        root = self._catalog.expected_root
        registered_path = record.resolved_path
        if not os.path.isabs(registered_path) or not self._is_under_root(root, registered_path):
            raise StoreSnapshotSelectionError(
                f"registered store path is outside expected root {root!r}: "
                f"logical_name={record.logical_name!r} path={registered_path!r}"
            )
        current_realpath = os.path.realpath(registered_path)
        if current_realpath != registered_path:
            raise StoreSnapshotSelectionError(
                "registered store path no longer resolves to its catalog identity: "
                f"logical_name={record.logical_name!r} registered={registered_path!r} "
                f"current={current_realpath!r}"
            )
        if not self._is_under_root(root, current_realpath):
            raise StoreSnapshotSelectionError(
                f"registered store path resolves outside expected root {root!r}: "
                f"logical_name={record.logical_name!r} path={current_realpath!r}"
            )

    def _snapshot_selected(self, store: _SelectedStore) -> SnapshotResult:
        try:
            source_stat = os.stat(store.resolved_path)
        except FileNotFoundError:
            return SnapshotResult(
                logical_names=store.logical_names,
                snapshot_path=None,
                verification="skipped",
                error=StoreSnapshotError("source_missing"),
            )
        if not stat.S_ISREG(source_stat.st_mode):
            raise StoreSnapshotSelectionError(
                f"registered store is not a regular file: {store.resolved_path!r}"
            )
        source_identity = (source_stat.st_dev, source_stat.st_ino)
        if store.dev_ino is not None and source_identity != store.dev_ino:
            raise StoreSnapshotSelectionError(
                f"registered store identity changed before snapshot: {store.resolved_path!r}"
            )

        output_dir = self._snapshot_directory()
        stem = _SAFE_FILENAME.sub("_", store.logical_names[0]).strip("._-") or "store"
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
        unique = uuid.uuid4().hex
        final_path = output_dir / f"{stem}-{timestamp}-{unique}.db"
        descriptor, temp_name = tempfile.mkstemp(
            dir=output_dir,
            prefix=f".{stem}-",
            suffix=".tmp",
        )
        os.close(descriptor)
        temp_path = Path(temp_name)

        try:
            try:
                self._backup_and_verify(store.resolved_path, temp_path, source_identity)
            except (FileNotFoundError, sqlite3.OperationalError):
                if not os.path.exists(store.resolved_path):
                    return SnapshotResult(
                        logical_names=store.logical_names,
                        snapshot_path=None,
                        verification="skipped",
                        error=StoreSnapshotError("source_missing"),
                    )
                raise
            temp_stat = temp_path.stat()
            if (temp_stat.st_dev, temp_stat.st_ino) == (
                source_stat.st_dev,
                source_stat.st_ino,
            ):
                raise StoreSnapshotVerificationError(
                    f"snapshot aliases source inode: {store.resolved_path!r}"
                )
            os.replace(temp_path, final_path)
            return SnapshotResult(
                logical_names=store.logical_names,
                snapshot_path=str(final_path),
                verification="ok",
            )
        finally:
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass

    def _backup_and_verify(
        self,
        source_path: str,
        destination_path: Path,
        expected_identity: tuple[int, int],
    ) -> None:
        source: sqlite3.Connection | None = None
        destination: sqlite3.Connection | None = None
        try:
            # The source connection deliberately executes no SQL at all — in
            # particular, never PRAGMA journal_mode. The Online Backup API is
            # the only operation performed against the live registered store.
            self._assert_source_identity(source_path, expected_identity)
            source = sqlite3.connect(
                Path(source_path).as_uri() + "?mode=ro",
                uri=True,
                timeout=30,
            )
            self._assert_source_identity(source_path, expected_identity)
            destination = sqlite3.connect(destination_path, timeout=30)
            source.backup(
                destination,
                pages=BACKUP_PAGES,
                progress=self._backup_progress,
                sleep=BACKUP_SLEEP_SECONDS,
            )
            self._assert_source_identity(source_path, expected_identity)
            check_rows = destination.execute("PRAGMA quick_check").fetchall()
            if check_rows != [("ok",)]:
                raise StoreSnapshotVerificationError(f"snapshot quick_check failed: {check_rows!r}")
        finally:
            if destination is not None:
                destination.close()
            if source is not None:
                source.close()

    def _assert_source_identity(
        self,
        source_path: str,
        expected_identity: tuple[int, int],
    ) -> None:
        current_realpath = os.path.realpath(source_path)
        current_stat = os.stat(source_path)
        current_identity = (current_stat.st_dev, current_stat.st_ino)
        if (
            current_realpath != source_path
            or current_identity != expected_identity
            or not stat.S_ISREG(current_stat.st_mode)
        ):
            raise StoreSnapshotSelectionError(
                f"registered store identity changed during snapshot: {source_path!r}"
            )

    def _backup_progress(self, _status: int, _remaining: int, _total: int) -> None:
        """Backup progress seam for deterministic concurrency contract tests."""

    def _snapshot_directory(self) -> Path:
        root = self._catalog.expected_root
        directory = Path(root) / "snapshots"
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        resolved_directory = os.path.realpath(directory)
        expected_directory = os.path.join(root, "snapshots")
        if resolved_directory != expected_directory or not self._is_under_root(
            root, resolved_directory
        ):
            raise StoreSnapshotSelectionError(
                f"snapshot directory resolves outside expected root {root!r}: "
                f"path={resolved_directory!r}"
            )
        return Path(resolved_directory)

    @staticmethod
    def _is_under_root(root: str, path: str) -> bool:
        try:
            return os.path.commonpath((root, path)) == root
        except ValueError:
            return False
