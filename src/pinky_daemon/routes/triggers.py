"""Trigger endpoints + public webhook receiver.

Extracted from api.py. The `/hooks/{token}` public endpoint sits in
`_public_prefixes` (set by api.py's auth middleware) so the token in
the URL is the credential.

In-memory rate-limit buckets are module-level here. They were
process-local in api.py too — a server restart resets them.
"""

from __future__ import annotations

import json
import re as _re2
import time
from datetime import datetime
from datetime import timezone as _tz
from typing import Any, Awaitable, Callable

from fastapi import APIRouter, HTTPException, Request

from pinky_daemon.api_models import CreateTriggerRequest, UpdateTriggerRequest

router = APIRouter(tags=["triggers"])


# ── Shared dependency state ───────────────────────────────────────────────────

_trigger_store: Any = None
_agents: Any = None
_log: Callable[[str], None] | None = None
_wake_callback: Callable[[str, str, str], Awaitable[None]] | None = None


def set_dependencies(
    *,
    trigger_store,
    agents,
    log: Callable[[str], None],
    wake_callback: Callable[[str, str, str], Awaitable[None]],
) -> None:
    """Wire shared state for the triggers router."""
    global _trigger_store, _agents, _log, _wake_callback
    _trigger_store = trigger_store
    _agents = agents
    _log = log
    _wake_callback = wake_callback


# ── Rate-limit buckets ────────────────────────────────────────────────────────
# token -> sliding-60s timestamp list, IP -> sliding-60s timestamp list.
# Module-local on purpose — server restarts reset the windows.

_hook_rate_buckets: dict[str, list[float]] = {}
_hook_ip_buckets: dict[str, list[float]] = {}

_HOOK_IP_RATE_LIMIT = 20
_HOOK_IP_RATE_WINDOW = 60.0


def _check_hook_ip_rate_limit(request: Request, now: float) -> bool:
    """Return True if the client IP is within the webhook rate limit."""
    ip = (
        request.headers.get("x-forwarded-for", "").split(",")[0].strip()
        or (request.client.host if request.client else "unknown")
    )
    timestamps = _hook_ip_buckets.get(ip, [])
    timestamps = [t for t in timestamps if now - t < _HOOK_IP_RATE_WINDOW]
    if len(timestamps) >= _HOOK_IP_RATE_LIMIT:
        _hook_ip_buckets[ip] = timestamps
        _log(f"hooks: IP rate limited {ip} ({len(timestamps)} reqs in {_HOOK_IP_RATE_WINDOW}s)")
        return False
    timestamps.append(now)
    _hook_ip_buckets[ip] = timestamps
    return True


def _check_hook_rate_limit(token: str, now: float) -> bool:
    """Return True if the request is within the rate limit (60/min per token)."""
    window = 60.0
    limit = 60
    timestamps = _hook_rate_buckets.get(token, [])
    timestamps = [t for t in timestamps if now - t < window]
    if len(timestamps) >= limit:
        _hook_rate_buckets[token] = timestamps
        return False
    timestamps.append(now)
    _hook_rate_buckets[token] = timestamps
    return True


def _render_trigger_prompt(template: str, trigger_name: str, body: dict | None, body_raw: str) -> str:
    """Render a trigger prompt template with {{body.field}} interpolation."""
    timestamp = datetime.now(_tz.utc).isoformat()

    def _extract_path(obj, path: str) -> str:
        parts = path.split(".")
        current = obj
        for part in parts:
            if isinstance(current, dict):
                current = current.get(part)
            elif isinstance(current, list):
                try:
                    current = current[int(part)]
                except (ValueError, IndexError):
                    return ""
            else:
                return ""
        return str(current) if current is not None else ""

    ctx: dict = {
        "trigger_name": trigger_name,
        "timestamp": timestamp,
        "body_raw": body_raw,
        "body": body or {},
    }

    def replacer(match) -> str:
        expr = match.group(1).strip()
        if expr in ctx and expr != "body":
            return str(ctx[expr])
        if expr.startswith("body.") and body:
            return _extract_path(body, expr[5:])
        return ""

    if not template:
        snippet = body_raw[:2000] if body_raw else ""
        return f"Webhook trigger '{trigger_name}' fired at {timestamp}:\n\n{snippet}"

    return _re2.sub(r"\{\{([^}]+)\}\}", replacer, template)


# ── Trigger CRUD ──────────────────────────────────────────────────────────────


@router.get("/triggers")
async def list_all_triggers(agent_name: str = ""):
    """List all triggers, optionally filtered by agent_name."""
    all_triggers = _trigger_store.list(agent_name=agent_name or None)
    return {
        "triggers": [t.to_dict() for t in all_triggers],
        "count": len(all_triggers),
    }


@router.get("/agents/{agent_name}/triggers")
async def list_agent_triggers(agent_name: str):
    """List all triggers for a specific agent."""
    if not _agents.get(agent_name):
        raise HTTPException(404, f"Agent '{agent_name}' not found")
    agent_triggers = _trigger_store.list(agent_name=agent_name)
    return {
        "agent": agent_name,
        "triggers": [t.to_dict() for t in agent_triggers],
        "count": len(agent_triggers),
    }


