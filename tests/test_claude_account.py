"""Tests for scripts/claude_account.py (#570 account-switch tooling).

Focus is the risky, security-critical core: the ``.env`` rewrite (exactly one
live line per key, comment/substring edges), the fail-closed switch (token +
flag written together, backup, revert), file permissions, and provenance. The
live daemon restart and the real ``claude`` probe are integration paths and are
stubbed here.
"""

from __future__ import annotations

import fcntl
import importlib.util
import os
import stat
from datetime import timedelta
from pathlib import Path

import pytest

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "claude_account.py"
SPEC = importlib.util.spec_from_file_location("claude_account", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
ca = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ca)


# ── fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def store(tmp_path, monkeypatch):
    """Point the token store + env file at a tmp dir; stub the probe to pass."""
    accounts = tmp_path / "accounts"
    monkeypatch.setenv("PINKY_ACCOUNTS_DIR", str(accounts))
    env_path = tmp_path / ".env"
    monkeypatch.setenv("PINKY_ENV_FILE", str(env_path))
    # default: probe passes, so store/switch logic is exercised without claude.
    monkeypatch.setattr(ca, "probe_token", lambda tok, cb="": (True, "ok"))
    return {"accounts": accounts, "env": env_path}


def write_env(path: Path, text: str) -> None:
    path.write_text(text)
    os.chmod(path, 0o600)


def run(*argv) -> int:
    return ca.main(list(argv))


# ── _is_live_assignment / set_env_var / read_live_value ─────────────────────


def test_is_live_assignment_edges():
    assert ca._is_live_assignment("FOO=1", "FOO")
    assert ca._is_live_assignment("  FOO = 1 ", "FOO")
    assert not ca._is_live_assignment("#FOO=1", "FOO")
    assert not ca._is_live_assignment("# FOO=1", "FOO")
    assert not ca._is_live_assignment("FOO_BAR=1", "FOO")  # substring must not match
    assert not ca._is_live_assignment("BARFOO=1", "FOO")
    assert not ca._is_live_assignment("", "FOO")
    assert not ca._is_live_assignment("FOO", "FOO")  # no '='


def test_set_env_var_replaces_first_live_and_drops_duplicates():
    lines = ["A=1", "TOK=old", "B=2", "TOK=stale", "C=3"]
    out = ca.set_env_var(lines, "TOK", "new")
    assert out.count("TOK=new") == 1
    assert not any(line == "TOK=stale" or line == "TOK=old" for line in out)
    # position of the first occurrence is preserved
    assert out == ["A=1", "TOK=new", "B=2", "C=3"]


def test_set_env_var_appends_when_absent():
    out = ca.set_env_var(["A=1"], "TOK", "new")
    assert out == ["A=1", "TOK=new"]


def test_set_env_var_leaves_comments_and_appends_live():
    # a commented-out key is NOT a live assignment → a live one is appended
    out = ca.set_env_var(["#TOK=old", "A=1"], "TOK", "new")
    assert "#TOK=old" in out  # comment preserved
    assert out.count("TOK=new") == 1
    assert ca.read_live_value(out, "TOK") == "new"


def test_read_live_value_last_wins_and_strips_quotes():
    # bash `source` is last-wins — the authoritative loader on the Mini
    assert ca.read_live_value(['TOK="quoted"', "TOK=second"], "TOK") == "second"
    assert ca.read_live_value(["#TOK=commented", "TOK=live"], "TOK") == "live"
    assert ca.read_live_value(["A=1"], "TOK") is None


def test_export_assignments_are_live():
    # bash `source .env` honors `export KEY=…`; the tool must treat it as live
    assert ca._live_key("export TOK=v") == "TOK"
    assert ca._live_key("  export   TOK=v") == "TOK"
    assert ca._live_key("exportTOK=v") == "exportTOK"  # not the `export ` prefix
    assert ca.read_live_value(["export TOK=fromexport"], "TOK") == "fromexport"
    # set_env_var collapses a bare + export pair to ONE canonical bare line
    out = ca.set_env_var(["export TOK=old", "A=1", "TOK=older"], "TOK", "new")
    assert [line for line in out if ca._live_key(line) == "TOK"] == ["TOK=new"]
    assert ca.read_live_value(out, "TOK") == "new"


