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


class TestAuthMiddlewareDefaultDeny:
    """Regression tests for issue #497 — middleware fall-through allowed
    unauthenticated non-browser requests onto every /agents/*, /tasks/*,
    /system/* and other ``_protected_api_prefixes`` route.

    The fix is to default-deny: any request that hasn't been granted access
    by one of the four legitimate gates (public path, HMAC-signed internal
    request, session cookie, browser-shape API request) returns 401.

    Murzik's review enumerated four cases this PR must pin:
      1. HMAC-valid → 200 (call_next)
      2. Browser session cookie → 200 (call_next)
      3. Plain curl JSON on /agents/* without HMAC/session → 401
      4. Public/voice/websocket/HTML-redirect carve-outs unchanged
    """

    def _make_client(self, monkeypatch):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        monkeypatch.setenv("PINKY_SESSION_SECRET", "test-session-secret")
        monkeypatch.delenv("PINKY_UI_PASSWORD", raising=False)
        app = create_api(max_sessions=10, default_working_dir="/tmp", db_path=path)
        return TestClient(app), path

    # ── Case 3: the bug. Plain curl-style hit on a protected API surface ──

    def test_unauth_plain_request_on_agents_hook_returns_401(self, monkeypatch):
        """Reproduces issue #497: pre-fix this returned 200 because the
        middleware fell through to call_next; post-fix it must be 401.
        """
        client, path = self._make_client(monkeypatch)
        # Non-browser shape: no Origin header, no cookie, no HMAC headers,
        # Content-Type set as a generic API client would.
        resp = client.post(
            "/agents/dymok/transport/wake",
            headers={"Content-Type": "application/json"},
            json={"event": "stop_hook_summary"},
        )
        assert resp.status_code == 401, (
            f"Default-deny regression: /agents/* hook should require auth, got {resp.status_code}"
        )
        assert resp.json() == {"detail": "Unauthorized"}
        os.unlink(path)

    def test_unauth_plain_request_on_arbitrary_protected_api_returns_401(self, monkeypatch):
        """The bug isn't /agents-specific — every prefix in
        ``_protected_api_prefixes`` was leaking. Spot-check a few.
        """
        client, path = self._make_client(monkeypatch)
        for protected_path in (
            "/tasks/next",
            "/system/status",
            "/scheduler/list",
            "/broker/route",
        ):
            resp = client.get(protected_path)
            assert resp.status_code == 401, (
                f"Default-deny regression: {protected_path} should require auth, "
                f"got {resp.status_code}"
            )
        os.unlink(path)

    # ── Case 1: HMAC carve-out preserved ──

    def test_hmac_signed_request_on_agents_hook_passes(self, monkeypatch):
        """HMAC-signed internal requests (hook scripts, agent-to-daemon)
        must still pass through to the route. /agents/* hook surfaces are
        the primary HMAC consumers — explicitly pin one.
        """
        client, path = self._make_client(monkeypatch)
        headers = build_internal_auth_headers(
            "test-session-secret",
            agent_name="barsik",
            method="POST",
            path="/agents/barsik/working-status",
        )
        resp = client.post(
            "/agents/barsik/working-status",
            headers={**headers, "Content-Type": "application/json"},
            json={"status": "busy"},
        )
        # Either succeeds or fails at the route layer — but NOT 401 from
        # the middleware. The route may return 404/400 depending on the
        # actual handler shape; what we're pinning is that middleware
        # didn't block.
        assert resp.status_code != 401, (
            f"HMAC carve-out regression: signed request blocked at middleware "
            f"({resp.status_code} {resp.json() if resp.headers.get('content-type','').startswith('application/json') else resp.text})"
        )
        os.unlink(path)

    # ── Case 2: session carve-out preserved ──

    def test_session_cookie_on_agents_hook_passes(self, monkeypatch):
        """A logged-in browser session passes through on hook endpoints
        too — session cookie was pulled up to be a general gate so the
        same cookie that lets the user load /dashboard also lets them
        hit /agents/*.
        """
        client, path = self._make_client(monkeypatch)
        client.post("/auth/setup", json={"password": "hunter22", "next": "/"})
        # client now has the pinky_session cookie set
        resp = client.post(
            "/agents/dymok/transport/wake",
            json={"event": "stop_hook_summary"},
        )
        # Same as above — must not be 401 from middleware. Route may
        # return its own status.
        assert resp.status_code != 401, (
            f"Session carve-out regression: session-authed request blocked at "
            f"middleware ({resp.status_code})"
        )
        os.unlink(path)

    # ── Case 4: public/voice/websocket/HTML-redirect carve-outs unchanged ──

    def test_public_api_path_remains_public(self, monkeypatch):
        """/api is in ``_public_exact_paths`` — must still return 200
        without any auth, default-deny notwithstanding.
        """
        client, path = self._make_client(monkeypatch)
        resp = client.get("/api")
        assert resp.status_code == 200
        os.unlink(path)

    def test_twilio_webhook_prefix_remains_public(self, monkeypatch):
        """/api/voice/twiml/ is in ``_public_prefixes`` (Twilio webhooks
        authenticate via X-Twilio-Signature, not session). Must reach the
        route layer regardless of session/HMAC state. The route may 404
        for an unknown sub-path, but it must NOT be middleware-401.
        """
        client, path = self._make_client(monkeypatch)
        resp = client.post("/api/voice/twiml/some-id")
        assert resp.status_code != 401, (
            f"Public-prefix carve-out regression: Twilio webhook prefix "
            f"blocked at middleware ({resp.status_code})"
        )
        os.unlink(path)

    def test_voice_api_still_requires_auth_for_curl_callers(self, monkeypatch):
        """Voice API (non-Twilio paths under /api/voice) requires auth
        from curl/non-browser callers. Previously this had its own
        explicit branch in the middleware; under default-deny it's
        covered by the catch-all 401. The behavior must be unchanged.
        """
        client, path = self._make_client(monkeypatch)
        resp = client.get("/api/voice/calls")  # not in _public_prefixes
        assert resp.status_code == 401
        os.unlink(path)

    def test_html_redirects_still_307(self, monkeypatch):
        """Protected HTML pages still 307-redirect to /setup or /login —
        the redirect is special-cased BEFORE default-deny because the
        browser needs the redirect for a clean login flow.
        """
        client, path = self._make_client(monkeypatch)
        resp = client.get("/dashboard", follow_redirects=False)
        assert resp.status_code == 307
        assert resp.headers["location"].startswith(("/login", "/setup"))
        os.unlink(path)

    def test_unmapped_path_returns_401_documented(self, monkeypatch):
        """Documented design choice: unmapped paths (typos like
        /random/url) return 401 from the default-deny, not 404 from
        FastAPI's route layer. Acceptable trade — security defaults to
        deny, and we don't reveal path existence to unauthenticated
        probes.
        """
        client, path = self._make_client(monkeypatch)
        resp = client.get("/this-path-does-not-exist")
        assert resp.status_code == 401
        os.unlink(path)

    # ── Bootstrap carve-out: no SESSION_SECRET ⇒ auth disabled ──

    def test_no_session_secret_bypasses_auth(self, monkeypatch):
        """If PINKY_SESSION_SECRET is unset, the daemon can't validate
        anything (HMAC + session cookie both require the secret), so
        authentication is meaningless and the middleware lets requests
        through. Matches pre-#497 behavior for unconfigured deployments
        and preserves the documented bootstrap lifecycle.

        Production deployments ALWAYS set the secret, so this branch is
        bootstrap-only in practice. The test fixture here exists to pin
        the carve-out exists and doesn't drift to default-deny silently.
        """
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        monkeypatch.delenv("PINKY_SESSION_SECRET", raising=False)
        monkeypatch.delenv("PINKY_UI_PASSWORD", raising=False)
        app = create_api(max_sessions=10, default_working_dir="/tmp", db_path=path)
        client = TestClient(app)
        # Hit a protected path without any auth headers — must NOT be
        # 401 from middleware because auth is disabled when there's no
        # secret to validate against.
        resp = client.get("/tasks")
        assert resp.status_code != 401, (
            f"Bootstrap regression: middleware blocked request when "
            f"PINKY_SESSION_SECRET is unset, got {resp.status_code}"
        )
        os.unlink(path)
