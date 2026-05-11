"""Calendar routes: CalDAV config, Google OAuth, per-agent enable/disable.

Extracted from api.py. The Google OAuth flow has two paths:
  1. Proxied via pinkybot.ai (default — `auth-url` + `fetch-token`)
  2. Legacy direct-Google flow with state-nonce CSRF defense
     (`direct-auth-url` + `callback`)

Both paths share `_google_token_store()` which is just a thin
constructor over the AgentRegistry settings table.
"""

from __future__ import annotations

import json as _json
import os as _os
import secrets
import urllib.request
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse

router = APIRouter(tags=["calendar"])


# ── Shared dependency state ───────────────────────────────────────────────────

_agents: Any = None


def set_dependencies(*, agents) -> None:
    """Wire AgentRegistry for calendar routes."""
    global _agents
    _agents = agents


# ── Local helpers ─────────────────────────────────────────────────────────────


def _google_token_store():
    from pinky_calendar.store import TokenStore
    return TokenStore(
        set_fn=_agents.set_setting,
        get_fn=_agents.get_setting,
        delete_fn=_agents.delete_setting,
    )


# ── Legacy direct-Google OAuth state lifecycle (#287) ─────────
# Keys: GOOGLE_OAUTH_STATE_{state_nonce} → ISO-8601 issued timestamp.
# State nonces are single-use with a 10-minute TTL; consumed by the
# callback handler before the code exchange is attempted.
_GOOGLE_OAUTH_STATE_TTL_SEC = 600
_GOOGLE_OAUTH_STATE_PREFIX = "GOOGLE_OAUTH_STATE_"


def _issue_google_oauth_state(nonce: str) -> None:
    _agents.set_setting(
        f"{_GOOGLE_OAUTH_STATE_PREFIX}{nonce}",
        datetime.now(tz=timezone.utc).isoformat(),
    )


def _consume_google_oauth_state(nonce: str) -> str:
    """Validate + delete a state nonce. Returns '' on success, else error reason.

    Callers MUST treat any non-empty return as a hard rejection — the token
    exchange must not proceed. Fails closed on every error path: the nonce
    is considered consumed only if the DELETE observably removed a row.

    Race semantics: if two callbacks fire concurrently with the same nonce
    (one legitimate, one forged replay), exactly one `delete_setting()`
    returns True and is accepted; the loser sees False and is rejected
    as "unknown or replayed state".
    """
    if not nonce:
        return "missing state parameter"
    key = f"{_GOOGLE_OAUTH_STATE_PREFIX}{nonce}"
    issued_raw = _agents.get_setting(key)
    if not issued_raw:
        return "unknown or replayed state"
    # DELETE is the authoritative consume step: its return value tells us
    # whether *we* were the one to remove the row. If False, another
    # caller beat us to it → treat as replay. If it raises, fail closed.
    try:
        deleted = _agents.delete_setting(key)
    except Exception:
        return "could not consume state"
    if not deleted:
        return "unknown or replayed state"
    try:
        issued = datetime.fromisoformat(issued_raw)
    except ValueError:
        return "corrupt state record"
    # Reject naive timestamps — `datetime.fromisoformat` happily accepts
    # ISO strings without a tz suffix and returns a naive datetime, and
    # subtracting a naive dt from an aware `now()` raises TypeError.
    # We only ever *issue* aware UTC timestamps, so a naive record here
    # implies corruption or tampering.
    if issued.tzinfo is None:
        return "corrupt state record"
    age_sec = (datetime.now(tz=timezone.utc) - issued).total_seconds()
    if age_sec > _GOOGLE_OAUTH_STATE_TTL_SEC:
        return "expired state"
    if age_sec < 0:
        return "state issued in the future"
    return ""


# ── CalDAV config + status ────────────────────────────────────────────────────


