"""Canonical inventory and boot-time validation for daemon SQLite stores."""

from __future__ import annotations

import errno
import fnmatch
import logging
import os
import re
import sqlite3
import stat
import sys
import threading
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qsl

from pinky_daemon.store_shutdown import StoreShutdownCoordinator, StoreShutdownReport

if TYPE_CHECKING:
    from pinky_daemon.storage_observability import RuntimeOperation, StorageObservability

logger = logging.getLogger("pinky.store_catalog")

_THIRTY_SECOND_BUSY_STORES = frozenset(
    {
        "sessions",
        "session_events",
        "conversations",
        "audit",
        "agent_comms",
        "activity",
        "dream_state",
        "skills",
        "outreach_config",
        "tasks",
        "research",
        "presentations",
        "triggers",
        "voice",
        "user_profiles",
    }
)
_CRITICALITY_RANK = {
    "telemetry": 0,
    "derived": 0,
    "memory": 1,
    "authority": 2,
    "authoritative": 2,
    "delivery": 3,
}
_SQLITE_DEFAULT_TIMEOUT_SECONDS = 5.0


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
_SQLITE_HEADER_PREFIX = b"SQLite format 3\x00"
_SQLITE_HEADER_JOURNAL_BYTES = slice(18, 20)
_READ_ONLY_SQL_TOKENS = frozenset({"EXPLAIN", "PRAGMA", "SELECT", "VALUES"})
_ROLLBACK_SQL_TOKENS = frozenset({"ROLLBACK"})


def _first_sql_token(statement: str) -> str:
    """Classify from at most 64 bytes of SQL without a regex or full-string scan."""
    prefix = statement[:64]
    index = 0
    while index < len(prefix):
        while index < len(prefix) and prefix[index].isspace():
            index += 1
        if prefix.startswith("--", index):
            newline = prefix.find("\n", index + 2)
            if newline < 0:
                return ""
            index = newline + 1
            continue
        if prefix.startswith("/*", index):
            comment_end = prefix.find("*/", index + 2)
            if comment_end < 0:
                return ""
            index = comment_end + 2
            continue
        break
    token_start = index
    while index < len(prefix) and (prefix[index].isalnum() or prefix[index] == "_"):
        index += 1
    return prefix[token_start:index].upper()


def _sql_is_lock_bearing(statement: str, token: str) -> bool:
    """Classify bounded SQL, including assignment-form mutating PRAGMAs."""
    if token != "PRAGMA":
        return token not in _READ_ONLY_SQL_TOKENS
    return "=" in statement


def _sqlite_header_journal_mode(header: bytes) -> str | None:
    if len(header) < 20 or not header.startswith(_SQLITE_HEADER_PREFIX):
        return None
    journal_bytes = header[_SQLITE_HEADER_JOURNAL_BYTES]
    if b"\x02" in journal_bytes:
        return "wal"
    if journal_bytes == b"\x01\x01":
        return "rollback"
    return None


class BoundSQLiteFile:
    """One immutable, no-follow file descriptor used for SQLite inspection.

    The descriptor is the sole physical identity. Path reconciliation is a
    reject-only detector: it can report that the directory entry disappeared or
    changed, but it can never refresh this handle to a replacement inode.
    """

    def __init__(self, path: str | os.PathLike[str], descriptor: int) -> None:
        self.path = os.path.abspath(os.fspath(path))
        self._descriptor = descriptor
        try:
            descriptor_stat = os.fstat(descriptor)
        except BaseException:
            os.close(descriptor)
            self._descriptor = -1
            raise
        if not stat.S_ISREG(descriptor_stat.st_mode):
            os.close(descriptor)
            self._descriptor = -1
            raise OSError("SQLite path is not a regular file")
        self.dev_ino = (descriptor_stat.st_dev, descriptor_stat.st_ino)

    @classmethod
    def open(cls, path: str | os.PathLike[str]) -> "BoundSQLiteFile":
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(os.path.abspath(os.fspath(path)), flags)
        return cls(path, descriptor)

    @classmethod
    def from_descriptor(
        cls,
        path: str | os.PathLike[str],
        descriptor: int,
    ) -> "BoundSQLiteFile":
        """Take ownership of an already-open regular-file descriptor."""
        return cls(path, descriptor)

    @property
    def descriptor(self) -> int:
        if self._descriptor < 0:
            raise OSError("bound SQLite descriptor is closed")
        return self._descriptor

    def header_journal_mode(self) -> str | None:
        if hasattr(os, "pread"):
            header = os.pread(self.descriptor, 20, 0)
        else:  # pragma: no cover - Python 3.11 Unix hosts expose pread
            os.lseek(self.descriptor, 0, os.SEEK_SET)
            header = os.read(self.descriptor, 20)
        return _sqlite_header_journal_mode(header)

    def connect_read_only(self, *, timeout: float = 5) -> sqlite3.Connection:
        if sys.platform == "linux":
            descriptor_path = Path(f"/proc/self/fd/{self.descriptor}")
        elif sys.platform == "darwin":
            descriptor_path = Path(f"/dev/fd/{self.descriptor}")
        else:  # pragma: no cover - Pinky daemon supports Linux and macOS
            raise OSError(f"no descriptor-backed SQLite adapter for {sys.platform!r}")
        return sqlite3.connect(
            descriptor_path.as_uri() + "?mode=ro",
            uri=True,
            timeout=timeout,
        )

    def path_state(self) -> str:
        """Return ``same``, ``absent``, or ``changed`` without opening SQLite."""
        try:
            path_stat = os.stat(self.path, follow_symlinks=False)
        except OSError as exc:
            if StoreCatalog._is_missing_path_error(exc):
                return "absent"
            raise
        observed_identity = (path_stat.st_dev, path_stat.st_ino)
        if not stat.S_ISREG(path_stat.st_mode) or observed_identity != self.dev_ino:
            return "changed"
        return "same"

    def require_path_unchanged(self) -> None:
        state = self.path_state()
        if state == "absent":
            raise FileNotFoundError(f"bound SQLite path disappeared: {self.path!r}")
        if state != "same":
            raise OSError(f"bound SQLite path changed physical identity: {self.path!r}")

    def close(self) -> None:
        if self._descriptor >= 0:
            os.close(self._descriptor)
            self._descriptor = -1

    def __enter__(self) -> "BoundSQLiteFile":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()


