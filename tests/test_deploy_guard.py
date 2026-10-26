"""Startup recovery decisions, private state and daemon-free notification."""

from __future__ import annotations

import asyncio
import importlib
import json
import logging
import os
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

P = "a" * 40
T = "b" * 40
TOKEN = "fixture-alert-secret-marker"


def guard_module():
    try:
        return importlib.import_module("pinky_daemon.deploy_guard")
    except ModuleNotFoundError:
        pytest.fail("the standalone startup guard is required")


def record(repo, **changes):
    value = dict(
        schema=1,
        deploy_id="c" * 32,
        created_at=900.0,
        repo_dir=str(repo),
        previous_ref=P,
        previous_label="previous",
        target_ref=T,
        target_label="target",
        deps_rebuilt=False,
        forced_reset=False,
        requested_by="",
        max_attempts=3,
        state="armed",
        attempts=[],
        post_rollback_attempts=[],
        rollback=None,
    )
    value.update(changes)
    return value


def save(state, name, value):
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = state / name
    path.write_text(json.dumps(value))
    path.chmod(0o600)


def load(state, name="deploy.json"):
    return json.loads((state / name).read_text())


class Runner:
    def __init__(self):
        self.head = T
        self.calls = []
        self.checkout_error = False
        self.wrong_head = False
        self.install_error = False
        self.guard_aware = True

    def __call__(self, cmd, **kwargs):
        self.calls.append((list(cmd), kwargs))
        if cmd[:2] == ["git", "rev-parse"]:
            return (self.head + "\n").encode()
        if cmd[:2] == ["git", "checkout"]:
            if self.checkout_error:
                raise subprocess.CalledProcessError(1, cmd, output=TOKEN.encode())
            self.head = P[:7] + "f" * 33 if self.wrong_head else P
        if cmd[:3] == ["git", "cat-file", "-e"] and not self.guard_aware:
            raise subprocess.CalledProcessError(128, cmd, output=b"missing fixture file")
        if "install" in cmd and self.install_error:
            raise subprocess.CalledProcessError(1, cmd, output=TOKEN.encode())
        return b""


@pytest.fixture
def harness(tmp_path):
    contexts = []

    def make(*, rec=None, history=(), now=1000.0, runner=None, sender=None, settings=None):
        g = guard_module()
        root = tmp_path / f"case-{len(contexts)}"
        root.mkdir()
        repo = root / "repo"
        repo.mkdir()
        state = root / "data" / "deploy"
        state.mkdir(mode=0o700, parents=True)
        if rec is not None:
            save(state, "deploy.json", record(repo, **rec))
        if history:
            path = state / "starts.jsonl"
            path.write_text("".join(json.dumps(row) + "\n" for row in history))
            path.chmod(0o600)
        clock = [now]
        waits, alerts = [], []
        runner = runner or Runner()
        ctx = g.begin_start(
            str(repo),
            state,
            clock=lambda: clock[0],
            sleep=waits.append,
            runner=runner,
            sender=sender or (lambda directory, text: alerts.append(text) or True),
            host="fixture-node",
            log_path="fixture.log",
            settings=settings or g.GuardSettings(settle_seconds=0.001),
        )
        contexts.append(ctx)
        return SimpleNamespace(
            g=g,
            ctx=ctx,
            repo=repo,
            state=state,
            clock=clock,
            waits=waits,
            alerts=alerts,
            runner=runner,
        )

    yield make
    for ctx in contexts:
        ctx.close()


@pytest.mark.parametrize(
    "changes,head,enabled,expected",
    [
        ({}, T, False, "ordinary"),
        (None, T, True, "ordinary"),
        ({"schema": 2}, T, True, "ordinary"),
        ({"repo_dir": "/fixture/other"}, T, True, "ordinary"),
        ({}, P, True, "stale"),
        ({}, T, True, "attempt"),
        ({"attempts": [{}, {}]}, T, True, "attempt"),
        ({"attempts": [{}, {}, {}]}, T, True, "rollback"),
        ({"state": "rolling_back", "rollback": {"tries": 1}}, T, True, "retry_rollback"),
        ({"state": "rolling_back", "rollback": {"tries": 2}}, T, True, "rollback_failed"),
        ({"state": "rolled_back"}, P, True, "post_rollback"),
        ({"state": "rollback_failed"}, P, True, "delay"),
        ({"state": "confirmed"}, T, True, "ordinary"),
        ({"state": "stale"}, T, True, "ordinary"),
        ({"state": "reported"}, T, True, "ordinary"),
    ],
    ids=[
        "off",
        "absent",
        "schema",
        "foreign",
        "moved",
        "first",
        "last-allowed",
        "threshold",
        "retry",
        "retry-exhausted",
        "previous",
        "checkout-failed",
        "confirmed",
        "stale",
        "reported",
    ],
)
def test_start_decision_table(tmp_path, changes, head, enabled, expected):
    g = guard_module()
    repo = str(tmp_path / "repo")
    rec = None if changes is None else record(repo, **changes)
    frozen = json.dumps(rec, sort_keys=True)
    assert g.decide(rec, head, repo, enabled) == expected
    assert json.dumps(rec, sort_keys=True) == frozen


