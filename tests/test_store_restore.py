"""P0.5 contracts for attended, daemon-down SQLite store restoration."""

from __future__ import annotations

import ast
import importlib
import importlib.util
import logging
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from pinky_daemon import api as api_module
from pinky_daemon import store_snapshot
from pinky_daemon.agent_comms import AgentComms
from pinky_daemon.conversation_store import ConversationStore
from pinky_daemon.store_catalog import StoreCatalog
from pinky_daemon.task_store import TaskStore


def _authority_module():
    spec = importlib.util.find_spec("pinky_daemon.store_authority")
    assert spec is not None, "pinky_daemon.store_authority is not implemented"
    return importlib.import_module("pinky_daemon.store_authority")


def _restore_module():
    spec = importlib.util.find_spec("pinky_daemon.store_restore")
    assert spec is not None, "pinky_daemon.store_restore is not implemented"
    return importlib.import_module("pinky_daemon.store_restore")


def _restore(
    base: Path,
    logical_name: str,
    snapshot_path: Path,
) -> Any:
    module = _restore_module()
    restore = getattr(module, "restore_store", None)
    assert restore is not None, "restore_store is not implemented"
    return restore(
        db_path=os.fspath(base),
        logical_name=logical_name,
        snapshot_path=os.fspath(snapshot_path),
    )


def _snapshot_conversations(
    base: Path,
    *,
    session_id: str = "restore-drill",
    messages: tuple[str, ...] = ("alpha", "beta"),
) -> Path:
    catalog = StoreCatalog(expected_root=base.parent, silence_allowlist={})
    store = ConversationStore(db_path=os.fspath(base), catalog=catalog)
    try:
        for index, message in enumerate(messages):
            store.append(
                session_id,
                "user" if index % 2 == 0 else "assistant",
                message,
                timestamp=1_700_000_000.0 + index,
            )
    finally:
        store.close()
    [result] = store_snapshot.StoreSnapshotService(catalog).create_snapshots("conversations")
    assert result.verification == "ok"
    assert result.snapshot_path is not None
    return Path(result.snapshot_path)


def _snapshot_tasks(base: Path) -> tuple[Path, Path]:
    tasks_path = Path(api_module._derive_api_store_manifest(base)["tasks"].path)
    catalog = StoreCatalog(expected_root=base.parent, silence_allowlist={})
    store = TaskStore(db_path=os.fspath(tasks_path), catalog=catalog)
    store.close()
    [result] = store_snapshot.StoreSnapshotService(catalog).create_snapshots("tasks")
    assert result.verification == "ok"
    assert result.snapshot_path is not None
    return tasks_path, Path(result.snapshot_path)


def _write_corrupt_target(path: Path) -> bytes:
    prefix = b"\x0d" + b"p0.5-corrupt-store"
    corrupt = prefix + bytes(4096 - len(prefix))
    assert len(corrupt) == 4096
    path.write_bytes(corrupt)
    return corrupt


def _immutable_connection(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)


def _quick_check(path: Path) -> list[tuple[str]]:
    connection = _immutable_connection(path)
    try:
        return connection.execute("PRAGMA quick_check").fetchall()
    finally:
        connection.close()


def _journal_mode(path: Path) -> str:
    connection = _immutable_connection(path)
    try:
        return str(connection.execute("PRAGMA journal_mode").fetchone()[0]).lower()
    finally:
        connection.close()


def _messages(path: Path, session_id: str = "restore-drill") -> list[str]:
    connection = _immutable_connection(path)
    try:
        rows = connection.execute(
            "SELECT content FROM messages WHERE session_id = ? ORDER BY id",
            (session_id,),
        ).fetchall()
        return [str(row[0]) for row in rows]
    finally:
        connection.close()


def _corrupt_artifacts(target: Path) -> list[Path]:
    return sorted(target.parent.glob(target.name + ".corrupt-*"))


