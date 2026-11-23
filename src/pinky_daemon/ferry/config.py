"""Ferry cross-fleet config — fleet identity + Route A (Tailscale bridge) wiring.

All ferry environment lives here. Two consumers:
  - ``inbound_server.build_ferry_app`` — the dedicated ``POST /ferry/inbound``
    listener, bound to the daemon's Tailscale IP only.
  - ``outbound.HttpMeshSender`` — POSTs an envelope to a peer's
    ``/ferry/inbound`` over Tailscale.

Trust model: Tailscale WireGuard + bearer authentication + source-IP allowlist
and peer-fleet ACL. A per-peer credential can only claim its assigned fleet;
the global credential cannot claim a fleet assigned its own credential.
Wire signatures remain a follow-up; ``verify_signatures`` stays ``False``.

Environment variables
---------------------
  PINKYBOT_FLEET_NAME            this fleet's name (default ``pinkybot``).
  PINKYBOT_FERRY_ENABLED        master switch for the inbound listener
                                (``1``/``true``/``yes``/``on``). Default off.
  PINKYBOT_FERRY_SHARED_SECRET  bearer token, identical on both fleets.
  PINKYBOT_FERRY_PEER_SECRETS   JSON-only object mapping fleet names to unique
                                secrets of at least 32 characters. A peer gets
                                its own secret for inbound and outbound traffic.
                                Generate a secret with:
                                python -c 'import secrets; print(secrets.token_urlsafe(32))'
  PINKYBOT_FERRY_BIND_HOST      Tailscale IP the listener binds to.
  PINKYBOT_FERRY_BIND_PORT      listener port (default 8899).
  PINKYBOT_FERRY_ALLOWED_PEER_IPS  comma-separated peer tailnet IPs allowed
                                to POST inbound. Empty = any source (rely on
                                bind + secret). Recommended: set it.
  PINKYBOT_FERRY_PEERS          fleet -> base URL map for outbound. Either a
                                JSON object ``{"peer": "http://100.64.0.2:8899"}``
                                or a comma list ``peer=http://...,other=http://...``.
                                The ``/ferry/inbound`` path is appended.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import re
from collections import Counter
from dataclasses import dataclass, field

DEFAULT_FLEET_NAME = "pinkybot"
DEFAULT_FERRY_PORT = 8899

_TRUTHY = ("1", "true", "yes", "on")
_FLEET_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*", re.ASCII)
_LOGGER = logging.getLogger(__name__)


def _is_unsafe_bind_host(host: str) -> bool:
    """True if ``host`` would expose the ferry listener beyond the tailnet.

    The ferry listener MUST bind to a Tailscale/private IP only (defense-in-depth
    layer #4). A wildcard (``0.0.0.0`` / ``::``), a public IP, or a non-literal
    hostname is unsafe — we fail-closed (refuse to enable) rather than bind
    publicly. Tailscale CGNAT IPs (100.64.0.0/10) are ``is_global=False`` and
    pass; so do RFC1918 / loopback.
    """
    h = (host or "").strip()
    if h in ("", "*"):
        return True
    try:
        ip = ipaddress.ip_address(h)
    except ValueError:
        # Not an IP literal (e.g. a hostname / MagicDNS name) — require an
        # explicit tailnet IP so the bind target is unambiguous.
        return True
    return ip.is_unspecified or ip.is_global


def fleet_name(env: dict[str, str] | None = None) -> str:
    """This daemon's fleet name (``PINKYBOT_FLEET_NAME``, default ``pinkybot``)."""
    e = env if env is not None else os.environ
    return (e.get("PINKYBOT_FLEET_NAME") or DEFAULT_FLEET_NAME).strip() or DEFAULT_FLEET_NAME


def _int(value: str | None, default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _parse_peers(raw: str) -> dict[str, str]:
    """Parse the fleet -> base-URL map from JSON or ``k=v,k=v`` form.

    Malformed input yields an empty map rather than raising — a bad peer map
    must not crash daemon startup; it degrades to "no outbound peer", which
    surfaces as a clear per-send error.
    """
    s = (raw or "").strip()
    if not s:
        return {}
    if s.startswith("{"):
        try:
            obj = json.loads(s)
        except json.JSONDecodeError:
            return {}
        if not isinstance(obj, dict):
            return {}
        return {
            str(k).strip(): str(v).strip()
            for k, v in obj.items()
            if str(k).strip() and str(v).strip()
        }
    out: dict[str, str] = {}
    for pair in s.split(","):
        if "=" not in pair:
            continue
        k, _, v = pair.partition("=")
        k, v = k.strip(), v.strip()
        if k and v:
            out[k] = v
    return out


def _configured_fleet_name(fleet: str, own_fleet: str, peers: dict[str, str]) -> str | None:
    """Return only a name sourced independently of credential-bearing input."""
    for configured in (*peers, own_fleet):
        if configured == fleet:
            return configured
    return None


def _parse_peer_secrets(
    raw: str, own_fleet: str, shared_secret: str, peers: dict[str, str]
) -> dict[str, str]:
    """Resolve peer credentials without echoing values in startup warnings."""
    if not raw.strip():
        return {}
    try:
        obj = json.loads(raw)
    except (ValueError, RecursionError):
        _LOGGER.warning("ferry peer secrets ignored: expected a JSON object")
        return {}
    if not isinstance(obj, dict):
        _LOGGER.warning("ferry peer secrets ignored: expected a JSON object")
        return {}
    counts = Counter(value for value in obj.values() if isinstance(value, str))
    result = {}
    for fleet, secret in obj.items():
        reason = ""
        if _FLEET_KEY.fullmatch(fleet) is None:
            reason = "invalid fleet name"
        elif fleet == own_fleet:
            reason = "own fleet"
        elif not isinstance(secret, str) or len(secret) < 32:
            reason = "secret must contain at least 32 characters"
        elif secret == shared_secret:
            reason = "secret equals global credential"
        elif counts[secret] > 1:
            reason = "duplicate credential"
        if reason:
            configured = _configured_fleet_name(fleet, own_fleet, peers)
            if configured is None:
                _LOGGER.warning(
                    "ferry peer secret ignored for an entry with an unconfigured fleet name: %s",
                    reason,
                )
            else:
                _LOGGER.warning(
                    "ferry peer secret ignored for fleet %s: %s", json.dumps(configured), reason
                )
        else:
            result[fleet] = secret
    return result


@dataclass(frozen=True)
class FerryConfig:
    """Resolved ferry configuration. Build via :meth:`from_env`."""

    enabled: bool
    fleet_name: str
    bind_host: str
    bind_port: int
    shared_secret: str
    allowed_peer_ips: frozenset[str]
    peers: dict[str, str]  # fleet -> base URL (no trailing /ferry/inbound)
    peer_secrets: dict[str, str] = field(default_factory=dict, repr=False)

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "FerryConfig":
        e = dict(env) if env is not None else dict(os.environ)
        secret = (e.get("PINKYBOT_FERRY_SHARED_SECRET") or "").strip()
        own_fleet = fleet_name(e)
        peers = _parse_peers(e.get("PINKYBOT_FERRY_PEERS") or "")
        peer_secrets = _parse_peer_secrets(
            e.get("PINKYBOT_FERRY_PEER_SECRETS") or "", own_fleet, secret, peers
        )
        enabled_flag = (e.get("PINKYBOT_FERRY_ENABLED") or "").strip().lower() in _TRUTHY
        bind_host = (e.get("PINKYBOT_FERRY_BIND_HOST") or "").strip()
        bind_port = _int(e.get("PINKYBOT_FERRY_BIND_PORT"), DEFAULT_FERRY_PORT)
        peer_ips = frozenset(
            ip.strip()
            for ip in (e.get("PINKYBOT_FERRY_ALLOWED_PEER_IPS") or "").split(",")
            if ip.strip()
        )
        # The inbound listener only starts when explicitly enabled AND it has a
        # secret AND a SAFE (tailnet/private, non-wildcard) bind host —
        # fail-closed, never an unauthenticated or publicly-bound listener by
        # omission or misconfiguration.
        enabled = (
            enabled_flag
            and bool(secret or peer_secrets)
            and bool(bind_host)
            and not _is_unsafe_bind_host(bind_host)
        )
        return cls(
            enabled=enabled,
            fleet_name=own_fleet,
            bind_host=bind_host,
            bind_port=bind_port,
            shared_secret=secret,
            allowed_peer_ips=peer_ips,
            peers=peers,
            peer_secrets=peer_secrets,
        )

    def why_disabled(self, env: dict[str, str] | None = None) -> str:
        """Human-readable reason the inbound listener is disabled.

        For startup logging when an operator set ``PINKYBOT_FERRY_ENABLED`` but
        the listener still won't start (missing secret/bind, or an unsafe bind).
        """
        e = dict(env) if env is not None else dict(os.environ)
        if (e.get("PINKYBOT_FERRY_ENABLED") or "").strip().lower() not in _TRUTHY:
            return "PINKYBOT_FERRY_ENABLED not set"
        if not self.shared_secret and not self.peer_secrets:
            return "PINKYBOT_FERRY_SHARED_SECRET not set"
        if not self.bind_host:
            return "PINKYBOT_FERRY_BIND_HOST not set"
        if _is_unsafe_bind_host(self.bind_host):
            return (
                f"PINKYBOT_FERRY_BIND_HOST={self.bind_host!r} is wildcard/public/"
                "non-literal — must be a Tailscale or private IP"
            )
        return "enabled"

    def peer_url(self, fleet: str) -> str | None:
        """Full ``/ferry/inbound`` URL for ``fleet``, or ``None`` if unmapped."""
        base = self.peers.get(fleet)
        if not base:
            return None
        return base.rstrip("/") + "/ferry/inbound"

    def secret_for_fleet(self, fleet: str) -> str:
        """Select only the credential assigned to this outbound destination."""
        return self.peer_secrets.get(fleet, self.shared_secret)

    def diagnostics(self) -> dict:
        """Operator-facing snapshot. Never leaks the secret value."""
        return {
            "enabled": self.enabled,
            "fleet_name": self.fleet_name,
            "bind": f"{self.bind_host}:{self.bind_port}" if self.bind_host else "",
            "shared_secret_set": bool(self.shared_secret),
            "peer_secret_fleets": sorted(name for name in self.peers if name in self.peer_secrets),
            "allowed_peer_ips": sorted(self.allowed_peer_ips),
            "peers": dict(self.peers),
        }