def test_stale_and_foreign_records_never_checkout(harness):
    moved = harness(rec={})
    moved.runner.head = P
    moved.ctx.activate()
    assert load(moved.state)["state"] == "stale"
    foreign = harness(rec={"repo_dir": "/fixture/other"})
    before = (foreign.state / "deploy.json").read_bytes()
    foreign.ctx.activate()
    assert (foreign.state / "deploy.json").read_bytes() == before
    assert not any("checkout" in cmd for h in [moved, foreign] for cmd, _ in h.runner.calls)


@pytest.mark.parametrize(
    "equal,deps_error,legacy",
    [
        (False, False, False),
        (False, True, False),
        (True, False, False),
        (False, False, True),
    ],
)
async def test_rollback_persists_and_never_builds_frontend(harness, equal, deps_error, legacy):
    runner = Runner()
    runner.install_error, runner.guard_aware = deps_error, not legacy
    h = harness(
        rec={
            "attempts": [{}, {}, {}],
            "deps_rebuilt": True,
            "previous_ref": T if equal else P,
            "forced_reset": True,
        },
        runner=runner,
    )
    if equal:
        runner.head = T
    with pytest.raises(SystemExit) as ended:
        h.ctx.activate()
    assert ended.value.code == 75
    current = load(h.state)
    assert current["state"] == "rolled_back"
    assert bool(current["rollback"].get("deps_error")) is deps_error
    checkouts = [cmd for cmd, _ in runner.calls if cmd[:2] == ["git", "checkout"]]
    assert len(checkouts) == (0 if equal else 1)
    installs = [(cmd, kw) for cmd, kw in runner.calls if "install" in cmd]
    assert len(installs) == 1 and installs[0][1]["timeout"] == 300
    assert not any(any(word in cmd for word in ["npm", "build", "npx"]) for cmd, _ in runner.calls)
    assert any(
        cmd == ["git", "cat-file", "-e", f"{T if equal else P}:src/pinky_daemon/deploy_guard.py"]
        for cmd, _ in runner.calls
    )
    assert len(h.alerts) == int(legacy)
    if legacy:
        assert "starting" in h.alerts[0] and "not confirm" in h.alerts[0]
    else:
        owner = AsyncMock(return_value=True)
        await h.ctx.ready(owner_notify=owner)
        owner.assert_awaited_once()
        assert ("dependencies" in owner.call_args.args[0].lower()) is deps_error
        assert "discarded" in owner.call_args.args[0].lower()
        assert load(h.state)["state"] == "reported"
    assert TOKEN not in json.dumps(current) + " ".join(h.alerts)


@pytest.mark.parametrize("failure", ["checkout", "full-ref"])
def test_failed_checkout_reports_once_and_continues(harness, failure):
    runner = Runner()
    runner.checkout_error, runner.wrong_head = failure == "checkout", failure == "full-ref"
    h = harness(rec={"attempts": [{}, {}, {}]}, runner=runner)
    h.ctx.activate()
    assert load(h.state)["state"] == "rollback_failed"
    assert len(h.alerts) == 1 and "did not work" in h.alerts[0]
    h.ctx.activate()
    assert len(h.alerts) == 1
    assert TOKEN not in h.g.guard_status(str(h.repo), h.state)["last_error"]


@pytest.mark.parametrize("tries,expected", [(1, "rolled_back"), (2, "rollback_failed")])
def test_interrupted_rollback_has_one_retry(harness, tries, expected):
    h = harness(rec={"state": "rolling_back", "rollback": {"tries": tries}})
    if tries == 1:
        with pytest.raises(SystemExit):
            h.ctx.activate()
    else:
        h.ctx.activate()
    assert load(h.state)["state"] == expected
    assert len(h.alerts) == int(tries == 2)


