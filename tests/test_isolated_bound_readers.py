"""Every isolated content read uses its owned descriptor and discovered identity."""

import json
import os
from pathlib import Path

import httpx
import pytest

from pinky_daemon import isolated_files
from pinky_daemon.codex_home import codex_home_for
from pinky_daemon.codex_tmux_session import CodexTmuxSession
from pinky_daemon.streaming_session import StreamingSessionConfig
from pinky_daemon.tmux_session import _InflightMeta, _QueuedTurn
from pinky_daemon.tmux_transcript import TmuxTranscriptTailer, claude_project_slug
from tests.isolated_policy_support import closure, signed
from tests.isolated_policy_support import daemon as daemon
from tests.test_isolated_codex_descriptor import discovered
from tests.test_isolated_review_boundaries import replace_path

pytestmark = pytest.mark.real_auth


def rollout(path, cwd, label="fixture"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"type": "session_meta", "payload": {
        "id": label, "cwd": str(cwd),
    }}) + "\n" + json.dumps({"type": "event_msg", "payload": {
        "type": "user_message", "message": label,
    }}) + "\n")
    return path


@pytest.mark.asyncio
@pytest.mark.parametrize("per_agent", [False, True])
async def test_codex_same_path_new_inode_is_refused(daemon, monkeypatch, per_agent, capsys):
    d = daemon()
    own, root, tailer, entries = await discovered(d, monkeypatch, per_agent)
    identity = (own.stat().st_dev, own.stat().st_ino)
    replacement = rollout(own.with_name("replacement.jsonl"), d.root / "tenant", "new inode")
    replacement.replace(own)
    assert (own.stat().st_dev, own.stat().st_ino) != identity
    assert await tailer.read_once() == 0
    assert await tailer.read_once() == 0
    assert (tailer.offset, entries) == (0, [])
    assert capsys.readouterr().err.count("transcript ownership rejected") == 1


@pytest.mark.asyncio
async def test_codex_bound_inode_moved_outside_root_is_refused(daemon, monkeypatch):
    d = daemon()
    own, root, tailer, entries = await discovered(d, monkeypatch, True)
    moved = d.root / "peer" / "moved-rollouts"
    own.parent.rename(moved)
    own.parent.symlink_to(moved, target_is_directory=True)
    assert await tailer.read_once() == 0
    assert (tailer.offset, entries) == (0, [])


@pytest.mark.asyncio
@pytest.mark.parametrize("part", [".codex", "sessions"])
async def test_codex_discovery_does_not_resolve_writable_root_tail(daemon, monkeypatch, part):
    d = daemon()
    monkeypatch.setenv("PINKY_CODEX_PER_AGENT_HOME", "1")
    working_dir = d.root / "tenant"
    own_home = codex_home_for(d.agents.get("tenant"))
    peer_home = codex_home_for(d.agents.get("peer"))
    candidate = rollout(peer_home / "sessions" / "rollout-spoofed.jsonl", working_dir)
    own_home.mkdir()
    if part == ".codex":
        own_home.rmdir()
        own_home.symlink_to(peer_home, target_is_directory=True)
    else:
        (own_home / "sessions").symlink_to(peer_home / "sessions", target_is_directory=True)
    session = CodexTmuxSession(
        StreamingSessionConfig(agent_name="tenant", working_dir=str(working_dir)), registry=d.agents,
    )
    assert session._discover_transcript_path() is None
    assert not session._is_own_transcript(own_home / "sessions" / candidate.name)


@pytest.mark.asyncio
async def test_codex_self_heal_carries_new_discovered_identity(daemon, monkeypatch):
    d = daemon()
    own, root, tailer, entries = await discovered(d, monkeypatch, True)
    assert await tailer.read_once() == own.stat().st_size
    newer = rollout(own.with_name("rollout-newer.jsonl"), d.root / "tenant", "new binding")
    os.utime(own, (1, 1))
    os.utime(newer, (2, 2))
    tailer._try_self_heal_repoint()
    assert tailer.transcript_path == newer
    assert tailer.offset == 0
    assert await tailer.read_once() == newer.stat().st_size
    assert entries[-1]["payload"]["message"] == "new binding"
    replacement = rollout(newer.with_name("replacement.jsonl"), d.root / "tenant", "replacement")
    replacement.replace(newer)
    offset, previous = tailer.offset, list(entries)
    assert await tailer.read_once() == 0
    assert (tailer.offset, entries) == (offset, previous)


