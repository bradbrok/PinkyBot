#!/usr/bin/env python3
"""
aiena_housekeeping.py — cleanup file locali accumulati.

Eseguito settimanalmente da cron (domenica notte).
Elimina report files > 30 giorni e staging files già deployati > 7 giorni.
NON tocca FTP (articoli live restano per SEO).
"""
import os
import sys
from pathlib import Path
from datetime import datetime, timedelta, timezone

REPORTS_DIR = Path("/home/pinky/.pinkybot/scripts/reports")
STAGING_DIR = Path("/home/pinky/staging")
RETENTION_REPORTS_DAYS = 30
RETENTION_STAGING_DAYS = 7

def log(msg: str) -> None:
    print(f"[housekeeping] {datetime.now().strftime('%H:%M')} {msg}")


def cleanup_reports(dry_run: bool = False) -> int:
    """Delete report files older than RETENTION_REPORTS_DAYS."""
    if not REPORTS_DIR.exists():
        return 0
    cutoff = datetime.now(timezone.utc) - timedelta(days=RETENTION_REPORTS_DAYS)
    deleted = 0
    for f in REPORTS_DIR.glob("*.json"):
        mtime = datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc)
        if mtime < cutoff:
            if dry_run:
                log(f"DRY-RUN: would delete {f.name}")
            else:
                f.unlink()
                log(f"Deleted: {f.name}")
            deleted += 1
    for f in REPORTS_DIR.glob("*.txt"):
        mtime = datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc)
        if mtime < cutoff:
            if dry_run:
                log(f"DRY-RUN: would delete {f.name}")
            else:
                f.unlink()
                log(f"Deleted: {f.name}")
            deleted += 1
    return deleted


def cleanup_staging(dry_run: bool = False) -> int:
    """Delete staging files older than RETENTION_STAGING_DAYS."""
    if not STAGING_DIR.exists():
        return 0
    cutoff = datetime.now(timezone.utc) - timedelta(days=RETENTION_STAGING_DAYS)
    deleted = 0
    for f in STAGING_DIR.rglob("*.html"):
        mtime = datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc)
        if mtime < cutoff:
            if dry_run:
                log(f"DRY-RUN: would delete staging/{f.name}")
            else:
                f.unlink()
                log(f"Deleted staging: {f.name}")
            deleted += 1
    return deleted


def main():
    dry_run = "--dry-run" in sys.argv
    if dry_run:
        log("DRY-RUN MODE")

    log("=== AIENA HOUSEKEEPING START ===")
    r = cleanup_reports(dry_run)
    s = cleanup_staging(dry_run)
    log(f"Done: {r} reports deleted, {s} staging files deleted")
    log("=== AIENA HOUSEKEEPING END ===")


if __name__ == "__main__":
    main()
