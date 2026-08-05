#!/usr/bin/env python3
"""
Rebuilds admin/index.html embedding current pipeline.json data inline.
Pipeline data is no longer fetched from a public URL — it's embedded at build time.
Admin HTML is password-protected; the JSON file stays local-only.

Usage: python3 aiena_admin_rebuild.py
Called by: aiena_post_publish.py, or manually after pipeline.json changes.
"""
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

PIPELINE_JSON = Path("/var/www/aiena.it/data/pipeline.json")
ADMIN_HTML = Path("/var/www/aiena.it/admin/index.html")
# Nginx webroot for admin.aiena.it (served locally, not via FTP)
ADMIN_NGINX_ROOT = Path("/var/www/aiena-admin/index.html")
FTP_HOST = "ftp.aiena.it"
FTP_USER_PLAIN = "aiena.it"


def _load_ftp_pass() -> str:
    """Load FTP password from .aiena_secrets (chmod 600) or env."""
    import os
    if os.environ.get("FTP_PASS"):
        return os.environ["FTP_PASS"]
    secrets_file = Path("/home/pinky/.pinkybot/scripts/.aiena_secrets")
    if secrets_file.exists():
        for line in secrets_file.read_text().splitlines():
            line = line.strip()
            if line.startswith("FTP_PASS="):
                return line.partition("=")[2].strip()
    return ""


FTP_USER = f"{FTP_USER_PLAIN}:{_load_ftp_pass()}"


def log(msg: str):
    now = datetime.now(ZoneInfo("Europe/Rome"))
    print(f"[admin-rebuild] {now.strftime('%H:%M')} {msg}")


def load_pipeline() -> dict:
    try:
        return json.loads(PIPELINE_JSON.read_text(encoding="utf-8"))
    except FileNotFoundError:
        log("ERROR: pipeline.json not found")
        return {"events": [], "articles": []}
    except json.JSONDecodeError as e:
        log(f"ERROR: pipeline.json is corrupted: {e}")
        return {"events": [], "articles": []}


def _render_event_row(ev, today) -> str:
    """Render a single event row as static HTML."""
    from datetime import datetime as _dt
    day_names = ['dom','lun','mar','mer','gio','ven','sab']
    # Date may carry a time component (e.g. "2026-06-01 10:11:56"); slice to date only
    ev_date = _dt.strptime(ev["date"][:10], "%Y-%m-%d").date()
    is_past = ev_date < today
    is_deadline = ev.get("deadline") is True
    day_num = ev_date.day
    dow = ev_date.isoweekday() % 7  # Sun=0,Mon=1..Sat=6
    day_name = day_names[dow]
    border_color = '#ff4444' if is_deadline else ('rgba(255,255,255,0.08)' if is_past else 'rgba(255,255,255,0.15)')
    dot_color = '#ff4444' if is_deadline else ('#555' if is_past else '#888')
    title_class = ' deadline-title' if is_deadline else ''
    day_color = '#ff4444' if is_deadline else '#fff'
    desc = ev.get("description", "")
    slug = ev.get("related_slug", "")
    icon = ev.get("icon", "📌")
    title = ev.get("title", "")
    slug_html = f'<div style="font-size:0.6rem;color:rgba(255,255,255,0.2);margin-top:0.3rem;">&#8594; {slug}</div>' if slug else ''
    return f"""<div class="event-row" style="border-left:2px solid {border_color};">
              <div class="event-date-col">
                <div style="font-size:1.1rem;font-weight:700;color:{day_color};">{day_num}</div>
                <div style="font-size:0.6rem;color:var(--text-muted);text-transform:uppercase;">{day_name}</div>
              </div>
              <div style="width:10px;height:10px;border-radius:50%;background:{dot_color};margin-top:0.3rem;flex-shrink:0;"></div>
              <div class="event-content">
                <div class="event-icon-title">
                  <span style="font-size:1rem;">{icon}</span>
                  <span class="event-title{title_class}">{title}</span>
                </div>
                <div class="event-desc">{desc}</div>
                {slug_html}
              </div>
            </div>"""


