"""Storage holds gate work while leaving operator controls available."""

from __future__ import annotations

import asyncio
import inspect
import os
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from pinky_daemon.auth import (
    SESSION_COOKIE_NAME,
    build_internal_auth_headers,
    create_session_cookie,
)
from pinky_daemon.broker import BrokerMessage
from tests.recovery_test_support import closure_value, set_flags
from tests.recovery_test_support import lifecycle_harness as _lifecycle_harness


@pytest.fixture
async def lifecycle_harness(tmp_path, monkeypatch):
    root = tmp_path / "data"
    root.mkdir(mode=0o700)
    generator = inspect.unwrap(_lifecycle_harness)(root, monkeypatch)
    try:
        yield await anext(generator)
    finally:
        await generator.aclose()


@pytest.fixture
async def held_subject(lifecycle_harness, tmp_path):
    h = lifecycle_harness
    h.subject = h.seed()
    h.subject.send = AsyncMock(return_value=True)
    h.subject._client.query.side_effect = None
    h.subject._client.set_model = AsyncMock()
    root = Path(h.app.state.store_catalog.expected_root)
    primary = root / "primary"
    primary.mkdir()
    h.app.state.agents.register("primary", working_dir=str(primary), model="sonnet")
    h.app.state.agents.set_main_agent("primary")
    directory = root / "agents" / "sample" / "data"
    directory.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    h.escape = directory / "escape"
    h.escape.symlink_to(outside, target_is_directory=True)
    finding = h.app.state.store_catalog.inspect_layout().findings[0]
    h.finding = {**finding, "scope": "sample"}
    h.app.state.storage_holds = {"sample": [h.finding]}
    return h


def active(h, held):
    h.app.state.storage_holds = {"sample": [h.finding]} if held else {}


def refused(response, h):
    assert response.status_code == 409, response.text
    detail = response.json().get("detail")
    assert isinstance(detail, dict) and detail.get("code") == "storage_hold", response.text
    assert detail["findings"] == [h.finding]


def reported(body, h):
    hold = body.get("storage_hold")
    assert isinstance(hold, dict) and hold["held"] is True, body
    assert hold["findings"] == [h.finding]


@pytest.mark.parametrize("mode", ["off", "both"])
@pytest.mark.parametrize("held", [False, True])
@pytest.mark.parametrize("entry", ["chat", "forward", "wake", "compact", "message"])
async def test_http_delivery_checks_holds_before_connected_session_work(
    held_subject, monkeypatch, mode, held, entry,
):
    h = held_subject
    set_flags(monkeypatch, mode)
    active(h, held)
    routes = {
        "chat": ("/agents/sample/chat", {"content": "fixture turn"}),
        "forward": ("/agents/sample/forward", {"content": "fixture turn"}),
        "wake": ("/agents/sample/wake", {}),
        "compact": ("/agents/sample/streaming/compact", {}),
        "message": ("/agents/sample/message", {"from_agent": "primary", "message": "fixture turn"}),
    }
    path, payload = routes[entry]
    response = await h.client.post(path, json=payload)
    if held:
        refused(response, h)
        assert h.subject.send.await_count == h.subject._client.query.await_count == 0
    else:
        assert response.status_code == 200, response.text
        observed = h.subject._client.query.await_count if entry == "compact" else h.subject.send.await_count
        assert observed == 1


@pytest.mark.parametrize("mode", ["off", "both"])
@pytest.mark.parametrize("held", [False, True])
@pytest.mark.parametrize("entry", ["inbound", "internal"])
async def test_installed_broker_delegates_gate_existing_connected_sessions(
    held_subject, monkeypatch, mode, held, entry,
):
    h = held_subject
    set_flags(monkeypatch, mode)
    active(h, held)
    if entry == "internal":
        result = await h.app.state.broker.inject_agent_message("primary", "sample", "fixture turn")
        assert result.delivered is (not held)
    else:
        h.app.state.agents.approve_user("sample", "fixture-chat", "Fixture", "fixture")
        message = BrokerMessage(agent_name="sample", platform="telegram", chat_id="fixture-chat", sender_id="fixture-chat",
                                sender_name="Fixture", content="fixture turn")
        await h.app.state.broker.handle_inbound(message)
        for _ in range(8):
            await asyncio.sleep(0)
    assert h.subject.send.await_count == (0 if held else 1)


@pytest.mark.parametrize("name", ["handle_inbound", "inject_agent_message", "_route_streaming"])
async def test_live_app_broker_binding_contains_its_storage_guard(held_subject, name):
    broker = held_subject.app.state.broker
    installed = getattr(broker, name)
    assert inspect.isfunction(installed), "The live app must install its guarded delegate"
    cells = inspect.getclosurevars(installed).nonlocals
    assert callable(cells.get("_storage_hold_for"))
    assert any(inspect.ismethod(value) and value.__self__ is broker for value in cells.values())


