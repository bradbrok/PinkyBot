"""Run the Pinky Memory MCP server standalone.

Usage:
    python -m pinky_memory                              # SQLite (default)
    python -m pinky_memory --db ./data/memory.db        # Custom DB path
"""

from __future__ import annotations

import argparse
import os
import sys


def main() -> None:
    parser = argparse.ArgumentParser(description="Pinky Memory MCP Server")
    parser.add_argument("--db", default=os.environ.get("PINKY_MEMORY_DB", "data/memory.db"),
                        help="SQLite database path (default: data/memory.db)")
    parser.add_argument("--transport", default="stdio", choices=["stdio", "sse"],
                        help="MCP transport (default: stdio)")
    parser.add_argument("--host", default="127.0.0.1", help="Host for SSE transport")
    parser.add_argument("--port", type=int, default=8100, help="Port for SSE transport")
    args = parser.parse_args()

    from pinky_memory.embeddings import build_embedding_client
    from pinky_memory.server import create_server
    from pinky_memory.store import ReflectionStore

    print(f"[pinky-memory] backend=sqlite db={args.db} transport={args.transport}", file=sys.stderr, flush=True)
    store = ReflectionStore(db_path=args.db)
    embedder = build_embedding_client()
    server = create_server(store, embedder, host=args.host, port=args.port)

    if args.transport == "stdio":
        server.run(transport="stdio")
    else:
        server.run(transport="sse")


if __name__ == "__main__":
    main()
