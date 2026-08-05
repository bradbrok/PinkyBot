#!/usr/bin/env python3
"""
aiena_git_backup.py — Backup giornaliero automatico su GitHub.
Copia site + scripts aggiornati nel repo aiena-sistema e fa commit+push.
"""
import subprocess
import shutil
import os
from datetime import date

REPO = "/home/pinky/aiena-sistema"
SITE_SRC = "/var/www/aiena.it"
SCRIPTS_SRC = "/home/pinky/.pinkybot/scripts"
TOKEN = "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"
REMOTE = f"https://{TOKEN}@github.com/ziomik/aiena-sistema.git"

def run(cmd, cwd=None):
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    return result.returncode == 0, result.stdout, result.stderr

def backup():
    print(f"[{date.today()}] Avvio backup aiena-sistema...")

    # Sync site files
    result = subprocess.run(
        ["rsync", "-a", "--delete",
         "--exclude=*.log", "--exclude=admin/data.json",
         f"{SITE_SRC}/", f"{REPO}/site/"],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        print("WARN rsync site:", result.stderr[:200])

    # Sync scripts
    scripts_dest = f"{REPO}/scripts"
    os.makedirs(scripts_dest, exist_ok=True)
    for f in os.listdir(SCRIPTS_SRC):
        if (f.startswith("aiena_") or f in ("email_monitor.py", "update_admin_data.py", "ftp_deploy_verify.sh")) and (f.endswith(".py") or f.endswith(".sh")):
            shutil.copy2(f"{SCRIPTS_SRC}/{f}", f"{scripts_dest}/{f}")

    # Git add
    run(["git", "add", "-A"], cwd=REPO)

    # Check if anything changed
    ok, out, _ = run(["git", "status", "--porcelain"], cwd=REPO)
    if not out.strip():
        print("Nothing to commit — no changes.")
        return

    today = date.today().isoformat()
    ok, _, err = run(["git", "commit", "-m", f"backup automatico: {today}"], cwd=REPO)
    if not ok:
        print("Commit failed:", err[:200])
        return

    # Set remote with token auth
    run(["git", "remote", "set-url", "origin", REMOTE], cwd=REPO)
    ok, out, err = run(["git", "push", "origin", "main"], cwd=REPO)
    if ok:
        print(f"Backup completato: {today}")
    else:
        print("Push failed:", err[:200])

if __name__ == "__main__":
    backup()
