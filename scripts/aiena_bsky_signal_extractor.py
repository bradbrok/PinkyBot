#!/usr/bin/env python3
"""
AIena Bluesky Signal Extractor — C3b: Social Signal Processor

Collega l'engagement Bluesky al sistema investigativo AIena.
Non modifica aiena_bsky_engage.py — legge il suo state file e processa
i post con cui AIena ha interagito per estrarne segnali investigativi.

Flusso:
1. Legge aiena_bsky_state.json (già_replied URIs + testi salvati)
2. Per ogni post recente con cui AIena ha interagito:
   - Estrae entità (aziende, enti, persone) dal testo
   - Confronta con KG (entità già note = boost priorità)
   - Valuta investigabilità (ANAC anomalies correlate)
3. Se entità nota nel KG → aggiorna `last_seen_at` in KG + booosta leads
4. Se entità nuova e score ≥ 0.5 → crea ticket Supabase (C3 pipeline)
5. Aggiorna il file `research_boost.json` usato dal backlog scout

Design:
- Modulare: si aggancia a bsky_engage senza toccare il codice
- Idempotente: tiene stato in own state file, non rielabora lo stesso post
- Scalabile: ogni fonte social può avere il suo extractor con stessa interfaccia

Cron: ogni 30 minuti (8:00-20:00 lun-ven, stessa cadenza di bsky_engage)
"""

import json
import os
import re
import sqlite3
import sys
import tempfile
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Import dal sistema AIena
sys.path.insert(0, str(Path(__file__).parent))
from aiena_secrets import _load_secrets
from aiena_kg.aiena_kg import KnowledgeGraph
from aiena_signals.entity_extractor import EntityExtractor

# ============================================================
# Paths e configurazione
# ============================================================

BSKY_STATE_FILE = Path("/home/pinky/.pinkybot/data/aiena_bsky_state.json")
OWN_STATE_FILE = Path("/home/pinky/.pinkybot/data/aiena_bsky_signal_state.json")
BSKY_POSTS_CACHE = Path("/home/pinky/.pinkybot/data/aiena_bsky_posts_cache.json")
KG_DB_PATH = Path("/home/pinky/.pinkybot/data/aiena_knowledge_graph.db")
PIPELINE_JSON = Path("/var/www/aiena.it/data/pipeline.json")

SUPABASE_URL = "https://fwyjxolljcogblvwvfca.supabase.co"
SUPABASE_KEY = _load_secrets().get("SB_SERVICE_KEY", "")

# Score minimo per creare ticket Supabase
MIN_SCORE_FOR_TICKET = 0.45

# Score minimo per boost backlog (entità nota nel KG)
MIN_SCORE_FOR_BOOST = 0.3

# Non rielaborare post più vecchi di N giorni
MAX_POST_AGE_DAYS = 7

# Keywords investigative che aumentano lo score
INVESTIGATIVE_KEYWORDS = [
    "appalto", "appalti", "gara", "gare", "corruzione", "corrotto",
    "pnrr", "fondi", "truffa", "truffato", "indagato", "indagine",
    "sequestro", "arresto", "rinviato a giudizio", "condannato",
    "anac", "anticorruzione", "guardia di finanza", "procura",
    "interdittiva", "antimafia", "confisca", "peculato",
    "tangente", "mazzetta", "abuso", "riciclaggio",
]


# ============================================================
# Stato
# ============================================================

def load_own_state() -> dict:
    """Carica stato processato (URI già elaborati)."""
    if OWN_STATE_FILE.exists():
        try:
            return json.loads(OWN_STATE_FILE.read_text())
        except Exception:
            pass
    return {"processed_uris": [], "last_run": None, "stats": {}}


def save_own_state(state: dict):
    """Salva stato — mantieni solo ultimi 2000 URI."""
    state["processed_uris"] = state["processed_uris"][-2000:]
    state["last_run"] = datetime.now(timezone.utc).isoformat()
    OWN_STATE_FILE.write_text(json.dumps(state, indent=2))


# ============================================================
# Lettura post da bsky_engage state
# ============================================================

def load_replied_posts() -> list[dict]:
    """
    Legge i post con cui AIena ha interagito dal cache locale.
    Il bsky_engage script salva i testi dei post nel cache file.
    """
    if not BSKY_POSTS_CACHE.exists():
        return []
    try:
        data = json.loads(BSKY_POSTS_CACHE.read_text())
        return data.get("posts", [])
    except Exception:
        return []


