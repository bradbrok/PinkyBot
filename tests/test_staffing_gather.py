import importlib.util
import io
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "staffing_gather.py"
FIXTURES = ROOT / "scripts" / "fixtures-543"
BTSPROBE = FIXTURES / "btsprobe_sample.json"
BOARD = FIXTURES / "bts_board_sample.json"

SPEC = importlib.util.spec_from_file_location("staffing_gather", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
staffing_gather = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(staffing_gather)


def _run(*args: object) -> tuple[subprocess.CompletedProcess[str], dict]:
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), *(str(arg) for arg in args)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    return completed, json.loads(completed.stdout)


def _base_args() -> tuple[object, ...]:
    return ("--btsprobe-file", BTSPROBE, "--board-file", BOARD)


def _write_json(path: Path, value: object) -> Path:
    path.write_text(json.dumps(value))
    return path


def test_fixture_join_preserves_btsprobe_blocks_and_board_loads():
    completed, result = _run(*_base_args())

    assert completed.returncode == 0
    assert result["ok"] is True
    day = result["members"]["danielson"]["days"]["2026-08-07"]
    assert {"type": "build", "h0": 15, "h1": 16, "rsvp": "needsAction"} in day["build"]
    assert set(day) == {"appt", "build", "queue", "ooo", "free"}
    assert day["free"] == [
        {"type": "free", "h0": 8, "h1": 10},
        {"type": "free", "h0": 12, "h1": 13},
        {"type": "free", "h0": 17, "h1": 18},
    ]

    build_load = result["members"]["danielson"]["loads"]["build"]
    assert build_load == {
        "status": "present",
        "load": 5,
        "provisional": 0,
        "effective": 5,
        "weight": 1.3,
        "weighted_rank": 5 / 1.3,
    }


def test_effective_load_includes_provisional_but_weighted_rank_uses_load(tmp_path):
    board = json.loads(BOARD.read_text())
    daniel = next(row for row in board["members"] if row["key"] == "daniel")
    daniel["load"] = 7
    daniel["provisional"] = 3
    daniel["weight"] = 2
    board_path = _write_json(tmp_path / "board.json", board)

    completed, result = _run("--btsprobe-file", BTSPROBE, "--board-file", board_path)

    assert completed.returncode == 0
    build_load = result["members"]["daniel"]["loads"]["build"]
    assert build_load["effective"] == 10
    assert build_load["weighted_rank"] == 3.5


def test_desk_absence_is_marked_absent_instead_of_zero():
    completed, result = _run(*_base_args())

    assert completed.returncode == 0
    assert result["sources"]["desk"] == {
        "mode": "absent",
        "status": "absent",
        "_err": [],
    }
    for member in staffing_gather.MEMBER_KEYS:
        assert result["members"][member]["loads"]["desk"] == {"status": "absent"}


def test_present_zero_desk_count_remains_distinct_from_absence(tmp_path):
    desk = {"danielson": 0, "daniel": 4, "aldo": 2, "julie": 1}
    desk_path = _write_json(tmp_path / "desk.json", desk)

    completed, result = _run(*_base_args(), "--desk-file", desk_path)

    assert completed.returncode == 0
    assert result["members"]["danielson"]["loads"]["desk"] == {
        "status": "present",
        "open_count": 0,
    }


def test_access_false_fails_closed_for_only_that_member(tmp_path):
    payload = json.loads(BTSPROBE.read_text())
    payload["access"]["julie"] = False
    btsprobe_path = _write_json(tmp_path / "btsprobe.json", payload)

    completed, result = _run("--btsprobe-file", btsprobe_path, "--board-file", BOARD)

    assert completed.returncode != 0
    assert result["ok"] is False
    assert result["members"]["julie"]["days"] == {}
    assert any("access is not true" in error for error in result["members"]["julie"]["_err"])
    assert result["members"]["aldo"]["days"]["2026-08-07"]["queue"]


