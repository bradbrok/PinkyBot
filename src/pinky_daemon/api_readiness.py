"""Keep prompt submission behind the daemon's own listening socket."""

from __future__ import annotations

import asyncio
import math
import os
import sys
import time
from collections.abc import Awaitable, Callable

DEFAULT_CAP_SECONDS = 600.0


class DeferredPrompt(str):
    """Reserve a queue slot without consuming wake material before delivery."""

    def __new__(cls, preview: str, build: Callable[[], str]):
        value = super().__new__(cls, preview)
        value.build = build
        value.rendered = None
        return value

    def render(self) -> str:
        if self.rendered is None:
            self.rendered = self.build()
        return self.rendered


def render_prompt(prompt: str) -> str:
    return prompt.render() if isinstance(prompt, DeferredPrompt) else prompt


def defer_wake(config, build: Callable[[], str]) -> str:
    gate = getattr(config, "api_readiness", None)
    if gate is not None and gate.attached:
        return DeferredPrompt(config.wake_context or "", build)
    return build()


async def wait_for_api(config, source: str) -> bool:
    gate = getattr(config, "api_readiness", None)
    return gate is None or await gate.wait(source)


def api_allows_submission(config, source: str) -> bool:
    gate = getattr(config, "api_readiness", None)
    return gate is None or gate.can_submit(source)


class ApiReadiness:
    """One startup deadline; timeout refuses old waiters but permits late recovery."""

    def __init__(self) -> None:
        self.server = None
        self.closed = False
        self.expired = False
        self.ready = False
        self.cap_seconds = DEFAULT_CAP_SECONDS
        self.started_at = 0.0
        self.refused_count = 0
        self._ready_event = asyncio.Event()
        self._waiters: dict[asyncio.Future[bool], str] = {}
        self._monitor_task: asyncio.Task | None = None
        self._after_ready_task: asyncio.Task | None = None

    @property
    def attached(self) -> bool:
        return self.server is not None

    def attach(self, server) -> None:
        self.server = server

    def start(self) -> None:
        if not self.attached or self._monitor_task is not None or self.closed:
            return
        raw = os.environ.get("PINKY_API_READINESS_CAP_SEC", str(DEFAULT_CAP_SECONDS))
        try:
            cap = float(raw)
            if not math.isfinite(cap) or cap <= 0:
                raise ValueError
        except ValueError:
            self._log("WARNING", "invalid cap; using default")
            cap = DEFAULT_CAP_SECONDS
        self.cap_seconds = cap
        self.started_at = time.monotonic()
        self._monitor_task = asyncio.create_task(self._monitor(), name="api-listener-readiness")

    def _log(self, level: str, detail: str) -> None:
        elapsed = max(0.0, time.monotonic() - self.started_at) if self.started_at else 0.0
        print(f"{level} api listener readiness {detail}; elapsed={elapsed:.3f}s "
              f"cap={self.cap_seconds:g}s refused_count={self.refused_count}", file=sys.stderr)

    def _refuse_waiters(self, reason: str) -> None:
        waiters = [(future, source) for future, source in self._waiters.items() if not future.done()]
        self.refused_count += len(waiters)
        if waiters or reason == "timeout":
            sources = ",".join(sorted({source for _, source in waiters})) or "monitor"
            self._log("ERROR", f"{reason}; refusing held prompts count={len(waiters)} source={sources}")
        for future, _ in waiters:
            future.set_result(False)

    def _refresh(self) -> None:
        if self.closed or not self.attached:
            return
        if self.server.should_exit:
            self._close_now("main server stopped")
            return
        if self.server.started:
            if not self.ready:
                self.ready = True
                if self.expired:
                    self._log("WARNING", "ready after cap; opening for new submissions")
                self._ready_event.set()
                for future in self._waiters:
                    if not future.done():
                        future.set_result(True)
            return
        if not self.expired and self.started_at and time.monotonic() - self.started_at >= self.cap_seconds:
            self.expired = True
            self._refuse_waiters("timeout")

    def can_submit(self, source: str) -> bool:
        self._refresh()
        if not self.closed and (not self.attached or self.ready):
            return True
        self.refused_count += 1
        reason = "main server unavailable" if self.closed else "timeout" if self.expired else "not ready"
        self._log("ERROR", f"{reason}; refusing submission source={source}")
        return False

    async def wait(self, source: str) -> bool:
        self._refresh()
        if not self.closed and (not self.attached or self.ready):
            return True
        if self.closed or self.expired:
            return self.can_submit(source)
        future = asyncio.get_running_loop().create_future()
        self._waiters[future] = source
        try:
            return await future
        finally:
            self._waiters.pop(future, None)

    async def _monitor(self) -> None:
        while not self.closed:
            self._refresh()
            if self.ready or self.closed:
                return
            await asyncio.sleep(0.01)

    def after_ready(self, callback: Callable[[], Awaitable[None]]) -> None:
        if self._after_ready_task is not None:
            return

        async def run() -> None:
            await self._ready_event.wait()
            self._refresh()
            if self.closed:
                return
            try:
                await callback()
            except Exception:
                self._log("ERROR", "deferred startup replay failed")
                raise

        self._after_ready_task = asyncio.create_task(run(), name="api-after-listener-ready")

    def _close_now(self, reason: str) -> None:
        if not self.closed:
            self.closed = True
            self._refuse_waiters(reason)
            self._ready_event.set()

    async def close(self, reason: str = "daemon shutdown") -> None:
        self._close_now(reason)
        tasks = [task for task in (self._monitor_task, self._after_ready_task)
                 if task is not None and task is not asyncio.current_task()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
