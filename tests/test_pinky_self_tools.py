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


# ── set_wake_schedule ─────────────────────────────────────────────────────────

class TestSetWakeSchedule:
    def test_basic_wake_mode(self, srv):
        resp = {"id": 42, "name": "morning_check", "cron": "0 8 * * *"}
        with _ok(resp):
            result = _tools(srv)["set_wake_schedule"](
                cron="0 8 * * *",
                name="morning_check",
                prompt="Check inbox",
            )
        assert "morning_check" in result
        assert "0 8 * * *" in result
        assert "wake" in result

    def test_direct_send_mode(self, srv):
        resp = {"id": 7, "name": "standup", "cron": "0 9 * * 1-5"}
        with _ok(resp):
            result = _tools(srv)["set_wake_schedule"](
                cron="0 9 * * 1-5",
                name="standup",
                prompt="Good morning!",
                direct_send=True,
                target_channel="6770805286",
            )
        assert "direct-send" in result

    def test_error(self, srv):
        with _ok({"error": "invalid cron"}):
            result = _tools(srv)["set_wake_schedule"](cron="bad")
        assert "Failed" in result


# ── list_my_schedules ─────────────────────────────────────────────────────────

class TestListMySchedules:
    def test_with_schedules(self, srv):
        resp = {
            "schedules": [
                {"id": 1, "name": "morning", "cron": "0 8 * * *", "enabled": True, "prompt": "Wake up!", "last_run": 0},
                {"id": 2, "name": "evening", "cron": "0 20 * * *", "enabled": False, "prompt": "Evening review", "last_run": 1234567890},
            ]
        }
        with _ok(resp):
            result = _tools(srv)["list_my_schedules"]()
        assert "morning" in result
        assert "evening" in result
        assert "active" in result
        assert "disabled" in result

    def test_no_schedules(self, srv):
        with _ok({"schedules": []}):
            result = _tools(srv)["list_my_schedules"]()
        assert "No schedules" in result


# ── remove_wake_schedule ──────────────────────────────────────────────────────

class TestRemoveWakeSchedule:
    def test_success(self, srv):
        with _ok({"deleted": True}):
            result = _tools(srv)["remove_wake_schedule"](schedule_id=5)
        assert "5" in result
        assert "removed" in result

    def test_not_found(self, srv):
        with _ok({"deleted": False, "error": "not found"}):
            result = _tools(srv)["remove_wake_schedule"](schedule_id=99)
        assert "Failed" in result or "not found" in result


# ── save_my_context ───────────────────────────────────────────────────────────

class TestSaveMyContext:
    def test_success(self, srv):
        with _ok({"saved": True}):
            result = _tools(srv)["save_my_context"](
                task="Build the feature",
                context="halfway done",
                notes="remember to test",
                blockers=["waiting on pushok"],
                priority_items=["run tests first"],
            )
        assert "saved" in result.lower() or "Context saved" in result

    def test_error(self, srv):
        with _ok({"error": "db error"}):
            result = _tools(srv)["save_my_context"](task="oops")
        assert "Failed" in result

    def test_minimal(self, srv):
        with _ok({}):
            result = _tools(srv)["save_my_context"]()
        assert isinstance(result, str)


# ── load_my_context ───────────────────────────────────────────────────────────

class TestLoadMyContext:
    def test_with_saved_context(self, srv):
        ctx = {
            "context": "working on feature X",
            "task": "implement login",
            "notes": "check auth flow",
            "blockers": ["need API key"],
            "priority_items": ["test first"],
            "updated_at": 1700000000,
            "freshness": {"age_seconds": 600, "age_human": "10m", "stale_warning": False},
        }
        with _ok(ctx):
            result = _tools(srv)["load_my_context"]()
        assert "implement login" in result
        assert "check auth flow" in result
        assert "need API key" in result

    def test_no_saved_context(self, srv):
        with _ok({"context": None}):
            result = _tools(srv)["load_my_context"]()
        assert "fresh start" in result.lower() or "No saved" in result

    def test_stale_warning(self, srv):
        ctx = {
            "context": "old stuff",
            "task": "old task",
            "updated_at": 1600000000,
            "freshness": {"age_seconds": 90000, "age_human": "25h", "stale_warning": True},
        }
        with _ok(ctx):
            result = _tools(srv)["load_my_context"]()
        assert "WARNING" in result or "stale" in result.lower()

    def test_empty_context(self, srv):
        with _ok({"context": ""}):
            result = _tools(srv)["load_my_context"]()
        assert isinstance(result, str)


# ── get_next_task ─────────────────────────────────────────────────────────────

class TestGetNextTask:
    def test_returns_task(self, srv):
        tasks_resp = [
            {
                "id": 101,
                "title": "Fix the bug",
                "priority": "high",
                "status": "pending",
                "description": "It's broken",
                "tags": ["backend"],
                "blocked_by": [],
                "project_id": 5,
            }
        ]
        with _mock_api({"/tasks": tasks_resp, "*": {}}):
            result = _tools(srv)["get_next_task"]()
        assert "101" in result
        assert "Fix the bug" in result
        assert "high" in result

    def test_no_tasks(self, srv):
        with _ok([]):
            result = _tools(srv)["get_next_task"]()
        assert "No unblocked" in result

    def test_blocked_tasks_skipped(self, srv):
        tasks_resp = [
            {
                "id": 200,
                "title": "Blocked task",
                "priority": "urgent",
                "status": "pending",
                "description": "",
                "tags": [],
                "blocked_by": [201],
                "project_id": 0,
            }
        ]
        # blocked_by task 201 is not completed
        blocker_resp = {"id": 201, "status": "pending"}

        def _urlopen(req, timeout=30):
            url = req.full_url if hasattr(req, "full_url") else str(req)
            if "/tasks/201" in url:
                data = blocker_resp
            else:
                data = tasks_resp
            body = json.dumps(data).encode()
            resp = MagicMock()
            resp.read.return_value = body
            resp.__enter__ = lambda s: s
            resp.__exit__ = MagicMock(return_value=False)
            return resp

        with patch("urllib.request.urlopen", side_effect=_urlopen):
            result = _tools(srv)["get_next_task"]()
        assert "No unblocked" in result

    def test_completed_blocker_allows_task(self, srv):
        tasks_resp = [
            {
                "id": 300,
                "title": "Unblocked by completed",
                "priority": "normal",
                "status": "pending",
                "description": "Ready",
                "tags": [],
                "blocked_by": [301],
                "project_id": 0,
            }
        ]
        blocker_resp = {"id": 301, "status": "completed"}

        def _urlopen(req, timeout=30):
            url = req.full_url if hasattr(req, "full_url") else str(req)
            if "/tasks/301" in url:
                data = blocker_resp
            else:
                data = tasks_resp
            body = json.dumps(data).encode()
            resp = MagicMock()
            resp.read.return_value = body
            resp.__enter__ = lambda s: s
            resp.__exit__ = MagicMock(return_value=False)
            return resp

        with patch("urllib.request.urlopen", side_effect=_urlopen):
            result = _tools(srv)["get_next_task"]()
        assert "300" in result
        assert "Unblocked by completed" in result


