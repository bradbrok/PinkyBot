"""OpenClaw → PinkyBot migration orchestrator.

Two main entry points:
    build_preview(workspace, config) → MigrationPreview
        Runs the Claude-assisted mapper on workspace data. No DB writes.
        Returns a structured preview manifest the user can review/approve.

    apply_migration(preview, agent_registry, memory_store) → MigrationResult
        Creates the agent in DB, writes tokens/schedules/directives/skills,
        and spawns a background task for memory embedding + batch insert.

Background memory task:
    - Chunks MEMORY.md into Reflection records (already classified in preview)
    - Batch inserts via ReflectionStore.insert()
    - Updates a shared task_status dict keyed by task_id

MigrationPreview encodes per-item status (ok / warning / error) so the
frontend can render ✅/⚠️/❌ badges before the user commits.
"""

from __future__ import annotations

import asyncio
import sys
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from pinky_daemon.migration.mapper import (
    ReflectionDraft,
    classify_memories,
    parse_heartbeat_schedules,
    split_directives,
    split_soul_boundaries,
    translate_model,
)
from pinky_daemon.migration.parser import (
    SUPPORTED_PLATFORMS,
    OpenClawConfig,
    WorkspaceData,
)


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# ── Status codes ──────────────────────────────────────────────────────────────

STATUS_OK = "ok"
STATUS_WARNING = "warning"
STATUS_ERROR = "error"


# ── Preview dataclasses ────────────────────────────────────────────────────────


@dataclass
class PreviewItem:
    """One reviewable item in the migration preview."""

    label: str = ""                 # Human-friendly label
    value: str = ""                 # The value that will be imported
    status: str = STATUS_OK         # ok | warning | error
    note: str = ""                  # Explanation for warning/error badges
    original: str = ""              # Original OpenClaw value (for diff view)


@dataclass
class IdentitySection:
    """Agent identity fields preview."""

    name: PreviewItem = field(default_factory=PreviewItem)
    display_name: PreviewItem = field(default_factory=PreviewItem)
    role: PreviewItem = field(default_factory=PreviewItem)
    model: PreviewItem = field(default_factory=PreviewItem)
    soul_preview: str = ""          # First 500 chars of extracted soul
    boundaries_preview: str = ""    # First 500 chars of extracted boundaries
    soul_full: str = ""             # Complete extracted soul text
    boundaries_full: str = ""       # Complete extracted boundaries text
    users_md: str = ""              # USER.md content (verbatim)


@dataclass
class ConnectionItem:
    """A platform connection preview."""

    platform: str = ""
    status: str = STATUS_OK
    note: str = ""
    has_token: bool = False
    allow_from: list[str] = field(default_factory=list)


@dataclass
class ConnectionsSection:
    """Platform connections preview."""

    items: list[ConnectionItem] = field(default_factory=list)


@dataclass
class MemorySection:
    """Memory import preview."""

    total_count: int = 0
    sample_entries: list[dict] = field(default_factory=list)   # First 5 items as dicts
    status: str = STATUS_OK
    note: str = ""
    drafts: list[ReflectionDraft] = field(default_factory=list)  # Full list (not serialised)


@dataclass
class ScheduleItem:
    """A single schedule preview."""

    name: str = ""
    description: str = ""
    cron: str = ""
    prompt: str = ""
    timezone: str = "America/Los_Angeles"
    status: str = STATUS_OK
    note: str = ""


@dataclass
class DirectiveItem:
    """A single directive preview."""

    directive: str = ""
    priority: int = 50
    status: str = STATUS_OK
    note: str = ""


@dataclass
class AutomationSection:
    """Schedules + directives preview."""

    schedules: list[ScheduleItem] = field(default_factory=list)
    directives: list[DirectiveItem] = field(default_factory=list)


@dataclass
class SkillItem:
    """A ClawHub skill preview."""

    name: str = ""
    status: str = STATUS_OK         # ok = matched in PinkyBot; warning = directive-only; error = unsupported
    note: str = ""
    has_env_keys: list[str] = field(default_factory=list)   # Env var names that need re-entry


