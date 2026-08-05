#!/usr/bin/env python3
"""
AIena Calendar Guardian — Reminder per articoli approvati senza date.

Logica:
1. Legge pipeline.json v2 (investigations[], leads[])
2. Per ogni articolo in status 'approvato' o 'approved':
   - Se pub_date e' None/vuoto: avvisa Mirko
   - Se pub_date e' domani: reminder a Mirko
3. MAI tocca pipeline.json, MAI chiama auto_publish, MAI pubblica nulla
4. Notifica solo via Telegram

Cron: 0 10 * * * (ogni giorno alle 10:00)
"""
import json
import sqlite3
import urllib.request
from datetime import datetime, date, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from aiena_secrets import _load_secrets

PIPELINE_JSON = Path("/var/www/aiena.it/data/pipeline.json")
STATE_DB = Path("/home/pinky/.pinkybot/data/aiena_stale_approver.db")

TELEGRAM_TOKEN = _load_secrets().get("TELEGRAM_BOT_TOKEN", "")  # fixed: was wrong token
TELEGRAM_CHAT_ID = "32405655"

# Supabase — source of truth for approval status
SB_URL = "https://fwyjxolljcogblvwvfca.supabase.co"
SB_KEY = _load_secrets().get("SB_SERVICE_KEY", "")
SB_HEADERS = {"apikey": SB_KEY, "Authorization": f"Bearer {SB_KEY}"}


def log(msg):
    now = datetime.now(ZoneInfo("Europe/Rome"))
    print(f"[calendar-guardian] {now.strftime('%H:%M')} {msg}")


def db_init():
    con = sqlite3.connect(str(STATE_DB))
    con.execute("""CREATE TABLE IF NOT EXISTS reminders (
        slug TEXT NOT NULL,
        reminder_type TEXT NOT NULL,
        sent_at TEXT NOT NULL,
        PRIMARY KEY (slug, reminder_type)
    )""")
    con.commit()
    con.close()


def reminder_already_sent(slug: str, reminder_type: str, hours: int = 24) -> bool:
    """Check if reminder was already sent in the last N hours."""
    con = sqlite3.connect(str(STATE_DB))
    row = con.execute(
        "SELECT sent_at FROM reminders WHERE slug=? AND reminder_type=?",
        (slug, reminder_type)
    ).fetchone()
    con.close()

    if not row:
        return False

    try:
        sent_at = datetime.fromisoformat(row[0])
        if sent_at.tzinfo is None:
            sent_at = sent_at.replace(tzinfo=ZoneInfo("Europe/Rome"))
        now = datetime.now(ZoneInfo("Europe/Rome"))
        return (now - sent_at).total_seconds() < hours * 3600
    except (ValueError, TypeError):
        return False


def mark_reminder_sent(slug: str, reminder_type: str):
    """Mark a reminder as sent."""
    con = sqlite3.connect(str(STATE_DB))
    now = datetime.now(ZoneInfo("Europe/Rome")).isoformat()
    con.execute(
        "INSERT OR REPLACE INTO reminders (slug, reminder_type, sent_at) VALUES (?,?,?)",
        (slug, reminder_type, now)
    )
    con.commit()
    con.close()


def send_telegram(text: str) -> bool:
    try:
        payload = json.dumps({"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data=payload,
            headers={"Content-Type": "application/json"}
        )
        urllib.request.urlopen(req, timeout=10)
        return True
    except Exception as e:
        log(f"Telegram error: {e}")
        return False


def _load_pipeline_meta() -> dict[str, dict]:
    """Legge pipeline.json e restituisce un dict slug → {title, pub_date, category}."""
    try:
        data = json.loads(PIPELINE_JSON.read_text(encoding="utf-8"))
        meta: dict[str, dict] = {}
        for key in ("investigations", "leads", "published"):
            for item in data.get(key, []):
                slug = item.get("slug", "")
                if slug:
                    meta[slug] = {
                        "title": item.get("title", slug),
                        "pub_date": item.get("pub_date"),
                        "category": item.get("category", ""),
                    }
        return meta
    except Exception as e:
        log(f"Errore lettura pipeline.json: {e}")
        return {}


def _query_supabase_pending() -> list[dict]:
    """
    Interroga Supabase article_approvals per record con status='pending'
    (= approvati da Mirko ma non ancora pubblicati).
    Ritorna lista di {slug, title, approved_at}.
    """
    url = (
        f"{SB_URL}/rest/v1/article_approvals"
        f"?select=slug,title,approved_at"
        f"&status=eq.pending"
    )
    req = urllib.request.Request(url, headers=SB_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())
    except Exception as e:
        log(f"Supabase fetch error: {e}")
        return []


def get_approved_articles() -> list[dict]:
    """
    Trova articoli approvati da Mirko (Supabase status=pending) ma non ancora pubblicati.
    Arricchisce con pub_date e title da pipeline.json.

    FIX: precedentemente cercava status 'approvato'/'approved' in pipeline.json,
    ma questi status sono computed at runtime e non esistono nel file JSON.
    Ora usa Supabase come source of truth per le approvazioni.
    """
    pending = _query_supabase_pending()
    if not pending:
        return []

    pipeline_meta = _load_pipeline_meta()
    result = []
    for record in pending:
        slug = record.get("slug", "")
        if not slug:
            continue
        meta = pipeline_meta.get(slug, {})
        result.append({
            "slug": slug,
            "title": meta.get("title") or record.get("title", slug),
            "pub_date": meta.get("pub_date"),
            "category": meta.get("category", ""),
            "approved_at": record.get("approved_at"),
        })
    return result


def run():
    db_init()
    today = date.today()
    tomorrow = today + timedelta(days=1)

    approved = get_approved_articles()

    if not approved:
        log("Nessun articolo approvato — nulla da fare")
        return

    for article in approved:
        slug = article.get("slug", "")
        title = article.get("title", "")
        if not slug:
            continue

        pub_date = article.get("pub_date")

        # Case 1: Approvato senza data di pubblicazione
        if not pub_date:
            reminder_type = "no_date"
            if reminder_already_sent(slug, reminder_type, hours=24):
                log(f"{slug}: reminder 'no_date' gia' inviato nelle ultime 24h — skip")
                continue

            msg = (
                f"<b>Articolo approvato senza data</b>\n\n"
                f"<b>{title}</b>\n"
                f"Status: approvato — manca la data di pubblicazione\n\n"
                f"Assegna una data nel calendario admin."
            )
            if send_telegram(msg):
                mark_reminder_sent(slug, reminder_type)
                log(f"{slug}: reminder 'no_date' inviato")
            continue

        # Case 2: Pubblicazione programmata per domani
        try:
            pub_date = date.fromisoformat(pub_date)
        except (ValueError, TypeError):
            log(f"{slug}: pub_date non valido: {pub_date}")
            continue

        if pub_date == tomorrow:
            reminder_type = "tomorrow"
            if reminder_already_sent(slug, reminder_type, hours=24):
                log(f"{slug}: reminder 'tomorrow' gia' inviato nelle ultime 24h — skip")
                continue

            msg = (
                f"<b>Pubblicazione domani</b>\n\n"
                f"<b>{title}</b>\n"
                f"Data: {pub_date}\n\n"
                f"Auto-publish alle 09:30 se confermato."
            )
            if send_telegram(msg):
                mark_reminder_sent(slug, reminder_type)
                log(f"{slug}: reminder 'tomorrow' inviato")
            continue

        log(f"{slug}: approvato, pub_date={pub_date} — nessun reminder necessario")


if __name__ == "__main__":
    run()
