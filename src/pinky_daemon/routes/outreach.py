"""Outreach platform configuration routes.

Extracted from api.py. Manages messaging-platform credentials and
toggles (Telegram, Discord, Slack, etc.) — distinct from the broker
*sending* endpoints (`/broker/*`) which still live in api.py.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from pinky_daemon.api_models import ConfigurePlatformRequest

router = APIRouter(tags=["outreach"])


# ── Shared dependency state ───────────────────────────────────────────────────

_outreach_config: Any = None


def set_dependencies(*, outreach_config) -> None:
    """Wire shared state for the outreach router."""
    global _outreach_config
    _outreach_config = outreach_config


# ── Routes ────────────────────────────────────────────────────────────────────


@router.get("/outreach/platforms")
async def list_platforms():
    """List all configured outreach platforms."""
    result = _outreach_config.list()
    return {"platforms": [p.to_dict() for p in result], "count": len(result)}


@router.get("/outreach/platforms/{platform}")
async def get_platform(platform: str):
    """Get platform configuration (token is never exposed)."""
    config = _outreach_config.get(platform)
    if not config:
        raise HTTPException(404, f"Platform '{platform}' not configured")
    return config.to_dict()


@router.put("/outreach/platforms/{platform}")
async def configure_platform(platform: str, req: ConfigurePlatformRequest):
    """Configure or update a messaging platform.

    Set token, enabled state, and platform-specific settings.
    Token is stored securely and never returned in API responses.
    """
    try:
        config = _outreach_config.configure(
            platform,
            token=req.token,
            enabled=req.enabled,
            settings=req.settings,
        )
        return config.to_dict()
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.post("/outreach/platforms/{platform}/enable")
async def enable_platform(platform: str):
    """Enable an outreach platform."""
    if not _outreach_config.enable(platform):
        raise HTTPException(404, f"Platform '{platform}' not configured")
    return {"enabled": True, "platform": platform}


@router.post("/outreach/platforms/{platform}/disable")
async def disable_platform(platform: str):
    """Disable an outreach platform."""
    if not _outreach_config.disable(platform):
        raise HTTPException(404, f"Platform '{platform}' not configured")
    return {"disabled": True, "platform": platform}


@router.post("/outreach/platforms/{platform}/test")
async def test_platform(platform: str):
    """Test connectivity to a platform.

    Attempts to call the platform's API with the stored token
    and returns success/error info.
    """
    return _outreach_config.test_connection(platform)


@router.delete("/outreach/platforms/{platform}")
async def delete_platform(platform: str):
    """Remove a platform configuration entirely."""
    deleted = _outreach_config.delete(platform)
    if not deleted:
        raise HTTPException(404, f"Platform '{platform}' not configured")
    return {"deleted": True, "platform": platform}