# ── valid_name (path-traversal guard) ────────────────────────────────────────


@pytest.mark.parametrize("bad", ["", "..", ".", "a/b", "a b", "../etc", "x" * 65, "a\tb"])
def test_valid_name_rejects(bad):
    assert not ca.valid_name(bad)


@pytest.mark.parametrize("good", ["brad-main", "ryan_2026", "acct.1", "A1"])
def test_valid_name_accepts(good):
    assert ca.valid_name(good)


# ── token store perms + roundtrip ────────────────────────────────────────────


def test_write_token_roundtrip_and_perms(store):
    ca.write_token("acct", "sk-ant-oat01-SECRET")
    assert ca.read_token("acct") == "sk-ant-oat01-SECRET"
    mode = stat.S_IMODE(os.stat(ca.token_path("acct")).st_mode)
    assert mode == 0o600
    dir_mode = stat.S_IMODE(os.stat(ca.store_dir()).st_mode)
    assert dir_mode == 0o700


def test_index_roundtrip_perms(store):
    ca.save_index({"version": 1, "accounts": {"x": {"billing": "subscription"}}})
    data = ca.load_index()
    assert data["accounts"]["x"]["billing"] == "subscription"
    assert stat.S_IMODE(os.stat(ca.index_path()).st_mode) == 0o600


# ── backup + atomic write ────────────────────────────────────────────────────


def test_backup_env_into_store_dir_0600(store):
    write_env(store["env"], "A=1\n")
    backup = ca.backup_env(store["env"], "acct")
    assert backup.exists()
    # backups live in the 0700 store dir OUTSIDE the repo tree, not beside .env
    assert backup.parent == ca.backup_dir()
    assert backup.parent != store["env"].parent
    assert backup.name.startswith(".env.bak.pre-acct-")
    assert backup.read_text() == "A=1\n"
    assert stat.S_IMODE(os.stat(backup).st_mode) == 0o600


def test_atomic_write_sets_mode(tmp_path):
    p = tmp_path / "secret"
    ca._atomic_write(p, "data\n", 0o600)
    assert p.read_text() == "data\n"
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600


# ── days_left / active_name / suffix ─────────────────────────────────────────


def test_days_left_math():
    future = ca._iso(ca._utcnow() + timedelta(days=10))
    assert ca.days_left({"expires_at": future}) in (9, 10)
    assert ca.days_left({}) is None


def test_active_name_provenance(store):
    ca.write_token("acct", "sk-ant-oat01-LIVE")
    ca.save_index({"version": 1, "accounts": {"acct": {"expires_at": ""}}})
    write_env(store["env"], "CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-LIVE\nPINKY_FORWARD_OAUTH_TOKEN=1\n")
    index = ca.load_index()
    assert ca.active_name(store["env"], index) == "acct"


def test_active_name_none_when_token_absent(store):
    ca.write_token("acct", "sk-ant-oat01-LIVE")
    ca.save_index({"version": 1, "accounts": {"acct": {}}})
    write_env(store["env"], "PINKY_FORWARD_OAUTH_TOKEN=1\n")  # no token line
    assert ca.active_name(store["env"], ca.load_index()) is None


# ── switch: fail-closed, backup, revert ──────────────────────────────────────


def _seed_account(store, name="acct", token="sk-ant-oat01-NEW", days=300):
    ca.write_token(name, token)
    exp = ca._iso(ca._utcnow() + timedelta(days=days))
    ca.save_index({"version": 1, "accounts": {name: {"expires_at": exp, "billing": "subscription"}}})


def test_switch_writes_token_and_flag_together(store):
    _seed_account(store)
    write_env(store["env"], "A=1\nCLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-OLD\n")
    rc = run("switch", "acct")
    assert rc == 0
    text = store["env"].read_text()
    assert "CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-NEW" in text
    assert "PINKY_FORWARD_OAUTH_TOKEN=1" in text
    # a backup was made in the store dir (not beside .env in the repo)
    backups = list(ca.backup_dir().glob(".env.bak.pre-acct-*"))
    assert len(backups) == 1
    assert "sk-ant-oat01-OLD" in backups[0].read_text()
    assert not list(store["env"].parent.glob(".env.bak.pre-*"))  # nothing in the repo tree


