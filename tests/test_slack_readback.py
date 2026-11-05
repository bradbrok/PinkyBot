"""Signed Slack reads distinguish absence from failed or incomplete reads."""

from __future__ import annotations

import asyncio
import hashlib
import sys
import threading
import time
from collections import deque
from types import SimpleNamespace
from urllib.parse import parse_qs, quote, urlencode

import httpx
import pytest
from fastapi.testclient import TestClient

from pinky_daemon.api import create_api
from pinky_daemon.auth import (
    SESSION_COOKIE_NAME,
    build_internal_auth_headers,
    create_session_cookie,
)
from pinky_outreach import slack as slack_module
from pinky_outreach.slack import SlackAdapter, SlackError

pytestmark = pytest.mark.real_auth
TOKEN = "xoxb-readback-fixture-secret"
SECRET = "readback-signature-fixture-secret"
CHANNEL = "C12345"
TS = "1730312345.123456"
PARENT = "1730312300.000001"
LATEST = "1730312400.000000"
TEXT = "Private café 🐈\nmessage body"
BOT = {"user_id": "U123", "bot_id": "B123", "team_id": "T123"}
MESSAGE_PATH = f"/agents/sample/slack/message/{CHANNEL}/{TS}"
HISTORY_PATH = f"/agents/sample/slack/history/{CHANNEL}"


def _message(**extra):
    return {"ts": TS, "text": TEXT, "user": "U123", **extra}


def _page(messages=None, *, more=False, cursor=""):
    return {"ok": True, "messages": [] if messages is None else messages,
            "has_more": more, "response_metadata": {"next_cursor": cursor}}


