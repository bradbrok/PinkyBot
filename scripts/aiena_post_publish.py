#!/usr/bin/env python3
"""
Auto-trigger post-pubblicazione aiena.it.
Rimuove articolo da pipeline, aggiorna diario, aggiorna hashtag, deploya via FTP.
Chiamato da Satoshi dopo ogni pubblicazione.

Uso:
  python3 aiena_post_publish.py --slug "slug-articolo-pubblicato"
  python3 aiena_post_publish.py --slug "appalti-sanitari-gare-che-non-tornano"

Passi eseguiti (pipeline.json v2):
  1. Trova articolo con slug dato in investigations[]
  2. Lo rimuove da investigations[]
  3. Lo sposta in published[] con published_at = oggi
  4. Aggiorna il Diario AIena in index.html con diary_text di investigations[0]
  5. Chiama aiena_update_ticker.py con hashtag di investigations[0]
  6. Deploya via FTP: pipeline.json, index.html
  7. Log finale

Autore: Satoshi (auto-generato)
"""
import argparse
import fcntl
import ftplib
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import date
from pathlib import Path

# Logging strutturato per tracciamento deploy e pipeline
from aiena_logging import journal_step, journal_milestone, ledger_ftp_from_file, alert_failure

# Paths
PIPELINE_JSON = Path("/var/www/aiena.it/data/pipeline.json")
INDEX_HTML    = Path("/var/www/aiena.it/index.html")
INDEX_LOCK    = Path("/tmp/aiena_index_html.lock")  # Condiviso con aiena_update_ticker.py
RESEARCH_LOG  = Path("/home/pinky/.pinkybot/data/aiena_research_log.json")
PREVIEW_DIR = Path("/var/www/aiena.it/preview")
TICKER_SCRIPT = Path("/home/pinky/.pinkybot/scripts/aiena_update_ticker.py")

def _load_secrets() -> dict:
    """Load sensitive credentials from .aiena_secrets file. Falls back to env vars."""
    secrets: dict = {}
    secrets_file = Path("/home/pinky/.pinkybot/scripts/.aiena_secrets")
    if secrets_file.exists():
        for line in secrets_file.read_text().splitlines():
            line = line.strip()
            if line and "=" in line and not line.startswith("#"):
                k, _, v = line.partition("=")
                secrets[k.strip()] = v.strip()
    for key in ("SB_SERVICE_KEY", "FTP_PASS"):
        if os.environ.get(key):
            secrets[key] = os.environ[key]
    return secrets


# FTP credentials
FTP_HOST = "ftp.aiena.it"
FTP_USER = "aiena.it"
FTP_PASS = _load_secrets().get("FTP_PASS", "")


def log(msg: str) -> None:
    print(f"[post-publish] {msg}")


def load_pipeline() -> dict:
    try:
        return json.loads(PIPELINE_JSON.read_text(encoding="utf-8"))
    except FileNotFoundError:
        log("ERROR: pipeline.json not found")
        return {"investigations": [], "leads": [], "published": []}
    except json.JSONDecodeError as e:
        log(f"ERROR: pipeline.json is corrupted: {e}")
        return {"investigations": [], "leads": [], "published": []}


def save_pipeline(data: dict) -> None:
    """Salva pipeline.json in modo atomico (write to temp + os.replace)."""
    import os
    import tempfile
    data["updated_at"] = date.today().isoformat()
    content = json.dumps(data, ensure_ascii=False, indent=2)
    # Scrittura atomica: scrivi su file temporaneo nella stessa directory, poi os.replace
    fd, tmp_path = tempfile.mkstemp(dir=PIPELINE_JSON.parent, suffix=".tmp", prefix="pipeline_")
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(content)
        os.replace(tmp_path, PIPELINE_JSON)
        log(f"Salvato (atomico): {PIPELINE_JSON}")
    except Exception as e:
        # Cleanup file temporaneo in caso di errore
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise e


