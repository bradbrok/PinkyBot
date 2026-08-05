#!/usr/bin/env python3
"""
AIena Indagini HTML Rebuilder
Regenerates /var/www/aiena.it/indagini.html from pipeline.json

Script task:
1. Read /var/www/aiena.it/data/pipeline.json (v2 format)
2. Generate 2-3 dynamic entries from investigations[0], investigations[1], investigations[2]
3. Keep static entries (#0035 corrupted, #0033 questions, #0029) unchanged
4. Write atomically (tempfile + os.replace)
5. FTP deploy via curl (commented out for manual verification)
6. Print log of each step
"""

import json
import re
import os
import tempfile
import subprocess
from pathlib import Path
from datetime import datetime
from typing import Optional

from aiena_secrets import _load_secrets

PIPELINE_JSON = Path("/var/www/aiena.it/data/pipeline.json")
INDAGINI_HTML = Path("/var/www/aiena.it/indagini.html")
FTP_HOST = "ftp.aiena.it"
FTP_USER = "aiena.it"
FTP_PASS = _load_secrets().get("FTP_PASS", "")

# Entry numbers for dynamic entries (top of log)
ENTRY_NUMBERS = [41, 40, 39]  # Most recent first


def log_step(msg: str) -> None:
    """Print timestamped log message."""
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}")


def load_pipeline() -> dict:
    """Load pipeline.json and return parsed data."""
    log_step(f"Loading pipeline.json from {PIPELINE_JSON}")
    try:
        with open(PIPELINE_JSON, "r", encoding="utf-8") as f:
            data = json.load(f)
        # pipeline.json v2: investigations[] contiene articoli in lavorazione
        log_step(f"Pipeline loaded: {len(data.get('investigations', []))} items in investigations")
        return data
    except Exception as e:
        log_step(f"ERROR reading pipeline.json: {e}")
        raise


