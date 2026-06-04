"""Deny-by-default auth coverage (#506).

Two guarantees:
  1. Every registered route is classified by the auth gate — no route falls
     through to the app unauthenticated (the audit returns zero UNCLASSIFIED).
     This is the regression gate: a new route added without a public/protected
     classification fails CI.
  2. The previously-leaking surfaces now require auth, the public app viewer
     stays reachable, and the PINKY_AUTH_DENY_DEFAULT shadow/enforce switch
     behaves as specified.
"""

from __future__ import annotations

import os
import sys
import tempfile

import pytest
from fastapi.testclient import TestClient

from pinky_daemon.api import create_api

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import audit_route_auth_coverage as audit_mod  # noqa: E402


def _client(mode: str | None = None):
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    old = os.environ.get("PINKY_AUTH_DENY_DEFAULT")
    if mode is not None:
        os.environ["PINKY_AUTH_DENY_DEFAULT"] = mode
    try:
        app = create_api(max_sessions=10, default_working_dir="/tmp", db_path=path)
    finally:
        if mode is not None:
            if old is None:
                os.environ.pop("PINKY_AUTH_DENY_DEFAULT", None)
            else:
                os.environ["PINKY_AUTH_DENY_DEFAULT"] = old
    return TestClient(app), path


def test_no_unclassified_routes():
    """The whole point of #506: not one registered route is unclassified."""
    unclassified = audit_mod.audit()
    assert unclassified == [], (
        "Routes fall through the auth gate unclassified — add each to a public "
        f"or protected set in api.py: {unclassified}"
    )


def test_audit_catches_unclassified_mount():
    """A future sub-app mounted at an unclassified prefix must be caught by the
    gate — the middleware sees the path before routing, so mounted subtrees
    need classification just like HTTP/WS routes (Murzik review, #675)."""
    from starlette.applications import Starlette

    app = audit_mod.build_app()
    app.mount("/synthetic-unmapped", Starlette())
    routes, sets = audit_mod.collect(app)
    classify = audit_mod.classifier(sets)

    assert ("MOUNT", "/synthetic-unmapped/") in routes, "mount not collected as a subtree"
    assert classify("/synthetic-unmapped/") == "UNCLASSIFIED"
    # And the real static mounts still classify cleanly (regression guard).
    assert classify("/static/") == "PUBLIC-prefix"
    assert classify("/assets/") == "PUBLIC-prefix"


# Surfaces that used to fall through the gate (now protected). A sampling
# across the prefixes added in #506; an unauthenticated request must 401.
_NEWLY_PROTECTED = [
    "/analytics/overview",
    "/api/migrate/openclaw/status/x",
    "/comms/messages",
    "/dreams",
    "/federation/peers",
    "/mesh/diagnostics",
    "/milestones/abc",
    "/plugins",
    "/render/pdf",
    "/schedules",
    "/soul-templates",
    "/sprints/abc/burndown",
    "/triggers",
]


@pytest.mark.real_auth
def test_previously_leaking_surfaces_require_auth():
    client, path = _client()
    try:
        for route in _NEWLY_PROTECTED:
            resp = client.get(route)
            assert resp.status_code == 401, f"{route} should require auth, got {resp.status_code}"
    finally:
        os.unlink(path)


@pytest.mark.real_auth
def test_public_app_viewer_not_blocked_by_auth():
    """The /a/{share_token} public viewer must pass the auth gate (it may 404
    for an unknown token, but it is NOT 401'd)."""
    client, path = _client()
    try:
        resp = client.get("/a/some-unknown-token")
        assert resp.status_code != 401
    finally:
        os.unlink(path)


@pytest.mark.real_auth
def test_known_public_routes_reachable():
    client, path = _client()
    try:
        assert client.get("/api").status_code == 200
        # /login is public (returns the login page or a redirect, never 401).
        assert client.get("/login", follow_redirects=False).status_code != 401
    finally:
        os.unlink(path)


@pytest.mark.real_auth
def test_enforce_mode_denies_unmapped_path():
    """In enforce mode an unauthenticated request to an unmapped, non-public
    path is 401'd rather than falling through to a 404."""
    client, path = _client(mode="enforce")
    try:
        resp = client.get("/zzz-unmapped-probe")
        assert resp.status_code == 401
    finally:
        os.unlink(path)


@pytest.mark.real_auth
def test_off_and_shadow_modes_fall_through_unmapped_path():
    """off/shadow preserve legacy behaviour: an unmapped path falls through to
    the app's 404 (shadow additionally logs a would-deny, but does not block)."""
    for mode in ("off", "shadow"):
        client, path = _client(mode=mode)
        try:
            resp = client.get("/zzz-unmapped-probe")
            assert resp.status_code == 404, f"mode={mode} should fall through, got {resp.status_code}"
        finally:
            os.unlink(path)
