"""Scratch-only fixtures for authorization boundary integration tests."""

from __future__ import annotations

import asyncio
import inspect
import io
import socket
import threading
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from pinky_daemon import api
from pinky_daemon.auth import build_internal_auth_headers
from pinky_daemon.shared_mcp import SharedMcpManager, derive_mcp_bearer


def closure(app, name):
    queue = [r.endpoint for r in app.routes if hasattr(r, "endpoint")]
    seen = set()
    while queue:
        fn = queue.pop()
        if not inspect.isfunction(fn) or id(fn) in seen:
            continue
        seen.add(id(fn))
        if fn.__name__ == name:
            return fn
        queue.extend(inspect.getclosurevars(fn).nonlocals.values())
    raise AssertionError(f"Existing application function not found: {name}")


def replace_cell(monkeypatch, fn, name, value):
    cells = dict(zip(fn.__code__.co_freevars, fn.__closure__ or ()))
    assert name in cells, (fn.__name__, name)
    monkeypatch.setattr(cells[name], "cell_contents", value)


@pytest.fixture
def daemon(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(api, "SHARED_MCP_ENABLED", True)
    monkeypatch.setenv("PINKY_ISOLATED_POLICY_MODE", "enforce")
    monkeypatch.setenv("PINKY_TOOL_POLICY", "off")
    monkeypatch.setenv("OPENAI_API_KEY", "")
    apps = []

    def build(mode="enforce"):
        if mode is None:
            monkeypatch.delenv("PINKY_ISOLATED_POLICY_MODE", raising=False)
        else:
            monkeypatch.setenv("PINKY_ISOLATED_POLICY_MODE", mode)
        root = tmp_path / f"app-{len(apps)}"
        root.mkdir()
        app = api.create_api(db_path=str(root / "memory.db"), default_working_dir=str(root))
        apps.append(app)
        agents = app.state.agents
        for name, isolated in (
            ("tenant", True),
            ("normal", False),
            ("peer", True),
            ("dreamer", False),
        ):
            work = root / name
            work.mkdir()
            agents.register(name, isolated=isolated, working_dir=str(work))
        skills = inspect.getclosurevars(closure(app, "_prepare_streaming_session")).nonlocals[
            "skills"
        ]
        if skills.get("pinky-self") is None:
            skills.register("pinky-self", description="Fixture tool entitlement")
        for name in ("tenant", "normal", "peer"):
            skills.assign_to_agent(name, "pinky-self")
        return SimpleNamespace(app=app, agents=agents, skills=skills, root=root)

    yield build
    for app in reversed(apps):
        app.state.store_catalog.close()


def signed(d, method, path, name="tenant"):
    return build_internal_auth_headers(
        d.agents.get_signing_key(name), agent_name=name, method=method, path=path
    )


def auth_headers(d, name="tenant", verified=True):
    headers = {"X-Agent-Name": name}
    if verified:
        headers["Authorization"] = "Bearer " + derive_mcp_bearer(d.agents.get_signing_key(name))
    return headers


@asynccontextmanager
async def shared_service(d, monkeypatch):
    """Real server/SDK transport; only outbound daemon HTTP is replaced."""
    import urllib.request

    import uvicorn

    outgoing = []
    opened_stores = []
    signing = []
    embeddings = []
    from pinky_memory.embeddings import NoOpEmbeddingClient

    original_embed = NoOpEmbeddingClient.embed

    def track_embed(self, text):
        embeddings.append(True)
        return original_embed(self, text)

    monkeypatch.setattr(NoOpEmbeddingClient, "embed", track_embed)

    def memory_path(name):
        opened_stores.append(name)
        return str(d.root / name / "test-memory.db")

    def key(name):
        return d.agents.get_signing_key(name)

    from pinky_messaging import server as messaging_server

    original_sign = messaging_server.build_internal_auth_headers

    def tracked_sign(*args, **kwargs):
        signing.append(kwargs.get("agent_name"))
        return original_sign(*args, **kwargs)

    monkeypatch.setattr(messaging_server, "build_internal_auth_headers", tracked_sign)

    def fake_urlopen(request, *args, **kwargs):
        del args, kwargs
        outgoing.append((request.method, request.selector, request.data))
        return io.BytesIO(b'{"ok":true,"success":true,"restarting":true}')

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    arguments = dict(
        api_url="http://127.0.0.1:1",
        signing_key_resolver=key,
        memory_db_resolver=memory_path,
        cross_agent_authorizer=lambda name: name == "dreamer",
    )
    # The base has no policy dependency input. Keep exercising its real
    # authenticated service instead of failing at construction with TypeError.
    # The proposed implementation adds this explicit registry dependency.
    if "agent_registry" in inspect.signature(SharedMcpManager).parameters:
        arguments["agent_registry"] = d.agents
    if "skill_store" in inspect.signature(SharedMcpManager).parameters:
        arguments["skill_store"] = d.skills
    manager = SharedMcpManager(**arguments)
    app = manager._create_app()
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(64)
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", log_config=None))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        await asyncio.sleep(0.01)
    assert server.started, "Scratch shared server did not start"
    try:
        yield SimpleNamespace(
            base=f"http://127.0.0.1:{port}",
            outgoing=outgoing,
            opened_stores=opened_stores,
            signing=signing,
            embeddings=embeddings,
        )
    finally:
        server.should_exit = True
        await asyncio.to_thread(thread.join, 5)
        sock.close()
        assert not thread.is_alive(), "Scratch shared server did not terminate"
        if manager._memory_pool:
            manager._memory_pool.close_all()


@asynccontextmanager
async def protocol(service, transport, headers, mount="self"):
    from datetime import timedelta

    from mcp import ClientSession
    from mcp.client.sse import sse_client
    from mcp.client.streamable_http import streamablehttp_client
    from mcp.shared._httpx_utils import create_mcp_http_client

    clients = []
    endpoints = []

    def factory(*args, **kwargs):
        client = create_mcp_http_client(*args, **kwargs)
        clients.append(client)
        return client

    if transport == "sse":
        client = sse_client(
            service.base + f"/mcp/{mount}/sse",
            headers=headers,
            timeout=5,
            httpx_client_factory=factory,
            on_session_created=endpoints.append,
        )
    else:
        client = streamablehttp_client(
            service.base + f"/mcp/{mount}/http/mcp",
            headers=headers,
            timeout=timedelta(seconds=5),
            httpx_client_factory=factory,
        )
    async with client as streams:
        async with ClientSession(streams[0], streams[1]) as session:
            await session.initialize()
            session.test_http_clients = clients
            session.test_endpoint = (
                service.base + f"/mcp/{mount}/messages/?session_id=" + endpoints[0]
                if endpoints
                else service.base + f"/mcp/{mount}/http/mcp"
            )
            session.test_session_id = streams[2] if transport == "http" else lambda: None
            yield session


async def names(session):
    return {t.name for t in (await session.list_tools()).tools}


async def assert_refused(session, tool, arguments, service):
    before = len(service.outgoing)
    result = await session.call_tool(tool, arguments)
    observed = (result.isError, len(service.outgoing) - before)
    assert observed == (True, 0), f"Tool executed instead of refusing: {tool}: {observed}"
