#!/usr/bin/env python3
"""
Backup orario di pipeline.json con retention 7 giorni.
Cron: 0 * * * * python3 aiena_pipeline_backup.py
"""
import json
import shutil
from pathlib import Path
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

PIPELINE_JSON = Path("/var/www/aiena.it/data/pipeline.json")
BACKUP_DIR = Path("/home/pinky/.pinkybot/data/pipeline_backups")

def run():
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    if not PIPELINE_JSON.exists():
        print("[pipeline-backup] pipeline.json non trovato — skip")
        return

    # Verifica che il file sia JSON valido prima di fare backup
    try:
        json.loads(PIPELINE_JSON.read_text())
    except json.JSONDecodeError:
        print("[WARN] pipeline.json non valido — backup saltato")
        return

    now = datetime.now(ZoneInfo("Europe/Rome"))
    ts = now.strftime("%Y%m%d_%H%M")
    dest = BACKUP_DIR / f"pipeline_{ts}.json"
    shutil.copy2(str(PIPELINE_JSON), str(dest))
    print(f"[pipeline-backup] Backup salvato: {dest}")

    # Retention: elimina backup > 7 giorni
    cutoff = now - timedelta(days=7)
    removed = 0
    for f in BACKUP_DIR.glob("pipeline_*.json"):
        try:
            # Parse timestamp dal nome file
            ts_str = f.stem.replace("pipeline_", "")
            file_dt = datetime.strptime(ts_str, "%Y%m%d_%H%M").replace(tzinfo=ZoneInfo("Europe/Rome"))
            if file_dt < cutoff:
                f.unlink()
                removed += 1
        except (ValueError, OSError):
            pass

    if removed:
        print(f"[pipeline-backup] Rimossi {removed} backup > 7 giorni")

if __name__ == "__main__":
    run()