def test_switch_never_leaves_flag_on_without_token(store):
    """Even starting from a bare .env, the write sets both keys atomically."""
    _seed_account(store)
    write_env(store["env"], "A=1\n")
    run("switch", "acct")
    lines = store["env"].read_text().splitlines()
    tok = ca.read_live_value(lines, "CLAUDE_CODE_OAUTH_TOKEN")
    flag = ca.read_live_value(lines, "PINKY_FORWARD_OAUTH_TOKEN")
    assert flag == "1"
    assert tok  # non-empty — never flag-on-token-missing


def test_switch_refuses_expired_token(store, capsys):
    _seed_account(store, days=-5)
    write_env(store["env"], "CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-OLD\n")
    rc = run("switch", "acct")
    assert rc == 1
    assert "CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-OLD" in store["env"].read_text()  # untouched


def test_switch_refuses_on_probe_failure(store, monkeypatch):
    _seed_account(store)
    monkeypatch.setattr(ca, "probe_token", lambda tok, cb="": (False, "bad token"))
    write_env(store["env"], "CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-OLD\n")
    rc = run("switch", "acct")
    assert rc == 1
    assert "sk-ant-oat01-OLD" in store["env"].read_text()  # untouched, no write


def test_switch_dry_run_no_write(store):
    _seed_account(store)
    write_env(store["env"], "CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-OLD\n")
    rc = run("switch", "acct", "--dry-run")
    assert rc == 0
    # dry-run previews only: .env untouched and NO backup file left behind
    assert store["env"].read_text() == "CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-OLD\n"
    assert not list(ca.backup_dir().glob(".env.bak.pre-*"))


def test_switch_already_active_is_noop(store, capsys):
    _seed_account(store, token="sk-ant-oat01-SAME")
    write_env(store["env"], "CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-SAME\nPINKY_FORWARD_OAUTH_TOKEN=1\n")
    rc = run("switch", "acct")
    assert rc == 0
    assert "nothing to do" in capsys.readouterr().out.lower()
    assert not list(ca.backup_dir().glob(".env.bak.pre-*"))  # no backup churn


def test_switch_restart_bypasses_already_active(store, monkeypatch):
    """--restart must (re)start even when .env already matches: the two-step
    workflow is `switch` (writes) then `switch --restart` (applies). The second
    invocation must NOT no-op — the running daemon may still hold the old token."""
    _seed_account(store, token="sk-ant-oat01-SAME")
    write_env(store["env"], "CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-SAME\nPINKY_FORWARD_OAUTH_TOKEN=1\n")
    calls = []

    def _restart(env_path, as_agent):
        calls.append(1)
        return True, "HTTP 200"

    monkeypatch.setattr(ca, "restart_daemon", _restart)
    gens = iter(["old-gen", "new-gen"])
    monkeypatch.setattr(ca, "daemon_generation", lambda: next(gens, "new-gen"))
    rc = run("switch", "acct", "--restart", "--as-agent", "x")
    assert rc == 0
    assert calls == [1]  # restart WAS performed, not skipped as "nothing to do"


def test_switch_token_matches_but_flag_off_proceeds(store):
    """Token already in .env but forwarding OFF → switch must set the flag (not no-op)."""
    _seed_account(store, token="sk-ant-oat01-SAME")
    write_env(store["env"], "CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-SAME\nPINKY_FORWARD_OAUTH_TOKEN=0\n")
    rc = run("switch", "acct")
    assert rc == 0
    lines = store["env"].read_text().splitlines()
    assert ca.read_live_value(lines, "PINKY_FORWARD_OAUTH_TOKEN") == "1"


def test_switch_empty_token_file_refuses(store):
    """A blanked token file must never be written with the flag, even with --no-probe."""
    _seed_account(store)
    ca.write_token("acct", "")  # blank the token file
    write_env(store["env"], "CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-OLD\n")
    rc = run("switch", "acct", "--no-probe")
    assert rc == 1
    assert store["env"].read_text() == "CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-OLD\n"  # untouched


