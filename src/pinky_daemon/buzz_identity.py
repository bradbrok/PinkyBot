"""Encrypted-at-rest Buzz (Nostr/secp256k1) identity primitives.

The agent registry owns lifecycle and persistence.  This module owns only the
secret envelope and deliberately has no DTO or logging surface for raw key
material.
"""

from __future__ import annotations

import hmac
import re
import secrets
from dataclasses import dataclass

from nacl.bindings import (
    crypto_aead_xchacha20poly1305_ietf_decrypt,
    crypto_aead_xchacha20poly1305_ietf_encrypt,
    crypto_aead_xchacha20poly1305_ietf_NPUBBYTES,
)

from pinky_identity.keystore import DeviceKey

BUZZ_WRAP_VERSION = 1
BUZZ_PRIVATE_KEY_BYTES = 32
_BUZZ_AAD_PREFIX = b"pinky-identity/v1/buzz-secp256k1"
_HEX_64_RE = re.compile(r"^[0-9a-f]{64}$")


class BuzzIdentityError(ValueError):
    """Base error for invalid or unusable Buzz identity material."""


class BuzzIdentityUnhealthyError(BuzzIdentityError):
    """The stored envelope cannot be trusted or decrypted."""


class BuzzDependencyError(BuzzIdentityError):
    """A required Buzz runtime dependency is missing."""


@dataclass(frozen=True)
class BuzzKeyEnvelope:
    """Encrypted private-key envelope; repr never exposes secret bytes."""

    agent: str
    pubkey: str
    wrap_version: int
    nonce: bytes
    ciphertext: bytes

    def __repr__(self) -> str:
        return (
            f"BuzzKeyEnvelope(agent={self.agent!r}, pubkey={self.pubkey!r}, "
            f"wrap_version={self.wrap_version}, "
            f"ciphertext=<{len(self.ciphertext)} bytes redacted>)"
        )


@dataclass(frozen=True)
class BuzzSigningMaterial:
    """Internal-only unwrapped identity handed directly to the sender."""

    agent: str
    pubkey: str
    private_key: bytes
    relay_url: str
    community_id: str

    def __repr__(self) -> str:
        return (
            f"BuzzSigningMaterial(agent={self.agent!r}, pubkey={self.pubkey!r}, "
            f"relay_url={self.relay_url!r}, community_id={self.community_id!r}, "
            "private_key=<redacted>)"
        )


def _coincurve():
    try:
        import coincurve
    except ImportError as exc:  # pragma: no cover - exercised by boot dependency probe
        raise BuzzDependencyError("Buzz secp256k1 dependency is unavailable") from exc
    return coincurve


def normalize_private_key(value: str | bytes) -> bytes:
    """Return one valid 32-byte secp256k1 secret without echoing bad input."""
    if isinstance(value, bytes):
        raw = bytes(value)
    elif isinstance(value, str) and _HEX_64_RE.fullmatch(value.strip().lower()):
        raw = bytes.fromhex(value.strip())
    else:
        raise BuzzIdentityError("Buzz private key must be 64 hexadecimal characters")
    if len(raw) != BUZZ_PRIVATE_KEY_BYTES:
        raise BuzzIdentityError("Buzz private key must be 32 bytes")
    try:
        _coincurve().PrivateKey(raw)
    except Exception as exc:
        raise BuzzIdentityError("Buzz private key is not a valid secp256k1 scalar") from exc
    return raw


def derive_xonly_pubkey(private_key: str | bytes) -> str:
    """Derive the canonical lowercase 64-hex BIP340 x-only public key."""
    raw = normalize_private_key(private_key)
    return _coincurve().PrivateKey(raw).public_key_xonly.format().hex()


def _aad(*, agent: str, pubkey: str, wrap_version: int) -> bytes:
    if not agent or not _HEX_64_RE.fullmatch(pubkey):
        raise BuzzIdentityError("invalid Buzz envelope identity metadata")
    return (
        _BUZZ_AAD_PREFIX
        + b"|v="
        + str(wrap_version).encode("ascii")
        + b"|agent="
        + agent.encode("utf-8")
        + b"|pubkey="
        + pubkey.encode("ascii")
    )


def wrap_buzz_private_key(
    private_key: str | bytes,
    *,
    agent: str,
    device_key: DeviceKey,
) -> BuzzKeyEnvelope:
    """Encrypt a Buzz private key with identity-bound XChaCha20-Poly1305 AAD."""
    if not isinstance(device_key, DeviceKey):
        raise TypeError("device_key must be a DeviceKey")
    raw = normalize_private_key(private_key)
    pubkey = derive_xonly_pubkey(raw)
    nonce = secrets.token_bytes(crypto_aead_xchacha20poly1305_ietf_NPUBBYTES)
    ciphertext = crypto_aead_xchacha20poly1305_ietf_encrypt(
        raw,
        _aad(agent=agent, pubkey=pubkey, wrap_version=BUZZ_WRAP_VERSION),
        nonce,
        device_key.material_insecure(),
    )
    return BuzzKeyEnvelope(
        agent=agent,
        pubkey=pubkey,
        wrap_version=BUZZ_WRAP_VERSION,
        nonce=nonce,
        ciphertext=ciphertext,
    )


def unwrap_buzz_private_key(
    envelope: BuzzKeyEnvelope,
    *,
    device_key: DeviceKey,
) -> bytes:
    """Decrypt and cross-check a stored Buzz secret.

    AAD binds the secret to both the owning agent and stored public key.  The
    explicit constant-time derived-key comparison is retained even though a
    metadata substitution should already fail AEAD authentication.
    """
    if not isinstance(envelope, BuzzKeyEnvelope):
        raise TypeError("envelope must be a BuzzKeyEnvelope")
    if not isinstance(device_key, DeviceKey):
        raise TypeError("device_key must be a DeviceKey")
    if envelope.wrap_version != BUZZ_WRAP_VERSION:
        raise BuzzIdentityUnhealthyError(f"unsupported Buzz wrap version {envelope.wrap_version}")
    try:
        raw = crypto_aead_xchacha20poly1305_ietf_decrypt(
            envelope.ciphertext,
            _aad(
                agent=envelope.agent,
                pubkey=envelope.pubkey,
                wrap_version=envelope.wrap_version,
            ),
            envelope.nonce,
            device_key.material_insecure(),
        )
    except Exception as exc:
        raise BuzzIdentityUnhealthyError("Buzz identity envelope authentication failed") from exc
    try:
        derived = derive_xonly_pubkey(raw)
    except BuzzIdentityError as exc:
        raise BuzzIdentityUnhealthyError("Buzz identity decrypted to an invalid key") from exc
    if not hmac.compare_digest(derived, envelope.pubkey):
        raise BuzzIdentityUnhealthyError(
            "Buzz identity public key does not match the decrypted private key"
        )
    return raw


__all__ = [
    "BUZZ_WRAP_VERSION",
    "BuzzDependencyError",
    "BuzzIdentityError",
    "BuzzIdentityUnhealthyError",
    "BuzzKeyEnvelope",
    "BuzzSigningMaterial",
    "derive_xonly_pubkey",
    "normalize_private_key",
    "unwrap_buzz_private_key",
    "wrap_buzz_private_key",
]
