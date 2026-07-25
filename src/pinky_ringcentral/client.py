"""Thin asynchronous client for RingCentral's JWT-authenticated SMS APIs."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping
from typing import Any

import httpx

TOKEN_PATH = "/restapi/oauth/token"
WS_TOKEN_PATH = "/restapi/oauth/wstoken"
API_BASE = "/restapi/v1.0"
SMS_PATH = f"{API_BASE}/account/~/extension/~/sms"
MESSAGE_STORE_PATH = f"{API_BASE}/account/~/extension/~/message-store"

_EXPIRY_SAFETY_MARGIN_SECONDS = 60
_MIN_CACHE_LIFETIME_SECONDS = 1


class RingCentralAPIError(Exception):
    """Safe-to-surface RingCentral or transport error."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int = 0,
        may_have_completed: bool = False,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.may_have_completed = may_have_completed


class RingCentralClient:
    """JWT-bearer OAuth client with in-memory token caching and one 401 retry."""

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        server: str,
        jwt: str,
        *,
        http: httpx.AsyncClient | None = None,
        clock: Any = time.monotonic,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._server = server.rstrip("/")
        self._jwt = jwt
        self._access_token = ""
        self._expires_at = 0.0
        self._clock = clock
        self._token_lock = asyncio.Lock()
        self._http = http or httpx.AsyncClient(timeout=30.0)
        self._owns_http = http is None

    @property
    def configured(self) -> bool:
        return bool(
            self._client_id and self._client_secret and self._server and self._jwt
        )

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def get_access_token(self, *, force: bool = False) -> str:
        """Return a cached token or run the JWT grant exactly once concurrently."""

        if not force and self._token_valid():
            return self._access_token
        async with self._token_lock:
            if not force and self._token_valid():
                return self._access_token
            if not self.configured:
                raise RingCentralAPIError("RingCentral credentials are not configured")
            try:
                response = await self._http.post(
                    f"{self._server}{TOKEN_PATH}",
                    data={
                        "grant_type": (
                            "urn:ietf:params:oauth:grant-type:jwt-bearer"
                        ),
                        "assertion": self._jwt,
                    },
                    auth=(self._client_id, self._client_secret),
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
            except httpx.HTTPError as exc:
                raise RingCentralAPIError(
                    f"RingCentral authentication transport failed: {type(exc).__name__}"
                ) from exc
            if response.status_code != 200:
                raise RingCentralAPIError(
                    self._error_message("RingCentral authentication failed", response),
                    status_code=response.status_code,
                )
            try:
                payload = response.json()
                token = str(payload["access_token"])
                expires_in = max(float(payload.get("expires_in", 3600)), 0.0)
            except (KeyError, TypeError, ValueError) as exc:
                raise RingCentralAPIError(
                    "RingCentral authentication response was malformed"
                ) from exc
            if not token:
                raise RingCentralAPIError(
                    "RingCentral authentication response omitted access_token"
                )
            cache_lifetime = max(
                expires_in - _EXPIRY_SAFETY_MARGIN_SECONDS,
                _MIN_CACHE_LIFETIME_SECONDS,
            )
            self._access_token = token
            self._expires_at = self._clock() + cache_lifetime
            return token

    def _token_valid(self) -> bool:
        return bool(self._access_token and self._clock() < self._expires_at)

    async def _request(
        self, method: str, path_or_url: str, **kwargs: Any
    ) -> httpx.Response:
        url = (
            path_or_url
            if path_or_url.startswith(("https://", "http://"))
            else f"{self._server}{path_or_url}"
        )
        token = await self.get_access_token()
        headers = dict(kwargs.pop("headers", {}) or {})
        headers["Authorization"] = f"Bearer {token}"
        response = await self._send(method, url, headers=headers, **kwargs)
        if response.status_code == 401:
            token = await self.get_access_token(force=True)
            headers["Authorization"] = f"Bearer {token}"
            response = await self._send(method, url, headers=headers, **kwargs)
        return response

    async def _send(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        try:
            return await self._http.request(method, url, **kwargs)
        except httpx.HTTPError as exc:
            raise RingCentralAPIError(
                f"RingCentral request transport failed: {type(exc).__name__}",
                may_have_completed=method.upper()
                not in {"GET", "HEAD", "OPTIONS"},
            ) from exc

    async def send_sms(
        self, *, from_phone: str, to_phone: str, text: str
    ) -> dict[str, Any]:
        response = await self._request(
            "POST",
            SMS_PATH,
            json={
                "from": {"phoneNumber": from_phone},
                "to": [{"phoneNumber": to_phone}],
                "text": text,
            },
        )
        return self._json_or_raise("SMS transmission failed", response)

    async def get_message(self, message_id: str) -> dict[str, Any]:
        response = await self._request(
            "GET", f"{MESSAGE_STORE_PATH}/{message_id}"
        )
        return self._json_or_raise("SMS status lookup failed", response)

    async def list_messages(
        self,
        *,
        phone_number: str = "",
        date_from: str = "",
        date_to: str = "",
        direction: str = "",
        page: int = 1,
        per_page: int = 100,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "messageType": "SMS",
            "availability": "Alive",
            "page": page,
            "perPage": per_page,
        }
        if phone_number:
            params["phoneNumber"] = phone_number
        if date_from:
            params["dateFrom"] = date_from
        if date_to:
            params["dateTo"] = date_to
        if direction:
            params["direction"] = direction
        response = await self._request("GET", MESSAGE_STORE_PATH, params=params)
        return self._json_or_raise("Message-store query failed", response)

    async def get_websocket_token(self) -> dict[str, Any]:
        response = await self._request("POST", WS_TOKEN_PATH)
        return self._json_or_raise("WebSocket token request failed", response)

    def _json_or_raise(
        self, context: str, response: httpx.Response
    ) -> dict[str, Any]:
        if 200 <= response.status_code < 300:
            try:
                payload = response.json()
            except ValueError as exc:
                raise RingCentralAPIError(
                    f"{context}: RingCentral returned malformed JSON",
                    status_code=response.status_code,
                ) from exc
            if not isinstance(payload, dict):
                raise RingCentralAPIError(
                    f"{context}: RingCentral returned an unexpected payload",
                    status_code=response.status_code,
                )
            return payload
        raise RingCentralAPIError(
            self._error_message(context, response),
            status_code=response.status_code,
        )

    def _error_message(self, context: str, response: httpx.Response) -> str:
        """Extract bounded diagnostics and redact every credential/token."""

        details: list[str] = []
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        if isinstance(payload, Mapping):
            for key in ("error", "errorCode", "message", "description"):
                value = payload.get(key)
                if value is not None:
                    details.append(f"{key}={value}")
        detail = "; ".join(details)[:500] or "no safe error detail"
        for secret in (
            self._client_id,
            self._client_secret,
            self._jwt,
            self._access_token,
        ):
            if secret:
                detail = detail.replace(secret, "[redacted]")
        return f"{context} ({response.status_code}): {detail}"
