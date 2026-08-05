#!/usr/bin/env python3
"""
alter_ego_git_backup.py — Backup giornaliero automatico alter-ego su GitHub.
Esegue git add -A + commit + push sul repo ziomik/alter-ego.
.env e data/ sono già in .gitignore — nessun dato sensibile viene pushato.

Cron: ogni notte alle 02:15 (dopo aiena_git_backup alle 02:00)
"""
import subprocess
from datetime import date

REPO = "/home/pinky/projects/alter-ego"
TOKEN = "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"
REMOTE = f"https://{TOKEN}@github.com/ziomik/alter-ego.git"


def run(cmd, cwd=None):
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    return result.returncode == 0, result.stdout, result.stderr


def backup():
    today = date.today().isoformat()
    print(f"[{today}] Avvio backup alter-ego...")

    # Set remote with token auth
    run(["git", "remote", "set-url", "origin", REMOTE], cwd=REPO)

    # Stage all changes (respects .gitignore — .env e data/ esclusi)
    run(["git", "add", "-A"], cwd=REPO)

    # Check if anything changed
    ok, out, _ = run(["git", "status", "--porcelain"], cwd=REPO)
    if not out.strip():
        print("Nothing to commit — no changes.")
        return

    # Count changed files
    changed = len([l for l in out.strip().splitlines() if l.strip()])
    ok, _, err = run(
        ["git", "commit", "-m", f"backup automatico: {today} ({changed} file)"],
        cwd=REPO
    )
    if not ok:
        print("Commit failed:", err[:200])
        return

    ok, out, err = run(["git", "push", "origin", "main"], cwd=REPO)
    if ok:
        print(f"Backup completato: {today} ({changed} file pushati)")
    else:
        print("Push failed:", err[:200])


if __name__ == "__main__":
    backup()