@router.get("/calendar/status")
async def calendar_status():
    """Get calendar configuration status (CalDAV + Google)."""
    from pinky_calendar.store import TokenStore

    caldav_url = _agents.get_setting("CALDAV_URL") or ""
    caldav_user = _agents.get_setting("CALDAV_USERNAME") or ""
    caldav_configured = bool(caldav_url and caldav_user)

    store = TokenStore(
        set_fn=_agents.set_setting,
        get_fn=_agents.get_setting,
        delete_fn=_agents.delete_setting,
    )
    google_configured = store.is_configured()
    google_connected = store.is_connected()

    # Fetch Google email if connected
    google_email: str | None = None
    if google_connected:
        try:
            tokens = store.get_tokens()
            client_id, client_secret = store.get_client_credentials()
            from pinky_calendar.adapters.google import GoogleCalendarAdapter
            adapter = GoogleCalendarAdapter(
                access_token=tokens["access_token"],
                refresh_token=tokens["refresh_token"],
                client_id=client_id,
                client_secret=client_secret,
                token_expiry=tokens.get("expiry"),
            )
            result = adapter.test_connection()
            if result.get("ok"):
                google_email = result.get("email")
        except Exception:
            pass

    if caldav_configured and google_connected:
        provider = "both"
    elif google_connected:
        provider = "google"
    elif caldav_configured:
        provider = "caldav"
    else:
        provider = "none"

    return {
        "provider": provider,
        "configured": caldav_configured or google_connected,
        "caldav_url": caldav_url,
        "caldav_username": caldav_user,
        "caldav_password_set": bool(_agents.get_setting("CALDAV_PASSWORD")),
        "caldav": {
            "configured": caldav_configured,
            "url": caldav_url,
            "username": caldav_user,
        },
        "google": {
            "configured": google_configured,
            "connected": google_connected,
            "email": google_email,
        },
    }


@router.put("/calendar/config")
async def configure_calendar(req: dict):
    """Save calendar credentials."""
    url = req.get("caldav_url", "").strip()
    username = req.get("caldav_username", "").strip()
    password = req.get("caldav_password", "").strip()
    if url:
        _agents.set_setting("CALDAV_URL", url)
    if username:
        _agents.set_setting("CALDAV_USERNAME", username)
    if password:
        _agents.set_setting("CALDAV_PASSWORD", password)
    return {"saved": True}


@router.post("/calendar/test")
async def test_calendar():
    """Test the CalDAV connection with stored credentials."""
    url = _agents.get_setting("CALDAV_URL") or ""
    username = _agents.get_setting("CALDAV_USERNAME") or ""
    password = _agents.get_setting("CALDAV_PASSWORD") or ""
    if not url or not username:
        return {"ok": False, "error": "Calendar not configured"}
    try:
        from pinky_calendar.adapters.caldav import CalDAVAdapter
        adapter = CalDAVAdapter(url=url, username=username, password=password)
        return adapter.test_connection()
    except Exception as e:
        return {"ok": False, "error": str(e)}


@router.delete("/calendar/config")
async def delete_calendar_config():
    """Remove all calendar credentials."""
    for key in ("CALDAV_URL", "CALDAV_USERNAME", "CALDAV_PASSWORD"):
        try:
            _agents.delete_setting(key)
        except Exception:
            pass
    return {"deleted": True}


# ── Google Calendar OAuth ─────────────────────────────────────────────────────


@router.get("/calendar/google/status")
async def google_calendar_status():
    """Return Google Calendar configuration and connection status."""
    store = _google_token_store()
    configured = store.is_configured()
    connected = store.is_connected()
    client_id, _ = store.get_client_credentials()

    email: str | None = None
    if connected:
        try:
            tokens = store.get_tokens()
            cid, csecret = store.get_client_credentials()
            from pinky_calendar.adapters.google import GoogleCalendarAdapter
            adapter = GoogleCalendarAdapter(
                access_token=tokens["access_token"],
                refresh_token=tokens["refresh_token"],
                client_id=cid,
                client_secret=csecret,
                token_expiry=tokens.get("expiry"),
            )
            result = adapter.test_connection()
            if result.get("ok"):
                email = result.get("email")
        except Exception:
            pass

    return {
        "configured": configured,
        "connected": connected,
        "email": email,
        "client_id_set": bool(client_id),
        "proxy_enabled": True,
    }