@dataclass
class SkillsSection:
    """Skills import preview."""

    items: list[SkillItem] = field(default_factory=list)


@dataclass
class MigrationPreview:
    """Full migration preview manifest returned by build_preview().

    This is what the frontend renders before the user confirms. All
    heavy Claude work is done here — apply_migration() is pure DB writes.
    """

    parse_id: str = ""              # UUID key in the in-memory parse store
    identity: IdentitySection = field(default_factory=IdentitySection)
    connections: ConnectionsSection = field(default_factory=ConnectionsSection)
    memory: MemorySection = field(default_factory=MemorySection)
    automation: AutomationSection = field(default_factory=AutomationSection)
    skills: SkillsSection = field(default_factory=SkillsSection)
    warnings: list[str] = field(default_factory=list)   # Global warnings
    errors: list[str] = field(default_factory=list)     # Blocking errors
    memory_store_available: bool = True  # False → memories won't import; show banner in UI

    def to_dict(self) -> dict:
        """Serialise preview to a JSON-safe dict (excludes full drafts list)."""
        return {
            "parse_id": self.parse_id,
            "identity": {
                "name": vars(self.identity.name),
                "display_name": vars(self.identity.display_name),
                "role": vars(self.identity.role),
                "model": vars(self.identity.model),
                "soul_preview": self.identity.soul_preview,
                "boundaries_preview": self.identity.boundaries_preview,
                "users_md": self.identity.users_md,
            },
            "connections": {
                "items": [vars(c) for c in self.connections.items],
            },
            "memory": {
                "total_count": self.memory.total_count,
                "sample_entries": self.memory.sample_entries,
                "status": self.memory.status,
                "note": self.memory.note,
            },
            "automation": {
                "schedules": [vars(s) for s in self.automation.schedules],
                "directives": [vars(d) for d in self.automation.directives],
            },
            "skills": {
                "items": [vars(s) for s in self.skills.items],
            },
            "warnings": self.warnings,
            "errors": self.errors,
            "memory_store_available": self.memory_store_available,
        }


# ── Migration result ───────────────────────────────────────────────────────────


@dataclass
class MigrationResult:
    """Result returned by apply_migration()."""

    agent_name: str = ""
    task_id: str = ""               # Background memory import task UUID
    items_created: dict = field(default_factory=dict)   # {"directives": 5, "schedules": 2, ...}
    warnings: list[str] = field(default_factory=list)


# ── Background task status registry ───────────────────────────────────────────

# In-memory task status — keyed by task_id (UUID string)
# Structure: {"total": int, "imported": int, "failed": int, "done": bool}
_task_status: dict[str, dict] = {}
_task_status_lock: asyncio.Lock | None = None  # Created lazily (needs running event loop)


def _get_task_lock() -> asyncio.Lock:
    """Return (or create) the asyncio.Lock for _task_status updates."""
    global _task_status_lock
    if _task_status_lock is None:
        _task_status_lock = asyncio.Lock()
    return _task_status_lock


def get_task_status(task_id: str) -> dict | None:
    """Get current status of a background memory import task (returns a copy)."""
    status = _task_status.get(task_id)
    return dict(status) if status is not None else None


# ── Preview builder ────────────────────────────────────────────────────────────


def build_preview(
    workspace: WorkspaceData,
    config: OpenClawConfig | None,
    *,
    parse_id: str = "",
    clawhub_skills: list[str] | None = None,
) -> MigrationPreview:
    """Build a full MigrationPreview from parsed workspace + optional config.

    Runs all Claude-assisted mapper functions. This is the slow/expensive step
    (several Claude calls). The result is stored in the parse store and returned
    to the frontend for review.

    Args:
        workspace: Parsed WorkspaceData from parser.parse_workspace()
        config: Parsed OpenClawConfig from parser.parse_openclaw_json() — can be None
                if user didn't upload openclaw.json
        parse_id: UUID key to embed in the preview (for apply step correlation)
        clawhub_skills: Skill names from .clawhub/lock.json — can be None

    Returns a MigrationPreview ready for serialisation and frontend rendering.
    """
    preview = MigrationPreview(parse_id=parse_id)

    _build_identity_section(preview, workspace, config)
    _build_connections_section(preview, config)
    _build_memory_section(preview, workspace)
    _build_automation_section(preview, workspace)
    _build_skills_section(preview, clawhub_skills or [], config)

    return preview


