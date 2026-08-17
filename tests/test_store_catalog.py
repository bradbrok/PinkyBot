from __future__ import annotations

import os
from pathlib import Path

import pytest

from pinky_daemon.store_catalog import StoreCatalog, StoreCatalogError, StoreRecord


def _register(
    catalog: StoreCatalog,
    logical_name: str,
    path: Path | str,
    *,
    journal_mode: str = "wal",
    owner: str | None = None,
) -> None:
    catalog.register(
        logical_name,
        path,
        journal_mode=journal_mode,
        owner=owner or f"{logical_name.title()}Store",
    )


def test_register_snapshot_round_trip(tmp_path: Path) -> None:
    db_path = tmp_path / "tasks.db"
    db_path.touch()
    catalog = StoreCatalog(expected_root=tmp_path)

    _register(catalog, "tasks", db_path, owner="TaskStore")

    stat = db_path.stat()
    assert catalog.snapshot() == [
        StoreRecord(
            logical_name="tasks",
            resolved_path=os.path.realpath(db_path),
            journal_mode="wal",
            owner="TaskStore",
            criticality="authoritative",
            dev_ino=(stat.st_dev, stat.st_ino),
            is_memory=False,
        )
    ]


def test_realpath_canonicalization_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "tasks.db"
    target.touch()
    alias = tmp_path / "tasks-alias.db"
    alias.symlink_to(target)
    catalog = StoreCatalog(expected_root=tmp_path)
    monkeypatch.chdir(tmp_path)

    _register(catalog, "tasks", "tasks.db", owner="TaskStore")
    _register(catalog, "tasks", target, owner="TaskStore")
    _register(catalog, "tasks", alias, owner="TaskStore")

    records = catalog.snapshot()
    assert len(records) == 1
    assert records[0].resolved_path == os.path.realpath(target)


def test_aliasing_with_different_owners_and_modes_fails(tmp_path: Path) -> None:
    shared = tmp_path / "shared.db"
    shared.touch()
    catalog = StoreCatalog(expected_root=tmp_path)
    _register(catalog, "sessions", shared, journal_mode="wal", owner="SessionStore")
    _register(
        catalog,
        "session_events",
        shared,
        journal_mode="truncate",
        owner="SessionEventStore",
    )

    with pytest.raises(StoreCatalogError) as exc_info:
        catalog.validate()

    message = str(exc_info.value)
    assert "sessions" in message
    assert "session_events" in message
    assert "wal" in message
    assert "truncate" in message


def test_aliasing_with_identical_mode_and_shared_owner_passes(tmp_path: Path) -> None:
    shared = tmp_path / "sessions.db"
    shared.touch()
    catalog = StoreCatalog(expected_root=tmp_path)
    owner = "SessionStore+SessionEventStore"
    _register(catalog, "sessions", shared, owner=owner)
    _register(catalog, "session_events", shared, owner=owner)

    catalog.validate()


def test_journal_mode_mismatch_on_shared_file_fails(tmp_path: Path) -> None:
    shared = tmp_path / "shared.db"
    shared.touch()
    catalog = StoreCatalog(expected_root=tmp_path)
    _register(catalog, "tasks", shared, journal_mode="wal", owner="TaskWriter")
    _register(catalog, "tasks", shared, journal_mode="truncate", owner="TaskReader")

    with pytest.raises(StoreCatalogError, match="journal mode"):
        catalog.validate()


def test_in_memory_connections_do_not_false_alias(tmp_path: Path) -> None:
    catalog = StoreCatalog(expected_root=tmp_path)
    _register(
        catalog,
        "tasks",
        ":memory:",
        journal_mode="memory",
        owner="TaskStore",
    )
    _register(
        catalog,
        "audit",
        ":memory:",
        journal_mode="memory",
        owner="AuditStore",
    )

    catalog.validate()


@pytest.mark.parametrize(
    "path",
    [
        ":memory:",
        "file::memory:",
        "file::memory:?cache=shared",
        "file::memory:?cache=shared#frag",
        "file:shared-cache?mode=memory&cache=shared",
        "file:shared-cache?mode=memory#frag",
        "file:shared-cache?mode=rwc&mode=memory",
    ],
)
def test_in_memory_identity_comes_from_raw_path(tmp_path: Path, path: str) -> None:
    catalog = StoreCatalog(expected_root=tmp_path)
    _register(catalog, "tasks", path, journal_mode="memory", owner="TaskStore")

    assert catalog.snapshot()[0].is_memory is True
    catalog.validate()


