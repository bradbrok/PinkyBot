"""Tests for dream transcript loading and watermarking."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

from pinky_daemon.api import create_api
from pinky_daemon.dream_runner import DreamRunner


class _FakeAgentConfig:
    """Minimal stand-in exposing the attrs _build_kg_llm_caller reads."""

    def __init__(self, provider_key: str = "") -> None:
        self.provider_key = provider_key


class TestDreamRunnerConcurrency:
    def test_point_read_hammer_uses_thread_local_connections(self, tmp_path):
        runner = DreamRunner(db_path=str(tmp_path / "dream.db"))
        worker_count = 12
        rounds = 25
        point_reads_per_round = 8
        start = threading.Barrier(worker_count)
        runner._save_state("shared-agent", "Shared dream summary", 123.0)

        def hammer(worker_index):
            point_reads = 0
            snapshots = []
            try:
                start.wait(timeout=10)
                connection = runner._db
                connection_id = id(connection)
                assert connection.execute(
                    "PRAGMA journal_mode"
                ).fetchone()[0].lower() == "truncate"
                assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 0
                agent_name = f"worker-{worker_index}"
                for round_index in range(rounds):
                    marker = f"{worker_index}-{round_index}"
                    runner._save_state(
                        agent_name,
                        f"Hammer dream summary {marker}",
                        float(round_index),
                    )
                    own = runner.get_state(agent_name)
                    assert own["last_summary"] == f"Hammer dream summary {marker}"
                    for _ in range(point_reads_per_round):
                        point_read = runner.get_state("shared-agent")
                        assert point_read["last_summary"] == "Shared dream summary"
                        point_reads += 1
                    snapshots.append(own)
                return connection_id, snapshots, point_reads, None
            except Exception as exc:
                return None, snapshots, point_reads, exc
            finally:
                runner.close()

        try:
            with ThreadPoolExecutor(max_workers=worker_count) as executor:
                results = list(executor.map(hammer, range(worker_count)))

            errors = [error for _, _, _, error in results if error is not None]
            database_errors = [
                error
                for error in errors
                if isinstance(error, sqlite3.DatabaseError)
                or "malformed" in str(error).lower()
            ]
            assert database_errors == []
            assert errors == []

            connection_ids = [connection_id for connection_id, _, _, _ in results]
            assert len(set(connection_ids)) == worker_count
            assert sum(point_reads for _, _, point_reads, _ in results) == (
                worker_count * rounds * point_reads_per_round
            )
            assert all(len(snapshots) == rounds for _, snapshots, _, _ in results)
            assert len(runner.list_states()) == worker_count + 1
        finally:
            runner.close()


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


@pytest.mark.real_auth
@pytest.mark.parametrize(
    ("isolated", "force_global_fallback"),
    [(False, True), (True, False)],
    ids=("global-fallback", "isolated-agent-key"),
)
def test_skill_proposal_uses_signed_headers_against_auth_gate(
    tmp_path, monkeypatch, isolated, force_global_fallback
):
    """Dream-created skills authenticate; the same bare API POST is denied.

    Non-isolated agents retain the shared-secret fallback. Isolated agents use
    their per-agent key because the auth gate deliberately rejects that shared
    secret for isolated identities.
    """
    monkeypatch.setenv("PINKY_SESSION_SECRET", "dream-test-session-secret")
    app = create_api(
        max_sessions=10,
        default_working_dir=str(tmp_path),
        db_path=str(tmp_path / "pinky.db"),
    )
    app.state.agents.register(
        "test-agent",
        model="opus",
        working_dir=str(tmp_path / "test-agent"),
        isolated=isolated,
    )

    # Keep the route's persistent skill write inside this test's temp tree.
    from pinky_daemon.routes import skills as skills_routes

    monkeypatch.setattr(skills_routes, "_pinky_root", tmp_path)

    client = TestClient(app)
    bare = client.post(
        "/skills/from-md",
        json={
            "content": "---\nname: bare-skill\ndescription: bare\n---\n\n# Bare\n",
            "agent_name": "test-agent",
        },
    )
    assert bare.status_code == 401

    class _UrlResponse:
        def __init__(self, content: bytes) -> None:
            self._content = content

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self) -> bytes:
            return self._content

    def _urlopen(req, timeout=None):
        response = client.request(
            req.get_method(),
            urlsplit(req.full_url).path,
            headers=dict(req.header_items()),
            content=req.data,
        )
        assert response.status_code == 200, response.text
        return _UrlResponse(response.content)

    monkeypatch.setattr(urllib.request, "urlopen", _urlopen)
    runner = app.state.dream_runner
    if force_global_fallback:
        runner._signing_key_provider = lambda _agent_name: None
    dream_output = """<proposed_skills>
    [{
      "skill_name": "test-dream-skill",
      "description": "Use this skill for a repeated test workflow.",
      "task_summary": "Run the workflow and verify its output.",
      "source_pattern": "Repeated test workflow"
    }]
    </proposed_skills>"""

    assert runner._extract_proposed_skills(dream_output, "test-agent") == 1

    assert (tmp_path / "skills" / "test-dream-skill" / "SKILL.md").exists()


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

    def test_calls_api_with_default_model_when_override_absent(self, monkeypatch):
        # End-to-end: absent model settings fall back to the cheap bare alias.
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("KG_EXTRACTION_MODEL", raising=False)
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
            assert captured["body"]["model"] == "claude-haiku-4-5"
            assert captured["headers"]["x-api-key"] == "sk-from-settings"
        finally:
            os.unlink(path)

    @pytest.mark.parametrize(
        ("settings_model", "env_model", "expected"),
        [
            ("claude-settings-model", "claude-env-model", "claude-settings-model"),
            ("", "claude-env-model", "claude-env-model"),
            ("  ", "  ", "claude-haiku-4-5"),
        ],
        ids=("settings-override", "environment-fallback", "blank-default"),
    )
    def test_resolves_configurable_model(
        self, monkeypatch, settings_model, env_model, expected
    ):
        monkeypatch.setenv("KG_EXTRACTION_MODEL", env_model)
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
            return _FakeResp()

        def _setting_provider(key):
            return {
                "ANTHROPIC_API_KEY": "sk-from-settings",
                "KG_EXTRACTION_MODEL": settings_model,
            }.get(key, "")

        monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)
        runner, path = _new_runner(setting_provider=_setting_provider)
        try:
            caller = runner._build_kg_llm_caller(_FakeAgentConfig())
            assert caller is not None
            assert caller("extract triples") == "ok"
            assert captured["body"]["model"] == expected
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
