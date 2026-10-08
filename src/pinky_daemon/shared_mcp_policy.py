"""Per-message MCP policy, independent of client-side tool visibility."""

import hashlib
import hmac
from dataclasses import dataclass

from mcp import types

from pinky_daemon.isolated_policy import (
    CORE_TOOLS,
    GATE_TOOL_NAMES,
    ISOLATED_TOOL_ROUTES,
    agent_tool_gates,
    isolation_flag,
    policy_mode,
)


@dataclass(frozen=True)
class Principal:
    name: str = ""
    verified: bool = False
    credential_digest: str = ""


def credential_digest(bearer: str) -> str:
    return hashlib.sha256(bearer.encode()).hexdigest()


def install_tool_policy(mcp, mount, registry, skills, signing_key_resolver):
    """Register guarded SDK dispatch functions; never mutate shared registrations.

    The request metadata comes from the individual SSE POST / HTTP message,
    not the context inherited when a persistent session was opened.
    """
    from pinky_daemon.shared_mcp import (
        _current_agent,
        _current_dream_correlation,
        _log,
        derive_mcp_bearer,
    )

    def current():
        try:
            request = mcp.get_context().request_context.request
            principal = request.scope.get("pinky.principal", Principal())
            correlation = request.scope.get("pinky.dream_correlation", "")
        except (ValueError, AttributeError):
            return Principal(), ""
        if principal.verified:
            try:
                expected = derive_mcp_bearer(signing_key_resolver(principal.name) or "")
                valid = bool(expected) and hmac.compare_digest(
                    credential_digest(expected), principal.credential_digest
                )
            except Exception:
                valid = False
            if not valid:
                return Principal(principal.name), ""
        return principal, correlation

    def permitted(principal):
        if not principal.verified:
            return set()
        allowed = set(CORE_TOOLS[mount])
        if mount == "self":
            for gate in agent_tool_gates(principal.name, skills, registry):
                allowed.update(GATE_TOOL_NAMES[gate])
        elif mount == "memory" and isolation_flag(registry, principal.name) is False:
            # The memory server independently applies its dreamer entitlement.
            allowed.update({"reflect_for", "kg_add_for", "recall_for"})
        if mount in {"self", "messaging"} and isolation_flag(registry, principal.name) is not False:
            allowed.intersection_update(
                name for group, name in ISOLATED_TOOL_ROUTES if group == mount
            )
        return allowed

    def notice(mode, principal, operation, name):
        # Only registered names are logged, never attacker-supplied tool text.
        known = name if mcp._tool_manager.get_tool(name) else "<unknown>"
        _log(
            f"isolation: {'WOULD DENY' if mode == 'shadow' else 'DENY'} "
            f"mcp {mount} {operation} {known} for {principal.name or '<unsigned>'} "
            f"verified={principal.verified} mode={mode}"
        )

    async def list_tools():
        principal, _ = current()
        mode = policy_mode(_log)
        tools = await mcp.list_tools()
        if mode == "off":
            return tools
        allowed = permitted(principal)
        for tool in tools:
            if tool.name not in allowed:
                notice(mode, principal, "list", tool.name)
        return [t for t in tools if t.name in allowed] if mode == "enforce" else tools

    async def call_tool(name, arguments):
        principal, correlation = current()
        mode = policy_mode(_log)
        if mode != "off" and name not in permitted(principal):
            notice(mode, principal, "call", name)
            if mode == "enforce":
                return types.CallToolResult(
                    isError=True,
                    content=[types.TextContent(type="text", text="Tool denied by caller policy")],
                )
        token = _current_agent.set(principal.name)
        dream_token = _current_dream_correlation.set(correlation)
        try:
            return await mcp.call_tool(name, arguments)
        finally:
            _current_dream_correlation.reset(dream_token)
            _current_agent.reset(token)

    mcp._mcp_server.list_tools()(list_tools)
    mcp._mcp_server.call_tool(validate_input=False)(call_tool)
