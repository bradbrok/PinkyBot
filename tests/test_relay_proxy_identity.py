"""The real HTTP stack preserves the TCP peer before source-IP admission."""

import asyncio
import contextlib
import hashlib
import json
import socket
import sys
from contextlib import asynccontextmanager

import aiohttp
import pytest
import uvicorn

from pinky_relay.__main__ import main

pytestmark = pytest.mark.skipif(sys.version_info < (3, 12), reason="relay requires Python >= 3.12")


@asynccontextmanager
async def entrypoint_server(tmp_path, monkeypatch):
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps({"receiver": hashlib.sha256(b"synthetic-private-token").hexdigest()}))
    config_file.chmod(0o600)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen()
    port = sock.getsockname()[1]
    captured = {}

    def run(app, **kwargs):
        # Consume the actual production entrypoint's arguments. Hardcoding
        # proxy_headers here would conceal its default or an omitted fix.
        config = uvicorn.Config(app, **kwargs)
        server = uvicorn.Server(config)
        server.capture_signals = contextlib.nullcontext
        captured.update(config=config, server=server)
        server.run(sockets=[sock])

    monkeypatch.setattr(uvicorn, "run", run)
    task = asyncio.create_task(asyncio.to_thread(main, ["--config", str(config_file),
                                                     "--db", str(tmp_path / "queue.db"),
                                                     "--port", str(port)]))
    try:
        async with asyncio.timeout(10):
            while "server" not in captured or not captured["server"].started:
                if task.done():
                    await task
                    pytest.fail("fixture server exited before readiness")
                await asyncio.sleep(0.01)
        yield f"http://127.0.0.1:{port}", captured["config"]
    finally:
        if "server" in captured:
            captured["server"].should_exit = True
        await asyncio.wait_for(task, timeout=10)
        sock.close()


async def test_effective_entrypoint_config_disables_proxy_header_rewriting(tmp_path, monkeypatch):
    async with entrypoint_server(tmp_path, monkeypatch) as (_, config):
        assert config.proxy_headers is False


async def test_forged_forwarded_for_cannot_rotate_the_bad_auth_bucket(tmp_path, monkeypatch):
    async with entrypoint_server(tmp_path, monkeypatch) as (url, _):
        async with aiohttp.ClientSession(trust_env=False) as client:
            statuses = []
            for index in range(31):
                headers = {"X-Real-IP": "198.51.100.11",
                           "X-Forwarded-For": f"203.0.113.{index + 1}"}
                async with client.get(url + "/v1/poll", headers=headers) as response:
                    statuses.append(response.status)
            assert statuses[:30] == [401] * 30
            assert statuses[30] == 429, "a forged forwarding header selected a fresh auth bucket"


async def test_overwritten_real_ip_values_have_independent_bad_auth_buckets(tmp_path, monkeypatch):
    async with entrypoint_server(tmp_path, monkeypatch) as (url, _):
        async with aiohttp.ClientSession(trust_env=False) as client:
            for address in ("198.51.100.11", "198.51.100.12"):
                statuses = []
                for _ in range(31):
                    async with client.get(url + "/v1/poll", headers={"X-Real-IP": address}) as response:
                        statuses.append(response.status)
                assert statuses[:30] == [401] * 30
                assert statuses[30] == 429
