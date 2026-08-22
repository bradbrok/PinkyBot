"""k-NN for consolidation on the sqlite-vec path (#486 difetto 3).

_knn_vec fetched a FIXED window (fetch_k = k * 3) and only afterwards dropped
inactive rows, the reference itself and the caller's exclude set. Rows removed
by those filters can fill the whole window, so consolidation sees fewer than
`k` neighbours — or none — while eligible ones exist just past the window.
"""
from __future__ import annotations

from pinky_memory.store import ReflectionStore
from pinky_memory.types import Reflection


def _store(tmp_path) -> ReflectionStore:
    return ReflectionStore(db_path=str(tmp_path / "reflections.db"))


def test_neighbours_found_behind_a_window_of_inactive_rows(tmp_path):
    """Inactive rows nearer than the neighbours must not starve the result."""
    store = _store(tmp_path)

    # 20 inactive rows, all nearer to the probe than the active ones.
    # With k=3 the old window was fetch_k=9 — entirely filled by these.
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

    probe = Reflection(content="probe", embedding=[1.0, 0.0, 0.0, 0.0], active=True)

    # Guard: the defect only exists on the vec path.
    assert store._vec_available is True
    assert store._vec_dimensions == len(probe.embedding)

    results = store._knn_for_consolidation(probe, k=3)

    assert len(results) == 3, f"knn returned {len(results)} of 3 available neighbours"
    assert {ref.id for _, ref in results} == set(active_ids)

    store.close()


def test_excluded_ids_do_not_starve_the_window(tmp_path):
    """The caller's exclude set is applied post-kNN and must not eat the window."""
    store = _store(tmp_path)

    excluded = []
    for i in range(20):
        ref = store.insert(
            Reflection(
                content=f"excluded {i}",
                embedding=[1.0, 0.0001 * (i + 1), 0.0, 0.0],
                active=True,
            )
        )
        excluded.append(ref.id)

    wanted = []
    for i in range(2):
        ref = store.insert(
            Reflection(
                content=f"wanted {i}",
                embedding=[1.0, 0.1 * (i + 1), 0.0, 0.0],
                active=True,
            )
        )
        wanted.append(ref.id)

    probe = Reflection(content="probe", embedding=[1.0, 0.0, 0.0, 0.0], active=True)
    assert store._vec_available is True

    results = store._knn_for_consolidation(probe, k=2, exclude_ids=set(excluded))

    assert len(results) == 2, f"knn returned {len(results)} of 2 eligible neighbours"
    assert {ref.id for _, ref in results} == set(wanted)

    store.close()


def test_window_stops_when_index_is_exhausted(tmp_path):
    """Fewer eligible rows than k must terminate, not loop forever."""
    store = _store(tmp_path)
    for i in range(4):
        store.insert(
            Reflection(
                content=f"inactive {i}",
                embedding=[1.0, 0.0001 * (i + 1), 0.0, 0.0],
                active=False,
            )
        )
    only = store.insert(
        Reflection(content="only active", embedding=[1.0, 0.2, 0.0, 0.0], active=True)
    )

    probe = Reflection(content="probe", embedding=[1.0, 0.0, 0.0, 0.0], active=True)
    results = store._knn_for_consolidation(probe, k=5)

    assert [ref.id for _, ref in results] == [only.id]

    store.close()
