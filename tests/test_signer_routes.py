"""Tests for pinky_daemon.signer_routes (POST /internal/signer/sign).

Drives the router via FastAPI TestClient with a real BearerTokenStore +
a real DaemonSigner. The DaemonSigner is constructed against an
in-memory _StubRegistry (same pattern as
tests/test_pinky_identity_signer.py) plus a real EncryptedSignerStore on
a tmp path. This exercises the full sign path end-to-end without
needing PR-3's registry impl.
"""

from __future__ import annotations

import base64
import importlib
import secrets
from dataclasses import dataclass
from datetime import timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pinky_identity import (
    DEFAULT_TAG,
    DEVICE_KEY_BYTES,
    KEY_ACTIVE,
    PURPOSE_SERVICE_AUTH,
    BearerTokenStore,
    DaemonSigner,
    DeviceKey,
    EncryptedSignerStore,
    PinkyKeyResolver,
    fingerprint,
    generate_keypair,
    kid_from_public_key,
    verify_request,
)
from pinky_identity.signer import DEFAULT_FLEET

# -- Stub registry (mirrors test_pinky_identity_signer.py) -------------------


@dataclass
class _StubKeyRecord:
    kid: str
    fingerprint: str
    status: str
    public_key: bytes


class _StubRegistry:
    def __init__(self) -> None:
        self._rows: dict[str, _StubKeyRecord] = {}
        self._active: dict[tuple[str, str, str], str] = {}

    def add(
        self,
        keypair,
        *,
        agent_name: str,
        fleet: str = DEFAULT_FLEET,
        purpose: str = PURPOSE_SERVICE_AUTH,
        status: str = KEY_ACTIVE,
    ) -> str:
        kid = kid_from_public_key(keypair.public)
        row = _StubKeyRecord(
            kid=kid,
            fingerprint=fingerprint(keypair.public),
            status=status,
            public_key=keypair.public.raw,
        )
        self._rows[kid] = row
        if status == KEY_ACTIVE:
            self._active[(fleet, agent_name, purpose)] = kid
        return kid

    def set_status(self, kid: str, status: str) -> None:
        self._rows[kid].status = status
        if status != KEY_ACTIVE:
            stale = [k for k, v in self._active.items() if v == kid]
            for k in stale:
                del self._active[k]

    def get_key(self, kid: str):
        return self._rows.get(kid)

    def get_active_key(self, fleet: str, agent_name: str, purpose: str):
        kid = self._active.get((fleet, agent_name, purpose))
        if kid is None:
            return None
        return self._rows[kid]


# -- Fixtures ----------------------------------------------------------------


@pytest.fixture
def device_key():
    return DeviceKey.from_bytes(secrets.token_bytes(DEVICE_KEY_BYTES))


@pytest.fixture
def store(tmp_path, device_key):
    s = EncryptedSignerStore(db_path=tmp_path / "signer.db", device_key=device_key)
    try:
        yield s
    finally:
        s.close()


@pytest.fixture
def bearer_store(tmp_path):
    s = BearerTokenStore(db_path=tmp_path / "bearer.db")
    try:
        yield s
    finally:
        s.close()


@pytest.fixture
def registry():
    return _StubRegistry()


@pytest.fixture
def signer(registry, store):
    return DaemonSigner(registry=registry, signer_store=store)


@pytest.fixture
def signer_routes_module():
    """Fresh-import the router module per test so module-level state
    (set_dependencies) doesn't bleed between cases."""
    import pinky_daemon.signer_routes as mod

    importlib.reload(mod)
    return mod


@pytest.fixture
def client(signer_routes_module, bearer_store, signer):
    signer_routes_module.set_dependencies(
        bearer_store=bearer_store, signer=signer
    )
    app = FastAPI()
    app.include_router(signer_routes_module.router)
    return TestClient(app)


# -- Helpers -----------------------------------------------------------------


def _mint_bearer(bearer_store, agent_name="alpha"):
    """Mint a bearer for the default identity tuple."""
    return bearer_store.mint(
        session_id="sess-1",
        agent_name=agent_name,
        fleet=DEFAULT_FLEET,
        purpose=PURPOSE_SERVICE_AUTH,
    )


def _enroll_agent(registry, store, *, agent_name="alpha"):
    """Add an active key for *agent_name* to both registry and store.
    Returns the keypair so tests can verify."""
    kp = generate_keypair()
    registry.add(kp, agent_name=agent_name)
    store.put_keypair(kp)
    return kp


