"""Cold-boot endpoint extraction and bounded TCP probing contracts."""

from __future__ import annotations

import asyncio
import importlib
import logging
import socket
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest


def _readiness():
    # Import inside each test so missing implementation does not mask other REDs.
    return importlib.import_module("pinky_daemon.mcp_readiness")


@pytest.mark.parametrize("root", ["mcpServers", "mcp_servers"])
def test_remote_mcp_endpoint_shapes_and_distinct_ports(root):
    config = {root: {
        "sse": {"type": "sse", "url": "https://bridge.example/sse"},
        "http": {"type": "http", "url": "http://bridge.example:8080/mcp"},
        "duplicate": {"url": "https://BRIDGE.example/another"},
        "ipv6": {"url": "http://[fd00::12]:9000/mcp"},
        "lan": {"url": "http://192.168.10.12:8000/mcp"},
        "stdio": {"command": "bridge", "args": ["https://ignored.example"]},
        "disabled": {"url": "https://disabled.example/mcp", "enabled": False},
    }}
    assert _readiness().remote_mcp_endpoints(config) == {
        ("bridge.example", 443), ("bridge.example", 8080),
        ("fd00::12", 9000), ("192.168.10.12", 8000),
    }


@pytest.mark.parametrize("host", [
    "127.0.0.1", "127.99.5.8", "[::1]", "localhost", "LOCALHOST",
    "0.0.0.0", "[::]", "169.254.10.9", "[fe80::12]",
])
def test_non_remote_address_classes_are_excluded(host):
    config = {"mcpServers": {"local": {"url": f"http://{host}:8889/mcp"}}}
    assert _readiness().remote_mcp_endpoints(config) == set()


def test_shared_listener_identity_excludes_alias_but_not_other_port():
    own = {("host.containers.internal", 8889), ("daemon.example", 8889)}
    config = {"mcpServers": {
        "container-core": {"url": "http://host.containers.internal:8889/mcp/self/sse"},
        "host-core": {"url": "http://daemon.example:8889/mcp/memory/sse"},
        "remote": {"url": "http://daemon.example:9000/mcp"},
    }}
    assert _readiness().remote_mcp_endpoints(config, excluded_endpoints=own) == {
        ("daemon.example", 9000),
    }


@pytest.mark.parametrize("url", [
    "not a url", "http://[broken", "http://bridge.example:bad/mcp",
    "http:///no-host", "https://bridge.example:65536/mcp",
    "https://user:private-token@bridge.example:bad/mcp?secret=private-query",
])
def test_bad_url_skips_with_one_sanitized_warning(url, caplog):
    with caplog.at_level(logging.WARNING):
        result = _readiness().remote_mcp_endpoints({"mcpServers": {"bad": {"url": url}}})
    assert result == set()
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "private-token" not in caplog.text and "private-query" not in caplog.text


class _ProbeClock:
    def __init__(self):
        self.now = 100.0
        self.waits = []

    async def wait(self, delay):
        self.waits.append(delay)
        self.now += delay


@pytest.mark.asyncio
async def test_probe_ladder_up_on_third_attempt(monkeypatch):
    mod = _readiness()
    clock = _ProbeClock()
    monkeypatch.setattr(mod, "time", SimpleNamespace(monotonic=lambda: clock.now))
    writer = SimpleNamespace(close=Mock(), wait_closed=AsyncMock())
    connect = AsyncMock(side_effect=[OSError("offline"), socket.gaierror(), (None, writer)])
    report = await mod.wait_for_endpoints(
        {("bridge.example", 443)}, connect=connect, wait=clock.wait,
    )
    result = report.results[("bridge.example", 443)]
    assert (result.status, result.attempts, result.waited_sec) == ("up", 3, 20)
    assert clock.waits == [5, 15]
    assert connect.await_count == 3
    writer.close.assert_called_once()
    writer.wait_closed.assert_awaited_once()


@pytest.mark.asyncio
async def test_probe_deadline_includes_failed_connect_time(monkeypatch):
    mod = _readiness()
    clock = _ProbeClock()
    monkeypatch.setattr(mod, "time", SimpleNamespace(monotonic=lambda: clock.now))

    async def connect(host, port):
        clock.now += 3
        raise OSError("unreachable")

    report = await mod.wait_for_endpoints(
        {("bridge.example", 443)}, cap_sec=20, connect=connect, wait=clock.wait,
    )
    result = report.results[("bridge.example", 443)]
    assert result.status == "unreachable"
    assert result.waited_sec == 20
    assert result.attempts == 2
    assert clock.waits == [5, 9]


@pytest.mark.asyncio
async def test_hanging_dns_or_connect_is_cancelled_at_total_cap():
    cancelled = asyncio.Event()

    async def connect(host, port):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    report = await asyncio.wait_for(_readiness().wait_for_endpoints(
        {("bridge.example", 443)}, cap_sec=0.03, connect=connect,
    ), timeout=1)
    assert report.results[("bridge.example", 443)].status == "unreachable"
    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_two_agents_share_one_endpoint_probe_task():
    mod = _readiness()
    release = asyncio.Event()
    entered = asyncio.Event()
    writer = SimpleNamespace(close=Mock(), wait_closed=AsyncMock())

    async def connect(host, port):
        entered.set()
        await release.wait()
        return None, writer

    connect_mock = AsyncMock(side_effect=connect)
    gate = mod.BootMcpReadiness(connect=connect_mock)
    endpoints = {("bridge.example", 443)}
    first = asyncio.create_task(gate.wait_for(endpoints))
    second = asyncio.create_task(gate.wait_for(endpoints))
    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        release.set()
        a, b = await asyncio.gather(first, second)
        assert a.results == b.results
        assert a.results[("bridge.example", 443)].status == "up"
        connect_mock.assert_awaited_once_with("bridge.example", 443)
    finally:
        release.set()
        await asyncio.gather(first, second, return_exceptions=True)


@pytest.mark.asyncio
async def test_gate_disabled_does_not_connect_or_sleep():
    connect, wait = AsyncMock(), AsyncMock()
    report = await _readiness().wait_for_endpoints(
        {("bridge.example", 443)}, cap_sec=0, connect=connect, wait=wait,
    )
    assert report.waited_sec == 0
    connect.assert_not_awaited()
    wait.assert_not_awaited()


@pytest.mark.asyncio
async def test_distinct_ports_on_same_host_are_independent_probes():
    writer = SimpleNamespace(close=Mock(), wait_closed=AsyncMock())
    connect = AsyncMock(return_value=(None, writer))
    endpoints = {("bridge.example", 443), ("bridge.example", 9443)}
    report = await _readiness().wait_for_endpoints(endpoints, connect=connect)
    assert set(report.results) == endpoints
    assert connect.await_count == 2
    assert {call.args for call in connect.await_args_list} == endpoints


@pytest.mark.asyncio
async def test_dns_failure_retries_and_fails_open(monkeypatch):
    mod = _readiness()
    clock = _ProbeClock()
    monkeypatch.setattr(mod, "time", SimpleNamespace(monotonic=lambda: clock.now))
    connect = AsyncMock(side_effect=socket.gaierror("name unavailable"))
    report = await mod.wait_for_endpoints(
        {("missing.example", 443)}, cap_sec=20, connect=connect, wait=clock.wait,
    )
    assert report.results[("missing.example", 443)].status == "unreachable"
    assert 2 <= connect.await_count <= 3
    assert sum(clock.waits) == 20


def test_readiness_documents_tcp_limit():
    assert "TCP reachability ≠ MCP initialize success; this gate only fixes ordering" in (
        _readiness().__doc__
    )
