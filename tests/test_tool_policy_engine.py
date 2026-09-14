"""Pure three-valued policy contract; context facts are supplied by the caller."""

from __future__ import annotations

import ast
import hashlib
import importlib
import json
import os
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
        known_recipients=frozenset({"telegram:owner-chat", "approved-chat"}),
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
    assert evaluation["type"] == ("static" if tool == "Read" else "rule" if rule else "default")
    assert evaluation["reason_code"] in api.REASON_CODES
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
    ("Bash(git log)", "Bash", {"command": "git log --oneline\n"}, True),
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
    ("rm -rf /work/sample/cache\n", "shell.destructive", False),
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
    ("telegram", "approved-chat", "allow"),
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
        source = _package_source(f"pinky_{namespace}", "server.py").read_text()
        for node in ast.walk(ast.parse(source)):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if any(isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute)
                   and d.func.attr == "tool" for d in node.decorator_list):
                registered.add(f"mcp__pinky-{namespace}__{node.name}")
    assert isinstance(api.STATIC_ALLOW_TOOLS, frozenset)
    expected = _expected_static_tools()
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
        if name.startswith("mcp__"):
            assert not name.split("__", 2)[2].startswith(prefixes)


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

    with monkeypatch.context() as patch:
        patch.setattr("builtins.open", unexpected)
        patch.setattr("pathlib.Path.open", unexpected)
        patch.setattr("pathlib.Path.resolve", unexpected)
        patch.setattr("socket.socket", unexpected)
        patch.setattr("sqlite3.connect", unexpected)
        patch.setattr("os.getenv", unexpected)
        patch.setattr(os.environ, "get", unexpected)
        patch.setattr("os.stat", unexpected)
        result = api.evaluate(ctx, now=100).to_record()
    assert result["evaluated_permission"] == "pause"


def test_reason_code_vocabulary_covers_all_default_rules():
    api = _policy()
    assert isinstance(api.REASON_CODES, frozenset)
    assert api.REASON_CODES
    for rule in api.DEFAULT_RULES:
        assert rule["reason_code"] in api.REASON_CODES
    for kind in ("static", "rule", "override", "default", "tamper", "unavailable"):
        tool = "Read" if kind == "static" else "Agent" if kind == "rule" else "LocalComputation"
        options = {kind: True} if kind in {"tamper", "unavailable"} else {}
        if kind == "override":
            options["overrides"] = [{"pattern": tool, "decision": "deny"}]
        record = api.evaluate(_context(api, tool), now=100, **options).to_record()
        assert record["evaluation"]["type"] == kind
        assert record["evaluation"]["reason_code"] in api.REASON_CODES


def _package_source(package, filename):
    spec = importlib.util.find_spec(package)
    assert spec is not None and spec.origin, f"cannot resolve installed package {package}"
    daemon = importlib.util.find_spec("pinky_daemon")
    assert daemon is not None and daemon.origin
    assert Path(spec.origin).resolve().parent.parent == Path(daemon.origin).resolve().parent.parent
    return Path(spec.origin).parent / filename


STATIC_INVARIANT = (
    "A static tool performs no writes beyond its own bookkeeping and sends no network "
    "request to a destination chosen at call time."
)
STATIC_EXCEPTIONS = {
    "TodoWrite": "session-local",
    "mcp__pinky-memory__recall": (
        "access bookkeeping write (accessed_at/access_count/weight) + query embedding sent "
        "to the configured embeddings provider (fixed destination)"
    ),
}
_WRITE_PREFIXES = (
    "write", "update_", "create_", "delete_", "set_", "register_", "deploy_", "install_",
    "add_", "remove_", "complete_", "claim_", "block_", "spawn_", "propose_", "publish_",
    "submit_", "kb_save", "kb_ingest", "kb_delete", "save_", "context_restart", "restart_",
    "discard_", "mesh_", "broadcast", "reflect", "unlink", "mkdir", "rmdir", "rename",
    "replace_file", "chmod", "chown", "touch", "truncate", "send", "commit", "executescript",
)


def _registered_handlers(package):
    source = _package_source(package, "server.py")
    tree = ast.parse(source.read_text())
    handlers = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and any(
            isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute) and d.func.attr == "tool"
            for d in node.decorator_list
        ):
            handlers[node.name] = node
    return tree, handlers


