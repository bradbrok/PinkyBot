"""Tests for agent scheduler, heartbeats, schedules, and session types."""

from __future__ import annotations

import os
import tempfile
import time
from datetime import datetime

import pytest

from pinky_daemon.agent_registry import AgentRegistry
from pinky_daemon.scheduler import AgentScheduler, cron_matches, next_cron_description

# ── Cron Parser Tests ──────────────────────────────────────


class TestCronParser:
    def test_every_minute(self):
        dt = datetime(2026, 3, 27, 14, 30)
        assert cron_matches("* * * * *", dt) is True

    def test_specific_minute(self):
        dt = datetime(2026, 3, 27, 14, 30)
        assert cron_matches("30 * * * *", dt) is True
        assert cron_matches("31 * * * *", dt) is False

    def test_specific_hour_minute(self):
        dt = datetime(2026, 3, 27, 8, 0)
        assert cron_matches("0 8 * * *", dt) is True
        assert cron_matches("0 9 * * *", dt) is False

    def test_step(self):
        dt = datetime(2026, 3, 27, 14, 15)
        assert cron_matches("*/15 * * * *", dt) is True
        dt2 = datetime(2026, 3, 27, 14, 7)
        assert cron_matches("*/15 * * * *", dt2) is False

    def test_range(self):
        dt = datetime(2026, 3, 27, 10, 0)
        assert cron_matches("0 8-17 * * *", dt) is True
        dt2 = datetime(2026, 3, 27, 20, 0)
        assert cron_matches("0 8-17 * * *", dt2) is False

    def test_list(self):
        dt = datetime(2026, 3, 27, 8, 0)
        assert cron_matches("0 8,12,18 * * *", dt) is True
        dt2 = datetime(2026, 3, 27, 10, 0)
        assert cron_matches("0 8,12,18 * * *", dt2) is False

    def test_day_of_week(self):
        # 2026-03-27 is a Friday = isoweekday() 5, %7 = 5
        dt = datetime(2026, 3, 27, 8, 0)
        assert cron_matches("0 8 * * 5", dt) is True
        assert cron_matches("0 8 * * 1", dt) is False

    def test_day_of_month(self):
        dt = datetime(2026, 3, 27, 8, 0)
        assert cron_matches("0 8 27 * *", dt) is True
        assert cron_matches("0 8 15 * *", dt) is False

    def test_month(self):
        dt = datetime(2026, 3, 27, 8, 0)
        assert cron_matches("0 8 * 3 *", dt) is True
        assert cron_matches("0 8 * 4 *", dt) is False

    def test_invalid_cron(self):
        dt = datetime(2026, 3, 27, 8, 0)
        assert cron_matches("bad", dt) is False
        assert cron_matches("* *", dt) is False

    def test_combined(self):
        # Every weekday at 9:00 AM
        dt_monday = datetime(2026, 3, 23, 9, 0)  # Monday
        dt_saturday = datetime(2026, 3, 28, 9, 0)  # Saturday
        assert cron_matches("0 9 * * 1-5", dt_monday) is True
        assert cron_matches("0 9 * * 1-5", dt_saturday) is False


class TestCronDescription:
    def test_hourly(self):
        desc = next_cron_description("0 8 * * *")
        assert "8:00" in desc

    def test_every_n_minutes(self):
        desc = next_cron_description("*/5 * * * *")
        assert "5 minutes" in desc


# ── Agent Schedule Tests ───────────────────────────────────


@pytest.fixture
def registry():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    reg = AgentRegistry(db_path=path)
    yield reg
    reg.close()
    os.unlink(path)


