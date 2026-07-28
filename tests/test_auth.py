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
    make_db_signing_key_resolver,
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


def test_resolve_request_signing_secret_shared_fallback_ignores_process_env_key(monkeypatch):
    # Pre-cutover hardening (#623): with a resolver present (shared multi-agent
    # process), a resolver-None must fall back to the GLOBAL secret only — never
    # a stray process-level PINKY_AGENT_KEY, which would be one wrong identity
    # for every agent.
    monkeypatch.setenv("PINKY_AGENT_KEY", "stray-process-key")
    monkeypatch.setenv("PINKY_SESSION_SECRET", "global-secret")
    assert resolve_request_signing_secret("alice", lambda name: None) == "global-secret"

    def boom(name):
        raise RuntimeError("db locked")

    assert resolve_request_signing_secret("alice", boom) == "global-secret"


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


# ── #641: request-time signing-key resolver (stale-env-key lockout fix) ──────


def _seed_signing_key(db_path: str, agent_name: str, key: str) -> None:
    """Create the agent_signing_keys table + row, mirroring AgentRegistry."""
    import sqlite3
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS agent_signing_keys "
        "(agent_name TEXT PRIMARY KEY, signing_key TEXT NOT NULL, created_at REAL)"
    )
    conn.execute(
        "INSERT OR REPLACE INTO agent_signing_keys (agent_name, signing_key, created_at) "
        "VALUES (?, ?, 0)",
        (agent_name, key),
    )
    conn.commit()
    conn.close()


def test_db_resolver_returns_current_key(tmp_path):
    db = str(tmp_path / "agents.db")
    _seed_signing_key(db, "alice", "current-key")
    resolver = make_db_signing_key_resolver(db)
    assert resolver("alice") == "current-key"


def test_db_resolver_unknown_agent_returns_none(tmp_path):
    db = str(tmp_path / "agents.db")
    _seed_signing_key(db, "alice", "current-key")
    resolver = make_db_signing_key_resolver(db)
    assert resolver("ghost") is None


def test_db_resolver_rejects_malformed_agent_name(tmp_path):
    # @murzik #644 hardening: a name outside the agent allowlist never reaches
    # the query — resolves to None (→ global fallback), staying inside the
    # agent-name trust boundary.
    db = str(tmp_path / "agents.db")
    _seed_signing_key(db, "alice", "current-key")
    resolver = make_db_signing_key_resolver(db)
    for bad in ("../../etc/passwd", "Alice", "a b", "a'; DROP TABLE x;--", "-leading"):
        assert resolver(bad) is None
    assert resolver("alice") == "current-key"  # valid name still works


def test_db_resolver_missing_db_fails_soft(tmp_path):
    # Missing file / unreadable DB must degrade to None (→ global-secret
    # fallback), never raise into the request path.
    resolver = make_db_signing_key_resolver(str(tmp_path / "does-not-exist.db"))
    assert resolver("alice") is None
    assert make_db_signing_key_resolver("")("alice") is None


def test_db_resolver_sees_key_rotation(tmp_path):
    # Each call reads fresh — a key changed in the DB after the resolver was
    # built is picked up on the very next request (the #641 property).
    db = str(tmp_path / "agents.db")
    _seed_signing_key(db, "alice", "old-key")
    resolver = make_db_signing_key_resolver(db)
    assert resolver("alice") == "old-key"
    _seed_signing_key(db, "alice", "new-key")
    assert resolver("alice") == "new-key"