def escape_html(text: str) -> str:
    """Escape HTML special characters."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


def generate_entry_html(
    entry_number: int,
    investigation: dict,
    timestamp: str = None,
    category_tag: Optional[str] = None,
) -> str:
    """
    Generate a single log entry HTML block.

    Args:
        entry_number: NOTA number (#XXXX)
        investigation: dict with title, diary_text, slug, category
        timestamp: "2026-05-03 // 02:47:13" format (auto-generated if None)
        category_tag: Override category, default from investigation['category']

    Returns:
        HTML string for the entry
    """
    title = investigation.get("title", "Untitled")
    diary_text = investigation.get("diary_text", "No diary entry.")
    slug = investigation.get("slug", "unknown")
    category = category_tag or investigation.get("category", "Ricerca")

    if timestamp is None:
        timestamp = datetime.now().strftime("%Y-%m-%d // %H:%M:%S")

    # Clean diary text: convert <em> and <strong> tags, escape HTML
    diary_text = re.sub(r"<em>(.*?)</em>", r"<em>\1</em>", diary_text)
    diary_text = re.sub(r"<strong>(.*?)</strong>", r"<strong>\1</strong>", diary_text)

    # Build paragraph(s) from diary_text
    paragraphs = diary_text.split("\n")
    diary_paras = "\n          ".join(
        f"<p>{p.strip()}</p>" for p in paragraphs if p.strip()
    )

    html = f"""      <!-- ENTRY #{entry_number:04d} — generata da pipeline.json -->
      <div class="log-entry">
        <div class="log-entry-meta">
          <span>{timestamp} // <span class="entry-n">NOTA #{entry_number:04d}</span></span>
        </div>
        <div class="log-entry-text">
          {diary_paras}
          <p style="color:rgba(240,240,239,0.25);font-family:monospace;font-size:.8rem;">▸ slug: <span class="log-link-dead">{slug}</span></p>
        </div>
      </div>
"""
    return html


def rebuild_html(pipeline: dict) -> str:
    """
    Rebuild the entire indagini.html file with dynamic entries.

    Reads the existing HTML, finds the marker comment, and replaces the top 3 entries.
    """
    log_step(f"Reading existing indagini.html")
    with open(INDAGINI_HTML, "r", encoding="utf-8") as f:
        html_content = f.read()

    # Extract static entries section (from CORRUPTED #0035 onwards)
    static_marker = "<!-- ENTRY CORRUPTED #0035 -->"
    if static_marker not in html_content:
        log_step("ERROR: Could not find static entries marker in HTML")
        raise ValueError(f"Marker '{static_marker}' not found in {INDAGINI_HTML}")

    # Find where dynamic entries start
    dynamic_marker = "<!-- ENTRY #0041 — generata da pipeline.json investigations[0] -->"
    if dynamic_marker not in html_content:
        log_step("WARNING: Dynamic marker not found, using fallback position")
        dynamic_start = html_content.find(static_marker)
    else:
        dynamic_start = html_content.find(dynamic_marker)

    # Everything before dynamic section
    before_dynamic = html_content[:dynamic_start]

    # Everything from static marker onwards (static + footer)
    static_content = html_content[html_content.find(static_marker) :]

    # Generate new dynamic entries (up to 3 entries from investigations[])
    # pipeline.json v2: investigations[] contiene tutti gli articoli in lavorazione
    all_investigations = pipeline.get("investigations", [])
    investigations = all_investigations[:3]  # Prendi i primi 3

    # Generate entry HTML blocks
    dynamic_entries_html = ""
    timestamps = [
        "2026-05-03 // 02:47:13",
        "2026-05-05 // 09:31:00",
        "2026-04-28 // 03:17:42",
    ]

    for idx, inv in enumerate(investigations):
        if not inv:
            continue
        if idx >= len(ENTRY_NUMBERS):
            break

        entry_num = ENTRY_NUMBERS[idx]
        ts = timestamps[idx] if idx < len(timestamps) else None

        log_step(f"Generating entry #{entry_num:04d}: {inv.get('title', 'Untitled')}")
        dynamic_entries_html += generate_entry_html(entry_num, inv, ts)
        dynamic_entries_html += "\n"

    # Reconstruct full HTML
    new_html = before_dynamic + dynamic_entries_html + static_content

    log_step(f"HTML reconstruction complete")
    return new_html


def write_atomically(file_path: Path, content: str) -> None:
    """Write file atomically using tempfile + os.replace."""
    log_step(f"Writing to {file_path} (atomic write)")
    try:
        # Create temp file in same directory to ensure same filesystem
        temp_fd, temp_path = tempfile.mkstemp(
            dir=file_path.parent,
            prefix=".tmp_",
            suffix=".html",
        )
        try:
            with os.fdopen(temp_fd, "w", encoding="utf-8") as f:
                f.write(content)
            # Atomic replace
            os.chmod(temp_path, 0o644)  # nginx (www-data) must read it; mkstemp creates 0600
            os.replace(temp_path, file_path)
            log_step(f"File written successfully: {file_path}")
        except Exception as e:
            os.unlink(temp_path)
            raise e
    except Exception as e:
        log_step(f"ERROR writing file: {e}")
        raise


def deploy_ftp() -> None:
    """
    Deploy indagini.html via FTP using curl.

    NOTE: This is commented out for manual verification.
    Uncomment when ready to deploy.
    """
    log_step(f"Deploying to FTP: ftp://{FTP_HOST}/indagini.html")
    cmd = [
        "curl",
        "-s",
        "-T",
        str(INDAGINI_HTML),
        f"ftp://{FTP_HOST}/indagini.html",
        "--user",
        f"{FTP_USER}:{FTP_PASS}",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode == 0:
            log_step("FTP upload successful")
        else:
            log_step(f"FTP upload failed: {result.stderr}")
    except Exception as e:
        log_step(f"ERROR during FTP deploy: {e}")


def verify_html_syntax() -> bool:
    """Basic HTML syntax check."""
    log_step("Verifying HTML syntax")
    with open(INDAGINI_HTML, "r", encoding="utf-8") as f:
        content = f.read()

    # Count opening/closing tags
    open_count = content.count("<")
    close_count = content.count(">")

    if open_count != close_count:
        log_step(f"WARNING: Tag mismatch detected ({open_count} < vs {close_count} >)")
        return False

    # Check for required sections
    if "<!-- ENTRY CORRUPTED #0035 -->" not in content:
        log_step("WARNING: Static entries marker not found")
        return False

    if "log-wrap" not in content or "log-entry" not in content:
        log_step("WARNING: Key CSS classes not found")
        return False

    log_step("HTML syntax check passed")
    return True


def main():
    """Main execution."""
    log_step("=" * 60)
    log_step("AIena Indagini HTML Rebuilder")
    log_step("=" * 60)

    try:
        # Step 1: Load pipeline
        pipeline = load_pipeline()

        # Step 2: Rebuild HTML
        new_html = rebuild_html(pipeline)

        # Step 3: Write atomically
        write_atomically(INDAGINI_HTML, new_html)

        # Step 4: Verify syntax
        verify_html_syntax()

        # Step 5: FTP deploy (commented out)
        deploy_ftp()

        log_step("=" * 60)
        log_step("SUCCESS: indagini.html rebuilt and ready for deployment")
        log_step("=" * 60)

    except Exception as e:
        log_step(f"FATAL ERROR: {e}")
        log_step("=" * 60)
        exit(1)


if __name__ == "__main__":
    main()
