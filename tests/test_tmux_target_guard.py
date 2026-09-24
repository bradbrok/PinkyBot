"""Static guard: every tmux target in src/ is built by the exact helpers.

A bare session name after ``-t`` lets tmux fall back to prefix and pattern
matching (see ``pinky_daemon.tmux_targets``). This guard parses every module
under ``src/`` and classifies every ``-t``/``-s`` flag literal in it.

A *tmux argv* is a list, tuple or call-argument sequence holding a tmux
command: a full command name anywhere in it, or, at the command position
(first element, after the tmux binary and its own options such as ``-L`` or
``-S``, or after a ``;`` separator), an alias or unique prefix that tmux
itself resolves. An element ending in ``;`` ends one tmux command and starts
the next. ``args = [...]`` grown by ``args.extend(...)``, ``args.append(...)``
or ``args += [...]`` in the same function, and ``[...] + [...]``, are read as
one argv.

In a tmux argv:

* the argument after a target flag (``-t``, and ``-s`` where the command takes
  a source target) is an inline ``exact_session_target(...)`` for a session
  target or ``exact_pane_target(...)`` for a window or pane target. Client
  targets are not sessions and are left alone.
* a target fused into its flag (``"-tNAME"``, ``"-t" + name``,
  ``f"-t{name}"``, ``"-t%s" % name``, ``"-t{}".format(name)``) or clustered
  with other flags (``"-pt"``) is refused.
* text arguments of ``send-keys`` and ``rename-*`` follow ``--``, so text
  starting with ``-`` is never parsed as flags.

Elsewhere:

* in a module that issues tmux commands, a target flag outside any argv
  (``flag = "-t"``) is refused: the guard cannot see where it lands.
* a ``-t``/``-s`` in another program's argv (``logger -t``,
  ``podman exec -t``, ``add_argument("-t")``) is left alone, unless an exact
  helper follows it: then it is meant for tmux, but its command is not
  visible, so the helper kind cannot be checked.

Every flag literal lands in exactly one bucket. The src test also checks that
an independent enumeration of flag literals is fully classified, so a call
shape the guard does not understand fails loudly instead of going unseen.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from pinky_daemon.agent_registry import _AGENT_NAME_RE
from pinky_daemon.tmux_targets import exact_pane_target, exact_session_target, text_argument

SRC = Path(__file__).resolve().parents[1] / "src"

SESSION = "exact_session_target"
PANE = "exact_pane_target"
CLIENT = "client"
HELPERS = (SESSION, PANE)

_S, _P, _C = SESSION, PANE, CLIENT
# tmux command table: name -> (alias, what -t names, what -s names).
# A ``None`` -t kind means the command takes no -t. A ``None`` -s kind means
# no -s, or an -s that is not a target (``new-session -s NAME``,
# ``paste-buffer -s SEPARATOR``, ``set-option -s``). Window targets use the
# pane helper: ``=NAME:`` names the session's current window and active pane.
COMMANDS: dict[str, tuple[str | None, str | None, str | None]] = {
    "attach-session": ("attach", _S, None),
    "bind-key": ("bind", None, None),
    "break-pane": ("breakp", _P, _P),
    "capture-pane": ("capturep", _P, None),
    "choose-buffer": (None, _P, None),
    "choose-client": (None, _P, None),
    "choose-tree": (None, _P, None),
    "clear-history": ("clearhist", _P, None),
    "clear-prompt-history": ("clearphist", None, None),
    "clock-mode": (None, _P, None),
    "command-prompt": (None, _C, None),
    "confirm-before": ("confirm", _C, None),
    "copy-mode": (None, _P, _P),
    "customize-mode": (None, _P, None),
    "delete-buffer": ("deleteb", None, None),
    "detach-client": ("detach", _C, _S),
    "display-menu": ("menu", _P, None),
    "display-message": ("display", _P, None),
    "display-panes": ("displayp", _C, None),
    "display-popup": ("popup", _P, None),
    "find-window": ("findw", _P, None),
    "has-session": ("has", _S, None),
    "if-shell": ("if", _P, None),
    "join-pane": ("joinp", _P, _P),
    "kill-pane": ("killp", _P, None),
    "kill-server": (None, None, None),
    "kill-session": (None, _S, None),
    "kill-window": ("killw", _P, None),
    "last-pane": ("lastp", _P, None),
    "last-window": ("last", _S, None),
    "link-window": ("linkw", _P, _P),
    "list-buffers": ("lsb", None, None),
    "list-clients": ("lsc", _S, None),
    "list-commands": ("lscm", None, None),
    "list-keys": ("lsk", None, None),
    "list-panes": ("lsp", _P, None),
    "list-sessions": ("ls", None, None),
    "list-windows": ("lsw", _S, None),
    "load-buffer": ("loadb", _C, None),
    "lock-client": ("lockc", _C, None),
    "lock-server": ("lock", None, None),
    "lock-session": ("locks", _S, None),
    "move-pane": ("movep", _P, _P),
    "move-window": ("movew", _P, _P),
    "new-session": ("new", _S, None),
    "new-window": ("neww", _P, None),
    "next-layout": ("nextl", _P, None),
    "next-window": ("next", _S, None),
    "paste-buffer": ("pasteb", _P, None),
    "pipe-pane": ("pipep", _P, None),
    "previous-layout": ("prevl", _P, None),
    "previous-window": ("prev", _S, None),
    "refresh-client": ("refresh", _C, None),
    "rename-session": ("rename", _S, None),
    "rename-window": ("renamew", _P, None),
    "resize-pane": ("resizep", _P, None),
    "resize-window": ("resizew", _P, None),
    "respawn-pane": ("respawnp", _P, None),
    "respawn-window": ("respawnw", _P, None),
    "rotate-window": ("rotatew", _P, None),
    "run-shell": ("run", _P, None),
    "save-buffer": ("saveb", None, None),
    "select-layout": ("selectl", _P, None),
    "select-pane": ("selectp", _P, None),
    "select-window": ("selectw", _P, None),
    "send-keys": ("send", _P, None),
    "send-prefix": (None, _P, None),
    "server-access": (None, None, None),
    "set-buffer": ("setb", _C, None),
    "set-environment": ("setenv", _S, None),
    "set-hook": (None, _P, None),
    "set-option": ("set", _P, None),
    "set-window-option": ("setw", _P, None),
    "show-buffer": ("showb", None, None),
    "show-environment": ("showenv", _S, None),
    "show-hooks": (None, _P, None),
    "show-messages": ("showmsgs", _C, None),
    "show-options": ("show", _P, None),
    "show-prompt-history": ("showphist", None, None),
    "show-window-options": ("showw", _P, None),
    "source-file": ("source", _P, None),
    "split-window": ("splitw", _P, None),
    "start-server": ("start", None, None),
    "suspend-client": ("suspendc", _C, None),
    "swap-pane": ("swapp", _P, _P),
    "swap-window": ("swapw", _P, _P),
    "switch-client": ("switchc", _S, None),
    "unbind-key": ("unbind", None, None),
    "unlink-window": ("unlinkw", _P, None),
    "wait-for": ("wait", None, None),
}
ALIASES = {alias: name for name, (alias, _t, _s) in COMMANDS.items() if alias}

# Commands with free-text arguments, and which of their flags take a value.
TEXT_COMMANDS = {"send-keys": "cNt", "rename-session": "t", "rename-window": "t"}

# tmux's own options (before the command) that take a value.
GLOBAL_VALUE_OPTIONS = "cfLST"

# A literal the guard must classify: it starts like a single-dash option whose
# letters include t or s.
FLAG_LITERAL = re.compile(r"-[A-Za-z0-9]*[ts]")
# A literal that can only be a -t/-s flag, possibly with its value glued on.
TARGET_FLAG = re.compile(r"-[ts](?![A-Za-z0-9])")
OPTION = re.compile(r"-([A-Za-z0-9]+)")


@dataclass(frozen=True)
class Violation:
    where: str
    reason: str


@dataclass
class Report:
    filename: str
    violations: list[Violation] = field(default_factory=list)
    sites: list[str] = field(default_factory=list)
    buckets: dict[int, str] = field(default_factory=dict)
    unclassified: list[str] = field(default_factory=list)

    def where(self, node: ast.AST) -> str:
        return f"{self.filename}:{getattr(node, 'lineno', '?')}"

    def classify(self, literal: ast.AST | None, bucket: str) -> None:
        if literal is not None and _is_flag_literal(literal):
            self.buckets.setdefault(id(literal), bucket)

    def violation(self, node: ast.AST, reason: str) -> None:
        self.violations.append(Violation(self.where(node), reason))
        self.classify(node, "violation")


def resolve_command(word: str) -> str | None:
    """Resolve a command word the way tmux does: name, alias, unique prefix."""
    if word in COMMANDS:
        return word
    if word in ALIASES:
        return ALIASES[word]
    matches = [name for name in COMMANDS if word and name.startswith(word)]
    return matches[0] if len(matches) == 1 else None


def _str_const(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _is_flag_literal(node: ast.AST) -> bool:
    text = _str_const(node)
    return text is not None and FLAG_LITERAL.match(text) is not None


def _head_is_flag(element: ast.AST) -> bool:
    head = _leading(element)[0]
    return head is not None and _is_flag_literal(head)


def _leading(node: ast.AST) -> tuple[ast.Constant | None, bool]:
    """The literal an argument expression starts with, and whether more follows."""
    if _str_const(node) is not None:
        return node, False
    if isinstance(node, ast.JoinedStr) and node.values:
        head = node.values[0]
        if _str_const(head) is not None:
            return head, len(node.values) > 1
        return None, True
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add | ast.Mod):
        return _leading(node.left)[0], True
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "format"
    ):
        return _leading(node.func.value)[0], True
    return None, True


def _argument_root(literal: ast.AST, parent: dict[ast.AST, ast.AST]) -> ast.AST | None:
    """The argument expression ``literal`` starts, or None when it sits inside."""
    current = literal
    while True:
        up = parent.get(current)
        if isinstance(up, ast.JoinedStr):
            if up.values[0] is not current:
                return None
        elif isinstance(up, ast.BinOp) and isinstance(up.op, ast.Add | ast.Mod):
            if up.left is not current:
                return None
        elif isinstance(up, ast.Attribute) and up.attr == "format":
            call = parent.get(up)
            if not (isinstance(call, ast.Call) and call.func is up):
                return current
            up = call
        elif isinstance(up, ast.FormattedValue):
            return None
        else:
            return current
        current = up


def _is_sequence_element(node: ast.AST, parent: dict[ast.AST, ast.AST]) -> bool:
    up = parent.get(node)
    if isinstance(up, ast.List | ast.Tuple):
        return any(element is node for element in up.elts)
    if isinstance(up, ast.Call):
        return any(arg is node for arg in up.args)
    return False


def _helper_name(node: ast.AST) -> str | None:
    if not isinstance(node, ast.Call):
        return None
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _is_tmux_binary(node: ast.AST) -> bool:
    text = _str_const(node)
    if text is not None:
        return text == "tmux" or text.endswith("/tmux")
    if isinstance(node, ast.Name):
        return "tmux" in node.id.lower()
    if isinstance(node, ast.Attribute):
        return "tmux" in node.attr.lower()
    return False


def _ends_command(node: ast.AST) -> bool:
    """tmux splits an argument ending in ``;`` (but not ``\\;``) off as a separator."""
    text = _str_const(node)
    if text is None and isinstance(node, ast.JoinedStr) and node.values:
        text = _str_const(node.values[-1])
    return text is not None and text.endswith(";") and not text.endswith("\\;")


def _segments(elements: list[ast.AST]) -> list[list[ast.AST]]:
    segments: list[list[ast.AST]] = [[]]
    for element in elements:
        segments[-1].append(element)
        if _ends_command(element):
            segments.append([])
    return segments


def _after_global_options(segment: list[ast.AST]) -> int:
    """Index of the command word in a segment led by the tmux binary."""
    index = 1
    while index < len(segment):
        text = _str_const(segment[index])
        if text is None or not text.startswith("-") or text == "-":
            break
        index += 1
        if text == "--":
            break
        if text[-1] in GLOBAL_VALUE_OPTIONS:
            index += 1
    return index


def _find_command(
    segment: list[ast.AST], position: int, tmux_context: bool
) -> tuple[int | None, str | None]:
    if position < len(segment):
        word = _str_const(segment[position])
        resolved = resolve_command(word) if word is not None else None
        has_target_flag = any(_head_is_flag(element) for element in segment)
        if resolved and (word == resolved or tmux_context or has_target_flag):
            return position, resolved
    for index, element in enumerate(segment):
        if _str_const(element) in COMMANDS:
            return index, _str_const(element)
    return None, None


def _check_argv(elements: list[ast.AST], report: Report) -> None:
    segments = _segments(elements)
    binary_led = bool(elements) and _is_tmux_binary(elements[0])
    found: list[tuple[int | None, str | None]] = []
    tmux_context = binary_led
    for number, segment in enumerate(segments):
        position = _after_global_options(segment) if number == 0 and binary_led else 0
        index, command = _find_command(segment, position, tmux_context)
        found.append((index, command))
        tmux_context = tmux_context or command is not None
    if not tmux_context:
        _check_other_program(elements, report)
        return
    for segment, (index, command) in zip(segments, found, strict=True):
        if command is None:
            for element in segment:
                if _head_is_flag(element):
                    report.violation(
                        _leading(element)[0], "cannot tell which tmux command this flag belongs to"
                    )
            continue
        for element in segment[:index]:
            report.classify(_leading(element)[0], "before the tmux command")
        _check_command(command, segment[index + 1 :], report)


def _check_other_program(elements: list[ast.AST], report: Report) -> None:
    for index, element in enumerate(elements):
        head = _leading(element)[0]
        follows = elements[index + 1] if index + 1 < len(elements) else None
        if (
            _str_const(element) in ("-t", "-s")
            and follows is not None
            and _helper_name(follows) in HELPERS
        ):
            report.violation(element, "cannot tell which tmux command this flag belongs to")
            continue
        report.classify(head, "another program's argv")


def _check_command(command: str, body: list[ast.AST], report: Report) -> None:
    _alias, t_kind, s_kind = COMMANDS[command]
    targets = {"t": t_kind}
    if s_kind is not None:
        targets["s"] = s_kind
    value_flags = TEXT_COMMANDS.get(command)
    pending: tuple[str, ast.AST, str] | str | None = None
    text_only = False
    for element in body:
        head, dynamic = _leading(element)
        text = head.value if head is not None else None
        if pending == "value":
            report.classify(head, "flag value")
            pending = None
            continue
        if pending is not None:
            _check_target_value(command, pending, element, report)
            report.classify(head, "target value")
            pending = None
            continue
        if text_only:
            report.classify(head, "text after --")
            continue
        if text == "--" and not dynamic:
            text_only = True
            continue
        option = OPTION.match(text) if text is not None else None
        if option is None:
            # A positional argument, or an expression the guard cannot read.
            if value_flags is not None:
                if head is None or dynamic:
                    report.violation(element, f"{command} text argument must follow --")
                else:
                    text_only = True  # a literal positional ends tmux's flag parsing
            report.classify(head, "positional")
            continue
        letters = option.group(1)
        glued = dynamic or len(option.group(0)) < len(text)
        target = next((i for i, letter in enumerate(letters) if letter in targets), None)
        if target is None:
            report.classify(head, "not a target flag")
            if value_flags is not None:
                if dynamic:
                    report.violation(element, f"{command} text argument must follow --")
                elif letters[-1] in value_flags and not glued:
                    pending = "value"
            continue
        flag = f"-{letters[target]}"
        kind = targets[letters[target]]
        if kind is None:
            report.violation(head, f"{command} takes no {flag} target")
        elif target > 0:
            report.violation(head, f"{command} {flag} clustered with other flags")
        elif glued or len(letters) > 1:
            report.violation(head, f"{command} target fused into the {flag} flag")
        else:
            pending = (kind, head, flag)
    if isinstance(pending, tuple):
        report.violation(pending[1], f"{pending[2]} without an inline target")


def _check_target_value(
    command: str, pending: tuple[str, ast.AST, str], value: ast.AST, report: Report
) -> None:
    kind, flag_node, flag = pending
    if kind == CLIENT:
        report.classify(flag_node, "client target")
        return
    helper = _helper_name(value)
    if helper not in HELPERS:
        report.violation(flag_node, f"{command} {flag} target is not built by an exact helper")
    elif helper != kind:
        report.violation(flag_node, f"{command} {flag} needs {kind}, not {helper}")
    else:
        report.classify(flag_node, "exact target")
        report.sites.append(f"{report.where(flag_node)} {command} {flag} {helper}")


def _scope_nodes(scope: ast.AST):
    stack = list(ast.iter_child_nodes(scope))
    while stack:
        node = stack.pop()
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef | ast.Lambda):
            continue
        yield node
        stack.extend(ast.iter_child_nodes(node))


def _grown_argvs(tree: ast.AST) -> tuple[list[list[ast.AST]], set[int]]:
    """argvs assigned as a literal and then grown in the same function."""
    argvs: list[list[ast.AST]] = []
    absorbed: set[int] = set()
    scopes = [
        tree,
        *(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)),
    ]
    for scope in scopes:
        events = []
        for node in _scope_nodes(scope):
            if isinstance(node, ast.Assign) and len(node.targets) == 1:
                if isinstance(node.targets[0], ast.Name):
                    events.append((node, "set", node.targets[0].id, node.value))
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                events.append((node, "set", node.target.id, node.value))
            elif isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name):
                if isinstance(node.op, ast.Add) and isinstance(node.value, ast.List | ast.Tuple):
                    events.append((node, "grow", node.target.id, [node.value]))
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and len(node.args) == 1
            ):
                name, arg = node.func.value.id, node.args[0]
                if node.func.attr == "extend" and isinstance(arg, ast.List | ast.Tuple):
                    events.append((node, "grow", name, [node, arg]))
                elif node.func.attr == "append":
                    events.append((node, "grow", name, [node]))
        events.sort(key=lambda event: (event[0].lineno, event[0].col_offset))
        live: dict[str, tuple[list[ast.AST], list[ast.AST], bool]] = {}
        done: list[tuple[list[ast.AST], list[ast.AST], bool]] = []
        for node, kind, name, payload in events:
            if kind == "set":
                if name in live:
                    done.append(live.pop(name))
                if isinstance(payload, ast.List | ast.Tuple):
                    live[name] = ([*payload.elts], [payload], False)
                continue
            if name not in live:
                continue
            elements, parts, _grown = live[name]
            if isinstance(payload[-1], ast.List | ast.Tuple):
                elements.extend(payload[-1].elts)
            else:
                elements.extend(node.args)
            live[name] = (elements, [*parts, *payload], True)
        for elements, parts, grown in [*done, *live.values()]:
            if grown:
                argvs.append(elements)
                absorbed.update(id(part) for part in parts)
    return argvs, absorbed


def _list_parts(node: ast.AST) -> list[ast.AST] | None:
    if isinstance(node, ast.List | ast.Tuple):
        return [node]
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _list_parts(node.left), _list_parts(node.right)
        if left is not None and right is not None:
            return left + right
    return None


def _concatenated_argvs(
    tree: ast.AST, parent: dict[ast.AST, ast.AST]
) -> tuple[list[list[ast.AST]], set[int]]:
    """argvs written as ``[...] + [...]``."""
    argvs: list[list[ast.AST]] = []
    absorbed: set[int] = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add)):
            continue
        parts = _list_parts(node)
        up = parent.get(node)
        if parts is None or (up is not None and _list_parts(up) is not None):
            continue  # not a list concatenation, or part of a longer one
        argvs.append([element for part in parts for element in part.elts])
        absorbed.update(id(part) for part in parts)
    return argvs, absorbed


def _issues_tmux_commands(tree: ast.AST) -> bool:
    return any(
        _str_const(node) in COMMANDS or _is_tmux_binary(node)
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
    )


def check_source(source: str, filename: str = "<src>") -> Report:
    """Classify every flag literal in ``source``; collect violations and exact sites."""
    tree = ast.parse(source, filename=filename)
    parent = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    report = Report(filename)

    grown, grown_parts = _grown_argvs(tree)
    joined, joined_parts = _concatenated_argvs(tree, parent)
    absorbed = grown_parts | joined_parts
    argvs = [*grown, *joined]
    for node in ast.walk(tree):
        if id(node) in absorbed:
            continue
        if isinstance(node, ast.List | ast.Tuple):
            argvs.append(node.elts)
        elif isinstance(node, ast.Call):
            argvs.append(node.args)
    for elements in argvs:
        _check_argv(list(elements), report)

    tmux_module = _issues_tmux_commands(tree)
    literals = [node for node in ast.walk(tree) if _is_flag_literal(node)]
    for literal in literals:
        if id(literal) in report.buckets:
            continue
        root = _argument_root(literal, parent)
        if root is None:
            report.classify(literal, "inside a longer string")
        elif _is_sequence_element(root, parent):
            continue  # the argv pass above must have classified it
        elif not tmux_module:
            report.classify(literal, "module issues no tmux commands")
        elif TARGET_FLAG.match(literal.value):
            report.violation(literal, "target flag outside an argv the guard can check")
        else:
            report.classify(literal, "not a flag")
    report.unclassified = [report.where(n) for n in literals if id(n) not in report.buckets]
    return report


@pytest.fixture(scope="module")
def src_reports() -> dict[str, Report]:
    reports = {}
    for path in sorted(SRC.rglob("*.py")):
        rel = str(path.relative_to(SRC))
        reports[rel] = check_source(path.read_text(encoding="utf-8"), rel)
    return reports


# -- the src scan ----------------------------------------------------------------


def test_every_src_tmux_target_uses_the_exact_helper(src_reports):
    violations = [v for report in src_reports.values() for v in report.violations]
    assert violations == []


def test_every_src_flag_literal_is_classified(src_reports):
    """No -t/-s literal in src/ goes unseen: each one is an exact target, a
    non-target flag, another program's argument, or text. A call shape the
    guard cannot read shows up here instead of silently passing."""
    unclassified = [where for report in src_reports.values() for where in report.unclassified]
    assert unclassified == []


def test_src_scan_sees_the_known_tmux_call_sites(src_reports):
    # Non-vacuity only: the number of sites is free to change.
    assert src_reports["pinky_daemon/tmux_session.py"].sites
    assert src_reports["pinky_daemon/tmux_dream_runner.py"].sites
    sites = [site for report in src_reports.values() for site in report.sites]
    assert any(" send-keys -t exact_pane_target" in site for site in sites)
    assert any(" kill-session -t exact_session_target" in site for site in sites)


# -- guard behaviour ---------------------------------------------------------------


BYPASS_SHAPES = [
    ('run("has-session", "-t", self.session_name)', "not built by an exact helper"),
    ('run("kill-session", "-t", "=" + name)', "not built by an exact helper"),
    ('run("send-keys", "-t", f"={name}:", "Enter")', "not built by an exact helper"),
    ('args = ["capture-pane", "-t", target, "-p"]', "not built by an exact helper"),
    ('run("send-keys", "-t", exact_session_target(n), "Enter")', "needs exact_pane_target"),
    ('run("resize-window", "-t", exact_session_target(n))', "needs exact_pane_target"),
    ('run("kill-session", "-t", exact_pane_target(n))', "needs exact_session_target"),
    ('args = ["send-keys"]; args += ["-t"]', "-t without an inline target"),
    ('run("send-keys", f"-t{name}")', "fused into the -t flag"),
    ('run("send-keys", "-t=pinky-x")', "fused into the -t flag"),
    ('run("send-keys", "-tpinky-x", "Enter")', "fused into the -t flag"),
    ('run("send-keys", "-t" + name, "Enter")', "fused into the -t flag"),
    ('run("send-keys", "-t%s" % name)', "fused into the -t flag"),
    ('run("send-keys", "-t{}".format(name))', "fused into the -t flag"),
    ('run(["tmux", "send-keys", f"-t{name}", "Enter"])', "fused into the -t flag"),
    ('run(["tmux", "send-keys", "-t", name, "Enter"])', "not built by an exact helper"),
    ('run([tmux, "-L", sock, "send-keys", "-t", name])', "not built by an exact helper"),
    ('flag = "-t"\nrun("send-keys", flag, name)', "outside an argv"),
    ('target = f"-t{name}"\nrun("send-keys", target)', "outside an argv"),
    ('run("swap-pane", "-t", name)', "not built by an exact helper"),
    ('run("swap-pane", "-s", name, "-t", exact_pane_target(n))', "not built by an exact helper"),
    (
        'run("join-pane", "-s", exact_session_target(a), "-t", exact_pane_target(b))',
        "needs exact_pane_target",
    ),
    (
        'run("kill-session", "-t", exact_session_target(a), ";",'
        ' "send-keys", "-t", exact_session_target(b))',
        "send-keys -t needs exact_pane_target",
    ),
    (
        'run("kill-session", "-t", exact_session_target(a), "Enter;", "send-keys", "-t", name)',
        "send-keys -t target is not built",
    ),
    ('run("capture-pane", "-pt", exact_pane_target(n))', "clustered"),
    ('run("send", "-t", name)', "not built by an exact helper"),
    ('run(["tmux", "send", "-t", name])', "not built by an exact helper"),
    ('run([tmux, "-L", sock, "send", "-t", name])', "not built by an exact helper"),
    ('run("send-k", "-t", name)', "not built by an exact helper"),
    ('args = ["send-keys"]\nargs.extend(["-t", name])', "not built by an exact helper"),
    ('args = ["send-keys"]\nargs += ["-t", name]', "not built by an exact helper"),
    ('args = ["send-keys"]\nargs.append("-t")\nargs.append(name)', "not built by an exact"),
    ('run(["send-keys"] + ["-t", name])', "not built by an exact helper"),
    ('args.extend(["-t", exact_pane_target(n)])', "cannot tell which tmux command"),
    ('run("list-sessions", "-t", exact_session_target(n))', "takes no -t target"),
    ('run("send-keys", "-t", exact_pane_target(n), "-l", text)', "must follow --"),
    ('run("send-keys", "-t", exact_pane_target(n), text, "Enter")', "must follow --"),
    ('run("send-keys", "-t", exact_pane_target(n), *keys)', "must follow --"),
    ('run("rename-session", "-t", exact_session_target(n), new_name)', "must follow --"),
]

CORRECT_SHAPES = [
    'run("kill-session", "-t", exact_session_target(self.session_name))',
    'run("has-session", "-t", tmux_targets.exact_session_target(name))',
    'args = ["capture-pane", "-t", exact_pane_target(t or name), "-p"]',
    'run("set-option", "-w", "-t", exact_pane_target(name), "remain-on-exit", "on")',
    'run("list-sessions", "-F", "#{session_name}")',
    'run(["tmux", "send-keys", "-t", exact_pane_target(n), "Enter"])',
    'run([tmux, "-L", sock, "send-keys", "-t", exact_pane_target(n), "Enter"])',
    'run(["-S", path, "kill-session", "-t", exact_session_target(n)])',
    'run(["tmux", "-S", path, "has-session", "-t", exact_session_target(n)])',
    'args = ["capture-pane", "-p"]\nargs.extend(["-t", exact_pane_target(n)])',
    'args = ["send-keys", "-t", exact_pane_target(n), "--", text]\nargs.append("Enter")',
    'run("send-keys", "-t", exact_pane_target(n), "-l", "--", text)',
    'run("send-keys", "-t", exact_pane_target(n), "--", "-t")',
    'run("send-keys", "-N", count, "-t", exact_pane_target(n), "Enter")',
    'run("rename-session", "-t", exact_session_target(n), "--", new_name)',
    'run("swap-pane", "-s", exact_pane_target(a), "-t", exact_pane_target(b))',
    'run("kill-session", "-t", exact_session_target(a), ";",'
    ' "send-keys", "-t", exact_pane_target(b), "Enter")',
    'run("new-session", "-d", "-s", name, "-c", cwd, command)',
    'run("paste-buffer", "-s", separator, "-t", exact_pane_target(n))',
    'run("load-buffer", "-t", client, "-")',
    'run(["podman", "exec", "-t", box, "tmux", "send-keys", "-t", exact_pane_target(n)])',
    'run("has-session", "-t", exact_session_target(n))\nrun(["logger", "-t", "tag", message])',
    'run("has-session", "-t", exact_session_target(n))\nrun(["podman", "exec", "-t", box, "sh"])',
    'run("has-session", "-t", exact_session_target(n))\nparser.add_argument("-t", "--tag")',
    'run("has-session", "-t", exact_session_target(n))\nlabel = f"{name}-tmux"',
]


@pytest.mark.parametrize(("snippet", "reason"), BYPASS_SHAPES)
def test_guard_flags_bare_indirect_and_mismatched_targets(snippet, reason):
    violations = check_source(snippet).violations
    assert any(reason in v.reason for v in violations), violations


@pytest.mark.parametrize("snippet", CORRECT_SHAPES)
def test_guard_accepts_correct_argv_shapes(snippet):
    report = check_source(snippet)
    assert report.violations == []
    assert report.unclassified == []


def test_guard_ignores_modules_that_issue_no_tmux_commands():
    # e.g. ``logger -t TAG`` and a stray ``-t`` in a module with no tmux commands.
    report = check_source('run(["logger", "-t", "tag", message])\nflag = "-t"')
    assert report.violations == []
    assert report.sites == []


# -- the exact helpers ------------------------------------------------------------


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


@pytest.mark.parametrize(
    ("text", "argument"),
    [
        ("plain", "plain"),
        ("-flag-like", "-flag-like"),
        ("a;b", "a;b"),
        (";", "\\;"),
        ("semi;", "semi\\;"),
        ("bs\\;", "bs\\\\;"),
    ],
)
def test_text_argument_escapes_only_a_trailing_separator(text, argument):
    assert text_argument(text) == argument
