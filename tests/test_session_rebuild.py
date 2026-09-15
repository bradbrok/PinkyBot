"""Session replacement and final-config contracts through real API/broker routes."""

from __future__ import annotations

import asyncio
import copy
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import HTTPException
from starlette.requests import Request

from pinky_daemon.api import create_api
from pinky_daemon.api_models import UpdateAgentRequest
from pinky_daemon.auth import SESSION_COOKIE_NAME, create_session_cookie
from pinky_daemon.broker import BrokerMessage
from pinky_daemon.codex_session import CodexSession
from pinky_daemon.codex_tmux_session import CodexTmuxSession
from pinky_daemon.streaming_session import StreamingSession, StreamingSessionConfig
from pinky_daemon.tmux_session import TmuxSession
from pinky_daemon.transport_state import SessionState

CLASSES = {
    ("claude_sdk", "sdk"): StreamingSession,
    ("claude_sdk", "tmux"): TmuxSession,
    ("codex_cli", "sdk"): CodexSession,
    ("codex_cli", "tmux"): CodexTmuxSession,
}
COMBINATIONS = list(CLASSES)


@pytest.fixture
async def harness(tmp_path, monkeypatch):
    monkeypatch.setenv("PINKY_SESSION_CLASS_REBUILD", "1")
    monkeypatch.setenv("PINKY_MODEL_RUNTIME_GUARD", "0")
    app = create_api(default_working_dir=str(tmp_path), db_path=str(tmp_path / "api.db"))
    trace = []
    live = set()
    launch_configs = []
    control = SimpleNamespace(preflight_failure=None, connect_failure=False, pause=None)

    def preflight(ss):
        trace.append(("preflight", ss))
        if type(ss) is control.preflight_failure:
            raise RuntimeError("target prerequisites unavailable")

    async def connect(ss, **kwargs):
        trace.append(("connect", ss))
        launch_configs.append((ss, copy.copy(ss._config)))
        if control.pause:
            control.pause[0].set()
            await control.pause[1].wait()
        if control.connect_failure:
            raise RuntimeError("candidate startup failed")
        ss._state_machine._state = SessionState.CONNECTED
        live.add(id(ss))

    async def disconnect(ss):
        trace.append(("disconnect", ss))
        ss._state_machine._state = SessionState.DEAD
        live.discard(id(ss))

    async def send(ss, prompt, **kwargs):
        trace.append(("send", ss))
        return True

    async def apply_model(ss, model):
        trace.append(("model", ss))
        return "applied"

    for cls in CLASSES.values():
        monkeypatch.setattr(cls, "connect", connect)
        monkeypatch.setattr(cls, "disconnect", disconnect)
        monkeypatch.setattr(cls, "send", send)
        monkeypatch.setattr(cls, "_preflight_transport_replacement", preflight)
        if hasattr(cls, "apply_model_live"):
            monkeypatch.setattr(cls, "apply_model_live", apply_model)
    monkeypatch.setattr(app.state.broker, "_send_message", AsyncMock())
    monkeypatch.setattr(app.state.broker, "_start_typing", AsyncMock())

    def seed(key=("claude_sdk", "sdk"), label="main"):
        runtime, transport = key
        workdir = tmp_path / "sample"
        workdir.mkdir(exist_ok=True)
        app.state.agents.register(
            "sample", runtime=runtime, transport=transport, model="sonnet",
            working_dir=str(workdir),
        )
        ss = CLASSES[key](StreamingSessionConfig(
            agent_name="sample", label=label, working_dir=str(workdir), model="sonnet",
            resume_handle="old-handle",
        ))
        ss._state_machine._state = SessionState.CONNECTED
        ss.resume_handle = "old-handle"
        ss._client = SimpleNamespace(
            get_context_usage=AsyncMock(return_value={"maxTokens": 200_000}),
            set_model=AsyncMock(), query=AsyncMock(),
        ) if key == ("claude_sdk", "sdk") else None
        app.state.broker.register_streaming("sample", ss, label=label)
        app.state.agents.set_streaming_session_id("sample", "old-handle", label=label)
        app.state.agents.set_context(
            "sample", task="Saved work", metadata={"source": "save_my_context"},
            updated_by="old-handle",
        )
        live.add(id(ss))
        return ss

    def target(key):
        app.state.agents.register("sample", runtime=key[0], transport=key[1])

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
        cookies={SESSION_COOKIE_NAME: create_session_cookie(os.environ["PINKY_SESSION_SECRET"])},
    ) as client:
        yield SimpleNamespace(
            app=app, client=client, seed=seed, target=target,
            trace=trace, live=live, configs=launch_configs, control=control,
        )


