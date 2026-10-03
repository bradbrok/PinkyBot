"""Real MCP transport controls and tenant-policy expectations."""

import asyncio
from types import SimpleNamespace

import httpx
import pytest

from tests.isolated_policy_support import (
    assert_refused,
    auth_headers,
    names,
    protocol,
    shared_service,
)
from tests.isolated_policy_support import (
    daemon as daemon,
)

pytestmark = [pytest.mark.real_auth, pytest.mark.asyncio]


@pytest.mark.parametrize("transport", ["sse", "http"])
async def test_authenticated_nonisolated_control(daemon, monkeypatch, transport):
    d = daemon()
    async with shared_service(d, monkeypatch) as service:
        async with protocol(service, transport, auth_headers(d, "normal")) as client:
            assert "restart_daemon" in await names(client)
            result = await client.call_tool("restart_daemon", {})
            assert not result.isError
            assert len(service.outgoing) == 1


@pytest.mark.parametrize("transport", ["sse", "http"])
async def test_isolated_tool_list_is_filtered(daemon, monkeypatch, transport):
    d = daemon()
    async with shared_service(d, monkeypatch) as service:
        async with protocol(service, transport, auth_headers(d)) as client:
            advertised = await names(client)
            assert not {"restart_daemon", "add_skill"} & advertised, sorted(advertised)


@pytest.mark.parametrize("transport", ["sse", "http"])
async def test_isolated_direct_tool_call_refused(daemon, monkeypatch, transport):
    d = daemon()
    async with shared_service(d, monkeypatch) as service:
        async with protocol(service, transport, auth_headers(d)) as client:
            await assert_refused(client, "restart_daemon", {}, service)


@pytest.mark.parametrize("transport", ["sse", "http"])
async def test_unsigned_loopback_privileged_call_refused(daemon, monkeypatch, transport):
    d = daemon()
    async with shared_service(d, monkeypatch) as service:
        async with protocol(service, transport, auth_headers(d, "normal", False)) as client:
            await assert_refused(client, "restart_daemon", {}, service)


UNSIGNED_CALLS = [
    ("self", "normal", "restart_daemon", {}),
    ("self", "normal", "send_heartbeat", {}),
    (
        "messaging",
        "normal",
        "send",
        {"chat_id": "fixture", "platform": "telegram", "text": "fixture"},
    ),
    ("memory", "normal", "reflect", {"content": "fixture memory"}),
    (
        "memory",
        "dreamer",
        "reflect_for",
        {"target_agent": "peer", "content": "fixture cross memory"},
    ),
    (
        "memory",
        "dreamer",
        "kg_add_for",
        {"target_agent": "peer", "subject": "fixture", "predicate": "is", "object": "harmless"},
    ),
]


@pytest.mark.parametrize("transport", ["sse", "http"])
@pytest.mark.parametrize(
    "mount,actor,tool,arguments", UNSIGNED_CALLS, ids=[x[2] for x in UNSIGNED_CALLS]
)
async def test_unsigned_cannot_act_on_any_shared_server(
    daemon,
    monkeypatch,
    transport,
    mount,
    actor,
    tool,
    arguments,
):
    d = daemon()
    async with shared_service(d, monkeypatch) as service:
        async with protocol(service, transport, auth_headers(d, actor, False), mount) as client:
            result = await client.call_tool(tool, arguments)
    assert (
        result.isError,
        service.outgoing,
        service.signing,
        service.opened_stores,
        service.embeddings,
    ) == (True, [], [], [], [])


@pytest.mark.parametrize("transport", ["sse", "http"])
@pytest.mark.parametrize("mount", ["self", "messaging", "memory"])
async def test_unsigned_tool_discovery_is_least_privileged(daemon, monkeypatch, transport, mount):
    d = daemon()
    async with shared_service(d, monkeypatch) as service:
        async with protocol(service, transport, auth_headers(d, "normal", False), mount) as client:
            advertised = await names(client)
    assert advertised == set(), sorted(advertised)


