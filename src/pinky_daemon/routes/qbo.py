"""QBO routes: agent-bound OAuth (direct callback only) + per-agent enable.

Read-only QuickBooks Online MCP wiring for a single agent (onesie, TOD fleet).
Mirrors ``routes/calendar.py`` but WITHOUT the pinkybot.ai proxy / postMessage
flow — the OAuth callback is a DIRECT, agent-bound redirect (spec §4 "agent-bound
direct callback only").

Security model (spec §4/§6):
  - ``state`` is bound to the agent + a CSRF nonce (``oauth.make_state``) and
    persisted server-side; the callback rejects any state that wasn't issued,
    was replayed, expired, or whose binding doesn't match the agent.
  - Secrets (client secret + rotating refresh token) are stored encrypted in the
    agent-scoped TokenStore; ``realmId`` comes from the callback query param.
  - The route never logs or echoes token material.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse

router = APIRouter(tags=["qbo"])


# ── Shared dependency state ───────────────────────────────────────────────────

_agents: Any = None


def set_dependencies(*, agents) -> None:
    """Wire AgentRegistry for QBO routes."""
    global _agents
    _agents = agents


# ── Agent-scoped token store ──────────────────────────────────────────────────


def _token_store(agent: str):
    from pinky_qbo.store import TokenStore

    return TokenStore(
        agent=agent,
        set_fn=_agents.set_agent_setting,
        get_fn=_agents.get_agent_setting,
    )


# ── Agent-bound OAuth state lifecycle (CSRF, single-use, TTL) ─────────────────
# Stored as an agent-scoped setting keyed by the issued state string itself, so
# the binding (agent) and the nonce both have to match. Single-use + 10-min TTL.
_QBO_OAUTH_STATE_TTL_SEC = 600
_QBO_OAUTH_STATE_KEY = "qbo_oauth_state"  # value = "<state>|<issued-iso>"


def _issue_qbo_oauth_state(agent: str, state: str) -> None:
    issued = datetime.now(tz=timezone.utc).isoformat()
    _agents.set_agent_setting(agent, _QBO_OAUTH_STATE_KEY, f"{state}|{issued}")


def _consume_qbo_oauth_state(agent: str, state: str) -> str:
    """Validate + delete the agent's pending state. '' on success, else reason.

    Fails closed: missing/unknown/replayed/expired/mismatched-binding all reject
    BEFORE any code exchange. Single-use — the record is cleared on consume.
    """
    from pinky_qbo import oauth

    if not state:
        return "missing state parameter"
    # Binding + shape check (agent must own this state).
    if not oauth.verify_state(state, agent):
        return "state not bound to this agent"

    stored = _agents.get_agent_setting(agent, _QBO_OAUTH_STATE_KEY, "")
    if not stored or "|" not in stored:
        return "unknown or replayed state"
    stored_state, _, issued_raw = stored.partition("|")

    # Constant-time-ish compare of the exact issued state.
    import hmac

    if not hmac.compare_digest(stored_state, state):
        return "unknown or replayed state"

    # Consume (single-use) BEFORE validating age so a replay can't reuse it.
    _agents.set_agent_setting(agent, _QBO_OAUTH_STATE_KEY, "")

    try:
        issued = datetime.fromisoformat(issued_raw)
    except ValueError:
        return "corrupt state record"
    if issued.tzinfo is None:
        return "corrupt state record"
    age = (datetime.now(tz=timezone.utc) - issued).total_seconds()
    if age > _QBO_OAUTH_STATE_TTL_SEC:
        return "expired state"
    if age < 0:
        return "state issued in the future"
    return ""


# ── Status / credentials ──────────────────────────────────────────────────────


@router.get("/agents/{name}/qbo/status")
async def qbo_status(name: str):
    """Report QBO configuration + connection status for an agent."""
    agent = _agents.get(name)
    if not agent:
        raise HTTPException(404, f"Agent '{name}' not found")
    store = _token_store(name)
    client_id, _ = store.get_client_credentials()
    return {
        "agent": name,
        "configured": store.is_configured(),
        "connected": store.is_connected(),
        "client_id_set": bool(client_id),
        # realmId is redacted in API responses (spec §6) — expose only whether
        # a company is connected, never the company id itself (review P1).
        "realm_id_set": bool(store.get_realm_id()),
        "env": store.get_env(),
    }


@router.put("/agents/{name}/qbo/credentials")
async def save_qbo_credentials(name: str, req: dict):
    """Save the Intuit app client id + secret (secret encrypted at rest)."""
    agent = _agents.get(name)
    if not agent:
        raise HTTPException(404, f"Agent '{name}' not found")
    client_id = (req.get("client_id") or "").strip()
    client_secret = (req.get("client_secret") or "").strip()
    if not client_id or not client_secret:
        raise HTTPException(400, "client_id and client_secret are required")
    store = _token_store(name)
    store.save_client_credentials(client_id, client_secret)
    return {"saved": True, "agent": name}


# ── Agent-bound OAuth (direct callback) ───────────────────────────────────────


@router.get("/agents/{name}/qbo/auth-url")
async def qbo_auth_url(name: str, session_id: str = ""):
    """Build the Intuit authorize URL with an agent-bound, CSRF state nonce.

    The state is persisted server-side; the direct callback validates it before
    exchanging the code. No pinkybot.ai proxy is involved.
    """
    agent = _agents.get(name)
    if not agent:
        raise HTTPException(404, f"Agent '{name}' not found")
    store = _token_store(name)
    client_id, client_secret = store.get_client_credentials()
    if not client_id or not client_secret:
        raise HTTPException(400, "QBO client credentials not configured")

    from pinky_qbo.oauth import get_auth_url, make_state

    state = make_state(name, session_id)
    _issue_qbo_oauth_state(name, state)
    auth_url = get_auth_url(client_id, state)
    return {"auth_url": auth_url, "state": state}


@router.get("/qbo/callback")
async def qbo_callback(
    code: str = "",
    state: str = "",
    realmId: str = "",  # noqa: N803 — Intuit sends this exact query param
):
    """Direct, agent-bound OAuth callback.

    The agent is recovered from the state binding (``<agent>:<session>.<nonce>``)
    so the redirect URI can stay agent-agnostic. Validates the state (issued,
    unconsumed, unexpired, bound to that agent) BEFORE any token exchange, then
    persists the rotating refresh token + realm in the agent-scoped store.
    """

    def _fail(msg: str, status: int = 400) -> HTMLResponse:
        return HTMLResponse(
            f"<html><body><h3>QBO OAuth error: {msg}.</h3>"
            "<p>Please retry from Settings — do not re-use an old link.</p>"
            "<script>window.close();</script></body></html>",
            status_code=status,
        )

    if not state or ":" not in state:
        return _fail("missing or malformed state")
    # Recover the bound agent from the state prefix "<agent>:<session>.<nonce>".
    agent_name = state.split(":", 1)[0]
    if not agent_name or not _agents.get(agent_name):
        return _fail("state bound to an unknown agent")

    state_error = _consume_qbo_oauth_state(agent_name, state)
    if state_error:
        return _fail(state_error)
    if not code:
        return _fail("missing authorization code")
    if not realmId:
        return _fail("missing realmId")

    store = _token_store(agent_name)
    client_id, client_secret = store.get_client_credentials()
    if not client_id or not client_secret:
        return _fail("client credentials not configured")

    try:
        from pinky_qbo.oauth import exchange_code

        tokens = exchange_code(client_id, client_secret, code)
        refresh_token = tokens.get("refresh_token")
        if not refresh_token:
            return _fail("token exchange returned no refresh token")
        store.save_refresh_token(refresh_token)
        store.save_realm(realmId)
    except Exception:
        # Never echo the exception (could contain token/code material).
        return _fail("token exchange failed")

    return HTMLResponse(
        "<html><body><p>QuickBooks connected! You can close this window.</p>"
        "<script>window.close();</script></body></html>"
    )


@router.delete("/agents/{name}/qbo/disconnect")
async def qbo_disconnect(name: str):
    """Remove the agent's QBO refresh token (keeps client creds + realm)."""
    agent = _agents.get(name)
    if not agent:
        raise HTTPException(404, f"Agent '{name}' not found")
    store = _token_store(name)
    store.clear_tokens()
    return {"disconnected": True, "agent": name}


