"""Filesystem hardening for the daemon's SQLite databases.

The SQLite files under ``data/`` hold plaintext secrets — bot tokens,
per-agent signing keys, user profiles, encrypted keypairs, token hashes.
They are created with the process umask (commonly ``0644``, i.e.
world-readable), which on a shared host exposes those secrets to any
local reader. This module locks each database to owner-only (``0600``),
along with its SQLite ``-wal`` / ``-shm`` / ``-journal`` sidecars, and
tightens the ``identity/`` directory holding the crown-jewel stores to
``0700``.

The actual chmod is delegated to :mod:`pinky_identity.fs_security`, which
does it through an ``O_NOFOLLOW`` fd so a final-component symlink is never
followed (leaf-only — see that module for the precise scope and threat
model) and there is no ``lstat``->``chmod`` race. The sweep here just
enumerates, canonicalizing its root first; it
is idempotent and cheap, so it runs once on daemon startup. Best-effort —
a file that has vanished or can't be chmod'd is skipped rather than
aborting startup.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

from pinky_identity.fs_security import (
    SECRET_DIR_MODE,
    SECRET_FILE_MODE,
    harden_secret_dir,
    harden_secret_file,
)

logger = logging.getLogger("pinky.db_security")
SWEEP_TIMEOUT_SECONDS = 900
_SIDECARS = ("-wal", "-shm", "-journal")
_DB_SIDECARS = tuple(".db" + suffix for suffix in _SIDECARS)

# Back-compat aliases for daemon-side callers/tests. SECRET_DIR_MODE and
# harden_secret_* are re-exported via the import above.
DB_FILE_MODE = SECRET_FILE_MODE
harden_db_file = harden_secret_file

__all__ = [
    "DB_FILE_MODE",
    "SECRET_DIR_MODE",
    "SECRET_FILE_MODE",
    "harden_db_file",
    "harden_secret_dir",
    "harden_secret_file",
    "sweep_db_permissions",
    "sweep_db_permissions_in_child",
]


class _Sweep:
    def __init__(self, data_dir: str | Path):
        self.root = Path(data_dir).resolve()
        self.uid = os.getuid()
        self.changed = 0
        self.critical: set[str] = set()
        self.foreign: set[Path] = set()
        self.attempted: set[Path] = set()

    def observe(self, path: Path, *, critical: bool = False):
        try:
            metadata = path.lstat()
        except OSError:
            return None
        if not (stat.S_ISREG(metadata.st_mode) or stat.S_ISDIR(metadata.st_mode)):
            return None
        if metadata.st_uid != self.uid:
            self.foreign.add(path)
        elif critical and metadata.st_mode & 0o077:
            self.critical.add(path.relative_to(self.root).as_posix())
        return metadata

    def database(self, path: Path, *, critical: bool = False) -> None:
        for suffix in ("", *_SIDECARS):
            candidate = path.with_name(path.name + suffix)
            self.observe(candidate, critical=critical)
            self.attempted.add(candidate)
        self.changed += harden_secret_file(path)

    def run(self, registered_paths=(), *, progress=None) -> dict:
        identity = self.root / "identity"
        self.observe(identity, critical=True)
        self.changed += harden_secret_dir(identity)
        for value in dict.fromkeys(registered_paths):
            path = Path(value)
            # The catalog supplies canonical paths below its data root.
            if path.is_relative_to(self.root):
                self.database(path, critical=True)
        if progress is not None:
            progress(self.changed)

        pending = [self.root]
        while pending:
            directory = pending.pop()
            try:
                with os.scandir(directory) as entries:
                    for entry in entries:
                        path = Path(entry.path)
                        critical = path.is_relative_to(identity)
                        if critical:
                            self.observe(path, critical=True)
                        try:
                            if entry.is_dir(follow_symlinks=False):
                                pending.append(path)
                            elif entry.name.endswith(".db"):
                                self.database(path, critical=critical)
                            elif entry.name.endswith(_DB_SIDECARS):
                                base = path.with_name(entry.name.rsplit("-", 1)[0])
                                self.database(base, critical=critical)
                        except OSError:
                            continue
            except OSError:
                continue

        remaining = []
        for path in sorted(self.attempted):
            metadata = self.observe(path)
            if (metadata is not None and stat.S_ISREG(metadata.st_mode)
                    and metadata.st_uid == self.uid and metadata.st_mode & 0o077):
                remaining.append(path.relative_to(self.root).as_posix())
        return {"changed": self.changed, "foreign_owned": len(self.foreign),
                "priority_done": True, "critical_loose": sorted(self.critical),
                "remaining_loose": remaining}


def sweep_db_permissions(data_dir: str | Path) -> int:
    """Harden SQLite families without opening live files in their owning process.

    Identity directory first, then a type-based walk, including workspace copies
    and orphan sidecars. Final-component symlinks retain the no-follow hardening.
    """
    return _Sweep(data_dir).run()["changed"]


def _command(data_dir, registered_paths):
    command = [sys.executable, "-m", "pinky_daemon.db_security", str(data_dir)]
    for path in registered_paths:
        command.extend(("--registered-store", str(path)))
    return command


def _finished(output: str | bytes, status: str, started: float) -> dict:
    if isinstance(output, bytes):
        output = output.decode("utf-8", errors="replace")
    lines = output.splitlines()
    priority = bool(lines and lines[0].startswith("priority-done changed="))
    report = {"changed": 0, "foreign_owned": 0, "critical_loose": [], "remaining_loose": []}
    if status == "ok":
        try:
            summary = json.loads(lines[1])
            if (len(lines) != 2 or not priority or summary["priority_done"] is not True
                    or any(type(summary[k]) is not int or summary[k] < 0
                           for k in ("changed", "foreign_owned"))
                    or any(not isinstance(summary[k], list) or any(
                        not isinstance(p, str) or Path(p).is_absolute() or ".." in Path(p).parts
                        for p in summary[k]) for k in ("critical_loose", "remaining_loose"))):
                raise ValueError("invalid sweep summary")
            report.update(summary)
        except (IndexError, KeyError, TypeError, ValueError):
            status = "failed"
    report.update(ok=status == "ok", status=status, priority_done=priority)
    if report["ok"]:
        # Pass no secret values: these are only elapsed time and counts.
        logger.info(
            "SQLite permission sweep completed in child process seconds=%.3f changed=%d foreign_owned=%d",
            time.monotonic() - started, report["changed"], report["foreign_owned"],
        )
    else:
        logger.warning("SQLite permission sweep failed in child process status=%s priority_done=%s",
                       status, "yes" if priority else "no")
    return report


def sweep_db_permissions_in_child(data_dir: str | Path, *, timeout: float = 30) -> bool:
    """Synchronous compatibility entry point; the API owns an async sweep instead."""
    started, output, status = time.monotonic(), "", "ok"
    try:
        result = subprocess.run(_command(data_dir, ()), capture_output=True,
                                text=True, check=True, timeout=timeout)
        output = result.stdout
    except subprocess.TimeoutExpired as exc:
        output, status = exc.output or "", "timeout"
    except subprocess.CalledProcessError as exc:
        output, status = exc.output or "", "failed"
    except Exception:
        status = "failed"
    return _finished(output, status, started)["ok"]


class PermissionSweep:
    """Own the background child and one owner-alert reservation for this boot."""

    def __init__(self, data_dir, registered_paths, notify, log):
        self.command = _command(data_dir, registered_paths)
        self.notify = notify
        self.log = log
        self.timeout = SWEEP_TIMEOUT_SECONDS
        self.sleep = asyncio.sleep
        self.child = None
        self.wait_task = None
        self.readers: list[asyncio.Task] = []
        self.alert_task = None
        self._reserved = False
        self._closing = False
        self._output = bytearray()

    async def _read(self, stream, *, stdout=False):
        while chunk := await stream.read(65536):
            if stdout:
                self._output.extend(chunk)

    async def _end_child(self):
        if self.child is not None and self.child.returncode is None:
            try:
                self.child.terminate()
            except ProcessLookupError:
                pass
            _, pending = await asyncio.wait({self.wait_task}, timeout=0.5)
            if pending:
                try:
                    self.child.kill()
                except ProcessLookupError:
                    pass
                await asyncio.wait(pending, timeout=0.5)
        if self.readers:
            _, pending = await asyncio.wait(self.readers, timeout=0.25)
            for task in pending:
                task.cancel()

    async def run(self) -> dict:
        started, status = time.monotonic(), "ok"
        try:
            self.child = await asyncio.create_subprocess_exec(
                *self.command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            self.wait_task = asyncio.create_task(self.child.wait())
            self.readers = [asyncio.create_task(self._read(self.child.stdout, stdout=True)),
                            asyncio.create_task(self._read(self.child.stderr))]
            await asyncio.wait_for(asyncio.shield(self.wait_task), self.timeout)
            await asyncio.gather(*self.readers)
            if self.child.returncode != 0:
                status = "failed"
        except asyncio.TimeoutError:
            status = "timeout"
            await self._end_child()
        except asyncio.CancelledError:
            await self._end_child()
            raise
        except Exception:
            status = "failed"
            await self._end_child()
        report = _finished(bytes(self._output), status, started)
        if not self._closing:
            self.alert_task = asyncio.create_task(self.notify_owner(report))
        return report

    async def notify_owner(self, report: dict) -> None:
        paths = sorted(set(report["critical_loose"] + report["remaining_loose"]))
        if self._reserved or (report["ok"] and not paths):
            return
        self._reserved = True
        if report["ok"]:
            text = "Database permission sweep found files with permissions open to other users."
        else:
            text = ("Database permission sweep " + ("timed out" if report["status"] == "timeout"
                    else "failed") + "; priority steps "
                    + ("finished." if report["priority_done"] else "not finished."))
        if paths:
            text += " Paths: " + ", ".join(json.dumps(path, ensure_ascii=True) for path in paths[:10])
            if len(paths) > 10:
                text += f"; {len(paths) - 10} more."
        for attempt in range(2):
            try:
                if await self.notify("admin", text):
                    return
            except Exception:
                pass
            if attempt == 0:
                await self.sleep(120)
        self.log("admin: db permission sweep owner alert was not delivered")

    async def close(self) -> None:
        self._closing = True
        await self._end_child()
        if self.alert_task is not None:
            self.alert_task.cancel()
            await asyncio.wait({self.alert_task}, timeout=0.25)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Harden SQLite permissions in a separate process")
    parser.add_argument("data_dir")
    parser.add_argument("--registered-store", action="append", default=[])
    arguments = parser.parse_args()
    result = _Sweep(arguments.data_dir).run(
        arguments.registered_store,
        progress=lambda changed: print(f"priority-done changed={changed}", flush=True),
    )
    print(json.dumps(result), flush=True)
