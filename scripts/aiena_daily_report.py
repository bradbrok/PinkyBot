#!/usr/bin/env python3
"""
Report giornaliero AIena — gira ogni sera alle 20:00.
Chiede ad AIena un report obbligatorio su cosa ha fatto durante il giorno.
AIena lo invia direttamente a Mirko su Telegram.
"""
import json
import os
import requests
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

# cron bootstrap: quando lo script gira da crontab, PINKY_SESSION_SECRET non è
# nell'ambiente → build_internal_auth_headers riceve secret="" → nessun header
# di firma → richiesta senza auth → 401.
# Carichiamo il .env del daemon se PINKY_SESSION_SECRET è assente o vuoto.
_DAEMON_ENV = Path("/home/pinky/.pinkybot/.env")
if not os.environ.get("PINKY_SESSION_SECRET") and _DAEMON_ENV.exists():
    for _ln in _DAEMON_ENV.read_text().splitlines():
        _ln = _ln.strip()
        if _ln and "=" in _ln and not _ln.startswith("#"):
            _ek, _, _ev = _ln.partition("=")
            _k = _ek.strip()
            _v = _ev.strip().strip('"').strip("'")
            if not os.environ.get(_k):
                os.environ[_k] = _v

import sys
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
from pinky_daemon.auth import build_internal_auth_headers, resolve_signing_secret

AIENA_ENDPOINT = "http://localhost:8888/agents/aiena/message"


def trigger_aiena(message: str):
    try:
        _signing_secret = resolve_signing_secret()
        _auth_hdrs = build_internal_auth_headers(
            _signing_secret,
            agent_name="satoshi",
            method="POST",
            path="/agents/aiena/message",
        ) if _signing_secret else {}
        resp = requests.post(
            AIENA_ENDPOINT,
            json={"from_agent": "satoshi", "message": message},
            headers={"Content-Type": "application/json", **_auth_hdrs},
            timeout=10
        )
        print(f"[daily-report] AIena triggered: {resp.status_code}")
    except Exception as e:
        print(f"[daily-report] Errore: {e}")


def main():
    now = datetime.now(ZoneInfo("Europe/Rome"))
    day_label = now.strftime("%A %d %b %Y")

    today_iso = now.strftime("%Y-%m-%d")
    today_display = now.strftime("%d %b %Y").upper()

    msg = (
        f"Report obbligatorio fine giornata — {day_label}.\n\n"
        f"STEP 1 — Aggiorna pipeline.json PRIMA di inviare il report.\n"
        f"File: /var/www/aiena.it/data/pipeline.json\n"
        f"Aggiungi un evento nell'array 'events' per OGNI azione concreta fatta oggi.\n"
        f"Formato evento:\n"
        f'{{"date":"{today_iso}","type":"ricerca","title":"[titolo breve]","description":"[descrizione azione e risultato]","related_slug":"[slug indagine se applicabile]","icon":"🔍"}}\n'
        f"Tipi evento: ricerca / scoperta / azione / pubblicazione / segnalazione / nota\n"
        f"Icone suggerite: 🔍 ricerca, 💡 scoperta, 📨 azione, 📰 pubblicazione, 🚩 segnalazione, 📝 nota\n"
        f"Se oggi non hai fatto nulla → aggiungi evento tipo 'nota' con description 'Nessuna attività rilevante.'\n\n"
        f"STEP 2 — Dopo aver aggiornato pipeline.json, esegui il rebuild admin:\n"
        f"import subprocess; subprocess.run(['/home/pinky/.pinkybot/.venv/bin/python3', '/home/pinky/.pinkybot/scripts/aiena_admin_rebuild.py'], timeout=60)\n\n"
        f"STEP 3 — Manda a Mirko (Telegram chat_id: 32405655) il report con:\n\n"
        f"1. **Cosa hai fatto oggi** — elenca ogni azione concreta (fonti cercate, query ANAC, documenti letti, lead trovati)\n"
        f"2. **Risultati concreti** — fatti verificabili trovati, con fonte. Non paroloni, dati.\n"
        f"3. **pipeline.json aggiornato?** — sì/no, cosa hai aggiunto\n"
        f"4. **Cosa non hai trovato o non hai potuto fare** — sii onesta\n"
        f"5. **Prossimo passo** — cosa farai domani\n\n"
        f"Formato: testo breve e diretto, nessun preambolo. Mirko vuole certezze, non prose.\n"
        f"Se oggi non hai fatto nulla di rilevante, dillo esplicitamente.\n\n"
        f"Questo report è obbligatorio ogni sera — fa parte del workflow."
    )

    print(f"[daily-report] Triggering AIena report for {day_label}")
    trigger_aiena(msg)


if __name__ == "__main__":
    main()
