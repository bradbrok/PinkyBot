#!/usr/bin/env python3
"""
Monitor aiena.it Tor hidden service (.onion)
Verifica HTTP 200 su porta 9080 locale (proxy Tor → nginx)
Alert via Telegram se down.
"""

import subprocess
import requests
import sys
import os
import json
import tempfile
from datetime import datetime

# Config
LOCAL_TOR_PORT = 9080
LOCAL_URL = "http://127.0.0.1:9080/"
ONION_URL = "http://obvorhi24h6ekd3rdjxuekcrd2endtcp37g6gxtfucxxp3o6vrjrzvad.onion/"
STATE_FILE = os.environ.get(
    "AIENA_TOR_MONITOR_STATE",
    "/home/pinky/.pinkybot/scripts/aiena_tor_monitor_state.json",
)

TS_FMT = "%Y-%m-%d %H:%M:%S"

# Ogni quante ore ri-allertare finche' il servizio resta down.
# Un singolo alert "una volta per evento" ha nascosto 77 giorni di down (mag-ago 2026):
# il primo alert parte subito, poi si ripete finche' qualcuno non interviene.
REALERT_HOURS = float(os.environ.get("AIENA_TOR_REALERT_HOURS", "24"))

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
MIRKO_CHAT_ID = "32405655"
AIENA_CHAT_ID = None  # notify aiena agent via Pinky if needed


def send_telegram(chat_id: str, text: str) -> bool:
    """Send Telegram notification via Pinky relay. True se l'invio e' riuscito.

    Il ritorno conta: l'orologio del re-alert va avanzato solo su invio riuscito,
    altrimenti un relay che fallisce in silenzio consuma l'alert e il down torna
    invisibile per REALERT_HOURS.
    """
    if not TELEGRAM_BOT_TOKEN:
        # Try via pinky relay script
        r = subprocess.run(
            ["python3", "/home/pinky/.pinkybot/scripts/claude-sdk-relay.py"],
            input=json.dumps({"chat_id": chat_id, "text": text}),
            capture_output=True, text=True, timeout=10
        )
        if r.returncode != 0:
            raise RuntimeError(f"relay uscito con {r.returncode}: {(r.stderr or '').strip()[:200]}")
        return True
    import urllib.request
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    data = json.dumps({"chat_id": chat_id, "text": text}).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=10)
    return True


def load_state() -> dict:
    state = {"last_status": "unknown", "down_since": None, "last_alert_at": None, "alert_count": 0}
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                state.update(json.load(f))
        except (json.JSONDecodeError, OSError):
            # Stato illeggibile: riparti pulito invece di crashare e perdere il giro.
            return state
    # Migrazione dal vecchio schema booleano (alert_sent) al timestamp.
    if state.get("last_alert_at") is None and state.pop("alert_sent", False):
        # Timestamp reale ignoto: usa down_since, cosi' il re-alert scatta subito
        # invece di restare zitto come faceva il vecchio schema.
        state["last_alert_at"] = state.get("down_since")
        state["alert_count"] = state.get("alert_count") or 1
    state.pop("alert_sent", None)
    return state


def save_state(state: dict):
    """Scrittura atomica: su questo file si regge tutta la memoria del monitor."""
    d = os.path.dirname(STATE_FILE) or "."
    fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
        os.chmod(tmp, 0o600)
        os.replace(tmp, STATE_FILE)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, TS_FMT)
    except (ValueError, TypeError):
        return None


def _format_duration(since: datetime | None, now: datetime) -> str:
    """Durata leggibile del down — e' l'informazione che mancava nei re-alert."""
    if since is None:
        return "sconosciuta"
    total = int((now - since).total_seconds())
    if total < 0:
        return "sconosciuta"
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}g {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def should_alert(state: dict, now: datetime) -> bool:
    """Primo down -> alert subito. Poi ogni REALERT_HOURS finche' non torna up."""
    last_alert = _parse_ts(state.get("last_alert_at"))
    if last_alert is None:
        return True
    return (now - last_alert).total_seconds() >= REALERT_HOURS * 3600


def check_local_port() -> tuple[bool, int]:
    """Check if nginx is serving on 9080."""
    try:
        r = requests.get(LOCAL_URL, timeout=5)
        return r.status_code == 200, r.status_code
    except Exception as e:
        return False, 0


def check_tor_service() -> bool:
    """Check if tor@default systemd service is running."""
    result = subprocess.run(
        ["systemctl", "is-active", "tor@default"],
        capture_output=True, text=True
    )
    return result.stdout.strip() == "active"


def main():
    now_dt = datetime.now()
    now = now_dt.strftime(TS_FMT)
    state = load_state()

    ok, status_code = check_local_port()
    tor_active = check_tor_service()

    if ok and tor_active:
        # Service is up
        if state.get("last_status") == "down" and state.get("last_alert_at"):
            # Recovery — notify
            downtime = _format_duration(_parse_ts(state.get("down_since")), now_dt)
            msg = (
                f"✅ aiena.onion — RIPRISTINATO\n"
                f"Servizio Tor tornato operativo.\n"
                f"Durata del down: {downtime}\n"
                f"Ora: {now}"
            )
            try:
                send_telegram(MIRKO_CHAT_ID, msg)
            except Exception:
                pass

        state["last_status"] = "up"
        state["down_since"] = None
        state["last_alert_at"] = None
        state["alert_count"] = 0
        save_state(state)
        print(f"[{now}] OK — nginx:9080={status_code}, tor={tor_active}")
        return 0

    else:
        # Something is down
        if state.get("last_status") != "down":
            state["down_since"] = now

        state["last_status"] = "down"

        # Build diagnostic
        issues = []
        if not tor_active:
            issues.append("Tor service non attivo")
        if not ok:
            issues.append(f"nginx:9080 risponde {status_code or 'timeout'}")

        print(f"[{now}] DOWN — {', '.join(issues)}")

        # Alert subito al primo down, poi ogni REALERT_HOURS finche' resta giu'.
        if should_alert(state, now_dt):
            downtime = _format_duration(_parse_ts(state.get("down_since")), now_dt)
            n = (state.get("alert_count") or 0) + 1
            header = "🔴 aiena.onion — DOWN" if n == 1 else f"🔴 aiena.onion — ANCORA DOWN (avviso #{n})"
            msg = (
                f"{header}\n"
                f"Problemi: {', '.join(issues)}\n"
                f"Down da: {state.get('down_since', now)} ({downtime})\n"
                f"Prossimo avviso tra {REALERT_HOURS:g}h se non rientra.\n"
                f"Verifica: systemctl status tor@default && curl http://127.0.0.1:9080/"
            )
            try:
                send_telegram(MIRKO_CHAT_ID, msg)
                # Solo su invio riuscito: altrimenti si riprova al giro dopo.
                state["last_alert_at"] = now
                state["alert_count"] = n
            except Exception as e:
                print(f"Alert send failed: {e}")

        save_state(state)
        return 1


if __name__ == "__main__":
    sys.exit(main())
