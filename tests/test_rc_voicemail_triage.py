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

        def download_attachment(self, href, destination):
            self.downloaded = (href, destination)
            destination.write_bytes(b"audio")

    desk = FakeDesk()
    path, caller = triage.fetch_voicemail_audio(desk, "ticket-1", tmp_path)

    assert desk.downloaded == ("audio", path)
    assert path.read_bytes() == b"audio"
    assert caller["callerid_name"] == "Mary"
    assert caller["number"] == "8015550100"


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

        def download_attachment(self, href, destination):
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
    (tmp_path / "active_rmas.jsonl").write_text('{"caller":"Mary Smith"}\n')
    label_dir = tmp_path / "label_requests"
    label_dir.mkdir()
    (label_dir / "open.md").write_text("call (801) 555-0100\n")
    (tmp_path / "active_cc.csv").write_text("site-1,chargeback\n")
    (tmp_path / "active_jamf.txt").write_text("Main Street device\n")
    (tmp_path / "ubereats.json").write_text('{"contact":"contact-1"}\n')

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
    filenames = {
        "active_rmas": "active_rmas.jsonl",
        "label_requests": "label_requests.jsonl",
        "active_cc": "active_cc.jsonl",
        "active_jamf": "active_jamf.jsonl",
        "integration_requests": "ubereats.jsonl",
    }
    for canonical, filename in filenames.items():
        if canonical != missing:
            (root / filename).write_text("")


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
        def list_threads(self, ticket_id):
            return [{"id": "vm", "fromEmailAddress": "notify@ringcentral.com"}]

        def get_thread(self, ticket_id, thread_id):
            return {
                "id": "vm",
                "summary": "From: Main Line - Mary (801) 555-0100 Length: 00:12",
                "attachments": [{"id": "mp3", "name": "voice.mp3", "href": "audio"}],
            }

        def download_attachment(self, href, destination):
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
    (ledger_root / "active_rmas.jsonl").write_text('{"caller":"Mary"}\n')
    for filename in ("label_requests", "active_cc", "active_jamf", "ubereats"):
        (ledger_root / f"{filename}.jsonl").write_text("")

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
            "key": "active_rmas.jsonl:1",
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


def test_main_returns_zero_for_valid_no_speech_result(monkeypatch, capsys, tmp_path):
    expected = triage.build_output(
        "123",
        {"callerid_name": None, "number": None, "duration_s": 48},
        "",
        True,
        [],
        [],
    )
    monkeypatch.setattr(triage, "load_access_token", lambda: "token")
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
    monkeypatch.setattr(triage, "load_access_token", lambda: "token")

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
