"""Tests for the V2 -> V3 cutover exporter."""

from __future__ import annotations

import hashlib
import stat
from pathlib import Path

import pytest

from pinky_daemon.agent_registry import AgentRegistry
from pinky_daemon.skill_store import SkillStore
from pinky_daemon.user_profile_store import UserProfileStore
from scripts.v3_export import export_agent_instructions

SNAPSHOT_AT = "2026-08-05T12:34:56.789Z"
INSTRUCTION_DETAIL_KEYS = {
    "agentSourceKey",
    "authorityDecisionId",
    "authorityKind",
    "compositionProtocol",
    "compositionVersion",
    "directiveSetSha256",
    "effectiveInstructionsSha256",
    "generatedOutputs",
    "legacyArtifactFreshness",
    "legacyArtifactPath",
    "legacyArtifactSha256",
    "ownerAgentId",
    "skillSetSha256",
    "soulSha256",
    "sourceSnapshotBoundary",
    "targetMode",
    "targetPath",
    "targetSha256",
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def v2_instruction_authority(tmp_path: Path) -> dict[str, Path]:
    agent_root = tmp_path / "agent-home"
    agents_db = tmp_path / "conversations_agents.db"
    skills_db = tmp_path / "conversations_skills.db"
    profiles_db = tmp_path / "user_profiles.db"

    registry = AgentRegistry(db_path=str(agents_db))
    registry.register(
        "barsik",
        soul="# Database Soul\nCurious and exact.",
        boundaries="## Database Boundaries\nNever guess.",
        working_dir=str(agent_root),
        runtime="claude_code",
        transport="tmux",
    )
    registry.add_directive("barsik", "Keep durable receipts", priority=90)
    registry.set_owner_profile(
        {
            "name": "Oleg",
            "timezone": "America/Los_Angeles",
            "comm_style": "Direct",
        }
    )
    registry.close()

    skills = SkillStore(db_path=str(skills_db))
    skills.register("calendar", description="Calendar operations")
    skills.assign_to_agent("barsik", "calendar")
    skills.close()

    profiles = UserProfileStore(db_path=str(profiles_db))
    profiles.close()

    # These are generated artifacts and evidence only. In particular, the
    # deliberately stale CLAUDE.md marker must never enter composed output.
    legacy = agent_root / "CLAUDE.md"
    legacy.write_text("STALE LEGACY INSTRUCTIONS — NEVER SOURCE THIS\n")
    claude_dir = agent_root / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    (claude_dir / "settings.json").write_text('{"hooks": {}}\n')
    (claude_dir / "hook_alpha.py").write_text("print('hook')\n")
    (agent_root / ".mcp.json").write_text('{"mcpServers": {}}\n')

    return {
        "root": agent_root,
        "legacy": legacy,
        "agents_db": agents_db,
        "skills_db": skills_db,
        "profiles_db": profiles_db,
    }


def _export(paths: dict[str, Path], target: Path) -> tuple[list[dict], dict]:
    return export_agent_instructions(
        paths["agents_db"],
        paths["skills_db"],
        paths["profiles_db"],
        "barsik",
        SNAPSHOT_AT,
        "owner-decision:barsik:instructions-v1",
        target,
        paths["legacy"],
    )


def test_exports_db_composition_to_private_artifact(
    v2_instruction_authority: dict[str, Path], tmp_path: Path
) -> None:
    paths = v2_instruction_authority
    source_hashes = {
        "agents": _sha256(paths["agents_db"]),
        "skills": _sha256(paths["skills_db"]),
        "profiles": _sha256(paths["profiles_db"]),
    }
    target = tmp_path / "private" / "barsik-instructions.md"

    records, summary = _export(paths, target)

    assert _sha256(paths["agents_db"]) == source_hashes["agents"]
    assert _sha256(paths["skills_db"]) == source_hashes["skills"]
    assert _sha256(paths["profiles_db"]) == source_hashes["profiles"]
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    composed = target.read_text()
    assert "# Database Soul" in composed
    assert "## Database Boundaries" in composed
    assert "Keep durable receipts" in composed
    assert "Equipped: calendar" in composed
    assert "**Name:** Oleg" in composed
    assert "STALE LEGACY INSTRUCTIONS" not in composed

    agent_record, instruction_record = records
    assert agent_record == {
        "kind": "inventory",
        "sourceKey": "inventory:agent:barsik",
        "scope": {"kind": "agent", "id": "barsik"},
        "inventoryKind": "agent",
        "name": "inventory:agent:barsik",
        "disposition": "preserve",
        "details": {
            "provider": "claude_code",
            "driver": "tmux",
            "cwd": str(paths["root"]),
            "commandRef": "claude-cli",
            "homeRef": str(paths["root"]),
        },
    }
    assert instruction_record["sourceKey"] == "inventory:instructions:barsik"
    assert instruction_record["name"] == instruction_record["sourceKey"]
    assert instruction_record["scope"] == {"kind": "agent", "id": "barsik"}
    assert instruction_record["inventoryKind"] == "agent-instructions"
    assert instruction_record["disposition"] == "preserve"

    details = instruction_record["details"]
    assert set(details) == INSTRUCTION_DETAIL_KEYS
    assert details["agentSourceKey"] == agent_record["sourceKey"]
    assert details["authorityKind"] == "V2_DB_COMPOSED"
    assert details["sourceSnapshotBoundary"] == SNAPSHOT_AT
    assert details["ownerAgentId"] == "barsik"
    assert details["targetPath"] == str(target)
    assert details["targetMode"] == "0600"
    assert details["targetSha256"] == _sha256(target)
    assert details["effectiveInstructionsSha256"] == _sha256(target)
    assert details["legacyArtifactSha256"] == _sha256(paths["legacy"])
    assert details["legacyArtifactFreshness"] == "DIFFERS_FROM_DB_COMPOSITION"
    assert [item["kind"] for item in details["generatedOutputs"]] == [
        "CLAUDE_SETTINGS",
        "CLAUDE_HOOKS",
        "MCP_CONFIG",
    ]
    assert all(item["evidenceSha256"] != "0" * 64 for item in details["generatedOutputs"])
    assert summary["artifact"] == str(target)
    assert summary["legacyArtifactFreshness"] == "DIFFERS_FROM_DB_COMPOSITION"


def test_freshness_matches_only_when_legacy_bytes_equal_db_composition(
    v2_instruction_authority: dict[str, Path], tmp_path: Path
) -> None:
    paths = v2_instruction_authority
    first_target = tmp_path / "first.md"
    _export(paths, first_target)
    paths["legacy"].write_bytes(first_target.read_bytes())

    second_target = tmp_path / "second.md"
    records, summary = _export(paths, second_target)

    details = records[1]["details"]
    assert details["legacyArtifactSha256"] == details["effectiveInstructionsSha256"]
    assert details["legacyArtifactFreshness"] == "MATCHES_DB_COMPOSITION"
    assert summary["legacyArtifactFreshness"] == "MATCHES_DB_COMPOSITION"


def test_refuses_to_overwrite_legacy_artifact(v2_instruction_authority: dict[str, Path]) -> None:
    paths = v2_instruction_authority
    original = paths["legacy"].read_bytes()

    with pytest.raises(ValueError, match="must not overwrite"):
        _export(paths, paths["legacy"])

    assert paths["legacy"].read_bytes() == original


# ── messaging continuity (task #516 / V3#12 item 4) ──────────────────────────

from scripts.v3_export import (  # noqa: E402
    PLATFORM_SOURCE_GROUP_TABLE,
    PLATFORM_SOURCE_ID_SHAPE,
    PLATFORM_SOURCE_UNRESOLVED,
    _resolve_platform,
    export_messaging_continuity,
)


@pytest.fixture
def v2_messaging_state(tmp_path: Path) -> Path:
    agents_db = tmp_path / "conversations_agents.db"
    registry = AgentRegistry(db_path=str(agents_db))
    registry.register("barsik", soul="soul", working_dir=str(tmp_path / "home"))
    registry.approve_user("barsik", "6770805286", display_name="Brad", approved_by="owner")
    registry.approve_user("barsik", "-5270435808", display_name="B & Barsik", approved_by="owner")
    registry.approve_user(
        "barsik", "754027672526389310", display_name="bradbrok", approved_by="owner"
    )
    registry.approve_user("barsik", "web", display_name="Console", approved_by="owner")
    # A gate-approved group with an explicit platform row.
    registry.upsert_group_chat(
        "barsik", "-5270435808", chat_title="B & Barsik, Yulia", platform="telegram"
    )
    # The orphaned-group failure class: active group, NO approved_users row.
    registry.upsert_group_chat(
        "barsik", "-5476255431", chat_title="B, Barsik and Yulia", platform="telegram"
    )
    # Gate-pending inbound that must be drained before old-lease stop.
    registry.queue_pending_message(
        "barsik", "telegram", "999999999999999", "Stranger", "hello?"
    )
    registry.approve_user("barsik", "999999999999999", display_name="Fourteen Digits")
    registry.close()
    return agents_db


def test_platform_resolution_provenance() -> None:
    groups = {"-5476255431": "telegram"}
    assert _resolve_platform("-5476255431", groups) == ("telegram", PLATFORM_SOURCE_GROUP_TABLE)
    assert _resolve_platform("6770805286", {}) == ("telegram", PLATFORM_SOURCE_ID_SHAPE)
    assert _resolve_platform("-5270435808", {}) == ("telegram", PLATFORM_SOURCE_ID_SHAPE)
    assert _resolve_platform("754027672526389310", {}) == ("discord", PLATFORM_SOURCE_ID_SHAPE)
    assert _resolve_platform("web", {}) == ("web", PLATFORM_SOURCE_ID_SHAPE)
    assert _resolve_platform("999999999999999", {})[1] == PLATFORM_SOURCE_UNRESOLVED
    assert _resolve_platform("ferry://pi/geordi", {})[1] == PLATFORM_SOURCE_UNRESOLVED


def test_every_proposal_is_deferred_never_imported(v2_messaging_state: Path) -> None:
    records, summary, doc = export_messaging_continuity(
        v2_messaging_state, "barsik", SNAPSHOT_AT
    )
    assert records, "expected at least one proposal record"
    assert all(r["disposition"] == "defer-with-owner-approval" for r in records)
    assert all(r["inventoryKind"] == "connector-binding" for r in records)
    assert all(r["scope"] == {"kind": "agent", "id": "barsik"} for r in records)
    allowed = {
        "provider", "accountRef", "appRef", "botRef", "communityRef",
        "conversationRef", "channelRef", "agentRef", "endpointRef", "secretRef",
        "legacyId", "notes", "reason", "replacement", "status", "version",
    }
    for r in records:
        assert set(r["details"]) <= allowed, set(r["details"]) - allowed
        assert "role=PENDING_OWNER_DECISION" in r["details"]["notes"]
    assert "NEGATIVE CONTROL" in doc


def test_orphaned_active_group_is_surfaced_not_dropped(v2_messaging_state: Path) -> None:
    records, summary, _doc = export_messaging_continuity(
        v2_messaging_state, "barsik", SNAPSHOT_AT
    )
    orphans = [
        r for r in records
        if r["details"]["status"] == "group-active-without-gate-row"
    ]
    assert [r["details"]["conversationRef"] for r in orphans] == ["-5476255431"]
    assert summary["groupsWithoutGateRow"] == 1


def test_held_pending_inbound_counted_and_unresolved_flagged(
    v2_messaging_state: Path,
) -> None:
    records, summary, doc = export_messaging_continuity(
        v2_messaging_state, "barsik", SNAPSHOT_AT
    )
    by_id = {r["details"]["conversationRef"]: r["details"] for r in records}
    assert "heldPendingInbound=1" in by_id["999999999999999"]["notes"]
    assert f"platformSource={PLATFORM_SOURCE_UNRESOLVED}" in by_id["999999999999999"]["notes"]
    assert summary["heldPendingInbound"] == 1
    assert summary["unresolvedPlatform"] == 1
    assert "1 gate-pending inbound" in doc


def test_group_platform_row_beats_id_heuristic(v2_messaging_state: Path) -> None:
    records, _summary, _doc = export_messaging_continuity(
        v2_messaging_state, "barsik", SNAPSHOT_AT
    )
    by_id = {r["details"]["conversationRef"]: r["details"] for r in records}
    assert f"platformSource={PLATFORM_SOURCE_GROUP_TABLE}" in by_id["-5270435808"]["notes"]
    assert "group=yes" in by_id["-5270435808"]["notes"]
