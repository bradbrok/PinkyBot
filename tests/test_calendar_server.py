"""Tests for pinky_calendar MCP tools."""

from __future__ import annotations

import json

from pinky_calendar.server import create_server


def _tools(srv):
    return {t.name: t.fn for t in srv._tool_manager.list_tools()}


class TestCalendarDateParsing:
    def test_get_events_invalid_start_date_returns_tool_error(self):
        srv = create_server(
            caldav_url="https://calendar.example.test",
            caldav_username="user",
            caldav_password="pass",
        )

        raw = _tools(srv)["get_events"](start_date="not-a-date")
        result = json.loads(raw)

        assert "error" in result
        assert "Invalid date" in result["error"]
