"""Derived-key signed authentication for the private RingCentral bridge."""

from __future__ import annotations

import hashlib
import hmac
import re
import time
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass

INTERNAL_AGENT_HEADER = "x-pinky-agent"
INTERNAL_TIMESTAMP_HEADER = "x-pinky-timestamp"
INTERNAL_SIGNATURE_HEADER = "x-pinky-signature"
INTERNAL_INSTANCE_HEADER = "x-pinky-instance"

AUTH_TTL_SECONDS = 300
DERIVATION_DOMAIN = "ringcentral"
_AGENT_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")


@dataclass(frozen=True)
class AgentIdentity:
    """Cryptographically verified bridge caller."""

    name: str
    instance_id: str


def normalize_path(path: str) -> str:
    """Return the path component covered by the signature."""

    return path.split("?", 1)[0]


def derive_agent_key(master_secret: str, agent_name: str, instance_id: str) -> str:
    """Derive a key bound to one agent and one daemon generation."""

    if not master_secret or not agent_name or not instance_id:
        raise ValueError("master_secret, agent_name, and instance_id are required")
    if not _AGENT_NAME_RE.fullmatch(agent_name):
        raise ValueError("invalid agent_name")
    payload = f"{DERIVATION_DOMAIN}|{agent_name}|{instance_id}".encode()
    return hmac.new(master_secret.encode(), payload, hashlib.sha256).hexdigest()


def build_signed_headers(
    secret: str,
    *,
    agent_name: str,
    method: str,
    path: str,
    timestamp: int | None = None,
    instance_id: str = "",
) -> dict[str, str]:
    """Build Pinky-style HMAC headers without putting a secret on the wire."""

    if not secret or not agent_name:
        return {}
    ts = int(timestamp if timestamp is not None else time.time())
    payload = (
        f"{agent_name}\n{method.upper()}\n{normalize_path(path)}\n{ts}".encode()
    )
    headers = {
        INTERNAL_AGENT_HEADER: agent_name,
        INTERNAL_TIMESTAMP_HEADER: str(ts),
        INTERNAL_SIGNATURE_HEADER: hmac.new(
            secret.encode(), payload, hashlib.sha256
        ).hexdigest(),
    }
    if instance_id:
        headers[INTERNAL_INSTANCE_HEADER] = instance_id
    return headers


def verify_signed_headers(
    secret: str,
    *,
    agent_name: str,
    method: str,
    path: str,
    timestamp: str,
    signature: str,
    now: int | None = None,
) -> bool:
    """Verify one signed request within the bounded replay window."""

    if (
        not secret
        or not agent_name
        or not _AGENT_NAME_RE.fullmatch(agent_name)
        or not timestamp
        or not signature
        or not signature.isascii()
    ):
        return False
    try:
        ts = int(timestamp)
    except (TypeError, ValueError):
        return False
    current = int(time.time() if now is None else now)
    if abs(current - ts) > AUTH_TTL_SECONDS:
        return False
    payload = (
        f"{agent_name}\n{method.upper()}\n{normalize_path(path)}\n{ts}".encode()
    )
    expected = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature, expected)


class DerivedKeyAuthenticator:
    """Verify callers using keys independently derived from the master secret."""

    def __init__(
        self,
        master_secret: str,
        *,
        allowed_agents: Collection[str] = ("geordi",),
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._master_secret = master_secret.strip()
        self._allowed_agents = frozenset(allowed_agents)
        self._clock = clock

    @property
    def configured(self) -> bool:
        return bool(self._master_secret and self._allowed_agents)

    def authenticate(
        self,
        *,
        method: str,
        path: str,
        headers: Mapping[str, str],
    ) -> AgentIdentity | None:
        """Return a verified identity, or ``None`` without revealing why."""

        if not self.configured:
            return None
        lowered = {key.lower(): value for key, value in headers.items()}
        agent_name = lowered.get(INTERNAL_AGENT_HEADER, "").strip()
        instance_id = lowered.get(INTERNAL_INSTANCE_HEADER, "").strip()
        timestamp = lowered.get(INTERNAL_TIMESTAMP_HEADER, "")
        signature = lowered.get(INTERNAL_SIGNATURE_HEADER, "")
        if (
            agent_name not in self._allowed_agents
            or not instance_id
            or len(instance_id) > 128
        ):
            return None
        try:
            key = derive_agent_key(self._master_secret, agent_name, instance_id)
        except ValueError:
            return None
        if not verify_signed_headers(
            key,
            agent_name=agent_name,
            method=method,
            path=path,
            timestamp=timestamp,
            signature=signature,
            now=int(self._clock()),
        ):
            return None
        return AgentIdentity(name=agent_name, instance_id=instance_id)
