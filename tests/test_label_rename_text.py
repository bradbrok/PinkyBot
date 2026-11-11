"""An alias cannot move an id whose text belongs to another registered name."""

import httpx
import pytest

from pinky_daemon.session_identity import resolve_session_owner
from tests.isolated_policy_support import closure, signed
from tests.isolated_policy_support import daemon as daemon
from tests.test_label_rename_claims import (
    _assert_claim_refusal,
    _assert_unchanged,
    _own_session,
    _state,
)
from tests.test_label_rename_refresh import _capture
from tests.test_session_owner_reads import _register
from tests.test_streaming_session_owner_reads import streaming_session

pytestmark = pytest.mark.real_auth


def _fts_capture(tables):
    return {
        name: [[{"hex": value.hex()} if isinstance(value, bytes) else value for value in row]
               for row in rows]
        for name, rows in tables.items()
    }


@pytest.mark.parametrize("query", ["", "?q=renameclaimneedle"], ids=["plain", "q"])
async def test_own_alias_cannot_move_retained_rows_under_another_names_text(
    daemon,
    monkeypatch,
    query,
):
    instance = daemon("off")
    _register(instance, "tenant-peer")
    monkeypatch.setenv("PINKY_ISOLATED_READ_POLICY_MODE", "enforce")
    caller = await _own_session(instance, "peer-unused")
    assert await caller.send("renameclaimneedle own alias row")
    other = streaming_session(instance, "tenant-peer", "unused")
    assert await other.send("renameclaimneedle retained peer row")
    await other.disconnect()
    broker = instance.app.state.broker
    broker.unregister_streaming("tenant-peer", label="unused")
    old_id, new_id = caller.id, "tenant-renamed"
    assert broker.get_streaming_session("tenant", "peer-unused") is caller
    assert broker.get_streaming_session("tenant-peer", "unused") is None
    assert instance.app.state.manager.list() == []
    ownership = closure(instance.app, "_agent_session_ownership")("tenant")
    assert ownership.explicit_owners[old_id] == {"tenant"}
    assert ownership.keeps(old_id)
    assert resolve_session_owner(old_id, ownership.registered_names) == "tenant-peer"
    assert not instance.agents.list_streaming_session_labels({"tenant-peer"})
    assert not instance.agents.has_streaming_session_id_claim({"tenant-peer"}, {old_id, new_id})
    before = _state(instance, caller, old_id, new_id)
    histories = {"before": {}, "after": {}}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=instance.app),
        base_url="http://fixture",
    ) as client:

        async def read_histories(stage):
            for owner in ("tenant", "tenant-peer"):
                path = "/agents/" + owner + "/chat-history"
                response = await client.get(
                    path + query,
                    headers=signed(instance, "GET", path, owner),
                )
                assert response.status_code == 200, response.text
                histories[stage][owner] = response.json()

        await read_histories("before")
        assert any(
            "retained peer row" in row["content"]
            for row in histories["before"]["tenant"]["messages"]
        )
        assert histories["before"]["tenant-peer"]["messages"] == []
        path = "/agents/tenant/streaming-sessions/peer-unused"
        response = await client.patch(
            path,
            json={"label": "renamed"},
            headers=signed(instance, "PATCH", path),
        )
        await read_histories("after")
    after = _state(instance, caller, old_id, new_id)
    _capture(
        "alias-text-" + ("plain" if not query else "q"),
        {
            "status": response.status_code,
            "response": response.json(),
            "histories": histories,
            "old_rows_before": before["old_rows"],
            "new_rows_before": before["new_rows"],
            "old_rows_after": after["old_rows"],
            "new_rows_after": after["new_rows"],
            "fts_before": _fts_capture(before["fts"]),
            "fts_after": _fts_capture(after["fts"]),
        },
    )
    _assert_claim_refusal(response, "peer-unused")
    _assert_unchanged(instance, caller, old_id, new_id, before)
    assert histories["after"] == histories["before"]
