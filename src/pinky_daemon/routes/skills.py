"""Skill catalog, plugin discovery, and session-scoped skill routes.

Extracted from api.py. The 7 `/agents/{name}/skills/*` endpoints are
NOT in this file — they call `_disconnect_streaming_sessions` and
`_start_streaming_session` which are deep closures in `create_api()`,
and pulling them across module boundaries would balloon scope. They
live in api.py until the agents domain is also extracted.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, HTTPException

from pinky_daemon.api_models import (
    CreateSkillFromMdRequest,
    InstallSkillFromGitRequest,
    RegisterSkillRequest,
    SessionSkillRequest,
    UpdateSkillRequest,
)
from pinky_daemon.skill_loader import (
    _CONSECUTIVE_HYPHENS,
    _NAME_RE,
    discover_all_skills,
    register_discovered_skills,
)

router = APIRouter(tags=["skills"])


# ── Shared dependency state ───────────────────────────────────────────────────

_skills: Any = None
_plugins: Any = None
_agents: Any = None
_manager: Any = None
_pinky_root: Path | None = None
_log: Callable[[str], None] | None = None


def set_dependencies(
    *,
    skills,
    plugins,
    agents,
    manager,
    pinky_root: Path,
    log: Callable[[str], None],
) -> None:
    """Wire shared instances and helpers for the skills router."""
    global _skills, _plugins, _agents, _manager, _pinky_root, _log
    _skills = skills
    _plugins = plugins
    _agents = agents
    _manager = manager
    _pinky_root = pinky_root
    _log = log


# ── Skill Management ──────────────────────────────────────────────────────────


@router.post("/skills")
async def register_skill(req: RegisterSkillRequest):
    """Register a new skill or update an existing one."""
    skill = _skills.register(
        req.name,
        description=req.description,
        skill_type=req.skill_type,
        version=req.version,
        enabled=req.enabled,
        config=req.config,
        mcp_server_config=req.mcp_server_config,
        tool_patterns=req.tool_patterns,
        directive=req.directive,
        requires=req.requires,
        self_assignable=req.self_assignable,
        category=req.category,
        shared=req.shared,
        file_templates=req.file_templates,
        default_config=req.default_config,
    )
    return skill.to_dict()


@router.get("/skills")
async def list_skills(
    skill_type: str = "",
    enabled_only: bool = False,
    category: str = "",
    shared_only: bool = False,
    self_assignable_only: bool = False,
):
    """List all registered skills."""
    result = _skills.list(
        skill_type=skill_type,
        enabled_only=enabled_only,
        category=category,
        shared_only=shared_only,
        self_assignable_only=self_assignable_only,
    )
    return {"skills": [s.to_dict() for s in result], "count": len(result)}


# ── Skill specific-path routes (must be before /skills/{name}) ───


@router.get("/skills/catalog")
async def get_skill_catalog():
    """Get all skills with agent assignment counts."""
    return {"skills": _skills.get_catalog_with_counts()}


@router.get("/skills/categories")
async def get_skill_categories():
    """Get distinct skill categories."""
    return {"categories": _skills.get_categories()}


@router.get("/skills/{name}")
async def get_skill(name: str):
    """Get a skill by name."""
    skill = _skills.get(name)
    if not skill:
        raise HTTPException(404, f"Skill '{name}' not found")
    return skill.to_dict()


@router.put("/skills/{name}")
async def update_skill(name: str, req: UpdateSkillRequest):
    """Update an existing skill's properties."""
    existing = _skills.get(name)
    if not existing:
        raise HTTPException(404, f"Skill '{name}' not found")

    skill = _skills.register(
        name,
        description=req.description if req.description is not None else existing.description,
        skill_type=req.skill_type if req.skill_type is not None else existing.skill_type,
        version=req.version if req.version is not None else existing.version,
        enabled=req.enabled if req.enabled is not None else existing.enabled,
        config=req.config if req.config is not None else existing.config,
        mcp_server_config=req.mcp_server_config if req.mcp_server_config is not None else existing.mcp_server_config,
        tool_patterns=req.tool_patterns if req.tool_patterns is not None else existing.tool_patterns,
        directive=req.directive if req.directive is not None else existing.directive,
        requires=req.requires if req.requires is not None else existing.requires,
        self_assignable=req.self_assignable if req.self_assignable is not None else existing.self_assignable,
        category=req.category if req.category is not None else existing.category,
        shared=req.shared if req.shared is not None else existing.shared,
        file_templates=req.file_templates if req.file_templates is not None else existing.file_templates,
        default_config=req.default_config if req.default_config is not None else existing.default_config,
    )
    return skill.to_dict()


