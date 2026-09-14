"""Signed policy evaluation and operator-only approval/override contracts."""

from __future__ import annotations

import hashlib
import json
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

from pinky_daemon.api import create_api
from pinky_daemon.auth import (
    SESSION_COOKIE_NAME,
    build_internal_auth_headers,
    create_session_cookie,
)

pytestmark = pytest.mark.real_auth
SECRET = "policy-api-test-secret-not-for-runtime"


@contextmanager
def _gateway(tmp_path, monkeypatch, *, mode="enforce", enabled=True, isolated=False, trust=True):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PINKY_SESSION_SECRET", SECRET)
    monkeypatch.setenv("PINKY_TOOL_POLICY", mode)
    monkeypatch.setenv("PINKY_TOOL_POLICY_TRUST_PRINCIPAL_BODY", "1" if trust else "0")
    app = create_api(db_path=str(tmp_path / "memory.db"), default_working_dir=str(tmp_path))
    agents = app.state.agents
    agents.register("sample", working_dir=str(tmp_path / "sample"), isolated=isolated)
    agents.register("other", working_dir=str(tmp_path / "other"))
    if enabled:
        agents.update("sample", tool_policy_enabled=True)
    client = TestClient(app)
    try:
        yield client
    finally:
        client.close()
        app.state.store_catalog.close()


def _signed(client, method, path, *, caller="sample", body=None):
    headers = build_internal_auth_headers(
        client.app.state.agents.get_signing_key(caller), agent_name=caller, method=method, path=path,
    )
    return client.request(method, path, headers=headers, json=body)


def _owner(client, method, path, *, body=None):
    return client.request(method, path, json=body, headers={
        "cookie": f"{SESSION_COOKIE_NAME}={create_session_cookie(SECRET)}",
    })


def _body(*, tool="mcp__pinky-messaging__broadcast", principal="owner", tool_id="tool-1"):
    return dict(session_id="session-1", tool_use_id=tool_id, tool_name=tool,
                tool_input={"text": "private payload"}, transport="tmux", cwd="/untrusted",
                hook_sha256="b" * 64, settings_sha256="c" * 64, principal_class=principal)


def _store(client):
    store = getattr(client.app.state, "tool_policy_store", None)
    assert store is not None, "API must own its catalog-registered tool-policy store"
    return store


def _pause(client):
    body = _body()
    response = _signed(client, "POST", "/agents/sample/policy/evaluate", body=body)
    assert response.status_code == 200, response.text
    assert response.json()["decision"] == "pause", response.text
    return response.json(), body


