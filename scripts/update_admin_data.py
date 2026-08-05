#!/usr/bin/env python3
"""
update_admin_data.py - Genera admin/data.json per AIena admin panel

Questo script genera un file JSON statico con:
- Stato AIena (heartbeat dal DB locale)
- Articoli in preview (/preview/*.html)
- Articoli pubblicati (/articles/*.html)

Il file viene deployato via FTP su aiena.it/admin/data.json

USO:
    python3 /home/pinky/.pinkybot/scripts/update_admin_data.py

QUANDO CHIAMARLO:
    - Satoshi dovrebbe invocarlo dopo ogni:
      (a) nuovo articolo deployato in /preview/
      (b) dopo ogni pubblicazione (spostamento da preview -> articles)
      (c) periodicamente (es. ogni 5 minuti via cron) per aggiornare heartbeat

    Esempio cron:
      */5 * * * * python3 /home/pinky/.pinkybot/scripts/update_admin_data.py

DIPENDENZE:
    - sqlite3 (stdlib)
    - subprocess per curl FTP
    - Accesso a /var/www/aiena.it/
    - Accesso a ~/.pinkybot/data/conversations_agents.db
"""

import json
import os
import re
import sqlite3
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone, date
from pathlib import Path

# cron bootstrap: cron non eredita PINKY_SESSION_SECRET dall'ambiente del daemon.
_DAEMON_ENV = Path("/home/pinky/.pinkybot/.env")
if not os.environ.get("PINKY_SESSION_SECRET") and _DAEMON_ENV.exists():
    for _ln in _DAEMON_ENV.read_text().splitlines():
        _ln = _ln.strip()
        if _ln and "=" in _ln and not _ln.startswith("#"):
            _ek, _, _ev = _ln.partition("=")
            _k = _ek.strip()
            _v = _ev.strip().strip('"').strip("'")
            if not os.environ.get(_k):
                os.environ[_k] = _v

# Configurazione
AIENA_ROOT = Path("/var/www/aiena.it")
PREVIEW_DIR = AIENA_ROOT / "preview"
ARTICLES_DIR = AIENA_ROOT / "articles"
ADMIN_DIR = AIENA_ROOT / "admin"
DATA_JSON_PATH = ADMIN_DIR / "data.json"

DB_PATH = Path.home() / ".pinkybot" / "data" / "conversations_agents.db"
FTP_DEPLOY_SCRIPT = Path.home() / ".pinkybot" / "scripts" / "ftp_deploy_verify.sh"

AIENA_PREVIEW_URL = "https://aiena.it/preview"
AIENA_ARTICLES_URL = "https://aiena.it/articles"


def extract_title_from_html(filepath: Path) -> str:
    """Estrae il titolo da un file HTML, rimuovendo ' | AIena' suffix."""
    try:
        content = filepath.read_text(encoding="utf-8", errors="ignore")
        # Cerca <title>...</title>
        match = re.search(r"<title>([^<]+)</title>", content, re.IGNORECASE)
        if match:
            title = match.group(1).strip()
            # Rimuovi suffix " | AIena"
            if title.endswith(" | AIena"):
                title = title[:-8].strip()
            return title
        # Fallback: cerca <h1>
        match = re.search(r"<h1[^>]*>([^<]+)</h1>", content, re.IGNORECASE)
        if match:
            return match.group(1).strip()
    except Exception as e:
        print(f"Warning: cannot read {filepath}: {e}", file=sys.stderr)
    return filepath.stem.replace("-", " ").title()


def get_file_mtime_iso(filepath: Path) -> str:
    """Ritorna la data di modifica del file in formato ISO."""
    mtime = filepath.stat().st_mtime
    return datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()


def scan_articles(
    directory: Path,
    base_url: str,
    article_type: str,
    pipeline_titles: dict | None = None,
) -> list[dict]:
    """Scansiona una directory per file .html e ritorna lista di articoli.

    pipeline_titles: dict slug -> title da pipeline.json (fonte di verità).
    Se presente, il titolo viene letto da pipeline.json e l'HTML viene usato
    solo come fallback (evita titoli stale quando pipeline.json viene aggiornato).
    """
    articles = []
    if not directory.exists():
        return articles

    for html_file in directory.glob("*.html"):
        slug = html_file.stem
        # Pipeline.json è la fonte di verità per i titoli
        title = (
            (pipeline_titles or {}).get(slug)
            or extract_title_from_html(html_file)
        )
        mtime = get_file_mtime_iso(html_file)
        url = f"{base_url}/{slug}.html"

        date_key = "uploaded_at" if article_type == "preview" else "published_at"
        articles.append({
            "slug": slug,
            "title": title,
            date_key: mtime,
            "url": url
        })

    # Ordina per data (piu recenti prima)
    date_key = "uploaded_at" if article_type == "preview" else "published_at"
    articles.sort(key=lambda x: x[date_key], reverse=True)
    return articles


def get_aiena_status() -> dict:
    """Legge lo stato di AIena dal database SQLite."""
    status = {
        "enabled": False,
        "last_heartbeat": None,
        "status": "unknown",
        "notes": ""
    }

    if not DB_PATH.exists():
        print(f"Warning: DB not found at {DB_PATH}", file=sys.stderr)
        return status

    try:
        conn = sqlite3.connect(str(DB_PATH))
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        # Stato agent
        cursor.execute("SELECT enabled FROM agents WHERE name = 'aiena'")
        row = cursor.fetchone()
        if row:
            status["enabled"] = bool(row["enabled"])

        # Ultimo heartbeat
        cursor.execute("""
            SELECT timestamp, status, notes
            FROM agent_heartbeats
            WHERE agent_name = 'aiena'
            ORDER BY timestamp DESC
            LIMIT 1
        """)
        row = cursor.fetchone()
        if row:
            # timestamp e' un float unix epoch
            ts = datetime.fromtimestamp(row["timestamp"], tz=timezone.utc)
            status["last_heartbeat"] = ts.isoformat()
            status["status"] = row["status"]
            status["notes"] = row["notes"] or ""

        conn.close()
    except Exception as e:
        print(f"Warning: DB error: {e}", file=sys.stderr)

    return status


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
SB_HEADERS = {"apikey": SB_KEY, "Authorization": f"Bearer {SB_KEY}"}

PIPELINE_JSON = Path("/var/www/aiena.it/data/pipeline.json")


