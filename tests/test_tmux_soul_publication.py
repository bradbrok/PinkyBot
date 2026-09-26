"""Create-only prompt publication at the real tmux process boundary."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

from pinky_daemon import api, tmux_session
from pinky_daemon.codex_tmux_session import CodexTmuxSession
from pinky_daemon.tmux_session import TmuxSession
from pinky_daemon.transport_state import SessionState


@pytest.fixture
def launch(tmp_path, monkeypatch):
    # All implicit legacy paths are confined to this test's private root.
    monkeypatch.chdir(tmp_path)
    app = api.create_api(db_path=str(tmp_path / "stores" / "conversations.db"))
    registry = app.state.agents
    work = tmp_path / "workspace"
    work.mkdir()
    registry.register(
        "test-agent", working_dir=str(work), runtime="claude_sdk", transport="tmux",
        soul="Fixture soul text", boundaries="Fixture boundaries marker",
    )
    registry.add_directive("test-agent", "Fixture active directive")
    # Reuse the API's actual skill store through its route dependency.
    route = next(r for r in app.routes if getattr(r, "path", "") == "/agents/{name}/skills/{skill_name}"
                 and "POST" in getattr(r, "methods", set()))
    import inspect

    skills = inspect.getclosurevars(route.endpoint).nonlocals["skills"]
    skills.register("fixture-skill", directive="Fixture skill instructions")
    skills.assign_to_agent("test-agent", "fixture-skill")
    launched = []
    sessions = []

    async def connect(session):
        # Exercise the real spawn method and hook, replacing only process/I/O seams.
        control = MagicMock()
        control.has_session = AsyncMock(side_effect=[False, True])

        async def new_session(**kwargs):
            path = work / "CLAUDE.md"
            launched.append(path.read_bytes() if path.is_file() else None)
            return SimpleNamespace(ok=True, launch_env=None)

        control.new_session = AsyncMock(side_effect=new_session)
        session._tmux = control
        session._container_agent = lambda **kw: None
        session._select_command_runner = lambda *args: None
        session._ensure_container_started = AsyncMock()
        session._reap_retained_spawn_cleanup_debt = AsyncMock()
        session._build_repl_env = lambda **kw: {}
        session._build_claude_cmd = lambda: "test-command"
        session._seed_container_trust = AsyncMock()
        session._seed_container_home_creds = AsyncMock()
        session._transcript_candidates = lambda: []
        session._start_tailer = AsyncMock()
        await TmuxSession._spawn_tmux_repl(session)
        session._state_machine._state = SessionState.CONNECTED
        sessions.append(session)

    monkeypatch.setattr(TmuxSession, "connect", connect)
    monkeypatch.setattr(TmuxSession, "_select_command_runner", lambda *args: None)
    monkeypatch.setattr("pinky_daemon.provisioning.get_provisioner", lambda *args: object())
    monkeypatch.setattr(CodexTmuxSession, "connect", connect)
    monkeypatch.setattr(CodexTmuxSession, "_seed_codex_trust", lambda *args: None)
    monkeypatch.setattr(tmux_session, "_seed_claude_trust_file", lambda *args: False)
    monkeypatch.setattr(tmux_session, "_async_sleep", AsyncMock())
    client = TestClient(app)

    async def request():
        # Keep requests on the store-owning thread, as in the daemon event loop.
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver",
            cookies=client.cookies,
        ) as http:
            return await http.post("/agents/test-agent/streaming-sessions")

    def run():
        response = asyncio.run(request())
        assert response.status_code == 200, response.text
        assert len(launched) == 1

    yield SimpleNamespace(
        run=run, registry=registry, work=work, launched=launched, sessions=sessions,
        app=app, skills=skills,
    )
    client.close()
    app.state.store_catalog.close()


@pytest.mark.parametrize("isolation", ["local", "container"])
def test_missing_prompt_published_before_process(launch, caplog, isolation):
    launch.registry.update("test-agent", isolation_mode=isolation)
    with caplog.at_level(logging.INFO):
        launch.run()
    content = (launch.work / "CLAUDE.md").read_text()
    for marker in ("Fixture soul text", "Fixture boundaries marker", "Fixture active directive",
                   "fixture-skill", 'load_skill("name")'):
        assert marker in content
    assert launch.launched == [content.encode()]
    versions = launch.registry.get_soul_versions("test-agent")
    assert len(versions) == 1
    assert versions[0]["source"] == "spawn"
    assert launch.registry.get_soul_version("test-agent", versions[0]["id"])["content"] == content
    assert [r.message for r in caplog.records if "published missing CLAUDE.md" in r.message] == [
        f"published missing CLAUDE.md for test-agent ({len(content)} chars)"
    ]


def test_existing_prompt_is_byte_identical_without_version(launch, monkeypatch):
    path = launch.work / "CLAUDE.md"
    original = b"User-owned prompt\r\n\xff\x00"
    path.write_bytes(original)
    monkeypatch.setattr(
        launch.registry, "build_system_prompt", MagicMock(side_effect=AssertionError("must not compile"))
    )
    launch.run()
    assert path.read_bytes() == original
    assert launch.registry.get_soul_versions("test-agent") == []


def test_dangling_symlink_is_present(launch):
    path = launch.work / "CLAUDE.md"
    target = launch.work.parent / "absent-target"
    path.symlink_to(target)
    launch.run()
    assert path.is_symlink()
    assert path.readlink() == target
    assert not target.exists()
    assert launch.registry.get_soul_versions("test-agent") == []


def test_codex_publication_unchanged(launch):
    launch.registry.update("test-agent", runtime="codex_cli")
    launch.run()
    assert not (launch.work / "CLAUDE.md").exists()
    assert not (launch.work / "AGENTS.md").exists()
    assert launch.registry.get_soul_versions("test-agent") == []


def test_compile_failure_warns_once_and_launches(launch, monkeypatch, caplog):
    def fail(*args, **kwargs):
        raise RuntimeError("compile fixture failure")

    monkeypatch.setattr(launch.registry, "build_system_prompt", fail)
    with caplog.at_level(logging.WARNING):
        launch.run()
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "test-agent" in warnings[0].message
    assert "CLAUDE.md" in warnings[0].message
    assert launch.launched == [None]


def test_write_failure_warns_once_and_launches(launch, monkeypatch, caplog):
    def fail(*args, **kwargs):
        raise PermissionError("write fixture failure")

    monkeypatch.setattr(api, "replace_agent_text", fail)
    with caplog.at_level(logging.WARNING):
        launch.run()
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "test-agent" in warnings[0].message
    assert launch.launched == [None]
    assert launch.registry.get_soul_versions("test-agent") == []


def test_unix_user_skipped_with_warning(launch, caplog):
    launch.registry.update("test-agent", isolation_mode="unix_user")
    with caplog.at_level(logging.WARNING):
        launch.run()
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "test-agent" in warnings[0].message
    assert "unix_user" in warnings[0].message
    assert launch.launched == [None]


@pytest.mark.parametrize("symlink", [False, True])
def test_concurrent_creator_wins_without_overwrite(launch, monkeypatch, symlink):
    build = launch.registry.build_system_prompt
    target = launch.work.parent / "other-file"
    target.write_text("Other owner's text")
    path = launch.work / "CLAUDE.md"

    def racing_build(*args, **kwargs):
        content = build(*args, **kwargs)
        if symlink:
            path.symlink_to(target)
        else:
            path.write_text("Concurrent owner's text")
        return content

    monkeypatch.setattr(launch.registry, "build_system_prompt", racing_build)
    launch.run()
    assert path.read_text() == ("Other owner's text" if symlink else "Concurrent owner's text")
    assert path.is_symlink() == symlink
    assert target.read_text() == "Other owner's text"
    assert launch.registry.get_soul_versions("test-agent") == []
    assert not list(launch.work.glob(".CLAUDE.md.*"))


def test_publish_opens_no_sqlite_connection(launch, monkeypatch):
    prepare = TmuxSession._prepare_tmux_spawn
    checked = []

    def checked_prepare(session):
        with patch("sqlite3.connect", wraps=sqlite3.connect) as connect:
            prepare(session)
        checked.append(connect.call_count)

    monkeypatch.setattr(TmuxSession, "_prepare_tmux_spawn", checked_prepare)
    launch.run()
    assert (launch.work / "CLAUDE.md").is_file()
    assert checked == [0]


def test_retained_session_republishes_fresh_prompt_only_when_missing(launch):
    launch.run()
    session = launch.sessions[0]
    original = (launch.work / "CLAUDE.md").read_bytes()
    launch.registry.update("test-agent", soul="Updated fixture soul")
    versions_before = launch.registry.get_soul_versions("test-agent")
    asyncio.run(session.connect())
    assert launch.launched == [original, original]
    assert launch.registry.get_soul_versions("test-agent") == versions_before
    (launch.work / "CLAUDE.md").unlink()
    asyncio.run(session.connect())
    content = (launch.work / "CLAUDE.md").read_bytes()
    assert b"Updated fixture soul" in content
    assert launch.launched[-1] == content
    versions = launch.registry.get_soul_versions("test-agent")
    assert len(versions) == len(versions_before) + 1
    assert versions[0]["source"] == "spawn"