@dataclass(slots=True)
class StoreObservation:
    """A preflight result whose physical identity stays pinned by a live fd."""

    logical_names: tuple[str, ...]
    path: str
    outcome: str
    journal_mode: str | None
    _bound_file: BoundSQLiteFile | None
    _claimed: bool = False

    @classmethod
    def absent(
        cls,
        logical_names: tuple[str, ...],
        path: str | os.PathLike[str],
    ) -> "StoreObservation":
        return cls(
            logical_names=logical_names,
            path=os.path.abspath(os.fspath(path)),
            outcome="skipped-absent",
            journal_mode=None,
            _bound_file=None,
        )

    @classmethod
    def from_descriptor(
        cls,
        logical_name: str,
        path: str | os.PathLike[str],
        *,
        journal_mode: str,
        descriptor: int,
    ) -> "StoreObservation":
        return cls(
            logical_names=(logical_name,),
            path=os.path.abspath(os.fspath(path)),
            outcome="ok",
            journal_mode=journal_mode.lower(),
            _bound_file=BoundSQLiteFile.from_descriptor(path, descriptor),
        )

    @property
    def dev_ino(self) -> tuple[int, int] | None:
        return None if self._bound_file is None else self._bound_file.dev_ino

    def require_path_unchanged(self) -> None:
        if self._bound_file is not None:
            self._bound_file.require_path_unchanged()
            return
        try:
            os.stat(self.path, follow_symlinks=False)
        except OSError as exc:
            if StoreCatalog._is_missing_path_error(exc):
                return
            raise
        raise OSError(f"preflight-absent SQLite path appeared: {self.path!r}")

    def close(self) -> None:
        if self._bound_file is not None:
            self._bound_file.close()


def sqlite_header_journal_mode(path: str | os.PathLike[str]) -> str | None:
    """Inspect persistent WAL/rollback state through one pinned descriptor.

    SQLite stores the database read/write format at header offsets 18 and 19.
    Reading those bytes through a no-follow file descriptor cannot create
    journal sidecars, unlike opening a persistent-WAL database with mode=ro.
    """
    with BoundSQLiteFile.open(path) as bound_file:
        journal_mode = bound_file.header_journal_mode()
        bound_file.require_path_unchanged()
        return journal_mode


@dataclass(frozen=True, slots=True)
class StoreConnectionPolicy:
    """One logical store's centrally declared SQLite open/retry policy."""

    busy_timeout_ms: int = 5_000
    rollback_retries: int = 6
    rollback_retry_delay_seconds: float = 0.2


def default_store_connection_policy(logical_name: str) -> StoreConnectionPolicy:
    """Return the behavior-preserving policy for a logical daemon store."""
    busy_timeout_ms = 30_000 if logical_name in _THIRTY_SECOND_BUSY_STORES else 5_000
    return StoreConnectionPolicy(busy_timeout_ms=busy_timeout_ms)


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
    recovery: str = "snapshot"
    connection_policy: StoreConnectionPolicy = field(default_factory=StoreConnectionPolicy)


@dataclass(frozen=True, slots=True)
class StoreIntegrityTarget:
    """One API-owned SQLite store that must pass the boot integrity gate."""

    logical_name: str
    path: str
    criticality: str = "authoritative"
    recovery: str = "snapshot"
    journal_mode: str | None = None
    connection_policy: StoreConnectionPolicy | None = None

    def __post_init__(self) -> None:
        if self.connection_policy is None:
            object.__setattr__(
                self,
                "connection_policy",
                default_store_connection_policy(self.logical_name),
            )


class StoreCatalogError(RuntimeError):
    """Raised when the daemon's store layout is unsafe or incoherent."""