@pytest.mark.parametrize("switch", ["file", "environment"])
def test_off_start_only_records_one_start(harness, monkeypatch, switch):
    h = harness(rec={"attempts": [{}, {}, {}]})
    # Switches are evaluated before actions, including a change after entry.
    if switch == "file":
        (h.state / "guard.off").touch()
    else:
        monkeypatch.setenv("PINKY_DEPLOY_GUARD", "0")
    before = (h.state / "deploy.json").read_bytes()
    h.ctx.activate()
    assert (h.state / "deploy.json").read_bytes() == before
    assert len((h.state / "starts.jsonl").read_text().splitlines()) == 1
    assert not h.alerts and not h.waits
    assert not any("checkout" in cmd for cmd, _ in h.runner.calls)


@pytest.mark.parametrize("problem", ["json", "directory", "git"])
def test_guard_trouble_continues_and_status_is_safe(harness, caplog, problem):
    h = harness()
    if problem == "json":
        (h.state / "deploy.json").write_text("{invalid")
    elif problem == "directory":
        h.state.chmod(0o755)
    else:

        def missing(*args, **kwargs):
            raise FileNotFoundError(TOKEN)

        h.ctx.runner = missing
    caplog.clear()
    with caplog.at_level(logging.ERROR):
        h.ctx.activate()
        status = h.g.guard_status(str(h.repo), h.state)
    assert status["last_error"]
    assert len([r for r in caplog.records if "ERROR deploy guard:" in r.message]) == 1
    assert TOKEN not in caplog.text + json.dumps(status)
    h.state.chmod(0o700)


@pytest.mark.parametrize(
    "key",
    [
        "PINKY_DEPLOY_MAX_ATTEMPTS",
        "PINKY_DEPLOY_START_DEADLINE_SEC",
        "PINKY_DEPLOY_SETTLE_SEC",
        "PINKY_DEPLOY_LOOP_STARTS",
        "PINKY_DEPLOY_LOOP_WINDOW_SEC",
    ],
)
@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf", "invalid"])
def test_settings_reject_nonpositive_or_nonfinite(key, value, caplog):
    g = guard_module()
    with caplog.at_level(logging.WARNING):
        actual = g.load_settings({key: value})
    assert actual == g.GuardSettings()
    assert len(caplog.records) == 1


def test_default_deadline_matches_listener_cap_by_value():
    from pinky_daemon.api_readiness import DEFAULT_CAP_SECONDS

    g = guard_module()
    assert g.GuardSettings().start_deadline_seconds == DEFAULT_CAP_SECONDS == 600.0
    assert g.load_settings({"PINKY_DEPLOY_MAX_ATTEMPTS": "4"}).max_attempts == 4


@pytest.mark.parametrize("mode,allowed", [(0o600, True), (0o640, False), (0o604, False)])
def test_route_file_permissions(tmp_path, mode, allowed, caplog):
    g = guard_module()
    state = tmp_path / "deploy"
    g.refresh_alert_route(
        state,
        [
            dict(
                platform="telegram",
                account_id="fixture-account",
                conversation_id="fixture-chat",
                token=TOKEN,
            )
        ],
    )
    assert state.stat().st_mode & 0o777 == 0o700
    path = state / "alert-route.json"
    assert path.stat().st_mode & 0o777 == 0o600
    path.chmod(mode)
    with caplog.at_level(logging.ERROR):
        route = g.read_alert_route(state)
    assert bool(route) is allowed
    assert TOKEN not in caplog.text


@pytest.mark.parametrize("bad", ["directory", "owner", "symlink"])
def test_unsafe_route_is_refused(tmp_path, bad, caplog):
    g = guard_module()
    state = tmp_path / "deploy"
    g.refresh_alert_route(
        state,
        [
            dict(
                platform="telegram",
                account_id="fixture-account",
                conversation_id="fixture-chat",
                token=TOKEN,
            )
        ],
    )
    expected_uid = None
    if bad == "directory":
        state.chmod(0o755)
    elif bad == "owner":
        expected_uid = os.getuid() + 1
    else:
        original = state / "route-copy.json"
        (state / "alert-route.json").rename(original)
        (state / "alert-route.json").symlink_to(original)
    assert g.read_alert_route(state, expected_uid=expected_uid) is None
    assert TOKEN not in caplog.text
    state.chmod(0o700)


def test_route_refresh_removes_empty_and_marks_unsupported(tmp_path, caplog):
    g = guard_module()
    state = tmp_path / "deploy"
    routes = [
        dict(
            platform="other",
            account_id="fixture-account",
            conversation_id="fixture-chat",
            token=TOKEN,
        )
    ]
    with caplog.at_level(logging.WARNING):
        g.refresh_alert_route(state, routes, clock=lambda: 1000.0)
    assert load(state, "alert-route.json")["routes"][0]["supported"] is False
    status = g.guard_status(str(tmp_path), state)["alert_route"]
    assert set(status) == {"present", "platforms", "written_at"}
    assert status["platforms"] == [{"platform": "other", "supported": False}]
    assert status["written_at"] == 1000.0
    assert len(caplog.records) == 1 and TOKEN not in caplog.text + json.dumps(status)
    g.refresh_alert_route(state, [], clock=lambda: 1001.0)
    assert not (state / "alert-route.json").exists()


