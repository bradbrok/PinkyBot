"""Own project aliases cannot create new trust roots for hooks or tailers."""

import os
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from pinky_daemon.tmux_transcript import claude_project_slug
from tests.isolated_policy_support import closure, replace_cell, signed
from tests.isolated_policy_support import daemon as daemon
from tests.test_isolated_nested_transcript import filesystem_recorder
from tests.test_isolated_path_walk import controls, observe
from tests.test_isolated_transcript_containment import record_session

pytestmark = pytest.mark.real_auth
OWN_DENIAL = {"detail": "transcript_path must be in the caller's own project directory"}


def configure(d, monkeypatch, kind, *, aliases=False):
    home = d.root / "project-home"
    home.mkdir()
    workdir = d.root / "tenant"
    if aliases:
        registered_home = d.root / "project-home-alias"
        registered_home.symlink_to(home, target_is_directory=True)
        registered_workdir = d.root / "project-work-alias"
        registered_workdir.symlink_to(workdir, target_is_directory=True)
    else:
        registered_home, registered_workdir = home, workdir
    monkeypatch.setenv("HOME", str(registered_home))
    d.agents._db.execute("UPDATE agents SET working_dir=? WHERE name='tenant'",
                         (str(registered_workdir),))
    d.agents._db.commit()
    if kind in ("local", "container"):
        d.agents.update("tenant", isolation_mode=kind, dedicated_config_dir=kind == "local")
    root = (registered_home / ".claude/projects" if kind == "shared" else
            registered_workdir / f".claude-{kind}/projects")
    return root, registered_workdir


def post(d, selected, caller):
    route = "/agents/tenant/transport/transcript-path"
    client = TestClient(d.app, raise_server_exceptions=False)
    try:
        return client.post(route, headers=signed(d, "POST", route, caller), json={
            "transcript_path": str(selected), "session_id": "fixture-session", "label": "main",
        })
    finally:
        client.close()


async def prepared_ownership(d, monkeypatch):
    prepare = closure(d.app, "_prepare_streaming_session")
    ownership = []

    class UnconnectedSession:
        def __init__(self, config, **kwargs):
            self._config = config

        def set_transcript_ownership(self, projects):
            ownership.append(set(projects))

    d.agents.update("tenant", runtime="claude_sdk", transport="tmux")
    replace_cell(monkeypatch, prepare, "_expected_session_class", lambda agent: UnconnectedSession)
    replace_cell(monkeypatch, prepare, "_enforce_isolation_runnable", lambda name: None)
    replace_cell(monkeypatch, prepare, "_effective_launch_model", lambda agent: ("", "", "fixture"))
    replace_cell(monkeypatch, prepare, "_launch_fingerprint", lambda agent: {})
    replace_cell(monkeypatch, prepare, "_build_streaming_wake_context", lambda *a, **k: "")
    replace_cell(monkeypatch, prepare, "_make_streaming_response_callback", AsyncMock(return_value=None))
    replace_cell(monkeypatch, prepare, "_make_streaming_event_callback", AsyncMock(return_value=None))
    monkeypatch.setattr(d.agents, "ensure_workspace_hooks", lambda name: None)
    session = await prepare("tenant")
    assert isinstance(session, UnconnectedSession)
    assert len(ownership) == 1
    return ownership[0]


@pytest.mark.parametrize("kind", ["shared", "local", "container"])
@pytest.mark.parametrize("mode", ["off", "shadow", "enforce"])
@pytest.mark.parametrize("spelling", ["registered", "canonical"])
@pytest.mark.parametrize("caller", ["tenant", "dreamer"])
def test_escaping_project_alias_never_binds_or_looks_past_link(
    daemon, monkeypatch, kind, mode, spelling, caller,
):
    d = daemon(mode)
    root, wd = configure(d, monkeypatch, kind)
    root.mkdir(parents=True)
    outside = d.root / "peer" / "outside-project"
    outside.mkdir()
    target = outside / "session.jsonl"
    target.write_text("outside fixture; no reader installed\n")
    own = root / claude_project_slug(wd)
    own.symlink_to(outside, target_is_directory=True)
    selected = own / target.name if spelling == "registered" else target
    bindings, ownership = record_session(d, monkeypatch, "tenant")
    effects, attempts, stopped = controls(d, monkeypatch)
    calls = filesystem_recorder(monkeypatch, [outside])
    response = post(d, selected, caller)
    assert response.status_code == 403, response.text
    if caller == "tenant" and spelling == "registered":
        assert response.json() == OWN_DENIAL
    else:
        assert response.json()["detail"].startswith("transcript_path must be under one of ")
    assert calls == [], "an escaping own-project alias must be refused before target lookup"
    assert bindings == ownership == effects == attempts == stopped == []


