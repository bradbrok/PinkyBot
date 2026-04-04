#!/usr/bin/env bash
set -euo pipefail

VPS="root@66.94.127.210"
ENV_FILE="/etc/pinkybot-oauth.env"

echo "==> Writing placeholder env file on VPS..."
ssh "$VPS" bash <<ENDSSH
if [ -f "$ENV_FILE" ]; then
  echo "WARNING: $ENV_FILE already exists — not overwriting."
else
  cat > "$ENV_FILE" <<'EOF'
GOOGLE_CLIENT_ID=your-client-id-here
GOOGLE_CLIENT_SECRET=your-client-secret-here
EOF
  chmod 600 "$ENV_FILE"
  echo "Created $ENV_FILE"
fi
ENDSSH

echo ""
echo "Edit $ENV_FILE on the VPS and restart the service:"
echo "  ssh $VPS 'nano $ENV_FILE && systemctl restart pinkybot-oauth'"