@pytest.mark.parametrize(
    "query",
    [
        "MODE=memory",
        "mode=memory&mode=rwc",
    ],
)
def test_non_effective_memory_uri_mode_is_not_exempted(tmp_path: Path, query: str) -> None:
    expected_root = tmp_path / "data"
    expected_root.mkdir()
    outside = tmp_path / "outside.db"
    outside.touch()
    catalog = StoreCatalog(expected_root=expected_root)
    uri = f"file:{outside}?{query}"
    _register(catalog, "tasks", uri, journal_mode="memory", owner="TaskStore")

    assert catalog.snapshot()[0].is_memory is False
    with pytest.raises(StoreCatalogError):
        catalog.validate()


def test_on_disk_memory_mode_alias_is_not_exempt(tmp_path: Path) -> None:
    shared = tmp_path / "shared.db"
    shared.touch()
    catalog = StoreCatalog(expected_root=tmp_path)
    _register(catalog, "tasks", shared, journal_mode="memory", owner="TaskStore")
    _register(catalog, "audit", shared, journal_mode="memory", owner="AuditStore")

    with pytest.raises(StoreCatalogError, match="same physical file"):
        catalog.validate()


def test_on_disk_memory_mode_outside_root_is_not_exempt(tmp_path: Path) -> None:
    expected_root = tmp_path / "data"
    expected_root.mkdir()
    outside = tmp_path / "outside.db"
    outside.touch()
    catalog = StoreCatalog(expected_root=expected_root)
    _register(catalog, "tasks", outside, journal_mode="memory", owner="TaskStore")

    with pytest.raises(StoreCatalogError, match="expected root"):
        catalog.validate()


def test_relative_path_fails_validation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    catalog = StoreCatalog(expected_root=tmp_path)
    _register(catalog, "tasks", "tasks.db", owner="TaskStore")

    with pytest.raises(StoreCatalogError, match="relative path"):
        catalog.validate()


def test_path_outside_expected_root_fails_validation(tmp_path: Path) -> None:
    expected_root = tmp_path / "data"
    expected_root.mkdir()
    outside = tmp_path / "outside.db"
    catalog = StoreCatalog(expected_root=expected_root)
    _register(catalog, "tasks", outside, owner="TaskStore")

    with pytest.raises(StoreCatalogError, match="expected root"):
        catalog.validate()


def test_unopened_absolute_store_does_not_fail(tmp_path: Path) -> None:
    catalog = StoreCatalog(expected_root=tmp_path)
    _register(catalog, "tasks", tmp_path / "not-opened.db", owner="TaskStore")

    catalog.validate()
    assert catalog.snapshot()[0].dev_ino is None


def test_duplicate_logical_name_with_divergent_path_fails(tmp_path: Path) -> None:
    catalog = StoreCatalog(expected_root=tmp_path)
    _register(catalog, "tasks", tmp_path / "tasks-a.db", owner="TaskStore")
    _register(catalog, "tasks", tmp_path / "tasks-b.db", owner="TaskStore")

    with pytest.raises(StoreCatalogError) as exc_info:
        catalog.validate()

    message = str(exc_info.value)
    assert "duplicate logical name" in message
    assert "tasks-a.db" in message
    assert "tasks-b.db" in message


def test_clean_production_shaped_layout_passes(tmp_path: Path) -> None:
    catalog = StoreCatalog(expected_root=tmp_path)
    stores = {
        "conversations": ("truncate", "ConversationStore"),
        "tasks": ("wal", "TaskStore"),
        "triggers": ("truncate", "TriggerStore"),
        "outreach_config": ("truncate", "OutreachConfigStore"),
        "user_profiles": ("truncate", "UserProfileStore"),
        "agents": ("truncate", "AgentRegistry"),
        "activity": ("wal", "ActivityStore"),
        "analytics": ("wal", "AnalyticsStore"),
        "mesh": ("wal", "MeshStore"),
        "research": ("wal", "ResearchStore"),
        "presentations": ("wal", "PresentationStore"),
        "apps": ("wal", "AppStore"),
        "skills": ("wal", "SkillStore"),
        "voice": ("wal", "VoiceStore"),
        "audit": ("wal", "AuditStore"),
        "message_context": ("wal", "MessageContextStore"),
        "dream_state": ("wal", "DreamRunner"),
    }
    for logical_name, (journal_mode, owner) in stores.items():
        db_path = tmp_path / f"{logical_name}.db"
        db_path.touch()
        _register(
            catalog,
            logical_name,
            db_path,
            journal_mode=journal_mode,
            owner=owner,
        )

    kb_path = tmp_path / "kb" / "kb.db"
    kb_path.parent.mkdir()
    kb_path.touch()
    _register(catalog, "kb", kb_path, owner="KBStore")

    sessions_path = tmp_path / "sessions.db"
    sessions_path.touch()
    session_owner = "SessionStore+SessionEventStore"
    _register(catalog, "sessions", sessions_path, owner=session_owner)
    _register(catalog, "session_events", sessions_path, owner=session_owner)

    for allowlisted_name in (
        "hub.db",
        "pinky.db",
        "pinkybot.db",
        "daemon.db",
        "messages.db",
        "pinky_daemon.db",
        "scheduler.db",
    ):
        (tmp_path / allowlisted_name).touch()

    assert catalog.validate() == []


