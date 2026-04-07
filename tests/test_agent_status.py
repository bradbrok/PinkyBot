"""Tests for agent status, session-meta, and session-event endpoints.

Covers:
  - POST /agents/{name}/status  (extended statuses)
  - GET  /agents/{name}/status
  - GET  /agents/{name}/session-meta
  - GET  /agents  (includes live_status fields)
  - Session event logging on connect / disconnect
  - Cost milestone activity logging
"""

from __future__ import annotations

import os
import tempfile
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient


# ── Helpers ────────────────────────────────────────────────────


def _make_app(db_path: str):
    from pinky_daemon.api import create_api
    return create_api(max_sessions=10, default_working_dir="/tmp", db_path=db_path)


class FakeContextClient:
    def __init__(self, total_tokens: int = 50_000, max_tokens: int = 200_000):
        self.total_tokens = total_tokens
        self.max_tokens = max_tokens

    async def get_context_usage(self):
        return {"totalTokens": self.total_tokens, "maxTokens": self.max_tokens}


class FakeStreamingSession:
    """Minimal stub that satisfies the api.py streaming session interface."""

    def __init__(
        self,
        agent_name: str,
        label: str = "main",
        *,
        connected: bool = True,
        total_tokens: int = 60_000,
        max_tokens: int = 200_000,
        cost_usd: float = 0.0,
        model: str = "claude-sonnet-4-6",
    ):
        self.agent_name = agent_name
        self.label = label
        self.session_id = f"{agent_name}-{label}-sdk-abc123"
        self.created_at = time.time() - 120  # 2 minutes old
        self.last_active = self.created_at
        self.is_connected = connected
        self._stats = {
            "messages_sent": 4,
            "turns": 7,
            "errors": 0,
            "reconnects": 0,
            "auto_restarts": 0,
        }
        self._config = SimpleNamespace(
            model=model,
            context_restart_pct=80,
            permission_mode="bypassPermissions",
            provider_url="",
        )
        self.usage = SimpleNamespace(
            total_cost_usd=cost_usd,
            input_tokens=0,
            output_tokens=0,
        )
        self.account_info: dict = {"apiProvider": "anthropic"}
        self._client = FakeContextClient(total_tokens, max_tokens) if connected else None
        self.disconnect_calls = 0

    @property
    def id(self) -> str:
        return f"{self.agent_name}-{self.label}"

    @property
    def stats(self) -> dict:
        return {
            **self._stats,
            "connected": self.is_connected,
            "pending_responses": 0,
            "current_activity": "",
            "current_thinking": "",
            "activity_log": [],
            "cost_usd": round(self.usage.total_cost_usd, 6),
            "account": self.account_info,
        }

    async def disconnect(self):
        self.disconnect_calls += 1
        self.is_connected = False

    async def connect(self):
        self.is_connected = True


# ── POST /agents/{name}/status ─────────────────────────────────


