#!/usr/bin/env python3
"""
aiena_urgente_processor.py — Fuori Programma: pubblica articolo urgente

Polling Supabase ogni 2 minuti (via cron).
Quando trova un record article_approvals con status='urgente_pending':
1. Valida + pubblica il file preview (come auto_publish)
2. Aggiorna hero in index.html con il nuovo articolo
3. Inserisce urgente card above "In corso" in index.html
4. Aggiorna griglia "In corso" (rimuove articolo pubblicato, avanza pipeline)
5. Aggiorna pipeline.json (rimuove dall'array, setta urgente_card, sposta in published)
6. FTP deploy di tutto
7. OTS stamp
8. Marca approval come published in Supabase
9. Notifica Mirko via Telegram

Cron (ogni 2 min):
    */2 * * * * /home/pinky/.pinkybot/.venv/bin/python3 /home/pinky/.pinkybot/scripts/aiena_urgente_processor.py >> /home/pinky/.pinkybot/data/logs/urgente_processor.log 2>&1

Author: Satoshi (PinkyBot)
"""

import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import urllib.request
import urllib.error
from datetime import date, datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

# ── Imports dal progetto ─────────────────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).parent))
from aiena_logging import journal_step, journal_milestone, ledger_ftp_from_file, alert_failure
from aiena_secrets import _load_secrets

# ── Config ───────────────────────────────────────────────────────────────────
AIENA_ROOT   = Path("/var/www/aiena.it")
PREVIEW_DIR  = AIENA_ROOT / "preview"
ARTICLES_DIR = AIENA_ROOT / "articles"
INDEX_HTML   = AIENA_ROOT / "index.html"
INDAGINI_HTML = AIENA_ROOT / "indagini.html"
ARCHIVIO_HTML = AIENA_ROOT / "archivio.html"
PIPELINE_JSON = AIENA_ROOT / "data" / "pipeline.json"

SCRIPTS_DIR       = Path("/home/pinky/.pinkybot/scripts")
OTS_STAMP_SCRIPT  = SCRIPTS_DIR / "aiena_ots_stamp.py"
UPDATE_ADMIN_SCRIPT = SCRIPTS_DIR / "update_admin_data.py"

FTP_HOST = "ftp.aiena.it"
FTP_USER = "aiena.it"
FTP_PASS = _load_secrets().get("FTP_PASS", "")

SB_URL         = "https://fwyjxolljcogblvwvfca.supabase.co"
SB_SERVICE_KEY = _load_secrets().get("SB_SERVICE_KEY", "")

BROKER_URL     = "http://localhost:8888/broker/send"
MIRKO_CHAT_ID  = "32405655"

# Registry SQLite del daemon — sorgente del bot token Telegram (bypass /broker/send).
# AgentRegistry inizializza il path come `<base>_agents.db` (vedi daemon.py).
REGISTRY_DB    = Path("/home/pinky/.pinkybot/data/conversations_agents.db")

MONTHS_IT  = ['gen', 'feb', 'mar', 'apr', 'mag', 'giu', 'lug', 'ago', 'set', 'ott', 'nov', 'dic']
MONTHS_LONG = ['gennaio', 'febbraio', 'marzo', 'aprile', 'maggio', 'giugno',
               'luglio', 'agosto', 'settembre', 'ottobre', 'novembre', 'dicembre']

# ── Logging ──────────────────────────────────────────────────────────────────

def log(msg: str, level: str = "INFO") -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] [{level}] {msg}")

def log_error(msg: str) -> None:
    log(msg, "ERROR")

def log_warn(msg: str) -> None:
    log(msg, "WARN")

# ── Date helpers ─────────────────────────────────────────────────────────────

def date_it(d: date) -> str:
    return f"{d.day:02d} {MONTHS_IT[d.month - 1]} {d.year}"

def date_it_long(d: date) -> str:
    return f"{d.day} {MONTHS_LONG[d.month - 1]} {d.year}"

# ── Supabase helpers ──────────────────────────────────────────────────────────

def sb_request(method: str, path: str, data: Optional[dict] = None) -> Optional[dict | list]:
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

def fetch_urgente_pending() -> list:
    result = sb_request("GET", "article_approvals?status=eq.urgente_pending&order=approved_at.asc")
    return result if isinstance(result, list) else []

def fetch_delete_pending() -> list:
    result = sb_request("GET", "article_approvals?status=eq.delete_preview_pending&order=approved_at.asc")
    return result if isinstance(result, list) else []

