"""Aggregate shutdown budgets must account for workers after their first share."""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

from pinky_daemon import store_shutdown
from tests._gc_quiet import gc_quiet

_DEADLINE = 0.9
_EPSILON = 0.3
_REASON = "aggregate deadline share exceeded; store never finalized"


def _observe_workers(monkeypatch, on_join=None):
    workers = {}
    joins = {}

    class ObservedThread(threading.Thread):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            key = self.name.removeprefix("store-shutdown-")
            workers[key] = self
            joins[key] = []
            self.key = key

        def join(self, timeout=None):
            started = time.monotonic()
            try:
                if on_join is not None:
                    on_join(self.key, len(joins[self.key]) + 1)
                return super().join(timeout)
            finally:
                joins[self.key].append((timeout, time.monotonic() - started))

    monkeypatch.setattr(store_shutdown, "threading", SimpleNamespace(Thread=ObservedThread))
    return workers, joins


def _shutdown_bounded(coordinator, releases, workers):
    """Bound the test itself and release every blocked callback even on failure."""
    done = threading.Event()
    result = {}

    def run():
        started = time.monotonic()
        try:
            result["report"] = coordinator.shutdown()
        except store_shutdown.StoreShutdownError as exc:
            result["error"] = exc
            result["report"] = exc.report
        except BaseException as exc:
            result["unexpected"] = exc
        finally:
            result["elapsed"] = time.monotonic() - started
            done.set()

    controller = threading.Thread(target=run, daemon=True)
    with gc_quiet():
        controller.start()
        try:
            completed = done.wait(_DEADLINE + _EPSILON)
        finally:
            for event in releases:
                event.set()
            controller.join(2)
            for worker in workers.values():
                threading.Thread.join(worker, 2)
        assert completed, "shutdown exceeded its aggregate deadline while callbacks stayed blocked"
        assert result["elapsed"] <= _DEADLINE + _EPSILON
    assert not controller.is_alive()
    assert all(not worker.is_alive() for worker in workers.values())
    if "unexpected" in result:
        raise result["unexpected"]
    return result


def _finish_during_rejoin(monkeypatch, *, raises=False):
    release = threading.Event()

    def on_join(key, call):
        if key == "straggler" and call == 2:
            release.set()

    workers, joins = _observe_workers(monkeypatch, on_join)

    def straggler():
        release.wait()
        if raises:
            raise ValueError("failure during rejoin")

    coordinator = store_shutdown.StoreShutdownCoordinator(deadline_seconds=_DEADLINE)
    coordinator.register("straggler", "telemetry", straggler)
    coordinator.register("later", "delivery", lambda: None)
    result = _shutdown_bounded(coordinator, [release], workers)
    return result, joins["straggler"]


def test_straggler_finishing_during_its_rejoin_is_finalized(monkeypatch):
    result, joins = _finish_during_rejoin(monkeypatch)

    assert "error" not in result, "a worker finishing during its rejoin must be finalized"
    assert result["report"].attempted == ("straggler", "later")
    assert result["report"].finalized == ("straggler", "later")
    assert result["report"].failures == ()
    assert len(joins) == 2, "the worker must receive exactly one rejoin"
    timeout, elapsed = joins[1]
    assert timeout is not None and 0 <= elapsed < timeout, (
        "the rejoin must return on worker completion before its timeout"
    )


def test_straggler_raising_during_its_rejoin_reports_callback_error(monkeypatch):
    result, joins = _finish_during_rejoin(monkeypatch, raises=True)

    assert "error" in result, "the callback error must escape shutdown"
    assert result["report"].attempted == ("straggler", "later")
    assert result["report"].finalized == ("later",)
    assert [(failure.logical_name, failure.reason) for failure in result["report"].failures] == [
        ("straggler", "ValueError: failure during rejoin")
    ], "a worker raising during its rejoin must report its callback error"
    assert len(joins) == 2, "the worker must receive exactly one rejoin"
    timeout, elapsed = joins[1]
    assert timeout is not None and 0 <= elapsed < timeout, (
        "the rejoin must return on worker completion before its timeout"
    )


