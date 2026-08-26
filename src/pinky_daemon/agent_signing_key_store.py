"""Typed ownership boundary for fleet and standalone agent signing keys."""

from __future__ import annotations

import errno
import os
import re
import secrets
import sqlite3
import stat
import sys
import time
from pathlib import Path
from typing import Protocol, runtime_checkable

from pinky_daemon.store_catalog import (
    BoundSQLiteFile,
    StoreCatalog,
    StoreObservation,
    open_store_connection,
)

AGENT_SIGNING_KEY_LOGICAL_NAME = "agent_signing_keys"
FLEET_SIGNING_KEY_OWNER = "AgentRegistry+AgentSigningKeyStore"
_AGENT_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
_SQLITE_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")
_SECRET_MODE = 0o600
_SECRET_OPEN_FLAGS = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
_DIRECTORY_OPEN_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
SIGNING_KEY_STAGING_ROOT_ENV = "PINKY_SIGNING_KEY_STAGING_ROOT"
_STAGING_DIRECTORY_NAME = ".signing-key-staging"
_STAGING_DATABASE_NAME = "agent_keys.db"


@runtime_checkable
class SigningKeyReader(Protocol):
    """Narrow read capability consumed by request-signing code."""

    def get_signing_key(self, agent_name: str) -> str | None: ...