@pytest.fixture
def gateway(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PINKY_SESSION_SECRET", SECRET)
    app = create_api(db_path=str(tmp_path / "memory.db"), default_working_dir=str(tmp_path))
    for name in ("sample", "other"):
        app.state.agents.register(name, working_dir=str(tmp_path / name))
    state = SimpleNamespace(app=app, calls=[], outcomes=deque(), auth=deque(),
                            before=None, selected=[], token=TOKEN)

    def respond(request):
        method = request.url.path.rsplit("/", 1)[-1]
        state.calls.append((method, request, threading.get_ident()))
        if state.before:
            state.before(method)
        if method == "auth.test":
            result = state.auth.popleft() if state.auth else {"ok": True, **BOT, "team": TEXT}
        else:
            assert method in ("conversations.history", "conversations.replies")
            result = state.outcomes.popleft() if state.outcomes else _page()
        if callable(result):
            result = result(request)
        if isinstance(result, Exception):
            raise result
        return result if isinstance(result, httpx.Response) else httpx.Response(200, json=result)

    adapter = SlackAdapter(TOKEN)
    adapter._client.close()
    adapter._client = httpx.Client(base_url=adapter.BASE_URL,
                                  headers={"Authorization": "Bearer " + TOKEN,
                                           "Content-Type": "application/json; charset=utf-8"},
                                  transport=httpx.MockTransport(respond))
    state.adapter = adapter

    def select(token):
        state.selected.append(token)
        return adapter

    monkeypatch.setattr(slack_module, "SlackAdapter", select)
    monkeypatch.setattr(app.state.agents, "get_raw_token", lambda *args: state.token)
    monkeypatch.setattr(app.state.agents, "get_raw_token_for_account",
                        lambda name, platform, account: TOKEN if account == "secondary" else "")
    state.client = TestClient(app)
    try:
        yield state
    finally:
        state.client.close()
        adapter.close()
        app.state.store_catalog.close()


def _headers(state, path, caller="sample"):
    return build_internal_auth_headers(state.app.state.agents.get_signing_key(caller),
                                       agent_name=caller, method="GET", path=path)


def _get(state, path=MESSAGE_PATH, *, caller="sample", **params):
    if params:
        path += "?" + urlencode(params)
    return state.client.get(path, headers=_headers(state, path, caller))


def _history(state, **params):
    return _get(state, HISTORY_PATH, **{"oldest": PARENT, "latest": LATEST, **params})


def _timing(monkeypatch):
    clock = SimpleNamespace(now=100.0, waits=[], threads=[])

    def sleep(seconds):
        clock.waits.append(seconds)
        clock.threads.append(threading.get_ident())
        clock.now += seconds

    module = sys.modules.get("pinky_daemon.slack_readback")
    if module is not None:
        monkeypatch.setattr(module, "monotonic", lambda: clock.now)
        monkeypatch.setattr(module, "sleep", sleep)
    return clock


def _forms(state):
    forms = []
    for method, request, _ in state.calls:
        assert request.headers["content-type"] == "application/x-www-form-urlencoded"
        forms.append((method, {k: v[0] for k, v in parse_qs(request.content.decode()).items()}))
    return forms


@pytest.mark.parametrize("identity,ours", [
    ({"user": "U123", "bot_id": "B999"}, True),
    ({"user": "U999", "bot_id": "B123"}, True),
    ({"user": "U999", "bot_id": "B999"}, False),
    ({"user": None, "bot_id": None}, False),
])
def test_message_metadata_and_exact_text_digest(gateway, caplog, identity, ours):
    gateway.outcomes.append(_page([_message(**identity, thread_ts=PARENT, app_id="A123",
                                          subtype="bot_message", reply_count=2, files=[{}, {}],
                                          edited={"ts": LATEST, "user": "U999", "text": TEXT})]))
    response = _get(gateway)
    assert response.status_code == 200
    assert response.json() == {
        "found": True, "channel": CHANNEL, "ts": TS, "bot": BOT,
        "message": {"ts": TS, "thread_ts": PARENT, **identity, "app_id": "A123",
                    "subtype": "bot_message", "edited": {"ts": LATEST, "user": "U999"},
                    "reply_count": 2, "files_count": 2,
                    "text_sha256": hashlib.sha256(TEXT.encode()).hexdigest(),
                    "text_length": len(TEXT), "ours": ours},
    }
    assert TEXT not in response.text and TOKEN not in response.text
    assert TEXT not in caplog.text and TOKEN not in caplog.text
    assert _forms(gateway) == [
        ("auth.test", {}),
        ("conversations.history", {"channel": CHANNEL, "oldest": TS, "latest": TS,
                                   "inclusive": "true", "limit": "1"}),
    ]


@pytest.mark.parametrize("thread_ts,method", [(PARENT, "conversations.replies"),
                                             (TS, "conversations.history")])
def test_thread_lookup_matches_exact_timestamp_and_keeps_parent_out(gateway, thread_ts, method):
    gateway.outcomes.append(_page([_message(ts=PARENT), _message(thread_ts=PARENT)]))
    response = _get(gateway, thread_ts=thread_ts)
    assert response.status_code == 200
    assert response.json()["message"]["ts"] == TS
    form = {"channel": CHANNEL, "oldest": TS, "latest": TS, "inclusive": "true", "limit": "1"}
    if method == "conversations.replies":
        form["ts"] = PARENT
        form["limit"] = "2"
    assert _forms(gateway)[1] == (method, form)


def test_thread_lookup_finds_reply_when_parent_counts_toward_limit(gateway):
    def parent_first(request):
        assert request.url.path.endswith("/conversations.replies")
        limit = int(parse_qs(request.content.decode())["limit"][0])
        return _page([_message(ts=PARENT), _message(thread_ts=PARENT)][:limit])

    gateway.outcomes.append(parent_first)
    response = _get(gateway, thread_ts=PARENT)
    assert response.status_code == 200
    assert response.json()["found"] is True
    assert response.json()["message"]["ts"] == TS
    assert response.json()["message"]["thread_ts"] == PARENT
    assert [method for method, _, _ in gateway.calls] == ["auth.test", "conversations.replies"]


@pytest.mark.parametrize("messages", [[], [_message(ts=PARENT)]])
def test_only_successful_exact_absence_is_not_found(gateway, messages):
    gateway.outcomes.append(_page(messages))
    response = _get(gateway, thread_ts=PARENT)
    assert response.status_code == 200
    assert response.json() == {"found": False, "channel": CHANNEL, "ts": TS,
                               "message": None, "bot": BOT}


@pytest.mark.parametrize("history", [False, True])
def test_other_signed_and_owner_session_callers_cannot_read(gateway, history):
    path = HISTORY_PATH + "?" + urlencode({"oldest": PARENT, "latest": LATEST}) if history else MESSAGE_PATH
    response = _get(gateway, path, caller="other")
    assert response.status_code == 403
    assert not gateway.calls
    gateway.client.cookies.set(SESSION_COOKIE_NAME, create_session_cookie(SECRET))
    response = gateway.client.get(path)
    assert response.status_code == 403
    assert response.json()["detail"] == "verified caller must match the target agent"
    assert not gateway.calls


@pytest.mark.parametrize("path,params", [
    (MESSAGE_PATH.replace(CHANNEL, "U123"), {}),
    (MESSAGE_PATH.replace(TS, "1730312345.12345"), {}),
    (MESSAGE_PATH.replace(TS, "1730312345.123456%0A"), {}),
    (MESSAGE_PATH, {"thread_ts": "broken"}),
    (MESSAGE_PATH, {"thread_ts": ""}),
    (HISTORY_PATH, {}),
    (HISTORY_PATH, {"oldest": LATEST, "latest": PARENT}),
    (HISTORY_PATH, {"oldest": PARENT, "latest": "bad"}),
    *[(HISTORY_PATH, {"oldest": PARENT, "latest": LATEST, "max_pages": v})
      for v in ("0", "51", "1.0", "abc")],
])
def test_bad_inputs_are_400_before_any_slack_call(gateway, path, params):
    response = _get(gateway, path, **params)
    assert response.status_code == 400
    assert not gateway.calls and not gateway.selected


@pytest.mark.parametrize("ts", ["١٧٣٠٣١٢٣٤٥.123456", "1730312345.١٢٣٤٥٦"])
def test_non_ascii_timestamp_is_400_before_any_slack_call(gateway, ts):
    response = _get(gateway, MESSAGE_PATH.replace(TS, quote(ts, safe="")))
    assert response.status_code == 400
    assert response.json() == {"detail": {"code": "invalid_timestamp"}}
    assert not gateway.calls and not gateway.selected


@pytest.mark.parametrize("history", [False, True])
def test_missing_token_returns_conflict(gateway, history):
    gateway.token = ""
    response = _history(gateway) if history else _get(gateway)
    assert response.status_code == 409
    assert response.json() == {"detail": {"code": "slack_not_configured"}}
    assert not gateway.calls


def test_account_selection_and_cached_identity(gateway):
    gateway.token = ""
    for _ in range(2):
        assert _get(gateway, account_id="secondary").status_code == 200
    assert gateway.selected == [TOKEN]
    assert [method for method, _, _ in gateway.calls].count("auth.test") == 1
    assert _get(gateway, account_id="missing").status_code == 409


@pytest.mark.parametrize("history", [False, True])
@pytest.mark.parametrize("error,expected", [
    ("not_in_channel", "not_in_channel"), ("missing_scope", "missing_scope"),
    ("invalid_auth", "invalid_auth"), (TOKEN, "invalid_response"),
    (TEXT, "invalid_response"), ("a" * 65, "invalid_response"),
])
def test_slack_errors_never_look_like_absence_or_leak(gateway, caplog, history, error, expected):
    gateway.outcomes.append({"ok": False, "error": error})
    response = _history(gateway) if history else _get(gateway)
    assert response.status_code == 502
    assert response.json() == {"detail": {"code": "slack_error", "error": expected}}
    assert TOKEN not in response.text + caplog.text and TEXT not in response.text + caplog.text


@pytest.mark.parametrize("history", [False, True])
@pytest.mark.parametrize("failure", [httpx.ReadTimeout(TOKEN + TEXT), httpx.ConnectError(TOKEN + TEXT)])
def test_transport_failure_is_504_without_exception_text(gateway, caplog, history, failure):
    gateway.outcomes.append(failure)
    response = _history(gateway) if history else _get(gateway)
    assert response.status_code == 504
    assert response.json() == {"detail": {"code": "slack_unreachable"}}
    assert TOKEN not in response.text + caplog.text and TEXT not in response.text + caplog.text


@pytest.mark.parametrize("body", [
    httpx.Response(200, content=TOKEN), {}, [], {"ok": "true", "messages": []},
    {"ok": True}, {"ok": True, "messages": {}},
    {"ok": True, "messages": ["bad"]}, {"ok": True, "messages": [{"ts": TS, "text": None}]},
    {"ok": True, "messages": [_message(ts="١٧٣٠٣١٢٣٤٥.١٢٣٤٥٦")]},
])
def test_malformed_upstream_is_an_explicit_failure(gateway, caplog, body):
    gateway.outcomes.append(body)
    response = _get(gateway)
    assert response.status_code == 502
    assert response.json() == {"detail": {"code": "slack_error", "error": "invalid_response"}}
    assert TOKEN not in response.text + caplog.text


def test_history_pages_with_exact_form_arguments(gateway):
    timestamps = [TS, PARENT, "1730312320.000001"]
    for index, ts in enumerate(timestamps):
        gateway.outcomes.append(_page([_message(ts=ts)], more=index < 2, cursor=f"page{index + 1}"))
    response = _history(gateway)
    assert response.status_code == 200
    body = response.json()
    assert {k: v for k, v in body.items() if k != "messages"} == {
        "complete": True, "stopped_reason": None, "pages": 3, "channel": CHANNEL,
        "oldest": PARENT, "latest": LATEST, "threads_included": False, "bot": BOT,
    }
    assert [message["ts"] for message in body["messages"]] == timestamps
    assert TEXT not in response.text and TOKEN not in response.text
    forms = _forms(gateway)
    for index, (method, form) in enumerate(forms[1:]):
        expected = {"channel": CHANNEL, "oldest": PARENT, "latest": LATEST,
                    "inclusive": "true", "limit": "200"}
        if index:
            expected["cursor"] = f"page{index}"
        assert (method, form) == ("conversations.history", expected)


def test_history_page_cap_preserves_successful_pages(gateway):
    gateway.outcomes.extend([_page([_message()], more=True, cursor="one"),
                             _page([_message(ts=PARENT)], more=True, cursor="two")])
    response = _history(gateway, max_pages=2)
    assert response.status_code == 200
    body = response.json()
    assert (body["complete"], body["stopped_reason"], body["pages"]) == (False, "page_cap", 2)
    assert len(body["messages"]) == 2
    assert len(gateway.calls) == 3


@pytest.mark.parametrize("cursors", [[""], ["one", "one"], ["one", "two", "one"]])
def test_invalid_cursor_preserves_partial_history(gateway, cursors):
    gateway.outcomes.extend(_page([_message()], more=True, cursor=c) for c in cursors)
    response = _history(gateway)
    assert response.status_code == 200
    body = response.json()
    assert (body["complete"], body["stopped_reason"], body["pages"]) == (False, "invalid_cursor", len(cursors))
    assert len(body["messages"]) == len(cursors)


def test_empty_history_is_complete(gateway):
    response = _history(gateway)
    assert response.status_code == 200
    body = response.json()
    assert body["complete"] is True and body["messages"] == [] and body["pages"] == 1


@pytest.mark.parametrize("header,wait", [("7", 7), ("100", 30), ("bad", 1), ("-5", 1), (None, 1)])
def test_retry_after_is_bounded_and_retries_the_same_page(gateway, monkeypatch, header, wait):
    clock = _timing(monkeypatch)
    gateway.outcomes.extend([httpx.Response(429, headers={} if header is None else {"Retry-After": header},
                                             content=TOKEN), _page([_message()])])
    response = _history(gateway)
    assert response.status_code == 200 and response.json()["complete"] is True
    assert response.json()["pages"] == 1
    assert clock.waits == [wait]
    assert _forms(gateway)[1] == _forms(gateway)[2]


@pytest.mark.parametrize("kind", ["history", "message", "auth"])
def test_four_rate_limits_stop_after_three_waits(gateway, monkeypatch, kind):
    clock = _timing(monkeypatch)
    responses = gateway.auth if kind == "auth" else gateway.outcomes
    if kind == "history":
        responses.append(_page([_message()], more=True, cursor="one"))
    responses.extend(httpx.Response(429, headers={"Retry-After": "2"}, content=TOKEN) for _ in range(4))
    response = _history(gateway) if kind in ("history", "auth") else _get(gateway)
    if kind == "history":
        assert response.status_code == 200
        body = response.json()
        assert (body["complete"], body["stopped_reason"], body["pages"]) == (False, "rate_limited", 1)
        assert len(body["messages"]) == 1
    else:
        assert response.status_code == 502
        assert response.json() == {"detail": {"code": "slack_error", "error": "ratelimited", "retry_after": 2}}
    assert clock.waits == [2, 2, 2]


def test_deadline_includes_prior_calls_and_waits(gateway, monkeypatch):
    clock = _timing(monkeypatch)

    def advance(method):
        if method == "conversations.history" and len(gateway.calls) == 2:
            clock.now += 58

    gateway.before = advance
    gateway.outcomes.extend([_page([_message()], more=True, cursor="one"),
                             httpx.Response(429, headers={"Retry-After": "3"})])
    response = _history(gateway)
    assert response.status_code == 200
    body = response.json()
    assert (body["complete"], body["stopped_reason"], body["pages"]) == (False, "deadline", 1)
    assert len(body["messages"]) == 1 and clock.waits == []
    assert len(gateway.calls) == 3


@pytest.mark.parametrize("method", ["auth.test", "conversations.history"])
async def test_slow_adapter_calls_do_not_block_the_event_loop(gateway, method):
    entered, release = threading.Event(), threading.Event()
    loop_thread = threading.get_ident()

    def slow(name):
        if name == method:
            entered.set()
            release.wait(2)

    gateway.before = slow
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gateway.app), base_url="http://test") as client:
        pending = asyncio.create_task(client.get(MESSAGE_PATH, headers=_headers(gateway, MESSAGE_PATH)))
        try:
            for _ in range(100):
                if entered.is_set() or pending.done():
                    break
                await asyncio.sleep(0.005)
            assert entered.is_set()
            assert not pending.done(), "the loop must run while the adapter is still waiting"
            assert all(thread != loop_thread for _, _, thread in gateway.calls)
        finally:
            release.set()
            response = await asyncio.wait_for(pending, 3)
        assert response.status_code == 200