def _configure_lsof(
    monkeypatch: pytest.MonkeyPatch,
    *,
    stdout: str = "p999999\nf7\nD0x7fffffff\ni9223372036854775806\n",
    stderr: str = "",
    returncode: int = 0,
    error: BaseException | None = None,
    on_run: Any | None = None,
) -> None:
    authority = _authority_module()
    resolver = getattr(authority, "_resolve_lsof_binary", None)
    assert resolver is not None, "store authority lsof resolver is not implemented"
    monkeypatch.setattr(authority, "_resolve_lsof_binary", lambda: "/fake/lsof")

    def run(*_args: Any, **_kwargs: Any) -> SimpleNamespace:
        if error is not None:
            raise error
        if on_run is not None:
            on_run()
        return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=returncode)

    monkeypatch.setattr(authority.subprocess, "run", run)


def _configure_lsof_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    authority = _authority_module()
    resolver = getattr(authority, "_resolve_lsof_binary", None)
    assert resolver is not None, "store authority lsof resolver is not implemented"
    monkeypatch.setattr(authority, "_resolve_lsof_binary", lambda: None)


def _storage_event_lines(
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> list[str]:
    messages = [record.getMessage() for record in caplog.records]
    captured = capsys.readouterr()
    messages.extend(captured.err.splitlines())
    messages.extend(captured.out.splitlines())
    return [message for message in messages if message.startswith("storage_event ")]


def test_store_authority_lock_is_canonical_nonblocking_and_never_unlinked(
    tmp_path: Path,
) -> None:
    authority = _authority_module()
    lock = getattr(authority, "store_authority_lock", None)
    lock_path_for = getattr(authority, "store_authority_lock_path", None)
    error_type = getattr(authority, "StoreAuthorityError", None)
    assert lock is not None, "store_authority_lock is not implemented"
    assert lock_path_for is not None, "store_authority_lock_path is not implemented"
    assert error_type is not None, "StoreAuthorityError is not implemented"
    raw_base = tmp_path / "nested" / ".." / "data" / "conversations.db"
    expected_root = Path(os.path.realpath(raw_base)).parent

    with lock(raw_base):
        lock_path = Path(lock_path_for(raw_base))
        assert lock_path.parent == expected_root
        assert lock_path.is_file()
        with pytest.raises(error_type):
            with lock(raw_base):
                pytest.fail("a second daemon/restore acquired the authority lock")

    assert lock_path.is_file(), "the stable lock inode must never be unlinked"
    with lock(raw_base):
        assert lock_path.is_file()


def test_supported_daemon_entrypoint_holds_authority_lock_before_create_api_and_uvicorn(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from pinky_daemon import __main__ as daemon_main

    authority = _authority_module()
    lock = getattr(authority, "store_authority_lock", None)
    error_type = getattr(authority, "StoreAuthorityError", None)
    assert lock is not None and error_type is not None
    base = tmp_path / "data" / "conversations.db"
    observations: list[str] = []

    def assert_lock_held(phase: str) -> None:
        with pytest.raises(error_type):
            with lock(base):
                pytest.fail(f"authority lock was not held during {phase}")
        observations.append(phase)

    def fake_create_api(**kwargs: Any) -> SimpleNamespace:
        assert kwargs["db_path"] == os.fspath(base)
        assert_lock_held("create_api")
        return SimpleNamespace(state=SimpleNamespace(ferry_listener=None))

    def fake_uvicorn_run(_app: Any, **_kwargs: Any) -> None:
        assert_lock_held("uvicorn")

    monkeypatch.delenv("PINKYBOT_FERRY_ENABLED", raising=False)
    monkeypatch.setattr(api_module, "create_api", fake_create_api)
    uvicorn = importlib.import_module("uvicorn")
    monkeypatch.setattr(uvicorn, "run", fake_uvicorn_run)
    args = SimpleNamespace(
        host="127.0.0.1",
        port=8888,
        working_dir=os.fspath(tmp_path),
        max_sessions=3,
        db_path=os.fspath(base),
    )

    daemon_main._run_api(args)

    assert observations == ["create_api", "uvicorn"]
    with lock(base):
        pass


def test_daemon_cli_accepts_nonbreaking_db_path_and_passes_it_to_api_mode(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from pinky_daemon import __main__ as daemon_main

    base = tmp_path / "custom" / "conversations.db"
    received: list[Any] = []
    monkeypatch.setattr(daemon_main, "_run_api", received.append)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "pinky_daemon",
            "--mode",
            "api",
            "--db-path",
            os.fspath(base),
        ],
    )

    daemon_main.main()

    assert len(received) == 1
    assert received[0].db_path == os.fspath(base)


def test_restore_refuses_while_daemon_authority_lock_is_held(
    tmp_path: Path,
) -> None:
    authority = _authority_module()
    restore = _restore_module()
    safety_error = getattr(restore, "StoreRestoreSafetyError", None)
    assert safety_error is not None, "StoreRestoreSafetyError is not implemented"
    base = tmp_path / "data" / "conversations.db"
    snapshot_path = _snapshot_conversations(base)
    corrupt_bytes = _write_corrupt_target(base)
    before_identity = (base.stat().st_dev, base.stat().st_ino)

    with authority.store_authority_lock(base):
        with pytest.raises(safety_error):
            _restore(base, "conversations", snapshot_path)

    assert base.read_bytes() == corrupt_bytes
    assert (base.stat().st_dev, base.stat().st_ino) == before_identity
    assert _corrupt_artifacts(base) == []


@pytest.mark.parametrize("held_suffix", ["", "-wal"], ids=["main", "wal-sidecar"])
def test_restore_refuses_foreign_descriptor_on_target_or_sidecar_inode(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    held_suffix: str,
) -> None:
    restore = _restore_module()
    safety_error = getattr(restore, "StoreRestoreSafetyError", None)
    assert safety_error is not None
    base = tmp_path / "data" / "conversations.db"
    snapshot_path = _snapshot_conversations(base)
    corrupt_bytes = _write_corrupt_target(base)
    held_path = Path(os.fspath(base) + held_suffix)
    if held_suffix:
        held_path.write_bytes(b"held-forensic-sidecar")
    held_stat = held_path.stat()

    with held_path.open("rb") as foreign_holder:
        _configure_lsof(
            monkeypatch,
            stdout=(
                f"p{os.getpid()}\n"
                f"f{foreign_holder.fileno()}\n"
                f"D0x{held_stat.st_dev:x}\n"
                f"i{held_stat.st_ino}\n"
            ),
        )
        with pytest.raises(safety_error):
            _restore(base, "conversations", snapshot_path)

    assert base.read_bytes() == corrupt_bytes
    if held_suffix:
        assert held_path.read_bytes() == b"held-forensic-sidecar"
    assert _corrupt_artifacts(base) == []


@pytest.mark.parametrize("race", ["target-identity", "sidecar-set"])
def test_restore_refuses_identity_or_sidecar_change_after_descriptor_scan(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    race: str,
) -> None:
    restore = _restore_module()
    safety_error = getattr(restore, "StoreRestoreSafetyError", None)
    assert safety_error is not None
    base = tmp_path / "data" / "conversations.db"
    snapshot_path = _snapshot_conversations(base)
    corrupt_bytes = _write_corrupt_target(base)
    before_identity = (base.stat().st_dev, base.stat().st_ino)
    raced_bytes = b"raced replacement must not be preserved as the selected target"

    def mutate_after_scan() -> None:
        if race == "target-identity":
            replacement = base.with_name("raced-replacement.tmp")
            replacement.write_bytes(raced_bytes)
            os.replace(replacement, base)
        else:
            Path(os.fspath(base) + "-wal").write_bytes(b"late-sidecar")

    _configure_lsof(monkeypatch, on_run=mutate_after_scan)

    with pytest.raises(safety_error):
        _restore(base, "conversations", snapshot_path)

    if race == "target-identity":
        assert base.read_bytes() == raced_bytes
        assert (base.stat().st_dev, base.stat().st_ino) != before_identity
    else:
        assert base.read_bytes() == corrupt_bytes
        assert Path(os.fspath(base) + "-wal").read_bytes() == b"late-sidecar"
    assert _corrupt_artifacts(base) == []


@pytest.mark.parametrize("failure", ["unavailable", "timeout", "malformed"])
def test_restore_fails_closed_when_descriptor_scan_cannot_prove_store_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failure: str,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    restore = _restore_module()
    safety_error = getattr(restore, "StoreRestoreSafetyError", None)
    assert safety_error is not None
    caplog.set_level(logging.INFO)
    base = tmp_path / "data" / "conversations.db"
    snapshot_path = _snapshot_conversations(base)
    corrupt_bytes = _write_corrupt_target(base)
    target_stat = base.stat()

    if failure == "unavailable":
        _configure_lsof_unavailable(monkeypatch)
    elif failure == "timeout":
        _configure_lsof(
            monkeypatch,
            error=subprocess.TimeoutExpired("lsof", 5),
        )
    else:
        _configure_lsof(
            monkeypatch,
            stdout=f"pnot-a-pid\nfwat\nDgarbage\ni{target_stat.st_ino}\n",
        )

    with pytest.raises(safety_error):
        _restore(base, "conversations", snapshot_path)

    assert base.read_bytes() == corrupt_bytes
    assert _corrupt_artifacts(base) == []
    events = _storage_event_lines(caplog, capsys)
    restore_events = [event for event in events if "event=restore" in event]
    assert any(
        "logical_name=conversations" in event and "outcome=refused" in event
        for event in restore_events
    )
    assert all(os.fspath(base.parent) not in event for event in restore_events)
    assert all("/fake/lsof" not in event for event in restore_events)
    assert all("TimeoutExpired" not in event for event in restore_events)
    assert all("not-a-pid" not in event for event in restore_events)


def test_restore_rejects_bad_snapshot_before_mutating_target_or_sidecars(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    restore = _restore_module()
    verification_error = getattr(restore, "StoreRestoreVerificationError", None)
    assert verification_error is not None, "StoreRestoreVerificationError is not implemented"
    caplog.set_level(logging.INFO)
    base = tmp_path / "data" / "conversations.db"
    good_snapshot = _snapshot_conversations(base)
    bad_snapshot = good_snapshot.with_name("bad-snapshot.db")
    bad_snapshot.write_bytes(b"\x0d" + bytes(4095))
    corrupt_bytes = _write_corrupt_target(base)
    sidecar = Path(os.fspath(base) + "-journal")
    sidecar.write_bytes(b"forensic-journal")
    before = {
        base: ((base.stat().st_dev, base.stat().st_ino), base.read_bytes()),
        sidecar: ((sidecar.stat().st_dev, sidecar.stat().st_ino), sidecar.read_bytes()),
    }
    _configure_lsof(monkeypatch)

    with pytest.raises(verification_error):
        _restore(base, "conversations", bad_snapshot)

    for path, (identity, content) in before.items():
        assert (path.stat().st_dev, path.stat().st_ino) == identity
        assert path.read_bytes() == content
    assert base.read_bytes() == corrupt_bytes
    assert _corrupt_artifacts(base) == []
    events = _storage_event_lines(caplog, capsys)
    restore_events = [event for event in events if "event=restore" in event]
    assert any(
        "logical_name=conversations" in event and "outcome=failed" in event
        for event in restore_events
    )
    assert all(os.fspath(base.parent) not in event for event in restore_events)
    assert all(os.fspath(bad_snapshot) not in event for event in restore_events)


def test_restore_rejects_quick_check_ok_snapshot_of_wrong_store_schema(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    restore = _restore_module()
    verification_error = getattr(restore, "StoreRestoreVerificationError", None)
    assert verification_error is not None
    base = tmp_path / "data" / "conversations.db"
    expected_snapshot = _snapshot_conversations(base)
    wrong_snapshot = expected_snapshot.with_name("healthy-wrong-store.db")
    wrong_store = AgentComms(db_path=os.fspath(wrong_snapshot))
    wrong_store.close()
    assert _quick_check(wrong_snapshot) == [("ok",)]
    corrupt_bytes = _write_corrupt_target(base)
    _configure_lsof(monkeypatch)

    with pytest.raises(verification_error, match="store|schema|table"):
        _restore(base, "conversations", wrong_snapshot)

    assert base.read_bytes() == corrupt_bytes
    assert _corrupt_artifacts(base) == []


def test_schema_sentinel_inventory_is_complete_for_every_manifest_logical_store(
    tmp_path: Path,
) -> None:
    restore = _restore_module()
    sentinels = getattr(restore, "STORE_SCHEMA_SENTINELS", None)
    assert sentinels is not None, "STORE_SCHEMA_SENTINELS is not implemented"
    manifest = api_module._derive_api_store_manifest(tmp_path / "conversations.db")

    assert set(sentinels) == set(manifest)
    for logical_name, required_tables in sentinels.items():
        assert isinstance(required_tables, tuple), logical_name
        assert required_tables, logical_name
        assert all(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table) for table in required_tables)
    assert "sessions" in sentinels["sessions"]
    assert "session_events" in sentinels["session_events"]
    assert "messages" in sentinels["conversations"]
    assert "messages_fts" in sentinels["conversations"]


@pytest.mark.parametrize("selection", ["unregistered", "absent", "nonregular", "symlink"])
def test_restore_refuses_unregistered_absent_or_unsafe_manifest_target(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    selection: str,
) -> None:
    restore = _restore_module()
    selection_error = getattr(restore, "StoreRestoreSelectionError", None)
    assert selection_error is not None, "StoreRestoreSelectionError is not implemented"
    source_base = tmp_path / "source" / "conversations.db"
    conversation_snapshot = _snapshot_conversations(source_base)
    target_base = tmp_path / "target" / "conversations.db"
    logical_name = "conversations"
    snapshot_path = conversation_snapshot
    selected_target = target_base

    if selection == "unregistered":
        logical_name = "not_in_manifest"
    elif selection == "nonregular":
        target_base.mkdir(parents=True)
    elif selection == "symlink":
        logical_name = "tasks"
        real_base = tmp_path / "tasks-source" / "conversations.db"
        real_tasks, snapshot_path = _snapshot_tasks(real_base)
        manifest_target = Path(api_module._derive_api_store_manifest(target_base)["tasks"].path)
        manifest_target.parent.mkdir(parents=True, exist_ok=True)
        manifest_target.symlink_to(real_tasks)
        selected_target = manifest_target

    before_lstat = selected_target.lstat() if selected_target.exists() else None

    _configure_lsof(monkeypatch)

    with pytest.raises(selection_error):
        _restore(target_base, logical_name, snapshot_path)

    assert _corrupt_artifacts(selected_target) == []
    if before_lstat is not None:
        after_lstat = selected_target.lstat()
        assert (after_lstat.st_dev, after_lstat.st_ino, after_lstat.st_mode) == (
            before_lstat.st_dev,
            before_lstat.st_ino,
            before_lstat.st_mode,
        )


def test_restore_preserves_corrupt_main_and_sidecars_and_atomically_installs_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    restore = _restore_module()
    caplog.set_level(logging.INFO)
    base = tmp_path / "data" / "conversations.db"
    snapshot_path = _snapshot_conversations(base, messages=("known-one", "known-two"))
    snapshot_mode = _journal_mode(snapshot_path)
    snapshot_bytes = snapshot_path.read_bytes()
    corrupt_bytes = _write_corrupt_target(base)
    sidecar_bytes = {
        "-wal": b"forensic-wal",
        "-shm": b"forensic-shm",
        "-journal": b"forensic-journal",
    }
    for suffix, content in sidecar_bytes.items():
        Path(os.fspath(base) + suffix).write_bytes(content)
    replace_calls: list[tuple[Path, Path]] = []
    real_replace = os.replace

    def recording_replace(source: str | os.PathLike[str], target: str | os.PathLike[str]) -> None:
        source_path = Path(source)
        target_path = Path(target)
        if target_path == base:
            assert source_path.parent == base.parent
            assert source_path.is_file()
            replace_calls.append((source_path, target_path))
        real_replace(source, target)

    monkeypatch.setattr(restore.os, "replace", recording_replace)
    _configure_lsof(monkeypatch)

    result = _restore(base, "conversations", snapshot_path)

    assert result.logical_names == ("conversations",)
    assert result.verification == "ok"
    corrupt_path = Path(result.corrupt_path)
    assert corrupt_path in _corrupt_artifacts(base)
    assert corrupt_path.read_bytes() == corrupt_bytes
    for suffix, content in sidecar_bytes.items():
        assert Path(os.fspath(corrupt_path) + suffix).read_bytes() == content
        assert not Path(os.fspath(base) + suffix).exists()
    assert _quick_check(base) == [("ok",)]
    assert _messages(base) == ["known-one", "known-two"]
    assert base.read_bytes() == snapshot_bytes
    assert snapshot_path.read_bytes() == snapshot_bytes
    assert _journal_mode(base) == snapshot_mode
    assert (base.stat().st_dev, base.stat().st_ino) != (
        snapshot_path.stat().st_dev,
        snapshot_path.stat().st_ino,
    )
    assert len(replace_calls) == 1
    assert replace_calls[0][1] == base
    assert list(base.parent.glob("*.tmp")) == []

    events = _storage_event_lines(caplog, capsys)
    restore_events = [event for event in events if "event=restore" in event]
    assert any(
        "logical_name=conversations" in event and "outcome=success" in event
        for event in restore_events
    )
    assert all(os.fspath(base.parent) not in event for event in restore_events)
    assert all(os.fspath(snapshot_path) not in event for event in restore_events)


def test_restore_verification_is_static_immutable_and_never_reads_or_sets_journal_mode(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    restore = _restore_module()
    base = tmp_path / "data" / "conversations.db"
    snapshot_path = _snapshot_conversations(base)
    _write_corrupt_target(base)
    real_connect = sqlite3.connect
    opens: list[tuple[str, dict[str, Any]]] = []
    statements: list[str] = []

    def recording_connect(database: Any, *args: Any, **kwargs: Any) -> sqlite3.Connection:
        rendered = os.fspath(database)
        opens.append((rendered, dict(kwargs)))
        connection = real_connect(database, *args, **kwargs)
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(restore.sqlite3, "connect", recording_connect)
    _configure_lsof(monkeypatch)

    _restore(base, "conversations", snapshot_path)

    assert len(opens) >= 2
    for database, kwargs in opens:
        assert "mode=ro" in database
        assert "immutable=1" in database
        assert kwargs.get("uri") is True
    normalized = [statement.upper() for statement in statements]
    assert any("PRAGMA QUICK_CHECK" in statement for statement in normalized)
    assert any("SQLITE_MASTER" in statement for statement in normalized)
    assert all("JOURNAL_MODE" not in statement for statement in normalized)


def test_full_p03_snapshot_corrupt_restore_p04_clean_boot_drill(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    base = tmp_path / "data" / "conversations.db"
    known_messages = ("before-corruption", "survives-restore")
    snapshot_path = _snapshot_conversations(base, messages=known_messages)
    corrupt_bytes = _write_corrupt_target(base)
    _configure_lsof(monkeypatch)

    result = _restore(base, "conversations", snapshot_path)

    corrupt_path = Path(result.corrupt_path)
    assert corrupt_path.read_bytes() == corrupt_bytes
    assert _quick_check(base) == [("ok",)]
    assert _messages(base) == list(known_messages)

    # Load-bearing recovery proof: the installed P0.3 copy clears P0.4's
    # pre-constructor integrity gate and the complete boot gate returns an app.
    fresh_app = api_module.create_api(
        default_working_dir=os.fspath(tmp_path),
        db_path=os.fspath(base),
    )
    assert fresh_app.state.store_catalog is not None
    assert [
        message.content
        for message in fresh_app.state.conversation_store.get_history("restore-drill")
    ] == list(known_messages)


def test_restore_cli_is_local_attended_wrapper_with_explicit_selection() -> None:
    script = Path(__file__).resolve().parents[1] / "scripts" / "store_restore.py"
    assert script.is_file(), "scripts/store_restore.py is not implemented"
    source = script.read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported_modules = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imported_from = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}

    assert "--db-path" in source
    assert "--logical-name" in source
    assert "--snapshot" in source
    assert "restore_store" in source
    assert not any(name and name.startswith("urllib") for name in imported_modules | imported_from)
    assert "/internal/" not in source
