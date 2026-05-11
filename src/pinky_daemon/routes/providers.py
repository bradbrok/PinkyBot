"""Global provider, model registry, and bot-token routes.

Extracted from api.py. All endpoints in this file act on the global
agent-registry DB (providers table, models table, bot tokens), not on
per-agent state — they live together because they share the same
`agents: AgentRegistry` dependency and were defined contiguously in
the original file.
"""

from __future__ import annotations

import os
import time
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException

router = APIRouter(tags=["providers"])


# ── Shared dependency state ───────────────────────────────────────────────────

_agents: Any = None


def set_dependencies(*, agents) -> None:
    """Wire the AgentRegistry for provider/model/bot-token routes."""
    global _agents
    _agents = agents


# ── Local helpers ─────────────────────────────────────────────────────────────


def _provider_row_to_dict(row) -> dict:
    return {
        "id": row[0],
        "name": row[1],
        "preset": row[2],
        "provider_url": row[3],
        "provider_key": row[4],
        "provider_model": row[5],
        "created_at": row[6],
        "updated_at": row[7],
    }


# ── Global Providers ──────────────────────────────────────────────────────────


@router.get("/providers")
async def list_providers():
    """List all global named providers."""
    rows = _agents._db.execute(
        "SELECT id, name, preset, provider_url, provider_key, provider_model, created_at, updated_at "
        "FROM providers ORDER BY name"
    ).fetchall()
    return [_provider_row_to_dict(r) for r in rows]


@router.post("/providers")
async def create_provider(req: dict):
    """Create a new global named provider."""
    name = (req.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "name is required")
    provider_url = (req.get("provider_url") or "").strip()
    if not provider_url:
        raise HTTPException(400, "provider_url is required")
    now = time.time()
    provider_id = uuid.uuid4().hex[:12]
    _agents._db.execute(
        "INSERT INTO providers (id, name, preset, provider_url, provider_key, provider_model, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            provider_id,
            name,
            (req.get("preset") or "").strip(),
            provider_url,
            (req.get("provider_key") or "").strip(),
            (req.get("provider_model") or "").strip(),
            now,
            now,
        ),
    )
    _agents._db.commit()
    row = _agents._db.execute(
        "SELECT id, name, preset, provider_url, provider_key, provider_model, created_at, updated_at "
        "FROM providers WHERE id=?",
        (provider_id,),
    ).fetchone()
    return _provider_row_to_dict(row)


@router.put("/providers/{provider_id}")
async def update_provider(provider_id: str, req: dict):
    """Update a global named provider."""
    row = _agents._db.execute(
        "SELECT id FROM providers WHERE id=?", (provider_id,)
    ).fetchone()
    if not row:
        raise HTTPException(404, f"Provider '{provider_id}' not found")
    updates: dict = {}
    for field in ("name", "preset", "provider_url", "provider_key", "provider_model"):
        if field in req:
            updates[field] = (req[field] or "").strip()
    if not updates:
        raise HTTPException(400, "No fields to update")
    updates["updated_at"] = time.time()
    set_clause = ", ".join(f"{k}=?" for k in updates)
    _agents._db.execute(
        f"UPDATE providers SET {set_clause} WHERE id=?",
        list(updates.values()) + [provider_id],
    )
    _agents._db.commit()
    row = _agents._db.execute(
        "SELECT id, name, preset, provider_url, provider_key, provider_model, created_at, updated_at "
        "FROM providers WHERE id=?",
        (provider_id,),
    ).fetchone()
    return _provider_row_to_dict(row)


@router.delete("/providers/{provider_id}")
async def delete_provider(provider_id: str):
    """Delete a global named provider."""
    cursor = _agents._db.execute("DELETE FROM providers WHERE id=?", (provider_id,))
    _agents._db.commit()
    if cursor.rowcount == 0:
        raise HTTPException(404, f"Provider '{provider_id}' not found")
    return {"deleted": True, "id": provider_id}


# ── Model Registry ────────────────────────────────────────────────────────────


