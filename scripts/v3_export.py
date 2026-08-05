#!/usr/bin/env python3
"""V2 -> V3 memory/inventory exporter (PinkyBotV3 #16, pinky.import schema 1).

Reads one agent's pinky-memory store (copy-then-read; never opens the live WAL
in place) and emits the V3 import JSON. The V3 importer's dry-run is the
authoritative validator for this output — run `pinky import dry-run <file>` on
a V3 instance and treat every fatal as a bug HERE, not there.

Contract notes encoded (V3 #16):
- confidence: V2 reflections carry no confidence -> field omitted entirely
  (the schema-13 validator fatals on non-int shapes; absent is legal).
- createdAt: emitted as ISO-8601 UTC (epoch integers are fatal).
- kind: V2 reflection types are a subset of V3 MEMORY_KINDS; emitted verbatim.
- subject: V2 has no subject; synthesized from the first content line (<=200
  chars). Deliberate, lossy-upward; flagged in the manifest notes.
- project rows: distinct V2 `project` labels become V3 project records, and
  their memories scope to them. Casing collisions (V2 labels are messy) are
  pre-merged here because the V3 importer fatals on case-insensitive name
  collisions (IMPORT_PROJECT_NAME_COLLISION).
- supersedes: V2 id -> deterministic sourceKey reference.
- inactive rows: skipped by default (--include-inactive to override); V2
  "active=0" mixes superseded-with-successor and soft-forgotten, and the V3
  tombstone model wants explicit intent, not a guess.
- effective instructions: composed exclusively from V2 DB authority through
  AgentRegistry.build_system_prompt(). The generated CLAUDE.md is hashed only
  as drift evidence and is never used as an instruction source.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from pinky_daemon.agent_registry import AgentRegistry
from pinky_daemon.skill_store import SkillStore

SCHEMA_VERSION = 1
# Mirror of PinkyBotV3 import-format.ts SECRET_STRING_PATTERNS (schema 13).
# The importer fatals on first match with no row pointer, so we pre-flight
# every row here and EXCLUDE matches with a named disposition report. Real
# secrets found this way are incidents, not export rows.
SECRET_PATTERNS = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(
        r"\b(?:sk-[A-Za-z0-9_-]{16,}|xox[baprs]-[A-Za-z0-9-]{12,}|gh[pousr]_[A-Za-z0-9]{20,})\b"
    ),
    re.compile(r"\bAKIA[A-Z0-9]{16}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{16,}\b", re.I),
    re.compile(
        r"\b(?:password|passwd|token|api[_-]?key|client[_-]?secret)\s*[:=]\s*[\"\']?[^\s\"\',}]{4,}",
        re.I,
    ),
    re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{30,}\b"),
    re.compile(r"\b[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{20,}\b"),
]


def secret_scan(data: dict) -> str | None:
    for field in ("subject", "content", "context"):
        value = data.get(field, "")
        for index, pattern in enumerate(SECRET_PATTERNS):
            if pattern.search(value):
                return f"{field}:pattern{index}"
    return None


V3_MEMORY_KINDS = {
    "fact",
    "preference",
    "decision",
    "procedure",
    "observation",
    "summary",
    "insight",
    "project_state",
    "interaction_pattern",
    "continuation",
}

GENERATED_OUTPUT_KINDS = (
    "CLAUDE_SETTINGS",
    "CLAUDE_HOOKS",
    "MCP_CONFIG",
)


def iso_utc(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            return datetime.fromtimestamp(float(text), tz=timezone.utc).isoformat()
        except (ValueError, OverflowError):
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def canonical_now_utc() -> str:
    """Return the V3 contract's canonical millisecond RFC 3339 form."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def synthesize_subject(content: str) -> str:
    first = content.strip().splitlines()[0] if content.strip() else "untitled memory"
    return first[:200] if first else "untitled memory"


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_text(value: str) -> str:
    return sha256_bytes(value.encode("utf-8"))


def stable_json_sha256(value) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256_bytes(encoded)