class AgentSigningKeyStore:
    """Own signing-key SQL for a fleet registry or one standalone tenant."""

    def __init__(
        self,
        db_path: str,
        *,
        connection: sqlite3.Connection | None = None,
        catalog: StoreCatalog | None = None,
        catalog_owner: str = FLEET_SIGNING_KEY_OWNER,
    ) -> None:
        self._db_path = os.fspath(Path(db_path).resolve())
        self._connection = connection
        if catalog is not None:
            journal_mode = "truncate" if connection is not None else "delete"
            catalog.register(
                AGENT_SIGNING_KEY_LOGICAL_NAME,
                self._db_path,
                journal_mode=journal_mode,
                owner=catalog_owner,
            )

    @classmethod
    def for_connection(
        cls,
        db_path: str,
        connection: sqlite3.Connection,
        *,
        catalog: StoreCatalog | None = None,
    ) -> "AgentSigningKeyStore":
        """Bind the owner to the AgentRegistry's existing writable connection."""
        return cls(
            db_path,
            connection=connection,
            catalog=catalog,
            catalog_owner=FLEET_SIGNING_KEY_OWNER,
        )

    @classmethod
    def read_only(cls, db_path: str) -> "AgentSigningKeyStore":
        """Return a fail-soft reader that opens the DB with SQLite ``mode=ro``."""
        return cls(db_path)

    def ensure_schema(self) -> None:
        """Create the unchanged signing-key table on a bound fleet connection."""
        connection = self._require_writable_connection()
        connection.execute(
            "CREATE TABLE IF NOT EXISTS agent_signing_keys ("
            "agent_name TEXT PRIMARY KEY, "
            "signing_key TEXT NOT NULL, "
            "created_at REAL NOT NULL DEFAULT 0)"
        )
        connection.commit()

    def get_signing_key(self, agent_name: str) -> str | None:
        """Return a current key, validating names and failing soft on read errors."""
        if not _valid_agent_name(agent_name):
            return None
        if self._connection is not None:
            try:
                return _select_signing_key(self._connection, agent_name)
            except Exception:
                return None

        try:
            with BoundSQLiteFile.open(self._db_path) as bound_file:
                if bound_file.header_journal_mode() == "wal":
                    return None
                connection = bound_file.connect_read_only(timeout=5)
                try:
                    signing_key = _select_signing_key(connection, agent_name)
                finally:
                    connection.close()
                bound_file.require_path_unchanged()
                return signing_key
        except Exception:
            return None

    def get_or_create_signing_key(self, agent_name: str) -> str:
        """Return the fleet key, atomically creating one on first use."""
        name = _validated_agent_name(agent_name)
        connection = self._require_writable_connection()
        existing = _select_signing_key(connection, name)
        if existing:
            return existing
        key = secrets.token_urlsafe(32)
        connection.execute(
            "INSERT OR REPLACE INTO agent_signing_keys "
            "(agent_name, signing_key, created_at) VALUES (?, ?, ?)",
            (name, key, time.time()),
        )
        connection.commit()
        return key

    def delete_signing_key(self, agent_name: str) -> None:
        """Delete one fleet signing key through the typed owner."""
        name = _validated_agent_name(agent_name)
        connection = self._require_writable_connection()
        connection.execute(
            "DELETE FROM agent_signing_keys WHERE agent_name=?",
            (name,),
        )

    @classmethod
    def provision_single_agent(
        cls,
        path: str,
        agent_name: str,
        signing_key: str,
        *,
        catalog: StoreCatalog,
        staging_root: str | os.PathLike[str],
    ) -> None:
        """Build privately, then atomically publish one tenant key database."""
        name = _validated_agent_name(agent_name)
        if not signing_key:
            raise ValueError("signing key must not be empty")
        target = Path(os.path.abspath(path))
        staging_root_path = Path(os.path.abspath(os.fspath(staging_root)))
        parent_descriptor: int | None = None
        staging_root_descriptor: int | None = None
        attempt_name: str | None = None
        attempt_descriptor: int | None = None
        staged_descriptor: int | None = None
        try:
            parent_descriptor, parent_stat = _open_secure_parent(target.parent, name)
            staging_root_descriptor = _open_daemon_staging_root(staging_root_path)
            staging_root_stat = os.fstat(staging_root_descriptor)
            if staging_root_stat.st_dev != parent_stat.st_dev:
                raise OSError(
                    errno.EXDEV,
                    "signing-key staging root is on a different filesystem than the "
                    f"tenant target; configure {SIGNING_KEY_STAGING_ROOT_ENV}",
                )

            reservation = catalog.reserve(
                AGENT_SIGNING_KEY_LOGICAL_NAME,
                target,
                journal_mode="delete",
                owner=cls.__name__,
            )
            with reservation:
                catalog.validate()
                _require_target_namespace_absent(parent_descriptor, target.name)
                attempt_name, attempt_descriptor = _create_staging_directory(
                    staging_root_descriptor
                )
                staged_create_descriptor = os.open(
                    _STAGING_DATABASE_NAME,
                    _SECRET_OPEN_FLAGS,
                    _SECRET_MODE,
                    dir_fd=attempt_descriptor,
                )
                os.close(staged_create_descriptor)
                database, uri = _staging_sqlite_target(
                    staging_root_path / attempt_name,
                    attempt_descriptor,
                )
                connection = open_store_connection(
                    catalog,
                    AGENT_SIGNING_KEY_LOGICAL_NAME,
                    database,
                    owner=cls.__name__,
                    uri=uri,
                )
                try:
                    row = connection.execute("PRAGMA journal_mode=DELETE").fetchone()
                    journal_mode = str(row[0]).lower() if row else ""
                    if journal_mode != "delete":
                        raise sqlite3.OperationalError(
                            "standalone signing-key DB refused rollback journal mode"
                        )
                    connection.execute(
                        "CREATE TABLE agent_signing_keys ("
                        "agent_name TEXT PRIMARY KEY, "
                        "signing_key TEXT NOT NULL, created_at REAL)"
                    )
                    connection.execute(
                        "INSERT INTO agent_signing_keys "
                        "(agent_name, signing_key, created_at) VALUES (?, ?, ?)",
                        (name, signing_key, time.time()),
                    )
                    connection.commit()
                finally:
                    connection.close()

                _require_staging_sidecars_absent(attempt_descriptor)
                staged_descriptor = os.open(
                    _STAGING_DATABASE_NAME,
                    os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=attempt_descriptor,
                )
                os.fchmod(staged_descriptor, _SECRET_MODE)
                os.fchown(staged_descriptor, parent_stat.st_uid, parent_stat.st_gid)
                os.fsync(staged_descriptor)
                os.fsync(attempt_descriptor)

                _require_target_namespace_absent(parent_descriptor, target.name)
                _publish_no_replace(
                    attempt_descriptor,
                    _STAGING_DATABASE_NAME,
                    parent_descriptor,
                    target.name,
                )
                os.fsync(parent_descriptor)

                observation_descriptor = staged_descriptor
                staged_descriptor = None
                observation = StoreObservation.from_descriptor(
                    AGENT_SIGNING_KEY_LOGICAL_NAME,
                    target,
                    journal_mode="delete",
                    descriptor=observation_descriptor,
                )
                try:
                    record = reservation.bind_observation(observation)
                except BaseException:
                    observation.close()
                    raise
                if record.dev_ino != observation.dev_ino:
                    raise OSError("published signing-key DB changed physical identity")
                # Drop the daemon-private staging name before catalog alias
                # reconciliation. The observation fd keeps the published inode
                # pinned; the tenant namespace is never touched by cleanup.
                _clean_staging_attempt(attempt_descriptor)
                os.fsync(attempt_descriptor)
                catalog.validate()
                reservation.commit()
        finally:
            if staged_descriptor is not None:
                os.close(staged_descriptor)
            if attempt_descriptor is not None:
                _clean_staging_attempt(attempt_descriptor)
                os.close(attempt_descriptor)
            if attempt_name is not None:
                try:
                    assert staging_root_descriptor is not None
                    os.rmdir(attempt_name, dir_fd=staging_root_descriptor)
                except OSError:
                    pass
            if staging_root_descriptor is not None:
                os.close(staging_root_descriptor)
            if parent_descriptor is not None:
                os.close(parent_descriptor)

    def _require_writable_connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise RuntimeError("signing-key store is read-only")
        return self._connection


