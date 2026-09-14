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
SYSTEM_KEYS = {"mode", "armed_agents", "pending_count", "tamper_count_24h", "trust_principal_body"}
DECISION_KEYS = {
    "id", "agent_name", "session_id", "tool_use_id", "tool_name", "evaluated_permission",
    "eval_type", "rule_id", "reason_code", "principal_class", "input_sha256", "result",
    "resolved_by", "latency_ms", "created_at", "hook_sha256", "settings_sha256",
}


@contextmanager
def _gateway(tmp_path, monkeypatch, *, mode="enforce", enabled=True, isolated=False, trust=None):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PINKY_SESSION_SECRET", SECRET)
    if mode is None:
        monkeypatch.delenv("PINKY_TOOL_POLICY", raising=False)
    else:
        monkeypatch.setenv("PINKY_TOOL_POLICY", mode)
    if trust is None:
        monkeypatch.delenv("PINKY_TOOL_POLICY_TRUST_PRINCIPAL_BODY", raising=False)
    else:
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
    response = _owner(client, "PUT", "/agents/sample/policy/overrides", body={
        "pattern": "mcp__pinky-messaging__broadcast", "decision": "pause",
        "rule_id": "outbound.broadcast",
    })
    assert response.status_code == 200, response.text
    body = _body()
    response = _signed(client, "POST", "/agents/sample/policy/evaluate", body=body)
    assert response.status_code == 200, response.text
    assert response.json()["decision"] == "pause", response.text
    assert response.json()["poll_after_s"] == 25
    assert 560 <= response.json()["deadline_ts"] - time.time() <= 571
    return response.json(), body


