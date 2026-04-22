"""Google OAuth2 helpers for Calendar integration.

Uses google_auth_oauthlib for the OAuth2 flow (auth URL + code exchange)
and google.oauth2.credentials for token refresh.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timezone

SCOPES = ["https://www.googleapis.com/auth/calendar"]
REDIRECT_URI = "http://localhost:8888/calendar/google/callback"


def _build_client_config(client_id: str, client_secret: str) -> dict:
    """Build the client_config dict expected by google_auth_oauthlib."""
    return {
        "web": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [REDIRECT_URI],
        }
    }


def get_auth_url(client_id: str, client_secret: str) -> tuple[str, str]:
    """Build the Google OAuth2 authorisation URL.

    Returns:
        (auth_url, state) — state is a random nonce for CSRF protection.
    """
    from google_auth_oauthlib.flow import Flow  # type: ignore[import]

    state = secrets.token_urlsafe(32)
    flow = Flow.from_client_config(
        _build_client_config(client_id, client_secret),
        scopes=SCOPES,
        redirect_uri=REDIRECT_URI,
    )
    auth_url, _ = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
        state=state,
    )
    return auth_url, state


def exchange_code(
    client_id: str,
    client_secret: str,
    code: str,
    state: str,  # noqa: ARG001 — caller is responsible for validating state first
) -> dict:
    """Exchange an authorisation code for access + refresh tokens.

    SECURITY: Callers MUST validate `state` against a previously issued,
    unexpired, unconsumed nonce before invoking this function. Passing an
    attacker-controlled `state` here without prior validation re-opens the
    CSRF / account-linking hole fixed by #287.

    Returns:
        dict with keys: access_token, refresh_token, expiry (datetime | None).
    """
    from google_auth_oauthlib.flow import Flow  # type: ignore[import]

    flow = Flow.from_client_config(
        _build_client_config(client_id, client_secret),
        scopes=SCOPES,
        redirect_uri=REDIRECT_URI,
    )
    flow.fetch_token(code=code)
    creds = flow.credentials

    expiry: datetime | None = None
    if creds.expiry is not None:
        expiry = (
            creds.expiry.replace(tzinfo=timezone.utc)
            if creds.expiry.tzinfo is None
            else creds.expiry
        )

    return {
        "access_token": creds.token,
        "refresh_token": creds.refresh_token,
        "expiry": expiry,
    }


def refresh_access_token(
    client_id: str,
    client_secret: str,
    refresh_token: str,
) -> dict:
    """Refresh an expired access token using the stored refresh token.

    Returns:
        dict with keys: access_token, expiry (datetime | None).
    """
    from google.auth.transport.requests import Request  # type: ignore[import]
    from google.oauth2.credentials import Credentials  # type: ignore[import]

    creds = Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=client_id,
        client_secret=client_secret,
        scopes=SCOPES,
    )
    creds.refresh(Request())

    expiry: datetime | None = None
    if creds.expiry is not None:
        expiry = (
            creds.expiry.replace(tzinfo=timezone.utc)
            if creds.expiry.tzinfo is None
            else creds.expiry
        )

    return {
        "access_token": creds.token,
        "expiry": expiry,
    }
