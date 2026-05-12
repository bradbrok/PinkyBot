"""Encrypted-at-rest wrapping for service-auth signing keys.

This module is the crypto primitive layer the registry (PR-3) and the
daemon-local signer (PR-4b) use to persist Ed25519 signing seeds without
leaving plaintext key material on disk.

Design rules (see ``docs/design/agent-asymmetric-identity.md`` and #461):

- **One device key per host.** A 32-byte symmetric secret persisted at
  ``data/identity/.device_key`` with mode ``0600``. Generated lazily on
  first use. Never copied off the host. Intentionally distinct from the
  :mod:`pinky_federation` device key — different purpose, different blast
  radius, no cross-purpose reuse.
- **AEAD: XChaCha20-Poly1305.** Same primitive as federation; PyNaCl
  bindings (the :mod:`cryptography` package doesn't expose XChaCha
  directly). 24-byte random nonce.
- **AAD binds (purpose, version, kid, fingerprint).** A wrapped blob from
  one agent can't be replayed under a different identity — the registry
  unwraps with the kid/fingerprint it stored alongside, and a tampered
  metadata column will fail the AEAD tag.
- **Wrapped blob carries only the 32-byte seed.** The public half is
  derivable from the seed and is stored unencrypted in the registry for
  fast lookups (no need to unwrap just to list keys).
- **No logging of secret material.** ``__repr__`` never shows ciphertext or
  device-key bytes beyond a 4-byte hex tag for debugging.
- **Discipline-aware exports.** Raw-seed exit points are explicitly named
  (``_seed_insecure``) so reviewers and grep can find every place plaintext
  leaves the wrapper.

This module is storage-agnostic. It returns :class:`WrappedKeypair`
dataclasses; the registry (PR-3) is responsible for persisting them into
SQLite or wherever else.
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
from nacl.signing import SigningKey as _NaclSigningKey

from pinky_identity.keys import (
    ED25519_SECRET_KEY_BYTES,
    PublicKey,
    SecretKey,
    SignatureError,
    SigningKeypair,
    fingerprint,
    kid_from_public_key,
    public_key_from_bytes,
)

# -- Constants ---------------------------------------------------------------

#: Length of the device key (a 256-bit symmetric secret).
DEVICE_KEY_BYTES = 32

#: Default on-disk path for the device key. Operators can override via
#: :meth:`DeviceKey.load_or_create`. Distinct from ``data/federation/...``
#: so the two systems' KEKs rotate independently.
DEFAULT_DEVICE_KEY_PATH = "data/identity/.device_key"

#: Current wrap envelope version. Bump if AAD layout or cipher changes —
#: :func:`unwrap_keypair` will refuse to decrypt unknown versions.
WRAP_VERSION = 1

#: AAD prefix pins the purpose. A blob from a different system that
#: happens to share the device key (it shouldn't) still fails the AEAD tag.
_WRAP_AAD_PREFIX = b"pinky-identity/v1/service-auth-seed"


# -- Errors ------------------------------------------------------------------


class KeystoreError(SignatureError):
    """Raised for wrap/unwrap failures. Subclass of SignatureError so a
    single ``except SignatureError`` clause covers identity-layer errors.
    """


class WrapVersionError(KeystoreError):
    """Raised when a wrapped blob's version is unknown to this build."""


# -- Helpers -----------------------------------------------------------------


def _xchacha_nonce() -> bytes:
    """Return a fresh 24-byte XChaCha20-Poly1305 nonce."""
    return secrets.token_bytes(crypto_aead_xchacha20poly1305_ietf_NPUBBYTES)


def _seed_from_keypair(keypair: SigningKeypair) -> bytes:
    """Return the 32-byte seed for *keypair*.

    Verbose internal helper rather than poking ``keypair.secret`` directly
    so grep finds all secret extraction sites.
    """
    return keypair.secret.secret_bytes_insecure()


def _aad_for(*, version: int, kid: str, fingerprint_hex: str) -> bytes:
    """Build the AAD for a wrap envelope.

    Binds the ciphertext to (purpose, version, kid, fingerprint). A wrap
    minted for one identity cannot be replayed under another — even if an
    attacker substitutes the ciphertext blob, the AAD mismatch will fail
    the AEAD tag.
    """
    return (
        _WRAP_AAD_PREFIX
        + b"|v="
        + str(version).encode("ascii")
        + b"|kid="
        + kid.encode("ascii")
        + b"|fp="
        + fingerprint_hex.encode("ascii")
    )


# -- Device key --------------------------------------------------------------


