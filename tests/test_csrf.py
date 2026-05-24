"""Tests for the CSRF middleware (#443)."""

from __future__ import annotations

import os
import tempfile

import pytest
from fastapi.testclient import TestClient

from pinky_daemon.auth import (
    INTERNAL_AGENT_HEADER,
    INTERNAL_SIGNATURE_HEADER,
    INTERNAL_TIMESTAMP_HEADER,
    SESSION_COOKIE_NAME,
)
from pinky_daemon.config import DeploymentMode
from pinky_daemon.middleware.csrf import (
    CSRF_COOKIE_NAME,
    CSRF_HEADER_NAME,
    UNSAFE_METHODS,
    build_csrf_middleware,
    generate_csrf_token,
    is_exempt_path,
)

# ── Pure helpers ──────────────────────────────────────────────


class TestPureHelpers:
    def test_unsafe_methods_set(self):
        assert UNSAFE_METHODS == frozenset({"POST", "PUT", "PATCH", "DELETE"})

    def test_generate_token_is_random(self):
        a = generate_csrf_token()
        b = generate_csrf_token()
        assert a != b
        assert len(a) >= 32

    def test_is_exempt_path(self):
        assert is_exempt_path("/triggers/webhook/abc")
        assert is_exempt_path("/twilio/voice")
        assert not is_exempt_path("/admin/update")
        assert not is_exempt_path("/auth/login")


# ── Middleware closure ────────────────────────────────────────


class _FakeApp:
    def __init__(self, mode, sec_audit=None):
        self.state = type("State", (), {})()
        self.state.deployment_mode = mode
        self.state.trusted_proxies = ()
        if sec_audit is not None:
            self.state.security_audit = sec_audit


class _FakeRequest:
    def __init__(
        self,
        method="POST",
        path="/admin/update",
        cookies=None,
        headers=None,
        peer="1.2.3.4",
        app=None,
    ):
        self.method = method
        self.url = type("U", (), {"path": path})()
        self.cookies = cookies or {}
        self.headers = headers or {}
        self.client = type("C", (), {"host": peer})()
        self.app = app


class _CapturingResponse:
    """Captures set_cookie calls for assertion."""

    def __init__(self, status_code=200):
        self.status_code = status_code
        self.headers: dict[str, str] = {}
        self.cookies_set: list[dict] = []

    def set_cookie(self, **kwargs):
        self.cookies_set.append(kwargs)


async def _ok(_req):
    return _CapturingResponse()


