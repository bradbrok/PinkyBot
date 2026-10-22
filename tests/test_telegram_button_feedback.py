"""A button tap shows the choice to the tapper and is delivered to the agent once per card."""

import json
from contextlib import contextmanager
from types import SimpleNamespace

import httpx
import pytest

from pinky_outreach.telegram import TelegramAdapter

DATA = "ap:sc-0123456789abcdef"
OTHER = "rj:sc-0123456789abcdef"
KEYBOARD = {"inline_keyboard": [[{"text": "Approve", "callback_data": DATA},
                                 {"text": "Reject", "callback_data": OTHER}]]}


def _callback(callback_id="tap_1", data=DATA, markup=KEYBOARD, message_id=41):
    message = {"message_id": message_id, "date": 1700000000,
               "chat": {"id": 101, "type": "private", "title": "Conversation"},
               "from": {"id": 303, "is_bot": True}, "text": "Pick one"}
    if markup is not None:
        message["reply_markup"] = markup
    return {"id": callback_id,
            "from": {"id": 202, "first_name": "Button", "is_bot": False},
            "message": message, "data": data}


def _updates(*callbacks):
    return [{"update_id": index, "callback_query": callback}
            for index, callback in enumerate(callbacks, start=1)]


@contextmanager
def _adapter(updates, *, edit_ok=True):
    calls = []
    served = []

    def response(request):
        method = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content)
        calls.append((method, body))
        if method == "getUpdates":
            result = [] if served else list(updates)
            served.append(True)
        elif method == "answerCallbackQuery":
            result = True
        elif method == "editMessageReplyMarkup":
            if not edit_ok:
                return httpx.Response(200, json={"ok": False, "description": "synthetic failure"})
            result = {"message_id": body["message_id"], "date": 1700000000,
                      "chat": {"id": 101}, "reply_markup": body["reply_markup"]}
        else:
            raise AssertionError("unexpected adapter request " + method)
        return httpx.Response(200, json={"ok": True, "result": result})

    adapter = TelegramAdapter("fixture")
    adapter._client.close()
    adapter._client = httpx.Client(transport=httpx.MockTransport(response))
    try:
        yield SimpleNamespace(adapter=adapter, calls=calls)
    finally:
        adapter.close()


def _bodies(state, method):
    return [body for name, body in state.calls if name == method]


def _toast(body):
    return body.get("text", "")


def test_first_tap_shows_the_chosen_label_to_the_tapper():
    with _adapter(_updates(_callback())) as state:
        messages = state.adapter.get_updates()
    assert [message.content for message in messages] == ["Button tapped: " + DATA]
    answers = _bodies(state, "answerCallbackQuery")
    assert len(answers) == 1 and answers[0]["callback_query_id"] == "tap_1"
    assert _toast(answers[0]) == "Selected: Approve"


def test_first_tap_replaces_the_keyboard_with_the_choice():
    with _adapter(_updates(_callback())) as state:
        state.adapter.get_updates()
    edits = _bodies(state, "editMessageReplyMarkup")
    assert len(edits) == 1
    edit = edits[0]
    assert str(edit["chat_id"]) == "101" and edit["message_id"] == 41
    rows = edit["reply_markup"]["inline_keyboard"]
    assert len(rows) == 1 and len(rows[0]) == 1
    assert rows[0][0]["text"] == "Selected: Approve"
    assert rows[0][0]["callback_data"] not in (DATA, OTHER)
    assert not _bodies(state, "editMessageText")


@pytest.mark.parametrize("second", [DATA, OTHER])
def test_later_taps_on_the_same_card_are_not_delivered(second):
    first = _callback("tap_1", DATA)
    later = [_callback(f"tap_{n}", second) for n in range(2, 5)]
    with _adapter(_updates(first, *later)) as state:
        messages = state.adapter.get_updates()
    assert [message.message_id for message in messages] == ["cb-tap_1"]
    answers = _bodies(state, "answerCallbackQuery")
    assert [body["callback_query_id"] for body in answers] == ["tap_1", "tap_2", "tap_3", "tap_4"]
    assert _toast(answers[0]) == "Selected: Approve"
    assert [_toast(body) for body in answers[1:]] == ["Already chosen"] * 3
    assert len(_bodies(state, "editMessageReplyMarkup")) == 1
    assert state.adapter._last_update_id == 4


