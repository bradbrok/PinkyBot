#!/usr/bin/env python3
"""
Monitor email ziomik@etik.com
Logica da segretario personale: euristica intelligente senza API esterne.
Zero costo, decision tree basato su mittente + subject pattern.
"""
import imaplib
import email
from email.header import decode_header
import sqlite3
import requests
import re
from datetime import datetime

# Config
IMAP_SERVER = "imap.infomaniak.com"
IMAP_PORT = 993
EMAIL_USER = "ziomik@etik.com"
EMAIL_PASS = "k!6t6#igEFBh!bKn"
TELEGRAM_CHAT_ID = "32405655"
PINKYBOT_DB = "/home/pinky/.pinkybot/data/conversations_agents.db"
STATE_DB = "/home/pinky/.pinkybot/scripts/email_monitor_state.db"

# ── BLOCKLIST: mittenti/domini da ignorare sempre ──────────────────────────────
SENDER_BLOCKLIST = [
    # Social media
    "linkedin", "facebook", "instagram", "twitter", "tiktok", "youtube",
    "pinterest", "reddit", "discord", "telegram",
    # Newsletter/marketing generici
    "mailchimp", "sendgrid", "mailgun", "constantcontact", "klaviyo",
    "hubspot", "marketo", "salesforce", "brevo", "sendinblue",
    # Notifiche automatiche dev/tech
    "github", "gitlab", "jira", "confluence", "sentry", "datadog",
    "newrelic", "pagerduty", "statuspage", "uptime",
    # Promo e-commerce generiche
    "newsletter", "promo", "offerte", "deals", "noreply@amazon",
    "noreply@aliexpress", "noreply@ebay",
    # Altro
    "unsubscribe", "no-reply@medium", "digest", "weekly",
]

# ── WHITELIST: mittenti/domini sempre da notificare ───────────────────────────
SENDER_WHITELIST = [
    # Mirko — tutte le forward da ziomik@ziomik.net sono priorità alta
    "ziomik@ziomik.net", "ziomik.net",
    # Pagamenti
    "paypal", "stripe", "revolut", "wise", "satispay", "klarna",
    # Banche italiane
    "intesasanpaolo", "unicredit", "fineco", "mediolanum", "poste.it",
    "bancasella", "ing.it", "n26",
    # Marketplace (solo VENDITE/ACQUISTI confermati — NON alert generici)
    "wallapop", "subito", "vinted", "depop",
    # Crypto
    "binance", "kraken", "coinbase", "crypto.com",
    # Sicurezza account
    "security@", "security-noreply@", "account-security@",
    "accounts.google.com", "account@google", "alert@",
    # Logistica/spedizioni
    "poste.it", "dhl", "fedex", "ups.com", "gls-italy", "brt.it",
    "nexive", "corriere",
    # Fatture/fisco
    "agenziaentrate", "fatturapa", "sdi@", "pec@",
    # Medico
    "cup@", "prenotazioni@", "recall@",
    # Deliveroo/food (ordini confermati)
    "deliveroo", "justeat", "glovo", "ubereats",
    # eBay (spedizioni e consegne, non marketing)
    "ebay@ebay.com", "ebay@ebay.it", "members.ebay.com",
    # Servizi pubblici italiani
    "io.italia.it", "comune.", "regione.", "agenzia",
    # Buste paga e documenti aziendali
    "dipendentincloud.it", "zucchetti", "paghe",
]

# ── SUBJECT: pattern che indicano importanza ──────────────────────────────────
SUBJECT_IMPORTANT = [
    # Sicurezza
    r"avviso di sicurezza", r"accesso .{0,20}account", r"nuov[oa] access",
    r"tentativo di accesso", r"password", r"verifica", r"autenticazione",
    r"codice di sicurezza", r"suspicious", r"unusual activity",
    # Pagamenti / Finanza
    r"pagamento", r"bonifico", r"fattura", r"ricevuta", r"estratto conto",
    r"rimborso", r"accredito", r"addebito", r"scadenza", r"bolletta",
    r"payment", r"invoice", r"receipt", r"refund", r"transaction",
    # Ordini e Vendite
    r"ordine confermato", r"ordine accettato", r"spedito", r"in consegna",
    r"consegnato", r"order confirmed", r"shipped", r"delivery",
    r"vendita confermata", r"vendita completata", r"acquisto confermato",
    r"hai venduto", r"hai ricevuto", r"offerta accettata",
    r"pagamento ricevuto", r"hai guadagnato",
    # Appuntamenti
    r"appuntamento", r"prenotazione", r"conferma visita", r"promemoria",
    r"disdetta", r"annullamento", r"reminder", r"booking",
    # Lavoro / Contratti
    r"contratto", r"proposta", r"preventivo", r"offerta di lavoro",
    r"colloquio", r"candidatura", r"assunzione",
    # Viaggi
    r"volo", r"biglietto", r"itinerario", r"hotel", r"check-in",
    r"prenotazione confermata", r"boarding pass",
    # Urgenza
    r"urgente", r"attenzione", r"importante", r"scade oggi", r"ultimo giorno",
]

