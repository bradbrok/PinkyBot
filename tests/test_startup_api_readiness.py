"""Model prompt delivery requires the main API listener, including during boot."""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from pinky_daemon import __main__ as daemon_main
from pinky_daemon import api
from pinky_daemon.codex_session import CodexSession
from pinky_daemon.codex_tmux_session import CodexTmuxSession
from pinky_daemon.streaming_session import StreamingSession
from pinky_daemon.tmux_session import TmuxSession
from pinky_daemon.transport_state import SessionState
from pinky_daemon.wake_prompt import WakeReason

MODES = ("claude-sdk", "claude-tmux", "codex-tmux", "codex-exec", "codex-app-server")
READY_PHASE = 2


@dataclass
class BootRun:
    app: object
    server: object = None
    observed: list[tuple[str, str, int, bool]] = field(default_factory=list)
    tasks: list[asyncio.Task] = field(default_factory=list)
    connections: list[str] = field(default_factory=list)
    bind: bool = True
    stop_before_bind: bool = False
    extra_source: str = ""
    manual: bool = False
    manual_result: object = None

    def submit(self, edge, prompt):
        self.observed.append((edge, prompt, self.server.phase, self.server.started))

    async def checkpoint(self):
        # This is a causal pre-bind window, not a wall-clock ordering guess.
        for _ in range(64):
            await asyncio.sleep(0)

    async def wait_for_delivery(self, count):
        async with asyncio.timeout(5):
            while len(self.observed) < count:
                await asyncio.sleep(0.001)


