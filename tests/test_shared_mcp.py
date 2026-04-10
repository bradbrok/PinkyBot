"""Tests for the shared MCP server infrastructure."""

from __future__ import annotations

import pytest

from pinky_daemon.shared_mcp import (
    SHARED_MCP_HOST,
    SHARED_MCP_PORT,
    AgentNameMiddleware,
    LazyAgentName,
    MemoryStorePool,
    SharedMcpManager,
    _current_agent,
    get_current_agent,
    make_agent_name_resolver,
)


class TestLazyAgentName:
    """LazyAgentName resolves dynamically via ContextVar in shared mode."""

    def test_fallback_when_no_contextvar(self):
        name = LazyAgentName("barsik")
        assert str(name) == "barsik"
        assert f"{name}" == "barsik"

    def test_contextvar_takes_precedence(self):
        name = LazyAgentName("barsik")
        token = _current_agent.set("pushok")
        try:
            assert str(name) == "pushok"
            assert f"{name}" == "pushok"
        finally:
            _current_agent.reset(token)

    def test_equality(self):
        name = LazyAgentName("barsik")
        assert name == "barsik"
        assert name != "pushok"

    def test_equality_with_contextvar(self):
        name = LazyAgentName("barsik")
        token = _current_agent.set("pushok")
        try:
            assert name == "pushok"
            assert name != "barsik"
        finally:
            _current_agent.reset(token)

    def test_hash_stable_in_stdio_mode(self):
        name = LazyAgentName("barsik")
        assert hash(name) == hash("barsik")
        token = _current_agent.set("pushok")
        try:
            # Hash stays stable in stdio mode (non-empty fallback)
            assert hash(name) == hash("barsik")
        finally:
            _current_agent.reset(token)

    def test_hash_raises_in_shared_mode(self):
        name = LazyAgentName("")  # shared mode — empty fallback
        with pytest.raises(TypeError, match="unhashable"):
            hash(name)

    def test_add_operations(self):
        name = LazyAgentName("barsik")
        assert name + "-agent" == "barsik-agent"
        assert "agent-" + name == "agent-barsik"

    def test_add_with_contextvar(self):
        name = LazyAgentName("barsik")
        token = _current_agent.set("pushok")
        try:
            assert name + "-agent" == "pushok-agent"
            assert "agent-" + name == "agent-pushok"
        finally:
            _current_agent.reset(token)

    def test_format_spec(self):
        name = LazyAgentName("barsik")
        assert f"{name:>10}" == "    barsik"

    def test_mod_operator(self):
        name = LazyAgentName("barsik")
        assert name % () == "barsik"

    def test_repr(self):
        name = LazyAgentName("barsik")
        assert repr(name) == "barsik"


class TestMakeAgentNameResolver:
    def test_fallback(self):
        resolve = make_agent_name_resolver("barsik")
        assert resolve() == "barsik"

    def test_contextvar_override(self):
        resolve = make_agent_name_resolver("barsik")
        token = _current_agent.set("pushok")
        try:
            assert resolve() == "pushok"
        finally:
            _current_agent.reset(token)


class TestGetCurrentAgent:
    def test_default_empty(self):
        assert get_current_agent() == ""

    def test_returns_set_value(self):
        token = _current_agent.set("ryzhik")
        try:
            assert get_current_agent() == "ryzhik"
        finally:
            _current_agent.reset(token)


