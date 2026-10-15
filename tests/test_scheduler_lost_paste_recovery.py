"""Lost-paste notices follow delivered context and survive failed handoffs."""

import asyncio
import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from pinky_daemon import api, tmux_session
from pinky_daemon.tmux_session import TmuxSession
from pinky_daemon.transport_state import SessionState
from pinky_daemon.wake_prompt import WakeReason
from tests.test_scheduler_lost_paste_alerts import FIRED, NOW
from tests.test_scheduler_lost_paste_alerts import api_session as api_session
from tests.test_scheduler_lost_paste_alerts import rig as rig


async def drain_notices(engine):
    while tasks := (set(engine._owner_alert_tasks)
                    | set(getattr(engine, "_agent_notice_tasks", ()))):
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=2)


def api_paste(app, *, pasted_at, name="periodic"):
    registry = app.state.agents
    schedule = registry.add_schedule("worker", "0 * * * *", name=name, prompt="check work")
    row, created = registry.persist_schedule_wake(
        schedule.id, agent_name="worker", schedule_name=name,
        prompt=schedule.prompt, fired_at=FIRED,
    )
    assert created
    assert registry.mark_schedule_wake_pasted(
        row.schedule_id, row.fired_at, pasted_at=pasted_at,
        session_id="previous-process", pasted_prompt=row.prompt,
    )
    return row


@pytest.fixture
def wake_transport(api_session, monkeypatch):
    app, previous, clock, _ = api_session
    monkeypatch.setattr(tmux_session, "time", SimpleNamespace(
        **{**vars(tmux_session.time), "time": lambda: clock.now},
    ))
    ss = TmuxSession(previous._config)
    ss._state_machine._state = SessionState.CONNECTED
    ss._config.on_wake_delivered = None
    app.state.broker._streaming["worker"]["main"] = ss
    queued = []

    async def enqueue(prompt, **kwargs):
        queued.append(SimpleNamespace(prompt=prompt, delivered=kwargs["on_delivered"]))

    monkeypatch.setattr(ss, "_enqueue_internal_prompt", enqueue)
    yield app, ss, clock, queued


def deliver(turn):
    if turn.delivered is not None:
        turn.delivered()


async def test_building_wake_text_does_not_claim_delivery(wake_transport):
    app, ss, clock, queued = wake_transport
    await ss._enqueue_wake_prompt(WakeReason.CONTEXT_RESTART)
    assert app.state.scheduler._delivery_context_rendered_at_fn("worker") == 0
    clock.now += 5
    deliver(queued[0])
    assert app.state.scheduler._delivery_context_rendered_at_fn("worker") == NOW


async def test_committed_probe_never_replaces_fallback_preview(wake_transport):
    app, ss, clock, queued = wake_transport
    app.state.agents.register("worker", watchdog_config={"mcp_recover": True})
    preview = ss._config.wake_context
    clock.now += 1
    await ss._enqueue_wake_prompt(WakeReason.CONTEXT_RESTART)
    assert "MCP BIND CHECK" in queued[0].prompt
    assert ss._config.wake_context == preview

    def failed_builder(*args, **kwargs):
        raise RuntimeError("rebuild unavailable")

    ss._config.wake_context_builder = failed_builder
    clock.now += 1
    await ss._enqueue_wake_prompt(WakeReason.CONTEXT_RESTART)
    assert "MCP BIND CHECK" not in queued[1].prompt
    deliver(queued[1])
    assert app.state.scheduler._delivery_context_rendered_at_fn("worker") == NOW


async def test_preview_after_failed_restart_keeps_delivered_snapshot(
    wake_transport, monkeypatch,
):
    app, ss, clock, queued = wake_transport
    monkeypatch.setenv("PINKY_RESUME_FAILSAFE", "1")
    await ss._enqueue_wake_prompt(WakeReason.CONTEXT_RESTART)
    deliver(queued[0])
    row = api_paste(app, pasted_at=NOW + 1)
    clock.now = NOW + 2
    app.state.agents.set_context(
        "worker", task="continue work", metadata={"source": "save_my_context"},
        updated_by=ss.resume_handle,
    )
    owner, notice = AsyncMock(return_value=True), AsyncMock(return_value=True)
    engine = app.state.scheduler
    engine._owner_notify_callback, engine._agent_notify_callback = owner, notice

    async def configure_only(**kwargs):
        await ss._run_replacement_step(kwargs["configure"])

    monkeypatch.setattr(ss, "restart_transport", configure_only)
    original = app.state.agents.set_streaming_session_id

    def refuse_write(name, value, **kwargs):
        if value == "":
            raise RuntimeError("session update refused")
        return original(name, value, **kwargs)

    monkeypatch.setattr(app.state.agents, "set_streaming_session_id", refuse_write)
    endpoint = next(r.endpoint for r in app.routes
                    if getattr(r, "path", "") == "/agents/{name}/streaming/restart")
    scope = {"type": "http"}
    await endpoint("worker", SimpleNamespace(scope=scope))
    scope["pinky.response_sent_handoff"]["schedule"]()
    await asyncio.gather(*app.state._context_restart_tasks)
    assert ss.state == SessionState.CONNECTED
    assert app.state.scheduler._delivery_context_rendered_at_fn("worker") == NOW
    engine._settle_lost_pastes("worker")
    await drain_notices(engine)
    assert app.state.agents.get_schedule_wake_by_fire(row.schedule_id, FIRED).abandoned_at
    owner.assert_not_awaited()
    notice.assert_awaited_once()