# ── SUBJECT: pattern da ignorare sempre ──────────────────────────────────────
SUBJECT_BLOCKLIST = [
    r"unsubscribe", r"newsletter", r"weekly digest", r"monthly recap",
    r"you have \d+ new notification", r"\d+ new messages? from",
    r"hai \d+ nuov", r"new followers?", r"liked your",
    r"commented on", r"tagged you", r"mentioned you",
    r"top stories", r"recommended for you", r"because you follow",
    r"don't miss", r"last chance", r"limited time offer",
    r"% off", r"sconto", r"offerta speciale", r"solo oggi",
    # eBay: alert inserzioni, watchlist, raccomandazioni — NON vendite
    r"nuova inserzione", r"inserzione salvata", r"articolo che segui",
    r"potrebbe interessarti", r"simile a", r"correlat",
    r"ha ridotto il prezzo", r"ancora disponibile",
    r"ebay deals", r"ebay offerte", r"ebay newsletter",
    r"hai ancora tempo", r"non perderti", r"offerta imperdibile",
    # Amazon: raccomandazioni, price drop alert
    r"amazon consiglia", r"in base ai tuoi acquisti",
    r"potrebbe piacerti", r"price drop", r"calo del prezzo",
]


def get_telegram_token():
    try:
        conn = sqlite3.connect(PINKYBOT_DB)
        c = conn.cursor()
        c.execute("SELECT token FROM agent_tokens WHERE platform='telegram' AND agent_name='satoshi'")
        row = c.fetchone()
        conn.close()
        return row[0] if row else None
    except:
        return None


def send_telegram(text):
    token = get_telegram_token()
    if not token:
        print("Telegram token non trovato")
        return
    requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"},
        timeout=10
    )