def _build_identity_section(
    preview: MigrationPreview,
    workspace: WorkspaceData,
    config: OpenClawConfig | None,
) -> None:
    """Populate identity section: name, role, model, soul/boundaries split."""
    identity = preview.identity

    # Agent name — from IDENTITY.md or fallback slug
    agent_name = workspace.agent_name or "imported-agent"
    identity.name = PreviewItem(
        label="Agent name",
        value=agent_name,
        original=workspace.agent_display_name,
        status=STATUS_OK if workspace.agent_name else STATUS_WARNING,
        note="" if workspace.agent_name else "No name found in IDENTITY.md — using 'imported-agent' placeholder",
    )
    identity.display_name = PreviewItem(
        label="Display name",
        value=workspace.agent_display_name or agent_name,
        status=STATUS_OK,
    )
    identity.role = PreviewItem(
        label="Role",
        value=workspace.agent_role or "sidekick",
        status=STATUS_OK if workspace.agent_role else STATUS_WARNING,
        note="" if workspace.agent_role else "No role found — defaulting to 'sidekick'",
    )

    # Model translation
    if config and config.model:
        model_str, provider = translate_model(config.model)
        model_status = STATUS_OK
        model_note = ""
        if provider == "custom":
            model_status = STATUS_WARNING
            model_note = f"Unknown model '{config.model}' — imported as-is, may not work"
        elif provider == "openrouter":
            model_status = STATUS_WARNING
            model_note = f"Uses OpenRouter ({config.model}) — ensure OpenRouter provider is configured in PinkyBot"
        identity.model = PreviewItem(
            label="Model",
            value=model_str,
            original=config.model,
            status=model_status,
            note=model_note,
        )
    else:
        identity.model = PreviewItem(
            label="Model",
            value="sonnet",
            status=STATUS_WARNING,
            note="No model configured — defaulting to sonnet",
        )

    # Soul / boundaries split (Claude-assisted)
    if workspace.soul_md:
        soul, boundaries = split_soul_boundaries(workspace.soul_md)
        identity.soul_full = soul
        identity.boundaries_full = boundaries
        identity.soul_preview = soul[:500] + ("..." if len(soul) > 500 else "")
        identity.boundaries_preview = boundaries[:500] + ("..." if len(boundaries) > 500 else "")
    else:
        preview.warnings.append("No SOUL.md found — agent will have no personality configured")

    # USER.md — verbatim
    identity.users_md = workspace.user_md


def _build_connections_section(
    preview: MigrationPreview,
    config: OpenClawConfig | None,
) -> None:
    """Populate platform connections section."""
    if not config or not config.channels:
        preview.connections.items.append(ConnectionItem(
            platform="(none)",
            status=STATUS_WARNING,
            note="No openclaw.json provided — no platform tokens will be imported",
            has_token=False,
        ))
        return

    for chan in config.channels:
        platform = chan.platform
        has_token = bool(chan.token)

        if platform in SUPPORTED_PLATFORMS:
            if has_token:
                item = ConnectionItem(
                    platform=platform,
                    status=STATUS_OK,
                    note="",
                    has_token=True,
                    allow_from=chan.allow_from,
                )
            else:
                item = ConnectionItem(
                    platform=platform,
                    status=STATUS_WARNING,
                    note=f"No token found for {platform} — you'll need to add it manually after migration",
                    has_token=False,
                    allow_from=chan.allow_from,
                )
        else:
            # Unsupported platform — name it explicitly per the spec
            item = ConnectionItem(
                platform=platform,
                status=STATUS_ERROR,
                note=f"OpenClaw's {platform.capitalize()} integration isn't supported in PinkyBot yet",
                has_token=False,
                allow_from=[],
            )
            preview.warnings.append(
                f"Platform '{platform}' is not supported in PinkyBot — "
                f"this connection will not be imported"
            )

        preview.connections.items.append(item)


