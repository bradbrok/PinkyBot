"""Canonical inventory and boot-time validation for daemon SQLite stores."""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class StoreRecord:
    """One logical store's observed physical SQLite identity."""

    logical_name: str
    resolved_path: str
    journal_mode: str
    owner: str
    criticality: str
    dev_ino: tuple[int, int] | None


class StoreCatalogError(RuntimeError):
    """Raised when the daemon's store layout is unsafe or incoherent."""


@dataclass(slots=True)
class _CatalogEntry:
    record: StoreRecord
    used_relative_path: bool


class StoreCatalog:
    """Observe store connection policy and reject an incoherent boot layout."""

    def __init__(self, expected_root: str | os.PathLike[str] | None = None) -> None:
        root = os.getcwd() if expected_root is None else os.fspath(expected_root)
        self._expected_root = os.path.realpath(root)
        self._entries: list[_CatalogEntry] = []
        self._lock = threading.RLock()

    def register(
        self,
        logical_name: str,
        path: str | os.PathLike[str],
        *,
        journal_mode: str,
        owner: str,
        criticality: str = "authoritative",
    ) -> None:
        """Record one store connection, canonicalized to physical path identity.

        Repeated connection opens for the same logical name, owner, path, and
        policy refresh the existing record. Divergent declarations are retained
        so :meth:`validate` can report the complete conflict at the boot gate.
        """
        raw_path = os.fspath(path)
        resolved_path = os.path.realpath(raw_path)
        record = StoreRecord(
            logical_name=logical_name,
            resolved_path=resolved_path,
            journal_mode=journal_mode.lower(),
            owner=owner,
            criticality=criticality,
            dev_ino=self._stat_identity(resolved_path),
        )
        used_relative_path = not os.path.isabs(raw_path)

        with self._lock:
            for entry in self._entries:
                current = entry.record
                if (
                    current.logical_name == record.logical_name
                    and current.owner == record.owner
                    and current.resolved_path == record.resolved_path
                    and current.journal_mode == record.journal_mode
                    and current.criticality == record.criticality
                ):
                    entry.record = record
                    entry.used_relative_path = entry.used_relative_path or used_relative_path
                    return
            self._entries.append(
                _CatalogEntry(
                    record=record,
                    used_relative_path=used_relative_path,
                )
            )

    def validate(self) -> None:
        """Raise when registered stores do not form one coherent layout."""
        with self._lock:
            self._refresh_identities()
            entries = list(self._entries)

        violations: list[str] = []
        records = [entry.record for entry in entries]

        for entry in entries:
            record = entry.record
            if record.journal_mode == "memory":
                continue
            if entry.used_relative_path:
                violations.append("relative path input for " + self._format_record(record))
            if not os.path.isabs(record.resolved_path):
                violations.append("unresolved non-absolute path for " + self._format_record(record))
            if not self._is_under_expected_root(record.resolved_path):
                violations.append(
                    f"path is outside expected root {self._expected_root!r}: "
                    + self._format_record(record)
                )

        records_by_logical_name: dict[str, list[StoreRecord]] = {}
        for record in records:
            records_by_logical_name.setdefault(record.logical_name, []).append(record)
        for logical_name, matching_records in records_by_logical_name.items():
            if len({record.resolved_path for record in matching_records}) > 1:
                violations.append(
                    f"duplicate logical name {logical_name!r} has divergent paths: "
                    + "; ".join(self._format_record(record) for record in matching_records)
                )

        for index, left in enumerate(records):
            for right in records[index + 1 :]:
                if "memory" in {left.journal_mode, right.journal_mode}:
                    continue
                same_realpath = left.resolved_path == right.resolved_path
                same_inode = (
                    left.dev_ino is not None
                    and right.dev_ino is not None
                    and left.dev_ino == right.dev_ino
                )
                if not (same_realpath or same_inode):
                    continue
                pair = f"{self._format_record(left)}; {self._format_record(right)}"
                if left.journal_mode != right.journal_mode:
                    violations.append("journal mode mismatch on same physical file: " + pair)
                if left.logical_name != right.logical_name and left.owner != right.owner:
                    violations.append(
                        "different logical stores share the same physical file "
                        "without a shared owner identity: " + pair
                    )

        if violations:
            raise StoreCatalogError(
                "Store catalog validation failed:\n- " + "\n- ".join(violations)
            )

    def snapshot(self) -> list[StoreRecord]:
        """Return the current canonical records in registration order."""
        with self._lock:
            self._refresh_identities()
            return [entry.record for entry in self._entries]

    @staticmethod
    def _stat_identity(path: str) -> tuple[int, int] | None:
        try:
            stat = os.stat(path)
        except OSError:
            return None
        return (stat.st_dev, stat.st_ino)

    def _refresh_identities(self) -> None:
        for entry in self._entries:
            record = entry.record
            dev_ino = self._stat_identity(record.resolved_path)
            if dev_ino != record.dev_ino:
                entry.record = StoreRecord(
                    logical_name=record.logical_name,
                    resolved_path=record.resolved_path,
                    journal_mode=record.journal_mode,
                    owner=record.owner,
                    criticality=record.criticality,
                    dev_ino=dev_ino,
                )

    def _is_under_expected_root(self, path: str) -> bool:
        try:
            return os.path.commonpath((self._expected_root, path)) == self._expected_root
        except ValueError:
            return False

    @staticmethod
    def _format_record(record: StoreRecord) -> str:
        return (
            f"{record.logical_name!r} owner={record.owner!r} "
            f"path={record.resolved_path!r} journal_mode={record.journal_mode!r} "
            f"dev_ino={record.dev_ino!r}"
        )
