#!/usr/bin/env python3
"""
testimone_memory_backup.py — Backup automatico risposte Valentina nel DB alter-ego.

Gira ogni 10 minuti via cron. Legge i messaggi di Valentina dalla chat-history
API del broker (agent: testimone) e li salva via HTTP API (interview-response endpoint) —
INDIPENDENTEMENTE da se l'agente testimone ha chiamato ingest_memory.

Log: /home/pinky/.pinkybot/scripts/testimone_memory_backup.log
"""

import json
import os
import sqlite3
import urllib.request
from datetime import datetime
from pathlib import Path

# cron bootstrap: cron non eredita PINKY_SESSION_SECRET dall'ambiente del daemon.
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
MEMORIES_API = "http://127.0.0.1:7778/memory/interview-response"
MEMORIES_DB = "/home/pinky/projects/alter-ego/data/memories.db"
TRACKER_FILE = "/home/pinky/.pinkybot/scripts/testimone_backup_state.json"
LOG_PATH = "/home/pinky/.pinkybot/scripts/testimone_memory_backup.log"
VALENTINA_CHAT_ID = "565110333"
AGENT = "testimone"
AGENTS_DB = "/home/pinky/.pinkybot/data/conversations_agents.db"
BACKUP_AGENT = "satoshi"  # caller identity for HMAC auth


def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    with open(LOG_PATH, "a") as f:
        f.write(line + "\n")


def _auth_headers(method: str, path: str) -> dict:
    """Build HMAC-signed internal auth headers."""
    try:
        import sys
        sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
        from pinky_daemon.auth import build_internal_auth_headers
        secret = os.environ.get("PINKY_SESSION_SECRET", "")
        if secret:
            return build_internal_auth_headers(secret, agent_name=BACKUP_AGENT, method=method, path=path)
    except Exception:
        pass
    return {}


def agent_exists() -> bool:
    """Controlla se l'agente AGENT esiste nel DB prima di fare chiamate API."""
    try:
        conn = sqlite3.connect(AGENTS_DB)
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM agents WHERE name=?", (AGENT,))
        exists = cur.fetchone() is not None
        conn.close()
        return exists
    except Exception:
        return False


def load_state():
    if Path(TRACKER_FILE).exists():
        with open(TRACKER_FILE) as f:
            return json.load(f)
    return {"last_msg_id": 0, "saved_msg_ids": []}


def save_state(state):
    with open(TRACKER_FILE, "w") as f:
        json.dump(state, f)


def get_chat_history(limit=500):
    try:
        path = f"/agents/{AGENT}/chat-history?limit={limit}"
        url = f"{API}{path}"
        headers = _auth_headers("GET", path)
        req = urllib.request.Request(url, headers=headers, method="GET")
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read()).get("messages", [])
    except Exception as e:
        log(f"  ERROR get_chat_history: {e}")
        return []


def extract_valentina_content(raw_content):
    """Estrae il testo pulito dal messaggio broker di Valentina."""
    lines = raw_content.strip().split("\n")
    cleaned = []
    for line in lines:
        if line.startswith("[telegram |") and VALENTINA_CHAT_ID in line:
            continue  # header
        if line.startswith("💬 Reply on telegram"):
            break
        cleaned.append(line)
    return "\n".join(cleaned).strip()


def detect_category(content):
    """Rileva categoria dal contenuto con keyword matching. Default: relationships."""
    content_lower = content.lower()

    # Relationships (default for Valentina)
    if any(kw in content_lower for kw in ["mamma", "papà", "famiglia", "valentina", "amici", "mirko", "amore", "amore"]):
        return "relationships"

    # Opinions / Tech
    if any(kw in content_lower for kw in ["bitcoin", "crypto", "ai", "tecnologia", "progetto", "lavoro"]):
        return "opinions"

    # Memories
    if any(kw in content_lower for kw in ["ricordo", "quando ero", "anni fa", "2012", "2013", "2014", "2015"]):
        return "memories"

    # Values
    if any(kw in content_lower for kw in ["penso", "credo", "libertà", "valore", "rispetto"]):
        return "values"

    # Default per Valentina
    return "relationships"


def already_saved(state, msg_id):
    """Checks if msg_id is in the saved_msg_ids set (primary check)."""
    return msg_id in state.get("saved_msg_ids", [])