def _build_memory_section(
    preview: MigrationPreview,
    workspace: WorkspaceData,
) -> None:
    """Classify MEMORY.md into Reflection drafts."""
    if not workspace.memory_md:
        preview.memory.status = STATUS_WARNING
        preview.memory.note = "No MEMORY.md found — no memories will be imported"
        return

    drafts = classify_memories(workspace.memory_md)
    preview.memory.drafts = drafts
    preview.memory.total_count = len(drafts)

    if drafts:
        # Build sample entries for UI display (first 5, no full draft objects)
        preview.memory.sample_entries = [
            {
                "content": d.content[:200] + ("..." if len(d.content) > 200 else ""),
                "type": d.reflection_type,
                "salience": d.salience,
            }
            for d in drafts[:5]
        ]
        preview.memory.status = STATUS_OK
    else:
        preview.memory.status = STATUS_WARNING
        preview.memory.note = "MEMORY.md was found but no memories could be extracted"


def _build_automation_section(
    preview: MigrationPreview,
    workspace: WorkspaceData,
) -> None:
    """Parse HEARTBEAT.md schedules and AGENTS.md directives."""
    # Schedules from HEARTBEAT.md
    if workspace.heartbeat_md:
        schedule_entries = parse_heartbeat_schedules(workspace.heartbeat_md)
        for entry in schedule_entries:
            status = STATUS_OK
            note = ""
            if entry.confidence == "low":
                status = STATUS_WARNING
                note = "Schedule timing was ambiguous — please review the cron expression"
            elif entry.confidence == "medium":
                note = "Schedule time was estimated — verify it matches your intent"
            preview.automation.schedules.append(ScheduleItem(
                name=entry.name,
                description=entry.description,
                cron=entry.cron,
                prompt=entry.prompt,
                timezone=entry.timezone,
                status=status,
                note=note,
            ))

    # Directives from AGENTS.md
    if workspace.agents_md:
        directive_drafts = split_directives(workspace.agents_md)
        for draft in directive_drafts:
            preview.automation.directives.append(DirectiveItem(
                directive=draft.directive,
                priority=draft.priority,
                status=STATUS_OK,
            ))


def _build_skills_section(
    preview: MigrationPreview,
    clawhub_skills: list[str],
    config: OpenClawConfig | None,
) -> None:
    """Map ClawHub skills to PinkyBot equivalents."""
    if not clawhub_skills:
        return

    # Skills that have known PinkyBot equivalents
    # This is a best-effort mapping — extend as the catalog grows
    _SKILL_MAP: dict[str, str] = {
        "pinky-memory": "pinky-memory",
        "memory": "pinky-memory",
        "telegram": "pinky-messaging",
        "messaging": "pinky-messaging",
        "calendar": "pinky-calendar",
        "web": "web-artifacts-builder",
        "search": "web-artifacts-builder",
    }

    skill_env = config.skill_env if config else {}

    for skill_name in clawhub_skills:
        skill_lower = skill_name.lower()

        # Find matching PinkyBot skill
        pinky_skill = None
        for key, val in _SKILL_MAP.items():
            if key in skill_lower:
                pinky_skill = val
                break

        # Check if this skill had env vars (API keys) in openclaw config
        env_keys: list[str] = []
        if skill_name in skill_env:
            env_keys = list(skill_env[skill_name].keys())
        elif skill_lower in skill_env:
            env_keys = list(skill_env[skill_lower].keys())

        if pinky_skill:
            note = ""
            status = STATUS_OK
            if env_keys:
                status = STATUS_WARNING
                key_list = ", ".join(f"`{k}`" for k in env_keys)
                note = f"This skill had API key(s) ({key_list}) — you'll need to re-enter them after migration"
            preview.skills.items.append(SkillItem(
                name=skill_name,
                status=status,
                note=note or f"Will import as '{pinky_skill}'",
                has_env_keys=env_keys,
            ))
        else:
            preview.skills.items.append(SkillItem(
                name=skill_name,
                status=STATUS_WARNING,
                note=f"'{skill_name}' is not available in PinkyBot — will import as directive-only",
                has_env_keys=env_keys,
            ))


# ── Migration application ──────────────────────────────────────────────────────