@pytest.mark.parametrize("source", COMBINATIONS)
@pytest.mark.parametrize("destination", COMBINATIONS)
async def test_force_restart_selects_target_class(harness, source, destination):
    h = harness
    old = h.seed(source)
    h.target(destination)
    response = await h.client.post("/admin/force-restart-agent/sample")
    assert response.status_code == 200, response.text
    current = h.app.state.broker._streaming["sample"]["main"]
    assert type(current) is CLASSES[destination]
    assert (current is old) == (source == destination)
    assert h.live == {id(current)}


@pytest.mark.parametrize("source", COMBINATIONS)
@pytest.mark.parametrize("route", ["ensure", "chat", "broker", "model"])
async def test_connected_handoffs_rebuild_before_use(harness, source, route):
    h = harness
    old = h.seed(source)
    destination = ("codex_cli" if source[0] == "claude_sdk" else "claude_sdk", "tmux")
    h.target(destination)
    if route == "ensure":
        await h.app.state.broker._ensure_session_callback("sample", label="main")
    elif route == "broker":
        await h.app.state.broker._route_streaming("sample", BrokerMessage(
            platform="web", chat_id="web", sender_name="User", sender_id="user",
            content="Continue", agent_name="sample",
        ))
    else:
        path = "streaming/model" if route == "model" else "chat"
        payload = {"model": "sonnet"} if route == "model" else {"content": "Continue"}
        response = await h.client.post(f"/agents/sample/{path}", json=payload)
        assert response.status_code == 200, response.text
    current = h.app.state.broker._streaming["sample"]["main"]
    assert type(current) is CLASSES[destination]
    assert current is not old
    assert not any(action in {"send", "model"} and ss is old for action, ss in h.trace)
    if old._client:
        old._client.set_model.assert_not_awaited()


@pytest.mark.parametrize("source,destination", [
    (("claude_sdk", "sdk"), ("codex_cli", "tmux")),
    (("codex_cli", "tmux"), ("claude_sdk", "sdk")),
])
async def test_target_preflight_failure_preserves_old_session(harness, source, destination):
    h = harness
    old = h.seed(source)
    h.target(destination)
    h.control.preflight_failure = CLASSES[destination]
    response = await h.client.post("/admin/force-restart-agent/sample")
    assert response.status_code >= 400
    assert h.app.state.broker._streaming["sample"]["main"] is old
    assert not any(action == "disconnect" for action, _ in h.trace)
    assert h.app.state.agents.get_streaming_session_id("sample") == "old-handle"


async def test_reverse_rebuild_ignores_old_launch_prerequisites(harness):
    h = harness
    old = h.seed(("codex_cli", "tmux"))
    h.target(("claude_sdk", "sdk"))
    h.control.preflight_failure = type(old)
    response = await h.client.post("/admin/force-restart-agent/sample")
    assert response.status_code == 200, response.text
    assert type(h.app.state.broker._streaming["sample"]["main"]) is StreamingSession


async def test_failed_replacement_retains_sibling(harness):
    h = harness
    h.seed()
    sibling = h.seed(label="secondary")
    h.target(("codex_cli", "tmux"))
    h.control.connect_failure = True
    response = await h.client.post("/admin/force-restart-agent/sample")
    assert response.status_code >= 400
    assert h.app.state.broker._streaming.get("sample", {}).get("secondary") is sibling


@pytest.mark.parametrize("disabled", [True, False], ids=["disabled", "deleted"])
async def test_absent_builder_never_reports_restart_success(harness, disabled):
    h = harness
    h.seed()
    if disabled:
        h.app.state.agents.register("sample", enabled=False)
    else:
        h.app.state.agents._db.execute("DELETE FROM agents WHERE name='sample'")
        h.app.state.agents._db.commit()
    response = await h.client.post("/admin/force-restart-agent/sample")
    assert response.status_code >= 400
    assert not any(action == "connect" for action, _ in h.trace)