class TestAgentSchedules:
    def test_add_schedule(self, registry):
        registry.register("oleg", model="opus")
        schedule = registry.add_schedule(
            "oleg", "0 8 * * *",
            name="morning", prompt="Good morning!",
        )
        assert schedule.id > 0
        assert schedule.agent_name == "oleg"
        assert schedule.cron == "0 8 * * *"
        assert schedule.name == "morning"
        assert schedule.prompt == "Good morning!"
        assert schedule.enabled is True

    def test_get_schedules(self, registry):
        registry.register("oleg")
        registry.add_schedule("oleg", "0 8 * * *", name="morning")
        registry.add_schedule("oleg", "0 21 * * *", name="evening")

        schedules = registry.get_schedules("oleg")
        assert len(schedules) == 2
        assert schedules[0].name == "morning"
        assert schedules[1].name == "evening"

    def test_get_schedules_enabled_only(self, registry):
        registry.register("oleg")
        _s1 = registry.add_schedule("oleg", "0 8 * * *", name="active")
        s2 = registry.add_schedule("oleg", "0 21 * * *", name="disabled")
        registry.toggle_schedule(s2.id, False)

        enabled = registry.get_schedules("oleg", enabled_only=True)
        assert len(enabled) == 1
        assert enabled[0].name == "active"

        all_schedules = registry.get_schedules("oleg", enabled_only=False)
        assert len(all_schedules) == 2

    def test_get_all_schedules(self, registry):
        registry.register("oleg")
        registry.register("rex")
        registry.add_schedule("oleg", "0 8 * * *", name="oleg-morning")
        registry.add_schedule("rex", "0 9 * * *", name="rex-morning")

        all_schedules = registry.get_all_schedules()
        assert len(all_schedules) == 2

    def test_remove_schedule(self, registry):
        registry.register("oleg")
        s = registry.add_schedule("oleg", "0 8 * * *")
        assert registry.remove_schedule(s.id) is True
        assert registry.get_schedules("oleg") == []

    def test_remove_missing(self, registry):
        assert registry.remove_schedule(999) is False

    def test_toggle_schedule(self, registry):
        registry.register("oleg")
        s = registry.add_schedule("oleg", "0 8 * * *")
        assert registry.toggle_schedule(s.id, False) is True

        schedules = registry.get_schedules("oleg", enabled_only=False)
        assert schedules[0].enabled is False

    def test_update_last_run(self, registry):
        registry.register("oleg")
        s = registry.add_schedule("oleg", "0 8 * * *")
        assert s.last_run == 0.0

        now = time.time()
        registry.update_schedule_last_run(s.id, now)

        schedules = registry.get_schedules("oleg")
        assert schedules[0].last_run == pytest.approx(now, abs=0.1)

    def test_cascade_delete(self, registry):
        registry.register("oleg")
        registry.add_schedule("oleg", "0 8 * * *")
        registry.add_schedule("oleg", "0 21 * * *")

        registry.delete("oleg")
        # Schedules should be cascade-deleted
        assert registry.get_schedules("oleg") == []


# ── Agent Heartbeat Tests ──────────────────────────────────


class TestAgentHeartbeats:
    def test_record_heartbeat(self, registry):
        registry.register("oleg")
        hb = registry.record_heartbeat(
            "oleg", session_id="oleg-main",
            status="alive", context_pct=45.0, message_count=12,
        )
        assert hb.agent_name == "oleg"
        assert hb.session_id == "oleg-main"
        assert hb.status == "alive"
        assert hb.context_pct == 45.0
        assert hb.message_count == 12

    def test_get_latest_heartbeat(self, registry):
        registry.register("oleg")
        registry.record_heartbeat("oleg", status="alive")
        registry.record_heartbeat("oleg", status="stale")

        latest = registry.get_latest_heartbeat("oleg")
        assert latest is not None
        assert latest.status == "stale"

    def test_get_latest_none(self, registry):
        registry.register("oleg")
        assert registry.get_latest_heartbeat("oleg") is None

    def test_get_heartbeats(self, registry):
        registry.register("oleg")
        for i in range(5):
            registry.record_heartbeat("oleg", context_pct=i * 10.0)

        heartbeats = registry.get_heartbeats("oleg", limit=3)
        assert len(heartbeats) == 3
        # Most recent first
        assert heartbeats[0].context_pct == 40.0

    def test_get_all_latest(self, registry):
        registry.register("oleg")
        registry.register("rex")
        registry.record_heartbeat("oleg", status="alive")
        registry.record_heartbeat("rex", status="stale")

        all_latest = registry.get_all_latest_heartbeats()
        assert len(all_latest) == 2
        names = {h.agent_name for h in all_latest}
        assert "oleg" in names
        assert "rex" in names

    def test_heartbeat_with_metadata(self, registry):
        registry.register("oleg")
        hb = registry.record_heartbeat(
            "oleg", metadata={"wake_reason": "cron", "schedule": "morning"},
        )
        assert hb.metadata["wake_reason"] == "cron"

    def test_cascade_delete(self, registry):
        registry.register("oleg")
        registry.record_heartbeat("oleg", status="alive")
        registry.delete("oleg")
        assert registry.get_latest_heartbeat("oleg") is None


# ── Agent Auto-Start Tests ─────────────────────────────────


