"""Hold policy across registration, actual wake delivery and application lifetime."""

from __future__ import annotations

import asyncio
import inspect
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import HTTPException

from pinky_daemon import api, provisioning
from pinky_daemon.auth import SESSION_COOKIE_NAME, create_session_cookie
from pinky_daemon.provisioning import ContainerNames, ContainerProvisioner, LocalProvisioner
from pinky_daemon.tmux_session import TmuxSession
from tests.recovery_test_support import closure_value
from tests.recovery_test_support import lifecycle_harness as original_lifecycle_harness
from tests.test_provisioning import RecordingContainerOps

pytestmark = pytest.mark.real_auth
NOTICE = "WARNING STORAGE HOLD: main agent exemption"
IMAGE = "fixture/agent:current"


@pytest.fixture
async def application_factory(tmp_path, monkeypatch):
    root = tmp_path / "data"
    root.mkdir(mode=0o700)
    generator = inspect.unwrap(original_lifecycle_harness)(root, monkeypatch)
    h = await anext(generator)
    apps, clients, publications = [], [], []
    closed_apps, closed_sessions = set(), set()
    monkeypatch.setattr(api, "SHARED_MCP_ENABLED", False)
    monkeypatch.setenv("PINKY_LOG_ROTATION", "off")
    monkeypatch.setattr(api, "_write_mcp_json", lambda *a, **k: publications.append((a, k)))

    async def query_succeeds(ss):
        if getattr(ss, "_client", None) is not None:
            ss._client.query.side_effect = None

    h.control.start_hook = query_succeeds

    async def close(app):
        if id(app) in closed_apps:
            return
        sessions = [ss for ss in h.sessions if id(ss) not in closed_sessions]
        await asyncio.gather(*(ss.disconnect() for ss in sessions), return_exceptions=True)
        closed_sessions.update(id(ss) for ss in sessions)
        app.state.agents._fire_trace.close()
        app.state.store_catalog.shutdown(deadline_seconds=10)
        closed_apps.add(id(app))

    def fresh():
        app = api.create_api(db_path=str(root / "api.db"), default_working_dir=str(root))
        apps.append(app)
        return app

    def client_for(app):
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test",
            cookies={SESSION_COOKIE_NAME: create_session_cookie(os.environ["PINKY_SESSION_SECRET"])},
        )
        clients.append(client)
        return client

    async def build(*, dirty, registered, main="primary"):
        primary = root / "primary"
        primary.mkdir()
        h.app.state.agents.register("primary", working_dir=str(primary), model="sonnet")
        workspace = root / "agents/sample/workspace"
        if registered:
            workspace.mkdir(parents=True)
            h.app.state.agents.register("sample", working_dir=str(workspace), model="sonnet")
        h.app.state.agents.set_main_agent(main)
        if dirty:
            directory = root / "agents/sample/data"
            directory.mkdir(parents=True)
            outside = tmp_path / "outside"
            outside.mkdir()
            (directory / "escape").symlink_to(outside, target_is_directory=True)
        await close(h.app)
        app = fresh()
        hold = closure_value(app, "_storage_hold_for")("sample")
        assert bool(hold) is dirty
        if dirty:
            assert hold["registered"] is registered
            assert hold["held"] is (registered and main != "sample")
        return SimpleNamespace(
            app=app, client=client_for(app), root=root, workspace=workspace,
            hold=hold, h=h, close=close, fresh=fresh, client_for=client_for,
            publications=publications,
        )

    try:
        yield build
    finally:
        for client in clients:
            await client.aclose()
        for app in reversed(apps):
            await close(app)
        h.sessions.clear()
        await generator.aclose()


class RecordingRuntime(RecordingContainerOps):
    def __init__(self):
        super().__init__(images=[IMAGE])
        self.probes = []

    def container_image(self, name):
        self.probes.append(("container_image", name))
        return "fixture/agent:previous"


def runtime_boundary(monkeypatch):
    ops = RecordingRuntime()
    provisioner = ContainerProvisioner(
        ops=ops, image_provider=lambda agent: agent.container_image,
        signing_key_provider=lambda name: "fixture-signing-key",
    )
    factories = []

    def get(mode, **kwargs):
        factories.append(mode)
        return provisioner if mode == "container" else LocalProvisioner()

    monkeypatch.setattr(provisioning, "get_provisioner", get)
    monkeypatch.setattr(provisioning, "container_runtime_enabled", lambda: True)
    monkeypatch.setattr(provisioning, "container_runtime_supported", lambda: True)
    monkeypatch.setattr(TmuxSession, "_check_container_image_contract", AsyncMock())

    async def container_spawn(ss):
        await ss._ensure_container_started()
        ss._skip_wake_prompt_for_tests = True

    monkeypatch.setattr(TmuxSession, "_spawn_tmux_repl", container_spawn)
    return ops, factories


