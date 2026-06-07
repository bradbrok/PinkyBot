"""Tests for dream transcript loading and watermarking."""

from __future__ import annotations

import json
import os
import tempfile
import urllib.request
from datetime import datetime

from pinky_daemon.dream_runner import DreamRunner


class _FakeAgentConfig:
    """Minimal stand-in exposing the attrs _build_kg_llm_caller reads."""

    def __init__(self, provider_key: str = "") -> None:
        self.provider_key = provider_key


def _new_runner(**kwargs) -> tuple[DreamRunner, str]:
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    return DreamRunner(db_path=path, **kwargs), path


class TestDreamRunner:
    def test_fetch_unprocessed_history_filters_and_orders_messages(self):
        messages = [
            {"timestamp": 300.0, "role": "assistant", "content": "third"},
            {"timestamp": 100.0, "role": "user", "content": "first"},
            {"timestamp": 200.0, "role": "user", "content": "second"},
        ]

        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            runner = DreamRunner(
                db_path=path,
                history_provider=lambda agent_name, after_ts, limit, role: messages,
            )

            lines, watermark = runner._fetch_unprocessed_history("pinky", after_ts=150.0)

            expected = [
                f"[{datetime.fromtimestamp(200.0).strftime('%Y-%m-%d %H:%M')}] [user] second",
                f"[{datetime.fromtimestamp(300.0).strftime('%Y-%m-%d %H:%M')}] [assistant] third",
            ]
            assert lines == [
                expected[0],
                expected[1],
            ]
            assert watermark == 300.0
        finally:
            os.unlink(path)


class TestBuildKGLLMCaller:
    """Regression tests for the KG-extraction LLM caller key resolution.

    The daemon process env does NOT carry ANTHROPIC_API_KEY (it lives in
    system_settings), so reading os.environ only returned None and silently
    skipped KG extraction on every dream run. The caller must resolve the key
    through provider_key -> setting_provider -> env.
    """

    def test_returns_none_when_no_key_anywhere(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        runner, path = _new_runner()  # no setting_provider
        try:
            assert runner._build_kg_llm_caller(_FakeAgentConfig()) is None
        finally:
            os.unlink(path)

    def test_resolves_key_from_setting_provider_when_env_missing(self, monkeypatch):
        # The real-world failure: env lacks the key but system_settings has it.
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        runner, path = _new_runner(
            setting_provider=lambda key: "sk-from-settings" if key == "ANTHROPIC_API_KEY" else "",
        )
        try:
            assert runner._build_kg_llm_caller(_FakeAgentConfig()) is not None
        finally:
            os.unlink(path)

    def test_calls_api_with_alias_model_and_resolved_key(self, monkeypatch):
        # End-to-end: proves both the key-resolution fix and the corrected
        # model alias (a dated Sonnet 4.6 snapshot would 404).
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        captured: dict = {}

        class _FakeResp:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return json.dumps({"content": [{"type": "text", "text": "ok"}]}).encode()

        def _fake_urlopen(req, timeout=None):
            captured["body"] = json.loads(req.data.decode())
            captured["headers"] = {k.lower(): v for k, v in req.headers.items()}
            return _FakeResp()

        monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)
        runner, path = _new_runner(
            setting_provider=lambda key: "sk-from-settings" if key == "ANTHROPIC_API_KEY" else "",
        )
        try:
            caller = runner._build_kg_llm_caller(_FakeAgentConfig())
            assert caller is not None
            assert caller("extract triples") == "ok"
            assert captured["body"]["model"] == "claude-sonnet-4-6"
            assert captured["headers"]["x-api-key"] == "sk-from-settings"
        finally:
            os.unlink(path)

    def test_provider_key_takes_precedence(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-from-env")
        captured: dict = {}

        class _FakeResp:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return json.dumps({"content": [{"type": "text", "text": "ok"}]}).encode()

        def _fake_urlopen(req, timeout=None):
            captured["headers"] = {k.lower(): v for k, v in req.headers.items()}
            return _FakeResp()

        monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)
        runner, path = _new_runner(setting_provider=lambda key: "sk-from-settings")
        try:
            caller = runner._build_kg_llm_caller(_FakeAgentConfig(provider_key="sk-from-agent"))
            assert caller is not None
            caller("extract triples")
            assert captured["headers"]["x-api-key"] == "sk-from-agent"
        finally:
            os.unlink(path)
