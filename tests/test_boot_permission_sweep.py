"""Priority hardening, child ownership and owner alerts during API boot."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from pinky_daemon import api, db_security


def _file(root, relative, mode=0o644):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"fixture")
    path.chmod(mode)
    return path


def _summary(**changes):
    report = {"changed": 0, "foreign_owned": 0, "priority_done": True,
              "critical_loose": [], "remaining_loose": []}
    report.update(changes)
    return report


def _output(report=None, *, progress=True):
    prefix = "priority-done changed=0\n" if progress else ""
    return prefix + (json.dumps(_summary() if report is None else report) + "\n")


def _cli(root, *arguments, code=None):
    command = ([sys.executable, "-B", "-m", "pinky_daemon.db_security", str(root), *arguments]
               if code is None else [sys.executable, "-B", "-c", code, str(root), *arguments])
    result = subprocess.run(command, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    return result.stdout


def _records(stdout):
    lines = stdout.splitlines()
    assert len(lines) == 2, "The sweep must emit priority progress and one JSON summary only"
    match = re.fullmatch(r"priority-done changed=(\d+)", lines[0])
    assert match is not None
    return int(match[1]), json.loads(lines[1])


def test_child_default_cli_emits_progress_then_only_summary(tmp_path):
    priority, result = _records(_cli(tmp_path))
    assert priority == 0
    assert result == _summary()


def test_priority_order_is_identity_registered_then_full_tree(tmp_path):
    root = tmp_path / "data"
    identity = root / "identity"
    identity.mkdir(parents=True)
    registered = _file(root, "registered.data")
    _file(root, "agents/worker/workspace/copy.db")
    code = r'''
import contextlib, io, json, pathlib, runpy, sys
from pinky_identity import fs_security
root=pathlib.Path(sys.argv[1]); calls=[]
fs_security.harden_secret_dir=lambda p: calls.append(["directory",str(pathlib.Path(p).relative_to(root))]) or 0
fs_security.harden_secret_file=lambda p: calls.append(["file",str(pathlib.Path(p).relative_to(root))]) or 0
sys.argv=["pinky_daemon.db_security",str(root),*sys.argv[2:]]
captured=io.StringIO()
with contextlib.redirect_stdout(captured):
    runpy.run_module("pinky_daemon.db_security",run_name="__main__")
print(json.dumps({"calls":calls,"stdout":captured.getvalue()}))
'''
    result = json.loads(_cli(root, "--registered-store", str(registered), code=code))
    assert result["calls"][:2] == [["directory", "identity"], ["file", "registered.data"]]
    assert result["calls"].index(["file", "agents/worker/workspace/copy.db"]) > 1
    _, report = _records(result["stdout"])
    assert report["priority_done"] is True


def test_full_sweep_uses_entry_types_and_still_covers_workspaces(tmp_path, monkeypatch):
    root = tmp_path / "data"
    database = _file(root, "agents/worker/workspace/copy.db")
    untouched = _file(root, "notes.txt")
    outside = _file(tmp_path, "outside/private.db")
    (root / "link").symlink_to(outside.parent, target_is_directory=True)

    def forbidden(*args, **kwargs):
        raise AssertionError("the full sweep must use entry types instead of rglob")

    monkeypatch.setattr(Path, "rglob", forbidden)
    assert db_security.sweep_db_permissions(root) == 1
    assert database.stat().st_mode & 0o777 == 0o600
    assert untouched.stat().st_mode & 0o777 == 0o644
    assert outside.stat().st_mode & 0o777 == 0o644


@pytest.mark.parametrize("suffix", ["-wal", "-shm", "-journal"])
def test_orphan_sidecars_are_hardened(tmp_path, suffix):
    sidecar = _file(tmp_path, "orphan.db" + suffix)
    assert db_security.sweep_db_permissions(tmp_path) == 1
    assert sidecar.stat().st_mode & 0o777 == 0o600


def test_summary_remembers_fixed_critical_files_and_private_directory_modes(tmp_path):
    root = tmp_path / "data"
    registered = _file(root, "registered.data")
    _file(root, "registered.data-wal")
    identity = _file(root, "identity/nested/key.data")
    identity.parent.chmod(0o700)
    (root / "identity").chmod(0o700)
    _, report = _records(_cli(root, "--registered-store", str(registered)))
    assert set(report["critical_loose"]) == {
        "registered.data", "registered.data-wal", "identity/nested/key.data",
    }
    assert report["remaining_loose"] == []
    assert registered.stat().st_mode & 0o777 == 0o600


def test_failed_hardening_reports_only_remaining_owned_files(tmp_path):
    root = tmp_path / "data"
    _file(root, "blocked.db")
    code = r'''
import os, runpy, sys
from pinky_identity import fs_security
def denied(fd, mode):
    raise PermissionError("fixture denied")
fs_security.os.fchmod=denied
sys.argv=["pinky_daemon.db_security",sys.argv[1]]
runpy.run_module("pinky_daemon.db_security",run_name="__main__")
'''
    _, report = _records(_cli(root, code=code))
    assert report["remaining_loose"] == ["blocked.db"]
    assert report["critical_loose"] == []
    assert report["changed"] == 0


def test_foreign_owned_files_are_counted_without_alert_paths(tmp_path):
    root = tmp_path / "data"
    registered = _file(root, "identity/store.db")
    code = r'''
import os, runpy, sys
from pinky_identity import fs_security
uid=os.getuid()
os.getuid=lambda: uid+1
def denied(fd, mode):
    raise PermissionError("fixture denied")
fs_security.os.fchmod=denied
sys.argv=["pinky_daemon.db_security",sys.argv[1],*sys.argv[2:]]
runpy.run_module("pinky_daemon.db_security",run_name="__main__")
'''
    _, report = _records(_cli(root, "--registered-store", str(registered), code=code))
    assert report["foreign_owned"] >= 1
    assert report["critical_loose"] == report["remaining_loose"] == []


@pytest.mark.parametrize("progress", [False, True])
@pytest.mark.parametrize("failure", ["timeout", "exit"])
def test_parent_failure_logs_one_warning_with_priority_status(
    tmp_path, monkeypatch, caplog, progress, failure
):
    output = "priority-done changed=3\n" if progress else ""

    def run(*args, **kwargs):
        if failure == "timeout":
            raise subprocess.TimeoutExpired("fixture", 1, output=output.encode())
        raise subprocess.CalledProcessError(1, "fixture", output=output)

    monkeypatch.setattr(subprocess, "run", run)
    with caplog.at_level(logging.INFO, logger="pinky.db_security"):
        assert db_security.sweep_db_permissions_in_child(tmp_path) is False
    rows = [row for row in caplog.records if row.name == "pinky.db_security"]
    assert len(rows) == 1 and rows[0].levelno == logging.WARNING
    assert f"priority_done={'yes' if progress else 'no'}" in rows[0].getMessage()
    assert rows[0].exc_info is None


def test_parent_success_logs_elapsed_changed_and_foreign_count(tmp_path, monkeypatch, caplog):
    report = _summary(changed=7, foreign_owned=2)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(
        a[0], 0, stdout=_output(report), stderr=""
    ))
    with caplog.at_level(logging.INFO, logger="pinky.db_security"):
        assert db_security.sweep_db_permissions_in_child(tmp_path) is True
    rows = [row for row in caplog.records if row.name == "pinky.db_security"]
    assert len(rows) == 1 and rows[0].levelno == logging.INFO
    text = rows[0].getMessage()
    assert "SQLite permission sweep completed in child process" in text
    assert "changed=7" in text and "foreign_owned=2" in text
    assert re.search(r"seconds=\d", text)


@pytest.mark.parametrize(
    "extra",
    ["", json.dumps(_summary()) + "\n", "unexpected output\n"],
    ids=["two-lines", "extra-summary", "extra-text"],
)
def test_parent_rejects_output_beyond_the_progress_and_summary(
    tmp_path, monkeypatch, caplog, extra
):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(
        a[0], 0, stdout=_output() + extra, stderr=""
    ))
    with caplog.at_level(logging.INFO, logger="pinky.db_security"):
        result = db_security.sweep_db_permissions_in_child(tmp_path)
    assert result is (extra == "")
    rows = [row for row in caplog.records if row.name == "pinky.db_security"]
    assert len(rows) == 1
    assert rows[0].levelno == (logging.WARNING if extra else logging.INFO)
    if extra:
        assert "status=failed" in rows[0].getMessage()
        assert "priority_done=yes" in rows[0].getMessage()


def test_background_permission_sweep_has_a_fifteen_minute_cap(tmp_path):
    assert db_security.SWEEP_TIMEOUT_SECONDS == 900
    sweep = db_security.PermissionSweep(tmp_path, [], AsyncMock(), lambda text: None)
    assert sweep.timeout == 900


class _Pipe:
    def __init__(self, child, output=b""):
        self.child, self.output = child, output

    async def read(self, size):
        if self.output:
            output, self.output = self.output, b""
            return output
        await self.child.release.wait()
        return b""


class _Child:
    def __init__(self, output, *, hold=False, resistant=False, returncode=0):
        self.output = output.encode()
        self.release = asyncio.Event()
        if not hold:
            self.release.set()
        self.returncode = None
        self.final_returncode = returncode
        self.resistant = resistant
        self.terminated = self.killed = 0
        self.stdout, self.stderr = _Pipe(self, self.output), _Pipe(self)

    async def communicate(self):
        await self.release.wait()
        self.returncode = self.final_returncode if self.returncode is None else self.returncode
        return self.output, b""

    async def wait(self):
        await self.release.wait()
        if self.returncode is None:
            self.returncode = self.final_returncode
        return self.returncode

    def terminate(self):
        self.terminated += 1
        if not self.resistant:
            self.returncode = -15
            self.release.set()

    def kill(self):
        self.killed += 1
        self.returncode = -9
        self.release.set()


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "SHARED_MCP_ENABLED", False)
    monkeypatch.setenv("PINKY_LOG_ROTATION", "off")
    application = api.create_api(
        db_path=str(tmp_path / "data/conversations.db"), default_working_dir=str(tmp_path)
    )
    application.state.scheduler._owner_notify_callback = AsyncMock(return_value=True)
    yield application
    application.state.store_catalog.shutdown(deadline_seconds=5)


def _background_task(app):
    task = getattr(app.state, "db_permission_sweep_task", None)
    assert isinstance(task, asyncio.Task), "The permission sweep must be an owned background task"
    return task


def _child_factory(monkeypatch, child):
    calls = []
    original = asyncio.create_subprocess_exec

    async def create(*args, **kwargs):
        if "pinky_daemon.db_security" in args:
            calls.append(args)
            return child
        return await original(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    return calls


async def test_startup_finishes_while_permission_child_is_blocked(app, monkeypatch):
    child = _Child(_output(), hold=True)
    calls = _child_factory(monkeypatch, child)
    async with app.router.lifespan_context(app):
        task = _background_task(app)
        for _ in range(10):
            if calls:
                break
            await asyncio.sleep(0)
        assert calls and not task.done()
        assert not child.release.is_set()
        registered = {row.resolved_path for row in app.state.store_catalog.snapshot()
                      if not row.is_memory}
        command = calls[0]
        assert registered <= set(command)
        assert command.count("--registered-store") == len(registered)
        child.release.set()
        await asyncio.wait_for(task, 5)


@pytest.mark.parametrize("resistant", [False, True])
async def test_shutdown_ends_the_owned_sweep_child(app, monkeypatch, resistant):
    child = _Child("priority-done changed=0\n", hold=True, resistant=resistant)
    calls = _child_factory(monkeypatch, child)
    async with app.router.lifespan_context(app):
        task = _background_task(app)
        for _ in range(10):
            if calls:
                break
            await asyncio.sleep(0)
        assert calls and not task.done()
    assert task.done()
    assert task.cancelled()
    assert child.terminated == 1
    assert child.killed == int(resistant)
    assert child.release.is_set()


async def test_close_prevents_alert_from_a_finishing_run(tmp_path, monkeypatch):
    child = _Child(_output(), hold=True)
    calls = _child_factory(monkeypatch, child)
    notify = AsyncMock(return_value=True)
    sweep = db_security.PermissionSweep(tmp_path, [], notify, lambda text: None)
    run_task = asyncio.create_task(sweep.run())
    try:
        for _ in range(10):
            if calls:
                break
            await asyncio.sleep(0)
        assert calls and not run_task.done()
        await sweep.close()
        report = await asyncio.wait_for(run_task, 5)
        assert not run_task.cancelled()
        assert report["status"] == "failed"
        assert sweep.alert_task is None
        notify.assert_not_awaited()
        assert child.terminated == 1
    finally:
        await sweep.close()


@pytest.mark.parametrize("kind", ["clean", "workspace", "foreign", "remaining", "critical"])
async def test_owner_alert_conditions_and_relative_path_limit(app, monkeypatch, kind):
    report = _summary(changed=3 if kind == "workspace" else 0,
                      foreign_owned=2 if kind == "foreign" else 0)
    if kind == "remaining":
        report["remaining_loose"] = [f"agents/worker/workspace/copy-{i:02}.db" for i in range(13)]
    if kind == "critical":
        report["critical_loose"] = ["identity/keys.data"]
    child = _Child(_output(report))
    _child_factory(monkeypatch, child)
    async with app.router.lifespan_context(app):
        await asyncio.wait_for(_background_task(app), 5)
        sweep = app.state.db_permission_sweep
        alert_task = getattr(sweep, "alert_task", None)
        if alert_task is not None:
            await asyncio.wait_for(alert_task, 5)
        notify = app.state.scheduler._owner_notify_callback
        if kind in {"clean", "workspace", "foreign"}:
            notify.assert_not_called()
        else:
            notify.assert_awaited_once()
            agent, text = notify.await_args.args
            assert agent == "admin"
            assert str(Path(app.state.agents._db_path).parent) not in text
            if kind == "critical":
                assert "identity/keys.data" in text
            else:
                assert "copy-09.db" in text and "copy-10.db" not in text
                assert "3" in text


@pytest.mark.parametrize("priority", [False, True])
@pytest.mark.parametrize("kind", ["timeout", "exit"])
async def test_failed_background_sweep_alerts_once_with_priority_status(
    app, monkeypatch, priority, kind
):
    prefix = "priority-done changed=0\n" if priority else ""
    child = _Child(prefix, hold=kind == "timeout", returncode=1)
    _child_factory(monkeypatch, child)
    monkeypatch.setattr(db_security, "SWEEP_TIMEOUT_SECONDS", 0.01, raising=False)
    async with app.router.lifespan_context(app):
        await asyncio.wait_for(_background_task(app), 5)
        sweep = app.state.db_permission_sweep
        await asyncio.wait_for(sweep.alert_task, 5)
        notify = app.state.scheduler._owner_notify_callback
        notify.assert_awaited_once()
        text = notify.await_args.args[1]
        assert "priority" in text.lower()
        assert ("finished" if priority else "not finished") in text.lower()


@pytest.mark.parametrize("first_failure", [False, RuntimeError("synthetic")])
@pytest.mark.parametrize("second_success", [False, True])
async def test_owner_delivery_retries_once_inside_one_boot_reservation(
    app, monkeypatch, first_failure, second_success
):
    child = _Child(_output(_summary(critical_loose=["identity/keys.data"])), hold=True)
    _child_factory(monkeypatch, child)
    calls, sleeps, logs = [], [], []

    async def notify(agent, text):
        calls.append((agent, text))
        outcome = first_failure if len(calls) == 1 else second_success
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def sleep(seconds):
        sleeps.append(seconds)
        await asyncio.sleep(0)

    app.state.scheduler._owner_notify_callback = notify
    monkeypatch.setattr(api, "_log", logs.append)
    async with app.router.lifespan_context(app):
        task = _background_task(app)
        sweep = app.state.db_permission_sweep
        sweep.sleep = sleep
        child.release.set()
        await asyncio.wait_for(task, 5)
        await asyncio.wait_for(sweep.alert_task, 5)
        assert len(calls) == 2 and calls[0] == calls[1]
        assert sleeps == [120]
        result = task.result()
        await sweep.notify_owner(result)
        assert len(calls) == 2
        message = "admin: db permission sweep owner alert was not delivered"
        assert logs.count(message) == (0 if second_success else 1)


async def test_owner_reservation_is_taken_before_delivery_waits(app, monkeypatch):
    child = _Child(_output(_summary(critical_loose=["identity/key.data"])))
    _child_factory(monkeypatch, child)
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def notify(agent, text):
        calls.append((agent, text))
        entered.set()
        await release.wait()
        return True

    app.state.scheduler._owner_notify_callback = notify
    async with app.router.lifespan_context(app):
        result = await asyncio.wait_for(_background_task(app), 5)
        sweep = app.state.db_permission_sweep
        await asyncio.wait_for(entered.wait(), 5)
        duplicate = asyncio.create_task(sweep.notify_owner(result))
        try:
            await asyncio.sleep(0)
            assert calls and len(calls) == 1
            assert duplicate.done()
        finally:
            release.set()
            await asyncio.gather(sweep.alert_task, duplicate)


@pytest.mark.parametrize("mode", [0o640, 0o604])
async def test_group_or_other_only_registered_permissions_alert_once(tmp_path, mode):
    root = tmp_path / "data"
    registered = _file(root, "registered.data", mode=mode)
    notify = AsyncMock(return_value=True)
    sweep = db_security.PermissionSweep(root, [registered], notify, lambda text: None)
    try:
        report = await asyncio.wait_for(sweep.run(), 10)
        await asyncio.wait_for(sweep.alert_task, 5)
        assert report["ok"] is True and report["critical_loose"] == ["registered.data"]
        assert report["remaining_loose"] == []
        assert registered.stat().st_mode & 0o777 == 0o600
        notify.assert_awaited_once_with(
            "admin", "Database permission sweep found files with permissions open to other users."
            ' Paths: "registered.data"',
        )
        await sweep.notify_owner(report)
        assert notify.await_count == 1
    finally:
        await sweep.close()


async def test_registered_paths_outside_data_root_keep_their_permissions(tmp_path):
    root = tmp_path / "data"
    registered = _file(root, "registered.data", mode=0o640)
    outside = _file(tmp_path, "outside/tenant/store.db", mode=0o644)
    sidecar = _file(tmp_path, "outside/tenant/store.db-wal", mode=0o644)
    notify = AsyncMock(return_value=True)
    sweep = db_security.PermissionSweep(
        root, [registered.resolve(), outside.resolve()], notify, lambda text: None,
    )
    try:
        report = await asyncio.wait_for(sweep.run(), 10)
        await asyncio.wait_for(sweep.alert_task, 5)
        assert report["ok"] is True
        assert report["status"] == "ok"
        assert report["priority_done"] is True
        assert report["critical_loose"] == ["registered.data"]
        assert report["remaining_loose"] == []
        assert registered.stat().st_mode & 0o777 == 0o600
        assert outside.stat().st_mode & 0o777 == 0o644
        assert sidecar.stat().st_mode & 0o777 == 0o644
        notify.assert_awaited_once_with(
            "admin", "Database permission sweep found files with permissions open to other users."
            ' Paths: "registered.data"',
        )
    finally:
        await sweep.close()


async def test_owner_alert_quotes_control_characters_in_relative_path(tmp_path):
    root = tmp_path / "data"
    relative = 'critical/line\n"quoted"\\escape\x1b\x85\u2028.data'
    registered = _file(root, relative, mode=0o640)
    contents = b"synthetic contents must remain private"
    registered.write_bytes(contents)
    notify = AsyncMock(return_value=True)
    sweep = db_security.PermissionSweep(root, [registered], notify, lambda text: None)
    try:
        report = await asyncio.wait_for(sweep.run(), 10)
        await asyncio.wait_for(sweep.alert_task, 5)
        assert report["ok"] is True and report["critical_loose"] == [relative]
        assert registered.stat().st_mode & 0o777 == 0o600
        notify.assert_awaited_once()
        agent, text = notify.await_args.args
        assert agent == "admin"
        quoted = r'"critical/line\n\"quoted\"\\escape\u001b\u0085\u2028.data"'
        assert text == (
            "Database permission sweep found files with permissions open to other users. Paths: "
            + quoted
        )
        assert all(character not in text for character in ("\n", "\x1b", "\x85", "\u2028"))
        assert str(root) not in text and contents.decode() not in text
        assert relative not in text
    finally:
        await sweep.close()
