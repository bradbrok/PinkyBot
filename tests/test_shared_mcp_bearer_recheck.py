"""A verified request scope must still prove its bearer at tool dispatch."""

import pytest
from mcp import types
from mcp.server.fastmcp import FastMCP
from mcp.server.lowlevel.server import request_ctx
from mcp.shared.context import RequestContext
from starlette.requests import Request

from pinky_daemon.shared_mcp import (
    AgentNameMiddleware,
    _current_agent,
    derive_mcp_bearer,
)
from pinky_daemon.shared_mcp_policy import (
    Principal,
    credential_digest,
    install_tool_policy,
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mount,tool_name", [("self", "send_to_agent"), ("messaging", "send"), ("memory", "reflect")]
)
@pytest.mark.parametrize("operation", ["list", "call"])
@pytest.mark.parametrize("credential", ["current", "rotated", "replaced-scope"])
async def test_dispatch_rechecks_scoped_bearer(
    monkeypatch, mount, tool_name, operation, credential
):
    monkeypatch.setenv("PINKY_ISOLATED_POLICY_MODE", "enforce")
    keys = {"caller": "initial-test-key"}
    executions = []
    entered = []
    server = FastMCP("credential-check")

    @server.tool(name=tool_name)
    async def act_as_agent() -> str:
        executions.append(_current_agent.get())
        return "action recorded"

    install_tool_policy(server, mount, None, None, keys.get)
    original_bearer = derive_mcp_bearer(keys["caller"])
    previous_agent = _current_agent.get()

    async def dispatch(scope, receive, send):
        entered.append(True)
        assert scope["pinky.principal"] == Principal(
            "caller", True, credential_digest(original_bearer)
        )
        assert _current_agent.get() == "caller"
        if credential == "rotated":
            keys["caller"] = "rotated-test-key"
        elif credential == "replaced-scope":
            scope["pinky.principal"] = Principal(
                "caller", True, credential_digest("untrusted-test-bearer")
            )
        assert scope["pinky.principal"].verified is True
        if credential != "current":
            assert scope["pinky.principal"].credential_digest != credential_digest(
                derive_mcp_bearer(keys["caller"])
            )

        token = request_ctx.set(
            RequestContext(
                request_id="test-message",
                meta=None,
                session=None,
                lifespan_context={},
                request=Request(scope),
            )
        )
        try:
            if operation == "list":
                response = await server._mcp_server.request_handlers[types.ListToolsRequest](
                    types.ListToolsRequest(method="tools/list")
                )
                advertised = [tool.name for tool in response.root.tools]
                assert _current_agent.get() == "caller"
                assert not executions
                assert advertised == ([tool_name] if credential == "current" else []), (
                    f"{credential} principal listed agent-acting tools: {advertised}"
                )
            else:
                response = await server._mcp_server.request_handlers[types.CallToolRequest](
                    types.CallToolRequest(
                        method="tools/call",
                        params=types.CallToolRequestParams(name=tool_name, arguments={}),
                    )
                )
                assert _current_agent.get() == "caller"
                assert executions == (["caller"] if credential == "current" else []), (
                    f"{credential} principal entered tool body: {executions}"
                )
                assert response.root.isError is (credential != "current")
                if credential != "current":
                    assert response.root.content == [
                        types.TextContent(type="text", text="Tool denied by caller policy")
                    ]
            assert _current_agent.get() == "caller"
            assert executions == (
                ["caller"] if operation == "call" and credential == "current" else []
            )
        finally:
            request_ctx.reset(token)

    middleware = AgentNameMiddleware(dispatch, signing_key_resolver=keys.get, require_auth=True)
    scope = {
        "type": "http",
        "method": "POST",
        "path": f"/mcp/{mount}/http",
        "query_string": b"",
        "headers": [
            (b"x-agent-name", b"caller"),
            (b"authorization", ("Bearer " + original_bearer).encode()),
        ],
        "server": ("127.0.0.1", 8000),
        "client": ("127.0.0.1", 12345),
    }
    await middleware(scope, None, None)
    assert entered == [True]
    assert _current_agent.get() == previous_agent
