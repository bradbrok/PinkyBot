"""Canonical inventory and boot-time validation for daemon SQLite stores."""

from __future__ import annotations

import fnmatch
import logging
import os
import re
import stat
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import parse_qsl

logger = logging.getLogger("pinky.store_catalog")


DEFAULT_FILESYSTEM_SILENCE_ALLOWLIST: dict[str, str] = {
    "hub.db": "pinky_hub component store; not owned by the API daemon",
    "pinky.db": "poll-mode --mode daemon base; unused in API mode",
    "pinkybot.db": "legacy daemon base; not owned by the current API daemon",
    "daemon.db": "zero-byte legacy daemon orphan; pending operator cleanup",
    "messages.db": "zero-byte legacy message-store orphan; pending operator cleanup",
    "pinky_daemon.db": "zero-byte legacy API-daemon orphan; pending operator cleanup",
    "scheduler.db": "zero-byte legacy scheduler orphan; pending operator cleanup",
}

_SQLITE_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")
_IGNORED_DIRECTORY_KINDS = frozenset({"snapshot", "snapshots", "temp", "temporary", "tmp"})


@dataclass(frozen=True, slots=True)
class StoreRecord:
    """One logical store's observed physical SQLite identity."""

    logical_name: str
    resolved_path: str
    journal_mode: str
    owner: str
    criticality: str
    dev_ino: tuple[int, int] | None
    is_memory: bool


class StoreCatalogError(RuntimeError):
    """Raised when the daemon's store layout is unsafe or incoherent."""


@dataclass(slots=True)
class _CatalogEntry:
    record: StoreRecord
    used_relative_path: bool


@dataclass(frozen=True, slots=True)
class _FilesystemDatabase:
    relative_path: str
    resolved_path: str
    dev_ino: tuple[int, int]