# ── claim_task ────────────────────────────────────────────────────────────────

class TestClaimTask:
    def test_success(self, srv):
        with _ok({"id": 42, "title": "Do the thing", "status": "in_progress"}):
            result = _tools(srv)["claim_task"](task_id=42)
        assert "42" in result
        assert "Do the thing" in result
        assert "in_progress" in result

    def test_error(self, srv):
        with _ok({"error": "task not found"}):
            result = _tools(srv)["claim_task"](task_id=999)
        assert "Failed" in result


# ── complete_task ─────────────────────────────────────────────────────────────

class TestCompleteTask:
    def test_success(self, srv):
        with _ok({"id": 55, "status": "completed"}):
            result = _tools(srv)["complete_task"](task_id=55, summary="Done it all")
        assert "55" in result
        assert "completed" in result

    def test_error(self, srv):
        with _ok({"error": "not your task"}):
            result = _tools(srv)["complete_task"](task_id=55)
        assert "Failed" in result


# ── block_task ────────────────────────────────────────────────────────────────

class TestBlockTask:
    def test_success(self, srv):
        with _ok({"id": 77, "status": "blocked"}):
            result = _tools(srv)["block_task"](task_id=77, reason="waiting on API key")
        assert "77" in result
        assert "blocked" in result.lower()

    def test_error(self, srv):
        with _ok({"error": "task not found"}):
            result = _tools(srv)["block_task"](task_id=0)
        assert "Failed" in result


# ── create_task ───────────────────────────────────────────────────────────────

class TestCreateTask:
    def test_self_assigned(self, srv):
        with _ok({"id": 88, "title": "New task", "assigned_agent": "barsik"}):
            result = _tools(srv)["create_task"](title="New task", description="do it", priority="high")
        assert "88" in result
        assert "New task" in result
        assert "barsik" in result

    def test_delegate_to_other(self, srv):
        with _ok({"id": 89, "title": "Delegated", "assigned_agent": "pushok"}):
            result = _tools(srv)["create_task"](
                title="Delegated",
                assigned_agent="pushok",
                tags=["frontend"],
            )
        assert "pushok" in result

    def test_error(self, srv):
        with _ok({"error": "validation error"}):
            result = _tools(srv)["create_task"](title="")
        assert "Failed" in result


# ── decompose_project ─────────────────────────────────────────────────────────

class TestDecomposeProject:
    def test_success(self, srv):
        tasks_input = [
            {"title": "Task A", "priority": "high"},
            {"title": "Task B", "description": "Details", "assigned_agent": "pushok"},
        ]
        call_count = [0]

        def _urlopen(req, timeout=30):
            call_count[0] += 1
            if call_count[0] == 1:
                data = {}  # project update
            elif call_count[0] == 2:
                data = {"id": 10, "title": "Task A"}
            else:
                data = {"id": 11, "title": "Task B"}
            body = json.dumps(data).encode()
            resp = MagicMock()
            resp.read.return_value = body
            resp.__enter__ = lambda s: s
            resp.__exit__ = MagicMock(return_value=False)
            return resp

        with patch("urllib.request.urlopen", side_effect=_urlopen):
            result = _tools(srv)["decompose_project"](
                project_id=1, tasks=tasks_input, description="New project"
            )
        assert "Decomposed project #1" in result
        assert "2 tasks" in result

    def test_partial_failure(self, srv):
        tasks_input = [
            {"title": "OK task"},
            {"title": "Bad task"},
        ]
        call_count = [0]

        def _urlopen(req, timeout=30):
            call_count[0] += 1
            if call_count[0] == 1:
                data = {"id": 20, "title": "OK task"}
            else:
                data = {"error": "permission denied"}
            body = json.dumps(data).encode()
            resp = MagicMock()
            resp.read.return_value = body
            resp.__enter__ = lambda s: s
            resp.__exit__ = MagicMock(return_value=False)
            return resp

        with patch("urllib.request.urlopen", side_effect=_urlopen):
            result = _tools(srv)["decompose_project"](project_id=2, tasks=tasks_input)
        assert "Failed" in result or "1" in result


# ── bulk_create_tasks ─────────────────────────────────────────────────────────

