"""Outbound-only WebSocket subscription with message-store polling fallback."""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from websockets.asyncio.client import connect

from pinky_ringcentral.client import MESSAGE_STORE_PATH, RingCentralAPIError
from pinky_ringcentral.service import SENDER_PHONE, SmsService

INBOUND_FILTER = f"{MESSAGE_STORE_PATH}/instant?type=SMS"
OUTBOUND_FILTER = f"{MESSAGE_STORE_PATH}?type=SMS&direction=Outbound"
POLL_CURSOR_KEY = "ringcentral_inbound_poll_cursor"
_TOKEN_QUERY_RE = re.compile(
    r"(?i)((?:access_token|wsc)=)[^&\s]+"
)


def _parse_time(value: str, *, default: datetime) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return default
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _safe_error(exc: Exception) -> str:
    value = _TOKEN_QUERY_RE.sub(r"\1[redacted]", str(exc))
    return f"{type(exc).__name__}: {value[:300]}"


def _websocket_uri(uri: str, access_token: str) -> str:
    if not uri.startswith("wss://") or not access_token:
        raise RingCentralAPIError(
            "WebSocket token response was missing connection data"
        )
    parsed = urlsplit(uri)
    query = parse_qsl(parsed.query, keep_blank_values=True)
    query = [(key, value) for key, value in query if key != "access_token"]
    query.append(("access_token", access_token))
    return urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment)
    )


