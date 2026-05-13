"""Tests for deployment-posture configuration (#434).

Covers the resolver, the bind-host helper, the insecure-exposure warning, and
the wiring through ``create_api`` so ``app.state.deployment_mode`` and the
``/api`` payload reflect the configured posture.
"""

from __future__ import annotations

import os
import tempfile
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from pinky_daemon.config import (
    DEPLOYMENT_MODE_ENV,
    DeploymentMode,
    InvalidDeploymentModeError,
    default_bind_host,
    resolve_deployment_mode,
    warn_if_insecure_exposure,
)

# ── resolve_deployment_mode ────────────────────────────────────


class TestResolveDeploymentMode:
    def test_unset_returns_trusted(self):
        assert resolve_deployment_mode(env={}) is DeploymentMode.TRUSTED

    def test_empty_string_returns_trusted(self):
        assert (
            resolve_deployment_mode(env={DEPLOYMENT_MODE_ENV: ""})
            is DeploymentMode.TRUSTED
        )

    def test_whitespace_only_returns_trusted(self):
        assert (
            resolve_deployment_mode(env={DEPLOYMENT_MODE_ENV: "   "})
            is DeploymentMode.TRUSTED
        )

    @pytest.mark.parametrize(
        "value,expected",
        [
            ("trusted", DeploymentMode.TRUSTED),
            ("lan", DeploymentMode.LAN),
            ("web", DeploymentMode.WEB),
        ],
    )
    def test_each_canonical_value(self, value, expected):
        assert resolve_deployment_mode(env={DEPLOYMENT_MODE_ENV: value}) is expected

    def test_case_insensitive(self):
        assert (
            resolve_deployment_mode(env={DEPLOYMENT_MODE_ENV: "WEB"})
            is DeploymentMode.WEB
        )
        assert (
            resolve_deployment_mode(env={DEPLOYMENT_MODE_ENV: "Lan"})
            is DeploymentMode.LAN
        )

    def test_strips_whitespace(self):
        assert (
            resolve_deployment_mode(env={DEPLOYMENT_MODE_ENV: "  web  "})
            is DeploymentMode.WEB
        )

    def test_invalid_value_raises(self):
        with pytest.raises(InvalidDeploymentModeError) as excinfo:
            resolve_deployment_mode(env={DEPLOYMENT_MODE_ENV: "prod"})
        # Error message should mention the env var, the bad value, and the
        # valid options so the operator knows how to fix it.
        msg = str(excinfo.value)
        assert "PINKY_DEPLOYMENT_MODE" in msg
        assert "prod" in msg
        assert "trusted" in msg
        assert "lan" in msg
        assert "web" in msg

    def test_invalid_alias_does_not_silently_pass(self):
        # No back-compat aliases — explicit only.
        for alias in ["production", "development", "dev", "local", "staging"]:
            with pytest.raises(InvalidDeploymentModeError):
                resolve_deployment_mode(env={DEPLOYMENT_MODE_ENV: alias})

    def test_reads_os_environ_by_default(self):
        with patch.dict(os.environ, {DEPLOYMENT_MODE_ENV: "lan"}, clear=False):
            assert resolve_deployment_mode() is DeploymentMode.LAN


# ── default_bind_host ──────────────────────────────────────────


class TestDefaultBindHost:
    def test_trusted_binds_all(self):
        # Back-compat with current Mac Mini deployment.
        assert default_bind_host(DeploymentMode.TRUSTED) == "0.0.0.0"

    def test_lan_binds_all(self):
        # LAN appliances need to be reachable on the local network.
        assert default_bind_host(DeploymentMode.LAN) == "0.0.0.0"

    def test_web_binds_loopback(self):
        # Web mode assumes a reverse proxy in front; refuse public bind.
        assert default_bind_host(DeploymentMode.WEB) == "127.0.0.1"


# ── warn_if_insecure_exposure ──────────────────────────────────