# ── Per-agent enable / disable ────────────────────────────────────────────────


@router.post("/agents/{name}/qbo/enable")
async def enable_agent_qbo(name: str):
    """Enable the QBO MCP server for an agent (adds it to .mcp.json).

    The MCP row env is MINIMAL — no secrets (spec §4): the stdio server loads
    client secret + rotating refresh token from the agent-scoped TokenStore at
    startup and on every refresh, so they can never go stale in the row.
    """
    import json as _json
    import os as _os

    agent = _agents.get(name)
    if not agent:
        raise HTTPException(404, f"Agent '{name}' not found")

    store = _token_store(name)
    if not store.is_configured():
        raise HTTPException(400, "QBO not configured. Save credentials first.")
    realm_id = store.get_realm_id() or ""
    if not realm_id:
        raise HTTPException(400, "QBO realm not connected. Complete OAuth first.")
    env = store.get_env()

    install_dir = _os.path.dirname(
        _os.path.dirname(_os.path.dirname(agent.working_dir))
    )
    venv_python = _os.path.join(install_dir, ".venv", "bin", "python")
    src_dir = _os.path.join(install_dir, "src")
    agents_db = _os.environ.get("PINKY_AGENTS_DB", "")

    mcp_path = _os.path.join(agent.working_dir, ".mcp.json")
    try:
        with open(mcp_path) as f:
            mcp_cfg = _json.load(f)
    except Exception:
        mcp_cfg = {"mcpServers": {}}

    mcp_cfg.setdefault("mcpServers", {})["pinky-qbo"] = {
        "command": venv_python,
        "args": [
            "-m",
            "pinky_qbo",
            "--agent",
            name,
            "--qbo-realm-id",
            realm_id,
            "--qbo-env",
            env,
        ],
        "cwd": src_dir,
        # MINIMAL env — NO secrets. Realm/env are also passed as args; the env
        # values are belt-and-suspenders for the loader, not credentials.
        "env": {
            "PINKY_AGENTS_DB": agents_db,
            "QBO_AGENT": name,
            "QBO_REALM_ID_EXPECTED": realm_id,
            "QBO_ENV_EXPECTED": env,
        },
    }
    with open(mcp_path, "w") as f:
        _json.dump(mcp_cfg, f, indent=2)

    _agents.set_agent_setting(name, "qbo_enabled", "true")
    return {"enabled": True, "agent": name}


