"""Ferry inbound — dedicated Tailscale-bound listener for ``POST /ferry/inbound``.

Route A (Tailscale bridge). A *separate* tiny FastAPI app + uvicorn server,
bound to the daemon's Tailscale IP only, so the cross-fleet attack surface is
exactly one authenticated endpoint; the main API keeps its existing bind.

Defense in depth, all fail-closed:
  1. global or fleet-bound per-peer bearer token
  2. source-IP allowlist (``PINKYBOT_FERRY_ALLOWED_PEER_IPS`` — peer tailnet IPs)
  3. peer-fleet ACL (enforced inside ``HostPinky.deliver``)
  4. listener bound to the Tailscale IP — never ``0.0.0.0`` / public

Wire signatures (``pinky_federation`` sealed-box / TOFU) are a follow-up;
until then trust = Tailscale WireGuard + the three checks above. See task #202.
"""

from __future__ import annotations

import hmac
import json
import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from pinky_daemon.ferry.types import FerryEnvelope, TraversalRecord

_LOGGER = logging.getLogger(__name__)


def _opt_str(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value)
    return s or None


def _opt_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _lit(value: Any, allowed: tuple[str, ...], default: str) -> str:
    s = str(value).strip() if value is not None else ""
    return s if s in allowed else default


def envelope_from_wire(payload: dict[str, Any]) -> FerryEnvelope:
    """Reconstruct a :class:`FerryEnvelope` from its on-wire JSON form.

    Inverse of ``outbound.envelope_to_wire``: maps ``"from"`` → ``from_`` and
    rebuilds the traversal records. Unknown/garbage fields are coerced to safe
    defaults rather than trusted — this is a cross-fleet boundary, so the
    envelope is reconstructed field-by-field, never ``**payload``-splatted.
    """
    if not isinstance(payload, dict):
        raise ValueError("envelope payload must be a JSON object")
    body = payload.get("body")
    if not isinstance(body, dict):
        raise ValueError("envelope.body must be an object")
    if "kind" not in body:
        raise ValueError("envelope.body must contain a 'kind' field")

    traversal: list[TraversalRecord] = []
    for t in payload.get("traversal") or []:
        if isinstance(t, dict):
            traversal.append(
                TraversalRecord(
                    broker=str(t.get("broker", "")),
                    at=_opt_int(t.get("at")) or 0,
                    via=str(t.get("via", "")),
                    signature=str(t.get("signature", "")),
                )
            )

    raw_headers = payload.get("headers") or {}
    headers = (
        {str(k): str(v) for k, v in raw_headers.items()}
        if isinstance(raw_headers, dict)
        else {}
    )

    return FerryEnvelope(
        v=str(payload.get("v", "0.1")),
        id=str(payload.get("id", "")),
        from_=str(payload.get("from", "")),
        to=str(payload.get("to", "")),
        ts=_opt_int(payload.get("ts")) or 0,
        body=dict(body),
        priority=_lit(payload.get("priority"), ("low", "normal", "high", "urgent"), "normal"),
        wake=_lit(payload.get("wake"), ("none", "if-idle", "when-free", "interrupt"), "if-idle"),
        ack=_lit(payload.get("ack"), ("none", "delivery", "processed"), "delivery"),
        ttl_ms=_opt_int(payload.get("ttl_ms")),
        correlation_id=_opt_str(payload.get("correlation_id")),
        reply_to=_opt_str(payload.get("reply_to")),
        subject=_opt_str(payload.get("subject")),
        traversal=traversal,
        headers=headers,
    )


