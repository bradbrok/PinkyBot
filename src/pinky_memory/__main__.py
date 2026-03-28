"""Run the Pinky Memory MCP server standalone.

Usage:
    python -m pinky_memory                          # File-based (default)
    python -m pinky_memory --dir ./memory            # Custom memory directory
    python -m pinky_memory --backend sqlite --db ./data/memory.db  # SQLite with vector search
"""

from __future__ import annotations

import argparse
import os
import sys


def main() -> None:
    parser = argparse.ArgumentParser(description="Pinky Memory MCP Server")
    parser.add_argument("--backend", default="file", choices=["file", "sqlite"],
                        help="Storage backend: file (default, Pulse-style markdown) or sqlite (vector search)")
    parser.add_argument("--dir", default=os.environ.get("PINKY_MEMORY_DIR", "memory"),
                        help="Memory directory for file backend (default: memory/)")
    parser.add_argument("--db", default=os.environ.get("PINKY_MEMORY_DB", "data/memory.db"),
                        help="SQLite database path for sqlite backend (default: data/memory.db)")
    parser.add_argument("--transport", default="stdio", choices=["stdio", "sse"],
                        help="MCP transport (default: stdio)")
    parser.add_argument("--host", default="127.0.0.1", help="Host for SSE transport")
    parser.add_argument("--port", type=int, default=8100, help="Port for SSE transport")
    args = parser.parse_args()

    if args.backend == "file":
        from pinky_memory.file_server import create_file_server
        print(f"[pinky-memory] backend=file dir={args.dir} transport={args.transport}", file=sys.stderr, flush=True)
        server = create_file_server(args.dir, host=args.host, port=args.port)
    else:
        from pinky_memory.embeddings import build_embedding_client
        from pinky_memory.store import ReflectionStore
        from pinky_memory.server import create_server
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