def test_641_resolver_signs_with_current_key_ignoring_stale_env(tmp_path, monkeypatch):
    """The core #641 scenario: a stdio server's env carries a STALE
    PINKY_AGENT_KEY (baked at spawn), but the DB now holds a different current
    key. With the DB resolver wired, signing uses the CURRENT key — so the
    daemon (verifying against the current agent_key) accepts it, and the stale
    key is dead. Pre-fix, signing preferred the stale env key → 401."""
    db = str(tmp_path / "agents.db")
    _seed_signing_key(db, "alice", "current-key")
    monkeypatch.setenv("PINKY_AGENT_KEY", "STALE-baked-key")
    monkeypatch.setenv("PINKY_SESSION_SECRET", "global-secret")

    resolver = make_db_signing_key_resolver(db)
    secret = resolve_request_signing_secret("alice", resolver)
    assert secret == "current-key"  # NOT the stale env key

    headers = build_internal_auth_headers(
        secret, agent_name="alice", method="POST", path="/agents/alice/status",
    )
    # Daemon verifies against the CURRENT per-agent key → accepted.
    assert verify_internal_request(
        "global-secret", agent_name="alice", method="POST",
        path="/agents/alice/status",
        timestamp=headers["x-pinky-timestamp"],
        signature=headers["x-pinky-signature"],
        agent_key="current-key",
    )
    # And the same signature does NOT verify against the stale key — proving
    # we did not sign with it.
    assert not verify_internal_request(
        "global-secret", agent_name="alice", method="POST",
        path="/agents/alice/status",
        timestamp=headers["x-pinky-timestamp"],
        signature=headers["x-pinky-signature"],
        agent_key="STALE-baked-key", allow_global_secret=False,
    )


def test_641_resolver_none_falls_back_to_global_not_stale_env(tmp_path, monkeypatch):
    """If the resolver can't find a key (unknown agent / DB error), signing
    falls back to the GLOBAL secret — never the stale process env key."""
    db = str(tmp_path / "agents.db")
    _seed_signing_key(db, "alice", "current-key")
    monkeypatch.setenv("PINKY_AGENT_KEY", "STALE-baked-key")
    monkeypatch.setenv("PINKY_SESSION_SECRET", "global-secret")
    resolver = make_db_signing_key_resolver(db)
    # 'ghost' has no DB key → resolver None → global secret, not stale env.
    assert resolve_request_signing_secret("ghost", resolver) == "global-secret"


def test_641_isolated_invariant_preserved(tmp_path, monkeypatch):
    """#640 invariant unchanged: a global-secret signature is rejected when
    allow_global_secret=False (isolated caller) even if the signer fell back to
    it. The fix changes only what the signer presents, never verifier policy."""
    monkeypatch.setenv("PINKY_SESSION_SECRET", "global-secret")
    # Signer falls back to global (no per-agent key resolvable).
    secret = resolve_request_signing_secret("iso", lambda name: None)
    assert secret == "global-secret"
    headers = build_internal_auth_headers(
        secret, agent_name="iso", method="POST", path="/agents/iso/status",
    )
    # Isolated verification (allow_global_secret=False) with no per-agent key
    # → fail closed, regardless of the global-secret signature.
    assert not verify_internal_request(
        "global-secret", agent_name="iso", method="POST",
        path="/agents/iso/status",
        timestamp=headers["x-pinky-timestamp"],
        signature=headers["x-pinky-signature"],
        agent_key=None, allow_global_secret=False,
    )


def test_session_cookie_rejects_tampering():
    token = create_session_cookie("top-secret")
    assert verify_session_cookie("top-secret", token)["user"] == "admin"
    tampered = token[:-1] + ("a" if token[-1] != "a" else "b")
    assert verify_session_cookie("top-secret", tampered) is None


def test_session_cookie_non_ascii_denied_not_raised():
    # Starlette decodes cookies as latin-1, so bytes >= 0x80 reach the verifier
    # as non-ASCII str. That must be a clean deny, never a TypeError/
    # UnicodeEncodeError (which the middleware would surface as a 500).
    assert verify_session_cookie("top-secret", "p\xe9yload.sig") is None
    assert verify_session_cookie("top-secret", "payload.s\xffg") is None


def test_internal_signature_non_ascii_denied_not_raised():
    # hmac.compare_digest raises TypeError on non-ASCII str; a malformed
    # attacker-controlled signature header must fail auth, not raise.
    assert verify_internal_request(
        "top-secret",
        agent_name="barsik",
        method="GET",
        path="/tasks/next",
        timestamp=str(int(time.time())),
        signature="\xfe\xff-bogus",
    ) is False


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


# ── #149 phase-3 inc2: isolated callers may not use the global secret ────────


