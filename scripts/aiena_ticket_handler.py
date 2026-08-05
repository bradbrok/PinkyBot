#!/usr/bin/env python3
"""
AIena Ticket Handler v2.0 — gestisce l'intero flusso ticket in modo unificato.

Sostituisce il vecchio aiena_ticket_dispatcher.py con logica aggiornata per pipeline v2.0.

WORKFLOW:
1. Legge nuovi ticket da Supabase (senza risposta AIena)
2. Notifica Mirko su Telegram (se non gia notificato)
3. Dispatcha ad AIena via messaggio interno (se non gia dispatchato)
4. Crea placeholder in pipeline.json["leads"] per ticket investigativi

CRON: ogni 10 minuti
  */10 * * * * /home/pinky/.pinkybot/.venv/bin/python3 /home/pinky/.pinkybot/scripts/aiena_ticket_handler.py >> /home/pinky/.pinkybot/data/logs/aiena_ticket_handler.log 2>&1

State files (consolidati):
  /home/pinky/.pinkybot/data/ticket_handler_state.json
    {
      "notified": ["uuid1", ...],      # Ticket notificati a Mirko
      "dispatched": ["uuid1", ...],    # Ticket dispatchati ad AIena
      "placeholder": ["ticket_code1", ...]  # Placeholder creati in leads[]
    }
"""
import base64
import hashlib
import hmac
import json
import os
import pathlib
import sys
import tempfile
import time
import urllib.request
import urllib.error

sys.path.insert(0, "/home/pinky/lib")
import broker_auth  # noqa: E402  (path aggiunto sopra)

from datetime import datetime, timezone, date

from aiena_secrets import _load_secrets

# ============================================================================
# CONFIG
# ============================================================================

SB_URL = "https://fwyjxolljcogblvwvfca.supabase.co"
SB_KEY = _load_secrets().get("SB_SERVICE_KEY", "")
SB_HDR = {
    "apikey": SB_KEY,
    "Authorization": "Bearer " + SB_KEY,
    "Content-Type": "application/json",
}

STATE_FILE = pathlib.Path("/home/pinky/.pinkybot/data/ticket_handler_state.json")
PIPELINE_JSON = pathlib.Path("/var/www/aiena.it/data/pipeline.json")
LOG_DIR = pathlib.Path("/home/pinky/.pinkybot/data/logs")

TG_CHAT_ID = "32405655"  # Mirko
BROKER_URL = "http://localhost:8888/broker/send"
DISPATCH_URL = "http://localhost:8888/agents/aiena/message"
AGENT_NAME = "satoshi"

# ============================================================================
# INTERNAL AUTH (HMAC)
# ============================================================================

def _build_auth_headers(method: str, path: str) -> dict:
    """Build HMAC-signed headers for internal API requests.

    Delega a /home/pinky/lib/broker_auth.py (sorgente unica della firma). La
    versione precedente faceva `return {}` col secret mancante: la richiesta
    partiva non firmata e prendeva 401, senza che nulla lo segnalasse.
    broker_auth solleva invece di degradare in silenzio.
    """
    return broker_auth.build_headers(method, path, agent=AGENT_NAME)

# Tipi di ticket che vanno dispatchati ad AIena (investigativi)
INVESTIGATIVE_TYPES = {"segnalazione", "correzione", "aziende", "politica", "altro"}

# Tipi di ticket che vanno solo notificati (non dispatchati)
NOTIFY_ONLY_TYPES = {"messaggio"}

TIPO_EMOJI = {
    "segnalazione": "📋",
    "correzione": "✏️",
    "aziende": "🏢",
    "politica": "🏛️",
    "messaggio": "💬",
    "altro": "📋",
}


# ============================================================================
# STATE MANAGEMENT
# ============================================================================

def load_state() -> dict:
    """Load consolidated state."""
    default = {"notified": [], "dispatched": [], "placeholder": []}
    if STATE_FILE.exists():
        try:
            data = json.loads(STATE_FILE.read_text())
            # Ensure all keys exist
            for k in default:
                if k not in data:
                    data[k] = []
            return data
        except Exception:
            pass
    return default


def save_state(state: dict) -> None:
    """Save state atomically."""
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(state, indent=2)
    fd, tmp = tempfile.mkstemp(dir=STATE_FILE.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, 'w') as f:
            f.write(content)
        os.replace(tmp, STATE_FILE)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ============================================================================
# SUPABASE API
# ============================================================================

def sb_get(path: str) -> list:
    """GET from Supabase REST API."""
    req = urllib.request.Request(f"{SB_URL}/rest/v1/{path}", headers=SB_HDR)
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def fetch_pending_tickets() -> list[dict]:
    """
    Fetch tickets that:
    - status != 'eliminato' (soft delete)
    - status != 'chiuso' (already handled)
    - No AIena reply yet
    """
    # Get recent tickets (last 7 days to be safe)
    tickets = sb_get(
        "tickets?select=id,ticket_code,tipo,titolo,descrizione,email,allegato_url,status,created_at"
        "&status=not.eq.eliminato&status=not.eq.chiuso"
        "&order=created_at.desc&limit=100"
    )

    if not tickets:
        return []

    # Check which don't have AIena reply
    pending = []
    for t in tickets:
        tid = t["id"]
        msgs = sb_get(f"ticket_messages?ticket_id=eq.{tid}&author=eq.aiena&limit=1")
        if not msgs:
            pending.append(t)

    return pending


