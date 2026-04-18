"""Federation keypair types.

We use:
- **X25519** for encryption keypairs (Diffie-Hellman with ephemeral sender keys).
- **Ed25519** for signing keypairs (tenant identity + per-message sender auth).

All private key bytes stay inside their keypair objects. Raw-bytes accessors
(`.private_bytes_insecure()`) are intentionally verbose so grep can flag callers.

No key material is written to `repr()` or logs.
"""

from __future__ import annotations

from dataclasses import dataclass

from nacl.bindings import (
    crypto_scalarmult,
    crypto_scalarmult_base,
)
from nacl.public import PrivateKey as _NaclX25519Private
from nacl.signing import SigningKey as _NaclEd25519Signing
from nacl.signing import VerifyKey as _NaclEd25519Verify

from pinky_federation.errors import SignatureError

X25519_KEY_BYTES = 32
ED25519_KEY_BYTES = 32
ED25519_SIG_BYTES = 64


# -- Encryption (X25519) ------------------------------------------------------


@dataclass(frozen=True)
class EncryptionPublicKey:
    """X25519 public key (32 bytes).

    Safe to share, serialize, and log. Equality + hashing are on raw bytes.
    """

    raw: bytes

    def __post_init__(self) -> None:
        if not isinstance(self.raw, bytes) or len(self.raw) != X25519_KEY_BYTES:
            raise ValueError(f"X25519 public key must be {X25519_KEY_BYTES} bytes")

    def to_bytes(self) -> bytes:
        return self.raw

    @classmethod
    def from_bytes(cls, data: bytes) -> EncryptionPublicKey:
        return cls(bytes(data))

    def __repr__(self) -> str:
        return f"EncryptionPublicKey(<{self.raw[:4].hex()}…>)"


class EncryptionKeyPair:
    """X25519 private + public pair.

    The private half never appears in `repr()` and never round-trips through
    serialization unless a caller explicitly asks via
    :meth:`private_bytes_insecure`.
    """

    __slots__ = ("_sk",)

    def __init__(self, sk: _NaclX25519Private) -> None:
        self._sk = sk

    @classmethod
    def generate(cls) -> EncryptionKeyPair:
        return cls(_NaclX25519Private.generate())

    @classmethod
    def from_private_bytes(cls, data: bytes) -> EncryptionKeyPair:
        if not isinstance(data, (bytes, bytearray)) or len(data) != X25519_KEY_BYTES:
            raise ValueError(f"X25519 private key must be {X25519_KEY_BYTES} bytes")
        return cls(_NaclX25519Private(bytes(data)))

    @property
    def public_key(self) -> EncryptionPublicKey:
        return EncryptionPublicKey(bytes(self._sk.public_key))

    def private_bytes_insecure(self) -> bytes:
        """Return raw 32-byte private scalar. Use only for at-rest persistence."""
        return bytes(self._sk)

    def dh(self, peer_public: EncryptionPublicKey) -> bytes:
        """Compute X25519 ECDH shared secret with *peer_public*.

        Returns the raw 32-byte shared point. Callers MUST feed this through
        an HKDF step before using it as a symmetric key.
        """
        if not isinstance(peer_public, EncryptionPublicKey):
            raise TypeError("peer_public must be EncryptionPublicKey")
        return crypto_scalarmult(self.private_bytes_insecure(), peer_public.raw)

    def __repr__(self) -> str:
        return f"EncryptionKeyPair(public={self.public_key!r})"


def x25519_public_from_private(sk_bytes: bytes) -> bytes:
    """Derive X25519 public from a raw private scalar (32 bytes)."""
    if len(sk_bytes) != X25519_KEY_BYTES:
        raise ValueError(f"X25519 private must be {X25519_KEY_BYTES} bytes")
    return crypto_scalarmult_base(sk_bytes)


# -- Signing (Ed25519) --------------------------------------------------------


@dataclass(frozen=True)
class SigningPublicKey:
    """Ed25519 verify key (32 bytes)."""

    raw: bytes

    def __post_init__(self) -> None:
        if not isinstance(self.raw, bytes) or len(self.raw) != ED25519_KEY_BYTES:
            raise ValueError(f"Ed25519 public key must be {ED25519_KEY_BYTES} bytes")

    def to_bytes(self) -> bytes:
        return self.raw

    @classmethod
    def from_bytes(cls, data: bytes) -> SigningPublicKey:
        return cls(bytes(data))

    def verify(self, message: bytes, signature: bytes) -> None:
        """Raise :class:`SignatureError` if *signature* is not valid for *message*.

        The underlying library raises its own exception type; we normalize so
        higher layers only catch our own exception hierarchy.
        """
        if len(signature) != ED25519_SIG_BYTES:
            raise SignatureError("signature must be 64 bytes")
        try:
            _NaclEd25519Verify(self.raw).verify(message, signature)
        except Exception as exc:  # noqa: BLE001 — normalize third-party types
            raise SignatureError("Ed25519 signature did not verify") from exc

    def __repr__(self) -> str:
        return f"SigningPublicKey(<{self.raw[:4].hex()}…>)"


class SigningKeyPair:
    """Ed25519 signing + verify pair (tenant identity key)."""

    __slots__ = ("_sk",)

    def __init__(self, sk: _NaclEd25519Signing) -> None:
        self._sk = sk

    @classmethod
    def generate(cls) -> SigningKeyPair:
        return cls(_NaclEd25519Signing.generate())

    @classmethod
    def from_seed(cls, seed: bytes) -> SigningKeyPair:
        """Construct from a 32-byte seed.

        Only use this for persistence/recovery — never derive a seed from a
        low-entropy source.
        """
        if not isinstance(seed, (bytes, bytearray)) or len(seed) != ED25519_KEY_BYTES:
            raise ValueError(f"Ed25519 seed must be {ED25519_KEY_BYTES} bytes")
        return cls(_NaclEd25519Signing(bytes(seed)))

    @property
    def public_key(self) -> SigningPublicKey:
        return SigningPublicKey(bytes(self._sk.verify_key))

    def seed_bytes_insecure(self) -> bytes:
        """Return the raw 32-byte Ed25519 seed. Use only for at-rest persistence."""
        return bytes(self._sk)

    def sign(self, message: bytes) -> bytes:
        """Sign *message* with Ed25519. Returns raw 64-byte signature."""
        if not isinstance(message, (bytes, bytearray)):
            raise TypeError("message must be bytes")
        return bytes(self._sk.sign(bytes(message)).signature)

    def __repr__(self) -> str:
        return f"SigningKeyPair(public={self.public_key!r})"
