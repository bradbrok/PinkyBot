"""One real-handler positive and a scope negative for each of the 25 draft allows."""

import io
import json
import time
import urllib.request
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from pinky_daemon import voice_routes
from pinky_daemon.broker import BrokerMessage
from pinky_daemon.routes import triggers
from pinky_daemon.transport_state import SessionState
from tests.isolated_policy_support import closure, replace_cell, signed
from tests.isolated_policy_support import daemon as daemon

pytestmark = pytest.mark.real_auth

# Independent review keys: the initial 30 minus the five explicit resource HOLDs.
ALLOW_PAIRS = [
    ("POST", "/broker/thread"),
    ("POST", "/broker/send"),
    ("POST", "/broker/react"),
    ("POST", "/broker/send-voice"),
    ("POST", "/broker/send-gif"),
    ("POST", "/broker/broadcast"),
    ("POST", "/agents/{agent_name}/schedules"),
    ("PATCH", "/agents/{agent_name}/schedules/{schedule_id}"),
    ("DELETE", "/agents/{agent_name}/pending-schedule-wakes/{pending_id}"),
    ("PUT", "/agents/{agent_name}/context"),
    ("POST", "/agents/{name}/streaming/restart"),
    ("POST", "/agents/{agent_name}/heartbeat"),
    ("POST", "/agents/{name}/sessions/{session_label}/effort"),
    ("POST", "/agents/{name}/message"),
    ("POST", "/agents/{name}/mesh/send"),
    ("DELETE", "/agents/{agent_name}/triggers/{trigger_id}"),
    ("POST", "/agents/{agent_name}/triggers/{trigger_id}/test"),
    ("POST", "/api/voice/request"),
    ("POST", "/agents/{name}/effort-drift"),
    ("POST", "/agents/{name}/transport/wake"),
    ("POST", "/agents/{name}/transport/tool-use"),
    ("POST", "/agents/{name}/transport/tool-result"),
    ("POST", "/agents/{name}/transport/stop-failure"),
    ("POST", "/agents/{name}/status"),
    ("POST", "/agents/{name}/policy/evaluate"),
]


