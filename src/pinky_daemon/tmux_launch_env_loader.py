# 4 KiB max.

import json
import os
import re
import stat
import sys

_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*", re.ASCII)
_NONCE = re.compile(r"[0-9a-f]{32}", re.ASCII)
BASE_ALLOWLIST = frozenset((
    "PATH HOME USER LOGNAME SHELL TERM LANG TZ TMPDIR HTTP_PROXY HTTPS_PROXY NO_PROXY "
    "http_proxy https_proxy no_proxy SSL_CERT_FILE SSL_CERT_DIR NODE_EXTRA_CA_CERTS "
    "REQUESTS_CA_BUNDLE"
).split())
DAEMON_ONLY = frozenset({"PINKY_SESSION_SECRET", "PINKYBOT_FERRY_SHARED_SECRET"})
_SHELL_NAMES = frozenset((
    "PWD OLDPWD SHLVL _ BASHOPTS BASH_VERSINFO EUID PPID SHELLOPTS UID IFS ENV BASH_ENV PS4"
).split())

def is_valid_key_name(key):
    return isinstance(key, str) and _KEY.fullmatch(key) is not None

def key_policy(key):
    if not is_valid_key_name(key):
        return "invalid"
    return "shell" if key in _SHELL_NAMES else (
        "reserved" if key.startswith("__PINKY_LAUNCH_") else None)

def _invalid():
    raise ValueError("invalid launch environment") from None

def validate_env(env):
    if not isinstance(env, dict):
        _invalid()
    for key, value in env.items():
        if key_policy(key) or not isinstance(value, str) or "\x00" in value:
            _invalid()
        try:
            value.encode("utf-8")
        except UnicodeError:
            _invalid()

def _private_regular(st):
    return stat.S_ISREG(st.st_mode) and st.st_uid == os.geteuid() and not st.st_mode & 0o077

def validate_inherit(env, inherit):
    if inherit not in ("all", "none"):
        _invalid()
    if inherit == "none" and DAEMON_ONLY.intersection(env):
        raise PermissionError("daemon-only key")

def load_env(path, nonce, command):
    fd, owned = None, False
    try:
        if _NONCE.fullmatch(nonce) is None:
            _invalid()
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        st = os.fstat(fd)
        owned = (
            os.path.basename(path) == f"env-{nonce}.json"
            and stat.S_ISREG(st.st_mode) and st.st_uid == os.geteuid()
        )
        if not owned or not _private_regular(st):
            raise PermissionError("unsafe launch file")
        with os.fdopen(fd, "r", encoding="utf-8") as stream:
            fd = None
            data = json.load(stream)
            if not isinstance(data, dict) or set(data) - {"codex_headers"} not in (
                {"nonce", "env"}, {"nonce", "env", "inherit", "granted"},
                {"nonce", "env", "inherit"},
            ):
                _invalid()
            scrub = data.get("codex_headers", False)
            if (type(scrub) is not bool or data["nonce"] != nonce
                    or ("inherit" in data and data["inherit"] != "none")):
                _invalid()
            validate_env(data["env"])
            inherit = data.get("inherit", "all")
            validate_inherit(data["env"], inherit)
            granted = data.get("granted", [])
            if not isinstance(granted, list) or any(k not in data["env"] for k in granted):
                _invalid()
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
        env = os.environ
        before = set(env)
        kept = {k: v for k, v in env.items()
                if (inherit != "none" or k in BASE_ALLOWLIST or k.startswith(("LC_", "XDG_")))
                and (not scrub or not k.startswith("PINKY_MCP_HDR_"))}
        kept.update(data["env"])
        env.clear()
        env.update(kept)
        if inherit == "none":
            print(f"inherited environment scrubbed; {len(before - env.keys())} names dropped; granted: "
                  + json.dumps(granted), file=sys.stderr, flush=True)
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
