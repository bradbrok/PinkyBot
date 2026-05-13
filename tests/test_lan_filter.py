"""Tests for the LAN-only middleware (#436).

Covers the private-CIDR helper, the env-driven allowlist composer, the
middleware closure (no-op in trusted/web, enforce in lan), and end-to-end
wiring through ``create_api`` (TestClient against an app spun up in each
mode).
"""

from __future__ import annotations

import ipaddress
import os
import tempfile
from typing import Any
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from pinky_daemon.config import DeploymentMode
from pinky_daemon.net.lan_filter import (
    DEFAULT_PRIVATE_CIDRS,
    EXTRA_CIDRS_ENV,
    build_lan_filter_middleware,
    is_private_ip,
    resolve_lan_allowed_cidrs,
)

# ── is_private_ip ──────────────────────────────────────────────


class TestIsPrivateIp:
    @pytest.mark.parametrize(
        "addr",
        [
            "10.0.0.5",
            "172.16.42.1",
            "172.31.255.254",
            "192.168.1.1",
            "127.0.0.1",
            "169.254.1.2",
            "::1",
            "fe80::1",
            "fdab:cdef::1",
        ],
    )
    def test_default_cidrs_accept_private(self, addr):
        assert is_private_ip(
            ipaddress.ip_address(addr), DEFAULT_PRIVATE_CIDRS
        )

    @pytest.mark.parametrize(
        "addr",
        [
            "8.8.8.8",
            "1.1.1.1",
            "203.0.113.7",
            "172.32.0.1",  # just outside 172.16/12
            "169.255.0.1",  # just outside link-local
            "100.64.0.5",  # CGNAT — explicitly NOT in defaults
            "2001:db8::1",  # public IPv6
        ],
    )
    def test_default_cidrs_reject_public(self, addr):
        assert not is_private_ip(
            ipaddress.ip_address(addr), DEFAULT_PRIVATE_CIDRS
        )

    def test_cgnat_only_accepted_when_extra(self):
        extras = (ipaddress.ip_network("100.64.0.0/10"),)
        allowlist = DEFAULT_PRIVATE_CIDRS + extras
        assert is_private_ip(ipaddress.ip_address("100.64.0.5"), allowlist)

    def test_ipv4_addr_against_ipv6_net_does_not_crash(self):
        only_v6 = (ipaddress.ip_network("fd00::/8"),)
        # No match, but importantly: no TypeError.
        assert not is_private_ip(ipaddress.ip_address("10.0.0.5"), only_v6)

    def test_empty_allowlist_rejects_everything(self):
        assert not is_private_ip(ipaddress.ip_address("10.0.0.5"), ())


# ── resolve_lan_allowed_cidrs ──────────────────────────────────


class TestResolveLanAllowedCidrs:
    def test_unset_returns_defaults(self):
        result = resolve_lan_allowed_cidrs(env={})
        assert result == DEFAULT_PRIVATE_CIDRS

    def test_extra_appended(self):
        result = resolve_lan_allowed_cidrs(
            env={EXTRA_CIDRS_ENV: "100.64.0.0/10"}
        )
        assert result[: len(DEFAULT_PRIVATE_CIDRS)] == DEFAULT_PRIVATE_CIDRS
        assert ipaddress.ip_network("100.64.0.0/10") in result

    def test_invalid_extras_skipped(self):
        result = resolve_lan_allowed_cidrs(
            env={EXTRA_CIDRS_ENV: "garbage,100.64.0.0/10"}
        )
        assert ipaddress.ip_network("100.64.0.0/10") in result
        # Length = defaults + 1 valid extra; junk dropped.
        assert len(result) == len(DEFAULT_PRIVATE_CIDRS) + 1


# ── Middleware closure (unit) ──────────────────────────────────


class _FakeApp:
    def __init__(self, mode, trusted=(), allowlist=None):
        self.state = type("State", (), {})()
        self.state.deployment_mode = mode
        self.state.trusted_proxies = trusted
        if allowlist is not None:
            self.state.lan_allowed_cidrs = allowlist


class _FakeRequest:
    def __init__(
        self,
        peer="10.0.0.5",
        method="GET",
        path="/",
        headers=None,
        app=None,
    ):
        self.client = type("C", (), {"host": peer})()
        self.method = method
        self.url = type("U", (), {"path": path})()
        self.headers = headers or {}
        self.app = app


async def _passthrough(_req):
    return "ok"