def test_rate_limit_exception_preserves_slack_error_contract(gateway):
    gateway.auth.append(httpx.Response(429, headers={"Retry-After": "4"}, content=TOKEN))
    with pytest.raises(Exception) as captured:
        gateway.adapter.get_bot_info()
    assert isinstance(captured.value, SlackError)
    assert captured.value.error == "ratelimited"
    assert getattr(captured.value, "retry_after", None) == 4
    assert TOKEN not in str(captured.value)


@pytest.mark.parametrize("failure,status", [
    ({"ok": False, "error": "channel_not_found"}, 502),
    (httpx.ReadTimeout(TOKEN), 504), (httpx.Response(200, content=TOKEN), 502),
])
def test_failed_page_after_success_is_still_an_error(gateway, failure, status):
    gateway.outcomes.extend([_page([_message()], more=True, cursor="next"), failure])
    response = _history(gateway)
    assert response.status_code == status
    assert "detail" in response.json() and "complete" not in response.json()
    assert TOKEN not in response.text


@pytest.mark.parametrize("body", [{"ok": True, "messages": []},
                                   {"ok": True, "messages": [], "has_more": "false"}])
def test_missing_or_nonboolean_completion_is_not_complete(gateway, body):
    gateway.outcomes.append(body)
    response = _history(gateway)
    assert response.status_code == 502
    assert response.json() == {"detail": {"code": "slack_error", "error": "invalid_response"}}


