"""Tests for UI authentication and internal request signing."""

from __future__ import annotations

import os
import tempfile
import time

import pytest
from fastapi.testclient import TestClient

from pinky_daemon.api import create_api
from pinky_daemon.auth import (
    build_internal_auth_headers,
    create_session_cookie,
    hash_password,
    password_source,
    resolve_request_signing_secret,
    resolve_signing_secret,
    verify_internal_request,
    verify_password,
    verify_session_cookie,
)

# Tests in this module exercise the real auth flow (redirects, 401s,
# cookie issuance). The conftest auto-injects a valid session cookie
# into every TestClient instance for the rest of the suite; here we opt
# out so each test starts from a clean unauthenticated state and sets
# up its own auth via ``monkeypatch.setenv`` + ``/auth/setup`` as the
# specific scenario requires.
pytestmark = pytest.mark.real_auth


def test_password_hash_round_trip():
    stored = hash_password("hunter2")
    assert verify_password("hunter2", stored) is True
    assert verify_password("nope", stored) is False


def test_password_source_prefers_env():
    assert password_source("env-pass", "") == "env"
    assert password_source("", hash_password("stored")) == "settings"
    assert password_source("", "") == "unset"


def test_resolve_signing_secret_prefers_agent_key(monkeypatch):
    # #623 increment 2: per-agent key wins over the global secret.
    monkeypatch.setenv("PINKY_AGENT_KEY", "per-agent-key")
    monkeypatch.setenv("PINKY_SESSION_SECRET", "global-secret")
    assert resolve_signing_secret() == "per-agent-key"


def test_resolve_signing_secret_falls_back_to_global(monkeypatch):
    monkeypatch.delenv("PINKY_AGENT_KEY", raising=False)
    monkeypatch.setenv("PINKY_SESSION_SECRET", "global-secret")
    assert resolve_signing_secret() == "global-secret"


def test_resolve_signing_secret_blank_agent_key_falls_back(monkeypatch):
    # Whitespace-only agent key must not shadow the global secret.
    monkeypatch.setenv("PINKY_AGENT_KEY", "   ")
    monkeypatch.setenv("PINKY_SESSION_SECRET", "global-secret")
    assert resolve_signing_secret() == "global-secret"


def test_resolve_signing_secret_empty_when_neither_set(monkeypatch):
    monkeypatch.delenv("PINKY_AGENT_KEY", raising=False)
    monkeypatch.delenv("PINKY_SESSION_SECRET", raising=False)
    assert resolve_signing_secret() == ""


def test_resolve_request_signing_secret_prefers_resolver_key(monkeypatch):
    # #623 increment 3 (shared-SSE): the per-request resolver wins.
    monkeypatch.delenv("PINKY_AGENT_KEY", raising=False)
    monkeypatch.setenv("PINKY_SESSION_SECRET", "global-secret")
    got = resolve_request_signing_secret("alice", lambda name: f"{name}-key")
    assert got == "alice-key"


def test_resolve_request_signing_secret_none_falls_back(monkeypatch):
    # Resolver returns None (unknown agent) → fall back to env/global secret.
    monkeypatch.delenv("PINKY_AGENT_KEY", raising=False)
    monkeypatch.setenv("PINKY_SESSION_SECRET", "global-secret")
    assert resolve_request_signing_secret("ghost", lambda name: None) == "global-secret"


def test_resolve_request_signing_secret_resolver_raises_falls_back(monkeypatch):
    # A resolver hiccup must never raise into the request path.
    monkeypatch.delenv("PINKY_AGENT_KEY", raising=False)
    monkeypatch.setenv("PINKY_SESSION_SECRET", "global-secret")

    def boom(name):
        raise RuntimeError("db locked")

    assert resolve_request_signing_secret("alice", boom) == "global-secret"


def test_resolve_request_signing_secret_no_resolver_uses_env(monkeypatch):
    # Stdio mode: no resolver, PINKY_AGENT_KEY in env (increment 2) wins.
    monkeypatch.setenv("PINKY_AGENT_KEY", "env-agent-key")
    monkeypatch.setenv("PINKY_SESSION_SECRET", "global-secret")
    assert resolve_request_signing_secret("alice", None) == "env-agent-key"


