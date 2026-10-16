"""Every wiki file access stays within the knowledge base's wiki directory."""

from pathlib import Path

import httpx
import pytest

from pinky_daemon.kb_store import KBStore
from pinky_daemon.routes import kb as kb_routes
from pinky_daemon.wiki_builder import save_wiki_pages
from tests.isolated_policy_support import daemon as daemon
from tests.isolated_policy_support import signed

pytestmark = pytest.mark.real_auth
REFUSED_SLUGS = ["../x", "a/../../x", "a/../b", ".", "a/./b", "/abs", "a\\b", "a\x00b", "a\x1fb", ""]


def outside_inventory(root, kb_dir):
    return sorted(str(p.relative_to(root)) for p in root.rglob("*") if not p.is_relative_to(kb_dir))


@pytest.mark.parametrize("slug", REFUSED_SLUGS)
def test_wiki_save_refuses_invalid_slugs_before_writing(tmp_path, slug):
    kb = KBStore(str(tmp_path / "kb"))
    before = outside_inventory(tmp_path, kb.kb_dir)
    with pytest.raises(ValueError):
        kb.save_wiki(slug, "Unsafe page", "A body")
    assert outside_inventory(tmp_path, kb.kb_dir) == before
    assert kb.list_wiki() == []
    assert not list((kb.kb_dir / "wiki").rglob("*.md"))
    page = kb.save_wiki("topics/claude-code", "A reference", "Valid content")
    assert (kb.kb_dir / page.file_path).is_file()


def test_wiki_save_refuses_a_symlink_escape(tmp_path):
    kb = KBStore(str(tmp_path / "kb"))
    outside = tmp_path / "outside"
    outside.mkdir()
    (kb.kb_dir / "wiki" / "alias").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError):
        kb.save_wiki("alias/page", "Unsafe page", "A body")
    assert list(outside.iterdir()) == []
    assert kb.list_wiki() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("slug", ["%2E%2E/x", "a/%2E%2E/%2E%2E/x"])
async def test_wiki_route_returns_a_fixed_bad_slug_error(daemon, slug):
    d = daemon()
    path = f"/kb/wiki/{slug}"
    before = outside_inventory(d.root, kb_routes._kb.kb_dir)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=d.app), base_url="http://test") as c:
        response = await c.put(path, headers=signed(d, "PUT", path, "normal"), json={"title": "Unsafe page", "content": "A body"})
    assert response.status_code == 400, response.text
    assert response.json() == {"detail": "invalid wiki slug"}
    assert outside_inventory(d.root, kb_routes._kb.kb_dir) == before


@pytest.mark.parametrize("slug", ["../escaped", "a/./page", "alias/page"])
def test_wiki_builder_skips_refused_pages_and_saves_the_rest(tmp_path, capsys, slug):
    kb = KBStore(str(tmp_path / "kb"))
    outside = tmp_path / "outside"
    outside.mkdir()
    (kb.kb_dir / "wiki" / "alias").symlink_to(outside, target_is_directory=True)
    before = outside_inventory(tmp_path, kb.kb_dir)
    saved = save_wiki_pages(kb, [
        {"slug": slug, "title": "Unsafe page", "content": "Unsafe body"},
        {"slug": "topics/reference", "title": "A reference", "content": "Valid body"},
    ])
    assert saved == ["topics/reference"]
    assert outside_inventory(tmp_path, kb.kb_dir) == before
    assert kb.get_wiki_content("topics/reference") == "Valid body"
    refused = [line for line in capsys.readouterr().err.splitlines() if "Skipping page" in line]
    assert len(refused) == 1
    assert "invalid wiki slug" in refused[0]


def stored_outside_page(tmp_path, stored_path):
    kb = KBStore(str(tmp_path / "kb"))
    outside = tmp_path / "outside.md"
    outside.write_text("Outside content must remain untouched")
    if stored_path == "absolute":
        path = str(outside)
    elif stored_path == "sibling":
        path = "wiki-adjacent/outside.md"
        (kb.kb_dir / path).parent.mkdir()
        (kb.kb_dir / path).write_text(outside.read_text())
    elif stored_path == "symlink":
        path = "wiki/alias.md"
        (kb.kb_dir / path).symlink_to(outside)
    else:
        path = "../../outside.md"
    kb.save_wiki("reference", "A reference", "Valid content")
    with kb._conn() as conn:
        conn.execute("UPDATE wiki_pages SET file_path=? WHERE slug='reference'", (path,))
    return kb, outside, Path(kb.kb_dir / path)


@pytest.mark.parametrize("stored_path", ["relative", "absolute", "sibling", "symlink"])
def test_wiki_reads_do_not_open_a_stored_path_outside_wiki(tmp_path, stored_path):
    kb, outside, target = stored_outside_page(tmp_path, stored_path)
    assert kb.get_wiki("reference") is not None
    assert kb.get_wiki_content("reference") is None
    assert target.read_text() == outside.read_text()


@pytest.mark.parametrize("stored_path", ["relative", "absolute", "sibling", "symlink"])
def test_wiki_delete_removes_the_row_but_keeps_an_outside_file(tmp_path, capsys, stored_path):
    kb, outside, target = stored_outside_page(tmp_path, stored_path)
    assert kb.delete_wiki("reference")
    assert target.exists()
    assert outside.read_text() == "Outside content must remain untouched"
    assert kb.get_wiki("reference") is None
    lines = [line for line in capsys.readouterr().err.splitlines() if "outside wiki" in line]
    assert len(lines) == 1