async def test_new_process_drops_earlier_delivery_and_late_callback(wake_transport, monkeypatch):
    app, ss, clock, queued = wake_transport
    await ss._enqueue_wake_prompt(WakeReason.CONTEXT_RESTART)
    deliver(queued[0])
    assert app.state.scheduler._delivery_context_rendered_at_fn("worker") == NOW
    ss._state_machine._state = SessionState.DEAD
    monkeypatch.setattr(ss, "_spawn_tmux_repl", AsyncMock())
    monkeypatch.setattr(ss, "_message_worker", AsyncMock())
    monkeypatch.setattr(ss, "_inflight_watchdog", AsyncMock())
    clock.now += 10
    await ss.connect()
    assert app.state.scheduler._delivery_context_rendered_at_fn("worker") == 0
    deliver(queued[0])
    assert app.state.scheduler._delivery_context_rendered_at_fn("worker") == 0
    deliver(queued[1])
    assert app.state.scheduler._delivery_context_rendered_at_fn("worker") == NOW + 10
    await asyncio.gather(ss._worker_task, ss._watchdog_task)


@pytest.mark.parametrize("path", ["direct", "pending", "reaper"])
async def test_restart_reservation_defers_every_settlement_path(api_session, path):
    app, ss, clock, _ = api_session
    row = api_paste(app, pasted_at=NOW - 1)
    app.state.agents.set_context(
        "worker", task="continue work", metadata={"source": "save_my_context"},
        updated_by=ss.resume_handle,
    )
    endpoint = next(r.endpoint for r in app.routes
                    if getattr(r, "path", "") == "/agents/{name}/streaming/restart")
    scope = {"type": "http"}
    await endpoint("worker", SimpleNamespace(scope=scope))
    engine = app.state.scheduler
    owner, notice = AsyncMock(return_value=True), AsyncMock(return_value=True)
    engine._owner_notify_callback, engine._agent_notify_callback = owner, notice
    ss._state_machine._state = SessionState.DEAD
    try:
        if path == "direct":
            engine._settle_lost_pastes("worker")
        elif path == "pending":
            engine._check_pending_wake_liveness(clock.now - 61)
            engine._check_pending_wake_liveness(clock.now)
            assert engine._pending_replay_tasks
            await asyncio.gather(*engine._pending_replay_tasks.values())
        else:
            engine._run_outbox_reaper_if_due(clock.now)
        await drain_notices(engine)
        saved = app.state.agents.get_schedule_wake_by_fire(row.schedule_id, FIRED)
        assert saved is not None and saved.abandoned_at == 0
        owner.assert_not_awaited()
        notice.assert_not_awaited()
    finally:
        scope["pinky.response_sent_handoff"]["abandon"]()


async def test_restart_reservation_deferral_has_a_cap(api_session, monkeypatch):
    app, ss, clock, _ = api_session
    monkeypatch.setattr(api, "time", SimpleNamespace(
        **{**vars(api.time), "monotonic": lambda: clock.now},
    ))
    row = api_paste(app, pasted_at=NOW - 1)
    app.state.agents.set_context(
        "worker", task="continue work", metadata={"source": "save_my_context"},
        updated_by=ss.resume_handle,
    )
    endpoint = next(r.endpoint for r in app.routes
                    if getattr(r, "path", "") == "/agents/{name}/streaming/restart")
    scope = {"type": "http"}
    await endpoint("worker", SimpleNamespace(scope=scope))
    ss._state_machine._state = SessionState.DEAD
    engine = app.state.scheduler
    owner = AsyncMock(return_value=True)
    engine._owner_notify_callback = owner
    try:
        cap = getattr(api, "SCHEDULER_REPLACEMENT_DEFER_MAX_SECONDS", 300.0)
        clock.now += cap - 0.01
        engine._settle_lost_pastes("worker")
        assert app.state.agents.get_schedule_wake_by_fire(row.schedule_id, FIRED).abandoned_at == 0
        clock.now += 0.02
        engine._settle_lost_pastes("worker")
        await drain_notices(engine)
        assert app.state.agents.get_schedule_wake_by_fire(row.schedule_id, FIRED).abandoned_at
        owner.assert_awaited_once()
    finally:
        scope["pinky.response_sent_handoff"]["abandon"]()


