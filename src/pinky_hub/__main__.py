"""Run the Pinky Hub API server.

Usage:
    python -m pinky_hub
    python -m pinky_hub --host 0.0.0.0 --port 8889 --db data/hub.db
"""

from __future__ import annotations

import argparse

import uvicorn

from pinky_hub.api import create_hub_app


def main() -> None:
    parser = argparse.ArgumentParser(description="Pinky Hub — pinkybot.ai backend")
    parser.add_argument("--host", default="0.0.0.0", help="Bind host (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8889, help="Bind port (default: 8889)")
    parser.add_argument("--db", default="data/hub.db", help="SQLite DB path (default: data/hub.db)")
    args = parser.parse_args()

    app = create_hub_app(db_path=args.db)
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
