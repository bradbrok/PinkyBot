"""Runtime state and retry policy for the best-effort ferry listener."""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

FERRY_LISTENER_STATES = frozenset({"up", "retrying", "disabled", "dead"})
DEFAULT_RETRY_DELAYS = (5.0, 15.0, 30.0, 60.0)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _error_text(error: BaseException) -> str:
    detail = str(error).strip()
    return f"{type(error).__name__}: {detail}" if detail else type(error).__name__


@dataclass
class FerryListenerState:
    """Mutable listener status shared by the serve loop and main API."""

    state: str = "disabled"
    bind: str = ""
    last_error: str = ""
    retry_count: int = 0
    since: str = field(default_factory=_utc_now)

    @classmethod
    def from_config(cls, config: Any) -> FerryListenerState:
        bind = (
            f"{config.bind_host}:{config.bind_port}"
            if getattr(config, "bind_host", "")
            else ""
        )
        if getattr(config, "enabled", False):
            return cls(
                state="dead",
                bind=bind,
                last_error="ferry listener has not started",
            )
        return cls(state="disabled", bind=bind)

    def update(
        self,
        state: str,
        *,
        bind: str | None = None,
        last_error: str | None = None,
        retry_count: int | None = None,
    ) -> None:
        if state not in FERRY_LISTENER_STATES:
            raise ValueError(f"invalid ferry listener state: {state!r}")
        if state != self.state:
            self.state = state
            self.since = _utc_now()
        if bind is not None:
            self.bind = bind
        if last_error is not None:
            self.last_error = last_error
        if retry_count is not None:
            self.retry_count = retry_count

    def snapshot(self) -> dict[str, str | int]:
        return {
            "state": self.state,
            "bind": self.bind,
            "last_error": self.last_error,
            "retry_count": self.retry_count,
            "since": self.since,
        }


async def serve_ferry_with_retry(
    server: Any,
    listener_state: FerryListenerState,
    *,
    retry_delays: Sequence[float] = DEFAULT_RETRY_DELAYS,
    wait: Callable[[float], Awaitable[None]] = asyncio.sleep,
    log: Callable[[str], None] | None = None,
) -> None:
    """Serve forever, retrying listener failures without escaping to the API.

    ``server`` is intentionally structural rather than typed as
    :class:`uvicorn.Server`, which keeps the retry policy cheap to unit test.
    """
    delays = tuple(retry_delays)
    if not delays:
        raise ValueError("retry_delays must not be empty")

    emit = log or (lambda message: print(message, file=sys.stderr))
    original_startup = server.startup

    async def _startup_with_state(*args, **kwargs) -> None:
        await original_startup(*args, **kwargs)
        if getattr(server, "started", False):
            listener_state.update("up", last_error="")
            emit(
                f"[pinky] Ferry listener up at {listener_state.bind} "
                f"after {listener_state.retry_count} retries"
            )

    server.startup = _startup_with_state
    listener_state.update("retrying", last_error="", retry_count=0)

    try:
        while not server.should_exit:
            attempt = listener_state.retry_count + 1
            emit(
                f"[pinky] Ferry listener attempt {attempt}: "
                f"bind {listener_state.bind}"
            )
            try:
                # ``uvicorn.Server.started`` is not reset by shutdown. Clear it
                # so a later retry cannot report up before it has rebound.
                server.started = False
                await server.serve()
                if server.should_exit:
                    break
                raise RuntimeError("ferry listener exited unexpectedly")
            except (Exception, SystemExit) as error:  # noqa: BLE001
                if server.should_exit:
                    break
                retry_count = listener_state.retry_count + 1
                delay = delays[min(retry_count - 1, len(delays) - 1)]
                error_text = _error_text(error)
                listener_state.update(
                    "retrying",
                    last_error=error_text,
                    retry_count=retry_count,
                )
                emit(
                    f"[pinky] Ferry listener failed attempt {attempt} "
                    f"(main API unaffected); retrying in {delay:g}s: {error_text}"
                )
                await wait(delay)
    finally:
        server.startup = original_startup
        if listener_state.state != "disabled":
            listener_state.update("dead")