def _copy_sqlite_bundle(source: Path, target: Path) -> None:
    """Copy a SQLite main file plus any live WAL sidecars into a private dir."""
    if not source.is_file():
        raise FileNotFoundError(f"SQLite source does not exist: {source}")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    for suffix in ("-wal", "-shm"):
        sidecar = source.with_name(source.name + suffix)
        if sidecar.exists():
            shutil.copy2(sidecar, target.with_name(target.name + suffix))


def _hash_path_evidence(path: Path, *, fallback_glob: str | None = None) -> str:
    """Hash one generated output or a canonical inventory of its tree."""
    files: list[Path]
    if path.is_file():
        return sha256_bytes(path.read_bytes())
    if path.is_dir():
        files = sorted(item for item in path.rglob("*") if item.is_file())
        root = path
    elif fallback_glob is not None:
        root = path.parent
        files = sorted(item for item in root.glob(fallback_glob) if item.is_file())
    else:
        files = []
        root = path.parent
    evidence = {
        "declaredPath": str(path),
        "files": [
            {
                "path": item.relative_to(root).as_posix(),
                "sha256": sha256_bytes(item.read_bytes()),
            }
            for item in files
        ],
        "status": "PRESENT" if files else "MISSING",
    }
    return stable_json_sha256(evidence)


def _generated_output_evidence(agent_root: Path) -> list[dict]:
    settings = agent_root / ".claude" / "settings.json"
    hooks = agent_root / ".claude" / "hooks"
    mcp = agent_root / ".mcp.json"
    digests = {
        "CLAUDE_SETTINGS": _hash_path_evidence(settings),
        # Current V2 emits hook_*.py beside settings.json. V3's contract names
        # the logical generated-output surface .claude/hooks, so bind the
        # canonical hook file set when that directory is absent.
        "CLAUDE_HOOKS": _hash_path_evidence(hooks, fallback_glob="hook*.py"),
        "MCP_CONFIG": _hash_path_evidence(mcp),
    }
    paths = {
        "CLAUDE_SETTINGS": settings,
        "CLAUDE_HOOKS": hooks,
        "MCP_CONFIG": mcp,
    }
    return [
        {
            "kind": kind,
            "legacyPath": str(paths[kind]),
            "classification": "GENERATED_OUTPUT",
            "disposition": "REGENERATE_FROM_OWNER_AUTHORITY",
            "evidenceSha256": digests[kind],
        }
        for kind in GENERATED_OUTPUT_KINDS
    ]


