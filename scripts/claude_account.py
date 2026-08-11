#!/usr/bin/env python3
"""claude-account — manage the fleet's forwarded Claude OAuth account (#570).

The tmux fleet authenticates every agent with a single long-lived
``claude setup-token`` that the daemon forwards as ``CLAUDE_CODE_OAUTH_TOKEN``
(gated by ``PINKY_FORWARD_OAUTH_TOKEN=1``). Switching which account the fleet
runs as means swapping that one token in ``.env`` and restarting the daemon —
a hand-run, error-prone recipe whose failure mode is a fleet-wide login wall.

This CLI stores named tokens (0600) and does a **safe, verified switch**:
back up ``.env`` → write the token and the forward flag together (fail-closed,
never flag-on-token-missing) → optionally restart → verify. The owner drives
the interactive mint; the token is never passed on argv, never printed, never
logged.

Subcommands::

    claude-account add <name>        mint/paste + isolation-probe + store a token
    claude-account list              stored accounts, active one, days-to-expiry
    claude-account current           which stored token the .env currently uses
    claude-account switch <name>     safe swap (backup + fail-closed write [+ restart])
    claude-account remove <name>     delete a stored token
    claude-account check-expiry      nudge (exit 3) if the active token expires soon

Scope: the box-wide forwarded token only. Per-agent dedicated-config-dir logins
are a separate concern and are not managed here.
"""

from __future__ import annotations

import argparse
import getpass
import hmac
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ── constants ────────────────────────────────────────────────────────────────

TOKEN_ENV_KEY = "CLAUDE_CODE_OAUTH_TOKEN"
FORWARD_FLAG_KEY = "PINKY_FORWARD_OAUTH_TOKEN"
SETUP_TOKEN_PREFIX = "sk-ant-oat01-"
DEFAULT_TTL_DAYS = 365
EXPIRY_WARN_DAYS = 30
PROBE_TIMEOUT_S = 60
RESTART_HEALTH_TIMEOUT_S = 90
INDEX_VERSION = 1

# billing modes the store recognises. Owner-declared per account: the auth
# method + token kind determine whether inference bills as subscription or as
# API usage, and that mapping is subtle enough to not guess — the default is
# "unknown" until the owner states it.
BILLING_MODES = ("api_usage", "subscription", "unknown")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(value: str) -> datetime | None:
    if not value:
        return None
    try:
        # tolerate a trailing Z
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    # A hand-edited index may carry a bare/naive timestamp (e.g. "2027-01-01");
    # treat it as UTC so a later `exp - _utcnow()` never mixes naive/aware.
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# ── paths ────────────────────────────────────────────────────────────────────


def store_dir() -> Path:
    """Where token files + the index live (override with PINKY_ACCOUNTS_DIR)."""
    override = os.environ.get("PINKY_ACCOUNTS_DIR", "").strip()
    base = Path(override) if override else Path.home() / ".pinkybot" / "accounts"
    return base


def index_path() -> Path:
    return store_dir() / "index.json"


def token_path(name: str) -> Path:
    return store_dir() / f"{name}.token"


def default_env_path() -> Path:
    """The repo-root ``.env`` (override with PINKY_ENV_FILE / --env-file).

    scripts/claude_account.py → parent (scripts) → parent (repo root).
    """
    override = os.environ.get("PINKY_ENV_FILE", "").strip()
    if override:
        return Path(override)
    return Path(__file__).resolve().parent.parent / ".env"


# ── name validation ──────────────────────────────────────────────────────────


# A stored token is interpolated verbatim into .env, which the macOS service
# loads via bash ``source`` — so it MUST be a single line containing no shell
# metacharacters (else a value like ``GOOD\nCLAUDE_CODE_OAUTH_TOKEN=`` injects a
# second assignment, and ``$()``/quotes/spaces would be interpreted by the shell).
# setup-tokens are base64/base64url + a fixed prefix — all within this charset.
_TOKEN_SAFE_RE = re.compile(r"\A[A-Za-z0-9_.\-=+/:]+\Z")


def token_serialization_safe(token: str) -> bool:
    """True iff ``token`` is one line safe to write bare into a shell-sourced .env."""
    return bool(token) and bool(_TOKEN_SAFE_RE.match(token))