@pytest.mark.parametrize("busy", [True, False], ids=["processing", "guarded"])
async def test_inbound_mismatch_refuses_without_destroying_work(harness, busy):
    h = harness
    old = h.seed()
    h.target(("codex_cli", "tmux"))
    old._processing = busy
    if not busy:
        h.app.state.agents._db.execute("DELETE FROM agent_contexts WHERE agent_name='sample'")
        h.app.state.agents._db.commit()
    response = await h.client.post("/agents/sample/chat", json={"content": "Continue"})
    assert response.status_code == 409, response.text
    assert h.app.state.broker._streaming["sample"]["main"] is old
    assert not any(action in {"connect", "disconnect", "send"} for action, _ in h.trace)


@pytest.mark.parametrize("destination", COMBINATIONS)
async def test_new_config_gets_fresh_intent_and_label(harness, destination):
    h = harness
    source = ("claude_sdk" if destination[0] == "codex_cli" else "codex_cli", "sdk")
    old = h.seed(source)
    h.target(destination)
    response = await h.client.post("/admin/force-restart-agent/sample")
    assert response.status_code == 200, response.text
    candidate, config = h.configs[-1]
    assert candidate is not old
    assert config.force_fresh_context_once is True
    assert config.resume_handle == ""
    assert config.restart_reason == "force_restart"
    assert config.label == "main"
    assert "Saved work" in config.wake_context


async def test_ensure_and_restart_serialize_one_replacement(harness):
    h = harness
    h.seed()
    h.target(("codex_cli", "tmux"))
    entered, release = asyncio.Event(), asyncio.Event()
    h.control.pause = (entered, release)
    restart = asyncio.create_task(h.client.post("/admin/force-restart-agent/sample"))
    await asyncio.wait_for(entered.wait(), 2)
    ensure = asyncio.create_task(h.app.state.broker._ensure_session_callback("sample"))
    try:
        for _ in range(5):
            await asyncio.sleep(0)
    finally:
        release.set()
    response, waiter = await asyncio.gather(restart, ensure)
    assert response.status_code == 200, response.text
    current = h.app.state.broker._streaming["sample"]["main"]
    assert waiter is current
    assert type(current) is CodexTmuxSession
    assert len([1 for action, _ in h.trace if action == "connect"]) == 1
    assert h.live == {id(current)}


@pytest.mark.parametrize("runtime", ["unexpected", "opencode"])
async def test_connected_ensure_preserves_runtime_refusal(harness, runtime):
    h = harness
    old = h.seed()
    h.app.state.agents.register("sample", runtime=runtime)
    with pytest.raises(HTTPException):
        await h.app.state.broker._ensure_session_callback("sample")
    assert h.app.state.broker._streaming["sample"]["main"] is old
    assert not any(action in {"connect", "disconnect"} for action, _ in h.trace)


@pytest.mark.parametrize("flag", [None, "0"])
async def test_rebuild_default_off_preserves_retained_identity(harness, monkeypatch, flag):
    h = harness
    old = h.seed()
    h.target(("codex_cli", "tmux"))
    if flag is None:
        monkeypatch.delenv("PINKY_SESSION_CLASS_REBUILD", raising=False)
    else:
        monkeypatch.setenv("PINKY_SESSION_CLASS_REBUILD", flag)
    response = await h.client.post("/admin/force-restart-agent/sample")
    assert response.status_code == 200, response.text
    assert h.app.state.broker._streaming["sample"]["main"] is old


@pytest.mark.parametrize("runtime,model", [
    ("claude_sdk", "gpt-5.6-sol"), ("codex_cli", "claude-opus-4-8"),
])
@pytest.mark.parametrize("route", ["put", "live"])
async def test_model_guard_rejects_merged_direct_pair_independently(
    harness, monkeypatch, runtime, model, route,
):
    h = harness
    h.seed((runtime, "sdk"))
    monkeypatch.setenv("PINKY_SESSION_CLASS_REBUILD", "0")
    monkeypatch.setenv("PINKY_MODEL_RUNTIME_GUARD", "1")
    before = h.app.state.agents.get("sample").model
    if route == "put":
        response = await h.client.put("/agents/sample", json={"model": model})
    else:
        response = await h.client.post("/agents/sample/streaming/model", json={"model": model})
    assert response.status_code == 422, response.text
    assert h.app.state.agents.get("sample").model == before
    assert not any(action in {"connect", "disconnect", "model"} for action, _ in h.trace)


