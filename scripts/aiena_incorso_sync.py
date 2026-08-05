#!/usr/bin/env python3
"""
AIena — Sincronizzazione card "In corso" in index.html

Legge pipeline.json, ricostruisce le 3 card "In corso" in index.html
e fa upload via FTP. Da eseguire quando cambia pipeline.json o in cron.

Usa la stessa logica di _update_incorso_cards in aiena_post_publish.py.
"""

import fcntl
import json
import os
import re
import sys
import tempfile
from ftplib import FTP
from pathlib import Path

from aiena_secrets import _load_secrets

PIPELINE_JSON = Path("/var/www/aiena.it/data/pipeline.json")
INDEX_HTML    = Path("/var/www/aiena.it/index.html")
INDEX_LOCK    = Path("/tmp/aiena_index_html.lock")

FTP_HOST = "ftp.aiena.it"
FTP_USER = "aiena.it"
FTP_PASS = _load_secrets().get("FTP_PASS", "")


def log(msg):
    print(f"[incorso_sync] {msg}")


def load_pipeline() -> dict:
    with open(PIPELINE_JSON, "r", encoding="utf-8") as f:
        return json.load(f)


def get_excerpt(item: dict) -> str:
    """Get excerpt from item, falling back through diary_text → summary → description."""
    raw = (
        item.get("diary_text")
        or item.get("summary")
        or item.get("description")
        or ""
    )
    # Strip HTML tags
    clean = re.sub(r"<[^>]+>", "", raw)
    if len(clean) > 120:
        clean = clean[:117] + "..."
    return clean


def rebuild_incorso_cards(data: dict) -> bool:
    """Rebuild 'In corso' section in index.html from pipeline investigations.

    REGOLA HOMEPAGE "In corso" (definita da Mirko 2026-06-16):
    - Mostra SOLO articoli con status='approvato' E pub_date assegnata
    - Ordinati per pub_date ASC (il più prossimo alla pubblicazione prima)
    - Prime 3 in ordine cronologico
    """
    investigations = data.get("investigations", [])

    # Solo approvati con data di pubblicazione
    approved = [
        inv for inv in investigations
        if inv.get("status") in ("approvato", "approved") and inv.get("pub_date")
    ]
    # Ordina per pub_date ASC
    approved.sort(key=lambda x: x.get("pub_date", "9999-99-99"))
    items = approved[:3]

    cards_html = ""
    for i, item in enumerate(items, 1):
        if not item.get("title"):
            continue
        title    = item["title"]
        category = item.get("category", "Indagine")
        excerpt  = get_excerpt(item)
        status   = item.get("status", "ricerca")
        status_map = {
            "scrittura": "In scrittura",
            "draft_ready": "Bozza pronta",
            "in_progress": "In scrittura",   # legacy
            "preview": "In preview",
            "approved": "Approvato",
            "approvato": "In coda",
            "ricerca": "In ricerca",
            "idea": "In ricerca",
            "lead": "Da promuovere",
            "attesa_risposta": "In attesa",
        }
        status_label = status_map.get(status, status.replace("_", " ").capitalize())

        cards_html += f"""
          <!-- Card {i} -->
          <div class="article-card" style="opacity: 0.55; cursor: default;">
            <div class="card-badges">
              <span class="badge badge-ongoing">In corso</span>
              <span class="badge badge-cat">{category}</span>
            </div>
            <h2 class="card-title">{title}</h2>
            <p class="card-excerpt">{excerpt}</p>
            <div class="card-meta"><span>AIena</span><span>{status_label}</span></div>
          </div>
"""

    if not cards_html:
        log("WARN: nessuna card generata — index.html non aggiornato")
        return False

    with open(INDEX_LOCK, "w") as lock_f:
        fcntl.flock(lock_f, fcntl.LOCK_EX)
        try:
            html = INDEX_HTML.read_text(encoding="utf-8")
            pattern = r"(<!-- Card 1 -->.*?)(</div>\s*</div>\s*</section>)"
            new_html, n = re.subn(pattern, cards_html + r"        \2", html, count=1, flags=re.DOTALL)
            if n == 0:
                log("WARN: pattern Card 1 non trovato in index.html")
                return False

            # Atomic write
            fd, tmp = tempfile.mkstemp(dir=INDEX_HTML.parent, suffix=".html.tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(new_html)
                os.chmod(tmp, 0o644)  # nginx (www-data) must read it; mkstemp creates 0600
                os.replace(tmp, INDEX_HTML)
            except Exception:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        finally:
            fcntl.flock(lock_f, fcntl.LOCK_UN)

    titles = [items[i].get("title", "?")[:30] for i in range(min(3, len(items)))]
    log(f"Card aggiornate: {' | '.join(titles)}")
    return True


def deploy_ftp() -> bool:
    """Upload index.html to FTP."""
    try:
        ftp = FTP(FTP_HOST, timeout=15)
        ftp.login(FTP_USER, FTP_PASS)
        for d in ["/htdocs", "/public_html", "/www"]:
            try:
                ftp.cwd(d)
                break
            except Exception:
                pass
        with open(INDEX_HTML, "rb") as f:
            ftp.storbinary("STOR index.html", f)
        ftp.quit()
        log("index.html uploaded via FTP ✓")
        return True
    except Exception as e:
        log(f"FTP error: {e}")
        return False


def main():
    data = load_pipeline()
    ok = rebuild_incorso_cards(data)
    if ok:
        deploy_ftp()
    else:
        log("Nessuna modifica — FTP skip")


if __name__ == "__main__":
    main()