@pytest.fixture
def boot_run(tmp_path, monkeypatch):
    import claude_agent_sdk
    import uvicorn

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PINKY_LOG_ROTATION", "off")
    monkeypatch.setenv("PINKY_MCP_READINESS_CAP_SEC", "0")
    monkeypatch.setenv("PINKY_API_READINESS_CAP_SEC", "300")
    monkeypatch.setenv("PINKYBOT_FERRY_ENABLED", "0")
    monkeypatch.setattr(api, "SHARED_MCP_ENABLED", False)
    app = api.create_api(db_path=str(tmp_path / "test.db"), default_working_dir=str(tmp_path))
    run = BootRun(app)
    monkeypatch.setattr(api, "create_api", lambda **kwargs: app)

    class FakeClient:
        def __init__(self, options):
            self.options = options

        async def connect(self):
            run.connections.append("sdk")

        async def get_server_info(self):
            return None

        async def query(self, prompt):
            run.submit("sdk-query", prompt)

        async def disconnect(self):
            pass

    monkeypatch.setattr(claude_agent_sdk, "ClaudeSDKClient", FakeClient)
    monkeypatch.setattr(StreamingSession, "_reader_loop", AsyncMock())
    monkeypatch.setattr(StreamingSession, "_analytics_session_started", Mock())

    async def pane_paste(prompt, **kwargs):
        run.submit("pane-paste", prompt)
        return SimpleNamespace(ok=True, returncode=0, stderr="")

    async def tmux_connect(session):
        run.connections.append("tmux")
        session._state_machine._state = SessionState.CONNECTED
        # SessionStart being ready must not be confused with API readiness.
        session._session_ready_event.set()
        session._tmux.paste_text = pane_paste
        session._config.live_status_fn = lambda: {"status": "idle", "last_updated": time.time()}
        # Replace transcript receipt I/O only, after the real paste edge.
        async def finish_submitted_turn(turn):
            session._resolve_submission_receipt(turn, True)
            session._fire_on_delivered(turn)
            if turn.scheduler_delivery is not None and not turn.scheduler_delivery.done():
                turn.scheduler_delivery.set_result(True)
            session._turn_done.set()

        session._finish_submitted_turn = finish_submitted_turn
        session._worker_task = asyncio.create_task(session._message_worker())
        await session._enqueue_wake_prompt(WakeReason.RESUME)

    async def transport_disconnect(session):
        session._state_machine._state = SessionState.DEAD
        worker = session._worker_task
        if worker is not None:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
        session._worker_task = None

    for cls in (TmuxSession, CodexTmuxSession):
        monkeypatch.setattr(cls, "connect", tmux_connect)
        monkeypatch.setattr(cls, "disconnect", transport_disconnect)

    class ExecInput:
        def write(self, data):
            run.submit("exec-stdin", data.decode())

        async def drain(self):
            pass

        def close(self):
            pass

        async def wait_closed(self):
            pass

    async def spawn_exec(*args, **kwargs):
        stdout = asyncio.StreamReader()
        stdout.feed_eof()
        return SimpleNamespace(
            stdin=ExecInput(), stdout=stdout, stderr=None, returncode=0,
            wait=AsyncMock(return_value=0), kill=Mock(),
        )

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn_exec)

    async def codex_connect(session):
        run.connections.append("codex")
        session._state_machine._state = SessionState.CONNECTED

        class AppClient:
            async def request(self, method, params):
                if method == "turn/start":
                    run.submit("app-server-turn", params["input"][0]["text"])
                    session._turn_done.set_result(None)
                    return {}
                return {"thread": {"id": "sample-thread"}}

            async def close(self):
                pass

        session._ensure_app_server = AsyncMock(return_value=True)
        session._app_client = AppClient()
        session._start_worker()
        await session._enqueue_wake()

    monkeypatch.setattr(CodexSession, "connect", codex_connect)
    monkeypatch.setattr(CodexSession, "disconnect", transport_disconnect)

    class FakeServer:
        def __init__(self, config):
            self.config = config
            self.started = False
            self.should_exit = False
            self.phase = 0
            run.server = self

        def run(self):
            asyncio.run(self.serve())

        async def serve(self):
            async with app.router.lifespan_context(app):
                assert run.connections, "The session must connect before the API bind"
                self.phase = 1
                if run.extra_source:
                    if run.extra_source == "schedule":
                        task = asyncio.create_task(app.state.scheduler._wake_callback(
                            "primary", "", "scheduled replay",
                        ))
                    else:
                        task = asyncio.create_task(app.state.broker.inject_agent_message(
                            "recovery", "primary", "context reload",
                        ))
                    run.tasks.append(task)
                await run.checkpoint()
                if run.bind and not run.stop_before_bind:
                    self.started = True
                    self.phase = READY_PHASE
                    expected = 1 + bool(run.extra_source)
                    await run.wait_for_delivery(expected)
                    if run.manual:
                        endpoint = next(
                            route.endpoint for route in app.routes
                            if getattr(route, "path", "") == "/agents/{agent_name}/wake"
                        )

                        async def unexpected_delay(*args, **kwargs):
                            raise AssertionError("Already-ready manual wake added a delay")

                        with monkeypatch.context() as fast_path:
                            fast_path.setattr(asyncio, "sleep", unexpected_delay)
                            run.manual_result = await endpoint("primary", "manual request")
                elif not run.stop_before_bind:
                    # Keep the listener unavailable beyond the configured cap.
                    # The generous scheduling margin tolerates concurrent suites.
                    await asyncio.sleep(0.15)
                self.should_exit = True
            self.phase = 3
            if run.stop_before_bind:
                # A late flag flip must never release a stopped delivery waiter.
                self.started = True
                await run.checkpoint()
            for task in run.tasks:
                task.cancel()
            await asyncio.gather(*run.tasks, return_exceptions=True)

    monkeypatch.setattr(uvicorn, "Server", FakeServer)
    monkeypatch.setattr(
        uvicorn, "run", lambda application, **kwargs: FakeServer(
            uvicorn.Config(application, **kwargs),
        ).run(),
    )
    return run


