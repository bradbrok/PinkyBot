"""Tests for the StopFailure CC hook + the
``/agents/{name}/transport/stop-failure`` endpoint.

Claude Code fires ``StopFailure`` when a turn ends due to an API error,
carrying a typed ``error_type``. The hook forwards it so the daemon can:

  - log every failure for observability (all classes), and
  - route terminal main-thread auth failures
    (authentication_failed / oauth_org_not_allowed) into the shared
    ``AuthFailureTracker`` — the same proactive operator-alert path the SDK
    reader loop uses. Fan-out-child failures stay observable but do not count
    as independent host-auth evidence. tmux/CLI agents have no SDK reader
    loop, so this hook is the only thing that surfaces a dead token before the
    agent goes silently dark.

The tracker's threshold/cooldown/host-wide logic and alert formatting are
covered by ``test_auth_alerts.py`` — here we pin that the endpoint *routes*
correctly and classifies error types.
"""

from __future__ import annotations

import json
import os
import tempfile

from fastapi.testclient import TestClient

from pinky_daemon.agent_registry import AgentRegistry


def _make_app(db_path: str):
    from pinky_daemon.api import create_api
    return create_api(max_sessions=10, default_working_dir="/tmp", db_path=db_path)


# ── Endpoint routing ───────────────────────────────────────────


