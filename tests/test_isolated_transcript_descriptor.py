"""Transcript ownership is checked on the file opened after a hook bind."""

import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pinky_daemon.streaming_session import StreamingSessionConfig
from pinky_daemon.tmux_session import TmuxSession
from pinky_daemon.tmux_transcript import TmuxTranscriptTailer, claude_project_slug
from tests.isolated_policy_support import closure, signed
from tests.isolated_policy_support import daemon as daemon

pytestmark = pytest.mark.real_auth


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["off", "shadow", "enforce"])
@pytest.mark.parametrize("config", ["shared", "container", "local"])
@pytest.mark.parametrize("swap", ["file", "directory"])
async def test_bound_transcript_replacement_cannot_read_peer(
    daemon, mode, config, swap, capsys,
):
    d = daemon(mode)
    wd = d.root / "tenant"
    if config == "shared":
        projects = Path.home() / ".claude/projects"
    else:
        d.agents.update("tenant", isolation_mode="container" if config == "container" else "local",
                        dedicated_config_dir=config == "local")
        projects = wd / f".claude-{config}/projects"
    own_dir = projects / claude_project_slug(wd)
    peer_dir = projects / claude_project_slug(d.root / "peer")
    own_dir.mkdir(parents=True)
    peer_dir.mkdir(parents=True)
    own = own_dir / "fixture.jsonl"
    peer = peer_dir / "fixture.jsonl"
    own.write_text("{}\n")
    peer.write_text("{}\n" + json.dumps({"type": "user", "message": {
        "role": "user", "content": "foreign harmless fixture"}}) + "\n")
    entries = []
    session = TmuxSession(StreamingSessionConfig(agent_name="tenant", working_dir=str(wd)))
    tailer = TmuxTranscriptTailer(own, lambda turn: None,
                                  on_entry=lambda entry, *args: entries.append(entry))
    session._tailer = tailer
    session._tailer_first_bind_pending = True
    d.app.state.broker.register_streaming("tenant", session, label="main")
    route = "/agents/tenant/transport/transcript-path"
    client = TestClient(d.app)
    try:
        response = client.post(route, headers=signed(d, "POST", route), json={
            "transcript_path": str(own), "session_id": "fixture", "label": "main"})
    finally:
        client.close()
    assert response.status_code == 200, response.text
    tailer.set_offset(3)
    if swap == "directory":
        own_dir.rename(own_dir.with_name(own_dir.name + "-parked"))
        own_dir.symlink_to(peer_dir, target_is_directory=True)
    else:
        staged = own_dir / "replacement"
        staged.symlink_to(peer)
        os.replace(staged, own)
    assert await tailer.read_once() == 0
    assert await tailer.read_once() == 0
    assert (tailer.offset, entries) == (3, [])
    assert capsys.readouterr().err.count("transcript ownership rejected") == 1


@pytest.mark.parametrize("mode", ["off", "shadow", "enforce"])
def test_nonlocal_dedicated_config_has_no_local_project_root(daemon, mode):
    d = daemon(mode)
    d.agents.update("tenant", isolation_mode="unix_user", dedicated_config_dir=True)
    forms = closure(d.app, "_claude_transcript_root_forms")(d.agents.get("tenant"))
    roots = [Path(resolved) for registered, resolved in forms]
    assert roots == [(Path.home() / ".claude/projects").resolve()]


@pytest.mark.asyncio
@pytest.mark.parametrize("caller", ["tenant", "normal"])
async def test_initial_discovery_checks_ownership_before_any_hook(daemon, monkeypatch, caller):
    d = daemon()
    d.agents.update(caller, runtime="claude_sdk", transport="tmux")
    session = await closure(d.app, "_prepare_streaming_session")(caller)
    assert isinstance(session, TmuxSession)
    project = Path.home() / ".claude/projects"
    own = project / claude_project_slug(d.root / caller) / "fixture.jsonl"
    peer = project / claude_project_slug(d.root / "peer") / "fixture.jsonl"
    for path in (own, peer):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}\n")
    monkeypatch.setattr(session, "_discover_transcript_path", lambda: own)

    async def defer_background_start(self):
        pass

    monkeypatch.setattr(TmuxTranscriptTailer, "start", defer_background_start)
    await session._start_tailer()
    try:
        tailer = session._tailer
        tailer.set_offset(0)
        entries = []
        tailer._on_entry = lambda entry, *args: entries.append(entry)
        own.unlink()
        own.symlink_to(peer)
        consumed = await tailer.read_once()
        if caller == "tenant":
            assert (consumed, tailer.offset, entries) == (0, 0, [])
        else:
            assert (consumed, tailer.offset, entries) == (3, 3, [{}])
    finally:
        await session._stop_tailer()


@pytest.mark.asyncio
@pytest.mark.parametrize("config", ["shared", "container", "local"])
async def test_owned_growing_transcript_still_dispatches(daemon, config):
    d = daemon()
    wd = d.root / "tenant"
    projects = Path.home() / ".claude/projects"
    if config != "shared":
        d.agents.update("tenant", isolation_mode="container" if config == "container" else "local",
                        dedicated_config_dir=config == "local")
        projects = wd / f".claude-{config}/projects"
    own = projects / claude_project_slug(wd) / "fixture.jsonl"
    own.parent.mkdir(parents=True)
    own.write_text("")
    entries = []
    session = TmuxSession(StreamingSessionConfig(agent_name="tenant", working_dir=str(wd)))
    session._tailer = TmuxTranscriptTailer(own, lambda turn: None,
                                          on_entry=lambda entry, *args: entries.append(entry))
    session._tailer_first_bind_pending = True
    d.app.state.broker.register_streaming("tenant", session, label="main")
    route = "/agents/tenant/transport/transcript-path"
    client = TestClient(d.app)
    try:
        response = client.post(route, headers=signed(d, "POST", route), json={
            "transcript_path": str(own), "session_id": "fixture", "label": "main"})
    finally:
        client.close()
    assert response.status_code == 200, response.text
    entry = {"type": "user", "message": {"role": "user", "content": "owned fixture"}}
    with own.open("a") as file:
        file.write(json.dumps(entry) + "\n")
    assert await session._tailer.read_once() == own.stat().st_size
    assert session._tailer.offset == own.stat().st_size
    assert entries == [entry]
