"""PinkyBot federation — sealed-box-v1 crypto and envelope primitives.

This package implements the reference crypto layer for PinkyBot federation v0.2:

- X25519 encryption keypairs (per-tenant, per-device)
- Ed25519 signing keypairs (per-tenant, client-owned)
- `sealed_box_v1`: authenticated ephemeral-X25519 + XChaCha20-Poly1305 + Ed25519 sender signature
- Versioned envelope serialization suitable for relay transport
- Deterministic fingerprints for TOFU pinning and UX display

Nothing in this package talks to a network. Higher layers (transport, invite,
attachments) consume these primitives.
"""

from pinky_federation.envelope import (
    Envelope,
    EnvelopeError,
    EnvelopeVersion,
)
from pinky_federation.errors import (
    CryptoError,
    DecryptionError,
    SignatureError,
)
from pinky_federation.fingerprint import (
    FINGERPRINT_BYTES,
    canonical_address,
    fingerprint,
    format_fingerprint,
)
from pinky_federation.keys import (
    EncryptionKeyPair,
    EncryptionPublicKey,
    SigningKeyPair,
    SigningPublicKey,
)
from pinky_federation.sealed_box import (
    SEALED_BOX_VERSION,
    seal,
    unseal,
)

__all__ = [
    "CryptoError",
    "DecryptionError",
    "Envelope",
    "EnvelopeError",
    "EnvelopeVersion",
    "EncryptionKeyPair",
    "EncryptionPublicKey",
    "FINGERPRINT_BYTES",
    "SEALED_BOX_VERSION",
    "SignatureError",
    "SigningKeyPair",
    "SigningPublicKey",
    "canonical_address",
    "fingerprint",
    "format_fingerprint",
    "seal",
    "unseal",
]