class TestMiddlewareEnforcement:
    @pytest.mark.asyncio
    async def test_trusted_mode_passthrough(self):
        mw = build_csrf_middleware()
        app = _FakeApp(DeploymentMode.TRUSTED)
        # Even with a bad token, trusted is a no-op.
        req = _FakeRequest(
            method="POST",
            path="/admin/update",
            cookies={SESSION_COOKIE_NAME: "abc", CSRF_COOKIE_NAME: "x"},
            headers={CSRF_HEADER_NAME: "y"},
            app=app,
        )
        resp = await mw(req, _ok)
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_safe_methods_pass(self):
        mw = build_csrf_middleware()
        app = _FakeApp(DeploymentMode.WEB)
        for method in ("GET", "HEAD", "OPTIONS"):
            req = _FakeRequest(
                method=method,
                path="/admin/update",
                cookies={SESSION_COOKIE_NAME: "abc"},
                app=app,
            )
            resp = await mw(req, _ok)
            assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_unsafe_post_without_token_rejected(self):
        mw = build_csrf_middleware()
        app = _FakeApp(DeploymentMode.WEB)
        req = _FakeRequest(
            method="POST",
            path="/admin/update",
            cookies={SESSION_COOKIE_NAME: "abc"},
            app=app,
        )
        resp = await mw(req, _ok)
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_unsafe_post_with_mismatch_rejected(self):
        mw = build_csrf_middleware()
        app = _FakeApp(DeploymentMode.WEB)
        req = _FakeRequest(
            method="POST",
            path="/admin/update",
            cookies={SESSION_COOKIE_NAME: "abc", CSRF_COOKIE_NAME: "tok-a"},
            headers={CSRF_HEADER_NAME: "tok-b"},
            app=app,
        )
        resp = await mw(req, _ok)
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_unsafe_post_with_match_passes(self):
        mw = build_csrf_middleware()
        app = _FakeApp(DeploymentMode.WEB)
        req = _FakeRequest(
            method="POST",
            path="/admin/update",
            cookies={SESSION_COOKIE_NAME: "abc", CSRF_COOKIE_NAME: "tok-a"},
            headers={CSRF_HEADER_NAME: "tok-a"},
            app=app,
        )
        resp = await mw(req, _ok)
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_bearer_auth_exempts(self):
        mw = build_csrf_middleware()
        app = _FakeApp(DeploymentMode.WEB)
        req = _FakeRequest(
            method="POST",
            path="/admin/update",
            cookies={SESSION_COOKIE_NAME: "abc"},  # has session cookie
            headers={"authorization": "Bearer abc.def"},
            app=app,
        )
        resp = await mw(req, _ok)
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_internal_signed_exempts(self):
        mw = build_csrf_middleware()
        app = _FakeApp(DeploymentMode.WEB)
        req = _FakeRequest(
            method="POST",
            path="/admin/update",
            cookies={SESSION_COOKIE_NAME: "abc"},
            headers={
                INTERNAL_AGENT_HEADER: "barsik",
                INTERNAL_TIMESTAMP_HEADER: "1234",
                INTERNAL_SIGNATURE_HEADER: "sig",
            },
            app=app,
        )
        resp = await mw(req, _ok)
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_webhook_path_exempts(self):
        mw = build_csrf_middleware()
        app = _FakeApp(DeploymentMode.WEB)
        req = _FakeRequest(
            method="POST",
            path="/triggers/webhook/abc123",
            cookies={SESSION_COOKIE_NAME: "abc"},
            app=app,
        )
        resp = await mw(req, _ok)
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_twilio_path_exempts(self):
        mw = build_csrf_middleware()
        app = _FakeApp(DeploymentMode.WEB)
        req = _FakeRequest(
            method="POST",
            path="/twilio/voice",
            cookies={SESSION_COOKIE_NAME: "abc"},
            app=app,
        )
        resp = await mw(req, _ok)
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_no_session_no_enforcement(self):
        # Anonymous request can do anything; CSRF protects ambient
        # credentials, and there are none here.
        mw = build_csrf_middleware()
        app = _FakeApp(DeploymentMode.WEB)
        req = _FakeRequest(
            method="POST", path="/auth/login", cookies={}, app=app
        )
        resp = await mw(req, _ok)
        assert resp.status_code == 200


# ── Token issuance ────────────────────────────────────────────


