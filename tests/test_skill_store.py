"""Tests for skill/plugin registry."""

from __future__ import annotations

import os
import sqlite3
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from pinky_daemon.skill_store import SkillStore


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

    def test_origin_agent_round_trips_and_operator_update_preserves_owner(self, store):
        created = store.register(
            "owned-draft",
            description="created by a signed caller",
            origin_agent="writer",
            agent_originated=True,
        )
        assert created.origin_agent == "writer"
        assert created.to_dict()["origin_agent"] == "writer"

        updated = store.register(
            "owned-draft",
            description="updated by an operator",
        )
        assert updated.origin_agent == "writer"

    def test_origin_agent_migration_marks_legacy_rows_operator_owned(self, tmp_path):
        db_path = tmp_path / "legacy-skills.db"
        legacy = sqlite3.connect(db_path)
        legacy.execute(
            """CREATE TABLE skills (
                   name TEXT PRIMARY KEY,
                   description TEXT NOT NULL DEFAULT '',
                   skill_type TEXT NOT NULL DEFAULT 'custom',
                   version TEXT NOT NULL DEFAULT '0.1.0',
                   enabled INTEGER NOT NULL DEFAULT 1,
                   config TEXT NOT NULL DEFAULT '{}',
                   created_at REAL NOT NULL,
                   updated_at REAL NOT NULL
               )"""
        )
        legacy.execute(
            """INSERT INTO skills
               (name, description, skill_type, version, enabled, config, created_at, updated_at)
               VALUES ('legacy-skill', 'legacy row', 'custom', '0.1.0', 1, '{}', 1.0, 1.0)"""
        )
        legacy.commit()
        legacy.close()

        migrated = SkillStore(str(db_path))
        try:
            columns = {
                row[1]
                for row in migrated._db.execute("PRAGMA table_info(skills)").fetchall()
            }
            skill = migrated.get("legacy-skill")

            assert "origin_agent" in columns
            assert skill is not None
            assert skill.origin_agent == ""
        finally:
            migrated.close()

    def test_set_tool_patterns_invalidates_privileged_opt_in_on_change(self, store):
        mcp_server_config = {
            "command": "custom-server",
            "args": ["--marker", "preserved"],
        }
        original = store.register(
            "operator-tools",
            skill_type="mcp_tool",
            mcp_server_config=mcp_server_config,
            tool_patterns=["Bash"],
            self_assignable=True,
            privileged_tool_opt_in=True,
        )
        assert original.privileged_tool_opt_in is True
        assert original.self_assignable is True

        updated = store.set_tool_patterns("operator-tools", ["Bash", "Write"])

        assert updated is not None
        assert updated.tool_patterns == ["Bash", "Write"]
        assert updated.privileged_tool_opt_in is False
        assert updated.self_assignable is False
        assert updated.mcp_server_config == mcp_server_config

    def test_set_tool_patterns_same_set_is_noop(self, store):
        original = store.register(
            "operator-tools",
            tool_patterns=["Bash", "Write"],
            self_assignable=True,
            privileged_tool_opt_in=True,
        )

        unchanged = store.set_tool_patterns("operator-tools", ["Write", "Bash"])

        assert unchanged == original
        assert unchanged.updated_at == original.updated_at

    def test_set_tool_patterns_missing_returns_none(self, store):
        assert store.set_tool_patterns("missing", ["Bash"]) is None

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


class TestSkillStoreConcurrency:
    def test_point_read_hammer_uses_thread_local_connections(self, tmp_path):
        store = SkillStore(db_path=str(tmp_path / "skills.db"))
        worker_count = 12
        rounds = 25
        point_reads_per_round = 8
        start = threading.Barrier(worker_count)
        store.register(
            "shared-skill",
            description="Shared point-read seed",
            mcp_server_config={
                "command": "shared-command",
                "env": {"SEED": "shared"},
            },
            tool_patterns=["mcp__shared__*"],
            requires=["base-skill"],
        )

        def hammer(worker_index):
            point_reads = 0
            snapshots = []
            try:
                start.wait(timeout=10)
                connection = store._db
                connection_id = id(connection)
                assert connection.execute(
                    "PRAGMA journal_mode"
                ).fetchone()[0].lower() == "wal"
                assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
                for round_index in range(rounds):
                    marker = f"{worker_index}-{round_index}"
                    created = store.register(
                        f"hammer-{marker}",
                        description=f"Hammer skill {marker}",
                        config={"marker": marker},
                        mcp_server_config={
                            "command": "hammer",
                            "args": [marker],
                        },
                        tool_patterns=[f"mcp__hammer__{marker}"],
                    )
                    own = store.get(created.name)
                    assert own is not None
                    assert own.config == {"marker": marker}
                    assert own.mcp_server_config["args"] == [marker]
                    for _ in range(point_reads_per_round):
                        shared = store.get("shared-skill")
                        assert shared is not None
                        assert shared.mcp_server_config == {
                            "command": "shared-command",
                            "env": {"SEED": "shared"},
                        }
                        assert shared.tool_patterns == ["mcp__shared__*"]
                        assert shared.requires == ["base-skill"]
                        point_reads += 1
                    snapshots.append((created.to_dict(), own.to_dict()))
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
            assert len(store.list()) == worker_count * rounds + 1
        finally:
            store.close()


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


