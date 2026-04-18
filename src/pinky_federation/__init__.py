"""PinkyBot federation — sealed-box-v1 crypto, envelope, and local state.

This package implements the reference crypto layer for PinkyBot federation v0.2:

- X25519 encryption keypairs (per-tenant, per-device)
- Ed25519 signing keypairs (per-tenant, client-owned)
- `sealed_box_v1`: authenticated ephemeral-X25519 + XChaCha20-Poly1305 + Ed25519 sender signature
- Versioned envelope serialization suitable for relay transport
- Deterministic fingerprints for TOFU pinning and UX display
- Local SQLite state store under ``data/federation/`` (P-02)
- Encrypted-at-rest tenant signing key persistence with device-local key (P-02)

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
from pinky_federation.key_store import (
    DEFAULT_DEVICE_KEY_PATH,
    DEVICE_KEY_BYTES,
    DeviceKey,
    EncryptedTenantKeyStore,
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
from pinky_federation.state import (
    DEFAULT_DB_PATH,
    INBOX_ARCHIVED,
    INBOX_NEW,
    INBOX_READ,
    INSTANCE_KEY_ACTIVE,
    INSTANCE_KEY_DECRYPT_ONLY,
    INSTANCE_KEY_KIND_ENCRYPTION,
    INSTANCE_KEY_KIND_SIGNING,
    INSTANCE_KEY_RETIRED,
    INVITE_ACTIVE,
    INVITE_EXPIRED,
    INVITE_REDEEMED,
    INVITE_REVOKED,
    OUTBOX_FAILED,
    OUTBOX_PENDING,
    OUTBOX_SENT,
    PEER_PIN_PINNED,
    PEER_PIN_REJECTED,
    PEER_PIN_ROTATED,
    ROLE_MEMBER,
    ROLE_OWNER,
    AttachmentRecord,
    FederationStateStore,
    InboxRecord,
    InstanceKeyRecord,
    InviteRecord,
    OutboxRecord,
    PeerPinRecord,
    TenantRecord,
)

__all__ = [
    # Errors
    "CryptoError",
    "DecryptionError",
    "SignatureError",
    # Envelope
    "Envelope",
    "EnvelopeError",
    "EnvelopeVersion",
    # Fingerprint
    "FINGERPRINT_BYTES",
    "canonical_address",
    "fingerprint",
    "format_fingerprint",
    # Keys
    "EncryptionKeyPair",
    "EncryptionPublicKey",
    "SigningKeyPair",
    "SigningPublicKey",
    # Sealed box
    "SEALED_BOX_VERSION",
    "seal",
    "unseal",
    # State store
    "DEFAULT_DB_PATH",
    "FederationStateStore",
    "TenantRecord",
    "InstanceKeyRecord",
    "PeerPinRecord",
    "OutboxRecord",
    "InboxRecord",
    "InviteRecord",
    "AttachmentRecord",
    # State constants
    "INSTANCE_KEY_ACTIVE",
    "INSTANCE_KEY_DECRYPT_ONLY",
    "INSTANCE_KEY_RETIRED",
    "INSTANCE_KEY_KIND_ENCRYPTION",
    "INSTANCE_KEY_KIND_SIGNING",
    "PEER_PIN_PINNED",
    "PEER_PIN_ROTATED",
    "PEER_PIN_REJECTED",
    "OUTBOX_PENDING",
    "OUTBOX_SENT",
    "OUTBOX_FAILED",
    "INBOX_NEW",
    "INBOX_READ",
    "INBOX_ARCHIVED",
    "INVITE_ACTIVE",
    "INVITE_REDEEMED",
    "INVITE_EXPIRED",
    "INVITE_REVOKED",
    "ROLE_OWNER",
    "ROLE_MEMBER",
    # Encrypted key store
    "DEFAULT_DEVICE_KEY_PATH",
    "DEVICE_KEY_BYTES",
    "DeviceKey",
    "EncryptedTenantKeyStore",
]
