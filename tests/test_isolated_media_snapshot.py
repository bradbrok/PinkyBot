"""Isolated attachments are bounded private copies with deterministic cleanup."""

import gc
import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest
from anyio.from_thread import start_blocking_portal
from fastapi import HTTPException
from fastapi.testclient import TestClient

from tests.isolated_policy_support import closure, replace_cell, signed
from tests.isolated_policy_support import daemon as daemon

pytestmark = pytest.mark.real_auth


def adapter(d, monkeypatch, send):
    replace_cell(monkeypatch, closure(d.app, "_send_file_message"),
                 "_get_platform_adapter", lambda *args: SimpleNamespace(send_document=send))


def post(d, file, caller="tenant", client=None):
    route = "/broker/send-document"
    owned_client = client is None
    client = client or TestClient(d.app)
    try:
        return client.post(route, headers=signed(d, "POST", route, caller), json={
            "agent_name": caller, "chat_id": "fixture", "file_path": str(file)})
    finally:
        if owned_client:
            client.close()


@pytest.mark.parametrize("outcome", ["success", "upstream", "http"])
def test_snapshot_is_private_named_and_removed_on_every_outcome(daemon, monkeypatch, outcome, capsys):
    d = daemon()
    own = d.root / "tenant" / "fixture.txt"
    own.write_bytes(b"owned fixture")
    seen = []
    attempts = []
    route = closure(d.app, "_broker_send_file_route")
    cells = dict(zip(route.__code__.co_freevars, route.__closure__))
    original_log = cells["_outreach_attempt_log"].cell_contents

    def log(**values):
        attempts.append(values)
        original_log(**values)

    replace_cell(monkeypatch, route, "_outreach_attempt_log", log)

    def send(chat, file, **kwargs):
        path = Path(file)
        seen.append(path)
        assert path.name == own.name
        assert path.read_bytes() == b"owned fixture"
        assert path.parent.parent == d.root / "tmp"
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
        for name in ("tenant", "peer", "normal", "dreamer"):
            assert not path.is_relative_to(d.root / name)
        if outcome == "upstream":
            raise RuntimeError("fixture adapter error")
        if outcome == "http":
            raise HTTPException(503, "fixture unavailable")
        return {"message_id": "fixture"}

    adapter(d, monkeypatch, send)
    response = post(d, own)
    assert response.status_code == {"success": 200, "upstream": 502, "http": 503}[outcome], response.text
    assert len(seen) == 1
    assert not seen[0].parent.exists()
    logged = capsys.readouterr().err
    assert [attempt["file_path"] for attempt in attempts] == [str(own.resolve())]
    assert str(seen[0]) not in logged


def test_snapshot_descriptors_do_not_leak_across_fifty_sends(daemon, monkeypatch):
    d = daemon()
    own = d.root / "tenant" / "fixture.txt"
    own.write_bytes(b"owned fixture")
    snapshots = []

    def send(chat, file, **kwargs):
        path = Path(file)
        assert path.read_bytes() == b"owned fixture"
        snapshots.append(path)
        return {"message_id": "fixture"}

    adapter(d, monkeypatch, send)
    descriptor_dir = "/dev/fd" if Path("/dev/fd").is_dir() else "/proc/self/fd"
    client = TestClient(d.app)
    try:
        # Reuse one request loop without invoking the application lifespan.
        # Warm its thread-local store connection before counting descriptors.
        with start_blocking_portal() as portal:
            client.portal = portal
            assert post(d, own, client=client).status_code == 200
            snapshots.clear()
            gc.collect()
            before = len(os.listdir(descriptor_dir))
            for _ in range(50):
                response = post(d, own, client=client)
                assert response.status_code == 200, response.text
            gc.collect()
            assert len(os.listdir(descriptor_dir)) == before
            client.portal = None
    finally:
        client.close()
    assert len(set(snapshots)) == 50
    assert all(not path.parent.exists() for path in snapshots)


@pytest.mark.parametrize("target", ["hardlink", "directory"])
def test_nonregular_or_multilink_attachment_is_rejected_before_adapter(daemon, monkeypatch, target):
    d = daemon()
    own = d.root / "tenant" / "fixture.txt"
    if target == "hardlink":
        peer = d.root / "peer" / "fixture.txt"
        peer.write_bytes(b"foreign fixture")
        os.link(peer, own)
    else:
        own.mkdir()
    effects = []
    adapter(d, monkeypatch, lambda *args, **kwargs: effects.append(args))
    response = post(d, own)
    assert (response.status_code, effects) == (400, []), response.text


