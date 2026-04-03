#!/bin/bash
# Restart PinkyBot services — API server + poll daemon
set -e

PINKY_DIR="/Users/oleg/PinkyBot"
cd "$PINKY_DIR"

LOG_DIR="$PINKY_DIR/logs"
mkdir -p "$LOG_DIR"

API_PORT=8888

# Graceful stop by process name
echo "Stopping services..."
pkill -TERM -f "pinky_daemon.*--mode api" 2>/dev/null || true
pkill -TERM -f "pinky_daemon.*--mode poll" 2>/dev/null || true
sleep 2

# Force-kill by process name
pkill -9 -f "pinky_daemon.*--mode api" 2>/dev/null || true
pkill -9 -f "pinky_daemon.*--mode poll" 2>/dev/null || true

# Nuclear option: kill whatever is holding the API port
PORT_PIDS=$(lsof -ti ":$API_PORT" 2>/dev/null || true)
if [ -n "$PORT_PIDS" ]; then
    echo "Force-killing processes on port $API_PORT: $PORT_PIDS"
    echo "$PORT_PIDS" | xargs kill -9 2>/dev/null || true
fi

sleep 1

# Verify port is free
if lsof -ti ":$API_PORT" >/dev/null 2>&1; then
    echo "ERROR: Port $API_PORT still in use after kill attempts. Aborting." >&2
    exit 1
fi

# Start API server — prefer launchd if available (avoids duplicate-process conflicts)
LAUNCHD_LABEL="com.pinkybot.daemon"
if launchctl list "$LAUNCHD_LABEL" &>/dev/null 2>&1; then
    echo "Restarting via launchd ($LAUNCHD_LABEL)..."
    launchctl kickstart -k "gui/$(id -u)/$LAUNCHD_LABEL"
    echo "API server restarted via launchd."
else
    nohup .venv/bin/python -m pinky_daemon --mode api --port $API_PORT --host 0.0.0.0 --working-dir . \
        >> "$LOG_DIR/api.log" 2>&1 &
    echo "API server started (PID: $!)"

    # Start poll daemon
    nohup .venv/bin/python -m pinky_daemon --mode poll --config pinky.yaml --working-dir . \
        >> "$LOG_DIR/poll.log" 2>&1 &
    echo "Poll daemon started (PID: $!)"
fi
