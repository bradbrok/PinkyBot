"""Envelope serialization tests: round-trip, header parsing, malformed inputs."""

from __future__ import annotations

import pytest

from pinky_federation.envelope import (
    MAX_CIPHERTEXT_BYTES,
    Envelope,
    EnvelopeVersion,
)
from pinky_federation.errors import EnvelopeError


def _sample_envelope(ct: bytes = b"ciphertext-goes-here") -> Envelope:
    return Envelope(
        version=EnvelopeVersion.V1,
        sender_fingerprint=b"\x01" * 16,
        recipient_fingerprint=b"\x02" * 16,
        ephemeral_public=b"\x03" * 32,
        nonce=b"\x04" * 24,
        signature=b"\x05" * 64,
        ciphertext=ct,
    )


def test_envelope_round_trip() -> None:
    env = _sample_envelope()
    data = env.to_bytes()
    restored = Envelope.from_bytes(data)
    assert restored == env


def test_envelope_serialized_length_matches_spec() -> None:
    env = _sample_envelope(ct=b"")
    data = env.to_bytes()
    # 4 magic + 1 ver + 1 flags + 16 + 16 + 32 + 24 + 64 + 4 = 162 fixed header.
    assert len(data) == 162
    env2 = _sample_envelope(ct=b"x" * 100)
    assert len(env2.to_bytes()) == 162 + 100


def test_envelope_magic_check() -> None:
    env = _sample_envelope()
    data = bytearray(env.to_bytes())
    data[0] = ord("Q")
    with pytest.raises(EnvelopeError, match="magic"):
        Envelope.from_bytes(bytes(data))


def test_envelope_version_check() -> None:
    env = _sample_envelope()
    data = bytearray(env.to_bytes())
    data[4] = 99  # version byte
    with pytest.raises(EnvelopeError, match="unsupported envelope version"):
        Envelope.from_bytes(bytes(data))


def test_envelope_flags_must_be_zero() -> None:
    env = _sample_envelope()
    data = bytearray(env.to_bytes())
    data[5] = 0x80  # flags byte
    with pytest.raises(EnvelopeError, match="flags"):
        Envelope.from_bytes(bytes(data))


def test_envelope_short_header_rejected() -> None:
    with pytest.raises(EnvelopeError, match="shorter than fixed header"):
        Envelope.from_bytes(b"PFv1")


def test_envelope_ct_length_mismatch_rejected() -> None:
    env = _sample_envelope(ct=b"hello")
    data = env.to_bytes()
    # Truncate ciphertext but leave header ct_len untouched.
    with pytest.raises(EnvelopeError, match="length mismatch"):
        Envelope.from_bytes(data[:-1])


def test_envelope_ct_length_too_large_rejected() -> None:
    env = _sample_envelope(ct=b"hello")
    data = bytearray(env.to_bytes())
    # Rewrite ct_len field (last 4 bytes of the 162-byte fixed header) to overflow.
    huge = (MAX_CIPHERTEXT_BYTES + 1).to_bytes(4, "big")
    data[158:162] = huge
    with pytest.raises(EnvelopeError, match="exceeds MAX_CIPHERTEXT_BYTES"):
        Envelope.from_bytes(bytes(data))


def test_envelope_construct_rejects_wrong_field_sizes() -> None:
    with pytest.raises(ValueError):
        Envelope(
            version=EnvelopeVersion.V1,
            sender_fingerprint=b"\x01" * 8,  # wrong size
            recipient_fingerprint=b"\x02" * 16,
            ephemeral_public=b"\x03" * 32,
            nonce=b"\x04" * 24,
            signature=b"\x05" * 64,
            ciphertext=b"",
        )


def test_envelope_rejects_non_bytes_input() -> None:
    with pytest.raises(EnvelopeError, match="must be bytes"):
        Envelope.from_bytes("not bytes")  # type: ignore[arg-type]


def test_envelope_deterministic_wire_vector() -> None:
    """Lock the wire format for a fixed envelope.

    If this hex changes, the wire protocol has changed — bump the version.
    """
    env = _sample_envelope(ct=b"hello")
    expected = (
        "50467631"  # magic "PFv1"
        "01"  # version
        "00"  # flags
        + ("01" * 16)  # sender_fp
        + ("02" * 16)  # recipient_fp
        + ("03" * 32)  # eph_pk
        + ("04" * 24)  # nonce
        + ("05" * 64)  # signature
        + "00000005"  # ct_len = 5
        + "68656c6c6f"  # "hello"
    )
    assert env.to_bytes().hex() == expected
