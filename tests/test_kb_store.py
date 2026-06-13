"""Tests for KB store — raw source CRUD operations."""

import sqlite3

import pytest

from pinky_daemon.kb_store import KBStore, _parse_frontmatter


@pytest.fixture
def kb(tmp_path):
    """Create a KBStore in a temp directory."""
    return KBStore(str(tmp_path / "kb"))


class TestParsesFrontmatter:
    def test_basic(self):
        text = "---\ntitle: Hello\ntags: [a, b]\n---\n\n# Hello\n\nBody text here."
        fm, body = _parse_frontmatter(text)
        assert fm["title"] == "Hello"
        assert fm["tags"] == ["a", "b"]
        assert body == "Body text here."

    def test_no_frontmatter(self):
        text = "Just plain text."
        fm, body = _parse_frontmatter(text)
        assert fm == {}
        assert body == "Just plain text."

    def test_empty_body(self):
        text = "---\ntitle: Empty\n---\n\n# Empty\n\n"
        fm, body = _parse_frontmatter(text)
        assert fm["title"] == "Empty"
        assert body == ""


class TestIngest:
    def test_ingest_creates_file_and_db(self, kb):
        source = kb.ingest(title="Test Source", content="Hello world", tags=["test"])
        assert source.id.startswith("raw-")
        assert source.title == "Test Source"
        assert source.tags == ["test"]

        # File exists
        full_path = kb.kb_dir / source.file_path
        assert full_path.exists()
        content = full_path.read_text()
        assert "Hello world" in content
        assert "Test Source" in content

    def test_ingest_indexes_in_fts(self, kb):
        kb.ingest(title="Searchable", content="unique keyword xyzzy", tags=["findme"])
        results = kb.search("xyzzy")
        assert len(results) == 1
        assert results[0]["title"] == "Searchable"


class TestDeleteRaw:
    def test_delete_removes_everything(self, kb):
        source = kb.ingest(title="To Delete", content="Bye bye", tags=["temp"])
        assert kb.get_raw(source.id) is not None

        deleted = kb.delete_raw(source.id)
        assert deleted is True

        # DB row gone
        assert kb.get_raw(source.id) is None

        # File gone
        full_path = kb.kb_dir / source.file_path
        assert not full_path.exists()

        # FTS gone
        results = kb.search("Bye bye")
        assert len(results) == 0

    def test_delete_nonexistent_returns_false(self, kb):
        assert kb.delete_raw("raw-9999-01-01-999") is False

    def test_delete_then_count(self, kb):
        kb.ingest(title="Keep", content="a")
        s2 = kb.ingest(title="Delete", content="b")
        assert kb.count_raw() == 2

        kb.delete_raw(s2.id)
        assert kb.count_raw() == 1


class TestUpdateRaw:
    def test_update_title(self, kb):
        source = kb.ingest(title="Old Title", content="Body text", tags=["a"])
        updated = kb.update_raw(source.id, title="New Title")
        assert updated is not None
        assert updated.title == "New Title"

        # File reflects change
        content = kb.get_raw_content(source.id)
        assert "# New Title" in content

    def test_update_tags(self, kb):
        source = kb.ingest(title="Tagged", content="Body", tags=["old"])
        updated = kb.update_raw(source.id, tags=["new", "fresh"])
        assert updated.tags == ["new", "fresh"]

        # FTS updated — search for new tag
        results = kb.search("fresh")
        assert len(results) == 1

    def test_update_content(self, kb):
        source = kb.ingest(title="Content", content="Original body")
        updated = kb.update_raw(source.id, content="Updated body text")
        assert updated is not None

        content = kb.get_raw_content(source.id)
        assert "Updated body text" in content
        assert "Original body" not in content

        # FTS reflects new content
        assert len(kb.search("Updated body")) == 1
        assert len(kb.search("Original body")) == 0

    def test_update_nonexistent_returns_none(self, kb):
        assert kb.update_raw("raw-9999-01-01-999", title="Nope") is None

    def test_update_preserves_unmodified_fields(self, kb):
        source = kb.ingest(
            title="Stable", content="Body",
            tags=["keep"], source_type="article", source_url="https://example.com"
        )
        updated = kb.update_raw(source.id, title="Changed Title")
        assert updated.source_type == "article"
        assert updated.source_url == "https://example.com"
        assert updated.tags == ["keep"]


class TestCountRaw:
    def test_count_all(self, kb):
        assert kb.count_raw() == 0
        kb.ingest(title="A", content="a")
        kb.ingest(title="B", content="b")
        assert kb.count_raw() == 2

    def test_count_with_type_filter(self, kb):
        kb.ingest(title="Article", content="a", source_type="article")
        kb.ingest(title="Note", content="b", source_type="note")
        assert kb.count_raw(source_type="article") == 1
        assert kb.count_raw(source_type="note") == 1

    def test_count_with_tag_filter(self, kb):
        kb.ingest(title="A", content="a", tags=["python"])
        kb.ingest(title="B", content="b", tags=["rust"])
        assert kb.count_raw(tag="python") == 1


