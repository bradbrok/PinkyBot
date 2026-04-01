"""Tests for skill/plugin registry."""

from __future__ import annotations

import os
import tempfile

import pytest

from pinky_daemon.skill_store import Skill, SkillStore


@pytest.fixture
def store():
    """Create a temporary skill store."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    s = SkillStore(db_path=path)
    yield s
    s.close()
    os.unlink(path)


class TestSkillStore:
    def test_register(self, store):
        skill = store.register("memory", description="Memory MCP tools")
        assert skill.name == "memory"
        assert skill.description == "Memory MCP tools"
        assert skill.enabled is True
        assert skill.created_at > 0

    def test_register_with_config(self, store):
        skill = store.register(
            "outreach",
            description="Outreach tools",
            skill_type="mcp_tool",
            config={"platforms": ["telegram", "discord"]},
        )
        assert skill.config == {"platforms": ["telegram", "discord"]}
        assert skill.skill_type == "mcp_tool"

    def test_register_update_existing(self, store):
        store.register("memory", description="v1")
        skill = store.register("memory", description="v2", version="0.2.0")
        assert skill.description == "v2"
        assert skill.version == "0.2.0"

    def test_get(self, store):
        store.register("test-skill")
        skill = store.get("test-skill")
        assert skill is not None
        assert skill.name == "test-skill"

    def test_get_missing(self, store):
        assert store.get("nope") is None

    def test_list_empty(self, store):
        assert store.list() == []

    def test_list(self, store):
        store.register("a")
        store.register("b")
        store.register("c")
        result = store.list()
        assert len(result) == 3
        assert [s.name for s in result] == ["a", "b", "c"]

    def test_list_by_type(self, store):
        store.register("mem", skill_type="mcp_tool")
        store.register("custom", skill_type="custom")
        result = store.list(skill_type="mcp_tool")
        assert len(result) == 1
        assert result[0].name == "mem"

    def test_list_enabled_only(self, store):
        store.register("on", enabled=True)
        store.register("off", enabled=False)
        result = store.list(enabled_only=True)
        assert len(result) == 1
        assert result[0].name == "on"

    def test_delete(self, store):
        store.register("doomed")
        assert store.delete("doomed") is True
        assert store.get("doomed") is None

    def test_delete_missing(self, store):
        assert store.delete("nope") is False

    def test_enable_disable(self, store):
        store.register("toggle", enabled=True)
        assert store.disable("toggle") is True
        assert store.get("toggle").enabled is False
        assert store.enable("toggle") is True
        assert store.get("toggle").enabled is True

    def test_enable_missing(self, store):
        assert store.enable("nope") is False

    def test_to_dict(self, store):
        skill = store.register("test", description="desc", skill_type="builtin")
        d = skill.to_dict()
        assert d["name"] == "test"
        assert d["description"] == "desc"
        assert d["skill_type"] == "builtin"
        assert d["enabled"] is True


class TestSessionSkills:
    def test_enable_for_session(self, store):
        store.register("memory")
        assert store.enable_for_session("sess-1", "memory") is True

    def test_disable_for_session(self, store):
        store.register("memory")
        assert store.disable_for_session("sess-1", "memory") is True

    def test_session_skill_missing_skill(self, store):
        assert store.enable_for_session("sess-1", "nope") is False

    def test_get_session_skills(self, store):
        store.register("a", enabled=True)
        store.register("b", enabled=True)
        store.register("c", enabled=False)

        # Override: disable "a" for this session, enable "c"
        store.disable_for_session("sess-1", "a")
        store.enable_for_session("sess-1", "c")

        result = store.get_session_skills("sess-1")
        assert len(result) == 3

        by_name = {s["name"]: s for s in result}
        assert by_name["a"]["global_enabled"] is True
        assert by_name["a"]["session_override"] is False
        assert by_name["a"]["effective_enabled"] is False

        assert by_name["b"]["session_override"] is None
        assert by_name["b"]["effective_enabled"] is True

        assert by_name["c"]["global_enabled"] is False
        assert by_name["c"]["session_override"] is True
        assert by_name["c"]["effective_enabled"] is True

    def test_clear_session_override(self, store):
        store.register("memory")
        store.disable_for_session("sess-1", "memory")
        assert store.clear_session_override("sess-1", "memory") is True

        result = store.get_session_skills("sess-1")
        by_name = {s["name"]: s for s in result}
        assert by_name["memory"]["session_override"] is None

    def test_clear_nonexistent_override(self, store):
        store.register("memory")
        assert store.clear_session_override("sess-1", "memory") is False


class TestSkillAPI:
    def _make_client(self):
        from pinky_daemon.api import create_api
        from fastapi.testclient import TestClient

        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        app = create_api(max_sessions=10, default_working_dir="/tmp", db_path=path)
        return TestClient(app)

    def test_register_skill(self):
        client = self._make_client()
        resp = client.post("/skills", json={"name": "memory", "description": "Memory tools"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "memory"
        assert data["description"] == "Memory tools"

    def test_list_skills(self):
        client = self._make_client()
        # Core skills are seeded on startup, so count those first
        base_resp = client.get("/skills")
        base_count = base_resp.json()["count"]
        client.post("/skills", json={"name": "test-a"})
        client.post("/skills", json={"name": "test-b"})
        resp = client.get("/skills")
        assert resp.status_code == 200
        assert resp.json()["count"] == base_count + 2

    def test_get_skill(self):
        client = self._make_client()
        client.post("/skills", json={"name": "test"})
        resp = client.get("/skills/test")
        assert resp.status_code == 200
        assert resp.json()["name"] == "test"

    def test_get_skill_not_found(self):
        client = self._make_client()
        resp = client.get("/skills/nope")
        assert resp.status_code == 404

    def test_update_skill(self):
        client = self._make_client()
        client.post("/skills", json={"name": "test", "description": "v1"})
        resp = client.put("/skills/test", json={"description": "v2"})
        assert resp.status_code == 200
        assert resp.json()["description"] == "v2"

    def test_update_skill_not_found(self):
        client = self._make_client()
        resp = client.put("/skills/nope", json={"description": "x"})
        assert resp.status_code == 404

    def test_delete_skill(self):
        client = self._make_client()
        client.post("/skills", json={"name": "test"})
        resp = client.delete("/skills/test")
        assert resp.status_code == 200
        assert resp.json()["deleted"] is True

    def test_enable_disable_skill(self):
        client = self._make_client()
        client.post("/skills", json={"name": "test"})
        resp = client.post("/skills/test/disable")
        assert resp.status_code == 200
        assert resp.json()["disabled"] is True

        resp = client.post("/skills/test/enable")
        assert resp.status_code == 200
        assert resp.json()["enabled"] is True

    def test_session_skills(self):
        client = self._make_client()
        client.post("/sessions", json={"session_id": "s1"})
        client.post("/skills", json={"name": "test-memory"})

        resp = client.get("/sessions/s1/skills")
        assert resp.status_code == 200
        # Count includes seeded core skills + our new one
        assert resp.json()["count"] >= 1

    def test_session_skill_override(self):
        client = self._make_client()
        client.post("/sessions", json={"session_id": "s1"})
        client.post("/skills", json={"name": "test-override-skill"})

        resp = client.put("/sessions/s1/skills/test-override-skill", json={"enabled": False})
        assert resp.status_code == 200

        resp = client.get("/sessions/s1/skills")
        skills = resp.json()["skills"]
        by_name = {s["name"]: s for s in skills}
        assert by_name["test-override-skill"]["effective_enabled"] is False

    def test_clear_session_override(self):
        client = self._make_client()
        client.post("/sessions", json={"session_id": "s1"})
        client.post("/skills", json={"name": "test-clear-skill"})
        client.put("/sessions/s1/skills/test-clear-skill", json={"enabled": False})

        resp = client.delete("/sessions/s1/skills/test-clear-skill")
        assert resp.status_code == 200
        assert resp.json()["override_cleared"] is True

    def test_session_skills_session_not_found(self):
        client = self._make_client()
        resp = client.get("/sessions/nope/skills")
        assert resp.status_code == 404