def render_events_html(events: list) -> str:
    """Pre-render ALL months as static HTML panels; JS just toggles visibility."""
    from datetime import date as _date
    today = _date.today()
    cur_year, cur_month = today.year, today.month  # 1-indexed

    month_names = ['Gennaio','Febbraio','Marzo','Aprile','Maggio','Giugno',
                   'Luglio','Agosto','Settembre','Ottobre','Novembre','Dicembre']

    if not events:
        return '<div class="section-title">Cronologia Workflow</div><div style="font-family:var(--font-mono);font-size:0.72rem;color:var(--text-muted);padding:1.5rem 0;">Nessun evento registrato.</div>'

    # Collect all unique year-month combos from events
    from collections import OrderedDict
    # FIX: filtra eventi con date malformate (es. "28 M..." invece di "YYYY-MM-DD")
    months_with_events = sorted(set(
        e["date"][:7] for e in events
        if e.get("date") and len(e["date"]) >= 7
           and e["date"][:4].isdigit() and e["date"][4] == '-' and e["date"][5:7].isdigit()
    ))
    # Add current month if not present
    cur_ym = f"{cur_year}-{str(cur_month).zfill(2)}"
    if cur_ym not in months_with_events:
        months_with_events.append(cur_ym)
        months_with_events.sort()

    btn_on = "background:rgba(255,255,255,0.12);color:#fff;border:none;padding:0.35rem 0.8rem;cursor:pointer;font-family:var(--font-mono);font-size:0.75rem;border-radius:3px;"
    btn_off = "background:rgba(255,255,255,0.05);color:rgba(255,255,255,0.2);border:none;padding:0.35rem 0.8rem;cursor:default;font-family:var(--font-mono);font-size:0.75rem;border-radius:3px;"

    # Build all panels
    panels_html = ""
    for idx, ym in enumerate(months_with_events):
        y, m = int(ym[:4]), int(ym[5:])
        is_current = (ym == cur_ym)
        display = "block" if is_current else "none"

        has_prev = idx > 0
        has_next = idx < len(months_with_events) - 1
        prev_ym = months_with_events[idx-1] if has_prev else ""
        next_ym = months_with_events[idx+1] if has_next else ""

        # Build events for this month
        month_events = [e for e in events if e.get("date","").startswith(ym)]
        month_events.sort(key=lambda e: e.get("date",""))

        if month_events:
            rows = "\n".join(_render_event_row(ev, today) for ev in month_events)
            events_content = f'<div class="events-timeline">\n{rows}\n</div>'
        else:
            events_content = '<div style="font-family:var(--font-mono);font-size:0.72rem;color:var(--text-muted);padding:1.5rem 0;">Nessun evento registrato questo mese.</div>'

        prev_btn = f'<button onclick="showEventsMonth(\'{prev_ym}\')" style="{btn_on}">&#8592; Prec</button>' if has_prev else f'<button disabled style="{btn_off}">&#8592; Prec</button>'
        next_btn = f'<button onclick="showEventsMonth(\'{next_ym}\')" style="{btn_on}">Succ &#8594;</button>' if has_next else f'<button disabled style="{btn_off}">Succ &#8594;</button>'

        panels_html += f"""<div id="events-panel-{ym}" data-ym="{ym}" style="display:{display};">
      <div style="display:flex;align-items:center;gap:1rem;margin-bottom:1.2rem;font-family:var(--font-mono);">
        {prev_btn}
        <span style="font-size:0.75rem;color:var(--text-muted);">{month_names[m-1]} {y}</span>
        {next_btn}
      </div>
      {events_content}
    </div>\n"""

    # Inline JS: just show/hide panels — no state, no dependencies
    nav_js = """<script>
    function showEventsMonth(ym) {
      document.querySelectorAll('[id^="events-panel-"]').forEach(function(el) {
        el.style.display = el.dataset.ym === ym ? 'block' : 'none';
      });
    }
    </script>"""

    return f'<div class="section-title">Cronologia Workflow</div>\n{nav_js}\n{panels_html}'.strip()


