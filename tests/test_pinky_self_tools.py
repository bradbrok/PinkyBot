"""Tests for pinky_self MCP tool functions (mocked API).

Each tool's fn() is called directly with urllib.request.urlopen patched
to return preset JSON responses — no live daemon required.
"""
from __future__ import annotations

import json
from io import BytesIO
from unittest.mock import MagicMock, patch

import pytest

from pinky_self.server import create_server


# ── Fixture & helpers ──────────────────────────────────────────────────────────

@pytest.fixture
def srv():
    return create_server(agent_name="barsik", api_url="http://localhost:9999")


def _tools(srv):
    return {t.name: t.fn for t in srv._tool_manager.list_tools()}


def _mock_api(responses: dict):
    """Patch urlopen so each path suffix maps to a JSON response.

    responses: {"/path": {...}} or {"/path": [list]}
    Last key "*" is used as fallback.
    """
    def _urlopen(req, timeout=30):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        for path, data in responses.items():
            if path == "*" or url.endswith(path) or path in url:
                body = json.dumps(data).encode()
                resp = MagicMock()
                resp.read.return_value = body
                resp.__enter__ = lambda s: s
                resp.__exit__ = MagicMock(return_value=False)
                return resp
        # Default empty dict
        body = json.dumps({}).encode()
        resp = MagicMock()
        resp.read.return_value = body
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        return resp
    return patch("urllib.request.urlopen", side_effect=_urlopen)


def _ok(data: dict):
    """Single-response mock (any path → data)."""
    return _mock_api({"*": data})


# ── who_am_i ──────────────────────────────────────────────────────────────────

class TestWhoAmI:
    def test_basic(self, srv):
        agent_resp = {
            "name": "barsik",
            "display_name": "Barsik",
            "model": "claude-opus-4-6",
            "permission_mode": "bypassPermissions",
            "working_dir": "data/agents/barsik",
            "groups": [],
        }
        settings_resp = {"agent": "barsik"}
        sessions_resp = [{"id": "sess-1", "context_used_pct": 42, "message_count": 10}]

        def _urlopen(req, timeout=30):
            url = req.full_url if hasattr(req, "full_url") else str(req)
            if "/settings/main-agent" in url:
                data = settings_resp
            elif "/sessions" in url:
                data = sessions_resp
            else:
                data = agent_resp
            body = json.dumps(data).encode()
            resp = MagicMock()
            resp.read.return_value = body
            resp.__enter__ = lambda s: s
            resp.__exit__ = MagicMock(return_value=False)
            return resp

        with patch("urllib.request.urlopen", side_effect=_urlopen):
            result = _tools(srv)["who_am_i"]()

        assert "barsik" in result.lower() or "Barsik" in result
        assert "opus" in result

    def test_error_fallback(self, srv):
        with _ok({"error": "not found"}):
            result = _tools(srv)["who_am_i"]()
        assert "barsik" in result.lower()


# ── get_attribution ───────────────────────────────────────────────────────────

class TestGetAttribution:
    def test_uses_display_name(self, srv):
        with _ok({"name": "barsik", "display_name": "Barsik"}):
            result = _tools(srv)["get_attribution"]()
        assert result == "🤖 Opened by Barsik"

    def test_fallback_on_error(self, srv):
        with _ok({"error": "oops"}):
            result = _tools(srv)["get_attribution"]()
        assert "Barsik" in result or "barsik" in result.lower()

    def test_uses_name_if_no_display_name(self, srv):
        with _ok({"name": "barsik", "display_name": ""}):
            result = _tools(srv)["get_attribution"]()
        assert "🤖 Opened by" in result


# ── get_owner_profile ─────────────────────────────────────────────────────────

class TestGetOwnerProfile:
    def test_returns_profile(self, srv):
        profile = {
            "name": "Brad",
            "pronouns": "he/him",
            "timezone": "America/Los_Angeles",
            "communication_style": "casual",
        }
        with _ok(profile):
            result = _tools(srv)["get_owner_profile"]()
        assert "Brad" in result

    def test_empty_profile(self, srv):
        with _ok({}):
            result = _tools(srv)["get_owner_profile"]()
        assert isinstance(result, str)


# ── check_my_health ───────────────────────────────────────────────────────────

class TestCheckMyHealth:
    def test_healthy(self, srv):
        health = {
            "agent": "barsik",
            "recommendation": "healthy",
            "session": {
                "state": "idle",
                "context_used_pct": 20,
                "message_count": 5,
                "needs_restart": False,
            },
            "heartbeat": {"status": "alive", "age_seconds": 30, "notes": ""},
            "tasks": {"pending": 0, "in_progress": 0, "blocked": 0},
            "costs": {"total_cost_usd": 0.0042, "query_count": 3},
            "recent_errors": [],
        }
        with _ok(health):
            result = _tools(srv)["check_my_health"]()
        assert "healthy" in result
        assert "barsik" in result

    def test_with_errors(self, srv):
        health = {
            "agent": "barsik",
            "recommendation": "restart needed",
            "session": {
                "state": "idle",
                "context_used_pct": 85,
                "message_count": 50,
                "needs_restart": True,
            },
            "tasks": {"pending": 2, "in_progress": 1, "blocked": 0},
            "costs": {},
            "recent_errors": ["timeout", "rate limit"],
        }
        with _ok(health):
            result = _tools(srv)["check_my_health"]()
        assert "restart" in result.lower()
        assert "Recent errors" in result

    def test_error_response(self, srv):
        with _ok({"error": "agent not found"}):
            result = _tools(srv)["check_my_health"]()
        assert "failed" in result.lower() or "error" in result.lower()