def test_unknown_desk_agent_id_does_not_cross_the_two_daniels(tmp_path):
    desk = {
        "danielson": 3,
        "daniel": 8,
        "aldo": 2,
        "julie": 1,
        staffing_gather.DESK_AGENT_IDS["daniel"]: 99,
    }
    desk_path = _write_json(tmp_path / "desk.json", desk)

    completed, result = _run(*_base_args(), "--desk-file", desk_path)

    assert completed.returncode != 0
    assert result["members"]["daniel"]["loads"]["desk"]["open_count"] == 8
    assert result["members"]["danielson"]["loads"]["desk"]["open_count"] == 3
    assert any("unknown member keys" in error for error in result["sources"]["desk"]["_err"])


def test_unreadable_btsprobe_file_is_nonzero_and_marks_every_calendar(tmp_path):
    missing = tmp_path / "missing-btsprobe.json"

    completed, result = _run("--btsprobe-file", missing, "--board-file", BOARD)

    assert completed.returncode != 0
    assert any("cannot read" in error for error in result["sources"]["btsprobe"]["_err"])
    for member in staffing_gather.MEMBER_KEYS:
        assert result["members"][member]["days"] == {}
        assert any("calendar unavailable" in error for error in result["members"][member]["_err"])


def test_malformed_btsprobe_json_is_nonzero_and_marks_every_calendar(tmp_path):
    malformed = tmp_path / "malformed-btsprobe.json"
    malformed.write_text('{"scan":')

    completed, result = _run("--btsprobe-file", malformed, "--board-file", BOARD)

    assert completed.returncode != 0
    assert any("invalid JSON" in error for error in result["sources"]["btsprobe"]["_err"])
    for member in staffing_gather.MEMBER_KEYS:
        assert result["members"][member]["days"] == {}
        assert any("calendar unavailable" in error for error in result["members"][member]["_err"])


def test_unreadable_board_is_nonzero_and_marks_every_build_load(tmp_path):
    missing = tmp_path / "missing-board.json"

    completed, result = _run("--btsprobe-file", BTSPROBE, "--board-file", missing)

    assert completed.returncode != 0
    assert any("cannot read" in error for error in result["sources"]["board"]["_err"])
    for member in staffing_gather.MEMBER_KEYS:
        assert result["members"][member]["loads"]["build"]["status"] == "error"


def test_json_null_sources_fail_loud_instead_of_looking_absent(tmp_path):
    null_source = _write_json(tmp_path / "null.json", None)

    bts_completed, bts_result = _run("--btsprobe-file", null_source, "--board-file", BOARD)
    board_completed, board_result = _run("--btsprobe-file", BTSPROBE, "--board-file", null_source)
    desk_completed, desk_result = _run(*_base_args(), "--desk-file", null_source)

    assert bts_completed.returncode != 0
    assert "source payload is unavailable" in bts_result["sources"]["btsprobe"]["_err"]
    assert board_completed.returncode != 0
    assert "source payload is unavailable" in board_result["sources"]["board"]["_err"]
    assert desk_completed.returncode != 0
    assert "supplied source payload is unavailable" in desk_result["sources"]["desk"]["_err"]


def test_empty_matches_day_is_preserved_without_error(tmp_path):
    payload = json.loads(BTSPROBE.read_text())
    payload["scan"]["members"]["aldo"]["2026-08-08"] = []
    btsprobe_path = _write_json(tmp_path / "btsprobe.json", payload)

    completed, result = _run("--btsprobe-file", btsprobe_path, "--board-file", BOARD)

    assert completed.returncode == 0
    assert result["members"]["aldo"]["days"]["2026-08-08"] == {
        "appt": [],
        "build": [],
        "queue": [],
        "ooo": [],
        "free": [{"type": "free", "h0": 8, "h1": 18}],
    }
    assert result["members"]["aldo"]["_err"] == []


def test_supplied_desk_file_requires_every_exact_member_key(tmp_path):
    desk_path = _write_json(tmp_path / "desk.json", {"danielson": 3, "aldo": 2, "julie": 1})

    completed, result = _run(*_base_args(), "--desk-file", desk_path)

    assert completed.returncode != 0
    assert result["members"]["daniel"]["loads"]["desk"]["status"] == "error"
    assert any("absent" in error for error in result["members"]["daniel"]["_err"])