def _write_private_artifact(path: Path, text: str) -> None:
    """Atomically replace path with UTF-8 text that is private from creation."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(text.encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        path.chmod(0o600)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def export_agent_instructions(
    agents_db_path: Path,
    skills_db_path: Path,
    user_profiles_db_path: Path,
    agent_name: str,
    snapshot_at: str,
    authority_decision_id: str,
    instructions_out_path: Path,
    legacy_artifact_path: Path | None = None,
) -> tuple[list[dict], dict]:
    """Compose one agent's DB-authoritative instructions and inventory facts."""
    invocation_root = Path.cwd()
    target_path = instructions_out_path.resolve()
    registry: AgentRegistry | None = None
    skill_store: SkillStore | None = None
    with tempfile.TemporaryDirectory() as tmp:
        snapshot_root = Path(tmp)
        agents_copy = snapshot_root / "agents.db"
        skills_copy = snapshot_root / "skills.db"
        profiles_copy = snapshot_root / "data" / "user_profiles.db"
        _copy_sqlite_bundle(agents_db_path.resolve(), agents_copy)
        _copy_sqlite_bundle(skills_db_path.resolve(), skills_copy)
        if user_profiles_db_path.exists():
            _copy_sqlite_bundle(user_profiles_db_path.resolve(), profiles_copy)

        original_cwd = Path.cwd()
        try:
            # build_system_prompt() creates UserProfileStore with its normal
            # relative path. Point that lookup at the copied snapshot, never
            # the live DB, without introducing a second prompt composer.
            os.chdir(snapshot_root)
            registry = AgentRegistry(db_path=str(agents_copy))
            skill_store = SkillStore(db_path=str(skills_copy))
            agent = registry.get(agent_name)
            if agent is None:
                raise ValueError(f"Agent does not exist in registry: {agent_name}")
            effective = registry.build_system_prompt(
                agent_name,
                skill_store=skill_store,
            )
            if not effective:
                raise ValueError(f"DB composition is empty for agent: {agent_name}")
            directives = [
                {"directive": item.directive, "priority": item.priority}
                for item in registry.get_directives(agent_name)
            ]
            skill_catalog = skill_store.materialize_for_agent(agent_name).get("catalog", [])
            configured_root = Path(agent.working_dir or f"data/agents/{agent_name}")
            agent_root = (
                configured_root
                if configured_root.is_absolute()
                else invocation_root / configured_root
            ).resolve()
            soul = agent.soul
            provider = (getattr(agent, "runtime", "") or "pinkybot-v2").strip()
            driver = (getattr(agent, "transport", "") or provider).strip()
        finally:
            if skill_store is not None:
                skill_store.close()
            if registry is not None:
                registry.close()
            os.chdir(original_cwd)

    legacy_path = (
        legacy_artifact_path.resolve()
        if legacy_artifact_path is not None
        else (agent_root / "CLAUDE.md").resolve()
    )
    if legacy_path.name != "CLAUDE.md":
        raise ValueError("Legacy instruction artifact must be named CLAUDE.md")
    if not legacy_path.is_file():
        raise FileNotFoundError(f"Legacy instruction artifact does not exist: {legacy_path}")
    if target_path == legacy_path:
        raise ValueError("Private target artifact must not overwrite legacy CLAUDE.md")
    if not authority_decision_id.strip():
        raise ValueError("An explicit instruction authority decision ID is required")

    effective_sha = sha256_text(effective)
    legacy_sha = sha256_bytes(legacy_path.read_bytes())
    freshness = (
        "MATCHES_DB_COMPOSITION" if legacy_sha == effective_sha else "DIFFERS_FROM_DB_COMPOSITION"
    )
    _write_private_artifact(target_path, effective)

    agent_source_key = f"inventory:agent:{agent_name}"
    instruction_source_key = f"inventory:instructions:{agent_name}"
    scope = {"kind": "agent", "id": agent_name}
    records = [
        {
            "kind": "inventory",
            "sourceKey": agent_source_key,
            "scope": scope,
            "inventoryKind": "agent",
            "name": agent_source_key,
            "disposition": "preserve",
            "details": {
                "provider": provider,
                "driver": driver,
                "cwd": str(agent_root),
                "commandRef": "claude-cli" if driver == "tmux" else driver,
                "homeRef": str(agent_root),
            },
        },
        {
            "kind": "inventory",
            "sourceKey": instruction_source_key,
            "scope": scope,
            "inventoryKind": "agent-instructions",
            "name": instruction_source_key,
            "disposition": "preserve",
            "details": {
                "agentSourceKey": agent_source_key,
                "authorityKind": "V2_DB_COMPOSED",
                "authorityDecisionId": authority_decision_id.strip(),
                "compositionProtocol": "pinkybot-v2-agent-instructions",
                "compositionVersion": 1,
                "soulSha256": sha256_text(soul),
                "directiveSetSha256": stable_json_sha256(directives),
                "skillSetSha256": stable_json_sha256(skill_catalog),
                "effectiveInstructionsSha256": effective_sha,
                "sourceSnapshotBoundary": snapshot_at,
                "legacyArtifactPath": str(legacy_path),
                "legacyArtifactSha256": legacy_sha,
                "legacyArtifactFreshness": freshness,
                "targetPath": str(target_path),
                "targetSha256": effective_sha,
                "targetMode": "0600",
                "ownerAgentId": agent_name,
                "generatedOutputs": _generated_output_evidence(agent_root),
            },
        },
    ]
    return records, {
        "artifact": str(target_path),
        "effectiveInstructionsSha256": effective_sha,
        "legacyArtifactSha256": legacy_sha,
        "legacyArtifactFreshness": freshness,
    }