@pytest.mark.parametrize("custom,model", [
    (True, "gpt-custom-model"), (False, "future-model-id"),
])
async def test_custom_and_unknown_model_policy(harness, monkeypatch, custom, model):
    h = harness
    h.seed()
    monkeypatch.setenv("PINKY_MODEL_RUNTIME_GUARD", "1")
    payload = {"model": model}
    if custom:
        payload.update(provider_url="https://proxy.example/v1", provider_model=model)
    response = await h.client.put("/agents/sample", json=payload)
    assert response.status_code == 200, response.text


async def test_provider_override_validates_effective_model(harness, monkeypatch):
    h = harness
    h.seed()
    h.app.state.agents.register("sample", provider_model="gpt-5.6-sol")
    monkeypatch.setenv("PINKY_MODEL_RUNTIME_GUARD", "1")
    response = await h.client.put("/agents/sample", json={"model": "sonnet"})
    assert response.status_code == 422, response.text


async def test_unrelated_edit_of_legacy_incompatible_record(harness, monkeypatch):
    h = harness
    h.seed()
    h.app.state.agents.register("sample", model="gpt-5.6-sol")
    monkeypatch.setenv("PINKY_MODEL_RUNTIME_GUARD", "1")
    response = await h.client.put("/agents/sample", json={"display_name": "Updated label"})
    assert response.status_code == 200, response.text


async def test_old_cleanup_failure_prevents_target_spawn(harness, monkeypatch):
    h = harness
    old = h.seed()
    h.target(("codex_cli", "tmux"))
    monkeypatch.setattr(old, "disconnect", AsyncMock(side_effect=RuntimeError("still live")))
    response = await h.client.post("/admin/force-restart-agent/sample")
    assert response.status_code >= 400
    assert not any(action == "connect" for action, _ in h.trace)
    assert h.live == {id(old)}


async def test_late_old_resume_callback_cannot_overwrite_new_identity(harness):
    h = harness
    h.seed()
    h.app.state.broker.unregister_streaming("sample", label="main")
    old = await h.app.state.broker._ensure_session_callback("sample")
    callback = old._on_resume_handle
    assert callable(callback)
    new = h.seed(("codex_cli", "tmux"))
    h.app.state.agents.set_streaming_session_id("sample", "new-thread", label="main")
    await callback("sample", "stale-sdk-handle")
    assert h.app.state.broker._streaming["sample"]["main"] is new
    assert h.app.state.agents.get_streaming_session_id("sample") == "new-thread"


@pytest.mark.parametrize("key", [("claude_sdk", "tmux"), ("codex_cli", "tmux")])
async def test_transcript_present_rebuild_uses_fresh_command(harness, monkeypatch, key):
    h = harness
    source = ("claude_sdk" if key[0] == "codex_cli" else "codex_cli", "sdk")
    h.seed(source)
    h.target(key)
    monkeypatch.setattr(CLASSES[key], "_has_prior_transcript", lambda self: True)
    response = await h.client.post("/admin/force-restart-agent/sample")
    assert response.status_code == 200, response.text
    new = h.app.state.broker._streaming["sample"]["main"]
    assert type(new) is CLASSES[key]
    command = new._build_claude_cmd()
    assert "--continue" not in command
    assert "resume --last" not in command
    assert ("codex" in command) == (key[0] == "codex_cli")


async def test_broker_waiter_resolves_replaced_object(harness, monkeypatch):
    h = harness
    old = h.seed()
    old._state_machine._state = SessionState.RECONNECTING
    monkeypatch.setattr("pinky_daemon.broker._INBOUND_RECONNECT_WAIT_SEC", 0.05)
    monkeypatch.setattr("pinky_daemon.broker._INBOUND_RECONNECT_POLL_SEC", 0.001)
    route = asyncio.create_task(h.app.state.broker._route_streaming("sample", BrokerMessage(
        platform="web", chat_id="web", sender_name="User", sender_id="user",
        content="Continue", agent_name="sample",
    )))
    await asyncio.sleep(0)
    new = h.seed()
    assert new is not old
    delivered = await route
    assert delivered is True
    assert ("send", new) in h.trace
    assert ("send", old) not in h.trace