def find_and_remove_article(data: dict, slug: str) -> tuple[dict | None, int]:
    """
    Trova articolo per slug in investigations[] (pipeline v2).
    Rimuove e ritorna (article, index) dove index e la posizione originale.
    Ritorna (None, -1) se non trovato.
    """
    investigations = data.get("investigations", [])
    for i, item in enumerate(investigations):
        if item.get("slug") == slug:
            return investigations.pop(i), i

    # Fallback: check leads[] (should not happen for published articles, but be safe)
    leads = data.get("leads", [])
    for i, item in enumerate(leads):
        if item.get("slug") == slug:
            log(f"WARN: articolo trovato in leads[] invece di investigations[] - rimuovo comunque")
            return leads.pop(i), -2  # -2 = was in leads

    return None, -1


def move_to_published(data: dict, article: dict) -> None:
    """Aggiunge articolo a published[] con data odierna."""
    published_entry = {
        "title": article["title"],
        "slug": article["slug"],
        "status": "pubblicato",
        "pipeline_status": "pubblicato",   # F4-AIE-4: mantieni in sync con status
        "has_preview": False,               # F4-AIE-4: preview rimossa al momento di pubblicazione
        "category": article.get("category", ""),
        "published_at": date.today().isoformat(),
        "url": f"https://aiena.it/articles/{article['slug']}.html"
    }
    if article.get("ticket_ref"):
        published_entry["ticket_ref"] = article["ticket_ref"]
    data.setdefault("published", []).insert(0, published_entry)
    log(f"Spostato in published[]: {article['title']}")


def get_current_investigation(data: dict) -> dict | None:
    """
    In pipeline v2, current investigation is simply investigations[0].
    Returns None if investigations[] is empty.
    """
    investigations = data.get("investigations", [])
    return investigations[0] if investigations else None


def get_next_investigation(data: dict) -> dict | None:
    """
    In pipeline v2, next investigation is investigations[1].
    Returns None if less than 2 items in investigations[].
    """
    investigations = data.get("investigations", [])
    return investigations[1] if len(investigations) > 1 else None


# Nomi interni che non devono mai comparire nel sito pubblico.
# Aggiungere qui eventuali altri identificatori interni.
_INTERNAL_NAMES = ["Mirko", "Satoshi", "AIena", "Pinky"]


def _sanitize_diary_text(text: str) -> str:
    """Rimuove riferimenti a nomi interni dal testo diary prima del rendering pubblico."""
    import re as _re
    replacements = {
        r"\bMirko\b": "la redazione",
        r"\bSatoshi\b": "il team tecnico",
    }
    for pattern, replacement in replacements.items():
        text = _re.sub(pattern, replacement, text)
    return text


def build_diary_slides(current: dict) -> tuple[list[str], int]:
    """
    Costruisce le slide del diario da diary[] (array) o diary_text (stringa).
    Ritorna (slides_html_list, total).
    Priorità: diary[] > diary_text > fallback.
    """
    slides = []

    diary_array = current.get("diary") or []

    if diary_array:
        # Usa diary[] — struttura completa con date e note (es. current_investigation)
        # entry con "public": false sono note operative interne, non vanno in homepage
        diary_array = [e for e in diary_array if e.get("public", True)]
        for i, entry in enumerate(diary_array):
            active_class = " active" if i == 0 else ""
            date_label = entry.get("date", "")
            note_label = entry.get("note", "")
            date_str = f"{date_label} — nota {note_label}" if note_label else date_label
            text = _sanitize_diary_text(entry.get("text", ""))
            slides.append(
                f'<div class="diary-slide{active_class}" id="ds-{i}">\n'
                f'              <div class="diary-date">{date_str}</div>\n'
                f'              <div class="diary-text">{text}</div>\n'
                f'            </div>'
            )
    else:
        # Fallback: diary_text singola stringa (es. next/pipeline items)
        diary_text = _sanitize_diary_text(current.get("diary_text", "In lavorazione."))
        today_str = date.today().strftime("%d %b %Y").upper()
        slides.append(
            f'<div class="diary-slide active" id="ds-0">\n'
            f'              <div class="diary-date">{today_str} — aggiornamento</div>\n'
            f'              <div class="diary-text">{diary_text}</div>\n'
            f'            </div>'
        )

    return slides, len(slides)


