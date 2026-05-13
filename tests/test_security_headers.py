"""Tests for the mode-aware security headers middleware (#437).

Covers:

* The static header matrix per mode (`headers_for_mode`).
* CSP construction per mode (`build_csp`).
* HSTS verification logic (`is_verified_https`).
* The ASGI middleware closure: header presence, CSP report-only vs.
  enforcing, HSTS only emitted when verified, websocket upgrades pass
  through untouched.
* End-to-end through `create_api` for trusted / lan / web.
"""

from __future__ import annotations

import ipaddress
import os
import tempfile
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from pinky_daemon.config import DeploymentMode
from pinky_daemon.middleware.security_headers import (
    CSP_ENFORCING_ENV,
    build_csp,
    build_security_headers_middleware,
    headers_for_mode,
    is_verified_https,
)

# ── Static header matrix ──────────────────────────────────────


class TestHeadersForMode:
    def test_trusted_minimal(self):
        h = headers_for_mode(DeploymentMode.TRUSTED)
        assert h["X-Content-Type-Options"] == "nosniff"
        assert h["X-XSS-Protection"] == "0"
        # Trusted does NOT add the lan/web set.
        assert "X-Frame-Options" not in h
        assert "Referrer-Policy" not in h
        assert "Permissions-Policy" not in h

    def test_lan_adds_clickjacking_referrer_perms(self):
        h = headers_for_mode(DeploymentMode.LAN)
        assert h["X-Content-Type-Options"] == "nosniff"
        assert h["X-XSS-Protection"] == "0"
        assert h["X-Frame-Options"] == "DENY"
        assert h["Referrer-Policy"] == "strict-origin-when-cross-origin"
        assert "Permissions-Policy" in h
        # Basic policy: a few things off.
        assert "camera=()" in h["Permissions-Policy"]

    def test_web_uses_strict_permissions_policy(self):
        h = headers_for_mode(DeploymentMode.WEB)
        # Strict policy lists more sinks.
        assert "interest-cohort=()" in h["Permissions-Policy"]
        assert "payment=()" in h["Permissions-Policy"]
        # Web still has the lan-set headers.
        assert h["X-Frame-Options"] == "DENY"

    def test_x_xss_protection_disabled_in_all_modes(self):
        # Modern recommendation: disable the legacy auditor explicitly.
        for mode in DeploymentMode:
            assert headers_for_mode(mode)["X-XSS-Protection"] == "0"


# ── CSP ────────────────────────────────────────────────────────


class TestBuildCsp:
    def test_lan_csp_has_required_directives(self):
        csp = build_csp(DeploymentMode.LAN)
        for directive in (
            "default-src 'self'",
            "script-src 'self'",
            "style-src 'self' 'unsafe-inline'",  # known compromise per CLAUDE.md
            "img-src 'self' data:",
            "connect-src 'self' ws: wss:",
            "frame-ancestors 'none'",
            "base-uri 'self'",
            "form-action 'self'",
        ):
            assert directive in csp

    def test_web_csp_adds_object_src_and_upgrade(self):
        csp = build_csp(DeploymentMode.WEB)
        assert "object-src 'none'" in csp
        assert "upgrade-insecure-requests" in csp

    def test_lan_csp_does_not_add_web_only_directives(self):
        csp = build_csp(DeploymentMode.LAN)
        assert "object-src 'none'" not in csp
        assert "upgrade-insecure-requests" not in csp


# ── HSTS verification ──────────────────────────────────────────


class _FakeApp:
    def __init__(self, mode, trusted=()):
        self.state = type("State", (), {})()
        self.state.deployment_mode = mode
        self.state.trusted_proxies = trusted


class _FakeRequest:
    def __init__(
        self,
        peer="127.0.0.1",
        scheme="http",
        headers=None,
        app=None,
    ):
        self.client = type("C", (), {"host": peer})()
        self.url = type("U", (), {"scheme": scheme})()
        self.headers = headers or {}
        self.app = app or _FakeApp(DeploymentMode.WEB)


