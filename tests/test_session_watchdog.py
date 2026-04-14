"""Tests for session_watchdog module."""
from __future__ import annotations

import asyncio
import time

import pytest

from pinky_daemon.session_watchdog import (
    SessionWatchdog,
    WatchdogConfig,
    _AgentState,
    _SessionSnapshot,
)


class FakeSession:
    """Minimal fake streaming session for testing."""

    def __init__(self, *, turns=0, pending=0, connected=True, activity=""):
        self._turns = turns
        self._pending = pending
        self._connected = connected
        self._activity = activity

    @property
    def stats(self):
        return {
            "turns": self._turns,
            "pending_responses": self._pending,
            "connected": self._connected,
            "current_activity": self._activity,
        }


@pytest.fixture
def make_watchdog():
    """Factory for creating a watchdog with fake sessions."""

    def _make(sessions=None, **kwargs):
        sessions = sessions or {}
        return SessionWatchdog(
            streaming_sessions_fn=lambda: sessions,
            **kwargs,
        )

    return _make


class TestWatchdogConfig:
    def test_defaults(self):
        cfg = WatchdogConfig()
        assert cfg.enabled is True
        assert cfg.mode == "recover"
        assert cfg.warn_after_seconds == 600
        assert cfg.recover_after_seconds == 900

    def test_override(self):
        cfg = WatchdogConfig(warn_after_seconds=120, mode="alert")
        assert cfg.warn_after_seconds == 120
        assert cfg.mode == "alert"


class TestSnapshotTaking:
    def test_snapshot_from_fake_session(self, make_watchdog):
        ss = FakeSession(turns=5, pending=3, activity="Edit — foo.py")
        wd = make_watchdog()
        snap = wd._take_snapshot("test-agent", "main", ss)
        assert snap.agent_name == "test-agent"
        assert snap.turns == 5
        assert snap.pending == 3
        assert snap.current_activity == "Edit — foo.py"
        assert snap.connected is True


class TestEvaluation:
    @pytest.mark.asyncio
    async def test_progress_resets_state(self, make_watchdog):
        wd = make_watchdog()

        # Initial snapshot
        snap1 = _SessionSnapshot(
            agent_name="a", label="main", connected=True,
            turns=1, pending=0, current_activity="Read",
            sample_time=time.time(),
        )
        await wd._evaluate(snap1, time.time())
        assert "a" in wd._states
        assert wd._states["a"].last_progress_turns == 1

        # Progress: turns increased
        snap2 = _SessionSnapshot(
            agent_name="a", label="main", connected=True,
            turns=2, pending=0, current_activity="Edit",
            sample_time=time.time(),
        )
        await wd._evaluate(snap2, time.time())
        assert wd._states["a"].last_progress_turns == 2
        assert wd._states["a"].warned is False

    @pytest.mark.asyncio
    async def test_stuck_triggers_warn(self, make_watchdog):
        alerts = []

        async def _alert(agent, msg):
            alerts.append((agent, msg))

        wd = make_watchdog(alert_fn=_alert)

        # Set up stale state
        now = time.time()
        wd._states["a"] = _AgentState(
            last_progress_turns=5,
            last_progress_activity="Edit — big.html",
            last_progress_at=now - 700,  # 700s ago > 600s warn threshold
        )

        snap = _SessionSnapshot(
            agent_name="a", label="main", connected=True,
            turns=5, pending=2, current_activity="Edit — big.html",
            sample_time=now,
        )
        await wd._evaluate(snap, now)

        assert wd._states["a"].warned is True
        assert len(alerts) == 1
        assert "stuck" in alerts[0][1]

    @pytest.mark.asyncio
    async def test_stuck_triggers_recover(self, make_watchdog):
        recoveries = []

        async def _recover(agent, label, reason):
            recoveries.append((agent, label, reason))

        wd = make_watchdog(recover_fn=_recover)

        now = time.time()
        wd._states["a"] = _AgentState(
            last_progress_turns=5,
            last_progress_activity="Edit — big.html",
            last_progress_at=now - 1000,  # 1000s ago > 900s recover threshold
            warned=True,
        )

        snap = _SessionSnapshot(
            agent_name="a", label="main", connected=True,
            turns=5, pending=3, current_activity="Edit — big.html",
            sample_time=now,
        )
        await wd._evaluate(snap, now)

        assert len(recoveries) == 1
        assert recoveries[0][0] == "a"

    @pytest.mark.asyncio
    async def test_no_backlog_no_warn(self, make_watchdog):
        """With require_backlog=True, no pending = no warning."""
        alerts = []

        async def _alert(agent, msg):
            alerts.append(msg)

        wd = make_watchdog(alert_fn=_alert)

        now = time.time()
        wd._states["a"] = _AgentState(
            last_progress_turns=5,
            last_progress_activity="Edit — big.html",
            last_progress_at=now - 700,
        )

        snap = _SessionSnapshot(
            agent_name="a", label="main", connected=True,
            turns=5, pending=0,  # no backlog
            current_activity="Edit — big.html",
            sample_time=now,
        )
        await wd._evaluate(snap, now)

        assert len(alerts) == 0

    @pytest.mark.asyncio
    async def test_disconnected_not_flagged(self, make_watchdog):
        alerts = []

        async def _alert(agent, msg):
            alerts.append(msg)

        wd = make_watchdog(alert_fn=_alert)

        now = time.time()
        wd._states["a"] = _AgentState(
            last_progress_turns=5,
            last_progress_activity="",
            last_progress_at=now - 700,
        )

        snap = _SessionSnapshot(
            agent_name="a", label="main", connected=False,
            turns=5, pending=5, current_activity="",
            sample_time=now,
        )
        await wd._evaluate(snap, now)
        assert len(alerts) == 0

    @pytest.mark.asyncio
    async def test_alert_mode_no_recover(self, make_watchdog):
        """In alert mode, warn but don't recover."""
        recoveries = []
        alerts = []

        async def _recover(agent, label, reason):
            recoveries.append(agent)

        async def _alert(agent, msg):
            alerts.append(agent)

        wd = make_watchdog(
            recover_fn=_recover,
            alert_fn=_alert,
            agent_config_fn=lambda _: WatchdogConfig(mode="alert"),
        )

        now = time.time()
        wd._states["a"] = _AgentState(
            last_progress_turns=5,
            last_progress_activity="Edit",
            last_progress_at=now - 1000,
        )

        snap = _SessionSnapshot(
            agent_name="a", label="main", connected=True,
            turns=5, pending=3, current_activity="Edit",
            sample_time=now,
        )
        await wd._evaluate(snap, now)

        assert len(alerts) == 1
        assert len(recoveries) == 0


class TestStatus:
    def test_status_empty(self, make_watchdog):
        wd = make_watchdog()
        s = wd.status()
        assert s["running"] is False
        assert s["agents"] == {}

    def test_status_with_state(self, make_watchdog):
        wd = make_watchdog()
        wd._states["barsik"] = _AgentState(
            last_progress_turns=10,
            last_progress_activity="Bash",
            last_progress_at=time.time() - 30,
        )
        s = wd.status()
        assert "barsik" in s["agents"]
        assert s["agents"]["barsik"]["last_progress_turns"] == 10
        assert s["agents"]["barsik"]["stale_seconds"] >= 29