def build_diary_dots(total: int) -> str:
    """Genera i dots HTML per il diario."""
    dots = []
    for i in range(total):
        active_class = " active" if i == 0 else ""
        dots.append(f'<div class="diary-dot{active_class}" data-i="{i}"></div>')
    return "\n              ".join(dots)


def update_diary_in_html(current: dict | None) -> None:
    """
    Aggiorna il Diario AIena in index.html.
    Supporta struttura a 1 o N slide:
    - Se current ha diary[] → usa tutte le voci (N slide)
    - Altrimenti usa diary_text → 1 slide
    """
    if not current:
        log("WARN: current_investigation null, diario non aggiornato")
        return

    with open(INDEX_LOCK, "w") as _lock_f:
        fcntl.flock(_lock_f, fcntl.LOCK_EX)
        try:
            _update_diary_locked(current)
        finally:
            fcntl.flock(_lock_f, fcntl.LOCK_UN)


def _update_diary_locked(current: dict) -> None:
    """Corpo di update_diary_in_html — eseguito sotto file lock."""
    html = INDEX_HTML.read_text(encoding="utf-8")

    # 1. Aggiorna la categoria nel diary-header
    category = current.get("category", "Indagine")
    html = re.sub(
        r'(<span class="diary-cat">)[^<]*(</span>)',
        rf'\g<1>{category}\g<2>',
        html
    )

    # 2. Costruisci slides e dots
    slides, total = build_diary_slides(current)
    slides_html = "\n            ".join(slides)
    dots_html = build_diary_dots(total)

    # 3. Sostituisci il contenuto interno di diary-body
    pattern = r'(<div class="diary-body">)\s*(.*?)\s*(</div>\s*<div class="diary-nav">)'

    def replace_diary_content(m):
        return m.group(1) + '\n            ' + slides_html + '\n          ' + m.group(3)

    new_html, n = re.subn(pattern, replace_diary_content, html, count=1, flags=re.DOTALL)

    if n == 0:
        log("WARN: pattern diary-body non trovato in index.html")
        return

    # 4. Aggiorna i dots
    new_html = re.sub(
        r'(<div class="diary-dots">)\s*.*?\s*(</div>\s*<a href="/segnala\.html")',
        rf'\g<1>\n              {dots_html}\n            \g<2>',
        new_html,
        flags=re.DOTALL
    )

    # 5. Aggiorna const total nel JS
    new_html = re.sub(r'const total = \d+;', f'const total = {total};', new_html)

    # Scrittura atomica
    _fd, _tmp = tempfile.mkstemp(dir=INDEX_HTML.parent, suffix=".html.tmp")
    try:
        with os.fdopen(_fd, "w", encoding="utf-8") as _f:
            _f.write(new_html)
        os.chmod(_tmp, 0o644)  # nginx (www-data) must read it; mkstemp creates 0600
        os.replace(_tmp, INDEX_HTML)
    except Exception:
        try:
            os.unlink(_tmp)
        except OSError:
            pass
        raise
    log(f"Diario aggiornato in index.html: {category} ({total} slide)")


