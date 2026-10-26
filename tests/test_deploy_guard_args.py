"""Early accounting uses the real parser's database choice and preserves other modes."""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.test_deploy_guard import TOKEN, guard_module


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


@pytest.mark.parametrize(
    "arguments,accepted_projection",
    [
        ([], None),
        (["--db-path", "-1"], None),
        (["--db-path", "-1.25"], None),
        (["--db-path", "-.5"], None),
        (["--db-path", "first", "--db-path", "-1"], None),
        (["--db-path", "first", "--db-path", "-1.25"], None),
        (["--db-path=VALUE"], None),
        (["--db-path=-fixture"], None),
        (["--db-path="], None),
        (["--"], []),
        (["--db-path", "before", "--"], ["--db-path", "before"]),
        (["--mode", "poll"], None),
        (["--mode", "check-startup"], None),
        (["--mode", "poll", "--mode=api", "--db-path", "last"], None),
        (["--db-path", "first", "--db-path"], ["--db-path", "first"]),
        (
            ["--mode", "api", "--db-path", "last", "--unknown-option"],
            ["--mode", "api", "--db-path", "last"],
        ),
        (["--db-path", "before", "--", "--db-path=after"], ["--db-path", "before"]),
        (["--mode", "poll", "--mode"], ["--mode", "poll"]),
        (["--db-path", "--mode", "api"], ["--mode", "api"]),
    ],
)
def test_fallback_parity_with_the_actual_entry_parser(monkeypatch, arguments, accepted_projection):
    from pinky_daemon import __main__ as entry

    actual, parser = main_parse(arguments, monkeypatch)
    if accepted_projection is None:
        assert actual is not None
    else:
        assert actual is None
        actual = parser.parse_args(accepted_projection)
    monkeypatch.setattr(sys, "argv", ["pinky", *arguments])
    fallback = entry._minimal_start_args()
    assert (fallback.mode, fallback.db_path) == (actual.mode, actual.db_path)


def test_negative_number_options_disable_negative_value_recovery(monkeypatch):
    from pinky_daemon import __main__ as entry

    initialize = entry.argparse.ArgumentParser.__init__

    def with_negative_option(parser, *args, **kwargs):
        initialize(parser, *args, **kwargs)
        parser.add_argument("-2", action="store_true")

    monkeypatch.setattr(entry.argparse.ArgumentParser, "__init__", with_negative_option)
    arguments = ["--db-path", "-1"]
    actual, parser = main_parse(arguments, monkeypatch)
    assert actual is None
    defaults = parser.parse_args([])
    monkeypatch.setattr(sys, "argv", ["pinky", *arguments])
    fallback = entry._minimal_start_args()
    assert (fallback.mode, fallback.db_path) == (defaults.mode, defaults.db_path)


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
