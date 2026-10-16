"""Shared wiki content is consumed through a bounded knowledge-base surface."""

import json
from pathlib import Path

import httpx
import pytest

from pinky_daemon import librarian_runner
from pinky_daemon.agent_registry import AgentRegistry
from pinky_daemon.kb_store import KBStore
from pinky_daemon.librarian_prompt import LIBRARIAN_SYSTEM_PROMPT
from tests.isolated_policy_support import daemon as daemon
from tests.isolated_policy_support import signed

pytestmark = pytest.mark.real_auth

CURATION_TOOLS = (
    "ToolSearch",
    "mcp__pinky-self__kb_search",
    "mcp__pinky-self__kb_get_wiki",
    "mcp__pinky-self__kb_stats",
    "mcp__pinky-self__kb_save_wiki",
    "mcp__pinky-self__kb_delete_wiki",
)


def test_librarian_exposes_only_knowledge_base_tools(tmp_path):
    server = {"type": "sse", "url": "http://127.0.0.1:1/self/sse"}
    (tmp_path / ".mcp.json").write_text(json.dumps({"mcpServers": {"pinky-self": server}}))
    config = librarian_runner.LibrarianRunner._build_sdk_config(str(tmp_path), "curate")

    assert config is not None
    assert config.tools == ["ToolSearch"]
    assert librarian_runner._LIBRARIAN_ALLOWED_TOOLS == CURATION_TOOLS
    assert config.allowed_tools == list(CURATION_TOOLS)


@pytest.mark.parametrize("tool", ["Read", "Glob", "Grep"])
async def test_librarian_guard_denies_file_tools(tool):
    result = await librarian_runner._librarian_pretooluse_guard({"tool_name": tool}, None, {})
    assert result == {"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": "Outside librarian tool set",
    }}


def test_librarian_prompt_does_not_offer_file_access():
    assert "You also have `Read`, `Glob`, and `Grep` for file access if needed." not in (
        LIBRARIAN_SYSTEM_PROMPT
    )


@pytest.mark.parametrize("stored", ["relative", "absolute", "symlink", "sibling"])
def test_reindex_skips_outside_wiki_files_and_keeps_their_rows(
    tmp_path, monkeypatch, capsys, stored
):
    kb = KBStore(tmp_path / "data")
    outside = tmp_path / "outside.md"
    outside.write_text("outside_canary_text")
    kb.save_wiki("reference", "Reference", "safe")
    kb.save_wiki("safe", "Safe", "safe page body")
    if stored == "relative":
        file_path = "../../outside.md"
    elif stored == "absolute":
        file_path = str(outside)
    elif stored == "sibling":
        file_path = "wiki-adjacent/outside.md"
        outside = kb.kb_dir / file_path
        outside.parent.mkdir()
        outside.write_text("outside_canary_text")
    else:
        file_path = "wiki/alias.md"
        (kb.kb_dir / file_path).symlink_to(outside)
    with kb._conn() as conn:
        conn.execute("UPDATE wiki_pages SET file_path=? WHERE slug='reference'", (file_path,))
    assert kb.get_wiki_content("reference") is None
    reads = []
    original_read = Path.read_text

    def tracked_read(path, *args, **kwargs):
        if path.resolve() == outside.resolve():
            reads.append(path)
        return original_read(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", tracked_read)
    capsys.readouterr()
    result = kb.reindex()

    assert reads == []
    assert result == {"raw": 0, "wiki": 1}
    assert not kb.search("outside_canary_text", scope="wiki")
    assert kb.get_wiki("reference") is not None
    assert original_read(outside) == "outside_canary_text"
    assert kb.get_wiki_content("safe").endswith("safe page body")
    lines = capsys.readouterr().err.splitlines()
    assert [line for line in lines if "outside wiki" in line] == [
        "[KB] Skipping file outside wiki directory during reindex"
    ]


async def test_wiki_route_keeps_non_slug_value_errors_as_server_errors(daemon):
    d = daemon()
    path = "/kb/wiki/nested/page"
    content = json.dumps({"title": "Reference", "content": "body\ud800"}).encode("ascii")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=d.app, raise_app_exceptions=False),
        base_url="http://test",
    ) as client:
        response = await client.put(
            path,
            headers={**signed(d, "PUT", path, "normal"), "content-type": "application/json"},
            content=content,
        )
    assert response.status_code == 500, (response.status_code, response.text)


