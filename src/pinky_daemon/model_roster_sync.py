"""Bounded remote roster reads and per-application synchronization ownership."""

from __future__ import annotations

import asyncio
import http.client
import os
import random
import sqlite3
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pinky_daemon.model_roster import MAX_BYTES, load_bundled, parse

DEFAULT_URL = (
    "https://raw.githubusercontent.com/bradbrok/PinkyBot/main/src/pinky_daemon/catalog/models.json"
)
_HOSTS = frozenset({"raw.githubusercontent.com", "pinkybot.ai"})
_REDIRECTS = frozenset({301, 302, 303, 307, 308})
_ERRORS = {
    "disabled": (409, "Remote roster sync is disabled."),
    "busy": (409, "A roster fetch is already in progress."),
    "closing": (503, "Roster synchronization is shutting down."),
    "invalid_url": (400, "The configured roster URL is invalid."),
    "timeout": (504, "The roster fetch deadline expired."),
    "fetch_timeout": (502, "The roster transport deadline expired."),
    "fetch_failed": (502, "The roster transport failed."),
    "redirect_refused": (502, "The roster redirect was refused."),
    "status_refused": (502, "The roster HTTP status was refused."),
    "body_refused": (502, "The roster response body was refused."),
    "parse_refused": (502, "The roster document was refused."),
    "apply_refused": (502, "The roster update was refused."),
    "storage_unavailable": (503, "The model registry is unavailable."),
    "request_invalid": (422, "The roster request is invalid."),
    "model_not_found": (404, "The full model identifier was not found."),
    "release_refused": (409, "The roster ownership release was refused."),
}


