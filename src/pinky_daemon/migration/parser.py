"""OpenClaw workspace parser — pure parsing, no Claude, no DB.

Reads OpenClaw workspace files from a zip or directory and normalises them
into plain dataclasses ready for the mapper stage.

OpenClaw workspace layout:
    SOUL.md          — personality + ethics/limits blended together
    IDENTITY.md      — name, agent ID, role label
    AGENTS.md        — operating procedures / workflows
    USER.md          — owner profile
    TOOLS.md         — tool docs and constraints
    HEARTBEAT.md     — scheduled tasks in plain English
    MEMORY.md        — long-term persistent facts

Global config at ~/.openclaw/openclaw.json:
    channels         — platform tokens + routing
    model            — provider/model string
    skills           — per-skill env vars

.clawhub/lock.json:
    skills           — list of installed skill identifiers
"""

from __future__ import annotations

import json
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

# ── Dataclasses ───────────────────────────────────────────────────────────────


@dataclass
class WorkspaceData:
    """All Markdown content extracted from an OpenClaw workspace."""

    # Core identity files
    soul_md: str = ""          # SOUL.md — personality + ethics blended
    identity_md: str = ""      # IDENTITY.md
    agents_md: str = ""        # AGENTS.md — operating procedures
    user_md: str = ""          # USER.md — owner profile
    tools_md: str = ""         # TOOLS.md
    heartbeat_md: str = ""     # HEARTBEAT.md — scheduled tasks
    memory_md: str = ""        # MEMORY.md — long-term facts

    # Extra files (keyed by relative path inside workspace)
    extra_files: dict[str, str] = field(default_factory=dict)

    # Derived quick-access fields (parsed from IDENTITY.md)
    agent_name: str = ""
    agent_display_name: str = ""
    agent_role: str = ""

    # Raw source path (temp dir or zip path), used for cleanup
    source_path: str = ""


@dataclass
class ChannelConfig:
    """A single platform channel extracted from openclaw.json."""

    platform: str = ""        # telegram, discord, slack, whatsapp, etc.
    token: str = ""           # Bot token
    allow_from: list[str] = field(default_factory=list)  # Allowed user/chat IDs
    settings: dict = field(default_factory=dict)          # Any other channel settings


@dataclass
class OpenClawConfig:
    """Parsed openclaw.json global config."""

    model: str = ""                         # e.g. "anthropic/claude-sonnet-4-5"
    channels: list[ChannelConfig] = field(default_factory=list)
    skill_env: dict[str, dict] = field(default_factory=dict)  # skill_name → {env_var: value}
    raw: dict = field(default_factory=dict)  # Original parsed JSON for reference


# ── File name constants ────────────────────────────────────────────────────────

_KNOWN_MD_FILES = {
    "soul.md": "soul_md",
    "identity.md": "identity_md",
    "agents.md": "agents_md",
    "user.md": "user_md",
    "tools.md": "tools_md",
    "heartbeat.md": "heartbeat_md",
    "memory.md": "memory_md",
}

# PinkyBot-supported messaging platforms
SUPPORTED_PLATFORMS = {"telegram", "discord", "slack"}


# ── Main parse functions ───────────────────────────────────────────────────────


def parse_workspace(zip_path: str) -> WorkspaceData:
    """Parse an OpenClaw workspace zip into a WorkspaceData struct.

    Accepts either:
    - A .zip file path — extracted in-memory (files read directly from zip)
    - A directory path — reads markdown files directly

    Returns a WorkspaceData with all available content. Missing files leave
    their corresponding fields as empty strings — callers must handle gracefully.
    """
    path = Path(zip_path)
    data = WorkspaceData(source_path=str(path))

    if path.is_dir():
        _read_workspace_dir(path, data)
    elif path.suffix.lower() == ".zip" and path.is_file():
        _read_workspace_zip(path, data)
    else:
        raise ValueError(f"parse_workspace: path must be a directory or .zip file, got: {zip_path}")

    # Parse identity fields from IDENTITY.md if available
    if data.identity_md:
        _extract_identity(data)

    return data


def parse_openclaw_json(json_path: str) -> OpenClawConfig:
    """Parse an openclaw.json (or JSON5-ish) config file.

    Returns an OpenClawConfig with channels, model, and skill env vars.
    Handles both JSON and relaxed JSON5 (comment stripping only — no full
    JSON5 parser required since openclaw.json is typically valid JSON).
    """
    path = Path(json_path)
    if not path.exists():
        raise FileNotFoundError(f"parse_openclaw_json: file not found: {json_path}")

    raw_text = path.read_text(encoding="utf-8")

    # Strip // and /* */ comments (common in JSON5 config files)
    raw_text = _strip_json_comments(raw_text)

    try:
        raw = json.loads(raw_text)
    except json.JSONDecodeError as e:
        raise ValueError(f"parse_openclaw_json: JSON parse error in {json_path}: {e}") from e

    return _build_config(raw)