@router.post("/agents/{agent_name}/triggers")
async def create_agent_trigger(agent_name: str, req: CreateTriggerRequest):
    """Create a new trigger for an agent."""
    if not _agents.get(agent_name):
        raise HTTPException(404, f"Agent '{agent_name}' not found")
    valid_types = {"webhook", "url", "file"}
    if req.trigger_type not in valid_types:
        raise HTTPException(400, f"trigger_type must be one of: {', '.join(sorted(valid_types))}")

    trigger = _trigger_store.create(
        agent_name=agent_name,
        name=req.name or req.trigger_type,
        trigger_type=req.trigger_type,
        url=req.url,
        method=req.method,
        condition=req.condition,
        condition_value=req.condition_value,
        file_path=req.file_path,
        interval_seconds=req.interval_seconds,
        prompt_template=req.prompt_template,
        enabled=req.enabled,
    )
    return trigger.to_dict(include_token=True)


@router.get("/agents/{agent_name}/triggers/{trigger_id}")
async def get_agent_trigger(agent_name: str, trigger_id: int):
    """Get a single trigger by ID."""
    trigger = _trigger_store.get(trigger_id)
    if not trigger or trigger.agent_name != agent_name:
        raise HTTPException(404, f"Trigger {trigger_id} not found")
    return trigger.to_dict()


@router.put("/agents/{agent_name}/triggers/{trigger_id}")
async def update_agent_trigger(agent_name: str, trigger_id: int, req: UpdateTriggerRequest):
    """Update a trigger's fields."""
    trigger = _trigger_store.get(trigger_id)
    if not trigger or trigger.agent_name != agent_name:
        raise HTTPException(404, f"Trigger {trigger_id} not found")
    updates = {k: v for k, v in req.model_dump().items() if v is not None}
    updated = _trigger_store.update(trigger_id, **updates)
    if not updated:
        raise HTTPException(404, f"Trigger {trigger_id} not found")
    return updated.to_dict()


@router.delete("/agents/{agent_name}/triggers/{trigger_id}")
async def delete_agent_trigger(agent_name: str, trigger_id: int):
    """Delete a trigger. For webhooks, the token is immediately invalidated."""
    trigger = _trigger_store.get(trigger_id)
    if not trigger or trigger.agent_name != agent_name:
        raise HTTPException(404, f"Trigger {trigger_id} not found")
    _trigger_store.delete(trigger_id)
    return {"ok": True}


@router.post("/agents/{agent_name}/triggers/{trigger_id}/rotate-token")
async def rotate_agent_trigger_token(agent_name: str, trigger_id: int):
    """Generate a new secret token for a webhook trigger."""
    trigger = _trigger_store.get(trigger_id)
    if not trigger or trigger.agent_name != agent_name:
        raise HTTPException(404, f"Trigger {trigger_id} not found")
    if trigger.trigger_type != "webhook":
        raise HTTPException(400, "Token rotation is only available for webhook triggers")
    new_token = _trigger_store.rotate_token(trigger_id)
    if not new_token:
        raise HTTPException(500, "Failed to rotate token")
    return {"token": new_token}


@router.post("/agents/{agent_name}/triggers/{trigger_id}/test")
async def test_agent_trigger(agent_name: str, trigger_id: int):
    """Manually fire a trigger to test it."""
    trigger = _trigger_store.get(trigger_id)
    if not trigger or trigger.agent_name != agent_name:
        raise HTTPException(404, f"Trigger {trigger_id} not found")
    if not _agents.get(agent_name):
        raise HTTPException(404, f"Agent '{agent_name}' not found")

    prompt = _render_trigger_prompt(
        trigger.prompt_template, trigger.name, None, "[test fire]"
    )
    try:
        await _wake_callback(agent_name, f"{agent_name}-main", prompt)
        _trigger_store.record_fire(trigger_id)
        agent_woken = True
    except Exception as e:
        _log(f"trigger test: wake failed for {agent_name}: {e}")
        agent_woken = False

    return {"fired": True, "prompt": prompt, "agent_woken": agent_woken}


# ── Public Webhook Receiver ───────────────────────────────────────────────────
# No auth middleware — token in path is the credential.
# /hooks/ prefix is in _public_prefixes (set in api.py) so auth middleware skips it.


@router.post("/hooks/{token}")
async def receive_webhook(token: str, request: Request):
    """Receive an inbound webhook and wake the associated agent."""
    now = time.time()

    # IP rate limit (anti-enumeration — runs before token lookup)
    if not _check_hook_ip_rate_limit(request, now):
        raise HTTPException(status_code=429, detail="rate limit exceeded")

    # Body size check
    body_bytes = await request.body()
    if len(body_bytes) > 1_048_576:
        raise HTTPException(status_code=413, detail="body too large")

    # Token lookup
    trigger = _trigger_store.get_by_token(token)
    if not trigger:
        raise HTTPException(status_code=404, detail="not found")

    # Rate limit: 60 requests per minute per token
    if not _check_hook_rate_limit(token, now):
        raise HTTPException(status_code=429, detail="rate limit exceeded")

    # Parse body
    body_raw = body_bytes.decode(errors="replace")
    body_json: dict | None = None
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        try:
            body_json = json.loads(body_raw)
        except Exception:
            body_json = None

    # Render prompt
    prompt = _render_trigger_prompt(
        trigger.prompt_template, trigger.name, body_json, body_raw
    )

    # Wake agent (fire-and-not-crash: log error but return 200)
    try:
        await _wake_callback(trigger.agent_name, f"{trigger.agent_name}-main", prompt)
    except Exception as e:
        _log(f"hooks: wake failed for {trigger.agent_name}: {e}")

    _trigger_store.record_fire(trigger.id)
    _log(f"hooks: trigger '{trigger.name}' fired for agent '{trigger.agent_name}'")

    return {"ok": True, "trigger_id": trigger.id, "agent": trigger.agent_name}
