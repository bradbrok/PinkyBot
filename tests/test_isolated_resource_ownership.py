"""Object and file ownership expectations, without live files or adapters."""

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from pinky_daemon.streaming_session import StreamingSessionConfig
from pinky_daemon.tmux_session import TmuxSession
from tests.isolated_policy_support import closure, replace_cell, signed
from tests.isolated_policy_support import daemon as daemon

# Mode-off resource ownership for these handlers is follow-up work; enforce mode
# already denies the routes (test_held_resource_routes_are_denied_before_handlers).
# strict=True turns the repair into a visible XPASS failure so the marks are removed.
HELD_MEDIA = pytest.mark.xfail(
    raises=AssertionError, strict=True, reason="mode-off media file containment is held"
)
HELD_TRANSCRIPT = pytest.mark.xfail(
    raises=AssertionError, strict=True, reason="mode-off transcript ownership is held"
)

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


@pytest.mark.parametrize("kind", ["photo", "document", "video"])
@pytest.mark.parametrize(
    "target",
    ["own", pytest.param("peer", marks=HELD_MEDIA), pytest.param("symlink", marks=HELD_MEDIA)],
)
def test_media_attachment_requires_caller_ownership(daemon, monkeypatch, kind, target):
    d = daemon("off")
    own = d.root / "tenant" / "own.txt"
    peer = d.root / "peer" / "peer.txt"
    own.write_text("own harmless fixture")
    peer.write_text("peer harmless fixture")
    link = d.root / "tenant" / "alias.txt"
    link.symlink_to(peer)
    selected = {"own": own, "peer": peer, "symlink": link}[target]
    opened = []

    def send_file(*args, **kwargs):
        del kwargs
        path = Path(args[1])
        opened.append((path.resolve(), path.read_text()))
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
    if target == "own":
        assert response.status_code == 200, response.text
        assert opened == [(own.resolve(), "own harmless fixture")]
    else:
        assert (response.status_code, opened) == (403, []), (response.status_code, opened)


def test_nonisolated_media_foreign_file_baseline_observation(daemon, monkeypatch):
    d = daemon("off")
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
@pytest.mark.parametrize("owner", ["tenant", pytest.param("peer", marks=HELD_TRANSCRIPT)])
def test_transcript_path_belongs_to_agent_and_session(daemon, initial, owner):
    d = daemon("off")
    session = TmuxSession(
        StreamingSessionConfig(agent_name="tenant", working_dir=str(d.root / "tenant"))
    )
    selected_dir = Path.home() / ".claude/projects" / str(d.root / owner).replace("/", "-")
    selected_dir.mkdir(parents=True, exist_ok=True)
    selected = selected_dir / f"{owner}-session.jsonl"
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
    [
        "/broker/send-photo",
        "/broker/send-document",
        "/broker/send-video",
        "/agents/tenant/transport/transcript-path",
        "/agents/tenant/triggers",
    ],
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
