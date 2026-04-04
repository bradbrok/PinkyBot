"""OpenClaw migration FastAPI router.

Endpoints:
    POST /api/migrate/openclaw/parse
        Upload workspace zip + optional openclaw.json + optional lock.json.
        Extracts to temp dir, parses all files, returns parse_id.

    POST /api/migrate/openclaw/preview
        Takes parse_id, runs Claude-assisted mapper, returns MigrationPreview.
        This is the slow step (multiple Claude calls).

    POST /api/migrate/openclaw/apply
        Takes parse_id + optional confirmed overrides.
        Creates agent synchronously, spawns background memory task.
        Returns MigrationResult.

    GET /api/migrate/openclaw/status/{task_id}
        Returns background memory import progress.

Parse state is stored in-memory (dict keyed by UUID). No persistence needed
for migration state — if the server restarts, user re-uploads. Temp dirs are
cleaned up after apply.

Dependency injection: call set_dependencies(agent_registry=...) from api.py
after the AgentRegistry is initialised. The memory_store is opened per-agent
in the importer, so it is not wired globally here.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, HTTPException, UploadFile
from pydantic import BaseModel, Field

from pinky_daemon.migration.importer import (
    MigrationPreview,
    apply_migration,
    build_preview,
    get_task_status,
)
from pinky_daemon.migration.parser import (
    OpenClawConfig,
    WorkspaceData,
    parse_clawhub_lock,
    parse_openclaw_json,
    parse_workspace,
)


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# ── Shared dependency state ───────────────────────────────────────────────────
# Populated by set_dependencies() called from create_api() in api.py.

_shared_agent_registry: Any = None
_shared_memory_store: Any = None


def set_dependencies(*, agent_registry: Any, memory_store: Any = None) -> None:
    """Wire shared instances so migration endpoints can access them.

    Call this from create_api() in api.py after stores are initialised:

        from pinky_daemon.migration.routes import router as migration_router, set_dependencies
        set_dependencies(agent_registry=agents)
        app.include_router(migration_router)
    """
    global _shared_agent_registry, _shared_memory_store
    _shared_agent_registry = agent_registry
    _shared_memory_store = memory_store


def _require_agent_registry() -> Any:
    """Return the shared AgentRegistry or raise a 500 if not wired."""
    if _shared_agent_registry is None:
        raise HTTPException(
            status_code=500,
            detail="Migration module not properly initialised — agent_registry missing",
        )
    return _shared_agent_registry


# ── In-memory parse store ─────────────────────────────────────────────────────
# Keyed by parse_id (UUID string).
# Value: {"workspace": WorkspaceData, "config": OpenClawConfig|None,
#         "clawhub_skills": list[str], "temp_dir": str, "preview": MigrationPreview|None}

_parse_store: dict[str, dict] = {}


# ── Router ─────────────────────────────────────────────────────────────────────

router = APIRouter(prefix="/api/migrate/openclaw", tags=["migration"])


# ── Request / response models ─────────────────────────────────────────────────


class PreviewRequest(BaseModel):
    """Request to build a migration preview from a previously parsed workspace."""

    parse_id: str


class ApplyRequest(BaseModel):
    """Request to apply a migration. User must have reviewed the preview first."""

    parse_id: str
    # Optional name override — replaces what was detected from IDENTITY.md
    agent_name: str = ""
    # Optional subset of memory draft indices to import (None = import all)
    confirmed_memory_ids: list[int] | None = None
    # Per-platform bot tokens — {platform: token_string}
    # Provided here so tokens never appear in the preview manifest (which is logged)
    tokens: dict[str, str] = Field(default_factory=dict)


class TaskStatusResponse(BaseModel):
    """Background memory import task status."""

    task_id: str
    total: int
    imported: int
    failed: int
    done: bool


class ParseResponse(BaseModel):
    """Result of the parse step."""

    parse_id: str
    agent_name: str
    agent_display_name: str
    has_soul: bool
    has_identity: bool
    has_heartbeat: bool
    has_memory: bool
    has_agents_md: bool
    has_openclaw_config: bool
    has_clawhub_lock: bool
    channel_platforms: list[str]
    clawhub_skill_count: int
    warnings: list[str]


# ── Endpoints ─────────────────────────────────────────────────────────────────


@router.post("/parse", response_model=ParseResponse, summary="Upload and parse OpenClaw workspace")
async def parse_endpoint(
    workspace_zip: UploadFile = File(..., description="OpenClaw workspace directory as .zip"),
    openclaw_json: UploadFile | None = File(None, description="~/.openclaw/openclaw.json (optional)"),
    clawhub_lock: UploadFile | None = File(None, description=".clawhub/lock.json (optional)"),
) -> ParseResponse:
    """Parse an uploaded OpenClaw workspace zip and optional config files.

    Extracts the zip to a temp directory, reads all Markdown files,
    and optionally parses openclaw.json and lock.json if provided.

    Returns a parse_id to use in subsequent /preview and /apply calls.
    The temp directory is kept alive until /apply is called or server restarts.
    """
    temp_dir = tempfile.mkdtemp(prefix="pinky_migration_")
    parse_id = str(uuid.uuid4())
    warnings: list[str] = []

    try:
        # Save and parse workspace zip
        zip_path = Path(temp_dir) / "workspace.zip"
        content = await workspace_zip.read()
        zip_path.write_bytes(content)

        try:
            workspace = parse_workspace(str(zip_path))
        except Exception as e:
            shutil.rmtree(temp_dir, ignore_errors=True)
            raise HTTPException(
                status_code=400,
                detail=f"Failed to parse workspace zip: {e}",
            ) from e

        # Parse optional openclaw.json
        config: OpenClawConfig | None = None
        if openclaw_json:
            config_path = Path(temp_dir) / "openclaw.json"
            config_content = await openclaw_json.read()
            config_path.write_bytes(config_content)
            try:
                config = parse_openclaw_json(str(config_path))
            except Exception as e:
                warnings.append(f"Could not parse openclaw.json: {e} — continuing without it")

        # Parse optional clawhub lock
        clawhub_skills: list[str] = []
        if clawhub_lock:
            lock_path = Path(temp_dir) / "clawhub_lock.json"
            lock_content = await clawhub_lock.read()
            lock_path.write_bytes(lock_content)
            try:
                clawhub_skills = parse_clawhub_lock(str(lock_path))
            except Exception as e:
                warnings.append(f"Could not parse clawhub lock.json: {e} — continuing without skill list")

        # Store in parse store for use by /preview and /apply
        _parse_store[parse_id] = {
            "workspace": workspace,
            "config": config,
            "clawhub_skills": clawhub_skills,
            "temp_dir": temp_dir,
            "preview": None,
        }

        _log(f"migration.parse: parse_id={parse_id}, agent='{workspace.agent_name}'")

        return ParseResponse(
            parse_id=parse_id,
            agent_name=workspace.agent_name,
            agent_display_name=workspace.agent_display_name,
            has_soul=bool(workspace.soul_md),
            has_identity=bool(workspace.identity_md),
            has_heartbeat=bool(workspace.heartbeat_md),
            has_memory=bool(workspace.memory_md),
            has_agents_md=bool(workspace.agents_md),
            has_openclaw_config=config is not None,
            has_clawhub_lock=bool(clawhub_skills),
            channel_platforms=[c.platform for c in (config.channels if config else [])],
            clawhub_skill_count=len(clawhub_skills),
            warnings=warnings,
        )

    except HTTPException:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise
    except Exception as e:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise HTTPException(status_code=500, detail=f"Unexpected error during parse: {e}") from e


@router.post("/preview", summary="Build migration preview with Claude-assisted processing")
async def preview_endpoint(req: PreviewRequest) -> dict:
    """Build a full MigrationPreview from a previously parsed workspace.

    Runs all Claude-assisted mapper functions (soul/boundaries split,
    schedule parsing, memory classification, directive splitting).
    This is the slow step — expect 5–30 seconds depending on workspace size.

    Returns a structured preview manifest. The frontend should render
    ok/warning/error status badges per item before asking the user to confirm.
    """
    state = _parse_store.get(req.parse_id)
    if not state:
        raise HTTPException(
            status_code=404,
            detail=f"parse_id '{req.parse_id}' not found — please re-upload the workspace",
        )

    workspace: WorkspaceData = state["workspace"]
    config: OpenClawConfig | None = state["config"]
    clawhub_skills: list[str] = state["clawhub_skills"]

    try:
        preview = build_preview(
            workspace,
            config,
            parse_id=req.parse_id,
            clawhub_skills=clawhub_skills,
        )
    except Exception as e:
        _log(f"migration.preview: error for parse_id={req.parse_id}: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to build migration preview: {e}",
        ) from e

    # Cache the full preview (including memory drafts) for the apply step
    state["preview"] = preview

    return preview.to_dict()


@router.post("/apply", summary="Apply migration — creates agent and imports memories")
async def apply_endpoint(req: ApplyRequest) -> dict:
    """Apply a confirmed migration preview.

    Synchronous: creates agent, writes directives/schedules/tokens to DB.
    Background async: chunks + embeds + batch-inserts memories into memory store.

    Returns MigrationResult with:
    - agent_name: the created agent's name (may differ from requested if conflict)
    - task_id: UUID to poll GET /status/{task_id} for memory import progress
    - items_created: summary counts per category
    - warnings: non-blocking issues encountered

    The temp directory is cleaned up after this call.
    """
    state = _parse_store.get(req.parse_id)
    if not state:
        raise HTTPException(
            status_code=404,
            detail=f"parse_id '{req.parse_id}' not found — please re-upload the workspace",
        )

    preview: MigrationPreview | None = state.get("preview")
    if not preview:
        raise HTTPException(
            status_code=400,
            detail="No preview found for this parse_id — call /preview first",
        )

    # Allow caller to override the agent name detected from IDENTITY.md
    if req.agent_name:
        preview.identity.name.value = req.agent_name

    agent_registry = _require_agent_registry()

    try:
        result = apply_migration(
            preview,
            agent_registry,
            _shared_memory_store,   # Can be None — memory import degrades gracefully
            confirmed_memory_ids=req.confirmed_memory_ids,
        )
    except Exception as e:
        _log(f"migration.apply: error for parse_id={req.parse_id}: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Migration failed: {e}",
        ) from e

    # Write platform tokens — passed in request so they never appear in preview manifest
    tokens_written = 0
    for platform, token in req.tokens.items():
        if token:
            try:
                agent_registry.set_token(result.agent_name, platform, token)
                tokens_written += 1
            except Exception as e:
                _log(f"migration.apply: failed to write token for {platform}: {e}")
                result.warnings.append(f"Token for {platform} could not be saved: {e}")
    result.items_created["tokens"] = tokens_written

    # Clean up temp directory and remove from parse store
    temp_dir = state.get("temp_dir", "")
    if temp_dir:
        shutil.rmtree(temp_dir, ignore_errors=True)
    _parse_store.pop(req.parse_id, None)

    _log(
        f"migration.apply: done — agent='{result.agent_name}', "
        f"task_id={result.task_id!r}, items={result.items_created}"
    )

    return {
        "agent_name": result.agent_name,
        "task_id": result.task_id,
        "items_created": result.items_created,
        "warnings": result.warnings,
    }


@router.get(
    "/status/{task_id}",
    response_model=TaskStatusResponse,
    summary="Poll background memory import progress",
)
async def status_endpoint(task_id: str) -> TaskStatusResponse:
    """Get the current status of a background memory import task.

    Poll after /apply returns a task_id. When done=true, the import is complete.
    Check failed > 0 for partial failure. Total = 0 means no memories were queued.
    """
    task = get_task_status(task_id)
    if task is None:
        raise HTTPException(
            status_code=404,
            detail=f"Task '{task_id}' not found — it may have expired or never existed",
        )
    return TaskStatusResponse(
        task_id=task_id,
        total=task.get("total", 0),
        imported=task.get("imported", 0),
        failed=task.get("failed", 0),
        done=task.get("done", False),
    )
