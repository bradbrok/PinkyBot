"""Tests for pinky_daemon conversation store."""

from __future__ import annotations

import os
import sqlite3
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient

from pinky_daemon.conversation_store import ConversationStore, StoredMessage


class TestConversationStore:
    def _make_store(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        store = ConversationStore(db_path=path)
        return store, path

    def _cleanup(self, store, path):
        store.close()
        os.unlink(path)

    def test_append(self):
        store, path = self._make_store()
        msg = store.append("session-1", "user", "Hello")
        assert msg.id > 0
        assert msg.session_id == "session-1"
        assert msg.role == "user"
        assert msg.content == "Hello"
        assert msg.timestamp > 0
        self._cleanup(store, path)

    def test_append_with_metadata(self):
        store, path = self._make_store()
        msg = store.append(
            "s1", "user", "Hi",
            platform="telegram",
            chat_id="12345",
            metadata={"extra": "data"},
        )
        assert msg.platform == "telegram"
        assert msg.chat_id == "12345"
        assert msg.metadata == {"extra": "data"}
        self._cleanup(store, path)

    def test_get_history(self):
        store, path = self._make_store()
        store.append("s1", "user", "Hello")
        store.append("s1", "assistant", "Hi there!")
        store.append("s1", "user", "How are you?")

        history = store.get_history("s1")
        assert len(history) == 3
        assert history[0].role == "user"
        assert history[0].content == "Hello"
        assert history[2].content == "How are you?"
        self._cleanup(store, path)

    def test_get_history_limit(self):
        store, path = self._make_store()
        for i in range(10):
            store.append("s1", "user", f"Message {i}")

        history = store.get_history("s1", limit=3)
        assert len(history) == 3
        # Should be the 3 most recent
        assert history[2].content == "Message 9"
        self._cleanup(store, path)

    def test_get_history_empty(self):
        store, path = self._make_store()
        history = store.get_history("nonexistent")
        assert history == []
        self._cleanup(store, path)

    def test_get_history_session_isolation(self):
        store, path = self._make_store()
        store.append("s1", "user", "Session 1 msg")
        store.append("s2", "user", "Session 2 msg")

        h1 = store.get_history("s1")
        h2 = store.get_history("s2")
        assert len(h1) == 1
        assert len(h2) == 1
        assert h1[0].content == "Session 1 msg"
        assert h2[0].content == "Session 2 msg"
        self._cleanup(store, path)

    def test_search(self):
        store, path = self._make_store()
        store.append("s1", "user", "I want to order pizza")
        store.append("s1", "assistant", "What toppings would you like?")
        store.append("s2", "user", "Tell me about restaurants")

        results = store.search("pizza")
        assert len(results) == 1
        assert results[0].content == "I want to order pizza"
        self._cleanup(store, path)

    def test_search_multiple_results(self):
        store, path = self._make_store()
        store.append("s1", "user", "Deploy the application")
        store.append("s1", "assistant", "Deployment started")
        store.append("s2", "user", "Check deployment status")

        results = store.search("deploy*")
        assert len(results) >= 2
        self._cleanup(store, path)

    def test_search_with_session_filter(self):
        store, path = self._make_store()
        store.append("s1", "user", "Hello world")
        store.append("s2", "user", "Hello world")

        results = store.search("hello", session_id="s1")
        assert len(results) == 1
        assert results[0].session_id == "s1"
        self._cleanup(store, path)

    def test_search_with_platform_filter(self):
        store, path = self._make_store()
        store.append("s1", "user", "Hello", platform="telegram")
        store.append("s2", "user", "Hello", platform="discord")

        results = store.search("hello", platform="telegram")
        assert len(results) == 1
        assert results[0].platform == "telegram"
        self._cleanup(store, path)

    def test_search_no_results(self):
        store, path = self._make_store()
        store.append("s1", "user", "Hello")
        results = store.search("xyznonexistent")
        assert results == []
        self._cleanup(store, path)

    def test_search_apostrophe_query_does_not_crash(self):
        store, path = self._make_store()
        store.append("s1", "user", "I don't like mondays")
        results = store.search("don't")
        assert len(results) == 1
        assert results[0].content == "I don't like mondays"
        self._cleanup(store, path)

    def test_search_unbalanced_quote_returns_empty(self):
        store, path = self._make_store()
        store.append("s1", "user", "plain text body")
        results = store.search('something "unbalanced')
        assert results == []
        self._cleanup(store, path)

    def test_search_whitespace_query_returns_empty(self):
        store, path = self._make_store()
        store.append("s1", "user", "Hello")
        assert store.search("   ") == []
        self._cleanup(store, path)

    def test_list_conversations(self):
        store, path = self._make_store()
        store.append("s1", "user", "Hello")
        store.append("s1", "assistant", "Hi")
        store.append("s2", "user", "Hey")

        convos = store.list_conversations()
        assert len(convos) == 2
        # Most recent first
        s1 = next(c for c in convos if c.session_id == "s1")
        assert s1.message_count == 2
        self._cleanup(store, path)

    def test_list_conversations_platform_filter(self):
        store, path = self._make_store()
        store.append("s1", "user", "Hello", platform="telegram")
        store.append("s2", "user", "Hello", platform="discord")

        convos = store.list_conversations(platform="telegram")
        assert len(convos) == 1
        assert convos[0].session_id == "s1"
        self._cleanup(store, path)

    def test_count(self):
        store, path = self._make_store()
        assert store.count() == 0

        store.append("s1", "user", "Hello")
        store.append("s1", "assistant", "Hi")
        assert store.count() == 2
        assert store.count("s1") == 2
        assert store.count("s2") == 0
        self._cleanup(store, path)

    def test_stored_message_to_dict(self):
        msg = StoredMessage(
            id=1,
            session_id="s1",
            role="user",
            content="Hello",
            timestamp=1711584000.0,
            platform="telegram",
            chat_id="12345",
        )
        d = msg.to_dict()
        assert d["id"] == 1
        assert d["session_id"] == "s1"
        assert d["platform"] == "telegram"
        assert "metadata" not in d  # Not in to_dict

    def test_conversation_summary_to_dict(self):
        from pinky_daemon.conversation_store import ConversationSummary
        cs = ConversationSummary(
            session_id="s1",
            message_count=5,
            first_message_at=1711584000.0,
            last_message_at=1711587600.0,
            platform="telegram",
            chat_id="12345",
        )
        d = cs.to_dict()
        assert d["session_id"] == "s1"
        assert d["message_count"] == 5


class TestConversationStoreConcurrency:
    def test_point_read_hammer_uses_thread_local_connections(self, tmp_path):
        store = ConversationStore(db_path=str(tmp_path / "conversations.db"))
        worker_count = 12
        rounds = 25
        point_reads_per_round = 8
        start = threading.Barrier(worker_count)
        store.append(
            "shared-session",
            "user",
            "Shared point-read seed",
            metadata={"kind": "shared-seed"},
        )

        def hammer(worker_index):
            point_reads = 0
            snapshots = []
            try:
                start.wait(timeout=10)
                connection_id = id(store._conn)
                session_id = f"worker-{worker_index}"
                for round_index in range(rounds):
                    marker = f"{worker_index}-{round_index}"
                    created = store.append(
                        session_id,
                        "assistant",
                        f"Hammer message {marker}",
                        metadata={"marker": marker},
                    )
                    own = store.get_history(session_id, limit=1)
                    assert own[0].id == created.id
                    assert own[0].metadata == {"marker": marker}
                    for _ in range(point_reads_per_round):
                        shared = store.get_history("shared-session", limit=1)
                        assert shared[0].content == "Shared point-read seed"
                        assert shared[0].metadata == {"kind": "shared-seed"}
                        point_reads += 1
                    snapshots.append((created.to_dict(), own[0].to_dict()))
                return connection_id, snapshots, point_reads, None
            except Exception as exc:
                return None, snapshots, point_reads, exc
            finally:
                store.close()

        try:
            with ThreadPoolExecutor(max_workers=worker_count) as executor:
                results = list(executor.map(hammer, range(worker_count)))

            errors = [error for _, _, _, error in results if error is not None]
            database_errors = [
                error
                for error in errors
                if isinstance(error, sqlite3.DatabaseError)
                or "malformed" in str(error).lower()
            ]
            assert database_errors == []
            assert errors == []

            connection_ids = [connection_id for connection_id, _, _, _ in results]
            assert len(set(connection_ids)) == worker_count
            assert sum(point_reads for _, _, point_reads, _ in results) == (
                worker_count * rounds * point_reads_per_round
            )
            assert all(len(snapshots) == rounds for _, snapshots, _, _ in results)
            assert store.count() == worker_count * rounds + 1
            assert all(
                store.count(f"worker-{index}") == rounds
                for index in range(worker_count)
            )
        finally:
            store.close()


class TestConversationAPI:
    """Test conversation store API endpoints."""

    def _make_client(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        from pinky_daemon.api import create_api
        app = create_api(max_sessions=10, default_working_dir="/tmp", db_path=path)
        return TestClient(app), path

    def test_list_conversations_empty(self):
        client, path = self._make_client()
        resp = client.get("/conversations")
        assert resp.status_code == 200
        assert resp.json()["count"] == 0
        os.unlink(path)

    def test_get_conversation_empty(self):
        client, path = self._make_client()
        resp = client.get("/conversations/nonexistent")
        assert resp.status_code == 200
        assert resp.json()["count"] == 0
        os.unlink(path)

    def test_search_requires_query(self):
        client, path = self._make_client()
        resp = client.get("/conversations/search?q=")
        assert resp.status_code == 400
        os.unlink(path)

    def test_search_no_results(self):
        client, path = self._make_client()
        resp = client.get("/conversations/search?q=nonexistent")
        assert resp.status_code == 200
        assert resp.json()["count"] == 0
        os.unlink(path)
