"""P0.3 contracts for daemon-owned, catalog-selected SQLite snapshots."""

from __future__ import annotations

import ast
import importlib
import json
import os
import sqlite3
import threading
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
from fastapi.testclient import TestClient

from pinky_daemon.api import create_api
from pinky_daemon.auth import build_internal_auth_headers
from pinky_daemon.store_catalog import StoreCatalog


def _snapshot_module():
    return importlib.import_module("pinky_daemon.store_snapshot")


def _create_store(
    path: Path,
    *,
    journal_mode: str = "delete",
    rows: int = 3,
) -> tuple[sqlite3.Connection, str]:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    observed_mode = str(
        connection.execute(f"PRAGMA journal_mode={journal_mode}").fetchone()[0]
    ).lower()
    connection.execute("CREATE TABLE inventory (id INTEGER PRIMARY KEY, value TEXT NOT NULL)")
    connection.executemany(
        "INSERT INTO inventory(value) VALUES (?)",
        [(f"row-{index}",) for index in range(rows)],
    )
    connection.commit()
    return connection, observed_mode


def _catalog_for(
    root: Path,
    path: Path,
    *,
    logical_name: str = "primary",
    journal_mode: str = "delete",
    criticality: str = "authoritative",
) -> StoreCatalog:
    catalog = StoreCatalog(expected_root=root, silence_allowlist={})
    catalog.register(
        logical_name,
        path,
        journal_mode=journal_mode,
        owner="test-owner",
        criticality=criticality,
    )
    return catalog


def _live_sidecar_inodes(path: Path) -> dict[str, tuple[int, int]]:
    identities = {}
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = path.with_name(path.name + suffix)
        try:
            sidecar_stat = sidecar.stat()
        except FileNotFoundError:
            continue
        identities[suffix] = (sidecar_stat.st_dev, sidecar_stat.st_ino)
    return identities


def test_consistent_copy_reproduces_inventory_and_passes_quick_check(tmp_path: Path) -> None:
    snapshot = _snapshot_module()
    source = tmp_path / "primary.db"
    connection, mode = _create_store(source, rows=7)
    connection.close()
    catalog = _catalog_for(tmp_path, source, journal_mode=mode)

    [result] = snapshot.StoreSnapshotService(catalog).create_snapshots("primary")

    assert result.verification == "ok"
    assert result.error is None
    assert result.snapshot_path is not None
    copy = sqlite3.connect(result.snapshot_path)
    try:
        assert copy.execute("PRAGMA quick_check").fetchall() == [("ok",)]
        assert copy.execute("SELECT id, value FROM inventory ORDER BY id").fetchall() == [
            (index + 1, f"row-{index}") for index in range(7)
        ]
    finally:
        copy.close()


@pytest.mark.parametrize("requested_mode", ["delete", "wal"])
def test_snapshot_preserves_live_sidecar_set_inodes_and_journal_mode(
    tmp_path: Path,
    requested_mode: str,
) -> None:
    """Keep sidecar existence/inodes stable; WAL read-mark bytes may change in place."""
    snapshot = _snapshot_module()
    source = tmp_path / f"{requested_mode}.db"
    authority_connection, observed_mode = _create_store(
        source,
        journal_mode=requested_mode,
    )
    catalog = _catalog_for(tmp_path, source, journal_mode=observed_mode)
    try:
        mode_before = str(authority_connection.execute("PRAGMA journal_mode").fetchone()[0]).lower()
        sidecars_before = _live_sidecar_inodes(source)
        if observed_mode == "wal":
            assert {"-wal", "-shm"}.issubset(sidecars_before)
        else:
            assert "-shm" not in sidecars_before

        [result] = snapshot.StoreSnapshotService(catalog).create_snapshots("primary")

        mode_after = str(authority_connection.execute("PRAGMA journal_mode").fetchone()[0]).lower()
        sidecars_after = _live_sidecar_inodes(source)
    finally:
        authority_connection.close()

    assert result.verification == "ok"
    assert set(sidecars_after) == set(sidecars_before)
    assert sidecars_after == sidecars_before
    assert mode_before == observed_mode
    assert mode_after == observed_mode
    # Deliberately do not compare sidecar bytes or mtimes. A WAL reader's
    # read-mark bookkeeping may legitimately update -shm bytes in place; inode
    # stability is what proves there was no unlink-and-recreate cycle.


