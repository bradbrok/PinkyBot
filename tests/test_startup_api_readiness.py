"""Model prompt delivery requires the main API listener, including during boot."""

from __future__ import annotations

import asyncio
import json
import re
import time
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
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
    approved_backlog: bool = False
    pending_before_stop: list[dict] = field(default_factory=list)
    surviving_tasks: list[str] = field(default_factory=list)
    late_bind: bool = False
    observed_before_late_bind: list = field(default_factory=list)
    post_cap_handoff: object = None
    replay_phases: list[int] = field(default_factory=list)
    start_manifest: bool = False
    manifest_before_stop: object = None
    entry_branch: str = "single"
    embedder: bool = False
    serve_early: str = ""
    embedder_run: object = None
    ferry_started_before_main: bool = False
    before_bind_hook: object = None
    after_ready_hook: object = None

    def submit(self, edge, prompt):
        self.observed.append((edge, prompt, self.server.phase, self.server.started))

    async def checkpoint(self):
        # This is a causal pre-bind window, not a wall-clock ordering guess.
        for _ in range(64):
            await asyncio.sleep(0)

    async def wait_for_delivery(self, count):
        try:
            async with asyncio.timeout(5):
                while len(self.observed) < count:
                    await asyncio.sleep(0.001)
        except TimeoutError:
            raise AssertionError("A ready listener did not release the expected prompts") from None


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
    original_migration_replay = api._resume_grandfather_migration

    async def record_migration_replay(*args, **kwargs):
        run.replay_phases.append(run.server.phase)
        return await original_migration_replay(*args, **kwargs)

    monkeypatch.setattr(api, "_resume_grandfather_migration", record_migration_replay)

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
            if config.app is app:
                run.server = self

        async def startup(self):
            self.started = True

        def run(self):
            asyncio.run(self.serve())

        async def serve(self):
            if self.config.app is not app:
                await self.startup()
                run.ferry_started_before_main = not run.server.started
                while not run.server.should_exit:
                    await asyncio.sleep(0)
                self.should_exit = True
                return
            if run.serve_early:
                if run.serve_early == "raise":
                    raise RuntimeError("server startup failed")
                return
            async with AsyncExitStack() as stack:
                # A delivery waiter inside foreground startup must not hold
                # the fake listener's bind hostage for the default five minutes.
                async with asyncio.timeout(5):
                    await stack.enter_async_context(app.router.lifespan_context(app))
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
                if run.before_bind_hook is not None:
                    await run.before_bind_hook()
                if run.late_bind:
                    await asyncio.sleep(0.15)
                    run.observed_before_late_bind = list(run.observed)
                    session = app.state.broker._get_streaming_session("primary")
                    async with asyncio.timeout(0.1):
                        run.post_cap_handoff = await session.send("new before late ready")
                    assert run.observed == [], "Post-cap delivery escaped an unavailable listener"
                if run.bind and not run.stop_before_bind:
                    self.started = True
                    self.phase = READY_PHASE
                    expected = (0 if run.late_bind else 1) + bool(run.extra_source) + run.approved_backlog
                    await run.wait_for_delivery(expected)
                    if run.after_ready_hook is not None:
                        await run.after_ready_hook()
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
                if run.approved_backlog:
                    run.pending_before_stop = app.state.agents.get_pending_messages("primary")
                if run.start_manifest:
                    manifest = Path(app.state.agents._db_path).parent / "restart_manifest.json"
                    run.manifest_before_stop = json.loads(manifest.read_text()) if manifest.exists() else None
                self.should_exit = True
            self.phase = 3
            if run.stop_before_bind:
                # A late flag flip must never release a stopped delivery waiter.
                self.started = True
                await run.checkpoint()
            for task in run.tasks:
                task.cancel()
            await asyncio.gather(*run.tasks, return_exceptions=True)
            await run.checkpoint()
            run.surviving_tasks = [
                task.get_name() for task in asyncio.all_tasks()
                if task is not asyncio.current_task() and not task.done()
            ]

    monkeypatch.setattr(uvicorn, "Server", FakeServer)
    monkeypatch.setattr(
        uvicorn, "run", lambda application, **kwargs: FakeServer(
            uvicorn.Config(application, **kwargs),
        ).run(),
    )
    run.embedder_run = lambda: FakeServer(uvicorn.Config(app)).run()
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
    if run.approved_backlog:
        run.app.state.agents.approve_user("primary", "sample-chat", display_name="sender")
        run.app.state.agents.queue_pending_message(
            "primary", "web", "sample-chat", "sender", "approved inbound replay",
        )
    if run.start_manifest:
        (tmp_path / "restart_manifest.json").write_text(json.dumps({
            "restart_time": datetime.now(timezone.utc).isoformat(),
            "agents": {"primary": {"in_progress": "reserved activity", "label": "main"}},
        }))
    if run.entry_branch != "single":
        from pinky_daemon.ferry import inbound_server
        from pinky_daemon.ferry.config import FerryConfig

        monkeypatch.setattr(FerryConfig, "from_env", lambda: SimpleNamespace(
            enabled=True, bind_host="", bind_port=0, fleet_name="sample",
        ))
        monkeypatch.setattr(inbound_server, "build_ferry_app", lambda **kwargs: object())
        if run.entry_branch == "fallback":
            run.app.state.host_pinky = None
    monkeypatch.setenv("PINKY_CODEX_APP_SERVER", "1" if mode == "codex-app-server" else "0")
    if run.embedder:
        run.embedder_run()
        return
    try:
        daemon_main._run_api_with_authority(SimpleNamespace(
            host="", port=0, working_dir=str(tmp_path), max_sessions=4,
            db_path=str(tmp_path / "test.db"),
        ))
    except SystemExit as failure:
        if failure.code != 3 or not (not run.bind or run.serve_early == "return"):
            raise


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
    assert boot_run.surviving_tasks == [], "Shutdown must join listener and delivery waiters"