def test_taps_on_different_cards_are_each_delivered():
    taps = _updates(_callback("tap_1", DATA, message_id=41), _callback("tap_2", OTHER, message_id=42))
    with _adapter(taps) as state:
        messages = state.adapter.get_updates()
    assert [message.message_id for message in messages] == ["cb-tap_1", "cb-tap_2"]
    assert [_toast(body) for body in _bodies(state, "answerCallbackQuery")] == [
        "Selected: Approve", "Selected: Reject",
    ]


def test_a_chosen_card_stays_chosen_for_a_fresh_adapter():
    with _adapter(_updates(_callback())) as first:
        first.adapter.get_updates()
    chosen = _bodies(first, "editMessageReplyMarkup")[0]["reply_markup"]
    marker = chosen["inline_keyboard"][0][0]["callback_data"]
    for data in (marker, DATA):
        with _adapter(_updates(_callback("tap_9", data, markup=chosen))) as state:
            messages = state.adapter.get_updates()
        assert messages == []
        answers = _bodies(state, "answerCallbackQuery")
        assert [_toast(body) for body in answers] == ["Already chosen"]
        assert not _bodies(state, "editMessageReplyMarkup")


def test_a_failed_edit_still_delivers_the_tap_once():
    taps = _updates(_callback("tap_1"), _callback("tap_2"))
    with _adapter(taps, edit_ok=False) as state:
        messages = state.adapter.get_updates()
    assert [message.message_id for message in messages] == ["cb-tap_1"]
    assert [_toast(body) for body in _bodies(state, "answerCallbackQuery")] == [
        "Selected: Approve", "Already chosen",
    ]


def test_spent_budget_delivers_a_new_card_once_without_edit(monkeypatch, capsys):
    from tests.test_telegram_buttons import _slow_answers

    taps = _updates(
        _callback("spend_1", markup=None, message_id=101),
        _callback("spend_2", markup=None, message_id=102),
        _callback("new_1", message_id=700),
        _callback("new_2", message_id=700),
    )
    with _adapter(taps) as state:
        clock = _slow_answers(monkeypatch, state.adapter)
        messages = state.adapter.get_updates()
    assert [message.message_id for message in messages] == ["cb-spend_1", "cb-spend_2", "cb-new_1"]
    assert clock.attempted == ["spend_1", "spend_2"]
    assert not _bodies(state, "editMessageReplyMarkup")
    assert state.adapter._last_update_id == 4
    assert capsys.readouterr().err.splitlines() == [
        "ReadTimeout", "ReadTimeout", "callback answers skipped: 2",
    ]


@pytest.fixture
def _send_gateway(tmp_path, monkeypatch):
    from tests.test_inbound_message_fields import gateway, private_home
    from tests.test_telegram_buttons import outbound

    private_home.__wrapped__(tmp_path, monkeypatch)
    with contextmanager(gateway.__wrapped__)(tmp_path) as app:
        with contextmanager(outbound.__wrapped__)(app) as sent:
            yield app, sent


@pytest.mark.real_auth
@pytest.mark.parametrize("encoded", [False, True])
def test_send_refuses_the_reserved_chosen_marker(_send_gateway, encoded):
    from tests.test_telegram_buttons import _post

    gateway, outbound = _send_gateway
    buttons = [{"text": "Choose", "data": "__chosen__"}]
    response = _post(gateway, json.dumps(buttons) if encoded else buttons)
    assert response.status_code == 400
    assert "reserved" in response.json()["detail"]
    outbound.mock.assert_not_called()


@pytest.mark.parametrize("case", ["no_markup", "missing_data", "inaccessible"])
def test_unknown_or_inaccessible_label_selects_once_without_edit(case):
    markup = None if case == "no_markup" else KEYBOARD
    data = "not_in_keyboard" if case == "missing_data" else DATA
    callbacks = [_callback(f"tap_{index}", data, markup=markup) for index in (1, 2)]
    if case == "inaccessible":
        for callback in callbacks:
            callback["message"]["date"] = 0
    with _adapter(_updates(*callbacks)) as state:
        messages = state.adapter.get_updates()
    assert [message.message_id for message in messages] == ["cb-tap_1"]
    assert [_toast(body) for body in _bodies(state, "answerCallbackQuery")] == [
        "Selected", "Already chosen",
    ]
    assert not _bodies(state, "editMessageReplyMarkup")


