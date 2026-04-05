"""Pinky Hub API — public hub for the Pinky ecosystem at pinkybot.ai.

Manages registered agent instances (users' local Pinky daemons), aggregates
public presentations, and provides the API for the pinkybot.ai website.

Usage:
    python -m pinky_hub
    curl http://localhost:8889/
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from pinky_hub.hub_store import HubStore

HUB_VERSION = "0.1.0"


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# ── Request Models ────────────────────────────────────────────


class RegisterInstanceRequest(BaseModel):
    label: str
    url: str
    api_key: str  # plain key; hub stores it as-is for MVP
    owner_email: str = ""
    owner_name: str = ""


class SyncPresentationsRequest(BaseModel):
    api_key: str  # used to authenticate the sync request


# ── Daemon HTTP helpers ───────────────────────────────────────


def _daemon_get(url: str, api_key: str, timeout: int = 10) -> Any:
    """GET a URL on a remote daemon with Pinky auth header. Returns parsed JSON."""
    req = urllib.request.Request(url, headers={"X-Pinky-Token": api_key})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        raise HTTPException(
            status_code=502, detail=f"Daemon returned HTTP {exc.code}: {exc.reason}"
        ) from exc
    except urllib.error.URLError as exc:
        raise HTTPException(
            status_code=502, detail=f"Could not reach daemon: {exc.reason}"
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=502, detail=f"Daemon communication error: {exc}"
        ) from exc


# ── App factory ───────────────────────────────────────────────


def create_hub_app(db_path: str = "data/hub.db") -> FastAPI:
    store = HubStore(db_path=db_path)

    app = FastAPI(
        title="Pinky Hub",
        description="Public hub for the Pinky ecosystem — instance registry and presentation aggregation",
        version=HUB_VERSION,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ── Root ──────────────────────────────────────────────────

    @app.get("/")
    def hub_info() -> dict:
        """Hub status and aggregate counts."""
        return {
            "name": "pinky-hub",
            "version": HUB_VERSION,
            "instance_count": store.count_instances(active_only=True),
            "presentation_count": store.count_presentations(),
            "timestamp": time.time(),
        }

    # ── Instance registration ─────────────────────────────────

    @app.post("/register")
    def register_instance(req: RegisterInstanceRequest) -> dict:
        """Register a new Pinky daemon instance.

        Validates the daemon is reachable by calling GET {url}/api with the
        provided api_key. On success, stores the instance and returns its record.
        """
        url = req.url.rstrip("/")

        # Validate: daemon must respond to GET /api with a "name" field
        try:
            daemon_info = _daemon_get(f"{url}/api", req.api_key)
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(
                status_code=502, detail=f"Daemon validation failed: {exc}"
            ) from exc

        if not isinstance(daemon_info, dict) or "name" not in daemon_info:
            raise HTTPException(
                status_code=502,
                detail="Daemon GET /api did not return a valid response (missing 'name' field)",
            )

        # Check for duplicate URL
        existing = store.get_instance_by_url(url)
        if existing and existing.is_active:
            raise HTTPException(
                status_code=409,
                detail=f"An active instance is already registered for URL: {url}",
            )

        instance = store.register_instance(
            label=req.label,
            url=url,
            api_key=req.api_key,  # MVP: stored plaintext; encrypt in production
            owner_email=req.owner_email,
            owner_name=req.owner_name,
        )
        _log(f"[hub] registered instance #{instance.id}: {req.label} @ {url}")
        return {"instance_id": instance.id, **instance.to_dict()}

    # ── Instance list ─────────────────────────────────────────

    @app.get("/instances")
    def list_instances() -> list[dict]:
        """List all active registered instances (no api_keys in response)."""
        return [inst.to_dict() for inst in store.list_instances(active_only=True)]

    # ── Sync presentations ────────────────────────────────────

    @app.post("/instances/{instance_id}/sync")
    def sync_presentations(instance_id: int, req: SyncPresentationsRequest) -> dict:
        """Pull public presentations from a registered daemon and upsert them here.

        Authenticates the request using the provided api_key (must match stored key).
        Then calls GET {daemon_url}/presentations?limit=100 on the daemon to fetch
        its presentations, upserting each into public_presentations.
        """
        instance = store.get_instance_by_id(instance_id)
        if not instance:
            raise HTTPException(status_code=404, detail="Instance not found")
        if not instance.is_active:
            raise HTTPException(status_code=403, detail="Instance is inactive")

        # Authenticate: caller must know the api_key
        if req.api_key != instance.api_key:
            raise HTTPException(status_code=401, detail="Invalid api_key")

        daemon_url = instance.url
        presentations_url = f"{daemon_url}/presentations?limit=100"

        data = _daemon_get(presentations_url, instance.api_key)

        # The daemon may return a list directly or wrap in {"presentations": [...]}
        if isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            items = data.get("presentations", data.get("items", []))
        else:
            items = []

        synced = 0
        for item in items:
            try:
                store.upsert_presentation(
                    instance_id=instance_id,
                    remote_id=int(item["id"]),
                    title=item.get("title", "Untitled"),
                    description=item.get("description", ""),
                    created_by=item.get("created_by", ""),
                    share_token=item.get("share_token", ""),
                    tags=item.get("tags", []),
                    version=int(item.get("current_version", item.get("version", 1))),
                )
                synced += 1
            except (KeyError, ValueError, TypeError) as exc:
                _log(f"[hub] skipping malformed presentation item: {exc}")

        store.update_last_seen(instance_id)
        _log(f"[hub] synced {synced} presentations from instance #{instance_id}")
        return {"synced": synced, "instance_id": instance_id}

    # ── Public presentations ──────────────────────────────────

    @app.get("/public/presentations")
    def list_public_presentations(limit: int = 50, offset: int = 0) -> list[dict]:
        """List aggregated public presentations across all active instances."""
        limit = min(limit, 200)  # cap at 200
        return [p.to_dict() for p in store.list_public_presentations(limit=limit, offset=offset)]

    @app.get("/public/presentations/{share_token}")
    def get_public_presentation(share_token: str) -> dict:
        """Get a single public presentation by share token, including the daemon URL for redirect."""
        pres = store.get_presentation_by_token(share_token)
        if not pres:
            raise HTTPException(status_code=404, detail="Presentation not found")
        instance = store.get_instance_by_id(pres.instance_id)
        if not instance or not instance.is_active:
            raise HTTPException(status_code=404, detail="Instance not available")
        return {
            **pres.to_dict(),
            "view_url": f"{instance.url}/p/{share_token}",
        }

    # ── Heartbeat ─────────────────────────────────────────────

    @app.post("/instances/{instance_id}/heartbeat")
    def heartbeat(instance_id: int) -> dict:
        """Update last_seen_at for a registered instance."""
        instance = store.get_instance_by_id(instance_id)
        if not instance:
            raise HTTPException(status_code=404, detail="Instance not found")
        store.update_last_seen(instance_id)
        return {"ok": True, "instance_id": instance_id, "last_seen_at": time.time()}

    # ── Deactivate ────────────────────────────────────────────

    @app.delete("/instances/{instance_id}")
    def deactivate_instance(instance_id: int) -> dict:
        """Deactivate a registered instance and remove its public presentations."""
        instance = store.get_instance_by_id(instance_id)
        if not instance:
            raise HTTPException(status_code=404, detail="Instance not found")
        store.delete_presentations_for_instance(instance_id)
        store.deactivate_instance(instance_id)
        _log(f"[hub] deactivated instance #{instance_id}")
        return {"ok": True, "instance_id": instance_id}

    return app