def test_store_finishing_after_two_shares_is_finalized_in_order(monkeypatch):
    workers, joins = _observe_workers(monkeypatch)
    release_first, release_second = threading.Event(), threading.Event()
    duration = {}

    def first():
        started = time.monotonic()
        release_first.wait()
        duration["first"] = time.monotonic() - started

    def second():
        release_second.wait()

    def last():
        # Reaching this callback proves both preceding fair-share joins returned.
        release_first.set()
        release_second.set()
        threading.Thread.join(workers["first"], 1)
        threading.Thread.join(workers["second"], 1)

    coordinator = store_shutdown.StoreShutdownCoordinator(deadline_seconds=_DEADLINE)
    coordinator.register("first", "telemetry", first)
    coordinator.register("second", "memory", second)
    coordinator.register("last", "delivery", last)
    result = _shutdown_bounded(coordinator, [release_first, release_second], workers)

    assert duration["first"] >= 1.8 * joins["first"][0][0], "first worker must span two shares"
    assert duration["first"] < _DEADLINE, "completion must leave aggregate budget available"
    assert "error" not in result, "a completed straggler must not remain a timeout failure"
    assert result["report"].attempted == ("first", "second", "last")
    assert result["report"].finalized == ("first", "second", "last")
    assert result["report"].failures == ()


def test_hung_store_stays_bounded_and_does_not_block_later_stores(monkeypatch):
    workers, _joins = _observe_workers(monkeypatch)
    release = threading.Event()
    attempted = []

    def blocked():
        attempted.append("blocked")
        release.wait()

    def later():
        attempted.append("later")

    coordinator = store_shutdown.StoreShutdownCoordinator(deadline_seconds=_DEADLINE)
    coordinator.register("blocked", "telemetry", blocked)
    coordinator.register("later", "delivery", later)
    result = _shutdown_bounded(coordinator, [release], workers)

    assert attempted == ["blocked", "later"]
    assert result["report"].attempted == ("blocked", "later")
    assert result["report"].finalized == ("later",)
    assert [(failure.logical_name, failure.reason) for failure in result["report"].failures] == [
        ("blocked", _REASON)
    ]


def test_straggler_callback_error_replaces_share_timeout(monkeypatch):
    workers, _joins = _observe_workers(monkeypatch)
    release = threading.Event()

    def late_error():
        release.wait()
        raise ValueError("late finalization failure")

    def later():
        release.set()
        threading.Thread.join(workers["late"], 1)

    coordinator = store_shutdown.StoreShutdownCoordinator(deadline_seconds=_DEADLINE)
    coordinator.register("late", "telemetry", late_error)
    coordinator.register("later", "delivery", later)
    result = _shutdown_bounded(coordinator, [release], workers)

    assert result["report"].finalized == ("later",)
    assert [(failure.logical_name, failure.reason) for failure in result["report"].failures] == [
        ("late", "ValueError: late finalization failure")
    ]


def test_second_straggler_cannot_receive_a_new_budget_after_first_rejoin(monkeypatch):
    workers, joins = _observe_workers(monkeypatch)
    release_first, release_second = threading.Event(), threading.Event()
    coordinator = store_shutdown.StoreShutdownCoordinator(deadline_seconds=_DEADLINE)
    coordinator.register("first", "telemetry", release_first.wait)
    coordinator.register("second", "memory", release_second.wait)
    coordinator.register("later", "delivery", lambda: None)
    result = _shutdown_bounded(coordinator, [release_first, release_second], workers)

    assert result["report"].attempted == ("first", "second", "later")
    assert result["report"].finalized == ("later",)
    assert [(failure.logical_name, failure.reason) for failure in result["report"].failures] == [
        ("first", _REASON),
        ("second", _REASON),
    ]
    assert len(joins["first"]) == 2, "the first straggler must receive the remaining aggregate time"
    assert joins["first"][1][0] is not None and joins["first"][1][0] > 0
    # The first re-join consumed the remaining deadline. The second may be
    # inspected without joining, or polled with zero timeout, but not waited on.
    assert all(timeout == 0 for timeout, _elapsed in joins["second"][1:])
