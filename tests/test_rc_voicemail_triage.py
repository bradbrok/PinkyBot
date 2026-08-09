import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "rc_voicemail_triage.py"

SPEC = importlib.util.spec_from_file_location("rc_voicemail_triage", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
triage = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(triage)


def test_summary_line_parses_name_digits_and_duration():
    caller = triage.parse_summary_line(
        "From: Main Line - Mary Smith (435) 555-1212 message Length: 00:44"
    )

    assert caller == {
        "callerid_name": "Mary Smith",
        "number": "4355551212",
        "duration_s": 44,
    }


def test_summary_line_keeps_unavailable_fields_explicitly_null():
    assert triage.parse_summary_line("RingCentral voicemail") == {
        "callerid_name": None,
        "number": None,
        "duration_s": None,
    }


def test_summary_line_accepts_hour_duration_and_rejects_invalid_seconds():
    assert (
        triage.parse_summary_line("From: Main Line - A (801) 555-1111 Length: 1:02:03")[
            "duration_s"
        ]
        == 3723
    )
    assert (
        triage.parse_summary_line("From: Main Line - A (801) 555-1111 Length: 00:99")["duration_s"]
        is None
    )


def test_extract_city_hint_requires_explicit_in_phrase():
    assert triage.extract_city_hint("Subway Cafe in Tooele") == "Tooele"
    assert triage.extract_city_hint("Mary Smith") is None


class FakeSearchDesk:
    mode = "direct"

    def __init__(self, responses):
        self.responses = responses
        self.queries = []

    def search_contacts(self, query):
        self.queries.append(query)
        return self.responses.get(query, [])


def test_contact_resolution_uses_digits_first_and_stops_on_hit():
    desk = FakeSearchDesk(
        {
            "8015550100": [
                {
                    "id": "contact-1",
                    "account": {"id": "site-1", "name": "Main Street"},
                }
            ]
        }
    )

    candidates = triage.resolve_site_candidates(
        desk,
        {"callerid_name": "Caller", "number": "8015550100", "duration_s": 12},
    )

    assert desk.queries == ["8015550100"]
    assert candidates == [
        {
            "contact_id": "contact-1",
            "account_id": "site-1",
            "account_name": "Main Street",
            "match_basis": "phone_exact",
            "verified": False,
        }
    ]


def test_contact_resolution_uses_formatted_phone_only_after_digits_miss():
    desk = FakeSearchDesk(
        {"(801) 555-0100": [{"id": "contact-2", "accountId": "site-2", "accountName": "Broadway"}]}
    )

    candidates = triage.resolve_site_candidates(
        desk,
        {"callerid_name": "Caller", "number": "8015550100", "duration_s": 12},
    )

    assert desk.queries == ["8015550100", "(801) 555-0100"]
    assert candidates[0]["contact_id"] == "contact-2"
    assert candidates[0]["match_basis"] == "phone_exact"


def test_empty_object_contact_search_is_a_valid_miss(monkeypatch):
    client = triage.DeskClient("token")
    seen = {}

    def fake_request(path, params):
        seen.update({"path": path, "params": params})
        return {}

    monkeypatch.setattr(client, "_request_json", fake_request)

    assert client.search_contacts("8015550100") == []
    assert seen == {
        "path": "search",
        "params": {
            "module": "contacts",
            "searchStr": "8015550100",
            "from": 0,
            "limit": 50,
        },
    }


class FakeHTTPResponse:
    def __init__(self, body):
        self.body = body
        self.offset = 0

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, size=-1):
        if size is None or size < 0:
            chunk = self.body[self.offset :]
            self.offset = len(self.body)
            return chunk
        chunk = self.body[self.offset : self.offset + size]
        self.offset += len(chunk)
        return chunk


def test_gateway_search_uses_gateway_base_prefix_q_and_path_auth_hook(monkeypatch):
    seen = {"requests": [], "headers": []}
    monkeypatch.setattr(triage.time, "time", lambda: 123)

    def opener(request, **kwargs):
        seen["requests"].append(request.full_url)
        seen["headers"].append({key.casefold(): value for key, value in request.header_items()})
        return FakeHTTPResponse(b"{}")

    client = triage.DeskClient(
        None,
        mode="gateway",
        gateway_hosts=("10.0.0.32", "10.0.0.209"),
        gateway_secret="shared-secret",
        gateway_agent="geordi",
        opener=opener,
    )

    assert client.search_contacts("8015550100") == []
    assert seen["requests"] == ["http://10.0.0.32:9100/desk/search?module=contacts&q=8015550100"]
    assert seen["headers"][0]["x-pinky-agent"] == "geordi"
    assert seen["headers"][0]["x-pinky-timestamp"] == "123"
    assert (
        seen["headers"][0]["x-pinky-signature"]
        == triage._sign("shared-secret", "geordi", "GET", "/desk/search", 123)["x-pinky-signature"]
    )


def test_gateway_phone_resolve_uses_crm_digits_and_maps_nullable_account(monkeypatch):
    seen = {"requests": [], "headers": []}
    monkeypatch.setattr(triage.time, "time", lambda: 123)

    def opener(request, **kwargs):
        seen["requests"].append(request.full_url)
        seen["headers"].append({key.casefold(): value for key, value in request.header_items()})
        return FakeHTTPResponse(
            json.dumps(
                {
                    "data": [
                        {
                            "id": 123,
                            "Account_Name": {"id": 456, "name": "Sourdough & Co - Monrovia"},
                        },
                        {"id": "contact-without-account", "Account_Name": None},
                    ]
                }
            ).encode()
        )

    client = triage.DeskClient(
        None,
        mode="gateway",
        gateway_hosts=("10.0.0.32",),
        gateway_secret="shared-secret",
        gateway_agent="geordi",
        opener=opener,
    )

    candidates = triage.resolve_site_candidates(
        client,
        {"callerid_name": "Ravjodh Heer", "number": "661-699-3557", "duration_s": 48},
    )

    assert seen["requests"] == ["http://10.0.0.32:9100/crm/Contacts/search?phone=6616993557"]
    assert (
        seen["headers"][0]["x-pinky-signature"]
        == triage._sign(
            "shared-secret",
            "geordi",
            "GET",
            "/crm/Contacts/search",
            123,
        )["x-pinky-signature"]
    )
    assert candidates == [
        {
            "contact_id": "123",
            "account_id": "456",
            "account_name": "Sourdough & Co - Monrovia",
            "match_basis": "phone_exact",
            "verified": False,
        },
        {
            "contact_id": "contact-without-account",
            "account_id": None,
            "account_name": None,
            "match_basis": "phone_exact",
            "verified": False,
        },
    ]


