"""CSRF protection for cookie-authenticated state-changing endpoints (#443).

Pattern
-------
Double-submit cookie + header. On every request:

* If the caller carries a session cookie but no ``pinky_csrf`` cookie,
  the response sets one — a 32-byte URL-safe random token,
  ``SameSite=Strict``, **not** HttpOnly so the SPA can read it.
* On every unsafe verb (POST / PUT / PATCH / DELETE) the middleware
  compares the ``X-Pinky-CSRF`` header against the ``pinky_csrf`` cookie.
  Mismatch → ``HTTP 403 {"error": "csrf_check_failed"}``.

Why not pure SameSite
---------------------
``SameSite=Strict`` blocks top-level cross-site POSTs in modern
browsers, but leaves real gaps:

* Older browsers without enforcement
* Multi-window cross-tab flows
* Subdomain-trust attacks if PinkyBot ever shares a parent domain
* OAuth/redirect flows that intentionally use ``SameSite=Lax``

Once #439's elevation endpoints exist, an explicit token is
required defense-in-depth.

Exemptions
----------
* **Bearer-token authentication.** No ambient credential vulnerability
  — the attacker has to steal the bearer token, at which point they
  don't need CSRF. Detected via the ``Authorization`` header.
* **Webhook routes** (``/triggers/webhook/*``) — signature/token gated
  upstream of any cookie auth.
* **Twilio callbacks** (``/twilio/*``) — Twilio signature gated.
* **Internal MCP-signed requests** (``X-Pinky-Agent`` + signature
  headers from :mod:`pinky_daemon.auth`).
* **Trusted mode** — middleware off for back-compat.

Token rotation
--------------
The cookie value is set fresh whenever a request with a session cookie
arrives without a CSRF cookie. Login (which clears the session and
sets a new one) naturally rotates: the next response sees a session
without CSRF and writes a new token. An explicit
``/auth/csrf/refresh`` endpoint is out of scope for this PR; the
session-bound rotation is enough for the routes shipping today.

Audit
-----
Every CSRF reject emits an audit event into ``app.state.security_audit``
when one is wired (no-op otherwise). Event type
``auth.csrf.rejected`` is registered in
:data:`pinky_daemon.security_audit.EVENT_DETAIL_ALLOWLIST`-extension
patterns; until that allowlist gains the entry, the redactor falls
back to the conservative default.

Part of #433. Issue #443.
"""

from __future__ import annotations

import logging
import secrets
from collections.abc import Awaitable, Callable

from fastapi import Request

from pinky_daemon.auth import (
    INTERNAL_AGENT_HEADER,
    INTERNAL_SIGNATURE_HEADER,
    SESSION_COOKIE_NAME,
)
from pinky_daemon.config import DeploymentMode

__all__ = [
    "CSRF_COOKIE_NAME",
    "CSRF_HEADER_NAME",
    "UNSAFE_METHODS",
    "build_csrf_middleware",
    "generate_csrf_token",
    "is_exempt_path",
]

logger = logging.getLogger(__name__)

CSRF_COOKIE_NAME = "pinky_csrf"
CSRF_HEADER_NAME = "x-pinky-csrf"

#: HTTP verbs that mutate state and therefore require a token.
UNSAFE_METHODS: frozenset[str] = frozenset({"POST", "PUT", "PATCH", "DELETE"})

#: Path prefixes that bypass CSRF (their own auth/signature gates).
_EXEMPT_PREFIXES: tuple[str, ...] = (
    "/triggers/webhook/",
    "/twilio/",
)


def generate_csrf_token() -> str:
    """Cryptographically random URL-safe CSRF token."""
    return secrets.token_urlsafe(32)


def is_exempt_path(path: str) -> bool:
    """``True`` when the path is auth-gated by something other than a
    cookie session (webhooks, Twilio callbacks)."""
    return any(path.startswith(prefix) for prefix in _EXEMPT_PREFIXES)


