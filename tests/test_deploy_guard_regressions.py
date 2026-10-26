"""Arming refuses unusable state while startup and recovery keep their contracts."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import stat
import sys
import urllib.error
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tests.test_deploy_guard import TOKEN, P, Runner, T, guard_module, load, record, save
from tests.test_deploy_guard import harness as _harness
from tests.test_startup_preflight import clean_report
from tests.test_startup_preflight import deployment as _deployment

harness = _harness
deployment = _deployment


@pytest.mark.parametrize("deps", [False, True], ids=["plain", "deps"])
@pytest.mark.parametrize(
    "condition",
    [
        "healthy",
        "off",
        "env-off",
        "wide",
        "unreadable",
        "unsearchable",
        "fifo",
        "symlink",
        "directory",
    ],
)
async def test_unusable_guard_state_uses_update_undo(deployment, monkeypatch, condition, deps):
    fx = deployment
    fx.set_target(report=clean_report(fx.data))
    state = fx.data / "deploy"
    state.mkdir(mode=0o700)
    switch = state / "guard.off"
    mode = {"wide": 0o755, "unreadable": 0, "unsearchable": 0o600}.get(condition, 0o700)
    if condition == "off":
        switch.touch(mode=0o644)
    elif condition == "env-off":
        mode = 0o755
        monkeypatch.setenv("PINKY_DEPLOY_GUARD", "0")
    elif condition == "fifo":
        os.mkfifo(switch, 0o600)
    elif condition == "symlink":
        target = fx.data / "fixture-switch-target"
        target.write_text("fixture")
        switch.symlink_to(target)
    elif condition == "directory":
        switch.mkdir(mode=0o700)
    state.chmod(mode)
    try:
        result = await fx.update(force_deps=deps)
        assert stat.S_IMODE(state.lstat().st_mode) == mode, "arming must not repair state"
        state.chmod(0o700)
        if condition in {"healthy", "off", "env-off"}:
            assert fx.scheduled and fx.git("rev-parse", "HEAD") == fx.target
            assert result["guard"]["armed"] is (condition == "healthy")
            if condition != "healthy":
                assert result["guard"]["reason"] == "switched off"
            return
        assert result["guard"] == {"armed": False, "reason": "state_unusable"}
        assert result["error"] == "deploy guard could not be armed"
        assert result["reason"] == "state_unusable" and result["undo"]["ok"]
        assert result["undo"]["deps_restored"] is (True if deps else None)
        assert fx.git("rev-parse", "HEAD") == fx.previous
        assert not fx.scheduled and not (state / "deploy.json").exists()
        assert TOKEN not in json.dumps(result)
        if deps:
            assert [ref for ref, _ in fx.installs] == [fx.target, fx.previous]
    finally:
        state.chmod(0o700)


@pytest.mark.parametrize(
    "entry", ["guard.off", "alert-route.json", ".guard.lock", "deploy.json", "temp"]
)
def test_arming_reports_state_helper_errors(tmp_path, monkeypatch, caplog, entry):
    g = guard_module()
    root = tmp_path / "data"
    root.mkdir(mode=0o700)
    state = root / "deploy"
    original = g._open_state

    @contextlib.contextmanager
    def refused(directory, name, *args, **kwargs):
        if name == entry or (entry == "temp" and name.startswith(".guard-")):
            raise PermissionError(TOKEN)
        with original(directory, name, *args, **kwargs) as fd:
            yield fd

    monkeypatch.setattr(g, "_open_state", refused)
    result = g.arm_deploy(
        str(tmp_path),
        state,
        previous_ref=P,
        target_ref=T,
        previous_label="previous",
        target_label="target",
        deps_rebuilt=False,
        forced_reset=False,
        routes=[
            dict(
                platform="telegram",
                account_id="fixture-account",
                conversation_id="fixture-chat",
                token=TOKEN,
            )
        ],
    )
    assert result == {"armed": False, "reason": "state_unusable"}
    assert not (state / "deploy.json").exists()
    assert TOKEN not in caplog.text


@pytest.mark.parametrize("kind", ["directory-owner", "switch-owner"])
def test_arming_refuses_foreign_state_without_repair(tmp_path, monkeypatch, kind):
    g = guard_module()
    root = tmp_path / "data"
    state = root / "deploy"
    state.mkdir(mode=0o700, parents=True)
    if kind == "switch-owner":
        (state / "guard.off").touch(mode=0o600)
        real_fstat = g.os.fstat

        def foreign(fd):
            fields = list(real_fstat(fd))
            fields[4] += 1
            return os.stat_result(fields)

        monkeypatch.setattr(g.os, "fstat", foreign)
    else:
        real_directory = g._directory

        def foreign_directory(directory, **kwargs):
            return real_directory(directory, expected_uid=os.getuid() + 1)

        monkeypatch.setattr(g, "_directory", foreign_directory)
    result = g.arm_deploy(
        str(tmp_path),
        state,
        previous_ref=P,
        target_ref=T,
        previous_label="previous",
        target_label="target",
        deps_rebuilt=False,
        forced_reset=False,
    )
    assert result == {"armed": False, "reason": "state_unusable"}
    assert stat.S_IMODE(state.lstat().st_mode) == 0o700
    assert not (state / "deploy.json").exists()


@pytest.mark.parametrize(
    "cause",
    [
        "runtime",
        "systemexit",
        "keyboardinterrupt",
        "dotenv-error",
        "dotenv-systemexit",
        "dotenv-interrupt",
        "bad-port",
        "unknown-option",
        "missing-db-value",
        "missing-mode-value",
        "parse-interrupt",
    ],
)
def test_api_base_exits_record_one_start_and_preserve_exception(tmp_path, monkeypatch, cause):
    from pinky_daemon import __main__ as entry

    g = guard_module()
    root = tmp_path / "data"
    root.mkdir(mode=0o700)
    arguments = ["pinky", "--mode", "api", "--db-path", str(root / "db")]
    additions = {
        "bad-port": ["--port", "invalid"],
        "unknown-option": ["--unknown-option"],
        "missing-db-value": ["--db-path"],
        "missing-mode-value": ["--mode"],
    }
    arguments += additions.get(cause, [])
    monkeypatch.setattr(sys, "argv", arguments)
    monkeypatch.setattr(entry, "_install_faulthandler", lambda: None)
    monkeypatch.setattr(g.socket, "gethostname", lambda: "fixture-node")
    failure = (
        KeyboardInterrupt
        if cause in {"keyboardinterrupt", "dotenv-interrupt", "parse-interrupt"}
        else SystemExit
        if cause in {"systemexit", "dotenv-systemexit"}
        else RuntimeError
    )(TOKEN)

    def stopped(*args):
        raise failure

    parsed_errors = []
    real_parse = entry.argparse.ArgumentParser.parse_args

    def parse(parser, *args, **kwargs):
        try:
            if cause == "parse-interrupt":
                raise failure
            return real_parse(parser, *args, **kwargs)
        except BaseException as exc:
            parsed_errors.append(exc)
            raise

    monkeypatch.setattr(entry.argparse.ArgumentParser, "parse_args", parse)
    monkeypatch.setattr(
        entry, "_load_dotenv", stopped if cause.startswith("dotenv-") else lambda: None
    )
    monkeypatch.setattr(entry, "_run_api", stopped)
    with pytest.raises(BaseException) as caught:
        entry.main()
    assert caught.value is (parsed_errors[0] if parsed_errors else failure)
    rows = [json.loads(line) for line in (root / "deploy/starts.jsonl").read_text().splitlines()]
    assert len(rows) == 1 and not rows[0].get("pending")
    recorded = load(root / "deploy", "last_failure.json")
    assert recorded["exc_type"] == type(caught.value).__name__ and recorded["detail"] == ""


@pytest.mark.parametrize(
    "arguments,expected",
    [
        ([], ("api", "data/conversations.db")),
        (["--mode", "poll", "--mode", "api", "--mode"], ("api", "data/conversations.db")),
        (["--db-path", "first", "--db-path", "last", "--db-path"], ("api", "last")),
        (["--mode", "poll", "--mode"], ("poll", "data/conversations.db")),
        (["--db-path", "--mode", "api"], ("api", "data/conversations.db")),
        (["--mode=api", "--db-path=fixture-db"], ("api", "fixture-db")),
        (["--mode", "poll", "--", "--mode", "api"], ("poll", "data/conversations.db")),
    ],
)
def test_fallback_argument_scan_is_total_and_last_value_wins(monkeypatch, arguments, expected):
    from pinky_daemon import __main__ as entry

    monkeypatch.setattr(sys, "argv", ["pinky", *arguments])
    recovered = entry._minimal_start_args()
    assert (recovered.mode, recovered.db_path) == expected


def test_fallback_and_real_parser_share_defaults(monkeypatch):
    from pinky_daemon import __main__ as entry

    monkeypatch.setattr(sys, "argv", ["pinky"])
    fallback = entry._minimal_start_args()
    captured = []
    monkeypatch.setattr(entry, "_install_faulthandler", lambda: None)
    monkeypatch.setattr(entry, "_load_dotenv", lambda: None)
    monkeypatch.setattr(entry, "_run_api", lambda args: None)

    def capture(args):
        captured.append((args.mode, args.db_path))
        return SimpleNamespace(close=lambda: None)

    monkeypatch.setattr(entry, "_start_api_guard", capture)
    entry.main()
    assert captured == [(fallback.mode, fallback.db_path)]


@pytest.mark.parametrize("failure_path", ["transport", "resolution"])
async def test_recovery_owner_failure_logs_keep_exception_text_private(
    deployment, monkeypatch, failure_path
):
    from pinky_daemon import api

    fx = deployment
    g = guard_module()
    agents = fx.app.state.agents
    agents.register("sender", enabled=True)
    agents.set_token("sender", "telegram", TOKEN, settings={"account_id": "fixture-account"})
    agents.set_owner_notification_destinations(
        [
            dict(
                platform="telegram",
                account_id="fixture-account",
                conversation_id="fixture-chat",
                principal_id="fixture-owner",
            )
        ]
        if failure_path == "transport"
        else []
    )
    owner = fx.app.state.dream_runner._owner_notify_callback
    private_url = "http://127.0.0.1/bot" + TOKEN + "/sendMessage"
    monkeypatch.setattr(
        fx.app.state.broker,
        "_send_callback",
        AsyncMock(side_effect=urllib.error.URLError(private_url)),
    )
    if failure_path == "resolution":

        def refused(**kwargs):
            raise ValueError(private_url)

        monkeypatch.setattr(api, "resolve_operator_chat", refused)
    logs = []
    monkeypatch.setattr(api, "_log", logs.append)
    state = fx.data / "deploy"
    save(
        state,
        "deploy.json",
        record(
            fx.tree,
            state="rolled_back",
            attempts=[{}, {}, {}],
            rollback={"tries": 1, "deps_error": None},
        ),
    )
    runner = Runner()
    runner.head = P
    ctx = g.begin_start(
        str(fx.tree),
        state,
        runner=runner,
        sleep=lambda _: None,
        sender=lambda directory, text: True,
        host="fixture-node",
        settings=g.GuardSettings(settle_seconds=0.001),
    )
    try:
        ctx.activate()
        await ctx.ready(owner_notify=lambda text: owner("admin", text))
        assert load(state)["state"] == "reported"
        failures = [line for line in logs if "OWNER_NOTIFY_" in line and "FAILURE" in line]
        assert any("OWNER_NOTIFY_DELIVERY_FAILURE" in line for line in failures)
        assert any("OWNER_NOTIFY_TERMINAL_FAILURE" in line for line in failures)
        expected_class = "URLError" if failure_path == "transport" else "ValueError"
        assert any(expected_class in line for line in failures)
        assert TOKEN not in "\n".join(logs) and private_url not in "\n".join(logs)
        if failure_path == "transport":
            assert any("via sender 'sender'" in line for line in failures)
    finally:
        ctx.close()


def test_guard_activation_precedes_application_construction(tmp_path, monkeypatch):
    from pinky_daemon import __main__ as entry
    from pinky_daemon import store_authority

    events = []

    @contextlib.contextmanager
    def authority(db_path):
        events.append("acquired")
        yield
        events.append("released")

    monkeypatch.setattr(store_authority, "store_authority_lock", authority)
    monkeypatch.setattr(entry, "_run_api_with_authority", lambda args: events.append("application"))
    args = SimpleNamespace(
        db_path=str(tmp_path / "db"),
        _deploy_guard=SimpleNamespace(activate=lambda: events.append("guard")),
    )
    entry._run_api(args)
    assert events == ["acquired", "guard", "application", "released"]


def test_busy_rows_inside_failures_are_neutral(harness):
    rows = [dict(ts=990.0 + i, pid=100 + i, ref=P) for i in range(5)]
    rows.insert(2, dict(ts=991.5, pid=300, lock_busy=True))
    rows.insert(4, dict(ts=992.5, pid=301, lock_busy=True))
    h = harness(history=rows)
    h.ctx.activate()
    assert len(h.alerts) == 1 and load(h.state, "incident.json")["failed_starts"] == 5
    assert h.waits == [5]


@pytest.mark.parametrize("age,opens", [(599.999, True), (600.0, True), (600.001, False)])
def test_failure_window_boundary_is_inclusive(harness, age, opens):
    h = harness(history=[dict(ts=1000.0 - age + i, pid=100 + i, ref=P) for i in range(5)])
    h.ctx.activate()
    assert bool(h.alerts) is opens


async def test_ready_cancels_deadline_before_any_settle_progress(harness):
    g = guard_module()
    h = harness(rec={}, settings=g.GuardSettings(start_deadline_seconds=0.05, settle_seconds=0.15))
    exits = []
    h.ctx.exit_process = exits.append
    h.ctx.activate()
    task = h.ctx.ready()
    try:
        h.ctx.deadline_thread.join(timeout=2)
        assert not h.ctx.deadline_thread.is_alive()
        assert h.ctx.cancel.is_set() and exits == []
        assert load(h.state)["state"] == "armed"
        await task
        assert load(h.state)["state"] == "confirmed"
    finally:
        if not task.done():
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


def test_state_writer_finishes_partial_writes(tmp_path, monkeypatch):
    g = guard_module()
    root = tmp_path / "data"
    root.mkdir(mode=0o700)
    original = os.write
    writes = []

    def partial(fd, data):
        written = original(fd, data[:3])
        writes.append(written)
        return written

    monkeypatch.setattr(g.os, "write", partial)
    result = g.arm_deploy(
        str(tmp_path),
        root / "deploy",
        previous_ref=P,
        target_ref=T,
        previous_label="previous",
        target_label="target",
        deps_rebuilt=False,
        forced_reset=False,
    )
    assert result["armed"] and load(root / "deploy")["target_ref"] == T
    assert len(writes) > 10


def test_alert_sender_bounds_plain_text_request(tmp_path, caplog):
    g = guard_module()
    state = tmp_path / "deploy"
    assert g.refresh_alert_route(
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
    captured = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self, limit):
            return b'{"ok":true}'

    def opener(request, **kwargs):
        captured.append((json.loads(request.data), kwargs))
        return Response()

    text = "<plain>&" * 100
    assert g.send_alert(state, text, base_url="http://127.0.0.1", opener=opener)
    assert captured == [({"chat_id": "fixture-chat", "text": text[:500]}, {"timeout": 10})]
    assert TOKEN not in caplog.text
