"""Tests for pinky_daemon.trigger_store.TriggerStore.

Uses tmp_path for isolated SQLite databases — no external dependencies.
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from pinky_daemon.trigger_store import Trigger, TriggerStore


# ── Helpers ────────────────────────────────────────────────────────────────────

def _store(tmp_path: Path) -> TriggerStore:
    return TriggerStore(str(tmp_path / "triggers.db"))


def _webhook(store: TriggerStore, name: str = "wh1", agent: str = "barsik") -> Trigger:
    return store.create(agent, name, "webhook")


def _url_trigger(
    store: TriggerStore,
    name: str = "url1",
    agent: str = "barsik",
    interval: int = 300,
) -> Trigger:
    return store.create(
        agent, name, "url",
        url="https://example.com/status",
        condition="status_change",
        interval_seconds=interval,
    )


def _file_trigger(store: TriggerStore, name: str = "file1", agent: str = "barsik") -> Trigger:
    return store.create(
        agent, name, "file",
        file_path="/tmp/watch.txt",
        condition="file_changed",
    )


# ── Trigger.to_dict ────────────────────────────────────────────────────────────

class TestTriggerToDict:
    def test_to_dict_basic_fields(self, tmp_path):
        store = _store(tmp_path)
        t = _url_trigger(store)
        d = t.to_dict()
        assert d["id"] == t.id
        assert d["agent_name"] == "barsik"
        assert d["name"] == "url1"
        assert d["trigger_type"] == "url"
        assert d["url"] == "https://example.com/status"
        assert "token" not in d  # token excluded by default

    def test_to_dict_include_token_false(self, tmp_path):
        store = _store(tmp_path)
        t = _webhook(store)
        d = t.to_dict(include_token=False)
        assert "token" not in d

    def test_to_dict_include_token_true(self, tmp_path):
        store = _store(tmp_path)
        t = _webhook(store)
        d = t.to_dict(include_token=True)
        assert "token" in d
        assert len(d["token"]) == 43  # secrets.token_urlsafe(32)

    def test_to_dict_no_token_for_non_webhook(self, tmp_path):
        store = _store(tmp_path)
        t = _url_trigger(store)
        d = t.to_dict(include_token=True)
        # url triggers have no token — key should not be present
        assert "token" not in d

    def test_to_dict_contains_all_fields(self, tmp_path):
        store = _store(tmp_path)
        t = _url_trigger(store)
        d = t.to_dict()
        for field in [
            "id", "agent_name", "name", "trigger_type", "url", "method",
            "condition", "condition_value", "file_path", "interval_seconds",
            "prompt_template", "enabled", "last_fired_at", "last_checked_at",
            "fire_count", "created_at",
        ]:
            assert field in d


# ── create ─────────────────────────────────────────────────────────────────────

class TestCreate:
    def test_create_webhook_generates_token(self, tmp_path):
        store = _store(tmp_path)
        t = _webhook(store)
        assert t.trigger_type == "webhook"
        assert len(t.token) == 43  # secrets.token_urlsafe(32)

    def test_create_url_trigger(self, tmp_path):
        store = _store(tmp_path)
        t = _url_trigger(store)
        assert t.trigger_type == "url"
        assert t.url == "https://example.com/status"
        assert t.token == ""  # no token for url triggers

    def test_create_file_trigger(self, tmp_path):
        store = _store(tmp_path)
        t = _file_trigger(store)
        assert t.trigger_type == "file"
        assert t.file_path == "/tmp/watch.txt"

    def test_create_assigns_id(self, tmp_path):
        store = _store(tmp_path)
        t = _webhook(store)
        assert t.id > 0

    def test_create_sets_created_at(self, tmp_path):
        store = _store(tmp_path)
        before = time.time()
        t = _webhook(store)
        after = time.time()
        assert before <= t.created_at <= after

    def test_create_defaults(self, tmp_path):
        store = _store(tmp_path)
        t = store.create("agent", "t", "url")
        assert t.enabled is True
        assert t.method == "GET"
        assert t.interval_seconds == 300
        assert t.fire_count == 0
        assert t.last_fired_at == 0.0

    def test_create_with_all_params(self, tmp_path):
        store = _store(tmp_path)
        t = store.create(
            "agent", "full", "url",
            url="https://api.example.com",
            method="POST",
            condition="json_path",
            condition_value='{"path": "$.status"}',
            interval_seconds=60,
            prompt_template="Status changed: {value}",
            enabled=False,
        )
        assert t.url == "https://api.example.com"
        assert t.method == "POST"
        assert t.condition == "json_path"
        assert t.interval_seconds == 60
        assert t.enabled is False

    def test_create_multiple_returns_different_ids(self, tmp_path):
        store = _store(tmp_path)
        t1 = _webhook(store, name="wh1")
        t2 = _webhook(store, name="wh2")
        assert t1.id != t2.id


# ── get ────────────────────────────────────────────────────────────────────────

class TestGet:
    def test_get_existing(self, tmp_path):
        store = _store(tmp_path)
        created = _webhook(store)
        fetched = store.get(created.id)
        assert fetched is not None
        assert fetched.id == created.id
        assert fetched.name == "wh1"

    def test_get_nonexistent_returns_none(self, tmp_path):
        store = _store(tmp_path)
        assert store.get(99999) is None


# ── get_by_token ───────────────────────────────────────────────────────────────

class TestGetByToken:
    def test_get_by_token_found(self, tmp_path):
        store = _store(tmp_path)
        t = _webhook(store)
        fetched = store.get_by_token(t.token)
        assert fetched is not None
        assert fetched.id == t.id

    def test_get_by_token_not_found(self, tmp_path):
        store = _store(tmp_path)
        assert store.get_by_token("invalid-token-xyz") is None

    def test_get_by_token_disabled_not_returned(self, tmp_path):
        store = _store(tmp_path)
        t = _webhook(store)
        store.update(t.id, enabled=False)
        # Disabled triggers should not be returned by token lookup
        assert store.get_by_token(t.token) is None


# ── list ───────────────────────────────────────────────────────────────────────

class TestList:
    def test_list_all(self, tmp_path):
        store = _store(tmp_path)
        _webhook(store, name="w1", agent="a1")
        _url_trigger(store, name="u1", agent="a2")
        _file_trigger(store, name="f1", agent="a1")
        result = store.list()
        assert len(result) == 3

    def test_list_by_agent(self, tmp_path):
        store = _store(tmp_path)
        _webhook(store, name="w1", agent="barsik")
        _url_trigger(store, name="u1", agent="barsik")
        _webhook(store, name="w2", agent="other-agent")
        result = store.list(agent_name="barsik")
        assert len(result) == 2
        assert all(t.agent_name == "barsik" for t in result)

    def test_list_enabled_only(self, tmp_path):
        store = _store(tmp_path)
        t1 = _webhook(store, name="w1")
        t2 = _webhook(store, name="w2")
        store.update(t2.id, enabled=False)
        result = store.list(enabled_only=True)
        assert len(result) == 1
        assert result[0].id == t1.id

    def test_list_agent_and_enabled_only(self, tmp_path):
        store = _store(tmp_path)
        t1 = _webhook(store, name="w1", agent="barsik")
        t2 = _webhook(store, name="w2", agent="barsik")
        _webhook(store, name="w3", agent="other")
        store.update(t2.id, enabled=False)
        result = store.list(agent_name="barsik", enabled_only=True)
        assert len(result) == 1
        assert result[0].id == t1.id

    def test_list_empty(self, tmp_path):
        store = _store(tmp_path)
        assert store.list() == []

    def test_list_ordered_by_created_at_desc(self, tmp_path):
        store = _store(tmp_path)
        t1 = _webhook(store, name="w1")
        t2 = _webhook(store, name="w2")
        t3 = _webhook(store, name="w3")
        result = store.list()
        # Most recently created first
        assert result[0].id == t3.id
        assert result[-1].id == t1.id


# ── update ─────────────────────────────────────────────────────────────────────

class TestUpdate:
    def test_update_name(self, tmp_path):
        store = _store(tmp_path)
        t = _webhook(store)
        updated = store.update(t.id, name="new-name")
        assert updated.name == "new-name"

    def test_update_enabled(self, tmp_path):
        store = _store(tmp_path)
        t = _webhook(store)
        assert t.enabled is True
        updated = store.update(t.id, enabled=False)
        assert updated.enabled is False

    def test_update_multiple_fields(self, tmp_path):
        store = _store(tmp_path)
        t = _url_trigger(store)
        updated = store.update(
            t.id,
            url="https://new.example.com",
            method="POST",
            interval_seconds=60,
        )
        assert updated.url == "https://new.example.com"
        assert updated.method == "POST"
        assert updated.interval_seconds == 60

    def test_update_no_fields_returns_current(self, tmp_path):
        store = _store(tmp_path)
        t = _webhook(store)
        result = store.update(t.id)
        assert result.id == t.id
        assert result.name == t.name

    def test_update_disallowed_fields_ignored(self, tmp_path):
        store = _store(tmp_path)
        t = _webhook(store)
        original_type = t.trigger_type
        # trigger_type is not in the allowed set
        result = store.update(t.id, trigger_type="file", name="new-name")
        assert result.trigger_type == original_type
        assert result.name == "new-name"

    def test_update_nonexistent_returns_none(self, tmp_path):
        store = _store(tmp_path)
        result = store.update(99999, name="ghost")
        assert result is None

    def test_update_prompt_template(self, tmp_path):
        store = _store(tmp_path)
        t = _url_trigger(store)
        updated = store.update(t.id, prompt_template="New value: {value}")
        assert updated.prompt_template == "New value: {value}"


# ── delete ─────────────────────────────────────────────────────────────────────

class TestDelete:
    def test_delete_existing(self, tmp_path):
        store = _store(tmp_path)
        t = _webhook(store)
        result = store.delete(t.id)
        assert result is True
        assert store.get(t.id) is None

    def test_delete_nonexistent(self, tmp_path):
        store = _store(tmp_path)
        result = store.delete(99999)
        assert result is False

    def test_delete_reduces_list(self, tmp_path):
        store = _store(tmp_path)
        t1 = _webhook(store, name="w1")
        t2 = _webhook(store, name="w2")
        store.delete(t1.id)
        remaining = store.list()
        assert len(remaining) == 1
        assert remaining[0].id == t2.id


# ── rotate_token ───────────────────────────────────────────────────────────────

class TestRotateToken:
    def test_rotate_generates_new_token(self, tmp_path):
        store = _store(tmp_path)
        t = _webhook(store)
        old_token = t.token
        new_token = store.rotate_token(t.id)
        assert new_token is not None
        assert new_token != old_token
        assert len(new_token) == 43  # secrets.token_urlsafe(32)

    def test_rotate_old_token_no_longer_valid(self, tmp_path):
        store = _store(tmp_path)
        t = _webhook(store)
        old_token = t.token
        store.rotate_token(t.id)
        assert store.get_by_token(old_token) is None

    def test_rotate_new_token_works(self, tmp_path):
        store = _store(tmp_path)
        t = _webhook(store)
        new_token = store.rotate_token(t.id)
        fetched = store.get_by_token(new_token)
        assert fetched is not None
        assert fetched.id == t.id

    def test_rotate_non_webhook_returns_none(self, tmp_path):
        store = _store(tmp_path)
        t = _url_trigger(store)
        result = store.rotate_token(t.id)
        assert result is None

    def test_rotate_nonexistent_returns_none(self, tmp_path):
        store = _store(tmp_path)
        result = store.rotate_token(99999)
        assert result is None


# ── record_fire ────────────────────────────────────────────────────────────────

class TestRecordFire:
    def test_increments_fire_count(self, tmp_path):
        store = _store(tmp_path)
        t = _webhook(store)
        assert t.fire_count == 0
        store.record_fire(t.id)
        updated = store.get(t.id)
        assert updated.fire_count == 1

    def test_multiple_fires(self, tmp_path):
        store = _store(tmp_path)
        t = _webhook(store)
        store.record_fire(t.id)
        store.record_fire(t.id)
        store.record_fire(t.id)
        assert store.get(t.id).fire_count == 3

    def test_sets_last_fired_at(self, tmp_path):
        store = _store(tmp_path)
        t = _webhook(store)
        before = time.time()
        store.record_fire(t.id)
        after = time.time()
        updated = store.get(t.id)
        assert before <= updated.last_fired_at <= after


# ── record_check ───────────────────────────────────────────────────────────────

class TestRecordCheck:
    def test_updates_last_checked_at(self, tmp_path):
        store = _store(tmp_path)
        t = _url_trigger(store)
        before = time.time()
        store.record_check(t.id, "200")
        after = time.time()
        updated = store.get(t.id)
        assert before <= updated.last_checked_at <= after

    def test_updates_last_value(self, tmp_path):
        store = _store(tmp_path)
        t = _url_trigger(store)
        store.record_check(t.id, '{"status": "ok"}')
        updated = store.get(t.id)
        assert updated.last_value == '{"status": "ok"}'

    def test_last_value_can_be_updated_multiple_times(self, tmp_path):
        store = _store(tmp_path)
        t = _url_trigger(store)
        store.record_check(t.id, "first")
        store.record_check(t.id, "second")
        assert store.get(t.id).last_value == "second"


# ── list_due_url_watchers ──────────────────────────────────────────────────────

class TestListDueUrlWatchers:
    def test_returns_overdue_triggers(self, tmp_path):
        store = _store(tmp_path)
        t = _url_trigger(store, interval=300)
        # last_checked_at = 0, so 0 + 300 = 300; any now > 300 makes it due
        due = store.list_due_url_watchers(now=1000.0)
        assert len(due) == 1
        assert due[0].id == t.id

    def test_not_due_not_returned(self, tmp_path):
        store = _store(tmp_path)
        t = _url_trigger(store, interval=300)
        # Record a recent check
        store.record_check(t.id, "ok")
        # Now + 100 seconds is not enough (need 300)
        now = store.get(t.id).last_checked_at + 100
        due = store.list_due_url_watchers(now=now)
        assert len(due) == 0

    def test_exactly_at_interval_is_due(self, tmp_path):
        store = _store(tmp_path)
        t = _url_trigger(store, interval=300)
        store.record_check(t.id, "ok")
        check_time = store.get(t.id).last_checked_at
        # Exactly at the boundary: last_checked_at + 300 <= now
        due = store.list_due_url_watchers(now=check_time + 300)
        assert len(due) == 1

    def test_disabled_not_returned(self, tmp_path):
        store = _store(tmp_path)
        t = _url_trigger(store, interval=300)
        store.update(t.id, enabled=False)
        due = store.list_due_url_watchers(now=9999.0)
        assert len(due) == 0

    def test_non_url_triggers_not_returned(self, tmp_path):
        store = _store(tmp_path)
        _webhook(store)
        _file_trigger(store)
        due = store.list_due_url_watchers(now=9999.0)
        assert len(due) == 0

    def test_multiple_due_ordered_by_last_checked_asc(self, tmp_path):
        store = _store(tmp_path)
        t1 = _url_trigger(store, name="u1", interval=300)
        t2 = _url_trigger(store, name="u2", interval=300)
        # Give t2 a more recent check
        store.record_check(t1.id, "a")
        time.sleep(0.01)
        store.record_check(t2.id, "b")
        now = time.time() + 500
        due = store.list_due_url_watchers(now=now)
        assert len(due) == 2
        # t1 was checked earlier, should come first
        assert due[0].id == t1.id
        assert due[1].id == t2.id


# ── Persistence ────────────────────────────────────────────────────────────────

class TestPersistence:
    def test_data_survives_reconnect(self, tmp_path):
        db_path = str(tmp_path / "persistent.db")
        store1 = TriggerStore(db_path)
        t = store1.create("agent", "persistent-trigger", "webhook")
        trigger_id = t.id

        # Open a second connection to same DB
        store2 = TriggerStore(db_path)
        fetched = store2.get(trigger_id)
        assert fetched is not None
        assert fetched.name == "persistent-trigger"