class TestIsVerifiedHttps:
    def test_direct_tls_is_verified(self):
        req = _FakeRequest(scheme="https", peer="127.0.0.1")
        assert is_verified_https(req) is True

    def test_xforwarded_proto_https_with_trusted_peer_is_verified(self):
        trusted = (ipaddress.ip_network("127.0.0.1/32"),)
        app = _FakeApp(DeploymentMode.WEB, trusted=trusted)
        req = _FakeRequest(
            peer="127.0.0.1",
            headers={"x-forwarded-proto": "https"},
            app=app,
        )
        assert is_verified_https(req) is True

    def test_xforwarded_proto_https_with_untrusted_peer_rejected(self):
        # Direct attacker on the open internet sets the header.
        # MUST NOT poison HSTS.
        app = _FakeApp(DeploymentMode.WEB, trusted=())
        req = _FakeRequest(
            peer="203.0.113.7",
            headers={"x-forwarded-proto": "https"},
            app=app,
        )
        assert is_verified_https(req) is False

    def test_xforwarded_proto_http_returns_false(self):
        trusted = (ipaddress.ip_network("127.0.0.1/32"),)
        app = _FakeApp(DeploymentMode.WEB, trusted=trusted)
        req = _FakeRequest(
            peer="127.0.0.1",
            headers={"x-forwarded-proto": "http"},
            app=app,
        )
        assert is_verified_https(req) is False

    def test_no_proto_header_no_tls_returns_false(self):
        req = _FakeRequest(peer="127.0.0.1")
        assert is_verified_https(req) is False


# ── Middleware closure ────────────────────────────────────────


async def _make_response(_req):
    from fastapi.responses import JSONResponse

    return JSONResponse({"ok": True})


class TestMiddlewareClosure:
    @pytest.mark.asyncio
    async def test_trusted_mode_minimal_headers(self):
        mw = build_security_headers_middleware(csp_enforcing=False)
        req = _FakeRequest(app=_FakeApp(DeploymentMode.TRUSTED))
        resp = await mw(req, _make_response)
        assert resp.headers["X-Content-Type-Options"] == "nosniff"
        assert resp.headers["X-XSS-Protection"] == "0"
        assert "X-Frame-Options" not in resp.headers
        assert "Content-Security-Policy" not in resp.headers
        assert "Content-Security-Policy-Report-Only" not in resp.headers
        assert "Strict-Transport-Security" not in resp.headers

    @pytest.mark.asyncio
    async def test_lan_mode_emits_csp_report_only_by_default(self):
        mw = build_security_headers_middleware(csp_enforcing=False)
        req = _FakeRequest(app=_FakeApp(DeploymentMode.LAN))
        resp = await mw(req, _make_response)
        assert resp.headers["X-Frame-Options"] == "DENY"
        assert "Content-Security-Policy-Report-Only" in resp.headers
        assert "Content-Security-Policy" not in resp.headers
        # No HSTS in LAN even with verified HTTPS.
        assert "Strict-Transport-Security" not in resp.headers

    @pytest.mark.asyncio
    async def test_csp_enforcing_emits_enforcing_header(self):
        mw = build_security_headers_middleware(csp_enforcing=True)
        req = _FakeRequest(app=_FakeApp(DeploymentMode.WEB))
        resp = await mw(req, _make_response)
        assert "Content-Security-Policy" in resp.headers
        assert "Content-Security-Policy-Report-Only" not in resp.headers

    @pytest.mark.asyncio
    async def test_csp_enforcing_env_flips_to_enforcing(self):
        mw = build_security_headers_middleware()
        req = _FakeRequest(app=_FakeApp(DeploymentMode.LAN))
        with patch.dict(os.environ, {CSP_ENFORCING_ENV: "true"}, clear=False):
            resp = await mw(req, _make_response)
        assert "Content-Security-Policy" in resp.headers

    @pytest.mark.asyncio
    async def test_web_mode_no_hsts_without_verified_https(self):
        mw = build_security_headers_middleware()
        req = _FakeRequest(app=_FakeApp(DeploymentMode.WEB))  # http
        resp = await mw(req, _make_response)
        assert "Strict-Transport-Security" not in resp.headers

    @pytest.mark.asyncio
    async def test_web_mode_hsts_when_direct_tls(self):
        mw = build_security_headers_middleware()
        req = _FakeRequest(
            scheme="https", app=_FakeApp(DeploymentMode.WEB)
        )
        resp = await mw(req, _make_response)
        assert (
            resp.headers["Strict-Transport-Security"]
            == "max-age=31536000; includeSubDomains"
        )

    @pytest.mark.asyncio
    async def test_web_mode_hsts_when_trusted_proxy_says_https(self):
        mw = build_security_headers_middleware()
        trusted = (ipaddress.ip_network("127.0.0.1/32"),)
        app = _FakeApp(DeploymentMode.WEB, trusted=trusted)
        req = _FakeRequest(
            peer="127.0.0.1",
            headers={"x-forwarded-proto": "https"},
            app=app,
        )
        resp = await mw(req, _make_response)
        assert "Strict-Transport-Security" in resp.headers

    @pytest.mark.asyncio
    async def test_web_mode_no_hsts_when_untrusted_proxy_claims_https(self):
        # The HSTS poisoning vector — must remain blocked.
        mw = build_security_headers_middleware()
        app = _FakeApp(DeploymentMode.WEB, trusted=())
        req = _FakeRequest(
            peer="203.0.113.7",
            headers={"x-forwarded-proto": "https"},
            app=app,
        )
        resp = await mw(req, _make_response)
        assert "Strict-Transport-Security" not in resp.headers

    @pytest.mark.asyncio
    async def test_websocket_upgrade_passes_through_untouched(self):
        # Returning a non-Response sentinel lets us assert no header
        # mutation happened. (The middleware short-circuits before
        # call_next's return is touched.)
        async def _ws_response(_req):
            return "ws-handshake-result"

        mw = build_security_headers_middleware()
        req = _FakeRequest(
            headers={"upgrade": "websocket"},
            app=_FakeApp(DeploymentMode.WEB),
        )
        out = await mw(req, _ws_response)
        assert out == "ws-handshake-result"


