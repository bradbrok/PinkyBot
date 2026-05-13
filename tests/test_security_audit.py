"""Tests for the security audit log (#440)."""

from __future__ import annotations

import os
import sqlite3
import tempfile
from unittest.mock import patch

import pytest

from pinky_daemon.security_audit import (
    DEFAULT_DETAIL_ALLOWLIST,
    EVENT_DETAIL_ALLOWLIST,
    AuditOutcome,
    SecurityAuditStore,
    redact_detail,
)

# ── Redaction ──────────────────────────────────────────────────


class TestRedactDetail:
    def test_none_passes_through(self):
        assert redact_detail("auth.login.success", None) is None

    def test_per_event_allowlist_keeps_only_allowed_keys(self):
        out = redact_detail(
            "auth.login.success",
            {
                "username": "alice",
                "method": "password",
                "password": "shh",
                "session_token": "abc",
            },
        )
        assert out == {"username": "alice", "method": "password"}

    def test_unknown_event_uses_default_allowlist(self):
        out = redact_detail(
            "made.up.event",
            {
                "reason": "unspecified",
                "duration_ms": 12,
                "secret_value": "do not leak",
            },
        )
        # "reason" + "duration_ms" are in DEFAULT_DETAIL_ALLOWLIST.
        assert out == {"reason": "unspecified", "duration_ms": 12}

    def test_default_allowlist_does_not_include_credential_words(self):
        # Sanity: future-defaulted fields don't carry secrets.
        for key in ("token", "password", "secret", "authorization"):
            assert key not in DEFAULT_DETAIL_ALLOWLIST

    def test_nested_dict_walked_with_same_allowlist(self):
        # The allowlist applies at every nesting level. Useful so a
        # whitelisted "fields_changed" sub-object can pass through but
        # any unexpected key inside it gets dropped.
        out = redact_detail(
            "admin.update",
            {
                "fields_changed": {"target_path": "/foo"},
                "target_path": "/x",
                "version_after": 7,
                "secret": "leak",
            },
        )
        assert out == {
            "fields_changed": {"target_path": "/foo"},
            "target_path": "/x",
            "version_after": 7,
        }

    def test_lists_are_walked(self):
        out = redact_detail(
            "rate_limit.exceeded",
            {
                "bucket": ["a", "b"],
                "count": 5,
                "limit": 3,
                "secret": "x",
            },
        )
        assert out == {"bucket": ["a", "b"], "count": 5, "limit": 3}

    def test_event_with_empty_allowlist_drops_everything(self):
        # Manufacture an empty allowlist by patching.
        with patch.dict(
            EVENT_DETAIL_ALLOWLIST, {"empty.event": frozenset()}, clear=False
        ):
            assert redact_detail("empty.event", {"x": 1}) == {}


# ── Store ──────────────────────────────────────────────────────


@pytest.fixture
def store():
    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, "sec.db")
        yield SecurityAuditStore(db_path=path)


class TestSecurityAuditStore:
    def test_log_event_returns_row_id(self, store):
        row_id = store.log_event(
            "auth.login.success",
            outcome=AuditOutcome.SUCCESS,
            actor_user_id=42,
            actor_ip="10.0.0.5",
            detail={"username": "alice", "method": "password"},
        )
        assert row_id is not None
        assert row_id > 0

    def test_log_redacts_detail(self, store):
        store.log_event(
            "auth.login.fail",
            outcome=AuditOutcome.FAILURE,
            detail={"username": "alice", "password": "shh", "reason": "bad"},
        )
        rows = store.query()
        assert len(rows) == 1
        assert rows[0].detail == {
            "username": "alice",
            "method": None if "method" in rows[0].detail else "missing",
            "reason": "bad",
        } or rows[0].detail == {"username": "alice", "reason": "bad"}
        # Either way, no password.
        assert "password" not in rows[0].detail

    def test_query_filter_by_event_type(self, store):
        store.log_event("auth.login.success", actor_user_id=1)
        store.log_event("auth.login.fail", actor_user_id=1)
        store.log_event("admin.update", actor_user_id=1)
        rows = store.query(event_type="auth.login.fail")
        assert len(rows) == 1
        assert rows[0].event_type == "auth.login.fail"

    def test_query_filter_by_actor(self, store):
        store.log_event("auth.login.success", actor_user_id=1)
        store.log_event("auth.login.success", actor_user_id=2)
        rows = store.query(actor_user_id=2)
        assert len(rows) == 1
        assert rows[0].actor_user_id == 2

    def test_query_filter_by_outcome(self, store):
        store.log_event("admin.update", outcome=AuditOutcome.SUCCESS)
        store.log_event("admin.update", outcome=AuditOutcome.DENIED)
        denied = store.query(outcome=AuditOutcome.DENIED)
        assert len(denied) == 1
        assert denied[0].outcome == "denied"

    def test_query_filter_by_time_range(self, store):
        store.log_event("auth.login.success", ts=1000.0)
        store.log_event("auth.login.success", ts=2000.0)
        store.log_event("auth.login.success", ts=3000.0)
        rows = store.query(since=1500.0, until=2500.0)
        assert len(rows) == 1
        assert rows[0].ts == 2000.0

    def test_query_filter_by_correlation_id(self, store):
        store.log_event("auth.login.success", correlation_id="req-1")
        store.log_event("admin.update", correlation_id="req-1")
        store.log_event("admin.update", correlation_id="req-2")
        rows = store.query(correlation_id="req-1")
        assert len(rows) == 2
        assert {r.event_type for r in rows} == {
            "auth.login.success",
            "admin.update",
        }

    def test_query_returns_newest_first(self, store):
        store.log_event("auth.login.success", ts=1000.0)
        store.log_event("auth.login.success", ts=3000.0)
        store.log_event("auth.login.success", ts=2000.0)
        rows = store.query()
        assert [r.ts for r in rows] == [3000.0, 2000.0, 1000.0]

    def test_query_pagination(self, store):
        for i in range(5):
            store.log_event("auth.login.success", ts=float(i))
        page1 = store.query(limit=2, offset=0)
        page2 = store.query(limit=2, offset=2)
        assert [r.ts for r in page1] == [4.0, 3.0]
        assert [r.ts for r in page2] == [2.0, 1.0]

    def test_count(self, store):
        store.log_event("auth.login.success", outcome=AuditOutcome.SUCCESS)
        store.log_event("auth.login.fail", outcome=AuditOutcome.FAILURE)
        store.log_event("auth.login.fail", outcome=AuditOutcome.FAILURE)
        assert store.count() == 3
        assert store.count(event_type="auth.login.fail") == 2
        assert store.count(outcome=AuditOutcome.SUCCESS) == 1

    def test_via_proxy_and_forwarded_chain_round_trip(self, store):
        store.log_event(
            "auth.login.success",
            actor_ip="203.0.113.7",
            via_proxy=True,
            forwarded_chain=["203.0.113.7", "10.0.0.5"],
        )
        rows = store.query()
        assert rows[0].via_proxy is True
        assert rows[0].forwarded_chain == ["203.0.113.7", "10.0.0.5"]

    def test_method_route_user_agent_round_trip(self, store):
        store.log_event(
            "admin.update",
            method="POST",
            route_name="admin.update",
            user_agent="curl/8.6.0",
            target="/admin/update",
        )
        r = store.query()[0]
        assert r.method == "POST"
        assert r.route_name == "admin.update"
        assert r.user_agent == "curl/8.6.0"
        assert r.target == "/admin/update"

    def test_log_event_swallows_db_error(self, store):
        # Force a write failure: close the underlying connection and try
        # to log. Best-effort contract → returns None instead of raising.
        store._db.close()
        # Replace with a closed connection so the next op raises.
        result = store.log_event(
            "auth.login.success", actor_user_id=99
        )
        assert result is None


