#!/usr/bin/env python3
"""
Brief serale Satoshi → Mirko — gira alle 20:00 lun/mar/mer/gio
Legge pipeline.json v2 (investigations[], leads[]) e invia stato editoriale aggiornato.
"""
import json
import requests
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo
from datetime import datetime

from aiena_secrets import _load_secrets

TELEGRAM_TOKEN = _load_secrets().get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = "32405655"
PIPELINE_JSON = Path("/var/www/aiena.it/data/pipeline.json")

DAY_NAMES = {0: "Domenica", 1: "Lunedì", 2: "Martedì", 3: "Mercoledì",
             4: "Giovedì", 5: "Venerdì", 6: "Sabato"}


def send_telegram(text: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"})


def main():
    now = datetime.now(ZoneInfo("Europe/Rome"))
    today_dow = now.weekday()  # 0=Lun, 6=Dom

    # Gira solo lun-gio (0-3)
    if today_dow > 3:
        print(f"[brief] Giorno {DAY_NAMES.get(today_dow+1 if today_dow < 6 else 0)} — nessun brief (weekend/venerdì)")
        return

    try:
        data = json.loads(PIPELINE_JSON.read_text())
    except Exception as e:
        print(f"[brief] Errore lettura pipeline: {e}")
        return

    # pipeline.json v2: investigations[] contiene articoli in lavorazione, leads[] contiene early-stage
    investigations = data.get("investigations", [])
    leads = data.get("leads", [])
    published = data.get("published", [])

    cur = investigations[0] if len(investigations) > 0 else {}
    nxt = investigations[1] if len(investigations) > 1 else {}

    # Stato indagine corrente
    cur_title = cur.get("title", "Nessuna indagine attiva") if cur else "Nessuna indagine attiva"
    cur_status = cur.get("status", "—") if cur else "—"
    cur_pub = cur.get("pub_date", cur.get("publish_date", "TBD")) if cur else "—"

    # Prossima
    nxt_title = nxt.get("title", "—") if nxt else "—"

    # Ultima pubblicata
    last_pub = published[0] if published else None
    last_pub_str = f"{last_pub['title']} ({last_pub['published_at']})" if last_pub else "—"

    msg = (
        f"🌙 <b>Brief serale — {DAY_NAMES.get(today_dow, '')} {now.strftime('%d/%m')}</b>\n\n"
        f"<b>Indagine corrente:</b>\n"
        f"  {cur_title}\n"
        f"  Stato: {cur_status} | Target: {cur_pub}\n\n"
        f"<b>Prossima in pipeline:</b>\n"
        f"  {nxt_title}\n\n"
        f"<b>Leads:</b> {len(leads)} articoli in cantiere\n"
        f"<b>Ultimo pubblicato:</b> {last_pub_str}\n\n"
        f"<i>Satoshi — report automatico 20:00</i>"
    )

    send_telegram(msg)
    print(f"[brief] Inviato brief serale per {DAY_NAMES.get(today_dow, '')}")


if __name__ == "__main__":
    main()
