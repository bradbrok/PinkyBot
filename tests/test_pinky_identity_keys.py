"""Tests for pinky_identity.keys — Ed25519 primitives for service-auth identity."""

from __future__ import annotations

import base64
import hashlib

import pytest
from nacl.signing import SigningKey as _NaclSigningKey

from pinky_identity import keys as ik

# -- generate_keypair --------------------------------------------------------


class TestGenerateKeypair:
    def test_returns_signing_keypair_with_correct_lengths(self):
        kp = ik.generate_keypair()
        assert isinstance(kp, ik.SigningKeypair)
        assert isinstance(kp.secret, ik.SecretKey)
        assert isinstance(kp.public, ik.PublicKey)
        assert len(kp.secret.secret_bytes_insecure()) == ik.ED25519_SECRET_KEY_BYTES
        assert len(kp.public.raw) == ik.ED25519_PUBLIC_KEY_BYTES

    def test_two_generations_differ(self):
        # Vanishingly unlikely to collide. Catches a broken RNG.
        a = ik.generate_keypair()
        b = ik.generate_keypair()
        assert a.secret.secret_bytes_insecure() != b.secret.secret_bytes_insecure()
        assert a.public.raw != b.public.raw

    def test_public_matches_secret_derivation(self):
        """Public key in the keypair must be the Ed25519 derivation of the seed."""
        kp = ik.generate_keypair()
        derived = _NaclSigningKey(kp.secret.secret_bytes_insecure()).verify_key.encode()
        assert kp.public.raw == bytes(derived)


# -- public_key_from_bytes ----------------------------------------------------


class TestPublicKeyFromBytes:
    def test_valid_bytes_roundtrip(self):
        kp = ik.generate_keypair()
        loaded = ik.public_key_from_bytes(kp.public.raw)
        assert loaded.raw == kp.public.raw
        assert loaded == kp.public  # frozen dataclass equality on raw

    def test_wrong_length_rejected(self):
        with pytest.raises(ValueError, match="32 bytes"):
            ik.public_key_from_bytes(b"\x00" * 31)
        with pytest.raises(ValueError, match="32 bytes"):
            ik.public_key_from_bytes(b"\x00" * 33)
        with pytest.raises(ValueError, match="32 bytes"):
            ik.public_key_from_bytes(b"")

    def test_non_bytes_rejected(self):
        with pytest.raises(ValueError):
            ik.public_key_from_bytes("not bytes")  # type: ignore[arg-type]


# -- SecretKey ---------------------------------------------------------------


class TestSecretKey:
    def test_wrong_length_rejected(self):
        with pytest.raises(ValueError, match="32 bytes"):
            ik.SecretKey(b"\x00" * 31)
        with pytest.raises(ValueError, match="32 bytes"):
            ik.SecretKey(b"\x00" * 64)

    def test_repr_redacts_secret_material(self):
        sk = ik.SecretKey(b"\xab" * 32)
        r = repr(sk)
        # Repr must not contain any byte of the secret in hex form.
        assert "ab" not in r.lower()
        assert "redacted" in r.lower()

    def test_secret_bytes_insecure_returns_raw(self):
        seed = bytes(range(32))
        sk = ik.SecretKey(seed)
        assert sk.secret_bytes_insecure() == seed

    def test_signing_keypair_repr_does_not_leak_secret(self):
        kp = ik.generate_keypair()
        r = repr(kp)
        # Should reference public via kid, not show secret hex.
        assert "redacted" not in r.lower() or "kid" in r.lower()
        # Secret hex should not appear in repr.
        secret_hex = kp.secret.secret_bytes_insecure().hex()
        assert secret_hex not in r

    def test_public_key_repr_uses_kid_not_raw_bytes(self):
        kp = ik.generate_keypair()
        r = repr(kp.public)
        assert "kid=" in r
        # Public is safe to print but kid is more useful — assert raw bytes
        # aren't included as a hex blob (>= 32 hex chars would be suspicious).
        pub_hex = kp.public.raw.hex()
        assert pub_hex not in r


# -- sign + verify -----------------------------------------------------------


class TestSignVerify:
    def test_sign_then_verify_roundtrip(self):
        kp = ik.generate_keypair()
        msg = b"hello world"
        sig = kp.sign(msg)
        assert len(sig) == ik.ED25519_SIGNATURE_BYTES
        # Must not raise.
        kp.public.verify(msg, sig)

    def test_signature_is_deterministic(self):
        """RFC 8032 §5.1.6 — Ed25519 signatures are deterministic over (key, msg)."""
        kp = ik.generate_keypair()
        msg = b"determinism check"
        s1 = kp.sign(msg)
        s2 = kp.sign(msg)
        assert s1 == s2

    def test_verify_rejects_tampered_message(self):
        kp = ik.generate_keypair()
        sig = kp.sign(b"original")
        with pytest.raises(ik.SignatureError):
            kp.public.verify(b"tampered", sig)

    def test_verify_rejects_wrong_key(self):
        kp1 = ik.generate_keypair()
        kp2 = ik.generate_keypair()
        sig = kp1.sign(b"msg")
        with pytest.raises(ik.SignatureError):
            kp2.public.verify(b"msg", sig)

    def test_verify_rejects_short_signature(self):
        kp = ik.generate_keypair()
        with pytest.raises(ik.SignatureError, match="64 bytes"):
            kp.public.verify(b"msg", b"\x00" * 32)

    def test_verify_rejects_long_signature(self):
        kp = ik.generate_keypair()
        with pytest.raises(ik.SignatureError, match="64 bytes"):
            kp.public.verify(b"msg", b"\x00" * 128)

    def test_verify_rejects_zero_signature(self):
        """All-zero signature must not verify against a random key+msg."""
        kp = ik.generate_keypair()
        with pytest.raises(ik.SignatureError):
            kp.public.verify(b"msg", b"\x00" * ik.ED25519_SIGNATURE_BYTES)


