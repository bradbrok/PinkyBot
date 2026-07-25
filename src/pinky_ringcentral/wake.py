"""Durable-event sink onto the Mini daemon's audited ferry wake rail."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlsplit

import httpx

from pinky_daemon.auth import build_internal_auth_headers


class WakeDeliveryError(Exception):
    """The daemon did not accept or publish a wake event."""


class DaemonFerryWakeSink:
    """Publish SMS events through the daemon, which ferries them to Geordi."""

    def __init__(
        self,
        *,
        api_url: str,
        session_secret: str,
        source_agent: str = "barsik",
        target: str = "geordi@pi",
        wake_url: str = "",
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self._source_agent = source_agent
        self._target = target
        self._session_secret = session_secret
        self._url = wake_url or (
            f"{api_url.rstrip('/')}/agents/{source_agent}/mesh/send"
        )
        self._path = urlsplit(self._url).path
        self._http = http or httpx.AsyncClient(timeout=15.0)
        self._owns_http = http is None

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def send_event(self, kind: str, payload: dict[str, Any]) -> None:
        if not self._session_secret:
            raise WakeDeliveryError("PINKY_SESSION_SECRET is not configured")
        event_json = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        body = {
            "text": (
                f"[RingCentral event: {kind}]\n"
                f"Full event payload:\n{event_json}"
            ),
            "event_type": kind,
            "event": payload,
        }
        headers = build_internal_auth_headers(
            self._session_secret,
            agent_name=self._source_agent,
            method="POST",
            path=self._path,
        )
        try:
            response = await self._http.post(
                self._url,
                json={
                    "target": self._target,
                    "body": body,
                    # A normal ferry message is the first-class wake path.
                    "kind": "msg",
                    "priority": "high",
                },
                headers=headers,
            )
        except httpx.HTTPError as exc:
            raise WakeDeliveryError(
                f"daemon wake transport failed: {type(exc).__name__}"
            ) from exc
        if response.status_code != 200:
            raise WakeDeliveryError(
                f"daemon wake rejected with HTTP {response.status_code}"
            )
        try:
            result = response.json()
        except ValueError as exc:
            raise WakeDeliveryError("daemon wake returned malformed JSON") from exc
        if not isinstance(result, dict) or not result.get("sent"):
            error = (
                str(result.get("error") or "publish was not confirmed")
                if isinstance(result, dict)
                else "unexpected response"
            )
            raise WakeDeliveryError(f"daemon ferry wake failed: {error[:300]}")


class MemoryWakeSink:
    """Test sink that records events without external effects."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    async def send_event(self, kind: str, payload: dict[str, Any]) -> None:
        self.events.append((kind, payload))
