"""Canonical inventory and boot-time validation for daemon SQLite stores."""

from __future__ import annotations

import errno
import fnmatch
import logging
import os
import re
import sqlite3
import stat
import threading
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
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


@dataclass(frozen=True, slots=True)
class StoreIntegrityTarget:
    """One API-owned SQLite store that must pass the boot integrity gate."""

    logical_name: str
    path: str
    criticality: str = "authoritative"
    recovery: str = "snapshot"


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


class StoreReservation:
    """One catalog record reserved before its SQLite file is created.

    The entry is visible immediately. Call :meth:`commit` only after creation,
    durable commit/fsync, identity refresh, and catalog validation succeed.
    Leaving the context without committing removes the exact reserved entry.
    """

    def __init__(self, catalog: "StoreCatalog", entry: _CatalogEntry) -> None:
        self._catalog = catalog
        self._entry = entry
        self._committed = False

    def __enter__(self) -> "StoreReservation":
        return self

    def refresh(self) -> StoreRecord:
        """Refresh and return the reserved file's current physical identity."""
        return self._catalog._refresh_reservation(self._entry)

    def commit(self) -> None:
        """Retain the reservation as a permanent catalog record."""
        self._catalog._commit_reservation(self._entry)
        self._committed = True

    def __exit__(self, exc_type, exc, traceback) -> None:
        if not self._committed:
            self._catalog._rollback_reservation(self._entry)


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

    @property
    def expected_root(self) -> str:
        """Return the canonical filesystem root this catalog governs."""
        return self._expected_root

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

    def reserve(
        self,
        logical_name: str,
        path: str | os.PathLike[str],
        *,
        journal_mode: str,
        owner: str,
        criticality: str = "authoritative",
    ) -> StoreReservation:
        """Publish an absent store in the catalog before its exclusive create.

        Reservations never coalesce with existing records: a caller trying to
        reserve an already-declared logical/path/owner tuple is an ownership
        error, not an idempotent reopen. This keeps rollback scoped to the exact
        entry created by this call.
        """
        raw_path = os.fspath(path)
        is_memory = self._is_memory_path(raw_path)
        if is_memory:
            raise StoreCatalogError("cannot reserve an in-memory store")
        record = StoreRecord(
            logical_name=logical_name,
            resolved_path=os.path.realpath(raw_path),
            journal_mode=journal_mode.lower(),
            owner=owner,
            criticality=criticality,
            dev_ino=self._stat_identity(os.path.realpath(raw_path)),
            is_memory=False,
        )
        entry = _CatalogEntry(
            record=record,
            used_relative_path=not os.path.isabs(raw_path),
        )
        with self._lock:
            if any(
                current.record.logical_name == record.logical_name
                and current.record.resolved_path == record.resolved_path
                and current.record.owner == record.owner
                for current in self._entries
            ):
                raise StoreCatalogError(
                    "store reservation duplicates an existing catalog record: "
                    + self._format_record(record)
                )
            self._entries.append(entry)
        return StoreReservation(self, entry)

    def _refresh_reservation(self, entry: _CatalogEntry) -> StoreRecord:
        with self._lock:
            if entry not in self._entries:
                raise StoreCatalogError("store reservation is no longer active")
            record = entry.record
            dev_ino = self._stat_identity(record.resolved_path)
            entry.record = StoreRecord(
                logical_name=record.logical_name,
                resolved_path=record.resolved_path,
                journal_mode=record.journal_mode,
                owner=record.owner,
                criticality=record.criticality,
                dev_ino=dev_ino,
                is_memory=False,
            )
            return entry.record

    def _commit_reservation(self, entry: _CatalogEntry) -> None:
        with self._lock:
            if entry not in self._entries:
                raise StoreCatalogError("store reservation is no longer active")
            if entry.record.dev_ino is None:
                raise StoreCatalogError("reserved store has no physical identity")

    def _rollback_reservation(self, entry: _CatalogEntry) -> None:
        with self._lock:
            try:
                self._entries.remove(entry)
            except ValueError:
                pass

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

    def preflight_integrity(
        self,
        targets: Iterable[StoreIntegrityTarget],
        *,
        on_outcome: Callable[[str, str], None] | None = None,
    ) -> dict[str, str]:
        """Fail closed when an existing API-owned SQLite file is corrupt.

        Targets that share a physical path are checked once and reported together.
        Missing and in-memory stores are left for their constructors to create.
        """
        targets_by_path: dict[str, list[StoreIntegrityTarget]] = {}
        outcomes: dict[str, str] = {}
        for target in targets:
            raw_path = os.fspath(target.path)
            if self._is_memory_path(raw_path):
                continue
            resolved_path = os.path.realpath(raw_path)
            targets_by_path.setdefault(resolved_path, []).append(target)

        for resolved_path, matching_targets in targets_by_path.items():
            try:
                os.stat(resolved_path)
            except OSError as exc:
                if self._is_missing_path_error(exc):
                    self._record_integrity_outcomes(
                        matching_targets,
                        "skipped-absent",
                        outcomes,
                        on_outcome,
                    )
                    continue
                self._record_integrity_outcomes(
                    matching_targets,
                    "failed-corrupt",
                    outcomes,
                    on_outcome,
                )
                self._raise_integrity_error(matching_targets, resolved_path, exc)

            try:
                connection = sqlite3.connect(
                    Path(resolved_path).as_uri() + "?mode=ro",
                    uri=True,
                )
                try:
                    rows = connection.execute("PRAGMA quick_check").fetchall()
                finally:
                    connection.close()
            except (sqlite3.Error, OSError) as exc:
                if self._path_is_absent(resolved_path):
                    self._record_integrity_outcomes(
                        matching_targets,
                        "skipped-absent",
                        outcomes,
                        on_outcome,
                    )
                    continue
                self._record_integrity_outcomes(
                    matching_targets,
                    "failed-corrupt",
                    outcomes,
                    on_outcome,
                )
                self._raise_integrity_error(matching_targets, resolved_path, exc)

            if rows != [("ok",)]:
                self._record_integrity_outcomes(
                    matching_targets,
                    "failed-corrupt",
                    outcomes,
                    on_outcome,
                )
                self._raise_integrity_error(
                    matching_targets,
                    resolved_path,
                    f"PRAGMA quick_check returned {rows!r}",
                )
            self._record_integrity_outcomes(
                matching_targets,
                "ok",
                outcomes,
                on_outcome,
            )

        return outcomes

    @staticmethod
    def _record_integrity_outcomes(
        targets: list[StoreIntegrityTarget],
        outcome: str,
        outcomes: dict[str, str],
        on_outcome: Callable[[str, str], None] | None,
    ) -> None:
        for target in targets:
            outcomes[target.logical_name] = outcome
            if on_outcome is not None:
                on_outcome(target.logical_name, outcome)

    @staticmethod
    def _raise_integrity_error(
        targets: list[StoreIntegrityTarget],
        resolved_path: str,
        detail: BaseException | str,
    ) -> None:
        logical_names = ", ".join(target.logical_name for target in targets)
        if isinstance(detail, BaseException):
            rendered_detail = f"{type(detail).__name__}: {detail}"
        else:
            rendered_detail = detail

        if any(
            target.criticality == "authoritative" or target.recovery == "snapshot"
            for target in targets
        ):
            recovery = (
                "Restore the authoritative store from a P0.3 snapshot or backup before restarting."
            )
        else:
            recovery = "Delete the derived store so it can rebuild before restarting."

        error = StoreCatalogError(
            "Store integrity preflight failed: "
            f"logical_stores=[{logical_names}] path={resolved_path!r} "
            f"error={rendered_detail}. {recovery}"
        )
        if isinstance(detail, BaseException):
            raise error from detail
        raise error

    def reconcile_filesystem(self) -> list[str]:
        """Reconcile registered stores with SQLite files found below the data root.

        A distinct path sharing a registered store's inode is unsafe and fails
        closed. Inspection failures within a registered store's path footprint
        also fail closed; unrelated unreadable paths warn instead. Standalone
        unclaimed databases and stale warning suppressions are surfaced as
        warnings so inventory drift cannot remain invisible or brick an otherwise
        safe boot.
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

        databases, warnings = self._enumerate_database_files(registered_realpaths)
        alias_violations: list[str] = []
        matched_allowlist_patterns: set[str] = set()

        for database in databases:
            registered_inode_paths = registered_paths_by_inode.get(database.dev_ino, set())
            if registered_inode_paths and database.resolved_path not in registered_inode_paths:
                alias_violations.append(
                    "unregistered path aliases registered store inode: "
                    f"path={database.relative_path!r} resolved_path={database.resolved_path!r} "
                    f"dev_ino={database.dev_ino!r} "
                    f"registered_realpaths={sorted(registered_inode_paths)!r}"
                )
                continue

            if database.resolved_path in registered_realpaths or self._is_in_ignored_directory(
                database.resolved_path
            ):
                continue

            matching_patterns = {
                pattern
                for pattern in self._silence_allowlist
                if self._allowlist_pattern_matches(pattern, database.relative_path)
            }
            matched_allowlist_patterns.update(matching_patterns)
            if matching_patterns:
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

    @classmethod
    def _path_is_absent(cls, path: str) -> bool:
        try:
            os.stat(path)
        except OSError as exc:
            return cls._is_missing_path_error(exc)
        return False

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

    def _enumerate_database_files(
        self, registered_realpaths: set[str]
    ) -> tuple[list[_FilesystemDatabase], list[str]]:
        databases: list[_FilesystemDatabase] = []
        warnings: list[str] = []
        pending_directories = [self._expected_root]
        # Alias-coverage cycle guard only; warning policy is derived from each file realpath.
        visited_directories: set[tuple[int, int]] = set()

        while pending_directories:
            directory = pending_directories.pop()
            try:
                resolved_directory = os.path.realpath(directory)
            except OSError as exc:
                if self._is_missing_path_error(exc):
                    continue
                self._warn_or_raise_inspection_error(
                    "directory",
                    directory,
                    exc,
                    footprint_path=os.path.abspath(directory),
                    registered_realpaths=registered_realpaths,
                    warnings=warnings,
                )
                continue
            try:
                directory_stat = os.stat(resolved_directory)
            except OSError as exc:
                if self._is_missing_path_error(exc):
                    continue
                self._warn_or_raise_inspection_error(
                    "directory",
                    directory,
                    exc,
                    footprint_path=resolved_directory,
                    registered_realpaths=registered_realpaths,
                    warnings=warnings,
                )
                continue
            if not stat.S_ISDIR(directory_stat.st_mode):
                database = self._filesystem_database_for_path(
                    directory, resolved_directory, directory_stat
                )
                if database is not None:
                    databases.append(database)
                continue
            if not self._is_under_expected_root(resolved_directory):
                raise StoreCatalogError(
                    "Store catalog filesystem alias coverage failed:\n- "
                    f"directory symlink escapes expected root {self._expected_root!r}: "
                    f"path={directory!r} resolved_path={resolved_directory!r}"
                )
            directory_identity = (directory_stat.st_dev, directory_stat.st_ino)
            if directory_identity in visited_directories:
                continue
            visited_directories.add(directory_identity)

            try:
                with os.scandir(resolved_directory) as iterator:
                    entries = sorted(iterator, key=lambda entry: entry.name)
            except OSError as exc:
                if self._is_missing_path_error(exc):
                    continue
                self._warn_or_raise_inspection_error(
                    "directory",
                    directory,
                    exc,
                    footprint_path=resolved_directory,
                    registered_realpaths=registered_realpaths,
                    warnings=warnings,
                )
                continue

            child_directories: list[str] = []
            for entry in entries:
                discovered_path = entry.path
                try:
                    resolved_path = os.path.realpath(discovered_path)
                except OSError as exc:
                    if self._is_missing_path_error(exc):
                        continue
                    self._warn_or_raise_inspection_error(
                        "file or directory",
                        discovered_path,
                        exc,
                        footprint_path=os.path.abspath(discovered_path),
                        registered_realpaths=registered_realpaths,
                        warnings=warnings,
                    )
                    continue
                try:
                    entry_stat = os.stat(resolved_path)
                except OSError as exc:
                    if self._is_missing_path_error(exc):
                        continue
                    self._warn_or_raise_inspection_error(
                        "file or directory",
                        discovered_path,
                        exc,
                        footprint_path=resolved_path,
                        registered_realpaths=registered_realpaths,
                        warnings=warnings,
                    )
                    continue

                if stat.S_ISDIR(entry_stat.st_mode):
                    if not self._is_under_expected_root(resolved_path):
                        raise StoreCatalogError(
                            "Store catalog filesystem alias coverage failed:\n- "
                            f"directory symlink escapes expected root {self._expected_root!r}: "
                            f"path={discovered_path!r} resolved_path={resolved_path!r}"
                        )
                    child_directories.append(discovered_path)
                    continue

                database = self._filesystem_database_for_path(
                    discovered_path, resolved_path, entry_stat
                )
                if database is not None:
                    databases.append(database)
            pending_directories.extend(reversed(child_directories))
        return databases, warnings

    def _filesystem_database_for_path(
        self,
        discovered_path: str,
        resolved_path: str,
        path_stat: os.stat_result,
    ) -> _FilesystemDatabase | None:
        name = os.path.basename(discovered_path)
        if not name.endswith(".db") or name.endswith(_SQLITE_SIDECAR_SUFFIXES):
            return None
        if not self._is_under_expected_root(resolved_path):
            raise StoreCatalogError(
                "Store catalog filesystem alias coverage failed:\n- "
                f"database symlink escapes expected root {self._expected_root!r}: "
                f"path={discovered_path!r} resolved_path={resolved_path!r}"
            )
        if not stat.S_ISREG(path_stat.st_mode):
            return None
        relative_path = os.path.relpath(resolved_path, self._expected_root).replace(os.sep, "/")
        return _FilesystemDatabase(
            relative_path=relative_path,
            resolved_path=resolved_path,
            dev_ino=(path_stat.st_dev, path_stat.st_ino),
        )

    def _warn_or_raise_inspection_error(
        self,
        kind: str,
        path: str,
        exc: OSError,
        *,
        footprint_path: str,
        registered_realpaths: set[str],
        warnings: list[str],
    ) -> None:
        if self._registered_store_at_or_under(footprint_path, registered_realpaths):
            self._raise_inspection_error(kind, path, exc)
        warnings.append(
            f"could not inspect database {kind} outside registered store footprint; "
            f"skipping: path={path!r} error={type(exc).__name__}: {exc}"
        )

    @staticmethod
    def _registered_store_at_or_under(path: str, registered_realpaths: set[str]) -> bool:
        normalized_path = os.path.normpath(path)
        path_prefix = (
            normalized_path if normalized_path.endswith(os.sep) else normalized_path + os.sep
        )
        return any(
            store_path == normalized_path or store_path.startswith(path_prefix)
            for store_path in registered_realpaths
        )

    @staticmethod
    def _raise_inspection_error(kind: str, path: str, exc: OSError) -> None:
        raise StoreCatalogError(
            "Store catalog filesystem alias coverage failed:\n- "
            f"could not inspect database {kind}: path={path!r} "
            f"error={type(exc).__name__}: {exc}"
        ) from exc

    @staticmethod
    def _is_missing_path_error(exc: OSError) -> bool:
        return isinstance(exc, FileNotFoundError) or exc.errno == errno.ENOENT

    @staticmethod
    def _is_ignored_directory(name: str) -> bool:
        tokens = [token for token in re.split(r"[._-]+", name.casefold()) if token]
        return any(token in _IGNORED_DIRECTORY_KINDS for token in tokens)

    def _is_in_ignored_directory(self, resolved_path: str) -> bool:
        relative_path = os.path.relpath(resolved_path, self._expected_root)
        directory_names = relative_path.split(os.sep)[:-1]
        return any(self._is_ignored_directory(name) for name in directory_names)

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
