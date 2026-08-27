"""P0.5 contracts for bounded storage-authority observability."""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from pinky_daemon import api as api_module
from pinky_daemon.auth import build_internal_auth_headers
from pinky_daemon.store_catalog import (
    DEFAULT_FILESYSTEM_SILENCE_ALLOWLIST,
    StoreCatalog,
    StoreCatalogError,
)


def _manifest_names(base: Path) -> set[str]:
    return set(api_module._derive_api_store_manifest(base))


def _register_operator(app, tmp_path: Path) -> str:
    app.state.agents.register(
        "storage-operator",
        model="opus",
        role="operator",
        working_dir=str(tmp_path / "operator"),
    )
    return app.state.agents.get_signing_key("storage-operator")


def _signed_headers(key: str, *, method: str, path: str) -> dict[str, str]:
    return build_internal_auth_headers(
        key,
        agent_name="storage-operator",
        method=method,
        path=path,
    )


def _storage_status(client: TestClient, key: str) -> dict:
    response = client.get(
        "/admin/watchdog",
        headers=_signed_headers(key, method="GET", path="/admin/watchdog"),
    )
    assert response.status_code == 200
    body = response.json()
    assert "storage" in body, "/admin/watchdog storage metrics are not implemented"
    return body["storage"]


def _storage_event_lines(
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> list[str]:
    # The daemon's established log channel includes both stdlib logging and
    # stderr `_log` calls. Accept either transport, but require one bounded
    # storage-event schema so failed boot attempts remain machine-readable.
    messages = [record.getMessage() for record in caplog.records]
    captured = capsys.readouterr()
    messages.extend(captured.err.splitlines())
    messages.extend(captured.out.splitlines())
    return [message for message in messages if message.startswith("storage_event ")]


def _assert_no_sensitive_metric_values(
    storage: dict,
    *,
    data_root: Path,
    secret: str,
) -> None:
    encoded = json.dumps(storage, sort_keys=True)
    assert os.fspath(data_root) not in encoded
    assert os.path.realpath(data_root) not in encoded
    assert secret not in encoded
    assert "storage-operator" not in encoded

    def walk(value: object) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                lowered = str(key).lower()
                assert lowered not in {"path", "resolved_path", "snapshot_path", "error"}
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(storage)


def test_admin_watchdog_exposes_bounded_boot_preflight_inventory_and_reconcile_metrics(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    secret = "p0.5-metrics-secret"
    monkeypatch.setenv("PINKY_SESSION_SECRET", secret)
    data_root = tmp_path / "data"
    base = data_root / "conversations.db"
    manifest_names = _manifest_names(base)
    assert len(manifest_names) == 24

    app = api_module.create_api(
        max_sessions=10,
        default_working_dir=str(tmp_path),
        db_path=os.fspath(base),
    )
    key = _register_operator(app, tmp_path)

    client = TestClient(app)
    try:
        storage = _storage_status(client, key)
    finally:
        client.close()

    assert set(storage) == {
        "boot_gate",
        "preflight",
        "inventory",
        "reconcile_warning_count",
        "snapshots",
        "runtime",
        "corruption",
    }
    assert storage["boot_gate"]["outcome"] == "warn"
    expected_warning_count = len(DEFAULT_FILESYSTEM_SILENCE_ALLOWLIST)
    assert storage["boot_gate"]["warning_count"] == expected_warning_count
    assert storage["boot_gate"]["timestamp"]
    assert storage["reconcile_warning_count"] == expected_warning_count

    # The preflight runs before constructors: a brand-new data root is absent,
    # then constructors create all 24 logical / 22 physical stores.
    assert storage["preflight"] == {
        logical_name: "skipped-absent" for logical_name in manifest_names
    }
    inventory = storage["inventory"]
    assert inventory["logical_count"] == 24
    assert inventory["physical_count"] == 22
    assert set(inventory["stores"]) == manifest_names
    assert {details["criticality"] for details in inventory["stores"].values()} == {
        "delivery",
        "authority",
        "memory",
        "telemetry",
    }
    assert inventory["stores"]["librarian_state"]["criticality"] == "telemetry"
    assert {details["journal_mode"] for details in inventory["stores"].values()} <= {
        "delete",
        "truncate",
        "wal",
    }

    assert set(storage["snapshots"]) == manifest_names
    assert all(
        metrics == {"count": 0, "last_timestamp": None, "last_outcome": None}
        for metrics in storage["snapshots"].values()
    )
    _assert_no_sensitive_metric_values(storage, data_root=data_root, secret=secret)


def test_clean_existing_store_records_preflight_ok_and_warning_free_boot_pass(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "data"
    base = data_root / "conversations.db"
    store = api_module.ConversationStore(db_path=os.fspath(base))
    store.close()

    class QuietStoreCatalog(api_module.DaemonStoreCatalog):
        def __init__(self, expected_root: str | os.PathLike[str]) -> None:
            super().__init__(expected_root, silence_allowlist={})

    monkeypatch.setattr(api_module, "DaemonStoreCatalog", QuietStoreCatalog)
    app = api_module.create_api(
        max_sessions=10,
        default_working_dir=str(tmp_path),
        db_path=os.fspath(base),
    )
    key = _register_operator(app, tmp_path)
    client = TestClient(app)
    try:
        storage = _storage_status(client, key)
    finally:
        client.close()

    manifest_names = _manifest_names(base)
    assert storage["boot_gate"]["outcome"] == "pass"
    assert storage["boot_gate"]["warning_count"] == 0
    assert storage["reconcile_warning_count"] == 0
    assert storage["preflight"]["conversations"] == "ok"
    assert {
        logical_name: outcome
        for logical_name, outcome in storage["preflight"].items()
        if logical_name != "conversations"
    } == {
        logical_name: "skipped-absent"
        for logical_name in manifest_names
        if logical_name != "conversations"
    }


def test_snapshot_metrics_count_each_logical_member_without_paths_and_do_not_persist_json(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    secret = "snapshot-metrics-secret"
    monkeypatch.setenv("PINKY_SESSION_SECRET", secret)
    caplog.set_level(logging.INFO)
    data_root = tmp_path / "data"
    base = data_root / "conversations.db"
    app = api_module.create_api(
        max_sessions=10,
        default_working_dir=str(tmp_path),
        db_path=os.fspath(base),
    )
    key = _register_operator(app, tmp_path)

    client = TestClient(app)
    try:
        response = client.post(
            "/internal/stores/snapshot",
            headers=_signed_headers(
                key,
                method="POST",
                path="/internal/stores/snapshot",
            ),
            json={"logical_name": "sessions"},
        )
        assert response.status_code == 200
        assert response.json()["status"] == "success"
        [published] = response.json()["snapshots"]
        snapshot_path = published["path"]

        def fail_snapshot(_selected: Any) -> None:
            raise sqlite3.DatabaseError("simulated snapshot corruption")

        monkeypatch.setattr(
            app.state.store_snapshot_service,
            "_snapshot_selected",
            fail_snapshot,
        )
        failed_response = client.post(
            "/internal/stores/snapshot",
            headers=_signed_headers(
                key,
                method="POST",
                path="/internal/stores/snapshot",
            ),
            json={"logical_name": "conversations"},
        )
        assert failed_response.status_code == 200
        assert failed_response.json()["status"] == "failed"
        storage = _storage_status(client, key)
    finally:
        client.close()

    for logical_name in ("sessions", "session_events"):
        metrics = storage["snapshots"][logical_name]
        assert metrics["count"] == 1
        assert metrics["last_timestamp"]
        assert metrics["last_outcome"] == "published"
    assert storage["snapshots"]["conversations"]["count"] == 1
    assert storage["snapshots"]["conversations"]["last_timestamp"]
    assert storage["snapshots"]["conversations"]["last_outcome"] == "failed"
    _assert_no_sensitive_metric_values(storage, data_root=data_root, secret=secret)

    events = _storage_event_lines(caplog, capsys)
    snapshot_events = [event for event in events if "event=snapshot" in event]
    assert snapshot_events
    assert any("logical_name=sessions" in event for event in snapshot_events)
    assert any("logical_name=session_events" in event for event in snapshot_events)
    for event in snapshot_events:
        assert snapshot_path not in event
        assert os.fspath(data_root) not in event
        assert secret not in event
        assert "storage-operator" not in event
        assert "simulated snapshot corruption" not in event

    # Barsik ruling: current-process counters only. Durable history stays in
    # structured logs + P0.3 audit; P0.5 must not add a JSON persistence surface.
    assert list(data_root.rglob("*.json")) == []


def test_corrupt_preflight_logs_bounded_failure_before_create_api_raises(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    base = tmp_path / "conversations.db"
    base.write_bytes(b"\x0d" + bytes(4095))
    caplog.set_level(logging.INFO)

    with pytest.raises(StoreCatalogError):
        api_module.create_api(db_path=os.fspath(base))

    events = _storage_event_lines(caplog, capsys)
    preflight = [event for event in events if "phase=preflight" in event]
    boot = [event for event in events if "phase=boot_gate" in event]
    assert any(
        "logical_name=conversations" in event and "outcome=failed-corrupt" in event
        for event in preflight
    )
    assert any("outcome=fail" in event for event in boot)
    for event in preflight + boot:
        assert os.path.realpath(base) not in event
        assert "file is not a database" not in event.lower()


def test_final_catalog_failure_logs_bounded_boot_gate_failure_before_raise(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    caplog.set_level(logging.INFO)
    forbidden = os.fspath(tmp_path / "secret-layout.db")

    def fail_validation(_catalog: StoreCatalog) -> list[str]:
        raise StoreCatalogError(f"unsafe layout at {forbidden}")

    monkeypatch.setattr(StoreCatalog, "validate", fail_validation)

    with pytest.raises(StoreCatalogError, match="unsafe layout"):
        api_module.create_api(db_path=os.fspath(tmp_path / "conversations.db"))

    events = _storage_event_lines(caplog, capsys)
    boot = [event for event in events if "phase=boot_gate" in event]
    assert any("outcome=fail" in event for event in boot)
    assert all(forbidden not in event for event in boot)
    assert all("unsafe layout" not in event for event in boot)
