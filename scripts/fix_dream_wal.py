#!/usr/bin/env python3
"""
Fix dream_runner.py WAL -> TRUNCATE mode.
Same bug as conversations_agents.db (#797/#220):
WAL -shm mmap goes stale under long-lived daemon connection -> disk I/O error on commit.
"""
import sys

TARGET = "/home/pinky/.pinkybot/src/pinky_daemon/dream_runner.py"

OLD = '        self._db.execute("PRAGMA journal_mode=WAL")'

NEW = '''        self._db.execute("PRAGMA busy_timeout=5000")
        # Switch to rollback (TRUNCATE) mode — mirrors _configure_agents_db_connection
        # fix for conversations_agents.db (#797/#220). WAL -shm mmap goes stale
        # under long-lived daemon connections and causes "disk I/O error" on commit.
        # TRUNCATE journal has no -shm at all, so the daemon never maps it.
        try:
            _cur = self._db.execute("PRAGMA journal_mode").fetchone()
            if _cur and str(_cur[0]).lower() == "wal":
                self._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception:
            pass
        for _attempt in range(6):
            _row = self._db.execute("PRAGMA journal_mode=TRUNCATE").fetchone()
            if _row and str(_row[0]).lower() == "truncate":
                break
            import time as _time
            _time.sleep(0.2 * (_attempt + 1))'''

with open(TARGET, "r") as f:
    content = f.read()

if OLD not in content:
    print(f"ERROR: old string not found in {TARGET}")
    sys.exit(1)

if "PRAGMA journal_mode=TRUNCATE" in content:
    print("INFO: fix already applied, skipping")
    sys.exit(0)

new_content = content.replace(OLD, NEW, 1)

with open(TARGET, "w") as f:
    f.write(new_content)

print(f"OK: dream_runner.py patched (WAL -> TRUNCATE) at line 97")

# Also clean up any stale -shm/-wal files
import glob, os
for pattern in [
    "/home/pinky/.pinkybot/data/dream_state.db-shm",
    "/home/pinky/.pinkybot/data/dream_state.db-wal",
    "/home/pinky/.pinkybot/data/agents/*/data/dream_state.db-shm",
    "/home/pinky/.pinkybot/data/agents/*/data/dream_state.db-wal",
]:
    for f in glob.glob(pattern):
        try:
            os.remove(f)
            print(f"Removed stale WAL file: {f}")
        except Exception as e:
            print(f"Could not remove {f}: {e}")

print("Done.")