def rebuild_admin(deploy: bool = True):
    data = load_pipeline()
    data_json = json.dumps(data, ensure_ascii=False, separators=(',', ':'))

    html = ADMIN_HTML.read_text(encoding="utf-8")

    now_str = datetime.now(ZoneInfo("Europe/Rome")).strftime("%d/%m/%Y %H:%M")

    new_load = f"""    function loadPipeline() {{
      // Pipeline data embedded at build time — {now_str}
      // pipeline.json is NOT served publicly for security
      // AIENA-PIPELINE-START
      const data = {data_json};
      window._pipelineData = data;
      // AIENA-PIPELINE-END
      renderContent(data);
    }}"""

    # Strategy 1: sentinel-based replacement (fastest, most reliable)
    SENTINEL_START = "// AIENA-PIPELINE-START"
    SENTINEL_END = "// AIENA-PIPELINE-END"
    if SENTINEL_START in html and SENTINEL_END in html:
        # Find the full function block (from "function loadPipeline" to its closing "}")
        func_start = html.rfind("    function loadPipeline()", 0, html.find(SENTINEL_START))
        func_end = html.find("\n    }", html.find(SENTINEL_END)) + len("\n    }")
        if func_start != -1 and func_end > func_start:
            html = html[:func_start] + new_load + html[func_end:]
            log("Pipeline data embedded (sentinel update)")
        else:
            log("WARN: sentinel markers found but function boundaries unclear — falling back")
            return False

    # Strategy 2: original async fetch() pattern (first install)
    elif "async function loadPipeline()" in html:
        old_load = """    async function loadPipeline() {
      try {
        const res = await fetch('/data/pipeline.json?t=' + Date.now());
        const data = await res.json();
        renderContent(data);
      } catch (err) {
        document.getElementById('content-loading').textContent = 'Errore nel caricamento: ' + err.message;
      }
    }"""
        if old_load in html:
            html = html.replace(old_load, new_load)
            log("Pipeline data embedded (replaced fetch)")
        else:
            log("WARN: async loadPipeline found but exact match failed. Check admin HTML.")
            return False

    # Strategy 3: position-based (find function by proximity to renderContent)
    elif "function loadPipeline()" in html:
        func_marker = "    function loadPipeline() {"
        start_idx = html.find(func_marker)
        next_func_idx = html.find("    function renderContent(data)", start_idx)
        if start_idx != -1 and next_func_idx != -1:
            html = html[:start_idx] + new_load + "\n\n" + html[next_func_idx:]
            log("Pipeline data embedded (position-based replacement)")
        else:
            log("WARN: loadPipeline found but renderContent not found after it.")
            return False

    else:
        log("INFO: loadPipeline() not found — admin HTML uses loadData() pattern. Skipping pipeline embed, proceeding with deploy.")
        # Fall through: just deploy the HTML as-is (data served via data.json symlink)

    # Strategy: pre-render events section as static HTML (no JS dependency)
    events_html = render_events_html(data.get("events", []))
    EVENTS_SENTINEL_START = "<!-- AIENA-EVENTS-START -->"
    EVENTS_SENTINEL_END = "<!-- AIENA-EVENTS-END -->"
    if EVENTS_SENTINEL_START in html and EVENTS_SENTINEL_END in html:
        start_idx = html.find(EVENTS_SENTINEL_START) + len(EVENTS_SENTINEL_START)
        end_idx = html.find(EVENTS_SENTINEL_END)
        html = html[:start_idx] + "\n    " + events_html + "\n    " + html[end_idx:]
        log("Events section pre-rendered (sentinel update)")
    elif 'id="section-events">' in html:
        # Inject static HTML directly into empty section div
        html = html.replace(
            'id="section-events">',
            f'id="section-events">{EVENTS_SENTINEL_START}\n    {events_html}\n    {EVENTS_SENTINEL_END}'
        )
        log("Events section pre-rendered (first inject)")
    else:
        log("WARN: section-events not found — events NOT pre-rendered")

    tmp_fd, tmp_path = tempfile.mkstemp(dir=ADMIN_HTML.parent, suffix=".tmp")
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
            fh.write(html)
        os.chmod(tmp_path, 0o644)  # nginx (www-data) must read it; mkstemp creates 0600
        os.replace(tmp_path, ADMIN_HTML)
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
    log(f"Admin HTML updated: {ADMIN_HTML}")

    # Deploy to nginx webroot (admin.aiena.it is served locally from /var/www/aiena-admin/)
    if ADMIN_NGINX_ROOT.exists() or ADMIN_NGINX_ROOT.parent.exists():
        try:
            import shutil
            shutil.copy2(str(ADMIN_HTML), str(ADMIN_NGINX_ROOT))
            import subprocess as sp
            sp.run(["sudo", "chown", "www-data:www-data", str(ADMIN_NGINX_ROOT)], check=False)
            log(f"Deployed to nginx webroot: {ADMIN_NGINX_ROOT} ✓")
        except Exception as e:
            log(f"WARN: nginx webroot deploy failed: {e}")

    if deploy:
        result = subprocess.run(
            ["curl", "-s", "-T", str(ADMIN_HTML),
             f"ftp://{FTP_HOST}/admin/index.html",
             "--user", FTP_USER],
            capture_output=True, text=True
        )
        if result.returncode == 0:
            log("Deployed admin/index.html via FTP ✓")
        else:
            log(f"FTP deploy error: {result.stderr}")
            return False

    return True


if __name__ == "__main__":
    import sys
    deploy = "--no-deploy" not in sys.argv
    ok = rebuild_admin(deploy=deploy)
    sys.exit(0 if ok else 1)
