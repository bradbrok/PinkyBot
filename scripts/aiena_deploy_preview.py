#!/usr/bin/env python3
"""
aiena_deploy_preview.py — Deploy atomico preview articolo AIena

Esegue TUTTI i passi necessari in una sola chiamata:
  1. FTP upload → ftp.aiena.it/preview/SLUG.html (sito pubblico)
  2. Copia locale → /var/www/aiena.it/preview/SLUG.html (VPS, per admin panel)
  3. Update pipeline.json → leads[slug] promosso a investigations[preview]
  4. Rigenera admin data.json via update_admin_data.py

USO:
    python3 aiena_deploy_preview.py --slug SLUG --html /path/to/file.html

ESEMPIO:
    python3 aiena_deploy_preview.py \\
        --slug juve-stabia-camorra-agnello-1-euro \\
        --html /tmp/juve-stabia-camorra-agnello-1-euro.html \\
        --title "La Squadra di Calcio, la Camorra e l'Euro Simbolico" \\
        --category "Criminalità organizzata"
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import date
from pathlib import Path

from aiena_secrets import _load_secrets


# Lazy-load secrets to avoid crash if env/config unavailable at import time
_secrets_cache = None

def _get_secrets():
    global _secrets_cache
    if _secrets_cache is None:
        _secrets_cache = _load_secrets()
    return _secrets_cache

def _get_ftp_credentials():
    """Get FTP credentials lazily."""
    s = _get_secrets()
    return s.get("FTP_PASS", "")


def _atomic_write_json(path: Path, data) -> None:
    """Write JSON atomically: write to tmp in same dir, then os.replace().

    Prevents corruption if the process is interrupted mid-write.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        dir=str(path.parent),
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
        encoding="utf-8",
    ) as tf:
        json.dump(data, tf, ensure_ascii=False, indent=2)
        tf.flush()
        os.fsync(tf.fileno())
        tmp_path = tf.name
    os.replace(tmp_path, str(path))

# ── Config ───────────────────────────────────────────────────────────────────
VPS_PREVIEW_DIR = Path("/var/www/aiena.it/preview")
PIPELINE_JSON    = Path("/var/www/aiena.it/data/pipeline.json")
UPDATE_ADMIN_PY  = Path("/home/pinky/.pinkybot/scripts/update_admin_data.py")
PYTHON           = "/home/pinky/.pinkybot/.venv/bin/python3"
FTP_USER         = "aiena.it"
FTP_BASE         = "ftp://ftp.aiena.it/preview"
PUBLIC_BASE      = "https://www.aiena.it/preview"


