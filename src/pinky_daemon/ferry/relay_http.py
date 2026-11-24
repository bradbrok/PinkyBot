"""One-origin, undecoded, bounded relay responses with absolute deadlines."""

from __future__ import annotations

import asyncio
from urllib.parse import urlsplit

import aiohttp

from pinky_relay.config import parse_json

RESPONSE_CAP = 2 * 1024 * 1024
SEND_DEADLINE = 30
ACK_DEADLINE = 15
CONNECT_DEADLINE = 10
SHUTDOWN_DEADLINE = 5


class RelayClientError(RuntimeError):
    pass


def _origin(url):
    parsed = urlsplit(str(url))
    return parsed.scheme, parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80)


class RelayHTTPClient:
    def __init__(self, settings):
        self.settings = settings
        self.session = None

    async def __aenter__(self):
        self.session = aiohttp.ClientSession(auto_decompress=False, trust_env=False)
        return self

    async def __aexit__(self, *args):
        if self.session is not None:
            async with asyncio.timeout(SHUTDOWN_DEADLINE):
                await self.session.close()

    async def _request(self, method, route, *, deadline, body=None, params=None):
        if route not in ("v1/send", "v1/poll", "v1/ack"):
            raise RelayClientError("invalid relay route")
        if self.session is None:
            raise RelayClientError("relay client is not open")
        headers = {"Authorization": "Bearer " + self.settings.token,
                   "Accept-Encoding": "identity"}
        url = self.settings.url.rstrip("/") + "/" + route
        try:
            async with asyncio.timeout(deadline):
                timeout = aiohttp.ClientTimeout(total=deadline, connect=CONNECT_DEADLINE)
                async with self.session.request(method, url, headers=headers, json=body,
                                                params=params, allow_redirects=False,
                                                timeout=timeout) as response:
                    if _origin(response.url) != _origin(self.settings.url):
                        raise RelayClientError("relay origin changed")
                    if 300 <= response.status < 400:
                        raise RelayClientError("relay redirects are refused")
                    if response.status != 200:
                        raise RelayClientError(f"relay HTTP {int(response.status)}")
                    encoding = response.headers.get("Content-Encoding", "identity").strip().lower()
                    if encoding != "identity":
                        raise RelayClientError("relay compression is refused")
                    raw = bytearray()
                    async for part in response.content.iter_chunked(64 * 1024):
                        if len(raw) + len(part) > RESPONSE_CAP:
                            raise RelayClientError("relay response exceeds size limit")
                        raw.extend(part)
                    result = parse_json(bytes(raw), depth=20)
                    if not isinstance(result, dict):
                        raise RelayClientError("relay response object required")
                    return result
        except RelayClientError:
            raise
        except (aiohttp.ClientError, OSError, TimeoutError, ValueError, TypeError) as exc:
            raise RelayClientError(f"relay request failed ({type(exc).__name__})") from None

    async def send(self, target, blob):
        import base64

        result = await self._request("POST", "v1/send", deadline=SEND_DEADLINE,
                                     body={"to_fleet": target,
                                           "blob_b64": base64.b64encode(blob).decode("ascii")})
        ident = result.get("id")
        if not isinstance(ident, str) or not ident or len(ident) > 64 or not ident.isascii():
            raise RelayClientError("invalid relay row id")
        return ident

    async def poll(self, wait=25):
        if type(wait) is not int or not 0 <= wait <= 25:
            raise RelayClientError("invalid relay poll wait")
        result = await self._request("GET", "v1/poll", deadline=wait + 15,
                                     params={"wait": wait})
        rows = result.get("rows")
        if not isinstance(rows, list) or len(rows) > 20:
            raise RelayClientError("invalid relay page")
        return rows

    async def ack(self, ids):
        result = await self._request("POST", "v1/ack", deadline=ACK_DEADLINE,
                                     body={"ids": ids})
        count = result.get("deleted")
        if type(count) is not int or not 0 <= count <= len(ids):
            raise RelayClientError("invalid relay acknowledgement")
        return count