class TestCheckDuplicate:
    def test_url_duplicate(self, kb):
        kb.ingest(title="A", content="x", source_url="https://example.com/a")
        dup = kb.check_duplicate(source_url="https://example.com/a")
        assert dup is not None

    def test_content_duplicate_matches_ingested_content(self, kb):
        content = "substantial body " * 10
        source = kb.ingest(title="Some Note", content=content)
        dup = kb.check_duplicate(content=content)
        assert dup is not None
        assert dup.id == source.id

    def test_short_content_not_deduped(self, kb):
        kb.ingest(title="Tiny", content="short")
        assert kb.check_duplicate(content="short") is None


class TestSearchRobustness:
    def test_apostrophe_query_does_not_crash(self, kb):
        kb.ingest(title="Mondays", content="I don't like mondays at all")
        results = kb.search("don't")
        assert len(results) == 1

    def test_unbalanced_quote_returns_no_error(self, kb):
        kb.ingest(title="Q", content="some plain text body")
        assert kb.search('something "unbalanced') == []

    def test_whitespace_query_returns_empty(self, kb):
        kb.ingest(title="Q", content="some plain text body")
        assert kb.search("   ") == []


class TestIngestFailureCleanup:
    def test_duplicate_url_insert_cleans_up_file(self, kb):
        kb.ingest(title="First", content="body one", source_url="https://example.com/dup")
        files_before = sorted(p.name for p in kb.raw_dir.glob("*.md"))
        with pytest.raises(sqlite3.IntegrityError):
            kb.ingest(title="Second", content="body two", source_url="https://example.com/dup")
        files_after = sorted(p.name for p in kb.raw_dir.glob("*.md"))
        assert files_after == files_before


class TestReindex:
    def test_reindex_preserves_space_joined_tags(self, kb):
        source = kb.ingest(title="Tagged", content="body text here", tags=["alpha", "beta"])
        kb.reindex()
        conn = sqlite3.connect(str(kb.db_path))
        try:
            row = conn.execute(
                "SELECT tags FROM fts_content WHERE ref_id = ? AND kind = 'raw'",
                (source.id,),
            ).fetchone()
        finally:
            conn.close()
        assert row[0] == "alpha beta"

    def test_reindex_keeps_search_working(self, kb):
        kb.ingest(title="Searchable", content="unique keyword xyzzy")
        kb.reindex()
        assert len(kb.search("xyzzy")) == 1


class TestUpsertSnapshot:
    """Idempotent singleton snapshots keyed by source_url (issue #703).

    The nightly people-profiles / project-state snapshots re-file under a
    constant source_url. A second call must REPLACE in place, never a second
    INSERT (which trips the UNIQUE raw_sources.source_url index).
    """

    URL = "internal://people-profiles-snapshot"

    def test_first_call_inserts(self, kb):
        s = kb.upsert_snapshot(source_url=self.URL, title="Snap", content="v1 body")
        assert s.source_url == self.URL
        assert (kb.kb_dir / s.file_path).exists()
        assert kb.count_raw() == 1

    def test_repeat_call_replaces_in_place(self, kb):
        s1 = kb.upsert_snapshot(source_url=self.URL, title="Snap", content="version one")
        # Pre-#703 this second call raised: UNIQUE constraint failed.
        s2 = kb.upsert_snapshot(source_url=self.URL, title="Snap", content="version two now")
        # Replaced, not duplicated.
        assert s2.id == s1.id
        assert kb.count_raw() == 1
        # File holds the new content, not the old.
        body = (kb.kb_dir / s2.file_path).read_text()
        assert "version two now" in body
        assert "version one" not in body
        # FTS reflects the new content (old tokens gone).
        assert len(kb.search("two")) == 1
        assert len(kb.search("one")) == 0

    def test_replaces_after_backing_file_deleted(self, kb):
        s1 = kb.upsert_snapshot(source_url=self.URL, title="Snap", content="v1")
        # Orphan the row: drop the file but leave the DB row + its UNIQUE url.
        (kb.kb_dir / s1.file_path).unlink()
        # Must re-create cleanly, not collide on the UNIQUE source_url index.
        s2 = kb.upsert_snapshot(source_url=self.URL, title="Snap", content="v2 reborn")
        assert (kb.kb_dir / s2.file_path).exists()
        assert "v2 reborn" in (kb.kb_dir / s2.file_path).read_text()
        assert kb.count_raw() == 1

    def test_distinct_urls_coexist(self, kb):
        a = kb.upsert_snapshot(source_url="internal://people", title="People", content="aaa")
        b = kb.upsert_snapshot(source_url="internal://projects", title="Projects", content="bbb")
        assert a.id != b.id
        assert kb.count_raw() == 2

    def test_replacement_bumps_filed_at_for_rediscovery(self, kb):
        # The librarian discovers work via list_raw(since=last_run), which
        # filters filed_at > last_run. If a replacement keeps the original
        # filed_at, curation silently misses every snapshot after the first
        # once the watermark has advanced (issue #703 P1, caught in review).
        s1 = kb.upsert_snapshot(source_url=self.URL, title="Snap", content="first")
        first_filed_at = s1.filed_at
        # Simulate the librarian having curated up to s1: nothing is newer.
        assert kb.list_raw(since=first_filed_at) == []

        s2 = kb.upsert_snapshot(source_url=self.URL, title="Snap", content="second")
        assert s2.id == s1.id
        # filed_at advanced, so the since-watermark query re-sees the snapshot.
        assert s2.filed_at > first_filed_at
        rediscovered = kb.list_raw(since=first_filed_at)
        assert any(r.id == s1.id for r in rediscovered)