def _update_incorso_cards(data: dict) -> None:
    """
    Aggiorna le 3 card 'In corso' in index.html con i primi 3 articoli della coda pubblicazione.
    Pipeline v2: Card 1 = investigations[0], Card 2 = investigations[1], Card 3 = investigations[2].
    """
    investigations = data.get("investigations", [])
    current = investigations[0] if len(investigations) > 0 else {}
    nxt = investigations[1] if len(investigations) > 1 else {}
    card3 = investigations[2] if len(investigations) > 2 else {}

    items = [current, nxt, card3]
    cards_html = ""
    for i, item in enumerate(items, 1):
        if not item.get("title"):
            continue
        title = item["title"]
        category = item.get("category", "Indagine")
        # Try diary_text first, fallback to summary/description
        excerpt_raw = (
            item.get("diary_text")
            or item.get("summary")
            or item.get("description")
            or ""
        )
        excerpt = excerpt_raw.replace("<strong>", "").replace("</strong>", "").replace("<em>", "").replace("</em>", "")
        # Truncate excerpt at 120 chars
        if len(excerpt) > 120:
            excerpt = excerpt[:117] + "..."
        # Status label
        status = item.get("status", "ricerca")
        status_map = {"in_progress": "In scrittura", "approved": "In preview", "preview": "In preview",
                      "ricerca": "In ricerca", "idea": "In ricerca", "attesa_risposta": "In attesa"}
        status_label = status_map.get(status, "In corso")
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
        return

    with open(INDEX_LOCK, "w") as _lock_f:
        fcntl.flock(_lock_f, fcntl.LOCK_EX)
        try:
            html = INDEX_HTML.read_text(encoding="utf-8")
            pattern = r'(<!-- Card 1 -->.*?)(</div>\s*</div>\s*</section>)'
            replacement = cards_html + r'        \2'
            new_html, n = re.subn(pattern, replacement, html, count=1, flags=re.DOTALL)
            if n == 0:
                log("WARN: pattern card 'In corso' non trovato in index.html")
                return
            # Scrittura atomica
            _fd, _tmp = tempfile.mkstemp(dir=INDEX_HTML.parent, suffix=".html.tmp")
            try:
                with os.fdopen(_fd, "w", encoding="utf-8") as _f:
                    _f.write(new_html)
                os.chmod(_tmp, 0o644)  # nginx (www-data) must read it; mkstemp creates 0600
                os.replace(_tmp, INDEX_HTML)
            except Exception:
                try:
                    os.unlink(_tmp)
                except OSError:
                    pass
                raise
        finally:
            fcntl.flock(_lock_f, fcntl.LOCK_UN)
    log(f"Card 'In corso' aggiornate: {current.get('title','?')} / {nxt.get('title','?')} / {card3.get('title','?')}")


