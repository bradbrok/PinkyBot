"""
aiena_secrets.py — Modulo condiviso per caricamento credenziali AIena.
Importare con: from aiena_secrets import _load_secrets
"""

import os
from pathlib import Path

_SECRETS_FILE = Path("/home/pinky/.pinkybot/scripts/.aiena_secrets")


def _load_secrets() -> dict:
    """Carica credenziali da .aiena_secrets o env vars."""
    secrets: dict = {}
    if _SECRETS_FILE.exists():
        for line in _SECRETS_FILE.read_text().splitlines():
            line = line.strip()
            if line and "=" in line and not line.startswith("#"):
                k, _, v = line.partition("=")
                secrets[k.strip()] = v.strip()
    for key in ("SB_SERVICE_KEY", "FTP_PASS", "BSKY_APP_PASS", "NOSTR_PRIVKEY",
                "SMTP_PASS", "SMTP_USER", "GITHUB_PAT", "TELEGRAM_BOT_TOKEN"):
        if os.environ.get(key):
            secrets[key] = os.environ[key]
    return secrets