def prepared(d, monkeypatch, method, template):
    effects = []
    values = dict(agent_name="tenant", name="tenant", session_label="main")
    body = {}

    def check(result):
        return bool(effects)

    async def deliver(*args, **kwargs):
        effects.append((args, kwargs))
        return {"sent": True, "message_id": "201"}

    if template.startswith("/broker/"):
        body = dict(
            agent_name="tenant",
            platform="telegram",
            chat_id="1",
            content="fixture",
            message_id="101",
            emoji="👍",
            text="fixture",
            query="fixture",
        )
        if template.endswith("thread"):
            d.app.state.broker.remember_message_context(
                BrokerMessage(
                    platform="telegram",
                    chat_id="1",
                    sender_name="fixture",
                    sender_id="1",
                    content="fixture inbound",
                    agent_name="tenant",
                    message_id="101",
                )
            )
        if template.endswith("broadcast"):
            d.agents.approve_user("tenant", "1", display_name="fixture")
        helper = (
            "_broker_react"
            if template.endswith("react")
            else "_broker_send_voice_message"
            if template.endswith("send-voice")
            else "_broker_send"
        )
        endpoint = next(r.endpoint for r in d.app.routes if getattr(r, "path", "") == template)
        if template.endswith("send-gif"):

            def send_animation(chat, path, **kwargs):
                from pathlib import Path

                effects.append((chat, Path(path).read_bytes()))
                return SimpleNamespace(message_id="201")

            adapter = SimpleNamespace(send_animation=send_animation)
            replace_cell(monkeypatch, endpoint, "_get_tg_adapter", lambda *a: adapter)

            def remote(url, **kwargs):
                if url.startswith("https://api.giphy.com/"):
                    return io.BytesIO(
                        json.dumps(
                            {
                                "data": [
                                    {
                                        "images": {
                                            "original": {"url": "https://fixture.test/example.gif"}
                                        }
                                    }
                                ]
                            }
                        ).encode()
                    )
                assert url == "https://fixture.test/example.gif"
                return io.BytesIO(b"GIF89a fixture")

            monkeypatch.setattr(urllib.request, "urlopen", remote)

            def check(result):
                return effects == [("1", b"GIF89a fixture")]
        else:
            replace_cell(monkeypatch, endpoint, helper, deliver)

            def check(result):
                return len(effects) == 1 and effects[0][0][0] == "tenant"
    elif template.endswith("/schedules"):
        body = dict(cron="0 1 * * *", name="allow-positive", prompt="fixture")

        def check(result):
            return any(
                s.id == result["id"] and s.prompt == "fixture"
                for s in d.agents.get_schedules("tenant")
            )
    elif "{schedule_id}" in template:
        row = d.agents.add_schedule("tenant", "0 1 * * *", prompt="old")
        values["schedule_id"] = row.id
        body = {"prompt": "updated"}

        def check(result):
            return d.agents.get_schedules("tenant")[0].prompt == "updated"
    elif "{pending_id}" in template:
        schedule = d.agents.add_schedule("tenant", "0 1 * * *", prompt="fixture")
        row, _ = d.agents.persist_schedule_wake(
            schedule.id,
            agent_name="tenant",
            schedule_name="fixture",
            prompt="fixture",
            fired_at=time.time(),
        )
        values["pending_id"] = row.id

        def check(result):
            return result["discarded"] and not d.agents.list_pending_schedule_wakes("tenant")
    elif template.endswith("/context"):
        body = {"task": "fixture task", "wake_action": "fixture next"}

        def check(result):
            return d.agents.get_context("tenant").task == "fixture task"
    elif template.endswith("/heartbeat"):
        body = {"status": "alive", "notes": "fixture heartbeat"}

        def check(result):
            return d.agents.get_latest_heartbeat("tenant").notes == "fixture heartbeat"
    elif "{trigger_id}" in template:
        row = triggers._trigger_store.create(
            agent_name="tenant", name="fixture", trigger_type="webhook", prompt_template="fixture"
        )
        values["trigger_id"] = row.id
        monkeypatch.setattr(triggers, "_wake_callback", deliver)
        if method == "DELETE":

            def check(result):
                return triggers._trigger_store.get(row.id) is None
        else:

            def check(result):
                return (
                    len(effects) == 1
                    and effects[0][0][0] == "tenant"
                    and triggers._trigger_store.get(row.id).fire_count == 1
                )
    elif template == "/api/voice/request":
        body = dict(target_name="fixture", target_phone="+15555550123", goal="fixture call")
        monkeypatch.setattr(voice_routes, "_notify_owner_call_request", deliver)

        def check(result):
            return (
                len(effects) == 1
                and voice_routes._voice_store.get_call_request(
                    result["request_id"]
                ).requested_by_agent
                == "tenant"
            )
    elif template.endswith("/message"):
        body = dict(from_agent="tenant", message="fixture self message")

        async def inject(*args):
            effects.append(args)
            return True, True

        monkeypatch.setattr(d.app.state.broker, "inject_agent_message", inject)

        def check(result):
            return (
                effects == [("tenant", "tenant", "fixture self message")]
                and result["delivered"]
                and result["message_id"] > 0
            )
    elif template.endswith("/mesh/send"):
        body = dict(target="ferry://fixture/peer", body="fixture message")
        d.agents.set_mesh_outbound_allowlist("tenant", ["ferry://fixture/peer"])

        def send(envelope):
            effects.append(envelope)
            return SimpleNamespace(
                sent=True,
                error="",
                correlation_id=envelope.correlation_id,
                subject="fixture",
                ts=envelope.ts,
            )

        replace_cell(
            monkeypatch,
            closure(d.app, "mesh_send"),
            "_build_mesh_sender",
            lambda: SimpleNamespace(send=send),
        )

        def check(result):
            return len(effects) == 1 and effects[0].from_.endswith("/tenant")
    elif template.endswith("/status"):
        body = {"status": "working"}

        def check(result):
            return d.agents.get("tenant").working_status == "working"
    elif template.endswith("/effort-drift"):
        body = {"expected": "high", "actual": "low", "session_id": "fixture"}

        def check(result):
            return bool(d.agents.get_effort_drift_events("tenant"))
    elif template.endswith("/policy/evaluate"):
        d.agents.update("tenant", tool_policy_enabled=True)
        body = {
            "tool_name": "Read",
            "tool_input": {"file_path": str(d.root / "tenant" / "own.txt")},
            "session_id": "tenant-main",
            "tool_use_id": "fixture",
        }

        def check(result):
            return bool(d.app.state.tool_policy_store.list_decisions("tenant", 0, 10))
    else:
        session = SimpleNamespace(
            state=SessionState.CONNECTED,
            resume_handle="fixture-session",
            last_active=time.time(),
            _stats={"turns": 0},
        )
        session.set_effort = lambda level: effects.append(("effort", level))
        session.notify_tail = lambda: effects.append(("tail",))
        session.record_tool_use_start = deliver
        session.record_tool_use_finish = deliver
        session.handle_stop_failure = deliver
        d.app.state.broker.register_streaming("tenant", session, label="main")
        if template.endswith("/effort"):
            body = {"effort": "high"}

            def check(result):
                return effects == [("effort", "high")]
        elif template.endswith("/restart"):
            d.agents.set_context(
                "tenant",
                task="fixture",
                metadata={"source": "save_my_context"},
                updated_by=session.resume_handle,
            )

            def restart(*args):
                effects.append(args[0])

                async def done():
                    pass

                return done()

            replace_cell(
                monkeypatch,
                closure(d.app, "restart_streaming_session"),
                "_restart_streaming_session_after_response",
                restart,
            )

            def check(result):
                return result["restart_scheduled"] and effects == ["tenant"]
        elif template.endswith("/wake"):
            body = {"event": "manual", "label": "main"}

            def check(result):
                return effects == [("tail",)]
        elif template.endswith("/stop-failure"):
            body = {"error_type": "api_error", "message": "fixture", "session_id": "fixture"}

            def check(result):
                return len(effects) == 1 and result["turn_resolved"]
        else:
            body = {"tool_use_id": "fixture", "tool_name": "Read", "label": "main"}

            def check(result):
                return len(effects) == 1 and effects[0][1]["tool_use_id"] == "fixture"

    return SimpleNamespace(path=template.format(**values), body=body, check=check, effects=effects)