def valid_name(name: str) -> bool:
    """A store name must be a safe filename fragment (no path traversal)."""
    if not name or len(name) > 64:
        return False
    return all(c.isalnum() or c in ("-", "_", ".") for c in name) and name not in (
        ".",
        "..",
    )


# ── token store ──────────────────────────────────────────────────────────────


def ensure_store() -> None:
    d = store_dir()
    d.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(d, 0o700)
    except OSError:
        pass


def load_index() -> dict:
    p = index_path()
    if not p.exists():
        return {"version": INDEX_VERSION, "accounts": {}}
    try:
        # ValueError covers both json.JSONDecodeError and a non-UTF-8 read
        # (UnicodeDecodeError) — a truncated/corrupt index becomes a clean exit.
        data = json.loads(p.read_text())
    except (ValueError, OSError) as exc:
        raise SystemExit(f"claude-account: corrupt index {p}: {exc}")
    if not isinstance(data, dict) or not isinstance(data.get("accounts"), dict):
        raise SystemExit(f"claude-account: unexpected index shape in {p}")
    return data


def save_index(data: dict) -> None:
    ensure_store()
    _atomic_write(index_path(), json.dumps(data, indent=2, sort_keys=True) + "\n", 0o600)


def read_token(name: str) -> str:
    p = token_path(name)
    if not p.exists():
        raise SystemExit(f"claude-account: no stored token file for {name!r}")
    return p.read_text().strip()


def write_token(name: str, token: str) -> None:
    ensure_store()
    _atomic_write(token_path(name), token + "\n", 0o600)


# ── atomic + backup file writes ──────────────────────────────────────────────


