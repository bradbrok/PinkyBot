"""Recover failed daemon deployments without importing the application."""

from __future__ import annotations

import asyncio
import contextlib
import errno
import fcntl
import inspect
import json
import logging
import math
import os
import re
import socket
import stat
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path

ROUTE_REFRESH_SECONDS = 3600.0
_REMINDER_SECONDS = 6 * 3600.0
_LOG = logging.getLogger(__name__)
_ERRORS: dict[str, str] = {}
_LAST_ARM: dict[str, dict] = {}
_LOGGED: set[tuple[str, str]] = set()
_ACTIVE: dict[str, StartGuard] = {}
_REF = re.compile(r"[0-9a-fA-F]{40}\Z")


@dataclass(frozen=True)
class GuardSettings:
    max_attempts: int = 3
    start_deadline_seconds: float = 600.0
    settle_seconds: float = 120.0
    loop_starts: int = 5
    loop_window_seconds: float = 600.0


def load_settings(environment=None) -> GuardSettings:
    environment = os.environ if environment is None else environment
    values = {}
    for name, key, default, integral in (
        ("max_attempts", "PINKY_DEPLOY_MAX_ATTEMPTS", 3, True),
        ("start_deadline_seconds", "PINKY_DEPLOY_START_DEADLINE_SEC", 600.0, False),
        ("settle_seconds", "PINKY_DEPLOY_SETTLE_SEC", 120.0, False),
        ("loop_starts", "PINKY_DEPLOY_LOOP_STARTS", 5, True),
        ("loop_window_seconds", "PINKY_DEPLOY_LOOP_WINDOW_SEC", 600.0, False),
    ):
        value = default
        if key in environment:
            try:
                number = float(environment[key])
                if not math.isfinite(number) or number <= 0:
                    raise ValueError
                value = math.ceil(number) if integral else number
            except (TypeError, ValueError, OverflowError):
                _LOG.warning("WARNING deploy guard: invalid %s; using default", key)
        values[name] = value
    return GuardSettings(**values)


def decide(record, head: str, repo_dir: str, enabled: bool = True) -> str:
    """Choose an action using values only; callers own all effects."""
    if not enabled or not record or record.get("schema") != 1 or record.get("repo_dir") != repo_dir:
        return "ordinary"
    state = record.get("state")
    if state == "armed":
        if head != record.get("target_ref"):
            return "stale"
        if len(record["attempts"]) < record["max_attempts"]:
            return "attempt"
        return "rollback"
    if state == "rolling_back":
        if (record.get("rollback") or {}).get("tries", 1) < 2:
            return "retry_rollback"
        return "rollback_failed"
    if state == "rolled_back":
        return "post_rollback"
    if state == "rollback_failed":
        return "delay"
    return "ordinary"


_SELF_REPO = "/fixture/repository"
_SELF_RECORD = {
    "schema": 1,
    "repo_dir": _SELF_REPO,
    "state": "armed",
    "target_ref": "b" * 40,
    "attempts": [],
    "max_attempts": 3,
}
SELFTEST_CASES = (
    (None, "b" * 40, _SELF_REPO, True, "ordinary"),
    (_SELF_RECORD, "b" * 40, _SELF_REPO, False, "ordinary"),
    (dict(_SELF_RECORD, schema=2), "b" * 40, _SELF_REPO, True, "ordinary"),
    (_SELF_RECORD, "b" * 40, "/fixture/other", True, "ordinary"),
    (_SELF_RECORD, "a" * 40, _SELF_REPO, True, "stale"),
    (_SELF_RECORD, "b" * 40, _SELF_REPO, True, "attempt"),
    (dict(_SELF_RECORD, attempts=[{}, {}, {}]), "b" * 40, _SELF_REPO, True, "rollback"),
    (
        dict(_SELF_RECORD, state="rolling_back", rollback={"tries": 1}),
        "b" * 40,
        _SELF_REPO,
        True,
        "retry_rollback",
    ),
    (
        dict(_SELF_RECORD, state="rolling_back", rollback={"tries": 2}),
        "b" * 40,
        _SELF_REPO,
        True,
        "rollback_failed",
    ),
    (dict(_SELF_RECORD, state="rolled_back"), "a" * 40, _SELF_REPO, True, "post_rollback"),
    (dict(_SELF_RECORD, state="rollback_failed"), "b" * 40, _SELF_REPO, True, "delay"),
    *(
        (dict(_SELF_RECORD, state=state), "b" * 40, _SELF_REPO, True, "ordinary")
        for state in ("confirmed", "stale", "reported")
    ),
)


def selftest() -> None:
    for record, head, repo_dir, enabled, expected in SELFTEST_CASES:
        if decide(record, head, repo_dir, enabled) != expected:
            raise RuntimeError("deploy guard self-test failed")


def _key(state_dir) -> str:
    return os.path.abspath(os.fspath(state_dir))


