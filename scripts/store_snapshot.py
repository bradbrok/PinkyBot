#!/usr/bin/env python3
"""Request daemon-owned SQLite snapshots; never open a live store directly."""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

from pinky_daemon.auth import build_internal_auth_headers

SNAPSHOT_PATH = "/internal/stores/snapshot"


def _default_api_url() -> str:
    port = os.environ.get("PINKY_API_PORT", "8888").strip() or "8888"
    return f"http://127.0.0.1:{port}"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Ask the PinkyBot daemon to produce a verified store snapshot. "
            "This wrapper never opens a SQLite database."
        )
    )
    parser.add_argument(
        "--logical-name",
        default="",
        help="Catalog logical name; omit to snapshot all authoritative stores.",
    )
    parser.add_argument(
        "--as-agent",
        default=os.environ.get("PINKY_AGENT_NAME", ""),
        help="Registered non-isolated caller identity (or PINKY_AGENT_NAME).",
    )
    parser.add_argument(
        "--api-url",
        default=_default_api_url(),
        help="Daemon base URL (default: http://127.0.0.1:$PINKY_API_PORT).",
    )
    parser.add_argument("--timeout", type=float, default=300.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    agent_name = args.as_agent.strip()
    if not agent_name:
        parser.error("pass --as-agent or set PINKY_AGENT_NAME")

    secret = os.environ.get("PINKY_AGENT_KEY", "").strip()
    if not secret:
        secret = os.environ.get("PINKY_SESSION_SECRET", "").strip()
    if not secret:
        parser.error("PINKY_AGENT_KEY or PINKY_SESSION_SECRET is required")

    body = {}
    if args.logical_name.strip():
        body["logical_name"] = args.logical_name.strip()
    encoded = json.dumps(body, separators=(",", ":")).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        **build_internal_auth_headers(
            secret,
            agent_name=agent_name,
            method="POST",
            path=SNAPSHOT_PATH,
        ),
    }
    request = urllib.request.Request(
        args.api_url.rstrip("/") + SNAPSHOT_PATH,
        data=encoded,
        method="POST",
        headers=headers,
    )
    try:
        with urllib.request.urlopen(request, timeout=args.timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        print(f"snapshot request failed: HTTP {exc.code}: {detail}", file=sys.stderr)
        return 1
    except (OSError, ValueError) as exc:
        print(f"snapshot request failed: {exc}", file=sys.stderr)
        return 1

    had_error = payload.get("status") in {"partial_success", "failed"}
    for result in payload.get("snapshots", []):
        if result.get("status") == "published" and result.get("path"):
            print(result["path"])
            continue
        had_error = True
        names = ",".join(result.get("logical_names", []))
        if not names:
            names = result.get("logical_name") or "unknown"
        print(
            f"{names}: {result.get('error') or 'snapshot failed'}",
            file=sys.stderr,
        )
    return 1 if had_error else 0


if __name__ == "__main__":
    raise SystemExit(main())
