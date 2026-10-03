"""Supplemental configuration, registration and dependency-wiring controls.

The sealed RED overlay is unchanged. These controls pin the real callsites
used by deletion mutants, including nonisolated availability on fail-closed
dependency omission.
"""

import inspect
import json

import httpx
import pytest

from pinky_daemon import api
from pinky_daemon.isolated_policy import (
    ALL_TOOL_GATES,
    CORE_TOOLS,
    GATE_TOOL_NAMES,
    ISOLATED_MUTATION_ALLOW,
)
from pinky_daemon.shared_mcp import AgentNameMiddleware
from tests import isolated_policy_support as support
from tests.isolated_policy_support import auth_headers, names, protocol, shared_service
from tests.isolated_policy_support import daemon as daemon
from tests.test_isolated_allow_positives import ALLOW_PAIRS
from tests.test_isolated_launch_policy import DENIED, RUNTIMES, prepare

pytestmark = pytest.mark.real_auth


def test_allow_keys_are_exactly_the_independent_25():
    assert ISOLATED_MUTATION_ALLOW == frozenset(ALLOW_PAIRS)


@pytest.mark.parametrize("gate", [None, *ALL_TOOL_GATES])
def test_self_registration_groups_match_policy(gate):
    from pinky_self.server import create_server

    server = create_server(tool_gates=[gate] if gate else [])
    actual = {tool.name for tool in server._tool_manager.list_tools()}
    expected = CORE_TOOLS["self"] | set(GATE_TOOL_NAMES[gate] if gate else [])
    assert actual == expected


@pytest.mark.parametrize("actor", ["tenant", "normal"])
def test_stdio_configuration_preserves_entitled_nonisolated_tools(daemon, monkeypatch, actor):
    from pinky_self.server import create_server

    d = daemon()
    monkeypatch.setattr(api, "SHARED_MCP_ENABLED", False)
    work = d.root / actor
    api._write_mcp_json(work, actor, agent_registry=d.agents, skill_store=d.skills)
    args = json.loads((work / ".mcp.json").read_text())["mcpServers"]["pinky-self"]["args"]
    gates = args[args.index("--tool-gates") + 1].split(",") if "--tool-gates" in args else []
    server = create_server(agent_name=actor, tool_gates=gates)
    advertised = {tool.name for tool in server._tool_manager.list_tools()}
    assert ("restart_daemon" in advertised, "add_skill" in advertised) == (
        actor == "normal",
        actor == "normal",
    )


@pytest.mark.parametrize("runtime", RUNTIMES)
async def test_runtime_keeps_entitled_nonisolated_tools(daemon, monkeypatch, runtime):
    d = daemon()
    session, _, _ = await prepare(d, monkeypatch, runtime, name="normal")
    assert not DENIED.intersection(session._config.disallowed_tools)


@pytest.mark.parametrize("actor", ["tenant", "normal"])
async def test_api_gateway_factory_wires_live_policy(daemon, monkeypatch, actor):
    d = daemon()
    startup = next(fn for fn in d.app.router.on_startup if fn.__name__ == "on_startup")
    factory = inspect.getclosurevars(startup).nonlocals["_create_shared_mcp_manager"]
    manager = factory()  # Actual API dependency wiring, without starting daemon tasks.
    # Reuse only the scratch socket/transport recorder from the sealed fixture.
    monkeypatch.setattr(support, "SharedMcpManager", lambda **kwargs: manager)
    async with shared_service(d, monkeypatch) as service:
        async with protocol(service, "http", auth_headers(d, actor)) as client:
            advertised = await names(client)
            result = await client.call_tool("restart_daemon", {})
    assert ("restart_daemon" in advertised) == (actor == "normal")
    assert result.isError == (actor == "tenant")
    assert len(service.outgoing) == (1 if actor == "normal" else 0)


@pytest.mark.parametrize("value", [b"", b"Basic value", b"Bearer ", b"Bearer \xff", b"Bearer a b"])
@pytest.mark.parametrize("mode", ["off", "shadow", "enforce"])
async def test_malformed_authentication_never_downgrades(monkeypatch, value, mode):
    monkeypatch.setenv("PINKY_ISOLATED_POLICY_MODE", mode)
    visited = []
    statuses = []

    async def inner(scope, receive, send):
        visited.append(True)

    async def send(message):
        if message["type"] == "http.response.start":
            statuses.append(message["status"])

    app = AgentNameMiddleware(inner, signing_key_resolver=lambda name: "fixture-key")
    await app(
        {
            "type": "http",
            "client": ("127.0.0.1", 1),
            "path": "/mcp/self/sse",
            "headers": [(b"x-agent-name", b"normal"), (b"authorization", value)],
        },
        None,
        send,
    )
    assert (statuses, visited) == ([401], [])


@pytest.mark.parametrize("transport", ["sse", "http"])
async def test_duplicate_session_identifiers_cannot_choose_another_principal(
    daemon, monkeypatch, transport
):
    d = daemon()
    async with shared_service(d, monkeypatch) as service:
        async with (
            protocol(service, transport, auth_headers(d, "normal")) as victim,
            protocol(service, transport, auth_headers(d, "tenant")) as caller,
        ):
            headers = list(auth_headers(d, "tenant").items()) + [
                ("Accept", "application/json, text/event-stream"),
                ("MCP-Protocol-Version", "2025-03-26"),
            ]
            if transport == "http":
                headers.extend(
                    [
                        ("Mcp-Session-Id", victim.test_session_id()),
                        ("Mcp-Session-Id", caller.test_session_id()),
                    ]
                )
                url = caller.test_endpoint
            else:
                victim_id = victim.test_endpoint.split("session_id=", 1)[1]
                url = caller.test_endpoint + "&session_id=" + victim_id
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    url,
                    headers=headers,
                    json={
                        "jsonrpc": "2.0",
                        "id": 100,
                        "method": "tools/call",
                        "params": {"name": "send_heartbeat", "arguments": {}},
                    },
                )
    assert response.status_code in (401, 403)
    assert not service.outgoing