class UnusableStateError(OSError):
    """Carry safe metadata about an unusable state node, never its contents."""

    def __init__(self, state_kind, node_kind, open_errno=None):
        super().__init__("guard state is unusable")
        self.state_kind = state_kind
        self.node_kind = node_kind
        self.open_errno = open_errno


def _state_kind(name):
    kinds = {
        "deploy.json": "deploy record",
        "starts.jsonl": "start history",
        "last_failure.json": "failure record",
        "incident.json": "incident record",
        "alert-route.json": "alert route",
        ".guard.lock": "lock",
        "guard.off": "switch",
    }
    return "temporary file" if name.startswith(".guard-") else kinds.get(name, "state file")


def _node_kind(mode):
    for predicate, label in (
        (stat.S_ISFIFO, "fifo"),
        (stat.S_ISLNK, "symlink"),
        (stat.S_ISDIR, "directory"),
        (stat.S_ISSOCK, "socket"),
        (stat.S_ISBLK, "block device"),
        (stat.S_ISCHR, "character device"),
        (stat.S_ISREG, "regular file"),
    ):
        if predicate(mode):
            return label
    return "unknown node"


def report_error(state_dir, message: str, exc=None, *, repeat: bool = False) -> None:
    # Exception strings can carry credentials or a request URL.
    if isinstance(exc, UnusableStateError):
        diagnostic = f"unusable {exc.state_kind} ({exc.node_kind})"
        safe = diagnostic
        if exc.open_errno in errno.errorcode:
            safe += f" [{errno.errorcode[exc.open_errno]}]"
    else:
        safe = message + (f" ({type(exc).__name__})" if exc is not None else "")
        diagnostic = safe
    key = _key(state_dir)
    _ERRORS[key] = safe
    if repeat or (key, diagnostic) not in _LOGGED:
        _LOGGED.add((key, diagnostic))
        _LOG.error("ERROR deploy guard: %s", safe)


def _directory(state_dir, *, create=False, expected_uid=None) -> Path:
    path = Path(state_dir)
    if create:
        path.mkdir(mode=0o700, exist_ok=True)
    info = path.lstat()
    uid = os.getuid() if expected_uid is None else expected_uid
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != uid or stat.S_IMODE(info.st_mode) != 0o700:
        raise PermissionError("unsafe guard directory")
    return path


@contextlib.contextmanager
def _open_state(state_dir, name, flags, *, missing_ok=False, private=True, expected_uid=None):
    """Validate a nonblocking descriptor before exposing it to any state I/O."""
    descriptor = None
    path = None
    try:
        try:
            path = _directory(
                state_dir,
                create=bool(flags & os.O_CREAT),
                expected_uid=expected_uid,
            )
            truncate = bool(flags & os.O_TRUNC)
            flags = (flags & ~os.O_TRUNC) | os.O_NOFOLLOW | os.O_NONBLOCK
            flags |= getattr(os, "O_CLOEXEC", 0)
            descriptor = os.open(path / name, flags, 0o600)
        except (FileNotFoundError, NotADirectoryError):
            if missing_ok:
                yield None
                return
            raise
        except OSError as exc:
            if path is None:
                raise
            try:
                kind = _node_kind((Path(state_dir) / name).lstat().st_mode)
            except OSError:
                kind = "unopenable node"
            if kind != "regular file" or exc.errno == errno.ENXIO:
                raise UnusableStateError(_state_kind(name), kind, exc.errno) from None
            raise
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise UnusableStateError(_state_kind(name), _node_kind(info.st_mode))
        uid = os.getuid() if expected_uid is None else expected_uid
        if info.st_uid != uid or (private and info.st_mode & 0o077):
            raise PermissionError("unsafe guard file")
        if truncate:
            os.ftruncate(descriptor, 0)
        yield descriptor
    finally:
        if descriptor is not None:
            os.close(descriptor)


@contextlib.contextmanager
def _locked(state_dir):
    with _open_state(state_dir, ".guard.lock", os.O_RDWR | os.O_CREAT) as fd:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            yield Path(state_dir)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)


def _read(state_dir, name, *, expected_uid=None):
    with _open_state(
        state_dir, name, os.O_RDONLY, missing_ok=True, expected_uid=expected_uid
    ) as fd:
        if fd is None:
            return None
        limit = 1024 * 1024
        data = bytearray()
        while len(data) <= limit:
            block = os.read(fd, limit + 1 - len(data))
            if not block:
                break
            data.extend(block)
        if len(data) > limit:
            raise ValueError("guard file too large")
        return data.decode("utf-8")


def _json(state_dir, name):
    text = _read(state_dir, name)
    if text is None:
        return None
    value = json.loads(text)
    if not isinstance(value, dict):
        raise ValueError("invalid guard record")
    return value