def _handler_effects(handler, tree, store_tree=None):
    """Follow local helpers and store methods; reject write and external-client sinks.

    Literal GETs through the daemon adapter are read operations. _get_store only
    obtains the agent's store capability; its methods are inspected at their call
    sites. Decorations and server construction are outside a handler invocation.
    This bounded source audit is backed by injected direct and transitive sinks.
    """
    import re

    functions = {n.name: n for n in ast.walk(tree)
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    methods = {} if store_tree is None else {
        n.name: n for n in ast.walk(store_tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    aliases = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            aliases.update({a.asname or a.name: a.name for a in node.names})
        elif isinstance(node, ast.ImportFrom):
            aliases.update({a.asname or a.name: f"{node.module}.{a.name}" for a in node.names})
    effects, seen = [], set()

    def visit(function, chain):
        if id(function) in seen:
            return
        seen.add(id(function))
        body = ast.Module(body=function.body, type_ignores=[])
        bindings = {}
        for node in ast.walk(body):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        bindings.setdefault(target.id, []).append(node.value)
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                bindings.setdefault(node.target.id, []).append(node.value)

        def strings(node, visiting=frozenset()):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                return [node.value]
            if isinstance(node, ast.Name) and node.id not in visiting:
                return [s for value in bindings.get(node.id, [])
                        for s in strings(value, visiting | {node.id})]
            if isinstance(node, ast.JoinedStr):
                return [s for value in node.values for s in strings(value, visiting)]
            if isinstance(node, ast.BinOp):
                return strings(node.left, visiting) + strings(node.right, visiting)
            return []

        for call in (n for n in ast.walk(body) if isinstance(n, ast.Call)):
            target = ast.unparse(call.func)
            name = target.rsplit(".", 1)[-1]
            root = target.split(".", 1)[0]
            expanded = aliases.get(root, root) + target[len(root):]
            location = " -> ".join((*chain, f"{target}:{call.lineno}"))
            if target in {"_api", "_api_async"}:
                if not call.args or not isinstance(call.args[0], ast.Constant) or (
                    call.args[0].value != "GET"
                ):
                    effects.append(("daemon_mutation", location))
                def is_path(value, seen_names=frozenset()):
                    if isinstance(value, ast.Name) and value.id not in seen_names:
                        sources = bindings.get(value.id, [])
                        return bool(sources) and all(is_path(v, seen_names | {value.id}) for v in sources)
                    if isinstance(value, ast.IfExp):
                        return is_path(value.body, seen_names) and is_path(value.orelse, seen_names)
                    if isinstance(value, ast.Constant) and isinstance(value.value, str):
                        return bool(re.fullmatch(r"/[a-z][a-z0-9_/?=&-]*", value.value))
                    if isinstance(value, ast.JoinedStr) and value.values:
                        first = value.values[0]
                        return isinstance(first, ast.Constant) and bool(re.match(
                            r"\A/[a-z][a-z0-9_-]*(?:/|\?|\Z)", first.value,
                        ))
                    return False

                if len(call.args) < 2 or not is_path(call.args[1]):
                    effects.append(("daemon_destination", location))
                if call.keywords or len(call.args) > 2:
                    effects.append(("daemon_arguments", location))
                continue
            if name.startswith(_WRITE_PREFIXES) or name in {
                "execute_write", "system", "popen", "Popen", "run", "exec", "eval",
                "update", "create", "delete", "save", "insert", "log",
            }:
                effects.append(("write_verb", location))
            if re.search(r"(?:urllib|httpx|requests|openai|socket|http\.client)(?:\.|$)", expanded):
                # urllib.parse formats query strings locally; it is not a client.
                if not expanded.startswith("urllib.parse."):
                    effects.append(("network_client", location))
            if name in {"urlopen", "request", "post", "put", "patch", "delete", "connect"} or (
                any(part in expanded.lower() for part in ("client.", "embeddings.", "embedder."))
            ):
                effects.append(("network_client", location))
            if name in {"open", "fdopen"}:
                mode = next((k.value for k in call.keywords if k.arg == "mode"),
                            call.args[1] if len(call.args) > 1 else ast.Constant("r"))
                if not isinstance(mode, ast.Constant) or mode.value not in {"r", "rb", "rt"}:
                    effects.append(("file_write", location))
            if name in {"execute", "executemany"}:
                sql = " ".join(strings(call.args[0])) if call.args else ""
                if not sql.lstrip().upper().startswith("SELECT") or re.search(
                    r"\b(?:UPDATE|INSERT|DELETE|REPLACE|CREATE|ALTER|DROP|ATTACH)\b", sql, re.I
                ):
                    effects.append(("sql_write", location))
            if isinstance(call.func, ast.Name) and name in functions and name != "_get_store":
                visit(functions[name], (*chain, name))
            elif isinstance(call.func, ast.Attribute) and name in methods:
                receiver = ast.unparse(call.func.value)
                sources = bindings.get(receiver, [])
                if receiver in {"self", "s", "store", "_get_store()"} or any(
                    isinstance(v, ast.Call) and ast.unparse(v.func) == "_get_store" for v in sources
                ):
                    visit(methods[name], (*chain, name))

    visit(handler, (handler.name,))
    return effects


def test_every_static_mcp_handler_has_no_unexcepted_effects():
    """A static tool performs no writes beyond its own bookkeeping and sends no network
    request to a destination chosen at call time. Recall alone is excepted for access
    bookkeeping and its fixed-provider embedding client; other write/client paths fail.
    """
    api = _policy()
    assert api.STATIC_ALLOW_EXCEPTIONS == STATIC_EXCEPTIONS
    engine_tree = ast.parse(Path(api.__file__).read_text())
    for index, node in enumerate(engine_tree.body):
        targets = node.targets if isinstance(node, ast.Assign) else (
            [node.target] if isinstance(node, ast.AnnAssign) else []
        )
        if any(isinstance(t, ast.Name) and t.id == "STATIC_ALLOW_TOOLS" for t in targets):
            doc = engine_tree.body[index + 1]
            assert isinstance(doc, ast.Expr) and isinstance(doc.value, ast.Constant)
            assert STATIC_INVARIANT in doc.value.value
            break
    else:
        pytest.fail("STATIC_ALLOW_TOOLS must carry the documented effect invariant")
    for tool in sorted(api.STATIC_ALLOW_TOOLS):
        if not tool.startswith("mcp__"):
            continue
        _, namespace, name = tool.split("__", 2)
        package = namespace.replace("-", "_")
        tree, handlers = _registered_handlers(package)
        assert name in handlers, f"unregistered static tool: {tool}"
        store_path = _package_source(package, "store.py")
        store_tree = ast.parse(store_path.read_text()) if store_path.exists() else None
        effects = _handler_effects(handlers[name], tree, store_tree)
        if tool in api.STATIC_ALLOW_EXCEPTIONS:
            assert effects, f"{tool}: effect traversal became vacuous"
            assert {kind for kind, _ in effects} <= {"write_verb", "sql_write", "network_client"}
        else:
            assert effects == [], f"{tool}: {effects}"


@pytest.mark.parametrize("sink,expected", [
    ('Path("file").write_text("payload")', {"write_verb"}),
    ('open("file", "w")', {"file_write"}),
    ('db.execute("UPDATE rows SET value=1")', {"sql_write"}),
    ('_api("POST", "/tasks", {})', {"daemon_mutation", "daemon_arguments"}),
    ('urllib.request.urlopen("https://example.test")', {"network_client"}),
    ('httpx.get("https://example.test")', {"network_client"}),
    ('requests.get("https://example.test")', {"network_client"}),
    ('client.embeddings.create(input="payload")', {"write_verb", "network_client"}),
    ('store.reflect("payload")', {"write_verb"}),
    ('_api("GET", url)', {"daemon_destination"}),
    ('_api("GET", "https://example.test/path")', {"daemon_destination"}),
    ('_api("GET", "//example.test/path")', {"daemon_destination"}),
    ('_api(method, "/tasks")', {"daemon_mutation"}),
])
@pytest.mark.parametrize("indirect", [False, True])
def test_static_effect_audit_detects_injected_writes_and_clients(sink, expected, indirect):
    # No engine import: these controls must pass even on a feature-absent RED head.
    body = f"def helper():\n    {sink}\n\ndef read_tool():\n    helper()\n" if indirect else (
        f"def read_tool():\n    {sink}\n"
    )
    tree = ast.parse(body)
    handler = next(n for n in tree.body if n.name == "read_tool")
    assert {kind for kind, _ in _handler_effects(handler, tree)} == expected, sink


def test_static_effect_audit_follows_store_methods_and_import_aliases():
    tree = ast.parse("def read_tool():\n    s.read_records()\n")
    store = ast.parse('def read_records(self):\n    self._conn.execute("DELETE FROM rows")\n')
    assert {kind for kind, _ in _handler_effects(tree.body[0], tree, store)} == {"sql_write"}
    tree = ast.parse('from urllib.request import urlopen as fetch\ndef read_tool():\n    fetch(url)\n')
    assert {kind for kind, _ in _handler_effects(tree.body[1], tree)} == {"network_client"}


def test_daemon_read_adapter_uses_configured_base_and_accepts_only_path_arguments():
    tree, handlers = _registered_handlers("pinky_self")
    assert all("api_url" not in {a.arg for a in n.args.args + n.args.kwonlyargs}
               for n in handlers.values())
    functions = {n.name: n for n in ast.walk(tree)
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    for name in ("_api", "_api_async"):
        function = functions[name]
        assert [a.arg for a in function.args.args] == ["method", "path", "body"]
        assert function.args.vararg is None and function.args.kwarg is None
        assert not any(isinstance(n, (ast.Global, ast.Nonlocal)) for n in ast.walk(function))
    api = functions["_api"]
    assignments = {t.id: n.value for n in ast.walk(api) if isinstance(n, ast.Assign)
                   for t in n.targets if isinstance(t, ast.Name)}
    assert ast.unparse(assignments["url"]) == "f'{api_url}{path}'"
    requests = [n for n in ast.walk(api) if isinstance(n, ast.Call)
                and ast.unparse(n.func) == "urllib.request.Request"]
    assert len(requests) == 1 and ast.unparse(requests[0].args[0]) == "url"
    opens = [n for n in ast.walk(api) if isinstance(n, ast.Call)
             and ast.unparse(n.func) == "urllib.request.urlopen"]
    assert len(opens) == 1 and ast.unparse(opens[0].args[0]) == "req"
    async_api = functions["_api_async"]
    calls = [n for n in ast.walk(async_api) if isinstance(n, ast.Call)]
    assert len(calls) == 1
    assert ast.unparse(calls[0]) == "asyncio.to_thread(_api, method, path, body)"
    factory = functions["create_server"]
    defaults = dict(zip((a.arg for a in factory.args.kwonlyargs), factory.args.kw_defaults))
    assert isinstance(defaults["api_url"], ast.Constant)
    assert defaults["api_url"].value == "http://localhost:8888"
    # The configured closure base cannot be rebound by a handler or helper.
    assert not any(isinstance(n, (ast.Global, ast.Nonlocal)) and "api_url" in n.names
                   for n in ast.walk(factory))
    assert not any(isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store) and n.id == "api_url"
                   for n in ast.walk(factory))


def _expected_static_tools():
    builtin = {"Read", "Glob", "Grep", "LS", "ToolSearch", "TodoWrite"}
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
    return frozenset(expected)


def test_static_audit_controls_cover_existing_handlers_without_engine():
    for tool in sorted(_expected_static_tools()):
        if not tool.startswith("mcp__"):
            continue
        _, namespace, name = tool.split("__", 2)
        package = namespace.replace("-", "_")
        tree, handlers = _registered_handlers(package)
        store_path = _package_source(package, "store.py")
        store_tree = ast.parse(store_path.read_text()) if store_path.exists() else None
        effects = _handler_effects(handlers[name], tree, store_tree)
        if tool in STATIC_EXCEPTIONS:
            assert effects, f"{tool}: effect traversal became vacuous"
            assert {kind for kind, _ in effects} <= {"write_verb", "sql_write", "network_client"}
        else:
            assert effects == [], tool


def test_static_effect_audit_tracks_assigned_store_and_path_branches():
    tree = ast.parse("def read_tool():\n    db = _get_store()\n    db.read_records()\n")
    store = ast.parse('def read_records(self):\n    self._conn.execute("UPDATE rows SET value=1")\n')
    assert {kind for kind, _ in _handler_effects(tree.body[0], tree, store)} == {"sql_write"}
    tree = ast.parse('def read_tool(url):\n    path = "/tasks" if safe else url\n    _api("GET", path)\n')
    assert {kind for kind, _ in _handler_effects(tree.body[0], tree)} == {"daemon_destination"}