# ============================================================================
# PIPELINE OPERATIONS
# ============================================================================

def load_pipeline() -> dict:
    """Load pipeline.json."""
    try:
        return json.loads(PIPELINE_JSON.read_text(encoding="utf-8"))
    except Exception:
        return {"version": "2.0", "investigations": [], "leads": [], "published": []}


def save_pipeline_atomic(data: dict) -> None:
    """Save pipeline.json atomically."""
    data["updated_at"] = date.today().isoformat()
    content = json.dumps(data, ensure_ascii=False, indent=2)
    fd, tmp = tempfile.mkstemp(dir=PIPELINE_JSON.parent, suffix=".tmp", prefix="pipeline_")
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(content)
        os.replace(tmp, PIPELINE_JSON)
    except Exception as e:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise e


def create_lead_placeholder(ticket: dict) -> bool:
    """
    Create a placeholder lead in pipeline.json["leads"] for an investigative ticket.
    Returns True if created, False if already exists.
    """
    ticket_code = ticket.get("ticket_code", "")
    if not ticket_code:
        return False

    data = load_pipeline()
    leads = data.get("leads", [])

    # Check if placeholder already exists
    for lead in leads:
        if lead.get("ticket_ref") == ticket_code:
            print(f"  Placeholder gia esistente per {ticket_code}")
            return False

    # Create placeholder
    titolo = (ticket.get("titolo") or "").strip()[:60]
    tipo = ticket.get("tipo", "altro")

    placeholder = {
        "title": f"Segnalazione: {titolo}" if titolo else f"Ticket {ticket_code}",
        "slug": f"ticket-{ticket_code.lower().replace('-', '')}",
        "status": "valutazione",
        "category": tipo if tipo in ("aziende", "politica") else "segnalazione",
        "ticket_ref": ticket_code,
        "added": date.today().isoformat(),
        "sources_count": 0,
        "description": (ticket.get("descrizione") or "")[:200],
        "research_notes": [f"Da ticket {ticket_code}: {titolo}"],
        "signal_source": "aiena_ticket_handler"
    }

    # Insert at beginning of leads[]
    data.setdefault("leads", []).insert(0, placeholder)
    save_pipeline_atomic(data)
    print(f"  Placeholder creato in leads[]: {placeholder['slug']}")
    return True


# ============================================================================
# TELEGRAM NOTIFICATION
# ============================================================================

def tg_notify(text: str) -> bool:
    """Send Telegram notification to Mirko via broker."""
    try:
        payload = json.dumps({
            "agent_name": AGENT_NAME,
            "chat_id": TG_CHAT_ID,
            "platform": "telegram",
            "content": text,
        }).encode()
        headers = _build_auth_headers("POST", "/broker/send")
        req = urllib.request.Request(BROKER_URL, data=payload, headers=headers)
        urllib.request.urlopen(req, timeout=10)
        return True
    except Exception as e:
        print(f"TG notify error: {e}", file=sys.stderr)
        return False


def format_notification(ticket: dict) -> str:
    """Format ticket for Telegram notification to Mirko."""
    code = ticket.get("ticket_code", "?")
    tipo = ticket.get("tipo", "altro")
    emoji = TIPO_EMOJI.get(tipo, "📋")
    titolo = (ticket.get("titolo") or "").strip()
    descrizione = (ticket.get("descrizione") or "").strip()[:300]
    email = ticket.get("email", "")
    created = (ticket.get("created_at") or "")[:10]
    allegato = ticket.get("allegato_url", "")

    lines = [
        f"{emoji} *Nuovo ticket — AIena*",
        f"",
        f"*Codice:* `{code}`",
        f"*Tipo:* {tipo}",
        f"*Data:* {created}",
    ]

    if titolo:
        lines.append(f"")
        lines.append(f"*{titolo}*")

    if descrizione:
        lines.append(f"{descrizione}")

    if allegato:
        lines.append(f"📎 _Allegato presente_")

    if email:
        lines.append(f"")
        lines.append(f"*Email:* {email}")

    lines.append(f"")
    lines.append(f"https://admin.aiena.it (tab Ticket)")

    return "\n".join(lines)


# ============================================================================
# DISPATCH TO AIENA
# ============================================================================

def send_to_aiena(ticket: dict) -> bool:
    """Dispatch ticket to AIena via internal message broker (HMAC-signed)."""
    msg = format_dispatch_message(ticket)

    payload = json.dumps({
        "message": msg,
        "from_agent": AGENT_NAME,
    }).encode()

    try:
        headers = _build_auth_headers("POST", "/agents/aiena/message")
        req = urllib.request.Request(DISPATCH_URL, data=payload, headers=headers)
        with urllib.request.urlopen(req, timeout=120) as r:
            result = json.loads(r.read())
            return result.get("delivered", False) or result.get("queued", False)
    except Exception as e:
        print(f"Dispatch error: {e}", file=sys.stderr)
        return False


