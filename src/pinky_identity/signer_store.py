"""SQLite-backed persistence for wrapped service-auth signing keys.

The :class:`EncryptedSignerStore` is the concrete on-disk backend for the
keystore primitives in :mod:`pinky_identity.keystore`. It holds
:class:`pinky_identity.WrappedKeypair` rows keyed by kid, decrypting on
demand under a :class:`DeviceKey`.

Design rules (see ``docs/design/agent-asymmetric-identity.md`` and #461):

- **Storage is daemon-local.** One SQLite file per host
  (``data/identity/signer_store.db`` by default). Agents do not have
  direct access; they go through the daemon-local signer service
  (:mod:`pinky_identity.signer`, PR-4b-2) which holds the
  :class:`DeviceKey` and unwraps in-process.
- **Schema mirrors :class:`WrappedKeypair`** plus a ``created_at``
  timestamp. No metadata — the registry (:mod:`pinky_identity.registry`)
  is the source of truth for lifecycle, status, fleet/agent/purpose
  tuples, etc. This store is the *secret-half mirror*.
- **Storage decouples from lifecycle.** Revocation/retirement happens in
  the registry; the corresponding ``delete()`` here is best-effort
  cleanup so an attacker who somehow obtains a stale row plus the device
  key still can't impersonate a retired agent (the registry won't return
  the kid as active).
- **WAL mode + journal_mode=WAL**, same as other PinkyBot SQLite stores.
- **No SQL injection surface.** All writes use parameterized queries; no
  string concatenation into SQL.
- **Errors normalize to** :class:`pinky_identity.keystore.KeystoreError`
  (subclass of :class:`SignatureError`) on decrypt failures and
  :class:`KeyError` on lookup miss.

Stand-alone primitive — does not import the registry, daemon, or HTTP
layers. PR-4b-2 (signer service) composes registry + this store.
"""

from __future__ import annotations

import os
import sqlite3
import time
from collections.abc import Iterable
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from pinky_identity.keys import SigningKeypair
from pinky_identity.keystore import (
    DeviceKey,
    WrappedKeypair,
    unwrap_keypair,
    wrap_keypair,
)

# -- Constants ---------------------------------------------------------------

#: Default on-disk path for the signer store. Distinct from the registry's
#: ``data/pinky_identity_registry.db`` — the registry holds public-half
#: lifecycle state; this file holds the encrypted secret seeds. Co-locating
#: them in the same DB would make a single read-only file leak both.
DEFAULT_SIGNER_STORE_PATH = "data/identity/signer_store.db"


def _lock_owner_only(db_path: Path) -> None:
    """Restrict this secret store (and its SQLite sidecars) to owner-only.

    The file holds encrypted key material and must never be world-readable.
    Best-effort: the daemon's startup sweep
    (:mod:`pinky_daemon.db_security`) is the primary guard; locking at
    creation also covers instantiation outside the daemon (tests, future
    standalone use). chmod failures are swallowed — a slightly-too-open
    file beats a crash on a platform without chmod.
    """
    for suffix in ("", "-wal", "-shm", "-journal"):
        try:
            os.chmod(db_path.with_name(db_path.name + suffix), 0o600)
        except OSError:
            pass


# -- Errors ------------------------------------------------------------------


class SignerStoreError(KeyError):
    """Raised on lookup misses or persistence failures.

    Subclass of :class:`KeyError` so generic ``except KeyError`` clauses
    catch the common "no key for this kid" case. Decryption failures
    bubble up as :class:`pinky_identity.keystore.KeystoreError` from
    :func:`unwrap_keypair` — the caller can catch
    :class:`pinky_identity.SignatureError` for both.
    """


# -- Row dataclass -----------------------------------------------------------


@dataclass(frozen=True)
class SignerStoreRow:
    """A persisted row in the signer store.

    Fields mirror :class:`WrappedKeypair` plus ``created_at``. The dataclass
    is exposed for diagnostics / admin tooling; routine signing paths
    should go through :meth:`EncryptedSignerStore.get_signing_keypair`
    which returns a fully unwrapped :class:`SigningKeypair`.
    """

    kid: str
    fingerprint: str
    public_key_raw: bytes
    wrap_version: int
    nonce: bytes
    ciphertext: bytes
    created_at: float

    def to_wrapped(self) -> WrappedKeypair:
        return WrappedKeypair(
            version=self.wrap_version,
            kid=self.kid,
            fingerprint=self.fingerprint,
            public_key_raw=self.public_key_raw,
            nonce=self.nonce,
            ciphertext=self.ciphertext,
        )

    def __repr__(self) -> str:
        return (
            f"SignerStoreRow(kid={self.kid!r}, "
            f"fingerprint={self.fingerprint[:12]!r}…, "
            f"wrap_version={self.wrap_version}, "
            f"ciphertext=<{len(self.ciphertext)} bytes redacted>, "
            f"created_at={self.created_at})"
        )