class RosterSyncError(Exception):
    """A fixed public reason, independent of transport/document exception text."""

    def __init__(self, code: str):
        self.code = code
        self.status_code, self.message = _ERRORS[code]
        super().__init__(code)

    def detail(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


@dataclass(frozen=True)
class RosterFetchResult:
    document: bytes
    url: str


def validate_roster_url(url: str) -> str:
    """Validate the complete URL without rewriting its successful source identity."""
    if (
        not isinstance(url, str)
        or not url
        or any(
            char.isspace() or ord(char) < 32 or ord(char) == 127 or char in "\\#" for char in url
        )
    ):
        raise RosterSyncError("invalid_url")
    try:
        parts = urllib.parse.urlsplit(url)
        valid = (
            parts.scheme == "https"
            and parts.hostname in _HOSTS
            and parts.username is None
            and parts.password is None
            and parts.port in (None, 443)
            and not parts.netloc.endswith(":")
        )
    except ValueError:
        raise RosterSyncError("invalid_url") from None
    if not valid:
        raise RosterSyncError("invalid_url")
    return url


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def fetch_roster(
    url: str,
    *,
    timeout: float = 10,
    opener=None,
    monotonic: Callable[[], float] = time.monotonic,
) -> RosterFetchResult:
    """Read only bytes; no registry, SQL, parser, or status writer enters the worker."""
    current = validate_roster_url(url)
    deadline = monotonic() + timeout

    def remaining():
        value = deadline - monotonic()
        if value <= 0:
            raise RosterSyncError("fetch_timeout")
        return value

    if opener is None:
        opener = urllib.request.build_opener(
            _NoRedirect(),
            urllib.request.HTTPSHandler(context=ssl.create_default_context()),
        )
    for hop in range(2):
        response = None
        try:
            request = urllib.request.Request(current, headers={"Accept-Encoding": "identity"})
            try:
                response = opener.open(request, timeout=remaining())
            except urllib.error.HTTPError as exc:
                response = exc
            remaining()
            status = response.getcode()
            if status in _REDIRECTS:
                locations = response.headers.get_all("Location", [])
                if (
                    hop == 1
                    or len(locations) != 1
                    or not locations[0]
                    or any(
                        char.isspace() or ord(char) < 32 or ord(char) == 127 or char == "\\"
                        for char in locations[0]
                    )
                ):
                    raise RosterSyncError("redirect_refused")
                target = urllib.parse.urljoin(current, locations[0])
                try:
                    current = validate_roster_url(target)
                except RosterSyncError:
                    raise RosterSyncError("redirect_refused") from None
                continue
            if status != 200:
                raise RosterSyncError("status_refused")
            encodings = response.headers.get_all("Content-Encoding", [])
            if encodings and (len(encodings) != 1 or encodings[0].strip().lower() != "identity"):
                raise RosterSyncError("body_refused")
            lengths = response.headers.get_all("Content-Length", [])
            declared = None
            if lengths:
                if (
                    len(lengths) != 1
                    or not lengths[0].strip().isascii()
                    or not lengths[0].strip().isdigit()
                ):
                    raise RosterSyncError("body_refused")
                declared = int(lengths[0])
                if declared > MAX_BYTES:
                    raise RosterSyncError("body_refused")
            limit = MAX_BYTES + 1 if declared is None else declared + 1
            chunks, total = [], 0
            while True:
                remaining()
                size = min(65536, limit - total)
                chunk = response.read1(size)
                remaining()
                if not isinstance(chunk, bytes) or len(chunk) > size:
                    raise RosterSyncError("body_refused")
                if not chunk:
                    break
                total += len(chunk)
                chunks.append(chunk)
                if total >= limit:
                    raise RosterSyncError("body_refused")
            if declared is not None and total != declared:
                raise RosterSyncError("body_refused")
            return RosterFetchResult(b"".join(chunks), current)
        except (OSError, http.client.HTTPException):
            raise RosterSyncError("fetch_failed") from None
        finally:
            if response is not None:
                response.close()
    raise RosterSyncError("redirect_refused")


class ModelRosterSync:
    """One admission slot and one retained data-only worker for an application."""

    def __init__(
        self,
        registry: Any,
        *,
        getter=None,
        url: str | None = None,
        enabled: bool | None = None,
        monotonic=time.monotonic,
        sleep=asyncio.sleep,
        jitter=None,
        timeout: float = 10,
        first_delay: float = 60,
    ):
        self.registry = registry
        self.getter = fetch_roster if getter is None else getter
        self.url = os.environ.get("PINKY_MODEL_ROSTER_URL", DEFAULT_URL) if url is None else url
        self.enabled = (
            os.environ.get("PINKY_MODEL_ROSTER_SYNC", "").strip().lower() not in ("off", "0", "false")
            if enabled is None
            else enabled
        )
        self.monotonic, self.sleep = monotonic, sleep
        self.jitter = (lambda: random.uniform(0, 1800)) if jitter is None else jitter
        self.timeout, self.first_delay = timeout, first_delay
        self.bundled_revision = load_bundled().revision
        self.closing = False
        self.worker_task: asyncio.Task | None = None
        self._active_task: asyncio.Task | None = None
        self._loop_task: asyncio.Task | None = None
        self._started = False
        self._lock = asyncio.Lock()

    def status(self) -> dict:
        try:
            return {
                **self.registry.get_model_roster_status(),
                "url": self.url,
                "enabled": self.enabled,
                "bundled_revision": self.bundled_revision,
            }
        except (sqlite3.Error, ValueError):
            raise RosterSyncError("storage_unavailable") from None

    @staticmethod
    def _consume_worker(task):
        # Retrieving an invalid worker's exception never authorizes a DB callback.
        if not task.cancelled():
            task.exception()

    async def sync(self, *, dry_run: bool = False) -> dict:
        owner = asyncio.current_task()
        async with self._lock:
            if self.closing:
                raise RosterSyncError("closing")
            if not self.enabled:
                raise RosterSyncError("disabled")
            if self._active_task is not None or (
                self.worker_task is not None and not self.worker_task.done()
            ):
                raise RosterSyncError("busy")
            self.worker_task = None
            self._active_task = owner
        applying = False
        try:
            validate_roster_url(self.url)
            deadline = self.monotonic() + self.timeout
            self.worker_task = asyncio.create_task(
                asyncio.to_thread(self.getter, self.url, timeout=self.timeout),
                name="model-roster-fetch",
            )
            self.worker_task.add_done_callback(self._consume_worker)
            outer_deadline = asyncio.timeout(self.timeout)
            try:
                async with outer_deadline:
                    fetched = await asyncio.shield(self.worker_task)
            except TimeoutError:
                reason = "timeout" if outer_deadline.expired() else "fetch_failed"
                raise RosterSyncError(reason) from None
            except RosterSyncError:
                raise
            except Exception:
                raise RosterSyncError("fetch_failed") from None
            if self.closing:
                raise RosterSyncError("closing")
            if self.monotonic() >= deadline:
                raise RosterSyncError("timeout")
            if not isinstance(fetched, RosterFetchResult):
                raise RosterSyncError("body_refused")
            try:
                validate_roster_url(fetched.url)
            except RosterSyncError:
                raise RosterSyncError("redirect_refused") from None
            try:
                parse(fetched.document)
            except ValueError:
                raise RosterSyncError("parse_refused") from None
            # No suspension separates this closing check from the local transaction.
            if self.closing:
                raise RosterSyncError("closing")
            applying = True
            try:
                return self.registry.apply_model_roster(
                    fetched.document,
                    source=fetched.url,
                    dry_run=dry_run,
                )
            except sqlite3.Error:
                raise RosterSyncError("storage_unavailable") from None
            except ValueError:
                raise RosterSyncError("apply_refused") from None
        except RosterSyncError as exc:
            if not dry_run and not applying and not self.closing:
                self.registry.record_model_roster_sync_error(exc)
            raise
        finally:
            if self._active_task is owner:
                self._active_task = None

    async def run(self) -> None:
        await self.sleep(self.first_delay)
        while not self.closing:
            started = self.monotonic()
            try:
                await self.sync(dry_run=False)
            except RosterSyncError:
                # Genuine attempt failures already own one fixed-code error record.
                pass
            except Exception:
                print("ERROR model roster: scheduled_attempt_failed", file=sys.stderr, flush=True)
            period = 84600 + self.jitter()
            delay = started + period - self.monotonic()
            await self.sleep(delay if delay > 0 else period)

    def start(self) -> asyncio.Task | None:
        if self.closing or not self.enabled:
            return None
        if not self._started:
            self._started = True
            self._loop_task = asyncio.create_task(self.run(), name="model-roster-sync")
        return self._loop_task

    def mark_closing(self) -> None:
        self.closing = True

    async def close(self) -> None:
        self.mark_closing()
        current = asyncio.current_task()
        tasks = {
            task
            for task in (self._loop_task, self._active_task)
            if task is not None and task is not current
        }
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