class _ManagedSQLiteConnection(sqlite3.Connection):
    """Catalog connection with optional synchronous runtime recording."""

    _store_authority: _StoreConnectionAuthority | None = None
    _store_handle_id: int | None = None
    _store_logical_name: str | None = None
    _store_observability: StorageObservability | None = None
    _store_transaction_started_ns: int | None = None
    _store_boundary_observation_depth = 0
    _store_closed = False

    @property
    def in_transaction(self) -> bool:
        """Preserve the final false state for post-close ledger inspection."""
        if self._store_closed:
            return False
        descriptor = sqlite3.Connection.in_transaction
        return bool(descriptor.__get__(self, type(self)))

    def execute(
        self,
        sql: str,
        parameters: Any = (),
        /,
    ) -> sqlite3.Cursor:
        execute = super().execute
        return self._run_observed_statement(sql, lambda: execute(sql, parameters))

    def executemany(
        self,
        sql: str,
        seq_of_parameters: Iterable[Any],
        /,
    ) -> sqlite3.Cursor:
        executemany = super().executemany
        return self._run_observed_statement(
            sql,
            lambda: executemany(sql, seq_of_parameters),
        )

    def executescript(self, sql_script: str, /) -> sqlite3.Cursor:
        executescript = super().executescript
        return self._run_observed_statement(sql_script, lambda: executescript(sql_script))

    def commit(self) -> None:
        commit = super().commit
        if self._store_boundary_observation_depth:
            return commit()
        self._run_observed_transaction_boundary(commit, sql_token="COMMIT")

    def rollback(self) -> None:
        rollback = super().rollback
        if self._store_boundary_observation_depth:
            return rollback()
        self._run_observed_transaction_boundary(rollback, sql_token="ROLLBACK")

    def __enter__(self) -> _ManagedSQLiteConnection:
        super().__enter__()
        return self

    def __exit__(self, exc_type, exc, traceback) -> bool:
        exit_context = super().__exit__
        if self._store_boundary_observation_depth:
            return bool(exit_context(exc_type, exc, traceback))
        suppressed = self._run_observed_transaction_boundary(
            lambda: exit_context(exc_type, exc, traceback),
            sql_token="COMMIT" if exc_type is None else "ROLLBACK",
        )
        return bool(suppressed)

    def _run_observed_transaction_boundary(
        self,
        call: Callable[[], Any],
        *,
        sql_token: str,
    ) -> Any:
        observability = self._store_observability
        logical_name = self._store_logical_name
        transaction_before = self.in_transaction
        if observability is None or logical_name is None or not transaction_before:
            return call()
        operation = observability.begin_runtime_operation(
            logical_name,
            lock_bearing=sql_token not in _ROLLBACK_SQL_TOKENS,
        )
        if operation is None:
            return call()
        if self._store_transaction_started_ns is None:
            self._store_transaction_started_ns = operation.started_ns

        self._store_boundary_observation_depth += 1
        try:
            result = call()
        except BaseException as exc:
            primary_error_code = None
            if isinstance(exc, sqlite3.Error):
                error_code = getattr(exc, "sqlite_errorcode", None)
                if error_code is not None:
                    primary_error_code = int(error_code) & 0xFF
            finished_ns = observability.finish_runtime_operation(
                operation,
                succeeded=False,
                sqlite_primary_error_code=primary_error_code,
            )
            self._record_transaction_transition(
                operation,
                finished_ns,
                sql_token,
                transaction_before=transaction_before,
                transaction_after=self.in_transaction,
                succeeded=False,
            )
            raise
        finally:
            self._store_boundary_observation_depth -= 1

        finished_ns = observability.finish_runtime_operation(operation, succeeded=True)
        self._record_transaction_transition(
            operation,
            finished_ns,
            sql_token,
            transaction_before=transaction_before,
            transaction_after=self.in_transaction,
            succeeded=True,
        )
        return result

    def _run_observed_statement(
        self,
        statement: str,
        call: Callable[[], sqlite3.Cursor],
    ) -> sqlite3.Cursor:
        observability = self._store_observability
        logical_name = self._store_logical_name
        if observability is None or logical_name is None:
            return call()
        token = _first_sql_token(statement)
        operation = observability.begin_runtime_operation(
            logical_name,
            lock_bearing=_sql_is_lock_bearing(statement, token),
        )
        if operation is None:
            return call()

        transaction_before = self.in_transaction
        try:
            cursor = call()
        except BaseException as exc:
            primary_error_code = None
            if isinstance(exc, sqlite3.Error):
                error_code = getattr(exc, "sqlite_errorcode", None)
                if error_code is not None:
                    primary_error_code = int(error_code) & 0xFF
            finished_ns = observability.finish_runtime_operation(
                operation,
                succeeded=False,
                sqlite_primary_error_code=primary_error_code,
            )
            self._record_transaction_transition(
                operation,
                finished_ns,
                token,
                transaction_before=transaction_before,
                transaction_after=self.in_transaction,
                succeeded=False,
            )
            raise

        finished_ns = observability.finish_runtime_operation(operation, succeeded=True)
        self._record_transaction_transition(
            operation,
            finished_ns,
            token,
            transaction_before=transaction_before,
            transaction_after=self.in_transaction,
            succeeded=True,
        )
        return cursor

    def _record_transaction_transition(
        self,
        operation: RuntimeOperation,
        finished_ns: int,
        sql_token: str,
        *,
        transaction_before: bool,
        transaction_after: bool,
        succeeded: bool,
    ) -> None:
        observability = self._store_observability
        logical_name = self._store_logical_name
        if observability is None or logical_name is None:
            return
        if not transaction_before and transaction_after:
            self._store_transaction_started_ns = operation.started_ns
            return
        if transaction_before and not transaction_after:
            started_ns = self._store_transaction_started_ns
            if started_ns is not None:
                outcome = (
                    "rolled_back"
                    if sql_token in _ROLLBACK_SQL_TOKENS or not succeeded
                    else "committed"
                )
                observability.record_transaction_duration(
                    logical_name,
                    started_ns,
                    finished_ns,
                    outcome=outcome,
                )
            self._store_transaction_started_ns = None
            return
        if (
            not transaction_before
            and not transaction_after
            and succeeded
            and operation.lock_bearing
        ):
            observability.record_transaction_duration(
                logical_name,
                operation.started_ns,
                finished_ns,
            )

    def close(self) -> None:
        transaction_active = self.in_transaction
        observability = self._store_observability
        logical_name = self._store_logical_name
        started_ns = self._store_transaction_started_ns
        if (
            transaction_active
            and started_ns is None
            and observability is not None
            and logical_name is not None
        ):
            started_ns = observability.start_transaction()
        self._store_boundary_observation_depth += 1
        try:
            super().close()
        finally:
            self._store_boundary_observation_depth -= 1
        self._store_closed = True
        try:
            if (
                transaction_active
                and observability is not None
                and logical_name is not None
                and started_ns is not None
            ):
                observability.finish_transaction(
                    logical_name,
                    started_ns,
                    outcome="rolled_back",
                )
        finally:
            self._store_transaction_started_ns = None
            authority = self._store_authority
            handle_id = self._store_handle_id
            if authority is not None and handle_id is not None:
                authority._forget(handle_id)
                self._store_authority = None
                self._store_handle_id = None


@dataclass(slots=True)
class _ManagedConnection:
    handle_id: int
    logical_name: str
    path: str
    owner: str
    connection: _ManagedSQLiteConnection
    sequence: int
    journal_mode: str | None