@pytest.mark.parametrize("body,status", [
    ({"ok": True}, 502), ({"ok": False, "error": "invalid_auth"}, 502),
    (httpx.ConnectError(TOKEN), 504),
])
def test_identity_failure_never_returns_success(gateway, body, status):
    gateway.auth.append(body)
    response = _history(gateway)
    assert response.status_code == status
    assert TOKEN not in response.text
    assert [method for method, _, _ in gateway.calls] == ["auth.test"]


@pytest.mark.parametrize("history", [False, True])
async def test_total_deadline_bounds_an_inflight_call(gateway, monkeypatch, history):
    module = sys.modules["pinky_daemon.slack_readback"]
    monkeypatch.setattr(module, "REQUEST_SECONDS", 0.1)
    release = threading.Event()
    if history:
        gateway.outcomes.extend([_page([_message()], more=True, cursor="one"), _page()])

    def slow(method):
        if method == "conversations.history" and (not history or len(gateway.calls) == 3):
            release.wait(2)

    gateway.before = slow
    path = HISTORY_PATH + "?" + urlencode({"oldest": PARENT, "latest": LATEST}) if history else MESSAGE_PATH
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gateway.app), base_url="http://test") as client:
        start = time.monotonic()
        try:
            response = await asyncio.wait_for(client.get(path, headers=_headers(gateway, path)), 1)
            elapsed = time.monotonic() - start
        finally:
            release.set()
        assert elapsed < 0.75
    if history:
        assert response.status_code == 200
        body = response.json()
        assert (body["complete"], body["stopped_reason"], body["pages"]) == (False, "deadline", 1)
        assert len(body["messages"]) == 1
    else:
        assert response.status_code == 504
        assert response.json() == {"detail": {"code": "slack_unreachable"}}