def test_alert_timeout_has_one_attempt_and_no_secret(tmp_path, caplog):
    g = guard_module()
    state = tmp_path / "deploy"
    route = dict(
        platform="telegram",
        account_id="fixture-account",
        conversation_id="fixture-chat",
        token=TOKEN,
    )
    g.refresh_alert_route(state, [route, dict(route, conversation_id="fixture-chat-2")])
    calls = []

    def timeout(request, **kwargs):
        calls.append(kwargs)
        raise TimeoutError("https://fixture.invalid/bot" + TOKEN)

    with caplog.at_level(logging.ERROR):
        assert g.send_alert(state, "fixture alert", opener=timeout) is False
    assert calls == [{"timeout": 10}]
    assert "alert delivery unknown" in caplog.text
    assert TOKEN not in caplog.text and "https://fixture.invalid" not in caplog.text
    assert TOKEN not in json.dumps(g.guard_status(str(tmp_path), state))


@pytest.mark.parametrize("served,old", [(False, False), (True, False), (False, True)])
async def test_incident_window_suppression_wait_and_recovery(harness, served, old):
    history = [dict(ts=990.0 - n, pid=n + 1, ref=P) for n in range(5, 0, -1)]
    if served:
        history.append(dict(ts=997.0, pid=5, serving=True))
    if old:
        history[0]["ts"] = 1.0
    h = harness(history=history)
    h.ctx.activate()
    assert len(h.alerts) == int(not served and not old)
    assert h.waits == ([5] if not served and not old else [])
    if served or old:
        return
    assert load(h.state, "incident.json")["open"] is True
    h.ctx.activate()
    assert len(h.alerts) == 1
    owner = AsyncMock(return_value=True)
    await h.ctx.ready(owner_notify=owner)
    assert load(h.state, "incident.json")["open"] is False
    assert "serving again" in owner.call_args.args[0]


def test_incident_timeout_stays_open_without_same_start_resend(harness, caplog):
    calls = []

    def sender(state, text):
        calls.append(text)
        g = guard_module()
        return g.send_alert(
            state, text, opener=lambda *a, **k: (_ for _ in ()).throw(TimeoutError(TOKEN))
        )

    history = [dict(ts=990.0 + n, pid=n + 1, ref=P) for n in range(5)]
    h = harness(history=history, sender=sender)
    h.g.refresh_alert_route(
        h.state,
        [
            dict(
                platform="telegram",
                account_id="fixture-account",
                conversation_id="fixture-chat",
                token=TOKEN,
            )
        ],
    )
    h.ctx.activate()
    h.ctx.activate()
    assert len(calls) == 1
    assert load(h.state, "incident.json")["open"] is True
    assert "alert delivery unknown" in caplog.text and TOKEN not in caplog.text


@pytest.mark.parametrize("count,expected", [(5, 5), (6, 10), (7, 20), (8, 40), (9, 60), (30, 60)])
def test_failed_start_delay_formula(harness, count, expected):
    h = harness(history=[dict(ts=950.0 + n, pid=n + 1, ref=P) for n in range(count)])
    h.ctx.activate()
    assert h.waits == [expected]


@pytest.mark.parametrize("busy", [True, False])
def test_lock_contention_is_neutral_even_at_attempt_threshold(harness, busy):
    from pinky_daemon.store_authority import StoreAuthorityError

    h = harness(
        rec={"attempts": [{}, {}, {}]} if busy else None,
        history=[dict(ts=950.0 + n, pid=n + 1, ref=P) for n in range(4)],
    )
    before = (h.state / "deploy.json").read_bytes() if busy else None
    exc = StoreAuthorityError("daemon store authority is already held" if busy else "other failure")
    h.ctx.failed(exc)
    lines = [json.loads(line) for line in (h.state / "starts.jsonl").read_text().splitlines()]
    if busy:
        assert set(lines[-1]) == {"ts", "pid", "lock_busy"}
        assert lines[-1]["lock_busy"] is True
        assert (h.state / "deploy.json").read_bytes() == before
        assert not (h.state / "last_failure.json").exists()
    else:
        assert "lock_busy" not in lines[-1]
        assert load(h.state, "last_failure.json")["exc_type"] == "StoreAuthorityError"
    assert not h.alerts and not any("checkout" in cmd for cmd, _ in h.runner.calls)


