"""Lane A contracts for store criticality, connection policy, and shutdown."""

from __future__ import annotations

import os
import signal
import sqlite3
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

from pinky_daemon.store_authority import assert_no_open_store_descriptors
from pinky_daemon.store_catalog import StoreCatalog, StoreCatalogError, StoreIntegrityTarget
from pinky_daemon.store_manifest import derive_fleet_store_manifest
from pinky_daemon.store_snapshot import StoreSnapshotService

_EXPECTED_CRITICALITY = {
    "sessions": "delivery",
    "session_events": "telemetry",
    "conversations": "memory",
    "analytics": "telemetry",
    "agents": "delivery",
    "agent_signing_keys": "authority",
    "audit": "memory",
    "agent_comms": "delivery",
    "activity": "telemetry",
    "message_context": "delivery",
    "dream_state": "memory",
    "skills": "authority",
    "plugins": "authority",
    "outreach_config": "authority",
    "tasks": "memory",
    "research": "memory",
    "presentations": "memory",
    "apps": "memory",
    "triggers": "delivery",
    "mesh": "delivery",
    "kb": "memory",
    "librarian_state": "telemetry",
    "voice": "delivery",
    "user_profiles": "memory",
}

_EXPECTED_SNAPSHOT_LOGICAL_NAMES = frozenset(_EXPECTED_CRITICALITY) - {"librarian_state"}

_THIRTY_SECOND_BUSY_STORES = {
    "sessions",
    "session_events",
    "conversations",
    "audit",
    "agent_comms",
    "activity",
    "dream_state",
    "skills",
    "outreach_config",
    "tasks",
    "research",
    "presentations",
    "triggers",
    "voice",
    "user_profiles",
}


def _target(
    logical_name: str,
    path: Path,
    criticality: str,
    *,
    recovery: str = "snapshot",
    journal_mode: str = "wal",
) -> StoreIntegrityTarget:
    return StoreIntegrityTarget(
        logical_name=logical_name,
        path=os.fspath(path),
        criticality=criticality,
        recovery=recovery,
        journal_mode=journal_mode,
    )