class TestTokenIssuance:
    @pytest.mark.asyncio
    async def test_session_without_csrf_cookie_gets_one_set(self):
        mw = build_csrf_middleware()
        app = _FakeApp(DeploymentMode.WEB)
        req = _FakeRequest(
            method="GET",
            path="/api",
            cookies={SESSION_COOKIE_NAME: "abc"},
            app=app,
        )
        resp = await mw(req, _ok)
        assert resp.status_code == 200
        # Cookie issued.
        assert any(
            c.get("key") == CSRF_COOKIE_NAME for c in resp.cookies_set
        )
        # SameSite=Strict + httponly=False so SPA can read it.
        cookie = next(
            c for c in resp.cookies_set if c.get("key") == CSRF_COOKIE_NAME
        )
        assert cookie["samesite"] == "strict"
        assert cookie["httponly"] is False

    @pytest.mark.asyncio
    async def test_session_with_csrf_cookie_does_not_reissue(self):
        mw = build_csrf_middleware()
        app = _FakeApp(DeploymentMode.WEB)
        req = _FakeRequest(
            method="GET",
            path="/api",
            cookies={SESSION_COOKIE_NAME: "abc", CSRF_COOKIE_NAME: "tok"},
            app=app,
        )
        resp = await mw(req, _ok)
        assert not any(
            c.get("key") == CSRF_COOKIE_NAME for c in resp.cookies_set
        )

    @pytest.mark.asyncio
    async def test_no_session_no_token_issued(self):
        # Anonymous traffic doesn't get a CSRF cookie until they have a
        # session to bind it to.
        mw = build_csrf_middleware()
        app = _FakeApp(DeploymentMode.WEB)
        req = _FakeRequest(
            method="GET", path="/api", cookies={}, app=app
        )
        resp = await mw(req, _ok)
        assert not any(
            c.get("key") == CSRF_COOKIE_NAME for c in resp.cookies_set
        )

    @pytest.mark.asyncio
    async def test_secure_flag_only_in_web_mode(self):
        mw = build_csrf_middleware()
        for mode, expected_secure in (
            (DeploymentMode.LAN, False),
            (DeploymentMode.WEB, True),
        ):
            app = _FakeApp(mode)
            req = _FakeRequest(
                method="GET",
                path="/api",
                cookies={SESSION_COOKIE_NAME: "abc"},
                app=app,
            )
            resp = await mw(req, _ok)
            cookie = next(
                c for c in resp.cookies_set if c.get("key") == CSRF_COOKIE_NAME
            )
            assert cookie["secure"] is expected_secure


# ── Audit emission ────────────────────────────────────────────


class TestAuditEmission:
    @pytest.mark.asyncio
    async def test_reject_emits_audit(self):
        from pinky_daemon.security_audit import SecurityAuditStore

        with tempfile.TemporaryDirectory() as tmpdir:
            sec = SecurityAuditStore(db_path=os.path.join(tmpdir, "x.db"))
            mw = build_csrf_middleware()
            app = _FakeApp(DeploymentMode.WEB, sec_audit=sec)
            req = _FakeRequest(
                method="POST",
                path="/admin/update",
                cookies={SESSION_COOKIE_NAME: "abc"},
                app=app,
            )
            resp = await mw(req, _ok)
            assert resp.status_code == 403
            rows = sec.query(event_type="auth.csrf.rejected")
            assert len(rows) == 1
            assert rows[0].outcome == "denied"
            assert rows[0].method == "POST"
            assert rows[0].target == "/admin/update"
            assert rows[0].detail == {"reason": "missing"}

    @pytest.mark.asyncio
    async def test_mismatch_audit_reason(self):
        from pinky_daemon.security_audit import SecurityAuditStore

        with tempfile.TemporaryDirectory() as tmpdir:
            sec = SecurityAuditStore(db_path=os.path.join(tmpdir, "x.db"))
            mw = build_csrf_middleware()
            app = _FakeApp(DeploymentMode.WEB, sec_audit=sec)
            req = _FakeRequest(
                method="POST",
                path="/admin/update",
                cookies={
                    SESSION_COOKIE_NAME: "abc",
                    CSRF_COOKIE_NAME: "tok-a",
                },
                headers={CSRF_HEADER_NAME: "tok-b"},
                app=app,
            )
            resp = await mw(req, _ok)
            assert resp.status_code == 403
            rows = sec.query(event_type="auth.csrf.rejected")
            assert rows[0].detail == {"reason": "mismatch"}


# ── End-to-end through create_api ─────────────────────────────


@pytest.fixture
def tmp_db_path():
    with tempfile.TemporaryDirectory() as tmpdir:
        yield os.path.join(tmpdir, "test.db")


class TestCreateApiCsrf:
    def test_trusted_mode_no_csrf_check(self, tmp_db_path):
        from pinky_daemon.api import create_api

        app = create_api(
            db_path=tmp_db_path, deployment_mode=DeploymentMode.TRUSTED
        )
        client = TestClient(app)
        # /api is GET; trusted mode shouldn't even issue a token.
        resp = client.get("/api")
        assert resp.status_code == 200
        assert CSRF_COOKIE_NAME not in resp.cookies
