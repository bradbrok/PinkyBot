"""Shared rate-limit parsing, diagnostics, and HTTP response regressions."""

import builtins
import json
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

import pinky_daemon.scheduler as scheduler_module

NOW = datetime(2026, 1, 15, 20, tzinfo=timezone.utc).timestamp()


@pytest.fixture
def rate_limit_file(tmp_path, monkeypatch):
    path = tmp_path / "rate-limits.json"
    monkeypatch.setattr(scheduler_module, "_RATE_LIMIT_FILE", str(path))
    monkeypatch.setattr(scheduler_module.time, "time", lambda: NOW)
    monkeypatch.setattr(scheduler_module, "_rate_limit_last_warned_at", None, raising=False)
    return path


@pytest.fixture
def api_client(tmp_path, rate_limit_file, monkeypatch):
    import pinky_daemon.api as api_module

    def isolated_open(file, *args, **kwargs):
        if file == "/tmp/claude-rate-limits.json":
            file = rate_limit_file
        return builtins.open(file, *args, **kwargs)

    monkeypatch.setattr(api_module, "open", isolated_open, raising=False)
    app = api_module.create_api(
        default_working_dir=str(tmp_path),
        db_path=str(tmp_path / "test.db"),
    )
    client = TestClient(app, raise_server_exceptions=False)
    yield client
    client.close()
    app.state.store_catalog.close()


@pytest.mark.parametrize(
    "content",
    [
        pytest.param(b'{"updated_at": null}', id="null-timestamp"),
        pytest.param(json.dumps({"updated_at": NOW, "five_hour": None}).encode(), id="null-window"),
        pytest.param(b"[]", id="non-object"),
        pytest.param(b"\xff", id="invalid-encoding"),
    ],
)
def test_api_info_omits_malformed_rate_limits(api_client, rate_limit_file, content):
    rate_limit_file.write_bytes(content)

    response = api_client.get("/api")

    assert response.status_code == 200
    assert "rate_limits" not in response.json()


def test_api_info_reports_both_rate_limit_percentages(api_client, rate_limit_file):
    rate_limit_file.write_text(
        json.dumps(
            {
                "updated_at": NOW,
                "five_hour": {"used_percentage": 23},
                "seven_day": {"used_percentage": 85},
            }
        )
    )

    response = api_client.get("/api")

    assert response.status_code == 200
    assert response.json()["rate_limits"] == {"five_hour_pct": 23, "seven_day_pct": 85}


@pytest.mark.parametrize("percentage", [float("nan"), float("inf"), -float("inf"), True, False])
@pytest.mark.parametrize("window", ["five_hour", "seven_day"])
def test_rate_limit_status_classifies_only_finite_numbers(rate_limit_file, percentage, window):
    data = {
        "updated_at": NOW,
        "five_hour": {"used_percentage": 85},
        "seven_day": {"used_percentage": 85},
    }
    data[window] = {"used_percentage": percentage}
    rate_limit_file.write_text(json.dumps(data))

    status = scheduler_module.read_rate_limit_status()

    assert getattr(status, f"{window}_pct") is None
    sibling = "seven_day" if window == "five_hour" else "five_hour"
    assert getattr(status, f"{sibling}_pct") == 85
    assert status.error is None
    assert status.stale is False
    assert scheduler_module._rate_limits_ok() is False


@pytest.mark.parametrize("content", [b"[]", b'{"updated_at":null}', b"\xff", b"{"])
def test_rate_limit_status_parse_errors_invalidate_both_windows(rate_limit_file, content):
    rate_limit_file.write_bytes(content)

    status = scheduler_module.read_rate_limit_status()

    assert status.five_hour_pct is None
    assert status.seven_day_pct is None
    assert status.error


def test_rate_limit_status_stale_values_are_retained_but_do_not_gate(rate_limit_file, capsys):
    rate_limit_file.write_text(
        json.dumps(
            {
                "updated_at": NOW - 301,
                "five_hour": {"used_percentage": 85},
                "seven_day": {"used_percentage": 23},
            }
        )
    )

    status = scheduler_module.read_rate_limit_status()

    assert status.five_hour_pct == 85
    assert status.seven_day_pct == 23
    assert status.stale is True
    assert status.error is None
    assert scheduler_module._rate_limits_ok() is True
    assert capsys.readouterr().err == ""


def test_rate_limit_fail_open_warning_is_throttled(rate_limit_file, monkeypatch, capsys):
    rate_limit_file.write_text('{"updated_at": "invalid"}')

    assert scheduler_module._rate_limits_ok() is True
    first_log = capsys.readouterr().err
    assert first_log.count("scheduler: rate-limit file unreadable, failing open:") == 1
    assert "TypeError:" in first_log

    monkeypatch.setattr(scheduler_module.time, "time", lambda: NOW + 299)
    assert scheduler_module._rate_limits_ok() is True
    assert capsys.readouterr().err == ""

    monkeypatch.setattr(scheduler_module.time, "time", lambda: NOW + 300)
    assert scheduler_module._rate_limits_ok() is True
    assert capsys.readouterr().err.count("rate-limit file unreadable, failing open:") == 1


def test_rate_limit_missing_file_is_quiet(rate_limit_file, capsys):
    assert not rate_limit_file.exists()

    assert scheduler_module._rate_limits_ok() is True

    assert capsys.readouterr().err == ""
