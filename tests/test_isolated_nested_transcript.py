"""Nested configured roots and isolated own-project walks, from MZ928 L1/L2/I2."""

import hashlib
import os
from pathlib import Path

import pytest
from fastapi import HTTPException

from pinky_daemon import api
from pinky_daemon.codex_home import codex_home_for
from pinky_daemon.tmux_transcript import claude_project_slug
from tests.isolated_policy_support import daemon as daemon
from tests.test_isolated_path_walk import controls
from tests.test_isolated_transcript_containment import post, record_session

pytestmark = pytest.mark.real_auth
OWN_DENIAL = {"detail": "transcript_path must be in the caller's own project directory"}


def owned_root(d):
    root = Path.home() / ".claude/projects" / claude_project_slug(
        d.agents.get("tenant").working_dir,
    )
    root.mkdir(parents=True, exist_ok=True)
    return root


def filesystem_recorder(monkeypatch, roots):
    """Watch both macOS spellings, including intermediate-directory lookups."""
    watched = {form for root in roots for form in (
        os.path.normpath(os.path.abspath(root)), os.path.realpath(root),
    )}
    calls = []
    for owner, name in (
        (api.os, "lstat"), (api.os, "stat"), (api.os, "readlink"),
        (api.os.path, "realpath"), (api.Path, "resolve"), (api.Path, "is_file"),
    ):
        original = getattr(owner, name)

        def record(path, *args, _original=original, _name=name, **kwargs):
            if not isinstance(path, int):
                value = os.path.normpath(os.fsdecode(path))
                if any(value == root or value.startswith(root + os.sep) for root in watched):
                    calls.append((_name, value))
            return _original(path, *args, **kwargs)

        monkeypatch.setattr(owner, name, record)
    return calls


def nested_path(d, monkeypatch, kind, caller):
    """Adapt the reviewer's four configs; keep raw HOME and working-dir forms."""
    projects = Path.home() / ".claude/projects"
    projects.mkdir(parents=True, exist_ok=True)
    case_id = hashlib.sha256(str(d.root).encode()).hexdigest()[:12]
    alias = projects / f"nested-{kind}-{case_id}"
    if "codex" in kind:
        real = d.root / caller / "codex-home"
        real.mkdir()
        alias.symlink_to(real, target_is_directory=True)
        if kind == "shared-codex":
            monkeypatch.delenv("PINKY_CODEX_PER_AGENT_HOME", raising=False)
            monkeypatch.setenv("CODEX_HOME", str(alias))
        else:
            monkeypatch.setenv("PINKY_CODEX_PER_AGENT_HOME", "1")
            d.agents.update(caller, codex_home=str(alias))
        selected = alias / "sessions/2026/10/07/new.jsonl"
    else:
        alias.symlink_to(d.root / caller, target_is_directory=True)
        d.agents._db.execute(
            "UPDATE agents SET working_dir=? WHERE name=?", (str(alias), caller),
        )
        d.agents._db.commit()
        mode = "local" if kind == "local-claude" else "container"
        d.agents.update(caller, isolation_mode=mode, dedicated_config_dir=mode == "local")
        selected = alias / f".claude-{mode}/projects" / claude_project_slug(alias) / "new.jsonl"
    selected.parent.mkdir(parents=True, exist_ok=True)
    selected.write_text("{}\n")
    return selected


@pytest.mark.parametrize("form", ["registered", "canonical"])
@pytest.mark.parametrize("kind", [
    "shared-codex", "override-codex", "local-claude", "container-claude",
])
def test_nested_registered_root_keeps_valid_session_start_binding(
    daemon, monkeypatch, kind, form,
):
    d = daemon()
    selected = nested_path(d, monkeypatch, kind, "normal")
    canonical = selected.resolve()
    bindings, ownership = record_session(d, monkeypatch, "normal")
    response = post(d, selected if form == "registered" else canonical, "normal")
    assert response.status_code == 200, response.text
    assert response.json()["transcript_path"] == str(canonical)
    assert bindings == [(canonical, "fixture-session")]
    assert ownership == []


@pytest.mark.parametrize("form", ["registered", "canonical"])
@pytest.mark.parametrize("kind", ["local-claude", "container-claude"])
def test_isolated_nested_claude_root_keeps_own_binding(daemon, monkeypatch, kind, form):
    d = daemon()
    selected = nested_path(d, monkeypatch, kind, "tenant")
    canonical = selected.resolve()
    bindings, ownership = record_session(d, monkeypatch, "tenant")
    response = post(d, selected if form == "registered" else canonical)
    assert response.status_code == 200, response.text
    assert bindings == [(canonical, "fixture-session")]
    assert len(ownership) == 1 and canonical.parent in ownership[0]


def test_outer_root_link_leaving_every_root_has_no_outside_lookup(daemon, monkeypatch):
    d = daemon()
    own = owned_root(d)
    outside = d.root / "peer" / "outside.jsonl"
    outside.write_text("{}\n")
    selected = own / "selected.jsonl"
    selected.symlink_to(outside)
    bindings, ownership = record_session(d, monkeypatch, "normal")
    calls = filesystem_recorder(monkeypatch, [outside.parent])
    response = post(d, selected, "normal")
    assert response.status_code == 403, response.text
    assert response.json()["detail"].startswith("transcript_path must be under one of ")
    assert (bindings, ownership, calls) == ([], [], [])


