#!/usr/bin/env python3
"""
AIena Web Enricher — Arricchimento entità via ricerca web.

Prende le top aziende dai leads (RECIDIVO, CONCENTRAZIONE_VALORE) e cerca:
- "{azienda} amministratore" → relazioni AMMINISTRA nel KG
- "{azienda} indagine appalti" → fonti investigative

Backend:
  1. Google CSE (API key — 100 req/day FREE)
     → Set: GOOGLE_CSE_KEY e GOOGLE_CSE_CX env vars
  2. DuckDuckGo HTML scraping (fallback, ~5s delay tra richieste)

Cache SQLite locale — TTL 30 giorni, idempotente.
Integra con KG: aggiunge entità PERSONA + relazione AMMINISTRA.

Uso:
    python3 aiena_web_enricher.py                   # Top 20 lead
    python3 aiena_web_enricher.py --limit 10        # Top 10
    python3 aiena_web_enricher.py --dry-run         # No KG write
    python3 aiena_web_enricher.py --company "CASTAF SRL"  # Singola
    python3 aiena_web_enricher.py --stats           # Report utilizzo API
"""

import argparse
import json
import os
import re
import sqlite3
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ============================================================
# Config
# ============================================================

LEADS_JSON = Path("/var/www/aiena.it/data/leads.json")
KG_DB_PATH = Path("/home/pinky/.pinkybot/data/aiena_knowledge_graph.db")
CACHE_DB_PATH = Path("/home/pinky/.pinkybot/data/aiena_web_cache.db")

# Google CSE — imposta via env vars
GOOGLE_CSE_KEY = os.environ.get("GOOGLE_CSE_KEY", "")
GOOGLE_CSE_CX = os.environ.get("GOOGLE_CSE_CX", "")

# Config
CACHE_TTL_DAYS = 30
MAX_RESULTS_PER_QUERY = 5       # result per query CSE (max 10 free)
DAILY_LIMIT_CSE = 90            # lascia margine su 100
DDGO_DELAY_SECONDS = 5.0        # delay tra richieste DuckDuckGo
DEFAULT_TOP_N = 20              # lead da processare per run

# Pattern regex per nomi italiani (approssimativo ma efficace)
ITALIAN_NAME_PATTERN = re.compile(
    r"\b([A-ZÀÈÉÌÒÙ][a-zàèéìòùä']+(?:\s+[A-ZÀÈÉÌÒÙ][a-zàèéìòùä']+){1,3})\b"
)

# Parole da escludere dai nomi (troppo generici)
NAME_STOP_WORDS = {
    "Tribunale", "Comune", "Regione", "Provincia", "Ministero", "Guardia",
    "Finanza", "Procura", "Repubblica", "Italia", "Itali", "Corte",
    "Prefettura", "Camera", "Senato", "Agenzia", "Impresa", "Imprese",
    "Autorità", "Anticorruzione", "Polizia", "Carabinieri", "Vigili",
    "Consiglio", "Giunta", "Assessore", "Sindaco", "Presidente",
    "Direttore", "Amministratore", "Delegato", "Socio", "Titolare",
    "Responsabile", "Referente", "Sistema", "Servizio", "Servizi",
    "Gestione", "Costruzioni", "Lavori", "Opere", "Forniture",
    "Appalto", "Appalti", "Contratto", "Contratti", "Gara", "Gare",
    "Pubblica", "Privata", "Generale", "Nazionale", "Locale",
    "Monday", "Tuesday", "January", "February",  # EN words that slip through
}

# Suffissi societari — se il nome termina con questi NON è una persona fisica
CORPORATE_SUFFIXES = {
    "Srl", "Spa", "Snc", "Sas", "Scarl", "Coop", "Scrl", "Sapa",
    "Onlus", "Aps", "Ets", "Srls", "Srlus",
}