@dataclass(frozen=True)
class DeviceKey:
    """Device-local 32-byte symmetric KEK for wrapping signing seeds.

    The bytes are accessible to the wrap/unwrap layer via
    :meth:`material_insecure`. ``__repr__`` never reveals them in full.
    """

    _material: bytes

    def __post_init__(self) -> None:
        if not isinstance(self._material, bytes) or len(self._material) != DEVICE_KEY_BYTES:
            raise ValueError(f"device key must be {DEVICE_KEY_BYTES} bytes")

    def material_insecure(self) -> bytes:
        """Raw 32-byte device key. Never log this value."""
        return self._material

    def __repr__(self) -> str:
        return f"DeviceKey(<{self._material[:4].hex()}…>)"

    @classmethod
    def load_or_create(cls, path: str = DEFAULT_DEVICE_KEY_PATH) -> "DeviceKey":
        """Load the device key from *path*, generating it on first use.

        File is created with mode ``0600`` (owner read/write only). On
        existing files we sanity-check size and refuse if permissions are
        looser than 0600 — we do NOT silently tighten, because that may
        mask a real security problem (e.g. a backup script that copied the
        file world-readable).
        """
        p = Path(path)
        if p.exists():
            data = p.read_bytes()
            if len(data) != DEVICE_KEY_BYTES:
                raise ValueError(
                    f"device key at {path!r} is {len(data)} bytes, "
                    f"expected {DEVICE_KEY_BYTES}"
                )
            mode = stat.S_IMODE(p.stat().st_mode)
            if mode & 0o077:
                raise PermissionError(
                    f"device key {path!r} has loose permissions {oct(mode)}; "
                    "expected 0o600"
                )
            return cls(data)
        p.parent.mkdir(parents=True, exist_ok=True)
        material = secrets.token_bytes(DEVICE_KEY_BYTES)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        fd = os.open(str(p), flags, 0o600)
        try:
            os.write(fd, material)
        finally:
            os.close(fd)
        return cls(material)

    @classmethod
    def from_bytes(cls, material: bytes) -> "DeviceKey":
        """Construct a DeviceKey from raw bytes.

        Useful for tests and for callers who manage the KEK out-of-band
        (e.g. operator-provided env var). Production deploys should
        prefer :meth:`load_or_create`.
        """
        return cls(bytes(material))


# -- Wrapped keypair ---------------------------------------------------------


@dataclass(frozen=True)
class WrappedKeypair:
    """An encrypted-at-rest envelope for a service-auth signing keypair.

    Carries everything needed to recover the keypair given the device
    key, plus the public-key metadata that storage layers want to expose
    without decrypting (kid, fingerprint, raw public bytes).

    Fields:

    - ``version``: wrap envelope version. Currently 1.
    - ``kid``: short key id (deterministic from the public key).
    - ``fingerprint``: full SHA-256 hex fingerprint of the public key.
    - ``public_key_raw``: raw 32 bytes of the public key. Stored
      unencrypted because (a) the public half is, by definition, public,
      and (b) the registry needs to list/lookup without unwrapping.
    - ``nonce``: 24-byte XChaCha20-Poly1305 nonce.
    - ``ciphertext``: AEAD-encrypted 32-byte seed (16-byte tag appended,
      so total length is 48 bytes).

    The dataclass is frozen and never logs secret material in ``__repr__``.
    """

    version: int
    kid: str
    fingerprint: str
    public_key_raw: bytes
    nonce: bytes
    ciphertext: bytes

    def __repr__(self) -> str:
        return (
            f"WrappedKeypair(version={self.version}, kid={self.kid!r}, "
            f"fingerprint={self.fingerprint[:12]!r}…, "
            f"ciphertext=<{len(self.ciphertext)} bytes redacted>)"
        )

    def public(self) -> PublicKey:
        """Return the public half without unwrapping."""
        return public_key_from_bytes(self.public_key_raw)


# -- Wrap / unwrap -----------------------------------------------------------


def wrap_keypair(
    keypair: SigningKeypair,
    *,
    device_key: DeviceKey,
) -> WrappedKeypair:
    """Encrypt *keypair*'s secret seed under *device_key*.

    The returned :class:`WrappedKeypair` carries the public-key metadata
    in cleartext (kid, fingerprint, raw public bytes) and the encrypted
    seed in :attr:`WrappedKeypair.ciphertext`. AAD binds (purpose,
    version, kid, fingerprint) so the blob cannot be replayed under a
    different identity.
    """
    if not isinstance(keypair, SigningKeypair):
        raise TypeError("keypair must be a SigningKeypair")
    if not isinstance(device_key, DeviceKey):
        raise TypeError("device_key must be a DeviceKey")

    kid = kid_from_public_key(keypair.public)
    fp = fingerprint(keypair.public)
    nonce = _xchacha_nonce()
    aad = _aad_for(version=WRAP_VERSION, kid=kid, fingerprint_hex=fp)
    seed = _seed_from_keypair(keypair)
    try:
        ct = crypto_aead_xchacha20poly1305_ietf_encrypt(
            seed, aad, nonce, device_key.material_insecure()
        )
    finally:
        # Drop our binding to the seed copy as soon as the AEAD is done.
        # Python doesn't let us truly zero memory; this is the best we can
        # do without going through ctypes.
        del seed

    return WrappedKeypair(
        version=WRAP_VERSION,
        kid=kid,
        fingerprint=fp,
        public_key_raw=keypair.public.raw,
        nonce=nonce,
        ciphertext=ct,
    )


