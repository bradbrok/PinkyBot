"""Bounded, class-ordered finalization for daemon SQLite stores."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

_CRITICALITY_ORDER = {
    "telemetry": 0,
    "derived": 0,
    "memory": 1,
    "authority": 2,
    "authoritative": 2,
    "delivery": 3,
}


@dataclass(frozen=True, slots=True)
class StoreShutdownFailure:
    """One registered store finalizer that did not finish successfully."""

    logical_name: str
    reason: str


@dataclass(frozen=True, slots=True)
class StoreShutdownReport:
    """Structured outcome from one aggregate shutdown attempt."""

    attempted: tuple[str, ...]
    finalized: tuple[str, ...]
    failures: tuple[StoreShutdownFailure, ...]

    @property
    def ok(self) -> bool:
        return not self.failures


class StoreShutdownError(RuntimeError):
    """Raised after every finalizer was attempted and at least one failed."""

    def __init__(self, report: StoreShutdownReport) -> None:
        self.report = report
        details = ", ".join(
            f"{failure.logical_name}: {failure.reason}" for failure in report.failures
        )
        super().__init__(f"Store shutdown failed after bounded attempts: {details}")


@dataclass(frozen=True, slots=True)
class _Finalizer:
    logical_name: str
    criticality: str
    callback: Callable[[], None]
    sequence: int


class StoreShutdownCoordinator:
    """Run store finalizers in dependency order within one aggregate deadline.

    Each callback first receives a fair share of the remaining aggregate budget.
    Callbacks that exceed their shares remain on daemon threads while later stores
    are attempted, then are rejoined in order using the remaining aggregate time.
    Finalized stores and failures follow dependency/registration order, regardless
    of completion order. Only workers still alive after these waits are unfinalized.
    """

    def __init__(self, *, deadline_seconds: float) -> None:
        if deadline_seconds <= 0:
            raise ValueError("store shutdown deadline must be positive")
        self._deadline_seconds = deadline_seconds
        self._finalizers: list[_Finalizer] = []

    def register(
        self,
        logical_name: str,
        criticality: str,
        finalizer: Callable[[], None],
    ) -> None:
        if criticality not in _CRITICALITY_ORDER:
            raise ValueError(f"unknown store criticality: {criticality!r}")
        self._finalizers.append(
            _Finalizer(
                logical_name=logical_name,
                criticality=criticality,
                callback=finalizer,
                sequence=len(self._finalizers),
            )
        )

    def shutdown(self) -> StoreShutdownReport:
        ordered = sorted(
            self._finalizers,
            key=lambda item: (_CRITICALITY_ORDER[item.criticality], -item.sequence),
        )
        deadline = time.monotonic() + self._deadline_seconds
        attempted: list[str] = []
        finalized: list[str] = []
        failures: list[StoreShutdownFailure] = []
        entries: list[tuple[_Finalizer, threading.Thread, list[BaseException]]] = []
        stragglers: list[threading.Thread] = []

        for index, item in enumerate(ordered):
            attempted.append(item.logical_name)
            callback_errors: list[BaseException] = []

            def run_finalizer(
                current: _Finalizer = item,
                errors: list[BaseException] = callback_errors,
            ) -> None:
                try:
                    current.callback()
                except BaseException as exc:  # surfaced structurally on the caller
                    errors.append(exc)

            worker = threading.Thread(
                target=run_finalizer,
                name=f"store-shutdown-{item.logical_name}",
                daemon=True,
            )
            worker.start()
            remaining_stores = len(ordered) - index
            remaining_seconds = max(0.0, deadline - time.monotonic())
            worker.join(remaining_seconds / remaining_stores)
            entries.append((item, worker, callback_errors))
            if worker.is_alive():
                stragglers.append(worker)

        for worker in stragglers:
            remaining_seconds = max(0.0, deadline - time.monotonic())
            if worker.is_alive() and remaining_seconds > 0:
                worker.join(remaining_seconds)

        for item, worker, callback_errors in entries:
            if worker.is_alive():
                failures.append(
                    StoreShutdownFailure(
                        logical_name=item.logical_name,
                        reason="aggregate deadline share exceeded; store never finalized",
                    )
                )
            elif callback_errors:
                exc = callback_errors[0]
                failures.append(
                    StoreShutdownFailure(
                        logical_name=item.logical_name,
                        reason=f"{type(exc).__name__}: {exc}",
                    )
                )
            else:
                finalized.append(item.logical_name)

        report = StoreShutdownReport(
            attempted=tuple(attempted),
            finalized=tuple(finalized),
            failures=tuple(failures),
        )
        if failures:
            raise StoreShutdownError(report)
        return report
