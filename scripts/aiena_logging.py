#!/usr/bin/env python3
"""
aiena_logging.py - Sistema di logging strutturato per l'ecosistema AIena.it

Fornisce:
1. Deploy Ledger: registro JSON append-only per operazioni FTP
2. Pipeline Journal: log testuale strutturato per step pipeline
3. Alert Telegram: notifica immediata su fallimenti (con anti-spam)

Uso:
    from aiena_logging import journal_step, ledger_ftp, alert_failure

    journal_step("aiena_auto_publish", "appalti-sanitari", "ftp_article", "ok")
    ledger_ftp("aiena_auto_publish", "appalti-sanitari", "articles/foo.html", "ftp_upload", True, 12345, "abc123")
    alert_failure("aiena_auto_publish", "appalti-sanitari", "ftp_article", "Connection timeout")

Author: Engineer (PinkyBot)
"""

import fcntl
import hashlib
import json
import os
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

sys.path.insert(0, "/home/pinky/lib")
import broker_auth  # noqa: E402  (path aggiunto sopra)

# ============================================================================
# Configuration
# ============================================================================

LOGS_DIR = Path("/home/pinky/.pinkybot/data/logs")
DEPLOY_LEDGER_PATH = LOGS_DIR / "deploy_ledger.json"
PIPELINE_JOURNAL_PATH = LOGS_DIR / "pipeline_journal.log"

# Telegram alert config
BROKER_URL = "http://localhost:8888/broker/send"
ALERT_CHAT_ID = "32405655"
ALERT_COOLDOWN_MINUTES = 5  # Non spammare lo stesso step entro N minuti


# ============================================================================
# Helper functions
# ============================================================================

def _ensure_logs_dir() -> None:
    """Crea la directory logs se non esiste."""
    LOGS_DIR.mkdir(parents=True, exist_ok=True)