class RingCentralInboundWorker:
    """Maintain a WS subscription and poll during every disconnected period."""

    def __init__(
        self,
        service: SmsService,
        *,
        poll_interval: float = 30.0,
        housekeeping_interval: float = 30.0,
        websocket_retry_interval: float = 30.0,
        websocket_connect: Callable[..., Awaitable[Any]] = connect,
        now: Any = lambda: datetime.now(UTC),
    ) -> None:
        self.service = service
        self.poll_interval = poll_interval
        self.housekeeping_interval = housekeeping_interval
        self.websocket_retry_interval = websocket_retry_interval
        self._connect = websocket_connect
        self._now = now
        self._stop = asyncio.Event()
        self.mode = "starting"
        self.last_error = ""

    def stop(self) -> None:
        self._stop.set()

    async def run_housekeeping_forever(self) -> None:
        """Retry durable wakes/statuses independently of WS connection state."""

        consecutive_pending = 0
        while not self._stop.is_set():
            delay = min(
                max(self.housekeeping_interval, 0.01)
                * (2**consecutive_pending),
                300.0,
            )
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
                return
            except TimeoutError:
                pass
            try:
                delivered = await self.service.deliver_pending_inbound()
                await self.service.refresh_pending_statuses()
                pending = bool(
                    self.service.store.pending_inbound(limit=1)
                )
                consecutive_pending = (
                    min(consecutive_pending + 1, 4)
                    if pending and delivered == 0
                    else 0
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                consecutive_pending = min(consecutive_pending + 1, 4)
                self.last_error = _safe_error(exc)

    async def run_forever(self) -> None:
        while not self._stop.is_set():
            try:
                await self._run_websocket()
                self.last_error = ""
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Never include frame bodies, OAuth tokens, or WS URIs here.
                self.last_error = _safe_error(exc)
            if self._stop.is_set():
                return
            self.mode = "polling"
            deadline = asyncio.get_running_loop().time() + max(
                self.websocket_retry_interval, self.poll_interval
            )
            while (
                not self._stop.is_set()
                and asyncio.get_running_loop().time() < deadline
            ):
                try:
                    await self.poll_once()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self.last_error = _safe_error(exc)
                try:
                    await asyncio.wait_for(
                        self._stop.wait(), timeout=self.poll_interval
                    )
                except TimeoutError:
                    pass

    async def poll_once(self) -> int:
        """Stage one snapshot-bounded scan, then advance its durable watermark."""

        scan_start = self._now().astimezone(UTC)
        stored = self.service.store.get_metadata(POLL_CURSOR_KEY)
        cursor = _parse_time(
            stored,
            default=scan_start - timedelta(minutes=5),
        )
        # Overlap closes timestamp/order races; the inbound table deduplicates.
        date_from = (cursor - timedelta(minutes=2)).isoformat()
        date_to = scan_start.isoformat()
        page = 1
        messages: list[dict[str, Any]] = []
        while True:
            payload = await self.service.client.list_messages(
                phone_number=SENDER_PHONE,
                date_from=date_from,
                date_to=date_to,
                direction="Inbound",
                page=page,
                per_page=100,
            )
            records = payload.get("records")
            if not isinstance(records, list):
                raise RingCentralAPIError(
                    "Inbound polling returned malformed records"
                )
            messages.extend(item for item in records if isinstance(item, dict))
            paging = payload.get("paging")
            total_pages = (
                int(paging.get("totalPages") or page)
                if isinstance(paging, dict)
                else page
            )
            if not records or page >= total_pages:
                break
            page += 1

        messages.sort(key=lambda item: str(item.get("creationTime") or ""))
        handled = 0
        for message in messages:
            await self.service.handle_inbound(message)
            handled += 1
        await self.service.deliver_pending_inbound()
        await self.service.refresh_pending_statuses()
        self.service.store.set_metadata(
            POLL_CURSOR_KEY,
            scan_start.isoformat().replace("+00:00", "Z"),
        )
        return handled

    async def _run_websocket(self) -> None:
        token = await self.service.client.get_websocket_token()
        uri = str(token.get("uri") or "")
        access_token = str(token.get("ws_access_token") or "")
        connection_uri = _websocket_uri(uri, access_token)

        async with self._connect(
            connection_uri,
            open_timeout=15,
            close_timeout=5,
            max_size=4 * 1024 * 1024,
            ping_interval=20,
            ping_timeout=20,
        ) as websocket:
            self.mode = "websocket"
            connection_frame = await asyncio.wait_for(
                websocket.recv(), timeout=15
            )
            if not self._has_connection_details(connection_frame):
                raise RingCentralAPIError(
                    "WebSocket omitted ConnectionDetails"
                )
            await websocket.send(json.dumps(self._subscription_request()))
            subscription_frame = await asyncio.wait_for(
                websocket.recv(), timeout=15
            )
            if not self._subscription_accepted(subscription_frame):
                raise RingCentralAPIError(
                    "WebSocket subscription was rejected"
                )
            # The accepted subscription buffers new traffic while this
            # overlapping snapshot catches the pre-subscription gap.
            await self.poll_once()
            while not self._stop.is_set():
                frame = await websocket.recv()
                await self._handle_frame(websocket, frame)

    async def _handle_frame(self, websocket: Any, frame: Any) -> None:
        try:
            items = json.loads(frame)
        except (TypeError, json.JSONDecodeError) as exc:
            raise RingCentralAPIError(
                "WebSocket delivered malformed JSON"
            ) from exc
        if not isinstance(items, list):
            raise RingCentralAPIError(
                "WebSocket delivered an unexpected frame"
            )
        metadata = items[0] if items and isinstance(items[0], dict) else {}
        if metadata.get("type") == "Heartbeat":
            # RingCentral Heartbeat frames are responses to client requests.
            # Liveness uses native WebSocket ping/pong, handled by websockets.
            return
        if metadata.get("type") != "ServerNotification":
            return
        notification = (
            items[1] if len(items) > 1 and isinstance(items[1], dict) else {}
        )
        event = str(notification.get("event") or "")
        body = notification.get("body")
        if "/instant" in event and isinstance(body, dict):
            await self.service.handle_inbound(body)
        elif "message-store" in event:
            await self.service.refresh_pending_statuses()

    @staticmethod
    def _has_connection_details(frame: Any) -> bool:
        try:
            items = json.loads(frame)
        except (TypeError, json.JSONDecodeError):
            return False
        return isinstance(items, list) and any(
            isinstance(item, dict) and item.get("type") == "ConnectionDetails"
            for item in items
        )

    @staticmethod
    def _subscription_accepted(frame: Any) -> bool:
        try:
            items = json.loads(frame)
        except (TypeError, json.JSONDecodeError):
            return False
        if not isinstance(items, list) or not items:
            return False
        metadata = items[0] if isinstance(items[0], dict) else {}
        try:
            status = int(metadata.get("status") or 0)
        except (TypeError, ValueError):
            return False
        return metadata.get("type") == "ClientResponse" and 200 <= status < 300

    @staticmethod
    def _subscription_request() -> list[dict[str, Any]]:
        return [
            {
                "type": "ClientRequest",
                "messageId": str(uuid.uuid4()),
                "method": "POST",
                "path": "/restapi/v1.0/subscription/",
            },
            {
                "eventFilters": [INBOUND_FILTER, OUTBOUND_FILTER],
                "deliveryMode": {"transportType": "WebSocket"},
            },
        ]