def load_bsky_state() -> dict:
    """Legge lo state file di bsky_engage."""
    if not BSKY_STATE_FILE.exists():
        return {}
    try:
        return json.loads(BSKY_STATE_FILE.read_text())
    except Exception:
        return {}


# ============================================================
# Scoring del segnale
# ============================================================

def compute_investigative_score(
    text: str,
    entities: list[dict],
    kg: KnowledgeGraph,
) -> tuple[float, list[str]]:
    """
    Calcola score investigativo 0-1 per un post Bluesky.
    Ritorna (score, reasons[]).
    """
    score = 0.0
    reasons = []

    text_low = text.lower()

    # 1. Keywords investigative nel testo (+0.05 ciascuna, max +0.30)
    kw_hits = [kw for kw in INVESTIGATIVE_KEYWORDS if kw in text_low]
    kw_score = min(0.30, len(kw_hits) * 0.05)
    if kw_hits:
        score += kw_score
        reasons.append(f"keywords investigative: {', '.join(kw_hits[:5])}")

    # 2. Entità nel KG → forte segnale (+0.25 per entità nota, +0.35 se sospetta)
    kg_hits = []
    for ent in entities:
        name = ent.get("name", "")
        if not name or len(name) < 3:
            continue
        matches = kg.search_entity(name, limit=2)
        for match in matches:
            dist = match.get("distance", 99)
            if dist <= 2:  # Match stretto
                entity_id = match.get("id")
                flags = _get_entity_flags(kg, entity_id)
                if flags:
                    score += 0.35
                    reasons.append(f"entità SOSPETTA nel KG: {name} ({', '.join(flags[:2])})")
                else:
                    score += 0.25
                    reasons.append(f"entità nota nel KG: {name}")
                kg_hits.append(match)

    # 3. Lunghezza testo (post più lunghi = più informativi)
    if len(text) > 200:
        score += 0.05
    if len(text) > 400:
        score += 0.05

    # 4. Entità multiple nel post (+0.10 se ≥3 entità)
    if len(entities) >= 3:
        score += 0.10
        reasons.append(f"{len(entities)} entità estratte")

    return min(1.0, round(score, 4)), reasons


def _get_entity_flags(kg: KnowledgeGraph, entity_id: int) -> list[str]:
    """Recupera flag anomalie per un'entità dal KG."""
    try:
        conn = sqlite3.connect(kg.db_path)
        c = conn.cursor()
        c.execute(
            "SELECT anomaly_type FROM anomaly_flags WHERE entity_id = ? LIMIT 5",
            (entity_id,)
        )
        flags = [row[0] for row in c.fetchall()]
        conn.close()
        return flags
    except Exception:
        return []


# ============================================================
# Creazione ticket Supabase
# ============================================================

