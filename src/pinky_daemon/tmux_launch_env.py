"""Stage a private, single-use shell environment in the execution namespace.

This module is stdlib-only so the same source can run through a wrapped command
runner. Request data arrives on stdin; it must never be interpolated into argv.
Launch ownership and predecessor teardown remain the caller's responsibility.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
import sys
from pathlib import Path

_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*", re.ASCII)


def is_valid_key_name(key: str) -> bool:
    """Return whether a name can be assigned by the launch shell."""
    return isinstance(key, str) and _KEY.fullmatch(key) is not None


def validate_env(env: dict[str, str]) -> None:
    """Reject unsourceable input before creating or pruning any file."""
    for key, value in env.items():
        if not is_valid_key_name(key) or key.startswith("__PINKY_LAUNCH_"):
            raise ValueError("invalid launch environment key")
        if not isinstance(value, str) or "\x00" in value:
            raise ValueError("invalid launch environment value")
        try:
            value.encode("utf-8")
        except UnicodeError:
            raise ValueError("invalid launch environment encoding") from None


def _directory(parent_fd: int, name: str, *, private: bool, create: bool) -> int:
    if create:
        try:
            os.mkdir(name, mode=0o700, dir_fd=parent_fd)
        except FileExistsError:
            pass
    fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
    try:
        info = os.fstat(fd)
        denied = 0o077 if private else 0o022
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & denied:
            raise PermissionError("unsafe launch environment directory")
    except BaseException:
        os.close(fd)
        raise
    return fd


def stage_env(env: dict[str, str], scope: str) -> dict[str, str] | None:
    """Prune this absent session's leftovers and stage a unique 0600 file.

    Files left by a hard kill or a pane that dies before sourcing remain private
    until the next launch. There is deliberately no acknowledgement wait or lock.
    """
    validate_env(env)
    if re.fullmatch(r"[0-9a-f]{64}", scope) is None:
        raise ValueError("invalid launch environment scope")
    populated = {key: value for key, value in env.items() if value != ""}
    guard = "__PINKY_LAUNCH_COMPLETE_" + secrets.token_hex(16)
    while guard in env:
        guard = "__PINKY_LAUNCH_COMPLETE_" + secrets.token_hex(16)
    text = "".join(key + "='" + value.replace("'", "'\\''") + "'\n" for key, value in populated.items())
    # A valid assignment prefix (including an empty file) must not authorize exec.
    # Disable auto-export before bookkeeping, and unset this variable in the pane.
    text += "set +a\n" + guard + "=1\n"
    home = Path.home()
    # Default tmux endpoints also depend on the execution namespace's environment.
    inherited_socket = os.environ.get("TMUX", "").rsplit(",", 2)[0]
    endpoint = [scope, os.environ.get("TMUX_TMPDIR", "/tmp"), inherited_socket]
    scope = hashlib.sha256(json.dumps(endpoint).encode()).hexdigest()
    parts = [".local", "state", "pinkybot", "tmux-launch-env", scope]
    fds = []
    name = None
    created = False
    try:
        fd = os.open(home, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        fds.append(fd)
        info = os.fstat(fd)
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o022:
            raise PermissionError("unsafe launch environment home")
        for index, part in enumerate(parts):
            try:
                fd = _directory(fd, part, private=index >= 3, create=bool(populated))
            except FileNotFoundError:
                if not populated:
                    return None
                raise
            fds.append(fd)
        for old in os.listdir(fd):
            if re.fullmatch(r"env-[0-9a-f]{32}\.sh", old):
                os.unlink(old, dir_fd=fd)
        if not populated:
            return None
        name = "env-" + secrets.token_hex(16) + ".sh"
        output_fd = os.open(
            name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd,
        )
        created = True
        try:
            with os.fdopen(output_fd, "w", encoding="utf-8", newline="\n") as output:
                output.write(text)
        except BaseException:
            try:
                os.close(output_fd)
            except OSError:
                pass
            raise
        return {"path": str(home.joinpath(*parts, name)), "guard": guard}
    except BaseException:
        if created:
            try:
                os.unlink(name, dir_fd=fd)
            except FileNotFoundError:
                pass
        raise
    finally:
        for opened in reversed(fds):
            os.close(opened)


def main() -> None:
    try:
        request = json.load(sys.stdin)
        result = stage_env(request["env"], request["scope"])
    except Exception:
        # Exceptions can carry request values; return only a fixed diagnostic.
        print("launch environment staging failed", file=sys.stderr)
        raise SystemExit(1) from None
    print(json.dumps(result))


if __name__ == "__main__":
    main()