def test_verify_rejects_global_secret_signature_when_disallowed():
    """An isolated caller signs with the global secret: accepted in dual mode,
    REJECTED once allow_global_secret=False (the daemon-side cutover)."""
    h = _signed("global-secret", agent_name="sasha", method="POST", path="/agents/sasha/status")
    common = dict(
        agent_name="sasha",
        method="POST",
        path="/agents/sasha/status",
        timestamp=h["x-pinky-timestamp"],
        signature=h["x-pinky-signature"],
    )
    # Baseline dual-accept still honors the global secret.
    assert verify_internal_request("global-secret", agent_key=None, **common) is True
    # Isolated cutover: the global-secret signature no longer authenticates.
    assert (
        verify_internal_request(
            "global-secret", agent_key=None, allow_global_secret=False, **common
        )
        is False
    )
    # Even with a per-agent key supplied, a GLOBAL-secret signature is rejected.
    assert (
        verify_internal_request(
            "global-secret", agent_key="sasha-key", allow_global_secret=False, **common
        )
        is False
    )


def test_verify_accepts_per_agent_key_when_global_disallowed():
    """With the global secret disallowed, the agent's OWN per-agent-key
    signature still authenticates — isolated agents keep working via their key."""
    h = _signed("sasha-key", agent_name="sasha", method="POST", path="/agents/sasha/status")
    assert (
        verify_internal_request(
            "global-secret",
            agent_name="sasha",
            method="POST",
            path="/agents/sasha/status",
            timestamp=h["x-pinky-timestamp"],
            signature=h["x-pinky-signature"],
            agent_key="sasha-key",
            allow_global_secret=False,
        )
        is True
    )


def test_verify_no_credential_when_global_disallowed_and_no_key():
    """Fail closed: a keyless isolated caller (global disallowed, agent_key
    None) cannot authenticate even with an otherwise-valid global signature."""
    h = _signed("global-secret", agent_name="sasha")
    assert (
        verify_internal_request(
            "global-secret",
            agent_name="sasha",
            method="GET",
            path="/tasks/next",
            timestamp=h["x-pinky-timestamp"],
            signature=h["x-pinky-signature"],
            agent_key=None,
            allow_global_secret=False,
        )
        is False
    )


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
        # #640: global-secret auth now requires a proven-registered, non-isolated
        # agent name — register the signer (production signers always are).
        client.app.state.agents.register(
            "test-agent", model="opus", working_dir="/tmp/test-agent"
        )
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
        # #640: global-secret auth now requires a proven-registered, non-isolated
        # agent name — register the signer (production signers always are).
        client.app.state.agents.register(
            "barsik", model="opus", working_dir="/tmp/barsik"
        )
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