@pytest.mark.parametrize("method,template", ALLOW_PAIRS, ids=[f"{m} {p}" for m, p in ALLOW_PAIRS])
def test_each_allow_pair_has_real_positive(daemon, monkeypatch, method, template):
    monkeypatch.setenv("PINKY_TOOL_POLICY", "log")
    d = daemon()
    case = prepared(d, monkeypatch, method, template)
    client = TestClient(d.app)
    try:
        response = client.request(
            method, case.path, json=case.body, headers=signed(d, method, case.path)
        )
    finally:
        client.close()
    assert response.status_code == 200, (template, response.status_code, response.text)
    assert case.check(response.json()), (template, response.text, case.effects)


@pytest.mark.parametrize("method,template", ALLOW_PAIRS, ids=[f"{m} {p}" for m, p in ALLOW_PAIRS])
def test_allow_pair_does_not_grant_peer_scope(daemon, monkeypatch, method, template):
    d = daemon()
    case = prepared(d, monkeypatch, method, template)
    path = case.path.replace("/agents/tenant/", "/agents/peer/")
    body = dict(case.body)
    if path.startswith("/broker/"):
        body["agent_name"] = "peer"
    if path == "/api/voice/request":
        body["requested_by_agent"] = "peer"
    client = TestClient(d.app)
    try:
        response = client.request(method, path, json=body, headers=signed(d, method, path))
    finally:
        client.close()
    assert (response.status_code, case.effects) == (403, []), (
        template,
        response.text,
        case.effects,
    )
