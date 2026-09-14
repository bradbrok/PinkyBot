"""Pure three-valued policy contract; context facts are supplied by the caller."""

from __future__ import annotations

import ast
import hashlib
import importlib
import json
from pathlib import Path

import pytest

PRINCIPALS = ("owner", "schedule", "approved_user", "group", "agent", "unknown")
RULE_CASES = [
    ("Read", {}, None, "allow", "allow"),
    ("Write", {"file_path": "/work/sample/.claude/settings.json"},
     "self.modify_guard", "deny", "deny"),
    ("mcp__pinky-self__restart_daemon", {}, "owner_only.daemon_control", "allow", "deny"),
    ("mcp__pinky-messaging__send", {"platform": "telegram", "chat_id": "stranger"},
     "outbound.third_party", "pause", "deny"),
    ("mcp__pinky-messaging__broadcast", {}, "outbound.broadcast", "pause", "deny"),
    ("mcp__pinky-self__propose_call", {}, "outbound.voice", "allow", "deny"),
    ("mcp__billing__payment", {}, "money.*", "pause", "deny"),
    ("mcp__pinky-self__publish_research", {}, "public.publish", "pause", "deny"),
    ("Bash", {"command": "rm -rf /outside"}, "shell.destructive", "pause", "deny"),
    ("Bash", {"command": "docker system prune"}, "host.container_ops", "pause", "deny"),
    ("Agent", {}, "agent.spawn_unbounded", "allow", "deny"),
    ("LocalComputation", {}, None, "allow", "allow"),
]


def _policy():
    try:
        return importlib.import_module("pinky_daemon.tool_policy")
    except ModuleNotFoundError as exc:
        if exc.name != "pinky_daemon.tool_policy":
            raise
        pytest.fail("missing pure tool-policy engine", pytrace=False)


def _context(api, tool="Bash", tool_input=None, **changes):
    fields = dict(
        agent_name="sample", isolated=False, principal_class="owner", transport="tmux",
        tool_name=tool, tool_input=tool_input or {}, agent_dir="/work/sample", home_dir="/home/test",
        tmp_roots=["/private/scratch"], public_remotes=["origin"],
        known_recipients=frozenset({"telegram:owner-chat", "slack:approved-chat"}),
        data_roots=["/repo/data"], repo_default_branches={"origin": "main"},
        is_worktree_checkout=True, cwd="/work/sample",
    )
    fields.update(changes)
    return api.PolicyContext(**fields)


@pytest.mark.parametrize("transport", ["tmux", "sdk"])
@pytest.mark.parametrize("principal", PRINCIPALS)
@pytest.mark.parametrize("tool,tool_input,rule,owner_decision,other_decision", RULE_CASES)
def test_rule_principal_transport_matrix(
    transport, principal, tool, tool_input, rule, owner_decision, other_decision
):
    api = _policy()
    record = api.evaluate(
        _context(api, tool, tool_input, principal_class=principal, transport=transport), now=100,
    ).to_record()
    expected = owner_decision if principal in {"owner", "schedule"} else other_decision
    assert record["evaluated_permission"] == expected
    evaluation = record["evaluation"]
    assert evaluation.get("rule_id") == rule
    assert evaluation["principal_class"] == ("group" if principal == "unknown" else principal)
    assert evaluation["input_sha256"] == api.canonical_input_sha256(tool_input)
    assert set(record) == {"evaluated_permission", "evaluation"}
    assert set(evaluation) == {
        "type", "reason_code", "principal_class", "input_sha256",
    } | ({"rule_id"} if rule else set())


@pytest.mark.parametrize("principal", ["owner", "schedule", "unknown", "", "unexpected"])
def test_isolated_or_unknown_principal_never_inherits_owner_authority(principal):
    api = _policy()
    ctx = _context(api, "Agent", isolated=True, principal_class=principal)
    result = api.evaluate(ctx, now=100).to_record()
    assert result["evaluated_permission"] == "deny"
    assert result["evaluation"]["principal_class"] == "group"


def test_input_hash_is_canonical_unicode_and_key_order_stable():
    api = _policy()
    left = {"z": [1, {"b": True, "a": "λ"}], "a": None}
    right = {"a": None, "z": [1, {"a": "λ", "b": True}]}
    expected = hashlib.sha256(json.dumps(
        left, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str,
    ).encode()).hexdigest()
    assert api.canonical_input_sha256(left) == api.canonical_input_sha256(right) == expected
    assert api.canonical_input_sha256({"path": Path("example")}) == (
        api.canonical_input_sha256({"path": "example"})
    )