def _sign_payload(
    *,
    method="GET",
    url="https://api.example.com/v1/things",
    headers=None,
    body=b"",
    covered_components=None,
):
    payload: dict = {
        "method": method,
        "url": url,
        "headers": headers or {},
        "body_b64": base64.b64encode(body).decode("ascii") if body else "",
    }
    if covered_components is not None:
        payload["covered_components"] = covered_components
    return payload


# -- Construction / wiring ---------------------------------------------------


def test_set_dependencies_rejects_wrong_types(signer_routes_module, signer):
    with pytest.raises(TypeError):
        signer_routes_module.set_dependencies(
            bearer_store={"not": "a store"}, signer=signer
        )


def test_set_dependencies_rejects_wrong_signer(
    signer_routes_module, bearer_store
):
    with pytest.raises(TypeError):
        signer_routes_module.set_dependencies(
            bearer_store=bearer_store, signer="not-a-signer"
        )


def test_sign_returns_503_when_bearer_store_unconfigured(signer_routes_module):
    # Module loaded but set_dependencies never called → 503.
    app = FastAPI()
    app.include_router(signer_routes_module.router)
    c = TestClient(app)
    r = c.post(
        "/internal/signer/sign",
        json=_sign_payload(),
        headers={"Authorization": "Bearer anything"},
    )
    assert r.status_code == 503
    assert "bearer_store" in r.json()["detail"]


def test_sign_returns_503_when_signer_unconfigured_but_bearer_present(
    signer_routes_module, bearer_store
):
    """Bearer store is wired but DaemonSigner isn't — first the bearer
    validates, then the missing signer triggers 503. Defensive: the
    daemon may wire them in stages."""
    # Manual half-wire: bearer set, signer left None.
    signer_routes_module._bearer_store = bearer_store
    signer_routes_module._signer = None
    app = FastAPI()
    app.include_router(signer_routes_module.router)
    c = TestClient(app)
    mint = _mint_bearer(bearer_store)
    r = c.post(
        "/internal/signer/sign",
        json=_sign_payload(),
        headers={"Authorization": f"Bearer {mint.token}"},
    )
    assert r.status_code == 503
    assert "DaemonSigner" in r.json()["detail"]


# -- Bearer auth -------------------------------------------------------------


def test_sign_requires_authorization_header(client):
    r = client.post("/internal/signer/sign", json=_sign_payload())
    assert r.status_code == 401
    assert r.headers.get("www-authenticate", "").lower().startswith("bearer")


def test_sign_requires_bearer_scheme(client):
    r = client.post(
        "/internal/signer/sign",
        json=_sign_payload(),
        headers={"Authorization": "Basic abcd"},
    )
    assert r.status_code == 401
    assert "Bearer" in r.json()["detail"]


def test_sign_rejects_empty_bearer(client):
    r = client.post(
        "/internal/signer/sign",
        json=_sign_payload(),
        headers={"Authorization": "Bearer "},
    )
    assert r.status_code == 401


def test_sign_rejects_unknown_bearer(client):
    r = client.post(
        "/internal/signer/sign",
        json=_sign_payload(),
        headers={"Authorization": "Bearer not-a-real-token-blah-blah"},
    )
    assert r.status_code == 401
    assert "invalid" in r.json()["detail"]


def test_sign_rejects_revoked_bearer(client, bearer_store, registry, store):
    _enroll_agent(registry, store)
    mint = _mint_bearer(bearer_store)
    bearer_store.invalidate(mint.token_id)
    r = client.post(
        "/internal/signer/sign",
        json=_sign_payload(),
        headers={"Authorization": f"Bearer {mint.token}"},
    )
    assert r.status_code == 401
    assert "revoked" in r.json()["detail"]


def test_sign_rejects_expired_bearer(
    signer_routes_module, bearer_store, registry, store, signer
):
    """Mint a token that expires in 1 second, then validate the
    endpoint refuses it. We rely on real time here rather than
    monkeypatching now() because the FastAPI dependency chain doesn't
    plumb time.now through."""
    import time as _time

    _enroll_agent(registry, store)
    # Override mint to use short TTL.
    mint = bearer_store.mint(
        session_id="sess-1",
        agent_name="alpha",
        fleet=DEFAULT_FLEET,
        purpose=PURPOSE_SERVICE_AUTH,
        ttl=timedelta(seconds=1),
    )
    signer_routes_module.set_dependencies(
        bearer_store=bearer_store, signer=signer
    )
    app = FastAPI()
    app.include_router(signer_routes_module.router)
    c = TestClient(app)
    _time.sleep(1.1)
    r = c.post(
        "/internal/signer/sign",
        json=_sign_payload(),
        headers={"Authorization": f"Bearer {mint.token}"},
    )
    assert r.status_code == 401
    assert "expired" in r.json()["detail"]