@pytest.mark.parametrize("failure", ["missing", "false", "exception"])
async def test_failed_agent_handoff_alerts_owner_once_in_plain_words(rig, failure):
    row = rig.pasted("morning check")
    rig.registry.set_owner_profile({"timezone": "Europe/Paris"})
    engine, wake = rig.engine(rendered=0, timezone="America/New_York")
    engine._agent_notify_callback = {
        "missing": None,
        "false": AsyncMock(return_value=False),
        "exception": AsyncMock(side_effect=RuntimeError("delivery failed")),
    }[failure]
    for _ in range(2):
        engine._settle_lost_pastes("worker")
        await drain_notices(engine)
    saved = rig.registry.get_schedule_wake_by_fire(row.schedule_id, FIRED)
    assert saved.abandoned_at and saved.accepted_at == 0
    assert rig.owners == [("worker", "Scheduled prompt 'morning check' for worker (fired "
                          "2023-11-14 23:13 CET) may not have run, and worker could not be "
                          "told; check before redoing.")]
    text = rig.owners[0][1]
    assert "went offline" not in text and "PASTED_UNCONFIRMED" not in text
    assert "fired_at=" not in text and str(int(FIRED)) not in text
    wake.assert_not_awaited()


async def test_session_drops_before_queued_agent_notice_runs(api_session):
    app, ss, _, sent = api_session
    api_paste(app, pasted_at=NOW + 1)
    engine = app.state.scheduler
    owner = AsyncMock(return_value=True)
    engine._owner_notify_callback = owner
    engine._settle_lost_pastes("worker")
    ss._state_machine._state = SessionState.DEAD
    await drain_notices(engine)
    engine._settle_lost_pastes("worker")
    await drain_notices(engine)
    owner.assert_awaited_once()
    assert "could not be told" in owner.call_args.args[1]
    assert "went offline" not in owner.call_args.args[1]
    assert sent == []


async def test_infinite_snapshot_is_unknown(rig):
    rig.pasted()
    engine, _ = rig.engine(rendered=float("inf"))
    engine._settle_lost_pastes("worker")
    await drain_notices(engine)
    assert len(rig.notices) == 1 and rig.owners == []


async def test_stop_waits_for_inflight_agent_notice(rig):
    rig.pasted()
    engine, _ = rig.engine()
    started, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def handoff(*args):
        started.set()
        await release.wait()
        finished.set()
        return True

    engine._agent_notify_callback = handoff
    engine._settle_lost_pastes("worker")
    await asyncio.wait_for(started.wait(), timeout=2)
    notices = set(engine._agent_notice_tasks)
    stopping = asyncio.create_task(engine.stop())
    await asyncio.sleep(0)
    try:
        assert not stopping.done()
    finally:
        release.set()
        await asyncio.wait_for(asyncio.gather(*notices), timeout=2)
        await asyncio.wait_for(stopping, timeout=2)
    assert finished.is_set() and not engine._agent_notice_tasks


@pytest.mark.parametrize("lookup", ["agent", "owner_with_agent", "owner_without_agent"])
async def test_timezone_lookup_exceptions_keep_recipient_clock(rig, monkeypatch, lookup):
    rig.pasted()
    rig.registry.set_owner_profile({"timezone": "Europe/Paris"})
    engine, _ = rig.engine(timezone="America/New_York" if lookup == "owner_with_agent" else "")

    def unavailable(*args):
        raise RuntimeError("clock unavailable")

    if lookup == "agent":
        engine._delivery_timezone_fn = unavailable
        expected = "2023-11-14 23:13 CET"
    else:
        monkeypatch.setattr(rig.registry, "get_owner_profile", unavailable)
        expected = ("2023-11-14 17:13 EST" if lookup == "owner_with_agent"
                    else "2023-11-14 22:13 UTC")
    engine._settle_lost_pastes("worker")
    await drain_notices(engine)
    assert len(rig.notices) == 1 and expected in rig.notices[0][1]