@pytest.mark.parametrize("short_circuit", ["tamper", "unavailable"])
def test_short_circuits_precede_static_and_overrides(short_circuit):
    api = _policy()
    result = api.evaluate(
        _context(api, "Read"), now=100, **{short_circuit: True},
        overrides=[{"pattern": "Read", "decision": "allow"}],
    ).to_record()
    assert result["evaluated_permission"] == "deny"
    assert result["evaluation"]["type"] == short_circuit


def test_static_precedes_override_and_expired_override_is_ignored():
    api = _policy()
    assert api.evaluate(_context(api, "Read"), now=100, overrides=[
        {"pattern": "Read", "decision": "deny"},
    ]).to_record()["evaluated_permission"] == "allow"
    assert api.evaluate(_context(api, "Agent", principal_class="group"), now=100, overrides=[
        {"pattern": "Agent", "decision": "allow", "valid_until": 100},
    ]).to_record()["evaluated_permission"] == "deny"


@pytest.mark.parametrize("decisions,expected", [
    (["pause", "allow"], "allow"), (["allow", "deny", "pause"], "deny"),
])
def test_equal_specificity_ties_are_order_independent(decisions, expected):
    api = _policy()
    rows = [{"pattern": "Bash", "decision": decision} for decision in decisions]
    for overrides in (rows, list(reversed(rows))):
        result = api.evaluate(_context(api, tool_input={"command": "true"}),
                              now=100, overrides=overrides).to_record()
        assert result["evaluated_permission"] == expected
        assert result["evaluation"]["type"] == "override"


def test_specific_override_and_rule_scope():
    api = _policy()
    ctx = _context(api, tool_input={"command": "git push --force main"})
    result = api.evaluate(ctx, now=100, overrides=[
        {"pattern": "B*", "decision": "deny"},
        {"pattern": "Bash", "decision": "pause"},
        {"pattern": "Bash(git push)", "decision": "allow"},
    ]).to_record()
    assert result["evaluated_permission"] == "allow"
    scoped = api.evaluate(_context(api, "Agent", principal_class="group"), now=100, overrides=[
        {"pattern": "Agent", "rule_id": "public.publish", "decision": "allow"},
    ]).to_record()
    assert scoped["evaluated_permission"] == "deny"


@pytest.mark.parametrize("pattern,tool,inputs,expected", [
    ("Bash", "Bash", {}, True), ("Bash", "BashExtra", {}, False),
    ("mcp__billing__*", "mcp__billing__refund", {}, True),
    ("Bash(git log)", "Bash", {"command": "git log --oneline"}, True),
    ("Bash(git log)", "Bash", {"command": "git status"}, False),
    ("Bash(git log)", "Bash", None, False),
])
def test_tool_pattern_grammar(pattern, tool, inputs, expected):
    api = _policy()
    assert api.matches_tool_pattern(pattern, tool, inputs) is expected


@pytest.mark.parametrize("command,rule,expected", [
    ("rm -rf /work/sample/cache", "shell.destructive", False),
    ("rm -rf '/work/sample/quoted cache'", "shell.destructive", False),
    ("rm -rf /private/scratch/cache", "shell.destructive", False),
    ("rm -rf /work/sample/scratchpad/cache", "shell.destructive", False),
    ("rm -rf /work/sample-other", "shell.destructive", True),
    ("rm -rf /work/sample/../../outside", "shell.destructive", True),
    ("rm -rf /work/sample/cache /outside", "shell.destructive", True),
    ("rm -rf '/outside/quoted path'", "shell.destructive", True),
    ("rm -rf /outside\n", "shell.destructive", True),
    ("git push --force origin main", "shell.destructive", True),
    ("git push --force-with-lease origin main", "shell.destructive", True),
    ("git push --force origin topic", "shell.destructive", False),
    ("git push --force origin master", "shell.destructive", True),
    ("sqlite3 /repo/data/state.db 'select 1'", "self.modify_guard", True),
    ("sqlite3 '/repo/data/nested/a.db' 'select 1'", "self.modify_guard", True),
    ("sqlite3 /repo/data-other/state.db 'select 1'", "self.modify_guard", False),
    ("sqlite3 /elsewhere/state.db 'select 1'", "self.modify_guard", False),
    ("git push origin main", "public.publish", True),
    ("git push origin topic", "public.publish", False),
    ("npm publish", "public.publish", True),
    ("npm publisher", "public.publish", False),
    ("echo npm publish", "public.publish", False),
    ("kill -9 -1", "shell.destructive", True),
    ("kill -9 -1suffix", "shell.destructive", False),
    ("echo safe\nkill -9 -1", "shell.destructive", True),
    ("docker rm example", "host.container_ops", True),
    ("docker rms example", "host.container_ops", False),
    ("launchctl kickstart gui/501/pinky", "owner_only.daemon_control", True),
    ("git reset --hard", "shell.destructive", False),
])
def test_bash_classifier_boundaries(command, rule, expected):
    api = _policy()
    assert (rule in api.classify_bash(command, _context(api))) is expected