async def test_config_write_waits_for_replacement_snapshot(harness):
    h = harness
    h.seed()
    h.target(("codex_cli", "tmux"))
    entered, release = asyncio.Event(), asyncio.Event()
    h.control.pause = (entered, release)
    restart = asyncio.create_task(h.client.post("/admin/force-restart-agent/sample"))
    await asyncio.wait_for(entered.wait(), 2)
    endpoint = next(route.endpoint for route in h.app.routes
                    if getattr(route, "path", "") == "/agents/{name}"
                    and "PUT" in getattr(route, "methods", set()))
    update = asyncio.create_task(endpoint(
        "sample", UpdateAgentRequest(runtime="claude_sdk"), Request({"type": "http"}),
    ))
    for _ in range(5):
        await asyncio.sleep(0)
    write_completed_during_connect = update.done()
    release.set()
    await asyncio.gather(restart, update)
    assert not write_completed_during_connect


async def test_late_failed_attempt_cannot_unregister_newer_session(harness):
    h = harness
    h.seed()
    h.target(("codex_cli", "tmux"))
    entered, release = asyncio.Event(), asyncio.Event()
    h.control.pause = (entered, release)
    h.control.connect_failure = True
    restart = asyncio.create_task(h.client.post("/admin/force-restart-agent/sample"))
    await asyncio.wait_for(entered.wait(), 2)
    newer = h.seed(("codex_cli", "tmux"))
    release.set()
    response = await restart
    assert response.status_code >= 400
    assert h.app.state.broker._streaming.get("sample", {}).get("main") is newer


@pytest.mark.parametrize("provider_source", ["ref", "default"])
async def test_effective_provider_model_override_rejected(harness, monkeypatch, provider_source):
    h = harness
    h.seed()
    h.app.state.agents._db.execute(
        "INSERT INTO providers (id, name, provider_url, provider_key, provider_model, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("direct", "Direct provider", "https://api.anthropic.com", "", "gpt-5.6-sol", 1, 1),
    )
    h.app.state.agents._db.commit()
    if provider_source == "ref":
        h.app.state.agents.register("sample", provider_ref="direct")
    else:
        h.app.state.agents.set_setting("default_provider_ref", "direct")
    monkeypatch.setenv("PINKY_MODEL_RUNTIME_GUARD", "1")
    response = await h.client.put("/agents/sample", json={"model": "sonnet"})
    assert response.status_code == 422, response.text


async def test_runtime_only_put_validates_merged_model(harness, monkeypatch):
    h = harness
    h.seed(("codex_cli", "sdk"))
    h.app.state.agents.register("sample", model="gpt-5.6-sol")
    monkeypatch.setenv("PINKY_MODEL_RUNTIME_GUARD", "1")
    response = await h.client.put("/agents/sample", json={"runtime": "claude_sdk"})
    assert response.status_code == 422, response.text
    assert h.app.state.agents.get("sample").runtime == "codex_cli"


@pytest.mark.parametrize("flag", [None, "0"])
async def test_model_guard_default_off_independent_of_rebuild(harness, monkeypatch, flag):
    h = harness
    h.seed()
    if flag is None:
        monkeypatch.delenv("PINKY_MODEL_RUNTIME_GUARD", raising=False)
    else:
        monkeypatch.setenv("PINKY_MODEL_RUNTIME_GUARD", flag)
    response = await h.client.put("/agents/sample", json={"model": "gpt-5.6-sol"})
    assert response.status_code == 200, response.text


async def test_mapped_label_rebuild_preserves_main_and_delivers_to_replacement(harness):
    h = harness
    main = h.seed()
    old = h.seed(label="secondary")
    h.app.state.agents.set_channel_session("sample", "channel", "secondary")
    h.target(("codex_cli", "tmux"))
    delivered = await h.app.state.broker._route_streaming("sample", BrokerMessage(
        platform="web", chat_id="channel", sender_name="User", sender_id="user",
        content="Continue", agent_name="sample",
    ))
    sessions = h.app.state.broker._streaming["sample"]
    assert delivered is True
    assert sessions["main"] is main
    assert type(sessions["secondary"]) is CodexTmuxSession
    assert sessions["secondary"] is not old
    assert ("send", sessions["secondary"]) in h.trace
    assert ("send", main) not in h.trace


