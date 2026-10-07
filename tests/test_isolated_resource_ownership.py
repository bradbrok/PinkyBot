"""Object and file ownership expectations, without live files or adapters."""

import asyncio
import io
import os
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from pinky_daemon.streaming_session import StreamingSessionConfig
from pinky_daemon.tmux_session import TmuxSession
from tests.isolated_policy_support import closure, replace_cell, signed
from tests.isolated_policy_support import daemon as daemon

pytestmark = pytest.mark.real_auth


@pytest.mark.parametrize("operation", ["delete", "disable", "enable"])
@pytest.mark.parametrize("owner", ["peer", "tenant"])
def test_schedule_row_owner_matches_path(daemon, operation, owner):
    # Off deliberately prevents route denial from hiding the handler defect.
    d = daemon("off")
    schedule = d.agents.add_schedule(owner, "0 1 * * *", prompt="fixture")
    if operation == "enable":
        d.agents.toggle_schedule(schedule.id, False)
    before = d.agents.get_schedules(owner, enabled_only=False)
    path = f"/agents/tenant/schedules/{schedule.id}"
    method = "DELETE" if operation == "delete" else "POST"
    if operation != "delete":
        path += "/toggle"
    client = TestClient(d.app)
    response = client.request(
        method, path, params={"enabled": operation == "enable"}, headers=signed(d, method, path)
    )
    client.close()
    after = d.agents.get_schedules(owner, enabled_only=False)
    if owner == "peer":
        assert response.status_code == 404, (response.status_code, response.text)
        assert [s.to_dict() for s in after] == [s.to_dict() for s in before]
    else:
        assert response.status_code == 200, response.text
        assert len(after) == (0 if operation == "delete" else 1)
        if after:
            assert after[0].enabled == (operation == "enable")


@pytest.mark.parametrize("mode", ["off", "shadow", "enforce"])
@pytest.mark.parametrize("kind", ["photo", "document", "video"])
@pytest.mark.parametrize(
    "target",
    ["own", "own-symlink", "peer", "symlink", "sibling-prefix"],
)
def test_media_attachment_requires_caller_ownership(daemon, monkeypatch, kind, target, mode):
    d = daemon(mode)
    own = d.root / "tenant" / "own.txt"
    peer = d.root / "peer" / "peer.txt"
    own.write_text("own harmless fixture")
    peer.write_text("peer harmless fixture")
    link = d.root / "tenant" / "alias.txt"
    link.symlink_to(peer)
    own_link = d.root / "tenant" / "own-alias.txt"
    own_link.symlink_to(own)
    sibling = d.root / "tenant2" / "sibling.txt"
    sibling.parent.mkdir()
    sibling.write_text("sibling harmless fixture")
    selected = {"own": own, "own-symlink": own_link, "peer": peer,
                "symlink": link, "sibling-prefix": sibling}[target]
    opened = []
    reads = []
    original_open = io.open

    def track_open(file, *args, **kwargs):
        if isinstance(file, (str, os.PathLike)) and Path(file).resolve() == selected.resolve():
            reads.append(Path(file))
        return original_open(file, *args, **kwargs)

    monkeypatch.setattr(io, "open", track_open)

    def send_file(*args, **kwargs):
        del kwargs
        path = Path(args[1])
        opened.append((path, path.read_text()))
        return {"message_id": "fixture-message"}

    adapter = SimpleNamespace(send_photo=send_file, send_document=send_file, send_video=send_file)
    replace_cell(
        monkeypatch,
        closure(d.app, "_send_file_message"),
        "_get_platform_adapter",
        lambda *args: adapter,
    )
    path = f"/broker/send-{kind}"
    client = TestClient(d.app)
    response = client.post(
        path,
        headers=signed(d, "POST", path),
        json={
            "agent_name": "tenant",
            "platform": "telegram",
            "chat_id": "fixture-chat",
            "file_path": str(selected),
        },
    )
    client.close()
    if target in {"own", "own-symlink"}:
        assert response.status_code == 200, response.text
        assert len(opened) == 1
        snapshot, payload = opened[0]
        assert payload == "own harmless fixture"
        assert snapshot.name == own.name
        assert not snapshot.is_relative_to(d.root / "tenant")
        assert not snapshot.parent.exists()
    else:
        assert (response.status_code, opened) == (403, []), (response.status_code, opened)
        assert reads == []


