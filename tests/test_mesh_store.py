"""Tests for MeshStore — ferry envelope log + federation peer registry.

Covers:
- schema migration / idempotent init
- log_message: inbound/outbound rows, side-effect peer touch, error semantics
- get_inbox: filters (kind, sender, direction), pagination, ordering
- get_message_by_correlation_id: most-recent on duplicate, missing returns None
- federation_peers: upsert (config wins), list filters, get/remove
- defensive: malformed body fields don't crash deserialization

PinkyBot issue: #419.
"""

from __future__ import annotations

import time

import pytest

from pinky_daemon.mesh_store import MeshStore


@pytest.fixture
def store(tmp_path):
    s = MeshStore(db_path=str(tmp_path / "mesh.db"))
    yield s
    s.close()


# -- Schema --------------------------------------------------------------------


class TestSchema:
    def test_init_idempotent(self, tmp_path):
        path = str(tmp_path / "mesh.db")
        s1 = MeshStore(db_path=path)
        s1.close()
        # Second open should not crash on existing tables.
        s2 = MeshStore(db_path=path)
        s2.close()


# -- log_message ---------------------------------------------------------------


class TestLogMessage:
    def test_outbound_success_logs_and_touches_peer(self, store):
        rid = store.log_message(
            direction="outbound",
            local_agent="barsik",
            remote_fleet="pulse",
            remote_agent="pulse",
            correlation_id="cid-1",
            kind="msg",
            body={"text": "hi", "kind": "msg"},
            envelope_ts=1234567890,
        )
        assert rid > 0
        peer = store.get_peer("pulse", "pulse")
        assert peer is not None
        assert peer.seed_source == "observed"

    def test_outbound_failure_does_not_touch_peer(self, store):
        store.log_message(
            direction="outbound",
            local_agent="barsik",
            remote_fleet="ghost",
            remote_agent="ghost",
            correlation_id="cid-fail",
            kind="msg",
            body={"text": "x", "kind": "msg"},
            error="nats: error: connection refused",
        )
        # Failed sends shouldn't auto-register the unreachable peer.
        assert store.get_peer("ghost", "ghost") is None

    def test_inbound_always_touches_peer(self, store):
        store.log_message(
            direction="inbound",
            local_agent="barsik",
            remote_fleet="pulse",
            remote_agent="misha",
            correlation_id="cid-in-1",
            kind="message",
            body={"text": "hello", "kind": "message"},
            error="rejected:acl_denied",  # even rejected inbound counts as "we saw them"
        )
        peer = store.get_peer("pulse", "misha")
        assert peer is not None
        assert peer.seed_source == "observed"

    def test_count_messages(self, store):
        for i in range(5):
            store.log_message(
                direction="outbound",
                local_agent="barsik",
                remote_fleet="pulse",
                remote_agent="pulse",
                correlation_id=f"cid-{i}",
                kind="msg",
                body={"kind": "msg"},
            )
        for _ in range(3):
            store.log_message(
                direction="inbound",
                local_agent="pushok",
                remote_fleet="pulse",
                remote_agent="misha",
                kind="message",
                body={"kind": "message"},
            )
        assert store.count_messages() == 8
        assert store.count_messages("barsik") == 5
        assert store.count_messages("pushok") == 3
        assert store.count_messages("nobody") == 0


# -- get_inbox -----------------------------------------------------------------


class TestGetInbox:
    def _seed(self, store):
        # Three messages spaced apart in time so ordering is stable.
        for i, kind in enumerate(["msg", "smoke", "msg"]):
            store.log_message(
                direction="outbound" if i % 2 else "inbound",
                local_agent="barsik",
                remote_fleet="pulse",
                remote_agent="pulse" if i < 2 else "misha",
                correlation_id=f"cid-{i}",
                kind=kind,
                body={"kind": kind, "n": i},
            )
            time.sleep(0.005)

    def test_newest_first(self, store):
        self._seed(store)
        msgs = store.get_inbox("barsik")
        assert len(msgs) == 3
        # received_at descends
        assert msgs[0].received_at >= msgs[1].received_at >= msgs[2].received_at

    def test_kind_filter(self, store):
        self._seed(store)
        smokes = store.get_inbox("barsik", kind="smoke")
        assert len(smokes) == 1
        assert smokes[0].kind == "smoke"

    def test_direction_filter(self, store):
        self._seed(store)
        out = store.get_inbox("barsik", direction="outbound")
        assert all(m.direction == "outbound" for m in out)

    def test_sender_filter(self, store):
        self._seed(store)
        from_misha = store.get_inbox("barsik", sender="misha@pulse")
        assert len(from_misha) == 1
        assert from_misha[0].remote_agent == "misha"

    def test_pagination(self, store):
        self._seed(store)
        page1 = store.get_inbox("barsik", limit=2, offset=0)
        page2 = store.get_inbox("barsik", limit=2, offset=2)
        assert len(page1) == 2
        assert len(page2) == 1
        # No overlap.
        assert page1[1].id != page2[0].id

    def test_other_agent_isolated(self, store):
        self._seed(store)
        store.log_message(
            direction="inbound",
            local_agent="pushok",
            remote_fleet="pulse",
            remote_agent="pulse",
            kind="msg",
            body={"kind": "msg"},
        )
        assert len(store.get_inbox("barsik")) == 3
        assert len(store.get_inbox("pushok")) == 1