@router.put("/calendar/google/credentials")
async def save_google_credentials(req: dict):
    """Save Google OAuth2 client ID and secret."""
    client_id = (req.get("client_id") or "").strip()
    client_secret = (req.get("client_secret") or "").strip()
    if not client_id or not client_secret:
        raise HTTPException(400, "client_id and client_secret are required")
    store = _google_token_store()
    store.save_client_credentials(client_id, client_secret)
    return {"saved": True}


@router.get("/calendar/google/auth-url")
async def google_auth_url():
    """Generate session + return pinkybot.ai proxy auth URL."""
    session_id = secrets.token_urlsafe(32)
    # store session_id in DB so we can validate the return
    _agents.set_setting(f"GOOGLE_OAUTH_SESSION_{session_id}", "pending")
    return {
        "auth_url": f"https://pinkybot.ai/oauth/google/start?session={session_id}",
        "session": session_id,
    }


@router.get("/calendar/google/direct-auth-url")
async def google_direct_auth_url():
    """Generate a direct-Google OAuth auth URL with a persisted state nonce.

    Used by the backward-compat flow where a user has registered their own
    Google Cloud client credentials and `/calendar/google/callback` as the
    redirect URI. The state nonce is persisted here so the callback can
    validate it (CSRF defense — see #287).
    """
    store = _google_token_store()
    client_id, client_secret = store.get_client_credentials()
    if not client_id or not client_secret:
        raise HTTPException(400, "Google client credentials not configured")
    from pinky_calendar.oauth import get_auth_url
    auth_url, state = get_auth_url(client_id, client_secret)
    _issue_google_oauth_state(state)
    return {"auth_url": auth_url, "state": state}


@router.get("/calendar/google/fetch-token")
async def fetch_google_token(session: str):
    """Retrieve tokens from pinkybot.ai proxy after OAuth completes."""
    # validate session was issued by us
    key = f"GOOGLE_OAUTH_SESSION_{session}"
    if not _agents.get_setting(key):
        raise HTTPException(400, "Unknown session")
    try:
        with urllib.request.urlopen(
            f"https://pinkybot.ai/oauth/google/token?session={session}", timeout=10
        ) as resp:
            data = _json.loads(resp.read())
    except Exception as e:
        raise HTTPException(502, f"Proxy error: {e}") from e
    if "error" in data:
        raise HTTPException(400, data["error"])
    store = _google_token_store()
    # Save client credentials from proxy (needed for token refresh)
    if data.get("client_id") and data.get("client_secret"):
        store.save_client_credentials(data["client_id"], data["client_secret"])
    store.save_tokens(
        access_token=data["access_token"],
        refresh_token=data["refresh_token"],
        expiry=data.get("expiry"),
    )
    # cleanup session key
    try:
        _agents.delete_setting(key)
    except Exception:
        pass
    return {"connected": True}


@router.get("/calendar/google/callback")
async def google_callback(code: str = "", state: str = ""):
    """Handle the OAuth2 redirect (backward-compat for users with own credentials).

    Requires a previously issued + unexpired + unconsumed state nonce
    (see `/calendar/google/direct-auth-url`). Missing / unknown / replayed /
    expired states are rejected before any token exchange happens, which
    closes the CSRF / account-linking hole described in #287.
    """
    state_error = _consume_google_oauth_state(state)
    if state_error:
        return HTMLResponse(
            f"<html><body><h3>OAuth state validation failed: {state_error}.</h3>"
            "<p>Please retry from Settings — do not re-use an old link.</p>"
            "<script>window.close();</script></body></html>",
            status_code=400,
        )
    if not code:
        return HTMLResponse(
            "<html><body><h3>OAuth error: missing authorization code.</h3>"
            "<script>window.close();</script></body></html>",
            status_code=400,
        )
    store = _google_token_store()
    client_id, client_secret = store.get_client_credentials()
    if not client_id or not client_secret:
        return HTMLResponse(
            "<html><body><h3>Error: Google credentials not configured.</h3>"
            "<script>window.close();</script></body></html>",
            status_code=400,
        )
    try:
        from pinky_calendar.oauth import exchange_code
        tokens = exchange_code(client_id, client_secret, code, state)
        store.save_tokens(
            access_token=tokens["access_token"],
            refresh_token=tokens["refresh_token"],
            expiry=tokens.get("expiry"),
        )
    except Exception as e:
        return HTMLResponse(
            f"<html><body><h3>OAuth error: {e}</h3>"
            "<script>window.close();</script></body></html>",
            status_code=400,
        )
    return HTMLResponse(
        "<html><body><p>Google Calendar connected! You can close this window.</p>"
        "<script>"
        "window.opener?.postMessage('google-calendar-connected', '*');"
        "window.close();"
        "</script></body></html>"
    )