@pytest.mark.parametrize("mode", ["committed", "fallback", "failed_delivery"])
async def test_sdk_records_only_the_snapshot_its_query_delivered(api_session, monkeypatch, mode):
    import claude_agent_sdk

    app, ss, clock, _ = api_session
    preview_time = ss._config.wake_context_rendered_at
    entered, release = asyncio.Event(), asyncio.Event()

    async def query(*args, **kwargs):
        entered.set()
        await release.wait()
        if mode == "failed_delivery":
            raise RuntimeError("query refused")

    stub = SimpleNamespace(connect=AsyncMock(), query=query,
                           get_server_info=AsyncMock(return_value={}))
    monkeypatch.setattr(claude_agent_sdk, "ClaudeSDKClient", lambda options: stub)
    if mode == "fallback":
        def unavailable(*args, **kwargs):
            raise RuntimeError("rebuild unavailable")
        ss._config.wake_context_builder = unavailable
    ss._state_machine._state = SessionState.DEAD
    clock.now = NOW + 10
    await ss.connect()
    await asyncio.wait_for(entered.wait(), timeout=2)
    assert app.state.scheduler._delivery_context_rendered_at_fn("worker") == 0
    clock.now += 5
    release.set()
    await asyncio.wait_for(ss._initial_wake_task, timeout=2)
    expected = (0 if mode == "failed_delivery" else
                preview_time if mode == "fallback" else NOW + 10)
    assert app.state.scheduler._delivery_context_rendered_at_fn("worker") == expected


@pytest.mark.parametrize("mode", ["committed", "fallback", "failed_delivery"])
async def test_codex_records_snapshot_after_successful_exec_only(api_session, monkeypatch, mode):
    from pinky_daemon import codex_session
    from pinky_daemon.codex_session import CodexSession, CodexTurnResult

    app, original, clock, _ = api_session
    monkeypatch.setattr(codex_session, "time", SimpleNamespace(
        **{**vars(codex_session.time), "time": lambda: clock.now},
    ))
    ss = CodexSession(original._config)
    app.state.broker._streaming["worker"]["main"] = ss
    monkeypatch.setattr(ss, "_bring_up_substrate", AsyncMock())
    monkeypatch.setattr(ss, "_start_worker", lambda: None)
    entered, release, idle = asyncio.Event(), asyncio.Event(), asyncio.Event()
    ss._config.on_turn_idle = lambda *_: idle.set()
    ss._config.on_wake_delivered = None
    preview_time = ss._config.wake_context_rendered_at
    if mode == "fallback":
        def unavailable(*args, **kwargs):
            raise RuntimeError("rebuild unavailable")
        ss._config.wake_context_builder = unavailable

    async def execute(*args, **kwargs):
        entered.set()
        await release.wait()
        return CodexTurnResult(failed=mode == "failed_delivery")

    monkeypatch.setattr(ss, "_exec_codex", execute)
    clock.now = NOW + 10
    await ss.connect()
    old_callback = ss._pending_wake_callback
    worker = asyncio.create_task(ss._message_worker())
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        assert app.state.scheduler._delivery_context_rendered_at_fn("worker") == 0
        clock.now += 5
        release.set()
        await asyncio.wait_for(idle.wait(), timeout=2)
        expected = (0 if mode == "failed_delivery" else
                    preview_time if mode == "fallback" else NOW + 10)
        assert app.state.scheduler._delivery_context_rendered_at_fn("worker") == expected
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
    ss._state_machine._state = SessionState.DEAD
    await ss.connect()
    assert app.state.scheduler._delivery_context_rendered_at_fn("worker") == 0
    if old_callback is not None:
        old_callback()
    assert app.state.scheduler._delivery_context_rendered_at_fn("worker") == 0


def api_function(app, name):
    pending = [r.endpoint for r in app.routes if hasattr(r, "endpoint")]
    pending += [app.state.broker._ensure_session_callback]
    pending += [app.state.scheduler._heartbeat_callback]
    pending += list(vars(app.state.watchdog).values())
    visited = set()
    while pending:
        value = pending.pop()
        if id(value) in visited:
            continue
        visited.add(id(value))
        if inspect.isfunction(value):
            if value.__name__ == name:
                return value
            if value.__closure__:
                pending.extend(cell.cell_contents for cell in value.__closure__)
            if hasattr(value, "__wrapped__"):
                pending.append(value.__wrapped__)
        elif type(value).__name__ in {"ContainerLifecycle", "ContainerLifecycleDeps"}:
            pending.extend(vars(value).values())
    raise AssertionError(f"API helper {name} is unreachable")


