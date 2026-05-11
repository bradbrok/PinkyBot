"""Tests for the effort-drift verification hook (#429).

Covers:
- ``Agent.strict_effort_enforcement`` column round-trip
- ``AgentRegistry.record_effort_drift`` + ``get_effort_drift_events``
- ``ensure_workspace_hooks`` idempotently installs the verify_effort hook
- ``_setup_hooks`` writes valid settings.json with both working + verify hooks
- ``_setup_hooks`` merges verify_effort into pre-existing settings.json
  without clobbering user-added hooks
- Subprocess-level behavior of ``hook_verify_effort.py``:
    - match → silent no-op
    - mismatch + warn mode → POSTs to ``/agents/{name}/effort-drift``
    - mismatch + strict mode → emits a block decision on stdout
    - no expected / actual / agent → silent no-op
    - ``expected=auto`` → silent no-op (intentionally adaptive)
- ``SDKRunnerConfig`` + ``StreamingSessionConfig`` carry the strict flag
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from pinky_daemon.agent_registry import (
    AgentRegistry,
    _verify_effort_hook_source,
)


@pytest.fixture
def registry():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    r = AgentRegistry(db_path=path)
    yield r
    r.close()
    os.unlink(path)


@pytest.fixture
def hook_script(tmp_path):
    """Write the hook source to a temp file we can invoke as a subprocess."""
    p = tmp_path / "hook_verify_effort.py"
    p.write_text(_verify_effort_hook_source())
    return p


class _CaptureHandler(BaseHTTPRequestHandler):
    """Tiny test HTTP server that records POST bodies for assertion."""

    captured: list = []

    def do_POST(self):  # noqa: N802 — http.server interface
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode() if length else ""
        try:
            data = json.loads(body)
        except Exception:
            data = {"raw": body}
        type(self).captured.append({
            "path": self.path,
            "body": data,
            "headers": dict(self.headers),
        })
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok": true}')

    def log_message(self, *_args, **_kwargs):  # silence test noise
        pass


@pytest.fixture
def fake_daemon():
    """Spin up an in-process HTTP server that captures hook POSTs."""

    class _Handler(_CaptureHandler):
        captured: list = []

    server = HTTPServer(("127.0.0.1", 0), _Handler)
    port = server.server_port
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield port, _Handler.captured
    server.shutdown()
    server.server_close()


def _run_hook(
    hook_path: Path,
    *,
    expected: str = "",
    actual: str = "",
    agent: str = "",
    strict: bool = False,
    daemon_url: str = "",
    stdin: str = "",
) -> subprocess.CompletedProcess:
    env = {**os.environ}
    # Wipe any inherited CLAUDE_EFFORT so test env is hermetic
    env.pop("CLAUDE_EFFORT", None)
    env.pop("PINKY_EXPECTED_EFFORT", None)
    env.pop("PINKY_AGENT_NAME", None)
    env.pop("PINKY_STRICT_EFFORT", None)
    env.pop("PINKY_DAEMON_URL", None)
    if actual:
        env["CLAUDE_EFFORT"] = actual
    if expected:
        env["PINKY_EXPECTED_EFFORT"] = expected
    if agent:
        env["PINKY_AGENT_NAME"] = agent
    if strict:
        env["PINKY_STRICT_EFFORT"] = "1"
    if daemon_url:
        env["PINKY_DAEMON_URL"] = daemon_url
    return subprocess.run(
        [sys.executable, str(hook_path)],
        env=env,
        input=stdin,
        capture_output=True,
        text=True,
        timeout=10,
    )


# ── Schema / dataclass ─────────────────────────────────────


class TestStrictEffortColumn:
    def test_default_false(self, registry):
        agent = registry.register("alpha", model="opus")
        assert agent.strict_effort_enforcement is False

    def test_set_via_register(self, registry):
        agent = registry.register("alpha", strict_effort_enforcement=True)
        assert agent.strict_effort_enforcement is True

    def test_update_round_trip(self, registry):
        registry.register("alpha")
        registry.register("alpha", strict_effort_enforcement=True)
        got = registry.get("alpha")
        assert got is not None
        assert got.strict_effort_enforcement is True
        # And back to off
        registry.register("alpha", strict_effort_enforcement=False)
        got2 = registry.get("alpha")
        assert got2 is not None
        assert got2.strict_effort_enforcement is False

    def test_to_dict_includes_field(self, registry):
        agent = registry.register("alpha", strict_effort_enforcement=True)
        d = agent.to_dict()
        assert d["strict_effort_enforcement"] is True


# ── record_effort_drift + get_effort_drift_events ──────────


class TestEffortDriftRecording:
    def test_record_writes_row(self, registry):
        registry.register("alpha")
        event_id = registry.record_effort_drift(
            "alpha", expected="high", actual="medium",
            session_id="sess-1", tool_name="Bash", strict=False,
        )
        assert event_id > 0
        events = registry.get_effort_drift_events("alpha")
        assert len(events) == 1
        e = events[0]
        assert e["expected"] == "high"
        assert e["actual"] == "medium"
        assert e["tool_name"] == "Bash"
        assert e["strict"] is False
        assert e["agent_name"] == "alpha"

    def test_record_writes_heartbeat_note(self, registry):
        registry.register("alpha")
        registry.record_effort_drift(
            "alpha", expected="max", actual="low", strict=True,
        )
        hb = registry.get_latest_heartbeat("alpha")
        assert hb is not None
        assert "effort drift" in hb.notes
        assert "blocked" in hb.notes  # strict label
        assert "expected=max" in hb.notes
        assert "actual=low" in hb.notes

    def test_record_warn_label_in_note(self, registry):
        registry.register("alpha")
        registry.record_effort_drift(
            "alpha", expected="high", actual="medium", strict=False,
        )
        hb = registry.get_latest_heartbeat("alpha")
        assert hb is not None
        assert "warn" in hb.notes

    def test_get_filters_by_agent(self, registry):
        registry.register("alpha")
        registry.register("beta")
        registry.record_effort_drift("alpha", expected="high", actual="low")
        registry.record_effort_drift("beta", expected="high", actual="medium")
        a_events = registry.get_effort_drift_events("alpha")
        b_events = registry.get_effort_drift_events("beta")
        assert len(a_events) == 1
        assert len(b_events) == 1
        assert a_events[0]["actual"] == "low"
        assert b_events[0]["actual"] == "medium"

    def test_get_filters_by_since(self, registry):
        import time

        registry.register("alpha")
        registry.record_effort_drift("alpha", expected="high", actual="low")
        cutoff = time.time() + 0.01
        time.sleep(0.05)
        registry.record_effort_drift("alpha", expected="high", actual="medium")
        events = registry.get_effort_drift_events("alpha", since=cutoff)
        # Only the second event should be returned
        assert len(events) == 1
        assert events[0]["actual"] == "medium"

    def test_get_fleet_wide(self, registry):
        registry.register("alpha")
        registry.register("beta")
        registry.record_effort_drift("alpha", expected="high", actual="low")
        registry.record_effort_drift("beta", expected="high", actual="medium")
        all_events = registry.get_effort_drift_events("")
        assert len(all_events) == 2


# ── settings.json installer ────────────────────────────────


class TestHookInstaller:
    def test_setup_creates_verify_effort_script(self, tmp_path):
        AgentRegistry._setup_hooks(tmp_path, "alpha")
        assert (tmp_path / ".claude" / "hook_verify_effort.py").exists()

    def test_setup_settings_has_verify_hook(self, tmp_path):
        AgentRegistry._setup_hooks(tmp_path, "alpha")
        settings = json.loads(
            (tmp_path / ".claude" / "settings.json").read_text()
        )
        commands = [
            h["command"]
            for entry in settings["hooks"]["PreToolUse"]
            for h in entry["hooks"]
        ]
        assert any("hook_verify_effort.py" in c for c in commands), commands
        # And working/idle still present
        assert any("hook_working.py" in c for c in commands), commands

    def test_setup_idempotent_double_call(self, tmp_path):
        AgentRegistry._setup_hooks(tmp_path, "alpha")
        first = (tmp_path / ".claude" / "settings.json").read_text()
        AgentRegistry._setup_hooks(tmp_path, "alpha")
        second = (tmp_path / ".claude" / "settings.json").read_text()
        # The file should be unchanged on second call (no duplicate hooks)
        # Count verify_effort references — must be exactly 1
        assert second.count("hook_verify_effort.py") == 1
        # And working count is also 1
        assert second.count("hook_working.py") == first.count("hook_working.py")

    def test_setup_merges_into_legacy_settings(self, tmp_path):
        # Simulate an old agent: settings.json exists with only working hook.
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        legacy = {
            "hooks": {
                "PreToolUse": [{
                    "matcher": ".*",
                    "hooks": [{
                        "type": "command",
                        "command": "python3 /custom/user-hook.py || true",
                    }],
                }],
            }
        }
        (claude_dir / "settings.json").write_text(json.dumps(legacy))

        AgentRegistry._setup_hooks(tmp_path, "alpha")
        merged = json.loads((claude_dir / "settings.json").read_text())
        commands = [
            h["command"]
            for entry in merged["hooks"]["PreToolUse"]
            for h in entry["hooks"]
        ]
        # User's custom hook preserved
        assert any("user-hook.py" in c for c in commands), commands
        # Verify hook added
        assert any("hook_verify_effort.py" in c for c in commands), commands

    def test_ensure_workspace_hooks_no_agent(self, registry):
        # Calling on a non-existent agent must not raise.
        registry.ensure_workspace_hooks("nonexistent")  # should no-op

    def test_ensure_workspace_hooks_installs(self, registry, tmp_path):
        registry.register(
            "alpha",
            working_dir=str(tmp_path / "agent"),
        )
        # Initial register already runs _setup_hooks; remove the
        # verify_effort entry to simulate pre-#429 state.
        settings_path = tmp_path / "agent" / ".claude" / "settings.json"
        settings = json.loads(settings_path.read_text())
        for entry in settings["hooks"]["PreToolUse"]:
            entry["hooks"] = [
                h for h in entry["hooks"]
                if "hook_verify_effort" not in (h.get("command") or "")
            ]
        settings_path.write_text(json.dumps(settings))
        assert "hook_verify_effort" not in settings_path.read_text()

        # Now re-ensure — hook should be re-added.
        registry.ensure_workspace_hooks("alpha")
        assert "hook_verify_effort" in settings_path.read_text()


# ── Hook subprocess behavior ───────────────────────────────


class TestVerifyEffortHookBehavior:
    def test_match_silent(self, hook_script):
        proc = _run_hook(
            hook_script, expected="high", actual="high", agent="alpha",
        )
        assert proc.returncode == 0
        assert proc.stdout == ""

    def test_no_expected_silent(self, hook_script):
        proc = _run_hook(hook_script, actual="high", agent="alpha")
        assert proc.returncode == 0
        assert proc.stdout == ""

    def test_no_actual_silent(self, hook_script):
        # Older Claude Code without v2.1.133 effort.level
        proc = _run_hook(hook_script, expected="high", agent="alpha")
        assert proc.returncode == 0
        assert proc.stdout == ""

    def test_auto_expected_silent(self, hook_script):
        proc = _run_hook(
            hook_script, expected="auto", actual="low", agent="alpha",
        )
        assert proc.returncode == 0
        assert proc.stdout == ""

    def test_no_agent_silent(self, hook_script):
        # Hook misconfigured — no agent name — must not crash + no output
        proc = _run_hook(
            hook_script, expected="high", actual="low",
        )
        assert proc.returncode == 0
        assert proc.stdout == ""

    def test_mismatch_warn_posts(self, hook_script, fake_daemon):
        port, captured = fake_daemon
        proc = _run_hook(
            hook_script,
            expected="high", actual="medium", agent="alpha",
            daemon_url=f"http://127.0.0.1:{port}",
        )
        assert proc.returncode == 0
        # Warn mode → no block decision printed
        assert proc.stdout == ""
        assert len(captured) == 1, captured
        msg = captured[0]
        assert msg["path"] == "/agents/alpha/effort-drift"
        assert msg["body"]["expected"] == "high"
        assert msg["body"]["actual"] == "medium"
        assert msg["body"]["strict"] is False

    def test_mismatch_strict_emits_block(self, hook_script, fake_daemon):
        port, captured = fake_daemon
        proc = _run_hook(
            hook_script,
            expected="high", actual="low", agent="alpha", strict=True,
            daemon_url=f"http://127.0.0.1:{port}",
        )
        assert proc.returncode == 0
        # Strict mode → block decision on stdout
        decision = json.loads(proc.stdout)
        assert decision["decision"] == "block"
        assert "high" in decision["reason"]
        assert "low" in decision["reason"]
        # Plus the drift POST still happens
        assert len(captured) == 1
        assert captured[0]["body"]["strict"] is True

    def test_mismatch_with_tool_name_from_stdin(self, hook_script, fake_daemon):
        port, captured = fake_daemon
        stdin_payload = json.dumps({"tool_name": "Bash", "session_id": "s1"})
        proc = _run_hook(
            hook_script,
            expected="high", actual="medium", agent="alpha",
            daemon_url=f"http://127.0.0.1:{port}",
            stdin=stdin_payload,
        )
        assert proc.returncode == 0
        assert len(captured) == 1
        assert captured[0]["body"]["tool_name"] == "Bash"


# ── Config plumbing ────────────────────────────────────────


class TestSDKRunnerConfigField:
    def test_default(self):
        from pinky_daemon.sdk_runner import SDKRunnerConfig
        cfg = SDKRunnerConfig()
        assert cfg.strict_effort_enforcement is False

    def test_set_true(self):
        from pinky_daemon.sdk_runner import SDKRunnerConfig
        cfg = SDKRunnerConfig(strict_effort_enforcement=True)
        assert cfg.strict_effort_enforcement is True


class TestStreamingSessionConfigField:
    def test_default(self):
        from pinky_daemon.streaming_session import StreamingSessionConfig
        cfg = StreamingSessionConfig(agent_name="x", working_dir="/tmp")
        assert cfg.strict_effort_enforcement is False

    def test_set_true(self):
        from pinky_daemon.streaming_session import StreamingSessionConfig
        cfg = StreamingSessionConfig(
            agent_name="x", working_dir="/tmp",
            strict_effort_enforcement=True,
        )
        assert cfg.strict_effort_enforcement is True
