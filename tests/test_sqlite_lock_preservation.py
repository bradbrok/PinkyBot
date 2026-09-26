"""Process-level regressions for SQLite locks across application startup."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

# Run the application in an isolated process: a broken implementation can leave
# an mmap pointing at truncated shared memory. Never risk the pytest process.
_SCENARIO = r'''
import asyncio
import ctypes
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

from pinky_daemon import api

PROBE = r"""
import ctypes, fcntl, json, os, sys
if sys.platform == 'darwin':
    fields = [('start', ctypes.c_longlong), ('length', ctypes.c_longlong),
              ('pid', ctypes.c_int), ('type', ctypes.c_short), ('whence', ctypes.c_short)]
else:
    fields = [('type', ctypes.c_short), ('whence', ctypes.c_short),
              ('start', ctypes.c_longlong), ('length', ctypes.c_longlong),
              ('pid', ctypes.c_int)]
class Lock(ctypes.Structure):
    _fields_ = fields
result = []
for suffix, offset, length in [('', 1073741826, 510), ('-shm', 128, 1)]:
    fd = os.open(sys.argv[1] + suffix, os.O_RDONLY)
    try:
        lock = Lock()
        lock.type, lock.whence, lock.start, lock.length = fcntl.F_WRLCK, os.SEEK_SET, offset, length
        held = Lock.from_buffer_copy(fcntl.fcntl(fd, fcntl.F_GETLK, bytes(lock)))
        result.append([held.type == fcntl.F_RDLCK, held.pid])
    finally:
        os.close(fd)
print(json.dumps(result))
"""

def child(code, *args):
    p = subprocess.run([sys.executable, '-c', code, *map(str, args)],
                       capture_output=True, text=True, timeout=30)
    assert p.returncode == 0, p.stderr
    return p.stdout.strip()

def assert_locks(path):
    locks = json.loads(child(PROBE, path))
    assert locks == [[True, os.getpid()], [True, os.getpid()]], (path, locks)

async def run():
    root, scenario = Path(sys.argv[1]), sys.argv[2]
    os.chdir(root)
    os.environ['PINKY_LOG_ROTATION'] = 'off'
    api.SHARED_MCP_ENABLED = False
    app = api.create_api(db_path=str(root / 'conversations.db'), default_working_dir=str(root))
    records = {r.logical_name: r for r in app.state.store_catalog.snapshot()}
    paths = [Path(records[n].resolved_path) for n in ('tasks', 'sessions')]
    for path in paths:
        assert_locks(path)  # Positive control before the startup sweep.
        for suffix in ("", "-wal", "-shm"):
            os.chmod(str(path) + suffix, 0o644)
    async with app.router.lifespan_context(app):
        if scenario == 'locks':
            for path in paths:
                assert_locks(path)
                for suffix in ('', '-wal', '-shm'):
                    assert Path(str(path) + suffix).stat().st_mode & 0o777 == 0o600
        elif scenario == 'orphan':
            db = app.state.session_store._db
            db.execute('CREATE TABLE lock_probe(value INTEGER)')
            db.execute('INSERT INTO lock_probe VALUES (1)')
            db.commit()
            path = paths[1]
            child("import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); "
                  "c.execute('SELECT * FROM lock_probe').fetchall(); c.close()", path)
            assert all(Path(str(path) + suffix).exists() for suffix in ('-wal', '-shm')), 'sidecars unlinked'
            db.execute('INSERT INTO lock_probe VALUES (2)')
            db.commit()
            count = child("import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); "
                          "print(c.execute('SELECT count(*) FROM lock_probe').fetchone()[0]); c.close()", path)
            assert count == '2', count
        elif scenario == 'readonly':
            for path in paths:
                # Read bytes in the child too: even an inspection close here
                # would drop this process's own DMS lock. Compare the two
                # WAL-index headers, excluding read marks and their mtime updates.
                result = child("""
import hashlib, json, os, sqlite3, sys
p = sys.argv[1] + '-shm'
def snapshot():
    s = os.stat(p)
    with open(p, 'rb') as stream:
        header = stream.read(96).hex()
    return [s.st_ino, s.st_size, header]