# -- Happy path --------------------------------------------------------------


def test_sign_get_round_trips_against_verifier(
    client, bearer_store, registry, store
):
    kp = _enroll_agent(registry, store)
    mint = _mint_bearer(bearer_store)

    r = client.post(
        "/internal/signer/sign",
        json=_sign_payload(),
        headers={"Authorization": f"Bearer {mint.token}"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["kid"] == kid_from_public_key(kp.public)
    # Default GET → no Content-Digest header.
    assert "Signature" in body["headers"]
    assert "Signature-Input" in body["headers"]
    assert "Content-Digest" not in body["headers"]

    # Reconstruct the would-be outbound request and verify.
    import requests as _req

    req = _req.Request("GET", "https://api.example.com/v1/things").prepare()
    for k, v in body["headers"].items():
        req.headers[k] = v
    resolver = PinkyKeyResolver.from_signing_keypairs([kp])
    result = verify_request(req, key_resolver=resolver)
    assert result.parameters["keyid"] == body["kid"]
    assert result.parameters["tag"] == DEFAULT_TAG


def test_sign_post_with_content_digest_round_trips(
    client, bearer_store, registry, store
):
    """POST with content-digest covered: signer auto-attaches the
    digest header, returns it alongside Signature/-Input, and the
    default verifier accepts the reassembled request."""
    kp = _enroll_agent(registry, store)
    mint = _mint_bearer(bearer_store)

    payload_body = b'{"hello":"world"}'
    payload = _sign_payload(
        method="POST",
        headers={"Content-Type": "application/json"},
        body=payload_body,
        covered_components=[
            "@method",
            "@target-uri",
            "@authority",
            "content-digest",
        ],
    )
    r = client.post(
        "/internal/signer/sign",
        json=payload,
        headers={"Authorization": f"Bearer {mint.token}"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert "Content-Digest" in body["headers"]

    # Reassemble + verify end-to-end.
    import requests as _req

    req = _req.Request(
        "POST",
        "https://api.example.com/v1/things",
        data=payload_body,
        headers={"Content-Type": "application/json"},
    ).prepare()
    for k, v in body["headers"].items():
        req.headers[k] = v
    resolver = PinkyKeyResolver.from_signing_keypairs([kp])
    verify_request(req, key_resolver=resolver)


def test_sign_response_only_contains_signer_attached_headers(
    client, bearer_store, registry, store
):
    """The endpoint must not echo back caller-supplied headers — just
    the signer's auth-header delta. Saves bandwidth and avoids
    accidentally surfacing sensitive headers (Authorization, cookies)."""
    _enroll_agent(registry, store)
    mint = _mint_bearer(bearer_store)

    payload = _sign_payload(
        headers={
            "Authorization": "Bearer dont-echo-me",
            "Cookie": "session=secret",
            "Accept": "*/*",
        }
    )
    r = client.post(
        "/internal/signer/sign",
        json=payload,
        headers={"Authorization": f"Bearer {mint.token}"},
    )
    assert r.status_code == 200
    keys = set(r.json()["headers"].keys())
    assert keys <= {"Signature", "Signature-Input", "Content-Digest"}


def test_sign_uses_bearer_identity_tuple(
    client, bearer_store, registry, store
):
    """Two agents enrolled; bearer is bound to one. The endpoint must
    sign as the bearer's agent, not the caller-supplied one (which
    isn't in the payload at all — that's the point)."""
    alpha = _enroll_agent(registry, store, agent_name="alpha")
    beta = _enroll_agent(registry, store, agent_name="beta")
    mint = bearer_store.mint(
        session_id="sess-1",
        agent_name="beta",
        fleet=DEFAULT_FLEET,
        purpose=PURPOSE_SERVICE_AUTH,
    )
    r = client.post(
        "/internal/signer/sign",
        json=_sign_payload(),
        headers={"Authorization": f"Bearer {mint.token}"},
    )
    assert r.status_code == 200
    assert r.json()["kid"] == kid_from_public_key(beta.public)
    assert r.json()["kid"] != kid_from_public_key(alpha.public)


# -- Signer error mapping ----------------------------------------------------


def test_sign_returns_404_when_agent_not_enrolled(
    client, bearer_store
):
    """Bearer claims an identity that's never been enrolled in the
    registry → KeyNotRegisteredError → 404."""
    mint = _mint_bearer(bearer_store, agent_name="ghost")
    r = client.post(
        "/internal/signer/sign",
        json=_sign_payload(),
        headers={"Authorization": f"Bearer {mint.token}"},
    )
    assert r.status_code == 404
    assert "ghost" in r.json()["detail"]


def test_sign_returns_404_when_active_key_revoked_post_mint(
    client, bearer_store, registry, store
):
    """Bearer outlives the key — at mint time the key was active; by
    the time the endpoint is called it's been revoked. With
    ``sign_request_for_agent`` the registry's ``get_active_key``
    returns None for non-active rows, surfacing as
    :class:`KeyNotRegisteredError` (no *active* key) → 404.

    The 409/``KeyNotActiveError`` mapping in the endpoint stays as
    defense-in-depth — it would fire if a future endpoint ever signs
    by an explicit kid that's gone non-active. Through the
    for-agent path, 404 is the observable outcome."""
    kp = _enroll_agent(registry, store)
    kid = kid_from_public_key(kp.public)
    mint = _mint_bearer(bearer_store)
    registry.set_status(kid, "revoked")
    r = client.post(
        "/internal/signer/sign",
        json=_sign_payload(),
        headers={"Authorization": f"Bearer {mint.token}"},
    )
    assert r.status_code == 404
    assert "alpha" in r.json()["detail"]


def test_sign_returns_400_for_body_without_content_digest(
    client, bearer_store, registry, store
):
    """Default covered components omit content-digest. A POST with a
    body must be rejected (BodyNotBoundError → 400). Same secure-by-
    default we test on the signer side, exercised through the wire."""
    _enroll_agent(registry, store)
    mint = _mint_bearer(bearer_store)
    payload = _sign_payload(
        method="POST",
        headers={"Content-Type": "application/json"},
        body=b'{"x":1}',
        # Default covered_components — no content-digest.
    )
    r = client.post(
        "/internal/signer/sign",
        json=payload,
        headers={"Authorization": f"Bearer {mint.token}"},
    )
    assert r.status_code == 400
    assert "content-digest" in r.json()["detail"].lower()


def test_sign_400_on_malformed_body_b64(client, bearer_store, registry, store):
    _enroll_agent(registry, store)
    mint = _mint_bearer(bearer_store)
    payload = _sign_payload()
    payload["body_b64"] = "this is not base64!@#"
    r = client.post(
        "/internal/signer/sign",
        json=payload,
        headers={"Authorization": f"Bearer {mint.token}"},
    )
    assert r.status_code == 400
    assert "base64" in r.json()["detail"]


# -- Payload validation ------------------------------------------------------


def test_sign_rejects_empty_method(client, bearer_store, registry, store):
    _enroll_agent(registry, store)
    mint = _mint_bearer(bearer_store)
    payload = _sign_payload(method="")
    r = client.post(
        "/internal/signer/sign",
        json=payload,
        headers={"Authorization": f"Bearer {mint.token}"},
    )
    assert r.status_code == 422  # pydantic field validation


def test_sign_rejects_excessive_expires_in_seconds(
    client, bearer_store, registry, store
):
    _enroll_agent(registry, store)
    mint = _mint_bearer(bearer_store)
    payload = _sign_payload()
    payload["expires_in_seconds"] = 999999  # exceeds 86400 cap
    r = client.post(
        "/internal/signer/sign",
        json=payload,
        headers={"Authorization": f"Bearer {mint.token}"},
    )
    assert r.status_code == 422


def test_sign_accepts_custom_tag(client, bearer_store, registry, store):
    """The tag knob propagates so callers can mint per-purpose tags
    that the verifier pins. Default is pinky-service-auth-v1."""
    _enroll_agent(registry, store)
    mint = _mint_bearer(bearer_store)
    payload = _sign_payload()
    payload["tag"] = "custom-tag-v2"
    r = client.post(
        "/internal/signer/sign",
        json=payload,
        headers={"Authorization": f"Bearer {mint.token}"},
    )
    assert r.status_code == 200
    assert 'tag="custom-tag-v2"' in r.json()["headers"]["Signature-Input"]


def test_sign_bumps_last_used_at_on_success(
    client, bearer_store, registry, store
):
    """End-to-end: a successful sign should bump the bearer's
    last_used_at via the validate() touch — useful for audit."""
    _enroll_agent(registry, store)
    mint = _mint_bearer(bearer_store)
    assert mint.claims.last_used_at is None
    client.post(
        "/internal/signer/sign",
        json=_sign_payload(),
        headers={"Authorization": f"Bearer {mint.token}"},
    )
    after = bearer_store.get_claims(mint.token_id)
    assert after.last_used_at is not None