class TestAgentIsolationScoping:
    """#149: an authenticated *isolated* agent may only act on its OWN
    resources; cross-agent access is denied 403 even with a valid signature.
    Two enforcement layers are covered here:

      * PATH-target surfaces (middleware): /agents/{other}/*, /autonomy/{other}/*
      * BODY-actor surfaces (handler): /broker/* where the acting agent is named
        in the request body (forgeable — the signature does NOT cover the body).

    Full-trust (non-isolated) agents are unaffected by either layer.
    """

    _MESH_HINT = (
        'cross-fleet messaging is available via mesh_remote_send(target="agent@fleet") '
        "— requires your mesh_outbound_allowlist to include the target"
    )

    def _make_client_with_agents(self, monkeypatch, tmp_path):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        monkeypatch.setenv("PINKY_SESSION_SECRET", "test-session-secret")
        monkeypatch.delenv("PINKY_UI_PASSWORD", raising=False)
        app = create_api(max_sessions=10, default_working_dir=str(tmp_path), db_path=path)
        # Seed via the app's OWN registry instance (app.state.agents) so the
        # middleware's isolation lookup sees the same rows.
        reg = app.state.agents
        reg.register("tenant", model="opus", isolated=True,
                     working_dir=str(tmp_path / "tenant"))
        reg.register("other", model="opus",
                     working_dir=str(tmp_path / "other"))
        return TestClient(app), path

    def _signing_key(self, client, agent_name):
        # #149 phase-3 inc2: isolated callers can no longer authenticate with
        # the global secret, so sign as each agent with its OWN per-agent key
        # (provisioned on register, #623) — the realistic post-cutover path.
        # Falls back to the global secret only if a key is somehow absent.
        return client.app.state.agents.get_signing_key(agent_name) or "test-session-secret"

    def _signed_get(self, client, agent_name, target_path):
        headers = build_internal_auth_headers(
            self._signing_key(client, agent_name), agent_name=agent_name,
            method="GET", path=target_path,
        )
        return client.get(target_path, headers=headers)

    def _signed_post(self, client, agent_name, target_path, body):
        # The signature binds the CALLER name + method + path, NOT the body —
        # so ``body`` can name any agent (the impersonation vector the
        # body-actor guard defends against).
        headers = build_internal_auth_headers(
            self._signing_key(client, agent_name), agent_name=agent_name,
            method="POST", path=target_path,
        )
        return client.post(target_path, json=body, headers=headers)

    def test_isolated_agent_denied_cross_agent_access(self, monkeypatch, tmp_path):
        client, path = self._make_client_with_agents(monkeypatch, tmp_path)
        resp = self._signed_get(client, "tenant", "/agents/other")
        assert resp.status_code == 403
        assert resp.json()["error"] == "isolated agent may only access its own resources"
        assert resp.json()["hint"] == self._MESH_HINT
        os.unlink(path)

    def test_isolated_agent_allowed_self_access(self, monkeypatch, tmp_path):
        client, path = self._make_client_with_agents(monkeypatch, tmp_path)
        resp = self._signed_get(client, "tenant", "/agents/tenant")
        # Acting on self → isolation guard allows; route handles it (not 403).
        assert resp.status_code != 403
        os.unlink(path)

    def test_non_isolated_agent_not_restricted(self, monkeypatch, tmp_path):
        client, path = self._make_client_with_agents(monkeypatch, tmp_path)
        # 'other' is full-trust — cross-agent access is NOT isolation-denied.
        resp = self._signed_get(client, "other", "/agents/tenant")
        assert resp.status_code != 403
        os.unlink(path)

    # ── #637: isolated teammates in the SAME group may message each other ──

    def _make_client_grouped(self, monkeypatch, tmp_path):
        """Two isolated teammates (chekov, geordi) share the 'support' group;
        a third isolated agent (sasha) sits in a different group."""
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        monkeypatch.setenv("PINKY_SESSION_SECRET", "test-session-secret")
        monkeypatch.delenv("PINKY_UI_PASSWORD", raising=False)
        app = create_api(max_sessions=10, default_working_dir=str(tmp_path), db_path=path)
        reg = app.state.agents
        reg.register("chekov", model="opus", isolated=True, groups=["support"],
                     working_dir=str(tmp_path / "chekov"))
        reg.register("geordi", model="opus", isolated=True, groups=["support"],
                     working_dir=str(tmp_path / "geordi"))
        reg.register("sasha", model="opus", isolated=True, groups=["sales"],
                     working_dir=str(tmp_path / "sasha"))
        return TestClient(app), path

    def test_isolated_same_group_peer_message_allowed(self, monkeypatch, tmp_path):
        # geordi → chekov /message, shared group → isolation guard lets it
        # through (handler runs; the guard's own signal is "not 403").
        client, path = self._make_client_grouped(monkeypatch, tmp_path)
        resp = self._signed_post(
            client, "geordi", "/agents/chekov/message",
            {"from_agent": "geordi", "message": "calendar for site X?"},
        )
        assert resp.status_code != 403, resp.text
        os.unlink(path)

    def test_isolated_no_shared_group_peer_message_denied(self, monkeypatch, tmp_path):
        # geordi (support) → sasha (sales) /message: no shared group → 403.
        client, path = self._make_client_grouped(monkeypatch, tmp_path)
        resp = self._signed_post(
            client, "geordi", "/agents/sasha/message",
            {"from_agent": "geordi", "message": "hi"},
        )
        assert resp.status_code == 403
        assert resp.json()["error"] == "isolated agent may only access its own resources"
        assert "hint" not in resp.json()
        os.unlink(path)

    def test_isolated_unknown_message_target_gets_cross_fleet_hint(
        self, monkeypatch, tmp_path
    ):
        client, path = self._make_client_grouped(monkeypatch, tmp_path)
        resp = self._signed_post(
            client,
            "geordi",
            "/agents/onesie@tod/message",
            {"from_agent": "geordi", "message": "hi"},
        )
        assert resp.status_code == 403
        assert resp.json()["error"] == "isolated agent may only access its own resources"
        assert resp.json()["hint"] == self._MESH_HINT
        os.unlink(path)

    def test_isolated_same_group_non_message_path_still_denied(self, monkeypatch, tmp_path):
        # Shared group does NOT open admin/read surfaces — only /message.
        client, path = self._make_client_grouped(monkeypatch, tmp_path)
        resp = self._signed_get(client, "geordi", "/agents/chekov")
        assert resp.status_code == 403
        assert "isolated" in resp.json().get("error", "").lower()
        os.unlink(path)

    def test_isolated_same_group_message_spoofed_sender_denied(self, monkeypatch, tmp_path):
        # geordi may reach chekov/message, but naming a THIRD agent as sender
        # is blocked by the body-actor guard (act only as yourself).
        client, path = self._make_client_grouped(monkeypatch, tmp_path)
        resp = self._signed_post(
            client, "geordi", "/agents/chekov/message",
            {"from_agent": "sasha", "message": "spoofed"},
        )
        assert resp.status_code == 403
        # handler-level guard raises HTTPException → {"detail": ...} (the
        # middleware deny uses {"error": ...}); accept either shape.
        body = resp.json()
        assert "isolated" in (body.get("detail") or body.get("error") or "").lower()
        assert "hint" not in body
        os.unlink(path)

    @pytest.mark.parametrize("from_agent", ["", "   "])
    def test_isolated_same_group_message_blank_sender_denied(
        self, monkeypatch, tmp_path, from_agent
    ):
        # Empty/blank is not self. The generic body-actor guard must deny it,
        # never treat a falsey actor as an operator/no-op identity.
        client, path = self._make_client_grouped(monkeypatch, tmp_path)
        resp = self._signed_post(
            client, "geordi", "/agents/chekov/message",
            {"from_agent": from_agent, "message": "anonymous spoof"},
        )
        assert resp.status_code == 403
        assert "isolated" in resp.json().get("detail", "").lower()
        os.unlink(path)

    # ── PATH-target: /autonomy/{name}/* (middleware) ──────────────────────

    def test_isolated_agent_denied_cross_agent_autonomy(self, monkeypatch, tmp_path):
        client, path = self._make_client_with_agents(monkeypatch, tmp_path)
        resp = self._signed_post(client, "tenant", "/autonomy/other/start", {})
        assert resp.status_code == 403
        assert "isolated" in resp.json().get("error", "").lower()
        os.unlink(path)

    def test_isolated_agent_allowed_self_autonomy(self, monkeypatch, tmp_path):
        client, path = self._make_client_with_agents(monkeypatch, tmp_path)
        resp = self._signed_post(client, "tenant", "/autonomy/tenant/start", {})
        # Acting on self → not isolation-denied (handler may still 4xx/5xx).
        assert resp.status_code != 403
        os.unlink(path)

    def test_isolated_agent_allowed_targetless_autonomy_status(self, monkeypatch, tmp_path):
        client, path = self._make_client_with_agents(monkeypatch, tmp_path)
        # /autonomy/status names no agent → not an isolation-relevant surface.
        resp = self._signed_get(client, "tenant", "/autonomy/status")
        assert resp.status_code != 403
        os.unlink(path)

    # ── BODY-actor: /broker/* (handler) ───────────────────────────────────

    def test_isolated_agent_denied_broker_send_as_other(self, monkeypatch, tmp_path):
        client, path = self._make_client_with_agents(monkeypatch, tmp_path)
        # Valid signature as 'tenant', but body forges agent_name='other'.
        resp = self._signed_post(
            client, "tenant", "/broker/send",
            {"agent_name": "other", "chat_id": "123", "content": "hi"},
        )
        assert resp.status_code == 403
        assert resp.json()["detail"] == "isolated agent may only act as itself"
        assert resp.json()["hint"] == self._MESH_HINT
        os.unlink(path)

    def test_isolated_agent_allowed_broker_send_as_self(self, monkeypatch, tmp_path):
        client, path = self._make_client_with_agents(monkeypatch, tmp_path)
        # body agent_name == caller → guard is a no-op; handler runs (may 5xx
        # since no real platform is wired up, but it is NOT isolation-denied).
        resp = self._signed_post(
            client, "tenant", "/broker/send",
            {"agent_name": "tenant", "chat_id": "123", "content": "hi"},
        )
        assert resp.status_code != 403
        os.unlink(path)

    def test_non_isolated_agent_broker_send_as_other_not_denied(self, monkeypatch, tmp_path):
        client, path = self._make_client_with_agents(monkeypatch, tmp_path)
        # 'other' is full-trust — the body-actor guard does not restrict it.
        resp = self._signed_post(
            client, "other", "/broker/send",
            {"agent_name": "tenant", "chat_id": "123", "content": "hi"},
        )
        assert resp.status_code != 403
        os.unlink(path)

    # ── ADMIN collection route: POST /agents (no path target) ─────────────
    # Murzik #635 catch: the agent-mint/upsert route has no agent name in the
    # path, so the middleware can't see it. An isolated tenant must not reach it.

    def test_isolated_agent_denied_register_new_agent(self, monkeypatch, tmp_path):
        client, path = self._make_client_with_agents(monkeypatch, tmp_path)
        # Isolated 'tenant' signs with its own identity and tries to MINT a new
        # full-trust agent via the collection route.
        resp = self._signed_post(
            client, "tenant", "/agents",
            {"name": "spawned", "model": "opus"},
        )
        assert resp.status_code == 403
        assert "isolated" in str(resp.json()).lower()
        # And the agent must NOT have been created.
        assert client.app.state.agents.get("spawned") is None
        os.unlink(path)

    def test_isolated_agent_denied_self_upsert_register(self, monkeypatch, tmp_path):
        client, path = self._make_client_with_agents(monkeypatch, tmp_path)
        # Even a self-named upsert is denied — it would let the tenant drop its
        # OWN isolated flag (escape).
        resp = self._signed_post(
            client, "tenant", "/agents",
            {"name": "tenant", "model": "opus", "isolated": False},
        )
        assert resp.status_code == 403
        # Flag unchanged — tenant is still isolated.
        assert client.app.state.agents.get("tenant").isolated is True
        os.unlink(path)

    def test_non_isolated_agent_register_not_denied(self, monkeypatch, tmp_path):
        client, path = self._make_client_with_agents(monkeypatch, tmp_path)
        # Full-trust 'other' may still register agents (route works as before).
        resp = self._signed_post(
            client, "other", "/agents",
            {"name": "spawned", "model": "opus", "working_dir": str(tmp_path / "spawned")},
        )
        assert resp.status_code != 403
        os.unlink(path)

    # ── #149 phase-3 inc2: the global secret no longer authenticates an
    # isolated caller (daemon-side cutover; dual-accept preserved otherwise) ──

    def test_isolated_agent_global_secret_auth_rejected(self, monkeypatch, tmp_path):
        client, path = self._make_client_with_agents(monkeypatch, tmp_path)
        # Isolated 'tenant' signs a SELF request with the forgeable GLOBAL
        # secret. Post-cutover the daemon refuses to authenticate it at all —
        # even on its own resource — so auth fails (401) before any route runs.
        headers = build_internal_auth_headers(
            "test-session-secret", agent_name="tenant",
            method="GET", path="/agents/tenant",
        )
        resp = client.get("/agents/tenant", headers=headers)
        assert resp.status_code == 401
        # Its OWN per-agent key still authenticates (self-access → not 401/403).
        ok = self._signed_get(client, "tenant", "/agents/tenant")
        assert ok.status_code not in (401, 403)
        os.unlink(path)

    def test_non_isolated_agent_global_secret_still_accepted(self, monkeypatch, tmp_path):
        client, path = self._make_client_with_agents(monkeypatch, tmp_path)
        # Dual-accept is preserved for full-trust agents: 'other' may still
        # authenticate with the global secret (until #623 inc4 drops it
        # fleet-wide). It must not get a 401.
        headers = build_internal_auth_headers(
            "test-session-secret", agent_name="other",
            method="GET", path="/agents/other",
        )
        resp = client.get("/agents/other", headers=headers)
        assert resp.status_code != 401
        os.unlink(path)

    def test_unknown_agent_global_secret_rejected(self, monkeypatch, tmp_path):
        client, path = self._make_client_with_agents(monkeypatch, tmp_path)
        # Murzik #640: the global secret is a universal bearer credential, so it
        # must NOT authenticate an UNKNOWN claimed identity — otherwise a leaked
        # secret signs as any made-up name. 'ghost' is unregistered → 401.
        headers = build_internal_auth_headers(
            "test-session-secret", agent_name="ghost",
            method="GET", path="/agents/tenant",
        )
        resp = client.get("/agents/tenant", headers=headers)
        assert resp.status_code == 401
        os.unlink(path)


