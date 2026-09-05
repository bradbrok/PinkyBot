"""T9: startup and token PUT share the owner-alert poller factories."""

import ast
import inspect

from fastapi.testclient import TestClient

from pinky_daemon import api, pollers


def test_api_factories_wire_owner_notify(tmp_path, monkeypatch):
    captured = []
    monkeypatch.setattr(api, "SHARED_MCP_ENABLED", False)
    monkeypatch.setattr(pollers, "start_poller", captured.append)
    app = api.create_api(default_working_dir=str(tmp_path), db_path=str(tmp_path / "test.db"))
    agents = app.state.agents
    agents.register("retry-test")
    monkeypatch.setattr(agents, "get_main_agent", lambda: None)
    agents.set_token("retry-test", "telegram", "test-telegram")
    agents.set_token(
        "retry-test",
        "discord",
        "test-discord",
        settings={
            "poll_interval_sec": 2.5,
            "watched_channels": ["test-channel"],
        },
    )
    try:
        with TestClient(app) as client:
            assert len(captured) == 2
            for poller in captured:
                assert callable(poller._owner_notify)
            assert captured[0]._owner_notify is captured[1]._owner_notify
            assert captured[1]._configured_channels == ["test-channel"]
            assert captured[1]._poll_interval == 2.5
            rows = client.get("/broker/status").json()["active_pollers"]
            assert len(rows) == 2
            for row in rows:
                assert row["connect_attempts"] == 0
                assert row["inbound_stalled_s"] is None
                assert row["stall_alerted"] is False
                assert "watchdog_fires" in row and "last_poll_ok_age_s" in row
            for platform in ("telegram", "discord"):
                response = client.put(
                    f"/agents/retry-test/tokens/{platform}",
                    json={
                        "token": "replacement-test-token",
                        "settings": {"poll_interval_sec": 3, "watched_channels": ["replacement"]},
                    },
                )
                assert response.status_code == 200, response.text
                assert captured[-1]._owner_notify is captured[0]._owner_notify
            assert len(captured) == 4
            assert captured[-1]._configured_channels == ["replacement"]
            assert captured[-1]._poll_interval == 3
    finally:
        for poller in captured:
            poller.stop()
            poller._adapter.close()

    # Enumerate every constructor sink: M6 must fail even if a direct PUT
    # construction happens to remember today's owner_notify argument.
    tree = ast.parse(inspect.getsource(api.create_api))
    for platform, constructor in (
        ("telegram", "BrokerTelegramPoller"),
        ("discord", "BrokerDiscordPoller"),
    ):
        factory = f"_new_{platform}_broker_poller"
        nodes = [n for n in ast.walk(tree) if isinstance(n, ast.Call)]
        constructions = [
            n for n in nodes if isinstance(n.func, ast.Name) and n.func.id == constructor
        ]
        assert len(constructions) == 1
        definition = next(
            n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == factory
        )
        assert constructions[0] in list(ast.walk(definition))
        for caller in ("set_agent_token", "on_startup"):
            body = next(
                n
                for n in ast.walk(tree)
                if isinstance(n, ast.AsyncFunctionDef) and n.name == caller
            )
            assert any(
                isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == factory
                for n in ast.walk(body)
            )