def _binding(body):
    return dict(tool_use_id=body["tool_use_id"], input_sha256=hashlib.sha256(json.dumps(
        body["tool_input"], sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode()).hexdigest(), result="allow", reason="reviewed")


@pytest.mark.parametrize("mode,enabled,principal,decision", [
    ("off", True, "owner", "allow"), ("enforce", False, "owner", "allow"),
    ("log", True, "owner", "allow"), ("log", True, "group", "allow"),
    ("enforce", True, "owner", "pause"), ("enforce", True, "group", "deny"),
])
def test_mode_matrix_and_decision_receipts(tmp_path, monkeypatch, mode, enabled, principal, decision):
    with _gateway(tmp_path, monkeypatch, mode=mode, enabled=enabled) as client:
        response = _signed(client, "POST", "/agents/sample/policy/evaluate",
                           body=_body(principal=principal))
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["decision"] == decision
        store = _store(client)
        rows = store.list_decisions("sample", 0, 10)
        if mode == "off" or not enabled:
            assert data == {"decision": "allow", "mode": "off" if mode == "off" else "disabled"}
            assert rows == []
            assert store.count_pending() == 0
            assert store._db.execute("SELECT count(*) FROM tool_policy_allow_counts").fetchone()[0] == 0
        else:
            assert len(rows) == 1
            assert rows[0]["hook_sha256"] == "b" * 64
            assert rows[0]["settings_sha256"] == "c" * 64
            if mode == "log":
                assert rows[0]["result"] == ("would_pause" if principal == "owner" else "would_deny")
                assert store.count_pending() == 0
            else:
                events = client.app.state.session_event_store.get_for_agent("sample")
                assert any(e["event_type"] == "tool_policy.decision" for e in events)
                if decision == "pause":
                    assert data["poll_after_s"] == 25
                    assert 568 <= data["deadline_ts"] - time.time() <= 571
                    assert store.count_pending() == 1


@pytest.mark.parametrize("isolated", [False, True])
@pytest.mark.parametrize("mode", ["off", "enforce"])
@pytest.mark.parametrize("method,suffix", [("POST", "evaluate"), ("GET", "pending/tp_unknown")])
def test_signed_caller_cannot_target_another_agent(tmp_path, monkeypatch, isolated, mode, method, suffix):
    with _gateway(tmp_path, monkeypatch, isolated=isolated, mode=mode) as client:
        response = _signed(client, method, f"/agents/other/policy/{suffix}", body=_body())
        assert response.status_code == 403


@pytest.mark.parametrize("method,path", [
    ("POST", "/agents/sample/policy/pending/tp_unknown/resolve"),
    ("PUT", "/agents/sample/policy/overrides"),
    ("DELETE", "/agents/sample/policy/overrides/1"),
    ("GET", "/agents/sample/policy/overrides"),
    ("GET", "/agents/sample/policy/decisions"),
    ("GET", "/system/tool-policy"),
])
def test_admin_routes_require_authentication(tmp_path, monkeypatch, method, path):
    with _gateway(tmp_path, monkeypatch) as client:
        assert client.request(method, path, json={}).status_code == 401


@pytest.mark.parametrize("isolated", [False, True])
@pytest.mark.parametrize("method,path,body", [
    ("PUT", "/agents/sample/policy/overrides", {"pattern": "Bash", "decision": "allow"}),
    ("DELETE", "/agents/sample/policy/overrides/1", None),
    ("POST", "/agents/sample/policy/pending/tp_unknown/resolve", {"result": "allow",
     "tool_use_id": "tool-1", "input_sha256": "a" * 64}),
    ("PUT", "/agents/sample", {"tool_policy_enabled": False}),
])
def test_internal_signer_cannot_mutate_own_policy(tmp_path, monkeypatch, isolated, method, path, body):
    with _gateway(tmp_path, monkeypatch, isolated=isolated) as client:
        response = _signed(client, method, path, body=body)
        assert response.status_code == 403, response.text


def test_operator_can_enable_but_registration_never_enables(tmp_path, monkeypatch):
    with _gateway(tmp_path, monkeypatch, enabled=False) as client:
        agents = client.app.state.agents
        for name in ("sample", "fresh"):
            result = agents.register(name, working_dir=str(tmp_path / name), tool_policy_enabled=True)
            assert result.to_dict().get("tool_policy_enabled") is False
        response = _owner(client, "PUT", "/agents/sample", body={"tool_policy_enabled": True})
        assert response.status_code == 200, response.text
        assert agents.get("sample").tool_policy_enabled is True
        response = _owner(client, "PUT", "/agents/sample", body={"tool_policy_enabled": False})
        assert response.status_code == 200
        assert agents.get("sample").tool_policy_enabled is False


@pytest.mark.parametrize("trusted,isolated,principal,expected", [
    (False, False, "owner", "deny"), (True, False, "owner", "pause"),
    (True, True, "owner", "deny"), (True, True, "schedule", "deny"),
])
def test_principal_body_requires_test_gate_and_isolation_still_wins(
    tmp_path, monkeypatch, trusted, isolated, principal, expected
):
    with _gateway(tmp_path, monkeypatch, trust=trusted, isolated=isolated) as client:
        response = _signed(client, "POST", "/agents/sample/policy/evaluate", body=_body(principal=principal))
        assert response.status_code == 200, response.text
        assert response.json()["decision"] == expected


def test_resolve_binding_repeat_unknown_and_cross_agent(tmp_path, monkeypatch):
    with _gateway(tmp_path, monkeypatch) as client:
        pause, body = _pause(client)
        path = f'/agents/sample/policy/pending/{pause["pending_id"]}'
        binding = _binding(body)
        for field in ("tool_use_id", "input_sha256"):
            wrong = {**binding, field: "wrong" if field == "tool_use_id" else "d" * 64}
            assert _owner(client, "POST", path + "/resolve", body=wrong).status_code == 422
        assert _signed(client, "GET", path.replace("/sample/", "/other/"), caller="other").status_code == 404
        assert _owner(client, "POST", path + "/resolve", body=binding).status_code == 200
        assert _owner(client, "POST", path + "/resolve", body=binding).status_code == 409
        assert _owner(client, "POST", "/agents/sample/policy/pending/tp_unknown/resolve",
                      body=binding).status_code == 404
        resolved = _signed(client, "GET", path).json()
        assert resolved["state"] == "resolved" and resolved["result"] == "allow"
        events = client.app.state.session_event_store.get_for_agent("sample")
        records = [e["metadata"] for e in events if e["event_type"] == "tool_policy.decision"]
        assert any(r.get("resolution", {}).get("result") == "allow" for r in records)


def test_pending_long_poll_returns_when_operator_resolves(tmp_path, monkeypatch):
    with _gateway(tmp_path, monkeypatch) as client:
        pause, body = _pause(client)
        path = f'/agents/sample/policy/pending/{pause["pending_id"]}'
        with ThreadPoolExecutor(max_workers=1) as pool:
            waiting = pool.submit(_signed, client, "GET", path + "?wait=5")
            time.sleep(0.1)
            assert _owner(client, "POST", path + "/resolve", body=_binding(body)).status_code == 200
            response = waiting.result(timeout=3)
        assert response.status_code == 200
        assert response.json()["result"] == "allow"


def test_pending_poll_expires_without_background_task(tmp_path, monkeypatch):
    with _gateway(tmp_path, monkeypatch) as client:
        pause, _ = _pause(client)
        store = _store(client)
        store._db.execute("UPDATE tool_policy_pending SET deadline_ts=? WHERE pending_id=?",
                          (time.time() + 0.1, pause["pending_id"]))
        store._db.commit()
        response = _signed(client, "GET", f'/agents/sample/policy/pending/{pause["pending_id"]}?wait=3')
        assert response.status_code == 200
        assert response.json() == {
            "state": "resolved", "result": "deny", "resolved_by": "timeout",
            "reason": "no owner decision within the approval window",
        }
        assert store.get_pending(pause["pending_id"])["result"] == "deny"
        events = client.app.state.session_event_store.get_for_agent("sample")
        assert any(e["metadata"].get("resolution", {}).get("by") == "timeout" for e in events)


def test_operator_override_and_read_surfaces(tmp_path, monkeypatch):
    with _gateway(tmp_path, monkeypatch) as client:
        path = "/agents/sample/policy/overrides"
        response = _owner(client, "PUT", path, body={
            "pattern": "mcp__pinky-messaging__broadcast", "decision": "allow", "note": "reviewed",
        })
        assert response.status_code == 200, response.text
        decision = _signed(client, "POST", "/agents/sample/policy/evaluate", body=_body())
        assert decision.status_code == 200 and decision.json()["decision"] == "allow"
        assert _owner(client, "GET", path).status_code == 200
        assert _signed(client, "GET", "/agents/sample/policy/decisions").status_code == 200
        system = _signed(client, "GET", "/system/tool-policy")
        assert system.status_code == 200
        assert system.json()["mode"] == "enforce"
        assert system.json()["armed_agents"] == ["sample"]
        assert system.json()["pending_count"] == 0
        assert system.json()["tamper_count_24h"] == 0
        assert system.json()["activation"] == "next_launch"


def test_session_cookie_is_not_a_signed_evaluate_request(tmp_path, monkeypatch):
    with _gateway(tmp_path, monkeypatch) as client:
        assert _owner(client, "POST", "/agents/sample/policy/evaluate", body=_body()).status_code == 403


@pytest.mark.parametrize("platform,recipient,decision", [
    ("telegram", "owner-chat", "allow"), ("telegram", "approved-chat", "allow"),
    ("slack", "approved-chat", "pause"), ("telegram", "other-only", "pause"),
    ("ferry", "peer", "pause"),
])
def test_registry_recipient_scope_is_authoritative(tmp_path, monkeypatch, platform, recipient, decision):
    with _gateway(tmp_path, monkeypatch) as client:
        agents = client.app.state.agents
        agents.set_owner_notification_destinations([{
            "platform": "telegram", "account_id": "test-account", "conversation_id": "owner-chat",
            "principal_id": "owner-principal",
        }])
        agents.approve_user("sample", "approved-chat")
        agents.approve_user("other", "other-only")
        body = _body(tool="mcp__pinky-messaging__send")
        body["tool_input"] = {"platform": platform, "chat_id": recipient, "text": "private"}
        body["known_recipients"] = [f"{platform}:{recipient}"]  # client cannot grant itself a recipient
        response = _signed(client, "POST", "/agents/sample/policy/evaluate", body=body)
        assert response.status_code == 200, response.text
        assert response.json()["decision"] == decision


@pytest.mark.parametrize("log_allows", [False, True])
def test_default_allow_counter_or_full_record(tmp_path, monkeypatch, log_allows):
    monkeypatch.setenv("PINKY_TOOL_POLICY_LOG_ALLOWS", "1" if log_allows else "0")
    with _gateway(tmp_path, monkeypatch) as client:
        response = _signed(client, "POST", "/agents/sample/policy/evaluate",
                           body=_body(tool="LocalComputation"))
        assert response.status_code == 200 and response.json()["decision"] == "allow"
        store = _store(client)
        assert len(store.list_decisions("sample", 0, 10)) == (1 if log_allows else 0)
        if not log_allows:
            assert store._db.execute("SELECT sum(n) FROM tool_policy_allow_counts").fetchone()[0] == 1


def test_negative_wait_is_nonblocking_and_unknown_pending_is_404(tmp_path, monkeypatch):
    with _gateway(tmp_path, monkeypatch) as client:
        pause, _ = _pause(client)
        started = time.monotonic()
        response = _signed(client, "GET", f'/agents/sample/policy/pending/{pause["pending_id"]}?wait=-5')
        assert time.monotonic() - started < 1
        assert response.status_code == 200 and response.json() == {"state": "pending"}
        assert _signed(client, "GET", "/agents/sample/policy/pending/tp_unknown").status_code == 404


@pytest.mark.parametrize("variable,value", [
    ("PINKY_TOOL_POLICY_TTL_SEC", "0"), ("PINKY_TOOL_POLICY_TTL_SEC", "600"),
    ("PINKY_TOOL_POLICY_TTL_SEC", "wrong"), ("PINKY_TOOL_POLICY", "enfroce"),
])
def test_invalid_policy_configuration_fails_before_serving(tmp_path, monkeypatch, variable, value):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(variable, value)
    with pytest.raises(ValueError, match=variable):
        create_api(db_path=str(tmp_path / "memory.db"), default_working_dir=str(tmp_path))


def test_off_boot_line_is_explicit(tmp_path, monkeypatch):
    messages = []
    monkeypatch.setattr("pinky_daemon.api._log", messages.append)
    with _gateway(tmp_path, monkeypatch, mode="off"):
        assert "TOOL_POLICY INERT (PINKY_TOOL_POLICY=off)" in messages


def test_armed_boot_line_sorts_only_enabled_agents(tmp_path, monkeypatch):
    from pinky_daemon.agent_registry import AgentRegistry

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PINKY_TOOL_POLICY", "log")
    registry = AgentRegistry(str(tmp_path / "memory_agents.db"))
    try:
        for name in ("sample-z", "sample-a", "disabled"):
            registry.register(name, working_dir=str(tmp_path / name))
            registry.update(name, tool_policy_enabled=(name != "disabled"))
    finally:
        registry.close()
    messages = []
    monkeypatch.setattr("pinky_daemon.api._log", messages.append)
    app = create_api(db_path=str(tmp_path / "memory.db"), default_working_dir=str(tmp_path))
    try:
        assert "TOOL_POLICY ARMED mode=log agents=[sample-a,sample-z]" in messages
    finally:
        app.state.store_catalog.close()
