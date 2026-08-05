#!/usr/bin/env python3
"""
refresh_claude_token.py — Refresh Claude OAuth token automaticamente.

Uso:
  python3 refresh_claude_token.py                          # aggiorna ~/.claude/.credentials.json
  python3 refresh_claude_token.py --file /path/to/creds   # aggiorna file specifico
  python3 refresh_claude_token.py --check                  # solo controlla scadenza

Endpoint: https://console.anthropic.com/v1/oauth/token
Client ID: 9d1c250a-e61b-44d9-88ed-5944d1962f5e (Claude Code CLI)
"""

import json
import time
import urllib.request
import urllib.error
import sys
import os
import argparse
from pathlib import Path

OAUTH_ENDPOINT = "https://console.anthropic.com/v1/oauth/token"
CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
REFRESH_BEFORE_EXPIRY_SEC = 1800  # Refresh se scade entro 30 min


def load_credentials(path: str) -> dict:
    with open(path) as f:
        return json.load(f)


def save_credentials(path: str, data: dict):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)  # atomic swap


def get_inner(data: dict) -> dict:
    return data.get("claudeAiOauth", data)


def token_expires_in(data: dict) -> float:
    """Secondi alla scadenza del token (negativo = già scaduto)."""
    inner = get_inner(data)
    exp_ms = inner.get("expiresAt", 0)
    return exp_ms / 1000 - time.time()


def do_refresh(refresh_token: str) -> dict:
    """Chiama l'endpoint OAuth e restituisce la risposta."""
    payload = json.dumps({
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": CLIENT_ID,
    }).encode()

    headers = {
        "Content-Type": "application/json",
        "User-Agent": "claude-cli/2.1.96 node/20.0.0",
        "Accept": "application/json",
    }

    req = urllib.request.Request(OAUTH_ENDPOINT, data=payload, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def refresh_file(creds_path: str, force: bool = False) -> bool:
    """
    Controlla e aggiorna le credenziali nel file specificato.
    Ritorna True se il refresh è avvenuto, False se non necessario.
    """
    data = load_credentials(creds_path)
    inner = get_inner(data)
    refresh_token = inner.get("refreshToken")

    if not refresh_token:
        print(f"ERROR: nessun refreshToken in {creds_path}", file=sys.stderr)
        return False

    expires_in = token_expires_in(data)
    expires_fmt = time.strftime("%H:%M:%S", time.localtime(inner.get("expiresAt", 0) / 1000))

    if not force and expires_in > REFRESH_BEFORE_EXPIRY_SEC:
        print(f"OK: token valido fino alle {expires_fmt} ({expires_in/60:.0f} min). Nessun refresh necessario.")
        return False

    if expires_in < 0:
        print(f"WARN: token scaduto alle {expires_fmt}. Tentativo di refresh...")
    else:
        print(f"INFO: token scade alle {expires_fmt} ({expires_in/60:.0f} min). Refresh preventivo...")

    try:
        result = do_refresh(refresh_token)
    except urllib.error.HTTPError as e:
        body = e.read().decode()[:200]
        print(f"ERROR HTTP {e.code}: {body}", file=sys.stderr)
        return False
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return False

    now_ms = int(time.time() * 1000)
    expires_ms = now_ms + result["expires_in"] * 1000

    # Aggiorna il dict (supporta sia struttura flat che nested claudeAiOauth)
    if "claudeAiOauth" in data:
        data["claudeAiOauth"]["accessToken"] = result["access_token"]
        data["claudeAiOauth"]["expiresAt"] = expires_ms
        if "refresh_token" in result:
            data["claudeAiOauth"]["refreshToken"] = result["refresh_token"]
    else:
        data["accessToken"] = result["access_token"]
        data["expiresAt"] = expires_ms
        if "refresh_token" in result:
            data["refreshToken"] = result["refresh_token"]

    save_credentials(creds_path, data)
    new_exp = time.strftime("%H:%M:%S", time.localtime(expires_ms / 1000))
    print(f"SUCCESS: token aggiornato, scade alle {new_exp}")
    if "refresh_token" in result:
        print("INFO: nuovo refreshToken salvato (rotation)")
    return True


def main():
    parser = argparse.ArgumentParser(description="Refresh Claude OAuth token")
    parser.add_argument("--file", default=str(Path.home() / ".claude/.credentials.json"),
                        help="Path al file credentials (default: ~/.claude/.credentials.json)")
    parser.add_argument("--check", action="store_true", help="Solo controlla scadenza, non refreshare")
    parser.add_argument("--force", action="store_true", help="Forza refresh anche se non scaduto")
    args = parser.parse_args()

    if not os.path.exists(args.file):
        print(f"ERROR: file non trovato: {args.file}", file=sys.stderr)
        sys.exit(1)

    if args.check:
        data = load_credentials(args.file)
        inner = get_inner(data)
        exp_ms = inner.get("expiresAt", 0)
        expires_in = exp_ms / 1000 - time.time()
        exp_fmt = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(exp_ms / 1000))
        status = "SCADUTO" if expires_in < 0 else f"valido ({expires_in/60:.0f} min)"
        print(f"Token {status} — scade: {exp_fmt}")
        sys.exit(1 if expires_in < 0 else 0)

    refresh_file(args.file, force=args.force)


if __name__ == "__main__":
    main()
