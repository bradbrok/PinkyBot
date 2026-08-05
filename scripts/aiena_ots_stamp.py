#!/usr/bin/env python3
"""
AIena OpenTimestamps stamper — usa il CLI 'ots' ufficiale.
Hash il file HTML (senza il bc-box), fa ots stamp, carica il .ots via FTP.
Da eseguire ad ogni nuovo articolo pubblicato.
"""
import ftplib, hashlib, json, pathlib, re, subprocess, sys, shutil
from datetime import datetime, timezone

from aiena_secrets import _load_secrets

ARTICLES_DIR = pathlib.Path("/var/www/aiena.it/articles")

def build_articles_list():
    """Scopre automaticamente tutti gli articoli pubblicati in articles/."""
    articles = []
    if ARTICLES_DIR.exists():
        for f in sorted(ARTICLES_DIR.glob("*.html")):
            slug = f.stem
            articles.append({
                "slug": slug,
                "file": str(f),
                "ftp_article_path": f"articles/{f.name}",
            })
    return articles

ARTICLES = build_articles_list()

FTP_HOST = "ftp.aiena.it"
FTP_USER = "aiena.it"
FTP_PASS = _load_secrets().get("FTP_PASS", "")

# OTS CLI — percorso assoluto per compatibilità cron (venv non nel PATH)
OTS_BIN = pathlib.Path("/home/pinky/.pinkybot/.venv/bin/ots")

STATE_FILE = pathlib.Path("/home/pinky/.pinkybot/data/aiena_ots_stamps.json")
OTS_DIR    = pathlib.Path("/home/pinky/.pinkybot/data/aiena_ots/")
TMP_DIR    = pathlib.Path("/tmp/aiena_ots_work")


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def save_state(state):
    OTS_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


def extract_article_content(html: str) -> str:
    """Extract plain text of the journalistic content only (no HTML tags).
    Immune to ANY technical change: CSS, JS, nav, attributes, class names.
    Only the actual words of the investigation affect the hash.

    Algorithm (must mirror the JS verifyIntegrity() exactly):
    1. Extract <article>...</article>
    2. Remove bc-box (<!-- BLOCKCHAIN INTEGRITY ... <!-- /BC-BOX -->)
    3. Strip all HTML tags with regex
    4. Normalise whitespace to single spaces
    """
    # 1. Extract <article>
    m = re.search(r'<article[\s\S]*?</article>', html, flags=re.DOTALL)
    content = m.group(0) if m else html
    # 2. Remove bc-box
    if '<!-- /BC-BOX -->' in content:
        start = content.index('<!-- BLOCKCHAIN INTEGRITY')
        end   = content.index('<!-- /BC-BOX -->') + len('<!-- /BC-BOX -->')
        content = content[:start] + content[end:]
    else:
        content = re.sub(
            r'<!-- BLOCKCHAIN INTEGRITY.*?</div>\s*\n\s*</div>',
            '',
            content,
            flags=re.DOTALL
        )
    # 3. Strip HTML tags
    content = re.sub(r'<[^>]+>', ' ', content)
    # 4. Normalise whitespace
    content = re.sub(r'\s+', ' ', content).strip()
    return content


def strip_bc_box(html: str) -> str:
    """Alias mantenuto per compatibilità — usa extract_article_content."""
    return extract_article_content(html)


def hash_article(html_path: str) -> tuple[bytes, str]:
    """Hash the article content (without bc-box). Returns (bytes, hex)."""
    html = pathlib.Path(html_path).read_text(encoding="utf-8")
    clean = strip_bc_box(html)
    digest = hashlib.sha256(clean.encode("utf-8")).digest()
    return digest, digest.hex()


def stamp_article(html_path: str, slug: str) -> pathlib.Path | None:
    """Use 'ots stamp' on a clean copy of the article. Returns .ots path or None."""
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    tmp_file = TMP_DIR / f"{slug}.html"

    # Write plain-text content (no HTML) to temp — mirrors extract_article_content()
    html = pathlib.Path(html_path).read_text(encoding="utf-8")
    clean = extract_article_content(html)
    tmp_file.write_text(clean, encoding="utf-8")

    ots_tmp = pathlib.Path(str(tmp_file) + ".ots")
    if ots_tmp.exists():
        ots_tmp.unlink()

    result = subprocess.run(
        [str(OTS_BIN), "stamp", str(tmp_file)],
        capture_output=True, text=True, timeout=60
    )
    print(result.stdout.strip())
    if result.returncode != 0 or not ots_tmp.exists():
        print(f"  ots stamp failed: {result.stderr}")
        return None

    return ots_tmp