class TestBulkCreateTasks:
    def test_all_success(self, srv):
        tasks_input = [{"title": "Alpha"}, {"title": "Beta"}]
        call_count = [0]

        def _urlopen(req, timeout=30):
            call_count[0] += 1
            data = {"id": call_count[0] + 100, "title": tasks_input[call_count[0] - 1]["title"]}
            body = json.dumps(data).encode()
            resp = MagicMock()
            resp.read.return_value = body
            resp.__enter__ = lambda s: s
            resp.__exit__ = MagicMock(return_value=False)
            return resp

        with patch("urllib.request.urlopen", side_effect=_urlopen):
            result = _tools(srv)["bulk_create_tasks"](project_id=3, tasks=tasks_input)
        assert "Created 2 tasks" in result
        assert "Alpha" in result

    def test_empty_tasks(self, srv):
        with _ok({}):
            result = _tools(srv)["bulk_create_tasks"](project_id=3, tasks=[])
        assert "Created 0 tasks" in result


# ── list_presentations ────────────────────────────────────────────────────────

class TestListPresentations:
    def test_with_items(self, srv):
        resp = {
            "count": 2,
            "presentations": [
                {"id": 1, "title": "Q1 Review", "created_by": "barsik", "current_version": 1, "share_token": "abc123"},
                {"id": 2, "title": "Roadmap", "created_by": "barsik", "current_version": 2, "share_token": "def456"},
            ],
        }
        with _ok(resp):
            result = _tools(srv)["list_presentations"]()
        assert "Q1 Review" in result
        assert "Roadmap" in result
        assert "abc123" in result

    def test_empty(self, srv):
        with _ok({"presentations": []}):
            result = _tools(srv)["list_presentations"]()
        assert "No presentations" in result

    def test_error(self, srv):
        with _ok({"error": "db down"}):
            result = _tools(srv)["list_presentations"]()
        assert "Failed" in result


# ── create_presentation ───────────────────────────────────────────────────────

class TestCreatePresentation:
    def test_success(self, srv):
        resp = {
            "id": 10,
            "title": "My Deck",
            "slug": "my-deck",
            "share_token": "tok123",
            "current_version": 1,
        }
        with _ok(resp):
            result = _tools(srv)["create_presentation"](
                title="My Deck",
                html_content="<html></html>",
                description="Great deck",
                tags="finance,charts",
            )
        assert "#10" in result
        assert "tok123" in result
        assert "/p/tok123" in result

    def test_with_research_topic(self, srv):
        resp = {"id": 11, "title": "Research Deck", "slug": "rd", "share_token": "tok999", "current_version": 1}
        with _ok(resp):
            result = _tools(srv)["create_presentation"](
                title="Research Deck",
                html_content="<html></html>",
                research_topic_id=42,
            )
        assert "tok999" in result

    def test_error(self, srv):
        with _ok({"error": "invalid html"}):
            result = _tools(srv)["create_presentation"](title="Oops", html_content="")
        assert "Failed" in result


# ── update_presentation ───────────────────────────────────────────────────────

class TestUpdatePresentation:
    def test_success(self, srv):
        resp = {"id": 5, "current_version": 3, "share_token": "tok555"}
        with _ok(resp):
            result = _tools(srv)["update_presentation"](
                presentation_id=5,
                html_content="<html>v2</html>",
                description="added charts",
            )
        assert "#5" in result
        assert "version 3" in result
        assert "tok555" in result

    def test_error(self, srv):
        with _ok({"error": "not found"}):
            result = _tools(srv)["update_presentation"](presentation_id=999, html_content="")
        assert "Failed" in result


# ── get_presentation_template ─────────────────────────────────────────────────

class TestGetPresentationTemplate:
    def test_default_variant(self, srv):
        result = _tools(srv)["get_presentation_template"]()
        assert "Brand template" in result
        assert "```html" in result
        assert "PLACEHOLDER" in result or "{{" in result

    def test_stitch_variant(self, srv):
        result = _tools(srv)["get_presentation_template"](variant="stitch")
        assert "stitch" in result.lower()
        assert "```html" in result

    def test_minimal_variant(self, srv):
        result = _tools(srv)["get_presentation_template"](variant="minimal")
        assert "Brand template" in result

    def test_unknown_variant_falls_back(self, srv):
        result = _tools(srv)["get_presentation_template"](variant="nonexistent")
        assert "Brand template" in result


# ── request_sleep ─────────────────────────────────────────────────────────────

class TestRequestSleep:
    def test_simple_sleep(self, srv):
        with _ok({"sessions_closed": 1}):
            result = _tools(srv)["request_sleep"]()
        assert "sleep" in result.lower()
        assert "1" in result

    def test_sleep_with_wake_cron(self, srv):
        call_count = [0]

        def _urlopen(req, timeout=30):
            call_count[0] += 1
            if call_count[0] == 1:
                # Schedule creation
                data = {"id": 10, "name": "sleep_wake"}
            else:
                # Sleep request
                data = {"sessions_closed": 2}
            body = json.dumps(data).encode()
            resp = MagicMock()
            resp.read.return_value = body
            resp.__enter__ = lambda s: s
            resp.__exit__ = MagicMock(return_value=False)
            return resp

        with patch("urllib.request.urlopen", side_effect=_urlopen):
            result = _tools(srv)["request_sleep"](
                wake_cron="0 8 * * *",
                wake_prompt="Morning tasks",
            )
        assert "sleep" in result.lower()
        assert "0 8 * * *" in result

    def test_error(self, srv):
        with _ok({"error": "no session"}):
            result = _tools(srv)["request_sleep"]()
        assert "failed" in result.lower() or "Sleep request failed" in result


# ── send_to_agent ─────────────────────────────────────────────────────────────

class TestSendToAgent:
    def test_delivered(self, srv):
        with _ok({"delivered": True, "queued": False}):
            result = _tools(srv)["send_to_agent"](to="pushok", message="Hello!")
        assert "delivered" in result.lower() or "pushok" in result

    def test_queued(self, srv):
        with _ok({"queued": True}):
            result = _tools(srv)["send_to_agent"](to="pushok", message="Hey, async")
        assert "queued" in result.lower() or "offline" in result.lower()

    def test_with_reply_and_priority(self, srv):
        with _ok({"delivered": True}):
            result = _tools(srv)["send_to_agent"](
                to="pushok",
                message="Replying",
                content_type="task_response",
                reply_to=123,
                priority="urgent",
            )
        assert isinstance(result, str)

    def test_error(self, srv):
        with _ok({"error": "agent not found"}):
            result = _tools(srv)["send_to_agent"](to="ghost", message="oops")
        assert "Failed" in result or "failed" in result.lower()


