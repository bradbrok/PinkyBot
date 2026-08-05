#!/usr/bin/env python3
"""
send_email.py — Invia email da ziomik@etik.com via SMTP e salva in IMAP Sent.

Uso:
    python3 send_email.py --to dest@example.com --subject "Oggetto" --body "Testo"
    python3 send_email.py --to dest@example.com --subject "Oggetto" --body-file /tmp/body.txt

Fix IMAP Sent: dopo l'invio SMTP, fa APPEND nella cartella "Sent" via IMAP.
Senza questo, l'email non compare nella posta inviata del client.
"""
from __future__ import annotations

import argparse
import imaplib
import smtplib
import sys
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

# Config
EMAIL_USER = "ziomik@etik.com"
EMAIL_PASS = "k!6t6#igEFBh!bKn"
SMTP_HOST = "mail.infomaniak.com"
SMTP_PORT = 587
IMAP_HOST = "imap.infomaniak.com"
IMAP_PORT = 993
SENT_FOLDER = "Sent"


def build_message(
    to: str,
    subject: str,
    body: str,
    from_name: str = "Mirko Feriotti",
    reply_to: str | None = None,
) -> MIMEMultipart:
    msg = MIMEMultipart("alternative")
    msg["From"] = f"{from_name} <{EMAIL_USER}>"
    msg["To"] = to
    msg["Subject"] = subject
    msg["Date"] = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")
    if reply_to:
        msg["Reply-To"] = reply_to
    msg.attach(MIMEText(body, "plain", "utf-8"))
    return msg


def send_and_save(msg: MIMEMultipart, to: str) -> None:
    raw = msg.as_bytes()

    # 1. Send via SMTP
    print(f"[SMTP] Connessione a {SMTP_HOST}:{SMTP_PORT} …", flush=True)
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
        s.ehlo()
        s.starttls()
        s.login(EMAIL_USER, EMAIL_PASS)
        s.sendmail(EMAIL_USER, [to], raw)
    print(f"[SMTP] Email inviata a {to}", flush=True)

    # 2. Append to IMAP Sent folder (fix: visible in Sent of email client)
    print(f"[IMAP] Salvataggio in '{SENT_FOLDER}' …", flush=True)
    imap = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
    imap.login(EMAIL_USER, EMAIL_PASS)
    imap.append(
        SENT_FOLDER,
        r"\Seen",
        imaplib.Time2Internaldate(datetime.now(timezone.utc)),
        raw,
    )
    imap.logout()
    print(f"[IMAP] Salvata in '{SENT_FOLDER}'", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Invia email con salvataggio IMAP Sent")
    parser.add_argument("--to", required=True, help="Destinatario")
    parser.add_argument("--subject", required=True, help="Oggetto")
    parser.add_argument("--body", help="Corpo del messaggio (testo)")
    parser.add_argument("--body-file", help="File contenente il corpo del messaggio")
    parser.add_argument("--from-name", default="Mirko Feriotti", help="Nome mittente")
    parser.add_argument("--reply-to", help="Reply-To address")
    args = parser.parse_args()

    if args.body_file:
        with open(args.body_file, "r", encoding="utf-8") as f:
            body = f.read()
    elif args.body:
        body = args.body
    else:
        print("Errore: fornire --body o --body-file", file=sys.stderr)
        sys.exit(1)

    msg = build_message(
        to=args.to,
        subject=args.subject,
        body=body,
        from_name=args.from_name,
        reply_to=args.reply_to,
    )
    send_and_save(msg, args.to)
    print("✓ Completato.")


if __name__ == "__main__":
    main()