class TestAutoStart:
    def test_list_auto_start(self, registry):
        registry.register("oleg", auto_start=True, enabled=True)
        registry.register("rex", auto_start=False, enabled=True)
        registry.register("dead", auto_start=True, enabled=False)

        auto = registry.list_auto_start_agents()
        assert len(auto) == 1
        assert auto[0].name == "oleg"

    def test_agent_role(self, registry):
        registry.register("oleg", role="sidekick", auto_start=True)
        agent = registry.get("oleg")
        assert agent.role == "sidekick"
        assert agent.auto_start is True

    def test_heartbeat_interval(self, registry):
        registry.register("oleg", heartbeat_interval=300)
        agent = registry.get("oleg")
        assert agent.heartbeat_interval == 300

    def test_update_auto_start(self, registry):
        registry.register("oleg", auto_start=False)
        registry.register("oleg", auto_start=True)
        agent = registry.get("oleg")
        assert agent.auto_start is True


# ── Session Type Tests ─────────────────────────────────────


class TestSessionTypes:
    def test_create_main_session(self):
        from pinky_daemon.sessions import SessionManager, SessionType

        mgr = SessionManager()
        session = mgr.create(
            session_id="oleg-main",
            model="opus",
            session_type="main",
            agent_name="oleg",
        )
        assert session.session_type == SessionType.main
        assert session.agent_name == "oleg"
        assert session.id == "oleg-main"

    def test_create_worker_session(self):
        from pinky_daemon.sessions import SessionManager, SessionType

        mgr = SessionManager()
        session = mgr.create(
            session_id="oleg-abc123",
            session_type="worker",
            agent_name="oleg",
        )
        assert session.session_type == SessionType.worker

    def test_create_chat_session(self):
        from pinky_daemon.sessions import SessionManager, SessionType

        mgr = SessionManager()
        session = mgr.create(session_id="pinky-test")
        assert session.session_type == SessionType.chat
        assert session.agent_name == ""

    def test_session_type_in_info(self):
        from pinky_daemon.sessions import SessionManager

        mgr = SessionManager()
        session = mgr.create(
            session_id="oleg-main",
            session_type="main",
            agent_name="oleg",
        )
        info = session.info
        assert info.session_type == "main"
        assert info.agent_name == "oleg"
        assert info.to_dict()["session_type"] == "main"
        assert info.to_dict()["agent_name"] == "oleg"

    def test_session_type_persists(self):
        from pinky_daemon.session_store import SessionStore
        from pinky_daemon.sessions import SessionManager, SessionType

        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)

        # Create with type
        st1 = SessionStore(db_path=path)
        mgr1 = SessionManager(store=st1)
        mgr1.create(session_id="oleg-main", session_type="main", agent_name="oleg")
        st1.close()

        # Restore
        st2 = SessionStore(db_path=path)
        mgr2 = SessionManager(store=st2)
        session = mgr2.get("oleg-main")
        assert session is not None
        assert session.session_type == SessionType.main
        assert session.agent_name == "oleg"

        st2.close()
        os.unlink(path)

    def test_list_by_agent(self):
        from pinky_daemon.session_store import SessionRecord, SessionStore

        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        store = SessionStore(db_path=path)

        now = time.time()
        store.save(SessionRecord(
            id="oleg-main", model="opus", soul="", working_dir=".",
            allowed_tools=[], max_turns=25, timeout=300, system_prompt="",
            restart_threshold_pct=80, auto_restart=True, permission_mode="",
            state="idle", created_at=now, last_active=now, restart_count=0,
            sdk_session_id="", session_type="main", agent_name="oleg",
        ))
        store.save(SessionRecord(
            id="rex-main", model="sonnet", soul="", working_dir=".",
            allowed_tools=[], max_turns=25, timeout=300, system_prompt="",
            restart_threshold_pct=80, auto_restart=True, permission_mode="",
            state="idle", created_at=now, last_active=now, restart_count=0,
            sdk_session_id="", session_type="main", agent_name="rex",
        ))

        oleg_sessions = store.list_by_agent("oleg")
        assert len(oleg_sessions) == 1
        assert oleg_sessions[0].agent_name == "oleg"

        main = store.get_main_session("oleg")
        assert main is not None
        assert main.id == "oleg-main"

        store.close()
        os.unlink(path)


# ── Scheduler Unit Tests ───────────────────────────────────


class TestScheduler:
    def test_init(self, registry):
        scheduler = AgentScheduler(registry)
        assert scheduler.running is False

    @pytest.mark.asyncio
    async def test_start_stop(self, registry):
        scheduler = AgentScheduler(registry, tick_interval=1)
        await scheduler.start()
        assert scheduler.running is True
        await scheduler.stop()
        assert scheduler.running is False

    @pytest.mark.asyncio
    async def test_fire_now(self, registry):
        fired = []

        async def wake_cb(agent_name, session_id, prompt):
            fired.append((agent_name, session_id, prompt))

        scheduler = AgentScheduler(registry, wake_callback=wake_cb)
        result = await scheduler.fire_now("oleg", "test wake")
        assert result is True
        assert len(fired) == 1
        assert fired[0][0] == "oleg"
        assert fired[0][2] == "test wake"

    @pytest.mark.asyncio
    async def test_fire_now_no_callback(self, registry):
        scheduler = AgentScheduler(registry)
        result = await scheduler.fire_now("oleg")
        assert result is False


