"""Bounded, shared TCP probes for remote MCP endpoints at daemon boot.

TCP reachability ≠ MCP initialize success; this gate only fixes ordering.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import math
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from urllib.parse import urlsplit

Endpoint = tuple[str, int]
DEFAULT_RETRY_DELAYS = (5, 15, 30, 60)
CONNECT_TIMEOUT_SEC = 3.0
_logger = logging.getLogger(__name__)


def remote_mcp_endpoints(
    mcp_config: dict, *, excluded_endpoints: Iterable[Endpoint] = (),
) -> set[Endpoint]:
    """Extract remote URLs, excluding the daemon's own listener identities."""
    if not isinstance(mcp_config, dict):
        _logger.warning("startup: skipped malformed MCP configuration")
        return set()
    excluded = {(host.lower().rstrip("."), port) for host, port in excluded_endpoints}
    servers = mcp_config.get("mcpServers", mcp_config.get("mcp_servers", {}))
    if not isinstance(servers, dict):
        return set()
    endpoints = set()
    for server in servers.values():
        if not isinstance(server, dict) or server.get("enabled") is False or "url" not in server:
            continue
        try:
            parsed = urlsplit(server["url"])
            host = (parsed.hostname or "").lower().rstrip(".")
            if parsed.scheme not in {"http", "https"} or not host or any(c.isspace() for c in host):
                raise ValueError("invalid HTTP URL")
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            if host == "localhost" or (host, port) in excluded:
                continue
            try:
                address = ipaddress.ip_address(host)
            except ValueError:
                address = None
            if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
                address = address.ipv4_mapped
            if address and (address.is_loopback or address.is_unspecified or address.is_link_local):
                continue
            endpoints.add((host, port))
        except (TypeError, ValueError, AttributeError):
            # URLs and parse errors may contain credentials or private query strings.
            _logger.warning("startup: skipped malformed remote MCP URL")
    return endpoints


@dataclass(frozen=True)
class EndpointReadiness:
    status: str
    waited_sec: float
    attempts: int


@dataclass(frozen=True)
class ReadinessReport:
    results: dict[Endpoint, EndpointReadiness]

    @property
    def waited_sec(self) -> float:
        return max((result.waited_sec for result in self.results.values()), default=0.0)


async def _connect(host: str, port: int):
    return await asyncio.open_connection(host, port)


async def _probe_endpoint(
    endpoint, *, delays, cap_sec, connect, wait, cancel_falls_open=False,
) -> EndpointReadiness:
    started = time.monotonic()
    deadline = started + cap_sec
    attempts = 0
    status = "unreachable"
    while (remaining := deadline - time.monotonic()) > 0:
        attempts += 1
        try:
            async with asyncio.timeout(min(CONNECT_TIMEOUT_SEC, remaining)):
                _, writer = await connect(*endpoint)
                writer.close()
                await writer.wait_closed()
            status = "up"
            break
        except asyncio.CancelledError:
            if not cancel_falls_open:
                raise
            break
        except Exception:
            # DNS failures, refused sockets and timeout all consume an attempt.
            pass
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        delay = min(delays[min(attempts - 1, len(delays) - 1)], remaining)
        try:
            async with asyncio.timeout(remaining):
                await wait(delay)
        except asyncio.CancelledError:
            if not cancel_falls_open:
                raise
            break
        except TimeoutError:
            break
    return EndpointReadiness(status, min(cap_sec, time.monotonic() - started), attempts)


async def wait_for_endpoints(
    endpoints: Iterable[Endpoint], *, delays=DEFAULT_RETRY_DELAYS, cap_sec=120,
    connect: Callable[..., Awaitable] | None = None, wait=asyncio.sleep,
) -> ReadinessReport:
    """Probe endpoints concurrently; each deadline includes DNS, connect and waits."""
    if cap_sec <= 0:
        return ReadinessReport({})
    if not math.isfinite(cap_sec) or not delays or any(delay <= 0 for delay in delays):
        raise ValueError("readiness requires a finite cap and positive retry delays")
    keys = sorted(set(endpoints))
    tasks = [asyncio.create_task(_probe_endpoint(
        key, delays=delays, cap_sec=cap_sec, connect=connect or _connect, wait=wait,
    )) for key in keys]
    try:
        return ReadinessReport(dict(zip(keys, await asyncio.gather(*tasks))))
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


class BootMcpReadiness:
    """One probe per endpoint shared by session launches in a single boot."""

    def __init__(self, *, cap_sec=120, connect=None, wait=asyncio.sleep):
        self.cap_sec = cap_sec
        self._connect = connect or _connect
        self._wait = wait
        self._tasks: dict[Endpoint, asyncio.Task] = {}

    async def wait_for(self, endpoints: Iterable[Endpoint]) -> ReadinessReport:
        if self.cap_sec <= 0:
            return ReadinessReport({})
        keys = sorted(set(endpoints))
        for key in keys:
            if key not in self._tasks:
                self._tasks[key] = asyncio.create_task(_probe_endpoint(
                    key, delays=DEFAULT_RETRY_DELAYS, cap_sec=self.cap_sec,
                    connect=self._connect, wait=self._wait, cancel_falls_open=True,
                ))
        # Teardown of the shared probe falls open; cancellation of this caller
        # still propagates from gather and cannot cancel the shielded probe.
        await asyncio.gather(
            *(asyncio.shield(self._tasks[key]) for key in keys), return_exceptions=True,
        )
        return ReadinessReport({key: self._result(self._tasks[key]) for key in keys})

    @staticmethod
    def _result(task: asyncio.Task) -> EndpointReadiness:
        # Cancellation before the coroutine starts has no attempts or elapsed wait.
        if task.cancelled():
            return EndpointReadiness("unreachable", 0.0, 0)
        return task.result()

    def report(self) -> ReadinessReport:
        return ReadinessReport({key: self._result(task) for key, task in self._tasks.items()
                                if task.done() and (task.cancelled() or not task.exception())})

    async def close(self) -> None:
        for task in self._tasks.values():
            if not task.done():
                task.cancel()
        await asyncio.gather(*self._tasks.values(), return_exceptions=True)
