"""Tests for LibrarianRunner watermark and failure handling.

The librarian must never advance last_run_at past sources it did not
actually process: a failed SDK run keeps the watermark (and records no
body hashes), and budget truncation only advances the watermark to the
newest source actually included in the prompt.
"""

from __future__ import annotations

import json
import time

import pytest

from pinky_daemon.claude_runner import RunResult
from pinky_daemon.kb_store import KBStore
from pinky_daemon.librarian_runner import LibrarianRunner


class _StubSDKRunner:
    """Stands in for SDKRunner inside LibrarianRunner.run."""

    result = None
    prompts: list = []
    last_config = None

    def __init__(self, config, agent_name=""):
        self.config = config
        self.agent_name = agent_name
        type(self).last_config = config

    async def run(self, prompt):
        type(self).prompts.append(prompt)
        return type(self).result


class _AgentConfig:
    def __init__(self, working_dir: str) -> None:
        self.working_dir = working_dir
        self.model = "sonnet"


@pytest.fixture
def kb(tmp_path):
    return KBStore(data_dir=tmp_path)


@pytest.fixture
def runner(kb, tmp_path):
    return LibrarianRunner(kb, db_path=tmp_path / "librarian_state.db")


@pytest.fixture
def stub_sdk(monkeypatch):
    _StubSDKRunner.prompts = []
    _StubSDKRunner.last_config = None
    monkeypatch.setattr("pinky_daemon.librarian_runner.SDKRunner", _StubSDKRunner)
    return _StubSDKRunner


class TestLibrarianFailureHandling:
    @pytest.mark.asyncio
    async def test_failed_run_keeps_watermark_and_hashes(
        self, kb, runner, tmp_path, stub_sdk
    ):
        src = kb.ingest(title="Note", content="hello world", filed_by="brad")
        cfg = _AgentConfig(str(tmp_path))

        stub_sdk.result = RunResult(output="", exit_code=1, error="boom")
        stats = await runner.run("ivan", cfg)

        assert stats.get("failed") is True
        assert "Librarian run failed" in stats["summary"]
        # Watermark untouched: the source is re-fetched on the next run
        assert runner._get_last_run_at("ivan") is None
        assert runner.has_new_sources("ivan") is True
        # Body hash not recorded: the retry is not hash-skipped
        assert runner._get_processed_hash(src.id) is None

        # A later successful run processes the same source
        stub_sdk.result = RunResult(output="Curated 1 source.", exit_code=0)
        stats2 = await runner.run("ivan", cfg)

        assert stats2["source_ids"] == [src.id]
        assert runner.has_new_sources("ivan") is False
        assert runner._get_processed_hash(src.id) is not None


class TestLibrarianAllUnchanged:
    @pytest.mark.asyncio
    async def test_all_unchanged_advances_watermark(self, kb, runner, tmp_path, stub_sdk):
        """Sources whose body hash is already recorded are skipped without an
        SDK run, and the watermark moves past them so they are not re-listed
        (and re-hashed) on every subsequent run."""
        src = kb.ingest(title="Note", content="hello world", filed_by="brad")
        content = kb.get_raw_content(src.id)
        runner._save_processed_hashes([(src.id, runner._body_hash(content))])
        cfg = _AgentConfig(str(tmp_path))
        stub_sdk.result = RunResult(output="Curated.", exit_code=0)

        stats = await runner.run("ivan", cfg)

        assert stats == {"sources_processed": 0, "skipped": True}
        assert stub_sdk.prompts == []  # no SDK run for unchanged content
        assert runner._get_last_run_at("ivan") == src.filed_at
        assert runner.has_new_sources("ivan") is False


class TestLibrarianTruncationWatermark:
    @pytest.mark.asyncio
    async def test_truncated_sources_survive_to_next_run(
        self, kb, runner, tmp_path, stub_sdk, monkeypatch
    ):
        monkeypatch.setattr("pinky_daemon.librarian_runner._MAX_SOURCE_CHARS", 800)
        old = kb.ingest(title="Old", content="x" * 100, filed_by="brad")
        time.sleep(0.005)  # distinct filed_at for a strict > watermark
        new = kb.ingest(title="New", content="y" * 2000, filed_by="brad")
        cfg = _AgentConfig(str(tmp_path))
        stub_sdk.result = RunResult(output="Curated.", exit_code=0)

        stats = await runner.run("ivan", cfg)

        # Oldest-first: only the old source fit the budget
        assert stats["source_ids"] == [old.id]
        assert runner._get_last_run_at("ivan") == old.filed_at

        # The truncated source is picked up by the next run
        stats2 = await runner.run("ivan", cfg)
        assert stats2["source_ids"] == [new.id]
        assert runner._get_last_run_at("ivan") == new.filed_at

    @pytest.mark.asyncio
    async def test_single_oversized_source_still_progresses(
        self, kb, runner, tmp_path, stub_sdk, monkeypatch
    ):
        monkeypatch.setattr("pinky_daemon.librarian_runner._MAX_SOURCE_CHARS", 800)
        big = kb.ingest(title="Big", content="z" * 5000, filed_by="brad")
        cfg = _AgentConfig(str(tmp_path))
        stub_sdk.result = RunResult(output="Curated.", exit_code=0)

        stats = await runner.run("ivan", cfg)

        # Included truncated, watermark moved past it: no livelock
        assert stats["source_ids"] == [big.id]
        assert runner._get_last_run_at("ivan") == big.filed_at
        assert runner.has_new_sources("ivan") is False


class TestLibrarianMcpServersWiring:
    """Without an explicit mcp_servers config, SDKRunner never forwards it to
    the SDK's ClaudeAgentOptions (it only sets the attribute when the dict is
    truthy) — so the librarian session needs to load .mcp.json itself, the
    same fallback streaming_session.py uses. Regression coverage: a librarian
    session with an empty mcp_servers dict has zero MCP tools available, so
    it can never call kb_search / kb_save_wiki / etc.
    """

    @pytest.mark.asyncio
    async def test_loads_mcp_servers_from_mcp_json(self, kb, runner, tmp_path, stub_sdk):
        fake_servers = {"pinky-self": {"type": "sse", "url": "http://x"}}
        (tmp_path / ".mcp.json").write_text(json.dumps({"mcpServers": fake_servers}))

        kb.ingest(title="Note", content="hello world", filed_by="brad")
        cfg = _AgentConfig(str(tmp_path))
        stub_sdk.result = RunResult(output="Curated.", exit_code=0)

        await runner.run("ivan", cfg)

        assert stub_sdk.last_config is not None
        assert stub_sdk.last_config.mcp_servers == fake_servers

    @pytest.mark.asyncio
    async def test_no_mcp_json_yields_empty_mcp_servers(self, kb, runner, tmp_path, stub_sdk):
        # No .mcp.json written into the working dir.
        kb.ingest(title="Note", content="hello world", filed_by="brad")
        cfg = _AgentConfig(str(tmp_path))
        stub_sdk.result = RunResult(output="Curated.", exit_code=0)

        await runner.run("ivan", cfg)

        assert stub_sdk.last_config is not None
        assert not stub_sdk.last_config.mcp_servers
