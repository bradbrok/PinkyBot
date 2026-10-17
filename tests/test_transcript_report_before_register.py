"""Early transcript reports and live idle settings survive cold starts."""

import asyncio
import inspect
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call

import pytest
from fastapi.testclient import TestClient

from pinky_daemon import api, tmux_session
from pinky_daemon.auth import build_internal_auth_headers
from pinky_daemon.streaming_session import StreamingSessionConfig
from pinky_daemon.tmux_session import TmuxCommandResult, TmuxSession, _QueuedTurn, _TmuxControl
from pinky_daemon.tmux_transcript import TmuxTranscriptTailer, claude_project_slug
from pinky_daemon.transport_state import SessionState, Trigger
from tests.isolated_policy_support import closure


@pytest.fixture
def rig(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PINKY_ISOLATED_POLICY_MODE", "enforce")
    monkeypatch.setenv("PINKY_SESSION_CLASS_REBUILD", "0")
    monkeypatch.setenv("PINKY_RESUME_FAILSAFE", "0")
    app = api.create_api(db_path=str(tmp_path / "memory.db"), default_working_dir=str(tmp_path))
    for name in ("worker", "peer"):
        work = tmp_path / name
        work.mkdir()
        app.state.agents.register(
            name, working_dir=str(work), model="sonnet", runtime="claude_sdk",
            transport="tmux", isolated=True, auto_sleep_hours=8,
        )
    clock = [100.0]
    monkeypatch.setattr(api, "time", SimpleNamespace(**{**vars(time), "monotonic": lambda: clock[0]}))
    logs = []
    monkeypatch.setattr(api, "_log", logs.append)
    client = TestClient(app)
    result = SimpleNamespace(app=app, agents=app.state.agents, broker=app.state.broker,
                             client=client, clock=clock, logs=logs, root=tmp_path, home=home)
    yield result
    client.close()
    app.state.store_catalog.close()


def _path(rig, *, name="worker", filename="session.jsonl"):
    work = rig.agents.get(name).working_dir
    project = rig.home / ".claude" / "projects" / claude_project_slug(work)
    project.mkdir(parents=True, exist_ok=True)
    path = project / filename
    path.write_text("")
    return path.resolve()


def _session(rig, monkeypatch, *, name="worker", label="main"):
    agent = rig.agents.get(name)
    config = StreamingSessionConfig(
        agent_name=name, label=label, working_dir=agent.working_dir,
        model="sonnet", idle_timeout=agent.auto_sleep_hours * 3600,
    )
    pane = MagicMock(spec=_TmuxControl)
    pane.session_name = "fixture"
    pane.socket_name = ""
    pane.tmux_binary = "tmux"
    for method in ("paste_text", "send_keys", "capture_pane", "kill_session"):
        setattr(pane, method, AsyncMock(return_value=TmuxCommandResult(0, "", "")))
    pane.has_session = AsyncMock(return_value=True)
    analytics = MagicMock()
    session = TmuxSession(config, tmux_control=pane, analytics_store=analytics)
    session._launch_runtime = "claude_sdk"
    session._launch_transport = "tmux"
    session._tailer = TmuxTranscriptTailer(
        transcript_path=rig.root / f"placeholder-{name}-{label}.jsonl",
        on_turn_complete=session._handle_turn_complete,
    )
    session._tailer_first_bind_pending = True
    bind = MagicMock(wraps=session.set_transcript_path)
    monkeypatch.setattr(session, "set_transcript_path", bind)
    monkeypatch.setattr(session, "idle_sleep", AsyncMock())
    return SimpleNamespace(session=session, pane=pane, analytics=analytics, bind=bind)


def _post(rig, path, *, name="worker", label="main", session_id="reported-session", signed=False):
    route = f"/agents/{name}/transport/transcript-path"
    headers = build_internal_auth_headers(
        rig.agents.get_signing_key(name), agent_name=name, method="POST", path=route,
    ) if signed else {}
    return rig.client.post(route, headers=headers, json={
        "transcript_path": str(path), "session_id": session_id, "label": label,
    })


async def _launch(rig, monkeypatch, holder, during_connect=None):
    session = holder.session

    async def connect():
        boot = await session._state_machine.request_transition(SessionState.BOOTING, Trigger.BOOT)
        assert boot.owner_token is not None
        if during_connect is not None:
            result = during_connect()
            if inspect.isawaitable(result):
                await result
        await session._state_machine.transition_complete(
            boot.owner_token, SessionState.CONNECTED, trigger=Trigger.BOOT_COMPLETE,
        )

    monkeypatch.setattr(session, "connect", connect)
    result = await closure(rig.app, "_connect_prepared_session_unchecked")(session)
    assert result is session
    assert rig.broker.get_streaming_session(session.agent_name, session._config.label) is session


def _pending(rig):
    endpoint = closure(rig.app, "transport_transcript_path")
    return inspect.getclosurevars(endpoint).nonlocals.get("_pending_transcript_reports", {})


async def _idle_check(rig, now):
    scheduler = rig.app.state.scheduler
    await scheduler._check_idle_sessions(now)
    await asyncio.gather(*scheduler._sleep_tasks.values())


@pytest.mark.asyncio
async def test_early_report_opens_the_wake_gate_at_registration(rig, monkeypatch):
    holder = _session(rig, monkeypatch)
    path = _path(rig)
    replies = []
    await _launch(rig, monkeypatch, holder, lambda: replies.append(_post(rig, path)))
    assert replies[0].status_code == 200
    ready = holder.session._session_ready_event.is_set()
    monkeypatch.setattr(tmux_session, "_SESSION_READY_GATE_TIMEOUT_SEC", 0.01)
    await holder.session._deliver_turn(_QueuedTurn(
        prompt="wake body", platform="", chat_id="", message_id="",
        internal=True, reason="wake_resume",
    ))
    gate = [item.kwargs["subtype"] for item in holder.analytics.log_activity.call_args_list
            if item.kwargs.get("event_type") == "wake_gate"]
    actual = {"held": replies[0].json().get("held"), "ready": ready, "gate": gate,
              "bindings": holder.bind.call_args_list}
    assert actual == {
        "held": True, "ready": True, "gate": ["instant"],
        "bindings": [call(path, session_id="reported-session")],
    }
    holder.pane.paste_text.assert_awaited_once_with("wake body", enter=True)
    assert ("worker", "main") not in _pending(rig)


@pytest.mark.asyncio
async def test_disabling_idle_sleep_updates_every_live_label(rig, monkeypatch):
    holders = [_session(rig, monkeypatch, label=label) for label in ("main", "side")]
    for holder in holders:
        await _launch(rig, monkeypatch, holder)
        assert holder.session._config.idle_timeout == 8 * 3600
    response = rig.client.put("/agents/worker", json={"auto_sleep_hours": 0})
    assert response.status_code == 200, response.text
    assert rig.agents.get("worker").auto_sleep_hours == 0
    await _idle_check(rig, max(h.session.last_active for h in holders) + 9 * 3600)
    assert [h.session.idle_sleep.await_count for h in holders] == [0, 0]
    assert [h.session._config.idle_timeout for h in holders] == [0, 0]


@pytest.mark.asyncio
async def test_report_from_an_earlier_launch_is_dropped(rig, monkeypatch):
    holder = _session(rig, monkeypatch)
    response = _post(rig, _path(rig))
    assert response.status_code == 200
    assert response.json() == {"ok": True, "agent": "worker", "session": None, "held": True}
    rig.clock[0] += 1
    await _launch(rig, monkeypatch, holder)
    holder.bind.assert_not_called()
    assert not holder.session._session_ready_event.is_set()
    assert ("worker", "main") not in _pending(rig)
    assert "api: early transcript report stale for worker (main); dropped" in rig.logs


@pytest.mark.asyncio
async def test_report_past_its_ttl_is_dropped(rig, monkeypatch):
    holder = _session(rig, monkeypatch)

    def report_then_wait():
        assert _post(rig, _path(rig)).json()["held"] is True
        rig.clock[0] += 121

    await _launch(rig, monkeypatch, holder, report_then_wait)
    holder.bind.assert_not_called()
    assert not holder.session._session_ready_event.is_set()
    assert ("worker", "main") not in _pending(rig)
    assert "api: early transcript report stale for worker (main); dropped" in rig.logs


@pytest.mark.asyncio
async def test_latest_reports_are_independent_per_agent_and_label(rig, monkeypatch):
    keys = [("worker", "main"), ("worker", "side"), ("peer", "main")]
    holders = [_session(rig, monkeypatch, name=name, label=label) for name, label in keys]
    entered = [asyncio.Event() for _ in keys]
    release = asyncio.Event()

    async def wait_for_reports(index):
        entered[index].set()
        await release.wait()

    tasks = [asyncio.create_task(_launch(
        rig, monkeypatch, holder, lambda index=index: wait_for_reports(index),
    )) for index, holder in enumerate(holders)]
    try:
        await asyncio.wait_for(asyncio.gather(*(event.wait() for event in entered)), timeout=2)
        rig.clock[0] += 1
        assert _post(rig, _path(rig, filename="old.jsonl"), session_id="old").json()["held"]
        expected = []
        for name, label in keys:
            path = _path(rig, name=name, filename=f"{label}.jsonl")
            session_id = f"{name}-{label}"
            response = _post(rig, path, name=name, label=label, session_id=session_id)
            assert response.status_code == 200
            assert response.json()["held"] is True
            expected.append(call(path, session_id=session_id))
    finally:
        release.set()
        await asyncio.gather(*tasks)
    for holder, binding in zip(holders, expected, strict=True):
        assert holder.bind.call_args_list == [binding]
        assert holder.session._session_ready_event.is_set()
    assert _pending(rig) == {}


@pytest.mark.real_auth
@pytest.mark.asyncio
async def test_signed_report_restricts_ownership_before_binding(rig, monkeypatch):
    holder = _session(rig, monkeypatch)
    path = _path(rig)
    outside = _path(rig, name="peer")
    expected_projects = {path.parent}
    order = []
    restrict = holder.session.set_transcript_ownership
    bind = holder.session.set_transcript_path

    def restrict_and_record(projects):
        order.append("ownership")
        assert projects == expected_projects
        restrict(projects)

    def bind_and_check(path, *, session_id):
        order.append("path")
        assert holder.session._transcript_ownership == frozenset(expected_projects)
        return bind(path, session_id=session_id)

    monkeypatch.setattr(holder.session, "set_transcript_ownership", restrict_and_record)
    monkeypatch.setattr(holder.session, "set_transcript_path", bind_and_check)

    def report():
        accepted = _post(rig, path, signed=True)
        assert accepted.status_code == 200
        assert accepted.json()["held"] is True
        rejected = _post(rig, outside, signed=True)
        assert rejected.status_code == 403
        assert rejected.json()["detail"] == (
            "transcript_path must be in the caller's own project directory"
        )

    await _launch(rig, monkeypatch, holder, report)
    assert order == ["ownership", "path"]
    holder.bind.assert_called_once_with(path, session_id="reported-session")
    assert holder.session._session_ready_event.is_set()
    assert _pending(rig) == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["false", "exception", "missing_id"])
