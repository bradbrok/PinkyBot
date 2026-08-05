#!/usr/bin/env python3
"""
aiena_auto_publish.py - Autonomous publication system for aiena.it

This script runs via cron at 09:30 CET on Tue/Wed/Thu (publication days).
It checks Supabase for approved articles and publishes the oldest one.

Workflow:
1. Check if today is a publication day (Tue=1, Wed=2, Thu=3)
2. Fetch pending approvals from Supabase (oldest first)
3. Move article from preview/ to articles/
4. Update hero section in index.html
5. Update indagini.html (remove card, add next investigation)
6. Update archivio.html (add new published article)
7. Call aiena_post_publish.py (handles pipeline.json, diary, ticker)
8. FTP deploy all changed files
9. OTS stamp the published article
10. Mark approval as published in Supabase
11. Regenerate admin data.json
12. Notify Mirko via Telegram

Usage:
    python3 aiena_auto_publish.py [--dry-run] [--force]

    --dry-run: Skip FTP deploy, Supabase writes, and Telegram
    --force: Ignore day-of-week check (for testing)

Cron entry (add via crontab -e):
    30 9 * * 2,3,4 /home/pinky/.pinkybot/.venv/bin/python3 /home/pinky/.pinkybot/scripts/aiena_auto_publish.py >> /home/pinky/.pinkybot/data/logs/auto_publish.log 2>&1

Author: Engineer (PinkyBot)
"""

import argparse
import base64
import hashlib
import hmac
import json
import os
import re
import tempfile
import subprocess
import sys
import time
import urllib.request
import urllib.error
from datetime import date, datetime, timezone, timedelta
from pathlib import Path
from typing import Optional


def _atomic_write(path: Path, content: str, encoding: str = "utf-8") -> None:
    """Scrive atomicamente: tempfile + os.replace(). Previene corruzione su kill mid-write."""
    tmp_fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(tmp_fd, "w", encoding=encoding) as f:
            f.write(content)
        os.chmod(tmp_path, 0o644)  # nginx (www-data) must read it; mkstemp creates 0600
        os.replace(tmp_path, path)
    except:
        try:
            os.unlink(tmp_path)
        except:
            pass
        raise


# Logging strutturato per tracciamento deploy e pipeline
from aiena_logging import journal_step, journal_milestone, ledger_ftp_from_file, alert_failure

# ============================================================================
# Publication Audit Log
# ============================================================================

PUBLICATION_AUDIT_LOG = Path("/home/pinky/.pinkybot/data/logs/publication_audit.log")


def write_audit_log(
    action: str,
    slug: str,
    reason: str,
    lock_before: str = "",
    lock_after: str = ""
) -> None:
    """
    Write a structured audit log entry for publication events.
    Actions: published, skipped, aborted, error
    """
    try:
        PUBLICATION_AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        entry = (
            f"DATE: {ts}\n"
            f"ACTION: {action}\n"
            f"SLUG: {slug}\n"
            f"REASON: {reason}\n"
            f"LOCK_BEFORE: {lock_before}\n"
            f"LOCK_AFTER: {lock_after}\n"
            f"---\n"
        )
        with open(PUBLICATION_AUDIT_LOG, "a", encoding="utf-8") as f:
            f.write(entry)
    except Exception as e:
        log_warn(f"Failed to write audit log: {e}")

# ============================================================================
# Configuration
# ============================================================================

AIENA_ROOT = Path("/var/www/aiena.it")
PREVIEW_DIR = AIENA_ROOT / "preview"
ARTICLES_DIR = AIENA_ROOT / "articles"
INDEX_HTML = AIENA_ROOT / "index.html"
INDAGINI_HTML = AIENA_ROOT / "indagini.html"
ARCHIVIO_HTML = AIENA_ROOT / "archivio.html"
PIPELINE_JSON = AIENA_ROOT / "data" / "pipeline.json"
FEED_XML = AIENA_ROOT / "feed.xml"
SITEMAP_XML = AIENA_ROOT / "sitemap.xml"

SCRIPTS_DIR = Path("/home/pinky/.pinkybot/scripts")
POST_PUBLISH_SCRIPT = SCRIPTS_DIR / "aiena_post_publish.py"
OTS_STAMP_SCRIPT = SCRIPTS_DIR / "aiena_ots_stamp.py"
UPDATE_ADMIN_SCRIPT = SCRIPTS_DIR / "update_admin_data.py"

# FTP credentials
FTP_HOST = "ftp.aiena.it"
FTP_USER = "aiena.it"

# Supabase
SB_URL = "https://fwyjxolljcogblvwvfca.supabase.co"


def _load_secrets() -> dict:
    """Load sensitive credentials from .aiena_secrets file (chmod 600).
    Falls back to environment variables for cron/daemon contexts."""
    secrets: dict = {}
    secrets_file = Path("/home/pinky/.pinkybot/scripts/.aiena_secrets")
    if secrets_file.exists():
        for line in secrets_file.read_text().splitlines():
            line = line.strip()
            if line and "=" in line and not line.startswith("#"):
                k, _, v = line.partition("=")
                secrets[k.strip()] = v.strip()
    # env override (higher priority)
    import os
    for key in ("SB_SERVICE_KEY", "FTP_PASS"):
        if os.environ.get(key):
            secrets[key] = os.environ[key]
    return secrets


_SECRETS = _load_secrets()
FTP_PASS = _SECRETS.get("FTP_PASS", "")
SB_SERVICE_KEY = _SECRETS.get("SB_SERVICE_KEY", "")

# Telegram notification
BROKER_URL = "http://localhost:8888/broker/send"
MIRKO_CHAT_ID = "32405655"
# Caller identity for HMAC-signed internal broker requests. Matches the
# body `agent_name` used in the broker payloads below, and mirrors the proven
# pattern in aiena_ticket_handler.py. Must be a registered, non-isolated agent
# for the daemon to accept the shared session secret (api.py #497 hardening).
BROKER_AGENT = "satoshi"


def _build_broker_headers(method: str, path: str) -> dict:
    """Build HMAC-signed headers for internal /broker requests.

    The daemon's auth_middleware (#497) default-denies unsigned requests to
    /broker/* with 401. We sign with PINKY_SESSION_SECRET, read from the env
    or falling back to the daemon .env (cron does not inherit the daemon env).
    Returns only Content-Type when the secret is unavailable so the caller
    still issues a request (which will 401 and be logged), never crashes.
    """
    secret = os.environ.get("PINKY_SESSION_SECRET", "")
    if not secret:
        env_file = Path("/home/pinky/.pinkybot/.env")
        if env_file.exists():
            for line in env_file.read_text().splitlines():
                if line.startswith("PINKY_SESSION_SECRET="):
                    secret = line.split("=", 1)[1].strip().strip('"').strip("'")
                    break
    if not secret:
        return {"Content-Type": "application/json"}
    ts = int(time.time())
    normalized_path = path.split("?", 1)[0]
    payload = f"{BROKER_AGENT}\n{method.upper()}\n{normalized_path}\n{ts}".encode("utf-8")
    digest = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).digest()
    signature = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return {
        "x-pinky-agent": BROKER_AGENT,
        "x-pinky-timestamp": str(ts),
        "x-pinky-signature": signature,
        "Content-Type": "application/json",
    }

# Publication days: Tuesday=1, Wednesday=2, Thursday=3
PUBLICATION_DAYS = {1, 2, 3}  # weekday() values

# Italian month abbreviations
MONTHS_IT = ['gen', 'feb', 'mar', 'apr', 'mag', 'giu', 'lug', 'ago', 'set', 'ott', 'nov', 'dic']


# ============================================================================
# Logging
# ============================================================================

def log(msg: str, level: str = "INFO") -> None:
    """Log with timestamp."""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] [{level}] {msg}")


def log_error(msg: str) -> None:
    log(msg, "ERROR")


def log_warn(msg: str) -> None:
    log(msg, "WARN")


# ============================================================================
# Date formatting
# ============================================================================

def date_it(d: date) -> str:
    """Format date as Italian: '03 mag 2026'."""
    return f"{d.day:02d} {MONTHS_IT[d.month - 1]} {d.year}"


def date_it_long(d: date) -> str:
    """Format date as Italian long: '3 maggio 2026'."""
    months_long = ['gennaio', 'febbraio', 'marzo', 'aprile', 'maggio', 'giugno',
                   'luglio', 'agosto', 'settembre', 'ottobre', 'novembre', 'dicembre']
    return f"{d.day} {months_long[d.month - 1]} {d.year}"


# ============================================================================
# Supabase helpers
# ============================================================================

def sb_request(method: str, path: str, data: Optional[dict] = None) -> Optional[dict | list]:
    """Make a Supabase REST API request."""
    url = f"{SB_URL}/rest/v1/{path}"
    headers = {
        "apikey": SB_SERVICE_KEY,
        "Authorization": f"Bearer {SB_SERVICE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation"
    }

    body = json.dumps(data).encode() if data else None
    req = urllib.request.Request(url, data=body, headers=headers, method=method)

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            if resp.status in (200, 201):
                content = resp.read().decode()
                return json.loads(content) if content else None
            return None
    except urllib.error.HTTPError as e:
        log_error(f"Supabase HTTP {e.code}: {e.read().decode()}")
        return None
    except Exception as e:
        log_error(f"Supabase error: {e}")
        return None


def fetch_pending_approvals() -> list:
    """
    Fetch pending approvals sorted by pipeline v2 order.
    Order: investigations[] order (by index), then by priority field, then leftover by date.
    Returns list with the FIRST article to publish as element [0].

    IMPORTANT: Each approval includes 'publish_date' field from Supabase which is
    the authoritative scheduled publication date. The caller MUST check this date
    before publishing.
    """
    all_pending = sb_request("GET", "article_approvals?status=eq.pending&order=approved_at.asc")
    if not all_pending or not isinstance(all_pending, list):
        return []

    # Load pipeline v2 to determine order based on investigations[]
    try:
        pipeline = json.loads(PIPELINE_JSON.read_text(encoding="utf-8"))

        # Build priority map: slug -> (index_in_investigations, priority_field)
        # Lower index = higher priority; within same index, use priority field
        investigation_order: dict[str, tuple[int, int]] = {}
        # Also build pub_date map from pipeline for date guard
        pipeline_pub_dates: dict[str, str] = {}
        today_str = date.today().isoformat()
        for idx, item in enumerate(pipeline.get("investigations", [])):
            slug = item.get("slug", "")
            if slug:
                priority = item.get("priority", 99)
                investigation_order[slug] = (idx, priority)
                pub_date = item.get("pub_date")
                if pub_date:
                    pipeline_pub_dates[slug] = pub_date

        pending_by_slug = {a["slug"]: a for a in all_pending}

        # Sort approvals: investigations[] order first, then pub_date for leftovers
        sorted_approvals = []

        # First: items in investigations[], sorted by their index
        # CRITICAL: Skip articles whose pipeline pub_date is in the future
        # Il calendario comanda — non l'ordine di approvazione
        in_pipeline = [(slug, investigation_order[slug]) for slug in pending_by_slug if slug in investigation_order]
        in_pipeline.sort(key=lambda x: x[1])  # Sort by (index, priority)
        for slug, _ in in_pipeline:
            pub_date = pipeline_pub_dates.get(slug)
            if pub_date and pub_date > today_str:
                log_warn(f"SKIP {slug}: pub_date={pub_date} è nel futuro (oggi={today_str}). Il calendario comanda.")
                continue  # Do not include future-dated articles
            approval = pending_by_slug.pop(slug)
            # Annotate with pipeline pub_date so callers can use it
            if pub_date:
                approval["_pipeline_pub_date"] = pub_date
            sorted_approvals.append(approval)

        # Then: remaining approvals (not in investigations[]) by approved_at date
        sorted_approvals.extend(pending_by_slug.values())
        return sorted_approvals
    except Exception as e:
        log_warn(f"Could not sort approvals by pipeline order: {e}. Using date order.")
        return all_pending