def _valid_agent_name(agent_name: str) -> bool:
    return bool(agent_name and _AGENT_NAME_RE.fullmatch(agent_name))


def _validated_agent_name(agent_name: str) -> str:
    if not _valid_agent_name(agent_name):
        raise ValueError(f"Invalid agent name: {agent_name!r}")
    return agent_name


def _select_signing_key(
    connection: sqlite3.Connection,
    agent_name: str,
) -> str | None:
    row = connection.execute(
        "SELECT signing_key FROM agent_signing_keys WHERE agent_name=?",
        (agent_name,),
    ).fetchone()
    return str(row[0]) if row and row[0] else None


def configured_signing_key_staging_root(
    data_root: str | os.PathLike[str] | None = None,
) -> Path:
    """Return the explicit daemon-owned root used for unpublished key DBs."""
    configured = os.environ.get(SIGNING_KEY_STAGING_ROOT_ENV, "").strip()
    if configured:
        return Path(os.path.abspath(configured))
    root = Path.cwd() / "data" if data_root is None else Path(data_root)
    return Path(os.path.abspath(root / _STAGING_DIRECTORY_NAME))


def validate_signing_key_staging_root(
    staging_root: str | os.PathLike[str],
    target_parents: tuple[str | os.PathLike[str], ...],
) -> None:
    """Fail loudly unless staging and every tenant target share one device."""
    root_path = Path(os.path.abspath(os.fspath(staging_root)))
    root_descriptor = _open_daemon_staging_root(root_path)
    try:
        root_stat = os.fstat(root_descriptor)
        for target_parent in target_parents:
            requested_parent = Path(os.path.abspath(os.fspath(target_parent)))
            existing_parent = requested_parent
            while True:
                try:
                    parent_descriptor = os.open(
                        existing_parent,
                        _DIRECTORY_OPEN_FLAGS,
                    )
                    break
                except FileNotFoundError:
                    if existing_parent.parent == existing_parent:
                        raise
                    existing_parent = existing_parent.parent
            try:
                parent_stat = os.fstat(parent_descriptor)
                if parent_stat.st_dev != root_stat.st_dev:
                    raise OSError(
                        errno.EXDEV,
                        "signing-key staging root and tenant home are on different "
                        f"filesystems: staging={os.fspath(root_path)!r} "
                        f"tenant_parent={os.fspath(requested_parent)!r}; configure "
                        f"{SIGNING_KEY_STAGING_ROOT_ENV}",
                    )
            finally:
                os.close(parent_descriptor)
    finally:
        os.close(root_descriptor)


def _allowed_parent_uids(agent_name: str) -> set[int]:
    allowed_uids = {os.geteuid()}
    if hasattr(os, "getuid"):
        allowed_uids.add(os.getuid())
    try:
        import pwd

        allowed_uids.add(pwd.getpwnam(f"pinky-{agent_name}").pw_uid)
    except (ImportError, KeyError):
        pass
    return allowed_uids


def _open_secure_parent(path: Path, agent_name: str) -> tuple[int, os.stat_result]:
    descriptor = os.open(path, _DIRECTORY_OPEN_FLAGS)
    parent_stat = os.fstat(descriptor)
    if not stat.S_ISDIR(parent_stat.st_mode):
        os.close(descriptor)
        raise PermissionError("signing-key parent must be a real directory")
    parent_mode = stat.S_IMODE(parent_stat.st_mode)
    if parent_mode != 0o700:
        os.close(descriptor)
        raise PermissionError(f"signing-key parent must be mode 0700, observed {parent_mode:04o}")
    if parent_stat.st_uid not in _allowed_parent_uids(agent_name):
        os.close(descriptor)
        raise PermissionError("signing-key parent is not owned by the daemon or expected tenant")
    return descriptor, parent_stat


