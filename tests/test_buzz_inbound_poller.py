"""Integrated NIP-42/REQ/liveness tests for the production Buzz poller."""

from __future__ import annotations

import asyncio
import json
import time

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


class SinceBlindLiveRelayRig:
    """Model Buzz: stored queries work, but wire-``since`` disables live push."""

    def __init__(self, *, stored_events: list[dict], live_event: dict) -> None:
        self.relay_url = ""
        self.stored_events = stored_events
        self.live_event = live_event
        self.captured: list[list] = []
        self.main_filter: dict = {}
        self.stored_batch_sent = asyncio.Event()
        self.release_live_event = asyncio.Event()
        self.live_event_pushed = asyncio.Event()

    async def handler(self, ws) -> None:  # noqa: ANN001
        await _authenticate(ws, self.relay_url, self.captured)
        main = json.loads(await ws.recv())
        self.captured.append(main)
        assert main[0] == "REQ"
        self.main_filter.update(main[2])

        # Stored-query delivery works for every filter shape on the real
        # relay, including filters with ``since``.  A no-since main REQ can
        # therefore receive the channel's full history before EOSE.
        for event in self.stored_events:
            await ws.send(json.dumps(["EVENT", main[1], event]))
        await ws.send(json.dumps(["EOSE", main[1]]))
        self.stored_batch_sent.set()

        await self.release_live_event.wait()
        # This is the production quirk isolated by the live probe matrix.
        if "since" not in self.main_filter:
            await ws.send(json.dumps(["EVENT", main[1], self.live_event]))
            self.live_event_pushed.set()

        try:
            while True:
                frame = json.loads(await ws.recv())
                self.captured.append(frame)
                if frame[0] == "REQ":
                    await ws.send(json.dumps(["EOSE", frame[1]]))
        except websockets.ConnectionClosed:
            return


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
        assert "since" not in initial_filter
        assert len(broker.calls) == 1
        assert broker.calls[0][1].content == "hello from Brad"
        assert poller.health["delivered"] == 1
        assert poller.health["ephemeral_ignored"] == 1
        assert notices == []
        assert store._db.execute(
            "SELECT event_id, kind, delivery_status FROM buzz_inbound_events"
        ).fetchall() == [(durable["id"], 9, "delivered")]
        heartbeat_filters = [
            frame[2]
            for frame in captured
            if frame[0] == "REQ" and frame[1].startswith("pinky-live-")
        ]
        assert heartbeat_filters
        assert all(
            isinstance(item["since"], int) and item["limit"] == 1
            for item in heartbeat_filters
        )
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
async def test_control_frame_trickle_cannot_starve_periodic_eose_probe(tmp_path):
    relay_url = ""
    heartbeat_seen = asyncio.Event()
    heartbeat_closed = asyncio.Event()
    captured: list[list] = []

    async def handler(ws):  # noqa: ANN001
        await _authenticate(ws, relay_url, captured)
        main = json.loads(await ws.recv())
        captured.append(main)
        await ws.send(json.dumps(["EOSE", main[1]]))
        counter = 0
        try:
            while True:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=0.01)
                except asyncio.TimeoutError:
                    # Alternate an ignorable OK with an unknown control shape.
                    # Neither proves the subscription is alive.
                    frame = (
                        ["OK", "00" * 32, True, "control trickle"]
                        if counter % 2 == 0
                        else ["IGNORED", counter]
                    )
                    counter += 1
                    await ws.send(json.dumps(frame))
                    continue
                frame = json.loads(raw)
                captured.append(frame)
                if frame[0] == "REQ":
                    heartbeat_seen.set()
                    await ws.send(json.dumps(["EOSE", frame[1]]))
                elif frame[0] == "CLOSE":
                    heartbeat_closed.set()
        except websockets.ConnectionClosed:
            return

    async with websockets.serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        relay_url = f"ws://127.0.0.1:{port}"
        store = _registry(tmp_path, relay_url)
        notices = []

        async def notify(agent, message):  # noqa: ANN001
            notices.append((agent, message))
            return True

        poller = BrokerBuzzPoller(
            store.get_buzz_signing_material("barsik"),
            FakeBroker(),
            store,
            notify,
            heartbeat_interval=0.05,
            liveness_timeout=0.3,
        )
        task = asyncio.create_task(poller.start())
        await asyncio.wait_for(heartbeat_seen.wait(), timeout=2)
        await asyncio.wait_for(heartbeat_closed.wait(), timeout=2)
        poller.stop()
        await asyncio.wait_for(task, timeout=2)

        assert any(frame[0] == "REQ" and frame[1].startswith("pinky-live-") for frame in captured)
        assert poller.health["last_liveness_at"] > 0
        assert notices == []
        assert not any(frame[0] == "EVENT" for frame in captured)
        store.close()