class TestAgentNameMiddleware:
    @pytest.mark.asyncio
    async def test_sets_contextvar_from_header(self):
        captured_agent = []

        async def inner_app(scope, receive, send):
            captured_agent.append(get_current_agent())

        middleware = AgentNameMiddleware(inner_app)
        scope = {
            "type": "http",
            "headers": [(b"x-agent-name", b"barsik")],
        }
        await middleware(scope, None, None)
        assert captured_agent == ["barsik"]

    @pytest.mark.asyncio
    async def test_no_header_passes_through(self):
        captured_agent = []

        async def inner_app(scope, receive, send):
            captured_agent.append(get_current_agent())

        middleware = AgentNameMiddleware(inner_app)
        scope = {
            "type": "http",
            "headers": [],
        }
        await middleware(scope, None, None)
        assert captured_agent == [""]

    @pytest.mark.asyncio
    async def test_rejects_invalid_agent_names(self):
        captured_agent = []

        async def inner_app(scope, receive, send):
            captured_agent.append(get_current_agent())

        middleware = AgentNameMiddleware(inner_app)
        # Path traversal attempt
        scope = {
            "type": "http",
            "headers": [(b"x-agent-name", b"../../../etc/passwd")],
        }
        await middleware(scope, None, None)
        assert captured_agent == [""]  # rejected, treated as no header

        captured_agent.clear()
        # Spaces / special chars
        scope = {
            "type": "http",
            "headers": [(b"x-agent-name", b"bad agent <script>")],
        }
        await middleware(scope, None, None)
        assert captured_agent == [""]

    @pytest.mark.asyncio
    async def test_resets_contextvar_after_request(self):
        async def inner_app(scope, receive, send):
            pass

        middleware = AgentNameMiddleware(inner_app)
        scope = {
            "type": "http",
            "headers": [(b"x-agent-name", b"barsik")],
        }
        await middleware(scope, None, None)
        # After request, ContextVar should be reset
        assert get_current_agent() == ""

    @pytest.mark.asyncio
    async def test_non_http_passes_through(self):
        called = []

        async def inner_app(scope, receive, send):
            called.append(True)

        middleware = AgentNameMiddleware(inner_app)
        scope = {"type": "websocket"}
        await middleware(scope, None, None)
        assert called == [True]


class TestSharedMcpManager:
    def test_defaults(self):
        mgr = SharedMcpManager()
        assert mgr._host == SHARED_MCP_HOST
        assert mgr._port == SHARED_MCP_PORT
        assert mgr.url == f"http://{SHARED_MCP_HOST}:{SHARED_MCP_PORT}"
        assert not mgr.is_running

    def test_custom_params(self):
        mgr = SharedMcpManager(host="0.0.0.0", port=9999, api_url="http://example.com")
        assert mgr._host == "0.0.0.0"
        assert mgr._port == 9999
        assert mgr._api_url == "http://example.com"


