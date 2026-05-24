"""Tests for the auth/admin rate limiter (#438)."""

from __future__ import annotations

import os
import tempfile
from typing import Any
from unittest.mock import patch

import pytest

from pinky_daemon.config import DeploymentMode
from pinky_daemon.middleware.rate_limit import (
    DEFAULT_BUCKETS,
    BucketConfig,
    InMemoryRateLimiter,
    build_rate_limit_middleware,
    classify_request,
)

# ── classify_request ───────────────────────────────────────────


class TestClassifyRequest:
    def test_login_post_is_auth(self):
        assert classify_request("POST", "/auth/login") == "auth"

    def test_login_get_is_default(self):
        # The login *page* GET shouldn't be in the brute-force bucket.
        assert classify_request("GET", "/auth/login") == "default"

    def test_password_post_is_auth(self):
        assert classify_request("POST", "/auth/password") == "auth"

    def test_admin_prefix_is_admin(self):
        assert classify_request("GET", "/admin/update") == "admin"
        assert classify_request("POST", "/admin/agents") == "admin"

    def test_webhook_prefix_is_webhook(self):
        assert classify_request("POST", "/triggers/webhook/abc123") == "webhook"

    def test_other_routes_are_default(self):
        assert classify_request("GET", "/api") == "default"
        assert classify_request("GET", "/agents") == "default"

    def test_method_case_insensitive(self):
        assert classify_request("post", "/auth/login") == "auth"


# ── InMemoryRateLimiter ────────────────────────────────────────


class TestInMemoryRateLimiter:
    def test_under_limit_allowed(self):
        limiter = InMemoryRateLimiter()
        for i in range(5):
            allowed, retry = limiter.check("auth", "1.2.3.4", now=i * 1.0)
            assert allowed
            assert retry == 0.0

    def test_at_limit_rejected(self):
        limiter = InMemoryRateLimiter()
        for i in range(5):
            limiter.check("auth", "1.2.3.4", now=i * 1.0)
        # 6th attempt within window → blocked + lockout.
        allowed, retry = limiter.check("auth", "1.2.3.4", now=5.0)
        assert not allowed
        assert retry > 0

    def test_lockout_persists_for_penalty_seconds(self):
        # Auth bucket has 900s lockout after exceed.
        limiter = InMemoryRateLimiter()
        for i in range(5):
            limiter.check("auth", "1.2.3.4", now=i * 1.0)
        limiter.check("auth", "1.2.3.4", now=5.0)  # triggers lockout
        # 100s later: still locked.
        allowed, retry = limiter.check("auth", "1.2.3.4", now=105.0)
        assert not allowed
        assert retry > 0
        # 906s later: lockout cleared (penalty + small slack).
        allowed, _ = limiter.check("auth", "1.2.3.4", now=911.0)
        assert allowed

    def test_window_rolls_off_for_non_lockout_bucket(self):
        limiter = InMemoryRateLimiter()
        # Admin bucket is 30/min; no lockout.
        for i in range(30):
            limiter.check("admin", "1.2.3.4", now=i * 0.1)
        # Hit limit at t=2.9
        allowed, _ = limiter.check("admin", "1.2.3.4", now=3.0)
        assert not allowed
        # 65s later, all old timestamps have rolled off.
        allowed, _ = limiter.check("admin", "1.2.3.4", now=70.0)
        assert allowed

    def test_keys_are_independent(self):
        limiter = InMemoryRateLimiter()
        for i in range(5):
            limiter.check("auth", "1.2.3.4", now=i * 1.0)
        # Different IP → fresh budget.
        allowed, _ = limiter.check("auth", "5.6.7.8", now=5.0)
        assert allowed

    def test_buckets_are_independent(self):
        limiter = InMemoryRateLimiter()
        for i in range(5):
            limiter.check("auth", "1.2.3.4", now=i * 1.0)
        # Same IP, different bucket → fresh budget.
        allowed, _ = limiter.check("admin", "1.2.3.4", now=5.0)
        assert allowed

    def test_unknown_bucket_passes(self):
        limiter = InMemoryRateLimiter()
        allowed, retry = limiter.check("never-defined", "1.2.3.4")
        assert allowed
        assert retry == 0.0

    def test_reset_clears_state(self):
        limiter = InMemoryRateLimiter()
        for i in range(5):
            limiter.check("auth", "1.2.3.4", now=i * 1.0)
        limiter.check("auth", "1.2.3.4", now=5.0)  # triggers lockout
        limiter.reset("auth", "1.2.3.4")
        # Lockout cleared, bucket fresh.
        allowed, _ = limiter.check("auth", "1.2.3.4", now=6.0)
        assert allowed

    def test_custom_buckets(self):
        custom = (BucketConfig(name="tiny", limit=2, window_seconds=10.0),)
        limiter = InMemoryRateLimiter(buckets=custom)
        assert limiter.check("tiny", "1.1.1.1", now=0.0)[0] is True
        assert limiter.check("tiny", "1.1.1.1", now=1.0)[0] is True
        assert limiter.check("tiny", "1.1.1.1", now=2.0)[0] is False


