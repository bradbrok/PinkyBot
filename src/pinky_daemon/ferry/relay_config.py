"""Relay route reservations survive every credential or pin configuration error."""

from __future__ import annotations

import base64
import hmac
import ipaddress
import json
import logging
import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from pinky_daemon.ferry.relay_identity import RelayIdentity, load_identity
from pinky_federation.fingerprint import fingerprint
from pinky_federation.keys import EncryptionPublicKey, SigningPublicKey
from pinky_relay.config import canonical_fleet, parse_json

NAMES = ("URL", "TOKEN", "KEY_FILE", "PEERS")
_log = logging.getLogger(__name__)


def validate_url(value):
    if not value or any(c.isspace() or ord(c) < 32 for c in value):
        raise ValueError("relay URL is invalid")
    try:
        parsed = urlsplit(value)
        if (parsed.scheme not in ("http", "https") or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment or parsed.port == 0):
            raise ValueError
        if parsed.scheme == "http" and not ipaddress.ip_address(parsed.hostname).is_loopback:
            raise ValueError
    except ValueError:
        raise ValueError("relay URL requires HTTPS or a literal loopback host") from None
    return value.rstrip("/") + "/"


@dataclass(frozen=True)
class RelayPeer:
    fleet: str
    expected: str = ""
    observed: str = ""
    reason: str = ""
    signing: SigningPublicKey | None = field(default=None, repr=False)
    encryption: EncryptionPublicKey | None = field(default=None, repr=False)

    @property
    def ok(self):
        return not self.reason and self.signing is not None and self.encryption is not None


def _peer(name, data):
    expected = observed = ""
    try:
        fleet = canonical_fleet(name)
        if not isinstance(data, dict):
            raise ValueError
        pin = data.get("fingerprint")
        if not isinstance(pin, str) or re.fullmatch(r"[0-9a-fA-F]{32}", pin) is None:
            raise ValueError
        expected = pin.lower()
        signing = SigningPublicKey.from_bytes(base64.b64decode(data["signing_pk"], validate=True))
        encryption = EncryptionPublicKey.from_bytes(base64.b64decode(data["encryption_pk"], validate=True))
        observed = fingerprint(fleet, signing, encryption).hex()
        if not hmac.compare_digest(expected, observed):
            return RelayPeer(fleet, expected, observed, "fingerprint pin mismatch")
        return RelayPeer(fleet, expected, observed, signing=signing, encryption=encryption)
    except (ValueError, TypeError, KeyError, OverflowError):
        return RelayPeer(name, expected, observed, "invalid peer pin")


@dataclass(frozen=True)
class RelaySettings:
    own_fleet: str
    reserved: frozenset[str]
    peers: dict[str, RelayPeer]
    url: str = field(default="", repr=False)
    token: str = field(default="", repr=False)
    key_file: str = field(default="", repr=False)
    identity: RelayIdentity | None = field(default=None, repr=False)
    error: str = ""
    block_all: bool = False

    @classmethod
    def from_env(cls, env, own_fleet):
        values = {name: env.get("PINKYBOT_FERRY_RELAY_" + name, "") for name in NAMES}
        if not any(values.values()):
            return None
        reserved, peers = set(), {}
        error = ""
        block_all = False
        try:
            raw_peers = parse_json(values["PEERS"] or "{}")
            if not isinstance(raw_peers, dict):
                raise ValueError
            for raw_name, data in raw_peers.items():
                folded = raw_name.casefold()
                if folded in reserved:
                    error = "case-alias relay fleet identities"
                reserved.add(folded)
                try:
                    if canonical_fleet(raw_name) != raw_name:
                        raise ValueError
                except ValueError:
                    error = error or "relay peer fleet must be canonical"
                    peers[folded] = RelayPeer(folded, reason="invalid fleet identity")
                else:
                    peers[folded] = _peer(folded, data)
        except (ValueError, TypeError):
            error = "invalid relay peer map"
            block_all = True
        url = ""
        token = values["TOKEN"]
        key_file = values["KEY_FILE"]
        identity = None
        try:
            own = canonical_fleet(own_fleet)
        except ValueError:
            own = ""
            error = error or "invalid own relay fleet"
        else:
            if own != own_fleet:
                error = error or "own relay fleet must be canonical"
        try:
            url = validate_url(values["URL"])
        except ValueError:
            error = error or "relay URL is missing or invalid"
        if not isinstance(token, str) or re.fullmatch(r"[\x21-\x7e]{32,}", token) is None:
            error = error or "relay credential is missing or invalid"
        if not key_file:
            error = error or "relay identity is missing"
        else:
            try:
                identity = load_identity(key_file, own_fleet=own)
            except ValueError:
                error = error or "relay identity is unavailable or unsafe"
        if error:
            _log.error("relay configuration blocked: %s", error)
        return cls(own, frozenset(reserved), peers, url, token, key_file, identity, error, block_all)

    @property
    def ready(self):
        return not self.error and self.identity is not None

    def is_reserved(self, name):
        return self.block_all or name.casefold() in self.reserved

    def diagnostics(self):
        snapshot = {"state": "ready" if self.ready else "blocked", "reason": self.error,
                    "peers": {name: {"state": "ok" if peer.ok else "blocked",
                                      "reason": peer.reason, "expected": peer.expected,
                                      "observed": peer.observed}
                              for name, peer in self.peers.items()}}
        # A malicious relay or an operator typo must not echo either private value.
        raw = json.dumps(snapshot)
        for private in (self.token, self.key_file):
            if private:
                raw = raw.replace(json.dumps(private)[1:-1], "<redacted>")
        return json.loads(raw)