@pytest.mark.parametrize("path", [
    "retained", "ensure", "rebuild", "watchdog", "container", "skills", "heartbeat", "start",
])
@pytest.mark.parametrize("ending", ["success", "exception", "cancel"])
async def test_api_replacement_paths_share_bound_and_release_marker(
    api_session, monkeypatch, path, ending,
):
    app, ss, clock, _ = api_session
    monkeypatch.setattr(api, "time", SimpleNamespace(
        **{**vars(api.time), "monotonic": lambda: clock.now},
    ))
    row = api_paste(app, pasted_at=NOW - 1)
    entered, release = asyncio.Event(), asyncio.Event()
    engine = app.state.scheduler
    owner = AsyncMock(return_value=True)
    engine._owner_notify_callback = owner

    async def replace(**kwargs):
        ss._state_machine._state = SessionState.DEAD
        entered.set()
        await release.wait()
        if ending == "exception":
            raise RuntimeError("replacement failed")
        ss._state_machine._state = SessionState.CONNECTED

    monkeypatch.setattr(ss, "restart_transport", replace)
    if path == "retained":
        operation = api_function(app, "_restart_compatible")(
            "worker", ss, configure=lambda: None, reason="context_restart",
        )
    elif path == "ensure":
        ss._state_machine._state = SessionState.DEAD
        monkeypatch.setattr(ss, "connect", replace)
        operation = app.state.broker._ensure_session_callback("worker")
    elif path == "rebuild":
        operation = api_function(app, "_replace_session_locked")(
            "worker", "main", ss, reason="model_change",
        )
    elif path == "watchdog":
        operation = api_function(app, "_watchdog_recover")("worker", "main", "reconnect")
    elif path == "skills":
        app.state.agents.set_context(
            "worker", task="continue work", metadata={"source": "save_my_context"},
            updated_by=ss.resume_handle,
        )
        monkeypatch.setattr(ss, "disconnect", replace)
        operation = api_function(app, "apply_agent_skills")("worker")
    elif path == "heartbeat":
        ss._state_machine._state = SessionState.DEAD
        monkeypatch.setattr(ss, "attempt_reconnect", replace)
        operation = api_function(app, "_heartbeat_resurrect")("worker", "")
    elif path == "start":
        from pinky_daemon.streaming_session import StreamingSession

        async def connect_candidate(candidate):
            await replace()
            candidate._state_machine._state = SessionState.CONNECTED

        monkeypatch.setattr(StreamingSession, "connect", connect_candidate)
        operation = api_function(app, "_start_streaming_session")("worker", label="main")
    else:
        async def container_apply():
            async with api_function(app, "_container_replacement_scope")("worker"):
                await replace()
        operation = container_apply()
    task = asyncio.create_task(operation)
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        assert engine._delivery_replacement_pending_fn("worker") is True
        engine._settle_lost_pastes("worker")
        assert app.state.agents.get_schedule_wake_by_fire(row.schedule_id, FIRED).abandoned_at == 0
        owner.assert_not_awaited()
        clock.now += api.SCHEDULER_REPLACEMENT_DEFER_MAX_SECONDS + 0.01
        assert engine._delivery_replacement_pending_fn("worker") is False
        engine._settle_lost_pastes("worker")
        await drain_notices(engine)
        owner.assert_awaited_once()
    finally:
        release.set()
        if ending == "cancel":
            task.cancel()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), timeout=2)
        if engine._pending_replay_tasks:
            await asyncio.gather(*engine._pending_replay_tasks.values(), return_exceptions=True)
        await drain_notices(engine)
    reservations = inspect.getclosurevars(
        engine._delivery_replacement_pending_fn,
    ).nonlocals["_scheduler_replacements"]
    assert "worker" not in reservations


async def test_stop_also_waits_for_owner_fallback_created_by_agent_failure(rig):
    rig.pasted()
    engine, _ = rig.engine()
    entered, release = asyncio.Event(), asyncio.Event()
    agent_started, agent_release = asyncio.Event(), asyncio.Event()

    async def agent(*args):
        agent_started.set()
        await agent_release.wait()
        return False

    async def owner(*args):
        entered.set()
        await release.wait()
        return True

    engine._owner_notify_callback = owner
    engine._agent_notify_callback = agent
    engine._settle_lost_pastes("worker")
    await asyncio.wait_for(agent_started.wait(), timeout=2)
    notices = set(engine._agent_notice_tasks)
    stopping = asyncio.create_task(engine.stop())
    try:
        await asyncio.sleep(0)
        agent_release.set()
        await asyncio.wait_for(asyncio.gather(*notices), timeout=2)
        await asyncio.wait_for(entered.wait(), timeout=2)
        assert not stopping.done()
    finally:
        release.set()
        await asyncio.wait_for(stopping, timeout=2)