@pytest.mark.asyncio
async def test_codex_never_follows_final_link_to_bound_inode(daemon, monkeypatch):
    d = daemon()
    own, root, tailer, entries = await discovered(d, monkeypatch, True)
    parked = own.with_name("parked.jsonl")
    own.rename(parked)
    own.symlink_to(parked)
    assert await tailer.read_once() == 0
    assert (tailer.offset, entries) == (0, [])


async def claude(d):
    d.agents.update("tenant", runtime="claude_sdk", transport="tmux")
    session = await closure(d.app, "_prepare_streaming_session")("tenant")
    own = Path.home() / ".claude/projects" / claude_project_slug(d.root / "tenant") / "fixture.jsonl"
    own.parent.mkdir(parents=True)
    own.write_bytes(b"{}\n")
    session._tailer = TmuxTranscriptTailer(
        own, lambda turn: None, owned_projects=session._transcript_ownership,
    )
    return session, own


@pytest.mark.asyncio
async def test_claude_never_follows_final_link_within_owned_project(daemon):
    d = daemon()
    session, own = await claude(d)
    peer = own.with_name("other.jsonl")
    peer.write_bytes(b'{"type":"user","message":"same project fixture"}\n')
    own.unlink()
    own.symlink_to(peer)
    assert await session._tailer.read_once() == 0
    assert session._tailer.offset == 0
    assert session._capture_transcript_occurrence_ticket().anchor is None


def test_owned_open_never_follows_final_link_to_allowed_file(tmp_path):
    target = tmp_path / "target.jsonl"
    target.write_bytes(b"owned fixture")
    link = tmp_path / "link.jsonl"
    link.symlink_to(target)
    handle = isolated_files.open_owned_transcript(link, lambda path: path.parent == tmp_path)
    try:
        assert handle is None
    finally:
        if handle is not None:
            handle.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("swap", ["file", "directory"])
async def test_claude_reconciliation_never_reads_replaced_peer(daemon, monkeypatch, swap):
    d = daemon()
    session, own = await claude(d)
    ticket = session._capture_transcript_occurrence_ticket()
    assert ticket.anchor == b"{}\n"
    entry = _InflightMeta(
        meta={}, completion_event=None, internal=False, dispatched_at=0,
        turn=_QueuedTurn(prompt="fixture", platform="", chat_id="", message_id=""),
        transcript_path_at_paste=own, transcript_file_identity_at_paste=ticket.identity,
        transcript_offset_at_paste=ticket.offset, transcript_anchor_start_at_paste=ticket.anchor_start,
        transcript_anchor_at_paste=ticket.anchor, transcript_ticket_captured_at_ns=ticket.captured_at_ns,
    )
    peer = own.parent.with_name(own.parent.name + "-peer") / own.name
    peer.parent.mkdir()
    peer.write_bytes(b'{"type":"user","message":"foreign harmless fixture"}\n')
    replace_path(own, peer, swap)
    reads = []
    original = Path.open

    class Reader:
        def __init__(self, handle):
            self._wrapped = handle

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return self._wrapped.__exit__(*args)

        def __getattr__(self, name):
            return getattr(self._wrapped, name)

        def read(self, *args):
            data = self._wrapped.read(*args)
            reads.append(data)
            return data

    def opened(path, *args, **kwargs):
        handle = original(path, *args, **kwargs)
        return Reader(handle) if path == own else handle

    monkeypatch.setattr(Path, "open", opened)
    assert session._phantom_consumption_verdicts([entry]) == [None]
    assert reads == []


@pytest.mark.asyncio
@pytest.mark.parametrize("runtime", ["claude", "codex"])
async def test_missing_owned_transcript_does_not_consume_refusal_log(
    daemon, monkeypatch, runtime, capsys,
):
    d = daemon()
    if runtime == "claude":
        session, own = await claude(d)
        tailer = session._tailer
    else:
        own, root, tailer, entries = await discovered(d, monkeypatch, True)
    original = os.open

    def disappear(selected, *args, **kwargs):
        if not isinstance(selected, int) and Path(selected) == own:
            raise FileNotFoundError("fixture vanished between exists and open")
        return original(selected, *args, **kwargs)

    capsys.readouterr()
    with monkeypatch.context() as patch:
        patch.setattr(os, "open", disappear)
        assert await tailer.read_once() == 0
    assert capsys.readouterr().err.count("transcript ownership rejected") == 0
    assert tailer._ownership_rejection_logged is False
    parked = own.with_name("parked.jsonl")
    own.rename(parked)
    own.symlink_to(parked)
    assert await tailer.read_once() == 0
    assert await tailer.read_once() == 0
    assert tailer.offset == 0
    assert capsys.readouterr().err.count("transcript ownership rejected") == 1