@pytest.mark.parametrize("worktree", [False, None])
@pytest.mark.parametrize("principal,decision", [("owner", "pause"), ("group", "deny")])
def test_unknown_or_non_worktree_reset_is_conservative(worktree, principal, decision):
    api = _policy()
    ctx = _context(api, tool_input={"command": "git reset --hard"},
                   is_worktree_checkout=worktree, principal_class=principal)
    assert api.evaluate(ctx, now=100).to_record()["evaluated_permission"] == decision


@pytest.mark.parametrize("tool", ["Write", "Edit", "NotebookEdit"])
@pytest.mark.parametrize("path", [
    "/work/sample/.claude/hook_tool_policy.py", "/work/sample/.mcp.json",
    "/home/test/.claude/settings.json", "/home/test/.claude/settings.local.json",
])
def test_self_modification_guards_file_tools(tool, path):
    api = _policy()
    ctx = _context(api, tool, {"file_path": path, "notebook_path": path})
    assert api.evaluate(ctx, now=100).to_record()["evaluation"]["rule_id"] == "self.modify_guard"


@pytest.mark.parametrize("platform,chat_id,expected", [
    ("telegram", "owner-chat", "allow"), ("slack", "approved-chat", "allow"),
    ("slack", "owner-chat", "pause"), ("ferry", "peer", "pause"),
    ("telegram", "unknown", "pause"),
])
def test_recipients_are_platform_scoped_and_unknown_is_third_party(platform, chat_id, expected):
    api = _policy()
    ctx = _context(api, "mcp__pinky-messaging__send", {"platform": platform, "chat_id": chat_id})
    assert api.evaluate(ctx, now=100).to_record()["evaluated_permission"] == expected


def test_static_names_are_registered_and_not_unbounded_wildcards():
    api = _policy()
    builtin = {"Read", "Glob", "Grep", "LS", "ToolSearch", "TodoWrite"}
    registered = set()
    for namespace in ("self", "memory"):
        source = Path(f"src/pinky_{namespace}/server.py").read_text()
        for node in ast.walk(ast.parse(source)):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if any(isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute)
                   and d.func.attr == "tool" for d in node.decorator_list):
                registered.add(f"mcp__pinky-{namespace}__{node.name}")
    assert isinstance(api.STATIC_ALLOW_TOOLS, frozenset)
    memory_names = {
        "recall", "introspect", "kg_query", "kg_connections", "kg_stats", "kg_timeline",
        "memory_query", "memory_links",
    }
    self_names = {
        "context_status", "who_am_i", "load_my_context", "agent_status", "check_my_health",
        "search_history", "list_my_schedules", "get_next_task", "get_presentation_template",
        "list_presentations", "get_owner_profile", "get_my_research_assignments",
        "list_research_topics", "get_research_detail", "list_agents", "list_my_skills",
        "list_available_skills", "list_triggers", "get_attribution", "get_agent_card",
        "list_voice_calls", "list_call_requests", "get_app_source", "list_apps",
    }
    expected = builtin | {f"mcp__pinky-memory__{name}" for name in memory_names} | {
        f"mcp__pinky-self__{name}" for name in self_names
    }
    assert api.STATIC_ALLOW_TOOLS == frozenset(expected)
    assert api.STATIC_ALLOW_TOOLS <= builtin | registered
    assert not any("*" in name for name in api.STATIC_ALLOW_TOOLS)
    assert not {"Write", "Edit", "NotebookEdit", "Bash", "Agent", "WebFetch", "WebSearch",
                "mcp__pinky-memory__reflect"} & (
        api.STATIC_ALLOW_TOOLS
    )