def test_gateway_phone_resolve_empty_objects_are_clean_zero_candidate_miss():
    seen = []

    def opener(request, **kwargs):
        seen.append(request.full_url)
        if "/Contacts/" in request.full_url:
            return FakeHTTPResponse(b"{}")
        return FakeHTTPResponse(b'{"data":[]}')

    client = triage.DeskClient(
        None,
        mode="gateway",
        gateway_hosts=("10.0.0.32",),
        gateway_secret="shared-secret",
        gateway_agent="geordi",
        opener=opener,
    )

    assert (
        triage.resolve_site_candidates(
            client,
            {"callerid_name": "Unknown", "number": "7075731100", "duration_s": 44},
        )
        == []
    )
    assert seen == [
        "http://10.0.0.32:9100/crm/Contacts/search?phone=7075731100",
        "http://10.0.0.32:9100/crm/Accounts/search?phone=7075731100",
    ]


def test_gateway_phone_resolve_rejects_nonempty_object_without_data():
    def opener(request, **kwargs):
        return FakeHTTPResponse(b'{"error":"gateway auth context missing"}')

    client = triage.DeskClient(
        None,
        mode="gateway",
        gateway_hosts=("10.0.0.32",),
        gateway_secret="shared-secret",
        gateway_agent="geordi",
        opener=opener,
    )

    with pytest.raises(triage.TriageError) as caught:
        triage.resolve_site_candidates(
            client,
            {"callerid_name": None, "number": "8015550100", "duration_s": 12},
        )

    assert caught.value.stage == "resolve"
    assert caught.value.exit_code == triage.EXIT_RESOLVE
    assert "no data field" in caught.value.message


def test_gateway_phone_resolve_falls_back_from_contacts_to_accounts():
    seen = []

    def opener(request, **kwargs):
        seen.append(request.full_url)
        if "/Contacts/" in request.full_url:
            return FakeHTTPResponse(b"{}")
        return FakeHTTPResponse(b'{"data":[{"id":"account-1","name":"Main Street"}]}')

    client = triage.DeskClient(
        None,
        mode="gateway",
        gateway_hosts=("10.0.0.32",),
        gateway_secret="shared-secret",
        gateway_agent="geordi",
        opener=opener,
    )

    candidates = triage.resolve_site_candidates(
        client,
        {"callerid_name": None, "number": "8015550100", "duration_s": 12},
    )

    assert [triage.urlparse(url).path for url in seen] == [
        "/crm/Contacts/search",
        "/crm/Accounts/search",
    ]
    assert candidates == [
        {
            "contact_id": None,
            "account_id": "account-1",
            "account_name": "Main Street",
            "match_basis": "account_phone",
            "verified": False,
        },
    ]


@pytest.mark.parametrize("bad_module", ["Contacts", "Accounts"])
@pytest.mark.parametrize(
    "bad_row",
    [pytest.param({}, id="missing"), pytest.param({"id": ""}, id="empty")],
)
def test_gateway_phone_resolve_rejects_idless_crm_rows(bad_module, bad_row):
    def opener(request, **kwargs):
        if bad_module == "Accounts" and "/Contacts/" in request.full_url:
            return FakeHTTPResponse(b"{}")
        return FakeHTTPResponse(json.dumps({"data": [bad_row]}).encode())

    client = triage.DeskClient(
        None,
        mode="gateway",
        gateway_hosts=("10.0.0.32",),
        gateway_secret="shared-secret",
        gateway_agent="geordi",
        opener=opener,
    )

    with pytest.raises(triage.TriageError) as caught:
        triage.resolve_site_candidates(
            client,
            {"callerid_name": None, "number": "8015550100", "duration_s": 12},
        )

    assert caught.value.stage == "resolve"
    assert caught.value.exit_code == triage.EXIT_RESOLVE
    assert f"CRM {bad_module} search result is missing its id" == caught.value.message


def test_gateway_request_tries_hosts_in_order_and_resigns_each_attempt(monkeypatch):
    seen = {"hosts": [], "timestamps": []}
    timestamps = iter((101, 102))
    monkeypatch.setattr(triage.time, "time", lambda: next(timestamps))

    def opener(request, **kwargs):
        host = triage.urlparse(request.full_url).hostname
        seen["hosts"].append(host)
        headers = {key.casefold(): value for key, value in request.header_items()}
        seen["timestamps"].append(headers["x-pinky-timestamp"])
        if host == "10.0.0.32":
            raise triage.URLError("first host unavailable")
        return FakeHTTPResponse(b"{}")

    client = triage.DeskClient(
        None,
        mode="gateway",
        gateway_hosts=("10.0.0.32", "10.0.0.209"),
        gateway_secret="shared-secret",
        gateway_agent="geordi",
        opener=opener,
    )

    assert client.search_contacts("8015550100") == []
    assert seen["hosts"] == ["10.0.0.32", "10.0.0.209"]
    assert seen["timestamps"] == ["101", "102"]