def _is_bearer_or_internal_authed(request: Request) -> bool:
    """``True`` when the request authenticates via something other than a
    cookie session — bearer token or signed internal MCP call."""
    auth = request.headers.get("authorization", "").strip()
    if auth.lower().startswith("bearer "):
        return True
    if request.headers.get(INTERNAL_AGENT_HEADER) and request.headers.get(
        INTERNAL_SIGNATURE_HEADER
    ):
        return True
    return False


def _has_session_cookie(request: Request) -> bool:
    return bool(request.cookies.get(SESSION_COOKIE_NAME))


def _csrf_cookie_kwargs(*, mode: DeploymentMode) -> dict:
    """Cookie attribute dict for the CSRF token. Secure flag is mode-aware
    so localhost dev (trusted/lan) doesn't need TLS."""
    return {
        "key": CSRF_COOKIE_NAME,
        "samesite": "strict",
        "httponly": False,  # frontend reads it
        "secure": mode is DeploymentMode.WEB,
        "path": "/",
    }


def build_csrf_middleware():
    """Return an ASGI middleware closure enforcing the CSRF token check.

    Trusted mode short-circuits to a no-op (back-compat). LAN and Web
    modes apply the check.

    Token issuance happens unconditionally for any request that carries
    a session cookie but no CSRF cookie — the response Set-Cookies a
    fresh token. That covers initial login (no cookie yet) and rotation
    after explicit logout.
    """

    async def csrf_mw(
        request: Request,
        call_next: Callable[[Request], Awaitable],
    ):
        state = request.app.state if request.app else None
        mode = getattr(state, "deployment_mode", DeploymentMode.TRUSTED)
        if mode is DeploymentMode.TRUSTED:
            return await call_next(request)

        method = request.method.upper()
        path = request.url.path

        # Determine whether to enforce on this request.
        enforce = (
            method in UNSAFE_METHODS
            and not is_exempt_path(path)
            and not _is_bearer_or_internal_authed(request)
            and _has_session_cookie(request)
        )

        if enforce:
            cookie_token = request.cookies.get(CSRF_COOKIE_NAME) or ""
            header_token = request.headers.get(CSRF_HEADER_NAME) or ""
            if (
                not cookie_token
                or not header_token
                or not secrets.compare_digest(cookie_token, header_token)
            ):
                _emit_reject_audit(state, request, has_cookie=bool(cookie_token))
                from fastapi.responses import JSONResponse

                return JSONResponse(
                    {
                        "error": "csrf_check_failed",
                        "detail": (
                            "missing or invalid CSRF token; refresh the page "
                            "to obtain a new token"
                        ),
                    },
                    status_code=403,
                )

        # Pass to the route. Issue a token cookie afterwards if the
        # caller had a session but no token yet.
        response = await call_next(request)

        if _has_session_cookie(request) and not request.cookies.get(
            CSRF_COOKIE_NAME
        ):
            response.set_cookie(
                value=generate_csrf_token(),
                **_csrf_cookie_kwargs(mode=mode),
            )

        return response

    return csrf_mw


# ── Audit emission ───────────────────────────────────────────────


def _emit_reject_audit(
    state, request: Request, *, has_cookie: bool
) -> None:
    """Best-effort audit emission for a CSRF rejection."""
    sec_audit = getattr(state, "security_audit", None) if state else None
    if sec_audit is None:
        return
    try:
        from pinky_daemon.net.client_identity import resolve_client_identity
        from pinky_daemon.security_audit import AuditOutcome

        trusted = (
            getattr(state, "trusted_proxies", None) if state else None
        ) or ()
        mode = getattr(state, "deployment_mode", DeploymentMode.TRUSTED)
        ci = resolve_client_identity(request, mode, trusted)
        sec_audit.log_event(
            "auth.csrf.rejected",
            outcome=AuditOutcome.DENIED,
            actor_ip=str(ci.ip),
            via_proxy=ci.via_proxy,
            forwarded_chain=[str(h) for h in ci.forwarded_chain]
            if ci.forwarded_chain
            else None,
            method=request.method,
            target=request.url.path,
            user_agent=request.headers.get("user-agent"),
            detail={"reason": "missing" if not has_cookie else "mismatch"},
        )
    except Exception:  # noqa: BLE001
        logger.exception("csrf: failed to emit audit event")
