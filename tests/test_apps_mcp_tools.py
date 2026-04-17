"""Tests for the apps MCP tool registration and gate wiring."""

from __future__ import annotations

from pinky_self.server import create_server

EXPECTED_APP_TOOLS = [
    "app_url",
    "create_app",
    "delete_app",
    "deploy_app",
    "get_app_source",
    "list_apps",
    "update_app",
]


def _get_tool_names(tool_gates: list[str]) -> list[str]:
    mcp = create_server(
        agent_name="test-agent",
        api_url="http://localhost:8888",
        tool_gates=tool_gates,
    )
    return [t.name for t in mcp._tool_manager.list_tools()]


class TestAppsToolRegistration:
    """Apps tools should register when the 'apps' gate is active."""

    def test_apps_tools_registered_with_gate(self):
        tool_names = _get_tool_names(["apps"])
        for tool in EXPECTED_APP_TOOLS:
            assert tool in tool_names, f"{tool} not registered with apps gate"

    def test_apps_tools_not_registered_without_gate(self):
        tool_names = _get_tool_names([])
        for tool in EXPECTED_APP_TOOLS:
            assert tool not in tool_names, f"{tool} should not be registered without gate"

    def test_apps_tools_coexist_with_other_gates(self):
        tool_names = _get_tool_names(["apps", "extras", "admin"])
        for tool in EXPECTED_APP_TOOLS:
            assert tool in tool_names
        # Spot-check other gates still work
        assert "get_attribution" in tool_names  # extras
        assert "update_and_restart" in tool_names  # admin


class TestAppsGateConfig:
    """Gate config maps are consistent."""

    def test_apps_in_all_tool_gates(self):
        from pinky_daemon.api import ALL_TOOL_GATES
        assert "apps" in ALL_TOOL_GATES

    def test_apps_in_gate_tool_names(self):
        from pinky_daemon.api import GATE_TOOL_NAMES
        assert "apps" in GATE_TOOL_NAMES
        assert sorted(GATE_TOOL_NAMES["apps"]) == EXPECTED_APP_TOOLS

    def test_apps_in_skill_to_gates(self):
        from pinky_daemon.api import SKILL_TO_GATES
        assert "apps" in SKILL_TO_GATES["pinky-self"]

    def test_gate_tool_names_match_registered(self):
        """Every tool listed in GATE_TOOL_NAMES['apps'] should actually register."""
        from pinky_daemon.api import GATE_TOOL_NAMES

        tool_names = _get_tool_names(["apps"])
        for tool in GATE_TOOL_NAMES["apps"]:
            assert tool in tool_names, f"{tool} in GATE_TOOL_NAMES but not registered"
