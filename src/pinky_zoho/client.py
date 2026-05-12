"""HTTP client for the HMAC-authenticated Zoho API server."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from typing import Any

FALLBACK_API_URLS = ("http://10.0.0.32:9100", "http://10.0.0.209:9100")


def resolve_secret() -> str:
    """Return the Zoho API secret from the supported client-side env vars."""
    return os.environ.get("ZOHO_API_SECRET") or os.environ.get("PINKY_SESSION_SECRET", "")


def normalize_path(path: str) -> str:
    """Return the path portion used by the Zoho server's HMAC verifier."""
    return path.split("?", 1)[0]


def sign_headers(
    secret: str,
    agent: str,
    method: str,
    path: str,
    *,
    timestamp: int | None = None,
) -> dict[str, str]:
    """Build signed request headers for the Zoho API server."""
    ts = int(time.time()) if timestamp is None else int(timestamp)
    payload = f"{agent}\n{method.upper()}\n{normalize_path(path)}\n{ts}".encode()
    signature = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    return {
        "x-pinky-agent": agent,
        "x-pinky-timestamp": str(ts),
        "x-pinky-signature": signature,
    }


def _coerce_param(value: Any) -> Any:
    if isinstance(value, bool):
        return "true" if value else "false"
    return value


def _clean_params(params: Mapping[str, Any] | None) -> dict[str, Any]:
    if not params:
        return {}
    return {
        key: _coerce_param(value)
        for key, value in params.items()
        if value is not None and value != ""
    }


class ZohoClient:
    """Small resilient client for the local Zoho API server."""

    def __init__(
        self,
        *,
        agent_name: str,
        api_url: str = "",
        secret: str = "",
        timeout: int = 30,
    ) -> None:
        if not agent_name:
            raise ValueError("agent_name is required")
        self.agent_name = agent_name
        self.secret = secret or resolve_secret()
        self.timeout = timeout
        self._api_urls = self._resolve_api_urls(api_url)
        self._active_api_url: str | None = None

    @property
    def active_api_url(self) -> str:
        return self._active_api_url or self._api_urls[0]

    @staticmethod
    def _resolve_api_urls(api_url: str) -> list[str]:
        configured = api_url or os.environ.get("ZOHO_API_URL", "")
        if configured:
            return [configured.rstrip("/")]
        return [url.rstrip("/") for url in FALLBACK_API_URLS]

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        body: Mapping[str, Any] | list[Any] | None = None,
    ) -> dict[str, Any]:
        """Call an endpoint and return a JSON-compatible response envelope."""
        method = method.upper()
        full_path = self._path_with_query(path, params)
        data = json.dumps(body).encode() if body is not None else None
        headers = sign_headers(self.secret, self.agent_name, method, full_path)
        if data is not None:
            headers["Content-Type"] = "application/json"

        bases = [self._active_api_url] if self._active_api_url else self._api_urls
        last_error = ""
        for base_url in bases:
            if not base_url:
                continue
            url = f"{base_url}{full_path}"
            req = urllib.request.Request(url, data=data, method=method, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    self._active_api_url = base_url
                    return self._read_response(resp)
            except urllib.error.HTTPError as exc:
                self._active_api_url = base_url
                return self._http_error(exc)
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = self._sanitize(str(exc))
                continue

        return {"error": last_error or "Zoho API unavailable", "status": 0}

    def get(self, path: str, **params: Any) -> dict[str, Any]:
        return self.request("GET", path, params=params)

    def post(self, path: str, body: Mapping[str, Any] | list[Any] | None = None) -> dict[str, Any]:
        return self.request("POST", path, body=body)

    def put(self, path: str, body: Mapping[str, Any] | list[Any] | None = None) -> dict[str, Any]:
        return self.request("PUT", path, body=body)

    def patch(self, path: str, body: Mapping[str, Any] | list[Any] | None = None) -> dict[str, Any]:
        return self.request("PATCH", path, body=body)

    @staticmethod
    def _path_with_query(path: str, params: Mapping[str, Any] | None) -> str:
        clean = _clean_params(params)
        if not clean:
            return path
        separator = "&" if "?" in path else "?"
        return f"{path}{separator}{urllib.parse.urlencode(clean, doseq=True)}"

    def _sanitize(self, value: str) -> str:
        if self.secret:
            value = value.replace(self.secret, "[redacted]")
        return value

    def _read_response(self, resp: Any) -> dict[str, Any]:
        raw = resp.read()
        if not raw:
            return {}
        text = raw.decode("utf-8", errors="replace")
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return {"text": self._sanitize(text)}
        return parsed if isinstance(parsed, dict) else {"data": parsed}

    def _http_error(self, exc: urllib.error.HTTPError) -> dict[str, Any]:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            parsed = {"error": self._sanitize(body)}
        if isinstance(parsed, dict):
            parsed.setdefault("status", exc.code)
            return parsed
        return {"error": self._sanitize(str(parsed)), "status": exc.code}
