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
        from pinky_daemon.transport_state import SessionState
        self._TS = SessionState
        self.agent_name = agent_name
        self.label = label
        self.resume_handle = f"{agent_name}-{label}-sdk-abc123"
        self.created_at = time.time() - 120  # 2 minutes old
        self.last_active = self.created_at
        self._state = SessionState.CONNECTED if connected else SessionState.DEAD
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
    def state(self):
        return self._state

    @property
    def stats(self) -> dict:
        return {
            **self._stats,
            "connected": self._state == self._TS.CONNECTED,
            "pending_responses": 0,
            "current_activity": "",
            "current_thinking": "",
            "activity_log": [],
            "cost_usd": round(self.usage.total_cost_usd, 6),
            "account": self.account_info,
        }

    async def disconnect(self):
        self.disconnect_calls += 1
        self._state = self._TS.DEAD

    async def connect(self):
        self._state = self._TS.CONNECTED


class FakeCodexStreamingSession(FakeStreamingSession):
    def __init__(
        self,
        agent_name: str,
        label: str = "main",
        *,
        connected: bool = True,
        total_tokens: int = 84_000,
        max_tokens: int = 200_000,
        cost_usd: float = 0.0,
        model: str = "gpt-5.4",
    ):
        super().__init__(
            agent_name,
            label,
            connected=connected,
            total_tokens=total_tokens,
            max_tokens=max_tokens,
            cost_usd=cost_usd,
            model=model,
        )
        self.account_info = {"apiProvider": "codex_cli"}
        self._client = None
        self._context_info = {
            "total_tokens": total_tokens,
            "max_tokens": max_tokens,
            "percentage": round(total_tokens / max_tokens * 100, 1),
            "categories": [],
            "mcp_tools": [],
        }

    def get_context_info(self):
        return self._context_info


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

    def test_session_meta_with_codex_fallback_context(self):
        app = _make_app(self._db)
        with TestClient(app) as client:
            client.post("/agents", json={"name": "zed", "model": "gpt-5.4"})
            fake = FakeCodexStreamingSession(
                "zed", "main",
                connected=True,
                total_tokens=84_000,
                max_tokens=200_000,
                cost_usd=0.0,
                model="gpt-5.4",
            )
            app.state.broker.register_streaming("zed", fake, label="main")

            resp = client.get("/agents/zed/session-meta")
            assert resp.status_code == 200
            data = resp.json()
            assert data["connected"] is True
            assert data["provider"] == "codex_cli"
            assert data["context_pct"] == 42

            status_resp = client.get("/agents/zed/streaming/status")
            assert status_resp.status_code == 200
            status = status_resp.json()
            assert status["context"]["percentage"] == 42.0

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


# ──────────────────────────────────────────────────────────────────────────
# PR8b — Transport wake + transcript-path endpoints
# ──────────────────────────────────────────────────────────────────────────


