#!/usr/bin/env bash
set -euo pipefail

VPS="root@66.94.127.210"
REMOTE_DIR="/opt/pinkybot-oauth"
SERVICE="pinkybot-oauth"

echo "==> Syncing files to VPS..."
rsync -a "$(dirname "$0")/" "$VPS:$REMOTE_DIR/"

echo "==> Installing dependencies..."
ssh "$VPS" "pip3 install -r $REMOTE_DIR/requirements.txt -q"

echo "==> Writing systemd service..."
ssh "$VPS" bash <<'ENDSSH'
SERVICE_FILE="/etc/systemd/system/pinkybot-oauth.service"
if [ ! -f "$SERVICE_FILE" ]; then
cat > "$SERVICE_FILE" <<'EOF'
[Unit]
Description=PinkyBot OAuth Proxy
After=network.target

[Service]
WorkingDirectory=/opt/pinkybot-oauth
ExecStart=/usr/bin/python3 -m uvicorn main:app --host 127.0.0.1 --port 8890
Restart=always
EnvironmentFile=/etc/pinkybot-oauth.env

[Install]
WantedBy=multi-user.target
EOF
echo "Service file created."
else
echo "Service file already exists, skipping."
fi
ENDSSH

echo "==> Enabling and restarting service..."
ssh "$VPS" "systemctl daemon-reload && systemctl enable $SERVICE && systemctl restart $SERVICE"

echo "==> Status:"
ssh "$VPS" "systemctl status $SERVICE --no-pager -l"