def export_agent(
    db_path: Path,
    agent_name: str,
    host: str,
    include_inactive: bool,
    snapshot_at: str | None = None,
) -> tuple[dict, dict]:
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / "memory.db"
        shutil.copy(db_path, copy)
        wal = db_path.with_name(db_path.name + "-wal")
        if wal.exists():
            shutil.copy(wal, copy.with_name(copy.name + "-wal"))
        conn = sqlite3.connect(f"file:{copy}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, type, content, context, project, salience, active,"
            "       no_recall, supersedes, created_at"
            "  FROM reflections ORDER BY created_at, id"
        ).fetchall()
        conn.close()

    records: list[dict] = []
    skipped: dict = {}
    # V3 lineage chains reject forks (IMPORT_REFERENCE_FORK): where several
    # rows claim the same supersedes parent, only the LATEST claimant keeps
    # the link; earlier claimants import as standalone rows (link dropped,
    # counted). V2 recorded exactly this shape at least once.
    claimants: dict[str, list] = {}
    for row in rows:
        if row["supersedes"] and (include_inactive or row["active"]):
            claimants.setdefault(row["supersedes"], []).append((row["created_at"], row["id"]))
    fork_winners = {parent: max(entries)[1] for parent, entries in claimants.items()}
    # Pre-merge project labels case-insensitively; first-seen casing wins.
    canonical_projects: dict[str, str] = {}
    for row in rows:
        label = (row["project"] or "").strip()
        if label:
            canonical_projects.setdefault(label.lower(), label)

    for folded, label in sorted(canonical_projects.items()):
        records.append(
            {
                "kind": "project",
                "sourceKey": f"project:{agent_name}:{folded}",
                "scope": {"kind": "project", "id": label},
                "data": {
                    "name": label,
                    "ownerAgent": agent_name,
                    "purpose": f"V2 project label '{label}' (imported)",
                },
            }
        )

    for row in rows:
        if not include_inactive and not row["active"]:
            skipped["inactive"] = skipped.get("inactive", 0) + 1
            continue
        kind = (row["type"] or "").strip()
        if kind not in V3_MEMORY_KINDS:
            skipped[f"unknown-kind:{kind}"] = skipped.get(f"unknown-kind:{kind}", 0) + 1
            continue
        content = (row["content"] or "").strip()
        if not content:
            skipped["empty-content"] = skipped.get("empty-content", 0) + 1
            continue
        created = iso_utc(row["created_at"])
        data: dict = {
            "kind": kind,
            "subject": synthesize_subject(content),
            "content": content,
            "salience": max(1, min(5, int(row["salience"] or 3))),
        }
        context = (row["context"] or "").strip()
        if context:
            data["context"] = context
        if created:
            data["createdAt"] = created
        if row["no_recall"]:
            data["noRecall"] = True
        if row["supersedes"] and row["id"] == fork_winners.get(row["supersedes"], row["id"]):
            data["supersedesSourceKey"] = f"memory:{agent_name}:{row['supersedes']}"
        elif row["supersedes"]:
            skipped["supersede-fork-link-dropped"] = (
                skipped.get("supersede-fork-link-dropped", 0) + 1
            )
        flagged = secret_scan(data)
        if flagged:
            skipped[f"secret-flagged({flagged})"] = skipped.get(f"secret-flagged({flagged})", 0) + 1
            rows_list = skipped.setdefault("secret-flagged-rows", [])
            rows_list.append(f"memory:{agent_name}:{row['id']}")
            continue
        label = (row["project"] or "").strip()
        scope = (
            {"kind": "project", "id": canonical_projects[label.lower()]}
            if label
            else {"kind": "agent", "id": agent_name}
        )
        records.append(
            {
                "kind": "memory",
                "sourceKey": f"memory:{agent_name}:{row['id']}",
                "scope": scope,
                "data": data,
            }
        )

    # V3 rejects dangling supersede references (IMPORT_DANGLING_REFERENCE):
    # a link may only point at a row that is itself in this export. Links to
    # excluded parents (inactive, secret-flagged, invalid) are dropped-and-
    # counted; the child still imports as a standalone row.
    exported_keys = {record["sourceKey"] for record in records}
    for record in records:
        if record["kind"] != "memory":
            continue
        target = record["data"].get("supersedesSourceKey")
        if target and target not in exported_keys:
            del record["data"]["supersedesSourceKey"]
            skipped["supersede-dangling-link-dropped"] = (
                skipped.get("supersede-dangling-link-dropped", 0) + 1
            )

    return {
        "schema_version": SCHEMA_VERSION,
        "source": {
            "system": "pinkybot-v2",
            "host": host,
            "authorityPath": str(db_path.resolve()),
            "snapshotAt": snapshot_at or canonical_now_utc(),
        },
        "records": records,
    }, skipped


