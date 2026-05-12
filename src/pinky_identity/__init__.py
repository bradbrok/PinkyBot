"""pinky_identity — per-agent asymmetric service-auth identity primitives.

Implements the design captured in https://github.com/bradbrok/PinkyBot/issues/461
(``docs/design/agent-asymmetric-identity.md``).

This package provides:

- Ed25519 keypair primitives for the ``service-auth`` purpose
  (``pinky_identity.keys``)
- RFC 9421 HTTP Message Signature signer/verifier helpers
  (``pinky_identity.http_signatures``)
- (Coming in PR-3) Registry storage + enrollment-token issuance + admin
  approval flow (``pinky_identity.registry``)
- (Coming in PR-4) Daemon-local signer + session-bound bearer-token
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

__all__ = [
    "CONTENT_DIGEST_ALGORITHM",
    "DEFAULT_COVERED_COMPONENTS",
    "DEFAULT_LABEL",
    "DEFAULT_MAX_AGE",
    "DEFAULT_REQUIRED_COMPONENTS",
    "DEFAULT_TAG",
    "ED25519_PUBLIC_KEY_BYTES",
    "ED25519_SECRET_KEY_BYTES",
    "ED25519_SIGNATURE_BYTES",
    "HttpSignatureVerificationError",
    "KID_HEX_LEN",
    "PinkyKeyResolver",
    "PublicKey",
    "SecretKey",
    "SignatureError",
    "SigningKeypair",
    "VerifyResult",
    "attach_content_digest",
    "compute_content_digest",
    "fingerprint",
    "generate_keypair",
    "jwk_export",
    "kid_from_public_key",
    "public_key_from_bytes",
    "sign_request",
    "verify_request",
]