def _now_iso() -> str:
    """Timestamp ISO 8601 con timezone UTC."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _file_sha256(filepath: Path) -> str:
    """Calcola SHA256 di un file."""
    try:
        h = hashlib.sha256()
        with open(filepath, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return ""


# ============================================================================
# Deploy Ledger (JSON append-only)
# ============================================================================

def ledger_ftp(
    script: str,
    slug: str,
    file: str,
    operation: str,
    success: bool,
    size_bytes: int = 0,
    sha256: str = "",
    error: Optional[str] = None
) -> bool:
    """
    Registra un'operazione FTP nel deploy ledger.

    Args:
        script: Nome dello script che esegue (es. "aiena_auto_publish")
        slug: Slug dell'articolo
        file: Path remoto del file (es. "articles/foo.html")
        operation: Tipo operazione (es. "ftp_upload", "ftp_delete")
        success: True se OK, False se fallito
        size_bytes: Dimensione file in bytes
        sha256: Hash SHA256 del file
        error: Messaggio errore se fallito

    Returns:
        True se registrazione OK, False altrimenti (non blocca mai lo script)
    """
    try:
        _ensure_logs_dir()

        entry = {
            "timestamp": _now_iso(),
            "script": script,
            "slug": slug,
            "file": file,
            "operation": operation,
            "status": "ok" if success else "fail",
            "size_bytes": size_bytes,
            "sha256": sha256,
            "error": error
        }

        # Thread-safe append con file locking
        with open(DEPLOY_LEDGER_PATH, "a+") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            try:
                f.seek(0)
                content = f.read()
                if content.strip():
                    try:
                        ledger = json.loads(content)
                    except json.JSONDecodeError:
                        # File corrotto, ricomincia
                        ledger = {"version": 1, "entries": []}
                else:
                    ledger = {"version": 1, "entries": []}

                ledger["entries"].append(entry)

                f.seek(0)
                f.truncate()
                f.write(json.dumps(ledger, ensure_ascii=False, indent=2))
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)

        return True

    except Exception as e:
        # Non-blocking: log fallimento ma non interrompere lo script
        print(f"[WARN] ledger_ftp failed: {e}")
        return False


def ledger_ftp_from_file(
    script: str,
    slug: str,
    local_path: Path,
    remote_path: str,
    operation: str,
    success: bool,
    error: Optional[str] = None
) -> bool:
    """
    Wrapper che calcola automaticamente size e sha256 dal file locale.
    """
    size_bytes = 0
    sha256 = ""

    if local_path.exists():
        try:
            size_bytes = local_path.stat().st_size
            sha256 = _file_sha256(local_path)
        except Exception:
            pass

    return ledger_ftp(
        script=script,
        slug=slug,
        file=remote_path,
        operation=operation,
        success=success,
        size_bytes=size_bytes,
        sha256=sha256,
        error=error
    )


# ============================================================================
# Pipeline Journal (log testuale strutturato)
# ============================================================================

def journal_step(
    script: str,
    slug: str,
    step: str,
    status: str,
    error: Optional[str] = None
) -> bool:
    """
    Registra uno step nel pipeline journal.

    Formato:
        [2026-05-05T09:30:00Z] [script] [slug] STEP: nome -> OK|FAIL (error)

    Args:
        script: Nome dello script (es. "aiena_auto_publish")
        slug: Slug dell'articolo
        step: Nome dello step (es. "ftp_article", "advance_pipeline")
        status: "ok" o "fail"
        error: Messaggio errore opzionale (solo se fail)

    Returns:
        True se registrazione OK, False altrimenti (non blocca mai)
    """
    try:
        _ensure_logs_dir()

        ts = _now_iso()
        status_upper = status.upper()

        if error and status.lower() == "fail":
            line = f"[{ts}] [{script}] [{slug}] STEP: {step} -> {status_upper} ({error})\n"
        else:
            line = f"[{ts}] [{script}] [{slug}] STEP: {step} -> {status_upper}\n"

        # Append-only con file locking
        with open(PIPELINE_JOURNAL_PATH, "a") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            try:
                f.write(line)
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)

        return True

    except Exception as e:
        print(f"[WARN] journal_step failed: {e}")
        return False


def journal_milestone(script: str, slug: str, milestone: str) -> bool:
    """
    Registra un milestone speciale (es. PIPELINE_COMPLETE, POST_PUBLISH).
    """
    try:
        _ensure_logs_dir()

        ts = _now_iso()
        line = f"[{ts}] [{script}] [{slug}] {milestone} -> OK\n"

        with open(PIPELINE_JOURNAL_PATH, "a") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            try:
                f.write(line)
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)

        return True

    except Exception as e:
        print(f"[WARN] journal_milestone failed: {e}")
        return False


# ============================================================================
# Alert Telegram (con anti-spam)
# ============================================================================

def _should_alert(script: str, step: str) -> bool:
    """
    Verifica se lo stesso step ha gia fallito negli ultimi N minuti.
    Legge le ultime righe del journal per evitare spam.
    """
    try:
        if not PIPELINE_JOURNAL_PATH.exists():
            return True

        cutoff = datetime.now(timezone.utc) - timedelta(minutes=ALERT_COOLDOWN_MINUTES)

        with open(PIPELINE_JOURNAL_PATH, "r") as f:
            lines = f.readlines()

        # Leggi ultime 100 righe
        for line in reversed(lines[-100:]):
            if f"[{script}]" not in line:
                continue
            if f"STEP: {step} ->" not in line:
                continue
            if "-> FAIL" not in line:
                continue

            # Estrai timestamp
            try:
                ts_str = line.split("]")[0].strip("[")
                ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                if ts > cutoff:
                    # Stesso step fallito di recente, non spammare
                    return False
            except Exception:
                continue

        return True

    except Exception:
        return True  # In caso di errore, manda comunque l'alert


def alert_failure(
    script: str,
    slug: str,
    step: str,
    error: str
) -> bool:
    """
    Invia alert Telegram per un fallimento.

    Formato:
        AIena Pipeline FAIL
        [script] -> [step] fallito
        Slug: [slug]
        Errore: [error]
        Timestamp: [ts]

    Anti-spam: non invia se lo stesso step e' fallito negli ultimi 5 minuti.

    Returns:
        True se alert inviato, False altrimenti
    """
    try:
        # Anti-spam check
        if not _should_alert(script, step):
            print(f"[INFO] Alert skipped (anti-spam): {script}/{step}")
            return False

        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        message = (
            f"AIena Pipeline FAIL\n"
            f"[{script}] -> {step} fallito\n"
            f"Slug: {slug}\n"
            f"Errore: {error}\n"
            f"Timestamp: {ts}"
        )

        # POST firmata: /broker/send rifiuta con 401 le richieste non
        # autenticate, e prima quel 401 spariva nell'except sotto — gli alert
        # di fallimento pipeline non arrivavano mai a destinazione.
        broker_auth.send_message(ALERT_CHAT_ID, message, agent="satoshi")
        return True

    except Exception as e:
        # Qui NON si solleva di proposito: alert_failure() gira dentro i rami di
        # errore delle pipeline, e un'eccezione qui mascherererebbe il
        # fallimento originale. L'errore va pero' su stderr, dove il cron lo
        # cattura, non piu' come un [WARN] indistinguibile dal rumore.
        print(f"[ERROR] alert_failure: alert NON consegnato a {ALERT_CHAT_ID} "
              f"({type(e).__name__}: {e}) — alert perso: {script}/{step} {slug}",
              file=sys.stderr)
        return False


# ============================================================================
# Utility: calcola hash di un file (per uso esterno)
# ============================================================================

def file_sha256(filepath: Path) -> str:
    """Calcola e ritorna SHA256 di un file."""
    return _file_sha256(filepath)


# ============================================================================
# Test standalone
# ============================================================================

if __name__ == "__main__":
    print("Testing aiena_logging...")

    # Test journal
    journal_step("test_script", "test-slug", "test_step", "ok")
    journal_step("test_script", "test-slug", "test_step_fail", "fail", "Test error message")
    journal_milestone("test_script", "test-slug", "TEST_COMPLETE")

    # Test ledger
    ledger_ftp("test_script", "test-slug", "test/file.html", "ftp_upload", True, 1234, "abc123def456")
    ledger_ftp("test_script", "test-slug", "test/file2.html", "ftp_upload", False, 0, "", "Connection refused")

    print("Done. Check:")
    print(f"  - {PIPELINE_JOURNAL_PATH}")
    print(f"  - {DEPLOY_LEDGER_PATH}")