# V2's approval gate is keyed by (agent_name, chat_id) with NO platform column
# (agent_registry.approved_users), while V3 principals bind per connector. The
# platform is therefore RESOLVED here, never assumed: group_chats rows carry an
# explicit platform; DM ids fall back to a shape heuristic; anything else is
# UNRESOLVED and must be settled by the owner at Phase-5 qualification against
# the old-lease-stop record. Every proposal row ships disposition
# "defer-with-owner-approval" so the V3 dry-run classifies it
# INVENTORY_DEFERRED_OWNER_APPROVAL — visible for the ceremony, never imported
# as an enabled principal.
PLATFORM_SOURCE_GROUP_TABLE = "group_chats"
PLATFORM_SOURCE_ID_SHAPE = "id-shape-heuristic"
PLATFORM_SOURCE_UNRESOLVED = "unresolved"


def _resolve_platform(chat_id: str, group_platforms: dict[str, str]) -> tuple[str, str]:
    if chat_id in group_platforms:
        return group_platforms[chat_id], PLATFORM_SOURCE_GROUP_TABLE
    text = chat_id.strip()
    if text in ("web", "admin"):
        return "web", PLATFORM_SOURCE_ID_SHAPE
    if re.fullmatch(r"-\d{1,16}", text):
        return "telegram", PLATFORM_SOURCE_ID_SHAPE
    if re.fullmatch(r"\d{17,20}", text):
        return "discord", PLATFORM_SOURCE_ID_SHAPE
    if re.fullmatch(r"\d{1,13}", text):
        return "telegram", PLATFORM_SOURCE_ID_SHAPE
    return "unknown", PLATFORM_SOURCE_UNRESOLVED


_SAFE_SOURCE_KEY_COMPONENT = re.compile(r"[A-Za-z0-9._:@-]{1,80}")


def _principal_key_component(chat_id: str) -> str:
    """Return a V3 source-key-safe component for an arbitrary V2 chat id.

    Realistic V2 identities include +phone, whitespace, and Unicode, all of
    which fatal V3's SOURCE_KEY_INVALID. Clean ids stay readable under a
    ``raw-`` prefix; anything else becomes a stable ``sha256-`` digest. The
    prefixes make the two namespaces disjoint BY CONSTRUCTION — a raw id
    that happens to look like a digest can never collide with a hashed one
    (review round 2 found exactly that SOURCE_KEY_DUPLICATE). Raw ids are
    preserved in conversationRef/legacyId either way.
    """
    if _SAFE_SOURCE_KEY_COMPONENT.fullmatch(chat_id):
        return f"raw-{chat_id}"
    return f"sha256-{sha256_text(chat_id)[:24]}"


def _md_cell(value) -> str:
    """Escape a DB-sourced value for the owner-decision Markdown table.

    A crafted display name containing pipes/newlines could otherwise forge
    rows in the artifact the owner signs off against.
    """
    text = str(value)
    text = "".join(
        ch for ch in text
        if (ord(ch) >= 0x20 and not (0x7F <= ord(ch) <= 0x9F)) or ch == "\t"
    )
    return text.replace("\\", "\\\\").replace("|", "\\|").replace("\t", " ")