def test_gateway_thread_attachments_use_explicit_allowlisted_route(monkeypatch):
    seen = {"requests": []}
    monkeypatch.setattr(triage.time, "time", lambda: 123)

    def opener(request, **kwargs):
        seen["requests"].append(request.full_url)
        return FakeHTTPResponse(b'{"data":[{"id":"attachment-1","name":"voice.mp3"}]}')

    client = triage.DeskClient(
        None,
        mode="gateway",
        gateway_hosts=("10.0.0.32",),
        gateway_secret="shared-secret",
        gateway_agent="geordi",
        opener=opener,
    )

    assert client.get_thread("ticket/1", "thread 2") == {
        "id": "thread 2",
        "attachments": [{"id": "attachment-1", "name": "voice.mp3"}],
    }
    expected_path = "/desk/tickets/ticket%2F1/threads/thread%202/attachments"
    assert seen["requests"] == [f"http://10.0.0.32:9100{expected_path}"]


def test_gateway_display_ticket_number_resolves_to_internal_id(monkeypatch):
    seen = {"requests": [], "headers": []}
    monkeypatch.setattr(triage.time, "time", lambda: 123)

    def opener(request, **kwargs):
        seen["requests"].append(request.full_url)
        seen["headers"].append({key.casefold(): value for key, value in request.header_items()})
        return FakeHTTPResponse(b'{"id":"637734000000123456","ticketNumber":"88629"}')

    client = triage.DeskClient(
        None,
        mode="gateway",
        gateway_hosts=("10.0.0.32",),
        gateway_secret="shared-secret",
        gateway_agent="geordi",
        opener=opener,
    )

    assert client.resolve_ticket_id("88629") == "637734000000123456"
    assert seen["requests"] == ["http://10.0.0.32:9100/desk/tickets/resolve?ticket_number=88629"]
    assert (
        seen["headers"][0]["x-pinky-signature"]
        == triage._sign(
            "shared-secret",
            "geordi",
            "GET",
            "/desk/tickets/resolve",
            123,
        )["x-pinky-signature"]
    )


def test_gateway_internal_ticket_id_passes_through_without_resolve_request():
    def opener(*args, **kwargs):
        raise AssertionError("internal ticket id must not require a resolve hop")

    client = triage.DeskClient(
        None,
        mode="gateway",
        gateway_hosts=("10.0.0.32",),
        gateway_secret="shared-secret",
        gateway_agent="geordi",
        opener=opener,
    )

    assert client.resolve_ticket_id("637734000000123456") == "637734000000123456"


def test_triage_fetch_chain_receives_resolved_internal_ticket_id(tmp_path, monkeypatch):
    seen = {}

    class FakeDesk:
        def resolve_ticket_id(self, ticket_id):
            seen["supplied"] = ticket_id
            return "637734000000123456"

    def fake_fetch(desk, ticket_id, destination_dir):
        seen["fetched"] = ticket_id
        return destination_dir / "voice.mp3", {
            "callerid_name": None,
            "number": None,
            "duration_s": 1,
        }

    monkeypatch.setattr(triage, "fetch_voicemail_audio", fake_fetch)
    monkeypatch.setattr(triage, "transcribe_audio", lambda *args, **kwargs: ("", True))
    monkeypatch.setattr(triage, "resolve_site_candidates", lambda *args, **kwargs: [])
    monkeypatch.setattr(triage, "scan_in_flight", lambda *args, **kwargs: [])

    result = triage.triage_ticket(
        "88629",
        FakeDesk(),
        transcriber_path=tmp_path / "unused-transcriber.py",
        ledger_root=tmp_path,
        destination_dir=tmp_path / "downloads",
    )

    assert seen == {"supplied": "88629", "fetched": "637734000000123456"}
    assert result["ticket_id"] == "88629"


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param([], id="non-object"),
        pytest.param({"ticketNumber": "88629"}, id="missing-internal-id"),
        pytest.param(
            {"id": "637734000000123456", "ticketNumber": "88630"},
            id="ticket-number-mismatch",
        ),
    ],
)
def test_gateway_ticket_resolve_malformed_or_mismatched_response_fails_fetch(tmp_path, payload):
    client = triage.DeskClient(
        None,
        mode="gateway",
        gateway_hosts=("10.0.0.32",),
        gateway_secret="shared-secret",
        gateway_agent="geordi",
        opener=lambda *args, **kwargs: FakeHTTPResponse(json.dumps(payload).encode()),
    )

    with pytest.raises(triage.TriageError) as caught:
        triage.triage_ticket(
            "88629",
            client,
            transcriber_path=tmp_path / "unused-transcriber.py",
            ledger_root=tmp_path,
            destination_dir=tmp_path / "downloads",
        )

    assert caught.value.stage == "fetch"
    assert caught.value.exit_code == triage.EXIT_FETCH


def test_direct_search_keeps_v1_base_query_and_oauth_header():
    seen = {}

    def opener(request, **kwargs):
        seen["url"] = request.full_url
        seen["authorization"] = request.get_header("Authorization")
        return FakeHTTPResponse(b"{}")

    client = triage.DeskClient("direct-token", opener=opener)

    assert client.search_contacts("8015550100") == []
    assert seen == {
        "url": (
            "https://desk.zoho.com/api/v1/search?module=contacts&"
            "searchStr=8015550100&from=0&limit=50"
        ),
        "authorization": "Zoho-oauthtoken direct-token",
    }


def test_gateway_attachment_uses_ids_not_desk_href_and_writes_raw_bytes(tmp_path, monkeypatch):
    seen = {"requests": []}
    monkeypatch.setattr(triage.time, "time", lambda: 123)

    def opener(request, **kwargs):
        seen["requests"].append(request.full_url)
        return FakeHTTPResponse(b"raw-mp3-bytes")

    client = triage.DeskClient(
        None,
        mode="gateway",
        gateway_hosts=("10.0.0.32",),
        gateway_secret="shared-secret",
        gateway_agent="geordi",
        opener=opener,
    )
    destination = tmp_path / "voice.mp3"

    client.download_attachment(
        "/api/v1/desk-native-wrong-href",
        destination,
        ticket_id="ticket 1",
        thread_id="thread/2",
        attachment_id="attachment?3",
    )

    expected_path = "/desk/tickets/ticket%201/threads/thread%2F2/attachments/attachment%3F3/content"
    assert seen["requests"] == [f"http://10.0.0.32:9100{expected_path}"]
    assert destination.read_bytes() == b"raw-mp3-bytes"
    assert not destination.with_suffix(".mp3.part").exists()