@pytest.mark.parametrize("mount", ["self", "messaging", "memory"])
@pytest.mark.parametrize("authorization", ["", "Bearer", "Bearer ", "Basic fixture"])
@pytest.mark.parametrize("transport", ["sse", "http"])
async def test_present_malformed_credentials_are_not_absent(
    daemon, monkeypatch, mount, authorization, transport
):
    d = daemon()
    if authorization == "Bearer ":
        # HTTP clients reject trailing whitespace before sending it. Exercise
        # the actual ASGI auth layer for this state instead of counting a
        # client-side LocalProtocolError as a security refusal.
        from pinky_daemon.shared_mcp import AgentNameMiddleware

        statuses = []

        async def inner(scope, receive, send):
            await send({"type": "http.response.start", "status": 204, "headers": []})

        async def send(message):
            if message["type"] == "http.response.start":
                statuses.append(message["status"])

        app = AgentNameMiddleware(inner, signing_key_resolver=d.agents.get_signing_key)
        await app(
            {
                "type": "http",
                "client": ("127.0.0.1", 1),
                "headers": [(b"x-agent-name", b"normal"), (b"authorization", b"Bearer ")],
                "path": f"/mcp/{mount}/{transport}",
            },
            None,
            send,
        )
        assert statuses == [401]
        return
    async with shared_service(d, monkeypatch) as service:
        headers = {
            "X-Agent-Name": "normal",
            "Authorization": authorization,
            "Accept": "application/json, text/event-stream",
        }
        async with httpx.AsyncClient() as client:
            if transport == "sse":
                async with client.stream(
                    "GET", service.base + f"/mcp/{mount}/sse", headers=headers
                ) as response:
                    status = response.status_code
            else:
                response = await client.post(
                    service.base + f"/mcp/{mount}/http/mcp",
                    headers=headers,
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {
                            "protocolVersion": "2025-03-26",
                            "capabilities": {},
                            "clientInfo": {"name": "fixture", "version": "1"},
                        },
                    },
                )
                status = response.status_code
    assert status == 401, status


async def raw_call(session, headers, mount):
    request_headers = {**headers, "Accept": "application/json, text/event-stream"}
    sid = session.test_session_id()
    if sid:
        request_headers["Mcp-Session-Id"] = sid
    request_headers["MCP-Protocol-Version"] = "2025-03-26"
    tools = {
        "self": ("restart_daemon", {}),
        "messaging": ("send", {"chat_id": "fixture", "platform": "telegram", "text": "fixture"}),
        "memory": ("reflect", {"content": "fixture session probe"}),
    }
    name, arguments = tools[mount]
    async with httpx.AsyncClient(follow_redirects=False) as client:
        return await client.post(
            session.test_endpoint,
            headers=request_headers,
            json={
                "jsonrpc": "2.0",
                "id": 999,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            },
        )


@pytest.mark.parametrize("transport", ["sse", "http"])
@pytest.mark.parametrize("mount", ["self", "messaging", "memory"])
@pytest.mark.parametrize("switch", ["different-principal", "same-name-unsigned", "revoked-key"])
async def test_established_session_authentication_is_bound_and_current(
    daemon,
    monkeypatch,
    transport,
    mount,
    switch,
):
    d = daemon()
    opening = auth_headers(d, "normal")
    async with shared_service(d, monkeypatch) as service:
        async with protocol(service, transport, opening, mount) as client:
            if switch == "different-principal":
                changed = auth_headers(d, "tenant")
            elif switch == "same-name-unsigned":
                changed = auth_headers(d, "normal", False)
            else:
                d.agents._signing_keys.delete_signing_key("normal")
                d.agents.get_or_create_signing_key("normal")
                changed = opening
            response = await raw_call(client, changed, mount)
    assert response.status_code in (401, 403), response.status_code
    assert not service.outgoing and not service.opened_stores and not service.signing