def _atomic(state_dir, name, value, *, raw=False):
    path = _directory(state_dir, create=True)
    # Validate an existing destination without truncating or writing to it.
    with _open_state(state_dir, name, os.O_WRONLY, missing_ok=True):
        pass
    temporary = ".guard-" + uuid.uuid4().hex
    created = False
    try:
        with _open_state(state_dir, temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL) as fd:
            created = True
            os.fchmod(fd, 0o600)
            data = value if raw else json.dumps(value, separators=(",", ":")) + "\n"
            remaining = memoryview(data.encode("utf-8"))
            while remaining:
                written = os.write(fd, remaining)
                if written <= 0:
                    raise OSError("incomplete guard write")
                remaining = remaining[written:]
            os.fsync(fd)
        os.replace(path / temporary, path / name)
    finally:
        if created:
            with contextlib.suppress(FileNotFoundError):
                (path / temporary).unlink()


def _history(state_dir) -> list[dict]:
    text = _read(state_dir, "starts.jsonl") or ""
    rows = []
    for line in text.splitlines():
        value = json.loads(line)
        if not isinstance(value, dict) or not isinstance(value.get("ts"), (int, float)):
            raise ValueError("invalid start history")
        rows.append(value)
    return rows


def _write_history(state_dir, rows):
    if len(rows) > 400:
        rows = rows[-200:]
    _atomic(state_dir, "starts.jsonl", "".join(json.dumps(row) + "\n" for row in rows), raw=True)


def _enabled(state_dir, *, strict=False) -> bool:
    if os.environ.get("PINKY_DEPLOY_GUARD") == "0":
        return False
    try:
        with _open_state(state_dir, "guard.off", os.O_RDONLY, missing_ok=True, private=False) as fd:
            return fd is None
    except OSError as exc:
        if strict:
            raise
        report_error(state_dir, "guard state unavailable", exc)
        return False


def _head(repo_dir, runner) -> str:
    value = (
        runner(["git", "rev-parse", "HEAD"], cwd=repo_dir, stderr=subprocess.DEVNULL, timeout=10)
        .decode()
        .strip()
    )
    if not _REF.fullmatch(value):
        raise ValueError("invalid repository ref")
    return value


def _valid_record(record, repo_dir):
    if not record or record.get("schema") != 1 or record.get("repo_dir") != repo_dir:
        return
    if (
        not all(
            isinstance(record.get(k), str) and _REF.fullmatch(record[k])
            for k in ("previous_ref", "target_ref")
        )
        or not isinstance(record.get("attempts"), list)
        or not isinstance(record.get("post_rollback_attempts"), list)
        or not isinstance(record.get("max_attempts"), (int, float))
        or not math.isfinite(record["max_attempts"])
        or record["max_attempts"] <= 0
        or record.get("state")
        not in {
            "armed",
            "confirmed",
            "stale",
            "rolling_back",
            "rolled_back",
            "rollback_failed",
            "reported",
        }
    ):
        raise ValueError("invalid deploy record")


def _write_route(state_dir, routes, clock):
    routes = [
        dict(
            platform=str(route["platform"]),
            account_id=str(route["account_id"]),
            conversation_id=str(route["conversation_id"]),
            token=str(route["token"]),
            supported=route["platform"] == "telegram",
        )
        for route in routes[:2]
    ]
    with _locked(state_dir):
        if not routes:
            (Path(state_dir) / "alert-route.json").unlink(missing_ok=True)
            return
        _atomic(
            state_dir, "alert-route.json", {"schema": 1, "written_at": clock(), "routes": routes}
        )
    if any(not route["supported"] for route in routes):
        _LOG.warning("WARNING deploy guard: unsupported alert platform")


def refresh_alert_route(state_dir, routes, *, clock=None) -> bool:
    try:
        _write_route(state_dir, routes, clock or time.time)
        return True
    except Exception as exc:
        report_error(state_dir, "guard state unavailable", exc)
        return False


def _alert_route_value(state_dir, expected_uid=None):
    text = _read(state_dir, "alert-route.json", expected_uid=expected_uid)
    if text is None:
        return None
    route = json.loads(text)
    if (
        route.get("schema") != 1
        or not isinstance(route.get("routes"), list)
        or len(route["routes"]) > 2
    ):
        raise ValueError("invalid alert route")
    for item in route["routes"]:
        if not all(
            isinstance(item.get(key), str)
            for key in ("platform", "account_id", "conversation_id", "token")
        ):
            raise ValueError("invalid alert route")
    return route


def read_alert_route(state_dir, *, expected_uid=None):
    try:
        return _alert_route_value(state_dir, expected_uid)
    except Exception as exc:
        report_error(state_dir, "guard state unavailable", exc)
        return None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