@router.delete("/calendar/google/disconnect")
async def google_disconnect():
    """Remove Google Calendar tokens (keeps client credentials)."""
    store = _google_token_store()
    store.clear_tokens()
    return {"disconnected": True}


# ── Per-agent calendar enable/disable ─────────────────────────────────────────


@router.post("/agents/{name}/calendar/enable")
async def enable_agent_calendar(name: str):
    """Enable calendar MCP server for an agent (adds to .mcp.json)."""
    agent = _agents.get(name)
    if not agent:
        raise HTTPException(404, f"Agent '{name}' not found")

    url = _agents.get_setting("CALDAV_URL") or ""
    username = _agents.get_setting("CALDAV_USERNAME") or ""
    password = _agents.get_setting("CALDAV_PASSWORD") or ""
    if not url or not username:
        raise HTTPException(400, "Calendar not configured. Set credentials first.")

    # Use the actual venv from INSTALL_DIR
    install_dir = _os.path.dirname(_os.path.dirname(_os.path.dirname(agent.working_dir)))
    venv_python = _os.path.join(install_dir, ".venv", "bin", "python")
    src_dir = _os.path.join(install_dir, "src")

    mcp_path = _os.path.join(agent.working_dir, ".mcp.json")
    try:
        with open(mcp_path) as f:
            mcp_cfg = _json.load(f)
    except Exception:
        mcp_cfg = {"mcpServers": {}}

    mcp_cfg.setdefault("mcpServers", {})["pinky-calendar"] = {
        "command": venv_python,
        "args": ["-m", "pinky_calendar"],
        "cwd": src_dir,
        "env": {
            "CALDAV_URL": url,
            "CALDAV_USERNAME": username,
            "CALDAV_PASSWORD": password,
        },
    }
    with open(mcp_path, "w") as f:
        _json.dump(mcp_cfg, f, indent=2)

    _agents.set_agent_setting(name, "calendar_enabled", "true")
    return {"enabled": True, "agent": name}


@router.post("/agents/{name}/calendar/disable")
async def disable_agent_calendar(name: str):
    """Disable calendar MCP server for an agent (removes from .mcp.json)."""
    agent = _agents.get(name)
    if not agent:
        raise HTTPException(404, f"Agent '{name}' not found")

    mcp_path = _os.path.join(agent.working_dir, ".mcp.json")
    try:
        with open(mcp_path) as f:
            mcp_cfg = _json.load(f)
        mcp_cfg.get("mcpServers", {}).pop("pinky-calendar", None)
        with open(mcp_path, "w") as f:
            _json.dump(mcp_cfg, f, indent=2)
    except Exception:
        pass

    _agents.set_agent_setting(name, "calendar_enabled", "false")
    return {"disabled": True, "agent": name}


@router.get("/agents/{name}/calendar/status")
async def agent_calendar_status(name: str):
    """Check if calendar is enabled for an agent."""
    agent = _agents.get(name)
    if not agent:
        raise HTTPException(404, f"Agent '{name}' not found")
    mcp_path = _os.path.join(agent.working_dir, ".mcp.json")
    enabled = False
    try:
        with open(mcp_path) as f:
            mcp_cfg = _json.load(f)
        enabled = "pinky-calendar" in mcp_cfg.get("mcpServers", {})
    except Exception:
        pass
    return {"agent": name, "calendar_enabled": enabled}