def test_oldest_card_is_forgotten_at_the_bound_but_newest_stays_chosen(monkeypatch):
    from pinky_outreach import telegram

    monkeypatch.setattr(telegram, "CALLBACK_CHOSEN_CAP", 3, raising=False)
    cards = (1, 2, 3, 4, 4, 1)
    taps = [_callback(f"tap_{index}", markup=None, message_id=card)
            for index, card in enumerate(cards, start=1)]
    with _adapter(_updates(*taps)) as state:
        messages = state.adapter.get_updates()
    assert [message.message_id for message in messages] == [
        "cb-tap_1", "cb-tap_2", "cb-tap_3", "cb-tap_4", "cb-tap_6",
    ]
    assert [_toast(body) for body in _bodies(state, "answerCallbackQuery")] == [
        "Selected", "Selected", "Selected", "Selected", "Already chosen", "Selected",
    ]
    assert not _bodies(state, "editMessageReplyMarkup")


@pytest.mark.parametrize("chat_type", ["group", "supergroup"])
def test_group_card_keeps_one_choice_for_everyone(chat_type):
    first, repeat, second = _callback("tap_1"), _callback("tap_2", OTHER), _callback("tap_3", OTHER)
    for callback in (first, repeat, second):
        callback["message"]["chat"] = {"id": -101, "type": chat_type}
    second["from"] = {"id": 404, "first_name": "Other", "is_bot": False}
    with _adapter(_updates(first, repeat, second)) as state:
        messages = state.adapter.get_updates()
    assert [(message.message_id, message.metadata["user_id"], message.content)
            for message in messages] == [
        ("cb-tap_1", "202", "Button tapped: " + DATA),
    ]
    assert [_toast(body) for body in _bodies(state, "answerCallbackQuery")] == [
        "Selected: Approve", "Already chosen", "Already chosen",
    ]
    edits = _bodies(state, "editMessageReplyMarkup")
    assert len(edits) == 1
    assert edits[0]["reply_markup"]["inline_keyboard"][0][0]["text"] == "Selected: Approve"
    assert state.adapter._last_update_id == 3


@pytest.mark.parametrize("chat_type", ["private", "channel", "unknown", None])
def test_other_chat_types_keep_one_choice_across_users(chat_type):
    first, second = _callback("tap_1"), _callback("tap_2", OTHER)
    for callback in (first, second):
        callback["message"]["chat"] = {"id": 101}
        if chat_type is not None:
            callback["message"]["chat"]["type"] = chat_type
    second["from"] = {"id": 404, "first_name": "Other", "is_bot": False}
    with _adapter(_updates(first, second)) as state:
        messages = state.adapter.get_updates()
    assert [message.message_id for message in messages] == ["cb-tap_1"]
    assert [_toast(body) for body in _bodies(state, "answerCallbackQuery")] == [
        "Selected: Approve", "Already chosen",
    ]
    assert len(_bodies(state, "editMessageReplyMarkup")) == 1


@pytest.mark.parametrize("chat_type", ["group", "supergroup"])
def test_a_marker_on_a_group_card_is_chosen_for_everyone_even_on_a_stale_callback(chat_type):
    chosen = {"inline_keyboard": [[{"text": "Selected: Approve", "callback_data": "__chosen__"}]]}
    first, second = _callback("tap_1", markup=chosen), _callback("tap_2", OTHER)
    for callback in (first, second):
        callback["message"]["chat"] = {"id": -101, "type": chat_type}
    second["from"] = {"id": 404, "first_name": "Other", "is_bot": False}
    with _adapter(_updates(first, second)) as state:
        messages = state.adapter.get_updates()
    assert messages == []
    assert [_toast(body) for body in _bodies(state, "answerCallbackQuery")] == [
        "Already chosen", "Already chosen",
    ]
    assert not _bodies(state, "editMessageReplyMarkup")


