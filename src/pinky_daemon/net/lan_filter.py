"""LAN-only network filter (#436).

Provides:

* :data:`DEFAULT_PRIVATE_CIDRS` — the RFC1918 / link-local / loopback / ULA
  ranges. CGNAT (``100.64.0.0/10``) is **not** included by default — operators
  who run on Tailscale or another shared-CGNAT fabric have to opt in via the
  ``PINKY_LAN_EXTRA_CIDRS`` env var so we don't accidentally trust the wider
  carrier-grade NAT space.

* :func:`is_private_ip` — pure helper that asks "is this address inside any of
  these CIDRs?", with IPv4/IPv6 type guards so a misconfigured operator doesn't
  blow up the request handler with a ``TypeError``.

* :func:`build_lan_filter_middleware` — factory that returns an ASGI middleware
  closure. Refuses any non-loopback / non-private source with HTTP 403 when
  ``app.state.deployment_mode`` is :class:`DeploymentMode.LAN`. In ``trusted``
  and ``web`` modes the middleware is a no-op pass-through; the operator
  declares intent through ``PINKY_DEPLOYMENT_MODE``, the middleware enforces
  it.

The middleware delegates IP resolution to
:func:`pinky_daemon.net.client_identity.resolve_client_identity` so XFF /
Forwarded handling, trust evaluation, and IPv6-mapped IPv4 normalisation
match the rest of the hardening track.

Part of #433. Issue #436.
"""

from __future__ import annotations

import ipaddress
import logging
import os
from collections.abc import Awaitable, Callable

from fastapi import Request
from fastapi.responses import JSONResponse

from pinky_daemon.config import DeploymentMode
from pinky_daemon.net.client_identity import (
    IPAddress,
    IPNetwork,
    parse_trusted_proxies,
    resolve_client_identity,
)

__all__ = [
    "DEFAULT_PRIVATE_CIDRS",
    "EXTRA_CIDRS_ENV",
    "build_lan_filter_middleware",
    "is_private_ip",
    "resolve_lan_allowed_cidrs",
]

logger = logging.getLogger(__name__)

#: Env var listing operator-supplied additional CIDRs that should be treated
#: as "private" for LAN-mode filtering. Useful for Tailscale (``100.64.0.0/10``)
#: or VPN tenant networks. Comma-separated, same syntax as
#: ``PINKY_TRUSTED_PROXIES``.
EXTRA_CIDRS_ENV = "PINKY_LAN_EXTRA_CIDRS"


#: Default LAN allowlist — RFC1918 + IPv4 link-local + IPv4 loopback + IPv6
#: ULA + IPv6 link-local + IPv6 loopback. CGNAT is **not** included by
#: design; opt in via :data:`EXTRA_CIDRS_ENV`.
DEFAULT_PRIVATE_CIDRS: tuple[IPNetwork, ...] = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("fd00::/8"),
    ipaddress.ip_network("fe80::/10"),
    ipaddress.ip_network("::1/128"),
)


def is_private_ip(addr: IPAddress, allowlist: tuple[IPNetwork, ...]) -> bool:
    """``True`` iff ``addr`` is inside any CIDR in ``allowlist``.

    Type-mismatched comparisons (IPv4 address vs IPv6 network or vice versa)
    are skipped silently rather than raising ``TypeError`` — happens any time
    the operator's allowlist mixes families.
    """
    for net in allowlist:
        if isinstance(addr, ipaddress.IPv4Address) and isinstance(
            net, ipaddress.IPv4Network
        ):
            if addr in net:
                return True
        elif isinstance(addr, ipaddress.IPv6Address) and isinstance(
            net, ipaddress.IPv6Network
        ):
            if addr in net:
                return True
    return False


def resolve_lan_allowed_cidrs(env: dict | None = None) -> tuple[IPNetwork, ...]:
    """Compose the effective LAN allowlist: defaults + operator extras.

    Reads :data:`EXTRA_CIDRS_ENV` from ``env`` (defaults to ``os.environ``),
    parses with :func:`parse_trusted_proxies` (so the same lenient
    "skip-bad-entries" semantics apply), and concatenates with
    :data:`DEFAULT_PRIVATE_CIDRS`. Order matters only for log readability;
    membership tests are O(n).
    """
    source = env if env is not None else os.environ
    extra = parse_trusted_proxies(source.get(EXTRA_CIDRS_ENV))
    return DEFAULT_PRIVATE_CIDRS + extra


# ── Middleware factory ────────────────────────────────────────────


_REJECT_BODY = {
    "error": "forbidden",
    "detail": (
        "Source address is not in the configured LAN allowlist. "
        "Set PINKY_LAN_EXTRA_CIDRS or change PINKY_DEPLOYMENT_MODE if "
        "this is a trusted client."
    ),
}


def build_lan_filter_middleware(
    allowlist: tuple[IPNetwork, ...] | None = None,
):
    """Return an ASGI middleware closure enforcing the LAN allowlist.

    The closure inspects each incoming request:

    * If ``request.app.state.deployment_mode`` is **not** ``LAN``, the
      request passes through untouched. ``trusted`` and ``web`` modes have
      their own enforcement layers (#441 and the reverse-proxy contract).
    * Otherwise, the canonical client IP is computed via
      :func:`resolve_client_identity` (so XFF parsing matches #442) and
      compared against the supplied ``allowlist`` (or the
      ``DEFAULT_PRIVATE_CIDRS`` + env extras when ``None``).
    * Non-private sources receive ``HTTP 403`` with a JSON body; the
      reject is logged at INFO so operators can see scan/probe traffic
      without flooding ERROR.

    Args:
        allowlist: Optional explicit override. When ``None``, the middleware
            recomputes from env on every request — slow path, but useful for
            tests that mutate env between calls. ``create_api`` should
            always pre-resolve and pass an explicit tuple.
    """

    async def lan_filter(
        request: Request,
        call_next: Callable[[Request], Awaitable],
    ):
        state = request.app.state if request.app else None
        mode = getattr(state, "deployment_mode", None)
        if mode is not DeploymentMode.LAN:
            return await call_next(request)

        # Use the cached parsed list when available; fall back to a fresh
        # env read so tests don't have to wire app.state.
        active_allowlist = allowlist
        if active_allowlist is None:
            cached = getattr(state, "lan_allowed_cidrs", None)
            active_allowlist = (
                cached if cached is not None else resolve_lan_allowed_cidrs()
            )

        trusted_proxies = (
            getattr(state, "trusted_proxies", None) if state else None
        ) or ()
        ci = resolve_client_identity(request, mode, trusted_proxies)

        if not is_private_ip(ci.ip, active_allowlist):
            logger.info(
                "lan_filter: rejecting %s %s from %s (raw_peer=%s, via_proxy=%s)",
                request.method,
                request.url.path,
                ci.ip,
                ci.raw_peer,
                ci.via_proxy,
            )
            return JSONResponse(_REJECT_BODY, status_code=403)

        return await call_next(request)

    return lan_filter