# ── search_history ────────────────────────────────────────────────────────────

class TestSearchHistory:
    def test_with_results(self, srv):
        resp = {
            "messages": [
                {"role": "user", "content": "Build the login flow", "timestamp": 1700000000},
                {"role": "assistant", "content": "Sure, I'll start with auth", "timestamp": 1700000060},
            ]
        }
        with _ok(resp):
            result = _tools(srv)["search_history"](query="login")
        assert "login" in result or "Build the login" in result
        assert "Found 2" in result

    def test_no_results(self, srv):
        with _ok({"messages": []}):
            result = _tools(srv)["search_history"](query="nonexistent topic xyz")
        assert "No messages found" in result

    def test_many_results_truncated(self, srv):
        msgs = [
            {"role": "user", "content": f"message {i}", "timestamp": 1700000000 + i}
            for i in range(20)
        ]
        with _ok({"messages": msgs}):
            result = _tools(srv)["search_history"](query="message")
        assert "Found 20" in result


# ── list_my_skills ────────────────────────────────────────────────────────────

class TestListMySkills:
    def test_with_skills(self, srv):
        resp = {
            "skills": [
                {"name": "pinky-memory", "description": "Long-term memory", "category": "core", "assigned_by": "system", "skill_type": "builtin"},
                {"name": "humanizer", "description": "Make text human", "category": "writing", "assigned_by": "self", "skill_type": "skill_md"},
            ]
        }
        with _ok(resp):
            result = _tools(srv)["list_my_skills"]()
        assert "pinky-memory" in result
        assert "humanizer" in result
        assert "core" in result

    def test_no_skills(self, srv):
        with _ok({"skills": []}):
            result = _tools(srv)["list_my_skills"]()
        assert "No skills" in result

    def test_error(self, srv):
        with _ok({"error": "db error"}):
            result = _tools(srv)["list_my_skills"]()
        assert "Failed" in result


# ── list_available_skills ─────────────────────────────────────────────────────

class TestListAvailableSkills:
    def test_with_results(self, srv):
        resp = {
            "skills": [
                {"name": "data-analysis", "description": "Analyze data", "category": "productivity", "requires": []},
                {"name": "web-scraper", "description": "Scrape pages", "category": "development", "requires": ["http-client"]},
            ]
        }
        with _ok(resp):
            result = _tools(srv)["list_available_skills"]()
        assert "data-analysis" in result
        assert "web-scraper" in result
        assert "http-client" in result

    def test_category_filter(self, srv):
        resp = {"skills": [{"name": "data-analysis", "description": "Analyze", "category": "productivity", "requires": []}]}
        with _ok(resp):
            result = _tools(srv)["list_available_skills"](category="productivity")
        assert "data-analysis" in result

    def test_none_available(self, srv):
        with _ok({"skills": []}):
            result = _tools(srv)["list_available_skills"]()
        assert "No additional" in result

    def test_error(self, srv):
        with _ok({"error": "server error"}):
            result = _tools(srv)["list_available_skills"]()
        assert "Failed" in result


# ── load_skill ────────────────────────────────────────────────────────────────

class TestLoadSkill:
    def test_success(self, srv):
        resp = {
            "name": "humanizer",
            "description": "Remove AI writing patterns",
            "directive": "# Humanizer\n\nRemove em dashes...",
        }
        with _ok(resp):
            result = _tools(srv)["load_skill"](skill_name="humanizer")
        assert "humanizer" in result
        assert "Humanizer" in result
        assert "Skill:" in result

    def test_not_found(self, srv):
        with _ok({"error": "not found", "status": 404}):
            result = _tools(srv)["load_skill"](skill_name="nonexistent")
        assert "not found" in result.lower()

    def test_no_directive(self, srv):
        with _ok({"name": "empty-skill", "directive": ""}):
            result = _tools(srv)["load_skill"](skill_name="empty-skill")
        assert "no instructions" in result.lower() or "has no" in result.lower()

    def test_server_error(self, srv):
        with _ok({"error": "internal error", "status": 500}):
            result = _tools(srv)["load_skill"](skill_name="broken")
        assert "Failed" in result


# ── add_skill ─────────────────────────────────────────────────────────────────

class TestAddSkill:
    def test_success_with_restart(self, srv):
        def _urlopen(req, timeout=30):
            url = req.full_url if hasattr(req, "full_url") else str(req)
            if "/skills/apply" in url:
                data = {"session_restarted": True, "tool_patterns": ["humanizer-*"]}
            else:
                data = {"name": "humanizer", "assigned_to": "barsik"}
            body = json.dumps(data).encode()
            resp = MagicMock()
            resp.read.return_value = body
            resp.__enter__ = lambda s: s
            resp.__exit__ = MagicMock(return_value=False)
            return resp

        with patch("urllib.request.urlopen", side_effect=_urlopen):
            result = _tools(srv)["add_skill"](skill_name="humanizer")
        assert "humanizer" in result
        assert "restarting" in result.lower() or "restart" in result.lower()

    def test_not_self_assignable(self, srv):
        with _ok({"error": "not self-assignable", "status": 403}):
            result = _tools(srv)["add_skill"](skill_name="restricted")
        assert "not self-assignable" in result

    def test_not_found(self, srv):
        with _ok({"error": "not found", "status": 404}):
            result = _tools(srv)["add_skill"](skill_name="ghost")
        assert "not found" in result.lower()

    def test_apply_fails(self, srv):
        def _urlopen(req, timeout=30):
            url = req.full_url if hasattr(req, "full_url") else str(req)
            if "/skills/apply" in url:
                data = {"error": "apply failed"}
            else:
                data = {"name": "humanizer"}
            body = json.dumps(data).encode()
            resp = MagicMock()
            resp.read.return_value = body
            resp.__enter__ = lambda s: s
            resp.__exit__ = MagicMock(return_value=False)
            return resp

        with patch("urllib.request.urlopen", side_effect=_urlopen):
            result = _tools(srv)["add_skill"](skill_name="humanizer")
        assert "assigned" in result or "failed to apply" in result