class _StoreConnectionAuthority:
    """Open and retain every live catalog-owned SQLite connection."""

    def __init__(self, catalog: StoreCatalog) -> None:
        self._catalog = catalog
        self._lock = threading.RLock()
        self._handles: dict[int, _ManagedConnection] = {}
        self._next_handle_id = 1
        self._accepting = True

    def open(
        self,
        logical_name: str,
        database: Any,
        *,
        owner: str,
        **kwargs: Any,
    ) -> sqlite3.Connection:
        policy = self._catalog.connection_policy(logical_name)
        requested_timeout = kwargs.pop("timeout", None)
        timeout_seconds = policy.busy_timeout_ms / 1_000
        if requested_timeout is not None and float(requested_timeout) != timeout_seconds:
            raise StoreCatalogError(
                "connection timeout override diverges from catalog policy: "
                f"logical_store={logical_name!r} requested={requested_timeout!r} "
                f"declared={timeout_seconds!r}"
            )
        if "factory" in kwargs:
            raise StoreCatalogError("catalog-owned store connections cannot override the factory")

        # Catalog-owned handles must be finalizable by the single shutdown
        # coordinator after request worker threads have quiesced. Stores retain
        # their existing thread-local access discipline; only the sqlite runtime
        # affinity check is relaxed for central close.
        kwargs["check_same_thread"] = False
        kwargs["timeout"] = (
            _SQLITE_DEFAULT_TIMEOUT_SECONDS
            if requested_timeout is None
            else float(requested_timeout)
        )
        kwargs["factory"] = _ManagedSQLiteConnection
        with self._lock:
            if not self._accepting:
                raise StoreCatalogError(
                    f"store connection opened after shutdown began: {logical_name!r}"
                )
            connection = sqlite3.connect(database, **kwargs)
            try:
                apply_store_connection_policy(connection, policy)
            except BaseException:
                connection.close()
                raise
            handle_id = self._next_handle_id
            self._next_handle_id += 1
            target = self._catalog.manifest_target(logical_name)
            path = os.path.realpath(os.fspath(target.path if target is not None else database))
            handle = _ManagedConnection(
                handle_id=handle_id,
                logical_name=logical_name,
                path=path,
                owner=owner,
                connection=connection,
                sequence=handle_id,
                journal_mode=None if target is None else target.journal_mode,
            )
            connection._store_authority = self
            connection._store_handle_id = handle_id
            connection._store_logical_name = logical_name
            connection._store_observability = self._catalog._observability
            self._handles[handle_id] = handle
            return connection

    def update_registration(self, record: StoreRecord) -> None:
        with self._lock:
            for handle in self._handles.values():
                if (
                    handle.logical_name == record.logical_name
                    and handle.path == record.resolved_path
                    and handle.owner == record.owner
                ):
                    handle.journal_mode = record.journal_mode

    def _forget(self, handle_id: int) -> None:
        with self._lock:
            self._handles.pop(handle_id, None)

    def shutdown(self, *, deadline_seconds: float) -> StoreShutdownReport:
        with self._lock:
            self._accepting = False
            handles = sorted(self._handles.values(), key=lambda handle: handle.sequence)
        coordinator = StoreShutdownCoordinator(deadline_seconds=deadline_seconds)
        for handle in handles:
            coordinator.register(
                handle.logical_name,
                self._catalog.effective_criticality(handle.logical_name),
                lambda handle=handle: self._finalize(handle),
            )
        return coordinator.shutdown()

    @staticmethod
    def _finalize(handle: _ManagedConnection) -> None:
        connection = handle.connection
        errors: list[BaseException] = []
        try:
            connection.commit()
            journal_mode = handle.journal_mode
            if journal_mode is None:
                row = connection.execute("PRAGMA journal_mode").fetchone()
                journal_mode = str(row[0]).lower() if row else None
            if journal_mode == "wal":
                checkpoint = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                if checkpoint and int(checkpoint[0]):
                    raise sqlite3.OperationalError(
                        "WAL checkpoint remained busy during store finalization"
                    )
        except BaseException as exc:
            errors.append(exc)
        finally:
            try:
                connection.close()
            except BaseException as exc:
                errors.append(exc)
        if errors:
            already_closed = (
                connection._store_authority is None
                and connection._store_handle_id is None
                and all(_is_closed_database_error(exc) for exc in errors)
            )
            if already_closed:
                return
            details = "; ".join(f"{type(exc).__name__}: {exc}" for exc in errors)
            raise RuntimeError(details)


def _is_closed_database_error(exc: BaseException) -> bool:
    return (
        isinstance(exc, sqlite3.ProgrammingError)
        and str(exc) == "Cannot operate on a closed database."
    )


@dataclass(slots=True)
class _CatalogEntry:
    record: StoreRecord
    used_relative_path: bool
    observation: StoreObservation | None = None


@dataclass(frozen=True, slots=True)
class _FilesystemDatabase:
    relative_path: str
    resolved_path: str
    dev_ino: tuple[int, int]


