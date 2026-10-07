"""Transcript hooks check caller-path containment before filesystem lookup."""

import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from pinky_daemon import api
from pinky_daemon.tmux_transcript import claude_project_slug
from tests.isolated_policy_support import daemon as daemon
from tests.isolated_policy_support import signed

pytestmark = pytest.mark.real_auth


def record_session(d, monkeypatch, caller, accepted=None):
    bindings, ownership = [], []

    def bind(path, *, session_id):
        bindings.append((path, session_id))
        return accepted

    session = SimpleNamespace(
        set_transcript_path=bind, set_transcript_ownership=ownership.append,
    )
    monkeypatch.setattr(
        d.app.state.broker, "get_streaming_session", lambda *args, **kwargs: session,
    )
    return bindings, ownership


def post(d, selected, caller="tenant"):
    route = f"/agents/{caller}/transport/transcript-path"
    # A nonisolated controller also covers targets whose container mode implies isolation.
    auth_caller = caller if caller == "tenant" else "dreamer"
    client = TestClient(d.app, raise_server_exceptions=False)
    try:
        return client.post(route, headers=signed(d, "POST", route, auth_caller), json={
            "transcript_path": str(selected), "session_id": "fixture-session", "label": "main",
        })
    finally:
        client.close()


@pytest.mark.parametrize("mode", ["off", "shadow", "enforce"])
@pytest.mark.parametrize("caller", ["tenant", "normal"])
@pytest.mark.parametrize("form", ["absolute", "symlink", "traversal", "sibling-prefix"])
def test_outside_transcript_existence_and_loops_are_not_observable(
    daemon, monkeypatch, mode, caller, form,
):
    d = daemon(mode)
    projects = Path.home() / ".claude/projects"
    own = projects / claude_project_slug(d.root / caller)
    own.mkdir(parents=True)
    outside = d.root / "peer"
    if form == "sibling-prefix":
        outside = projects.with_name("projects2") / claude_project_slug(d.root / caller)
        outside.mkdir(parents=True)
    targets = [outside / "present.jsonl", outside / "missing.jsonl", outside / "loop.jsonl"]
    targets[0].write_text("{}\n")
    targets[2].symlink_to(targets[2].name)
    if form == "symlink":
        selected = [own / f"link-{i}.jsonl" for i in range(3)]
        for link, target in zip(selected, targets):
            link.symlink_to(target)
    elif form == "traversal":
        selected = [Path(os.path.join(str(own), os.path.relpath(target, own)))
                    for target in targets]
    else:
        selected = targets
    bindings, ownership = record_session(d, monkeypatch, caller)
    calls = []
    if caller == "tenant" and form == "symlink":
        from tests.test_isolated_nested_transcript import filesystem_recorder

        calls = filesystem_recorder(monkeypatch, [outside])
    responses = [post(d, path, caller) for path in selected]
    assert [r.status_code for r in responses] == [403, 403, 403]
    assert responses[0].json() == responses[1].json() == responses[2].json()
    if caller == "tenant" and form == "symlink":
        assert responses[0].json() == {
            "detail": "transcript_path must be in the caller's own project directory",
        }
        assert calls == []
    else:
        assert responses[0].json()["detail"].startswith("transcript_path must be under one of ")
    assert (bindings, ownership) == ([], [])


