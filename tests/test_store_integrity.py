"""P0.4 contracts for fail-closed boot-time SQLite integrity checks."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from pinky_daemon import api as api_module
from pinky_daemon import store_catalog as store_catalog_module
from pinky_daemon.store_catalog import StoreCatalog, StoreCatalogError


def _target(
    logical_name: str,
    path: str | Path,
    *,
    criticality: str = "authoritative",
    recovery: str = "snapshot",
) -> Any:
    target_type = getattr(store_catalog_module, "StoreIntegrityTarget", None)
    assert target_type is not None, "StoreIntegrityTarget is not implemented"
    return target_type(
        logical_name=logical_name,
        path=os.fspath(path),
        criticality=criticality,
        recovery=recovery,
    )


def _preflight(catalog: StoreCatalog, targets: list[Any]) -> None:
    preflight = getattr(catalog, "preflight_integrity", None)
    assert preflight is not None, "StoreCatalog.preflight_integrity is not implemented"
    preflight(targets)


def _manifest(db_path: Path) -> dict[str, Any]:
    derive = getattr(api_module, "_derive_api_store_manifest", None)
    assert derive is not None, "_derive_api_store_manifest is not implemented"
    manifest = derive(os.fspath(db_path))
    assert isinstance(manifest, dict)
    return manifest


def _create_database(path: Path, *, journal_mode: str = "delete") -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    observed = str(
        connection.execute(f"PRAGMA journal_mode={journal_mode}").fetchone()[0]
    ).lower()
    assert observed == journal_mode
    connection.execute("CREATE TABLE inventory (id INTEGER PRIMARY KEY, value TEXT NOT NULL)")
    connection.execute("INSERT INTO inventory(value) VALUES ('seed')")
    connection.commit()
    return connection


def _sidecar_identities(path: Path) -> dict[str, tuple[int, int]]:
    identities = {}
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = path.with_name(path.name + suffix)
        try:
            sidecar_stat = sidecar.stat()
        except FileNotFoundError:
            continue
        identities[suffix] = (sidecar_stat.st_dev, sidecar_stat.st_ino)
    return identities


class _RecordingConnection:
    def __init__(self, rows: list[tuple[str]]) -> None:
        self.rows = rows
        self.statements: list[str] = []
        self.closed = False

    def execute(self, statement: str) -> _RecordingConnection:
        self.statements.append(statement)
        return self

    def fetchall(self) -> list[tuple[str]]:
        return self.rows

    def close(self) -> None:
        self.closed = True


def test_corrupt_header_fails_closed_with_store_catalog_error(tmp_path: Path) -> None:
    source = tmp_path / "user_profiles.db"
    source.write_bytes(b"\x0d" + b"not-a-sqlite-header" + bytes(4076))
    catalog = StoreCatalog(expected_root=tmp_path, silence_allowlist={})

    with pytest.raises(StoreCatalogError) as exc_info:
        _preflight(catalog, [_target("user_profiles", source)])

    message = str(exc_info.value)
    assert "user_profiles" in message
    assert os.path.realpath(source) in message
    assert "file is not a database" in message.lower()
    assert "P0.3 snapshot" in message
    assert "before restarting" in message


def test_non_ok_quick_check_fails_closed_and_names_all_logical_stores(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "conversations_sessions.db"
    source.touch()
    connection = _RecordingConnection([("*** in database main ***",), ("Page 2 is never used",)])
    connect_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def connect(*args: Any, **kwargs: Any) -> _RecordingConnection:
        connect_calls.append((args, kwargs))
        return connection

    monkeypatch.setattr(sqlite3, "connect", connect)
    catalog = StoreCatalog(expected_root=tmp_path, silence_allowlist={})
    targets = [
        _target("sessions", source),
        _target("session_events", source),
    ]

    with pytest.raises(StoreCatalogError) as exc_info:
        _preflight(catalog, targets)

    message = str(exc_info.value)
    assert "sessions" in message
    assert "session_events" in message
    assert os.path.realpath(source) in message
    assert repr(connection.rows) in message
    assert connection.statements == ["PRAGMA quick_check"]
    assert connection.closed is True
    assert len(connect_calls) == 1
    assert connect_calls[0][0][0] == source.resolve().as_uri() + "?mode=ro"
    assert connect_calls[0][1]["uri"] is True


def test_clean_store_passes_quick_check_without_false_positive(tmp_path: Path) -> None:
    source = tmp_path / "tasks.db"
    connection = _create_database(source)
    connection.close()
    catalog = StoreCatalog(expected_root=tmp_path, silence_allowlist={})

    _preflight(catalog, [_target("tasks", source)])


def test_absent_store_is_skipped_without_opening(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "not-created.db"

    def unexpected_connect(*_args: Any, **_kwargs: Any) -> None:
        pytest.fail("an absent store must not be opened")

    monkeypatch.setattr(sqlite3, "connect", unexpected_connect)
    catalog = StoreCatalog(expected_root=tmp_path, silence_allowlist={})

    _preflight(catalog, [_target("not_created", source)])


def test_memory_store_is_skipped_without_opening(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_connect(*_args: Any, **_kwargs: Any) -> None:
        pytest.fail("an in-memory store must not be opened by the file preflight")

    monkeypatch.setattr(sqlite3, "connect", unexpected_connect)
    catalog = StoreCatalog(expected_root=tmp_path, silence_allowlist={})

    _preflight(catalog, [_target("memory", ":memory:")])


@pytest.mark.parametrize(
    ("requested_mode", "expected_reopen_mode"),
    [("delete", "delete"), ("truncate", "delete"), ("wal", "wal")],
)
def test_preflight_preserves_sidecar_inodes_and_persisted_journal_mode(
    tmp_path: Path,
    requested_mode: str,
    expected_reopen_mode: str,
) -> None:
    source = tmp_path / f"{requested_mode}.db"
    authority = _create_database(source, journal_mode=requested_mode)
    try:
        if requested_mode == "wal":
            authority.execute("UPDATE inventory SET value = 'wal-live' WHERE id = 1")
            authority.commit()
        else:
            authority.execute("BEGIN IMMEDIATE")
            authority.execute("UPDATE inventory SET value = 'rollback-live' WHERE id = 1")
        mode_before = str(authority.execute("PRAGMA journal_mode").fetchone()[0]).lower()
        sidecars_before = _sidecar_identities(source)
        if requested_mode == "wal":
            assert {"-wal", "-shm"}.issubset(sidecars_before)
        else:
            assert "-journal" in sidecars_before

        catalog = StoreCatalog(expected_root=tmp_path, silence_allowlist={})
        _preflight(catalog, [_target("primary", source)])

        mode_after_live = str(authority.execute("PRAGMA journal_mode").fetchone()[0]).lower()
        sidecars_after = _sidecar_identities(source)
    finally:
        authority.rollback()
        authority.close()

    persisted = sqlite3.connect(source)
    try:
        mode_after_reopen = str(persisted.execute("PRAGMA journal_mode").fetchone()[0]).lower()
    finally:
        persisted.close()

    assert mode_before == requested_mode
    assert mode_after_live == mode_before
    # SQLite persists only WAL-vs-rollback in the header; TRUNCATE therefore
    # reopens as DELETE. The probe must preserve that WAL/rollback boundary.
    assert mode_after_reopen == expected_reopen_mode
    assert sidecars_after == sidecars_before


@pytest.mark.parametrize(
    "open_error",
    [
        sqlite3.DatabaseError("file is not a database"),
        OSError("simulated read failure"),
    ],
    ids=["sqlite", "os"],
)
def test_open_error_is_actionable_and_preserves_cause(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    open_error: Exception,
) -> None:
    source = tmp_path / "audit.db"
    source.touch()

    def fail_open(*_args: Any, **_kwargs: Any) -> None:
        raise open_error

    monkeypatch.setattr(sqlite3, "connect", fail_open)
    catalog = StoreCatalog(expected_root=tmp_path, silence_allowlist={})

    with pytest.raises(StoreCatalogError) as exc_info:
        _preflight(catalog, [_target("audit", source)])

    message = str(exc_info.value)
    assert "audit" in message
    assert os.path.realpath(source) in message
    assert f"{type(open_error).__name__}: {open_error}" in message
    assert "restore" in message.lower()
    assert "P0.3 snapshot" in message
    assert "before restarting" in message
    assert exc_info.value.__cause__ is open_error


def test_quick_check_error_closes_connection_and_preserves_cause(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "research.db"
    source.touch()
    quick_check_error = sqlite3.DatabaseError("database disk image is malformed")

    class FailingQuickCheckConnection(_RecordingConnection):
        def execute(self, statement: str) -> _RecordingConnection:
            self.statements.append(statement)
            raise quick_check_error

    connection = FailingQuickCheckConnection([])
    monkeypatch.setattr(sqlite3, "connect", lambda *_args, **_kwargs: connection)
    catalog = StoreCatalog(expected_root=tmp_path, silence_allowlist={})

    with pytest.raises(StoreCatalogError) as exc_info:
        _preflight(catalog, [_target("research", source)])

    assert connection.statements == ["PRAGMA quick_check"]
    assert connection.closed is True
    assert "database disk image is malformed" in str(exc_info.value)
    assert exc_info.value.__cause__ is quick_check_error


def test_programming_defect_propagates_from_preflight(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "apps.db"
    source.touch()

    def programming_bug(*_args: Any, **_kwargs: Any) -> None:
        raise TypeError("programming bug")

    monkeypatch.setattr(sqlite3, "connect", programming_bug)
    catalog = StoreCatalog(expected_root=tmp_path, silence_allowlist={})

    with pytest.raises(TypeError, match="programming bug"):
        _preflight(catalog, [_target("apps", source)])


def test_manifest_preserves_every_legacy_inline_path_byte_for_byte(tmp_path: Path) -> None:
    raw_base = tmp_path / "parent.db.directory" / "fleet.db"
    base = os.path.realpath(raw_base)
    manifest = _manifest(raw_base)
    data_dir = Path(base).parent
    expected = {
        "sessions": base.replace(".db", "_sessions.db"),
        "session_events": base.replace(".db", "_sessions.db"),
        "conversations": base,
        "analytics": base.replace(".db", "_analytics.db"),
        "agents": base.replace(".db", "_agents.db"),
        "audit": base.replace(".db", "_audit.db"),
        "agent_comms": base.replace(".db", "_agent_comms.db"),
        "activity": base.replace(".db", "_activity.db"),
        "message_context": base.replace(".db", "_message_context.db"),
        "dream_state": base.replace(".db", "_dream_state.db"),
        "skills": base.replace(".db", "_skills.db"),
        "plugins": base.replace(".db", "_plugins.db"),
        "outreach_config": base.replace(".db", "_outreach.db"),
        "tasks": base.replace(".db", "_tasks.db"),
        "research": base.replace(".db", "_research.db"),
        "presentations": base.replace(".db", "_presentations.db"),
        "apps": base.replace(".db", "_apps.db"),
        "triggers": base.replace(".db", "_triggers.db"),
        "mesh": base.replace(".db", "_mesh.db"),
        "kb": os.fspath(data_dir / "kb" / "kb.db"),
        "librarian_state": base.replace(".db", "_librarian_state.db"),
        "voice": os.fspath(data_dir / "voice_calls.db"),
        "user_profiles": os.fspath(data_dir / "user_profiles.db"),
    }

    assert set(manifest) == set(expected)
    assert {name: target.path for name, target in manifest.items()} == expected


def test_manifest_matches_every_boot_opened_catalog_record(tmp_path: Path) -> None:
    base = tmp_path / "conversations.db"
    manifest = _manifest(base)

    app = api_module.create_api(db_path=os.fspath(base))

    records = app.state.store_catalog.snapshot()
    catalog_grouping = {
        (record.logical_name, record.resolved_path, record.criticality) for record in records
    }
    manifest_grouping = {
        (target.logical_name, os.path.realpath(target.path), target.criticality)
        for target in manifest.values()
    }
    assert manifest_grouping == catalog_grouping
    authoritative_manifest = {
        (logical_name, path)
        for logical_name, path, criticality in manifest_grouping
        if criticality == "authoritative"
    }
    authoritative_catalog = {
        (logical_name, path)
        for logical_name, path, criticality in catalog_grouping
        if criticality == "authoritative"
    }
    assert authoritative_manifest == authoritative_catalog


def test_clean_preexisting_store_set_boots_normally(tmp_path: Path) -> None:
    base = tmp_path / "conversations.db"
    manifest = _manifest(base)
    seeded_paths: set[str] = set()
    for target in manifest.values():
        resolved_path = os.path.realpath(target.path)
        if resolved_path in seeded_paths:
            continue
        seeded_paths.add(resolved_path)
        connection = _create_database(Path(resolved_path))
        connection.close()

    app = api_module.create_api(db_path=os.fspath(base))

    assert app.state.store_catalog is not None


def test_create_api_preflights_corruption_before_any_store_constructor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "conversations.db"
    source.write_bytes(b"\x0d" + bytes(4095))

    def unexpected_store_construction(*_args: Any, **_kwargs: Any) -> None:
        pytest.fail("store construction began before the integrity preflight")

    monkeypatch.setattr(api_module, "SessionStore", unexpected_store_construction)

    with pytest.raises(StoreCatalogError) as exc_info:
        api_module.create_api(db_path=os.fspath(source))

    message = str(exc_info.value)
    assert "conversations" in message
    assert os.path.realpath(source) in message
    assert "file is not a database" in message.lower()
    assert "P0.3 snapshot" in message


def test_corrupt_boot_opened_derived_store_fails_with_rebuild_guidance(
    tmp_path: Path,
) -> None:
    base = tmp_path / "conversations.db"
    derived = Path(os.path.realpath(base).replace(".db", "_librarian_state.db"))
    derived.write_bytes(b"\x0d" + bytes(4095))

    with pytest.raises(StoreCatalogError) as exc_info:
        api_module.create_api(db_path=os.fspath(base))

    message = str(exc_info.value)
    assert "librarian_state" in message
    assert os.path.realpath(derived) in message
    assert "file is not a database" in message.lower()
    assert "delete" in message.lower()
    assert "rebuild" in message.lower() or "regenerate" in message.lower()
    assert "before restarting" in message
    assert "P0.3 snapshot" not in message


def test_unclaimed_corrupt_database_is_not_preflighted(tmp_path: Path) -> None:
    base = tmp_path / "conversations.db"
    unclaimed = tmp_path / "unclaimed.db"
    unclaimed.write_bytes(b"\x0d" + bytes(4095))

    app = api_module.create_api(db_path=os.fspath(base))

    assert app.state.store_catalog is not None