# -- fingerprint + kid -------------------------------------------------------


class TestFingerprintAndKid:
    def test_fingerprint_is_sha256_hex_of_public(self):
        kp = ik.generate_keypair()
        fp = ik.fingerprint(kp.public)
        assert fp == hashlib.sha256(kp.public.raw).hexdigest()
        assert len(fp) == 64
        assert all(c in "0123456789abcdef" for c in fp)

    def test_kid_is_prefix_of_fingerprint(self):
        kp = ik.generate_keypair()
        kid = ik.kid_from_public_key(kp.public)
        fp = ik.fingerprint(kp.public)
        assert kid == fp[: ik.KID_HEX_LEN]
        assert len(kid) == ik.KID_HEX_LEN

    def test_kid_and_fingerprint_are_deterministic(self):
        kp = ik.generate_keypair()
        assert ik.kid_from_public_key(kp.public) == ik.kid_from_public_key(kp.public)
        assert ik.fingerprint(kp.public) == ik.fingerprint(kp.public)

    def test_different_keys_give_different_kids(self):
        # Strong claim — fingerprints differ with overwhelming probability,
        # and the kid prefix inherits that uniqueness within a fleet.
        kids = {ik.kid_from_public_key(ik.generate_keypair().public) for _ in range(50)}
        assert len(kids) == 50

    def test_kid_format_is_hex(self):
        kp = ik.generate_keypair()
        kid = ik.kid_from_public_key(kp.public)
        assert all(c in "0123456789abcdef" for c in kid)


# -- JWK export --------------------------------------------------------------


class TestJwkExport:
    def test_okp_jwk_shape(self):
        kp = ik.generate_keypair()
        jwk = ik.jwk_export(kp.public)
        assert jwk["kty"] == "OKP"
        assert jwk["crv"] == "Ed25519"
        assert jwk["alg"] == "EdDSA"
        assert jwk["use"] == "sig"
        assert jwk["kid"] == ik.kid_from_public_key(kp.public)
        # x is base64url without padding.
        assert "=" not in jwk["x"]
        assert "+" not in jwk["x"]
        assert "/" not in jwk["x"]

    def test_jwk_x_decodes_to_public_key_bytes(self):
        kp = ik.generate_keypair()
        jwk = ik.jwk_export(kp.public)
        # Re-add padding for decoding.
        x = jwk["x"]
        padded = x + "=" * (-len(x) % 4)
        decoded = base64.urlsafe_b64decode(padded)
        assert decoded == kp.public.raw

    def test_jwk_custom_kid(self):
        kp = ik.generate_keypair()
        jwk = ik.jwk_export(kp.public, kid="custom-kid")
        assert jwk["kid"] == "custom-kid"

    def test_jwk_export_returns_fresh_dict_each_call(self):
        """Callers must be free to mutate the returned dict without sharing state."""
        kp = ik.generate_keypair()
        a = ik.jwk_export(kp.public)
        b = ik.jwk_export(kp.public)
        a["use"] = "enc"  # mutate one
        assert b["use"] == "sig"  # other unaffected


# -- Cross-key isolation -----------------------------------------------------


class TestCrossKeyIsolation:
    def test_signatures_do_not_cross_verify(self):
        """The core cross-agent-impersonation defense at the crypto layer:
        a signature made by key A cannot be verified by key B's public key.

        Higher-level cross-agent tests live in the daemon-local signer and
        verifier libraries — this test pins the underlying invariant so a
        regression in the wrapper would surface here first.
        """
        kp_lera = ik.generate_keypair()
        kp_olga = ik.generate_keypair()
        msg = b"i am lera"
        sig = kp_lera.sign(msg)
        # Lera's pubkey verifies — that's the baseline.
        kp_lera.public.verify(msg, sig)
        # Olga's pubkey must NOT verify it.
        with pytest.raises(ik.SignatureError):
            kp_olga.public.verify(msg, sig)


# -- Public package exports --------------------------------------------------


class TestPackageExports:
    def test_public_api_is_importable_from_package_root(self):
        """Anything we promise via __all__ must import cleanly from the
        package root. Catches typos in __init__.py + accidental moves.
        """
        import pinky_identity

        for name in pinky_identity.__all__:
            assert hasattr(pinky_identity, name), f"{name} missing from pinky_identity"