def ftp_upload(local: Path, slug: str) -> bool:
    """Upload file to FTP."""
    url = f"{FTP_BASE}/{slug}.html"
    ftp_pass = _get_ftp_credentials()
    result = subprocess.run(
        ["curl", "-s", "-T", str(local), url, "--user", f"{FTP_USER}:{ftp_pass}"],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        print(f"[ERROR] FTP upload fallito: {result.stderr}", file=sys.stderr)
        return False
    print(f"[1/4] ✓ FTP upload → {url}")
    return True


def copy_to_vps(local: Path, slug: str) -> bool:
    """Copy HTML to local VPS preview dir (for update_admin_data.py scan)."""
    dest = VPS_PREVIEW_DIR / f"{slug}.html"
    try:
        shutil.copy2(str(local), str(dest))
        print(f"[2/4] ✓ Copia VPS locale → {dest}")
        return True
    except Exception as e:
        print(f"[ERROR] Copia VPS fallita: {e}", file=sys.stderr)
        return False


def update_pipeline(slug: str, title: str, category: str) -> bool:
    """Promuovi slug da leads[] a investigations[] in pipeline.json."""
    try:
        with open(PIPELINE_JSON) as f:
            d = json.load(f)

        # Cerca in leads[]
        lead_data = None
        new_leads = []
        for lead in d.get("leads", []):
            if lead.get("slug") == slug:
                lead_data = lead
            else:
                new_leads.append(lead)

        if lead_data:
            # Promuovi a investigations[]
            inv = dict(lead_data)
            inv["status"] = "preview"
            inv["pipeline_status"] = "preview"
            inv["has_preview"] = True
            inv["is_approved"] = False
            inv["preview_url"] = f"{PUBLIC_BASE}/{slug}.html"
            inv["preview_date"] = str(date.today())
            if title:
                inv["title"] = title
            if category:
                inv["category"] = category
            d.setdefault("investigations", []).append(inv)
            d["leads"] = new_leads
            print(f"[3/4] ✓ pipeline.json: leads[{slug}] → investigations[preview]")
        else:
            # Già in investigations o non trovato in leads — aggiorna status se c'è
            found = False
            for inv in d.get("investigations", []):
                if inv.get("slug") == slug:
                    inv["status"] = "preview"
                    inv["pipeline_status"] = "preview"  # F4-AIE-3 fix: mantieni in sync
                    inv["has_preview"] = True
                    inv["preview_url"] = f"{PUBLIC_BASE}/{slug}.html"
                    inv["preview_date"] = str(date.today())
                    found = True
                    print(f"[3/4] ✓ pipeline.json: investigations[{slug}] → status=preview")
                    break
            if not found:
                print(f"[3/4] ⚠ {slug} non trovato in leads[] né investigations[] — aggiunto come nuovo")
                d.setdefault("investigations", []).append({
                    "slug": slug,
                    "title": title or slug,
                    "category": category or "Inchiesta",
                    "status": "preview",
                    "pipeline_status": "preview",
                    "has_preview": True,
                    "is_approved": False,
                    "preview_url": f"{PUBLIC_BASE}/{slug}.html",
                    "preview_date": str(date.today()),
                    "added": str(date.today()),
                })

        d["updated_at"] = str(date.today())
        _atomic_write_json(PIPELINE_JSON, d)
        return True

    except Exception as e:
        print(f"[ERROR] Update pipeline.json fallito: {e}", file=sys.stderr)
        return False


def rebuild_admin() -> bool:
    """Rigenera data.json per admin.aiena.it."""
    result = subprocess.run(
        [PYTHON, str(UPDATE_ADMIN_PY)],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        print(f"[ERROR] update_admin_data.py fallito: {result.stderr[:200]}", file=sys.stderr)
        return False
    # Estrai riga chiave dall'output
    for line in result.stdout.splitlines():
        if "preview articles" in line or "SUCCESS" in line:
            print(f"[4/4] ✓ Admin rebuild: {line.strip()}")
    return True


def validate_preview_html(path: Path) -> list[str]:
    """
    Verifica invarianti strutturali di un preview HTML prima del deploy.
    Restituisce lista di errori trovati (vuota = tutto OK).

    F5-OUTPUT-CONFORMITY check — aggiunto 2026-06-16 per prevenire deploy
    di HTML generati da script ad-hoc che bypassano il pipeline standardizzato.
    """
    errors = []
    try:
        html = path.read_text("utf-8")
    except Exception as e:
        return [f"Cannot read file: {e}"]

    # Invarianti OBBLIGATORIE
    required = {
        ".preview-banner": '<div class="preview-banner"',
        ".ai-notice":      '<div class="ai-notice"',
        "canonical link":  '<link rel="canonical"',
        "robots noindex":  'name="robots"',
        "stylesheet":      '<link rel="stylesheet"',
    }
    for label, marker in required.items():
        if marker not in html:
            errors.append(f"MANCANTE: {label} ({marker!r})")

    # Invarianti VIETATE
    import re

    # <hr> dentro l'article body
    art_match = re.search(r'<article[^>]*>(.*?)</article>', html, re.DOTALL | re.IGNORECASE)
    if art_match:
        article_body = art_match.group(1)
        hr_count = len(re.findall(r'<hr[\s/]', article_body, re.IGNORECASE))
        if hr_count:
            errors.append(f"VIETATO: {hr_count} tag <hr> trovati in article body (usare section-divider)")

        # Testo grezzo di metadati visibile nel corpo
        if "**Categoria:**" in article_body or "<strong>Categoria:" in article_body:
            errors.append("VIETATO: metadati grezzi AIena visibili nel corpo (**Categoria:**)")
        if "**Slug:**" in article_body or "<strong>Slug:" in article_body:
            errors.append("VIETATO: metadati grezzi AIena visibili nel corpo (**Slug:**)")

        # Frontmatter YAML non parsato
        if "titolo:" in article_body and "data_pubblicazione:" in article_body:
            errors.append("VIETATO: frontmatter YAML non parsato visibile nel corpo")

    # CSS path: path relativo valido per /preview/subdir
    # ../styles.css è valido se il server lo risolve; /styles.css è sempre sicuro
    # MA se c'è href="../../../styles.css" o href="styles.css" (senza /) è sbagliato
    bad_css = re.search(r'href=["\'](?!\.\.?/|/)([^"\']+\.css)', html)
    if bad_css:
        errors.append(f"WARN: CSS path relativo non ancorato: {bad_css.group(0)!r}")

    return errors


def main():
    parser = argparse.ArgumentParser(description="Deploy atomico preview AIena")
    parser.add_argument("--slug", required=True, help="Slug articolo (es. juve-stabia-camorra-agnello-1-euro)")
    parser.add_argument("--html", required=True, help="Path file HTML locale")
    parser.add_argument("--title", default="", help="Titolo articolo (opzionale)")
    parser.add_argument("--category", default="", help="Categoria (opzionale)")
    parser.add_argument("--skip-validation", action="store_true", help="Salta validazione HTML (non raccomandato)")
    args = parser.parse_args()

    local = Path(args.html)
    if not local.exists():
        print(f"[ERROR] File non trovato: {local}", file=sys.stderr)
        sys.exit(1)

    print(f"\n=== Deploy Preview: {args.slug} ===")

    # F5-OUTPUT-CONFORMITY: valida HTML prima del deploy
    if not args.skip_validation:
        errors = validate_preview_html(local)
        if errors:
            print(f"\n[VALIDAZIONE FALLITA] {len(errors)} problema/i trovati:", file=sys.stderr)
            for e in errors:
                print(f"  ✗ {e}", file=sys.stderr)
            print("\n  ⛔ Deploy bloccato. Correggere l'HTML o usare --skip-validation.", file=sys.stderr)
            sys.exit(2)
        else:
            print("[0/4] ✓ Validazione HTML: OK (F5-OUTPUT-CONFORMITY)")

    ok1 = ftp_upload(local, args.slug)
    ok2 = copy_to_vps(local, args.slug)
    ok3 = update_pipeline(args.slug, args.title, args.category)
    ok4 = rebuild_admin()

    if all([ok1, ok2, ok3, ok4]):
        print(f"\n✅ Deploy completo → {PUBLIC_BASE}/{args.slug}.html")
        print(f"   Admin panel aggiornato: https://admin.aiena.it/")
    else:
        print(f"\n⚠ Deploy parziale — verifica i passaggi falliti", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
