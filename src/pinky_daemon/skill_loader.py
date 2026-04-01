"""SKILL.md loader — agentskills.io standard skill discovery.

Scans directories for SKILL.md files following the agentskills.io open standard,
parses frontmatter + body, and auto-registers them into the SkillStore.

Discovery paths (in precedence order):
  1. Per-agent workspace: <agent_working_dir>/.pinky/skills/
  2. Per-agent workspace: <agent_working_dir>/.agents/skills/
  3. Global project: <pinky_root>/skills/
  4. Global cross-client: ~/.agents/skills/
  5. User pinky: ~/.pinky/skills/

Skills discovered from filesystem get skill_type="skill" in the SkillStore.
The SKILL.md body becomes the directive (injected into system prompt).
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

import yaml


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


@dataclass
class ParsedSkill:
    """A skill parsed from a SKILL.md file."""

    name: str
    description: str
    body: str  # Markdown instructions (after frontmatter)
    location: str  # Absolute path to SKILL.md
    base_dir: str  # Parent directory of SKILL.md

    # Optional frontmatter fields
    license: str = ""
    compatibility: str = ""
    metadata: dict = field(default_factory=dict)
    allowed_tools: list[str] = field(default_factory=list)

    # Discovered resources
    resources: list[str] = field(default_factory=list)  # Relative paths


# ── SKILL.md Parsing ──────────────────────────────────────


_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)", re.DOTALL)

# Name validation: lowercase letters, numbers, hyphens; no start/end/consecutive hyphens
_NAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")
_CONSECUTIVE_HYPHENS = re.compile(r"--")


def parse_skill_md(path: str | Path) -> ParsedSkill | None:
    """Parse a SKILL.md file into a ParsedSkill.

    Returns None if the file is unparseable or missing required fields.
    Follows lenient validation per agentskills.io spec:
    - Missing description → skip (required for disclosure)
    - Name issues → warn, load anyway
    - YAML unparseable → skip
    """
    path = Path(path)
    if not path.exists() or not path.is_file():
        return None

    try:
        content = path.read_text(encoding="utf-8")
    except Exception as e:
        _log(f"skill_loader: failed to read {path}: {e}")
        return None

    # Extract frontmatter
    match = _FRONTMATTER_RE.match(content)
    if not match:
        _log(f"skill_loader: no valid frontmatter in {path}")
        return None

    yaml_text, body = match.group(1), match.group(2).strip()

    # Parse YAML (with fallback for common issues)
    try:
        fm = yaml.safe_load(yaml_text)
    except yaml.YAMLError:
        # Try fixing unquoted colons (common cross-client issue)
        try:
            fixed = _fix_yaml_colons(yaml_text)
            fm = yaml.safe_load(fixed)
        except yaml.YAMLError as e:
            _log(f"skill_loader: unparseable YAML in {path}: {e}")
            return None

    if not isinstance(fm, dict):
        _log(f"skill_loader: frontmatter is not a mapping in {path}")
        return None

    # Required fields
    name = str(fm.get("name", "")).strip()
    description = str(fm.get("description", "")).strip()

    if not description:
        _log(f"skill_loader: skipping {path} — missing description")
        return None

    # Name validation (lenient)
    if not name:
        # Fall back to directory name
        name = path.parent.name
        _log(f"skill_loader: missing name in {path}, using directory name: {name}")

    # Warn on name issues but don't skip
    if name != path.parent.name:
        _log(f"skill_loader: name '{name}' doesn't match directory '{path.parent.name}' in {path}")
    if not _NAME_RE.match(name) or _CONSECUTIVE_HYPHENS.search(name):
        _log(f"skill_loader: name '{name}' doesn't follow naming convention in {path}")
    if len(name) > 64:
        _log(f"skill_loader: name '{name}' exceeds 64 chars in {path}")

    # Optional fields
    license_val = str(fm.get("license", "")).strip()
    compatibility = str(fm.get("compatibility", "")).strip()
    metadata = fm.get("metadata", {}) or {}
    if not isinstance(metadata, dict):
        metadata = {}

    # allowed-tools (space-delimited string → list)
    allowed_tools_raw = str(fm.get("allowed-tools", "")).strip()
    allowed_tools = allowed_tools_raw.split() if allowed_tools_raw else []

    # Discover bundled resources
    base_dir = path.parent
    resources = _discover_resources(base_dir)

    return ParsedSkill(
        name=name,
        description=description,
        body=body,
        location=str(path.resolve()),
        base_dir=str(base_dir.resolve()),
        license=license_val,
        compatibility=compatibility,
        metadata=metadata,
        allowed_tools=allowed_tools,
        resources=resources,
    )


def _fix_yaml_colons(yaml_text: str) -> str:
    """Attempt to fix unquoted values containing colons."""
    lines = []
    for line in yaml_text.split("\n"):
        if ":" in line and not line.strip().startswith("#"):
            # Find the first colon (key separator)
            first_colon = line.index(":")
            key = line[:first_colon].strip()
            value = line[first_colon + 1:].strip()
            # If value contains another colon and isn't quoted, quote it
            if ":" in value and not (value.startswith('"') or value.startswith("'")):
                line = f"{key}: \"{value}\""
        lines.append(line)
    return "\n".join(lines)


def _discover_resources(base_dir: Path) -> list[str]:
    """Discover bundled resources (scripts, references, assets) in a skill directory."""
    resources = []
    for subdir in ("scripts", "references", "assets"):
        sub = base_dir / subdir
        if sub.is_dir():
            for f in sorted(sub.rglob("*")):
                if f.is_file():
                    resources.append(str(f.relative_to(base_dir)))
    return resources


# ── Directory Scanning ────────────────────────────────────


_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".tox", ".mypy_cache"}


def scan_skills_directory(directory: str | Path, *, max_depth: int = 4) -> list[ParsedSkill]:
    """Scan a directory for SKILL.md files.

    Looks for subdirectories containing a SKILL.md file, up to max_depth levels.
    Returns a list of successfully parsed skills.
    """
    directory = Path(directory)
    if not directory.is_dir():
        return []

    skills = []
    _scan_recursive(directory, skills, depth=0, max_depth=max_depth)
    return skills


def _scan_recursive(
    directory: Path, results: list[ParsedSkill], *, depth: int, max_depth: int,
) -> None:
    """Recursively scan for SKILL.md files."""
    if depth > max_depth:
        return

    skill_md = directory / "SKILL.md"
    if skill_md.is_file():
        parsed = parse_skill_md(skill_md)
        if parsed:
            results.append(parsed)
        return  # Don't scan subdirs of a skill directory

    try:
        entries = sorted(directory.iterdir())
    except PermissionError:
        return

    for entry in entries:
        if entry.is_dir() and entry.name not in _SKIP_DIRS:
            _scan_recursive(entry, results, depth=depth + 1, max_depth=max_depth)


def discover_all_skills(
    *,
    agent_working_dir: str | Path | None = None,
    project_root: str | Path | None = None,
) -> list[ParsedSkill]:
    """Discover skills from all standard locations.

    Returns skills with project-level taking precedence over user-level.
    Deduplicates by name (first-found wins).
    """
    seen_names: set[str] = set()
    all_skills: list[ParsedSkill] = []

    scan_dirs: list[Path] = []

    # 1. Per-agent workspace (highest precedence)
    if agent_working_dir:
        wd = Path(agent_working_dir)
        scan_dirs.append(wd / ".pinky" / "skills")
        scan_dirs.append(wd / ".agents" / "skills")
        scan_dirs.append(wd / "skills")

    # 2. Project root
    if project_root and str(project_root) != str(agent_working_dir):
        pr = Path(project_root)
        scan_dirs.append(pr / ".pinky" / "skills")
        scan_dirs.append(pr / ".agents" / "skills")
        scan_dirs.append(pr / "skills")

    # 3. Global pinky skills directory
    pinky_root = Path(__file__).resolve().parent.parent.parent
    scan_dirs.append(pinky_root / "skills")

    # 4. User-level (lowest precedence)
    home = Path.home()
    scan_dirs.append(home / ".agents" / "skills")
    scan_dirs.append(home / ".pinky" / "skills")

    for scan_dir in scan_dirs:
        if not scan_dir.is_dir():
            continue
        found = scan_skills_directory(scan_dir)
        for skill in found:
            if skill.name not in seen_names:
                seen_names.add(skill.name)
                all_skills.append(skill)
            else:
                _log(
                    f"skill_loader: shadowed skill '{skill.name}' at {skill.location} "
                    f"(already loaded from a higher-precedence location)"
                )

    return all_skills


# ── SkillStore Registration ───────────────────────────────


def register_discovered_skills(
    skill_store,
    skills: list[ParsedSkill],
    *,
    overwrite: bool = False,
) -> dict:
    """Register discovered SKILL.md skills into the SkillStore.

    Args:
        skill_store: SkillStore instance
        skills: List of parsed skills to register
        overwrite: If True, update existing skills. If False, skip existing.

    Returns:
        {"registered": [...], "skipped": [...], "updated": [...]}
    """
    registered = []
    skipped = []
    updated = []

    for skill in skills:
        existing = skill_store.get(skill.name)

        if existing and not overwrite:
            # Don't overwrite skills registered via API/UI
            if existing.skill_type != "skill":
                skipped.append(skill.name)
                continue
            # Update if it was previously discovered (same type)
            # but check if content changed
            if existing.directive == skill.body and existing.description == skill.description:
                skipped.append(skill.name)
                continue

        # Build tool_patterns from allowed-tools frontmatter
        tool_patterns = skill.allowed_tools if skill.allowed_tools else []

        # Store resource listing and location in config
        config = {
            "location": skill.location,
            "base_dir": skill.base_dir,
            "resources": skill.resources,
            "compatibility": skill.compatibility,
            "license": skill.license,
            "source": "filesystem",
        }
        if skill.metadata:
            config["metadata"] = skill.metadata

        skill_store.register(
            skill.name,
            description=skill.description,
            skill_type="skill",  # agentskills.io markdown skill
            version=skill.metadata.get("version", "1.0.0") if skill.metadata else "1.0.0",
            enabled=True,
            config=config,
            tool_patterns=tool_patterns,
            directive=skill.body,  # Full SKILL.md body as directive
            self_assignable=True,  # Agents can add filesystem skills to themselves
            category="skill",  # Distinct from core/development/productivity
            shared=False,  # Not auto-applied; agents opt in
        )

        if existing:
            updated.append(skill.name)
        else:
            registered.append(skill.name)

    result = {"registered": registered, "skipped": skipped, "updated": updated}
    total = len(registered) + len(updated)
    if total > 0:
        _log(f"skill_loader: registered {len(registered)}, updated {len(updated)}, skipped {len(skipped)} skills")
    return result
