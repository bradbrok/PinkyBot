"""Sealed-box-v1 end-to-end tests: round-trip, tamper detection, test vectors."""

from __future__ import annotations

import pytest

from pinky_federation.envelope import Envelope
from pinky_federation.errors import DecryptionError, SignatureError
from pinky_federation.keys import (
    EncryptionKeyPair,
    SigningKeyPair,
    SigningPublicKey,
)
from pinky_federation.sealed_box import (
    SEALED_BOX_VERSION,
    seal,
    unseal,
)

# -- Fixtures -----------------------------------------------------------------


@pytest.fixture
def alice_signing() -> SigningKeyPair:
    return SigningKeyPair.from_seed(b"\x11" * 32)


@pytest.fixture
def bob_encryption() -> EncryptionKeyPair:
    return EncryptionKeyPair.from_private_bytes(b"\x22" * 32)


@pytest.fixture
def alice_fp() -> bytes:
    return b"\xa1" * 16


@pytest.fixture
def bob_fp() -> bytes:
    return b"\xb0" * 16


# -- Round-trip ---------------------------------------------------------------


def test_seal_unseal_round_trip(
    alice_signing: SigningKeyPair,
    bob_encryption: EncryptionKeyPair,
    alice_fp: bytes,
    bob_fp: bytes,
) -> None:
    plaintext = b"hello bob, this is alice"
    env = seal(
        plaintext,
        sender_signing=alice_signing,
        sender_fingerprint=alice_fp,
        recipient_encryption=bob_encryption.public_key,
        recipient_fingerprint=bob_fp,
    )
    assert isinstance(env, Envelope)
    assert env.version is SEALED_BOX_VERSION
    assert env.sender_fingerprint == alice_fp
    assert env.recipient_fingerprint == bob_fp

    out = unseal(
        env,
        recipient_encryption=bob_encryption,
        sender_signing_public=alice_signing.public_key,
    )
    assert out == plaintext


def test_seal_empty_plaintext_round_trip(
    alice_signing: SigningKeyPair,
    bob_encryption: EncryptionKeyPair,
    alice_fp: bytes,
    bob_fp: bytes,
) -> None:
    env = seal(
        b"",
        sender_signing=alice_signing,
        sender_fingerprint=alice_fp,
        recipient_encryption=bob_encryption.public_key,
        recipient_fingerprint=bob_fp,
    )
    out = unseal(
        env,
        recipient_encryption=bob_encryption,
        sender_signing_public=alice_signing.public_key,
    )
    assert out == b""


def test_envelope_wire_round_trip_after_seal(
    alice_signing: SigningKeyPair,
    bob_encryption: EncryptionKeyPair,
    alice_fp: bytes,
    bob_fp: bytes,
) -> None:
    env = seal(
        b"across the wire",
        sender_signing=alice_signing,
        sender_fingerprint=alice_fp,
        recipient_encryption=bob_encryption.public_key,
        recipient_fingerprint=bob_fp,
    )
    restored = Envelope.from_bytes(env.to_bytes())
    out = unseal(
        restored,
        recipient_encryption=bob_encryption,
        sender_signing_public=alice_signing.public_key,
    )
    assert out == b"across the wire"


# -- Tamper detection ---------------------------------------------------------


def test_unseal_rejects_tampered_ciphertext(
    alice_signing: SigningKeyPair,
    bob_encryption: EncryptionKeyPair,
    alice_fp: bytes,
    bob_fp: bytes,
) -> None:
    env = seal(
        b"secret",
        sender_signing=alice_signing,
        sender_fingerprint=alice_fp,
        recipient_encryption=bob_encryption.public_key,
        recipient_fingerprint=bob_fp,
    )
    # Flip a ciphertext bit — AEAD should reject.
    tampered_ct = bytearray(env.ciphertext)
    tampered_ct[0] ^= 0x01
    tampered = Envelope(
        version=env.version,
        sender_fingerprint=env.sender_fingerprint,
        recipient_fingerprint=env.recipient_fingerprint,
        ephemeral_public=env.ephemeral_public,
        nonce=env.nonce,
        signature=env.signature,
        ciphertext=bytes(tampered_ct),
    )
    # Signature covers ciphertext, so the first failure is actually SignatureError.
    with pytest.raises(SignatureError):
        unseal(
            tampered,
            recipient_encryption=bob_encryption,
            sender_signing_public=alice_signing.public_key,
        )


