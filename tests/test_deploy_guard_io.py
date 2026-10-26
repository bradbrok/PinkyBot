"""Unusable state files cannot block startup or expose their contents."""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.test_deploy_guard import guard_module

STATE_PATHS = [
    "deploy.json",
    "starts.jsonl",
    "last_failure.json",
    "incident.json",
    "alert-route.json",
    ".guard.lock",
    ".guard-fixture-temp",
    "guard.off",
]
MARKER = "fixture-state-content-marker"

CHILD = r"""
import json, sys
from pathlib import Path
from types import SimpleNamespace
import pinky_daemon
from pinky_daemon import deploy_guard as g
print("TREE_IMPORT=" + pinky_daemon.__file__, flush=True)
root, entry, fifo = Path(sys.argv[1]), sys.argv[2], sys.argv[3] == "fifo"
state = root / "state"
if entry == ".guard-fixture-temp":
    g.uuid.uuid4 = lambda: SimpleNamespace(hex="fixture-temp")
    if hasattr(g, "tempfile"):
        g.tempfile._get_candidate_names = lambda: iter(["fixture-temp"])
ctx = g.begin_start(
    str(root / "release"), state, clock=lambda: 1000.0,
    sleep=lambda seconds: None, host="fixture-node", log_path="fixture.log",
    runner=lambda *args, **kwargs: (("b" * 40) + "\n").encode(),
    sender=lambda directory, text: True,
)
class Response:
    def __enter__(self):
        return self
    def __exit__(self, *args):
        pass
    def read(self, size):
        return b'{"ok":true}'
try:
    ctx.activate()
    ctx.failed(RuntimeError("fixture startup error"))
    sent = g.send_alert(state, "fixture alert", opener=lambda *args, **kwargs: Response())
    assert sent is not (fifo and entry == "alert-route.json")
    print("CONTINUED", flush=True)
finally:
    ctx.close()
"""


@pytest.mark.parametrize("entry", STATE_PATHS)
@pytest.mark.parametrize("fifo", [False, True], ids=["regular", "fifo"])
def test_each_state_path_is_bounded_and_type_checked(tmp_path, entry, fifo):
    g = guard_module()
    root = tmp_path / "fixture"
    package = root / "release/src/pinky_daemon"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "deploy_guard.py").write_bytes(Path(g.__file__).read_bytes())
    state = root / "state"
    state.mkdir(mode=0o700)
    payloads = {
        "deploy.json": dict(schema=2),
        "last_failure.json": dict(ts=900.0, pid=-1, exc_type="RuntimeError", detail=MARKER),
        "incident.json": dict(open=True, started_at=990.0, last_alert_at=1000.0, failed_starts=5),
        "alert-route.json": dict(
            schema=1,
            written_at=0.0,
            routes=[
                dict(
                    platform="telegram",
                    account_id="fixture-account",
                    conversation_id="fixture-chat",
                    token=MARKER,
                ),
            ],
        ),
    }
    for name, value in payloads.items():
        (state / name).write_text(json.dumps(value))
        (state / name).chmod(0o600)
    (state / ".guard.lock").touch(mode=0o600)
    (state / "starts.jsonl").write_text(
        "".join(json.dumps(dict(ts=990.0 + n, pid=-n - 1, ref="a" * 40)) + "\n" for n in range(5))
    )
    (state / "starts.jsonl").chmod(0o600)
    if entry == "guard.off":
        (state / entry).write_text(MARKER)
        (state / entry).chmod(0o644)
    if fifo:
        (state / entry).unlink(missing_ok=True)
        os.mkfifo(state / entry, 0o600)
    env = dict(os.environ, PYTHONPATH=str(package.parent), PYTHONDONTWRITEBYTECODE="1")
    command = [sys.executable, "-B", "-c", CHILD, str(root), entry, "fifo" if fifo else "regular"]
    result = subprocess.run(
        command, cwd=root / "release", env=env, text=True, capture_output=True, timeout=5
    )
    assert result.returncode == 0, result.stderr
    assert f"TREE_IMPORT={package / '__init__.py'}" in result.stdout
    assert "CONTINUED" in result.stdout
    assert MARKER not in result.stdout + result.stderr
    lines = [line for line in result.stderr.splitlines() if "ERROR deploy guard:" in line]
    assert len(lines) == int(fifo), result.stderr
    if fifo:
        assert "fifo" in lines[0].lower()
        if entry == "last_failure.json":
            assert "ENXIO" in lines[0]


def test_all_state_opens_share_one_validating_helper():
    g = guard_module()
    tree = ast.parse(Path(g.__file__).read_text())
    helpers = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_open_state"
    ]
    assert len(helpers) == 1
    helper = helpers[0]
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    opens = []
    for call in ast.walk(tree):
        if not isinstance(call, ast.Call):
            continue
        name = (
            call.func.id
            if isinstance(call.func, ast.Name)
            else (call.func.attr if isinstance(call.func, ast.Attribute) else "")
        )
        if name not in {"open", "fdopen", "mkstemp"}:
            continue
        opens.append(call)
        cursor = call
        while cursor in parents and cursor is not helper:
            cursor = parents[cursor]
        assert cursor is helper, "guard state opens must use the shared helper"
    assert len(opens) == 1
    attributes = {node.attr for node in ast.walk(helper) if isinstance(node, ast.Attribute)}
    constants = {node.value for node in ast.walk(helper) if isinstance(node, ast.Constant)}
    assert {"O_NOFOLLOW", "O_NONBLOCK", "fstat", "S_ISREG"} <= attributes
    assert "O_CLOEXEC" in attributes | constants
    calls = [node for node in ast.walk(helper) if isinstance(node, ast.Call)]
    fstat = next(node for node in calls if getattr(node.func, "attr", None) == "fstat")
    regular = next(node for node in calls if getattr(node.func, "attr", None) == "S_ISREG")
    yields = [node for node in ast.walk(helper) if isinstance(node, ast.Yield)]
    descriptor_yield = next(
        node for node in yields if isinstance(node.value, ast.Name)
    )
    assert opens[0].lineno < fstat.lineno < regular.lineno < descriptor_yield.lineno
    assert not any(
        getattr(call.func, "attr", None) in {"read", "write", "ftruncate"}
        and call.lineno < regular.lineno
        for call in calls
    )