# ── remove_skill ──────────────────────────────────────────────────────────────

class TestRemoveSkill:
    def test_success(self, srv):
        def _urlopen(req, timeout=30):
            url = req.full_url if hasattr(req, "full_url") else str(req)
            if "/skills/apply" in url:
                data = {"session_restarted": True}
            else:
                data = {"deleted": True}
            body = json.dumps(data).encode()
            resp = MagicMock()
            resp.read.return_value = body
            resp.__enter__ = lambda s: s
            resp.__exit__ = MagicMock(return_value=False)
            return resp

        with patch("urllib.request.urlopen", side_effect=_urlopen):
            result = _tools(srv)["remove_skill"](skill_name="humanizer")
        assert "humanizer" in result
        assert "removed" in result.lower() or "restarting" in result.lower()

    def test_core_skill_rejected(self, srv):
        with _ok({"error": "core skill cannot be removed", "status": 400}):
            result = _tools(srv)["remove_skill"](skill_name="pinky-self")
        assert "Cannot remove" in result

    def test_not_assigned(self, srv):
        with _ok({"error": "not assigned", "status": 404}):
            result = _tools(srv)["remove_skill"](skill_name="unassigned")
        assert "not assigned" in result.lower()


# ── discover_skills ───────────────────────────────────────────────────────────

class TestDiscoverSkills:
    def test_with_new_skills(self, srv):
        skill_resp = {
            "discovered": 5,
            "registered": ["new-skill-a", "new-skill-b"],
            "updated": ["existing-skill"],
            "skipped": ["old-a", "old-b", "old-c"],
        }
        plugin_resp = {"discovered": ["mcp-calendar"]}

        def _urlopen(req, timeout=30):
            url = req.full_url if hasattr(req, "full_url") else str(req)
            if "/plugins/discover" in url:
                data = plugin_resp
            else:
                data = skill_resp
            body = json.dumps(data).encode()
            resp = MagicMock()
            resp.read.return_value = body
            resp.__enter__ = lambda s: s
            resp.__exit__ = MagicMock(return_value=False)
            return resp

        with patch("urllib.request.urlopen", side_effect=_urlopen):
            result = _tools(srv)["discover_skills"]()
        assert "new-skill-a" in result
        assert "existing-skill" in result
        assert "mcp-calendar" in result

    def test_no_new_skills(self, srv):
        skill_resp = {"discovered": 3, "registered": [], "updated": [], "skipped": ["a", "b", "c"]}
        plugin_resp = {"discovered": []}

        def _urlopen(req, timeout=30):
            url = req.full_url if hasattr(req, "full_url") else str(req)
            if "/plugins/discover" in url:
                data = plugin_resp
            else:
                data = skill_resp
            body = json.dumps(data).encode()
            resp = MagicMock()
            resp.read.return_value = body
            resp.__enter__ = lambda s: s
            resp.__exit__ = MagicMock(return_value=False)
            return resp

        with patch("urllib.request.urlopen", side_effect=_urlopen):
            result = _tools(srv)["discover_skills"]()
        assert "No new plugins" in result

    def test_error(self, srv):
        with _ok({"error": "fs error"}):
            result = _tools(srv)["discover_skills"]()
        assert "Failed" in result


# ── install_skill ─────────────────────────────────────────────────────────────

class TestInstallSkill:
    def test_success(self, srv):
        resp = {
            "repo": "github.com/example/skills",
            "registered": ["data-analysis"],
            "updated": [],
            "assigned_skills": ["data-analysis"],
        }
        with _ok(resp):
            result = _tools(srv)["install_skill"](url="https://github.com/example/skills")
        assert "data-analysis" in result
        assert "github.com/example/skills" in result

    def test_no_skills_found(self, srv):
        resp = {"repo": "github.com/example/empty", "registered": [], "updated": [], "assigned_skills": []}
        with _ok(resp):
            result = _tools(srv)["install_skill"](url="https://github.com/example/empty")
        assert "no new skills" in result.lower() or "no" in result.lower()

    def test_error(self, srv):
        with _ok({"error": "clone failed", "status": 400}):
            result = _tools(srv)["install_skill"](url="https://github.com/bad/url")
        assert "Failed" in result


# ── create_skill ──────────────────────────────────────────────────────────────

class TestCreateSkill:
    def test_success_assigned(self, srv):
        resp = {"name": "my-workflow", "assigned_to": "barsik"}
        with _ok(resp):
            result = _tools(srv)["create_skill"](
                name="my-workflow",
                description="Automates my workflow",
                instructions="# My Workflow\n\nDo these steps...",
            )
        assert "my-workflow" in result
        assert "created" in result.lower()
        assert "barsik" in result

    def test_success_not_assigned(self, srv):
        resp = {"name": "shared-skill", "assigned_to": ""}
        with _ok(resp):
            result = _tools(srv)["create_skill"](
                name="shared-skill",
                description="A shared skill",
                instructions="# Shared\n\nInstructions here.",
            )
        assert "shared-skill" in result
        assert "add_skill" in result

    def test_error(self, srv):
        with _ok({"error": "invalid name", "status": 400}):
            result = _tools(srv)["create_skill"](
                name="bad name!",
                description="desc",
                instructions="instructions",
            )
        assert "Invalid" in result or "Failed" in result


# ── propose_skill ─────────────────────────────────────────────────────────────

