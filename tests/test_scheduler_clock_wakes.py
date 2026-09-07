"""Regression coverage for rate-limit gates and clock-aligned wake delivery."""

import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

import pinky_daemon.scheduler as scheduler_module
from pinky_daemon.agent_registry import AgentRegistry
from pinky_daemon.scheduler import AgentScheduler

NOW = datetime(2026, 1, 15, 20, tzinfo=timezone.utc).timestamp()


@pytest.fixture
def rate_limit_file(tmp_path, monkeypatch):
    path = tmp_path / "rate-limits.json"
    monkeypatch.setattr(scheduler_module, "_RATE_LIMIT_FILE", str(path))
    monkeypatch.setattr(scheduler_module.time, "time", lambda: NOW)
    path.write_text(
        json.dumps(
            {
                "five_hour": {"used_percentage": None},
                "seven_day": {"used_percentage": 23},
                "updated_at": NOW,
            }
        )
    )
    return path


@pytest.fixture
def clock_registry():
    registry = Mock(spec=AgentRegistry)
    registry.list.return_value = [
        SimpleNamespace(
            name="worker",
            runtime="claude_sdk",
            wake_interval=3600,
            clock_aligned=True,
            dream_timezone="UTC",
        )
    ]
    registry.get_heartbeat_prompt.return_value = "Scheduled wake"
    return registry


@pytest.mark.parametrize(
    ("five_hour", "seven_day", "age", "expected"),
    [
        pytest.param(
            {"used_percentage": None}, {"used_percentage": 23}, 0, True, id="null-five-hour"
        ),
        pytest.param(
            {"used_percentage": 23}, {"used_percentage": None}, 0, True, id="null-seven-day"
        ),
        pytest.param({"used_percentage": None}, {"used_percentage": None}, 0, True, id="both-null"),
        pytest.param(
            {"used_percentage": "unknown"}, {"used_percentage": 23}, 0, True, id="string-five-hour"
        ),
        pytest.param(
            {"used_percentage": 23}, {"used_percentage": "unknown"}, 0, True, id="string-seven-day"
        ),
        pytest.param({}, {"used_percentage": 23}, 0, True, id="missing-five-percentage"),
        pytest.param({"used_percentage": 23}, {}, 0, True, id="missing-seven-percentage"),
        pytest.param(
            {"used_percentage": 85}, {"used_percentage": 23}, 0, False, id="five-hour-over-limit"
        ),
        pytest.param(
            {"used_percentage": 23}, {"used_percentage": 85}, 0, False, id="seven-day-over-limit"
        ),
        pytest.param(
            {"used_percentage": None},
            {"used_percentage": 85},
            0,
            False,
            id="unknown-five-hour-known-seven-day-limit",
        ),
        pytest.param(
            {"used_percentage": 85},
            {"used_percentage": None},
            0,
            False,
            id="known-five-hour-limit-unknown-seven-day",
        ),
        pytest.param({"used_percentage": 80.0}, {}, 0, False, id="threshold"),
        pytest.param({"used_percentage": 79.5}, {}, 0, True, id="below-threshold"),
        pytest.param({"used_percentage": None}, {"used_percentage": 85}, 301, True, id="stale"),
    ],
)
def test_rate_limits_ok_unknown_windows(rate_limit_file, five_hour, seven_day, age, expected):
    rate_limit_file.write_text(
        json.dumps(
            {
                "five_hour": five_hour,
                "seven_day": seven_day,
                "updated_at": NOW - age,
            }
        )
    )

    assert scheduler_module._rate_limits_ok() is expected


@pytest.mark.parametrize("unknown", [None, "85", [], {}])
@pytest.mark.parametrize("window", ["five_hour", "seven_day"])
def test_rate_limits_ok_checks_known_sibling(rate_limit_file, unknown, window):
    data = {
        "five_hour": {"used_percentage": 85},
        "seven_day": {"used_percentage": 85},
        "updated_at": NOW,
    }
    data[window] = {"used_percentage": unknown}
    rate_limit_file.write_text(json.dumps(data))

    assert scheduler_module._rate_limits_ok() is False


@pytest.mark.parametrize(
    "data",
    [None, [], "invalid", {}, {"updated_at": None}, {"updated_at": "invalid"}],
)
def test_rate_limits_ok_malformed_data_fails_open(rate_limit_file, data):
    rate_limit_file.write_text(json.dumps(data))

    assert scheduler_module._rate_limits_ok() is True


@pytest.mark.parametrize("window", ["five_hour", "seven_day"])
@pytest.mark.parametrize("malformed", [None, [], "invalid"])
def test_rate_limits_ok_malformed_window_preserves_known_limit(rate_limit_file, window, malformed):
    data = {
        "five_hour": {"used_percentage": 85},
        "seven_day": {"used_percentage": 85},
        "updated_at": NOW,
    }
    data[window] = malformed
    rate_limit_file.write_text(json.dumps(data))

    assert scheduler_module._rate_limits_ok() is False


@pytest.mark.parametrize("content", [b"{", b"\xff"])
def test_rate_limits_ok_unreadable_data_fails_open(rate_limit_file, content):
    rate_limit_file.write_bytes(content)

    assert scheduler_module._rate_limits_ok() is True