def test_group_cards_share_the_same_bound(monkeypatch):
    from pinky_outreach import telegram

    monkeypatch.setattr(telegram, "CALLBACK_CHOSEN_CAP", 3, raising=False)
    taps = [_callback(f"tap_{index}", markup=None, message_id=card)
            for index, card in enumerate((1, 2, 3, 4, 4, 1), start=1)]
    for callback, user in zip(taps, (202, 202, 202, 202, 404, 606)):
        callback["message"]["chat"] = {"id": -101, "type": "group"}
        callback["from"]["id"] = user
    with _adapter(_updates(*taps)) as state:
        messages = state.adapter.get_updates()
    assert [message.message_id for message in messages] == [
        "cb-tap_1", "cb-tap_2", "cb-tap_3", "cb-tap_4", "cb-tap_6",
    ]
    assert not _bodies(state, "editMessageReplyMarkup")


def test_edit_time_spends_the_shared_answer_budget(monkeypatch, capsys):
    from pinky_outreach import telegram

    clock = SimpleNamespace(now=100.0)
    monkeypatch.setattr(telegram, "time", SimpleNamespace(monotonic=lambda: clock.now))
    taps = _updates(_callback("tap_1", message_id=1),
                    _callback("tap_2", message_id=2), _callback("tap_3", message_id=2))
    with _adapter(taps) as state:
        request = state.adapter._request

        def timed(method, **kwargs):
            result = request(method, **kwargs)
            clock.now += {"answerCallbackQuery": 0.5, "editMessageReplyMarkup": 9.5}.get(method, 0)
            return result

        monkeypatch.setattr(state.adapter, "_request", timed)
        messages = state.adapter.get_updates()
    assert [message.message_id for message in messages] == ["cb-tap_1", "cb-tap_2"]
    assert len(_bodies(state, "answerCallbackQuery")) == 1
    assert len(_bodies(state, "editMessageReplyMarkup")) == 1
    assert capsys.readouterr().err.splitlines() == ["callback answers skipped: 2"]


def test_an_answer_spending_the_last_budget_prevents_the_edit(monkeypatch, capsys):
    from pinky_outreach import telegram
    from tests.test_telegram_buttons import _slow_answers

    monkeypatch.setattr(telegram, "CALLBACK_ANSWER_BUDGET", 5.0)
    with _adapter(_updates(_callback("tap_1"), _callback("tap_2"))) as state:
        clock = _slow_answers(monkeypatch, state.adapter)
        messages = state.adapter.get_updates()
    assert [message.message_id for message in messages] == ["cb-tap_1"]
    assert clock.attempted == ["tap_1"]
    assert not _bodies(state, "editMessageReplyMarkup")
    assert capsys.readouterr().err.splitlines() == ["ReadTimeout", "callback answers skipped: 1"]


@pytest.mark.parametrize("kind", ["telegram", "exception"])
def test_edit_failure_logs_only_its_type_and_keeps_the_card_chosen(monkeypatch, capsys, kind):
    with _adapter(_updates(_callback("tap_1"), _callback("tap_2")),
                  edit_ok=kind != "telegram") as state:
        if kind == "exception":
            request = state.adapter._request

            def fail_edit(method, **kwargs):
                if method == "editMessageReplyMarkup":
                    raise RuntimeError("private response and token")
                return request(method, **kwargs)

            monkeypatch.setattr(state.adapter, "_request", fail_edit)
        messages = state.adapter.get_updates()
    assert [message.message_id for message in messages] == ["cb-tap_1"]
    assert [_toast(body) for body in _bodies(state, "answerCallbackQuery")] == [
        "Selected: Approve", "Already chosen",
    ]
    assert capsys.readouterr().err.splitlines() == [
        "TelegramError" if kind == "telegram" else "RuntimeError",
    ]


def test_the_same_message_number_in_different_chats_is_independent():
    first, second = _callback("tap_1"), _callback("tap_2")
    second["message"]["chat"]["id"] = 102
    with _adapter(_updates(first, second)) as state:
        messages = state.adapter.get_updates()
    assert [(message.chat_id, message.message_id) for message in messages] == [
        ("101", "cb-tap_1"), ("102", "cb-tap_2"),
    ]
    assert len(_bodies(state, "editMessageReplyMarkup")) == 2


