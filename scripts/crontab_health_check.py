#!/usr/bin/env python3
"""
crontab_health_check.py — Monitor completo per tutti i cron attivi.
Gira ogni 30 minuti. Alert Telegram solo se un cron PASSA da OK ad ALERT
(no spam ripetuto). Digest mattutino separato (gestito da aiena_daily_brief).
"""
import json
import requests
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

TELEGRAM_TOKEN = "8591673518:AAFbH2XpFB_e1geoKvWB1W-UdvmWVoaUK-8"
TELEGRAM_CHAT_ID = "32405655"
ROME = ZoneInfo("Europe/Rome")
STATE_FILE = Path("/home/pinky/.pinkybot/data/cron_health_state.json")

# ── Cron registry ────────────────────────────────────────────────────────────
# max_age_minutes: quanto tempo prima di considerarlo ALERT
# skip_hours: ore in cui è normale che non giri (es. cron solo notturni)
SCRIPTS = [
    # ── ogni 2 minuti ────────────────────────────────────────────────────────
    {"name": "watchdog",              "log": "/home/pinky/watchdog/watchdog.log",
     "max_age": 6,   "desc": "Watchdog sistema"},

    # ── ogni 5 minuti ────────────────────────────────────────────────────────
    {"name": "ticket_handler",         "log": "/home/pinky/.pinkybot/logs/aiena_tickets.log",
     "max_age": 12,  "desc": "AIena ticket handler (unified)"},
    {"name": "tips_notifier",         "log": "/home/pinky/.pinkybot/data/aiena_tips_notifier.log",
     "max_age": 12,  "desc": "AIena tips notifier"},
    {"name": "update_admin_data",     "log": "/home/pinky/.pinkybot/data/logs/admin_data.log",
     "max_age": 12,  "desc": "Admin panel data refresh"},
    # DISABLED: tickets_notifier_v2 merged into ticket_handler
    {"name": "rebuild_magazine",      "log": "/var/www/bitcoinmarket.net/logs/magazine_rebuild.log",
     "max_age": 12,  "desc": "BitcoinMarket magazine rebuild"},
    # dnd_watchdog scrive solo su errori — monitorato tramite dnd_health_check

    # ── ogni 10 minuti ───────────────────────────────────────────────────────
    # DISABLED 2026-05-07: ticket_dispatcher cron sospeso per migrazione v2 pipeline
    # {"name": "ticket_dispatcher",     "log": "/home/pinky/.pinkybot/data/aiena_ticket_dispatcher.log",
    #  "max_age": 20,  "desc": "AIena ticket dispatcher"},
    # DISABLED 2026-05-20: dnd-ai-master superseded by Taverna2
    # {"name": "dnd_health_check",      "log": "/home/pinky/projects/dnd-ai-master/logs/health_check.log",
    #  "max_age": 20,  "desc": "DnD backend health check"},

    # ── ogni 15 minuti ───────────────────────────────────────────────────────
    {"name": "email_monitor",         "log": "/home/pinky/.pinkybot/scripts/reports/email_monitor.log",
     "max_age": 25,  "desc": "Email monitor"},

    # ── ogni 30 minuti ───────────────────────────────────────────────────────
    {"name": "bitcoinmarket_check",   "log": "/home/pinky/scripts/bitcoinmarket_check.log",
     "max_age": 45,  "desc": "BitcoinMarket check"},
    {"name": "btc_signal_updater",    "log": "/var/log/btc-signal.log",
     "max_age": 45,  "desc": "BTC signal updater"},

    # ── ogni ora (:30) ───────────────────────────────────────────────────────
    {"name": "bsky_engage",           "log": "/home/pinky/.pinkybot/logs/aiena_bsky_engage.log",
     "max_age": 80,  "desc": "AIena Bluesky engagement"},
    {"name": "nostr_publish",         "log": "/home/pinky/.pinkybot/data/aiena_nostr_publish.log",
     "max_age": 80,  "desc": "AIena Nostr publish"},
    {"name": "admin_rebuild",         "log": "/home/pinky/.pinkybot/logs/aiena_admin_rebuild.log",
     "max_age": 80,  "desc": "AIena admin rebuild"},
    {"name": "ots_verify",            "log": "/home/pinky/.pinkybot/data/aiena_ots_verify.log",
     "max_age": 80,  "desc": "AIena OTS verify"},

    # ── giornalieri ──────────────────────────────────────────────────────────
    {"name": "qa_watchdog",           "log": "/home/pinky/.pinkybot/scripts/reports/qa_watchdog.log",
     "max_age": 26*60, "desc": "QA watchdog"},
    {"name": "affiliate_watchdog",    "log": "/home/pinky/.pinkybot/scripts/reports/affiliate_watchdog.log",
     "max_age": 26*60, "desc": "Affiliate link watchdog"},
    {"name": "sentinel",              "log": "/home/pinky/.pinkybot/scripts/reports/sentinel.log",
     "max_age": 26*60, "desc": "Sentinel site audit"},
    {"name": "source_monitor",        "log": "/home/pinky/.pinkybot/data/aiena_monitor.log",
     "max_age": 26*60, "desc": "AIena source monitor"},
    {"name": "git_backup",            "log": "/home/pinky/.pinkybot/logs/aiena_git_backup.log",
     "max_age": 26*60, "desc": "AIena git backup",    "skip_if_no_log": True},
    {"name": "daily_report",          "log": "/home/pinky/.pinkybot/logs/aiena_daily_report.log",
     "max_age": 26*60, "desc": "AIena daily report"},
    {"name": "aiena_stats_generator", "log": "/home/pinky/.pinkybot/logs/aiena_stats.log",
     "max_age": 26*60, "desc": "AIena stats generator", "skip_if_no_log": True},
    {"name": "reflink_checker",       "log": "/home/pinky/scripts/bitcoinmarket_reflink_checker.log",
     "max_age": 26*60, "desc": "BitcoinMarket reflink checker"},
    {"name": "dnd_db_backup",         "log": "/home/pinky/projects/dnd-ai-master/backups/backup.log",
     "max_age": 26*60, "desc": "DnD DB backup"},
]