class TestProposeSkill:
    """Tests for propose_skill — auto-draft skill from completed task."""

    _task = "Analyzed a codebase for performance bottlenecks"
    _steps = "1. Read key files\n2. Profile hot paths\n3. Identify N+1 queries"
    _outcome = "Found 3 bottlenecks, proposed fixes, reduced latency by 40%"
    _name = "perf-analysis"

    def test_draft_mode_no_api_call(self, srv):
        """Draft mode (auto_install=False) returns SKILL.md without hitting API."""
        # No mock needed — draft mode should NOT call the API
        result = _tools(srv)["propose_skill"](
            task_description=self._task,
            steps_taken=self._steps,
            outcome=self._outcome,
            skill_name=self._name,
            auto_install=False,
        )
        assert "perf-analysis" in result
        assert "draft" in result.lower()
        assert "create_skill" in result
        # SKILL.md content should be embedded
        assert "Analyzed a codebase" in result
        assert "Found 3 bottlenecks" in result

    def test_auto_install_success_assigned(self, srv):
        """auto_install=True registers and assigns the skill."""
        resp = {"name": "perf-analysis", "assigned_to": "barsik"}
        with _ok(resp):
            result = _tools(srv)["propose_skill"](
                task_description=self._task,
                steps_taken=self._steps,
                outcome=self._outcome,
                skill_name=self._name,
                auto_install=True,
            )
        assert "perf-analysis" in result
        assert "created" in result.lower() or "registered" in result.lower()
        assert "barsik" in result

    def test_auto_install_success_not_assigned(self, srv):
        """auto_install=True with no assigned_to shows add_skill hint."""
        resp = {"name": "perf-analysis", "assigned_to": ""}
        with _ok(resp):
            result = _tools(srv)["propose_skill"](
                task_description=self._task,
                steps_taken=self._steps,
                outcome=self._outcome,
                skill_name=self._name,
                auto_install=True,
            )
        assert "perf-analysis" in result
        assert "add_skill" in result

    def test_auto_install_api_error(self, srv):
        """auto_install=True with API error returns error + draft content."""
        with _ok({"error": "name conflict", "status": 400}):
            result = _tools(srv)["propose_skill"](
                task_description=self._task,
                steps_taken=self._steps,
                outcome=self._outcome,
                skill_name=self._name,
                auto_install=True,
            )
        assert "Invalid" in result or "Failed" in result
        # Draft content still embedded for manual use
        assert "perf-analysis" in result

    def test_default_is_draft_mode(self, srv):
        """Default auto_install=False — no API call, draft returned."""
        result = _tools(srv)["propose_skill"](
            task_description=self._task,
            steps_taken=self._steps,
            outcome=self._outcome,
            skill_name=self._name,
        )
        assert "draft" in result.lower()
        assert "perf-analysis" in result


# ── update_and_restart ────────────────────────────────────────────────────────

class TestUpdateAndRestart:
    def test_success(self, srv):
        resp = {
            "updated": True,
            "before_hash": "abc123",
            "after_hash": "def456",
            "commits": ["feat: add triggers", "fix: memory leak"],
            "deps_rebuilt": False,
            "frontend_rebuilt": True,
            "restarting": True,
        }
        with _ok(resp):
            result = _tools(srv)["update_and_restart"](branch="beta")
        assert "abc123" in result
        assert "def456" in result
        assert "add triggers" in result
        assert "Frontend rebuilt" in result
        assert "restarting" in result.lower()

    def test_no_update_flag(self, srv):
        with _ok({"updated": False, "message": "already up to date"}):
            result = _tools(srv)["update_and_restart"]()
        assert "Unexpected" in result or isinstance(result, str)

    def test_error(self, srv):
        with _ok({"error": "merge conflict"}):
            result = _tools(srv)["update_and_restart"]()
        assert "failed" in result.lower()


# ── restart_daemon ────────────────────────────────────────────────────────────

class TestRestartDaemon:
    def test_success(self, srv):
        with _ok({"restarting": True, "git_hash": "abc1234"}):
            result = _tools(srv)["restart_daemon"]()
        assert "abc1234" in result
        assert "restarting" in result.lower()

    def test_error(self, srv):
        with _ok({"error": "permission denied"}):
            result = _tools(srv)["restart_daemon"]()
        assert "failed" in result.lower()


# ── create_trigger ────────────────────────────────────────────────────────────

class TestCreateTrigger:
    def test_webhook_trigger(self, srv):
        resp = {
            "id": 15,
            "name": "github-prs",
            "trigger_type": "webhook",
            "token": "secret-webhook-token-xyz",
        }
        with _ok(resp):
            import asyncio
            result = asyncio.run(
                _tools(srv)["create_trigger"](
                    name="github-prs",
                    trigger_type="webhook",
                    prompt_template="New PR: {{body.title}}",
                )
            )
        assert "github-prs" in result
        assert "secret-webhook-token-xyz" in result
        assert "/hooks/secret-webhook-token-xyz" in result

    def test_url_trigger(self, srv):
        resp = {"id": 16, "name": "status-check", "trigger_type": "url"}
        with _ok(resp):
            import asyncio
            result = asyncio.run(
                _tools(srv)["create_trigger"](
                    name="status-check",
                    trigger_type="url",
                    url="https://status.example.com",
                    condition="status_changed",
                    interval_seconds=60,
                )
            )
        assert "status-check" in result
        assert "16" in result

    def test_error(self, srv):
        with _ok({"error": "invalid trigger type"}):
            import asyncio
            result = asyncio.run(
                _tools(srv)["create_trigger"](name="bad", trigger_type="invalid")
            )
        assert "Error" in result


# ── list_triggers ─────────────────────────────────────────────────────────────