def send_alert(state_dir, text, *, base_url="https://api.telegram.org", opener=None) -> bool:
    try:
        route = _alert_route_value(state_dir)
    except Exception as exc:
        report_error(state_dir, "guard state unavailable", exc)
        return False
    selected = next(
        (
            item
            for item in (route or {}).get("routes", [])
            if item["platform"] == "telegram" and item["token"]
        ),
        None,
    )
    if selected is None:
        report_error(state_dir, "alert not sent (no route)", repeat=True)
        return False
    try:
        url = (
            base_url.rstrip("/")
            + "/bot"
            + urllib.parse.quote(selected["token"], safe="")
            + "/sendMessage"
        )
        body = json.dumps({"chat_id": selected["conversation_id"], "text": text[:500]}).encode()
        request = urllib.request.Request(
            url, data=body, method="POST", headers={"Content-Type": "application/json"}
        )
        # One attempt, including a timeout whose delivery outcome is unknown.
        open_once = opener or urllib.request.build_opener(_NoRedirect()).open
        with open_once(request, timeout=10) as response:
            delivered = json.loads(response.read(65536)).get("ok") is True
        if not delivered:
            report_error(state_dir, "alert not sent", repeat=True)
        return delivered
    except (TimeoutError, urllib.error.URLError) as exc:
        timed_out = isinstance(exc, TimeoutError) or isinstance(
            getattr(exc, "reason", None), TimeoutError
        )
        report_error(
            state_dir,
            "alert delivery unknown" if timed_out else "alert not sent",
            None if timed_out else exc,
            repeat=True,
        )
    except Exception as exc:
        report_error(state_dir, "alert not sent", exc, repeat=True)
    return False


def check_update_pending(repo_dir, state_dir, *, runner=None) -> bool:
    if not _enabled(state_dir):
        return False
    try:
        record = _json(state_dir, "deploy.json")
        if (
            record
            and record.get("schema") == 1
            and record.get("repo_dir") == os.path.realpath(repo_dir)
            and record.get("state") == "armed"
        ):
            return record.get("target_ref") == _head(repo_dir, runner or subprocess.check_output)
    except Exception as exc:
        report_error(state_dir, "guard state unavailable", exc)
    return False


def _arm_result(state_dir, result):
    _LAST_ARM[_key(state_dir)] = dict(result)
    return result


def arm_deploy(
    repo_dir,
    state_dir,
    *,
    previous_ref,
    target_ref,
    previous_label,
    target_label,
    deps_rebuilt,
    forced_reset,
    requested_by="",
    routes=(),
    settings=None,
    clock=None,
):
    try:
        if not _enabled(state_dir, strict=True):
            return _arm_result(state_dir, {"armed": False, "reason": "switched off"})
        try:
            root_info = Path(state_dir).parent.lstat()
        except (FileNotFoundError, NotADirectoryError):
            root_info = None
        if root_info is None or not stat.S_ISDIR(root_info.st_mode):
            _LOG.warning("WARNING deploy guard: data root absent; update not armed")
            return _arm_result(state_dir, {"armed": False, "reason": "data_root_absent"})
        settings, clock = settings or load_settings(), clock or time.time
        if not _REF.fullmatch(previous_ref) or not _REF.fullmatch(target_ref):
            raise ValueError("invalid deploy refs")
        # A failed route write must not leave an apparently usable armed record.
        _write_route(state_dir, list(routes), clock)
        record = {
            "schema": 1,
            "deploy_id": uuid.uuid4().hex,
            "created_at": clock(),
            "repo_dir": os.path.realpath(repo_dir),
            "previous_ref": previous_ref,
            "previous_label": previous_label,
            "target_ref": target_ref,
            "target_label": target_label,
            "deps_rebuilt": bool(deps_rebuilt),
            "forced_reset": bool(forced_reset),
            "requested_by": requested_by,
            "max_attempts": settings.max_attempts,
            "state": "armed",
            "attempts": [],
            "post_rollback_attempts": [],
            "rollback": None,
        }
        with _locked(state_dir):
            _atomic(state_dir, "deploy.json", record)
        return _arm_result(
            state_dir,
            {
                "armed": True,
                "deploy_id": record["deploy_id"],
                "previous_ref": previous_ref,
                "max_attempts": settings.max_attempts,
            },
        )
    except Exception as exc:
        report_error(state_dir, "deploy record could not be armed", exc)
        return _arm_result(state_dir, {"armed": False, "reason": "state_unusable"})


def guard_status(repo_dir, state_dir):
    result = {
        "enabled": _enabled(state_dir),
        "state": None,
        "deploy_id": None,
        "arming": _LAST_ARM.get(_key(state_dir)),
        "last_error": _ERRORS.get(_key(state_dir)),
        "frontend_warning": None,
        "alert_route": {"present": False, "platforms": [], "written_at": None},
    }
    try:
        record = _json(state_dir, "deploy.json")
        _valid_record(record, os.path.realpath(repo_dir))
        if (
            record
            and record.get("schema") == 1
            and record.get("repo_dir") == os.path.realpath(repo_dir)
        ):
            result.update(state=record.get("state"), deploy_id=record.get("deploy_id"))
            if record.get("rollback"):
                result["frontend_warning"] = (
                    "After rollback the served frontend may be the newer build "
                    "until the next update."
                )
        route = read_alert_route(state_dir)
        if route:
            result["alert_route"] = {
                "present": True,
                "written_at": route.get("written_at"),
                "platforms": [
                    {"platform": item["platform"], "supported": item["platform"] == "telegram"}
                    for item in route["routes"]
                ],
            }
    except Exception as exc:
        report_error(state_dir, "guard state unavailable", exc)
    result["last_error"] = _ERRORS.get(_key(state_dir))
    return result


