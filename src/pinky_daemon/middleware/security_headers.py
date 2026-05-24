"""Security headers middleware (#437).

Helmet-style HTTP response headers, gated by deployment mode. The ``trusted``
posture stays minimal (we don't want to break the existing Mac Mini SPA in
ways the operator wouldn't notice); ``lan`` adds clickjacking + referrer
hardening; ``web`` adds HSTS and a Content Security Policy.

Header matrix
-------------
============================  ========  =====  =====
Header                        trusted   lan    web
============================  ========  =====  =====
X-Content-Type-Options        ✓         ✓      ✓
X-Frame-Options               ✗         ✓      ✓
Referrer-Policy               ✗         ✓      ✓
Permissions-Policy            ✗         ✓      ✓
Content-Security-Policy       ✗         basic  strict
Strict-Transport-Security     ✗         ✗      ✓ (verified-HTTPS only)
X-XSS-Protection              ``0``     ``0``  ``0``
============================  ========  =====  =====

``X-XSS-Protection: 0`` is the modern recommendation: the legacy auditor in
old browsers introduced its own XSS class and OWASP / MDN now recommend
disabling it explicitly. See OWASP "Secure Headers" project, 2024 guidance.

CSP enforcement
---------------
Lands in **report-only** mode by default (``Content-Security-Policy-Report-Only``).
Operators flip to enforcing via :data:`CSP_ENFORCING_ENV`. Rationale: the
Svelte SPA uses inline styles deliberately; flipping straight to enforcing
is a recipe for breaking the UI on a Friday. Soak in report-only first,
fix any genuine surprises, then enforce.

HSTS
----
Only emitted when the request's HTTPS-ness can be **verified**. A bare
``X-Forwarded-Proto: https`` from an untrusted peer must NOT trigger HSTS,
because an attacker who can MITM your HTTP traffic could pin HSTS for a
year on a victim's browser. The check uses
:func:`pinky_daemon.net.client_identity.resolve_client_identity` so trust
evaluation matches the rest of the #433 track.

Part of #433. Issue #437.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Awaitable, Callable

from fastapi import Request

from pinky_daemon.config import DeploymentMode
from pinky_daemon.net.client_identity import (
    peer_is_trusted,
    raw_peer_from_request,
)

__all__ = [
    "CSP_ENFORCING_ENV",
    "build_csp",
    "build_security_headers_middleware",
    "headers_for_mode",
    "is_verified_https",
]

logger = logging.getLogger(__name__)

#: Env var that flips CSP from report-only to enforcing.
CSP_ENFORCING_ENV = "PINKY_CSP_ENFORCING"


# ── Header builders ───────────────────────────────────────────────


_PERMISSIONS_POLICY_BASIC = "camera=(), microphone=(), geolocation=()"
_PERMISSIONS_POLICY_STRICT = (
    "camera=(), microphone=(), geolocation=(), interest-cohort=(), "
    "payment=(), usb=(), magnetometer=(), accelerometer=(), gyroscope=()"
)


def build_csp(mode: DeploymentMode) -> str:
    """Build the Content-Security-Policy string for ``mode``.

    Both ``lan`` and ``web`` policies allow ``'unsafe-inline'`` for
    ``style-src`` because the Svelte SPA uses inline styles deliberately
    (per CLAUDE.md frontend conventions). Removing it requires a refactor
    that's out of scope for #437 — call out as known compromise.

    ``web`` also locks down ``object-src`` and adds ``upgrade-insecure-requests``.
    """
    common = [
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self' 'unsafe-inline'",  # Svelte inline styles, see CLAUDE.md
        "img-src 'self' data:",
        "font-src 'self' data:",
        # SSE + websocket to same origin
        "connect-src 'self' ws: wss:",
        "frame-ancestors 'none'",
        "base-uri 'self'",
        "form-action 'self'",
    ]
    if mode is DeploymentMode.WEB:
        return "; ".join(
            common + ["object-src 'none'", "upgrade-insecure-requests"]
        )
    return "; ".join(common)


def headers_for_mode(mode: DeploymentMode) -> dict[str, str]:
    """Return the static (non-HSTS) header set for a given posture.

    HSTS is intentionally absent — its inclusion depends on per-request
    HTTPS-verification logic that lives in the middleware closure.
    Callers that just want to inspect "what headers would mode X emit" use
    this helper for the static slice.
    """
    out: dict[str, str] = {
        "X-Content-Type-Options": "nosniff",
        "X-XSS-Protection": "0",
    }
    if mode is DeploymentMode.TRUSTED:
        return out
    # lan + web share clickjacking + referrer + permissions-policy.
    out["X-Frame-Options"] = "DENY"
    out["Referrer-Policy"] = "strict-origin-when-cross-origin"
    out["Permissions-Policy"] = (
        _PERMISSIONS_POLICY_STRICT
        if mode is DeploymentMode.WEB
        else _PERMISSIONS_POLICY_BASIC
    )
    return out


# ── HSTS / HTTPS verification ─────────────────────────────────────


def is_verified_https(request: Request) -> bool:
    """``True`` when the request can be **verified** to have arrived over HTTPS.

    Two acceptance paths:

    1. Direct TLS — ``request.url.scheme == "https"``. The ASGI server
       (uvicorn behind a TLS-terminating socket) sets this; can't be
       spoofed by header.
    2. Trusted reverse proxy — ``X-Forwarded-Proto: https`` AND the
       immediate peer is one of the configured trusted proxies (#442).

    A bare ``X-Forwarded-Proto: https`` from an untrusted peer is **not**
    enough — that's the HSTS-poisoning vector we explicitly want to avoid.
    """
    if request.url.scheme == "https":
        return True

    proto = request.headers.get("x-forwarded-proto", "").lower().strip()
    if proto != "https":
        return False

    # The header says HTTPS — only trust it when it came from a configured
    # reverse proxy. Bare X-Forwarded-Proto from an arbitrary peer must NOT
    # be enough; that's the HSTS-poisoning vector.
    state = request.app.state if request.app else None
    trusted = (
        getattr(state, "trusted_proxies", None) if state else None
    ) or ()
    raw_peer = raw_peer_from_request(request)
    return peer_is_trusted(raw_peer, trusted)


# ── Middleware factory ────────────────────────────────────────────


def _csp_enforcing_from_env(env: dict | None = None) -> bool:
    source = env if env is not None else os.environ
    raw = (source.get(CSP_ENFORCING_ENV) or "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def build_security_headers_middleware(*, csp_enforcing: bool | None = None):
    """Return an ASGI middleware closure that emits hardened response headers.

    Args:
        csp_enforcing: When ``True``, emit ``Content-Security-Policy``
            (enforcing). When ``False``, emit ``Content-Security-Policy-Report-Only``.
            When ``None`` (default), read :data:`CSP_ENFORCING_ENV` per
            request — slow path but lets tests flip behavior without
            recreating the middleware.

    The middleware reads ``app.state.deployment_mode`` per request so the
    same closure works in tests that swap modes. Websocket upgrades pass
    through untouched (header injection breaks WS handshakes in some
    clients).
    """

    async def security_headers_mw(
        request: Request,
        call_next: Callable[[Request], Awaitable],
    ):
        # Don't touch WebSocket upgrades — Starlette handles those before
        # response middleware anyway, but be explicit.
        if request.headers.get("upgrade", "").lower() == "websocket":
            return await call_next(request)

        response = await call_next(request)

        state = request.app.state if request.app else None
        mode = getattr(state, "deployment_mode", DeploymentMode.TRUSTED)

        for name, value in headers_for_mode(mode).items():
            response.headers[name] = value

        # CSP only in lan/web. Trusted is purely back-compat.
        if mode is not DeploymentMode.TRUSTED:
            csp_value = build_csp(mode)
            enforcing = (
                csp_enforcing
                if csp_enforcing is not None
                else _csp_enforcing_from_env()
            )
            header_name = (
                "Content-Security-Policy"
                if enforcing
                else "Content-Security-Policy-Report-Only"
            )
            response.headers[header_name] = csp_value

        # HSTS only in web mode and only when HTTPS is verified.
        if mode is DeploymentMode.WEB and is_verified_https(request):
            response.headers["Strict-Transport-Security"] = (
                "max-age=31536000; includeSubDomains"
            )

        return response

    return security_headers_mw