# ── End-to-end through create_api ──────────────────────────────


@pytest.fixture
def tmp_db_path():
    with tempfile.TemporaryDirectory() as tmpdir:
        yield os.path.join(tmpdir, "test.db")


def _api_with_lan_bypass(tmp_db_path, mode: DeploymentMode):
    """Build a real FastAPI app for ``mode`` while letting TestClient
    through the LAN filter (TestClient's peer is 0.0.0.0)."""
    from pinky_daemon.api import create_api

    env = {**os.environ, "PINKY_LAN_EXTRA_CIDRS": "0.0.0.0/32"}
    with patch.dict(os.environ, env, clear=True):
        return create_api(db_path=tmp_db_path, deployment_mode=mode)


class TestCreateApiSecurityHeaders:
    def test_trusted_mode_minimal_headers_on_real_request(self, tmp_db_path):
        app = _api_with_lan_bypass(tmp_db_path, DeploymentMode.TRUSTED)
        client = TestClient(app)
        resp = client.get("/api")
        assert resp.headers["X-Content-Type-Options"] == "nosniff"
        assert resp.headers["X-XSS-Protection"] == "0"
        assert "X-Frame-Options" not in resp.headers
        assert "Content-Security-Policy" not in resp.headers
        assert "Content-Security-Policy-Report-Only" not in resp.headers

    def test_lan_mode_full_set_minus_hsts(self, tmp_db_path):
        app = _api_with_lan_bypass(tmp_db_path, DeploymentMode.LAN)
        client = TestClient(app)
        resp = client.get("/api")
        assert resp.headers["X-Frame-Options"] == "DENY"
        assert resp.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
        # CSP report-only by default
        assert "Content-Security-Policy-Report-Only" in resp.headers
        assert "Strict-Transport-Security" not in resp.headers

    def test_web_mode_no_hsts_over_plain_http(self, tmp_db_path):
        # TestClient uses http://, no trusted-proxy header → HSTS must NOT
        # appear, even though we're in web mode.
        app = _api_with_lan_bypass(tmp_db_path, DeploymentMode.WEB)
        client = TestClient(app)
        resp = client.get("/api")
        assert "Strict-Transport-Security" not in resp.headers
        assert "Content-Security-Policy-Report-Only" in resp.headers