@pytest.mark.parametrize("mode", ["off", "shadow", "enforce"])
@pytest.mark.parametrize("caller", ["tenant", "normal"])
@pytest.mark.parametrize("form", ["absolute", "traversal"])
def test_lexically_outside_transcript_has_no_filesystem_lookup(
    daemon, monkeypatch, mode, caller, form,
):
    d = daemon(mode)
    selected = d.root / "peer" / "outside.jsonl"
    selected.write_text("{}\n")
    projects = Path.home() / ".claude/projects"
    incoming = selected if form == "absolute" else Path(
        os.path.join(str(projects), os.path.relpath(selected, projects)),
    )
    bindings, ownership = record_session(d, monkeypatch, caller)
    calls = []
    untrusted = {str(selected), str(incoming)}
    untrusted |= {os.path.normpath(value) for value in untrusted}
    untrusted |= {os.path.realpath(value) for value in untrusted}

    def wrap(name, original):
        def tracked(path, *args, **kwargs):
            if not isinstance(path, int) and os.fsdecode(path) in untrusted:
                calls.append((name, os.fsdecode(path)))
            return original(path, *args, **kwargs)
        return tracked

    monkeypatch.setattr(api.os.path, "realpath", wrap("realpath", api.os.path.realpath))
    monkeypatch.setattr(api.os, "stat", wrap("stat", api.os.stat))
    monkeypatch.setattr(api.os, "lstat", wrap("lstat", api.os.lstat))
    monkeypatch.setattr(api.Path, "resolve", wrap("resolve", api.Path.resolve))
    response = post(d, incoming, caller)
    assert response.status_code == 403, response.text
    assert (bindings, ownership) == ([], [])
    assert calls == []


@pytest.mark.parametrize("mode", ["off", "shadow", "enforce"])
@pytest.mark.parametrize("caller", ["tenant", "normal"])
@pytest.mark.parametrize("location", ["inside", "outside"])
def test_transcript_nul_is_a_bad_request_without_binding(
    daemon, monkeypatch, mode, caller, location,
):
    d = daemon(mode)
    root = Path.home() / ".claude/projects" if location == "inside" else d.root / "peer"
    incoming = str(root / "bad") + "\x00.jsonl"
    bindings, ownership = record_session(d, monkeypatch, caller)
    response = post(d, incoming, caller)
    assert response.status_code == 400, response.text
    assert response.json()["detail"].startswith("transcript_path could not be resolved: ")
    assert "null" in response.json()["detail"].lower()
    assert (bindings, ownership) == ([], [])


def hook_path(d, monkeypatch, caller, root_kind, form, exists):
    real_home = d.root.parent / "hook-home"
    real_home.mkdir()
    home_alias = d.root.parent / "home-alias"
    home_alias.symlink_to(real_home, target_is_directory=True)
    monkeypatch.setenv("HOME", str(home_alias))
    work_alias = d.root.parent / "work-alias"
    work_alias.symlink_to(d.root, target_is_directory=True)
    registered_work = work_alias / caller
    d.agents._db.execute(
        "UPDATE agents SET working_dir=? WHERE name=?", (str(registered_work), caller),
    )
    d.agents._db.commit()
    if root_kind == "shared":
        root = home_alias / ".claude/projects"
    elif root_kind in ("local", "container"):
        d.agents.update(
            caller, isolation_mode=root_kind, dedicated_config_dir=root_kind == "local",
        )
        root = registered_work / f".claude-{root_kind}/projects"
    elif root_kind == "per-agent-codex":
        monkeypatch.setenv("PINKY_CODEX_PER_AGENT_HOME", "1")
        root = registered_work / ".codex/sessions"
    else:
        monkeypatch.delenv("PINKY_CODEX_PER_AGENT_HOME", raising=False)
        monkeypatch.setenv("CODEX_HOME", str(home_alias / ".codex"))
        root = home_alias / ".codex/sessions"
    if "codex" in root_kind:
        selected = root / "2026/10/07/rollout.jsonl"
    else:
        cwd = registered_work if form == "registered" else registered_work.resolve()
        selected = root / claude_project_slug(cwd) / "session.jsonl"
    selected.parent.mkdir(parents=True)
    if exists:
        selected.write_text("{}\n")
    return selected if form == "registered" else selected.resolve()


