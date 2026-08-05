#!/usr/bin/env python3
"""
AIena Rank Report — Invia report posizioni Google a Mirko su Telegram.
Cron: 33 8 * * * (ogni mattina alle 08:33)
"""

import subprocess
import requests
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

from aiena_secrets import _load_secrets

TELEGRAM_TOKEN = _load_secrets().get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = "32405655"
SCRIPT_PATH = "/home/pinky/.pinkybot/scripts/aiena_rank_tracker.py"
VENV_PYTHON = "/home/pinky/.pinkybot/.venv/bin/python3"


def send_telegram(text: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    resp = requests.post(url, json={
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML"
    })
    return resp.ok


def main():
    now = datetime.now(ZoneInfo("Europe/Rome"))
    date_str = now.strftime("%-d %B %Y")

    # Esegui il rank tracker e cattura output
    try:
        result = subprocess.run(
            [VENV_PYTHON, SCRIPT_PATH, "--report"],
            capture_output=True,
            text=True,
            timeout=30
        )
        output = result.stdout
    except Exception as e:
        print(f"[rank-report] Errore esecuzione tracker: {e}", file=sys.stderr)
        return 1

    # Estrai la sezione Telegram dall'output
    telegram_section = ""
    in_section = False
    for line in output.splitlines():
        if "SEZIONE TELEGRAM" in line:
            in_section = True
            continue
        if in_section:
            telegram_section += line + "\n"

    if not telegram_section.strip():
        # Fallback: usa tutto l'output SEZIONE REPORT TELEGRAM
        in_section = False
        for line in output.splitlines():
            if "SEZIONE REPORT TELEGRAM" in line:
                in_section = True
                continue
            if in_section:
                telegram_section += line + "\n"

    if not telegram_section.strip():
        print("[rank-report] Nessuna sezione Telegram trovata nell'output", file=sys.stderr)
        return 1

    # Componi messaggio finale
    header = f"📊 <b>AIena.it — SEO Report | {date_str}</b>\n\n"
    message = header + telegram_section.strip()

    if send_telegram(message):
        print(f"[rank-report] Report inviato a Mirko ({TELEGRAM_CHAT_ID})")
        return 0
    else:
        print("[rank-report] Errore invio Telegram", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