class TestTransportEndpoints:
    """Tests for ``/agents/<name>/transport/wake`` and
    ``/agents/<name>/transport/transcript-path`` (PR8b).

    Path-validation hardening is from Pushok's PR #496 round-1 Case 4a
    review — restricted to ``~/.claude/projects/`` with symlink
    resolution to defend against a valid-HMAC caller pointing the
    tailer at arbitrary files.
    """

    def setup_method(self):
        self._tmpdir = tempfile.mkdtemp()
        self._db = os.path.join(self._tmpdir, "test.db")

    def _client_with_agent(self, name: str = "dymok"):
        app = _make_app(self._db)
        client = TestClient(app)
        r = client.post("/agents", json={"name": name, "model": "sonnet"})
        assert r.status_code == 200
        return client

    def test_wake_endpoint_returns_ok_for_missing_session(self):
        """Wake endpoint is a no-op if no streaming session is registered
        (fires before connect / after disconnect). Returns 200 with
        ``session: None`` so the hook script's ``|| true`` doesn't fail
        the model turn."""
        client = self._client_with_agent()
        resp = client.post(
            "/agents/dymok/transport/wake",
            json={"event": "stop_hook_summary"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["session"] is None

    def test_wake_endpoint_404_for_unknown_agent(self):
        client = self._client_with_agent()
        resp = client.post(
            "/agents/nobody/transport/wake",
            json={"event": "stop_hook_summary"},
        )
        assert resp.status_code == 404

    def test_transcript_path_rejects_non_absolute(self):
        """Pushok Case 4a: relative paths rejected with 400."""
        client = self._client_with_agent()
        resp = client.post(
            "/agents/dymok/transport/transcript-path",
            json={"transcript_path": "relative/path.jsonl"},
        )
        assert resp.status_code == 400
        assert "absolute" in resp.json()["detail"].lower()

    def test_transcript_path_rejects_outside_projects_dir(self):
        """Pushok Case 4a: paths outside ``~/.claude/projects/`` rejected
        with 403 — the perimeter defense against pointing the tailer at
        ``/var/log/system.log`` etc."""
        client = self._client_with_agent()
        # Use a deliberately-attacker-flavored absolute path.
        resp = client.post(
            "/agents/dymok/transport/transcript-path",
            json={"transcript_path": "/var/log/system.log"},
        )
        assert resp.status_code == 403
        assert "projects" in resp.json()["detail"].lower()

    def test_transcript_path_accepts_valid_path(self):
        """Path under ``~/.claude/projects/`` passes validation (200).

        Test agent has no streaming session registered, so response is
        ``ok: True, session: None`` — the validation gate fired
        successfully, which is what we're pinning."""
        from pathlib import Path
        client = self._client_with_agent()
        projects = Path.home() / ".claude" / "projects" / "test-agent"
        candidate = projects / "test-session.jsonl"
        # Don't need to create the file — endpoint accepts missing
        # files (Claude Code creates them on first append).
        resp = client.post(
            "/agents/dymok/transport/transcript-path",
            json={"transcript_path": str(candidate)},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        # No streaming session registered → endpoint short-circuits with
        # session: None (the path validation already succeeded, which is
        # what this test pins).
        assert body.get("session") is None


# ──────────────────────────────────────────────────────────────────────────
# Tmux pane stream — read-only live viewer endpoint
# ──────────────────────────────────────────────────────────────────────────


class TestTmuxPaneStream:
    """Tests for ``GET /agents/<name>/tmux/pane/stream``.

    SSE endpoint feeding xterm.js in the chat UI. Streams full
    capture-pane snapshots (with ANSI escapes) on a timer. Tests
    pin the boundary conditions; live snapshot streaming is exercised
    by the TmuxSession-level get_pane_snapshot tests.
    """

    def setup_method(self):
        self._tmpdir = tempfile.mkdtemp()
        self._db = os.path.join(self._tmpdir, "test.db")

    def _client_with_agent(self, name: str = "dymok"):
        app = _make_app(self._db)
        client = TestClient(app)
        r = client.post("/agents", json={"name": name, "model": "sonnet"})
        assert r.status_code == 200
        return client

    def test_pane_stream_404_for_unknown_agent(self):
        client = self._client_with_agent()
        resp = client.get("/agents/nobody/tmux/pane/stream")
        assert resp.status_code == 404

    def test_pane_stream_emits_not_tmux_when_no_session(self):
        """No streaming session registered (or session lacks
        ``get_pane_snapshot``) → the generator emits a single
        ``not_tmux`` event and closes. The UI renders a hint instead
        of spinning forever on an empty terminal."""
        import json as _json
        client = self._client_with_agent()
        # Stream the response in raw bytes; the first event should be
        # the not_tmux signal and the stream should close immediately.
        with client.stream("GET", "/agents/dymok/tmux/pane/stream") as resp:
            assert resp.status_code == 200
            assert resp.headers["content-type"].startswith(
                "text/event-stream"
            )
            chunks = []
            for chunk in resp.iter_bytes():
                chunks.append(chunk)
                if len(b"".join(chunks)) > 256:
                    break
            body = b"".join(chunks).decode("utf-8")
            # SSE frame: "data: <json>\n\n"
            assert body.startswith("data: ")
            payload = _json.loads(body[6:].split("\n\n", 1)[0])
            assert payload["type"] == "not_tmux"
            assert payload["agent"] == "dymok"


class TestTmuxPaneKeys:
    """Tests for ``POST /agents/<name>/tmux/pane/keys``.

    Route-level contract for the typeable pane view (Murzik's #737
    follow-up): the API layer must reject bad input — control bytes in
    ``text`` especially, since a literal "\\x04" is C-d and would bypass
    the named-key whitelist — before anything reaches the session.
    Session-layer enforcement is covered in test_tmux_session.py.
    """

    def setup_method(self):
        self._tmpdir = tempfile.mkdtemp()
        self._db = os.path.join(self._tmpdir, "test.db")

    def _client(self, name: str = "dymok"):
        app = _make_app(self._db)
        client = TestClient(app)
        r = client.post("/agents", json={"name": name, "model": "sonnet"})
        assert r.status_code == 200
        return app, client

    def _post(self, client, body, name: str = "dymok"):
        return client.post(f"/agents/{name}/tmux/pane/keys", json=body)

    def test_404_for_unknown_agent(self):
        _, client = self._client()
        assert self._post(client, {"text": "hi"}, name="nobody").status_code == 404

    def test_400_when_both_or_neither_mode(self):
        _, client = self._client()
        assert self._post(client, {}).status_code == 400
        assert self._post(client, {"text": "x", "key": "Enter"}).status_code == 400

    def test_400_on_control_chars_in_text(self):
        """The literal channel must not smuggle control bytes past the
        named-key whitelist: "\\u0004" == C-d, which ``key`` denies."""
        _, client = self._client()
        for text in ("\x04", "ok\x04", "\x1b[A", "\x7f", "a\nb"):
            resp = self._post(client, {"text": text})
            assert resp.status_code == 400, repr(text)
            assert "control" in resp.json()["detail"].lower()

    def test_400_on_overlong_text(self):
        _, client = self._client()
        assert self._post(client, {"text": "x" * 1025}).status_code == 400

    def test_400_on_non_whitelisted_key(self):
        _, client = self._client()
        for key in ("C-d", "kill-server", "F12"):
            assert self._post(client, {"key": key}).status_code == 400, key

    def test_409_without_tmux_session(self):
        """Valid input but no live tmux session (or a session without
        ``send_pane_keys`` — e.g. SDK transport) → 409, not 500."""
        _, client = self._client()
        assert self._post(client, {"text": "hello"}).status_code == 409
        assert self._post(client, {"key": "Enter"}).status_code == 409

    def test_valid_input_reaches_session(self):
        """Happy path: printable text and a whitelisted key are forwarded
        to the session's ``send_pane_keys`` and the API returns sent=True."""
        app, client = self._client()
        calls = []

        class _PaneSession:
            async def send_pane_keys(self, *, text="", key=""):
                calls.append({"text": text, "key": key})
                return True

        app.state.broker.register_streaming("dymok", _PaneSession(), label="main")
        resp = self._post(client, {"text": "ls -la"})
        assert resp.status_code == 200
        assert resp.json() == {"sent": True, "agent": "dymok"}
        resp = self._post(client, {"key": "Enter"})
        assert resp.status_code == 200
        assert calls == [
            {"text": "ls -la", "key": ""},
            {"text": "", "key": "Enter"},
        ]

    def test_502_when_session_send_fails(self):
        app, client = self._client()

        class _FailingPaneSession:
            async def send_pane_keys(self, *, text="", key=""):
                return False

        app.state.broker.register_streaming(
            "dymok", _FailingPaneSession(), label="main"
        )
        assert self._post(client, {"key": "Enter"}).status_code == 502


class TestSessionToolCalls:
    """Tests for ``GET /agents/<name>/sessions/<label>/tool-calls``.

    Frontend Chat.svelte calls this on chat load so the per-turn chip
    strips survive a page refresh — analytics_tool_calls rows (written
    by the PR #527 tmux hooks via ``record_tool_use_start`` /
    ``record_tool_use_finish``) are the source of truth.
    """

    def setup_method(self):
        self._tmpdir = tempfile.mkdtemp()
        self._db = os.path.join(self._tmpdir, "test.db")

    def _client_with_agent(self, name: str = "dymok"):
        app = _make_app(self._db)
        client = TestClient(app)
        r = client.post("/agents", json={"name": name, "model": "sonnet"})
        assert r.status_code == 200
        return client

    def _analytics_store(self):
        from pinky_daemon.analytics_store import AnalyticsStore
        return AnalyticsStore(
            db_path=self._db.replace(".db", "_analytics.db"),
        )

    def test_404_for_unknown_agent(self):
        client = self._client_with_agent()
        resp = client.get("/agents/nobody/sessions/main/tool-calls")
        assert resp.status_code == 404

    def test_empty_list_when_no_rows(self):
        client = self._client_with_agent()
        resp = client.get("/agents/dymok/sessions/main/tool-calls")
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["agent"] == "dymok"
        assert body["session_id"] == "dymok-main"
        assert body["tool_calls"] == []

    def test_returns_oldest_first_with_normalized_fields(self):
        """Returned rows must be chronologically ordered (oldest first)
        and carry description + arg_keys + result_preview pulled from
        the analytics row's metadata — the frontend reuses the
        liveToolCalls chip shape, so all four fields need to be there.
        """
        client = self._client_with_agent()
        store = self._analytics_store()

        # Older call: pinky-self/send_to_agent — finished successfully.
        store.start_tool_call(
            session_id="dymok-main",
            agent_name="dymok",
            turn_seq=None,
            tool_call_key="toolu_aaa",
            tool_name="mcp__pinky-self__send_to_agent",
            tool_namespace="pinky-self",
            metadata={
                "arg_keys": ["message", "to"],
                "description": "send_to_agent → barsik",
            },
            ts="2026-05-17T19:00:00Z",
        )
        store.finish_tool_call(
            session_id="dymok-main",
            agent_name="dymok",
            tool_call_key="toolu_aaa",
            success=True,
            metadata={"result_preview": "sent: true"},
            ts="2026-05-17T19:00:01Z",
        )

        # Newer call: Bash — finished with error.
        store.start_tool_call(
            session_id="dymok-main",
            agent_name="dymok",
            turn_seq=None,
            tool_call_key="toolu_bbb",
            tool_name="Bash",
            tool_namespace="",
            metadata={"arg_keys": ["command"], "description": "Bash — ls /nope"},
            ts="2026-05-17T19:00:10Z",
        )
        store.finish_tool_call(
            session_id="dymok-main",
            agent_name="dymok",
            tool_call_key="toolu_bbb",
            success=False,
            error_type="tool_error",
            metadata={"result_preview": "ls: /nope: no such file"},
            ts="2026-05-17T19:00:11Z",
        )

        resp = client.get("/agents/dymok/sessions/main/tool-calls")
        assert resp.status_code == 200
        calls = resp.json()["tool_calls"]
        assert len(calls) == 2

        first, second = calls
        # Oldest first — chronological order is what the chat UI walks.
        assert first["tool_use_id"] == "toolu_aaa"
        assert first["tool_name"] == "mcp__pinky-self__send_to_agent"
        assert first["tool_namespace"] == "pinky-self"
        assert first["description"] == "send_to_agent → barsik"
        assert first["arg_keys"] == ["message", "to"]
        assert first["result_preview"] == "sent: true"
        assert first["success"] is True
        assert first["is_error"] is False
        assert first["finished"] is True
        assert first["status"] == "ok"

        assert second["tool_use_id"] == "toolu_bbb"
        assert second["tool_name"] == "Bash"
        assert second["description"] == "Bash — ls /nope"
        assert second["result_preview"] == "ls: /nope: no such file"
        assert second["is_error"] is True
        assert second["error_type"] == "tool_error"
        assert second["status"] == "error"
        assert second["success"] is False

    def test_filters_by_session_label(self):
        """A label other than 'main' must scope to the matching
        session_id; rows for other labels of the same agent are excluded.
        """
        client = self._client_with_agent()
        store = self._analytics_store()
        store.start_tool_call(
            session_id="dymok-main",
            agent_name="dymok",
            turn_seq=None,
            tool_call_key="toolu_main",
            tool_name="Read",
            metadata={"arg_keys": ["file_path"]},
            ts="2026-05-17T19:00:00Z",
        )
        store.start_tool_call(
            session_id="dymok-secondary",
            agent_name="dymok",
            turn_seq=None,
            tool_call_key="toolu_other",
            tool_name="Grep",
            metadata={"arg_keys": ["pattern"]},
            ts="2026-05-17T19:00:05Z",
        )

        resp = client.get("/agents/dymok/sessions/secondary/tool-calls")
        assert resp.status_code == 200
        calls = resp.json()["tool_calls"]
        assert len(calls) == 1
        assert calls[0]["tool_use_id"] == "toolu_other"
        assert resp.json()["session_id"] == "dymok-secondary"

    def test_in_flight_call_marked_not_finished(self):
        """A row without an ended_at (status='running') must surface
        with finished=False so the chip strip renders the in-flight
        indicator instead of a success/error icon.
        """
        client = self._client_with_agent()
        store = self._analytics_store()
        store.start_tool_call(
            session_id="dymok-main",
            agent_name="dymok",
            turn_seq=None,
            tool_call_key="toolu_running",
            tool_name="Bash",
            metadata={"arg_keys": ["command"], "description": "Bash — long task"},
            ts="2026-05-17T19:00:00Z",
        )

        resp = client.get("/agents/dymok/sessions/main/tool-calls")
        assert resp.status_code == 200
        calls = resp.json()["tool_calls"]
        assert len(calls) == 1
        assert calls[0]["finished"] is False
        assert calls[0]["status"] == "running"
        assert calls[0]["result_preview"] == ""
