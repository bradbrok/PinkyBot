"""Tests for pinky_daemon agent communications."""

from __future__ import annotations

import os
import sqlite3
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from pinky_daemon.agent_comms import AgentComms, AgentMessage


class TestAgentComms:
    def _make_comms(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        return AgentComms(db_path=path), path

    def _cleanup(self, comms, path):
        comms.close()
        os.unlink(path)

    # ── Direct Messages ──────────────────────────────────────

    def test_send_direct(self):
        comms, path = self._make_comms()
        msg = comms.send("alice", "bob", "Hello Bob!")
        assert msg.id > 0
        assert msg.from_session == "alice"
        assert msg.to_session == "bob"
        assert msg.content == "Hello Bob!"
        assert msg.message_type == "direct"
        self._cleanup(comms, path)

    def test_direct_is_audit_only_and_inbox_stays_empty(self):
        comms, path = self._make_comms()
        msg = comms.send("alice", "bob", "Hey!")

        assert comms.get_inbox("bob") == []
        assert comms.unread_count("bob") == 0
        unread_rows = comms._conn.execute(
            "SELECT COUNT(*) FROM inbox WHERE session_id = 'bob' AND read = 0"
        ).fetchone()[0]
        assert unread_rows == 0
        messages, total = comms.get_all_messages()
        assert total == 1
        assert messages[0].id == msg.id
        assert messages[0].content == "Hey!"
        self._cleanup(comms, path)

    def test_startup_retires_legacy_unread_rows(self):
        comms, path = self._make_comms()
        comms.send("alice", "bob", "Legacy queued message")
        comms._conn.execute("UPDATE inbox SET read = 0 WHERE session_id = 'bob'")
        comms._conn.commit()
        comms.close()

        reopened = AgentComms(db_path=path)
        unread_rows = reopened._conn.execute(
            "SELECT COUNT(*) FROM inbox WHERE session_id = 'bob' AND read = 0"
        ).fetchone()[0]
        assert unread_rows == 0
        assert reopened.get_inbox("bob") == []
        assert reopened.unread_count("bob") == 0
        self._cleanup(reopened, path)

    def test_direct_not_in_sender_inbox(self):
        comms, path = self._make_comms()
        comms.send("alice", "bob", "Hey!")

        inbox = comms.get_inbox("alice")
        assert len(inbox) == 0
        self._cleanup(comms, path)

    def test_mark_read(self):
        comms, path = self._make_comms()
        msg = comms.send("alice", "bob", "Hey!")

        assert comms.unread_count("bob") == 0
        assert comms.mark_read("bob", [msg.id]) == 0
        assert comms.unread_count("bob") == 0

        assert comms.get_inbox("bob", unread_only=True) == []
        assert comms.get_inbox("bob", unread_only=False) == []
        assert msg.read is True
        self._cleanup(comms, path)

    def test_mark_all_read(self):
        comms, path = self._make_comms()
        comms.send("alice", "bob", "Msg 1")
        comms.send("alice", "bob", "Msg 2")
        comms.send("alice", "bob", "Msg 3")

        assert comms.unread_count("bob") == 0
        count = comms.mark_read("bob")
        assert count == 0
        assert comms.unread_count("bob") == 0
        self._cleanup(comms, path)

    def test_multiple_conversations(self):
        comms, path = self._make_comms()
        comms.send("alice", "bob", "Hi Bob")
        comms.send("charlie", "bob", "Hi Bob from Charlie")
        comms.send("alice", "charlie", "Hi Charlie")

        assert comms.get_inbox("bob") == []
        assert comms.get_inbox("charlie") == []
        messages, total = comms.get_all_messages()
        assert total == 3
        assert {message.content for message in messages} == {
            "Hi Bob",
            "Hi Bob from Charlie",
            "Hi Charlie",
        }
        self._cleanup(comms, path)

    # ── Broadcast ────────────────────────────────────────────

    def test_broadcast(self):
        comms, path = self._make_comms()
        msg = comms.broadcast(
            "alice", "Everyone listen up!",
            active_sessions=["alice", "bob", "charlie"],
        )
        assert msg.message_type == "broadcast"
        assert msg.to_session == "*"

        assert comms.get_inbox("bob") == []
        assert comms.get_inbox("charlie") == []
        assert comms.get_inbox("alice") == []
        self._cleanup(comms, path)

    def test_broadcast_content(self):
        comms, path = self._make_comms()
        comms.broadcast(
            "alice", "Important update",
            active_sessions=["bob", "charlie"],
        )

        messages, total = comms.get_all_messages()
        assert total == 1
        assert messages[0].content == "Important update"
        assert messages[0].from_session == "alice"
        self._cleanup(comms, path)

    # ── Groups ───────────────────────────────────────────────

    def test_create_group(self):
        comms, path = self._make_comms()
        result = comms.create_group("team", ["alice", "bob", "charlie"])
        assert result["name"] == "team"
        assert len(result["members"]) == 3
        self._cleanup(comms, path)

    def test_group_message(self):
        comms, path = self._make_comms()
        comms.create_group("team", ["alice", "bob", "charlie"])

        msg = comms.send_group("alice", "team", "Team update!")
        assert msg.message_type == "group"
        assert msg.group == "team"

        assert comms.get_inbox("bob") == []
        assert comms.get_inbox("charlie") == []
        assert comms.get_inbox("alice") == []
        self._cleanup(comms, path)

    def test_join_group(self):
        comms, path = self._make_comms()
        comms.create_group("team", ["alice"])
        comms.join_group("team", "bob")

        members = comms.get_group_members("team")
        assert "alice" in members
        assert "bob" in members
        self._cleanup(comms, path)

    def test_leave_group(self):
        comms, path = self._make_comms()
        comms.create_group("team", ["alice", "bob"])
        comms.leave_group("team", "bob")

        members = comms.get_group_members("team")
        assert "alice" in members
        assert "bob" not in members
        self._cleanup(comms, path)

    def test_list_groups(self):
        comms, path = self._make_comms()
        comms.create_group("team-a", ["alice", "bob"])
        comms.create_group("team-b", ["charlie"])

        groups = comms.list_groups()
        assert len(groups) == 2
        names = {g["name"] for g in groups}
        assert "team-a" in names
        assert "team-b" in names
        self._cleanup(comms, path)

    def test_get_group_members_empty(self):
        comms, path = self._make_comms()
        members = comms.get_group_members("nonexistent")
        assert members == []
        self._cleanup(comms, path)

    # ── Message to_dict ──────────────────────────────────────

    def test_message_to_dict(self):
        msg = AgentMessage(
            id=1,
            from_session="alice",
            to_session="bob",
            content="Hello",
            timestamp=1711584000.0,
            message_type="direct",
        )
        d = msg.to_dict()
        assert d["from"] == "alice"
        assert d["to"] == "bob"
        assert d["type"] == "direct"
        assert d["read"] is False


class TestAgentCommsConcurrency:
    def test_point_read_hammer_uses_thread_local_connections(self, tmp_path):
        comms = AgentComms(db_path=str(tmp_path / "agent-comms.db"))
        worker_count = 12
        rounds = 25
        point_reads_per_round = 8
        start = threading.Barrier(worker_count)
        seed = comms.send(
            "seed-sender",
            "shared-recipient",
            "Shared point-read seed",
            metadata={"kind": "shared-seed"},
        )

        def hammer(worker_index):
            point_reads = 0
            snapshots = []
            try:
                start.wait(timeout=10)
                connection_id = id(comms._conn)
                sender = f"worker-{worker_index}"
                for round_index in range(rounds):
                    marker = f"{worker_index}-{round_index}"
                    created = comms.send(
                        sender,
                        "shared-recipient",
                        f"Hammer message {marker}",
                        metadata={"marker": marker},
                    )
                    own_thread = comms.get_thread(created.id)
                    assert own_thread[0].id == created.id
                    assert own_thread[0].metadata == {"marker": marker}
                    for _ in range(point_reads_per_round):
                        shared_thread = comms.get_thread(seed.id)
                        assert shared_thread[0].content == "Shared point-read seed"
                        assert shared_thread[0].metadata == {"kind": "shared-seed"}
                        point_reads += 1
                    snapshots.append(
                        (created.to_dict(), own_thread[0].to_dict())
                    )
                return connection_id, snapshots, point_reads, None
            except Exception as exc:
                return None, snapshots, point_reads, exc
            finally:
                comms.close()

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
            messages, total = comms.get_all_messages(
                limit=worker_count * rounds + 1
            )
            assert total == worker_count * rounds + 1
            assert len(messages) == total
        finally:
            comms.close()


class TestAgentCommsAPI:
    def _make_client(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        from pinky_daemon.api import create_api
        app = create_api(max_sessions=10, default_working_dir="/tmp", db_path=path)
        return TestClient(app), path

    def test_send_direct(self):
        client, path = self._make_client()
        # Create two sessions
        client.post("/sessions", json={"session_id": "alice"})
        client.post("/sessions", json={"session_id": "bob"})

        # Send from alice to bob
        resp = client.post("/sessions/alice/send", json={
            "to": "bob",
            "content": "Hey Bob!",
        })
        assert resp.status_code == 200
        assert resp.json()["from"] == "alice"
        assert resp.json()["to"] == "bob"
        os.unlink(path)

    def test_get_inbox(self):
        client, path = self._make_client()
        client.post("/sessions", json={"session_id": "alice"})
        client.post("/sessions", json={"session_id": "bob"})
        client.post("/sessions/alice/send", json={"to": "bob", "content": "Hey!"})

        resp = client.get("/sessions/bob/inbox")
        assert resp.status_code == 200
        data = resp.json()
        assert data == {
            "session_id": "bob",
            "messages": [],
            "count": 0,
            "unread": 0,
            "deprecated": True,
        }
        os.unlink(path)

    def test_mark_read(self):
        client, path = self._make_client()
        client.post("/sessions", json={"session_id": "alice"})
        client.post("/sessions", json={"session_id": "bob"})
        client.post("/sessions/alice/send", json={"to": "bob", "content": "Hey!"})

        resp = client.post("/sessions/bob/inbox/read")
        assert resp.status_code == 200
        assert resp.json() == {"marked": 0, "deprecated": True}
        os.unlink(path)

    def test_broadcast(self):
        client, path = self._make_client()
        client.post("/sessions", json={"session_id": "alice"})
        client.post("/sessions", json={"session_id": "bob"})
        client.post("/sessions", json={"session_id": "charlie"})

        resp = client.post("/sessions/alice/send", json={
            "to": "*",
            "content": "Broadcast!",
        })
        assert resp.status_code == 200
        assert resp.json()["type"] == "broadcast"
        os.unlink(path)

    def test_create_group(self):
        client, path = self._make_client()
        resp = client.post("/groups", json={
            "name": "team",
            "members": ["alice", "bob"],
        })
        assert resp.status_code == 200
        assert resp.json()["name"] == "team"
        os.unlink(path)

    def test_list_groups(self):
        client, path = self._make_client()
        client.post("/groups", json={"name": "team-a", "members": ["alice"]})
        client.post("/groups", json={"name": "team-b", "members": ["bob"]})

        resp = client.get("/groups")
        assert resp.status_code == 200
        assert len(resp.json()["groups"]) == 2
        os.unlink(path)

    def test_get_group(self):
        client, path = self._make_client()
        client.post("/groups", json={"name": "team", "members": ["alice", "bob"]})

        resp = client.get("/groups/team")
        assert resp.status_code == 200
        assert len(resp.json()["members"]) == 2
        os.unlink(path)

    def test_get_group_not_found(self):
        client, path = self._make_client()
        resp = client.get("/groups/nope")
        assert resp.status_code == 404
        os.unlink(path)

    def test_join_group(self):
        client, path = self._make_client()
        client.post("/groups", json={"name": "team", "members": ["alice"]})

        resp = client.post("/groups/team/join", json={"session_id": "bob"})
        assert resp.status_code == 200
        assert resp.json()["joined"] is True

        members = client.get("/groups/team").json()["members"]
        assert "bob" in members
        os.unlink(path)

    def test_group_message_via_api(self):
        client, path = self._make_client()
        client.post("/sessions", json={"session_id": "alice"})
        client.post("/sessions", json={"session_id": "bob"})
        client.post("/groups", json={"name": "team", "members": ["alice", "bob"]})

        resp = client.post("/sessions/alice/send", json={
            "to": "team",
            "content": "Team update!",
        })
        assert resp.status_code == 200
        assert resp.json()["type"] == "group"

        inbox = client.get("/sessions/bob/inbox").json()
        assert inbox["count"] == 0
        assert inbox["deprecated"] is True
        os.unlink(path)

    def test_send_not_found_session(self):
        client, path = self._make_client()
        resp = client.post("/sessions/nope/send", json={
            "to": "bob", "content": "Hey",
        })
        assert resp.status_code == 404
        os.unlink(path)

    def test_send_with_content_type(self):
        client, path = self._make_client()
        client.post("/sessions", json={"session_id": "alice"})
        client.post("/sessions", json={"session_id": "bob"})
        resp = client.post("/sessions/alice/send", json={
            "to": "bob",
            "content": "Do the thing",
            "content_type": "task_request",
            "priority": 2,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["content_type"] == "task_request"
        assert data["priority"] == 2

        inbox = client.get("/sessions/bob/inbox").json()
        assert inbox["messages"] == []
        assert inbox["deprecated"] is True
        os.unlink(path)

    def test_send_with_threading(self):
        client, path = self._make_client()
        client.post("/sessions", json={"session_id": "alice"})
        client.post("/sessions", json={"session_id": "bob"})

        # Send original message
        resp1 = client.post("/sessions/alice/send", json={
            "to": "bob", "content": "Original question",
        })
        msg_id = resp1.json()["id"]

        # Reply to it
        resp2 = client.post("/sessions/bob/send", json={
            "to": "alice", "content": "Here's the answer",
            "parent_message_id": msg_id,
        })
        assert resp2.status_code == 200
        reply = resp2.json()
        assert reply["parent_message_id"] == msg_id
        os.unlink(path)

    def test_get_thread(self):
        client, path = self._make_client()
        client.post("/sessions", json={"session_id": "alice"})
        client.post("/sessions", json={"session_id": "bob"})

        # Create a thread: original + reply
        resp1 = client.post("/sessions/alice/send", json={
            "to": "bob", "content": "Q1",
        })
        msg_id = resp1.json()["id"]

        client.post("/sessions/bob/send", json={
            "to": "alice", "content": "A1",
            "parent_message_id": msg_id,
        })

        # Get the thread
        resp = client.get(f"/sessions/alice/inbox/{msg_id}/thread")
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 2
        assert data["messages"][0]["content"] == "Q1"
        assert data["messages"][1]["content"] == "A1"
        os.unlink(path)


class TestAgentCommsNewFeatures:
    """Tests for new inter-agent comms features: structured types, threading, TTL, priority."""

    def _make_comms(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        return AgentComms(db_path=path), path

    def _cleanup(self, comms, path):
        comms.close()
        os.unlink(path)

    # ── Structured Types ──────────────────────────────────────

    def test_send_with_content_type(self):
        comms, path = self._make_comms()
        msg = comms.send("alice", "bob", "Do task X", content_type="task_request")
        assert msg.content_type == "task_request"

        assert comms.get_inbox("bob") == []
        messages, total = comms.get_all_messages()
        assert total == 1
        assert messages[0].content_type == "task_request"
        self._cleanup(comms, path)

    def test_default_content_type_is_text(self):
        comms, path = self._make_comms()
        msg = comms.send("alice", "bob", "Hello")
        assert msg.content_type == "text"
        self._cleanup(comms, path)

    def test_content_type_in_to_dict(self):
        msg = AgentMessage(
            id=1, from_session="a", to_session="b",
            content="test", timestamp=1.0,
            content_type="task_response",
        )
        d = msg.to_dict()
        assert d["content_type"] == "task_response"

    # ── Threading ─────────────────────────────────────────────

    def test_send_with_parent_message_id(self):
        comms, path = self._make_comms()
        original = comms.send("alice", "bob", "Question?")
        reply = comms.send("bob", "alice", "Answer!", parent_message_id=original.id)
        assert reply.parent_message_id == original.id
        assert comms.get_inbox("alice") == []
        self._cleanup(comms, path)

    def test_get_thread(self):
        comms, path = self._make_comms()
        root = comms.send("alice", "bob", "Root message")
        comms.send("bob", "alice", "Reply 1", parent_message_id=root.id)
        comms.send("alice", "bob", "Reply 2", parent_message_id=root.id)

        thread = comms.get_thread(root.id)
        assert len(thread) == 3
        assert thread[0].content == "Root message"
        assert thread[1].content == "Reply 1"
        assert thread[2].content == "Reply 2"
        self._cleanup(comms, path)

    def test_get_thread_from_reply(self):
        comms, path = self._make_comms()
        root = comms.send("alice", "bob", "Root")
        reply = comms.send("bob", "alice", "Reply", parent_message_id=root.id)

        # Asking for thread from the reply ID should still return full thread
        thread = comms.get_thread(reply.id)
        assert len(thread) == 2
        assert thread[0].id == root.id
        self._cleanup(comms, path)

    def test_get_thread_deep_nesting(self):
        """Recursive CTE should fetch replies-to-replies at any depth."""
        comms, path = self._make_comms()
        root = comms.send("alice", "bob", "Level 0")
        r1 = comms.send("bob", "alice", "Level 1", parent_message_id=root.id)
        r2 = comms.send("alice", "bob", "Level 2", parent_message_id=r1.id)
        comms.send("bob", "alice", "Level 3", parent_message_id=r2.id)

        thread = comms.get_thread(root.id)
        assert len(thread) == 4
        assert [m.content for m in thread] == ["Level 0", "Level 1", "Level 2", "Level 3"]

        # Also works when starting from deepest reply
        thread2 = comms.get_thread(r2.id)
        assert len(thread2) == 4
        self._cleanup(comms, path)

    def test_get_thread_session_filter(self):
        """Thread filtered by session_id only returns messages where session is a participant."""
        comms, path = self._make_comms()
        root = comms.send("alice", "bob", "A->B")
        comms.send("bob", "charlie", "B->C", parent_message_id=root.id)
        comms.send("charlie", "alice", "C->A", parent_message_id=root.id)

        # Alice should see A->B and C->A but not B->C
        thread = comms.get_thread(root.id, session_id="alice")
        assert len(thread) == 2
        contents = {m.content for m in thread}
        assert "A->B" in contents
        assert "C->A" in contents
        assert "B->C" not in contents
        self._cleanup(comms, path)

    def test_get_thread_has_no_unread_state(self):
        """Thread history is always read because delivery inboxes are deprecated."""
        comms, path = self._make_comms()
        root = comms.send("alice", "bob", "Question?")
        comms.send("bob", "alice", "Answer!", parent_message_id=root.id)

        thread = comms.get_thread(root.id, session_id="bob")
        assert len(thread) == 2
        root_msg = [m for m in thread if m.content == "Question?"][0]
        assert root_msg.read is True

        assert comms.mark_read("bob", [root.id]) == 0

        thread2 = comms.get_thread(root.id, session_id="bob")
        root_msg2 = [m for m in thread2 if m.content == "Question?"][0]
        assert root_msg2.read is True
        self._cleanup(comms, path)

    # ── Priority ──────────────────────────────────────────────

    def test_priority_is_preserved_in_audit_without_an_inbox(self):
        comms, path = self._make_comms()
        comms.send("alice", "bob", "Normal message", priority=0)
        comms.send("charlie", "bob", "Urgent!", priority=2)
        comms.send("dave", "bob", "High priority", priority=1)

        assert comms.get_inbox("bob") == []
        messages, total = comms.get_all_messages()
        assert total == 3
        assert {message.content: message.priority for message in messages} == {
            "Normal message": 0,
            "Urgent!": 2,
            "High priority": 1,
        }
        self._cleanup(comms, path)

    def test_default_priority_is_zero(self):
        comms, path = self._make_comms()
        msg = comms.send("alice", "bob", "Hello")
        assert msg.priority == 0
        self._cleanup(comms, path)

    # ── TTL / Expiry ──────────────────────────────────────────

    def test_message_with_ttl(self):
        comms, path = self._make_comms()
        msg = comms.send("alice", "bob", "Temporary", ttl_seconds=3600)
        assert comms.get_inbox("bob") == []
        row = comms._conn.execute(
            "SELECT read, expires_at FROM inbox WHERE message_id = ?", (msg.id,)
        ).fetchone()
        assert row["read"] == 1
        assert row["expires_at"] is not None
        self._cleanup(comms, path)

    def test_expired_message_not_in_inbox(self):
        comms, path = self._make_comms()
        # Send with a TTL of 1 second
        comms.send("alice", "bob", "Ephemeral", ttl_seconds=1)

        assert comms.get_inbox("bob") == []

        # Manually expire it by updating the DB
        import time
        comms._conn.execute(
            "UPDATE inbox SET expires_at = ? WHERE session_id = 'bob'",
            (time.time() - 10,),
        )
        comms._conn.commit()

        assert comms.get_inbox("bob") == []
        self._cleanup(comms, path)

    def test_cleanup_expired(self):
        comms, path = self._make_comms()
        import time
        comms.send("alice", "bob", "Expired msg", ttl_seconds=1)
        comms.send("alice", "bob", "Permanent msg")

        # Force-expire the first message
        comms._conn.execute(
            "UPDATE inbox SET expires_at = ? WHERE id = 1",
            (time.time() - 10,),
        )
        comms._conn.commit()

        count = comms.cleanup_expired()
        assert count == 1

        remaining = comms._conn.execute(
            """SELECT m.content FROM inbox i
               JOIN messages m ON m.id = i.message_id"""
        ).fetchall()
        assert [row["content"] for row in remaining] == ["Permanent msg"]
        self._cleanup(comms, path)

    def test_no_ttl_never_expires(self):
        comms, path = self._make_comms()
        comms.send("alice", "bob", "Forever message")

        # Cleanup should not remove messages without TTL
        count = comms.cleanup_expired()
        assert count == 0
        assert comms.get_inbox("bob") == []
        self._cleanup(comms, path)

    def test_broadcast_default_ttl(self):
        comms, path = self._make_comms()
        comms.broadcast("alice", "Broadcast!", active_sessions=["bob", "charlie"])

        # Broadcasts should have expires_at set (7-day default)
        row = comms._conn.execute(
            "SELECT read, expires_at FROM inbox WHERE session_id = 'bob'"
        ).fetchone()
        assert row["read"] == 1
        assert row["expires_at"] is not None
        import time
        # Should expire roughly 7 days from now
        assert row["expires_at"] > time.time() + 6 * 24 * 3600
        self._cleanup(comms, path)

    # -- Thread visibility for group/broadcast recipients --

    def test_get_thread_group_recipient_sees_messages(self):
        """A group message recipient must see the thread even though
        to_session stores the group name, not their session id."""
        comms, path = self._make_comms()
        comms.create_group("team", ["alice", "bob", "charlie"])
        root = comms.send_group("alice", "team", "Team update")
        comms.send_group("bob", "team", "Ack", parent_message_id=root.id)

        thread = comms.get_thread(root.id, session_id="charlie")
        contents = {m.content for m in thread}
        assert "Team update" in contents
        assert "Ack" in contents
        self._cleanup(comms, path)

    def test_get_thread_broadcast_recipient_sees_message(self):
        comms, path = self._make_comms()
        root = comms.broadcast("alice", "Hear ye", active_sessions=["bob", "charlie"])

        thread = comms.get_thread(root.id, session_id="bob")
        assert len(thread) == 1
        assert thread[0].content == "Hear ye"
        assert thread[0].read is True
        self._cleanup(comms, path)

    def test_get_thread_still_hides_unrelated_messages(self):
        comms, path = self._make_comms()
        root = comms.send("alice", "bob", "A->B")
        comms.send("bob", "charlie", "B->C", parent_message_id=root.id)

        thread = comms.get_thread(root.id, session_id="dave")
        assert thread == []
        self._cleanup(comms, path)

    # -- Parent validation / cycle safety --

    def test_send_rejects_unknown_parent(self):
        comms, path = self._make_comms()
        with pytest.raises(ValueError):
            comms.send("alice", "bob", "reply", parent_message_id=999)
        self._cleanup(comms, path)

    def test_get_thread_terminates_on_cyclic_parents(self):
        """Legacy rows with cyclic parent chains must not hang the CTE."""
        comms, path = self._make_comms()
        a = comms.send("alice", "bob", "A")
        b = comms.send("bob", "alice", "B", parent_message_id=a.id)
        comms._conn.execute(
            "UPDATE messages SET parent_message_id = ? WHERE id = ?", (b.id, a.id)
        )
        comms._conn.commit()

        thread = comms.get_thread(a.id)
        assert {m.content for m in thread} == {"A", "B"}

        thread = comms.get_thread(a.id, session_id="alice")
        assert {m.content for m in thread} == {"A", "B"}
        self._cleanup(comms, path)

    # -- File transfer hardening --

    def test_send_file_rejects_path_traversal(self):
        comms, path = self._make_comms()
        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as f:
            f.write(b"payload")
            src = f.name
        try:
            for bad in ("../../evil", "..", ".", "", "/tmp/abs"):
                with pytest.raises(ValueError):
                    comms.send_file("alice", bad, src)
        finally:
            os.unlink(src)
        self._cleanup(comms, path)

    def test_send_file_writes_under_transfers_root(self):
        comms, path = self._make_comms()
        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as f:
            f.write(b"payload")
            src = f.name
        try:
            msg = comms.send_file("alice", "bob", src)
            dest = msg.metadata["file_path"]
            root = os.path.realpath(os.path.join(os.path.dirname(path), "transfers"))
            assert os.path.realpath(dest).startswith(root + os.sep)
            assert os.path.isfile(dest)
            os.unlink(dest)
        finally:
            os.unlink(src)
        self._cleanup(comms, path)

    # -- Audit listing dedup --

    def test_get_all_messages_no_duplicates_for_multi_recipient(self):
        comms, path = self._make_comms()
        comms.create_group("team", ["alice", "bob", "charlie"])
        comms.send_group("alice", "team", "Team update")
        comms.send("alice", "bob", "Direct")

        messages, total = comms.get_all_messages()
        assert total == 2
        assert len(messages) == 2
        assert len({m.id for m in messages}) == 2

        # Pagination over deduped rows must not overlap
        page1, _ = comms.get_all_messages(limit=1, offset=0)
        page2, _ = comms.get_all_messages(limit=1, offset=1)
        assert page1[0].id != page2[0].id
        self._cleanup(comms, path)

    # -- mark_read empty list --

    def test_mark_read_empty_list_marks_nothing(self):
        comms, path = self._make_comms()
        comms.send("alice", "bob", "Msg 1")
        comms.send("alice", "bob", "Msg 2")

        assert comms.mark_read("bob", []) == 0
        assert comms.unread_count("bob") == 0
        self._cleanup(comms, path)

    # ── Inbox Fallback (API) ──────────────────────────────────

    def test_agent_message_auto_wake_or_live_delivery_only(self):
        """Inter-agent messages may auto-wake, but they are never queued."""
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        from pinky_daemon.api import create_api
        app = create_api(max_sessions=10, default_working_dir="/tmp", db_path=path)
        client = TestClient(app)

        # Register an agent
        client.post("/agents", json={"name": "target_agent", "model": "opus"})

        # Send message to agent that has no streaming session
        resp = client.post("/agents/target_agent/message", json={
            "from_agent": "sender",
            "message": "Hello offline agent",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["message_id"] > 0
        assert data["queued"] is False
        assert client.get("/sessions/target_agent/inbox").json()["messages"] == []
        os.unlink(path)

    # ── Agent Card (API) ──────────────────────────────────────

    def test_agent_card(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        from pinky_daemon.api import create_api
        app = create_api(max_sessions=10, default_working_dir="/tmp", db_path=path)
        client = TestClient(app)

        client.post("/agents", json={
            "name": "barsik", "model": "opus", "display_name": "Barsik",
        })

        resp = client.get("/agents/barsik/card")
        assert resp.status_code == 200
        card = resp.json()
        assert card["name"] == "barsik"
        assert card["display_name"] == "Barsik"
        assert card["model"] == "opus"
        assert card["status"] in ("offline", "unknown")  # no heartbeats = unknown
        assert "capabilities" in card
        assert "groups" in card
        os.unlink(path)

    def test_agent_card_not_found(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        from pinky_daemon.api import create_api
        app = create_api(max_sessions=10, default_working_dir="/tmp", db_path=path)
        client = TestClient(app)

        resp = client.get("/agents/nonexistent/card")
        assert resp.status_code == 404
        os.unlink(path)

    # ── Presence (API) ────────────────────────────────────────

    def test_agent_presence(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        from pinky_daemon.api import create_api
        app = create_api(max_sessions=10, default_working_dir="/tmp", db_path=path)
        client = TestClient(app)

        client.post("/agents", json={"name": "test_agent", "model": "opus"})
        resp = client.get("/agents/test_agent/presence")
        assert resp.status_code == 200
        data = resp.json()
        assert data["agent"] == "test_agent"
        assert data["status"] in ("unknown", "offline")
        assert data["streaming"] is False
        os.unlink(path)

    def test_all_agents_presence(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        from pinky_daemon.api import create_api
        app = create_api(max_sessions=10, default_working_dir="/tmp", db_path=path)
        client = TestClient(app)

        client.post("/agents", json={"name": "agent1", "model": "opus"})
        client.post("/agents", json={"name": "agent2", "model": "sonnet"})
        resp = client.get("/agents/presence")
        assert resp.status_code == 200
        agents = resp.json()["agents"]
        assert len(agents) >= 2
        names = {a["agent"] for a in agents}
        assert "agent1" in names
        assert "agent2" in names
        os.unlink(path)
