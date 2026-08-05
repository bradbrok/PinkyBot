#!/usr/bin/env python3
"""
One-shot: imposta decreto-commissari come urgente_pending su Supabase.
Da eseguire dopo scadenza urgente_card flotilla (8 maggio 2026 19:04 CEST).
"""
import os
import requests, sys
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
SLUG = "decreto-commissari-ponte-stretto-corte-conti"

headers = {
    "apikey": SB_KEY,
    "Authorization": f"Bearer {SB_KEY}",
    "Content-Type": "application/json",
    "Prefer": "return=representation"
}

# Verifica che urgente_card flotilla sia scaduta
import json, os
pipeline_path = "/var/www/aiena.it/data/pipeline.json"
with open(pipeline_path) as f:
    pipeline = json.load(f)

uc = pipeline.get("urgente_card")
if uc:
    slug_uc = uc.get("slug", "")
    until_str = uc.get("urgente_until", "")
    if until_str:
        from datetime import datetime
        until_dt = datetime.fromisoformat(until_str.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        if now < until_dt and slug_uc != SLUG:
            print(f"⚠️  urgente_card attiva fino a {until_dt} per '{slug_uc}' — attendi scadenza.")
            sys.exit(1)

# Aggiorna status a urgente_pending
r = requests.patch(
    f"{SB_URL}/rest/v1/article_approvals?slug=eq.{SLUG}",
    headers=headers,
    json={"status": "urgente_pending"}
)
print(f"Status: {r.status_code} — {r.text[:200]}")
if r.status_code in (200, 204):
    print("✅ decreto impostato come urgente_pending — il cron lo pubblica entro 2 minuti.")
else:
    print("❌ Errore aggiornamento Supabase")
    sys.exit(1)
