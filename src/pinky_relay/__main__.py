"""Run the ciphertext relay behind a prefix-stripping local reverse proxy."""

import argparse
import logging

from pinky_relay.config import RelayConfig
from pinky_relay.store import QueueStore, require_runtime


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run the bounded ciphertext relay")
    parser.add_argument("--config", required=True)
    parser.add_argument("--db", required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8898)
    args = parser.parse_args(argv)
    require_runtime()
    if args.host != "127.0.0.1":
        parser.error("relay must bind 127.0.0.1")
    config = RelayConfig.from_file(args.config)
    store = QueueStore(args.db, config.digests)
    import uvicorn

    from pinky_relay.app import create_relay_app

    logging.getLogger(__name__).info("relay SQLite version=%s", __import__("sqlite3").sqlite_version)
    try:
        uvicorn.run(create_relay_app(config, store), host="127.0.0.1", port=args.port,
                    http="h11", h11_max_incomplete_event_size=16 * 1024, workers=1,
                    access_log=False)
    finally:
        store.close()


if __name__ == "__main__":
    main()
