"""Argument recovery delegates to a lenient parser without changing the real CLI."""

import contextlib
import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.test_deploy_guard import TOKEN, guard_module

PARSER_CASES = [
    ("defaults", [], True, "api", "data/conversations.db"),
    ("literal-api", ["--mode", "api"], True, "api", "data/conversations.db"),
    ("abbreviated-api", ["--mo", "api"], True, "api", "data/conversations.db"),
    ("attached-abbreviated-api", ["--mo=api"], True, "api", "data/conversations.db"),
    (
        "abbreviated-inspection",
        ["--mo", "check-startup"],
        True,
        "check-startup",
        "data/conversations.db",
    ),
    ("abbreviated-poll", ["--mo", "poll"], True, "poll", "data/conversations.db"),
    ("abbreviated-database", ["--db", "x"], True, "api", "x"),
    ("abbreviated-database-dash", ["--db-p", "-"], True, "api", "-"),
    ("dash-spaces-short", ["--db-path", "-x y"], True, "api", "-x y"),
    ("negative-integer", ["--db-path", "-1"], True, "api", "-1"),
    ("repeated-negative", ["--db-path", "-1.25", "--db-path", "-2"], True, "api", "-2"),
    ("attached-dash", ["--db-path=-q"], True, "api", "-q"),
    ("attached-poll", ["--mode=poll"], True, "poll", "data/conversations.db"),
    ("ambiguous-mode", ["--m", "check-startup"], False, None, None),
    ("dangling-mode", ["--mode", "check-startup", "--mode"], False, None, None),
    ("missing-database", ["--db-path", "--mode", "poll"], False, None, None),
    ("help", ["-h"], False, None, None),
    ("abbreviated-help", ["--mode", "poll", "--he"], False, None, None),
    ("api-invalid-port", ["--mode", "api", "--port", "abc"], False, "api", "data/conversations.db"),
    (
        "inspection-invalid-port",
        ["--port", "abc", "--mode", "check-startup"],
        False,
        "check-startup",
        "data/conversations.db",
    ),
    ("unknown-choice", ["--mode", "foo"], False, "foo", "data/conversations.db"),
    ("separator", ["--", "--mode", "poll"], False, "api", "data/conversations.db"),
    ("unknown-option", ["--mode", "api", "--bogus"], False, "api", "data/conversations.db"),
    ("attached-abbreviated-database", ["--db=value"], True, "api", "value"),
    ("last-abbreviated-database", ["--db-path", "first", "--db=last"], True, "api", "last"),
    (
        "attached-abbreviated-inspection",
        ["--mo=check-startup"],
        True,
        "check-startup",
        "data/conversations.db",
    ),
    ("dash-spaces", ["--db-path", "- path with spaces"], True, "api", "- path with spaces"),
    ("fraction", ["--db-path", "-.5"], True, "api", "-.5"),
    ("negative-zero", ["--db-path", "-0"], True, "api", "-0"),
    ("exponent", ["--db-path", "-1e3"], False, None, None),
    ("dash-option", ["--db-path", "-x"], False, None, None),
    ("explicit-negative", ["--mode", "api", "--db-path", "-3"], True, "api", "-3"),
    ("explicit-review-negative", ["--mode", "api", "--db-path", "-1"], True, "api", "-1"),
    (
        "ordinary-spaces",
        ["--mode", "api", "--db-path", "path with spaces"],
        True,
        "api",
        "path with spaces",
    ),
    ("full-single-dash", ["--db-path", "-"], True, "api", "-"),
    ("negative-decimal", ["--db-path", "-1.25"], True, "api", "-1.25"),
    ("last-negative", ["--db-path", "first", "--db-path", "-1"], True, "api", "-1"),
    ("attached-value", ["--db-path=VALUE"], True, "api", "VALUE"),
    ("attached-empty", ["--db-path="], True, "api", ""),
    ("bare-separator", ["--"], False, "api", "data/conversations.db"),
    (
        "database-before-separator",
        ["--db-path", "before", "--", "--db-path=after"],
        False,
        "api",
        "before",
    ),
    ("last-normal-value", ["--db-path", "first", "--db-path", "last"], True, "api", "last"),
    ("last-mode", ["--mode", "poll", "--mode=api", "--db-path", "last"], True, "api", "last"),
    ("dangling-database", ["--db-path", "first", "--db-path"], False, None, None),
    ("last-dangling-mode", ["--mode", "poll", "--mode", "api", "--mode"], False, None, None),
    ("abbreviation-selected-root", ["--db=selected/db"], True, "api", "selected/db"),
    (
        "last-abbreviation-selected-root",
        ["--db-path", "previous/db", "--db", "selected/db"],
        True,
        "api",
        "selected/db",
    ),
    (
        "invalid-session-count",
        ["--max-sessions", "abc", "--mo", "poll"],
        False,
        "poll",
        "data/conversations.db",
    ),
]