@router.post("/agents/{name}/qbo/disable")
async def disable_agent_qbo(name: str):
    """Disable the QBO MCP server for an agent (removes it from .mcp.json)."""
    import json as _json
    import os as _os

    agent = _agents.get(name)
    if not agent:
        raise HTTPException(404, f"Agent '{name}' not found")

    mcp_path = _os.path.join(agent.working_dir, ".mcp.json")
    try:
        with open(mcp_path) as f:
            mcp_cfg = _json.load(f)
        mcp_cfg.get("mcpServers", {}).pop("pinky-qbo", None)
        with open(mcp_path, "w") as f:
            _json.dump(mcp_cfg, f, indent=2)
    except Exception:
        pass

    _agents.set_agent_setting(name, "qbo_enabled", "false")
    return {"disabled": True, "agent": name}


@router.get("/agents/{name}/qbo/enabled")
async def agent_qbo_enabled(name: str):
    """Check if the QBO server is present in the agent's .mcp.json."""
    import json as _json
    import os as _os

    agent = _agents.get(name)
    if not agent:
        raise HTTPException(404, f"Agent '{name}' not found")
    mcp_path = _os.path.join(agent.working_dir, ".mcp.json")
    enabled = False
    try:
        with open(mcp_path) as f:
            mcp_cfg = _json.load(f)
        enabled = "pinky-qbo" in mcp_cfg.get("mcpServers", {})
    except Exception:
        pass
    return {"agent": name, "qbo_enabled": enabled}