def _register_manifest_member(
    catalog: StoreCatalog,
    target: StoreIntegrityTarget,
    *,
    owner: str | None = None,
) -> None:
    path = Path(target.path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(exist_ok=True)
    catalog.register(
        target.logical_name,
        path,
        journal_mode=target.journal_mode or "wal",
        owner=owner or f"{target.logical_name}-owner",
    )


def test_fleet_manifest_declares_exact_criticality_and_connection_policy(tmp_path: Path) -> None:
    manifest = derive_fleet_store_manifest(tmp_path / "conversations.db")

    assert {name: target.criticality for name, target in manifest.items()} == _EXPECTED_CRITICALITY
    assert {
        name
        for name, target in manifest.items()
        if target.connection_policy.busy_timeout_ms == 30_000
    } == _THIRTY_SECOND_BUSY_STORES
    assert {
        name
        for name, target in manifest.items()
        if target.connection_policy.busy_timeout_ms == 5_000
    } == set(manifest) - _THIRTY_SECOND_BUSY_STORES
    assert {target.connection_policy.rollback_retries for target in manifest.values()} == {6}
    assert {
        target.connection_policy.rollback_retry_delay_seconds for target in manifest.values()
    } == {0.2}


def test_post_construction_absent_telemetry_degrades_loud(tmp_path: Path) -> None:
    manifest = {
        "delivery": _target("delivery", tmp_path / "delivery.db", "delivery"),
        "telemetry": _target(
            "telemetry",
            tmp_path / "telemetry.db",
            "telemetry",
            recovery="rebuild",
        ),
    }
    catalog = StoreCatalog(expected_root=tmp_path, silence_allowlist={}, manifest=manifest)
    _register_manifest_member(catalog, manifest["delivery"])

    warnings = catalog.validate()

    assert len(warnings) == 1
    assert "telemetry" in warnings[0]
    assert "degraded" in warnings[0]


def test_post_construction_absent_delivery_refuses_boot(tmp_path: Path) -> None:
    manifest = {
        "delivery": _target("delivery", tmp_path / "delivery.db", "delivery"),
        "telemetry": _target(
            "telemetry",
            tmp_path / "telemetry.db",
            "telemetry",
            recovery="rebuild",
        ),
    }
    catalog = StoreCatalog(expected_root=tmp_path, silence_allowlist={}, manifest=manifest)
    _register_manifest_member(catalog, manifest["telemetry"])

    with pytest.raises(StoreCatalogError, match=r"delivery.*post-construction"):
        catalog.validate()


def test_missing_shared_path_member_inherits_the_cohorts_strongest_class(tmp_path: Path) -> None:
    shared = tmp_path / "sessions.db"
    manifest = {
        "sessions": _target("sessions", shared, "delivery"),
        "session_events": _target(
            "session_events",
            shared,
            "telemetry",
            recovery="snapshot",
        ),
    }
    catalog = StoreCatalog(expected_root=tmp_path, silence_allowlist={}, manifest=manifest)
    _register_manifest_member(catalog, manifest["sessions"], owner="shared-owner")

    with pytest.raises(StoreCatalogError, match=r"session_events.*delivery"):
        catalog.validate()


def test_p03_default_snapshot_logical_name_set_is_identical_after_reclassification(
    tmp_path: Path,
) -> None:
    manifest = derive_fleet_store_manifest(tmp_path / "conversations.db")
    assert {
        name for name, target in manifest.items() if target.recovery == "snapshot"
    } == _EXPECTED_SNAPSHOT_LOGICAL_NAMES
    catalog = StoreCatalog(expected_root=tmp_path, silence_allowlist={}, manifest=manifest)
    for target in manifest.values():
        _register_manifest_member(catalog, target)

    selected = StoreSnapshotService(catalog)._select(None)
    selected_logical_names = {
        logical_name for physical_store in selected for logical_name in physical_store.logical_names
    }

    assert selected_logical_names == _EXPECTED_SNAPSHOT_LOGICAL_NAMES


def test_catalog_shutdown_is_class_ordered_lifo_and_leaves_no_writer_fd(
    tmp_path: Path,
) -> None:
    declarations = [
        ("delivery_old", "delivery"),
        ("telemetry_old", "telemetry"),
        ("authority", "authority"),
        ("memory", "memory"),
        ("telemetry_new", "telemetry"),
        ("delivery_new", "delivery"),
    ]
    manifest = {
        name: _target(name, tmp_path / f"{name}.db", criticality)
        for name, criticality in declarations
    }
    catalog = StoreCatalog(expected_root=tmp_path, silence_allowlist={}, manifest=manifest)
    paths: list[Path] = []
    for name, _criticality in declarations:
        target = manifest[name]
        path = Path(target.path)
        paths.append(path)
        connection = catalog.open_connection(name, path, owner=f"{name}-owner")
        mode = str(connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]).lower()
        connection.execute("CREATE TABLE payload (value TEXT NOT NULL)")
        connection.execute("INSERT INTO payload(value) VALUES ('committed')")
        connection.commit()
        catalog.register(name, path, journal_mode=mode, owner=f"{name}-owner")

    report = catalog.shutdown(deadline_seconds=2.0)

    assert report.ok is True
    assert report.failures == ()
    assert report.finalized == (
        "telemetry_new",
        "telemetry_old",
        "memory",
        "authority",
        "delivery_new",
        "delivery_old",
    )
    assert_no_open_store_descriptors(paths)
    for path in paths:
        assert not Path(os.fspath(path) + "-wal").exists()
        assert not Path(os.fspath(path) + "-shm").exists()
        assert not Path(os.fspath(path) + "-journal").exists()


