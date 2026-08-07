"""Native Buzz inbound verification, authorization, and relay supervision."""

from __future__ import annotations

import asyncio
import inspect
import json
import secrets
import sys
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable
from uuid import UUID

import websockets

from pinky_daemon.broker import BrokerMessage
from pinky_outreach.buzz import BuzzNostrSigner, verify_nostr_event

_HEX_64 = frozenset("0123456789abcdef")
_ALLOWED_KINDS = {9, 20002}
_MAX_FRAME_BYTES = 128 * 1024
_MAX_CONTENT_BYTES = 32 * 1024
_MAX_TAGS = 64
_MAX_TAG_PARTS = 4
_MAX_TAG_PART_BYTES = 512
_MAX_FUTURE_SECONDS = 5 * 60
_RECENT_EVENT_IDS = 4096


def _log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


class BuzzInboundError(RuntimeError):
    """Base inbound Buzz failure."""


class BuzzInboundRejectedError(BuzzInboundError):
    """An event failed strict schema, cryptographic, or target validation."""


class BuzzInboundDeliveryError(BuzzInboundError):
    """A verified/authorized durable event was not accepted by the persona."""


class BuzzRelayProtocolError(BuzzInboundError):
    """The relay failed the required NIP-42/NIP-01 protocol contract."""


class BuzzRelayLivenessError(BuzzInboundError):
    """The relay socket existed but failed an active EOSE liveness probe."""


@dataclass(frozen=True)
class ValidatedBuzzInboundEvent:
    event: dict
    channel_id: str
    mentioned_self: bool
    reply_to: str