def apply_migration(
    preview: MigrationPreview,
    agent_registry: Any,
    memory_store: Any | None,
    *,
    confirmed_memory_ids: list[int] | None = None,
) -> MigrationResult:
    """Apply a confirmed MigrationPreview: create agent, write DB records,
    spawn background memory import task.

    Args:
        preview: The preview approved by the user (from build_preview)
        agent_registry: AgentRegistry instance
        memory_store: ReflectionStore instance (can be None — memories skipped)
        confirmed_memory_ids: Optional subset of memory draft indices to import.
                               If None, all memories from the preview are imported.

    Returns MigrationResult with agent_name, task_id, and item counts.

    Agent is created in "stopped" state (auto_start=False, enabled=True) per spec.
    Naming conflicts are handled by appending "-imported" suffix.
    """
    result = MigrationResult()
    warnings = list(preview.warnings)

    # ── Resolve agent name ────────────────────────────────────────────────────
    desired_name = preview.identity.name.value or "imported-agent"
    agent_name = _resolve_agent_name(desired_name, agent_registry)
    if agent_name != desired_name:
        warnings.append(
            f"Agent name '{desired_name}' already exists — created as '{agent_name}' instead"
        )
    result.agent_name = agent_name

    # ── Create agent record ───────────────────────────────────────────────────
    model = preview.identity.model.value or "sonnet"
    display_name = preview.identity.display_name.value or agent_name
    role = preview.identity.role.value or ""
    soul = preview.identity.soul_full or ""
    boundaries = preview.identity.boundaries_full or ""
    users = preview.identity.users_md or ""

    agent_registry.register(
        agent_name,
        display_name=display_name,
        model=model,
        soul=soul,
        boundaries=boundaries,
        users=users,
        role=role,
        auto_start=False,    # stopped state per spec
        enabled=True,
    )
    _log(f"migration: created agent '{agent_name}'")

    items_created: dict[str, int] = {"agent": 1}

    # ── Write platform tokens ─────────────────────────────────────────────────
    tokens_written = 0
    for conn in preview.connections.items:
        if conn.status == STATUS_ERROR:
            continue  # Unsupported platform — skip
        # We need the original token from the parse store. Tokens are passed
        # through the preview as has_token=True/False. The actual token value
        # must come from the parse store (never stored in preview for security).
        # The routes.py layer is responsible for passing tokens through apply.
        # Here we skip token writing — routes.py calls set_token directly.

    items_created["tokens"] = tokens_written

    # ── Write approved users ──────────────────────────────────────────────────
    users_written = 0
    for conn in preview.connections.items:
        if conn.status == STATUS_ERROR or not conn.allow_from:
            continue
        for chat_id in conn.allow_from:
            try:
                agent_registry._db.execute(
                    """INSERT OR IGNORE INTO approved_users
                       (agent_name, chat_id, display_name, status, approved_by, created_at, updated_at)
                       VALUES (?, ?, '', 'approved', 'openclaw-migration', ?, ?)""",
                    (agent_name, str(chat_id), time.time(), time.time()),
                )
                users_written += 1
            except Exception as e:
                _log(f"migration: failed to write approved user {chat_id}: {e}")
    if users_written:
        agent_registry._db.commit()
    items_created["approved_users"] = users_written

    # ── Write schedules ───────────────────────────────────────────────────────
    schedules_written = 0
    for sched in preview.automation.schedules:
        if not sched.cron:
            continue
        try:
            agent_registry.add_schedule(
                agent_name,
                sched.cron,
                name=sched.name,
                prompt=sched.prompt,
                timezone=sched.timezone,
            )
            schedules_written += 1
        except Exception as e:
            _log(f"migration: failed to write schedule '{sched.name}': {e}")
            warnings.append(f"Schedule '{sched.name}' could not be imported: {e}")
    items_created["schedules"] = schedules_written

    # ── Write directives ──────────────────────────────────────────────────────
    directives_written = 0
    for directive in preview.automation.directives:
        if not directive.directive:
            continue
        try:
            agent_registry.add_directive(agent_name, directive.directive, priority=directive.priority)
            directives_written += 1
        except Exception as e:
            _log(f"migration: failed to write directive: {e}")
            warnings.append(f"A directive could not be imported: {e}")
    items_created["directives"] = directives_written

    # ── Spawn background memory import ────────────────────────────────────────
    task_id = ""
    if memory_store and preview.memory.drafts:
        # Select which drafts to import
        if confirmed_memory_ids is not None:
            drafts_to_import = [
                d for i, d in enumerate(preview.memory.drafts)
                if i in confirmed_memory_ids
            ]
        else:
            drafts_to_import = list(preview.memory.drafts)

        if drafts_to_import:
            task_id = str(uuid.uuid4())
            _task_status[task_id] = {
                "total": len(drafts_to_import),
                "imported": 0,
                "failed": 0,
                "done": False,
            }
            # Spawn as asyncio task (non-blocking)
            asyncio.create_task(
                _import_memories_background(task_id, drafts_to_import, memory_store)
            )
            _log(f"migration: spawned memory import task {task_id} ({len(drafts_to_import)} items)")
            items_created["memories_queued"] = len(drafts_to_import)
    elif not memory_store and preview.memory.drafts:
        warnings.append(
            "Memory store not available — memories could not be imported. "
            "Ensure pinky_memory is installed and configured."
        )

    result.task_id = task_id
    result.items_created = items_created
    result.warnings = warnings
    return result


