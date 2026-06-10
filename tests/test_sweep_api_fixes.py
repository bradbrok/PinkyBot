"""Regression tests for the api sweep fixes (cold-start race, broker send
kwargs, iMessage outbound, adapter cache eviction, fail-closed tool gates,
auth rate limiting, streaming-session rename, model-change persistence)."""

from __future__ import annotations

import asyncio
import os
import tempfile
import time
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient


def _make_app(db_path: str):
    from pinky_daemon.api import create_api
    return create_api(max_sessions=10, default_working_dir="/tmp", db_path=db_path)


class _FakeContextClient:
    def __init__(self, total_tokens=0, max_tokens=200_000):
        self.total_tokens = total_tokens
        self.max_tokens = max_tokens
        self.queries: list[str] = []

    async def get_context_usage(self):
        return {"totalTokens": self.total_tokens, "maxTokens": self.max_tokens}

    async def query(self, prompt: str):
        self.queries.append(prompt)


class _FakeStreamingSession:
    def __init__(self, agent_name: str, label: str = "main", *, max_tokens: int = 200_000):
        from pinky_daemon.transport_state import SessionState
        self._TS = SessionState
        self.agent_name = agent_name
        self.label = label
        self.resume_handle = f"{agent_name}-{label}-sdk"
        self.created_at = time.time()
        self.last_active = self.created_at
        self._state = SessionState.CONNECTED
        self._stats = {
            "messages_sent": 2, "turns": 3, "errors": 0,
            "reconnects": 0, "auto_restarts": 0,
        }
        self._config = SimpleNamespace(
            model="sonnet", label=label, context_restart_pct=80,
            permission_mode="bypassPermissions",
        )
        self.usage = SimpleNamespace(total_cost_usd=0.0, input_tokens=0, output_tokens=0)
        self._client = _FakeContextClient(max_tokens=max_tokens)
        self.disconnect_calls = 0
        self.connect_calls = 0

    @property
    def state(self):
        return self._state

    @property
    def id(self) -> str:
        return f"{self.agent_name}-{self._config.label}"

    @property
    def stats(self) -> dict:
        return {
            **self._stats,
            "connected": self._state == self._TS.CONNECTED,
            "pending_responses": 0, "cost_usd": 0.0, "account": {},
        }

    async def disconnect(self):
        self.disconnect_calls += 1
        self._state = self._TS.DEAD

    async def connect(self):
        self.connect_calls += 1
        self._state = self._TS.CONNECTED


class TestColdStartRace:
    def test_concurrent_cold_starts_share_one_session(self):
        """Concurrent _ensure_streaming_session calls for the same (agent,
        label) must serialize: one cold start, every caller gets the same
        session object, no orphaned loser subprocess."""
        import pinky_daemon.streaming_session as ss_mod

        connect_count = {"n": 0}

        async def fake_connect(self):
            from pinky_daemon.transport_state import SessionState
            connect_count["n"] += 1
            await asyncio.sleep(0.05)  # widen the race window
            self._state_machine._state = SessionState.CONNECTED

        with tempfile.TemporaryDirectory() as tmpdir, \
                patch.object(ss_mod.StreamingSession, "connect", new=fake_connect):
            db_path = os.path.join(tmpdir, "test.db")
            app = _make_app(db_path)
            with TestClient(app) as client:
                client.post("/agents", json={"name": "race-agent", "model": "sonnet"})
                ensure = app.state.broker._ensure_session_callback

                async def run():
                    return await asyncio.gather(
                        ensure("race-agent"), ensure("race-agent"), ensure("race-agent")
                    )

                s1, s2, s3 = asyncio.run(run())
                assert s1 is not None
                assert s1 is s2 is s3
                assert connect_count["n"] == 1, (
                    f"expected exactly one cold start, got {connect_count['n']} "
                    f"(concurrent triggers each launched their own session)"
                )


class TestColdStartTimeoutAlert:
    def test_timeout_alert_uses_content_kwarg(self):
        """The cold-start-timeout owner alert must actually reach the broker
        send callback (it used to pass text= and TypeError every time)."""
        import pytest

        import pinky_daemon.api as api_mod
        import pinky_daemon.streaming_session as ss_mod

        async def hang_connect(self):
            await asyncio.sleep(30)

        sent: list[str] = []

        async def recorder(agent_name, platform, chat_id, content, *,
                           reply_to="", parse_mode="", silent=False):
            sent.append(content)
            return {"sent": True}

        with tempfile.TemporaryDirectory() as tmpdir, \
                patch.object(ss_mod.StreamingSession, "connect", new=hang_connect), \
                patch.object(api_mod, "COLD_START_CONNECT_TIMEOUT_SEC", 0.05):
            db_path = os.path.join(tmpdir, "test.db")
            app = _make_app(db_path)
            with TestClient(app) as client:
                client.post("/agents", json={"name": "slow-agent", "model": "sonnet"})
                app.state.agents.get_owner_profile = lambda: {"chat_id": "777"}
                broker = app.state.broker
                broker._send_callback = recorder
                ensure = broker._ensure_session_callback

                with pytest.raises(asyncio.TimeoutError):
                    asyncio.run(ensure("slow-agent"))

                assert sent, "owner alert never reached the broker send callback"
                assert "cold-start" in sent[0]


