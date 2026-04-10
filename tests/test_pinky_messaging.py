"""Tests for the pinky_messaging MCP server surface."""

from __future__ import annotations


class TestPinkyMessagingServer:
    def test_explicit_and_legacy_tools_are_registered(self):
        from pinky_messaging.server import create_server

        server = create_server(agent_name="barsik", api_url="http://localhost:8888")
        tool_names = {tool.name for tool in server._tool_manager.list_tools()}

        assert {
            "thread",
            "send",
            "thread",
            "react",
            "send_gif",
            "send_voice",
            "send_photo",
            "send_document",
            "broadcast",
        }.issubset(tool_names)