# ── DEFAULT_BUCKETS sanity ─────────────────────────────────────


class TestDefaultBuckets:
    def test_auth_has_lockout(self):
        auth = next(b for b in DEFAULT_BUCKETS if b.name == "auth")
        assert auth.limit == 5
        assert auth.window_seconds == 300.0
        assert auth.penalty_seconds == 900.0

    def test_admin_no_lockout(self):
        admin = next(b for b in DEFAULT_BUCKETS if b.name == "admin")
        assert admin.penalty_seconds == 0.0

    def test_default_bucket_is_300_per_minute(self):
        d = next(b for b in DEFAULT_BUCKETS if b.name == "default")
        assert d.limit == 300
        assert d.window_seconds == 60.0


# ── Middleware closure ─────────────────────────────────────────


class _FakeApp:
    def __init__(
        self,
        mode,
        limiter=None,
        trusted=(),
        sec_audit=None,
    ):
        self.state = type("State", (), {})()
        self.state.deployment_mode = mode
        self.state.rate_limiter = limiter
        self.state.trusted_proxies = trusted
        if sec_audit is not None:
            self.state.security_audit = sec_audit


class _FakeRequest:
    def __init__(
        self, peer="1.2.3.4", method="POST", path="/auth/login", app=None
    ):
        self.client = type("C", (), {"host": peer})()
        self.method = method
        self.url = type("U", (), {"path": path})()
        self.headers = {}
        self.app = app


async def _passthrough(_req):
    from fastapi.responses import JSONResponse

    return JSONResponse({"ok": True})