# ── Heartbeat Watchdog Resurrection (issue #338) ──────────────────────────


class TestHeartbeatResurrection:
    """The watchdog must invoke heartbeat_callback for dead agents,
    rate-limited to RESURRECTION_MAX_ATTEMPTS per RESURRECTION_WINDOW_SECONDS.
    """

    class _FakeStreamingSession:
        is_connected = True
        id = "ivan-main"
        context_used_pct = 12.5
        stats = {"messages_sent": 2, "turns": 3}

    @pytest.mark.asyncio
    async def test_dead_agent_triggers_resurrection_callback(self, registry):
        registry.register("ivan", model="opus", heartbeat_interval=60)
        # Backdate a heartbeat to >2x interval ago → "dead" range
        registry.record_heartbeat("ivan", session_id="old", status="alive")
        # Force the heartbeat to look stale by rewriting timestamp
        registry._db.execute(
            "UPDATE agent_heartbeats SET timestamp = ? WHERE agent_name = ?",
            (time.time() - 600, "ivan"),
        )
        registry._db.commit()

        called = []

        async def cb(agent_name, session_id):
            called.append((agent_name, session_id))

        scheduler = AgentScheduler(registry, heartbeat_callback=cb)
        await scheduler._check_heartbeats(time.time())

        assert called == [("ivan", "old")]

    @pytest.mark.asyncio
    async def test_resurrection_is_rate_limited(self, registry):
        """_maybe_resurrect itself caps attempts per agent within the window."""
        called = []

        async def cb(agent_name, session_id):
            called.append(agent_name)

        scheduler = AgentScheduler(registry, heartbeat_callback=cb)
        now = time.time()
        # Drive the resurrection helper directly past the cap
        for _ in range(scheduler.RESURRECTION_MAX_ATTEMPTS + 3):
            await scheduler._maybe_resurrect("ivan", "sid", now)

        assert len(called) == scheduler.RESURRECTION_MAX_ATTEMPTS

    @pytest.mark.asyncio
    async def test_resurrection_window_resets_after_expiry(self, registry):
        called = []

        async def cb(agent_name, session_id):
            called.append(agent_name)

        scheduler = AgentScheduler(registry, heartbeat_callback=cb)
        now = time.time()
        # Fill the window
        for _ in range(scheduler.RESURRECTION_MAX_ATTEMPTS):
            await scheduler._maybe_resurrect("ivan", "sid", now)
        # One more — should be capped
        await scheduler._maybe_resurrect("ivan", "sid", now)
        assert len(called) == scheduler.RESURRECTION_MAX_ATTEMPTS

        # Jump past the window — old attempts age out, new attempt allowed
        future = now + scheduler.RESURRECTION_WINDOW_SECONDS + 1
        await scheduler._maybe_resurrect("ivan", "sid", future)
        assert len(called) == scheduler.RESURRECTION_MAX_ATTEMPTS + 1

    @pytest.mark.asyncio
    async def test_resurrection_skipped_when_precondition_false(self, registry):
        """is_resurrectable_fn returning False short-circuits before budget/log.

        Regression for the "watchdog spams 5/5 every 30s on idle-sleeping
        agents" bug: the API callback used to be the only place idle-sleep
        was checked, so the scheduler still consumed a budget slot and
        emitted a log line for each tick. With is_resurrectable_fn, the
        scheduler skips entirely.
        """
        called = []

        async def cb(agent_name, session_id):
            called.append(agent_name)

        scheduler = AgentScheduler(
            registry,
            heartbeat_callback=cb,
            is_resurrectable_fn=lambda name: False,  # never resurrectable
        )
        now = time.time()
        for _ in range(scheduler.RESURRECTION_MAX_ATTEMPTS + 3):
            await scheduler._maybe_resurrect("ivan", "sid", now)

        # Callback never fired
        assert called == []
        # And budget was never consumed — the attempt list stays empty so a
        # later state-change (agent stops idle-sleeping) gets the full quota.
        assert scheduler._resurrection_attempts.get("ivan", []) == []

    @pytest.mark.asyncio
    async def test_resurrection_runs_when_precondition_true(self, registry):
        """is_resurrectable_fn returning True preserves old behavior."""
        called = []

        async def cb(agent_name, session_id):
            called.append(agent_name)

        scheduler = AgentScheduler(
            registry,
            heartbeat_callback=cb,
            is_resurrectable_fn=lambda name: True,
        )
        now = time.time()
        await scheduler._maybe_resurrect("ivan", "sid", now)
        assert called == ["ivan"]

    @pytest.mark.asyncio
    async def test_resurrection_precondition_failure_fails_open(self, registry):
        """If is_resurrectable_fn raises, fall through (don't silently disable)."""
        called = []

        async def cb(agent_name, session_id):
            called.append(agent_name)

        def bad_precondition(name):
            raise RuntimeError("oops")

        scheduler = AgentScheduler(
            registry,
            heartbeat_callback=cb,
            is_resurrectable_fn=bad_precondition,
        )
        now = time.time()
        await scheduler._maybe_resurrect("ivan", "sid", now)
        # Fail-open: resurrection proceeds despite precondition exception
        assert called == ["ivan"]

    @pytest.mark.asyncio
    async def test_no_callback_means_no_crash(self, registry):
        """Dead agents are still legal even when no resurrection wiring exists."""
        registry.register("ivan", model="opus", heartbeat_interval=60)
        registry.record_heartbeat("ivan", session_id="old", status="alive")
        registry._db.execute(
            "UPDATE agent_heartbeats SET timestamp = ? WHERE agent_name = ?",
            (time.time() - 600, "ivan"),
        )
        registry._db.commit()

        scheduler = AgentScheduler(registry)  # heartbeat_callback omitted
        # Should not raise
        await scheduler._check_heartbeats(time.time())

    @pytest.mark.asyncio
    async def test_callback_exception_does_not_break_loop(self, registry):
        registry.register("ivan", model="opus", heartbeat_interval=60)
        registry.record_heartbeat("ivan", session_id="old", status="alive")
        registry._db.execute(
            "UPDATE agent_heartbeats SET timestamp = ? WHERE agent_name = ?",
            (time.time() - 600, "ivan"),
        )
        registry._db.commit()

        async def cb(agent_name, session_id):
            raise RuntimeError("boom")

        scheduler = AgentScheduler(registry, heartbeat_callback=cb)
        # Should swallow and log, not propagate
        await scheduler._check_heartbeats(time.time())

    @pytest.mark.asyncio
    async def test_connected_streaming_session_clears_dead_heartbeat(self, registry):
        registry.register("ivan", model="opus", heartbeat_interval=60)
        registry.record_heartbeat("ivan", session_id="old", status="dead")
        registry._db.execute(
            "UPDATE agent_heartbeats SET timestamp = ? WHERE agent_name = ?",
            (time.time() - 600, "ivan"),
        )
        registry._db.commit()
        called = []

        async def cb(agent_name, session_id):
            called.append((agent_name, session_id))

        scheduler = AgentScheduler(
            registry,
            heartbeat_callback=cb,
            streaming_sessions_fn=lambda: {
                "ivan": {"main": self._FakeStreamingSession()},
            },
        )
        await scheduler._check_heartbeats(time.time())

        latest = registry.get_latest_heartbeat("ivan")
        assert called == []
        assert latest is not None
        assert latest.status == "alive"
        assert latest.session_id == "ivan-main"
        assert latest.context_pct == 12.5
        assert latest.message_count == 5
        assert latest.metadata["source"] == "server_presence"
        assert latest.metadata["reason"] == "connected_streaming_session"

    @pytest.mark.asyncio
    async def test_fresh_last_seen_suppresses_dead_heartbeat_resurrection(self, registry):
        registry.register("ivan", model="opus", heartbeat_interval=60)
        registry.record_heartbeat("ivan", session_id="old", status="dead")
        old_ts = time.time() - 600
        registry._db.execute(
            "UPDATE agent_heartbeats SET timestamp = ? WHERE agent_name = ?",
            (old_ts, "ivan"),
        )
        registry._db.commit()
        now = time.time()
        registry.stamp_last_seen("ivan", ts=now - 10)
        called = []

        async def cb(agent_name, session_id):
            called.append((agent_name, session_id))

        scheduler = AgentScheduler(
            registry,
            heartbeat_callback=cb,
            streaming_sessions_fn=lambda: {},
        )
        await scheduler._check_heartbeats(now)

        latest = registry.get_latest_heartbeat("ivan")
        assert called == []
        assert latest is not None
        assert latest.status == "alive"
        assert latest.metadata["source"] == "server_presence"
        assert latest.metadata["reason"] == "fresh_last_seen"
        assert latest.metadata["last_seen_at"] == pytest.approx(now - 10)