class TestIMessageOutbound:
    def test_broker_send_imessage_reaches_adapter(self):
        """platform=imessage must route to the iMessage adapter instead of
        503ing on the generic adapter lookup."""
        sent = []

        class FakeIM:
            def __init__(self):
                pass

            def send_message(self, chat_id, content):
                sent.append((chat_id, content))
                return {"message_id": "im-1"}

        with tempfile.TemporaryDirectory() as tmpdir, \
                patch("pinky_outreach.imessage.iMessageAdapter", FakeIM):
            db_path = os.path.join(tmpdir, "test.db")
            app = _make_app(db_path)
            with TestClient(app) as client:
                client.post("/agents", json={"name": "im-agent", "model": "sonnet"})
                r = client.put(
                    "/agents/im-agent/tokens/imessage", json={"token": "enabled"}
                )
                assert r.status_code == 200, r.text

                r = client.post("/broker/send", json={
                    "agent_name": "im-agent",
                    "platform": "imessage",
                    "chat_id": "+15551234567",
                    "content": "hello",
                })
                assert r.status_code == 200, r.text
                assert sent == [("+15551234567", "hello")]


class TestAdapterCacheEviction:
    def test_token_rotation_evicts_cached_outbound_adapter(self):
        """After PUT /tokens/{platform}, outbound sends must use the new
        token instead of the stale cached adapter."""

        class FakeTG:
            instances: list = []

            def __init__(self, token, timeout=None, **kwargs):
                self.token = token
                self.sent: list = []
                FakeTG.instances.append(self)

            def send_message(self, chat_id, content, **kwargs):
                self.sent.append((chat_id, content))
                return {"message_id": "1"}

        class FakePoller:
            def __init__(self, adapter, agent_name, broker, registry=None, **kwargs):
                self._agent_name = agent_name

            async def start(self):
                pass

            def stop(self):
                pass

        with tempfile.TemporaryDirectory() as tmpdir, \
                patch("pinky_outreach.telegram.TelegramAdapter", FakeTG), \
                patch("pinky_daemon.pollers.BrokerTelegramPoller", FakePoller):
            db_path = os.path.join(tmpdir, "test.db")
            app = _make_app(db_path)
            with TestClient(app) as client:
                client.post("/agents", json={"name": "tg-agent", "model": "sonnet"})
                client.put("/agents/tg-agent/tokens/telegram", json={"token": "tok-A"})
                r = client.post("/broker/send", json={
                    "agent_name": "tg-agent", "platform": "telegram",
                    "chat_id": "1", "content": "first",
                })
                assert r.status_code == 200, r.text

                client.put("/agents/tg-agent/tokens/telegram", json={"token": "tok-B"})
                r = client.post("/broker/send", json={
                    "agent_name": "tg-agent", "platform": "telegram",
                    "chat_id": "1", "content": "second",
                })
                assert r.status_code == 200, r.text

                tokens_used = [i.token for i in FakeTG.instances if i.sent]
                assert "tok-B" in tokens_used, (
                    f"send after rotation still used stale adapter "
                    f"(tokens used: {tokens_used})"
                )


class TestAuthRateLimit:
    def test_spoofed_x_forwarded_for_does_not_reset_budget(self):
        """The limiter must key on the socket peer, not the client-controlled
        X-Forwarded-For header (which also grew the dict without bound)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            app = _make_app(db_path)
            with TestClient(app) as client:
                for i in range(5):
                    r = client.post(
                        "/auth/login",
                        json={"password": "wrong"},
                        headers={"X-Forwarded-For": f"10.0.0.{i}"},
                    )
                    assert r.status_code != 429
                r = client.post(
                    "/auth/login",
                    json={"password": "wrong"},
                    headers={"X-Forwarded-For": "10.99.99.99"},
                )
                assert r.status_code == 429, (
                    "a fresh spoofed X-Forwarded-For value reset the rate budget"
                )


class TestRenameStreamingSession:
    def test_rename_retargets_live_session(self):
        """Renaming must update the live session's label and rebind its
        resume-handle persistence to the new label."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            app = _make_app(db_path)
            with TestClient(app) as client:
                client.post("/agents", json={"name": "ren-agent", "model": "sonnet"})
                fake = _FakeStreamingSession("ren-agent", "side")
                app.state.broker.register_streaming("ren-agent", fake, label="side")
                agents = app.state.agents
                agents.set_streaming_session_id("ren-agent", "old-sid", label="side")

                r = client.patch(
                    "/agents/ren-agent/streaming-sessions/side",
                    json={"label": "worker"},
                )
                assert r.status_code == 200, r.text

                assert fake._config.label == "worker"
                assert fake.id == "ren-agent-worker"
                assert agents.get_streaming_session_id("ren-agent", label="worker") == "old-sid"

                # New resume handles must persist under the NEW label
                asyncio.run(fake._on_resume_handle("ren-agent", "fresh-handle"))
                assert (
                    agents.get_streaming_session_id("ren-agent", label="worker")
                    == "fresh-handle"
                )
                assert agents.get_streaming_session_id("ren-agent", label="side") == ""