@router.delete("/skills/{name}")
async def delete_skill(name: str):
    """Unregister a skill."""
    deleted = _skills.delete(name)
    if not deleted:
        raise HTTPException(404, f"Skill '{name}' not found")
    return {"deleted": True, "name": name}


@router.post("/skills/{name}/enable")
async def enable_skill(name: str):
    """Enable a skill globally."""
    if not _skills.enable(name):
        raise HTTPException(404, f"Skill '{name}' not found")
    return {"enabled": True, "name": name}


@router.post("/skills/{name}/disable")
async def disable_skill(name: str):
    """Disable a skill globally."""
    if not _skills.disable(name):
        raise HTTPException(404, f"Skill '{name}' not found")
    return {"disabled": True, "name": name}


# ── Session-scoped skills ─────────────────────────────────────────────────────


@router.get("/sessions/{session_id}/skills")
async def get_session_skills(session_id: str):
    """Get skills for a session with effective enabled state."""
    session = _manager.get(session_id)
    if not session:
        raise HTTPException(404, f"Session '{session_id}' not found")
    result = _skills.get_session_skills(session_id)
    return {"session_id": session_id, "skills": result, "count": len(result)}


@router.put("/sessions/{session_id}/skills/{skill_name}")
async def set_session_skill(session_id: str, skill_name: str, req: SessionSkillRequest):
    """Enable or disable a skill for a specific session."""
    session = _manager.get(session_id)
    if not session:
        raise HTTPException(404, f"Session '{session_id}' not found")

    if req.enabled:
        ok = _skills.enable_for_session(session_id, skill_name)
    else:
        ok = _skills.disable_for_session(session_id, skill_name)

    if not ok:
        raise HTTPException(404, f"Skill '{skill_name}' not found")
    return {"session_id": session_id, "skill": skill_name, "enabled": req.enabled}


@router.delete("/sessions/{session_id}/skills/{skill_name}")
async def clear_session_skill_override(session_id: str, skill_name: str):
    """Remove per-session override, reverting to global default."""
    session = _manager.get(session_id)
    if not session:
        raise HTTPException(404, f"Session '{session_id}' not found")
    _skills.clear_session_override(session_id, skill_name)
    return {"session_id": session_id, "skill": skill_name, "override_cleared": True}


# ── Skill Discovery & Plugin Endpoints ────────────────────────────────────────


