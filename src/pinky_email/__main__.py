"""Run the Pinky Email MCP server standalone.

Usage:
    python -m pinky_email --email-user user@example.com --email-pass secret
    python -m pinky_email --imap-host imap.example.com --smtp-host smtp.example.com ...
"""

from __future__ import annotations

import argparse
import sys


def main() -> None:
    parser = argparse.ArgumentParser(description="Pinky Email MCP Server")
    parser.add_argument(
        "--email-user", required=True,
        help="Email address / IMAP username"
    )
    parser.add_argument(
        "--email-pass", required=True,
        help="Email password"
    )
    parser.add_argument(
        "--imap-host", default="imap.infomaniak.com",
        help="IMAP server hostname (default: imap.infomaniak.com)"
    )
    parser.add_argument(
        "--imap-port", type=int, default=993,
        help="IMAP server port (default: 993 for SSL)"
    )
    parser.add_argument(
        "--smtp-host", default="mail.infomaniak.com",
        help="SMTP server hostname (default: mail.infomaniak.com)"
    )
    parser.add_argument(
        "--smtp-port", type=int, default=587,
        help="SMTP server port (default: 587 for STARTTLS)"
    )
    args = parser.parse_args()

    from pinky_email.server import create_server

    print(f"[pinky-email] Starting for {args.email_user}", file=sys.stderr, flush=True)
    print(f"  IMAP: {args.imap_host}:{args.imap_port}", file=sys.stderr, flush=True)
    print(f"  SMTP: {args.smtp_host}:{args.smtp_port}", file=sys.stderr, flush=True)

    server = create_server(
        email_user=args.email_user,
        email_pass=args.email_pass,
        imap_host=args.imap_host,
        imap_port=args.imap_port,
        smtp_host=args.smtp_host,
        smtp_port=args.smtp_port,
    )

    server.run(transport="stdio")


if __name__ == "__main__":
    main()
