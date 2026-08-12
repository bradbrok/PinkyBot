"""Tests for dream transcript loading and watermarking."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import tempfile
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

import pinky_daemon.dream_runner as dream_runner_module
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
                ).fetchone()[0].lower() == "wal"
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


class TestDreamReflectionLoudness:
    async def _record_night(
        self,
        runner: DreamRunner,
        night_key: str,
        embedded_reflections: int,
        sequence: int,
    ) -> None:
        await runner._record_reflection_outcome(
            "pinky",
            embedded_reflections,
            night_key=night_key,
            summary=f"night {night_key}",
            last_message_ts=float(sequence),
        )

    @pytest.mark.asyncio
    async def test_zero_streak_warns_persists_notifies_and_resets(
        self, tmp_path, monkeypatch, capsys
    ):
        from pinky_daemon.claude_runner import RunResult

        monkeypatch.delenv("PINKY_DREAM_TRANSPORT", raising=False)
        monkeypatch.delenv("PINKY_KG_PROACTIVE", raising=False)
        monkeypatch.setattr("pinky_daemon.dream_runner.SDKRunner", _StubSDKRunner)
        _StubSDKRunner.result = RunResult(output="Consolidated.", exit_code=0)
        night_keys = iter(
            ("2026-08-09", "2026-08-10", "2026-08-11", "2026-08-12")
        )
        monkeypatch.setattr(
            DreamRunner,
            "_night_key",
            staticmethod(lambda agent_config, dream_start: next(night_keys)),
        )

        db_path = str(tmp_path / "dream.db")

        def messages(agent, after_ts, limit, role):
            return [
                {
                    "timestamp": after_ts + 1.0,
                    "role": "user",
                    "content": "new nightly context",
                }
            ]

        notices: list[tuple[str, str]] = []

        async def owner_notify(agent_name: str, message: str) -> bool:
            notices.append((agent_name, message))
            return True

        runner = DreamRunner(
            db_path=db_path,
            history_provider=messages,
            owner_notify_callback=owner_notify,
        )
        monkeypatch.setattr(runner, "_build_memory_links", lambda *args, **kwargs: 0)
        await runner.run_dream("pinky", _DreamAgentConfig(str(tmp_path)))
        assert runner.get_state("pinky")["zero_reflection_streak"] == 1
        runner.close()

        runner = DreamRunner(
            db_path=db_path,
            history_provider=messages,
            owner_notify_callback=owner_notify,
        )
        monkeypatch.setattr(runner, "_build_memory_links", lambda *args, **kwargs: 0)
        await runner.run_dream("pinky", _DreamAgentConfig(str(tmp_path)))
        await runner.run_dream("pinky", _DreamAgentConfig(str(tmp_path)))

        assert runner.get_state("pinky")["zero_reflection_streak"] == 3
        assert len(notices) == 1
        assert notices[0][0] == "pinky"
        assert "3 consecutive nights" in notices[0][1]
        warnings = capsys.readouterr().err
        assert warnings.count("WARNING 'pinky' completed with zero embedded reflections") == 3

        monkeypatch.setattr(runner, "_build_memory_links", lambda *args, **kwargs: 2)
        await runner.run_dream("pinky", _DreamAgentConfig(str(tmp_path)))

        assert runner.get_state("pinky")["zero_reflection_streak"] == 0
        assert len(notices) == 1
        assert "zero embedded reflections" not in capsys.readouterr().err

    def test_crash_between_state_and_outcome_writes_rolls_back_both(
        self, tmp_path, monkeypatch
    ):
        db_path = str(tmp_path / "dream.db")
        runner = DreamRunner(db_path=db_path)
        runner._save_state("pinky", "seed", last_message_ts=100.0)
        runner._db.execute(
            "UPDATE dream_state SET zero_reflection_streak=2 WHERE agent_name=?",
            ("pinky",),
        )
        runner._db.commit()

        def simulated_crash(*args, **kwargs):
            raise RuntimeError("simulated crash after state write")

        monkeypatch.setattr(runner, "_apply_reflection_outcome", simulated_crash)
        with pytest.raises(RuntimeError, match="simulated crash"):
            runner._commit_reflection_outcome(
                "pinky",
                "2026-08-12",
                0,
                summary="completed dream",
                last_message_ts=200.0,
            )
        runner.close()

        reopened = DreamRunner(db_path=db_path)
        state = reopened.get_state("pinky")
        assert state["last_summary"] == "seed"
        assert state["zero_reflection_streak"] == 2
        assert reopened._get_last_message_ts("pinky") == 100.0
        assert reopened._db.execute(
            "SELECT COUNT(*) FROM dream_reflection_outcomes"
        ).fetchone()[0] == 0

        created, streak, delivered = reopened._commit_reflection_outcome(
            "pinky",
            "2026-08-12",
            0,
            summary="completed dream",
            last_message_ts=200.0,
        )
        assert (created, streak, delivered) == (True, 3, False)
        assert reopened.get_state("pinky")["last_summary"] == "completed dream"
        assert reopened._get_last_message_ts("pinky") == 200.0
        assert reopened._db.execute(
            "SELECT COUNT(*) FROM dream_reflection_outcomes"
        ).fetchone()[0] == 1
        reopened.close()

    @pytest.mark.asyncio
    async def test_same_night_zero_updates_watermark_without_double_counting(
        self, tmp_path
    ):
        db_path = str(tmp_path / "dream.db")
        runner = DreamRunner(db_path=db_path)

        await self._record_night(runner, "2026-08-12", 0, 1)
        runner.close()

        runner = DreamRunner(db_path=db_path)
        await self._record_night(runner, "2026-08-12", 0, 2)

        state = runner.get_state("pinky")
        assert state["zero_reflection_streak"] == 1
        assert state["last_summary"] == "night 2026-08-12"
        assert runner._get_last_message_ts("pinky") == 2.0
        assert runner._db.execute(
            "SELECT COUNT(*) FROM dream_reflection_outcomes"
        ).fetchone()[0] == 1

    @pytest.mark.asyncio
    async def test_cross_db_crash_recovers_reflection_without_rerunning(
        self, tmp_path, monkeypatch
    ):
        from pinky_daemon.claude_runner import RunResult
        from pinky_memory.store import ReflectionStore
        from pinky_memory.types import Reflection

        monkeypatch.delenv("PINKY_DREAM_TRANSPORT", raising=False)
        monkeypatch.delenv("PINKY_KG_PROACTIVE", raising=False)
        memory_dir = tmp_path / "data"
        memory_dir.mkdir()
        memory_db = memory_dir / "memory.db"
        sdk_calls = {"n": 0}

        class ReflectingSDKRunner:
            def __init__(self, config, agent_name=""):
                self.config = config
                self.agent_name = agent_name

            async def run(self, prompt):
                sdk_calls["n"] += 1
                correlation_id = prompt.split("source_session_id='", 1)[1].split(
                    "'", 1
                )[0]
                store = ReflectionStore(db_path=str(memory_db))
                store.insert(
                    Reflection(
                        content="durable first-attempt reflection",
                        embedding=[1.0, 0.0],
                        source_session_id=correlation_id,
                    )
                )
                store.close()
                return RunResult(output="Consolidated.", exit_code=0)

        monkeypatch.setattr(
            "pinky_daemon.dream_runner.SDKRunner", ReflectingSDKRunner
        )
        monkeypatch.setattr(
            DreamRunner,
            "_night_key",
            staticmethod(lambda agent_config, dream_start: "2026-08-12"),
        )
        messages = [
            {"timestamp": 200.0, "role": "user", "content": "new context"}
        ]
        db_path = str(tmp_path / "dream.db")
        runner = DreamRunner(
            db_path=db_path,
            history_provider=lambda agent, after_ts, limit, role: messages,
        )
        runner._save_state("pinky", "seed", last_message_ts=100.0)

        async def crash_before_finalize(*args, **kwargs):
            raise RuntimeError("simulated crash after durable reflect")

        monkeypatch.setattr(
            runner, "_record_reflection_outcome", crash_before_finalize
        )
        with pytest.raises(RuntimeError, match="after durable reflect"):
            await runner.run_dream("pinky", _DreamAgentConfig(str(tmp_path)))

        assert runner._get_last_message_ts("pinky") == 100.0
        assert runner._db.execute(
            """SELECT status FROM dream_reflection_attempts
               WHERE agent_name='pinky'"""
        ).fetchone()[0] == "pending"
        # A simulated process death must make the old owner's lease stale;
        # live/unexpired owners are deliberately not recoverable.
        runner._db.execute(
            """UPDATE dream_reflection_attempts
               SET lease_expires_at=0 WHERE agent_name='pinky'"""
        )
        runner._db.commit()
        runner.close()

        reopened = DreamRunner(
            db_path=db_path,
            history_provider=lambda agent, after_ts, limit, role: messages,
        )
        summary = await reopened.run_dream(
            "pinky", _DreamAgentConfig(str(tmp_path))
        )

        assert summary == "Consolidated."
        assert sdk_calls["n"] == 1
        with sqlite3.connect(memory_db) as memory_connection:
            assert memory_connection.execute(
                "SELECT COUNT(*) FROM reflections"
            ).fetchone()[0] == 1
        assert reopened._get_last_message_ts("pinky") == 200.0
        assert reopened.get_state("pinky")["zero_reflection_streak"] == 0
        assert reopened._db.execute(
            """SELECT embedded_reflections FROM dream_reflection_outcomes
               WHERE agent_name='pinky' AND night_key='2026-08-12'"""
        ).fetchone()[0] == 1
        assert reopened._db.execute(
            """SELECT status, embedded_reflections
               FROM dream_reflection_attempts WHERE agent_name='pinky'"""
        ).fetchone() == ("completed", 1)
        reopened.close()

    @pytest.mark.asyncio
    async def test_same_night_new_history_updates_outcome_and_resets_latch(
        self, tmp_path, monkeypatch
    ):
        from pinky_daemon.claude_runner import RunResult

        monkeypatch.delenv("PINKY_DREAM_TRANSPORT", raising=False)
        monkeypatch.delenv("PINKY_KG_PROACTIVE", raising=False)
        monkeypatch.setattr("pinky_daemon.dream_runner.SDKRunner", _StubSDKRunner)
        _StubSDKRunner.result = RunResult(output="Consolidated.", exit_code=0)
        monkeypatch.setattr(
            DreamRunner,
            "_night_key",
            staticmethod(lambda agent_config, dream_start: "2026-08-12"),
        )

        messages = [
            {"timestamp": 100.0, "role": "user", "content": "first batch"}
        ]
        notices: list[str] = []

        async def owner_notify(agent_name: str, message: str) -> bool:
            notices.append(message)
            return True

        runner = DreamRunner(
            db_path=str(tmp_path / "dream.db"),
            history_provider=lambda agent, after_ts, limit, role: list(messages),
            owner_notify_callback=owner_notify,
        )
        await self._record_night(runner, "2026-08-10", 0, 1)
        await self._record_night(runner, "2026-08-11", 0, 2)
        link_counts = iter((0, 2))
        monkeypatch.setattr(
            runner,
            "_build_memory_links",
            lambda *args, **kwargs: next(link_counts),
        )

        await runner.run_dream("pinky", _DreamAgentConfig(str(tmp_path)))
        latched = runner.get_state("pinky")
        assert latched["zero_reflection_streak"] == 3
        assert latched["zero_reflection_alert_delivered_at"] is not None

        messages.append(
            {"timestamp": 200.0, "role": "user", "content": "manual new batch"}
        )
        await runner.run_dream("pinky", _DreamAgentConfig(str(tmp_path)))

        state = runner.get_state("pinky")
        assert runner._get_last_message_ts("pinky") == 200.0
        assert state["zero_reflection_streak"] == 0
        assert state["zero_reflection_alert_attempted_at"] is None
        assert state["zero_reflection_alert_delivered_at"] is None
        assert len(notices) == 1
        assert runner._db.execute(
            """SELECT embedded_reflections FROM dream_reflection_outcomes
               WHERE agent_name='pinky' AND night_key='2026-08-12'"""
        ).fetchone()[0] == 2
        assert runner._db.execute(
            """SELECT status, embedded_reflections
               FROM dream_reflection_attempts
               WHERE agent_name='pinky' AND night_key='2026-08-12'
               ORDER BY attempt_n"""
        ).fetchall() == [("completed", 0), ("completed", 2)]

    @pytest.mark.asyncio
    async def test_true_same_night_duplicate_has_no_runner_or_link_side_effects(
        self, tmp_path, monkeypatch
    ):
        from pinky_daemon.claude_runner import RunResult

        monkeypatch.delenv("PINKY_DREAM_TRANSPORT", raising=False)
        monkeypatch.delenv("PINKY_KG_PROACTIVE", raising=False)
        monkeypatch.setattr("pinky_daemon.dream_runner.SDKRunner", _StubSDKRunner)
        _StubSDKRunner.result = RunResult(output="Consolidated.", exit_code=0)
        monkeypatch.setattr(
            DreamRunner,
            "_night_key",
            staticmethod(lambda agent_config, dream_start: "2026-08-12"),
        )
        sdk_calls = {"n": 0}
        link_calls = {"n": 0}

        async def counted_run(self, prompt):
            sdk_calls["n"] += 1
            return type(self).result

        monkeypatch.setattr(_StubSDKRunner, "run", counted_run)
        messages = [
            {"timestamp": 100.0, "role": "user", "content": "only batch"}
        ]
        runner = DreamRunner(
            db_path=str(tmp_path / "dream.db"),
            history_provider=lambda agent, after_ts, limit, role: messages,
        )

        def counted_links(*args, **kwargs):
            link_calls["n"] += 1
            return 1

        monkeypatch.setattr(runner, "_build_memory_links", counted_links)

        await runner.run_dream("pinky", _DreamAgentConfig(str(tmp_path)))
        before = runner.get_state("pinky")
        duplicate_summary = await runner.run_dream(
            "pinky", _DreamAgentConfig(str(tmp_path))
        )

        assert duplicate_summary == "No new conversation history to process."
        assert sdk_calls["n"] == 1
        assert link_calls["n"] == 1
        assert runner.get_state("pinky") == before
        assert runner._db.execute(
            "SELECT COUNT(*) FROM dream_reflection_attempts"
        ).fetchone()[0] == 1
        assert runner._db.execute(
            "SELECT COUNT(*) FROM dream_reflection_outcomes"
        ).fetchone()[0] == 1

    @pytest.mark.asyncio
    async def test_overlapping_triggers_run_sdk_once(self, tmp_path, monkeypatch):
        from pinky_daemon.claude_runner import RunResult

        monkeypatch.delenv("PINKY_DREAM_TRANSPORT", raising=False)
        monkeypatch.delenv("PINKY_KG_PROACTIVE", raising=False)
        entered = asyncio.Event()
        release = asyncio.Event()
        sdk_calls = {"n": 0}

        class BlockingSDKRunner:
            def __init__(self, config, agent_name=""):
                self.config = config

            async def run(self, prompt):
                sdk_calls["n"] += 1
                entered.set()
                await release.wait()
                return RunResult(output="Consolidated.", exit_code=0)

        monkeypatch.setattr(
            "pinky_daemon.dream_runner.SDKRunner", BlockingSDKRunner
        )
        messages = [
            {"timestamp": 200.0, "role": "user", "content": "one interval"}
        ]
        runner = DreamRunner(
            db_path=str(tmp_path / "dream.db"),
            history_provider=lambda agent, after_ts, limit, role: messages,
        )

        trigger_a = asyncio.create_task(
            runner.run_dream("pinky", _DreamAgentConfig(str(tmp_path)))
        )
        await entered.wait()
        trigger_b = asyncio.create_task(
            runner.run_dream("pinky", _DreamAgentConfig(str(tmp_path)))
        )
        await asyncio.sleep(0)
        assert sdk_calls["n"] == 1

        release.set()
        assert await trigger_a == "Consolidated."
        assert await trigger_b == "No new conversation history to process."
        assert sdk_calls["n"] == 1

    @pytest.mark.asyncio
    async def test_pending_lease_takeover_requires_expiry(self, tmp_path, monkeypatch):
        from pinky_daemon.claude_runner import RunResult

        monkeypatch.delenv("PINKY_DREAM_TRANSPORT", raising=False)
        monkeypatch.delenv("PINKY_KG_PROACTIVE", raising=False)
        messages = [
            {"timestamp": 200.0, "role": "user", "content": "leased interval"}
        ]
        db_path = str(tmp_path / "dream.db")
        owner = DreamRunner(
            db_path=db_path,
            history_provider=lambda agent, after_ts, limit, role: messages,
        )
        owner._save_state("pinky", "seed", last_message_ts=100.0)
        receipt = owner._begin_reflection_attempt(
            "pinky",
            "2026-08-12",
            dream_start=datetime.now(timezone.utc),
            history_start_ts=100.0,
            history_end_ts=200.0,
        )
        assert receipt is not None

        calls = {"n": 0}

        class CountedSDKRunner:
            def __init__(self, config, agent_name=""):
                self.config = config

            async def run(self, prompt):
                calls["n"] += 1
                return RunResult(output="Consolidated.", exit_code=0)

        monkeypatch.setattr(
            "pinky_daemon.dream_runner.SDKRunner", CountedSDKRunner
        )
        contender = DreamRunner(
            db_path=db_path,
            history_provider=lambda agent, after_ts, limit, role: messages,
        )
        blocked = await contender.run_dream(
            "pinky", _DreamAgentConfig(str(tmp_path))
        )
        assert "active lease" in blocked
        assert calls["n"] == 0

        owner._db.execute(
            "UPDATE dream_reflection_attempts SET lease_expires_at=0"
        )
        owner._db.commit()
        assert await contender.run_dream(
            "pinky", _DreamAgentConfig(str(tmp_path))
        ) == "Consolidated."
        assert calls["n"] == 1

    @pytest.mark.asyncio
    async def test_live_runner_periodically_extends_lease_and_blocks_contender(
        self, tmp_path, monkeypatch
    ):
        from pinky_daemon.claude_runner import RunResult

        monkeypatch.delenv("PINKY_DREAM_TRANSPORT", raising=False)
        monkeypatch.delenv("PINKY_KG_PROACTIVE", raising=False)
        monkeypatch.setattr(dream_runner_module, "_REFLECTION_ATTEMPT_LEASE_S", 5.0)
        monkeypatch.setattr(
            dream_runner_module, "_REFLECTION_ATTEMPT_RENEW_INTERVAL_S", 0.02
        )
        monkeypatch.setattr(dream_runner_module, "_SDK_DREAM_TIMEOUT_S", 2.0)
        entered = asyncio.Event()
        release = asyncio.Event()
        calls = {"n": 0}

        class BlockingSDKRunner:
            def __init__(self, config, agent_name=""):
                self.config = config

            async def run(self, prompt):
                calls["n"] += 1
                entered.set()
                await release.wait()
                return RunResult(output="Consolidated.", exit_code=0)

        monkeypatch.setattr(
            "pinky_daemon.dream_runner.SDKRunner", BlockingSDKRunner
        )
        messages = [
            {"timestamp": 200.0, "role": "user", "content": "long live interval"}
        ]
        db_path = str(tmp_path / "dream.db")
        owner = DreamRunner(
            db_path=db_path,
            history_provider=lambda agent, after_ts, limit, role: messages,
        )
        contender = DreamRunner(
            db_path=db_path,
            history_provider=lambda agent, after_ts, limit, role: messages,
        )

        owner_task = asyncio.create_task(
            owner.run_dream("pinky", _DreamAgentConfig(str(tmp_path)))
        )
        await entered.wait()
        original_expiry = owner._db.execute(
            "SELECT lease_expires_at FROM dream_reflection_attempts"
        ).fetchone()[0]
        renewed_expiry = original_expiry
        async with asyncio.timeout(2.0):
            while renewed_expiry <= original_expiry:
                await asyncio.sleep(0.01)
                renewed_expiry = owner._db.execute(
                    "SELECT lease_expires_at FROM dream_reflection_attempts"
                ).fetchone()[0]
        assert renewed_expiry > original_expiry
        assert renewed_expiry > time.time()

        blocked = await contender.run_dream(
            "pinky", _DreamAgentConfig(str(tmp_path))
        )
        assert "active lease" in blocked
        assert calls["n"] == 1

        release.set()
        assert await owner_task == "Consolidated."
        assert calls["n"] == 1

    def test_owner_renewal_prevents_takeover_past_original_expiry(
        self, tmp_path, monkeypatch
    ):
        clock = {"now": 1_000.0}
        monkeypatch.setattr(dream_runner_module.time, "time", lambda: clock["now"])
        monkeypatch.setattr(dream_runner_module, "_REFLECTION_ATTEMPT_LEASE_S", 120.0)
        db_path = str(tmp_path / "dream.db")
        owner = DreamRunner(db_path=db_path)
        attempt = owner._begin_reflection_attempt(
            "pinky",
            "2026-08-12",
            dream_start=datetime.now(timezone.utc),
            history_start_ts=100.0,
            history_end_ts=200.0,
        )
        assert attempt is not None
        original_expiry = attempt.lease_expires_at

        clock["now"] = original_expiry - 1.0
        assert owner._renew_reflection_attempt_lease(attempt) is True
        renewed_expiry = owner._db.execute(
            "SELECT lease_expires_at FROM dream_reflection_attempts"
        ).fetchone()[0]
        assert renewed_expiry > original_expiry

        clock["now"] = original_expiry + 1.0
        contender = DreamRunner(db_path=db_path)
        assert contender._claim_pending_reflection_attempt("pinky") is None
        assert owner._db.execute(
            "SELECT lease_owner, lease_expires_at FROM dream_reflection_attempts"
        ).fetchone() == (owner._lease_owner, renewed_expiry)

    @pytest.mark.asyncio
    async def test_renewal_refuses_at_sdk_deadline(self, tmp_path, monkeypatch):
        monkeypatch.setattr(dream_runner_module, "_REFLECTION_ATTEMPT_RENEW_INTERVAL_S", 0.02)
        runner = DreamRunner(db_path=str(tmp_path / "dream.db"))
        attempt = runner._begin_reflection_attempt(
            "pinky", "2026-08-12", dream_start=datetime.now(timezone.utc),
            history_start_ts=0, history_end_ts=1,
        )
        assert attempt is not None
        renewals = []
        monkeypatch.setattr(runner, "_renew_reflection_attempt_lease", lambda a: renewals.append(a) or True)
        stopped = asyncio.Event()
        deadline = asyncio.get_running_loop().time() + 0.02
        task = asyncio.create_task(
            runner._renew_reflection_attempt_lease_until_stopped(attempt, stopped, deadline)
        )
        await asyncio.sleep(0.05)
        stopped.set()
        await task
        assert renewals == []

    @pytest.mark.asyncio
    async def test_sdk_wall_timeout_is_failed_attempt_and_cancels_run(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.delenv("PINKY_DREAM_TRANSPORT", raising=False)
        monkeypatch.delenv("PINKY_KG_PROACTIVE", raising=False)
        monkeypatch.setattr(dream_runner_module, "_SDK_DREAM_TIMEOUT_S", 0.03)
        cancelled = asyncio.Event()

        class HangingSDKRunner:
            def __init__(self, config, agent_name=""):
                self.config = config

            async def run(self, prompt):
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()

        monkeypatch.setattr(
            "pinky_daemon.dream_runner.SDKRunner", HangingSDKRunner
        )
        runner = DreamRunner(
            db_path=str(tmp_path / "dream.db"),
            history_provider=lambda agent, after_ts, limit, role: [
                {"timestamp": 200.0, "role": "user", "content": "hung interval"}
            ],
        )

        summary = await runner.run_dream(
            "pinky", _DreamAgentConfig(str(tmp_path))
        )

        assert "TimeoutError" in summary
        assert "0.03s wall timeout" in summary
        assert cancelled.is_set()
        assert runner._db.execute(
            "SELECT attempt_n, status, terminal FROM dream_reflection_attempts"
        ).fetchone() == (1, "failed", 0)
        assert runner._db.execute(
            "SELECT runner_completed_at FROM dream_reflection_attempts"
        ).fetchone() == (None,)
        assert runner._db.execute(
            "SELECT last_message_ts FROM dream_state WHERE agent_name='pinky'"
        ).fetchone()[0] == 0
        await asyncio.sleep(0.08)
        assert runner._db.execute(
            "SELECT status, runner_completed_at FROM dream_reflection_attempts"
        ).fetchone() == ("failed", None)
        assert runner._db.execute(
            "SELECT last_message_ts FROM dream_state WHERE agent_name='pinky'"
        ).fetchone()[0] == 0

    @pytest.mark.asyncio
    async def test_sdk_timeout_discards_late_success_from_cancellation_suppressor(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.delenv("PINKY_DREAM_TRANSPORT", raising=False)
        monkeypatch.setattr(dream_runner_module, "_SDK_DREAM_TIMEOUT_S", 0.02)
        late_returned = asyncio.Event()

        class CancellationSuppressingRunner:
            def __init__(self, config, agent_name=""):
                pass

            async def run(self, prompt):
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    await asyncio.sleep(0.06)
                    from pinky_daemon.claude_runner import RunResult
                    late_returned.set()
                    return RunResult(output="late success", exit_code=0)

        monkeypatch.setattr(dream_runner_module, "SDKRunner", CancellationSuppressingRunner)
        runner = DreamRunner(
            db_path=str(tmp_path / "dream.db"),
            history_provider=lambda *args: [{"timestamp": 200.0, "role": "user", "content": "x"}],
        )
        summary = await runner.run_dream("pinky", _DreamAgentConfig(str(tmp_path)))
        assert "TimeoutError" in summary
        assert runner._db.execute(
            "SELECT status, terminal FROM dream_reflection_attempts"
        ).fetchone() == ("failed", 0)
        assert runner._db.execute(
            "SELECT runner_completed_at FROM dream_reflection_attempts"
        ).fetchone() == (None,)
        assert runner._db.execute(
            "SELECT last_message_ts FROM dream_state WHERE agent_name='pinky'"
        ).fetchone()[0] == 0
        await late_returned.wait()
        assert runner._db.execute(
            "SELECT status, runner_completed_at FROM dream_reflection_attempts"
        ).fetchone() == ("failed", None)
        assert runner._db.execute(
            "SELECT last_message_ts FROM dream_state WHERE agent_name='pinky'"
        ).fetchone()[0] == 0

    def test_concurrent_constructors_serialize_migration(self, tmp_path):
        db_path = str(tmp_path / "dream.db")

        def construct(_):
            runner = DreamRunner(db_path=db_path)
            return runner._db.execute("PRAGMA user_version").fetchone()[0]

        with ThreadPoolExecutor(max_workers=12) as pool:
            versions = list(pool.map(construct, range(12)))
        assert versions == [1] * 12

    @pytest.mark.asyncio
    async def test_untagged_window_never_proves_completion_and_cap_unblocks_next_night(
        self, tmp_path, monkeypatch
    ):
        from pinky_daemon.claude_runner import RunResult
        from pinky_memory.store import ReflectionStore
        from pinky_memory.types import Reflection

        monkeypatch.delenv("PINKY_DREAM_TRANSPORT", raising=False)
        monkeypatch.delenv("PINKY_KG_PROACTIVE", raising=False)
        memory_dir = tmp_path / "data"
        memory_dir.mkdir()
        memory_db = memory_dir / "memory.db"
        messages = [
            {"timestamp": 200.0, "role": "user", "content": "frozen interval"}
        ]
        night_keys = iter(("2026-08-12", "2026-08-13"))
        monkeypatch.setattr(
            DreamRunner,
            "_night_key",
            staticmethod(lambda agent_config, dream_start: next(night_keys)),
        )

        class SimulatedProcessDeath(BaseException):
            pass

        class DyingSDKRunner:
            def __init__(self, config, agent_name=""):
                self.config = config

            async def run(self, prompt):
                raise SimulatedProcessDeath("pre-write process death")

        monkeypatch.setattr(
            "pinky_daemon.dream_runner.SDKRunner", DyingSDKRunner
        )
        db_path = str(tmp_path / "dream.db")
        runner = DreamRunner(
            db_path=db_path,
            history_provider=lambda agent, after_ts, limit, role: list(messages),
        )
        runner._save_state("pinky", "seed", last_message_ts=100.0)
        with pytest.raises(SimulatedProcessDeath):
            await runner.run_dream("pinky", _DreamAgentConfig(str(tmp_path)))

        store = ReflectionStore(db_path=str(memory_db))
        unrelated = store.insert(
            Reflection(content="ordinary untagged memory", embedding=[1.0, 0.0])
        )
        store.close()
        runner._db.execute(
            "UPDATE dream_reflection_attempts SET lease_expires_at=0"
        )
        runner._db.commit()
        runner.close()

        results = iter(
            (
                RunResult(output="", exit_code=1, error="boom one"),
                RunResult(output="", exit_code=1, error="boom two"),
                RunResult(output="", exit_code=1, error="boom three"),
                RunResult(output="Consolidated next night.", exit_code=0),
            )
        )

        class RetryingSDKRunner:
            def __init__(self, config, agent_name=""):
                self.config = config

            async def run(self, prompt):
                return next(results)

        monkeypatch.setattr(
            "pinky_daemon.dream_runner.SDKRunner", RetryingSDKRunner
        )
        reopened = DreamRunner(
            db_path=db_path,
            history_provider=lambda agent, after_ts, limit, role: list(messages),
        )
        for _ in range(3):
            await reopened.run_dream("pinky", _DreamAgentConfig(str(tmp_path)))

        assert reopened._get_last_message_ts("pinky") == 200.0
        outcome = reopened._db.execute(
            """SELECT embedded_reflections, outcome_status
               FROM dream_reflection_outcomes
               WHERE agent_name='pinky' AND night_key='2026-08-12'"""
        ).fetchone()
        assert outcome == (0, "failed-terminal")
        assert reopened._db.execute(
            """SELECT status, terminal FROM dream_reflection_attempts
               WHERE night_key='2026-08-12' ORDER BY attempt_n"""
        ).fetchall() == [("failed", 0), ("failed", 0), ("failed", 1)]
        with sqlite3.connect(memory_db) as connection:
            assert connection.execute(
                "SELECT source_session_id FROM reflections WHERE id=?",
                (unrelated.id,),
            ).fetchone()[0] is None

        messages.append(
            {"timestamp": 300.0, "role": "user", "content": "next night interval"}
        )
        assert await reopened.run_dream(
            "pinky", _DreamAgentConfig(str(tmp_path))
        ) == "Consolidated next night."
        assert reopened._get_last_message_ts("pinky") == 300.0

    @pytest.mark.asyncio
    async def test_mixed_tagged_and_untagged_counts_only_exact_tag(
        self, tmp_path, monkeypatch
    ):
        from pinky_daemon.claude_runner import RunResult
        from pinky_memory.store import ReflectionStore
        from pinky_memory.types import Reflection

        monkeypatch.delenv("PINKY_DREAM_TRANSPORT", raising=False)
        monkeypatch.delenv("PINKY_KG_PROACTIVE", raising=False)
        memory_dir = tmp_path / "data"
        memory_dir.mkdir()
        memory_db = memory_dir / "memory.db"
        messages = [
            {"timestamp": 200.0, "role": "user", "content": "first interval"}
        ]
        calls = {"n": 0}

        class MixedSDKRunner:
            def __init__(self, config, agent_name=""):
                self.config = config

            async def run(self, prompt):
                calls["n"] += 1
                correlation_id = prompt.split("source_session_id='", 1)[1].split(
                    "'", 1
                )[0]
                store = ReflectionStore(db_path=str(memory_db))
                store.insert(
                    Reflection(
                        content=f"tagged {calls['n']}",
                        embedding=[1.0, 0.0],
                        source_session_id=correlation_id,
                    )
                )
                store.insert(
                    Reflection(
                        content=f"untagged {calls['n']}", embedding=[1.0, 0.0]
                    )
                )
                store.close()
                return RunResult(
                    output="partial",
                    exit_code=1 if calls["n"] == 1 else 0,
                    error="crash after mixed writes" if calls["n"] == 1 else "",
                )

        monkeypatch.setattr(
            "pinky_daemon.dream_runner.SDKRunner", MixedSDKRunner
        )
        monkeypatch.setattr(
            DreamRunner,
            "_night_key",
            staticmethod(lambda agent_config, dream_start: "2026-08-12"),
        )
        runner = DreamRunner(
            db_path=str(tmp_path / "dream.db"),
            history_provider=lambda agent, after_ts, limit, role: list(messages),
        )

        assert await runner.run_dream(
            "pinky", _DreamAgentConfig(str(tmp_path))
        ) == "partial"
        messages.append(
            {"timestamp": 300.0, "role": "user", "content": "later interval"}
        )
        await runner.run_dream("pinky", _DreamAgentConfig(str(tmp_path)))

        assert runner._db.execute(
            """SELECT embedded_reflections FROM dream_reflection_attempts
               ORDER BY attempt_n"""
        ).fetchall() == [(1,), (1,)]
        assert runner._db.execute(
            """SELECT embedded_reflections FROM dream_reflection_outcomes
               WHERE agent_name='pinky' AND night_key='2026-08-12'"""
        ).fetchone()[0] == 2
        with sqlite3.connect(memory_db) as connection:
            assert connection.execute(
                "SELECT COUNT(*) FROM reflections WHERE source_session_id IS NULL"
            ).fetchone()[0] == 2

    @pytest.mark.asyncio
    async def test_per_run_mcp_config_injects_header_and_is_removed(
        self, tmp_path, monkeypatch
    ):
        from pinky_daemon.claude_runner import RunResult

        monkeypatch.delenv("PINKY_DREAM_TRANSPORT", raising=False)
        monkeypatch.delenv("PINKY_KG_PROACTIVE", raising=False)
        canonical = {
            "mcpServers": {
                "pinky-memory": {
                    "type": "sse",
                    "url": "http://127.0.0.1:8890/mcp/memory/sse",
                    "headers": {"X-Agent-Name": "pinky"},
                },
                "pinky-self": {"command": "unchanged"},
            }
        }
        (tmp_path / ".mcp.json").write_text(json.dumps(canonical))
        captured = {}

        class ConfigInspectingSDKRunner:
            def __init__(self, config, agent_name=""):
                path = Path(config.mcp_servers)
                captured["path"] = path
                captured["mode"] = path.stat().st_mode & 0o777
                captured["config"] = json.loads(path.read_text())

            async def run(self, prompt):
                return RunResult(output="Consolidated.", exit_code=0)

        monkeypatch.setattr(
            "pinky_daemon.dream_runner.SDKRunner", ConfigInspectingSDKRunner
        )
        runner = DreamRunner(
            db_path=str(tmp_path / "dream.db"),
            history_provider=lambda agent, after_ts, limit, role: [
                {"timestamp": 200.0, "role": "user", "content": "context"}
            ],
        )
        await runner.run_dream("pinky", _DreamAgentConfig(str(tmp_path)))

        header = captured["config"]["mcpServers"]["pinky-memory"]["headers"]
        assert header["X-Agent-Name"] == "pinky"
        assert header["X-Dream-Correlation"].startswith("dream:pinky:")
        assert captured["mode"] == 0o600
        assert not captured["path"].exists()
        assert json.loads((tmp_path / ".mcp.json").read_text()) == canonical

    @pytest.mark.asyncio
    async def test_constructor_failure_cleans_secret_config_and_consumes_attempt(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.delenv("PINKY_DREAM_TRANSPORT", raising=False)
        monkeypatch.delenv("PINKY_KG_PROACTIVE", raising=False)
        bearer = "Bearer constructor-secret"
        canonical = {
            "mcpServers": {
                "pinky-memory": {
                    "type": "sse",
                    "url": "http://127.0.0.1:8890/mcp/memory/sse",
                    "headers": {"Authorization": bearer},
                }
            }
        }
        (tmp_path / ".mcp.json").write_text(json.dumps(canonical))
        captured: list[tuple[Path, int, str]] = []

        class FailingConstructorSDKRunner:
            def __init__(self, config, agent_name=""):
                path = Path(config.mcp_servers)
                captured.append(
                    (path, path.stat().st_mode & 0o777, path.read_text())
                )
                raise ImportError("missing claude-agent-sdk")

        monkeypatch.setattr(
            "pinky_daemon.dream_runner.SDKRunner", FailingConstructorSDKRunner
        )
        runner = DreamRunner(
            db_path=str(tmp_path / "dream.db"),
            history_provider=lambda agent, after_ts, limit, role: [
                {"timestamp": 200.0, "role": "user", "content": "frozen interval"}
            ],
        )

        for expected_attempt in (1, 2, 3):
            summary = await runner.run_dream(
                "pinky", _DreamAgentConfig(str(tmp_path))
            )
            assert "ImportError" in summary
            assert runner._db.execute(
                "SELECT MAX(attempt_n) FROM dream_reflection_attempts"
            ).fetchone()[0] == expected_attempt

        assert len(captured) == 3
        assert all(mode == 0o600 for _path, mode, _content in captured)
        assert all(bearer in content for _path, _mode, content in captured)
        assert all(not path.exists() for path, _mode, _content in captured)
        assert list((tmp_path / "dreams").glob(".mcp-dream-*.json")) == []
        assert runner._db.execute(
            "SELECT attempt_n, status, terminal FROM dream_reflection_attempts "
            "ORDER BY attempt_n"
        ).fetchall() == [(1, "failed", 0), (2, "failed", 0), (3, "failed", 1)]
        assert runner._get_last_message_ts("pinky") == 200.0
        assert json.loads((tmp_path / ".mcp.json").read_text()) == canonical

    def test_atomic_mcp_config_write_unlinks_partial_on_failure(
        self, tmp_path, monkeypatch
    ):
        bearer = "Bearer partial-secret"
        (tmp_path / ".mcp.json").write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "pinky-memory": {
                            "type": "sse",
                            "url": "http://127.0.0.1/mcp",
                            "headers": {"Authorization": bearer},
                        }
                    }
                }
            )
        )
        real_open = os.open
        open_calls: list[tuple[int, int]] = []

        def capture_open(path, flags, mode=0o777):
            open_calls.append((flags, mode))
            return real_open(path, flags, mode)

        def fail_after_partial_write(config, config_file, indent=None):
            config_file.write(f'{{"token": {bearer!r}')
            config_file.flush()
            raise OSError("simulated write failure")

        monkeypatch.setattr(dream_runner_module.os, "open", capture_open)
        monkeypatch.setattr(dream_runner_module.json, "dump", fail_after_partial_write)

        with pytest.raises(OSError, match="simulated write failure"):
            DreamRunner._write_dream_mcp_config(str(tmp_path), "dream:test")

        assert len(open_calls) == 1
        flags, mode = open_calls[0]
        assert flags & os.O_CREAT
        assert flags & os.O_EXCL
        assert flags & os.O_WRONLY
        assert mode == 0o600
        assert list((tmp_path / "dreams").glob(".mcp-dream-*.json")) == []

    def test_atomic_mcp_config_collision_never_unlinks_existing_file(
        self, tmp_path, monkeypatch
    ):
        (tmp_path / ".mcp.json").write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "pinky-memory": {
                            "command": "memory-server",
                        }
                    }
                }
            )
        )
        dreams_dir = tmp_path / "dreams"
        dreams_dir.mkdir()
        existing = dreams_dir / ".mcp-dream-collision.json"
        existing.write_text("belongs to another run")

        class CollisionUUID:
            hex = "collision"

        monkeypatch.setattr(
            dream_runner_module.uuid, "uuid4", lambda: CollisionUUID()
        )

        with pytest.raises(FileExistsError):
            DreamRunner._write_dream_mcp_config(str(tmp_path), "dream:test")

        assert existing.read_text() == "belongs to another run"

    @pytest.mark.asyncio
    async def test_nightly_entry_prunes_only_mcp_config_orphans_over_24h(
        self, tmp_path
    ):
        dreams_dir = tmp_path / "dreams"
        dreams_dir.mkdir()
        old_config = dreams_dir / ".mcp-dream-old.json"
        fresh_config = dreams_dir / ".mcp-dream-fresh.json"
        unrelated = dreams_dir / "notes.json"
        for path in (old_config, fresh_config, unrelated):
            path.write_text("secret")
        now = time.time()
        os.utime(old_config, (now - 25 * 3600, now - 25 * 3600))
        os.utime(fresh_config, (now - 23 * 3600, now - 23 * 3600))
        os.utime(unrelated, (now - 25 * 3600, now - 25 * 3600))
        runner = DreamRunner(
            db_path=str(tmp_path / "dream.db"),
            history_provider=lambda agent, after_ts, limit, role: [],
        )

        await runner.run_dream("pinky", _DreamAgentConfig(str(tmp_path)))

        assert not old_config.exists()
        assert fresh_config.exists()
        assert unrelated.exists()

    def test_receipt_prune_removes_only_expired_closed_rows(self, tmp_path):
        runner = DreamRunner(db_path=str(tmp_path / "dream.db"))
        now = time.time()

        for agent in (
            "old-completed",
            "old-terminal",
            "old-retryable",
            "recent",
            "pending",
        ):
            receipt = runner._begin_reflection_attempt(
                agent,
                "2026-08-12",
                dream_start=datetime.now(timezone.utc),
                history_start_ts=100.0,
                history_end_ts=200.0,
            )
            assert receipt is not None
        runner._db.execute(
            """UPDATE dream_reflection_attempts
               SET status='completed', completed_at=?
               WHERE agent_name='old-completed'""",
            (now - 31 * 24 * 3600,),
        )
        runner._db.execute(
            """UPDATE dream_reflection_attempts
               SET status='failed', terminal=1, completed_at=?
               WHERE agent_name='old-terminal'""",
            (now - 31 * 24 * 3600,),
        )
        runner._db.execute(
            """UPDATE dream_reflection_attempts
               SET status='failed', terminal=0, completed_at=?
               WHERE agent_name='old-retryable'""",
            (now - 31 * 24 * 3600,),
        )
        runner._db.execute(
            """UPDATE dream_reflection_attempts
               SET status='completed', completed_at=?
               WHERE agent_name='recent'""",
            (now - 29 * 24 * 3600,),
        )
        runner._db.commit()

        assert runner._get_retryable_failed_attempt("old-retryable") is not None
        assert runner._prune_reflection_attempts(now=now) == 2
        assert runner._get_retryable_failed_attempt("old-retryable") is not None
        assert runner._db.execute(
            "SELECT agent_name FROM dream_reflection_attempts ORDER BY agent_name"
        ).fetchall() == [("old-retryable",), ("pending",), ("recent",)]

    def test_dream_schema_future_version_refuses_startup(self, tmp_path):
        db_path = tmp_path / "dream.db"
        with sqlite3.connect(db_path) as connection:
            connection.execute("PRAGMA user_version=2")
            connection.commit()

        with pytest.raises(RuntimeError) as exc_info:
            DreamRunner(db_path=str(db_path))

        message = str(exc_info.value)
        assert "durable-dream-receipts-v1" in message
        assert "PinkyBot >=26.08.020" in message
        assert "refusing startup" in message
        with sqlite3.connect(db_path) as connection:
            assert connection.execute("PRAGMA user_version").fetchone()[0] == 2

    def test_schema_bump_before_constructor_is_refused_without_mutation(self, tmp_path):
        db_path = tmp_path / "dream.db"
        with sqlite3.connect(db_path) as connection:
            connection.execute("CREATE TABLE sentinel (value TEXT)")
            connection.execute("INSERT INTO sentinel VALUES ('before')")
            connection.execute("PRAGMA user_version=2")
            connection.commit()
        with pytest.raises(RuntimeError, match="schema is newer"):
            DreamRunner(db_path=str(db_path))
        with sqlite3.connect(db_path) as connection:
            assert connection.execute("SELECT * FROM sentinel").fetchone() == ("before",)
            assert connection.execute("PRAGMA user_version").fetchone()[0] == 2

    def test_round_two_rowid_boundary_schema_is_dropped_on_migration(self, tmp_path):
        db_path = tmp_path / "dream.db"
        with sqlite3.connect(db_path) as connection:
            connection.execute(
                """CREATE TABLE dream_reflection_attempts (
                       agent_name TEXT NOT NULL,
                       night_key TEXT NOT NULL,
                       attempt_n INTEGER NOT NULL,
                       status TEXT NOT NULL,
                       dream_started_at TEXT NOT NULL,
                       history_start_ts REAL NOT NULL,
                       history_end_ts REAL NOT NULL,
                       correlation_id TEXT NOT NULL,
                       reflection_rowid_floor INTEGER NOT NULL,
                       summary TEXT,
                       runner_completed_at REAL,
                       embedded_reflections INTEGER,
                       completed_at REAL,
                       PRIMARY KEY (agent_name, night_key, attempt_n)
                   )"""
            )

        runner = DreamRunner(db_path=str(db_path))
        columns = {
            row[1]
            for row in runner._db.execute(
                "PRAGMA table_info(dream_reflection_attempts)"
            ).fetchall()
        }
        assert "reflection_rowid_floor" not in columns
        assert {"lease_owner", "lease_expires_at", "terminal"} <= columns
        assert runner._db.execute("PRAGMA user_version").fetchone()[0] == 1
        assert runner._begin_reflection_attempt(
            "pinky",
            "2026-08-12",
            dream_start=datetime.now(timezone.utc),
            history_start_ts=100.0,
            history_end_ts=200.0,
        ) is not None

    @pytest.mark.asyncio
    async def test_failed_alert_retries_until_one_positive_receipt(self, tmp_path):
        attempts: list[str] = []
        receipts = iter((False, True))

        async def owner_notify(agent_name: str, message: str) -> bool:
            attempts.append(message)
            return next(receipts)

        runner = DreamRunner(
            db_path=str(tmp_path / "dream.db"),
            owner_notify_callback=owner_notify,
        )
        for sequence, night_key in enumerate(
            (
                "2026-08-08",
                "2026-08-09",
                "2026-08-10",
                "2026-08-11",
                "2026-08-12",
            ),
            start=1,
        ):
            await self._record_night(runner, night_key, 0, sequence)

        state = runner.get_state("pinky")
        assert state["zero_reflection_streak"] == 5
        assert state["zero_reflection_alert_attempted_at"] is not None
        assert state["zero_reflection_alert_delivered_at"] is not None
        assert len(attempts) == 2
        assert "3 consecutive nights" in attempts[0]
        assert "4 consecutive nights" in attempts[1]

        alert_rows = runner._db.execute(
            """SELECT night_key, alert_attempted_at, alert_delivered_at
               FROM dream_reflection_outcomes
               WHERE alert_attempted_at IS NOT NULL
               ORDER BY night_key"""
        ).fetchall()
        assert len(alert_rows) == 2
        assert alert_rows[0][2] is None
        assert alert_rows[1][2] is not None

    @pytest.mark.asyncio
    async def test_positive_night_resets_streak_and_delivery_latch(self, tmp_path):
        notices: list[str] = []

        async def owner_notify(agent_name: str, message: str) -> bool:
            notices.append(message)
            return True

        runner = DreamRunner(
            db_path=str(tmp_path / "dream.db"),
            owner_notify_callback=owner_notify,
        )
        for sequence, night_key in enumerate(
            ("2026-08-06", "2026-08-07", "2026-08-08"), start=1
        ):
            await self._record_night(runner, night_key, 0, sequence)

        delivered = runner.get_state("pinky")
        assert delivered["zero_reflection_streak"] == 3
        assert delivered["zero_reflection_alert_attempted_at"] is not None
        assert delivered["zero_reflection_alert_delivered_at"] is not None
        assert len(notices) == 1

        await self._record_night(runner, "2026-08-09", 2, 4)
        reset = runner.get_state("pinky")
        assert reset["zero_reflection_streak"] == 0
        assert reset["zero_reflection_alert_attempted_at"] is None
        assert reset["zero_reflection_alert_delivered_at"] is None

        for sequence, night_key in enumerate(
            ("2026-08-10", "2026-08-11", "2026-08-12"), start=5
        ):
            await self._record_night(runner, night_key, 0, sequence)

        assert runner.get_state("pinky")["zero_reflection_streak"] == 3
        assert len(notices) == 2
