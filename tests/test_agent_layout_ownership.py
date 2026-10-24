"""Keep the boot walk's directory ownership aligned with agent path builders."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from pinky_daemon import store_catalog


def _agent_path_components(source: str, *, writes_only: bool = False) -> set[str]:
    """Conservatively collect fixed children of agent roots, including read paths.

    Scan constructors rather than imports or runtime branches: disabled transports
    and MCP entry points still own their fixed paths. Track aliases within each
    function so a new builder cannot escape the check by naming its root `base`.
    Operator-supplied paths (templates, uploads and configured overrides) have no
    fixed first component; they remain agent content, not daemon layout claims.
    """
    tree = ast.parse(source)
    constants = {
        node.targets[0].id: node.value.value
        for node in tree.body
        if isinstance(node, ast.Assign) and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name) and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    }
    components = set()

    def literal(node):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.Name):
            return constants.get(node.id)
        return None

    class Paths(ast.NodeVisitor):
        def __init__(self, aliases=None):
            self.aliases = dict(aliases or {})
            self.aliases.update({name: () for name in (
                "working_dir", "work_dir", "agent_working_dir", "agent_dir",
            )})

        def value(self, node):
            if isinstance(node, ast.Name):
                return self.aliases.get(node.id)
            if isinstance(node, ast.Attribute):
                if node.attr in {"working_dir", "_working_dir"}:
                    return ()
                if node.attr == "parent":
                    base = self.value(node.value)
                    return base[:-1] if base else None
                return self.aliases.get(ast.unparse(node))
            if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Div, ast.Add)):
                base, child = self.value(node.left), literal(node.right)
                if base is not None and child:
                    return (*base, *child.strip("/").split("/"))
            if isinstance(node, ast.BoolOp):
                return next((value for item in node.values
                             if (value := self.value(item)) is not None), None)
            if isinstance(node, ast.IfExp):
                value = self.value(node.body)
                return value if value is not None else self.value(node.orelse)
            if isinstance(node, ast.JoinedStr) and node.values:
                first, *rest = node.values
                if isinstance(first, ast.FormattedValue):
                    base = self.value(first.value)
                    if base is not None and rest and (child := literal(rest[0])):
                        return (*base, *child.strip("/").split("/"))
            if not isinstance(node, ast.Call):
                return None
            name = ast.unparse(node.func).split(".")[-1]
            if name == "_agent_working_dir":
                return ()
            if (name == "getattr" and len(node.args) > 1
                    and literal(node.args[1]) == "working_dir"):
                return ()
            if name in {"resolve_agent_path", "_agent_path_or_400"}:
                offset = 2 if name == "resolve_agent_path" else 1
                children = [literal(item) for item in node.args[offset:]]
                if all(child is not None for child in children):
                    return tuple(part for child in children for part in child.split("/"))
            if name in {"Path", "str", "abspath", "realpath", "expanduser"} and node.args:
                return self.value(node.args[0])
            if isinstance(node.func, ast.Attribute):
                if name in {"resolve", "expanduser", "strip"}:
                    return self.value(node.func.value)
                if name == "joinpath":
                    base, children = self.value(node.func.value), node.args
                elif name == "join" and node.args:
                    base, children = self.value(node.args[0]), node.args[1:]
                else:
                    return None
                children = [literal(child) for child in children]
                if base is not None and all(child is not None for child in children):
                    return (*base, *(part for child in children for part in child.split("/")))
            return None

        def visit_FunctionDef(self, node):
            visitor = Paths(self.aliases)
            for statement in node.body:
                visitor.visit(statement)

        def visit_AsyncFunctionDef(self, node):
            self.visit_FunctionDef(node)

        def visit_Assign(self, node):
            value = self.value(node.value)
            if value is not None:
                for target in node.targets:
                    self.aliases[ast.unparse(target)] = value
            self.generic_visit(node)

        def visit_AnnAssign(self, node):
            value = self.value(node.value)
            if value is not None:
                self.aliases[ast.unparse(node.target)] = value
            self.generic_visit(node)

        def generic_visit(self, node):
            candidate = node
            if writes_only:
                candidate = None
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                    name = node.func.attr
                    if name in {"mkdir", "touch", "write_text", "write_bytes"}:
                        candidate = (node.args[0] if name == "mkdir" and node.args
                                     and ast.unparse(node.func.value) == "os" else node.func.value)
                    elif name in {"makedirs", "copy", "copy2", "copytree", "move", "replace"}:
                        offset = 1 if name in {"copy", "copy2", "copytree", "move", "replace"} else 0
                        if len(node.args) > offset:
                            candidate = node.args[offset]
                if isinstance(node, ast.Call) and ast.unparse(node.func).split(".")[-1] == "open":
                    # Include writable Path.open(), builtin open() and os.open().
                    path_open = (isinstance(node.func, ast.Attribute)
                                 and ast.unparse(node.func.value) != "os")
                    offset = 0 if path_open else 1
                    mode = literal(node.args[offset]) if len(node.args) > offset else "r"
                    mode = next((literal(kw.value) for kw in node.keywords if kw.arg == "mode"), mode)
                    flags = {part.attr for arg in node.args for part in ast.walk(arg)
                             if isinstance(part, ast.Attribute)}
                    if (mode and any(char in mode for char in "wax+")) \
                            or flags & {"O_WRONLY", "O_RDWR", "O_CREAT"}:
                        if path_open:
                            candidate = node.func.value
                        elif node.args:
                            candidate = node.args[0]
            if candidate is not None and isinstance(node, (ast.BinOp, ast.Call, ast.JoinedStr)):
                value = self.value(candidate)
                if value:
                    components.add(value[0])
            super().generic_visit(node)

    Paths().visit(tree)
    return components


def test_fixed_agent_paths_are_covered_by_boot_ownership():
    source_root = Path(__file__).resolve().parents[1] / "src"
    owned = getattr(store_catalog, "_DAEMON_AGENT_SUBDIRECTORIES", frozenset())
    # Exact root files are inspected without descending into a directory.
    root_files = {".mcp.json", "CLAUDE.md"}
    # These are discovered/read but never materialized by the daemon.
    read_only = {
        "pinky_daemon/skill_loader.py": {".pinky", ".agents", "skills"},
        "pinky_daemon/api.py": {".git"},
        # This module's agent_dir is a global review folder, not an agent root.
        "pinky_daemon/dream_runner.py": {"index.jsonl"},
    }
    missing = {}
    for source in sorted(source_root.rglob("*.py")):
        if source.parent.name == "pinky_cli":
            continue
        relative = source.relative_to(source_root).as_posix()
        children = _agent_path_components(source.read_text())
        uncovered = children - owned - root_files - read_only.get(relative, set())
        # A formerly read-only path becoming writable must also update ownership.
        writes = _agent_path_components(source.read_text(), writes_only=True)
        uncovered |= writes - owned - root_files - (
            {"index.jsonl"} if relative == "pinky_daemon/dream_runner.py" else set()
        )
        if uncovered:
            missing[relative] = sorted(uncovered)
    assert not missing, f"agent path builders missing boot ownership: {missing}"


@pytest.mark.parametrize("builder", [
    'Path(agent.working_dir) / "new-state"',
    'os.path.join(working_dir, "new-state")',
    'resolve_agent_path(name, work_dir, "new-state", "state.json")',
    'Path(working_dir).joinpath("new-state")',
    'f"{working_dir}/new-state/state.json"',
    'base / "new-state"',
])
def test_source_scan_catches_uncovered_agent_path_builders(builder):
    source = f'base = Path(agent.working_dir).resolve()\nstate = {builder}\nstate.mkdir()\n'
    assert "new-state" in _agent_path_components(source)
    assert "new-state" in _agent_path_components(source, writes_only=True)
