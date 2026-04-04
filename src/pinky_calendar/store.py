"""Token storage for Google Calendar OAuth2 credentials.

Reads/writes to system_settings via injected get_fn/set_fn/delete_fn callables,
matching the AgentRegistry interface.
"""

from __future__ import annotations

from datetime import datetime
from typing import Callable

_KEY_CLIENT_ID = "GOOGLE_CALENDAR_CLIENT_ID"
_KEY_CLIENT_SECRET = "GOOGLE_CALENDAR_CLIENT_SECRET"
_KEY_ACCESS_TOKEN = "GOOGLE_CALENDAR_ACCESS_TOKEN"
_KEY_REFRESH_TOKEN = "GOOGLE_CALENDAR_REFRESH_TOKEN"
_KEY_TOKEN_EXPIRY = "GOOGLE_CALENDAR_TOKEN_EXPIRY"


class TokenStore:
    """Persist Google Calendar OAuth2 tokens in system_settings."""

    def __init__(
        self,
        set_fn: Callable[[str, str], None],
        get_fn: Callable[[str], str | None],
        delete_fn: Callable[[str], None] | None = None,
    ) -> None:
        self._set = set_fn
        self._get = get_fn
        self._del = delete_fn or (lambda _k: None)

    # ── Client credentials ──────────────────────────────────────────────────

    def save_client_credentials(self, client_id: str, client_secret: str) -> None:
        """Persist OAuth2 client ID and secret."""
        self._set(_KEY_CLIENT_ID, client_id.strip())
        self._set(_KEY_CLIENT_SECRET, client_secret.strip())

    def get_client_credentials(self) -> tuple[str, str] | tuple[None, None]:
        """Return (client_id, client_secret) or (None, None) if not stored."""
        client_id = self._get(_KEY_CLIENT_ID)
        client_secret = self._get(_KEY_CLIENT_SECRET)
        if client_id and client_secret:
            return client_id, client_secret
        return None, None

    # ── Tokens ──────────────────────────────────────────────────────────────

    def save_tokens(
        self,
        access_token: str,
        refresh_token: str,
        expiry: datetime | None,
    ) -> None:
        """Persist OAuth2 access + refresh tokens."""
        self._set(_KEY_ACCESS_TOKEN, access_token)
        self._set(_KEY_REFRESH_TOKEN, refresh_token)
        if expiry is not None:
            self._set(_KEY_TOKEN_EXPIRY, expiry.isoformat())
        else:
            try:
                self._del(_KEY_TOKEN_EXPIRY)
            except Exception:
                pass

    def get_tokens(self) -> dict:
        """Return all token fields as a dict."""
        return {
            "access_token": self._get(_KEY_ACCESS_TOKEN),
            "refresh_token": self._get(_KEY_REFRESH_TOKEN),
            "expiry": self._get(_KEY_TOKEN_EXPIRY),
        }

    # ── State helpers ───────────────────────────────────────────────────────

    def is_configured(self) -> bool:
        """True when client ID and secret are stored."""
        client_id, client_secret = self.get_client_credentials()
        return bool(client_id and client_secret)

    def is_connected(self) -> bool:
        """True when OAuth tokens are stored."""
        tokens = self.get_tokens()
        return bool(tokens.get("access_token") and tokens.get("refresh_token"))

    # ── Cleanup ─────────────────────────────────────────────────────────────

    def clear_tokens(self) -> None:
        """Remove OAuth tokens, keep client credentials."""
        for key in (_KEY_ACCESS_TOKEN, _KEY_REFRESH_TOKEN, _KEY_TOKEN_EXPIRY):
            try:
                self._del(key)
            except Exception:
                pass

    def clear_all(self) -> None:
        """Remove all Google Calendar settings."""
        for key in (
            _KEY_CLIENT_ID,
            _KEY_CLIENT_SECRET,
            _KEY_ACCESS_TOKEN,
            _KEY_REFRESH_TOKEN,
            _KEY_TOKEN_EXPIRY,
        ):
            try:
                self._del(key)
            except Exception:
                pass
