"""Agent-scoped token storage for QBO OAuth credentials (spec §4).

Persists per-agent QBO settings via the registry's
``set_agent_setting(agent, key, value)`` / ``get_agent_setting(agent, key,
default="")`` callables, so a stray global ``QBO_REFRESH_TOKEN`` can never
become a fleet credential. Keys are namespaced ``agent:<agent>:qbo_*`` by the
registry's accessors (which prefix ``agent:<agent>:`` for us).

Secrets (``refresh_token`` + ``client_secret``) are wrapped via ``crypto.py``
*before* write and unwrapped on read — ``system_settings`` is plaintext on
disk. Non-secret fields (``client_id``, ``realm_id``, ``env``) are stored as-is.

Never logs values.
"""

from __future__ import annotations

from collections.abc import Callable

from pinky_qbo import crypto

# Bare key names; the registry accessor prefixes ``agent:<agent>:`` so the
# on-disk system_settings keys become ``agent:onesie:qbo_client_id`` etc.
_KEY_CLIENT_ID = "qbo_client_id"
_KEY_CLIENT_SECRET = "qbo_client_secret"  # encrypted
_KEY_REFRESH_TOKEN = "qbo_refresh_token"  # encrypted
_KEY_REALM_ID = "qbo_realm_id"
_KEY_ENV = "qbo_env"

# Fields that must be wrapped by the crypto envelope before persisting.
_SECRET_KEYS = frozenset({_KEY_CLIENT_SECRET, _KEY_REFRESH_TOKEN})


class TokenStore:
    """Persist QBO OAuth credentials in the agent-scoped settings store.

    Args:
        agent: the bound agent name (e.g. ``"onesie"``).
        set_fn: ``registry.set_agent_setting`` — ``(agent, key, value) -> None``.
        get_fn: ``registry.get_agent_setting`` — ``(agent, key, default="") -> str``.
    """

    def __init__(
        self,
        agent: str,
        set_fn: Callable[[str, str, str], None],
        get_fn: Callable[..., str],
    ) -> None:
        self._agent = agent
        self._set_raw = set_fn
        self._get_raw = get_fn

    # ── low-level get/set (with crypto for secrets) ──────────────────────────

    def _set(self, key: str, value: str) -> None:
        if key in _SECRET_KEYS:
            value = crypto.encrypt(value)
        self._set_raw(self._agent, key, value)

    def _get(self, key: str) -> str:
        value = self._get_raw(self._agent, key, "")
        if not value:
            return ""
        if key in _SECRET_KEYS:
            # Only attempt to unwrap values that look like our envelope; a bare
            # legacy plaintext value is returned untouched so a half-migrated
            # store doesn't hard-fail (it just isn't confidential — that's a
            # migration bug, not a read bug).
            if crypto.is_envelope(value):
                return crypto.decrypt(value)
        return value

    # ── client credentials ──────────────────────────────────────────────────

    def save_client_credentials(self, client_id: str, client_secret: str) -> None:
        """Persist the Intuit app client id (plaintext) + secret (encrypted)."""
        self._set(_KEY_CLIENT_ID, client_id.strip())
        self._set(_KEY_CLIENT_SECRET, client_secret.strip())

    def get_client_credentials(self) -> tuple[str, str] | tuple[None, None]:
        """Return (client_id, client_secret) or (None, None) if not stored."""
        client_id = self._get(_KEY_CLIENT_ID)
        client_secret = self._get(_KEY_CLIENT_SECRET)
        if client_id and client_secret:
            return client_id, client_secret
        return None, None

    # ── refresh token (rotates on use — persist atomically under a lock) ─────

    def save_refresh_token(self, refresh_token: str) -> None:
        """Persist the (newly rotated) refresh token, encrypted."""
        self._set(_KEY_REFRESH_TOKEN, refresh_token.strip())

    def get_refresh_token(self) -> str | None:
        """Return the stored refresh token, or None."""
        return self._get(_KEY_REFRESH_TOKEN) or None

    # ── realm / environment (non-secret) ─────────────────────────────────────

    def save_realm(self, realm_id: str, env: str = "production") -> None:
        """Persist the QBO realm id and environment (sandbox|production)."""
        self._set(_KEY_REALM_ID, realm_id.strip())
        self._set(_KEY_ENV, env.strip())

    def get_realm_id(self) -> str | None:
        """Return the stored realm id, or None."""
        return self._get(_KEY_REALM_ID) or None

    def get_env(self) -> str:
        """Return the stored QBO environment (default 'production')."""
        return self._get(_KEY_ENV) or "production"

    # ── state helpers ────────────────────────────────────────────────────────

    def is_configured(self) -> bool:
        """True when client id + secret are stored."""
        client_id, client_secret = self.get_client_credentials()
        return bool(client_id and client_secret)

    def is_connected(self) -> bool:
        """True when a refresh token + realm are stored (ready to call QBO)."""
        return bool(self.get_refresh_token() and self.get_realm_id())

    # ── cleanup ──────────────────────────────────────────────────────────────

    def clear_tokens(self) -> None:
        """Remove the refresh token, keep client creds + realm."""
        self._set(_KEY_REFRESH_TOKEN, "")

    def clear_all(self) -> None:
        """Remove all QBO settings for this agent."""
        for key in (
            _KEY_CLIENT_ID,
            _KEY_CLIENT_SECRET,
            _KEY_REFRESH_TOKEN,
            _KEY_REALM_ID,
            _KEY_ENV,
        ):
            # Write empties through the raw setter to avoid encrypting "".
            self._set_raw(self._agent, key, "")