def _atomic_write(path: Path, text: str, mode: int) -> None:
    """Write via a temp file in the same dir + os.replace, chmod BEFORE the swap.

    Same-directory temp so os.replace is atomic (no cross-device copy). The mode
    is applied to the temp file before the rename so the final path is never
    briefly world-readable — matters for token / .env files.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _atomic_copy(src: Path, dst: Path, mode: int) -> None:
    """Byte-exact copy src → dst with `mode` applied BEFORE the rename.

    Used for credential files (.env and its backups): never leaves the
    destination torn (os.replace is atomic) and never briefly world-readable
    (chmod precedes the swap) — unlike shutil.copy2, which creates the dest at
    0o666&~umask and only tightens it after streaming the secret.
    """
    data = src.read_bytes()
    dst.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(dst.parent), prefix=f".{dst.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, dst)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def backup_dir() -> Path:
    return store_dir() / "env-backups"


def backup_env(env_path: Path, tag: str) -> Path:
    """Copy .env → <store>/env-backups/<name>.bak.pre-<tag>-<UTC>, 0600, atomic.

    Backups go in the 0700 store dir OUTSIDE the repo tree: a credential backup
    written beside .env in a (public) repo is one ``git add -A`` from disclosure,
    and the bare ``.env`` .gitignore rule does not match ``.env.bak.*``.
    """
    ensure_store()
    bdir = backup_dir()
    bdir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(bdir, 0o700)
    except OSError:
        pass
    stamp = _utcnow().strftime("%Y%m%d-%H%M%S")
    backup = bdir / f"{env_path.name}.bak.pre-{tag}-{stamp}"
    _atomic_copy(env_path, backup, 0o600)
    return backup


# ── .env line rewriting ──────────────────────────────────────────────────────


def _live_key(line: str) -> str | None:
    """The KEY assigned on this line, or None if it is not a live assignment.

    The shipped macOS service loads .env via bash ``set -a && source .env`` (see
    scripts/launchctl/com.pinkybot.daemon.plist), so BOTH a bare ``KEY=…`` and an
    ``export KEY=…`` are live assignments — and bash is LAST-wins. A leading ``#``
    is a comment. The key is the text before the first ``=`` (minus an optional
    ``export`` prefix). ``FOO_BAR`` must not match ``FOO``.
    """
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or "=" not in stripped:
        return None
    lhs = stripped.split("=", 1)[0].strip()
    if lhs.startswith("export") and (len(lhs) == 6 or lhs[6].isspace()):
        lhs = lhs[6:].strip()
    return lhs or None


def _is_live_assignment(line: str, key: str) -> bool:
    return _live_key(line) == key


def set_env_var(lines: list[str], key: str, value: str) -> list[str]:
    """Return ``lines`` with EXACTLY ONE live ``key=value`` assignment (bare form).

    Collapses every live form of the key — bare ``KEY=`` AND ``export KEY=`` — to a
    single canonical bare line, dropping all other live occurrences. This is what
    makes the result unambiguous across all three loaders the fleet may use (bash
    ``source`` = last-wins + honors ``export``; systemd EnvironmentFile; the Python
    parser = first-wins): with exactly one live assignment and no ``export``
    variants left to shadow it, they all resolve the same value. Comment lines are
    left untouched. Appends one if the key is absent.
    """
    out: list[str] = []
    seen = False
    for line in lines:
        if _live_key(line) == key:
            if not seen:
                out.append(f"{key}={value}")
                seen = True
            # else: drop the duplicate/variant live assignment
            continue
        out.append(line)
    if not seen:
        out.append(f"{key}={value}")
    return out


def read_live_value(lines: list[str], key: str) -> str | None:
    """The value of the LAST live ``key=…`` line (quotes stripped), else None.

    LAST, not first: the authoritative loader is bash ``source`` (last-wins), so
    this reflects what the daemon actually resolves for a messy pre-rewrite file.
    (After set_env_var there is exactly one, so first/last coincide.)
    """
    found: str | None = None
    for line in lines:
        if _live_key(line) == key:
            raw = line.strip().split("=", 1)[1].strip()
            found = raw.strip("\"'")
    return found


def read_env_lines(env_path: Path) -> list[str]:
    if not env_path.exists():
        raise SystemExit(f"claude-account: .env not found at {env_path}")
    # splitlines drops the trailing newline; we re-add exactly one on write.
    return env_path.read_text().splitlines()


def write_env_lines(env_path: Path, lines: list[str]) -> None:
    # Always 0600 — .env holds live credentials. Preserving a permissive source
    # mode (e.g. a 0644 .env) would leave the freshly-written OAuth token
    # group/world-readable; clamp it closed on every write.
    _atomic_write(env_path, "\n".join(lines) + "\n", 0o600)


# ── isolation probe ──────────────────────────────────────────────────────────


def resolve_claude_bin(explicit: str = "") -> str:
    if explicit:
        return explicit
    found = shutil.which("claude")
    if found:
        return found
    candidate = Path.home() / ".local" / "bin" / "claude"
    return str(candidate) if candidate.exists() else "claude"


def probe_token(token: str, claude_bin: str = "") -> tuple[bool, str]:
    """Isolation-probe a token BEFORE trusting it (never wall the fleet).

    Runs ``claude --print`` in a minimal env carrying only the candidate token,
    so a bad token is caught here instead of at fleet startup. The token is
    passed via env (never argv) and never echoed. ``--print`` does NOT surface
    the interactive Fable-credit consent, so a pass here means "authenticates",
    not "no consent prompt at real startup".
    """
    binary = resolve_claude_bin(claude_bin)
    env = {
        "HOME": os.environ.get("HOME", str(Path.home())),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        TOKEN_ENV_KEY: token,
        # keep the probe quiet + non-interactive
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    }
    try:
        proc = subprocess.run(
            [binary, "--print", "say OK"],
            env=env,
            capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT_S,
        )
    except FileNotFoundError:
        return False, f"claude binary not found at {binary!r}"
    except subprocess.TimeoutExpired:
        return False, f"probe timed out after {PROBE_TIMEOUT_S}s"
    if proc.returncode != 0:
        # NEVER surface the child's stdout/stderr — claude may echo the candidate
        # token in an error line, and this string is printed to the operator. A
        # generic, category-only reason keeps the secret out of the console/logs.
        return False, (
            f"claude rejected the token or errored (exit {proc.returncode}); "
            f"run the probe by hand to see details"
        )
    if not (proc.stdout or "").strip():
        return False, "probe returned empty output"
    return True, "ok"


# ── daemon restart ───────────────────────────────────────────────────────────


def _daemon_base_url() -> str:
    port = os.environ.get("PINKY_API_PORT", "8888").strip() or "8888"
    return f"http://127.0.0.1:{port}"


def restart_daemon(env_path: Path, as_agent: str) -> tuple[bool, str]:
    """POST /admin/restart with internal auth (the path the daemon's own restart
    tool uses). The daemon abstracts launchctl-vs-systemd, so we never
    reimplement box-specific restart logic here.

    ``as_agent`` is the identity the request is signed as. The daemon accepts the
    shared secret only for a REGISTERED, non-isolated agent name, so this must be
    a real agent on this box (supplied via --as-agent / PINKY_AGENT_NAME) — there
    is no baked-in default. ``env_path`` is the same .env the switch operated on,
    so the secret is sourced from the file being managed.
    """
    if not as_agent:
        return False, (
            "no caller identity — pass --as-agent <a registered non-isolated agent> "
            "or set PINKY_AGENT_NAME (the daemon signs /admin with the shared secret "
            "only for a registered agent name)"
        )
    secret = os.environ.get("PINKY_SESSION_SECRET", "").strip()
    if not secret:
        # fall back to the .env the switch is managing (not the repo-root default)
        try:
            secret = read_live_value(read_env_lines(env_path), "PINKY_SESSION_SECRET") or ""
        except SystemExit:
            secret = ""
    if not secret:
        return False, "PINKY_SESSION_SECRET unavailable — cannot auth the restart call"
    try:
        import urllib.error
        import urllib.request

        from pinky_daemon.auth import build_internal_auth_headers
    except Exception as exc:  # noqa: BLE001 — import path may be absent off-box
        return False, f"cannot import daemon auth/urllib: {exc}"

    path = "/admin/restart"
    headers = build_internal_auth_headers(
        secret, agent_name=as_agent, method="POST", path=path
    )
    req = urllib.request.Request(
        _daemon_base_url() + path, method="POST", headers=headers, data=b""
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return (200 <= resp.status < 300), f"HTTP {resp.status}"
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code}"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def daemon_generation() -> str | None:
    """The running daemon's process-generation marker (``/api`` ``started_at``,
    a per-boot ``time.time()``), or None when /api is unreachable / not 2xx.

    This is the RESTART signal, not a bare liveness check: /admin/restart returns
    200 and only SIGTERMs ~1s later, and the OLD process keeps answering /api
    (same ``started_at``) until then — so a 200 alone is not proof of restart.
    A changed ``started_at`` means a genuinely new process is serving.
    """
    try:
        import json as _json
        import urllib.request

        with urllib.request.urlopen(_daemon_base_url() + "/api", timeout=5) as resp:
            if not (200 <= resp.status < 300):
                return None
            data = _json.loads(resp.read().decode("utf-8"))
    except Exception:  # noqa: BLE001
        return None
    started = data.get("started_at")
    return None if started is None else str(started)


# ── account metadata helpers ─────────────────────────────────────────────────


def days_left(meta: dict) -> int | None:
    exp = _parse_iso(meta.get("expires_at", ""))
    if not exp:
        return None
    return (exp - _utcnow()).days


def forwarding_on(env_path: Path) -> bool:
    """Whether the .env enables static-token forwarding to the fleet."""
    lines = read_env_lines(env_path)
    return (read_live_value(lines, FORWARD_FLAG_KEY) or "").lower() in (
        "1", "true", "yes", "on",
    )


def token_provenance(env_path: Path, index: dict) -> str | None:
    """Which stored account matches the .env's live token — IGNORING the flag.

    Constant-time compare against each stored token; the matching name, or None
    when the token is absent / matches nothing.
    """
    lines = read_env_lines(env_path)
    live = read_live_value(lines, TOKEN_ENV_KEY)
    if not live:
        return None
    for name in index.get("accounts", {}):
        try:
            stored = read_token(name)
        except SystemExit:
            continue
        if stored and hmac.compare_digest(live, stored):
            return name
    return None


def active_name(env_path: Path, index: dict) -> str | None:
    """The account the fleet is ACTUALLY running as: the live token matches a
    stored account AND forwarding is enabled. Returns None otherwise — a token
    present with forwarding off is not being used, so it is not 'active'.
    """
    if not forwarding_on(env_path):
        return None
    return token_provenance(env_path, index)


def suffix_of(token: str) -> str:
    return token[-4:] if len(token) >= 4 else "?"


# ── commands ─────────────────────────────────────────────────────────────────


def cmd_add(args: argparse.Namespace) -> int:
    if not valid_name(args.name):
        print(f"claude-account: invalid name {args.name!r}", file=sys.stderr)
        return 2
    index = load_index()
    if args.name in index["accounts"] and not args.force:
        print(
            f"claude-account: {args.name!r} already exists — pass --force to replace it",
            file=sys.stderr,
        )
        return 2

    if args.mint:
        binary = resolve_claude_bin(args.claude_bin)
        print(f"Launching `{binary} setup-token` — sign in as the target account…\n")
        subprocess.run([binary, "setup-token"], check=False)
        print()

    # Token via hidden prompt — NEVER on argv (would leak in ps / shell history).
    token = getpass.getpass("Paste the setup-token (input hidden): ").strip()
    if not token:
        print("claude-account: no token entered", file=sys.stderr)
        return 2
    if not token.startswith(SETUP_TOKEN_PREFIX) and not args.allow_any:
        print(
            f"claude-account: token does not look like a setup-token "
            f"({SETUP_TOKEN_PREFIX}…). Pass --allow-any to store it anyway.",
            file=sys.stderr,
        )
        return 2
    # Serialization safety is non-negotiable (even with --allow-any): the token is
    # written bare into a bash-sourced .env, so a newline / shell metachar would
    # inject a second assignment or be interpreted by the shell.
    if not token_serialization_safe(token):
        print(
            "claude-account: token contains characters unsafe to write into a "
            "shell-sourced .env (newline or shell metacharacter) — refusing.",
            file=sys.stderr,
        )
        return 2
    # Reject a token already stored under another name (constant-time): otherwise
    # provenance is ambiguous — switching to the second name would report success
    # while list/current still resolve the first.
    for other, meta in index["accounts"].items():
        if other == args.name:
            continue
        try:
            if hmac.compare_digest(token, read_token(other)):
                print(
                    f"claude-account: that exact token is already stored as {other!r} — "
                    f"refusing (duplicate token makes the active account ambiguous). "
                    f"Use {other!r}, or remove it first.",
                    file=sys.stderr,
                )
                return 2
        except SystemExit:
            continue

    if not args.no_probe:
        print("Isolation-probing the token…")
        ok, detail = probe_token(token, args.claude_bin)
        if not ok:
            print(f"claude-account: refusing to store — {detail}", file=sys.stderr)
            return 1
        print("  probe OK")
    probe_meta = {"ok": not args.no_probe, "at": _iso(_utcnow())}

    now = _utcnow()
    if args.expires_at:
        exp = _parse_iso(args.expires_at)
        if not exp:
            print(f"claude-account: bad --expires-at {args.expires_at!r}", file=sys.stderr)
            return 2
    else:
        exp = now + timedelta(days=args.ttl_days)
    billing = args.billing
    if billing not in BILLING_MODES:
        print(f"claude-account: --billing must be one of {BILLING_MODES}", file=sys.stderr)
        return 2

    write_token(args.name, token)
    index["accounts"][args.name] = {
        "account_label": args.label or args.name,
        "token_kind": "setup_token" if token.startswith(SETUP_TOKEN_PREFIX) else "other",
        "billing": billing,
        "added_at": _iso(now),
        "expires_at": _iso(exp),
        "token_suffix": suffix_of(token),
        "probe": probe_meta,
    }
    save_index(index)
    print(f"Stored {args.name!r} (…{suffix_of(token)}), expires {_iso(exp)}, billing={billing}.")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    index = load_index()
    accounts = index.get("accounts", {})
    if not accounts:
        print("No stored accounts. Add one with `claude-account add <name>`.")
        return 0
    try:
        active = active_name(args.env_file, index)
        prov = token_provenance(args.env_file, index)
    except SystemExit as exc:
        print(str(exc), file=sys.stderr)
        active = prov = None
    header = f"{'':2} {'NAME':16} {'BILLING':12} {'EXPIRES':22} {'DAYS':>5}  LABEL"
    print(header)
    for name in sorted(accounts):
        meta = accounts[name]
        mark = "*" if name == active else " "
        dl = days_left(meta)
        dl_s = "—" if dl is None else str(dl)
        warn = " ⚠" if (dl is not None and dl <= EXPIRY_WARN_DAYS) else ""
        print(
            f"{mark:2} {name:16} {meta.get('billing','?'):12} "
            f"{meta.get('expires_at','?'):22} {dl_s:>5}{warn}  {meta.get('account_label','')}"
        )
    if active is None:
        if prov:
            print(f"\n(forwarding is OFF — {prov!r} is in .env but not being forwarded)")
        else:
            print("\n(no stored token matches the live .env token — active account unknown)")
    return 0


def cmd_current(args: argparse.Namespace) -> int:
    index = load_index()
    lines = read_env_lines(args.env_file)
    flag = (read_live_value(lines, FORWARD_FLAG_KEY) or "").lower() in ("1", "true", "yes", "on")
    live = read_live_value(lines, TOKEN_ENV_KEY)
    prov = token_provenance(args.env_file, index)
    print(f"forward flag ({FORWARD_FLAG_KEY}): {'ON' if flag else 'OFF'}")
    if not live:
        print(f"{TOKEN_ENV_KEY}: (not set)")
    else:
        print(f"{TOKEN_ENV_KEY}: set (…{suffix_of(live)})")
    if prov and flag:
        meta = index["accounts"][prov]
        dl = days_left(meta)
        print(f"active account: {prov}  (billing={meta.get('billing','?')}, "
              f"expires {meta.get('expires_at','?')}, {dl if dl is not None else '?'}d left)")
    elif prov and not flag:
        print(f"active account: none — {prov!r} token is in .env but forwarding is OFF")
    elif live:
        print("active account: unknown (live token not in the store)")
    else:
        print("active account: none")
    if flag and not live:
        print("\n⚠ FAIL-OPEN RISK: forward flag is ON but no token is set — "
              "the fleet would hit a login wall on restart.")
    return 0


def cmd_switch(args: argparse.Namespace) -> int:
    if not valid_name(args.name):
        print(f"claude-account: invalid name {args.name!r}", file=sys.stderr)
        return 2
    index = load_index()
    if args.name not in index["accounts"]:
        print(f"claude-account: no stored account {args.name!r}", file=sys.stderr)
        return 2
    meta = index["accounts"][args.name]

    # Refuse a dead token before it can wall the fleet.
    dl = days_left(meta)
    if dl is not None and dl < 0 and not args.force:
        print(
            f"claude-account: {args.name!r} expired {abs(dl)}d ago "
            f"({meta.get('expires_at')}) — refusing. Re-add it, or pass --force.",
            file=sys.stderr,
        )
        return 1

    token = read_token(args.name)
    # Fail-closed backstop independent of the probe: an empty/blanked token file
    # must NEVER be written together with the flag (that IS the login wall). This
    # is the only guard on the --no-probe path.
    if not token.strip():
        print(
            f"claude-account: stored token for {args.name!r} is empty — refusing "
            f"(would wall the fleet). Re-add it with `claude-account add {args.name} --force`.",
            file=sys.stderr,
        )
        return 1
    # Guard against a hand-edited token file that would inject into the .env the
    # macOS service sources with bash (newline → second assignment; metachars).
    if not token_serialization_safe(token):
        print(
            f"claude-account: stored token for {args.name!r} is not a single "
            f"shell-safe line — refusing to write it into .env. Re-add it.",
            file=sys.stderr,
        )
        return 1

    env_path = args.env_file
    # active_name is flag-aware: this is true only when the token already matches
    # AND forwarding is already on. A token match with the flag OFF falls through
    # so the switch actually sets the flag (the fix for the silent no-op).
    if active_name(env_path, index) == args.name and not args.force:
        print(f"Already on {args.name!r} with forwarding on; nothing to do "
              f"(pass --force to rewrite anyway).")
        return 0

    if args.dry_run:
        flag = "ON" if forwarding_on(env_path) else "OFF"
        print(f"[dry-run] would set {TOKEN_ENV_KEY}=(…{suffix_of(token)}) and "
              f"{FORWARD_FLAG_KEY}=1 in {env_path} (forwarding currently {flag}). "
              f"No write, no backup, no probe.")
        return 0

    if not args.no_probe:
        print("Pre-switch isolation-probe…")
        ok, detail = probe_token(token, args.claude_bin)
        if not ok:
            print(
                f"claude-account: refusing to switch — {detail}. "
                f"The current .env is untouched.",
                file=sys.stderr,
            )
            return 1
        print("  probe OK")

    lines = read_env_lines(env_path)
    backup = backup_env(env_path, args.name)
    # Fail-closed invariant: token + forward flag land in the SAME atomic write —
    # there is never a persisted state with flag=1 and no token.
    new_lines = set_env_var(lines, TOKEN_ENV_KEY, token)
    new_lines = set_env_var(new_lines, FORWARD_FLAG_KEY, "1")
    write_env_lines(env_path, new_lines)
    print(f"Wrote {env_path} (backup: {backup}). Active token → {args.name} (…{suffix_of(token)}).")

    if not args.restart:
        print("\nNot restarting (the daemon only picks up .env on restart). Next:")
        print("  • restart the daemon (re-run with --restart, or the box's restart command)")
        print("  • then spot-check a few agent panes for a login wall.")
        return 0

    as_agent = (args.as_agent or os.environ.get("PINKY_AGENT_NAME", "")).strip()
    # Capture the process generation BEFORE the restart. /admin/restart returns
    # 200 and only SIGTERMs ~1s later, and the OLD process keeps answering /api
    # until then — so a bare 200 is NOT proof of restart. We require started_at to
    # ADVANCE (a genuinely new process) before declaring success. (retry a few
    # times: the daemon is up — /admin/restart is about to 200 — so a None here is
    # a transient miss, not "down".)
    before_gen = None
    for _ in range(3):
        before_gen = daemon_generation()
        if before_gen is not None:
            break

    print("Restarting the daemon…")
    ok, detail = restart_daemon(env_path, as_agent)
    if not ok:
        _revert(env_path, backup, f"restart call failed ({detail})")
        return 1

    if before_gen is None:
        # Restart accepted but no baseline to verify against — do NOT claim success
        # and do NOT revert (the restart likely proceeded; reverting could undo a
        # good switch). Hand it to the operator.
        print(
            "claude-account: restart accepted but the daemon's pre-restart generation "
            "was unreadable, so the restart could not be auto-verified. The new token is "
            "written and NOT reverted — confirm the daemon came back and spot-check panes "
            "by hand.",
            file=sys.stderr,
        )
        return 1

    import time

    deadline = time.monotonic() + RESTART_HEALTH_TIMEOUT_S
    while time.monotonic() < deadline:
        cur = daemon_generation()
        if cur is not None and cur != before_gen:
            print("Daemon restarted — a new process is serving.")
            print("⚠ Spot-check a few agent panes — --print auth passing does not "
                  "guarantee no Fable-credit consent prompt at real startup.")
            return 0
        time.sleep(3)
    _revert(env_path, backup,
            f"daemon generation did not advance within {RESTART_HEALTH_TIMEOUT_S}s "
            f"(restart not confirmed — the new process never came up)")
    return 1


def _revert(env_path: Path, backup: Path, why: str) -> None:
    print(f"\nclaude-account: {why} — reverting {env_path} from {backup.name}.",
          file=sys.stderr)
    try:
        # 0600 — the restored .env still holds credentials (clamp, don't preserve).
        _atomic_copy(backup, env_path, 0o600)
        print("Reverted. Restart the daemon to restore the previous token.", file=sys.stderr)
    except OSError as exc:
        print(f"REVERT FAILED ({exc}) — restore {backup} to {env_path} by hand.",
              file=sys.stderr)


def cmd_remove(args: argparse.Namespace) -> int:
    if not valid_name(args.name):
        print(f"claude-account: invalid name {args.name!r}", file=sys.stderr)
        return 2
    index = load_index()
    if args.name not in index["accounts"]:
        print(f"claude-account: no stored account {args.name!r}", file=sys.stderr)
        return 2
    # Refuse if the live .env token IS this account's token — regardless of the
    # forward flag. Deleting a token that literally sits in .env is dangerous even
    # with forwarding off (someone could flip the flag on later).
    if token_provenance(args.env_file, index) == args.name and not args.force:
        print(
            f"claude-account: {args.name!r} is the token currently in .env — refusing. "
            f"Switch away first, or pass --force.",
            file=sys.stderr,
        )
        return 1
    tp = token_path(args.name)
    if tp.exists():
        tp.unlink()
    del index["accounts"][args.name]
    save_index(index)
    print(f"Removed {args.name!r}.")
    return 0


def cmd_check_expiry(args: argparse.Namespace) -> int:
    index = load_index()
    active = active_name(args.env_file, index)
    if not active:
        print("check-expiry: no active account matched (nothing to warn on).")
        return 0
    meta = index["accounts"][active]
    dl = days_left(meta)
    if dl is None:
        print(f"check-expiry: active account {active!r} has no expiry recorded.")
        return 0
    if dl <= args.days:
        print(
            f"check-expiry: ACTIVE token {active!r} expires in {dl}d "
            f"({meta.get('expires_at')}). Mint a fresh token and switch.",
        )
        return 3  # non-zero → a scheduled wake can turn this into an owner nudge
    print(f"check-expiry: {active!r} OK ({dl}d left).")
    return 0


# ── argument parsing ─────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="claude-account", description=__doc__.splitlines()[0])
    p.add_argument("--env-file", type=Path, default=None,
                   help="path to .env (default: repo-root .env / PINKY_ENV_FILE)")
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("add", help="mint/paste + probe + store a token")
    a.add_argument("name")
    a.add_argument("--label", default="", help="human account label (e.g. email)")
    a.add_argument("--billing", default="unknown", choices=BILLING_MODES,
                   help="owner-declared billing mode (subscription | api_usage | unknown)")
    a.add_argument("--ttl-days", type=int, default=DEFAULT_TTL_DAYS)
    a.add_argument("--expires-at", default="", help="explicit ISO expiry (overrides --ttl-days)")
    a.add_argument("--mint", action="store_true", help="launch `claude setup-token` first")
    a.add_argument("--no-probe", action="store_true", help="skip the isolation-probe (unsafe)")
    a.add_argument("--allow-any", action="store_true", help="accept a non setup-token string")
    a.add_argument("--claude-bin", default="", help="path to the claude binary")
    a.add_argument("--force", action="store_true", help="replace an existing entry")
    a.set_defaults(func=cmd_add)

    li = sub.add_parser("list", help="stored accounts + active + expiry")
    li.set_defaults(func=cmd_list)

    c = sub.add_parser("current", help="which token the .env currently uses")
    c.set_defaults(func=cmd_current)

    s = sub.add_parser("switch", help="safe swap (backup + fail-closed write [+restart])")
    s.add_argument("name")
    s.add_argument("--restart", action="store_true", help="also restart the daemon")
    s.add_argument("--as-agent", default="",
                   help="identity to sign the --restart call as (a registered "
                        "non-isolated agent; falls back to PINKY_AGENT_NAME)")
    s.add_argument("--dry-run", action="store_true", help="preview the .env change without writing")
    s.add_argument("--no-probe", action="store_true", help="skip the pre-switch probe (unsafe)")
    s.add_argument("--claude-bin", default="")
    s.add_argument("--force", action="store_true", help="switch even if expired / already active")
    s.set_defaults(func=cmd_switch)

    r = sub.add_parser("remove", help="delete a stored token")
    r.add_argument("name")
    r.add_argument("--force", action="store_true", help="remove even the active account")
    r.set_defaults(func=cmd_remove)

    e = sub.add_parser("check-expiry", help="nudge (exit 3) if the active token expires soon")
    e.add_argument("--days", type=int, default=EXPIRY_WARN_DAYS)
    e.set_defaults(func=cmd_check_expiry)
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    # resolve the env path once, after parsing (default is lazy so tests can patch)
    args.env_file = args.env_file or default_env_path()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
