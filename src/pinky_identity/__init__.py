"""pinky_identity — per-agent asymmetric service-auth identity primitives.

Implements the design captured in https://github.com/bradbrok/PinkyBot/issues/461
(``docs/design/agent-asymmetric-identity.md``).

This package provides:

- Ed25519 keypair primitives for the ``service-auth`` purpose
  (``pinky_identity.keys``)
- Encrypted-at-rest wrapping for signing seeds (``pinky_identity.keystore``)
- SQLite-backed persistence for wrapped signing keys
  (``pinky_identity.signer_store``)
- RFC 9421 HTTP Message Signature signer/verifier helpers
  (``pinky_identity.http_signatures``)
- (Coming in PR-3) Registry storage + enrollment-token issuance + admin
  approval flow (``pinky_identity.registry``)
- (Coming in PR-4b-2) Daemon-local signer + session-bound bearer-token
  capability (``pinky_identity.signer``)

The package is intentionally separated from ``pinky_federation``: the
crypto primitives are similar (both use Ed25519) but the identity
semantics, registry, and lifecycle are distinct. ``service-auth`` keys
authenticate an agent to external services and MCPs; ``federation`` keys
are mesh-transport tenant identities. Per the design doc, keys must not
be shared across purposes.
"""

from pinky_identity.http_signatures import (
    CONTENT_DIGEST_ALGORITHM,
    DEFAULT_COVERED_COMPONENTS,
    DEFAULT_LABEL,
    DEFAULT_MAX_AGE,
    DEFAULT_REQUIRED_COMPONENTS,
    DEFAULT_TAG,
    HttpSignatureVerificationError,
    PinkyKeyResolver,
    VerifyResult,
    attach_content_digest,
    compute_content_digest,
    sign_request,
    verify_request,
)
from pinky_identity.keys import (
    ED25519_PUBLIC_KEY_BYTES,
    ED25519_SECRET_KEY_BYTES,
    ED25519_SIGNATURE_BYTES,
    KID_HEX_LEN,
    PublicKey,
    SecretKey,
    SignatureError,
    SigningKeypair,
    fingerprint,
    generate_keypair,
    jwk_export,
    kid_from_public_key,
    public_key_from_bytes,
)
from pinky_identity.keystore import (
    DEFAULT_DEVICE_KEY_PATH,
    DEVICE_KEY_BYTES,
    WRAP_VERSION,
    DeviceKey,
    KeystoreError,
    WrappedKeypair,
    WrapVersionError,
    deserialize_wrapped,
    serialize_wrapped,
    unwrap_keypair,
    wrap_keypair,
)
from pinky_identity.signer_store import (
    DEFAULT_SIGNER_STORE_PATH,
    EncryptedSignerStore,
    SignerStoreError,
    SignerStoreRow,
)

__all__ = [
    "CONTENT_DIGEST_ALGORITHM",
    "DEFAULT_COVERED_COMPONENTS",
    "DEFAULT_DEVICE_KEY_PATH",
    "DEFAULT_LABEL",
    "DEFAULT_MAX_AGE",
    "DEFAULT_REQUIRED_COMPONENTS",
    "DEFAULT_SIGNER_STORE_PATH",
    "DEFAULT_TAG",
    "DEVICE_KEY_BYTES",
    "ED25519_PUBLIC_KEY_BYTES",
    "ED25519_SECRET_KEY_BYTES",
    "ED25519_SIGNATURE_BYTES",
    "KID_HEX_LEN",
    "WRAP_VERSION",
    "DeviceKey",
    "EncryptedSignerStore",
    "HttpSignatureVerificationError",
    "KeystoreError",
    "PinkyKeyResolver",
    "PublicKey",
    "SecretKey",
    "SignatureError",
    "SignerStoreError",
    "SignerStoreRow",
    "SigningKeypair",
    "VerifyResult",
    "WrapVersionError",
    "WrappedKeypair",
    "attach_content_digest",
    "compute_content_digest",
    "deserialize_wrapped",
    "fingerprint",
    "generate_keypair",
    "jwk_export",
    "kid_from_public_key",
    "public_key_from_bytes",
    "serialize_wrapped",
    "sign_request",
    "unwrap_keypair",
    "verify_request",
    "wrap_keypair",
]
