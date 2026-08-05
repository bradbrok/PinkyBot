#!/usr/bin/env python3
"""
AIena Tips Notifier — controlla nuovi contributi su Supabase e notifica Mirko.
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

STATE_FILE = pathlib.Path("/home/pinky/.pinkybot/data/aiena_tips_notified.json")

ARTICLE_TITLES = {
    "m5s-casaleggio-philip-morris": "Il Guru del Movimento e i Soldi di Big Tobacco",
}

TIPO_LABELS = {
    "fonte": "📎 Fonte pubblica",
    "documento": "📄 Documento",
    "aggiornamento": "🔄 Aggiornamento",
    "altro": "💬 Altro",
}


def load_notified():
    if STATE_FILE.exists():
        return set(json.loads(STATE_FILE.read_text()))
    return set()


def save_notified(ids):
    STATE_FILE.write_text(json.dumps(list(ids)))


def fetch_pending():
    url = (
        SB_URL
        + "/rest/v1/article_tips"
        + "?status=eq.pending&select=id,article_slug,tipo,contenuto,url,email,created_at"
        + "&order=created_at.desc"
    )
    req = urllib.request.Request(url, headers=SB_HDR)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())
    except Exception as e:
        print(f"Supabase error: {e}")
        return []


def tg_send(text):
    # POST firmata: senza header HMAC il daemon risponde 401 e la notifica
    # non arriva mai (broker_auth solleva su secret mancante e su non-2xx).
    try:
        broker_auth.send_message("32405655", text, agent="satoshi")
    except Exception as e:
        print(f"ERRORE INVIO /broker/send: {type(e).__name__}: {e}", file=sys.stderr)
        raise


def format_tip(tip):
    slug = tip.get("article_slug", "?")
    title = ARTICLE_TITLES.get(slug, slug)
    tipo = TIPO_LABELS.get(tip.get("tipo", "altro"), "💬 Altro")
    contenuto = tip.get("contenuto", "").strip()[:400]
    url = tip.get("url", "")
    email = tip.get("email", "")
    created = tip.get("created_at", "")[:10]

    lines = [
        f"📬 *Nuovo contributo — AIena*",
        f"",
        f"*Articolo:* {title}",
        f"*Tipo:* {tipo}",
        f"*Data:* {created}",
        f"",
        f"{contenuto}",
    ]
    if url:
        lines.append(f"*Link:* {url}")
    if email:
        lines.append(f"*Contatto:* {email}")
    lines.append(f"")
    lines.append(f"_Per approvare: Supabase → article\\_tips → cambia status in `approvato`_")
    return "\n".join(lines)


def run():
    notified = load_notified()
    tips = fetch_pending()

    new_tips = [t for t in tips if str(t.get("id")) not in notified]
    if not new_tips:
        print(f"No new tips ({len(tips)} pending total).")
        return

    for tip in new_tips:
        msg = format_tip(tip)
        tg_send(msg)
        notified.add(str(tip["id"]))
        print(f"Notified: {tip['id']} — {tip.get('tipo')} on {tip.get('article_slug')}")

    save_notified(notified)
    print(f"Done — {len(new_tips)} new tip(s) sent.")


if __name__ == "__main__":
    run()