async def test_refused_held_bind_does_not_fail_registration(rig, monkeypatch, outcome):
    holder = _session(rig, monkeypatch)
    path = _path(rig)
    if outcome == "false":
        holder.bind.return_value = False
    elif outcome == "exception":
        holder.bind.side_effect = RuntimeError("bind refused")
    session_id = "" if outcome == "missing_id" else "reported-session"

    def report():
        assert _post(rig, path, session_id=session_id).json()["held"] is True

    await _launch(rig, monkeypatch, holder, report)
    holder.bind.assert_called_once_with(path, session_id=session_id)
    assert not holder.session._session_ready_event.is_set()
    assert ("worker", "main") not in _pending(rig)
    assert any("early transcript report refused for worker (main)" in line for line in rig.logs)


@pytest.mark.asyncio
async def test_registered_session_applies_report_immediately(rig, monkeypatch):
    holder = _session(rig, monkeypatch)
    await _launch(rig, monkeypatch, holder)
    path = _path(rig)
    response = _post(rig, path)
    assert response.status_code == 200
    assert response.json() == {"ok": True, "agent": "worker", "transcript_path": str(path)}
    holder.bind.assert_called_once_with(path, session_id="reported-session")
    assert holder.session._session_ready_event.is_set()
    assert _pending(rig) == {}