@pytest.mark.parametrize("transport", ["sse", "http"])
@pytest.mark.parametrize("mount", ["self", "memory", "messaging"])
async def test_live_isolation_flag_refresh_on_established_connection(
    daemon, monkeypatch, transport, mount
):
    d = daemon()
    actor = "dreamer" if mount == "memory" else "normal"
    tool, args = (
        {
            "self": ("restart_daemon", {}),
            "memory": ("reflect_for", {"target_agent": "peer", "content": "fixture"}),
            "messaging": (
                "send",
                {"chat_id": "fixture", "platform": "telegram", "text": "fixture"},
            ),
        }
    )[mount]
    async with shared_service(d, monkeypatch) as service:
        async with protocol(service, transport, auth_headers(d, actor), mount) as client:
            before = await client.call_tool(tool, args)
            assert not before.isError, "Fixture must begin with an entitled principal"
            d.agents.update(actor, isolated=True)
            service.outgoing.clear()
            service.signing.clear()
            service.embeddings.clear()
            after = await client.call_tool(tool, args)
    if mount == "messaging":
        # Ordinary signed self-sender messaging remains permitted after the flip.
        assert not after.isError and service.signing == [actor]
    else:
        assert after.isError and not service.outgoing and not service.embeddings


@pytest.mark.parametrize("transport", ["sse", "http"])
@pytest.mark.parametrize("mount", ["self", "memory", "messaging"])
async def test_concurrent_signed_sessions_keep_separate_identities(
    daemon, monkeypatch, transport, mount
):
    d = daemon("off")
    async with shared_service(d, monkeypatch) as service:

        async def act(actor):
            async with protocol(service, transport, auth_headers(d, actor), mount) as client:
                if mount == "memory":
                    return await client.call_tool("reflect", {"content": f"fixture {actor}"})
                if mount == "messaging":
                    return await client.call_tool(
                        "send", {"chat_id": "fixture", "platform": "telegram", "text": actor}
                    )
                return await client.call_tool("send_heartbeat", {})

        results = await asyncio.gather(act("normal"), act("tenant"))
    assert all(not r.isError for r in results)
    if mount == "memory":
        assert set(service.opened_stores) == {"normal", "tenant"}
    elif mount == "messaging":
        assert set(service.signing) == {"normal", "tenant"}
    else:
        assert {p for _, p, _ in service.outgoing} == {
            "/agents/normal/heartbeat",
            "/agents/tenant/heartbeat",
        }


@pytest.mark.parametrize("transport", ["sse", "http"])
@pytest.mark.parametrize(
    "failure", ["error", "missing", "missing-attribute", "null", "string", "integer"]
)
async def test_registry_uncertainty_never_grants_shared_privilege(
    daemon,
    monkeypatch,
    transport,
    failure,
):
    d = daemon()
    headers = auth_headers(d, "normal")
    original = d.agents.get

    def lookup(name):
        if name != "normal":
            return original(name)
        if failure == "error":
            raise RuntimeError("fixture policy lookup unavailable")
        if failure == "missing":
            return None
        if failure == "missing-attribute":
            return SimpleNamespace(name="normal")
        return SimpleNamespace(
            name="normal", isolated={"null": None, "string": "false", "integer": 0}[failure]
        )

    monkeypatch.setattr(d.agents, "get", lookup)
    async with shared_service(d, monkeypatch) as service:
        async with protocol(service, transport, headers) as client:
            advertised = await names(client)
            result = await client.call_tool("restart_daemon", {})
    assert (
        bool({"restart_daemon", "add_skill"} & advertised),
        result.isError,
        service.outgoing,
    ) == (False, True, [])


@pytest.mark.parametrize("transport", ["sse", "http"])
@pytest.mark.parametrize(
    "mount,tool,args",
    [
        ("self", "send_heartbeat", {}),
        ("messaging", "send", {"chat_id": "fixture", "platform": "telegram", "text": "fixture"}),
        ("memory", "reflect", {"content": "fixture own memory"}),
    ],
)
async def test_verified_isolated_core_remains_available(
    daemon,
    monkeypatch,
    transport,
    mount,
    tool,
    args,
):
    d = daemon()
    async with shared_service(d, monkeypatch) as service:
        async with protocol(service, transport, auth_headers(d), mount) as client:
            assert tool in await names(client)
            result = await client.call_tool(tool, args)
    assert not result.isError, result
    if mount == "memory":
        assert service.opened_stores == ["tenant"]
    elif mount == "messaging":
        assert service.signing == ["tenant"] and len(service.outgoing) == 1
    else:
        assert len(service.outgoing) == 1 and service.outgoing[0][1] == "/agents/tenant/heartbeat"