@pytest.mark.asyncio
async def test_wire_subscription_omits_since_but_client_floor_rejects_stale_event(
    tmp_path,
    monkeypatch,
):
    relay_url = ""
    subscription_filter = {}
    subscription_floor = int(time.time()) - 60

    async def handler(ws):  # noqa: ANN001
        await _authenticate(ws, relay_url, [])
        main = json.loads(await ws.recv())
        subscription_filter.update(main[2])
        stale = USER.sign_event(
            kind=9,
            tags=[["h", CHANNEL]],
            content="historic signed command",
            created_at=subscription_floor - 86340,
        )
        fresh = USER.sign_event(
            kind=9,
            tags=[["h", CHANNEL]],
            content="current signed command",
            created_at=subscription_floor,
        )
        await ws.send(json.dumps(["EVENT", main[1], stale]))
        await ws.send(json.dumps(["EVENT", main[1], fresh]))
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
        monkeypatch.setattr(
            store,
            "get_buzz_subscription_since",
            lambda _agent_name: subscription_floor,
        )
        broker = FakeBroker()

        async def notify(_agent, _message):
            return True

        poller = BrokerBuzzPoller(
            store.get_buzz_signing_material("barsik"),
            broker,
            store,
            notify,
            heartbeat_interval=10,
            liveness_timeout=1,
        )
        task = asyncio.create_task(poller.start())
        await asyncio.wait_for(broker.delivered.wait(), timeout=2)
        poller.stop()
        await asyncio.wait_for(task, timeout=2)

        assert "since" not in subscription_filter
        assert [call[1].content for call in broker.calls] == ["current signed command"]
        assert poller.health["rejected"] == 1
        assert store._db.execute(
            "SELECT event_created_at, delivery_status FROM buzz_inbound_events"
        ).fetchall() == [(float(subscription_floor), "delivered")]
        store.close()


@pytest.mark.asyncio
async def test_since_blind_relay_live_push_survives_large_stale_eose_burst(
    tmp_path,
    monkeypatch,
):
    subscription_floor = int(time.time()) - 60
    stale_events = [
        USER.sign_event(
            kind=9,
            tags=[["h", CHANNEL]],
            content=f"historic signed command {index}",
            created_at=subscription_floor - 86400 - index,
        )
        for index in range(256)
    ]
    # Repeat part of the history burst. Rejected pre-floor events must not
    # consume the recent-ID cache or reach durable dedupe state.
    stored_burst = [*stale_events, *stale_events[:32]]
    live_event = USER.sign_event(
        kind=9,
        tags=[["h", CHANNEL]],
        content="live event after EOSE",
        created_at=subscription_floor,
    )
    rig = SinceBlindLiveRelayRig(stored_events=stored_burst, live_event=live_event)

    async with websockets.serve(rig.handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        rig.relay_url = f"ws://127.0.0.1:{port}"
        store = _registry(tmp_path, rig.relay_url)
        monkeypatch.setattr(
            store,
            "get_buzz_subscription_since",
            lambda _agent_name: subscription_floor,
        )
        broker = FakeBroker()

        async def notify(_agent, _message):
            return True

        poller = BrokerBuzzPoller(
            store.get_buzz_signing_material("barsik"),
            broker,
            store,
            notify,
            heartbeat_interval=10,
            liveness_timeout=2,
        )
        task = asyncio.create_task(poller.start())
        await asyncio.wait_for(rig.stored_batch_sent.wait(), timeout=2)

        for _ in range(200):
            if poller.health["status"] == "connected":
                break
            await asyncio.sleep(0.01)
        assert poller.health["status"] == "connected"

        assert rig.main_filter == {"kinds": [9, 20002], "#h": [CHANNEL]}
        assert poller.health["rejected"] == len(stored_burst)
        assert poller._processor._recent_ids == set()
        assert broker.calls == []
        assert store._db.execute(
            "SELECT COUNT(*) FROM buzz_inbound_events"
        ).fetchone()[0] == 0
        assert store._db.execute(
            "SELECT last_seen_at FROM buzz_inbound_principals WHERE agent='barsik'"
        ).fetchall() == [(0.0,), (0.0,)]

        rig.release_live_event.set()
        await asyncio.wait_for(rig.live_event_pushed.wait(), timeout=2)
        await asyncio.wait_for(broker.delivered.wait(), timeout=2)
        poller.stop()
        await asyncio.wait_for(task, timeout=2)

        assert [call[1].content for call in broker.calls] == ["live event after EOSE"]
        assert poller.health["delivered"] == 1
        assert poller._processor._recent_ids == {live_event["id"]}
        assert store._db.execute(
            "SELECT event_id, delivery_status FROM buzz_inbound_events"
        ).fetchall() == [(live_event["id"], "delivered")]
        assert not any(frame[0] == "EVENT" for frame in rig.captured)
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