def _run_boot(run, tmp_path, monkeypatch, mode):
    runtime = "codex_cli" if mode.startswith("codex") else "claude_sdk"
    transport = "tmux" if mode.endswith("tmux") else "sdk"
    work = tmp_path / "workspace"
    work.mkdir()
    run.app.state.agents.register(
        "primary", runtime=runtime, transport=transport, working_dir=str(work),
    )
    run.app.state.agents.set_main_agent("primary")
    monkeypatch.setenv("PINKY_CODEX_APP_SERVER", "1" if mode == "codex-app-server" else "0")
    daemon_main._run_api_with_authority(SimpleNamespace(
        host="", port=0, working_dir=str(tmp_path), max_sessions=4,
        db_path=str(tmp_path / "test.db"),
    ))


@pytest.mark.parametrize("mode", MODES)
def test_boot_submits_every_prompt_after_main_listener_ready(
    boot_run, tmp_path, monkeypatch, mode,
):
    _run_boot(boot_run, tmp_path, monkeypatch, mode)
    assert boot_run.observed, "A ready listener must release the reserved boot wake"
    assert all(
        phase >= READY_PHASE and ready
        for _, _, phase, ready in boot_run.observed
    ), f"Boot prompt observed before main listener readiness: {boot_run.observed!r}"


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("source", ("broker-recovery", "schedule"))
def test_early_recovery_and_schedule_replay_use_listener_barrier(
    boot_run, tmp_path, monkeypatch, mode, source,
):
    boot_run.extra_source = source
    _run_boot(boot_run, tmp_path, monkeypatch, mode)
    expected = "scheduled replay" if source == "schedule" else "context reload"
    replay = [entry for entry in boot_run.observed if expected in entry[1]]
    assert replay, "A ready listener must release the replay, not lose it"
    assert all(phase >= READY_PHASE and ready for _, _, phase, ready in replay), (
        f"Replay prompt observed before main listener readiness: {replay!r}"
    )


@pytest.mark.parametrize("mode", MODES)
def test_listener_timeout_refuses_held_prompt_delivery(
    boot_run, tmp_path, monkeypatch, mode,
):
    boot_run.bind = False
    monkeypatch.setenv("PINKY_API_READINESS_CAP_SEC", "0.03")
    _run_boot(boot_run, tmp_path, monkeypatch, mode)
    assert boot_run.observed == [], (
        f"Unavailable API listener must refuse held prompts: {boot_run.observed!r}"
    )


def test_listener_timeout_reports_a_loud_delivery_refusal(
    boot_run, tmp_path, monkeypatch, capsys, caplog,
):
    boot_run.bind = False
    monkeypatch.setenv("PINKY_API_READINESS_CAP_SEC", "0.03")
    _run_boot(boot_run, tmp_path, monkeypatch, "claude-sdk")
    output = capsys.readouterr().err + caplog.text
    assert re.search(
        r"(?is)error.*api.*(?:listener|readiness).*"
        r"(?:timed out|timeout|unavailable|not ready).*(?:refus|drop|not submit)", output,
    ), "Listener readiness timeout must log an ERROR explaining delivery refusal"


@pytest.mark.parametrize("mode", MODES)
def test_shutdown_cancels_waiting_prompts_before_a_late_ready_flag(
    boot_run, tmp_path, monkeypatch, mode,
):
    boot_run.stop_before_bind = True
    _run_boot(boot_run, tmp_path, monkeypatch, mode)
    assert boot_run.observed == [], (
        f"Shutdown must cancel held prompts; none may survive stop: {boot_run.observed!r}"
    )


def test_post_startup_manual_wake_handler_has_no_readiness_delay(
    boot_run, tmp_path, monkeypatch,
):
    boot_run.manual = True
    _run_boot(boot_run, tmp_path, monkeypatch, "claude-sdk")
    manual = [entry for entry in boot_run.observed if entry[1] == "manual request"]
    assert manual == [("sdk-query", "manual request", READY_PHASE, True)]
    assert boot_run.manual_result["sent"] is True