def parse_clawhub_lock(lock_path: str) -> list[str]:
    """Parse a .clawhub/lock.json file and return a list of installed skill names.

    Returns an empty list if the file doesn't exist (graceful degradation).
    """
    path = Path(lock_path)
    if not path.exists():
        return []

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []

    # ClawHub lock.json has various formats — try common keys
    skills: list[str] = []

    if isinstance(raw, list):
        # Plain list of skill names or objects
        for item in raw:
            if isinstance(item, str):
                skills.append(item)
            elif isinstance(item, dict):
                name = item.get("name") or item.get("id") or item.get("skill")
                if name:
                    skills.append(str(name))

    elif isinstance(raw, dict):
        # {skills: [...]} or {packages: {...}} or {dependencies: {...}}
        candidates = (
            raw.get("skills")
            or raw.get("packages")
            or raw.get("dependencies")
            or raw.get("installed")
        )
        if isinstance(candidates, list):
            for item in candidates:
                if isinstance(item, str):
                    skills.append(item)
                elif isinstance(item, dict):
                    name = item.get("name") or item.get("id")
                    if name:
                        skills.append(str(name))
        elif isinstance(candidates, dict):
            skills.extend(candidates.keys())

    return skills


# ── Internal helpers ───────────────────────────────────────────────────────────


def _read_workspace_dir(root: Path, data: WorkspaceData) -> None:
    """Read workspace markdown files from a directory."""
    for filename, attr in _KNOWN_MD_FILES.items():
        candidate = root / filename.upper()
        if not candidate.exists():
            candidate = root / filename  # lowercase fallback
        if candidate.exists():
            try:
                setattr(data, attr, candidate.read_text(encoding="utf-8"))
            except OSError:
                pass  # Skip unreadable files

    # Collect extra .md files not in the known list
    for md_file in root.rglob("*.md"):
        rel = md_file.relative_to(root)
        rel_lower = str(rel).lower()
        if rel_lower not in _KNOWN_MD_FILES and not rel_lower.startswith("."):
            try:
                data.extra_files[str(rel)] = md_file.read_text(encoding="utf-8")
            except OSError:
                pass


def _safe_zip_member(name: str) -> bool:
    """Return True if a zip member path is safe (no path traversal)."""
    try:
        p = Path(name)
    except Exception:
        return False
    if p.is_absolute():
        return False
    if any(part == ".." for part in p.parts):
        return False
    return True


def _read_workspace_zip(zip_path: Path, data: WorkspaceData) -> None:
    """Read workspace markdown files directly from a zip archive."""
    with zipfile.ZipFile(zip_path, "r") as zf:
        # Validate all member paths before processing — prevent zip path traversal
        names = [n for n in zf.namelist() if _safe_zip_member(n)]

        # Detect common top-level directory (e.g. when zipped as "agent-workspace/")
        prefix = _detect_zip_prefix(names)

        for filename, attr in _KNOWN_MD_FILES.items():
            # Try common case variants with and without prefix.
            # OpenClaw uses all-caps stems (SOUL.md, IDENTITY.md) by convention;
            # some exports use all-lower. We try both.
            stem = filename.rsplit(".", 1)[0]  # e.g. "soul"
            ext = filename.rsplit(".", 1)[1]   # "md"
            candidates = [
                prefix + stem.upper() + "." + ext,  # workspace/SOUL.md
                prefix + filename,                   # workspace/soul.md
                stem.upper() + "." + ext,            # SOUL.md (no prefix)
                filename,                             # soul.md (no prefix)
            ]
            for candidate in candidates:
                if candidate in names:
                    try:
                        setattr(data, attr, zf.read(candidate).decode("utf-8", errors="replace"))
                    except Exception:
                        pass
                    break  # Found this file, move to next

        # Collect extra .md files
        for name in names:
            rel = name[len(prefix):] if name.startswith(prefix) else name
            rel_lower = rel.lower()
            if (
                rel_lower.endswith(".md")
                and rel_lower not in _KNOWN_MD_FILES
                and not rel_lower.startswith(".")
                and "/" not in rel_lower.rstrip("/")  # top-level only for extras
            ):
                try:
                    data.extra_files[rel] = zf.read(name).decode("utf-8", errors="replace")
                except Exception:
                    pass


def _detect_zip_prefix(names: list[str]) -> str:
    """Detect a common directory prefix in zip file entries (e.g. 'workspace/')."""
    if not names:
        return ""
    # If all names start with a common directory, return it as prefix
    first_parts = [n.split("/")[0] for n in names if "/" in n]
    if not first_parts:
        return ""
    candidate = first_parts[0]
    if all(n.startswith(candidate + "/") or n == candidate + "/" for n in names):
        return candidate + "/"
    return ""