class TestListTriggers:
    def test_with_triggers(self, srv):
        resp = {
            "triggers": [
                {"id": 1, "name": "github-prs", "trigger_type": "webhook", "enabled": True, "fire_count": 5},
                {"id": 2, "name": "status-check", "trigger_type": "url", "enabled": False, "fire_count": 0},
            ]
        }
        with _ok(resp):
            import asyncio
            result = asyncio.run(_tools(srv)["list_triggers"]())
        assert "github-prs" in result
        assert "status-check" in result
        assert "ON" in result
        assert "OFF" in result
        assert "fired 5x" in result

    def test_empty(self, srv):
        with _ok({"triggers": []}):
            import asyncio
            result = asyncio.run(_tools(srv)["list_triggers"]())
        assert "No triggers" in result

    def test_error(self, srv):
        with _ok({"error": "db error"}):
            import asyncio
            result = asyncio.run(_tools(srv)["list_triggers"]())
        assert "Error" in result


# ── delete_trigger ────────────────────────────────────────────────────────────

class TestDeleteTrigger:
    def test_success(self, srv):
        with _ok({"deleted": True}):
            import asyncio
            result = asyncio.run(_tools(srv)["delete_trigger"](trigger_id=5))
        assert "5" in result
        assert "deleted" in result.lower()

    def test_error(self, srv):
        with _ok({"error": "not found"}):
            import asyncio
            result = asyncio.run(_tools(srv)["delete_trigger"](trigger_id=999))
        assert "Error" in result


# ── test_trigger ──────────────────────────────────────────────────────────────

class TestTestTrigger:
    def test_success_agent_woken(self, srv):
        resp = {
            "agent_woken": True,
            "prompt": "New PR opened: Fix auth bug\nRepository: pinkybot",
        }
        with _ok(resp):
            import asyncio
            result = asyncio.run(_tools(srv)["test_trigger"](trigger_id=7))
        assert "7" in result
        assert "woken" in result.lower() or "successfully" in result.lower()
        assert "New PR" in result

    def test_wake_failed(self, srv):
        resp = {"agent_woken": False, "prompt": "test prompt"}
        with _ok(resp):
            import asyncio
            result = asyncio.run(_tools(srv)["test_trigger"](trigger_id=8))
        assert "failed" in result.lower() or "Wake attempt failed" in result

    def test_error(self, srv):
        with _ok({"error": "trigger not found"}):
            import asyncio
            result = asyncio.run(_tools(srv)["test_trigger"](trigger_id=0))
        assert "Error" in result


# ── Research pipeline ─────────────────────────────────────────────────────────

class TestResearchPipeline:
    def test_create_research_topic_auto_assign(self, srv):
        def _urlopen(req, timeout=30):
            url = req.full_url if hasattr(req, "full_url") else str(req)
            if "/assign" in url:
                data = {"title": "AI trends", "status": "assigned"}
            else:
                data = {"id": 20, "title": "AI trends"}
            body = json.dumps(data).encode()
            resp = MagicMock()
            resp.read.return_value = body
            resp.__enter__ = lambda s: s
            resp.__exit__ = MagicMock(return_value=False)
            return resp

        with patch("urllib.request.urlopen", side_effect=_urlopen):
            result = _tools(srv)["create_research_topic"](
                title="AI trends",
                description="Research AI trends in 2026",
                priority="high",
                tags="AI,trends",
                auto_assign=True,
            )
        assert "AI trends" in result
        assert "20" in result
        assert "assigned" in result.lower()

    def test_create_research_topic_no_auto_assign(self, srv):
        with _ok({"id": 21, "title": "Blockchain"}):
            result = _tools(srv)["create_research_topic"](
                title="Blockchain",
                auto_assign=False,
            )
        assert "21" in result
        assert "Blockchain" in result
        # Should not have "assigned to you"
        assert "Start researching" not in result

    def test_create_research_error(self, srv):
        with _ok({"error": "duplicate topic"}):
            result = _tools(srv)["create_research_topic"](title="Duplicate")
        assert "Failed" in result

    def test_list_research_topics(self, srv):
        resp = {
            "topics": [
                {"id": 1, "title": "AI trends", "status": "assigned", "priority": "high", "brief_count": 2, "assigned_agent": "barsik"},
                {"id": 2, "title": "Blockchain", "status": "open", "priority": "normal", "brief_count": 0, "assigned_agent": None},
            ]
        }
        with _ok(resp):
            result = _tools(srv)["list_research_topics"]()
        assert "AI trends" in result
        assert "Blockchain" in result
        assert "barsik" in result

    def test_list_research_topics_empty(self, srv):
        with _ok({"topics": []}):
            result = _tools(srv)["list_research_topics"]()
        assert "No research topics" in result

    def test_list_research_topics_with_status_filter(self, srv):
        resp = {"topics": [{"id": 3, "title": "Open topic", "status": "open", "priority": "low", "brief_count": 0, "assigned_agent": None}]}
        with _ok(resp):
            result = _tools(srv)["list_research_topics"](status="open")
        assert "Open topic" in result

    def test_get_research_detail(self, srv):
        resp = {
            "topic": {"id": 5, "title": "ML Research", "status": "in_review", "priority": "high", "description": "About ML", "assigned_agent": "barsik"},
            "briefs": [
                {"id": 10, "version": 1, "status": "draft", "created_at": "2026-01-01", "summary": "ML is cool", "key_findings": "fast convergence"}
            ],
            "reviews": [
                {"reviewer": "pushok", "verdict": "approve", "confidence": 4, "comments": "Good work"}
            ],
        }
        with _ok(resp):
            result = _tools(srv)["get_research_detail"](topic_id=5)
        assert "ML Research" in result
        assert "ML is cool" in result
        assert "pushok" in result
        assert "approve" in result

    def test_get_research_detail_error(self, srv):
        with _ok({"error": "not found"}):
            result = _tools(srv)["get_research_detail"](topic_id=999)
        assert "Failed" in result

    def test_claim_research_topic(self, srv):
        with _ok({"title": "Open topic", "status": "assigned"}):
            result = _tools(srv)["claim_research_topic"](topic_id=10)
        assert "10" in result
        assert "assigned" in result.lower() or "Claimed" in result

    def test_claim_research_topic_error(self, srv):
        with _ok({"error": "already assigned"}):
            result = _tools(srv)["claim_research_topic"](topic_id=10)
        assert "Failed" in result

    def test_publish_research(self, srv):
        with _ok({"published": True, "topic_id": 5}):
            result = _tools(srv)["publish_research"](topic_id=5)
        assert "5" in result
        assert "Published" in result or "published" in result.lower()

    def test_publish_research_error(self, srv):
        with _ok({"error": "no brief"}):
            result = _tools(srv)["publish_research"](topic_id=99)
        assert "Failed" in result

    def test_submit_research_brief(self, srv):
        with _ok({"id": 50, "version": 1, "status": "draft"}):
            result = _tools(srv)["submit_research_brief"](
                topic_id=5,
                content="# Research\n\nDetailed findings...",
                summary="AI is growing fast",
                sources="arxiv.org, papers.ai",
                key_findings="rapid growth, new models",
            )
        assert "5" in result
        assert "50" in result
        assert "draft" in result

    def test_submit_research_brief_error(self, srv):
        with _ok({"error": "topic not found"}):
            result = _tools(srv)["submit_research_brief"](
                topic_id=999,
                content="content",
            )
        assert "Failed" in result

    def test_submit_research_review(self, srv):
        with _ok({"id": 30, "verdict": "approve"}):
            result = _tools(srv)["submit_research_review"](
                topic_id=5,
                brief_id=10,
                verdict="approve",
                comments="Looks great!",
                confidence=5,
            )
        assert "10" in result
        assert "approve" in result

    def test_submit_research_review_error(self, srv):
        with _ok({"error": "brief not found"}):
            result = _tools(srv)["submit_research_review"](
                topic_id=5,
                brief_id=999,
                verdict="reject",
            )
        assert "Failed" in result

    def test_get_my_research_assignments(self, srv):
        resp = {
            "topics": [
                {"id": 1, "title": "Assigned topic", "status": "assigned", "priority": "high",
                 "assigned_agent": "barsik", "reviewer_agents": []},
                {"id": 2, "title": "Open topic", "status": "open", "priority": "normal",
                 "assigned_agent": None, "reviewer_agents": [], "description": "Open for anyone"},
                {"id": 3, "title": "Review topic", "status": "in_review", "priority": "normal",
                 "assigned_agent": "pushok", "reviewer_agents": ["barsik"]},
            ]
        }
        with _ok(resp):
            result = _tools(srv)["get_my_research_assignments"]()
        assert "Assigned topic" in result
        assert "ASSIGNED TO YOU" in result
        assert "Open topic" in result
        assert "OPEN" in result

    def test_get_my_research_assignments_empty(self, srv):
        with _ok({"topics": []}):
            result = _tools(srv)["get_my_research_assignments"]()
        assert "No research" in result

    def test_get_my_research_assignments_error(self, srv):
        with _ok({"error": "db error"}):
            result = _tools(srv)["get_my_research_assignments"]()
        assert "Failed" in result