async def test_retry_sleep_runs_off_the_event_loop(gateway, monkeypatch):
    module = sys.modules["pinky_daemon.slack_readback"]
    entered, release = threading.Event(), threading.Event()
    loop_thread, sleep_threads = threading.get_ident(), []

    def slow_sleep(seconds):
        assert seconds == 1
        sleep_threads.append(threading.get_ident())
        entered.set()
        release.wait(2)

    monkeypatch.setattr(module, "sleep", slow_sleep)
    gateway.outcomes.extend([httpx.Response(429, headers={"Retry-After": "1"}), _page()])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gateway.app), base_url="http://test") as client:
        pending = asyncio.create_task(client.get(MESSAGE_PATH, headers=_headers(gateway, MESSAGE_PATH)))
        try:
            for _ in range(100):
                if entered.is_set() or pending.done():
                    break
                await asyncio.sleep(0.005)
            assert entered.is_set() and not pending.done()
            assert sleep_threads and all(thread != loop_thread for thread in sleep_threads)
        finally:
            release.set()
            response = await asyncio.wait_for(pending, 3)
        assert response.status_code == 200


def test_token_test_caller_handles_rate_limit_without_leaking(gateway, tmp_path):
    from pinky_daemon.outreach_config import OutreachConfigStore

    gateway.auth.append(httpx.Response(429, content=TOKEN))
    store = OutreachConfigStore(str(tmp_path / "outreach.db"))
    try:
        result = store._test_slack(TOKEN)
    finally:
        store.close()
    assert result == {"platform": "slack", "success": False,
                      "error": "Slack API error: ratelimited"}
    assert TOKEN not in str(result)
