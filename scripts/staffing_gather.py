#!/usr/bin/env python3
"""Build a deterministic staffing-availability matrix from existing sources.

The script is intentionally standalone and uses only the Python standard
library. It consumes the scheduler-owned ``btsprobe`` classification output,
joins the board-owned build loads, and optionally joins Desk open-ticket
counts. It never calls an LLM and never reclassifies Calendar events.

Examples:

    python3 scripts/staffing_gather.py \
      --btsprobe-file btsprobe.json \
      --board-file bts_board.json \
      --desk-file desk_counts.json

    python3 scripts/staffing_gather.py \
      --btsprobe-url 'https://example.invalid/exec?btsprobe=1&from=...&to=...' \
      --board-file bts_board.json

JSON is always emitted on stdout. Any required-source, access, validation, or
join error is represented in ``_err`` and makes the process exit nonzero.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

DESK_AGENT_IDS = {
    # Agent-id VALUES below are placeholders — real Zoho Desk IDs are masked for
    # the public repo. Only the KEYS are consumed (MEMBER_KEYS = tuple(...)); the
    # desk-count join is keyed by member key, never by agent id.
    "danielson": "000000000000000001",
    "daniel": "000000000000000002",
    "aldo": "000000000000000003",
    "julie": "000000000000000004",
}
MEMBER_KEYS = tuple(DESK_AGENT_IDS)
SCAN_TYPES = ("appt", "build", "queue", "ooo")
WORKDAY_START = 8
WORKDAY_END = 18


def _source_state(mode: str) -> dict[str, Any]:
    return {"mode": mode, "_err": []}


def _append_unique(items: list[str], message: str) -> None:
    if message not in items:
        items.append(message)


def _source_error(result: dict[str, Any], source: str, message: str) -> None:
    _append_unique(result["sources"][source]["_err"], message)


def _member_error(result: dict[str, Any], member: str, message: str) -> None:
    _append_unique(result["members"][member]["_err"], message)


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _is_hour(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 24


def _read_json_file(path: Path) -> tuple[Any | None, str | None]:
    try:
        with path.open(encoding="utf-8") as handle:
            return json.load(handle), None
    except OSError as exc:
        return None, f"cannot read {path}: {exc}"
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        return None, f"invalid JSON in {path}: {exc}"


def _read_json_url(
    url: str, timeout: float, bearer_token: str | None = None
) -> tuple[Any | None, str | None]:
    headers = {"Accept": "application/json", "User-Agent": "pinky-staffing-gather/1"}
    if bearer_token:
        headers["Authorization"] = f"Bearer {bearer_token}"
    try:
        request = Request(
            url,
            headers=headers,
        )
        with urlopen(request, timeout=timeout) as response:  # noqa: S310 - operator-supplied URL
            return json.load(response), None
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        return None, f"btsprobe URL returned invalid JSON: {exc}"
    except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
        return None, f"cannot fetch btsprobe URL: {exc}"


def _valid_day(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _range_days(payload: dict[str, Any]) -> tuple[list[str], str | None]:
    from_value = payload.get("from")
    to_value = payload.get("to")
    if not _valid_day(from_value) or not _valid_day(to_value):
        return [], "from/to must be ISO dates"
    start = date.fromisoformat(from_value)
    end = date.fromisoformat(to_value)
    if start >= end:
        return [], "from must be earlier than exclusive to"
    return [
        (start + timedelta(days=offset)).isoformat() for offset in range((end - start).days)
    ], None


def _typed_day(blocks: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    typed = {block_type: [] for block_type in SCAN_TYPES}
    typed["free"] = []
    for block in blocks:
        typed[block["type"]].append(block)

    occupied = sorted(
        (max(WORKDAY_START, block["h0"]), min(WORKDAY_END, block["h1"]))
        for block in blocks
        if block["h1"] > WORKDAY_START and block["h0"] < WORKDAY_END
    )
    cursor = WORKDAY_START
    for start, end in occupied:
        if start > cursor:
            typed["free"].append({"type": "free", "h0": cursor, "h1": start})
        cursor = max(cursor, end)
    if cursor < WORKDAY_END:
        typed["free"].append({"type": "free", "h0": cursor, "h1": WORKDAY_END})
    return typed


def _copy_member_days(
    result: dict[str, Any], member: str, days: object, range_days: list[str]
) -> dict[str, dict[str, list[dict[str, Any]]]]:
    copied: dict[str, dict[str, list[dict[str, Any]]]] = {}
    if not isinstance(days, dict):
        message = "calendar scan must be an object keyed by ISO date"
        _member_error(result, member, message)
        _source_error(result, "btsprobe", f"{member}: {message}")
        return copied

    unexpected_days = sorted(set(days) - set(range_days))
    if unexpected_days:
        message = f"calendar scan has days outside requested range: {', '.join(unexpected_days)}"
        _member_error(result, member, message)
        _source_error(result, "btsprobe", f"{member}: {message}")

    for day_key in range_days:
        raw_blocks = days.get(day_key, [])
        if not _valid_day(day_key):
            message = f"calendar scan has invalid day key {day_key!r}"
            _member_error(result, member, message)
            _source_error(result, "btsprobe", f"{member}: {message}")
            continue
        if not isinstance(raw_blocks, list):
            message = f"calendar day {day_key} must contain a block list"
            _member_error(result, member, message)
            _source_error(result, "btsprobe", f"{member}: {message}")
            continue

        blocks: list[dict[str, Any]] = []
        day_is_valid = True
        for index, raw_block in enumerate(raw_blocks):
            if not isinstance(raw_block, dict):
                message = f"calendar day {day_key} block {index} must be an object"
                _member_error(result, member, message)
                _source_error(result, "btsprobe", f"{member}: {message}")
                day_is_valid = False
                continue
            block_type = raw_block.get("type")
            h0 = raw_block.get("h0")
            h1 = raw_block.get("h1")
            if block_type not in SCAN_TYPES:
                message = f"calendar day {day_key} block {index} has unknown type {block_type!r}"
                _member_error(result, member, message)
                _source_error(result, "btsprobe", f"{member}: {message}")
                day_is_valid = False
                continue
            if not _is_hour(h0) or not _is_hour(h1) or h0 >= h1:
                message = f"calendar day {day_key} block {index} has invalid h0/h1"
                _member_error(result, member, message)
                _source_error(result, "btsprobe", f"{member}: {message}")
                day_is_valid = False
                continue

            # Copy values exactly. In particular, h0/h1 already carry the
            # scheduler's whole-hour rounding and must never be recomputed.
            blocks.append(dict(raw_block))

        # An empty input list is a successful read with no matches. A malformed
        # non-empty list is omitted so invalid data can never manufacture free
        # time from the blocks that happened to validate.
        if day_is_valid:
            copied[day_key] = _typed_day(blocks)
    return copied


def _copy_group_days(result: dict[str, Any], days: object, range_days: list[str]) -> dict[str, Any]:
    copied: dict[str, Any] = {}
    if not isinstance(days, dict):
        _source_error(result, "btsprobe", "group scan must be an object keyed by ISO date")
        _append_unique(result["group"]["_err"], "group scan is unreadable")
        return copied

    unexpected_days = sorted(set(days) - set(range_days))
    if unexpected_days:
        message = f"group scan has days outside requested range: {', '.join(unexpected_days)}"
        _source_error(result, "btsprobe", message)
        _append_unique(result["group"]["_err"], message)

    for day_key in range_days:
        raw_blocks = days.get(day_key, [])
        if not isinstance(raw_blocks, list):
            message = f"group scan day {day_key!r} must contain a block list"
            _source_error(result, "btsprobe", message)
            _append_unique(result["group"]["_err"], message)
            continue
        blocks: list[dict[str, Any]] = []
        day_is_valid = True
        for index, raw_block in enumerate(raw_blocks):
            if not isinstance(raw_block, dict):
                message = f"group day {day_key} block {index} must be an object"
                _source_error(result, "btsprobe", message)
                _append_unique(result["group"]["_err"], message)
                day_is_valid = False
                continue
            h0 = raw_block.get("h0")
            h1 = raw_block.get("h1")
            if not _is_hour(h0) or not _is_hour(h1) or h0 >= h1:
                message = f"group day {day_key} block {index} has invalid h0/h1"
                _source_error(result, "btsprobe", message)
                _append_unique(result["group"]["_err"], message)
                day_is_valid = False
                continue
            blocks.append(dict(raw_block))
        if day_is_valid:
            copied[day_key] = {"blocks": blocks}
    return copied


def _join_btsprobe(result: dict[str, Any], payload: object | None) -> None:
    if payload is None:
        _source_error(result, "btsprobe", "source payload is unavailable")
        for member in MEMBER_KEYS:
            _member_error(result, member, "calendar unavailable because btsprobe failed")
        _append_unique(result["group"]["_err"], "group calendar unavailable")
        return
    if not isinstance(payload, dict):
        _source_error(result, "btsprobe", "root value must be an object")
        for member in MEMBER_KEYS:
            _member_error(result, member, "calendar unavailable because btsprobe is invalid")
        _append_unique(result["group"]["_err"], "group calendar unavailable")
        return

    range_days, range_error = _range_days(payload)
    result["range"] = {"from": payload.get("from"), "to": payload.get("to")}
    if range_error:
        _source_error(result, "btsprobe", range_error)
    access = payload.get("access")
    scan = payload.get("scan")
    if not isinstance(access, dict):
        _source_error(result, "btsprobe", "access diagnostics must be an object")
        access = {}
    if not isinstance(scan, dict):
        _source_error(result, "btsprobe", "scan must be an object")
        scan = {}
    raw_members = scan.get("members")
    if not isinstance(raw_members, dict):
        _source_error(result, "btsprobe", "scan.members must be an object")
        raw_members = {}

    unknown_members = sorted(set(raw_members) - set(MEMBER_KEYS))
    if unknown_members:
        _source_error(
            result,
            "btsprobe",
            f"unknown member keys in scan: {', '.join(unknown_members)}",
        )

    for member in MEMBER_KEYS:
        if access.get(member) is not True:
            message = "calendar access is not true; refusing to infer availability"
            _member_error(result, member, message)
            _source_error(result, "btsprobe", f"{member}: {message}")
            continue
        if member not in raw_members:
            message = "calendar is absent from scan.members"
            _member_error(result, member, message)
            _source_error(result, "btsprobe", f"{member}: {message}")
            continue
        result["members"][member]["days"] = _copy_member_days(
            result, member, raw_members[member], range_days
        )

    if access.get("group") is not True:
        message = "group calendar access is not true"
        _source_error(result, "btsprobe", message)
        _append_unique(result["group"]["_err"], message)
    elif "group" not in scan:
        message = "group calendar is absent from scan"
        _source_error(result, "btsprobe", message)
        _append_unique(result["group"]["_err"], message)
    else:
        result["group"]["days"] = _copy_group_days(result, scan["group"], range_days)


def _join_board(result: dict[str, Any], payload: object | None) -> None:
    if payload is None:
        _source_error(result, "board", "source payload is unavailable")
        for member in MEMBER_KEYS:
            message = "build load unavailable because board source failed"
            result["members"][member]["loads"]["build"] = {
                "status": "error",
                "_err": [message],
            }
            _member_error(result, member, message)
        return
    if not isinstance(payload, dict) or not isinstance(payload.get("members"), list):
        _source_error(result, "board", "members must be a list")
        _join_board(result, None)
        return

    rows: dict[str, dict[str, Any]] = {}
    for index, raw_row in enumerate(payload["members"]):
        if not isinstance(raw_row, dict) or not isinstance(raw_row.get("key"), str):
            _source_error(result, "board", f"members[{index}] has no string key")
            continue
        key = raw_row["key"]
        if key not in MEMBER_KEYS:
            _source_error(result, "board", f"unknown member key {key!r}")
            continue
        if key in rows:
            _source_error(result, "board", f"duplicate member key {key!r}")
            continue
        rows[key] = raw_row

    for member in MEMBER_KEYS:
        row = rows.get(member)
        if row is None:
            message = "member is absent from board"
            _source_error(result, "board", f"{member}: {message}")
            _member_error(result, member, message)
            result["members"][member]["loads"]["build"] = {
                "status": "error",
                "_err": [message],
            }
            continue
        load = row.get("load")
        provisional = row.get("provisional")
        weight = row.get("weight")
        if (
            not _is_number(load)
            or load < 0
            or not _is_number(provisional)
            or provisional < 0
            or not _is_number(weight)
            or weight <= 0
        ):
            message = "load, provisional, and positive weight must be finite numbers"
            _source_error(result, "board", f"{member}: {message}")
            _member_error(result, member, message)
            result["members"][member]["loads"]["build"] = {
                "status": "error",
                "_err": [message],
            }
            continue
        result["members"][member]["loads"]["build"] = {
            "status": "present",
            "load": load,
            "provisional": provisional,
            "effective": load + provisional,
            "weight": weight,
            "weighted_rank": load / weight,
        }


def _join_desk(result: dict[str, Any], payload: object | None, *, supplied: bool) -> None:
    if not supplied:
        result["sources"]["desk"]["status"] = "absent"
        for member in MEMBER_KEYS:
            result["members"][member]["loads"]["desk"] = {"status": "absent"}
        return

    result["sources"]["desk"]["status"] = "present"
    if payload is None:
        _source_error(result, "desk", "supplied source payload is unavailable")
        for member in MEMBER_KEYS:
            message = "Desk load unavailable because supplied source failed"
            result["members"][member]["loads"]["desk"] = {
                "status": "error",
                "_err": [message],
            }
            _member_error(result, member, message)
        return
    if not isinstance(payload, dict):
        _source_error(result, "desk", "root value must be an object keyed by member key")
        _join_desk(result, None, supplied=True)
        return

    unknown_keys = sorted(set(payload) - set(MEMBER_KEYS))
    if unknown_keys:
        _source_error(result, "desk", f"unknown member keys: {', '.join(unknown_keys)}")

    for member in MEMBER_KEYS:
        if member not in payload:
            message = "member is absent from supplied Desk counts"
            _source_error(result, "desk", f"{member}: {message}")
            _member_error(result, member, message)
            result["members"][member]["loads"]["desk"] = {
                "status": "error",
                "_err": [message],
            }
            continue
        count = payload[member]
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            message = "open count must be a non-negative integer"
            _source_error(result, "desk", f"{member}: {message}")
            _member_error(result, member, message)
            result["members"][member]["loads"]["desk"] = {
                "status": "error",
                "_err": [message],
            }
            continue
        result["members"][member]["loads"]["desk"] = {
            "status": "present",
            "open_count": count,
        }


def build_matrix(
    btsprobe: object | None,
    board: object | None,
    desk: object | None = None,
    *,
    desk_supplied: bool = False,
    source_errors: dict[str, list[str]] | None = None,
    source_modes: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Validate and join already-decoded source values."""
    source_errors = source_errors or {}
    source_modes = source_modes or {}
    result: dict[str, Any] = {
        "ok": False,
        "range": {"from": None, "to": None},
        "members": {member: {"days": {}, "loads": {}, "_err": []} for member in MEMBER_KEYS},
        "group": {"days": {}, "_err": []},
        "sources": {
            "btsprobe": _source_state(source_modes.get("btsprobe", "value")),
            "board": _source_state(source_modes.get("board", "value")),
            "desk": _source_state(source_modes.get("desk", "absent")),
        },
        "_err": [],
    }
    for source, messages in source_errors.items():
        for message in messages:
            _source_error(result, source, message)

    _join_btsprobe(result, btsprobe)
    _join_board(result, board)
    _join_desk(result, desk, supplied=desk_supplied)

    for source in ("btsprobe", "board", "desk"):
        for message in result["sources"][source]["_err"]:
            result["_err"].append(f"{source}: {message}")
    result["ok"] = not result["_err"]
    return result


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    btsprobe_source = parser.add_mutually_exclusive_group(required=True)
    btsprobe_source.add_argument(
        "--btsprobe-file", type=Path, help="Read previously fetched btsprobe JSON"
    )
    btsprobe_source.add_argument(
        "--btsprobe-url", help="Fetch btsprobe JSON from this complete URL"
    )
    parser.add_argument("--board-file", type=Path, required=True)
    parser.add_argument(
        "--desk-file",
        type=Path,
        help="Optional JSON object mapping exact member keys to open counts",
    )
    parser.add_argument("--timeout", type=float, default=30.0, help="URL timeout in seconds")
    parser.add_argument(
        "--btsprobe-token-env",
        default="BTSPROBE_TOKEN",
        help="Environment variable containing the optional btsprobe Bearer token",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    source_errors: dict[str, list[str]] = {"btsprobe": [], "board": [], "desk": []}

    if args.btsprobe_file is not None:
        btsprobe, error = _read_json_file(args.btsprobe_file)
        btsprobe_mode = "file"
    else:
        btsprobe, error = _read_json_url(
            args.btsprobe_url,
            args.timeout,
            os.environ.get(args.btsprobe_token_env),
        )
        btsprobe_mode = "url"
    if error:
        source_errors["btsprobe"].append(error)

    board, error = _read_json_file(args.board_file)
    if error:
        source_errors["board"].append(error)

    desk = None
    desk_supplied = args.desk_file is not None
    if desk_supplied:
        desk, error = _read_json_file(args.desk_file)
        if error:
            source_errors["desk"].append(error)

    result = build_matrix(
        btsprobe,
        board,
        desk,
        desk_supplied=desk_supplied,
        source_errors=source_errors,
        source_modes={
            "btsprobe": btsprobe_mode,
            "board": "file",
            "desk": "file" if desk_supplied else "absent",
        },
    )
    json.dump(result, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
