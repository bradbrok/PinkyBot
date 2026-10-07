"""Shared lexical guards preserve root boundaries and reject invalid media paths."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pinky_daemon import api
from tests.isolated_policy_support import closure, replace_cell, signed
from tests.isolated_policy_support import daemon as daemon
from tests.test_isolated_media_containment import install_adapter

pytestmark = pytest.mark.real_auth


def post_media(d, path, caller="tenant", kind="document"):
    route = f"/broker/send-{kind}"
    client = TestClient(d.app, raise_server_exceptions=False)
    try:
        return client.post(route, headers=signed(d, "POST", route, caller), json={
            "agent_name": caller, "chat_id": "fixture", "file_path": str(path),
        })
    finally:
        client.close()


@pytest.mark.parametrize("mode", ["off", "shadow", "enforce"])
@pytest.mark.parametrize("caller", ["tenant", "normal"])
@pytest.mark.parametrize("location", ["inside", "outside"])
@pytest.mark.parametrize("kind", ["photo", "document"])
def test_media_nul_rejects_logs_and_stops_typing(
    daemon, monkeypatch, capsys, mode, caller, location, kind,
):
    d = daemon(mode)
    selected = d.root / (caller if location == "inside" else "peer") / "bad"
    effects, attempts, stopped = [], [], []
    install_adapter(d, monkeypatch, effects)
    route = closure(d.app, "_broker_send_file_route")
    original = dict(zip(route.__code__.co_freevars, route.__closure__))["_outreach_attempt_log"]
    logger = original.cell_contents

    def record(**values):
        attempts.append(values)
        logger(**values)

    replace_cell(monkeypatch, route, "_outreach_attempt_log", record)
    monkeypatch.setattr(d.app.state.broker, "_stop_typing", lambda *args: stopped.append(args))
    response = post_media(d, str(selected) + "\x00.txt", caller, kind)
    assert response.status_code == 400, response.text
    assert response.json()["detail"].startswith(f"Failed to send_{kind}: ValueError: ")
    assert effects == []
    assert [attempt["outcome"] for attempt in attempts] == ["rejected"]
    assert stopped == [(caller, "fixture")]
    log = capsys.readouterr().err
    assert "outreach-attempt:" in log and "outcome=rejected" in log and "ValueError" in log


@pytest.mark.parametrize("mode", ["off", "shadow", "enforce"])
@pytest.mark.parametrize("kind", ["photo", "document"])
@pytest.mark.parametrize("spelling", ["single-slash", "double-slash"])
def test_sole_slash_root_accepts_inside_media(daemon, monkeypatch, mode, kind, spelling):
    d = daemon(mode)
    # Use the real read-only registration preflight, without initializing the host root.
    d.agents._db.execute("DELETE FROM agents WHERE name<>?", ("tenant",))
    d.agents._db.commit()
    assert d.agents.resolve_registration_workspace("tenant", "/") == Path("/")
    d.agents._db.execute("UPDATE agents SET working_dir=? WHERE name=?", ("/", "tenant"))
    d.agents._db.commit()
    selected = d.root / "tenant" / "ordinary.txt"
    content = b"ordinary slash-root fixture"
    selected.write_bytes(content)
    incoming = str(selected)
    if spelling == "double-slash":
        incoming = "/" + incoming
    effects = []
    install_adapter(d, monkeypatch, effects)
    response = post_media(d, incoming, kind=kind)
    assert response.status_code == 200, response.text
    assert len(effects) == 1 and effects[0][1] == content
    assert not effects[0][0].parent.exists()


@pytest.mark.parametrize("path,root,expected", [
    ("/", "/", True),
    ("//", "/", True),
    ("/file", "/", True),
    ("//file", "/", True),
    ("relative", "/", False),
    ("/file", "//", False),
    ("//file", "//", True),
    ("/tenant", "/tenant", True),
    ("/tenant/file", "/tenant", True),
    ("/tenant2/file", "/tenant", False),
    ("//tenant/file", "/tenant", False),
    ("//tenant/file", "//tenant", True),
    ("//tenant2/file", "//tenant", False),
])
def test_shared_path_guard_is_lexical_and_preserves_separator_boundaries(
    monkeypatch, path, root, expected,
):
    within = getattr(api, "_path_within", None)
    assert callable(within), "routes need one shared lexical containment helper"
    calls = []

    def wrap(name, original):
        def tracked(*args, **kwargs):
            calls.append(name)
            return original(*args, **kwargs)
        return tracked

    monkeypatch.setattr(api.os.path, "realpath", wrap("realpath", api.os.path.realpath))
    monkeypatch.setattr(api.os, "stat", wrap("stat", api.os.stat))
    monkeypatch.setattr(api.os, "lstat", wrap("lstat", api.os.lstat))
    monkeypatch.setattr(api.Path, "resolve", wrap("resolve", api.Path.resolve))
    assert within(path, root) is expected
    assert calls == []