def _extract_identity(data: WorkspaceData) -> None:
    """Parse name, display name, and role from IDENTITY.md.

    OpenClaw IDENTITY.md typically has a YAML-ish frontmatter or a heading
    followed by key: value pairs. We handle both formats.
    """
    text = data.identity_md

    # Try YAML-style frontmatter (--- ... ---)
    if text.strip().startswith("---"):
        fm = _parse_yaml_frontmatter(text)
        data.agent_name = _slugify(fm.get("name", fm.get("id", "")))
        data.agent_display_name = fm.get("name", fm.get("display_name", ""))
        data.agent_role = fm.get("role", fm.get("type", ""))
        return

    # Try markdown heading + key: value pairs
    lines = text.strip().splitlines()
    kv: dict[str, str] = {}
    for line in lines:
        if line.startswith("#"):
            # Extract name from first H1/H2/H3
            if not data.agent_display_name:
                data.agent_display_name = line.lstrip("#").strip()
        elif ":" in line:
            key, _, val = line.partition(":")
            kv[key.strip().lower()] = val.strip()

    data.agent_name = _slugify(kv.get("name", kv.get("id", data.agent_display_name)))
    if not data.agent_display_name:
        data.agent_display_name = kv.get("name", kv.get("display_name", ""))
    data.agent_role = kv.get("role", kv.get("type", ""))


def _parse_yaml_frontmatter(text: str) -> dict[str, str]:
    """Minimally parse YAML-style frontmatter (key: value only, no nesting)."""
    result: dict[str, str] = {}
    in_fm = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped == "---":
            if not in_fm:
                in_fm = True
                continue
            else:
                break  # End of frontmatter
        if in_fm and ":" in stripped:
            key, _, val = stripped.partition(":")
            result[key.strip().lower()] = val.strip().strip('"').strip("'")
    return result


def _strip_json_comments(text: str) -> str:
    """Strip // line comments and /* */ block comments from JSON-ish text."""
    # Block comments
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    # Line comments (but not URLs like https://)
    text = re.sub(r'(?<!:)//[^\n]*', "", text)
    return text


def _slugify(name: str) -> str:
    """Convert a display name to a lowercase hyphenated slug suitable for agent names."""
    if not name:
        return ""
    slug = name.lower().strip()
    slug = re.sub(r"[^\w\s-]", "", slug)
    slug = re.sub(r"[\s_]+", "-", slug)
    slug = slug.strip("-")
    return slug


def _build_config(raw: dict) -> OpenClawConfig:
    """Build OpenClawConfig from parsed openclaw.json dict."""
    config = OpenClawConfig(raw=raw)

    # Model string
    config.model = raw.get("model", raw.get("defaultModel", ""))

    # Channels / tokens — openclaw.json uses "channels" as a dict keyed by platform
    channels_raw = raw.get("channels", {})
    if isinstance(channels_raw, dict):
        for platform, chan_data in channels_raw.items():
            if not isinstance(chan_data, dict):
                continue
            token = chan_data.get("token", chan_data.get("botToken", chan_data.get("apiKey", "")))
            allow_from_raw = chan_data.get("allowFrom", chan_data.get("allowedUsers", []))
            allow_from = [str(uid) for uid in allow_from_raw] if isinstance(allow_from_raw, list) else []
            settings = {k: v for k, v in chan_data.items() if k not in ("token", "botToken", "apiKey", "allowFrom", "allowedUsers")}
            config.channels.append(ChannelConfig(
                platform=platform.lower(),
                token=token,
                allow_from=allow_from,
                settings=settings,
            ))
    elif isinstance(channels_raw, list):
        # Some versions use a list of channel objects
        for chan_data in channels_raw:
            if not isinstance(chan_data, dict):
                continue
            platform = (chan_data.get("platform") or chan_data.get("type") or "").lower()
            token = chan_data.get("token", chan_data.get("botToken", ""))
            allow_from_raw = chan_data.get("allowFrom", [])
            allow_from = [str(uid) for uid in allow_from_raw] if isinstance(allow_from_raw, list) else []
            settings = {k: v for k, v in chan_data.items() if k not in ("platform", "type", "token", "botToken", "allowFrom")}
            config.channels.append(ChannelConfig(
                platform=platform,
                token=token,
                allow_from=allow_from,
                settings=settings,
            ))

    # Skill env vars — {"skillName": {"ENV_VAR": "value", ...}}
    skills_raw = raw.get("skills", raw.get("skillEnv", {}))
    if isinstance(skills_raw, dict):
        for skill_name, env in skills_raw.items():
            if isinstance(env, dict):
                config.skill_env[skill_name] = env

    return config
