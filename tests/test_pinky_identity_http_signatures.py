"""Tests for pinky_identity.http_signatures (RFC 9421 wrappers)."""

from __future__ import annotations

import base64
import hashlib
from datetime import datetime, timedelta, timezone

import pytest
import requests

from pinky_identity import (
    DEFAULT_TAG,
    HttpSignatureVerificationError,
    PinkyKeyResolver,
    attach_content_digest,
    compute_content_digest,
    generate_keypair,
    kid_from_public_key,
    sign_request,
    verify_request,
)
from pinky_identity.http_signatures import _strip_component_params

# -- Fixtures ----------------------------------------------------------------


@pytest.fixture
def keypair():
    return generate_keypair()


@pytest.fixture
def resolver(keypair):
    return PinkyKeyResolver.from_signing_keypairs([keypair])


def _prep_get(url: str = "https://api.example.com/v1/things") -> requests.PreparedRequest:
    return requests.Request("GET", url).prepare()


def _prep_post(
    url: str = "https://api.example.com/v1/things",
    json_body: dict | None = None,
) -> requests.PreparedRequest:
    return requests.Request("POST", url, json=json_body or {"hello": "world"}).prepare()


# -- Roundtrip ---------------------------------------------------------------


def test_sign_and_verify_roundtrip_get(keypair, resolver):
    req = _prep_get()
    kid = sign_request(req, keypair=keypair)

    assert kid == kid_from_public_key(keypair.public)
    assert "Signature" in req.headers
    assert "Signature-Input" in req.headers

    result = verify_request(req, key_resolver=resolver)

    assert result.parameters["keyid"] == kid
    assert result.parameters["alg"] == "ed25519"
    assert result.parameters["tag"] == DEFAULT_TAG
    assert "nonce" in result.parameters
    assert "created" in result.parameters


def test_sign_and_verify_roundtrip_post_with_content_digest(keypair, resolver):
    req = _prep_post()
    attach_content_digest(req)

    sign_request(
        req,
        keypair=keypair,
        covered_components=(
            "@method",
            "@target-uri",
            "@authority",
            "content-digest",
        ),
    )

    result = verify_request(
        req,
        key_resolver=resolver,
        required_components={"@method", "@target-uri", "content-digest"},
    )
    assert "content-digest" in {_strip_component_params(k) for k in result.covered_components}


def test_explicit_kid_override_signs_and_verifies(keypair):
    req = _prep_get()
    override = "agent-vacation-01"
    returned_kid = sign_request(req, keypair=keypair, kid=override)
    assert returned_kid == override

    # Caller has to publish the override kid the same way to the verifier.
    resolver = PinkyKeyResolver()
    resolver.public_keys[override] = keypair.public
    result = verify_request(req, key_resolver=resolver)
    assert result.parameters["keyid"] == override


# -- Tamper detection --------------------------------------------------------


def test_verify_rejects_tampered_signature(keypair, resolver):
    req = _prep_get()
    sign_request(req, keypair=keypair)

    # Flip a byte in the base64-encoded signature payload.
    sig = req.headers["Signature"]
    label, _, sig_value = sig.partition("=:")
    flipped = bytearray(base64.b64decode(sig_value.rstrip(":")))
    flipped[0] ^= 0x01
    req.headers["Signature"] = (
        f"{label}=:{base64.b64encode(bytes(flipped)).decode('ascii')}:"
    )

    with pytest.raises(HttpSignatureVerificationError):
        verify_request(req, key_resolver=resolver)


def test_verify_rejects_wrong_method(keypair, resolver):
    req = _prep_get()
    sign_request(req, keypair=keypair)
    req.method = "POST"
    with pytest.raises(HttpSignatureVerificationError):
        verify_request(req, key_resolver=resolver)


def test_verify_rejects_wrong_target_uri(keypair, resolver):
    req = _prep_get("https://api.example.com/v1/things")
    sign_request(req, keypair=keypair)
    req.url = "https://api.example.com/v1/OTHER"
    with pytest.raises(HttpSignatureVerificationError):
        verify_request(req, key_resolver=resolver)


def test_verify_rejects_wrong_authority(keypair, resolver):
    req = _prep_get("https://api.example.com/v1/things")
    sign_request(req, keypair=keypair)
    req.url = "https://evil.example.com/v1/things"
    with pytest.raises(HttpSignatureVerificationError):
        verify_request(req, key_resolver=resolver)


def test_verify_rejects_tampered_body(keypair, resolver):
    req = _prep_post()
    attach_content_digest(req)
    sign_request(
        req,
        keypair=keypair,
        covered_components=(
            "@method",
            "@target-uri",
            "@authority",
            "content-digest",
        ),
    )

    # Tamper the body but keep the original content-digest header — the
    # library doesn't recompute the digest at verify time, but the signed
    # digest no longer matches what the verifier would compute from the
    # new body. To exercise the spec path, we instead tamper the digest
    # header value (simulates either body or digest substitution).
    req.headers["Content-Digest"] = compute_content_digest(b"tampered!")

    with pytest.raises(HttpSignatureVerificationError):
        verify_request(
            req,
            key_resolver=resolver,
            required_components={"@method", "@target-uri", "content-digest"},
        )