def install_dependencies(repo_dir, runner, *, timeout=180, command=None, on_attempt=None, log=None):
    """Select and run the same installer for an update, undo or rollback."""
    log = log or (lambda message: None)
    if command is None:
        venv_pip = Path(repo_dir) / ".venv" / "bin" / "pip"
        venv_python = Path(repo_dir) / ".venv" / "bin" / "python"
        if venv_pip.exists():
            try:
                runner(
                    [str(venv_python), "-B", "-m", "pip", "--version"],
                    cwd=repo_dir,
                    stderr=subprocess.STDOUT,
                    timeout=10,
                    env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"),
                )
            except Exception as probe_exc:
                probe_output = getattr(probe_exc, "output", None)
                if isinstance(probe_output, bytes):
                    probe_detail = probe_output.decode(errors="replace")
                elif probe_output:
                    probe_detail = str(probe_output)
                else:
                    probe_detail = f"{type(probe_exc).__name__}: {probe_exc}"
                probe_detail = " ".join(probe_detail.split())[:500]
                if not probe_detail:
                    probe_detail = f"{type(probe_exc).__name__}: {probe_exc}"
                log(
                    "admin: .venv exists but its pip is broken "
                    f"({probe_detail}) — treating it as vestigial and "
                    f"falling back to {sys.executable}"
                )
            else:
                command = [
                    str(venv_python),
                    "-B",
                    "-m",
                    "pip",
                    "install",
                    "-e",
                    ".[all]",
                    "--quiet",
                ]
        if command is None:
            command = [
                sys.executable,
                "-B",
                "-m",
                "pip",
                "install",
                "-e",
                ".[all]",
                "--quiet",
                "--break-system-packages",
            ]
    if on_attempt is not None:
        on_attempt(command)
    runner(
        command,
        cwd=repo_dir,
        stderr=subprocess.STDOUT,
        timeout=timeout,
        env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"),
    )
    return command