def test_range_is_from_inclusive_and_to_exclusive():
    completed, result = _run(*_base_args())

    assert completed.returncode == 0
    assert list(result["members"]["aldo"]["days"]) == [
        "2026-08-07",
        "2026-08-08",
        "2026-08-09",
    ]
    assert "2026-08-10" not in result["members"]["aldo"]["days"]


def test_event_starting_at_17_occupies_the_final_hour(tmp_path):
    payload = json.loads(BTSPROBE.read_text())
    payload["scan"]["members"]["aldo"]["2026-08-07"] = [
        {"type": "appt", "h0": 17, "h1": 18, "rsvp": "accepted"}
    ]
    btsprobe_path = _write_json(tmp_path / "btsprobe.json", payload)

    completed, result = _run("--btsprobe-file", btsprobe_path, "--board-file", BOARD)

    assert completed.returncode == 0
    day = result["members"]["aldo"]["days"]["2026-08-07"]
    assert day["appt"] == [{"type": "appt", "h0": 17, "h1": 18, "rsvp": "accepted"}]
    assert day["free"] == [{"type": "free", "h0": 8, "h1": 17}]


def test_ooo_day_has_no_free_time(tmp_path):
    payload = json.loads(BTSPROBE.read_text())
    payload["scan"]["members"]["julie"]["2026-08-07"] = [
        {"type": "ooo", "h0": 0, "h1": 24, "rsvp": "accepted"}
    ]
    btsprobe_path = _write_json(tmp_path / "btsprobe.json", payload)

    completed, result = _run("--btsprobe-file", btsprobe_path, "--board-file", BOARD)

    assert completed.returncode == 0
    day = result["members"]["julie"]["days"]["2026-08-07"]
    assert day["ooo"] == [{"type": "ooo", "h0": 0, "h1": 24, "rsvp": "accepted"}]
    assert day["free"] == []


def test_non_whole_hour_block_fails_closed_instead_of_manufacturing_free(tmp_path):
    payload = json.loads(BTSPROBE.read_text())
    payload["scan"]["members"]["aldo"]["2026-08-07"] = [
        {"type": "appt", "h0": 10.5, "h1": 12, "rsvp": "accepted"}
    ]
    btsprobe_path = _write_json(tmp_path / "btsprobe.json", payload)

    completed, result = _run("--btsprobe-file", btsprobe_path, "--board-file", BOARD)

    assert completed.returncode != 0
    assert "2026-08-07" not in result["members"]["aldo"]["days"]
    assert any("invalid h0/h1" in error for error in result["members"]["aldo"]["_err"])


def test_unknown_scan_type_fails_closed_instead_of_manufacturing_free(tmp_path):
    payload = json.loads(BTSPROBE.read_text())
    payload["scan"]["members"]["aldo"]["2026-08-07"] = [
        {"type": "free", "h0": 8, "h1": 18, "rsvp": "accepted"}
    ]
    btsprobe_path = _write_json(tmp_path / "btsprobe.json", payload)

    completed, result = _run("--btsprobe-file", btsprobe_path, "--board-file", BOARD)

    assert completed.returncode != 0
    assert "2026-08-07" not in result["members"]["aldo"]["days"]
    assert any("unknown type" in error for error in result["members"]["aldo"]["_err"])


def test_group_calendar_remains_a_separate_stream():
    completed, result = _run(*_base_args())

    assert completed.returncode == 0
    assert result["group"]["days"]["2026-08-07"]["blocks"]
    assert "group" not in result["members"]["julie"]["days"]["2026-08-07"]


def test_url_fetch_uses_optional_bearer_token(monkeypatch):
    seen = {}

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            self.close()

    def fake_urlopen(request, *, timeout):
        seen["authorization"] = request.get_header("Authorization")
        seen["timeout"] = timeout
        return Response(b'{"scan": {}}')

    monkeypatch.setattr(staffing_gather, "urlopen", fake_urlopen)

    payload, error = staffing_gather._read_json_url(
        "https://example.invalid/exec?btsprobe=1", 12.0, "secret-token"
    )

    assert error is None
    assert payload == {"scan": {}}
    assert seen == {"authorization": "Bearer secret-token", "timeout": 12.0}