def update_ticker(current: dict | None) -> None:
    """Chiama aiena_update_ticker.py con gli hashtag di investigations[0]."""
    if not current:
        log("WARN: investigations[0] null, ticker non aggiornato")
        return

    hashtags = current.get("hashtags", [])
    if not hashtags:
        log("WARN: nessun hashtag in investigations[0]")
        return

    title = current.get("title", "Prossima indagine")
    hashtags_str = " ".join(hashtags)

    cmd = [
        sys.executable, str(TICKER_SCRIPT),
        "--titolo", title,
        "--hashtags", hashtags_str
    ]

    log(f"Eseguo: {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode == 0:
        log("Ticker aggiornato")
    else:
        log(f"ERRORE ticker: {result.stderr}")


def deploy_ftp(file_path: Path, remote_path: str, slug: str = "") -> bool:
    """Deploya un file via FTP con logging nel ledger."""
    cmd = [
        "curl", "-s", "-T", str(file_path),
        f"ftp://{FTP_HOST}/{remote_path}",
        "--user", f"{FTP_USER}:{FTP_PASS}"
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode == 0:
        log(f"FTP OK: {remote_path}")
        # Registra nel deploy ledger
        ledger_ftp_from_file(
            script="aiena_post_publish",
            slug=slug,
            local_path=file_path,
            remote_path=remote_path,
            operation="ftp_upload",
            success=True
        )
        return True
    else:
        error_msg = result.stderr or "Unknown FTP error"
        log(f"FTP ERRORE {remote_path}: {error_msg}")
        ledger_ftp_from_file(
            script="aiena_post_publish",
            slug=slug,
            local_path=file_path,
            remote_path=remote_path,
            operation="ftp_upload",
            success=False,
            error=error_msg
        )
        return False


def cleanup_preview_file(slug: str) -> None:
    """
    Rimuove il file preview dopo la pubblicazione.
    1. Elimina il file locale /var/www/aiena.it/preview/{slug}.html
    2. Elimina il file dal FTP /preview/{slug}.html
    """
    # 1. Elimina file locale
    local_preview = PREVIEW_DIR / f"{slug}.html"
    if local_preview.exists():
        try:
            local_preview.unlink()
            log(f"Preview locale eliminato: {local_preview}")
        except Exception as e:
            log(f"WARN: impossibile eliminare preview locale: {e}")
    else:
        log(f"Preview locale non esistente (gia rimosso?): {local_preview}")

    # 2. Elimina da FTP
    try:
        ftp = ftplib.FTP(FTP_HOST)
        ftp.login(FTP_USER, FTP_PASS)
        try:
            ftp.delete(f"/preview/{slug}.html")
            log(f"Preview FTP eliminato: /preview/{slug}.html")
        except Exception as e:
            # Non fatale: il file potrebbe non esistere su FTP
            log(f"WARN: preview FTP non eliminato (potrebbe non esistere): {e}")
        ftp.quit()
    except Exception as e:
        log(f"WARN: connessione FTP per cleanup preview fallita: {e}")

    # Log nel journal
    journal_step("aiena_post_publish", slug, "cleanup_preview", "ok")


def update_research_log(article: dict) -> None:
    """Aggiorna aiena_research_log.json con i dati di ricerca dell'articolo appena pubblicato.

    Legge i campi opzionali dall'articolo pipeline:
      stats_fonti, stats_documenti, stats_connessioni, stats_ore
    Se nessun campo presente, skip silenzioso (non invasivo).
    Se l'articolo esiste già nel log (stesso slug), aggiorna i valori.
    """
    slug = article.get("slug", "")
    fonti = article.get("stats_fonti")
    documenti = article.get("stats_documenti")
    connessioni = article.get("stats_connessioni")
    ore = article.get("stats_ore")

    # Se nessun campo stats presente, skip silenzioso
    if all(v is None for v in [fonti, documenti, connessioni, ore]):
        log("research_log: nessun dato stats_ nell'articolo — skip")
        return

    # Carica log esistente
    try:
        with open(RESEARCH_LOG) as f:
            log_data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        log_data = []

    # Cerca entry esistente per questo slug
    existing = next((e for e in log_data if e.get("articolo") == slug), None)

    if existing:
        # Aggiorna i valori presenti
        if fonti is not None: existing["fonti"] = fonti
        if documenti is not None: existing["documenti"] = documenti
        if connessioni is not None: existing["connessioni"] = connessioni
        if ore is not None: existing["ore"] = ore
        log(f"research_log: aggiornata entry esistente per '{slug}'")
    else:
        # Crea nuova entry
        entry = {
            "articolo": slug,
            "titolo": article.get("title", ""),
            "data": date.today().isoformat(),
            "fonti": fonti or 0,
            "documenti": documenti or 0,
            "connessioni": connessioni or 0,
            "ore": ore or 0,
        }
        log_data.append(entry)
        log(f"research_log: aggiunta nuova entry per '{slug}' (fonti={fonti}, ore={ore})")

    # Salva
    with open(RESEARCH_LOG, "w") as f:
        json.dump(log_data, f, ensure_ascii=False, indent=2)
    journal_step("aiena_post_publish", slug, "update_research_log", "ok")


def main():
    parser = argparse.ArgumentParser(description="Post-pubblicazione aiena.it")
    parser.add_argument("--slug", required=True, help="Slug dell'articolo appena pubblicato")
    parser.add_argument("--dry-run", action="store_true", help="Non esegue FTP deploy")
    args = parser.parse_args()

    slug = args.slug
    log(f"=== POST-PUBBLICAZIONE: {slug} ===")

    # Backup pipeline.json prima di qualsiasi modifica (per rollback in caso di errore FTP)
    pipeline_backup = None
    if PIPELINE_JSON.exists():
        try:
            pipeline_backup = PIPELINE_JSON.read_text(encoding="utf-8")
            log("Backup pipeline.json creato in memoria")
        except Exception as e:
            log(f"WARN: impossibile creare backup pipeline.json: {e}")

    # 1. Carica pipeline
    data = load_pipeline()

    # 2. Trova e rimuovi articolo da investigations[]
    article, orig_index = find_and_remove_article(data, slug)
    if not article:
        log(f"ERRORE: articolo con slug '{slug}' non trovato in investigations[]")
        journal_step("aiena_post_publish", slug, "advance_pipeline", "fail", "Article not found in investigations[]")
        alert_failure("aiena_post_publish", slug, "advance_pipeline", "Article not found in pipeline.json investigations[]")
        sys.exit(1)

    log(f"Trovato in investigations[{orig_index}], rimosso")

    # 3. Sposta in published[]
    move_to_published(data, article)

    # 3b. Aggiorna research log (non invasivo — skip se stats_ non presenti)
    try:
        update_research_log(article)
    except Exception as e:
        log(f"WARN: update_research_log fallito: {e} — non blocca il processo")

    # Pipeline v2: non c'e bisogno di promozioni esplicite.
    # Dopo aver rimosso l'articolo da investigations[], il nuovo investigations[0]
    # diventa automaticamente la "current investigation".

    # Log advance_pipeline come OK
    journal_step("aiena_post_publish", slug, "advance_pipeline", "ok")

    if args.dry_run:
        log("DRY-RUN: pipeline non salvato, diario non aggiornato, ticker non aggiornato")
        investigations = data.get("investigations", [])
        log(f"DRY-RUN: nuova pipeline sarebbe -> investigations[0]={(investigations[0] if investigations else {}).get('slug')}, "
            f"investigations[1]={(investigations[1] if len(investigations) > 1 else {}).get('slug')}, "
            f"total_investigations={len(investigations)}")
        log("=== COMPLETATO (DRY-RUN) ===")
    else:
        # 6. Salva pipeline.json
        save_pipeline(data)

        # 7. Aggiorna diario in index.html (usa investigations[0] come current)
        update_diary_in_html(get_current_investigation(data))
        journal_step("aiena_post_publish", slug, "update_diario", "ok")

        # 8. Aggiorna ticker (usa investigations[0] come current)
        update_ticker(get_current_investigation(data))

        # 8b. Aggiorna le 3 card "In corso" in index.html con la coda pubblicazione corrente
        try:
            _update_incorso_cards(data)
            journal_step("aiena_post_publish", slug, "update_incorso_cards", "ok")
        except Exception as e:
            log(f"WARN: aggiornamento card in-corso fallito: {e}")

        # 8c. Aggiorna RSS feed.xml
        rss_script = Path("/home/pinky/.pinkybot/scripts/aiena_rss_update.py")
        if rss_script.exists():
            try:
                result = subprocess.run(
                    [sys.executable, str(rss_script)],
                    capture_output=True, text=True
                )
                if result.returncode == 0:
                    log("RSS feed.xml aggiornato ✓")
                    journal_step("aiena_post_publish", slug, "rss_update", "ok")
                else:
                    log(f"WARN: RSS update fallito: {result.stderr[:200]}")
            except Exception as e:
                log(f"WARN: RSS update error: {e}")

        # 9. Rebuild admin HTML con dati pipeline inline (sicurezza: pipeline.json non è pubblico)
        admin_rebuild = Path("/home/pinky/.pinkybot/scripts/aiena_admin_rebuild.py")
        if admin_rebuild.exists():
            try:
                result = subprocess.run(
                    [sys.executable, str(admin_rebuild), "--no-deploy"],
                    capture_output=True, text=True
                )
                if result.returncode == 0:
                    log("Admin HTML rebuilt con nuovi dati pipeline")
                else:
                    log(f"WARN: admin rebuild fallito: {result.stderr}")
            except Exception as e:
                log(f"WARN: admin rebuild error: {e}")

    # 10. Deploy FTP (solo se non dry-run — il blocco dry-run sopra ha già fatto return)
    if not args.dry_run:
        log("=== FTP DEPLOY ===")
        ftp_failed = False

        try:
            # FTP index.html
            ftp_ok = deploy_ftp(INDEX_HTML, "index.html", slug=slug)
            if ftp_ok:
                journal_step("aiena_post_publish", slug, "ftp_index", "ok")
            else:
                journal_step("aiena_post_publish", slug, "ftp_index", "fail", "FTP upload failed")
                alert_failure("aiena_post_publish", slug, "ftp_index", "FTP upload failed")
                ftp_failed = True

            # FTP pipeline.json
            ftp_ok = deploy_ftp(PIPELINE_JSON, "data/pipeline.json", slug=slug)
            if ftp_ok:
                journal_step("aiena_post_publish", slug, "ftp_pipeline", "ok")
            else:
                journal_step("aiena_post_publish", slug, "ftp_pipeline", "fail", "FTP upload failed")
                alert_failure("aiena_post_publish", slug, "ftp_pipeline", "FTP upload failed")
                ftp_failed = True
        except Exception as e:
            log(f"ERRORE CRITICO FTP: {e}")
            ftp_failed = True

        # Rollback pipeline.json locale se FTP ha fallito (scrittura atomica)
        if ftp_failed and pipeline_backup:
            log("FTP fallito — ripristino pipeline.json dal backup")
            try:
                import tempfile as _tempfile
                _fd, _tmp = _tempfile.mkstemp(dir=PIPELINE_JSON.parent, suffix=".tmp")
                try:
                    import os as _os
                    with _os.fdopen(_fd, "w", encoding="utf-8") as _f:
                        _f.write(pipeline_backup)
                    _os.replace(_tmp, PIPELINE_JSON)
                except Exception:
                    try:
                        _os.unlink(_tmp)
                    except OSError:
                        pass
                    raise
                log("Pipeline.json ripristinato con successo")
                journal_step("aiena_post_publish", slug, "rollback_pipeline", "ok")
            except Exception as e:
                log(f"ERRORE: impossibile ripristinare pipeline.json: {e}")
                journal_step("aiena_post_publish", slug, "rollback_pipeline", "fail", str(e))

        # Deploy admin HTML con dati aggiornati
        admin_html = Path("/var/www/aiena.it/admin/index.html")
        if admin_html.exists():
            deploy_ftp(admin_html, "admin/index.html", slug=slug)

        # Cleanup: rimuovi file preview locale e da FTP
        cleanup_preview_file(slug)

        # Notify ticket reporters about published articles
        log("Triggering ticket notifications...")
        try:
            notify_result = subprocess.run(
                [
                    "/home/pinky/.pinkybot/.venv/bin/python3",
                    "/home/pinky/.pinkybot/scripts/update_admin_data.py",
                    "--notify-tickets"
                ],
                capture_output=True,
                text=True,
                timeout=30
            )
            if notify_result.returncode == 0:
                log("Ticket notifications triggered")
                journal_step("aiena_post_publish", slug, "notify_tickets", "ok")
            else:
                log(f"WARN: ticket notification failed: {notify_result.stderr}")
                journal_step("aiena_post_publish", slug, "notify_tickets", "fail", notify_result.stderr)
        except Exception as e:
            log(f"WARN: ticket notification error: {e}")
            journal_step("aiena_post_publish", slug, "notify_tickets", "fail", str(e))

        # 10b. Pubblica su Substack (se config presente)
        substack_script = Path("/home/pinky/.pinkybot/scripts/aiena_substack_publisher.py")
        substack_config = Path("/home/pinky/.pinkybot/config/substack_config.json")
        if substack_script.exists() and substack_config.exists():
            try:
                result = subprocess.run(
                    [sys.executable, str(substack_script), "--slug", slug],
                    capture_output=True, text=True, timeout=60
                )
                if result.returncode == 0:
                    log("Substack: pubblicato ✓")
                    journal_step("aiena_post_publish", slug, "substack_publish", "ok")
                else:
                    log(f"WARN: Substack publish fallito: {result.stderr[:200]}")
                    journal_step("aiena_post_publish", slug, "substack_publish", "fail", result.stderr[:200])
            except Exception as e:
                log(f"WARN: Substack publish error: {e}")
        else:
            log("Substack: config non presente — skip")

        # Milestone finale
        journal_milestone("aiena_post_publish", slug, "PIPELINE_COMPLETE")

        log("=== COMPLETATO ===")
        print(f"\nArticolo pubblicato: {article['title']}")
        current = get_current_investigation(data)
        next_inv = get_next_investigation(data)
        if current:
            print(f"Nuova indagine corrente: {current['title']}")
        if next_inv:
            print(f"Prossima in coda: {next_inv['title']}")


if __name__ == "__main__":
    main()