class TestWarnIfInsecureExposure:
    def test_trusted_with_all_interfaces_warns(self):
        warning = warn_if_insecure_exposure(DeploymentMode.TRUSTED, "0.0.0.0")
        assert warning is not None
        assert "trusted" in warning
        assert "0.0.0.0" in warning
        # Tells operator what to do about it.
        assert "PINKY_DEPLOYMENT_MODE" in warning
        assert "web" in warning

    def test_trusted_with_loopback_no_warning(self):
        assert warn_if_insecure_exposure(DeploymentMode.TRUSTED, "127.0.0.1") is None

    def test_trusted_with_lan_address_no_warning(self):
        # A specific bind address means the operator made a deliberate choice.
        assert warn_if_insecure_exposure(DeploymentMode.TRUSTED, "10.0.0.32") is None

    def test_lan_no_warning(self):
        assert warn_if_insecure_exposure(DeploymentMode.LAN, "0.0.0.0") is None

    def test_web_no_warning(self):
        # Web mode has its own enforcement (#436/#441); this helper only
        # catches the silent-trusted-exposure case.
        assert warn_if_insecure_exposure(DeploymentMode.WEB, "0.0.0.0") is None
        assert warn_if_insecure_exposure(DeploymentMode.WEB, "127.0.0.1") is None


# ── DeploymentMode enum ────────────────────────────────────────


class TestDeploymentModeEnum:
    def test_str_returns_value(self):
        assert str(DeploymentMode.TRUSTED) == "trusted"
        assert str(DeploymentMode.LAN) == "lan"
        assert str(DeploymentMode.WEB) == "web"

    def test_is_str_subclass(self):
        # Useful for direct comparison and JSON serialisation.
        assert isinstance(DeploymentMode.WEB, str)
        assert DeploymentMode.WEB == "web"


# ── create_api wiring ──────────────────────────────────────────


@pytest.fixture
def tmp_db_path():
    """Throwaway DB path so create_api doesn't touch the project's data dir."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield os.path.join(tmpdir, "test.db")


class TestCreateApiDeploymentMode:
    def test_explicit_mode_cached_on_app_state(self, tmp_db_path):
        from pinky_daemon.api import create_api

        app = create_api(db_path=tmp_db_path, deployment_mode=DeploymentMode.WEB)
        assert app.state.deployment_mode is DeploymentMode.WEB

    def test_default_resolves_from_env(self, tmp_db_path):
        from pinky_daemon.api import create_api

        with patch.dict(
            os.environ, {DEPLOYMENT_MODE_ENV: "lan"}, clear=False
        ):
            app = create_api(db_path=tmp_db_path)
        assert app.state.deployment_mode is DeploymentMode.LAN

    def test_default_when_env_unset(self, tmp_db_path):
        from pinky_daemon.api import create_api

        # Strip the env var so resolution lands on TRUSTED.
        env = {k: v for k, v in os.environ.items() if k != DEPLOYMENT_MODE_ENV}
        with patch.dict(os.environ, env, clear=True):
            app = create_api(db_path=tmp_db_path)
        assert app.state.deployment_mode is DeploymentMode.TRUSTED

    def test_api_endpoint_exposes_mode(self, tmp_db_path):
        from pinky_daemon.api import create_api

        app = create_api(db_path=tmp_db_path, deployment_mode=DeploymentMode.WEB)
        client = TestClient(app)
        resp = client.get("/api")
        assert resp.status_code == 200
        body = resp.json()
        assert body["deployment_mode"] == "web"

    def test_api_endpoint_exposes_mode_for_each_value(self, tmp_db_path):
        from pinky_daemon.api import create_api

        # In LAN mode the LAN filter (#436) would reject TestClient's
        # synthetic peer; opt 0.0.0.0/32 in via env so the test reaches
        # /api in every mode.
        env = {**os.environ, "PINKY_LAN_EXTRA_CIDRS": "0.0.0.0/32"}
        with patch.dict(os.environ, env, clear=True):
            for mode in DeploymentMode:
                app = create_api(db_path=tmp_db_path, deployment_mode=mode)
                client = TestClient(app)
                resp = client.get("/api")
            assert resp.json()["deployment_mode"] == mode.value