@router.post("/skills/from-md")
async def create_skill_from_md(req: CreateSkillFromMdRequest):
    """Create a skill by parsing SKILL.md content inline.

    Parses the frontmatter + body, registers as a skill, and optionally
    assigns it to an agent.
    """
    from pinky_daemon.skill_loader import parse_skill_md

    if not req.content.strip():
        raise HTTPException(400, "Empty SKILL.md content")

    # Write to a temp file so the parser can work with it
    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", prefix="skill_", delete=False) as f:
        f.write(req.content)
        tmp_path = f.name

    try:
        parsed = parse_skill_md(tmp_path)
    finally:
        os.unlink(tmp_path)

    if not parsed:
        raise HTTPException(400, "Failed to parse SKILL.md — check frontmatter (name and description required)")

    # The skill name becomes a directory under skills/, so enforce the naming
    # convention (reject, not warn) and assert containment — a crafted
    # frontmatter name must not be able to traverse out of skills/ and write
    # SKILL.md to an arbitrary path.
    if (
        not parsed.name
        or not _NAME_RE.match(parsed.name)
        or _CONSECUTIVE_HYPHENS.search(parsed.name)
        or len(parsed.name) > 64
    ):
        raise HTTPException(
            400,
            "Invalid skill name — use lowercase letters, digits, and single hyphens (max 64 chars)",
        )
    # Also write to skills/ directory for persistence
    skills_root = (_pinky_root / "skills").resolve()
    skill_dir = (skills_root / parsed.name).resolve()
    if not skill_dir.is_relative_to(skills_root):
        raise HTTPException(400, "Invalid skill name")
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(req.content)

    # Register in SkillStore
    config = {
        "location": str(skill_dir / "SKILL.md"),
        "base_dir": str(skill_dir),
        "source": "ui",
    }
    if parsed.metadata:
        config["metadata"] = parsed.metadata

    skill = _skills.register(
        parsed.name,
        description=parsed.description,
        skill_type="skill",
        version=parsed.metadata.get("version", "1.0.0") if parsed.metadata else "1.0.0",
        enabled=True,
        config=config,
        tool_patterns=parsed.allowed_tools,
        directive=parsed.body,
        self_assignable=True,
        category="skill",
        shared=False,
    )

    result = skill.to_dict()

    # Auto-assign to agent if specified
    if req.agent_name:
        agent = _agents.get(req.agent_name)
        if agent:
            _skills.assign_to_agent(req.agent_name, parsed.name, assigned_by="user")
            result["assigned_to"] = req.agent_name

    return result


@router.post("/skills/from-git")
async def install_skill_from_git(req: InstallSkillFromGitRequest):
    """Clone a git repo into skills/ and register any SKILL.md files found.

    Supports:
    - Full repo: https://github.com/org/skill-name
    - Repo with .git suffix: https://github.com/org/skill-name.git
    - Subdirectory hint: https://github.com/org/skills-collection/tree/main/my-skill
    """
    import re as _re
    import subprocess as sp

    url = req.url.strip()
    if not url:
        raise HTTPException(400, "URL is required")

    # Parse GitHub tree/blob URLs:
    #   github.com/org/repo/tree/branch/path
    #   github.com/org/repo/blob/branch/path/to/SKILL.md
    subdir = ""
    gh_match = _re.match(
        r"https?://github\.com/([^/]+/[^/]+)/(?:tree|blob)/[^/]+/(.*)", url,
    )
    if gh_match:
        repo_slug = gh_match.group(1)
        path = gh_match.group(2).rstrip("/")
        # If pointing at a file, use its parent directory
        if path.endswith(".md") or "." in path.rsplit("/", 1)[-1]:
            path = path.rsplit("/", 1)[0] if "/" in path else ""
        subdir = path
        url = f"https://github.com/{repo_slug}.git"
    elif _re.match(r"https?://github\.com/[^/]+/[^/]+$", url):
        # Plain repo URL: github.com/org/repo (no tree/blob)
        url = url.rstrip("/") + ".git" if not url.endswith(".git") else url

    # Derive a directory name from the URL
    repo_name = url.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")
    target_dir = _pinky_root / "skills" / repo_name

    try:
        if target_dir.exists():
            # Pull latest
            sp.run(
                ["git", "-C", str(target_dir), "pull", "--ff-only"],
                capture_output=True, timeout=60, check=True,
            )
            _log(f"api: updated skill repo {repo_name}")
        else:
            # Clone
            sp.run(
                ["git", "clone", "--depth", "1", url, str(target_dir)],
                capture_output=True, timeout=120, check=True,
            )
            _log(f"api: cloned skill repo {repo_name} to {target_dir}")
    except sp.CalledProcessError as e:
        stderr = e.stderr.decode() if e.stderr else str(e)
        raise HTTPException(400, f"Git clone failed: {stderr.strip()}") from e
    except sp.TimeoutExpired as e:
        raise HTTPException(504, "Git clone timed out") from e

    # Scan the cloned directory (or subdirectory) for SKILL.md files
    scan_root = target_dir / subdir if subdir else target_dir
    if not scan_root.is_dir():
        raise HTTPException(400, f"Subdirectory '{subdir}' not found in cloned repo")

    from pinky_daemon.skill_loader import register_discovered_skills as _register
    from pinky_daemon.skill_loader import scan_skills_directory

    # Check if scan_root itself has a SKILL.md (repo IS a skill)
    found = scan_skills_directory(scan_root)

    # If nothing found in subdirs, check root-level SKILL.md
    if not found and (scan_root / "SKILL.md").is_file():
        from pinky_daemon.skill_loader import parse_skill_md
        parsed = parse_skill_md(scan_root / "SKILL.md")
        if parsed:
            found = [parsed]

    if not found:
        raise HTTPException(400, f"No SKILL.md files found in {repo_name}" + (f"/{subdir}" if subdir else ""))

    result = _register(_skills, found, overwrite=True)

    # Auto-assign to agent if specified
    assigned = []
    if req.agent_name:
        agent = _agents.get(req.agent_name)
        if agent:
            for name in result["registered"] + result["updated"]:
                _skills.assign_to_agent(req.agent_name, name, assigned_by="user")
                assigned.append(name)

    return {
        "repo": repo_name,
        "skills_found": len(found),
        "registered": result["registered"],
        "updated": result["updated"],
        "skipped": result["skipped"],
        "assigned_to": req.agent_name if assigned else "",
        "assigned_skills": assigned,
    }


