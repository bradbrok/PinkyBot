"""Tests for pinky_daemon agent communications."""

from __future__ import annotations

import os
import tempfile

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

    def test_direct_appears_in_inbox(self):
        comms, path = self._make_comms()
        comms.send("alice", "bob", "Hey!")

        inbox = comms.get_inbox("bob")
        assert len(inbox) == 1
        assert inbox[0].content == "Hey!"
        assert inbox[0].from_session == "alice"
        assert inbox[0].read is False
        self._cleanup(comms, path)

    def test_direct_not_in_sender_inbox(self):
        comms, path = self._make_comms()
        comms.send("alice", "bob", "Hey!")

        inbox = comms.get_inbox("alice")
        assert len(inbox) == 0
        self._cleanup(comms, path)

    def test_mark_read(self):
        comms, path = self._make_comms()
        msg = comms.send("alice", "bob", "Hey!")

        assert comms.unread_count("bob") == 1
        comms.mark_read("bob", [msg.id])
        assert comms.unread_count("bob") == 0

        # Unread_only should now be empty
        inbox = comms.get_inbox("bob", unread_only=True)
        assert len(inbox) == 0

        # But all messages should still show
        inbox = comms.get_inbox("bob", unread_only=False)
        assert len(inbox) == 1
        self._cleanup(comms, path)

    def test_mark_all_read(self):
        comms, path = self._make_comms()
        comms.send("alice", "bob", "Msg 1")
        comms.send("alice", "bob", "Msg 2")
        comms.send("alice", "bob", "Msg 3")

        assert comms.unread_count("bob") == 3
        count = comms.mark_read("bob")
        assert count == 3
        assert comms.unread_count("bob") == 0
        self._cleanup(comms, path)

    def test_multiple_conversations(self):
        comms, path = self._make_comms()
        comms.send("alice", "bob", "Hi Bob")
        comms.send("charlie", "bob", "Hi Bob from Charlie")
        comms.send("alice", "charlie", "Hi Charlie")

        bob_inbox = comms.get_inbox("bob")
        assert len(bob_inbox) == 2

        charlie_inbox = comms.get_inbox("charlie")
        assert len(charlie_inbox) == 1
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

        # Should be in bob and charlie's inbox, not alice's
        assert len(comms.get_inbox("bob")) == 1
        assert len(comms.get_inbox("charlie")) == 1
        assert len(comms.get_inbox("alice")) == 0
        self._cleanup(comms, path)

    def test_broadcast_content(self):
        comms, path = self._make_comms()
        comms.broadcast(
            "alice", "Important update",
            active_sessions=["bob", "charlie"],
        )

        bob_msg = comms.get_inbox("bob")[0]
        assert bob_msg.content == "Important update"
        assert bob_msg.from_session == "alice"
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

        # bob and charlie get it, alice doesn't
        assert len(comms.get_inbox("bob")) == 1
        assert len(comms.get_inbox("charlie")) == 1
        assert len(comms.get_inbox("alice")) == 0
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
        assert data["count"] == 1
        assert data["unread"] == 1
        os.unlink(path)

    def test_mark_read(self):
        client, path = self._make_client()
        client.post("/sessions", json={"session_id": "alice"})
        client.post("/sessions", json={"session_id": "bob"})
        client.post("/sessions/alice/send", json={"to": "bob", "content": "Hey!"})

        resp = client.post("/sessions/bob/inbox/read")
        assert resp.status_code == 200
        assert resp.json()["marked"] == 1
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

        # Bob should have it
        inbox = client.get("/sessions/bob/inbox").json()
        assert inbox["count"] == 1
        os.unlink(path)

    def test_send_not_found_session(self):
        client, path = self._make_client()
        resp = client.post("/sessions/nope/send", json={
            "to": "bob", "content": "Hey",
        })
        assert resp.status_code == 404
        os.unlink(path)