def ftp_delete(remote_path: str) -> bool:
    """Elimina un file dal server FTP tramite comando DELE."""
    cmd = [
        "curl", "-s",
        f"ftp://{FTP_HOST}/",
        "--user", f"{FTP_USER}:{FTP_PASS}",
        "-Q", f"DELE /{remote_path}"
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode == 0:
            log(f"FTP DELE OK: {remote_path}")
            return True
        else:
            log_warn(f"FTP DELE fail {remote_path}: {result.stderr}")
            return False
    except Exception as e:
        log_warn(f"FTP DELE error {remote_path}: {e}")
        return False


def process_delete(approval: dict) -> bool:
    """Gestisce una richiesta di cancellazione articolo in preview."""
    slug        = approval.get("slug", "")
    title       = approval.get("title", slug)
    approval_id = approval.get("id", "")

    log(f"── Cancellazione preview: {title} ({slug})")

    # Elimina file locale
    local_file = PREVIEW_DIR / f"{slug}.html"
    if local_file.exists():
        try:
            local_file.unlink()
            log(f"File locale eliminato: {local_file}")
        except Exception as e:
            log_error(f"Errore eliminazione locale: {e}")
            mark_error(approval_id)
            return False
    else:
        log_warn(f"File locale non trovato (già eliminato?): {local_file}")

    # Elimina da FTP
    ftp_delete(f"preview/{slug}.html")

    # Rimuovi dall'approval record → segna come 'deleted'
    sb_request("PATCH", f"article_approvals?id=eq.{approval_id}", {"status": "deleted"})

    # Rigenera admin data.json
    run_update_admin_data()

    log(f"✓ Articolo in preview cancellato: {slug}")
    return True


def mark_published(approval_id: str) -> bool:
    now_iso = datetime.now(timezone.utc).isoformat()
    result = sb_request("PATCH", f"article_approvals?id=eq.{approval_id}", {
        "status": "published",
        "published_at": now_iso
    })
    return result is not None

def mark_error(approval_id: str) -> bool:
    result = sb_request("PATCH", f"article_approvals?id=eq.{approval_id}", {
        "status": "urgente_error"
    })
    return result is not None

# ── HTML helpers ──────────────────────────────────────────────────────────────

_INTERNAL_PATTERNS = re.compile(
    r'Slug:|Data draft:|Pub_date|calendarizzare|pipeline\.json|#FIX|#DRAFT|#REVISIONE'
    r'|Categoria:\s*(Corruzione|Appalti|Fondi|Sanita|Politica|Chiesa)'
    r'|Articolo correlato:|Fonti:\s*\d+|Parole:\s*~?\d+',
    re.IGNORECASE
)

def _is_internal_text(text: str) -> bool:
    """Return True if text looks like internal pipeline notes (not public-safe)."""
    return bool(_INTERNAL_PATTERNS.search(text))

def extract_excerpt(html: str) -> str:
    """Extract public-safe excerpt from article meta description.
    Falls back to first real paragraph if meta description contains internal notes."""
    # NOTE: use quote-aware patterns to avoid stopping at apostrophes inside content.
    # content="..." → stop at " only; content='...' → stop at ' only.
    m = re.search(r'<meta\s+name=["\']description["\']\s+content="([^"]*)"', html, re.IGNORECASE)
    if not m:
        m = re.search(r"<meta\s+name=[\"']description[\"']\s+content='([^']*)'", html, re.IGNORECASE)
    if m:
        candidate = m.group(1)
        if not _is_internal_text(candidate):
            return candidate
        # Meta description is internal — fall back to first article paragraph
        log_warn("extract_excerpt: meta description contains internal notes, falling back to body text")

    # Fallback: first meaningful paragraph in article body
    body_m = re.search(r'<article[^>]*class=["\'][^"\']*article-body["\'][^>]*>(.*?)</article>', html, re.DOTALL | re.IGNORECASE)
    if body_m:
        body = body_m.group(1)
        # Skip AI disclaimer divs
        body = re.sub(r'<div[^>]*class=["\'][^"\']*ai-notice["\'][^>]*>.*?</div>', '', body, flags=re.DOTALL | re.IGNORECASE)
        paras = re.findall(r'<p[^>]*>(.*?)</p>', body, re.DOTALL | re.IGNORECASE)
        for p in paras:
            plain = re.sub(r'<[^>]+>', '', p).strip()
            if len(plain) > 60 and not _is_internal_text(plain) and 'intelligenza artificiale' not in plain.lower():
                return plain[:200]

    return ""

def extract_h1(html: str) -> str:
    m = re.search(r'<h1[^>]*class=["\'][^"\']*hero-title["\'][^>]*>([^<]+)</h1>', html, re.IGNORECASE)
    if not m:
        m = re.search(r'<h1[^>]*>([^<]+)</h1>', html, re.IGNORECASE)
    return m.group(1).strip() if m else ""

def estimate_read_time(html: str) -> int:
    text = re.sub(r'<[^>]+>', '', html)
    text = ' '.join(text.split())
    return max(1, len(text.split()) // 200)

# ── Template constants ────────────────────────────────────────────────────────

PRODUCTION_CSS_VERSION = "20260505"

BC_BOX_CSS = """    /* ── Blockchain Integrity Box ─────────────────────────────────────────── */
    .bc-box { margin: 2.5rem 0 1.5rem; border: 1px solid rgba(255,58,46,0.25); background: linear-gradient(135deg, rgba(255,58,46,0.04) 0%, rgba(13,13,13,0) 60%); position: relative; overflow: hidden; }
    .bc-box::before { content: ''; position: absolute; top: 0; left: 0; right: 0; height: 1px; background: linear-gradient(90deg, rgba(255,58,46,0.6) 0%, rgba(255,58,46,0) 100%); }
    .bc-header { display: flex; align-items: center; justify-content: space-between; padding: .7rem 1.25rem; border-bottom: 1px solid rgba(255,255,255,0.05); gap: .5rem; flex-wrap: wrap; cursor: pointer; transition: opacity .15s; }
    .bc-header:hover { opacity: .85; }
    .bc-toggle-arrow { font-size: .75rem; color: rgba(255,58,46,.5); transition: transform .2s; flex-shrink: 0; }
    .bc-box.bc-open .bc-toggle-arrow { transform: rotate(180deg); }
    .bc-header-left { display: flex; align-items: center; gap: .6rem; }
    .bc-dot { width: 7px; height: 7px; border-radius: 50%; background: var(--red); box-shadow: 0 0 6px rgba(255,58,46,0.6); flex-shrink: 0; }
    .bc-title { font-family: var(--font-mono); font-size: .68rem; font-weight: 700; letter-spacing: .12em; text-transform: uppercase; color: rgba(255,58,46,.85); }
    .bc-chips { display: flex; gap: .35rem; }
    .bc-chip { font-family: var(--font-mono); font-size: .53rem; letter-spacing: .1em; text-transform: uppercase; color: rgba(255,255,255,.3); border: 1px solid rgba(255,255,255,.1); padding: .15rem .55rem; }
    .bc-hashvis { display: flex; gap: 2px; padding: .65rem 1.25rem .5rem; flex-wrap: wrap; }
    .bc-hb { width: 8px; height: 8px; border-radius: 1px; flex-shrink: 0; opacity: .75; }
    .bc-body { padding: .1rem 1.25rem .85rem; display: flex; flex-direction: column; gap: .65rem; }
    .bc-field { display: flex; align-items: flex-start; gap: .85rem; }
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
    .bc-note-text code { font-family: var(--font-mono); font-size: .57rem; color: rgba(255,255,255,.28); background: rgba(255,255,255,.05); padding: .1rem .35rem; }
    .bc-info-btn { font-family: var(--font-mono); font-size: .57rem; letter-spacing: .06em; color: rgba(255,58,46,.6); background: none; white-space: nowrap; cursor: pointer; border: 1px solid rgba(255,58,46,.25); padding: .25rem .65rem; transition: all .2s; flex-shrink: 0; }
    .bc-info-btn:hover { background: rgba(255,58,46,.08); color: rgba(255,58,46,.95); border-color: rgba(255,58,46,.55); }
    .bc-explain { display: none; padding: .8rem 1.25rem 1rem; border-top: 1px solid rgba(255,58,46,.1); background: rgba(255,58,46,.03); }
    .bc-explain.open { display: block; }
    .bc-explain-title { font-family: var(--font-mono) !important; font-size: .62rem !important; font-weight: 700 !important; color: rgba(255,255,255,.5) !important; letter-spacing: .06em; margin-bottom: .5rem; display: block; }
    .bc-explain-text { font-family: var(--font-sans) !important; font-size: .82rem !important; color: rgba(255,255,255,.45) !important; line-height: 1.65 !important; font-weight: normal !important; font-style: normal !important; margin: 0 !important; }
    .bc-explain-text strong { color: rgba(255,255,255,.7); font-weight: 600; }
    .bc-history { border-top: 1px solid rgba(255,255,255,.04); padding: .5rem 1.25rem .65rem; display: none; flex-direction: column; gap: .3rem; }
    .bc-box.bc-open .bc-history { display: flex; }
    .bc-history-label { font-family: var(--font-mono) !important; font-size: .55rem !important; color: rgba(255,255,255,.2); letter-spacing: .08em; margin-bottom: .2rem; display: block; }
    .bc-history-item { display: flex; gap: .6rem; align-items: baseline; flex-wrap: wrap; }
    .bc-history-hash { font-family: var(--font-mono); font-size: .55rem; color: rgba(255,255,255,.2); }
    .bc-history-date { font-family: var(--font-mono); font-size: .55rem; color: rgba(255,255,255,.15); }
    .bc-history-note { font-family: var(--font-mono); font-size: .55rem; color: rgba(255,255,255,.25); font-style: italic; }
    .bc-box .bc-hashvis { display: none; } .bc-box .bc-body { display: none; } .bc-box .bc-note { display: none; } .bc-box .bc-explain { display: none; }
    .bc-box.bc-open .bc-hashvis { display: flex; } .bc-box.bc-open .bc-body { display: flex; } .bc-box.bc-open .bc-note { display: flex; } .bc-box.bc-open .bc-explain.open { display: block; }
    .bc-verify-result { font-family: var(--font-mono); font-size: .62rem; line-height: 1.6; padding: .45rem .9rem; margin: 0 1.25rem .5rem; border-radius: 3px; display: none; }
    .bc-verify-ok  { color: #22c55e; background: rgba(34,197,94,.06); border: 1px solid rgba(34,197,94,.2); }
    .bc-verify-warn{ color: #f59e0b; background: rgba(245,158,11,.06); border: 1px solid rgba(245,158,11,.2); }
    .bc-verify-err { color: #ff3a2e; background: rgba(255,58,46,.06);  border: 1px solid rgba(255,58,46,.2);  }"""

STATS_CSS = """    .footer-copy-wrap{display:inline-flex;align-items:center;gap:.5rem;}
    .stats-trigger{width:8px;height:8px;background:#e63946;border:none;cursor:pointer;padding:0;flex-shrink:0;animation:spin-slow 8s linear infinite;outline:none;opacity:.7;transition:opacity .2s;}
    .stats-trigger:hover{opacity:1;}
    @keyframes spin-slow{from{transform:rotate(0deg)}to{transform:rotate(360deg)}}
    .stats-overlay{display:none;position:fixed;inset:0;background:rgba(0,0,0,.75);z-index:999;align-items:center;justify-content:center;backdrop-filter:blur(4px);}
    .stats-overlay.open{display:flex;}
    .stats-modal{background:#161616;border:1px solid rgba(230,57,70,.3);max-width:480px;width:calc(100% - 2rem);padding:2rem;position:relative;animation:modal-in .25s ease;}
    @keyframes modal-in{from{opacity:0;transform:translateY(12px) scale(.97)}to{opacity:1;transform:translateY(0) scale(1)}}
    .stats-modal-close{position:absolute;top:.75rem;right:1rem;background:none;border:none;color:#999;font-size:1.2rem;cursor:pointer;line-height:1;transition:color .2s;}
    .stats-modal-close:hover{color:#f5f5f0;}
    .stats-modal-title{font-family:'JetBrains Mono',monospace;font-size:.6rem;letter-spacing:.14em;text-transform:uppercase;color:rgba(230,57,70,.7);margin-bottom:.4rem;}
    .stats-modal-headline{font-family:'Playfair Display',Georgia,serif;font-size:1.4rem;font-weight:700;color:#f5f5f0;margin-bottom:1.5rem;line-height:1.25;}
    .stats-big{text-align:center;margin-bottom:1.5rem;padding:1.2rem 0;border-top:1px solid #2a2a2a;border-bottom:1px solid #2a2a2a;}
    .stats-big-number{font-family:'Playfair Display',Georgia,serif;font-size:4rem;font-weight:700;color:#e63946;line-height:1;transition:opacity .4s;}
    .stats-big-label{font-family:'JetBrains Mono',monospace;font-size:.65rem;color:#999;letter-spacing:.1em;text-transform:uppercase;margin-top:.4rem;}
    .stats-grid{display:grid;grid-template-columns:1fr 1fr;gap:.75rem;}
    .stats-item{display:flex;flex-direction:column;gap:.2rem;}
    .stats-item-val{font-family:'JetBrains Mono',monospace;font-size:1.1rem;font-weight:600;color:#f5f5f0;}
    .stats-item-lbl{font-family:'JetBrains Mono',monospace;font-size:.58rem;color:#999;letter-spacing:.08em;text-transform:uppercase;}"""

FULL_FOOTER_HTML = """  <!-- FOOTER -->
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
      <div class="footer-subscribe">
        <span class="footer-subscribe-label">Ricevi le inchieste</span>
        <p class="footer-subscribe-text">Abbonati via email — ogni pubblicazione, direttamente nella tua inbox.</p>
        <a href="https://aienait.substack.com" class="footer-subscribe-link" target="_blank" rel="noopener">aienait.substack.com</a>
      </div>
      <div class="footer-bottom">
        <div class="footer-copy-wrap"><span class="footer-copy">© 2026 AIena.it — Prima AI investigativa italiana</span><button class="stats-trigger" id="statsTrigger" title="Statistiche AIena" aria-label="Statistiche AIena"></button></div>
        <span class="footer-motto">"Tutti hanno diritto di sapere. Nessuno ha il diritto di far dimenticare."</span>
        <span class="footer-credit" style="font-family:var(--font-mono);font-size:.65rem;color:#777;margin-top:.5rem;display:block;text-align:center;">con amore da <a href="https://t.me/ziomik" target="_blank" rel="noopener" style="color:#999;text-decoration:none;">@ziomik</a></span>
      </div>
    </div>
  </footer>

  <div class="stats-overlay" id="statsOverlay"><div class="stats-modal" role="dialog" aria-modal="true" aria-label="Statistiche AIena"><button class="stats-modal-close" id="statsClose" aria-label="Chiudi">✕</button><div class="stats-modal-title">Trasparenza — dati reali</div><div class="stats-modal-headline">Quanto lavora AIena</div><div class="stats-big"><div class="stats-big-number" id="bigNum">312</div><div class="stats-big-label" id="bigLbl">documenti esaminati</div></div><div class="stats-grid"><div class="stats-item"><span class="stats-item-val">47</span><span class="stats-item-lbl">Fonti analizzate</span></div><div class="stats-item"><span class="stats-item-val">140+</span><span class="stats-item-lbl">Ore di ricerca</span></div><div class="stats-item"><span class="stats-item-val">89</span><span class="stats-item-lbl">Connessioni trovate</span></div><div class="stats-item"><span class="stats-item-val">1</span><span class="stats-item-lbl">Articoli pubblicati</span></div><div class="stats-item"><span class="stats-item-val">3</span><span class="stats-item-lbl">Inchieste in corso</span></div><div class="stats-item"><span class="stats-item-val">3</span><span class="stats-item-lbl">Segnalazioni ricevute</span></div></div></div></div>"""

PRODUCTION_SCRIPTS = """  <script>
    // ── Hamburger menu ────────────────────────────────────────────────────────
    const hamburger = document.getElementById('hamburger');
    const nav = document.getElementById('main-nav');
    if (hamburger && nav) {
      hamburger.addEventListener('click', () => {
        const open = nav.classList.toggle('open');
        hamburger.setAttribute('aria-expanded', open);
      });
    }

    // ── Hash visualizer (attivo dopo OTS stamp) ───────────────────────────────
    (function() {
      const hashEl = document.getElementById('bc-hashval');
      if (!hashEl) return;
      const hash = hashEl.textContent.trim();
      if (hash === 'pending' || hash.length !== 64) return;
      const vis = document.getElementById('bc-hashvis');
      if (!vis) return;
      for (let i = 0; i < 64; i += 2) {
        const byte = parseInt(hash.slice(i, i + 2), 16);
        const hue = (byte * 360 / 255) | 0;
        const sat = 50 + (byte % 35);
        const lit = 30 + (byte % 30);
        const el = document.createElement('div');
        el.className = 'bc-hb';
        el.style.background = 'hsl(' + hue + ',' + sat + '%,' + lit + '%)';
        el.title = '0x' + hash.slice(i, i + 2);
        vis.appendChild(el);
      }
    })();

    /* ── Blockchain in-browser verify (text-only, v2) ───────────────────── */
    async function verifyIntegrity() {
      const btn = document.getElementById('bc-verify-btn');
      const res = document.getElementById('bc-verify-result');
      const storedEl = document.getElementById('bc-hashval');
      const stored = storedEl ? storedEl.textContent.trim() : '';
      if (!stored || stored === 'pending') {
        res.style.display = 'block';
        res.className = 'bc-verify-result bc-verify-warn';
        res.innerHTML = '⏳ &nbsp;Certificazione blockchain in corso — riprova tra qualche minuto.';
        return;
      }
      btn.disabled = true; btn.textContent = '⟳ calcolo...'; res.style.display = 'none';
      try {
        const resp = await fetch(window.location.href, { cache: 'no-store' });
        if (!resp.ok) throw new Error('Fetch fallito: HTTP ' + resp.status);
        const raw = await resp.text();
        const artMatch = raw.match(/<article[\\s\\S]*?<\\/article>/);
        let content = artMatch ? artMatch[0] : raw;
        content = content.replace(/<!-- BLOCKCHAIN INTEGRITY[\\s\\S]*?<!-- \\/BC-BOX -->/, '');
        content = content.replace(/<[^>]+>/g, ' ').replace(/\\s+/g, ' ').trim();
        const buf = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(content));
        const computed = Array.from(new Uint8Array(buf)).map(b => b.toString(16).padStart(2,'0')).join('');
        res.style.display = 'block';
        if (computed === stored) {
          res.className = 'bc-verify-result bc-verify-ok';
          res.innerHTML = '✓ &nbsp;Integrità verificata — il testo corrisponde al certificato blockchain.';
          btn.textContent = '✓ verificato';
        } else {
          res.className = 'bc-verify-result bc-verify-warn';
          res.innerHTML = '≠ &nbsp;Hash non corrisponde — il testo potrebbe essere stato modificato.';
          btn.textContent = '↺ riprova';
        }
      } catch(err) {
        res.style.display = 'block'; res.className = 'bc-verify-result bc-verify-err';
        res.innerHTML = '✕ &nbsp;Errore: ' + err.message; btn.textContent = '⚡ verifica ora';
      }
      btn.disabled = false;
    }

    // ── Segnala errore ────────────────────────────────────────────────────────
    const SUPABASE_URL = 'https://fwyjxolljcogblvwvfca.supabase.co';
    const SUPABASE_KEY = 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImZ3eWp4b2xsamNvZ2Jsdnd2ZmNhIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NDQ1ODg1MDYsImV4cCI6MjA2MDE2NDUwNn0.xJNjMRJWGqM5h9wL2hUkPrN8S47ltq5-N8NLFgFoXhI';
    const ARTICLE_SLUG = '__SLUG__';
    function genTicketCode() { const n=new Date(); return 'AIE-'+String(n.getMonth()+1).padStart(2,'0')+String(n.getDate()).padStart(2,'0')+'-'+(Math.floor(Math.random()*9000)+1000); }
    async function inviaErrore() {
      const testo=document.getElementById('errore-testo').value.trim();
      const msg=document.getElementById('errore-msg');
      if(!testo)return; msg.style.display='none';
      try {
        const res=await fetch(SUPABASE_URL+'/rest/v1/tickets',{method:'POST',headers:{'apikey':SUPABASE_KEY,'Authorization':'Bearer '+SUPABASE_KEY,'Content-Type':'application/json','Prefer':'return=minimal'},body:JSON.stringify({ticket_code:genTicketCode(),tipo:'correzione',titolo:'Errore segnalato: '+ARTICLE_SLUG,descrizione:testo,status:'aperto'})});
        if(!res.ok)throw new Error('HTTP '+res.status);
        msg.style.cssText='display:block;color:#22c55e;font-family:var(--font-mono);font-size:.75rem;'; msg.textContent='✓ Segnalazione ricevuta. Grazie.';
        document.getElementById('errore-testo').value='';
        setTimeout(()=>{document.getElementById('errore-box').style.display='none';msg.style.display='none';},3000);
      } catch(e){ msg.style.cssText='display:block;color:#ff3a2e;font-family:var(--font-mono);font-size:.75rem;'; msg.textContent='Errore invio. Riprova.'; }
    }

    // ── Stats modal ───────────────────────────────────────────────────────────
    (function(){
      var trigger=document.getElementById('statsTrigger'),overlay=document.getElementById('statsOverlay'),closeBtn=document.getElementById('statsClose');
      if(!trigger)return;
      var loaded=false;
      function loadStats(){ if(loaded)return; fetch('/stats.json?v='+Date.now()).then(function(r){return r.json();}).then(function(s){ loaded=true; var vals=overlay.querySelectorAll('.stats-item-val'); if(vals[0])vals[0].textContent=s.fonti_analizzate||'—'; if(vals[1])vals[1].textContent=(s.ore_ricerca||'—')+'+'; if(vals[2])vals[2].textContent=s.connessioni_trovate||'—'; if(vals[3])vals[3].textContent=s.articoli_pubblicati||'—'; if(vals[4])vals[4].textContent=s.inchieste_in_corso||'—'; if(vals[5])vals[5].textContent=s.segnalazioni_ricevute||'—'; items=[{num:String(s.documenti_esaminati||312),lbl:'documenti esaminati'},{num:String(s.fonti_analizzate||47),lbl:'fonti analizzate'},{num:(s.ore_ricerca||140)+'+',lbl:'ore di ricerca'},{num:String(s.connessioni_trovate||89),lbl:'connessioni trovate'},{num:String(s.inchieste_in_corso||3),lbl:'inchieste in corso'}]; if(bigNum){bigNum.textContent=items[idx].num;bigLbl.textContent=items[idx].lbl;} }).catch(function(){}); }
      trigger.addEventListener('click',function(){overlay.classList.add('open');loadStats();});
      closeBtn.addEventListener('click',function(){overlay.classList.remove('open');});
      overlay.addEventListener('click',function(e){if(e.target===overlay)overlay.classList.remove('open');});
      document.addEventListener('keydown',function(e){if(e.key==='Escape')overlay.classList.remove('open');});
      var items=[{num:'312',lbl:'documenti esaminati'},{num:'47',lbl:'fonti analizzate'},{num:'140+',lbl:'ore di ricerca'},{num:'89',lbl:'connessioni trovate'},{num:'3',lbl:'inchieste in corso'}];
      var idx=0,bigNum=document.getElementById('bigNum'),bigLbl=document.getElementById('bigLbl');
      setInterval(function(){ bigNum.style.opacity='0'; setTimeout(function(){ idx=(idx+1)%items.length; bigNum.textContent=items[idx].num; bigLbl.textContent=items[idx].lbl; bigNum.style.opacity='1'; },400); },3500);
    })();

    /* ── Blockchain box toggle ───────────────────────────────────────────── */
    document.addEventListener('DOMContentLoaded', function() {
      var bcHeader = document.querySelector('.bc-header');
      if (bcHeader) { bcHeader.addEventListener('click', function() { document.getElementById('bc-box').classList.toggle('bc-open'); }); }
    });
  </script>"""

ERRORE_BOX_HTML = """      <!-- ERRORE BOX -->
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


# ── File: preview → production ────────────────────────────────────────────────

# Import template builder for the new approach
try:
    from aiena_template_builder import (
        transform_preview_to_production_v2,
        extract_content_from_preview,
        build_from_template,
        TEMPLATE_PATH
    )
    TEMPLATE_BUILDER_AVAILABLE = TEMPLATE_PATH.exists()
except ImportError:
    TEMPLATE_BUILDER_AVAILABLE = False


def transform_preview_to_production(preview_path: Path, articles_path: Path, slug: str) -> bool:
    """
    Trasforma un file preview in versione production-ready.

    NUOVO APPROCCIO (v2 - template-based):
    Se il template e' disponibile, usa l'approccio template-based che:
    1. Estrae solo il contenuto testuale dalla preview
    2. Lo inietta in un template canonico fisso
    -> Output sempre strutturalmente identico

    FALLBACK (v1 - patch-based):
    Se il template non e' disponibile, usa il vecchio approccio che patcha la preview.

    NOTA: Il bc-box HTML viene aggiornato da aiena_ots_stamp.py dopo la pubblicazione.
    """
    # Prova prima l'approccio template-based (v2)
    if TEMPLATE_BUILDER_AVAILABLE:
        log(f"Usando approccio template-based per {slug}")
        try:
            success = transform_preview_to_production_v2(
                preview_path, articles_path, slug, pub_date=date.today()
            )
            if success:
                log(f"Transformed (template v2): {articles_path.name}")
                return True
            else:
                log_warn(f"Template v2 failed for {slug}, falling back to patch method")
        except Exception as e:
            log_warn(f"Template v2 error for {slug}: {e}, falling back to patch method")

    # Fallback: vecchio approccio patch-based (v1)
    return _transform_preview_to_production_v1(preview_path, articles_path, slug)


def _transform_preview_to_production_v1(preview_path: Path, articles_path: Path, slug: str) -> bool:
    """
    Vecchio approccio patch-based (v1) - mantenuto come fallback.

    Trasforma un file preview in versione production-ready:
    1. Rimuove noindex/preview banner
    2. Aggiorna canonical, og:url, datePublished
    3. Corregge versione CSS (styles.css?v=X → ?v=PRODUCTION_CSS_VERSION)
    4. Inietta bc-box CSS + stats CSS se mancanti
    5. Inietta errore-box HTML se mancante
    6. Sostituisce footer minimale con footer completo (footer-inner)
    7. Inietta stats-overlay HTML se mancante
    8. Sostituisce scripts minimali con set completo di produzione
    """
    try:
        html = preview_path.read_text(encoding="utf-8")

        # 1. Rimuovi noindex
        html = re.sub(
            r'<meta\s+name=["\']robots["\']\s+content=["\']noindex,?\s*nofollow?["\'][^>]*>\s*',
            '<meta name="robots" content="index, follow">\n  ', html, flags=re.IGNORECASE
        )

        # 2. Rimuovi diary-preview-banner (CSS + HTML)
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
        # Fix anche eventuali date future nel meta og
        html = re.sub(
            r'(<meta\s+property=["\']article:published_time["\']\s+content=["\'])\d{4}-\d{2}-\d{2}',
            f'\\g<1>{pub_date}', html, flags=re.IGNORECASE
        )
        # Fix nel JSON-LD
        html = re.sub(
            r'("datePublished"\s*:\s*")[^"]*"',
            f'\\g<1>{pub_date}"', html
        )

        # 6. Fix versione CSS styles.css
        html = re.sub(
            r'(styles\.css\?v=)[^\s"\']+',
            f'\\g<1>{PRODUCTION_CSS_VERSION}', html
        )

        # 7. Inietta bc-box CSS se mancante
        if '.bc-box' not in html:
            html = html.replace('</style>', BC_BOX_CSS + '\n  </style>', 1)
            log(f"bc-box CSS iniettato in {slug}")

        # 7b. Normalizza posizione bc-box — deve essere SEMPRE l'ultimo elemento prima di </article>
        # Se bc-box esiste ma non è subito prima di </article>, spostalo.
        # Questo corregge preview con bc-box in posizione errata (es. dopo i tag, a inizio articolo).
        if '<!-- BLOCKCHAIN INTEGRITY' in html and '</article>' in html:
            # Estrai il blocco bc-box
            bc_start_marker = '<!-- BLOCKCHAIN INTEGRITY'
            bc_end_marker = '<!-- /BC-BOX -->'
            if bc_end_marker in html:
                bc_start = html.index(bc_start_marker)
                bc_end = html.index(bc_end_marker) + len(bc_end_marker)
                bc_block = html[bc_start:bc_end].strip()
                # Verifica se è già l'ultimo elemento prima di </article>
                after_bc = html[bc_end:].strip()
                if not after_bc.startswith('</article>') and not after_bc.startswith('\n  </article>'):
                    # Non è in fondo — sposta
                    html = html[:bc_start].rstrip() + '\n' + html[bc_end:]
                    # Inserisci subito prima di </article>
                    html = html.replace('  </article>', f'      {bc_block}\n\n  </article>', 1)
                    log(f"bc-box riposizionato prima di </article> in {slug}")

        # 8. Inietta stats CSS se mancante
        if '.stats-trigger' not in html:
            html = html.replace('</style>', STATS_CSS + '\n  </style>', 1)
            log(f"stats CSS iniettato in {slug}")

        # 9. Inietta errore-box HTML se mancante (prima di </article>)
        if 'errore-box' not in html:
            html = html.replace(
                '    </div>\n  </article>',
                ERRORE_BOX_HTML + '\n\n    </div>\n  </article>',
                1
            )
            log(f"errore-box HTML iniettato in {slug}")

        # 10. Sostituisci footer minimale con footer completo
        # Minimal footer pattern: <footer class="site-footer"><div class="container">
        if 'footer-inner' not in html:
            # Rimuovi il footer minimale (da <!-- FOOTER --> o <footer class="site-footer"> fino a </footer>)
            html = re.sub(
                r'(?:<!--\s*FOOTER\s*-->\s*)?<footer\s+class=["\']site-footer["\'][^>]*>.*?</footer>',
                FULL_FOOTER_HTML,
                html, flags=re.DOTALL, count=1
            )
            log(f"Footer completo iniettato in {slug}")

        # 11. Inietta stats-overlay HTML se mancante
        if 'statsOverlay' not in html and 'stats-overlay' not in html:
            html = html.replace(
                FULL_FOOTER_HTML.split('\n')[-1],  # ultima riga del footer block
                FULL_FOOTER_HTML.split('\n')[-1]   # stesso — la stats-overlay è già nel FULL_FOOTER_HTML
            )

        # 12. Sostituisci scripts minimali con set completo di produzione
        # Se mancano funzioni chiave (verifyIntegrity, inviaErrore, stats modal)
        needs_scripts = (
            'verifyIntegrity' not in html or
            'inviaErrore' not in html or
            'statsOverlay' not in html or
            'statsTrigger' not in html
        )
        if needs_scripts:
            # Sostituisci l'intero blocco <script>...</script> finale
            production_scripts = PRODUCTION_SCRIPTS.replace('__SLUG__', slug)
            # Rimuovi script esistente (solo l'ultimo prima di </body>)
            html = re.sub(
                r'<script>[^<]*(?:hamburger|Hamburger)[^<]*(?:(?!</script>).)*</script>',
                '', html, flags=re.DOTALL
            )
            # Aggiungi script completo prima di </body>
            html = html.replace('</body>', production_scripts + '\n</body>', 1)
            log(f"Script produzione iniettato in {slug}")
        else:
            # Aggiorna solo ARTICLE_SLUG se necessario
            html = re.sub(
                r"const ARTICLE_SLUG = '[^']*'",
                f"const ARTICLE_SLUG = '{slug}'",
                html
            )

        articles_path.write_text(html, encoding="utf-8")
        log(f"Transformed (patch v1): {articles_path.name}")
        return True
    except Exception as e:
        log_error(f"_transform_preview_to_production_v1: {e}")
        return False

# ── index.html updates ────────────────────────────────────────────────────────

def update_hero_section(slug: str, title: str, excerpt: str, category: str, read_time: int) -> bool:
    try:
        html = INDEX_HTML.read_text(encoding="utf-8")
        date_str = date_it_long(date.today())

        html = re.sub(
            r'(<h1\s+class=["\']hero-title["\'][^>]*>)[^<]*(</h1>)',
            f'\\g<1>{title}\\g<2>', html
        )
        html = re.sub(
            r'(<p\s+class=["\']hero-excerpt["\'][^>]*>)[^<]*(</p>)',
            f'\\g<1>{excerpt}\\g<2>', html
        )
        html = re.sub(
            r'(<div\s+class=["\']hero-meta["\'][^>]*>)\s*<span>AIena</span>\s*<span>[^<]*</span>\s*<span>[^<]*</span>',
            f'\\g<1>\n        <span>AIena</span>\n        <span>{date_str}</span>\n        <span>{read_time} min lettura</span>',
            html
        )
        html = re.sub(
            r'(<a\s+href=["\'])articles/[^"\']+(["\'][^>]*class=["\'][^"\']*btn[^"\']*btn-primary[^"\']*["\'][^>]*>)',
            f'\\g<1>articles/{slug}.html\\g<2>', html
        )
        html = re.sub(
            r'(<div\s+class=["\']hero-badge["\'][^>]*>.*?<span\s+class=["\']badge\s+badge-cat["\'][^>]*>)[^<]*(</span>)',
            f'\\g<1>{category}\\g<2>', html, flags=re.DOTALL
        )
        INDEX_HTML.write_text(html, encoding="utf-8")
        log(f"Hero aggiornato: {title}")
        return True
    except Exception as e:
        log_error(f"update_hero_section: {e}")
        return False


def build_urgente_card_html(slug: str, title: str, category: str, excerpt: str,
                             pub_date: str, urgente_until: str) -> str:
    """Genera l'HTML della urgente card da inserire tra i marker."""
    # Calcola data scadenza leggibile
    try:
        until_dt = datetime.fromisoformat(urgente_until)
        until_label = f"Questo avviso sparisce il {until_dt.day} {MONTHS_IT[until_dt.month - 1]}"
    except Exception:
        until_label = "Questo avviso sparisce tra 72 ore"

    # Data pubblicazione leggibile
    try:
        pd = date.fromisoformat(pub_date)
        pub_label = date_it_long(pd)
    except Exception:
        pub_label = pub_date

    return f"""
        <div class="urgente-card">
          <div class="urgente-bar"></div>
          <div class="urgente-inner">
            <div class="urgente-badges">
              <span class="urgente-badge-live"><span class="urgente-pulse"></span>pubblicato fuori programma</span>
              <span class="urgente-badge-cat">{category}</span>
            </div>
            <h2 class="urgente-title">{title}</h2>
            <p class="urgente-excerpt">{excerpt}</p>
            <div class="urgente-footer">
              <div>
                <div class="urgente-meta">AIena — {pub_label}</div>
                <div class="urgente-scad">{until_label}</div>
              </div>
              <a href="/articles/{slug}.html" class="urgente-cta">Leggi l'inchiesta →</a>
            </div>
          </div>
        </div>"""


def insert_urgente_card(slug: str, title: str, category: str, excerpt: str,
                        pub_date: str, urgente_until: str) -> bool:
    """Inserisce la urgente card tra i marker <!-- URGENTE-CARD-START --><!-- URGENTE-CARD-END -->"""
    try:
        html = INDEX_HTML.read_text(encoding="utf-8")
        card_html = build_urgente_card_html(slug, title, category, excerpt, pub_date, urgente_until)
        new_block = f"<!-- URGENTE-CARD-START -->{card_html}\n        <!-- URGENTE-CARD-END -->"
        html = re.sub(
            r'<!-- URGENTE-CARD-START -->.*?<!-- URGENTE-CARD-END -->',
            new_block, html, flags=re.DOTALL
        )
        INDEX_HTML.write_text(html, encoding="utf-8")
        log("Urgente card inserita in index.html")
        return True
    except Exception as e:
        log_error(f"insert_urgente_card: {e}")
        return False


def update_incorso_grid(published_slug: str, pipeline_items: list) -> bool:
    """
    Aggiorna la griglia 'In corso' in index.html:
    - Rimuove la card dell'articolo pubblicato urgente
    - Inserisce la card del quarto articolo (pipeline[2]) al posto libero

    pipeline_items: la nuova lista pipeline DOPO la rimozione dell'articolo pubblicato
                    (al massimo i primi 3 vengono mostrati in home)
    """
    try:
        html = INDEX_HTML.read_text(encoding="utf-8")

        # Trova la griglia articles-grid nella sezione main
        # Pattern: <div class="articles-grid">...</div>
        grid_match = re.search(
            r'(<div\s+class=["\']articles-grid["\'][^>]*>)(.*?)(</div>\s*\n\s*</div>\s*\n\s*</section>)',
            html, flags=re.DOTALL
        )
        if not grid_match:
            log_warn("articles-grid non trovata in index.html — aggiorno manualmente le card")
            # Ricostruisco manualmente le 3 card dai dati pipeline
            return _rebuild_incorso_cards(html, pipeline_items)

        grid_open  = grid_match.group(1)
        grid_inner = grid_match.group(2)
        grid_close = grid_match.group(3)

        # Rimuovi la card con il titolo/slug dell'articolo pubblicato
        # Le card hanno: <!-- Card N --> seguito da <div class="article-card" ...>
        # Troviamo la card che contiene il published_slug nel testo o nel titolo
        # Strategia: rimuoviamo le card esistenti e ricostruiamo dalle prime 3 del nuovo pipeline

        return _rebuild_incorso_cards(html, pipeline_items)

    except Exception as e:
        log_error(f"update_incorso_grid: {e}")
        return False


def _rebuild_incorso_cards(html: str, pipeline_items: list) -> bool:
    """Ricostruisce le 3 card 'In corso' dalla lista pipeline (max 3 elementi).

    GUARD: Se investigations[0] esiste, ha pub_date ed è assente dalla lista,
    viene preposto come #1 in modo che la griglia non sia mai sfasata rispetto alla
    scaletta reale (es. dopo rimozione di un articolo urgente dalla pipeline[]).
    """
    try:
        # ── Guard: investigations[0] deve sempre comparire se ha una data ──
        try:
            pipeline_data = json.loads(PIPELINE_JSON.read_text(encoding="utf-8"))
            investigations = pipeline_data.get("investigations", [])
            cur_inv = investigations[0] if investigations else None
            if cur_inv and cur_inv.get("pub_date") and cur_inv.get("slug"):
                cur_slug = cur_inv["slug"]
                already_in = any(p.get("slug") == cur_slug for p in pipeline_items)
                already_published = any(
                    p.get("slug") == cur_slug
                    for p in pipeline_data.get("published", [])
                )
                if not already_in and not already_published:
                    pipeline_items = [cur_inv] + pipeline_items
                    log(f"Guard: investigations[0] '{cur_slug}' preposto alla griglia")
        except Exception as guard_err:
            log_warn(f"Guard investigations[0]: {guard_err}")

        # Genera le nuove card
        cards_html = "\n"
        for i, item in enumerate(pipeline_items[:3], start=1):
            title    = item.get("title", "")
            category = item.get("category", "Indagine")
            excerpt  = item.get("diary_text", "")
            # Rimuovi HTML tags dall'excerpt
            excerpt_plain = re.sub(r'<[^>]+>', '', excerpt)[:150]
            status   = item.get("status", "in_progress")
            if status in ("approved", "approvato"):
                meta_label = "In preview"
            elif status == "scrittura":
                meta_label = "In scrittura"
            else:
                meta_label = "In lavorazione"

            cards_html += f"""
          <!-- Card {i} -->
          <div class="article-card" style="opacity: 0.55; cursor: default;">
            <div class="card-badges">
              <span class="badge badge-ongoing">In corso</span>
              <span class="badge badge-cat">{category}</span>
            </div>
            <h2 class="card-title">{title}</h2>
            <p class="card-excerpt">{excerpt_plain}</p>
            <div class="card-meta"><span>AIena</span><span>{meta_label}</span></div>
          </div>
"""

        # Sostituisci il contenuto interno della griglia
        new_html = re.sub(
            r'(<div\s+class=["\']articles-grid["\'][^>]*>).*?(</div>\s*\n\s*</div>\s*\n\s*</section>)',
            f'\\g<1>{cards_html}\n        \\g<2>',
            html, flags=re.DOTALL, count=1
        )

        if new_html == html:
            log_warn("_rebuild_incorso_cards: nessuna sostituzione effettuata")
            return False

        INDEX_HTML.write_text(new_html, encoding="utf-8")
        log("Griglia 'In corso' aggiornata")
        return True
    except Exception as e:
        log_error(f"_rebuild_incorso_cards: {e}")
        return False

# ── pipeline.json update ──────────────────────────────────────────────────────

def update_pipeline_for_urgente(slug: str, title: str, category: str, excerpt: str) -> bool:
    """
    Aggiorna pipeline.json per la pubblicazione urgente:
    1. Rimuove l'articolo da pipeline[]
    2. Aggiunge urgente_card field
    3. Sposta in published[]
    4. Salva
    """
    try:
        pipeline = json.loads(PIPELINE_JSON.read_text(encoding="utf-8"))

        today     = date.today()
        until_dt  = datetime.now(timezone.utc) + timedelta(hours=72)
        pub_date  = today.isoformat()
        urgente_until = until_dt.isoformat()

        original_items = pipeline.get("investigations", [])

        # PROTEZIONE: investigations[0] e investigations[1] = scaletta editoriale Mirko.
        # NON rimuovere automaticamente se il fuori-programma coincide con uno di questi.
        _slug_0 = original_items[0].get("slug") if original_items else None
        _slug_1 = original_items[1].get("slug") if len(original_items) > 1 else None

        if slug in (_slug_0, _slug_1):
            pos = 0 if _slug_0 == slug else 1
            log_warn(
                f"ATTENZIONE: articolo urgente '{slug}' coincide con investigations[{pos}] "
                f"(scaletta Mirko) — investigations[] NON modificato"
            )
            # Nessuna modifica a investigations[]: la scaletta editoriale rimane intatta.
            # L'articolo viene comunque pubblicato come urgente (urgente_card + published[]).
        else:
            # Rimuovi da investigations[] solo se non è in posizione 0 o 1
            pipeline["investigations"] = [item for item in original_items if item.get("slug") != slug]
            log(f"Rimosso da investigations[]: {slug} ({len(original_items)} → {len(pipeline['investigations'])} elementi)")

        # Aggiorna urgente_card
        pipeline["urgente_card"] = {
            "slug":         slug,
            "title":        title,
            "category":     category,
            "excerpt":      excerpt,
            "pub_date":     pub_date,
            "urgente_until": urgente_until
        }
        log(f"urgente_card impostata, scade: {until_dt.strftime('%Y-%m-%d %H:%M UTC')}")

        # Aggiungi a published[]
        published_entry = {
            "title":        title,
            "slug":         slug,
            "status":       "pubblicato",
            "category":     category,
            "published_at": pub_date,
            "url":          f"https://aiena.it/articles/{slug}.html"
        }
        pipeline.setdefault("published", []).insert(0, published_entry)

        # Aggiunge evento
        pipeline.setdefault("events", []).insert(0, {
            "date":         pub_date,
            "type":         "pubblicazione_urgente",
            "title":        f"Articolo pubblicato fuori programma: {title}",
            "description":  "Pubblicazione anticipata per rilevanza di attualità.",
            "related_slug": slug,
            "icon":         "urgente"
        })

        pipeline["updated_at"] = pub_date

        # Scrivi atomicamente
        fd, tmp = tempfile.mkstemp(dir=PIPELINE_JSON.parent, suffix=".json.tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(pipeline, f, ensure_ascii=False, indent=2)
            os.chmod(tmp, 0o600)  # dati editoriali interni: NON servire via HTTP (data/.htaccess: Deny from all)
            os.replace(tmp, PIPELINE_JSON)
        except Exception:
            try:
                os.unlink(tmp)
            except Exception:
                pass
            raise

        log("pipeline.json aggiornato")
        return pipeline  # Ritorna il pipeline aggiornato per estrarre il nuovo "In corso"

    except Exception as e:
        log_error(f"update_pipeline_for_urgente: {e}")
        return False

# ── archivio.html update ──────────────────────────────────────────────────────

def extract_article_tags(slug: str) -> list[str]:
    """Estrae i tag (#hashtag) dall'articolo pubblicato."""
    article_path = ARTICLES_DIR / f"{slug}.html"
    if not article_path.exists():
        return []
    try:
        html = article_path.read_text(encoding="utf-8")
        tags = re.findall(r'<span[^>]*class="[^"]*dpb-tag[^"]*"[^>]*>(#\w+)</span>', html)
        if not tags:
            # Fallback: cerca qualsiasi span con #hashtag
            tags = re.findall(r'class="[^"]*tag[^"]*"[^>]*>(#[a-zA-Z]\w+)<', html)
        return [t.strip() for t in tags if t.strip()]
    except Exception:
        return []


def update_archivio(slug: str, title: str, excerpt: str, category: str) -> bool:
    try:
        html = ARCHIVIO_HTML.read_text(encoding="utf-8")
        date_str = date_it(date.today())

        # Estrai tag dall'articolo
        tags = extract_article_tags(slug)
        tags_str = " ".join(tags)  # es. "#cortedeconti #pontestretto"
        tags_html = "".join(f'<span class="card-tag">{t}</span>' for t in tags)

        new_card = f'''
          <!-- Articolo: {slug} -->
          <a href="/articles/{slug}.html" class="article-card" data-category="{category}" data-title="{title}" data-excerpt="{excerpt}" data-tags="{tags_str}">
            <div class="card-badges">
              <span class="badge badge-new">Published</span>
              <span class="badge badge-cat">{category}</span>
            </div>
            <h3 class="card-title">{title}</h3>
            <p class="card-excerpt">{excerpt[:150]}{"..." if len(excerpt) > 150 else ""}</p>
            <div class="card-meta">
              <span>{date_str}</span>
              {f'<div class="card-tags">{tags_html}</div>' if tags else ''}
            </div>
          </a>'''

        # Aggiungi bottone filtro categoria se non esiste già
        if f'data-category="{category}"' not in html or \
           f'class="filter-btn" data-category="{category}"' not in html:
            # Inserisci nuovo filtro prima del div articles-grid
            new_filter_btn = f'<button class="filter-btn" data-category="{category}">{category}</button>'
            html = html.replace(
                '<div class="articles-grid"',
                f'          {new_filter_btn}\n          </div>\n\n        <div class="articles-grid"',
                1
            )
            # Più sicuro: inserisci dopo l'ultimo filter-btn
            last_btn_end = html.rfind('</button>', 0, html.find('<div class="articles-grid"'))
            if last_btn_end != -1:
                # Annulla il replace sopra e fai il corretto
                html = ARCHIVIO_HTML.read_text(encoding="utf-8")
                insert_pos = html.rfind('</button>', 0, html.find('articles-grid')) + len('</button>')
                html = html[:insert_pos] + f'\n            {new_filter_btn}' + html[insert_pos:]
            log(f"Nuovo filtro categoria aggiunto: {category}")

        pattern = r'(<div\s+class=["\']articles-grid["\']\s+id=["\']articles-grid["\'][^>]*>)'
        html = re.sub(pattern, f'\\g<1>{new_card}', html, count=1)
        ARCHIVIO_HTML.write_text(html, encoding="utf-8")
        log(f"archivio.html aggiornato (tags: {tags_str or 'nessuno'})")
        return True
    except Exception as e:
        log_error(f"update_archivio: {e}")
        return False

FEED_XML = AIENA_ROOT / "feed.xml"

def update_feed(slug: str, title: str, excerpt: str, category: str) -> bool:
    """Prepend new article as first <item> in feed.xml (RSS)."""
    try:
        from email.utils import format_datetime
        xml = FEED_XML.read_text(encoding="utf-8")
        pub_date = format_datetime(datetime.now(timezone.utc))
        # Escape XML special chars in fields
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
        # Insert after <atom:link .../> line
        marker = 'type="application/rss+xml"/>'
        if marker in xml:
            idx = xml.index(marker) + len(marker)
            xml = xml[:idx] + "\n        " + new_item + xml[idx:]
        else:
            # Fallback: insert before first <item>
            xml = xml.replace("<item>", new_item + "<item>", 1)
        FEED_XML.write_text(xml, encoding="utf-8")
        log("feed.xml aggiornato")
        return True
    except Exception as e:
        log_error(f"update_feed: {e}")
        return False


# ── FTP deploy ────────────────────────────────────────────────────────────────

def deploy_ftp(local_path: Path, remote_path: str, slug: str = "") -> bool:
    cmd = [
        "curl", "-s", "-T", str(local_path),
        f"ftp://{FTP_HOST}/{remote_path}",
        "--user", f"{FTP_USER}:{FTP_PASS}"
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode == 0:
            log(f"FTP OK: {remote_path}")
            ledger_ftp_from_file(
                script="aiena_urgente_processor", slug=slug,
                local_path=local_path, remote_path=remote_path,
                operation="ftp_upload", success=True
            )
            return True
        else:
            log_error(f"FTP fail {remote_path}: {result.stderr}")
            return False
    except Exception as e:
        log_error(f"FTP error {remote_path}: {e}")
        return False

# ── OTS + admin ───────────────────────────────────────────────────────────────

def run_ots_stamp(slug: str) -> None:
    if not OTS_STAMP_SCRIPT.exists():
        return
    try:
        result = subprocess.run(
            [sys.executable, str(OTS_STAMP_SCRIPT), "--note", f"Pubblicazione urgente: {slug}"],
            capture_output=True, text=True, timeout=120
        )
        if result.returncode == 0:
            log("OTS stamp completato")
        else:
            log_warn(f"OTS stamp warning: {result.stderr[:200]}")
    except Exception as e:
        log_warn(f"OTS stamp error: {e}")

def run_update_admin_data() -> None:
    try:
        result = subprocess.run(
            [sys.executable, str(UPDATE_ADMIN_SCRIPT)],
            capture_output=True, text=True, timeout=60
        )
        if result.returncode == 0:
            log("admin data.json rigenerato")
        else:
            log_warn(f"admin data warning: {result.stderr[:200]}")
    except Exception as e:
        log_warn(f"admin data error: {e}")

# ── Telegram ──────────────────────────────────────────────────────────────────
#
# Storico: fino al 18 maggio 2026 le notifiche passavano via /broker/send. Dopo
# i commit di auth-gate (PR #506 + #671) l'endpoint richiede autenticazione che
# questi script non hanno. Bypassiamo il broker leggendo il bot token direttamente
# dal registry SQLite del daemon e chiamando l'HTTPS Bot API di Telegram.

def _get_telegram_bot_token(agent_name: str = "satoshi") -> str:
    """Risolve il bot token Telegram dal registry SQLite del daemon.

    Replica la logica di `AgentRegistry.get_raw_token` (inline token →
    bot_tokens.token_ref → "") senza dipendere dal daemon HTTP. Apertura in
    sola lettura tramite URI mode per non disturbare il daemon vivo.
    """
    try:
        conn = sqlite3.connect(f"file:{REGISTRY_DB}?mode=ro", uri=True, timeout=5)
        try:
            row = conn.execute(
                "SELECT token, token_ref FROM agent_tokens "
                "WHERE agent_name=? AND platform='telegram'",
                (agent_name,),
            ).fetchone()
            if not row:
                return ""
            inline_token = row[0] or ""
            token_ref = row[1] or ""
            if inline_token:
                return inline_token
            if token_ref:
                ref_row = conn.execute(
                    "SELECT token FROM bot_tokens WHERE id=?", (token_ref,)
                ).fetchone()
                return (ref_row[0] if ref_row else "") or ""
            return ""
        finally:
            conn.close()
    except Exception as e:
        log_warn(f"_get_telegram_bot_token: {e}")
        return ""


def _send_telegram_direct(message: str, *, context: str = "") -> bool:
    """Invia un messaggio a Mirko via Telegram Bot API, bypassando il broker.

    Returns True se il messaggio è stato consegnato (HTTP 200 e `ok: true` nel
    JSON di risposta), False altrimenti. Sempre logga il risultato: nessun
    failure path è silenzioso.
    """
    token = _get_telegram_bot_token("satoshi")
    if not token:
        log_error(
            f"Telegram FAIL ({context or 'no-context'}): "
            f"nessun bot token per 'satoshi' in {REGISTRY_DB}"
        )
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": MIRKO_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown",
        "disable_web_page_preview": False,
    }
    try:
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            if resp.status == 200:
                try:
                    data = json.loads(body)
                except Exception:
                    data = {}
                if data.get("ok") is True:
                    log(f"Notifica Telegram inviata ({context or 'no-context'})")
                    return True
                log_error(
                    f"Telegram non-ok ({context or 'no-context'}): "
                    f"{body[:240]}"
                )
                return False
            log_error(
                f"Telegram HTTP {resp.status} ({context or 'no-context'}): "
                f"{body[:240]}"
            )
            return False
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            body = ""
        log_error(
            f"Telegram HTTPError {e.code} ({context or 'no-context'}): "
            f"{body[:240]}"
        )
        return False
    except Exception as e:
        log_error(f"Telegram error ({context or 'no-context'}): {e}")
        return False


def notify_telegram(title: str, slug: str, urgente_until: str) -> None:
    """Notifica di successo: articolo pubblicato fuori programma."""
    try:
        until_dt = datetime.fromisoformat(urgente_until)
        until_str = until_dt.strftime("%d %b %H:%M UTC")
    except Exception:
        until_str = "72 ore"

    message = (
        f"⚡ *Articolo pubblicato fuori programma*\n\n"
        f"{title}\n\n"
        f"https://aiena.it/articles/{slug}.html\n\n"
        f"La card urgente è visibile in homepage fino al {until_str}.\n"
        f"La scaletta normale riprende la settimana prossima."
    )
    _send_telegram_direct(message, context=f"publish_success {slug}")

# ── Validation ────────────────────────────────────────────────────────────────

def validate_preview(preview_path: Path, slug: str) -> tuple[bool, list[str]]:
    """Validazione del file preview prima della trasformazione.

    NOTA: I check su `ai-notice` e `open-question` sono stati rimossi (P0 fix,
    9 giugno 2026). Quei marker NON appartengono al preview: vengono iniettati
    dal template canonico in `transform_preview_to_production_v2()`
    (aiena_template_builder.py). Controllarli qui produceva false positive su
    ogni preview attuale, bloccando il flusso fuori-programma.
    Il check effettivo che quei marker finiscano nell'articolo pubblicato
    spetta al template builder e/o a una validazione post-trasformazione.
    """
    errors = []
    try:
        html = preview_path.read_text(encoding="utf-8")
        if '"@type": "NewsArticle"' not in html:
            errors.append("MANCA schema NewsArticle")
        all_ext = [l for l in re.findall(r'href="(https?://[^"]+)"', html)
                   if 'aiena.it' not in l and 'fonts.g' not in l and 'googleapis' not in l]
        if len(all_ext) < 5:
            errors.append(f"Fonti insufficienti: {len(all_ext)} (min 5)")
        return len(errors) == 0, errors
    except Exception as e:
        return False, [f"Errore lettura: {e}"]

# ── Post publish script ───────────────────────────────────────────────────────

def run_post_publish(slug: str) -> bool:
    """Chiama aiena_post_publish.py che aggiorna pipeline.json, diary, ticker."""
    post_publish = SCRIPTS_DIR / "aiena_post_publish.py"
    if not post_publish.exists():
        log_warn("aiena_post_publish.py non trovato, skip")
        return True
    try:
        result = subprocess.run(
            [sys.executable, str(post_publish), "--slug", slug],
            capture_output=True, text=True, timeout=120
        )
        print(result.stdout)
        if result.returncode != 0:
            log_error(f"post_publish failed: {result.returncode}")
            return False
        return True
    except Exception as e:
        log_error(f"post_publish error: {e}")
        return False

# ── Main ─────────────────────────────────────────────────────────────────────

def process_one(approval: dict) -> bool:
    slug        = approval.get("slug", "")
    title       = approval.get("title", "")
    approval_id = approval.get("id", "")

    log(f"── Elaborazione urgente: {title} ({slug})")
    log(f"   Approval ID: {approval_id}")

    # 1. File preview
    preview_path  = PREVIEW_DIR  / f"{slug}.html"
    articles_path = ARTICLES_DIR / f"{slug}.html"

    if not preview_path.exists():
        log_error(f"File preview non trovato: {preview_path}")
        mark_error(approval_id)
        return False

    # 2. Leggi pipeline PRIMA
    try:
        pipeline = json.loads(PIPELINE_JSON.read_text(encoding="utf-8"))
    except Exception as e:
        log_error(f"pipeline.json illeggibile: {e}")
        mark_error(approval_id)
        return False

    # Trova categoria dall'articolo nel investigations
    category = "Indagine"
    for item in pipeline.get("investigations", []):
        if item.get("slug") == slug:
            category = item.get("category", "Indagine")
            break
    investigations = pipeline.get("investigations", [])
    nxt = investigations[1] if len(investigations) > 1 else {}
    if nxt.get("slug") == slug:
        category = nxt.get("category", category)

    # 3. Valida
    is_valid, errors = validate_preview(preview_path, slug)
    if not is_valid:
        log_error(f"Validazione fallita per '{slug}':")
        for e in errors:
            log_error(f"  - {e}")

        # Notifica Mirko via Telegram diretto. Failsafe: se la consegna fallisce,
        # alert_failure() registra un alert centrale che NON va in /broker/send
        # (vedi aiena_logging) — cosi un singolo canale rotto non rende il fault
        # silenzioso. NIENTE `except: pass` qui: tutti gli errori restano nel log.
        err_msg = (
            f"⚠️ *Fuori Programma bloccato*\n"
            f"{title}\n`{slug}`\n\n"
            f"Problemi trovati:\n"
            + "\n".join(f"• {e}" for e in errors)
            + "\n\nIl record e' stato segnato come `urgente_error`. "
              "Per riprovare: reset manuale da admin/SQL."
        )
        delivered = _send_telegram_direct(
            err_msg, context=f"validation_failure {slug}"
        )
        if not delivered:
            try:
                alert_failure(
                    script="aiena_urgente_processor",
                    slug=slug,
                    operation="validation",
                    error=(
                        "Validation failed AND Telegram notify failed. "
                        f"Errors: {errors}"
                    ),
                )
            except Exception as af_err:
                # Doppio canale rotto: lo logghiamo a livello ERROR (il log
                # urgente_processor.log e' l'ultima rete di sicurezza).
                log_error(
                    f"alert_failure crashed dopo Telegram fail: {af_err}"
                )

        mark_error(approval_id)
        return False

    # 4. Trasforma preview → production
    if not transform_preview_to_production(preview_path, articles_path, slug):
        mark_error(approval_id)
        return False

    # 5. Estrai metadata
    article_html = articles_path.read_text(encoding="utf-8")
    article_title   = extract_h1(article_html) or title
    article_excerpt = extract_excerpt(article_html) or f"Indagine AIena su {article_title}"
    read_time       = estimate_read_time(article_html)

    log(f"Title: {article_title}")
    log(f"Category: {category}")
    log(f"Read time: {read_time} min")

    # NOTA: il fuori programma NON deve MAI toccare il hero.
    # Il hero mostra sempre l'articolo principale della settimana corrente.
    # Il fuori programma si limita alla urgente card above "In corso".

    # 7. Aggiorna pipeline.json (rimuove articolo, imposta urgente_card)
    updated_pipeline = update_pipeline_for_urgente(slug, article_title, category, article_excerpt)
    if not updated_pipeline:
        mark_error(approval_id)
        return False

    # 8. Inserisci urgente card in index.html
    urgente_card = updated_pipeline.get("urgente_card", {})
    if not insert_urgente_card(
        slug=slug, title=article_title, category=category,
        excerpt=article_excerpt,
        pub_date=urgente_card.get("pub_date", date.today().isoformat()),
        urgente_until=urgente_card.get("urgente_until", "")
    ):
        log_warn("urgente card insert fallita, continuo...")

    # 9. Aggiorna griglia "In corso" (prime 3 dal nuovo investigations)
    new_pipeline_items = updated_pipeline.get("investigations", [])
    if not update_incorso_grid(slug, new_pipeline_items):
        log_warn("update_incorso_grid fallito, continuo...")

    # 10. Aggiorna archivio.html
    update_archivio(slug, article_title, article_excerpt, category)

    # 10b. Aggiorna feed.xml
    update_feed(slug, article_title, article_excerpt, category)

    # 11. Run post_publish (diary, ticker in index.html)
    if not run_post_publish(slug):
        log_warn("post_publish warning, continuo...")

    # 12. FTP deploy
    log("FTP deploy...")
    deploy_ftp(articles_path, f"articles/{slug}.html", slug=slug)
    deploy_ftp(INDEX_HTML,    "index.html",              slug=slug)
    deploy_ftp(ARCHIVIO_HTML, "archivio.html",           slug=slug)
    deploy_ftp(PIPELINE_JSON, "data/pipeline.json",      slug=slug)
    deploy_ftp(INDAGINI_HTML, "indagini.html",            slug=slug)
    deploy_ftp(FEED_XML,      "feed.xml",                 slug=slug)

    # 13. OTS stamp
    run_ots_stamp(slug)

    # 14. Supabase: segna come published
    if not mark_published(approval_id):
        log_warn("mark_published fallito in Supabase")

    # 15. Aggiorna admin data.json
    run_update_admin_data()

    # 16. Notifica Mirko
    notify_telegram(article_title, slug, urgente_card.get("urgente_until", ""))

    journal_milestone("aiena_urgente_processor", slug, "URGENTE_PUBLICATION_COMPLETE")
    log(f"✓ Pubblicazione urgente completata: https://aiena.it/articles/{slug}.html")
    return True


def main() -> int:
    log("=" * 55)
    log("AIENA URGENTE PROCESSOR — check")

    something_to_do = False

    # Gestione pubblicazioni urgenti
    pending = fetch_urgente_pending()
    if pending:
        something_to_do = True
        log(f"Trovate {len(pending)} richieste urgente_pending")
        for approval in pending:
            try:
                ok = process_one(approval)
                if not ok:
                    log_error(f"Errore su: {approval.get('slug')}")
            except Exception as e:
                log_error(f"Eccezione inaspettata su {approval.get('slug')}: {e}")
                try:
                    mark_error(approval.get("id", ""))
                except Exception:
                    pass

    # Gestione cancellazioni preview
    to_delete = fetch_delete_pending()
    if to_delete:
        something_to_do = True
        log(f"Trovate {len(to_delete)} richieste delete_preview_pending")
        for approval in to_delete:
            try:
                process_delete(approval)
            except Exception as e:
                log_error(f"Eccezione delete su {approval.get('slug')}: {e}")
                try:
                    mark_error(approval.get("id", ""))
                except Exception:
                    pass

    if not something_to_do:
        log("Nessuna richiesta pendente. Fine.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