class StoreReservation:
    """One catalog record reserved before its SQLite file is created.

    The entry is visible immediately. Call :meth:`commit` only after creation,
    durable commit/fsync, binding or refreshing physical identity, and catalog
    validation succeed. Leaving the context without committing removes the exact
    reserved entry.
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

    def bind_observation(self, observation: StoreObservation) -> StoreRecord:
        """Bind the reservation to a live descriptor without reopening its path."""
        return self._catalog._bind_reservation_observation(self._entry, observation)

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
        manifest: Mapping[str, StoreIntegrityTarget] | None = None,
        observability: StorageObservability | None = None,
    ) -> None:
        root = os.getcwd() if expected_root is None else os.fspath(expected_root)
        self._expected_root = os.path.realpath(root)
        configured_allowlist = (
            DEFAULT_FILESYSTEM_SILENCE_ALLOWLIST if silence_allowlist is None else silence_allowlist
        )
        self._silence_allowlist = dict(configured_allowlist)
        self._manifest = dict(manifest or {})
        self._observability = observability
        self._entries: list[_CatalogEntry] = []
        self._observations: list[StoreObservation] = []
        self._lock = threading.RLock()
        self._connection_authority = _StoreConnectionAuthority(self)

    @property
    def expected_root(self) -> str:
        """Return the canonical filesystem root this catalog governs."""
        return self._expected_root

    def manifest_target(self, logical_name: str) -> StoreIntegrityTarget | None:
        """Return the declared target for one logical store, if configured."""
        return self._manifest.get(logical_name)

    def configure_manifest(
        self,
        manifest: Mapping[str, StoreIntegrityTarget],
    ) -> None:
        """Attach the boot manifest before any constructor registers a store."""
        with self._lock:
            if self._entries:
                raise StoreCatalogError("store manifest cannot change after registration")
            self._manifest = dict(manifest)

    def configure_observability(self, observability: StorageObservability) -> None:
        """Attach the optional recorder before catalog-owned connections open."""
        with self._lock:
            if self._entries:
                raise StoreCatalogError("storage observability cannot change after registration")
            self._observability = observability

    def connection_policy(self, logical_name: str) -> StoreConnectionPolicy:
        """Return the catalog-declared connection policy for one store."""
        target = self.manifest_target(logical_name)
        if target is not None:
            return target.connection_policy
        return default_store_connection_policy(logical_name)

    def effective_criticality(self, logical_name: str) -> str:
        """Return physical-cohort criticality using the strongest declaration."""
        target = self.manifest_target(logical_name)
        if target is not None:
            target_path = os.path.realpath(os.fspath(target.path))
            cohort = [
                candidate.criticality
                for candidate in self._manifest.values()
                if os.path.realpath(os.fspath(candidate.path)) == target_path
            ]
            return max(cohort, key=lambda criticality: _CRITICALITY_RANK[criticality])
        with self._lock:
            records = [
                entry.record for entry in self._entries if entry.record.logical_name == logical_name
            ]
        if not records:
            return "authority"
        record = records[-1]
        cohort = []
        with self._lock:
            for entry in self._entries:
                if entry.record.resolved_path == record.resolved_path:
                    cohort.append(entry.record.criticality)
        return max(cohort, key=lambda criticality: _CRITICALITY_RANK[criticality])

    def open_connection(
        self,
        logical_name: str,
        database: Any,
        *,
        owner: str,
        **kwargs: Any,
    ) -> sqlite3.Connection:
        """Open and track one connection under the catalog's declared policy."""
        if self._observability is not None:
            self._observability.enable_runtime_if_armed()
        return self._connection_authority.open(
            logical_name,
            database,
            owner=owner,
            **kwargs,
        )

    def shutdown(self, *, deadline_seconds: float) -> StoreShutdownReport:
        """Finalize every tracked connection, then release preflight descriptors."""
        try:
            return self._connection_authority.shutdown(deadline_seconds=deadline_seconds)
        finally:
            self.close()

    def register(
        self,
        logical_name: str,
        path: str | os.PathLike[str],
        *,
        journal_mode: str,
        owner: str,
        criticality: str | None = None,
        recovery: str | None = None,
        connection_policy: StoreConnectionPolicy | None = None,
    ) -> None:
        """Record one store connection, canonicalized to physical path identity.

        Repeated connection opens for the same logical name, owner, path, and
        policy refresh the existing record. Divergent declarations are retained
        so :meth:`validate` can report the complete conflict at the boot gate.
        """
        raw_path = os.fspath(path)
        is_memory = self._is_memory_path(raw_path)
        resolved_path = os.path.realpath(raw_path)
        target = self.manifest_target(logical_name)
        declared_criticality = (
            target.criticality if target is not None else criticality or "authoritative"
        )
        declared_recovery = target.recovery if target is not None else recovery or "snapshot"
        declared_policy = (
            target.connection_policy
            if target is not None
            else connection_policy or default_store_connection_policy(logical_name)
        )
        record = StoreRecord(
            logical_name=logical_name,
            resolved_path=resolved_path,
            journal_mode=journal_mode.lower(),
            owner=owner,
            criticality=declared_criticality,
            dev_ino=None if is_memory else self._stat_identity(resolved_path),
            is_memory=is_memory,
            recovery=declared_recovery,
            connection_policy=declared_policy,
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
                    and current.recovery == record.recovery
                    and current.connection_policy == record.connection_policy
                    and current.is_memory == record.is_memory
                ):
                    entry.record = record
                    entry.used_relative_path = entry.used_relative_path or used_relative_path
                    self._connection_authority.update_registration(record)
                    return
            self._entries.append(
                _CatalogEntry(
                    record=record,
                    used_relative_path=used_relative_path,
                )
            )
            self._connection_authority.update_registration(record)

    def register_observed(
        self,
        target: StoreIntegrityTarget,
        observation: StoreObservation,
        *,
        owner: str,
    ) -> None:
        """Consume a live preflight handle as the record's sole physical truth."""
        raw_path = os.fspath(target.path)
        absolute_path = os.path.abspath(raw_path)
        with self._lock:
            if not any(current is observation for current in self._observations):
                raise StoreCatalogError("preflight observation is not owned by this catalog")
            if observation._claimed:
                raise StoreCatalogError("preflight observation was already consumed")
            if target.logical_name not in observation.logical_names:
                raise StoreCatalogError(
                    f"preflight observation does not cover logical store {target.logical_name!r}"
                )
            if absolute_path != observation.path:
                raise StoreCatalogError("preflight observation path does not match registration")
            try:
                observation.require_path_unchanged()
            except OSError as exc:
                raise StoreCatalogError(
                    "preflight observation path changed before registration: "
                    f"logical_store={target.logical_name!r} path={absolute_path!r}"
                ) from exc

            journal_mode = (observation.journal_mode or target.journal_mode or "delete").lower()
            record = StoreRecord(
                logical_name=target.logical_name,
                resolved_path=absolute_path,
                journal_mode=journal_mode,
                owner=owner,
                criticality=target.criticality,
                dev_ino=observation.dev_ino,
                is_memory=False,
                recovery=target.recovery,
                connection_policy=target.connection_policy,
            )
            if any(
                entry.record.logical_name == record.logical_name
                and entry.record.resolved_path == record.resolved_path
                and entry.record.owner == record.owner
                for entry in self._entries
            ):
                raise StoreCatalogError(
                    "observed store registration duplicates an existing catalog record: "
                    + self._format_record(record)
                )
            observation._claimed = True
            self._entries.append(
                _CatalogEntry(
                    record=record,
                    used_relative_path=not os.path.isabs(raw_path),
                    observation=(observation if observation.dev_ino is not None else None),
                )
            )
            self._connection_authority.update_registration(record)

    def reserve(
        self,
        logical_name: str,
        path: str | os.PathLike[str],
        *,
        journal_mode: str,
        owner: str,
        criticality: str | None = None,
        recovery: str | None = None,
        connection_policy: StoreConnectionPolicy | None = None,
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
        absolute_path = os.path.abspath(raw_path)
        target = self.manifest_target(logical_name)
        record = StoreRecord(
            logical_name=logical_name,
            resolved_path=absolute_path,
            journal_mode=journal_mode.lower(),
            owner=owner,
            criticality=(
                target.criticality if target is not None else criticality or "authoritative"
            ),
            dev_ino=self._stat_identity(absolute_path),
            is_memory=False,
            recovery=target.recovery if target is not None else recovery or "snapshot",
            connection_policy=(
                target.connection_policy
                if target is not None
                else connection_policy or default_store_connection_policy(logical_name)
            ),
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
            if entry.observation is not None:
                raise StoreCatalogError(
                    "descriptor-bound store reservation cannot refresh from a pathname"
                )
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
                recovery=record.recovery,
                connection_policy=record.connection_policy,
            )
            return entry.record

    def _bind_reservation_observation(
        self,
        entry: _CatalogEntry,
        observation: StoreObservation,
    ) -> StoreRecord:
        with self._lock:
            if entry not in self._entries:
                raise StoreCatalogError("store reservation is no longer active")
            if entry.observation is not None:
                raise StoreCatalogError("store reservation already has a bound observation")
            record = entry.record
            if observation.logical_names != (record.logical_name,):
                raise StoreCatalogError("store observation does not match reservation logical name")
            if observation.path != record.resolved_path:
                raise StoreCatalogError("store observation path does not match reservation")
            if observation.journal_mode != record.journal_mode:
                raise StoreCatalogError("store observation mode does not match reservation")
            try:
                observation.require_path_unchanged()
            except OSError as exc:
                raise StoreCatalogError(
                    "published store path changed before reservation bind"
                ) from exc
            if observation.dev_ino is None:
                raise StoreCatalogError("published store observation has no physical identity")
            observation._claimed = True
            entry.observation = observation
            entry.record = StoreRecord(
                logical_name=record.logical_name,
                resolved_path=record.resolved_path,
                journal_mode=record.journal_mode,
                owner=record.owner,
                criticality=record.criticality,
                dev_ino=observation.dev_ino,
                is_memory=False,
                recovery=record.recovery,
                connection_policy=record.connection_policy,
            )
            self._observations.append(observation)
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
            if entry.observation is not None:
                entry.observation.close()
                self._observations = [
                    observation
                    for observation in self._observations
                    if observation is not entry.observation
                ]

    def validate(self) -> list[str]:
        """Raise for an unsafe layout and return filesystem hygiene warnings."""
        with self._lock:
            self._require_observations_unchanged()
            self._refresh_identities()
            entries = list(self._entries)

        violations: list[str] = []
        warnings: list[str] = []
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
            if record.criticality not in _CRITICALITY_RANK:
                violations.append("unknown criticality on " + self._format_record(record))
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

        registered_logical_names = set(records_by_logical_name)
        for logical_name in self._manifest:
            if logical_name in registered_logical_names:
                continue
            effective_criticality = self.effective_criticality(logical_name)
            absence = (
                f"logical_store={logical_name!r} "
                f"effective_criticality={effective_criticality!r} "
                "post-construction catalog absence"
            )
            if effective_criticality == "telemetry":
                warnings.append(f"degraded telemetry store: {absence}")
            else:
                violations.append(absence)

        if violations:
            raise StoreCatalogError(
                "Store catalog validation failed:\n- " + "\n- ".join(violations)
            )

        for warning in warnings:
            logger.warning("STORE CATALOG WARNING: %s", warning)
        return [*warnings, *self.reconcile_filesystem()]

    def preflight_integrity(
        self,
        targets: Iterable[StoreIntegrityTarget],
        *,
        on_outcome: Callable[[str, str], None] | None = None,
    ) -> dict[str, StoreObservation]:
        """Fail closed when an existing API-owned SQLite file is corrupt.

        Targets that share a physical path are checked once and reported together.
        Missing and in-memory stores are left for their constructors to create.
        """
        targets_by_path: dict[str, list[StoreIntegrityTarget]] = {}
        outcomes: dict[str, str] = {}
        observations: dict[str, StoreObservation] = {}
        for target in targets:
            raw_path = os.fspath(target.path)
            if self._is_memory_path(raw_path):
                continue
            absolute_path = os.path.abspath(raw_path)
            targets_by_path.setdefault(absolute_path, []).append(target)

        for absolute_path, matching_targets in targets_by_path.items():
            logical_names = tuple(target.logical_name for target in matching_targets)
            try:
                bound_file = BoundSQLiteFile.open(absolute_path)
            except OSError as exc:
                if self._is_missing_path_error(exc):
                    observation = StoreObservation.absent(logical_names, absolute_path)
                    with self._lock:
                        self._observations.append(observation)
                    for target in matching_targets:
                        observations[target.logical_name] = observation
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
                self._raise_integrity_error(matching_targets, absolute_path, exc)

            retain_bound_file = False
            quick_check_failed = False
            try:
                header_journal_mode = bound_file.header_journal_mode()
                rollback_required = any(
                    target.journal_mode is not None and target.journal_mode.lower() != "wal"
                    for target in matching_targets
                )
                if header_journal_mode == "wal" and rollback_required:
                    self._record_integrity_outcomes(
                        matching_targets,
                        "failed-corrupt",
                        outcomes,
                        on_outcome,
                    )
                    self._raise_integrity_error(
                        matching_targets,
                        absolute_path,
                        "persistent WAL journal conflicts with the required rollback-journal "
                        "policy; refused before a mode=ro open could create WAL/SHM sidecars",
                    )
                observed_journal_mode = header_journal_mode
                if header_journal_mode == "wal":
                    connection = self._connect_wal_for_preflight(
                        bound_file,
                        absolute_path,
                    )
                else:
                    connection = bound_file.connect_read_only()
                try:
                    if any(target.journal_mode is not None for target in matching_targets):
                        journal_row = connection.execute("PRAGMA journal_mode").fetchone()
                        if journal_row and journal_row[0]:
                            observed_journal_mode = str(journal_row[0]).lower()
                    try:
                        rows = connection.execute("PRAGMA quick_check").fetchall()
                    except sqlite3.Error:
                        quick_check_failed = True
                        raise
                finally:
                    connection.close()

                path_state = bound_file.path_state()
                if path_state == "absent":
                    observation = StoreObservation.absent(logical_names, absolute_path)
                    with self._lock:
                        self._observations.append(observation)
                    for target in matching_targets:
                        observations[target.logical_name] = observation
                    self._record_integrity_outcomes(
                        matching_targets,
                        "skipped-absent",
                        outcomes,
                        on_outcome,
                    )
                    continue
                if path_state != "same":
                    self._record_integrity_outcomes(
                        matching_targets,
                        "failed-corrupt",
                        outcomes,
                        on_outcome,
                    )
                    self._raise_integrity_error(
                        matching_targets,
                        absolute_path,
                        "SQLite path changed physical identity while its preflight fd was held",
                    )
            except (sqlite3.Error, OSError) as exc:
                try:
                    failure_path_state = bound_file.path_state()
                except OSError as reconciliation_error:
                    self._record_integrity_outcomes(
                        matching_targets,
                        "failed-corrupt",
                        outcomes,
                        on_outcome,
                    )
                    self._raise_integrity_error(
                        matching_targets,
                        absolute_path,
                        "SQLite preflight failed and its reject-only path reconciliation "
                        f"was unperformable: preflight={type(exc).__name__}: {exc}; "
                        "reconciliation="
                        f"{type(reconciliation_error).__name__}: {reconciliation_error}",
                        quick_check_failed=quick_check_failed,
                    )
                if failure_path_state == "absent":
                    observation = StoreObservation.absent(logical_names, absolute_path)
                    with self._lock:
                        self._observations.append(observation)
                    for target in matching_targets:
                        observations[target.logical_name] = observation
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
                self._raise_integrity_error(
                    matching_targets,
                    absolute_path,
                    exc,
                    quick_check_failed=quick_check_failed,
                )
            else:
                if rows != [("ok",)]:
                    self._record_integrity_outcomes(
                        matching_targets,
                        "failed-corrupt",
                        outcomes,
                        on_outcome,
                    )
                    self._raise_integrity_error(
                        matching_targets,
                        absolute_path,
                        f"PRAGMA quick_check returned {rows!r}",
                        quick_check_failed=True,
                    )
                observation = StoreObservation(
                    logical_names=logical_names,
                    path=absolute_path,
                    outcome="ok",
                    journal_mode=observed_journal_mode,
                    _bound_file=bound_file,
                )
                retain_bound_file = True
                with self._lock:
                    self._observations.append(observation)
                for target in matching_targets:
                    observations[target.logical_name] = observation
                self._record_integrity_outcomes(
                    matching_targets,
                    "ok",
                    outcomes,
                    on_outcome,
                )
            finally:
                if not retain_bound_file:
                    bound_file.close()

        return observations

    def _connect_wal_for_preflight(
        self,
        bound_file: BoundSQLiteFile,
        absolute_path: str,
    ) -> sqlite3.Connection:
        """Keep tenant-capable catalogs structurally descriptor-only."""
        raise PermissionError(
            "descriptor-only store catalog refuses pathname resolution for persistent WAL: "
            f"{absolute_path!r}"
        )

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

    def _raise_integrity_error(
        self,
        targets: list[StoreIntegrityTarget],
        resolved_path: str,
        detail: BaseException | str,
        *,
        quick_check_failed: bool = False,
    ) -> None:
        if self._observability is not None:
            self._observability.record_preflight_refusal(
                (target.logical_name for target in targets),
                quick_check_failed=quick_check_failed,
            )
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
            self._require_observations_unchanged()
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
            self._require_observations_unchanged()
            self._refresh_identities()
            return [entry.record for entry in self._entries]

    def close(self) -> None:
        """Release every descriptor retained by live preflight observations."""
        with self._lock:
            observations = self._observations
            self._observations = []
            for entry in self._entries:
                entry.observation = None
        closed: set[int] = set()
        for observation in observations:
            identity = id(observation)
            if identity in closed:
                continue
            observation.close()
            closed.add(identity)

    def _require_observations_unchanged(self) -> None:
        violations: list[str] = []
        checked: set[int] = set()
        for observation in self._observations:
            identity = id(observation)
            if identity in checked:
                continue
            checked.add(identity)
            try:
                observation.require_path_unchanged()
            except OSError as exc:
                violations.append(
                    "pinned preflight path changed; refusing replacement: "
                    f"logical_stores={list(observation.logical_names)!r} "
                    f"path={observation.path!r} error={type(exc).__name__}: {exc}"
                )
        if violations:
            raise StoreCatalogError(
                "Store catalog pinned-path reconciliation failed:\n- " + "\n- ".join(violations)
            )

    @staticmethod
    def _stat_identity(path: str) -> tuple[int, int] | None:
        try:
            stat = os.stat(path)
        except OSError:
            return None
        return (stat.st_dev, stat.st_ino)

    def _refresh_identities(self) -> None:
        for entry in self._entries:
            if entry.observation is not None:
                continue
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
                    recovery=record.recovery,
                    connection_policy=record.connection_policy,
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
            f"criticality={record.criticality!r} recovery={record.recovery!r} "
            f"dev_ino={record.dev_ino!r} is_memory={record.is_memory!r}"
        )


