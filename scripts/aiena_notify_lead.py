#!/usr/bin/env python3
"""
aiena_notify_lead.py — Wrapper obbligatorio per notifica lead ad alta priorità.

REGOLA: AIena usa SEMPRE questo script per notificare Mirko di un nuovo lead.
MAI chiamare Telegram API direttamente per lead. Questo script garantisce:
  1. Il lead viene aggiunto a pipeline.json["leads"] PRIMA della notifica
  2. La scrittura viene verificata
  3. Solo dopo, la notifica Telegram viene inviata
  4. Audit trail completo

Uso:
  python3 aiena_notify_lead.py \
    --title "San Raffaele: Il Primario con 70 Morti di Troppo" \
    --slug "maisano-zurigo-san-raffaele" \
    --category "Sanità" \
    --description "Descrizione del lead..." \
    --priority altissima \
    [--ticket-ref "AIE-0507-001"] \
    [--status ricerca]
"""
import argparse
import json
import os
import sys
import tempfile
import requests
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from aiena_secrets import _load_secrets

PIPELINE_JSON = Path("/var/www/aiena.it/data/pipeline.json")
TELEGRAM_TOKEN = _load_secrets().get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = "32405655"
ROME = ZoneInfo("Europe/Rome")

PRIORITY_EMOJI = {
    "altissima": "🚨",
    "alta":      "🔴",
    "media":     "🟡",
    "bassa":     "🔵",
}


def log(msg: str):
    ts = datetime.now(ROME).strftime("%H:%M:%S")
    print(f"[notify-lead] {ts} {msg}")


def load_pipeline() -> dict:
    try:
        return json.loads(PIPELINE_JSON.read_text(encoding="utf-8"))
    except Exception as e:
        log(f"ERRORE lettura pipeline.json: {e}")
        sys.exit(1)


def save_pipeline_atomic(data: dict):
    """Scrittura atomica su pipeline.json."""
    tmp_fd, tmp_path = tempfile.mkstemp(
        dir=PIPELINE_JSON.parent,
        prefix=".tmp_pipeline_",
        suffix=".json"
    )
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, PIPELINE_JSON)
    except Exception as e:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass
        log(f"ERRORE scrittura pipeline.json: {e}")
        sys.exit(1)


def add_lead_to_pipeline(slug: str, title: str, category: str, description: str,
                          status: str, priority: str, ticket_ref: str) -> bool:
    """Aggiunge lead a pipeline.json["leads"]. Ritorna True se aggiunto (False se già esiste)."""
    data = load_pipeline()
    leads = data.setdefault("leads", [])

    # Deduplication
    existing_slugs = {l.get("slug") for l in leads}
    if slug in existing_slugs:
        log(f"Lead già presente: {slug} — skip aggiunta, procedo con notifica")
        return False

    lead = {
        "title": title,
        "slug": slug,
        "status": status,
        "category": category,
        "description": description,
        "priority_score": {"altissima": 95, "alta": 80, "media": 60, "bassa": 40}.get(priority, 60),
        "research_notes": [],
        "sources": [],
        "added": datetime.now(ROME).strftime("%Y-%m-%d"),
        "pub_date": None,
        "signal_source": "aiena_notify_lead",
    }
    if ticket_ref:
        lead["ticket_ref"] = ticket_ref

    # Inserisci in cima
    leads.insert(0, lead)

    # Salva
    save_pipeline_atomic(data)

    # Verifica scrittura
    data_check = load_pipeline()
    slugs_check = {l.get("slug") for l in data_check.get("leads", [])}
    if slug not in slugs_check:
        log(f"ERRORE CRITICO: scrittura pipeline.json fallita — {slug} non trovato dopo write")
        sys.exit(1)

    log(f"Lead aggiunto: {slug} ({len(data_check['leads'])} totali)")
    return True


def send_telegram(title: str, slug: str, category: str, description: str,
                   priority: str, ticket_ref: str):
    emoji = PRIORITY_EMOJI.get(priority, "📌")
    lines = [
        f"{emoji} <b>NUOVO LEAD — {priority.upper()}</b>",
        f"",
        f"<b>{title}</b>",
        f"Categoria: {category}",
        f"Slug: <code>{slug}</code>",
    ]
    if description:
        lines.append(f"")
        lines.append(f"{description[:200]}{'...' if len(description) > 200 else ''}")
    if ticket_ref:
        lines.append(f"🎫 Ticket: {ticket_ref}")
    lines.append(f"")
    lines.append(f"<i>Visibile in admin panel entro 5 min.</i>")

    text = "\n".join(lines)
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=10
        )
        if resp.status_code == 200:
            log("Notifica Telegram inviata ✓")
        else:
            log(f"WARN Telegram: {resp.status_code} {resp.text[:100]}")
    except Exception as e:
        log(f"ERRORE Telegram: {e}")


def trigger_admin_update():
    """Aggiorna data.json per admin panel."""
    try:
        import subprocess
        result = subprocess.run(
            [sys.executable, "/home/pinky/.pinkybot/scripts/update_admin_data.py"],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0:
            log("Admin data aggiornato ✓")
        else:
            log(f"WARN admin update: {result.stderr[:100]}")
    except Exception as e:
        log(f"WARN admin update: {e}")


def main():
    parser = argparse.ArgumentParser(description="Notifica lead ad AIena → pipeline.json → Telegram")
    parser.add_argument("--title",       required=True,  help="Titolo del lead")
    parser.add_argument("--slug",        required=True,  help="Slug univoco (kebab-case)")
    parser.add_argument("--category",    required=True,  help="Categoria (Sanità, Corruzione, Appalti...)")
    parser.add_argument("--description", default="",     help="Descrizione breve del lead")
    parser.add_argument("--priority",    default="alta",
                        choices=["altissima", "alta", "media", "bassa"],
                        help="Priorità del lead")
    parser.add_argument("--status",      default="ricerca",
                        choices=["idea", "ricerca", "valutazione", "monitoring"],
                        help="Status iniziale del lead")
    parser.add_argument("--ticket-ref",  default="",     help="Riferimento ticket Supabase (opzionale)")
    parser.add_argument("--no-notify",   action="store_true",
                        help="Aggiungi a pipeline senza inviare notifica Telegram")

    args = parser.parse_args()

    log(f"=== Lead-first flow: {args.slug} ===")

    # Step 1: Aggiungi a pipeline.json (OBBLIGATORIO — prima di tutto)
    added = add_lead_to_pipeline(
        slug=args.slug,
        title=args.title,
        category=args.category,
        description=args.description,
        status=args.status,
        priority=args.priority,
        ticket_ref=args.ticket_ref,
    )

    # Step 2: Aggiorna admin panel
    trigger_admin_update()

    # Step 3: Notifica Telegram (solo dopo scrittura verificata)
    if not args.no_notify:
        send_telegram(
            title=args.title,
            slug=args.slug,
            category=args.category,
            description=args.description,
            priority=args.priority,
            ticket_ref=args.ticket_ref,
        )

    status = "AGGIUNTO" if added else "GIA' PRESENTE"
    log(f"=== DONE ({status}) ===")


if __name__ == "__main__":
    main()
