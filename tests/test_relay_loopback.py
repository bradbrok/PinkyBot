"""A real loopback server preserves the deployed reverse-proxy prefix contract."""

import asyncio
import contextlib
import hashlib
import socket
import sys
from contextlib import asynccontextmanager

import aiohttp
import pytest
import uvicorn

from pinky_relay.app import create_relay_app
from pinky_relay.config import RelayConfig
from pinky_relay.store import MIB, QueueStore
from tests.test_relay_service import TOKENS

pytestmark = pytest.mark.skipif(sys.version_info < (3, 12), reason="relay requires Python >= 3.12")


class PrefixProxy:
    """The test proxy strips exactly the deployed prefix before forwarding."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            scope = dict(scope)
            prefix = "/ferry-relay"
            assert scope["path"].startswith(prefix + "/")
            scope["path"] = scope["path"][len(prefix):]
            scope["raw_path"] = scope["path"].encode()
            scope["root_path"] = prefix
        await self.app(scope, receive, send)


@asynccontextmanager
async def serve_relay(tmp_path, tokens=None):
    tokens = tokens or TOKENS
    config = RelayConfig.from_mapping({k: hashlib.sha256(v.encode()).hexdigest() for k, v in tokens.items()})
    store = QueueStore(tmp_path / "queue.db", config.digests, disk_cap=96 * MIB)
    app = create_relay_app(config, store)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen()
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(PrefixProxy(app), host="127.0.0.1", port=port,
                                          log_level="warning", access_log=False, http="h11",
                                          h11_max_incomplete_event_size=16 * 1024))
    server.capture_signals = contextlib.nullcontext
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        async with asyncio.timeout(10):
            while not server.started:
                if task.done():
                    await task
                    raise AssertionError("relay server exited before readiness")
                await asyncio.sleep(0.01)
        yield f"http://127.0.0.1:{port}/ferry-relay/", store
    finally:
        server.should_exit = True
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), timeout=10)
        sock.close()
        store.close()


async def test_real_proxy_prefixed_health_auth_send_poll_ack(tmp_path):
    async with serve_relay(tmp_path) as (url, store):
        async with aiohttp.ClientSession(trust_env=False, auto_decompress=False) as client:
            async with client.get(url + "health") as response:
                assert response.status == 200 and await response.json() == {"ok": True}
            async with client.get(url + "v1/poll") as response:
                assert response.status == 401
            headers = {"Authorization": "Bearer " + TOKENS["peer-one"]}
            async with client.post(url + "v1/send", headers=headers,
                                   json={"to_fleet": "peer-two", "blob_b64": "c2VhbGVk"}) as response:
                assert response.status == 200
                ident = (await response.json())["id"]
            receiver = {"Authorization": "Bearer " + TOKENS["peer-two"]}
            async with client.get(url + "v1/poll", headers=receiver) as response:
                assert response.status == 200
                rows = (await response.json())["rows"]
                assert [row["id"] for row in rows] == [ident]
                assert rows[0]["sender_fleet"] == "peer-one"
            async with client.post(url + "v1/ack", headers=receiver, json={"ids": [ident]}) as response:
                assert response.status == 200 and await response.json() == {"deleted": 1}
            assert store._db.execute("SELECT count(*) FROM messages").fetchone()[0] == 0


@pytest.mark.parametrize("host", ["", "0.0.0.0", "::", "192.0.2.1", "localhost"])
def test_cli_refuses_every_nonliteral_loopback_bind_before_reading_config(tmp_path, host):
    from pinky_relay.__main__ import main

    with pytest.raises(SystemExit) as result:
        main(["--config", str(tmp_path / "missing"), "--db", str(tmp_path / "queue.db"), "--host", host])
    assert result.value.code == 2


def test_cli_pins_single_worker_h11_header_cap_and_disables_raw_access_log(tmp_path, monkeypatch):
    import json

    from pinky_relay.__main__ import main

    config = tmp_path / "config.json"
    config.write_text(json.dumps({"peer-one": hashlib.sha256(b"synthetic-token").hexdigest()}))
    config.chmod(0o600)
    calls = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: calls.append(kwargs))
    main(["--config", str(config), "--db", str(tmp_path / "queue.db")])
    assert calls == [{"host": "127.0.0.1", "port": 8898, "http": "h11",
                      "h11_max_incomplete_event_size": 16384, "workers": 1,
                      "access_log": False}]
