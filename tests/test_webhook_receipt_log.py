"""Tests for the /hooks/{token} unconditional receipt log.

Every delivery attempt must leave a receipt line, including the early-drop
paths (unknown-token 404, IP/per-token 429, oversized 413) that previously
exited with no log at all — making a dropped delivery indistinguishable from
a sender that never sent. The full token is a credential and must never be
logged; only its prefix may appear.
"""

from __future__ import annotations

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


@pytest.fixture()
def rig(monkeypatch):
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
