"""Tests for the KG reasoning layer — multi-hop connections, what-changed
diff, and functional-predicate contradiction detection (steal-roadmap headline).

Invariants under test (agreed with Murzik on design review):
  * depth=1 kg_connections stays shape-compatible (no new keys).
  * depth>1 is bounded, active-only, cycle-guarded, deterministic.
  * kg_what_changed buckets added (extracted_at) vs ended/superseded (valid_to)
    in their own native types — no mixed string/float comparison.
  * kg_find_contradictions flags FUNCTIONAL predicates only; multi-valued stay
    out; the suggested winner is advisory and nothing is written.
"""

from __future__ import annotations

import pytest

from pinky_memory.store import ReflectionStore


@pytest.fixture
def store(tmp_path):
    db_path = str(tmp_path / "test_memory.db")
    return ReflectionStore(db_path=db_path)


class TestKGConnectionsMultiHop:
    def test_depth1_shape_unchanged(self, store):
        """depth=1 returns exactly outgoing/incoming — no extra keys (back-compat)."""
        store.kg_add("Brad", "works_on", "TOD")
        result = store.kg_connections("Brad")  # default depth=1
        assert set(result.keys()) == {"outgoing", "incoming"}
        assert result["outgoing"][0]["target"] == "TOD"
        assert result["outgoing"][0]["predicate"] == "works_on"

    def test_depth2_reaches_two_hops(self, store):
        store.kg_add("Brad", "works_on", "TOD")
        store.kg_add("TOD", "uses", "Postgres")
        result = store.kg_connections("Brad", depth=2)
        assert result["depth"] == 2
        reached = {r["entity"] for r in result["reachable"]}
        assert "Postgres" in reached
        # Postgres is distance 2 via TOD
        pg = next(r for r in result["reachable"] if r["entity"] == "Postgres")
        assert pg["distance"] == 2
        assert pg["via"] == "TOD"

    def test_distance1_not_duplicated_in_reachable(self, store):
        """Direct neighbours stay in outgoing/incoming, not re-listed in reachable."""
        store.kg_add("Brad", "works_on", "TOD")
        store.kg_add("TOD", "uses", "Postgres")
        result = store.kg_connections("Brad", depth=2)
        reached = {r["entity"] for r in result["reachable"]}
        assert "TOD" not in reached  # TOD is a distance-1 direct edge

    def test_cycle_guarded(self, store):
        """A↔B mutual edges must not loop or re-add the root."""
        store.kg_add("Brad", "knows", "Yulia")
        store.kg_add("Yulia", "knows", "Brad")
        result = store.kg_connections("Brad", depth=3)
        reached = {r["entity"] for r in result["reachable"]}
        assert "Brad" not in reached  # root never re-recorded

    def test_depth_clamped(self, store):
        store.kg_add("A", "x", "B")
        result = store.kg_connections("A", depth=99)
        assert result["depth"] == store._KG_MAX_DEPTH  # capped, not 99

    def test_active_only_traversal(self, store):
        """Expired (valid_to set) edges are not traversed."""
        store.kg_add("Brad", "works_on", "TOD")
        store.kg_add("TOD", "uses", "OldDB")
        store.kg_invalidate("TOD", "uses", "OldDB")  # ends that edge
        result = store.kg_connections("Brad", depth=2)
        reached = {r["entity"] for r in result["reachable"]}
        assert "OldDB" not in reached


class TestKGWhatChanged:
    def test_added_bucket(self, store):
        store.kg_add("Brad", "uses", "Python")
        result = store.kg_what_changed(since="2000-01-01")
        objs = {a["object"] for a in result["added"]}
        assert "Python" in objs
        assert result["counts"]["added"] >= 1

    def test_future_since_excludes(self, store):
        store.kg_add("Brad", "uses", "Python")
        result = store.kg_what_changed(since="2099-01-01")
        assert result["counts"]["added"] == 0

    def test_ended_and_superseded_buckets(self, store):
        store.kg_add("Brad", "lives_in", "Denver")
        store.kg_invalidate("Brad", "lives_in", "Denver")  # sets valid_to + status=superseded
        result = store.kg_what_changed(since="2000-01-01")
        ended_objs = {e["object"] for e in result["ended"]}
        assert "Denver" in ended_objs
        assert result["counts"]["superseded"] >= 1

    def test_partial_date_parses(self, store):
        """A partial 'since' (year, or year-month) must not raise."""
        store.kg_add("Brad", "uses", "Python")
        for since in ("2000", "2000-06", "2000-06-01"):
            result = store.kg_what_changed(since=since)
            assert result["counts"]["added"] >= 1

    def test_bare_year_is_year_not_epoch(self, store):
        """since='2099' means the year 2099, not 2099 epoch seconds (1970)."""
        store.kg_add("Brad", "uses", "Python")
        result = store.kg_what_changed(since="2099")
        assert result["counts"]["added"] == 0

    def test_epoch_since_still_parses(self, store):
        store.kg_add("Brad", "uses", "Python")
        # Float epoch and a >4-digit integer epoch both stay epoch-parsed
        for since in ("946684800.0", "946684800"):  # 2000-01-01 UTC
            result = store.kg_what_changed(since=since)
            assert result["counts"]["added"] >= 1

    def test_malformed_since_safe(self, store):
        store.kg_add("Brad", "uses", "Python")
        # garbage falls back to "everything changed", never raises
        result = store.kg_what_changed(since="not-a-date")
        assert "added" in result


class TestKGFindContradictions:
    def test_functional_conflict_detected(self, store):
        store.kg_add("Brad", "lives_in", "Denver", valid_from="2026-01")
        store.kg_add("Brad", "lives_in", "Austin", valid_from="2026-05")
        result = store.kg_find_contradictions()
        assert len(result) == 1
        c = result[0]
        assert c["predicate"] == "lives_in"
        assert set(c["active_values"]) == {"Denver", "Austin"}

    def test_suggested_winner_is_newest_valid_from(self, store):
        store.kg_add("Brad", "lives_in", "Denver", valid_from="2026-01")
        store.kg_add("Brad", "lives_in", "Austin", valid_from="2026-05")
        c = store.kg_find_contradictions()[0]
        assert c["suggested_winner"]["object"] == "Austin"  # newer valid_from wins

    def test_multivalued_not_flagged(self, store):
        """'uses' is multi-valued — two active values is NOT a contradiction."""
        store.kg_add("Brad", "uses", "Python")
        store.kg_add("Brad", "uses", "Rust")
        assert store.kg_find_contradictions() == []

    def test_single_value_not_flagged(self, store):
        store.kg_add("Brad", "lives_in", "Denver")
        assert store.kg_find_contradictions() == []

    def test_read_only_no_mutation(self, store):
        """Detection must not write — the conflicting triples stay active."""
        store.kg_add("Brad", "timezone", "America/Los_Angeles", valid_from="2026-01")
        store.kg_add("Brad", "timezone", "America/New_York", valid_from="2026-05")
        store.kg_find_contradictions()
        active = store.kg_query(entity="Brad", predicate="timezone")
        assert len(active) == 2  # both still active — nothing was invalidated