@pytest.mark.parametrize("held", [False, True])
@pytest.mark.parametrize("entry", ["model", "provider", "update", "disable", "skills"])
async def test_operator_configuration_saves_without_waking_a_held_subject(
    held_subject, monkeypatch, held, entry,
):
    h = held_subject
    active(h, held)
    if entry == "skills":
        response = await h.client.post("/agents/sample/skills/apply")
    else:
        method, path, payload = {
            "model": ("POST", "/agents/sample/streaming/model", {"model": "opus"}),
            "provider": ("PUT", "/agents/sample/provider", {"provider_model": "fixture-model"}),
            "update": ("PUT", "/agents/sample", {"display_name": "Fixture subject"}),
            "disable": ("PUT", "/agents/sample", {"enabled": False}),
        }[entry]
        response = await h.client.request(method, path, json=payload)
    assert response.status_code == 200, response.text
    if held:
        reported(response.json(), h)
        assert len(h.clients) == 1
        assert h.subject._client.query.await_count == 0
        assert h.subject._client.set_model.await_count == 0
    current = h.app.state.agents.get("sample")
    if entry == "model":
        assert current.model == "opus"
        if not held:
            assert h.subject._client.set_model.await_count == 1
    elif entry == "provider":
        assert current.provider_model == "fixture-model"
    elif entry == "update":
        assert current.display_name == "Fixture subject"
    elif entry == "disable":
        assert current.enabled is False
    else:
        assert response.json()["applied"] is True
        assert response.json()["session_restarted"] is (not held)


@pytest.mark.parametrize("held", [False, True])
@pytest.mark.parametrize("entry", ["containerize", "decontainerize", "mode-change"])
async def test_container_runtime_actions_refuse_before_any_work_when_held(
    held_subject, monkeypatch, held, entry,
):
    from pinky_daemon import provisioning

    h = held_subject
    active(h, held)
    before = h.app.state.agents.get("sample").to_dict()
    lifecycle = closure_value(h.app, "_container_lifecycle")
    action = "containerize" if entry == "containerize" else "decontainerize"
    runtime = AsyncMock()
    monkeypatch.setattr(lifecycle, action, runtime)
    monkeypatch.setattr(provisioning, "container_runtime_enabled", lambda: True)
    monkeypatch.setattr(provisioning, "container_runtime_supported", lambda: True)
    if entry == "mode-change":
        response = await h.client.put("/agents/sample", json={
            "isolation_mode": "container", "container_image": "fixture/image:v1", "transport": "tmux",
        })
    else:
        response = await h.client.post(f"/agents/sample/{entry}", json={
            "force": True, "start": True, **({"image": "fixture/image:v1"} if entry == "containerize" else {}),
        })
        await asyncio.sleep(0)
    if held:
        refused(response, h)
        assert h.app.state.agents.get("sample").to_dict() == before
        assert runtime.await_count == 0
        assert not h.trace
    else:
        assert response.status_code == (200 if entry == "mode-change" else 202), response.text
        if entry == "mode-change":
            assert h.app.state.agents.get("sample").isolation_mode == "container"
        else:
            assert runtime.await_count == 1


@pytest.mark.parametrize("held", [False, True])
@pytest.mark.parametrize("entry", ["stop", "delete", "rename"])
async def test_explicit_operator_session_controls_remain_available(held_subject, held, entry):
    h = held_subject
    active(h, held)
    if entry == "stop":
        response = await h.client.post("/agents/sample/stop")
    else:
        h.seed(label="work")
        if entry == "delete":
            response = await h.client.delete("/agents/sample/streaming-sessions/work")
        else:
            response = await h.client.patch("/agents/sample/streaming-sessions/work", json={"label": "renamed"})
    assert response.status_code == 200, response.text
    assert h.subject.send.await_count == 0
    assert h.app.state.storage_holds == ({"sample": [h.finding]} if held else {})


@pytest.mark.parametrize("held", [False, True])
async def test_explicit_archive_skips_prompt_and_relaunch_while_held(held_subject, held):
    h = held_subject
    active(h, held)
    peer = h.subject._client
    response = await h.client.post("/agents/sample/streaming/archive")
    assert response.status_code == 200, response.text
    assert response.json()["archived"] is True
    if held:
        reported(response.json(), h)
        assert peer.query.await_count == 0
        assert len(h.clients) == 1
    else:
        assert peer.query.await_count == 1
        assert len(h.clients) == 2


@pytest.mark.parametrize("identity", ["owner", "viewer", "internal", "anonymous"])
async def test_start_anyway_requires_the_owner_and_keeps_the_registry_unchanged(
    held_subject, identity,
):
    h = held_subject
    before = h.app.state.agents.get("sample").to_dict()
    path = "/admin/agents/sample/storage/start-anyway"
    if identity != "owner":
        h.client.cookies.clear()
    if identity == "viewer":
        h.client.cookies.set(SESSION_COOKIE_NAME, create_session_cookie(
            os.environ["PINKY_SESSION_SECRET"], user="viewer",
        ))
    headers = build_internal_auth_headers(
        os.environ["PINKY_SESSION_SECRET"], agent_name="primary", method="POST", path=path,
    ) if identity == "internal" else {}
    response = await h.client.post(path, headers=headers)
    if identity == "owner":
        assert response.status_code == 200, response.text
        assert response.json()["storage_hold"]["overridden"] is True
        assert response.json()["storage_hold"]["findings"] == [h.finding]
    else:
        assert response.status_code in {401, 403}, response.text
        assert h.app.state.storage_hold_overrides == set()
    assert h.app.state.agents.get("sample").to_dict() == before


@pytest.mark.parametrize("removed", [False, True])
async def test_recheck_retains_unsafe_findings_or_clears_a_repaired_scope(held_subject, removed):
    h = held_subject
    if removed:
        h.escape.unlink()
    response = await h.client.post("/agents/sample/storage/recheck")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["cleared"] is removed
    if not removed:
        assert body["findings"] == [h.finding]
    h.subject.send.reset_mock()
    wake = await h.client.post("/agents/sample/wake")
    if removed:
        assert wake.status_code == 200, wake.text
        assert h.subject.send.await_count == 1
    else:
        refused(wake, h)
        assert h.subject.send.await_count == 0


