"""Tests for the canonical trusted-client-IP resolver (#442).

Covers:

* ``parse_trusted_proxies`` — comma list → IPNetwork tuple, malformed entries
  skipped, bare IPs accepted as /32 or /128.
* ``resolve_client_identity`` per deployment mode (trusted/lan/web), including
  trusted vs. untrusted upstream peers, IPv6-mapped IPv4 normalisation,
  comma-separated XFF chains, RFC 7239 ``Forwarded`` precedence over XFF, and
  malformed-header fallback.
* The ``get_client_identity`` FastAPI dependency reading from app state and
  falling back to env when state is empty.
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
from pinky_daemon.net.client_identity import (
    TRUSTED_PROXIES_ENV,
    ClientIdentity,
    get_client_identity,
    parse_trusted_proxies,
    resolve_client_identity,
    trusted_proxies_from_env,
)

# ── Fake Request helper ────────────────────────────────────────


class _FakeRequest:
    """Minimal stand-in for starlette.Request — enough for the resolver.

    We don't go through the ASGI test client for unit tests so we can
    precisely control the peer address (TestClient always reports
    ``testclient`` which would defeat the point of these checks).
    """

    def __init__(
        self,
        peer: str | None = "127.0.0.1",
        headers: dict[str, str] | None = None,
        app: Any = None,
    ):
        self.client = (
            None if peer is None else type("Client", (), {"host": peer})()
        )
        self.headers = headers or {}
        self.app = app


# ── parse_trusted_proxies ──────────────────────────────────────


class TestParseTrustedProxies:
    def test_empty_string(self):
        assert parse_trusted_proxies("") == ()

    def test_none(self):
        assert parse_trusted_proxies(None) == ()

    def test_whitespace_only(self):
        assert parse_trusted_proxies("   ") == ()

    def test_single_cidr(self):
        result = parse_trusted_proxies("10.0.0.0/8")
        assert result == (ipaddress.ip_network("10.0.0.0/8"),)

    def test_bare_ipv4_treated_as_slash_32(self):
        result = parse_trusted_proxies("127.0.0.1")
        assert result == (ipaddress.ip_network("127.0.0.1/32"),)

    def test_bare_ipv6_treated_as_slash_128(self):
        result = parse_trusted_proxies("::1")
        assert result == (ipaddress.ip_network("::1/128"),)

    def test_multiple_with_whitespace(self):
        result = parse_trusted_proxies(" 127.0.0.1/32 , 10.0.0.0/8 ")
        assert result == (
            ipaddress.ip_network("127.0.0.1/32"),
            ipaddress.ip_network("10.0.0.0/8"),
        )

    def test_invalid_entry_skipped(self):
        result = parse_trusted_proxies("not-an-ip,10.0.0.0/8,99.99.99.999")
        # Only the valid entry survives.
        assert result == (ipaddress.ip_network("10.0.0.0/8"),)

    def test_empty_token_skipped(self):
        result = parse_trusted_proxies("10.0.0.0/8,,127.0.0.1")
        assert ipaddress.ip_network("10.0.0.0/8") in result
        assert ipaddress.ip_network("127.0.0.1/32") in result
        assert len(result) == 2

    def test_host_bits_set_accepted(self):
        # strict=False so 10.0.0.5/8 doesn't reject — operators copy-paste
        # specific server addresses with subnet masks all the time.
        result = parse_trusted_proxies("10.0.0.5/8")
        assert result == (ipaddress.ip_network("10.0.0.0/8"),)


class TestTrustedProxiesFromEnv:
    def test_unset(self):
        assert trusted_proxies_from_env(env={}) == ()

    def test_reads_env(self):
        result = trusted_proxies_from_env(env={TRUSTED_PROXIES_ENV: "10.0.0.0/8"})
        assert result == (ipaddress.ip_network("10.0.0.0/8"),)


# ── resolve_client_identity: trusted mode ──────────────────────


class TestResolveTrustedMode:
    def test_uses_raw_peer(self):
        req = _FakeRequest(peer="192.168.1.42")
        ci = resolve_client_identity(req, DeploymentMode.TRUSTED)
        assert ci.ip == ipaddress.ip_address("192.168.1.42")
        assert ci.via_proxy is False
        assert ci.raw_peer == ipaddress.ip_address("192.168.1.42")
        assert ci.forwarded_chain == ()

    def test_ignores_xff(self):
        req = _FakeRequest(
            peer="192.168.1.42", headers={"x-forwarded-for": "1.2.3.4"}
        )
        ci = resolve_client_identity(req, DeploymentMode.TRUSTED)
        # XFF is dropped on the floor — peer is the truth.
        assert ci.ip == ipaddress.ip_address("192.168.1.42")
        assert ci.via_proxy is False
        assert ci.forwarded_chain == ()

    def test_ignores_forwarded_header(self):
        req = _FakeRequest(
            peer="192.168.1.42", headers={"forwarded": "for=1.2.3.4"}
        )
        ci = resolve_client_identity(req, DeploymentMode.TRUSTED)
        assert ci.ip == ipaddress.ip_address("192.168.1.42")
        assert ci.via_proxy is False


# ── resolve_client_identity: lan / web mode ────────────────────


@pytest.mark.parametrize("mode", [DeploymentMode.LAN, DeploymentMode.WEB])
class TestResolveProxyAware:
    def test_no_xff_returns_peer(self, mode):
        req = _FakeRequest(peer="10.0.0.5")
        ci = resolve_client_identity(req, mode, trusted_proxies=())
        assert ci.ip == ipaddress.ip_address("10.0.0.5")
        assert ci.via_proxy is False

    def test_untrusted_peer_xff_ignored(self, mode):
        # Public peer claiming to be a proxy with a private "client"
        # address. With no trusted_proxies configured the chain is dropped.
        req = _FakeRequest(
            peer="203.0.113.7",
            headers={"x-forwarded-for": "10.0.0.99"},
        )
        ci = resolve_client_identity(req, mode, trusted_proxies=())
        assert ci.ip == ipaddress.ip_address("203.0.113.7")
        assert ci.via_proxy is False
        assert ci.forwarded_chain == ()

    def test_trusted_peer_xff_honored(self, mode):
        proxies = parse_trusted_proxies("127.0.0.1")
        req = _FakeRequest(
            peer="127.0.0.1",
            headers={"x-forwarded-for": "203.0.113.7"},
        )
        ci = resolve_client_identity(req, mode, trusted_proxies=proxies)
        assert ci.ip == ipaddress.ip_address("203.0.113.7")
        assert ci.via_proxy is True
        assert ci.raw_peer == ipaddress.ip_address("127.0.0.1")
        assert ci.forwarded_chain == (ipaddress.ip_address("203.0.113.7"),)

    def test_trusted_peer_xff_chain_skips_trusted_hops(self, mode):
        # Two trusted proxies in series: 127.0.0.1 in front, 10.0.0.5 behind.
        # Real client is 198.51.100.42.
        proxies = parse_trusted_proxies("127.0.0.1,10.0.0.0/8")
        req = _FakeRequest(
            peer="127.0.0.1",
            headers={"x-forwarded-for": "198.51.100.42, 10.0.0.5"},
        )
        ci = resolve_client_identity(req, mode, trusted_proxies=proxies)
        assert ci.ip == ipaddress.ip_address("198.51.100.42")
        assert ci.via_proxy is True
        assert ci.forwarded_chain == (
            ipaddress.ip_address("198.51.100.42"),
            ipaddress.ip_address("10.0.0.5"),
        )

    def test_all_trusted_chain_returns_leftmost(self, mode):
        # If the whole chain is trusted proxies (synthetic or misconfigured),
        # the leftmost is the closest-to-client we can name.
        proxies = parse_trusted_proxies("10.0.0.0/8")
        req = _FakeRequest(
            peer="10.0.0.1",
            headers={"x-forwarded-for": "10.0.0.99, 10.0.0.5"},
        )
        ci = resolve_client_identity(req, mode, trusted_proxies=proxies)
        assert ci.ip == ipaddress.ip_address("10.0.0.99")
        assert ci.via_proxy is True

    def test_ipv6_mapped_ipv4_normalised(self, mode):
        # Dual-stack listener gives us ::ffff:10.0.0.5 as the peer; trust
        # rule is IPv4. Should still be honored.
        proxies = parse_trusted_proxies("10.0.0.0/8")
        req = _FakeRequest(
            peer="::ffff:10.0.0.5",
            headers={"x-forwarded-for": "203.0.113.7"},
        )
        ci = resolve_client_identity(req, mode, trusted_proxies=proxies)
        assert ci.ip == ipaddress.ip_address("203.0.113.7")
        assert ci.via_proxy is True
        assert ci.raw_peer == ipaddress.ip_address("10.0.0.5")

    def test_forwarded_header_takes_precedence_over_xff(self, mode):
        proxies = parse_trusted_proxies("127.0.0.1")
        req = _FakeRequest(
            peer="127.0.0.1",
            headers={
                "forwarded": 'for=198.51.100.10',
                "x-forwarded-for": "203.0.113.7",
            },
        )
        ci = resolve_client_identity(req, mode, trusted_proxies=proxies)
        # Forwarded wins.
        assert ci.ip == ipaddress.ip_address("198.51.100.10")
        assert ci.via_proxy is True

    def test_forwarded_header_with_quoted_ipv6(self, mode):
        proxies = parse_trusted_proxies("127.0.0.1")
        req = _FakeRequest(
            peer="127.0.0.1",
            headers={"forwarded": 'for="[2001:db8::1]:443"'},
        )
        ci = resolve_client_identity(req, mode, trusted_proxies=proxies)
        assert ci.ip == ipaddress.ip_address("2001:db8::1")
        assert ci.via_proxy is True

    def test_xff_with_port_stripped(self, mode):
        # Some proxies emit IPv4 with a port appended.
        proxies = parse_trusted_proxies("127.0.0.1")
        req = _FakeRequest(
            peer="127.0.0.1",
            headers={"x-forwarded-for": "203.0.113.7:54321"},
        )
        ci = resolve_client_identity(req, mode, trusted_proxies=proxies)
        assert ci.ip == ipaddress.ip_address("203.0.113.7")

    def test_malformed_xff_falls_back_to_peer(self, mode):
        proxies = parse_trusted_proxies("127.0.0.1")
        req = _FakeRequest(
            peer="127.0.0.1",
            headers={"x-forwarded-for": "garbage,more-garbage"},
        )
        ci = resolve_client_identity(req, mode, trusted_proxies=proxies)
        # Whole chain unparseable → no client found → fall back to peer.
        assert ci.ip == ipaddress.ip_address("127.0.0.1")
        assert ci.via_proxy is False
        assert ci.forwarded_chain == ()

    def test_partially_malformed_xff_keeps_valid_entries(self, mode):
        proxies = parse_trusted_proxies("127.0.0.1")
        req = _FakeRequest(
            peer="127.0.0.1",
            headers={"x-forwarded-for": "garbage,203.0.113.7"},
        )
        ci = resolve_client_identity(req, mode, trusted_proxies=proxies)
        # The good entry is honored; junk is dropped silently.
        assert ci.ip == ipaddress.ip_address("203.0.113.7")
        assert ci.via_proxy is True

    def test_missing_client_attribute(self, mode):
        # Starlette can produce a Request with client=None (lifespan / test).
        req = _FakeRequest(peer=None)
        ci = resolve_client_identity(req, mode, trusted_proxies=())
        assert ci.raw_peer == ipaddress.ip_address("0.0.0.0")
        assert ci.ip == ipaddress.ip_address("0.0.0.0")


# ── ClientIdentity dataclass ───────────────────────────────────


class TestClientIdentityDataclass:
    def test_immutable(self):
        ci = ClientIdentity(
            ip=ipaddress.ip_address("10.0.0.1"),
            via_proxy=False,
            raw_peer=ipaddress.ip_address("10.0.0.1"),
        )
        with pytest.raises(Exception):  # FrozenInstanceError or AttributeError
            ci.ip = ipaddress.ip_address("10.0.0.2")  # type: ignore[misc]

    def test_default_chain_is_empty_tuple(self):
        ci = ClientIdentity(
            ip=ipaddress.ip_address("10.0.0.1"),
            via_proxy=False,
            raw_peer=ipaddress.ip_address("10.0.0.1"),
        )
        assert ci.forwarded_chain == ()


# ── get_client_identity (FastAPI dep) ──────────────────────────


class TestGetClientIdentityDep:
    def test_reads_from_app_state(self):
        proxies = parse_trusted_proxies("127.0.0.1")
        app = type("App", (), {})()
        app.state = type("State", (), {})()
        app.state.deployment_mode = DeploymentMode.WEB
        app.state.trusted_proxies = proxies
        req = _FakeRequest(
            peer="127.0.0.1",
            headers={"x-forwarded-for": "203.0.113.7"},
            app=app,
        )
        ci = get_client_identity(req)
        assert ci.ip == ipaddress.ip_address("203.0.113.7")
        assert ci.via_proxy is True

    def test_falls_back_to_env_when_state_unset(self):
        env = {
            k: v
            for k, v in os.environ.items()
            if k not in {"PINKY_DEPLOYMENT_MODE", TRUSTED_PROXIES_ENV}
        }
        env["PINKY_DEPLOYMENT_MODE"] = "web"
        env[TRUSTED_PROXIES_ENV] = "127.0.0.1"
        app = type("App", (), {})()
        app.state = type("State", (), {})()
        # No deployment_mode / trusted_proxies on state → fall back to env.
        req = _FakeRequest(
            peer="127.0.0.1",
            headers={"x-forwarded-for": "198.51.100.10"},
            app=app,
        )
        with patch.dict(os.environ, env, clear=True):
            ci = get_client_identity(req)
        assert ci.ip == ipaddress.ip_address("198.51.100.10")
        assert ci.via_proxy is True


# ── End-to-end through create_api ──────────────────────────────


@pytest.fixture
def tmp_db_path():
    with tempfile.TemporaryDirectory() as tmpdir:
        yield os.path.join(tmpdir, "test.db")


class TestCreateApiTrustedProxies:
    def test_app_state_carries_trusted_proxies(self, tmp_db_path):
        from pinky_daemon.api import create_api

        env = {**os.environ, TRUSTED_PROXIES_ENV: "10.0.0.0/8,127.0.0.1"}
        with patch.dict(os.environ, env, clear=True):
            app = create_api(
                db_path=tmp_db_path, deployment_mode=DeploymentMode.WEB
            )
        assert ipaddress.ip_network("10.0.0.0/8") in app.state.trusted_proxies
        assert ipaddress.ip_network("127.0.0.1/32") in app.state.trusted_proxies

    def test_app_state_defaults_to_empty_when_env_unset(self, tmp_db_path):
        from pinky_daemon.api import create_api

        env = {k: v for k, v in os.environ.items() if k != TRUSTED_PROXIES_ENV}
        with patch.dict(os.environ, env, clear=True):
            app = create_api(
                db_path=tmp_db_path, deployment_mode=DeploymentMode.WEB
            )
        assert app.state.trusted_proxies == ()

    def test_dependency_resolves_through_real_app(self, tmp_db_path):
        # End-to-end: a route uses Depends(get_client_identity), the
        # TestClient sends an XFF header, and the resolver picks it up
        # because we configured ``0.0.0.0`` to be trusted. (Starlette's
        # TestClient reports its peer as the unspecified address since it
        # bypasses the socket layer.)
        from fastapi import Depends

        from pinky_daemon.api import create_api

        env = {
            **os.environ,
            "PINKY_DEPLOYMENT_MODE": "web",
            TRUSTED_PROXIES_ENV: "0.0.0.0/32",
        }
        with patch.dict(os.environ, env, clear=True):
            app = create_api(db_path=tmp_db_path)

        @app.get("/_test/who")
        def _who(ci=Depends(get_client_identity)):  # noqa: B008
            return {
                "ip": str(ci.ip),
                "via_proxy": ci.via_proxy,
                "raw_peer": str(ci.raw_peer),
            }

        client = TestClient(app)
        resp = client.get("/_test/who", headers={"x-forwarded-for": "203.0.113.7"})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["ip"] == "203.0.113.7"
        assert body["via_proxy"] is True
        # raw_peer is whatever Starlette's TestClient reports, normalised
        # through the parser. We just confirm the field is present and
        # doesn't crash; it doesn't matter for the contract.
        assert "raw_peer" in body


# ── Sanity: untrusted-peer warning is logged ───────────────────


class TestWarningSurfacing:
    def test_untrusted_proxy_warning_logged(self, caplog):
        # Reset throttle state so the test warning isn't suppressed by an
        # earlier test in the same process.
        from pinky_daemon.net import client_identity as ci_mod

        ci_mod._warn_state.clear()

        req = _FakeRequest(
            peer="203.0.113.7",
            headers={"x-forwarded-for": "10.0.0.99"},
        )
        with caplog.at_level("WARNING", logger=ci_mod.logger.name):
            resolve_client_identity(req, DeploymentMode.WEB, trusted_proxies=())
        assert any(
            "untrusted peer" in rec.message
            and "dropping chain" in rec.message
            for rec in caplog.records
        )