def test_explicit_city_fallback_returns_list_without_auto_binding():
    desk = FakeSearchDesk(
        {
            "Tooele": [
                {"id": "c1", "city": "Tooele", "accountId": "a1", "accountName": "North"},
                {"id": "c2", "cf": {"service_city": "Tooele"}},
                {"id": "c3", "city": "Provo"},
            ]
        }
    )

    candidates = triage.resolve_site_candidates(
        desk,
        {
            "callerid_name": "Subway Cafe in Tooele",
            "number": "8015559999",
            "duration_s": 48,
        },
    )

    assert desk.queries == ["8015559999", "(801) 555-9999", "Tooele"]
    assert [candidate["contact_id"] for candidate in candidates] == ["c1", "c2"]
    assert all(candidate["match_basis"] == "city_only" for candidate in candidates)
    assert all(candidate["verified"] is False for candidate in candidates)


def test_transcript_city_hint_is_extracted_mid_sentence_and_unioned_with_cnam():
    desk = FakeSearchDesk(
        {
            "Bakersfield": [{"id": "c1", "city": "Bakersfield"}],
            "Tooele": [
                {"id": "c2", "city": "Tooele", "accountId": "a2"},
                {"id": "c3", "city": "Provo"},
            ],
        }
    )

    candidates = triage.resolve_site_candidates(
        desk,
        {"callerid_name": "Main Line in Bakersfield", "number": None, "duration_s": 48},
        "Hi, this is Subway Cafe in Tooele about my order. Please call me back.",
    )

    assert desk.queries == ["Bakersfield", "Tooele"]
    assert [(candidate["contact_id"], candidate["match_basis"]) for candidate in candidates] == [
        ("c1", "city_only"),
        ("c2", "city_only"),
    ]


@pytest.mark.parametrize(
    "city",
    ["St. George", "St. Louis", "St. Paul", "Mt. Pleasant", "Ft. Worth", "Washington D.C."],
)
def test_transcript_city_hints_preserve_dotted_place_names(city):
    transcript = f"This is our store in {city} about a register issue."

    assert triage._city_hints(None, transcript) == [city]


def test_dotted_initialism_city_survives_sentence_boundary():
    transcript = "This is our store in Washington D.C. Please call me back."

    assert triage._city_hints(None, transcript) == ["Washington D.C."]


def test_dotted_transcript_city_survives_exact_membership_filter():
    desk = FakeSearchDesk(
        {
            "St. George": [
                {"id": "c1", "city": "St. George"},
                {"id": "c2", "city": "St. George Island"},
            ]
        }
    )

    candidates = triage.resolve_site_candidates(
        desk,
        {"callerid_name": None, "number": None, "duration_s": 48},
        "This is our store in St. George about a register issue.",
    )

    assert desk.queries == ["St. George"]
    assert [(candidate["contact_id"], candidate["match_basis"]) for candidate in candidates] == [
        ("c1", "city_only")
    ]


def test_lowercase_transcript_location_does_not_surface_city_candidate():
    desk = FakeSearchDesk(
        {
            "orange": [
                {
                    "id": "orange-contact",
                    "city": "Orange",
                    "accountId": "orange-site",
                    "accountName": "Orange Cafe",
                }
            ]
        }
    )

    candidates = triage.resolve_site_candidates(
        desk,
        {"callerid_name": None, "number": None, "duration_s": 48},
        "The status indicator is showing in orange.",
    )

    assert desk.queries == []
    assert candidates == []


def test_gateway_88632_transcript_city_fallback_runs_after_both_crm_phone_misses():
    seen = []

    def opener(request, **kwargs):
        seen.append(triage.urlparse(request.full_url).path)
        if "/crm/" in request.full_url:
            return FakeHTTPResponse(b"{}")
        return FakeHTTPResponse(b'{"data":[{"id":"c1","city":"Tooele"}]}')

    client = triage.DeskClient(
        None,
        mode="gateway",
        gateway_hosts=("10.0.0.32",),
        gateway_secret="shared-secret",
        gateway_agent="geordi",
        opener=opener,
    )

    candidates = triage.resolve_site_candidates(
        client,
        {"callerid_name": "HANSEN,DAWSON", "number": "4355550100", "duration_s": 30},
        "Hi, this is Subway Cafe in Tooele about my order. Please call me back.",
    )

    assert seen == [
        "/crm/Contacts/search",
        "/crm/Accounts/search",
        "/desk/search",
    ]
    assert [(candidate["contact_id"], candidate["match_basis"]) for candidate in candidates] == [
        ("c1", "city_only")
    ]


def test_multiple_accounts_remain_multiple_unverified_candidates():
    contacts = [
        {
            "id": "c1",
            "accounts": [
                {"id": "a1", "name": "First"},
                {"id": "a2", "name": "Second"},
            ],
        }
    ]

    candidates = triage._site_candidates(contacts, match_basis="phone_exact")

    assert [candidate["account_id"] for candidate in candidates] == ["a1", "a2"]
    assert all(candidate["verified"] is False for candidate in candidates)


def test_machine_readable_no_speech_is_successful_and_explicit():
    assert triage.parse_transcriber_output('{"transcript":"", "no_speech":true}') == (
        "",
        True,
    )
    assert triage.parse_transcriber_output('{"text":""}') == ("", True)
    assert triage.parse_transcriber_output("[NO_SPEECH]") == ("", True)
    assert triage.parse_transcriber_output("No speech detected.") == ("", True)


def test_zero_exit_empty_transcript_is_valid_no_speech():
    assert triage.parse_transcriber_output("\n") == ("", True)


def test_transcriber_false_no_speech_without_text_is_failure():
    with pytest.raises(triage.TriageError):
        triage.parse_transcriber_output('{"transcript":"", "no_speech":false}')


