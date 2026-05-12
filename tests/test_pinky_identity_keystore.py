"""Tests for pinky_identity.keystore (encrypted-at-rest wrap/unwrap)."""

from __future__ import annotations

import os
import secrets
import stat
from dataclasses import replace

import pytest

from pinky_identity import (
    SignatureError,
    fingerprint,
    generate_keypair,
    kid_from_public_key,
)
from pinky_identity.keystore import (
    DEVICE_KEY_BYTES,
    WRAP_VERSION,
    DeviceKey,
    KeystoreError,
    WrapVersionError,
    deserialize_wrapped,
    serialize_wrapped,
    unwrap_keypair,
    wrap_keypair,
)

# -- Fixtures ----------------------------------------------------------------


@pytest.fixture
def device_key():
    return DeviceKey.from_bytes(secrets.token_bytes(DEVICE_KEY_BYTES))


@pytest.fixture
def keypair():
    return generate_keypair()


# -- DeviceKey: construction -------------------------------------------------


def test_device_key_from_bytes_round_trip():
    raw = secrets.token_bytes(DEVICE_KEY_BYTES)
    dk = DeviceKey.from_bytes(raw)
    assert dk.material_insecure() == raw


def test_device_key_rejects_wrong_length():
    with pytest.raises(ValueError, match="32 bytes"):
        DeviceKey.from_bytes(b"\x00" * 16)


def test_device_key_repr_redacts_material():
    dk = DeviceKey.from_bytes(b"\xaa" * 32)
    text = repr(dk)
    # Should reveal the 4-byte tag for debugging but not the full key.
    assert "aaaaaaaa" in text
    assert "aaaaaaaaaaaaaaaaaaaaaaaa" not in text  # not the full 32 bytes


# -- DeviceKey: file-backed --------------------------------------------------


def test_device_key_load_or_create_generates_on_first_use(tmp_path):
    path = tmp_path / "subdir" / ".device_key"
    dk = DeviceKey.load_or_create(str(path))
    assert path.exists()
    assert len(path.read_bytes()) == DEVICE_KEY_BYTES
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600
    assert dk.material_insecure() == path.read_bytes()


def test_device_key_load_or_create_returns_existing(tmp_path):
    path = tmp_path / ".device_key"
    material = secrets.token_bytes(DEVICE_KEY_BYTES)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, material)
    finally:
        os.close(fd)
    dk = DeviceKey.load_or_create(str(path))
    assert dk.material_insecure() == material


def test_device_key_load_or_create_rejects_wrong_length(tmp_path):
    path = tmp_path / ".device_key"
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, b"too-short")
    finally:
        os.close(fd)
    with pytest.raises(ValueError, match="expected 32"):
        DeviceKey.load_or_create(str(path))


def test_device_key_load_or_create_rejects_loose_permissions(tmp_path):
    path = tmp_path / ".device_key"
    path.write_bytes(secrets.token_bytes(DEVICE_KEY_BYTES))
    os.chmod(path, 0o644)
    with pytest.raises(PermissionError, match="loose permissions"):
        DeviceKey.load_or_create(str(path))


# -- wrap / unwrap roundtrip -------------------------------------------------


def test_wrap_unwrap_round_trips(keypair, device_key):
    wrapped = wrap_keypair(keypair, device_key=device_key)
    assert wrapped.version == WRAP_VERSION
    assert wrapped.kid == kid_from_public_key(keypair.public)
    assert wrapped.fingerprint == fingerprint(keypair.public)
    assert wrapped.public_key_raw == keypair.public.raw

    unwrapped = unwrap_keypair(wrapped, device_key=device_key)
    assert unwrapped.public == keypair.public
    assert (
        unwrapped.secret.secret_bytes_insecure()
        == keypair.secret.secret_bytes_insecure()
    )


def test_wrap_produces_distinct_ciphertexts_for_same_keypair(keypair, device_key):
    # Random nonce → distinct ciphertexts per wrap, even for the same key.
    w1 = wrap_keypair(keypair, device_key=device_key)
    w2 = wrap_keypair(keypair, device_key=device_key)
    assert w1.nonce != w2.nonce
    assert w1.ciphertext != w2.ciphertext
    # But both unwrap to the same keypair.
    assert unwrap_keypair(w1, device_key=device_key).public == keypair.public
    assert unwrap_keypair(w2, device_key=device_key).public == keypair.public


def test_wrap_ciphertext_is_seed_length_plus_tag(keypair, device_key):
    # Poly1305 tag = 16 bytes; seed = 32 bytes; total = 48.
    wrapped = wrap_keypair(keypair, device_key=device_key)
    assert len(wrapped.ciphertext) == 48


# -- unwrap: failure modes ---------------------------------------------------


def test_unwrap_rejects_wrong_device_key(keypair, device_key):
    wrapped = wrap_keypair(keypair, device_key=device_key)
    other_dk = DeviceKey.from_bytes(secrets.token_bytes(DEVICE_KEY_BYTES))
    with pytest.raises(KeystoreError):
        unwrap_keypair(wrapped, device_key=other_dk)


def test_unwrap_rejects_tampered_ciphertext(keypair, device_key):
    wrapped = wrap_keypair(keypair, device_key=device_key)
    bad = bytearray(wrapped.ciphertext)
    bad[0] ^= 0x01
    tampered = replace(wrapped, ciphertext=bytes(bad))
    with pytest.raises(KeystoreError):
        unwrap_keypair(tampered, device_key=device_key)


