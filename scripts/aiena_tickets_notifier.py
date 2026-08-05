#!/usr/bin/env python3
"""
AIena Tickets Notifier — controlla nuove segnalazioni errori (tipo 'correzione')
su Supabase e notifica Mirko.
Gira ogni 5 minuti via crontab.
"""
import json, pathlib, sys, urllib.request
from datetime import datetime, timezone

sys.path.insert(0, "/home/pinky/lib")
import broker_auth  # noqa: E402  (path aggiunto sopra)

from aiena_secrets import _load_secrets

SB_URL = "https://fwyjxolljcogblvwvfca.supabase.co"
SB_KEY = _load_secrets().get("SB_SERVICE_KEY", "")
SB_HDR = {
    "apikey": SB_KEY,
    "Authorization": "Bearer " + SB_KEY,
    "Content-Type": "application/json",
}

STATE_FILE = pathlib.Path("/home/pinky/.pinkybot/data/aiena_tickets_notified.json")

ARTICLE_TITLES = {
    "m5s-casaleggio-philip-morris": "Il Guru del Movimento e i Soldi di Big Tobacco",
}


def load_notified():
    if STATE_FILE.exists():
        return set(json.loads(STATE_FILE.read_text()))
    return set()


def save_notified(ids):
    STATE_FILE.write_text(json.dumps(list(ids)))


def fetch_new_tickets():
    url = (
        SB_URL
        + "/rest/v1/tickets"
        + "?select=id,ticket_code,tipo,titolo,descrizione,email,status,created_at"
        + "&order=created_at.desc"
        + "&limit=50"
    )
    req = urllib.request.Request(url, headers=SB_HDR)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())
    except Exception as e:
        print(f"Supabase error: {e}")
        return []


def tg_send(text):
    # POST firmata: senza header HMAC il daemon risponde 401. Qui si SOLLEVA di
    # proposito — prima un invio fallito veniva ignorato e il ticket finiva
    # comunque in save_notified(), cioe' era perso per sempre.
    try:
        broker_auth.send_message("32405655", text, agent="satoshi")
    except Exception as e:
        print(f"ERRORE INVIO /broker/send: {type(e).__name__}: {e}", file=sys.stderr)
        raise


TIPO_EMOJI = {
    "correzione": "✏️",
    "aziende": "🏢",
    "politica": "🏛️",
    "altro": "💬",
}


def format_ticket(t):
    code = t.get("ticket_code", "?")
    tipo = t.get("tipo", "altro")
    emoji = TIPO_EMOJI.get(tipo, "📋")
    titolo = (t.get("titolo") or "").strip()
    descrizione = (t.get("descrizione") or "").strip()[:400]
    email = t.get("email", "")
    status = t.get("status", "ricevuto")
    created = (t.get("created_at") or "")[:10]

    lines = [
        f"{emoji} *Nuova segnalazione — AIena*",
        f"",
        f"*Ticket:* `{code}`",
        f"*Tipo:* {tipo}",
        f"*Stato:* {status}",
        f"*Data:* {created}",
        f"",
        f"*{titolo}*" if titolo else "",
        f"{descrizione}" if descrizione else "",
    ]
    if email:
        lines.append(f"*Contatto:* {email}")
    # clean empty lines at end
    return "\n".join(l for l in lines if l is not None)


def run():
    notified = load_notified()
    tickets = fetch_new_tickets()

    new_tickets = [t for t in tickets if str(t.get("id")) not in notified]
    if not new_tickets:
        print(f"No new tickets ({len(tickets)} total).")
        return

    for t in new_tickets:
        msg = format_ticket(t)
        tg_send(msg)
        notified.add(str(t["id"]))
        print(f"Notified ticket: {t.get('ticket_code')} — {t.get('tipo')} on {t.get('article_slug')}")

    save_notified(notified)
    print(f"Done — {len(new_tickets)} new ticket(s) sent.")


if __name__ == "__main__":
    run()
