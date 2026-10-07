"""Attachment validation runs in a bounded child without ending daemon locks."""

import ast
import asyncio
import builtins
import os
import sqlite3
import sys
import time
from pathlib import Path

import httpx
import pytest

from pinky_daemon import isolated_files
from pinky_identity import live_sqlite
from tests.isolated_file_support import before_child, child_hook
from tests.isolated_policy_support import daemon as daemon
from tests.isolated_policy_support import signed
from tests.test_isolated_media_snapshot import adapter, post

pytestmark = pytest.mark.real_auth


async def async_post(d, path):
    route = "/broker/send-document"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=d.app), base_url="http://fixture",
    ) as client:
        return await client.post(route, headers=signed(d, "POST", route), json={
            "agent_name": "tenant", "chat_id": "fixture", "file_path": str(path),
        })


async def ticking(request):
    ticks = []

    async def tick():
        while True:
            ticks.append(time.monotonic())
            await asyncio.sleep(0.002)

    task = asyncio.create_task(tick())
    await asyncio.sleep(0)
    try:
        return await request, ticks
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


@pytest.mark.asyncio
@pytest.mark.timeout(2)
async def test_named_pipe_is_rejected_as_nonregular_without_stalling_loop(daemon, monkeypatch):
    d = daemon()
    own = d.root / "tenant" / "fixture.fifo"
    os.mkfifo(own)
    assert own.stat().st_nlink == 1
    monkeypatch.setattr(isolated_files, "MEDIA_COPY_TIMEOUT_SEC", 0.25, raising=False)
    effects = []
    adapter(d, monkeypatch, lambda *args, **kwargs: effects.append(args))
    started = time.monotonic()
    response, ticks = await ticking(async_post(d, own))
    assert (response.status_code, effects) == (400, []), response.text
    assert "regular file" in response.text, response.text
    assert time.monotonic() - started < 1
    assert len(ticks) > 2
    assert not list((d.root / "tmp").glob("media-*"))


def test_caller_file_is_never_opened_in_daemon_process(daemon, monkeypatch):
    d = daemon()
    own = d.root / "tenant" / "fixture.txt"
    own.write_bytes(b"owned fixture")
    seen = []
    original_os_open, original_open = os.open, builtins.open

    def record_os_open(path, *args, **kwargs):
        if not isinstance(path, int) and Path(path) == own:
            seen.append("os.open")
        return original_os_open(path, *args, **kwargs)

    def record_open(path, *args, **kwargs):
        if not isinstance(path, int) and Path(path) == own:
            seen.append("open")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", record_os_open)
    monkeypatch.setattr(builtins, "open", record_open)
    adapter(d, monkeypatch, lambda *args, **kwargs: {"message_id": "fixture"})
    response = post(d, own)
    assert response.status_code == 200, response.text
    assert seen == []


