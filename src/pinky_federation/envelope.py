"""Envelope wire format for federation v0.2.

An envelope is the single unit that moves through the relay. It contains
everything a recipient needs to authenticate and decrypt a sealed-box-v1
message, *given* their own private key and a trust-store entry for the
sender fingerprint.

Wire format (v1), all integers big-endian:

    magic        4   b"PFv1"
    version      1   == 1
    flags        1   reserved, must be 0
    sender_fp    16  fingerprint bytes
    recipient_fp 16  fingerprint bytes
    eph_pk       32  ephemeral X25519 public key
    nonce        24  XChaCha20-Poly1305 nonce
    sig          64  Ed25519 signature
    ct_len       4   length of ciphertext in bytes
    ciphertext   ct_len

Fixed-length fields simplify parsing and test-vector generation; the only
variable-length piece is the AEAD ciphertext, length-prefixed with a 32-bit
big-endian integer (max 4 GiB — more than enough for in-relay messages).
"""

from __future__ import annotations

import enum
from dataclasses import dataclass

from pinky_federation.errors import EnvelopeError
from pinky_federation.fingerprint import FINGERPRINT_BYTES
from pinky_federation.keys import ED25519_SIG_BYTES, X25519_KEY_BYTES

_MAGIC = b"PFv1"
_NONCE_BYTES = 24
_CT_LEN_FIELD_BYTES = 4
#: Hard cap on ciphertext size (16 MiB). Larger blobs belong in the attachment
#: pipeline, not the main envelope.
MAX_CIPHERTEXT_BYTES = 16 * 1024 * 1024

# Offsets within the fixed-size header.
_HDR_MAGIC_END = len(_MAGIC)
_HDR_VERSION = _HDR_MAGIC_END
_HDR_FLAGS = _HDR_VERSION + 1
_HDR_SENDER_FP = _HDR_FLAGS + 1
_HDR_RECIPIENT_FP = _HDR_SENDER_FP + FINGERPRINT_BYTES
_HDR_EPH_PK = _HDR_RECIPIENT_FP + FINGERPRINT_BYTES
_HDR_NONCE = _HDR_EPH_PK + X25519_KEY_BYTES
_HDR_SIG = _HDR_NONCE + _NONCE_BYTES
_HDR_CT_LEN = _HDR_SIG + ED25519_SIG_BYTES
_HDR_FIXED = _HDR_CT_LEN + _CT_LEN_FIELD_BYTES  # 142 bytes


class EnvelopeVersion(enum.IntEnum):
    """Wire-protocol version for sealed-box envelopes."""

    V1 = 1


@dataclass(frozen=True)
class Envelope:
    """Parsed sealed-box envelope.

    Field sizes are validated in ``__post_init__``; instances constructed via
    :meth:`from_bytes` are guaranteed well-shaped.
    """

    version: EnvelopeVersion
    sender_fingerprint: bytes
    recipient_fingerprint: bytes
    ephemeral_public: bytes
    nonce: bytes
    signature: bytes
    ciphertext: bytes

    def __post_init__(self) -> None:
        if not isinstance(self.version, EnvelopeVersion):
            raise ValueError("version must be EnvelopeVersion")
        self._check_len("sender_fingerprint", self.sender_fingerprint, FINGERPRINT_BYTES)
        self._check_len("recipient_fingerprint", self.recipient_fingerprint, FINGERPRINT_BYTES)
        self._check_len("ephemeral_public", self.ephemeral_public, X25519_KEY_BYTES)
        self._check_len("nonce", self.nonce, _NONCE_BYTES)
        self._check_len("signature", self.signature, ED25519_SIG_BYTES)
        if not isinstance(self.ciphertext, (bytes, bytearray)):
            raise ValueError("ciphertext must be bytes")
        if len(self.ciphertext) > MAX_CIPHERTEXT_BYTES:
            raise ValueError("ciphertext exceeds MAX_CIPHERTEXT_BYTES")

    @staticmethod
    def _check_len(name: str, val: bytes, expected: int) -> None:
        if not isinstance(val, (bytes, bytearray)) or len(val) != expected:
            raise ValueError(f"{name} must be {expected} bytes")

    # -- Serialization --------------------------------------------------------

    def to_bytes(self) -> bytes:
        parts = [
            _MAGIC,
            bytes([self.version.value]),
            bytes([0]),  # flags reserved
            bytes(self.sender_fingerprint),
            bytes(self.recipient_fingerprint),
            bytes(self.ephemeral_public),
            bytes(self.nonce),
            bytes(self.signature),
            len(self.ciphertext).to_bytes(_CT_LEN_FIELD_BYTES, "big"),
            bytes(self.ciphertext),
        ]
        return b"".join(parts)

    @classmethod
    def from_bytes(cls, data: bytes) -> Envelope:
        if not isinstance(data, (bytes, bytearray)):
            raise EnvelopeError("envelope must be bytes")
        if len(data) < _HDR_FIXED:
            raise EnvelopeError("envelope shorter than fixed header")
        if data[: _HDR_MAGIC_END] != _MAGIC:
            raise EnvelopeError("envelope magic mismatch")

        version_byte = data[_HDR_VERSION]
        try:
            version = EnvelopeVersion(version_byte)
        except ValueError as exc:
            raise EnvelopeError(f"unsupported envelope version: {version_byte}") from exc

        flags = data[_HDR_FLAGS]
        if flags != 0:
            raise EnvelopeError(f"unsupported envelope flags: 0x{flags:02x}")

        sender_fp = bytes(data[_HDR_SENDER_FP:_HDR_RECIPIENT_FP])
        recipient_fp = bytes(data[_HDR_RECIPIENT_FP:_HDR_EPH_PK])
        eph_pk = bytes(data[_HDR_EPH_PK:_HDR_NONCE])
        nonce = bytes(data[_HDR_NONCE:_HDR_SIG])
        signature = bytes(data[_HDR_SIG:_HDR_CT_LEN])

        ct_len = int.from_bytes(data[_HDR_CT_LEN:_HDR_FIXED], "big")
        if ct_len > MAX_CIPHERTEXT_BYTES:
            raise EnvelopeError("ciphertext length exceeds MAX_CIPHERTEXT_BYTES")
        expected_end = _HDR_FIXED + ct_len
        if len(data) != expected_end:
            raise EnvelopeError(
                f"envelope length mismatch: header says ct_len={ct_len}, "
                f"total_expected={expected_end}, got={len(data)}"
            )
        ciphertext = bytes(data[_HDR_FIXED:expected_end])

        return cls(
            version=version,
            sender_fingerprint=sender_fp,
            recipient_fingerprint=recipient_fp,
            ephemeral_public=eph_pk,
            nonce=nonce,
            signature=signature,
            ciphertext=ciphertext,
        )