# -- get_message_by_correlation_id --------------------------------------------


class TestGetByCorrelationId:
    def test_missing_returns_none(self, store):
        assert store.get_message_by_correlation_id("nope") is None
        assert store.get_message_by_correlation_id("") is None

    def test_returns_most_recent_on_duplicate(self, store):
        store.log_message(
            direction="outbound", local_agent="barsik",
            remote_fleet="p", remote_agent="p",
            correlation_id="cid-dup", kind="msg", body={"kind": "msg", "v": 1},
        )
        time.sleep(0.005)
        store.log_message(
            direction="inbound", local_agent="barsik",
            remote_fleet="p", remote_agent="p",
            correlation_id="cid-dup", kind="ack", body={"kind": "ack", "v": 2},
        )
        latest = store.get_message_by_correlation_id("cid-dup")
        assert latest is not None
        assert latest.kind == "ack"
        assert latest.body == {"kind": "ack", "v": 2}


# -- federation_peers ----------------------------------------------------------


class TestFederationPeers:
    def test_upsert_inserts_then_updates(self, store):
        store.upsert_peer(fleet="pulse", agent="pulse", display_name="Pulse")
        p1 = store.get_peer("pulse", "pulse")
        assert p1 is not None
        assert p1.display_name == "Pulse"
        assert p1.seed_source == "config"

        time.sleep(0.005)
        store.upsert_peer(fleet="pulse", agent="pulse", display_name="Pulse v2")
        p2 = store.get_peer("pulse", "pulse")
        assert p2 is not None
        assert p2.display_name == "Pulse v2"
        # first_seen preserved across update
        assert p2.first_seen == p1.first_seen
        assert p2.last_seen >= p1.last_seen

    def test_config_beats_observed_on_promotion(self, store):
        # Observed first.
        store.log_message(
            direction="inbound", local_agent="barsik",
            remote_fleet="pulse", remote_agent="pulse",
            kind="msg", body={"kind": "msg"},
        )
        assert store.get_peer("pulse", "pulse").seed_source == "observed"
        # Then config-promoted.
        store.upsert_peer(fleet="pulse", agent="pulse", display_name="Pulse")
        assert store.get_peer("pulse", "pulse").seed_source == "config"

    def test_observed_does_not_downgrade_config(self, store):
        store.upsert_peer(fleet="pulse", agent="pulse", display_name="Pulse")
        # Subsequent observed traffic shouldn't downgrade seed_source.
        store.log_message(
            direction="inbound", local_agent="barsik",
            remote_fleet="pulse", remote_agent="pulse",
            kind="msg", body={"kind": "msg"},
        )
        assert store.get_peer("pulse", "pulse").seed_source == "config"

    def test_list_peers_filters(self, store):
        store.upsert_peer(fleet="pulse", agent="pulse", seed_source="config")
        store.upsert_peer(fleet="pulse", agent="misha", seed_source="config")
        store.upsert_peer(fleet="sigil", agent="agent3", seed_source="observed")

        all_peers = store.list_peers()
        assert len(all_peers) == 3

        pulse_only = store.list_peers(fleet="pulse")
        assert len(pulse_only) == 2
        assert {p.agent for p in pulse_only} == {"pulse", "misha"}

        config_only = store.list_peers(seed_source="config")
        assert len(config_only) == 2

        observed_only = store.list_peers(seed_source="observed")
        assert len(observed_only) == 1
        assert observed_only[0].fleet == "sigil"

    def test_remove_peer(self, store):
        store.upsert_peer(fleet="pulse", agent="pulse")
        assert store.remove_peer("pulse", "pulse") is True
        assert store.remove_peer("pulse", "pulse") is False  # already gone
        assert store.get_peer("pulse", "pulse") is None

    def test_address_field_in_to_dict(self, store):
        store.upsert_peer(fleet="pulse", agent="misha")
        peer = store.get_peer("pulse", "misha")
        assert peer.to_dict()["address"] == "misha@pulse"


# -- Defensive deserialization -------------------------------------------------


class TestDefensive:
    def test_malformed_body_json_does_not_crash(self, store):
        # Bypass the public API to write a row with broken JSON.
        store._db.execute(
            """INSERT INTO mesh_messages
               (correlation_id, direction, local_agent, remote_fleet,
                remote_agent, kind, body, envelope_ts, received_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("cid-x", "inbound", "barsik", "p", "p", "msg",
             "{not valid json", 0, time.time()),
        )
        store._db.commit()
        msgs = store.get_inbox("barsik")
        assert len(msgs) == 1
        assert "_raw" in msgs[0].body  # fell back gracefully
