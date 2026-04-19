"""Fingerprint tests: canonicalization, determinism, collision resistance, display."""

from __future__ import annotations

import pytest

from pinky_federation.fingerprint import (
    FINGERPRINT_BYTES,
    canonical_address,
    fingerprint,
    format_fingerprint,
)
from pinky_federation.keys import (
    EncryptionKeyPair,
    SigningKeyPair,
)

# -- canonical_address --------------------------------------------------------


def test_canonical_address_strips_and_lowercases() -> None:
    assert canonical_address("  Alice@Example.COM  ") == "alice@example.com"


def test_canonical_address_rejects_empty() -> None:
    with pytest.raises(ValueError):
        canonical_address("")
    with pytest.raises(ValueError):
        canonical_address("   ")


def test_canonical_address_rejects_whitespace_inside() -> None:
    with pytest.raises(ValueError):
        canonical_address("alice @example.com")


def test_canonical_address_rejects_control_chars() -> None:
    with pytest.raises(ValueError):
        canonical_address("alice\x07@example.com")


def test_canonical_address_rejects_non_string() -> None:
    with pytest.raises(TypeError):
        canonical_address(b"alice@example.com")  # type: ignore[arg-type]


# -- fingerprint --------------------------------------------------------------


def _sample_identity():
    sig = SigningKeyPair.from_seed(b"\x01" * 32)
    enc = EncryptionKeyPair.from_private_bytes(b"\x02" * 32)
    return sig.public_key, enc.public_key


def test_fingerprint_is_deterministic_for_same_inputs() -> None:
    sig_pk, enc_pk = _sample_identity()
    fp1 = fingerprint("alice@example.com", sig_pk, enc_pk)
    fp2 = fingerprint("alice@example.com", sig_pk, enc_pk)
    assert fp1 == fp2
    assert len(fp1) == FINGERPRINT_BYTES


def test_fingerprint_invariant_across_address_case_and_whitespace() -> None:
    sig_pk, enc_pk = _sample_identity()
    fp_lower = fingerprint("alice@example.com", sig_pk, enc_pk)
    fp_upper = fingerprint("  ALICE@EXAMPLE.COM  ", sig_pk, enc_pk)
    assert fp_lower == fp_upper


def test_fingerprint_changes_with_address() -> None:
    sig_pk, enc_pk = _sample_identity()
    assert fingerprint("alice@example.com", sig_pk, enc_pk) != fingerprint(
        "bob@example.com", sig_pk, enc_pk
    )


def test_fingerprint_changes_with_signing_key() -> None:
    _, enc_pk = _sample_identity()
    sig_a = SigningKeyPair.from_seed(b"\xaa" * 32).public_key
    sig_b = SigningKeyPair.from_seed(b"\xbb" * 32).public_key
    assert fingerprint("alice@example.com", sig_a, enc_pk) != fingerprint(
        "alice@example.com", sig_b, enc_pk
    )


def test_fingerprint_changes_with_encryption_key() -> None:
    sig_pk, _ = _sample_identity()
    enc_a = EncryptionKeyPair.from_private_bytes(b"\xaa" * 32).public_key
    enc_b = EncryptionKeyPair.from_private_bytes(b"\xbb" * 32).public_key
    assert fingerprint("alice@example.com", sig_pk, enc_a) != fingerprint(
        "alice@example.com", sig_pk, enc_b
    )


def test_fingerprint_known_vector_stable() -> None:
    """Lock the fingerprint for a known (address, sig_pk, enc_pk) tuple.

    If this test fails we have accidentally changed the fingerprint scheme —
    which would invalidate every stored TOFU pin. Update this value only when
    bumping the fingerprint domain string intentionally.
    """
    sig_pk, enc_pk = _sample_identity()
    fp = fingerprint("alice@example.com", sig_pk, enc_pk)
    assert fp.hex() == KNOWN_ALICE_FP_HEX


# Expected fingerprint for:
#   address       = "alice@example.com"
#   signing seed  = b"\x01" * 32
#   encryption sk = b"\x02" * 32
# Pinned by test_fingerprint_known_vector_stable.
KNOWN_ALICE_FP_HEX = "cdc6e694d8699f0fc18657242abade83"


# -- format_fingerprint -------------------------------------------------------


def test_format_fingerprint_groups_default() -> None:
    fp = bytes.fromhex("a1b2c3d4e5f607080910111213141516")
    out = format_fingerprint(fp)
    # Default: 4 groups of 4 hex chars.
    assert out == "a1b2 c3d4 e5f6 0708"


def test_format_fingerprint_custom_groups() -> None:
    fp = bytes.fromhex("a1b2c3d4e5f607080910111213141516")
    assert format_fingerprint(fp, groups=2, group_size=8) == "a1b2c3d4 e5f60708"


def test_format_fingerprint_rejects_invalid_params() -> None:
    with pytest.raises(TypeError):
        format_fingerprint("not bytes")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        format_fingerprint(b"\x00" * 16, groups=0)
    with pytest.raises(ValueError):
        format_fingerprint(b"\x00" * 16, group_size=0)
