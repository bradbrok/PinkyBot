"""Database content is refused from private attachment copies before dispatch."""

import os
import sqlite3
from pathlib import Path

import pytest

from pinky_daemon import isolated_files
from pinky_identity import live_sqlite
from tests.isolated_file_support import before_child
from tests.isolated_policy_support import daemon as daemon
from tests.test_isolated_child_copy import async_post
from tests.test_isolated_media_snapshot import adapter, post

pytestmark = pytest.mark.real_auth

MAIN_HEADER = b"SQLite format 3\x00"
WAL_HEADERS = (b"\x37\x7f\x06\x82", b"\x37\x7f\x06\x83")
JOURNAL_HEADER = b"\xd9\xd5\x05\xf9\x20\xa1\x63\xd7"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["off", "shadow", "enforce"])
async def test_registration_after_final_registry_refresh_refuses_private_database(
    daemon, monkeypatch, mode,
):
    d = daemon(mode)
    own = d.root / "tenant" / "ordinary.txt"
    own.write_bytes(b"ordinary fixture")
    store = d.root / "peer" / "closed-store.data"
    with sqlite3.connect(store) as connection:
        connection.execute("CREATE TABLE fixture(value TEXT)").close()
        connection.execute("INSERT INTO fixture VALUES ('synthetic database bytes')").close()
    connection.close()
    identity = (store.stat().st_dev, store.stat().st_ino)
    original = live_sqlite.live_sqlite_identities
    assert identity not in original()
    calls, connections, snapshots, effects = [], [], [], []

    def registry_read():
        calls.append(True)
        result = original()
        snapshots.append(set(result))
        if len(calls) == 2:
            connection = sqlite3.connect(
                own, factory=live_sqlite.LiveSQLiteConnection, check_same_thread=False,
            )
            connections.append(connection)
            live_sqlite.track_sqlite_connection(connection, own)
            assert identity in original()
        return result

    before_child(monkeypatch, lambda payload: store.replace(own))
    monkeypatch.setattr(live_sqlite, "live_sqlite_identities", registry_read)

    def send(chat, path, **kwargs):
        effects.append(Path(path).read_bytes())
        return {"message_id": "fixture"}

    adapter(d, monkeypatch, send)
    try:
        response = await async_post(d, own)
        assert calls == [True, True]
        assert all(identity not in captured for captured in snapshots)
        assert identity in original()
        assert not list((d.root / "tmp").glob("media-*"))
        assert (response.status_code, effects) == (400, []), response.text
    finally:
        for connection in connections:
            connection.close()


@pytest.mark.parametrize("name,header", [
    ("notes.txt", MAIN_HEADER),
    ("notes.sqlite", MAIN_HEADER),
    ("notes.sqlite3", MAIN_HEADER),
    ("notes.txt", WAL_HEADERS[0]),
    ("notes.txt", WAL_HEADERS[1]),
    ("notes.txt", JOURNAL_HEADER),
], ids=["renamed-main", "sqlite-main", "sqlite3-main", "wal-82", "wal-83", "journal"])
def test_unregistered_database_content_is_refused(daemon, monkeypatch, name, header):
    d = daemon()
    own = d.root / "tenant" / name
    if header == MAIN_HEADER:
        with sqlite3.connect(own) as connection:
            connection.execute("CREATE TABLE fixture(value TEXT)").close()
        connection.close()
    else:
        own.write_bytes(header + bytes(64))
    assert own.read_bytes().startswith(header)
    assert (own.stat().st_dev, own.stat().st_ino) not in live_sqlite.live_sqlite_identities()
    effects = []
    adapter(d, monkeypatch, lambda *args, **kwargs: effects.append(args))
    response = post(d, own)
    assert (response.status_code, effects) == (400, []), response.text
    assert not list((d.root / "tmp").glob("media-*"))


@pytest.mark.parametrize("content", [
    b"SQLite format 3\n", b"SQLite format 2\x00", b"", b"0123456789",
    b"\x89PNG\r\n\x1a\nordinary image fixture",
], ids=["newline", "version-two", "empty", "ten-bytes", "image"])
def test_non_database_header_controls_are_sent(daemon, monkeypatch, content):
    d = daemon()
    own = d.root / "tenant" / "notes.txt"
    own.write_bytes(content)
    effects = []

    def send(chat, path, **kwargs):
        effects.append(Path(path).read_bytes())
        return {"message_id": "fixture"}

    adapter(d, monkeypatch, send)
    response = post(d, own)
    assert (response.status_code, effects) == (200, [content]), response.text
    assert not list((d.root / "tmp").glob("media-*"))


def test_database_header_check_reads_copy_after_caller_replaced(daemon, monkeypatch):
    d = daemon()
    own = d.root / "tenant" / "notes.txt"
    own.write_bytes(MAIN_HEADER + bytes(64))
    original = isolated_files._copy_media
    copied = []

    async def replace_caller_after_copy(payload):
        result = await original(payload)
        snapshot = Path(payload["target"])
        copied.append(snapshot.read_bytes())
        replacement = own.with_name("replacement.txt")
        replacement.write_bytes(b"ordinary replacement fixture")
        replacement.replace(own)
        return result

    monkeypatch.setattr(isolated_files, "_copy_media", replace_caller_after_copy)
    effects = []
    adapter(d, monkeypatch, lambda *args, **kwargs: effects.append(args))
    response = post(d, own)
    assert copied == [MAIN_HEADER + bytes(64)]
    assert own.read_bytes() == b"ordinary replacement fixture"
    assert (response.status_code, effects) == (400, []), response.text
    assert not list((d.root / "tmp").glob("media-*"))


@pytest.mark.parametrize("change", ["inode", "size"])
def test_private_copy_change_before_header_open_is_refused(daemon, monkeypatch, change):
    d = daemon()
    own = d.root / "tenant" / "notes.txt"
    content = b"ordinary fixture"
    own.write_bytes(content)
    original_open, original_copy = os.open, isolated_files._copy_media
    targets, changed = [], []

    async def remember_copy(payload):
        result = await original_copy(payload)
        targets.append(Path(payload["target"]))
        return result

    def change_before_open(path, flags, *args, **kwargs):
        if targets and not isinstance(path, int) and Path(path) == targets[0] and not changed:
            changed.append(True)
            if change == "inode":
                replacement = targets[0].with_name("replacement.txt")
                replacement.write_bytes(content)
                replacement.replace(targets[0])
            else:
                with targets[0].open("ab") as handle:
                    handle.write(b"more bytes")
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(isolated_files, "_copy_media", remember_copy)
    monkeypatch.setattr(os, "open", change_before_open)
    effects = []
    adapter(d, monkeypatch, lambda *args, **kwargs: effects.append(args))
    response = post(d, own)
    assert changed == [True]
    assert (response.status_code, effects) == (400, []), response.text
    assert not list((d.root / "tmp").glob("media-*"))


@pytest.mark.parametrize("header", [MAIN_HEADER, WAL_HEADERS[0], JOURNAL_HEADER],
                         ids=["main", "wal", "journal"])
def test_nonisolated_database_content_send_is_unchanged(daemon, monkeypatch, header):
    d = daemon()
    own = d.root / "normal" / "notes.txt"
    content = header + bytes(64)
    own.write_bytes(content)
    effects = []

    def send(chat, path, **kwargs):
        effects.append((Path(path), Path(path).read_bytes()))
        return {"message_id": "fixture"}

    adapter(d, monkeypatch, send)
    response = post(d, own, caller="normal")
    assert (response.status_code, effects) == (200, [(own, content)]), response.text
    assert not list((d.root / "tmp").glob("media-*"))
