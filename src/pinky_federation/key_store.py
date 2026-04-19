"""Encrypted at-rest persistence for tenant signing keys.

The tenant signing key (Ed25519) is the *identity* of a tenant. If it leaks,
an attacker can impersonate the tenant indefinitely (federation v0.2 has no
escrow, no recovery — by Brad's design decision Q3, key loss means starting
over). This module exists so that key never sits on disk in plaintext.

Layered design:

- :class:`DeviceKey` — a 32-byte symmetric secret persisted at
  ``data/federation/.device_key`` with mode ``0600``. Generated on first use
  on this device; never copied off. This is the encryption key for tenant
  signing seeds.
- :class:`EncryptedTenantKeyStore` — uses a :class:`DeviceKey` to write
  tenant signing seeds (32 bytes) into the ``tenant_signing_keys`` table
  created by :mod:`pinky_federation.state`, encrypted with
  XChaCha20-Poly1305. Reads decrypt only on explicit request.

Discipline:

- The seed is **never** logged. ``__repr__`` on this class never shows seed
  bytes. Errors talk about "decrypt failure" without exposing material.
- The seed is **never** included in :meth:`FederationStateStore.stats` or
  any diagnostics; that table contributes a row count only.
- Export of the raw seed is gated behind
  :meth:`export_signing_seed_explicit` which requires a positional
  acknowledgement flag — defensive against accidental "give me the key"
  flows from generic settings/debug APIs.

This layer does **not** touch ``instance_keys.encrypted_secret`` — those
per-device receive keys have their own lifecycle and rotate freely. The
device key here is also reusable for that table if a caller wants to
encrypt instance secrets the same way (recommended).
"""

from __future__ import annotations

import os
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path

from nacl.bindings import (
    crypto_aead_xchacha20poly1305_ietf_decrypt,
    crypto_aead_xchacha20poly1305_ietf_encrypt,
    crypto_aead_xchacha20poly1305_ietf_NPUBBYTES,
)

from pinky_federation.errors import DecryptionError
from pinky_federation.keys import ED25519_KEY_BYTES, SigningKeyPair
from pinky_federation.state import FederationStateStore

DEVICE_KEY_BYTES = 32
DEFAULT_DEVICE_KEY_PATH = "data/federation/.device_key"

# AAD prefix binds ciphertext to its purpose so a raw blob from one
# table can't be replayed against another decrypt path.
_TENANT_SIGNING_AAD_PREFIX = b"pinky-fed/v1/tenant-signing-seed"
_KDF_VERSION = 1  # bumped if we change AAD layout / cipher choice


def _xchacha_nonce() -> bytes:
    """Return a fresh 24-byte XChaCha20-Poly1305 nonce."""
    return secrets.token_bytes(crypto_aead_xchacha20poly1305_ietf_NPUBBYTES)


# -- Device key ------------------------------------------------------------


@dataclass(frozen=True)
class DeviceKey:
    """A device-local 32-byte symmetric secret.

    This wrapper never serializes its bytes via ``repr``. The bytes are
    accessible via :meth:`material_insecure` for the encryption layer.
    """

    _material: bytes

    def __post_init__(self) -> None:
        if not isinstance(self._material, bytes) or len(self._material) != DEVICE_KEY_BYTES:
            raise ValueError(f"device key must be {DEVICE_KEY_BYTES} bytes")

    def material_insecure(self) -> bytes:
        """Raw 32-byte device key. Never log this value."""
        return self._material

    def __repr__(self) -> str:
        # Show a 4-byte tag for log debugging; never the full key.
        return f"DeviceKey(<{self._material[:4].hex()}…>)"

    @classmethod
    def load_or_create(cls, path: str = DEFAULT_DEVICE_KEY_PATH) -> DeviceKey:
        """Load the device key from *path*, generating it on first use.

        The file is created with mode ``0600`` (owner read/write only). On
        existing files we sanity-check size and warn (via raised error) if
        permissions are looser than ``0600`` — we do NOT silently tighten,
        because that may mask a real security problem.
        """
        p = Path(path)
        if p.exists():
            data = p.read_bytes()
            if len(data) != DEVICE_KEY_BYTES:
                raise ValueError(
                    f"device key at {path!r} is {len(data)} bytes, expected {DEVICE_KEY_BYTES}"
                )
            mode = stat.S_IMODE(p.stat().st_mode)
            # Owner-only; reject if group/other can read or write.
            if mode & 0o077:
                raise PermissionError(
                    f"device key {path!r} has loose permissions {oct(mode)}; expected 0o600"
                )
            return cls(data)
        p.parent.mkdir(parents=True, exist_ok=True)
        material = secrets.token_bytes(DEVICE_KEY_BYTES)
        # Write with restrictive permissions atomically.
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        fd = os.open(str(p), flags, 0o600)
        try:
            os.write(fd, material)
        finally:
            os.close(fd)
        return cls(material)