@pytest.mark.parametrize("main", [False, True])
async def test_main_exemption_puts_the_finding_in_wake_context(held_subject, main):
    h = held_subject
    if main:
        h.app.state.agents.set_main_agent("sample")
    build = h.app.state._build_streaming_wake_context
    for _ in range(2):
        text = build("sample", commit=False)
        assert (h.finding["path"] in text) is main
    response = await h.client.post("/agents/sample/wake")
    if main:
        assert response.status_code == 200, response.text
        assert h.subject.send.await_count == 1
    else:
        refused(response, h)


async def test_hold_findings_are_visible_on_detail_health_and_update_status(held_subject):
    h = held_subject
    detail = await h.client.get("/agents/sample")
    reported(detail.json(), h)
    for path in ("/system/health", "/admin/update/status"):
        response = await h.client.get(path)
        assert response.status_code == 200
        holds = response.json().get("storage_holds")
        assert isinstance(holds, list)
        assert any(row["agent"] == "sample" and row["held"] and row["findings"] == [h.finding]
                   for row in holds)


async def test_owner_stop_command_on_a_held_route_still_executes(held_subject):
    h = held_subject
    h.app.state.agents.set_primary_user("fixture-chat", "Fixture")
    peer = h.subject._client
    h.app.state.broker._send_callback = AsyncMock(return_value={"sent": True})
    message = BrokerMessage(agent_name="sample", platform="telegram", chat_id="fixture-chat", sender_id="fixture-chat",
                            sender_name="Fixture", content="/stop")
    await h.app.state.broker.handle_inbound(message)
    assert peer.disconnect.await_count == 1
    assert h.subject.send.await_count == 0


@pytest.mark.parametrize("held", [False, True])
@pytest.mark.parametrize("entry", ["scheduler", "heartbeat", "watchdog", "mcp-notice", "restart-worker"])
async def test_automatic_callbacks_leave_a_held_transport_untouched(held_subject, held, entry):
    h = held_subject
    active(h, held)
    peer = h.subject._client
    callbacks = {
        "scheduler": ("_wake_callback", ("sample", "", "fixture turn")),
        "heartbeat": ("_heartbeat_resurrect", ("sample", "")),
        "watchdog": ("_watchdog_recover", ("sample", "main", "fixture recovery")),
        "mcp-notice": ("_watchdog_mcp_notice", ("sample", "fixture notice")),
        "restart-worker": ("_restart_streaming_session_after_response", ("sample",)),
    }
    name, args = callbacks[entry]
    if entry == "restart-worker":
        # The public restart endpoint owns the worker's handoff parameters.
        response = await h.client.post("/agents/sample/streaming/restart")
        if held:
            refused(response, h)
    else:
        callback = next(value for obj in (h.app.state.scheduler, h.app.state.watchdog)
                        for value in vars(obj).values()
                        if inspect.isfunction(value) and value.__name__ == name)
        await callback(*args)
    if held:
        assert peer.disconnect.await_count == 0
        assert peer.query.await_count == 0 and h.subject.send.await_count == 0
        assert len(h.clients) == 1 and not h.trace
    elif entry in {"scheduler", "mcp-notice"}:
        assert h.subject.send.await_count == 1


@pytest.mark.parametrize("held", [False, True])
async def test_automatic_shutdown_preserves_only_the_held_survivor(held_subject, monkeypatch, held):
    h = held_subject
    active(h, held)
    peer = h.subject._client
    shutdown = next(fn for fn in h.app.router.on_shutdown if fn.__name__ == "on_shutdown")
    await shutdown()
    assert peer.disconnect.await_count == (0 if held else 1)
    assert (h.app.state.broker.get_streaming_session("sample") is h.subject) is held