def test_switch_restart_success_when_generation_advances(store, monkeypatch):
    _seed_account(store)
    write_env(store["env"], "CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-OLD\n")
    monkeypatch.setattr(ca, "restart_daemon", lambda env_path, as_agent: (True, "HTTP 200"))
    # baseline "old-gen" then a new process "new-gen" → success
    gens = iter(["old-gen", "new-gen"])
    monkeypatch.setattr(ca, "daemon_generation", lambda: next(gens, "new-gen"))
    rc = run("switch", "acct", "--restart", "--as-agent", "someagent")
    assert rc == 0
    assert "sk-ant-oat01-NEW" in store["env"].read_text()  # kept, not reverted


def test_switch_restart_reverts_on_unchanged_generation(store, monkeypatch):
    """The false-green case: the OLD process still answers /api (same started_at)
    but never restarts → must NOT be accepted; revert instead."""
    _seed_account(store)
    write_env(store["env"], "CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-OLD\n")
    monkeypatch.setattr(ca, "restart_daemon", lambda env_path, as_agent: (True, "HTTP 200"))
    monkeypatch.setattr(ca, "daemon_generation", lambda: "same-gen")  # never advances
    monkeypatch.setattr(ca, "RESTART_HEALTH_TIMEOUT_S", 0)
    rc = run("switch", "acct", "--restart", "--as-agent", "someagent")
    assert rc == 1
    # atomic revert restored the exact original .env
    assert store["env"].read_text() == "CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-OLD\n"


def test_switch_restart_unreadable_baseline_aborts_untouched(store, monkeypatch):
    """No pre-restart generation to verify against → abort BEFORE writing; never
    persist an unverifiable switch, and never POST the restart."""
    _seed_account(store)
    write_env(store["env"], "CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-OLD\n")
    monkeypatch.setattr(ca, "daemon_generation", lambda: None)  # never readable

    def _boom(*a, **k):
        raise AssertionError("restart must not be POSTed when the baseline is unreadable")

    monkeypatch.setattr(ca, "restart_daemon", _boom)
    rc = run("switch", "acct", "--restart", "--as-agent", "someagent")
    assert rc == 1
    assert store["env"].read_text() == "CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-OLD\n"  # untouched
    assert not list(ca.backup_dir().glob(".env.bak.pre-*"))  # no backup, no write


def test_switch_restart_without_identity_aborts_untouched(store, monkeypatch):
    _seed_account(store)
    write_env(store["env"], "CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-OLD\n")
    monkeypatch.delenv("PINKY_AGENT_NAME", raising=False)

    def _boom(*a, **k):
        raise AssertionError("must not restart without an identity")

    monkeypatch.setattr(ca, "restart_daemon", _boom)
    rc = run("switch", "acct", "--restart")  # no --as-agent, no PINKY_AGENT_NAME
    assert rc == 1
    assert store["env"].read_text() == "CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-OLD\n"  # untouched
    assert not list(ca.backup_dir().glob(".env.bak.pre-*"))


def test_restart_daemon_requires_identity(store):
    ok, detail = ca.restart_daemon(store["env"], "")
    assert ok is False
    assert "identity" in detail.lower()


# ── advisory lock / directory-mode fail-closed ───────────────────────────────


