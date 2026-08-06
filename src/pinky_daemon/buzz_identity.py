"""Encrypted-at-rest Buzz (Nostr/secp256k1) identity primitives.

The agent registry owns lifecycle and persistence.  This module owns only the
secret envelope and deliberately has no DTO or logging surface for raw key
material.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import time
from dataclasses import dataclass

from nacl.bindings import (
    crypto_aead_xchacha20poly1305_ietf_decrypt,
    crypto_aead_xchacha20poly1305_ietf_encrypt,
    crypto_aead_xchacha20poly1305_ietf_NPUBBYTES,
)

from pinky_identity.keystore import DeviceKey

BUZZ_WRAP_VERSION = 1
BUZZ_PRIVATE_KEY_BYTES = 32
BUZZ_TOS_POLICY = "buzz-tos-and-18-plus"
BUZZ_TOS_POLICY_VERSION = 1
_BUZZ_AAD_PREFIX = b"pinky-identity/v1/buzz-secp256k1"
_HEX_64_RE = re.compile(r"^[0-9a-f]{64}$")
_OWNER_ACTOR_RE = re.compile(r"^ui:[a-zA-Z0-9_.-]{1,120}$")
_APPROVAL_REF_RE = re.compile(r"^owner-control:[0-9a-f]{32}:sha256:[0-9a-f]{64}$")


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


@dataclass(frozen=True)
class BuzzOwnerApproval:
    """Server-issued authority record for one exact Buzz identity binding."""

    receipt: str
    approved_by: str
    approved_at: float
    approval_ref: str


def canonical_buzz_tos_receipt(
    *,
    agent: str,
    pubkey: str,
    relay_url: str,
    community_id: str,
) -> str:
    """Build the canonical, identity-scoped receipt for an owner bind action."""
    if not agent or not _HEX_64_RE.fullmatch(pubkey):
        raise BuzzIdentityError("invalid Buzz approval identity metadata")
    return json.dumps(
        {
            "action": "buzz.identity.bind",
            "agent": agent,
            "community_id": community_id,
            "policy": BUZZ_TOS_POLICY,
            "policy_version": BUZZ_TOS_POLICY_VERSION,
            "pubkey": pubkey,
            "relay_url": relay_url,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def validate_buzz_owner_actor(owner_actor: str) -> str:
    """Validate the trusted owner principal derived by the HTTP session gate."""
    actor = str(owner_actor or "").strip()
    if not _OWNER_ACTOR_RE.fullmatch(actor):
        raise BuzzIdentityError("Buzz owner approval actor is invalid")
    return actor


def issue_buzz_owner_approval(
    *,
    owner_actor: str,
    agent: str,
    pubkey: str,
    relay_url: str,
    community_id: str,
) -> BuzzOwnerApproval:
    """Issue fresh provenance for one authenticated owner action.

    The API caller supplies none of these bytes. The trusted route derives the
    actor from its session, while this function derives the receipt, time, and
    single-use reference inside the daemon.
    """
    actor = validate_buzz_owner_actor(owner_actor)
    receipt = canonical_buzz_tos_receipt(
        agent=agent,
        pubkey=pubkey,
        relay_url=relay_url,
        community_id=community_id,
    )
    digest = hashlib.sha256(receipt.encode("utf-8")).hexdigest()
    return BuzzOwnerApproval(
        receipt=receipt,
        approved_by=actor,
        approved_at=time.time(),
        approval_ref=f"owner-control:{secrets.token_hex(16)}:sha256:{digest}",
    )


def validate_buzz_owner_approval(
    *,
    agent: str,
    pubkey: str,
    relay_url: str,
    community_id: str,
    receipt: str,
    approved_by: str,
    approved_at: float,
    approval_ref: str,
) -> None:
    """Validate stored provenance against its exact identity and policy scope."""
    expected = canonical_buzz_tos_receipt(
        agent=agent,
        pubkey=pubkey,
        relay_url=relay_url,
        community_id=community_id,
    )
    if not hmac.compare_digest(str(receipt or ""), expected):
        raise BuzzIdentityUnhealthyError("Buzz owner approval receipt is invalid")
    actor = str(approved_by or "")
    if not _OWNER_ACTOR_RE.fullmatch(actor) or float(approved_at or 0) <= 0:
        raise BuzzIdentityUnhealthyError("Buzz owner approval provenance is invalid")
    ref = str(approval_ref or "")
    expected_digest = hashlib.sha256(expected.encode("utf-8")).hexdigest()
    if not _APPROVAL_REF_RE.fullmatch(ref) or not hmac.compare_digest(
        ref.rsplit(":", 1)[-1], expected_digest
    ):
        raise BuzzIdentityUnhealthyError("Buzz owner approval reference is invalid")


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
    "BUZZ_TOS_POLICY",
    "BUZZ_TOS_POLICY_VERSION",
    "BUZZ_WRAP_VERSION",
    "BuzzDependencyError",
    "BuzzIdentityError",
    "BuzzIdentityUnhealthyError",
    "BuzzKeyEnvelope",
    "BuzzOwnerApproval",
    "BuzzSigningMaterial",
    "canonical_buzz_tos_receipt",
    "derive_xonly_pubkey",
    "issue_buzz_owner_approval",
    "normalize_private_key",
    "unwrap_buzz_private_key",
    "validate_buzz_owner_actor",
    "validate_buzz_owner_approval",
    "wrap_buzz_private_key",
]