def format_dispatch_message(ticket: dict) -> str:
    """Format ticket message for AIena dispatch."""
    tipo = ticket.get("tipo", "altro")
    code = ticket.get("ticket_code", "?")
    titolo = (ticket.get("titolo") or "").strip()
    descrizione = (ticket.get("descrizione") or "").strip()
    email = ticket.get("email") or ""
    created = (ticket.get("created_at") or "")[:10]
    allegato_url = (ticket.get("allegato_url") or "").strip()
    ticket_id = ticket.get("id", "")

    allegato_block = ""
    if allegato_url:
        ext = allegato_url.split(".")[-1].lower().split("?")[0]
        if ext in ("jpg", "jpeg", "png", "webp"):
            allegato_block = f"""
📎 Allegato (immagine): {allegato_url}
→ Scarica e analizza con: import urllib.request; data = urllib.request.urlopen("{allegato_url}").read()
→ Oppure usa il tool Read per visualizzarlo direttamente."""
        elif ext == "pdf":
            allegato_block = f"""
📎 Allegato (PDF): {allegato_url}
→ Scarica con: import urllib.request; data = urllib.request.urlopen("{allegato_url}").read(); open("/tmp/allegato_{code}.pdf","wb").write(data)
→ Poi leggi con il tool Read su /tmp/allegato_{code}.pdf"""
        else:
            allegato_block = f"""
📎 Allegato: {allegato_url}
→ Scarica con: import urllib.request; data = urllib.request.urlopen("{allegato_url}").read()"""

    msg = f"""📬 Nuovo ticket da gestire — {code}

Tipo: {tipo}
Data: {created}
{"Email utente: " + email if email else ""}

Titolo: {titolo}
{descrizione}{allegato_block}

---
ISTRUZIONI:
1. {"Analizza l'allegato PRIMA di rispondere." if allegato_url else "Valuta la rilevanza del ticket."}
2. Se rilevante per indagine: il placeholder e' gia' in pipeline.json["leads"] con ticket_ref="{code}"
3. Rispondi al ticket usando: python3 /home/pinky/.pinkybot/scripts/supabase_ticket_reply.py {ticket_id} "la tua risposta" [--close]
4. Tono: professionale, diretto, investigativo — sei AIena."""

    return msg


# ============================================================================
# MAIN
# ============================================================================

def run():
    """Main handler loop."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    print(f"[{datetime.now().isoformat()}] AIena Ticket Handler v2.0")

    state = load_state()
    notified_set = set(state.get("notified", []))
    dispatched_set = set(state.get("dispatched", []))
    placeholder_set = set(state.get("placeholder", []))

    try:
        tickets = fetch_pending_tickets()
    except Exception as e:
        print(f"ERROR: Supabase fetch failed: {e}", file=sys.stderr)
        return 1

    if not tickets:
        print(f"Nessun ticket pendente.")
        return 0

    print(f"Trovati {len(tickets)} ticket pendenti (senza risposta AIena)")

    stats = {"notified": 0, "dispatched": 0, "placeholder": 0}

    for ticket in tickets:
        tid = str(ticket["id"])
        ticket_code = ticket.get("ticket_code", "")
        tipo = ticket.get("tipo", "altro")

        # 1. Notify Mirko (all tickets)
        if tid not in notified_set:
            msg = format_notification(ticket)
            if tg_notify(msg):
                notified_set.add(tid)
                stats["notified"] += 1
                print(f"  NOTIFICATO: {ticket_code} ({tipo})")
            else:
                print(f"  WARN: notifica fallita per {ticket_code}")

        # 2. Create placeholder (investigative types only)
        if tipo in INVESTIGATIVE_TYPES and ticket_code and ticket_code not in placeholder_set:
            try:
                if create_lead_placeholder(ticket):
                    placeholder_set.add(ticket_code)
                    stats["placeholder"] += 1
            except Exception as e:
                print(f"  WARN: placeholder fallito per {ticket_code}: {e}")

        # 3. Dispatch to AIena (investigative types only)
        if tipo in INVESTIGATIVE_TYPES and tid not in dispatched_set:
            if send_to_aiena(ticket):
                dispatched_set.add(tid)
                stats["dispatched"] += 1
                print(f"  DISPATCHATO: {ticket_code}")
            else:
                print(f"  WARN: dispatch fallito per {ticket_code}")
        elif tipo in NOTIFY_ONLY_TYPES:
            print(f"  SKIP dispatch (tipo={tipo}): {ticket_code}")

    # Save updated state
    state["notified"] = list(notified_set)
    state["dispatched"] = list(dispatched_set)
    state["placeholder"] = list(placeholder_set)
    save_state(state)

    print(f"Completato: {stats['notified']} notificati, {stats['dispatched']} dispatchati, {stats['placeholder']} placeholder")
    return 0


if __name__ == "__main__":
    sys.exit(run())
