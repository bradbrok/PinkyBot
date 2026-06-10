"""Tests for presentation store - versioning and listing."""

from __future__ import annotations

import pytest

from pinky_daemon.presentation_store import PresentationStore


@pytest.fixture
def store(tmp_path):
    return PresentationStore(str(tmp_path / "presentations.db"))


class TestVersioning:
    def test_update_increments_version(self, store):
        pres = store.create("Deck", "<p>v1</p>")
        assert pres.current_version == 1
        updated = store.update(pres.id, "<p>v2</p>")
        assert updated.current_version == 2

    def test_restore_version_rewinds(self, store):
        pres = store.create("Deck", "<p>v1</p>")
        store.update(pres.id, "<p>v2</p>")
        restored = store.restore_version(pres.id, 1)
        assert restored.current_version == 1
        assert restored.current_html == "<p>v1</p>"

    def test_update_after_restore_does_not_collide(self, store):
        pres = store.create("Deck", "<p>v1</p>")
        store.update(pres.id, "<p>v2</p>")
        store.update(pres.id, "<p>v3</p>")
        restored = store.restore_version(pres.id, 2)
        assert restored.current_version == 2

        updated = store.update(pres.id, "<p>v4</p>")
        assert updated is not None
        assert updated.current_version == 4
        assert updated.current_html == "<p>v4</p>"

        # Subsequent edits keep working
        again = store.update(pres.id, "<p>v5</p>")
        assert again.current_version == 5


class TestListTagFilter:
    def test_tag_filter_applies_before_limit(self, store):
        # Tagged presentation is the oldest (lowest updated_at), so it falls
        # outside the LIMIT window unless the tag filter runs in SQL.
        tagged = store.create("Tagged", "<p>t</p>", tags=["demo"])
        for i in range(3):
            store.create(f"Other {i}", "<p>o</p>")
        results = store.list(tag="demo", limit=2)
        assert [p.id for p in results] == [tagged.id]

    def test_tag_filter_no_match(self, store):
        store.create("Plain", "<p>p</p>")
        assert store.list(tag="missing") == []

    def test_list_without_tag_unaffected(self, store):
        store.create("A", "<p>a</p>")
        store.create("B", "<p>b</p>")
        assert len(store.list()) == 2