@pytest.mark.parametrize("container", [False, True])
@pytest.mark.parametrize("dirty", [False, True])
async def test_registration_projects_a_discovered_hold_before_any_save(
    application_factory, monkeypatch, dirty, container,
):
    f = await application_factory(dirty=dirty, registered=False)
    ops, factories = runtime_boundary(monkeypatch)
    registry = f.app.state.agents
    register = registry.register
    writes = []

    def record_register(*args, **kwargs):
        writes.append((args, kwargs))
        return register(*args, **kwargs)

    monkeypatch.setattr(registry, "register", record_register)
    f.publications.clear()
    response = await f.client.post("/agents", json={
        "name": "sample", "working_dir": str(f.workspace), "model": "sonnet",
        "runtime": "claude_sdk", "transport": "tmux" if container else "sdk",
        "isolation_mode": "container" if container else "local",
        "container_image": IMAGE if container else "",
    })
    if dirty and container:
        assert response.status_code == 409, response.text
        detail = response.json()["detail"]
        assert detail["code"] == "storage_hold"
        assert detail["findings"] == f.hold["findings"]
        assert writes == [] and factories == [] and ops.commands == []
        assert ops.secrets_written == [] and f.publications == []
        assert registry.get("sample") is None
        current = closure_value(f.app, "_storage_hold_for")("sample")
        assert current["registered"] is False and current["held"] is False
    else:
        assert response.status_code == 200, response.text
        assert len(writes) == 1 and registry.get("sample") is not None
        if container:
            assert any(command[1] == "create" for command in ops.commands)
            assert len(ops.secrets_written) == 1
        else:
            assert ops.commands == [] and ops.secrets_written == []
        if dirty:
            hold = response.json().get("storage_hold")
            assert isinstance(hold, dict), response.text
            assert hold["held"] and hold["registered"]
            assert hold["findings"] == f.hold["findings"]


@pytest.mark.parametrize("main", ["configured", "automatic"])
async def test_registration_projection_preserves_the_main_exemption(
    application_factory, monkeypatch, main,
):
    f = await application_factory(dirty=True, registered=False)
    f.app.state.agents.set_main_agent("sample" if main == "configured" else "")
    ops, _ = runtime_boundary(monkeypatch)
    response = await f.client.post("/agents", json={
        "name": "sample", "working_dir": str(f.workspace), "model": "sonnet",
        "runtime": "claude_sdk", "transport": "tmux", "isolation_mode": "container",
        "container_image": IMAGE,
    })
    assert response.status_code == 200, response.text
    assert f.app.state.agents.get_main_agent() == "sample"
    assert any(command[1] == "create" for command in ops.commands)
    hold = closure_value(f.app, "_storage_hold_for")("sample")
    assert hold["registered"] and hold["main_exempt"] and not hold["held"]


@pytest.mark.parametrize("path", ["containerize", "cold", "image-recreate"])
@pytest.mark.parametrize("dirty", [False, True])
async def test_each_container_effect_path_has_a_real_guarded_twin(
    application_factory, monkeypatch, dirty, path,
):
    f = await application_factory(dirty=dirty, registered=True)
    ops, factories = runtime_boundary(monkeypatch)
    if path != "containerize":
        f.app.state.agents.update(
            "sample", isolation_mode="container", container_image=IMAGE,
            isolated=True, transport="tmux",
        )
    if path == "image-recreate":
        names = ContainerNames.for_agent("sample")
        ops._containers.add(names.container)
        ops._volumes.add(names.volume)
        ops._secrets.add(names.secret)
    if path == "containerize":
        response = await f.client.post("/agents/sample/containerize", json={
            "image": IMAGE, "start": False,
        })
        assert response.status_code == (409 if dirty else 202), response.text
        if not dirty:
            async def terminal():
                registry = closure_value(f.app, "_container_op_registry")
                while registry.in_flight("sample"):
                    await asyncio.sleep(0)
                return registry.get("sample")

            record = await asyncio.wait_for(terminal(), timeout=10)
            assert record.status == "ready", record
    elif dirty:
        with pytest.raises(HTTPException) as exc:
            await closure_value(f.app, "_container_lifecycle").deps.start_session("sample")
        assert exc.value.status_code == 409
        assert exc.value.detail["code"] == "storage_hold"
    else:
        assert await closure_value(f.app, "_container_lifecycle").deps.start_session("sample") is not None
    if dirty:
        assert factories == [] and ops.commands == [] and ops.probes == []
        assert ops.secrets_written == []
    else:
        assert any(command[1] == "create" for command in ops.commands)
        assert any(command[1] == "start" for command in ops.commands)
        if path == "image-recreate":
            assert ops.probes and any(command[1:3] == ["rm", "-f"] for command in ops.commands)


