"""Each caller-controlled link stays inside its trusted root before being followed."""

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from pinky_daemon import api
from pinky_daemon.tmux_transcript import claude_project_slug
from tests.isolated_policy_support import closure, replace_cell, signed
from tests.isolated_policy_support import daemon as daemon
from tests.test_isolated_transcript_containment import hook_path, post, record_session

pytestmark = pytest.mark.real_auth
DENIAL = {"detail": "file_path must be inside the caller's working directory"}


def controls(d, monkeypatch):
    effects, attempts, stopped = [], [], []

    def send(chat, file, **kwargs):
        path = Path(file)
        effects.append({"path": str(path), "bytes": path.read_bytes().decode()})
        return {"message_id": "fixture"}

    replace_cell(monkeypatch, closure(d.app, "_send_file_message"),
                 "_get_platform_adapter", lambda *args: SimpleNamespace(send_document=send))
    route = closure(d.app, "_broker_send_file_route")
    cells = dict(zip(route.__code__.co_freevars, route.__closure__))
    logger = cells["_outreach_attempt_log"].cell_contents

    def log(**values):
        attempts.append(values)
        logger(**values)

    replace_cell(monkeypatch, route, "_outreach_attempt_log", log)
    monkeypatch.setattr(cells["broker"].cell_contents, "_stop_typing",
                        lambda *args: stopped.append(args))
    return effects, attempts, stopped


def media(d, selected):
    route = "/broker/send-document"
    client = TestClient(d.app, raise_server_exceptions=False)
    try:
        return client.post(route, headers=signed(d, "POST", route), json={
            "agent_name": "tenant", "chat_id": "fixture", "file_path": str(selected),
        })
    finally:
        client.close()


def observe(tmp_path, name, value):
    directory = Path(os.environ.get("PATH_GUARD_OBSERVATIONS", str(tmp_path)))
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(json.dumps(value, indent=2) + "\n")


@pytest.mark.parametrize("mode", ["off", "shadow", "enforce"])
def test_outside_backlink_states_have_consistent_denial(daemon, monkeypatch, capsys, tmp_path, mode):
    d = daemon(mode)
    owned = d.root / "tenant" / "owned.txt"
    owned.write_bytes(b"ordinary owned fixture")
    outside = d.root / "peer" / "preexisting-host-link"
    selected = d.root / "tenant" / "selected-link"
    selected.symlink_to(outside)
    effects, attempts, stopped = controls(d, monkeypatch)
    observations = []
    for state in ("backlink-present", "missing", "directory", "regular-file"):
        if outside.is_symlink() or outside.is_file():
            outside.unlink()
        elif outside.is_dir():
            outside.rmdir()
        if state == "backlink-present":
            outside.symlink_to(owned)
        elif state == "directory":
            outside.mkdir()
        elif state == "regular-file":
            outside.write_bytes(b"ordinary outside fixture")
        capsys.readouterr()
        response = media(d, selected)
        observations.append({"state": state, "status": response.status_code,
                             "body": response.json(),
                             "outreach_lines": [line for line in capsys.readouterr().err.splitlines()
                                                if "outreach-attempt:" in line]})
    observe(tmp_path, f"backlink-{mode}.json", {"observations": observations, "effects": effects})
    assert [(row["status"], row["body"]) for row in observations] == [(403, DENIAL)] * 4
    assert effects == []
    assert all(row["outreach_lines"] == [] for row in observations)
    assert attempts == []


def test_filesystem_root_working_dir_keeps_owned_attachment_success(daemon, monkeypatch, tmp_path):
    d = daemon()
    own = d.root / "tenant" / "ordinary.txt"
    own.write_bytes(b"ordinary owned fixture")
    d.agents._db.execute("DELETE FROM agents WHERE name<>?", ("tenant",))
    d.agents._db.commit()
    assert d.agents.resolve_registration_workspace("tenant", "/") == Path("/")
    d.agents._db.execute("UPDATE agents SET working_dir=? WHERE name=?", ("/", "tenant"))
    d.agents._db.commit()
    effects, attempts, stopped = controls(d, monkeypatch)
    response = media(d, own)
    observe(tmp_path, "root-boundary.json", {"status": response.status_code, "effects": effects})
    assert response.status_code == 200, response.text
    assert len(effects) == 1 and effects[0]["bytes"] == "ordinary owned fixture"
    assert not Path(effects[0]["path"]).parent.exists()


