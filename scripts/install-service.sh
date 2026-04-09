#!/bin/bash
# Install PinkyBot as a managed service (auto-restart on crash/update)
# Supports macOS (launchctl) and Linux (systemd)
#
# Usage:
#   bash scripts/install-service.sh          # auto-detect (user service on desktop, system on headless)
#   bash scripts/install-service.sh --system # force system-level service (needs sudo, good for servers/RPi)
#   bash scripts/install-service.sh --user   # force user-level service (needs DBUS session)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PINKY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
PINKY_USER="$(whoami)"

OS="$(uname -s)"
MODE="${1:-auto}"

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

        if [ ! -f "$UNIT_SRC" ]; then
            echo "Error: $UNIT_SRC not found"
            exit 1
        fi

        mkdir -p "$PINKY_DIR/logs"

        # Auto-detect: use system service if no DBUS session (headless/SSH/RPi)
        if [ "$MODE" = "auto" ]; then
            if [ -n "${DBUS_SESSION_BUS_ADDRESS:-}" ]; then
                MODE="user"
            else
                MODE="system"
            fi
            echo "Auto-detected mode: $MODE"
        fi

        if [ "$MODE" = "system" ]; then
            # System-level service — works on headless servers, RPi, SSH
            UNIT_DST="/etc/systemd/system/pinkybot.service"

            # Build system unit with correct paths and user
            cat > /tmp/pinkybot.service <<UNITEOF
[Unit]
Description=PinkyBot Daemon
After=network.target

[Service]
Type=simple
User=$PINKY_USER
WorkingDirectory=$PINKY_DIR
EnvironmentFile=$PINKY_DIR/.env
ExecStart=$PINKY_DIR/.venv/bin/python -m pinky_daemon --mode api --port 8888 --host 0.0.0.0 --working-dir .
Restart=always
RestartSec=5
StandardOutput=append:$PINKY_DIR/logs/api.log
StandardError=append:$PINKY_DIR/logs/api.log

[Install]
WantedBy=multi-user.target
UNITEOF

            # Need sudo for system service
            if [ "$(id -u)" -ne 0 ]; then
                echo "Installing system service (needs sudo)..."
                sudo cp /tmp/pinkybot.service "$UNIT_DST"
                sudo systemctl daemon-reload
                sudo systemctl enable --now pinkybot
            else
                cp /tmp/pinkybot.service "$UNIT_DST"
                systemctl daemon-reload
                systemctl enable --now pinkybot
            fi
            rm -f /tmp/pinkybot.service

            echo "Installed: $UNIT_DST (system service)"
            echo "PinkyBot will auto-start on boot and restart on crash."
            echo ""
            echo "Commands:"
            echo "  sudo systemctl status pinkybot     # check status"
            echo "  sudo systemctl restart pinkybot    # restart"
            echo "  sudo systemctl stop pinkybot       # stop (will restart)"
            echo "  sudo systemctl disable pinkybot    # disable permanently"
            echo "  journalctl -u pinkybot -f          # follow logs"

        else
            # User-level service — needs DBUS session (desktop Linux)
            UNIT_DIR="$HOME/.config/systemd/user"
            UNIT_DST="$UNIT_DIR/pinkybot.service"

            mkdir -p "$UNIT_DIR"

            # Enable linger so user services survive logout
            if command -v loginctl &>/dev/null; then
                sudo loginctl enable-linger "$PINKY_USER" 2>/dev/null || true
            fi

            # Update paths in unit file to match actual install location
            sed "s|/opt/pinkybot|$PINKY_DIR|g" "$UNIT_SRC" > "$UNIT_DST"

            systemctl --user daemon-reload
            systemctl --user enable --now pinkybot

            echo "Installed: $UNIT_DST (user service)"
            echo "PinkyBot will auto-start and restart on crash."
            echo ""
            echo "Commands:"
            echo "  systemctl --user status pinkybot    # check status"
            echo "  systemctl --user restart pinkybot   # restart"
            echo "  systemctl --user stop pinkybot      # stop (will restart)"
            echo "  systemctl --user disable pinkybot   # disable permanently"
        fi
        ;;

    *)
        echo "Unsupported OS: $OS"
        echo "Supported: Darwin (macOS), Linux"
        exit 1
        ;;
esac
