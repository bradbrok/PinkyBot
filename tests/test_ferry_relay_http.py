"""An untrusted relay cannot redirect, decompress, stream or wait without bounds."""

import asyncio
import contextlib
import socket
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp import web

from pinky_daemon.ferry.config import FerryConfig
from pinky_daemon.ferry.relay_http import (
    ACK_DEADLINE,
    CONNECT_DEADLINE,
    RESPONSE_CAP,
    SEND_DEADLINE,
    SHUTDOWN_DEADLINE,
    RelayClientError,
    RelayHTTPClient,
)
from pinky_daemon.ferry.relay_inbound import RelayReceiver
from tests.test_ferry_relay_config import TOKEN, relay_environment
from tests.test_ferry_relay_replay import Clock, store_at


@asynccontextmanager
async def malicious_relay(handler):
    app = web.Application()
    app.router.add_route("*", "/ferry-relay/{rest:.*}", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen()
    port = sock.getsockname()[1]
    site = web.SockSite(runner, sock)
    await site.start()
    try:
        yield f"http://127.0.0.1:{port}/ferry-relay/"
    finally:
        await runner.cleanup()
        sock.close()


@pytest.mark.parametrize("attack", ["oversized", "compressed", "deep", "redirect", "page"])
async def test_malicious_response_discards_whole_page_and_never_acks(tmp_path, monkeypatch, attack, caplog):
    requests = []

    async def handler(request):
        requests.append(request.path)
        assert request.headers["Accept-Encoding"] == "identity"
        if request.path.endswith("/ack"):
            pytest.fail("invalid response must never produce an acknowledgement")
        if attack == "redirect":
            return web.Response(status=302, headers={"Location": "/ferry-relay/second"})
        if attack == "compressed":
            return web.Response(body=b"not-even-a-compressed-body", headers={"Content-Encoding": "gzip"})
        if attack == "deep":
            return web.Response(body=b'{"rows":' + b'[' * 21 + b']' * 21 + b'}')
        if attack == "page":
            return web.json_response({"rows": [{"id": f"row-{index}"} for index in range(21)]})
        response = web.StreamResponse()
        await response.prepare(request)
        with contextlib.suppress(ConnectionResetError):
            for _ in range(34):
                await response.write(b"x" * (64 * 1024))
            await response.write_eof()
        return response

    async with malicious_relay(handler) as url:
        env, own, peer = relay_environment(tmp_path, url=url)
        settings = FerryConfig.from_env(env).relay
        replay = store_at(tmp_path / "replay.db", Clock())
        host = SimpleNamespace(deliver=AsyncMock())
        receiver = RelayReceiver(settings, replay, host)
        try:
            async with RelayHTTPClient(settings) as client:
                with pytest.raises(RelayClientError):
                    await receiver.run_once(client)
            assert requests == ["/ferry-relay/v1/poll"]
            host.deliver.assert_not_awaited()
            assert replay.snapshot()["states"] == {}
            assert TOKEN not in caplog.text
        finally:
            await receiver.close()


@pytest.mark.parametrize("operation", ["send", "ack", "poll-stream"])
async def test_absolute_deadline_covers_hanging_send_ack_and_endless_stream(tmp_path, monkeypatch, operation):
    import pinky_daemon.ferry.relay_http as module

    assert (CONNECT_DEADLINE, SEND_DEADLINE, ACK_DEADLINE, SHUTDOWN_DEADLINE, RESPONSE_CAP) == (10, 30, 15, 5, 2 * 1024 * 1024)
    finished = asyncio.Event()

    async def handler(request):
        if operation == "poll-stream":
            response = web.StreamResponse()
            await response.prepare(request)
            try:
                while not finished.is_set():
                    await response.write(b" ")
                    await asyncio.sleep(0.005)
            except ConnectionResetError:
                pass
            return response
        await finished.wait()
        return web.json_response({"id": "row", "deleted": 1})

    async with malicious_relay(handler) as url:
        env, own, peer = relay_environment(tmp_path, url=url)
        settings = FerryConfig.from_env(env).relay
        monkeypatch.setattr(module, "SEND_DEADLINE", 0.05)
        monkeypatch.setattr(module, "ACK_DEADLINE", 0.05)
        try:
            async with RelayHTTPClient(settings) as client:
                if operation == "send":
                    request = client.send("peer-one", b"sealed-fixture")
                elif operation == "ack":
                    request = client.ack(["row-one"])
                else:
                    request = client._request("GET", "v1/poll", deadline=0.05)
                async with asyncio.timeout(1):
                    with pytest.raises(RelayClientError, match="TimeoutError"):
                        await request
        finally:
            finished.set()


async def test_ack_uses_only_current_returned_ids_including_poison(tmp_path):
    env, own, peer = relay_environment(tmp_path)
    replay = store_at(tmp_path / "replay.db", Clock())
    receiver = RelayReceiver(FerryConfig.from_env(env).relay, replay,
                             SimpleNamespace(deliver=AsyncMock()))
    client = SimpleNamespace(poll=AsyncMock(return_value=[{"id": "current"}, {"id": "bad\u00e9"}, {"id": "x" * 65}]), ack=AsyncMock())
    try:
        await receiver.run_once(client)
        assert client.ack.await_args_list[0].args == (["current"],)
        assert client.ack.await_count == 1
        assert replay.snapshot()["states"] == {}
        receiver.host.deliver.assert_not_awaited()
    finally:
        await receiver.close()