def test_startup_approved_backlog_flush_waits_for_listener_without_blocking_bind(
    boot_run, tmp_path, monkeypatch,
):
    boot_run.approved_backlog = True
    _run_boot(boot_run, tmp_path, monkeypatch, "claude-sdk")
    replay = [entry for entry in boot_run.observed if "approved inbound replay" in entry[1]]
    assert replay, "The approved startup backlog must be replayed after binding"
    assert all(phase >= READY_PHASE and ready for _, _, phase, ready in replay), (
        f"Approved startup replay observed before main listener readiness: {replay!r}"
    )


def test_listener_timeout_preserves_unsubmitted_approved_backlog(
    boot_run, tmp_path, monkeypatch,
):
    boot_run.approved_backlog = True
    boot_run.bind = False
    monkeypatch.setenv("PINKY_API_READINESS_CAP_SEC", "0.03")
    _run_boot(boot_run, tmp_path, monkeypatch, "claude-sdk")
    assert len(boot_run.pending_before_stop) == 1, (
        "Listener timeout must preserve the durable inbound row for a later retry"
    )
    assert boot_run.observed == [], "An unavailable listener must not accept backlog delivery"


def test_post_startup_manual_wake_handler_has_no_readiness_delay(
    boot_run, tmp_path, monkeypatch,
):
    boot_run.manual = True
    _run_boot(boot_run, tmp_path, monkeypatch, "claude-sdk")
    manual = [entry for entry in boot_run.observed if entry[1] == "manual request"]
    assert manual == [("sdk-query", "manual request", READY_PHASE, True)]
    assert boot_run.manual_result["sent"] is True


def test_timeout_then_late_ready_admits_only_new_prompts_and_flushes_backlog_once(
    boot_run, tmp_path, monkeypatch, capsys,
):
    boot_run.late_bind = True
    boot_run.approved_backlog = True
    boot_run.manual = True
    monkeypatch.setenv("PINKY_API_READINESS_CAP_SEC", "0.03")
    _run_boot(boot_run, tmp_path, monkeypatch, "claude-sdk")
    assert boot_run.observed_before_late_bind == []
    assert boot_run.post_cap_handoff is False
    assert len(boot_run.observed) == 2, "Refused boot wakes must never release on late readiness"
    assert "approved inbound replay" in boot_run.observed[0][1]
    assert boot_run.observed[1][1] == "manual request"
    assert all(phase == READY_PHASE and ready for _, _, phase, ready in boot_run.observed)
    assert boot_run.replay_phases == [READY_PHASE], "Deferred startup jobs must run exactly once"
    assert boot_run.pending_before_stop == []
    output = capsys.readouterr().err
    assert re.search(r"WARNING.*ready after cap.*elapsed=", output)
    assert "ERROR" in output and "source=" in output and "refused_count=" in output


@pytest.mark.parametrize("branch", ("fallback", "dual"))
def test_each_daemon_entry_branch_waits_for_the_main_listener(
    boot_run, tmp_path, monkeypatch, branch,
):
    boot_run.entry_branch = branch
    _run_boot(boot_run, tmp_path, monkeypatch, "claude-sdk")
    assert boot_run.observed
    assert all(phase >= READY_PHASE and ready for _, _, phase, ready in boot_run.observed)
    assert boot_run.app.state.api_readiness.attached
    if branch == "dual":
        assert boot_run.ferry_started_before_main, "The ferry must be ready during the pre-bind window"