def sb_get(path: str) -> list:
    """Fetch from Supabase REST API."""
    req = urllib.request.Request(f"{SB_URL}/rest/v1/{path}", headers=SB_HEADERS)
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def sb_patch(path: str, data: dict) -> bool:
    """PATCH to Supabase REST API. Returns True on success."""
    body = json.dumps(data).encode()
    headers = {**SB_HEADERS, "Content-Type": "application/json", "Prefer": "return=minimal"}
    req = urllib.request.Request(
        f"{SB_URL}/rest/v1/{path}", data=body, headers=headers, method="PATCH"
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status in (200, 204)
    except Exception as e:
        print(f"[sb_patch] Error: {e}")
        return False


def sync_pub_dates_to_supabase(pipeline_data: dict) -> int:
    """
    Sync pub_date from pipeline.json investigations[] → Supabase article_approvals.publish_date.
    pipeline.json (admin panel) è l'unica fonte di verità per le date di pubblicazione.
    Ritorna il numero di record aggiornati.
    """
    investigations = pipeline_data.get("investigations", [])
    updated = 0
    for item in investigations:
        slug = item.get("slug")
        pub_date = item.get("pub_date")
        if not slug or not pub_date:
            continue
        try:
            ok = sb_patch(
                f"article_approvals?slug=eq.{slug}",
                {"publish_date": pub_date}
            )
            if ok:
                updated += 1
        except Exception as e:
            print(f"[sync_pub_dates] Error syncing {slug}: {e}")
    return updated


def fetch_tickets_with_messages() -> list[dict]:
    """Recupera ticket + messaggi da Supabase con join manuale."""
    try:
        # Escludi ticket con status "eliminato" (soft delete da pannello admin)
        tickets = sb_get("tickets?select=*&status=not.eq.eliminato&order=created_at.desc&limit=50")
        if not tickets:
            return []
        # Fetch messages for all tickets in one call
        ids = ",".join(f'"{t["id"]}"' for t in tickets)
        messages = sb_get(f"ticket_messages?select=*&ticket_id=in.({ids})&order=created_at.asc")
        # Group messages by ticket_id
        msgs_by_ticket: dict = {}
        for m in messages:
            tid = m["ticket_id"]
            msgs_by_ticket.setdefault(tid, []).append({
                "author": m.get("author", "utente"),
                "content": m.get("content", ""),
                "created_at": m.get("created_at", "")
            })
        # Attach messages to tickets
        for t in tickets:
            t["messages"] = msgs_by_ticket.get(t["id"], [])
        return tickets
    except Exception as e:
        print(f"Warning: Supabase fetch failed: {e}", file=sys.stderr)
        return []


def fetch_approvals() -> dict[str, dict]:
    """
    Fetch pending article approvals from Supabase.
    Returns a dict mapping slug -> {approved_at, title, id, ...}
    """
    try:
        approvals = sb_get("article_approvals?status=eq.pending&order=approved_at.asc")
        if not approvals:
            return {}
        return {a["slug"]: a for a in approvals}
    except Exception as e:
        print(f"Warning: Supabase approvals fetch failed: {e}", file=sys.stderr)
        return {}


def build_publication_queue(approvals: dict) -> list[dict]:
    """
    Costruisce la coda di pubblicazione basandosi su pipeline.json v2.0.

    Pipeline v2.0: legge da investigations[] ordinati per priority.
    Solo investigations con status approvato/preview entrano nella coda.

    REGOLA:
    - Gli slot seguono l'ordine di priority in investigations[]
    - Articoli approvati che NON sono in investigations[] vanno DOPO
    - Un articolo per settimana, sempre il martedi.
    """
    from datetime import date, timedelta

    MONTHS_IT = ['gen', 'feb', 'mar', 'apr', 'mag', 'giu', 'lug', 'ago', 'set', 'ott', 'nov', 'dic']
    DAY_IT = ['lunedi', 'martedi', 'mercoledi', 'giovedi', 'venerdi', 'sabato', 'domenica']

    def fmt_date(d: date) -> str:
        return f"{DAY_IT[d.weekday()]} {d.day} {MONTHS_IT[d.month - 1]} {d.year}"

    # 1. Carica pipeline v2.0
    try:
        pipeline = json.loads(PIPELINE_JSON.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"Warning: pipeline.json read failed in build_publication_queue: {e}", file=sys.stderr)
        pipeline = {}

    # 2. Pipeline v2.0: leggi investigations[] ordinati per pub_date (il calendario comanda)
    # Se pub_date assente, fallback su priority, poi su indice originale (999).
    # Questo garantisce che l'ordine della coda rifletta il calendario editoriale.
    def _sort_key(item: dict) -> tuple:
        pub_date = item.get("pub_date") or ""
        priority = item.get("priority", 999)
        return (pub_date, priority)

    investigations = sorted(
        pipeline.get("investigations", []),
        key=_sort_key
    )

    # Converti in home_items format per compatibilita
    home_items: list[dict] = []
    for inv in investigations:
        s = inv.get("slug", "")
        if s:
            home_items.append({
                "slug": s,
                "title": inv.get("title", s),
                "category": inv.get("category", ""),
                "pub_date": inv.get("pub_date"),  # data da pipeline v2.0
            })

    # 3. Calcola prossimo martedi (o oggi se oggi e martedi)
    today = date.today()
    days_ahead = 1 - today.weekday()  # martedi = weekday 1
    if days_ahead < 0:
        days_ahead += 7
    next_tue = today + timedelta(days=days_ahead)

    # FIX Bug 1: Se il prossimo martedi calcolato e gia stato usato, salta
    lock_file = Path.home() / ".pinkybot" / "data" / "logs" / "last_publish_week.txt"
    next_tue_already_used = False
    if lock_file.exists():
        try:
            saved_date_str = lock_file.read_text().strip()
            if "-" in saved_date_str and len(saved_date_str) == 10:
                last_publish_date = date.fromisoformat(saved_date_str)
                if last_publish_date == next_tue:
                    next_tue_already_used = True
            else:
                saved_week = saved_date_str
                next_tue_week = next_tue.strftime("%Y-%W")
                if saved_week == next_tue_week:
                    next_tue_already_used = True
        except Exception:
            pass
    if next_tue_already_used:
        next_tue += timedelta(weeks=1)

    home_slugs = {x["slug"] for x in home_items}
    queue = []

    # Insieme di slug con file HTML in /preview/ (articoli effettivamente pronti)
    preview_dir = Path("/var/www/aiena.it/preview")
    preview_slugs = {f.stem for f in preview_dir.glob("*.html")} if preview_dir.exists() else set()

    # 4. Slot per investigations[] (ordinati per pub_date)
    # REGOLA: gli articoli APPROVATI occupano martedì consecutivi senza buchi.
    # Gli articoli NON approvati (preview/Da approvare) non contano per il calcolo date —
    # il prossimo approvato prende il martedì subito dopo l'ultimo approvato.
    position = 0
    next_slot = next_tue  # prossimo martedì libero per articoli approvati
    for item in home_items:
        slug = item["slug"]
        a = approvals.get(slug)
        has_preview = slug in preview_slugs
        # Salta se non ha preview E non e approvato
        if not has_preview and not a:
            continue

        # Solo articoli approvati ottengono posizione e data di pubblicazione
        if a is not None:
            position += 1
            # Usa pub_date da pipeline se presente E non crea buchi rispetto a next_slot
            pipeline_pub_date = None
            if item.get("pub_date"):
                try:
                    pipeline_pub_date = date.fromisoformat(item["pub_date"])
                except (ValueError, TypeError):
                    pass
            # Scegli la data: pipeline pub_date se >= next_slot, altrimenti next_slot
            if pipeline_pub_date and pipeline_pub_date >= next_slot:
                pub_date = pipeline_pub_date
            else:
                pub_date = next_slot
            next_slot = pub_date + timedelta(weeks=1)  # prossimo slot libero
            queue.append({
                "position": position,
                "slug": slug,
                "title": a.get("title", item["title"]),
                "category": item["category"],
                "approved": True,
                "approved_at": a.get("approved_at", ""),
                "approval_id": a.get("id", ""),
                "pub_date": pub_date.isoformat(),
                "pub_date_label": fmt_date(pub_date),
            })
        else:
            # Preview-only (non approvato): nessuna posizione/data, non avanza next_slot
            queue.append({
                "position": None,
                "slug": slug,
                "title": item["title"],
                "category": item["category"],
                "approved": False,
                "approved_at": "",
                "approval_id": "",
                "pub_date": item.get("pub_date"),
                "pub_date_label": "Da approvare",
            })

    # 5. Articoli approvati NON in investigations[] -> dopo tutti gli slot correnti
    extras = [
        approvals[slug]
        for slug in sorted(approvals.keys(), key=lambda x: approvals[x].get("approved_at", ""))
        if slug not in home_slugs
    ]
    for a in extras:
        position += 1
        pub_date = next_slot
        next_slot = pub_date + timedelta(weeks=1)
        queue.append({
            "position": position,
            "slug": a["slug"],
            "title": a.get("title", a["slug"]),
            "category": a.get("category", ""),
            "approved": True,
            "approved_at": a.get("approved_at", ""),
            "approval_id": a.get("id", ""),
            "pub_date": pub_date.isoformat(),
            "pub_date_label": fmt_date(pub_date),
        })

    # Mostra la coda solo se c'e almeno un articolo approvato
    has_any_approved = any(x["approved"] for x in queue)
    if not has_any_approved:
        return []

    return queue


def fetch_all_approvals() -> dict[str, dict]:
    """
    Fetch ALL article approvals from Supabase (pending + published).
    Returns a dict mapping slug -> {status, approved_at, published_at, ...}
    """
    try:
        # Fetch both pending and published
        approvals = sb_get("article_approvals?select=*&order=approved_at.desc")
        if not approvals:
            return {}
        return {a["slug"]: a for a in approvals}
    except Exception as e:
        print(f"Warning: Supabase all approvals fetch failed: {e}", file=sys.stderr)
        return {}


def compute_effective_status(slug: str, pipeline_status: str, preview_slugs: set, approvals: dict) -> str:
    """
    Calcola lo stato effettivo di un articolo basandosi su:
    1. Supabase status="published" -> "pubblicato"
    2. Supabase status="pending" -> "approvato"
    3. Preview HTML esiste -> "preview"
    4. Altrimenti -> pipeline_status originale
    """
    approval = approvals.get(slug)
    if approval:
        sb_status = approval.get("status", "")
        if sb_status == "published":
            return "pubblicato"
        elif sb_status == "pending":
            return "approvato"
    # Check if preview exists
    if slug in preview_slugs:
        return "preview"
    # Fallback to pipeline status
    return pipeline_status


def calculate_backlog_score(item: dict) -> int:
    """
    Calculate priority score for a backlog item.
    Higher score = higher priority for investigation.

    Formula:
    - base_score (ticket_ref, sources_count, status)
    - × category_multiplier (appalti/corruzione highest)
    - + age_bonus (older items get bonus)
    """
    score = 0

    # Base score
    if item.get("ticket_ref"):
        score += 50  # User-submitted tips get priority (democratic journalism)

    # Sources count: use sources[] array length OR leads field if present
    sources = item.get("sources_count", 0)
    if sources == 0:
        sources = len(item.get("sources", []))
    if sources == 0:
        sources = item.get("leads", 0) or 0
    # Fallback: research_notes (conservative approximation)
    if sources == 0:
        notes = item.get("research_notes", [])
        if len(notes) >= 3:
            sources = max(1, len(notes) // 2)

    if sources >= 5:
        score += 30
    elif sources >= 3:
        score += 15
    elif sources >= 2:
        score += 5
    elif sources == 0 and item.get("status") == "idea":
        score -= 10

    if item.get("status") == "ricerca":
        score += 10

    # Category multiplier
    category = (item.get("category") or "").lower()
    multiplier = 1.0
    if any(k in category for k in ["appalti", "corruzione", "fondi"]):
        multiplier = 1.5
    elif any(k in category for k in ["politica", "conflitti"]):
        multiplier = 1.4
    elif any(k in category for k in ["aziende", "criminalità", "organizzata", "ndrangheta", "mafia"]):
        multiplier = 1.3
    elif any(k in category for k in ["cronaca", "patrimoni", "immobili"]):
        multiplier = 1.2

    score = int(score * multiplier)

    # Age bonus
    added = item.get("added_date") or item.get("added") or item.get("created_at", "")
    if not added:
        # Fallback: usa mtime di pipeline.json come data approssimativa
        try:
            mtime = PIPELINE_JSON.stat().st_mtime
            from datetime import datetime as _dt
            added = _dt.fromtimestamp(mtime).date().isoformat()
        except Exception:
            pass
    if added:
        try:
            added_date = date.fromisoformat(added[:10])
            age_days = (date.today() - added_date).days
            if age_days > 60:
                score += 20
            elif age_days > 30:
                score += 10
            elif age_days > 14:
                score += 5
        except (ValueError, TypeError):
            pass

    return max(0, score)


def _is_stale(item: dict, threshold_days: int = 14) -> bool:
    """
    Restituisce True se un item è bloccato nello stesso status da più di threshold_days.
    Ignora item in status 'pubblicato' o 'idea' (non ancora attivi).
    """
    if item.get("status") in ("pubblicato", "idea"):
        return False
    added = item.get("added_date") or item.get("added") or item.get("created_at", "")
    if not added:
        return False
    try:
        from datetime import date as _date
        added_date = _date.fromisoformat(added[:10])
        return (_date.today() - added_date).days > threshold_days
    except (ValueError, TypeError):
        return False


def load_pipeline_status() -> dict:
    """
    Carica lo stato del pipeline per la research box in admin.
    Pipeline v2.0: investigations[] + leads[] separati.

    Ritorna:
    {
        "investigations": [enriched_investigation, ...],  # sorted by priority
        "leads": [enriched_lead, ...],  # sorted by priority_score desc
        "updated_at": "..."
    }

    IMPORTANTE: lo status viene calcolato in modo "effettivo":
    - Supabase published -> "pubblicato"
    - Supabase pending -> "approvato"
    - Preview HTML esiste -> "preview"
    - Altrimenti -> status da pipeline.json
    """
    try:
        data = json.loads(PIPELINE_JSON.read_text(encoding="utf-8"))
        preview_dir = Path("/var/www/aiena.it/preview")
        preview_slugs = {f.stem for f in preview_dir.glob("*.html")} if preview_dir.exists() else set()

        # Fetch approvals from Supabase for effective status calculation
        approvals = fetch_all_approvals()

        def enrich_investigation(item):
            """Enriches an investigation item with effective status and computed fields."""
            if not item:
                return None
            slug = item.get("slug", "")
            pipeline_status = item.get("status", "ricerca")
            effective_status = compute_effective_status(slug, pipeline_status, preview_slugs, approvals)
            return {
                "title": item.get("title", ""),
                "slug": slug,
                "category": item.get("category", ""),
                "status": effective_status,
                "pipeline_status": pipeline_status,
                "pub_date": item.get("pub_date"),
                "priority": item.get("priority", 999),
                "has_preview": slug in preview_slugs,
                "is_approved": slug in approvals and approvals[slug].get("status") == "pending",
                "diary": item.get("diary", []),
                "diary_text": item.get("diary_text", ""),
                "research_notes": item.get("research_notes", [])[:5],
                "sources_count": item.get("sources_count", 0) or len(item.get("sources", [])),
                "ticket_ref": item.get("ticket_ref"),
                "added": item.get("added", ""),
            }

        def enrich_lead(item, precomputed_score=None):
            """Enriches a lead item with computed fields."""
            if not item:
                return None
            score = precomputed_score if precomputed_score is not None else calculate_backlog_score(item)
            return {
                "title": item.get("title", ""),
                "slug": item.get("slug", ""),
                "category": item.get("category", ""),
                "status": item.get("status", "idea"),
                "priority_score": item.get("priority_score", 0),
                "sources_count": item.get("sources_count", 0),
                "description": item.get("description", ""),
                "research_notes": item.get("research_notes", [])[:5],
                "ticket_ref": item.get("ticket_ref"),
                "added": item.get("added", ""),
                "monitor_date": item.get("monitor_date"),
                "monitor_note": item.get("monitor_note"),
                "score": score,
                "stale": _is_stale(item),
            }

        # Pipeline v2.0 structure
        investigations = [
            enrich_investigation(inv)
            for inv in sorted(
                data.get("investigations", []),
                key=lambda x: x.get("priority", 999)
            )
            if inv
        ]

        # Recupera slug archiviati da Supabase (pipeline_archive) per escluderli
        archived_slugs: set[str] = set()
        try:
            archived = sb_get("article_approvals?status=eq.pipeline_archive&select=slug")
            if archived:
                archived_slugs = {a["slug"] for a in archived}
        except Exception:
            pass

        # Prima calcola lo score per ogni lead, poi ordina per score decrescente
        # Escludi lead archiviati via Supabase e lead senza slug
        raw_leads = [
            l for l in data.get("leads", [])
            if l and l.get("slug") and l.get("slug") not in archived_slugs
            and l.get("status") != "archiviato"
        ]
        leads_with_score = [(l, calculate_backlog_score(l)) for l in raw_leads]
        leads_with_score.sort(key=lambda x: x[1], reverse=True)
        leads = [enrich_lead(l, score) for l, score in leads_with_score]

        return {
            "investigations": investigations,
            "leads": leads,
            "updated_at": data.get("updated_at", ""),
        }
    except Exception as e:
        print(f"Warning: pipeline_status load failed: {e}", file=sys.stderr)
        return {}


def load_events() -> list[dict]:
    """Carica eventi da pipeline.json."""
    try:
        if PIPELINE_JSON.exists():
            data = json.loads(PIPELINE_JSON.read_text(encoding="utf-8"))
            return data.get("events", [])
    except Exception as e:
        print(f"Warning: pipeline.json read failed: {e}", file=sys.stderr)
    return []


# ============================================================================
# Pipeline Journal e Deploy Ledger (per tab Log in admin)
# ============================================================================

LOGS_DIR = Path("/home/pinky/.pinkybot/data/logs")
PIPELINE_JOURNAL_PATH = LOGS_DIR / "pipeline_journal.log"
DEPLOY_LEDGER_PATH = LOGS_DIR / "deploy_ledger.json"


def load_pipeline_journal(limit: int = 50) -> list[dict]:
    """
    Legge le ultime N entries dal pipeline journal.

    Formato linea:
        [2026-05-05T09:30:00Z] [script] [slug] STEP: nome -> OK|FAIL (error)
        [2026-05-05T09:30:00Z] [script] [slug] MILESTONE -> OK

    Ritorna lista di dict con: timestamp, script, slug, step, status, error
    """
    entries = []
    try:
        if not PIPELINE_JOURNAL_PATH.exists():
            return entries

        lines = PIPELINE_JOURNAL_PATH.read_text(encoding="utf-8").strip().split("\n")

        # Leggi le ultime 200 righe (per avere margine), poi prendi le 50 piu' recenti
        import re
        pattern = re.compile(
            r'\[([^\]]+)\]\s*\[([^\]]+)\]\s*\[([^\]]+)\]\s*(STEP:\s*([^\s]+)|([A-Z_]+))\s*->\s*(OK|FAIL)(?:\s*\(([^)]+)\))?'
        )

        for line in reversed(lines[-200:]):
            if len(entries) >= limit:
                break

            m = pattern.match(line.strip())
            if m:
                timestamp = m.group(1)
                script = m.group(2)
                slug = m.group(3)
                # group(5) = step name se STEP:, group(6) = milestone se MILESTONE
                step = m.group(5) if m.group(5) else m.group(6)
                status = m.group(7).lower()
                error = m.group(8) if m.group(8) else None

                entries.append({
                    "timestamp": timestamp,
                    "script": script,
                    "slug": slug,
                    "step": step,
                    "status": status,
                    "error": error
                })

        # Reversa per avere i piu' recenti per primi
        return list(reversed(entries))

    except Exception as e:
        print(f"Warning: pipeline_journal read failed: {e}", file=sys.stderr)
        return []


def load_deploy_ledger(limit: int = 20) -> list[dict]:
    """
    Legge le ultime N entries dal deploy ledger.

    Ritorna lista di dict con: timestamp, script, slug, file, operation, status, size_bytes, sha256, error
    """
    try:
        if not DEPLOY_LEDGER_PATH.exists():
            return []

        ledger = json.loads(DEPLOY_LEDGER_PATH.read_text(encoding="utf-8"))
        entries = ledger.get("entries", [])

        # Ritorna le ultime N (piu' recenti per prime)
        return list(reversed(entries[-limit:]))

    except Exception as e:
        print(f"Warning: deploy_ledger read failed: {e}", file=sys.stderr)
        return []


def _build_auth_headers(method: str, path: str) -> dict:
    """Build HMAC-signed internal auth headers for daemon API calls.

    Delega a /home/pinky/lib/broker_auth.py (sorgente unica della firma).
    Prima leggeva SOLO os.environ, che il cron non popola: cadeva sempre nel
    `return {}` finale e ogni chiamata partiva non firmata (401). broker_auth
    legge anche /home/pinky/.pinkybot/.env e solleva se il secret non c'e'.
    """
    sys.path.insert(0, "/home/pinky/lib")
    import broker_auth
    return broker_auth.build_headers(method, path, agent="satoshi")


def fetch_pending_tasks() -> list[dict]:
    """Recupera task pendenti dal PinkyBot API."""
    try:
        path = "/tasks?limit=50"
        headers = _build_auth_headers("GET", path)
        req = urllib.request.Request(f"http://localhost:8888{path}", headers=headers)
        with urllib.request.urlopen(req, timeout=5) as r:
            data = json.loads(r.read())
        items = data if isinstance(data, list) else data.get("tasks", data.get("items", []))
        return [t for t in items if t.get("status") not in ("done", "cancelled")]
    except Exception as e:
        print(f"Warning: tasks fetch failed: {e}", file=sys.stderr)
        return []


def build_schedule(events: list, preview_articles: list, tasks: list, publication_queue: list | None = None) -> list[dict]:
    """
    Costruisce il programma editoriale dei prossimi 7 giorni per aiena.it SOLO.
    Mostra SOLO il workflow editoriale di aiena.it, dedotto dai cron job e dalla pipeline.
    NON mostra: Segugio, BitcoinMarket, task generici PinkyBot, routine automatiche.

    publication_queue: lista da build_publication_queue() — usata per mostrare
    il titolo specifico dell'articolo schedulato su ogni martedì.
    """
    from datetime import date, timedelta

    today = date.today()
    # Finestre pubblicazione aiena.it: SOLO mar/mer/gio
    AIENA_PUB_DAYS = {1, 2, 3}  # lun=0, mar=1, mer=2, gio=3
    DAY_NAMES = ["lun", "mar", "mer", "gio", "ven", "sab", "dom"]

    # Index queue by pub_date → articolo approvato per quel giorno
    queue_by_date: dict = {}
    for item in (publication_queue or []):
        if item.get("approved") and item.get("pub_date"):
            queue_by_date[item["pub_date"]] = item

    # Index pipeline events by date
    events_by_date: dict = {}
    for ev in events:
        d = ev.get("date", "")
        if d:
            events_by_date.setdefault(d, []).append(ev)

    schedule = []
    for offset in range(7):
        day = today + timedelta(days=offset)
        ds = day.isoformat()
        dow = day.weekday()  # 0=lun
        is_pub_window = dow in AIENA_PUB_DAYS
        day_tasks = []

        # ── Pipeline events per questo giorno (scadenze, milestone, pubblicazioni) ──
        for ev in events_by_date.get(ds, []):
            day_tasks.append({
                "agent": "sistema",
                "type": ev.get("type", "evento"),
                "title": ev.get("title", "Evento"),
                "description": ev.get("description", ""),
                "priority": "alta" if ev.get("deadline") else "normale",
                "icon": ev.get("icon", "")
            })

        # ── MARTEDÌ — Finestra principale pubblicazione aiena.it ──
        if dow == 1:
            scheduled = queue_by_date.get(ds)
            if scheduled:
                # Articolo specifico schedulato per questo martedì
                day_tasks.append({
                    "agent": "satoshi",
                    "type": "pubblicazione",
                    "title": f"Pubblica: {scheduled['title'][:40]}",
                    "description": f"Slot #{scheduled['position']} — {scheduled.get('category', '')} — auto alle 09:30",
                    "priority": "alta",
                    "icon": "📰"
                })
            elif preview_articles:
                day_tasks.append({
                    "agent": "satoshi",
                    "type": "pubblicazione",
                    "title": f"Finestra pubblicazione AIena ({len(preview_articles)} in preview)",
                    "description": "Nessun articolo ancora approvato per questa data",
                    "priority": "normale",
                    "icon": ""
                })

        # ── MERCOLEDÌ — Backup finestra pubblicazione aiena.it ──
        elif dow == 2:
            # Verifica se il martedì della stessa settimana ha già uno schedulato
            tue_of_week = (day - timedelta(days=1)).isoformat()
            if not queue_by_date.get(tue_of_week) and preview_articles:
                day_tasks.append({
                    "agent": "satoshi",
                    "type": "pubblicazione",
                    "title": f"Backup pubblicazione AIena ({len(preview_articles)} in preview)",
                    "description": "Se non pubblicato martedì",
                    "priority": "normale",
                    "icon": ""
                })

        # ── GIOVEDÌ — Ultima finestra backup pubblicazione aiena.it ──
        elif dow == 3:
            tue_of_week = (day - timedelta(days=2)).isoformat()
            if not queue_by_date.get(tue_of_week) and preview_articles:
                day_tasks.append({
                    "agent": "satoshi",
                    "type": "pubblicazione",
                    "title": f"Ultima finestra pubblicazione AIena ({len(preview_articles)} in preview)",
                    "description": "Ultimo giorno utile della settimana",
                    "priority": "alta",
                    "icon": ""
                })

        # ── VENERDÌ — Report settimanale AIena (cron 18:00) ──
        elif dow == 4:
            day_tasks.append({
                "agent": "aiena",
                "type": "report",
                "title": "Report settimanale (ore 18:00)",
                "description": "aiena_friday_report.py — riepilogo indagini settimana",
                "priority": "normale",
                "icon": ""
            })

        # ── SABATO — Ricerca nuove storie (weekly trigger ore 09:00) ──
        elif dow == 5:
            day_tasks.append({
                "agent": "aiena",
                "type": "ricerca",
                "title": "Ricerca nuove storie (ore 09:00)",
                "description": "aiena_weekly_trigger.py — scout fonti ANAC, OpenData, notizie IT",
                "priority": "normale",
                "icon": ""
            })

        # ── DOMENICA — Scrittura articolo settimana (weekly trigger ore 09:00) ──
        elif dow == 6:
            day_tasks.append({
                "agent": "aiena",
                "type": "scrittura",
                "title": "Scrittura articolo settimana (ore 09:00)",
                "description": "aiena_weekly_trigger.py — elaborazione e stesura inchiesta",
                "priority": "normale",
                "icon": ""
            })

        # ── LUNEDÌ — Backlog scout (10:30) + brief serale ──
        elif dow == 0:
            day_tasks.append({
                "agent": "aiena",
                "type": "ricerca",
                "title": "Backlog scout (ore 10:30)",
                "description": "aiena_backlog_scout.py — nuovi lead da fonti pubbliche",
                "priority": "normale",
                "icon": ""
            })

        schedule.append({
            "date": ds,
            "day_name": DAY_NAMES[dow],
            "day_num": day.day,
            "is_pub_window": is_pub_window,
            "is_today": offset == 0,
            "tasks": day_tasks
        })

    return schedule


def deploy_to_ftp(local_path: Path, ftp_path: str, http_url: str) -> bool:
    """Deploya un file via FTP usando lo script esistente."""
    if not FTP_DEPLOY_SCRIPT.exists():
        print(f"Warning: FTP script not found at {FTP_DEPLOY_SCRIPT}", file=sys.stderr)
        # Fallback: usa curl direttamente
        try:
            result = subprocess.run([
                "curl", "-s", "-T", str(local_path),
                f"ftp://ftp.aiena.it{ftp_path}",
                "--user", "aiena.it:Ugh6ooth"
            ], capture_output=True, text=True, timeout=30)
            return result.returncode == 0
        except Exception as e:
            print(f"FTP error: {e}", file=sys.stderr)
            return False

    try:
        result = subprocess.run(
            ["bash", str(FTP_DEPLOY_SCRIPT), str(local_path), ftp_path, http_url],
            capture_output=True, text=True, timeout=30
        )
        print(result.stdout.strip())
        if result.stderr:
            print(result.stderr.strip(), file=sys.stderr)
        return result.returncode == 0
    except Exception as e:
        print(f"FTP deploy error: {e}", file=sys.stderr)
        return False


def _tg_notify(text: str) -> None:
    """Send a Telegram notification to Mirko (best-effort)."""
    try:
        req = urllib.request.Request(
            "https://api.telegram.org/bot7584023512:AAFt_kRNMlc6x2f4aFYQvQaAuOMCAo9Etns/sendMessage",
            data=json.dumps({"chat_id": "32405655", "text": text}).encode(),
            headers={"Content-Type": "application/json"}
        )
        urllib.request.urlopen(req, timeout=5)
    except Exception:
        pass


def notify_ticket_outcomes() -> None:
    """
    Notifica ai segnalatori l'esito delle loro segnalazioni.

    1. Articoli pubblicati con ticket_ref: invia messaggio finale al ticket
    2. Backlog items con ticket_ref scaduti (>60 giorni, status=idea): notifica scarto
       + Telegram notification a Mirko

    La funzione è idempotente: verifica se il ticket ha già ricevuto notifica
    cercando parole chiave nei messaggi esistenti.
    """
    from datetime import date, timedelta

    TICKET_REPLY_SCRIPT = Path.home() / ".pinkybot" / "scripts" / "supabase_ticket_reply.py"

    if not TICKET_REPLY_SCRIPT.exists():
        print(f"Warning: ticket reply script not found at {TICKET_REPLY_SCRIPT}", file=sys.stderr)
        return

    try:
        pipeline_data = json.loads(PIPELINE_JSON.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"Warning: cannot read pipeline.json for ticket notifications: {e}", file=sys.stderr)
        return

    # Pipeline v2.0: raccogli items pubblicati da published[] + investigations[] con status pubblicato
    published_items = pipeline_data.get("published", [])
    published_items += [
        inv for inv in pipeline_data.get("investigations", [])
        if inv.get("status") == "pubblicato"
    ]

    # Pipeline v2.0: raccogli leads[]
    leads = pipeline_data.get("leads", [])
    today = date.today()
    stale_threshold = today - timedelta(days=60)

    for item in published_items:
        ticket_ref = item.get("ticket_ref", "").strip()
        if not ticket_ref:
            continue

        # Cerca il ticket UUID dal ticket_code
        try:
            tickets = sb_get(f"tickets?ticket_code=eq.{ticket_ref}&select=id,ticket_code,status")
            if not tickets:
                print(f"  - Ticket {ticket_ref} non trovato in Supabase, skip")
                continue
            ticket_id = tickets[0]["id"]

            # Verifica se già notificato (cerca nei messaggi "pubblicato" o "inchiesta")
            messages = sb_get(f"ticket_messages?ticket_id=eq.{ticket_id}&select=content")
            already_notified = any(
                "pubblicat" in (m.get("content", "") or "").lower() or
                "inchiesta" in (m.get("content", "") or "").lower()
                for m in messages
            )

            if already_notified:
                print(f"  - Ticket {ticket_ref}: già notificato (pubblicazione), skip")
                continue

            # Invia notifica pubblicazione
            msg = "La vicenda che hai segnalato ha portato a un'inchiesta pubblicata su AIena.it. Grazie per il contributo."
            result = subprocess.run(
                ["python3", str(TICKET_REPLY_SCRIPT), ticket_id, msg, "--close", "--status", "non_rilevante"],
                capture_output=True, text=True, timeout=30
            )
            if result.returncode == 0:
                print(f"  - Ticket {ticket_ref}: notifica pubblicazione inviata e ticket chiuso")
            else:
                print(f"  - Ticket {ticket_ref}: errore invio notifica: {result.stderr}", file=sys.stderr)

        except Exception as e:
            print(f"  - Ticket {ticket_ref}: errore: {e}", file=sys.stderr)

    # Notifica leads scaduti (>60 giorni, status=idea)
    for item in leads:
        ticket_ref = (item.get("ticket_ref") or "").strip()
        if not ticket_ref:
            continue

        status = item.get("status", "")
        added = item.get("added", "")

        # Solo items con status=idea e più vecchi di 60 giorni
        if status != "idea":
            continue

        try:
            added_date = date.fromisoformat(added)
            if added_date > stale_threshold:
                continue  # Non ancora scaduto
        except (ValueError, TypeError):
            continue  # Data non valida, skip

        try:
            tickets = sb_get(f"tickets?ticket_code=eq.{ticket_ref}&select=id,ticket_code,status")
            if not tickets:
                continue
            ticket_id = tickets[0]["id"]

            # Skip se già chiuso
            if tickets[0].get("status") == "chiuso":
                continue

            # Verifica se già notificato scarto
            messages = sb_get(f"ticket_messages?ticket_id=eq.{ticket_id}&select=content")
            already_notified = any(
                "insufficien" in (m.get("content", "") or "").lower() or
                "archivia" in (m.get("content", "") or "").lower()
                for m in messages
            )

            if already_notified:
                print(f"  - Ticket {ticket_ref}: già notificato (scarto), skip")
                continue

            # Invia notifica scarto
            msg = "La segnalazione e stata valutata ma non sono emersi elementi sufficienti per procedere con un'inchiesta. Grazie comunque per il contributo."
            result = subprocess.run(
                ["python3", str(TICKET_REPLY_SCRIPT), ticket_id, msg, "--close", "--status", "non_rilevante"],
                capture_output=True, text=True, timeout=30
            )
            if result.returncode == 0:
                print(f"  - Ticket {ticket_ref}: notifica scarto inviata e ticket chiuso (>60 giorni, status=idea)")
                # FIX 7: Telegram notification to Mirko
                item_title = item.get("title", ticket_ref)
                _tg_notify(f"Ticket scartato per eta' (>60gg): {item_title}\nRef: {ticket_ref}")
            else:
                print(f"  - Ticket {ticket_ref}: errore invio notifica scarto: {result.stderr}", file=sys.stderr)

        except Exception as e:
            print(f"  - Ticket {ticket_ref} (scarto): errore: {e}", file=sys.stderr)


def get_investigations() -> list[dict]:
    """
    Legge tutte le cartelle in indagini/ e restituisce lista di dict.
    Ogni indagine ha: slug, soggetto, status, categoria, priority_score, go_nogo,
    fasi_completate, pending_actions, ticket_ref, note, created_at, diary_last_modified.
    """
    indagini_dir = Path.home() / ".pinkybot" / "data" / "agents" / "aiena" / "indagini"
    results = []
    if not indagini_dir.exists():
        return results

    for case_dir in sorted(indagini_dir.iterdir()):
        if not case_dir.is_dir():
            continue
        meta_path = case_dir / "META.json"
        diary_path = case_dir / "DIARY.md"

        meta = {}
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except Exception:
                pass

        # Conta pending actions in DIARY.md (checkbox non completate)
        pending_count = 0
        if diary_path.exists():
            try:
                content = diary_path.read_text(encoding="utf-8")
                pending_count = content.count("- [ ]")
            except Exception:
                pass

        # Last modified del diary
        last_mod = None
        if diary_path.exists():
            try:
                last_mod = datetime.fromtimestamp(
                    diary_path.stat().st_mtime, tz=timezone.utc
                ).isoformat()
            except Exception:
                pass

        results.append({
            "slug": meta.get("slug", case_dir.name),
            "soggetto": meta.get("soggetto_principale", case_dir.name),
            "status": meta.get("status", "intake"),
            "categoria": meta.get("categoria", ""),
            "priority_score": meta.get("priority_score", 0),
            "go_nogo": meta.get("go_nogo"),
            "fasi_completate": meta.get("fasi_completate", []),
            "pending_actions": pending_count,
            "ticket_ref": meta.get("ticket_ref"),
            "note": meta.get("note", ""),
            "created_at": meta.get("created_at"),
            "diary_last_modified": last_mod
        })

    # Sort: priorita alta prima, poi per data creazione (piu recenti prima)
    results.sort(key=lambda x: (-x.get("priority_score", 0), x.get("created_at") or ""))
    return results


def process_promote_signals():
    """
    Process promote_pending signals from Supabase.

    Workflow:
    1. Fetch records with status='promote_pending' from article_approvals
    2. For each, call local API POST /api/leads/promote
    3. Update Supabase status to 'promoted' on success
    """
    try:
        pending = sb_get("article_approvals?status=eq.promote_pending&select=*")
        if not pending:
            print("  No promote_pending signals found")
            return

        print(f"  Found {len(pending)} promote_pending signals")

        for record in pending:
            slug = record.get("slug")
            title = record.get("title", "")
            category = record.get("category", "")

            if not slug:
                print(f"  - Skipping record without slug: {record}")
                continue

            print(f"  - Processing promote for: {slug}")

            # Call local API
            try:
                payload = json.dumps({
                    "slug": slug,
                    "title": title,
                    "category": category
                }).encode()

                req = urllib.request.Request(
                    "http://localhost:8888/api/leads/promote",
                    data=payload,
                    headers={"Content-Type": "application/json"},
                    method="POST"
                )

                with urllib.request.urlopen(req, timeout=30) as resp:
                    result = json.loads(resp.read())

                if result.get("promoted"):
                    print(f"    SUCCESS: {slug} promoted to investigation")
                    # Update Supabase status to 'promoted'
                    sb_patch(
                        f"article_approvals?slug=eq.{slug}",
                        {"status": "promoted"}
                    )
                else:
                    print(f"    SKIP: {slug} - {result.get('message', 'unknown')}")
                    # Mark as processed anyway to avoid infinite loop
                    sb_patch(
                        f"article_approvals?slug=eq.{slug}",
                        {"status": "promote_failed"}
                    )

            except urllib.error.URLError as e:
                print(f"    ERROR: API call failed for {slug}: {e}")
                # Don't update status - will retry next run
            except Exception as e:
                print(f"    ERROR: Unexpected error for {slug}: {e}")

    except Exception as e:
        print(f"  Error fetching promote_pending: {e}")


def process_delete_preview_pending() -> None:
    """
    Processa le richieste di cancellazione preview pendenti.
    Cerca article_approvals con status=delete_preview_pending,
    elimina il file HTML da /preview/ e aggiorna lo status su Supabase.
    """
    try:
        pending = sb_get("article_approvals?status=eq.delete_preview_pending")
    except Exception as e:
        print(f"[delete_preview] Supabase fetch error: {e}", file=sys.stderr)
        return

    if not pending:
        return

    for record in pending:
        slug = record.get("slug", "")
        record_id = record.get("id", "")
        if not slug or not record_id:
            continue

        preview_file = PREVIEW_DIR / f"{slug}.html"
        deleted_file = False

        if preview_file.exists():
            try:
                preview_file.unlink()
                print(f"[delete_preview] Deleted: {preview_file}")
                deleted_file = True
            except Exception as e:
                print(f"[delete_preview] ERROR deleting {preview_file}: {e}", file=sys.stderr)
                continue
        else:
            print(f"[delete_preview] File not found (already gone): {preview_file}")
            deleted_file = True  # File non esiste = già eliminato, aggiorna Supabase comunque

        if deleted_file:
            ok = sb_patch(f"article_approvals?id=eq.{record_id}", {"status": "deleted"})
            if ok:
                print(f"[delete_preview] Supabase status -> deleted for slug={slug}")
            else:
                print(f"[delete_preview] WARNING: Supabase update failed for slug={slug}", file=sys.stderr)


def main():
    print(f"[{datetime.now().isoformat()}] Generating admin data.json...")

    # Costruisci mappa slug -> metadati da pipeline.json (fonte di verità)
    # pipeline_titles: slug -> title (per scan_articles)
    # pipeline_meta:   slug -> {priority, pub_date, category} (per arricchire preview_articles)
    pipeline_titles: dict[str, str] = {}
    pipeline_meta: dict[str, dict] = {}
    try:
        if PIPELINE_JSON.exists():
            _pipe = json.loads(PIPELINE_JSON.read_text(encoding="utf-8"))
            for inv in _pipe.get("investigations", []) + _pipe.get("published", []):
                s = inv.get("slug", "")
                if not s:
                    continue
                t = inv.get("title", "")
                if t:
                    pipeline_titles[s] = t
                pipeline_meta[s] = {
                    "priority": inv.get("priority"),
                    "pub_date": inv.get("pub_date"),
                    "category": inv.get("category", ""),
                }
    except Exception as e:
        print(f"Warning: cannot build pipeline_titles: {e}", file=sys.stderr)

    # Raccogli dati
    preview_articles = scan_articles(PREVIEW_DIR, AIENA_PREVIEW_URL, "preview", pipeline_titles)
    published_articles = scan_articles(ARTICLES_DIR, AIENA_ARTICLES_URL, "published", pipeline_titles)

    # BUG FIX: Filtra articoli pubblicati dalla lista preview
    # Se un file preview non e' stato eliminato dopo pubblicazione, non deve apparire in preview
    published_slugs = {a["slug"] for a in published_articles}
    preview_articles = [a for a in preview_articles if a["slug"] not in published_slugs]

    aiena_status = get_aiena_status()
    tickets = fetch_tickets_with_messages()
    events = load_events()
    pending_tasks = fetch_pending_tasks()
    schedule = build_schedule(events, preview_articles, pending_tasks)

    # Fetch ALL approvals (pending + published) for status computation
    all_approvals = fetch_all_approvals()
    # Fetch pending-only approvals for queue building
    approvals = fetch_approvals()

    # Enrich preview articles with approval info, status, priority, pub_date
    for art in preview_articles:
        slug = art.get("slug", "")
        approval = all_approvals.get(slug)
        meta = pipeline_meta.get(slug, {})
        # Priority e pub_date da pipeline.json (fonte di verità)
        art["priority"] = meta.get("priority")
        art["pub_date"] = meta.get("pub_date")
        if approval:
            sb_status = approval.get("status", "")
            # "pending" in Supabase = approvato ma non pubblicato
            art["status"] = "approvato" if sb_status == "pending" else "preview"
            art["approved"] = sb_status == "pending"
            art["approved_at"] = approval.get("approved_at")
            art["category"] = approval.get("category", "") or meta.get("category", "")
        else:
            art["status"] = "preview"
            art["approved"] = False
            art["approved_at"] = None
            art["category"] = meta.get("category", "")

    # Coda pubblicazione (ordine home, martedì per settimana)
    publication_queue = build_publication_queue(approvals)
    pipeline_status = load_pipeline_status()

    # DISABLED 2026-06-15: article_approvals table has no 'publish_date' column
    # (columns: id, slug, title, category, excerpt, approved_at, status, published_at)
    # sync_pub_dates_to_supabase was generating 400 errors on every run.
    # pub_date is embedded in admin/data.json directly from pipeline.json — no Supabase sync needed.

    # Rigenera schedule con info coda (mostra articolo specifico per ogni martedì)
    schedule = build_schedule(events, preview_articles, pending_tasks, publication_queue)

    # Carica log per tab Log in admin
    pipeline_journal = load_pipeline_journal(limit=50)
    deploy_ledger = load_deploy_ledger(limit=20)

    # Carica indagini in corso
    investigations = get_investigations()

    # Costruisci JSON
    data = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "aiena_status": aiena_status,
        "preview_articles": preview_articles,
        "published_articles": published_articles,
        "tickets": tickets,
        "events": events,
        "schedule": schedule,
        "approvals": list(approvals.values()),
        "publication_queue": publication_queue,
        "pipeline_status": pipeline_status,
        "pipeline_journal": pipeline_journal,
        "deploy_ledger": deploy_ledger,
        "investigations": investigations
    }

    # Scrivi file locale
    ADMIN_DIR.mkdir(parents=True, exist_ok=True)
    DATA_JSON_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Written: {DATA_JSON_PATH}")
    print(f"  - {len(preview_articles)} preview articles")
    print(f"  - {len(published_articles)} published articles")
    print(f"  - {len(tickets)} tickets")
    print(f"  - {len(events)} events")
    print(f"  - {len(schedule)} schedule days")
    print(f"  - {len(pipeline_journal)} journal entries")
    print(f"  - {len(deploy_ledger)} deploy ledger entries")
    print(f"  - {len(investigations)} investigations")
    print(f"  - AIena status: {aiena_status['status']}")

    # Nessun FTP necessario — data.json è servito via symlink locale
    # /var/www/aiena-admin/data.json -> /var/www/aiena.it/admin/data.json
    # admin.aiena.it/data.json risponde direttamente dal VPS
    print("SUCCESS: data.json live su admin.aiena.it/data.json (symlink locale)")

    # Processa cancellazioni preview pendenti
    print("Checking delete_preview_pending signals...")
    process_delete_preview_pending()

    # Processa segnali di promozione lead -> investigation
    print("Checking promote_pending signals...")
    process_promote_signals()

    # Notifica esiti ticket (pubblicazioni e scarti)
    print("Checking ticket outcomes...")
    notify_ticket_outcomes()

    return 0


if __name__ == "__main__":
    # Handle CLI arguments
    if "--notify-tickets" in sys.argv:
        print(f"[{datetime.now().isoformat()}] Running ticket notifications only...")
        notify_ticket_outcomes()
        sys.exit(0)

    sys.exit(main())