@pytest.mark.asyncio
async def test_reenabled_idle_sleep_uses_the_new_timeout(rig, monkeypatch):
    holder = _session(rig, monkeypatch)
    await _launch(rig, monkeypatch, holder)
    for hours in (0, 2):
        response = rig.client.put("/agents/worker", json={"auto_sleep_hours": hours})
        assert response.status_code == 200, response.text
        assert holder.session._config.idle_timeout == hours * 3600
    assert rig.agents.get("worker").auto_sleep_hours == 2
    await _idle_check(rig, holder.session.last_active + 2 * 3600 - 1)
    holder.session.idle_sleep.assert_not_awaited()
    await _idle_check(rig, holder.session.last_active + 2 * 3600 + 1)
    holder.session.idle_sleep.assert_awaited_once()


@pytest.mark.parametrize("hours", [0, 2])
def test_reconnect_refresh_updates_idle_timeout(rig, monkeypatch, hours):
    holder = _session(rig, monkeypatch)
    assert holder.session._config.idle_timeout == 8 * 3600
    rig.agents.update("worker", auto_sleep_hours=hours)
    rig.app.state._refresh_streaming_launch_config("worker", holder.session)
    assert holder.session._config.idle_timeout == hours * 3600


@pytest.mark.asyncio
async def test_idle_update_preserves_other_settings_and_other_agents(rig, monkeypatch):
    holder = _session(rig, monkeypatch)
    peer = _session(rig, monkeypatch, name="peer")
    for item in (holder, peer):
        await _launch(rig, monkeypatch, item)
    holder.session._config.model = "retained-model"
    response = rig.client.put("/agents/worker", json={"auto_sleep_hours": 0})
    assert response.status_code == 200, response.text
    assert holder.session._config.idle_timeout == 0
    assert holder.session._config.model == "retained-model"
    assert peer.session._config.idle_timeout == 8 * 3600
    holder.pane.kill_session.assert_not_awaited()
    peer.pane.kill_session.assert_not_awaited()
