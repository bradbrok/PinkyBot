#!/bin/bash
# watchdog_agents.sh — Riavvia agenti offline automaticamente
# Cron: */5 * * * * /home/pinky/.pinkybot/scripts/watchdog_agents.sh >> /tmp/watchdog_agents.log 2>&1

AGENTS=("tutortesi")
DAEMON_URL="http://localhost:8888"
SECRET="XHZnKJmDbWy2B6q6lD4rAch8AJpIhMb7pxt8OmeeQ70="
LOG_PREFIX="[$(date '+%Y-%m-%d %H:%M:%S')]"

# 1. Assicura che tmux server sia attivo
if ! tmux -L default list-sessions &>/dev/null; then
    echo "$LOG_PREFIX tmux server morto — riavvio"
    tmux -L default new-session -d -s watchdog_boot
fi

# 2. Controlla ogni agente
for AGENT in "${AGENTS[@]}"; do
    # Verifica session tmux
    if ! tmux -L default has-session -t "pinky-$AGENT" 2>/dev/null; then
        echo "$LOG_PREFIX $AGENT: sessione tmux mancante — wake via API"

        # Genera HMAC headers e chiama wake
        RESULT=$(cd /home/pinky/.pinkybot && /home/pinky/.pinkybot/.venv/bin/python3 -c "
import requests, sys
sys.path.insert(0, 'src')
from pinky_self.server import build_internal_auth_headers
secret = '$SECRET'
headers = build_internal_auth_headers(secret, agent_name='satoshi', method='POST', path='/agents/$AGENT/wake')
try:
    r = requests.post('$DAEMON_URL/agents/$AGENT/wake', headers=headers, timeout=15)
    print(r.status_code, r.json().get('connected', False))
except Exception as e:
    print('ERROR', e)
" 2>&1)
        echo "$LOG_PREFIX $AGENT wake result: $RESULT"
    else
        echo "$LOG_PREFIX $AGENT: OK (sessione attiva)"
    fi
done
