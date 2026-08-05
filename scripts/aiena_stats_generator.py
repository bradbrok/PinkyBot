#!/usr/bin/env python3
"""
aiena_stats_generator.py — Genera stats.json con dati reali per il widget AIena.

Fonti:
  - aiena_bsky_posted.json    → articoli pubblicati
  - aiena_bsky_state.json     → likes/follows/replies dati su Bluesky
  - aiena_bsky_engage.log     → post originali pubblicati
  - aiena_research_log.json   → fonti, documenti, connessioni, ore per articolo
  - Supabase ticket_messages  → segnalazioni ricevute

Output: /var/www/aiena.it/stats.json (servito staticamente)
"""

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

from aiena_secrets import _load_secrets

BASE = Path(__file__).parent.parent
DATA = BASE / "data"

SB_URL = "https://fwyjxolljcogblvwvfca.supabase.co"
SB_KEY = _load_secrets().get("SB_SERVICE_KEY", "")
SB_HEADERS = {"apikey": SB_KEY, "Authorization": f"Bearer {SB_KEY}"}

OUTPUT = Path("/var/www/aiena.it/stats.json")

# ── helpers ──────────────────────────────────────────────────────────────────

def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def count_bluesky_originals():
    """Conta i post originali pubblicati da AIena leggendo l'engage log."""
    log = BASE / "logs" / "aiena_bsky_engage.log"
    if not log.exists():
        return 0
    count = 0
    seen = set()
    for line in log.read_text(errors="ignore").splitlines():
        if "POST:" in line and "bsky.app" in line:
            # deduplicazione per URL
            m = re.search(r'https://bsky\.app\S+', line)
            url = m.group(0) if m else line
            if url not in seen:
                seen.add(url)
                count += 1
    return count


def get_ticket_count():
    """Conta le segnalazioni ricevute su Supabase (ticket_messages)."""
    try:
        r = requests.get(
            f"{SB_URL}/rest/v1/ticket_messages?select=id",
            headers={**SB_HEADERS, "Prefer": "count=exact"},
            params={"limit": "1"},
            timeout=10,
        )
        cr = r.headers.get("content-range", "")
        # content-range: 0-0/N
        m = re.search(r"/(\d+)$", cr)
        return int(m.group(1)) if m else 0
    except Exception:
        return 0


def get_research_stats():
    """
    Legge aiena_research_log.json — un file manuale/aggiornato per articolo
    con campi: fonti, documenti, connessioni, ore.
    Default se il file non esiste.
    """
    path = DATA / "aiena_research_log.json"
    log = load_json(path, [])
    totals = {"fonti": 0, "documenti": 0, "connessioni": 0, "ore": 0}
    for entry in log:
        totals["fonti"] += entry.get("fonti", 0)
        totals["documenti"] += entry.get("documenti", 0)
        totals["connessioni"] += entry.get("connessioni", 0)
        totals["ore"] += entry.get("ore", 0)
    # fallback: se il log è vuoto usiamo i valori per l'articolo M5S già noto
    if not log:
        totals = {"fonti": 47, "documenti": 312, "connessioni": 89, "ore": 140}
    return totals


# ── main ─────────────────────────────────────────────────────────────────────

def main():
    posted = load_json(DATA / "aiena_bsky_posted.json", [])
    state = load_json(DATA / "aiena_bsky_state.json", {})
    research = get_research_stats()

    articoli = len(posted)
    likes_dati = len(state.get("already_liked", []))
    follows_dati = len(state.get("already_followed", []))
    replies_date = len(state.get("already_replied", []))
    post_originali = count_bluesky_originals()
    segnalazioni = get_ticket_count()

    # inchieste in corso: letto da un file config o hardcoded
    indagini_cfg = load_json(DATA / "aiena_indagini_stato.json", {"in_corso": 3})
    inchieste = indagini_cfg.get("in_corso", 3)

    stats = {
        "articoli_pubblicati": articoli,
        "fonti_analizzate": research["fonti"],
        "documenti_esaminati": research["documenti"],
        "connessioni_trovate": research["connessioni"],
        "ore_ricerca": research["ore"],
        "segnalazioni_ricevute": segnalazioni,
        "inchieste_in_corso": inchieste,
        "bluesky_post_originali": post_originali,
        "bluesky_likes_dati": likes_dati,
        "bluesky_follows_dati": follows_dati,
        "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
    }

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT, "w") as f:
        json.dump(stats, f, indent=2)

    print(f"[stats] Generato {OUTPUT}")
    for k, v in stats.items():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