# ── helpers ──────────────────────────────────────────────────────────────────

def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def save_state(state: dict):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


def send_alert(message: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": message,
                                 "parse_mode": "HTML"}, timeout=8)
    except Exception as e:
        print(f"  TELEGRAM ERROR: {e}")


def check_one(script: dict, now: datetime) -> tuple[bool, str]:
    """Ritorna (ok, reason)."""
    log = Path(script["log"])
    if not log.exists():
        if script.get("skip_if_no_log"):
            return True, "ok (primo run atteso)"
        return False, "log non trovato"
    age_min = (now - datetime.fromtimestamp(log.stat().st_mtime, tz=ROME)).total_seconds() / 60
    if age_min > script["max_age"]:
        return False, f"silenzioso da {int(age_min)}min (soglia {script['max_age']}min)"
    return True, "ok"


# ── main ─────────────────────────────────────────────────────────────────────

def main():
    now = datetime.now(ROME)
    print(f"[{now.strftime('%H:%M')}] Crontab health check — {len(SCRIPTS)} script monitorati")

    state = load_state()
    new_alerts = []
    recovered = []

    for s in SCRIPTS:
        ok, reason = check_one(s, now)
        was_alert = state.get(s["name"]) == "ALERT"
        status = "OK" if ok else "ALERT"
        print(f"  {s['name']}: {status}" + (f" ({reason})" if not ok else ""))

        if not ok and not was_alert:
            new_alerts.append((s["desc"], reason, s["log"]))
        elif ok and was_alert:
            recovered.append(s["desc"])

        state[s["name"]] = status

    save_state(state)

    # alert solo per NUOVI problemi
    if new_alerts:
        lines = ["⚠️ <b>Cron ALERT</b>"]
        for desc, reason, log in new_alerts:
            lines.append(f"• {desc}\n  → {reason}")
        send_alert("\n".join(lines))

    # recovery silenzioso (solo log, no telegram)
    for desc in recovered:
        print(f"  RECOVERED: {desc}")

    total_alert = sum(1 for v in state.values() if v == "ALERT")
    if total_alert:
        print(f"  [{total_alert} script in ALERT]")
    else:
        print("  Tutti gli script attivi.")


if __name__ == "__main__":
    main()