@pytest.mark.parametrize("requested_mode", ["delete", "truncate", "wal"])
def test_snapshot_never_mutates_source_journal_mode(
    tmp_path: Path,
    requested_mode: str,
) -> None:
    snapshot = _snapshot_module()
    source = tmp_path / f"{requested_mode}.db"
    authority_connection, observed_mode = _create_store(
        source,
        journal_mode=requested_mode,
    )
    catalog = _catalog_for(tmp_path, source, journal_mode=observed_mode)

    snapshot.StoreSnapshotService(catalog).create_snapshots("primary")

    try:
        mode_after = str(authority_connection.execute("PRAGMA journal_mode").fetchone()[0]).lower()
    finally:
        authority_connection.close()
    assert mode_after == observed_mode


def test_snapshot_is_a_distinct_inode_not_a_hardlink(tmp_path: Path) -> None:
    snapshot = _snapshot_module()
    source = tmp_path / "primary.db"
    connection, mode = _create_store(source)
    connection.close()
    catalog = _catalog_for(tmp_path, source, journal_mode=mode)

    [result] = snapshot.StoreSnapshotService(catalog).create_snapshots("primary")

    source_stat = source.stat()
    snapshot_stat = Path(result.snapshot_path).stat()
    assert (snapshot_stat.st_dev, snapshot_stat.st_ino) != (
        source_stat.st_dev,
        source_stat.st_ino,
    )


def test_snapshot_directory_stays_clean_under_catalog_reconciliation(tmp_path: Path) -> None:
    snapshot = _snapshot_module()
    source = tmp_path / "primary.db"
    connection, mode = _create_store(source)
    connection.close()
    catalog = _catalog_for(tmp_path, source, journal_mode=mode)

    snapshot.StoreSnapshotService(catalog).create_snapshots("primary")

    assert catalog.reconcile_filesystem() == []


def test_explicit_selection_returns_all_logical_names_for_shared_physical_store(
    tmp_path: Path,
) -> None:
    snapshot = _snapshot_module()
    source = tmp_path / "shared.db"
    connection, mode = _create_store(source)
    connection.close()
    catalog = _catalog_for(
        tmp_path,
        source,
        logical_name="sessions",
        journal_mode=mode,
    )
    catalog.register(
        "session_events",
        source,
        journal_mode=mode,
        owner="test-owner",
    )

    [result] = snapshot.StoreSnapshotService(catalog).create_snapshots("sessions")

    assert result.logical_names == ("sessions", "session_events")


def test_path_retargeted_after_selection_is_rejected_before_sqlite_open(
    tmp_path: Path,
) -> None:
    snapshot = _snapshot_module()
    root = tmp_path / "data"
    source = root / "primary.db"
    source_connection, mode = _create_store(source)
    source_connection.close()
    outside = tmp_path / "outside.db"
    outside_connection, _outside_mode = _create_store(outside)
    outside_connection.close()
    catalog = _catalog_for(root, source, journal_mode=mode)

    class RetargetingSnapshotService(snapshot.StoreSnapshotService):
        def _backup_and_verify(self, source_path: str, destination_path: Path, *args) -> None:
            Path(source_path).unlink()
            Path(source_path).symlink_to(outside)
            return super()._backup_and_verify(source_path, destination_path, *args)

    service = RetargetingSnapshotService(catalog)

    with pytest.raises(snapshot.StoreSnapshotSelectionError, match="identity"):
        service.create_snapshots("primary")


