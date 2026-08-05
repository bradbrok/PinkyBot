#!/usr/bin/env python3
"""
ESMA MiCA CASPS.csv update monitor — con deduplicazione reale.
Confronta la data "Last update: XX Month YYYY" dalla pagina ESMA
con l'ultima data processata salvata in state file.
Notifica segugio SOLO se la data cambia.

Cron: ogni 6 ore (0 */6 * * *)
State file: /home/pinky/.pinkybot/data/esma_casps_last_date.txt
"""

import re
import sys
import json
import urllib.request
import urllib.error
from pathlib import Path
from datetime import datetime

try:
    import requests
    from pinky_daemon.auth import build_internal_auth_headers, resolve_signing_secret
    _HAS_PINKY = True
except ImportError:
    _HAS_PINKY = False

# Config
ESMA_URL = "https://www.esma.europa.eu/esmas-activities/digital-finance-and-innovation/markets-crypto-assets-regulation-mica"
STATE_FILE = Path("/home/pinky/.pinkybot/data/esma_casps_last_date.txt")
PINKYBOT_API = "http://localhost:8888"
LOG_FILE = Path("/home/pinky/.pinkybot/logs/esma_casps_monitor.log")


def log(msg: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def fetch_page() -> str | None:
    req = urllib.request.Request(
        ESMA_URL,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; BMN-monitor/1.0)",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except urllib.error.URLError as e:
        log(f"ERROR fetch: {e}")
        return None


def extract_date(html: str) -> str | None:
    """Estrae 'Last update: DD Month YYYY' o simile dalla pagina ESMA."""
    # Pattern 1: "Last update: 26 June 2026"
    m = re.search(r"Last update[:\s]+(\d{1,2}\s+\w+\s+\d{4})", html, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    # Pattern 2: date in formato "26/06/2026"
    m = re.search(r"Last update[:\s]+(\d{1,2}/\d{2}/\d{4})", html, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    # Pattern 3: cerca la data vicino a "CASPS" nel testo
    m = re.search(r"CASPS.*?(\d{1,2}\s+\w+\s+\d{4})", html, re.IGNORECASE | re.DOTALL)
    if m:
        return m.group(1).strip()
    return None


def load_state() -> str:
    if STATE_FILE.exists():
        return STATE_FILE.read_text().strip()
    return ""


def save_state(date_str: str):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(date_str)


def notify_segugio(new_date: str, old_date: str):
    """Invia messaggio a segugio via PinkyBot API con HMAC auth."""
    msg = (
        f"ESMA MiCA CASPS aggiornato — nuova data rilevata: {new_date}\n"
        f"(precedente: {old_date if old_date else 'nessuna'})\n\n"
        f"Scarica il nuovo CASPS.csv dalla pagina ESMA e verifica i nuovi CASP autorizzati."
    )
    endpoint = f"{PINKYBOT_API}/agents/segugio/message"

    if _HAS_PINKY:
        try:
            secret = resolve_signing_secret()
            auth_hdrs = build_internal_auth_headers(
                secret, agent_name="satoshi", method="POST", path="/agents/segugio/message"
            ) if secret else {}
            resp = requests.post(
                endpoint,
                json={"from_agent": "satoshi", "message": msg},
                headers={"Content-Type": "application/json", **auth_hdrs},
                timeout=10
            )
            log(f"Notifica segugio inviata: {resp.status_code}")
            return
        except Exception as e:
            log(f"WARN pinky-auth fallback urllib: {e}")

    # Fallback urllib (no auth)
    payload = json.dumps({"content": msg}).encode()
    req = urllib.request.Request(
        endpoint,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            log(f"Notifica segugio inviata (urllib): {resp.status}")
    except Exception as e:
        log(f"ERROR notifica segugio: {e}")


def main():
    log("=== ESMA CASPS monitor check ===")

    html = fetch_page()
    if html is None:
        log("SKIP: pagina non raggiungibile")
        return

    current_date = extract_date(html)
    if not current_date:
        log("WARN: data 'Last update' non trovata nella pagina")
        return

    log(f"Data estratta dalla pagina: {current_date}")

    last_date = load_state()
    log(f"Ultima data processata: {last_date if last_date else '(nessuna)'}")

    if current_date == last_date:
        log("Nessun aggiornamento — data identica, nessuna notifica.")
        return

    log(f"NOVITÀ: data cambiata da '{last_date}' a '{current_date}' — notifico segugio")
    notify_segugio(current_date, last_date)
    save_state(current_date)
    log("State file aggiornato.")


if __name__ == "__main__":
    main()