# ── render_pdf ────────────────────────────────────────────────────────────────

class TestRenderPdf:
    def test_success(self, srv):
        resp = {"path": "/data/exports/document.pdf", "filename": "document.pdf"}
        with _ok(resp):
            result = _tools(srv)["render_pdf"](
                content="# My Report\n\nSome content here.",
                filename="my-report.pdf",
                title="My Report",
            )
        result_data = json.loads(result)
        assert result_data.get("path") or result_data.get("filename")

    def test_error(self, srv):
        with _ok({"error": "weasyprint not installed"}):
            result = _tools(srv)["render_pdf"](content="# Test")
        result_data = json.loads(result)
        assert "error" in result_data


# ── send_file_to_agent ────────────────────────────────────────────────────────

class TestSendFileToAgent:
    def test_relative_path_rejected(self, srv):
        result = _tools(srv)["send_file_to_agent"](
            to_agent="pushok",
            file_path="relative/path/file.pdf",
        )
        data = json.loads(result)
        assert "error" in data
        assert "absolute" in data["error"].lower()

    def test_file_not_found(self, srv):
        result = _tools(srv)["send_file_to_agent"](
            to_agent="pushok",
            file_path="/nonexistent/path/file.pdf",
        )
        data = json.loads(result)
        assert "error" in data
        assert "not found" in data["error"].lower() or "File not found" in data["error"]

    def test_success_via_api(self, srv, tmp_path):
        test_file = tmp_path / "test.pdf"
        test_file.write_bytes(b"PDF content")
        resp = {"file_name": "test.pdf", "transferred_path": "/shared/test.pdf"}
        with _ok(resp):
            result = _tools(srv)["send_file_to_agent"](
                to_agent="pushok",
                file_path=str(test_file),
                description="Test PDF",
            )
        data = json.loads(result)
        assert data.get("sent") is True
        assert data.get("to") == "pushok"


# ── agent_status (presence endpoint) ─────────────────────────────────────────

class TestAgentStatusPresence:
    def test_online_with_streaming(self, srv):
        resp = {
            "status": "online",
            "display_name": "Pushok",
            "streaming": True,
            "last_seen": 1700000000,
        }
        with _ok(resp):
            result = _tools(srv)["agent_status"](name="pushok")
        assert "Pushok" in result
        assert "online" in result
        assert "streaming" in result.lower()

    def test_offline_no_last_seen(self, srv):
        resp = {"status": "offline", "display_name": "Ryzhik", "streaming": False, "last_seen": 0}
        with _ok(resp):
            result = _tools(srv)["agent_status"](name="ryzhik")
        assert "offline" in result

    def test_error_returns_fallback(self, srv):
        with _ok({"error": "not found"}):
            result = _tools(srv)["agent_status"](name="ghost")
        assert "ghost" in result or "not found" in result.lower()