def test_resolve_request_signing_secret_resolver_signature_dual_accepted(monkeypatch):
    # End-to-end: a shared-server request signed via the resolver's per-agent
    # key verifies on the daemon through the agent_key path (dual-accept), with
    # the resolved agent name bound in.
    monkeypatch.delenv("PINKY_AGENT_KEY", raising=False)
    monkeypatch.setenv("PINKY_SESSION_SECRET", "global-secret")
    secret = resolve_request_signing_secret("alice", lambda name: "alice-key")
    headers = build_internal_auth_headers(
        secret, agent_name="alice", method="POST", path="/agents/alice/status",
    )
    assert verify_internal_request(
        "global-secret",  # daemon global secret does NOT match the signature
        agent_name="alice",
        method="POST",
        path="/agents/alice/status",
        timestamp=headers["x-pinky-timestamp"],
        signature=headers["x-pinky-signature"],
        agent_key="alice-key",  # ...but the per-agent key does
    )


def test_per_agent_key_signs_request_verifiable_by_daemon(monkeypatch):
    # End-to-end: a process holding only its per-agent key produces headers
    # the daemon dual-accepts (agent_key path), with the name bound in.
    monkeypatch.setenv("PINKY_AGENT_KEY", "alice-key")
    monkeypatch.delenv("PINKY_SESSION_SECRET", raising=False)
    headers = build_internal_auth_headers(
        resolve_signing_secret(), agent_name="alice", method="POST", path="/agents/alice/status",
    )
    assert verify_internal_request(
        "global-secret",  # daemon's global secret — does NOT match the signature
        agent_name="alice",
        method="POST",
        path="/agents/alice/status",
        timestamp=headers["x-pinky-timestamp"],
        signature=headers["x-pinky-signature"],
        agent_key="alice-key",  # ...but the per-agent key does
    )


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


# ── Per-agent signing keys / dual-accept (#623) ─────────────────────────────


def _signed(signer_key: str, *, agent_name: str, method: str = "GET", path: str = "/tasks/next"):
    return build_internal_auth_headers(
        signer_key, agent_name=agent_name, method=method, path=path, timestamp=int(time.time())
    )


def test_internal_signature_per_agent_key_round_trip():
    """A request signed with the agent's per-agent key verifies when that key
    is supplied as agent_key (the #623 per-agent path)."""
    h = _signed("agent-A-key", agent_name="barsik")
    assert verify_internal_request(
        "global-secret",
        agent_name="barsik",
        method="GET",
        path="/tasks/next",
        timestamp=h["x-pinky-timestamp"],
        signature=h["x-pinky-signature"],
        agent_key="agent-A-key",
    ) is True


def test_internal_signature_global_secret_still_accepted_in_dual_mode():
    """Dual-accept migration: a request signed with the GLOBAL secret still
    verifies even when a (different) per-agent key is supplied as fallback."""
    h = _signed("global-secret", agent_name="barsik", method="POST", path="/agents")
    assert verify_internal_request(
        "global-secret",
        agent_name="barsik",
        method="POST",
        path="/agents",
        timestamp=h["x-pinky-timestamp"],
        signature=h["x-pinky-signature"],
        agent_key="some-other-agent-key",
    ) is True


def test_internal_signature_rejected_when_neither_key_matches():
    h = _signed("agent-A-key", agent_name="barsik")
    assert verify_internal_request(
        "global-secret",
        agent_name="barsik",
        method="GET",
        path="/tasks/next",
        timestamp=h["x-pinky-timestamp"],
        signature=h["x-pinky-signature"],
        agent_key="wrong-agent-key",
    ) is False


def test_internal_signature_name_bound_into_payload():
    """A signature minted for agent 'alice' must not verify when presented as
    'bob' — the agent name is part of the signed payload."""
    h = _signed("agent-A-key", agent_name="alice")
    assert verify_internal_request(
        "global-secret",
        agent_name="bob",
        method="GET",
        path="/tasks/next",
        timestamp=h["x-pinky-timestamp"],
        signature=h["x-pinky-signature"],
        agent_key="agent-A-key",
    ) is False