def create_supabase_ticket(
    text: str,
    entities: list[dict],
    score: float,
    reasons: list[str],
    source_uri: str,
) -> bool:
    """Crea ticket segnalazione su Supabase dalla fonte Bluesky."""
    # Titolo sintetico
    entity_names = [e["name"] for e in entities[:3] if e.get("name")]
    title = f"[Bluesky Signal] {', '.join(entity_names)}" if entity_names else "[Bluesky Signal] Post investigativo"

    body = (
        f"Segnale investigativo rilevato su Bluesky (score: {score:.2f})\n\n"
        f"Testo originale:\n{text[:500]}\n\n"
        f"Perché è interessante:\n" + "\n".join(f"• {r}" for r in reasons) + "\n\n"
        f"Entità rilevate:\n" + "\n".join(f"• {e['name']} ({e.get('type', '?')})" for e in entities[:10])
        + f"\n\nFonte: {source_uri}"
    )

    payload = json.dumps({
        "source": "bluesky_signal",
        "title": title,
        "body": body,
        "score": score,
        "status": "new",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "metadata": json.dumps({
            "entities": [e["name"] for e in entities[:10]],
            "reasons": reasons,
            "bsky_uri": source_uri,
        })
    }).encode()

    req = urllib.request.Request(
        f"{SUPABASE_URL}/rest/v1/tickets",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Prefer": "return=minimal",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status in (200, 201)
    except Exception as e:
        print(f"[BskySignal] Errore creazione ticket: {e}")
        return False


# ============================================================
# Update backlog boost
# ============================================================

def boost_backlog_item(entity_name: str, score: float, reason: str):
    """
    Aggiorna pipeline.json per boostare elementi che menzionano questa entità.
    Aggiunge research_notes con il segnale Bluesky.
    """
    if not PIPELINE_JSON.exists():
        return

    try:
        pipeline = json.loads(PIPELINE_JSON.read_text())
        backlog = pipeline.get("leads", [])
        entity_low = entity_name.lower()

        boosted = 0
        for item in backlog:
            item_text = (
                item.get("title", "") + " " +
                item.get("description", "") + " " +
                " ".join(item.get("entities", []))
            ).lower()

            if entity_low in item_text:
                notes = item.setdefault("research_notes", [])
                note = {
                    "source": "bluesky_signal",
                    "note": f"Segnale social: {reason} (score={score:.2f})",
                    "added_at": datetime.now(timezone.utc).isoformat(),
                }
                # Non duplicare
                existing = [n.get("note", "") for n in notes]
                if note["note"] not in existing:
                    notes.append(note)
                    boosted += 1

        if boosted > 0:
            # Scrittura atomica: tempfile + os.replace per evitare corruzione
            tmp_fd, tmp_path = tempfile.mkstemp(dir=PIPELINE_JSON.parent, suffix=".tmp")
            try:
                with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                    f.write(json.dumps(pipeline, indent=2, ensure_ascii=False))
                os.replace(tmp_path, str(PIPELINE_JSON))
            except Exception:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
            print(f"[BskySignal] Boostato {boosted} item in pipeline per entità: {entity_name}")

    except Exception as e:
        print(f"[BskySignal] Errore boost backlog: {e}")


# ============================================================
# Main
# ============================================================

def process_post(
    uri: str,
    text: str,
    extractor: EntityExtractor,
    kg: KnowledgeGraph,
    own_state: dict,
) -> dict | None:
    """
    Processa un singolo post Bluesky.
    Ritorna un dict con i risultati, o None se non rilevante.
    """
    # Estrai entità
    entities = extractor.extract(text)
    # Filtra entità troppo corte o troppo generiche
    entities = [
        e for e in entities
        if e.get("name") and len(e["name"]) >= 4 and len(e["name"]) <= 80
    ]

    # Calcola score
    score, reasons = compute_investigative_score(text, entities, kg)

    if score < MIN_SCORE_FOR_BOOST:
        return None

    result = {
        "uri": uri,
        "text": text[:300],
        "entities": entities,
        "score": score,
        "reasons": reasons,
        "processed_at": datetime.now(timezone.utc).isoformat(),
    }

    # Boost backlog per entità note nel KG
    for entity in entities:
        name = entity.get("name", "")
        if not name:
            continue
        matches = kg.search_entity(name, limit=1)
        if matches and matches[0].get("distance", 99) <= 2:
            boost_backlog_item(name, score, "; ".join(reasons[:2]))

    # Crea ticket Supabase se score alto e ha entità significative
    if score >= MIN_SCORE_FOR_TICKET and entities:
        created = create_supabase_ticket(text, entities, score, reasons, uri)
        result["ticket_created"] = created
        if created:
            print(f"[BskySignal] Ticket creato per post score={score:.2f}: {text[:80]}...")

    return result


def run():
    """Entry point principale."""
    print(f"[BskySignal] {datetime.now().strftime('%H:%M:%S')} — avvio")

    own_state = load_own_state()
    processed_uris = set(own_state["processed_uris"])

    # Carica post dalla cache Bluesky
    posts = load_replied_posts()
    if not posts:
        print("[BskySignal] Nessun post in cache Bluesky — verificare bsky_engage")
        # Uscita pulita: niente da processare
        return

    # Inizializza estrattore entità e KG
    try:
        extractor = EntityExtractor()
        kg = KnowledgeGraph(KG_DB_PATH)
    except Exception as e:
        print(f"[BskySignal] Errore init: {e}")
        return

    results = []
    new_uris = []

    for post in posts:
        uri = post.get("uri", "")
        text = post.get("text", "")
        if not uri or not text:
            continue
        if uri in processed_uris:
            continue

        result = process_post(uri, text, extractor, kg, own_state)
        new_uris.append(uri)

        if result:
            results.append(result)
            print(
                f"[BskySignal] Segnale rilevato (score={result['score']:.2f}): "
                f"{text[:60]}... | entità: {[e['name'] for e in result['entities'][:3]]}"
            )

    # Aggiorna stato
    own_state["processed_uris"].extend(new_uris)
    own_state["stats"]["total_processed"] = own_state["stats"].get("total_processed", 0) + len(new_uris)
    own_state["stats"]["total_signals"] = own_state["stats"].get("total_signals", 0) + len(results)
    save_own_state(own_state)

    print(
        f"[BskySignal] DONE — processati {len(new_uris)} post, "
        f"{len(results)} segnali rilevati"
    )


if __name__ == "__main__":
    run()
