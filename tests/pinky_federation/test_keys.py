"""Key primitive tests: generation, serialization, DH correctness, repr safety."""

from __future__ import annotations

import os

import pytest

from pinky_federation.errors import SignatureError
from pinky_federation.keys import (
    ED25519_KEY_BYTES,
    ED25519_SIG_BYTES,
    X25519_KEY_BYTES,
    EncryptionKeyPair,
    EncryptionPublicKey,
    SigningKeyPair,
    SigningPublicKey,
    x25519_public_from_private,
)

# -- Encryption keys ----------------------------------------------------------


def test_encryption_keypair_generate_produces_unique_keys() -> None:
    a = EncryptionKeyPair.generate()
    b = EncryptionKeyPair.generate()
    assert a.public_key.raw != b.public_key.raw
    assert a.private_bytes_insecure() != b.private_bytes_insecure()


def test_encryption_public_key_length_validation() -> None:
    with pytest.raises(ValueError):
        EncryptionPublicKey(b"\x00" * 31)
    with pytest.raises(ValueError):
        EncryptionPublicKey(b"\x00" * 33)


def test_encryption_keypair_round_trip_private_bytes() -> None:
    original = EncryptionKeyPair.generate()
    restored = EncryptionKeyPair.from_private_bytes(original.private_bytes_insecure())
    # Public keys must match exactly after reconstruction from private bytes.
    assert restored.public_key.raw == original.public_key.raw


def test_encryption_repr_hides_private_material() -> None:
    kp = EncryptionKeyPair.generate()
    text = repr(kp)
    # Private scalar never appears in repr; only a short public prefix does.
    assert kp.private_bytes_insecure().hex() not in text
    assert kp.public_key.raw.hex() not in text  # only a prefix shown


def test_dh_produces_symmetric_shared_secret() -> None:
    alice = EncryptionKeyPair.generate()
    bob = EncryptionKeyPair.generate()
    s1 = alice.dh(bob.public_key)
    s2 = bob.dh(alice.public_key)
    assert s1 == s2
    assert len(s1) == X25519_KEY_BYTES


def test_dh_rejects_wrong_type() -> None:
    alice = EncryptionKeyPair.generate()
    with pytest.raises(TypeError):
        alice.dh(b"\x00" * 32)  # type: ignore[arg-type]


def test_x25519_public_from_private_matches_keypair() -> None:
    sk_bytes = os.urandom(32)
    derived = x25519_public_from_private(sk_bytes)
    kp = EncryptionKeyPair.from_private_bytes(sk_bytes)
    assert derived == kp.public_key.raw


# -- Signing keys -------------------------------------------------------------


def test_signing_keypair_sign_verify_round_trip() -> None:
    kp = SigningKeyPair.generate()
    msg = b"hello federation"
    sig = kp.sign(msg)
    assert len(sig) == ED25519_SIG_BYTES
    # Should not raise.
    kp.public_key.verify(msg, sig)


def test_signing_verify_rejects_tampered_message() -> None:
    kp = SigningKeyPair.generate()
    sig = kp.sign(b"original")
    with pytest.raises(SignatureError):
        kp.public_key.verify(b"tampered", sig)


def test_signing_verify_rejects_wrong_key() -> None:
    kp = SigningKeyPair.generate()
    other = SigningKeyPair.generate()
    sig = kp.sign(b"hi")
    with pytest.raises(SignatureError):
        other.public_key.verify(b"hi", sig)


def test_signing_verify_rejects_wrong_sig_length() -> None:
    kp = SigningKeyPair.generate()
    with pytest.raises(SignatureError):
        kp.public_key.verify(b"hi", b"\x00" * 63)


def test_signing_from_seed_is_deterministic() -> None:
    seed = b"\x42" * ED25519_KEY_BYTES
    a = SigningKeyPair.from_seed(seed)
    b = SigningKeyPair.from_seed(seed)
    assert a.public_key.raw == b.public_key.raw
    # Signatures over the same message must match byte-for-byte (Ed25519 is deterministic).
    assert a.sign(b"msg") == b.sign(b"msg")


def test_signing_repr_hides_seed() -> None:
    kp = SigningKeyPair.generate()
    assert kp.seed_bytes_insecure().hex() not in repr(kp)


def test_signing_public_round_trip() -> None:
    kp = SigningKeyPair.generate()
    raw = kp.public_key.to_bytes()
    restored = SigningPublicKey.from_bytes(raw)
    assert restored.raw == raw


def test_signing_sign_rejects_non_bytes() -> None:
    kp = SigningKeyPair.generate()
    with pytest.raises(TypeError):
        kp.sign("not bytes")  # type: ignore[arg-type]
