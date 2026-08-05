#!/usr/bin/env python3
"""
agents_watchdog.py — Mantiene tutti gli agenti Telegram sempre attivi e correttamente configurati.

Controlla ogni 2 minuti (via cron):
1. allowed_tools include mcp__pinky-messaging__send per ogni agente con utenti TG
2. plain_text_fallback = 0 per testimone (non vuole fallback plain text)
3. Sessione streaming attiva per agenti che ne necessitano

Log: /home/pinky/.pinkybot/scripts/testimone_watchdog.log
"""

import json
import os
import sqlite3
import urllib.request
import urllib.error
from datetime import datetime
from pathlib import Path

# cron bootstrap: cron non eredita PINKY_SESSION_SECRET dall'ambiente del daemon
# → build_internal_auth_headers riceve secret="" → nessun header di firma → 401.
# Carichiamo il .env del daemon se PINKY_SESSION_SECRET è assente o vuoto.
_DAEMON_ENV = Path("/home/pinky/.pinkybot/.env")
if not os.environ.get("PINKY_SESSION_SECRET") and _DAEMON_ENV.exists():
    for _ln in _DAEMON_ENV.read_text().splitlines():
        _ln = _ln.strip()
        if _ln and "=" in _ln and not _ln.startswith("#"):
            _ek, _, _ev = _ln.partition("=")
            _k = _ek.strip()
            _v = _ev.strip().strip('"').strip("'")
            if not os.environ.get(_k):
                os.environ[_k] = _v

API = "http://localhost:8888"
WATCHDOG_AGENT = "satoshi"  # caller identity for HMAC auth
DB_PATH = "/home/pinky/.pinkybot/data/conversations_agents.db"
LOG_PATH = "/home/pinky/.pinkybot/scripts/testimone_watchdog.log"

# Agenti con utenti TG approvati che devono poter usare messaging
# formato: { agent_name: [tools_da_aggiungere_se_mancanti] }
MESSAGING_REQUIRED = {
    "testimone": ["mcp__pinky-messaging__send", "mcp__pinky-messaging__thread"],
    "sentinel":  ["mcp__pinky-messaging__send", "mcp__pinky-messaging__thread"],
    "seo-pro":   ["mcp__pinky-messaging__send", "mcp__pinky-messaging__thread"],
}

# Agenti che DEVONO avere una sessione streaming attiva (con utenti TG non-Mirko)
SESSION_REQUIRED = ["testimone"]

# Agenti con plain_text_fallback forzato a 0
FALLBACK_ZERO = ["testimone"]


def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    with open(LOG_PATH, "a") as f:
        f.write(line + "\n")


def _auth_headers(method: str, path: str) -> dict:
    """Build HMAC-signed internal auth headers."""
    try:
        from pinky_daemon.auth import build_internal_auth_headers
        secret = os.environ.get("PINKY_SESSION_SECRET", "")
        if secret:
            return build_internal_auth_headers(secret, agent_name=WATCHDOG_AGENT, method=method, path=path)
    except Exception:
        pass
    return {}


def api_post(path, data=None):
    try:
        body = json.dumps(data or {}).encode()
        headers = {"Content-Type": "application/json"}
        headers.update(_auth_headers("POST", path))
        req = urllib.request.Request(
            f"{API}{path}", data=body,
            headers=headers,
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())
    except Exception as e:
        log(f"  POST {path} failed: {e}")
        return None


def api_get(path):
    try:
        headers = _auth_headers("GET", path)
        req = urllib.request.Request(f"{API}{path}", headers=headers, method="GET")
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read())
    except Exception:
        return None


def check_and_fix_db():
    """Verifica e corregge allowed_tools e plain_text_fallback per tutti gli agenti."""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    issues = []

    for agent, required_tools in MESSAGING_REQUIRED.items():
        cur.execute("SELECT allowed_tools, plain_text_fallback FROM agents WHERE name=?", (agent,))
        row = cur.fetchone()
        if not row:
            log(f"  WARN: agente '{agent}' non trovato in DB")
            continue

        tools_json, fallback = row
        try:
            tools = json.loads(tools_json) if tools_json and tools_json != "[]" else []
        except Exception:
            tools = []

        changed = False
        # Se allowed_tools è vuoto [] = tutti permessi, non serve toccare
        # Intervieni solo se c'è una whitelist esplicita che esclude messaging
        if tools:  # whitelist attiva
            for t in required_tools:
                if t not in tools:
                    tools.append(t)
                    changed = True
                    log(f"  FIX [{agent}]: aggiunto tool: {t}")

            if changed:
                cur.execute("UPDATE agents SET allowed_tools=? WHERE name=?",
                            (json.dumps(tools), agent))
                issues.append(f"{agent}:tools_fixed")

        # Fix plain_text_fallback
        if agent in FALLBACK_ZERO and fallback != 0:
            cur.execute("UPDATE agents SET plain_text_fallback=0 WHERE name=?", (agent,))
            log(f"  FIX [{agent}]: plain_text_fallback -> 0")
            issues.append(f"{agent}:fallback_fixed")

    conn.commit()
    conn.close()
    return issues


def agent_exists_in_db(agent: str) -> bool:
    """Controlla se l'agente esiste nel DB prima di fare chiamate API."""
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM agents WHERE name=?", (agent,))
        exists = cur.fetchone() is not None
        conn.close()
        return exists
    except Exception:
        return False


def check_and_fix_sessions():
    """Verifica e ricrea sessioni streaming mancanti."""
    issues = []

    for agent in SESSION_REQUIRED:
        # Salta agenti non registrati nel DB — non ha senso fare chiamate API
        # per agenti che non esistono (genererebbero 404 o 401 rumorosi).
        if not agent_exists_in_db(agent):
            log(f"  SKIP [{agent}]: agente non trovato nel DB — rimuoverlo da SESSION_REQUIRED o registrarlo")
            continue

        resp = api_get(f"/agents/{agent}/streaming-sessions")
        # API returns {"agent": ..., "sessions": [...]} — extract the list
        if isinstance(resp, dict):
            sessions = resp.get("sessions", [])
        elif isinstance(resp, list):
            sessions = resp
        else:
            sessions = []
        if sessions:
            continue  # OK

        log(f"  ALERT [{agent}]: nessuna sessione streaming attiva — ricreo")
        result = api_post(f"/agents/{agent}/streaming-sessions", {"label": "default"})
        if result and result.get("created"):
            log(f"  OK [{agent}]: sessione ricreata")
            issues.append(f"{agent}:session_recreated")
        else:
            log(f"  ERROR [{agent}]: impossibile ricreare sessione: {result}")
            issues.append(f"{agent}:session_failed")

    return issues


def main():
    all_issues = []
    all_issues += check_and_fix_db()
    all_issues += check_and_fix_sessions()

    if all_issues:
        log(f"WATCHDOG: interventi effettuati: {all_issues}")
    else:
        log("WATCHDOG: ok — tutti gli agenti configurati correttamente")


if __name__ == "__main__":
    main()
