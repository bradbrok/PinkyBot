#!/usr/bin/env bash
# =============================================================================
# VPS Backup Script — PinkyBot + Postgres + Qdrant
# Runs nightly at 03:00 via cron. Retention: 7 days local.
# Logs: /home/pinky/.pinkybot/logs/backup.log
# =============================================================================

set -euo pipefail

BACKUP_DIR="/home/pinky/backups"
LOG="/home/pinky/.pinkybot/logs/backup.log"
RETENTION_DAYS=7
DATE=$(date +%Y%m%d_%H%M%S)
ERRORS=0

mkdir -p "$BACKUP_DIR"
mkdir -p "$(dirname "$LOG")"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$LOG"; }

log "=== BACKUP START ==="

# ─────────────────────────────────────────────
# 1. SQLite — tutti i .db di PinkyBot
# ─────────────────────────────────────────────
SQLITE_DEST="$BACKUP_DIR/sqlite_${DATE}"
mkdir -p "$SQLITE_DEST"
log "Backup SQLite databases..."

for db in /home/pinky/.pinkybot/data/*.db; do
    name=$(basename "$db")
    if sqlite3 "$db" ".backup '$SQLITE_DEST/$name'" 2>/dev/null; then
        log "  ✓ $name"
    else
        log "  ✗ $name (failed)"
        ERRORS=$((ERRORS+1))
    fi
done

# Comprimi
tar -czf "${SQLITE_DEST}.tar.gz" -C "$BACKUP_DIR" "sqlite_${DATE}" && rm -rf "$SQLITE_DEST"
log "SQLite archived: sqlite_${DATE}.tar.gz"

# ─────────────────────────────────────────────
# 2. PostgreSQL (dnd-ai)
# ─────────────────────────────────────────────
PG_DEST="$BACKUP_DIR/postgres_${DATE}.sql.gz"
log "Backup PostgreSQL..."

if docker exec dnd-postgres pg_dumpall -U dnduser 2>/dev/null | gzip > "$PG_DEST"; then
    SIZE=$(du -sh "$PG_DEST" | cut -f1)
    log "PostgreSQL archived: postgres_${DATE}.sql.gz ($SIZE)"
else
    log "PostgreSQL backup FAILED"
    ERRORS=$((ERRORS+1))
fi

# ─────────────────────────────────────────────
# 3. Qdrant snapshot
# ─────────────────────────────────────────────
QDRANT_DEST="$BACKUP_DIR/qdrant_${DATE}"
mkdir -p "$QDRANT_DEST"
log "Backup Qdrant..."

COLLECTIONS=$(curl -s --max-time 10 http://localhost:6333/collections 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); print(' '.join([c['name'] for c in d['result']['collections']]))" 2>/dev/null || echo "")

if [ -n "$COLLECTIONS" ]; then
    for col in $COLLECTIONS; do
        SNAP=$(curl -s -X POST --max-time 60 "http://localhost:6333/collections/${col}/snapshots" 2>/dev/null)
        SNAP_NAME=$(echo "$SNAP" | python3 -c "import sys,json; print(json.load(sys.stdin)['result']['name'])" 2>/dev/null || echo "")
        if [ -n "$SNAP_NAME" ]; then
            curl -s --max-time 60 -o "${QDRANT_DEST}/${col}.snapshot" \
                "http://localhost:6333/collections/${col}/snapshots/${SNAP_NAME}" 2>/dev/null
            # Cleanup snapshot from Qdrant
            curl -s -X DELETE --max-time 10 \
                "http://localhost:6333/collections/${col}/snapshots/${SNAP_NAME}" > /dev/null 2>&1
            log "  ✓ Qdrant collection: $col"
        else
            log "  ✗ Qdrant collection: $col (snapshot failed)"
            ERRORS=$((ERRORS+1))
        fi
    done
    tar -czf "${QDRANT_DEST}.tar.gz" -C "$BACKUP_DIR" "qdrant_${DATE}" && rm -rf "$QDRANT_DEST"
    log "Qdrant archived: qdrant_${DATE}.tar.gz"
else
    log "Qdrant: no collections found or service unreachable"
    rmdir "$QDRANT_DEST" 2>/dev/null || true
fi

# ─────────────────────────────────────────────
# 4. aiena.it data
# ─────────────────────────────────────────────
AIENA_DEST="$BACKUP_DIR/aiena_data_${DATE}.tar.gz"
log "Backup aiena.it data..."
if [ -d "/var/www/aiena.it/data" ]; then
    tar -czf "$AIENA_DEST" /var/www/aiena.it/data 2>/dev/null && \
        log "aiena.it data archived: aiena_data_${DATE}.tar.gz" || \
        { log "aiena.it data backup FAILED"; ERRORS=$((ERRORS+1)); }
else
    log "aiena.it data dir not found — skip"
fi

# ─────────────────────────────────────────────
# 5. Retention — elimina backup più vecchi di N giorni
# ─────────────────────────────────────────────
log "Pruning backups older than ${RETENTION_DAYS} days..."
find "$BACKUP_DIR" -name "*.tar.gz" -mtime +${RETENTION_DAYS} -delete
find "$BACKUP_DIR" -name "*.sql.gz" -mtime +${RETENTION_DAYS} -delete

TOTAL_SIZE=$(du -sh "$BACKUP_DIR" 2>/dev/null | cut -f1 || echo "?")
log "Backup dir size: $TOTAL_SIZE"

# ─────────────────────────────────────────────
# 6. Report
# ─────────────────────────────────────────────
if [ "$ERRORS" -eq 0 ]; then
    log "=== BACKUP COMPLETE — no errors ==="
else
    log "=== BACKUP COMPLETE — ${ERRORS} ERROR(S) ==="
    # Notifica Mirko via PinkyBot API se ci sono errori
    curl -s -X POST "http://localhost:8888/agents/satoshi/message" \
        -H "Content-Type: application/json" \
        -d "{\"message\": \"⚠️ Backup notturno: ${ERRORS} errori. Controlla /home/pinky/.pinkybot/logs/backup.log\"}" \
        > /dev/null 2>&1 || true
fi

# ─────────────────────────────────────────────
# Healthchecks ping — segnala completamento
# ─────────────────────────────────────────────
curl -fsS --retry 3 --max-time 10 "http://localhost:8200/ping/aefb532d-9f21-4901-a5be-6a9990941f57$([ $ERRORS -gt 0 ] && echo '/fail')" > /dev/null 2>&1 || true
