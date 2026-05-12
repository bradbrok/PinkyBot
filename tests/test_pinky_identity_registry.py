"""Tests for the service-auth identity registry and enrollment routes."""

from __future__ import annotations

import base64

import pytest
from fastapi.testclient import TestClient

from pinky_daemon.api import create_api
from pinky_daemon.auth import SESSION_COOKIE_NAME, create_session_cookie
from pinky_identity import (
    KEY_ACTIVE,
    KEY_PENDING,
    KEY_RETIRED,
    KEY_REVOKED,
    TOKEN_USED,
    IdentityRegistry,
    IdentityRegistryError,
    fingerprint,
    generate_keypair,
    kid_from_public_key,
)


def _b64url_nopad(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


@pytest.fixture
def registry(tmp_path):
    store = IdentityRegistry(db_path=str(tmp_path / "identity.db"))
    yield store
    store.close()


class TestIdentityRegistryStore:
    def test_issue_token_returns_raw_once_and_stores_hash_only(self, registry):
        kp = generate_keypair()
        fp = fingerprint(kp.public)

        token = registry.issue_enrollment_token(
            fleet="pos",
            agent_name="lera",
            public_key_fingerprint=fp,
            issued_by="brad",
            ttl_seconds=60,
            reason="new agent",
        )

        assert token.token
        assert token.public_key_fingerprint == fp
        assert "token" not in token.to_dict()
        assert token.to_dict(include_token=True)["token"] == token.token

        stored = registry.get_enrollment_token(token.token_id)
        assert stored is not None
        assert stored.token == ""
        assert stored.token_hash
        assert token.token not in stored.token_hash

    def test_redeem_matching_token_activates_key_and_marks_token_used(self, registry):
        kp = generate_keypair()
        token = registry.issue_enrollment_token(
            fleet="pos",
            agent_name="lera",
            public_key_fingerprint=fingerprint(kp.public),
            issued_by="brad",
        )

        key = registry.redeem_enrollment_token(
            token=token.token,
            public_key=kp.public,
            fleet="pos",
            agent_name="lera",
            actor="lera",
        )

        assert key.status == KEY_ACTIVE
        assert key.kid == kid_from_public_key(kp.public)
        assert key.public_jwk["kty"] == "OKP"
        assert registry.get_active_key(fleet="pos", agent_name="lera") == key

        stored_token = registry.get_enrollment_token(token.token_id)
        assert stored_token is not None
        assert stored_token.status == TOKEN_USED
        assert stored_token.used_at > 0

        events = registry.list_audit(agent_name="lera")
        assert {e.event_type for e in events} >= {
            "enrollment_token.issued",
            "identity_key.enrolled",
        }

    def test_redeem_rejects_binding_mismatch(self, registry):
        token_key = generate_keypair()
        submitted_key = generate_keypair()
        token = registry.issue_enrollment_token(
            fleet="pos",
            agent_name="lera",
            public_key_fingerprint=fingerprint(token_key.public),
            issued_by="brad",
        )

        with pytest.raises(IdentityRegistryError, match="binding"):
            registry.redeem_enrollment_token(
                token=token.token,
                public_key=submitted_key.public,
                fleet="pos",
                agent_name="lera",
            )

        assert registry.get_key(kid_from_public_key(submitted_key.public)) is None
        failures = registry.list_audit(event_type="enrollment_token.redeem_failed")
        assert failures
        assert failures[0].outcome == "failure"

    def test_redeem_rejects_replay(self, registry):
        kp = generate_keypair()
        token = registry.issue_enrollment_token(
            fleet="pos",
            agent_name="lera",
            public_key_fingerprint=fingerprint(kp.public),
            issued_by="brad",
        )
        registry.redeem_enrollment_token(
            token=token.token,
            public_key=kp.public,
            fleet="pos",
            agent_name="lera",
        )

        with pytest.raises(IdentityRegistryError, match="not active"):
            registry.redeem_enrollment_token(
                token=token.token,
                public_key=kp.public,
                fleet="pos",
                agent_name="lera",
            )

    def test_redeem_rejects_expired_token(self, registry, monkeypatch):
        kp = generate_keypair()
        token = registry.issue_enrollment_token(
            fleet="pos",
            agent_name="lera",
            public_key_fingerprint=fingerprint(kp.public),
            issued_by="brad",
            ttl_seconds=1,
        )

        monkeypatch.setattr("pinky_identity.registry._now", lambda: token.expires_at + 1)
        with pytest.raises(IdentityRegistryError, match="expired"):
            registry.redeem_enrollment_token(
                token=token.token,
                public_key=kp.public,
                fleet="pos",
                agent_name="lera",
            )

    def test_approve_pending_key_retires_existing_active_key(self, registry):
        first = generate_keypair()
        first_token = registry.issue_enrollment_token(
            fleet="pos",
            agent_name="lera",
            public_key_fingerprint=fingerprint(first.public),
            issued_by="brad",
        )
        active = registry.redeem_enrollment_token(
            token=first_token.token,
            public_key=first.public,
            fleet="pos",
            agent_name="lera",
        )

        replacement = generate_keypair()
        pending = registry.submit_pending_key(
            public_key=replacement.public,
            fleet="pos",
            agent_name="lera",
            actor="brad",
        )
        assert pending.status == KEY_PENDING

        approved = registry.approve_key(
            pending.kid, approved_by="brad", reason="break-glass rotate"
        )
        assert approved.status == KEY_ACTIVE
        old = registry.get_key(active.kid)
        assert old is not None
        assert old.status == KEY_RETIRED
        assert old.replaced_by_kid == pending.kid

    def test_revoke_key_marks_revoked_and_audits_reason(self, registry):
        kp = generate_keypair()
        pending = registry.submit_pending_key(
            public_key=kp.public,
            fleet="pos",
            agent_name="olga",
            actor="brad",
        )

        revoked = registry.revoke_key(
            pending.kid,
            revoked_by="brad",
            reason="compromised",
        )

        assert revoked.status == KEY_REVOKED
        assert revoked.revoked_by == "brad"
        assert revoked.revoke_reason == "compromised"
        events = registry.list_audit(event_type="identity_key.revoked")
        assert events[0].reason == "compromised"


class TestIdentityRegistryRoutes:
    def _client(self, tmp_path, monkeypatch):
        db_path = str(tmp_path / "pinky.db")
        monkeypatch.setenv("PINKY_SESSION_SECRET", "test-session-secret")
        monkeypatch.delenv("PINKY_UI_PASSWORD", raising=False)
        app = create_api(max_sessions=10, default_working_dir="/tmp", db_path=db_path)
        return TestClient(app)

    def _admin_client(self, tmp_path, monkeypatch):
        client = self._client(tmp_path, monkeypatch)
        client.cookies.set(
            SESSION_COOKIE_NAME,
            create_session_cookie("test-session-secret"),
            domain="testserver.local",
        )
        return client

    def test_admin_token_issue_requires_session_even_for_non_browser_call(
        self, tmp_path, monkeypatch
    ):
        client = self._client(tmp_path, monkeypatch)
        kp = generate_keypair()

        resp = client.post(
            "/identity/admin/enrollment-tokens",
            json={
                "fleet": "pos",
                "agent_name": "lera",
                "public_key_fingerprint": fingerprint(kp.public),
                "issued_by": "brad",
            },
        )

        assert resp.status_code == 401

    def test_token_issue_redeem_and_public_lookup_route(self, tmp_path, monkeypatch):
        client = self._admin_client(tmp_path, monkeypatch)
        kp = generate_keypair()
        fp = fingerprint(kp.public)

        issued = client.post(
            "/identity/admin/enrollment-tokens",
            json={
                "fleet": "pos",
                "agent_name": "lera",
                "public_key_fingerprint": fp,
                "issued_by": "brad",
                "ttl_seconds": 60,
            },
        )
        assert issued.status_code == 200
        token = issued.json()["token"]
        assert token
        assert "token_hash" not in issued.json()

        redeem = client.post(
            "/identity/enroll/redeem",
            json={
                "token": token,
                "fleet": "pos",
                "agent_name": "lera",
                "public_key_b64": _b64url_nopad(kp.public.raw),
            },
        )
        assert redeem.status_code == 200
        key = redeem.json()
        assert key["status"] == KEY_ACTIVE
        assert key["fingerprint"] == fp

        lookup = client.get(f"/identity/keys/{key['kid']}")
        assert lookup.status_code == 200
        assert lookup.json()["public_jwk"]["kid"] == key["kid"]

    def test_admin_pending_approve_revoke_and_audit_routes(self, tmp_path, monkeypatch):
        client = self._admin_client(tmp_path, monkeypatch)
        kp = generate_keypair()

        pending = client.post(
            "/identity/admin/keys/pending",
            json={
                "fleet": "pos",
                "agent_name": "olga",
                "public_jwk": {"x": _b64url_nopad(kp.public.raw)},
                "actor": "brad",
            },
        )
        assert pending.status_code == 200
        kid = pending.json()["kid"]
        assert pending.json()["status"] == KEY_PENDING

        approved = client.post(
            f"/identity/admin/keys/{kid}/approve",
            json={"approved_by": "brad", "reason": "manual break-glass"},
        )
        assert approved.status_code == 200
        assert approved.json()["status"] == KEY_ACTIVE

        revoked = client.post(
            f"/identity/admin/keys/{kid}/revoke",
            json={"revoked_by": "brad", "reason": "test revoke"},
        )
        assert revoked.status_code == 200
        assert revoked.json()["status"] == KEY_REVOKED

        audit = client.get("/identity/admin/audit?agent_name=olga")
        assert audit.status_code == 200
        event_types = {e["event_type"] for e in audit.json()["entries"]}
        assert {
            "identity_key.submitted",
            "identity_key.approved",
            "identity_key.revoked",
        }.issubset(event_types)