def already_saved_in_db(cur, msg_id):
    """Secondary check: looks up in SQLite as fallback."""
    cur.execute(
        "SELECT id FROM episodic_memories WHERE source_id=?",
        (f"valentina_{msg_id}",)
    )
    return cur.fetchone() is not None


def save_message_via_api(content):
    """Salva il messaggio via HTTP API."""
    category = detect_category(content)
    payload = {
        "content": f"[Valentina su Mirko] {content}",
        "category": category,
        "source": "valentina",
        "question": ""
    }

    try:
        req = urllib.request.Request(
            MEMORIES_API,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            resp = json.loads(r.read())
            return True, resp
    except Exception as e:
        return False, str(e)


def save_message_fallback(cur, msg_id, content, timestamp):
    """Fallback: inserisce direttamente in episodic_memories."""
    dt = datetime.fromtimestamp(timestamp)
    occurred_at = dt.strftime("%Y-%m-%dT%H:%M:%S")
    remembered_at = datetime.now().isoformat()

    cur.execute("""
        INSERT INTO episodic_memories
            (content, occurred_at, remembered_at, time_precision, importance,
             source, source_id, tags, people_involved)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        f"[Valentina su Mirko] {content}",
        occurred_at,
        remembered_at,
        "minute",
        0.75,
        "valentina",
        f"valentina_{msg_id}",
        "valentina,testimonianza,relazioni,backup_automatico",
        "Valentina"
    ))


def main():
    # Salta silenziosamente se l'agente non è registrato — evita 401/404 continui
    if not agent_exists():
        log(f"SKIP: agente '{AGENT}' non trovato nel DB — nessuna chat-history da backuppare")
        return

    state = load_state()
    last_id = state["last_msg_id"]
    saved_msg_ids = set(state.get("saved_msg_ids", []))

    messages = get_chat_history(limit=500)
    if not messages:
        log("Nessun messaggio dalla chat-history API.")
        return

    # Filtra: solo user messages di Valentina, più recenti dell'ultimo salvato
    new_msgs = [
        m for m in messages
        if m.get("role") == "user"
        and VALENTINA_CHAT_ID in m.get("content", "")
        and "Heartbeat" not in m.get("content", "")
        and m["id"] > last_id
    ]

    if not new_msgs:
        log(f"Nessun nuovo messaggio di Valentina (last_id={last_id}).")
        return

    conn = sqlite3.connect(MEMORIES_DB)
    cur = conn.cursor()

    saved = 0
    skipped = 0
    max_id = last_id

    for msg in sorted(new_msgs, key=lambda x: x["id"]):
        msg_id = msg["id"]
        raw_content = msg.get("content", "")
        content = extract_valentina_content(raw_content)

        if not content or len(content) < 3:
            skipped += 1
            continue

        # Primary check: state-based dedup
        if msg_id in saved_msg_ids:
            skipped += 1
            max_id = max(max_id, msg_id)
            continue

        # Secondary check: SQLite fallback
        if already_saved_in_db(cur, msg_id):
            skipped += 1
            saved_msg_ids.add(msg_id)
            max_id = max(max_id, msg_id)
            continue

        # Try API first
        success, result = save_message_via_api(content)
        if success:
            saved += 1
            saved_msg_ids.add(msg_id)
            max_id = max(max_id, msg_id)
            log(f"  SAVED (API) id={msg_id}: {content[:80]}")
        else:
            # Fallback to SQLite
            log(f"  API failed ({result}), using SQLite fallback for id={msg_id}")
            try:
                save_message_fallback(cur, msg_id, content, msg["timestamp"])
                saved += 1
                saved_msg_ids.add(msg_id)
                max_id = max(max_id, msg_id)
                log(f"  SAVED (SQLite) id={msg_id}: {content[:80]}")
            except Exception as e:
                log(f"  FAILED id={msg_id}: {e}")
                skipped += 1

    conn.commit()
    conn.close()

    # Aggiorna stato con saved_msg_ids
    if max_id > last_id:
        state["last_msg_id"] = max_id
        state["saved_msg_ids"] = list(saved_msg_ids)
        save_state(state)

    if saved > 0:
        log(f"BACKUP VALENTINA: salvati {saved} messaggi (saltati {skipped}).")
    else:
        log(f"BACKUP VALENTINA: tutto già salvato (saltati {skipped}).")


if __name__ == "__main__":
    main()
