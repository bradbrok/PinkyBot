"""Tests for dream transcript loading and watermarking."""

from __future__ import annotations

import json
import os
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime

import pytest

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


class _FakeResp:
    def __init__(self, text="ok"):
        self._text = text

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return json.dumps({"content": [{"type": "text", "text": self._text}]}).encode()


class TestKGCallerRetry:
    """The KG caller retries transient failures (read timeouts, 5xx/429) with
    backoff and fails fast on client errors. Regression for #172: once #686
    raised max_tokens to 8192, the heaviest reflections blew past the old fixed
    30s read timeout and were lost with no retry (~34% of a rotation)."""

    def _caller(self, monkeypatch):
        # No real backoff sleeps — call_llm's `time` is the global module.
        monkeypatch.setattr(time, "sleep", lambda *_a: None)
        runner, path = _new_runner(
            setting_provider=lambda key: "sk" if key == "ANTHROPIC_API_KEY" else "",
        )
        return runner._build_kg_llm_caller(_FakeAgentConfig()), path

    def test_retries_timeout_then_succeeds(self, monkeypatch):
        calls = {"n": 0}

        def _urlopen(req, timeout=None):
            calls["n"] += 1
            if calls["n"] < 3:
                raise TimeoutError("The read operation timed out")
            return _FakeResp("recovered")

        monkeypatch.setattr(urllib.request, "urlopen", _urlopen)
        caller, path = self._caller(monkeypatch)
        try:
            assert caller("prompt") == "recovered"
            assert calls["n"] == 3  # 2 timeouts + 1 success
        finally:
            os.unlink(path)

    def test_raises_after_exhausting_retries(self, monkeypatch):
        calls = {"n": 0}

        def _urlopen(req, timeout=None):
            calls["n"] += 1
            raise TimeoutError("The read operation timed out")

        monkeypatch.setattr(urllib.request, "urlopen", _urlopen)
        caller, path = self._caller(monkeypatch)
        try:
            with pytest.raises(TimeoutError):
                caller("prompt")
            assert calls["n"] == 3  # bounded: 1 initial + 2 retries
        finally:
            os.unlink(path)

    def test_uses_widened_read_timeout(self, monkeypatch):
        # Regression: the old fixed 30s timeout is what #172 blew past.
        seen = {}

        def _urlopen(req, timeout=None):
            seen["timeout"] = timeout
            return _FakeResp("ok")

        monkeypatch.setattr(urllib.request, "urlopen", _urlopen)
        caller, path = self._caller(monkeypatch)
        try:
            caller("prompt")
            assert seen["timeout"] >= 90
        finally:
            os.unlink(path)

    def test_no_retry_on_client_error(self, monkeypatch):
        calls = {"n": 0}

        def _urlopen(req, timeout=None):
            calls["n"] += 1
            raise urllib.error.HTTPError("http://x", 400, "Bad Request", {}, None)

        monkeypatch.setattr(urllib.request, "urlopen", _urlopen)
        caller, path = self._caller(monkeypatch)
        try:
            with pytest.raises(urllib.error.HTTPError):
                caller("prompt")
            assert calls["n"] == 1  # fail fast on 4xx — no retry
        finally:
            os.unlink(path)

    def test_retries_on_overloaded_529(self, monkeypatch):
        calls = {"n": 0}

        def _urlopen(req, timeout=None):
            calls["n"] += 1
            if calls["n"] < 2:
                raise urllib.error.HTTPError("http://x", 529, "Overloaded", {}, None)
            return _FakeResp("ok")

        monkeypatch.setattr(urllib.request, "urlopen", _urlopen)
        caller, path = self._caller(monkeypatch)
        try:
            assert caller("prompt") == "ok"
            assert calls["n"] == 2  # 529 is transient — retried
        finally:
            os.unlink(path)


class _StubSDKRunner:
    """Stands in for SDKRunner inside run_dream; returns a canned RunResult."""

    result = None

    def __init__(self, config, agent_name=""):
        self.config = config
        self.agent_name = agent_name

    async def run(self, prompt):
        return type(self).result


class _DreamAgentConfig:
    def __init__(self, working_dir: str) -> None:
        self.working_dir = working_dir
        self.model = "sonnet"
        self.dream_model = ""
        self.provider_key = ""


class TestRunDreamWatermark:
    """run_dream must never lose the last_message_ts watermark: an idle night
    must not reset it to 0 (full-history reprocess), and a failed run must not
    advance it (that night's history silently skipped forever)."""

    def _runner(self, tmp_path, messages):
        return DreamRunner(
            db_path=str(tmp_path / "dream.db"),
            history_provider=lambda agent, after_ts, limit, role: messages,
        )

    @pytest.mark.asyncio
    async def test_idle_night_preserves_watermark(self, tmp_path, monkeypatch):
        monkeypatch.delenv("PINKY_DREAM_TRANSPORT", raising=False)
        runner = self._runner(tmp_path, [])
        runner._save_state("pinky", "seed", last_message_ts=123.0)

        summary = await runner.run_dream("pinky", _DreamAgentConfig(str(tmp_path)))

        assert "No new conversation history" in summary
        assert runner._get_last_message_ts("pinky") == 123.0

    @pytest.mark.asyncio
    async def test_failed_run_does_not_advance_watermark(self, tmp_path, monkeypatch):
        from pinky_daemon.claude_runner import RunResult

        monkeypatch.delenv("PINKY_DREAM_TRANSPORT", raising=False)
        monkeypatch.delenv("PINKY_KG_PROACTIVE", raising=False)
        monkeypatch.setattr("pinky_daemon.dream_runner.SDKRunner", _StubSDKRunner)
        _StubSDKRunner.result = RunResult(output="", exit_code=1, error="boom")

        messages = [{"timestamp": 200.0, "role": "user", "content": "hello"}]
        runner = self._runner(tmp_path, messages)
        runner._save_state("pinky", "seed", last_message_ts=100.0)

        summary = await runner.run_dream("pinky", _DreamAgentConfig(str(tmp_path)))

        assert "Dream run failed" in summary
        # Failed run: the night's history stays unprocessed for the next cycle
        assert runner._get_last_message_ts("pinky") == 100.0

    @pytest.mark.asyncio
    async def test_successful_run_advances_watermark(self, tmp_path, monkeypatch):
        from pinky_daemon.claude_runner import RunResult

        monkeypatch.delenv("PINKY_DREAM_TRANSPORT", raising=False)
        monkeypatch.delenv("PINKY_KG_PROACTIVE", raising=False)
        monkeypatch.setattr("pinky_daemon.dream_runner.SDKRunner", _StubSDKRunner)
        _StubSDKRunner.result = RunResult(output="Consolidated.", exit_code=0)

        messages = [{"timestamp": 200.0, "role": "user", "content": "hello"}]
        runner = self._runner(tmp_path, messages)
        runner._save_state("pinky", "seed", last_message_ts=100.0)

        summary = await runner.run_dream("pinky", _DreamAgentConfig(str(tmp_path)))

        assert summary == "Consolidated."
        assert runner._get_last_message_ts("pinky") == 200.0
