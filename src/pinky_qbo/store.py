"""Agent-scoped token store for QuickBooks Online credentials.

Secrets (client_secret, refresh_token) are encrypted at rest via the
crypto module before writing to the agent-scoped system_settings store.
Access tokens are NEVER persisted — memory-only in the adapter.

Keys written to the store (namespaced agent:onesie:* by the caller):
    qbo_client_id       — plain
    qbo_client_secret   — Fernet-encrypted
    qbo_refresh_token   — Fernet-encrypted
    qbo_realm_id        — plain
    qbo_env             — plain ("sandbox" | "production")
"""

from __future__ import annotations

from collections.abc import Callable

# Logical key names (caller namespaces them via agent-scoped get/set)
_KEY_CLIENT_ID = "qbo_client_id"
_KEY_CLIENT_SECRET = "qbo_client_secret"
_KEY_REFRESH_TOKEN = "qbo_refresh_token"
_KEY_REALM_ID = "qbo_realm_id"
_KEY_ENV = "qbo_env"


class QboTokenStore:
    """Persist QBO OAuth2 credentials in the agent-scoped system_settings store.

    The get_fn/set_fn/delete_fn callables are injected from __main__ and wrap
    AgentRegistry.get_agent_setting / set_agent_setting / delete_setting so all
    keys are automatically namespaced as agent:onesie:qbo_*.
    """

    def __init__(
        self,
        get_fn: Callable[[str], str | None],
        set_fn: Callable[[str, str], None],
        delete_fn: Callable[[str], None] | None = None,
    ) -> None:
        self._get = get_fn
        self._set = set_fn
        self._del = delete_fn or (lambda _k: None)

    # ── Encryption helpers ──────────────────────────────────────────────────

    @staticmethod
    def _encrypt(value: str) -> str:
        from pinky_qbo.crypto import encrypt  # lazy import — key may not be ready at init

        return encrypt(value)

    @staticmethod
    def _decrypt(value: str) -> str:
        from pinky_qbo.crypto import decrypt

        return decrypt(value)

    # ── Client credentials ──────────────────────────────────────────────────

    def save_client_credentials(self, client_id: str, client_secret: str) -> None:
        """Persist OAuth2 client ID (plain) and client secret (encrypted)."""
        self._set(_KEY_CLIENT_ID, client_id.strip())
        self._set(_KEY_CLIENT_SECRET, self._encrypt(client_secret.strip()))

    def get_client_credentials(self) -> tuple[str, str] | tuple[None, None]:
        """Return (client_id, client_secret) decrypted, or (None, None) if not stored."""
        client_id = self._get(_KEY_CLIENT_ID)
        encrypted_secret = self._get(_KEY_CLIENT_SECRET)
        if not client_id or not encrypted_secret:
            return None, None
        try:
            client_secret = self._decrypt(encrypted_secret)
        except Exception:
            return None, None
        return client_id, client_secret

    # ── Refresh token ───────────────────────────────────────────────────────

    def save_refresh_token(self, tok: str) -> None:
        """Persist the refresh token encrypted at rest."""
        self._set(_KEY_REFRESH_TOKEN, self._encrypt(tok.strip()))

    def get_refresh_token(self) -> str | None:
        """Return the decrypted refresh token, or None if not stored."""
        encrypted = self._get(_KEY_REFRESH_TOKEN)
        if not encrypted:
            return None
        try:
            return self._decrypt(encrypted)
        except Exception:
            return None

    # ── Realm & environment ─────────────────────────────────────────────────

    def get_realm(self) -> str | None:
        return self._get(_KEY_REALM_ID) or None

    def set_realm(self, realm_id: str) -> None:
        self._set(_KEY_REALM_ID, realm_id.strip())

    def get_env(self) -> str | None:
        return self._get(_KEY_ENV) or None

    def set_env(self, env: str) -> None:
        if env not in ("sandbox", "production"):
            raise ValueError(f"env must be 'sandbox' or 'production', got '{env}'")
        self._set(_KEY_ENV, env)

    # ── State ───────────────────────────────────────────────────────────────

    def is_configured(self) -> bool:
        """True when client credentials AND a refresh token are stored."""
        client_id, client_secret = self.get_client_credentials()
        refresh_token = self.get_refresh_token()
        return bool(client_id and client_secret and refresh_token)

    # ── Cleanup ─────────────────────────────────────────────────────────────

    def clear(self) -> None:
        """Remove all QBO settings from the store."""
        for key in (_KEY_CLIENT_ID, _KEY_CLIENT_SECRET, _KEY_REFRESH_TOKEN, _KEY_REALM_ID, _KEY_ENV):
            try:
                self._del(key)
            except Exception:
                pass
