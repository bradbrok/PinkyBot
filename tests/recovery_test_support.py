"""Real lifecycle methods with disposable external-client boundaries."""

from __future__ import annotations

import asyncio
import inspect
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from pinky_daemon.api import create_api
from pinky_daemon.auth import SESSION_COOKIE_NAME, create_session_cookie
from pinky_daemon.codex_session import CodexSession
from pinky_daemon.codex_tmux_session import CodexTmuxSession
from pinky_daemon.streaming_session import StreamingSession, StreamingSessionConfig
from pinky_daemon.tmux_session import TmuxSession
from pinky_daemon.transport_state import SessionState

CLASSES = {
    ("claude_sdk", "sdk"): StreamingSession,
    ("claude_sdk", "tmux"): TmuxSession,
    ("codex_cli", "sdk"): CodexSession,
    ("codex_cli", "tmux"): CodexTmuxSession,
}


class SDKPeer:
    def __init__(self, options=None):
        self.options = options
        self.closed = asyncio.Event()
        self.connect = AsyncMock()
        self.disconnect = AsyncMock(side_effect=self.close)
        self.query = AsyncMock(side_effect=RuntimeError("Scripted peer has no wake turn"))
        self.get_server_info = AsyncMock(return_value={})

    async def close(self):
        self.closed.set()

    async def receive_messages(self):
        await asyncio.Event().wait()
        if False:
            yield None


def closure_value(app, name):
    """Find an actual API closure; never replace the callback being tested."""
    pending = [r.endpoint for r in app.routes if hasattr(r, "endpoint")]
    seen = set()
    while pending:
        fn = pending.pop()
        if id(fn) in seen or not inspect.isfunction(fn):
            continue
        seen.add(id(fn))
        values = inspect.getclosurevars(fn).nonlocals
        if name in values:
            return values[name]
        pending.extend(v for v in values.values() if inspect.isfunction(v))
    raise AssertionError(f"API closure {name} is not reachable")


@pytest.fixture
async def lifecycle_harness(tmp_path, monkeypatch):
    monkeypatch.setenv("PINKY_SESSION_CLASS_REBUILD", "1")
    monkeypatch.setenv("PINKY_CODEX_APP_SERVER", "1")
    monkeypatch.setenv("PINKY_RESUME_FAILSAFE", "1")
    app = create_api(default_working_dir=str(tmp_path), db_path=str(tmp_path / "api.db"))
    sessions, clients, trace = [], [], []
    control = SimpleNamespace(start_error=None, cleanup_error=None, start_hook=None)

    def sdk_factory(options):
        client = SDKPeer(options)

        async def initialize():
            if control.start_hook:
                await control.start_hook(
                    next(ss for ss in sessions if getattr(ss, "_client", None) is client)
                )
            if control.start_error:
                raise control.start_error

        client.connect.side_effect = initialize
        if control.cleanup_error:
            client.disconnect.side_effect = control.cleanup_error
        clients.append(client)
        return client

    monkeypatch.setattr("claude_agent_sdk.ClaudeSDKClient", sdk_factory)

    async def app_server(ss):
        if ss._app_client is None:
            trace.append(("substrate", ss))
            if control.start_hook:
                await control.start_hook(ss)
            if control.start_error:
                raise control.start_error

            async def request(method, params):
                if method == "turn/start":
                    await ss._on_appserver_notification(
                        "turn/completed",
                        {
                            "threadId": "22222222-2222-4222-8222-222222222222",
                            "turn": {"id": "one", "status": "completed"},
                        },
                    )
                    return {"turn": {"id": "one"}}
                return {"thread": {"id": "22222222-2222-4222-8222-222222222222"}}

            ss._app_client = SimpleNamespace(close=AsyncMock(), request=request)
        return True

    monkeypatch.setattr(CodexSession, "_ensure_app_server", app_server)

    async def tmux_spawn(ss):
        trace.append(("substrate", ss))
        ss._skip_wake_prompt_for_tests = True
        if control.start_hook:
            await control.start_hook(ss)
        if control.start_error:
            raise control.start_error

    monkeypatch.setattr(TmuxSession, "_spawn_tmux_repl", tmux_spawn)
    monkeypatch.setattr(CodexTmuxSession, "_spawn_tmux_repl", tmux_spawn)
    # The isolated-home filesystem prerequisite is outside transport ownership.
    monkeypatch.setattr(
        "pinky_daemon.codex_tmux_session.validate_agent_codex_home", lambda *a, **k: None
    )

    for cls in CLASSES.values():
        original_init = cls.__init__

        def init(ss, *args, _original=original_init, **kwargs):
            _original(ss, *args, **kwargs)
            if ss not in sessions:
                sessions.append(ss)
            if isinstance(ss, TmuxSession):
                ss._tmux.kill_session = AsyncMock(return_value=SimpleNamespace(ok=True))
                if control.cleanup_error:
                    ss._tmux.kill_session.side_effect = control.cleanup_error

        monkeypatch.setattr(cls, "__init__", init)
        original_connect = cls.connect

        async def connect(ss, *args, _original=original_connect, **kwargs):
            trace.append(("connect", ss))
            return await _original(ss, *args, **kwargs)

        monkeypatch.setattr(cls, "connect", connect)

    def seed(source=("claude_sdk", "sdk"), label="main"):
        workdir = tmp_path / "sample"
        workdir.mkdir(exist_ok=True)
        app.state.agents.register(
            "sample",
            runtime=source[0],
            transport=source[1],
            model="sonnet",
            working_dir=str(workdir),
        )
        ss = CLASSES[source](
            StreamingSessionConfig(
                agent_name="sample",
                working_dir=str(workdir),
                label=label,
                model="sonnet",
            )
        )
        ss._state_machine._state = SessionState.CONNECTED
        ss.resume_handle = "saved"
        if type(ss) is StreamingSession:
            ss._client = SDKPeer()
            clients.append(ss._client)
        app.state.broker.register_streaming("sample", ss, label=label)
        app.state.agents.set_context(
            "sample", task="Saved work", metadata={"source": "save_my_context"}, updated_by="saved"
        )
        return ss

    monkeypatch.setattr(app.state.broker, "_send_message", AsyncMock())
    monkeypatch.setattr(app.state.broker, "_start_typing", AsyncMock())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        cookies={SESSION_COOKIE_NAME: create_session_cookie(os.environ["PINKY_SESSION_SECRET"])},
    ) as client:
        try:
            yield SimpleNamespace(
                app=app,
                client=client,
                seed=seed,
                control=control,
                clients=clients,
                sessions=sessions,
                trace=trace,
            )
        finally:
            tasks = []
            for ss in sessions:
                recovery = getattr(ss, "_reconnect_task", None)
                if recovery and not recovery.done():
                    recovery.cancel()
                    tasks.append(recovery)
                ss._replacement_cleanup_strict = False
                if getattr(ss, "_client", None) and isinstance(ss._client, SDKPeer):
                    ss._client.disconnect.side_effect = ss._client.close
                tasks.append(asyncio.create_task(ss.disconnect()))
            await asyncio.gather(*tasks, return_exceptions=True)


def set_flags(monkeypatch, mode):
    for name, on in [
        ("PINKY_SESSION_CLASS_REBUILD", mode in {"a", "both"}),
        ("PINKY_RESUME_FAILSAFE", mode in {"b", "both"}),
        ("PINKY_MODEL_RUNTIME_GUARD", mode == "q2"),
    ]:
        monkeypatch.setenv(name, "1" if on else "0")
