"""Authentication helpers for the Pinky web UI and internal clients."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from typing import Any

SESSION_COOKIE_NAME = "pinky_session"
INTERNAL_AGENT_HEADER = "x-pinky-agent"
INTERNAL_TIMESTAMP_HEADER = "x-pinky-timestamp"
INTERNAL_SIGNATURE_HEADER = "x-pinky-signature"

_PASSWORD_SCHEME = "pbkdf2_sha256"
_PASSWORD_ITERATIONS = 600_000
_SESSION_TTL_SECONDS = 7 * 24 * 3600
_INTERNAL_TTL_SECONDS = 300


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode((data + padding).encode("ascii"))


def hash_password(password: str, *, salt: bytes | None = None, iterations: int = _PASSWORD_ITERATIONS) -> str:
    """Create a versioned PBKDF2 password hash."""
    if not password:
        raise ValueError("password is required")
    salt_bytes = salt or os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt_bytes, iterations)
    return "$".join((
        _PASSWORD_SCHEME,
        str(iterations),
        _b64encode(salt_bytes),
        _b64encode(digest),
    ))


def verify_password(password: str, stored_hash: str) -> bool:
    """Verify a password against the stored PBKDF2 hash."""
    if not password or not stored_hash:
        return False
    try:
        scheme, iterations_raw, salt_raw, digest_raw = stored_hash.split("$", 3)
        if scheme != _PASSWORD_SCHEME:
            return False
        iterations = int(iterations_raw)
        salt = _b64decode(salt_raw)
        expected = _b64decode(digest_raw)
    except Exception:
        return False
    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(actual, expected)


def _sign_bytes(secret: str, payload: bytes) -> str:
    digest = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).digest()
    return _b64encode(digest)


def create_session_cookie(secret: str, *, user: str = "admin", now: int | None = None) -> str:
    """Create a signed UI session cookie."""
    ts = int(now or time.time())
    payload = {
        "user": user,
        "iat": ts,
        "exp": ts + _SESSION_TTL_SECONDS,
    }
    payload_bytes = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    payload_b64 = _b64encode(payload_bytes)
    signature = _sign_bytes(secret, payload_b64.encode("ascii"))
    return f"{payload_b64}.{signature}"


def verify_session_cookie(secret: str, token: str) -> dict[str, Any] | None:
    """Validate and decode a signed UI session cookie."""
    if not secret or not token or "." not in token:
        return None
    try:
        payload_b64, signature = token.split(".", 1)
    except ValueError:
        return None
    expected = _sign_bytes(secret, payload_b64.encode("ascii"))
    if not hmac.compare_digest(signature, expected):
        return None
    try:
        payload = json.loads(_b64decode(payload_b64))
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    if int(payload.get("exp", 0) or 0) < int(time.time()):
        return None
    return payload


def resolve_signing_secret() -> str:
    """Return the secret a per-agent process should sign internal requests with.

    Prefers the agent's per-agent signing key (``PINKY_AGENT_KEY``, provisioned
    into per-agent environments at #623 increment 2) over the shared global
    ``PINKY_SESSION_SECRET``. During the dual-accept migration the daemon verifies
    a signature against EITHER key (see ``verify_internal_request``), so:

    - A process that has its per-agent key (tmux hooks, stdio MCP servers) signs
      with a non-forgeable identity.
    - A process that only has the global secret (e.g. the shared-SSE MCP server,
      whose per-request key binding lands in a later increment, or SDK-agent hooks
      inheriting daemon env) keeps working unchanged.

    Empty string when neither is set — callers already treat that as "no auth".
    """
    return (
        os.environ.get("PINKY_AGENT_KEY", "").strip()
        or os.environ.get("PINKY_SESSION_SECRET", "").strip()
    )


def build_internal_auth_headers(secret: str, *, agent_name: str, method: str, path: str, timestamp: int | None = None) -> dict[str, str]:
    """Build signed headers for local MCP-to-daemon requests."""
    if not secret or not agent_name:
        return {}
    ts = int(timestamp or time.time())
    normalized_path = path.split("?", 1)[0]
    payload = f"{agent_name}\n{method.upper()}\n{normalized_path}\n{ts}".encode("utf-8")
    return {
        INTERNAL_AGENT_HEADER: agent_name,
        INTERNAL_TIMESTAMP_HEADER: str(ts),
        INTERNAL_SIGNATURE_HEADER: _sign_bytes(secret, payload),
    }


def verify_internal_request(
    secret: str,
    *,
    agent_name: str,
    method: str,
    path: str,
    timestamp: str,
    signature: str,
    agent_key: str | None = None,
) -> bool:
    """Verify signed local MCP-to-daemon request headers.

    Dual-accept (#623 migration): the signature is accepted if it matches
    EITHER the agent's per-agent signing key (``agent_key``) OR the shared
    global ``secret``. Per-agent keys give each agent a non-forgeable identity;
    the global secret remains accepted until the cutover PR provisions
    per-agent keys into agent environments and drops global-secret acceptance.
    At least one of ``secret`` / ``agent_key`` must be present.
    """
    if not agent_name or not timestamp or not signature:
        return False
    if not secret and not agent_key:
        return False
    try:
        ts = int(timestamp)
    except Exception:
        return False
    if abs(int(time.time()) - ts) > _INTERNAL_TTL_SECONDS:
        return False
    normalized_path = path.split("?", 1)[0]
    payload = f"{agent_name}\n{method.upper()}\n{normalized_path}\n{ts}".encode("utf-8")
    # Accept a match against the per-agent key OR the global secret. Each
    # comparison is constant-time; we only short-circuit on a match.
    for candidate in (agent_key, secret):
        if candidate and hmac.compare_digest(signature, _sign_bytes(candidate, payload)):
            return True
    return False


def password_source(env_password: str, stored_hash: str) -> str:
    """Return the active password source."""
    if env_password:
        return "env"
    if stored_hash:
        return "settings"
    return "unset"


def is_browser_json_request(headers: Any) -> bool:
    """Best-effort detection for browser fetch/XHR requests."""
    if headers.get("origin") or headers.get("referer"):
        return True
    if headers.get("sec-fetch-mode") or headers.get("sec-fetch-site"):
        return True
    requested_with = headers.get("x-requested-with", "")
    return requested_with.lower() == "xmlhttprequest"
