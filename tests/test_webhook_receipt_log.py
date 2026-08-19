"""Tests for the /hooks/{token} unconditional receipt log.

Every delivery attempt must leave a receipt line, including the early-drop
paths (unknown-token 404, IP/per-token 429, oversized 413) that previously
exited with no log at all — making a dropped delivery indistinguishable from
a sender that never sent. The full token is a credential and must never be
logged; only its prefix may appear.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pinky_daemon.routes import triggers as triggers_module

TOKEN = "whk_valid_token_abcdef1234567890"
UNKNOWN_TOKEN = "whk_unknown_token_0987654321fedc"


@dataclass
class _Trigger:
    id: int = 1
    name: str = "t1"
    agent_name: str = "agent-a"
    prompt_template: str = "fired: {{body_raw}}"


class _Store:
    def __init__(self):
        self.fired: list[int] = []

    def get_by_token(self, token: str):
        return _Trigger() if token == TOKEN else None

    def record_fire(self, trigger_id: int) -> None:
        self.fired.append(trigger_id)


def _wire(monkeypatch) -> tuple[FastAPI, list[str]]:
    monkeypatch.setattr(triggers_module, "_hook_rate_buckets", {})
    monkeypatch.setattr(triggers_module, "_hook_ip_buckets", {})
    logs: list[str] = []

    async def wake(agent_name: str, session_id: str, prompt: str) -> None:
        return None

    triggers_module.set_dependencies(
        trigger_store=_Store(),
        agents=None,
        log=logs.append,
        wake_callback=wake,
    )
    app = FastAPI()
    app.include_router(triggers_module.router)
    return app, logs


@pytest.fixture()
def rig(monkeypatch):
    app, logs = _wire(monkeypatch)
    return TestClient(app), logs


def _receipts(logs: list[str]) -> list[str]:
    return [line for line in logs if line.startswith("hooks: receipt ")]


def _assert_token_never_logged(logs: list[str], token: str) -> None:
    assert all(token not in line for line in logs), "full token leaked into logs"


class TestWebhookReceiptLog:
    def test_success_path_logs_receipt_with_prefix_only(self, rig):
        client, logs = rig
        response = client.post(
            f"/hooks/{TOKEN}", json={"k": "v"},
        )
        assert response.status_code == 200
        receipts = _receipts(logs)
        assert len(receipts) == 1
        assert f"token={TOKEN[:8]}*" in receipts[0]
        assert "ip=" in receipts[0]
        assert "len=" in receipts[0]
        assert "type=application/json" in receipts[0]
        _assert_token_never_logged(logs, TOKEN)

    def test_unknown_token_404_still_logs_receipt(self, rig):
        client, logs = rig
        response = client.post(f"/hooks/{UNKNOWN_TOKEN}", json={})
        assert response.status_code == 404
        receipts = _receipts(logs)
        assert len(receipts) == 1
        assert f"token={UNKNOWN_TOKEN[:8]}*" in receipts[0]
        _assert_token_never_logged(logs, UNKNOWN_TOKEN)

    def test_oversized_413_still_logs_receipt(self, rig):
        client, logs = rig
        response = client.post(
            f"/hooks/{TOKEN}", content=b"x" * 1_048_577,
        )
        assert response.status_code == 413
        assert len(_receipts(logs)) == 1
        _assert_token_never_logged(logs, TOKEN)

    def test_token_rate_limited_429_still_logs_receipt(self, rig):
        client, logs = rig
        import time as time_module

        now = time_module.time()
        triggers_module._hook_rate_buckets[TOKEN] = [now] * 60
        response = client.post(f"/hooks/{TOKEN}", json={})
        assert response.status_code == 429
        assert len(_receipts(logs)) == 1
        _assert_token_never_logged(logs, TOKEN)

    def test_ip_rate_limited_429_still_logs_receipt(self, rig):
        client, logs = rig
        import time as time_module

        now = time_module.time()
        triggers_module._hook_ip_buckets["testclient"] = [now] * 20
        response = client.post(f"/hooks/{TOKEN}", json={})
        assert response.status_code == 429
        assert len(_receipts(logs)) == 1
        _assert_token_never_logged(logs, TOKEN)

    def test_every_attempt_gets_its_own_receipt(self, rig):
        client, logs = rig
        client.post(f"/hooks/{TOKEN}", json={})
        client.post(f"/hooks/{UNKNOWN_TOKEN}", json={})
        client.post(f"/hooks/{TOKEN}", json={})
        assert len(_receipts(logs)) == 3


class TestRateLimiterClockWedge:
    """A backwards clock step must not wedge the limiter buckets.

    The prune ``now - t < window`` keeps FUTURE-stamped entries forever
    (``now - t`` is negative), so timestamps written under a fast clock that
    NTP later corrects backwards occupy the bucket until process death —
    every delivery is then silently rejected while the sender sees 429s.
    Observed in production 2026-08-18: a host power-cut's clock correction
    silenced a webhook token for the process's entire remaining life, cured
    only by restart. Future debris is clock damage, not load: discard it.
    """

    def test_future_stamped_token_bucket_recovers(self, rig):
        client, _ = rig
        now = time.time()
        triggers_module._hook_rate_buckets[TOKEN] = [now + 3_600.0] * 60
        response = client.post(f"/hooks/{TOKEN}", json={})
        assert response.status_code == 200
        # The wedge debris is gone; only this request's stamp remains.
        bucket = triggers_module._hook_rate_buckets[TOKEN]
        assert all(t <= time.time() for t in bucket)
        assert len(bucket) == 1

    def test_future_stamped_ip_bucket_recovers(self, rig):
        client, _ = rig
        now = time.time()
        triggers_module._hook_ip_buckets["testclient"] = [now + 3_600.0] * 20
        response = client.post(f"/hooks/{TOKEN}", json={})
        assert response.status_code == 200

    def test_genuine_token_flood_still_limited_and_loud(self, rig):
        client, logs = rig
        now = time.time()
        triggers_module._hook_rate_buckets[TOKEN] = [now - 1.0] * 60
        response = client.post(f"/hooks/{TOKEN}", json={})
        assert response.status_code == 429
        limited = [
            line for line in logs if "token rate limited" in line
        ]
        assert len(limited) == 1
        assert f"{TOKEN[:8]}*" in limited[0]
        _assert_token_never_logged(logs, TOKEN)


class _CaptureHandler(logging.Handler):
    def __init__(self, sink: list[str]):
        super().__init__()
        self.sink = sink

    def emit(self, record: logging.LogRecord) -> None:
        self.sink.append(self.format(record))


@pytest.fixture()
def access_capture():
    """Capture formatted uvicorn.access records with redaction installed."""
    access_logger = logging.getLogger("uvicorn.access")
    redaction_filter = triggers_module.HookTokenRedactionFilter()
    access_logger.addFilter(redaction_filter)
    records: list[str] = []
    handler = _CaptureHandler(records)
    previous_level = access_logger.level
    access_logger.addHandler(handler)
    access_logger.setLevel(logging.INFO)
    yield records
    access_logger.removeHandler(handler)
    access_logger.setLevel(previous_level)
    access_logger.removeFilter(redaction_filter)


class TestAccessLogTokenRedaction:
    def test_request_line_args_are_redacted(self, access_capture):
        # Exact record shape uvicorn's protocol emits for an access line.
        logging.getLogger("uvicorn.access").info(
            '%s - "%s %s HTTP/%s" %d',
            "127.0.0.1:12345",
            "POST",
            f"/hooks/{TOKEN}",
            "1.1",
            404,
        )
        assert len(access_capture) == 1
        assert TOKEN not in access_capture[0]
        assert f"/hooks/{TOKEN[:8]}*" in access_capture[0]

    def test_non_hook_paths_pass_through_unchanged(self, access_capture):
        logging.getLogger("uvicorn.access").info(
            '%s - "%s %s HTTP/%s" %d',
            "127.0.0.1:12345",
            "GET",
            "/agents/barsik/status",
            "1.1",
            200,
        )
        assert access_capture == [
            '127.0.0.1:12345 - "GET /agents/barsik/status HTTP/1.1" 200'
        ]

    def test_uvicorn_log_config_wires_redaction_into_access_handler(self):
        import uvicorn.config

        config = triggers_module.uvicorn_log_config()
        assert config["filters"]["hook_token_redaction"]["()"] == (
            "pinky_daemon.routes.triggers.HookTokenRedactionFilter"
        )
        assert "hook_token_redaction" in config["handlers"]["access"]["filters"]
        # The patched dict must be a copy, not a mutation of uvicorn's default.
        assert "hook_token_redaction" not in (
            uvicorn.config.LOGGING_CONFIG.get("filters") or {}
        )

    def test_live_uvicorn_access_log_never_emits_full_token(
        self, monkeypatch, capfd
    ):
        """End-to-end with the production log config: the emitted access
        record must carry the token prefix, never the full credential."""
        import threading
        import urllib.request

        import uvicorn

        app, _ = _wire(monkeypatch)
        config = uvicorn.Config(
            app,
            host="127.0.0.1",
            port=0,
            access_log=True,
            log_config=triggers_module.uvicorn_log_config(),
        )
        server = uvicorn.Server(config)
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        try:
            deadline = time.time() + 15
            while not server.started and time.time() < deadline:
                time.sleep(0.05)
            assert server.started, "uvicorn did not start in time"
            port = server.servers[0].sockets[0].getsockname()[1]
            request = urllib.request.Request(
                f"http://127.0.0.1:{port}/hooks/{TOKEN}",
                data=b"{}",
                headers={"content-type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=10) as response:
                assert response.status == 200
            deadline = time.time() + 10
            emitted = ""
            while "/hooks/" not in emitted and time.time() < deadline:
                time.sleep(0.05)
                captured = capfd.readouterr()
                emitted += captured.out + captured.err
        finally:
            server.should_exit = True
            thread.join(timeout=15)
        assert "/hooks/" in emitted, "no access-log line observed"
        assert TOKEN not in emitted
        assert f"/hooks/{TOKEN[:8]}*" in emitted


def _balanced_call(text: str, open_paren_index: int) -> str:
    depth = 0
    for end in range(open_paren_index, len(text)):
        char = text[end]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return text[open_paren_index : end + 1]
    return text[open_paren_index:]


def test_every_daemon_uvicorn_site_passes_the_redacting_log_config():
    """Any uvicorn Config/run in the daemon process re-runs dictConfig
    process-wide; an unpatched site would strip the token redaction off the
    access handler (the shared-MCP supervisor restarts uvicorn at arbitrary
    times). Every in-process site must pass log_config explicitly.

    Scope is the pinky_daemon package only: pinky_hub runs as its own
    process, so its logging config cannot strip this daemon's filter.
    """
    from pathlib import Path

    package_root = Path(triggers_module.__file__).resolve().parents[1]
    violations = []
    for source_path in sorted(package_root.rglob("*.py")):
        text = source_path.read_text(encoding="utf-8")
        for pattern in ("uvicorn.run(", "uvicorn.Config("):
            start = 0
            while True:
                index = text.find(pattern, start)
                if index == -1:
                    break
                call_text = _balanced_call(text, index + len(pattern) - 1)
                if "log_config" not in call_text:
                    line_number = text.count("\n", 0, index) + 1
                    violations.append(
                        f"{source_path.relative_to(package_root)}:{line_number}"
                    )
                start = index + len(pattern)
    assert violations == [], (
        "uvicorn started without the redacting log_config at: "
        + ", ".join(violations)
    )
