"""Pinned, bounded relay ingestion with a durable marker before every handoff."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import time
from collections import Counter, deque

from pinky_daemon.ferry.inbound_server import envelope_from_wire
from pinky_daemon.ferry.outbound import parse_address
from pinky_daemon.ferry.relay_http import RelayHTTPClient
from pinky_daemon.ferry.relay_identity import load_identity
from pinky_federation.envelope import Envelope
from pinky_federation.errors import CryptoError
from pinky_federation.sealed_box import unseal
from pinky_relay.config import canonical_fleet, parse_json

MAX_BLOB = 256 * 1024
MAX_TTL = 604_800_000
_log = logging.getLogger(__name__)


class RelayReceiverError(RuntimeError):
    pass


class PoisonRowError(ValueError):
    """A fixed reason only; no untrusted value may enter diagnostics."""


def row_id(row):
    value = row.get("id") if isinstance(row, dict) else None
    return value if isinstance(value, str) and 1 <= len(value) <= 64 and value.isascii() else None


def _bound_fleet(address):
    parsed = parse_address(address)
    if parsed is None or not isinstance(address, str):
        raise PoisonRowError("invalid inner address")
    fleet, agent = parsed
    try:
        name = canonical_fleet(fleet)
    except ValueError:
        raise PoisonRowError("invalid inner address") from None
    if (fleet != name or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in agent)
            or "/" in agent or address not in (f"ferry://{name}/{agent}", f"{agent}@{name}")):
        raise PoisonRowError("noncanonical inner address")
    return name


def authenticate_row(row, settings, identity, now):
    """Steps a-h run completely before a claim can hold authenticated metadata."""
    if row_id(row) is None or not isinstance(row.get("blob_b64"), str):
        raise PoisonRowError("invalid row shape")
    encoded = row["blob_b64"]
    if len(encoded) > 4 * ((MAX_BLOB + 2) // 3):
        raise PoisonRowError("sealed blob exceeds limit")
    try:
        blob = base64.b64decode(encoded, validate=True)
    except (ValueError, UnicodeError):
        raise PoisonRowError("invalid blob encoding") from None
    if len(blob) > MAX_BLOB:
        raise PoisonRowError("sealed blob exceeds limit")
    try:
        outer = Envelope.from_bytes(blob)
    except (ValueError, CryptoError):
        raise PoisonRowError("invalid outer envelope") from None
    peer = next((peer for peer in settings.peers.values()
                 if peer.ok and bytes.fromhex(peer.expected) == outer.sender_fingerprint), None)
    if peer is None:
        raise PoisonRowError("unpinned sender")
    if outer.recipient_fingerprint != identity.fingerprint:
        raise PoisonRowError("wrong recipient fingerprint")
    try:
        plaintext = unseal(outer, recipient_encryption=identity.encryption,
                           sender_signing_public=peer.signing)
    except CryptoError:
        raise PoisonRowError("sealed authentication failed") from None
    try:
        data = parse_json(plaintext, depth=20)
        if not isinstance(data, dict):
            raise ValueError
        ident, stamp, ttl = data.get("id"), data.get("ts"), data.get("ttl_ms")
        if (not isinstance(ident, str) or not 1 <= len(ident) <= 128
                or any(not 32 <= ord(c) <= 126 for c in ident)
                or type(stamp) is not int or not -(2**63) <= stamp < 2**63
                or ttl is not None and (type(ttl) is not int or not 1 <= ttl <= MAX_TTL)):
            raise ValueError
        # A present null TTL is not the same contract as an absent optional TTL.
        if "ttl_ms" in data and ttl is None:
            raise ValueError
        if (_bound_fleet(data.get("from")) != peer.fleet
                or _bound_fleet(data.get("to")) != settings.own_fleet
                or row.get("sender_fleet") != peer.fleet):
            raise PoisonRowError("sender or recipient fleet binding failed")
        expires = stamp + (ttl if ttl is not None else MAX_TTL)
        if stamp > now + 600_000 or now > expires:
            raise PoisonRowError("message is outside freshness window")
        message = envelope_from_wire(data)
    except PoisonRowError:
        raise
    except (ValueError, TypeError, OverflowError, UnicodeError):
        raise PoisonRowError("invalid inner envelope") from None
    return message, outer.sender_fingerprint, hashlib.sha256(plaintext).digest(), expires


class RelayPacer:
    """Novelty never refills the independent poll or row budget."""

    def __init__(self, *, monotonic=time.monotonic, sleep=asyncio.sleep):
        self.monotonic, self.sleep = monotonic, sleep
        self.updated = monotonic()
        self.tokens = 5.0
        self.rows = deque()
        self.previous = set()
        self.backoff = 0

    async def before_poll(self):
        while True:
            now = self.monotonic()
            self.tokens = min(5.0, self.tokens + max(0, now - self.updated) * 0.5)
            self.updated = now
            if self.tokens >= 1:
                self.tokens -= 1
                return
            await self.sleep((1 - self.tokens) / 0.5)

    async def before_row(self):
        while True:
            now = self.monotonic()
            while self.rows and self.rows[0] <= now - 60:
                self.rows.popleft()
            if len(self.rows) < 600:
                self.rows.append(now)
                return
            await self.sleep(max(0.001, self.rows[0] + 60 - now))

    async def after_page(self, rows, elapsed, wait):
        current = {ident for row in rows if (ident := row_id(row)) is not None}
        repeated = not current or current.issubset(self.previous)
        self.previous = current
        self.backoff = min(60, self.backoff * 2 or 1) if repeated and elapsed < wait else 0
        if self.backoff:
            await self.sleep(self.backoff)


class RelayReceiver:
    def __init__(self, settings, replay, host, *, client_factory=RelayHTTPClient, pacer=None):
        self.settings, self.replay, self.host = settings, replay, host
        self.client_factory = client_factory
        self.pacer = pacer or RelayPacer()
        self.task = None
        self.closing = False
        self.state = "stopped"
        self.last_error = ""
        self.counters = Counter()
        self._logged = {}

    def _reason(self, reason):
        self.counters[reason] += 1
        now = time.monotonic()
        if now - self._logged.get(reason, -60) >= 60:
            _log.warning("relay receiver reason=%s count=%d", reason, self.counters[reason])
            self._logged[reason] = now

    def _identity(self):
        if not self.settings.ready:
            raise RelayReceiverError("relay configuration is blocked")
        try:
            identity = load_identity(self.settings.key_file, own_fleet=self.settings.own_fleet)
        except ValueError:
            raise RelayReceiverError("relay identity is unavailable or unsafe") from None
        if identity.fingerprint != self.settings.identity.fingerprint:
            raise RelayReceiverError("relay identity changed; explicit reconfiguration required")
        return identity

    def _clock_ready(self):
        clock = self.replay.clock()
        if clock["state"] == "clock behind floor":
            self.state = clock["state"]
            return False
        if self.state == "clock behind floor":
            self.state = "running"
        return not self.closing

    async def process_row(self, row):
        # Local authority failures precede poison classification and never ack.
        identity = self._identity()
        if not self._clock_ready():
            return False
        try:
            message, sender, digest, expires = authenticate_row(
                row, self.settings, identity, self.replay.wall_ms(),
            )
        except PoisonRowError as exc:
            self._reason(str(exc))
            return row_id(row) is not None
        key = (sender, message.id)
        acquired = self.replay.claim(sender, message.id, digest, row["id"], expires, message.ts)
        if acquired in {"terminal", "equivocation", "expired"}:
            self._reason("equivocation" if acquired == "equivocation" else "replay or expired")
            return True
        if acquired != "claimed":
            self._reason("replay quota reached" if acquired == "quota" else "claim deferred")
            return False
        if not self.replay.handoff(key):
            return False
        terminal = "UNKNOWN"
        try:
            if not self._clock_ready():
                return False
            result = await self.host.deliver(message)
            if result.status in {"delivered", "rejected", "queued"}:
                terminal = result.status.upper()
            elif result.status == "transient_failure" and result.pre_dispatch is True:
                terminal = self.replay.retry(key)
                self._reason("pre-dispatch retry" if terminal == "RETRY" else "retry limit reached")
                return terminal == "DROPPED"
        except asyncio.CancelledError:
            raise
        except Exception:
            self._reason("ambiguous handoff")
        finally:
            # A cancellation or local failure after the durable marker can
            # never cause another handoff, even if this terminal write fails.
            if terminal == "UNKNOWN":
                self._finish_unknown(key)
        if terminal != "UNKNOWN":
            try:
                self.replay.finish(key, terminal)
            except Exception:
                self._finish_unknown(key)
        return True

    def _finish_unknown(self, key):
        try:
            self.replay.finish(key, "UNKNOWN")
        except Exception:
            # HANDING_OFF is already durable; recovery and replay treat it as
            # UNKNOWN. The error must be loud, never an implicit retry.
            self.last_error = "replay terminal write failed"
        self._reason("unknown handoff")

    async def run_once(self, client, *, wait=25):
        self._identity()
        if not self._clock_ready():
            await self.pacer.sleep(1)
            return
        self.replay.prune()
        await self.pacer.before_poll()
        started = self.pacer.monotonic()
        rows = await client.poll(wait)
        returned = {ident for row in rows if (ident := row_id(row)) is not None}
        for row in rows:
            await self.pacer.before_row()
            if await self.process_row(row) and self._clock_ready():
                ident = row_id(row)
                if ident in returned:
                    await client.ack([ident])
        await self.pacer.after_page(rows, self.pacer.monotonic() - started, wait)

    async def _run(self):
        self.state = "running"
        try:
            async with self.client_factory(self.settings) as client:
                while not self.closing:
                    try:
                        await self.run_once(client)
                        self.last_error = ""
                        if self.state != "clock behind floor":
                            self.state = "running"
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        self.last_error = f"relay receiver paused ({type(exc).__name__})"
                        self.state = "paused"
                        self._reason(self.last_error)
                        await self.pacer.sleep(1)
        finally:
            self.state = "stopped"

    def start(self):
        if self.task is not None or self.closing:
            raise RelayReceiverError("relay receiver already started or closed")
        self.task = asyncio.create_task(self._run(), name="ferry-relay-receiver")

    async def close(self):
        self.closing = True
        if self.task is not None:
            self.task.cancel()
            done, pending = await asyncio.wait({self.task}, timeout=4.5)
            if pending:
                raise RelayReceiverError("relay receiver shutdown deadline exceeded")
            await asyncio.gather(*done, return_exceptions=True)
        self.replay.close()
        self.state = "stopped"

    def diagnostics(self):
        return {**self.settings.diagnostics(), "poller": self.state,
                "last_error": self.last_error, "counters": dict(self.counters),
                "replay": self.replay.snapshot(), "clock": self.replay.clock()["state"]}