@pytest.mark.parametrize("mode", ["off", "shadow", "enforce"])
def test_nonisolated_media_foreign_file_baseline_observation(daemon, monkeypatch, mode):
    d = daemon(mode)
    peer = d.root / "peer" / "observation.txt"
    peer.write_text("harmless baseline")
    opened = []

    def send_file(*args, **kwargs):
        opened.append(Path(args[1]).resolve())
        return {"message_id": "fixture-message"}

    adapter = SimpleNamespace(send_document=send_file)
    replace_cell(
        monkeypatch,
        closure(d.app, "_send_file_message"),
        "_get_platform_adapter",
        lambda *args: adapter,
    )
    path = "/broker/send-document"
    client = TestClient(d.app)
    response = client.post(
        path,
        headers=signed(d, "POST", path, "normal"),
        json={
            "agent_name": "normal",
            "platform": "telegram",
            "chat_id": "fixture-chat",
            "file_path": str(peer),
        },
    )
    client.close()
    assert response.status_code == 200, response.text
    assert opened == [peer.resolve()]


@pytest.mark.parametrize("initial", [False, True], ids=["accepted-own-id", "fresh-bind"])
@pytest.mark.parametrize("owner", ["tenant", "peer", "own-subdir", "sibling-prefix", "symlink"])
@pytest.mark.parametrize("mode", ["off", "shadow", "enforce"])
def test_transcript_path_belongs_to_agent_and_session(daemon, initial, owner, mode):
    d = daemon(mode)
    session = TmuxSession(
        StreamingSessionConfig(agent_name="tenant", working_dir=str(d.root / "tenant"))
    )
    project = Path.home() / ".claude/projects"
    own_dir = project / re.sub(r"[^a-zA-Z0-9]", "-", str(d.root / "tenant"))
    peer_dir = project / re.sub(r"[^a-zA-Z0-9]", "-", str(d.root / "peer"))
    selected_dir = {"tenant": own_dir, "peer": peer_dir, "symlink": own_dir,
                    "own-subdir": own_dir / "nested",
                    "sibling-prefix": own_dir.with_name(own_dir.name + "2")}[owner]
    selected_dir.mkdir(parents=True, exist_ok=True)
    selected = selected_dir / f"{owner}-session.jsonl"
    if owner == "symlink":
        peer_dir.mkdir(parents=True, exist_ok=True)
        peer_file = peer_dir / "peer.jsonl"
        peer_file.write_text("{}\n")
        selected.symlink_to(peer_file)
    else:
        selected.write_text("{}\n")
    repointed = []
    session._tailer = SimpleNamespace(set_transcript_path=lambda path, **kw: repointed.append(path))
    session._tailer_first_bind_pending = initial
    session._last_launch_used_continue = False
    session._bound_transcript_session_id = "" if initial else "tenant-session"
    d.app.state.broker.register_streaming("tenant", session, label="main")
    path = "/agents/tenant/transport/transcript-path"
    # Accepted A lineage is deliberately paired with B's file in the attack.
    client = TestClient(d.app)
    response = client.post(
        path,
        headers=signed(d, "POST", path),
        json={
            "transcript_path": str(selected),
            "session_id": "tenant-session",
            "label": "main",
        },
    )
    client.close()
    if owner == "tenant":
        assert response.status_code == 200, response.text
        assert repointed == [selected.resolve()]
    else:
        assert (response.status_code, repointed) == (403, []), (response.status_code, repointed)


@pytest.mark.parametrize(
    "path",
    ["/agents/tenant/triggers"],
)
def test_held_resource_routes_are_denied_before_handlers(daemon, monkeypatch, path):
    d = daemon()
    effects = []
    body = {}
    if path.startswith("/broker/"):
        own = d.root / "tenant" / "held-own.txt"
        own.write_text("harmless own file")

        def send_file(*args, **kwargs):
            effects.append(True)
            return {"message_id": "fixture"}

        adapter = SimpleNamespace(
            send_photo=send_file, send_document=send_file, send_video=send_file
        )
        replace_cell(
            monkeypatch,
            closure(d.app, "_send_file_message"),
            "_get_platform_adapter",
            lambda *args: adapter,
        )
        body = {"agent_name": "tenant", "chat_id": "fixture", "file_path": str(own)}
    elif path.endswith("/triggers"):
        body = {"trigger_type": "url", "url": "https://example.test/fixture", "name": "fixture"}
    else:
        own = (
            Path.home()
            / ".claude/projects"
            / str(d.root / "tenant").replace("/", "-")
            / "own.jsonl"
        )
        own.parent.mkdir(parents=True, exist_ok=True)
        own.write_text("{}\n")
        body = {"transcript_path": str(own), "session_id": "own"}
    client = TestClient(d.app)
    response = client.post(path, headers=signed(d, "POST", path), json=body)
    client.close()
    assert (response.status_code, effects) == (403, []), (response.status_code, response.text)