def export_messaging_continuity(
    agents_db_path: Path,
    agent_name: str,
    snapshot_at: str,
    lease_stop_record_path: Path | None = None,
) -> tuple[list[dict], dict, str]:
    """Propose the V3 connector-principal set from V2 gate + group state.

    Returns (records, summary, proposal_markdown). Records carry
    disposition "defer-with-owner-approval" by design: enabling principals
    is the Phase-5 owner ceremony's decision, not an import side effect.

    ``lease_stop_record_path`` is the Phase-5 step-2 old-lease-stop record
    (``{"activeChats": [{"chatId": ..., "platform": ..., "title": ...}]}``).
    When present, chats active at lease stop but absent from every V2 table
    are surfaced as their own status instead of relying on the operator's
    manual cross-check; pre-cutover exports may omit it.
    """
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / "agents.db"
        _copy_sqlite_bundle(agents_db_path.resolve(), copy)
        conn = sqlite3.connect(f"file:{copy}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        approved = conn.execute(
            "SELECT chat_id, display_name, status, approved_by, timezone"
            "  FROM approved_users WHERE agent_name=? ORDER BY created_at, id",
            (agent_name,),
        ).fetchall()
        groups = conn.execute(
            "SELECT chat_id, platform, chat_title, alias, chat_type, active"
            "  FROM group_chats WHERE agent_name=? ORDER BY joined_at, id",
            (agent_name,),
        ).fetchall()
        # delivered=1 rows are retained delivery HISTORY (V2 checkpoints, not
        # deletes); counting them ordered phantom drains in review round 1.
        held = {
            row["chat_id"]: row["held"]
            for row in conn.execute(
                "SELECT chat_id, COUNT(*) AS held FROM pending_messages"
                "  WHERE agent_name=? AND delivered=0 GROUP BY chat_id",
                (agent_name,),
            ).fetchall()
        }
        conn.close()

    group_platforms = {row["chat_id"]: row["platform"] for row in groups}
    group_meta = {row["chat_id"]: row for row in groups}
    approved_ids = {row["chat_id"] for row in approved}

    proposals: list[dict] = []
    for row in approved:
        platform, source = _resolve_platform(row["chat_id"], group_platforms)
        meta = group_meta.get(row["chat_id"])
        proposals.append({
            "externalPrincipalId": row["chat_id"],
            "platform": platform,
            "platformSource": source,
            "displayName": row["display_name"] or (meta["chat_title"] if meta else ""),
            "gateStatus": row["status"],
            "approvedBy": row["approved_by"],
            "isGroup": meta is not None and meta["chat_type"] != "dm",
            "heldPendingInbound": held.get(row["chat_id"], 0),
            "proposedRole": "UNASSIGNED_PENDING_OWNER_DECISION",
            "gateSource": "approved_users",
        })
    # Active groups the gate table never recorded are the V2 "orphaned group"
    # failure class in the making — surface them rather than silently dropping.
    for row in groups:
        if row["chat_id"] in approved_ids or not row["active"]:
            continue
        proposals.append({
            "externalPrincipalId": row["chat_id"],
            "platform": row["platform"],
            "platformSource": PLATFORM_SOURCE_GROUP_TABLE,
            "displayName": row["chat_title"] or row["alias"],
            "gateStatus": "group-active-without-gate-row",
            "approvedBy": "",
            "isGroup": True,
            "heldPendingInbound": held.get(row["chat_id"], 0),
            "proposedRole": "UNASSIGNED_PENDING_OWNER_DECISION",
            "gateSource": "group_chats",
        })
    # Undelivered inbound from senders with NO gate row and NO group row is
    # the NORMAL gate-held shape (a stranger's message is why the gate
    # exists) — round 1 silently dropped exactly this class.
    covered = {p["externalPrincipalId"] for p in proposals}
    for chat_id, count in sorted(held.items()):
        if chat_id in covered:
            continue
        platform, source = _resolve_platform(chat_id, group_platforms)
        proposals.append({
            "externalPrincipalId": chat_id,
            "platform": platform,
            "platformSource": source,
            "displayName": "",
            "gateStatus": "pending-inbound-without-gate-row",
            "approvedBy": "",
            "isGroup": False,
            "heldPendingInbound": count,
            "proposedRole": "UNASSIGNED_PENDING_OWNER_DECISION",
            "gateSource": "pending_messages",
        })

    # Phase-5 continuity hole closure: anything alive at old-lease stop but
    # unknown to every V2 table must confront the owner explicitly.
    lease_stop_status = "no-record"
    if lease_stop_record_path is not None:
        lease_stop = json.loads(lease_stop_record_path.read_text())
        active_chats = lease_stop.get("activeChats")
        if not isinstance(active_chats, list):
            raise ValueError(
                "Lease-stop record must contain an 'activeChats' list"
            )
        lease_stop_ids = set()
        for entry in active_chats:
            chat_id = str(entry.get("chatId", "")).strip()
            if not chat_id:
                raise ValueError("Lease-stop activeChats entry lacks chatId")
            lease_stop_ids.add(chat_id)
            if chat_id in {p["externalPrincipalId"] for p in proposals}:
                continue
            proposals.append({
                "externalPrincipalId": chat_id,
                "platform": str(entry.get("platform", "unknown")),
                "platformSource": "lease-stop-record",
                "displayName": str(entry.get("title", "")),
                "gateStatus": "active-without-v2-state",
                "approvedBy": "",
                "isGroup": bool(entry.get("isGroup", False)),
                "heldPendingInbound": held.get(chat_id, 0),
                "proposedRole": "UNASSIGNED_PENDING_OWNER_DECISION",
                "gateSource": "lease-stop-record",
            })
        for p in proposals:
            if p["gateSource"] == "lease-stop-record":
                p["leaseStop"] = "only-in-lease-stop"
            else:
                p["leaseStop"] = (
                    "confirmed"
                    if p["externalPrincipalId"] in lease_stop_ids
                    else "absent-at-lease-stop"
                )
        lease_stop_status = "reconciled"
    else:
        for p in proposals:
            p["leaseStop"] = "no-record"

    # The connector-binding detail schema is a FATAL-enforced allowlist of
    # bounded identity refs plus the common keys (import-format.ts
    # INVENTORY_DETAIL_KEYS) — verified against the real inspector, which
    # rejected the first draft's free-form fields. Rich provenance lives in
    # the ceremony proposal document; the ledger row carries only bounded
    # facts, with the human-facing fields folded into `notes`.
    scope = {"kind": "agent", "id": agent_name}
    records = [
        {
            "kind": "inventory",
            "sourceKey": (
                f"inventory:connector-principal:{agent_name}:"
                f"{_principal_key_component(item['externalPrincipalId'])}"
            ),
            "scope": scope,
            "inventoryKind": "connector-binding",
            "name": (
                f"inventory:connector-principal:{agent_name}:"
                f"{_principal_key_component(item['externalPrincipalId'])}"
            ),
            "disposition": "defer-with-owner-approval",
            "details": {
                "provider": item["platform"],
                "conversationRef": item["externalPrincipalId"],
                "agentRef": agent_name,
                "legacyId": item["externalPrincipalId"],
                "status": item["gateStatus"],
                "notes": (
                    f"display={item['displayName'] or '-'};"
                    f" platformSource={item['platformSource']};"
                    f" group={'yes' if item['isGroup'] else 'no'};"
                    f" heldPendingInbound={item['heldPendingInbound']};"
                    f" approvedBy={item['approvedBy'] or '-'};"
                    f" gateSource={item['gateSource']};"
                    f" leaseStop={item['leaseStop']};"
                    f" role=PENDING_OWNER_DECISION;"
                    f" snapshotAt={snapshot_at}"
                ),
            },
        }
        for item in proposals
    ]

    unresolved = [p for p in proposals if p["platformSource"] == PLATFORM_SOURCE_UNRESOLVED]
    held_total = sum(p["heldPendingInbound"] for p in proposals)
    lease_stop_only = [p for p in proposals if p["gateSource"] == "lease-stop-record"]
    lines = [
        f"# Phase-5 principal proposal — {agent_name} ({snapshot_at})",
        "",
        "Every row requires an explicit owner enable at qualification; nothing",
        "below is imported as an enabled principal.",
        (
            "Lease-stop record: RECONCILED below."
            if lease_stop_status == "reconciled"
            else "Lease-stop record: NOT provided — cross-check active"
            " chats/topics/senders manually before enabling."
        ),
        "",
        "| principal id | platform (source) | name | gate status | group | held inbound | lease stop |",
        "|---|---|---|---|---|---|---|",
    ]
    for p in proposals:
        lines.append(
            f"| {_md_cell(p['externalPrincipalId'])} "
            f"| {_md_cell(p['platform'])} ({_md_cell(p['platformSource'])}) "
            f"| {_md_cell(p['displayName'])} | {_md_cell(p['gateStatus'])} "
            f"| {p['isGroup']} | {p['heldPendingInbound']} "
            f"| {_md_cell(p['leaseStop'])} |"
        )
    lines += [
        "",
        "## Ceremony checklist",
        "- [ ] Every expected chat above is present (owner DM + all groups).",
        f"- [ ] {len(unresolved)} unresolved-platform row(s) settled against the lease-stop record.",
        f"- [ ] {held_total} gate-pending inbound message(s) drained/classified before old-lease stop.",
        f"- [ ] {len(lease_stop_only)} lease-stop-only chat(s) (active at stop, unknown to V2 tables) decided.",
        "- [ ] NEGATIVE CONTROL after enable: a known-unapproved chat id still gets the",
        "      generic denial — the gate itself must survive the migration, not just the approvals.",
    ]
    summary = {
        "proposals": len(proposals),
        "unresolvedPlatform": len(unresolved),
        "heldPendingInbound": held_total,
        "groupsWithoutGateRow": sum(
            1 for p in proposals if p["gateStatus"] == "group-active-without-gate-row"
        ),
        "pendingWithoutGateRow": sum(
            1 for p in proposals if p["gateStatus"] == "pending-inbound-without-gate-row"
        ),
        "leaseStopOnly": len(lease_stop_only),
        "leaseStopStatus": lease_stop_status,
    }
    return records, summary, "\n".join(lines) + "\n"


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("db", type=Path)
    ap.add_argument("agent")
    ap.add_argument("--host", default="mini")
    ap.add_argument("--include-inactive", action="store_true")
    ap.add_argument("--agents-db", type=Path, default=Path("data/conversations_agents.db"))
    ap.add_argument("--skills-db", type=Path, default=Path("data/conversations_skills.db"))
    ap.add_argument("--user-profiles-db", type=Path, default=Path("data/user_profiles.db"))
    ap.add_argument("--legacy-artifact", type=Path)
    ap.add_argument("--instructions-out", type=Path)
    ap.add_argument("--authority-decision-id", required=True)
    ap.add_argument("--lease-stop-record", type=Path)
    ap.add_argument("-o", "--out", type=Path, required=True)
    args = ap.parse_args()
    snapshot_at = canonical_now_utc()
    result, skipped = export_agent(
        args.db,
        args.agent,
        args.host,
        args.include_inactive,
        snapshot_at,
    )
    instructions_out = (
        args.instructions_out
        if args.instructions_out is not None
        else args.out.with_name(f"{args.out.stem}.{args.agent}.instructions.md")
    )
    instruction_records, instruction_summary = export_agent_instructions(
        args.agents_db,
        args.skills_db,
        args.user_profiles_db,
        args.agent,
        snapshot_at,
        args.authority_decision_id,
        instructions_out,
        args.legacy_artifact,
    )
    continuity_records, continuity_summary, proposal_doc = export_messaging_continuity(
        args.agents_db,
        args.agent,
        snapshot_at,
        lease_stop_record_path=args.lease_stop_record,
    )
    proposal_out = args.out.with_name(f"{args.out.stem}.{args.agent}.principals.md")
    proposal_out.write_text(proposal_doc)
    result["records"] = instruction_records + continuity_records + result["records"]
    args.out.write_text(json.dumps(result, indent=1, ensure_ascii=False))
    counts: dict[str, int] = {}
    for record in result["records"]:
        counts[record["kind"]] = counts.get(record["kind"], 0) + 1
    print(
        json.dumps(
            {
                "records": counts,
                "skipped": skipped,
                "notes": {
                    "subject_synthesis": "first content line, <=200 chars",
                    "confidence": "omitted (V2 reflections carry none)",
                    "instruction_authority": "V2_DB_COMPOSED",
                },
                "instructions": instruction_summary,
                "messagingContinuity": {**continuity_summary, "proposal": str(proposal_out)},
                "out": str(args.out),
            },
            indent=1,
        )
    )
