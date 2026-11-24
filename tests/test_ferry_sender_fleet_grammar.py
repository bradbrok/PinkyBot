"""Fleet syntax keeps scoped agent identities unique across credentials."""

from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from pinky_daemon.ferry.host_pinky import HostPinky
from pinky_daemon.ferry.inbound_server import build_ferry_app
from pinky_daemon.ferry.types import AgentCardSelector
from tests.test_ferry_host_pinky import FakeBroker, FakeRegistry
from tests.test_ferry_peer_secrets import (
    GLOBAL_SECRET,
    PEER_SECRET,
    _config,
    _DeliveryRecorder,
    _post,
)


def test_global_uri_fleet_cannot_alias_a_scoped_peer_agent_acl():
    sender = "ferry://scope@peer-one/sender"
    selector = AgentCardSelector(agent_id="sender@scope@peer-one", fleet=None)
    host = HostPinky(
        registry=FakeRegistry({"receiver": [selector]}), broker=FakeBroker(), fleet_name="local"
    )
    assert host._acl_allows("receiver", sender), "the ACL must reproduce the scoped-id alias"
    spy = AsyncMock(wraps=host.deliver)
    host.deliver = spy
    with TestClient(build_ferry_app(host_pinky=host, config=_config())) as client:
        response = _post(client, token=GLOBAL_SECRET, sender=sender)
    assert response.status_code == 403
    assert response.json() == {"ok": False, "error": "sender fleet does not match credential"}
    spy.assert_not_awaited()


@pytest.mark.parametrize("sender", [
    "ferry://scope/peer-one/sender", "ferry://scope%40peer-one/sender", "ferry://*/sender",
    "ferry://.peer-one/sender", "ferry://-peer-one/sender", "ferry:///sender",
])
def test_every_invalid_uri_fleet_is_refused_before_delivery(sender):
    recorder = _DeliveryRecorder()
    with TestClient(build_ferry_app(host_pinky=recorder, config=_config())) as client:
        response = _post(client, token=GLOBAL_SECRET, sender=sender)
    assert response.status_code == 403
    assert response.json() == {"ok": False, "error": "sender fleet does not match credential"}
    assert recorder.delivered == []


@pytest.mark.parametrize("sender,token", [
    ("ferry://peer-one/sender@scope", PEER_SECRET), ("ferry://a.b/sender", GLOBAL_SECRET),
])
def test_scoped_agent_and_dotted_fleet_remain_valid(sender, token):
    recorder = _DeliveryRecorder()
    with TestClient(build_ferry_app(host_pinky=recorder, config=_config())) as client:
        response = _post(client, token=token, sender=sender)
    assert response.status_code == 200
    assert [envelope.from_ for envelope in recorder.delivered] == [sender]