@pytest.mark.parametrize("field", ["transcript", "text"])
def test_transcriber_rejects_text_with_no_speech_true(field):
    payload = json.dumps({field: "hello", "no_speech": True})

    with pytest.raises(triage.TriageError) as caught:
        triage.parse_transcriber_output(payload)

    assert caught.value.stage == "transcribe"
    assert caught.value.exit_code == triage.EXIT_TRANSCRIBE


@pytest.mark.parametrize("no_speech", [None, True])
def test_transcriber_rejects_empty_transcript_masking_nonempty_text(no_speech):
    payload = {"transcript": "", "text": "hello"}
    if no_speech is not None:
        payload["no_speech"] = no_speech

    with pytest.raises(triage.TriageError) as caught:
        triage.parse_transcriber_output(json.dumps(payload))

    assert caught.value.stage == "transcribe"
    assert caught.value.exit_code == triage.EXIT_TRANSCRIBE


def test_transcriber_rejects_disagreeing_nonempty_text_aliases():
    with pytest.raises(triage.TriageError) as caught:
        triage.parse_transcriber_output('{"transcript":"hello", "text":"goodbye"}')

    assert caught.value.stage == "transcribe"
    assert caught.value.exit_code == triage.EXIT_TRANSCRIBE


def test_transcriber_validates_every_present_text_alias():
    with pytest.raises(triage.TriageError) as caught:
        triage.parse_transcriber_output('{"transcript":"hello", "text":7}')

    assert caught.value.stage == "transcribe"
    assert caught.value.exit_code == triage.EXIT_TRANSCRIBE


def test_transcriber_accepts_matching_text_aliases():
    assert triage.parse_transcriber_output('{"transcript":"hello", "text":" hello "}') == (
        "hello",
        False,
    )


def test_transcriber_invocation_is_promptless_and_scrubs_prompt_env(tmp_path, monkeypatch):
    audio = tmp_path / "message.mp3"
    audio.write_bytes(b"audio")
    transcriber = tmp_path / "whisper_transcribe.py"
    transcriber.write_text("# helper")
    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        seen["prompt_keys"] = sorted(key for key in kwargs["env"] if "prompt" in key.casefold())
        return subprocess.CompletedProcess(argv, 0, '{"text":"hello", "no_speech":false}', "")

    monkeypatch.setenv("WHISPER_PROMPT", "hallucinate this")
    monkeypatch.setenv("TRANSCRIPTION_PROMPT", "also forbidden")
    monkeypatch.setattr(triage.subprocess, "run", fake_run)

    assert triage.transcribe_audio(audio, transcriber) == ("hello", False)
    assert seen["argv"] == [sys.executable, str(transcriber), str(audio)]
    assert "WHISPER_PROMPT" not in seen["prompt_keys"]
    assert "TRANSCRIPTION_PROMPT" not in seen["prompt_keys"]
    assert all("prompt" not in arg.casefold() for arg in seen["argv"])


def test_transcriber_crash_fails_loud(tmp_path, monkeypatch):
    audio = tmp_path / "message.mp3"
    audio.write_bytes(b"audio")
    transcriber = tmp_path / "whisper_transcribe.py"
    transcriber.write_text("# helper")
    monkeypatch.setattr(
        triage.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 9, "", "API failed"),
    )

    with pytest.raises(triage.TriageError) as caught:
        triage.transcribe_audio(audio, transcriber)

    assert caught.value.exit_code == triage.EXIT_TRANSCRIBE
    assert "exited 9" in caught.value.message


def test_fetch_chain_uses_only_notify_thread_mp3(tmp_path):
    class FakeDesk:
        def __init__(self):
            self.downloaded = None

        def list_threads(self, ticket_id):
            assert ticket_id == "ticket-1"
            return [
                {"id": "other", "fromEmailAddress": "person@example.com"},
                {"id": "vm", "fromEmailAddress": "notify@ringcentral.com"},
            ]

        def get_thread(self, ticket_id, thread_id):
            assert (ticket_id, thread_id) == ("ticket-1", "vm")
            return {
                "id": "vm",
                "summary": "From: Main Line - Mary (801) 555-0100 Length: 00:12",
                "attachments": [
                    {"id": "txt", "name": "note.txt", "href": "note"},
                    {"id": "mp3", "name": "voice.MP3", "href": "audio"},
                ],
            }

        def download_attachment(self, href, destination, **ids):
            self.downloaded = (href, destination)
            destination.write_bytes(b"audio")

    desk = FakeDesk()
    path, caller = triage.fetch_voicemail_audio(desk, "ticket-1", tmp_path)

    assert desk.downloaded == ("audio", path)
    assert path.read_bytes() == b"audio"
    assert caller["callerid_name"] == "Mary"
    assert caller["number"] == "8015550100"


@pytest.mark.parametrize(
    "sender",
    [
        pytest.param('"RingCentral"<notify@ringcentral.com>', id="live-wrapped"),
        pytest.param(
            '"Voicemail Notification" <notify@ringcentral.com>',
            id="different-display-name",
        ),
        pytest.param(
            '  "RingCentral" <notify@ringcentral.com>  ',
            id="surrounding-whitespace",
        ),
    ],
)
def test_fetch_chain_accepts_display_name_wrapped_notify_sender(tmp_path, sender):
    class FakeDesk:
        def __init__(self):
            self.downloaded = None

        def list_threads(self, ticket_id):
            return [{"id": "vm", "fromEmailAddress": sender}]

        def get_thread(self, ticket_id, thread_id):
            return {
                "id": "vm",
                "summary": "From: Main Line - Mary (801) 555-0100 Length: 00:12",
                "attachments": [{"id": "mp3", "name": "voice.mp3", "href": "audio"}],
            }

        def download_attachment(self, href, destination, **ids):
            self.downloaded = (href, destination)
            destination.write_bytes(b"audio")

    desk = FakeDesk()

    path, caller = triage.fetch_voicemail_audio(desk, "ticket-1", tmp_path)

    assert desk.downloaded == ("audio", path)
    assert path.read_bytes() == b"audio"
    assert caller["number"] == "8015550100"


