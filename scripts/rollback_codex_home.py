#!/usr/bin/env python3
"""Move cwd-matched rollouts from an isolated Codex home back to shared."""

from __future__ import annotations

import argparse
from pathlib import Path

from pinky_daemon.codex_home import (
    per_agent_codex_home_enabled,
    rollback_agent_rollouts,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Move one working directory's Codex rollouts back to the shared "
            "user-level session store after disabling per-agent homes."
        )
    )
    parser.add_argument("working_dir", type=Path)
    parser.add_argument(
        "--agent-home",
        type=Path,
        help="isolated CODEX_HOME (default: <working_dir>/.codex)",
    )
    parser.add_argument(
        "--shared-home",
        type=Path,
        help="shared CODEX_HOME (default: CODEX_HOME or ~/.codex)",
    )
    args = parser.parse_args()

    if per_agent_codex_home_enabled():
        parser.error(
            "disable PINKY_CODEX_PER_AGENT_HOME before moving rollouts back"
        )

    working_dir = args.working_dir.expanduser().resolve()
    agent_home = (
        args.agent_home.expanduser().resolve()
        if args.agent_home is not None
        else working_dir / ".codex"
    )
    moved = rollback_agent_rollouts(
        working_dir,
        agent_home,
        shared_home=args.shared_home,
        log=print,
    )
    print(f"moved={moved}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
