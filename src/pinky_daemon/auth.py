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


def resolve_request_signing_secret(agent_name, signing_key_resolver=None) -> str:
    """Per-request signing secret for the shared MCP servers (#623 increment 3).

    Shared-SSE serves many agents from one process, so the per-agent key can't
    come from the process env. The daemon passes ``signing_key_resolver``
    (``agent_name -> key | None``); this prefers that resolved key, else falls
    back to ``resolve_signing_secret()`` (the env-based PINKY_AGENT_KEY / global
    secret used by stdio-mode and hooks). Resolver errors degrade to the
    fallback — never raise into the request path. The daemon dual-accepts, so a
    None resolution keeps working via the global secret.
    """
    if signing_key_resolver:
        try:
            key = signing_key_resolver(agent_name)
            if key:
                return key
        except Exception:
            pass
        # Shared-mode fallback: GLOBAL secret only. A resolver means we're the
        # multi-agent shared process — a process-level PINKY_AGENT_KEY (if one
        # ever leaked into this env) would be a single WRONG identity for every
        # agent, so never honor it here. Use the global secret (dual-accept).
        return os.environ.get("PINKY_SESSION_SECRET", "").strip()
    return resolve_signing_secret()


def make_db_signing_key_resolver(db_path: str):
    """Build a request-time signing-key resolver backed by the agents DB (#641).

    Returns ``agent_name -> current signing key | None``. Each call opens the
    agents DB **read-only** and SELECTs the agent's current per-agent key, so a
    stale ``PINKY_AGENT_KEY`` captured into a long-lived stdio MCP server's env
    at spawn can never shadow the key the DB actually holds. That env-vs-DB
    desync is the root cause of the post-daemon-restart 401 lockout (#641):
    ``.mcp.json`` bakes the key statically, ``resolve_signing_secret`` prefers
    it, and the daemon rejects once it drifts.

    Pair with :func:`resolve_request_signing_secret`: when a resolver is present
    it ignores process-env ``PINKY_AGENT_KEY`` entirely and falls back to the
    GLOBAL secret only — so the stale env key is dead, exactly as in shared-SSE
    mode. The daemon stays the policy authority (dual-accept for non-isolated,
    fail-closed for isolated per #640); this only changes what the *signer*
    presents, never what the verifier accepts.

    Fails soft: any error (missing file, missing table, locked DB) returns
    None, so signing degrades to the global secret rather than raising into the
    request path.

    NOTE (#149 inc3c): a unix_user-isolated tenant must NOT read the fleet DB —
    it holds every agent's signing key. This DB-backed resolver is the
    local-mode repair only; an isolated tenant gets a single-agent,
    provisioner-placed key source swapped in behind this same resolver seam.
    """
    import re
    import sqlite3

    # Same allowlist the registry/API enforce on agent names. Parameter binding
    # already neutralizes SQL injection, but validating here keeps the resolver
    # inside the agent-name trust boundary (@murzik #644 hardening): a malformed
    # name can never reach the query and simply resolves to None → global fallback.
    _name_re = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")

    def _resolve(agent_name: str) -> str | None:
        if not agent_name or not db_path or not _name_re.fullmatch(agent_name):
            return None
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
            try:
                row = conn.execute(
                    "SELECT signing_key FROM agent_signing_keys WHERE agent_name=?",
                    (agent_name,),
                ).fetchone()
            finally:
                conn.close()
            return row[0] if row and row[0] else None
        except Exception:
            return None

    return _resolve


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
    allow_global_secret: bool = True,
) -> bool:
    """Verify signed local MCP-to-daemon request headers.

    Dual-accept (#623 migration): the signature is accepted if it matches
    EITHER the agent's per-agent signing key (``agent_key``) OR the shared
    global ``secret``. Per-agent keys give each agent a non-forgeable identity;
    the global secret remains accepted until the cutover PR provisions
    per-agent keys into agent environments and drops global-secret acceptance.
    At least one of ``secret`` / ``agent_key`` must be present.

    #149 phase-3 inc2: ``allow_global_secret=False`` removes the global secret
    from the accepted candidates, so the signature must match the per-agent
    ``agent_key``. Used for ISOLATED callers — the global secret is accepted for
    EVERY agent name, so honoring it for an isolated tenant would stay a forgery
    path even after the env gate (#639) stops handing it out. With it False a
    caller that has no per-agent key cannot authenticate at all (fail closed).
    """
    if not agent_name or not timestamp or not signature:
        return False
    usable_secret = secret if allow_global_secret else ""
    if not usable_secret and not agent_key:
        return False
    try:
        ts = int(timestamp)
    except Exception:
        return False
    if abs(int(time.time()) - ts) > _INTERNAL_TTL_SECONDS:
        return False
    normalized_path = path.split("?", 1)[0]
    payload = f"{agent_name}\n{method.upper()}\n{normalized_path}\n{ts}".encode("utf-8")
    # Accept a match against the per-agent key OR (when allowed) the global
    # secret. Each comparison is constant-time; we only short-circuit on a match.
    for candidate in (agent_key, usable_secret):
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