@pytest.mark.asyncio
async def test_codex_owned_rebind_returns_false_and_logs_once_per_path(
    daemon, monkeypatch, capsys,
):
    d = daemon()
    own, root, tailer, entries = await discovered(d, monkeypatch, True)
    refused = rollout(own.with_name("rollout-foreign.jsonl"), d.root / "peer")
    other = rollout(own.with_name("rollout-other-foreign.jsonl"), d.root / "peer")
    original = (tailer.transcript_path, tailer.offset, tailer._owned_identity,
                tailer._swap_generation, tailer.bound_size)
    capsys.readouterr()
    for candidate in (refused, refused, other, other):
        assert tailer.set_transcript_path(candidate) is False
        assert (tailer.transcript_path, tailer.offset, tailer._owned_identity,
                tailer._swap_generation, tailer.bound_size) == original
    assert capsys.readouterr().err.count("transcript ownership rejected") == 2
    valid = rollout(own.with_name("rollout-valid.jsonl"), d.root / "tenant")
    assert tailer.set_transcript_path(valid) is not False
    assert tailer.transcript_path == valid
    assert tailer.offset == valid.stat().st_size
    assert tailer._owned_identity == (valid.stat().st_dev, valid.stat().st_ino)


async def codex_session(d, monkeypatch):
    own, root, tailer, entries = await discovered(d, monkeypatch, True)
    session = CodexTmuxSession(
        StreamingSessionConfig(agent_name="tenant", working_dir=str(d.root / "tenant")),
        registry=d.agents,
    )
    session._tailer = tailer
    session._tailer_first_bind_pending = True
    session._last_launch_used_continue = False
    session._bound_transcript_session_id = "fixture"
    session._session_ready_event.clear()
    return session, own


@pytest.mark.asyncio
async def test_codex_discovery_ignores_candidate_missing_at_owned_open(daemon, monkeypatch):
    d = daemon()
    session, own = await codex_session(d, monkeypatch)
    original = os.open

    def disappear(selected, *args, **kwargs):
        if not isinstance(selected, int) and Path(selected) == own:
            raise FileNotFoundError("fixture vanished before metadata discovery")
        return original(selected, *args, **kwargs)

    monkeypatch.setattr(os, "open", disappear)
    assert session._discover_transcript_path() is None
    assert session._is_own_transcript(own) is None


@pytest.mark.asyncio
async def test_codex_session_propagates_owned_rebind_refusal_without_consuming_window(
    daemon, monkeypatch,
):
    d = daemon()
    session, own = await codex_session(d, monkeypatch)
    refused = rollout(own.with_name("rollout-foreign.jsonl"), d.root / "peer")
    assert session.set_transcript_path(refused, session_id="new-fixture") is False
    assert session._tailer.transcript_path == own
    assert session._tailer_first_bind_pending is True
    assert session._bound_transcript_session_id == "fixture"
    assert not session._session_ready_event.is_set()
    valid = rollout(own.with_name("rollout-valid.jsonl"), d.root / "tenant")
    assert session.set_transcript_path(valid, session_id="new-fixture") is True
    assert session._tailer.transcript_path == valid
    assert session._tailer.offset == 0
    assert session._tailer_first_bind_pending is False
    assert session._bound_transcript_session_id == "new-fixture"
    assert session._session_ready_event.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["off", "shadow", "enforce"])
@pytest.mark.parametrize("owned", [False, True])
async def test_controller_route_reports_owned_codex_bind_result(
    daemon, monkeypatch, mode, owned,
):
    d = daemon(mode)
    session, own = await codex_session(d, monkeypatch)
    selected = rollout(own.with_name("rollout-selected.jsonl"), d.root / ("tenant" if owned else "peer"))
    d.app.state.broker.register_streaming("tenant", session, label="main")
    route = "/agents/tenant/transport/transcript-path"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=d.app), base_url="http://fixture",
    ) as client:
        response = await client.post(route, headers=signed(d, "POST", route, name="normal"), json={
            "transcript_path": str(selected), "session_id": "new-fixture", "label": "main",
        })
    assert response.status_code == (200 if owned else 409), response.text
    assert session._tailer.transcript_path == (selected if owned else own)
    if not owned:
        assert response.json()["detail"] == "transcript bind rejected"
        assert session._tailer_first_bind_pending is True
        assert session._bound_transcript_session_id == "fixture"
