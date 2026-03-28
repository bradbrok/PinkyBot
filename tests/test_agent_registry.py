"""Tests for agent registry."""

from __future__ import annotations

import os
import tempfile

import pytest

from pinky_daemon.agent_registry import Agent, AgentDirective, AgentRegistry, AgentToken


@pytest.fixture
def registry():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    r = AgentRegistry(db_path=path)
    yield r
    r.close()
    os.unlink(path)


class TestAgentCRUD:
    def test_register(self, registry):
        agent = registry.register("oleg", display_name="Oleg", model="opus")
        assert agent.name == "oleg"
        assert agent.display_name == "Oleg"
        assert agent.model == "opus"
        assert agent.enabled is True
        assert agent.created_at > 0

    def test_register_with_full_config(self, registry):
        agent = registry.register(
            "leo",
            display_name="Leo",
            model="sonnet",
            soul="# Leo the Worker",
            system_prompt="You are a code worker.",
            working_dir="/workspace",
            permission_mode="auto",
            allowed_tools=["Read", "Glob", "Grep", "Edit"],
            max_turns=50,
            timeout=600.0,
            parent="oleg",
            groups=["butter-team"],
            max_sessions=3,
        )
        assert agent.model == "sonnet"
        assert agent.soul == "# Leo the Worker"
        assert agent.allowed_tools == ["Read", "Glob", "Grep", "Edit"]
        assert agent.parent == "oleg"
        assert agent.groups == ["butter-team"]
        assert agent.max_sessions == 3

    def test_register_update(self, registry):
        registry.register("oleg", model="sonnet")
        agent = registry.register("oleg", model="opus")
        assert agent.model == "opus"

    def test_get(self, registry):
        registry.register("test")
        agent = registry.get("test")
        assert agent is not None
        assert agent.name == "test"

    def test_get_missing(self, registry):
        assert registry.get("nope") is None

    def test_list(self, registry):
        registry.register("a")
        registry.register("b")
        registry.register("c")
        agents = registry.list()
        assert len(agents) == 3
        assert [a.name for a in agents] == ["a", "b", "c"]

    def test_list_by_parent(self, registry):
        registry.register("lead")
        registry.register("worker1", parent="lead")
        registry.register("worker2", parent="lead")
        registry.register("solo")
        children = registry.list(parent="lead")
        assert len(children) == 2

    def test_list_by_group(self, registry):
        registry.register("a", groups=["team-1"])
        registry.register("b", groups=["team-1", "team-2"])
        registry.register("c", groups=["team-2"])
        team1 = registry.list(group="team-1")
        assert len(team1) == 2

    def test_list_enabled_only(self, registry):
        registry.register("on", enabled=True)
        registry.register("off", enabled=False)
        active = registry.list(enabled_only=True)
        assert len(active) == 1

    def test_delete(self, registry):
        registry.register("doomed")
        assert registry.delete("doomed") is True
        assert registry.get("doomed") is None

    def test_delete_missing(self, registry):
        assert registry.delete("nope") is False

    def test_get_children(self, registry):
        registry.register("boss")
        registry.register("w1", parent="boss")
        registry.register("w2", parent="boss")
        children = registry.get_children("boss")
        assert len(children) == 2

    def test_hierarchy(self, registry):
        registry.register("boss")
        registry.register("lead", parent="boss")
        registry.register("worker", parent="lead")
        tree = registry.get_hierarchy("boss")
        assert tree["agent"]["name"] == "boss"
        assert len(tree["children"]) == 1
        assert tree["children"][0]["agent"]["name"] == "lead"
        assert len(tree["children"][0]["children"]) == 1

    def test_to_dict(self, registry):
        agent = registry.register("test", display_name="Test Agent", model="opus")
        d = agent.to_dict()
        assert d["name"] == "test"
        assert d["display_name"] == "Test Agent"
        assert d["model"] == "opus"
        assert d["enabled"] is True


