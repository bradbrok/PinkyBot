"""Attachment containment refuses outside paths before revealing their existence."""

import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from pinky_daemon import api
from tests.isolated_policy_support import closure, replace_cell, signed
from tests.isolated_policy_support import daemon as daemon

pytestmark = pytest.mark.real_auth
DENIAL = {"detail": "file_path must be inside the caller's working directory"}


def install_adapter(d, monkeypatch, effects):
    def send(chat, path, **kwargs):
        effects.append((Path(path), Path(path).read_bytes()))
        return {"message_id": "fixture"}

    adapter = SimpleNamespace(send_photo=send, send_document=send)
    replace_cell(
        monkeypatch, closure(d.app, "_send_file_message"),
        "_get_platform_adapter", lambda *args: adapter,
    )


def post(d, path, kind="document", caller="tenant"):
    route = f"/broker/send-{kind}"
    client = TestClient(d.app)
    try:
        return client.post(route, headers=signed(d, "POST", route, caller), json={
            "agent_name": caller, "chat_id": "fixture", "file_path": str(path),
        })
    finally:
        client.close()


@pytest.mark.parametrize("mode", ["off", "shadow", "enforce"])
@pytest.mark.parametrize("kind", ["photo", "document"])
@pytest.mark.parametrize("form", ["absolute", "symlink", "traversal"])
def test_outside_attachment_existence_is_not_observable(
    daemon, monkeypatch, mode, kind, form,
):
    d = daemon(mode)
    present = d.root / "peer" / "present.txt"
    absent = d.root / "peer" / "absent.txt"
    present.write_bytes(b"ordinary outside fixture")
    if form == "symlink":
        paths = [d.root / "tenant" / "present-link.txt", d.root / "tenant" / "absent-link.txt"]
        for link, target in zip(paths, [present, absent]):
            link.symlink_to(target)
    elif form == "traversal":
        paths = [d.root / "tenant" / ".." / "peer" / p.name for p in [present, absent]]
    else:
        paths = [present, absent]
    effects = []
    install_adapter(d, monkeypatch, effects)
    responses = [post(d, path, kind) for path in paths]
    assert [(r.status_code, r.json()) for r in responses] == [(403, DENIAL), (403, DENIAL)]
    assert effects == []


@pytest.mark.parametrize("exists", [False, True])
def test_sibling_directory_prefix_is_outside(daemon, monkeypatch, exists):
    d = daemon()
    sibling = d.root / "tenant2"
    sibling.mkdir()
    selected = sibling / "fixture.txt"
    if exists:
        selected.write_bytes(b"ordinary sibling fixture")
    effects = []
    install_adapter(d, monkeypatch, effects)
    response = post(d, selected)
    assert (response.status_code, response.json(), effects) == (403, DENIAL, [])


@pytest.mark.parametrize("target", ["missing", "directory"])
def test_inside_missing_or_nonfile_keeps_rejected_log_and_stops_typing(
    daemon, monkeypatch, target,
):
    d = daemon()
    selected = d.root / "tenant" / "fixture.txt"
    if target == "directory":
        selected.mkdir()
    effects, attempts, stopped = [], [], []
    install_adapter(d, monkeypatch, effects)
    route = closure(d.app, "_broker_send_file_route")
    replace_cell(
        monkeypatch, route, "_outreach_attempt_log", lambda **values: attempts.append(values),
    )
    broker = dict(zip(route.__code__.co_freevars, route.__closure__))["broker"].cell_contents
    monkeypatch.setattr(broker, "_stop_typing", lambda *args: stopped.append(args))
    response = post(d, selected)
    assert response.status_code == 400, response.text
    assert effects == []
    assert [attempt["outcome"] for attempt in attempts] == ["rejected"]
    assert stopped == [("tenant", "fixture")]


@pytest.mark.parametrize("kind", ["photo", "document"])
@pytest.mark.parametrize("form", ["registered", "resolved"])
def test_symlinked_registered_working_root_accepts_both_forms(daemon, monkeypatch, kind, form):
    d = daemon()
    alias = d.root.parent / "registered-root"
    alias.symlink_to(d.root, target_is_directory=True)
    registered = alias / "tenant"
    d.agents._db.execute(
        "UPDATE agents SET working_dir=? WHERE name=?", (str(registered), "tenant"),
    )
    d.agents._db.commit()
    assert d.agents.get("tenant").working_dir == str(registered)
    own = d.root / "tenant" / "fixture.txt"
    content = b"ordinary owned fixture"
    own.write_bytes(content)
    selected = registered / own.name if form == "registered" else own
    effects = []
    install_adapter(d, monkeypatch, effects)
    response = post(d, selected, kind)
    assert response.status_code == 200, response.text
    assert len(effects) == 1 and effects[0][1] == content
    assert not effects[0][0].parent.exists()


@pytest.mark.parametrize("form", ["absolute", "relative", "traversal"])
def test_lexically_outside_path_has_no_filesystem_lookup(daemon, monkeypatch, form):
    d = daemon()
    selected = d.root / "peer" / "outside-lookup.txt"
    selected.write_bytes(b"ordinary outside fixture")
    if form == "relative":
        incoming = Path(os.path.relpath(selected))
    elif form == "traversal":
        incoming = d.root / "tenant" / ".." / "peer" / selected.name
    else:
        incoming = selected
    effects, calls = [], []
    install_adapter(d, monkeypatch, effects)
    original_realpath, original_stat = api.os.path.realpath, api.os.stat
    original_lstat, original_resolve = api.os.lstat, api.Path.resolve
    untrusted = {str(selected), str(incoming)}

    def record(method, path):
        if not isinstance(path, int) and os.fsdecode(path) in untrusted:
            calls.append((method, os.fsdecode(path)))

    def realpath(path, *args, **kwargs):
        record("realpath", path)
        return original_realpath(path, *args, **kwargs)

    def stat(path, *args, **kwargs):
        record("stat", path)
        return original_stat(path, *args, **kwargs)

    def lstat(path, *args, **kwargs):
        record("lstat", path)
        return original_lstat(path, *args, **kwargs)

    def resolve(path, *args, **kwargs):
        record("resolve", path)
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(api.os.path, "realpath", realpath)
    monkeypatch.setattr(api.os, "stat", stat)
    monkeypatch.setattr(api.os, "lstat", lstat)
    monkeypatch.setattr(api.Path, "resolve", resolve)
    response = post(d, incoming)
    assert (response.status_code, response.json(), effects) == (403, DENIAL, [])
    # Resolving the trusted registered root is allowed; this path is never looked up.
    assert calls == []


def test_nonisolated_outside_attachment_send_is_unchanged(daemon, monkeypatch):
    d = daemon()
    selected = d.root / "peer" / "ordinary.txt"
    content = b"ordinary outside fixture"
    selected.write_bytes(content)
    effects = []
    install_adapter(d, monkeypatch, effects)
    response = post(d, selected, caller="normal")
    assert (response.status_code, effects) == (200, [(selected, content)]), response.text