# -- Store -------------------------------------------------------------------


class EncryptedSignerStore:
    """Persists :class:`WrappedKeypair` rows in SQLite, indexed by kid.

    Construction creates the schema if needed. The :class:`DeviceKey` is
    held for the lifetime of the store — wraps and unwraps go through it.

    Concurrency: single-process. SQLite is opened with WAL mode for
    crash-consistency and to allow concurrent readers (e.g. a debug query
    while signing). Cross-process use is not supported — only one daemon
    should own this file.
    """

    __slots__ = ("_db_path", "_db", "_device_key")

    def __init__(
        self,
        *,
        db_path: str | Path = DEFAULT_SIGNER_STORE_PATH,
        device_key: DeviceKey,
    ) -> None:
        if not isinstance(device_key, DeviceKey):
            raise TypeError("device_key must be a DeviceKey")
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._device_key = device_key
        self._db = sqlite3.connect(
            str(self._db_path), isolation_level=None, check_same_thread=False
        )
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._ensure_schema()
        _lock_owner_only(self._db_path)

    # -- schema --

    def _ensure_schema(self) -> None:
        # CREATE TABLE IF NOT EXISTS — idempotent across daemon restarts.
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS signing_keystore (
                kid           TEXT PRIMARY KEY,
                fingerprint   TEXT UNIQUE NOT NULL,
                public_key    BLOB NOT NULL,
                wrap_version  INTEGER NOT NULL,
                nonce         BLOB NOT NULL,
                ciphertext    BLOB NOT NULL,
                created_at    REAL NOT NULL
            )
            """
        )
        self._db.execute(
            "CREATE INDEX IF NOT EXISTS signing_keystore_fingerprint_idx "
            "ON signing_keystore (fingerprint)"
        )

    # -- writes --

    def put_wrapped(self, wrapped: WrappedKeypair) -> SignerStoreRow:
        """Persist a pre-wrapped :class:`WrappedKeypair`.

        Idempotent on ``kid``: re-puts overwrite the existing row. Useful
        for key-rotation flows that re-wrap with a new device key.
        """
        if not isinstance(wrapped, WrappedKeypair):
            raise TypeError("wrapped must be a WrappedKeypair")
        now = time.time()
        with self._txn():
            self._db.execute(
                """
                INSERT INTO signing_keystore
                    (kid, fingerprint, public_key, wrap_version, nonce,
                     ciphertext, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(kid) DO UPDATE SET
                    fingerprint=excluded.fingerprint,
                    public_key=excluded.public_key,
                    wrap_version=excluded.wrap_version,
                    nonce=excluded.nonce,
                    ciphertext=excluded.ciphertext
                """,
                (
                    wrapped.kid,
                    wrapped.fingerprint,
                    wrapped.public_key_raw,
                    wrapped.version,
                    wrapped.nonce,
                    wrapped.ciphertext,
                    now,
                ),
            )
        return SignerStoreRow(
            kid=wrapped.kid,
            fingerprint=wrapped.fingerprint,
            public_key_raw=wrapped.public_key_raw,
            wrap_version=wrapped.version,
            nonce=wrapped.nonce,
            ciphertext=wrapped.ciphertext,
            created_at=now,
        )

    def put_keypair(self, keypair: SigningKeypair) -> SignerStoreRow:
        """Wrap *keypair* under the store's device key and persist.

        Convenience wrapper around :func:`wrap_keypair` + :meth:`put_wrapped`
        for the common "fresh keypair" path.
        """
        if not isinstance(keypair, SigningKeypair):
            raise TypeError("keypair must be a SigningKeypair")
        wrapped = wrap_keypair(keypair, device_key=self._device_key)
        return self.put_wrapped(wrapped)

    # -- reads --

    def has(self, kid: str) -> bool:
        row = self._db.execute(
            "SELECT 1 FROM signing_keystore WHERE kid = ?", (kid,)
        ).fetchone()
        return row is not None

    def get_row(self, kid: str) -> SignerStoreRow:
        """Return the raw stored row for *kid* without decrypting.

        Raises :class:`SignerStoreError` if no row exists. Useful for
        admin tooling that wants to inspect storage state without
        triggering an unwrap.
        """
        row = self._db.execute(
            "SELECT kid, fingerprint, public_key, wrap_version, nonce, "
            "ciphertext, created_at FROM signing_keystore WHERE kid = ?",
            (kid,),
        ).fetchone()
        if row is None:
            raise SignerStoreError(f"no signing key stored for kid {kid!r}")
        return SignerStoreRow(
            kid=row["kid"],
            fingerprint=row["fingerprint"],
            public_key_raw=bytes(row["public_key"]),
            wrap_version=int(row["wrap_version"]),
            nonce=bytes(row["nonce"]),
            ciphertext=bytes(row["ciphertext"]),
            created_at=float(row["created_at"]),
        )

    def get_signing_keypair(self, kid: str) -> SigningKeypair:
        """Decrypt and return the :class:`SigningKeypair` for *kid*.

        Raises
        ------
        SignerStoreError
            If no row is stored under *kid*.
        pinky_identity.keystore.KeystoreError
            If the stored row fails to decrypt under the store's device
            key (wrong device key, corrupt blob, version mismatch).
        """
        row = self.get_row(kid)
        return unwrap_keypair(row.to_wrapped(), device_key=self._device_key)

    def get_row_by_fingerprint(self, fingerprint_hex: str) -> SignerStoreRow:
        """Return the row whose full SHA-256 fingerprint matches.

        kid is short (16 hex) and a registry collision check uses the full
        fingerprint. Lookup-by-fingerprint is provided for admin tooling
        that wants the cryptographic identifier rather than the short id.
        """
        row = self._db.execute(
            "SELECT kid, fingerprint, public_key, wrap_version, nonce, "
            "ciphertext, created_at FROM signing_keystore WHERE fingerprint = ?",
            (fingerprint_hex,),
        ).fetchone()
        if row is None:
            raise SignerStoreError(
                f"no signing key stored for fingerprint {fingerprint_hex!r}"
            )
        return SignerStoreRow(
            kid=row["kid"],
            fingerprint=row["fingerprint"],
            public_key_raw=bytes(row["public_key"]),
            wrap_version=int(row["wrap_version"]),
            nonce=bytes(row["nonce"]),
            ciphertext=bytes(row["ciphertext"]),
            created_at=float(row["created_at"]),
        )

    def list_kids(self) -> list[str]:
        """Return all stored kids, sorted lexicographically."""
        rows = self._db.execute(
            "SELECT kid FROM signing_keystore ORDER BY kid"
        ).fetchall()
        return [r["kid"] for r in rows]

    def iter_rows(self) -> Iterable[SignerStoreRow]:
        """Iterate over all rows. Caller-friendly — yields one row at a time."""
        cursor = self._db.execute(
            "SELECT kid, fingerprint, public_key, wrap_version, nonce, "
            "ciphertext, created_at FROM signing_keystore ORDER BY kid"
        )
        for row in cursor:
            yield SignerStoreRow(
                kid=row["kid"],
                fingerprint=row["fingerprint"],
                public_key_raw=bytes(row["public_key"]),
                wrap_version=int(row["wrap_version"]),
                nonce=bytes(row["nonce"]),
                ciphertext=bytes(row["ciphertext"]),
                created_at=float(row["created_at"]),
            )

    # -- deletes --

    def delete(self, kid: str) -> bool:
        """Remove the row for *kid*. Returns True if a row was deleted.

        Best-effort cleanup. The registry's revoke/retire is the
        authoritative lifecycle event; this method just clears the
        encrypted seed so even a device-key compromise can't reanimate
        the agent.
        """
        with self._txn():
            cursor = self._db.execute(
                "DELETE FROM signing_keystore WHERE kid = ?", (kid,)
            )
            return cursor.rowcount > 0

    # -- bookkeeping --

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> "EncryptedSignerStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @contextmanager
    def _txn(self):
        # isolation_level=None means autocommit; we wrap explicit BEGIN/COMMIT
        # so multi-statement writes are atomic on this connection.
        self._db.execute("BEGIN")
        try:
            yield
            self._db.execute("COMMIT")
        except Exception:
            self._db.execute("ROLLBACK")
            raise

    def __repr__(self) -> str:
        return (
            f"EncryptedSignerStore(db_path={str(self._db_path)!r}, "
            f"device_key={self._device_key!r})"
        )


__all__ = [
    "DEFAULT_SIGNER_STORE_PATH",
    "EncryptedSignerStore",
    "SignerStoreError",
    "SignerStoreRow",
]