class TestGateToolNames:
    """GATE_TOOL_NAMES and _get_shared_mode_disallowed_tools."""

    def test_all_gates_covered(self):
        from pinky_daemon.api import ALL_TOOL_GATES, GATE_TOOL_NAMES
        assert set(GATE_TOOL_NAMES.keys()) == set(ALL_TOOL_GATES)

    def test_gate_tool_names_match_actual_registrations(self):
        """Validate GATE_TOOL_NAMES against actual tools registered per gate.

        Catches drift: if a tool is added to a gate in server.py but not in
        GATE_TOOL_NAMES, this test fails.
        """
        from pinky_daemon.api import ALL_TOOL_GATES, GATE_TOOL_NAMES
        from pinky_self.server import create_server

        # Register with ALL gates
        all_server = create_server(
            agent_name="test", api_url="http://localhost:8888",
            tool_gates=list(ALL_TOOL_GATES),
        )
        all_tools = {t.name for t in all_server._tool_manager.list_tools()}

        # Register with NO gates
        no_gate_server = create_server(
            agent_name="test", api_url="http://localhost:8888",
            tool_gates=[],
        )
        core_tools = {t.name for t in no_gate_server._tool_manager.list_tools()}

        # All gated tools = (all gates) minus (no gates)
        actual_gated = all_tools - core_tools

        # All tools listed in GATE_TOOL_NAMES
        declared_gated = set()
        for tools in GATE_TOOL_NAMES.values():
            declared_gated.update(tools)

        assert declared_gated == actual_gated, (
            f"GATE_TOOL_NAMES drift detected!\n"
            f"  In server but not declared: {actual_gated - declared_gated}\n"
            f"  Declared but not in server: {declared_gated - actual_gated}"
        )

    def test_all_gated_tools_prefixed(self):
        from pinky_daemon.api import ALL_GATED_TOOL_NAMES
        for name in ALL_GATED_TOOL_NAMES:
            assert name.startswith("mcp__pinky-self__")

    def test_no_skills_disallows_everything(self):
        """Agent with no skills gets all gated tools disallowed."""
        from pinky_daemon.api import _get_shared_mode_disallowed_tools
        disallowed = _get_shared_mode_disallowed_tools("no-skills-agent", skill_store=None)
        # No skill_store → _get_agent_tool_gates returns ALL gates → nothing disallowed
        # (safety fallback: all gates open when no skill store)
        assert disallowed == []

    def test_with_mock_skill_store_no_skills(self):
        """Agent with skill store but no skills → all gated tools disallowed."""
        from pinky_daemon.api import ALL_GATED_TOOL_NAMES, _get_shared_mode_disallowed_tools

        class MockSkillStore:
            def get_agent_skills(self, name, enabled_only=True):
                return []

        disallowed = _get_shared_mode_disallowed_tools("test", skill_store=MockSkillStore())
        assert set(disallowed) == ALL_GATED_TOOL_NAMES

    def test_with_pinky_self_skill(self):
        """Agent with pinky-self skill gets schedule/admin/etc gates open."""
        from pinky_daemon.api import GATE_TOOL_NAMES, _get_shared_mode_disallowed_tools

        class MockSkillStore:
            def get_agent_skills(self, name, enabled_only=True):
                return [{"name": "pinky-self"}]

        disallowed = _get_shared_mode_disallowed_tools("test", skill_store=MockSkillStore())
        # pinky-self maps to: schedule, admin, skill-admin, triggers, extras, tasks-admin
        # So kb, research, presentations gates should be disallowed
        disallowed_set = set(disallowed)
        for tool in GATE_TOOL_NAMES["kb"]:
            assert f"mcp__pinky-self__{tool}" in disallowed_set
        for tool in GATE_TOOL_NAMES["research"]:
            assert f"mcp__pinky-self__{tool}" in disallowed_set
        for tool in GATE_TOOL_NAMES["presentations"]:
            assert f"mcp__pinky-self__{tool}" in disallowed_set
        # schedule tools should NOT be disallowed
        for tool in GATE_TOOL_NAMES["schedule"]:
            assert f"mcp__pinky-self__{tool}" not in disallowed_set