def _gateway_fetch_client(listed_summary):
    listed_thread = {
        "id": "vm",
        "fromEmailAddress": "notify@ringcentral.com",
    }
    if listed_summary is not None:
        listed_thread["summary"] = listed_summary

    def opener(request, **kwargs):
        path = triage.urlparse(request.full_url).path
        if path.endswith("/threads"):
            return FakeHTTPResponse(json.dumps({"data": [listed_thread]}).encode())
        if path.endswith("/attachments"):
            return FakeHTTPResponse(b'{"data":[{"id":"mp3","name":"voice.mp3"}]}')
        if path.endswith("/content"):
            return FakeHTTPResponse(b"raw-mp3-bytes")
        raise AssertionError(f"unexpected gateway request: {path}")

    return triage.DeskClient(
        None,
        mode="gateway",
        gateway_hosts=("10.0.0.32",),
        gateway_secret="shared-secret",
        gateway_agent="geordi",
        opener=opener,
    )


def test_gateway_fetch_preserves_list_summary_when_detail_only_has_attachments(tmp_path):
    desk = _gateway_fetch_client("From: Main Line - Mary Smith (801) 555-0100 Length: 00:12")

    path, caller = triage.fetch_voicemail_audio(desk, "ticket-1", tmp_path)

    assert path.read_bytes() == b"raw-mp3-bytes"
    assert caller == {
        "callerid_name": "Mary Smith",
        "number": "8015550100",
        "duration_s": 12,
    }


@pytest.mark.parametrize(
    "listed_summary",
    [
        pytest.param(None, id="omitted"),
        pytest.param("", id="empty"),
        pytest.param("   ", id="whitespace-only"),
    ],
)
def test_gateway_fetch_missing_or_empty_list_summary_fails_fetch(tmp_path, listed_summary):
    desk = _gateway_fetch_client(listed_summary)

    with pytest.raises(triage.TriageError) as caught:
        triage.fetch_voicemail_audio(desk, "ticket-1", tmp_path)

    assert caught.value.stage == "fetch"
    assert caught.value.exit_code == triage.EXIT_FETCH
    assert "summary" in caught.value.message


def test_configured_destination_does_not_overwrite_prior_download(tmp_path):
    class FakeDesk:
        def list_threads(self, ticket_id):
            return [{"id": "vm", "fromEmailAddress": "notify@ringcentral.com"}]

        def get_thread(self, ticket_id, thread_id):
            return {
                "id": "vm",
                "summary": "From: Main Line - Mary (801) 555-0100 Length: 00:12",
                "attachments": [{"id": "mp3", "name": "voice.mp3", "href": "audio"}],
            }

        def download_attachment(self, href, destination, **ids):
            destination.write_bytes(b"new")

    original = tmp_path / "ticket-1_vm_mp3.mp3"
    original.write_bytes(b"keep")

    downloaded, _ = triage.fetch_voicemail_audio(FakeDesk(), "ticket-1", tmp_path)

    assert original.read_bytes() == b"keep"
    assert downloaded.name == "ticket-1_vm_mp3_2.mp3"
    assert downloaded.read_bytes() == b"new"


def test_missing_attachment_fails_nonzero_stage():
    class FakeDesk:
        def list_threads(self, ticket_id):
            return [{"id": "vm", "fromEmailAddress": "notify@ringcentral.com"}]

        def get_thread(self, ticket_id, thread_id):
            return {"id": "vm", "attachments": []}

    with pytest.raises(triage.TriageError) as caught:
        triage.fetch_voicemail_audio(FakeDesk(), "1", Path("unused"))

    assert caught.value.stage == "fetch"
    assert caught.value.exit_code == triage.EXIT_FETCH


def test_in_flight_scans_all_required_ledgers_and_maps_ubereats(tmp_path):
    for directory in ("rma", "cc_creds", "jamf", "ubereats"):
        (tmp_path / directory).mkdir()
    (tmp_path / "rma/active_rmas.json").write_text('{"caller":"Mary Smith"}\n')
    (tmp_path / "rma/label_requests.json").write_text("call (801) 555-0100\n")
    (tmp_path / "cc_creds/active_cc.json").write_text("site-1,chargeback\n")
    (tmp_path / "jamf/active_jamf.json").write_text("Main Street device\n")
    (tmp_path / "ubereats/integration_requests.json").write_text('{"contact":"contact-1"}\n')

    caller = {"callerid_name": "Mary Smith", "number": "8015550100", "duration_s": 12}
    candidates = [
        {
            "contact_id": "contact-1",
            "account_id": "site-1",
            "account_name": "Main Street",
            "match_basis": "phone_exact",
            "verified": False,
        }
    ]

    hits = triage.scan_in_flight(tmp_path, caller, candidates, env={})

    assert {hit["ledger"] for hit in hits} == {
        "active_rmas",
        "label_requests",
        "active_cc",
        "active_jamf",
        "integration_requests",
    }
    assert all(set(hit) == {"ledger", "key", "one_line"} for hit in hits)


def test_explicit_missing_ledger_path_fails_loud(tmp_path):
    env = {"RC_VOICEMAIL_LEDGER_ACTIVE_RMAS": str(tmp_path / "missing")}

    with pytest.raises(triage.TriageError) as caught:
        triage.scan_in_flight(
            tmp_path,
            {"callerid_name": "Mary", "number": None, "duration_s": 1},
            [],
            env=env,
        )

    assert caught.value.stage == "in_flight"


def _create_empty_required_ledgers(root, *, missing=None):
    for path in triage.LEDGER_DEFAULT_PATHS.values():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
    for canonical, path in triage.LEDGER_DEFAULT_PATHS.items():
        if canonical != missing:
            (root / path).write_text("")


def test_ledger_defaults_read_only_exact_files_and_ignore_backup_siblings(tmp_path):
    _create_empty_required_ledgers(tmp_path)
    exact = tmp_path / triage.LEDGER_DEFAULT_PATHS["active_cc"]
    exact.write_text("no active match\n")
    exact.with_name("active_cc.json.bak.pre-r3").write_text("Mary Smith 8015550100 stale\n")

    assert (
        triage.scan_in_flight(
            tmp_path,
            {"callerid_name": "Mary Smith", "number": "8015550100", "duration_s": 1},
            [],
            env={},
        )
        == []
    )


