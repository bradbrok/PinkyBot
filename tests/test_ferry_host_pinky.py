"""Tests for @ferry/host-pinky — substrate ↔ pinky-memory bridge.

Acceptance test uses substrate v0.1 §12 worked example as fixtures:
  Entry 1: feedback, trust 0.95, scope user/brad ("Brad wants terse responses…")
  Entry 2: pattern,  trust 0.7,  scope user/brad ("Brad's tolerance for response length…")
            links → Entry 1

See PinkyBot issue #413 and ferry packages/host-pinky/README.md.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict

import pytest

from pinky_daemon.agent_registry import AgentRegistry
from pinky_daemon.ferry.host_pinky import (
    HostPinky,
    parse_peer_card,
    parse_pinkybot_address,
)
from pinky_daemon.ferry.substrate import (
    classify_entry_destination,
    map_substrate_type_to_reflection,
    populate_port_history,
    to_reflection_input,
    trust_to_salience,
)
from pinky_daemon.ferry.types import (
    AgentCardSelector,
    FerryEnvelope,
    SubstrateEntry,
    SubstrateLifecycle,
    SubstrateScope,
    SubstrateSource,
    TraversalRecord,
)

# -- §12 worked example fixtures (substrate v0.1) ------------------------------


def _entry_1_feedback() -> SubstrateEntry:
    """Entry 1 from substrate v0.1 §12.2 — user-stated feedback."""
    return SubstrateEntry(
        id="0c7e3d12-9f8a-4f1e-b6e2-2d8a8c1f4f9a",
        type="feedback",
        scope=SubstrateScope(kind="user", ref="brad"),
        source=SubstrateSource(
            origin="user-stated",
            by="misha@pinky.local",
            evidence=(
                "Brad in DM, 2026-04-12 16:23 PT: 'stop summarizing what you "
                "just did at the end of every response, I can read the diff'"
            ),
            trust=0.95,
        ),
        created_at="2026-04-12T16:24:11-07:00",
        updated_at="2026-04-12T16:24:11-07:00",
        content=(
            "Brad wants terse responses with no trailing summaries.\n"
            "Why: he reads the diff himself; trailing summaries are noise.\n"
            "How to apply: end responses at the last substantive sentence; "
            "do not append a recap of what was changed."
        ),
        links=[],
        lifecycle=SubstrateLifecycle(state="active"),
        port_history=[],
    )


def _entry_2_pattern() -> SubstrateEntry:
    """Entry 2 from substrate v0.1 §12.3 — agent-inferred pattern."""
    return SubstrateEntry(
        id="4a9b2e57-3c1d-4e8f-9a0b-7e6c5d4f3e2a",
        type="pattern",
        scope=SubstrateScope(kind="user", ref="brad"),
        source=SubstrateSource(
            origin="agent-inferred",
            by="misha@pinky.local",
            evidence=(
                "Refs to 12 conversation entries: [list of 12 ids elided]; "
                "pattern: Brad responds with curtness or 'too long' when "
                "responses exceed ~3 sentences without functional content."
            ),
            trust=0.7,
        ),
        created_at="2026-05-02T09:11:32-07:00",
        updated_at="2026-05-02T09:11:32-07:00",
        content=(
            "Brad's tolerance for response length appears bounded around 3 "
            "sentences of non-functional prose. Beyond that, he often "
            "pushes back ('too long', curtness, or no reply).\n"
            "This pattern holds even when he hasn't explicitly asked for brevity.\n"
            "Implication for response calibration: default to terse; verbose "
            "only when Brad explicitly asks for depth or the content is "
            "irreducibly long."
        ),
        links=["0c7e3d12-9f8a-4f1e-b6e2-2d8a8c1f4f9a"],
        lifecycle=SubstrateLifecycle(state="active"),
        port_history=[],
    )


def _build_envelope_substrate_batch(entries: list[SubstrateEntry]) -> FerryEnvelope:
    """Wrap §12 entries in a ferry envelope addressed to barsik."""
    return FerryEnvelope(
        v="0.1",
        id="01891e0e-7a00-7000-9000-000000000042",  # uuid-v7 placeholder
        from_="misha@pinky.local",
        to="ferry://pinkybot/barsik",
        ts=1746846000000,
        body={
            "kind": "substrate.batch",
            "entries": [_entry_to_dict(e) for e in entries],
        },
        traversal=[
            TraversalRecord(
                broker="ferry-broker:misha-local",
                at=1746846000050,
                via="leaf-link",
                signature="<broker-sig-stub>",
            ),
        ],
    )


def _entry_to_dict(e: SubstrateEntry) -> dict:
    """Serialize a SubstrateEntry to the wire-shape dict (handles from_ → from)."""
    raw = {
        "id": e.id,
        "type": e.type,
        "scope": asdict(e.scope),
        "source": asdict(e.source),
        "created_at": e.created_at,
        "updated_at": e.updated_at,
        "content": e.content,
        "links": list(e.links),
        "lifecycle": asdict(e.lifecycle),
        "port_history": [asdict(p) for p in e.port_history],
    }
    return raw


# -- Fakes ---------------------------------------------------------------------


class FakeRegistry:
    """Minimal AgentRegistry stand-in for host-pinky tests."""

    def __init__(self, agents: dict[str, list[AgentCardSelector]]) -> None:
        self._agents = agents

    def has_agent(self, name: str) -> bool:
        return name in self._agents

    def list_agents(self) -> list[dict]:
        return [{"name": n} for n in self._agents]

    def get_peer_fleet_acl(self, name: str) -> list[AgentCardSelector]:
        return list(self._agents.get(name, []))


class FakeBroker:
    """Records every dispatch_pre_authorized call.

    Note: host-pinky calls ``broker.dispatch_pre_authorized(agent_name,
    broker_msg)`` — NOT ``broker.handle_inbound``. The latter is the
    human-platform onboarding path that ferry inbound MUST bypass to
    keep peer-fleet identities out of the ``approved_users`` table.
    See PR #414 round-2 — round-1 had this wired to ``handle_inbound``,
    which leaked the peer-fleet identity into the human-approval flow.
    """

    def __init__(self) -> None:
        self.received: list = []
        self.dispatched_for: list[str] = []

    async def dispatch_pre_authorized(self, agent_name, broker_msg) -> None:  # noqa: ANN001
        self.dispatched_for.append(agent_name)
        self.received.append(broker_msg)


class FakeReflection:
    """Reflection stand-in for FakeMemoryStore.insert."""

    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)
        if not getattr(self, "id", ""):
            self.id = f"refl-{abs(hash(self.content)) % 10**8}"


class FakeMemoryStore:
    """Records every insert call."""

    def __init__(self) -> None:
        self.inserted: list = []

    def insert(self, reflection):  # noqa: ANN001
        # Accept both the real Reflection and our FakeReflection stand-in.
        # Add an id if not present.
        if not getattr(reflection, "id", ""):
            reflection.id = f"refl-{len(self.inserted) + 1}"
        self.inserted.append(reflection)
        return reflection


class FakeTaskStore:
    """Records every create_task call."""

    def __init__(self) -> None:
        self.created: list = []

    def create_task(self, **kwargs):
        kwargs["id"] = f"task-{len(self.created) + 1}"
        self.created.append(kwargs)
        return kwargs


# -- substrate.py unit tests ---------------------------------------------------


class TestTypeMapping:
    def test_fact_event_reference_map_to_pinky_memory_fact(self):
        for sub in ("fact", "event", "reference"):
            assert map_substrate_type_to_reflection(sub) == "fact"

    def test_feedback_decision_map_to_insight(self):
        for sub in ("feedback", "decision"):
            assert map_substrate_type_to_reflection(sub) == "insight"

    def test_pattern_maps_to_interaction_pattern(self):
        assert map_substrate_type_to_reflection("pattern") == "interaction_pattern"

    def test_pending_does_not_map_to_pinky_memory(self):
        assert map_substrate_type_to_reflection("pending") is None

    def test_unknown_does_not_map(self):
        assert map_substrate_type_to_reflection("nonsense") is None


class TestTrustToSalience:
    @pytest.mark.parametrize(
        "trust,expected",
        [
            (0.0, 1),
            (0.1, 1),  # 1 + round(0.4) = 1 + 0 = 1
            (0.25, 2),  # 1 + round(1.0) = 2
            (0.5, 3),
            (0.7, 4),  # Entry 2 from §12 — trust 0.7 → salience 4
            (0.95, 5),  # Entry 1 from §12 — trust 0.95 → salience 5
            (1.0, 5),
            (-0.1, 1),  # clamped
            (1.5, 5),  # clamped
        ],
    )
    def test_formula(self, trust, expected):
        assert trust_to_salience(trust) == expected


class TestPortHistory:
    def test_single_hop_with_traversal_record(self):
        entry = _entry_1_feedback()
        envelope = _build_envelope_substrate_batch([entry])
        populate_port_history(entry, envelope, "ferry://pinkybot/barsik")

        # One traversal hop in fixture → broker hop + final delivery hop = 2 entries
        assert len(entry.port_history) == 2
        assert entry.port_history[0].from_ == "misha@pinky.local"
        assert entry.port_history[0].to == "ferry-broker:misha-local"
        assert entry.port_history[-1].to == "ferry://pinkybot/barsik"
        # All hops re_grounded=False on inbound (§6.4)
        assert all(p.re_grounded is False for p in entry.port_history)
        # attested_by distinction (round-2 finding #5):
        # broker-stamped hop gets "broker", receiver-fabricated final hop "receiver"
        assert entry.port_history[0].attested_by == "broker"
        assert entry.port_history[-1].attested_by == "receiver"

    def test_no_traversal_writes_single_synthetic_hop(self):
        entry = _entry_1_feedback()
        envelope = FerryEnvelope(
            v="0.1",
            id="abc",
            from_="misha@pinky.local",
            to="ferry://pinkybot/barsik",
            ts=1,
            body={"kind": "substrate.entry", "entry": _entry_to_dict(entry)},
        )
        populate_port_history(entry, envelope, "ferry://pinkybot/barsik")
        assert len(entry.port_history) == 1
        assert entry.port_history[0].from_ == "misha@pinky.local"
        assert entry.port_history[0].to == "ferry://pinkybot/barsik"
        # No traversal records → entire hop is receiver-attested
        assert entry.port_history[0].attested_by == "receiver"


class TestToReflectionInput:
    def test_entry_1_lands_as_insight_salience_5(self):
        e = _entry_1_feedback()
        ref = to_reflection_input(e)

        assert ref["type"] == "insight"
        assert ref["salience"] == 5
        assert ref["entities"] == ["brad"]
        assert ref["source_channel"] == "ferry"
        assert "Brad wants terse responses" in ref["content"]

        # Sidecar preserves source.by per §6.1
        sidecar = json.loads(ref["context"])
        assert sidecar["substrate_id"] == e.id
        assert sidecar["substrate_type"] == "feedback"
        assert sidecar["source"]["by"] == "misha@pinky.local"
        assert sidecar["source"]["origin"] == "user-stated"
        assert sidecar["scope"] == {"kind": "user", "ref": "brad"}

    def test_entry_2_lands_as_interaction_pattern_salience_4_with_links(self):
        e = _entry_2_pattern()
        ref = to_reflection_input(e)

        assert ref["type"] == "interaction_pattern"
        assert ref["salience"] == 4
        assert ref["entities"] == ["brad"]

        sidecar = json.loads(ref["context"])
        # links surface preserved (no native pinky-memory edge surface; v0.2 §3.2.1 recipe)
        assert sidecar["links"] == ["0c7e3d12-9f8a-4f1e-b6e2-2d8a8c1f4f9a"]

    def test_pending_entry_raises(self):
        e = _entry_1_feedback()
        e.type = "pending"  # type: ignore[assignment]
        with pytest.raises(ValueError, match="pinky-self"):
            to_reflection_input(e)


class TestClassifyEntryDestination:
    def test_pending_routes_to_pinky_self(self):
        e = _entry_1_feedback()
        e.type = "pending"  # type: ignore[assignment]
        assert classify_entry_destination(e) == "pinky-self"

    def test_decayed_lifecycle_skips(self):
        e = _entry_1_feedback()
        e.lifecycle = SubstrateLifecycle(state="decayed")
        assert classify_entry_destination(e) == "skipped"

    def test_active_feedback_routes_to_pinky_memory(self):
        assert classify_entry_destination(_entry_1_feedback()) == "pinky-memory"


# -- HostPinky.deliver() integration tests -------------------------------------


@pytest.fixture
def allow_misha_acl() -> dict[str, list[AgentCardSelector]]:
    """Barsik has an ACL allowing the entire pinky.local fleet."""
    return {
        "barsik": [AgentCardSelector(fleet="pinky.local", agent_id="*")],
    }


@pytest.mark.asyncio
class TestDeliverMessage:
    async def test_message_routes_to_broker_with_ferry_metadata(self, allow_misha_acl):
        registry = FakeRegistry(allow_misha_acl)
        broker = FakeBroker()
        host = HostPinky(registry=registry, broker=broker)

        envelope = FerryEnvelope(
            v="0.1",
            id="msg-001",
            from_="misha@pinky.local",
            to="ferry://pinkybot/barsik",
            ts=1746846000000,
            body={"kind": "message", "text": "ping from misha"},
        )

        result = await host.deliver(envelope)

        assert result.status == "delivered"
        assert len(broker.received) == 1
        bm = broker.received[0]
        assert bm.platform == "ferry"
        assert bm.agent_name == "barsik"
        assert bm.content == "ping from misha"
        assert bm.message_id == "msg-001"
        assert bm.metadata["ferry"]["from"] == "misha@pinky.local"

    async def test_unknown_agent_rejected(self, allow_misha_acl):
        registry = FakeRegistry(allow_misha_acl)
        broker = FakeBroker()
        host = HostPinky(registry=registry, broker=broker)

        envelope = FerryEnvelope(
            v="0.1",
            id="msg-002",
            from_="misha@pinky.local",
            to="ferry://pinkybot/nonexistent",
            ts=1,
            body={"kind": "message", "text": "x"},
        )

        result = await host.deliver(envelope)
        assert result.status == "rejected"
        assert result.reason == "unknown_agent"
        assert broker.received == []

    async def test_acl_denied_rejected(self):
        # Empty ACL → deny all
        registry = FakeRegistry({"barsik": []})
        broker = FakeBroker()
        host = HostPinky(registry=registry, broker=broker)

        envelope = FerryEnvelope(
            v="0.1",
            id="msg-003",
            from_="pulse@studio@sigil",
            to="ferry://pinkybot/barsik",
            ts=1,
            body={"kind": "message", "text": "x"},
        )

        result = await host.deliver(envelope)
        assert result.status == "rejected"
        assert result.reason == "acl_denied"
        assert broker.received == []


@pytest.mark.asyncio
class TestDeliverSubstrateBatch:
    """Acceptance test: substrate v0.1 §12 worked example, ported A → B."""

    async def test_two_entries_land_in_pinky_memory_with_correct_shape(
        self, allow_misha_acl
    ):
        registry = FakeRegistry(allow_misha_acl)
        broker = FakeBroker()
        memory = FakeMemoryStore()
        tasks = FakeTaskStore()
        host = HostPinky(
            registry=registry,
            broker=broker,
            memory_store=memory,
            task_store=tasks,
        )

        envelope = _build_envelope_substrate_batch(
            [_entry_1_feedback(), _entry_2_pattern()]
        )

        # Patch out the real Reflection import inside the host_pinky module so
        # we can run the test without the pinky_memory dependency providing a
        # concrete model. The test substitutes our FakeReflection.
        import pinky_daemon.ferry.host_pinky as host_pinky_mod

        original_insert = host_pinky_mod.HostPinky._insert_reflection

        def _fake_insert(self, ref_kwargs):  # noqa: ANN001
            r = FakeReflection(**ref_kwargs)
            self._memory_store.insert(r)
            return r.id

        host_pinky_mod.HostPinky._insert_reflection = _fake_insert  # type: ignore[method-assign]
        try:
            result = await host.deliver(envelope)
        finally:
            host_pinky_mod.HostPinky._insert_reflection = original_insert  # type: ignore[method-assign]

        assert result.status == "delivered"
        assert len(memory.inserted) == 2
        assert len(tasks.created) == 0  # no `pending` entries in §12 example

        # Entry 1: feedback → insight, salience 5
        e1 = memory.inserted[0]
        assert e1.type == "insight"
        assert e1.salience == 5
        assert "Brad wants terse responses" in e1.content
        sidecar1 = json.loads(e1.context)
        assert sidecar1["source"]["by"] == "misha@pinky.local"  # §6.1 preserved
        assert sidecar1["substrate_type"] == "feedback"
        assert len(sidecar1["port_history"]) >= 1
        assert sidecar1["port_history"][-1]["to"] == "ferry://pinkybot/barsik"

        # Entry 2: pattern → interaction_pattern, salience 4, links preserved
        e2 = memory.inserted[1]
        assert e2.type == "interaction_pattern"
        assert e2.salience == 4
        sidecar2 = json.loads(e2.context)
        assert sidecar2["links"] == ["0c7e3d12-9f8a-4f1e-b6e2-2d8a8c1f4f9a"]


# -- Address parsing -----------------------------------------------------------


class TestAddressParsing:
    def test_parse_pinkybot_address_extracts_agent(self):
        assert parse_pinkybot_address("ferry://pinkybot/barsik") == "barsik"

    def test_parse_pinkybot_address_rejects_other_fleets(self):
        assert parse_pinkybot_address("ferry://sigil/pulse") is None
        assert parse_pinkybot_address("misha@pinky.local") is None

    def test_parse_peer_card_at_form(self):
        assert parse_peer_card("pulse@studio@sigil") == ("sigil", "pulse@studio@sigil")
        assert parse_peer_card("misha@pinky.local") == ("pinky.local", "misha@pinky.local")

    def test_parse_peer_card_ferry_form(self):
        assert parse_peer_card("ferry://sigil/pulse") == ("sigil", "ferry://sigil/pulse")


class TestAgentCardSelector:
    def test_empty_selector_rejected(self):
        with pytest.raises(ValueError, match="at least one"):
            AgentCardSelector()

    def test_fleet_wildcard_matches_any_agent(self):
        sel = AgentCardSelector(fleet="sigil", agent_id="*")
        assert sel.matches("sigil", "pulse@studio@sigil") is True
        assert sel.matches("sigil", "kain@ces-mini@sigil") is True
        assert sel.matches("pinky.local", "misha@pinky.local") is False

    def test_specific_match(self):
        sel = AgentCardSelector(fleet="sigil", agent_id="pulse@studio@sigil")
        assert sel.matches("sigil", "pulse@studio@sigil") is True
        assert sel.matches("sigil", "kain@ces-mini@sigil") is False


# -- AgentRegistry peer_fleet_acl persistence ---------------------------------


@pytest.fixture
def real_registry():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    r = AgentRegistry(db_path=path)
    r.register("barsik", model="opus")
    yield r
    r.close()
    os.unlink(path)


class TestPeerFleetAclPersistence:
    def test_unset_returns_empty_list(self, real_registry):
        assert real_registry.get_peer_fleet_acl("barsik") == []

    def test_unknown_agent_returns_empty_list(self, real_registry):
        assert real_registry.get_peer_fleet_acl("nonexistent") == []

    def test_has_agent(self, real_registry):
        assert real_registry.has_agent("barsik") is True
        assert real_registry.has_agent("nonexistent") is False

    def test_set_and_get_roundtrip(self, real_registry):
        real_registry.set_peer_fleet_acl(
            "barsik",
            [
                {"fleet": "sigil", "agent_id": "pulse@studio@sigil", "pinky_type": None},
                {"fleet": "sigil", "agent_id": "kain@ces-mini@sigil", "pinky_type": None},
            ],
        )
        loaded = real_registry.get_peer_fleet_acl("barsik")
        assert len(loaded) == 2
        assert loaded[0]["fleet"] == "sigil"
        assert loaded[0]["agent_id"] == "pulse@studio@sigil"

    def test_set_drops_empty_selectors(self, real_registry):
        real_registry.set_peer_fleet_acl(
            "barsik",
            [
                {"fleet": "sigil", "agent_id": "pulse@studio@sigil"},
                {"fleet": "", "agent_id": "", "pinky_type": ""},  # empty — dropped
                {},  # empty — dropped
                "not-a-dict",  # type-invalid — dropped
            ],
        )
        loaded = real_registry.get_peer_fleet_acl("barsik")
        assert len(loaded) == 1
        assert loaded[0]["agent_id"] == "pulse@studio@sigil"

    def test_set_replaces_not_merges(self, real_registry):
        real_registry.set_peer_fleet_acl(
            "barsik", [{"fleet": "sigil", "agent_id": "*"}]
        )
        real_registry.set_peer_fleet_acl(
            "barsik", [{"fleet": "pinky.local", "agent_id": "*"}]
        )
        loaded = real_registry.get_peer_fleet_acl("barsik")
        assert len(loaded) == 1
        assert loaded[0]["fleet"] == "pinky.local"

    def test_add_idempotent(self, real_registry):
        added1 = real_registry.add_peer_fleet_acl(
            "barsik", fleet="sigil", agent_id="pulse@studio@sigil"
        )
        added2 = real_registry.add_peer_fleet_acl(
            "barsik", fleet="sigil", agent_id="pulse@studio@sigil"
        )
        assert added1 is True
        assert added2 is True  # idempotent — already-present is success
        assert len(real_registry.get_peer_fleet_acl("barsik")) == 1

    def test_add_rejects_empty(self, real_registry):
        added = real_registry.add_peer_fleet_acl("barsik")
        assert added is False
        assert real_registry.get_peer_fleet_acl("barsik") == []

    def test_remove(self, real_registry):
        real_registry.add_peer_fleet_acl("barsik", fleet="sigil", agent_id="pulse@studio@sigil")
        real_registry.add_peer_fleet_acl("barsik", fleet="sigil", agent_id="kain@ces-mini@sigil")
        removed = real_registry.remove_peer_fleet_acl(
            "barsik", fleet="sigil", agent_id="pulse@studio@sigil"
        )
        assert removed == 1
        loaded = real_registry.get_peer_fleet_acl("barsik")
        assert len(loaded) == 1
        assert loaded[0]["agent_id"] == "kain@ces-mini@sigil"

    def test_remove_no_match_returns_zero(self, real_registry):
        real_registry.add_peer_fleet_acl("barsik", fleet="sigil", agent_id="*")
        removed = real_registry.remove_peer_fleet_acl(
            "barsik", fleet="other", agent_id="x"
        )
        assert removed == 0


@pytest.mark.asyncio
class TestHostPinkyAgainstRealRegistry:
    """End-to-end: real AgentRegistry + FakeBroker confirms ACL plumbing works."""

    async def test_acl_allows_then_denies_after_remove(self, real_registry):
        broker = FakeBroker()
        host = HostPinky(registry=real_registry, broker=broker)

        envelope = FerryEnvelope(
            v="0.1",
            id="msg-allow-flow",
            from_="pulse@studio@sigil",
            to="ferry://pinkybot/barsik",
            ts=1,
            body={"kind": "message", "text": "hello"},
        )

        # Default: deny-all
        result = await host.deliver(envelope)
        assert result.status == "rejected"
        assert result.reason == "acl_denied"

        # Allow Pulse
        real_registry.add_peer_fleet_acl(
            "barsik", fleet="sigil", agent_id="pulse@studio@sigil"
        )
        result = await host.deliver(envelope)
        assert result.status == "delivered"
        assert len(broker.received) == 1

        # Remove Pulse — back to deny
        real_registry.remove_peer_fleet_acl(
            "barsik", fleet="sigil", agent_id="pulse@studio@sigil"
        )
        result = await host.deliver(envelope)
        assert result.status == "rejected"
        assert result.reason == "acl_denied"

    async def test_unknown_agent_uses_real_has_agent(self, real_registry):
        broker = FakeBroker()
        host = HostPinky(registry=real_registry, broker=broker)

        envelope = FerryEnvelope(
            v="0.1",
            id="msg-unknown",
            from_="misha@pinky.local",
            to="ferry://pinkybot/nonexistent",
            ts=1,
            body={"kind": "message", "text": "hi"},
        )

        result = await host.deliver(envelope)
        assert result.status == "rejected"
        assert result.reason == "unknown_agent"


# -- API: peer-fleet-acl endpoints --------------------------------------------


@pytest.fixture
def api_client():
    """FastAPI TestClient with a fresh registry containing 'barsik'."""
    from fastapi.testclient import TestClient

    from pinky_daemon.api import create_api

    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    app = create_api(max_sessions=10, default_working_dir="/tmp", db_path=path)
    client = TestClient(app)
    # Register barsik via the API
    r = client.post("/agents", json={"name": "barsik", "model": "opus"})
    assert r.status_code in (200, 201), f"agent register failed: {r.status_code} {r.text}"
    yield client
    client.close()
    os.unlink(path)


class TestPeerFleetAclApi:
    def test_get_empty_for_known_agent(self, api_client):
        r = api_client.get("/agents/barsik/peer-fleet-acl")
        assert r.status_code == 200
        assert r.json() == {"agent": "barsik", "selectors": []}

    def test_get_404_for_unknown_agent(self, api_client):
        r = api_client.get("/agents/nonexistent/peer-fleet-acl")
        assert r.status_code == 404

    def test_post_adds_selector(self, api_client):
        r = api_client.post(
            "/agents/barsik/peer-fleet-acl",
            json={"fleet": "sigil", "agent_id": "pulse@studio@sigil"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["added"] is True
        assert len(body["selectors"]) == 1
        assert body["selectors"][0]["fleet"] == "sigil"
        assert body["selectors"][0]["agent_id"] == "pulse@studio@sigil"

    def test_post_rejects_empty_selector(self, api_client):
        r = api_client.post(
            "/agents/barsik/peer-fleet-acl",
            json={"fleet": "", "agent_id": "", "pinky_type": ""},
        )
        assert r.status_code == 400
        assert "at least one" in r.json()["detail"]

    def test_post_idempotent(self, api_client):
        for _ in range(3):
            r = api_client.post(
                "/agents/barsik/peer-fleet-acl",
                json={"fleet": "sigil", "agent_id": "pulse@studio@sigil"},
            )
            assert r.status_code == 200
        r = api_client.get("/agents/barsik/peer-fleet-acl")
        assert len(r.json()["selectors"]) == 1

    def test_put_replaces_full_acl(self, api_client):
        # Add two
        api_client.post(
            "/agents/barsik/peer-fleet-acl",
            json={"fleet": "sigil", "agent_id": "pulse@studio@sigil"},
        )
        api_client.post(
            "/agents/barsik/peer-fleet-acl",
            json={"fleet": "sigil", "agent_id": "kain@ces-mini@sigil"},
        )
        # PUT to replace with just one different selector
        r = api_client.put(
            "/agents/barsik/peer-fleet-acl",
            json={"selectors": [{"fleet": "pinky.local", "agent_id": "*"}]},
        )
        assert r.status_code == 200
        body = r.json()
        assert len(body["selectors"]) == 1
        assert body["selectors"][0]["fleet"] == "pinky.local"

    def test_delete_by_query_removes_matching(self, api_client):
        api_client.post(
            "/agents/barsik/peer-fleet-acl",
            json={"fleet": "sigil", "agent_id": "pulse@studio@sigil"},
        )
        api_client.post(
            "/agents/barsik/peer-fleet-acl",
            json={"fleet": "sigil", "agent_id": "kain@ces-mini@sigil"},
        )
        r = api_client.delete(
            "/agents/barsik/peer-fleet-acl",
            params={"fleet": "sigil", "agent_id": "pulse@studio@sigil"},
        )
        assert r.status_code == 200
        assert r.json()["removed"] == 1
        remaining = r.json()["selectors"]
        assert len(remaining) == 1
        assert remaining[0]["agent_id"] == "kain@ces-mini@sigil"

    def test_delete_no_match_returns_zero(self, api_client):
        api_client.post(
            "/agents/barsik/peer-fleet-acl",
            json={"fleet": "sigil", "agent_id": "*"},
        )
        r = api_client.delete(
            "/agents/barsik/peer-fleet-acl",
            params={"fleet": "other", "agent_id": "pulse"},
        )
        assert r.status_code == 200
        assert r.json()["removed"] == 0


# -- Round-2 additions: transient ACL, wildcard remove, pending substrate, broker integration --


class _RaisingRegistry:
    """Registry stub whose get_peer_fleet_acl raises a sqlite3.Error.

    Models the "DB locked / schema mismatch during rolling deploy" scenario
    that Misha's PR #414 review #1 flagged. See round-2 finding #1.
    """

    def __init__(self) -> None:
        import sqlite3 as _sq3
        self._exc = _sq3.OperationalError("database is locked")

    def has_agent(self, name: str) -> bool:
        return name == "barsik"

    def get_peer_fleet_acl(self, name: str):  # noqa: ANN001
        raise self._exc


@pytest.mark.asyncio
class TestTransientACLLoadFailure:
    """A sqlite3 error during ACL load yields ``transient_failure``,
    NOT ``rejected``/``acl_denied`` (round-2 finding #1).

    Without this distinction, a brief DB lock during a rolling deploy
    permanently drops a real ACL-allowed message because the broker
    can't tell "operator hiccup" from "policy denial."
    """

    async def test_sqlite_error_during_acl_load_yields_transient_failure(self):
        registry = _RaisingRegistry()
        broker = FakeBroker()
        host = HostPinky(registry=registry, broker=broker)

        envelope = FerryEnvelope(
            v="0.1",
            id="msg-transient",
            from_="misha@pinky.local",
            to="ferry://pinkybot/barsik",
            ts=1,
            body={"kind": "message", "text": "x"},
        )

        result = await host.deliver(envelope)

        assert result.status == "transient_failure"
        assert result.reason == "acl_load_failed"
        assert "database is locked" in result.detail.get("detail", "")
        # Critically: did NOT degrade to rejected/acl_denied
        assert result.status != "rejected"
        # Broker not invoked (we never got to dispatch)
        assert broker.received == []
        # Stats incremented on the right counter
        assert host.stats["transient_failures"] == 1
        assert host.stats["rejected"] == 0


class TestACLRemoveWildcardSemantics:
    """The exact-match contract on ``remove_peer_fleet_acl`` is documented
    on the method (round-2 nit). This test pins the documented behavior:
    a stored ``agent_id="*"`` is removed only by passing ``agent_id="*"``.
    """

    def test_remove_without_wildcard_does_not_match_stored_wildcard(self, real_registry):
        # Store a fleet-wide allow
        real_registry.add_peer_fleet_acl("barsik", fleet="sigil", agent_id="*")

        # Forgetting the wildcard MUST silently zero-remove (don't surprise-delete)
        removed = real_registry.remove_peer_fleet_acl("barsik", fleet="sigil")
        assert removed == 0
        assert len(real_registry.get_peer_fleet_acl("barsik")) == 1

        # Passing the exact stored selector removes it
        removed = real_registry.remove_peer_fleet_acl(
            "barsik", fleet="sigil", agent_id="*",
        )
        assert removed == 1
        assert real_registry.get_peer_fleet_acl("barsik") == []


@pytest.mark.asyncio
class TestDeliverPendingSubstrate:
    """End-to-end test of the ``pending`` substrate path through ``deliver()``.

    Round-2 finding #2 / test gap: round-1 only exercised entries that
    routed to pinky-memory; the pinky-self ``_insert_task`` path was
    untested, which kept a latent ternary bug undetectable. This fixture
    routes a ``pending`` entry through the full deliver() flow and
    confirms it lands in the task store.
    """

    async def test_pending_entry_routes_to_task_store(self, allow_misha_acl):
        registry = FakeRegistry(allow_misha_acl)
        broker = FakeBroker()
        memory = FakeMemoryStore()
        tasks = FakeTaskStore()
        host = HostPinky(
            registry=registry,
            broker=broker,
            memory_store=memory,
            task_store=tasks,
        )

        pending_entry = SubstrateEntry(
            id="pending-001",
            type="pending",  # routes to pinky-self per classify_entry_destination
            scope=SubstrateScope(kind="user", ref="brad"),
            source=SubstrateSource(
                origin="agent-inferred",
                by="misha@pinky.local",
                evidence="Brad mentioned needing to follow up with Matt re: NATS creds",
                trust=0.85,
            ),
            created_at="2026-05-09T10:00:00-07:00",
            updated_at="2026-05-09T10:00:00-07:00",
            content="Follow up with Matt about NATS creds for ferry demo",
            links=[],
            lifecycle=SubstrateLifecycle(state="pending"),
            port_history=[],
        )

        envelope = FerryEnvelope(
            v="0.1",
            id="msg-pending",
            from_="misha@pinky.local",
            to="ferry://pinkybot/barsik",
            ts=1746846000000,
            body={
                "kind": "substrate.entry",
                "entry": _entry_to_dict(pending_entry),
            },
        )

        result = await host.deliver(envelope)

        assert result.status == "delivered"
        # Pending → pinky-self, not pinky-memory
        assert len(tasks.created) == 1
        assert len(memory.inserted) == 0
        # Confirm the task carries the right substrate provenance
        task = tasks.created[0]
        assert "Follow up with Matt" in task["title"] or "Follow up with Matt" in task.get("description", "")
        assert task["assigned_agent"] == "barsik"
        # IngestResult shape
        results = result.detail["ingested"]
        assert results[0]["target_store"] == "pinky-self"
        assert results[0]["target_id"]  # non-empty (round-2 finding #2 — ternary returned object str before)


@pytest.mark.asyncio
class TestBrokerIntegrationFerryInbound:
    """Regression guard for round-2 blocking finding (broker boundary leak).

    Round-1 wired host-pinky's message dispatch through ``broker.handle_inbound``
    — which runs the human-platform onboarding flow (``get_user_status`` →
    ``add_pending_user`` → ``/approve_…`` Telegram prompt to the owner) and
    writes the ferry sender_id into the human ``approved_users`` table. That
    defeated the load-bearing identity-primitive separation claim of the PR.

    Round-2 fixed it by routing through ``broker.dispatch_pre_authorized``
    (a public broker entry point that bypasses onboarding). This test pipes
    a ferry envelope through real Broker + real AgentRegistry and confirms:

      1. ``approved_users`` does NOT contain the ferry sender_id
      2. The owner does NOT receive an ``/approve_…`` Telegram prompt
      3. No pending message is queued under the ferry sender_id

    If a future refactor of ``handle_inbound`` re-introduces the leak, this
    test fails. None of the FakeBroker tests above would have caught it.
    """

    async def test_ferry_inbound_does_not_touch_human_approval_path(self, real_registry):
        # Real Broker, real Registry. send_callback records every outbound;
        # we assert no /approve_ prompt was emitted.
        from pinky_daemon.broker import MessageBroker

        sent: list[tuple[str, str, str, str]] = []

        async def record_send(agent_name, platform, chat_id, content):  # noqa: ANN001
            sent.append((agent_name, platform, chat_id, content))

        broker = MessageBroker(
            real_registry,
            session_manager=None,  # not exercised — _route_streaming finds no session
            send_callback=record_send,
        )

        # Allow misha@pinky.local fleet-wide
        real_registry.add_peer_fleet_acl(
            "barsik", fleet="pinky.local", agent_id="*",
        )

        host = HostPinky(registry=real_registry, broker=broker)
        envelope = FerryEnvelope(
            v="0.1",
            id="msg-ferry-integration",
            from_="misha@pinky.local",
            to="ferry://pinkybot/barsik",
            ts=1746846000000,
            body={"kind": "message", "text": "hello from misha across the fleet"},
        )

        result = await host.deliver(envelope)
        assert result.status == "delivered"

        # 1. ferry sender NOT in approved_users (the human-identity table)
        ferry_user_id = "ferry:misha@pinky.local"
        status = real_registry.get_user_status("barsik", ferry_user_id)
        assert status is None, (
            f"ferry sender_id {ferry_user_id!r} leaked into approved_users "
            f"with status={status!r} — round-1 bug regression"
        )

        # 2. no /approve_ prompt sent (would have gone to owner on round-1 bug)
        for _, _, _, content in sent:
            assert "/approve_" not in content, (
                f"owner received /approve_ prompt for ferry inbound: {content!r}"
            )
            assert "wants to talk to" not in content, (
                f"owner received human-onboarding prompt for ferry inbound: {content!r}"
            )

        # 3. no pending message queued under ferry sender_id
        pending = real_registry.get_pending_messages("barsik", ferry_user_id)
        assert pending == [], (
            f"ferry message queued as pending under {ferry_user_id!r}: {pending}"
        )

    async def test_acl_denied_does_not_leak_either(self, real_registry):
        """Same regression guard, denied path. ACL-deny must not fall back
        to onboarding either — the agent should never see the message and
        the owner should never see an /approve_ prompt."""
        from pinky_daemon.broker import MessageBroker

        sent: list[tuple[str, str, str, str]] = []

        async def record_send(agent_name, platform, chat_id, content):  # noqa: ANN001
            sent.append((agent_name, platform, chat_id, content))

        broker = MessageBroker(
            real_registry, session_manager=None, send_callback=record_send,
        )
        # Empty ACL — default-deny
        host = HostPinky(registry=real_registry, broker=broker)
        envelope = FerryEnvelope(
            v="0.1",
            id="msg-ferry-deny",
            from_="pulse@studio@sigil",  # not on barsik's empty ACL
            to="ferry://pinkybot/barsik",
            ts=1,
            body={"kind": "message", "text": "x"},
        )

        result = await host.deliver(envelope)
        assert result.status == "rejected"
        assert result.reason == "acl_denied"

        # Owner gets nothing
        assert sent == []
        # Registry untouched
        assert real_registry.get_user_status(
            "barsik", "ferry:pulse@studio@sigil",
        ) is None