class TestDirectives:
    def test_add_directive(self, registry):
        registry.register("oleg")
        d = registry.add_directive("oleg", "Always write tests")
        assert d.directive == "Always write tests"
        assert d.active is True
        assert d.id > 0

    def test_add_with_priority(self, registry):
        registry.register("oleg")
        registry.add_directive("oleg", "Low priority", priority=0)
        registry.add_directive("oleg", "High priority", priority=10)
        directives = registry.get_directives("oleg")
        assert directives[0].directive == "High priority"
        assert directives[1].directive == "Low priority"

    def test_get_directives(self, registry):
        registry.register("oleg")
        registry.add_directive("oleg", "Rule 1")
        registry.add_directive("oleg", "Rule 2")
        directives = registry.get_directives("oleg")
        assert len(directives) == 2

    def test_get_directives_active_only(self, registry):
        registry.register("oleg")
        d1 = registry.add_directive("oleg", "Active")
        d2 = registry.add_directive("oleg", "Inactive")
        registry.toggle_directive(d2.id, False)
        active = registry.get_directives("oleg", active_only=True)
        assert len(active) == 1
        all_d = registry.get_directives("oleg", active_only=False)
        assert len(all_d) == 2

    def test_remove_directive(self, registry):
        registry.register("oleg")
        d = registry.add_directive("oleg", "Temp rule")
        assert registry.remove_directive(d.id) is True
        assert len(registry.get_directives("oleg")) == 0

    def test_toggle_directive(self, registry):
        registry.register("oleg")
        d = registry.add_directive("oleg", "Toggle me")
        registry.toggle_directive(d.id, False)
        directives = registry.get_directives("oleg", active_only=False)
        assert directives[0].active is False

    def test_build_system_prompt(self, registry):
        registry.register("oleg", soul="# Oleg Soul", system_prompt="Be helpful")
        registry.add_directive("oleg", "Write tests for every PR", priority=10)
        registry.add_directive("oleg", "Use Python 3.11+", priority=5)
        prompt = registry.build_system_prompt("oleg")
        assert "# Oleg Soul" in prompt
        assert "Be helpful" in prompt
        assert "Write tests for every PR" in prompt
        assert "Use Python 3.11+" in prompt

    def test_build_system_prompt_missing_agent(self, registry):
        assert registry.build_system_prompt("nope") == ""

    def test_cascade_delete(self, registry):
        registry.register("temp")
        registry.add_directive("temp", "Will be deleted")
        registry.delete("temp")
        # Directives should be gone too
        assert len(registry.get_directives("temp")) == 0


class TestTokens:
    def test_set_token(self, registry):
        registry.register("oleg")
        token = registry.set_token("oleg", "telegram", "bot123:secret")
        assert token.agent_name == "oleg"
        assert token.platform == "telegram"
        assert token.token_set is True
        assert token.enabled is True

    def test_token_not_exposed(self, registry):
        registry.register("oleg")
        registry.set_token("oleg", "telegram", "super-secret")
        token = registry.get_token("oleg", "telegram")
        d = token.to_dict()
        assert "super-secret" not in str(d)
        assert d["token_set"] is True

    def test_get_raw_token(self, registry):
        registry.register("oleg")
        registry.set_token("oleg", "telegram", "bot123:raw")
        assert registry.get_raw_token("oleg", "telegram") == "bot123:raw"

    def test_get_raw_token_missing(self, registry):
        assert registry.get_raw_token("nope", "telegram") == ""

    def test_update_token(self, registry):
        registry.register("oleg")
        registry.set_token("oleg", "telegram", "old-token")
        registry.set_token("oleg", "telegram", "new-token")
        assert registry.get_raw_token("oleg", "telegram") == "new-token"

    def test_list_tokens(self, registry):
        registry.register("oleg")
        registry.set_token("oleg", "telegram", "t1")
        registry.set_token("oleg", "discord", "d1")
        tokens = registry.list_tokens("oleg")
        assert len(tokens) == 2
        platforms = {t.platform for t in tokens}
        assert "telegram" in platforms
        assert "discord" in platforms

    def test_remove_token(self, registry):
        registry.register("oleg")
        registry.set_token("oleg", "telegram", "t1")
        assert registry.remove_token("oleg", "telegram") is True
        assert registry.get_token("oleg", "telegram") is None

    def test_token_with_settings(self, registry):
        registry.register("oleg")
        registry.set_token("oleg", "telegram", "t1", settings={"allowed_chats": ["123"]})
        token = registry.get_token("oleg", "telegram")
        assert token.settings == {"allowed_chats": ["123"]}

    def test_cascade_delete_tokens(self, registry):
        registry.register("temp")
        registry.set_token("temp", "telegram", "t1")
        registry.delete("temp")
        assert registry.get_token("temp", "telegram") is None