@pytest.mark.parametrize("mode", ["off", "shadow", "enforce"])
@pytest.mark.parametrize("resource", ["media", "transcript-registered", "transcript-realpath"])
@pytest.mark.parametrize("owner", ["tenant", "peer"])
def test_symlinked_working_dir_owns_only_its_resolved_resources(
    daemon, monkeypatch, mode, resource, owner
):
    d = daemon(mode)
    work = {}
    for name in ("tenant", "peer"):
        real = d.root / f"real.{name}_work"
        real.mkdir()
        registered = d.root / f"linked-{name}"
        registered.symlink_to(real, target_is_directory=True)
        # Model a stored symlink path from before workspace canonicalization.
        assert Path(d.agents._db_path).is_relative_to(d.root)
        d.agents._db.execute("UPDATE agents SET working_dir=? WHERE name=?", (str(registered), name))
        d.agents._db.commit()
        assert d.agents.get(name).working_dir == str(registered)
        work[name] = (registered, real)
    effects = []
    if resource == "media":
        selected = work[owner][0] / "own.txt"
        selected.write_text("harmless fixture")

        def send_file(chat, file, **kwargs):
            effects.append(Path(file))
            return {"message_id": "fixture"}

        replace_cell(monkeypatch, closure(d.app, "_send_file_message"),
                     "_get_platform_adapter", lambda *args: SimpleNamespace(send_photo=send_file))
        path = "/broker/send-photo"
        body = {"agent_name": "tenant", "chat_id": "fixture", "file_path": str(selected)}
    else:
        index = 0 if resource == "transcript-registered" else 1
        encoded = re.sub(r"[^a-zA-Z0-9]", "-", str(work[owner][index]))
        selected = Path.home() / ".claude/projects" / encoded / "fixture.jsonl"
        selected.parent.mkdir(parents=True, exist_ok=True)
        selected.write_text("{}\n")
        session = TmuxSession(StreamingSessionConfig(
            agent_name="tenant", working_dir=str(work["tenant"][0])))
        session._tailer = SimpleNamespace(set_transcript_path=lambda file, **kw: effects.append(file))
        session._tailer_first_bind_pending = True
        session._last_launch_used_continue = False
        d.app.state.broker.register_streaming("tenant", session, label="main")
        path = "/agents/tenant/transport/transcript-path"
        body = {"transcript_path": str(selected), "session_id": "fixture", "label": "main"}
    client = TestClient(d.app)
    try:
        response = client.post(path, headers=signed(d, "POST", path), json=body)
    finally:
        client.close()
    if owner == "tenant":
        assert response.status_code == 200, response.text
        if resource == "media":
            assert len(effects) == 1
            assert effects[0].name == selected.name
            assert not effects[0].is_relative_to(work["tenant"][1])
            assert not effects[0].parent.exists()
        else:
            assert effects == [selected.resolve()]
    else:
        assert (response.status_code, effects) == (403, []), response.text


@pytest.mark.parametrize("mode", ["off", "shadow", "enforce"])
@pytest.mark.parametrize("resource", ["photo", "document", "video", "transcript-path"])
def test_resource_registry_uncertainty_fails_closed(daemon, monkeypatch, mode, resource):
    from pinky_daemon import api

    d = daemon(mode)
    effects = []
    monkeypatch.setattr(api, "isolation_flag", lambda *args: None)
    if resource == "transcript-path":
        path = "/agents/tenant/transport/transcript-path"
        session = SimpleNamespace(set_transcript_path=lambda *args, **kw: effects.append(args))
        d.app.state.broker.register_streaming("tenant", session, label="main")
        own = Path.home() / ".claude/projects" / "fixture" / "session.jsonl"
        body = {"transcript_path": str(own), "session_id": "fixture"}
    else:
        path = f"/broker/send-{resource}"
        own = d.root / "tenant" / "own.txt"
        own.write_text("harmless fixture")

        def send_file(*args, **kwargs):
            effects.append(args)
            return {"message_id": "fixture"}

        adapter = SimpleNamespace(send_photo=send_file, send_document=send_file, send_video=send_file)
        replace_cell(monkeypatch, closure(d.app, "_send_file_message"),
                     "_get_platform_adapter", lambda *args: adapter)
        body = {"agent_name": "tenant", "chat_id": "fixture", "file_path": str(own)}
    client = TestClient(d.app)
    try:
        response = client.post(path, headers=signed(d, "POST", path), json=body)
    finally:
        client.close()
    assert (response.status_code, effects) == (403, []), response.text


