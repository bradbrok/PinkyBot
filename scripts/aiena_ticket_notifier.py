#!/usr/bin/env python3
"""
AIena ticket notifier — controlla nuovi ticket su Supabase,
invia email a aiena@agentmail.to e notifica Mirko su Telegram.
Da lanciare ogni heartbeat (~5 min).
"""
import base64, hashlib, hmac, json, os, smtplib, tempfile, time, urllib.request
from datetime import datetime, timezone, timedelta
from email.mime.text import MIMEText
from pathlib import Path

from aiena_secrets import _load_secrets

SUPABASE_URL = "https://fwyjxolljcogblvwvfca.supabase.co"
ANON_KEY = "sb_publishable_Sszk3RxIjr1KgqTf9wqEAg_ZJzxrR_9"
SMTP_HOST = "mail.infomaniak.com"
SMTP_PORT = 465

# Lazy-load secrets to avoid crash if env/config unavailable at import time
_secrets_cache = None

def _get_secrets():
    global _secrets_cache
    if _secrets_cache is None:
        _secrets_cache = _load_secrets()
    return _secrets_cache

def _get_smtp_credentials():
    """Get SMTP credentials lazily."""
    s = _get_secrets()
    return s.get("SMTP_USER", "ziomik@etik.com"), s.get("SMTP_PASS", "")
NOTIFY_TO  = "aiena@agentmail.to"
TG_CHAT_ID = "32405655"

STATE_FILE = Path("/home/pinky/.pinkybot/data/aiena_notified_tickets.json")

def load_notified():
    if STATE_FILE.exists():
        return set(json.loads(STATE_FILE.read_text()))
    return set()

def save_notified(ids):
    tmp_fd, tmp_path = tempfile.mkstemp(dir=STATE_FILE.parent, suffix=".tmp")
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(list(ids)))
        os.replace(tmp_path, STATE_FILE)
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)

def fetch_recent_tickets():
    since = (datetime.now(timezone.utc) - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
    url = f"{SUPABASE_URL}/rest/v1/tickets?created_at=gte.{since}&select=id,ticket_code,tipo,titolo,descrizione,email,created_at&order=created_at.desc"
    req = urllib.request.Request(url, headers={
        "apikey": ANON_KEY,
        "Authorization": f"Bearer {ANON_KEY}"
    })
    res = urllib.request.urlopen(req, timeout=10)
    return json.loads(res.read())

def send_notification(ticket):
    tipo = ticket.get('tipo','—')
    code = ticket.get('ticket_code','—')
    titolo = ticket.get('titolo','(senza titolo)')
    desc = (ticket.get('descrizione') or '')[:500]
    email_utente = ticket.get('email') or '(anonimo)'
    created = ticket.get('created_at','')[:16].replace('T',' ')

    body = f"""📬 NUOVA SEGNALAZIONE — AIena.it

Ticket: {code}
Tipo: {tipo}
Titolo: {titolo}
Email: {email_utente}
Ricevuta: {created} UTC

---
{desc}

---
Gestisci: https://aiena.it/ticket.html?ticket={code}
"""
    msg = MIMEText(body, 'plain', 'utf-8')
    msg['Subject'] = f"[AIena-Ticket] {code} — {titolo[:50]}"

    # Get SMTP credentials lazily
    smtp_user, smtp_pass = _get_smtp_credentials()
    msg['From'] = smtp_user
    msg['To'] = NOTIFY_TO

    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=15) as server:
        server.login(smtp_user, smtp_pass)
        server.sendmail(smtp_user, [NOTIFY_TO], msg.as_string())

def _broker_headers(method: str, path: str, agent: str = "aiena") -> dict:
    """Build HMAC-signed headers for /broker/* endpoints (mirrors aiena_auto_publish pattern)."""
    secret = os.environ.get("PINKY_SESSION_SECRET", "")
    if not secret:
        env_file = Path("/home/pinky/.pinkybot/.env")
        if env_file.exists():
            for line in env_file.read_text().splitlines():
                if line.startswith("PINKY_SESSION_SECRET="):
                    secret = line.split("=", 1)[1].strip().strip('"').strip("'")
                    break
    if not secret:
        return {"Content-Type": "application/json"}
    ts = int(time.time())
    normalized_path = path.split("?", 1)[0]
    sig_payload = f"{agent}\n{method.upper()}\n{normalized_path}\n{ts}".encode("utf-8")
    digest = hmac.new(secret.encode("utf-8"), sig_payload, hashlib.sha256).digest()
    signature = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return {
        "x-pinky-agent": agent,
        "x-pinky-timestamp": str(ts),
        "x-pinky-signature": signature,
        "Content-Type": "application/json",
    }

def tg_notify(text):
    """Send Telegram notification to Mirko via pinky broker API (CTRL-1 fix)."""
    try:
        payload = json.dumps({
            "agent_name": "aiena",
            "chat_id": TG_CHAT_ID,
            "platform": "telegram",
            "content": text
        }).encode()
        req = urllib.request.Request(
            "http://localhost:8888/broker/send",
            data=payload,
            headers=_broker_headers("POST", "/broker/send")
        )
        urllib.request.urlopen(req, timeout=5)
    except Exception as e:
        print(f"TG notify error: {e}")

def run():
    notified = load_notified()
    tickets = fetch_recent_tickets()
    new_sent = 0
    tg_messages = []

    for t in tickets:
        tid = t['id']
        if tid not in notified:
            try:
                send_notification(t)
                notified.add(tid)
                new_sent += 1
                tipo = t.get('tipo','—')
                code = t.get('ticket_code','—')
                titolo = (t.get('titolo') or '(senza titolo)')[:60]
                email_u = t.get('email') or 'anonimo'
                tg_messages.append(f"• `{code}` [{tipo}] — {titolo} ({email_u})")
                print(f"✓ Notified: {code}")
            except Exception as e:
                print(f"✗ Error {t.get('ticket_code','?')}: {e}")

    if tg_messages:
        msg = f"📬 *Nuov{'a' if len(tg_messages)==1 else 'e'} segnalazion{'e' if len(tg_messages)==1 else 'i'} su AIena.it*\n\n"
        msg += "\n".join(tg_messages)
        msg += "\n\nhttps://aiena.it/ticket.html"
        tg_notify(msg)

    save_notified(notified)
    print(f"Done. New notifications: {new_sent}")
    return new_sent

if __name__ == "__main__":
    run()