def test_symlink_and_target_are_detected_as_same_inode(tmp_path: Path) -> None:
    target = tmp_path / "shared.db"
    target.touch()
    alias = tmp_path / "shared-alias.db"
    alias.symlink_to(target)
    catalog = StoreCatalog(expected_root=tmp_path)
    _register(catalog, "tasks", target, owner="TaskStore")
    _register(catalog, "audit", alias, owner="AuditStore")

    records = catalog.snapshot()
    assert records[0].dev_ino == records[1].dev_ino
    with pytest.raises(StoreCatalogError, match="same physical file"):
        catalog.validate()


def test_hardlinks_are_detected_as_same_inode(tmp_path: Path) -> None:
    target = tmp_path / "tasks.db"
    target.touch()
    alias = tmp_path / "audit.db"
    os.link(target, alias)
    catalog = StoreCatalog(expected_root=tmp_path)
    _register(catalog, "tasks", target, owner="TaskStore")
    _register(catalog, "audit", alias, owner="AuditStore")

    records = catalog.snapshot()
    assert records[0].resolved_path != records[1].resolved_path
    assert records[0].dev_ino == records[1].dev_ino
    with pytest.raises(StoreCatalogError, match="same physical file"):
        catalog.validate()


def test_reconcile_rejects_unregistered_hardlink_to_registered_store(tmp_path: Path) -> None:
    registered = tmp_path / "tasks.db"
    registered.touch()
    allowlisted_alias = tmp_path / "hub.db"
    os.link(registered, allowlisted_alias)
    catalog = StoreCatalog(expected_root=tmp_path)
    _register(catalog, "tasks", registered, owner="TaskStore")

    with pytest.raises(StoreCatalogError, match="unregistered path aliases registered store inode"):
        catalog.validate()