def test_isolated_missing_media_remains_bad_input_without_adapter(daemon, monkeypatch):
    d = daemon("off")
    effects = []

    def send_file(*args, **kwargs):
        effects.append(args)
        raise FileNotFoundError("missing fixture")

    replace_cell(monkeypatch, closure(d.app, "_send_file_message"),
                 "_get_platform_adapter", lambda *args: SimpleNamespace(send_photo=send_file))
    path = "/broker/send-photo"
    client = TestClient(d.app)
    try:
        response = client.post(path, headers=signed(d, "POST", path), json={
            "agent_name": "tenant", "chat_id": "fixture",
            "file_path": str(d.root / "tenant" / "missing.txt")})
    finally:
        client.close()
    assert response.status_code == 400, response.text
    assert "FileNotFoundError" in response.json()["detail"]
    assert effects == []


def test_send_animation_stays_unlisted(daemon, monkeypatch):
    d = daemon()
    effects = []
    own = d.root / "tenant" / "own.gif"
    own.write_bytes(b"GIF89a fixture")
    replace_cell(monkeypatch, closure(d.app, "_send_file_message"),
                 "_get_platform_adapter", lambda *args: effects.append(args))
    path = "/broker/send-animation"
    client = TestClient(d.app)
    try:
        response = client.post(path, headers=signed(d, "POST", path), json={
            "agent_name": "tenant", "chat_id": "fixture", "file_path": str(own)})
    finally:
        client.close()
    assert (response.status_code, effects) == (403, []), response.text


@pytest.mark.parametrize("mode", ["off", "shadow", "enforce"])
@pytest.mark.parametrize("isolated", [True, False])
def test_transcript_binding_preserves_missing_own_and_nonisolated_peer_paths(
    daemon, mode, isolated
):
    d = daemon(mode)
    actor = "tenant" if isolated else "normal"
    owner = actor if isolated else "peer"
    own = Path.home() / ".claude/projects" / re.sub(
        r"[^a-zA-Z0-9]", "-", str(d.root / owner)) / "missing.jsonl"
    assert not own.exists()
    effects = []
    session = SimpleNamespace(set_transcript_path=lambda file, **kw: effects.append(file))
    d.app.state.broker.register_streaming(actor, session, label="main")
    path = f"/agents/{actor}/transport/transcript-path"
    client = TestClient(d.app)
    try:
        response = client.post(path, headers=signed(d, "POST", path, actor), json={
            "transcript_path": str(own), "session_id": "fixture"})
    finally:
        client.close()
    assert response.status_code == 200, response.text
    assert effects == [own.resolve()]


def test_unverified_agent_header_does_not_restrict_browser_media(daemon, monkeypatch):
    from pinky_daemon.auth import INTERNAL_AGENT_HEADER, SESSION_COOKIE_NAME, create_session_cookie

    d = daemon("enforce")
    peer = d.root / "peer" / "fixture.txt"
    peer.write_text("harmless fixture")
    effects = []

    def send_file(chat, file, **kwargs):
        effects.append(Path(file))
        return {"message_id": "fixture"}

    replace_cell(monkeypatch, closure(d.app, "_send_file_message"),
                 "_get_platform_adapter", lambda *args: SimpleNamespace(send_photo=send_file))
    client = TestClient(d.app)
    try:
        client.cookies.set(SESSION_COOKIE_NAME, create_session_cookie(os.environ["PINKY_SESSION_SECRET"]))
        response = client.post("/broker/send-photo", headers={INTERNAL_AGENT_HEADER: "tenant"}, json={
            "agent_name": "tenant", "chat_id": "fixture", "file_path": str(peer)})
    finally:
        client.close()
    assert response.status_code == 200, response.text
    assert effects == [peer]


