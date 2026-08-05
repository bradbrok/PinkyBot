#!/usr/bin/env python3
"""
Report settimanale venerdì — Satoshi → Mirko
Riepilogo settimana editoriale: pubblicazioni, stato pipeline, leads.
Legge pipeline.json v2 (investigations[], leads[]).
"""
import json
import requests
from pathlib import Path
from zoneinfo import ZoneInfo
from datetime import datetime, timedelta, date

from aiena_secrets import _load_secrets

TELEGRAM_TOKEN = _load_secrets().get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = "32405655"
PIPELINE_JSON = Path("/var/www/aiena.it/data/pipeline.json")
BSKY_LOG = Path("/home/pinky/.pinkybot/logs/aiena_bsky_engage.log")


def send_telegram(text: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"})


def count_bsky_actions_this_week() -> dict:
    """Conta like/follow/reply/post da log Bluesky ultimi 7 giorni."""
    counts = {"LIKE": 0, "FOLLOW": 0, "REPLY": 0, "POST": 0}
    if not BSKY_LOG.exists():
        return counts
    cutoff = datetime.now(ZoneInfo("Europe/Rome")) - timedelta(days=7)
    try:
        lines = BSKY_LOG.read_text().splitlines()[-500:]  # ultime 500 righe
        for line in lines:
            for key in counts:
                if f"] {key}:" in line:
                    counts[key] += 1
    except Exception:
        pass
    return counts


def main():
    now = datetime.now(ZoneInfo("Europe/Rome"))
    # Sicurezza: gira solo il venerdì (crontab già limita, ma doppia verifica)
    if now.weekday() != 4:  # 4=Venerdì
        print(f"[weekly] Non è venerdì — skip (chiamata fuori schedule)")
        return

    try:
        data = json.loads(PIPELINE_JSON.read_text())
    except Exception as e:
        print(f"[weekly] Errore lettura pipeline: {e}")
        return

    # pipeline.json v2: investigations[] contiene articoli in lavorazione, leads[] contiene early-stage
    investigations = data.get("investigations", [])
    leads = data.get("leads", [])
    published = data.get("published", [])

    cur = investigations[0] if len(investigations) > 0 else {}
    nxt = investigations[1] if len(investigations) > 1 else {}

    now = datetime.now(ZoneInfo("Europe/Rome"))

    # Pubblicazioni questa settimana
    week_start = (now - timedelta(days=now.weekday())).date()
    pubs_this_week = [
        p for p in published
        if p.get("published_at") and date.fromisoformat(p["published_at"]) >= week_start
    ]

    # Bluesky stats
    bsky = count_bsky_actions_this_week()

    cur_title = cur.get("title", "—") if cur else "—"
    cur_status = cur.get("status", "—") if cur else "—"
    nxt_title = nxt.get("title", "—") if nxt else "—"

    pub_section = ""
    if pubs_this_week:
        pub_section = "\n".join(f"  ✅ {p['title']}" for p in pubs_this_week)
    else:
        pub_section = "  Nessuna pubblicazione questa settimana"

    msg = (
        f"📊 <b>Report settimanale — Venerdì {now.strftime('%d/%m/%Y')}</b>\n\n"
        f"<b>Pubblicazioni settimana:</b>\n{pub_section}\n\n"
        f"<b>Indagine in corso:</b>\n"
        f"  {cur_title} ({cur_status})\n\n"
        f"<b>Prossima:</b> {nxt_title}\n"
        f"<b>Leads:</b> {len(leads)} articoli\n\n"
        f"<b>AIena su Bluesky (7gg):</b>\n"
        f"  Like: {bsky['LIKE']} | Follow: {bsky['FOLLOW']} | Reply: {bsky['REPLY']} | Post: {bsky['POST']}\n\n"
        f"<i>Satoshi — report automatico venerdì</i>"
    )

    send_telegram(msg)
    print(f"[weekly] Report settimanale inviato")


if __name__ == "__main__":
    main()
