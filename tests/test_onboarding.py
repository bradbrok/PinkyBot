"""Tests for onboarding wizard endpoints."""

from __future__ import annotations

import os
import tempfile

from fastapi.testclient import TestClient

from pinky_daemon.api import create_api


class TestOnboarding:
    def _make_client(self, monkeypatch):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        monkeypatch.setenv("PINKY_SESSION_SECRET", "test-session-secret")
        monkeypatch.delenv("PINKY_UI_PASSWORD", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        app = create_api(max_sessions=10, default_working_dir="/tmp", db_path=path)
        client = TestClient(app)
        # Set up password so endpoints are accessible
        client.post("/auth/setup", json={"password": "hunter22", "next": "/"})
        return client, path

    def test_onboarding_status_fresh_install(self, monkeypatch):
        client, path = self._make_client(monkeypatch)
        resp = client.get("/system/onboarding-status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["onboarding_completed"] is False
        assert data["has_agents"] is False
        assert data["agent_count"] == 0
        os.unlink(path)

    def test_onboarding_complete_and_status(self, monkeypatch):
        client, path = self._make_client(monkeypatch)
        resp = client.post("/system/onboarding-complete")
        assert resp.status_code == 200
        assert resp.json()["completed"] is True

        status = client.get("/system/onboarding-status")
        assert status.json()["onboarding_completed"] is True
        os.unlink(path)

    def test_onboarding_reset(self, monkeypatch):
        client, path = self._make_client(monkeypatch)
        client.post("/system/onboarding-complete")
        assert client.get("/system/onboarding-status").json()["onboarding_completed"] is True

        resp = client.post("/system/onboarding-reset")
        assert resp.status_code == 200
        assert resp.json()["reset"] is True

        assert client.get("/system/onboarding-status").json()["onboarding_completed"] is False
        os.unlink(path)

    def test_onboarding_status_with_agent(self, monkeypatch):
        client, path = self._make_client(monkeypatch)
        client.post("/agents", json={"name": "test-agent", "soul": "Test soul"})
        status = client.get("/system/onboarding-status")
        assert status.json()["has_agents"] is True
        assert status.json()["agent_count"] == 1
        os.unlink(path)
