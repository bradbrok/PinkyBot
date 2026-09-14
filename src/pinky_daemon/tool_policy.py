"""Three-valued action policy with explicit caller-resolved facts and no I/O.

The Bash classifier is a bounded pattern table, not a shell parser or a sandbox.
Lexical path checks do not resolve symlinks. Native tool denials, signed daemon
boundaries and process isolation remain the hard enforcement boundaries.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import posixpath
import re
import shlex
from dataclasses import dataclass, field
from typing import Any

from pinky_daemon.skill_tool_policy import ToolPatternValidationError, parse_tool_pattern

STATIC_ALLOW_TOOLS = frozenset({
    "Read", "Glob", "Grep", "LS", "ToolSearch", "TodoWrite",
    *[f"mcp__pinky-memory__{name}" for name in (
        "recall", "introspect", "kg_query", "kg_connections", "kg_stats", "kg_timeline",
        "memory_query", "memory_links",
    )],
    *[f"mcp__pinky-self__{name}" for name in (
        "context_status", "who_am_i", "load_my_context", "agent_status", "check_my_health",
        "search_history", "list_my_schedules", "get_next_task", "get_presentation_template",
        "list_presentations", "get_owner_profile", "get_my_research_assignments",
        "list_research_topics", "get_research_detail", "list_agents", "list_my_skills",
        "list_available_skills", "list_triggers", "get_attribution", "get_agent_card",
        "list_voice_calls", "list_call_requests", "get_app_source", "list_apps",
    )],
})
"""A static tool performs no writes beyond its own bookkeeping and sends no network request to a destination chosen at call time.

