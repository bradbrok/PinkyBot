"""Ed25519 keypair primitives for service-auth identity.

Wraps PyNaCl's Ed25519 signing primitives in a small dataclass-based API
designed for the per-agent identity registry and the daemon-local signer.

Design rules (see docs/design/agent-asymmetric-identity.md):

- One Ed25519 keypair per agent, scoped to the ``service-auth`` purpose.
- The kid is a deterministic prefix of the SHA-256 of the public key.
  Truncating the fingerprint to a fixed-length hex string keeps kids
  short enough for HTTP headers and URL fragments while remaining
  collision-resistant within a fleet.
- The full SHA-256 hex fingerprint is the canonical identifier for
  manual verification and for binding enrollment tokens to a specific
  public key.
- Private key material is held inside :class:`SecretKey` and
  :class:`SigningKeypair`. Raw-bytes accessors are explicitly named
  ``secret_bytes_insecure`` so grep can flag callers and reviewers can
  catch unintended exposure.
- ``__repr__`` on key types never includes secret material.
- No on-disk format helpers live here; storage is the registry/signer's
  job (PR-3 / PR-4).
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from dataclasses import dataclass

from nacl.exceptions import BadSignatureError as _NaclBadSignatureError
from nacl.signing import SigningKey as _NaclSigningKey
from nacl.signing import VerifyKey as _NaclVerifyKey

# -- Constants ----------------------------------------------------------------

#: Ed25519 public key length in bytes (RFC 8032 §5.1.5).
ED25519_PUBLIC_KEY_BYTES = 32

#: Ed25519 secret (seed) key length in bytes. PyNaCl's ``SigningKey`` takes a
#: 32-byte seed; the 64-byte "secret key" used by some libraries is the seed
#: concatenated with the derived public key — we don't use that here.
ED25519_SECRET_KEY_BYTES = 32

#: Ed25519 signature length in bytes (RFC 8032 §5.1.6).
ED25519_SIGNATURE_BYTES = 64

#: kid is a hex prefix of the SHA-256 of the public key. 16 hex chars =
#: 64 bits of fingerprint collision resistance, ample within a fleet's
#: registry. The full ``fingerprint(pub)`` remains available for manual
#: verification when stronger comparison is needed.
KID_HEX_LEN = 16


# -- Errors -------------------------------------------------------------------


class SignatureError(Exception):
    """Raised when a signature fails verification."""


# -- Public key ---------------------------------------------------------------


@dataclass(frozen=True)
class PublicKey:
    """Ed25519 public key (32 bytes).

    Safe to share, serialize, and log. Equality and hashing are on raw
    bytes so two ``PublicKey`` instances built from the same bytes are
    interchangeable.
    """

    raw: bytes

    def __post_init__(self) -> None:
        if not isinstance(self.raw, (bytes, bytearray)) or len(self.raw) != ED25519_PUBLIC_KEY_BYTES:
            raise ValueError(
                f"Ed25519 public key must be exactly {ED25519_PUBLIC_KEY_BYTES} bytes"
            )
        # Normalize bytearray → bytes so equality works as expected.
        if isinstance(self.raw, bytearray):
            object.__setattr__(self, "raw", bytes(self.raw))

    def to_bytes(self) -> bytes:
        return self.raw

    def verify(self, message: bytes, signature: bytes) -> None:
        """Verify ``signature`` over ``message``.

        Raises :class:`SignatureError` on any failure (wrong key, tampered
        message, wrong-length signature, malformed signature). Does not
        leak which failure occurred — verification is binary.
        """
        if not isinstance(signature, (bytes, bytearray)) or len(signature) != ED25519_SIGNATURE_BYTES:
            raise SignatureError("signature must be exactly 64 bytes")
        try:
            _NaclVerifyKey(self.raw).verify(bytes(message), bytes(signature))
        except _NaclBadSignatureError as e:
            raise SignatureError(str(e)) from e

    def __repr__(self) -> str:
        # Show kid, not raw bytes — public is safe to print but kid is more useful.
        return f"PublicKey(kid={kid_from_public_key(self)!r})"


# -- Secret key + keypair -----------------------------------------------------


@dataclass(frozen=True)
class SecretKey:
    """Ed25519 32-byte secret seed.

    Holds raw seed bytes. Use :meth:`secret_bytes_insecure` to read them
    out; the verbose name is intentional so reviewers and grep can find
    every place secret material is exfiltrated from this object.

    ``__repr__`` never includes secret material.
    """

    _raw: bytes

    def __post_init__(self) -> None:
        if not isinstance(self._raw, (bytes, bytearray)) or len(self._raw) != ED25519_SECRET_KEY_BYTES:
            raise ValueError(
                f"Ed25519 secret seed must be exactly {ED25519_SECRET_KEY_BYTES} bytes"
            )
        if isinstance(self._raw, bytearray):
            object.__setattr__(self, "_raw", bytes(self._raw))

    def secret_bytes_insecure(self) -> bytes:
        """Return raw seed bytes.

        Verbose name so any caller is flagged for review. Storage and
        signer layers are expected to be the only callers.
        """
        return self._raw

    def __repr__(self) -> str:
        return "SecretKey(<32 bytes redacted>)"


@dataclass(frozen=True)
class SigningKeypair:
    """An Ed25519 signing keypair, public + secret bound together.

    Cheap to construct and pass around. Holds secret material — same
    care as :class:`SecretKey`. The public half is always available via
    :attr:`public`.

    The constructor enforces that ``public`` is the Ed25519 public key
    derived from ``secret``. Mismatched pairs would produce signatures
    that do not verify under the published public key (since kid /
    fingerprint / JWK all come from ``public`` but the signature is made
    with ``secret``), which is a foot-gun this class refuses to load.
    Use :meth:`from_secret` to build a keypair from a seed alone.
    """

    secret: SecretKey
    public: PublicKey

    def __post_init__(self) -> None:
        derived_pub_raw = bytes(
            _NaclSigningKey(self.secret.secret_bytes_insecure()).verify_key.encode()
        )
        if derived_pub_raw != self.public.raw:
            raise ValueError(
                "SigningKeypair public does not match the public key derived "
                "from secret; this would produce signatures that don't verify "
                "under the published public key. Use SigningKeypair.from_secret("
                "secret) to build a keypair from the seed alone."
            )

    def sign(self, message: bytes) -> bytes:
        """Return a detached Ed25519 signature over ``message``.

        Deterministic per RFC 8032 — same input produces same signature.
        """
        signing = _NaclSigningKey(self.secret.secret_bytes_insecure())
        signed = signing.sign(bytes(message))
        return bytes(signed.signature)

    @classmethod
    def from_secret(cls, secret: SecretKey) -> "SigningKeypair":
        """Build a keypair from a :class:`SecretKey`, deriving the public.

        Convenience for the common path where the seed is the only thing
        the caller has on hand (e.g. after decrypting a wrapped seed).
        Always returns a self-consistent pair.
        """
        if not isinstance(secret, SecretKey):
            raise TypeError("secret must be a SecretKey")
        pub_raw = bytes(
            _NaclSigningKey(secret.secret_bytes_insecure()).verify_key.encode()
        )
        return cls(secret=secret, public=PublicKey(pub_raw))

    def __repr__(self) -> str:
        return f"SigningKeypair(public={self.public!r})"


# -- Constructors -------------------------------------------------------------


def generate_keypair() -> SigningKeypair:
    """Generate a fresh Ed25519 keypair using OS randomness.

    Uses :mod:`secrets` for the seed source so callers can rely on a
    CSPRNG-backed key without thinking about it. Returns a
    :class:`SigningKeypair` with bound public key.
    """
    seed = secrets.token_bytes(ED25519_SECRET_KEY_BYTES)
    signing = _NaclSigningKey(seed)
    pub_raw = bytes(signing.verify_key.encode())
    return SigningKeypair(secret=SecretKey(seed), public=PublicKey(pub_raw))


def public_key_from_bytes(raw: bytes) -> PublicKey:
    """Build a :class:`PublicKey` from 32 raw bytes.

    Raises :class:`ValueError` for wrong-length or non-bytes input.
    Provided as a named constructor so callers don't directly poke at
    the dataclass field.
    """
    if not isinstance(raw, (bytes, bytearray)):
        raise ValueError("public_key_from_bytes requires bytes input")
    return PublicKey(bytes(raw))


# -- Fingerprints + key IDs ---------------------------------------------------


def fingerprint(public_key: PublicKey) -> str:
    """Return the canonical full-length SHA-256 hex fingerprint.

    This is the authoritative identifier for a public key in
    enrollment-token bindings and for manual verification. Always 64
    lowercase hex chars.
    """
    return hashlib.sha256(public_key.raw).hexdigest()


def kid_from_public_key(public_key: PublicKey) -> str:
    """Return the short kid (key ID) for HTTP headers and registry lookup.

    Defined as the first :data:`KID_HEX_LEN` hex characters of the SHA-256
    of the raw public key. Deterministic, collision-resistant within a
    fleet, short enough for ``Signature-Input: ...;keyid="..."`` HTTP
    headers without bloat.

    A registry MUST also store and expose the full :func:`fingerprint`
    for manual verification and for binding enrollment tokens — kid
    collisions are computationally unlikely but the fingerprint is the
    cryptographic identity.
    """
    return fingerprint(public_key)[:KID_HEX_LEN]


# -- JWK / OKP export ---------------------------------------------------------


def _b64url_nopad(data: bytes) -> str:
    """Base64url without padding, per RFC 7515 §2."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def jwk_export(public_key: PublicKey, *, kid: str | None = None) -> dict:
    """Export a public key as a JWK in OKP/Ed25519 form.

    Conforms to RFC 8037 §2 for the OKP key type. Includes ``alg=EdDSA``
    and ``use=sig`` so JOSE consumers can pick the key for signing
    verification without extra metadata.

    The returned dict is a fresh object — callers are free to add their
    own fields (``"use": "..."``, ``"kid": "..."``, etc.) before
    serializing.
    """
    out = {
        "kty": "OKP",
        "crv": "Ed25519",
        "alg": "EdDSA",
        "use": "sig",
        "x": _b64url_nopad(public_key.raw),
        "kid": kid if kid is not None else kid_from_public_key(public_key),
    }
    return out