# -- Encrypted tenant key store --------------------------------------------


@dataclass(frozen=True)
class _StoredSeed:
    encrypted_seed: bytes
    nonce: bytes
    kdf_version: int


class EncryptedTenantKeyStore:
    """Persists tenant signing seeds encrypted with the device key."""

    __slots__ = ("_state", "_device_key")

    def __init__(self, state: FederationStateStore, device_key: DeviceKey) -> None:
        if not isinstance(state, FederationStateStore):
            raise TypeError("state must be a FederationStateStore")
        if not isinstance(device_key, DeviceKey):
            raise TypeError("device_key must be a DeviceKey")
        self._state = state
        self._device_key = device_key

    # -- writes --------------------------------------------------------

    def put_signing_key(self, tenant_id: str, signing_keypair: SigningKeyPair) -> None:
        """Encrypt and persist the seed for *signing_keypair* under *tenant_id*.

        Raises ``ValueError`` if the tenant row does not yet exist — the
        caller must have inserted into :meth:`FederationStateStore.upsert_tenant`
        first so foreign keys hold.
        """
        if not isinstance(tenant_id, str) or not tenant_id:
            raise ValueError("tenant_id must be a non-empty string")
        if not isinstance(signing_keypair, SigningKeyPair):
            raise TypeError("signing_keypair must be a SigningKeyPair")
        if self._state.get_tenant(tenant_id) is None:
            raise ValueError(
                f"tenant {tenant_id!r} not registered; insert into tenants first"
            )
        seed = signing_keypair.seed_bytes_insecure()
        try:
            stored = self._encrypt_seed(tenant_id, seed)
            self._state._db.execute(  # noqa: SLF001 — intentional cross-module write
                """
                INSERT INTO tenant_signing_keys
                    (tenant_id, encrypted_seed, nonce, kdf_version, created_at)
                VALUES (?, ?, ?, ?, strftime('%s','now'))
                ON CONFLICT(tenant_id) DO UPDATE SET
                    encrypted_seed=excluded.encrypted_seed,
                    nonce=excluded.nonce,
                    kdf_version=excluded.kdf_version
                """,
                (tenant_id, stored.encrypted_seed, stored.nonce, stored.kdf_version),
            )
            self._state._db.commit()  # noqa: SLF001
        finally:
            # Best-effort wipe of the seed copy we just used. Python doesn't
            # let us truly zero memory but we drop the binding immediately.
            del seed

    # -- reads ---------------------------------------------------------

    def has_signing_key(self, tenant_id: str) -> bool:
        row = self._state._db.execute(  # noqa: SLF001
            "SELECT 1 FROM tenant_signing_keys WHERE tenant_id = ?", (tenant_id,)
        ).fetchone()
        return row is not None

    def get_signing_key(self, tenant_id: str) -> SigningKeyPair:
        """Decrypt and return the signing keypair for *tenant_id*.

        Raises :class:`KeyError` if no key is stored, and
        :class:`pinky_federation.errors.DecryptionError` if the on-disk
        ciphertext is corrupt or was encrypted under a different device key.
        """
        stored = self._load_stored_seed(tenant_id)
        seed = self._decrypt_seed(tenant_id, stored)
        try:
            return SigningKeyPair.from_seed(seed)
        finally:
            del seed

    def export_signing_seed_explicit(
        self,
        tenant_id: str,
        *,
        operator_acknowledged: bool,
        reason: str,
    ) -> bytes:
        """Return the raw 32-byte seed.

        This is the only path that yields raw key material outside the
        :class:`SigningKeyPair` wrapper. It is intentionally awkward:

        - ``operator_acknowledged`` must be passed positionally as a
          keyword arg (``True``).
        - ``reason`` must be a non-empty string and is logged via the audit
          channel by callers (this layer does not log).

        Generic settings/debug APIs that just want "give me everything"
        should never call this — they should call :meth:`get_signing_key`
        and never expose seed bytes.
        """
        if operator_acknowledged is not True:
            raise PermissionError(
                "export_signing_seed_explicit requires operator_acknowledged=True"
            )
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("export_signing_seed_explicit requires a non-empty reason")
        stored = self._load_stored_seed(tenant_id)
        return self._decrypt_seed(tenant_id, stored)

    # -- crypto internals ---------------------------------------------

    def _encrypt_seed(self, tenant_id: str, seed: bytes) -> _StoredSeed:
        if not isinstance(seed, bytes) or len(seed) != ED25519_KEY_BYTES:
            raise ValueError(f"signing seed must be {ED25519_KEY_BYTES} bytes")
        nonce = _xchacha_nonce()
        aad = self._aad_for(tenant_id)
        ct = crypto_aead_xchacha20poly1305_ietf_encrypt(
            seed, aad, nonce, self._device_key.material_insecure()
        )
        return _StoredSeed(encrypted_seed=ct, nonce=nonce, kdf_version=_KDF_VERSION)

    def _decrypt_seed(self, tenant_id: str, stored: _StoredSeed) -> bytes:
        if stored.kdf_version != _KDF_VERSION:
            raise DecryptionError(
                f"unsupported kdf_version {stored.kdf_version} (this build supports {_KDF_VERSION})"
            )
        aad = self._aad_for(tenant_id)
        try:
            pt = crypto_aead_xchacha20poly1305_ietf_decrypt(
                stored.encrypted_seed, aad, stored.nonce, self._device_key.material_insecure()
            )
        except Exception as exc:  # noqa: BLE001 — normalize to our error type
            raise DecryptionError(
                f"failed to decrypt signing seed for tenant {tenant_id!r}"
            ) from exc
        if len(pt) != ED25519_KEY_BYTES:
            raise DecryptionError(
                f"decrypted seed has wrong length: {len(pt)} != {ED25519_KEY_BYTES}"
            )
        return pt

    def _load_stored_seed(self, tenant_id: str) -> _StoredSeed:
        row = self._state._db.execute(  # noqa: SLF001
            "SELECT encrypted_seed, nonce, kdf_version FROM tenant_signing_keys "
            "WHERE tenant_id = ?",
            (tenant_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"no signing key stored for tenant {tenant_id!r}")
        return _StoredSeed(
            encrypted_seed=bytes(row["encrypted_seed"]),
            nonce=bytes(row["nonce"]),
            kdf_version=int(row["kdf_version"]),
        )

    @staticmethod
    def _aad_for(tenant_id: str) -> bytes:
        # Bind ciphertext to (purpose, tenant_id) so a row from one tenant
        # cannot be replayed under a different tenant_id.
        return _TENANT_SIGNING_AAD_PREFIX + b"|" + tenant_id.encode("utf-8")

    # -- discipline ---------------------------------------------------

    def __repr__(self) -> str:
        return (
            f"EncryptedTenantKeyStore(state={self._state!r}, device_key={self._device_key!r})"
        )