class DaemonStoreCatalog(StoreCatalog):
    """Fleet catalog allowed to inspect WAL in a verified daemon-only namespace."""

    def register(
        self,
        logical_name: str,
        path: str | os.PathLike[str],
        *,
        journal_mode: str,
        owner: str,
        criticality: str | None = None,
        recovery: str | None = None,
        connection_policy: StoreConnectionPolicy | None = None,
    ) -> None:
        """Allow a fleet authority constructor to fulfill a preflight absence."""
        raw_path = os.fspath(path)
        absolute_path = os.path.abspath(raw_path)
        with self._lock:
            fulfilled_absences = [
                observation
                for observation in self._observations
                if observation.outcome == "skipped-absent"
                and observation.path == absolute_path
                and logical_name in observation.logical_names
            ]
        super().register(
            logical_name,
            path,
            journal_mode=journal_mode,
            owner=owner,
            criticality=criticality,
            recovery=recovery,
            connection_policy=connection_policy,
        )
        if fulfilled_absences:
            with self._lock:
                self._observations = [
                    observation
                    for observation in self._observations
                    if observation not in fulfilled_absences
                ]

    def _connect_wal_for_preflight(
        self,
        bound_file: BoundSQLiteFile,
        absolute_path: str,
    ) -> sqlite3.Connection:
        """Open fleet WAL only after the namespace itself is proven non-tenant-writable.

        The ownership/mode check is the load-bearing swap defense.  The descriptor
        inventory below is only corroboration: SQLite may reuse a legitimate prior
        descriptor for this inode, so finding a matching non-pinned descriptor cannot
        independently prove which pathname SQLite opened.
        """
        resolved_path = self._verified_daemon_owned_path(absolute_path)
        bound_file.require_path_unchanged()
        descriptors_before = self._open_descriptor_identities()
        connection = sqlite3.connect(
            Path(resolved_path).as_uri() + "?mode=ro",
            uri=True,
        )
        try:
            descriptors_after = self._open_descriptor_identities()
            matching_descriptors = {
                descriptor
                for descriptor, identity in descriptors_after.items()
                if descriptor != bound_file.descriptor and identity == bound_file.dev_ino
            }
            # Defense in depth only: a pre-existing descriptor can be SQLite's valid
            # same-inode reuse, but it could also mask a divergent pathname open if the
            # verified daemon-only namespace fence above were ever bypassed.
            newly_opened = matching_descriptors.difference(descriptors_before)
            sqlite_reused = matching_descriptors.intersection(descriptors_before)
            if not newly_opened and not sqlite_reused:
                raise OSError("pathname-opened WAL database does not match its pinned preflight fd")
            bound_file.require_path_unchanged()
        except BaseException:
            connection.close()
            raise
        return connection

    def _verified_daemon_owned_path(self, absolute_path: str) -> str:
        """Return a canonical path only when its full directory chain is trusted."""
        resolved_path = os.path.realpath(absolute_path)
        resolved_parent = os.path.dirname(resolved_path)
        if not self._is_under_expected_root(resolved_path):
            raise PermissionError(
                f"WAL preflight path escapes the daemon-owned catalog root: {resolved_path!r}"
            )

        daemon_uid = os.geteuid()
        expected_root = self.expected_root
        directory = Path(resolved_parent)
        directories = [directory, *directory.parents]
        for current in reversed(directories):
            current_path = os.fspath(current)
            current_stat = os.stat(current_path, follow_symlinks=False)
            if not stat.S_ISDIR(current_stat.st_mode):
                raise PermissionError(
                    f"WAL preflight path component is not a directory: {current_path!r}"
                )
            mode = stat.S_IMODE(current_stat.st_mode)
            if mode & 0o022:
                raise PermissionError(
                    "WAL preflight path component is writable by a non-daemon uid: "
                    f"path={current_path!r} mode={mode:04o}"
                )
            inside_catalog = self._path_at_or_below(current_path, expected_root)
            allowed_uids = {daemon_uid} if inside_catalog else {0, daemon_uid}
            if current_stat.st_uid not in allowed_uids:
                raise PermissionError(
                    "WAL preflight path component has a non-daemon owner: "
                    f"path={current_path!r} uid={current_stat.st_uid} "
                    f"daemon_uid={daemon_uid}"
                )
        return resolved_path

    @staticmethod
    def _path_at_or_below(path: str, root: str) -> bool:
        try:
            return os.path.commonpath((root, path)) == root
        except ValueError:
            return False

    @staticmethod
    def _open_descriptor_identities() -> dict[int, tuple[int, int]]:
        descriptor_root = "/proc/self/fd" if sys.platform == "linux" else "/dev/fd"
        identities: dict[int, tuple[int, int]] = {}
        for name in os.listdir(descriptor_root):
            try:
                descriptor = int(name)
                descriptor_stat = os.fstat(descriptor)
            except (OSError, ValueError):
                continue
            identities[descriptor] = (descriptor_stat.st_dev, descriptor_stat.st_ino)
        return identities


