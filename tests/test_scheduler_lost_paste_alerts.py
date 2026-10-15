"""Lost scheduler pastes notify the audience that can check their outcome."""

import asyncio
import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from pinky_daemon import agent_registry, scheduler
from pinky_daemon.agent_registry import AgentRegistry
from pinky_daemon.scheduler import AgentScheduler

FIRED = 1_700_000_000.0
PASTED = FIRED + 20
NOW = FIRED + 1000
REASON = "PASTED_UNCONFIRMED_SESSION_LOST"


@pytest.fixture
def rig(tmp_path, monkeypatch):
    clock = SimpleNamespace(now=NOW)
    for module in (scheduler, agent_registry):
        monkeypatch.setattr(module, "time", SimpleNamespace(
            **{**vars(module.time), "time": lambda: clock.now},
        ))
    registry = AgentRegistry(db_path=str(tmp_path / "registry.db"))
    registry.register("worker", working_dir=str(tmp_path / "worker"))
    registry.set_owner_profile({"timezone": "UTC"})
    logs, owners, notices = [], [], []
    monkeypatch.setattr(scheduler, "_log", logs.append)

    def pasted(name="periodic", *, fired_at=FIRED, pasted_at=PASTED):
        schedule = registry.add_schedule("worker", "0 * * * *", name=name, prompt="check work")
        row, created = registry.persist_schedule_wake(
            schedule.id, agent_name="worker", schedule_name=name,
            prompt=schedule.prompt, fired_at=fired_at,
        )
        assert created
        assert registry.mark_schedule_wake_pasted(
            row.schedule_id, row.fired_at, pasted_at=pasted_at,
            session_id="old-session", pasted_prompt=row.prompt,
        )
        return row

    async def owner(name, text):
        owners.append((name, text))
        return True

    async def agent(name, text):
        notices.append((name, text))
        return True

    def engine(*, live="replacement", rendered=0.0, timezone="UTC"):
        wake = AsyncMock(return_value=True)
        kwargs = {
            "wake_callback": wake,
            "delivery_session_fn": lambda _: live,
            "owner_notify_callback": owner,
            "delivery_context_rendered_at_fn": (rendered if callable(rendered)
                                                else lambda _: rendered),
            "delivery_timezone_fn": lambda _: timezone,
            "agent_notify_callback": agent,
        }
        # Base runs exercise the old audience decision rather than failing at construction.
        supported = inspect.signature(AgentScheduler).parameters
        result = AgentScheduler(registry, **{k: v for k, v in kwargs.items() if k in supported})
        return result, wake

    yield SimpleNamespace(
        registry=registry, clock=clock, pasted=pasted, engine=engine,
        logs=logs, owners=owners, notices=notices,
    )
    registry.close()


async def finish_notices(engine):
    tasks = set(engine._owner_alert_tasks) | set(getattr(engine, "_agent_notice_tasks", ()))
    if tasks:
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=2)


def assert_abandoned(rig, rows):
    for row in rows:
        saved = rig.registry.get_schedule_wake_by_fire(row.schedule_id, row.fired_at)
        assert saved.abandoned_at == NOW
        assert saved.last_error.startswith(REASON)
        assert saved.accepted_at == 0
    assert sum(REASON in line for line in rig.logs) == len(rows)


@pytest.mark.parametrize("rendered", [PASTED, PASTED + 1])
async def test_replacement_context_covers_two_pastes_without_notifications(rig, rendered):
    rows = [rig.pasted("alpha"), rig.pasted("beta", fired_at=FIRED + 1)]
    engine, wake = rig.engine(rendered=rendered)
    engine._settle_lost_pastes("worker")
    engine._settle_lost_pastes("worker")
    await finish_notices(engine)
    assert_abandoned(rig, rows)
    assert rig.owners == rig.notices == []
    wake.assert_not_awaited()


