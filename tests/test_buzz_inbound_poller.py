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
STRANGER = BuzzNostrSigner(bytes.fromhex("55" * 32))
RELAY_AUTHORITY = BuzzNostrSigner(bytes.fromhex("66" * 32))
CHANNEL = "00000000-0000-4000-8000-000000000001"
OTHER_CHANNEL = "00000000-0000-4000-8000-000000000002"
COMMUNITY = "example"


class FakeBroker:
    def __init__(self) -> None:
        self.calls = []
        self.delivered = asyncio.Event()

    async def dispatch_pre_authorized(self, agent_name, message):  # noqa: ANN001
        self.calls.append((agent_name, message))
        self.delivered.set()
        return True


def _registry(tmp_path, relay_url: str, *, channels: list[dict] | None = None):
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
        relay_signing_pubkey=RELAY_AUTHORITY.pubkey,
        enabled=True,
        owner_actor="ui:admin",
    )
    store.configure_buzz_inbound_owner_control(
        "barsik",
        owner_pubkey=OWNER.pubkey,
        channels=channels or [{"channel_id": CHANNEL, "label": "#general"}],
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


class LiveFanoutQuirkRelayRig:
    """Model Buzz live suppression for wire-``since`` or multi-``#h`` REQs."""

    def __init__(
        self,
        *,
        channels: list[str],
        stored_events: list[dict],
        live_events: list[dict],
        hold_final_eose: bool = False,
    ) -> None:
        self.relay_url = ""
        self.channels = channels
        self.stored_events = stored_events
        self.live_events = live_events
        self.hold_final_eose = hold_final_eose
        self.captured: list[list] = []
        self.main_requests: list[list] = []
        self.membership_request: list | None = None
        self.main_requests_received = asyncio.Event()
        self.partial_eose_sent = asyncio.Event()
        self.release_final_eose = asyncio.Event()
        self.all_eose_sent = asyncio.Event()
        self.stored_batch_sent = asyncio.Event()
        self.release_live_events = asyncio.Event()
        self.live_events_pushed = asyncio.Event()

    @staticmethod
    def _channel_id(event: dict) -> str:
        return next(tag[1] for tag in event["tags"] if tag[0] == "h")

    async def handler(self, ws) -> None:  # noqa: ANN001
        await _authenticate(ws, self.relay_url, self.captured)
        membership = json.loads(await ws.recv())
        self.captured.append(membership)
        assert membership[0] == "REQ"
        self.membership_request = membership
        await ws.send(json.dumps(["EOSE", membership[1]]))
        for _ in self.channels:
            main = json.loads(await ws.recv())
            self.captured.append(main)
            assert main[0] == "REQ"
            self.main_requests.append(main)
        self.main_requests_received.set()

        # Stored-query delivery works for every filter shape on the real
        # relay, including filters with ``since`` or multiple ``#h`` values.
        for event in self.stored_events:
            channel_id = self._channel_id(event)
            for main in self.main_requests:
                if channel_id in main[2].get("#h", []):
                    await ws.send(json.dumps(["EVENT", main[1], event]))
        eose_requests = self.main_requests
        if self.hold_final_eose:
            eose_requests = self.main_requests[:-1]
        for main in eose_requests:
            await ws.send(json.dumps(["EOSE", main[1]]))
        if self.hold_final_eose:
            self.partial_eose_sent.set()
            await self.release_final_eose.wait()
            await ws.send(json.dumps(["EOSE", self.main_requests[-1][1]]))
        self.all_eose_sent.set()
        self.stored_batch_sent.set()

        await self.release_live_events.wait()
        pushed = 0
        for main in self.main_requests:
            subscription_filter = main[2]
            # These are the two independent production quirks isolated by the
            # controlled live probe matrices: either shape suppresses fan-out.
            if "since" in subscription_filter or len(subscription_filter.get("#h", [])) != 1:
                continue
            for event in self.live_events:
                if self._channel_id(event) in subscription_filter["#h"]:
                    await ws.send(json.dumps(["EVENT", main[1], event]))
                    pushed += 1
        if pushed:
            self.live_events_pushed.set()

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
    membership_seen = asyncio.Event()
    channel_seen = asyncio.Event()
    heartbeat_seen = asyncio.Event()
    relay_url = ""

    durable = USER.sign_event(kind=9, tags=[["h", CHANNEL]], content="hello from Brad")
    ephemeral = USER.sign_event(kind=20002, tags=[["h", CHANNEL]], content="")

    async def handler(ws):  # noqa: ANN001
        await _authenticate(ws, relay_url, captured)
        membership = json.loads(await ws.recv())
        captured.append(membership)
        membership_seen.set()
        await ws.send(json.dumps(["EOSE", membership[1]]))
        main = json.loads(await ws.recv())
        captured.append(main)
        assert main[0] == "REQ"
        channel_seen.set()
        await ws.send(json.dumps(["EVENT", main[1], ephemeral]))
        await ws.send(json.dumps(["EVENT", main[1], durable]))
        await ws.send(json.dumps(["EOSE", main[1]]))
        while True:
            try:
                frame = json.loads(await ws.recv())
            except websockets.ConnectionClosed:
                return
            captured.append(frame)
            if frame[0] == "REQ" and frame[1].startswith("pinky-live-"):
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
        await asyncio.wait_for(membership_seen.wait(), timeout=5)
        await asyncio.wait_for(channel_seen.wait(), timeout=5)
        await asyncio.wait_for(broker.delivered.wait(), timeout=5)
        await asyncio.wait_for(heartbeat_seen.wait(), timeout=5)
        poller.stop()
        await asyncio.wait_for(task, timeout=5)

        membership_filters = [
            frame[2]
            for frame in captured
            if frame[0] == "REQ" and frame[1].startswith("pinky-membership-")
        ]
        assert {
            json.dumps(item, sort_keys=True) for item in membership_filters
        } == {
            json.dumps(
                {"kinds": [44100, 44101], "#p": [material.pubkey]},
                sort_keys=True,
            )
        }
        channel_filters = [
            frame[2]
            for frame in captured
            if frame[0] == "REQ" and frame[1].startswith("pinky-barsik-")
        ]
        assert {json.dumps(item, sort_keys=True) for item in channel_filters} == {
            json.dumps(
                {"kinds": [9, 20002], "#h": [CHANNEL]}, sort_keys=True
            )
        }
        assert len(broker.calls) == 1
        assert broker.calls[0][1].message_id == durable["id"]
        assert broker.calls[0][1].content == "hello from Brad"
        assert poller.health["delivered"] == 1
        assert poller.health["ephemeral_ignored"] >= 1
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
        assert all(type(item["since"]) is int for item in heartbeat_filters)
        assert {
            json.dumps({**item, "since": "<dynamic>"}, sort_keys=True)
            for item in heartbeat_filters
        } == {
            json.dumps(
                {
                    "kinds": [9, 20002],
                    "#h": [CHANNEL],
                    "since": "<dynamic>",
                    "limit": 1,
                },
                sort_keys=True,
            )
        }
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
        membership = json.loads(await ws.recv())
        await ws.send(json.dumps(["EOSE", membership[1]]))
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
        membership = json.loads(await ws.recv())
        captured.append(membership)
        await ws.send(json.dumps(["EOSE", membership[1]]))
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
        membership = json.loads(await ws.recv())
        await ws.send(json.dumps(["EOSE", membership[1]]))
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
    rig = LiveFanoutQuirkRelayRig(
        channels=[CHANNEL],
        stored_events=stored_burst,
        live_events=[live_event],
    )

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

        assert rig.main_requests[0][2] == {"kinds": [9, 20002], "#h": [CHANNEL]}
        assert poller.health["rejected"] == len(stored_burst)
        assert poller._processor._recent_ids == set()
        assert broker.calls == []
        assert store._db.execute(
            "SELECT COUNT(*) FROM buzz_inbound_events"
        ).fetchone()[0] == 0
        assert store._db.execute(
            "SELECT last_seen_at FROM buzz_inbound_principals WHERE agent='barsik'"
        ).fetchall() == [(0.0,), (0.0,)]

        rig.release_live_events.set()
        await asyncio.wait_for(rig.live_events_pushed.wait(), timeout=2)
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
async def test_live_fanout_uses_one_req_per_channel_and_waits_for_every_eose(tmp_path):
    stored_event = USER.sign_event(
        kind=9,
        tags=[["h", CHANNEL]],
        content="stored event before all EOSE",
    )
    live_events = [
        USER.sign_event(
            kind=9,
            tags=[["h", CHANNEL]],
            content="live event in general",
        ),
        USER.sign_event(
            kind=9,
            tags=[["h", OTHER_CHANNEL]],
            content="live event in support",
        ),
    ]
    rig = LiveFanoutQuirkRelayRig(
        channels=[CHANNEL, OTHER_CHANNEL],
        stored_events=[stored_event],
        live_events=live_events,
        hold_final_eose=True,
    )

    async with websockets.serve(rig.handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        rig.relay_url = f"ws://127.0.0.1:{port}"
        store = _registry(
            tmp_path,
            rig.relay_url,
            channels=[
                {"channel_id": CHANNEL, "label": "#general"},
                {"channel_id": OTHER_CHANNEL, "label": "#support"},
            ],
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
        await asyncio.wait_for(rig.main_requests_received.wait(), timeout=2)
        await asyncio.wait_for(rig.partial_eose_sent.wait(), timeout=2)
        await asyncio.wait_for(broker.delivered.wait(), timeout=2)
        for _ in range(200):
            if poller.poll_count >= 4:
                break
            await asyncio.sleep(0.01)

        assert poller.poll_count >= 4
        assert poller.health["status"] == "starting"
        assert store.get_buzz_inbound_policy("barsik")["status"] != "connected"
        assert len(rig.main_requests) == 2
        assert len({frame[1] for frame in rig.main_requests}) == 2
        assert {tuple(frame[2]["#h"]) for frame in rig.main_requests} == {
            (CHANNEL,),
            (OTHER_CHANNEL,),
        }
        assert all(frame[2]["kinds"] == [9, 20002] for frame in rig.main_requests)
        assert all("since" not in frame[2] for frame in rig.main_requests)
        assert rig.membership_request is not None
        assert rig.membership_request[2] == {
            "kinds": [44100, 44101],
            "#p": [store.get_buzz_identity("barsik")["pubkey"]],
        }

        rig.release_final_eose.set()
        await asyncio.wait_for(rig.all_eose_sent.wait(), timeout=2)
        for _ in range(200):
            if poller.health["status"] == "connected":
                break
            await asyncio.sleep(0.01)
        assert poller.health["status"] == "connected"
        assert store.get_buzz_inbound_policy("barsik")["status"] == "connected"

        rig.release_live_events.set()
        await asyncio.wait_for(rig.live_events_pushed.wait(), timeout=2)
        for _ in range(200):
            if len(broker.calls) == len(live_events) + 1:
                break
            await asyncio.sleep(0.01)
        poller.stop()
        await asyncio.wait_for(task, timeout=2)

        delivered_ids = [call[1].message_id for call in broker.calls]
        assert delivered_ids[0] == stored_event["id"]
        assert all(delivered_ids.count(event["id"]) == 1 for event in live_events)
        assert len(delivered_ids) == len(set(delivered_ids)) == 3
        assert store._db.execute(
            "SELECT event_id, delivery_status FROM buzz_inbound_events ORDER BY event_id"
        ).fetchall() == sorted(
            (event["id"], "delivered") for event in [stored_event, *live_events]
        )
        assert not any(frame[0] == "EVENT" for frame in rig.captured)
        store.close()


@pytest.mark.asyncio
async def test_membership_add_opens_channel_live_and_remove_closes_it(tmp_path):
    relay_url = ""
    agent_pubkey = BuzzNostrSigner(bytes.fromhex(AGENT_KEY)).pubkey
    added = RELAY_AUTHORITY.sign_event(
        kind=44100,
        tags=[["p", agent_pubkey], ["h", OTHER_CHANNEL], ["name", "#support"]],
        content="",
    )
    removed = RELAY_AUTHORITY.sign_event(
        kind=44101,
        tags=[["p", agent_pubkey], ["h", OTHER_CHANNEL]],
        content="",
    )
    channel_message = USER.sign_event(
        kind=9,
        tags=[["h", OTHER_CHANNEL]],
        content="live after membership add",
    )
    pre_membership_message = USER.sign_event(
        kind=9,
        tags=[["h", OTHER_CHANNEL]],
        content="stale before membership add",
        created_at=added["created_at"] - 1,
    )
    dynamic_opened = asyncio.Event()
    release_remove = asyncio.Event()
    dynamic_closed = asyncio.Event()
    captured: list[list] = []

    async def handler(ws):  # noqa: ANN001
        await _authenticate(ws, relay_url, captured)
        membership = json.loads(await ws.recv())
        captured.append(membership)
        await ws.send(json.dumps(["EOSE", membership[1]]))
        initial = json.loads(await ws.recv())
        captured.append(initial)
        await ws.send(json.dumps(["EOSE", initial[1]]))
        await ws.send(json.dumps(["EVENT", membership[1], added]))

        dynamic = json.loads(await ws.recv())
        captured.append(dynamic)
        assert dynamic[0] == "REQ"
        assert dynamic[2] == {"kinds": [9, 20002], "#h": [OTHER_CHANNEL]}
        dynamic_opened.set()
        await ws.send(json.dumps(["EOSE", dynamic[1]]))
        await ws.send(json.dumps(["EVENT", dynamic[1], pre_membership_message]))
        await ws.send(json.dumps(["EVENT", dynamic[1], channel_message]))

        await release_remove.wait()
        await ws.send(json.dumps(["EVENT", membership[1], removed]))
        close = json.loads(await ws.recv())
        captured.append(close)
        assert close == ["CLOSE", dynamic[1]]
        dynamic_closed.set()
        try:
            while True:
                frame = json.loads(await ws.recv())
                captured.append(frame)
                if frame[0] == "REQ":
                    await ws.send(json.dumps(["EOSE", frame[1]]))
        except websockets.ConnectionClosed:
            return

    async with websockets.serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        relay_url = f"ws://127.0.0.1:{port}"
        store = _registry(tmp_path, relay_url)
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
        await asyncio.wait_for(dynamic_opened.wait(), timeout=2)
        await asyncio.wait_for(broker.delivered.wait(), timeout=2)

        channel = store.get_buzz_inbound_channel(
            "barsik", COMMUNITY, relay_url, OTHER_CHANNEL
        )
        assert channel == {"channel_id": OTHER_CHANNEL, "label": "#support"}
        support = next(
            chat for chat in store.list_group_chats("barsik")
            if chat["chat_id"] == OTHER_CHANNEL
        )
        assert (support["platform"], support["chat_title"], support["active"]) == (
            "buzz",
            "#support",
            True,
        )
        assert [call[1].content for call in broker.calls] == ["live after membership add"]
        assert poller.health["rejected"] == 1

        release_remove.set()
        await asyncio.wait_for(dynamic_closed.wait(), timeout=2)
        assert store.get_buzz_inbound_channel(
            "barsik", COMMUNITY, relay_url, OTHER_CHANNEL
        ) is None
        support = next(
            chat for chat in store.list_group_chats("barsik", active_only=False)
            if chat["chat_id"] == OTHER_CHANNEL
        )
        assert support["active"] is False

        poller.stop()
        await asyncio.wait_for(task, timeout=2)
        store.close()


@pytest.mark.asyncio
async def test_membership_history_newest_removal_cannot_be_reopened_by_older_add(tmp_path):
    relay_url = "ws://127.0.0.1:1"
    store = _registry(tmp_path, relay_url)
    material = store.get_buzz_signing_material("barsik")
    poller = BrokerBuzzPoller(material, FakeBroker(), store, lambda *_args: True)
    now = int(time.time())
    removed = RELAY_AUTHORITY.sign_event(
        kind=44101,
        tags=[["p", material.pubkey], ["h", OTHER_CHANNEL]],
        content="",
        created_at=now - 5,
    )
    older_add = RELAY_AUTHORITY.sign_event(
        kind=44100,
        tags=[["p", material.pubkey], ["h", OTHER_CHANNEL]],
        content="",
        created_at=now - 10,
    )

    class FakeSocket:
        def __init__(self):
            self.sent = []

        async def send(self, payload):  # noqa: ANN001
            self.sent.append(json.loads(payload))

    ws = FakeSocket()
    subscription_since: dict[str, int] = {}
    channel_subscriptions: dict[str, str] = {}
    channels = [CHANNEL]
    kwargs = {
        "subscription_since": subscription_since,
        "channel_subscriptions": channel_subscriptions,
        "channels": channels,
        "subscription_floor": now - 60,
        "connection_token": "test",
    }
    await poller._handle_membership_event(ws, removed, **kwargs)
    await poller._handle_membership_event(ws, older_add, **kwargs)

    assert store.get_buzz_inbound_channel(
        "barsik", COMMUNITY, relay_url, OTHER_CHANNEL
    ) is None
    assert OTHER_CHANNEL not in channels
    assert channel_subscriptions == {}
    assert ws.sent == []
    store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stored_pin", "signer", "expected_log"),
    [
        ("", RELAY_AUTHORITY, "membership processing disabled"),
        (RELAY_AUTHORITY.pubkey, STRANGER, "rejected membership event"),
    ],
    ids=["absent-pin", "stranger-55-scalar"],
)
async def test_membership_authority_failures_are_inert_and_loud(
    tmp_path,
    capsys,
    stored_pin,
    signer,
    expected_log,
):
    relay_url = "ws://127.0.0.1:1"
    store = _registry(tmp_path, relay_url)
    if not stored_pin:
        store._db.execute(
            "UPDATE buzz_identities SET relay_signing_pubkey='' WHERE agent='barsik'"
        )
        store._db.commit()
    material = store.get_buzz_signing_material("barsik")
    poller = BrokerBuzzPoller(material, FakeBroker(), store, lambda *_args: True)
    forged = signer.sign_event(
        kind=44100,
        tags=[["p", material.pubkey], ["h", OTHER_CHANNEL], ["name", "#forged"]],
        content="",
    )

    class FakeSocket:
        def __init__(self):
            self.sent = []

        async def send(self, payload):  # noqa: ANN001
            self.sent.append(json.loads(payload))

    ws = FakeSocket()
    channels = [CHANNEL]
    channel_subscriptions: dict[str, str] = {}
    await poller._handle_membership_event(
        ws,
        forged,
        subscription_since={},
        channel_subscriptions=channel_subscriptions,
        channels=channels,
        subscription_floor=int(time.time()) - 60,
        connection_token="test",
    )

    assert store.get_buzz_inbound_channel(
        "barsik", COMMUNITY, relay_url, OTHER_CHANNEL
    ) is None
    assert all(
        chat["chat_id"] != OTHER_CHANNEL
        for chat in store.list_group_chats("barsik", active_only=False)
    )
    assert channels == [CHANNEL]
    assert channel_subscriptions == {}
    assert poller._membership_versions == {}
    assert ws.sent == []
    captured = capsys.readouterr().err
    assert expected_log in captured
    if stored_pin:
        assert f"{signer.pubkey[:12]}…" in captured
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
        membership = json.loads(await ws.recv())
        await ws.send(json.dumps(["EOSE", membership[1]]))
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