class TestAgentSkills:
    def test_enabled_assignment_refuses_globally_disabled_skill(self, store):
        store.register(
            "disabled-catalog-skill",
            enabled=False,
            tool_patterns=["Read"],
            self_assignable=True,
        )

        assert (
            store.assign_to_agent(
                "target",
                "disabled-catalog-skill",
                assigned_by="user",
                enabled=True,
            )
            is False
        )
        assert store.get_agent_skills("target", enabled_only=False) == []

        assert store.assign_to_agent(
            "target",
            "disabled-catalog-skill",
            assigned_by="user",
            enabled=False,
        )

    def test_shared_skill_optout_overrides_when_enabled_only(self, store):
        # The POST /agents/{name}/skills/{skill}/disable opt-out flow: a shared
        # skill is opted out via a disabled assignment row, which must override
        # the shared auto-apply even in the enabled_only mode used by
        # materialize_for_agent.
        store.register(
            "shared-skill",
            shared=True,
            enabled=True,
            mcp_server_config={"command": "run-shared"},
            directive="shared directive",
            tool_patterns=["mcp__shared-skill__*"],
        )
        assert any(
            s["name"] == "shared-skill"
            for s in store.get_agent_skills("bob", enabled_only=True)
        )

        store.assign_to_agent("bob", "shared-skill", assigned_by="user")
        store.set_agent_skill_enabled("bob", "shared-skill", False)

        assert all(
            s["name"] != "shared-skill"
            for s in store.get_agent_skills("bob", enabled_only=True)
        )
        mat = store.materialize_for_agent("bob")
        assert "shared-skill" not in mat["mcp_servers"]
        assert "shared directive" not in mat["directives"]
        assert "mcp__shared-skill__*" not in mat["tool_patterns"]

        # Other agents keep the shared skill.
        assert any(
            s["name"] == "shared-skill"
            for s in store.get_agent_skills("alice", enabled_only=True)
        )

    def test_optout_row_visible_when_not_enabled_only(self, store):
        store.register("shared-skill", shared=True, enabled=True)
        store.assign_to_agent("bob", "shared-skill")
        store.set_agent_skill_enabled("bob", "shared-skill", False)
        by_name = {s["name"]: s for s in store.get_agent_skills("bob", enabled_only=False)}
        assert by_name["shared-skill"]["effective_enabled"] is False
        assert by_name["shared-skill"]["agent_enabled"] is False

    def test_disabled_direct_assignment_excluded_when_enabled_only(self, store):
        store.register("plain")
        store.assign_to_agent("bob", "plain")
        store.set_agent_skill_enabled("bob", "plain", False)
        assert store.get_agent_skills("bob", enabled_only=True) == []

    def test_config_overrides_merge_into_mcp_config(self, store):
        store.register(
            "svc",
            mcp_server_config={"command": "run", "env": {"A": "1", "B": "2"}},
        )
        store.assign_to_agent(
            "bob", "svc",
            config_overrides={"env": {"B": "9", "C": "{agent_name}"}, "cwd": "/tmp"},
        )
        cfg = store.materialize_for_agent("bob")["mcp_servers"]["svc"]
        assert cfg["command"] == "run"
        assert cfg["env"] == {"A": "1", "B": "9", "C": "bob"}
        assert cfg["cwd"] == "/tmp"

    def test_no_overrides_leaves_mcp_config_untouched(self, store):
        store.register("svc", mcp_server_config={"command": "run-{agent_name}"})
        store.assign_to_agent("bob", "svc")
        cfg = store.materialize_for_agent("bob")["mcp_servers"]["svc"]
        assert cfg == {"command": "run-bob"}


class TestSkillAPI:
    def _make_client(self):
        from fastapi.testclient import TestClient

        from pinky_daemon.api import create_api

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