def test_embedded_nul_path_error_is_rejected_without_server_error(daemon, monkeypatch, tmp_path):
    d = daemon()
    selected = str(d.root / "tenant" / "invalid") + "\x00suffix"
    effects, attempts, stopped = controls(d, monkeypatch)
    errors = []

    def wrap(original):
        def capture(path, *args, **kwargs):
            try:
                return original(path, *args, **kwargs)
            except Exception as error:
                if os.fsdecode(path) == selected:
                    errors.append(type(error).__name__)
                raise
        return capture

    monkeypatch.setattr(api.os.path, "realpath", wrap(api.os.path.realpath))
    # Validation can reject this unrepresentable path before any syscall.
    if hasattr(api, "_validate_path"):
        monkeypatch.setattr(api, "_validate_path", wrap(api._validate_path))
    response = media(d, selected)
    observe(tmp_path, "embedded-nul.json", {"status": response.status_code,
                                           "errors": errors, "effects": effects})
    assert errors == ["ValueError"]
    assert response.status_code == 400, response.text
    assert effects == []
    assert [attempt["outcome"] for attempt in attempts] == ["rejected"]
    assert stopped == [("tenant", "fixture")]


@pytest.mark.parametrize("mode", ["off", "shadow", "enforce"])
@pytest.mark.parametrize("caller", ["tenant", "normal"])
def test_outside_transcript_backlink_states_have_consistent_denial(
    daemon, monkeypatch, mode, caller,
):
    d = daemon(mode)
    own_dir = Path.home() / ".claude/projects" / claude_project_slug(d.root / caller)
    own_dir.mkdir(parents=True)
    owned = own_dir / "owned.jsonl"
    owned.write_text("{}\n")
    outside = d.root / "peer" / "preexisting-link"
    selected = own_dir / "selected.jsonl"
    selected.symlink_to(outside)
    bindings, ownership = record_session(d, monkeypatch, caller)
    results = []
    for state in ("backlink-present", "missing", "directory", "regular-file"):
        if outside.is_symlink() or outside.is_file():
            outside.unlink()
        elif outside.is_dir():
            outside.rmdir()
        if state == "backlink-present":
            outside.symlink_to(owned)
        elif state == "directory":
            outside.mkdir()
        elif state == "regular-file":
            outside.write_text("{}\n")
        response = post(d, selected, caller)
        results.append((response.status_code, response.json()))
    assert [status for status, body in results] == [403] * 4
    assert all(body == results[0][1] for status, body in results)
    assert (bindings, ownership) == ([], [])


@pytest.mark.parametrize("mode", ["off", "shadow", "enforce"])
@pytest.mark.parametrize("endpoint", ["media", "transcript"])
@pytest.mark.parametrize("outside_state", ["missing", "regular", "backlink"])
def test_inside_link_never_looks_up_its_outside_target(
    daemon, monkeypatch, mode, endpoint, outside_state,
):
    d = daemon(mode)
    root = d.root / "tenant"
    if endpoint == "transcript":
        root = Path.home() / ".claude/projects" / claude_project_slug(root)
        root.mkdir(parents=True)
    owned = root / "owned.jsonl"
    owned.write_text("{}\n")
    outside = d.root / "peer" / "outside-target"
    if outside_state == "regular":
        outside.write_text("{}\n")
    elif outside_state == "backlink":
        outside.symlink_to(owned)
    selected = root / "selected.jsonl"
    selected.symlink_to(outside)
    effects, attempts, stopped = controls(d, monkeypatch)
    bindings, ownership = record_session(d, monkeypatch, "tenant")
    calls = []
    outside_roots = {str(outside.parent), os.path.realpath(outside.parent)}

    def wrap(name, original):
        def tracked(path, *args, **kwargs):
            value = None if isinstance(path, int) else os.fsdecode(path)
            if value and any(value == root or value.startswith(root + os.sep)
                             for root in outside_roots):
                calls.append((name, value))
            return original(path, *args, **kwargs)
        return tracked

    monkeypatch.setattr(api.os, "lstat", wrap("lstat", api.os.lstat))
    monkeypatch.setattr(api.os, "stat", wrap("stat", api.os.stat))
    monkeypatch.setattr(api.os, "readlink", wrap("readlink", api.os.readlink))
    response = media(d, selected) if endpoint == "media" else post(d, selected)
    assert response.status_code == 403, response.text
    assert (effects, bindings, ownership) == ([], [], [])
    assert calls == []