async def test_catalog_read_failure_is_not_unknown_model_allow(harness, monkeypatch):
    h = harness
    h.seed()
    monkeypatch.setenv("PINKY_MODEL_RUNTIME_GUARD", "1")
    monkeypatch.setattr(h.app.state.agents, "get_model", lambda _: (_ for _ in ()).throw(
        RuntimeError("catalog unavailable"),
    ))
    before = h.app.state.agents.get("sample").model
    try:
        response = await h.client.put("/agents/sample", json={"model": "future-model-id"})
    except RuntimeError as exc:
        assert "catalog unavailable" in str(exc)
    else:
        assert response.status_code >= 500, response.text
    assert h.app.state.agents.get("sample").model == before


async def test_delivery_rechecks_class_after_attachment_download(harness, monkeypatch):
    h = harness
    old = h.seed()

    async def download(*args):
        h.target(("codex_cli", "tmux"))

    monkeypatch.setattr(h.app.state.broker, "_download_photo_attachments", download)
    assert await h.app.state.broker._route_streaming("sample", BrokerMessage(
        platform="web", chat_id="web", sender_name="User", sender_id="user",
        content="Continue", agent_name="sample",
    ))
    current = h.app.state.broker._streaming["sample"]["main"]
    assert type(current) is CodexTmuxSession
    assert ("send", current) in h.trace
    assert ("send", old) not in h.trace


async def test_custom_endpoint_keeps_known_foreign_model_id(harness, monkeypatch):
    h = harness
    h.seed()
    monkeypatch.setenv("PINKY_MODEL_RUNTIME_GUARD", "1")
    response = await h.client.put("/agents/sample", json={
        "provider_url": "https://proxy.example/v1", "provider_model": "gpt-5.6-sol",
    })
    assert response.status_code == 200
    assert h.app.state.agents.get("sample").provider_model == "gpt-5.6-sol"


async def test_model_rebuild_launches_requested_model_before_live_control(harness):
    h = harness
    old = h.seed()
    h.target(("codex_cli", "sdk"))
    response = await h.client.post("/agents/sample/streaming/model", json={"model": "gpt-5.6-sol"})
    assert response.status_code == 200, response.text
    current, config = h.configs[-1]
    assert type(current) is CodexSession
    assert config.model == "gpt-5.6-sol"
    assert h.app.state.agents.get("sample").model == "gpt-5.6-sol"
    old._client.set_model.assert_not_awaited()


async def test_model_route_refuses_transition_without_waiting_under_lifecycle_lock(harness):
    h = harness
    old = h.seed()
    old._state_machine._state = SessionState.RECONNECTING
    response = await asyncio.wait_for(h.client.post(
        "/agents/sample/streaming/model", json={"model": "sonnet"},
    ), timeout=1)
    assert response.status_code == 409
    assert not any(action in {"connect", "disconnect", "model"} for action, _ in h.trace)


async def test_failed_teardown_keeps_old_identity_for_cleanup(harness, monkeypatch):
    h = harness
    old = h.seed()
    h.target(("codex_cli", "tmux"))

    async def disconnect():
        old._state_machine._state = SessionState.DEAD
        raise RuntimeError("child cleanup uncertain")

    monkeypatch.setattr(old, "disconnect", disconnect)
    response = await h.client.post("/admin/force-restart-agent/sample")
    assert response.status_code >= 400
    assert h.app.state.broker._streaming["sample"]["main"] is old
    assert not any(action == "connect" for action, _ in h.trace)


async def test_failed_candidate_cleanup_blocks_next_spawn_until_cleanup_succeeds(harness, monkeypatch):
    h = harness
    h.seed()
    h.target(("codex_cli", "tmux"))
    h.control.connect_failure = True
    original_disconnect = CodexTmuxSession.disconnect
    monkeypatch.setattr(CodexTmuxSession, "disconnect", AsyncMock(side_effect=RuntimeError("still live")))
    response = await h.client.post("/admin/force-restart-agent/sample")
    assert response.status_code >= 400
    assert h.app.state.broker._streaming.get("sample", {}).get("main") is None
    with pytest.raises(RuntimeError, match="still live"):
        await h.app.state.broker._ensure_session_callback("sample")
    assert len([event for event in h.trace if event[0] == "connect"]) == 1
    monkeypatch.setattr(CodexTmuxSession, "disconnect", original_disconnect)
    h.control.connect_failure = False
    replacement = await h.app.state.broker._ensure_session_callback("sample")
    assert replacement.state == SessionState.CONNECTED
    assert len([event for event in h.trace if event[0] == "connect"]) == 2
