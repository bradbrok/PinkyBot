"""P1 authentication boundary for the private RingCentral bridge.

The bridge uses deployment-static secret material, an explicitly configured
instance pin, and a 300-second signature TTL. SMS writes remain bounded by the
bridge's compliance and audit gates. Instance rotation is a coordinated config
update on the bridge and MCP; it does not automatically revoke orphaned MCP
processes. True per-spawn revocation requires daemon integration outside #447.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import time
import urllib.parse
import uuid
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass

INTERNAL_AGENT_HEADER = "x-pinky-agent"
INTERNAL_TIMESTAMP_HEADER = "x-pinky-timestamp"
INTERNAL_SIGNATURE_HEADER = "x-pinky-signature"
INTERNAL_INSTANCE_HEADER = "x-pinky-instance"
INTERNAL_CONTENT_DIGEST_HEADER = "x-pinky-content-sha256"
INTERNAL_REQUEST_ID_HEADER = "x-pinky-request-id"

AUTH_TTL_SECONDS = 300
DERIVATION_DOMAIN = "ringcentral"
_AGENT_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
_CONTENT_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")


@dataclass(frozen=True)
class AgentIdentity:
    """Cryptographically verified bridge caller."""

    name: str
    instance_id: str
    request_id: str


def canonical_target(path: str) -> str:
    """Return a path plus deterministically ordered query string."""

    parsed = urllib.parse.urlsplit(path)
    query = urllib.parse.urlencode(
        sorted(
            urllib.parse.parse_qsl(
                parsed.query,
                keep_blank_values=True,
            ),
            key=lambda item: item[0],
        ),
        doseq=True,
    )
    return f"{parsed.path}?{query}" if query else parsed.path


def _signature_payload(
    *,
    agent_name: str,
    method: str,
    path: str,
    timestamp: int,
    content_digest: str,
    request_id: str,
) -> bytes:
    return (
        f"{agent_name}\n{method.upper()}\n{canonical_target(path)}\n"
        f"{timestamp}\n{content_digest}\n{request_id}"
    ).encode()


def derive_agent_key(master_secret: str, agent_name: str, instance_id: str) -> str:
    """Derive the deployment key for one agent and configured instance pin."""

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
    body: bytes = b"",
    request_id: str = "",
) -> dict[str, str]:
    """Build Pinky-style HMAC headers without putting a secret on the wire."""

    if not secret or not agent_name:
        return {}
    ts = int(timestamp if timestamp is not None else time.time())
    signed_request_id = request_id or uuid.uuid4().hex
    if not _REQUEST_ID_RE.fullmatch(signed_request_id):
        raise ValueError("request_id has an invalid shape")
    content_digest = hashlib.sha256(body).hexdigest()
    payload = _signature_payload(
        agent_name=agent_name,
        method=method,
        path=path,
        timestamp=ts,
        content_digest=content_digest,
        request_id=signed_request_id,
    )
    headers = {
        INTERNAL_AGENT_HEADER: agent_name,
        INTERNAL_TIMESTAMP_HEADER: str(ts),
        INTERNAL_CONTENT_DIGEST_HEADER: content_digest,
        INTERNAL_REQUEST_ID_HEADER: signed_request_id,
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
    content_digest: str,
    request_id: str,
    body: bytes = b"",
    now: int | None = None,
) -> bool:
    """Verify one query/body-bound request within the signature TTL."""

    if (
        not secret
        or not agent_name
        or not _AGENT_NAME_RE.fullmatch(agent_name)
        or not timestamp
        or not signature
        or not signature.isascii()
        or not _CONTENT_DIGEST_RE.fullmatch(content_digest)
        or not _REQUEST_ID_RE.fullmatch(request_id)
    ):
        return False
    try:
        ts = int(timestamp)
    except (TypeError, ValueError):
        return False
    current = int(time.time() if now is None else now)
    if abs(current - ts) > AUTH_TTL_SECONDS:
        return False
    actual_content_digest = hashlib.sha256(body).hexdigest()
    if not hmac.compare_digest(content_digest, actual_content_digest):
        return False
    try:
        payload = _signature_payload(
            agent_name=agent_name,
            method=method,
            path=path,
            timestamp=ts,
            content_digest=content_digest,
            request_id=request_id,
        )
    except ValueError:
        return False
    expected = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature, expected)


class DerivedKeyAuthenticator:
    """Verify callers against one deployment-configured instance pin."""

    def __init__(
        self,
        master_secret: str,
        *,
        current_instance_id: str,
        allowed_agents: Collection[str] = ("geordi",),
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._master_secret = master_secret.strip()
        self._current_instance_id = current_instance_id.strip()
        self._allowed_agents = frozenset(allowed_agents)
        self._clock = clock

    @property
    def configured(self) -> bool:
        return bool(
            self._master_secret
            and self._allowed_agents
            and self._current_instance_id
            and len(self._current_instance_id) <= 128
        )

    def authenticate(
        self,
        *,
        method: str,
        path: str,
        headers: Mapping[str, str],
        body: bytes = b"",
    ) -> AgentIdentity | None:
        """Return a verified identity, or ``None`` without revealing why."""

        if not self.configured:
            return None
        lowered = {key.lower(): value for key, value in headers.items()}
        agent_name = lowered.get(INTERNAL_AGENT_HEADER, "").strip()
        instance_id = lowered.get(INTERNAL_INSTANCE_HEADER, "").strip()
        timestamp = lowered.get(INTERNAL_TIMESTAMP_HEADER, "")
        signature = lowered.get(INTERNAL_SIGNATURE_HEADER, "")
        content_digest = lowered.get(INTERNAL_CONTENT_DIGEST_HEADER, "")
        request_id = lowered.get(INTERNAL_REQUEST_ID_HEADER, "")
        if (
            agent_name not in self._allowed_agents
            or not instance_id
            or len(instance_id) > 128
            or instance_id != self._current_instance_id
        ):
            return None
        try:
            key = derive_agent_key(
                self._master_secret,
                agent_name,
                self._current_instance_id,
            )
        except ValueError:
            return None
        if not verify_signed_headers(
            key,
            agent_name=agent_name,
            method=method,
            path=path,
            timestamp=timestamp,
            signature=signature,
            content_digest=content_digest,
            request_id=request_id,
            body=body,
            now=int(self._clock()),
        ):
            return None
        return AgentIdentity(
            name=agent_name,
            instance_id=self._current_instance_id,
            request_id=request_id,
        )
