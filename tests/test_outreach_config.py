"""Tests for outreach configuration store."""

from __future__ import annotations

import os
import tempfile

import pytest

from pinky_daemon.outreach_config import OutreachConfigStore, PlatformConfig


@pytest.fixture
def store():
    """Create a temporary outreach config store."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    s = OutreachConfigStore(db_path=path)
    yield s
    s.close()
    os.unlink(path)


class TestOutreachConfigStore:
    def test_configure_new(self, store):
        config = store.configure("telegram", token="bot123", enabled=True)
        assert config.platform == "telegram"
        assert config.enabled is True
        assert config.token_set is True
        assert config.created_at > 0

    def test_token_never_exposed(self, store):
        config = store.configure("telegram", token="secret-token-123")
        d = config.to_dict()
        assert "secret-token-123" not in str(d)
        assert d["token_set"] is True

    def test_get_token_internal(self, store):
        store.configure("telegram", token="my-secret")
        assert store.get_token("telegram") == "my-secret"

    def test_get_token_missing(self, store):
        assert store.get_token("telegram") == ""

    def test_configure_with_settings(self, store):
        config = store.configure(
            "telegram",
            token="bot123",
            settings={"allowed_chats": ["123", "456"], "poll_timeout": 30},
        )
        assert config.settings["allowed_chats"] == ["123", "456"]
        assert config.settings["poll_timeout"] == 30

    def test_configure_update(self, store):
        store.configure("telegram", token="old-token")
        config = store.configure("telegram", token="new-token")
        assert store.get_token("telegram") == "new-token"

    def test_configure_partial_update(self, store):
        store.configure("telegram", token="my-token", enabled=True)
        # Update only settings, keep token
        config = store.configure("telegram", settings={"poll_timeout": 60})
        assert config.enabled is True
        assert store.get_token("telegram") == "my-token"
        assert config.settings["poll_timeout"] == 60

    def test_configure_unsupported_platform(self, store):
        with pytest.raises(ValueError, match="Unsupported platform"):
            store.configure("whatsapp")

    def test_get(self, store):
        store.configure("discord", token="disc-token")
        config = store.get("discord")
        assert config is not None
        assert config.platform == "discord"
        assert config.token_set is True

    def test_get_missing(self, store):
        assert store.get("telegram") is None

    def test_list_empty(self, store):
        assert store.list() == []

    def test_list(self, store):
        store.configure("telegram", token="t1")
        store.configure("discord", token="d1")
        result = store.list()
        assert len(result) == 2
        platforms = [p.platform for p in result]
        assert "discord" in platforms
        assert "telegram" in platforms

    def test_enable_disable(self, store):
        store.configure("telegram", token="t1", enabled=True)
        assert store.disable("telegram") is True
        assert store.get("telegram").enabled is False
        assert store.enable("telegram") is True
        assert store.get("telegram").enabled is True

    def test_enable_missing(self, store):
        assert store.enable("telegram") is False

    def test_delete(self, store):
        store.configure("telegram", token="t1")
        assert store.delete("telegram") is True
        assert store.get("telegram") is None

    def test_delete_missing(self, store):
        assert store.delete("telegram") is False

    def test_auto_enable_on_token(self, store):
        config = store.configure("telegram", token="bot123")
        assert config.enabled is True

    def test_no_auto_enable_without_token(self, store):
        config = store.configure("telegram")
        assert config.enabled is False

    def test_test_connection_not_configured(self, store):
        result = store.test_connection("telegram")
        assert result["success"] is False
        assert "Not configured" in result["error"]

    def test_test_connection_no_token(self, store):
        store.configure("telegram", enabled=True)
        result = store.test_connection("telegram")
        assert result["success"] is False
        assert "No token" in result["error"]

    def test_to_dict(self, store):
        config = store.configure("telegram", token="t1", settings={"key": "val"})
        d = config.to_dict()
        assert d["platform"] == "telegram"
        assert d["token_set"] is True
        assert d["settings"] == {"key": "val"}


class TestOutreachConfigAPI:
    def _make_client(self):
        from pinky_daemon.api import create_api
        from fastapi.testclient import TestClient

        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        app = create_api(max_sessions=10, default_working_dir="/tmp", db_path=path)
        return TestClient(app)

    def test_list_platforms_empty(self):
        client = self._make_client()
        resp = client.get("/outreach/platforms")
        assert resp.status_code == 200
        assert resp.json()["count"] == 0

    def test_configure_platform(self):
        client = self._make_client()
        resp = client.put("/outreach/platforms/telegram", json={"token": "bot123"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["platform"] == "telegram"
        assert data["token_set"] is True
        assert data["enabled"] is True

    def test_configure_bad_platform(self):
        client = self._make_client()
        resp = client.put("/outreach/platforms/whatsapp", json={"token": "x"})
        assert resp.status_code == 400

    def test_get_platform(self):
        client = self._make_client()
        client.put("/outreach/platforms/telegram", json={"token": "bot123"})
        resp = client.get("/outreach/platforms/telegram")
        assert resp.status_code == 200
        assert resp.json()["platform"] == "telegram"

    def test_get_platform_not_found(self):
        client = self._make_client()
        resp = client.get("/outreach/platforms/telegram")
        assert resp.status_code == 404

    def test_enable_disable_platform(self):
        client = self._make_client()
        client.put("/outreach/platforms/telegram", json={"token": "bot123"})

        resp = client.post("/outreach/platforms/telegram/disable")
        assert resp.status_code == 200
        assert resp.json()["disabled"] is True

        resp = client.post("/outreach/platforms/telegram/enable")
        assert resp.status_code == 200
        assert resp.json()["enabled"] is True

    def test_enable_not_configured(self):
        client = self._make_client()
        resp = client.post("/outreach/platforms/telegram/enable")
        assert resp.status_code == 404

    def test_delete_platform(self):
        client = self._make_client()
        client.put("/outreach/platforms/telegram", json={"token": "bot123"})
        resp = client.delete("/outreach/platforms/telegram")
        assert resp.status_code == 200
        assert resp.json()["deleted"] is True

    def test_delete_not_configured(self):
        client = self._make_client()
        resp = client.delete("/outreach/platforms/telegram")
        assert resp.status_code == 404

    def test_test_platform_not_configured(self):
        client = self._make_client()
        resp = client.post("/outreach/platforms/telegram/test")
        assert resp.status_code == 200
        assert resp.json()["success"] is False

    def test_list_platforms(self):
        client = self._make_client()
        client.put("/outreach/platforms/telegram", json={"token": "t1"})
        client.put("/outreach/platforms/discord", json={"token": "d1"})
        resp = client.get("/outreach/platforms")
        assert resp.status_code == 200
        assert resp.json()["count"] == 2

    def test_configure_with_settings(self):
        client = self._make_client()
        resp = client.put("/outreach/platforms/telegram", json={
            "token": "bot123",
            "settings": {"allowed_chats": ["123"], "poll_timeout": 60},
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["settings"]["allowed_chats"] == ["123"]
        assert data["settings"]["poll_timeout"] == 60

    def test_token_not_in_response(self):
        client = self._make_client()
        resp = client.put("/outreach/platforms/telegram", json={"token": "super-secret-token"})
        assert "super-secret-token" not in resp.text
        resp = client.get("/outreach/platforms/telegram")
        assert "super-secret-token" not in resp.text