@pytest.mark.parametrize("main", ["", "primary", "sample"])
@pytest.mark.parametrize("registered", [False, True])
async def test_real_boot_installs_scoped_holds_and_keeps_management_running(
    held_subject, monkeypatch, capsys, main, registered,
):
    from pinky_daemon import api, tmux_session
    from pinky_daemon.store_catalog import StoreCatalogError
    from pinky_daemon.transport_state import SessionState

    h = held_subject
    root = Path(h.app.state.store_catalog.expected_root)
    registry = h.app.state.agents
    registry.set_main_agent(main)
    if not registered:
        # A retained directory cannot replace the removed registration.
        registry._db.execute("DELETE FROM agents WHERE name='sample'")
        registry._db.commit()
    h.app.state.agents._fire_trace.close()
    h.app.state.store_catalog.shutdown(deadline_seconds=10)
    monkeypatch.setenv("PINKY_LOG_ROTATION", "off")
    monkeypatch.setenv("PINKY_MCP_READINESS_CAP_SEC", "0")
    monkeypatch.setattr(api, "SHARED_MCP_ENABLED", False)
    monkeypatch.setattr(tmux_session, "reap_legacy_tmux_sessions", AsyncMock())
    monkeypatch.setattr(tmux_session, "reconcile_tmux_spawn_cleanup_debts", AsyncMock(return_value=(0, 0)))
    if not registered:
        created = []
        constructor = api.AgentRegistry

        def record_registry(*args, **kwargs):
            result = constructor(*args, **kwargs)
            created.append(result)
            return result

        monkeypatch.setattr(api, "AgentRegistry", record_registry)
        rejected = False
        try:
            api.create_api(db_path=str(root / "api.db"), default_working_dir=str(root))
        except StoreCatalogError:
            rejected = True
        finally:
            for fresh_registry in created:
                fresh_registry._fire_trace.close()
                fresh_registry._catalog.shutdown(deadline_seconds=10)
        assert rejected, "An unregistered directory must not become an inert storage hold"
        return
    try:
        app = api.create_api(db_path=str(root / "api.db"), default_working_dir=str(root))
    except StoreCatalogError as exc:
        pytest.fail(f"A scoped finding must allow application construction: {exc}")
    app.state.agents.set_main_agent(main)
    launches = []

    async def connect(ss):
        launches.append((ss.agent_name, ss._config.wake_context))
        ss._state_machine._state = SessionState.CONNECTED

    monkeypatch.setattr("pinky_daemon.streaming_session.StreamingSession.connect", connect)
    notify = AsyncMock(return_value=True)
    app.state.scheduler._owner_notify_callback = notify
    # The fixture registry's transports are SDK-backed; the real boot path
    # still builds and submits each selected session's initial context.
    try:
        async with app.router.lifespan_context(app):
            assert app.state.api_readiness.closed is False
            assert [name for name, _ in launches] == ([main] if main and (registered or main != "sample") else [])
            if registered and main != "sample":
                hold = closure_value(app, "_storage_hold_for")("sample")
                assert hold["held"] and hold["findings"][0]["path"] == h.finding["path"]
            if registered and main == "sample":
                assert h.finding["path"] in launches[0][1]
                assert h.finding["resolved_path"] in launches[0][1]
            assert notify.await_count == 1
            assert h.finding["path"] in notify.call_args.args[1]
    finally:
        app.state.agents._fire_trace.close()
        app.state.store_catalog.shutdown(deadline_seconds=10)
    lines = [line for line in capsys.readouterr().err.splitlines() if "STORAGE HOLD" in line]
    expected = "WARNING" if registered and main == "sample" else "ERROR"
    assert len(lines) == 1 and expected in lines[0]


async def test_an_owner_override_does_not_survive_a_fresh_application(held_subject):
    from pinky_daemon.api import create_api
    from pinky_daemon.store_catalog import StoreCatalogError

    h = held_subject
    response = await h.client.post("/admin/agents/sample/storage/start-anyway")
    assert response.status_code == 200, response.text
    root = Path(h.app.state.store_catalog.expected_root)
    h.app.state.agents._fire_trace.close()
    h.app.state.store_catalog.shutdown(deadline_seconds=10)
    try:
        fresh = create_api(db_path=str(root / "api.db"), default_working_dir=str(root))
    except StoreCatalogError as exc:
        pytest.fail(f"A scoped finding must allow a fresh application: {exc}")
    try:
        hold = closure_value(fresh, "_storage_hold_for")("sample")
        assert hold["held"] is True and hold["overridden"] is False
        assert hold["findings"][0]["path"] == h.finding["path"]
    finally:
        fresh.state.agents._fire_trace.close()
        fresh.state.store_catalog.shutdown(deadline_seconds=10)


@pytest.mark.parametrize("held", [False, True])
async def test_startup_alert_uses_the_daemon_sender_even_when_its_subject_is_held(
    held_subject, monkeypatch, held,
):
    from types import SimpleNamespace

    from pinky_daemon import api, tmux_session

    h = held_subject
    active(h, held)
    # Keep the same finding visible as a main exemption in the clean twin.
    h.app.state.storage_holds = {"sample": [h.finding]}
    h.app.state.agents.set_main_agent("" if held else "sample")
    h.app.state.agents.set_owner_notification_destinations([{
        "platform": "telegram", "account_id": "fixture-account",
        "conversation_id": "fixture-owner", "principal_id": "fixture-owner",
    }])
    h.app.state.agents.set_token("sample", "telegram", "fixture-token", settings={"account_id": "fixture-account"})
    sent = []

    def send(*args, **kwargs):
        sent.append((args, kwargs))
        return SimpleNamespace(id="fixture-delivery")

    callback = h.app.state.broker.send_callback
    cell = dict(zip(callback.__code__.co_freevars, callback.__closure__))["_send_text_message"]
    original = cell.cell_contents
    cell.cell_contents = send
    monkeypatch.setenv("PINKY_LOG_ROTATION", "off")
    monkeypatch.setenv("PINKY_MCP_READINESS_CAP_SEC", "0")
    monkeypatch.setattr(api, "SHARED_MCP_ENABLED", False)
    monkeypatch.setattr("pinky_daemon.pollers.BrokerTelegramPoller.start", AsyncMock())
    monkeypatch.setattr(tmux_session, "reap_legacy_tmux_sessions", AsyncMock())
    monkeypatch.setattr(tmux_session, "reconcile_tmux_spawn_cleanup_debts", AsyncMock(return_value=(0, 0)))
    try:
        async with h.app.router.lifespan_context(h.app):
            assert len(sent) == 1
            args, kwargs = sent[0]
            assert args[:3] == ("sample", "telegram", "fixture-owner")
            assert h.finding["path"] in args[3] and h.finding["resolved_path"] in args[3]
            assert kwargs["account_id"] == "fixture-account"
            assert h.subject.send.await_count == 0
            if held:
                assert not h.trace
    finally:
        cell.cell_contents = original


