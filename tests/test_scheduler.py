"""Tests for agent scheduler, heartbeats, schedules, and session types."""

from __future__ import annotations

import asyncio
import os
import sqlite3
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import pytest

from pinky_daemon.agent_registry import AgentRegistry, ScheduleNameConflictError
from pinky_daemon.scheduler import (
    _SCHEDULE_PROMPT_WARN_INTERVAL_SEC,
    AgentScheduler,
    ScheduleWakeReceipt,
    cron_matches,
    next_cron_description,
)

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


class TestCronRobustness:
    """Malformed crons must never raise (one bad schedule would abort the
    whole scheduler tick), and standard name/range-step tokens must parse."""

    def test_day_name_token(self):
        dt_monday = datetime(2026, 3, 23, 9, 0)  # Monday
        dt_tuesday = datetime(2026, 3, 24, 9, 0)  # Tuesday
        assert cron_matches("0 9 * * mon", dt_monday) is True
        assert cron_matches("0 9 * * mon", dt_tuesday) is False
        assert cron_matches("0 9 * * mon-fri", dt_monday) is True

    def test_month_name_token(self):
        dt = datetime(2026, 3, 27, 8, 0)
        assert cron_matches("0 8 * mar *", dt) is True
        assert cron_matches("0 8 * apr *", dt) is False

    def test_range_with_step(self):
        assert cron_matches("1-5/2 * * * *", datetime(2026, 3, 23, 9, 3)) is True
        assert cron_matches("1-5/2 * * * *", datetime(2026, 3, 23, 9, 4)) is False
        assert cron_matches("1-5/2 * * * *", datetime(2026, 3, 23, 9, 7)) is False

    def test_malformed_cron_returns_false_instead_of_raising(self):
        dt = datetime(2026, 3, 23, 9, 0)
        assert cron_matches("0 9 * * funky", dt) is False
        assert cron_matches("*/x * * * *", dt) is False
        assert cron_matches("0 9 * * 1-x", dt) is False
        assert cron_matches("*/0 * * * *", dt) is False


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

    def test_add_schedule_rejects_enabled_duplicate_name(self, registry):
        registry.register("oleg")
        schedule = registry.add_schedule("oleg", "0 8 * * *", name="morning")

        with pytest.raises(
            ScheduleNameConflictError,
            match=rf"distinct name.*update_wake_schedule with ID {schedule.id}",
        ):
            registry.add_schedule("oleg", "0 9 * * *", name="morning")

        assert [row.id for row in registry.get_schedules("oleg")] == [schedule.id]

    @pytest.mark.parametrize("one_shot", [False, True])
    def test_add_schedule_reuses_disabled_name(self, registry, one_shot):
        registry.register("oleg")
        old = registry.add_schedule(
            "oleg",
            "0 8 * * *",
            name="morning",
            one_shot=one_shot,
        )
        registry.toggle_schedule(old.id, False)

        new = registry.add_schedule("oleg", "0 9 * * *", name="morning")

        assert new.id != old.id
        assert new.enabled is True

    def test_add_schedule_rejects_invalid_cron(self, registry):
        registry.register("oleg")

        with pytest.raises(ValueError, match="five fields"):
            registry.add_schedule("oleg", "not a cron", name="broken")

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

    def test_get_oversized_enabled_schedule_prompts_filters_and_orders(self, registry):
        registry.register("oleg")
        exact = registry.add_schedule(
            "oleg", "0 8 * * *", name="exact", prompt="x" * 8_000
        )
        smaller_hit = registry.add_schedule(
            "oleg", "0 9 * * *", name="large", prompt="x" * 8_001
        )
        larger_hit = registry.add_schedule(
            "oleg", "0 10 * * *", name="larger", prompt="x" * 20_001
        )
        disabled = registry.add_schedule(
            "oleg", "0 11 * * *", name="disabled", prompt="x" * 30_000
        )
        registry.toggle_schedule(disabled.id, False)

        rows = registry.get_oversized_enabled_schedule_prompts(8_000)

        assert rows == [
            (larger_hit.id, "oleg", "larger", 20_001),
            (smaller_hit.id, "oleg", "large", 8_001),
        ]
        assert exact.id not in {row[0] for row in rows}

    def test_remove_schedule(self, registry):
        registry.register("oleg")
        s = registry.add_schedule("oleg", "0 8 * * *")
        assert registry.remove_schedule(s.id) is True
        assert registry.get_schedules("oleg") == []

    def test_update_schedule_partial_preserves_id_and_omitted_fields(self, registry):
        registry.register("oleg")
        schedule = registry.add_schedule(
            "oleg",
            "0 8 * * *",
            name="morning",
            prompt="Original",
            direct_send=True,
            target_channel="123",
            one_shot=True,
        )

        updated = registry.update_schedule(
            schedule.id,
            cron="30 8 * * 1-5",
            prompt="Updated",
            direct_send=False,
            target_channel="",
        )

        assert updated is not None
        assert updated.id == schedule.id
        assert updated.name == "morning"
        assert updated.cron == "30 8 * * 1-5"
        assert updated.prompt == "Updated"
        assert updated.timezone == "America/Los_Angeles"
        assert updated.direct_send is False
        assert updated.target_channel == ""
        assert updated.one_shot is True

    def test_update_schedule_supports_remaining_fields_and_empty_prompt(self, registry):
        registry.register("oleg")
        schedule = registry.add_schedule(
            "oleg",
            "0 8 * * *",
            name="morning",
            prompt="Original",
            one_shot=True,
        )

        updated = registry.update_schedule(
            schedule.id,
            name="weekday",
            prompt="",
            timezone="UTC",
            one_shot=False,
        )

        assert updated is not None
        assert updated.name == "weekday"
        assert updated.prompt == ""
        assert updated.timezone == "UTC"
        assert updated.one_shot is False

    def test_update_schedule_refuses_empty_update(self, registry):
        registry.register("oleg")
        schedule = registry.add_schedule("oleg", "0 8 * * *")

        with pytest.raises(ValueError, match="at least one field"):
            registry.update_schedule(schedule.id)

    def test_update_schedule_rejects_invalid_cron_without_mutating(self, registry):
        registry.register("oleg")
        schedule = registry.add_schedule("oleg", "0 8 * * *")

        with pytest.raises(ValueError, match="Invalid cron"):
            registry.update_schedule(schedule.id, cron="99 * * * *")

        assert registry.get_schedules("oleg")[0].cron == "0 8 * * *"

    def test_update_schedule_missing(self, registry):
        assert registry.update_schedule(999, prompt="new") is None

    def test_update_schedule_rejects_enabled_name_collision(self, registry):
        registry.register("oleg")
        existing = registry.add_schedule("oleg", "0 8 * * *", name="morning")
        renamed = registry.add_schedule("oleg", "0 9 * * *", name="evening")

        with pytest.raises(ScheduleNameConflictError, match=rf"ID {existing.id}"):
            registry.update_schedule(renamed.id, name="morning")

        schedules = registry.get_schedules("oleg")
        assert [(row.id, row.name) for row in schedules] == [
            (existing.id, "morning"),
            (renamed.id, "evening"),
        ]

    def test_update_schedule_allows_self_rename(self, registry):
        registry.register("oleg")
        schedule = registry.add_schedule("oleg", "0 8 * * *", name="morning")

        updated = registry.update_schedule(schedule.id, name="morning")

        assert updated is not None
        assert updated.id == schedule.id
        assert updated.name == "morning"

    def test_remove_missing(self, registry):
        assert registry.remove_schedule(999) is False

    def test_toggle_schedule(self, registry):
        registry.register("oleg")
        s = registry.add_schedule("oleg", "0 8 * * *")
        assert registry.toggle_schedule(s.id, False) is True

        schedules = registry.get_schedules("oleg", enabled_only=False)
        assert schedules[0].enabled is False

    def test_toggle_schedule_rejects_reenable_name_collision(self, registry):
        registry.register("oleg")
        old = registry.add_schedule("oleg", "0 8 * * *", name="morning")
        assert registry.toggle_schedule(old.id, False) is True
        replacement = registry.add_schedule("oleg", "0 9 * * *", name="morning")

        with pytest.raises(ScheduleNameConflictError, match=rf"ID {replacement.id}"):
            registry.toggle_schedule(old.id, True)

        schedules = registry.get_schedules("oleg", enabled_only=False)
        assert [(row.id, row.enabled) for row in schedules] == [
            (old.id, False),
            (replacement.id, True),
        ]

    def test_toggle_schedule_enables_without_conflict(self, registry):
        registry.register("oleg")
        schedule = registry.add_schedule("oleg", "0 8 * * *", name="morning")
        assert registry.toggle_schedule(schedule.id, False) is True

        assert registry.toggle_schedule(schedule.id, True) is True
        assert registry.get_schedules("oleg")[0].id == schedule.id

    def test_update_last_run(self, registry):
        registry.register("oleg")
        s = registry.add_schedule("oleg", "0 8 * * *")
        assert s.last_run == 0.0
        assert s.last_delivered == 0.0

        now = time.time()
        assert registry.update_schedule_last_run(s.id, now) is True

        schedules = registry.get_schedules("oleg")
        assert schedules[0].last_run == pytest.approx(now, abs=0.1)
        assert schedules[0].last_delivered == 0.0

    def test_update_last_run_compare_and_swap(self, registry):
        registry.register("oleg")
        schedule = registry.add_schedule("oleg", "0 8 * * *")
        contender = AgentRegistry(db_path=registry._db_path)
        try:
            contender_snapshot = contender.get_schedules("oleg")[0]

            assert registry.update_schedule_last_run(
                schedule.id,
                100.0,
                expected_last_run=schedule.last_run,
            ) is True
            assert contender.update_schedule_last_run(
                schedule.id,
                200.0,
                expected_last_run=contender_snapshot.last_run,
            ) is False

            stored = registry.get_schedules("oleg")[0]
            assert stored.last_run == 100.0
        finally:
            contender.close()

    def test_update_last_delivered_is_distinct_from_last_run(self, registry):
        registry.register("oleg")
        schedule = registry.add_schedule("oleg", "0 8 * * *")

        delivered_at = time.time()
        registry.update_schedule_last_delivered(schedule.id, delivered_at)

        stored = registry.get_schedules("oleg")[0]
        assert stored.last_run == 0.0
        assert stored.last_delivered == pytest.approx(delivered_at, abs=0.1)
        assert stored.to_dict()["last_delivered"] == pytest.approx(
            delivered_at, abs=0.1
        )

    def test_pending_schedule_wake_is_idempotent_until_confirmed(self, registry):
        registry.register("oleg")
        schedule = registry.add_schedule(
            "oleg", "0 8 * * *", name="morning", prompt="check mail"
        )
        fired_at = time.time()
        registry.update_schedule_last_run(schedule.id, fired_at)

        first, first_created = registry.persist_schedule_wake(
            schedule.id,
            agent_name="oleg",
            schedule_name="morning",
            prompt="check mail",
            fired_at=fired_at,
        )
        duplicate, duplicate_created = registry.persist_schedule_wake(
            schedule.id,
            agent_name="oleg",
            schedule_name="morning",
            prompt="check mail",
            fired_at=fired_at,
        )

        assert first_created is True
        assert duplicate_created is False
        assert duplicate.id == first.id
        assert registry.list_pending_schedule_wakes("oleg") == [first]

        delivered_at = time.time()
        assert registry.confirm_pending_schedule_wake(
            first.id, delivered_at=delivered_at
        ) is True
        assert registry.list_pending_schedule_wakes("oleg") == []
        stored = registry.get_schedules("oleg")[0]
        assert stored.last_delivered == pytest.approx(delivered_at)

    def test_pending_schedule_wake_health_counts_active_rows_only(self, registry):
        registry.register("oleg")
        registry.register("barsik")
        first = registry.add_schedule(
            "oleg", "* * * * *", name="first", prompt="one"
        )
        second = registry.add_schedule(
            "barsik", "* * * * *", name="second", prompt="two"
        )
        old, _ = registry.persist_schedule_wake(
            first.id,
            agent_name="oleg",
            schedule_name="first",
            prompt="one",
            fired_at=100.0,
        )
        quarantined, _ = registry.persist_schedule_wake(
            first.id,
            agent_name="oleg",
            schedule_name="first",
            prompt="parked",
            fired_at=200.0,
        )
        accepted, _ = registry.persist_schedule_wake(
            second.id,
            agent_name="barsik",
            schedule_name="second",
            prompt="accepted",
            fired_at=300.0,
        )
        assert registry.park_pending_schedule_wake(quarantined.id)
        assert registry.confirm_pending_schedule_wake(
            accepted.id, delivered_at=400.0
        )

        health = registry.get_pending_schedule_wake_health(now=500.0)

        assert health == [
            {
                "agent_name": "oleg",
                "count": 1,
                "oldest_fired_at": 100.0,
                "newest_fired_at": 100.0,
                "oldest_age_seconds": 400.0,
            }
        ]
        assert old.id > 0

    def test_schedule_wake_ledger_exposes_exact_terminal_states(self, registry):
        registry.register("oleg")
        schedule = registry.add_schedule(
            "oleg", "* * * * *", name="ledger", prompt="run it"
        )
        accepted, _ = registry.persist_schedule_wake(
            schedule.id,
            agent_name="oleg",
            schedule_name="ledger",
            prompt="accepted",
            fired_at=100.0,
        )
        quarantined, _ = registry.persist_schedule_wake(
            schedule.id,
            agent_name="oleg",
            schedule_name="ledger",
            prompt="quarantined",
            fired_at=200.0,
        )
        registry.persist_schedule_wake(
            schedule.id,
            agent_name="oleg",
            schedule_name="ledger",
            prompt="pending",
            fired_at=300.0,
        )

        assert registry.confirm_pending_schedule_wake(
            accepted.id, delivered_at=150.0
        )
        assert registry.park_pending_schedule_wake(
            quarantined.id,
            parked_at=250.0,
            reason="explicit misfire quarantine",
        )

        assert [
            row.ledger_state for row in registry.list_schedule_wake_ledger("oleg")
        ] == ["pending", "quarantined", "receipted-ran-once"]
        assert [
            row.prompt
            for row in registry.list_schedule_wake_ledger(
                "oleg", state="receipted-ran-once"
            )
        ] == ["accepted"]
        assert registry.list_schedule_wake_ledger(
            "oleg", state="quarantined"
        )[0].last_error == "explicit misfire quarantine"
        assert [
            row.prompt
            for row in registry.list_schedule_wake_ledger(
                "oleg", fired_after=150.0
            )
        ] == ["pending", "quarantined"]
        assert [row.prompt for row in registry.list_pending_schedule_wakes("oleg")] == [
            "pending"
        ]

    def test_confirm_pending_wake_by_fire_missing_is_harmless(
        self, registry, capsys
    ):
        registry.register("oleg")
        schedule = registry.add_schedule(
            "oleg", "0 8 * * *", name="morning", prompt="check mail"
        )

        assert registry.confirm_pending_schedule_wake_by_fire(
            schedule.id, 100.0, delivered_at=200.0
        ) is False

        assert registry.get_schedules("oleg")[0].last_delivered == 0.0
        captured = capsys.readouterr().err
        assert "PERSISTED_WAKE_RETIRED_ON_LATE_CONFIRM" not in captured

    def test_confirm_pending_wake_by_fire_retires_parked_row(
        self, registry, capsys
    ):
        registry.register("oleg")
        schedule = registry.add_schedule(
            "oleg", "0 8 * * *", name="morning", prompt="check mail"
        )
        pending, _ = registry.persist_schedule_wake(
            schedule.id,
            agent_name="oleg",
            schedule_name="morning",
            prompt="check mail",
            fired_at=100.0,
        )
        assert registry.park_pending_schedule_wake(
            pending.id, parked_at=150.0
        ) is True

        assert registry.confirm_pending_schedule_wake_by_fire(
            schedule.id, 100.0, delivered_at=200.0
        ) is True

        assert registry.list_pending_schedule_wakes(
            "oleg", include_parked=True
        ) == []
        assert registry.get_schedules("oleg")[0].last_delivered == 200.0
        captured = capsys.readouterr().err
        assert "SCHEDULE_WAKE_RECEIPTED" in captured
        assert f"pending #{pending.id}, schedule #{schedule.id}" in captured

    def test_pending_wake_columns_migrate_idempotently_on_existing_db(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        legacy = sqlite3.connect(path)
        legacy.execute(
            """CREATE TABLE pending_schedule_wakes (
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   schedule_id INTEGER NOT NULL,
                   agent_name TEXT NOT NULL,
                   schedule_name TEXT NOT NULL DEFAULT '',
                   prompt TEXT NOT NULL DEFAULT '',
                   fired_at REAL NOT NULL,
                   created_at REAL NOT NULL,
                   UNIQUE(schedule_id, fired_at)
               )"""
        )
        legacy.commit()
        legacy.close()

        first = AgentRegistry(db_path=path)
        first.close()
        reopened = AgentRegistry(db_path=path)
        try:
            columns = [
                row[1]
                for row in reopened._db.execute(
                    "PRAGMA table_info(pending_schedule_wakes)"
                ).fetchall()
            ]
            assert columns.count("attempts") == 1
            assert columns.count("parked_at") == 1
            assert columns.count("accepted_at") == 1
            assert columns.count("failed_at") == 1
            assert columns.count("last_error") == 1
        finally:
            reopened.close()
            os.unlink(path)

    def test_cascade_delete(self, registry):
        registry.register("oleg")
        registry.add_schedule("oleg", "0 8 * * *", name="morning")
        registry.add_schedule("oleg", "0 21 * * *", name="evening")

        registry.delete("oleg")
        # Schedules should be cascade-deleted
        assert registry.get_schedules("oleg") == []

    def test_concurrent_add_same_name_allows_only_one(self, registry):
        """Same-process threads serialize the guard and insert."""
        registry.register("oleg")
        worker_count = 12
        start = threading.Barrier(worker_count)

        def create_schedule(index):
            start.wait(timeout=5)
            try:
                schedule = registry.add_schedule(
                    "oleg",
                    f"{index} 8 * * *",
                    name="morning",
                )
            except ScheduleNameConflictError:
                return "conflict", None
            return "created", schedule.id

        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            results = list(executor.map(create_schedule, range(worker_count)))

        assert [status for status, _ in results].count("created") == 1
        assert [status for status, _ in results].count("conflict") == worker_count - 1
        enabled = registry.get_schedules("oleg")
        assert len(enabled) == 1
        assert enabled[0].id == next(
            row_id for status, row_id in results if status == "created"
        )

    def test_concurrent_rename_to_same_free_name_allows_only_one(self, registry):
        """Same-process threads serialize each guarded enabled-row rename."""
        registry.register("oleg")
        worker_count = 12
        schedules = [
            registry.add_schedule("oleg", f"{index} 8 * * *", name=f"slot-{index}")
            for index in range(worker_count)
        ]
        start = threading.Barrier(worker_count)

        def rename_schedule(schedule):
            start.wait(timeout=5)
            try:
                updated = registry.update_schedule(schedule.id, name="target")
            except ScheduleNameConflictError:
                return "conflict"
            assert updated is not None
            return "renamed"

        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            results = list(executor.map(rename_schedule, schedules))

        assert results.count("renamed") == 1
        assert results.count("conflict") == worker_count - 1
        enabled = registry.get_schedules("oleg")
        assert len(enabled) == worker_count
        assert [schedule.name for schedule in enabled].count("target") == 1

    def test_concurrent_reenable_same_name_allows_only_one(self, registry):
        """Same-process threads serialize each guarded re-enable."""
        registry.register("oleg")
        worker_count = 12
        # Create 12 schedules with distinct names, disable them, then rename all
        # to "morning" while disabled (rename check skips disabled rows).
        schedules = [
            registry.add_schedule("oleg", f"{index} 8 * * *", name=f"slot-{index}")
            for index in range(worker_count)
        ]
        for schedule in schedules:
            registry.toggle_schedule(schedule.id, False)
        for schedule in schedules:
            registry.update_schedule(schedule.id, name="morning")

        start = threading.Barrier(worker_count)

        def enable_schedule(schedule):
            start.wait(timeout=5)
            try:
                enabled = registry.toggle_schedule(schedule.id, True)
            except ScheduleNameConflictError:
                return "conflict"
            assert enabled is True
            return "enabled"

        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            results = list(executor.map(enable_schedule, schedules))

        assert results.count("enabled") == 1
        assert results.count("conflict") == worker_count - 1
        enabled = registry.get_schedules("oleg")
        assert len(enabled) == 1
        assert enabled[0].name == "morning"


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
    @pytest.mark.parametrize(
        "raw_value", ["garbage", "0", "-1", "-inf", "inf", "nan"]
    )
    def test_invalid_receipt_extension_env_uses_finite_default(
        self, registry, monkeypatch, capsys, raw_value
    ):
        monkeypatch.setenv(
            "PINKY_SCHEDULE_RECEIPT_EXTENSION_MAX_AGE_SEC", raw_value
        )

        scheduler = AgentScheduler(registry)

        assert scheduler._receipt_extension_max_age_sec == 3_600.0
        error_log = capsys.readouterr().err
        assert "invalid receipt-extension ceiling" in error_log
        assert repr(raw_value) in error_log
        assert "using 3600s" in error_log

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
    async def test_start_warns_once_for_each_large_enabled_prompt(
        self, registry, capsys, monkeypatch
    ):
        registry.register("oleg")
        registry.add_schedule(
            "oleg", "0 8 * * *", name="large", prompt="x" * 8_001
        )
        disabled = registry.add_schedule(
            "oleg", "0 9 * * *", name="disabled", prompt="x" * 20_000
        )
        registry.toggle_schedule(disabled.id, False)
        scheduler = AgentScheduler(registry)

        async def no_loop():
            return None

        monkeypatch.setattr(scheduler, "_loop", no_loop)
        await scheduler.start()
        await asyncio.sleep(0)
        await scheduler.stop()

        err = capsys.readouterr().err
        warnings = [line for line in err.splitlines() if "large enabled" in line]
        assert len(warnings) == 1
        assert "name='large'" in warnings[0]
        assert "prompt_chars=8001" in warnings[0]
        assert "disabled" not in warnings[0]
        assert "x" * 100 not in warnings[0]  # prompt content is never logged

    def test_large_prompt_warning_repeats_only_after_periodic_window(
        self, registry, capsys
    ):
        registry.register("oleg")
        registry.add_schedule(
            "oleg", "0 8 * * *", name="large", prompt="x" * 8_001
        )
        scheduler = AgentScheduler(registry)

        scheduler._warn_oversized_schedule_prompts(100.0, force=True)
        capsys.readouterr()
        scheduler._warn_oversized_schedule_prompts(
            100.0 + _SCHEDULE_PROMPT_WARN_INTERVAL_SEC - 0.001
        )
        assert "large enabled" not in capsys.readouterr().err

        scheduler._warn_oversized_schedule_prompts(
            100.0 + _SCHEDULE_PROMPT_WARN_INTERVAL_SEC
        )
        assert "large enabled" in capsys.readouterr().err

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

    @pytest.mark.asyncio
    async def test_same_tick_prompts_wait_for_each_delivery_confirmation(
        self, registry
    ):
        """A busy session must not swallow the tail of a same-tick cohort."""
        registry.register("oleg")
        registry.add_schedule(
            "oleg", "* * * * *", name="first", prompt="first prompt"
        )
        registry.add_schedule(
            "oleg", "* * * * *", name="second", prompt="second prompt"
        )

        busy = False
        delivered: list[str] = []
        events: list[str] = []

        class Activity:
            def log(self, agent_name, event_type, summary):
                del agent_name, summary
                events.append(event_type)

        async def wake_cb(agent_name, session_id, prompt):
            nonlocal busy
            del agent_name, session_id
            receipt = asyncio.get_running_loop().create_future()
            if busy:
                receipt.set_result(False)
                return receipt

            busy = True

            def confirm_delivery():
                nonlocal busy
                delivered.append(prompt)
                busy = False
                receipt.set_result(True)

            asyncio.get_running_loop().call_soon(confirm_delivery)
            return receipt

        scheduler = AgentScheduler(
            registry, wake_callback=wake_cb, activity=Activity()
        )
        await scheduler._check_schedules(time.time())
        await asyncio.sleep(0.05)

        assert delivered == ["first prompt", "second prompt"]
        schedules = registry.get_schedules("oleg")
        assert all(schedule.last_run > 0 for schedule in schedules)
        assert all(schedule.last_delivered > 0 for schedule in schedules)
        assert events == [
            "schedule_fired",
            "schedule_fired",
            "schedule_delivered",
            "schedule_delivered",
        ]

    @pytest.mark.asyncio
    async def test_primary_confirm_log_includes_fire_identity(
        self, registry, capsys
    ):
        registry.register("oleg")
        schedule = registry.add_schedule(
            "oleg", "* * * * *", name="forensic", prompt="run once"
        )
        fired_at = 1_800_000_000.0
        assert registry.update_schedule_last_run(
            schedule.id,
            fired_at,
            expected_last_run=schedule.last_run,
        ) is True
        schedule.last_run = fired_at

        async def confirmed(agent_name, session_id, prompt):
            del agent_name, session_id, prompt
            return True

        scheduler = AgentScheduler(registry, wake_callback=confirmed)
        await scheduler._deliver_schedule(schedule)

        assert (
            f"scheduler: delivery confirmed for schedule 'forensic' "
            f"(#{schedule.id}) for agent 'oleg' (fired_at={fired_at})"
        ) in capsys.readouterr().err

    @pytest.mark.asyncio
    async def test_lost_last_run_claim_race_skips_fire(
        self, registry, monkeypatch, capsys
    ):
        registry.register("oleg")
        schedule = registry.add_schedule(
            "oleg", "* * * * *", name="claimed", prompt="run once"
        )
        wake_calls: list[str] = []
        events: list[str] = []
        real_claim = registry.claim_schedule_fire
        competing_claim_made = False

        def lose_to_competing_claim(
            schedule_id,
            *,
            timestamp,
            expected_last_run,
            agent_name,
            schedule_name,
            prompt,
        ):
            nonlocal competing_claim_made
            if not competing_claim_made:
                competing_claim_made = True
                claimed, _row = real_claim(
                    schedule_id,
                    timestamp=timestamp,
                    expected_last_run=expected_last_run,
                    agent_name=agent_name,
                    schedule_name=schedule_name,
                    prompt=prompt,
                )
                assert claimed is True
            return real_claim(
                schedule_id,
                timestamp=timestamp,
                expected_last_run=expected_last_run,
                agent_name=agent_name,
                schedule_name=schedule_name,
                prompt=prompt,
            )

        class Activity:
            def log(self, agent_name, event_type, summary):
                del agent_name, summary
                events.append(event_type)

        async def wake_cb(agent_name, session_id, prompt):
            del agent_name, session_id
            wake_calls.append(prompt)
            return True

        monkeypatch.setattr(
            registry,
            "claim_schedule_fire",
            lose_to_competing_claim,
        )
        scheduler = AgentScheduler(
            registry, wake_callback=wake_cb, activity=Activity()
        )
        fired_at = 1_800_000_000.0

        await scheduler._check_schedules(fired_at)

        stored = registry.get_schedules("oleg")[0]
        assert competing_claim_made is True
        assert stored.last_run == fired_at
        assert stored.last_delivered == 0.0
        assert wake_calls == []
        assert events == []
        assert scheduler._schedule_delivery_tasks == set()
        error_log = capsys.readouterr().err
        assert (
            f"scheduler: lost last_run claim race for "
            f"#{schedule.id} — skipping fire"
        ) in error_log
        assert "scheduler: firing schedule" not in error_log

    @pytest.mark.asyncio
    async def test_unconfirmed_delivery_stays_fired_but_undelivered(
        self, registry, capsys
    ):
        registry.register("oleg")
        registry.add_schedule(
            "oleg", "* * * * *", name="unconfirmed", prompt="prompt"
        )
        events: list[str] = []

        class Activity:
            def log(self, agent_name, event_type, summary):
                del agent_name, summary
                events.append(event_type)

        async def wake_cb(agent_name, session_id, prompt):
            del agent_name, session_id, prompt
            return asyncio.get_running_loop().create_future()

        scheduler = AgentScheduler(
            registry,
            wake_callback=wake_cb,
            activity=Activity(),
            schedule_delivery_timeout=0.01,
        )
        fired_at = time.time()
        await scheduler._check_schedules(fired_at)
        await asyncio.sleep(0.05)

        stored = registry.get_schedules("oleg")[0]
        assert stored.last_run == pytest.approx(fired_at)
        assert stored.last_delivered == 0.0
        pending = registry.list_pending_schedule_wakes("oleg")
        assert [(wake.schedule_id, wake.prompt) for wake in pending] == [
            (stored.id, "prompt")
        ]
        assert events == ["schedule_fired", "schedule_undelivered"]
        assert "FIRED BUT UNDELIVERED" in capsys.readouterr().err

    @pytest.mark.asyncio
    async def test_late_primary_confirm_retires_persisted_wake(
        self, registry, capsys
    ):
        registry.register("oleg")
        schedule = registry.add_schedule(
            "oleg", "* * * * *", name="late", prompt="run once"
        )
        fired_at = time.time()
        registry.update_schedule_last_run(schedule.id, fired_at)
        schedule.last_run = fired_at

        async def no_receipt(agent_name, session_id, prompt):
            del agent_name, session_id, prompt
            return asyncio.get_running_loop().create_future()

        scheduler = AgentScheduler(
            registry,
            wake_callback=no_receipt,
            schedule_delivery_timeout=0.01,
        )
        await scheduler._deliver_schedule(schedule)
        pending = registry.list_pending_schedule_wakes("oleg")
        assert len(pending) == 1

        attempts: list[str] = []

        async def confirmed(agent_name, session_id, prompt):
            del agent_name, session_id
            attempts.append(prompt)
            return True

        scheduler._wake_callback = confirmed
        await scheduler._deliver_schedule(schedule)

        assert attempts == ["run once"]
        assert registry.list_pending_schedule_wakes(
            "oleg", include_parked=True
        ) == []
        assert registry.get_schedules("oleg")[0].last_delivered > 0
        captured = capsys.readouterr().err
        assert "SCHEDULE_WAKE_RECEIPTED" in captured
        assert f"pending #{pending[0].id}, schedule #{schedule.id}" in captured

        scheduler.replay_pending_for_agent("oleg")
        await scheduler._pending_replay_tasks["oleg"]
        assert attempts == ["run once"]

    @pytest.mark.asyncio
    async def test_replay_drops_row_older_than_its_fire_interval(
        self, registry, monkeypatch, capsys
    ):
        """A frozen prompt older than the schedule cadence never executes."""
        registry.register("oleg")
        schedule = registry.add_schedule(
            "oleg", "* * * * *", name="minutely", prompt="frozen"
        )
        now = 1_800_000_000.0
        stale, _ = registry.persist_schedule_wake(
            schedule.id,
            agent_name="oleg",
            schedule_name="minutely",
            prompt="stale frozen prompt",
            fired_at=now - 61.0,
        )
        boundary, _ = registry.persist_schedule_wake(
            schedule.id,
            agent_name="oleg",
            schedule_name="minutely",
            prompt="boundary frozen prompt",
            fired_at=now - 60.0,
        )
        fresh, _ = registry.persist_schedule_wake(
            schedule.id,
            agent_name="oleg",
            schedule_name="minutely",
            prompt="fresh frozen prompt",
            fired_at=now - 59.0,
        )
        attempts: list[str] = []

        async def confirmed(agent_name, session_id, prompt):
            del agent_name, session_id
            attempts.append(prompt)
            return True

        monkeypatch.setattr("pinky_daemon.scheduler.time.time", lambda: now)
        scheduler = AgentScheduler(registry, wake_callback=confirmed)
        await scheduler._replay_pending_locked("oleg")

        assert len(attempts) == 1
        assert "Note: 1 fire of recurring schedule 'minutely'" in attempts[0]
        assert "The work that fire would have done was NOT performed." in attempts[0]
        assert attempts[0].endswith("\n\nfresh frozen prompt")
        assert registry.list_recurring_schedule_stale_drops("oleg") == []
        assert registry.get_schedule_wake_by_fire(
            schedule.id, stale.fired_at
        ) is None
        collapsed = registry.get_schedule_wake_by_fire(
            schedule.id, boundary.fired_at
        )
        assert collapsed.parked_at == pytest.approx(now)
        assert collapsed.last_error.startswith("recurrence collapsed")
        assert registry.get_schedule_wake_by_fire(
            schedule.id, fresh.fired_at
        ).accepted_at == pytest.approx(now)
        logs = capsys.readouterr().err
        assert f"PERSISTED_WAKE_STALE_DROPPED pending #{stale.id}" in logs
        assert "RECURRENCE_COLLAPSED" in logs
        assert "oldest_age_s=61.0" in logs

    @pytest.mark.asyncio
    async def test_replay_ceiling_drops_sparse_schedule_before_next_fire(
        self, registry, monkeypatch
    ):
        registry.register("oleg")
        schedule = registry.add_schedule(
            "oleg", "0 8 * * *", name="daily", prompt="too late"
        )
        now = 1_800_000_000.0
        pending, _ = registry.persist_schedule_wake(
            schedule.id,
            agent_name="oleg",
            schedule_name="daily",
            prompt="too late",
            fired_at=now - 3_601.0,
        )
        attempts: list[str] = []

        async def confirmed(agent_name, session_id, prompt):
            attempts.append(prompt)
            return True

        monkeypatch.setattr("pinky_daemon.scheduler.time.time", lambda: now)
        scheduler = AgentScheduler(
            registry,
            wake_callback=confirmed,
            pending_wake_max_age_sec=3_600.0,
        )
        await scheduler._replay_pending_locked("oleg")

        assert attempts == []
        assert registry.get_schedule_wake_by_fire(
            schedule.id, pending.fired_at
        ) is None

    @pytest.mark.asyncio
    async def test_replay_skips_stale_healthy_parked_row_without_drop_log(
        self, registry, monkeypatch, capsys
    ):
        registry.register("oleg")
        schedule = registry.add_schedule(
            "oleg", "* * * * *", name="parked", prompt="held for review"
        )
        now = 1_800_000_000.0
        pending, _ = registry.persist_schedule_wake(
            schedule.id,
            agent_name="oleg",
            schedule_name="parked",
            prompt="held for review",
            fired_at=now - 61.0,
        )
        assert registry.park_pending_schedule_wake(pending.id)
        monkeypatch.setattr("pinky_daemon.scheduler.time.time", lambda: now)
        scheduler = AgentScheduler(registry)

        await scheduler._replay_pending_locked("oleg")

        retained = registry.get_schedule_wake_by_fire(
            schedule.id, pending.fired_at
        )
        assert retained is not None
        assert retained.parked_at > 0
        assert "PERSISTED_WAKE_STALE_DROPPED" not in capsys.readouterr().err

    @pytest.mark.asyncio
    async def test_replay_alerts_owner_when_stale_one_shot_is_dropped(
        self, registry, monkeypatch
    ):
        registry.register("oleg")
        schedule = registry.add_schedule(
            "oleg",
            "0 8 * * *",
            name="one-time-enumeration",
            prompt="enumerate owed work",
            one_shot=True,
        )
        now = 1_800_000_000.0
        pending, _ = registry.persist_schedule_wake(
            schedule.id,
            agent_name="oleg",
            schedule_name=schedule.name,
            prompt=schedule.prompt,
            fired_at=now - 3_601.0,
        )
        alerts: list[tuple[str, str]] = []

        async def owner_notify(agent_name, message):
            alerts.append((agent_name, message))
            return True

        monkeypatch.setattr("pinky_daemon.scheduler.time.time", lambda: now)
        scheduler = AgentScheduler(
            registry,
            owner_notify_callback=owner_notify,
            pending_wake_max_age_sec=3_600.0,
        )

        await scheduler._replay_pending_locked("oleg")
        await asyncio.gather(*list(scheduler._owner_alert_tasks))

        assert registry.get_schedule_wake_by_fire(
            schedule.id, pending.fired_at
        ) is None
        assert len(alerts) == 1
        assert alerts[0][0] == "oleg"
        assert "STALE ONE-SHOT WAKE DROPPED" in alerts[0][1]
        assert f"outbox row #{pending.id}" in alerts[0][1]
        assert "has no next occurrence" in alerts[0][1]
        assert registry.list_recurring_schedule_stale_drops("oleg") == []

    @pytest.mark.asyncio
    async def test_kill_after_durable_accept_before_future_resolve_never_replays(
        self, capsys
    ):
        """Exercise the exact #991 crash seam, not a nearby timeout.

        The callback commits ``accepted_at`` and then deliberately returns an
        unresolved Future. Cancelling the delivery group models daemon death
        before AgentScheduler can observe/confirm that Future. A reopened
        scheduler must read the retained receipt and perform zero replay.
        """
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        first = AgentRegistry(db_path=path)
        first.register("oleg")
        schedule = first.add_schedule(
            "oleg", "* * * * *", name="crash-seam", prompt="run once"
        )
        fired_at = time.time()
        claimed, row = first.claim_schedule_fire(
            schedule.id,
            timestamp=fired_at,
            expected_last_run=schedule.last_run,
            agent_name="oleg",
            schedule_name=schedule.name,
            prompt=schedule.prompt,
        )
        assert claimed is True and row is not None
        schedule.last_run = fired_at
        accepted_before_future = asyncio.Event()
        process_local_receipt = asyncio.get_running_loop().create_future()

        async def accept_then_stall(
            agent_name,
            session_id,
            prompt,
            *,
            schedule_receipt,
        ):
            del agent_name, session_id, prompt
            assert not process_local_receipt.done()
            assert schedule_receipt.accept() is True
            committed = first.get_schedule_wake_by_fire(
                schedule.id, fired_at
            )
            assert committed is not None
            assert committed.ledger_state == "receipted-ran-once"
            assert not process_local_receipt.done()
            accepted_before_future.set()
            return process_local_receipt

        first_scheduler = AgentScheduler(
            first, wake_callback=accept_then_stall
        )
        delivery = asyncio.create_task(
            first_scheduler._deliver_schedule_group("oleg", [schedule])
        )
        await asyncio.wait_for(accepted_before_future.wait(), timeout=1)
        assert not delivery.done()

        delivery.cancel()
        await asyncio.gather(delivery, return_exceptions=True)
        assert first.list_pending_schedule_wakes("oleg") == []
        assert "FIRED BUT UNDELIVERED" not in capsys.readouterr().err

        uncertain = first.add_schedule(
            "oleg",
            "0 0 1 1 *",
            name="unconfirmed-at-kill",
            prompt="quarantine me",
        )
        uncertain_fire = fired_at - 1
        uncertain_claimed, _ = first.claim_schedule_fire(
            uncertain.id,
            timestamp=uncertain_fire,
            expected_last_run=uncertain.last_run,
            agent_name="oleg",
            schedule_name=uncertain.name,
            prompt=uncertain.prompt,
        )
        assert uncertain_claimed is True
        first.close()

        reopened = AgentRegistry(db_path=path)
        replay_attempts: list[str] = []

        async def replay_only_unconfirmed(agent_name, session_id, prompt):
            del agent_name, session_id
            replay_attempts.append(prompt)
            return False

        try:
            restarted = AgentScheduler(
                reopened,
                wake_callback=replay_only_unconfirmed,
                tick_interval=3600,
            )
            restarted.PERSISTED_WAKE_ATTEMPT_CAP = 1
            await restarted.start()
            await asyncio.wait_for(
                restarted._pending_replay_tasks["oleg"], timeout=1
            )
            await restarted.stop()

            assert replay_attempts == ["quarantine me"]
            ledger = reopened.list_schedule_wake_ledger(
                "oleg", state="receipted-ran-once"
            )
            assert len(ledger) == 1
            assert ledger[0].fired_at == fired_at
            quarantined = reopened.list_schedule_wake_ledger(
                "oleg", state="quarantined"
            )
            assert len(quarantined) == 1
            assert quarantined[0].fired_at == uncertain_fire
            assert reopened.list_pending_schedule_wakes("oleg") == []
        finally:
            reopened.close()
            os.unlink(path)

    @pytest.mark.asyncio
    async def test_overlapping_fires_keep_distinct_exact_outbox_rows(
        self, registry
    ):
        """A later tick must not replace an older cohort's fire identity."""
        registry.register("oleg")
        registry.add_schedule(
            "oleg", "* * * * *", name="overlap", prompt="same schedule"
        )
        first_started = asyncio.Event()
        attempts: list[str] = []

        async def no_receipt(agent_name, session_id, prompt):
            del agent_name, session_id
            attempts.append(prompt)
            first_started.set()
            return asyncio.get_running_loop().create_future()

        scheduler = AgentScheduler(
            registry,
            wake_callback=no_receipt,
            schedule_delivery_timeout=0.03,
        )
        first_fire = 1_800_000_000.0
        second_fire = first_fire + 60

        await scheduler._check_schedules(first_fire)
        await asyncio.wait_for(first_started.wait(), timeout=1)
        await scheduler._check_schedules(second_fire)
        delivery_tasks = list(scheduler._schedule_delivery_tasks)
        await asyncio.wait_for(
            asyncio.gather(*delivery_tasks, return_exceptions=True), timeout=1
        )

        assert attempts == ["same schedule"]
        assert [
            pending.fired_at
            for pending in registry.list_pending_schedule_wakes("oleg")
        ] == [first_fire, second_fire]

    @pytest.mark.asyncio
    async def test_busy_not_wedged_extends_receipt_timeout(self, registry, capsys):
        registry.register("oleg")
        schedule = registry.add_schedule(
            "oleg", "* * * * *", name="long-turn", prompt="after busy turn"
        )
        receipt = asyncio.get_running_loop().create_future()
        busy_checks: list[str] = []

        async def wake_cb(agent_name, session_id, prompt):
            del agent_name, session_id, prompt
            asyncio.get_running_loop().call_later(
                0.025, receipt.set_result, True
            )
            return receipt

        def busy_fn(agent_name):
            busy_checks.append(agent_name)
            return True

        scheduler = AgentScheduler(
            registry,
            wake_callback=wake_cb,
            delivery_busy_fn=busy_fn,
            schedule_delivery_timeout=0.01,
        )
        await scheduler._deliver_schedule(schedule)

        assert len(busy_checks) >= 2
        assert set(busy_checks) == {"oleg"}
        assert registry.get_schedules("oleg")[0].last_delivered > 0
        assert registry.list_pending_schedule_wakes("oleg") == []
        assert "busy-not-wedged; extending" in capsys.readouterr().err

    @pytest.mark.asyncio
    async def test_pasted_wake_extends_timeout_instead_of_cancelling(
        self, registry, capsys
    ):
        """A pasted-but-unaccepted wake must extend, never re-persist.

        Reproduces the 2026-08-01 duplicate-execution incident: the 600s
        receipt timeout hit while the prompt was already pasted to the pane
        (watchdog liveness blipped false between turns). Cancelling there
        cannot recall the paste — the REPL executes it anyway — so the
        re-persisted outbox row replays a wake that already ran. The
        transport execution-state probe must block the cancel.
        """
        registry.register("oleg")
        schedule = registry.add_schedule(
            "oleg", "* * * * *", name="sweep", prompt="run the sweep"
        )
        receipt = asyncio.get_running_loop().create_future()
        probes: list[tuple[str, str]] = []

        async def wake_cb(agent_name, session_id, prompt):
            del agent_name, session_id, prompt
            # Acceptance lands well after several timeout boundaries.
            asyncio.get_running_loop().call_later(
                0.05, receipt.set_result, True
            )
            return receipt

        def inflight_fn(agent_name, prompt):
            probes.append((agent_name, prompt))
            return True  # prompt is pasted, receipt open

        scheduler = AgentScheduler(
            registry,
            wake_callback=wake_cb,
            delivery_busy_fn=lambda agent_name: False,  # liveness blip
            delivery_inflight_fn=inflight_fn,
            schedule_delivery_timeout=0.01,
        )
        await scheduler._deliver_schedule(schedule)

        assert probes and set(probes) == {("oleg", "run the sweep")}
        assert registry.get_schedules("oleg")[0].last_delivered > 0
        assert registry.list_pending_schedule_wakes("oleg") == []
        assert "already pasted to the transport" in capsys.readouterr().err

    @pytest.mark.asyncio
    async def test_receipt_ceiling_uses_persisted_fired_at_across_restart(
        self, registry, monkeypatch, capsys
    ):
        """A restart must quarantine an old pasted row without resubmitting it.

        This catches the buggy replay path that called the wake callback for a
        prompt the transport had already pasted and could no longer recall.
        """
        registry.register("worker")
        schedule = registry.add_schedule(
            "worker", "0 * * * *", name="aged", prompt="already pasted"
        )
        fired_at = 1_800_000_000.0
        registry.persist_schedule_wake(
            schedule.id,
            agent_name="worker",
            schedule_name=schedule.name,
            prompt=schedule.prompt,
            fired_at=fired_at,
        )
        db_path = registry._db_path
        registry.close()
        restarted_registry = AgentRegistry(db_path=db_path)
        monkeypatch.setattr(
            "pinky_daemon.scheduler.time.time", lambda: fired_at + 91.0
        )
        monkeypatch.setattr(
            "pinky_daemon.scheduler._ABANDONED_RECEIPT_OBSERVER_INTERVAL_SEC",
            0.01,
        )
        submissions: list[str] = []
        pasted_inflight = True

        async def pasted(agent_name, session_id, prompt, **kwargs):
            del agent_name, session_id, kwargs
            submissions.append(prompt)
            return False

        def inflight(agent_name, prompt):
            del agent_name, prompt
            return pasted_inflight

        restarted = AgentScheduler(
            restarted_registry,
            wake_callback=pasted,
            delivery_inflight_fn=inflight,
            schedule_delivery_timeout=10.0,
            receipt_extension_max_age_sec=90.0,
        )
        restarted.replay_pending_for_agent("worker")
        await restarted._pending_replay_tasks["worker"]

        ledger = restarted_registry.get_schedule_wake_by_fire(
            schedule.id, fired_at
        )
        assert ledger.ledger_state == "quarantined"
        assert "RECEIPT_ABANDONED" in ledger.last_error
        assert submissions == []
        assert not restarted._schedule_delivery_locks["worker"].locked()
        assert "age_s=91.0 ceiling_s=90.0" in capsys.readouterr().err
        assert ScheduleWakeReceipt(
            restarted_registry, schedule.id, fired_at
        ).accept() is True
        pasted_inflight = False
        await asyncio.gather(*list(restarted._detached_receipt_tasks))
        assert restarted_registry.get_schedule_wake_by_fire(
            schedule.id, fired_at
        ).ledger_state == "receipted-ran-once"
        restarted_registry.close()

    @pytest.mark.asyncio
    async def test_production_ceiling_abandons_pasted_replay_before_stale_drop(
        self, registry, monkeypatch, capsys
    ):
        """An id-mismatched pasted row is abandoned without a duplicate paste.

        A prior receipted row forces ``pending.id != pending.schedule_id``.
        This catches both the wrong-key lookup that left the active row pending
        and the callback attempt that submitted the same physical prompt twice.
        """
        registry.register("worker")
        schedule = registry.add_schedule(
            "worker", "0 8 * * *", name="production-edge", prompt="pasted"
        )
        fired_at = 1_800_000_000.0
        prior, _ = registry.persist_schedule_wake(
            schedule.id,
            agent_name="worker",
            schedule_name=schedule.name,
            prompt=schedule.prompt,
            fired_at=fired_at - 1.0,
        )
        assert registry.confirm_pending_schedule_wake(
            prior.id, delivered_at=fired_at - 0.5
        )
        pending, _ = registry.persist_schedule_wake(
            schedule.id,
            agent_name="worker",
            schedule_name=schedule.name,
            prompt=schedule.prompt,
            fired_at=fired_at,
        )
        monkeypatch.setattr(
            "pinky_daemon.scheduler.time.time", lambda: fired_at + 3_601.0
        )
        monkeypatch.setattr(
            "pinky_daemon.scheduler._ABANDONED_RECEIPT_OBSERVER_INTERVAL_SEC",
            0.01,
        )
        submissions: list[str] = []
        probes: list[tuple[str, str]] = []
        pasted_inflight = True

        async def pasted(agent_name, session_id, prompt, **kwargs):
            del agent_name, session_id, kwargs
            submissions.append(prompt)
            return False

        def inflight(agent_name, prompt):
            probes.append((agent_name, prompt))
            return pasted_inflight

        scheduler = AgentScheduler(
            registry,
            wake_callback=pasted,
            delivery_inflight_fn=inflight,
            schedule_delivery_timeout=10.0,
        )

        await scheduler._replay_pending_locked("worker")

        ledger = registry.get_schedule_wake_by_fire(schedule.id, fired_at)
        assert ledger is not None
        assert ledger.id == pending.id
        assert ledger.id != ledger.schedule_id
        assert ledger.ledger_state == "quarantined"
        assert "RECEIPT_ABANDONED" in ledger.last_error
        assert submissions == []
        assert probes and set(probes) == {("worker", "pasted")}
        assert len(scheduler._detached_receipt_tasks) == 1
        logs = capsys.readouterr().err
        assert "receipt abandonment takes precedence over stale deletion" in logs
        assert "age_s=3601.0 ceiling_s=3600.0" in logs

        assert ScheduleWakeReceipt(
            registry, schedule.id, fired_at
        ).accept() is True
        pasted_inflight = False
        await asyncio.gather(*list(scheduler._detached_receipt_tasks))
        assert registry.get_schedule_wake_by_fire(
            schedule.id, fired_at
        ).ledger_state == "receipted-ran-once"

    @pytest.mark.asyncio
    async def test_replay_receipt_capability_uses_schedule_id_not_pending_id(
        self, registry
    ):
        """Durable acceptance must target the schedule when outbox IDs differ.

        This catches ``ScheduleWakeReceipt(..., pending.id, ...)``: the buggy
        capability cannot commit acceptance at the transport edge and leaves
        the exact replay row vulnerable across a process-local Future loss.
        """
        registry.register("worker")
        schedule = registry.add_schedule(
            "worker", "0 * * * *", name="identity", prompt="accept exactly"
        )
        fired_at = time.time()
        prior, _ = registry.persist_schedule_wake(
            schedule.id,
            agent_name="worker",
            schedule_name=schedule.name,
            prompt=schedule.prompt,
            fired_at=fired_at - 1.0,
        )
        assert registry.confirm_pending_schedule_wake(
            prior.id, delivered_at=fired_at - 0.5
        )
        pending, _ = registry.persist_schedule_wake(
            schedule.id,
            agent_name="worker",
            schedule_name=schedule.name,
            prompt=schedule.prompt,
            fired_at=fired_at,
        )
        assert pending.id != pending.schedule_id
        accepted_at_transport = asyncio.Event()
        process_local_receipt = asyncio.get_running_loop().create_future()

        async def accept_then_stall(
            agent_name,
            session_id,
            prompt,
            *,
            schedule_receipt,
        ):
            del agent_name, session_id, prompt
            assert schedule_receipt.schedule_id == schedule.id
            assert schedule_receipt.accept() is True
            accepted_at_transport.set()
            return process_local_receipt

        scheduler = AgentScheduler(registry, wake_callback=accept_then_stall)
        replay = asyncio.create_task(
            scheduler._replay_pending_locked("worker")
        )
        await asyncio.wait_for(accepted_at_transport.wait(), timeout=1)

        ledger = registry.get_schedule_wake_by_fire(schedule.id, fired_at)
        assert ledger is not None
        assert ledger.id == pending.id
        assert ledger.ledger_state == "receipted-ran-once"
        replay.cancel()
        await asyncio.gather(replay, return_exceptions=True)

    @pytest.mark.asyncio
    async def test_replay_abandonment_and_late_receipt_use_schedule_id(
        self, registry, monkeypatch, capsys
    ):
        """A replay crossing the ceiling mid-attempt keeps exact authority.

        The active outbox row is deliberately #2 for schedule #1. This catches
        both wrong-key quarantine and wrong-key late confirmation after a
        legitimate replay submission becomes pasted while its receipt waits.
        """
        registry.register("worker")
        schedule = registry.add_schedule(
            "worker", "0 * * * *", name="late-identity", prompt="wait exactly"
        )
        fired_at = 1_800_000_000.0
        prior, _ = registry.persist_schedule_wake(
            schedule.id,
            agent_name="worker",
            schedule_name=schedule.name,
            prompt=schedule.prompt,
            fired_at=fired_at - 1.0,
        )
        assert registry.confirm_pending_schedule_wake(
            prior.id, delivered_at=fired_at - 0.5
        )
        pending, _ = registry.persist_schedule_wake(
            schedule.id,
            agent_name="worker",
            schedule_name=schedule.name,
            prompt=schedule.prompt,
            fired_at=fired_at,
        )
        assert pending.id != pending.schedule_id
        now = [fired_at + 89.0]
        monkeypatch.setattr(
            "pinky_daemon.scheduler.time.time", lambda: now[0]
        )
        process_local_receipt = asyncio.get_running_loop().create_future()
        durable_receipt = None

        async def paste_then_cross_ceiling(
            agent_name,
            session_id,
            prompt,
            *,
            schedule_receipt,
        ):
            nonlocal durable_receipt
            del agent_name, session_id, prompt
            durable_receipt = schedule_receipt
            now[0] = fired_at + 91.0
            return process_local_receipt

        scheduler = AgentScheduler(
            registry,
            wake_callback=paste_then_cross_ceiling,
            delivery_inflight_fn=lambda agent_name, prompt: True,
            schedule_delivery_timeout=0.01,
            receipt_extension_max_age_sec=90.0,
        )
        await scheduler._replay_pending_locked("worker")

        ledger = registry.get_schedule_wake_by_fire(schedule.id, fired_at)
        assert ledger is not None
        assert ledger.id == pending.id
        assert ledger.ledger_state == "quarantined"
        assert "RECEIPT_ABANDONED" in ledger.last_error
        assert durable_receipt is not None
        assert durable_receipt.schedule_id == schedule.id
        logs = capsys.readouterr().err
        assert "schedule 'late-identity' (#1)" in logs
        assert "quarantined=True" in logs

        assert durable_receipt.accept() is True
        process_local_receipt.set_result(True)
        await asyncio.gather(*list(scheduler._detached_receipt_tasks))
        assert registry.get_schedule_wake_by_fire(
            schedule.id, fired_at
        ).ledger_state == "receipted-ran-once"

    @pytest.mark.asyncio
    async def test_abandonment_blocks_newer_recurrence_until_late_receipt(
        self, registry, monkeypatch, capsys
    ):
        """An old pasted fire retains authority ahead of recurrence collapse.

        The two-row fixture catches the buggy newest-fire collapse that marked
        the unrecallable old row RECURRENCE_COLLAPSED and submitted the newer
        equal prompt, allowing both physical turns to execute.
        """
        registry.register("worker")
        schedule = registry.add_schedule(
            "worker", "0 8 * * *", name="daily", prompt="same prompt"
        )
        older_fired_at = 1_800_000_000.0
        newer_fired_at = older_fired_at + 100.0
        older, _ = registry.persist_schedule_wake(
            schedule.id,
            agent_name="worker",
            schedule_name=schedule.name,
            prompt=schedule.prompt,
            fired_at=older_fired_at,
        )
        newer, _ = registry.persist_schedule_wake(
            schedule.id,
            agent_name="worker",
            schedule_name=schedule.name,
            prompt=schedule.prompt,
            fired_at=newer_fired_at,
        )
        monkeypatch.setattr(
            "pinky_daemon.scheduler.time.time",
            lambda: older_fired_at + 3_601.0,
        )
        monkeypatch.setattr(
            "pinky_daemon.scheduler._ABANDONED_RECEIPT_OBSERVER_INTERVAL_SEC",
            0.01,
        )
        pasted_inflight = True
        submissions: list[str] = []

        async def confirmed(agent_name, session_id, prompt):
            del agent_name, session_id
            submissions.append(prompt)
            return True

        def inflight(agent_name, prompt):
            del agent_name, prompt
            return pasted_inflight

        scheduler = AgentScheduler(
            registry,
            wake_callback=confirmed,
            delivery_inflight_fn=inflight,
        )
        await scheduler._replay_pending_locked("worker")

        by_id = {
            row.id: row for row in registry.list_schedule_wake_ledger("worker")
        }
        assert submissions == []
        assert by_id[older.id].ledger_state == "quarantined"
        assert "RECEIPT_ABANDONED" in by_id[older.id].last_error
        assert by_id[newer.id].ledger_state == "pending"
        assert by_id[newer.id].attempts == 0
        logs = capsys.readouterr().err
        assert "recurrence collapse" in logs
        assert "RECURRENCE_COLLAPSED" not in logs

        assert ScheduleWakeReceipt(
            registry, schedule.id, older_fired_at
        ).accept() is True
        pasted_inflight = False
        await asyncio.gather(*list(scheduler._detached_receipt_tasks))
        await scheduler._replay_pending_locked("worker")

        assert submissions == ["same prompt"]
        assert registry.get_schedule_wake_by_fire(
            schedule.id, newer_fired_at
        ).ledger_state == "receipted-ran-once"

    @pytest.mark.asyncio
    async def test_pending_recurrences_collapse_to_newest_with_trace(
        self, registry, capsys
    ):
        registry.register("worker")
        schedule = registry.add_schedule(
            "worker", "0 * * * *", name="hourly", prompt="run once"
        )
        now = time.time()
        rows = [
            registry.persist_schedule_wake(
                schedule.id,
                agent_name="worker",
                schedule_name=schedule.name,
                prompt=schedule.prompt,
                fired_at=now + offset,
            )[0]
            for offset in (1.0, 2.0, 3.0)
        ]
        attempts: list[str] = []

        async def confirmed(agent_name, session_id, prompt):
            del agent_name, session_id
            attempts.append(prompt)
            return True

        scheduler = AgentScheduler(registry, wake_callback=confirmed)
        await scheduler._replay_pending_locked("worker")

        assert attempts == ["run once"]
        ledger = registry.list_schedule_wake_ledger("worker")
        by_id = {row.id: row for row in ledger}
        assert by_id[rows[2].id].ledger_state == "receipted-ran-once"
        assert all(
            by_id[row.id].ledger_state == "quarantined"
            and by_id[row.id].last_error.startswith("recurrence collapsed")
            for row in rows[:2]
        )
        assert capsys.readouterr().err.count("RECURRENCE_COLLAPSED") == 2

    @pytest.mark.asyncio
    async def test_busy_fire_waits_for_idle_trigger_then_delivers(
        self, registry
    ):
        registry.register("worker")
        registry.add_schedule(
            "worker", "* * * * *", name="idle-driven", prompt="deliver on idle"
        )
        attempts: list[str] = []
        busy = True

        async def confirmed(agent_name, session_id, prompt):
            del agent_name, session_id
            attempts.append(prompt)
            return True

        scheduler = AgentScheduler(
            registry,
            wake_callback=confirmed,
            delivery_busy_fn=lambda agent_name: busy,
        )
        await scheduler._check_schedules(time.time())
        await asyncio.gather(
            *list(scheduler._schedule_delivery_tasks),
            return_exceptions=True,
        )
        assert attempts == []
        assert len(registry.list_pending_schedule_wakes("worker")) == 1

        busy = False
        scheduler.notify_agent_idle("worker")
        await scheduler._pending_replay_tasks["worker"]

        assert attempts == ["deliver on idle"]
        assert registry.list_pending_schedule_wakes("worker") == []

    @pytest.mark.asyncio
    async def test_periodic_drain_recovers_busy_fire_when_idle_edge_is_lost(
        self, registry, monkeypatch
    ):
        """Heartbeat=0 agents still drain a lost-idle deferred fire live."""
        registry.register("worker")
        registry.add_schedule(
            "worker", "* * * * *", name="recurring", prompt="run work"
        )
        base = 1_800_000_000.0
        clock = [base]
        monkeypatch.setattr(
            "pinky_daemon.scheduler.time.time", lambda: clock[0]
        )
        busy = True
        attempts: list[float] = []

        async def confirmed(agent_name, session_id, prompt):
            del agent_name, session_id, prompt
            attempts.append(clock[0])
            return True

        scheduler = AgentScheduler(
            registry,
            wake_callback=confirmed,
            delivery_busy_fn=lambda agent_name: busy,
        )

        await scheduler._check_schedules(base)
        await asyncio.gather(*list(scheduler._schedule_delivery_tasks))
        assert attempts == []
        assert len(registry.list_pending_schedule_wakes("worker")) == 1

        # Model the lost on_turn_idle callback: busy clears, but no explicit
        # notify_agent_idle call occurs.  Establish the periodic cadence.
        busy = False
        scheduler._check_pending_wake_liveness(base)

        # A later recurrence delivers normally; the periodic fallback then
        # drains the original exact fire at the replay-window boundary.
        clock[0] = base + 60.0
        await scheduler._check_schedules(clock[0])
        await asyncio.gather(*list(scheduler._schedule_delivery_tasks))
        scheduler._check_pending_wake_liveness(clock[0])
        await scheduler._pending_replay_tasks["worker"]

        clock[0] = base + 120.0
        await scheduler._check_schedules(clock[0])
        await asyncio.gather(*list(scheduler._schedule_delivery_tasks))
        scheduler._check_pending_wake_liveness(clock[0])

        assert attempts == [base + 60.0, base + 60.0, base + 120.0]
        assert registry.list_pending_schedule_wakes("worker") == []
        ledger = registry.list_schedule_wake_ledger("worker")
        assert len(ledger) == 3
        assert all(row.ledger_state == "receipted-ran-once" for row in ledger)
        assert registry.get("worker").heartbeat_interval == 0

    @pytest.mark.asyncio
    async def test_idle_trigger_during_replay_guarantees_follow_up_pass(
        self, registry
    ):
        registry.register("worker")
        schedule = registry.add_schedule(
            "worker", "0 * * * *", name="follow-up", prompt="deliver later"
        )
        registry.persist_schedule_wake(
            schedule.id,
            agent_name="worker",
            schedule_name=schedule.name,
            prompt=schedule.prompt,
            fired_at=time.time(),
        )
        attempts: list[str] = []

        async def confirmed(agent_name, session_id, prompt):
            del agent_name, session_id
            attempts.append(prompt)
            return True

        scheduler = AgentScheduler(registry, wake_callback=confirmed)
        first_started = asyncio.Event()
        release_first = asyncio.Event()
        original_replay = scheduler._replay_pending_locked
        passes = 0

        async def controlled_replay(agent_name):
            nonlocal passes
            passes += 1
            if passes == 1:
                first_started.set()
                await release_first.wait()
                return
            await original_replay(agent_name)

        scheduler._replay_pending_locked = controlled_replay
        scheduler.replay_pending_for_agent("worker")
        await first_started.wait()
        scheduler.notify_agent_idle("worker")
        release_first.set()

        for _ in range(100):
            if not registry.list_pending_schedule_wakes("worker"):
                break
            await asyncio.sleep(0)

        assert passes == 2
        assert attempts == ["deliver later"]
        assert registry.list_pending_schedule_wakes("worker") == []

    @pytest.mark.asyncio
    async def test_unpasted_wake_still_persists_on_timeout(self, registry):
        """Probe False (never pasted) keeps the durable-persist behavior."""
        registry.register("oleg")
        schedule = registry.add_schedule(
            "oleg", "* * * * *", name="queued-only", prompt="never pasted"
        )
        fired_at = time.time()
        registry.update_schedule_last_run(schedule.id, fired_at)
        schedule.last_run = fired_at

        async def no_receipt(agent_name, session_id, prompt):
            del agent_name, session_id, prompt
            return asyncio.get_running_loop().create_future()

        scheduler = AgentScheduler(
            registry,
            wake_callback=no_receipt,
            delivery_inflight_fn=lambda agent_name, prompt: False,
            schedule_delivery_timeout=0.01,
        )
        await scheduler._deliver_schedule(schedule)

        pending = registry.list_pending_schedule_wakes("oleg")
        assert [wake.prompt for wake in pending] == ["never pasted"]

    @pytest.mark.asyncio
    async def test_inflight_probe_failure_fails_closed(self, registry):
        """A broken probe degrades to the pre-probe cancel path, loudly."""
        registry.register("oleg")
        schedule = registry.add_schedule(
            "oleg", "* * * * *", name="broken-probe", prompt="probe breaks"
        )
        fired_at = time.time()
        registry.update_schedule_last_run(schedule.id, fired_at)
        schedule.last_run = fired_at

        async def no_receipt(agent_name, session_id, prompt):
            del agent_name, session_id, prompt
            return asyncio.get_running_loop().create_future()

        def broken(agent_name, prompt):
            raise RuntimeError("probe transport gone")

        scheduler = AgentScheduler(
            registry,
            wake_callback=no_receipt,
            delivery_inflight_fn=broken,
            schedule_delivery_timeout=0.01,
        )
        await scheduler._deliver_schedule(schedule)

        pending = registry.list_pending_schedule_wakes("oleg")
        assert [wake.prompt for wake in pending] == ["probe breaks"]

    @pytest.mark.asyncio
    async def test_undelivered_does_not_alert_owner_and_replays_on_next_boot(
        self, registry
    ):
        registry.register("oleg")
        schedule = registry.add_schedule(
            "oleg", "* * * * *", name="durable", prompt="durable prompt"
        )
        fired_at = time.time()
        registry.update_schedule_last_run(schedule.id, fired_at)
        schedule.last_run = fired_at
        alerts: list[tuple[str, str]] = []

        async def no_receipt(agent_name, session_id, prompt):
            del agent_name, session_id, prompt
            return asyncio.get_running_loop().create_future()

        async def owner_notify(agent_name, message):
            alerts.append((agent_name, message))
            return True

        first_session = AgentScheduler(
            registry,
            wake_callback=no_receipt,
            owner_notify_callback=owner_notify,
            schedule_delivery_timeout=0.01,
        )
        await first_session._deliver_schedule(schedule)
        await asyncio.sleep(0)

        pending = registry.list_pending_schedule_wakes("oleg")
        assert len(pending) == 1
        # Delivery-receipt failures are NOT owner-notified: they are frequently
        # false positives (the wake persists + replays), so the owner sees
        # nothing — only the FIRED BUT UNDELIVERED operator log line.
        assert alerts == []

        attempts: list[str] = []

        async def confirmed(agent_name, session_id, prompt):
            del agent_name, session_id
            attempts.append(prompt)
            return True

        next_session = AgentScheduler(registry, wake_callback=confirmed)
        next_session.replay_pending_for_agent("oleg")
        replay_task = next_session._pending_replay_tasks["oleg"]
        await replay_task

        assert attempts == ["durable prompt"]
        assert registry.list_pending_schedule_wakes("oleg") == []
        assert registry.get_schedules("oleg")[0].last_delivered > 0

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", ["alive", "ok", "busy", "finishing"])
    async def test_fresh_heartbeat_drains_stranded_outbox(
        self, registry, status
    ):
        """A session revived by a heartbeat (no confirmed wake) still replays.

        Reproduces the 2026-08-01 live incident: a context_restart whose
        orientation wake never produced a receipt left the session alive but
        never fired on_wake_delivered, so the durable outbox — the only two
        triggers being daemon start() and on_wake_delivered — never drained.
        _check_heartbeats must treat a fresh heartbeat as the proof-of-life
        boundary and replay the backlog.
        """
        registry.register("oleg", heartbeat_interval=60)
        schedule = registry.add_schedule(
            "oleg", "0 8 * * *", name="inbox", prompt="morning inbox"
        )
        fired_at = time.time() - 300
        registry.update_schedule_last_run(schedule.id, fired_at)
        registry.persist_schedule_wake(
            schedule.id,
            agent_name="oleg",
            schedule_name="inbox",
            prompt="morning inbox",
            fired_at=fired_at,
        )
        # Session is provably alive again: a fresh heartbeat, exactly as
        # the live incident's reviving heartbeat proved — but NO confirmed wake landed.
        registry.record_heartbeat("oleg", session_id="oleg-main", status=status)

        attempts: list[str] = []

        async def confirmed(agent_name, session_id, prompt):
            del agent_name, session_id
            attempts.append(prompt)
            return True

        # streaming_sessions_fn returns {} so server-liveness does not short
        # -circuit — the fresh-heartbeat branch is what must drain the outbox.
        scheduler = AgentScheduler(
            registry,
            wake_callback=confirmed,
            streaming_sessions_fn=lambda: {},
        )
        await scheduler._check_heartbeats(time.time())
        replay_task = scheduler._pending_replay_tasks.get("oleg")
        assert replay_task is not None, "fresh heartbeat did not trigger replay"
        await replay_task

        assert attempts == ["morning inbox"]
        assert registry.list_pending_schedule_wakes("oleg") == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", ["stale", "dead"])
    async def test_fresh_but_non_alive_heartbeat_never_drains(
        self, registry, status
    ):
        """A temporally-fresh stale/dead heartbeat row is NOT proof-of-life.

        The scheduler itself writes "stale"/"dead" rows with current
        timestamps, so on the next tick those rows pass the age check while
        the session is provably NOT live. Draining there would cold-start a
        dead (or deliberately non-resurrectable) runtime through the wake
        callback, bypassing the proven-live-only policy (Murzik review,
        PR #981). The pending row must be retained untouched.
        """
        registry.register("oleg", heartbeat_interval=60)
        schedule = registry.add_schedule(
            "oleg", "0 8 * * *", name="inbox", prompt="morning inbox"
        )
        fired_at = time.time() - 300
        registry.update_schedule_last_run(schedule.id, fired_at)
        registry.persist_schedule_wake(
            schedule.id,
            agent_name="oleg",
            schedule_name="inbox",
            prompt="morning inbox",
            fired_at=fired_at,
        )
        # Temporally fresh, but the status says the session is not live —
        # exactly what the scheduler's own bookkeeping writes.
        heartbeat = registry.record_heartbeat(
            "oleg", session_id="oleg-main", status=status
        )

        attempts: list[str] = []

        async def confirmed(agent_name, session_id, prompt):
            del agent_name, session_id
            attempts.append(prompt)
            return True

        scheduler = AgentScheduler(
            registry,
            wake_callback=confirmed,
            streaming_sessions_fn=lambda: {},
        )
        await scheduler._check_heartbeats(heartbeat.timestamp + 1)
        replay_task = scheduler._pending_replay_tasks.get("oleg")
        if replay_task is not None:
            await replay_task

        assert attempts == []
        assert [
            wake.prompt for wake in registry.list_pending_schedule_wakes("oleg")
        ] == ["morning inbox"]

    @pytest.mark.asyncio
    async def test_aged_agent_heartbeat_never_drains(self, registry):
        registry.register("oleg", heartbeat_interval=60)
        schedule = registry.add_schedule(
            "oleg", "0 8 * * *", name="inbox", prompt="morning inbox"
        )
        fired_at = time.time() - 300
        registry.update_schedule_last_run(schedule.id, fired_at)
        registry.persist_schedule_wake(
            schedule.id,
            agent_name="oleg",
            schedule_name="inbox",
            prompt="morning inbox",
            fired_at=fired_at,
        )
        heartbeat = registry.record_heartbeat(
            "oleg", session_id="oleg-main", status="ok"
        )
        attempts: list[str] = []

        async def confirmed(agent_name, session_id, prompt):
            del agent_name, session_id
            attempts.append(prompt)
            return True

        scheduler = AgentScheduler(
            registry,
            wake_callback=confirmed,
            streaming_sessions_fn=lambda: {},
        )
        await scheduler._check_heartbeats(heartbeat.timestamp + 61)
        replay_task = scheduler._pending_replay_tasks.get("oleg")
        if replay_task is not None:
            await replay_task

        assert attempts == []
        assert [
            wake.prompt for wake in registry.list_pending_schedule_wakes("oleg")
        ] == ["morning inbox"]

    @pytest.mark.asyncio
    async def test_persisted_fifo_replays_before_new_schedule_cohort(
        self, registry
    ):
        registry.register("oleg")
        older = registry.add_schedule(
            "oleg", "0 8 * * *", name="older", prompt="older pending"
        )
        newer = registry.add_schedule(
            "oleg", "0 9 * * *", name="newer", prompt="new live fire"
        )
        older_fired_at = time.time() - 60
        registry.update_schedule_last_run(older.id, older_fired_at)
        registry.persist_schedule_wake(
            older.id,
            agent_name="oleg",
            schedule_name="older",
            prompt="older pending",
            fired_at=older_fired_at,
        )
        attempts: list[str] = []

        async def confirmed(agent_name, session_id, prompt):
            del agent_name, session_id
            attempts.append(prompt)
            return True

        scheduler = AgentScheduler(registry, wake_callback=confirmed)
        scheduler.replay_pending_for_agent("oleg")
        await scheduler._deliver_schedule_group("oleg", [newer])

        assert attempts == ["older pending", "new live fire"]
        assert registry.list_pending_schedule_wakes("oleg") == []

    @pytest.mark.asyncio
    async def test_transient_oldest_replay_failure_halts_fifo(self, registry):
        registry.register("oleg")
        schedule = registry.add_schedule(
            "oleg", "* * * * *", name="fifo", prompt="unused"
        )
        newer_schedule = registry.add_schedule(
            schedule.agent_name,
            "* * * * *",
            name="fifo-newer",
            prompt="unused",
        )
        now = time.time()
        registry.persist_schedule_wake(
            schedule.id,
            agent_name="oleg",
            schedule_name="fifo",
            prompt="oldest",
            fired_at=now - 2.0,
        )
        registry.persist_schedule_wake(
            newer_schedule.id,
            agent_name="oleg",
            schedule_name="fifo-newer",
            prompt="newer",
            fired_at=now - 1.0,
        )
        attempts: list[str] = []

        async def first_fails(agent_name, session_id, prompt):
            del agent_name, session_id
            attempts.append(prompt)
            return False

        scheduler = AgentScheduler(registry, wake_callback=first_fails)
        scheduler.replay_pending_for_agent("oleg")
        await scheduler._pending_replay_tasks["oleg"]

        assert attempts == ["oldest"]
        assert [
            pending.prompt
            for pending in registry.list_pending_schedule_wakes("oleg")
        ] == ["oldest", "newer"]

    @pytest.mark.asyncio
    async def test_replay_failure_cap_parks_alerts_once_and_stops_drain(
        self, registry, monkeypatch, capsys
    ):
        registry.register("oleg")
        schedule = registry.add_schedule(
            "oleg", "* * * * *", name="storm-head", prompt="retry me"
        )
        registry.persist_schedule_wake(
            schedule.id,
            agent_name="oleg",
            schedule_name="storm-head",
            prompt="retry me",
            fired_at=time.time(),
        )
        attempts: list[str] = []
        alerts: list[tuple[str, str]] = []

        async def unconfirmed(agent_name, session_id, prompt):
            del agent_name, session_id
            attempts.append(prompt)
            return False

        async def owner_notify(agent_name, message):
            alerts.append((agent_name, message))
            return True

        scheduler = AgentScheduler(
            registry,
            wake_callback=unconfirmed,
            owner_notify_callback=owner_notify,
        )
        for _ in range(scheduler.PERSISTED_WAKE_ATTEMPT_CAP):
            scheduler.replay_pending_for_agent("oleg")
            await scheduler._pending_replay_tasks["oleg"]
            await asyncio.sleep(0)
        await asyncio.gather(*list(scheduler._owner_alert_tasks))

        assert attempts == ["retry me"] * scheduler.PERSISTED_WAKE_ATTEMPT_CAP
        assert registry.list_pending_schedule_wakes("oleg") == []
        parked = registry.list_pending_schedule_wakes(
            "oleg", include_parked=True
        )
        assert len(parked) == 1
        assert parked[0].attempts == scheduler.PERSISTED_WAKE_ATTEMPT_CAP
        assert parked[0].parked_at > 0
        assert len(alerts) == 1
        assert alerts[0][0] == "oleg"
        assert "queryable quarantine" in alerts[0][1]
        assert capsys.readouterr().err.count("PERSISTED_WAKE_PARKED") == 1

        drain_triggers: list[str] = []
        monkeypatch.setattr(
            scheduler, "replay_pending_for_agent", drain_triggers.append
        )
        scheduler._drain_outbox_if_pending("oleg")
        assert drain_triggers == []

    @pytest.mark.asyncio
    async def test_confirmed_fifth_replay_retires_instead_of_parking(
        self, registry
    ):
        registry.register("oleg")
        schedule = registry.add_schedule(
            "oleg", "* * * * *", name="eventual", prompt="confirm me"
        )
        pending, _ = registry.persist_schedule_wake(
            schedule.id,
            agent_name="oleg",
            schedule_name="eventual",
            prompt="confirm me",
            fired_at=time.time(),
        )
        for expected in range(1, AgentScheduler.PERSISTED_WAKE_ATTEMPT_CAP):
            assert (
                registry.increment_pending_schedule_wake_attempts(pending.id)
                == expected
            )
        attempts: list[str] = []

        async def confirmed(agent_name, session_id, prompt):
            del agent_name, session_id
            attempts.append(prompt)
            return True

        scheduler = AgentScheduler(registry, wake_callback=confirmed)
        scheduler.replay_pending_for_agent("oleg")
        await scheduler._pending_replay_tasks["oleg"]

        assert attempts == ["confirm me"]
        assert registry.list_pending_schedule_wakes(
            "oleg", include_parked=True
        ) == []

    @pytest.mark.asyncio
    async def test_zombie_behind_stuck_head_is_reaped_before_delivery(
        self, registry, capsys
    ):
        registry.register("oleg")
        stuck = registry.add_schedule(
            "oleg", "* * * * *", name="stuck", prompt="stuck head"
        )
        zombie = registry.add_schedule(
            "oleg", "* * * * *", name="zombie", prompt="never deliver"
        )
        now = time.time()
        registry.persist_schedule_wake(
            stuck.id,
            agent_name="oleg",
            schedule_name="stuck",
            prompt="stuck head",
            fired_at=now - 1.0,
        )
        registry.persist_schedule_wake(
            zombie.id,
            agent_name="oleg",
            schedule_name="zombie",
            prompt="never deliver",
            fired_at=200.0,
        )
        registry.remove_schedule(zombie.id)
        attempts: list[str] = []

        async def unconfirmed(agent_name, session_id, prompt):
            del agent_name, session_id
            attempts.append(prompt)
            return False

        scheduler = AgentScheduler(registry, wake_callback=unconfirmed)
        scheduler.replay_pending_for_agent("oleg")
        await scheduler._pending_replay_tasks["oleg"]

        assert attempts == ["stuck head"]
        assert [
            pending.prompt
            for pending in registry.list_pending_schedule_wakes("oleg")
        ] == ["stuck head"]
        quarantined = registry.list_schedule_wake_ledger(
            "oleg", state="quarantined"
        )
        assert [row.prompt for row in quarantined] == ["never deliver"]
        error_log = capsys.readouterr().err
        assert "PERSISTED_WAKE_ZOMBIE_QUARANTINED" in error_log
        assert f"schedule #{zombie.id}" in error_log

    @pytest.mark.asyncio
    async def test_replay_quarantines_live_zombie_once_then_skips_terminal_row(
        self, registry, monkeypatch, capsys
    ):
        registry.register("oleg")
        schedule = registry.add_schedule(
            "oleg", "* * * * *", name="zombie", prompt="never deliver"
        )
        pending, _ = registry.persist_schedule_wake(
            schedule.id,
            agent_name="oleg",
            schedule_name="zombie",
            prompt="never deliver",
            fired_at=100.0,
        )
        registry.remove_schedule(schedule.id)
        park_calls: list[int] = []
        original_park = registry.park_pending_schedule_wake

        def tracked_park(pending_id, **kwargs):
            park_calls.append(pending_id)
            return original_park(pending_id, **kwargs)

        monkeypatch.setattr(
            registry, "park_pending_schedule_wake", tracked_park
        )
        attempts: list[str] = []

        async def confirmed(agent_name, session_id, prompt):
            del agent_name, session_id
            attempts.append(prompt)
            return True

        first_boot = AgentScheduler(registry, wake_callback=confirmed)
        await first_boot._replay_pending_locked("oleg")
        first_terminal_state = registry.get_schedule_wake_by_fire(
            schedule.id, 100.0
        ).to_dict()

        for _ in range(5):
            later_boot = AgentScheduler(registry, wake_callback=confirmed)
            await later_boot._replay_pending_locked("oleg")

        assert attempts == []
        assert park_calls == [pending.id]
        assert registry.get_schedule_wake_by_fire(
            schedule.id, 100.0
        ).to_dict() == first_terminal_state
        assert first_terminal_state["state"] == "quarantined"
        assert first_terminal_state["last_error"].endswith(
            "schedule deleted"
        )
        assert (
            capsys.readouterr().err.count(
                "PERSISTED_WAKE_ZOMBIE_QUARANTINED"
            )
            == 1
        )

    @pytest.mark.asyncio
    async def test_replay_logs_live_zombie_park_noop(
        self, registry, monkeypatch, capsys
    ):
        registry.register("oleg")
        schedule = registry.add_schedule(
            "oleg", "* * * * *", name="zombie", prompt="never deliver"
        )
        pending, _ = registry.persist_schedule_wake(
            schedule.id,
            agent_name="oleg",
            schedule_name="zombie",
            prompt="never deliver",
            fired_at=100.0,
        )
        registry.remove_schedule(schedule.id)
        monkeypatch.setattr(
            registry,
            "park_pending_schedule_wake",
            lambda pending_id, **kwargs: False,
        )

        scheduler = AgentScheduler(registry)
        await scheduler._replay_pending_locked("oleg")

        stored = registry.get_schedule_wake_by_fire(schedule.id, 100.0)
        assert stored is not None
        assert stored.to_dict() == pending.to_dict()
        error_log = capsys.readouterr().err
        assert "PERSISTED_WAKE_ZOMBIE_PARK_NOOP" in error_log
        assert f"pending #{pending.id}" in error_log
        assert "PERSISTED_WAKE_ZOMBIE_QUARANTINED" not in error_log

    @pytest.mark.asyncio
    async def test_terminal_zombie_head_retires_and_fifo_advances(
        self, registry, capsys
    ):
        registry.register("oleg")
        zombie = registry.add_schedule(
            "oleg", "* * * * *", name="zombie", prompt="never deliver"
        )
        live = registry.add_schedule(
            "oleg", "* * * * *", name="live", prompt="unused"
        )
        live_two = registry.add_schedule(
            live.agent_name,
            "* * * * *",
            name="live-two",
            prompt="unused",
        )
        now = time.time()
        registry.persist_schedule_wake(
            zombie.id,
            agent_name="oleg",
            schedule_name="zombie",
            prompt="never deliver",
            fired_at=100.0,
        )
        registry.persist_schedule_wake(
            live.id,
            agent_name="oleg",
            schedule_name="live",
            prompt="live one",
            fired_at=now - 2.0,
        )
        registry.persist_schedule_wake(
            live_two.id,
            agent_name="oleg",
            schedule_name="live-two",
            prompt="live two",
            fired_at=now - 1.0,
        )
        registry.remove_schedule(zombie.id)
        attempts: list[str] = []

        async def confirmed(agent_name, session_id, prompt):
            del agent_name, session_id
            attempts.append(prompt)
            return True

        scheduler = AgentScheduler(registry, wake_callback=confirmed)
        scheduler.replay_pending_for_agent("oleg")
        await scheduler._pending_replay_tasks["oleg"]

        assert attempts == ["live one", "live two"]
        assert registry.list_pending_schedule_wakes("oleg") == []
        assert "PERSISTED_WAKE_ZOMBIE_QUARANTINED" in capsys.readouterr().err

    @pytest.mark.asyncio
    async def test_new_cron_fire_does_not_replay_backlog_into_old_session(
        self, registry
    ):
        registry.register("oleg")
        older = registry.add_schedule(
            "oleg", "0 8 * * *", name="older", prompt="next session only"
        )
        newer = registry.add_schedule(
            "oleg", "0 9 * * *", name="newer", prompt="new live fire"
        )
        older_fired_at = time.time() - 60
        registry.update_schedule_last_run(older.id, older_fired_at)
        registry.persist_schedule_wake(
            older.id,
            agent_name="oleg",
            schedule_name="older",
            prompt="next session only",
            fired_at=older_fired_at,
        )
        attempts: list[str] = []

        async def confirmed(agent_name, session_id, prompt):
            del agent_name, session_id
            attempts.append(prompt)
            return True

        scheduler = AgentScheduler(registry, wake_callback=confirmed)

        await scheduler._deliver_schedule_group("oleg", [newer])

        assert attempts == ["new live fire"]
        assert [
            pending.prompt
            for pending in registry.list_pending_schedule_wakes("oleg")
        ] == ["next session only"]

    @pytest.mark.asyncio
    async def test_canceled_cohort_waiting_for_boot_replay_is_persisted(
        self, registry
    ):
        registry.register("oleg")
        older = registry.add_schedule(
            "oleg", "0 8 * * *", name="older", prompt="older pending"
        )
        newer = registry.add_schedule(
            "oleg", "0 9 * * *", name="newer", prompt="new live fire"
        )
        older_fired_at = time.time() - 60
        registry.update_schedule_last_run(older.id, older_fired_at)
        registry.persist_schedule_wake(
            older.id,
            agent_name="oleg",
            schedule_name="older",
            prompt="older pending",
            fired_at=older_fired_at,
        )
        newer_fired_at = time.time()
        registry.update_schedule_last_run(newer.id, newer_fired_at)
        newer.last_run = newer_fired_at
        replay_started = asyncio.Event()

        async def blocked(agent_name, session_id, prompt):
            del agent_name, session_id, prompt
            replay_started.set()
            return asyncio.get_running_loop().create_future()

        scheduler = AgentScheduler(registry, wake_callback=blocked)
        scheduler.replay_pending_for_agent("oleg")
        await asyncio.wait_for(replay_started.wait(), timeout=1)
        cohort = asyncio.create_task(
            scheduler._deliver_schedule_group("oleg", [newer])
        )
        await asyncio.sleep(0)

        cohort.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cohort
        replay = scheduler._pending_replay_tasks["oleg"]
        replay.cancel()
        await asyncio.gather(replay, return_exceptions=True)

        assert [
            pending.prompt
            for pending in registry.list_pending_schedule_wakes("oleg")
        ] == ["older pending", "new live fire"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("mutation", "reason"),
        [("delete", "schedule deleted"), ("disable", "schedule disabled")],
    )
    async def test_replay_drops_deleted_or_disabled_zombie_wake(
        self, registry, capsys, mutation, reason
    ):
        registry.register("oleg")
        schedule = registry.add_schedule(
            "oleg", "0 8 * * *", name="obsolete", prompt="do not deliver"
        )
        fired_at = time.time()
        registry.update_schedule_last_run(schedule.id, fired_at)
        registry.persist_schedule_wake(
            schedule.id,
            agent_name="oleg",
            schedule_name="obsolete",
            prompt="do not deliver",
            fired_at=fired_at,
        )
        if mutation == "delete":
            registry.remove_schedule(schedule.id)
        else:
            registry.toggle_schedule(schedule.id, False)
        attempts: list[str] = []

        async def confirmed(agent_name, session_id, prompt):
            del agent_name, session_id
            attempts.append(prompt)
            return True

        scheduler = AgentScheduler(registry, wake_callback=confirmed)
        scheduler.replay_pending_for_agent("oleg")
        replay_task = scheduler._pending_replay_tasks["oleg"]
        await replay_task

        assert attempts == []
        assert registry.list_pending_schedule_wakes("oleg") == []
        error_log = capsys.readouterr().err
        assert "PERSISTED_WAKE_ZOMBIE_QUARANTINED" in error_log
        assert reason in error_log
        assert registry.list_schedule_wake_ledger(
            "oleg", state="quarantined"
        )[0].last_error.endswith(reason)

    @pytest.mark.asyncio
    async def test_disabled_one_shot_replays_once_across_scheduler_restart(
        self, registry
    ):
        registry.register("oleg")
        schedule = registry.add_schedule(
            "oleg",
            "0 8 * * *",
            name="one-shot",
            prompt="deliver exactly once",
            one_shot=True,
        )
        fired_at = time.time()
        registry.update_schedule_last_run(schedule.id, fired_at)
        registry.toggle_schedule(schedule.id, False)
        registry.persist_schedule_wake(
            schedule.id,
            agent_name="oleg",
            schedule_name="one-shot",
            prompt="deliver exactly once",
            fired_at=fired_at,
        )
        db_path = registry._db_path
        registry.close()
        reopened = AgentRegistry(db_path=db_path)
        attempts: list[str] = []

        async def confirmed(agent_name, session_id, prompt):
            del agent_name, session_id
            attempts.append(prompt)
            return True

        try:
            first_boot = AgentScheduler(
                reopened, wake_callback=confirmed, tick_interval=3600
            )
            await first_boot.start()
            first_replay = first_boot._pending_replay_tasks["oleg"]
            await asyncio.wait_for(first_replay, timeout=1)
            await first_boot.stop()

            second_boot = AgentScheduler(
                reopened, wake_callback=confirmed, tick_interval=3600
            )
            await second_boot.start()
            await asyncio.sleep(0.01)
            await second_boot.stop()

            assert attempts == ["deliver exactly once"]
            assert reopened.list_pending_schedule_wakes("oleg") == []
        finally:
            reopened.close()

    @pytest.mark.asyncio
    async def test_canceled_cohort_accounts_current_and_remaining_as_undelivered(
        self, registry, capsys
    ):
        registry.register("oleg")
        registry.add_schedule(
            "oleg", "* * * * *", name="first", prompt="first"
        )
        registry.add_schedule(
            "oleg", "* * * * *", name="second", prompt="second"
        )
        first_started = asyncio.Event()
        attempts: list[str] = []
        events: list[str] = []

        class Activity:
            def log(self, agent_name, event_type, summary):
                del agent_name, summary
                events.append(event_type)

        async def wake_cb(agent_name, session_id, prompt):
            del agent_name, session_id
            attempts.append(prompt)
            first_started.set()
            return asyncio.get_running_loop().create_future()

        scheduler = AgentScheduler(
            registry, wake_callback=wake_cb, activity=Activity()
        )
        await scheduler._check_schedules(time.time())
        await asyncio.wait_for(first_started.wait(), timeout=1)
        await asyncio.wait_for(scheduler.stop(), timeout=1)

        schedules = registry.get_schedules("oleg")
        assert attempts == ["first"]
        assert all(schedule.last_run > 0 for schedule in schedules)
        assert all(schedule.last_delivered == 0 for schedule in schedules)
        assert [
            pending.prompt
            for pending in registry.list_pending_schedule_wakes("oleg")
        ] == ["first", "second"]
        assert events == [
            "schedule_fired",
            "schedule_fired",
            "schedule_undelivered",
            "schedule_undelivered",
        ]
        assert capsys.readouterr().err.count("FIRED BUT UNDELIVERED") == 2

    @pytest.mark.asyncio
    async def test_misconfigured_direct_send_never_falls_back_to_agent_wake(
        self, registry, capsys
    ):
        registry.register("oleg")
        registry.add_schedule(
            "oleg",
            "* * * * *",
            name="direct",
            prompt="target prompt",
            target_channel="12345",
            direct_send=True,
        )
        wake_calls: list[str] = []

        async def wake_cb(agent_name, session_id, prompt):
            del agent_name, session_id
            wake_calls.append(prompt)
            return True

        scheduler = AgentScheduler(registry, wake_callback=wake_cb)
        schedule = registry.get_schedules("oleg")[0]
        await scheduler._deliver_schedule(schedule)

        stored = registry.get_schedules("oleg")[0]
        assert wake_calls == []
        assert stored.last_delivered == 0.0
        assert "FIRED BUT UNDELIVERED" in capsys.readouterr().err

    @pytest.mark.asyncio
    async def test_busy_agent_does_not_defer_independent_direct_send(
        self, registry
    ):
        registry.register("worker")
        schedule = registry.add_schedule(
            "worker",
            "* * * * *",
            name="direct",
            prompt="send now",
            target_channel="test-channel",
            direct_send=True,
        )
        sent: list[tuple[str, str, str, str]] = []

        async def direct_send(agent_name, platform, chat_id, message):
            sent.append((agent_name, platform, chat_id, message))

        scheduler = AgentScheduler(
            registry,
            direct_send_callback=direct_send,
            delivery_busy_fn=lambda agent_name: True,
        )
        await scheduler._deliver_schedule_group("worker", [schedule])

        assert sent == [
            ("worker", "telegram", "test-channel", "send now")
        ]


# ── Heartbeat Watchdog Resurrection (issue #338) ──────────────────────────


class TestHeartbeatResurrection:
    """The watchdog must invoke heartbeat_callback for dead agents,
    rate-limited to RESURRECTION_MAX_ATTEMPTS per RESURRECTION_WINDOW_SECONDS.
    """

    class _FakeStreamingSession:
        from pinky_daemon.transport_state import SessionState
        state = SessionState.CONNECTED
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


# ── Non-blocking idle/auto-sleep fires (#702 class) ────────────────────────


class _FakeIdleSession:
    """Streaming-session stand-in for idle/auto-sleep scheduling tests."""

    def __init__(
        self, *, idle_timeout: int = 60, inflight_active: bool = False
    ) -> None:
        from types import SimpleNamespace

        from pinky_daemon.transport_state import SessionState

        self.state = SessionState.CONNECTED
        self.last_active = 0.0
        self._config = SimpleNamespace(idle_timeout=idle_timeout)
        self.sleep_calls = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self._inflight_active = inflight_active

    @property
    def stats(self) -> dict:
        # #230 — the scheduler reads ``inflight_active`` to skip idle-sleeping a
        # session running a live Workflow/background turn.
        active = self._inflight_active
        return {
            "inflight_active": active,
            "inflight_turns": 1 if active else 0,
            "inflight_liveness_reason": (
                "background_transcript_recent" if active else "quiet"
            ),
            "inflight_liveness_age_s": 5.0 if active else None,
        }

    async def idle_sleep(self) -> bool:
        self.sleep_calls += 1
        self.started.set()
        await self.release.wait()
        return True


class TestIdleSleepNonBlocking:
    """idle_sleep() waits up to a minute for the pre-sleep memory-save turn,
    so the tick must spawn it as a background task. N idle agents awaited
    serially would stall every cron minute, heartbeat, and wake in the
    window — the same class of freeze #702 fixed for dreams.
    """

    @pytest.mark.asyncio
    async def test_idle_check_does_not_block_on_slow_idle_sleep(self, registry):
        ss = _FakeIdleSession()
        scheduler = AgentScheduler(
            registry, streaming_sessions_fn=lambda: {"ivan": {"main": ss}}
        )
        # Before the fix this await would hang until idle_sleep finished.
        await asyncio.wait_for(scheduler._check_idle_sessions(time.time()), timeout=2)
        await asyncio.wait_for(ss.started.wait(), timeout=2)
        task = scheduler._sleep_tasks[("ivan", "main")]
        assert not task.done()
        ss.release.set()
        await asyncio.wait_for(task, timeout=2)
        assert ss.sleep_calls == 1

    @pytest.mark.asyncio
    async def test_idle_sleep_overlap_guard_skips_refire(self, registry):
        ss = _FakeIdleSession()
        scheduler = AgentScheduler(
            registry, streaming_sessions_fn=lambda: {"ivan": {"main": ss}}
        )
        await scheduler._check_idle_sessions(time.time())
        await asyncio.sleep(0)
        # Session still CONNECTED mid-save: the next tick must not fire a
        # second idle_sleep (second save prompt) for the same (agent, label).
        await scheduler._check_idle_sessions(time.time())
        await asyncio.sleep(0)
        assert ss.sleep_calls == 1
        ss.release.set()
        await asyncio.wait_for(scheduler._sleep_tasks[("ivan", "main")], timeout=2)

    @pytest.mark.asyncio
    async def test_auto_sleep_fallback_does_not_block_tick(self, registry):
        registry.register("ivan", model="opus", auto_sleep_hours=1)
        ss = _FakeIdleSession(idle_timeout=0)
        scheduler = AgentScheduler(
            registry, streaming_sessions_fn=lambda: {"ivan": {"main": ss}}
        )
        await asyncio.wait_for(scheduler._check_auto_sleep(time.time()), timeout=2)
        await asyncio.wait_for(ss.started.wait(), timeout=2)
        assert ss.sleep_calls == 1
        ss.release.set()
        await asyncio.wait_for(scheduler._sleep_tasks[("ivan", "main")], timeout=2)

    @pytest.mark.asyncio
    async def test_stop_cancels_inflight_idle_sleep(self, registry):
        ss = _FakeIdleSession()
        scheduler = AgentScheduler(
            registry, streaming_sessions_fn=lambda: {"ivan": {"main": ss}}
        )
        await scheduler._check_idle_sessions(time.time())
        await asyncio.sleep(0)
        await scheduler.stop()
        assert scheduler._sleep_tasks == {}

    @pytest.mark.asyncio
    async def test_inflight_active_skips_idle_sleep(self, registry):
        # A session running a live Workflow/background turn must NOT be
        # idle-slept even though last_active is stale (#230).
        ss = _FakeIdleSession(inflight_active=True)
        scheduler = AgentScheduler(
            registry, streaming_sessions_fn=lambda: {"ivan": {"main": ss}}
        )
        await scheduler._check_idle_sessions(time.time())
        await asyncio.sleep(0)
        assert ss.sleep_calls == 0
        assert ("ivan", "main") not in scheduler._sleep_tasks

    @pytest.mark.asyncio
    async def test_inflight_inactive_still_sleeps(self, registry):
        # The carve-out RELEASES when there's no live work: a quiet/finished
        # session still sleeps normally (#230).
        ss = _FakeIdleSession(inflight_active=False)
        scheduler = AgentScheduler(
            registry, streaming_sessions_fn=lambda: {"ivan": {"main": ss}}
        )
        await scheduler._check_idle_sessions(time.time())
        await asyncio.wait_for(ss.started.wait(), timeout=2)
        assert ss.sleep_calls == 1
        ss.release.set()
        await asyncio.wait_for(scheduler._sleep_tasks[("ivan", "main")], timeout=2)

    # #230 (Murzik #825 review) — _check_auto_sleep runs BEFORE
    # _check_idle_sessions in _tick() and shares the stale-last_active threshold
    # (auto_sleep_hours also feeds idle_timeout), so it needs the SAME carve-out.

    @pytest.mark.asyncio
    async def test_auto_sleep_fallback_skipped_when_inflight_active(self, registry):
        registry.register("ivan", model="opus", auto_sleep_hours=1)
        ss = _FakeIdleSession(idle_timeout=0, inflight_active=True)
        scheduler = AgentScheduler(
            registry, streaming_sessions_fn=lambda: {"ivan": {"main": ss}}
        )
        await scheduler._check_auto_sleep(time.time())
        await asyncio.sleep(0)
        assert ss.sleep_calls == 0
        assert ("ivan", "main") not in scheduler._sleep_tasks

    @pytest.mark.asyncio
    async def test_auto_sleep_callback_skipped_when_inflight_active(self, registry):
        registry.register("ivan", model="opus", auto_sleep_hours=1)
        ss = _FakeIdleSession(idle_timeout=0, inflight_active=True)
        calls = []

        async def _cb(agent_name, reason):
            calls.append(agent_name)

        scheduler = AgentScheduler(
            registry,
            streaming_sessions_fn=lambda: {"ivan": {"main": ss}},
            auto_sleep_callback=_cb,
        )
        await scheduler._check_auto_sleep(time.time())
        assert calls == []

    @pytest.mark.asyncio
    async def test_auto_sleep_still_fires_when_inactive(self, registry):
        registry.register("ivan", model="opus", auto_sleep_hours=1)
        ss = _FakeIdleSession(idle_timeout=0, inflight_active=False)
        scheduler = AgentScheduler(
            registry, streaming_sessions_fn=lambda: {"ivan": {"main": ss}}
        )
        await asyncio.wait_for(scheduler._check_auto_sleep(time.time()), timeout=2)
        await asyncio.wait_for(ss.started.wait(), timeout=2)
        assert ss.sleep_calls == 1
        ss.release.set()
        await asyncio.wait_for(scheduler._sleep_tasks[("ivan", "main")], timeout=2)


# ── Non-blocking dream/librarian fires (issue #702) ────────────────────────


class TestDreamNonBlocking:
    """Dream/librarian callbacks must run as background tasks. The old inline
    await froze the tick loop for the dream's full duration (~1h with KG
    extraction), silently skipping every cron schedule in the window (#702).
    """

    @pytest.mark.asyncio
    async def test_dream_fire_does_not_block_check(self, registry):
        registry.register(
            "ivan", model="opus", dream_enabled=True, dream_schedule="* * * * *"
        )
        started = asyncio.Event()
        release = asyncio.Event()

        async def dream_cb(agent_name, agent):
            started.set()
            await release.wait()

        scheduler = AgentScheduler(registry, dream_callback=dream_cb)
        # Before #702 this await would hang until the dream finished
        await asyncio.wait_for(scheduler._check_dreams(time.time()), timeout=2)
        await asyncio.wait_for(started.wait(), timeout=2)
        task = scheduler._dream_tasks["ivan"]
        assert not task.done()
        release.set()
        await asyncio.wait_for(task, timeout=2)

    @pytest.mark.asyncio
    async def test_schedules_fire_while_dream_runs(self, registry):
        """The #702 regression: a cron schedule due mid-dream must still fire."""
        registry.register(
            "ivan", model="opus", dream_enabled=True, dream_schedule="* * * * *"
        )
        registry.add_schedule("ivan", "* * * * *", name="mid-dream", prompt="hi")
        release = asyncio.Event()
        fired = []
        receipt = asyncio.get_running_loop().create_future()

        async def dream_cb(agent_name, agent):
            await release.wait()

        async def wake_cb(agent_name, session_id, prompt):
            fired.append(agent_name)
            return receipt

        scheduler = AgentScheduler(
            registry, dream_callback=dream_cb, wake_callback=wake_cb
        )
        now = time.time()
        await asyncio.wait_for(scheduler._check_dreams(now), timeout=2)
        await asyncio.wait_for(scheduler._check_schedules(now), timeout=2)
        assert fired == ["ivan"]
        delivery_tasks = list(scheduler._schedule_delivery_tasks)
        assert delivery_tasks
        assert all(not task.done() for task in delivery_tasks)
        receipt.set_result(True)
        await asyncio.wait_for(asyncio.gather(*delivery_tasks), timeout=2)
        release.set()
        await asyncio.wait_for(scheduler._dream_tasks["ivan"], timeout=2)

    @pytest.mark.asyncio
    async def test_overlap_guard_skips_refire(self, registry):
        registry.register(
            "ivan", model="opus", dream_enabled=True, dream_schedule="* * * * *"
        )
        starts = []
        release = asyncio.Event()

        async def dream_cb(agent_name, agent):
            starts.append(agent_name)
            await release.wait()

        scheduler = AgentScheduler(registry, dream_callback=dream_cb)
        await scheduler._check_dreams(time.time())
        await asyncio.sleep(0)  # let the background task start
        # Bypass the (date, minute) dedup to simulate the next cron minute
        scheduler._last_dream_check.clear()
        await scheduler._check_dreams(time.time())
        await asyncio.sleep(0)
        assert starts == ["ivan"]
        release.set()
        await asyncio.wait_for(scheduler._dream_tasks["ivan"], timeout=2)

    @pytest.mark.asyncio
    async def test_callback_exception_is_contained(self, registry):
        registry.register(
            "ivan", model="opus", dream_enabled=True, dream_schedule="* * * * *"
        )

        async def dream_cb(agent_name, agent):
            raise RuntimeError("boom")

        scheduler = AgentScheduler(registry, dream_callback=dream_cb)
        await scheduler._check_dreams(time.time())
        # Must not raise out of the task wrapper
        await asyncio.wait_for(scheduler._dream_tasks["ivan"], timeout=2)

    @pytest.mark.asyncio
    async def test_stop_cancels_inflight_dream(self, registry):
        registry.register(
            "ivan", model="opus", dream_enabled=True, dream_schedule="* * * * *"
        )
        cancelled = asyncio.Event()

        async def dream_cb(agent_name, agent):
            try:
                await asyncio.Event().wait()  # blocks until cancelled
            except asyncio.CancelledError:
                cancelled.set()
                raise

        scheduler = AgentScheduler(registry, dream_callback=dream_cb)
        await scheduler._check_dreams(time.time())
        await asyncio.sleep(0)
        await scheduler.stop()
        assert cancelled.is_set()
        assert scheduler._dream_tasks == {}

    @pytest.mark.asyncio
    async def test_librarian_fire_does_not_block_check(self, registry):
        registry.register(
            "ivan",
            model="opus",
            librarian_enabled=True,
            librarian_schedule="* * * * *",
        )
        started = asyncio.Event()
        release = asyncio.Event()

        async def lib_cb(agent_name, agent):
            started.set()
            await release.wait()

        scheduler = AgentScheduler(registry, librarian_callback=lib_cb)
        await asyncio.wait_for(scheduler._check_librarian(time.time()), timeout=2)
        await asyncio.wait_for(started.wait(), timeout=2)
        release.set()
        await asyncio.wait_for(
            scheduler._librarian_tasks[AgentScheduler.LIBRARIAN_GLOBAL_KEY], timeout=2
        )

    @pytest.mark.asyncio
    async def test_librarian_guard_is_global_across_agents(self, registry):
        """The librarian curates one shared KBStore: two librarian-enabled
        agents due in the same minute must NOT run concurrently — the second
        fire is skipped while the first is in flight (global slot, not
        per-agent). The old inline await serialized these; per-agent tasks
        would race the shared raw/wiki store.
        """
        registry.register(
            "ivan",
            model="opus",
            librarian_enabled=True,
            librarian_schedule="* * * * *",
        )
        registry.register(
            "petr",
            model="opus",
            librarian_enabled=True,
            librarian_schedule="* * * * *",
        )
        starts = []
        release = asyncio.Event()

        async def lib_cb(agent_name, agent):
            starts.append(agent_name)
            await release.wait()

        scheduler = AgentScheduler(registry, librarian_callback=lib_cb)
        # Both agents match the same cron minute in one check pass
        await asyncio.wait_for(scheduler._check_librarian(time.time()), timeout=2)
        await asyncio.sleep(0)  # let the background task start
        # Bypass the (date, minute) dedup to simulate later cron minutes too
        scheduler._last_librarian_check.clear()
        await asyncio.wait_for(scheduler._check_librarian(time.time()), timeout=2)
        await asyncio.sleep(0)
        # Only ONE shared-KB run started, fleet-wide
        assert len(starts) == 1
        release.set()
        await asyncio.wait_for(
            scheduler._librarian_tasks[AgentScheduler.LIBRARIAN_GLOBAL_KEY], timeout=2
        )
        # Once the in-flight run finishes, a later fire can start again
        scheduler._last_librarian_check.clear()
        await asyncio.wait_for(scheduler._check_librarian(time.time()), timeout=2)
        await asyncio.sleep(0)
        assert len(starts) == 2
        release.set()
        await asyncio.wait_for(
            scheduler._librarian_tasks[AgentScheduler.LIBRARIAN_GLOBAL_KEY], timeout=2
        )


# -- URL Watcher Tests ---------------------------------------


class _StubTrigger:
    def __init__(self) -> None:
        self.id = 1
        self.name = "watch"
        self.agent_name = "ivan"
        self.url = "http://example.invalid/status"
        self.method = "GET"
        self.condition = "status_is"
        self.condition_value = "200"
        self.last_value = ""
        self.prompt_template = ""


class _StubTriggerStore:
    def __init__(self) -> None:
        self.checks: list = []
        self.fires: list = []

    def record_check(self, trigger_id, value):
        self.checks.append((trigger_id, value))

    def record_fire(self, trigger_id):
        self.fires.append(trigger_id)


class TestUrlWatcherOffLoop:
    @pytest.mark.asyncio
    async def test_fetch_runs_off_event_loop(self, registry, monkeypatch):
        """The urlopen+read must run in a worker thread, not block the shared
        event loop (which also serves the API, pollers, and broker)."""
        import threading
        import urllib.request

        loop_thread = threading.current_thread()
        seen = {}

        class _Resp:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self, n=-1):
                return b"ok"

        def _fake_urlopen(req, timeout=None):
            seen["thread"] = threading.current_thread()
            return _Resp()

        monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)

        store = _StubTriggerStore()
        fired = []

        async def wake_cb(agent_name, session_id, prompt):
            fired.append(agent_name)

        scheduler = AgentScheduler(registry, wake_callback=wake_cb, trigger_store=store)
        await scheduler._poll_url_trigger(_StubTrigger(), time.time())

        assert seen["thread"] is not loop_thread
        assert fired == ["ivan"]
        assert store.fires == [1]
        assert store.checks == [(1, "200")]