class TestPostAgentStatus:
    def setup_method(self):
        self._tmpdir = tempfile.mkdtemp()
        self._db = os.path.join(self._tmpdir, "test.db")

    def _client_with_agent(self):
        app = _make_app(self._db)
        client = TestClient(app)
        r = client.post("/agents", json={"name": "arty", "model": "sonnet"})
        assert r.status_code == 200
        return client

    def test_idle_status(self):
        client = self._client_with_agent()
        resp = client.post("/agents/arty/status", json={"status": "idle"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "idle"
        assert data["ok"] is True

    def test_working_status(self):
        client = self._client_with_agent()
        resp = client.post("/agents/arty/status", json={"status": "working"})
        assert resp.status_code == 200
        assert resp.json()["status"] == "working"

    def test_thinking_status(self):
        client = self._client_with_agent()
        resp = client.post("/agents/arty/status", json={"status": "thinking"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "thinking"
        # DB-persisted coarse status should be "working" for sub-states
        assert data["working_status"] == "working"

    def test_tool_use_status_with_tool_name(self):
        client = self._client_with_agent()
        resp = client.post(
            "/agents/arty/status",
            json={"status": "tool_use", "tool_name": "Bash", "detail": "running tests"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "tool_use"

    def test_offline_status(self):
        client = self._client_with_agent()
        resp = client.post("/agents/arty/status", json={"status": "offline"})
        assert resp.status_code == 200
        assert resp.json()["status"] == "offline"

    def test_invalid_status_rejected(self):
        client = self._client_with_agent()
        resp = client.post("/agents/arty/status", json={"status": "vibing"})
        assert resp.status_code == 422

    def test_unknown_agent_404(self):
        client = self._client_with_agent()
        resp = client.post("/agents/nobody/status", json={"status": "idle"})
        assert resp.status_code == 404


# ── GET /agents/{name}/status ──────────────────────────────────


class TestGetAgentStatus:
    def setup_method(self):
        self._tmpdir = tempfile.mkdtemp()
        self._db = os.path.join(self._tmpdir, "test.db")

    def test_default_status_before_any_update(self):
        app = _make_app(self._db)
        with TestClient(app) as client:
            client.post("/agents", json={"name": "nova", "model": "sonnet"})
            resp = client.get("/agents/nova/status")
            assert resp.status_code == 200
            data = resp.json()
            assert data["agent"] == "nova"
            assert data["status"] in ("idle", "working", "offline", "thinking", "tool_use")
            assert "last_updated" in data

    def test_status_reflects_last_post(self):
        app = _make_app(self._db)
        with TestClient(app) as client:
            client.post("/agents", json={"name": "nova", "model": "sonnet"})
            client.post("/agents/nova/status", json={"status": "thinking"})
            resp = client.get("/agents/nova/status")
            assert resp.status_code == 200
            assert resp.json()["status"] == "thinking"

    def test_tool_name_and_detail_returned(self):
        app = _make_app(self._db)
        with TestClient(app) as client:
            client.post("/agents", json={"name": "nova", "model": "sonnet"})
            client.post(
                "/agents/nova/status",
                json={"status": "tool_use", "tool_name": "Read", "detail": "reading CLAUDE.md"},
            )
            resp = client.get("/agents/nova/status")
            assert resp.status_code == 200
            data = resp.json()
            assert data["tool_name"] == "Read"
            assert data["detail"] == "reading CLAUDE.md"

    def test_unknown_agent_404(self):
        app = _make_app(self._db)
        with TestClient(app) as client:
            resp = client.get("/agents/ghost/status")
            assert resp.status_code == 404


# ── GET /agents (fleet includes live_status) ───────────────────


class TestFleetLiveStatus:
    def setup_method(self):
        self._tmpdir = tempfile.mkdtemp()
        self._db = os.path.join(self._tmpdir, "test.db")

    def test_fleet_includes_live_status_fields(self):
        app = _make_app(self._db)
        with TestClient(app) as client:
            client.post("/agents", json={"name": "bolt", "model": "sonnet"})
            resp = client.get("/agents")
            assert resp.status_code == 200
            agents = resp.json()["agents"]
            assert len(agents) == 1
            a = agents[0]
            assert "live_status" in a
            assert "live_tool_name" in a
            assert "live_detail" in a
            assert "streaming" in a

    def test_fleet_live_status_updates_after_post(self):
        app = _make_app(self._db)
        with TestClient(app) as client:
            client.post("/agents", json={"name": "bolt", "model": "sonnet"})
            client.post("/agents/bolt/status", json={"status": "thinking"})
            resp = client.get("/agents")
            agents = resp.json()["agents"]
            assert agents[0]["live_status"] == "thinking"

    def test_fleet_streaming_flag_true_when_session_registered(self):
        app = _make_app(self._db)
        with TestClient(app) as client:
            client.post("/agents", json={"name": "bolt", "model": "sonnet"})
            fake = FakeStreamingSession("bolt", "main")
            app.state.broker.register_streaming("bolt", fake, label="main")
            resp = client.get("/agents")
            agents = resp.json()["agents"]
            assert agents[0]["streaming"] is True


# ── GET /agents/{name}/session-meta ───────────────────────────


class TestSessionMeta:
    def setup_method(self):
        self._tmpdir = tempfile.mkdtemp()
        self._db = os.path.join(self._tmpdir, "test.db")

    def test_session_meta_no_live_session(self):
        app = _make_app(self._db)
        with TestClient(app) as client:
            client.post("/agents", json={"name": "zed", "model": "claude-opus-4-6"})
            resp = client.get("/agents/zed/session-meta")
            assert resp.status_code == 200
            data = resp.json()
            assert data["agent"] == "zed"
            assert data["connected"] is False
            assert data["context_pct"] == 0
            assert data["turns"] == 0

    def test_session_meta_with_live_session(self):
        app = _make_app(self._db)
        with TestClient(app) as client:
            client.post("/agents", json={"name": "zed", "model": "claude-haiku-3-5"})
            fake = FakeStreamingSession(
                "zed", "main",
                connected=True,
                total_tokens=80_000,
                max_tokens=200_000,
                cost_usd=2.5,
                model="claude-haiku-3-5",  # not a 1M model — reported max is accurate
            )
            app.state.broker.register_streaming("zed", fake, label="main")

            resp = client.get("/agents/zed/session-meta")
            assert resp.status_code == 200
            data = resp.json()
            assert data["agent"] == "zed"
            assert data["connected"] is True
            assert data["cost_usd"] == pytest.approx(2.5, abs=0.001)
            assert data["turns"] == 7
            assert data["context_pct"] == 40  # 80k / 200k = 40%
            assert data["uptime_seconds"] >= 0
            assert data["provider"] == "anthropic"
            assert data["model"] == "claude-haiku-3-5"

    def test_session_meta_unknown_agent_404(self):
        app = _make_app(self._db)
        with TestClient(app) as client:
            resp = client.get("/agents/phantom/session-meta")
            assert resp.status_code == 404


# ── Activity & Session Event Logging ──────────────────────────


class TestActivityLogging:
    """Verify activity_store entries are created for key lifecycle events."""

    def setup_method(self):
        self._tmpdir = tempfile.mkdtemp()
        self._db = os.path.join(self._tmpdir, "test.db")

    def test_working_status_logs_activity(self):
        app = _make_app(self._db)
        with TestClient(app) as client:
            client.post("/agents", json={"name": "pix", "model": "sonnet"})
            client.post("/agents/pix/status", json={"status": "working"})

            resp = client.get("/activity", params={"agent_name": "pix"})
            assert resp.status_code == 200
            events = resp.json()["events"]
            types = [e["event_type"] for e in events]
            assert "agent_working" in types

    def test_idle_status_logs_activity(self):
        app = _make_app(self._db)
        with TestClient(app) as client:
            client.post("/agents", json={"name": "pix", "model": "sonnet"})
            client.post("/agents/pix/status", json={"status": "idle"})

            resp = client.get("/activity", params={"agent_name": "pix"})
            events = resp.json()["events"]
            types = [e["event_type"] for e in events]
            assert "agent_idle" in types

    def test_thinking_status_does_not_flood_activity(self):
        """thinking/tool_use sub-states should NOT log to activity (too frequent)."""
        app = _make_app(self._db)
        with TestClient(app) as client:
            client.post("/agents", json={"name": "pix", "model": "sonnet"})
            for _ in range(5):
                client.post("/agents/pix/status", json={"status": "thinking"})

            resp = client.get("/activity", params={"agent_name": "pix"})
            events = resp.json()["events"]
            types = [e["event_type"] for e in events]
            assert "agent_thinking" not in types
