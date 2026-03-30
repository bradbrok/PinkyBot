#!/bin/bash
# Restart PinkyBot daemon — always from the correct directory
set -e

PINKY_DIR="/Users/oleg/.pulse-v2/agents/misha/workspace/PinkyBot"
cd "$PINKY_DIR"

# Kill existing
pkill -f "pinky_daemon.*8888" 2>/dev/null || true
sleep 1

# Start fresh
nohup .venv/bin/python -m pinky_daemon --mode api --port 8888 --host 0.0.0.0 --working-dir . > /tmp/pinkybot.log 2>&1 &
echo "PinkyBot started (PID: $!)"
