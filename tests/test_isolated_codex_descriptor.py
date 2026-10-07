"""A discovered rollout stays bound to the file validated for that session."""

import json

import pytest

from pinky_daemon.codex_home import codex_home_for
from pinky_daemon.codex_tmux_session import CodexTmuxSession
from pinky_daemon.streaming_session import StreamingSessionConfig
from pinky_daemon.tmux_session import TmuxSession
from tests.isolated_policy_support import daemon as daemon

pytestmark = pytest.mark.real_auth


async def discovered(d, monkeypatch, per_agent, caller="tenant"):
    monkeypatch.setenv("PINKY_CODEX_PER_AGENT_HOME", "1" if per_agent else "0")
    config = StreamingSessionConfig(agent_name=caller, working_dir=str(d.root / caller))
    root = codex_home_for(d.agents.get(caller)) / "sessions"
    if d.agents.get(caller).isolated:
        root = root.resolve()
    own = root / d.root.parent.name / "rollout-fixture.jsonl"
    own.parent.mkdir(parents=True)
    own.write_text(json.dumps({"type": "session_meta", "payload": {
        "id": "fixture", "cwd": config.working_dir}}) + "\n")
    session = CodexTmuxSession(config, registry=d.agents)
    assert session._discover_transcript_path() == own

    async def defer_background_start(self):
        pass

    # Exercise real Codex construction without starting a polling/recovery task.
    monkeypatch.setattr(TmuxSession, "_start_tailer", defer_background_start)
    await session._start_tailer()
    tailer = session._tailer
    entries = []
    tailer._on_entry = entries.append
    tailer.set_offset(0)
    return own, root, tailer, entries


@pytest.mark.asyncio
@pytest.mark.parametrize("per_agent", [False, True])
@pytest.mark.parametrize("swap", ["file", "directory"])
async def test_discovered_rollout_replacement_cannot_read_peer(
    daemon, monkeypatch, per_agent, swap, capsys,
):
    d = daemon()
    own, root, tailer, entries = await discovered(d, monkeypatch, per_agent)
    peer = own.parent.with_name(own.parent.name + "-peer") / own.name
    peer.parent.mkdir()
    peer.write_text(json.dumps({"type": "session_meta", "payload": {
        "id": "peer-fixture", "cwd": str(d.root / "peer")}}) + "\n" + json.dumps({
        "type": "event_msg", "payload": {
            "type": "user_message", "message": "foreign harmless fixture"}}) + "\n")
    if swap == "file":
        own.unlink()
        own.symlink_to(peer)
    else:
        own.parent.rename(own.parent.with_name(own.parent.name + "-parked"))
        own.parent.symlink_to(peer.parent, target_is_directory=True)
    assert await tailer.read_once() == 0
    assert await tailer.read_once() == 0
    assert (tailer.offset, entries) == (0, [])
    assert capsys.readouterr().err.count("transcript ownership rejected") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("per_agent", [False, True])
@pytest.mark.parametrize("caller", ["tenant", "normal"])
async def test_growing_rollout_still_dispatches_and_advances_offset(
    daemon, monkeypatch, per_agent, caller,
):
    d = daemon()
    own, root, tailer, entries = await discovered(d, monkeypatch, per_agent, caller)
    before = own.stat().st_size
    assert await tailer.read_once() == before
    assert tailer.offset == before
    assert len(entries) == 1
    entry = {"type": "event_msg", "payload": {
        "type": "user_message", "message": "owned growing fixture"}}
    with own.open("a") as file:
        file.write(json.dumps(entry) + "\n")
    assert await tailer.read_once() == own.stat().st_size - before
    assert tailer.offset == own.stat().st_size
    assert entries == [entries[0], entry]
    assert await tailer.read_once() == 0
