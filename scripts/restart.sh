#!/bin/bash
# Restart PinkyBot services — API server + poll daemon
set -e

PINKY_DIR="/Users/oleg/PinkyBot"
cd "$PINKY_DIR"

LOG_DIR="$PINKY_DIR/logs"
mkdir -p "$LOG_DIR"

# Graceful stop
echo "Stopping services..."
pkill -TERM -f "pinky_daemon.*--mode api" 2>/dev/null || true
pkill -TERM -f "pinky_daemon.*--mode poll" 2>/dev/null || true
sleep 3
pkill -9 -f "pinky_daemon.*--mode api" 2>/dev/null || true
pkill -9 -f "pinky_daemon.*--mode poll" 2>/dev/null || true
sleep 1

# Start API server
nohup .venv/bin/python -m pinky_daemon --mode api --port 8888 --host 0.0.0.0 --working-dir . \
    >> "$LOG_DIR/api.log" 2>&1 &
echo "API server started (PID: $!)"

# Start poll daemon
nohup .venv/bin/python -m pinky_daemon --mode poll --config pinky.yaml --working-dir . \
    >> "$LOG_DIR/poll.log" 2>&1 &
echo "Poll daemon started (PID: $!)"
