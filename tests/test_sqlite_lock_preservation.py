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
    async with app.router.lifespan_context(app):
        if scenario == 'locks':
            for path in paths:
                assert_locks(path)
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
                # would drop this process's own DMS lock.
                result = child("""
import hashlib, json, os, sqlite3, sys
p = sys.argv[1] + '-shm'
def snapshot():
    s = os.stat(p)
    with open(p, 'rb') as f:
        digest = hashlib.sha256(f.read()).hexdigest()
    return [s.st_ino, s.st_size, s.st_mtime_ns, digest]
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
