"""Relay reservations select one sealed route and never fall back."""

from __future__ import annotations

import asyncio

from pinky_daemon.ferry.outbound import SendResult, envelope_to_wire, parse_address
from pinky_daemon.ferry.relay_http import RelayClientError, RelayHTTPClient
from pinky_federation.sealed_box import seal
from pinky_relay.config import canonical_fleet

MAX_SEALED = 256 * 1024


class RelayMeshSender:
    def __init__(self, settings, *, client_factory=RelayHTTPClient):
        self.settings = settings
        self.client_factory = client_factory

    async def send_async(self, envelope, fleet):
        peer = self.settings.peers.get(fleet)
        if not self.settings.ready or peer is None or not peer.ok:
            reason = self.settings.error or (peer.reason if peer is not None else "peer pin missing")
            raise RelayClientError("relay route unavailable: " + reason)
        sender = parse_address(envelope.from_)
        if sender is None or canonical_fleet(sender[0]) != self.settings.own_fleet:
            raise RelayClientError("relay sender does not match local identity")
        plaintext = envelope_to_wire(envelope).encode("utf-8")
        if len(plaintext) > MAX_SEALED:
            raise RelayClientError("relay plaintext exceeds size limit")
        identity = self.settings.identity
        sealed = seal(plaintext, sender_signing=identity.signing,
                      sender_fingerprint=identity.fingerprint,
                      recipient_encryption=peer.encryption,
                      recipient_fingerprint=bytes.fromhex(peer.expected)).to_bytes()
        if len(sealed) > MAX_SEALED:
            raise RelayClientError("sealed relay message exceeds size limit")
        async with self.client_factory(self.settings) as client:
            await client.send(fleet, sealed)

    def send(self, envelope):
        result = dict(sent=False, correlation_id=envelope.correlation_id, subject="relay",
                      ts=envelope.ts)
        try:
            parsed = parse_address(envelope.to)
            if parsed is None:
                raise RelayClientError("relay target is invalid")
            fleet = canonical_fleet(parsed[0])
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                pass
            else:
                raise RelayClientError("relay send requires a worker context")
            asyncio.run(self.send_async(envelope, fleet))
        except RelayClientError as exc:
            return SendResult(**result, error=str(exc))
        except Exception as exc:
            # Only the class can cross back into API details or audit records.
            return SendResult(**result, error=f"relay send failed ({type(exc).__name__})")
        result["sent"] = True
        return SendResult(**result)


class RoutingMeshSender:
    def __init__(self, settings, legacy):
        self.settings, self.legacy = settings, legacy
        self.relay = RelayMeshSender(settings)

    def send(self, envelope):
        parsed = parse_address(envelope.to)
        if parsed is not None and self.settings.is_reserved(parsed[0]):
            return self.relay.send(envelope)
        if self.settings.block_all:
            return SendResult(False, envelope.correlation_id, "relay", envelope.ts,
                              "relay peer map is invalid")
        return self.legacy.send(envelope)

    @property
    def configured(self):
        return self.legacy.configured or self.settings.ready

    def diagnostics(self):
        return {**self.legacy.diagnostics(), "transport": "per-destination",
                "relay": self.settings.diagnostics()}