class StoreCatalog:
    """Observe store connection policy and reject an incoherent boot layout."""

    def __init__(
        self,
        expected_root: str | os.PathLike[str] | None = None,
        *,
        silence_allowlist: Mapping[str, str] | None = None,
    ) -> None:
        root = os.getcwd() if expected_root is None else os.fspath(expected_root)
        self._expected_root = os.path.realpath(root)
        configured_allowlist = (
            DEFAULT_FILESYSTEM_SILENCE_ALLOWLIST if silence_allowlist is None else silence_allowlist
        )
        self._silence_allowlist = dict(configured_allowlist)
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
        is_memory = self._is_memory_path(raw_path)
        resolved_path = os.path.realpath(raw_path)
        record = StoreRecord(
            logical_name=logical_name,
            resolved_path=resolved_path,
            journal_mode=journal_mode.lower(),
            owner=owner,
            criticality=criticality,
            dev_ino=None if is_memory else self._stat_identity(resolved_path),
            is_memory=is_memory,
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
                    and current.is_memory == record.is_memory
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

    def validate(self) -> list[str]:
        """Raise for an unsafe layout and return filesystem hygiene warnings."""
        with self._lock:
            self._refresh_identities()
            entries = list(self._entries)

        violations: list[str] = []
        records = [entry.record for entry in entries]

        for entry in entries:
            record = entry.record
            if record.is_memory:
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
                if left.is_memory or right.is_memory:
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

        return self.reconcile_filesystem()

    def reconcile_filesystem(self) -> list[str]:
        """Reconcile registered stores with SQLite files found below the data root.

        A distinct path sharing a registered store's inode is unsafe and fails
        closed. Standalone unclaimed databases and stale warning suppressions are
        surfaced as warnings so inventory drift cannot remain invisible or brick
        an otherwise safe boot.
        """
        with self._lock:
            self._refresh_identities()
            records = [entry.record for entry in self._entries if not entry.record.is_memory]

        registered_realpaths = {record.resolved_path for record in records}
        registered_paths_by_inode: dict[tuple[int, int], set[str]] = {}
        for record in records:
            if record.dev_ino is not None:
                registered_paths_by_inode.setdefault(record.dev_ino, set()).add(
                    record.resolved_path
                )

        databases, warnings = self._enumerate_database_files()
        alias_violations: list[str] = []
        matched_allowlist_patterns: set[str] = set()

        for database in databases:
            matching_patterns = {
                pattern
                for pattern in self._silence_allowlist
                if self._allowlist_pattern_matches(pattern, database.relative_path)
            }
            matched_allowlist_patterns.update(matching_patterns)

            registered_inode_paths = registered_paths_by_inode.get(database.dev_ino, set())
            if registered_inode_paths and database.resolved_path not in registered_inode_paths:
                alias_violations.append(
                    "unregistered path aliases registered store inode: "
                    f"path={database.relative_path!r} resolved_path={database.resolved_path!r} "
                    f"dev_ino={database.dev_ino!r} "
                    f"registered_realpaths={sorted(registered_inode_paths)!r}"
                )
                continue

            if database.resolved_path in registered_realpaths or matching_patterns:
                continue

            warnings.append(
                "unclaimed database file: "
                f"path={database.relative_path!r} resolved_path={database.resolved_path!r} "
                f"dev_ino={database.dev_ino!r}"
            )

        if alias_violations:
            raise StoreCatalogError(
                "Store catalog filesystem reconciliation failed:\n- "
                + "\n- ".join(alias_violations)
            )

        for pattern, reason in self._silence_allowlist.items():
            if pattern not in matched_allowlist_patterns:
                warnings.append(
                    f"dead silence-allowlist entry {pattern!r} matched no database files "
                    f"(reason: {reason})"
                )

        for warning in warnings:
            logger.warning("STORE CATALOG WARNING: %s", warning)
        return warnings

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
            dev_ino = None if record.is_memory else self._stat_identity(record.resolved_path)
            if dev_ino != record.dev_ino:
                entry.record = StoreRecord(
                    logical_name=record.logical_name,
                    resolved_path=record.resolved_path,
                    journal_mode=record.journal_mode,
                    owner=record.owner,
                    criticality=record.criticality,
                    dev_ino=dev_ino,
                    is_memory=record.is_memory,
                )

    def _enumerate_database_files(self) -> tuple[list[_FilesystemDatabase], list[str]]:
        databases: list[_FilesystemDatabase] = []
        warnings: list[str] = []

        def record_walk_error(exc: OSError) -> None:
            warnings.append(
                "could not inspect database directory: "
                f"path={exc.filename!r} error={type(exc).__name__}: {exc}"
            )

        for directory, subdirectories, filenames in os.walk(
            self._expected_root,
            followlinks=False,
            onerror=record_walk_error,
        ):
            subdirectories[:] = sorted(
                name for name in subdirectories if not self._is_ignored_directory(name)
            )
            for filename in sorted(filenames):
                if not filename.endswith(".db") or filename.endswith(_SQLITE_SIDECAR_SUFFIXES):
                    continue
                discovered_path = os.path.join(directory, filename)
                relative_path = os.path.relpath(discovered_path, self._expected_root).replace(
                    os.sep, "/"
                )
                resolved_path = os.path.realpath(discovered_path)
                try:
                    file_stat = os.stat(resolved_path)
                except OSError as exc:
                    warnings.append(
                        "could not inspect database file: "
                        f"path={relative_path!r} error={type(exc).__name__}: {exc}"
                    )
                    continue
                if not stat.S_ISREG(file_stat.st_mode):
                    continue
                databases.append(
                    _FilesystemDatabase(
                        relative_path=relative_path,
                        resolved_path=resolved_path,
                        dev_ino=(file_stat.st_dev, file_stat.st_ino),
                    )
                )
        return databases, warnings

    @staticmethod
    def _is_ignored_directory(name: str) -> bool:
        tokens = [token for token in re.split(r"[._-]+", name.casefold()) if token]
        return any(token in _IGNORED_DIRECTORY_KINDS for token in tokens)

    @staticmethod
    def _allowlist_pattern_matches(pattern: str, relative_path: str) -> bool:
        normalized_pattern = pattern.replace(os.sep, "/")
        candidate = relative_path if "/" in normalized_pattern else os.path.basename(relative_path)
        return fnmatch.fnmatchcase(candidate, normalized_pattern)

    @staticmethod
    def _is_memory_path(raw_path: str) -> bool:
        if raw_path == ":memory:":
            return True
        if not raw_path.startswith("file:"):
            return False
        uri, _, _fragment = raw_path.partition("#")
        uri_path, separator, query = uri.partition("?")
        if uri_path == "file::memory:":
            return True
        if not separator:
            return False
        effective_mode: str | None = None
        for key, value in parse_qsl(query, keep_blank_values=True):
            if key == "mode":
                effective_mode = value
        return effective_mode == "memory"

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
            f"dev_ino={record.dev_ino!r} is_memory={record.is_memory!r}"
        )
