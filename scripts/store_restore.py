#!/usr/bin/env python3
"""Attended local wrapper for daemon-down SQLite store restoration."""

from __future__ import annotations

import argparse
import json
import sys

from pinky_daemon.store_manifest import (
    FLEET_MANIFEST_KIND,
    STANDALONE_TENANT_MANIFEST_KIND,
    manifest_provider_for_kind,
)
from pinky_daemon.store_restore import StoreRestoreError, restore_store


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Restore one explicit daemon SQLite store from a verified snapshot. "
            "Stop the daemon before running this command."
        )
    )
    parser.add_argument("--db-path", required=True, help="Canonical conversations DB path.")
    parser.add_argument(
        "--manifest-kind",
        required=True,
        choices=(FLEET_MANIFEST_KIND, STANDALONE_TENANT_MANIFEST_KIND),
        help="Explicit store layout; never inferred from --db-path.",
    )
    parser.add_argument("--logical-name", required=True, help="Exact manifest logical name.")
    parser.add_argument("--snapshot", required=True, help="Static snapshot file to install.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = restore_store(
            db_path=args.db_path,
            logical_name=args.logical_name,
            snapshot_path=args.snapshot,
            manifest_provider=manifest_provider_for_kind(args.manifest_kind),
        )
    except StoreRestoreError as exc:
        print(f"restore refused or failed: {exc}", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "logical_names": list(result.logical_names),
                "corrupt_path": result.corrupt_path,
                "verification": result.verification,
            },
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
