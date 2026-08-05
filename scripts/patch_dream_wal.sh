#!/bin/bash
# patch_dream_wal.sh — Fix dream_runner.py WAL -> TRUNCATE (idempotent)
# Mirrors the conversations_agents.db fix (#797/#220): WAL -shm mmap goes stale
# under long-lived connections causing "disk I/O error" on commit.
set -e

DREAM_RUNNER="/home/pinky/.pinkybot/src/pinky_daemon/dream_runner.py"

if grep -q 'PRAGMA journal_mode=TRUNCATE' "$DREAM_RUNNER"; then
    echo "  dream_wal_fix: already applied, skipping"
    exit 0
fi

python3 /tmp/fix_dream_wal.py
echo "  dream_wal_fix: applied"