@pytest.mark.parametrize("swap", ["file", "directory"])
def test_opened_attachment_is_revalidated_after_path_replacement(daemon, monkeypatch, swap):
    d = daemon()
    directory = d.root / "tenant" / "outbox"
    directory.mkdir()
    own = directory / "fixture.txt"
    own.write_bytes(b"original fixture")
    other_dir = d.root / ("tenant" if swap == "file" else "peer") / "replacement"
    other_dir.mkdir()
    other = other_dir / own.name
    other.write_bytes(b"replacement fixture")
    original_open = os.open
    replaced = []

    def replace_before_open(path, flags, *args, **kwargs):
        if Path(path) == own and not replaced:
            replaced.append(True)
            if swap == "file":
                own.unlink()
                own.symlink_to(other)
            else:
                directory.rename(d.root / "tenant" / "parked")
                directory.symlink_to(other_dir, target_is_directory=True)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", replace_before_open)
    effects = []
    adapter(d, monkeypatch, lambda *args, **kwargs: effects.append(args))
    response = post(d, own)
    assert replaced == [True]
    assert (response.status_code, effects) == (400, []), response.text


def test_overcap_attachment_is_rejected_without_reading_it(daemon, monkeypatch):
    from pinky_daemon import isolated_files

    d = daemon()
    own = d.root / "tenant" / "large.txt"
    with own.open("wb") as file:
        file.truncate(isolated_files.MAX_MEDIA_BYTES + 1)
    reads = []
    original_read = os.read

    def read(fd, count):
        reads.append(count)
        return original_read(fd, count)

    monkeypatch.setattr(isolated_files.os, "read", read)
    effects = []
    adapter(d, monkeypatch, lambda *args, **kwargs: effects.append(args))
    response = post(d, own)
    assert (response.status_code, effects, reads) == (400, [], []), response.text
    assert not (d.root / "tmp").exists()


def test_attachment_growth_is_bounded_and_partial_snapshot_is_removed(daemon, monkeypatch):
    from pinky_daemon import isolated_files

    d = daemon()
    own = d.root / "tenant" / "fixture.txt"
    own.write_bytes(b"tiny")
    monkeypatch.setattr(isolated_files, "MAX_MEDIA_BYTES", 8)
    original_path = isolated_files.path_for_fd

    def grow(fd):
        path = original_path(fd)
        with own.open("ab") as file:
            file.write(b"extra bytes beyond the limit")
        return path

    monkeypatch.setattr(isolated_files, "path_for_fd", grow)
    effects = []
    adapter(d, monkeypatch, lambda *args, **kwargs: effects.append(args))
    response = post(d, own)
    assert (response.status_code, effects) == (400, []), response.text
    assert list((d.root / "tmp").iterdir()) == []


def test_unavailable_opened_path_lookup_fails_closed(daemon, monkeypatch):
    from pinky_daemon import isolated_files

    d = daemon()
    own = d.root / "tenant" / "fixture.txt"
    own.write_bytes(b"owned fixture")

    def unavailable(fd):
        raise OSError("fixture lookup unavailable")

    monkeypatch.setattr(isolated_files, "path_for_fd", unavailable)
    effects = []
    adapter(d, monkeypatch, lambda *args, **kwargs: effects.append(args))
    response = post(d, own)
    assert (response.status_code, effects) == (400, []), response.text


def test_nonisolated_adapter_keeps_original_unresolved_argument(daemon, monkeypatch):
    d = daemon()
    own = d.root / "normal" / "fixture.txt"
    own.write_bytes(b"fixture")
    alias = own.with_name("alias.txt")
    alias.symlink_to(own)
    effects = []

    def send(chat, file, **kwargs):
        effects.append(file)
        return {"message_id": "fixture"}

    adapter(d, monkeypatch, send)
    response = post(d, alias, "normal")
    assert response.status_code == 200, response.text
    assert effects == [str(alias)]
    assert not (d.root / "tmp").exists()