async def test_real_tenant_composition_keeps_scoped_looking_findings_fatal(held_subject, tmp_path, monkeypatch):
    from pinky_daemon import api
    from pinky_daemon.store_catalog import StoreCatalogError
    from pinky_daemon.store_manifest import derive_standalone_tenant_store_manifest

    h = held_subject
    h.escape.unlink()
    h.app.state.agents.register("sample", isolation_mode="unix_user", isolated=True)
    root = Path(h.app.state.store_catalog.expected_root)
    tenant = tmp_path / "tenant"
    directory = tenant / "agents/sample/data"
    directory.mkdir(parents=True)
    (directory / "escape").symlink_to(tmp_path / "outside", target_is_directory=True)
    manifest = derive_standalone_tenant_store_manifest(tenant / "keys.db")
    monkeypatch.setattr(api, "_derive_standalone_tenant_store_manifest", lambda name: manifest)
    h.app.state.agents._fire_trace.close()
    h.app.state.store_catalog.shutdown(deadline_seconds=10)
    with pytest.raises(StoreCatalogError, match="directory symlink escapes"):
        api.create_api(db_path=str(root / "api.db"), default_working_dir=str(root))


@pytest.mark.parametrize("held", [False, True])
async def test_boot_purge_keeps_the_held_legacy_record_and_resume_identity(held_subject, monkeypatch, held):
    from types import SimpleNamespace
    from unittest.mock import Mock

    from pinky_daemon import api, tmux_session

    h = held_subject
    active(h, held)
    h.app.state.agents.set_main_agent("")
    h.app.state.agents.set_streaming_session_id("sample", "fixture-survivor")
    manager = closure_value(h.app, "manager")
    monkeypatch.setattr(manager, "list", lambda: [SimpleNamespace(id="fixture-legacy", agent_name="sample")])
    delete = Mock()
    monkeypatch.setattr(manager, "delete", delete)
    monkeypatch.setenv("PINKY_LOG_ROTATION", "off")
    monkeypatch.setattr(api, "SHARED_MCP_ENABLED", False)
    monkeypatch.setattr(tmux_session, "reap_legacy_tmux_sessions", AsyncMock())
    monkeypatch.setattr(tmux_session, "reconcile_tmux_spawn_cleanup_debts", AsyncMock(return_value=(0, 0)))
    h.app.state.scheduler._owner_notify_callback = AsyncMock(return_value=True)
    peer = h.subject._client
    async with h.app.router.lifespan_context(h.app):
        assert delete.call_count == (0 if held else 1)
        assert h.app.state.agents.get_streaming_session_id("sample") == "fixture-survivor"
        assert peer.disconnect.await_count == 0
        assert h.subject.send.await_count == 0


@pytest.mark.parametrize("failure", ["incomplete", "fatal"])
async def test_recheck_never_clears_after_an_incomplete_or_fatal_scan(held_subject, monkeypatch, failure):
    from pinky_daemon.store_catalog import LayoutInspection

    h = held_subject
    if failure == "incomplete":
        def inspect_layout(**kwargs):
            raise PermissionError("fixture scan unavailable")
    else:
        def inspect_layout(**kwargs):
            return LayoutInspection([{**h.finding, "kind": "alias_of_registered_store", "scope": None}], [])
    monkeypatch.setattr(h.app.state.store_catalog, "inspect_layout", inspect_layout)
    response = await h.client.post("/agents/sample/storage/recheck")
    assert response.status_code == 200, response.text
    assert response.json()["cleared"] is False
    hold = closure_value(h.app, "_storage_hold_for")("sample")
    assert hold["held"] is True


@pytest.mark.parametrize("mode", ["off", "both"])
@pytest.mark.parametrize("held", [False, True])
@pytest.mark.parametrize("entry", ["prepare", "connect", "connect-unchecked", "start", "start-unchecked",
                                  "replace", "retained", "restart", "ensure", "ensure-idle",
                                  "ensure-cold", "deliver"])