class TestDiscardTombstone:
    """#566 — discard must tombstone (park), not delete, so a retired fire
    cannot be re-created and re-fired by a later reconciliation."""

    def _persist(self, registry, *, schedule_id, agent, fired_at):
        return registry.persist_schedule_wake(
            schedule_id,
            agent_name=agent,
            schedule_name="one-shot",
            prompt="wake prompt",
            fired_at=fired_at,
        )

    def test_discard_tombstones_instead_of_deleting(self, registry):
        registry.register("oleg")
        s = registry.add_schedule(
            "oleg", "0 8 * * *", name="one-shot", prompt="p"
        )
        row, created = self._persist(
            registry, schedule_id=s.id, agent="oleg", fired_at=1000.0
        )
        assert created
        assert any(
            r.id == row.id
            for r in registry.list_pending_schedule_wakes("oleg")
        )

        assert (
            registry.discard_pending_schedule_wake(row.id, agent_name="oleg")
            is True
        )

        # Not deleted: excluded from the active outbox, present as a tombstone.
        assert all(
            r.id != row.id
            for r in registry.list_pending_schedule_wakes("oleg")
        )
        parked = registry.list_pending_schedule_wakes(
            "oleg", include_parked=True
        )
        tomb = next(r for r in parked if r.id == row.id)
        assert tomb.parked_at > 0

    def test_discarded_fire_cannot_be_recreated(self, registry):
        # The zombie-refire regression: after discard, re-persisting the SAME
        # (schedule_id, fired_at) must NOT create a new active row — the parked
        # tombstone still holds the UNIQUE key so INSERT OR IGNORE no-ops.
        registry.register("oleg")
        s = registry.add_schedule(
            "oleg", "0 8 * * *", name="one-shot", prompt="p"
        )
        row, _ = self._persist(
            registry, schedule_id=s.id, agent="oleg", fired_at=1000.0
        )
        registry.discard_pending_schedule_wake(row.id, agent_name="oleg")

        _row2, created2 = self._persist(
            registry, schedule_id=s.id, agent="oleg", fired_at=1000.0
        )
        assert created2 is False
        assert registry.list_pending_schedule_wakes("oleg") == []

    def test_discard_is_noop_on_already_terminal_row(self, registry):
        registry.register("oleg")
        s = registry.add_schedule(
            "oleg", "0 8 * * *", name="one-shot", prompt="p"
        )
        row, _ = self._persist(
            registry, schedule_id=s.id, agent="oleg", fired_at=1000.0
        )
        assert (
            registry.discard_pending_schedule_wake(row.id, agent_name="oleg")
            is True
        )
        # Second discard on the now-parked (terminal) row is a no-op.
        assert (
            registry.discard_pending_schedule_wake(row.id, agent_name="oleg")
            is False
        )

    @pytest.mark.asyncio
    async def test_discarded_fire_is_not_re_delivered(self, registry):
        # A fire discarded (parked) while its cohort task waited behind the
        # per-agent delivery lock must NOT be delivered by the queued live
        # path — delivering would resurrect the tombstone (confirm clears it).
        registry.register("oleg")
        schedule = registry.add_schedule(
            "oleg", "0 8 * * *", name="one-shot", prompt="run once"
        )
        fired = 1000.0
        schedule.last_run = fired
        row, _ = self._persist(
            registry, schedule_id=schedule.id, agent="oleg", fired_at=fired
        )
        registry.discard_pending_schedule_wake(row.id, agent_name="oleg")

        calls: list[str] = []

        async def wake_cb(agent_name, session_id, prompt):
            calls.append(prompt)
            return True

        scheduler = AgentScheduler(registry, wake_callback=wake_cb)
        await scheduler._deliver_schedule(schedule)

        assert calls == []  # the retired fire is not delivered
        tomb = registry.get_schedule_wake_by_fire(schedule.id, fired)
        assert tomb is not None
        assert tomb.parked_at > 0 and tomb.accepted_at == 0  # tombstone intact