# ── Retention ──────────────────────────────────────────────────


class TestRetention:
    def test_prune_older_than(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = SecurityAuditStore(db_path=os.path.join(tmpdir, "x.db"))
            now = 10_000_000.0  # arbitrary anchor
            store.log_event("auth.login.success", ts=now - 86400 * 400)
            store.log_event("auth.login.success", ts=now - 86400 * 200)
            store.log_event("auth.login.success", ts=now - 86400 * 1)
            deleted = store.prune_older_than(365, now=now)
            assert deleted == 1
            remaining = store.query()
            assert len(remaining) == 2

    def test_prune_zero_or_negative_is_noop(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = SecurityAuditStore(db_path=os.path.join(tmpdir, "x.db"))
            store.log_event("auth.login.success", ts=1.0)
            assert store.prune_older_than(0) == 0
            assert store.prune_older_than(-7) == 0
            assert store.count() == 1


# ── Schema ─────────────────────────────────────────────────────


class TestSchema:
    def test_table_and_indexes_present(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "s.db")
            SecurityAuditStore(db_path=path)
            con = sqlite3.connect(path)
            tables = {
                r[0]
                for r in con.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            assert "security_audit_log" in tables
            indexes = {
                r[0]
                for r in con.execute(
                    "SELECT name FROM sqlite_master WHERE type='index'"
                ).fetchall()
            }
            assert "idx_sec_audit_ts" in indexes
            assert "idx_sec_audit_event" in indexes
            assert "idx_sec_audit_actor" in indexes
            assert "idx_sec_audit_correlation" in indexes

    def test_prev_hash_column_reserved(self):
        # Reserved for future hash-chain integrity work — not populated
        # in this PR, but the column must exist so we don't take a
        # migration later.
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "s.db")
            SecurityAuditStore(db_path=path)
            con = sqlite3.connect(path)
            cols = {
                r[1]
                for r in con.execute(
                    "PRAGMA table_info(security_audit_log)"
                ).fetchall()
            }
            assert "prev_hash" in cols


# ── Wiring through create_api ──────────────────────────────────


class TestCreateApiWiring:
    def test_app_state_carries_security_audit(self):
        from pinky_daemon.api import create_api
        from pinky_daemon.config import DeploymentMode

        with tempfile.TemporaryDirectory() as tmpdir:
            db = os.path.join(tmpdir, "test.db")
            app = create_api(
                db_path=db, deployment_mode=DeploymentMode.TRUSTED
            )
            assert isinstance(
                app.state.security_audit, SecurityAuditStore
            )
            # Smoke: write + read back a row.
            row_id = app.state.security_audit.log_event(
                "boot.daemon.start",
                outcome=AuditOutcome.SUCCESS,
                detail={"deployment_mode": "trusted"},
            )
            assert row_id is not None
            rows = app.state.security_audit.query()
            assert len(rows) == 1
            assert rows[0].detail == {"deployment_mode": "trusted"}
