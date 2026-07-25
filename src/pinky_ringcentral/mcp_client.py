"""Thin HMAC-authenticated HTTP client used by the Pi-side MCP wrapper."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from typing import Any

from pinky_ringcentral.auth import build_signed_headers

FALLBACK_BRIDGE_URLS = (
    "http://100.108.59.106:9101",
    "http://10.0.0.32:9101",
    "http://10.0.0.209:9101",
)

_TRUSTED_BRIDGE_ERROR_CODES = frozenset(
    {
        "sms_service_error",
        "invalid_request",
        "compliance_state_unavailable",
        "do_not_text",
        "quiet_hours",
        "audit_unavailable",
        "sms_not_found",
        "sms_integrity_error",
        "ringcentral_api_error",
    }
)


class RingCentralBridgeClient:
    """At-most-once writes and resilient, idempotent reads."""

    def __init__(
        self,
        *,
        agent_name: str,
        secret: str,
        instance_id: str,
        bridge_url: str = "",
        timeout: float = 30.0,
    ) -> None:
        if not agent_name or not secret or not instance_id:
            raise ValueError("agent_name, secret, and instance_id are required")
        configured = bridge_url or os.environ.get("RINGCENTRAL_BRIDGE_URL", "")
        self._urls = (
            [configured.rstrip("/")]
            if configured
            else [url.rstrip("/") for url in FALLBACK_BRIDGE_URLS]
        )
        self.agent_name = agent_name
        self.secret = secret
        self.instance_id = instance_id
        self.timeout = timeout
        self._active_url: str | None = None

    def get(self, path: str, **params: Any) -> dict[str, Any]:
        clean = {
            key: value
            for key, value in params.items()
            if value is not None and value != ""
        }
        full_path = path
        if clean:
            full_path += "?" + urllib.parse.urlencode(clean, doseq=True)
        return self.request("GET", full_path)

    def post(
        self, path: str, body: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        return self.request("POST", path, body=body)

    def request(
        self,
        method: str,
        path: str,
        *,
        body: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        method = method.upper()
        data = json.dumps(body).encode() if body is not None else None
        headers = build_signed_headers(
            self.secret,
            agent_name=self.agent_name,
            method=method,
            path=path,
            instance_id=self.instance_id,
            body=data or b"",
        )
        if data is not None:
            headers["Content-Type"] = "application/json"
        idempotent = method in {"GET", "HEAD", "OPTIONS"}
        bases = (
            [self._active_url]
            + [url for url in self._urls if url != self._active_url]
            if self._active_url
            else self._urls
        )
        last_error = ""
        for base in bases:
            if not base:
                continue
            request = urllib.request.Request(
                f"{base}{path}",
                data=data,
                method=method,
                headers=headers,
            )
            try:
                with urllib.request.urlopen(
                    request, timeout=self.timeout
                ) as response:
                    self._active_url = base
                    return self._read_response(
                        response, may_have_completed=not idempotent
                    )
            except urllib.error.HTTPError as exc:
                self._active_url = base
                return self._http_error(
                    exc,
                    non_idempotent=not idempotent,
                )
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = self._sanitize(str(exc))
                # A POST may have reached the Mini. Never try a second address.
                if not idempotent:
                    break
        return {
            "error": "bridge_unavailable",
            "message": last_error or "RingCentral bridge unavailable",
            "status": 0,
            "may_have_completed": not idempotent,
        }

    def _read_response(
        self, response: Any, *, may_have_completed: bool
    ) -> dict[str, Any]:
        raw = response.read()
        if not raw:
            return {
                "error": "bridge_malformed_response",
                "may_have_completed": may_have_completed,
            }
        try:
            parsed = json.loads(raw.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            return {
                "error": "bridge_malformed_response",
                "may_have_completed": may_have_completed,
            }
        if not isinstance(parsed, dict):
            return {
                "error": "bridge_malformed_response",
                "may_have_completed": may_have_completed,
            }
        return parsed

    def _http_error(
        self,
        exc: urllib.error.HTTPError,
        *,
        non_idempotent: bool,
    ) -> dict[str, Any]:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            parsed = {
                "error": "bridge_http_error",
                "message": self._sanitize(body)[:500],
            }
        if not isinstance(parsed, dict):
            parsed = {"error": "bridge_http_error", "data": parsed}
        trusted_uncertainty = (
            parsed.get("error") in _TRUSTED_BRIDGE_ERROR_CODES
            and isinstance(parsed.get("message"), str)
            and isinstance(parsed.get("may_have_completed"), bool)
        )
        if (
            non_idempotent
            and 500 <= exc.code < 600
            and not trusted_uncertainty
        ):
            parsed["may_have_completed"] = True
        parsed.setdefault("status", exc.code)
        return parsed

    def _sanitize(self, value: str) -> str:
        return value.replace(self.secret, "[redacted]") if self.secret else value