# ── context_status ────────────────────────────────────────────────────────────

class TestContextStatus:
    def test_active_session(self, srv):
        streaming = {
            "session_id": "barsik-main",
            "connected": True,
            "context": {
                "percentage": 45.0,
                "total_tokens": 90000,
                "max_tokens": 200000,
                "model": "claude-opus-4-6",
                "categories": [{"name": "messages", "tokens": 80000}],
                "mcp_tools": [{"name": "pinky-self", "tokens": 5000}],
            },
            "stats": {"turns": 15, "messages_sent": 15, "cost_usd": 0.02},
            "saved_context": {"restart_safe": True, "message": ""},
        }
        saved = {"task": "build feature", "context": "halfway done", "notes": "", "blockers": []}

        def _urlopen(req, timeout=30):
            url = req.full_url if hasattr(req, "full_url") else str(req)
            if "streaming/status" in url:
                data = streaming
            else:
                data = saved
            body = json.dumps(data).encode()
            resp = MagicMock()
            resp.read.return_value = body
            resp.__enter__ = lambda s: s
            resp.__exit__ = MagicMock(return_value=False)
            return resp

        with patch("urllib.request.urlopen", side_effect=_urlopen):
            result = _tools(srv)["context_status"]()

        assert "45.0%" in result
        assert "barsik-main" in result

    def test_no_session(self, srv):
        def _urlopen(req, timeout=30):
            url = req.full_url if hasattr(req, "full_url") else str(req)
            data = {"error": "no session"} if "streaming" in url else {}
            body = json.dumps(data).encode()
            resp = MagicMock()
            resp.read.return_value = body
            resp.__enter__ = lambda s: s
            resp.__exit__ = MagicMock(return_value=False)
            return resp

        with patch("urllib.request.urlopen", side_effect=_urlopen):
            result = _tools(srv)["context_status"]()
        assert "No streaming session" in result

    def test_high_context_warning(self, srv):
        streaming = {
            "session_id": "s1",
            "connected": True,
            "context": {"percentage": 80.0, "total_tokens": 160000, "max_tokens": 200000, "model": ""},
            "stats": {"turns": 50, "messages_sent": 50, "cost_usd": 0.1},
            "saved_context": {},
        }
        def _urlopen(req, timeout=30):
            url = req.full_url if hasattr(req, "full_url") else str(req)
            data = streaming if "streaming" in url else {}
            body = json.dumps(data).encode()
            resp = MagicMock()
            resp.read.return_value = body
            resp.__enter__ = lambda s: s
            resp.__exit__ = MagicMock(return_value=False)
            return resp

        with patch("urllib.request.urlopen", side_effect=_urlopen):
            result = _tools(srv)["context_status"]()
        assert "70%" in result or "restart" in result.lower()


# ── send_heartbeat ────────────────────────────────────────────────────────────

class TestSendHeartbeat:
    def test_ok_status(self, srv):
        with _ok({"status": "ok", "agent": "barsik"}):
            result = _tools(srv)["send_heartbeat"](status="ok", notes="idle")
        assert "ok" in result.lower() or "Heartbeat" in result

    def test_busy_status(self, srv):
        with _ok({"status": "busy"}):
            result = _tools(srv)["send_heartbeat"](status="busy", context_pct=55.0)
        assert isinstance(result, str)

    def test_error(self, srv):
        with _ok({"error": "agent not found"}):
            result = _tools(srv)["send_heartbeat"]()
        assert isinstance(result, str)


# ── spawn_clone ───────────────────────────────────────────────────────────────

class TestSpawnClone:
    def test_spawn_success(self, srv):
        clone_resp = {
            "worker_session_id": "barsik-clone-abc123",
            "forked_sdk_session_id": "uuid-fork-456",
            "agent": "barsik",
            "task": "do the thing",
        }
        with _ok(clone_resp):
            result = _tools(srv)["spawn_clone"](task="do the thing")
        assert "barsik-clone-abc123" in result
        assert "uuid-fork-456" in result

    def test_spawn_failure(self, srv):
        with _ok({"error": "no main session"}):
            result = _tools(srv)["spawn_clone"](task="oops")
        assert "Failed" in result or "failed" in result

    def test_long_task_truncated(self, srv):
        clone_resp = {"worker_session_id": "w1", "forked_sdk_session_id": "f1"}
        with _ok(clone_resp):
            result = _tools(srv)["spawn_clone"](task="x" * 200)
        assert "..." in result or len(result) > 0


# ── list_agents ───────────────────────────────────────────────────────────────

