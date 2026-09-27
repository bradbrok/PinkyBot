"""Vector recall on the sqlite-vec path (#486 difetto 1).

_search_by_vec fetches a FIXED window (fetch_k = limit * 5) and only afterwards
applies the active / no_recall / project / entity filters. Rows excluded by
those filters can fill the whole window, so recall() silently returns fewer
than `limit` results — or none at all — while matching rows exist.
"""
from __future__ import annotations

from pinky_memory.store import ReflectionStore
from pinky_memory.types import Reflection


def _store(tmp_path) -> ReflectionStore:
    return ReflectionStore(db_path=str(tmp_path / "reflections.db"))


def test_active_rows_found_behind_a_window_of_inactive_neighbours(tmp_path):
    """Inactive rows nearer than the matches must not starve the result set."""
    store = _store(tmp_path)
    query = [1.0, 0.0, 0.0, 0.0]

    # 20 inactive rows, all nearer to the query than the active ones.
    # With limit=3 the old window was k=15 — entirely filled by these.
    for i in range(20):
        store.insert(
            Reflection(
                content=f"inactive {i}",
                embedding=[1.0, 0.0001 * (i + 1), 0.0, 0.0],
                active=False,
            )
        )

    active_ids = []
    for i in range(3):
        ref = store.insert(
            Reflection(
                content=f"active {i}",
                embedding=[1.0, 0.1 * (i + 1), 0.0, 0.0],
                active=True,
            )
        )
        active_ids.append(ref.id)

    # Guard: the defect only exists on the vec path.
    assert store._vec_available is True
    assert store._vec_dimensions == len(query)

    results = store.search_by_embedding_scored(query, limit=3, active_only=True)

    assert len(results) == 3, f"recall returned {len(results)} of 3 available matches"
    assert {ref.id for _, ref in results} == set(active_ids)

    store.close()


def test_no_recall_rows_do_not_starve_the_window(tmp_path):
    """no_recall rows are excluded post-kNN and must not eat the window either."""
    store = _store(tmp_path)
    query = [1.0, 0.0, 0.0, 0.0]

    for i in range(20):
        store.insert(
            Reflection(
                content=f"hidden {i}",
                embedding=[1.0, 0.0001 * (i + 1), 0.0, 0.0],
                active=True,
                no_recall=True,
            )
        )

    wanted = store.insert(
        Reflection(content="wanted", embedding=[1.0, 0.2, 0.0, 0.0], active=True)
    )

    assert store._vec_available is True

    results = store.search_by_embedding_scored(query, limit=2, active_only=True)

    assert [ref.id for _, ref in results] == [wanted.id]

    store.close()


def test_limit_is_still_respected_when_everything_matches(tmp_path):
    """Widening the window must not return more than `limit`."""
    store = _store(tmp_path)
    query = [1.0, 0.0, 0.0, 0.0]

    for i in range(10):
        store.insert(
            Reflection(
                content=f"match {i}",
                embedding=[1.0, 0.01 * (i + 1), 0.0, 0.0],
                active=True,
            )
        )

    assert store._vec_available is True

    results = store.search_by_embedding_scored(query, limit=4, active_only=True)
    assert len(results) == 4

    store.close()
