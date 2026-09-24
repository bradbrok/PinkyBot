"""Static guard: every tmux ``-t`` target in src/ goes through the exact helpers.

A bare session name after ``-t`` lets tmux fall back to prefix and pattern
matching (see ``pinky_daemon.tmux_targets``). This guard parses every module
under ``src/`` and, in modules that issue tmux commands, requires the argument
after each ``"-t"`` to be an inline call to ``exact_session_target`` (for
target-session commands) or ``exact_pane_target`` (for target-window and
target-pane commands).

A module "issues tmux commands" when it contains a string constant equal to a
full tmux command name. Short aliases (``has``, ``ls``, ...) are not
recognised; use full command names.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

import pytest

from pinky_daemon.agent_registry import _AGENT_NAME_RE
from pinky_daemon.tmux_targets import exact_pane_target, exact_session_target

SRC = Path(__file__).resolve().parents[1] / "src"

SESSION_HELPER = "exact_session_target"
PANE_HELPER = "exact_pane_target"

# -t kind per tmux command: target-session commands take ``=NAME``;
# target-window / target-pane commands take ``=NAME:``.
TARGET_HELPER = {
    **dict.fromkeys(
        (
            "attach-session",
            "has-session",
            "kill-session",
            "list-windows",
            "lock-session",
            "rename-session",
            "set-environment",
            "show-environment",
            "switch-client",
        ),
        SESSION_HELPER,
    ),
    **dict.fromkeys(
        (
            "capture-pane",
            "clear-history",
            "display-message",
            "kill-pane",
            "kill-window",
            "list-panes",
            "new-window",
            "paste-buffer",
            "pipe-pane",
            "rename-window",
            "resize-pane",
            "resize-window",
            "respawn-pane",
            "respawn-window",
            "select-pane",
            "select-window",
            "send-keys",
            "set-option",
            "set-window-option",
            "show-options",
            "show-window-options",
            "split-window",
        ),
        PANE_HELPER,
    ),
}
# Commands without a -t target; they still mark a module as issuing tmux commands.
UNTARGETED = {"kill-server", "list-sessions", "load-buffer", "new-session", "set-buffer"}
TMUX_COMMANDS = set(TARGET_HELPER) | UNTARGETED


@dataclass(frozen=True)
class Violation:
    where: str
    reason: str


def _str_const(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _helper_name(node: ast.AST) -> str | None:
    if not isinstance(node, ast.Call):
        return None
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _sequences(tree: ast.AST):
    for node in ast.walk(tree):
        if isinstance(node, ast.List | ast.Tuple):
            yield node, node.elts
        elif isinstance(node, ast.Call):
            yield node, node.args


def _issues_tmux_commands(tree: ast.AST) -> bool:
    return any(_str_const(node) in TMUX_COMMANDS for node in ast.walk(tree))


def _fused_target_flag(node: ast.AST) -> bool:
    """``"-tNAME"`` or ``f"-t{name}"``: a target glued onto the flag."""
    text = _str_const(node)
    if text is not None:
        return text.startswith("-t") and text != "-t"
    if isinstance(node, ast.JoinedStr) and node.values:
        head = _str_const(node.values[0])
        return head is not None and head.startswith("-t")
    return False


def check_source(source: str, filename: str = "<src>") -> tuple[list[Violation], int]:
    """Return (violations, number of well-formed tmux ``-t`` sites)."""
    tree = ast.parse(source, filename=filename)
    if not _issues_tmux_commands(tree):
        return [], 0
    violations: list[Violation] = []
    sites = 0
    for _owner, elements in _sequences(tree):
        leading = next((c for c in map(_str_const, elements) if c is not None), None)
        in_tmux_argv = leading in TMUX_COMMANDS
        for index, element in enumerate(elements):
            where = f"{filename}:{getattr(element, 'lineno', '?')}"
            if in_tmux_argv and _fused_target_flag(element):
                violations.append(Violation(where, "target fused into the -t flag"))
                continue
            if _str_const(element) != "-t":
                continue
            command = leading
            if index + 1 >= len(elements):
                violations.append(Violation(where, "-t without an inline target"))
                continue
            helper = _helper_name(elements[index + 1])
            if helper not in (SESSION_HELPER, PANE_HELPER):
                violations.append(
                    Violation(
                        where, f"{command or 'tmux'} -t target is not built by an exact helper"
                    )
                )
                continue
            if command not in TARGET_HELPER:
                violations.append(
                    Violation(
                        where, f"unclassified tmux command {command!r}; add it to TARGET_HELPER"
                    )
                )
                continue
            if helper != TARGET_HELPER[command]:
                violations.append(
                    Violation(where, f"{command} needs {TARGET_HELPER[command]}, not {helper}")
                )
                continue
            sites += 1
    return violations, sites


def _scan_src() -> tuple[list[Violation], int, set[str]]:
    violations: list[Violation] = []
    sites = 0
    tmux_modules: set[str] = set()
    for path in sorted(SRC.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        rel = str(path.relative_to(SRC))
        found, count = check_source(source, rel)
        violations.extend(found)
        sites += count
        if _issues_tmux_commands(ast.parse(source)):
            tmux_modules.add(rel)
    return violations, sites, tmux_modules


def test_every_src_tmux_target_uses_the_exact_helper():
    violations, sites, tmux_modules = _scan_src()
    assert violations == []
    # Non-vacuity: the scan really sees the known tmux call sites.
    assert {"pinky_daemon/tmux_session.py", "pinky_daemon/tmux_dream_runner.py"} <= tmux_modules
    assert sites >= 20


@pytest.mark.parametrize(
    ("snippet", "reason"),
    [
        ('run("has-session", "-t", self.session_name)', "not built by an exact helper"),
        ('run("kill-session", "-t", "=" + name)', "not built by an exact helper"),
        ('run("send-keys", "-t", f"={name}:", "Enter")', "not built by an exact helper"),
        ('args = ["capture-pane", "-t", target, "-p"]', "not built by an exact helper"),
        ('run("send-keys", "-t", exact_session_target(n), "Enter")', "needs exact_pane_target"),
        ('run("resize-window", "-t", exact_session_target(n))', "needs exact_pane_target"),
        ('run("kill-session", "-t", exact_pane_target(n))', "needs exact_session_target"),
        (
            'run("send-keys", "-t", exact_pane_target(n))\n'
            'run("swap-pane", "-t", exact_pane_target(n))',
            "unclassified tmux command",
        ),
        ('args = ["send-keys"]; args += ["-t"]', "-t without an inline target"),
        ('run("send-keys", f"-t{name}")', "fused into the -t flag"),
        ('run("send-keys", "-t=pinky-x")', "fused into the -t flag"),
        ('run("send-keys", "-tpinky-x", "Enter")', "fused into the -t flag"),
    ],
)
def test_guard_flags_bare_or_mismatched_targets(snippet, reason):
    violations, _ = check_source(snippet)
    assert violations, snippet
    assert any(reason in v.reason for v in violations), violations


@pytest.mark.parametrize(
    "snippet",
    [
        'run("kill-session", "-t", exact_session_target(self.session_name))',
        'run("has-session", "-t", tmux_targets.exact_session_target(name))',
        'args = ["capture-pane", "-t", exact_pane_target(t or name), "-p"]',
        'run("set-option", "-w", "-t", exact_pane_target(name), "remain-on-exit", "on")',
        'run("list-sessions", "-F", "#{session_name}")',
    ],
)
def test_guard_accepts_exact_targets(snippet):
    assert check_source(snippet)[0] == []


def test_guard_ignores_modules_that_issue_no_tmux_commands():
    # e.g. ``logger -t TAG`` in a module with no tmux command names.
    assert check_source('run(["logger", "-t", "tag", message])') == ([], 0)


def test_exact_session_target_form():
    assert exact_session_target("pinky-x") == "=pinky-x"


def test_exact_pane_target_form():
    assert exact_pane_target("pinky-x") == "=pinky-x:"
    assert exact_pane_target("login-hold-x") == "=login-hold-x:"


REFUSED_NAMES = [
    "",
    None,
    "pinky-x.0",
    "pinky-x:1",
    "$1",
    "pinky-x;",
    "pinky-a;b",
    "pinky-x\n",
    "pinky-a\tb",
    "pinky-\x1bx",
    "pinky-x\x00",
    "pinky x",
    "Pinky-x",
    "pinky-é",
    "pinky-x*",
    "pinky-'x'",
    "pinky-#x",
    "pinky-%1",
    "pinky-{x}",
    "pinky-x,y",
    "pinky/x",
    "=pinky-x",
]


@pytest.mark.parametrize("helper", [exact_session_target, exact_pane_target])
@pytest.mark.parametrize("name", REFUSED_NAMES)
def test_names_outside_the_allowlist_are_refused(helper, name):
    with pytest.raises(ValueError):
        helper(name)


@pytest.mark.parametrize("helper", [exact_session_target, exact_pane_target])
def test_allowlist_is_the_agent_name_alphabet(helper):
    """Session names are a prefix plus an agent name: every character an agent
    name may hold is accepted, and every other ASCII character is refused."""
    for code in range(0x80):
        char = chr(code)
        if _AGENT_NAME_RE.fullmatch("a" + char):
            assert helper("pinky-a" + char).startswith("=pinky-a" + char)
        else:
            with pytest.raises(ValueError):
                helper("pinky-a" + char)


@pytest.mark.parametrize("helper", [exact_session_target, exact_pane_target])
@pytest.mark.parametrize(
    "prefix", ["pinky-", "pinky-codex-", "pinky-codex-as-", "pinky-dream-", "login-hold-"]
)
@pytest.mark.parametrize("agent", ["a", "0", "a_b-9", "a" * 63])
def test_every_daemon_session_name_is_accepted(helper, prefix, agent):
    assert _AGENT_NAME_RE.fullmatch(agent)
    assert helper(prefix + agent).startswith("=" + prefix + agent)