async def test_paste_after_render_gets_one_normal_agent_notice(rig):
    row = rig.pasted("morning check")
    rig.registry.set_owner_profile({"timezone": "Asia/Tokyo"})
    engine, wake = rig.engine(rendered=PASTED - 1, timezone="America/New_York")
    engine._settle_lost_pastes("worker")
    engine._settle_lost_pastes("worker")
    await finish_notices(engine)
    assert_abandoned(rig, [row])
    assert rig.owners == []
    assert rig.notices == [("worker", "Scheduled prompt 'morning check' (fired "
                           "2023-11-14 17:13 EST) was pasted into your previous session and "
                           "may not have run; check before redoing.")]
    wake.assert_not_awaited()
    replacement, _ = rig.engine(rendered=0)
    replacement._settle_lost_pastes("worker")
    await finish_notices(replacement)
    assert len(rig.notices) == 1
    assert len(rig.registry.list_pending_schedule_wakes("worker", include_parked=True)) == 1


def unavailable_render(_):
    raise RuntimeError("render time unavailable")


@pytest.mark.parametrize("rendered", [0.0, None, unavailable_render])
async def test_unknown_render_time_notifies_agent(rig, rendered):
    row = rig.pasted()
    engine, _ = rig.engine(rendered=rendered)
    engine._settle_lost_pastes("worker")
    await finish_notices(engine)
    assert_abandoned(rig, [row])
    assert rig.owners == []
    assert len(rig.notices) == 1
    assert "2023-11-14 22:13 UTC" in rig.notices[0][1]


@pytest.mark.parametrize("reaper", [False, True])
async def test_no_live_session_alerts_owner_in_plain_local_words(rig, reaper):
    row = rig.pasted("morning check")
    rig.registry.set_owner_profile({"timezone": "Europe/Paris"})
    engine, wake = rig.engine(live="", rendered=NOW, timezone="America/New_York")
    for _ in range(2):
        if reaper:
            engine._run_outbox_reaper_if_due(NOW)
        else:
            engine._settle_lost_pastes("worker")
        await finish_notices(engine)
    assert_abandoned(rig, [row])
    assert rig.notices == []
    assert rig.owners == [("worker", "worker went offline before it confirmed the scheduled "
                          "prompt 'morning check' (fired 2023-11-14 23:13 CET); it may not have run.")]
    text = rig.owners[0][1]
    assert "fired_at=" not in text and "PASTED_UNCONFIRMED" not in text
    assert str(int(FIRED)) not in text
    wake.assert_not_awaited()


@pytest.mark.parametrize(("agent_zone", "owner_zone", "time_text"), [
    ("", "America/New_York", "2023-11-14 17:13 EST"),
    ("invalid/zone", "America/New_York", "2023-11-14 17:13 EST"),
    ("invalid/zone", "invalid/owner", "2023-11-14 22:13 UTC"),
    ("America/New_York", "invalid/owner", "2023-11-14 17:13 EST"),
])
async def test_agent_notice_timezone_fallbacks(rig, agent_zone, owner_zone, time_text):
    rig.pasted()
    rig.registry.set_owner_profile({"timezone": owner_zone})
    engine, _ = rig.engine(rendered=0, timezone=agent_zone)
    engine._settle_lost_pastes("worker")
    await finish_notices(engine)
    assert rig.owners == []
    assert len(rig.notices) == 1 and time_text in rig.notices[0][1]


async def test_owner_notice_invalid_timezone_uses_named_utc(rig):
    rig.pasted()
    rig.registry.set_owner_profile({"timezone": "invalid/owner"})
    engine, _ = rig.engine(live="")
    engine._settle_lost_pastes("worker")
    await finish_notices(engine)
    assert rig.notices == []
    assert len(rig.owners) == 1 and "2023-11-14 22:13 UTC" in rig.owners[0][1]


async def test_current_paste_session_is_not_lost(rig):
    row = rig.pasted()
    engine, _ = rig.engine(live="old-session", rendered=0)
    engine._settle_lost_pastes("worker")
    await finish_notices(engine)
    assert rig.registry.get_schedule_wake_by_fire(row.schedule_id, row.fired_at).abandoned_at == 0
    assert rig.owners == rig.notices == []


