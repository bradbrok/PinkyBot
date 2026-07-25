"""Run the credential-holding RingCentral bridge on the Mac Mini."""

from __future__ import annotations

import argparse
import os
import stat
from pathlib import Path


def _load_env_file(path: Path) -> None:
    """Load a mode-600 KEY=VALUE file without evaluating shell syntax."""

    if not path.exists():
        return
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise SystemExit(
            f"Error: credential file {path} must not be group/world accessible"
        )
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise SystemExit(f"Error: cannot read credential file {path}: {exc}") from None
    for number, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("export "):
            stripped = stripped[7:].lstrip()
        if "=" not in stripped:
            raise SystemExit(f"Error: malformed credential file line {number}")
        key, value = stripped.split("=", 1)
        key = key.strip()
        value = value.strip()
        if (
            not key
            or not key.replace("_", "").isalnum()
            or not key[0].isalpha()
        ):
            raise SystemExit(f"Error: invalid credential key on line {number}")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ.setdefault(key, value)


def build_app(*, env_file: str = ""):
    """Construct the production bridge from environment-backed configuration."""

    repo_root = Path(__file__).resolve().parents[2]
    credentials = Path(
        env_file
        or os.environ.get(
            "RINGCENTRAL_ENV_FILE",
            str(repo_root / ".secrets" / "ringcentral.env"),
        )
    )
    _load_env_file(credentials)

    from pinky_ringcentral.auth import DerivedKeyAuthenticator
    from pinky_ringcentral.bridge import create_app
    from pinky_ringcentral.client import RingCentralClient
    from pinky_ringcentral.inbound import RingCentralInboundWorker
    from pinky_ringcentral.service import SmsService
    from pinky_ringcentral.store import BridgeStore, DoNotTextStore
    from pinky_ringcentral.wake import DaemonFerryWakeSink

    master_secret = (
        os.environ.get("RINGCENTRAL_BRIDGE_MASTER_SECRET", "").strip()
        or os.environ.get("PINKY_SESSION_SECRET", "").strip()
    )
    if not master_secret:
        raise SystemExit(
            "Error: RINGCENTRAL_BRIDGE_MASTER_SECRET or "
            "PINKY_SESSION_SECRET is required"
        )
    session_secret = os.environ.get("PINKY_SESSION_SECRET", "").strip()
    if not session_secret:
        raise SystemExit("Error: PINKY_SESSION_SECRET is required for ferry wake")

    state_dir = Path(
        os.environ.get(
            "RINGCENTRAL_STATE_DIR",
            str(repo_root / "data" / "ringcentral"),
        )
    )
    client = RingCentralClient(
        os.environ.get("RINGCENTRAL_CLIENT_ID", ""),
        os.environ.get("RINGCENTRAL_CLIENT_SECRET", ""),
        os.environ.get("RINGCENTRAL_SERVER", ""),
        os.environ.get("RINGCENTRAL_JWT", ""),
    )
    wake_sink = DaemonFerryWakeSink(
        api_url=os.environ.get("PINKY_DAEMON_URL", "http://127.0.0.1:8888"),
        wake_url=os.environ.get("RINGCENTRAL_WAKE_URL", ""),
        session_secret=session_secret,
        source_agent=os.environ.get(
            "RINGCENTRAL_WAKE_SOURCE_AGENT", "barsik"
        ),
        target=os.environ.get("RINGCENTRAL_WAKE_TARGET", "geordi@pi"),
    )
    service = SmsService(
        client,
        DoNotTextStore(state_dir / "do_not_text.json"),
        BridgeStore(state_dir / "bridge.sqlite3"),
        wake_sink,
    )
    agents = {
        value.strip()
        for value in os.environ.get(
            "RINGCENTRAL_ALLOWED_AGENTS", "geordi"
        ).split(",")
        if value.strip()
    }
    authenticator = DerivedKeyAuthenticator(
        master_secret, allowed_agents=agents
    )
    inbound_enabled = os.environ.get(
        "RINGCENTRAL_INBOUND_ENABLED", "1"
    ).lower() not in {"0", "false", "no"}
    worker = RingCentralInboundWorker(service) if inbound_enabled else None
    return create_app(service, authenticator, worker=worker)


def main() -> None:
    parser = argparse.ArgumentParser(description="Pinky RingCentral bridge")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=9101)
    parser.add_argument("--env-file", default="")
    args = parser.parse_args()

    import uvicorn

    uvicorn.run(
        build_app(env_file=args.env_file),
        host=args.host,
        port=args.port,
        log_level="info",
    )


if __name__ == "__main__":
    main()