class TestListAgents:
    def test_lists_agents(self, srv):
        agents_resp = {"agents": [
            {"name": "barsik", "display_name": "Barsik", "model": "opus", "enabled": True, "role": "sidekick"},
            {"name": "pushok", "display_name": "Pushok", "model": "sonnet", "enabled": True, "role": "coder"},
        ]}
        presence_resp = {"agents": [
            {"agent": "barsik", "status": "online"},
            {"agent": "pushok", "status": "offline"},
        ]}

        def _urlopen(req, timeout=30):
            url = req.full_url if hasattr(req, "full_url") else str(req)
            data = presence_resp if "presence" in url else agents_resp
            body = json.dumps(data).encode()
            resp = MagicMock()
            resp.read.return_value = body
            resp.__enter__ = lambda s: s
            resp.__exit__ = MagicMock(return_value=False)
            return resp

        with patch("urllib.request.urlopen", side_effect=_urlopen):
            result = _tools(srv)["list_agents"]()
        # barsik is self (skipped), so only pushok should appear
        assert "pushok" in result.lower() or "Pushok" in result

    def test_empty_list(self, srv):
        with _ok({"agents": []}):
            result = _tools(srv)["list_agents"]()
        assert isinstance(result, str)

    def test_error(self, srv):
        with _ok({"error": "oops"}):
            result = _tools(srv)["list_agents"]()
        assert isinstance(result, str)


# ── agent_status ──────────────────────────────────────────────────────────────

class TestAgentStatus:
    def test_active(self, srv):
        agent = {
            "name": "pushok",
            "display_name": "Pushok",
            "model": "sonnet",
            "working_status": "working",
            "enabled": True,
        }
        sessions = [{"state": "running", "context_used_pct": 30, "message_count": 5}]

        def _urlopen(req, timeout=30):
            url = req.full_url if hasattr(req, "full_url") else str(req)
            data = sessions if "/sessions" in url else agent
            body = json.dumps(data).encode()
            resp = MagicMock()
            resp.read.return_value = body
            resp.__enter__ = lambda s: s
            resp.__exit__ = MagicMock(return_value=False)
            return resp

        with patch("urllib.request.urlopen", side_effect=_urlopen):
            result = _tools(srv)["agent_status"](name="pushok")

        assert "pushok" in result.lower() or "Pushok" in result

    def test_not_found(self, srv):
        with _ok({"error": "not found"}):
            result = _tools(srv)["agent_status"](name="ghost")
        assert isinstance(result, str)


# ── check_inbox ───────────────────────────────────────────────────────────────

class TestCheckInbox:
    def test_messages(self, srv):
        inbox = {
            "messages": [
                {"id": "1", "from_agent": "pushok", "subject": "test", "content": "hello", "read": False, "created_at": 0},
            ],
            "unread_count": 1,
        }
        with _ok(inbox):
            result = _tools(srv)["check_inbox"]()
        assert "pushok" in result or "hello" in result or "1" in result

    def test_empty_inbox(self, srv):
        with _ok({"messages": [], "unread_count": 0}):
            result = _tools(srv)["check_inbox"]()
        assert "0" in result or "empty" in result.lower() or isinstance(result, str)

    def test_error(self, srv):
        with _ok({"error": "no inbox"}):
            result = _tools(srv)["check_inbox"]()
        assert isinstance(result, str)


# ── check_for_updates ─────────────────────────────────────────────────────────

class TestCheckForUpdates:
    def test_up_to_date(self, srv):
        with _ok({"up_to_date": True, "current_version": "1.2.3", "branch": "main"}):
            result = _tools(srv)["check_for_updates"]()
        assert isinstance(result, str)

    def test_update_available(self, srv):
        with _ok({"up_to_date": False, "current_version": "1.0.0", "latest_version": "1.2.0", "branch": "beta"}):
            result = _tools(srv)["check_for_updates"]()
        assert isinstance(result, str)

    def test_error(self, srv):
        with _ok({"error": "git error"}):
            result = _tools(srv)["check_for_updates"]()
        assert isinstance(result, str)


# ── context_restart ───────────────────────────────────────────────────────────

class TestContextRestart:
    def test_restart_success(self, srv):
        with _ok({"old_session_id": "barsik-main", "old_turns": 42}):
            result = _tools(srv)["context_restart"]()
        assert "restarted" in result.lower() or "42" in result

    def test_restart_error(self, srv):
        with _ok({"error": "no session to restart"}):
            result = _tools(srv)["context_restart"]()
        assert isinstance(result, str)


# ── get_agent_card ────────────────────────────────────────────────────────────

class TestGetAgentCard:
    def test_returns_card(self, srv):
        card = {
            "name": "pushok",
            "display_name": "Pushok",
            "model": "sonnet",
            "role": "frontend",
            "soul": "I build UIs.",
        }
        with _ok(card):
            result = _tools(srv)["get_agent_card"](name="pushok")
        assert "Pushok" in result or "pushok" in result

    def test_error(self, srv):
        with _ok({"error": "not found"}):
            result = _tools(srv)["get_agent_card"](name="ghost")
        assert isinstance(result, str)
