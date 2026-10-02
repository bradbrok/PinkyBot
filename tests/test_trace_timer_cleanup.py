"""A pre-start test failure must not be masked by joining an unstarted timer."""

from types import SimpleNamespace

import pytest

from tests import test_schedule_fire_trace_isolation as trace_tests


async def test_trace_read_failure_before_timer_start_preserves_error_and_closes_resources(
    monkeypatch, tmp_path
):
    from pinky_daemon import api

    class BeforeTimerStartError(RuntimeError):
        pass

    primary = BeforeTimerStartError("failure before timer start")
    closed = []

    class Connection:
        def execute(self, sql):
            assert sql == "BEGIN EXCLUSIVE"
            raise primary

        def rollback(self):
            pass

        def close(self):
            closed.append("connection")

    registry = SimpleNamespace(
        register=lambda *args, **kwargs: None,
        _fire_trace=SimpleNamespace(path=tmp_path / "unused.db"),
        close=lambda: closed.append("registry"),
    )
    monkeypatch.setattr(
        api, "create_api", lambda **kwargs: SimpleNamespace(state=SimpleNamespace(agents=registry))
    )
    monkeypatch.setattr(trace_tests, "fire", lambda registry: None)
    monkeypatch.setattr(
        trace_tests, "sqlite3", SimpleNamespace(connect=lambda *args, **kwargs: Connection())
    )

    try:
        await trace_tests.test_trace_read_endpoint_must_not_block_event_loop(tmp_path, "status")
    except BaseException as exc:
        actual = exc
    else:
        pytest.fail("the injected pre-start failure must escape the test")

    assert (actual, closed) == (primary, ["connection", "registry"]), (
        "the primary failure must surface and both resources must close; "
        f"observed error={actual!r}, closed={closed!r}"
    )


@pytest.mark.parametrize("cleanup_failure", ["join", "connection-close"])
async def test_trace_read_cleanup_failure_still_closes_both_resources(
    monkeypatch, tmp_path, cleanup_failure
):
    from pinky_daemon import api

    primary = RuntimeError("injected operation failure")
    secondary = RuntimeError("injected cleanup failure")
    closed = []

    class Connection:
        def execute(self, sql):
            if cleanup_failure == "connection-close":
                raise primary

        def rollback(self):
            pass

        def close(self):
            closed.append("connection")
            if cleanup_failure == "connection-close":
                raise secondary

    class Timer:
        ident = None

        def __init__(self, *args):
            pass

        def start(self):
            # Model an operation failure after the timer acquired an identity;
            # the cleanup path must attempt its join, which fails independently.
            self.ident = 1
            raise primary

        def join(self):
            raise secondary

    registry = SimpleNamespace(
        register=lambda *args, **kwargs: None,
        _fire_trace=SimpleNamespace(path=tmp_path / "unused.db"),
        close=lambda: closed.append("registry"),
    )
    monkeypatch.setattr(
        api, "create_api", lambda **kwargs: SimpleNamespace(state=SimpleNamespace(agents=registry))
    )
    monkeypatch.setattr(trace_tests, "fire", lambda registry: None)
    monkeypatch.setattr(
        trace_tests, "sqlite3", SimpleNamespace(connect=lambda *args, **kwargs: Connection())
    )
    monkeypatch.setattr(trace_tests, "threading", SimpleNamespace(Timer=Timer))

    try:
        await trace_tests.test_trace_read_endpoint_must_not_block_event_loop(tmp_path, "status")
    except BaseException as exc:
        actual = exc
    else:
        pytest.fail("the injected cleanup failure must escape the test")

    assert (actual, closed) == (secondary, ["connection", "registry"]), (
        "a cleanup failure must not skip subsequent resource cleanup; "
        f"observed error={actual!r}, closed={closed!r}"
    )