@pytest.mark.parametrize("mode", ["off", "shadow", "enforce"])
@pytest.mark.parametrize("caller", ["tenant", "normal"])
@pytest.mark.parametrize("root_kind", ["shared", "local", "container"])
@pytest.mark.parametrize("form", ["registered", "resolved"])
@pytest.mark.parametrize("exists", [False, True])
def test_claude_hook_forms_keep_binding_and_missing_file_support(
    daemon, monkeypatch, mode, caller, root_kind, form, exists,
):
    d = daemon(mode)
    selected = hook_path(d, monkeypatch, caller, root_kind, form, exists)
    bindings, ownership = record_session(d, monkeypatch, caller)
    response = post(d, selected, caller)
    expected = selected.resolve()
    assert response.status_code == 200, response.text
    assert response.json()["transcript_path"] == str(expected)
    assert bindings == [(expected, "fixture-session")]
    if caller == "tenant":
        assert len(ownership) == 1 and expected.parent in ownership[0]
    else:
        assert ownership == []
    assert expected.exists() is exists


@pytest.mark.parametrize("mode", ["off", "shadow", "enforce"])
@pytest.mark.parametrize("root_kind", ["per-agent-codex", "shared-codex"])
@pytest.mark.parametrize("form", ["registered", "resolved"])
@pytest.mark.parametrize("exists", [False, True])
def test_nonisolated_codex_hook_forms_keep_binding(
    daemon, monkeypatch, mode, root_kind, form, exists,
):
    d = daemon(mode)
    selected = hook_path(d, monkeypatch, "normal", root_kind, form, exists)
    bindings, ownership = record_session(d, monkeypatch, "normal")
    response = post(d, selected, "normal")
    assert response.status_code == 200, response.text
    assert bindings == [(selected.resolve(), "fixture-session")]
    assert ownership == []


@pytest.mark.parametrize("mode", ["off", "shadow", "enforce"])
@pytest.mark.parametrize("root_kind", ["per-agent-codex", "shared-codex"])
@pytest.mark.parametrize("form", ["registered", "resolved"])
def test_isolated_codex_hook_keeps_own_project_refusal(
    daemon, monkeypatch, mode, root_kind, form,
):
    d = daemon(mode)
    selected = hook_path(d, monkeypatch, "tenant", root_kind, form, False)
    bindings, ownership = record_session(d, monkeypatch, "tenant")
    response = post(d, selected)
    assert response.status_code == 403, response.text
    assert response.json() == {
        "detail": "transcript_path must be in the caller's own project directory",
    }
    assert (bindings, ownership) == ([], [])


@pytest.mark.parametrize("mode", ["off", "shadow", "enforce"])
@pytest.mark.parametrize("root_kind", ["shared", "local", "container"])
def test_isolated_peer_project_stays_denied(daemon, monkeypatch, mode, root_kind):
    d = daemon(mode)
    own = hook_path(d, monkeypatch, "tenant", root_kind, "registered", False)
    peer = own.parent.parent / claude_project_slug(d.root / "peer") / "peer.jsonl"
    bindings, ownership = record_session(d, monkeypatch, "tenant")
    response = post(d, peer)
    assert response.status_code == 403, response.text
    assert response.json() == {
        "detail": "transcript_path must be in the caller's own project directory",
    }
    assert (bindings, ownership) == ([], [])


@pytest.mark.parametrize("caller", ["tenant", "normal"])
def test_owned_bind_refusal_keeps_409(daemon, monkeypatch, caller):
    d = daemon()
    selected = Path.home() / ".claude/projects" / claude_project_slug(d.root / caller) / "fresh"
    bindings, ownership = record_session(d, monkeypatch, caller, accepted=False)
    response = post(d, selected, caller)
    assert response.status_code == 409, response.text
    assert response.json() == {"detail": "transcript bind rejected"}
    assert bindings == [(selected.resolve(), "fixture-session")]


def test_relative_transcript_keeps_400(daemon, monkeypatch):
    d = daemon()
    bindings, ownership = record_session(d, monkeypatch, "tenant")
    response = post(d, "relative.jsonl")
    assert (response.status_code, response.json(), bindings, ownership) == (
        400, {"detail": "transcript_path must be absolute"}, [], [],
    )