def test_reconcile_warns_for_unclaimed_file_without_failing(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    orphan = tmp_path / "orphan.db"
    orphan.touch()
    catalog = StoreCatalog(expected_root=tmp_path, silence_allowlist={})

    warnings = catalog.validate()

    assert len(warnings) == 1
    assert "unclaimed database file" in warnings[0]
    assert os.path.realpath(orphan) in warnings[0]
    assert warnings[0] in caplog.text


def test_reconcile_silences_allowlisted_unclaimed_file(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    nested = tmp_path / "component"
    nested.mkdir()
    (nested / "known-benign.db").touch()
    catalog = StoreCatalog(
        expected_root=tmp_path,
        silence_allowlist={"known-*.db": "fixture owned by another component"},
    )

    assert catalog.reconcile_filesystem() == []
    assert "STORE CATALOG WARNING" not in caplog.text


def test_reconcile_warns_for_dead_silence_allowlist_entry(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    catalog = StoreCatalog(
        expected_root=tmp_path,
        silence_allowlist={"missing-*.db": "obsolete fixture"},
    )

    warnings = catalog.reconcile_filesystem()

    assert len(warnings) == 1
    assert "dead silence-allowlist entry" in warnings[0]
    assert "missing-*.db" in warnings[0]
    assert "obsolete fixture" in warnings[0]
    assert warnings[0] in caplog.text


def test_reconcile_excludes_sidecars_and_temp_snapshot_directories(tmp_path: Path) -> None:
    registered = tmp_path / "tasks.db"
    registered.touch()
    for suffix in ("-wal", "-shm", "-journal"):
        (tmp_path / f"tasks.db{suffix}").touch()
    for directory_name in ("tmp", "snapshots", "pre-migration-snapshot"):
        ignored_dir = tmp_path / directory_name
        ignored_dir.mkdir()
        (ignored_dir / "ignored.db").touch()
    catalog = StoreCatalog(expected_root=tmp_path, silence_allowlist={})
    _register(catalog, "tasks", registered, owner="TaskStore")

    assert catalog.reconcile_filesystem() == []


def test_reconcile_warns_for_normal_orphan_reached_first_through_snapshot_symlink(
    tmp_path: Path,
) -> None:
    normal_dir = tmp_path / "zzz-data"
    normal_dir.mkdir()
    orphan = normal_dir / "orphan.db"
    orphan.touch()
    (tmp_path / "snapshots").symlink_to(normal_dir, target_is_directory=True)
    catalog = StoreCatalog(expected_root=tmp_path, silence_allowlist={})

    warnings = catalog.validate()

    assert len(warnings) == 1
    assert "path='zzz-data/orphan.db'" in warnings[0]
    assert os.path.realpath(orphan) in warnings[0]


def test_reconcile_quiets_snapshot_orphan_reached_first_through_normal_symlink(
    tmp_path: Path,
) -> None:
    snapshot_dir = tmp_path / "snapshots"
    snapshot_dir.mkdir()
    (snapshot_dir / "orphan.db").touch()
    (tmp_path / "aaa-link").symlink_to(snapshot_dir, target_is_directory=True)
    catalog = StoreCatalog(expected_root=tmp_path, silence_allowlist={})

    assert catalog.validate() == []


def test_reconcile_rejects_alias_behind_symlinked_directory(tmp_path: Path) -> None:
    expected_root = tmp_path / "data"
    expected_root.mkdir()
    registered = expected_root / "tasks.db"
    registered.touch()
    outside = tmp_path / "outside"
    outside.mkdir()
    os.link(registered, outside / "alias.db")
    (expected_root / "linked").symlink_to(outside, target_is_directory=True)
    catalog = StoreCatalog(expected_root=expected_root, silence_allowlist={})
    _register(catalog, "tasks", registered, owner="TaskStore")

    with pytest.raises(StoreCatalogError, match="directory symlink escapes expected root"):
        catalog.validate()


def test_reconcile_rejects_alias_in_snapshot_directory(tmp_path: Path) -> None:
    registered = tmp_path / "tasks.db"
    registered.touch()
    snapshot_dir = tmp_path / "snapshots"
    snapshot_dir.mkdir()
    os.link(registered, snapshot_dir / "alias.db")
    catalog = StoreCatalog(expected_root=tmp_path, silence_allowlist={})
    _register(catalog, "tasks", registered, owner="TaskStore")

    with pytest.raises(StoreCatalogError, match="unregistered path aliases registered store inode"):
        catalog.validate()


def test_reconcile_fails_closed_when_directory_cannot_be_inspected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registered = tmp_path / "tasks.db"
    registered.touch()
    unreadable = tmp_path / "unreadable"
    unreadable.mkdir()
    os.link(registered, unreadable / "alias.db")
    catalog = StoreCatalog(expected_root=tmp_path, silence_allowlist={})
    _register(catalog, "tasks", registered, owner="TaskStore")
    original_scandir = os.scandir

    def deny_unreadable(path: str | os.PathLike[str]) -> os.ScandirIterator[str]:
        if os.path.realpath(path) == os.path.realpath(unreadable):
            raise PermissionError("test directory is unreadable")
        return original_scandir(path)

    monkeypatch.setattr(os, "scandir", deny_unreadable)

    with pytest.raises(StoreCatalogError, match="could not inspect database directory"):
        catalog.validate()


def test_reconcile_symlink_cycle_terminates_cleanly(tmp_path: Path) -> None:
    registered = tmp_path / "tasks.db"
    registered.touch()
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "loop").symlink_to(tmp_path, target_is_directory=True)
    catalog = StoreCatalog(expected_root=tmp_path, silence_allowlist={})
    _register(catalog, "tasks", registered, owner="TaskStore")

    assert catalog.validate() == []


def test_reconcile_recognizes_nested_kb_store_as_claimed(tmp_path: Path) -> None:
    kb_dir = tmp_path / "kb"
    kb_dir.mkdir()
    kb_path = kb_dir / "kb.db"
    kb_path.touch()
    catalog = StoreCatalog(expected_root=tmp_path, silence_allowlist={})
    _register(catalog, "kb", kb_path, owner="KBStore")

    assert catalog.reconcile_filesystem() == []


def test_reconcile_does_not_flag_registered_store_file(tmp_path: Path) -> None:
    registered = tmp_path / "tasks.db"
    registered.touch()
    catalog = StoreCatalog(expected_root=tmp_path, silence_allowlist={})
    _register(catalog, "tasks", registered, owner="TaskStore")

    assert catalog.reconcile_filesystem() == []


def test_agent_comms_participates_in_alias_validation(tmp_path: Path) -> None:
    from pinky_daemon.agent_comms import AgentComms

    shared = tmp_path / "shared.db"
    catalog = StoreCatalog(expected_root=tmp_path)
    _register(catalog, "tasks", shared, owner="TaskStore")

    AgentComms(db_path=str(shared), catalog=catalog)

    record = next(record for record in catalog.snapshot() if record.logical_name == "agent_comms")
    assert record.owner == "AgentComms"
    assert record.criticality == "authoritative"
    with pytest.raises(StoreCatalogError, match="same physical file"):
        catalog.validate()


def test_plugin_manager_participates_in_alias_validation(tmp_path: Path) -> None:
    from pinky_daemon.plugin_manager import PluginManager

    shared = tmp_path / "shared.db"
    catalog = StoreCatalog(expected_root=tmp_path)
    _register(catalog, "tasks", shared, owner="TaskStore")

    PluginManager(db_path=str(shared), working_dir=str(tmp_path), catalog=catalog)

    record = next(record for record in catalog.snapshot() if record.logical_name == "plugins")
    assert record.owner == "PluginManager"
    assert record.criticality == "authoritative"
    with pytest.raises(StoreCatalogError, match="same physical file"):
        catalog.validate()


def test_plugin_context_registers_the_manager_store_identity(tmp_path: Path) -> None:
    from pinky_daemon.plugin_manager import PluginContext

    shared = tmp_path / "shared.db"
    catalog = StoreCatalog(expected_root=tmp_path)
    _register(catalog, "tasks", shared, owner="TaskStore")
    context = PluginContext("example", db_path=str(shared), catalog=catalog)

    _ = context.db

    record = next(record for record in catalog.snapshot() if record.logical_name == "plugins")
    assert record.owner == "PluginManager"
    with pytest.raises(StoreCatalogError, match="same physical file"):
        catalog.validate()
    context.close()


def test_librarian_runner_participates_in_alias_validation(tmp_path: Path) -> None:
    from pinky_daemon.kb_store import KBStore
    from pinky_daemon.librarian_runner import LibrarianRunner

    shared = tmp_path / "shared.db"
    catalog = StoreCatalog(expected_root=tmp_path)
    _register(catalog, "tasks", shared, owner="TaskStore")

    LibrarianRunner(
        kb_store=KBStore(data_dir=tmp_path / "kb-data"),
        db_path=shared,
        catalog=catalog,
    )

    record = next(
        record for record in catalog.snapshot() if record.logical_name == "librarian_state"
    )
    assert record.owner == "LibrarianRunner"
    assert record.criticality == "derived"
    with pytest.raises(StoreCatalogError, match="same physical file"):
        catalog.validate()


def test_create_api_registers_all_authoritative_stores_and_validates_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pinky_daemon.api import create_api

    validation_snapshots: list[list[StoreRecord]] = []
    original_validate = StoreCatalog.validate

    def validate_spy(catalog: StoreCatalog) -> None:
        validation_snapshots.append(catalog.snapshot())
        original_validate(catalog)

    monkeypatch.setattr(StoreCatalog, "validate", validate_spy)

    app = create_api(db_path=str(tmp_path / "conversations.db"))

    assert len(validation_snapshots) == 1
    assert app.state.store_catalog.snapshot() == validation_snapshots[0]
    records = validation_snapshots[0]
    assert {record.logical_name for record in records} == {
        "conversations",
        "agent_comms",
        "tasks",
        "triggers",
        "outreach_config",
        "user_profiles",
        "agents",
        "sessions",
        "session_events",
        "activity",
        "analytics",
        "mesh",
        "research",
        "presentations",
        "apps",
        "skills",
        "plugins",
        "voice",
        "audit",
        "message_context",
        "kb",
        "librarian_state",
        "dream_state",
    }
    assert len(records) == 23
    assert len({record.resolved_path for record in records}) == 22
    assert {record.journal_mode for record in records} == {"truncate", "wal"}
    assert (
        next(record for record in records if record.logical_name == "librarian_state").criticality
        == "derived"
    )
    session_records = [
        record for record in records if record.logical_name in {"sessions", "session_events"}
    ]
    assert len(session_records) == 2
    assert len({record.resolved_path for record in session_records}) == 1
    assert {record.journal_mode for record in session_records} == {"wal"}
    assert {record.owner for record in session_records} == {"SessionStore+SessionEventStore"}


def test_create_api_logs_and_reraises_catalog_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from pinky_daemon.api import create_api

    def fail_validation(_catalog: StoreCatalog) -> None:
        raise StoreCatalogError("incoherent test layout")

    monkeypatch.setattr(StoreCatalog, "validate", fail_validation)

    with pytest.raises(StoreCatalogError, match="incoherent test layout"):
        create_api(db_path=str(tmp_path / "conversations.db"))

    assert "ERROR startup: store catalog validation failed" in capsys.readouterr().err