def build_ferry_app(*, host_pinky: Any, config: Any):
    """Build the dedicated ferry FastAPI app.

    ``host_pinky`` is the daemon's singleton ``HostPinky`` (shares the live
    registry + broker so delivery reaches real streaming sessions). ``config``
    is a :class:`pinky_daemon.ferry.config.FerryConfig`.
    """
    app = FastAPI(title="pinky-ferry", docs_url=None, redoc_url=None, openapi_url=None)

    def _client_ip(request: Request) -> str:
        return request.client.host if request.client else ""

    def _credential(request: Request) -> tuple[str, str] | None:
        auth = request.headers.get("authorization", "")
        token = auth[7:].strip() if auth[:7].lower() == "bearer " else ""
        credential = None
        candidates = [(('global', ''), config.shared_secret)] + [
            (('peer', fleet), secret) for fleet, secret in config.peer_secrets.items()
        ]
        for identity, secret in candidates:
            matched = hmac.compare_digest(token.encode("utf-8"), secret.encode("utf-8"))
            if matched and token and secret:
                credential = identity
        # Source-IP allowlist — only enforced when configured. Empty list means
        # "any tailnet source" (the bind + bearer are the boundary then).
        if config.allowed_peer_ips and _client_ip(request) not in config.allowed_peer_ips:
            return None
        return credential

    @app.get("/ferry/health")
    async def ferry_health():  # noqa: ANN202
        return {"ok": True, "fleet": config.fleet_name, "service": "pinky-ferry"}

    @app.post("/ferry/inbound")
    async def ferry_inbound(request: Request):  # noqa: ANN202
        if not config.shared_secret and not config.peer_secrets:
            return JSONResponse(
                status_code=503, content={"ok": False, "error": "ferry not configured"}
            )
        credential = _credential(request)
        if credential is None:
            return JSONResponse(
                status_code=401, content={"ok": False, "error": "unauthorized"}
            )
        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001 — any parse failure is a 400
            return JSONResponse(
                status_code=400, content={"ok": False, "error": "invalid json"}
            )
        try:
            envelope = envelope_from_wire(payload)
        except (ValueError, TypeError) as e:
            return JSONResponse(
                status_code=400, content={"ok": False, "error": f"bad envelope: {e}"}
            )

        from pinky_daemon.ferry.config import (
            _CONTROL_CHARACTERS,
            _FLEET_KEY,
            _configured_fleet_name,
        )
        from pinky_daemon.ferry.host_pinky import parse_peer_card
        from pinky_daemon.ferry.outbound import parse_address

        parsed = parse_address(envelope.from_)
        claimed_fleet, agent = parsed or ("", "")
        raw_fleet, _ = parse_peer_card(envelope.from_)
        valid_sender = (
            parsed is not None
            and bool(claimed_fleet)
            and _FLEET_KEY.fullmatch(claimed_fleet) is not None
            and raw_fleet == claimed_fleet
            and envelope.from_ in (f"ferry://{claimed_fleet}/{agent}", f"{agent}@{claimed_fleet}")
            and _CONTROL_CHARACTERS.search(envelope.from_) is None
            and "/" not in agent
            and not any(character.isspace() for character in envelope.from_)
        )
        kind, fleet = credential
        if kind == "global":
            refused = not valid_sender or claimed_fleet in config.peer_secrets
        else:
            refused = not valid_sender or claimed_fleet != fleet
        if refused:
            _LOGGER.warning(
                "ferry sender refused credential_fleet=%s claimed_fleet=%s source_ip=%s",
                json.dumps(
                    (_configured_fleet_name(fleet, config.fleet_name, config.peers) or "unconfigured")
                    if kind == "peer" else "global"
                ),
                json.dumps(
                    _configured_fleet_name(claimed_fleet, config.fleet_name, config.peers)
                    or "unconfigured"
                ),
                json.dumps(_client_ip(request)),
            )
            return JSONResponse(
                status_code=403,
                content={"ok": False, "error": "sender fleet does not match credential"},
            )

        result = await host_pinky.deliver(envelope)

        if result.status in ("delivered", "queued"):
            http_status = 200
        elif result.status == "rejected":
            http_status = 403
        else:  # transient_failure → 503 so the sender can retry
            http_status = 503
        return JSONResponse(
            status_code=http_status,
            content={
                "ok": result.status in ("delivered", "queued"),
                "status": result.status,
                "reason": result.reason,
            },
        )

    return app