class TestMiddleware:
    @pytest.mark.asyncio
    async def test_trusted_mode_passthrough(self):
        mw = build_rate_limit_middleware()
        # Even without a limiter, trusted mode short-circuits.
        app = _FakeApp(DeploymentMode.TRUSTED)
        for _ in range(20):
            resp = await mw(
                _FakeRequest(peer="1.2.3.4", path="/auth/login", app=app),
                _passthrough,
            )
            assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_lan_mode_throttles_auth(self):
        limiter = InMemoryRateLimiter()
        mw = build_rate_limit_middleware(limiter=limiter)
        app = _FakeApp(DeploymentMode.LAN, limiter=limiter)
        # Allow 5, deny 6th.
        for _ in range(5):
            resp = await mw(
                _FakeRequest(peer="1.2.3.4", path="/auth/login", app=app),
                _passthrough,
            )
            assert resp.status_code == 200
        resp = await mw(
            _FakeRequest(peer="1.2.3.4", path="/auth/login", app=app),
            _passthrough,
        )
        assert resp.status_code == 429
        assert resp.headers.get("Retry-After") is not None

    @pytest.mark.asyncio
    async def test_lan_mode_default_bucket_off(self):
        limiter = InMemoryRateLimiter(
            buckets=(BucketConfig(name="default", limit=2, window_seconds=60),)
        )
        mw = build_rate_limit_middleware(limiter=limiter)
        app = _FakeApp(DeploymentMode.LAN, limiter=limiter)
        # Default bucket is off in LAN mode regardless of limit.
        for _ in range(5):
            resp = await mw(
                _FakeRequest(peer="1.2.3.4", method="GET", path="/api", app=app),
                _passthrough,
            )
            assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_web_mode_default_bucket_active(self):
        limiter = InMemoryRateLimiter(
            buckets=(BucketConfig(name="default", limit=2, window_seconds=60),)
        )
        mw = build_rate_limit_middleware(limiter=limiter)
        app = _FakeApp(DeploymentMode.WEB, limiter=limiter)
        # Web mode honors the default bucket.
        for _ in range(2):
            resp = await mw(
                _FakeRequest(peer="1.2.3.4", method="GET", path="/api", app=app),
                _passthrough,
            )
            assert resp.status_code == 200
        resp = await mw(
            _FakeRequest(peer="1.2.3.4", method="GET", path="/api", app=app),
            _passthrough,
        )
        assert resp.status_code == 429

    @pytest.mark.asyncio
    async def test_audit_emitted_on_429(self):
        from pinky_daemon.security_audit import SecurityAuditStore

        with tempfile.TemporaryDirectory() as tmpdir:
            sec_audit = SecurityAuditStore(
                db_path=os.path.join(tmpdir, "sec.db")
            )
            limiter = InMemoryRateLimiter()
            mw = build_rate_limit_middleware(limiter=limiter)
            app = _FakeApp(
                DeploymentMode.WEB,
                limiter=limiter,
                sec_audit=sec_audit,
            )
            # Burn through auth bucket.
            for _ in range(5):
                await mw(
                    _FakeRequest(peer="1.2.3.4", path="/auth/login", app=app),
                    _passthrough,
                )
            await mw(
                _FakeRequest(peer="1.2.3.4", path="/auth/login", app=app),
                _passthrough,
            )
            rows = sec_audit.query(event_type="rate_limit.exceeded")
            assert len(rows) == 1
            assert rows[0].outcome == "denied"
            assert rows[0].route_name == "auth"
            assert rows[0].actor_ip == "1.2.3.4"
            assert rows[0].detail == {"bucket": "auth", "limit": 5}

    @pytest.mark.asyncio
    async def test_uses_canonical_ip_from_resolver(self):
        # Untrusted peer can't pretend to be a different IP via XFF →
        # rate limit keys on the peer, not the spoofed XFF.
        import ipaddress

        limiter = InMemoryRateLimiter()
        mw = build_rate_limit_middleware(limiter=limiter)
        # No trusted_proxies → XFF dropped.
        app = _FakeApp(
            DeploymentMode.WEB, limiter=limiter, trusted=()
        )
        # Burn the bucket from peer 1.2.3.4 with spoofed XFF claiming
        # 9.9.9.9 — the limiter must key on 1.2.3.4.
        req_with_spoof = _FakeRequest(
            peer="1.2.3.4", path="/auth/login", app=app
        )
        req_with_spoof.headers = {"x-forwarded-for": "9.9.9.9"}
        for _ in range(5):
            await mw(req_with_spoof, _passthrough)
        # 6th from same peer (any spoof) → blocked.
        resp = await mw(req_with_spoof, _passthrough)
        assert resp.status_code == 429
        # Confirm attribution: a clean request from 9.9.9.9 with no XFF
        # is *not* blocked (the spoofed IP wasn't really being charged).
        clean = _FakeRequest(peer="9.9.9.9", path="/auth/login", app=app)
        resp_clean = await mw(clean, _passthrough)
        assert resp_clean.status_code == 200
        # Avoid unused-import warning
        _ = ipaddress


# ── End-to-end through create_api ──────────────────────────────


@pytest.fixture
def tmp_db_path():
    with tempfile.TemporaryDirectory() as tmpdir:
        yield os.path.join(tmpdir, "test.db")


class TestCreateApiRateLimit:
    def test_app_state_carries_limiter(self, tmp_db_path):
        from pinky_daemon.api import create_api

        app = create_api(
            db_path=tmp_db_path, deployment_mode=DeploymentMode.WEB
        )
        assert isinstance(
            app.state.rate_limiter, InMemoryRateLimiter
        )

    def test_trusted_mode_no_throttle_through_real_app(self, tmp_db_path):
        from fastapi.testclient import TestClient

        from pinky_daemon.api import create_api

        env = {**os.environ, "PINKY_LAN_EXTRA_CIDRS": "0.0.0.0/32"}
        with patch.dict(os.environ, env, clear=True):
            app = create_api(
                db_path=tmp_db_path, deployment_mode=DeploymentMode.TRUSTED
            )
        client = TestClient(app)
        # Hit /api many times; trusted mode never throttles.
        for _ in range(50):
            resp = client.get("/api")
            assert resp.status_code == 200


# Suppress unused import warning
_ = Any