def main_parse(arguments, monkeypatch):
    from pinky_daemon import __main__ as entry

    captured, parsers = [], []
    real_parse = entry.argparse.ArgumentParser.parse_args

    def parse(parser, *args, **kwargs):
        parsers.append(parser)
        return real_parse(parser, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(sys, "argv", ["pinky", *arguments])
        patch.setattr(entry, "_install_faulthandler", lambda: None)
        patch.setattr(entry, "_load_dotenv", lambda: None)
        patch.setattr(entry, "_quiet_http_client_loggers", lambda: None)
        patch.setattr(entry, "_record_bootstrap_failure", lambda *args: None)
        patch.setattr(entry.argparse.ArgumentParser, "parse_args", parse)

        def capture(args):
            captured.append(args)
            return SimpleNamespace(close=lambda: None)

        patch.setattr(entry, "_start_api_guard", capture)
        patch.setattr(entry, "_run_api", lambda args: None)
        patch.setattr(entry, "_run_poll", lambda args: None)
        patch.setattr(entry, "_run_startup_check", lambda args: 0)
        try:
            entry.main()
        except SystemExit as exc:
            assert exc.code == (0 if captured else 2)
    assert len(parsers) == 1
    return (captured[0] if captured else None), parsers[0]


@pytest.mark.parametrize("case", PARSER_CASES, ids=[case[0] for case in PARSER_CASES])
def test_fallback_parity_with_the_actual_entry_parser(monkeypatch, capsys, case):
    from pinky_daemon import __main__ as entry

    _, arguments, accepted, expected_mode, expected_db = case
    parser = entry._argument_parser()
    assert parser.allow_abbrev is True
    actual = None
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        try:
            actual = parser.parse_args(arguments)
        except SystemExit as exc:
            assert exc.code in {0, 2}
    assert (actual is not None) is accepted
    monkeypatch.setattr(sys, "argv", ["pinky", *arguments])
    fallback = entry._minimal_start_args()
    assert capsys.readouterr() == ("", "")
    assert (fallback.mode, fallback.db_path) == (expected_mode, expected_db)
    if accepted:
        assert (fallback.mode, fallback.db_path) == (actual.mode, actual.db_path)


@pytest.mark.parametrize("case", PARSER_CASES, ids=[case[0] for case in PARSER_CASES])
@pytest.mark.parametrize("phase", ["dotenv", "parse"])
def test_each_recovered_mode_controls_early_accounting(tmp_path, monkeypatch, capsys, case, phase):
    from pinky_daemon import __main__ as entry

    _, arguments, _, expected_mode, expected_db = case
    work = tmp_path / "working"
    work.mkdir(mode=0o700)
    (work / "data").mkdir(mode=0o700)
    monkeypatch.chdir(work)
    expected_root = Path(expected_db).resolve().parent if expected_mode == "api" else None
    if expected_root is not None:
        expected_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    g = guard_module()
    failure = KeyboardInterrupt(TOKEN)

    def stopped():
        raise failure

    originals = []
    real_parse = entry.argparse.ArgumentParser.parse_args

    def parse(parser, *args, **kwargs):
        try:
            return real_parse(parser, *args, **kwargs)
        except BaseException as exc:
            originals.append(exc)
            raise

    monkeypatch.setattr(entry.argparse.ArgumentParser, "parse_args", parse)
    monkeypatch.setattr(entry, "_install_faulthandler", lambda: None)
    monkeypatch.setattr(entry, "_load_dotenv", stopped if phase == "dotenv" else lambda: None)
    monkeypatch.setattr(g.socket, "gethostname", lambda: "fixture-node")
    monkeypatch.setattr(
        sys, "argv", ["pinky", *arguments, *(["--port", "invalid"] if phase == "parse" else [])]
    )
    with pytest.raises(KeyboardInterrupt if phase == "dotenv" else SystemExit) as caught:
        entry.main()
    assert caught.value is (failure if phase == "dotenv" else originals[0])
    if phase == "dotenv":
        assert capsys.readouterr() == ("", "")
    histories = list(tmp_path.rglob("starts.jsonl"))
    if expected_mode == "api":
        history = expected_root / "deploy/starts.jsonl"
        assert histories == [history]
        rows = [json.loads(line) for line in history.read_text().splitlines()]
        assert len(rows) == 1 and not rows[0].get("pending")
        recorded = json.loads((expected_root / "deploy/last_failure.json").read_text())
        assert recorded["exc_type"] == type(caught.value).__name__ and recorded["detail"] == ""
    else:
        assert histories == [] and list(tmp_path.rglob("deploy")) == []


def test_negative_number_options_make_an_unrecoverable_value_unknown(monkeypatch, capsys):
    from pinky_daemon import __main__ as entry

    initialize = entry.argparse.ArgumentParser.__init__

    def with_negative_option(parser, *args, **kwargs):
        initialize(parser, *args, **kwargs)
        parser.add_argument("-2", action="store_true")

    monkeypatch.setattr(entry.argparse.ArgumentParser, "__init__", with_negative_option)
    monkeypatch.setattr(sys, "argv", ["pinky", "--db-path", "-1"])
    fallback = entry._minimal_start_args()
    assert capsys.readouterr() == ("", "")
    assert (fallback.mode, fallback.db_path) == (None, None)


@pytest.mark.parametrize("form", ["integer", "decimal", "last-negative"])
@pytest.mark.parametrize("phase", ["dotenv", "parse"])
def test_negative_database_failures_record_at_the_parser_selected_root(
    tmp_path, monkeypatch, form, phase
):
    from pinky_daemon import __main__ as entry

    g = guard_module()
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir(mode=0o700)
    previous = tmp_path / "previous"
    previous.mkdir(mode=0o700)
    database = {
        "integer": ["--db-path", "-1"],
        "decimal": ["--db-path", "-1.25"],
        "last-negative": ["--db-path", str(previous / "db"), "--db-path", "-1"],
    }[form]
    arguments = ["--mode", "api", *database]
    selected, _ = main_parse(arguments, monkeypatch)
    expected_root = Path(selected.db_path).resolve().parent
    assert expected_root == tmp_path
    failure = KeyboardInterrupt(TOKEN)

    def stopped():
        raise failure

    original_exits = []
    real_parse = entry.argparse.ArgumentParser.parse_args

    def parse(parser, *args, **kwargs):
        try:
            return real_parse(parser, *args, **kwargs)
        except BaseException as exc:
            original_exits.append(exc)
            raise

    monkeypatch.setattr(entry.argparse.ArgumentParser, "parse_args", parse)
    monkeypatch.setattr(entry, "_install_faulthandler", lambda: None)
    monkeypatch.setattr(entry, "_load_dotenv", stopped if phase == "dotenv" else lambda: None)
    monkeypatch.setattr(g.socket, "gethostname", lambda: "fixture-node")
    monkeypatch.setattr(
        sys, "argv", ["pinky", *arguments, *(["--port", "invalid"] if phase == "parse" else [])]
    )
    with pytest.raises(KeyboardInterrupt if phase == "dotenv" else SystemExit) as caught:
        entry.main()
    assert caught.value is (failure if phase == "dotenv" else original_exits[0])
    if phase == "parse":
        assert caught.value.code == 2
    history = expected_root / "deploy/starts.jsonl"
    rows = (
        [json.loads(line) for line in history.read_text().splitlines()] if history.exists() else []
    )
    assert len(rows) == 1 and not rows[0].get("pending")
    assert list(tmp_path.rglob("starts.jsonl")) == [history]
    recorded = json.loads((expected_root / "deploy/last_failure.json").read_text())
    assert recorded["exc_type"] == type(caught.value).__name__ and recorded["detail"] == ""


class LoaderAbort(BaseException):
    pass


@pytest.mark.parametrize("mode", ["api", "poll", "check-startup", "future-mode"])
@pytest.mark.parametrize("failure_type", [KeyboardInterrupt, GeneratorExit, LoaderAbort])
def test_additional_loader_base_exceptions_are_accounted_only_in_api_mode(
    tmp_path, monkeypatch, capsys, mode, failure_type
):
    from pinky_daemon import __main__ as entry

    g = guard_module()
    failure = failure_type(TOKEN)

    def stopped():
        raise failure

    monkeypatch.setattr(sys, "argv", ["pinky", "--mode", mode, "--db-path", str(tmp_path / "db")])
    monkeypatch.setattr(entry, "_install_faulthandler", lambda: None)
    monkeypatch.setattr(entry, "_load_dotenv", stopped)
    monkeypatch.setattr(g.socket, "gethostname", lambda: "fixture-node")
    with pytest.raises(failure_type) as caught:
        entry.main()
    assert caught.value is failure
    assert capsys.readouterr() == ("", "")
    history = tmp_path / "deploy/starts.jsonl"
    if mode == "api":
        assert len(history.read_text().splitlines()) == 1
    else:
        assert not history.exists() and not (tmp_path / "deploy").exists()


@pytest.mark.parametrize("mode", ["api", "poll", "check-startup", "future-mode"])
@pytest.mark.parametrize("failure_type", [RuntimeError, SystemExit])
def test_existing_loader_exception_paths_keep_their_original_mode_behavior(
    tmp_path, monkeypatch, capsys, mode, failure_type
):
    from pinky_daemon import __main__ as entry

    g = guard_module()
    failure = failure_type(7)

    def stopped():
        raise failure

    monkeypatch.setattr(sys, "argv", ["pinky", "--mode", mode, "--db-path", str(tmp_path / "db")])
    monkeypatch.setattr(entry, "_install_faulthandler", lambda: None)
    monkeypatch.setattr(entry, "_load_dotenv", stopped)
    monkeypatch.setattr(g.socket, "gethostname", lambda: "fixture-node")
    with pytest.raises(SystemExit if mode == "check-startup" else failure_type) as caught:
        entry.main()
    output = capsys.readouterr()
    if mode == "check-startup":
        assert caught.value.code == 2 and caught.value is not failure
        assert output.out == ""
        assert output.err == f"startup check could not complete: {failure_type.__name__}: 7\n"
    else:
        assert caught.value is failure and output == ("", "")
    assert (tmp_path / "deploy/starts.jsonl").exists() is (mode == "api")


# The historical loader boundary is retained verbatim as the comparison oracle.
HISTORICAL_LOADER_SOURCE = 'def _startup_check_incomplete(exc: BaseException) -> int:\n    detail = " ".join(str(exc).splitlines())\n    print(f"startup check could not complete: {type(exc).__name__}: {detail}", file=sys.stderr)\n    return 2\n\n\ndef main() -> None:\n    _install_faulthandler()\n    try:\n        _load_dotenv()\n    except (Exception, SystemExit) as exc:\n        mode_parser = argparse.ArgumentParser(add_help=False)\n        mode_parser.add_argument("--mode", default="api")\n        mode, _ = mode_parser.parse_known_args()\n        if mode.mode != "check-startup":\n            raise\n        sys.exit(_startup_check_incomplete(exc))\n'


@pytest.mark.parametrize(
    "spelling", [("--mo", "poll"), ("--mo", "check-startup"), ("--mo=check-startup",)]
)
@pytest.mark.parametrize(
    "failure_type", [RuntimeError, SystemExit, KeyboardInterrupt, GeneratorExit, LoaderAbort]
)
def test_abbreviated_loader_modes_match_the_historical_boundary(
    tmp_path, monkeypatch, spelling, failure_type
):
    from pinky_daemon import __main__ as entry

    failure = failure_type(7)

    def stopped():
        raise failure

    namespace = {
        "argparse": entry.argparse,
        "sys": sys,
        "_install_faulthandler": lambda: None,
        "_load_dotenv": stopped,
    }
    exec(HISTORICAL_LOADER_SOURCE, namespace)
    monkeypatch.setattr(sys, "argv", ["pinky", *spelling, "--db-path", str(tmp_path / "db")])
    monkeypatch.setattr(entry, "_install_faulthandler", lambda: None)
    monkeypatch.setattr(entry, "_load_dotenv", stopped)
    monkeypatch.setattr(guard_module().socket, "gethostname", lambda: "fixture-node")
    outcomes = []
    for call in [namespace["main"], entry.main]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            try:
                call()
            except BaseException as caught:
                outcomes.append(
                    (
                        type(caught),
                        getattr(caught, "code", None),
                        caught is failure,
                        stdout.getvalue(),
                        stderr.getvalue(),
                    )
                )
            else:
                pytest.fail("the loader failure must propagate")
    assert outcomes[0] == outcomes[1]
    assert not (tmp_path / "deploy").exists()


@pytest.mark.parametrize("stage", ["build", "parse"])
@pytest.mark.parametrize(
    "failure_type", [RuntimeError, SystemExit, KeyboardInterrupt, GeneratorExit, LoaderAbort]
)
def test_copy_failures_return_unknown_without_output(monkeypatch, capsys, stage, failure_type):
    from pinky_daemon import __main__ as entry

    original = entry._argument_parser
    failure = failure_type("fixture copy failure")

    def stop(*args, **kwargs):
        raise failure

    def build():
        if stage == "build":
            raise failure
        parser = original()
        monkeypatch.setattr(parser, "parse_known_args", stop)
        return parser

    monkeypatch.setattr(entry, "_argument_parser", build)
    monkeypatch.setattr(sys, "argv", ["pinky", "--mode", "api"])
    try:
        fallback = entry._minimal_start_args()
    except BaseException as caught:
        pytest.fail(f"a copy failure must return UNKNOWN, got {type(caught).__name__}")
    assert (fallback.mode, fallback.db_path) == (None, None)
    assert capsys.readouterr() == ("", "")


def test_recovery_copy_does_not_mutate_the_real_parser(monkeypatch, capsys):
    from pinky_daemon import __main__ as entry

    factory, built = entry._argument_parser, []

    def build():
        parser = factory()
        built.append(parser)
        return parser

    monkeypatch.setattr(entry, "_argument_parser", build)
    actual = entry._argument_parser()
    help_before = actual.format_help()
    monkeypatch.setattr(sys, "argv", ["pinky", "--port", "abc", "--mode", "foo"])
    recovered = entry._minimal_start_args()
    assert (recovered.mode, recovered.db_path) == ("foo", "data/conversations.db")
    assert capsys.readouterr() == ("", "")
    assert len(built) == 2 and built[0] is not built[1]
    assert actual.allow_abbrev is True and actual.format_help() == help_before
    with contextlib.redirect_stderr(io.StringIO()):
        with pytest.raises(SystemExit) as bad_port:
            actual.parse_args(["--port", "abc"])
        with pytest.raises(SystemExit) as bad_mode:
            actual.parse_args(["--mode", "foo"])
    assert bad_port.value.code == bad_mode.value.code == 2
    assert actual.parse_args(["--mo", "poll", "--port", "123"]).port == 123
