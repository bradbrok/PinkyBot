#!/usr/bin/env python3
"""
claude-sdk-relay — TCP relay per Claude Code SDK.

Gira come LaunchAgent macOS (sessione utente, accesso Keychain).
Spawna il claude CLI bundled nella sessione utente → autenticazione funziona.
Accetta connessioni TCP su localhost:9001, espone stdin/stdout del processo claude.

Protocollo:
  Client → Server: prima riga = JSON array degli argomenti CLI, es:
                   ["--output-format", "stream-json", "--verbose", ...]\n
  Dopo: flusso stdin bidirezionale (client→claude stdin, claude stdout→client).

Port: 9001
"""

import asyncio
import json
import os
import sys
import subprocess
from pathlib import Path

RELAY_PORT = 9001
RELAY_HOST = "127.0.0.1"

# Usa il claude bundled nel venv PinkyBot (stessa versione usata da Grigetto)
VENV = Path("/Users/personalia/.pinkybot/.venv")
PYTHON_VER = next(
    (p.name for p in (VENV / "lib").iterdir() if p.name.startswith("python")), "python3.14"
)
BUNDLED_CLAUDE = VENV / "lib" / PYTHON_VER / "site-packages" / "claude_agent_sdk" / "_bundled" / "claude"
FALLBACK_CLAUDE = Path("/Users/personalia/.local/bin/claude")

CLAUDE_BIN = str(BUNDLED_CLAUDE) if BUNDLED_CLAUDE.exists() else str(FALLBACK_CLAUDE)


async def pipe(reader: asyncio.StreamReader, writer) -> None:
    """Pipe data from reader to writer until EOF."""
    try:
        while True:
            chunk = await reader.read(4096)
            if not chunk:
                break
            if hasattr(writer, "write"):
                writer.write(chunk)
            else:
                os.write(writer, chunk)
    except (asyncio.CancelledError, BrokenPipeError, ConnectionResetError):
        pass


async def handle_client(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> None:
    """Handle single connection: read args → spawn claude → proxy I/O."""
    peer = writer.get_extra_info("peername", ("?", 0))
    try:
        # First line: JSON array of CLI args
        line = await asyncio.wait_for(reader.readline(), timeout=10.0)
        if not line:
            return

        # Special request: "TOKEN" → serve ~/.claude/.credentials.json (no rotation)
        if line.strip() in (b'"TOKEN"', b'TOKEN'):
            creds_path = Path.home() / ".claude" / ".credentials.json"
            try:
                writer.write(creds_path.read_bytes())
                await writer.drain()
            except Exception as e:
                writer.write(json.dumps({"error": str(e)}).encode())
                await writer.drain()
            return

        try:
            extra_args = json.loads(line.strip())
            if not isinstance(extra_args, list):
                raise ValueError("args must be JSON array")
        except (json.JSONDecodeError, ValueError) as e:
            writer.write(f"ERROR: bad args: {e}\n".encode())
            await writer.drain()
            return

        cmd = [CLAUDE_BIN] + extra_args

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "CLAUDE_CODE_ENTRYPOINT": "sdk-py"},
        )

        # Bidirectional proxy: socket↔process
        async def forward_stdin():
            try:
                while True:
                    chunk = await reader.read(4096)
                    if not chunk:
                        break
                    proc.stdin.write(chunk)
                    await proc.stdin.drain()
            except Exception:
                pass
            finally:
                try:
                    proc.stdin.close()
                except Exception:
                    pass

        async def forward_stdout():
            try:
                while True:
                    chunk = await proc.stdout.read(4096)
                    if not chunk:
                        break
                    writer.write(chunk)
                    await writer.drain()
            except Exception:
                pass

        await asyncio.gather(forward_stdin(), forward_stdout())
        await proc.wait()

    except asyncio.TimeoutError:
        writer.write(b'{"error": "connection timeout"}\n')
    except Exception as e:
        try:
            writer.write(json.dumps({"error": str(e)}).encode() + b"\n")
        except Exception:
            pass
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass


async def main() -> None:
    server = await asyncio.start_server(handle_client, RELAY_HOST, RELAY_PORT)
    addr = server.sockets[0].getsockname()
    print(f"[claude-sdk-relay] Listening on {addr[0]}:{addr[1]}", flush=True)
    print(f"[claude-sdk-relay] Claude binary: {CLAUDE_BIN}", flush=True)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
