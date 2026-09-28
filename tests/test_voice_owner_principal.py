"""Voice decisions require the owner and proposals bind the signed caller."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

import pinky_daemon.voice_engine as voice_engine
import pinky_daemon.voice_routes as voice_routes
from pinky_daemon.api import create_api
from pinky_daemon.auth import (
    SESSION_COOKIE_NAME,
    build_internal_auth_headers,
    create_session_cookie,
    verify_session_cookie,
)

pytestmark = pytest.mark.real_auth
UI_SECRET = "synthetic-owner-session-signing-key"
UI_PASSWORD = "synthetic-owner-login-password"


@pytest.fixture
def voice_api(tmp_path, monkeypatch):
    monkeypatch.setenv("PINKY_SESSION_SECRET", UI_SECRET)
    monkeypatch.setenv("PINKY_UI_PASSWORD", UI_PASSWORD)
    for name in ("_voice_store", "_agents", "_broker_send", "_base_url"):
        monkeypatch.setattr(voice_routes, name, getattr(voice_routes, name))
    app = create_api(default_working_dir=str(tmp_path), db_path=str(tmp_path / "state.db"))
    registry = app.state.agents
    trusted = next(iter(voice_routes.AUTO_APPROVE_AGENTS))
    for name in ("tenant", "other", trusted):
        registry.register(name, working_dir=str(tmp_path / name))
    assert "tenant" not in voice_routes.AUTO_APPROVE_AGENTS
    dial = AsyncMock(return_value={"call_sid": "CA_test"})
    pending_notice, approved_notice = AsyncMock(), AsyncMock()
    monkeypatch.setattr(voice_engine, "dial_approved_call", dial)
    monkeypatch.setattr(voice_routes, "_notify_owner_call_request", pending_notice)
    monkeypatch.setattr(voice_routes, "_notify_owner_auto_approved", approved_notice)
    monkeypatch.setattr(voice_routes, "_base_url", "voice.example.invalid")
    store = voice_routes._voice_store
    client = TestClient(app)
    try:
        yield SimpleNamespace(
            client=client,
            registry=registry,
            store=store,
            trusted=trusted,
            dial=dial,
            pending_notice=pending_notice,
            approved_notice=approved_notice,
        )
    finally:
        client.close()
        store.close()
        registry.close()


def login_owner(api):
    response = api.client.post("/auth/login", json={"password": UI_PASSWORD})
    assert response.status_code == 200, response.text
    session = verify_session_cookie(UI_SECRET, api.client.cookies[SESSION_COOKIE_NAME])
    assert session is not None
    return session["user"]


def signed_post(api, path, body, caller="tenant"):
    key = api.registry.get_signing_key(caller)
    assert key
    headers = build_internal_auth_headers(key, agent_name=caller, method="POST", path=path)
    return api.client.post(path, json=body, headers=headers)


def proposal(**extra):
    return dict(
        target_name="Test contact",
        target_phone="+15551234567",
        goal="Test request",
        context={},
        fallback_behavior="hang_up",
        **extra,
    )


def seed_request(api):
    return api.store.create_call_request(requested_by_agent="tenant", **proposal())


def assert_no_effect(api, before):
    assert [row.to_dict() for row in api.store.list_call_requests()] == before
    api.dial.assert_not_awaited()
    api.pending_notice.assert_not_awaited()
    api.approved_notice.assert_not_awaited()


@pytest.mark.parametrize("action", ["approve", "deny", "cancel"])
@pytest.mark.parametrize("isolated", [False, True])
@pytest.mark.parametrize("owner_cookie", [False, True])
def test_signed_decision_requires_owner(voice_api, action, isolated, owner_cookie):
    api = voice_api
    api.registry.register("tenant", isolated=isolated)
    request = seed_request(api)
    before = [request.to_dict()]
    if owner_cookie:
        login_owner(api)
    response = signed_post(api, f"/api/voice/request/{request.id}/{action}", {})
    assert response.status_code == 403, response.text
    assert_no_effect(api, before)
    assert api.store.get_call_request(request.id).approval_state == "pending_approval"


@pytest.mark.parametrize("action", ["approve", "deny", "cancel"])
def test_non_owner_session_cannot_decide(voice_api, action):
    api = voice_api
    request = seed_request(api)
    before = [request.to_dict()]
    api.client.cookies.set(SESSION_COOKIE_NAME, create_session_cookie(UI_SECRET, user="other"))
    # The cookie is valid, but its user is not the principal issued by owner login.
    assert api.client.get("/auth/status").json()["authenticated"] is True
    response = api.client.post(f"/api/voice/request/{request.id}/{action}", json={})
    assert response.status_code == 403, response.text
    assert_no_effect(api, before)


@pytest.mark.parametrize(
    "action,state",
    [
        ("approve", "approved"),
        ("deny", "rejected"),
        ("cancel", "cancelled"),
    ],
)
def test_owner_login_can_decide(voice_api, action, state):
    api = voice_api
    request = seed_request(api)
    login_owner(api)
    response = api.client.post(f"/api/voice/request/{request.id}/{action}", json={})
    assert response.status_code == 200, response.text
    assert api.store.get_call_request(request.id).approval_state == state
    if action == "approve":
        api.dial.assert_awaited_once()
    else:
        api.dial.assert_not_awaited()


def test_owner_approval_records_verified_principal(voice_api):
    api = voice_api
    request = seed_request(api)
    owner = login_owner(api)
    response = api.client.post(f"/api/voice/request/{request.id}/approve")
    assert response.status_code == 200, response.text
    stored = api.store.get_call_request(request.id)
    assert stored.authorized_by == f"ui:{owner}"
    assert api.dial.call_args.args[0].authorized_by == f"ui:{owner}"


@pytest.mark.parametrize("isolated", [False, True])
@pytest.mark.parametrize("target", ["trusted", "other", ""])
def test_signed_proposal_rejects_identity_mismatch(voice_api, isolated, target):
    api = voice_api
    api.registry.register("tenant", isolated=isolated)
    requested_by = api.trusted if target == "trusted" else target
    response = signed_post(api, "/api/voice/request", proposal(requested_by_agent=requested_by))
    assert response.status_code == 403, response.text
    assert_no_effect(api, [])


@pytest.mark.parametrize("isolated", [False, True])
def test_omitted_requester_uses_signed_caller(voice_api, isolated):
    api = voice_api
    api.registry.register("tenant", isolated=isolated)
    response = signed_post(api, "/api/voice/request", proposal())
    assert response.status_code == 200, response.text
    assert response.json()["approval_state"] == "pending_approval"
    stored = api.store.get_call_request(response.json()["request_id"])
    assert stored.requested_by_agent == "tenant"
    api.dial.assert_not_awaited()
    api.pending_notice.assert_awaited_once()
    api.approved_notice.assert_not_awaited()


@pytest.mark.parametrize("trusted", [False, True])
def test_signed_caller_can_propose_as_itself(voice_api, trusted):
    api = voice_api
    caller = api.trusted if trusted else "tenant"
    response = signed_post(api, "/api/voice/request", proposal(requested_by_agent=caller), caller)
    assert response.status_code == 200, response.text
    row = api.store.get_call_request(response.json()["request_id"])
    assert row.requested_by_agent == caller
    assert row.approval_state == ("approved" if trusted else "pending_approval")
    if trusted:
        assert row.authorized_by == "auto_approve"
        api.dial.assert_awaited_once()
    else:
        api.dial.assert_not_awaited()


@pytest.mark.parametrize(
    "method,suffix",
    [
        ("POST", "/request"),
        ("GET", "/requests"),
        ("GET", "/request/{id}"),
        ("POST", "/request/{id}/approve"),
        ("POST", "/request/{id}/deny"),
        ("POST", "/request/{id}/cancel"),
    ],
)
@pytest.mark.parametrize("bad_cookie", [False, True])
def test_voice_request_routes_require_auth(voice_api, method, suffix, bad_cookie):
    api = voice_api
    request = seed_request(api)
    before = [request.to_dict()]
    if bad_cookie:
        api.client.cookies.set(SESSION_COOKIE_NAME, "invalid-session")
    path = "/api/voice" + suffix.format(id=request.id)
    response = api.client.request(method, path, json=proposal())
    assert response.status_code == 401, response.text
    assert_no_effect(api, before)


@pytest.mark.parametrize("shared", [False, True])
def test_propose_tool_signs_the_requesting_identity(voice_api, monkeypatch, shared):
    from pinky_daemon.shared_mcp import _current_agent
    from pinky_self.server import create_server

    api = voice_api
    server = create_server(
        agent_name="" if shared else "tenant",
        tool_gates=["voice"],
        signing_key_resolver=api.registry.get_signing_key,
    )
    captured = []

    def local_request(request, timeout):
        assert request.full_url.endswith("/api/voice/request")
        body = json.loads(request.data)
        headers = dict(request.header_items())
        response = api.client.post("/api/voice/request", json=body, headers=headers)
        captured.append((body, headers, response.status_code))
        assert response.status_code == 200, response.text
        result = MagicMock()
        result.__enter__.return_value = result
        result.read.return_value = response.content
        return result

    monkeypatch.setattr("pinky_self.server.urllib.request.urlopen", local_request)
    token = _current_agent.set("tenant") if shared else None
    try:
        tool = next(
            tool.fn for tool in server._tool_manager.list_tools() if tool.name == "propose_call"
        )
        result = json.loads(
            tool(
                target_name="Test contact",
                target_phone="+15551234567",
                goal="Test request",
                context={},
                fallback_behavior="hang_up",
            )
        )
    finally:
        if token is not None:
            _current_agent.reset(token)
    assert len(captured) == 1
    assert result["approval_state"] == "pending_approval"
    assert api.store.get_call_request(result["request_id"]).requested_by_agent == "tenant"
    api.dial.assert_not_awaited()


@pytest.mark.parametrize("action", ["approve", "deny", "cancel"])
def test_auto_approval_requester_still_needs_owner_for_decisions(voice_api, action):
    api = voice_api
    request = seed_request(api)
    before = [request.to_dict()]
    response = signed_post(api, f"/api/voice/request/{request.id}/{action}", {}, api.trusted)
    assert response.status_code == 403, response.text
    assert_no_effect(api, before)