@pytest.mark.parametrize("mode", ["off", "shadow", "enforce"])
@pytest.mark.parametrize("scope", ["peer-claude-project", "shared-codex"])
def test_peer_scope_permission_error_keeps_own_project_refusal(
    daemon, monkeypatch, capsys, mode, scope,
):
    d = daemon(mode)
    own = owned_root(d)
    peer = (
        own.parent / claude_project_slug(d.root / "peer")
        if scope == "peer-claude-project"
        else codex_home_for(d.agents.get("tenant")) / "sessions/peer-scope"
    )
    peer.mkdir(parents=True, exist_ok=True)
    selected = peer / "selected.jsonl"
    selected.write_text("{}\n")
    bindings, ownership = record_session(d, monkeypatch, "tenant")
    effects, attempts, stopped = controls(d, monkeypatch)
    calls = filesystem_recorder(monkeypatch, [peer])
    ordinary = post(d, selected)
    peer.chmod(0o000)
    try:
        capsys.readouterr()
        response = post(d, selected)
        log = capsys.readouterr().err
    finally:
        peer.chmod(0o700)
    assert ordinary.status_code == 403, ordinary.text
    assert response.status_code == 403, response.text
    assert response.json() == OWN_DENIAL
    assert (bindings, ownership, effects, attempts, stopped) == ([], [], [], [], [])
    assert log == ""
    assert calls == []


@pytest.mark.parametrize("scope", ["peer-claude-project", "shared-codex"])
@pytest.mark.parametrize("exists", [False, True])
def test_isolated_peer_candidate_is_refused_before_any_lookup(
    daemon, monkeypatch, scope, exists,
):
    d = daemon()
    own = owned_root(d)
    peer = (
        own.parent / claude_project_slug(d.root / "peer")
        if scope == "peer-claude-project"
        else codex_home_for(d.agents.get("tenant")) / "sessions/peer-scope"
    )
    peer.mkdir(parents=True, exist_ok=True)
    selected = peer / "selected.jsonl"
    if exists:
        selected.write_text("{}\n")
    bindings, ownership = record_session(d, monkeypatch, "tenant")
    calls = filesystem_recorder(monkeypatch, [peer])
    response = post(d, selected)
    assert (response.status_code, response.json()) == (403, OWN_DENIAL)
    assert (bindings, ownership, calls) == ([], [], [])


@pytest.mark.parametrize("nested", [False, True])
def test_own_link_to_peer_is_refused_without_peer_lookup(daemon, monkeypatch, nested):
    d = daemon()
    if nested:
        selected = nested_path(d, monkeypatch, "local-claude", "tenant")
        selected.unlink()
        own = selected.parent
    else:
        own = owned_root(d)
        selected = own / "selected.jsonl"
    peer = own.parent / "fixture-peer-project"
    peer.mkdir()
    target = peer / "peer.jsonl"
    target.write_text("{}\n")
    selected.symlink_to(target)
    bindings, ownership = record_session(d, monkeypatch, "tenant")
    calls = filesystem_recorder(monkeypatch, [peer])
    response = post(d, selected)
    assert (response.status_code, response.json()) == (403, OWN_DENIAL)
    assert (bindings, ownership, calls) == ([], [], [])


@pytest.mark.parametrize("exists", [False, True])
def test_own_link_to_own_file_keeps_canonical_binding(daemon, monkeypatch, exists):
    d = daemon()
    own = owned_root(d)
    target = own / "target.jsonl"
    if exists:
        target.write_text("{}\n")
    selected = own / "selected.jsonl"
    selected.symlink_to(target.name)
    canonical = target.resolve()
    bindings, ownership = record_session(d, monkeypatch, "tenant")
    response = post(d, selected)
    assert response.status_code == 200, response.text
    assert bindings == [(canonical, "fixture-session")]
    assert len(ownership) == 1 and canonical.parent in ownership[0]


@pytest.mark.parametrize("error", [OSError, RuntimeError, ValueError])
def test_own_project_lookup_errors_are_bad_requests(daemon, monkeypatch, error):
    d = daemon()
    selected = owned_root(d) / "new.jsonl"
    wd = d.agents.get("tenant").working_dir
    bindings, ownership = record_session(d, monkeypatch, "tenant")
    effects, attempts, stopped = controls(d, monkeypatch)
    original, hits = api.Path.resolve, []

    def fail(path, *args, **kwargs):
        if os.fsdecode(path) == wd:
            hits.append(str(path))
            raise error("owned fixture own-project lookup failure")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(api.Path, "resolve", fail)
    response = post(d, selected)
    assert hits == [wd]
    assert response.status_code == 400, response.text
    assert response.json()["detail"].startswith("transcript_path could not be resolved: ")
    assert (bindings, ownership, effects, attempts, stopped) == ([], [], [], [], [])


def test_own_project_http_exception_keeps_its_status(daemon, monkeypatch):
    d = daemon()
    selected = owned_root(d) / "new.jsonl"
    wd = d.agents.get("tenant").working_dir
    bindings, ownership = record_session(d, monkeypatch, "tenant")
    original = api.Path.resolve

    def fail(path, *args, **kwargs):
        if os.fsdecode(path) == wd:
            raise HTTPException(409, "fixture configured refusal")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(api.Path, "resolve", fail)
    response = post(d, selected)
    assert (response.status_code, response.json()) == (409, {"detail": "fixture configured refusal"})
    assert (bindings, ownership) == ([], [])