class TestStopFailureEndpoint:
    def setup_method(self):
        self._tmpdir = tempfile.mkdtemp()
        self._db = os.path.join(self._tmpdir, "test.db")

    def _client_with_agent(self, name: str = "dymok"):
        app = _make_app(self._db)
        client = TestClient(app)
        r = client.post("/agents", json={"name": name, "model": "sonnet"})
        assert r.status_code == 200
        # Spy the shared auth tracker's record_failure so we can assert the
        # endpoint routes auth-class failures into it without triggering a
        # real operator DM. Returning should_alert=False short-circuits
        # _on_auth_failure before any send.
        calls: list[tuple[str, str]] = []

        def _spy(agent_name, error=""):
            calls.append((agent_name, error))
            return {
                "should_alert": False,
                "reason": "below_threshold",
                "count": len(calls),
                "agents_failing": 1,
            }

        app.state.auth_tracker.record_failure = _spy
        self._app = app
        return client, calls

    def _spy_transport_tracker(self, error_type: str) -> list:
        """Spy a per-class transport tracker's record_failure (#104).

        Returns a list that records (agent_name, error) calls and keeps the
        tracker below threshold (should_alert=False) so no real operator DM is
        attempted — mirrors the auth spy in ``_client_with_agent``.
        """
        calls: list[tuple[str, str]] = []

        def _spy(agent_name, error=""):
            calls.append((agent_name, error))
            return {
                "should_alert": False,
                "reason": "below_threshold",
                "count": len(calls),
                "agents_failing": 1,
            }

        self._app.state.transport_failure_trackers[error_type].record_failure = _spy
        return calls

    def test_unknown_agent_404(self):
        client, calls = self._client_with_agent()
        r = client.post(
            "/agents/nobody/transport/stop-failure",
            json={"error_type": "authentication_failed"},
        )
        assert r.status_code == 404
        assert calls == []

    def test_auth_failed_routes_to_tracker(self):
        client, calls = self._client_with_agent()
        r = client.post(
            "/agents/dymok/transport/stop-failure",
            json={"error_type": "authentication_failed", "message": "401"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["error_type"] == "authentication_failed"
        assert body["auth_failure"] is True
        assert calls == [("dymok", "authentication_failed")]

    def test_oauth_org_not_allowed_routes_to_tracker(self):
        """oauth_org_not_allowed is auth-adjacent (same re-auth remedy)."""
        client, calls = self._client_with_agent()
        r = client.post(
            "/agents/dymok/transport/stop-failure",
            json={"error_type": "oauth_org_not_allowed"},
        )
        assert r.status_code == 200
        assert r.json()["auth_failure"] is True
        assert calls == [("dymok", "oauth_org_not_allowed")]

    def test_rate_limit_routes_to_rate_limit_tracker_not_auth(self):
        """#104: rate_limit routes to its OWN per-class tracker (sustained-
        throttle alert), never the auth tracker."""
        client, auth_calls = self._client_with_agent()
        rl_calls = self._spy_transport_tracker("rate_limit")
        r = client.post(
            "/agents/dymok/transport/stop-failure",
            json={"error_type": "rate_limit"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["auth_failure"] is False
        assert body["alert_routed"] is True
        assert auth_calls == []  # NOT the auth tracker
        assert rl_calls == [("dymok", "rate_limit")]  # its own tracker

    def test_billing_error_routes_to_billing_tracker_not_auth(self):
        """#104: billing_error routes to its OWN per-class tracker with a
        billing remedy (re-auth wouldn't help), never the auth tracker."""
        client, auth_calls = self._client_with_agent()
        billing_calls = self._spy_transport_tracker("billing_error")
        r = client.post(
            "/agents/dymok/transport/stop-failure",
            json={"error_type": "billing_error"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["auth_failure"] is False
        assert body["alert_routed"] is True
        assert auth_calls == []  # NOT the auth tracker
        assert billing_calls == [("dymok", "billing_error")]  # its own tracker

    def test_server_error_routed_nowhere(self):
        """#104: server_error is Anthropic-side / self-resolving → log-only.
        Not routed to the auth tracker nor any per-class tracker."""
        client, auth_calls = self._client_with_agent()
        rl_calls = self._spy_transport_tracker("rate_limit")
        billing_calls = self._spy_transport_tracker("billing_error")
        r = client.post(
            "/agents/dymok/transport/stop-failure",
            json={"error_type": "server_error"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["auth_failure"] is False
        assert body["alert_routed"] is False
        assert auth_calls == []
        assert rl_calls == []
        assert billing_calls == []

    def test_missing_error_type_defaults_unknown(self):
        client, calls = self._client_with_agent()
        r = client.post("/agents/dymok/transport/stop-failure", json={})
        assert r.status_code == 200
        assert r.json()["error_type"] == "unknown"
        assert r.json()["auth_failure"] is False
        assert calls == []

    def test_repeated_auth_failures_each_recorded(self):
        """Each StopFailure is one tracker record — the threshold logic
        (3-in-window) then lives in the tracker, covered by
        test_auth_alerts.py. Here we pin that N posts → N records."""
        client, calls = self._client_with_agent()
        for _ in range(3):
            client.post(
                "/agents/dymok/transport/stop-failure",
                json={"error_type": "authentication_failed"},
            )
        assert calls == [("dymok", "authentication_failed")] * 3

    def test_subagent_auth_failure_is_observed_but_not_paged(self):
        """A fan-out child has its own terminal StopFailure, but it does not
        prove the agent's shared credential is broken. Claude's main thread
        may still retry and finish successfully, as in #355."""
        client, calls = self._client_with_agent()
        r = client.post(
            "/agents/dymok/transport/stop-failure",
            json={
                "error_type": "authentication_failed",
                "agent_id": "child-a1b2",
                "agent_type": "Explore",
            },
        )
        assert r.status_code == 200
        body = r.json()
        assert body["auth_failure"] is True
        assert body["subagent"] is True
        assert body["alert_routed"] is False
        assert calls == []


# ── Observability ──────────────────────────────────────────────


class TestStopFailureObservability:
    def setup_method(self):
        self._tmpdir = tempfile.mkdtemp()
        self._db = os.path.join(self._tmpdir, "test.db")

    def test_failure_logged_to_activity(self):
        app = _make_app(self._db)
        with TestClient(app) as client:
            client.post("/agents", json={"name": "pix", "model": "sonnet"})
            client.post(
                "/agents/pix/transport/stop-failure",
                json={"error_type": "rate_limit"},
            )
            resp = client.get("/activity", params={"agent_name": "pix"})
            assert resp.status_code == 200
            types = [e["event_type"] for e in resp.json()["events"]]
            assert "cc_stop_failure" in types


# ── Hook installation ──────────────────────────────────────────


class TestStopFailureHookInstall:
    def test_setup_creates_stop_failure_script(self, tmp_path):
        AgentRegistry._setup_hooks(tmp_path, "alpha")
        script = tmp_path / ".claude" / "hook_tmux_stop_failure.py"
        assert script.exists()
        src = script.read_text()
        assert "transport/stop-failure" in src
        assert "error_type" in src
        # Reads CC's real ``error`` field — regression: the hook used to
        # read only ``error_type``, which CC never sends.
        assert 'payload_in.get("error")' in src

    def test_settings_wires_stop_failure_event(self, tmp_path):
        AgentRegistry._setup_hooks(tmp_path, "alpha")
        settings = json.loads(
            (tmp_path / ".claude" / "settings.json").read_text()
        )
        assert "StopFailure" in settings["hooks"]
        cmds = [
            h["command"]
            for entry in settings["hooks"]["StopFailure"]
            for h in entry["hooks"]
        ]
        assert any("hook_tmux_stop_failure.py" in c for c in cmds)

    def test_merge_into_legacy_settings(self, tmp_path):
        """An agent whose settings.json predates StopFailure gets the
        bucket backfilled without nuking existing hooks."""
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        legacy = {
            "hooks": {
                "Stop": [
                    {
                        "matcher": ".*",
                        "hooks": [{"type": "command", "command": "echo legacy"}],
                    }
                ]
            }
        }
        (claude_dir / "settings.json").write_text(json.dumps(legacy))
        AgentRegistry._setup_hooks(tmp_path, "alpha")
        merged = json.loads((claude_dir / "settings.json").read_text())
        assert "StopFailure" in merged["hooks"]
        cmds = [
            h["command"]
            for entry in merged["hooks"]["StopFailure"]
            for h in entry["hooks"]
        ]
        assert any("hook_tmux_stop_failure.py" in c for c in cmds)
        stop_cmds = [
            h["command"]
            for entry in merged["hooks"]["Stop"]
            for h in entry["hooks"]
        ]
        assert any("echo legacy" in c for c in stop_cmds)

    def test_setup_idempotent(self, tmp_path):
        AgentRegistry._setup_hooks(tmp_path, "alpha")
        first = (tmp_path / ".claude" / "settings.json").read_text()
        AgentRegistry._setup_hooks(tmp_path, "alpha")
        second = (tmp_path / ".claude" / "settings.json").read_text()
        assert first == second


# ── Hook payload contract (real Claude Code StopFailure schema) ──


class TestStopFailureHookPayloadContract:
    """Exercise the *generated hook source* against Claude Code's real
    StopFailure payload. CC delivers the typed failure in the ``error``
    field (NOT ``error_type``):
    https://code.claude.com/docs/en/hooks#stopfailure-input

    Regression for the silent no-op where the hook read ``error_type`` — a
    field CC never sends — so every production StopFailure posted
    ``unknown`` and auth failures never reached the tracker. The endpoint
    tests above post ``error_type`` directly, so they never caught this;
    these run the hook's actual stdin parse.
    """

    def _run_hook(self, payload: dict) -> dict:
        """Run the generated hook source against ``payload`` on stdin,
        intercepting urllib so nothing leaves the process. Returns the
        decoded JSON body the hook would POST."""
        import io
        import sys
        import urllib.request

        from pinky_daemon.agent_registry import _tmux_stop_failure_hook_source

        src = _tmux_stop_failure_hook_source("dymok")
        captured: dict = {}

        def _fake_urlopen(req, timeout=0):
            captured["url"] = req.full_url
            captured["body"] = json.loads(req.data.decode())
            return None

        real_stdin, real_urlopen = sys.stdin, urllib.request.urlopen
        real_secret = os.environ.get("PINKY_SESSION_SECRET")
        try:
            sys.stdin = io.StringIO(json.dumps(payload))
            urllib.request.urlopen = _fake_urlopen
            os.environ["PINKY_SESSION_SECRET"] = "test-secret"
            exec(
                compile(src, "<stop_failure_hook>", "exec"),
                {"__name__": "__main__"},
            )
        finally:
            sys.stdin = real_stdin
            urllib.request.urlopen = real_urlopen
            if real_secret is None:
                os.environ.pop("PINKY_SESSION_SECRET", None)
            else:
                os.environ["PINKY_SESSION_SECRET"] = real_secret
        return captured

    def test_cc_error_field_maps_to_error_type(self):
        """The real CC payload carries the typed failure in ``error``."""
        captured = self._run_hook(
            {
                "hook_event_name": "StopFailure",
                "error": "authentication_failed",
                "error_details": "401 Unauthorized",
                "last_assistant_message": "API Error: authentication_failed",
                "session_id": "abc123",
            }
        )
        assert captured["url"].endswith("/agents/dymok/transport/stop-failure")
        assert captured["body"]["error_type"] == "authentication_failed"
        assert captured["body"]["session_id"] == "abc123"
        # message prefers the rendered error text CC surfaces
        assert captured["body"]["message"] == "API Error: authentication_failed"

    def test_subagent_identity_is_forwarded(self):
        captured = self._run_hook(
            {
                "error": "authentication_failed",
                "session_id": "session-1",
                "agent_id": "child-a1b2",
                "agent_type": "Explore",
            }
        )
        assert captured["body"]["agent_id"] == "child-a1b2"
        assert captured["body"]["agent_type"] == "Explore"

    def test_error_details_used_when_no_last_message(self):
        captured = self._run_hook(
            {"error": "rate_limit", "error_details": "429 Too Many Requests"}
        )
        assert captured["body"]["error_type"] == "rate_limit"
        assert captured["body"]["message"] == "429 Too Many Requests"

    def test_error_type_alias_still_honored(self):
        """Defensive: internal callers/tests that post ``error_type`` work."""
        captured = self._run_hook({"error_type": "billing_error"})
        assert captured["body"]["error_type"] == "billing_error"

    def test_no_typed_field_defaults_unknown(self):
        captured = self._run_hook({"hook_event_name": "StopFailure"})
        assert captured["body"]["error_type"] == "unknown"


# ── In-flight turn resolution (#108) ───────────────────────────


class _FakeTmuxSession:
    """Minimal stand-in exposing ``handle_stop_failure`` so the endpoint's
    duck-typed resolve path can be exercised without a real tmux REPL."""

    def __init__(self, resolved: bool = True):
        self.calls: list[tuple[str, str, str]] = []
        self._resolved = resolved

    async def handle_stop_failure(
        self, error_type: str, message: str = "", session_id: str = "",
    ) -> bool:
        self.calls.append((error_type, message, session_id))
        return self._resolved


class TestStopFailureTurnResolve:
    """#108 — the endpoint duck-calls ``TmuxSession.handle_stop_failure``
    AFTER its #584 logging + auth routing, so a StopFailure-ended tmux turn
    is unwedged immediately instead of aging out the 10-min inflight
    watchdog. Sessions without the method (SDK/Codex) and absent sessions
    degrade to ``turn_resolved=False`` without failing the hook.
    """

    def setup_method(self):
        self._tmpdir = tempfile.mkdtemp()
        self._db = os.path.join(self._tmpdir, "test.db")

    def _client(self, name: str = "dymok"):
        app = _make_app(self._db)
        client = TestClient(app)
        r = client.post("/agents", json={"name": name, "model": "sonnet"})
        assert r.status_code == 200
        calls: list[tuple[str, str]] = []

        def _spy(agent_name, error=""):
            calls.append((agent_name, error))
            return {
                "should_alert": False,
                "reason": "below_threshold",
                "count": len(calls),
                "agents_failing": 1,
            }

        app.state.auth_tracker.record_failure = _spy
        return client, app, calls

    def test_no_live_session_resolves_false(self):
        """No registered session → turn_resolved False, no crash."""
        client, _app, _calls = self._client()
        r = client.post(
            "/agents/dymok/transport/stop-failure",
            json={"error_type": "rate_limit"},
        )
        assert r.status_code == 200
        assert r.json()["turn_resolved"] is False

    def test_resolves_inflight_via_live_session(self):
        """A live tmux session gets handle_stop_failure called with the
        forwarded error_type / message / session_id."""
        client, app, _calls = self._client()
        fake = _FakeTmuxSession(resolved=True)
        app.state.broker._streaming["dymok"] = {"main": fake}
        r = client.post(
            "/agents/dymok/transport/stop-failure",
            json={
                "error_type": "rate_limit",
                "message": "429 Too Many Requests",
                "session_id": "s1",
            },
        )
        assert r.status_code == 200
        assert r.json()["turn_resolved"] is True
        assert fake.calls == [("rate_limit", "429 Too Many Requests", "s1")]

    def test_auth_routing_preserved_alongside_resolve(self):
        """#584 auth alert AND #108 turn resolve both fire for an
        auth-class failure — the resolve is additive, not a replacement."""
        client, app, calls = self._client()
        fake = _FakeTmuxSession(resolved=True)
        app.state.broker._streaming["dymok"] = {"main": fake}
        r = client.post(
            "/agents/dymok/transport/stop-failure",
            json={"error_type": "authentication_failed"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["auth_failure"] is True
        assert body["turn_resolved"] is True
        # #584 path intact.
        assert calls == [("dymok", "authentication_failed")]
        # #108 path also ran.
        assert fake.calls and fake.calls[0][0] == "authentication_failed"

    def test_subagent_failure_does_not_resolve_parent_turn(self):
        """A child hook shares the Pinky agent endpoint but is not the
        parent tmux turn-end marker. Resolving here would pop the live parent
        turn while Claude Code is still retrying it."""
        client, app, calls = self._client()
        fake = _FakeTmuxSession(resolved=True)
        app.state.broker._streaming["dymok"] = {"main": fake}
        r = client.post(
            "/agents/dymok/transport/stop-failure",
            json={
                "error_type": "authentication_failed",
                "agent_id": "child-a1b2",
            },
        )
        assert r.status_code == 200
        body = r.json()
        assert body["subagent"] is True
        assert body["turn_resolved"] is False
        assert calls == []
        assert fake.calls == []

    def test_session_without_method_degrades(self):
        """An SDK/Codex session lacking handle_stop_failure → turn_resolved
        False, hook still 200 (duck-typed, tolerated absence)."""
        client, app, _calls = self._client()
        app.state.broker._streaming["dymok"] = {"main": object()}
        r = client.post(
            "/agents/dymok/transport/stop-failure",
            json={"error_type": "rate_limit"},
        )
        assert r.status_code == 200
        assert r.json()["turn_resolved"] is False

    def test_resolver_exception_does_not_fail_hook(self):
        """A raising handle_stop_failure is swallowed — fire-and-forget; the
        hook must never fail the model turn."""
        client, app, _calls = self._client()

        class _Boom:
            async def handle_stop_failure(self, *a, **k):
                raise RuntimeError("boom")

        app.state.broker._streaming["dymok"] = {"main": _Boom()}
        r = client.post(
            "/agents/dymok/transport/stop-failure",
            json={"error_type": "rate_limit"},
        )
        assert r.status_code == 200
        assert r.json()["turn_resolved"] is False


# ── Main-thread success clear ──────────────────────────────────


class TestStopSuccessAuthClear:
    def setup_method(self):
        self._tmpdir = tempfile.mkdtemp()
        self._db = os.path.join(self._tmpdir, "test.db")

    def _client(self):
        app = _make_app(self._db)
        client = TestClient(app)
        r = client.post("/agents", json={"name": "dymok", "model": "sonnet"})
        assert r.status_code == 200
        calls: list[str] = []
        app.state.auth_tracker.record_success = calls.append
        return client, calls

    def test_main_stop_clears_tmux_auth_failures_even_without_live_session(self):
        client, calls = self._client()
        r = client.post(
            "/agents/dymok/transport/wake",
            json={"event": "stop_hook_summary", "session_id": "main-session"},
        )
        assert r.status_code == 200
        assert r.json()["session"] is None
        assert calls == ["dymok"]

    def test_subagent_stop_does_not_clear_main_auth_failure_state(self):
        client, calls = self._client()
        r = client.post(
            "/agents/dymok/transport/wake",
            json={
                "event": "stop_hook_summary",
                "agent_id": "child-a1b2",
                "agent_type": "Explore",
            },
        )
        assert r.status_code == 200
        assert calls == []

    def test_main_success_breaks_terminal_auth_failure_streak(self):
        """Two isolated failed turns around a successful turn are not a
        sustained three-failure outage. The Stop hook must clear the first
        pair before the next terminal failure is recorded."""
        app = _make_app(self._db)
        client = TestClient(app)
        r = client.post("/agents", json={"name": "dymok", "model": "sonnet"})
        assert r.status_code == 200

        for _ in range(2):
            r = client.post(
                "/agents/dymok/transport/stop-failure",
                json={"error_type": "authentication_failed"},
            )
            assert r.status_code == 200
        before = app.state.auth_tracker.status()
        assert before["status"] == "degraded"
        assert before["agents_failing"][0]["failures_in_window"] == 2

        r = client.post(
            "/agents/dymok/transport/wake",
            json={"event": "stop_hook_summary", "session_id": "main-session"},
        )
        assert r.status_code == 200
        assert app.state.auth_tracker.status()["status"] == "ok"

        r = client.post(
            "/agents/dymok/transport/stop-failure",
            json={"error_type": "authentication_failed"},
        )
        assert r.status_code == 200
        after = app.state.auth_tracker.status()
        assert after["status"] == "degraded"
        assert after["agents_failing"][0]["failures_in_window"] == 1


class TestStopHookPayloadContract:
    def _run_hook(self, payload: dict) -> dict:
        import io
        import sys
        import urllib.request

        from pinky_daemon.agent_registry import _tmux_wake_hook_source

        src = _tmux_wake_hook_source("dymok")
        captured: dict = {}

        def _fake_urlopen(req, timeout=0):
            captured["url"] = req.full_url
            captured["body"] = json.loads(req.data.decode())
            return None

        real_stdin, real_urlopen = sys.stdin, urllib.request.urlopen
        real_secret = os.environ.get("PINKY_SESSION_SECRET")
        try:
            sys.stdin = io.StringIO(json.dumps(payload))
            urllib.request.urlopen = _fake_urlopen
            os.environ["PINKY_SESSION_SECRET"] = "test-secret"
            exec(
                compile(src, "<stop_hook>", "exec"),
                {"__name__": "__main__"},
            )
        finally:
            sys.stdin = real_stdin
            urllib.request.urlopen = real_urlopen
            if real_secret is None:
                os.environ.pop("PINKY_SESSION_SECRET", None)
            else:
                os.environ["PINKY_SESSION_SECRET"] = real_secret
        return captured

    def test_forwards_main_and_subagent_identity_fields(self):
        captured = self._run_hook(
            {
                "session_id": "session-1",
                "agent_id": "child-a1b2",
                "agent_type": "Explore",
            }
        )
        assert captured["url"].endswith("/agents/dymok/transport/wake")
        assert captured["body"] == {
            "event": "stop_hook_summary",
            "session_id": "session-1",
            "agent_id": "child-a1b2",
            "agent_type": "Explore",
        }
