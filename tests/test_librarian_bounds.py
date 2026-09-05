"""Independent bounds on the one-shot curation session."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from pinky_daemon import librarian_runner
from pinky_daemon.claude_runner import RunResult
from pinky_daemon.kb_store import KBStore
from pinky_daemon.librarian_runner import LibrarianRunner

BUILTINS = ["Read", "Glob", "Grep", "ToolSearch"]
ALLOWED = (
    "Read", "Glob", "Grep", "ToolSearch",
    "mcp__pinky-self__kb_search",
    "mcp__pinky-self__kb_get_wiki",
    "mcp__pinky-self__kb_stats",
    "mcp__pinky-self__kb_save_wiki",
    "mcp__pinky-self__kb_delete_wiki",
)
SELF_SERVER = {"type": "sse", "url": "http://127.0.0.1:1/self/sse"}


@pytest.fixture
def bounded_runner(tmp_path):
    (tmp_path / ".mcp.json").write_text(json.dumps({"mcpServers": {
        "pinky-self": SELF_SERVER,
        "unrelated": {"command": "must-not-run"},
    }}))
    kb = KBStore(data_dir=tmp_path)
    return LibrarianRunner(kb, db_path=tmp_path / "librarian_state.db")


def test_librarian_config_is_bounded(bounded_runner, tmp_path):
    cfg = bounded_runner._build_sdk_config(str(tmp_path), "sys")

    assert cfg is not None
    assert cfg.tools == BUILTINS
    assert cfg.permission_mode == "dontAsk"
    assert librarian_runner._LIBRARIAN_ALLOWED_TOOLS == ALLOWED
    assert set(cfg.allowed_tools) == set(ALLOWED)
    assert len(cfg.allowed_tools) == 9
    assert list(cfg.mcp_servers) == ["pinky-self"]
    assert cfg.mcp_servers["pinky-self"] == SELF_SERVER
    assert cfg.strict_mcp_config is True
    assert cfg.setting_sources == []
    assert set(cfg.hooks) == {"PreToolUse"}
    matchers = cfg.hooks["PreToolUse"]
    assert len(matchers) == 1
    assert matchers[0].matcher is None
    assert matchers[0].hooks == [librarian_runner._librarian_pretooluse_guard]
    assert cfg.working_dir == str(tmp_path)
    assert cfg.system_prompt == "sys"
    assert cfg.model == "sonnet"
    assert cfg.max_turns == 50


@pytest.mark.parametrize("contents", [
    None, '{"mcpServers": {"unrelated": {}}}', '{broken', '[]',
    '{"mcpServers": []}', '{"mcpServers": null}', '{"mcpServers": {"pinky-self": null}}',
    '{"mcpServers": {"pinky-self": {}}}',
], ids=["missing", "missing-entry", "malformed", "non-object", "invalid-servers", "null-servers",
        "null-entry", "empty-entry"])
async def test_missing_pinky_self_entry_fails_closed(
    bounded_runner, tmp_path, monkeypatch, capsys, contents
):
    config_path = tmp_path / ".mcp.json"
    config_path.unlink()
    if contents is not None:
        config_path.write_text(contents)
    source = bounded_runner._kb.ingest(title="Note", content="New source", filed_by="test")
    bounded_runner._save_state("test", {}, last_run_at="2000-01-01T00:00:00+00:00")
    state_before = bounded_runner.get_state("test")
    sdk = MagicMock()
    sdk.return_value.run = AsyncMock(return_value=RunResult(output="Curated.", exit_code=0))
    monkeypatch.setattr(librarian_runner, "SDKRunner", sdk)

    stats = await bounded_runner.run("test", SimpleNamespace(working_dir=str(tmp_path)))

    assert stats.get("skipped") is True
    assert "cannot bound MCP surface" in stats["error"]
    sdk.assert_not_called()
    assert bounded_runner.get_state("test") == state_before
    assert bounded_runner._get_processed_hash(source.id) is None
    assert bounded_runner.get_run_history("test") == []
    logs = capsys.readouterr().err
    assert "ERROR librarian[" in logs
    assert "cannot bound MCP surface" in logs
    assert "; skipping run" in logs


@pytest.mark.parametrize("tool_name", [
    "Bash", "Agent", "mcp__pinky-self__update_and_restart",
    "mcp__pinky-self__kb_searchX", "mcp__pinky-self__who_am_i", "", *ALLOWED,
])
async def test_pretooluse_guard(bounded_runner, tmp_path, tool_name):
    guard = librarian_runner._librarian_pretooluse_guard
    cfg = bounded_runner._build_sdk_config(str(tmp_path), "sys")
    assert cfg.hooks["PreToolUse"][0].hooks == [guard]
    result = await guard({"tool_name": tool_name}, None, {})

    if tool_name in ALLOWED:
        assert result == {}
    else:
        assert result == {"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": "Outside librarian tool set",
        }}