def init_state_db():
    conn = sqlite3.connect(STATE_DB)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS notified_emails (
            uid TEXT PRIMARY KEY,
            subject TEXT,
            sender TEXT,
            notified_at TEXT,
            telegram_sent INTEGER DEFAULT 0
        )
    """)
    # Migrazione: aggiungi colonna telegram_sent se manca
    try:
        c.execute("ALTER TABLE notified_emails ADD COLUMN telegram_sent INTEGER DEFAULT 0")
    except Exception:
        pass  # colonna già esiste
    conn.commit()
    conn.close()


def is_notified(uid):
    conn = sqlite3.connect(STATE_DB)
    c = conn.cursor()
    c.execute("SELECT 1 FROM notified_emails WHERE uid=?", (uid,))
    r = c.fetchone()
    conn.close()
    return r is not None


def mark_notified(uid, subject, sender, telegram_sent: bool = False):
    conn = sqlite3.connect(STATE_DB)
    c = conn.cursor()
    c.execute(
        "INSERT OR IGNORE INTO notified_emails (uid, subject, sender, notified_at, telegram_sent) VALUES (?,?,?,?,?)",
        (uid, subject, sender, datetime.now().isoformat(), 1 if telegram_sent else 0)
    )
    conn.commit()
    conn.close()


def decode_str(s):
    if s is None:
        return ""
    parts = decode_header(s)
    result = []
    for part, enc in parts:
        if isinstance(part, bytes):
            result.append(part.decode(enc or 'utf-8', errors='replace'))
        else:
            result.append(part)
    return " ".join(result)


def is_real_person(sender: str) -> bool:
    """Controlla se il mittente sembra una persona reale (non bot/sistema)."""
    s = sender.lower()
    bot_patterns = [
        "noreply", "no-reply", "donotreply", "do-not-reply",
        "notification", "notifications", "alert", "alerts",
        "info@", "support@", "hello@", "team@", "news@",
        "marketing@", "promo@", "digest@", "updates@",
        "newsletter@", "mailer@", "postmaster@", "bounce@",
    ]
    # Se contiene pattern bot → non è persona reale
    for p in bot_patterns:
        if p in s:
            return False
    # Se il display name sembra un'azienda (tutto maiuscolo o contiene Inc/Ltd/SRL)
    if re.search(r'\b(inc|ltd|srl|spa|gmbh|llc|corp)\b', s, re.I):
        return False
    # Heuristica: display name con spazio (es. "Mario Rossi") → persona reale
    display_match = re.match(r'^"?([^<"]+)"?\s*<', sender)
    if display_match:
        name = display_match.group(1).strip()
        if ' ' in name and not any(p in name.lower() for p in ['team', 'support', 'service', 'system']):
            return True
    return False


def evaluate_email(subject: str, sender: str) -> tuple[bool, str]:
    """
    Ritorna (notify: bool, reason: str).
    Logica: whitelist > blocklist > subject patterns > persona reale.
    """
    subj_low = subject.lower()
    send_low = sender.lower()

    # 0. PRIORITÀ ASSOLUTA: alert di sistema (healthchecks DOWN/UP)
    if re.match(r'^(down|up) \|', subj_low):
        status = "DOWN" if subj_low.startswith("down") else "UP"
        check_name = subject.split("|", 1)[1].strip() if "|" in subject else subject
        if status == "DOWN":
            return True, f"🚨 SISTEMA GIU': {check_name} — cron non risponde"
        else:
            return True, f"✅ Sistema ripristinato: {check_name}"

    # 1. Blocklist subject → mai notificare
    for pattern in SUBJECT_BLOCKLIST:
        if re.search(pattern, subj_low, re.I):
            return False, f"blocklist pattern: {pattern}"

    # 2. Blocklist mittente → mai notificare (salvo override subject)
    sender_blocked = any(b in send_low for b in SENDER_BLOCKLIST)

    # 3. Whitelist mittente → notifica sempre
    for w in SENDER_WHITELIST:
        if w in send_low:
            # Cerca anche il motivo nel subject
            reason = _find_subject_reason(subj_low) or f"mittente rilevante ({w})"
            return True, reason

    # 4. Pattern importanti nel subject
    reason = _find_subject_reason(subj_low)
    if reason and not sender_blocked:
        return True, reason

    # 5. Persona reale che ti scrive direttamente
    if is_real_person(sender) and not sender_blocked:
        return True, "email da persona reale"

    return False, "non rilevante"


def _find_subject_reason(subj_low: str) -> str | None:
    """Ritorna una descrizione human-readable del perché il subject è importante."""
    checks = [
        (r"avviso di sicurezza|accesso|password|verifica|suspicious", "avviso sicurezza account"),
        (r"pagamento|fattura|ricevuta|bonifico|rimborso|accredito", "pagamento/fattura"),
        (r"busta paga|cedolino|stipendio|payslip", "busta paga"),
        (r"ordine|spedito|consegna|shipped|delivery|order|in consegna|consegnato", "ordine/spedizione"),
        (r"vendita|hai venduto|offerta accettata", "vendita completata"),
        (r"appuntamento|prenotazione|visita|reminder", "appuntamento/prenotazione"),
        (r"contratto|preventivo|offerta di lavoro|colloquio", "lavoro/contratto"),
        (r"volo|biglietto|hotel|boarding|check-in", "viaggio/prenotazione"),
        (r"urgente|attenzione|scade oggi|importante", "urgenza"),
        (r"estratto conto|bolletta|scadenza|tassa|canone|rata", "scadenza/bolletta"),
        (r"multa|cartella|avviso di accertamento|notifica atti", "atti pubblici"),
    ]
    for pattern, label in checks:
        if re.search(pattern, subj_low, re.I):
            return label
    return None


def check_emails():
    init_state_db()

    try:
        m = imaplib.IMAP4_SSL(IMAP_SERVER, IMAP_PORT)
        m.login(EMAIL_USER, EMAIL_PASS)
        m.select("INBOX")

        status, data = m.search(None, "UNSEEN")
        if status != "OK" or not data[0]:
            m.logout()
            return []

        uids = data[0].split()
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Non letti: {len(uids)}")

        important = []
        for uid in uids[-50:]:  # ultimi 50 non letti
            uid_str = uid.decode()
            if is_notified(uid_str):
                continue

            status, msg_data = m.fetch(uid, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])")
            if status != "OK":
                continue

            raw = msg_data[0][1]
            msg = email.message_from_bytes(raw)
            subject = decode_str(msg.get("Subject", "(nessun oggetto)"))
            sender = decode_str(msg.get("From", ""))
            date = msg.get("Date", "")

            # Valuta PRIMA di marcare — così se eval o Telegram falliscono,
            # l'email non sparisce silenziosamente
            notify, reason = evaluate_email(subject, sender)
            if notify:
                important.append({
                    "uid": uid_str,
                    "subject": subject,
                    "sender": sender,
                    "date": date,
                    "reason": reason
                })
                # telegram_sent=False per ora — verrà aggiornato dopo l'invio
                mark_notified(uid_str, subject, sender, telegram_sent=False)
            else:
                # Non importante — marca come processata (non spedita)
                mark_notified(uid_str, subject, sender, telegram_sent=False)

        m.logout()
        return important

    except Exception as e:
        print(f"Errore IMAP: {e}")
        return []


def check_hc_alerts():
    """
    Cerca email DOWN|UP delle ultime 24h INDIPENDENTEMENTE dal flag SEEN.
    Fix per HC alerts che Mirko legge nel webmail prima che giri il monitor.
    Usa state DB prefissato con 'hc_' per evitare collisioni con sequenze UNSEEN.
    """
    from datetime import timedelta
    init_state_db()

    try:
        m = imaplib.IMAP4_SSL(IMAP_SERVER, IMAP_PORT)
        m.login(EMAIL_USER, EMAIL_PASS)
        m.select("INBOX")

        since_date = (datetime.now() - timedelta(days=1)).strftime("%d-%b-%Y")
        found = []
        for keyword in ["DOWN |", "UP |"]:
            status, data = m.search(None, f'(SINCE {since_date} SUBJECT "{keyword}")')
            if status == "OK" and data[0]:
                found.extend(data[0].split())

        alerts_sent = []
        for uid in found:
            # Prefisso 'hc_' per namespace separato nel state DB
            uid_key = f"hc_{uid.decode()}"
            if is_notified(uid_key):
                continue

            status, msg_data = m.fetch(uid, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])")
            if status != "OK":
                continue

            raw = msg_data[0][1]
            msg = email.message_from_bytes(raw)
            subject = decode_str(msg.get("Subject", ""))
            sender = decode_str(msg.get("From", ""))

            subj_low = subject.lower()
            is_down = subj_low.startswith("down")
            emoji = "🚨" if is_down else "✅"
            check_name = subject.split("|", 1)[1].strip() if "|" in subject else subject
            msg_text = (
                f"{emoji} *{'SISTEMA GIU' if is_down else 'Sistema OK'}*\n"
                f"Check: *{check_name}*\n"
                f"{'Il cron non risponde — intervento richiesto.' if is_down else 'Il cron ha ripreso a rispondere.'}\n"
                f"Dashboard: https://admin.aiena.it/hc/"
            )
            try:
                send_telegram(msg_text)
                mark_notified(uid_key, subject, sender, telegram_sent=True)
                print(f"[HC ALERT] {subject}")
                alerts_sent.append(subject)
            except Exception as ex:
                print(f"[ERRORE] HC alert Telegram: {ex}")

        m.logout()
        return alerts_sent

    except Exception as e:
        print(f"[HC] Errore IMAP: {e}")
        return []


AGENTMAIL_API_KEY = "am_us_8dffa2de93ecf7679114c586d70deaedb8da6120e9a226788ac15ba5331ab77e"
AGENTMAIL_INBOX = "aiena@agentmail.to"
AGENTMAIL_STATE_FILE = "/home/pinky/.pinkybot/data/agentmail_seen.txt"


def check_agentmail():
    """Check aiena@agentmail.to for bounces, replies from official entities, and important messages."""
    import pathlib

    seen_file = pathlib.Path(AGENTMAIL_STATE_FILE)
    seen_ids = set(seen_file.read_text().splitlines()) if seen_file.exists() else set()

    try:
        resp = requests.get(
            f"https://api.agentmail.to/v0/inboxes/{AGENTMAIL_INBOX}/messages",
            headers={"Authorization": f"Bearer {AGENTMAIL_API_KEY}"},
            timeout=15
        )
        if resp.status_code != 200:
            return
        data = resp.json()
        messages = data.get("messages", [])
    except Exception as e:
        print(f"[agentmail] Errore: {e}")
        return

    alerts = []
    new_seen = set()

    for msg in messages:
        msg_id = msg.get("thread_id", "")
        if msg_id in seen_ids:
            continue
        new_seen.add(msg_id)

        labels = msg.get("labels", [])
        subject = msg.get("subject", "")
        sender = msg.get("from", "")
        preview = msg.get("preview", "")[:200]

        # Bounce notification (only the mailer-daemon report, not the sent msg itself)
        if ("mailer-daemon" in sender.lower() or "delivery status" in subject.lower()) and "received" in labels:
            alerts.append(
                f"⚠️ *Email AIena bounced*\n"
                f"A: {msg.get('to', ['?'])[0] if isinstance(msg.get('to'), list) else msg.get('to', '?')}\n"
                f"Oggetto: {subject}\n"
                f"Motivo: {preview[:150]}"
            )
        # Reply from an official entity (comune, asl, etc.) or non-automated sender
        elif "received" in labels and "unread" in labels:
            sender_low = sender.lower()
            # Skip Bluesky/system/automated
            skip_patterns = ["bluesky", "bsky.social", "noreply", "no-reply", "donotreply",
                             "amazonses", "mailer-daemon", "postmaster"]
            if any(p in sender_low for p in skip_patterns):
                continue
            # Skip ticket forwards from ziomik (already handled by AIena)
            if "aiena-ticket" in subject.lower():
                continue
            # Anything else is potentially a real reply
            alerts.append(
                f"📨 *Risposta su aiena@agentmail.to*\n"
                f"Da: {sender}\n"
                f"Oggetto: {subject}\n"
                f"{preview}"
            )

    # Save new seen IDs
    if new_seen:
        seen_file.write_text("\n".join(seen_ids | new_seen))

    for alert in alerts:
        send_telegram(alert)
        print(f"[agentmail] Alert inviato: {alert[:80]}")


def mark_telegram_sent(uids: list[str]):
    """Aggiorna telegram_sent=1 per le email effettivamente notificate."""
    conn = sqlite3.connect(STATE_DB)
    c = conn.cursor()
    for uid in uids:
        c.execute("UPDATE notified_emails SET telegram_sent=1 WHERE uid=?", (uid,))
    conn.commit()
    conn.close()


def main():
    # Controlla HC alerts indipendentemente da SEEN (fix: alert letti nel webmail)
    check_hc_alerts()

    emails = check_emails()

    if emails:
        # Separa alert di sistema HC (DOWN/UP) dagli altri
        hc_alerts = [e for e in emails if re.match(r'^(DOWN|UP) \|', e['subject'], re.I)]
        other_emails = [e for e in emails if e not in hc_alerts]

        # Invia alert HC immediati con formato urgente
        for e in hc_alerts:
            is_down = e['subject'].upper().startswith("DOWN")
            emoji = "🚨" if is_down else "✅"
            check_name = e['subject'].split("|", 1)[1].strip() if "|" in e['subject'] else e['subject']
            msg = (
                f"{emoji} *{'SISTEMA GIU' if is_down else 'Sistema OK'}*\n"
                f"Check: *{check_name}*\n"
                f"{'Il cron non risponde — intervento richiesto.' if is_down else 'Il cron ha ripreso a rispondere.'}\n"
                f"Dashboard: https://admin.aiena.it/hc/"
            )
            try:
                send_telegram(msg)
                mark_telegram_sent([e["uid"]])
                print(f"[HC ALERT] {e['subject']}")
            except Exception as ex:
                print(f"[ERRORE] HC alert Telegram fallito: {ex}")

        # Invia il resto normalmente
        if other_emails:
            lines = ["📬 *Email da leggere* — ziomik@etik.com\n"]
            for e in other_emails:
                lines.append(f"📌 *{e['subject'][:80]}*")
                lines.append(f"   Da: {e['sender'][:60]}")
                lines.append(f"   💡 {e['reason']}\n")

        if other_emails:
            try:
                send_telegram("\n".join(lines))
                mark_telegram_sent([e["uid"] for e in other_emails])
                print(f"Notificate {len(other_emails)} email")
            except Exception as ex:
                print(f"[ERRORE] Telegram send fallito: {ex} — email NON marcate come sent")
    else:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Nessuna email da segnalare")

    # Also check AIena's AgentMail inbox
    check_agentmail()


if __name__ == "__main__":
    main()