class TestCohortStalenessGuard:
    """#567 — a fire that aged past its replay window while its cohort task
    waited behind the per-agent delivery lock must be dropped, not pasted."""

    @pytest.mark.asyncio
    async def test_stale_cohort_fire_is_dropped_not_delivered(
        self, registry, monkeypatch
    ):
        registry.register("oleg")
        schedule = registry.add_schedule(
            "oleg", "0 8 * * *", name="daily", prompt="stale work"
        )
        now = 1_800_000_000.0
        schedule.last_run = now - 3_601.0  # older than the 3600s ceiling
        registry.persist_schedule_wake(
            schedule.id,
            agent_name="oleg",
            schedule_name="daily",
            prompt="stale work",
            fired_at=schedule.last_run,
        )
        calls: list[str] = []

        async def wake_cb(agent_name, session_id, prompt):
            calls.append(prompt)
            return True

        monkeypatch.setattr("pinky_daemon.scheduler.time.time", lambda: now)
        scheduler = AgentScheduler(
            registry, wake_callback=wake_cb, pending_wake_max_age_sec=3600.0
        )
        await scheduler._deliver_schedule(schedule)

        assert calls == []  # stale fire is not delivered
        # and its outbox row is dropped
        assert (
            registry.get_schedule_wake_by_fire(schedule.id, schedule.last_run)
            is None
        )
        notices = registry.list_recurring_schedule_stale_drops("oleg")
        assert len(notices) == 1
        assert notices[0].schedule_id == schedule.id
        assert notices[0].drop_count == 1

    @pytest.mark.asyncio
    async def test_stale_one_shot_cohort_drop_alerts_owner(
        self, registry, monkeypatch
    ):
        # A queued one-shot that goes stale is dropped AND owner-alerted (it
        # was auto-disabled on fire, so the owed work is otherwise lost with no
        # next occurrence) — mirrors the replay path.
        registry.register("oleg")
        schedule = registry.add_schedule(
            "oleg",
            "0 8 * * *",
            name="one-shot",
            prompt="owed work",
            one_shot=True,
        )
        now = 1_800_000_000.0
        schedule.last_run = now - 3_601.0
        registry.persist_schedule_wake(
            schedule.id,
            agent_name="oleg",
            schedule_name="one-shot",
            prompt="owed work",
            fired_at=schedule.last_run,
        )
        calls: list[str] = []
        alerts: list[tuple[str, str]] = []

        async def wake_cb(agent_name, session_id, prompt):
            calls.append(prompt)
            return True

        async def owner_notify(agent_name, message):
            alerts.append((agent_name, message))
            return True

        monkeypatch.setattr("pinky_daemon.scheduler.time.time", lambda: now)
        scheduler = AgentScheduler(
            registry,
            wake_callback=wake_cb,
            owner_notify_callback=owner_notify,
            pending_wake_max_age_sec=3600.0,
        )
        await scheduler._deliver_schedule(schedule)
        await asyncio.gather(*list(scheduler._owner_alert_tasks))

        assert calls == []  # not delivered
        assert (
            registry.get_schedule_wake_by_fire(schedule.id, schedule.last_run)
            is None
        )  # dropped
        assert len(alerts) == 1  # owner alerted about the lost one-shot
        assert alerts[0][0] == "oleg"
        assert "STALE ONE-SHOT WAKE DROPPED" in alerts[0][1]
        assert registry.list_recurring_schedule_stale_drops("oleg") == []


