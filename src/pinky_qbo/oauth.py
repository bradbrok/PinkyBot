"""Intuit (QuickBooks Online) OAuth 2.0 helpers — raw ``requests``, no intuitlib.

QBO is OAuth 2.0 Authorization Code (no API-key path). Endpoints (spec §4):
  - authorize:  https://appcenter.intuit.com/connect/oauth2
  - token+refresh: https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer
        (HTTP Basic client_id:client_secret, ``application/x-www-form-urlencoded``)
  - revoke: https://developer.api.intuit.com/v2/oauth2/tokens/revoke

Scope ``com.intuit.quickbooks.accounting`` (read+write at the grant level —
read-only is enforced app-side, see spec §6). Access token ~3600s; the refresh
token ROTATES on every use (persist the newest atomically; reuse ⇒
``invalid_grant``) and dies after 100d inactivity.

``state`` is bound to the agent + a CSRF nonce + the initiating session
(spec §4 "agent-bound direct callback"). Never logs token material.
"""

from __future__ import annotations

import secrets
import sys

import requests

SCOPES = ["com.intuit.quickbooks.accounting"]

AUTH_URL = "https://appcenter.intuit.com/connect/oauth2"
TOKEN_URL = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
REVOKE_URL = "https://developer.api.intuit.com/v2/oauth2/tokens/revoke"

# Agent-bound direct callback (NOT the pinky_calendar pinkybot.ai proxy flow).
REDIRECT_URI = "http://localhost:8888/qbo/callback"

_TIMEOUT = 30
# Delimiter between the agent/session binding and the random nonce in `state`.
_STATE_SEP = "."


def _log(msg: str) -> None:
    print(f"[pinky-qbo.oauth] {msg}", file=sys.stderr)


# ── CSRF / agent-bound state helpers ─────────────────────────────────────────


def make_state(agent: str, session_id: str = "") -> str:
    """Build an agent-bound, CSRF-resistant ``state`` value.

    Format: ``<agent>:<session_id>.<nonce>``. The nonce is random; the caller
    persists the full state and only accepts a callback whose ``state`` matches
    a previously issued, unconsumed value (see ``verify_state``).
    """
    if not agent:
        raise ValueError("agent is required for state binding")
    nonce = secrets.token_urlsafe(32)
    binding = f"{agent}:{session_id}"
    return f"{binding}{_STATE_SEP}{nonce}"


def verify_state(state: str, agent: str) -> bool:
    """Verify a returned ``state`` is bound to ``agent`` and well-formed.

    This checks SHAPE + agent binding only. The caller MUST also confirm the
    exact ``state`` matches a previously issued, unexpired, unconsumed nonce
    (constant-time compare against the stored value) before exchanging the code.
    """
    if not state or not agent:
        return False
    if _STATE_SEP not in state:
        return False
    binding, _, nonce = state.partition(_STATE_SEP)
    if not nonce:
        return False
    expected_prefix = f"{agent}:"
    return binding.startswith(expected_prefix)


# ── Auth URL ─────────────────────────────────────────────────────────────────


def get_auth_url(client_id: str, state: str, redirect_uri: str = REDIRECT_URI) -> str:
    """Build the Intuit OAuth2 authorization URL.

    The caller supplies ``state`` from ``make_state`` and must persist it for
    later verification.
    """
    from urllib.parse import urlencode

    params = {
        "client_id": client_id,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "redirect_uri": redirect_uri,
        "state": state,
    }
    return f"{AUTH_URL}?{urlencode(params)}"


# ── Token endpoint (exchange / refresh) ──────────────────────────────────────


def _token_request(
    client_id: str,
    client_secret: str,
    form: dict,
) -> dict:
    """POST to the Intuit token endpoint with HTTP Basic + form body.

    Returns the parsed JSON dict. Raises requests.HTTPError on non-2xx.
    Never logs the form body or response token fields.
    """
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    resp = requests.post(
        TOKEN_URL,
        auth=(client_id, client_secret),  # HTTP Basic
        data=form,
        headers=headers,
        timeout=_TIMEOUT,
    )
    if resp.status_code >= 400:
        # Surface Intuit's error code (e.g. invalid_grant) without token data.
        detail = ""
        try:
            body = resp.json()
            detail = body.get("error", "") or body.get("error_description", "")
        except ValueError:
            detail = resp.text[:200]
        raise requests.HTTPError(
            f"Intuit token endpoint returned {resp.status_code}: {detail}",
            response=resp,
        )
    return resp.json()


def exchange_code(
    client_id: str,
    client_secret: str,
    code: str,
    redirect_uri: str = REDIRECT_URI,
) -> dict:
    """Exchange an authorization code for tokens.

    SECURITY: the caller MUST validate ``state`` (``verify_state`` + stored-nonce
    match) BEFORE calling this.

    Returns dict: access_token, refresh_token, expires_in, x_refresh_token_expires_in,
    token_type, realm_id (None — read from the callback's ``realmId`` query param).
    """
    payload = _token_request(
        client_id,
        client_secret,
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
        },
    )
    return {
        "access_token": payload.get("access_token"),
        "refresh_token": payload.get("refresh_token"),
        "expires_in": payload.get("expires_in"),
        "x_refresh_token_expires_in": payload.get("x_refresh_token_expires_in"),
        "token_type": payload.get("token_type"),
        # realmId comes from the callback query string, not this response.
        "realm_id": None,
    }


def refresh(client_id: str, client_secret: str, refresh_token: str) -> dict:
    """Refresh tokens. The refresh token ROTATES — persist the returned one.

    Returns dict: access_token, refresh_token (NEW — persist atomically),
    expires_in, x_refresh_token_expires_in, token_type.
    """
    payload = _token_request(
        client_id,
        client_secret,
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        },
    )
    return {
        "access_token": payload.get("access_token"),
        "refresh_token": payload.get("refresh_token"),
        "expires_in": payload.get("expires_in"),
        "x_refresh_token_expires_in": payload.get("x_refresh_token_expires_in"),
        "token_type": payload.get("token_type"),
    }


def revoke(client_id: str, client_secret: str, token: str) -> bool:
    """Revoke an access or refresh token. Returns True on success (HTTP 200)."""
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    resp = requests.post(
        REVOKE_URL,
        auth=(client_id, client_secret),
        json={"token": token},
        headers=headers,
        timeout=_TIMEOUT,
    )
    return resp.status_code == 200
