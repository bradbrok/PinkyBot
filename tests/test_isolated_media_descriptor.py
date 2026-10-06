"""Replacements after validation cannot redirect an isolated attachment."""

import os
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from pinky_identity import live_sqlite
from tests.isolated_policy_support import closure, replace_cell, signed
from tests.isolated_policy_support import daemon as daemon

pytestmark = pytest.mark.real_auth
CASES = [(mode, kind) for mode in ("off", "shadow", "enforce")
         for kind in ("photo", "document", "video", "animation")
         if (mode, kind) != ("enforce", "animation")]


def request_after_swap(d, monkeypatch, mode, kind, swap):
    own_dir = d.root / "tenant" / "outbox"
    peer_dir = d.root / "peer" / "outbox"
    own_dir.mkdir()
    peer_dir.mkdir()
    own = own_dir / "fixture.txt"
    peer = peer_dir / "fixture.txt"
    own.write_bytes(b"owned harmless fixture")
    peer.write_bytes(b"foreign harmless fixture")
    resolved_own = own.resolve(strict=True)
    incoming = own
    if swap == "original-alias":
        incoming = own_dir / "alias.txt"
        incoming.symlink_to(own)
    staged = d.root / "tenant" / "staged-link"
    staged.symlink_to(peer_dir if swap == "directory" else peer,
                      target_is_directory=swap == "directory")
    validated = threading.Event()
    swapped = threading.Event()
    cancelled = threading.Event()
    thread_errors = []
    checked = []
    opened = []
    original_guard = live_sqlite.refuse_sqlite_attachment

    def pause_after_actual_sqlite_guard(file):
        # The original containment and SQLite guard execute unchanged.
        original_guard(file)
        assert Path(file) == resolved_own, (file, resolved_own)
        checked.append(Path(file))
        validated.set()
        assert swapped.wait(5), "scratch swap worker did not complete"

    def attacker():
        try:
            assert validated.wait(5), "handler never reached the checked-file pause"
            if cancelled.is_set():
                return
            if swap == "directory":
                own_dir.rename(d.root / "tenant" / "parked-outbox")
                os.replace(staged, own_dir)
            else:
                os.replace(staged, incoming)
        except BaseException as error:
            thread_errors.append(repr(error))
        finally:
            swapped.set()

    def read_attachment(chat, file, **kwargs):
        del chat, kwargs
        selected = Path(file)
        opened.append((selected, selected.read_bytes()))
        return {"message_id": "fixture"}

    monkeypatch.setattr(live_sqlite, "refuse_sqlite_attachment", pause_after_actual_sqlite_guard)
    adapter = SimpleNamespace(**{f"send_{item}": read_attachment
                                 for item in ("photo", "document", "video", "animation")})
    replace_cell(monkeypatch, closure(d.app, "_send_file_message"),
                 "_get_platform_adapter", lambda *args: adapter)
    worker = threading.Thread(target=attacker, name="scratch-file-swap")
    worker.start()
    route = f"/broker/send-{kind}"
    client = TestClient(d.app)
    try:
        response = client.post(route, headers=signed(d, "POST", route), json={
            "agent_name": "tenant", "chat_id": "fixture", "file_path": str(incoming)})
    finally:
        client.close()
        cancelled.set()
        validated.set()
        worker.join(5)
    assert not worker.is_alive()
    assert not thread_errors, thread_errors
    return response, checked, opened, resolved_own


@pytest.mark.parametrize("mode,kind", CASES)
@pytest.mark.parametrize("swap", ["file", "directory"])
def test_isolated_checked_path_replacement_never_reads_peer(daemon, monkeypatch, mode, kind, swap):
    d = daemon(mode)
    response, checked, opened, resolved = request_after_swap(d, monkeypatch, mode, kind, swap)
    assert not any(payload == b"foreign harmless fixture" for _, payload in opened), (
        response.status_code, checked, opened, resolved)


@pytest.mark.parametrize("kind", ["photo", "document", "video"])
def test_changing_original_alias_cannot_redirect_resolved_argument(daemon, monkeypatch, kind):
    d = daemon("enforce")
    response, checked, opened, resolved = request_after_swap(
        d, monkeypatch, "enforce", kind, "original-alias")
    assert response.status_code == 200, response.text
    assert checked == [resolved]
    assert len(opened) == 1
    selected, payload = opened[0]
    assert payload == b"owned harmless fixture"
    assert selected.name == resolved.name
    assert not selected.is_relative_to(d.root / "tenant")
    assert not selected.parent.exists()
