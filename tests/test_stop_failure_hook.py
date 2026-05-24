"""Tests for the StopFailure CC hook + the
``/agents/{name}/transport/stop-failure`` endpoint.

Claude Code fires ``StopFailure`` when a turn ends due to an API error,
carrying a typed ``error_type``. The hook forwards it so the daemon can:

  - log every failure for observability (all classes), and
  - route auth-class failures (authentication_failed / oauth_org_not_allowed)
    into the shared ``AuthFailureTracker`` — the same proactive operator-alert
    path the SDK reader loop uses. tmux/CLI agents have no SDK reader loop, so
    this hook is the only thing that surfaces a dead token before the agent
    goes silently dark.

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
        return client, calls

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

    def test_rate_limit_not_routed(self):
        """Transient class — logged for observability, not routed to the
        auth tracker, no operator alert."""
        client, calls = self._client_with_agent()
        r = client.post(
            "/agents/dymok/transport/stop-failure",
            json={"error_type": "rate_limit"},
        )
        assert r.status_code == 200
        assert r.json()["auth_failure"] is False
        assert calls == []

    def test_billing_error_not_routed_to_auth(self):
        """billing_error is a different remedy than re-auth — logged, not
        routed through the auth tracker."""
        client, calls = self._client_with_agent()
        r = client.post(
            "/agents/dymok/transport/stop-failure",
            json={"error_type": "billing_error"},
        )
        assert r.status_code == 200
        assert r.json()["auth_failure"] is False
        assert calls == []

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
