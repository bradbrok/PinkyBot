"""Tests for session persistence store."""

from __future__ import annotations

import os
import sqlite3
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from pinky_daemon.conversation_store import ConversationStore
from pinky_daemon.session_store import SessionEventStore, SessionRecord, SessionStore


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


class TestSessionStoreConcurrency:
    def test_point_read_hammer_uses_thread_local_connections(self, tmp_path):
        db_path = str(tmp_path / "sessions.db")
        store = SessionStore(db_path=db_path)
        event_store = SessionEventStore(db_path=db_path)
        worker_count = 12
        rounds = 25
        point_reads_per_round = 8
        start = threading.Barrier(worker_count)
        store.save(
            _make_record(
                id="shared-session",
                agent_name="shared-agent",
                allowed_tools=["Read", "Glob"],
                disallowed_tools=["Bash"],
            )
        )
        event_store.log(
            "shared-session",
            "shared-agent",
            "seed",
            {"kind": "shared-seed"},
        )

        def hammer(worker_index):
            point_reads = 0
            snapshots = []
            try:
                start.wait(timeout=10)
                session_connection_id = id(store._db)
                event_connection_id = id(event_store._db)
                session_id = f"worker-{worker_index}"
                agent_name = f"agent-{worker_index}"
                for round_index in range(rounds):
                    marker = f"{worker_index}-{round_index}"
                    store.save(
                        _make_record(
                            id=session_id,
                            agent_name=agent_name,
                            sdk_session_id=marker,
                            allowed_tools=["Read", marker],
                        )
                    )
                    event_store.log(
                        session_id,
                        agent_name,
                        "hammer",
                        {"marker": marker},
                    )
                    for _ in range(point_reads_per_round):
                        shared = store.get("shared-session")
                        assert shared is not None
                        assert shared.allowed_tools == ["Read", "Glob"]
                        assert shared.disallowed_tools == ["Bash"]
                        shared_events = event_store.get_for_session(
                            "shared-session",
                            limit=1,
                        )
                        assert shared_events[0]["metadata"] == {
                            "kind": "shared-seed"
                        }
                        point_reads += 1
                    own = store.get(session_id)
                    assert own is not None
                    assert own.sdk_session_id == marker
                    snapshots.append((own.id, own.allowed_tools, marker))
                return (
                    session_connection_id,
                    event_connection_id,
                    snapshots,
                    point_reads,
                    None,
                )
            except Exception as exc:
                return None, None, snapshots, point_reads, exc
            finally:
                event_store.close()
                store.close()

        try:
            with ThreadPoolExecutor(max_workers=worker_count) as executor:
                results = list(executor.map(hammer, range(worker_count)))

            errors = [error for *_, error in results if error is not None]
            database_errors = [
                error
                for error in errors
                if isinstance(error, sqlite3.DatabaseError)
                or "malformed" in str(error).lower()
            ]
            assert database_errors == []
            assert errors == []

            session_connection_ids = [result[0] for result in results]
            event_connection_ids = [result[1] for result in results]
            assert len(set(session_connection_ids)) == worker_count
            assert len(set(event_connection_ids)) == worker_count
            assert sum(result[3] for result in results) == (
                worker_count * rounds * point_reads_per_round
            )
            assert all(len(result[2]) == rounds for result in results)
            assert len(store.list_all()) == worker_count + 1
            assert sum(
                len(event_store.get_for_session(f"worker-{index}", limit=rounds))
                for index in range(worker_count)
            ) == worker_count * rounds
        finally:
            event_store.close()
            store.close()


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