def check_publish_date_guard(approval: dict, force: bool = False) -> tuple[bool, str]:
    """
    PRIMARY GUARD: Check if the article's publish_date allows publication today.

    Returns (can_publish, reason).
    - If publish_date is in the future and not --force: (False, "reason")
    - If publish_date is today or in the past: (True, "")
    - If publish_date is None/empty: (True, "") - no date restriction

    This is the AUTHORITATIVE check from Supabase approval record.
    """
    today = date.today()
    publish_date_str = approval.get("publish_date")

    if not publish_date_str:
        # No scheduled date - can publish anytime
        return True, ""

    try:
        # Handle both date string and datetime string
        if "T" in publish_date_str:
            publish_date = date.fromisoformat(publish_date_str[:10])
        else:
            publish_date = date.fromisoformat(publish_date_str)

        if publish_date > today:
            reason = (
                f"Article scheduled for {publish_date.isoformat()} (today is {today.isoformat()}). "
                f"Publication blocked: article has a future publish_date."
            )
            if force:
                return True, f"FORCE: {reason} - publishing anyway due to --force flag"
            return False, reason

        return True, ""
    except (ValueError, TypeError) as e:
        log_warn(f"Invalid publish_date format '{publish_date_str}': {e}")
        # Invalid date format - allow publication but log warning
        return True, f"WARNING: Invalid publish_date format '{publish_date_str}'"


def mark_approval_published(approval_id: str, dry_run: bool = False) -> bool:
    """Mark approval as published in Supabase."""
    if dry_run:
        log("DRY-RUN: Skipping Supabase approval update")
        return True

    now_iso = datetime.now(timezone.utc).isoformat()
    result = sb_request("PATCH", f"article_approvals?id=eq.{approval_id}", {
        "status": "published",
        "published_at": now_iso
    })
    return result is not None


def mark_approval_error(approval_id: str, dry_run: bool = False) -> bool:
    """Mark approval as error in Supabase."""
    if dry_run:
        log("DRY-RUN: Skipping Supabase error update")
        return True

    result = sb_request("PATCH", f"article_approvals?id=eq.{approval_id}", {
        "status": "error"
    })
    return result is not None


def check_and_recover_stale_errors(dry_run: bool = False) -> None:
    """W2: Check for articles stuck in 'error' status.

    Articles that have been in 'error' for >24h are automatically reset to
    'pending' so the next cron run can retry them.  Mirko is alerted for each
    reset so he can investigate the root cause.
    """
    error_items = sb_request("GET", "article_approvals?status=eq.error&order=approved_at.asc")
    if not error_items or not isinstance(error_items, list):
        return

    now = datetime.now(timezone.utc)
    stale_threshold_hours = 24

    for item in error_items:
        approval_id = item.get("id", "")
        slug = item.get("slug", item.get("title", "unknown"))
        approved_at_str = item.get("approved_at", "")

        # Parse approved_at timestamp
        stale = False
        age_hours = 0.0
        if approved_at_str:
            try:
                # Supabase ISO format: "2026-06-01T10:11:56+00:00" or "...Z"
                approved_at = datetime.fromisoformat(
                    approved_at_str.replace("Z", "+00:00")
                )
                age_hours = (now - approved_at).total_seconds() / 3600
                stale = age_hours > stale_threshold_hours
            except (ValueError, TypeError):
                stale = True  # unknown age → treat as stale

        if stale:
            log_warn(f"Stale error ({age_hours:.0f}h): {slug} [{approval_id}] → resetting to pending")
            if not dry_run:
                sb_request("PATCH", f"article_approvals?id=eq.{approval_id}", {
                    "status": "pending"
                })
                notify_telegram(
                    f"⚠️ Auto-recovery: articolo '{slug}' era bloccato in stato 'error' "
                    f"da {age_hours:.0f}h → reimpostato a 'pending' per retry automatico. "
                    f"Controlla i log per la causa originale.",
                    dry_run=dry_run,
                )
        else:
            log_warn(f"Recent error ({age_hours:.1f}h): {slug} [{approval_id}] — skip recovery (< {stale_threshold_hours}h)")


# ============================================================================
# HTML helpers
# ============================================================================

_INTERNAL_NOTES_PATTERN = re.compile(
    r'(Categoria:|Slug:|Data\s+draft:|Pub_date\s+prevista:|Pub_date:|Nota\s*:|Versione\s*\d)',
    re.IGNORECASE,
)


def _is_internal_notes(text: str) -> bool:
    """Restituisce True se il testo contiene pattern di note interne da draft pipeline."""
    return bool(_INTERNAL_NOTES_PATTERN.search(text))


def extract_excerpt(html: str) -> str:
    """Extract meta description from HTML.
    Sanitizes the result to remove unescaped quotes that would break HTML attributes.
    Returns empty string if the description contains internal draft notes \u2014 caller
    will use the fallback 'Indagine AIena su ...' string instead.
    """
    m = re.search(r'<meta\s+name=["\']description["\']\s+content=["\'](.*?)["\']', html, re.IGNORECASE)
    if m:
        excerpt = m.group(1)
        # Guard: se contiene note interne, rifiuta e forza fallback
        if _is_internal_notes(excerpt):
            log_warn(f"extract_excerpt: meta description contiene note interne \u2014 excerpt scartato: '{excerpt[:80]}...'")
            return ""
        # Sanitize: replace ASCII/Unicode quotes that would break HTML content="" attributes
        excerpt = excerpt.replace('"', '&quot;').replace('\u201c', '&ldquo;').replace('\u201d', '&rdquo;')
        return excerpt
    # Fallback: try og:description
    m2 = re.search(r'property=["\']og:description["\']\s+content=["\'](.*?)["\']', html, re.IGNORECASE)
    if m2:
        excerpt2 = m2.group(1)
        if _is_internal_notes(excerpt2):
            log_warn(f"extract_excerpt: og:description contiene note interne \u2014 excerpt scartato: '{excerpt2[:80]}...'")
            return ""
        return excerpt2
    return ""


def extract_h1(html: str) -> str:
    """Extract H1 title from HTML."""
    # Try hero-title first
    m = re.search(r'<h1[^>]*class=["\'][^"\']*hero-title["\'][^>]*>([^<]+)</h1>', html, re.IGNORECASE)
    if not m:
        m = re.search(r'<h1[^>]*>([^<]+)</h1>', html, re.IGNORECASE)
    return m.group(1).strip() if m else ""


