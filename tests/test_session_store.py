"""Tests for session persistence store."""

from __future__ import annotations

import os
import tempfile

import pytest

from pinky_daemon.session_store import SessionRecord, SessionStore


@pytest.fixture
def store():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    s = SessionStore(db_path=path)
    yield s
    s.close()
    os.unlink(path)


def _make_record(**kwargs) -> SessionRecord:
    defaults = dict(
        id="test-session",
        model="opus",
        soul="# Test Soul",
        working_dir="/tmp",
        allowed_tools=["Read", "Glob"],
        max_turns=25,
        timeout=300.0,
        system_prompt="Be helpful",
        restart_threshold_pct=80.0,
        auto_restart=True,
        permission_mode="auto",
        state="idle",
        created_at=1000.0,
        last_active=1000.0,
        restart_count=0,
        sdk_session_id="",
    )
    defaults.update(kwargs)
    return SessionRecord(**defaults)


class TestSessionStore:
    def test_save_and_get(self, store):
        record = _make_record()
        store.save(record)
        got = store.get("test-session")
        assert got is not None
        assert got.id == "test-session"
        assert got.model == "opus"
        assert got.soul == "# Test Soul"
        assert got.allowed_tools == ["Read", "Glob"]
        assert got.permission_mode == "auto"

    def test_get_missing(self, store):
        assert store.get("nope") is None

    def test_save_update(self, store):
        store.save(_make_record(model="sonnet"))
        store.save(_make_record(model="opus"))
        got = store.get("test-session")
        assert got.model == "opus"

    def test_list_active(self, store):
        store.save(_make_record(id="a", state="idle"))
        store.save(_make_record(id="b", state="running"))
        store.save(_make_record(id="c", state="closed"))
        active = store.list_active()
        assert len(active) == 2
        ids = {r.id for r in active}
        assert "a" in ids
        assert "b" in ids
        assert "c" not in ids

    def test_list_all(self, store):
        store.save(_make_record(id="a", state="idle"))
        store.save(_make_record(id="b", state="closed"))
        assert len(store.list_all()) == 2

    def test_update_state(self, store):
        store.save(_make_record())
        store.update_state("test-session", "running")
        got = store.get("test-session")
        assert got.state == "running"

    def test_update_activity(self, store):
        store.save(_make_record(last_active=1000.0))
        store.update_activity("test-session")
        got = store.get("test-session")
        assert got.last_active > 1000.0

    def test_update_sdk_session_id(self, store):
        store.save(_make_record())
        store.update_sdk_session_id("test-session", "real-sdk-id-123")
        got = store.get("test-session")
        assert got.sdk_session_id == "real-sdk-id-123"

    def test_update_restart_count(self, store):
        store.save(_make_record())
        store.update_restart_count("test-session", 3)
        got = store.get("test-session")
        assert got.restart_count == 3

    def test_delete_soft(self, store):
        store.save(_make_record())
        assert store.delete("test-session") is True
        got = store.get("test-session")
        assert got.state == "closed"

    def test_delete_missing(self, store):
        assert store.delete("nope") is False

    def test_hard_delete(self, store):
        store.save(_make_record())
        assert store.hard_delete("test-session") is True
        assert store.get("test-session") is None

    def test_preserves_allowed_tools(self, store):
        tools = ["mcp__memory__*", "mcp__outreach__*", "Read"]
        store.save(_make_record(allowed_tools=tools))
        got = store.get("test-session")
        assert got.allowed_tools == tools

    def test_preserves_bool_fields(self, store):
        store.save(_make_record(auto_restart=False))
        got = store.get("test-session")
        assert got.auto_restart is False


class TestSessionManagerPersistence:
    def test_create_persists(self):
        from pinky_daemon.sessions import SessionManager

        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        st = SessionStore(db_path=path)

        mgr = SessionManager(store=st)
        mgr.create(session_id="persist-me", model="opus")

        # Check it's in the store
        got = st.get("persist-me")
        assert got is not None
        assert got.model == "opus"

        st.close()
        os.unlink(path)

    def test_restore_on_init(self):
        from pinky_daemon.sessions import SessionManager

        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)

        # Create session in first manager
        st1 = SessionStore(db_path=path)
        mgr1 = SessionManager(store=st1)
        mgr1.create(session_id="survivor", model="sonnet")
        assert mgr1.count == 1
        st1.close()

        # New manager should restore it
        st2 = SessionStore(db_path=path)
        mgr2 = SessionManager(store=st2)
        assert mgr2.count == 1
        session = mgr2.get("survivor")
        assert session is not None
        assert session.model == "sonnet"

        st2.close()
        os.unlink(path)

    def test_delete_persists(self):
        from pinky_daemon.sessions import SessionManager

        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        st = SessionStore(db_path=path)

        mgr = SessionManager(store=st)
        mgr.create(session_id="doomed")
        mgr.delete("doomed")

        got = st.get("doomed")
        assert got.state == "closed"

        st.close()
        os.unlink(path)

    def test_closed_not_restored(self):
        from pinky_daemon.sessions import SessionManager

        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)

        st1 = SessionStore(db_path=path)
        mgr1 = SessionManager(store=st1)
        mgr1.create(session_id="alive")
        mgr1.create(session_id="dead")
        mgr1.delete("dead")
        st1.close()

        st2 = SessionStore(db_path=path)
        mgr2 = SessionManager(store=st2)
        assert mgr2.count == 1
        assert mgr2.get("alive") is not None
        assert mgr2.get("dead") is None

        st2.close()
        os.unlink(path)

    def test_no_store_still_works(self):
        from pinky_daemon.sessions import SessionManager

        mgr = SessionManager()
        mgr.create(session_id="ephemeral")
        assert mgr.count == 1
        mgr.delete("ephemeral")
        assert mgr.count == 0