async def _import_memories_background(
    task_id: str,
    drafts: list[ReflectionDraft],
    memory_store: Any,
) -> None:
    """Background task: batch-insert reflection drafts into the memory store.

    Inserts without embeddings — the reflections_vec backfill mechanism will
    handle embedding generation lazily. Never depends on imported vectors.

    Updates _task_status[task_id] as items are processed.
    """
    try:
        from pinky_memory.types import Reflection, ReflectionType
    except ImportError:
        _log(f"migration bg task {task_id}: pinky_memory not available, aborting")
        async with _get_task_lock():
            _task_status[task_id].update({"done": True, "failed": len(drafts)})
        return

    _log(f"migration bg task {task_id}: starting, {len(drafts)} memories to import")

    BATCH_SIZE = 50  # Insert in batches to avoid holding DB lock too long

    imported = 0
    failed = 0

    for i, draft in enumerate(drafts):
        try:
            # Map string type to ReflectionType enum
            try:
                rtype = ReflectionType(draft.reflection_type)
            except ValueError:
                rtype = ReflectionType.fact

            reflection = Reflection(
                type=rtype,
                content=draft.content,
                context=draft.context or "Imported from OpenClaw",
                project=draft.project,
                salience=draft.salience,
                entities=draft.entities,
                active=True,
                no_recall=False,
                embedding=[],  # Empty — will be filled by backfill mechanism
                source_channel="openclaw-migration",
            )
            memory_store.insert(reflection)
            imported += 1
        except Exception as e:
            _log(f"migration bg task {task_id}: failed to insert memory {i}: {e}")
            failed += 1

        # Yield to event loop every batch and update shared status
        if (i + 1) % BATCH_SIZE == 0:
            async with _get_task_lock():
                _task_status[task_id].update({"imported": imported, "failed": failed})
            await asyncio.sleep(0)

    async with _get_task_lock():
        _task_status[task_id].update({"imported": imported, "failed": failed, "done": True})
    _log(
        f"migration bg task {task_id}: done. "
        f"imported={imported}, failed={failed}"
    )


# ── Helpers ────────────────────────────────────────────────────────────────────


def _resolve_agent_name(desired: str, agent_registry: Any) -> str:
    """Resolve a potentially conflicting agent name by appending a suffix.

    Tries: desired → desired-imported → desired-imported-2 → ...
    """
    if not agent_registry.get(desired):
        return desired

    candidate = f"{desired}-imported"
    if not agent_registry.get(candidate):
        return candidate

    counter = 2
    while True:
        candidate = f"{desired}-imported-{counter}"
        if not agent_registry.get(candidate):
            return candidate
        counter += 1
        if counter > 100:
            # Extremely unlikely but avoid infinite loop
            return f"{desired}-{uuid.uuid4().hex[:6]}"
