"""Private JSONL receipts for HTTP requests, with credential-safe path logging."""
from __future__ import annotations

import json
import os
import re
import stat
import threading
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic
from typing import Callable

# Each segment is a bearer credential: webhook secret, application share token,
# page share token, or voice session identifier. Pure ASGI paths are decoded;
# whitespace and question marks are credential bytes, not segment boundaries.
_CREDENTIAL_POSITIONS = ("hooks", "a", "p", "ws/voice")
_PREFIX = "(?:" + "|".join(re.escape(item) for item in _CREDENTIAL_POSITIONS) + ")"
_CREDENTIAL_PATH = re.compile(r"(^/" + _PREFIX + r"/)([^/]*)")
_REQUEST_LINE_PATH = re.compile(r'(/' + _PREFIX + r'/)([^/\s"?]*)')
_IDENTITY = re.compile(r"[A-Za-z0-9_.-]{1,64}\Z")


def redact_path(value: str, *, request_line: bool = False) -> str:
    """Redact credential positions; console request lines retain URI delimiters."""
    pattern = _REQUEST_LINE_PATH if request_line else _CREDENTIAL_PATH
    return pattern.sub(r"\1<redacted>", value)[:512]


def _redact_hook_path(value: str) -> str:
    """Compatibility entry point for the existing console request-line filter."""
    return redact_path(value, request_line=True)


def safe_identity(value: str | None) -> str | None:
    return value if value and _IDENTITY.fullmatch(value) else None


class AccessLogWriter:
    """Append receipts, serialized with partial-write recovery and fd replacement.

    Rename rotation hands off the descriptor before compressing the old inode.
    The lock drains any in-progress append before closing that inode; subsequent
    writes use the fresh file. Failures count gaps but never escape to a request.
    """

    def __init__(self, path: str | Path, *, log: Callable[[str], None]) -> None:
        self.path = str(path)
        self.fd: int | None = None
        self.write_failures = 0
        self._lock = threading.RLock()
        self._log = log
        self._last_error = float("-inf")
        self._off = self.path.strip().lower() == "off"
        self._closed = False
        self._damaged = False
        if self._off:
            self._console("ACCESS LOG OFF: HTTP receipts are disabled")
        else:
            try:
                self.reopen()
            except Exception as exc:
                self._report_error(exc)

    @property
    def enabled(self) -> bool:
        return not self._off and not self._closed and self.fd is not None

    def _console(self, message: str) -> None:
        try:
            self._log(message)
        except Exception:
            pass

    def _report_error(self, exc: Exception) -> None:
        now = monotonic()
        if now - self._last_error >= 60:
            self._last_error = now
            # Exception text can contain caller-controlled paths or credentials.
            self._console(f"ACCESS LOG ERROR: {type(exc).__name__}; HTTP receipt gap")

    def reopen(self) -> None:
        """Open the new live inode before retiring the previous descriptor."""
        with self._lock:
            if self._off or self._closed:
                return
            if self._damaged:
                raise OSError("access log disabled after failed append rollback")
            path = Path(self.path)
            path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(
                path,
                os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                0o600,
            )
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    raise OSError("access log must be a regular file")
                os.fchmod(fd, 0o600)
            except BaseException:
                os.close(fd)
                raise
            previous, self.fd = self.fd, fd
            if previous is not None:
                os.close(previous)

    def write(self, record: dict) -> None:
        with self._lock:
            if self._off or self._closed:
                return
            offset = None
            try:
                if self.fd is None:
                    raise OSError("access log unavailable")
                data = (json.dumps(record, ensure_ascii=True, separators=(",", ":")) + "\n").encode("utf-8")
                offset = os.fstat(self.fd).st_size
                remaining = data
                while remaining:
                    written = os.write(self.fd, remaining)
                    if written <= 0:
                        raise OSError("access log write made no progress")
                    remaining = remaining[written:]
            except Exception as exc:
                if offset is not None:
                    try:
                        os.ftruncate(self.fd, offset)
                    except OSError:
                        self._damaged = True
                        fd, self.fd = self.fd, None
                        try:
                            os.close(fd)
                        except OSError:
                            pass
                self.write_failures += 1
                self._report_error(exc)

    def status(self) -> dict:
        with self._lock:
            return {"enabled": self.enabled, "path": self.path, "write_failures": self.write_failures}

    def close(self) -> None:
        with self._lock:
            self._closed = True
            fd, self.fd = self.fd, None
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass


def request_record(request, *, rid: str, status: int, duration: float, error: str | None = None) -> dict:
    """Build the stable public schema without bodies or credential header values."""
    headers = request.headers
    gate = getattr(request.state, "auth_gate", "unclassified")
    caller = "-"
    if gate == "internal_hmac":
        caller = safe_identity(getattr(request.state, "internal_caller", None)) or "-"
    elif gate == "session":
        caller = getattr(request.state, "auth_user", "-")
    server = request.scope.get("server")
    record = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "rid": rid,
        "crid": safe_identity(headers.get("x-request-id")),
        "peer": request.client.host if request.client else "-",
        "port": server[1] if server else None,
        "xff": headers.get("x-forwarded-for", "")[:256] or None,
        "ts_user": headers.get("tailscale-user-login", "")[:256] or None,
        "method": request.method,
        "path": redact_path(request.scope.get("path", request.url.path)),
        "qk": sorted(request.query_params.keys()),
        "status": status,
        "dur_ms": round(duration * 1000, 1),
        "gate": gate,
        "caller": caller,
        "ua": headers.get("user-agent", "")[:200] or None,
        "upgrade": headers.get("upgrade", "")[:32] or None,
    }
    if error is not None:
        record["error"] = error
    return record
