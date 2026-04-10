"""Tests for UI authentication and internal request signing."""

from __future__ import annotations

import os
import tempfile
import time

from fastapi.testclient import TestClient

from pinky_daemon.api import create_api
from pinky_daemon.auth import (
    build_internal_auth_headers,
    create_session_cookie,
    hash_password,
    password_source,
    verify_internal_request,
    verify_password,
    verify_session_cookie,
)


def test_password_hash_round_trip():
    stored = hash_password("hunter2")
    assert verify_password("hunter2", stored) is True
    assert verify_password("nope", stored) is False


def test_password_source_prefers_env():
    assert password_source("env-pass", "") == "env"
    assert password_source("", hash_password("stored")) == "settings"
    assert password_source("", "") == "unset"


def test_session_cookie_rejects_tampering():
    token = create_session_cookie("top-secret")
    assert verify_session_cookie("top-secret", token)["user"] == "admin"
    tampered = token[:-1] + ("a" if token[-1] != "a" else "b")
    assert verify_session_cookie("top-secret", tampered) is None


def test_internal_signature_round_trip():
    now = int(time.time())
    headers = build_internal_auth_headers(
        "top-secret",
        agent_name="barsik",
        method="GET",
        path="/tasks/next?agent_name=barsik",
        timestamp=now,
    )
    assert verify_internal_request(
        "top-secret",
        agent_name=headers["x-pinky-agent"],
        method="GET",
        path="/tasks/next",
        timestamp=headers["x-pinky-timestamp"],
        signature=headers["x-pinky-signature"],
    ) is True


class TestUIAuthAPI:
    def _make_client(self, monkeypatch):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        monkeypatch.setenv("PINKY_SESSION_SECRET", "test-session-secret")
        monkeypatch.delenv("PINKY_UI_PASSWORD", raising=False)
        app = create_api(max_sessions=10, default_working_dir="/tmp", db_path=path)
        return TestClient(app), path

    def test_html_redirects_to_setup_when_unconfigured(self, monkeypatch):
        client, path = self._make_client(monkeypatch)
        resp = client.get("/settings", follow_redirects=False)
        assert resp.status_code == 307
        assert resp.headers["location"].startswith("/setup")
        os.unlink(path)

    def test_setup_creates_password_and_cookie(self, monkeypatch):
        client, path = self._make_client(monkeypatch)
        resp = client.post("/auth/setup", json={"password": "hunter22", "next": "/settings"})
        assert resp.status_code == 200
        assert resp.json()["configured"] is True
        assert "pinky_session" in client.cookies

        status = client.get("/auth/status")
        assert status.status_code == 200
        assert status.json()["authenticated"] is True
        assert status.json()["password_source"] == "settings"
        os.unlink(path)

    def test_setup_rejects_short_password(self, monkeypatch):
        client, path = self._make_client(monkeypatch)
        resp = client.post("/auth/setup", json={"password": "short", "next": "/"})
        assert resp.status_code == 400
        assert "at least 8 characters" in resp.text
        os.unlink(path)

    def test_html_redirects_to_login_when_password_exists(self, monkeypatch):
        client, path = self._make_client(monkeypatch)
        client.post("/auth/setup", json={"password": "hunter22", "next": "/"})

        second_client = TestClient(client.app)
        resp = second_client.get("/settings", follow_redirects=False)
        assert resp.status_code == 307
        assert resp.headers["location"].startswith("/login")
        os.unlink(path)

    def test_browser_api_requires_auth(self, monkeypatch):
        client, path = self._make_client(monkeypatch)
        client.post("/auth/setup", json={"password": "hunter22", "next": "/"})

        second_client = TestClient(client.app)
        resp = second_client.get("/agents", headers={"Origin": "http://localhost:8888"})
        assert resp.status_code == 401
        assert resp.json()["authenticated"] is False
        assert resp.json()["setup_required"] is False
        os.unlink(path)

    def test_public_api_stays_open(self, monkeypatch):
        client, path = self._make_client(monkeypatch)
        resp = client.get("/api")
        assert resp.status_code == 200
        os.unlink(path)

    def test_login_and_logout(self, monkeypatch):
        client, path = self._make_client(monkeypatch)
        client.post("/auth/setup", json={"password": "hunter22", "next": "/"})

        second_client = TestClient(client.app)
        login = second_client.post("/auth/login", json={"password": "hunter22", "next": "/fleet"})
        assert login.status_code == 200
        assert login.json()["authenticated"] is True
        assert "pinky_session" in second_client.cookies

        logout = second_client.post("/auth/logout")
        assert logout.status_code == 200
        assert second_client.cookies.get("pinky_session") is None
        os.unlink(path)

    def test_env_override_disables_password_updates(self, monkeypatch):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        monkeypatch.setenv("PINKY_SESSION_SECRET", "test-session-secret")
        monkeypatch.setenv("PINKY_UI_PASSWORD", "env-pass")
        app = create_api(max_sessions=10, default_working_dir="/tmp", db_path=path)
        client = TestClient(app)

        login = client.post("/auth/login", json={"password": "env-pass", "next": "/"})
        assert login.status_code == 200

        update = client.put(
            "/auth/password",
            headers={"Origin": "http://localhost:8888"},
            json={"password": "new-pass"},
        )
        assert update.status_code == 409
        os.unlink(path)

    def test_password_update_requires_session_and_min_length(self, monkeypatch):
        client, path = self._make_client(monkeypatch)
        client.post("/auth/setup", json={"password": "hunter22", "next": "/"})

        unauthenticated = TestClient(client.app)
        rejected = unauthenticated.put(
            "/auth/password",
            headers={"Origin": "http://localhost:8888"},
            json={"password": "long-enough"},
        )
        assert rejected.status_code == 401

        short = client.put(
            "/auth/password",
            headers={"Origin": "http://localhost:8888"},
            json={"password": "short"},
        )
        assert short.status_code == 400
        assert "at least 8 characters" in short.text
        os.unlink(path)

    def test_internal_headers_bypass_browser_auth(self, monkeypatch):
        client, path = self._make_client(monkeypatch)
        headers = {
            "Origin": "http://localhost:8888",
            **build_internal_auth_headers(
                "test-session-secret",
                agent_name="test-agent",
                method="GET",
                path="/agents",
            ),
        }
        resp = client.get("/agents", headers=headers)
        assert resp.status_code == 200
        os.unlink(path)