@pytest.mark.parametrize("slug", ["a//b", "a/", "a//"])
def test_wiki_slugs_refuse_empty_segments(tmp_path, slug):
    kb = KBStore(tmp_path)
    with pytest.raises(ValueError, match="invalid wiki slug"):
        kb.save_wiki(slug, "Reference", "body")
    assert kb.list_wiki() == []
    assert not list(kb.wiki_dir.rglob("*.md"))
    page = kb.save_wiki("topics/claude-code", "Reference", "valid body")
    assert kb.get_wiki_content(page.slug).endswith("valid body")


@pytest.mark.parametrize("control", ["\x7f", "\x85"], ids=["del", "c1"])
def test_wiki_slugs_refuse_the_full_control_character_class(tmp_path, control):
    kb = KBStore(tmp_path)
    with pytest.raises(ValueError, match="invalid wiki slug"):
        kb.save_wiki(f"a{control}b", "Reference", "body")
    assert kb.list_wiki() == []
    assert not list(kb.wiki_dir.rglob("*.md"))


def test_wiki_symlink_loop_has_a_fixed_slug_error(tmp_path):
    kb = KBStore(tmp_path)
    (kb.wiki_dir / "loop.md").symlink_to("loop.md")
    with pytest.raises(Exception) as raised:
        kb.save_wiki("loop", "Reference", "body")
    assert type(raised.value) is ValueError
    assert str(raised.value) == "invalid wiki slug"
    assert kb.list_wiki() == []


def test_wiki_target_may_be_a_new_nested_file(tmp_path):
    kb = KBStore(tmp_path)
    target = kb.wiki_dir / "topics" / "new-page.md"
    assert kb._resolve_wiki_file(target) == target.resolve()
    assert not target.exists()


@pytest.mark.parametrize("obstacle", ["loop", "file"])
def test_unusable_stored_wiki_paths_are_skipped(tmp_path, capsys, obstacle):
    kb = KBStore(tmp_path)
    kb.save_wiki("reference", "Reference", "body")
    blocked = kb.wiki_dir / "blocked"
    if obstacle == "loop":
        blocked.symlink_to("blocked")
        file_path = "wiki/blocked"
    else:
        blocked.write_text("keep this file")
        file_path = "wiki/blocked/child.md"
    with kb._conn() as conn:
        conn.execute("UPDATE wiki_pages SET file_path=? WHERE slug='reference'", (file_path,))

    try:
        content = kb.get_wiki_content("reference")
    except OSError as exc:
        content = type(exc).__name__
    assert content is None

    capsys.readouterr()
    try:
        result = kb.reindex()
    except OSError as exc:
        result = type(exc).__name__
    assert result == {"raw": 0, "wiki": 0}
    assert [line for line in capsys.readouterr().err.splitlines() if "outside wiki" in line] == [
        "[KB] Skipping file outside wiki directory during reindex"
    ]
    assert kb.get_wiki("reference") is not None

    assert kb.delete_wiki("reference") is True
    assert kb.get_wiki("reference") is None
    assert [line for line in capsys.readouterr().err.splitlines() if "outside wiki" in line] == [
        "[KB] Skipping file outside wiki directory during delete"
    ]
    if obstacle == "loop":
        assert blocked.is_symlink()
    else:
        assert blocked.read_text() == "keep this file"


def test_deeply_nested_stored_grants_fail_closed():
    raw = "[" * 3000 + '"kb_writer"' + "]" * 3000
    try:
        result = AgentRegistry._decode_isolated_grants(raw)
    except RecursionError:
        result = "decoder raised RecursionError"
    assert result == []


def test_wiki_save_uses_the_checked_target_after_a_final_link_changes(tmp_path, monkeypatch):
    kb = KBStore(tmp_path)
    inside = kb.wiki_dir / "inside.md"
    inside.write_text("inside original")
    outside = tmp_path / "outside.md"
    outside.write_text("outside original")
    link = kb.wiki_dir / "alias.md"
    link.symlink_to(inside)
    original_write = Path.write_text

    def retarget_then_write(path, *args, **kwargs):
        link.unlink()
        link.symlink_to(outside)
        return original_write(path, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", retarget_then_write)
    kb.save_wiki("alias", "Reference", "replacement body")
    assert outside.read_text() == "outside original"
    assert inside.read_text().endswith("replacement body")
