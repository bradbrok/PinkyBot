"""Small stdlib-only loader and shared launch environment validation."""
from __future__ import annotations

import json
import os
import re
import stat
import sys
from pathlib import Path

_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*", re.ASCII)
_NONCE = re.compile(r"[0-9a-f]{32}", re.ASCII)
_SHELL_NAMES = frozenset({
    "PWD", "OLDPWD", "SHLVL", "_", "BASHOPTS", "BASH_VERSINFO", "EUID", "PPID",
    "SHELLOPTS", "UID", "IFS", "ENV", "BASH_ENV", "PS4",
})


def is_valid_key_name(key: str) -> bool:
    return isinstance(key, str) and _KEY.fullmatch(key) is not None


def key_policy(key: str) -> str | None:
    """Use one name policy for ambient filtering and strict validation."""
    if not is_valid_key_name(key):
        return "invalid"
    if key in _SHELL_NAMES:
        return "shell"
    if key.startswith("__PINKY_LAUNCH_"):
        return "reserved"
    return None


def validate_env(env: dict[str, str]) -> None:
    if not isinstance(env, dict):
        raise ValueError("invalid launch environment mapping")
    for key, value in env.items():
        if key_policy(key) is not None:
            raise ValueError("invalid launch environment key")
        if not isinstance(value, str) or "\x00" in value:
            raise ValueError("invalid launch environment value")
        try:
            value.encode("utf-8")
        except UnicodeError:
            raise ValueError("invalid launch environment encoding") from None


def _private_regular(info) -> bool:
    return (
        stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid()
        and not stat.S_IMODE(info.st_mode) & 0o077
    )


def load_env(path: str, nonce: str, command: str) -> None:
    """Consume validated data before changing the environment or executing."""
    fd = None
    owned = False
    try:
        if not isinstance(nonce, str) or _NONCE.fullmatch(nonce) is None:
            raise ValueError("invalid launch nonce")
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        info = os.fstat(fd)
        owned = (
            Path(path).name == f"env-{nonce}.json"
            and stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid()
        )
        if not owned or not _private_regular(info):
            raise PermissionError("unsafe launch environment file")
        stream = os.fdopen(fd, "r", encoding="utf-8")
        fd = None
        with stream:
            data = json.load(stream)
            if not isinstance(data, dict) or set(data) != {"nonce", "env"}:
                raise ValueError("invalid launch payload")
            if data["nonce"] != nonce:
                raise ValueError("invalid launch nonce")
            validate_env(data["env"])
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
        os.environ.update(data["env"])
        os.execv("/bin/sh", ["/bin/sh", "-c", command])
    except Exception:
        if owned:
            try:
                os.unlink(path)
            except OSError:
                pass
        print("launch environment load failed", file=sys.stderr)
        raise SystemExit(1) from None
    finally:
        if fd is not None:
            os.close(fd)


if __name__ == "__main__":
    load_env(*sys.argv[1:])
