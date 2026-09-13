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
    discovered = _discover_pollers()
    assert FuturePoller in discovered
    # A minimum inventory prevents a discovery filter from silently dropping
    # an existing transport. Collection above still includes future classes.
    assert {
        "TelegramPoller", "BrokerTelegramPoller", "BrokeriMessagePoller",
        "BrokerDiscordPoller", "BrokerSlackPoller", "BrokerBuzzPoller",
    } <= {cls.__name__ for cls in discovered}


@pytest.mark.parametrize("broken_field, error_type", [
    ("poll_count", RuntimeError),
    ("agent_name", RuntimeError),
    ("last_poll_ok", RuntimeError),
    ("connect_attempts", AttributeError),
    ("inbound_stalled_s", AttributeError),
    ("stall_alerted", AttributeError),
    ("watchdog_fires", AttributeError),
    ("last_poll_ok", AttributeError),
])
def test_broker_status_degrades_one_row(status_client, caplog, broken_field, error_type):
    class BrokenPoller:
        agent_name = "broken-test"
        poll_count = 0
        is_running = False

        def fail(self):
            raise error_type("status unavailable")

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
        "error": f"{error_type.__name__}: status unavailable",
    }
    for row, sibling in ((rows[0], before), (rows[2], after)):
        assert "error" not in row
        assert row["agent"] == sibling.agent_name
        assert row["polls"] == sibling.poll_count
        assert row["running"] is sibling.is_running
    assert any(
        record.levelno == logging.ERROR
        and "BrokenPoller" in record.getMessage()
        and f"{error_type.__name__}: status unavailable" in record.getMessage()
        for record in caplog.records
    )


@pytest.mark.parametrize("storage", ["class", "instance"])
def test_broker_status_preserves_optional_telemetry(status_client, storage):
    class HealthyPoller:
        agent_name = "healthy-test"
        poll_count = 2
        is_running = True

    client, active = status_client
    poller = HealthyPoller()
    telemetry = {
        "connect_attempts": 3,
        "inbound_stalled_s": 12.5,
        "stall_alerted": True,
        "watchdog_fires": 2,
        "last_poll_ok": 1.0,
    }
    target = HealthyPoller if storage == "class" else poller
    for field, value in telemetry.items():
        setattr(target, field, value)
    active.append(poller)
    response = client.get("/broker/status")
    assert response.status_code == 200, response.text
    row, = response.json()["active_pollers"]
    assert "error" not in row
    for field in telemetry.keys() - {"last_poll_ok"}:
        assert row[field] == telemetry[field]
    assert isinstance(row["last_poll_ok_age_s"], float)
    assert row["last_poll_ok_age_s"] > 0


@pytest.mark.parametrize("broken_field, value", [
    pytest.param("agent_name", object(), id="agent_name-object"),
    pytest.param("poll_count", object(), id="poll_count-object"),
    pytest.param("poll_count", "5", id="poll_count-str"),
    pytest.param("poll_count", True, id="poll_count-bool"),
    pytest.param("is_running", 1, id="is_running-int"),
])
def test_broker_status_rejects_invalid_types(status_client, caplog, broken_field, value):
    class BrokenPoller:
        agent_name = "broken-test"
        poll_count = 0
        is_running = False

    setattr(BrokenPoller, broken_field, value)
    client, active = status_client
    active.extend([
        BrokenPoller(),
        SimpleNamespace(agent_name="healthy-test", poll_count=3, is_running=True),
    ])
    with caplog.at_level(logging.ERROR):
        response = client.get("/broker/status")
    assert response.status_code == 200, response.text
    broken, healthy = response.json()["active_pollers"]
    assert set(broken) == {"agent", "error"}
    assert broken["agent"] == ("?" if broken_field == "agent_name" else "broken-test")
    assert broken["error"].startswith("TypeError:")
    assert broken_field in broken["error"]
    assert type(value).__name__ in broken["error"]
    assert healthy["agent"] == "healthy-test"
    assert healthy["polls"] == 3
    assert healthy["running"] is True
    assert any(
        record.levelno == logging.ERROR
        and "BrokenPoller" in record.getMessage()
        and broken["error"] in record.getMessage()
        for record in caplog.records
    )


@pytest.mark.parametrize("error_type", [RuntimeError, AttributeError])
def test_broker_status_degrades_stats(status_client, monkeypatch, caplog, error_type):
    client, active = status_client
    broker = client.app.state.broker
    active.append(SimpleNamespace(agent_name="healthy-test", poll_count=5, is_running=True))

    def fail(self):
        raise error_type("stats unavailable")

    with monkeypatch.context() as patch:
        patch.setattr(type(broker), "stats", property(fail))
        with caplog.at_level(logging.ERROR):
            response = client.get("/broker/status")
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["stats"] == {"error": f"{error_type.__name__}: stats unavailable"}
    row, = payload["active_pollers"]
    assert "error" not in row
    assert row["agent"] == "healthy-test"
    assert row["polls"] == 5
    assert row["running"] is True
    assert any(
        record.levelno == logging.ERROR
        and "stats" in record.getMessage()
        and payload["stats"]["error"] in record.getMessage()
        for record in caplog.records
    )