# -- Policy rejections -------------------------------------------------------


def test_verify_rejects_tag_mismatch(keypair, resolver):
    req = _prep_get()
    sign_request(req, keypair=keypair, tag="some-other-purpose")
    with pytest.raises(HttpSignatureVerificationError):
        verify_request(req, key_resolver=resolver, expect_tag=DEFAULT_TAG)


def test_verify_rejects_unknown_kid(keypair):
    req = _prep_get()
    sign_request(req, keypair=keypair)
    # Verifier has a completely different keypair registered.
    other = generate_keypair()
    resolver = PinkyKeyResolver.from_signing_keypairs([other])
    with pytest.raises(HttpSignatureVerificationError):
        verify_request(req, key_resolver=resolver)


def test_verify_rejects_expired(keypair, resolver):
    req = _prep_get()
    long_ago = datetime.now(tz=timezone.utc) - timedelta(hours=1)
    sign_request(req, keypair=keypair, created=long_ago)
    with pytest.raises(HttpSignatureVerificationError):
        verify_request(
            req, key_resolver=resolver, max_age=timedelta(minutes=5)
        )


def test_verify_accepts_within_max_age(keypair, resolver):
    req = _prep_get()
    recent = datetime.now(tz=timezone.utc) - timedelta(seconds=10)
    sign_request(req, keypair=keypair, created=recent)
    result = verify_request(req, key_resolver=resolver, max_age=timedelta(minutes=5))
    assert result.parameters["tag"] == DEFAULT_TAG


def test_verify_rejects_missing_required_component(keypair, resolver):
    req = _prep_get()
    # Sender only covers @method — verifier insists on @target-uri.
    sign_request(req, keypair=keypair, covered_components=("@method",))
    with pytest.raises(HttpSignatureVerificationError) as exc:
        verify_request(
            req,
            key_resolver=resolver,
            required_components={"@method", "@target-uri"},
        )
    assert "@target-uri" in str(exc.value)


def test_verify_rejects_when_no_signature(resolver):
    req = _prep_get()
    with pytest.raises(HttpSignatureVerificationError):
        verify_request(req, key_resolver=resolver)


# -- Content-digest helpers --------------------------------------------------


def test_compute_content_digest_matches_rfc9530_format():
    body = b'{"hello":"world"}'
    digest = compute_content_digest(body)
    raw = hashlib.sha256(body).digest()
    expected = f"sha-256=:{base64.b64encode(raw).decode('ascii')}:"
    assert digest == expected


def test_attach_content_digest_sets_header_for_bytes_body():
    req = _prep_post()
    expected = compute_content_digest(req.body)  # bytes
    digest = attach_content_digest(req)
    assert digest == expected
    assert req.headers["Content-Digest"] == expected


def test_attach_content_digest_handles_str_body():
    req = requests.Request("POST", "https://api.example.com/", data="hello").prepare()
    digest = attach_content_digest(req)
    assert digest == compute_content_digest(b"hello")


def test_attach_content_digest_handles_none_body():
    req = _prep_get()
    digest = attach_content_digest(req)
    assert digest == compute_content_digest(b"")


def test_attach_content_digest_is_idempotent():
    req = _prep_post()
    first = attach_content_digest(req)
    second = attach_content_digest(req)
    assert first == second


# -- Resolver ----------------------------------------------------------------


def test_resolver_round_trips_register_keypair():
    kp = generate_keypair()
    resolver = PinkyKeyResolver()
    kid = resolver.register_keypair(kp)
    assert resolver.signing_keys[kid] is kp
    assert resolver.public_keys[kid] == kp.public


def test_resolver_register_public_key_only():
    kp = generate_keypair()
    resolver = PinkyKeyResolver()
    kid = resolver.register_public_key(kp.public)
    assert kid in resolver.public_keys
    assert kid not in resolver.signing_keys


def test_resolver_unknown_kid_raises_clean_error():
    resolver = PinkyKeyResolver()
    with pytest.raises(HttpSignatureVerificationError):
        resolver.resolve_public_key("unknown")
    with pytest.raises(HttpSignatureVerificationError):
        resolver.resolve_private_key("unknown")


# -- Signature-Input shape ---------------------------------------------------


def test_signature_input_includes_keyid_alg_created_nonce_tag(keypair, resolver):
    req = _prep_get()
    sign_request(req, keypair=keypair)
    sig_input = req.headers["Signature-Input"]
    assert "keyid=" in sig_input
    assert 'alg="ed25519"' in sig_input
    assert "created=" in sig_input
    assert "nonce=" in sig_input
    assert f'tag="{DEFAULT_TAG}"' in sig_input


def test_explicit_expires_appears_in_signature_input(keypair, resolver):
    req = _prep_get()
    sign_request(req, keypair=keypair, expires_in=timedelta(seconds=120))
    sig_input = req.headers["Signature-Input"]
    assert "expires=" in sig_input
    result = verify_request(req, key_resolver=resolver)
    assert "expires" in result.parameters


def test_strip_component_params_helper():
    assert _strip_component_params('"@method"') == "@method"
    assert _strip_component_params('"content-type"') == "content-type"
    assert _strip_component_params('"@query-param";name="foo"') == "@query-param"
