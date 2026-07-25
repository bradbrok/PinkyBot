"""Run the Pi-side RingCentral MCP wrapper over stdio."""

from __future__ import annotations

import argparse
import os
import sys


def _read_secret_fd(fd: int) -> str:
    if fd < 0:
        raise ValueError("secret FD must be non-negative")
    try:
        with os.fdopen(fd, "rb", closefd=True) as handle:
            value = handle.read().decode("utf-8", errors="strict").strip()
    except (OSError, UnicodeError) as exc:
        raise SystemExit(f"Error: cannot read --secret-fd {fd}: {exc}") from None
    if not value:
        raise SystemExit(f"Error: --secret-fd {fd} returned an empty key")
    return value


def run(
    *,
    agent_name: str,
    bridge_url: str = "",
    secret_fd: int = -1,
    instance_id: str = "",
) -> None:
    if not agent_name:
        raise SystemExit("Error: --agent is required")
    env_key = os.environ.get("RINGCENTRAL_DERIVED_KEY", "").strip()
    instance_id = (
        instance_id
        or os.environ.get("RINGCENTRAL_INSTANCE_ID", "").strip()
        or os.environ.get("PINKY_INSTANCE_ID", "").strip()
    )
    if secret_fd >= 0:
        secret = _read_secret_fd(secret_fd)
        auth_mode = "derived-fd"
    elif env_key:
        secret = env_key
        auth_mode = "derived-env"
    else:
        raise SystemExit(
            "Error: configure --secret-fd or RINGCENTRAL_DERIVED_KEY"
        )
    if not instance_id:
        raise SystemExit(
            "Error: --instance-id or RINGCENTRAL_INSTANCE_ID is required"
        )

    from pinky_ringcentral.server import create_server

    server = create_server(
        agent_name=agent_name,
        secret=secret,
        instance_id=instance_id,
        bridge_url=bridge_url,
    )
    target = (
        bridge_url
        or os.environ.get("RINGCENTRAL_BRIDGE_URL")
        or "Mini fallback addresses"
    )
    print(
        f"[ringcentral-mcp] {agent_name} -> {target} (auth={auth_mode})",
        file=sys.stderr,
    )
    server.run(transport="stdio")


def main() -> None:
    parser = argparse.ArgumentParser(description="Pinky RingCentral MCP")
    parser.add_argument(
        "--agent",
        default=os.environ.get("PINKY_AGENT_NAME", "geordi"),
    )
    parser.add_argument("--bridge-url", default="")
    parser.add_argument("--secret-fd", type=int, default=-1)
    parser.add_argument(
        "--instance-id",
        default=os.environ.get("PINKY_INSTANCE_ID", ""),
    )
    args = parser.parse_args()
    run(
        agent_name=args.agent,
        bridge_url=args.bridge_url,
        secret_fd=args.secret_fd,
        instance_id=args.instance_id,
    )


if __name__ == "__main__":
    main()
