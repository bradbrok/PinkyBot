"""Explicitly provisioned relay keys; no discovery, TOFU or automatic rotation."""

from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from pinky_federation.fingerprint import fingerprint
from pinky_federation.keys import EncryptionKeyPair, SigningKeyPair
from pinky_relay.config import canonical_fleet, parse_json, read_private_file


@dataclass(frozen=True)
class RelayIdentity:
    fleet: str
    signing: SigningKeyPair = field(repr=False)
    encryption: EncryptionKeyPair = field(repr=False)

    @property
    def fingerprint(self) -> bytes:
        return fingerprint(self.fleet, self.signing.public_key, self.encryption.public_key)

    def public(self):
        return {"fleet": self.fleet,
                "signing_pk": base64.b64encode(self.signing.public_key.to_bytes()).decode("ascii"),
                "encryption_pk": base64.b64encode(self.encryption.public_key.to_bytes()).decode("ascii"),
                "fingerprint": self.fingerprint.hex()}


def load_identity(path, *, own_fleet=None):
    try:
        data = parse_json(read_private_file(path, limit=16 * 1024))
        if (not isinstance(data, dict) or set(data) != {"v", "fleet", "signing_sk", "encryption_sk"}
                or type(data["v"]) is not int or data["v"] != 1):
            raise ValueError
        name = canonical_fleet(data["fleet"])
        if data["fleet"] != name or own_fleet is not None and name != canonical_fleet(own_fleet):
            raise ValueError
        signing = base64.b64decode(data["signing_sk"], validate=True)
        encryption = base64.b64decode(data["encryption_sk"], validate=True)
        return RelayIdentity(name, SigningKeyPair.from_seed(signing),
                             EncryptionKeyPair.from_private_bytes(encryption))
    except (ValueError, TypeError, KeyError, OSError, UnicodeError):
        raise ValueError("relay identity is unavailable or unsafe") from None


def generate_identity(path, fleet):
    name = canonical_fleet(fleet)
    identity = RelayIdentity(name, SigningKeyPair.generate(), EncryptionKeyPair.generate())
    try:
        identity.fingerprint
    except (OverflowError, UnicodeError):
        raise ValueError("relay fleet identity exceeds fingerprint format") from None
    data = {"v": 1, "fleet": name,
            "signing_sk": base64.b64encode(identity.signing.seed_bytes_insecure()).decode("ascii"),
            "encryption_sk": base64.b64encode(identity.encryption.private_bytes_insecure()).decode("ascii")}
    path = Path(path)
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except OSError:
        raise ValueError("relay identity already exists or cannot be created safely") from None
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(data, stream, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except OSError:
        raise ValueError("relay identity write failed") from None
    return identity
