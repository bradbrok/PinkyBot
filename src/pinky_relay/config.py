"""Owner-provisioned relay identities; clear bearer values are never persisted."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path

FLEET = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*", re.ASCII)


def canonical_fleet(value: str) -> str:
    if not isinstance(value, str) or FLEET.fullmatch(value) is None:
        raise ValueError("invalid fleet identity")
    return value.lower()


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _invalid_constant(value):
    raise ValueError("invalid JSON constant")


def parse_json(raw: bytes | str, *, depth: int = 20):
    try:
        value = json.loads(raw, object_pairs_hook=_unique_object, parse_constant=_invalid_constant)
    except (ValueError, UnicodeError, RecursionError):
        raise ValueError("invalid JSON") from None
    pending = [(value, 0)]
    while pending:
        item, level = pending.pop()
        if isinstance(item, (dict, list)):
            if level >= depth:
                raise ValueError("JSON nesting limit exceeded")
            children = item.values() if isinstance(item, dict) else item
            pending.extend((child, level + 1) for child in children)
    return value


def read_private_file(path: str | Path, *, limit: int = 1024 * 1024) -> bytes:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                    or stat.S_IMODE(info.st_mode) != 0o600):
                raise ValueError("identity file must be owner-owned regular mode 0600")
            raw = stream.read(limit + 1)
            if len(raw) > limit:
                raise ValueError("identity file exceeds size limit")
            return raw
    except OSError:
        raise ValueError("identity file cannot be read safely") from None


@dataclass(frozen=True)
class RelayConfig:
    digests: dict[str, bytes] = field(repr=False)

    @classmethod
    def from_mapping(cls, mapping: dict) -> RelayConfig:
        if not isinstance(mapping, dict) or not mapping:
            raise ValueError("relay requires fleet credential digests")
        digests = {}
        for raw_name, value in mapping.items():
            name = canonical_fleet(raw_name)
            if name in digests:
                raise ValueError("case-alias fleet identities")
            if not isinstance(value, str) or re.fullmatch(r"[0-9a-fA-F]{64}", value) is None:
                raise ValueError("relay credential must be a SHA256 digest")
            digest = bytes.fromhex(value)
            if digest in digests.values():
                raise ValueError("relay credential digests must be distinct")
            digests[name] = digest
        return cls(digests)

    @classmethod
    def from_file(cls, path: str | Path) -> RelayConfig:
        return cls.from_mapping(parse_json(read_private_file(path)))

    def authenticate(self, bearer: str) -> str | None:
        try:
            candidate = hashlib.sha256(bearer.encode("utf-8")).digest()
        except UnicodeError:
            candidate = bytes(32)
            bearer = ""
        found = None
        for name, expected in self.digests.items():
            matched = hmac.compare_digest(candidate, expected)
            if matched and bearer:
                found = name
        return found
