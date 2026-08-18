"""Typed ownership boundary for fleet and standalone agent signing keys."""

from __future__ import annotations

import os
import re
import secrets
import sqlite3
import time
from pathlib import Path
from typing import Protocol, runtime_checkable

from pinky_daemon.store_catalog import StoreCatalog

AGENT_SIGNING_KEY_LOGICAL_NAME = "agent_signing_keys"
FLEET_SIGNING_KEY_OWNER = "AgentRegistry+AgentSigningKeyStore"
_AGENT_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
_SQLITE_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")
_SECRET_MODE = 0o600
_SECRET_OPEN_FLAGS = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)


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
            uri = Path(self._db_path).as_uri() + "?mode=ro"
            connection = sqlite3.connect(uri, uri=True, timeout=5)
            try:
                return _select_signing_key(connection, agent_name)
            finally:
                connection.close()
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
    ) -> None:
        """Exclusively create, durably commit, and register one tenant key DB."""
        name = _validated_agent_name(agent_name)
        if not signing_key:
            raise ValueError("signing key must not be empty")
        # Preserve the lexical pathname for the exclusive open: resolving it
        # here would follow a planted dangling symlink before O_NOFOLLOW gets a
        # chance to reject it. StoreCatalog canonicalizes its own record.
        target = Path(os.path.abspath(path))
        reservation = catalog.reserve(
            AGENT_SIGNING_KEY_LOGICAL_NAME,
            target,
            journal_mode="delete",
            owner=cls.__name__,
        )
        created = False
        created_identity: tuple[int, int] | None = None
        with reservation:
            try:
                # Enforce the scoped-root boundary while the target is still
                # absent. The reservation is already visible at this point.
                catalog.validate()
                descriptor = os.open(target, _SECRET_OPEN_FLAGS, _SECRET_MODE)
                created = True
                try:
                    descriptor_stat = os.fstat(descriptor)
                    created_identity = (descriptor_stat.st_dev, descriptor_stat.st_ino)
                finally:
                    os.close(descriptor)
                connection = sqlite3.connect(target)
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
                os.chmod(target, _SECRET_MODE)
                _fsync_file(target)
                _fsync_directory(target.parent)
                record = reservation.refresh()
                if record.dev_ino is None or record.dev_ino != created_identity:
                    raise OSError("created signing-key DB changed physical identity")
                catalog.validate()
                reservation.commit()
            except Exception:
                if created:
                    _remove_store_and_sidecars(target)
                raise

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


def _fsync_file(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _remove_store_and_sidecars(path: Path) -> None:
    for candidate in (
        path,
        *(Path(os.fspath(path) + suffix) for suffix in _SQLITE_SIDECAR_SUFFIXES),
    ):
        try:
            candidate.unlink()
        except FileNotFoundError:
            pass
