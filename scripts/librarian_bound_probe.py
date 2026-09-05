#!/usr/bin/env python3
"""Opt-in live regression probe for the librarian's production SDK configuration.

Run with PINKY_LIBRARIAN_PROBE=1 and an explicit agent directory and receipt path.
The probe uses a fresh one-shot session and only requests diagnostic operations.
Receipts contain private tool results and must not be posted publicly.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from importlib.metadata import version
from pathlib import Path
from unittest.mock import patch

import claude_agent_sdk
from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)

from pinky_daemon.librarian_runner import LibrarianRunner
from pinky_daemon.sdk_runner import SDKRunner

PROMPT = (
    "Do all four, report each result verbatim: "
    "(1) run `echo PROBE-BASH` with the Bash tool; "
    "(2) spawn a subagent via Agent that replies 'PROBE-AGENT'; "
    "(3) call mcp__pinky-self__who_am_i; "
    "(4) call mcp__pinky-self__kb_stats."
)
EXPECTED_TOOLS = frozenset({
    "Read", "Glob", "Grep", "ToolSearch",
    "mcp__pinky-self__kb_search", "mcp__pinky-self__kb_get_wiki",
    "mcp__pinky-self__kb_stats", "mcp__pinky-self__kb_save_wiki",
    "mcp__pinky-self__kb_delete_wiki",
})


def evaluate(receipt: dict, expectation: str) -> dict[str, bool]:
    """Separate attempted calls, explicit denials, and successful tool results."""
    results = {item["tool_use_id"]: item for item in receipt["tool_results"]}
    denial_keys = {
        (item.get("tool_use_id"), item.get("tool_name"))
        for item in receipt.get("permission_denials", [])
    }
    executed = []
    denied = []
    unresolved = []
    for attempt in receipt["tool_uses"]:
        result = results.get(attempt["id"])
        if result is None:
            unresolved.append(attempt)
            continue
        # A handler may execute and then return an error saying "permission
        # denied". Only the SDK's correlated permission record proves denial;
        # tool-result text is retained as explanation, never as evidence.
        is_denial = result["is_error"] and (
            attempt["id"], attempt["name"]
        ) in denial_keys
        if is_denial:
            denied.append({**attempt, "reason": result["content"]})
        elif not result["is_error"]:
            executed.append(attempt)
        else:
            unresolved.append(attempt)
    receipt["executed_tools"] = executed
    receipt["denied_tools"] = denied
    receipt["unresolved_tools"] = unresolved
    attempted_names = {item["name"] for item in receipt["tool_uses"]}
    executed_names = {item["name"] for item in executed}
    denied_ids = {item["id"] for item in denied}
    if expectation == "red":
        return {
            "bash_attempted": "Bash" in attempted_names,
            "agent_attempted": "Agent" in attempted_names,
            "identity_executed": "mcp__pinky-self__who_am_i" in executed_names,
            "kb_stats_executed": "mcp__pinky-self__kb_stats" in executed_names,
        }
    return {
        "no_bash_or_agent_attempt": not (attempted_names & {"Bash", "Agent"}),
        "every_outside_attempt_denied": all(
            item["id"] in denied_ids
            for item in receipt["tool_uses"] if item["name"] not in EXPECTED_TOOLS
        ),
        "identity_denied": any(item["name"] == "mcp__pinky-self__who_am_i" for item in denied),
        "kb_stats_executed": "mcp__pinky-self__kb_stats" in executed_names,
        "executions_within_bound": executed_names <= EXPECTED_TOOLS,
        "all_attempts_resolved": not unresolved,
    }


async def probe(agent_dir: Path, receipt: dict, timeout: float) -> None:
    config = LibrarianRunner._build_sdk_config(str(agent_dir), "Run the requested tool diagnostics.")
    if config is None:
        raise RuntimeError("production config builder refused the MCP configuration")

    receipt["config"] = {
        "model": config.model,
        "tools": getattr(config, "tools", None),
        "permission_mode": config.permission_mode,
        "allowed_tools": list(config.allowed_tools),
        "mcp_servers": list(config.mcp_servers),
        "strict_mcp_config": getattr(config, "strict_mcp_config", False),
        "setting_sources": getattr(config, "setting_sources", None),
        "hook_events": list(getattr(config, "hooks", None) or {}),
    }
    original_query = claude_agent_sdk.query

    async def recording_query(*, prompt, options):
        stream = original_query(prompt=prompt, options=options)
        try:
            async for message in stream:
                content = getattr(message, "content", [])
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, ToolUseBlock):
                            receipt["tool_uses"].append({"id": block.id, "name": block.name})
                        elif isinstance(block, ToolResultBlock):
                            receipt["tool_results"].append({
                                "tool_use_id": block.tool_use_id,
                                "is_error": block.is_error,
                                "content": block.content,
                            })
                        elif isinstance(message, AssistantMessage) and isinstance(block, TextBlock):
                            receipt["assistant_text"].append(block.text)
                if isinstance(message, ResultMessage):
                    receipt["permission_denials"].extend(message.permission_denials or [])
                    receipt["sdk_result"] = {
                        "is_error": message.is_error,
                        "subtype": message.subtype,
                        "result": message.result,
                        "session_id": message.session_id,
                        "cost_usd": message.total_cost_usd,
                    }
                yield message
        finally:
            await stream.aclose()

    with patch.object(claude_agent_sdk, "query", recording_query):
        async with asyncio.timeout(timeout):
            result = await SDKRunner(config, agent_name=f"{agent_dir.name}-librarian").run(PROMPT)
    receipt["run_result"] = {
        "exit_code": result.exit_code,
        "error": result.error,
        "output": result.output,
    }
    if not result.ok or receipt.get("sdk_result", {}).get("is_error", True):
        raise RuntimeError("SDK run failed; inspect the receipt")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent-dir", required=True, type=Path)
    parser.add_argument("--receipt", required=True, type=Path)
    parser.add_argument("--expect", required=True, choices=["red", "green"])
    parser.add_argument("--timeout", type=float, default=180)
    args = parser.parse_args()
    if os.environ.get("PINKY_LIBRARIAN_PROBE") != "1":
        parser.error("set PINKY_LIBRARIAN_PROBE=1 to opt in to a live SDK session")
    if not args.agent_dir.is_dir():
        parser.error("--agent-dir must be an existing agent directory")
    if args.timeout <= 0:
        parser.error("--timeout must be positive")

    root = Path(__file__).resolve().parents[1]
    receipt = {
        "head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip(),
        "worktree_status": subprocess.check_output(
            ["git", "status", "--porcelain"], cwd=root, text=True
        ),
        "python": sys.version,
        "sdk_version": version("claude-agent-sdk"),
        "cli_version": subprocess.check_output(
            [str(Path(claude_agent_sdk.__file__).parent / "_bundled/claude"), "--version"],
            text=True,
        ).strip(),
        "agent_dir": str(args.agent_dir.resolve()),
        "started_at": datetime.now(timezone.utc).isoformat(),
        "prompt": PROMPT,
        "tool_uses": [],
        "tool_results": [],
        "assistant_text": [],
        "permission_denials": [],
        "expectation": args.expect,
    }
    args.receipt.parent.mkdir(parents=True, exist_ok=True)
    with args.receipt.open("x") as output:
        os.chmod(args.receipt, 0o600)
        json.dump(receipt, output, indent=2)
    status = 0
    try:
        asyncio.run(probe(args.agent_dir.resolve(), receipt, args.timeout))
        receipt["checks"] = evaluate(receipt, args.expect)
        if not all(receipt["checks"].values()):
            status = 1
    except Exception as exc:
        receipt["error"] = f"{type(exc).__name__}: {exc}"
        status = 1
    finally:
        receipt["finished_at"] = datetime.now(timezone.utc).isoformat()
        with args.receipt.open("w") as output:
            json.dump(receipt, output, indent=2)
            output.write("\n")
        print(f"Receipt: {args.receipt}")
        print("Tool attempts:", ", ".join(item["name"] for item in receipt["tool_uses"]))
        print("Checks:", json.dumps(receipt.get("checks", {})))
    return status


if __name__ == "__main__":
    raise SystemExit(main())