def estimate_read_time(html: str) -> int:
    """Estimate read time from HTML content (words / 200 = minutes)."""
    # Remove HTML tags
    text = re.sub(r'<[^>]+>', '', html)
    # Remove extra whitespace
    text = ' '.join(text.split())
    word_count = len(text.split())
    return max(1, word_count // 200)


# ============================================================================
# File operations
# ============================================================================

def validate_preview_before_publish(preview_path: Path, slug: str) -> tuple[bool, list[str]]:
    """
    Valida il file preview prima della pubblicazione.
    Controlla: fonti, canonical. (NewsArticle schema, AI disclaimer, open-question
    vengono iniettati da transform_preview_to_production_v2 — NON devono essere
    nel preview file. Fix 2026-06-10, allineato con urgente_processor fix 2026-06-09.)
    Ritorna (is_valid, list_of_errors).
    """
    errors = []
    try:
        html = preview_path.read_text(encoding="utf-8")

        # NOTE: Schema NewsArticle, AI disclaimer (.ai-notice), open-question box
        # vengono tutti iniettati da aiena_template_builder::transform_preview_to_production_v2().
        # Il preview file NON li contiene — controllarli qui produrrebbe falsi negativi.
        # (Stesso fix applicato su aiena_urgente_processor.py il 2026-06-09.)

        # 3. Fonti verificabili (min 5)
        # Conta <li[^>]*><a href="https://..."> nella sezione fonti
        # FIX 2026-06-09: regex era r'<li><a' — non matchava <li id="fn1"><a> o <li class="..."><a>
        sources_li = re.findall(r'<li[^>]*><a href="https?://', html)
        # Conta link esterni escludendo nav/footer/assets (aiena.it, fonts, googleapis, cdn)
        all_ext = [
            lnk for lnk in re.findall(r'href="(https?://[^"]+)"', html)
            if not any(
                domain in lnk
                for domain in ("aiena.it", "fonts.g", "googleapis", "gstatic", "cdn.", "schema.org")
            )
        ]
        # FIX: era 'and' (check non scattava mai perché all_ext include link nav/footer)
        # Ora 'or': basta che UNA delle due condizioni indichi fonti insufficienti
        if len(sources_li) < 5 or len(all_ext) < 5:
            errors.append(f"Fonti insufficienti: {len(sources_li)} in <li>, {len(all_ext)} ext (minimo 5)")

        # 4. Open question — il template_builder la copia dal preview as-is.
        # BLOCKER 2026-07-07: se il preview contiene il placeholder "[Inserire la domanda..."
        # viene pubblicato letteralmente in produzione. Controlla sia nel preview che nel body.
        PLACEHOLDER_PATTERNS = [
            r'\[Inserire la domanda aperta',
            r'\[inserire la domanda',
            r'\[INSERIRE',
            r'\[placeholder',
            r'\[da compilare',
            r'\[TO DO\]',
        ]
        for pat in PLACEHOLDER_PATTERNS:
            if re.search(pat, html, re.IGNORECASE):
                errors.append(
                    f"BLOCKER: Placeholder non sostituito trovato (pattern: '{pat}'). "
                    "La domanda senza risposta deve essere scritta da AIena prima della pubblicazione."
                )
                break  # Un errore è sufficiente

        # 5. Canonical presente (non blocca, solo warn)
        if 'rel="canonical"' not in html and "rel='canonical'" not in html:
            errors.append("MANCA canonical (verrà aggiunto automaticamente)")

        # 5b. Meta description — verifica che non contenga quote non escaped
        meta_m = re.search(r'<meta\s+name=["\']description["\']\s+content="([^"]*)"', html)
        if meta_m:
            desc = meta_m.group(1)
            # Se il testo estratto termina abruptamente (meno di 30 chars) potrebbe essere troncato
            if len(desc) < 30:
                errors.append(f"Meta description sospettosamente corta ({len(desc)} chars) — possibile quote non escaped nel testo")

        # 6. Diary banner check (solo se presente — non obbligatorio)
        if 'diary-preview-banner' in html:
            if '.diary-preview-banner' not in html:
                errors.append("WARN: diary-preview-banner HTML presente ma CSS mancante nel <style>")
            # Check date progressive nel diary
            import re as _re
            dates = _re.findall(r'letter-spacing:\.05em">([^<]+)</strong>', html)
            if dates and len(set(dates)) == 1 and len(dates) > 1:
                errors.append(f"WARN: diary ha {len(dates)} slide con date identiche: '{dates[0]}'")

        is_valid = len(errors) == 0
        return is_valid, errors
    except Exception as e:
        return False, [f"Errore lettura file: {e}"]


_PRODUCTION_CSS_VERSION = "20260505"

_BC_BOX_CSS = """    /* ── Blockchain Integrity Box ─────────────────────────────────────────── */
    .bc-box { margin: 2.5rem 0 1.5rem; border: 1px solid rgba(255,58,46,0.25); background: linear-gradient(135deg, rgba(255,58,46,0.04) 0%, rgba(13,13,13,0) 60%); position: relative; overflow: hidden; }
    .bc-box::before { content: ''; position: absolute; top: 0; left: 0; right: 0; height: 1px; background: linear-gradient(90deg, rgba(255,58,46,0.6) 0%, rgba(255,58,46,0) 100%); }
    .bc-header { display: flex; align-items: center; justify-content: space-between; padding: .7rem 1.25rem; border-bottom: 1px solid rgba(255,255,255,0.05); gap: .5rem; flex-wrap: wrap; cursor: pointer; transition: opacity .15s; }
    .bc-header:hover { opacity: .85; } .bc-toggle-arrow { font-size: .75rem; color: rgba(255,58,46,.5); transition: transform .2s; flex-shrink: 0; }
    .bc-box.bc-open .bc-toggle-arrow { transform: rotate(180deg); } .bc-header-left { display: flex; align-items: center; gap: .6rem; }
    .bc-dot { width: 7px; height: 7px; border-radius: 50%; background: var(--red); box-shadow: 0 0 6px rgba(255,58,46,0.6); flex-shrink: 0; }
    .bc-title { font-family: var(--font-mono); font-size: .68rem; font-weight: 700; letter-spacing: .12em; text-transform: uppercase; color: rgba(255,58,46,.85); }
    .bc-chips { display: flex; gap: .35rem; } .bc-chip { font-family: var(--font-mono); font-size: .53rem; letter-spacing: .1em; text-transform: uppercase; color: rgba(255,255,255,.3); border: 1px solid rgba(255,255,255,.1); padding: .15rem .55rem; }
    .bc-hashvis { display: flex; gap: 2px; padding: .65rem 1.25rem .5rem; flex-wrap: wrap; } .bc-hb { width: 8px; height: 8px; border-radius: 1px; flex-shrink: 0; opacity: .75; }
    .bc-body { padding: .1rem 1.25rem .85rem; display: flex; flex-direction: column; gap: .65rem; } .bc-field { display: flex; align-items: flex-start; gap: .85rem; }
    .bc-label { font-family: var(--font-mono) !important; font-size: .6rem !important; color: rgba(255,255,255,.28); flex-shrink: 0; min-width: 115px; padding-top: 2px; letter-spacing: .04em; }
    .bc-hashtext { font-family: var(--font-mono) !important; font-size: .6rem !important; color: rgba(255,255,255,.55); word-break: break-all; line-height: 1.7; display: flex; align-items: flex-start; gap: .5rem; flex-wrap: wrap; }
    .bc-copy { background: none; border: 1px solid rgba(255,255,255,.1); color: rgba(255,255,255,.3); font-family: var(--font-mono); font-size: .5rem; padding: .1rem .35rem; cursor: pointer; flex-shrink: 0; transition: all .2s; margin-top: 1px; }
    .bc-copy:hover { border-color: rgba(255,58,46,.4); color: rgba(255,58,46,.7); }
    .bc-val { font-family: var(--font-mono) !important; font-size: .62rem !important; color: rgba(255,255,255,.38); line-height: 1.55; font-weight: normal !important; }
    .bc-val a { color: rgba(255,58,46,.8); text-decoration: none; border-bottom: 1px solid rgba(255,58,46,.25); padding-bottom: 1px; }
    .bc-status-dot { display: inline-block; width: 6px; height: 6px; border-radius: 50%; background: #f59e0b; margin-right: .4rem; vertical-align: middle; animation: bc-blink 2s ease-in-out infinite; }
    @keyframes bc-blink { 0%,100%{opacity:.4} 50%{opacity:1} }
    .bc-note { border-top: 1px solid rgba(255,255,255,.04); padding: .6rem 1.25rem; display: flex; align-items: center; gap: .75rem; flex-wrap: wrap; justify-content: space-between; }
    .bc-note-text { font-family: var(--font-mono) !important; font-size: .57rem !important; color: rgba(255,255,255,.18) !important; line-height: 1.6 !important; font-weight: normal !important; font-style: normal !important; margin: 0 !important; }
    .bc-info-btn { font-family: var(--font-mono); font-size: .57rem; letter-spacing: .06em; color: rgba(255,58,46,.6); background: none; white-space: nowrap; cursor: pointer; border: 1px solid rgba(255,58,46,.25); padding: .25rem .65rem; transition: all .2s; flex-shrink: 0; }
    .bc-info-btn:hover { background: rgba(255,58,46,.08); color: rgba(255,58,46,.95); border-color: rgba(255,58,46,.55); }
    .bc-explain { display: none; padding: .8rem 1.25rem 1rem; border-top: 1px solid rgba(255,58,46,.1); background: rgba(255,58,46,.03); } .bc-explain.open { display: block; }
    .bc-explain-title { font-family: var(--font-mono) !important; font-size: .62rem !important; font-weight: 700 !important; color: rgba(255,255,255,.5) !important; letter-spacing: .06em; margin-bottom: .5rem; display: block; }
    .bc-explain-text { font-family: var(--font-sans) !important; font-size: .82rem !important; color: rgba(255,255,255,.45) !important; line-height: 1.65 !important; font-weight: normal !important; font-style: normal !important; margin: 0 !important; }
    .bc-explain-text strong { color: rgba(255,255,255,.7); font-weight: 600; }
    .bc-history { border-top: 1px solid rgba(255,255,255,.04); padding: .5rem 1.25rem .65rem; display: none; flex-direction: column; gap: .3rem; }
    .bc-box.bc-open .bc-history { display: flex; } .bc-history-label { font-family: var(--font-mono) !important; font-size: .55rem !important; color: rgba(255,255,255,.2); letter-spacing: .08em; margin-bottom: .2rem; display: block; }
    .bc-history-item { display: flex; gap: .6rem; align-items: baseline; flex-wrap: wrap; } .bc-history-hash { font-family: var(--font-mono); font-size: .55rem; color: rgba(255,255,255,.2); } .bc-history-date { font-family: var(--font-mono); font-size: .55rem; color: rgba(255,255,255,.15); } .bc-history-note { font-family: var(--font-mono); font-size: .55rem; color: rgba(255,255,255,.25); font-style: italic; }
    .bc-box .bc-hashvis,.bc-box .bc-body,.bc-box .bc-note,.bc-box .bc-explain{display:none;}
    .bc-box.bc-open .bc-hashvis{display:flex;} .bc-box.bc-open .bc-body{display:flex;} .bc-box.bc-open .bc-note{display:flex;} .bc-box.bc-open .bc-explain.open{display:block;}
    .bc-verify-result{font-family:var(--font-mono);font-size:.62rem;line-height:1.6;padding:.45rem .9rem;margin:0 1.25rem .5rem;border-radius:3px;display:none;}
    .bc-verify-ok{color:#22c55e;background:rgba(34,197,94,.06);border:1px solid rgba(34,197,94,.2);} .bc-verify-warn{color:#f59e0b;background:rgba(245,158,11,.06);border:1px solid rgba(245,158,11,.2);} .bc-verify-err{color:#ff3a2e;background:rgba(255,58,46,.06);border:1px solid rgba(255,58,46,.2);}"""

_STATS_CSS = """    .footer-copy-wrap{display:inline-flex;align-items:center;gap:.5rem;}
    .stats-trigger{width:8px;height:8px;background:#e63946;border:none;cursor:pointer;padding:0;flex-shrink:0;animation:spin-slow 8s linear infinite;outline:none;opacity:.7;transition:opacity .2s;} .stats-trigger:hover{opacity:1;}
    @keyframes spin-slow{from{transform:rotate(0deg)}to{transform:rotate(360deg)}}
    .stats-overlay{display:none;position:fixed;inset:0;background:rgba(0,0,0,.75);z-index:999;align-items:center;justify-content:center;backdrop-filter:blur(4px);} .stats-overlay.open{display:flex;}
    .stats-modal{background:#161616;border:1px solid rgba(230,57,70,.3);max-width:480px;width:calc(100% - 2rem);padding:2rem;position:relative;}
    .stats-modal-close{position:absolute;top:.75rem;right:1rem;background:none;border:none;color:#999;font-size:1.2rem;cursor:pointer;}
    .stats-modal-title{font-family:'JetBrains Mono',monospace;font-size:.6rem;letter-spacing:.14em;text-transform:uppercase;color:rgba(230,57,70,.7);margin-bottom:.4rem;}
    .stats-modal-headline{font-family:'Playfair Display',Georgia,serif;font-size:1.4rem;font-weight:700;color:#f5f5f0;margin-bottom:1.5rem;}
    .stats-big{text-align:center;margin-bottom:1.5rem;padding:1.2rem 0;border-top:1px solid #2a2a2a;border-bottom:1px solid #2a2a2a;}
    .stats-big-number{font-family:'Playfair Display',Georgia,serif;font-size:4rem;font-weight:700;color:#e63946;line-height:1;transition:opacity .4s;}
    .stats-big-label{font-family:'JetBrains Mono',monospace;font-size:.65rem;color:#999;letter-spacing:.1em;text-transform:uppercase;margin-top:.4rem;}
    .stats-grid{display:grid;grid-template-columns:1fr 1fr;gap:.75rem;} .stats-item{display:flex;flex-direction:column;gap:.2rem;}
    .stats-item-val{font-family:'JetBrains Mono',monospace;font-size:1.1rem;font-weight:600;color:#f5f5f0;} .stats-item-lbl{font-family:'JetBrains Mono',monospace;font-size:.58rem;color:#999;letter-spacing:.08em;text-transform:uppercase;}"""

_FULL_FOOTER_HTML = """  <!-- FOOTER -->
  <footer class="site-footer">
    <div class="footer-inner">
      <div class="footer-top">
        <div class="footer-logo"><span>AI</span>ena.it</div>
        <nav class="footer-nav">
          <a href="/archivio.html">Archivio</a>
          <a href="/chi-siamo.html">Chi sono</a>
          <a href="/privacy.html">Privacy</a>
          <a href="https://bsky.app/profile/aiena-it.bsky.social" target="_blank" rel="noopener">Bluesky</a>
          <a href="/segnala.html">Segnala</a>
        </nav>
      </div>
      <div class="footer-disclaimer">
        <p>AIena.it non è una testata giornalistica registrata ai sensi della Legge n. 47/1948. I contenuti pubblicati hanno carattere informativo e di ricerca, basati su fonti pubblicamente verificabili, e non costituiscono notizie giornalistiche in senso tecnico-giuridico. AIena è un sistema di intelligenza artificiale. Ogni contenuto è soggetto a supervisione editoriale prima della pubblicazione. I fatti riportati sono verificabili attraverso le fonti indicate. Ai sensi del D.Lgs. 70/2003, per contattare il responsabile del sito usa la pagina <a href="/segnala.html">Segnala</a>.</p>
      </div>
      <div class="trust-strip">
        <div class="trust-badges">
          <span class="trust-badge tb-bitcoin">Articoli certificati su blockchain</span>
          <span class="trust-badge tb-green">Correzioni pubbliche</span>
          <span class="trust-badge">Segnalazioni anonime</span>
          <span class="trust-badge">AI + revisione umana</span>
          <span class="trust-badge">Zero inserzionisti</span>
          <span class="trust-badge">Zero conflitti di interesse</span>
          <span class="trust-badge"><a href="http://obvorhi24h6ekd3rdjxuekcrd2endtcp37g6gxtfucxxp3o6vrjrzvad.onion" style="color:inherit;text-decoration:none;" target="_blank" rel="noopener">Mirror .onion</a></span>
        </div>
      </div>
      <div class="footer-bottom">
        <div class="footer-copy-wrap"><span class="footer-copy">© 2026 AIena.it — Prima AI investigativa italiana</span><button class="stats-trigger" id="statsTrigger" title="Statistiche AIena" aria-label="Statistiche AIena"></button></div>
        <span class="footer-motto">"Tutti hanno diritto di sapere. Nessuno ha il diritto di far dimenticare."</span>
        <span class="footer-credit" style="font-family:var(--font-mono);font-size:.65rem;color:#777;margin-top:.5rem;display:block;text-align:center;">con amore da <a href="https://t.me/ziomik" target="_blank" rel="noopener" style="color:#999;text-decoration:none;">@ziomik</a></span>
      </div>
    </div>
  </footer>

  <div class="stats-overlay" id="statsOverlay"><div class="stats-modal" role="dialog" aria-modal="true" aria-label="Statistiche AIena"><button class="stats-modal-close" id="statsClose" aria-label="Chiudi">✕</button><div class="stats-modal-title">Trasparenza — dati reali</div><div class="stats-modal-headline">Quanto lavora AIena</div><div class="stats-big"><div class="stats-big-number" id="bigNum">312</div><div class="stats-big-label" id="bigLbl">documenti esaminati</div></div><div class="stats-grid"><div class="stats-item"><span class="stats-item-val">47</span><span class="stats-item-lbl">Fonti analizzate</span></div><div class="stats-item"><span class="stats-item-val">140+</span><span class="stats-item-lbl">Ore di ricerca</span></div><div class="stats-item"><span class="stats-item-val">89</span><span class="stats-item-lbl">Connessioni trovate</span></div><div class="stats-item"><span class="stats-item-val">1</span><span class="stats-item-lbl">Articoli pubblicati</span></div><div class="stats-item"><span class="stats-item-val">3</span><span class="stats-item-lbl">Inchieste in corso</span></div><div class="stats-item"><span class="stats-item-val">3</span><span class="stats-item-lbl">Segnalazioni ricevute</span></div></div></div></div>"""

_CTA_GAP_CSS = """
    /* ── CTA Gap Box (Inchiesta incompleta) ───────────────────────────────── */
    .cta-gap-box{margin:2.5rem 0 2rem;border:1px solid rgba(230,57,70,.35);background:rgba(230,57,70,.04);padding:1.75rem;}
    .cta-gap-box .cta-label{font-family:var(--font-mono,monospace);font-size:.6rem;letter-spacing:.15em;text-transform:uppercase;color:rgba(230,57,70,.7);margin-bottom:.9rem;display:flex;align-items:center;gap:.5rem;}
    .cta-gap-box .cta-label::before{content:'';display:inline-block;width:6px;height:6px;border-radius:50%;background:#e63946;box-shadow:0 0 6px rgba(230,57,70,.6);animation:blink-dot 2s ease-in-out infinite;}
    @keyframes blink-dot{0%,100%{opacity:.4}50%{opacity:1}}
    .cta-gap-box h3{font-family:var(--font-serif,Georgia,serif);font-size:1.2rem;font-weight:700;color:#f5f5f0;margin:0 0 1.25rem;line-height:1.3;}
    .cta-gap-box .gap-list{list-style:none;padding:0;margin:0 0 1.25rem;display:flex;flex-direction:column;gap:.85rem;}
    .cta-gap-box .gap-list li{font-size:.9rem;line-height:1.6;color:rgba(255,255,255,.8);padding-left:1.2rem;position:relative;}
    .cta-gap-box .gap-list li::before{content:'→';position:absolute;left:0;color:rgba(230,57,70,.7);}
    .cta-gap-box .gap-list li strong{color:#f5f5f0;}
    .cta-gap-box .cta-actions{display:flex;align-items:center;gap:1rem;flex-wrap:wrap;border-top:1px solid rgba(255,255,255,.07);padding-top:1.1rem;margin-top:.25rem;}
    .cta-gap-box .cta-btn{display:inline-block;background:#e63946;color:#fff;padding:.6rem 1.4rem;font-family:var(--font-mono,monospace);font-size:.75rem;font-weight:700;letter-spacing:.08em;text-transform:uppercase;text-decoration:none;transition:opacity .2s;}
    .cta-gap-box .cta-btn:hover{opacity:.85;}
    .cta-gap-box .cta-anon{font-family:var(--font-mono,monospace);font-size:.65rem;color:rgba(255,255,255,.3);margin-top:.6rem;}"""


_DOC_GAPS_DIR = Path("/home/pinky/.pinkybot/data/agents/aiena")


def build_cta_gap_html(slug: str) -> str:
    """Read doc_gaps_{slug}.json and return CTA box HTML, or empty string if no gaps."""
    gap_file = _DOC_GAPS_DIR / f"doc_gaps_{slug}.json"
    if not gap_file.exists():
        return ""
    try:
        data = json.loads(gap_file.read_text(encoding="utf-8"))
        gaps = data.get("gaps", [])
        if not gaps:
            return ""
        items_html = "\n".join(
            f'          <li><strong>{g["label"]}</strong>'
            + (f' — {g["detail"]}' if g.get("detail") else "")
            + "</li>"
            for g in gaps
        )
        return f"""
      <!-- CTA GAP BOX — generato da doc_gaps_{slug}.json -->
      <div class="cta-gap-box">
        <div class="cta-label">📨 Inchiesta incompleta — cerco documenti</div>
        <h3>Questa inchiesta è incompleta. Hai accesso a questi documenti?</h3>
        <ul class="gap-list">
{items_html}
        </ul>
        <div class="cta-actions">
          <a href="https://aiena.it/segnala.html" class="cta-btn" target="_blank" rel="noopener">→ Segnala in modo anonimo</a>
        </div>
        <p class="cta-anon">Nessuna email richiesta. <strong style="color:rgba(255,255,255,.5);">Anonimato garantito.</strong> Accesso anche via Tor/onion.</p>
      </div>"""
    except Exception as e:
        log(f"WARN: build_cta_gap_html failed for {slug}: {e}")
        return ""


_ERRORE_BOX_HTML = """      <!-- ERRORE BOX -->
      <div id="errore-box" style="display:none;margin:1rem 0;border:1px solid rgba(255,255,255,.08);border-left:3px solid rgba(255,58,46,.5);padding:1.25rem 1.5rem;background:rgba(255,58,46,.03);">
        <div style="font-family:var(--font-mono);font-size:0.6rem;letter-spacing:.1em;text-transform:uppercase;color:rgba(255,58,46,.6);margin-bottom:.75rem;">// Segnala un errore</div>
        <div style="display:flex;flex-direction:column;gap:.75rem;">
          <div style="display:flex;flex-direction:column;gap:.3rem;">
            <label style="font-family:var(--font-mono);font-size:.6rem;text-transform:uppercase;letter-spacing:.1em;color:rgba(255,255,255,.3);">Cosa c'è di sbagliato?</label>
            <textarea id="errore-testo" rows="3" maxlength="500" placeholder="Descrivi l'errore: testo errato, dato non corretto, link rotto…" style="background:rgba(0,0,0,.4);color:rgba(255,255,255,.9);border:1px solid rgba(255,255,255,.1);padding:.65rem .8rem;font-family:var(--font-sans);font-size:.875rem;resize:vertical;border-radius:0;appearance:none;"></textarea>
          </div>
          <div style="display:flex;gap:.75rem;align-items:center;flex-wrap:wrap;">
            <button onclick="inviaErrore()" style="background:rgba(255,58,46,.8);color:#fff;font-weight:700;border:none;padding:.6rem 1.2rem;cursor:pointer;font-family:var(--font-mono);font-size:.75rem;text-transform:uppercase;letter-spacing:.06em;border-radius:0;">→ Invia</button>
            <button onclick="document.getElementById('errore-box').style.display='none'" style="background:none;border:none;color:rgba(255,255,255,.3);cursor:pointer;font-family:var(--font-mono);font-size:.7rem;">✕ annulla</button>
          </div>
          <div id="errore-msg" style="font-family:var(--font-mono);font-size:.75rem;display:none;"></div>
        </div>
      </div>
      <div style="margin:.5rem 0 1.5rem;text-align:right;">
        <button onclick="document.getElementById('errore-box').style.display=document.getElementById('errore-box').style.display==='none'?'block':'none'" style="background:none;border:none;color:rgba(255,255,255,.25);cursor:pointer;font-family:var(--font-mono);font-size:.65rem;letter-spacing:.05em;text-decoration:underline;text-underline-offset:3px;">⚠ Segnala un errore in questo articolo</button>
      </div>"""

_PRODUCTION_SCRIPTS_TPL = """  <script>
    const hamburger = document.getElementById('hamburger');
    const nav = document.getElementById('main-nav');
    if (hamburger && nav) { hamburger.addEventListener('click', () => { const open = nav.classList.toggle('open'); hamburger.setAttribute('aria-expanded', open); }); }
    (function() { const hashEl=document.getElementById('bc-hashval'); if(!hashEl)return; const hash=hashEl.textContent.trim(); if(hash==='pending'||hash.length!==64)return; const vis=document.getElementById('bc-hashvis'); if(!vis)return; for(let i=0;i<64;i+=2){const byte=parseInt(hash.slice(i,i+2),16);const hue=(byte*360/255)|0;const sat=50+(byte%35);const lit=30+(byte%30);const el=document.createElement('div');el.className='bc-hb';el.style.background='hsl('+hue+','+sat+'%,'+lit+'%)';el.title='0x'+hash.slice(i,i+2);vis.appendChild(el);} })();
    async function verifyIntegrity() { const btn=document.getElementById('bc-verify-btn'),res=document.getElementById('bc-verify-result'),storedEl=document.getElementById('bc-hashval'),stored=storedEl?storedEl.textContent.trim():''; if(!stored||stored==='pending'){res.style.display='block';res.className='bc-verify-result bc-verify-warn';res.innerHTML='⏳ &nbsp;Certificazione blockchain in corso — riprova tra qualche minuto.';return;} btn.disabled=true;btn.textContent='⟳ calcolo...';res.style.display='none'; try{const resp=await fetch(window.location.href,{cache:'no-store'});if(!resp.ok)throw new Error('Fetch fallito: HTTP '+resp.status);const raw=await resp.text();const artMatch=raw.match(/<article[\\s\\S]*?<\\/article>/);let content=artMatch?artMatch[0]:raw;content=content.replace(/<!-- BLOCKCHAIN INTEGRITY[\\s\\S]*?<!-- \\/BC-BOX -->/,'');content=content.replace(/<[^>]+>/g,' ').replace(/\\s+/g,' ').trim();const buf=await crypto.subtle.digest('SHA-256',new TextEncoder().encode(content));const computed=Array.from(new Uint8Array(buf)).map(b=>b.toString(16).padStart(2,'0')).join('');res.style.display='block';if(computed===stored){res.className='bc-verify-result bc-verify-ok';res.innerHTML='✓ &nbsp;Integrità verificata — il testo corrisponde al certificato blockchain.';btn.textContent='✓ verificato';}else{res.className='bc-verify-result bc-verify-warn';res.innerHTML='≠ &nbsp;Hash non corrisponde — il testo potrebbe essere stato modificato.';btn.textContent='↺ riprova';}}catch(err){res.style.display='block';res.className='bc-verify-result bc-verify-err';res.innerHTML='✕ &nbsp;Errore: '+err.message;btn.textContent='⚡ verifica ora';} btn.disabled=false; }
    const SUPABASE_URL='https://fwyjxolljcogblvwvfca.supabase.co',SUPABASE_KEY='eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImZ3eWp4b2xsamNvZ2Jsdnd2ZmNhIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NDQ1ODg1MDYsImV4cCI6MjA2MDE2NDUwNn0.xJNjMRJWGqM5h9wL2hUkPrN8S47ltq5-N8NLFgFoXhI',ARTICLE_SLUG='__SLUG__';
    function genTicketCode(){const n=new Date();return 'AIE-'+String(n.getMonth()+1).padStart(2,'0')+String(n.getDate()).padStart(2,'0')+'-'+(Math.floor(Math.random()*9000)+1000);}
    async function inviaErrore(){const testo=document.getElementById('errore-testo').value.trim(),msg=document.getElementById('errore-msg');if(!testo)return;msg.style.display='none';try{const res=await fetch(SUPABASE_URL+'/rest/v1/tickets',{method:'POST',headers:{'apikey':SUPABASE_KEY,'Authorization':'Bearer '+SUPABASE_KEY,'Content-Type':'application/json','Prefer':'return=minimal'},body:JSON.stringify({ticket_code:genTicketCode(),tipo:'correzione',titolo:'Errore segnalato: '+ARTICLE_SLUG,descrizione:testo,status:'aperto'})});if(!res.ok)throw new Error('HTTP '+res.status);msg.style.cssText='display:block;color:#22c55e;font-family:var(--font-mono);font-size:.75rem;';msg.textContent='✓ Segnalazione ricevuta. Grazie.';document.getElementById('errore-testo').value='';setTimeout(()=>{document.getElementById('errore-box').style.display='none';msg.style.display='none';},3000);}catch(e){msg.style.cssText='display:block;color:#ff3a2e;font-family:var(--font-mono);font-size:.75rem;';msg.textContent='Errore invio. Riprova.';}}
    (function(){var trigger=document.getElementById('statsTrigger'),overlay=document.getElementById('statsOverlay'),closeBtn=document.getElementById('statsClose');if(!trigger)return;var loaded=false;function loadStats(){if(loaded)return;fetch('/stats.json?v='+Date.now()).then(function(r){return r.json();}).then(function(s){loaded=true;var vals=overlay.querySelectorAll('.stats-item-val');if(vals[0])vals[0].textContent=s.fonti_analizzate||'—';if(vals[1])vals[1].textContent=(s.ore_ricerca||'—')+'+';if(vals[2])vals[2].textContent=s.connessioni_trovate||'—';if(vals[3])vals[3].textContent=s.articoli_pubblicati||'—';if(vals[4])vals[4].textContent=s.inchieste_in_corso||'—';if(vals[5])vals[5].textContent=s.segnalazioni_ricevute||'—';}).catch(function(){});}
    trigger.addEventListener('click',function(){overlay.classList.add('open');loadStats();});closeBtn.addEventListener('click',function(){overlay.classList.remove('open');});overlay.addEventListener('click',function(e){if(e.target===overlay)overlay.classList.remove('open');});document.addEventListener('keydown',function(e){if(e.key==='Escape')overlay.classList.remove('open');});})();
    document.addEventListener('DOMContentLoaded',function(){var bcHeader=document.querySelector('.bc-header');if(bcHeader){bcHeader.addEventListener('click',function(){document.getElementById('bc-box').classList.toggle('bc-open');});}});
  </script>"""


# Import template builder for the new approach
try:
    from aiena_template_builder import (
        transform_preview_to_production_v2,
        TEMPLATE_PATH
    )
    _TEMPLATE_BUILDER_AVAILABLE = TEMPLATE_PATH.exists()
except ImportError:
    _TEMPLATE_BUILDER_AVAILABLE = False


def transform_preview_to_production(preview_path: Path, articles_path: Path, slug: str) -> bool:
    """Transform preview HTML for production.

    NEW APPROACH (v2 - template-based):
    If template is available, uses template-based approach that:
    1. Extracts only textual content from preview
    2. Injects it into a canonical fixed template
    -> Output is always structurally identical

    FALLBACK (v1 - patch-based):
    If template not available, patches the preview with regex.

    NOTE: bc-box HTML is handled by ots_stamp after publication.
    """
    # Try template-based approach first (v2)
    if _TEMPLATE_BUILDER_AVAILABLE:
        log(f"Using template-based approach for {slug}")
        try:
            success = transform_preview_to_production_v2(
                preview_path, articles_path, slug, pub_date=date.today()
            )
            if success:
                log(f"Transformed (template v2): {articles_path}")
                return True
            else:
                log_warn(f"Template v2 failed for {slug}, falling back to patch method")
        except Exception as e:
            log_warn(f"Template v2 error for {slug}: {e}, falling back to patch method")

    # Fallback: old patch-based approach (v1)
    return _transform_preview_to_production_v1(preview_path, articles_path, slug)


def _transform_preview_to_production_v1(preview_path: Path, articles_path: Path, slug: str) -> bool:
    """Old patch-based approach (v1) - kept as fallback.

    Transform preview HTML for production: fix CSS, inject bc-box/stats CSS,
    replace minimal footer, inject scripts. bc-box HTML is handled by ots_stamp later."""
    try:
        html = preview_path.read_text(encoding="utf-8")

        # 0. Rimuovi metadati brief Segugio (non devono apparire nel corpo dell'articolo)
        _brief_meta_patterns = [
            r'<p><strong>Categoria:</strong>[^<]*<br\s*/>\s*\n?\s*<strong>Data pubblicazione prevista:</strong>[^<]*<br\s*/>\s*\n?\s*<strong>Autore:</strong>[^<]*</p>\s*\n?\s*<hr\s*/>\s*',
            r'<p><strong>Parole:</strong>[^<]*<br\s*/>\s*\n?\s*<strong>Caratteri[^<]*</strong>[^<]*<br\s*/>\s*\n?\s*<strong>Tempo lettura stimato:</strong>[^<]*</p>\s*',
            r'<p><strong>Note redazionali:</strong><br\s*/>[^<]*(?:<br\s*/>[^<]*)*(?:\n[^<]*)*</p>\s*',
        ]
        for pat in _brief_meta_patterns:
            html = re.sub(pat, '', html, flags=re.DOTALL)

        # 1. Rimuovi noindex
        html = re.sub(
            r'<meta\s+name=["\']robots["\']\s+content=["\']noindex,?\s*nofollow?["\'][^>]*>\s*',
            '<meta name="robots" content="index, follow">\n  ', html, flags=re.IGNORECASE
        )
        # 2. Rimuovi diary-preview-banner
        html = re.sub(
            r'\s*<div\s+class=["\']diary-preview-banner["\'][^>]*>.*?(?=\s*<!--\s*ARTICLE\s)',
            '', html, flags=re.DOTALL | re.IGNORECASE
        )
        # 3. Canonical
        canonical_url = f"https://aiena.it/articles/{slug}.html"
        if 'rel="canonical"' not in html and "rel='canonical'" not in html:
            html = re.sub(r'(</head>)', f'  <link rel="canonical" href="{canonical_url}">\n\\1', html)
        # 4. og:url
        html = re.sub(
            r'(<meta\s+property=["\']og:url["\']\s+content=["\'])[^"\']+(["\'][^>]*>)',
            f'\\g<1>{canonical_url}\\g<2>', html, flags=re.IGNORECASE
        )
        # 5. datePublished
        pub_date = date.today().isoformat()
        html = html.replace('"[DATA PUBBLICAZIONE]"', f'"{pub_date}"')
        html = re.sub(
            r'(<meta\s+property=["\']article:published_time["\']\s+content=["\'])\d{4}-\d{2}-\d{2}',
            f'\\g<1>{pub_date}', html, flags=re.IGNORECASE
        )
        html = re.sub(r'("datePublished"\s*:\s*")[^"]*"', f'\\g<1>{pub_date}"', html)
        # 6. Fix CSS version
        html = re.sub(r'(styles\.css\?v=)[^\s"\']+', f'\\g<1>{_PRODUCTION_CSS_VERSION}', html)
        # 7. Inietta bc-box CSS se mancante
        if '.bc-box' not in html:
            html = html.replace('</style>', _BC_BOX_CSS + '\n  </style>', 1)
            log(f"bc-box CSS injected: {slug}")
        # 8. Inietta stats CSS se mancante
        if '.stats-trigger' not in html:
            html = html.replace('</style>', _STATS_CSS + '\n  </style>', 1)
            log(f"stats CSS injected: {slug}")
        # 9. Inietta CTA gap box da doc_gaps_{slug}.json se non già presente
        if 'cta-gap-box' not in html:
            cta_html = build_cta_gap_html(slug)
            if cta_html:
                html = html.replace(
                    '    </div>\n  </article>',
                    cta_html + '\n\n    </div>\n  </article>', 1
                )
                # CSS ora in styles.css globale — no iniezione inline necessaria
                log(f"cta-gap-box injected from JSON: {slug}")
        # 10. Inietta errore-box HTML se mancante
        if 'errore-box' not in html:
            html = html.replace(
                '    </div>\n  </article>',
                _ERRORE_BOX_HTML + '\n\n    </div>\n  </article>', 1
            )
            log(f"errore-box injected: {slug}")
        # 10. Sostituisci footer minimale con footer completo
        if 'footer-inner' not in html:
            html = re.sub(
                r'(?:<!--\s*FOOTER\s*-->\s*)?<footer\s+class=["\']site-footer["\'][^>]*>.*?</footer>',
                _FULL_FOOTER_HTML, html, flags=re.DOTALL, count=1
            )
            log(f"Full footer injected: {slug}")
        # 11. Sostituisci/inietta script produzione completi
        needs_scripts = 'verifyIntegrity' not in html or 'inviaErrore' not in html or 'statsTrigger' not in html
        if needs_scripts:
            production_scripts = _PRODUCTION_SCRIPTS_TPL.replace('__SLUG__', slug)
            html = re.sub(
                r'<script>[^<]*(?:hamburger|Hamburger)[^<]*(?:(?!</script>).)*</script>',
                '', html, flags=re.DOTALL
            )
            html = html.replace('</body>', production_scripts + '\n</body>', 1)
            log(f"Production scripts injected: {slug}")
        else:
            html = re.sub(r"const ARTICLE_SLUG\s*=\s*'[^']*'", f"const ARTICLE_SLUG='{slug}'", html)

        _atomic_write(articles_path, html)
        log(f"Transformed (patch v1): {articles_path}")
        return True
    except Exception as e:
        log_error(f"Failed to transform preview (v1): {e}")
        return False


def update_hero_section(slug: str, title: str, excerpt: str, category: str, read_time: int) -> bool:
    """Update hero section in index.html."""
    try:
        html = INDEX_HTML.read_text(encoding="utf-8")
        today = date.today()
        date_str = date_it_long(today)

        # Update hero title
        html = re.sub(
            r'(<h1\s+class=["\']hero-title["\'][^>]*>)[^<]*(</h1>)',
            f'\\g<1>{title}\\g<2>',
            html
        )

        # Update hero excerpt
        html = re.sub(
            r'(<p\s+class=["\']hero-excerpt["\'][^>]*>)[^<]*(</p>)',
            f'\\g<1>{excerpt}\\g<2>',
            html
        )

        # Update hero meta (date and read time)
        # Pattern: <div class="hero-meta">...<span>AIena</span><span>DATE</span><span>N min lettura</span>...</div>
        html = re.sub(
            r'(<div\s+class=["\']hero-meta["\'][^>]*>)\s*<span>AIena</span>\s*<span>[^<]*</span>\s*<span>[^<]*</span>',
            f'\\g<1>\n        <span>AIena</span>\n        <span>{date_str}</span>\n        <span>{read_time} min lettura</span>',
            html
        )

        # Update hero button link
        html = re.sub(
            r'(<a\s+href=["\'])articles/[^"\']+(["\'][^>]*class=["\'][^"\']*btn[^"\']*btn-primary[^"\']*["\'][^>]*>)',
            f'\\g<1>articles/{slug}.html\\g<2>',
            html
        )

        # Update hero badge category
        # Find: <span class="badge badge-cat">CATEGORY</span> within hero-badge
        html = re.sub(
            r'(<div\s+class=["\']hero-badge["\'][^>]*>.*?<span\s+class=["\']badge\s+badge-cat["\'][^>]*>)[^<]*(</span>)',
            f'\\g<1>{category}\\g<2>',
            html,
            flags=re.DOTALL
        )

        _atomic_write(INDEX_HTML, html)
        log(f"Updated hero section: {title}")
        return True
    except Exception as e:
        log_error(f"Failed to update hero: {e}")
        return False


def update_indagini(slug: str, title: str, pipeline_data: dict) -> bool:
    """
    Update indagini.html:
    1. Remove the card for the published article
    2. Add a card for the next investigation
    3. Update diary section
    """
    try:
        html = INDAGINI_HTML.read_text(encoding="utf-8")

        # 1. Remove the card matching the published article title
        # Find card containing the title and remove entire <div class="article-card"...>...</div>
        # Use non-greedy match to find the specific card
        pattern = rf'<div\s+class=["\']article-card["\'][^>]*>.*?<h3\s+class=["\']card-title["\'][^>]*>{re.escape(title)}</h3>.*?</div>\s*</div>\s*</div>'
        html = re.sub(pattern, '', html, flags=re.DOTALL)

        # Alternative: try matching by looking for card structure
        # Pattern for a complete card block
        card_pattern = rf'<div\s+class=["\']article-card["\'][^>]*>\s*<div\s+class=["\']card-badges["\'][^>]*>.*?</div>\s*<h3\s+class=["\']card-title["\'][^>]*>{re.escape(title)}</h3>.*?</div>\s*</div>'
        html = re.sub(card_pattern, '', html, flags=re.DOTALL)

        # 2. Get next investigation from pipeline v2 (BEFORE post_publish runs)
        # In v2: investigations[1] is the "next" after current investigations[0]
        investigations = pipeline_data.get("investigations", [])
        next_inv = investigations[1] if len(investigations) > 1 else {}
        if next_inv:
            next_title = next_inv.get("title", "")
            next_cat = next_inv.get("category", "Indagine")
            # Use diary_text first, fallback to summary/description
            next_diary = (
                next_inv.get("diary_text")
                or next_inv.get("summary")
                or next_inv.get("description")
                or ""
            )
            # Strip HTML for excerpt
            next_excerpt = re.sub(r'<[^>]+>', '', next_diary)[:150]
            if len(next_diary) > 150:
                next_excerpt += "..."

            # Create new card HTML
            new_card = f'''
          <div class="article-card" style="opacity: 0.7; cursor: default;">
            <div class="card-badges">
              <span class="badge badge-ongoing">In Ricerca</span>
              <span class="badge badge-cat">{next_cat}</span>
            </div>
            <h3 class="card-title">{next_title}</h3>
            <p class="card-excerpt">{next_excerpt}</p>
            <div class="card-meta"><span>In ricerca</span></div>
          </div>'''

            # Find articles-grid and append the new card before closing </div>
            # Pattern: <div class="articles-grid">...</div>
            grid_pattern = r'(<div\s+class=["\']articles-grid["\'][^>]*>)(.*?)(</div>\s*</div>\s*</section>)'

            def add_card(m):
                return m.group(1) + m.group(2) + new_card + '\n        ' + m.group(3)

            html = re.sub(grid_pattern, add_card, html, count=1, flags=re.DOTALL)

        # 3. Update diary section with current investigation (investigations[0])
        current = investigations[0] if investigations else {}
        if current:
            _public_diary = [e for e in current.get("diary", []) if e.get("public", True)]
            _raw_diary = current.get("diary_text") or (_public_diary[0].get("text") if _public_diary else "In lavorazione.")
            diary_text = re.sub(r"\bMirko\b", "la redazione", re.sub(r"\bSatoshi\b", "il team tecnico", _raw_diary))
            category = current.get("category", "Indagine")
            today_str = date.today().strftime("%d %b %Y").upper()

            # Update diary category
            html = re.sub(
                r'(<span class="diary-cat">)[^<]*(</span>)',
                f'\\g<1>{category}\\g<2>',
                html
            )

            # Update diary slides - replace all slides with one
            new_slide = f'''<div class="diary-slide active" id="ds-0">
              <div class="diary-date">{today_str} -- aggiornamento</div>
              <div class="diary-text">{diary_text}</div>
            </div>'''

            pattern = r'(<div class="diary-body">)\s*(.*?)\s*(</div>\s*<div class="diary-nav">)'

            def replace_diary(m):
                return m.group(1) + '\n            ' + new_slide + '\n          ' + m.group(3)

            html = re.sub(pattern, replace_diary, html, count=1, flags=re.DOTALL)

            # Update dots to single dot
            html = re.sub(
                r'(<div class="diary-dots">)\s*.*?\s*(</div>\s*<a href="/segnala\.html")',
                r'\g<1>\n              <div class="diary-dot active" data-i="0"></div>\n            \g<2>',
                html,
                flags=re.DOTALL
            )

            # Update JS total
            html = re.sub(r'const total = \d+;', 'const total = 1;', html)

        _atomic_write(INDAGINI_HTML, html)
        log(f"Updated indagini.html")
        return True
    except Exception as e:
        log_error(f"Failed to update indagini.html: {e}")
        return False


def update_archivio(slug: str, title: str, excerpt: str, category: str) -> bool:
    """Add new article card to top of archivio.html."""
    try:
        html = ARCHIVIO_HTML.read_text(encoding="utf-8")
        today = date.today()
        date_str = date_it(today)

        # Create new card HTML
        new_card = f'''
          <!-- Articolo: {slug} -->
          <a href="/articles/{slug}.html" class="article-card" data-category="{category}" data-title="{title}" data-excerpt="{excerpt}">
            <div class="card-badges">
              <span class="badge badge-new">Published</span>
              <span class="badge badge-cat">{category}</span>
            </div>
            <h3 class="card-title">{title}</h3>
            <p class="card-excerpt">{excerpt[:150]}{'...' if len(excerpt) > 150 else ''}</p>
            <div class="card-meta"><span>{date_str}</span></div>
          </a>'''

        # Insert after <div class="articles-grid" id="articles-grid">
        pattern = r'(<div\s+class=["\']articles-grid["\']\s+id=["\']articles-grid["\'][^>]*>)'
        html = re.sub(pattern, f'\\g<1>{new_card}', html, count=1)

        _atomic_write(ARCHIVIO_HTML, html)
        log(f"Updated archivio.html with new article")
        return True
    except Exception as e:
        log_error(f"Failed to update archivio.html: {e}")
        return False


def update_feed(slug: str, title: str, excerpt: str, category: str) -> bool:
    """Prepend new article as first <item> in feed.xml (RSS)."""
    try:
        from email.utils import format_datetime
        xml = FEED_XML.read_text(encoding="utf-8")
        pub_date = format_datetime(datetime.now(timezone.utc))
        def xe(s): return s.replace("&","&amp;").replace("<","&lt;").replace(">","&gt;").replace('"',"&quot;")
        url = f"https://aiena.it/articles/{slug}.html"
        new_item = f"""        <item>
      <title>{xe(title)}</title>
      <link>{url}</link>
      <description>{xe(excerpt[:200])}</description>
      <pubDate>{pub_date}</pubDate>
      <category>{xe(category)}</category>
      <author>aiena@agentmail.to (AIena)</author>
      <guid isPermaLink="true">{url}</guid>
    </item>

    """
        marker = 'type="application/rss+xml"/>'
        if marker in xml:
            idx = xml.index(marker) + len(marker)
            xml = xml[:idx] + "\n        " + new_item + xml[idx:]
        else:
            xml = xml.replace("<item>", new_item + "<item>", 1)
        _atomic_write(FEED_XML, xml)
        log("feed.xml aggiornato")
        return True
    except Exception as e:
        log_error(f"update_feed: {e}")
        return False


# ============================================================================
# Sitemap auto-update
# ============================================================================

def update_sitemap(slug: str, dry_run: bool = False) -> bool:
    """
    Ensure the published article URL is present in sitemap.xml.
    If missing, adds it with lastmod=today, priority=0.85, changefreq=monthly.
    Returns True if sitemap was updated (or already correct), False on error.
    """
    article_url = f"https://www.aiena.it/articles/{slug}.html"
    try:
        sitemap_content = SITEMAP_XML.read_text(encoding="utf-8")
    except Exception as e:
        log_error(f"sitemap read error: {e}")
        return False

    if article_url in sitemap_content:
        log(f"sitemap: {slug} già presente")
        return True

    today_str = date.today().isoformat()
    new_entry = (
        f"  <url>\n"
        f"    <loc>{article_url}</loc>\n"
        f"    <lastmod>{today_str}</lastmod>\n"
        f"    <priority>0.85</priority>\n"
        f"    <changefreq>monthly</changefreq>\n"
        f"  </url>\n"
    )
    updated = sitemap_content.replace("</urlset>", f"{new_entry}</urlset>")

    if dry_run:
        log(f"DRY-RUN: Would add {slug} to sitemap.xml")
        return True

    try:
        _atomic_write(SITEMAP_XML, updated)
        log(f"sitemap: aggiunto {slug}")
        return True
    except Exception as e:
        log_error(f"sitemap write error: {e}")
        return False


# ============================================================================
# TASK B: HTML validation (internal notes check before FTP)
# ============================================================================

def verify_live_html_clean(url: str, slug: str) -> tuple[bool, str]:
    """
    TASK C: Fetch the published article from the live URL and verify
    that internal metadata did NOT leak into the published HTML.

    Returns (is_clean, error_msg).
    If internal notes found: (False, error message with details)
    If clean: (True, "")
    """
    try:
        response = urllib.request.urlopen(url, timeout=15)
        html_content = response.read().decode('utf-8')
    except Exception as e:
        # Network error - return (True, "") to not block on network issues
        log_warn(f"Could not fetch live HTML for verification: {e}")
        return True, ""

    # Use same pattern as Task B validation
    pattern = r'(Categoria:|Slug:|Data\s+draft:|Pub_date:|Nota\s*GUP|Versione\s*\d)'

    # 1. Check meta tags
    meta_patterns = [
        (r'<meta\s+name=["\']description["\']\s+content=["\']([^"\']*)["\']', 'description'),
        (r'<meta\s+property=["\']og:description["\']\s+content=["\']([^"\']*)["\']', 'og:description'),
        (r'<meta\s+property=["\']twitter:description["\']\s+content=["\']([^"\']*)["\']', 'twitter:description'),
    ]

    for meta_regex, meta_name in meta_patterns:
        m = re.search(meta_regex, html_content, re.IGNORECASE)
        if m:
            meta_content = m.group(1)
            if re.search(pattern, meta_content, re.IGNORECASE):
                found_match = re.search(pattern, meta_content, re.IGNORECASE).group(1)
                return False, f"LIVE: <meta {meta_name}> contains '{found_match}'"

    # 2. Check body content
    body_match = re.search(r'<article[^>]*>(.*?)(?:<hr|</article>)', html_content, re.IGNORECASE | re.DOTALL)
    if body_match:
        body_text = body_match.group(1)
        if re.search(pattern, body_text, re.IGNORECASE):
            match_obj = re.search(pattern, body_text, re.IGNORECASE)
            found_match = match_obj.group(1)
            return False, f"LIVE: Body contains '{found_match}'"

    return True, ""


def validate_html_for_internal_notes(html_content: str, slug: str) -> tuple[bool, str]:
    """
    Validate HTML content for internal metadata that should NOT be published.
    Checks:
      - Meta tags (description, og:description, twitter:description)
      - Body content (before first <hr>)

    Pattern to detect (IGNORECASE):
      Categoria:|Slug:|Data\s+draft:|Pub_date:|Nota\s*GUP|Versione\s*\d

    Returns (is_valid, error_msg).
    If pattern is found: (False, detailed error message)
    If no pattern: (True, "")
    """
    pattern = r'(Categoria:|Slug:|Data\s+draft:|Pub_date:|Nota\s*GUP|Versione\s*\d)'

    # 1. Check meta tags (description, og:description, twitter:description)
    meta_patterns = [
        (r'<meta\s+name=["\']description["\']\s+content=["\']([^"\']*)["\']', 'description'),
        (r'<meta\s+property=["\']og:description["\']\s+content=["\']([^"\']*)["\']', 'og:description'),
        (r'<meta\s+property=["\']twitter:description["\']\s+content=["\']([^"\']*)["\']', 'twitter:description'),
    ]

    for meta_regex, meta_name in meta_patterns:
        m = re.search(meta_regex, html_content, re.IGNORECASE)
        if m:
            meta_content = m.group(1)
            if re.search(pattern, meta_content, re.IGNORECASE):
                found_match = re.search(pattern, meta_content, re.IGNORECASE).group(1)
                return False, f"Internal metadata found in <meta {meta_name}>: '{found_match}' detected in '{meta_content[:80]}...'"

    # 2. Check body content (before first <hr> or end of article)
    body_match = re.search(r'<article[^>]*>(.*?)(?:<hr|</article>)', html_content, re.IGNORECASE | re.DOTALL)
    if body_match:
        body_text = body_match.group(1)
        if re.search(pattern, body_text, re.IGNORECASE):
            match_obj = re.search(pattern, body_text, re.IGNORECASE)
            found_match = match_obj.group(1)
            # Extract context around match (first 80 chars)
            match_pos = match_obj.start()
            context_start = max(0, match_pos - 40)
            context_end = min(len(body_text), match_pos + 100)
            context = body_text[context_start:context_end].replace('<', '').replace('>', '')[:100]
            return False, f"Internal metadata found in article body: '{found_match}' near '{context}...'"

    return True, ""


def notify_aiena_and_mirko(message_aiena: str, message_mirko: str = "", slug: str = "", dry_run: bool = False):
    """
    Send notification to AIena (internal) and optionally to Mirko (Telegram).
    """
    # Notify AIena
    try:
        payload = json.dumps({
            "from_agent": "satoshi",
            "message": message_aiena,
            "priority": 3,
        }).encode()
        req = urllib.request.Request(
            f"http://localhost:8888/agents/aiena/message",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        log_warn(f"Failed to notify AIena: {e}")

    # Notify Mirko if message provided
    if message_mirko and not dry_run:
        try:
            payload = json.dumps({
                "agent_name": "satoshi",
                "chat_id": MIRKO_CHAT_ID,
                "platform": "telegram",
                "content": message_mirko,
            }).encode()
            req = urllib.request.Request(
                BROKER_URL,
                data=payload,
                headers=_build_broker_headers("POST", "/broker/send"),
                method="POST",
            )
            urllib.request.urlopen(req, timeout=10)
        except Exception as e:
            log_warn(f"Failed to notify Mirko: {e}")


# ============================================================================
# FTP deployment
# ============================================================================

def deploy_ftp(local_path: Path, remote_path: str, dry_run: bool = False, slug: str = "") -> bool:
    """Deploy a file via FTP with ledger logging."""
    if dry_run:
        log(f"DRY-RUN: Would deploy {local_path.name} -> {remote_path}")
        return True

    cmd = [
        "curl", "-s", "-T", str(local_path),
        f"ftp://{FTP_HOST}/{remote_path}",
        "--user", f"{FTP_USER}:{FTP_PASS}"
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode == 0:
            log(f"FTP OK: {remote_path}")
            # Registra nel deploy ledger
            ledger_ftp_from_file(
                script="aiena_auto_publish",
                slug=slug,
                local_path=local_path,
                remote_path=remote_path,
                operation="ftp_upload",
                success=True
            )
            return True
        else:
            error_msg = result.stderr or "Unknown FTP error"
            log_error(f"FTP failed {remote_path}: {error_msg}")
            # Registra fallimento nel ledger
            ledger_ftp_from_file(
                script="aiena_auto_publish",
                slug=slug,
                local_path=local_path,
                remote_path=remote_path,
                operation="ftp_upload",
                success=False,
                error=error_msg
            )
            return False
    except subprocess.TimeoutExpired:
        log_error(f"FTP timeout: {remote_path}")
        ledger_ftp_from_file(
            script="aiena_auto_publish",
            slug=slug,
            local_path=local_path,
            remote_path=remote_path,
            operation="ftp_upload",
            success=False,
            error="Timeout after 60s"
        )
        return False
    except Exception as e:
        log_error(f"FTP error: {e}")
        ledger_ftp_from_file(
            script="aiena_auto_publish",
            slug=slug,
            local_path=local_path,
            remote_path=remote_path,
            operation="ftp_upload",
            success=False,
            error=str(e)
        )
        return False


# ============================================================================
# External script calls
# ============================================================================

def run_post_publish(slug: str, dry_run: bool = False) -> bool:
    """Run aiena_post_publish.py to update pipeline, diary, ticker."""
    cmd = [sys.executable, str(POST_PUBLISH_SCRIPT), "--slug", slug]
    if dry_run:
        cmd.append("--dry-run")

    log(f"Running: {' '.join(cmd)}")
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        print(result.stdout)
        if result.stderr:
            print(result.stderr, file=sys.stderr)
        if result.returncode != 0:
            log_error(f"post_publish failed with code {result.returncode}")
            return False
        return True
    except Exception as e:
        log_error(f"post_publish error: {e}")
        return False


def run_ots_stamp(slug: str, articles_path: Path, dry_run: bool = False) -> bool:
    """Run OTS stamp on the published article.

    NOTE: aiena_ots_stamp.py uses a hardcoded ARTICLES list and only accepts:
      --note "description"  (optional note for version history)
      --dry-run             (calculate hash without stamping)

    To stamp a new article, it must first be added to ARTICLES in aiena_ots_stamp.py.
    """
    if dry_run:
        log("DRY-RUN: Skipping OTS stamp")
        return True

    if not OTS_STAMP_SCRIPT.exists():
        log_warn("OTS stamp script not found, skipping")
        return True

    # aiena_ots_stamp.py accepts --note and --dry-run, NOT --slug/--file
    # It stamps all articles in its hardcoded ARTICLES list
    cmd = [sys.executable, str(OTS_STAMP_SCRIPT), "--note", f"Pubblicazione automatica: {slug}"]

    log(f"Running OTS stamp: {slug}")
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        print(result.stdout)
        if result.returncode == 0:
            log("OTS stamp completed")
            return True
        else:
            log_warn(f"OTS stamp warning: {result.stderr}")
            return True  # Non-critical, don't fail
    except Exception as e:
        log_warn(f"OTS stamp error: {e}")
        return True  # Non-critical


def run_update_admin_data(dry_run: bool = False) -> bool:
    """Regenerate admin data.json."""
    if dry_run:
        log("DRY-RUN: Skipping admin data regeneration")
        return True

    cmd = [sys.executable, str(UPDATE_ADMIN_SCRIPT)]

    log("Regenerating admin data.json...")
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode == 0:
            log("Admin data.json regenerated")
            return True
        else:
            log_warn(f"Admin data regeneration warning: {result.stderr}")
            return True
    except Exception as e:
        log_warn(f"Admin data error: {e}")
        return True


# ============================================================================
# Telegram notification
# ============================================================================

def notify_telegram(title: str, slug: str = None, dry_run: bool = False) -> bool:
    """Send Telegram notification to Mirko.

    If ``slug`` is provided, sends a structured "Articolo pubblicato" message
    with link. If ``slug`` is None, the first argument is treated as a
    pre-formatted plain message (used for alerts/warnings).
    """
    if dry_run:
        log("DRY-RUN: Skipping Telegram notification")
        return True

    if slug is None:
        # Bare-message mode: `title` is the full pre-formatted message
        message = title
    else:
        message = (
            f"*Articolo pubblicato*\n\n"
            f"{title}\n\n"
            f"https://aiena.it/articles/{slug}.html\n\n"
            f"Diario, ticker e indagini aggiornati in automatico."
        )

    payload = {
        "agent_name": "satoshi",
        "chat_id": MIRKO_CHAT_ID,
        "platform": "telegram",
        "content": message
    }

    try:
        req = urllib.request.Request(
            BROKER_URL,
            data=json.dumps(payload).encode(),
            headers=_build_broker_headers("POST", "/broker/send"),
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status == 200:
                log("Telegram notification sent")
                return True
            log_warn(f"Telegram notification status: {resp.status}")
            return True
    except Exception as e:
        log_warn(f"Telegram notification error: {e}")
        return True  # Non-critical


# ============================================================================
# Main publication flow
# ============================================================================

def main():
    parser = argparse.ArgumentParser(description="Autonomous publication for aiena.it")
    parser.add_argument("--dry-run", action="store_true", help="Skip FTP, Supabase writes, Telegram")
    parser.add_argument("--force", action="store_true", help="Ignore day-of-week check")
    args = parser.parse_args()

    dry_run = args.dry_run

    log("=" * 60)
    log("AIENA AUTO-PUBLISH STARTING")
    log("=" * 60)

    if dry_run:
        log("DRY-RUN MODE ENABLED")

    # W2: Check for stale error approvals — runs every cron invocation regardless of day
    check_and_recover_stale_errors(dry_run=dry_run)

    # 1. Check day of week
    today = date.today()
    weekday = today.weekday()

    if weekday not in PUBLICATION_DAYS and not args.force:
        log("Oggi non e' giorno di pubblicazione (mar/mer/gio).")
        log(f"Giorno corrente: {today.strftime('%A')} (weekday={weekday})")
        return 0

    if args.force and weekday not in PUBLICATION_DAYS:
        log_warn(f"FORCE mode: publishing on {today.strftime('%A')}")

    # 2. Controlla se è già stato pubblicato qualcosa questa settimana
    # NOTA: usiamo SOLO il lock file (non Supabase) perché i fuori programma
    # vengono pubblicati da aiena_urgente_processor.py e impostano status=published
    # in Supabase — ma non devono bloccare la pubblicazione programmata.
    # Il lock file è scritto SOLO da questo script → fonte di verità affidabile.

    # Lock file: prevent double-publish even if Supabase check fails
    # Formato lock file: YYYY-MM-DD (data esplicita del martedì di pubblicazione)
    lock_path = Path("/home/pinky/.pinkybot/data/logs/last_publish_week.txt")

    # Calcola il martedì di questa settimana (il giorno target di pubblicazione)
    days_since_tuesday = (today.weekday() - 1) % 7  # martedì = weekday 1
    this_week_tuesday = today - timedelta(days=days_since_tuesday)

    # Read lock file BEFORE any modifications (for audit log)
    lock_before = ""
    if lock_path.exists():
        lock_before = lock_path.read_text().strip()
    else:
        # SAFEGUARD: If lock file doesn't exist, create it with a sentinel value
        # that is BEFORE this week's Tuesday, so the check below works correctly
        log("Lock file does not exist. Creating sentinel with last week's date.")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        last_week_tuesday = this_week_tuesday - timedelta(weeks=1)
        lock_path.write_text(last_week_tuesday.isoformat())
        lock_before = f"[CREATED: {last_week_tuesday.isoformat()}]"
        log(f"Lock file initialized: {last_week_tuesday.isoformat()}")

    # Now check if we already published this week
    locked_date_str = lock_path.read_text().strip()
    try:
        # Supporta sia vecchio formato YYYY-WW che nuovo YYYY-MM-DD
        if "-" in locked_date_str and len(locked_date_str) == 10:
            # Nuovo formato: YYYY-MM-DD
            locked_date = date.fromisoformat(locked_date_str)
            # Se l'ultima pubblicazione è avvenuta nel martedì di questa settimana, skip
            if locked_date == this_week_tuesday:
                log(f"Lock file: già pubblicato il {locked_date_str} (martedì di questa settimana). Skip.")
                write_audit_log("skipped", "", f"Already published this week ({locked_date_str})", lock_before, locked_date_str)
                return 0
        else:
            # Vecchio formato YYYY-WW: confronta con settimana corrente
            current_week = datetime.now().strftime("%Y-%W")
            if locked_date_str == current_week:
                log(f"Lock file (legacy): già pubblicato nella settimana {current_week}. Skip.")
                write_audit_log("skipped", "", f"Already published this week (legacy: {current_week})", lock_before, locked_date_str)
                return 0
    except Exception as e:
        log_warn(f"Lock file parse error: {e}")

    # 3. Fetch pending approvals in home order (pipeline position)
    log("Fetching pending approvals from Supabase...")
    approvals = fetch_pending_approvals()

    if not approvals:
        log("Nessun articolo approvato in coda.")
        return 0

    # Take the first (oldest) approval
    approval = approvals[0]
    slug = approval.get("slug", "")
    title = approval.get("title", "")
    approval_id = approval.get("id", "")

    log(f"Processing: {title} ({slug})")
    log(f"Approval ID: {approval_id}")
    log(f"Approved at: {approval.get('approved_at', 'unknown')}")

    # =========================================================================
    # PRIMARY GUARD: Check publish_date from Supabase approval record
    # This is the AUTHORITATIVE source - not admin/data.json which is derived
    # =========================================================================
    can_publish, date_reason = check_publish_date_guard(approval, args.force)
    def _safe_read_lock() -> str:
        """Legge lock file senza crash se corrotto o mancante."""
        try:
            return lock_path.read_text().strip() if lock_path.exists() else ""
        except Exception:
            return ""

    if not can_publish:
        log_error(f"PUBLICATION BLOCKED: {date_reason}")
        write_audit_log(
            "aborted",
            slug,
            date_reason,
            lock_before,
            _safe_read_lock(),
        )
        # Do NOT mark as error - the article is fine, just not ready yet
        return 0
    elif date_reason:
        # Warning or force message
        log_warn(date_reason)

    # SECONDARY CHECK: Verify against admin/data.json publication_queue
    # This is a belt-and-suspenders check - the primary guard above should catch this
    data_json_path = Path("/var/www/aiena.it/admin/data.json")
    if data_json_path.exists():
        try:
            admin_data = json.loads(data_json_path.read_text())
            pub_queue = admin_data.get("publication_queue", [])
            for q_item in pub_queue:
                if q_item.get("slug") == slug and q_item.get("pub_date"):
                    pub_date = date.fromisoformat(q_item["pub_date"])
                    if pub_date > date.today() and not args.force:
                        log_error(f"SECONDARY CHECK FAILED: {slug} scheduled for {pub_date} in admin queue")
                        write_audit_log(
                            "aborted",
                            slug,
                            f"Secondary check: scheduled for {pub_date} in publication_queue",
                            lock_before,
                            _safe_read_lock(),
                        )
                        return 0
        except Exception as e:
            log_warn(f"Could not check admin queue dates: {e}")

    # 3. Check preview file exists
    preview_path = PREVIEW_DIR / f"{slug}.html"
    articles_path = ARTICLES_DIR / f"{slug}.html"

    if not preview_path.exists():
        log_error(f"Preview file not found: {preview_path}")
        mark_approval_error(approval_id, dry_run)
        return 1

    # 4. Load pipeline.json BEFORE any modifications (for indagini.html update)
    try:
        pipeline_data = json.loads(PIPELINE_JSON.read_text(encoding="utf-8"))
    except Exception as e:
        log_error(f"Failed to load pipeline.json: {e}")
        mark_approval_error(approval_id, dry_run)
        return 1

    # Get article metadata from pipeline v2 (check investigations[] and leads[])
    category = "Indagine"
    all_items = pipeline_data.get("investigations", []) + pipeline_data.get("leads", [])
    for item in all_items:
        if item.get("slug") == slug:
            category = item.get("category", "Indagine")
            break

    # 5a. Valida preview prima di pubblicare
    is_valid, validation_errors = validate_preview_before_publish(preview_path, slug)
    if not is_valid:
        log_error(f"VALIDAZIONE FALLITA per '{slug}':")
        for err in validation_errors:
            log_error(f"  - {err}")
        # Log nel journal
        journal_step("aiena_auto_publish", slug, "validate_article", "fail", "; ".join(validation_errors))
        alert_failure("aiena_auto_publish", slug, "validate_article", "; ".join(validation_errors))
        # Notifica Mirko via Telegram (usa BROKER_URL come notify_telegram())
        try:
            msg = (
                f"⚠️ *Pubblicazione bloccata*: {title}\n\n"
                + "\n".join(f"- {e}" for e in validation_errors)
                + f"\n\nSlug: {slug}\nCorreggi il preview e riprova."
            )
            _payload = json.dumps({
                "agent_name": "satoshi",
                "chat_id": MIRKO_CHAT_ID,
                "platform": "telegram",
                "content": msg,
            }).encode()
            _req = urllib.request.Request(
                BROKER_URL,
                data=_payload,
                headers=_build_broker_headers("POST", "/broker/send"),
                method="POST",
            )
            urllib.request.urlopen(_req, timeout=10)
        except Exception:
            pass
        mark_approval_error(approval_id, dry_run)
        return 1

    # Validazione OK
    journal_step("aiena_auto_publish", slug, "validate_article", "ok")

    # 5b. Transform preview HTML for production
    if not transform_preview_to_production(preview_path, articles_path, slug):
        journal_step("aiena_auto_publish", slug, "transform_preview", "fail", "Failed to transform preview to production")
        alert_failure("aiena_auto_publish", slug, "transform_preview", "Failed to transform preview to production")
        mark_approval_error(approval_id, dry_run)
        return 1

    # 6. Extract metadata from article HTML
    article_html = articles_path.read_text(encoding="utf-8")

    # 6b. Validate HTML for internal notes BEFORE updating index.html (TASK B — early check)
    html_is_clean_early, html_error_early = validate_html_for_internal_notes(article_html, slug)
    if not html_is_clean_early:
        log_error(f"HTML VALIDATION FAILED (early check): {html_error_early}")
        journal_step("aiena_auto_publish", slug, "validate_html_early", "fail", html_error_early)
        alert_failure("aiena_auto_publish", slug, "validate_html_early", html_error_early)
        msg_aiena = (
            f"ERRORE VALIDAZIONE HTML (pre-deploy): [{slug}]\n"
            f"{html_error_early}\n"
            f"L'articolo NON è stato pubblicato e index.html NON è stato aggiornato.\n"
            f"Correggi il contenuto e riprova."
        )
        msg_mirko = (
            f"⚠️ *Blocco pubblicazione*: {title}\n\n"
            f"Metadati interni rilevati (pre-deploy):\n{html_error_early}\n\n"
            f"Slug: {slug}\nL'articolo NON è stato pubblicato."
        )
        notify_aiena_and_mirko(msg_aiena, msg_mirko, slug, dry_run)
        mark_approval_error(approval_id, dry_run)
        return 1

    article_title = extract_h1(article_html) or title
    article_excerpt = extract_excerpt(article_html) or f"Indagine AIena su {article_title}"
    read_time = estimate_read_time(article_html)

    log(f"Article: {article_title}")
    log(f"Category: {category}")
    log(f"Excerpt: {article_excerpt[:80]}...")
    log(f"Read time: {read_time} min")

    # 7. Update hero in index.html
    log("Updating hero section in index.html...")
    if not update_hero_section(slug, article_title, article_excerpt, category, read_time):
        log_warn("Hero update had issues, continuing...")

    # 8. Update indagini.html
    log("Updating indagini.html...")
    if not update_indagini(slug, article_title, pipeline_data):
        log_warn("indagini.html update had issues, continuing...")

    # 9. Update archivio.html
    log("Updating archivio.html...")
    if not update_archivio(slug, article_title, article_excerpt, category):
        log_warn("archivio.html update had issues, continuing...")

    # 9b. Update feed.xml
    log("Updating feed.xml...")
    if not update_feed(slug, article_title, article_excerpt, category):
        log_warn("feed.xml update had issues, continuing...")

    # 10. Run post_publish.py (handles pipeline.json, diary in index.html, ticker)
    # IMPORTANT: This reads index.html (with our hero update) and modifies only the diary section
    log("Running post_publish.py...")
    journal_step("aiena_auto_publish", slug, "POST_PUBLISH", "ok")
    if not run_post_publish(slug, dry_run):
        log_error("post_publish.py failed")
        journal_step("aiena_auto_publish", slug, "POST_PUBLISH", "fail", "post_publish.py returned error")
        alert_failure("aiena_auto_publish", slug, "POST_PUBLISH", "post_publish.py returned error")
        mark_approval_error(approval_id, dry_run)
        return 1

    # 10.5. TASK B: Validate article HTML before FTP deployment
    log("Validating article HTML for internal metadata...")
    article_html_to_check = articles_path.read_text(encoding="utf-8")
    html_is_clean, html_error = validate_html_for_internal_notes(article_html_to_check, slug)
    if not html_is_clean:
        log_error(f"HTML VALIDATION FAILED: {html_error}")
        journal_step("aiena_auto_publish", slug, "validate_html", "fail", html_error)
        alert_failure("aiena_auto_publish", slug, "validate_html", html_error)
        # Block FTP and notify
        msg_aiena = (
            f"ERRORE VALIDAZIONE HTML: [{slug}]\n"
            f"{html_error}\n"
            f"L'articolo NON è stato pubblicato.\n"
            f"Correggi il contenuto e riprova."
        )
        msg_mirko = (
            f"⚠️ *Blocco pubblicazione*: {title}\n\n"
            f"Metadati interni rilevati nel HTML:\n{html_error}\n\n"
            f"Slug: {slug}\nL'articolo NON è stato pubblicato."
        )
        notify_aiena_and_mirko(msg_aiena, msg_mirko, slug, dry_run)
        mark_approval_error(approval_id, dry_run)
        return 1

    journal_step("aiena_auto_publish", slug, "validate_html", "ok")

    # 11. FTP deploy additional files (post_publish deploys index.html and pipeline.json)
    log("Deploying files via FTP...")

    # FTP article — W4: critical step; failure blocks published status
    article_ftp_ok = deploy_ftp(articles_path, f"articles/{slug}.html", dry_run, slug=slug)
    ftp_ok = article_ftp_ok
    if article_ftp_ok:
        journal_step("aiena_auto_publish", slug, "ftp_article", "ok")

        # TASK C: Verify live HTML is clean (no internal notes leaked)
        log("Verifying live HTML for internal metadata...")
        live_url = f"https://www.aiena.it/articles/{slug}.html"
        verify_ok, verify_error = verify_live_html_clean(live_url, slug)
        if not verify_ok:
            log_error(f"LIVE HTML VERIFICATION FAILED: {verify_error}")
            journal_step("aiena_auto_publish", slug, "verify_live_html", "fail", verify_error)
            msg_aiena = (
                f"ALERT POST-DEPLOY: [{slug}]\n"
                f"L'HTML LIVE contiene metadati interni:\n{verify_error}\n"
                f"L'articolo è pubblico ma CORROTTO.\n"
                f"Verifica immediatamente."
            )
            msg_mirko = (
                f"🚨 *ALERT POST-DEPLOY*: {title}\n\n"
                f"L'articolo è online ma contiene metadati interni:\n{verify_error}\n\n"
                f"URL: {live_url}\nVerifica URGENTE."
            )
            notify_aiena_and_mirko(msg_aiena, msg_mirko, slug, dry_run)
        else:
            journal_step("aiena_auto_publish", slug, "verify_live_html", "ok")
            log("Live HTML verification OK")
    else:
        journal_step("aiena_auto_publish", slug, "ftp_article", "fail", "FTP upload failed")
        alert_failure("aiena_auto_publish", slug, "ftp_article", "FTP upload failed")
        # W4: article not on server → do NOT mark as published, leave pending for retry
        log_warn("W4 guard: FTP article failed → aborting publish, leaving approval as pending")
        notify_telegram(
            f"⚠️ PUBLISH ABORTED: FTP upload fallito per '{slug}'. "
            f"Articolo NON pubblicato. Supabase lasciato in 'pending' per retry automatico.",
            dry_run=dry_run,
        )
        return 0

    # Sitemap auto-update: ensure new article is indexed
    sitemap_updated = update_sitemap(slug, dry_run)
    if sitemap_updated:
        ftp_ok = deploy_ftp(SITEMAP_XML, "sitemap.xml", dry_run, slug=slug)
        if ftp_ok:
            journal_step("aiena_auto_publish", slug, "ftp_sitemap", "ok")
        else:
            journal_step("aiena_auto_publish", slug, "ftp_sitemap", "fail", "sitemap FTP failed")
            log_warn("Sitemap FTP deploy failed — sitemap potrebbe non essere aggiornata")
    else:
        journal_step("aiena_auto_publish", slug, "sitemap_update", "fail", "sitemap update error")

    # FTP indagini
    ftp_ok = deploy_ftp(INDAGINI_HTML, "indagini.html", dry_run, slug=slug)
    if ftp_ok:
        journal_step("aiena_auto_publish", slug, "ftp_indagini", "ok")
    else:
        journal_step("aiena_auto_publish", slug, "ftp_indagini", "fail", "FTP upload failed")
        alert_failure("aiena_auto_publish", slug, "ftp_indagini", "FTP upload failed")

    # FTP archivio
    ftp_ok = deploy_ftp(ARCHIVIO_HTML, "archivio.html", dry_run, slug=slug)
    if ftp_ok:
        journal_step("aiena_auto_publish", slug, "ftp_archivio", "ok")
    else:
        journal_step("aiena_auto_publish", slug, "ftp_archivio", "fail", "FTP upload failed")
        alert_failure("aiena_auto_publish", slug, "ftp_archivio", "FTP upload failed")

    # FTP feed
    ftp_ok = deploy_ftp(FEED_XML, "feed.xml", dry_run, slug=slug)
    if ftp_ok:
        journal_step("aiena_auto_publish", slug, "ftp_feed", "ok")
    else:
        journal_step("aiena_auto_publish", slug, "ftp_feed", "fail", "FTP upload failed")
        alert_failure("aiena_auto_publish", slug, "ftp_feed", "FTP upload failed")

    # 12. OTS stamp
    ots_ok = run_ots_stamp(slug, articles_path, dry_run)
    if ots_ok:
        journal_step("aiena_auto_publish", slug, "ots_stamp", "ok")
    else:
        journal_step("aiena_auto_publish", slug, "ots_stamp", "fail", "OTS stamp failed")
        # Non manda alert per OTS perche' non e' critico

    # 13. Mark approval as published
    log("Marking approval as published in Supabase...")
    sb_ok = mark_approval_published(approval_id, dry_run)
    if sb_ok:
        journal_step("aiena_auto_publish", slug, "supabase_update", "ok")
    else:
        log_warn("Failed to mark approval as published in Supabase")
        journal_step("aiena_auto_publish", slug, "supabase_update", "fail", "Supabase update failed")
        alert_failure("aiena_auto_publish", slug, "supabase_update", "Failed to mark as published")

    # 14. Regenerate admin data.json
    run_update_admin_data(dry_run)

    # 15. Send Telegram notification
    tg_ok = notify_telegram(article_title, slug, dry_run)
    if tg_ok:
        journal_step("aiena_auto_publish", slug, "telegram_notify", "ok")
    else:
        journal_step("aiena_auto_publish", slug, "telegram_notify", "fail", "Telegram notification failed")
        # Non manda alert per Telegram perche' non e' critico

    # Log milestone finale
    journal_milestone("aiena_auto_publish", slug, "PUBLICATION_COMPLETE")

    # Write lock file to prevent double-publish this week
    # Salva la data del martedì di questa settimana (formato YYYY-MM-DD)
    if not dry_run:
        expected_lock = this_week_tuesday.isoformat()
        lock_path.write_text(expected_lock)
        log(f"Lock file scritto: {expected_lock}")

        # SAFEGUARD: Verify lock file was written correctly
        actual_lock = lock_path.read_text().strip()
        if actual_lock != expected_lock:
            log_error(f"LOCK FILE VERIFICATION FAILED: expected '{expected_lock}', got '{actual_lock}'")
            alert_failure("aiena_auto_publish", slug, "lock_verification", f"Lock mismatch: {actual_lock}")
        else:
            log("Lock file verified OK")

        # Write audit log for successful publication
        write_audit_log(
            "published",
            slug,
            f"Publication complete: {article_title}",
            lock_before,
            actual_lock
        )

    log("=" * 60)
    log("PUBLICATION COMPLETE")
    log(f"Article: {article_title}")
    log(f"URL: https://aiena.it/articles/{slug}.html")
    log("=" * 60)

    return 0


if __name__ == "__main__":
    sys.exit(main())