@pytest.mark.parametrize("kind", ["shared", "local", "container"])
@pytest.mark.parametrize("mode", ["off", "shadow", "enforce"])
async def test_escaping_project_alias_is_not_given_to_tailer(daemon, monkeypatch, kind, mode):
    d = daemon(mode)
    root, wd = configure(d, monkeypatch, kind)
    root.mkdir(parents=True)
    outside = d.root / "peer" / "outside-project"
    outside.mkdir()
    outside_canonical = outside.resolve()
    (root / claude_project_slug(wd)).symlink_to(outside, target_is_directory=True)
    calls = filesystem_recorder(monkeypatch, [outside])
    projects = await prepared_ownership(d, monkeypatch)
    observe(d.root, f"tailer-escape-{kind}-{mode}.json", {
        "configured_root": os.fsdecode(root), "outside": os.fsdecode(outside_canonical),
        "ownership": sorted(map(os.fsdecode, projects)), "outside_calls": calls,
    })
    assert outside_canonical not in projects, "tailer must never own an unconfigured promoted root"
    assert calls == [], "ownership derivation must refuse the link before target lookup"


@pytest.mark.parametrize("kind", ["shared", "local", "container"])
@pytest.mark.parametrize("mode", ["off", "shadow", "enforce"])
@pytest.mark.parametrize("spelling", ["registered-wd", "canonical-wd"])
@pytest.mark.parametrize("caller", ["tenant", "dreamer"])
async def test_same_root_relative_slug_link_keeps_binding_and_tailer_ownership(
    daemon, monkeypatch, kind, mode, spelling, caller,
):
    d = daemon(mode)
    root, wd = configure(d, monkeypatch, kind, aliases=True)
    registered = root / claude_project_slug(wd)
    canonical = root / claude_project_slug(wd.resolve())
    assert registered != canonical
    registered.mkdir(parents=True)
    canonical.symlink_to("./" + registered.name, target_is_directory=True)
    target = registered / "session.jsonl"
    target.write_text("same-root NEW slug layout\n")
    selected = (registered if spelling == "registered-wd" else canonical) / target.name
    expected_path = target.resolve()
    agent = d.agents.get("tenant")
    root_forms = closure(d.app, "_claude_transcript_root_forms")(agent)
    roots = [Path(resolved) for registered_form, resolved in root_forms]
    expected_projects = {(r / claude_project_slug(workdir)).resolve()
                         for r in roots for workdir in (agent.working_dir, wd.resolve())}
    projects = closure(d.app, "_claude_owned_projects")(agent)
    forms = closure(d.app, "_claude_owned_project_forms")(agent)
    assert projects == expected_projects, "legitimate tailer ownership must retain its exact set"
    assert {Path(resolved) for registered_form, resolved in forms} == projects
    bindings, ownership = record_session(d, monkeypatch, "tenant")
    effects, attempts, stopped = controls(d, monkeypatch)
    response = post(d, selected, caller)
    assert response.status_code == 200, response.text
    assert response.json()["transcript_path"] == str(expected_path)
    assert bindings == [(expected_path, "fixture-session")]
    assert ownership == ([expected_projects] if caller == "tenant" else [])
    assert effects == attempts == stopped == []
    tailer_projects = await prepared_ownership(d, monkeypatch)
    observe(d.root, f"tailer-new-{kind}-{mode}-{spelling}-{caller}.json", {
        "configured_root": os.fsdecode(root), "input": os.fsdecode(selected),
        "relative_link": "./" + registered.name,
        "expected_ownership": sorted(map(os.fsdecode, expected_projects)),
        "ownership": sorted(map(os.fsdecode, tailer_projects)),
    })
    assert tailer_projects == expected_projects
