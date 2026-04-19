"""Deterministic fingerprints for federation identities.

A fingerprint uniquely identifies the tuple *(canonical address, signing pubkey,
encryption pubkey)*. It is used for:

- TOFU pinning (stored on first receive, verified on subsequent receives)
- Short UX display strings ("Verify: `a1b2 c3d4 … f7e8`")
- Addressing senders in the wire envelope (so receivers can look up trust)

Design:

- Input canonicalization is strict — the same tenant address must produce
  identical bytes regardless of case, whitespace, or surrounding noise.
- The hash is domain-separated with a version string, so we can rotate the
  scheme without fingerprint collisions across versions.
- We use SHA-256 truncated to 16 bytes (128 bits). That is comfortably above
  the preimage/collision bar for TOFU and short enough for display.
"""

from __future__ import annotations

import hashlib

from pinky_federation.keys import (
    EncryptionPublicKey,
    SigningPublicKey,
)

#: Fingerprint length in bytes. 16 bytes = 128 bits.
FINGERPRINT_BYTES = 16

_FP_DOMAIN = b"pinky-federation/fingerprint/v1"


def canonical_address(address: str) -> str:
    """Canonicalize a federation address (``user@host`` style).

    Rules:

    - Strip surrounding whitespace.
    - Lowercase the entire string (federation addresses are case-insensitive
      in both the local part and host, per our spec; real email is
      case-sensitive in the local part, but we want TOFU to be forgiving).
    - Reject empty strings and strings containing control bytes or whitespace
      after stripping.
    """
    if not isinstance(address, str):
        raise TypeError("address must be str")
    cleaned = address.strip().lower()
    if not cleaned:
        raise ValueError("address must not be empty")
    for ch in cleaned:
        if ch.isspace() or ord(ch) < 0x20:
            raise ValueError("address must not contain whitespace or control chars")
    return cleaned


def fingerprint(
    address: str,
    signing_key: SigningPublicKey,
    encryption_key: EncryptionPublicKey,
) -> bytes:
    """Compute the 16-byte fingerprint for a federation identity.

    The hash input is:

        FP_DOMAIN || u16(len(addr)) || utf8(addr) ||
        u8(len(sig_pk)) || sig_pk || u8(len(enc_pk)) || enc_pk

    Length-prefixing prevents cross-field ambiguity (otherwise concatenation
    would let two different identities produce the same hash input).
    """
    addr = canonical_address(address).encode("utf-8")
    sig_pk = signing_key.to_bytes()
    enc_pk = encryption_key.to_bytes()

    h = hashlib.sha256()
    h.update(_FP_DOMAIN)
    h.update(len(addr).to_bytes(2, "big"))
    h.update(addr)
    h.update(len(sig_pk).to_bytes(1, "big"))
    h.update(sig_pk)
    h.update(len(enc_pk).to_bytes(1, "big"))
    h.update(enc_pk)
    return h.digest()[:FINGERPRINT_BYTES]


def format_fingerprint(fp: bytes, *, groups: int = 4, group_size: int = 4) -> str:
    """Format *fp* as a human-readable grouped hex string.

    Default layout: ``"a1b2 c3d4 e5f6 …"`` with 4-char groups separated by
    spaces. Accepts any fingerprint length that is a multiple of ``group_size``.
    """
    if not isinstance(fp, (bytes, bytearray)):
        raise TypeError("fp must be bytes")
    if groups <= 0 or group_size <= 0:
        raise ValueError("groups and group_size must be positive")
    hex_str = fp.hex()
    # Break into group_size-char chunks, then join the requested number of
    # groups with spaces. Any remainder past `groups * group_size` chars is
    # dropped — callers wanting the full hex should use fp.hex() directly.
    chunks: list[str] = []
    for i in range(0, len(hex_str), group_size):
        chunks.append(hex_str[i : i + group_size])
        if len(chunks) == groups:
            break
    return " ".join(chunks)