def test_switch_refuses_when_lock_held(store):
    _seed_account(store)
    write_env(store["env"], "CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-OLD\n")
    ca.ensure_store()
    fd = os.open(str(ca.store_dir() / ".lock"), os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # hold it (simulates a concurrent op)
    try:
        with pytest.raises(SystemExit):
            run("switch", "acct")
        assert store["env"].read_text() == "CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-OLD\n"  # untouched
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def test_ensure_store_fails_closed_on_permissive_dir(store, monkeypatch):
    d = ca.store_dir()
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(d, 0o777)
    monkeypatch.setattr(ca.os, "chmod", lambda *a, **k: None)  # simulate chmod denied
    with pytest.raises(SystemExit):
        ca.ensure_store()


# ── remove ───────────────────────────────────────────────────────────────────


def test_remove_refuses_active_without_force(store, capsys):
    _seed_account(store, token="sk-ant-oat01-LIVE")
    write_env(store["env"], "CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-LIVE\n")
    rc = run("remove", "acct")
    assert rc == 1
    assert ca.token_path("acct").exists()  # not deleted


def test_remove_force_deletes_active(store):
    _seed_account(store, token="sk-ant-oat01-LIVE")
    write_env(store["env"], "CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-LIVE\n")
    rc = run("remove", "acct", "--force")
    assert rc == 0
    assert not ca.token_path("acct").exists()
    assert "acct" not in ca.load_index()["accounts"]


# ── check-expiry ─────────────────────────────────────────────────────────────


def test_check_expiry_warns_within_window(store):
    _seed_account(store, token="sk-ant-oat01-LIVE", days=10)
    write_env(store["env"], "CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-LIVE\nPINKY_FORWARD_OAUTH_TOKEN=1\n")
    assert run("check-expiry", "--days", "30") == 3


def test_check_expiry_ok_outside_window(store):
    _seed_account(store, token="sk-ant-oat01-LIVE", days=200)
    write_env(store["env"], "CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-LIVE\nPINKY_FORWARD_OAUTH_TOKEN=1\n")
    assert run("check-expiry", "--days", "30") == 0


def test_check_expiry_no_active(store):
    ca.save_index({"version": 1, "accounts": {}})
    write_env(store["env"], "A=1\n")
    assert run("check-expiry") == 0


def test_check_expiry_no_nudge_when_forwarding_off(store):
    """Near-expiry but forwarding OFF → not fleet-active → no spurious nudge."""
    _seed_account(store, token="sk-ant-oat01-LIVE", days=5)
    write_env(store["env"], "CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-LIVE\nPINKY_FORWARD_OAUTH_TOKEN=0\n")
    assert run("check-expiry", "--days", "30") == 0


# ── active_name flag-awareness / provenance / index guards ───────────────────


def test_active_name_none_when_forwarding_off(store):
    ca.write_token("acct", "sk-ant-oat01-LIVE")
    ca.save_index({"version": 1, "accounts": {"acct": {}}})
    write_env(store["env"], "CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-LIVE\nPINKY_FORWARD_OAUTH_TOKEN=0\n")
    index = ca.load_index()
    assert ca.active_name(store["env"], index) is None       # forwarding off → not active
    assert ca.token_provenance(store["env"], index) == "acct"  # but the token matches


def test_load_index_rejects_non_dict_accounts(store):
    ca.ensure_store()
    ca.index_path().write_text('{"version":1,"accounts":null}')
    with pytest.raises(SystemExit):
        ca.load_index()


def test_load_index_rejects_bad_utf8(store):
    ca.ensure_store()
    ca.index_path().write_bytes(b"\xff\xfe not utf-8 \x80")
    with pytest.raises(SystemExit):
        ca.load_index()


def test_days_left_naive_timestamp_does_not_crash():
    # a hand-edited bare date (naive) must not raise on aware/naive subtraction
    assert isinstance(ca.days_left({"expires_at": "2099-01-01"}), int)


# ── add ──────────────────────────────────────────────────────────────────────


def test_add_stores_metadata(store, monkeypatch):
    monkeypatch.setattr(ca.getpass, "getpass", lambda prompt="": "sk-ant-oat01-ADDED")
    rc = run("add", "brad", "--label", "bradbrok", "--billing", "subscription")
    assert rc == 0
    idx = ca.load_index()["accounts"]["brad"]
    assert idx["billing"] == "subscription"
    assert idx["token_kind"] == "setup_token"
    assert "token_suffix" not in idx  # no token-derived data stored
    assert ca.read_token("brad") == "sk-ant-oat01-ADDED"


def test_add_rejects_non_setup_token(store, monkeypatch, capsys):
    monkeypatch.setattr(ca.getpass, "getpass", lambda prompt="": "not-a-token")
    rc = run("add", "x")
    assert rc == 2
    assert "brad" not in ca.load_index()["accounts"]


def test_add_allow_any_accepts_other(store, monkeypatch):
    monkeypatch.setattr(ca.getpass, "getpass", lambda prompt="": "custom-token-xyz")
    rc = run("add", "x", "--allow-any", "--billing", "unknown")
    assert rc == 0
    assert ca.load_index()["accounts"]["x"]["token_kind"] == "other"


def test_add_refuses_duplicate_without_force(store, monkeypatch):
    monkeypatch.setattr(ca.getpass, "getpass", lambda prompt="": "sk-ant-oat01-ONE")
    assert run("add", "x") == 0
    monkeypatch.setattr(ca.getpass, "getpass", lambda prompt="": "sk-ant-oat01-TWO")
    assert run("add", "x") == 2  # duplicate refused
    assert ca.read_token("x") == "sk-ant-oat01-ONE"  # original kept


def test_add_probe_failure_does_not_store(store, monkeypatch):
    monkeypatch.setattr(ca.getpass, "getpass", lambda prompt="": "sk-ant-oat01-BAD")
    monkeypatch.setattr(ca, "probe_token", lambda tok, cb="": (False, "nope"))
    rc = run("add", "x")
    assert rc == 1
    assert "x" not in ca.load_index()["accounts"]
    assert not ca.token_path("x").exists()


def test_add_never_spawns_a_token_printing_child(store, monkeypatch):
    """The tool must never spawn `claude setup-token` — that flow prints the
    long-lived token to stdout, which inherited stdio would leak."""
    monkeypatch.setattr(ca.getpass, "getpass", lambda prompt="": "sk-ant-oat01-X")
    calls = []
    monkeypatch.setattr(ca.subprocess, "run", lambda *a, **k: calls.append(list(a[0]) if a else []))
    assert run("add", "x") == 0
    assert all("setup-token" not in part for c in calls for part in c)


def test_mint_flag_is_removed():
    with pytest.raises(SystemExit):  # argparse rejects the unknown flag
        ca.build_parser().parse_args(["add", "x", "--mint"])


# ── serialization safety / duplicate / perms / probe hygiene ─────────────────


def test_add_rejects_shell_unsafe_token(store, monkeypatch):
    # the Beaverton-style injection: a newline would write a SECOND live .env line
    injected = "sk-ant-oat01-GOOD\nCLAUDE_CODE_OAUTH_TOKEN="
    monkeypatch.setattr(ca.getpass, "getpass", lambda prompt="": injected)
    assert run("add", "x", "--allow-any") == 2
    assert "x" not in ca.load_index()["accounts"]


def test_switch_rejects_shell_unsafe_token_file(store):
    _seed_account(store)
    ca.write_token("acct", "sk-ant-oat01-GOOD\nPINKY_FORWARD_OAUTH_TOKEN=1")  # hand-edited injection
    write_env(store["env"], "CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-OLD\n")
    rc = run("switch", "acct", "--no-probe")
    assert rc == 1
    assert store["env"].read_text() == "CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-OLD\n"  # untouched


def test_add_rejects_duplicate_token_under_different_name(store, monkeypatch):
    monkeypatch.setattr(ca.getpass, "getpass", lambda prompt="": "sk-ant-oat01-SHARED")
    assert run("add", "first") == 0
    monkeypatch.setattr(ca.getpass, "getpass", lambda prompt="": "sk-ant-oat01-SHARED")
    assert run("add", "second") == 2  # same token, different name → ambiguous
    assert "second" not in ca.load_index()["accounts"]


def test_switch_clamps_env_to_0600_from_permissive_source(store):
    _seed_account(store)
    store["env"].write_text("CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-OLD\n")
    os.chmod(store["env"], 0o644)  # permissive .env
    rc = run("switch", "acct")
    assert rc == 0
    assert stat.S_IMODE(os.stat(store["env"]).st_mode) == 0o600  # clamped closed


def test_probe_token_does_not_leak_the_candidate(monkeypatch):
    tok = "sk-ant-oat01-SUPERSECRETVALUE"

    class FakeProc:
        returncode = 1
        stdout = ""
        stderr = f"error: invalid token {tok} was rejected"

    monkeypatch.setattr(ca, "resolve_claude_bin", lambda explicit="": "/bin/true")
    monkeypatch.setattr(ca.subprocess, "run", lambda *a, **k: FakeProc())
    ok, detail = ca.probe_token(tok)
    assert ok is False
    assert tok not in detail
    assert "SUPERSECRET" not in detail
