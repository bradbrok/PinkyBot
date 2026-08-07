"""Integrated NIP-42/REQ/liveness tests for the production Buzz poller."""

from __future__ import annotations

import asyncio
import json

import pytest
import websockets

from pinky_daemon.agent_registry import AgentRegistry
from pinky_daemon.buzz_inbound import BrokerBuzzPoller
from pinky_outreach.buzz import BuzzNostrSigner, verify_nostr_event

AGENT_KEY = "11" * 32
OWNER = BuzzNostrSigner(bytes.fromhex("33" * 32))
USER = BuzzNostrSigner(bytes.fromhex("44" * 32))
CHANNEL = "00000000-0000-4000-8000-000000000001"
COMMUNITY = "example"


class FakeBroker:
    def __init__(self) -> None:
        self.calls = []
        self.delivered = asyncio.Event()

    async def dispatch_pre_authorized(self, agent_name, message):  # noqa: ANN001
        self.calls.append((agent_name, message))
        self.delivered.set()
        return True


def _registry(tmp_path, relay_url: str):
    store = AgentRegistry(
        str(tmp_path / "agents.db"),
        buzz_device_key_path=str(tmp_path / "identity" / ".device_key"),
    )
    store.register("barsik", model="sonnet", working_dir=str(tmp_path / "barsik"))
    store.bind_buzz_identity_owner_control(
        "barsik",
        private_key=AGENT_KEY,
        relay_url=relay_url,
        community_id=COMMUNITY,
        enabled=True,
        owner_actor="ui:admin",
    )
    store.configure_buzz_inbound_owner_control(
        "barsik",
        owner_pubkey=OWNER.pubkey,
        channels=[{"channel_id": CHANNEL, "label": "#general"}],
        approved_users=[{"pubkey": USER.pubkey, "display_name": "Brad"}],
        owner_actor="ui:admin",
    )
    return store


async def _authenticate(ws, relay_url: str, captured: list[list]) -> list:
    challenge = "challenge-" + "ab" * 16
    await ws.send(json.dumps(["AUTH", challenge]))
    frame = json.loads(await ws.recv())
    captured.append(frame)
    assert frame[0] == "AUTH"
    event = frame[1]
    assert verify_nostr_event(event)
    assert event["kind"] == 22242
    assert event["content"] == ""
    assert event["tags"] == [["relay", relay_url], ["challenge", challenge]]
    await ws.send(json.dumps(["OK", event["id"], True, "authenticated"]))
    return event


@pytest.mark.asyncio
async def test_real_websocket_auth_subscription_ephemeral_suppression_and_eose_health(
    tmp_path,
):
    captured: list[list] = []
    initial_filter = {}
    heartbeat_seen = asyncio.Event()
    relay_url = ""

    durable = USER.sign_event(kind=9, tags=[["h", CHANNEL]], content="hello from Brad")
    ephemeral = USER.sign_event(kind=20002, tags=[["h", CHANNEL]], content="")

    async def handler(ws):  # noqa: ANN001
        await _authenticate(ws, relay_url, captured)
        main = json.loads(await ws.recv())
        captured.append(main)
        assert main[0] == "REQ"
        initial_filter.update(main[2])
        await ws.send(json.dumps(["EVENT", main[1], ephemeral]))
        await ws.send(json.dumps(["EVENT", main[1], durable]))
        await ws.send(json.dumps(["EOSE", main[1]]))
        while True:
            try:
                frame = json.loads(await ws.recv())
            except websockets.ConnectionClosed:
                return
            captured.append(frame)
            if frame[0] == "REQ":
                heartbeat_seen.set()
                await ws.send(json.dumps(["EOSE", frame[1]]))

    async with websockets.serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        relay_url = f"ws://127.0.0.1:{port}"
        store = _registry(tmp_path, relay_url)
        broker = FakeBroker()
        notices = []

        async def notify(agent, message):  # noqa: ANN001
            notices.append((agent, message))
            return True

        material = store.get_buzz_signing_material("barsik")
        poller = BrokerBuzzPoller(
            material,
            broker,
            store,
            notify,
            heartbeat_interval=0.05,
            liveness_timeout=0.3,
            reconnect_base=0.01,
            reconnect_max=0.02,
        )
        task = asyncio.create_task(poller.start())
        await asyncio.wait_for(broker.delivered.wait(), timeout=2)
        await asyncio.wait_for(heartbeat_seen.wait(), timeout=2)
        poller.stop()
        await asyncio.wait_for(task, timeout=2)

        assert initial_filter["kinds"] == [9, 20002]
        assert initial_filter["#h"] == [CHANNEL]
        assert isinstance(initial_filter["since"], int)
        assert len(broker.calls) == 1
        assert broker.calls[0][1].content == "hello from Brad"
        assert poller.health["delivered"] == 1
        assert poller.health["ephemeral_ignored"] == 1
        assert notices == []
        assert store._db.execute(
            "SELECT event_id, kind, delivery_status FROM buzz_inbound_events"
        ).fetchall() == [(durable["id"], 9, "delivered")]
        assert all(frame[0] in {"AUTH", "REQ", "CLOSE"} for frame in captured)
        assert not any(frame[0] == "EVENT" for frame in captured)
        store.close()


