#!/usr/bin/env python3
"""
promote_lead.py — Promuove un lead a investigation in modo atomico.

Gestisce tutto:
1. Sposta il lead da leads[] a investigations[] in pipeline.json
2. Assegna priority (prossimo disponibile)
3. INSERT record in Supabase article_approvals (status=pending)
4. Rebuild admin panel

Uso:
  python3 promote_lead.py <slug> [pub_date]
  python3 promote_lead.py sorrento-appalti-2026 2026-07-14

Se pub_date non specificata, viene impostata a +14 giorni da oggi (prossimo martedì utile).

Autore: Satoshi — 2026-05-31
"""
import json
import os
import sys
import datetime
import urllib.request
import urllib.error
import subprocess
from pathlib import Path
from zoneinfo import ZoneInfo


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


PIPELINE_JSON   = Path("/var/www/aiena.it/data/pipeline.json")
SUPABASE_URL    = "https://fwyjxolljcogblvwvfca.supabase.co"
SUPABASE_KEY    = _sb_service_key()
ADMIN_REBUILD   = Path("/home/pinky/.pinkybot/scripts/aiena_admin_rebuild.py")

ROME_TZ = ZoneInfo("Europe/Rome")


def log(msg: str):
    now = datetime.datetime.now(ROME_TZ).strftime("%H:%M:%S")
    print(f"[promote_lead] {now} {msg}")


def next_tuesday(from_date: datetime.date) -> datetime.date:
    """Prossimo martedì (pub day BMN/AIena) da from_date."""
    days_ahead = 1 - from_date.weekday()  # martedì = 1
    if days_ahead <= 0:
        days_ahead += 7
    return from_date + datetime.timedelta(days=days_ahead)


def load_pipeline() -> dict:
    return json.loads(PIPELINE_JSON.read_text(encoding="utf-8"))


def save_pipeline(p: dict):
    PIPELINE_JSON.write_text(
        json.dumps(p, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )


def find_lead(p: dict, slug: str) -> dict | None:
    for lead in p.get("leads", []):
        if lead.get("slug") == slug:
            return lead
    return None


def find_investigation(p: dict, slug: str) -> dict | None:
    for inv in p.get("investigations", []):
        if inv.get("slug") == slug:
            return inv
    return None


def next_priority(p: dict) -> int:
    existing = [inv.get("priority") for inv in p.get("investigations", [])
                if isinstance(inv.get("priority"), int)]
    return max(existing, default=0) + 1


def supabase_insert(slug: str, title: str, category: str, pub_date: str) -> bool:
    """INSERT in article_approvals con service key (bypassa RLS)."""
    payload = {
        "slug": slug,
        "title": title,
        "category": category,
        "approved_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "status": "pending",
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{SUPABASE_URL}/rest/v1/article_approvals",
        data=data,
        headers={
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json",
            "Prefer": "return=minimal",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req) as r:
            log(f"Supabase INSERT OK — status {r.status}")
            return True
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        log(f"Supabase INSERT ERROR: HTTP {e.code} — {body[:300]}")
        return False


def supabase_already_exists(slug: str) -> bool:
    req = urllib.request.Request(
        f"{SUPABASE_URL}/rest/v1/article_approvals?slug=eq.{slug}&select=slug",
        headers={
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
        },
    )
    try:
        with urllib.request.urlopen(req) as r:
            data = json.loads(r.read())
            return len(data) > 0
    except Exception:
        return False


def promote(slug: str, pub_date_str: str | None = None) -> bool:
    p = load_pipeline()

    # 1. Trova il lead
    lead = find_lead(p, slug)
    if not lead:
        # Controlla se è già un'investigation
        if find_investigation(p, slug):
            log(f"WARN: '{slug}' è già in investigations[]")
            return False
        log(f"ERROR: slug '{slug}' non trovato in leads[]")
        return False

    log(f"Lead trovato: {lead.get('title', slug)[:60]}")

    # 2. Calcola pub_date
    if pub_date_str:
        pub_date = datetime.date.fromisoformat(pub_date_str)
    else:
        # Prossimo martedì disponibile (+14 giorni minimo)
        from_date = datetime.date.today() + datetime.timedelta(days=14)
        pub_date = next_tuesday(from_date)
        pub_date_str = pub_date.isoformat()
        log(f"pub_date non specificata → {pub_date_str} (prossimo martedì utile)")

    # 3. Assegna priority
    priority = next_priority(p)
    log(f"Priority assegnata: {priority}")

    # 4. Costruisci investigation entry
    inv_entry = {
        "title": lead.get("title", ""),
        "slug": slug,
        "status": "approvato",
        "category": lead.get("category", ""),
        "pub_date": pub_date_str,
        "publish_day": pub_date_str,
        "priority": priority,
        "ticket_ref": lead.get("ticket_ref"),
        "added": lead.get("added", datetime.date.today().isoformat()),
    }
    # Preserva campi opzionali se presenti
    for field in ("research_notes", "sources", "sources_count", "diary", "hashtags", "description"):
        if lead.get(field):
            inv_entry[field] = lead[field]

    # 5. Sposta da leads[] a investigations[]
    p["leads"] = [l for l in p.get("leads", []) if l.get("slug") != slug]
    p.setdefault("investigations", []).append(inv_entry)
    save_pipeline(p)
    log(f"pipeline.json aggiornato: lead → investigation, priority={priority}, pub_date={pub_date_str}")

    # 6. INSERT Supabase (idempotente)
    if supabase_already_exists(slug):
        log(f"Supabase: record già esistente per '{slug}' — skip INSERT")
    else:
        ok = supabase_insert(slug, inv_entry["title"], inv_entry["category"], pub_date_str)
        if not ok:
            log("WARN: Supabase INSERT fallito — pipeline.json aggiornato ma record Supabase mancante")

    # 7. Rebuild admin
    log("Avvio rebuild admin panel...")
    try:
        result = subprocess.run(
            [sys.executable, str(ADMIN_REBUILD)],
            capture_output=True, text=True, timeout=60
        )
        if result.returncode == 0:
            log("Admin rebuild OK")
        else:
            log(f"Admin rebuild WARN: {result.stderr[-200:]}")
    except Exception as e:
        log(f"Admin rebuild ERROR: {e}")

    log(f"✅ Promozione completata: {slug} → investigation priority={priority} pub={pub_date_str}")
    return True


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Uso: python3 promote_lead.py <slug> [pub_date YYYY-MM-DD]")
        sys.exit(1)

    slug_arg = sys.argv[1]
    pub_arg = sys.argv[2] if len(sys.argv) > 2 else None
    success = promote(slug_arg, pub_arg)
    sys.exit(0 if success else 1)