class TestRecurringStaleDropSurfacing:
    """#1053 — recurring stale drops surface to the agent, never the owner."""

    def test_no_subsequent_wake_stays_bounded_and_cascades(self, registry):
        registry.register("oleg")
        schedule = registry.add_schedule(
            "oleg", "* * * * *", name="hourly-ish", prompt="work"
        )

        for offset in range(100):
            registry.record_recurring_schedule_stale_drop(
                schedule.id,
                agent_name="oleg",
                schedule_name=schedule.name,
                dropped_at=1_800_000_000.0 + offset,
                row_age_s=61.0 + offset,
            )

        notices = registry.list_recurring_schedule_stale_drops("oleg")
        assert len(notices) == 1
        assert notices[0].drop_count == 100
        assert notices[0].generation == 100
        assert notices[0].first_dropped_at == pytest.approx(1_800_000_000.0)
        assert notices[0].last_dropped_at == pytest.approx(1_800_000_099.0)
        assert notices[0].max_row_age_s == pytest.approx(160.0)
        assert registry.list_pending_schedule_wakes("oleg") == []

        assert registry.remove_schedule(schedule.id) is True
        assert registry.list_recurring_schedule_stale_drops("oleg") == []

    def test_surface_ack_does_not_erase_concurrent_new_drop(self, registry):
        registry.register("oleg")
        schedule = registry.add_schedule(
            "oleg", "* * * * *", name="concurrent", prompt="work"
        )
        registry.record_recurring_schedule_stale_drop(
            schedule.id,
            agent_name="oleg",
            schedule_name=schedule.name,
            dropped_at=1_800_000_000.0,
            row_age_s=61.0,
        )
        surfaced_snapshot = registry.list_recurring_schedule_stale_drops("oleg")

        registry.record_recurring_schedule_stale_drop(
            schedule.id,
            agent_name="oleg",
            schedule_name=schedule.name,
            dropped_at=1_800_000_001.0,
            row_age_s=62.0,
        )

        assert (
            registry.acknowledge_recurring_schedule_stale_drops(
                "oleg", surfaced_snapshot
            )
            == 0
        )
        retained = registry.list_recurring_schedule_stale_drops("oleg")
        assert len(retained) == 1
        assert retained[0].drop_count == 2
        assert retained[0].generation == 2

    def test_late_ack_cannot_erase_post_ack_drop(self, registry):
        """A retained revision prevents delete/reinsert ABA acknowledgement."""
        registry.register("worker")
        casualty = registry.add_schedule(
            "worker", "* * * * *", name="casualty", prompt="work"
        )
        first_at = 1_800_000_000.0
        registry.record_recurring_schedule_stale_drop(
            casualty.id,
            agent_name="worker",
            schedule_name=casualty.name,
            dropped_at=first_at,
            row_age_s=61.0,
        )

        abandoned_snapshot = registry.list_recurring_schedule_stale_drops(
            "worker"
        )
        superseding_snapshot = list(abandoned_snapshot)
        assert {notice.generation for notice in abandoned_snapshot} == {1}

        assert (
            registry.acknowledge_recurring_schedule_stale_drops(
                "worker", superseding_snapshot
            )
            == 1
        )
        registry.record_recurring_schedule_stale_drop(
            casualty.id,
            agent_name="worker",
            schedule_name=casualty.name,
            dropped_at=first_at + 1.0,
            row_age_s=62.0,
        )

        assert (
            registry.acknowledge_recurring_schedule_stale_drops(
                "worker", abandoned_snapshot
            )
            == 0
        )
        retained = registry.list_recurring_schedule_stale_drops("worker")
        assert len(retained) == 1
        assert retained[0].schedule_id == casualty.id
        assert retained[0].drop_count == 1
        assert retained[0].generation == 2

    @pytest.mark.asyncio
    async def test_both_drop_sites_aggregate_surface_and_clear_once(
        self, registry, monkeypatch
    ):
        registry.register("oleg")
        lost = registry.add_schedule(
            "oleg", "* * * * *", name="hourly sweep", prompt="lost work"
        )
        next_wake = registry.add_schedule(
            "oleg", "* * * * *", name="next wake", prompt="new work"
        )
        now = 1_800_000_000.0
        monkeypatch.setattr("pinky_daemon.scheduler.time.time", lambda: now)
        attempts: list[str] = []
        owner_alerts: list[tuple[str, str]] = []

        async def confirmed(agent_name, session_id, prompt):
            del agent_name, session_id
            attempts.append(prompt)
            return True

        async def owner_notify(agent_name, message):
            owner_alerts.append((agent_name, message))
            return True

        scheduler = AgentScheduler(
            registry,
            wake_callback=confirmed,
            owner_notify_callback=owner_notify,
            pending_wake_max_age_sec=3_600.0,
        )

        # Replay-path stale drop.
        replay_fire = now - 62.0
        registry.persist_schedule_wake(
            lost.id,
            agent_name="oleg",
            schedule_name=lost.name,
            prompt=lost.prompt,
            fired_at=replay_fire,
        )
        await scheduler._replay_pending_locked("oleg")

        # Live-cohort stale drop for the same schedule.
        lost.last_run = now - 61.0
        registry.persist_schedule_wake(
            lost.id,
            agent_name="oleg",
            schedule_name=lost.name,
            prompt=lost.prompt,
            fired_at=lost.last_run,
        )
        await scheduler._deliver_schedule(lost)

        notices = registry.list_recurring_schedule_stale_drops("oleg")
        assert len(notices) == 1
        assert notices[0].schedule_id == lost.id
        assert notices[0].drop_count == 2
        assert attempts == []
        assert owner_alerts == []
        assert registry.list_pending_schedule_wakes("oleg") == []

        # The notice registry alone never triggers or resurrects a wake.
        await scheduler._replay_pending_locked("oleg")
        assert attempts == []
        assert len(registry.list_recurring_schedule_stale_drops("oleg")) == 1

        # The next genuinely delivered wake carries one aggregate and clears it.
        next_wake.last_run = now - 1.0
        await scheduler._deliver_schedule(next_wake)
        assert len(attempts) == 1
        assert "Note: 2 fires of recurring schedule 'hourly sweep'" in attempts[0]
        assert "The work those fires would have done was NOT performed." in attempts[0]
        assert attempts[0].endswith("\n\nnew work")
        assert registry.list_recurring_schedule_stale_drops("oleg") == []

        # A later wake sees neither a duplicate note nor any owner delivery.
        next_wake.last_run = now
        await scheduler._deliver_schedule(next_wake)
        assert attempts[1] == "new work"
        assert owner_alerts == []

    @pytest.mark.asyncio
    async def test_unconfirmed_wake_keeps_note_for_next_success(
        self, registry, monkeypatch
    ):
        registry.register("oleg")
        dropped = registry.add_schedule(
            "oleg", "* * * * *", name="dropped", prompt="lost work"
        )
        delivery = registry.add_schedule(
            "oleg", "* * * * *", name="delivery", prompt="new work"
        )
        now = 1_800_000_000.0
        monkeypatch.setattr("pinky_daemon.scheduler.time.time", lambda: now)
        registry.record_recurring_schedule_stale_drop(
            dropped.id,
            agent_name="oleg",
            schedule_name=dropped.name,
            dropped_at=now - 1.0,
            row_age_s=61.0,
        )
        delivery.last_run = now - 1.0
        attempts: list[str] = []

        async def first_fails(agent_name, session_id, prompt):
            del agent_name, session_id
            attempts.append(prompt)
            return False

        scheduler = AgentScheduler(registry, wake_callback=first_fails)
        await scheduler._deliver_schedule(delivery)

        assert "Note: 1 fire of recurring schedule 'dropped'" in attempts[0]
        assert len(registry.list_recurring_schedule_stale_drops("oleg")) == 1

        async def then_succeeds(agent_name, session_id, prompt):
            del agent_name, session_id
            attempts.append(prompt)
            return True

        scheduler._wake_callback = then_succeeds
        await scheduler._replay_pending_locked("oleg")

        assert "Note: 1 fire of recurring schedule 'dropped'" in attempts[1]
        assert attempts[1].endswith("\n\nnew work")
        assert registry.list_recurring_schedule_stale_drops("oleg") == []

    @pytest.mark.asyncio
    async def test_no_drops_leaves_wake_prompt_unchanged(
        self, registry, monkeypatch
    ):
        registry.register("oleg")
        schedule = registry.add_schedule(
            "oleg", "* * * * *", name="ordinary", prompt="ordinary work"
        )
        now = 1_800_000_000.0
        schedule.last_run = now
        monkeypatch.setattr("pinky_daemon.scheduler.time.time", lambda: now)
        attempts: list[str] = []

        async def confirmed(agent_name, session_id, prompt):
            del agent_name, session_id
            attempts.append(prompt)
            return True

        scheduler = AgentScheduler(registry, wake_callback=confirmed)
        await scheduler._deliver_schedule(schedule)

        assert attempts == ["ordinary work"]
        assert registry.list_recurring_schedule_stale_drops("oleg") == []