async def test_core_lifecycle_boundaries_gate_before_transport_work(held_subject, monkeypatch, mode, held, entry):
    from fastapi import HTTPException

    from pinky_daemon.transport_state import SessionState

    h = held_subject
    active(h, held)
    set_flags(monkeypatch, mode)
    peer = h.subject._client
    connect = AsyncMock()
    agent = h.app.state.agents.get("sample")
    h.subject._launch_snapshot = closure_value(h.app, "_launch_fingerprint")(agent)
    h.subject._launch_provider_snapshot = closure_value(h.app, "_effective_launch_model")(agent)
    h.subject._launch_runtime, h.subject._launch_transport = "claude_sdk", "sdk"
    if entry in {"connect", "connect-unchecked", "retained", "ensure-idle"}:
        async def connected():
            h.subject._state_machine._state = SessionState.CONNECTED
        connect.side_effect = connected
        monkeypatch.setattr(h.subject, "connect", connect)
    if entry == "ensure-idle":
        h.subject._state_machine._state = SessionState.IDLE_SLEEPING
    if entry in {"ensure-cold", "start", "start-unchecked"}:
        h.app.state.broker.unregister_streaming("sample")
    name, args, kwargs = {
        "prepare": ("_prepare_streaming_session", ("sample",), {}),
        "connect": ("_connect_prepared_session", (h.subject,), {}),
        "connect-unchecked": ("_connect_prepared_session_unchecked", (h.subject,), {}),
        "start": ("_start_streaming_session", ("sample",), {}),
        "start-unchecked": ("_start_streaming_session_unchecked", ("sample",), {}),
        "replace": ("_replace_session_locked", ("sample", "main", h.subject), {"reason": "fixture"}),
        "retained": ("_connect_retained_session", ("sample", "main", h.subject), {}),
        "restart": ("_restart_compatible", ("sample", h.subject), {"configure": lambda: None, "reason": "fixture"}),
        "ensure": ("_ensure_streaming_session", ("sample",), {}),
        "ensure-idle": ("_ensure_streaming_session", ("sample",), {}),
        "ensure-cold": ("_ensure_streaming_session", ("sample",), {}),
        "deliver": ("_deliver_streaming", ("sample", "fixture turn"), {}),
    }[entry]
    call = closure_value(h.app, name)
    if held:
        with pytest.raises(HTTPException) as raised:
            await call(*args, **kwargs)
        assert raised.value.status_code == 409 and raised.value.detail["code"] == "storage_hold"
        assert raised.value.detail["findings"] == [h.finding]
        assert len(h.clients) == len(h.sessions) == 1
        assert connect.await_count == peer.disconnect.await_count == h.subject.send.await_count == 0
        assert not h.trace
    else:
        result = await call(*args, **kwargs)
        if entry in {"prepare", "start", "start-unchecked", "ensure-cold", "replace", "restart"}:
            assert len(h.sessions) >= (1 if entry == "restart" else 2)
            if entry != "prepare":
                assert len(h.clients) == 2
        elif entry in {"connect", "connect-unchecked", "retained", "ensure-idle"}:
            assert connect.await_count == 1
        elif entry == "deliver":
            assert result == (h.subject, True) and h.subject.send.await_count == 1
        else:
            assert result is h.subject


@pytest.mark.parametrize("held", [False, True])
@pytest.mark.parametrize("entry", ["fork", "clone", "send", "restart", "refresh", "refresh-all", "legacy-send", "legacy-send-agent", "spawn", "force-restart"])
async def test_legacy_and_worker_routes_use_the_same_subject_policy(held_subject, monkeypatch, held, entry):
    from types import SimpleNamespace
    from unittest.mock import Mock

    import claude_agent_sdk

    from pinky_daemon.sessions import Session

    h = held_subject
    active(h, held)
    manager = h.app.state.manager
    legacy = manager.create(session_id="fixture-legacy", agent_name="sample", session_type="main",
                            working_dir=h.subject._config.working_dir, model="sonnet")
    legacy._sdk_session_id = "fixture-transcript"
    manager.create(session_id="fixture-sender", agent_name="primary", model="sonnet")
    fork = Mock(return_value=SimpleNamespace(session_id="fixture-fork"))
    monkeypatch.setattr(claude_agent_sdk, "fork_session", fork)
    send = AsyncMock(return_value=SimpleNamespace(role="assistant", content="fixture answer", timestamp=1.0,
                                                duration_ms=0, error=""))
    monkeypatch.setattr(Session, "send", send)
    restart = AsyncMock(return_value=SimpleNamespace(summary="fixture", message_count=1, estimated_tokens_at_checkpoint=2))
    monkeypatch.setattr(legacy, "restart", restart)
    create = Mock(wraps=manager.create)
    refresh = Mock(wraps=manager.refresh)
    monkeypatch.setattr(manager, "create", create)
    monkeypatch.setattr(manager, "refresh", refresh)
    path, payload = {
        "fork": ("/sessions/fixture-legacy/fork", {}),
        "clone": ("/agents/sample/clone-worker", {"task": "fixture turn"}),
        "send": ("/sessions/fixture-legacy/message", {"content": "fixture turn"}),
        "restart": ("/sessions/fixture-legacy/restart", {}),
        "refresh": ("/sessions/fixture-legacy/refresh", {}),
        "refresh-all": ("/agents/sample/sessions/refresh", {}),
        "legacy-send": ("/sessions/fixture-sender/send", {"to": "fixture-legacy", "content": "fixture turn"}),
        "legacy-send-agent": ("/sessions/fixture-sender/send", {"to": "sample", "content": "fixture turn"}),
        "spawn": ("/agents/sample/sessions", {"session_id": "fixture-spawn", "session_type": "worker"}),
        "force-restart": ("/admin/force-restart-agent/sample", {"reason": "fixture recovery", "min_heartbeat_age_sec": 0}),
    }[entry]
    response = await h.client.post(path, json=payload)
    await asyncio.sleep(0)
    if held:
        refused(response, h)
        assert fork.call_count == create.call_count == refresh.call_count == 0
        assert send.await_count == restart.await_count == 0
        assert len(h.clients) == 1 and not h.trace
    else:
        assert response.status_code == 200, response.text
        if entry in {"fork", "clone"}:
            assert fork.call_count == 1
        if entry in {"clone", "spawn"}:
            assert create.call_count == 1
        if entry == "send":
            assert send.await_count == 1
        if entry == "restart":
            assert restart.await_count == 1
        if entry in {"refresh", "refresh-all"}:
            assert refresh.call_count == 1
        if entry == "force-restart":
            assert len(h.clients) == 2