def test_source_and_destination_connections_close_when_verification_fails(
    tmp_path: Path,
) -> None:
    snapshot = _snapshot_module()
    source_path = tmp_path / "source.db"
    source_path.touch()
    source_stat = source_path.stat()
    identity = (source_stat.st_dev, source_stat.st_ino)

    class SourceConnection:
        closed = False

        def backup(self, _destination, **_kwargs) -> None:
            return None

        def close(self) -> None:
            self.closed = True

    class DestinationConnection:
        closed = False

        def execute(self, statement: str):
            assert statement == "PRAGMA quick_check"
            return self

        def fetchall(self) -> list[tuple[str]]:
            return [("corrupt",)]

        def close(self) -> None:
            self.closed = True

    source = SourceConnection()
    destination = DestinationConnection()
    service = snapshot.StoreSnapshotService(StoreCatalog(expected_root=tmp_path))

    with (
        patch.object(snapshot.sqlite3, "connect", side_effect=[source, destination]),
        pytest.raises(snapshot.StoreSnapshotVerificationError, match="quick_check"),
    ):
        service._backup_and_verify(source_path.as_posix(), tmp_path / "copy.tmp", identity)

    assert source.closed is True
    assert destination.closed is True


def test_snapshot_is_consistent_when_source_commit_lands_mid_backup(tmp_path: Path) -> None:
    snapshot = _snapshot_module()
    source = tmp_path / "busy.db"
    authority = sqlite3.connect(source)
    mode = str(authority.execute("PRAGMA journal_mode=WAL").fetchone()[0]).lower()
    authority.executescript(
        """
        CREATE TABLE left_rows (id INTEGER PRIMARY KEY, payload BLOB NOT NULL);
        CREATE TABLE right_rows (id INTEGER PRIMARY KEY, payload BLOB NOT NULL);
        """
    )
    payload = b"x" * 4096
    authority.executemany(
        "INSERT INTO left_rows(id, payload) VALUES (?, ?)",
        [(index, payload) for index in range(1, 1501)],
    )
    authority.executemany(
        "INSERT INTO right_rows(id, payload) VALUES (?, ?)",
        [(index, payload) for index in range(1, 1501)],
    )
    authority.commit()
    catalog = _catalog_for(tmp_path, source, journal_mode=mode)
    backup_started = threading.Event()
    writer_committed = threading.Event()
    writer_errors: list[BaseException] = []

    class CoordinatedSnapshotService(snapshot.StoreSnapshotService):
        def _backup_progress(self, _status: int, remaining: int, _total: int) -> None:
            if remaining and not backup_started.is_set():
                backup_started.set()
                assert writer_committed.wait(10), "writer did not commit during backup"

    def _write_during_backup() -> None:
        try:
            assert backup_started.wait(10), "backup never reached a partial page step"
            writer = sqlite3.connect(source, timeout=10)
            try:
                writer.execute("BEGIN IMMEDIATE")
                writer.execute(
                    "INSERT INTO left_rows(id, payload) VALUES (?, ?)",
                    (1501, payload),
                )
                writer.execute(
                    "INSERT INTO right_rows(id, payload) VALUES (?, ?)",
                    (1501, payload),
                )
                writer.commit()
            finally:
                writer.close()
        except BaseException as exc:  # noqa: BLE001 - re-raised in the test thread
            writer_errors.append(exc)
        finally:
            writer_committed.set()

    writer_thread = threading.Thread(target=_write_during_backup)
    writer_thread.start()
    try:
        [result] = CoordinatedSnapshotService(catalog).create_snapshots("primary")
    finally:
        writer_thread.join(timeout=15)
        authority.close()

    assert not writer_thread.is_alive()
    assert writer_errors == []
    copy = sqlite3.connect(result.snapshot_path)
    try:
        assert copy.execute("PRAGMA quick_check").fetchall() == [("ok",)]
        left_ids = copy.execute("SELECT id FROM left_rows ORDER BY id").fetchall()
        right_ids = copy.execute("SELECT id FROM right_rows ORDER BY id").fetchall()
    finally:
        copy.close()
    assert left_ids == right_ids
    assert len(left_ids) in {1500, 1501}