class TestSensitivePrefixesRequireAuth:
    """Auth-gate coverage for admin-control, credential, and PII API surfaces.
    /admin, /providers, /bot-tokens, /user-profiles, /calendar, /apps, /models
    are in ``_protected_api_prefixes`` and require a session cookie or signed
    internal auth. These tests pin (a) unauth requests get 401, (b) the legit
    gates still pass — notably HMAC on /admin so agent self-update keeps
    working, and (c) the public carve-outs are unbroken: the Google OAuth
    callback stays reachable and the public app viewer (/a/{token}) is not
    under /apps.
    """

    _MW_DENY = {"detail": "Unauthorized"}

    def _make_client(self, monkeypatch):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        monkeypatch.setenv("PINKY_SESSION_SECRET", "test-session-secret")
        monkeypatch.delenv("PINKY_UI_PASSWORD", raising=False)
        app = create_api(max_sessions=10, default_working_dir="/tmp", db_path=path)
        return TestClient(app), path

    def test_unauth_sensitive_prefixes_now_401(self, monkeypatch):
        client, path = self._make_client(monkeypatch)
        # Non-browser (curl-shape): no Origin, no cookie, no HMAC.
        for p in (
            "/admin/restart",
            "/admin/update",
            "/providers",
            "/bot-tokens",
            "/user-profiles",
            "/models",
            "/apps",
            "/calendar/config",
            "/calendar/google/credentials",
        ):
            resp = client.get(p)
            assert resp.status_code == 401, (
                f"{p} must require auth (was an unauth fall-through), got {resp.status_code}"
            )
            assert resp.json() == self._MW_DENY, f"{p} should hit the middleware deny"
        os.unlink(path)

    def test_oauth_callback_carveout_stays_public(self, monkeypatch):
        """/calendar/google/callback must NOT be blocked by the gate — Google's
        redirect arrives cross-site with no cookie; the route self-validates a
        state nonce. It reaches the route (which may 3xx/4xx on a bad state) but
        is never the middleware default-deny 401."""
        client, path = self._make_client(monkeypatch)
        resp = client.get("/calendar/google/callback?state=x&code=y", follow_redirects=False)
        blocked_by_mw = resp.status_code == 401 and (
            resp.headers.get("content-type", "").startswith("application/json")
            and resp.json() == self._MW_DENY
        )
        assert not blocked_by_mw, "OAuth callback must stay reachable past the auth gate"
        os.unlink(path)

    def test_public_app_viewer_not_under_apps_prefix(self, monkeypatch):
        """Protecting /apps must not break public app viewing — the public
        viewer lives at /a/{share_token}, a different prefix that still falls
        through to its route."""
        client, path = self._make_client(monkeypatch)
        resp = client.get("/a/some-share-token", follow_redirects=False)
        blocked_by_mw = resp.status_code == 401 and (
            resp.headers.get("content-type", "").startswith("application/json")
            and resp.json() == self._MW_DENY
        )
        assert not blocked_by_mw, "/a/{token} public viewer must not be gated by the /apps protection"
        os.unlink(path)

    def test_session_cookie_passes_sensitive_prefix(self, monkeypatch):
        client, path = self._make_client(monkeypatch)
        client.post("/auth/setup", json={"password": "hunter22", "next": "/"})
        # logged-in browser session → must not be middleware-401 on /providers
        resp = client.get("/providers")
        assert resp.status_code != 401, (
            f"session-authed request blocked at middleware ({resp.status_code})"
        )
        os.unlink(path)

    def test_hmac_signed_passes_admin(self, monkeypatch):
        client, path = self._make_client(monkeypatch)
        client.app.state.agents.register("barsik", model="opus", working_dir="/tmp/barsik")
        headers = build_internal_auth_headers(
            "test-session-secret", agent_name="barsik",
            method="POST", path="/admin/update/status",
        )
        resp = client.post(
            "/admin/update/status",
            headers={**headers, "Content-Type": "application/json"},
            json={},
        )
        assert resp.status_code != 401, (
            f"HMAC-signed admin request blocked at middleware ({resp.status_code}) — "
            f"would break update_and_restart self-update"
        )
        os.unlink(path)
