#!/usr/bin/env python3
"""
Helper per AIena: risponde a un ticket Supabase senza gestire auth HTTP.

Uso:
  python3 supabase_ticket_reply.py <ticket_id> <content> [--close] [--status STATUS]

--status valori: ricevuto | in_analisi | lead_creato | non_rilevante
  - in_analisi   → AIena ha preso in carico, sta analizzando
  - lead_creato  → AIena ha creato un lead investigativo
  - non_rilevante → ticket archiviato come non investigabile

Esempio:
  python3 supabase_ticket_reply.py AIE-001 "Ticket ricevuto, sto analizzando" --status in_analisi
  python3 supabase_ticket_reply.py AIE-001 "Non rilevante per questa testata" --close --status non_rilevante
"""
import os
import sys, json, urllib.request, urllib.error, urllib.parse
from pathlib import Path


def _sb_service_key() -> str:
    """Legge SB_SERVICE_KEY da scripts/.aiena_secrets (fallback: variabile d'ambiente)."""
    if os.environ.get("SB_SERVICE_KEY"):
        return os.environ["SB_SERVICE_KEY"]
    secrets_file = Path("/home/pinky/.pinkybot/scripts/.aiena_secrets")
    if secrets_file.exists():
        for line in secrets_file.read_text().splitlines():
            line = line.strip()
            if line and "=" in line and not line.startswith("#"):
                k, _, v = line.partition("=")
                if k.strip() == "SB_SERVICE_KEY":
                    return v.strip()
    raise SystemExit("SB_SERVICE_KEY mancante: controlla scripts/.aiena_secrets")


SB_URL = "https://fwyjxolljcogblvwvfca.supabase.co"
SB_KEY = _sb_service_key()
HDRS = {
    "apikey": SB_KEY,
    "Authorization": "Bearer " + SB_KEY,
    "Content-Type": "application/json",
    "Prefer": "return=representation"
}

VALID_STATUSES = {"ricevuto", "in_analisi", "lead_creato", "non_rilevante"}


def resolve_uuid(ticket_code: str) -> str:
    """Resolve friendly ticket_code (es. AIE-0504-2745) to UUID.
    If already a UUID (contains hyphens and len>30), returns as-is."""
    import re
    if re.match(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', ticket_code, re.I):
        return ticket_code
    url = SB_URL + f"/rest/v1/tickets?ticket_code=eq.{urllib.parse.quote(ticket_code)}&select=id"
    req = urllib.request.Request(url, headers={k: v for k, v in HDRS.items() if k != "Prefer"})
    with urllib.request.urlopen(req, timeout=15) as r:
        rows = json.loads(r.read())
    if not rows:
        raise ValueError(f"Ticket non trovato: {ticket_code}")
    return rows[0]["id"]


def post_reply(ticket_id: str, content: str, close: bool = False,
               investigation_status: str = "") -> None:
    """Posta risposta AIena e aggiorna stato investigation/ticket.
    ticket_id può essere UUID o friendly code (es. AIE-0504-2745)."""

    # 0. Resolve to UUID if necessary
    uuid = resolve_uuid(ticket_id)

    # 1. Post message
    payload = json.dumps({
        "ticket_id": uuid,
        "author": "aiena",
        "content": content
    }).encode()
    req = urllib.request.Request(
        SB_URL + "/rest/v1/ticket_messages",
        data=payload, headers=HDRS, method="POST"
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        result = json.loads(r.read())
        print(f"OK: messaggio postato (id: {result[0].get('id','?')[:8]}...)")

    # 2. Patch ticket status/investigation_status
    patch_data: dict = {}

    if close:
        patch_data["status"] = "chiuso"

    if investigation_status:
        if investigation_status not in VALID_STATUSES:
            print(f"WARNING: investigation_status '{investigation_status}' non valido. "
                  f"Valori validi: {', '.join(sorted(VALID_STATUSES))}", file=sys.stderr)
        else:
            patch_data["investigation_status"] = investigation_status

    if patch_data:
        patch = json.dumps(patch_data).encode()
        req2 = urllib.request.Request(
            SB_URL + f"/rest/v1/tickets?id=eq.{uuid}",
            data=patch, headers=HDRS, method="PATCH"
        )
        try:
            with urllib.request.urlopen(req2, timeout=15) as r:
                fields = list(patch_data.keys())
                print(f"OK: ticket aggiornato ({', '.join(fields)})")
        except urllib.error.HTTPError as e:
            body = e.read().decode()
            # Graceful: if investigation_status column doesn't exist yet, skip it
            if "investigation_status" in body and "does not exist" in body:
                print("WARNING: colonna investigation_status non ancora presente in Supabase. "
                      "Aggiungi con: ALTER TABLE tickets ADD COLUMN investigation_status TEXT DEFAULT 'ricevuto';",
                      file=sys.stderr)
                # Retry without investigation_status if close was also requested
                if close and patch_data.get("status"):
                    retry = json.dumps({"status": "chiuso"}).encode()
                    req3 = urllib.request.Request(
                        SB_URL + f"/rest/v1/tickets?id=eq.{ticket_id}",
                        data=retry, headers=HDRS, method="PATCH"
                    )
                    with urllib.request.urlopen(req3, timeout=15) as r:
                        print("OK: ticket chiuso (senza investigation_status)")
            else:
                raise


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="AIena ticket reply helper")
    p.add_argument("ticket_id", help="ID ticket (es. AIE-0504-2745)")
    p.add_argument("content", help="Testo della risposta")
    p.add_argument("--close", action="store_true", help="Chiudi il ticket")
    p.add_argument("--status", dest="investigation_status", default="",
                   metavar="STATUS",
                   help=f"Stato investigation: {', '.join(sorted(VALID_STATUSES))}")
    args = p.parse_args()
    post_reply(args.ticket_id, args.content, args.close, args.investigation_status)
