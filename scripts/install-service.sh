#!/bin/bash
# Install PinkyBot as a managed service (auto-restart on crash/update)
# Supports macOS (launchctl) and Linux (systemd --user)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PINKY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

OS="$(uname -s)"

case "$OS" in
    Darwin)
        PLIST_SRC="$SCRIPT_DIR/launchctl/com.pinkybot.daemon.plist"
        PLIST_DST="$HOME/Library/LaunchAgents/com.pinkybot.daemon.plist"

        if [ ! -f "$PLIST_SRC" ]; then
            echo "Error: $PLIST_SRC not found"
            exit 1
        fi

        # Update paths in plist to match actual install location
        sed "s|/Users/oleg/PinkyBot|$PINKY_DIR|g" "$PLIST_SRC" > "$PLIST_DST"

        mkdir -p "$PINKY_DIR/logs"

        # Unload if already loaded
        launchctl unload "$PLIST_DST" 2>/dev/null || true

        # Load and start
        launchctl load -w "$PLIST_DST"

        echo "Installed: $PLIST_DST"
        echo "PinkyBot will auto-start and restart on crash."
        echo ""
        echo "Commands:"
        echo "  launchctl start com.pinkybot.daemon    # start now"
        echo "  launchctl stop com.pinkybot.daemon     # stop (will restart)"
        echo "  launchctl unload -w $PLIST_DST         # disable permanently"
        ;;

    Linux)
        UNIT_SRC="$SCRIPT_DIR/systemd/pinkybot.service"
        UNIT_DIR="$HOME/.config/systemd/user"
        UNIT_DST="$UNIT_DIR/pinkybot.service"

        if [ ! -f "$UNIT_SRC" ]; then
            echo "Error: $UNIT_SRC not found"
            exit 1
        fi

        mkdir -p "$UNIT_DIR"
        mkdir -p "$PINKY_DIR/logs"

        # Update paths in unit file to match actual install location
        sed "s|/opt/pinkybot|$PINKY_DIR|g" "$UNIT_SRC" > "$UNIT_DST"

        systemctl --user daemon-reload
        systemctl --user enable --now pinkybot

        echo "Installed: $UNIT_DST"
        echo "PinkyBot will auto-start and restart on crash."
        echo ""
        echo "Commands:"
        echo "  systemctl --user status pinkybot    # check status"
        echo "  systemctl --user restart pinkybot   # restart"
        echo "  systemctl --user stop pinkybot      # stop (will restart)"
        echo "  systemctl --user disable pinkybot   # disable permanently"
        ;;

    *)
        echo "Unsupported OS: $OS"
        echo "Supported: Darwin (macOS), Linux"
        exit 1
        ;;
esac