def _open_daemon_staging_root(path: Path) -> int:
    created = False
    try:
        os.mkdir(path, 0o700)
        created = True
    except FileExistsError:
        pass
    descriptor = os.open(path, _DIRECTORY_OPEN_FLAGS)
    if created:
        os.fchmod(descriptor, 0o700)
    root_stat = os.fstat(descriptor)
    root_mode = stat.S_IMODE(root_stat.st_mode)
    if not stat.S_ISDIR(root_stat.st_mode):
        os.close(descriptor)
        raise PermissionError("signing-key staging root must be a real directory")
    if root_mode != 0o700:
        os.close(descriptor)
        raise PermissionError(
            f"signing-key staging root must be mode 0700, observed {root_mode:04o}"
        )
    if root_stat.st_uid != os.geteuid():
        os.close(descriptor)
        raise PermissionError("signing-key staging root must be owned by the daemon uid")
    return descriptor


def _require_target_namespace_absent(parent_descriptor: int, target_name: str) -> None:
    names = (target_name,) + tuple(target_name + suffix for suffix in _SQLITE_SIDECAR_SUFFIXES)
    existing: list[str] = []
    for name in names:
        try:
            os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        except FileNotFoundError:
            continue
        existing.append(name)
    if existing:
        if target_name in existing:
            raise FileExistsError(f"signing-key target already exists: {target_name!r}")
        raise FileExistsError(
            "refusing to publish signing-key DB beside pre-existing SQLite sidecar "
            f"paths: {existing!r}"
        )


def _create_staging_directory(root_descriptor: int) -> tuple[str, int]:
    for _attempt in range(128):
        name = f"attempt-{secrets.token_hex(16)}"
        try:
            os.mkdir(name, 0o700, dir_fd=root_descriptor)
        except FileExistsError:
            continue
        descriptor = os.open(name, _DIRECTORY_OPEN_FLAGS, dir_fd=root_descriptor)
        os.fchmod(descriptor, 0o700)
        return name, descriptor
    raise FileExistsError("could not reserve a unique signing-key staging directory")


def _staging_sqlite_target(
    attempt_path: Path,
    attempt_descriptor: int,
) -> tuple[str, bool]:
    if sys.platform == "linux":
        descriptor_path = Path(f"/proc/self/fd/{attempt_descriptor}/{_STAGING_DATABASE_NAME}")
        return descriptor_path.as_uri() + "?mode=rw", True
    if sys.platform == "darwin":
        return os.fspath(attempt_path / _STAGING_DATABASE_NAME), False
    raise OSError(f"no writable staging SQLite adapter for {sys.platform!r}")


def _require_staging_sidecars_absent(attempt_descriptor: int) -> None:
    existing: list[str] = []
    for suffix in _SQLITE_SIDECAR_SUFFIXES:
        name = _STAGING_DATABASE_NAME + suffix
        try:
            os.stat(name, dir_fd=attempt_descriptor, follow_symlinks=False)
        except FileNotFoundError:
            continue
        existing.append(name)
    if existing:
        raise OSError(f"staged signing-key DB retained SQLite sidecars: {existing!r}")


def _publish_no_replace(
    source_directory: int,
    source_name: str,
    target_directory: int,
    target_name: str,
) -> None:
    """Atomically create the target name by linkat; never replace an entry."""
    try:
        os.link(
            source_name,
            target_name,
            src_dir_fd=source_directory,
            dst_dir_fd=target_directory,
            follow_symlinks=False,
        )
    except OSError as exc:
        if exc.errno == errno.EXDEV:
            raise OSError(
                errno.EXDEV,
                "signing-key publish crossed filesystems; configure "
                f"{SIGNING_KEY_STAGING_ROOT_ENV}",
            ) from exc
        raise
    except (NotImplementedError, TypeError) as exc:  # pragma: no cover - platform guard
        raise OSError(
            errno.ENOTSUP,
            "atomic no-overwrite signing-key publish is unavailable",
        ) from exc


def _clean_staging_attempt(attempt_descriptor: int) -> None:
    names = (_STAGING_DATABASE_NAME,) + tuple(
        _STAGING_DATABASE_NAME + suffix for suffix in _SQLITE_SIDECAR_SUFFIXES
    )
    for name in names:
        try:
            os.unlink(name, dir_fd=attempt_descriptor)
        except FileNotFoundError:
            pass