class TestSessionHistoryPersistence:
    """Tests for conversation history restoration on session restore."""

    def _make_stores(self):
        """Create session store + conversation store with temp DBs."""
        fd1, session_path = tempfile.mkstemp(suffix="_sessions.db")
        os.close(fd1)
        fd2, convo_path = tempfile.mkstemp(suffix="_conversations.db")
        os.close(fd2)
        return (
            SessionStore(db_path=session_path),
            ConversationStore(db_path=convo_path),
            session_path,
            convo_path,
        )

    def _cleanup(self, ss, cs, sp, cp):
        ss.close()
        cs.close()
        os.unlink(sp)
        os.unlink(cp)

    def test_restore_loads_history(self):
        """Restored sessions should have message history from conversation store."""
        from pinky_daemon.sessions import SessionManager

        ss, cs, sp, cp = self._make_stores()

        # Create session and simulate messages via conversation store
        mgr1 = SessionManager(store=ss, conversation_store=cs)
        mgr1.create(session_id="chat-1", model="sonnet")

        cs.append("chat-1", "user", "Hello")
        cs.append("chat-1", "assistant", "Hi there!")
        cs.append("chat-1", "user", "How are you?")
        cs.append("chat-1", "assistant", "I'm great!")

        ss.close()

        # Simulate server restart — new store instances, new manager
        ss2 = SessionStore(db_path=sp)
        mgr2 = SessionManager(store=ss2, conversation_store=cs)

        session = mgr2.get("chat-1")
        assert session is not None
        assert session.message_count == 4
        assert session.history[0].role == "user"
        assert session.history[0].content == "Hello"
        assert session.history[3].content == "I'm great!"

        ss2.close()
        cs.close()
        os.unlink(sp)
        os.unlink(cp)

    def test_restore_context_pct_accurate(self):
        """Context used percentage should reflect restored history."""
        from pinky_daemon.sessions import SessionManager

        ss, cs, sp, cp = self._make_stores()

        mgr1 = SessionManager(store=ss, conversation_store=cs)
        mgr1.create(session_id="ctx-test", model="sonnet")

        # Add enough messages to have non-zero context
        cs.append("ctx-test", "user", "x" * 1000)
        cs.append("ctx-test", "assistant", "y" * 1000)

        ss.close()

        ss2 = SessionStore(db_path=sp)
        mgr2 = SessionManager(store=ss2, conversation_store=cs)

        session = mgr2.get("ctx-test")
        assert session is not None
        assert session.context_used_pct > 0
        assert session.message_count == 2

        ss2.close()
        cs.close()
        os.unlink(sp)
        os.unlink(cp)

    def test_restore_empty_conversation(self):
        """Sessions with no messages should restore with empty history."""
        from pinky_daemon.sessions import SessionManager

        ss, cs, sp, cp = self._make_stores()

        mgr1 = SessionManager(store=ss, conversation_store=cs)
        mgr1.create(session_id="empty-chat", model="sonnet")
        ss.close()

        ss2 = SessionStore(db_path=sp)
        mgr2 = SessionManager(store=ss2, conversation_store=cs)

        session = mgr2.get("empty-chat")
        assert session is not None
        assert session.message_count == 0
        assert session.history == []

        ss2.close()
        cs.close()
        os.unlink(sp)
        os.unlink(cp)

    def test_restore_without_conversation_store(self):
        """Sessions should still restore (without history) if no conversation store."""
        from pinky_daemon.sessions import SessionManager

        fd, sp = tempfile.mkstemp(suffix="_sessions.db")
        os.close(fd)

        ss = SessionStore(db_path=sp)
        mgr1 = SessionManager(store=ss)
        mgr1.create(session_id="no-convo", model="opus")
        ss.close()

        ss2 = SessionStore(db_path=sp)
        mgr2 = SessionManager(store=ss2)  # No conversation_store

        session = mgr2.get("no-convo")
        assert session is not None
        assert session.message_count == 0

        ss2.close()
        os.unlink(sp)

    def test_session_info_reflects_restored_history(self):
        """SessionInfo.message_count should reflect restored messages."""
        from pinky_daemon.sessions import SessionManager

        ss, cs, sp, cp = self._make_stores()

        mgr1 = SessionManager(store=ss, conversation_store=cs)
        mgr1.create(session_id="info-test", model="sonnet")

        cs.append("info-test", "user", "Message 1")
        cs.append("info-test", "assistant", "Reply 1")

        ss.close()

        ss2 = SessionStore(db_path=sp)
        mgr2 = SessionManager(store=ss2, conversation_store=cs)

        # Check via list() which uses session.info
        sessions = mgr2.list()
        assert len(sessions) == 1
        assert sessions[0].message_count == 2

        ss2.close()
        cs.close()
        os.unlink(sp)
        os.unlink(cp)


def test_is_purgeable_legacy_session():
    """Startup purge predicate (#667): unowned ghosts AND superseded sessions
    are purgeable; an agent-owned session without a streaming session is kept."""
    from pinky_daemon.sessions import is_purgeable_legacy_session

    streaming = {"barsik", "pushok"}
    # Unowned ghost (blank agent_name) — always purgeable, regardless of which
    # agents are streaming. This is the #667 case (2-month-dead pinky-<hex> row).
    assert is_purgeable_legacy_session("", streaming) is True
    assert is_purgeable_legacy_session("", set()) is True
    # Superseded — the owning agent now has a live streaming session.
    assert is_purgeable_legacy_session("barsik", streaming) is True
    # Owned by an agent that does NOT have a streaming session — keep it.
    assert is_purgeable_legacy_session("persik", streaming) is False
    assert is_purgeable_legacy_session("persik", set()) is False