@pytest.mark.parametrize("held", [False, True])
@pytest.mark.parametrize("entry", ["config", "liveness", "login-probe", "login-freeze"])
async def test_watchdog_never_probes_or_changes_a_held_terminal(held_subject, monkeypatch, held, entry):
    from types import SimpleNamespace

    from fastapi import HTTPException

    h = held_subject
    active(h, held)
    h.app.state.agents.register("sample", transport="tmux")
    tmux = SimpleNamespace(session_name="fixture-terminal", has_session=AsyncMock(return_value=True),
                           capture_pane=AsyncMock(return_value=SimpleNamespace(ok=True, stdout="fixture pane")),
                           rename_session=AsyncMock(return_value=SimpleNamespace(ok=True)))
    session = SimpleNamespace(_tmux=tmux)
    attr = {"config": "_config_fn", "liveness": "_tmux_liveness_fn",
            "login-probe": "_login_wall_probe_fn", "login-freeze": "_login_wall_freeze_fn"}[entry]
    callback = getattr(h.app.state.watchdog, attr)
    if entry == "config":
        assert callback("sample").enabled is (not held)
    elif held and entry == "login-freeze":
        with pytest.raises(HTTPException) as raised:
            await callback("sample", "main", session)
        assert raised.value.detail["code"] == "storage_hold"
    else:
        result = await callback("sample", "main", session)
        if held:
            assert result is None
        elif entry == "liveness":
            assert result == (True, "fixture-terminal")
        else:
            assert tmux.capture_pane.await_count == 1
    if held:
        assert tmux.has_session.await_count == tmux.capture_pane.await_count == tmux.rename_session.await_count == 0


@pytest.mark.parametrize("kind", ["directory", "database"])
async def test_daemon_starts_when_every_registered_subject_is_held(held_subject, monkeypatch, tmp_path, kind):
    from pinky_daemon import api, tmux_session
    from pinky_daemon.store_catalog import StoreCatalogError

    h = held_subject
    root = Path(h.app.state.store_catalog.expected_root)
    directory = root / "agents/primary/data"
    directory.mkdir(parents=True)
    target = tmp_path / "outside"
    name = "escape"
    if kind == "database":
        h.escape.unlink()
        target = target / "fixture-content"
        target.write_bytes(b"fixture")
        name += ".db"
        h.escape.with_name(name).symlink_to(target)
    (directory / name).symlink_to(target, target_is_directory=kind == "directory")
    h.app.state.agents.set_main_agent("")
    h.app.state.agents._fire_trace.close()
    h.app.state.store_catalog.shutdown(deadline_seconds=10)
    monkeypatch.setattr(api, "SHARED_MCP_ENABLED", False)
    monkeypatch.setenv("PINKY_LOG_ROTATION", "off")
    cleanup = AsyncMock()
    monkeypatch.setattr(tmux_session, "reap_legacy_tmux_sessions", cleanup)
    monkeypatch.setattr(tmux_session, "reconcile_tmux_spawn_cleanup_debts", AsyncMock(return_value=(0, 0)))
    try:
        app = api.create_api(db_path=str(root / "api.db"), default_working_dir=str(root))
    except StoreCatalogError as exc:
        pytest.fail(f"An entirely held registry must still start the daemon: {exc}")
    app.state.agents.set_main_agent("")
    app.state.scheduler._owner_notify_callback = AsyncMock(return_value=True)
    before = len(h.clients), len(h.sessions)
    async with app.router.lifespan_context(app):
        holds = closure_value(app, "_storage_hold_for")()
        assert {view["agent"] for view in holds if view["held"]} == {a.name for a in app.state.agents.list()}
        assert len(holds) == 2
        assert (len(h.clients), len(h.sessions)) == before
        assert app.state.api_readiness.closed is False
        assert app.state.scheduler._owner_notify_callback.await_count == 1
        assert cleanup.call_args.args[0].list() == []


@pytest.mark.parametrize("held", [False, True])
async def test_boot_cleanup_keeps_a_held_debt_and_reaps_an_unheld_debt(held_subject, monkeypatch, held):
    import time
    from types import SimpleNamespace

    from pinky_daemon import api, tmux_session

    h = held_subject
    active(h, held)
    root = Path(h.app.state.store_catalog.expected_root)
    h.app.state.agents.set_main_agent("")
    paths = {}
    for name in ("sample", "primary"):
        debt = tmux_session._TmuxSpawnCleanupDebt(
            agent_name=name, session_name=f"fixture-{name}", socket_name="fixture-server", socket_path="",
            tmux_binary="fixture-tmux", runner={"kind": "local"}, site="fixture", created_at=time.time(),
        )
        paths[name] = tmux_session._persist_tmux_spawn_cleanup_debt(root, debt)
    before = paths["sample"].read_bytes()
    controls = []

    def control(session_name, **kwargs):
        controls.append(session_name)
        return SimpleNamespace(kill_session=AsyncMock(return_value=SimpleNamespace(ok=True)))

    monkeypatch.setattr(tmux_session, "production_tmux_control", control)
    monkeypatch.setattr(tmux_session, "reap_legacy_tmux_sessions", AsyncMock())
    monkeypatch.setattr(api, "SHARED_MCP_ENABLED", False)
    monkeypatch.setenv("PINKY_LOG_ROTATION", "off")
    h.app.state.scheduler._owner_notify_callback = AsyncMock(return_value=True)
    async with h.app.router.lifespan_context(h.app):
        assert not paths["primary"].exists()
        assert paths["sample"].exists() is held
        assert set(controls) == ({"fixture-primary"} if held else {"fixture-primary", "fixture-sample"})
        if held:
            assert paths["sample"].read_bytes() == before
            assert h.subject._client.disconnect.await_count == 0