def test_city_only_candidate_does_not_seed_in_flight_but_caller_phone_does(tmp_path):
    _create_empty_required_ledgers(tmp_path)
    active_rmas = tmp_path / triage.LEDGER_DEFAULT_PATHS["active_rmas"]
    active_rmas.write_text(
        "Orange Cafe orange-site orange-contact active\nMary Smith 8015550100 active\n"
    )
    city_candidate = {
        "contact_id": "orange-contact",
        "account_id": "orange-site",
        "account_name": "Orange Cafe",
        "match_basis": "city_only",
        "verified": False,
    }

    assert (
        triage.scan_in_flight(
            tmp_path,
            {"callerid_name": None, "number": None, "duration_s": 1},
            [city_candidate],
            env={},
        )
        == []
    )

    hits = triage.scan_in_flight(
        tmp_path,
        {"callerid_name": "Mary Smith", "number": "8015550100", "duration_s": 1},
        [city_candidate],
        env={},
    )

    assert [(hit["ledger"], hit["one_line"]) for hit in hits] == [
        ("active_rmas", "Mary Smith 8015550100 active")
    ]


def test_directory_ledger_override_resolves_only_canonical_json_file(tmp_path):
    _create_empty_required_ledgers(tmp_path)
    pinned = tmp_path / "custom-rma"
    pinned.mkdir()
    (pinned / "active_rmas.json").write_text("Mary Smith active\n")
    (pinned / "active_rmas.json.bak.pre-r3").write_text("Mary Smith stale\n")

    hits = triage.scan_in_flight(
        tmp_path,
        {"callerid_name": "Mary Smith", "number": None, "duration_s": 1},
        [],
        env={"RC_VOICEMAIL_LEDGER_ACTIVE_RMAS": str(pinned)},
    )

    assert [(hit["ledger"], hit["one_line"]) for hit in hits] == [
        ("active_rmas", "Mary Smith active")
    ]


def test_empty_ledger_root_fails_loud(tmp_path):
    with pytest.raises(triage.TriageError) as caught:
        triage.scan_in_flight(
            tmp_path,
            {"callerid_name": "Mary", "number": "8015550100", "duration_s": 1},
            [],
            env={},
        )

    assert caught.value.stage == "in_flight"
    assert "active_rmas" in caught.value.message


def test_one_missing_canonical_ledger_fails_loud(tmp_path):
    _create_empty_required_ledgers(tmp_path, missing="active_jamf")

    with pytest.raises(triage.TriageError) as caught:
        triage.scan_in_flight(
            tmp_path,
            {"callerid_name": "Mary", "number": "8015550100", "duration_s": 1},
            [],
            env={},
        )

    assert caught.value.stage == "in_flight"
    assert "active_jamf" in caught.value.message


def test_present_but_empty_ledgers_succeed(tmp_path):
    _create_empty_required_ledgers(tmp_path)

    assert (
        triage.scan_in_flight(
            tmp_path,
            {"callerid_name": "Mary", "number": "8015550100", "duration_s": 1},
            [],
            env={},
        )
        == []
    )


def test_output_contract_has_exact_shape_and_list_candidates():
    output = triage.build_output(
        "123",
        {"callerid_name": None, "number": None, "duration_s": 48},
        "",
        True,
        [],
        [],
    )

    assert list(output) == [
        "ticket_id",
        "caller",
        "transcript",
        "no_speech",
        "site_candidates",
        "in_flight",
    ]
    assert output == {
        "ticket_id": "123",
        "caller": {"callerid_name": None, "number": None, "duration_s": 48},
        "transcript": "",
        "no_speech": True,
        "site_candidates": [],
        "in_flight": [],
    }


def test_pipeline_smoke_combines_fetch_transcript_candidates_and_ledgers(tmp_path):
    class FakeDesk:
        mode = "direct"

        def resolve_ticket_id(self, ticket_id):
            return ticket_id

        def list_threads(self, ticket_id):
            return [{"id": "vm", "fromEmailAddress": "notify@ringcentral.com"}]

        def get_thread(self, ticket_id, thread_id):
            return {
                "id": "vm",
                "summary": "From: Main Line - Mary (801) 555-0100 Length: 00:12",
                "attachments": [{"id": "mp3", "name": "voice.mp3", "href": "audio"}],
            }

        def download_attachment(self, href, destination, **ids):
            destination.write_bytes(b"audio")

        def search_contacts(self, query):
            if query == "8015550100":
                return [
                    {
                        "id": "contact-1",
                        "account": {"id": "site-1", "name": "Main Street"},
                    }
                ]
            return []

    transcriber = tmp_path / "whisper_transcribe.py"
    transcriber.write_text('print(\'{"transcript":"Please call me", "no_speech":false}\')\n')
    ledger_root = tmp_path / "ledgers"
    ledger_root.mkdir()
    _create_empty_required_ledgers(ledger_root)
    (ledger_root / triage.LEDGER_DEFAULT_PATHS["active_rmas"]).write_text('{"caller":"Mary"}\n')

    output = triage.triage_ticket(
        "123",
        FakeDesk(),
        transcriber_path=transcriber,
        ledger_root=ledger_root,
        destination_dir=tmp_path / "downloads",
    )

    assert output["transcript"] == "Please call me"
    assert output["no_speech"] is False
    assert output["site_candidates"] == [
        {
            "contact_id": "contact-1",
            "account_id": "site-1",
            "account_name": "Main Street",
            "match_basis": "phone_exact",
            "verified": False,
        }
    ]
    assert output["in_flight"] == [
        {
            "ledger": "active_rmas",
            "key": "rma/active_rmas.json:1",
            "one_line": '{"caller":"Mary"}',
        }
    ]


