"""Handshake auth for the OpenClaw gateway.

The gateway exposes device methods (camera, SMS, location, contacts, callLog)
to anyone who completes the handshake, so an unconfigured token must NOT mean
"open to everyone". _auth_ok() is fail-closed: no token configured => refuse,
unless the owner explicitly opts out with OPENCLAW_GATEWAY_ALLOW_ANON=1.
"""

import pytest

from pinky_daemon import openclaw_gateway as gw

TOKEN = "s3cret-token"


@pytest.fixture(autouse=True)
def clean_env(tmp_path, monkeypatch):
    """No token / flag in the environment and no .env file to fall back to."""
    monkeypatch.setattr(gw, "_ENV_FILES", [str(tmp_path / "missing.env")])
    monkeypatch.delenv("OPENCLAW_GATEWAY_TOKEN", raising=False)
    monkeypatch.delenv("OPENCLAW_GATEWAY_ALLOW_ANON", raising=False)


def test_rejects_when_no_token_configured():
    ok, reason = gw._auth_ok({"auth": {"token": "whatever"}})
    assert ok is False
    assert "not configured" in reason


def test_allows_anonymous_when_explicitly_opted_in(monkeypatch):
    monkeypatch.setenv("OPENCLAW_GATEWAY_ALLOW_ANON", "1")
    ok, _ = gw._auth_ok({})
    assert ok is True


def test_accepts_matching_token(monkeypatch):
    monkeypatch.setenv("OPENCLAW_GATEWAY_TOKEN", TOKEN)
    ok, _ = gw._auth_ok({"auth": {"token": TOKEN}})
    assert ok is True


def test_rejects_wrong_token(monkeypatch):
    monkeypatch.setenv("OPENCLAW_GATEWAY_TOKEN", TOKEN)
    ok, reason = gw._auth_ok({"auth": {"token": "wrong"}})
    assert ok is False
    assert "invalid or missing" in reason


@pytest.mark.parametrize("field", ["token", "bootstrapToken", "password"])
def test_accepts_token_in_any_auth_field(monkeypatch, field):
    monkeypatch.setenv("OPENCLAW_GATEWAY_TOKEN", TOKEN)
    ok, _ = gw._auth_ok({"auth": {field: TOKEN}})
    assert ok is True


def test_token_resolved_from_env_file_when_environ_is_empty(tmp_path, monkeypatch):
    """A restart that skips _load_dotenv() must not silently drop enforcement."""
    env_file = tmp_path / ".env"
    env_file.write_text(f'OPENCLAW_GATEWAY_TOKEN="{TOKEN}"\n')
    monkeypatch.setattr(gw, "_ENV_FILES", [str(env_file)])
    assert gw._auth_ok({"auth": {"token": TOKEN}})[0] is True
    assert gw._auth_ok({"auth": {"token": "wrong"}})[0] is False


def test_anon_flag_only_honoured_for_truthy_values(monkeypatch):
    monkeypatch.setenv("OPENCLAW_GATEWAY_ALLOW_ANON", "0")
    assert gw._auth_ok({})[0] is False