@pytest.mark.parametrize("mode", ["off", "both"])
@pytest.mark.parametrize("held", [False, True])
@pytest.mark.parametrize("entry", ["pre-authorized", "pending-replay", "approval"])
async def test_broker_replay_and_pre_authorized_delivery_preserve_a_held_backlog(
    held_subject, monkeypatch, mode, held, entry,
):
    h = held_subject
    active(h, held)
    set_flags(monkeypatch, mode)
    registry = h.app.state.agents
    if entry == "pre-authorized":
        message = BrokerMessage(agent_name="sample", platform="telegram", chat_id="fixture-chat",
                                sender_id="fixture-chat", sender_name="Fixture", content="fixture turn")
        result = await h.app.state.broker.dispatch_pre_authorized("sample", message)
        assert bool(result) is (not held)
    else:
        registry.add_pending_user("sample", "fixture-chat", "Fixture")
        registry.queue_pending_message("sample", "telegram", "fixture-chat", "Fixture", "fixture turn")
        if entry == "pending-replay":
            registry.approve_user("sample", "fixture-chat", "Fixture", "fixture")
            delivered = await h.app.state.broker.reconcile_approved_pending_messages()
        else:
            response = await h.client.post("/agents/sample/approved-users", json={
                "chat_id": "fixture-chat", "display_name": "Fixture",
            })
            assert response.status_code == 200, response.text
            assert registry.get_user_status("sample", "fixture-chat") == "approved"
            if held:
                reported(response.json(), h)
            delivered = response.json()["pending_delivered"]
        assert delivered == (0 if held else 1)
        assert len(registry.get_pending_messages("sample", "fixture-chat")) == (1 if held else 0)
    assert h.subject.send.await_count == (0 if held else 1)


async def test_startup_diagnostic_keeps_a_newline_path_on_one_line(held_subject, monkeypatch, capsys):
    from pinky_daemon import api, tmux_session

    h = held_subject
    renamed = h.escape.with_name("escape\nERROR STORAGE HOLD injected")
    h.escape.rename(renamed)
    finding = h.app.state.store_catalog.inspect_layout().findings[0]
    h.app.state.storage_holds = {"sample": [finding]}
    h.app.state.agents.set_main_agent("")
    monkeypatch.setattr(api, "SHARED_MCP_ENABLED", False)
    monkeypatch.setenv("PINKY_LOG_ROTATION", "off")
    monkeypatch.setattr(tmux_session, "reap_legacy_tmux_sessions", AsyncMock())
    monkeypatch.setattr(tmux_session, "reconcile_tmux_spawn_cleanup_debts", AsyncMock(return_value=(0, 0)))
    h.app.state.scheduler._owner_notify_callback = AsyncMock(return_value=True)
    async with h.app.router.lifespan_context(h.app):
        lines = [line for line in capsys.readouterr().err.splitlines() if line.startswith("ERROR STORAGE HOLD ")]
        assert len(lines) == 1
        assert str(renamed).encode("unicode_escape").decode("ascii") in lines[0]


@pytest.mark.parametrize("held", [False, True])
@pytest.mark.parametrize("name", ["handle_inbound", "inject_agent_message", "_route_streaming"])
async def test_guarded_broker_uses_late_class_replacement(held_subject, monkeypatch, held, name):
    h = held_subject
    active(h, held)
    broker = h.app.state.broker
    calls = []

    async def replacement(owner, *args):
        from pinky_daemon.broker import InjectResult

        calls.append((owner, args))
        return InjectResult(True, True) if name == "inject_agent_message" else True

    monkeypatch.setattr(type(broker), name, replacement)
    message = BrokerMessage(agent_name="sample", platform="telegram", chat_id="fixture-chat",
                            sender_id="fixture-chat", sender_name="Fixture", content="fixture turn")
    h.app.state.agents.approve_user("sample", "fixture-chat", "Fixture", "fixture")
    args = {
        "handle_inbound": (message,),
        "inject_agent_message": ("primary", "sample", "fixture turn"),
        "_route_streaming": ("sample", message),
    }[name]
    result = await getattr(broker, name)(*args)
    assert calls == ([] if held else [(broker, args)])
    if name == "inject_agent_message":
        assert result.delivered is (not held)
    elif name == "_route_streaming":
        assert result is (not held)


async def test_relocated_in_root_directory_keeps_recheck_incomplete_and_hold_intact(held_subject):
    import copy
    import json
    from dataclasses import asdict

    from tests.test_storage_hold_boundaries import registered_kwargs

    h = held_subject
    catalog = h.app.state.store_catalog
    root = Path(catalog.expected_root)
    directory = h.escape.parent
    moved = root / "relocated-data"
    directory.rename(moved)
    directory.symlink_to(moved, target_is_directory=True)
    names = {agent.name for agent in h.app.state.agents.list()}

    def full_report():
        report = catalog.inspect_layout(**registered_kwargs(catalog.inspect_layout, names))
        assert any(row["kind"] == "directory_outside_root" for row in report.findings)
        return json.dumps(asdict(report), sort_keys=True, separators=(",", ":"))

    before_full = full_report()
    before_hold = copy.deepcopy(h.app.state.storage_holds)
    response = await h.client.post("/agents/sample/storage/recheck")
    assert response.status_code == 200, response.text
    body = response.json()
    cleared, reason = body.get("cleared"), body.get("reason")
    assert cleared is False
    assert reason == "incomplete"
    assert body["storage_hold"]["held"] is True
    assert any(warning.startswith("could not inspect") for warning in body["warnings"])
    assert h.app.state.storage_holds == before_hold
    assert full_report() == before_full
