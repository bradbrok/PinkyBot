#!/bin/bash
# Setup cron job for AIena heartbeat
# Run every 6 hours at: 0:00, 6:00, 12:00, 18:00

SCRIPT="/home/pinky/.pinkybot/scripts/aiena_heartbeat.py"

# Check if already exists
if crontab -l 2>/dev/null | grep -q "aiena_heartbeat"; then
    echo "Cron job already exists, skipping..."
    exit 0
fi

# Create cron job
CRON_ENTRY="0 */6 * * * /usr/bin/python3 $SCRIPT"
(crontab -l 2>/dev/null; echo "$CRON_ENTRY") | crontab -

echo "Cron job installed successfully"
echo "The heartbeat will run every 6 hours (0:00, 6:00, 12:00, 18:00)"
echo ""
echo "Current crontab:"
crontab -l | grep aiena || true