def test_shutdown_deadline_is_loud_and_remaining_stores_are_attempted() -> None:
    try:
        from pinky_daemon.store_shutdown import StoreShutdownCoordinator, StoreShutdownError
    except ImportError:
        pytest.fail("central store shutdown coordinator is not implemented")

    release = threading.Event()
    attempted: list[str] = []

    def blocked() -> None:
        attempted.append("blocked_telemetry")
        release.wait()

    def finalize(name: str):
        def _finalize() -> None:
            attempted.append(name)

        return _finalize

    coordinator = StoreShutdownCoordinator(deadline_seconds=0.18)
    coordinator.register("delivery", "delivery", finalize("delivery"))
    coordinator.register("memory", "memory", finalize("memory"))
    coordinator.register("blocked_telemetry", "telemetry", blocked)
    started = time.monotonic()

    with pytest.raises(StoreShutdownError) as exc_info:
        coordinator.shutdown()
    elapsed = time.monotonic() - started
    release.set()

    assert elapsed < 0.5
    assert attempted == ["blocked_telemetry", "memory", "delivery"]
    assert exc_info.value.report.attempted == (
        "blocked_telemetry",
        "memory",
        "delivery",
    )
    assert [failure.logical_name for failure in exc_info.value.report.failures] == [
        "blocked_telemetry"
    ]
    assert "deadline" in str(exc_info.value).lower()
    assert "blocked_telemetry" in str(exc_info.value)


_SIGNAL_CHILD = textwrap.dedent(
    """
    import os
    import signal
    import sys
    import time
    from pathlib import Path

    from pinky_daemon.store_catalog import StoreCatalog, StoreIntegrityTarget

    database = Path(sys.argv[1])
    graceful = sys.argv[2] == "graceful"
    target = StoreIntegrityTarget(
        logical_name="signal_store",
        path=os.fspath(database),
        criticality="delivery",
        recovery="snapshot",
        journal_mode="delete",
    )
    catalog = StoreCatalog(
        expected_root=database.parent,
        silence_allowlist={},
        manifest={"signal_store": target},
    )
    connection = catalog.open_connection(
        "signal_store",
        database,
        owner="signal-owner",
    )
    mode = str(connection.execute("PRAGMA journal_mode=DELETE").fetchone()[0]).lower()
    connection.execute("CREATE TABLE IF NOT EXISTS payload (value TEXT NOT NULL)")
    connection.commit()
    catalog.register(
        "signal_store",
        database,
        journal_mode=mode,
        owner="signal-owner",
    )
    connection.execute("BEGIN IMMEDIATE")
    connection.execute("INSERT INTO payload(value) VALUES ('inflight')")

    if graceful:
        def stop(_signum, _frame):
            catalog.shutdown(deadline_seconds=1.0)
            raise SystemExit(0)

        signal.signal(signal.SIGTERM, stop)

    print("READY", flush=True)
    while True:
        time.sleep(1)
    """
)


def _run_signal_child(path: Path, mode: str) -> int:
    process = subprocess.Popen(
        [sys.executable, "-c", _SIGNAL_CHILD, os.fspath(path), mode],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert process.stdout is not None
    ready = process.stdout.readline().strip()
    if ready != "READY":
        assert process.stderr is not None
        pytest.fail(f"signal child failed before readiness: {process.stderr.read()}")
    process.send_signal(signal.SIGTERM)
    return process.wait(timeout=5)


def test_graceful_shutdown_clears_recovery_work_while_raw_sigterm_retains_it(
    tmp_path: Path,
) -> None:
    graceful_path = tmp_path / "graceful.db"
    assert _run_signal_child(graceful_path, "graceful") == 0
    assert not Path(os.fspath(graceful_path) + "-journal").exists()
    with sqlite3.connect(graceful_path) as connection:
        assert connection.execute("PRAGMA quick_check").fetchall() == [("ok",)]

    raw_path = tmp_path / "raw.db"
    assert _run_signal_child(raw_path, "raw") == -signal.SIGTERM
    assert Path(os.fspath(raw_path) + "-journal").exists()