def test_rate_limits_ok_missing_file_fails_open(rate_limit_file):
    rate_limit_file.unlink()

    assert scheduler_module._rate_limits_ok() is True


async def test_clock_wake_queues_with_null_window(rate_limit_file, clock_registry):
    queued = []

    async def wake(name, session_id, prompt):
        queued.append((name, session_id, prompt))

    scheduler = AgentScheduler(clock_registry, wake_callback=wake)
    await scheduler._check_clock_aligned_wakes(NOW)

    assert len(queued) == 1
    assert queued[0][:2] == ("worker", "worker-main")
    assert queued[0][2].endswith("Scheduled wake")
    assert scheduler._last_clock_slot == {"worker": 720}

    await scheduler._check_clock_aligned_wakes(NOW + 30)
    assert len(queued) == 1


async def test_clock_wake_rate_limit_skip_retries_same_slot(clock_registry, monkeypatch):
    monkeypatch.setattr(scheduler_module, "_rate_limits_ok", Mock(side_effect=[False, True]))
    queued = []

    async def wake(name, session_id, prompt):
        queued.append(name)

    scheduler = AgentScheduler(clock_registry, wake_callback=wake)
    await scheduler._check_clock_aligned_wakes(NOW)

    assert queued == []
    assert scheduler._last_clock_slot == {}

    await scheduler._check_clock_aligned_wakes(NOW + 30)
    assert queued == ["worker"]
    assert scheduler._last_clock_slot == {"worker": 720}


async def test_clock_wake_without_callback_preserves_slot(clock_registry, monkeypatch):
    monkeypatch.setattr(scheduler_module, "_rate_limits_ok", lambda: True)
    scheduler = AgentScheduler(clock_registry)

    await scheduler._check_clock_aligned_wakes(NOW)

    assert scheduler._last_clock_slot == {}


async def test_clock_wake_false_callback_result_consumes_slot(clock_registry, monkeypatch):
    monkeypatch.setattr(scheduler_module, "_rate_limits_ok", lambda: True)
    wake = AsyncMock(return_value=False)
    scheduler = AgentScheduler(clock_registry, wake_callback=wake)

    await scheduler._check_clock_aligned_wakes(NOW)

    wake.assert_awaited_once()
    assert scheduler._last_clock_slot == {"worker": 720}

    await scheduler._check_clock_aligned_wakes(NOW + 30)

    wake.assert_awaited_once()


async def test_clock_wake_callback_failure_retries_same_slot(clock_registry, monkeypatch, capsys):
    monkeypatch.setattr(scheduler_module, "_rate_limits_ok", lambda: True)
    queued = []
    attempts = 0

    async def wake(name, session_id, prompt):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("queue unavailable")
        queued.append(name)

    scheduler = AgentScheduler(clock_registry, wake_callback=wake)
    await scheduler._check_clock_aligned_wakes(NOW)

    assert queued == []
    assert "worker" not in scheduler._last_clock_slot
    assert "clock-aligned wake failed for worker: queue unavailable" in capsys.readouterr().err

    await scheduler._check_clock_aligned_wakes(NOW + 30)

    assert attempts == 2
    assert queued == ["worker"]
    assert scheduler._last_clock_slot == {"worker": 720}


async def test_clock_wake_gate_exception_continues_same_tick(clock_registry, monkeypatch, capsys):
    clock_registry.list.return_value.append(
        SimpleNamespace(
            name="worker-next",
            runtime="claude_sdk",
            wake_interval=3600,
            clock_aligned=True,
            dream_timezone="UTC",
        )
    )
    gate = Mock(side_effect=[RuntimeError("gate unavailable"), True, True])
    monkeypatch.setattr(scheduler_module, "_rate_limits_ok", gate)
    queued = []

    async def wake(name, session_id, prompt):
        queued.append(name)

    scheduler = AgentScheduler(clock_registry, wake_callback=wake)
    monkeypatch.setattr(scheduler_module.time, "time", lambda: NOW)
    for method in (
        "_run_outbox_reaper_if_due",
        "_warn_oversized_schedule_prompts",
        "_check_pending_wake_liveness",
        "_cleanup_expired_messages",
    ):
        monkeypatch.setattr(scheduler, method, Mock())
    later_checks = {}
    for method in (
        "_check_schedules",
        "_check_heartbeats",
        "_check_auto_sleep",
        "_check_idle_sessions",
        "_check_dreams",
        "_check_librarian",
        "_check_url_watchers",
    ):
        later_checks[method] = AsyncMock()
        monkeypatch.setattr(scheduler, method, later_checks[method])

    await scheduler._tick()

    assert queued == ["worker-next"]
    assert gate.call_count == 2
    assert scheduler._last_clock_slot == {"worker-next": 720}
    assert "clock-aligned wake failed for worker: gate unavailable" in capsys.readouterr().err
    for check in later_checks.values():
        check.assert_awaited_once_with(NOW)

    monkeypatch.setattr(scheduler_module.time, "time", lambda: NOW + 30)
    await scheduler._tick()
    assert queued == ["worker-next", "worker"]
    assert scheduler._last_clock_slot == {"worker-next": 720, "worker": 720}