class TestGuardrailPersistence:
    """disallowed_tools and provider overrides must survive a restart --
    a restored session silently losing its tool restrictions or reverting
    to the default Anthropic endpoint is config drift with no error."""

    def test_record_roundtrips_disallowed_tools_and_provider(self, store):
        store.save(_make_record(
            disallowed_tools=["Bash", "Write"],
            provider_url="http://localhost:11434",
            provider_key="sk-local",
        ))
        got = store.get("test-session")
        assert got.disallowed_tools == ["Bash", "Write"]
        assert got.provider_url == "http://localhost:11434"
        assert got.provider_key == "sk-local"

    def test_record_defaults_when_fields_omitted(self, store):
        store.save(_make_record())
        got = store.get("test-session")
        assert got.disallowed_tools == []
        assert got.provider_url == ""
        assert got.provider_key == ""

    def test_migration_adds_columns_to_old_database(self):
        """Opening a pre-existing DB without the new columns must migrate it
        in place and read old rows with safe defaults."""
        import sqlite3

        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            conn = sqlite3.connect(path)
            conn.execute("""
                CREATE TABLE sessions (
                    id TEXT PRIMARY KEY,
                    model TEXT NOT NULL DEFAULT '',
                    soul TEXT NOT NULL DEFAULT '',
                    working_dir TEXT NOT NULL DEFAULT '.',
                    allowed_tools TEXT NOT NULL DEFAULT '[]',
                    max_turns INTEGER NOT NULL DEFAULT 0,
                    timeout REAL NOT NULL DEFAULT 300.0,
                    system_prompt TEXT NOT NULL DEFAULT '',
                    restart_threshold_pct REAL NOT NULL DEFAULT 80.0,
                    auto_restart INTEGER NOT NULL DEFAULT 1,
                    permission_mode TEXT NOT NULL DEFAULT '',
                    state TEXT NOT NULL DEFAULT 'idle',
                    created_at REAL NOT NULL,
                    last_active REAL NOT NULL,
                    restart_count INTEGER NOT NULL DEFAULT 0,
                    sdk_session_id TEXT NOT NULL DEFAULT '',
                    session_type TEXT NOT NULL DEFAULT 'chat',
                    agent_name TEXT NOT NULL DEFAULT ''
                )
            """)
            conn.execute(
                "INSERT INTO sessions (id, created_at, last_active) VALUES (?, ?, ?)",
                ("legacy", 1000.0, 1000.0),
            )
            conn.commit()
            conn.close()

            s = SessionStore(db_path=path)
            got = s.get("legacy")
            assert got is not None
            assert got.disallowed_tools == []
            assert got.provider_url == ""
            assert got.provider_key == ""
            # New fields persist on the migrated DB too.
            s.save(_make_record(id="legacy", disallowed_tools=["Bash"]))
            assert s.get("legacy").disallowed_tools == ["Bash"]
            s.close()
        finally:
            os.unlink(path)

    def test_manager_restore_passes_guardrails_to_session(self):
        from pinky_daemon.sessions import SessionManager

        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            st1 = SessionStore(db_path=path)
            mgr1 = SessionManager(store=st1)
            mgr1.create(
                session_id="locked",
                model="sonnet",
                disallowed_tools=["Bash", "Write"],
                provider_url="http://localhost:11434",
                provider_key="sk-local",
            )
            st1.close()

            st2 = SessionStore(db_path=path)
            mgr2 = SessionManager(store=st2)
            session = mgr2.get("locked")
            assert session is not None
            assert session.disallowed_tools == ["Bash", "Write"], (
                "restored session silently lost its tool restrictions"
            )
            assert session._provider_url == "http://localhost:11434"
            assert session._provider_key == "sk-local"
            st2.close()
        finally:
            os.unlink(path)


class TestSendStateRecovery:
    """Session.send must not strand the session in 'running' when the runner
    raises or the caller is cancelled -- a stuck-running session is
    unevictable and rejects all future sends as busy."""

    @pytest.mark.asyncio
    async def test_send_resets_state_when_runner_cancelled(self):
        import asyncio

        from pinky_daemon.sessions import SessionManager, SessionState

        mgr = SessionManager()
        session = mgr.create(session_id="cancel-me")

        class _CancelledRunner:
            async def run(self, content, **kwargs):
                raise asyncio.CancelledError()

        session._runner = _CancelledRunner()
        session._runner_type = "sdk"

        with pytest.raises(asyncio.CancelledError):
            await session.send("hello")

        assert session.state == SessionState.error, (
            f"state must not stay 'running' after cancellation, got {session.state}"
        )

    @pytest.mark.asyncio
    async def test_send_resets_state_when_runner_raises(self):
        from pinky_daemon.sessions import SessionManager, SessionState

        mgr = SessionManager()
        session = mgr.create(session_id="boom")

        class _BoomRunner:
            async def run(self, content, **kwargs):
                raise RuntimeError("runner exploded")

        session._runner = _BoomRunner()
        session._runner_type = "sdk"

        with pytest.raises(RuntimeError, match="runner exploded"):
            await session.send("hello")

        assert session.state == SessionState.error