def test_static_set_excludes_write_and_outbound_families():
    api = _policy()
    prefixes = (
        "update_", "create_", "delete_", "set_", "register_", "deploy_", "install_", "add_",
        "remove_", "complete_", "claim_", "block_", "spawn_", "propose_", "publish_", "submit_",
        "kb_save", "kb_ingest", "kb_delete", "save_", "context_restart", "restart_", "discard_",
        "mesh_", "broadcast",
    )
    for name in api.STATIC_ALLOW_TOOLS:
        assert not name.startswith(("mcp__pinky-web__", "mcp__pinky-messaging__"))
        if name.startswith("mcp__pinky-self__"):
            assert not name.removeprefix("mcp__pinky-self__").startswith(prefixes)


@pytest.mark.parametrize("tool,inputs,rule", [
    ("mcp__billing__purchase", {}, "money.*"),
    ("mcp__billing__refund", {}, "money.*"),
    ("mcp__billing__invoice_send", {}, "money.*"),
    ("mcp__billing__charge", {}, "money.*"),
    ("mcp__pinky-self__update_app", {"status": "deployed"}, "public.publish"),
    ("mcp__pinky-self__create_presentation", {}, "public.publish"),
    ("mcp__pinky-self__spawn_clone", {}, "agent.spawn_unbounded"),
    ("Workflow", {}, "agent.spawn_unbounded"),
    ("mcp__pinky-messaging__thread", {"message_id": "unknown"}, "outbound.third_party"),
    ("mcp__pinky-self__kb_delete_document", {}, "owner_only.daemon_control"),
    ("Bash", {"command": "echo disabled > /work/sample/.claude/settings.json"}, "self.modify_guard"),
    ("Bash", {"command": "rm -rf /work/sample/.claude"}, "self.modify_guard"),
])
def test_rule_family_variants(tool, inputs, rule):
    api = _policy()
    record = api.evaluate(_context(api, tool, inputs, principal_class="group"), now=100).to_record()
    assert record["evaluated_permission"] == "deny"
    assert record["evaluation"]["rule_id"] == rule


def test_non_deployed_app_update_and_web_requests_default_allow():
    api = _policy()
    for tool, inputs in [("mcp__pinky-self__update_app", {"status": "draft"}),
                         ("WebFetch", {"url": "https://example.test"}), ("WebSearch", {})]:
        record = api.evaluate(_context(api, tool, inputs), now=100).to_record()
        assert record["evaluated_permission"] == "allow"
        assert record["evaluation"]["type"] == "default"


def test_public_remote_default_branch_is_not_assumed_main():
    api = _policy()
    ctx = _context(api, tool_input={"command": "git push origin stable"},
                   repo_default_branches={"origin": "stable"})
    assert api.evaluate(ctx, now=100).to_record()["evaluated_permission"] == "pause"


@pytest.mark.parametrize("principal,decision", [("owner", "pause"), ("group", "deny")])
def test_unknown_repository_facts_use_conservative_branch(principal, decision):
    api = _policy()
    ctx = _context(api, tool_input={"command": "git push origin main"},
                   repo_default_branches={}, principal_class=principal)
    assert api.evaluate(ctx, now=100).to_record()["evaluated_permission"] == decision


def test_engine_evaluation_does_not_read_files_or_external_state(monkeypatch):
    api = _policy()
    ctx = _context(api, tool_input={"command": "rm -rf /outside"})

    def unexpected(*args, **kwargs):
        raise AssertionError("pure policy evaluation attempted I/O")

    monkeypatch.setattr("builtins.open", unexpected)
    monkeypatch.setattr("pathlib.Path.open", unexpected)
    monkeypatch.setattr("pathlib.Path.resolve", unexpected)
    monkeypatch.setattr("socket.socket", unexpected)
    monkeypatch.setattr("sqlite3.connect", unexpected)
    monkeypatch.setattr("os.getenv", unexpected)
    assert api.evaluate(ctx, now=100).to_record()["evaluated_permission"] == "pause"
