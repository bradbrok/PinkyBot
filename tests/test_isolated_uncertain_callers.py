"""Cross-target access requires an explicit non-isolated caller decision."""

import inspect
from copy import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from pinky_daemon.auth import (
    SESSION_COOKIE_NAME,
    build_internal_auth_headers,
    create_session_cookie,
)
from tests.conftest import TEST_SESSION_SECRET
from tests.isolated_policy_support import closure
from tests.isolated_policy_support import daemon as daemon

pytestmark = pytest.mark.real_auth
MODES = ("off", "shadow", "enforce")
UNCERTAIN = ("unregistered", "missing", "absent_flag", "null", "zero", "false_string", "one")


@pytest.fixture
def boundary(daemon, monkeypatch):
    def build(mode, state):
        d = daemon(mode)
        d.agents.update("tenant", groups=["shared"])
        d.agents.update("peer", groups=["shared"])
        caller = "unknown" if state == "unregistered" else "tenant"
        key = d.agents.get_or_create_signing_key(caller)
        original = d.agents.get
        row = copy(original("tenant"))
        flags = {"null": None, "zero": 0, "false_string": "false", "one": 1,
                 "isolated": True, "normal": False}
        if state == "absent_flag":
            row = SimpleNamespace(**{k: v for k, v in vars(row).items() if k != "isolated"})
        elif state in flags:
            row.isolated = flags[state]

        def get(name):
            if name != caller or state == "unregistered":
                return original(name)
            if state == "error":
                raise RuntimeError("synthetic registry failure")
            return None if state == "missing" else row

        monkeypatch.setattr(d.agents, "get", get)
        broker = inspect.getclosurevars(closure(d.app, "send_agent_message_direct")).nonlocals["broker"]
        delivery = AsyncMock(return_value=(True, True))
        monkeypatch.setattr(broker, "inject_agent_message", delivery)
        client = TestClient(d.app)

        def request(method, path, body=None):
            headers = build_internal_auth_headers(key, agent_name=caller, method=method, path=path)
            return client.request(method, path, headers=headers, json=body)

        return SimpleNamespace(d=d, client=client, caller=caller, request=request, delivery=delivery)

    clients = []

    def tracked(*args):
        result = build(*args)
        clients.append(result.client)
        return result

    yield tracked
    for client in clients:
        client.close()


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("state", UNCERTAIN)
@pytest.mark.parametrize("surface", ["health", "heartbeat"])
def test_uncertain_caller_cannot_read_or_write_peer(boundary, mode, state, surface):
    b = boundary(mode, state)
    path = f"/agents/peer/{surface}"
    before = b.d.agents.get_latest_heartbeat("peer")
    response = b.request("GET" if surface == "health" else "POST", path,
                         None if surface == "health" else {"status": "healthy"})
    assert response.status_code == 403, response.text
    assert response.json()["agent"] == b.caller
    assert response.json()["error"] == "isolated agent may only access its own resources"
    assert b.d.agents.get_latest_heartbeat("peer") == before
    b.delivery.assert_not_awaited()


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("state", UNCERTAIN)
def test_uncertain_caller_cannot_use_shared_group_message(boundary, mode, state):
    b = boundary(mode, state)
    response = b.request("POST", "/agents/peer/message",
                         {"from_agent": b.caller, "message": "example"})
    assert response.status_code == 403, response.text
    b.delivery.assert_not_awaited()


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("state", ["isolated", "normal"])
def test_literal_boolean_cross_target_controls(boundary, mode, state):
    b = boundary(mode, state)
    expected = 403 if state == "isolated" else 200
    assert b.request("GET", "/agents/peer/health").status_code == expected
    response = b.request("POST", "/agents/peer/heartbeat", {"status": "healthy"})
    assert response.status_code == expected, response.text
    heartbeat = b.d.agents.get_latest_heartbeat("peer")
    assert (heartbeat is not None) is (state == "normal")


@pytest.mark.parametrize("mode", MODES)
def test_literal_true_shared_group_message_still_delivers(boundary, mode):
    b = boundary(mode, "isolated")
    response = b.request("POST", "/agents/peer/message",
                         {"from_agent": "tenant", "message": "example"})
    assert response.status_code == 200, response.text
    assert response.json()["delivered"] is True
    assert response.json()["confirmed"] is True
    b.delivery.assert_awaited_once_with("tenant", "peer", "example")


@pytest.mark.parametrize("mode", MODES)
def test_unknown_self_and_non_target_requests_reach_handler(boundary, mode):
    b = boundary(mode, "unregistered")
    response = b.request("GET", "/agents/unknown/health")
    assert response.status_code == 404
    assert response.json() == {"detail": "Agent 'unknown' not found"}
    assert b.request("GET", "/tasks/stats").status_code == 200


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("surface", ["health", "heartbeat", "message"])
def test_registry_exception_denies_cross_target(boundary, mode, surface):
    b = boundary(mode, "error")
    body = {"from_agent": "tenant", "message": "example"} if surface == "message" else {"status": "healthy"}
    response = b.request("GET" if surface == "health" else "POST", f"/agents/peer/{surface}",
                         None if surface == "health" else body)
    assert response.status_code == 403, response.text
    b.delivery.assert_not_awaited()


@pytest.mark.parametrize("mode", MODES)
def test_browser_and_unsigned_cross_target_controls(boundary, mode):
    b = boundary(mode, "unregistered")
    assert b.client.get("/agents/peer/health").status_code == 401
    assert b.client.post("/agents/peer/heartbeat", json={"status": "healthy"}).status_code == 401
    b.client.cookies.set(SESSION_COOKIE_NAME, create_session_cookie(TEST_SESSION_SECRET))
    assert b.client.get("/agents/peer/health").status_code == 200
    assert b.client.post("/agents/peer/heartbeat", json={"status": "healthy"}).status_code == 200