def test_unwrap_rejects_tampered_nonce(keypair, device_key):
    wrapped = wrap_keypair(keypair, device_key=device_key)
    bad_nonce = bytearray(wrapped.nonce)
    bad_nonce[0] ^= 0x01
    tampered = replace(wrapped, nonce=bytes(bad_nonce))
    with pytest.raises(KeystoreError):
        unwrap_keypair(tampered, device_key=device_key)


def test_unwrap_rejects_swapped_kid(keypair, device_key):
    # AAD pins kid; swapping kid to a different value breaks the AEAD tag.
    other_kp = generate_keypair()
    wrapped = wrap_keypair(keypair, device_key=device_key)
    tampered = replace(wrapped, kid=kid_from_public_key(other_kp.public))
    with pytest.raises(KeystoreError):
        unwrap_keypair(tampered, device_key=device_key)


def test_unwrap_rejects_swapped_fingerprint(keypair, device_key):
    other_kp = generate_keypair()
    wrapped = wrap_keypair(keypair, device_key=device_key)
    tampered = replace(wrapped, fingerprint=fingerprint(other_kp.public))
    with pytest.raises(KeystoreError):
        unwrap_keypair(tampered, device_key=device_key)


def test_unwrap_rejects_swapped_public_key_raw(keypair, device_key):
    # Even if AAD (kid/fingerprint) is unchanged, swapping the cleartext
    # public_key_raw should be caught by the post-decrypt cross-check.
    # In practice, kid and fingerprint are derived from public_key_raw, so
    # tampering only the raw bytes leaves the AAD honest — the post-decrypt
    # check is the safety net.
    other_kp = generate_keypair()
    wrapped = wrap_keypair(keypair, device_key=device_key)
    tampered = replace(wrapped, public_key_raw=other_kp.public.raw)
    with pytest.raises(KeystoreError, match="public key in wrapper"):
        unwrap_keypair(tampered, device_key=device_key)


def test_unwrap_rejects_unsupported_version(keypair, device_key):
    wrapped = wrap_keypair(keypair, device_key=device_key)
    future = replace(wrapped, version=WRAP_VERSION + 99)
    with pytest.raises(WrapVersionError):
        unwrap_keypair(future, device_key=device_key)


def test_keystore_error_is_signature_error_subclass(keypair, device_key):
    # Callers should be able to catch identity-layer failures with the
    # umbrella SignatureError.
    wrapped = wrap_keypair(keypair, device_key=device_key)
    other_dk = DeviceKey.from_bytes(secrets.token_bytes(DEVICE_KEY_BYTES))
    with pytest.raises(SignatureError):
        unwrap_keypair(wrapped, device_key=other_dk)


# -- Type guards -------------------------------------------------------------


def test_wrap_rejects_non_keypair(device_key):
    with pytest.raises(TypeError):
        wrap_keypair("not a keypair", device_key=device_key)  # type: ignore[arg-type]


def test_wrap_rejects_non_device_key(keypair):
    with pytest.raises(TypeError):
        wrap_keypair(keypair, device_key=b"\x00" * 32)  # type: ignore[arg-type]


def test_unwrap_rejects_non_wrapped(device_key):
    with pytest.raises(TypeError):
        unwrap_keypair({"not": "a wrapped keypair"}, device_key=device_key)  # type: ignore[arg-type]


# -- Public-key accessor -----------------------------------------------------


def test_wrapped_public_returns_public_key(keypair, device_key):
    wrapped = wrap_keypair(keypair, device_key=device_key)
    pk = wrapped.public()
    assert pk == keypair.public


# -- Repr does not leak secret material --------------------------------------


def test_wrapped_repr_redacts_ciphertext(keypair, device_key):
    wrapped = wrap_keypair(keypair, device_key=device_key)
    text = repr(wrapped)
    assert "redacted" in text
    assert wrapped.ciphertext.hex() not in text


# -- Serialization round-trip ------------------------------------------------


def test_serialize_deserialize_round_trips(keypair, device_key):
    wrapped = wrap_keypair(keypair, device_key=device_key)
    blob = serialize_wrapped(wrapped)
    # All values are JSON-friendly types.
    import json

    rehydrated = deserialize_wrapped(json.loads(json.dumps(blob)))
    assert rehydrated == wrapped
    assert unwrap_keypair(rehydrated, device_key=device_key).public == keypair.public


def test_deserialize_rejects_missing_field():
    with pytest.raises(ValueError, match="missing"):
        deserialize_wrapped({"version": 1, "kid": "abc"})


def test_deserialize_rejects_malformed_hex():
    blob = {
        "version": 1,
        "kid": "abc",
        "fingerprint": "deadbeef",
        "public_key_hex": "not-hex!!",
        "nonce_hex": "00",
        "ciphertext_hex": "00",
    }
    with pytest.raises(ValueError, match="malformed"):
        deserialize_wrapped(blob)


# -- AAD purpose isolation ---------------------------------------------------


def test_aad_purpose_pin_blocks_cross_system_replay(keypair, device_key):
    # Construct a "wrap-like" ciphertext under the same device key but
    # with a different AAD purpose; it must NOT decrypt with our unwrap.
    from nacl.bindings import crypto_aead_xchacha20poly1305_ietf_encrypt

    wrapped = wrap_keypair(keypair, device_key=device_key)
    foreign_aad = b"some-other-system|v=1|kid=" + wrapped.kid.encode()
    ct = crypto_aead_xchacha20poly1305_ietf_encrypt(
        keypair.secret.secret_bytes_insecure(),
        foreign_aad,
        wrapped.nonce,
        device_key.material_insecure(),
    )
    foreign = replace(wrapped, ciphertext=ct)
    with pytest.raises(KeystoreError):
        unwrap_keypair(foreign, device_key=device_key)
