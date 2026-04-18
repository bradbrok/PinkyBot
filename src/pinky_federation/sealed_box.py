"""``sealed_box_v1`` — authenticated per-message envelope crypto.

Protocol (v1):

1. Sender generates an ephemeral X25519 keypair ``(eph_sk, eph_pk)``.
2. Computes ``shared = X25519(eph_sk, recipient_enc_pk)``.
3. Derives ``key = HKDF-SHA256(shared, salt=eph_pk||recipient_enc_pk, info="…/key")``.
4. Picks a fresh 24-byte random nonce.
5. Encrypts plaintext with XChaCha20-Poly1305 under (key, nonce), with AAD
   covering protocol domain, version, both fingerprints, and ``eph_pk``.
6. Signs a canonical "to-be-signed" byte string with the sender's Ed25519 key.
   The signed bytes include the ciphertext, so tamper detection is
   end-to-end even if the AEAD tag alone were somehow stripped.

Properties:

- **Forward secrecy** (for a given message) — the ephemeral private key is
  discarded after use, so compromise of a long-term encryption key does not
  retroactively decrypt past messages.
- **Sender authenticity** — the Ed25519 signature binds a specific sender
  identity to the envelope; the recipient's trust store resolves
  ``sender_fingerprint`` → signing public key.
- **Replay/cross-recipient resistance** — the AAD and signed bytes include
  the recipient fingerprint, so an envelope can't be rebound to a different
  recipient without invalidating the MAC and the signature.

No state is kept in this module. Higher layers handle persistence and
ratcheting (planned for v2).
"""

from __future__ import annotations

import os

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from nacl.bindings import (
    crypto_aead_xchacha20poly1305_ietf_decrypt,
    crypto_aead_xchacha20poly1305_ietf_encrypt,
    crypto_aead_xchacha20poly1305_ietf_KEYBYTES,
    crypto_aead_xchacha20poly1305_ietf_NPUBBYTES,
)

from pinky_federation.envelope import (
    Envelope,
    EnvelopeVersion,
)
from pinky_federation.errors import (
    DecryptionError,
)
from pinky_federation.fingerprint import (
    FINGERPRINT_BYTES,
)
from pinky_federation.keys import (
    EncryptionKeyPair,
    EncryptionPublicKey,
    SigningKeyPair,
    SigningPublicKey,
)

#: Current sealed-box protocol version. Bumped only on breaking wire changes.
SEALED_BOX_VERSION = EnvelopeVersion.V1

_KEY_BYTES = crypto_aead_xchacha20poly1305_ietf_KEYBYTES  # 32
_NONCE_BYTES = crypto_aead_xchacha20poly1305_ietf_NPUBBYTES  # 24

_HKDF_INFO = b"pinky-federation/sealed_box_v1/key"
_AAD_DOMAIN = b"pinky-federation/sealed_box_v1/aad"
_SIG_DOMAIN = b"pinky-federation/sealed_box_v1/sig"


def _derive_key(shared: bytes, eph_pk: bytes, recipient_enc_pk: bytes) -> bytes:
    return HKDF(
        algorithm=hashes.SHA256(),
        length=_KEY_BYTES,
        salt=eph_pk + recipient_enc_pk,
        info=_HKDF_INFO,
    ).derive(shared)


def _build_aad(
    version: int,
    sender_fp: bytes,
    recipient_fp: bytes,
    eph_pk: bytes,
) -> bytes:
    if len(sender_fp) != FINGERPRINT_BYTES or len(recipient_fp) != FINGERPRINT_BYTES:
        raise ValueError("fingerprints must be FINGERPRINT_BYTES bytes")
    if len(eph_pk) != 32:
        raise ValueError("eph_pk must be 32 bytes")
    return b"".join(
        [
            _AAD_DOMAIN,
            bytes([version]),
            sender_fp,
            recipient_fp,
            eph_pk,
        ]
    )


def _build_sig_tbs(
    version: int,
    sender_fp: bytes,
    recipient_fp: bytes,
    eph_pk: bytes,
    nonce: bytes,
    ciphertext: bytes,
) -> bytes:
    return b"".join(
        [
            _SIG_DOMAIN,
            bytes([version]),
            sender_fp,
            recipient_fp,
            eph_pk,
            nonce,
            len(ciphertext).to_bytes(4, "big"),
            ciphertext,
        ]
    )