def test_token_source_accepts_direct_plain_file_and_json_file(tmp_path):
    plain = tmp_path / "plain-token"
    plain.write_text("plain-secret\n")
    json_file = tmp_path / "token.json"
    json_file.write_text('{"oauth": {"access_token": "json-secret"}}')

    assert triage.load_access_token({"ZOHO_DESK_ACCESS_TOKEN": "direct-secret"}) == (
        "direct-secret"
    )
    assert triage.load_access_token({"ZOHO_DESK_TOKEN_FILE": str(plain)}) == "plain-secret"
    assert triage.load_access_token({"ZOHO_DESK_TOKEN_FILE": str(json_file)}) == "json-secret"


def test_gateway_signing_payload_is_exact_and_uses_hex_digest():
    assert triage._sign(
        "shared-secret",
        "geordi",
        "get",
        "/desk/tickets/123/threads?ignored=query",
        1_700_000_000,
    ) == {
        "x-pinky-agent": "geordi",
        "x-pinky-timestamp": "1700000000",
        "x-pinky-signature": ("901d1d90a36ad82a3e4aa2f906ae7d168ad46e7d1d168e9623638aacb0bda270"),
    }


def test_auth_mode_auto_defaults_to_gateway_for_complete_gateway_group():
    assert (
        triage.select_auth_mode(
            {
                "ZOHO_API_HOST": "10.0.0.32,10.0.0.209",
                "ZOHO_API_SECRET": "shared-secret",
                "ZOHO_AGENT_NAME": "geordi",
                "ZOHO_DESK_ACCESS_TOKEN": "direct-token",
            }
        )
        == "gateway"
    )


def test_auth_mode_auto_defaults_to_direct_when_gateway_group_is_incomplete():
    assert (
        triage.select_auth_mode(
            {
                "ZOHO_API_HOST": "10.0.0.32",
                "ZOHO_API_SECRET": "shared-secret",
                "ZOHO_DESK_ACCESS_TOKEN": "direct-token",
            }
        )
        == "direct"
    )


def test_auth_mode_without_usable_group_fails_loud_and_names_missing_groups():
    with pytest.raises(triage.TriageError) as caught:
        triage.select_auth_mode({})

    assert caught.value.stage == "config"
    assert caught.value.exit_code == triage.EXIT_CONFIG
    assert "ZOHO_API_HOST" in caught.value.message
    assert "ZOHO_API_SECRET" in caught.value.message
    assert "ZOHO_AGENT_NAME" in caught.value.message
    assert "ZOHO_DESK_ACCESS_TOKEN or ZOHO_DESK_TOKEN_FILE" in caught.value.message


def test_explicit_gateway_mode_requires_agent_name_without_a_default():
    with pytest.raises(triage.TriageError) as caught:
        triage.select_auth_mode(
            {
                "RC_VOICEMAIL_AUTH_MODE": "gateway",
                "ZOHO_API_HOST": "10.0.0.32",
                "ZOHO_API_SECRET": "shared-secret",
            }
        )

    assert caught.value.stage == "config"
    assert caught.value.exit_code == triage.EXIT_CONFIG
    assert "ZOHO_AGENT_NAME" in caught.value.message
    assert "luka" not in caught.value.message
    assert "sasha" not in caught.value.message


def test_cli_client_factory_uses_ordered_gateway_hosts_and_explicit_credentials():
    args = triage.argparse.Namespace(
        desk_org_id=None,
        desk_base_url=triage.DEFAULT_DESK_BASE_URL,
        timeout=17.0,
    )

    client = triage._desk_client_from_env(
        args,
        {
            "ZOHO_API_HOST": "10.0.0.32, 10.0.0.209",
            "ZOHO_API_SECRET": "shared-secret",
            "ZOHO_AGENT_NAME": "geordi",
        },
    )

    assert client.mode == "gateway"
    assert client.base_urls == (
        "http://10.0.0.32:9100/desk/",
        "http://10.0.0.209:9100/desk/",
    )
    assert client._gateway_secret == "shared-secret"
    assert client._gateway_agent == "geordi"
    assert client.timeout_s == 17.0


def test_main_returns_zero_for_valid_no_speech_result(monkeypatch, capsys, tmp_path):
    expected = triage.build_output(
        "123",
        {"callerid_name": None, "number": None, "duration_s": 48},
        "",
        True,
        [],
        [],
    )
    monkeypatch.setattr(triage, "_desk_client_from_env", lambda args: object())
    monkeypatch.setattr(triage, "triage_ticket", lambda *args, **kwargs: expected)

    return_code = triage.main(
        [
            "123",
            "--transcriber",
            str(tmp_path / "whisper_transcribe.py"),
            "--ledger-root",
            str(tmp_path),
        ]
    )

    captured = capsys.readouterr()
    assert return_code == 0
    assert captured.err == ""
    assert json.loads(captured.out) == expected


def test_main_failure_is_nonzero_and_self_describing(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(triage, "_desk_client_from_env", lambda args: object())

    def fail(*args, **kwargs):
        raise triage.TriageError("transcribe", "helper crashed", triage.EXIT_TRANSCRIBE)

    monkeypatch.setattr(triage, "triage_ticket", fail)

    return_code = triage.main(
        [
            "123",
            "--transcriber",
            str(tmp_path / "whisper_transcribe.py"),
            "--ledger-root",
            str(tmp_path),
        ]
    )

    captured = capsys.readouterr()
    assert return_code == triage.EXIT_TRANSCRIBE
    assert captured.out == ""
    assert "stage=transcribe" in captured.err
    assert "ticket_id=123" in captured.err
    assert "helper crashed" in captured.err


@pytest.mark.parametrize(
    ("arguments", "ticket_fragment"),
    [
        (["123", "--timeout", "nope"], "ticket_id=123"),
        (["--timeout", "nope"], "ticket_id=''"),
    ],
)
def test_cli_argparse_failures_are_self_describing(arguments, ticket_fragment):
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), *arguments],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == triage.EXIT_CONFIG
    assert completed.stdout == ""
    assert "stage=config" in completed.stderr
    assert ticket_fragment in completed.stderr
    assert "usage:" not in completed.stderr