def _binding(body):
    return dict(tool_use_id=body["tool_use_id"], input_sha256=hashlib.sha256(json.dumps(
        body["tool_input"], sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode()).hexdigest(), result="allow", reason="reviewed")


@pytest.mark.parametrize("mode,enabled,principal,decision", [
    ("off", True, "owner", "allow"), ("enforce", False, "owner", "allow"),
    ("log", True, "owner", "allow"), ("log", True, "group", "allow"),
    ("enforce", True, "owner", "deny"), ("enforce", True, "group", "deny"),
])
def test_mode_matrix_and_decision_receipts(tmp_path, monkeypatch, mode, enabled, principal, decision):
    with _gateway(tmp_path, monkeypatch, mode=mode, enabled=enabled, trust=(mode == "log")) as client:
        response = _signed(client, "POST", "/agents/sample/policy/evaluate",
                           body=_body(principal=principal))
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["decision"] == decision
        store = _store(client)
        rows = store.list_decisions("sample", 0, 10)
        if mode == "off" or not enabled:
            expected = {"decision": "allow", "mode": "off" if mode == "off" else "disabled"}
            if mode != "off":
                expected["reason_code"] = "policy_disabled"
            assert data == expected
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


@pytest.mark.parametrize("isolated", [False, True])
@pytest.mark.parametrize("mode", ["off", "enforce"])
@pytest.mark.parametrize("method,suffix", [("POST", "evaluate"), ("GET", "pending/tp_unknown")])
def test_signed_caller_cannot_target_another_agent(tmp_path, monkeypatch, isolated, mode, method, suffix):
    with _gateway(tmp_path, monkeypatch, isolated=isolated, mode=mode) as client:
        response = _signed(client, method, f"/agents/other/policy/{suffix}", body=_body())
        assert response.status_code == 403
        # A same-name control proves this is route authorization, not only the global guard.
        if method == "POST":
            control = _signed(client, method, "/agents/sample/policy/evaluate",
                              body=_body(tool="LocalComputation"))
            assert control.status_code == 200, control.text
            assert control.json()["decision"] == "allow"
        else:
            pending_id = _store(client).create_pending(
                agent_name="sample", session_id="session-1", tool_use_id="tool-1",
                tool_name="Bash", input_sha256="a" * 64, summary="test",
                created_at=time.time(), deadline_ts=time.time() + 570,
            )
            control = _signed(client, "GET", f"/agents/sample/policy/pending/{pending_id}?wait=0")
            assert control.status_code == 200, control.text
            assert control.json() == {"state": "pending"}


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
        if method == "POST":
            pause, body = _pause(client)
            path = f'/agents/sample/policy/pending/{pause["pending_id"]}/resolve'
            response = _owner(client, method, path, body=_binding(body))
            assert response.status_code == 200, response.text
            poll = _signed(client, "GET", path.removesuffix("/resolve") + "?wait=0")
            assert poll.status_code == 200
            assert set(poll.json()) == {"state", "result", "resolved_by", "reason"}
            assert poll.json()["result"] == "allow"
        elif method == "DELETE":
            store = _store(client)
            oid = store.put_override(agent="sample", pattern="Bash", rule_id=None,
                                     decision="deny", note="test", created_by="owner:test",
                                     valid_until=None)
            response = _owner(client, method, f"/agents/sample/policy/overrides/{oid}")
            assert response.status_code == 200, response.text
            assert store.list_overrides("sample", time.time()) == []
        elif method == "PUT":
            response = _owner(client, method, path, body={
                "pattern": "Bash", "decision": "deny", "note": "test",
            })
            assert response.status_code == 200, response.text
            rows = _owner(client, "GET", path)
            assert rows.status_code == 200
            assert isinstance(rows.json(), list)
            [row] = rows.json()
            assert {"id", "pattern", "decision", "note", "created_by"} <= row.keys()
            assert row["pattern"] == "Bash" and row["decision"] == "deny"
        else:
            response = _owner(client, method, path)
            assert response.status_code == 200, response.text
            if path == "/system/tool-policy":
                assert set(response.json()) == SYSTEM_KEYS
            else:
                assert response.json() == []


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
    (None, False, "owner", "deny"), (False, False, "owner", "deny"), (True, False, "owner", "pause"),
    (True, True, "owner", "deny"), (True, True, "schedule", "deny"),
])
def test_principal_body_requires_test_gate_and_isolation_still_wins(
    tmp_path, monkeypatch, trusted, isolated, principal, expected
):
    with _gateway(tmp_path, monkeypatch, trust=trusted, isolated=isolated,
                  mode="log" if trusted else "enforce") as client:
        response = _signed(client, "POST", "/agents/sample/policy/evaluate", body=_body(principal=principal))
        assert response.status_code == 200, response.text
        assert response.json()["decision"] == ("allow" if trusted else expected)
        [record] = _store(client).list_decisions("sample", 0, 10)
        assert record["evaluated_permission"] == expected
        if trusted is not True:
            [row] = _store(client).list_decisions("sample", 0, 10)
            assert row["principal_class"] == "group"


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
        resolved_rows = [r for r in _store(client).list_decisions("sample", 0, 10)
                         if r["tool_use_id"] == body["tool_use_id"] and r["result"] == "allow"]
        assert resolved_rows
        assert all(r["latency_ms"] > 0 and r["resolved_by"] == "owner:admin" for r in resolved_rows)


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
        records = [e["metadata"] for e in events if e["event_type"] == "tool_policy.decision"]
        assert any(r.get("resolution", {}).get("by") == "timeout" for r in records)


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
        system = _owner(client, "GET", "/system/tool-policy")
        assert system.status_code == 200
        assert system.json()["mode"] == "enforce"
        assert system.json()["armed_agents"] == ["sample"]
        assert system.json()["pending_count"] == 0
        assert system.json()["tamper_count_24h"] == 0
        assert set(system.json()) == SYSTEM_KEYS


def test_session_cookie_is_not_a_signed_evaluate_request(tmp_path, monkeypatch):
    with _gateway(tmp_path, monkeypatch) as client:
        assert _owner(client, "POST", "/agents/sample/policy/evaluate", body=_body()).status_code == 403