@pytest.mark.parametrize("mode", ["off", "shadow", "enforce"])
@pytest.mark.parametrize("caller", ["tenant", "normal"])
@pytest.mark.parametrize("root_kind", ["shared", "local", "container"])
@pytest.mark.parametrize("target_form", ["registered", "resolved"])
def test_inside_absolute_link_target_accepts_both_hook_root_forms(
    daemon, monkeypatch, mode, caller, root_kind, target_form,
):
    d = daemon(mode)
    target = hook_path(d, monkeypatch, caller, root_kind, target_form, True)
    selected = target.parent / "inside-link.jsonl"
    selected.symlink_to(str(target))
    bindings, ownership = record_session(d, monkeypatch, caller)
    response = post(d, selected, caller)
    assert response.status_code == 200, response.text
    assert bindings == [(target.resolve(), "fixture-session")]
    if caller == "tenant":
        assert len(ownership) == 1 and target.resolve().parent in ownership[0]
    else:
        assert ownership == []


@pytest.mark.parametrize("mode", ["off", "shadow", "enforce"])
@pytest.mark.parametrize("endpoint", ["media", "transcript"])
@pytest.mark.parametrize("hops,status", [(40, 200), (41, 403)])
def test_inside_link_chain_has_a_forty_hop_limit(daemon, monkeypatch, mode, endpoint, hops, status):
    d = daemon(mode)
    root = d.root / "tenant"
    if endpoint == "transcript":
        root = Path.home() / ".claude/projects" / claude_project_slug(root)
        root.mkdir(parents=True)
    target = root / "owned.jsonl"
    target.write_text("{}\n")
    expected_target = Path(os.path.realpath(target))
    selected = target
    for number in range(hops):
        link = root / f"hop-{number}.jsonl"
        link.symlink_to(selected.name)
        selected = link
    effects, attempts, stopped = controls(d, monkeypatch)
    bindings, ownership = record_session(d, monkeypatch, "tenant")
    response = media(d, selected) if endpoint == "media" else post(d, selected)
    assert response.status_code == status, response.text
    if status == 403:
        assert (effects, bindings, ownership) == ([], [], [])
    elif endpoint == "media":
        assert len(effects) == 1 and effects[0]["bytes"] == "{}\n"
        assert not Path(effects[0]["path"]).parent.exists()
    else:
        assert bindings == [(expected_target, "fixture-session")]


def test_inside_link_loop_is_refused_within_a_bound(tmp_path):
    root = tmp_path.resolve()
    loop = root / "loop"
    loop.symlink_to(loop.name)
    code = (
        "import sys\nfrom pinky_daemon.api import _resolve_path_within\n"
        "value = _resolve_path_within(sys.argv[1], sys.argv[2], sys.argv[2])\n"
        "assert value is None, value\nprint('refused')\n"
    )
    try:
        result = subprocess.run(
            [sys.executable, "-c", code, str(loop), str(root)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=3,
        )
    except subprocess.TimeoutExpired:
        # subprocess.run kills and reaps its child before raising this exception.
        assert False, "the path walker did not refuse an inside loop within three seconds"
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "refused"


@pytest.mark.parametrize("endpoint,caller", [
    ("media", "tenant"), ("transcript", "tenant"), ("transcript", "normal"),
])
def test_final_resolved_path_is_checked_again(daemon, monkeypatch, endpoint, caller):
    d = daemon()
    root = d.root / "tenant"
    if endpoint == "transcript":
        root = Path.home() / ".claude/projects" / claude_project_slug(root)
        root.mkdir(parents=True)
    selected = root / "ordinary.jsonl"
    selected.write_text("{}\n")
    outside = d.root / "peer" / "outside.jsonl"
    outside.write_text("{}\n")
    selected_forms = {str(selected), os.path.realpath(selected)}
    expected_outside = os.path.realpath(outside)
    effects, attempts, stopped = controls(d, monkeypatch)
    bindings, ownership = record_session(d, monkeypatch, caller)
    original = api.os.path.realpath

    def changed(path, *args, **kwargs):
        if os.fsdecode(path) in selected_forms:
            return expected_outside
        return original(path, *args, **kwargs)

    monkeypatch.setattr(api.os.path, "realpath", changed)
    response = media(d, selected) if endpoint == "media" else post(d, selected, caller)
    assert response.status_code == 403, response.text
    if endpoint == "transcript":
        if caller == "tenant":
            assert response.json() == {
                "detail": "transcript_path must be in the caller's own project directory",
            }
        else:
            assert response.json()["detail"].startswith("transcript_path must be under one of ")
    assert (effects, bindings, ownership) == ([], [], [])
