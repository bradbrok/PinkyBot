#!/bin/bash
# #52 garmin-givemydata — watcher enrollment MFA.
#
# Sostituisce il polling LLM ogni 30 min: qui il check e' shell (~2s) e
# sveglia l'agente Engineer via webhook SOLO quando i token compaiono.
#
# La VPS non puo' raggiungere il daemon (webhook su localhost:8888) e il
# daemon non va esposto su internet, quindi il poller vive lato daemon:
# il costo si sposta da un turno Opus a un comando shell.
#
# Idempotente: dopo il primo fire crea un sentinel e ogni run successiva
# esce subito. Per riarmare: rm -f "$SENTINEL"

set -uo pipefail

WEBHOOK_URL="http://localhost:8888/hooks/aGWdqL3a4tOfz7UdCQtwFK2LFHbB7r-Jz9o2FciPLMo"
SENTINEL="/home/pinky/.pinkybot/data/garmin_enrollment_fired"
LOG="/home/pinky/.pinkybot/logs/garmin_enrollment_watch.log"
# Prova di vita: sul percorso normale (token assenti) lo script esce muto, e
# "watcher vivo" diventa indistinguibile da "cron non lo esegue piu'". Questo
# file viene riscritto ad ogni check riuscito: se il suo mtime e' vecchio di
# piu' di ~20 min, il watcher e' morto e l'attesa non finirebbe mai.
LASTOK="/home/pinky/.pinkybot/data/garmin_enrollment_watch_lastok"

JUMP_HOST="ziomik@195.32.122.60"
JUMP_PORT=23
JUMP_KEY="/home/pinky/.ssh/id_ed25519"
TARGET="pinky@62.171.183.138"
TOKEN_DIR='~/.garth_tokens'

log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*" >> "$LOG"; }

# Gia' scattato: niente SSH, niente rumore.
[ -e "$SENTINEL" ] && exit 0

mkdir -p "$(dirname "$LOG")" "$(dirname "$SENTINEL")"

# Conta i file nella dir sul target. Stampa un intero, oppure niente se la
# dir non esiste. -A cosi' una dir vuota (enrollment a meta') non fa fuoco.
count=$(timeout 45 ssh -o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new \
    -p "$JUMP_PORT" -i "$JUMP_KEY" "$JUMP_HOST" \
    "ssh -o BatchMode=yes -o ConnectTimeout=10 $TARGET 'ls -A $TOKEN_DIR 2>/dev/null | wc -l'" 2>/dev/null | tr -d '[:space:]')
ssh_rc=$?

# SSH irraggiungibile o output non numerico: non e' un "no", e' un "non lo so".
# Non fare fuoco, riprova al prossimo giro.
if [ $ssh_rc -ne 0 ] || ! [[ "$count" =~ ^[0-9]+$ ]]; then
    log "SSH check fallito (rc=$ssh_rc, out='${count}') — nessun fire, ritento al prossimo run"
    exit 0
fi

# Check andato a buon fine (risposta numerica dal target): lascia la prova di
# vita PRIMA di ramificare, cosi' vale sia per count=0 che per il fire.
printf '%s count=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$count" > "$LASTOK"

if [ "$count" -eq 0 ]; then
    exit 0
fi

log "ENROLLMENT RILEVATO: $count file in $TOKEN_DIR — fire webhook"

http_code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 30 \
    -X POST -H 'Content-Type: application/json' \
    -d "{\"event\":\"garmin_enrollment_detected\",\"token_files\":$count}" \
    "$WEBHOOK_URL")

if [ "$http_code" = "200" ] || [ "$http_code" = "202" ] || [ "$http_code" = "204" ]; then
    date -u +%Y-%m-%dT%H:%M:%SZ > "$SENTINEL"
    log "Webhook OK (HTTP $http_code) — sentinel creato, watcher disarmato"
else
    # Nessun sentinel: se il daemon era giu' vogliamo ritentare.
    log "Webhook FALLITO (HTTP $http_code) — nessun sentinel, ritento al prossimo run"
fi