@pytest.fixture
def api_session(tmp_path, monkeypatch):
    import claude_agent_sdk

    from pinky_daemon import api
    from pinky_daemon import streaming_session as sdk_transport
    from pinky_daemon.api import create_api
    from pinky_daemon.streaming_session import StreamingSession

    clock = SimpleNamespace(now=NOW)
    for module in (api, sdk_transport, scheduler, agent_registry):
        monkeypatch.setattr(module, "time", SimpleNamespace(
            **{**vars(module.time), "time": lambda: clock.now},
        ))
    sent = []
    client_stub = SimpleNamespace(
        connect=AsyncMock(), query=AsyncMock(), disconnect=AsyncMock(),
        get_server_info=AsyncMock(return_value={}),
    )

    async def send(ss, prompt, *args, **kwargs):
        sent.append(prompt)
        return True

    monkeypatch.setattr(claude_agent_sdk, "ClaudeSDKClient", lambda options: client_stub)
    monkeypatch.setattr(StreamingSession, "_reader_loop", AsyncMock())
    monkeypatch.setattr(StreamingSession, "send", send)
    app = create_api(default_working_dir=str(tmp_path), db_path=str(tmp_path / "api.db"))
    app.state.agents.register("worker", working_dir=str(tmp_path / "worker"))
    app.state.agents.set_owner_profile({"timezone": "UTC"})
    client = TestClient(app)
    response = client.post("/agents/worker/wake", params={"prompt": "hello"})
    assert response.status_code == 200, response.text
    ss = app.state.broker._streaming["worker"]["main"]
    sent.clear()
    yield app, ss, clock, sent
    client.close()
    app.state.agents.close()


def test_session_keeps_preview_separate_from_committed_context(api_session):
    from pinky_daemon.wake_prompt import WakeReason

    app, ss, clock, _ = api_session
    assert getattr(ss._config, "wake_context_rendered_at", 0) == NOW
    preview = ss._config.wake_context
    clock.now += 10
    ss._config.wake_context_builder("worker", WakeReason.CONTEXT_RESTART)
    assert getattr(ss._config, "wake_context_rendered_at", 0) == NOW
    assert ss._config.wake_context == preview


@pytest.mark.parametrize("after_render", [False, True])
async def test_api_uses_live_session_render_and_normal_notice_route(api_session, after_render):
    app, ss, clock, sent = api_session
    registry = app.state.agents
    schedule = registry.add_schedule("worker", "0 * * * *", name="periodic", prompt="check work")
    row, _ = registry.persist_schedule_wake(
        schedule.id, agent_name="worker", schedule_name=schedule.name,
        prompt=schedule.prompt, fired_at=FIRED,
    )
    pasted_at = NOW + 1 if after_render else NOW - 1
    assert registry.mark_schedule_wake_pasted(
        row.schedule_id, row.fired_at, pasted_at=pasted_at, session_id="previous",
        pasted_prompt=row.prompt,
    )
    if not after_render:
        from pinky_daemon.wake_prompt import WakeReason

        second = registry.add_schedule("worker", "0 * * * *", name="secondary", prompt="check more")
        other, _ = registry.persist_schedule_wake(
            second.id, agent_name="worker", schedule_name=second.name,
            prompt=second.prompt, fired_at=FIRED + 1,
        )
        assert registry.mark_schedule_wake_pasted(
            other.schedule_id, other.fired_at, pasted_at=pasted_at,
            session_id="previous", pasted_prompt=other.prompt,
        )
        body = ss._config.wake_context_builder("worker", WakeReason.CONTEXT_RESTART)
        assert "Owed scheduled wakes:" in body
        assert f"- periodic (fired_at={FIRED})" in body
        assert f"- secondary (fired_at={FIRED + 1})" in body
        # Deliver that rebuilt context through the SDK's actual wake hook.
        await ss.connect()
        await ss._initial_wake_task
    ss._config.timezone = "America/New_York"
    engine = app.state.scheduler
    owner = AsyncMock(return_value=True)
    engine._owner_notify_callback = owner
    wake = AsyncMock(return_value=True)
    engine._wake_callback = wake
    engine._settle_lost_pastes("worker")
    engine._settle_lost_pastes("worker")
    await finish_notices(engine)
    owner.assert_not_awaited()
    wake.assert_not_awaited()
    if after_render:
        assert len(sent) == 1 and sent[0].startswith("[agent | system | internal | ")
        assert "2023-11-14 17:13 EST" in sent[0]
        assert "may not have run; check before redoing." in sent[0]
    else:
        assert sent == []