def test_a_card_stays_chosen_across_polls_without_a_marker(monkeypatch):
    batches = iter([_updates(_callback("tap_1", markup=None)),
                    [{"update_id": 2, "callback_query": _callback("tap_2", markup=None)}], []])
    offsets = []
    with _adapter([]) as state:
        request = state.adapter._request

        def poll(method, **kwargs):
            if method == "getUpdates":
                offsets.append(kwargs.get("offset"))
                return next(batches)
            return request(method, **kwargs)

        monkeypatch.setattr(state.adapter, "_request", poll)
        assert [message.message_id for message in state.adapter.get_updates()] == ["cb-tap_1"]
        assert state.adapter.get_updates() == []
        assert state.adapter.get_updates() == []
    assert offsets == [None, 2, 3]
    assert [_toast(body) for body in _bodies(state, "answerCallbackQuery")] == [
        "Selected", "Already chosen",
    ]


def test_observing_a_marker_also_protects_a_later_stale_keyboard():
    chosen = {"inline_keyboard": [[{"text": "Selected: Approve", "callback_data": "__chosen__"}]]}
    with _adapter(_updates(_callback("tap_1", markup=chosen), _callback("tap_2"))) as state:
        messages = state.adapter.get_updates()
    assert messages == []
    assert [_toast(body) for body in _bodies(state, "answerCallbackQuery")] == [
        "Already chosen", "Already chosen",
    ]
    assert not _bodies(state, "editMessageReplyMarkup")


@pytest.mark.parametrize("chat_type", ["private", "group", "supergroup"])
def test_overlapping_polls_claim_a_card_once_without_holding_the_lock_during_http(
    monkeypatch, chat_type
):
    import threading

    before_record = threading.Event()
    second_attempt = threading.Event()
    local = threading.local()
    lock = threading.Lock()
    network_under_lock = []
    calls = []
    errors = []
    results = [None, None]

    class ObservedLock:
        def __enter__(self):
            if before_record.is_set():
                second_attempt.set()
            assert lock.acquire(timeout=3), "claim lock did not become available"
            local.held = True
            return self

        def __exit__(self, *exc):
            local.held = False
            lock.release()

    class ScheduledRecord(dict):
        def __setitem__(self, key, value):
            if not before_record.is_set():
                before_record.set()
                assert second_attempt.wait(3), "replacement poll did not reach its claim"
            else:
                second_attempt.set()
            super().__setitem__(key, value)

    adapter = TelegramAdapter("fixture")
    monkeypatch.setattr(adapter, "_choice_lock", ObservedLock(), raising=False)
    monkeypatch.setattr(adapter, "_chosen_cards", ScheduledRecord(), raising=False)

    def request(method, **kwargs):
        if method == "getUpdates":
            callback = _callback(f"tap_{local.index + 1}")
            callback["message"]["chat"]["type"] = chat_type
            return [{"update_id": local.index + 1, "callback_query": callback}]
        if getattr(local, "held", False):
            network_under_lock.append(method)
        calls.append((method, kwargs))
        return True

    monkeypatch.setattr(adapter, "_request", request)

    def poll(index):
        local.index = index
        try:
            results[index] = adapter.get_updates()
        except Exception as error:
            errors.append(error)

    workers = [threading.Thread(target=poll, args=(index,), daemon=True) for index in (0, 1)]
    try:
        workers[0].start()
        assert before_record.wait(3), "first poll did not reach its claim"
        workers[1].start()
        for worker in workers:
            worker.join(timeout=4)
        assert not any(worker.is_alive() for worker in workers), "poll did not finish"
        assert not errors
        assert [message.message_id for batch in results for message in batch] == ["cb-tap_1"]
        answers = {body["callback_query_id"]: body.get("text")
                   for method, body in calls if method == "answerCallbackQuery"}
        assert answers == {"tap_1": "Selected: Approve", "tap_2": "Already chosen"}
        edits = [body for method, body in calls if method == "editMessageReplyMarkup"]
        assert len(edits) == 1
        assert network_under_lock == []
        assert adapter._last_update_id == 2
    finally:
        second_attempt.set()
        for worker in workers:
            if worker.ident is not None:
                worker.join(timeout=4)
        adapter.close()