@pytest.mark.parametrize("failure", ["none", "preparation", "submission"])
@pytest.mark.parametrize("dirty", [False, True])
async def test_main_notice_is_consumed_only_after_actual_wake_delivery(
    application_factory, dirty, failure,
):
    f = await application_factory(dirty=dirty, registered=True, main="sample")
    build = f.app.state._build_streaming_wake_context
    for _ in range(2):
        assert (NOTICE in build("sample", commit=False)) is dirty
    successful, attempted = [], []
    fail_submission = False

    async def connect_peer(ss):
        async def query(prompt):
            attempted.append(str(prompt))
            if fail_submission:
                raise RuntimeError("fixture submission failed")
            successful.append(str(prompt))

        ss._client.query.side_effect = query

    f.h.control.start_hook = connect_peer
    prepare = closure_value(f.app, "_prepare_streaming_session")
    connect = closure_value(f.app, "_connect_prepared_session")

    async def wake(label, resume=""):
        prepared = await prepare("sample", label=label, resume_id=resume)
        assert prepared is not None
        await connect(prepared)
        await asyncio.wait_for(asyncio.shield(prepared._initial_wake_task), timeout=10)

    if failure == "preparation":
        f.h.control.start_error = RuntimeError("fixture preparation failed")
        with pytest.raises(RuntimeError, match="fixture preparation failed"):
            await wake("failed")
        f.h.control.start_error = None
    elif failure == "submission":
        fail_submission = True
        await wake("failed")
        fail_submission = False
        assert len(attempted) == 1 and successful == []
    for _ in range(2):
        assert (NOTICE in build("sample", commit=False)) is dirty
    await wake("first")
    await wake("second", resume="fixture-resume")
    assert [NOTICE in text for text in successful] == ([True, False] if dirty else [False, False])
    if dirty:
        assert all(finding["path"] in successful[0] for finding in f.hold["findings"])
    activity = inspect.getclosurevars(closure_value(f.app, "_log_agent_wake_event")).nonlocals["activity"]
    rows = activity.list(agent_name="sample", event_type="agent_wake", limit=10)
    assert len(rows) == 2
    assert {row["metadata"]["reason"] for row in rows} == {"new_session", "resume"}
    assert NOTICE not in build("sample", commit=False)


async def test_real_owner_action_keeps_original_override_object_and_next_app_is_held(
    application_factory,
):
    f = await application_factory(dirty=True, registered=True)
    first = f.app
    allocated = first.state.storage_hold_overrides
    assert allocated == set()
    response = await f.client.post("/admin/agents/sample/storage/start-anyway")
    assert response.status_code == 200, response.text
    assert first.state.storage_hold_overrides is allocated
    assert allocated == {"sample"}
    assert response.json()["storage_hold"]["overridden"] is True
    await f.close(first)
    second = f.fresh()
    assert second.state.storage_hold_overrides is not allocated
    assert second.state.storage_hold_overrides == set()
    hold = closure_value(second, "_storage_hold_for")("sample")
    assert hold["registered"] and hold["held"] and not hold["overridden"]
    assert hold["findings"] == f.hold["findings"]


async def test_an_older_preview_cannot_replay_a_delivered_notice_on_builder_fallback(
    application_factory, monkeypatch,
):
    f = await application_factory(dirty=True, registered=True, main="sample")
    f.app.state.agents.set_context(
        "sample", task="fixture retained wake context",
        metadata={"source": "save_my_context"}, updated_by="saved",
    )
    submitted = []

    async def connect_peer(ss):
        async def query(prompt):
            submitted.append(str(prompt))

        ss._client.query.side_effect = query

    f.h.control.start_hook = connect_peer
    prepare = closure_value(f.app, "_prepare_streaming_session")
    connect = closure_value(f.app, "_connect_prepared_session")
    first = await prepare("sample", label="first")
    second = await prepare("sample", label="second")
    assert NOTICE in first._config.wake_context and NOTICE in second._config.wake_context
    await connect(first)
    await asyncio.wait_for(asyncio.shield(first._initial_wake_task), timeout=10)

    def unreadable_context(name):
        raise RuntimeError("fixture wake-context read failed")

    monkeypatch.setattr(f.app.state.agents, "get_context", unreadable_context)
    await connect(second)
    await asyncio.wait_for(asyncio.shield(second._initial_wake_task), timeout=10)
    assert [NOTICE in text for text in submitted] == [True, False]
    assert "fixture retained wake context" in submitted[1]