# Keywords che indicano informazioni investigative rilevanti
INVESTIGATIVE_PATTERNS = [
    (re.compile(r"indaga[to|ti|ta]", re.I), "indagato"),
    (re.compile(r"arrest[o|ato|ati|ata]", re.I), "arrestato"),
    (re.compile(r"sequestra[to|ti|ta]", re.I), "sequestro"),
    (re.compile(r"corruzion[e|i]", re.I), "corruzione"),
    (re.compile(r"truffa", re.I), "truffa"),
    (re.compile(r"peculato", re.I), "peculato"),
    (re.compile(r"turbativa", re.I), "turbativa d'asta"),
    (re.compile(r"guardia di finanza", re.I), "GdF"),
    (re.compile(r"procura della repubblica", re.I), "procura"),
    (re.compile(r"rinviato a giudizio", re.I), "rinviato a giudizio"),
    (re.compile(r"interdittiva antimafia", re.I), "interdittiva antimafia"),
    (re.compile(r"misura cautelare", re.I), "misura cautelare"),
    (re.compile(r"confisca", re.I), "confisca"),
]


# ============================================================
# Cache DB
# ============================================================

def init_cache_db() -> sqlite3.Connection:
    """Inizializza il database di cache."""
    conn = sqlite3.connect(str(CACHE_DB_PATH), timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS search_cache (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            query TEXT NOT NULL,
            backend TEXT NOT NULL DEFAULT 'ddgo',
            results_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(query, backend)
        );

        CREATE TABLE IF NOT EXISTS api_usage (
            date TEXT NOT NULL,
            backend TEXT NOT NULL,
            requests INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (date, backend)
        );

        CREATE TABLE IF NOT EXISTS enrichment_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_name TEXT NOT NULL,
            query_type TEXT NOT NULL,
            persons_found TEXT,
            investigation_flags TEXT,
            kg_updated INTEGER DEFAULT 0,
            processed_at TEXT NOT NULL,
            UNIQUE(company_name, query_type)
        );
    """)
    conn.commit()
    return conn


def cache_get(conn: sqlite3.Connection, query: str, backend: str) -> list | None:
    """Legge dalla cache. Ritorna None se scaduta o assente."""
    row = conn.execute(
        "SELECT results_json, created_at FROM search_cache WHERE query=? AND backend=?",
        (query, backend)
    ).fetchone()
    if not row:
        return None
    # Controlla TTL
    created = datetime.fromisoformat(row[1])
    if datetime.now(timezone.utc) - created > timedelta(days=CACHE_TTL_DAYS):
        conn.execute("DELETE FROM search_cache WHERE query=? AND backend=?", (query, backend))
        return None
    return json.loads(row[0])


def cache_set(conn: sqlite3.Connection, query: str, backend: str, results: list):
    """Salva in cache."""
    conn.execute(
        """INSERT OR REPLACE INTO search_cache (query, backend, results_json, created_at)
           VALUES (?, ?, ?, ?)""",
        (query, backend, json.dumps(results), datetime.now(timezone.utc).isoformat())
    )
    conn.commit()


def usage_today(conn: sqlite3.Connection, backend: str) -> int:
    """Quante richieste oggi per questo backend."""
    today = datetime.now().strftime("%Y-%m-%d")
    row = conn.execute(
        "SELECT requests FROM api_usage WHERE date=? AND backend=?",
        (today, backend)
    ).fetchone()
    return row[0] if row else 0


def usage_increment(conn: sqlite3.Connection, backend: str):
    """Incrementa contatore uso giornaliero."""
    today = datetime.now().strftime("%Y-%m-%d")
    conn.execute(
        """INSERT INTO api_usage (date, backend, requests) VALUES (?, ?, 1)
           ON CONFLICT(date, backend) DO UPDATE SET requests = requests + 1""",
        (today, backend)
    )
    conn.commit()


# ============================================================
# Search backends
# ============================================================

def search_google_cse(query: str, cache_conn: sqlite3.Connection) -> list[dict]:
    """
    Cerca via Google Custom Search Engine API.
    Ritorna lista di {title, link, snippet}.
    """
    if not GOOGLE_CSE_KEY or not GOOGLE_CSE_CX:
        return []

    # Check cache
    cached = cache_get(cache_conn, query, "cse")
    if cached is not None:
        return cached

    # Check daily limit
    used = usage_today(cache_conn, "cse")
    if used >= DAILY_LIMIT_CSE:
        print(f"[WebEnricher] CSE limit raggiunto ({used}/{DAILY_LIMIT_CSE}) — fallback DuckDuckGo")
        return []

    params = urllib.parse.urlencode({
        "key": GOOGLE_CSE_KEY,
        "cx": GOOGLE_CSE_CX,
        "q": query,
        "num": MAX_RESULTS_PER_QUERY,
        "lr": "lang_it",
        "gl": "it",
    })
    url = f"https://www.googleapis.com/customsearch/v1?{params}"

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "AIena/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        usage_increment(cache_conn, "cse")

        results = []
        for item in data.get("items", []):
            results.append({
                "title": item.get("title", ""),
                "link": item.get("link", ""),
                "snippet": item.get("snippet", ""),
            })

        cache_set(cache_conn, query, "cse", results)
        return results

    except Exception as e:
        print(f"[WebEnricher] CSE error: {e}")
        return []


def search_duckduckgo(query: str, cache_conn: sqlite3.Connection) -> list[dict]:
    """
    Cerca via DuckDuckGo Lite HTML (fallback, no API key).
    Usa lite.duckduckgo.com che è più stabile e meno soggetto a CAPTCHA.
    """
    cached = cache_get(cache_conn, query, "ddgo")
    if cached is not None:
        return cached

    encoded = urllib.parse.quote_plus(query)
    url = f"https://lite.duckduckgo.com/lite/?q={encoded}&kl=it-it"

    try:
        req = urllib.request.Request(
            url,
            data=urllib.parse.urlencode({"q": query, "kl": "it-it"}).encode(),
            headers={
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:120.0) Gecko/20100101 Firefox/120.0",
                "Accept-Language": "it-IT,it;q=0.9",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            html = resp.read().decode("utf-8", errors="replace")

        # Controlla CAPTCHA / blocco
        if "anomaly" in html.lower() or "captcha" in html.lower() or len(html) < 2000:
            print(f"[WebEnricher] DuckDuckGo blocco rilevato — skip")
            return []

        results = []

        # DDG Lite: struttura HTML stabile con class='result-link' e class='result-snippet'
        # Estrai link
        link_pat = re.compile(r"class='result-link'[^>]*>(?:<[^>]+>)*([^<]+)</a>.*?href=\"(https?://[^\"]+)\"", re.S)
        # Approccio alternativo: trova href dentro result-link
        result_links = re.findall(
            r'href="(https?://[^"]+)"[^>]*class=\'result-link\'>([^<]+)</a>', html
        )
        if not result_links:
            # Fallback: qualsiasi href su risultato
            result_links = re.findall(
                r'<a[^>]+class=\'result-link\'[^>]*href="(https?://[^"]+)"[^>]*>([^<]+)</a>', html
            )
        if not result_links:
            result_links = re.findall(
                r'class=\'result-link\'.*?href="(https?://[^"]+)".*?>([^<]+)<', html, re.S
            )

        # Estrai snippet
        snippets = re.findall(
            r"class='result-snippet'[^>]*>(.*?)</td>", html, re.S
        )
        snippets_clean = [re.sub(r"<[^>]+>", "", s).strip()[:300] for s in snippets]

        for i, item in enumerate(result_links[:10]):
            if len(item) == 2:
                link, title = item
            else:
                continue
            if "duckduckgo.com" in link:
                continue
            results.append({
                "title": title.strip(),
                "link": link,
                "snippet": snippets_clean[i] if i < len(snippets_clean) else "",
            })

        if results:
            cache_set(cache_conn, query, "ddgo", results)
            usage_increment(cache_conn, "ddgo")
        time.sleep(DDGO_DELAY_SECONDS)
        return results

    except Exception as e:
        print(f"[WebEnricher] DuckDuckGo error: {e}")
        return []


def search(query: str, cache_conn: sqlite3.Connection) -> tuple[list[dict], str]:
    """
    Sceglie il backend migliore: CSE se disponibile e con quota, altrimenti DDGo.
    Ritorna (results, backend_used).
    """
    if GOOGLE_CSE_KEY and GOOGLE_CSE_CX:
        results = search_google_cse(query, cache_conn)
        if results:
            return results, "cse"

    results = search_duckduckgo(query, cache_conn)
    return results, "ddgo"


# ============================================================
# Estrazione entità dal testo
# ============================================================

def extract_persons(texts: list[str], company_name: str = "") -> list[str]:
    """
    Estrae nomi di persone fisiche dai testi di snippet/titoli.

    Strategia 1 — context-aware (più affidabile):
      Cerca nomi dopo keyword come "amministratore", "titolare", "socio", ecc.

    Strategia 2 — frequenza:
      Nomi che appaiono ≥2 volte nel testo aggregato (riduce falsi positivi).
    """
    candidates: dict[str, int] = {}  # nome → frequenza
    context_hits: set[str] = set()   # nomi trovati in contesto → alta confidenza

    # Keyword contestuali per trovare nomi di persone fisiche
    CONTEXT_PATTERNS = [
        re.compile(
            r"(?:amministratore(?:\s+unico|\s+delegato)?|titolare|socio(?:\s+unico)?|"
            r"legale\s+rappresentante|rappresentante\s+legale|ceo|presidente|"
            r"fondatore|direttore\s+generale)\s*[:\-–]?\s*"
            r"([A-ZÀÈÉÌÒÙ][a-zàèéìòùä']+(?:\s+[A-ZÀÈÉÌÒÙ][a-zàèéìòùä']+){1,2})",
            re.I
        ),
        # Pattern "Mario Rossi, amministratore" (nome PRIMA del ruolo)
        re.compile(
            r"([A-ZÀÈÉÌÒÙ][a-zàèéìòùä']+\s+[A-ZÀÈÉÌÒÙ][a-zàèéìòùä']+)"
            r"\s*[,–\-]\s*(?:amministratore|titolare|socio|presidente|ceo|"
            r"legale rappresentante|fondatore)",
            re.I
        ),
    ]

    full_text = " ".join(texts)

    # Strategia 1: context-aware
    for pattern in CONTEXT_PATTERNS:
        for match in pattern.finditer(full_text):
            name = match.group(1).strip()
            parts = name.split()
            if len(parts) < 2 or len(parts) > 3:
                continue
            if any(p in NAME_STOP_WORDS for p in parts):
                continue
            if any(len(p) < 3 for p in parts):
                continue
            if name == name.upper():
                continue
            if parts[-1] in CORPORATE_SUFFIXES:
                continue
            # Escludi il nome dell'azienda stessa
            if company_name and name.upper() in company_name.upper():
                continue
            context_hits.add(name)
            candidates[name] = candidates.get(name, 0) + 3  # boost contestuale

    # Strategia 2: frequenza (solo nomi che appaiono ≥2 volte)
    for match in ITALIAN_NAME_PATTERN.finditer(full_text):
        name = match.group(1).strip()
        parts = name.split()
        # Solo 2 parole per la strategia di frequenza (Cognome + Nome)
        if len(parts) != 2:
            continue
        if any(p in NAME_STOP_WORDS for p in parts):
            continue
        if any(len(p) < 3 for p in parts):
            continue
        if name == name.upper():
            continue
        if parts[-1] in CORPORATE_SUFFIXES:
            continue
        if company_name and name.upper() in company_name.upper():
            continue
        candidates[name] = candidates.get(name, 0) + 1

    # Filtra strategia 2: mantieni solo quelli con frequenza ≥2 O in context_hits
    result = []
    seen = set()
    for name in sorted(candidates, key=lambda n: -candidates[n]):
        if name in seen:
            continue
        if name in context_hits or candidates[name] >= 2:
            result.append(name)
            seen.add(name)

    return result[:5]  # max 5 persone per azienda


def extract_investigation_flags(texts: list[str]) -> list[str]:
    """Estrae flag investigative dai testi (indagato, arrestato, ecc.)."""
    full_text = " ".join(texts).lower()
    found = []
    for pattern, label in INVESTIGATIVE_PATTERNS:
        if pattern.search(full_text):
            found.append(label)
    return found


# ============================================================
# KG integration
# ============================================================

def kg_add_person_relation(
    company_name: str,
    person_name: str,
    relation_type: str = "AMMINISTRA",
    source: str = "WEB_SEARCH",
    confidence: float = 0.5,
    dry_run: bool = False,
) -> bool:
    """
    Aggiunge al KG:
    - Entità PERSONA (person_name)
    - Relazione AMMINISTRA: PERSONA → AZIENDA
    Idempotente.
    """
    now = datetime.now(timezone.utc).isoformat()

    if dry_run:
        print(f"  [DRY-RUN] KG: {person_name} --{relation_type}--> {company_name}")
        return True

    try:
        conn = sqlite3.connect(str(KG_DB_PATH), timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.row_factory = sqlite3.Row

        def get_or_create_entity(name: str, etype: str) -> int:
            n = " ".join(name.upper().split())
            row = conn.execute("SELECT id FROM entities WHERE name=?", (n,)).fetchone()
            if row:
                return row["id"]
            cur = conn.execute(
                """INSERT INTO entities
                   (name, name_original, entity_type, metadata, confidence,
                    first_seen_at, last_updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (n, name, etype, "{}", confidence, now, now)
            )
            return cur.lastrowid

        person_id = get_or_create_entity(person_name, "PERSONA")
        company_id = get_or_create_entity(company_name, "AZIENDA")

        # Controlla se relazione esiste già
        exists = conn.execute(
            """SELECT 1 FROM relations
               WHERE entity1_id=? AND entity2_id=? AND relation_type=? LIMIT 1""",
            (person_id, company_id, relation_type)
        ).fetchone()

        if not exists:
            conn.execute(
                """INSERT INTO relations
                   (entity1_id, entity2_id, relation_type, source_type,
                    confidence, is_suspicious, created_at, weight)
                   VALUES (?, ?, ?, ?, ?, 0, ?, ?)""",
                (person_id, company_id, relation_type, source,
                 confidence, now, confidence)
            )
            conn.commit()
            conn.close()
            return True

        conn.close()
        return False  # già esistente

    except Exception as e:
        print(f"  [KG] Errore: {e}")
        return False


