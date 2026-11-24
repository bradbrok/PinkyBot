"""The main API owns a relay receiver independently of the direct listener."""

import asyncio
import json
import sys
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from pinky_daemon.ferry.config import FerryConfig
from pinky_daemon.ferry.outbound import HttpMeshSender, MeshSender, build_envelope
from pinky_daemon.ferry.relay_http import RelayHTTPClient
from pinky_daemon.ferry.relay_identity import generate_identity
from pinky_daemon.ferry.relay_inbound import RelayReceiver
from pinky_daemon.ferry.relay_outbound import RelayMeshSender
from pinky_daemon.ferry.relay_replay import ReplayStore
from pinky_daemon.ferry.types import DeliveryResult
from tests.test_ferry_relay_config import TOKEN, relay_environment


@pytest.mark.usefixtures("stub_sdk_transport")
def test_api_lifecycle_starts_relay_with_direct_listener_disabled_and_closes_owner(tmp_path, monkeypatch):
    from pinky_daemon.api import create_api

    env, own, peer = relay_environment(tmp_path)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    began, cancelled = threading.Event(), threading.Event()

    async def poll(client, wait=25):
        began.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(RelayHTTPClient, "poll", poll)
    app = create_api(db_path=str(tmp_path / "conversations.db"), default_working_dir=str(tmp_path))
    receiver = app.state.relay_receiver
    try:
        assert not FerryConfig.from_env(env).enabled
        assert receiver.settings.ready
        record = next(r for r in app.state.store_catalog.snapshot() if r.logical_name == "ferry_replay")
        assert record.recovery == "manual"
        with TestClient(app):
            assert began.wait(3)
            assert receiver.task is not None and not receiver.task.done()
        assert cancelled.is_set() and receiver.task.done()
        resumed = ReplayStore(tmp_path / "ferry_relay_replay.db")
        resumed.close()
    finally:
        app.state.store_catalog.close()


@pytest.mark.usefixtures("stub_sdk_transport")
async def test_real_broker_reply_callback_uses_reserved_relay_and_never_http_or_nats(tmp_path, monkeypatch):
    from pinky_daemon.api import create_api

    env, own, peer = relay_environment(tmp_path)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    app = create_api(db_path=str(tmp_path / "conversations.db"), default_working_dir=str(tmp_path))
    sends, forbidden = [], []

    async def relay_send(self, message, fleet):
        sends.append((message, fleet))

    monkeypatch.setattr(RelayMeshSender, "send_async", relay_send)
    monkeypatch.setattr(HttpMeshSender, "send", lambda *a: forbidden.append("http"))
    monkeypatch.setattr(MeshSender, "send", lambda *a: forbidden.append("nats"))
    try:
        app.state.agents.register("sender", model="sonnet", working_dir=str(tmp_path / "sender"))
        app.state.agents.set_mesh_outbound_allowlist("sender", ["*@peer-one"])
        result = await app.state.broker.send_callback("sender", "ferry", "receiver@peer-one", "reply fixture", reply_to="original-id")
        assert result["sent"] and forbidden == []
        assert len(sends) == 1 and sends[0][1] == "peer-one"
        assert sends[0][0].reply_to == "original-id"
        assert sends[0][0].body["text"] == "reply fixture"
    finally:
        await app.state.relay_receiver.close()
        app.state.store_catalog.close()


async def test_receiver_close_cancels_inflight_poll_within_five_seconds(tmp_path):
    env, own, peer = relay_environment(tmp_path)
    replay = ReplayStore(tmp_path / "replay.db")
    began = asyncio.Event()

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return

        async def poll(self, wait):
            began.set()
            await asyncio.Event().wait()

    receiver = RelayReceiver(FerryConfig.from_env(env).relay, replay,
                             SimpleNamespace(deliver=AsyncMock()), client_factory=lambda _: Client())
    receiver.start()
    await asyncio.wait_for(began.wait(), timeout=1)
    with pytest.raises(Exception, match="already started"):
        receiver.start()
    async with asyncio.timeout(5):
        await receiver.close()
    assert receiver.task.done() and receiver.state == "stopped"


@pytest.mark.skipif(sys.version_info < (3, 12), reason="relay requires Python >= 3.12")
async def test_real_relay_roundtrip_both_directions_and_restart_replay(tmp_path):
    from tests.test_relay_loopback import serve_relay

    token_b = "synthetic-peer-two-relay-token-0123456789"
    async with serve_relay(tmp_path / "service", {"peer-one": TOKEN, "peer-two": token_b}) as (url, queue):
        paths = [tmp_path / "one.json", tmp_path / "two.json"]
        identities = [generate_identity(path, name) for path, name in zip(paths, ("peer-one", "peer-two"), strict=True)]
        configs, receivers, hosts = [], [], []
        for index in range(2):
            own, peer = identities[index], identities[1 - index]
            env = {"PINKYBOT_FLEET_NAME": own.fleet, "PINKYBOT_FERRY_RELAY_URL": url,
                   "PINKYBOT_FERRY_RELAY_TOKEN": (TOKEN, token_b)[index],
                   "PINKYBOT_FERRY_RELAY_KEY_FILE": str(paths[index]),
                   "PINKYBOT_FERRY_RELAY_PEERS": json.dumps({peer.fleet: peer.public()})}
            config = FerryConfig.from_env(env)
            assert not config.enabled and config.relay.ready
            configs.append(config.relay)
            host = SimpleNamespace(deliver=AsyncMock(return_value=DeliveryResult("delivered")))
            hosts.append(host)
            receivers.append(RelayReceiver(config.relay, ReplayStore(tmp_path / f"replay-{index}.db"), host))
        captured = []
        try:
            for index in range(2):
                sender, recipient = identities[index], identities[1 - index]
                message = build_envelope(from_=f"sender@{sender.fleet}", to=f"receiver@{recipient.fleet}", body={"kind": "msg", "text": f"roundtrip-{index}"})
                await RelayMeshSender(configs[index]).send_async(message, recipient.fleet)
                captured.append(queue._db.execute("SELECT sender_fleet,to_fleet,blob FROM messages").fetchone())
                async with RelayHTTPClient(configs[1 - index]) as client:
                    await receivers[1 - index].run_once(client, wait=0)
                received = hosts[1 - index].deliver.await_args.args[0]
                assert received.id == message.id and received.body == message.body
            assert queue._db.execute("SELECT count(*) FROM messages").fetchone() == (0,)
            assert [host.deliver.await_count for host in hosts] == [1, 1]
            await receivers[1].close()
            receivers[1] = RelayReceiver(configs[1], ReplayStore(tmp_path / "replay-1.db"), hosts[1])
            queue.send(*captured[0])
            async with RelayHTTPClient(configs[1]) as client:
                await receivers[1].run_once(client, wait=0)
            assert hosts[1].deliver.await_count == 1
            assert queue._db.execute("SELECT count(*) FROM messages").fetchone() == (0,)
        finally:
            for receiver in receivers:
                await receiver.close()