Literal GET through the configured daemon adapter is a read boundary. The named
exceptions below document session state and fixed-provider memory bookkeeping.
"""
STATIC_ALLOW_EXCEPTIONS = {
    "TodoWrite": "session-local",
    "mcp__pinky-memory__recall": (
        "access bookkeeping write (accessed_at/access_count/weight) + query embedding sent "
        "to the configured embeddings provider (fixed destination)"
    ),
}
REASON_CODES = frozenset({
    "static_readonly", "default_allow", "override", "hook_tamper", "policy_unavailable",
    "self_modification", "owner_only", "third_party_recipient", "broadcast", "financial",
    "public_surface", "destructive_shell", "host_ops",
})
UNAVAILABLE_REASON = "Denied: policy service unavailable; retry the same call in 10 seconds."
TIMEOUT_REASON = (
    "Denied: no owner decision within the approval window; do not retry automatically, "
    "tell the owner what you needed."
)


@dataclass(frozen=True)
class PolicyContext:
    agent_name: str
    isolated: bool
    principal_class: str
    transport: str
    tool_name: str
    tool_input: dict
    agent_dir: str
    home_dir: str
    tmp_roots: list[str]
    public_remotes: list[str]
    known_recipients: frozenset[str] = frozenset()
    data_roots: list[str] = field(default_factory=list)
    repo_default_branches: dict[str, str] = field(default_factory=dict)
    is_worktree_checkout: bool | None = None
    cwd: str = ""


@dataclass(frozen=True)
class Evaluation:
    evaluated_permission: str
    type: str
    reason_code: str
    principal_class: str
    input_sha256: str
    rule_id: str | None = None

    def to_record(self) -> dict:
        details = {"type": self.type, "reason_code": self.reason_code,
                   "principal_class": self.principal_class, "input_sha256": self.input_sha256}
        if self.rule_id is not None:
            details["rule_id"] = self.rule_id
        return {"evaluated_permission": self.evaluated_permission, "evaluation": details}


def canonical_input_sha256(tool_input: dict) -> str:
    return hashlib.sha256(json.dumps(tool_input, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False, default=str).encode()).hexdigest()


def matches_tool_pattern(pattern: str, tool_name: str, tool_input: dict | None = None) -> bool:
    try:
        name, argument = parse_tool_pattern(pattern)
    except ToolPatternValidationError:
        return False
    if not fnmatch.fnmatchcase(tool_name, name):
        return False
    return argument is None or (tool_input is not None and any(
        argument in value for value in tool_input.values() if isinstance(value, str)
    ))


def _path(path: str, ctx: PolicyContext) -> str:
    if path == "~":
        path = ctx.home_dir
    if path.startswith("~/"):
        path = posixpath.join(ctx.home_dir, path[2:])
    return posixpath.normpath(posixpath.join(ctx.cwd or ctx.agent_dir, path))


def _within(path: str, root: str, ctx: PolicyContext) -> bool:
    path, root = _path(path, ctx), _path(root, ctx)
    return path == root or path.startswith(root.rstrip("/") + "/")


def _protected(path: str, ctx: PolicyContext) -> bool:
    normalized = _path(path, ctx)
    return (_within(path, posixpath.join(ctx.agent_dir, ".claude"), ctx)
            or normalized == posixpath.join(ctx.agent_dir, ".mcp.json")
            or (_within(path, posixpath.join(ctx.home_dir, ".claude"), ctx)
                and fnmatch.fnmatchcase(posixpath.basename(normalized), "settings*.json")))


def classify_bash(command: str, ctx: PolicyContext) -> list[str]:
    found: set[str] = set()
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|\n<>")
        lexer.whitespace = " \t\r"
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        return ["shell.destructive"]
    segments: list[list[str]] = [[]]
    for token in tokens:
        if token and all(c in ";&|\n" for c in token):
            segments.append([])
        else:
            segments[-1].append(token)
    for args in segments:
        if not args:
            continue
        executable = posixpath.basename(args[0])
        rest = args[1:]
        literal = " ".join(args)
        if any(_protected(value, ctx) for value in rest if value):
            found.add("self.modify_guard")
        if executable == "sqlite3" and any(
            _within(value, root, ctx) for value in rest if not value.startswith("-")
            for root in ctx.data_roots
        ):
            found.add("self.modify_guard")
        if executable == "rm" and any(
            arg == "--recursive" or (arg.startswith("-") and "r" in arg.lower()) for arg in rest
        ):
            targets = [arg for arg in rest if not arg.startswith("-")]
            roots = [ctx.agent_dir, *ctx.tmp_roots]
            if not targets or any(not any(_within(t, root, ctx) for root in roots) for t in targets):
                found.add("shell.destructive")
        if executable == "git" and rest[:1] == ["push"]:
            positional = [arg for arg in rest[1:] if not arg.startswith("-")]
            remote = positional[0] if positional else "origin"
            branches = [arg.rsplit(":", 1)[-1] for arg in positional[1:]]
            if any(arg.startswith("--force") or arg == "-f" for arg in rest) and (
                not branches or any(b in {"main", "master"} for b in branches)
            ):
                found.add("shell.destructive")
            if remote in ctx.public_remotes or not ctx.repo_default_branches:
                default = ctx.repo_default_branches.get(remote)
                if not default or not branches or default in branches:
                    found.add("public.publish")
        if executable == "git" and rest[:1] == ["reset"] and "--hard" in rest:
            if ctx.is_worktree_checkout is not True:
                found.add("shell.destructive")
        if (executable in {"sudo", "mkfs", "killall"} or executable.startswith("mkfs.")
                or (executable == "kill" and rest == ["-9", "-1"])
                or (executable == "pkill" and "-f" in rest
                    and re.search(r"\b(pinky|claude|codex)\b", literal))
                or (executable == "dd" and any(a.startswith("of=/dev/") for a in rest))
                or (executable == "chmod" and "-R" in rest and "777" in rest)
                or (executable == "crontab" and "-r" in rest)
                or re.search(r"\b(?:DROP\s+TABLE|DELETE\s+FROM\s+\S+\s+WHERE\s+1)\b", literal, re.I)):
            found.add("shell.destructive")
        if ((executable in {"docker", "podman"} and (rest[:1] in (["rm"], ["rmi"])
                or rest[:2] in (["system", "prune"], ["volume", "rm"])))
                or (executable.startswith("lima") and rest[:1] in (["delete"], ["stop"]))
                or (executable == "tailscale" and rest[:1] in (["down"], ["logout"], ["funnel"]))):
            found.add("host.container_ops")
        if ((executable == "launchctl" and rest[:1] in (["kickstart"], ["bootout"], ["unload"]))
                or (executable == "systemctl" and rest[:1] in (["restart"], ["stop"]))):
            if "pinky" in literal:
                found.add("owner_only.daemon_control")
        if ((executable == "npm" and rest[:1] == ["publish"])
                or (executable in {"pip", "twine"} and rest[:1] == ["upload"])
                or (executable == "gh" and (rest[:2] in (["release", "create"], ["pr", "merge"])
                    or (rest[:2] == ["repo", "edit"] and "--visibility" in rest)))):
            found.add("public.publish")
    return [rule["rule_id"] for rule in DEFAULT_RULES if rule["rule_id"] in found]


def _matches(rule_id: str, ctx: PolicyContext) -> bool:
    tool, values = ctx.tool_name, ctx.tool_input
    name = tool.removeprefix("mcp__pinky-self__")
    if tool == "Bash":
        return rule_id in classify_bash(str(values.get("command", "")), ctx)
    if rule_id == "self.modify_guard":
        return tool in {"Write", "Edit", "NotebookEdit"} and any(
            _protected(str(values[key]), ctx) for key in ("file_path", "notebook_path") if values.get(key)
        )
    if rule_id == "owner_only.daemon_control":
        return tool.startswith("mcp__pinky-self__") and (name in {
            "update_and_restart", "restart_daemon", "register_agent", "delete_app",
            "remove_skill", "add_skill", "install_skill", "set_thinking_effort",
        } or name.startswith("kb_delete"))
    if rule_id == "outbound.third_party":
        if not tool.startswith("mcp__pinky-messaging__") or tool.endswith("__broadcast"):
            return False
        if not (tool.endswith(("__send", "__thread")) or "__send_" in tool):
            return False
        chat = str(values.get("chat_id", ""))
        platform = str(values.get("platform", ""))
        return not chat or not ({chat, f"{platform}:{chat}"} & ctx.known_recipients)
    if rule_id == "outbound.broadcast":
        return tool == "mcp__pinky-messaging__broadcast"
    if rule_id == "outbound.voice":
        return tool == "mcp__pinky-self__propose_call"
    if rule_id == "money.*":
        return bool(re.search(r"\Amcp__.*(?:purchas|payment|refund|invoice_send|charge)", tool, re.I))
    if rule_id == "public.publish":
        return tool in {"mcp__pinky-self__publish_research", "mcp__pinky-self__create_presentation"} or (
            tool == "mcp__pinky-self__update_app" and values.get("status") == "deployed"
        )
    if rule_id == "agent.spawn_unbounded":
        return tool in {"Agent", "Workflow", "mcp__pinky-self__spawn_clone"}
    return False


DEFAULT_RULES: tuple[dict[str, Any], ...] = tuple(
    {"rule_id": rid, "reason_code": reason, "owner": owner, "other": "deny",
     "matcher": (lambda ctx, rid=rid: _matches(rid, ctx))}
    for rid, reason, owner in (
        ("self.modify_guard", "self_modification", "deny"),
        ("owner_only.daemon_control", "owner_only", "allow"),
        ("outbound.third_party", "third_party_recipient", "pause"),
        ("outbound.broadcast", "broadcast", "pause"),
        ("outbound.voice", "owner_only", "allow"),
        ("money.*", "financial", "pause"),
        ("public.publish", "public_surface", "pause"),
        ("shell.destructive", "destructive_shell", "pause"),
        ("host.container_ops", "host_ops", "pause"),
        ("agent.spawn_unbounded", "owner_only", "allow"),
    )
)


def evaluate(ctx: PolicyContext, *, overrides=(), now: float,
             tamper: bool = False, unavailable: bool = False) -> Evaluation:
    principal = ctx.principal_class
    if principal not in {"owner", "schedule", "approved_user", "group", "agent"} or (
        ctx.isolated and principal in {"owner", "schedule"}
    ):
        principal = "group"
    digest = canonical_input_sha256(ctx.tool_input)

    def result(permission, kind, reason, rule_id=None):
        return Evaluation(permission, kind, reason, principal, digest, rule_id)

    if tamper:
        return result("deny", "tamper", "hook_tamper")
    if unavailable:
        return result("deny", "unavailable", "policy_unavailable")
    if ctx.tool_name in STATIC_ALLOW_TOOLS:
        return result("allow", "static", "static_readonly")
    matched = next((rule for rule in DEFAULT_RULES if rule["matcher"](ctx)), None)
    candidates = []
    for row in overrides:
        if row.get("valid_until") is not None and row["valid_until"] <= now:
            continue
        if row.get("rule_id") and (not matched or row["rule_id"] != matched["rule_id"]):
            continue
        pattern = row.get("pattern", "")
        if row.get("decision") not in {"allow", "deny", "pause"} or not matches_tool_pattern(
            pattern, ctx.tool_name, ctx.tool_input
        ):
            continue
        name, argument = parse_tool_pattern(pattern)
        specificity = (3 if argument else 1 if "*" in name else 2, len(name.replace("*", "")),
                       len(argument or ""), {"pause": 0, "allow": 1, "deny": 2}[row["decision"]])
        candidates.append((specificity, row))
    if candidates:
        row = max(candidates, key=lambda item: item[0])[1]
        return result(row["decision"], "override", "override", row.get("rule_id"))
    if matched:
        return result(matched["owner" if principal in {"owner", "schedule"} else "other"],
                      "rule", matched["reason_code"], matched["rule_id"])
    return result("allow", "default", "default_allow")
