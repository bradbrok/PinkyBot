"""Tests for pinky-self MCP server tools."""

from __future__ import annotations

import json
import os
import tempfile
from unittest.mock import patch, MagicMock

import pytest

from pinky_self.server import create_server


@pytest.fixture
def server():
    """Create a pinky-self server for testing."""
    return create_server(agent_name="test-agent", api_url="http://localhost:9999")


class TestSelfServerCreation:
    def test_create_server(self, server):
        assert server is not None
        assert server.name == "pinky-self"

    def test_tools_registered(self, server):
        """All self-management tools should be registered."""
        tool_names = [t.name for t in server._tool_manager.list_tools()]
        expected = [
            "set_wake_schedule",
            "list_my_schedules",
            "remove_wake_schedule",
            "save_my_context",
            "load_my_context",
            "get_next_task",
            "claim_task",
            "complete_task",
            "block_task",
            "create_task",
            "check_my_health",
            "request_sleep",
            "send_heartbeat",
        ]
        for name in expected:
            assert name in tool_names, f"Missing tool: {name}"


class TestSelfToolsWithAPI:
    """Test tools against real PinkyBot API."""

    def _make_client_and_server(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        from pinky_daemon.api import create_api
        app = create_api(max_sessions=10, default_working_dir="/tmp", db_path=path)
        from fastapi.testclient import TestClient
        client = TestClient(app)

        # Register a test agent
        client.post("/agents", json={"name": "test-agent", "model": "sonnet"})

        return client, path

    def test_set_and_list_schedules(self):
        client, path = self._make_client_and_server()

        # Set schedule via API (simulating what the MCP tool does)
        resp = client.post("/agents/test-agent/schedules", json={
            "name": "morning",
            "cron": "0 8 * * *",
            "prompt": "Check inbox",
        })
        assert resp.status_code == 200
        assert resp.json()["name"] == "morning"

        # List
        resp = client.get("/agents/test-agent/schedules")
        assert resp.json()["count"] == 1

        os.unlink(path)

    def test_save_and_load_context(self):
        client, path = self._make_client_and_server()

        # Save context
        resp = client.put("/agents/test-agent/context", json={
            "task": "Building the widget",
            "context": "Halfway through implementation",
            "notes": "Need to add tests",
            "blockers": ["waiting on API spec"],
        })
        assert resp.status_code == 200
        assert resp.json()["task"] == "Building the widget"

        # Load context
        resp = client.get("/agents/test-agent/context")
        assert resp.json()["task"] == "Building the widget"
        assert resp.json()["blockers"] == ["waiting on API spec"]

        os.unlink(path)

    def test_task_workflow(self):
        client, path = self._make_client_and_server()

        # Create task
        resp = client.post("/tasks", json={
            "title": "Fix the bug",
            "priority": "high",
            "created_by": "user",
        })
        assert resp.status_code == 200
        task_id = resp.json()["id"]

        # Get next (should find unassigned task)
        resp = client.get("/tasks/next?agent_name=test-agent")
        assert resp.json()["task"]["id"] == task_id
        assert resp.json()["source"] == "unassigned"

        # Claim it
        resp = client.post(f"/tasks/claim/{task_id}?agent_name=test-agent")
        assert resp.status_code == 200
        assert resp.json()["status"] == "in_progress"
        assert resp.json()["assigned_agent"] == "test-agent"

        # Complete it
        resp = client.post(f"/tasks/complete/{task_id}?agent_name=test-agent&summary=Fixed+the+null+check")
        assert resp.status_code == 200
        assert resp.json()["status"] == "completed"

        os.unlink(path)

    def test_health_check(self):
        client, path = self._make_client_and_server()

        resp = client.get("/agents/test-agent/health")
        assert resp.status_code == 200
        assert resp.json()["agent"] == "test-agent"
        assert resp.json()["recommendation"] == "healthy"

        os.unlink(path)

    def test_heartbeat(self):
        client, path = self._make_client_and_server()

        resp = client.post("/agents/test-agent/heartbeat", json={
            "session_id": "test-agent-main",
            "status": "alive",
        })
        assert resp.status_code == 200
        assert resp.json()["status"] == "alive"

        os.unlink(path)

    def test_deep_sleep(self):
        client, path = self._make_client_and_server()

        resp = client.post("/agents/test-agent/sleep")
        assert resp.status_code == 200
        assert resp.json()["status"] == "sleeping"

        os.unlink(path)