def test_selection_fails_closed_before_opening_unknown_or_outside_store(
    tmp_path: Path,
) -> None:
    snapshot = _snapshot_module()
    root = tmp_path / "data"
    root.mkdir()
    registered = root / "registered.db"
    connection, mode = _create_store(registered)
    connection.close()
    outside = tmp_path / "outside.db"
    outside_connection, outside_mode = _create_store(outside)
    outside_connection.close()
    catalog = _catalog_for(root, registered, journal_mode=mode)
    catalog.register(
        "outside",
        outside,
        journal_mode=outside_mode,
        owner="test-owner",
    )
    service = snapshot.StoreSnapshotService(catalog)

    with patch.object(snapshot.sqlite3, "connect") as connect:
        with pytest.raises(snapshot.StoreSnapshotSelectionError, match="unregistered"):
            service.create_snapshots("unknown")
        with pytest.raises(snapshot.StoreSnapshotSelectionError, match="expected root"):
            service.create_snapshots("outside")

    connect.assert_not_called()


def _snapshot_api_client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> TestClient:
    monkeypatch.setenv("PINKY_SESSION_SECRET", "snapshot-test-secret")
    monkeypatch.delenv("PINKY_UI_PASSWORD", raising=False)
    data_dir = tmp_path / "api-data"
    app = create_api(
        max_sessions=10,
        default_working_dir=str(tmp_path),
        db_path=str(data_dir / "conversations.db"),
    )
    app.state.agents.register(
        "operator",
        model="opus",
        role="operator",
        working_dir=str(tmp_path / "operator"),
    )
    app.state.agents.register(
        "tenant",
        model="opus",
        isolated=True,
        working_dir=str(tmp_path / "tenant"),
    )
    return TestClient(app)


def _signed_snapshot_headers(client: TestClient, agent_name: str) -> dict[str, str]:
    key = client.app.state.agents.get_signing_key(agent_name)
    return build_internal_auth_headers(
        key,
        agent_name=agent_name,
        method="POST",
        path="/internal/stores/snapshot",
    )