def upload_ots(ots_path: pathlib.Path, ots_name: str):
    """Upload .ots file to FTP assets/ots/."""
    try:
        ftp = ftplib.FTP(FTP_HOST)
        ftp.login(FTP_USER, FTP_PASS)
        try:
            ftp.mkd("assets/ots")
        except Exception:
            pass
        with open(ots_path, "rb") as f:
            ftp.storbinary(f"STOR assets/ots/{ots_name}", f)
        ftp.quit()
        print(f"  ✓ FTP: assets/ots/{ots_name}")
    except Exception as e:
        print(f"  FTP error: {e}")


def build_history_html(history: list) -> str:
    """Build the bc-history section HTML from history list."""
    if not history:
        return ""
    items = ""
    for entry in history:
        h = entry.get("hash", "")[:16] + "…"
        date = entry.get("date", "")
        note = entry.get("note", "")
        items += f"""          <div class="bc-history-item">
            <span class="bc-history-hash">{h}</span>
            <span class="bc-history-date">{date}</span>
            <span class="bc-history-note">{note}</span>
          </div>\n"""
    return f"""        <div class="bc-history" id="bc-history">
          <span class="bc-history-label">// versioni precedenti</span>
{items}        </div>"""


def update_article_bc_box(html_path: str, hash_hex: str, ots_name: str, history: list | None = None) -> bool:
    """Inject/update the bc-box with the correct hash and .ots link."""
    html = pathlib.Path(html_path).read_text(encoding="utf-8")
    short = hash_hex[:8]

    bc_box = f"""      <!-- BLOCKCHAIN INTEGRITY — OpenTimestamps -->
      <div class="bc-box" id="bc-box">
        <div class="bc-header">
          <div class="bc-header-left">
            <div class="bc-dot"></div>
            <span class="bc-title">Integrità — Proof of Existence</span>
          </div>
          <div class="bc-chips">
            <span class="bc-chip">⛓ Blockchain</span>
            <span class="bc-chip">OpenTimestamps</span>
          </div>
          <span class="bc-toggle-arrow">▾</span>
        </div>
        <!-- Hash visualizer: 32 colored blocks, one per byte -->
        <div class="bc-hashvis" id="bc-hashvis" aria-hidden="true"></div>
        <div class="bc-body">
          <div class="bc-field">
            <span class="bc-label">SHA-256</span>
            <span class="bc-hashtext">
              <span id="bc-hashval">{hash_hex}</span>
              <button class="bc-copy" onclick="navigator.clipboard.writeText('{hash_hex}').then(()=>{{this.textContent='✓';setTimeout(()=>this.textContent='⎘',1200)}})" title="Copia hash">⎘</button>
            </span>
          </div>
          <div class="bc-field">
            <span class="bc-label">Blockchain proof</span>
            <span class="bc-val"><span class="bc-status-dot"></span>Pendente conf. (~1h) — <a href="/assets/ots/{ots_name}" download="{ots_name}">Scarica .ots</a></span>
          </div>
          <div class="bc-field">
            <span class="bc-label">Certificato il</span>
            <span class="bc-val">{datetime.now(timezone.utc).strftime('%Y-%m-%d')} UTC — sistema AIena</span>
          </div>
        </div>
        <div class="bc-note">
          <span class="bc-note-text">Hash SHA-256 del testo (senza questo blocco). Verifica in-browser oppure: <code>ots verify {ots_name}</code></span>
          <div style="display:flex;gap:.5rem;flex-shrink:0;flex-wrap:wrap;">
            <button class="bc-info-btn" id="bc-verify-btn" onclick="verifyIntegrity()">⚡ verifica ora</button>
            <button class="bc-info-btn" id="bc-info-btn" onclick="var e=document.getElementById('bc-explain');e.classList.toggle('open');this.textContent=e.classList.contains('open')?'✕ chiudi':'? come funziona'">? come funziona</button>
          </div>
        </div>
        <div id="bc-verify-result" class="bc-verify-result"></div>
        <div class="bc-explain" id="bc-explain">
          <span class="bc-explain-title">// in parole semplici</span>
          <p class="bc-explain-text">
            Questo articolo ha un'<strong>impronta digitale unica</strong> — il codice sopra. È come un'impronta del pollice: cambia anche solo se viene modificata una virgola del testo.<br><br>
            Questa impronta è stata <strong>registrata sulla blockchain</strong>, che è un registro pubblico immutabile tenuto da migliaia di computer nel mondo. Nessuno — neanche AIena stessa — può cancellare o alterare questa registrazione.<br><br>
            Questo significa che <strong>la data di pubblicazione e il contenuto di questo articolo sono certificati</strong>. Se qualcuno in futuro sostenesse che l'articolo è stato modificato dopo la pubblicazione, chiunque può verificarlo.<br><br>
            <strong>Come verificare subito:</strong> clicca <em>⚡ verifica ora</em> — il tuo browser calcolerà l'impronta del testo direttamente sul tuo dispositivo, senza inviare nulla a nessun server, e la confronterà con quella registrata sulla blockchain.
          </p>
        </div>
{build_history_html(history or [])}
      </div>
      <!-- /BC-BOX -->"""

    # Replace existing bc-box (from comment to end marker) — robust, no regex nesting issues
    if '<!-- BLOCKCHAIN INTEGRITY' in html and '<!-- /BC-BOX -->' in html:
        start = html.index('<!-- BLOCKCHAIN INTEGRITY')
        end   = html.index('<!-- /BC-BOX -->') + len('<!-- /BC-BOX -->')
        html  = html[:start] + bc_box + html[end:]
    elif '<!-- BLOCKCHAIN INTEGRITY' in html:
        # Legacy: no end marker — use div-counting approach to find bc-box end
        # Support both <div class="bc-box"...> and <div id="bc-box" class="bc-box"...>
        comment_start = html.index('<!-- BLOCKCHAIN INTEGRITY')
        m = re.search(r'<div[^>]+class=["\']bc-box["\']', html[comment_start:])
        if not m:
            return False
        bc_div_start = comment_start + m.start()
        depth = 0
        i = bc_div_start
        while i < len(html):
            if html[i:i+4] == '<div': depth += 1
            elif html[i:i+6] == '</div>':
                depth -= 1
                if depth == 0:
                    html = html[:comment_start] + bc_box + html[i+6:]
                    break
            i += 1
    else:
        html = html.replace('    </div>\n  </article>', f'{bc_box}\n\n    </div>\n  </article>')

    # Update hash visualizer JS
    html = re.sub(
        r"const hash = '[0-9a-f]+'",
        f"const hash = '{hash_hex}'",
        html
    )

    # Inject .bc-verify-result CSS if not already present
    bc_verify_css = """    /* Blockchain in-browser verify */
    .bc-verify-result {
      font-family: var(--font-mono); font-size: .62rem; line-height: 1.6;
      padding: .45rem .9rem; margin: 0 1.25rem .5rem;
      border-radius: 3px; display: none;
    }
    .bc-verify-ok  { color: #22c55e; background: rgba(34,197,94,.06); border: 1px solid rgba(34,197,94,.2); }
    .bc-verify-warn{ color: #f59e0b; background: rgba(245,158,11,.06); border: 1px solid rgba(245,158,11,.2); }
    .bc-verify-err { color: #ff3a2e; background: rgba(255,58,46,.06);  border: 1px solid rgba(255,58,46,.2);  }"""
    bc_history_css = """    /* BC version history */
    .bc-history {
      border-top: 1px solid rgba(255,255,255,.04);
      padding: .5rem 1.25rem .65rem; display: none; flex-direction: column; gap: .3rem;
    }
    .bc-box.bc-open .bc-history { display: flex; }
    .bc-history-label {
      font-family: var(--font-mono) !important; font-size: .55rem !important;
      color: rgba(255,255,255,.2); letter-spacing: .08em; margin-bottom: .2rem;
    }
    .bc-history-item {
      display: flex; gap: .6rem; align-items: baseline; flex-wrap: wrap;
    }
    .bc-history-hash {
      font-family: var(--font-mono); font-size: .55rem; color: rgba(255,255,255,.2);
    }
    .bc-history-date {
      font-family: var(--font-mono); font-size: .55rem; color: rgba(255,255,255,.15);
    }
    .bc-history-note {
      font-family: var(--font-mono); font-size: .55rem; color: rgba(255,255,255,.25);
      font-style: italic;
    }"""
    if '.bc-history' not in html:
        html = html.replace('</style>', bc_history_css + '\n  </style>', 1)

    bc_toggle_css = """    /* BC box collapsible */
    .bc-header { cursor: pointer; transition: opacity .15s; }
    .bc-header:hover { opacity: .85; }
    .bc-toggle-arrow { font-size: .75rem; color: rgba(255,58,46,.5); transition: transform .2s; flex-shrink: 0; }
    .bc-box.bc-open .bc-toggle-arrow { transform: rotate(180deg); }
    .bc-hashvis, .bc-body, .bc-note, .bc-verify-result, .bc-explain { display: none; }
    .bc-box.bc-open .bc-hashvis { display: flex; }
    .bc-box.bc-open .bc-body { display: flex; }
    .bc-box.bc-open .bc-note { display: flex; }
    .bc-box.bc-open .bc-explain.open { display: block; }"""
    if '.bc-toggle-arrow' not in html:
        html = html.replace('</style>', bc_toggle_css + '\n  </style>', 1)

    if '.bc-verify-result' not in html:
        html = html.replace('</style>', bc_verify_css + '\n  </style>', 1)

    # Inject verifyIntegrity() JS if not already present
    bc_verify_js = """
    /* ── Blockchain in-browser verify (text-only, v2) ───────────────────── */
    async function verifyIntegrity() {
      const btn = document.getElementById('bc-verify-btn');
      const res = document.getElementById('bc-verify-result');
      btn.disabled = true;
      btn.textContent = '⟳ calcolo...';
      res.style.display = 'none';
      try {
        const resp = await fetch(window.location.href, { cache: 'no-store' });
        if (!resp.ok) throw new Error('Fetch fallito: HTTP ' + resp.status);
        const raw = await resp.text();
        // 1. Estrai <article>
        const artMatch = raw.match(/<article[\\s\\S]*?<\\/article>/);
        let content = artMatch ? artMatch[0] : raw;
        // 2. Rimuovi bc-box
        content = content.replace(/<!-- BLOCKCHAIN INTEGRITY[\\s\\S]*?<!-- \\/BC-BOX -->/, '');
        // 3. Strip tag HTML (identico a Python re.sub('<[^>]+>',' ',content))
        content = content.replace(/<[^>]+>/g, ' ');
        // 4. Normalizza spazi (identico a Python re.sub('\\s+',' ',content).strip())
        content = content.replace(/\\s+/g, ' ').trim();
        // 5. SHA-256 UTF-8
        const buf = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(content));
        const computed = Array.from(new Uint8Array(buf)).map(b => b.toString(16).padStart(2,'0')).join('');
        const stored = document.getElementById('bc-hashval').textContent.trim();
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
        res.style.display = 'block';
        res.className = 'bc-verify-result bc-verify-err';
        res.innerHTML = '✕ &nbsp;Errore: ' + err.message;
        btn.textContent = '⚡ verifica ora';
      }
      btn.disabled = false;
    }"""
    # Inject bc-box toggle JS
    bc_toggle_js = """
    /* ── Blockchain box toggle ───────────────────────────────────────────── */
    document.addEventListener('DOMContentLoaded', function() {
      var bcHeader = document.querySelector('.bc-header');
      if (bcHeader) {
        bcHeader.addEventListener('click', function() {
          document.getElementById('bc-box').classList.toggle('bc-open');
        });
      }
    });"""
    if 'bc-box toggle' not in html and '.bc-header' not in html.split('<script')[-1]:
        # Try double newline first, then single newline
        if '</script>\n\n</body>' in html:
            html = html.replace('</script>\n\n</body>', bc_toggle_js + '\n  </script>\n\n</body>', 1)
        elif '</script>\n</body>' in html:
            html = html.replace('</script>\n</body>', bc_toggle_js + '\n  </script>\n</body>', 1)
        else:
            # Fallback: inject before </body>
            html = html.replace('</body>', f'  <script>{bc_toggle_js}\n  </script>\n</body>', 1)

    if 'verifyIntegrity' not in html:
        html = html.replace('</script>\n\n</body>', bc_verify_js + '\n  </script>\n\n</body>', 1)
        # Fallback: inject before </body>
        if 'verifyIntegrity' not in html:
            html = html.replace('</body>', f'  <script>{bc_verify_js}\n  </script>\n</body>', 1)

    # Inject hamburger menu listener if missing (safety net for articles generated without inline JS)
    hamburger_js = """
    /* ── Hamburger menu ──────────────────────────────────────────────────── */
    (function() {
      var hb = document.getElementById('hamburger');
      var nv = document.getElementById('main-nav');
      if (hb && nv) {
        hb.addEventListener('click', function() {
          var open = nv.classList.toggle('open');
          hb.setAttribute('aria-expanded', open);
        });
      }
    })();"""
    if 'hamburger' in html and "getElementById('hamburger')" not in html and 'getElementById("hamburger")' not in html:
        html = html.replace('</body>', f'  <script>{hamburger_js}\n  </script>\n</body>', 1)

    pathlib.Path(html_path).write_text(html, encoding="utf-8")
    return True


