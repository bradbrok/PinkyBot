"""Near-duplicate detection on the sqlite-vec path (#486 difetto 2).

The vec path fetches a FIXED window of neighbours (k) and only afterwards
filters by `active = 1`. Superseded versions of a reflection are its nearest
neighbours in absolute terms, so they can fill the whole window and make
find_near_duplicate() return None — the duplicate then gets written again.
"""
from __future__ import annotations

from pinky_memory.store import ReflectionStore
from pinky_memory.types import Reflection


def _store(tmp_path) -> ReflectionStore:
    return ReflectionStore(db_path=str(tmp_path / "reflections.db"))


def _insert(store: ReflectionStore, content: str, embedding: list[float], active: bool):
    return store.insert(
        Reflection(content=content, embedding=embedding, active=active)
    )


def test_active_duplicate_found_behind_a_window_of_superseded_neighbours(tmp_path):
    """An active duplicate must be found even when many inactive rows are closer."""
    store = _store(tmp_path)
    query = [1.0, 0.0, 0.0, 0.0]

    # Six superseded (inactive) versions, all nearer to the query than the
    # active duplicate — enough to fill any small fixed kNN window.
    for i in range(6):
        _insert(store, f"superseded v{i}", [1.0, 0.0001 * (i + 1), 0.0, 0.0], False)

    # The active near-duplicate: cosine similarity ≈ 0.98, above threshold.
    active = _insert(store, "active duplicate", [1.0, 0.2, 0.0, 0.0], True)

    # Guard: the bug only exists on the vec path — make sure we exercise it.
    assert store._vec_available is True
    assert store._vec_dimensions == len(query)

    result = store.find_near_duplicate(query, threshold=0.90, active_only=True)

    assert result is not None, "active duplicate hidden behind superseded neighbours"
    similarity, found = result
    assert found.id == active.id
    assert similarity > 0.90

    store.close()


def test_returns_none_when_no_active_row_is_above_threshold(tmp_path):
    """No false positives: a distant active row must not be reported."""
    store = _store(tmp_path)
    query = [1.0, 0.0, 0.0, 0.0]

    _insert(store, "orthogonal", [0.0, 1.0, 0.0, 0.0], True)

    assert store._vec_available is True

    assert store.find_near_duplicate(query, threshold=0.90, active_only=True) is None

    store.close()