def test_standalone_copier_imports_only_stdlib():
    path = Path(isolated_files.__file__).with_name("isolated_media_copy.py")
    assert path.is_file()
    imports = []
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            imports.extend(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imports.append(node.module.split(".")[0])
    assert imports and set(imports) <= sys.stdlib_module_names
    assert not any(name.startswith("pinky_") for name in imports)


def test_sqlite_is_refused_before_child_launch(daemon, monkeypatch):
    d = daemon()
    own = d.root / "tenant" / "fixture.db"
    own.write_bytes(b"synthetic database name")
    started = []
    before_child(monkeypatch, lambda payload: started.append(payload))
    effects = []
    adapter(d, monkeypatch, lambda *args, **kwargs: effects.append(args))
    response = post(d, own)
    assert (response.status_code, started, effects) == (400, [], []), response.text


def test_child_rejects_live_inode_replaced_after_precheck(daemon, monkeypatch):
    d = daemon()
    own = d.root / "tenant" / "fixture.txt"
    own.write_bytes(b"ordinary owned fixture")
    store = own.with_name("active-store.data")
    connection = sqlite3.connect(store, factory=live_sqlite.LiveSQLiteConnection)
    live_sqlite.track_sqlite_connection(connection, store)
    connection.execute("CREATE TABLE fixture(value TEXT)").close()
    connection.commit()
    connection.execute("BEGIN IMMEDIATE").close()
    identities = []

    def swap(payload):
        info = store.stat()
        assert (info.st_dev, info.st_ino) in {tuple(pair) for pair in payload["live_sqlite"]}
        identities.append((info.st_dev, info.st_ino))
        store.replace(own)

    before_child(monkeypatch, swap)
    effects = []
    adapter(d, monkeypatch, lambda *args, **kwargs: effects.append(args))
    try:
        response = post(d, own)
        assert identities
        assert (response.status_code, effects) == (400, []), response.text
        assert "live SQLite inode" in response.text
    finally:
        connection.rollback()
        connection.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("finish", ["timeout", "cancel"])
async def test_slow_child_keeps_loop_live_and_is_reaped_with_snapshot_removed(
    daemon, monkeypatch, tmp_path, finish,
):
    d = daemon()
    own = d.root / "tenant" / "fixture.txt"
    own.write_bytes(b"owned fixture")
    slow = tmp_path / "slow_copy.py"
    slow.write_text("import time\ntime.sleep(60)\n")
    monkeypatch.setattr(isolated_files, "_COPY_CHILD", slow, raising=False)
    monkeypatch.setattr(isolated_files, "MEDIA_COPY_TIMEOUT_SEC", 0.15, raising=False)
    processes = []
    spawned = asyncio.Event()
    original = asyncio.create_subprocess_exec

    async def create(*args, **kwargs):
        assert args[:3] == (sys.executable, "-I", str(slow))
        process = await original(*args, **kwargs)
        processes.append(process)
        spawned.set()
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    effects = []
    adapter(d, monkeypatch, lambda *args, **kwargs: effects.append(args))
    if finish == "timeout":
        response, ticks = await ticking(async_post(d, own))
        assert response.status_code == 400, response.text
        assert "timed out" in response.text
        assert len(ticks) > 10
    else:
        request = asyncio.create_task(async_post(d, own))
        await asyncio.wait_for(spawned.wait(), 2)
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request
    assert len(processes) == 1
    assert processes[0].returncode is not None
    with pytest.raises(ChildProcessError):
        os.waitpid(processes[0].pid, os.WNOHANG)
    assert effects == []
    assert list((d.root / "tmp").iterdir()) == []


def test_reported_snapshot_size_is_checked_before_send(daemon, monkeypatch, tmp_path):
    d = daemon()
    own = d.root / "tenant" / "fixture.txt"
    own.write_bytes(b"owned fixture")
    child_hook(monkeypatch, tmp_path, """
original = env['copy_attachment']
def lie(payload):
    result = original(payload)
    result['size'] += 1
    return result
env['copy_attachment'] = lie
""")
    effects = []
    adapter(d, monkeypatch, lambda *args, **kwargs: effects.append(args))
    response = post(d, own)
    assert (response.status_code, effects) == (400, []), response.text
    assert list((d.root / "tmp").iterdir()) == []


@pytest.mark.parametrize("output", ["null", "not JSON", '{"ok": true, "size": 0}'])
def test_invalid_child_output_is_rejected_and_cleaned(daemon, monkeypatch, tmp_path, output):
    d = daemon()
    own = d.root / "tenant" / "fixture.txt"
    own.write_bytes(b"owned fixture")
    script = tmp_path / "invalid_copy.py"
    script.write_text(f"print({output!r})\n")
    monkeypatch.setattr(isolated_files, "_COPY_CHILD", script, raising=False)
    effects = []
    adapter(d, monkeypatch, lambda *args, **kwargs: effects.append(args))
    response = post(d, own)
    assert (response.status_code, effects) == (400, []), response.text
    assert list((d.root / "tmp").iterdir()) == []


def test_child_never_follows_final_link_to_otherwise_allowed_file(daemon, monkeypatch):
    d = daemon()
    own = d.root / "tenant" / "fixture.txt"
    own.write_bytes(b"owned fixture")

    def swap(payload):
        target = own.with_name("parked.txt")
        own.rename(target)
        own.symlink_to(target)

    before_child(monkeypatch, swap)
    effects = []
    adapter(d, monkeypatch, lambda *args, **kwargs: effects.append(args))
    response = post(d, own)
    assert (response.status_code, effects) == (400, []), response.text


@pytest.mark.parametrize("mode", ["off", "shadow", "enforce"])
def test_sqlite_registered_after_identity_snapshot_is_refused(daemon, monkeypatch, mode):
    d = daemon(mode)
    own = d.root / "tenant" / "owned.txt"
    own.write_bytes(b"ordinary harmless fixture")
    store = d.root / "peer" / "new-store.data"
    connections = []
    registered = []

    def register_and_replace(payload):
        connection = sqlite3.connect(
            store, factory=live_sqlite.LiveSQLiteConnection, check_same_thread=False,
        )
        connections.append(connection)
        live_sqlite.track_sqlite_connection(connection, store)
        connection.execute("CREATE TABLE fixture(value TEXT)").close()
        connection.execute("INSERT INTO fixture VALUES ('harmless new database fixture')").close()
        connection.commit()
        # Keep the inode registered without a recognizable database header.
        store.write_bytes(b"ordinary harmless fixture")
        info = store.stat()
        identity = (info.st_dev, info.st_ino)
        assert identity in live_sqlite.live_sqlite_identities()
        assert identity not in {tuple(pair) for pair in payload["live_sqlite"]}
        registered.append(identity)
        store.replace(own)
        assert identity in live_sqlite.live_sqlite_identities()

    before_child(monkeypatch, register_and_replace)
    copied = []

    def send(chat, path, **kwargs):
        copied.append(Path(path).read_bytes())
        return {"message_id": "fixture"}

    adapter(d, monkeypatch, send)
    try:
        response = post(d, own)
        assert len(registered) == 1
        assert (response.status_code, copied) == (400, []), response.text
        assert "live SQLite inode" in response.text
        assert not list((d.root / "tmp").glob("media-*"))
    finally:
        for connection in connections:
            connection.close()


@pytest.mark.parametrize("mode", ["off", "shadow", "enforce"])
def test_unrelated_sqlite_registered_during_copy_still_allows_send(daemon, monkeypatch, mode):
    d = daemon(mode)
    own = d.root / "tenant" / "owned.txt"
    own.write_bytes(b"ordinary harmless fixture")
    connections = []
    registered = []

    def register_unrelated(payload):
        store = d.root / "peer" / "unrelated.data"
        connection = sqlite3.connect(
            store, factory=live_sqlite.LiveSQLiteConnection, check_same_thread=False,
        )
        connections.append(connection)
        live_sqlite.track_sqlite_connection(connection, store)
        connection.execute("CREATE TABLE fixture(value TEXT)").close()
        connection.commit()
        info = store.stat()
        identity = (info.st_dev, info.st_ino)
        assert identity not in {tuple(pair) for pair in payload["live_sqlite"]}
        registered.append(identity)

    before_child(monkeypatch, register_unrelated)
    copied = []

    def send(chat, path, **kwargs):
        copied.append(Path(path).read_bytes())
        return {"message_id": "fixture"}

    adapter(d, monkeypatch, send)
    try:
        response = post(d, own)
        assert response.status_code == 200, response.text
        assert len(registered) == 1
        assert registered[0] in live_sqlite.live_sqlite_identities()
        assert copied == [b"ordinary harmless fixture"]
        assert not list((d.root / "tmp").glob("media-*"))
    finally:
        for connection in connections:
            connection.close()


@pytest.mark.asyncio
async def test_copy_child_uses_explicit_empty_environment(daemon, monkeypatch):
    d = daemon()
    own = d.root / "tenant" / "owned.txt"
    own.write_bytes(b"harmless own fixture")
    monkeypatch.setenv("PINKY_COPY_DAEMON_ONLY", "parent-only fixture")
    environments = []
    original = asyncio.create_subprocess_exec

    async def create(*args, **kwargs):
        environments.append(kwargs.get("env"))
        return await original(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    copied = []

    def send(chat, path, **kwargs):
        copied.append(Path(path).read_bytes())
        return {"message_id": "fixture"}

    adapter(d, monkeypatch, send)
    response = await async_post(d, own)
    assert response.status_code == 200, response.text
    assert copied == [b"harmless own fixture"]
    assert environments == [{}]
    assert not list((d.root / "tmp").glob("media-*"))
