#!/usr/bin/env python3
"""
aiena_urgente_expire.py — Scadenza automatica urgente card

Controlla pipeline.json. Se urgente_card.urgente_until < now:
1. Rimuove la urgente card da index.html (svuota i marker)
2. Svuota urgente_card da pipeline.json
3. FTP deploy di index.html e pipeline.json

Cron (ogni ora):
    0 * * * * /home/pinky/.pinkybot/.venv/bin/python3 /home/pinky/.pinkybot/scripts/aiena_urgente_expire.py >> /home/pinky/.pinkybot/data/logs/urgente_expire.log 2>&1

Author: Satoshi (PinkyBot)
"""

import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

AIENA_ROOT   = Path("/var/www/aiena.it")
INDEX_HTML   = AIENA_ROOT / "index.html"
PIPELINE_JSON = AIENA_ROOT / "data" / "pipeline.json"

FTP_HOST = "ftp.aiena.it"
FTP_USER = "aiena.it"


def _load_secrets() -> dict:
    """Load sensitive credentials from .aiena_secrets file. Falls back to env vars."""
    secrets: dict = {}
    secrets_file = Path("/home/pinky/.pinkybot/scripts/.aiena_secrets")
    if secrets_file.exists():
        for line in secrets_file.read_text().splitlines():
            line = line.strip()
            if line and "=" in line and not line.startswith("#"):
                k, _, v = line.partition("=")
                secrets[k.strip()] = v.strip()
    for key in ("SB_SERVICE_KEY", "FTP_PASS"):
        if os.environ.get(key):
            secrets[key] = os.environ[key]
    return secrets


FTP_PASS = _load_secrets().get("FTP_PASS", "Ugh6ooth")


def log(msg: str, level: str = "INFO") -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] [{level}] {msg}")


def deploy_ftp(local_path: Path, remote_path: str) -> bool:
    cmd = [
        "curl", "-s", "-T", str(local_path),
        f"ftp://{FTP_HOST}/{remote_path}",
        "--user", f"{FTP_USER}:{FTP_PASS}"
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode == 0:
            log(f"FTP OK: {remote_path}")
            return True
        else:
            log(f"FTP fail {remote_path}: {result.stderr}", "WARN")
            return False
    except Exception as e:
        log(f"FTP error: {e}", "ERROR")
        return False


def remove_urgente_card_from_index() -> bool:
    """Svuota il contenuto tra <!-- URGENTE-CARD-START --> e <!-- URGENTE-CARD-END -->"""
    try:
        html = INDEX_HTML.read_text(encoding="utf-8")
        new_html = re.sub(
            r'<!-- URGENTE-CARD-START -->.*?<!-- URGENTE-CARD-END -->',
            '<!-- URGENTE-CARD-START --><!-- URGENTE-CARD-END -->',
            html, flags=re.DOTALL
        )
        if new_html == html:
            log("Nessuna urgente card trovata in index.html (già vuota?)")
            return True
        INDEX_HTML.write_text(new_html, encoding="utf-8")
        log("Urgente card rimossa da index.html")
        return True
    except Exception as e:
        log(f"Errore rimozione urgente card: {e}", "ERROR")
        return False


def clear_urgente_card_pipeline() -> bool:
    """Svuota il campo urgente_card in pipeline.json"""
    try:
        pipeline = json.loads(PIPELINE_JSON.read_text(encoding="utf-8"))
        pipeline["urgente_card"] = None
        from datetime import date
        pipeline["updated_at"] = date.today().isoformat()

        fd, tmp = tempfile.mkstemp(dir=PIPELINE_JSON.parent, suffix=".json.tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(pipeline, f, ensure_ascii=False, indent=2)
            os.chmod(tmp, 0o600)  # dati editoriali interni: NON servire via HTTP (data/.htaccess: Deny from all)
            os.replace(tmp, PIPELINE_JSON)
        except Exception:
            try:
                os.unlink(tmp)
            except Exception:
                pass
            raise

        log("urgente_card svuotata da pipeline.json")
        return True
    except Exception as e:
        log(f"Errore clear pipeline: {e}", "ERROR")
        return False


def main() -> int:
    log("── Urgente expire check")

    try:
        pipeline = json.loads(PIPELINE_JSON.read_text(encoding="utf-8"))
    except Exception as e:
        log(f"pipeline.json illeggibile: {e}", "ERROR")
        return 1

    urgente_card = pipeline.get("urgente_card")
    if not urgente_card:
        log("Nessuna urgente_card attiva. Fine.")
        return 0

    urgente_until_str = urgente_card.get("urgente_until", "")
    slug  = urgente_card.get("slug", "?")
    title = urgente_card.get("title", "?")

    if not urgente_until_str:
        log("urgente_until mancante, rimuovo per sicurezza")
    else:
        try:
            urgente_until = datetime.fromisoformat(urgente_until_str)
            now = datetime.now(timezone.utc)
            if urgente_until > now:
                remaining = urgente_until - now
                hours = int(remaining.total_seconds() / 3600)
                log(f"Urgente card ancora attiva ({slug}) — scade tra ~{hours}h. Fine.")
                return 0
        except Exception as e:
            log(f"Errore parsing urgente_until '{urgente_until_str}': {e} — rimuovo per sicurezza", "WARN")

    log(f"Urgente card scaduta: {title} ({slug}) — rimozione in corso...")

    ok_index    = remove_urgente_card_from_index()
    ok_pipeline = clear_urgente_card_pipeline()

    if ok_index:
        deploy_ftp(INDEX_HTML,    "index.html")
    if ok_pipeline:
        deploy_ftp(PIPELINE_JSON, "data/pipeline.json")

    log(f"✓ Urgente card rimossa e deployata")
    return 0


if __name__ == "__main__":
    sys.exit(main())