class TestWriteMcpJsonSharedMode:
    """Test that _write_mcp_json emits SSE configs when shared mode is active."""

    def test_stdio_mode_default(self, tmp_path):
        """Without SHARED_MCP_ENABLED, should produce stdio configs."""
        import pinky_daemon.api as api_mod

        original = api_mod.SHARED_MCP_ENABLED
        api_mod.SHARED_MCP_ENABLED = False
        try:
            work_dir = tmp_path / "agent"
            work_dir.mkdir()
            api_mod._write_mcp_json(work_dir, "barsik")
            import json
            config = json.loads((work_dir / ".mcp.json").read_text())
            servers = config["mcpServers"]
            # stdio mode: command-based configs
            assert "command" in servers["pinky-self"]
            assert "command" in servers["pinky-messaging"]
            assert "command" in servers["pinky-memory"]
        finally:
            api_mod.SHARED_MCP_ENABLED = original

    def test_shared_mode_sse(self, tmp_path):
        """With SHARED_MCP_ENABLED, pinky-self and pinky-messaging use SSE."""
        import pinky_daemon.api as api_mod

        original = api_mod.SHARED_MCP_ENABLED
        api_mod.SHARED_MCP_ENABLED = True
        try:
            work_dir = tmp_path / "agent"
            work_dir.mkdir()
            api_mod._write_mcp_json(work_dir, "barsik")
            import json
            config = json.loads((work_dir / ".mcp.json").read_text())
            servers = config["mcpServers"]

            # pinky-self should be SSE
            assert servers["pinky-self"]["type"] == "sse"
            assert "/mcp/self/sse" in servers["pinky-self"]["url"]
            assert servers["pinky-self"]["headers"]["X-Agent-Name"] == "barsik"
            assert "command" not in servers["pinky-self"]

            # pinky-messaging should be SSE
            assert servers["pinky-messaging"]["type"] == "sse"
            assert "/mcp/messaging/sse" in servers["pinky-messaging"]["url"]
            assert servers["pinky-messaging"]["headers"]["X-Agent-Name"] == "barsik"

            # pinky-memory should also be SSE (shared store pool)
            assert servers["pinky-memory"]["type"] == "sse"
            assert "/mcp/memory/sse" in servers["pinky-memory"]["url"]
            assert servers["pinky-memory"]["headers"]["X-Agent-Name"] == "barsik"
        finally:
            api_mod.SHARED_MCP_ENABLED = original

    def test_shared_mode_different_agents(self, tmp_path):
        """Each agent gets its own X-Agent-Name header."""
        import pinky_daemon.api as api_mod

        original = api_mod.SHARED_MCP_ENABLED
        api_mod.SHARED_MCP_ENABLED = True
        try:
            import json
            for name in ["barsik", "pushok", "ryzhik"]:
                work_dir = tmp_path / name
                work_dir.mkdir()
                api_mod._write_mcp_json(work_dir, name)
                config = json.loads((work_dir / ".mcp.json").read_text())
                assert config["mcpServers"]["pinky-self"]["headers"]["X-Agent-Name"] == name
                assert config["mcpServers"]["pinky-messaging"]["headers"]["X-Agent-Name"] == name
                assert config["mcpServers"]["pinky-memory"]["headers"]["X-Agent-Name"] == name
        finally:
            api_mod.SHARED_MCP_ENABLED = original


class TestMemoryStorePool:
    """Tests for the MemoryStorePool used in shared SSE mode."""

    def test_lazy_creation(self, tmp_path):
        """Stores are created lazily on first access."""
        db_paths = {}

        def resolver(agent_name):
            db_path = str(tmp_path / agent_name / "memory.db")
            (tmp_path / agent_name).mkdir(exist_ok=True)
            db_paths[agent_name] = db_path
            return db_path

        pool = MemoryStorePool(resolver)

        # No stores created yet
        assert len(pool._stores) == 0

        # First access creates the store
        store1 = pool.get_store("barsik")
        assert store1 is not None
        assert len(pool._stores) == 1
        assert "barsik" in db_paths

        # Second access returns the same instance
        store2 = pool.get_store("barsik")
        assert store1 is store2
        assert len(pool._stores) == 1

        # Different agent gets a different store
        store3 = pool.get_store("pushok")
        assert store3 is not store1
        assert len(pool._stores) == 2

        pool.close_all()

    def test_remove_store(self, tmp_path):
        """Removing a store closes it and removes from cache."""
        def resolver(agent_name):
            db_path = str(tmp_path / f"{agent_name}.db")
            return db_path

        pool = MemoryStorePool(resolver)
        pool.get_store("barsik")
        assert "barsik" in pool._stores

        pool.remove_store("barsik")
        assert "barsik" not in pool._stores

        # Removing non-existent agent is a no-op
        pool.remove_store("nonexistent")

        pool.close_all()

    def test_close_all(self, tmp_path):
        """close_all closes all stores and clears cache."""
        def resolver(agent_name):
            return str(tmp_path / f"{agent_name}.db")

        pool = MemoryStorePool(resolver)
        pool.get_store("barsik")
        pool.get_store("pushok")
        assert len(pool._stores) == 2

        pool.close_all()
        assert len(pool._stores) == 0

    def test_resolver_error(self, tmp_path):
        """ValueError from resolver propagates."""
        def resolver(agent_name):
            raise ValueError(f"Unknown agent: {agent_name}")

        pool = MemoryStorePool(resolver)
        with pytest.raises(ValueError, match="Unknown agent"):
            pool.get_store("nonexistent")
