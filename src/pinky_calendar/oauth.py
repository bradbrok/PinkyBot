"""OAuth2 helpers for calendar provider authorization flows."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Optional


# ── Config ────────────────────────────────────────────────────


@dataclass
class GoogleOAuthConfig:
    client_id: str
    client_secret: str
    redirect_uri: str
    scopes: list[str] = None

    def __post_init__(self):
        if self.scopes is None:
            self.scopes = ["https://www.googleapis.com/auth/calendar"]


def google_config_from_env(base_url: str = "") -> Optional[GoogleOAuthConfig]:
    """Load Google OAuth config from system settings env vars."""
    client_id = os.environ.get("GOOGLE_CALENDAR_CLIENT_ID", "")
    client_secret = os.environ.get("GOOGLE_CALENDAR_CLIENT_SECRET", "")
    if not client_id or not client_secret:
        return None
    redirect_uri = f"{base_url.rstrip('/')}/oauth/calendar/google/callback"
    return GoogleOAuthConfig(
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=redirect_uri,
    )


# ── Google OAuth flow ─────────────────────────────────────────


def google_auth_url(config: GoogleOAuthConfig, state: str) -> str:
    """Generate the Google OAuth2 authorization URL."""
    from google_auth_oauthlib.flow import Flow

    flow = Flow.from_client_config(
        {
            "web": {
                "client_id": config.client_id,
                "client_secret": config.client_secret,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": [config.redirect_uri],
            }
        },
        scopes=config.scopes,
        redirect_uri=config.redirect_uri,
    )
    auth_url, _ = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
        state=state,
    )
    return auth_url


def google_exchange_code(
    config: GoogleOAuthConfig, code: str
) -> dict:
    """Exchange an auth code for tokens. Returns dict with access/refresh tokens."""
    from google_auth_oauthlib.flow import Flow

    flow = Flow.from_client_config(
        {
            "web": {
                "client_id": config.client_id,
                "client_secret": config.client_secret,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": [config.redirect_uri],
            }
        },
        scopes=config.scopes,
        redirect_uri=config.redirect_uri,
    )
    flow.fetch_token(code=code)
    creds = flow.credentials
    return {
        "access_token": creds.token,
        "refresh_token": creds.refresh_token or "",
        "expires_at": time.time() + (creds.expiry.timestamp() - time.time() if creds.expiry else 3600),
        "scopes": list(creds.scopes or []),
    }


# ── State management (CSRF protection) ───────────────────────

import hashlib
import secrets as _secrets


def generate_state(user_id: str) -> str:
    """Generate a short-lived opaque state token tied to user_id."""
    token = _secrets.token_urlsafe(16)
    # In production this should be stored server-side with TTL.
    # For now encode user_id in the state (verified on callback).
    return f"{user_id}:{token}"


def parse_state(state: str) -> tuple[str, str]:
    """Parse state into (user_id, token). Raises ValueError if malformed."""
    parts = state.split(":", 1)
    if len(parts) != 2:
        raise ValueError(f"Invalid OAuth state: {state!r}")
    return parts[0], parts[1]
