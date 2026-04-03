"""Run pinky-self MCP server standalone."""

import argparse
import sys

from pinky_self.server import create_server


def main():
    parser = argparse.ArgumentParser(description="Pinky Self-Management MCP Server")
    parser.add_argument("--agent", required=True, help="Agent name (e.g. oleg)")
    parser.add_argument("--api-url", default="http://localhost:8888", help="PinkyBot API URL")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8010)
    args = parser.parse_args()

    server = create_server(
        agent_name=args.agent,
        api_url=args.api_url,
        host=args.host,
        port=args.port,
    )

    print(f"[pinky-self] Starting for agent '{args.agent}'", file=sys.stderr)
    print(f"  API: {args.api_url}", file=sys.stderr)
    print(f"  Host: {args.host}:{args.port}", file=sys.stderr)
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