def test_unseal_rejects_wrong_sender_pubkey(
    alice_signing: SigningKeyPair,
    bob_encryption: EncryptionKeyPair,
    alice_fp: bytes,
    bob_fp: bytes,
) -> None:
    env = seal(
        b"secret",
        sender_signing=alice_signing,
        sender_fingerprint=alice_fp,
        recipient_encryption=bob_encryption.public_key,
        recipient_fingerprint=bob_fp,
    )
    impostor = SigningKeyPair.generate().public_key
    with pytest.raises(SignatureError):
        unseal(
            env,
            recipient_encryption=bob_encryption,
            sender_signing_public=impostor,
        )


def test_unseal_rejects_wrong_recipient_key(
    alice_signing: SigningKeyPair,
    bob_encryption: EncryptionKeyPair,
    alice_fp: bytes,
    bob_fp: bytes,
) -> None:
    env = seal(
        b"secret",
        sender_signing=alice_signing,
        sender_fingerprint=alice_fp,
        recipient_encryption=bob_encryption.public_key,
        recipient_fingerprint=bob_fp,
    )
    mallory = EncryptionKeyPair.generate()
    # Wrong recipient → AEAD key mismatch → DecryptionError (signature still valid).
    with pytest.raises(DecryptionError):
        unseal(
            env,
            recipient_encryption=mallory,
            sender_signing_public=alice_signing.public_key,
        )


def test_unseal_rejects_rebound_recipient_fingerprint(
    alice_signing: SigningKeyPair,
    bob_encryption: EncryptionKeyPair,
    alice_fp: bytes,
    bob_fp: bytes,
) -> None:
    env = seal(
        b"secret",
        sender_signing=alice_signing,
        sender_fingerprint=alice_fp,
        recipient_encryption=bob_encryption.public_key,
        recipient_fingerprint=bob_fp,
    )
    # Attacker swaps recipient_fingerprint without re-signing → sig fails.
    rebound = Envelope(
        version=env.version,
        sender_fingerprint=env.sender_fingerprint,
        recipient_fingerprint=b"\xcc" * 16,
        ephemeral_public=env.ephemeral_public,
        nonce=env.nonce,
        signature=env.signature,
        ciphertext=env.ciphertext,
    )
    with pytest.raises(SignatureError):
        unseal(
            rebound,
            recipient_encryption=bob_encryption,
            sender_signing_public=alice_signing.public_key,
        )


def test_unseal_rejects_replayed_sender_fingerprint(
    alice_signing: SigningKeyPair,
    bob_encryption: EncryptionKeyPair,
    alice_fp: bytes,
    bob_fp: bytes,
) -> None:
    env = seal(
        b"secret",
        sender_signing=alice_signing,
        sender_fingerprint=alice_fp,
        recipient_encryption=bob_encryption.public_key,
        recipient_fingerprint=bob_fp,
    )
    rebound = Envelope(
        version=env.version,
        sender_fingerprint=b"\xee" * 16,
        recipient_fingerprint=env.recipient_fingerprint,
        ephemeral_public=env.ephemeral_public,
        nonce=env.nonce,
        signature=env.signature,
        ciphertext=env.ciphertext,
    )
    with pytest.raises(SignatureError):
        unseal(
            rebound,
            recipient_encryption=bob_encryption,
            sender_signing_public=alice_signing.public_key,
        )


def test_unseal_rejects_unsupported_version(
    alice_signing: SigningKeyPair,
    bob_encryption: EncryptionKeyPair,
    alice_fp: bytes,
    bob_fp: bytes,
) -> None:
    env = seal(
        b"secret",
        sender_signing=alice_signing,
        sender_fingerprint=alice_fp,
        recipient_encryption=bob_encryption.public_key,
        recipient_fingerprint=bob_fp,
    )
    # Force an unknown version number past IntEnum validation.
    bogus = object.__new__(Envelope)
    object.__setattr__(bogus, "version", object())  # not EnvelopeVersion
    object.__setattr__(bogus, "sender_fingerprint", env.sender_fingerprint)
    object.__setattr__(bogus, "recipient_fingerprint", env.recipient_fingerprint)
    object.__setattr__(bogus, "ephemeral_public", env.ephemeral_public)
    object.__setattr__(bogus, "nonce", env.nonce)
    object.__setattr__(bogus, "signature", env.signature)
    object.__setattr__(bogus, "ciphertext", env.ciphertext)
    with pytest.raises(DecryptionError, match="unsupported envelope version"):
        unseal(
            bogus,  # type: ignore[arg-type]
            recipient_encryption=bob_encryption,
            sender_signing_public=alice_signing.public_key,
        )


# -- Input validation --------------------------------------------------------