def _config_workdirs(d, config):
    work = {}
    for name in ("tenant", "peer"):
        real = d.root / f"real-{name}"
        real.mkdir()
        registered = d.root / f"alias-{name}"
        registered.symlink_to(real, target_is_directory=True)
        d.agents.update(
            name, isolation_mode="container" if config == "container" else "local",
            dedicated_config_dir=config == "local",
        )
        # Seed the legacy registered alias only in the owned synthetic registry.
        assert Path(d.agents._db_path).is_relative_to(d.root)
        d.agents._db.execute("UPDATE agents SET working_dir=? WHERE name=?", (str(registered), name))
        d.agents._db.commit()
        assert d.agents.get(name).working_dir == str(registered)
        work[name] = (registered, real)
    return work


@pytest.mark.parametrize("mode", ["off", "shadow", "enforce"])
@pytest.mark.parametrize("config", ["container", "local"])
@pytest.mark.parametrize("encoding", ["registered", "realpath"])
@pytest.mark.parametrize("owner", ["tenant", "peer", "nested"])
def test_isolated_config_transcript_owns_exact_project(daemon, mode, config, encoding, owner):
    d = daemon(mode)
    work = _config_workdirs(d, config)
    selected_owner = "peer" if owner == "peer" else "tenant"
    registered, real = work[selected_owner]
    cwd = registered if encoding == "registered" else real
    encoded = re.sub(r"[^a-zA-Z0-9]", "-", str(cwd))
    selected = real / f".claude-{config}" / "projects" / encoded
    if owner == "nested":
        selected /= "nested"
    selected /= "fixture.jsonl"
    selected.parent.mkdir(parents=True)
    selected.write_text("{}\n")
    effects = []
    session = TmuxSession(StreamingSessionConfig(
        agent_name="tenant", working_dir=str(work["tenant"][0])))
    session._tailer = SimpleNamespace(set_transcript_path=lambda file, **kw: effects.append(file))
    session._tailer_first_bind_pending = True
    session._last_launch_used_continue = False
    d.app.state.broker.register_streaming("tenant", session, label="main")
    path = "/agents/tenant/transport/transcript-path"
    client = TestClient(d.app)
    try:
        response = client.post(path, headers=signed(d, "POST", path), json={
            "transcript_path": str(selected), "session_id": "fixture", "label": "main"})
    finally:
        client.close()
    if owner == "tenant":
        assert response.status_code == 200, response.text
        assert effects == [selected.resolve()]
    else:
        assert (response.status_code, effects) == (403, []), response.text


def test_off_transcript_roots_come_from_container_caller_not_path_agent(daemon):
    from fastapi import HTTPException, Request

    from pinky_daemon.api_models import TransportTranscriptPathRequest

    d = daemon("off")
    work = _config_workdirs(d, "container")
    registered, real = work["tenant"]
    encoded = re.sub(r"[^a-zA-Z0-9]", "-", str(registered))
    selected = real / ".claude-container" / "projects" / encoded / "fixture.jsonl"
    selected.parent.mkdir(parents=True)
    selected.write_text("{}\n")
    effects = []
    session = SimpleNamespace(set_transcript_path=lambda file, **kw: effects.append(file))
    d.app.state.broker.register_streaming("normal", session, label="main")
    assert d.agents.get("normal").isolation_mode == "local"
    path = "/agents/normal/transport/transcript-path"
    body = {"transcript_path": str(selected), "session_id": "fixture", "label": "main"}
    client = TestClient(d.app)
    try:
        response = client.post(path, headers=signed(d, "POST", path), json=body)
    finally:
        client.close()
    # The legacy cross-agent guard denies this request even in off mode.
    assert (response.status_code, effects) == (403, []), response.text
    request = Request({
        "type": "http", "method": "POST", "path": path, "headers": [],
        "state": {"internal_caller": "tenant"},
    })
    handler = closure(d.app, "transport_transcript_path")
    try:
        result = asyncio.run(handler(
            "normal", TransportTranscriptPathRequest(**body), request))
    except HTTPException as exc:
        pytest.fail(f"Handler rejected the caller's config root: {exc.status_code}: {exc.detail}")
    assert result["ok"] is True
    assert effects == [selected.resolve()]