def test_snapshot_endpoint_denies_unauthenticated_session_only_and_isolated_callers(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    client = _snapshot_api_client(monkeypatch, tmp_path)
    worker = Mock()
    client.app.state.store_snapshot_service.create_snapshots = worker

    unauthenticated = client.post("/internal/stores/snapshot", json={})
    assert unauthenticated.status_code == 401

    client.post("/auth/setup", json={"password": "hunter22", "next": "/"})
    session_only = client.post("/internal/stores/snapshot", json={})
    assert session_only.status_code == 403

    isolated = client.post(
        "/internal/stores/snapshot",
        headers=_signed_snapshot_headers(client, "tenant"),
        json={},
    )
    assert isolated.status_code == 403
    worker.assert_not_called()


def test_successful_snapshot_request_is_audited_with_caller_selection_and_paths(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    client = _snapshot_api_client(monkeypatch, tmp_path)
    response = client.post(
        "/internal/stores/snapshot",
        headers=_signed_snapshot_headers(client, "operator"),
        json={"logical_name": "conversations"},
    )

    assert response.status_code == 200
    paths = [item["snapshot_path"] for item in response.json()["snapshots"]]
    entries = client.app.state.audit.get_log(event="store_snapshot", limit=5)
    assert len(entries) == 1
    entry = entries[0]
    assert entry.agent_name == "operator"
    assert entry.timestamp > 0
    summary = json.loads(entry.tool_input_summary)
    assert summary["logical_name"] == "conversations"
    assert summary["snapshot_paths"] == paths


def test_missing_registered_store_is_reported_without_sinking_batch(tmp_path: Path) -> None:
    snapshot = _snapshot_module()
    source = tmp_path / "present.db"
    connection, mode = _create_store(source)
    connection.close()
    catalog = _catalog_for(
        tmp_path,
        source,
        logical_name="present",
        journal_mode=mode,
    )
    catalog.register(
        "missing",
        tmp_path / "missing.db",
        journal_mode="delete",
        owner="test-owner",
    )

    results = snapshot.StoreSnapshotService(catalog).create_snapshots()

    by_name = {result.logical_names: result for result in results}
    assert by_name[("present",)].verification == "ok"
    assert Path(by_name[("present",)].snapshot_path).is_file()
    missing = by_name[("missing",)]
    assert missing.snapshot_path is None
    assert missing.verification == "skipped"
    assert missing.error == "source_missing"


def test_snapshot_endpoint_rate_limits_before_third_request_does_work(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    snapshot = _snapshot_module()
    client = _snapshot_api_client(monkeypatch, tmp_path)
    result = snapshot.SnapshotResult(
        logical_names=("conversations",),
        snapshot_path=str(tmp_path / "copy.db"),
        verification="ok",
        error=None,
    )
    worker = Mock(return_value=[result])
    client.app.state.store_snapshot_service.create_snapshots = worker
    headers = _signed_snapshot_headers(client, "operator")

    first = client.post("/internal/stores/snapshot", headers=headers, json={})
    second = client.post("/internal/stores/snapshot", headers=headers, json={})
    third = client.post("/internal/stores/snapshot", headers=headers, json={})

    assert first.status_code == 200
    assert second.status_code == 200
    assert third.status_code == 429
    assert int(third.headers["Retry-After"]) >= 1
    assert worker.call_count == 2


def test_cli_is_endpoint_only_and_requires_explicit_signing_identity() -> None:
    script = Path(__file__).resolve().parents[1] / "scripts" / "store_snapshot.py"
    source = script.read_text(encoding="utf-8")
    tree = ast.parse(source)

    imported_modules = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imported_from = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    assert "sqlite3" not in imported_modules
    assert "sqlite3" not in imported_from
    assert "/internal/stores/snapshot" in source
    assert "--as-agent" in source
    assert "PINKY_AGENT_NAME" in source
    assert source.index("PINKY_AGENT_KEY") < source.index("PINKY_SESSION_SECRET")
    assert not os.path.exists(script.with_name("safe_db_read.py"))


def test_cli_signs_endpoint_request_and_prints_only_returned_copy_path(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    script = Path(__file__).resolve().parents[1] / "scripts" / "store_snapshot.py"
    spec = importlib.util.spec_from_file_location("store_snapshot_cli", script)
    assert spec is not None and spec.loader is not None
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    captured_requests = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def read(self) -> bytes:
            return json.dumps(
                {
                    "snapshots": [
                        {
                            "logical_names": ["conversations"],
                            "snapshot_path": "/data/snapshots/conversations-copy.db",
                            "verification": "ok",
                            "error": None,
                        }
                    ]
                }
            ).encode()

    def _urlopen(request, *, timeout: float):
        captured_requests.append((request, timeout))
        return Response()

    monkeypatch.setenv("PINKY_AGENT_KEY", "agent-specific-secret")
    monkeypatch.setenv("PINKY_SESSION_SECRET", "global-secret-must-not-win")
    monkeypatch.setattr(cli.urllib.request, "urlopen", _urlopen)

    assert (
        cli.main(
            [
                "--as-agent",
                "operator",
                "--logical-name",
                "conversations",
                "--api-url",
                "http://127.0.0.1:9999",
            ]
        )
        == 0
    )

    output = capsys.readouterr()
    assert output.out == "/data/snapshots/conversations-copy.db\n"
    assert output.err == ""
    [(request, timeout)] = captured_requests
    assert request.full_url == "http://127.0.0.1:9999/internal/stores/snapshot"
    assert request.get_method() == "POST"
    assert json.loads(request.data) == {"logical_name": "conversations"}
    assert request.headers["X-pinky-agent"] == "operator"
    assert request.headers["X-pinky-signature"]
    assert timeout == 300.0
