"""Every platform poller must remain observable through the status endpoint."""

import inspect
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from pinky_daemon import api, buzz_inbound, pollers
from pinky_outreach.buzz import BuzzNostrSigner


def _discover_pollers():
    return [
        cls
        for module in (pollers, buzz_inbound)
        for name, cls in inspect.getmembers(module, inspect.isclass)
        if name.endswith("Poller") and not name.startswith("_")
    ]


def _make_poller(cls):
    private_key = bytes.fromhex("11" * 32)
    args = {
        "adapter": MagicMock(),
        "handler": MagicMock(),
        "agent_name": "status-test",
        "broker": SimpleNamespace(handle_inbound=AsyncMock()),
        "registry": MagicMock(),
        "owner_notify": AsyncMock(),
        "app_token": "xapp-test",
        "signing_material": SimpleNamespace(
            agent="status-test",
            private_key=private_key,
            pubkey=BuzzNostrSigner(private_key).pubkey,
            relay_url="wss://relay.example.test",
            community_id="test",
            relay_signing_pubkey=BuzzNostrSigner(private_key).pubkey,
        ),
    }
    # Constructor inputs are stubbed; status properties are always real. A new
    # required input fails collection/execution loudly instead of skipping a class.
    return cls(**{
        name: args[name]
        for name, param in inspect.signature(cls).parameters.items()
        if param.default is inspect.Parameter.empty
        and param.kind not in (param.VAR_POSITIONAL, param.VAR_KEYWORD)
    })


@pytest.fixture
def status_client(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "SHARED_MCP_ENABLED", False)
    app = api.create_api(default_working_dir=str(tmp_path), db_path=str(tmp_path / "test.db"))
    endpoint = next(route.endpoint for route in app.routes if route.path == "/broker/status")
    active = inspect.getclosurevars(endpoint).nonlocals["_broker_pollers"]
    with TestClient(app, raise_server_exceptions=False) as client:
        try:
            yield client, active
        finally:
            active.clear()


@pytest.mark.parametrize("poller_class", _discover_pollers(), ids=lambda cls: cls.__name__)
def test_broker_status_all_pollers(status_client, poller_class):
    client, active = status_client
    poller = _make_poller(poller_class)
    active.append(poller)
    try:
        response = client.get("/broker/status")
        assert response.status_code == 200, response.text
        rows = response.json()["active_pollers"]
        assert len(rows) == 1
        row = rows[0]
        assert "error" not in row, row
        assert isinstance(poller.agent_name, str)
        assert row["agent"] == poller.agent_name
        assert isinstance(row["polls"], int)
        assert row["polls"] == 0
        assert isinstance(row["running"], bool)
        assert row["running"] is False
        if isinstance(poller, pollers.BrokerSlackPoller):
            assert not hasattr(poller, "last_poll_ok")
            assert row["last_poll_ok_age_s"] is None
            assert row["connect_attempts"] == 0
    finally:
        poller.stop()


@pytest.mark.parametrize("module", [pollers, buzz_inbound], ids=lambda module: module.__name__)
def test_discovery_includes_future_poller(module, monkeypatch):
    class FuturePoller:
        pass

    monkeypatch.setattr(module, "FuturePoller", FuturePoller, raising=False)
    assert FuturePoller in _discover_pollers()


@pytest.mark.parametrize("broken_field", ["poll_count", "agent_name", "last_poll_ok"])
def test_broker_status_degrades_one_row(status_client, caplog, broken_field):
    class BrokenPoller:
        agent_name = "broken-test"
        poll_count = 0
        is_running = False

        def fail(self):
            raise RuntimeError("status unavailable")

    setattr(BrokenPoller, broken_field, property(BrokenPoller.fail))
    client, active = status_client
    before = SimpleNamespace(agent_name="before-test", poll_count=7, is_running=True)
    after = SimpleNamespace(agent_name="after-test", poll_count=9, is_running=False)
    active.extend([before, BrokenPoller(), after])
    with caplog.at_level(logging.ERROR):
        response = client.get("/broker/status")
    assert response.status_code == 200, response.text
    rows = response.json()["active_pollers"]
    assert len(rows) == 3
    assert rows[1] == {
        "agent": "?" if broken_field == "agent_name" else "broken-test",
        "error": "RuntimeError: status unavailable",
    }
    for row, sibling in ((rows[0], before), (rows[2], after)):
        assert "error" not in row
        assert row["agent"] == sibling.agent_name
        assert row["polls"] == sibling.poll_count
        assert row["running"] is sibling.is_running
    assert any(
        record.levelno == logging.ERROR
        and "BrokenPoller" in record.getMessage()
        and "RuntimeError: status unavailable" in record.getMessage()
        for record in caplog.records
    )
