"""Read child reports after their writers publish a complete sibling file."""

import asyncio
import time
from pathlib import Path


async def wait_for_child_report(path: Path, message: str) -> str:
    """Allow slow child startup without exposing an unfinished report."""
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        try:
            return path.read_text()
        except FileNotFoundError:
            await asyncio.sleep(0.01)
    raise AssertionError(message)