async def test_ready_does_not_confirm_a_later_deploy(harness):
    h = harness()
    h.ctx.activate()
    save(h.state, "deploy.json", record(h.repo, created_at=1001.0, deploy_id="d" * 32))
    h.clock[0] = 1002.0
    await h.ctx.ready()
    assert load(h.state)["state"] == "armed"


def test_external_head_change_without_record_never_rolls_back(harness):
    h = harness()
    h.runner.head = P
    h.ctx.activate()
    assert not (h.state / "deploy.json").exists()
    assert not any("checkout" in cmd for cmd, _ in h.runner.calls)


def test_start_log_compaction_keeps_newest_two_hundred(harness):
    h = harness(history=[dict(ts=float(n), pid=n + 1, ref=P) for n in range(400)])
    lines = [json.loads(line) for line in (h.state / "starts.jsonl").read_text().splitlines()]
    assert len(lines) == 200 and lines[0]["ts"] == 201.0
    assert lines[-1]["pid"] == os.getpid()


@pytest.mark.parametrize("catalog", [False, True])
def test_failure_detail_is_only_retained_for_catalog_errors(harness, catalog):
    from pinky_daemon.store_catalog import StoreCatalogError

    h = harness()
    h.ctx.activate()
    h.ctx.failed(StoreCatalogError("fixture/path") if catalog else RuntimeError(TOKEN))
    failure = load(h.state, "last_failure.json")
    assert failure["detail"] == ("fixture/path" if catalog else "")
    assert TOKEN not in json.dumps(failure)


@pytest.mark.parametrize("project", [False, True])
def test_dependency_installer_preserves_commands_and_environment(tmp_path, project):
    g = guard_module()
    if project:
        folder = tmp_path / ".venv" / "bin"
        folder.mkdir(parents=True)
        (folder / "pip").touch()
    calls, attempted = [], []

    def runner(cmd, **kwargs):
        calls.append((list(cmd), kwargs))
        return b"fixture version"

    command = g.install_dependencies(str(tmp_path), runner, on_attempt=attempted.append)
    expected = str(tmp_path / ".venv/bin/python") if project else sys.executable
    assert command[:5] == [expected, "-B", "-m", "pip", "install"]
    assert command[5:] == ["-e", ".[all]", "--quiet"] + (
        [] if project else ["--break-system-packages"]
    )
    assert attempted == [command]
    for cmd, kwargs in calls:
        assert cmd[1] == "-B" and kwargs["env"] == dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        assert kwargs["timeout"] == (10 if cmd[-1] == "--version" else 180)
    calls.clear()
    g.install_dependencies(str(tmp_path), runner, command=command)
    assert len(calls) == 1 and calls[0][0] == command


def test_stale_transition_does_not_open_an_incident(harness):
    h = harness(rec={}, history=[dict(ts=990.0 + n, pid=n + 1, ref=P) for n in range(5)])
    h.runner.head = P
    h.ctx.activate()
    assert load(h.state)["state"] == "stale"
    assert not h.alerts and not h.waits and not (h.state / "incident.json").exists()


@pytest.mark.parametrize("elapsed,alerted", [(0, False), (21599.999, False), (21600, True)])
def test_open_incident_reminds_only_after_six_hours(harness, elapsed, alerted):
    h = harness(now=30000.0,
                history=[dict(ts=29990.0 + n, pid=n + 1, ref=P) for n in range(5)])
    save(h.state, "incident.json", dict(open=True, started_at=29990.0,
                                       last_alert_at=30000.0 - elapsed, failed_starts=5))
    h.ctx.activate()
    assert len(h.alerts) == int(alerted)
    assert load(h.state, "incident.json")["open"] is True


@pytest.mark.parametrize("early", [True, False])
async def test_ready_requires_a_surviving_settle_period(harness, early):
    g = guard_module()
    h = harness(rec={}, settings=g.GuardSettings(settle_seconds=0.01))
    h.ctx.activate()
    task = h.ctx.ready()
    if early:
        h.ctx.close()
    await asyncio.gather(task, return_exceptions=True)
    assert load(h.state)["state"] == ("armed" if early else "confirmed")
    assert len(load(h.state)["attempts"]) == 1


async def test_success_after_failed_checkout_reports_recovery_duration(harness):
    h = harness(rec={"state": "rollback_failed", "rollback": {"tries": 2}})
    h.ctx.activate()
    owner = AsyncMock(return_value=True)
    await h.ctx.ready(owner_notify=owner)
    assert load(h.state)["state"] == "reported"
    assert "serving again" in owner.call_args.args[0]
    assert "over" in owner.call_args.args[0]
