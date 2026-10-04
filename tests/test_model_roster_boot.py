"""Bundled local apply is owned by registry construction, without a running daemon."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest

import pinky_daemon
from pinky_daemon.agent_registry import AgentRegistry
from tests._model_roster_local import (
    DOCUMENT_KEY,
    MARKER,
    apply,
    bundled_bytes,
    document,
    encode,
    legacy_db,
    model_row,
    new_model,
    owned,
    snapshot,
    status,
)


def test_fresh_constructor_classifies_then_applies_the_bundle(tmp_path):
    instance = AgentRegistry(db_path=str(tmp_path / "agents.db"))
    try:
        assert instance.get_setting(MARKER) == "1"
        assert status(instance)["last_applied_revision"] == json.loads(bundled_bytes())["revision"]
        assert status(instance)["source"] == "bundled"
        assert instance.get_setting(DOCUMENT_KEY).encode("utf-8") == bundled_bytes()
        assert owned(instance) == set()
    finally:
        instance.close()


def test_constructor_invokes_apply_only_after_classification(tmp_path, monkeypatch):
    observations = []

    def apply_spy(self, blob, *, source, dry_run=False):
        observations.append((self.get_setting(MARKER), blob, source, dry_run))
        return {"counts": {}, "rows": []}

    monkeypatch.setattr(AgentRegistry, "apply_model_roster", apply_spy, raising=False)
    instance = AgentRegistry(db_path=str(tmp_path / "agents.db"))
    try:
        assert observations == [("1", bundled_bytes(), "bundled", False)]
    finally:
        instance.close()


@pytest.mark.parametrize(
    "error",
    [
        ValueError("invalid roster"),
        RuntimeError("apply failed"),
        sqlite3.OperationalError("database busy"),
    ],
    ids=["validation", "runtime", "sqlite"],
)
def test_constructor_apply_error_is_logged_recorded_and_nonfatal(
    tmp_path, monkeypatch, capsys, error
):
    calls = []

    def fail(self, blob, *, source, dry_run=False):
        calls.append(source)
        raise error

    monkeypatch.setattr(AgentRegistry, "apply_model_roster", fail, raising=False)
    instance = AgentRegistry(db_path=str(tmp_path / "agents.db"))
    try:
        assert calls == ["bundled"]
        output = capsys.readouterr()
        assert "ERROR" in output.out + output.err
        assert instance.get_setting("model_roster.last_error")
        assert instance.list_models()
    finally:
        instance.close()


def test_constructor_still_returns_when_error_metadata_cannot_be_written(
    tmp_path, monkeypatch, capsys
):
    path = legacy_db(tmp_path / "legacy.db")
    with closing(sqlite3.connect(path)) as connection, connection:
        for operation in ("INSERT", "UPDATE"):
            connection.execute(f"""CREATE TRIGGER reject_error_{operation.lower()}
                BEFORE {operation} ON system_settings
                WHEN NEW.key='model_roster.last_error' AND NEW.value<>''
                BEGIN SELECT RAISE(ABORT, 'error metadata unavailable'); END""")
    calls = []

    def fail(self, blob, *, source, dry_run=False):
        calls.append(source)
        raise ValueError("invalid roster")

    monkeypatch.setattr(AgentRegistry, "apply_model_roster", fail, raising=False)
    instance = AgentRegistry(db_path=str(path))
    try:
        assert calls == ["bundled"]
        output = capsys.readouterr()
        assert "ERROR" in output.out + output.err
        assert not instance._db.in_transaction
        assert instance.list_models()
    finally:
        instance.close()


def test_older_bundle_after_remote_revision_changes_nothing(tmp_path):
    path = tmp_path / "agents.db"
    instance = AgentRegistry(db_path=str(path))
    value = document(10)
    model_row(value)["pricing"]["input"] = 7.0
    try:
        apply(instance, value)
        original = snapshot(instance)
    finally:
        instance.close()
    reopened = AgentRegistry(db_path=str(path))
    try:
        assert snapshot(reopened) == original
        assert status(reopened)["last_applied_revision"] == 10
    finally:
        reopened.close()


def run_copied_bundle(tmp_path, value, script):
    """Change only a synthetic package copy, before its first isolated import."""
    copied_src = tmp_path / "source"
    package = copied_src / "pinky_daemon"
    shutil.copytree(
        Path(pinky_daemon.__file__).parent,
        package,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    (package / "catalog/models.json").write_bytes(encode(value))
    result_path = tmp_path / "result.json"
    code = (
        """
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import pinky_daemon
assert Path(pinky_daemon.__file__).resolve().is_relative_to(Path(sys.argv[1]).resolve()), 'child must import the synthetic package copy'
"""
        + script
    )
    child_env = {
        key: value
        for key, value in os.environ.items()
        if key in {"PATH", "HOME", "TMPDIR", "LANG"} or key.startswith("LC_")
    }
    completed = subprocess.run(
        [
            sys.executable,
            "-I",
            "-B",
            "-c",
            code,
            str(copied_src),
            str(tmp_path / "agents.db"),
            str(result_path),
        ],
        cwd=tmp_path,
        env=child_env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(result_path.read_text())


@pytest.mark.parametrize("count", [21, 500])
def test_new_bundle_rows_are_managed_after_baseline_classification(tmp_path, count):
    value = document(5)
    model_row(value)["pricing"]["input"] = 7.0
    for index in range(count - len(value["models"])):
        value["models"].append(new_model(f"roster-added-{index}"))
    result = run_copied_bundle(
        tmp_path,
        value,
        """
import json
from pathlib import Path
from pinky_daemon.agent_registry import AgentRegistry
instance = AgentRegistry(db_path=sys.argv[2])
try:
    rows = instance.list_models(active_only=False)
    row = instance.get_model('anthropic/claude-sonnet-5')
    assert row.get('operator_fields') == '[]', 'current bundle differences must not become operator-owned'
    assert row['input_price'] == 7.0 and row.get('roster_revision') == 5
    assert all(r.get('operator_fields') == '[]' for r in rows)
    Path(sys.argv[3]).write_text(json.dumps({'count': len(rows)}))
finally:
    instance.close()
""",
    )
    assert result["count"] == count


def test_new_inactive_bundle_row_is_not_inserted_at_boot(tmp_path):
    value = document(5)
    value["models"].append(new_model(active=False))
    result = run_copied_bundle(
        tmp_path,
        value,
        """
import json
from pathlib import Path
from pinky_daemon.agent_registry import AgentRegistry
instance = AgentRegistry(db_path=sys.argv[2])
try:
    assert instance.get_model('openai/roster-added-model') is None, 'inactive bundle row must not be seeded'
    Path(sys.argv[3]).write_text(json.dumps({'count': len(instance.list_models())}))
finally:
    instance.close()
""",
    )
    assert result["count"] == len(document()["models"])


def test_poll_mode_constructor_uses_the_same_local_bundle(tmp_path, monkeypatch):
    from pinky_daemon.daemon import Daemon, DaemonConfig

    calls = []

    def apply_spy(self, blob, *, source, dry_run=False):
        calls.append((blob, source, self.get_setting(MARKER)))
        return {"counts": {}, "rows": []}

    monkeypatch.setattr(AgentRegistry, "apply_model_roster", apply_spy, raising=False)
    daemon = Daemon(DaemonConfig(working_dir=str(tmp_path)))
    try:
        assert calls == [(bundled_bytes(), "bundled", "1")]
        assert not daemon.is_running and daemon._pollers == [] and daemon._tasks == []
    finally:
        daemon._conversation_store.close()
        daemon._task_store.close()
        daemon._registry.close()
