"""Tests for session_watchdog module."""
from __future__ import annotations

import time

import pytest

from pinky_daemon.session_watchdog import (
    DEFAULT_MCP_CHECKABLE_HEARTBEAT_MAX_AGE,
    DEFAULT_MCP_UNBOUND_FLOOR,
    SessionWatchdog,
    WatchdogConfig,
    _AgentState,
    _SessionSnapshot,
    compute_mcp_checkable,
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

    def test_snapshot_pending_messages_fallback(self, make_watchdog):
        """Codex sessions expose pending_messages instead of pending_responses."""

        class CodexLikeSession:
            @property
            def stats(self):
                return {
                    "turns": 3,
                    "pending_messages": 7,
                    "connected": True,
                    "current_activity": "Bash",
                }

        wd = make_watchdog()
        snap = wd._take_snapshot("codex-agent", "main", CodexLikeSession())
        assert snap.pending == 7


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
        assert ("a", "main") in wd._states
        assert wd._states[("a", "main")].last_progress_turns == 1

        # Progress: turns increased
        snap2 = _SessionSnapshot(
            agent_name="a", label="main", connected=True,
            turns=2, pending=0, current_activity="Edit",
            sample_time=time.time(),
        )
        await wd._evaluate(snap2, time.time())
        assert wd._states[("a", "main")].last_progress_turns == 2
        assert wd._states[("a", "main")].warned is False

    @pytest.mark.asyncio
    async def test_stuck_triggers_warn(self, make_watchdog):
        alerts = []

        async def _alert(agent, msg):
            alerts.append((agent, msg))

        wd = make_watchdog(alert_fn=_alert)

        # Set up stale state
        now = time.time()
        wd._states[("a", "main")] = _AgentState(
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

        assert wd._states[("a", "main")].warned is True
        assert len(alerts) == 1
        assert "stuck" in alerts[0][1]

    @pytest.mark.asyncio
    async def test_stuck_triggers_recover(self, make_watchdog):
        recoveries = []

        async def _recover(agent, label, reason):
            recoveries.append((agent, label, reason))

        wd = make_watchdog(recover_fn=_recover)

        now = time.time()
        wd._states[("a", "main")] = _AgentState(
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
        wd._states[("a", "main")] = _AgentState(
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
        wd._states[("a", "main")] = _AgentState(
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
        wd._states[("a", "main")] = _AgentState(
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

    @pytest.mark.asyncio
    async def test_multi_session_isolation(self, make_watchdog):
        """Activity in one label should not reset stale timer for another."""
        alerts = []

        async def _alert(agent, msg):
            alerts.append((agent, msg))

        wd = make_watchdog(alert_fn=_alert)

        now = time.time()
        # Label "worker" is stuck
        wd._states[("a", "worker")] = _AgentState(
            last_progress_turns=5,
            last_progress_activity="Edit — big.html",
            last_progress_at=now - 700,
        )

        # Label "main" makes progress — should NOT affect "worker"
        snap_main = _SessionSnapshot(
            agent_name="a", label="main", connected=True,
            turns=10, pending=0, current_activity="Read",
            sample_time=now,
        )
        await wd._evaluate(snap_main, now)

        # Worker should still be stale
        snap_worker = _SessionSnapshot(
            agent_name="a", label="worker", connected=True,
            turns=5, pending=2, current_activity="Edit — big.html",
            sample_time=now,
        )
        await wd._evaluate(snap_worker, now)

        assert wd._states[("a", "worker")].warned is True
        assert len(alerts) == 1
        assert "stuck" in alerts[0][1]


class TestStatus:
    def test_status_empty(self, make_watchdog):
        wd = make_watchdog()
        s = wd.status()
        assert s["running"] is False
        assert s["agents"] == {}

    def test_status_with_state(self, make_watchdog):
        wd = make_watchdog()
        wd._states[("barsik", "main")] = _AgentState(
            last_progress_turns=10,
            last_progress_activity="Bash",
            last_progress_at=time.time() - 30,
        )
        s = wd.status()
        assert "barsik/main" in s["agents"]
        assert s["agents"]["barsik/main"]["last_progress_turns"] == 10
        assert s["agents"]["barsik/main"]["stale_seconds"] >= 29


# ── #109: lifecycle transition-age watchdog ─────────────────────────


class TestSnapshotState:
    def test_derives_connected_from_state_when_absent(self, make_watchdog):
        """tmux stats expose `state` but not `connected` — derive it so the
        progress watchdog doesn't treat every tmux session as disconnected."""

        class TmuxLikeSession:
            @property
            def stats(self):
                return {
                    "turns": 4,
                    "pending_responses": 0,
                    "current_activity": "Bash",
                    "state": "connected",
                }

        wd = make_watchdog()
        snap = wd._take_snapshot("tmux-agent", "main", TmuxLikeSession())
        assert snap.state == "connected"
        assert snap.connected is True

    def test_derived_connected_false_for_transition_state(self, make_watchdog):
        class TmuxLikeSession:
            @property
            def stats(self):
                return {"state": "reconnecting", "turns": 0}

        wd = make_watchdog()
        snap = wd._take_snapshot("tmux-agent", "main", TmuxLikeSession())
        assert snap.state == "reconnecting"
        assert snap.connected is False

    def test_explicit_connected_preserved(self, make_watchdog):
        """Streaming/Codex expose `connected` directly — keep their value
        even when `state` is also present."""

        class StreamingLike:
            @property
            def stats(self):
                return {"state": "reconnecting", "connected": False, "turns": 1}

        wd = make_watchdog()
        snap = wd._take_snapshot("s", "main", StreamingLike())
        assert snap.connected is False
        assert snap.state == "reconnecting"


def _transition_snap(state, *, agent="a", label="main", connected=False, pending=0):
    return _SessionSnapshot(
        agent_name=agent, label=label, connected=connected,
        turns=0, pending=pending, current_activity="",
        sample_time=time.time(), state=state,
    )


class TestLifecycleTransition:
    def test_config_transition_defaults(self):
        cfg = WatchdogConfig()
        assert cfg.transition_warn_after_seconds == 240
        assert cfg.transition_recover_after_seconds == 360

    @pytest.mark.asyncio
    async def test_first_observation_starts_timer_no_warn(self, make_watchdog):
        """First sweep observing a transition starts the sampled timer; no
        age has accrued, so it must not warn/recover yet."""
        alerts = []

        async def _alert(a, m):
            alerts.append(m)

        wd = make_watchdog(alert_fn=_alert)
        now = time.time()
        await wd._evaluate(_transition_snap("reconnecting"), now)
        st = wd._states[("a", "main")]
        assert st.transition_state == "reconnecting"
        assert abs(st.transition_since - now) < 1.0
        assert st.transition_warned is False
        assert alerts == []

    @pytest.mark.asyncio
    async def test_booting_warns_without_backlog(self, make_watchdog):
        """BOOTING warns despite connected=False and zero backlog; in alert
        mode it warns but does not recover."""
        alerts, recoveries = [], []

        async def _alert(a, m):
            alerts.append(m)

        async def _recover(a, label, r):
            recoveries.append(a)

        wd = make_watchdog(
            alert_fn=_alert, recover_fn=_recover,
            agent_config_fn=lambda _: WatchdogConfig(mode="alert"),
        )
        now = time.time()
        wd._states[("a", "main")] = _AgentState(
            transition_state="booting", transition_since=now - 300,
        )
        await wd._evaluate(_transition_snap("booting"), now)
        assert len(alerts) == 1
        assert "booting" in alerts[0]
        assert wd._states[("a", "main")].transition_warned is True
        assert recoveries == []

    @pytest.mark.asyncio
    async def test_reconnecting_recovers_once(self, make_watchdog):
        recoveries = []

        async def _recover(a, label, r):
            recoveries.append((a, label, r))

        wd = make_watchdog(recover_fn=_recover)  # default mode == "recover"
        now = time.time()
        wd._states[("a", "main")] = _AgentState(
            transition_state="reconnecting", transition_since=now - 400,
            transition_warned=True,
        )
        await wd._evaluate(_transition_snap("reconnecting"), now)
        assert len(recoveries) == 1
        st = wd._states[("a", "main")]
        assert st.transition_recovered_at > 0
        assert st.transition_state == ""  # cleared after recovery

    @pytest.mark.asyncio
    async def test_recover_grace_prevents_refire(self, make_watchdog):
        """After a recovery, the fresh session's own BOOTING must not
        immediately re-trip within the grace window."""
        recoveries = []

        async def _recover(a, label, r):
            recoveries.append(a)

        wd = make_watchdog(recover_fn=_recover)
        now = time.time()
        wd._states[("a", "main")] = _AgentState(
            transition_state="booting", transition_since=now - 400,
            transition_recovered_at=now - 10,  # just recovered
        )
        await wd._evaluate(_transition_snap("booting"), now)
        assert recoveries == []

    @pytest.mark.asyncio
    async def test_transition_state_change_resets_timer(self, make_watchdog):
        alerts = []

        async def _alert(a, m):
            alerts.append(m)

        wd = make_watchdog(alert_fn=_alert)
        now = time.time()
        wd._states[("a", "main")] = _AgentState(
            transition_state="booting", transition_since=now - 300,
        )
        await wd._evaluate(_transition_snap("reconnecting"), now)
        st = wd._states[("a", "main")]
        assert st.transition_state == "reconnecting"
        assert abs(st.transition_since - now) < 1.0  # reset
        assert alerts == []

    @pytest.mark.asyncio
    async def test_exit_to_connected_clears_and_progress_runs(self, make_watchdog):
        wd = make_watchdog()
        now = time.time()
        wd._states[("a", "main")] = _AgentState(
            last_progress_turns=0,
            transition_state="reconnecting", transition_since=now - 300,
        )
        snap = _SessionSnapshot(
            agent_name="a", label="main", connected=True,
            turns=5, pending=0, current_activity="Edit",
            sample_time=now, state="connected",
        )
        await wd._evaluate(snap, now)
        st = wd._states[("a", "main")]
        assert st.transition_state == ""
        assert st.transition_since == 0.0
        assert st.last_progress_turns == 5  # normal progress logic ran

    @pytest.mark.asyncio
    @pytest.mark.parametrize("rest_state", ["idle_sleeping", "dead", "uninitialized"])
    async def test_non_transition_states_no_action(self, make_watchdog, rest_state):
        """IDLE_SLEEPING/DEAD/UNINITIALIZED are not transition-wedges — the
        transition branch must not warn or recover for them (even with backlog)."""
        alerts, recoveries = [], []

        async def _alert(a, m):
            alerts.append(m)

        async def _recover(a, label, r):
            recoveries.append(a)

        wd = make_watchdog(alert_fn=_alert, recover_fn=_recover)
        now = time.time()
        await wd._evaluate(
            _transition_snap(rest_state, connected=False, pending=5), now
        )
        assert alerts == []
        assert recoveries == []
        assert wd._states[("a", "main")].transition_state == ""

    @pytest.mark.asyncio
    async def test_recovery_uses_callback_not_force_restart(self, make_watchdog):
        """Transition recovery goes through the generic _recover_fn (the
        already-hard recovery path). The watchdog must never drive
        force_restart — that would request the wrong edge through the wedged
        owner. Pinned against future regressions."""
        force_restart_calls = []
        recoveries = []

        class FakeTmuxSession:
            async def force_restart(self, *, bypass_guard=False):
                force_restart_calls.append(bypass_guard)
                return True

        async def _recover(a, label, r):
            recoveries.append(a)  # hard recovery path — no force_restart

        wd = make_watchdog(recover_fn=_recover)
        now = time.time()
        wd._states[("a", "main")] = _AgentState(
            transition_state="reconnecting", transition_since=now - 400,
            transition_warned=True,
        )
        await wd._evaluate(_transition_snap("reconnecting"), now)
        assert recoveries == ["a"]
        assert force_restart_calls == []


class TestConfigMerge:
    """WatchdogConfig.from_raw is the single merge seam used by the API layer
    (_get_watchdog_config). Pins that per-agent overrides — including the
    #109 transition thresholds — are actually threaded through."""

    def test_from_raw_empty_returns_defaults(self):
        assert WatchdogConfig.from_raw(None) == WatchdogConfig()
        assert WatchdogConfig.from_raw({}) == WatchdogConfig()

    def test_from_raw_merges_transition_thresholds(self):
        cfg = WatchdogConfig.from_raw({
            "transition_warn_after_seconds": 90,
            "transition_recover_after_seconds": 150,
        })
        assert cfg.transition_warn_after_seconds == 90
        assert cfg.transition_recover_after_seconds == 150
        # Untouched fields keep their defaults.
        assert cfg.mode == "recover"
        assert cfg.warn_after_seconds == 600

    def test_from_raw_merges_legacy_fields(self):
        cfg = WatchdogConfig.from_raw({"mode": "alert", "min_pending": 3})
        assert cfg.mode == "alert"
        assert cfg.min_pending == 3
        # New transition fields fall back to defaults when unspecified.
        assert cfg.transition_warn_after_seconds == 240
        assert cfg.transition_recover_after_seconds == 360

    def test_from_raw_ignores_unknown_keys(self):
        cfg = WatchdogConfig.from_raw({"bogus_key": 123, "mode": "alert"})
        assert cfg.mode == "alert"

    def test_from_raw_merges_mcp_recover(self):
        assert WatchdogConfig.from_raw({"mcp_recover": True}).mcp_recover is True
        assert WatchdogConfig.from_raw({}).mcp_recover is False  # default OFF


def _conn_snap(agent="a", connected=True):
    """A connected (or not) session snapshot for MCP-bind tests."""
    return _SessionSnapshot(
        agent_name=agent, label="main", connected=connected,
        turns=0, pending=0, current_activity="", sample_time=time.time(),
        state="connected" if connected else "idle",
    )


class TestMcpBindRecovery:
    """#663 — watchdog force-fresh recovery of an MCP-unbound session."""

    def _wd(self, make_watchdog, *, status, recovered):
        async def rec(agent, label, reason):
            recovered.append((agent, label, reason))
        return make_watchdog(
            mcp_bind_status_fn=lambda n: status,
            mcp_recover_fn=rec,
        )

    @pytest.mark.asyncio
    async def test_unbound_recovers_after_sustained_deadline(self, make_watchdog):
        recovered = []
        wd = self._wd(make_watchdog, recovered=recovered,
                      status={"checkable": True, "bound": False, "heartbeat_interval": 60})
        cfg = WatchdogConfig(mcp_recover=True)
        st = _AgentState()
        snap = _conn_snap()
        now = 1000.0
        # First observation only arms the clock — no recovery.
        assert await wd._evaluate_mcp_bind(snap, st, cfg, now) is False
        assert st.mcp_unbound_since == now
        # Still within deadline — no recovery.
        assert await wd._evaluate_mcp_bind(snap, st, cfg, now + 100) is False
        assert recovered == []
        # Sustained past the deadline — force-fresh recover fires.
        assert await wd._evaluate_mcp_bind(
            snap, st, cfg, now + DEFAULT_MCP_UNBOUND_FLOOR + 1) is True
        assert recovered and recovered[0][0] == "a"
        assert st.mcp_unbound_since == 0.0
        assert st.mcp_recovered_at == now + DEFAULT_MCP_UNBOUND_FLOOR + 1

    @pytest.mark.asyncio
    async def test_bound_clears_clock_no_recover(self, make_watchdog):
        recovered = []
        wd = self._wd(make_watchdog, recovered=recovered,
                      status={"checkable": True, "bound": True, "heartbeat_interval": 60})
        st = _AgentState(mcp_unbound_since=500.0)
        assert await wd._evaluate_mcp_bind(
            _conn_snap(), st, WatchdogConfig(mcp_recover=True), 2000.0) is False
        assert st.mcp_unbound_since == 0.0
        assert recovered == []

    @pytest.mark.asyncio
    async def test_heartbeat_disabled_never_trips(self, make_watchdog):
        recovered = []
        wd = self._wd(make_watchdog, recovered=recovered,
                      status={"checkable": False, "bound": False, "heartbeat_interval": 0})
        st = _AgentState(mcp_unbound_since=1.0)
        assert await wd._evaluate_mcp_bind(
            _conn_snap(), st, WatchdogConfig(mcp_recover=True), 1e9) is False
        assert st.mcp_unbound_since == 0.0
        assert recovered == []

    @pytest.mark.asyncio
    async def test_flag_off_never_recovers(self, make_watchdog):
        recovered = []
        wd = self._wd(make_watchdog, recovered=recovered,
                      status={"checkable": True, "bound": False, "heartbeat_interval": 60})
        st = _AgentState(mcp_unbound_since=1.0)
        assert await wd._evaluate_mcp_bind(
            _conn_snap(), st, WatchdogConfig(mcp_recover=False), 1e9) is False
        assert recovered == []

    @pytest.mark.asyncio
    async def test_disconnected_clears_and_skips(self, make_watchdog):
        recovered = []
        wd = self._wd(make_watchdog, recovered=recovered,
                      status={"checkable": True, "bound": False, "heartbeat_interval": 60})
        st = _AgentState(mcp_unbound_since=1.0)
        assert await wd._evaluate_mcp_bind(
            _conn_snap(connected=False), st, WatchdogConfig(mcp_recover=True), 1e9) is False
        assert st.mcp_unbound_since == 0.0
        assert recovered == []

    @pytest.mark.asyncio
    async def test_post_recover_grace_blocks_immediate_retrip(self, make_watchdog):
        recovered = []
        wd = self._wd(make_watchdog, recovered=recovered,
                      status={"checkable": True, "bound": False, "heartbeat_interval": 60})
        now = 5000.0
        st = _AgentState(mcp_unbound_since=1.0, mcp_recovered_at=now)
        assert await wd._evaluate_mcp_bind(
            _conn_snap(), st, WatchdogConfig(mcp_recover=True), now + 10) is False
        assert recovered == []

    @pytest.mark.asyncio
    async def test_global_rate_limit_defers_then_allows(self, make_watchdog):
        recovered = []
        wd = self._wd(make_watchdog, recovered=recovered,
                      status={"checkable": True, "bound": False, "heartbeat_interval": 60})
        cfg = WatchdogConfig(mcp_recover=True)
        now = 9000.0
        wd._last_mcp_recover_at = now  # a fleet recovery just happened
        st = _AgentState(mcp_unbound_since=now - 1000)  # long unbound, past deadline
        # Within the global min-interval — deferred even though this agent qualifies.
        assert await wd._evaluate_mcp_bind(_conn_snap("b"), st, cfg, now + 10) is False
        assert recovered == []
        # After the global interval elapses — allowed.
        assert await wd._evaluate_mcp_bind(_conn_snap("b"), st, cfg, now + 200) is True
        assert recovered and recovered[0][0] == "b"

    @pytest.mark.asyncio
    async def test_status_error_is_swallowed(self, make_watchdog):
        recovered = []

        def boom(_n):
            raise RuntimeError("status backend down")

        async def rec(a, _l, _r):
            recovered.append(a)

        wd = make_watchdog(mcp_bind_status_fn=boom, mcp_recover_fn=rec)
        st = _AgentState(mcp_unbound_since=1.0)
        assert await wd._evaluate_mcp_bind(
            _conn_snap(), st, WatchdogConfig(mcp_recover=True), 1e9) is False
        assert recovered == []

    @pytest.mark.asyncio
    async def test_recover_failure_does_not_burn_ratelimit(self, make_watchdog):
        async def rec(_a, _l, _r):
            raise RuntimeError("connect failed")

        wd = make_watchdog(
            mcp_bind_status_fn=lambda n: {
                "checkable": True, "bound": False, "heartbeat_interval": 60},
            mcp_recover_fn=rec,
        )
        now = 7000.0
        st = _AgentState(mcp_unbound_since=now - 1000)
        assert await wd._evaluate_mcp_bind(
            _conn_snap(), st, WatchdogConfig(mcp_recover=True), now) is False
        assert wd._last_mcp_recover_at == 0.0  # not burned → next sweep retries
        assert st.mcp_recovered_at == 0.0  # not marked recovered

    @pytest.mark.asyncio
    async def test_evaluate_routes_to_mcp_branch(self, make_watchdog):
        recovered = []

        async def rec(a, _l, _r):
            recovered.append(a)

        wd = make_watchdog(
            mcp_bind_status_fn=lambda n: {
                "checkable": True, "bound": False, "heartbeat_interval": 60},
            mcp_recover_fn=rec,
            agent_config_fn=lambda n: WatchdogConfig(mcp_recover=True),
        )
        snap = _conn_snap()
        now = 3000.0
        await wd._evaluate(snap, now)  # arms the unbound clock
        assert recovered == []
        await wd._evaluate(snap, now + DEFAULT_MCP_UNBOUND_FLOOR + 5)  # fires
        assert recovered == ["a"]

    @pytest.mark.asyncio
    async def test_evaluate_mcp_branch_runs_when_watchdog_disabled(self, make_watchdog):
        """R1 (#663): mcp_recover is its own opt-in — the MCP-bind branch fires
        even when the master watchdog (cfg.enabled) is OFF."""
        recovered = []

        async def rec(a, _l, _r):
            recovered.append(a)

        wd = make_watchdog(
            mcp_bind_status_fn=lambda n: {
                "checkable": True, "bound": False, "heartbeat_interval": 0},
            mcp_recover_fn=rec,
            # The exact barsik shape: progress watchdog disabled, mcp_recover on.
            agent_config_fn=lambda n: WatchdogConfig(enabled=False, mcp_recover=True),
        )
        snap = _conn_snap()
        now = 3000.0
        await wd._evaluate(snap, now)  # arms the unbound clock
        assert recovered == []
        await wd._evaluate(snap, now + DEFAULT_MCP_UNBOUND_FLOOR + 5)  # fires
        assert recovered == ["a"]

    @pytest.mark.asyncio
    async def test_evaluate_disabled_without_mcp_recover_is_noop(self, make_watchdog):
        """A fully-disabled agent (enabled=False, mcp_recover=False) returns
        before any work — no MCP-bind lookup, no tracked state entry."""
        calls = []
        wd = make_watchdog(
            mcp_bind_status_fn=lambda n: calls.append(n) or {
                "checkable": True, "bound": False, "heartbeat_interval": 0},
            mcp_recover_fn=lambda *a: None,
            agent_config_fn=lambda n: WatchdogConfig(enabled=False, mcp_recover=False),
        )
        await wd._evaluate(_conn_snap(), 1000.0)
        assert calls == []  # status fn never consulted
        assert ("a", "main") not in wd._states  # no state created for a no-op agent

    @pytest.mark.asyncio
    async def test_disabled_watchdog_skips_progress_logic(self, make_watchdog):
        """With enabled=False + mcp_recover=True, the progress/backlog watchdog
        stays gated OFF — a maximally-stuck session never warns or recovers via
        the generic path; only the MCP-bind branch is live."""
        recovered, alerts = [], []

        async def rec(a, _l, _r):
            recovered.append(a)

        async def alert(a, _m):
            alerts.append(a)

        wd = make_watchdog(
            recover_fn=rec,
            alert_fn=alert,
            # bound=True → MCP branch is a no-op, isolating the progress gate.
            mcp_bind_status_fn=lambda n: {
                "checkable": True, "bound": True, "heartbeat_interval": 0},
            mcp_recover_fn=lambda *a: None,
            agent_config_fn=lambda n: WatchdogConfig(enabled=False, mcp_recover=True),
        )
        # A connected session with backlog that makes no progress across sweeps.
        stuck = _SessionSnapshot(
            agent_name="a", label="main", connected=True, turns=0, pending=3,
            current_activity="thinking", sample_time=time.time(), state="connected",
        )
        now = 1000.0
        await wd._evaluate(stuck, now)  # seed state
        # Jump far past warn_after (600) and recover_after (900).
        await wd._evaluate(stuck, now + 10_000)
        assert recovered == []  # progress-recover gated off
        assert alerts == []  # progress-warn gated off


class TestComputeMcpCheckable:
    """R2 (#663): checkable policy — recent agent-origin heartbeat covers
    heartbeat_interval==0 sidekicks (Murzik Finding #2)."""

    NOW = 10_000.0

    def test_no_agent_never_checkable(self):
        assert compute_mcp_checkable(
            agent_exists=False, epoch="e1", heartbeat_interval=60,
            latest_agent_heartbeat_ts=self.NOW, now=self.NOW) is False

    def test_no_epoch_never_checkable(self):
        # No gateway generation established yet — nothing to bind against.
        assert compute_mcp_checkable(
            agent_exists=True, epoch="", heartbeat_interval=60,
            latest_agent_heartbeat_ts=self.NOW, now=self.NOW) is False

    def test_cadence_agent_checkable_regardless_of_history(self):
        # heartbeat_interval>0 is sufficient — even with no heartbeat history.
        assert compute_mcp_checkable(
            agent_exists=True, epoch="e1", heartbeat_interval=60,
            latest_agent_heartbeat_ts=None, now=self.NOW) is True

    def test_zero_interval_recent_heartbeat_checkable(self):
        # barsik's shape: heartbeat_interval==0 but a recent agent-origin beat.
        assert compute_mcp_checkable(
            agent_exists=True, epoch="e1", heartbeat_interval=0,
            latest_agent_heartbeat_ts=self.NOW - 60, now=self.NOW) is True

    def test_zero_interval_stale_heartbeat_not_checkable(self):
        assert compute_mcp_checkable(
            agent_exists=True, epoch="e1", heartbeat_interval=0,
            latest_agent_heartbeat_ts=self.NOW - DEFAULT_MCP_CHECKABLE_HEARTBEAT_MAX_AGE - 1,
            now=self.NOW) is False

    def test_zero_interval_no_history_not_checkable(self):
        assert compute_mcp_checkable(
            agent_exists=True, epoch="e1", heartbeat_interval=0,
            latest_agent_heartbeat_ts=None, now=self.NOW) is False

    def test_window_boundary_inclusive(self):
        # Exactly at the window edge counts as fresh (<=).
        assert compute_mcp_checkable(
            agent_exists=True, epoch="e1", heartbeat_interval=0,
            latest_agent_heartbeat_ts=self.NOW - DEFAULT_MCP_CHECKABLE_HEARTBEAT_MAX_AGE,
            now=self.NOW) is True