@pytest.mark.asyncio
async def test_socket_open_without_eose_heartbeat_pages_owner_and_reconnects(tmp_path):
    relay_url = ""
    notified = asyncio.Event()
    notices = []
    connections = 0

    async def handler(ws):  # noqa: ANN001
        nonlocal connections
        connections += 1
        await _authenticate(ws, relay_url, [])
        main = json.loads(await ws.recv())
        await ws.send(json.dumps(["EOSE", main[1]]))
        # Keep the TCP/WebSocket open but intentionally never answer the next
        # heartbeat REQ. This is the exact socket-open/subscription-dead seam.
        try:
            while True:
                await ws.recv()
        except websockets.ConnectionClosed:
            return

    async with websockets.serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        relay_url = f"ws://127.0.0.1:{port}"
        store = _registry(tmp_path, relay_url)

        async def notify(agent, message):  # noqa: ANN001
            notices.append((agent, message))
            notified.set()
            return True

        poller = BrokerBuzzPoller(
            store.get_buzz_signing_material("barsik"),
            FakeBroker(),
            store,
            notify,
            heartbeat_interval=0.05,
            liveness_timeout=0.08,
            reconnect_base=0.01,
            reconnect_max=0.02,
        )
        task = asyncio.create_task(poller.start())
        await asyncio.wait_for(notified.wait(), timeout=2)

        assert connections >= 1
        assert notices[0][0] == "barsik"
        assert "active EOSE liveness failed" in notices[0][1]
        assert poller.health["last_error"] == "BuzzRelayLivenessError"
        poller.stop()
        await asyncio.wait_for(task, timeout=2)
        store.close()


@pytest.mark.asyncio
async def test_restart_replays_overlap_but_dedupes_delivered_event(tmp_path):
    relay_url = ""
    connection_number = 0
    first = USER.sign_event(kind=9, tags=[["h", CHANNEL]], content="first")
    second = USER.sign_event(kind=9, tags=[["h", CHANNEL]], content="second")

    async def handler(ws):  # noqa: ANN001
        nonlocal connection_number
        connection_number += 1
        this_connection = connection_number
        await _authenticate(ws, relay_url, [])
        main = json.loads(await ws.recv())
        await ws.send(json.dumps(["EVENT", main[1], first]))
        if this_connection >= 2:
            await ws.send(json.dumps(["EVENT", main[1], second]))
        await ws.send(json.dumps(["EOSE", main[1]]))
        try:
            while True:
                await ws.recv()
        except websockets.ConnectionClosed:
            return

    async with websockets.serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        relay_url = f"ws://127.0.0.1:{port}"
        store = _registry(tmp_path, relay_url)
        broker = FakeBroker()

        async def notify(_agent, _message):
            return True

        first_poller = BrokerBuzzPoller(
            store.get_buzz_signing_material("barsik"),
            broker,
            store,
            notify,
            heartbeat_interval=10,
            liveness_timeout=1,
        )
        first_task = asyncio.create_task(first_poller.start())
        await asyncio.wait_for(broker.delivered.wait(), timeout=2)
        first_poller.stop()
        await asyncio.wait_for(first_task, timeout=2)

        broker.delivered.clear()
        second_poller = BrokerBuzzPoller(
            store.get_buzz_signing_material("barsik"),
            broker,
            store,
            notify,
            heartbeat_interval=10,
            liveness_timeout=1,
        )
        second_task = asyncio.create_task(second_poller.start())
        await asyncio.wait_for(broker.delivered.wait(), timeout=2)
        second_poller.stop()
        await asyncio.wait_for(second_task, timeout=2)

        assert [call[1].content for call in broker.calls] == ["first", "second"]
        assert (
            store._db.execute(
                "SELECT COUNT(*) FROM buzz_inbound_events WHERE delivery_status='delivered'"
            ).fetchone()[0]
            == 2
        )
        store.close()