def test_seal_rejects_wrong_fingerprint_sizes(
    alice_signing: SigningKeyPair,
    bob_encryption: EncryptionKeyPair,
    bob_fp: bytes,
) -> None:
    with pytest.raises(ValueError, match="sender_fingerprint"):
        seal(
            b"x",
            sender_signing=alice_signing,
            sender_fingerprint=b"\x00" * 8,
            recipient_encryption=bob_encryption.public_key,
            recipient_fingerprint=bob_fp,
        )
    with pytest.raises(ValueError, match="recipient_fingerprint"):
        seal(
            b"x",
            sender_signing=alice_signing,
            sender_fingerprint=b"\x00" * 16,
            recipient_encryption=bob_encryption.public_key,
            recipient_fingerprint=b"\x00" * 8,
        )


def test_seal_rejects_non_bytes_plaintext(
    alice_signing: SigningKeyPair,
    bob_encryption: EncryptionKeyPair,
    alice_fp: bytes,
    bob_fp: bytes,
) -> None:
    with pytest.raises(TypeError):
        seal(
            "not bytes",  # type: ignore[arg-type]
            sender_signing=alice_signing,
            sender_fingerprint=alice_fp,
            recipient_encryption=bob_encryption.public_key,
            recipient_fingerprint=bob_fp,
        )


def test_seal_nonce_override_requires_correct_size(
    alice_signing: SigningKeyPair,
    bob_encryption: EncryptionKeyPair,
    alice_fp: bytes,
    bob_fp: bytes,
) -> None:
    with pytest.raises(ValueError, match="nonce"):
        seal(
            b"x",
            sender_signing=alice_signing,
            sender_fingerprint=alice_fp,
            recipient_encryption=bob_encryption.public_key,
            recipient_fingerprint=bob_fp,
            nonce=b"\x00" * 8,
        )


# -- Deterministic test vector (for relay compat tests) -----------------------


def test_deterministic_vector_stable(
    alice_signing: SigningKeyPair,
    bob_encryption: EncryptionKeyPair,
    alice_fp: bytes,
    bob_fp: bytes,
) -> None:
    """Fully deterministic seal() output.

    Purpose: lock the exact wire bytes so the Python relay and any future
    clients can cross-verify. If this hex changes, the protocol has changed.

    Inputs:
        sender signing seed    = 0x11 * 32
        recipient encryption sk = 0x22 * 32
        ephemeral encryption sk = 0x33 * 32
        sender_fp              = 0xa1 * 16
        recipient_fp           = 0xb0 * 16
        nonce                  = 0x44 * 24
        plaintext              = b"hello bob"
    """
    ephemeral = EncryptionKeyPair.from_private_bytes(b"\x33" * 32)
    env = seal(
        b"hello bob",
        sender_signing=alice_signing,
        sender_fingerprint=alice_fp,
        recipient_encryption=bob_encryption.public_key,
        recipient_fingerprint=bob_fp,
        nonce=b"\x44" * 24,
        ephemeral=ephemeral,
    )
    wire = env.to_bytes()
    assert wire.hex() == KNOWN_VECTOR_HEX

    # And it must round-trip.
    out = unseal(
        env,
        recipient_encryption=bob_encryption,
        sender_signing_public=alice_signing.public_key,
    )
    assert out == b"hello bob"


# Expected wire bytes for the deterministic vector above. Pinned by
# test_deterministic_vector_stable. Update only on intentional protocol bumps.
KNOWN_VECTOR_HEX = (
    "504676310100"
    + ("a1" * 16)  # sender_fp
    + ("b0" * 16)  # recipient_fp
    + "7b0d47d93427f8311160781c7c733fd89f88970aef490d8aa0ee19a4cb8a1b14"  # eph_pk
    + ("44" * 24)  # nonce
    + "65b3193d2dbf9fbee57daecda9bfbdd2b030835b13b42e6d7994747e1f6436c4"
    + "c2c17ad573b2e788a03c6f647d2210d4df354a58c4bd4d4d52aac152933b0c05"  # sig
    + "00000019"  # ct_len = 25
    + "30065ea9d478499236d899a67f530cb513b720dfcc3704f4b2"  # ciphertext
)


def test_deterministic_vector_sender_pubkey_matches_seed(
    alice_signing: SigningKeyPair,
) -> None:
    """Sanity check so the vector above is easy to reconstruct in other languages."""
    expected = SigningKeyPair.from_seed(b"\x11" * 32).public_key.to_bytes()
    assert alice_signing.public_key.to_bytes() == expected
    assert isinstance(alice_signing.public_key, SigningPublicKey)
