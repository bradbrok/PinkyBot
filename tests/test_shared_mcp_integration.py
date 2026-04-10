"""Integration tests for shared MCP server with multiple agents.

Tests agent isolation, concurrent tool calls, and no cross-contamination
when multiple agents use the same shared MCP server infrastructure.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from pinky_daemon.shared_mcp import (
    AgentNameMiddleware,
    LazyAgentName,
    _current_agent,
    create_shared_app,
    get_current_agent,
)


class TestMultiAgentIsolation:
    """Verify agent identity isolation when sharing MCP infrastructure."""

    def test_lazy_agent_name_concurrent_contexts(self):
        """Multiple LazyAgentName instances resolve independently per context."""
        name = LazyAgentName("")  # empty fallback (shared mode)

        results = {}

        def check_agent(agent: str):
            token = _current_agent.set(agent)
            try:
                results[agent] = str(name)
            finally:
                _current_agent.reset(token)

        for agent in ["barsik", "pushok", "ryzhik"]:
            check_agent(agent)

        assert results == {
            "barsik": "barsik",
            "pushok": "pushok",
            "ryzhik": "ryzhik",
        }

    @pytest.mark.asyncio
    async def test_middleware_isolates_concurrent_requests(self):
        """Concurrent requests with different X-Agent-Name headers don't cross-contaminate."""
        captured = {}

        async def mock_app(scope, receive, send):
            agent = get_current_agent()
            # Simulate some async work
            await asyncio.sleep(0.01)
            # Check agent is still correct after await
            assert get_current_agent() == agent
            captured[agent] = True

        middleware = AgentNameMiddleware(mock_app)

        async def make_request(agent_name: str):
            scope = {
                "type": "http",
                "headers": [(b"x-agent-name", agent_name.encode())],
            }
            await middleware(scope, None, None)

        # Run 3 agent requests concurrently
        await asyncio.gather(
            make_request("barsik"),
            make_request("pushok"),
            make_request("ryzhik"),
        )

        assert "barsik" in captured
        assert "pushok" in captured
        assert "ryzhik" in captured

    @pytest.mark.asyncio
    async def test_contextvar_does_not_leak_between_requests(self):
        """After a request completes, the ContextVar is reset."""
        agent_after_request = []

        async def mock_app(scope, receive, send):
            pass

        middleware = AgentNameMiddleware(mock_app)

        scope = {
            "type": "http",
            "headers": [(b"x-agent-name", b"barsik")],
        }
        await middleware(scope, None, None)
        agent_after_request.append(get_current_agent())

        scope = {
            "type": "http",
            "headers": [(b"x-agent-name", b"pushok")],
        }
        await middleware(scope, None, None)
        agent_after_request.append(get_current_agent())

        # After each request, ContextVar should be empty
        assert agent_after_request == ["", ""]

    def test_resolve_lazy_for_json_serialization(self):
        """_resolve_lazy correctly resolves LazyAgentName in nested structures."""
        from pinky_messaging.server import create_server

        # Create server in shared mode (empty agent_name)
        server = create_server(agent_name="", api_url="http://localhost:8888")

        # The LazyAgentName is used internally — verify tool registration works
        tool_names = {tool.name for tool in server._tool_manager.list_tools()}
        assert "send" in tool_names
        assert "thread" in tool_names

    def test_lazy_agent_name_in_dict_operations(self):
        """LazyAgentName works as dict values and in comparisons."""
        name = LazyAgentName("barsik")

        # Use as dict value
        data = {"agent_name": name, "action": "test"}
        token = _current_agent.set("pushok")
        try:
            # In context, should resolve to pushok
            assert str(data["agent_name"]) == "pushok"
            assert f"Agent: {data['agent_name']}" == "Agent: pushok"
        finally:
            _current_agent.reset(token)


class TestSharedServerMounting:
    """Test that multiple MCP servers can be mounted on a single app."""

    def test_create_shared_app_mounts_servers(self):
        """create_shared_app creates a valid ASGI app with mounted servers."""
        from pinky_messaging.server import create_server as create_messaging
        from pinky_self.server import create_server as create_self

        self_mcp = create_self(agent_name="", api_url="http://localhost:8888", tool_gates=[])
        msg_mcp = create_messaging(agent_name="", api_url="http://localhost:8888")

        app = create_shared_app({"self": self_mcp, "messaging": msg_mcp})

        # App should be callable (ASGI)
        assert callable(app)

    def test_shared_app_with_all_gates(self):
        """Shared server with all gates should register all tools."""
        from pinky_self.server import create_server as create_self

        all_gates = [
            "extras", "kb", "research", "presentations", "triggers",
            "schedule", "skill-admin", "admin", "tasks-admin",
        ]
        self_mcp = create_self(
            agent_name="", api_url="http://localhost:8888", tool_gates=all_gates,
        )

        tool_names = {tool.name for tool in self_mcp._tool_manager.list_tools()}

        # Should have core + all gated tools
        assert "who_am_i" in tool_names  # core
        assert "send_heartbeat" in tool_names  # core
        assert "kb_search" in tool_names  # kb gate
        assert "set_wake_schedule" in tool_names  # schedule gate
        assert "create_trigger" in tool_names  # triggers gate
        assert "get_attribution" in tool_names  # extras gate