@router.post("/skills/discover")
async def discover_skills_endpoint():
    """Re-scan filesystem for SKILL.md files and register new skills."""
    found = discover_all_skills(project_root=str(_pinky_root))
    result = register_discovered_skills(_skills, found, overwrite=False)
    return {
        "discovered": len(found),
        **result,
    }


@router.get("/plugins")
async def list_plugins_endpoint():
    """List all discovered plugins with their state."""
    plugin_list = _plugins.list_plugins()
    return {"plugins": plugin_list, "count": len(plugin_list)}


@router.post("/plugins/discover")
async def discover_plugins_endpoint():
    """Re-scan filesystem for Python plugins."""
    found = _plugins.discover_all(project_root=str(_pinky_root))
    return {"discovered": [m.name for m in found], "count": len(found)}


@router.post("/plugins/{name}/enable")
async def enable_plugin(name: str):
    """Enable a discovered plugin."""
    info = _plugins.get(name)
    if not info:
        raise HTTPException(404, f"Plugin '{name}' not found")
    ok = _plugins.enable(name)
    if not ok:
        raise HTTPException(500, f"Failed to enable plugin: {info.error}")
    _plugins.register_in_skill_store(_skills, name)
    return {"enabled": True, "name": name}


@router.post("/plugins/{name}/disable")
async def disable_plugin(name: str):
    """Disable an active plugin."""
    info = _plugins.get(name)
    if not info:
        raise HTTPException(404, f"Plugin '{name}' not found")
    _plugins.disable(name)
    return {"disabled": True, "name": name}


@router.get("/plugins/{name}")
async def get_plugin(name: str):
    """Get plugin details."""
    info = _plugins.get(name)
    if not info:
        raise HTTPException(404, f"Plugin '{name}' not found")
    m = info.manifest
    return {
        "name": m.name,
        "description": m.description,
        "version": m.version,
        "author": m.author,
        "state": info.state.value,
        "error": info.error,
        "permissions": m.permissions,
        "tools": m.tools,
        "hooks": m.hooks,
        "directory": m.directory,
    }
