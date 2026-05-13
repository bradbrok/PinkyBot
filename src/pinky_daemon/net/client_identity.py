"""Canonical trusted-client-IP resolver.

Single source of truth for "what is the real client IP for this request?".
Every IP-dependent feature in the #433 hardening track reads from this resolver
instead of parsing ``X-Forwarded-For`` independently:

* #436 LAN-only middleware (private-CIDR check)
* #438 rate limiting (key extraction)
* #440 audit log (attribution + forensic chain)
* #441 boot guards (trusted-proxy config sanity)

Centralising the logic keeps the spoofing surface minimal and makes attribution
consistent across subsystems.

Spec
----
The behaviour is gated by :class:`pinky_daemon.config.DeploymentMode`:

============  =========================================================
Mode          Behaviour
============  =========================================================
``trusted``   ``ip = raw_peer``; ``X-Forwarded-For``/``Forwarded``
              ignored entirely. The trusted-LAN posture has no proxy.
``lan``       ``ip = raw_peer``; ``XFF``/``Forwarded`` parsed and only
              honored when ``raw_peer`` is inside one of the operator's
              ``PINKY_TRUSTED_PROXIES`` CIDRs.
``web``       Same trust check as ``lan``; otherwise XFF is dropped and
              ``via_proxy`` reports ``False``.
============  =========================================================

``PINKY_TRUSTED_PROXIES`` is a comma-separated list of CIDRs. Loopback is
**not** trusted by default — operators opt in explicitly so that audit and
rate-limit attribution can't silently flatten to ``127.0.0.1`` when something
goes wrong upstream.

Header parsing
--------------
* ``Forwarded`` (RFC 7239) wins over ``X-Forwarded-For`` when both are present.
* ``X-Forwarded-For`` is walked from the right: the rightmost trusted proxy
  is skipped, the next hop to the left is the client. If every hop is trusted,
  the leftmost IP in the chain is the client.
* IPv6-mapped IPv4 (``::ffff:1.2.3.4``) is normalised to the IPv4 form before
  CIDR comparisons so an operator's ``10.0.0.0/8`` rule actually matches.
* Malformed headers cause the parser to drop XFF/Forwarded entirely and fall
  back to ``raw_peer``. A warning is logged at most once per minute per
  failure class to keep noisy clients from filling logs.

Part of #433. Issue #442.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import time
from dataclasses import dataclass, field

from fastapi import Request

from pinky_daemon.config import DEPLOYMENT_MODE_ENV, DeploymentMode  # noqa: F401

__all__ = [
    "TRUSTED_PROXIES_ENV",
    "ClientIdentity",
    "get_client_identity",
    "parse_trusted_proxies",
    "resolve_client_identity",
]

logger = logging.getLogger(__name__)

#: Env var listing trusted reverse-proxy CIDRs (comma-separated).
TRUSTED_PROXIES_ENV = "PINKY_TRUSTED_PROXIES"

# IP address / network type aliases — kept narrow so callers don't need to
# import ipaddress just to type-annotate.
IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network


@dataclass(frozen=True)
class ClientIdentity:
    """Resolved client identity for a single HTTP request.

    Attributes:
        ip: The canonical client IP — what audit/rate-limit/CIDR checks
            should use. Always populated.
        via_proxy: ``True`` when the request arrived through a configured
            trusted proxy and the proxy-supplied client IP was honored.
        raw_peer: The immediate TCP peer (the address that opened the
            socket). Useful for forensic logging when the proxy chain looks
            suspicious.
        forwarded_chain: Full parsed forwarding chain (innermost first).
            Empty when no ``XFF``/``Forwarded`` header was present or when
            the chain was rejected.
    """

    ip: IPAddress
    via_proxy: bool
    raw_peer: IPAddress
    forwarded_chain: tuple[IPAddress, ...] = field(default_factory=tuple)


# ── Trusted-proxy parsing ─────────────────────────────────────────


def parse_trusted_proxies(value: str | None) -> tuple[IPNetwork, ...]:
    """Parse a comma-separated list of CIDRs into IP networks.

    Empty / unset / whitespace → ``()``. Bare addresses (no ``/``) are accepted
    and treated as ``/32`` or ``/128``. Malformed entries are logged and
    skipped rather than raising — the daemon prefers to start with a
    diminished trust list than crash on bad operator input.
    """
    if not value:
        return ()
    parsed: list[IPNetwork] = []
    for raw in value.split(","):
        token = raw.strip()
        if not token:
            continue
        try:
            parsed.append(ipaddress.ip_network(token, strict=False))
        except ValueError:
            logger.warning(
                "%s: ignoring invalid CIDR %r", TRUSTED_PROXIES_ENV, token
            )
    return tuple(parsed)


def trusted_proxies_from_env(env: dict | None = None) -> tuple[IPNetwork, ...]:
    """Read and parse :data:`TRUSTED_PROXIES_ENV` from the given mapping."""
    source = env if env is not None else os.environ
    return parse_trusted_proxies(source.get(TRUSTED_PROXIES_ENV))


# ── IP normalisation helpers ──────────────────────────────────────


def _normalise(addr: IPAddress) -> IPAddress:
    """Collapse IPv6-mapped IPv4 (``::ffff:1.2.3.4``) to IPv4.

    Required so an operator's ``10.0.0.0/8`` rule actually matches a request
    that comes in as an IPv6-mapped peer (common when a server listens on a
    dual-stack socket).
    """
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        return addr.ipv4_mapped
    return addr


def _parse_ip(token: str) -> IPAddress | None:
    """Parse an IP literal, normalising mapped IPv4. Returns ``None`` on junk.

    Strips brackets (``[2001:db8::1]``) and an optional ``:port`` suffix,
    which is what RFC 7239 ``Forwarded: for=...`` and most XFF producers
    emit for IPv6 / port-bearing values.
    """
    raw = token.strip()
    if not raw:
        return None
    # Strip optional surrounding double-quotes (RFC 7239 quoted-string)
    if len(raw) >= 2 and raw[0] == '"' and raw[-1] == '"':
        raw = raw[1:-1].strip()
    if not raw:
        return None
    # Bracketed IPv6: [2001:db8::1] or [2001:db8::1]:443
    if raw.startswith("["):
        end = raw.find("]")
        if end == -1:
            return None
        raw = raw[1:end]
    elif raw.count(":") == 1:
        # Looks like host:port (IPv4 + port). Strip port.
        host, _, _port = raw.partition(":")
        raw = host
    try:
        return _normalise(ipaddress.ip_address(raw))
    except ValueError:
        return None


def _peer_is_trusted(peer: IPAddress, trusted: tuple[IPNetwork, ...]) -> bool:
    """True when ``peer`` falls inside any configured trusted-proxy CIDR."""
    for net in trusted:
        # IPv4 vs IPv6 mismatch raises TypeError on `in` — guard explicitly so
        # mixed configs don't blow up.
        if isinstance(peer, ipaddress.IPv4Address) and isinstance(
            net, ipaddress.IPv4Network
        ):
            if peer in net:
                return True
        elif isinstance(peer, ipaddress.IPv6Address) and isinstance(
            net, ipaddress.IPv6Network
        ):
            if peer in net:
                return True
    return False


# ── Forwarded / XFF parsing ──────────────────────────────────────


def _parse_forwarded_header(header: str) -> tuple[IPAddress, ...]:
    """Parse RFC 7239 ``Forwarded`` header. Returns the chain (innermost first).

    Each comma-separated element is a list of ``key=value`` pairs joined by
    semicolons. We extract every ``for=...`` token in order.
    """
    chain: list[IPAddress] = []
    for element in header.split(","):
        for pair in element.split(";"):
            key, sep, value = pair.partition("=")
            if not sep or key.strip().lower() != "for":
                continue
            ip = _parse_ip(value)
            if ip is not None:
                chain.append(ip)
    return tuple(chain)


def _parse_xff_header(header: str) -> tuple[IPAddress, ...]:
    """Parse ``X-Forwarded-For`` header into a chain (innermost first).

    Innermost = leftmost in the header (the original client, if the chain
    can be trusted).
    """
    chain: list[IPAddress] = []
    for token in header.split(","):
        ip = _parse_ip(token)
        if ip is not None:
            chain.append(ip)
    return tuple(chain)


# ── Once-per-minute warning throttle ──────────────────────────────


_warn_state: dict[str, float] = {}


def _warn_throttled(key: str, message: str) -> None:
    """Log ``message`` at WARNING, but at most once per minute per ``key``.

    Keeps a misbehaving client from filling the log with the same parse
    failure. The state dict is process-local; that's enough for the alert
    surface this resolver targets.
    """
    now = time.monotonic()
    last = _warn_state.get(key, 0.0)
    if now - last < 60.0:
        return
    _warn_state[key] = now
    logger.warning(message)


# ── Resolver ──────────────────────────────────────────────────────


def _raw_peer_from_request(request: Request) -> IPAddress:
    """Pull the immediate TCP peer off a Starlette/FastAPI request.

    Falls back to the unspecified IPv4 address (``0.0.0.0``) when ``client``
    is missing — happens with Starlette's ``TestClient`` if no client tuple
    was provided. Callers can still rely on having an :class:`IPAddress`.
    """
    client = getattr(request, "client", None)
    host = getattr(client, "host", None) if client is not None else None
    if not host:
        return ipaddress.IPv4Address("0.0.0.0")
    try:
        return _normalise(ipaddress.ip_address(host))
    except ValueError:
        # Starlette occasionally hands back things like ``"unix:..."`` for
        # ASGI lifespans; treat as unspecified.
        return ipaddress.IPv4Address("0.0.0.0")


def _client_from_chain(
    chain: tuple[IPAddress, ...], trusted: tuple[IPNetwork, ...]
) -> IPAddress | None:
    """Walk a forwarding chain right-to-left, skipping trusted hops.

    Returns the first non-trusted address encountered (the original client),
    or the leftmost address when every hop is trusted, or ``None`` if the
    chain is empty.
    """
    if not chain:
        return None
    # Walk from the right (closest hop = last entry per RFC 7239 / XFF
    # convention). Skip any hop that is itself a trusted proxy.
    for hop in reversed(chain):
        if not _peer_is_trusted(hop, trusted):
            return hop
    # Whole chain is trusted proxies — the leftmost entry is the client.
    return chain[0]


def resolve_client_identity(
    request: Request,
    mode: DeploymentMode,
    trusted_proxies: tuple[IPNetwork, ...] = (),
) -> ClientIdentity:
    """Resolve the canonical :class:`ClientIdentity` for ``request``.

    See module docstring for the full spec. This is the single function every
    IP-consuming feature in the hardening track should call.
    """
    raw_peer = _raw_peer_from_request(request)

    # Trusted-LAN posture: never honor forwarded headers, full stop.
    if mode is DeploymentMode.TRUSTED:
        return ClientIdentity(ip=raw_peer, via_proxy=False, raw_peer=raw_peer)

    # lan / web posture: parse forwarding headers, but only trust them when
    # the immediate peer is one of our configured proxies.
    forwarded_header = request.headers.get("forwarded")
    xff_header = request.headers.get("x-forwarded-for")

    chain: tuple[IPAddress, ...] = ()
    parse_failed = False
    if forwarded_header:
        try:
            chain = _parse_forwarded_header(forwarded_header)
        except Exception:  # pragma: no cover - defensive
            parse_failed = True
    if not chain and xff_header:
        try:
            chain = _parse_xff_header(xff_header)
        except Exception:  # pragma: no cover - defensive
            parse_failed = True

    if parse_failed and (forwarded_header or xff_header):
        _warn_throttled(
            "client_identity.parse_failed",
            "client-IP resolver: failed to parse forwarding header; "
            "falling back to raw peer",
        )
        chain = ()

    # Did anyone send a forwarding header at all?
    sent_forwarding = bool(forwarded_header or xff_header)

    if not _peer_is_trusted(raw_peer, trusted_proxies):
        # Untrusted peer claiming to be a proxy — drop their chain and log
        # so audit can correlate suspected spoofing attempts.
        if sent_forwarding:
            _warn_throttled(
                "client_identity.untrusted_proxy",
                f"client-IP resolver: untrusted peer {raw_peer} sent "
                "forwarding headers; dropping chain",
            )
        return ClientIdentity(
            ip=raw_peer, via_proxy=False, raw_peer=raw_peer, forwarded_chain=()
        )

    # Trusted peer with a chain → identify the original client.
    client_ip = _client_from_chain(chain, trusted_proxies)
    if client_ip is None:
        # Trusted peer but no parseable chain — treat the peer itself as the
        # client. This is the legitimate "proxy talking to us directly" case.
        return ClientIdentity(
            ip=raw_peer, via_proxy=False, raw_peer=raw_peer, forwarded_chain=()
        )

    return ClientIdentity(
        ip=client_ip,
        via_proxy=True,
        raw_peer=raw_peer,
        forwarded_chain=chain,
    )


# ── FastAPI dependency ────────────────────────────────────────────


def get_client_identity(request: Request) -> ClientIdentity:
    """FastAPI dependency: ``Depends(get_client_identity) -> ClientIdentity``.

    Reads:
        * ``request.app.state.deployment_mode`` — set by ``create_api``
        * ``request.app.state.trusted_proxies`` — set by ``create_api`` from
          :data:`TRUSTED_PROXIES_ENV`. Falls back to env on each call when
          unset, so this dep stays usable from tests that don't bother
          configuring app state.
    """
    state = getattr(request, "app", None)
    state = getattr(state, "state", None) if state is not None else None

    mode = getattr(state, "deployment_mode", None) if state is not None else None
    if mode is None:
        from pinky_daemon.config import resolve_deployment_mode

        mode = resolve_deployment_mode()

    trusted = (
        getattr(state, "trusted_proxies", None) if state is not None else None
    )
    if trusted is None:
        trusted = trusted_proxies_from_env()

    return resolve_client_identity(request, mode, trusted)