class TestSetStreamingModelPersistence:
    def test_guard_409_leaves_model_unchanged(self):
        """A 409 from the restart guard must not have already persisted the
        new model to the agents DB."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            app = _make_app(db_path)
            with TestClient(app) as client:
                client.post("/agents", json={"name": "mod-agent", "model": "sonnet"})
                fake = _FakeStreamingSession("mod-agent", "main", max_tokens=200_000)
                app.state.broker.register_streaming("mod-agent", fake, label="main")
                # No recent context save -> restart guard refuses the restart.
                r = client.post(
                    "/agents/mod-agent/streaming/model",
                    json={"model": "claude-opus-4-7"},  # 1M model -> needs restart
                )
                assert r.status_code == 409, r.text
                assert app.state.agents.get("mod-agent").model == "sonnet"

    def test_hot_swap_failure_leaves_model_unchanged(self):
        """A failed hot swap (500) must not have persisted the new model."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            app = _make_app(db_path)
            with TestClient(app) as client:
                client.post("/agents", json={"name": "mod-agent", "model": "sonnet"})
                fake = _FakeStreamingSession("mod-agent", "main", max_tokens=200_000)
                # _FakeContextClient has no set_model -> hot swap raises -> 500
                app.state.broker.register_streaming("mod-agent", fake, label="main")
                r = client.post(
                    "/agents/mod-agent/streaming/model",
                    json={"model": "claude-haiku-4-5"},  # 200k class -> hot swap
                )
                assert r.status_code == 500, r.text
                assert app.state.agents.get("mod-agent").model == "sonnet"


class TestProviderNullFields:
    def test_provider_null_value_is_treated_as_empty(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            app = _make_app(db_path)
            with TestClient(app) as client:
                client.post("/agents", json={"name": "prov-agent", "model": "sonnet"})
                r = client.put(
                    "/agents/prov-agent/provider", json={"provider_url": None}
                )
                assert r.status_code == 200, r.text
                assert r.json()["provider_url"] == ""

    def test_api_key_null_value_is_400_not_500(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            app = _make_app(db_path)
            with TestClient(app) as client:
                r = client.put(
                    "/system/api-keys/OPENAI_API_KEY", json={"value": None}
                )
                assert r.status_code == 400, r.text


class TestCodexSharedMcpBearer:
    def test_codex_mcp_headers_include_derived_bearer(self):
        """Codex agents must carry the derived bearer so shared-MCP configs
        that require auth do not silently reject all their tool calls."""
        import pinky_daemon.api as api_mod
        import pinky_daemon.codex_session as codex_mod
        from pinky_daemon.shared_mcp import derive_mcp_bearer
        from pinky_daemon.transport_state import SessionState

        class FakeCodexSession:
            last_config = None

            def __init__(self, config, **kwargs):
                FakeCodexSession.last_config = config
                self._config = config
                self.agent_name = config.agent_name
                self.resume_handle = ""
                self._state = SessionState.UNINITIALIZED

            @property
            def state(self):
                return self._state

            @property
            def id(self):
                return f"{self.agent_name}-{self._config.label}"

            async def connect(self):
                self._state = SessionState.CONNECTED

            async def disconnect(self):
                self._state = SessionState.DEAD

        with tempfile.TemporaryDirectory() as tmpdir, \
                patch.object(api_mod, "SHARED_MCP_ENABLED", True), \
                patch.object(codex_mod, "CodexSession", FakeCodexSession):
            db_path = os.path.join(tmpdir, "test.db")
            app = _make_app(db_path)
            with TestClient(app) as client:
                client.post("/agents", json={"name": "codex-agent", "model": "gpt-5"})
                agents = app.state.agents
                agents.register("codex-agent", runtime="codex_cli")
                ensure = app.state.broker._ensure_session_callback

                ss = asyncio.run(ensure("codex-agent"))
                assert ss is not None
                cfg = FakeCodexSession.last_config
                assert cfg is not None
                for srv in ("pinky-self", "pinky-memory", "pinky-messaging"):
                    headers = cfg.mcp_servers[srv]["headers"]
                    assert headers["X-Agent-Name"] == "codex-agent"
                    key = agents.get_signing_key("codex-agent")
                    assert headers["Authorization"] == f"Bearer {derive_mcp_bearer(key)}"