def kg_add_investigation_source(
    company_name: str,
    flag: str,
    url: str,
    dry_run: bool = False,
) -> bool:
    """Aggiunge fonte investigativa all'entità nel KG."""
    now = datetime.now(timezone.utc).isoformat()

    if dry_run:
        print(f"  [DRY-RUN] KG source: {company_name} | {flag} | {url[:60]}")
        return True

    try:
        conn = sqlite3.connect(str(KG_DB_PATH), timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")

        n = " ".join(company_name.upper().split())
        row = conn.execute("SELECT id FROM entities WHERE name=?", (n,)).fetchone()
        if not row:
            conn.close()
            return False
        entity_id = row[0]

        # Controlla se fonte già presente
        exists = conn.execute(
            "SELECT 1 FROM entity_sources WHERE entity_id=? AND source_url=? LIMIT 1",
            (entity_id, url[:500])
        ).fetchone() if _table_exists(conn, "entity_sources") else None

        if not exists:
            # Salva in metadata dell'entità (entity_sources potrebbe non esistere)
            # → aggiorniamo il campo metadata con le fonti investigative
            meta_row = conn.execute("SELECT metadata FROM entities WHERE id=?", (entity_id,)).fetchone()
            try:
                meta = json.loads(meta_row[0]) if meta_row and meta_row[0] else {}
            except Exception:
                meta = {}

            sources = meta.get("investigation_sources", [])
            entry = {"flag": flag, "url": url[:300], "added_at": now}
            if entry not in sources:
                sources.append(entry)
                meta["investigation_sources"] = sources[-10:]  # keep last 10
                conn.execute(
                    "UPDATE entities SET metadata=?, last_updated_at=? WHERE id=?",
                    (json.dumps(meta, ensure_ascii=False), now, entity_id)
                )
                conn.commit()

        conn.close()
        return True

    except Exception as e:
        print(f"  [KG] Source error: {e}")
        return False


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


# ============================================================
# Core enrichment
# ============================================================

def enrich_company(
    company_name: str,
    cache_conn: sqlite3.Connection,
    dry_run: bool = False,
    verbose: bool = True,
) -> dict:
    """
    Arricchisce un'azienda cercando su web.
    Ritorna un dict con persons, flags, sources.
    """
    result = {
        "company": company_name,
        "persons": [],
        "investigation_flags": [],
        "sources": [],
        "kg_persons_added": 0,
        "kg_sources_added": 0,
    }

    if verbose:
        print(f"\n  [{company_name}]")

    queries = [
        (f'{company_name} amministratore', "admin"),
        (f'{company_name} appalti indagine corruzione', "investigation"),
    ]

    all_texts = []
    all_sources = []

    for query, qtype in queries:
        results, backend = search(query, cache_conn)
        if verbose:
            print(f"    {qtype}: {len(results)} risultati [{backend}]")

        for r in results:
            text = f"{r.get('title','')} {r.get('snippet','')}"
            all_texts.append(text)
            if r.get("link"):
                all_sources.append((r["link"], r.get("title", ""), qtype))

    # Estrai persone
    persons = extract_persons(all_texts, company_name=company_name)[:5]  # max 5 per company
    result["persons"] = persons

    # Estrai flag investigative
    flags = extract_investigation_flags(all_texts)
    result["investigation_flags"] = flags

    if verbose and persons:
        print(f"    Persone rilevate: {', '.join(persons)}")
    if verbose and flags:
        print(f"    Flag investigative: {', '.join(flags)}")

    # Aggiorna KG
    for person in persons:
        added = kg_add_person_relation(
            company_name, person,
            relation_type="AMMINISTRA",
            source="WEB_SEARCH",
            confidence=0.4,  # bassa: dati non verificati
            dry_run=dry_run,
        )
        if added:
            result["kg_persons_added"] += 1

    for url, title, qtype in all_sources[:3]:
        for flag in flags:
            added = kg_add_investigation_source(
                company_name, flag, url, dry_run=dry_run
            )
            if added:
                result["kg_sources_added"] += 1
                break

    result["sources"] = [{"url": u, "title": t} for u, t, _ in all_sources[:5]]

    # Log nel cache DB
    if not dry_run:
        cache_conn.execute(
            """INSERT OR REPLACE INTO enrichment_log
               (company_name, query_type, persons_found, investigation_flags,
                kg_updated, processed_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                company_name, "full",
                json.dumps(persons),
                json.dumps(flags),
                1 if (result["kg_persons_added"] + result["kg_sources_added"]) > 0 else 0,
                datetime.now(timezone.utc).isoformat(),
            )
        )
        cache_conn.commit()

    return result


# ============================================================
# Carica lead da leads.json
# ============================================================

def load_top_leads(n: int = DEFAULT_TOP_N) -> list[dict]:
    """Carica i top N lead da leads.json ordinati per scoop_score."""
    if not LEADS_JSON.exists():
        print(f"[WebEnricher] leads.json non trovato: {LEADS_JSON}")
        return []

    leads = json.loads(LEADS_JSON.read_text())
    if isinstance(leads, dict):
        leads = leads.get("leads", [])

    # Priorità: RECIDIVO prima, poi CONCENTRAZIONE_VALORE
    recidivo = [l for l in leads if l.get("pattern") == "RECIDIVO"]
    others = [l for l in leads if l.get("pattern") != "RECIDIVO"]

    ordered = sorted(recidivo, key=lambda x: -x.get("scoop_score", 0)) + \
              sorted(others, key=lambda x: -x.get("scoop_score", 0))

    return ordered[:n]


def get_company_name_from_lead(lead: dict) -> str:
    """Estrae il nome dell'azienda da un lead."""
    # Campo principale
    # Prova campi standard
    for field in ("supplier_name", "company", "entity_name", "name"):
        val = lead.get(field, "")
        if val and len(val) > 2:
            return val.strip()
    # Prova campo entities (lista)
    entities = lead.get("entities", [])
    if entities and isinstance(entities, list) and entities[0]:
        return entities[0].strip()
    # Fallback: cerca nel title_seed o description
    for field in ("title_seed", "description", "pattern_detail"):
        desc = lead.get(field, "")
        if not desc:
            continue
        # Cerca nome azienda tutto caps con forma giuridica
        m = re.search(r"([A-Z][A-Z\s&\.\-\']{3,}(?:S\.?R\.?L\.?|S\.?P\.?A\.?|S\.?N\.?C\.?|S\.?A\.?S\.?|SCARL|COOP|SOC\.|SRL|SPA))", desc)
        if m:
            return m.group(1).strip()
    return ""


# ============================================================
# Stats report
# ============================================================

def print_stats(cache_conn: sqlite3.Connection):
    """Stampa statistiche uso API e enrichment."""
    print("\n=== STATS UTILIZZO ===")

    # API usage
    rows = cache_conn.execute(
        "SELECT date, backend, requests FROM api_usage ORDER BY date DESC LIMIT 14"
    ).fetchall()
    print("\nUtilizzo API (ultimi 14 giorni):")
    for row in rows:
        print(f"  {row[0]} | {row[1]:<6} | {row[2]} richieste")

    # Cache hits
    total_cached = cache_conn.execute("SELECT COUNT(*) FROM search_cache").fetchone()[0]
    print(f"\nEntry in cache: {total_cached}")

    # Enrichment log
    enriched = cache_conn.execute(
        "SELECT COUNT(*) FROM enrichment_log WHERE kg_updated=1"
    ).fetchone()[0]
    total_enriched = cache_conn.execute("SELECT COUNT(*) FROM enrichment_log").fetchone()[0]
    print(f"Aziende processate: {total_enriched} ({enriched} con aggiornamento KG)")

    # Persone trovate nel KG
    kg_persons = sqlite3.connect(str(KG_DB_PATH)).execute(
        "SELECT COUNT(*) FROM entities WHERE entity_type='PERSONA'"
    ).fetchone()[0]
    kg_admin_rels = sqlite3.connect(str(KG_DB_PATH)).execute(
        "SELECT COUNT(*) FROM relations WHERE relation_type='AMMINISTRA'"
    ).fetchone()[0]
    print(f"KG: {kg_persons} persone, {kg_admin_rels} relazioni AMMINISTRA")


# ============================================================
# Main
# ============================================================

def run(
    top_n: int = DEFAULT_TOP_N,
    company: str = "",
    dry_run: bool = False,
    stats_only: bool = False,
):
    print(f"[WebEnricher] {datetime.now().strftime('%H:%M:%S')} — avvio")
    print(f"  Backend: {'Google CSE + DDGo' if (GOOGLE_CSE_KEY and GOOGLE_CSE_CX) else 'DuckDuckGo (no CSE key)'}")
    if dry_run:
        print("  MODALITÀ DRY-RUN — KG non modificato")

    cache_conn = init_cache_db()

    if stats_only:
        print_stats(cache_conn)
        return

    # Determina lista aziende da processare
    if company:
        companies = [{"supplier_name": company}]
    else:
        leads = load_top_leads(top_n)
        if not leads:
            print("[WebEnricher] Nessun lead trovato")
            return
        companies = leads
        print(f"  Lead da processare: {len(companies)}")

    # Check quota CSE rimanente
    if GOOGLE_CSE_KEY:
        used_today = usage_today(cache_conn, "cse")
        print(f"  CSE quota oggi: {used_today}/{DAILY_LIMIT_CSE} usate")

    stats = {
        "processed": 0,
        "persons_found": 0,
        "flags_found": 0,
        "kg_persons": 0,
        "kg_sources": 0,
        "skipped_no_name": 0,
    }

    results_all = []

    for lead in companies:
        name = company if company else get_company_name_from_lead(lead)
        if not name or len(name) < 3:
            stats["skipped_no_name"] += 1
            continue

        result = enrich_company(name, cache_conn, dry_run=dry_run, verbose=True)
        results_all.append(result)

        stats["processed"] += 1
        stats["persons_found"] += len(result["persons"])
        stats["flags_found"] += len(result["investigation_flags"])
        stats["kg_persons"] += result["kg_persons_added"]
        stats["kg_sources"] += result["kg_sources_added"]

    # Riepilogo
    cache_conn.close()

    print("\n" + "=" * 50)
    print(f"{'DRY-RUN ' if dry_run else ''}COMPLETATO")
    print(f"  Aziende processate:    {stats['processed']}")
    print(f"  Persone rilevate:      {stats['persons_found']}")
    print(f"  Flag investigative:    {stats['flags_found']}")
    print(f"  Entità KG aggiunte:    {stats['kg_persons']}")
    print(f"  Fonti KG aggiunte:     {stats['kg_sources']}")

    if stats["flags_found"] > 0:
        flagged = [r for r in results_all if r["investigation_flags"]]
        print(f"\n⚠️  Aziende con flag investigative ({len(flagged)}):")
        for r in flagged:
            print(f"  • {r['company']}: {', '.join(r['investigation_flags'])}")

    return results_all


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=DEFAULT_TOP_N)
    parser.add_argument("--company", type=str, default="")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--stats", action="store_true")
    args = parser.parse_args()

    run(
        top_n=args.limit,
        company=args.company,
        dry_run=args.dry_run,
        stats_only=args.stats,
    )