def test_internal_signature_requires_some_secret():
    """With neither a global secret nor a per-agent key, verification fails."""
    h = _signed("agent-A-key", agent_name="barsik")
    assert verify_internal_request(
        "",
        agent_name="barsik",
        method="GET",
        path="/tasks/next",
        timestamp=h["x-pinky-timestamp"],
        signature=h["x-pinky-signature"],
        agent_key=None,
    ) is False


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

    def test_unmapped_path_falls_through_to_fastapi_404(self, monkeypatch):
        """Per Murzik's PR #504 round-2 review: default-deny is scoped
        to ``_protected_api_prefixes`` so public-but-state-protected
        routes (notably the Google OAuth callback) reach their handlers
        without a session cookie. The cost is that unmapped paths now
        return FastAPI's 404 instead of a flat 401. Acceptable: there's
        no protected surface to leak, and 401-ing every unmapped path
        broke unrelated routes.
        """
        client, path = self._make_client(monkeypatch)
        resp = client.get("/this-path-does-not-exist")
        assert resp.status_code == 404
        os.unlink(path)

    def test_oauth_callback_unauth_reaches_route(self, monkeypatch):
        """Regression for Murzik's PR #504 round-2 catch: an earlier
        revision of this PR globally default-denied any unlisted route,
        which blocked ``/calendar/google/callback`` at the middleware
        before the route could run state validation. Real OAuth
        redirects from Google arrive cross-site without our session
        cookie (SameSite=strict), so middleware must let the request
        through to the route, which then validates the one-time state
        nonce.
        """
        client, path = self._make_client(monkeypatch)
        resp = client.get(
            "/calendar/google/callback",
            params={"code": "auth-code", "state": ""},
            follow_redirects=False,
        )
        # Route returns 400 for missing state; what we're pinning is
        # that middleware did NOT 401 the request before the route saw
        # it. (Route may also return 200/302/etc. if state is valid in
        # a different test setup — the invariant is "not 401".)
        assert resp.status_code != 401, (
            f"Default-deny regression: OAuth callback blocked at middleware "
            f"({resp.status_code} {resp.text[:200]})"
        )
        os.unlink(path)

    # ── Unconfigured PINKY_SESSION_SECRET: fail closed for protected
    #    surfaces, but keep public/bootstrap routes reachable so the
    #    operator can still navigate to the setup-flow message. ──
    #
    #    Per Murzik's review of PR #504: an earlier version of this PR
    #    added a global ``if not _session_secret(): call_next`` short-
    #    circuit at the top of the middleware, which made every
    #    protected surface (/settings, /dashboard, /agents, /tasks)
    #    reachable without auth in any unconfigured-secret deployment.
    #    That was strictly broader than pre-#497 behavior and a real
    #    fail-open regression. The current middleware leans on the
    #    existing per-path logic: with no secret,
    #    ``_has_valid_internal_auth`` and ``_has_valid_session`` both
    #    return False, so protected paths fall through to the redirect
    #    (HTML) or 401 (API) branches naturally. The tests below pin
    #    that behavior.

    def _make_unconfigured_client(self, monkeypatch):
        """Build a client with PINKY_SESSION_SECRET explicitly unset."""
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        monkeypatch.delenv("PINKY_SESSION_SECRET", raising=False)
        monkeypatch.delenv("PINKY_UI_PASSWORD", raising=False)
        app = create_api(max_sessions=10, default_working_dir="/tmp", db_path=path)
        return TestClient(app), path

    def test_unconfigured_secret_public_path_still_reachable(self, monkeypatch):
        """Public paths (``/api``, ``/login``, ``/setup``, ``/auth/*``,
        assets, hooks, Twilio webhooks) must stay reachable when no
        secret is configured — otherwise the operator can't navigate to
        the setup flow at all.
        """
        client, path = self._make_unconfigured_client(monkeypatch)
        resp = client.get("/api")
        assert resp.status_code == 200
        os.unlink(path)

    def test_unconfigured_secret_protected_html_redirects_to_setup(self, monkeypatch):
        """Protected HTML pages (``/settings``, ``/dashboard``, ...)
        must redirect, not return 200. Specifically to ``/setup``
        because ``_setup_required()`` is True when no password is
        configured.
        """
        client, path = self._make_unconfigured_client(monkeypatch)
        for protected_html in ("/settings", "/dashboard", "/chat", "/agents-ui"):
            resp = client.get(protected_html, follow_redirects=False)
            assert resp.status_code == 307, (
                f"Fail-open regression: {protected_html} should redirect "
                f"when PINKY_SESSION_SECRET is unset, got {resp.status_code}"
            )
            assert resp.headers["location"].startswith("/setup"), (
                f"Expected /setup redirect, got {resp.headers['location']}"
            )
        os.unlink(path)

    def test_unconfigured_secret_protected_api_returns_401(self, monkeypatch):
        """Protected API surfaces must fail closed with no secret, not
        return 200. Covers Murzik's exact reported reproduction set
        (``/agents``, ``/tasks``) plus a broader sweep.
        """
        client, path = self._make_unconfigured_client(monkeypatch)
        for protected_api in (
            "/agents",
            "/tasks",
            "/tasks/next",
            "/system/status",
            "/scheduler/list",
            "/broker/route",
            "/api/voice/calls",
        ):
            resp = client.get(protected_api)
            assert resp.status_code == 401, (
                f"Fail-open regression: {protected_api} should 401 when "
                f"PINKY_SESSION_SECRET is unset, got {resp.status_code}"
            )
        os.unlink(path)