def seal(
    plaintext: bytes,
    *,
    sender_signing: SigningKeyPair,
    sender_fingerprint: bytes,
    recipient_encryption: EncryptionPublicKey,
    recipient_fingerprint: bytes,
    nonce: bytes | None = None,
    ephemeral: EncryptionKeyPair | None = None,
) -> Envelope:
    """Encrypt + sign *plaintext* into a v1 envelope addressed to the recipient.

    Arguments:
        plaintext: Bytes to seal. May be empty.
        sender_signing: Sender's long-term Ed25519 signing keypair.
        sender_fingerprint: Sender identity fingerprint to embed in the envelope.
        recipient_encryption: Recipient's long-term X25519 public key.
        recipient_fingerprint: Recipient identity fingerprint.
        nonce: Optional 24-byte override. ``None`` means generate fresh randomness.
            **Only supply this for deterministic test vectors** — reusing a
            nonce with the same key is catastrophic.
        ephemeral: Optional ephemeral X25519 keypair override (same caveat).

    The returned envelope is safe to serialize via ``envelope.to_bytes()``.
    """
    if not isinstance(plaintext, (bytes, bytearray)):
        raise TypeError("plaintext must be bytes")
    if not isinstance(sender_fingerprint, (bytes, bytearray)) or len(sender_fingerprint) != FINGERPRINT_BYTES:
        raise ValueError(f"sender_fingerprint must be {FINGERPRINT_BYTES} bytes")
    if not isinstance(recipient_fingerprint, (bytes, bytearray)) or len(recipient_fingerprint) != FINGERPRINT_BYTES:
        raise ValueError(f"recipient_fingerprint must be {FINGERPRINT_BYTES} bytes")

    eph = ephemeral if ephemeral is not None else EncryptionKeyPair.generate()
    eph_pk = eph.public_key.to_bytes()

    shared = eph.dh(recipient_encryption)
    key = _derive_key(shared, eph_pk, recipient_encryption.to_bytes())

    if nonce is None:
        nonce_bytes = os.urandom(_NONCE_BYTES)
    else:
        if len(nonce) != _NONCE_BYTES:
            raise ValueError(f"nonce must be {_NONCE_BYTES} bytes")
        nonce_bytes = bytes(nonce)

    aad = _build_aad(
        SEALED_BOX_VERSION.value,
        bytes(sender_fingerprint),
        bytes(recipient_fingerprint),
        eph_pk,
    )
    ciphertext = crypto_aead_xchacha20poly1305_ietf_encrypt(
        bytes(plaintext), aad, nonce_bytes, key
    )

    sig_tbs = _build_sig_tbs(
        SEALED_BOX_VERSION.value,
        bytes(sender_fingerprint),
        bytes(recipient_fingerprint),
        eph_pk,
        nonce_bytes,
        ciphertext,
    )
    signature = sender_signing.sign(sig_tbs)

    return Envelope(
        version=SEALED_BOX_VERSION,
        sender_fingerprint=bytes(sender_fingerprint),
        recipient_fingerprint=bytes(recipient_fingerprint),
        ephemeral_public=eph_pk,
        nonce=nonce_bytes,
        signature=signature,
        ciphertext=ciphertext,
    )


def unseal(
    envelope: Envelope,
    *,
    recipient_encryption: EncryptionKeyPair,
    sender_signing_public: SigningPublicKey,
) -> bytes:
    """Verify + decrypt *envelope* using the recipient's private key.

    ``sender_signing_public`` must be resolved by the caller from their local
    trust store keyed on ``envelope.sender_fingerprint``. This function does
    not talk to storage.

    Raises:
        SignatureError: Sender signature failed verification.
        DecryptionError: AEAD tag did not match (tamper / wrong key / wrong version).
    """
    if envelope.version is not SEALED_BOX_VERSION:
        raise DecryptionError(f"unsupported envelope version: {envelope.version}")

    sig_tbs = _build_sig_tbs(
        envelope.version.value,
        envelope.sender_fingerprint,
        envelope.recipient_fingerprint,
        envelope.ephemeral_public,
        envelope.nonce,
        envelope.ciphertext,
    )
    # Raises SignatureError on failure (normalized inside SigningPublicKey).
    sender_signing_public.verify(sig_tbs, envelope.signature)

    shared = recipient_encryption.dh(EncryptionPublicKey(envelope.ephemeral_public))
    key = _derive_key(
        shared,
        envelope.ephemeral_public,
        recipient_encryption.public_key.to_bytes(),
    )

    aad = _build_aad(
        envelope.version.value,
        envelope.sender_fingerprint,
        envelope.recipient_fingerprint,
        envelope.ephemeral_public,
    )
    try:
        plaintext = crypto_aead_xchacha20poly1305_ietf_decrypt(
            envelope.ciphertext, aad, envelope.nonce, key
        )
    except Exception as exc:  # noqa: BLE001 — libsodium raises CryptoError
        raise DecryptionError("AEAD decryption failed") from exc
    return plaintext


# Re-export for unit-test access. Not part of the public API.
__test__ = {
    "_derive_key": _derive_key,
    "_build_aad": _build_aad,
    "_build_sig_tbs": _build_sig_tbs,
}