class TestMiddlewareModes:
    @pytest.mark.asyncio
    async def test_trusted_mode_passthrough(self):
        mw = build_lan_filter_middleware(allowlist=DEFAULT_PRIVATE_CIDRS)
        app = _FakeApp(DeploymentMode.TRUSTED)
        # Public IP should still pass — trusted mode is no-op.
        req = _FakeRequest(peer="8.8.8.8", app=app)
        result = await mw(req, _passthrough)
        assert result == "ok"

    @pytest.mark.asyncio
    async def test_web_mode_passthrough(self):
        mw = build_lan_filter_middleware(allowlist=DEFAULT_PRIVATE_CIDRS)
        app = _FakeApp(DeploymentMode.WEB)
        req = _FakeRequest(peer="8.8.8.8", app=app)
        result = await mw(req, _passthrough)
        assert result == "ok"

    @pytest.mark.asyncio
    async def test_lan_mode_accepts_private(self):
        mw = build_lan_filter_middleware(allowlist=DEFAULT_PRIVATE_CIDRS)
        app = _FakeApp(DeploymentMode.LAN)
        req = _FakeRequest(peer="192.168.1.10", app=app)
        result = await mw(req, _passthrough)
        assert result == "ok"

    @pytest.mark.asyncio
    async def test_lan_mode_rejects_public(self):
        from fastapi.responses import JSONResponse

        mw = build_lan_filter_middleware(allowlist=DEFAULT_PRIVATE_CIDRS)
        app = _FakeApp(DeploymentMode.LAN)
        req = _FakeRequest(peer="8.8.8.8", app=app)
        result = await mw(req, _passthrough)
        assert isinstance(result, JSONResponse)
        assert result.status_code == 403

    @pytest.mark.asyncio
    async def test_lan_mode_xff_only_honored_when_proxy_trusted(self):
        # Untrusted peer claims a private "client" → must NOT pass.
        from fastapi.responses import JSONResponse

        mw = build_lan_filter_middleware(allowlist=DEFAULT_PRIVATE_CIDRS)
        app = _FakeApp(DeploymentMode.LAN, trusted=())
        req = _FakeRequest(
            peer="8.8.8.8",
            headers={"x-forwarded-for": "10.0.0.5"},
            app=app,
        )
        result = await mw(req, _passthrough)
        assert isinstance(result, JSONResponse)
        assert result.status_code == 403

    @pytest.mark.asyncio
    async def test_lan_mode_xff_honored_when_proxy_trusted(self):
        # Trusted peer can vouch for a private client.
        mw = build_lan_filter_middleware(allowlist=DEFAULT_PRIVATE_CIDRS)
        trusted = (ipaddress.ip_network("172.16.0.0/12"),)
        app = _FakeApp(DeploymentMode.LAN, trusted=trusted)
        req = _FakeRequest(
            peer="172.16.0.1",
            headers={"x-forwarded-for": "10.0.0.5"},
            app=app,
        )
        result = await mw(req, _passthrough)
        assert result == "ok"

    @pytest.mark.asyncio
    async def test_lan_mode_extra_cidrs_via_state(self):
        # Operator opted into CGNAT via env → cached on app.state.
        cgnat = (ipaddress.ip_network("100.64.0.0/10"),)
        custom_allowlist = DEFAULT_PRIVATE_CIDRS + cgnat
        mw = build_lan_filter_middleware(allowlist=custom_allowlist)
        app = _FakeApp(DeploymentMode.LAN)
        req = _FakeRequest(peer="100.64.0.5", app=app)
        result = await mw(req, _passthrough)
        assert result == "ok"

    @pytest.mark.asyncio
    async def test_lan_mode_falls_back_to_env_when_no_allowlist_passed(self):
        # No explicit allowlist on the closure or on app.state → middleware
        # recomputes from env on each call.
        mw = build_lan_filter_middleware(allowlist=None)
        app = _FakeApp(DeploymentMode.LAN)
        # Make sure no lan_allowed_cidrs is on state.
        if hasattr(app.state, "lan_allowed_cidrs"):
            del app.state.lan_allowed_cidrs
        req = _FakeRequest(peer="10.0.0.5", app=app)
        result = await mw(req, _passthrough)
        assert result == "ok"


# ── End-to-end through create_api ──────────────────────────────


@pytest.fixture
def tmp_db_path():
    with tempfile.TemporaryDirectory() as tmpdir:
        yield os.path.join(tmpdir, "test.db")


class TestCreateApiLanFilter:
    def test_trusted_mode_no_filter_applied(self, tmp_db_path):
        # Public source → still 200 (the unrelated /api endpoint).
        from pinky_daemon.api import create_api

        env = {k: v for k, v in os.environ.items() if k != EXTRA_CIDRS_ENV}
        with patch.dict(os.environ, env, clear=True):
            app = create_api(
                db_path=tmp_db_path, deployment_mode=DeploymentMode.TRUSTED
            )
        client = TestClient(app)
        # TestClient peer ends up as 0.0.0.0 (unspecified) — would be
        # rejected by the LAN filter, but trusted mode is a no-op so it
        # should still hit the route.
        resp = client.get("/api")
        assert resp.status_code == 200

    def test_lan_mode_default_peer_rejected(self, tmp_db_path):
        # TestClient's peer is 0.0.0.0 / "testclient" → not in private
        # CIDRs → 403 in LAN mode.
        from pinky_daemon.api import create_api

        env = {k: v for k, v in os.environ.items() if k != EXTRA_CIDRS_ENV}
        with patch.dict(os.environ, env, clear=True):
            app = create_api(
                db_path=tmp_db_path, deployment_mode=DeploymentMode.LAN
            )
        client = TestClient(app)
        resp = client.get("/api")
        assert resp.status_code == 403
        body = resp.json()
        assert body["error"] == "forbidden"

    def test_lan_mode_extra_cidrs_unblocks_test_client(self, tmp_db_path):
        # If we add 0.0.0.0/32 to the allowlist via env, TestClient's
        # synthetic peer passes.
        from pinky_daemon.api import create_api

        env = {**os.environ, EXTRA_CIDRS_ENV: "0.0.0.0/32"}
        with patch.dict(os.environ, env, clear=True):
            app = create_api(
                db_path=tmp_db_path, deployment_mode=DeploymentMode.LAN
            )
        client = TestClient(app)
        resp = client.get("/api")
        assert resp.status_code == 200

    def test_web_mode_no_filter_applied(self, tmp_db_path):
        # Web mode delegates to the reverse-proxy contract (#441 + bind
        # default). The LAN middleware itself must not interfere.
        from pinky_daemon.api import create_api

        env = {k: v for k, v in os.environ.items() if k != EXTRA_CIDRS_ENV}
        with patch.dict(os.environ, env, clear=True):
            app = create_api(
                db_path=tmp_db_path, deployment_mode=DeploymentMode.WEB
            )
        client = TestClient(app)
        resp = client.get("/api")
        # /api is unauthenticated; web mode should not block on source.
        assert resp.status_code == 200


# Placeholder to keep type-checkers happy on the unused import.
_ = Any