class StartGuard:
    def __init__(
        self,
        repo_dir,
        state_dir,
        *,
        settings=None,
        clock=None,
        sleep=None,
        runner=None,
        sender=None,
        exit_process=None,
        host=None,
        log_path=None,
    ):
        self.repo_dir = os.path.realpath(repo_dir)
        self.state_dir = Path(state_dir)
        self.settings = settings or load_settings()
        self.clock = clock or time.time
        self.sleep = sleep or time.sleep
        self.runner = runner or subprocess.check_output
        self.sender = sender or send_alert
        self.exit_process = exit_process or os._exit
        try:
            self.host = host or socket.gethostname()
        except OSError as exc:
            self.host = "unknown"
            report_error(self.state_dir, "host unavailable", exc)
        self.log_path = log_path or str(Path(self.repo_dir) / "logs/api.log")
        self.pid, self.started_at = os.getpid(), self.clock()
        self.cancel = threading.Event()
        self.closed = self.activated = self.intentional_exit = False
        self.ready_task = self.route_task = self.deadline_thread = None
        self.initial_id = None
        self.head = "unknown"
        try:
            record = _json(self.state_dir, "deploy.json")
            self.initial_id = (record or {}).get("deploy_id")
        except Exception as exc:
            report_error(self.state_dir, "guard state unavailable", exc)

    def begin(self):
        try:
            self.state_dir.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        except Exception as exc:
            report_error(self.state_dir, "guard state unavailable", exc)
        try:
            self.head = _head(self.repo_dir, self.runner)
        except Exception as exc:
            report_error(self.state_dir, "repository unavailable", exc)
        try:
            with _locked(self.state_dir):
                rows = _history(self.state_dir)
                # Contending starts remain neutral until authority is resolved.
                rows.append(
                    {"ts": self.started_at, "pid": self.pid, "ref": self.head, "pending": True}
                )
                _write_history(self.state_dir, rows)
        except Exception as exc:
            report_error(self.state_dir, "guard state unavailable", exc)
        return self

    def _own(self, row):
        return row.get("pid") == self.pid and row.get("ts") == self.started_at

    def _prior_failures(self, rows):
        starts = []
        for row in rows:
            if self._own(row):
                break
            if row.get("lock_busy") or row.get("pending"):
                continue
            if row.get("serving"):
                starts.clear()
            elif "ref" in row:
                starts.append(row)
        return starts

    def _delay(self, count):
        self.sleep(min(60.0, 5.0 * 2 ** min(4, count - self.settings.loop_starts)))

    def _error_summary(self):
        failure = _json(self.state_dir, "last_failure.json") or {}
        name = str(failure.get("exc_type") or "unknown")
        detail = failure.get("detail") if name == "StoreCatalogError" else ""
        return (name + (f": {detail}" if detail else ""))[:240]

    def _send(self, text):
        try:
            return bool(self.sender(self.state_dir, text[:500]))
        except Exception as exc:
            report_error(self.state_dir, "alert not sent", exc, repeat=True)
            return False

    def _ordinary(self, prior):
        count = len(prior)
        if count < self.settings.loop_starts or any(
            self.clock() - row["ts"] > self.settings.loop_window_seconds
            for row in prior[-self.settings.loop_starts :]
        ):
            return
        text = None
        with _locked(self.state_dir):
            incident = _json(self.state_dir, "incident.json") or {}
            if not incident.get("open"):
                incident = {"open": True, "started_at": prior[0]["ts"], "last_alert_at": None}
            incident["failed_starts"] = count
            last = incident.get("last_alert_at")
            if last is None or self.clock() - last >= _REMINDER_SECONDS:
                incident["last_alert_at"] = self.clock()
                minutes = max(0.0, self.clock() - prior[0]["ts"]) / 60
                text = (
                    f"Daemon on {self.host} failed to start {count} times in {minutes:g} min. "
                    f"Last error: {self._error_summary()}. It keeps retrying. Log: {self.log_path}."
                )
            _atomic(self.state_dir, "incident.json", incident)
        if text:
            self._send(text)
        self._delay(count)

    def activate(self):
        """Apply the start decision after the caller has acquired authority."""
        if self.activated or self.closed:
            return
        self.activated = True
        try:
            with _locked(self.state_dir):
                rows = _history(self.state_dir)
                prior = self._prior_failures(rows)
                for row in rows:
                    if self._own(row):
                        row.pop("pending", None)
                _write_history(self.state_dir, rows)
                if not _enabled(self.state_dir):
                    return
                try:
                    record = _json(self.state_dir, "deploy.json")
                    _valid_record(record, self.repo_dir)
                except Exception as exc:
                    report_error(self.state_dir, "guard state unavailable", exc)
                    record = None
                try:
                    self.head = _head(self.repo_dir, self.runner)
                except Exception as exc:
                    report_error(self.state_dir, "repository unavailable", exc)
                    self.head = "unknown"
                action = (
                    decide(record, self.head, self.repo_dir)
                    if self.head != "unknown"
                    else "ordinary"
                )
                text = None
                if action == "stale":
                    record["state"] = "stale"
                    _atomic(self.state_dir, "deploy.json", record)
                elif action == "attempt":
                    record["attempts"].append(
                        {
                            "n": len(record["attempts"]) + 1,
                            "pid": self.pid,
                            "started_at": self.started_at,
                        }
                    )
                    _atomic(self.state_dir, "deploy.json", record)
                elif action == "post_rollback":
                    record["post_rollback_attempts"].append(
                        {
                            "n": len(record["post_rollback_attempts"]) + 1,
                            "pid": self.pid,
                            "started_at": self.started_at,
                        }
                    )
                    count = len(record["post_rollback_attempts"])
                    last = record.get("post_rollback_alert_at")
                    if count >= record["max_attempts"] and (
                        last is None or self.clock() - last >= _REMINDER_SECONDS
                    ):
                        record["post_rollback_alert_at"] = self.clock()
                        text = (
                            f"Update to {record['target_label']} failed to start, and "
                            f"{record['previous_label']} does not start either "
                            f"({count} more failed "
                            f"starts). Last error: {self._error_summary()}. Manual action needed."
                        )
                    _atomic(self.state_dir, "deploy.json", record)
            if action in {"rollback", "retry_rollback"}:
                self._rollback(record)
            elif action == "rollback_failed":
                self._rollback_failed(record, RuntimeError("interrupted rollback"))
            elif action == "attempt":
                self._start_deadline(record["deploy_id"])
            elif action in {"post_rollback", "delay"}:
                if text:
                    self._send(text)
                self._delay(len(prior))
            elif action == "ordinary":
                self._ordinary(prior)
        except Exception as exc:
            report_error(self.state_dir, "guard state unavailable", exc)

    def _rollback_failed(self, record, exc):
        report_error(self.state_dir, "rollback checkout failed", exc)
        rollback = record.get("rollback") or {}
        send = not rollback.get("alert_sent_at")
        rollback.update(error_class=type(exc).__name__, alert_sent_at=self.clock())
        record.update(state="rollback_failed", rollback=rollback)
        with _locked(self.state_dir):
            _atomic(self.state_dir, "deploy.json", record)
        if send:
            self._send(
                f"Update to {record['target_label']} failed to start and the checkout of "
                f"{record['previous_label']} did not work ({type(exc).__name__}). "
                "Manual action needed."
            )

    def _rollback(self, record):
        rollback = record.get("rollback") or {}
        rollback.update(tries=rollback.get("tries", 0) + 1, started_at=self.clock())
        record.update(state="rolling_back", rollback=rollback)
        with _locked(self.state_dir):
            _atomic(self.state_dir, "deploy.json", record)
        try:
            if record["previous_ref"] != record["target_ref"]:
                self.runner(
                    ["git", "checkout", "--detach", record["previous_ref"]],
                    cwd=self.repo_dir,
                    stderr=subprocess.STDOUT,
                    timeout=60,
                )
                if _head(self.repo_dir, self.runner) != record["previous_ref"]:
                    raise RuntimeError("rollback HEAD does not match previous ref")
        except Exception as exc:
            self._rollback_failed(record, exc)
            return
        rollback["deps_error"] = None
        if record["deps_rebuilt"]:
            try:
                install_dependencies(self.repo_dir, self.runner, timeout=300)
            except Exception as exc:
                rollback["deps_error"] = type(exc).__name__
                report_error(self.state_dir, "dependency restore failed", exc)
        record["state"] = "rolled_back"
        rollback["finished_at"] = self.clock()
        with _locked(self.state_dir):
            _atomic(self.state_dir, "deploy.json", record)
        try:
            self.runner(
                [
                    "git",
                    "cat-file",
                    "-e",
                    f"{record['previous_ref']}:src/pinky_daemon/deploy_guard.py",
                ],
                cwd=self.repo_dir,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )
        except Exception:
            if not rollback.get("legacy_alert_at"):
                rollback["legacy_alert_at"] = self.clock()
                with _locked(self.state_dir):
                    _atomic(self.state_dir, "deploy.json", record)
                self._send(
                    f"Update to {record['target_label']} failed to start "
                    f"({len(record['attempts'])} attempts; {self._error_summary()}). "
                    f"Checked out {record['previous_label']}; the previous release is starting "
                    "and will not confirm readiness."
                )
        self.intentional_exit = True
        raise SystemExit(75)

    def _start_deadline(self, deploy_id):
        def deadline():
            remaining = self.settings.start_deadline_seconds
            while remaining > 0:
                interval = min(remaining, threading.TIMEOUT_MAX)
                if self.cancel.wait(interval):
                    return
                remaining -= interval
            try:
                with _locked(self.state_dir):
                    record = _json(self.state_dir, "deploy.json")
                    if (
                        self.cancel.is_set()
                        or not record
                        or record.get("deploy_id") != deploy_id
                        or record.get("state") != "armed"
                    ):
                        return
                    for attempt in record["attempts"]:
                        if attempt["pid"] == self.pid and attempt["started_at"] == self.started_at:
                            attempt["failure"] = "start deadline exceeded"
                    _atomic(self.state_dir, "deploy.json", record)
                    _atomic(
                        self.state_dir,
                        "last_failure.json",
                        {
                            "ts": self.clock(),
                            "pid": self.pid,
                            "exc_type": "StartDeadlineExceeded",
                            "detail": "",
                        },
                    )
            except Exception as exc:
                report_error(self.state_dir, "guard state unavailable", exc)
            if not self.cancel.is_set():
                self.exit_process(75)

        self.deadline_thread = threading.Thread(
            target=deadline, name="deploy-start-deadline", daemon=True
        )
        self.deadline_thread.start()

    def failed(self, exc):
        if self.intentional_exit:
            return
        busy = (
            any(
                cls.__name__ == "StoreAuthorityError"
                and cls.__module__ == "pinky_daemon.store_authority"
                for cls in type(exc).__mro__
            )
            and str(exc) == "daemon store authority is already held"
        )
        if busy:
            self.cancel.set()
            try:
                with _locked(self.state_dir):
                    rows = _history(self.state_dir)
                    for index, row in enumerate(rows):
                        if self._own(row):
                            rows[index] = {
                                "ts": self.started_at,
                                "pid": self.pid,
                                "lock_busy": True,
                            }
                    _write_history(self.state_dir, rows)
                    record = _json(self.state_dir, "deploy.json")
                    if self.activated and record and record.get("repo_dir") == self.repo_dir:
                        for key in ("attempts", "post_rollback_attempts"):
                            record[key] = [
                                a
                                for a in record[key]
                                if not (
                                    a.get("pid") == self.pid
                                    and a.get("started_at") == self.started_at
                                )
                            ]
                        _atomic(self.state_dir, "deploy.json", record)
            except Exception as error:
                report_error(self.state_dir, "guard state unavailable", error)
            return
        # Bootstrap failures have not reached the authority-acquired hook.
        self.activate()
        self.cancel.set()
        if not _enabled(self.state_dir):
            return
        detail = (
            str(exc)
            if any(
                cls.__name__ == "StoreCatalogError"
                and cls.__module__ == "pinky_daemon.store_catalog"
                for cls in type(exc).__mro__
            )
            else ""
        )
        try:
            with _locked(self.state_dir):
                _atomic(
                    self.state_dir,
                    "last_failure.json",
                    {
                        "ts": self.clock(),
                        "pid": self.pid,
                        "exc_type": type(exc).__name__,
                        "detail": detail,
                    },
                )
        except Exception as error:
            report_error(self.state_dir, "guard state unavailable", error)

    async def _normal_notice(self, text, owner_notify):
        delivered = False
        if owner_notify is not None:
            try:
                value = owner_notify(text[:500])
                delivered = bool(await value if inspect.isawaitable(value) else value)
            except Exception as exc:
                report_error(self.state_dir, "owner alert not delivered", exc)
        if not delivered:
            self._send(text)

    def ready(self, *, owner_notify=None, internal_notify=None, route_provider=None):
        self.cancel.set()
        if self.ready_task is not None:
            return self.ready_task
        try:
            with _locked(self.state_dir):
                rows = _history(self.state_dir)
                rows.append({"ts": self.clock(), "pid": self.pid, "serving": True})
                _write_history(self.state_dir, rows)
        except Exception as exc:
            report_error(self.state_dir, "guard state unavailable", exc)

        async def settle():
            await asyncio.sleep(self.settings.settle_seconds)
            if self.closed:
                return
            try:
                notices = []
                with _locked(self.state_dir):
                    record = _json(self.state_dir, "deploy.json")
                    _valid_record(record, self.repo_dir)
                    if (
                        record
                        and record.get("schema") == 1
                        and record.get("repo_dir") == self.repo_dir
                        and record.get("deploy_id") == self.initial_id
                    ):
                        state = record["state"]
                        if (
                            state == "armed"
                            and _head(self.repo_dir, self.runner) == record["target_ref"]
                        ):
                            record["state"] = "confirmed"
                        elif state in {"rolled_back", "rollback_failed"}:
                            if state == "rolled_back":
                                text = (
                                    f"Update to {record['target_label']} failed to start "
                                    f"({len(record['attempts'])} attempts; "
                                    f"{self._error_summary()}). "
                                    f"Checked out {record['previous_label']}; "
                                    "the daemon is serving again."
                                )
                                if (record.get("rollback") or {}).get("deps_error"):
                                    text += (
                                        " Dependency restore failed; "
                                        "installed dependencies may not match."
                                    )
                                if record.get("forced_reset"):
                                    text += " Local tracked changes were discarded."
                            else:
                                duration = max(0.0, self.clock() - record["created_at"])
                                text = (
                                    f"Daemon is serving again after {len(record['attempts'])} "
                                    f"failed starts over {duration:g} seconds."
                                )
                            notices.append((text[:500], record.get("requested_by", "")))
                            record["state"] = "reported"
                        _atomic(self.state_dir, "deploy.json", record)
                    incident = _json(self.state_dir, "incident.json")
                    if incident and incident.get("open"):
                        incident.update(open=False, closed_at=self.clock())
                        _atomic(self.state_dir, "incident.json", incident)
                        duration = max(0.0, self.clock() - incident["started_at"])
                        notices.append(
                            (
                                f"Daemon is serving again after {incident['failed_starts']} "
                                f"failed starts over {duration:g} seconds.",
                                "",
                            )
                        )
                for text, requester in notices:
                    await self._normal_notice(text, owner_notify)
                    if requester and internal_notify is not None:
                        try:
                            value = internal_notify(requester, text)
                            if inspect.isawaitable(value):
                                await value
                        except Exception as exc:
                            report_error(self.state_dir, "internal alert not delivered", exc)
            except Exception as exc:
                report_error(self.state_dir, "guard state unavailable", exc)
            self._refresh_route(route_provider)

        async def hourly():
            while not self.closed:
                await asyncio.sleep(ROUTE_REFRESH_SECONDS)
                self._refresh_route(route_provider)

        self.ready_task = asyncio.create_task(settle(), name="deploy-guard-settle")
        if route_provider is not None:
            self.route_task = asyncio.create_task(hourly(), name="deploy-guard-route-refresh")
        return self.ready_task

    def _refresh_route(self, provider):
        if provider is not None:
            try:
                refresh_alert_route(self.state_dir, provider(), clock=self.clock)
            except Exception as exc:
                report_error(self.state_dir, "guard state unavailable", exc)

    def close(self):
        self.closed = True
        self.cancel.set()
        for task in (self.ready_task, self.route_task):
            if task is not None and not task.done() and not task.get_loop().is_closed():
                task.cancel()
        if _ACTIVE.get(_key(self.state_dir)) is self:
            _ACTIVE.pop(_key(self.state_dir), None)


def begin_start(repo_dir, state_dir, **kwargs):
    guard = StartGuard(repo_dir, state_dir, **kwargs)
    _ACTIVE[_key(state_dir)] = guard
    return guard.begin()


def mark_ready(repo_dir, state_dir, **kwargs):
    guard = _ACTIVE.get(_key(state_dir))
    if guard is None:
        guard = StartGuard(repo_dir, state_dir)
        _ACTIVE[_key(state_dir)] = guard
    return guard.ready(**kwargs)


async def close_guard(state_dir):
    guard = _ACTIVE.get(_key(state_dir))
    if guard is not None:
        guard.close()
        tasks = [task for task in (guard.ready_task, guard.route_task) if task is not None]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