@pytest.mark.parametrize("platform,recipient,decision", [
    ("telegram", "owner-chat", "allow"), ("telegram", "approved-chat", "allow"),
    ("slack", "approved-chat", "allow"), ("telegram", "other-only", "deny"),
    ("ferry", "peer", "deny"),
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


@pytest.mark.parametrize("mode", [None, "off"])
def test_off_boot_line_is_explicit(tmp_path, monkeypatch, mode):
    messages = []
    monkeypatch.setattr("pinky_daemon.api._log", messages.append)
    with _gateway(tmp_path, monkeypatch, mode=mode):
        value = "unset" if mode is None else mode
        assert f"TOOL_POLICY INERT (PINKY_TOOL_POLICY={value})" in messages


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
        assert "TOOL_POLICY ARMED mode=log agents=[sample-a,sample-z] trust_principal_body=false" in messages
    finally:
        app.state.store_catalog.close()


@pytest.mark.parametrize("isolated", [False, True])
def test_signed_agent_can_update_unrelated_field(tmp_path, monkeypatch, isolated):
    with _gateway(tmp_path, monkeypatch, isolated=isolated) as client:
        response = _signed(client, "PUT", "/agents/sample", body={"display_name": "Updated"})
        assert response.status_code == 200, response.text
        assert client.app.state.agents.get("sample").display_name == "Updated"


def test_rule_deny_is_sanitized_and_decision_round_trips(tmp_path, monkeypatch):
    with _gateway(tmp_path, monkeypatch) as client:
        body = _body(principal="group", tool_id="distinct-tool-use-947")
        body["session_id"] = "distinct-session-382"
        body["tool_input"] = {"text": "distinct-private-payload-615"}
        response = _signed(client, "POST", "/agents/sample/policy/evaluate", body=body)
        assert response.status_code == 200, response.text
        assert set(response.json()) == {"decision", "reason", "rule_id"}
        assert response.json()["decision"] == "deny"
        assert response.json()["rule_id"] == "outbound.broadcast"
        reason = response.json()["reason"]
        assert reason.startswith("Denied by policy rule outbound.broadcast: ")
        assert "\n" not in reason
        for private in (body["session_id"], body["tool_use_id"], body["tool_input"]["text"]):
            assert private not in reason
        [stored] = _store(client).list_decisions("sample", 0, 10)
        response = _owner(client, "GET", "/agents/sample/policy/decisions?since=0&limit=10")
        assert response.status_code == 200, response.text
        assert isinstance(response.json(), list)
        [exposed] = response.json()
        for row in (stored, exposed):
            assert set(row) == DECISION_KEYS
            assert row["eval_type"] == "rule"
            assert row["rule_id"] == "outbound.broadcast"
            assert row["reason_code"] == "broadcast"
            assert row["principal_class"] == "group"
            assert row["input_sha256"] == _binding(body)["input_sha256"]
            assert row["result"] == "deny"
            assert row["latency_ms"] >= 0
            assert row["session_id"] == body["session_id"]
            assert row["tool_use_id"] == body["tool_use_id"]
        assert exposed == stored


def test_log_mode_defaults_to_full_allow_logging(tmp_path, monkeypatch):
    monkeypatch.delenv("PINKY_TOOL_POLICY_LOG_ALLOWS", raising=False)
    with _gateway(tmp_path, monkeypatch, mode="log") as client:
        response = _signed(client, "POST", "/agents/sample/policy/evaluate",
                           body=_body(tool="LocalComputation"))
        assert response.status_code == 200 and response.json()["decision"] == "allow"
        [row] = _store(client).list_decisions("sample", 0, 10)
        assert row["eval_type"] == "default"
        assert row["result"] == "allow"


def test_pending_wait_is_clamped_to_thirty_seconds(tmp_path, monkeypatch):
    # Virtualize only the route's clock/sleep; no thirty-second wall-clock test.
    import pinky_daemon.api as api_module

    with _gateway(tmp_path, monkeypatch) as client:
        pause, _ = _pause(client)
        elapsed = 0.0
        sleeps = []
        real_time = time.time()
        real_asyncio = api_module.asyncio

        async def advance(delay):
            nonlocal elapsed
            assert 0 < delay <= 5
            sleeps.append(delay)
            elapsed += delay
            assert elapsed <= 30.5, "wait was not clamped to thirty seconds"
            await real_asyncio.sleep(0)

        class Clock:
            def __getattr__(self, name):
                return getattr(time, name)

            def time(self):
                return real_time + elapsed

            def monotonic(self):
                return elapsed

        class Asyncio:
            def __getattr__(self, name):
                return getattr(real_asyncio, name)

            sleep = staticmethod(advance)

        monkeypatch.setattr(api_module, "time", Clock())
        monkeypatch.setattr(api_module, "asyncio", Asyncio())
        response = _signed(client, "GET", f'/agents/sample/policy/pending/{pause["pending_id"]}?wait=999')
        assert response.status_code == 200, response.text
        assert response.json() == {"state": "pending"}
        assert sum(sleeps) == 30


def test_default_pending_ttl_is_570_below_hook_deadline(tmp_path, monkeypatch):
    from pinky_daemon import agent_registry

    monkeypatch.delenv("PINKY_TOOL_POLICY_TTL_SEC", raising=False)
    with _gateway(tmp_path, monkeypatch) as client:
        pause, _ = _pause(client)
        row = _store(client).get_pending(pause["pending_id"])
        ttl = row["deadline_ts"] - row["created_at"]
        assert ttl == pytest.approx(570, abs=0.01)
        assert ttl < agent_registry.TOOL_POLICY_HOOK_DEADLINE_SEC == 600


@pytest.mark.parametrize("rule_id", [None, "outbound.broadcast"])
def test_owner_deny_override_names_its_source_and_forbids_retry(tmp_path, monkeypatch, rule_id):
    with _gateway(tmp_path, monkeypatch, trust=None) as client:
        created = _owner(client, "PUT", "/agents/sample/policy/overrides", body={
            "pattern": "mcp__pinky-messaging__broadcast", "decision": "deny", "rule_id": rule_id,
        })
        assert created.status_code == 200, created.text
        override_id = created.json()["id"]
        response = _signed(client, "POST", "/agents/sample/policy/evaluate", body=_body())
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["decision"] == "deny"
        assert data["override_id"] == override_id
        assert f"owner override {override_id}" in data["reason"]
        assert "do not retry" in data["reason"]
        assert "retry the same call" not in data["reason"]
        assert "unavailable" not in data["reason"]
        assert data["rule_id"] == rule_id
        if rule_id is not None:
            assert f"policy rule {rule_id}" in data["reason"]


@pytest.mark.parametrize("rule_id,status", [
    ("outbound.broadcats", 422), ("", 422), ("outbound.broadcast", 200),
])
def test_override_rule_binding_must_name_a_default_rule(tmp_path, monkeypatch, rule_id, status):
    with _gateway(tmp_path, monkeypatch) as client:
        response = _owner(client, "PUT", "/agents/sample/policy/overrides", body={
            "pattern": "mcp__pinky-messaging__broadcast", "decision": "deny", "rule_id": rule_id,
        })
        assert response.status_code == status, response.text
        if status == 422:
            assert response.json()["detail"] == "unknown rule_id"
            assert _store(client).list_overrides("sample", time.time()) == []
        else:
            assert response.json()["rule_id"] == rule_id


@pytest.mark.parametrize("kind", ["tamper", "unavailable"])
def test_non_rule_denials_keep_distinct_model_reasons(tmp_path, monkeypatch, kind):
    from pinky_daemon import api as api_module
    from pinky_daemon.tool_policy import UNAVAILABLE_REASON, evaluate

    def force_evaluation(ctx, *, now, **_kwargs):
        return evaluate(ctx, now=now, **{kind: True})

    with _gateway(tmp_path, monkeypatch) as client:
        monkeypatch.setattr(api_module, "evaluate_policy", force_evaluation)
        response = _signed(client, "POST", "/agents/sample/policy/evaluate", body=_body())
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["decision"] == "deny"
        assert "override_id" not in data
        if kind == "unavailable":
            assert data["reason"] == UNAVAILABLE_REASON
        else:
            assert "integrity" in data["reason"]
            assert "retry the same call" not in data["reason"]
            assert "unavailable" not in data["reason"]



def test_operator_pause_override_reaches_approval_without_trusted_principal(tmp_path, monkeypatch):
    with _gateway(tmp_path, monkeypatch, trust=None) as client:
        created = _owner(client, "PUT", "/agents/sample/policy/overrides", body={
            "pattern": "Bash", "decision": "pause",
        })
        assert created.status_code == 200, created.text
        body = _body(tool="Bash", principal="owner")
        body["tool_input"] = {"command": "true"}
        response = _signed(client, "POST", "/agents/sample/policy/evaluate", body=body)
        assert response.status_code == 200, response.text
        pause = response.json()
        assert pause["decision"] == "pause" and pause["pending_id"].startswith("tp_")
        [record] = _store(client).list_decisions("sample", 0, 10)
        assert record["principal_class"] == "group"
        assert record["eval_type"] == "override" and record["result"] == "pause"
        path = f'/agents/sample/policy/pending/{pause["pending_id"]}'
        resolved = _owner(client, "POST", path + "/resolve", body=_binding(body))
        assert resolved.status_code == 200, resolved.text
        polled = _signed(client, "GET", path)
        assert polled.status_code == 200
        assert polled.json()["state"] == "resolved"
        assert polled.json()["result"] == "allow"
        assert polled.json()["resolved_by"] == "owner:admin"



def test_signed_api_description_cannot_expand_owner_grant(tmp_path, monkeypatch):
    with _gateway(tmp_path, monkeypatch, trust=None) as client:
        response = _owner(client, "PUT", "/agents/sample/policy/overrides", body={
            "pattern": "Bash(git log)", "decision": "allow",
        })
        assert response.status_code == 200, response.text
        body = _body(tool="Bash", principal="unknown")
        body["tool_input"] = {"command": "rm -rf /outside"}
        before = _signed(client, "POST", "/agents/sample/policy/evaluate", body=body)
        assert before.status_code == 200 and before.json()["decision"] == "deny"
        body["tool_use_id"] = "tool-with-description"
        body["tool_input"]["description"] = "Cleanup before git log"
        after = _signed(client, "POST", "/agents/sample/policy/evaluate", body=body)
        assert after.status_code == 200 and after.json()["decision"] == "deny"


@pytest.mark.parametrize("pattern,status", [
    ("Unmapped(payload)", 422), ("Agent(payload)", 422), ("B*(git log)", 422),
    ("Bash(git log)", 200), ("mcp__pinky-messaging__send_*(peer-1)", 200),
    ("mcp__pinky-messaging__thread(message-1)", 200), ("Unmapped", 200),
])
def test_override_argument_constraints_require_a_mapped_tool(tmp_path, monkeypatch, pattern, status):
    with _gateway(tmp_path, monkeypatch) as client:
        response = _owner(client, "PUT", "/agents/sample/policy/overrides", body={
            "pattern": pattern, "decision": "allow",
        })
        assert response.status_code == status, response.text
        if status == 422:
            assert _store(client).list_overrides("sample", time.time()) == []


def test_resolution_preserves_evaluation_after_1001_other_decisions(tmp_path, monkeypatch):
    with _gateway(tmp_path, monkeypatch, trust=None) as client:
        store = _store(client)
        response = _owner(client, "PUT", "/agents/sample/policy/overrides", body={
            "pattern": "mcp__pinky-messaging__broadcast", "decision": "pause",
        })
        assert response.status_code == 200
        pause, body = _pause(client)
        original = store.list_decisions("sample", 0, 1)[0]
        other = {"evaluated_permission": "allow", "evaluation": {
            "type": "default", "reason_code": "default_allow", "principal_class": "group",
            "input_sha256": "d" * 64,
        }}
        for i in range(1001):
            store.record_decision(agent_name="sample", session_id="other-session",
                                  tool_use_id=f"other-{i}", tool_name="LocalComputation", evaluation=other)
        list_decisions = store.list_decisions

        def forbid_dashboard_scan(*_args, **_kwargs):
            raise AssertionError("resolution provenance must not use dashboard pagination")

        with monkeypatch.context() as patch:
            patch.setattr(store, "list_decisions", forbid_dashboard_scan)
            response = _owner(client, "POST", f'/agents/sample/policy/pending/{pause["pending_id"]}/resolve',
                              body=_binding(body))
        assert response.status_code == 200, response.text
        resolved = list_decisions("sample", 0, 1)[0]
        fields = ("eval_type", "rule_id", "reason_code", "principal_class", "input_sha256",
                  "hook_sha256", "settings_sha256")
        assert {k: resolved[k] for k in fields} == {k: original[k] for k in fields}


def test_expiry_inside_resolve_still_records_timeout(tmp_path, monkeypatch):
    with _gateway(tmp_path, monkeypatch) as client:
        pause, body = _pause(client)
        store = _store(client)
        resolve = store.resolve_pending

        def delayed(*args, **kwargs):
            # The route's first expiry sweep has already run; cross the deadline now.
            assert store.get_pending(pause["pending_id"])["state"] == "pending"
            deadline = time.time() + 0.1
            store._db.execute("UPDATE tool_policy_pending SET deadline_ts=? WHERE pending_id=?",
                              (deadline, pause["pending_id"]))
            store._db.commit()
            time.sleep(max(0, deadline - time.time()) + 0.05)
            return resolve(*args, **kwargs)

        monkeypatch.setattr(store, "resolve_pending", delayed)
        path = f'/agents/sample/policy/pending/{pause["pending_id"]}'
        response = _owner(client, "POST", path + "/resolve", body=_binding(body))
        assert response.status_code == 409, response.text
        assert store.get_pending(pause["pending_id"])["resolved_by"] == "timeout"
        assert _signed(client, "GET", path).json()["result"] == "deny"
        records = store.list_decisions("sample", 0, 10)
        timeouts = [r for r in records if r["resolved_by"] == "timeout"]
        assert len(timeouts) == 1 and timeouts[0]["result"] == "deny"
        events = client.app.state.session_event_store.get_for_agent("sample")
        resolutions = [e["metadata"]["resolution"] for e in events
                       if e["event_type"] == "tool_policy.decision" and "resolution" in e["metadata"]]
        assert len(resolutions) == 1
        assert resolutions[0] == {"result": "deny", "by": "timeout",
                                  "reason": "no owner decision within the approval window",
                                  "latency_ms": timeouts[0]["latency_ms"]}


def test_resolution_event_contains_reason_and_latency(tmp_path, monkeypatch):
    with _gateway(tmp_path, monkeypatch) as client:
        pause, body = _pause(client)
        events = client.app.state.session_event_store.get_for_agent("sample")
        original = next(e["metadata"] for e in events if e["event_type"] == "tool_policy.decision")
        response = _owner(client, "POST", f'/agents/sample/policy/pending/{pause["pending_id"]}/resolve',
                          body=_binding(body))
        assert response.status_code == 200
        events = client.app.state.session_event_store.get_for_agent("sample")
        metadata = next(e["metadata"] for e in events
                        if e["event_type"] == "tool_policy.decision" and "resolution" in e["metadata"])
        resolved = _store(client).list_decisions("sample", 0, 1)[0]
        assert resolved["latency_ms"] > 0
        assert metadata == {**original, "resolution": {
            "result": "allow", "by": "owner:admin", "reason": "reviewed",
            "latency_ms": resolved["latency_ms"],
        }}


@pytest.mark.parametrize("fallback", ["pending", "legacy"])
def test_resolution_missing_audit_uses_saved_provenance_or_loud_integrity_failure(
    tmp_path, monkeypatch, caplog, fallback
):
    with _gateway(tmp_path, monkeypatch) as client:
        pause, body = _pause(client)
        store = _store(client)
        original = store.list_decisions("sample", 0, 1)[0]
        row = store.get_pending(pause["pending_id"])
        for field in ("eval_type", "reason_code", "principal_class", "rule_id"):
            assert row[field] == original[field]
        store._db.execute("DELETE FROM tool_policy_decisions")
        if fallback == "legacy":
            store._db.execute("UPDATE tool_policy_pending SET eval_type=NULL, reason_code=NULL, "
                              "principal_class=NULL, rule_id=NULL")
        store._db.commit()
        response = _owner(client, "POST", f'/agents/sample/policy/pending/{pause["pending_id"]}/resolve',
                          body=_binding(body))
        assert response.status_code == 200, response.text
        [record] = store.list_decisions("sample", 0, 10)
        assert record["hook_sha256"] is record["settings_sha256"] is None
        events = client.app.state.session_event_store.get_for_agent("sample")
        metadata = next(e["metadata"] for e in events
                        if e["event_type"] == "tool_policy.decision" and "resolution" in e["metadata"])
        expected_level = "WARNING" if fallback == "pending" else "ERROR"
        assert any(r.levelname == expected_level and "sample" in r.message
                   and pause["pending_id"] in r.message for r in caplog.records)
        if fallback == "pending":
            for field in ("eval_type", "reason_code", "principal_class", "rule_id"):
                assert record[field] == original[field]
            assert "provenance" not in metadata
        else:
            assert record["eval_type"] == "unavailable"
            assert record["reason_code"] == "policy_unavailable"
            assert record["rule_id"] is None
            assert metadata["provenance"] == "missing"


def test_enforce_refuses_trusted_body_principals_at_boot(tmp_path, monkeypatch):
    with pytest.raises(ValueError, match="PINKY_TOOL_POLICY_TRUST_PRINCIPAL_BODY"):
        with _gateway(tmp_path, monkeypatch, mode="enforce", trust=True):
            pass


@pytest.mark.parametrize("trust", [None, False, True])
def test_log_boot_and_status_expose_body_trust(tmp_path, monkeypatch, trust):
    messages = []
    monkeypatch.setattr("pinky_daemon.api._log", messages.append)
    with _gateway(tmp_path, monkeypatch, mode="log", trust=trust) as client:
        response = _owner(client, "GET", "/system/tool-policy")
        assert response.status_code == 200
        assert response.json()["trust_principal_body"] is (trust is True)
        assert any("TOOL_POLICY" in line and f"trust_principal_body={str(trust is True).lower()}" in line
                   for line in messages)


@pytest.mark.parametrize("suffix", ["overrides", "decisions"])
def test_policy_reads_are_bound_to_the_agent_or_owner(tmp_path, monkeypatch, suffix):
    with _gateway(tmp_path, monkeypatch) as client:
        for agent in ("sample", "other"):
            path = f"/agents/{agent}/policy/{suffix}"
            assert _signed(client, "GET", path).status_code == (200 if agent == "sample" else 403)
            assert _owner(client, "GET", path).status_code == 200
        assert _signed(client, "GET", "/system/tool-policy").status_code == 403
        assert _owner(client, "GET", "/system/tool-policy").status_code == 200


@pytest.mark.parametrize("name,note,status", [
    ("unknown", "", 404), ("bad!name", "", 400), ("sample", "n" * 500, 200),
    ("sample", "n" * 501, 422),
])
def test_overrides_validate_agent_and_note(tmp_path, monkeypatch, name, note, status):
    with _gateway(tmp_path, monkeypatch) as client:
        response = _owner(client, "PUT", f"/agents/{name}/policy/overrides", body={
            "pattern": "Bash", "decision": "allow", "note": note,
        })
        assert response.status_code == status, response.text
        if status != 200:
            assert _store(client).list_overrides(name, 0) == []


def test_exact_static_overrides_are_rejected_but_globs_are_supported(tmp_path, monkeypatch):
    from pinky_daemon.tool_policy import STATIC_ALLOW_TOOLS

    with _gateway(tmp_path, monkeypatch) as client:
        for tool in sorted(STATIC_ALLOW_TOOLS):
            response = _owner(client, "PUT", "/agents/sample/policy/overrides", body={
                "pattern": tool, "decision": "deny",
            })
            assert response.status_code == 422, (tool, response.text)
        response = _owner(client, "PUT", "/agents/sample/policy/overrides", body={
            "pattern": "mcp__pinky-self__*", "decision": "deny",
        })
        assert response.status_code == 200, response.text


@pytest.mark.parametrize("field", ["tool_name", "session_id", "tool_use_id"])
@pytest.mark.parametrize("value,status", [("n" * 200, 200), ("n" * 201, 422), ("", 422),
                                         ("name\n", 422), ("na\x00me", 422), ("name\x7f", 422)])
def test_evaluate_identifiers_have_bounded_control_free_shape(tmp_path, monkeypatch, field, value, status):
    with _gateway(tmp_path, monkeypatch) as client:
        body = _body(tool="LocalComputation")
        body[field] = value
        response = _signed(client, "POST", "/agents/sample/policy/evaluate", body=body)
        assert response.status_code == status, response.text


@pytest.mark.parametrize("due", [False, True])
def test_pending_poll_throttles_global_writes_but_expires_due_row(tmp_path, monkeypatch, due):
    with _gateway(tmp_path, monkeypatch) as client:
        pause, _ = _pause(client)
        store = _store(client)
        calls = []
        reads = []
        get_pending = store.get_pending

        def observed_read(pending_id):
            reads.append(pending_id)
            return get_pending(pending_id)

        monkeypatch.setattr(store, "get_pending", observed_read)
        expire = store.expire_due

        def observed(now):
            calls.append(now)
            return expire(now)

        monkeypatch.setattr(store, "expire_due", observed)
        if due:
            store._db.execute("UPDATE tool_policy_pending SET deadline_ts=? WHERE pending_id=?",
                              (time.time() + 0.1, pause["pending_id"]))
            store._db.commit()
        started = time.monotonic()
        response = _signed(client, "GET", f'/agents/sample/policy/pending/{pause["pending_id"]}?wait=3')
        elapsed = time.monotonic() - started
        assert response.status_code == 200
        if due:
            assert response.json()["resolved_by"] == "timeout"
            assert elapsed < 1.5
        else:
            assert response.json() == {"state": "pending"}
            assert 3 <= elapsed < 5
            assert len(calls) <= 1
            assert reads.count(pause["pending_id"]) >= 4



def test_resolve_sweep_advances_the_global_poll_throttle(tmp_path, monkeypatch):
    import pinky_daemon.api as api_module

    with _gateway(tmp_path, monkeypatch) as client:
        pause, body = _pause(client)
        second = _signed(client, "POST", "/agents/sample/policy/evaluate", body=_body(tool_id="tool-2"))
        assert second.status_code == 200 and second.json()["decision"] == "pause"
        store = _store(client)
        calls = []
        expire = store.expire_due
        real_time = api_module.time
        # Put any preceding poll sweep beyond the five-second throttle window.
        now = real_time.monotonic() + 10

        class Clock:
            def __getattr__(self, name):
                return getattr(real_time, name)

            def monotonic(self):
                return now

        def observed(timestamp):
            calls.append(timestamp)
            return expire(timestamp)

        monkeypatch.setattr(api_module, "time", Clock())
        monkeypatch.setattr(store, "expire_due", observed)
        response = _owner(client, "POST", f'/agents/sample/policy/pending/{pause["pending_id"]}/resolve',
                          body=_binding(body))
        assert response.status_code == 200
        assert len(calls) == 1, "owner resolution performs a global expiry sweep"
        response = _signed(client, "GET", f'/agents/sample/policy/pending/{second.json()["pending_id"]}?wait=0')
        assert response.status_code == 200 and response.json() == {"state": "pending"}
        assert len(calls) == 1, "undue polling must honor the resolve-triggered sweep timestamp"


@pytest.mark.parametrize("command", [
    "git log # comment\nrm -rf /outside", "git log $(rm -rf /outside)",
])
def test_signed_bash_grant_cannot_hide_another_command(tmp_path, monkeypatch, command):
    with _gateway(tmp_path, monkeypatch) as client:
        broad = _owner(client, "PUT", "/agents/sample/policy/overrides", body={
            "pattern": "Bash", "decision": "deny",
        })
        scoped = _owner(client, "PUT", "/agents/sample/policy/overrides", body={
            "pattern": "Bash(git log)", "decision": "allow",
        })
        assert broad.status_code == scoped.status_code == 200
        # Signed evaluation only; none of these shell strings are executed.
        body = _body(tool="Bash", tool_id="safe-control")
        body["tool_input"] = {"command": "git log --oneline"}
        control = _signed(client, "POST", "/agents/sample/policy/evaluate", body=body)
        assert control.status_code == 200 and control.json()["decision"] == "allow"
        body["tool_use_id"] = "hidden-command"
        body["tool_input"] = {"command": command}
        response = _signed(client, "POST", "/agents/sample/policy/evaluate", body=body)
        assert response.status_code == 200, response.text
        assert response.json()["decision"] == "deny"
        assert response.json()["override_id"] == broad.json()["id"]
        assert response.json()["override_id"] != scoped.json()["id"]