@pytest.mark.parametrize("mode", ["off", "shadow", "enforce"])
@pytest.mark.parametrize("per_agent", [False, True])
def test_isolated_transcript_never_owns_codex_sessions(daemon, monkeypatch, mode, per_agent):
    from pinky_daemon.codex_home import codex_home_for

    d = daemon(mode)
    monkeypatch.setenv("PINKY_CODEX_PER_AGENT_HOME", "1" if per_agent else "0")
    encoded = re.sub(r"[^a-zA-Z0-9]", "-", d.agents.get("tenant").working_dir)
    selected = codex_home_for(d.agents.get("tenant")) / "sessions" / encoded / "fixture.jsonl"
    selected.parent.mkdir(parents=True)
    selected.write_text("{}\n")
    effects = []
    session = SimpleNamespace(set_transcript_path=lambda file, **kw: effects.append(file))
    d.app.state.broker.register_streaming("tenant", session, label="main")
    path = "/agents/tenant/transport/transcript-path"
    client = TestClient(d.app)
    try:
        response = client.post(path, headers=signed(d, "POST", path), json={
            "transcript_path": str(selected), "session_id": "fixture", "label": "main"})
    finally:
        client.close()
    assert (response.status_code, effects) == (403, []), response.text
    assert "caller's own project" in response.json()["detail"]


@pytest.mark.parametrize("mode", ["off", "shadow", "enforce"])
@pytest.mark.parametrize("kind", ["photo", "document", "video"])
@pytest.mark.parametrize("failure,filesystem_call", [
    pytest.param("broken-symlink", None, id="broken-symlink"),
    pytest.param("not-directory", None, id="not-directory"),
    pytest.param("loop", None, id="loop"),
    pytest.param("permission", "realpath", id="permission"),
    pytest.param("runtime", "realpath", id="runtime"),
    pytest.param("permission", "is-file", id="permission-is-file"),
    pytest.param("runtime", "is-file", id="runtime-is-file"),
    pytest.param("oserror", "realpath", id="oserror-realpath"),
    pytest.param("oserror", "is-file", id="oserror-is-file"),
])
def test_isolated_media_resolve_errors_are_rejected(
    daemon, monkeypatch, capsys, mode, kind, failure, filesystem_call,
):
    d = daemon(mode)
    selected = d.root / "tenant" / "invalid"
    if failure == "broken-symlink":
        selected.symlink_to(selected.with_name("missing"))
    elif failure == "not-directory":
        selected.write_text("fixture")
        selected /= "child"
    elif failure == "loop":
        other = selected.with_name("other")
        selected.symlink_to(other)
        other.symlink_to(selected)
    else:
        selected.write_text("fixture")
        forms = {str(selected), str(selected.resolve(strict=True))}
        error = {"permission": PermissionError, "runtime": RuntimeError, "oserror": OSError}[failure]
        if filesystem_call == "realpath":
            original_realpath = os.path.realpath

            def realpath(path, *args, **kwargs):
                if os.fsdecode(path) in forms:
                    raise error("fixture filesystem error")
                return original_realpath(path, *args, **kwargs)

            monkeypatch.setattr(os.path, "realpath", realpath)
        else:
            original_is_file = Path.is_file

            def is_file(path, *args, **kwargs):
                if str(path) in forms:
                    raise error("fixture filesystem error")
                return original_is_file(path, *args, **kwargs)

            monkeypatch.setattr(Path, "is_file", is_file)
    adapters, attempts, stopped = [], [], []
    replace_cell(monkeypatch, closure(d.app, "_send_file_message"),
                 "_get_platform_adapter", lambda *args: adapters.append(args))
    route = closure(d.app, "_broker_send_file_route")
    original_log = dict(zip(route.__code__.co_freevars, route.__closure__))[
        "_outreach_attempt_log"
    ].cell_contents

    def log(**kwargs):
        attempts.append(kwargs)
        original_log(**kwargs)

    replace_cell(monkeypatch, route, "_outreach_attempt_log", log)
    monkeypatch.setattr(d.app.state.broker, "_stop_typing", lambda *args: stopped.append(args))
    path = f"/broker/send-{kind}"
    client = TestClient(d.app, raise_server_exceptions=False)
    try:
        response = client.post(path, headers=signed(d, "POST", path), json={
            "agent_name": "tenant", "chat_id": "fixture", "file_path": str(selected)})
    finally:
        client.close()
    assert response.status_code == 400, response.text
    assert adapters == []
    assert len(attempts) == 1 and attempts[0]["outcome"] == "rejected"
    assert stopped == [("tenant", "fixture")]
    logged = capsys.readouterr().err
    assert "outreach-attempt: agent=tenant" in logged and "outcome=rejected" in logged
    if filesystem_call is not None:
        assert error.__name__ in response.text and f"error={error.__name__}:" in logged