@pytest.mark.parametrize("transport", ["sse", "http"])
@pytest.mark.parametrize("mode", ["off", "shadow", "enforce"])
@pytest.mark.parametrize("verified", [False, True], ids=["unsigned", "verified"])
async def test_shared_policy_modes_preserve_or_refuse_with_shadow_notice(
    daemon,
    monkeypatch,
    capsys,
    caplog,
    transport,
    mode,
    verified,
):
    d = daemon(mode)
    async with shared_service(d, monkeypatch) as service:
        async with protocol(service, transport, auth_headers(d, "tenant", verified)) as client:
            advertised = await names(client)
            result = await client.call_tool("restart_daemon", {})
    if mode == "enforce":
        assert "restart_daemon" not in advertised
        assert result.isError and not service.outgoing
    else:
        assert "restart_daemon" in advertised and not result.isError
        assert len(service.outgoing) == 1
        if mode == "shadow":
            logs = (capsys.readouterr().err + caplog.text).lower()
            assert "would" in logs and "restart_daemon" in logs, logs


@pytest.mark.parametrize("transport", ["sse", "http"])
async def test_live_flag_refresh_filters_list_without_reconnect(daemon, monkeypatch, transport):
    d = daemon()
    async with shared_service(d, monkeypatch) as service:
        async with protocol(service, transport, auth_headers(d, "normal")) as client:
            assert {"restart_daemon", "add_skill"} <= await names(client)
            d.agents.update("normal", isolated=True)
            after = await names(client)
    assert not {"restart_daemon", "add_skill"} & after


@pytest.mark.parametrize("transport", ["sse", "http"])
@pytest.mark.parametrize("mount", ["self", "messaging", "memory"])
async def test_wrong_bearer_is_rejected_without_tool_or_store_access(
    daemon,
    monkeypatch,
    transport,
    mount,
):
    d = daemon()
    headers = {
        "X-Agent-Name": "normal",
        "Authorization": "Bearer invalid",
        "Accept": "application/json, text/event-stream",
    }
    async with shared_service(d, monkeypatch) as service:
        async with httpx.AsyncClient() as client:
            if transport == "sse":
                async with client.stream(
                    "GET", service.base + f"/mcp/{mount}/sse", headers=headers
                ) as response:
                    status = response.status_code
            else:
                response = await client.post(
                    service.base + f"/mcp/{mount}/http/mcp",
                    headers=headers,
                    json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                )
                status = response.status_code
    assert status == 401
    assert not service.outgoing and not service.signing and not service.opened_stores


@pytest.mark.parametrize("mount", ["self", "messaging", "memory"])
@pytest.mark.parametrize("operation", ["DELETE", "GET"])
@pytest.mark.parametrize("principal", ["same", "different", "unsigned"])
async def test_http_session_resume_and_delete_require_bound_principal(
    daemon,
    monkeypatch,
    mount,
    operation,
    principal,
):
    d = daemon()
    original = auth_headers(d, "normal")
    async with shared_service(d, monkeypatch) as service:
        url = service.base + f"/mcp/{mount}/http/mcp"
        async with httpx.AsyncClient(timeout=5) as client:
            opened = await client.post(
                url,
                headers={**original, "Accept": "application/json, text/event-stream"},
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-03-26",
                        "capabilities": {},
                        "clientInfo": {"name": "fixture", "version": "1"},
                    },
                },
            )
            assert opened.status_code == 200
            sid = opened.headers["mcp-session-id"]
            headers = (
                original
                if principal == "same"
                else auth_headers(d, "tenant")
                if principal == "different"
                else auth_headers(d, "normal", False)
            )
            headers = {
                **headers,
                "Mcp-Session-Id": sid,
                "MCP-Protocol-Version": "2025-03-26",
                "Accept": "application/json, text/event-stream",
            }
            async with client.stream(operation, url, headers=headers) as response:
                status = response.status_code
            # Release any surviving scratch session with the rightful principal.
            await client.delete(url, headers={**headers, **original})
    assert status == (200 if principal == "same" else 403) or (
        principal != "same" and status == 401
    ), status
    assert not service.outgoing and not service.opened_stores