def upload_article(html_path: str, ftp_path: str):
    try:
        ftp = ftplib.FTP(FTP_HOST)
        ftp.login(FTP_USER, FTP_PASS)
        # Navigate to directory
        parts = ftp_path.split("/")
        if len(parts) > 1:
            ftp.cwd(parts[0])
            ftp_name = parts[1]
        else:
            ftp_name = parts[0]
        with open(html_path, "rb") as f:
            ftp.storbinary(f"STOR {ftp_name}", f)
        ftp.quit()
        print(f"  ✓ FTP: {ftp_path}")
    except Exception as e:
        print(f"  FTP error: {e}")


def run(note: str = ""):
    OTS_DIR.mkdir(parents=True, exist_ok=True)
    state = load_state()
    changed = False

    for article in ARTICLES:
        slug = article["slug"]
        html_path = article["file"]

        if not pathlib.Path(html_path).exists():
            print(f"[{slug}] File not found")
            continue

        # Hash the content (without bc-box)
        _, hash_hex = hash_article(html_path)
        print(f"[{slug}] SHA-256 (no bc-box): {hash_hex}")

        # Skip if already stamped with same hash
        if state.get(slug, {}).get("hash") == hash_hex:
            print(f"[{slug}] Already stamped, skip.")
            continue

        # Move current hash to history before replacing
        existing = state.get(slug, {})
        history = existing.get("history", [])
        if existing.get("hash"):
            prev_entry = {
                "hash": existing["hash"],
                "date": existing.get("stamped_at", "")[:10],
                "note": note or "Aggiornamento"
            }
            history = [prev_entry] + history  # newest first

        # Stamp
        print(f"[{slug}] Stamping via ots CLI…")
        ots_tmp = stamp_article(html_path, slug)
        if not ots_tmp:
            print(f"[{slug}] ✗ Stamp failed")
            continue

        ots_name = f"aiena-{hash_hex[:8]}.ots"
        ots_dest = OTS_DIR / ots_name
        shutil.copy(ots_tmp, ots_dest)

        # Upload .ots to FTP
        upload_ots(ots_dest, ots_name)

        # Update article HTML with new hash, .ots link and version history
        update_article_bc_box(html_path, hash_hex, ots_name, history=history)

        # Upload updated article
        upload_article(html_path, article["ftp_article_path"])

        state[slug] = {
            "hash": hash_hex,
            "ots_file": ots_name,
            "ots_url": f"https://aiena.it/assets/ots/{ots_name}",
            "stamped_at": datetime.now(timezone.utc).isoformat(),
            "status": "pending",
            "history": history,
        }
        changed = True
        print(f"[{slug}] ✓ Done — {ots_name}")

    if changed:
        save_state(state)

    print("\n=== State ===")
    for slug, info in state.items():
        print(f"  {slug}: {info.get('hash','?')[:16]}… — {info.get('status')} — {info.get('stamped_at','')[:10]}")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="AIena OTS stamper")
    parser.add_argument("--note", default="", help="Nota descrittiva per questa versione (es. 'Correzione paragrafo 3')")
    parser.add_argument("--dry-run", action="store_true", help="Calcola hash senza stampare")
    args = parser.parse_args()
    if args.dry_run:
        state = load_state()
        for article in ARTICLES:
            _, h = hash_article(article["file"])
            prev = state.get(article["slug"], {}).get("hash", "nessuno")
            same = "✓ UGUALE" if h == prev else "≠ DIVERSO"
            print(f"[{article['slug']}] {same} — {h}")
    else:
        run(note=args.note)