@router.get("/models")
async def list_models(provider: str = "", active_only: bool = True):
    """List available AI models."""
    return _agents.list_models(provider=provider, active_only=active_only)


@router.get("/models/{model_id:path}")
async def get_model(model_id: str):
    """Get a specific model by ID."""
    model = _agents.get_model(model_id)
    if not model:
        raise HTTPException(404, f"Model '{model_id}' not found")
    return model


@router.post("/models")
async def add_model(req: dict):
    """Add or update a model in the registry."""
    provider = (req.get("provider") or "").strip()
    model_id = (req.get("model_id") or "").strip()
    if not provider or not model_id:
        raise HTTPException(400, "provider and model_id are required")
    return _agents.add_model(
        provider=provider,
        model_id=model_id,
        display_name=req.get("display_name", ""),
        description=req.get("description", ""),
        tier=req.get("tier", ""),
        context_window=req.get("context_window", 200_000),
        is_1m=req.get("is_1m", False),
        input_price=req.get("input_price", 0),
        output_price=req.get("output_price", 0),
        cached_input_price=req.get("cached_input_price", 0),
        supports_thinking=req.get("supports_thinking", True),
        sort_order=req.get("sort_order", 100),
    )


@router.delete("/models/{model_id:path}")
async def delete_model(model_id: str):
    """Soft-delete a model (deactivate)."""
    if not _agents.delete_model(model_id):
        raise HTTPException(404, f"Model '{model_id}' not found")
    return {"deleted": True, "id": model_id}


@router.post("/models/sync")
async def sync_models():
    """Sync models from the Anthropic API. Auto-discovers new models."""
    import httpx as _httpx
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise HTTPException(400, "ANTHROPIC_API_KEY not set")
    try:
        async with _httpx.AsyncClient() as client:
            resp = await client.get(
                "https://api.anthropic.com/v1/models",
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                },
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        raise HTTPException(502, f"Failed to fetch models from Anthropic: {e}") from e

    added = []
    for m in data.get("data", []):
        mid = m.get("id", "")
        if not mid:
            continue
        # Skip non-Claude models
        if not mid.startswith("claude-"):
            continue
        # Determine tier from model name
        tier = "unknown"
        if "opus" in mid:
            tier = "opus"
        elif "sonnet" in mid:
            tier = "sonnet"
        elif "haiku" in mid:
            tier = "haiku"
        # Check if already exists
        existing = _agents.get_model(mid)
        if existing:
            continue
        # Determine display name
        display = m.get("display_name", mid)
        _agents.add_model(
            provider="anthropic",
            model_id=mid,
            display_name=display,
            description="Auto-discovered from Anthropic API",
            tier=tier,
            context_window=200_000,
            is_1m=False,
            supports_thinking=True,
            sort_order=100,
        )
        added.append(mid)

    return {"synced": len(added), "new_models": added}


# ── Global Bot Tokens ─────────────────────────────────────────────────────────


@router.get("/bot-tokens")
async def list_bot_tokens():
    """List all global bot tokens (token values redacted)."""
    return _agents.list_bot_tokens()


@router.post("/bot-tokens")
async def create_bot_token(req: dict):
    """Create a new global bot token."""
    name = (req.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "name is required")
    platform = (req.get("platform") or "telegram").strip()
    token = (req.get("token") or "").strip()
    return _agents.create_bot_token(name, platform, token)


@router.put("/bot-tokens/{token_id}")
async def update_bot_token(token_id: str, req: dict):
    """Update a global bot token."""
    existing = _agents.get_bot_token(token_id)
    if not existing:
        raise HTTPException(404, f"Bot token '{token_id}' not found")
    kwargs = {}
    for field in ("name", "platform", "token"):
        if field in req:
            kwargs[field] = (req[field] or "").strip()
    return _agents.update_bot_token(token_id, **kwargs)


@router.delete("/bot-tokens/{token_id}")
async def delete_bot_token(token_id: str):
    """Delete a global bot token. Clears refs in agent tokens."""
    if not _agents.delete_bot_token(token_id):
        raise HTTPException(404, f"Bot token '{token_id}' not found")
    return {"deleted": True, "id": token_id}