def unwrap_keypair(
    wrapped: WrappedKeypair,
    *,
    device_key: DeviceKey,
) -> SigningKeypair:
    """Decrypt *wrapped* into a :class:`SigningKeypair`.

    Raises
    ------
    WrapVersionError
        If :attr:`WrappedKeypair.version` is not understood by this build.
    KeystoreError
        On any AEAD failure: corrupt ciphertext, wrong device key,
        tampered AAD (kid/fingerprint substitution), wrong-length
        plaintext.
    """
    if not isinstance(wrapped, WrappedKeypair):
        raise TypeError("wrapped must be a WrappedKeypair")
    if not isinstance(device_key, DeviceKey):
        raise TypeError("device_key must be a DeviceKey")
    if wrapped.version != WRAP_VERSION:
        raise WrapVersionError(
            f"unsupported wrap version {wrapped.version} "
            f"(this build supports {WRAP_VERSION})"
        )

    aad = _aad_for(
        version=wrapped.version,
        kid=wrapped.kid,
        fingerprint_hex=wrapped.fingerprint,
    )
    try:
        seed = crypto_aead_xchacha20poly1305_ietf_decrypt(
            wrapped.ciphertext,
            aad,
            wrapped.nonce,
            device_key.material_insecure(),
        )
    except Exception as exc:  # noqa: BLE001 — normalize to our error type
        raise KeystoreError(
            f"failed to decrypt wrapped keypair for kid {wrapped.kid!r}"
        ) from exc

    if len(seed) != ED25519_SECRET_KEY_BYTES:
        raise KeystoreError(
            f"decrypted seed has wrong length {len(seed)} "
            f"(expected {ED25519_SECRET_KEY_BYTES})"
        )

    # Reconstruct the public half from the seed and cross-check against
    # the cleartext metadata. If the registry's public_key_raw was
    # swapped for another key, the AAD check above would have failed —
    # but belt and suspenders.
    pub_bytes = bytes(_NaclSigningKey(seed).verify_key.encode())
    if pub_bytes != wrapped.public_key_raw:
        # Should never happen if AAD was honest. Treat as corruption.
        raise KeystoreError(
            "public key in wrapper does not match the key derived from "
            "the decrypted seed"
        )

    return SigningKeypair(
        secret=SecretKey(seed),
        public=PublicKey(pub_bytes),
    )


# -- Serialization ----------------------------------------------------------


def serialize_wrapped(wrapped: WrappedKeypair) -> dict:
    """Return a JSON-serializable dict for a :class:`WrappedKeypair`.

    Binary fields (``nonce``, ``ciphertext``, ``public_key_raw``) are
    hex-encoded so the result survives JSON / SQLite TEXT columns. The
    registry is free to store the binary fields as BLOB instead — this
    helper is for transport (e.g. enrollment-response payloads) and
    debugging.
    """
    return {
        "version": wrapped.version,
        "kid": wrapped.kid,
        "fingerprint": wrapped.fingerprint,
        "public_key_hex": wrapped.public_key_raw.hex(),
        "nonce_hex": wrapped.nonce.hex(),
        "ciphertext_hex": wrapped.ciphertext.hex(),
    }


def deserialize_wrapped(blob: dict) -> WrappedKeypair:
    """Inverse of :func:`serialize_wrapped`.

    Raises ``ValueError`` if required fields are missing or malformed.
    Does not perform any crypto — that's :func:`unwrap_keypair`'s job.
    """
    try:
        return WrappedKeypair(
            version=int(blob["version"]),
            kid=str(blob["kid"]),
            fingerprint=str(blob["fingerprint"]),
            public_key_raw=bytes.fromhex(blob["public_key_hex"]),
            nonce=bytes.fromhex(blob["nonce_hex"]),
            ciphertext=bytes.fromhex(blob["ciphertext_hex"]),
        )
    except KeyError as e:
        raise ValueError(f"wrapped blob missing required field: {e}") from e
    except (TypeError, ValueError) as e:
        raise ValueError(f"wrapped blob is malformed: {e}") from e


__all__ = [
    "DEFAULT_DEVICE_KEY_PATH",
    "DEVICE_KEY_BYTES",
    "DeviceKey",
    "KeystoreError",
    "WRAP_VERSION",
    "WrapVersionError",
    "WrappedKeypair",
    "deserialize_wrapped",
    "serialize_wrapped",
    "unwrap_keypair",
    "wrap_keypair",
]
