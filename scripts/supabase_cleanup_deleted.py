#!/usr/bin/env python3
"""
supabase_cleanup_deleted.py — Pulizia settimanale ticket eliminati.

Elimina fisicamente dal DB Supabase tutti i ticket con status="eliminato".
I messaggi collegati vengono eliminati prima (cascade manuale).

Cron: domenica notte alle 03:00
  0 3 * * 0 /home/pinky/.pinkybot/.venv/bin/python3 /home/pinky/.pinkybot/scripts/supabase_cleanup_deleted.py >> /home/pinky/.pinkybot/data/logs/cleanup_deleted.log 2>&1
"""

import json
import os
import sys
import urllib.request
from datetime import datetime, timezone
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
}


def sb_request(path: str, method: str = "GET", body: dict | None = None) -> list:
    url = f"{SB_URL}/rest/v1/{path}"
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, headers=HDRS, method=method)
    with urllib.request.urlopen(req, timeout=15) as r:
        content = r.read()
        return json.loads(content) if content else []


def main():
    ts = datetime.now(timezone.utc).isoformat()
    print(f"[{ts}] Cleanup ticket eliminati...")

    # 1. Trova tutti i ticket con status=eliminato
    deleted = sb_request("tickets?status=eq.eliminato&select=id,ticket_code,created_at")
    if not deleted:
        print("  Nessun ticket da eliminare.")
        return

    print(f"  Trovati {len(deleted)} ticket da eliminare fisicamente.")

    for t in deleted:
        tid = t["id"]
        code = t.get("ticket_code", tid[:8])

        try:
            # Elimina prima i messaggi collegati
            sb_request(f"ticket_messages?ticket_id=eq.{tid}", method="DELETE")
            # Poi elimina il ticket
            sb_request(f"tickets?id=eq.{tid}", method="DELETE")
            print(f"  ✓ Eliminato: {code} ({tid[:8]})")
        except Exception as e:
            print(f"  ✗ Errore su {code}: {e}", file=sys.stderr)

    print(f"  Done — {len(deleted)} ticket rimossi dal DB.")


if __name__ == "__main__":
    main()
