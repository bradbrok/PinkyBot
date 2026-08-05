#!/usr/bin/env bash
# safe_pytest.sh — Single-instance pytest wrapper
# Prevents parallel pytest runs that can saturate RAM + swap.
#
# Usage: safe_pytest.sh [pytest args...]
# Example: safe_pytest.sh tests/ -x -q
#
# Lock behavior:
#   - If another pytest is running → exit 1 immediately (no wait, no retry)
#   - Lock file: /tmp/pinkybot_pytest.lock
#   - PID checked to detect stale locks from crashed processes

LOCKFILE="/tmp/pinkybot_pytest.lock"

# --- Lock acquisition ---
acquire_lock() {
    if [ -f "$LOCKFILE" ]; then
        LOCKED_PID=$(cat "$LOCKFILE" 2>/dev/null)
        if kill -0 "$LOCKED_PID" 2>/dev/null; then
            echo "[safe_pytest] BLOCKED: pytest already running (PID $LOCKED_PID)" >&2
            echo "[safe_pytest] Kill it first: kill $LOCKED_PID" >&2
            exit 1
        else
            echo "[safe_pytest] Stale lock detected (PID $LOCKED_PID gone), removing." >&2
            rm -f "$LOCKFILE"
        fi
    fi

    # Atomic write using shell PID
    echo $$ > "$LOCKFILE"

    # Double-check we actually got the lock (race condition guard)
    WRITTEN_PID=$(cat "$LOCKFILE" 2>/dev/null)
    if [ "$WRITTEN_PID" != "$$" ]; then
        echo "[safe_pytest] Race condition — another process grabbed the lock. Aborting." >&2
        exit 1
    fi
}

release_lock() {
    rm -f "$LOCKFILE"
}

# Always release on exit
trap release_lock EXIT

# --- Memory check (abort if system is already under stress) ---
check_memory() {
    AVAIL_KB=$(awk '/MemAvailable/ {print $2}' /proc/meminfo 2>/dev/null || echo 0)
    AVAIL_MB=$((AVAIL_KB / 1024))
    SWAP_TOTAL=$(awk '/SwapTotal/ {print $2}' /proc/meminfo 2>/dev/null || echo 1)
    SWAP_FREE=$(awk '/SwapFree/ {print $2}' /proc/meminfo 2>/dev/null || echo 1)
    SWAP_USED_PCT=$(( (SWAP_TOTAL - SWAP_FREE) * 100 / (SWAP_TOTAL + 1) ))

    if [ "$AVAIL_MB" -lt 512 ]; then
        echo "[safe_pytest] ABORT: Only ${AVAIL_MB}MB RAM available (minimum 512MB required)." >&2
        exit 1
    fi

    if [ "$SWAP_USED_PCT" -gt 80 ]; then
        echo "[safe_pytest] ABORT: Swap is ${SWAP_USED_PCT}% full. Wait for system to recover." >&2
        exit 1
    fi

    echo "[safe_pytest] Memory OK: ${AVAIL_MB}MB available, swap ${SWAP_USED_PCT}% used."
}

# --- Main ---
acquire_lock
check_memory

echo "[safe_pytest] Starting pytest (PID $$, lock: $LOCKFILE)"
echo "[safe_pytest] Args: $*"
echo "[safe_pytest] Time: $(date '+%Y-%m-%d %H:%M:%S')"

# Run pytest with memory-safe defaults:
# --forked not used (requires pytest-forked and adds overhead)
# -p no:randomly prevents test order randomization overhead
python3 -m pytest "$@"
EXIT_CODE=$?

echo "[safe_pytest] pytest exited with code $EXIT_CODE"
exit $EXIT_CODE