@pytest.mark.parametrize("exit_kind", ("return", "raise"))
def test_main_serve_completion_or_error_is_terminal_despite_late_started_flag(
    boot_run, tmp_path, monkeypatch, capsys, exit_kind,
):
    boot_run.serve_early = exit_kind
    if exit_kind == "raise":
        with pytest.raises(RuntimeError, match="server startup failed"):
            _run_boot(boot_run, tmp_path, monkeypatch, "claude-sdk")
    else:
        _run_boot(boot_run, tmp_path, monkeypatch, "claude-sdk")
    gate = boot_run.app.state.api_readiness
    assert not boot_run.server.should_exit, "Completion must be observed independently of should_exit"
    assert asyncio.run(gate.wait("direct-probe")) is False
    boot_run.server.started = True
    assert asyncio.run(gate.wait("late-probe")) is False
    assert "ERROR" in capsys.readouterr().err


@pytest.mark.parametrize("kind", ("missing", "null"))
def test_daemon_refuses_to_start_without_a_readiness_barrier(tmp_path, monkeypatch, capsys, kind):
    import uvicorn

    state = SimpleNamespace()
    if kind == "null":
        state.api_readiness = None
    monkeypatch.setattr(api, "create_api", lambda **kwargs: SimpleNamespace(state=state))
    server = Mock()
    monkeypatch.setattr(uvicorn, "Server", server)
    args = SimpleNamespace(
        host=None, port=0, working_dir=str(tmp_path), max_sessions=1,
        db_path=str(tmp_path / "test.db"),
    )
    with pytest.raises(RuntimeError, match="daemon API readiness barrier missing"):
        daemon_main._run_api(args)
    server.assert_not_called()
    assert "ERROR daemon API readiness barrier missing; refusing to start" in capsys.readouterr().err


@pytest.mark.parametrize("mode", MODES)
def test_refused_boot_wake_preserves_one_shot_restart_manifest(
    boot_run, tmp_path, monkeypatch, mode,
):
    boot_run.start_manifest = True
    boot_run.bind = False
    monkeypatch.setenv("PINKY_API_READINESS_CAP_SEC", "0.03")
    _run_boot(boot_run, tmp_path, monkeypatch, mode)
    assert boot_run.observed == []
    assert boot_run.manifest_before_stop is not None, "A refused wake consumed its manifest"
    assert boot_run.manifest_before_stop["agents"]["primary"]["in_progress"] == "reserved activity"


def test_embedder_without_listener_owner_keeps_immediate_delivery(
    boot_run, tmp_path, monkeypatch,
):
    boot_run.embedder = True
    _run_boot(boot_run, tmp_path, monkeypatch, "claude-sdk")
    assert not boot_run.app.state.api_readiness.attached
    assert boot_run.observed and all(phase < READY_PHASE for _, _, phase, _ in boot_run.observed)


@pytest.mark.parametrize("mode", ("claude-tmux", "codex-tmux"))
def test_tmux_pending_inbound_row_survives_listener_timeout(
    boot_run, tmp_path, monkeypatch, mode,
):
    boot_run.approved_backlog = True
    boot_run.bind = False
    monkeypatch.setenv("PINKY_API_READINESS_CAP_SEC", "0.03")
    _run_boot(boot_run, tmp_path, monkeypatch, mode)
    assert len(boot_run.pending_before_stop) == 1
    assert boot_run.replay_phases == []
    assert boot_run.observed == []


@pytest.mark.parametrize("use_app_server", (False, True))
@pytest.mark.asyncio
async def test_direct_idle_save_exec_waits_without_a_message_worker(
    boot_run, tmp_path, monkeypatch, use_app_server,
):
    from pinky_daemon.api_readiness import ApiReadiness
    from pinky_daemon.streaming_session import StreamingSessionConfig

    monkeypatch.setenv("PINKY_CODEX_APP_SERVER", "1" if use_app_server else "0")
    gate = ApiReadiness()
    server = SimpleNamespace(started=False, should_exit=False, phase=1)
    boot_run.server = server
    gate.attach(server)
    gate.start()
    session = CodexSession(StreamingSessionConfig(
        agent_name="primary", working_dir=str(tmp_path), api_readiness=gate,
    ))

    class Client:
        async def request(self, method, params):
            if method == "turn/start":
                boot_run.submit("direct-turn", params["input"][0]["text"])
                session._turn_done.set_result(None)
                return {}
            return {"thread": {"id": "sample-thread"}}

    session._app_client = Client()
    session._ensure_app_server = AsyncMock(return_value=True)
    task = asyncio.create_task(session._exec_codex("direct idle save"))
    try:
        await boot_run.checkpoint()
        assert boot_run.observed == [], "Direct idle-save exec bypassed API readiness"
        assert not task.done(), "A held direct prompt must await readiness rather than disappear"
        server.started = True
        server.phase = READY_PHASE
        async with asyncio.timeout(2):
            result = await task
        assert not result.failed
        assert len(boot_run.observed) == 1
        assert boot_run.observed[0][1:] == ("direct idle save", READY_PHASE, True)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await gate.close()
