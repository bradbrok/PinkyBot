#!/usr/bin/env python3
"""
AIena Health Monitor — Avvisa se AIena non fa sessioni da >24h.
Cron: 0 9 * * * (ogni giorno alle 09:00)
"""
import sqlite3
import json
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

from aiena_secrets import _load_secrets

DB_PATH = Path.home() / ".pinkybot" / "data" / "conversations_agents.db"
TELEGRAM_TOKEN = _load_secrets().get("TELEGRAM_BOT_TOKEN", "")  # fixed: was wrong token
TELEGRAM_CHAT_ID = "32405655"
ALERT_THRESHOLD_HOURS = 24


def send_telegram(text: str):
    try:
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data=json.dumps({"chat_id": TELEGRAM_CHAT_ID, "text": text}).encode(),
            headers={"Content-Type": "application/json"}
        )
        urllib.request.urlopen(req, timeout=10)
    except Exception:
        pass


def check():
    if not DB_PATH.exists():
        print("[health-monitor] DB non trovato")
        return

    con = sqlite3.connect(str(DB_PATH))
    row = con.execute(
        "SELECT timestamp, status, notes FROM agent_heartbeats WHERE agent_name='aiena' ORDER BY timestamp DESC LIMIT 1"
    ).fetchone()
    con.close()

    now = datetime.now(timezone.utc)

    if not row:
        send_telegram("⚠️ AIena health: nessun heartbeat mai registrato nel DB")
        return

    last_ts = datetime.fromtimestamp(row[0], tz=timezone.utc)
    age_hours = (now - last_ts).total_seconds() / 3600

    if age_hours > ALERT_THRESHOLD_HOURS:
        last_str = last_ts.strftime("%d %b %H:%M")
        send_telegram(
            f"⚠️ AIena inattiva da {age_hours:.0f}h\n"
            f"Ultimo heartbeat: {last_str}\n"
            f"Stato: {row[1]} — {row[2][:100] if row[2] else 'N/A'}\n"
            f"Controlla i log: /logs/aiena_enriched_scout.log"
        )
        print(f"[health-monitor] ALERT: AIena inattiva da {age_hours:.0f}h")
    else:
        print(f"[health-monitor] OK: AIena attiva {age_hours:.0f}h fa")


if __name__ == "__main__":
    check()