def store_connection_policy(
    catalog: StoreCatalog | None,
    logical_name: str,
) -> StoreConnectionPolicy:
    """Resolve one store's policy with or without a boot catalog."""
    if catalog is not None:
        return catalog.connection_policy(logical_name)
    return default_store_connection_policy(logical_name)


def apply_store_connection_policy(
    connection: sqlite3.Connection,
    policy: StoreConnectionPolicy,
) -> None:
    """Apply a declared post-open policy without changing journal mode."""
    connection.execute(f"PRAGMA busy_timeout={int(policy.busy_timeout_ms)}")


def open_store_connection(
    catalog: StoreCatalog | None,
    logical_name: str,
    database: Any,
    *,
    owner: str,
    **kwargs: Any,
) -> sqlite3.Connection:
    """Route every owner-side SQLite open through the central policy seam."""
    if catalog is not None:
        return catalog.open_connection(logical_name, database, owner=owner, **kwargs)

    policy = default_store_connection_policy(logical_name)
    timeout_seconds = policy.busy_timeout_ms / 1_000
    requested_timeout = kwargs.pop("timeout", None)
    if requested_timeout is not None and float(requested_timeout) != timeout_seconds:
        raise ValueError(
            "connection timeout override diverges from store policy: "
            f"logical_store={logical_name!r} requested={requested_timeout!r} "
            f"declared={timeout_seconds!r}"
        )
    kwargs["timeout"] = (
        _SQLITE_DEFAULT_TIMEOUT_SECONDS if requested_timeout is None else float(requested_timeout)
    )
    connection = sqlite3.connect(database, **kwargs)
    try:
        apply_store_connection_policy(connection, policy)
    except BaseException:
        connection.close()
        raise
    return connection
