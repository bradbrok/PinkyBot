"""@ferry/host-pinky — outbound side: build + publish ferry envelopes.

Companion to host_pinky.py (inbound). Where host_pinky.py receives envelopes
and routes them into PinkyBot, this module builds envelopes from PinkyBot
agents and publishes them onto the ferry transport.

Design choice — shellout, not in-process NATS client:
    host_pinky.py's docstring is explicit that the Python module is the
    **adapter** and "does no transport (NATS / leaf-link plumbing is owned
    by ferry-core)". To keep that boundary clean on the outbound side, we
    shell out to the `nats` CLI (https://github.com/nats-io/natscli) rather
    than introduce nats-py as a dep. One-shot publishes are the only outbound
    pattern we need; the CLI matches that shape and we already proved it
    works end-to-end (Barsik smoke 2026-05-10 12:14 PDT, sub-2s).

If we later need a long-lived in-process publisher (e.g. for high-throughput
streaming), this module's surface (`MeshSender.send`) lets us swap the
shellout for an async nats-py client without touching callers.

References:
- ferry PROTOCOL.md v0.1 §1 — envelope schema (mirrored by ferry/types.py)
- PinkyBot issue #418 — outbound mesh_remote_send tool
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass
from typing import Any

from pinky_daemon.ferry.types import FerryEnvelope

# -- Address parsing -----------------------------------------------------------

_FERRY_SCHEME = "ferry://"


def parse_address(addr: str) -> tuple[str, str] | None:
    """Parse a ferry address into ``(fleet, agent_slug)``.

    Accepts two forms:
      - ``ferry://<fleet>/<agent_slug>``  — preferred canonical form
      - ``<agent_slug>@<fleet>``          — at-form for casual use

    Returns ``None`` for malformed input. ``agent_slug`` may itself contain
    ``@`` (e.g. for scoped peer addresses like ``pulse@studio@sigil``); only
    the rightmost ``@`` is the fleet boundary.
    """
    if not addr or not isinstance(addr, str):
        return None
    s = addr.strip()
    if not s:
        return None

    if s.startswith(_FERRY_SCHEME):
        rest = s[len(_FERRY_SCHEME):]
        parts = rest.split("/", 1)
        if len(parts) != 2:
            return None
        fleet, agent_slug = parts[0].strip(), parts[1].strip()
        if not fleet or not agent_slug:
            return None
        return (fleet, agent_slug)

    # at-form: rightmost @ is the fleet boundary
    if "@" not in s:
        return None
    head, _, fleet = s.rpartition("@")
    head, fleet = head.strip(), fleet.strip()
    if not head or not fleet:
        return None
    return (fleet, head)


def _sanitize_token(s: str) -> str:
    """Make a string safe to use as a NATS subject token.

    NATS subjects are dot-separated; ``.`` and ``@`` are replaced with ``-``
    so each segment occupies exactly one token. Sanitization is reversible
    at the application layer via the envelope's ``from`` / ``to`` fields,
    which preserve the original address forms.
    """
    return s.replace(".", "-").replace("@", "-")


def derive_subject(fleet: str, agent_slug: str) -> str:
    """NATS subject for a ferry envelope addressed to ``agent_slug@fleet``.

    Convention: ``ferry.<fleet>.<agent_slug>.inbox``. Both ``fleet`` and
    ``agent_slug`` are sanitized — see ``_sanitize_token``.

    The ``ferry.>`` prefix matches the ``pinkybot_fleet`` ACL granted to
    PinkyBot on Nova (confirmed by Pulse 2026-05-10).
    """
    return f"ferry.{_sanitize_token(fleet)}.{_sanitize_token(agent_slug)}.inbox"


# -- Allowlist matching --------------------------------------------------------


def allowlist_matches(target: str, allowlist: list[str]) -> bool:
    """True if ``target`` matches any pattern in ``allowlist``.

    Patterns:
      - exact match (e.g. ``pulse@pulse``)
      - wildcard agent: ``*@<fleet>`` (any agent on the fleet)
      - wildcard fleet: ``<agent>@*`` (any fleet for that agent slug)
      - global: ``*@*`` or ``*`` (any target — use sparingly)

    Empty allowlist means default-deny. Wildcards do NOT match across
    address forms — pass canonical form (at-form) only.
    """
    if not allowlist:
        return False
    parsed_target = parse_address(target)
    if parsed_target is None:
        return False
    t_fleet, t_agent = parsed_target
    for pat in allowlist:
        if not pat:
            continue
        p = pat.strip()
        if p in ("*", "*@*"):
            return True
        parsed_pat = parse_address(p)
        if parsed_pat is None:
            continue
        p_fleet, p_agent = parsed_pat
        fleet_ok = p_fleet == "*" or p_fleet == t_fleet
        agent_ok = p_agent == "*" or p_agent == t_agent
        if fleet_ok and agent_ok:
            return True
    return False


# -- Envelope builder ----------------------------------------------------------


def build_envelope(
    *,
    from_: str,
    to: str,
    body: dict[str, Any],
    correlation_id: str | None = None,
    reply_to: str | None = None,
    priority: str = "normal",
    headers: dict[str, str] | None = None,
) -> FerryEnvelope:
    """Build a canonical ferry envelope per PROTOCOL.md v0.1 §1.

    ``body`` MUST contain a ``kind`` field (per spec). Caller is responsible
    for setting it; this function does not infer it.

    ``id`` is a uuid (v4 for now; spec calls for uuid-v7 — followup once
    Python stdlib lands it in 3.13+ or we add the ``uuid-utils`` dep).
    """
    if not isinstance(body, dict) or "kind" not in body:
        raise ValueError("envelope.body must be a dict containing a 'kind' field")
    return FerryEnvelope(
        v="0.1",
        id=str(uuid.uuid4()),
        from_=from_,
        to=to,
        ts=int(time.time() * 1000),
        body=dict(body),
        priority=priority if priority in ("low", "normal", "high", "urgent") else "normal",
        correlation_id=correlation_id,
        reply_to=reply_to,
        headers=dict(headers or {}),
    )


def envelope_to_wire(env: FerryEnvelope) -> str:
    """Serialize an envelope to its on-wire JSON form.

    Renames ``from_`` → ``from`` (Python keyword workaround), drops
    default-valued optional fields to keep wire compact.
    """
    payload: dict[str, Any] = {
        "v": env.v,
        "id": env.id,
        "from": env.from_,
        "to": env.to,
        "ts": env.ts,
        "body": env.body,
    }
    if env.priority != "normal":
        payload["priority"] = env.priority
    if env.wake != "if-idle":
        payload["wake"] = env.wake
    if env.ack != "delivery":
        payload["ack"] = env.ack
    if env.ttl_ms is not None:
        payload["ttl_ms"] = env.ttl_ms
    if env.correlation_id:
        payload["correlation_id"] = env.correlation_id
    if env.reply_to:
        payload["reply_to"] = env.reply_to
    if env.subject:
        payload["subject"] = env.subject
    if env.traversal:
        # FerryEnvelope.traversal is List[TraversalRecord] dataclasses;
        # serialize each via __dict__ (small, flat, no nested dataclasses).
        payload["traversal"] = [t.__dict__ for t in env.traversal]
    if env.headers:
        payload["headers"] = env.headers
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)


# -- Sender --------------------------------------------------------------------


@dataclass
class SendResult:
    """Outcome of a single ``MeshSender.send`` call."""

    sent: bool
    correlation_id: str | None
    subject: str
    ts: int
    error: str | None = None


class MeshSender:
    """Daemon-owned outbound publisher.

    One per-daemon instance, constructed at startup. Reads NATS credentials
    and server URL from the environment; never accepts them as arguments
    so they don't leak into agent context, request bodies, or logs.

    Environment variables:
      PINKYBOT_FLEET_USER       — NATS user (e.g. "pinkybot_fleet")
      PINKYBOT_FLEET_PASSWORD   — NATS password (treated as a secret)
      PINKYBOT_FLEET_NATS_URL   — server URL (default wss://fleet-nats.kostverse.com)
      PINKYBOT_FERRY_NATS_BIN   — path to nats CLI (default looks up "nats" on PATH)
    """

    DEFAULT_NATS_URL = "wss://fleet-nats.kostverse.com"
    DEFAULT_TIMEOUT_S = 10.0

    def __init__(
        self,
        *,
        env: dict[str, str] | None = None,
        bin_path: str | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        e = env if env is not None else dict(os.environ)
        self._user = e.get("PINKYBOT_FLEET_USER", "").strip()
        self._password = e.get("PINKYBOT_FLEET_PASSWORD", "").strip()
        self._nats_url = e.get("PINKYBOT_FLEET_NATS_URL", self.DEFAULT_NATS_URL).strip()
        self._bin_path = (
            bin_path
            or e.get("PINKYBOT_FERRY_NATS_BIN", "").strip()
            or shutil.which("nats")
            or ""
        )
        self._timeout_s = timeout_s

    @property
    def configured(self) -> bool:
        """True iff creds + URL + nats CLI binary are all available."""
        return bool(self._user and self._password and self._nats_url and self._bin_path)

    def diagnostics(self) -> dict[str, Any]:
        """Operator-facing health snapshot. Never includes the password."""
        return {
            "configured": self.configured,
            "user_set": bool(self._user),
            "password_set": bool(self._password),
            "nats_url": self._nats_url,
            "nats_bin": self._bin_path,
        }

    def send(self, envelope: FerryEnvelope) -> SendResult:
        """Publish an envelope to its derived subject. Synchronous.

        Failure modes are returned as ``SendResult(sent=False, error=...)``
        rather than raised — callers (the API endpoint) want a structured
        response either way.
        """
        ts = envelope.ts
        cid = envelope.correlation_id

        if not self.configured:
            return SendResult(
                sent=False,
                correlation_id=cid,
                subject="",
                ts=ts,
                error="mesh sender not configured (missing creds, URL, or nats CLI)",
            )

        parsed = parse_address(envelope.to)
        if parsed is None:
            return SendResult(
                sent=False,
                correlation_id=cid,
                subject="",
                ts=ts,
                error=f"unparseable target address: {envelope.to!r}",
            )
        fleet, agent_slug = parsed
        subject = derive_subject(fleet, agent_slug)
        wire = envelope_to_wire(envelope)

        # The password goes through the child's environment (the nats CLI
        # reads NATS_PASSWORD), never through argv -- command lines are
        # world-readable via ps / /proc/<pid>/cmdline.
        cmd = [
            self._bin_path,
            "--user", self._user,
            "-s", self._nats_url,
            "pub", subject, wire,
        ]
        child_env = {**os.environ, "NATS_PASSWORD": self._password}
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self._timeout_s,
                check=False,
                env=child_env,
            )
        except subprocess.TimeoutExpired:
            return SendResult(
                sent=False,
                correlation_id=cid,
                subject=subject,
                ts=ts,
                error=f"nats CLI timed out after {self._timeout_s}s",
            )
        except OSError as exc:
            return SendResult(
                sent=False,
                correlation_id=cid,
                subject=subject,
                ts=ts,
                error=f"nats CLI invocation failed: {exc}",
            )

        if proc.returncode != 0:
            # stderr may contain auth/ACL detail — include but do NOT
            # include the child environment (which has the password).
            err_tail = (proc.stderr or proc.stdout or "").strip().splitlines()
            err_msg = err_tail[-1] if err_tail else f"nats CLI exit {proc.returncode}"
            return SendResult(
                sent=False,
                correlation_id=cid,
                subject=subject,
                ts=ts,
                error=err_msg,
            )

        return SendResult(
            sent=True,
            correlation_id=cid,
            subject=subject,
            ts=ts,
            error=None,
        )


class HttpMeshSender:
    """Route A outbound — POST a ferry envelope to the peer fleet's
    ``/ferry/inbound`` over Tailscale.

    Same surface as :class:`MeshSender` (``send`` / ``configured`` /
    ``diagnostics``) so the API endpoint can pick either transport without
    branching on shape. Where ``MeshSender`` publishes to a NATS broker, this
    POSTs directly to the addressed peer daemon's dedicated ferry listener
    over the tailnet — no third-party broker, no public-internet hop.

    The shared secret is sent as a bearer token and never logged. The peer
    URL map + secret come from :class:`pinky_daemon.ferry.config.FerryConfig`
    (the environment), never from the request body, so they don't leak into
    agent context.
    """

    DEFAULT_TIMEOUT_S = 10.0

    def __init__(
        self,
        *,
        config: Any | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        from pinky_daemon.ferry.config import FerryConfig

        self._config = config if config is not None else FerryConfig.from_env()
        self._timeout_s = timeout_s

    @property
    def configured(self) -> bool:
        """True iff a shared secret and at least one peer URL are set."""
        return bool(self._config.shared_secret and self._config.peers)

    def diagnostics(self) -> dict[str, Any]:
        """Operator-facing health snapshot. Never includes the secret value."""
        return {"transport": "http", "configured": self.configured, **self._config.diagnostics()}

    def send(self, envelope: FerryEnvelope) -> SendResult:
        """POST an envelope to its peer fleet's ferry listener. Synchronous.

        Failures are returned as ``SendResult(sent=False, error=...)`` rather
        than raised — callers want a structured response either way. This is a
        blocking call (stdlib ``urllib``); the API endpoint runs it in a
        threadpool so it never blocks the event loop.
        """
        import urllib.error
        import urllib.request

        ts = envelope.ts
        cid = envelope.correlation_id

        parsed = parse_address(envelope.to)
        if parsed is None:
            return SendResult(
                sent=False, correlation_id=cid, subject="", ts=ts,
                error=f"unparseable target address: {envelope.to!r}",
            )
        fleet, _agent = parsed
        url = self._config.peer_url(fleet)
        if not url:
            return SendResult(
                sent=False, correlation_id=cid, subject="", ts=ts,
                error=f"no ferry peer URL configured for fleet {fleet!r}",
            )
        if not self._config.shared_secret:
            return SendResult(
                sent=False, correlation_id=cid, subject=url, ts=ts,
                error="ferry shared secret not configured",
            )

        wire = envelope_to_wire(envelope)
        req = urllib.request.Request(
            url,
            data=wire.encode("utf-8"),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._config.shared_secret}",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout_s) as resp:
                code = resp.getcode()
                resp.read()  # drain so the connection can be reused/closed
        except urllib.error.HTTPError as exc:
            # Peer reachable but rejected (401 auth, 403 ACL, 503 transient...).
            return SendResult(
                sent=False, correlation_id=cid, subject=url, ts=ts,
                error=f"peer returned HTTP {exc.code}",
            )
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            return SendResult(
                sent=False, correlation_id=cid, subject=url, ts=ts,
                error=f"ferry POST failed: {exc}",
            )

        if code != 200:
            return SendResult(
                sent=False, correlation_id=cid, subject=url, ts=ts,
                error=f"peer returned HTTP {code}",
            )
        return SendResult(sent=True, correlation_id=cid, subject=url, ts=ts, error=None)