before = snapshot()
c = sqlite3.connect('file:' + sys.argv[1] + '?mode=ro', uri=True)
c.execute('SELECT count(*) FROM sqlite_master').fetchone()
c.close()
print(json.dumps([before, snapshot()]))
""", path)
                before, after = json.loads(result)
                assert before == after, (path, before, after)
                assert_locks(path)

asyncio.run(run())
'''


@pytest.mark.parametrize("scenario", ["locks", "orphan", "readonly"])
def test_real_startup_preserves_sqlite_locks(tmp_path, scenario):
    result = subprocess.run(
        [sys.executable, "-c", _SCENARIO, str(tmp_path), scenario],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=90,
        env={**os.environ, "PINKY_SHARED_MCP": "0"},
    )
    assert result.returncode == 0, result.stdout + result.stderr


def _probe(path):
    # Reuse the independent probe from the RED subprocess scenario, not the
    # implementation under test.
    namespace = {}
    exec(_SCENARIO.split("async def run():")[0], namespace)
    import json

    return json.loads(namespace["child"](namespace["PROBE"], path))


@pytest.mark.parametrize("kind", ["bearer", "signer"])
def test_identity_store_permissions_and_locks(tmp_path, kind):
    from pinky_identity.bearer_tokens import BearerTokenStore
    from pinky_identity.keystore import DeviceKey
    from pinky_identity.signer_store import EncryptedSignerStore

    path = tmp_path / "identity.db"
    kwargs = {"db_path": path}
    cls = BearerTokenStore
    if kind == "signer":
        cls = EncryptedSignerStore
        kwargs["device_key"] = DeviceKey.from_bytes(bytes(range(32)))
    with cls(**kwargs):
        assert _probe(path) == [[True, os.getpid()], [True, os.getpid()]]
        for suffix in ("", "-wal", "-shm"):
            assert Path(str(path) + suffix).stat().st_mode & 0o777 == 0o600


def test_hardening_refuses_registered_main_sidecars_and_aliases(tmp_path, monkeypatch):
    from pinky_daemon.store_catalog import BoundSQLiteFile, DaemonStoreCatalog
    from pinky_daemon.task_store import TaskStore
    from pinky_identity.fs_security import harden_secret_file
    from pinky_identity.live_sqlite import LiveSQLiteFileError

    path = tmp_path / "tasks.db"
    catalog = DaemonStoreCatalog(expected_root=tmp_path)
    store = TaskStore(str(path), catalog=catalog)
    aliases = []
    for suffix in ("", "-wal", "-shm"):
        alias = tmp_path / ("alias" + suffix)
        os.link(str(path) + suffix, alias)
        aliases.append(alias)
    try:
        with monkeypatch.context() as patch:

            def forbidden(*args, **kwargs):
                pytest.fail("guard opened a registered inode")

            patch.setattr(os, "open", forbidden)
            for target in [path, *aliases]:
                with pytest.raises(LiveSQLiteFileError):
                    harden_secret_file(target)
                with pytest.raises(LiveSQLiteFileError):
                    BoundSQLiteFile.open(target)
        assert _probe(path) == [[True, os.getpid()], [True, os.getpid()]]
    finally:
        store.close()
        catalog.close()
    # Explicit close removes registration so offline hardening works again.
    harden_secret_file(path)


@pytest.mark.parametrize("existing", [False, True])
def test_update_preflight_preserves_locks_and_checks_authority(tmp_path, existing):
    import sqlite3

    from pinky_daemon.store_catalog import (
        DaemonStoreCatalog,
        StoreIntegrityTarget,
        StorePathAuthorityError,
    )
    from pinky_daemon.task_store import TaskStore

    path = tmp_path / "tasks.db"
    if existing:
        with sqlite3.connect(path) as seed:
            seed.execute("PRAGMA journal_mode=WAL")
            seed.execute("CREATE TABLE seed(value)")
        seed.close()
    target = StoreIntegrityTarget("tasks", str(path), journal_mode="wal")
    catalog = DaemonStoreCatalog(expected_root=tmp_path, manifest={"tasks": target})
    catalog.preflight_integrity([target])
    store = TaskStore(str(path), catalog=catalog)
    try:
        catalog.preflight_ancestor_chains()
        assert _probe(path) == [[True, os.getpid()], [True, os.getpid()]]
        original_mode = tmp_path.stat().st_mode & 0o777
        tmp_path.chmod(0o777)
        try:
            with pytest.raises(StorePathAuthorityError):
                catalog.preflight_ancestor_chains()
        finally:
            tmp_path.chmod(original_mode)
        assert _probe(path) == [[True, os.getpid()], [True, os.getpid()]]
    finally:
        store.close()
        catalog.close()


def test_missing_locks_log_critical_and_mark_health(tmp_path, caplog):
    from pinky_daemon.sqlite_lock_check import check_sqlite_locks
    from pinky_daemon.task_store import TaskStore

    path = tmp_path / "tasks.db"
    store = TaskStore(str(path))
    try:
        assert check_sqlite_locks([("tasks", str(path))])["healthy"]
        os.close(os.open(path, os.O_RDONLY))
        os.close(os.open(str(path) + "-shm", os.O_RDONLY))
        health = check_sqlite_locks([("tasks", str(path))])
        assert health == {"healthy": False, "checked": 1, "missing": ["tasks:SHARED", "tasks:DMS"]}
        critical = [r for r in caplog.records if r.levelname == "CRITICAL"]
        assert len(critical) == 1 and "tasks" in critical[0].message
    finally:
        store.close()


@pytest.mark.parametrize("failure", ["timeout", "exit"])
def test_permission_child_failure_is_nonfatal(tmp_path, monkeypatch, caplog, failure):
    from pinky_daemon import db_security

    error = (
        subprocess.TimeoutExpired("child", 1)
        if failure == "timeout"
        else subprocess.CalledProcessError(1, "child")
    )

    def fail(*args, **kwargs):
        assert kwargs["timeout"] > 0
        raise error

    monkeypatch.setattr(subprocess, "run", fail)
    assert db_security.sweep_db_permissions_in_child(tmp_path) == 0
    assert "permission sweep failed" in caplog.text


@pytest.mark.parametrize("route", ["document", "photo", "video", "animation"])
@pytest.mark.parametrize("suffix", [".db", ".db-wal", ".db-shm", ".db-journal", "alias"])
async def test_broker_refuses_database_without_open(tmp_path, monkeypatch, route, suffix):
    import builtins

    from starlette.requests import Request

    from pinky_daemon import api

    monkeypatch.chdir(tmp_path)
    app = api.create_api(db_path=str(tmp_path / "conversations.db"))
    path = tmp_path / ("attachment" + suffix)
    if suffix == "alias":
        record = next(r for r in app.state.store_catalog.snapshot() if r.logical_name == "tasks")
        os.link(record.resolved_path, path)

    def forbidden(*args, **kwargs):
        pytest.fail("broker opened a database attachment")

    try:
        with monkeypatch.context() as patch:
            patch.setattr(builtins, "open", forbidden)
            patch.setattr(os, "open", forbidden)
            # Exercise the route itself without external transport or auth setup.
            endpoint = next(
                r.endpoint for r in app.routes if getattr(r, "path", "") == "/broker/send-" + route
            )
            from fastapi import HTTPException

            with pytest.raises(HTTPException) as error:
                await endpoint(
                    {"agent_name": "test", "chat_id": "123", "file_path": str(path)},
                    Request({"type": "http", "headers": []}),
                )
            assert error.value.status_code == 400
            assert "SQLite" in error.value.detail
    finally:
        app.state.store_catalog.shutdown(deadline_seconds=5)


async def test_boot_lock_health_is_exposed(tmp_path, monkeypatch):
    from pinky_daemon import api

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(api, "SHARED_MCP_ENABLED", False)
    app = api.create_api(db_path=str(tmp_path / "conversations.db"))
    async with app.router.lifespan_context(app):
        endpoint = next(
            r.endpoint for r in app.routes if getattr(r, "path", "") == "/system/health"
        )
        status = await endpoint()
        assert status["sqlite_locks"]["healthy"] is True
        assert status["sqlite_locks"]["checked"] >= 2


async def test_boot_reports_dropped_locks_on_health_surface(tmp_path, monkeypatch, caplog):
    from pinky_daemon import api, db_security

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(api, "SHARED_MCP_ENABLED", False)
    app = api.create_api(db_path=str(tmp_path / "conversations.db"))
    path = next(
        r.resolved_path for r in app.state.store_catalog.snapshot() if r.logical_name == "tasks"
    )
    sweep = db_security.sweep_db_permissions_in_child

    def drop_after_sweep(root):
        result = sweep(root)
        os.close(os.open(path, os.O_RDONLY))
        os.close(os.open(path + "-shm", os.O_RDONLY))
        return result

    monkeypatch.setattr(db_security, "sweep_db_permissions_in_child", drop_after_sweep)
    async with app.router.lifespan_context(app):
        endpoint = next(
            r.endpoint for r in app.routes if getattr(r, "path", "") == "/system/health"
        )
        health = (await endpoint())["sqlite_locks"]
        assert health["healthy"] is False
        assert {"tasks:SHARED", "tasks:DMS"} <= set(health["missing"])
        critical = [r for r in caplog.records if r.levelname == "CRITICAL"]
        assert len(critical) == 1 and "tasks" in critical[0].message


def test_entry_point_restricts_creation_mode_before_configuration(monkeypatch):
    from pinky_daemon import __main__ as entry

    class StopBeforeLaunchError(Exception):
        pass

    def configuration():
        assert os.umask(0o077) == 0o077
        raise StopBeforeLaunchError

    previous = os.umask(0o022)
    try:
        monkeypatch.setattr(entry, "_install_faulthandler", lambda: None)
        monkeypatch.setattr(entry, "_load_dotenv", configuration)
        with pytest.raises(StopBeforeLaunchError):
            entry.main()
    finally:
        os.umask(previous)
