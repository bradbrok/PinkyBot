#!/usr/bin/env python3
"""
AIena Ticket Dispatcher — invia ad AIena i ticket senza risposta per la gestione autonoma.
Gira ogni 10 minuti via crontab.
"""
import json
import os
import pathlib
import tempfile
import urllib.request
from datetime import datetime, timezone, date

from aiena_secrets import _load_secrets

SB_URL = "https://fwyjxolljcogblvwvfca.supabase.co"
SB_KEY = _load_secrets().get("SB_SERVICE_KEY", "")
SB_HDR = {
    "apikey": SB_KEY,
    "Authorization": "Bearer " + SB_KEY,
    "Content-Type": "application/json",
}

STATE_FILE = pathlib.Path("/home/pinky/.pinkybot/data/aiena_ticket_dispatched.json")
PIPELINE_JSON = pathlib.Path("/var/www/aiena.it/data/pipeline.json")


def load_dispatched():
    if STATE_FILE.exists():
        return set(json.loads(STATE_FILE.read_text()))
    return set()


def save_dispatched(ids):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(list(ids)))


def load_pipeline() -> dict:
    """Load pipeline.json."""
    try:
        return json.loads(PIPELINE_JSON.read_text(encoding="utf-8"))
    except Exception:
        return {"backlog": [], "pipeline": []}


def save_pipeline_atomic(data: dict) -> None:
    """Save pipeline.json atomically (tempfile + os.replace)."""
    data["updated_at"] = date.today().isoformat()
    content = json.dumps(data, ensure_ascii=False, indent=2)
    fd, tmp_path = tempfile.mkstemp(dir=PIPELINE_JSON.parent, suffix=".tmp", prefix="pipeline_")
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(content)
        os.replace(tmp_path, PIPELINE_JSON)
    except Exception as e:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise e


def add_ticket_placeholder(ticket_code: str, ticket_subject: str) -> bool:
    """
    Add a placeholder backlog item for a ticket if not already present.
    Returns True if placeholder was added, False if already exists.
    """
    data = load_pipeline()
    backlog = data.get("backlog", [])

    # Check if ticket already has a backlog item
    for item in backlog:
        if item.get("ticket_ref") == ticket_code:
            print(f"  Placeholder gia' esistente per {ticket_code}")
            return False

    # Create placeholder
    placeholder = {
        "title": f"Segnalazione: {ticket_subject[:60]}",
        "slug": f"ticket-{ticket_code[:12]}",
        "status": "valutazione",
        "category": "segnalazione",
        "ticket_ref": ticket_code,
        "added": date.today().isoformat(),
        "sources": [],
        "research_notes": [f"Segnalazione ticket {ticket_code}: {ticket_subject}"]
    }

    data.setdefault("backlog", []).append(placeholder)
    save_pipeline_atomic(data)
    print(f"  Placeholder aggiunto al backlog: {placeholder['slug']}")
    return True


def fetch_tickets_without_response():
    """Prende ticket che non hanno ancora una risposta di AIena."""
    url = SB_URL + "/rest/v1/tickets?select=id,ticket_code,tipo,titolo,descrizione,email,allegato_url,status,created_at&order=created_at.desc&limit=50"
    req = urllib.request.Request(url, headers=SB_HDR)
    with urllib.request.urlopen(req, timeout=10) as r:
        tickets = json.loads(r.read())

    result = []
    for t in tickets:
        tid = t["id"]
        # Controlla se esiste già una risposta di AIena
        msg_url = SB_URL + f"/rest/v1/ticket_messages?ticket_id=eq.{tid}&author=eq.aiena&limit=1"
        msg_req = urllib.request.Request(msg_url, headers=SB_HDR)
        with urllib.request.urlopen(msg_req, timeout=10) as r:
            msgs = json.loads(r.read())
        if not msgs:
            result.append(t)

    return result


def send_to_agent(agent_name, message):
    payload = json.dumps({
        "message": message,
        "from_agent": "satoshi",
    }).encode()
    req = urllib.request.Request(
        f"http://localhost:8888/agents/{agent_name}/message",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            result = json.loads(r.read())
            return result.get("delivered", False)
    except Exception as e:
        print(f"Broker error: {e}")
        return False


def format_ticket_message(t):
    tipo = t.get("tipo", "altro")
    code = t.get("ticket_code", "?")
    titolo = (t.get("titolo") or "").strip()
    descrizione = (t.get("descrizione") or "").strip()
    email = t.get("email") or ""
    created = (t.get("created_at") or "")[:10]
    allegato_url = (t.get("allegato_url") or "").strip()

    allegato_block = ""
    if allegato_url:
        ext = allegato_url.split(".")[-1].lower().split("?")[0]
        if ext in ("jpg", "jpeg", "png", "webp"):
            allegato_block = f"""
📎 Allegato (immagine): {allegato_url}
→ Scarica e analizza con: import urllib.request; data = urllib.request.urlopen("{allegato_url}").read()
→ Oppure usa il tool Read per visualizzarlo direttamente se è un'immagine."""
        elif ext == "pdf":
            allegato_block = f"""
📎 Allegato (PDF): {allegato_url}
→ Scarica con: import urllib.request; data = urllib.request.urlopen("{allegato_url}").read(); open("/tmp/allegato_{code}.pdf","wb").write(data)
→ Poi leggi con il tool Read su /tmp/allegato_{code}.pdf"""
        else:
            allegato_block = f"""
📎 Allegato: {allegato_url}
→ Scarica con: import urllib.request; data = urllib.request.urlopen("{allegato_url}").read(); open("/tmp/allegato_{code}.{ext}","wb").write(data)"""

    msg = f"""📬 Nuovo ticket da gestire — {code}

Tipo: {tipo}
Data: {created}
{"Email utente: " + email if email else ""}

Titolo: {titolo}
{descrizione}{allegato_block}

---
IMPORTANTE: {"L'utente ha allegato un file — analizzalo prima di rispondere. Non chiedere documenti che sono già stati inviati." if allegato_url else ""}
Rispondi nella tabella ticket_messages con:
- author: "aiena"
- ticket_id: {t['id']}
- content: la tua risposta (tono AIena: professionale, diretto, investigativo)

Se è un lead valido: aggiungi al tuo ciclo investigativo.
Se è spam/falso: chiudi con risposta breve e professionale.
Se manca info: chiedi all'utente via ticket_messages."""

    return msg


def run():
    dispatched = load_dispatched()
    tickets = fetch_tickets_without_response()

    new = [t for t in tickets if str(t["id"]) not in dispatched]
    if not new:
        print(f"Nessun ticket da dispatciare ({len(tickets)} totali).")
        return

    for t in new:
        ticket_code = t.get("ticket_code", "")
        ticket_subject = (t.get("titolo") or "").strip()

        msg = format_ticket_message(t)
        ok = send_to_agent("aiena", msg)
        if ok:
            dispatched.add(str(t["id"]))
            print(f"Dispatciato a AIena: {ticket_code} — {t.get('tipo')}")
            # Pre-create placeholder in backlog (Fix 4)
            if ticket_code and ticket_subject:
                try:
                    add_ticket_placeholder(ticket_code, ticket_subject)
                except Exception as e:
                    print(f"  WARN: impossibile creare placeholder: {e}")
        else:
            print(f"Fallito dispatch: {ticket_code}")

    save_dispatched(dispatched)
    print(f"Done — {len(new)} ticket(s) inviati ad AIena.")


if __name__ == "__main__":
    run()