def _is_hex_64(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and not (set(value) - _HEX_64)


def validate_buzz_inbound_event(
    event: object,
    *,
    identity_pubkey: str,
    now: float | None = None,
    subscription_since: int | None = None,
) -> ValidatedBuzzInboundEvent:
    """Strictly validate one event before either inbound authorization gate."""
    if not isinstance(event, dict):
        raise BuzzInboundRejectedError("event_not_object")
    try:
        encoded = json.dumps(event, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise BuzzInboundRejectedError("event_not_json") from exc
    if len(encoded) > _MAX_FRAME_BYTES:
        raise BuzzInboundRejectedError("event_too_large")
    if not verify_nostr_event(event):
        raise BuzzInboundRejectedError("event_signature_or_id_invalid")
    if event["kind"] not in _ALLOWED_KINDS:
        raise BuzzInboundRejectedError("event_kind_not_allowed")
    if event["pubkey"] == identity_pubkey:
        raise BuzzInboundRejectedError("self_event")
    current = time.time() if now is None else float(now)
    if event["created_at"] > int(current + _MAX_FUTURE_SECONDS):
        raise BuzzInboundRejectedError("event_from_future")
    if subscription_since is not None and event["created_at"] < int(subscription_since):
        # A relay filter is advisory, not an authorization boundary. Enforce
        # the exact REQ floor again before any gate, cache, or durable write.
        raise BuzzInboundRejectedError("event_before_subscription_since")
    if len(event["content"].encode("utf-8")) > _MAX_CONTENT_BYTES:
        raise BuzzInboundRejectedError("content_too_large")
    tags = event["tags"]
    if len(tags) > _MAX_TAGS:
        raise BuzzInboundRejectedError("too_many_tags")
    if any(
        len(tag) < 2
        or len(tag) > _MAX_TAG_PARTS
        or any(len(part.encode("utf-8")) > _MAX_TAG_PART_BYTES for part in tag)
        for tag in tags
    ):
        raise BuzzInboundRejectedError("tag_bounds_invalid")

    channel_tags = [tag for tag in tags if tag[0] == "h"]
    if len(channel_tags) != 1 or len(channel_tags[0]) != 2:
        raise BuzzInboundRejectedError("channel_tag_invalid")
    channel_id = channel_tags[0][1]
    try:
        if str(UUID(channel_id)) != channel_id:
            raise ValueError
    except (TypeError, ValueError, AttributeError) as exc:
        raise BuzzInboundRejectedError("channel_tag_invalid") from exc

    p_tags = [tag for tag in tags if tag[0] == "p"]
    if len(p_tags) > 1:
        raise BuzzInboundRejectedError("target_pubkey_duplicate")
    if p_tags:
        if len(p_tags[0]) != 2 or not _is_hex_64(p_tags[0][1]):
            raise BuzzInboundRejectedError("target_pubkey_malformed")
        if p_tags[0][1] != identity_pubkey:
            # Conservative inc2 choice: a message explicitly addressed to a
            # different identity never enters this persona. No-p broadcasts
            # remain eligible after both load-bearing authorization gates.
            raise BuzzInboundRejectedError("target_pubkey_foreign")

    reply_tags: list[str] = []
    for tag in tags:
        if tag[0] != "e":
            continue
        if not _is_hex_64(tag[1]):
            raise BuzzInboundRejectedError("reply_event_id_invalid")
        if len(tag) == 4 and tag[3] == "reply":
            reply_tags.append(tag[1])
    if len(reply_tags) > 1:
        raise BuzzInboundRejectedError("reply_tag_duplicate")

    return ValidatedBuzzInboundEvent(
        event=event,
        channel_id=channel_id,
        mentioned_self=bool(p_tags),
        reply_to=reply_tags[0] if reply_tags else "",
    )


class BuzzInboundProcessor:
    """Apply both DB-backed authorization gates before persona dispatch."""

    def __init__(
        self,
        *,
        registry,
        broker,
        agent_name: str,
        identity_pubkey: str,
        community_id: str,
        relay_url: str,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._registry = registry
        self._broker = broker
        self.agent_name = agent_name
        self.identity_pubkey = identity_pubkey
        self.community_id = community_id
        self.relay_url = relay_url
        self._clock = clock
        self._recent_order: deque[str] = deque()
        self._recent_ids: set[str] = set()
        self.stats = {
            "delivered": 0,
            "duplicates": 0,
            "ephemeral_ignored": 0,
            "rejected": 0,
            "unlisted_channel": 0,
            "unapproved_principal": 0,
        }

    def _remember(self, event_id: str) -> bool:
        if event_id in self._recent_ids:
            return False
        self._recent_ids.add(event_id)
        self._recent_order.append(event_id)
        if len(self._recent_order) > _RECENT_EVENT_IDS:
            self._recent_ids.discard(self._recent_order.popleft())
        return True

    async def process_event(
        self,
        event: object,
        *,
        replay: bool = False,
        subscription_since: int | None = None,
    ) -> str:
        try:
            validated = validate_buzz_inbound_event(
                event,
                identity_pubkey=self.identity_pubkey,
                now=self._clock(),
                # Durable pending rows were admitted under an earlier live
                # subscription and are the only sanctioned freshness bypass.
                subscription_since=None if replay else subscription_since,
            )
        except BuzzInboundRejectedError:
            self.stats["rejected"] += 1
            return "rejected"

        # kind-20002 is typing/diagnostic only. Return before the in-memory
        # duplicate cache, either authorization lookup, or any durable write.
        if validated.event["kind"] == 20002:
            self.stats["ephemeral_ignored"] += 1
            return "ephemeral_ignored"

        event_id = validated.event["id"]
        if not replay and not self._remember(event_id):
            self.stats["duplicates"] += 1
            return "duplicate"

        # Gate A: exact identity community/relay plus exact channel UUID.
        channel = self._registry.get_buzz_inbound_channel(
            self.agent_name,
            self.community_id,
            self.relay_url,
            validated.channel_id,
        )
        if channel is None:
            self.stats["unlisted_channel"] += 1
            return "unlisted_channel"

        # Gate B: full, platform-qualified, community-scoped verified author.
        author = self._registry.get_buzz_inbound_principal(
            self.agent_name,
            self.community_id,
            validated.event["pubkey"],
        )
        if author is None:
            self.stats["unapproved_principal"] += 1
            return "unapproved_principal"

        self._registry.note_buzz_inbound_principal_seen(
            self.agent_name,
            self.community_id,
            author["pubkey"],
            float(validated.event["created_at"]),
        )

        if not self._registry.begin_buzz_inbound_event_delivery(
            self.agent_name,
            validated.event,
            community_id=self.community_id,
            channel_id=validated.channel_id,
            replay=replay,
        ):
            self.stats["duplicates"] += 1
            return "duplicate"

        principal = author["principal"]
        display_name = author["display_name"] or f"Buzz user {author['pubkey'][:16]}"
        message = BrokerMessage(
            platform="buzz",
            chat_id=validated.channel_id,
            sender_name=display_name,
            sender_id=principal,
            content=validated.event["content"],
            agent_name=self.agent_name,
            timestamp=float(validated.event["created_at"]),
            message_id=event_id,
            chat_title=channel["label"],
            is_group=True,
            reply_to=validated.reply_to,
            metadata={
                "buzz_verified_event": {
                    "verified": True,
                    "event_id": event_id,
                    "kind": 9,
                    "author_pubkey": author["pubkey"],
                    "channel_id": validated.channel_id,
                },
                "buzz_verified_principal": principal,
                "buzz_display_name_untrusted": True,
                "buzz_mentioned_self": validated.mentioned_self,
            },
        )
        try:
            accepted = await self._broker.dispatch_pre_authorized(self.agent_name, message)
            if accepted is False:
                raise BuzzInboundDeliveryError("persona_handoff_unavailable")
        except Exception as exc:
            self._registry.mark_buzz_inbound_event_retry(
                self.agent_name,
                event_id,
                type(exc).__name__,
            )
            if isinstance(exc, BuzzInboundDeliveryError):
                raise
            raise BuzzInboundDeliveryError("persona_handoff_failed") from exc

        self._registry.mark_buzz_inbound_event_delivered(self.agent_name, event_id)
        self._registry.update_buzz_inbound_health(
            self.agent_name,
            status="connected",
            event_at=self._clock(),
        )
        self.stats["delivered"] += 1
        return "delivered"

    async def replay_pending(self) -> int:
        delivered = 0
        for event in self._registry.list_pending_buzz_inbound_events(self.agent_name):
            if await self.process_event(event, replay=True) == "delivered":
                delivered += 1
        return delivered


class BrokerBuzzPoller:
    """Supervised NIP-42 relay subscription for one encrypted Buzz identity."""

    def __init__(
        self,
        signing_material,
        broker,
        registry,
        owner_notify,
        *,
        heartbeat_interval: float = 60.0,
        liveness_timeout: float = 15.0,
        auth_timeout: float = 15.0,
        reconnect_base: float = 1.0,
        reconnect_max: float = 30.0,
        connect_factory: Callable[..., Any] = websockets.connect,
    ) -> None:
        self._agent_name = signing_material.agent
        self._pubkey = signing_material.pubkey
        self._relay_url = signing_material.relay_url
        self._community_id = signing_material.community_id
        self._signer = BuzzNostrSigner(signing_material.private_key)
        if not secrets.compare_digest(self._signer.pubkey, self._pubkey):
            raise BuzzRelayProtocolError("signing material pubkey mismatch")
        self._broker = broker
        self._registry = registry
        self._owner_notify = owner_notify
        self._heartbeat_interval = max(0.05, float(heartbeat_interval))
        self._liveness_timeout = max(0.05, float(liveness_timeout))
        self._auth_timeout = max(0.05, float(auth_timeout))
        self._reconnect_base = max(0.01, float(reconnect_base))
        self._reconnect_max = max(self._reconnect_base, float(reconnect_max))
        self._connect_factory = connect_factory
        self._running = False
        self._ws = None
        self._poll_count = 0
        self._consecutive_failures = 0
        self._outage_notified = False
        self._status = "stopped"
        self._last_error = ""
        self._last_liveness_at = 0.0
        self._last_event_at = 0.0
        self._processor = BuzzInboundProcessor(
            registry=registry,
            broker=broker,
            agent_name=self._agent_name,
            identity_pubkey=self._pubkey,
            community_id=self._community_id,
            relay_url=self._relay_url,
        )

    def __repr__(self) -> str:
        return (
            f"BrokerBuzzPoller(agent={self._agent_name!r}, pubkey={self._pubkey!r}, "
            f"community_id={self._community_id!r}, private_key=<redacted>)"
        )

    @property
    def agent_name(self) -> str:
        return self._agent_name

    @property
    def poll_count(self) -> int:
        return self._poll_count

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def health(self) -> dict:
        return {
            "platform": "buzz",
            "agent": self._agent_name,
            "status": self._status,
            "running": self._running,
            "last_error": self._last_error,
            "last_liveness_at": self._last_liveness_at,
            "last_event_at": self._last_event_at,
            "consecutive_failures": self._consecutive_failures,
            "polls": self._poll_count,
            **self._processor.stats,
        }

    async def start(self) -> None:
        self._running = True
        self._status = "starting"
        delay = self._reconnect_base
        while self._running:
            try:
                await self._run_connection()
                if self._running:
                    raise BuzzRelayProtocolError("relay_connection_ended")
            except asyncio.CancelledError:
                self._status = "stopped"
                self._running = False
                raise
            except Exception as exc:  # noqa: BLE001 — supervisor owns reconnect policy
                if not self._running:
                    break
                self._consecutive_failures += 1
                if self._consecutive_failures == 1:
                    # A completed authenticated/liveness handshake resets the
                    # counter to zero, so the next unrelated outage starts at
                    # the base delay instead of inheriting an old backoff.
                    delay = self._reconnect_base
                self._status = "reconnecting"
                self._last_error = type(exc).__name__
                self._registry.update_buzz_inbound_health(
                    self._agent_name,
                    status="reconnecting",
                    last_error=self._last_error,
                )
                measured_dead = isinstance(exc, BuzzRelayLivenessError)
                if measured_dead or self._consecutive_failures >= 3:
                    await self._notify_outage_once(measured_dead=measured_dead)
                await asyncio.sleep(delay)
                delay = min(delay * 2, self._reconnect_max)
            else:  # pragma: no cover - _run_connection runs until stop/error
                delay = self._reconnect_base
        self._status = "stopped"
        self._registry.update_buzz_inbound_health(
            self._agent_name,
            status="stopped",
            last_error=self._last_error,
        )

    def stop(self) -> None:
        self._running = False
        self._status = "stopping"
        if self._ws is not None:
            try:
                asyncio.get_running_loop().create_task(self._ws.close())
            except RuntimeError:
                # No running loop means an asynchronous close cannot be scheduled.
                pass

    async def _run_connection(self) -> None:
        policy = self._registry.get_buzz_inbound_policy(self._agent_name)
        if not policy or not policy["channels"] or not policy["principals"]:
            raise BuzzRelayProtocolError("inbound_policy_default_deny")
        if policy["community_id"] != self._community_id or policy["relay_url"] != self._relay_url:
            raise BuzzRelayProtocolError("inbound_policy_identity_scope_mismatch")
        channels = [item["channel_id"] for item in policy["channels"]]
        connect = self._connect_factory(
            self._relay_url,
            open_timeout=self._auth_timeout,
            close_timeout=3,
            ping_interval=min(self._heartbeat_interval, 20.0),
            ping_timeout=self._liveness_timeout,
            max_size=_MAX_FRAME_BYTES,
            additional_headers={"User-Agent": "PinkyBot-Buzz/1.0"},
        )
        try:
            async with connect as ws:
                self._ws = ws
                await self._run_authenticated_subscription(ws, channels)
        finally:
            self._ws = None

    async def _run_authenticated_subscription(self, ws, channels: list[str]) -> None:
        challenge_frame = await self._receive_frame(ws, timeout=self._auth_timeout)
        if (
            len(challenge_frame) != 2
            or challenge_frame[0] != "AUTH"
            or not isinstance(challenge_frame[1], str)
        ):
            raise BuzzRelayProtocolError("relay_missing_auth_challenge")
        auth_event = self._signer.sign_relay_auth(
            self._relay_url,
            challenge_frame[1],
        )
        await self._send_frame(ws, ["AUTH", auth_event])
        auth_result = await self._receive_frame(ws, timeout=self._auth_timeout)
        if (
            len(auth_result) < 3
            or auth_result[0] != "OK"
            or auth_result[1] != auth_event["id"]
            or auth_result[2] is not True
        ):
            raise BuzzRelayProtocolError("relay_auth_refused")

        await self._processor.replay_pending()
        main_sub = f"pinky-{self._agent_name}-{secrets.token_hex(8)}"
        since = self._registry.get_buzz_subscription_since(self._agent_name)
        await self._send_frame(
            ws,
            [
                "REQ",
                main_sub,
                # The production Buzz relay does not live-fanout events to a
                # subscription whose filter carries ``since``.  Keep the wire
                # subscription open-ended and enforce this exact floor in the
                # client before any cache, authorization gate, or durable write.
                {"kinds": [9, 20002], "#h": channels},
            ],
        )
        await self._wait_for_eose(
            ws,
            subscription_id=main_sub,
            timeout=self._liveness_timeout,
            subscription_since={main_sub: since},
        )
        now = time.time()
        self._status = "connected"
        self._last_error = ""
        self._last_liveness_at = now
        self._consecutive_failures = 0
        self._outage_notified = False
        self._registry.update_buzz_inbound_health(
            self._agent_name,
            status="connected",
            connected_at=now,
            liveness_at=now,
        )
        await self._check_owner_silence()

        loop = asyncio.get_running_loop()
        next_heartbeat = loop.time() + self._heartbeat_interval
        while self._running:
            remaining = next_heartbeat - loop.time()
            if remaining <= 0:
                await self._active_liveness_probe(
                    ws,
                    channels,
                    main_subscription=main_sub,
                    main_since=since,
                )
                next_heartbeat = loop.time() + self._heartbeat_interval
                await self._processor.replay_pending()
                await self._check_owner_silence()
                continue
            try:
                frame = await self._receive_frame(ws, timeout=remaining)
            except asyncio.TimeoutError:
                continue
            await self._handle_frame(
                ws,
                frame,
                subscription_since={main_sub: since},
            )

    async def _active_liveness_probe(
        self,
        ws,
        channels: list[str],
        *,
        main_subscription: str,
        main_since: int,
    ) -> None:
        heartbeat_sub = f"pinky-live-{secrets.token_hex(8)}"
        heartbeat_since = int(time.time())
        await self._send_frame(
            ws,
            [
                "REQ",
                heartbeat_sub,
                {
                    "kinds": [9, 20002],
                    "#h": channels,
                    "since": heartbeat_since,
                    "limit": 1,
                },
            ],
        )
        try:
            await self._wait_for_eose(
                ws,
                subscription_id=heartbeat_sub,
                timeout=self._liveness_timeout,
                subscription_since={
                    main_subscription: main_since,
                    heartbeat_sub: heartbeat_since,
                },
            )
        except asyncio.TimeoutError as exc:
            raise BuzzRelayLivenessError("relay_eose_heartbeat_timed_out") from exc
        finally:
            try:
                await self._send_frame(ws, ["CLOSE", heartbeat_sub])
            except Exception:
                # Best-effort CLOSE must not hide the measured liveness result.
                pass
        now = time.time()
        self._last_liveness_at = now
        self._registry.update_buzz_inbound_health(
            self._agent_name,
            status="connected",
            liveness_at=now,
        )

    async def _wait_for_eose(
        self,
        ws,
        *,
        subscription_id: str,
        timeout: float,
        subscription_since: dict[str, int],
    ) -> None:
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise asyncio.TimeoutError
            frame = await self._receive_frame(ws, timeout=remaining)
            if len(frame) >= 2 and frame[0] == "EOSE" and frame[1] == subscription_id:
                return
            await self._handle_frame(
                ws,
                frame,
                subscription_since=subscription_since,
            )

    async def _handle_frame(
        self,
        ws,
        frame: list,
        *,
        subscription_since: dict[str, int] | None,
    ) -> None:
        if not frame:
            raise BuzzRelayProtocolError("relay_frame_empty")
        kind = frame[0]
        if kind == "EVENT":
            if len(frame) != 3 or not isinstance(frame[1], str):
                raise BuzzRelayProtocolError("relay_event_frame_invalid")
            if subscription_since is not None and frame[1] not in subscription_since:
                raise BuzzRelayProtocolError("relay_event_subscription_mismatch")
            result = await self._processor.process_event(
                frame[2],
                subscription_since=(
                    subscription_since[frame[1]] if subscription_since is not None else None
                ),
            )
            if result == "delivered":
                self._last_event_at = time.time()
            return
        if kind == "AUTH" and len(frame) == 2 and isinstance(frame[1], str):
            auth_event = self._signer.sign_relay_auth(self._relay_url, frame[1])
            await self._send_frame(ws, ["AUTH", auth_event])
            return
        if kind == "CLOSED" and (
            subscription_since is None or (len(frame) >= 2 and frame[1] in subscription_since)
        ):
            raise BuzzRelayProtocolError("relay_subscription_closed")
        if kind == "NOTICE":
            raise BuzzRelayProtocolError("relay_notice")
        # OK/EOSE for another request are control frames, not inbound content.

    async def _receive_frame(self, ws, *, timeout: float) -> list:
        raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
        self._poll_count += 1
        if not isinstance(raw, str) or len(raw.encode("utf-8")) > _MAX_FRAME_BYTES:
            raise BuzzRelayProtocolError("relay_frame_bounds_invalid")
        try:
            frame = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise BuzzRelayProtocolError("relay_frame_not_json") from exc
        if not isinstance(frame, list) or not frame or not isinstance(frame[0], str):
            raise BuzzRelayProtocolError("relay_frame_shape_invalid")
        return frame

    @staticmethod
    async def _send_frame(ws, frame: list) -> None:
        await ws.send(json.dumps(frame, separators=(",", ":"), ensure_ascii=False))

    async def _check_owner_silence(self) -> None:
        due = self._registry.buzz_owner_silence_alert_due(self._agent_name)
        if not due:
            return
        message = (
            f"BUZZ OWNER KEY SILENCE for {self._agent_name}: the configured owner "
            f"principal {due['owner_principal']} has not been seen for at least "
            f"{due['days']} days. If the owner rotated keys, use the documented "
            "owner-control rotation procedure; never approve a first-message claim."
        )
        if await self._notify(message):
            self._registry.mark_buzz_owner_silence_notified(self._agent_name)

    async def _notify_outage_once(self, *, measured_dead: bool) -> None:
        if self._outage_notified:
            return
        reason = "active EOSE liveness failed" if measured_dead else "repeated connection failures"
        message = (
            f"BUZZ INBOUND UNHEALTHY for {self._agent_name}: {reason}; "
            f"the daemon is reconnecting and no unverified event is routed. "
            f"error_type={self._last_error}"
        )
        if await self._notify(message):
            self._outage_notified = True

    async def _notify(self, message: str) -> bool:
        try:
            result = self._owner_notify(self._agent_name, message)
            return bool(await result) if inspect.isawaitable(result) else bool(result)
        except Exception as exc:  # noqa: BLE001 — supervision must survive alert failure
            _log(f"buzz-inbound: owner-notify failed for {self._agent_name} ({type(exc).__name__})")
            return False


__all__ = [
    "BrokerBuzzPoller",
    "BuzzInboundDeliveryError",
    "BuzzInboundProcessor",
    "BuzzInboundRejectedError",
    "BuzzRelayLivenessError",
    "BuzzRelayProtocolError",
    "ValidatedBuzzInboundEvent",
    "validate_buzz_inbound_event",
]