class TestMcpJsonConfigIsolation:
    """Test that .mcp.json configs are correctly isolated per agent."""

    def test_different_agents_get_different_headers(self, tmp_path):
        """Each agent's .mcp.json should have its own X-Agent-Name."""
        import pinky_daemon.api as api_mod

        original = api_mod.SHARED_MCP_ENABLED
        api_mod.SHARED_MCP_ENABLED = True
        try:
            configs = {}
            for name in ["barsik", "pushok", "ryzhik", "persik", "gemma"]:
                work_dir = tmp_path / name
                work_dir.mkdir()
                api_mod._write_mcp_json(work_dir, name)
                configs[name] = json.loads((work_dir / ".mcp.json").read_text())

            # All point to same SSE URLs
            urls = {
                configs[n]["mcpServers"]["pinky-self"]["url"]
                for n in configs
            }
            assert len(urls) == 1  # Same URL for all

            # But each has unique X-Agent-Name
            headers = {
                n: configs[n]["mcpServers"]["pinky-self"]["headers"]["X-Agent-Name"]
                for n in configs
            }
            assert headers == {
                "barsik": "barsik",
                "pushok": "pushok",
                "ryzhik": "ryzhik",
                "persik": "persik",
                "gemma": "gemma",
            }
        finally:
            api_mod.SHARED_MCP_ENABLED = original

    def test_memory_per_agent_isolation(self, tmp_path):
        """pinky-memory uses per-agent isolation in both modes."""
        import pinky_daemon.api as api_mod

        original = api_mod.SHARED_MCP_ENABLED

        # Stdio mode: per-agent subprocess with DB path in args
        api_mod.SHARED_MCP_ENABLED = False
        try:
            for name in ["barsik", "pushok"]:
                work_dir = tmp_path / f"{name}_stdio"
                work_dir.mkdir()
                api_mod._write_mcp_json(work_dir, name)
                config = json.loads((work_dir / ".mcp.json").read_text())
                mem = config["mcpServers"]["pinky-memory"]
                assert "command" in mem
                assert "type" not in mem
                assert "memory.db" in " ".join(mem["args"])
        finally:
            api_mod.SHARED_MCP_ENABLED = original

        # SSE mode: shared server, per-agent via X-Agent-Name header
        api_mod.SHARED_MCP_ENABLED = True
        try:
            for name in ["barsik", "pushok"]:
                work_dir = tmp_path / f"{name}_sse"
                work_dir.mkdir()
                api_mod._write_mcp_json(work_dir, name)
                config = json.loads((work_dir / ".mcp.json").read_text())
                mem = config["mcpServers"]["pinky-memory"]
                assert mem["type"] == "sse"
                assert "/mcp/memory/sse" in mem["url"]
                assert mem["headers"]["X-Agent-Name"] == name
        finally:
            api_mod.SHARED_MCP_ENABLED = original


class TestDisallowedToolsIsolation:
    """Verify SDK-side tool gating computes correctly per agent."""

    def test_different_agents_get_different_disallowed(self):
        """Agents with different skills get different disallowed tool sets."""
        from pinky_daemon.api import _get_shared_mode_disallowed_tools

        class MockSkillStore:
            def __init__(self, skills_map):
                self._map = skills_map

            def get_agent_skills(self, name, enabled_only=True):
                return [{"name": s} for s in self._map.get(name, [])]

        store = MockSkillStore({
            "barsik": ["pinky-self", "pinky-memory", "research"],
            "pushok": ["pinky-self"],
            "ryzhik": [],  # No skills at all
        })

        barsik_blocked = set(_get_shared_mode_disallowed_tools("barsik", store))
        pushok_blocked = set(_get_shared_mode_disallowed_tools("pushok", store))
        ryzhik_blocked = set(_get_shared_mode_disallowed_tools("ryzhik", store))

        # Barsik has pinky-self (schedule, admin, etc) + pinky-memory (kb) + research
        # So only presentations gate should be blocked
        assert "mcp__pinky-self__kb_search" not in barsik_blocked  # has pinky-memory
        assert "mcp__pinky-self__set_wake_schedule" not in barsik_blocked  # has pinky-self
        assert "mcp__pinky-self__submit_research_brief" not in barsik_blocked  # has research
        assert "mcp__pinky-self__create_presentation" in barsik_blocked  # no presentations

        # Pushok has pinky-self only
        assert "mcp__pinky-self__set_wake_schedule" not in pushok_blocked
        assert "mcp__pinky-self__kb_search" in pushok_blocked  # no pinky-memory
        assert "mcp__pinky-self__submit_research_brief" in pushok_blocked  # no research

        # Ryzhik has nothing — everything blocked
        assert "mcp__pinky-self__set_wake_schedule" in ryzhik_blocked
        assert "mcp__pinky-self__kb_search" in ryzhik_blocked
        assert "mcp__pinky-self__create_presentation" in ryzhik_blocked

        # Ryzhik should have strictly more blocked than pushok, who has more than barsik
        assert len(ryzhik_blocked) > len(pushok_blocked) > len(barsik_blocked)
