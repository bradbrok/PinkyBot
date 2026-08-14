"""Per-agent Codex home isolation, auth, and rollout migration tests."""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import stat
import threading
import time
import tomllib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from pinky_daemon import codex_home as codex_home_mod
from pinky_daemon.codex_app_server_tmux import (
    CodexAppServerSupervisor,
    _TmuxAppServerProc,
)
from pinky_daemon.codex_home import (
    MANAGED_CONFIG_SENTINEL,
    PER_AGENT_CODEX_HOME_ENV,
    ROLLOUT_MIGRATION_MARKER,
    codex_home_for,
    move_matching_rollouts,
    prepare_agent_codex_home,
    rollback_agent_rollouts,
)
from pinky_daemon.codex_session import CodexSession
from pinky_daemon.codex_tmux_session import CodexTmuxSession
from pinky_daemon.codex_tmux_transcript import _discover_codex_rollout
from pinky_daemon.streaming_session import StreamingSessionConfig
from pinky_daemon.tmux_session import TmuxCommandResult, TmuxSession, _TmuxControl


def _scope(
    working_dir: Path,
    *,
    codex_home: str = "",
    system_prompt: str = "",
) -> SimpleNamespace:
    return SimpleNamespace(
        agent_name="test-agent",
        working_dir=str(working_dir),
        codex_home=codex_home,
        system_prompt=system_prompt,
    )


class _SoulStore:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    def save_soul_version(
        self,
        agent_name: str,
        content: str,
        source: str = "unknown",
    ) -> int:
        self.calls.append((agent_name, content, source))
        return len(self.calls)


def _auth(shared_home: Path) -> Path:
    shared_home.mkdir(parents=True, exist_ok=True)
    path = shared_home / "auth.json"
    path.write_text('{"test": true}\n', encoding="utf-8")
    path.chmod(0o600)
    return path


def _rollout(home: Path, name: str, cwd: Path) -> Path:
    path = home / "sessions" / "2026" / "08" / "13" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "type": "session_meta",
                "payload": {"id": name, "cwd": str(cwd.resolve())},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def _mark_tmux_server_socket(tmp_path: Path, monkeypatch) -> Path:
    """Make the verifier take its server-alive session-listing leg."""
    tmux_tmpdir = tmp_path / "tmux-tmp"
    socket_path = tmux_tmpdir / f"tmux-{os.getuid()}" / "default"
    socket_path.parent.mkdir(parents=True)
    socket_path.touch()
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.setenv("TMUX_TMPDIR", str(tmux_tmpdir))
    return socket_path


def test_flag_off_preserves_shared_path_and_performs_no_bootstrap(tmp_path, monkeypatch):
    shared_home = tmp_path / "shared"
    working_dir = tmp_path / "agent"
    override = tmp_path / "override"
    monkeypatch.setenv("CODEX_HOME", str(shared_home))
    monkeypatch.delenv(PER_AGENT_CODEX_HOME_ENV, raising=False)
    monkeypatch.setenv(codex_home_mod.MIGRATION_LOCK_TIMEOUT_ENV, "invalid")
    scope = _scope(
        working_dir,
        codex_home=str(override),
        system_prompt="compiled soul",
    )
    soul_store = _SoulStore()

    assert codex_home_for(scope) == shared_home
    assert prepare_agent_codex_home(
        scope,
        log=lambda _message: None,
        soul_version_store=soul_store,
    ) == shared_home
    assert not working_dir.exists()
    assert not override.exists()
    assert not shared_home.exists()
    assert soul_store.calls == []


def test_prepare_writes_agent_soul_and_snapshots_self_edit_before_overwrite(
    tmp_path,
    monkeypatch,
):
    shared_home = tmp_path / "shared"
    working_dir = tmp_path / "agent"
    agent_home = working_dir / ".codex"
    working_dir.mkdir()
    agent_home.mkdir()
    _auth(shared_home)
    agents_md = agent_home / "AGENTS.md"
    agents_md.write_text("agent-authored edit", encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(shared_home))
    monkeypatch.setenv(PER_AGENT_CODEX_HOME_ENV, "1")
    soul_store = _SoulStore()

    prepared = prepare_agent_codex_home(
        _scope(working_dir, system_prompt="compiled soul"),
        log=lambda _message: None,
        soul_version_store=soul_store,
    )

    assert prepared == agent_home
    assert agents_md.read_text(encoding="utf-8") == "compiled soul"
    assert stat.S_IMODE(agents_md.stat().st_mode) == 0o600
    assert soul_store.calls == [
        ("test-agent", "agent-authored edit", "agent"),
        ("test-agent", "compiled soul", "spawn"),
    ]
    assert not (shared_home / "AGENTS.md").exists()


def test_prepare_replaces_linked_soul_without_writing_its_shared_target(
    tmp_path,
    monkeypatch,
):
    shared_home = tmp_path / "shared"
    working_dir = tmp_path / "agent"
    agent_home = working_dir / ".codex"
    working_dir.mkdir()
    agent_home.mkdir()
    _auth(shared_home)
    shared_soul = shared_home / "AGENTS.md"
    shared_soul.write_text("shared operator soul", encoding="utf-8")
    agents_md = agent_home / "AGENTS.md"
    agents_md.symlink_to(shared_soul)
    monkeypatch.setenv("CODEX_HOME", str(shared_home))
    monkeypatch.setenv(PER_AGENT_CODEX_HOME_ENV, "1")
    soul_store = _SoulStore()

    prepare_agent_codex_home(
        _scope(working_dir, system_prompt="compiled soul"),
        log=lambda _message: None,
        soul_version_store=soul_store,
    )

    assert not agents_md.is_symlink()
    assert agents_md.read_text(encoding="utf-8") == "compiled soul"
    assert shared_soul.read_text(encoding="utf-8") == "shared operator soul"
    assert soul_store.calls == [
        ("test-agent", "compiled soul", "spawn"),
    ]


def test_prepare_replaces_dangling_soul_symlink_without_creating_target(
    tmp_path,
    monkeypatch,
):
    shared_home = tmp_path / "shared"
    working_dir = tmp_path / "agent"
    agent_home = working_dir / ".codex"
    working_dir.mkdir()
    agent_home.mkdir()
    _auth(shared_home)
    missing_target = shared_home / "missing-operator-soul.md"
    agents_md = agent_home / "AGENTS.md"
    agents_md.symlink_to(missing_target)
    monkeypatch.setenv("CODEX_HOME", str(shared_home))
    monkeypatch.setenv(PER_AGENT_CODEX_HOME_ENV, "1")
    soul_store = _SoulStore()

    prepare_agent_codex_home(
        _scope(working_dir, system_prompt="compiled soul"),
        log=lambda _message: None,
        soul_version_store=soul_store,
    )

    assert not agents_md.is_symlink()
    assert agents_md.read_text(encoding="utf-8") == "compiled soul"
    assert not missing_target.exists()
    assert soul_store.calls == [("test-agent", "compiled soul", "spawn")]


def test_prepare_publishes_soul_mode_0600_under_restrictive_umask(
    tmp_path,
    monkeypatch,
):
    shared_home = tmp_path / "shared"
    working_dir = tmp_path / "agent"
    working_dir.mkdir()
    _auth(shared_home)
    monkeypatch.setenv("CODEX_HOME", str(shared_home))
    monkeypatch.setenv(PER_AGENT_CODEX_HOME_ENV, "1")

    previous_umask = os.umask(0o777)
    try:
        prepare_agent_codex_home(
            _scope(working_dir, system_prompt="compiled soul"),
            log=lambda _message: None,
            soul_version_store=_SoulStore(),
        )
    finally:
        os.umask(previous_umask)

    agents_md = working_dir / ".codex" / "AGENTS.md"
    assert stat.S_IMODE(agents_md.stat().st_mode) == 0o600
    assert agents_md.read_text(encoding="utf-8") == "compiled soul"


def test_shared_home_equality_refuses_before_every_writer_and_preserves_bytes(
    tmp_path,
    monkeypatch,
):
    shared_home = tmp_path / "shared"
    working_dir = tmp_path / "agent"
    working_dir.mkdir()
    _auth(shared_home)
    (shared_home / "AGENTS.md").write_text("shared operator soul", encoding="utf-8")
    (shared_home / "config.toml").write_text("shared operator config", encoding="utf-8")
    before = {
        path.name: path.read_bytes()
        for path in shared_home.iterdir()
        if path.is_file()
    }
    monkeypatch.setenv("CODEX_HOME", str(shared_home))
    monkeypatch.setenv(PER_AGENT_CODEX_HOME_ENV, "1")
    writer_calls: list[str] = []

    def _writer_spy(name):
        def _record(*_args, **_kwargs):
            writer_calls.append(name)

        return _record

    monkeypatch.setattr(codex_home_mod, "_write_managed_config", _writer_spy("config"))
    monkeypatch.setattr(codex_home_mod, "_ensure_auth_link", _writer_spy("auth"))
    monkeypatch.setattr(codex_home_mod, "_write_agent_soul", _writer_spy("soul"))
    monkeypatch.setattr(
        codex_home_mod,
        "_run_initial_rollout_migration",
        _writer_spy("migration"),
    )
    soul_store = _SoulStore()

    with pytest.raises(RuntimeError, match="resolves to the shared home"):
        prepare_agent_codex_home(
            _scope(
                working_dir,
                codex_home=str(shared_home),
                system_prompt="compiled soul",
            ),
            log=lambda _message: None,
            soul_version_store=soul_store,
        )

    after = {
        path.name: path.read_bytes()
        for path in shared_home.iterdir()
        if path.is_file()
    }
    assert writer_calls == []
    assert soul_store.calls == []
    assert after == before


def test_subprocess_transport_flag_off_has_zero_env_delta_and_zero_soul_writes(
    tmp_path,
    monkeypatch,
):
    shared_home = tmp_path / "shared"
    working_dir = tmp_path / "agent"
    monkeypatch.setenv("CODEX_HOME", str(shared_home))
    monkeypatch.delenv(PER_AGENT_CODEX_HOME_ENV, raising=False)
    config = StreamingSessionConfig(
        agent_name="test-agent",
        working_dir=str(working_dir),
        provider_url="codex_cli",
        system_prompt="compiled soul",
    )
    soul_store = _SoulStore()
    before = dict(os.environ)

    env = CodexSession(config, registry=soul_store)._build_codex_env()

    assert env == before
    assert soul_store.calls == []
    assert not (shared_home / "AGENTS.md").exists()
    assert not (working_dir / ".codex" / "AGENTS.md").exists()


def test_flag_off_home_fallback_is_test_controlled(tmp_path, monkeypatch):
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.delenv(PER_AGENT_CODEX_HOME_ENV, raising=False)
    monkeypatch.setattr("pinky_daemon.codex_home.Path.home", lambda: tmp_path)

    assert codex_home_for(_scope(tmp_path / "agent")) == tmp_path / ".codex"


def test_prepare_generates_minimal_home_auth_link_and_migrates_matches(tmp_path, monkeypatch):
    shared_home = tmp_path / "shared"
    working_dir = tmp_path / "agent"
    other_dir = tmp_path / "other"
    working_dir.mkdir()
    other_dir.mkdir()
    auth_source = _auth(shared_home)
    matching = _rollout(shared_home, "rollout-match.jsonl", working_dir)
    other = _rollout(shared_home, "rollout-other.jsonl", other_dir)
    logs: list[str] = []

    monkeypatch.setenv("CODEX_HOME", str(shared_home))
    monkeypatch.setenv(PER_AGENT_CODEX_HOME_ENV, "1")
    home = prepare_agent_codex_home(_scope(working_dir), log=logs.append)

    assert home == working_dir / ".codex"
    config_text = (home / "config.toml").read_text(encoding="utf-8")
    assert config_text.startswith(MANAGED_CONFIG_SENTINEL)
    config = tomllib.loads(config_text)
    assert config["features"] == {"apps": False, "plugins": False}
    assert config["projects"][str(working_dir.resolve())]["trust_level"] == "trusted"
    assert "mcp_servers" not in config
    assert "plugins" not in config
    assert "marketplaces" not in config
    assert (home / "sessions").is_dir()
    assert (home / "auth.json").is_symlink()
    assert os.path.samefile(home / "auth.json", auth_source)

    migrated = home / "sessions" / matching.relative_to(shared_home / "sessions")
    assert migrated.exists()
    assert not matching.exists()
    assert other.exists()
    assert any("migrated 1 cwd-matched rollout(s)" in message for message in logs)

    before = config_text
    prepare_agent_codex_home(_scope(working_dir), log=logs.append)
    assert (home / "config.toml").read_text(encoding="utf-8") == before


def test_migration_marker_permanently_hides_late_shared_rollout(tmp_path, monkeypatch):
    """Review P1-1: migration is a one-time bridge, not a spawn-time scan."""
    shared_home = tmp_path / "shared"
    working_dir = tmp_path / "agent"
    working_dir.mkdir()
    _auth(shared_home)
    first = _rollout(shared_home, "rollout-first.jsonl", working_dir)
    monkeypatch.setenv("CODEX_HOME", str(shared_home))
    monkeypatch.setenv(PER_AGENT_CODEX_HOME_ENV, "1")

    home = prepare_agent_codex_home(_scope(working_dir), log=lambda _message: None)
    migrated = home / "sessions" / first.relative_to(shared_home / "sessions")
    assert migrated.exists()
    marker = home / ROLLOUT_MIGRATION_MARKER
    marker_payload = json.loads(marker.read_text(encoding="utf-8"))
    assert marker_payload["moved_count"] == 1
    assert marker_payload["completed_at"]
    marker_bytes = marker.read_bytes()

    # Make the isolated store intentionally empty, then plant a new ambient
    # rollout with the same cwd. A later spawn must remain fresh.
    migrated.unlink()
    late = _rollout(shared_home, "rollout-planted-after-gate.jsonl", working_dir)
    second_logs: list[str] = []
    prepare_agent_codex_home(_scope(working_dir), log=second_logs.append)

    assert marker.read_bytes() == marker_bytes
    assert late.exists()
    assert not (home / "sessions" / late.relative_to(shared_home / "sessions")).exists()
    assert any("migration marker present" in message for message in second_logs)
    assert any("migrated 0 cwd-matched rollout(s)" in message for message in second_logs)

    config = StreamingSessionConfig(
        agent_name="test-agent",
        working_dir=str(working_dir),
        provider_url="codex_cli",
    )
    session = CodexTmuxSession(config)
    command = session._build_claude_cmd()
    assert "resume --last" not in command
    assert " -C " in command
    assert session._last_launch_used_continue is False


def test_explicit_override_wins_only_when_flag_is_enabled(tmp_path, monkeypatch):
    shared_home = tmp_path / "shared"
    working_dir = tmp_path / "agent"
    override = tmp_path / "custom-codex"
    working_dir.mkdir()
    _auth(shared_home)
    monkeypatch.setenv("CODEX_HOME", str(shared_home))
    monkeypatch.setenv(PER_AGENT_CODEX_HOME_ENV, "1")

    scope = _scope(working_dir, codex_home=str(override))
    assert codex_home_for(scope) == override
    assert prepare_agent_codex_home(scope, log=lambda _message: None) == override
    assert (override / "auth.json").is_symlink()


def test_unmanaged_config_is_preserved_with_loud_log(tmp_path, monkeypatch):
    shared_home = tmp_path / "shared"
    working_dir = tmp_path / "agent"
    agent_home = working_dir / ".codex"
    working_dir.mkdir()
    agent_home.mkdir()
    _auth(shared_home)
    manual = "# operator-managed\n[features]\nplugins = true\n"
    (agent_home / "config.toml").write_text(manual, encoding="utf-8")
    logs: list[str] = []
    monkeypatch.setenv("CODEX_HOME", str(shared_home))
    monkeypatch.setenv(PER_AGENT_CODEX_HOME_ENV, "1")

    prepare_agent_codex_home(_scope(working_dir), log=logs.append)

    assert (agent_home / "config.toml").read_text(encoding="utf-8") == manual
    assert any("preserving unmanaged config" in message for message in logs)


def test_linked_config_never_mutates_shared_config(tmp_path, monkeypatch):
    shared_home = tmp_path / "shared"
    working_dir = tmp_path / "agent"
    agent_home = working_dir / ".codex"
    working_dir.mkdir()
    agent_home.mkdir()
    _auth(shared_home)
    shared_config = shared_home / "config.toml"
    original = f"{MANAGED_CONFIG_SENTINEL}\n# shared\n"
    shared_config.write_text(original, encoding="utf-8")
    (agent_home / "config.toml").symlink_to(shared_config)
    logs: list[str] = []
    monkeypatch.setenv("CODEX_HOME", str(shared_home))
    monkeypatch.setenv(PER_AGENT_CODEX_HOME_ENV, "1")

    prepare_agent_codex_home(_scope(working_dir), log=logs.append)

    assert shared_config.read_text(encoding="utf-8") == original
    assert any("preserving linked config" in message for message in logs)


def test_dangling_config_symlink_is_preserved_without_write_through(tmp_path, monkeypatch):
    """Review P1-2: exists() must never precede symlink classification."""
    shared_home = tmp_path / "shared"
    working_dir = tmp_path / "agent"
    agent_home = working_dir / ".codex"
    working_dir.mkdir()
    agent_home.mkdir()
    _auth(shared_home)
    missing_shared_target = shared_home / "config.toml"
    config_link = agent_home / "config.toml"
    config_link.symlink_to(missing_shared_target)
    logs: list[str] = []
    monkeypatch.setenv("CODEX_HOME", str(shared_home))
    monkeypatch.setenv(PER_AGENT_CODEX_HOME_ENV, "1")

    prepare_agent_codex_home(_scope(working_dir), log=logs.append)

    assert config_link.is_symlink()
    assert not missing_shared_target.exists()
    assert any("preserving linked config" in message for message in logs)


def test_partial_migration_final_is_quarantined_and_retry_converges(tmp_path, monkeypatch):
    """Review P1-3: recover an old crash artifact without shadowing source."""
    shared_home = tmp_path / "shared"
    working_dir = tmp_path / "agent"
    agent_home = working_dir / ".codex"
    working_dir.mkdir()
    _auth(shared_home)
    source = _rollout(shared_home, "rollout-crash.jsonl", working_dir)
    source_bytes = source.read_bytes()
    relative = source.relative_to(shared_home / "sessions")
    final = agent_home / "sessions" / relative
    final.parent.mkdir(parents=True, exist_ok=True)
    partial_bytes = b'\xff{"type":"session_meta"'
    final.write_bytes(partial_bytes)
    abandoned = final.parent / ".pinkybot-rollout-migration-abandoned.tmp"
    abandoned.write_text("partial", encoding="utf-8")
    logs: list[str] = []
    monkeypatch.setenv("CODEX_HOME", str(shared_home))
    monkeypatch.setenv(PER_AGENT_CODEX_HOME_ENV, "1")

    prepare_agent_codex_home(_scope(working_dir), log=logs.append)

    assert not source.exists()
    assert final.read_bytes() == source_bytes
    assert not abandoned.exists()
    quarantines = list(final.parent.glob(f".pinkybot-rollout-quarantine-{final.name}-*"))
    assert len(quarantines) == 1
    assert quarantines[0].read_bytes() == partial_bytes
    assert any("removed abandoned rollout migration temp" in message for message in logs)
    assert any("quarantined invalid rollout migration final" in message for message in logs)


def test_quarantine_uuid_collision_retains_both_artifacts(tmp_path, monkeypatch):
    """R3 P2-3: quarantine reservation retries instead of replacing history."""
    destination = tmp_path / "rollout-collision.jsonl"
    destination.write_bytes(b"new invalid bytes")
    collision_hex = "a" * 32
    fresh_hex = "b" * 32
    prior = destination.with_name(
        f"{codex_home_mod._MIGRATION_QUARANTINE_PREFIX}"
        f"{destination.name}-{collision_hex}"
    )
    prior.write_bytes(b"prior invalid bytes")
    generated = iter(
        [SimpleNamespace(hex=collision_hex), SimpleNamespace(hex=fresh_hex)]
    )
    monkeypatch.setattr(codex_home_mod.uuid, "uuid4", lambda: next(generated))

    retained = codex_home_mod._quarantine_rollout(
        destination,
        log=lambda _message: None,
    )

    assert prior.read_bytes() == b"prior invalid bytes"
    assert retained.read_bytes() == b"new invalid bytes"
    assert retained != prior
    assert not destination.exists()


def test_initial_migration_lock_deadline_preserves_source_and_markerless_state(
    tmp_path, monkeypatch
):
    """R2 P1-1: a held destination lock cannot block prepare indefinitely."""
    shared_home = tmp_path / "shared"
    working_dir = tmp_path / "agent"
    agent_home = working_dir / ".codex"
    working_dir.mkdir()
    _auth(shared_home)
    source = _rollout(shared_home, "rollout-held-lock.jsonl", working_dir)
    source_bytes = source.read_bytes()
    destination_sessions = agent_home / "sessions"
    destination_sessions.mkdir(parents=True)
    lock_path = destination_sessions / codex_home_mod._MIGRATION_LOCK_NAME
    logs: list[str] = []
    monkeypatch.setenv("CODEX_HOME", str(shared_home))
    monkeypatch.setenv(PER_AGENT_CODEX_HOME_ENV, "1")
    monkeypatch.setenv(codex_home_mod.MIGRATION_LOCK_TIMEOUT_ENV, "0.05")

    with lock_path.open("a+b") as held_lock:
        fcntl.flock(held_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        started = time.monotonic()
        with pytest.raises(RuntimeError, match="migration lock deadline exceeded"):
            prepare_agent_codex_home(_scope(working_dir), log=logs.append)
        elapsed = time.monotonic() - started

    assert elapsed < 0.5
    assert source.read_bytes() == source_bytes
    assert not (agent_home / ROLLOUT_MIGRATION_MARKER).exists()
    assert any("migration lock deadline exceeded" in message for message in logs)


def test_reverse_migration_uses_same_bounded_lock_acquisition(tmp_path, monkeypatch):
    """R2 P1-1: the public reverse-migration entry point shares the deadline."""
    source_home = tmp_path / "agent-home"
    destination_home = tmp_path / "shared"
    working_dir = tmp_path / "agent"
    working_dir.mkdir()
    source = _rollout(source_home, "rollout-reverse-held-lock.jsonl", working_dir)
    source_bytes = source.read_bytes()
    destination_sessions = destination_home / "sessions"
    destination_sessions.mkdir(parents=True)
    lock_path = destination_sessions / codex_home_mod._MIGRATION_LOCK_NAME
    monkeypatch.setenv(codex_home_mod.MIGRATION_LOCK_TIMEOUT_ENV, "0.05")

    with lock_path.open("a+b") as held_lock:
        fcntl.flock(held_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(RuntimeError, match="migration lock deadline exceeded"):
            move_matching_rollouts(
                source_home,
                destination_home,
                working_dir,
                log=lambda _message: None,
            )

    assert source.read_bytes() == source_bytes


def test_claim_boundary_never_removes_fresh_canonical_writer_bytes(tmp_path, monkeypatch):
    """R3 P1-1: a write inside the former check/unlink interval survives."""
    shared_home = tmp_path / "shared"
    working_dir = tmp_path / "agent"
    agent_home = working_dir / ".codex"
    working_dir.mkdir()
    _auth(shared_home)
    source = _rollout(shared_home, "rollout-late-append.jsonl", working_dir)
    original = source.read_bytes()
    appended = b'{"type":"event_msg","payload":{"message":"late append"}}\n'
    relative = source.relative_to(shared_home / "sessions")
    destination = agent_home / "sessions" / relative
    real_unlink = Path.unlink
    injected = False

    def write_at_claim_removal(path: Path, *args, **kwargs):
        nonlocal injected
        if (
            path.parent == source.parent
            and path.name.startswith(codex_home_mod._MIGRATION_CLAIM_PREFIX)
            and not injected
        ):
            source.write_bytes(appended)
            injected = True
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", write_at_claim_removal)
    monkeypatch.setenv("CODEX_HOME", str(shared_home))
    monkeypatch.setenv(PER_AGENT_CODEX_HOME_ENV, "1")

    prepare_agent_codex_home(_scope(working_dir), log=lambda _message: None)

    assert injected is True
    assert source.read_bytes() == appended
    assert destination.read_bytes() == original
    assert not list(source.parent.glob(f"{codex_home_mod._MIGRATION_CLAIM_PREFIX}*"))
    marker_payload = json.loads(
        (agent_home / ROLLOUT_MIGRATION_MARKER).read_text(encoding="utf-8")
    )
    assert str(relative) in marker_payload["retry_sources"]


def test_descriptor_held_writer_retains_claim_then_remigrates_bytes(
    tmp_path, monkeypatch
):
    """R4 P1-1: a pre-claim descriptor write survives the retirement boundary."""
    shared_home = tmp_path / "shared"
    working_dir = tmp_path / "agent"
    agent_home = working_dir / ".codex"
    working_dir.mkdir()
    _auth(shared_home)
    source = _rollout(shared_home, "rollout-held-descriptor.jsonl", working_dir)
    original = source.read_bytes()
    appended = b'{"type":"event_msg","payload":{"message":"late append"}}\n'
    assert len(appended) == 57
    relative = source.relative_to(shared_home / "sessions")
    destination = agent_home / "sessions" / relative
    real_file_signature = codex_home_mod._file_signature
    injected = False

    with source.open("ab", buffering=0) as held_writer:

        def append_after_claim_signature(path: Path):
            nonlocal injected
            signature = real_file_signature(path)
            if (
                path.parent == source.parent
                and path.name.startswith(codex_home_mod._MIGRATION_CLAIM_PREFIX)
                and not injected
            ):
                held_writer.write(appended)
                os.fsync(held_writer.fileno())
                injected = True
            return signature

        monkeypatch.setattr(
            codex_home_mod,
            "_file_signature",
            append_after_claim_signature,
        )
        monkeypatch.setenv("CODEX_HOME", str(shared_home))
        monkeypatch.setenv(PER_AGENT_CODEX_HOME_ENV, "1")

        prepare_agent_codex_home(_scope(working_dir), log=lambda _message: None)

        claims = list(
            source.parent.glob(f"{codex_home_mod._MIGRATION_CLAIM_PREFIX}*")
        )
        assert injected is True
        assert len(claims) == 1
        assert claims[0].read_bytes() == original + appended
        assert destination.read_bytes() == original

    prepare_agent_codex_home(_scope(working_dir), log=lambda _message: None)

    assert destination.read_bytes() == original + appended


def test_zero_holder_identical_claim_retires(tmp_path, monkeypatch):
    """R4 P1-1: zero holders plus reverified identity retires the tombstone."""
    working_dir = tmp_path / "agent"
    working_dir.mkdir()
    claim_home = tmp_path / "claims"
    destination_home = tmp_path / "destination"
    claim = _rollout(claim_home, "rollout-zero-holder.jsonl", working_dir)
    destination = _rollout(
        destination_home,
        "rollout-zero-holder.jsonl",
        working_dir,
    )
    monkeypatch.setattr(
        codex_home_mod,
        "_claim_has_open_descriptors",
        lambda _claim, _log: False,
    )

    retired = codex_home_mod._retire_rollout_claim(
        claim,
        destination,
        os.path.realpath(working_dir),
        log=lambda _message: None,
    )

    assert retired is True
    assert not claim.exists()


def test_frozen_claim_with_held_writer_retains_every_append_in_named_claim(
    tmp_path, monkeypatch
):
    """R7 R1: a holder may keep writing, so retirement retains its pathname."""
    working_dir = tmp_path / "agent"
    working_dir.mkdir()
    claim_home = tmp_path / "claims"
    destination_home = tmp_path / "destination"
    source = _rollout(
        claim_home,
        "rollout-retirement-timing-window.jsonl",
        working_dir,
    )
    original = source.read_bytes()
    claim = codex_home_mod._claim_rollout(source, log=lambda _message: None)
    assert claim is not None
    destination = _rollout(
        destination_home,
        "rollout-retirement-timing-window.jsonl",
        working_dir,
    )
    before_freeze = b'{"type":"event_msg","payload":{"message":"before freeze"}}\n'
    after_scan = b'{"type":"event_msg","payload":{"message":"after scan"}}\n'
    real_rollout_is_valid = codex_home_mod._rollout_is_valid
    real_inode_scan = codex_home_mod._inode_has_open_descriptors
    late_writer = None
    injected = False
    logs: list[str] = []

    def append_during_destination_verification(
        path: Path,
        target_cwd: str,
        *,
        expected_signature=None,
    ) -> bool:
        nonlocal injected, late_writer
        valid = real_rollout_is_valid(
            path,
            target_cwd,
            expected_signature=expected_signature,
        )
        if path == destination and expected_signature is not None and not injected:
            late_writer = claim.open("ab", buffering=0)
            late_writer.write(before_freeze)
            os.fsync(late_writer.fileno())
            injected = True
        return valid

    monkeypatch.setattr(
        codex_home_mod,
        "_rollout_is_valid",
        append_during_destination_verification,
    )

    def append_after_frozen_scan_decision(claim_handle, claim_path, log):
        holders = real_inode_scan(claim_handle, claim_path, log)
        assert late_writer is not None
        late_writer.write(after_scan)
        os.fsync(late_writer.fileno())
        return holders

    monkeypatch.setattr(
        codex_home_mod,
        "_inode_has_open_descriptors",
        append_after_frozen_scan_decision,
    )
    try:
        claim_signature = codex_home_mod._file_signature(claim)
        assert codex_home_mod._rollout_is_valid(
            destination,
            os.path.realpath(working_dir),
            expected_signature=claim_signature,
        )
        retired = codex_home_mod._retire_rollout_claim(
            claim,
            destination,
            os.path.realpath(working_dir),
            log=logs.append,
        )

        assert injected is True
        assert retired is False
        assert claim.read_bytes() == original + before_freeze + after_scan
        assert destination.read_bytes() == original
        assert any(
            "open descriptor holder(s) remain at the frozen boundary" in message
            for message in logs
        )
        assert stat.S_IMODE(claim.stat().st_mode) != 0
    finally:
        if late_writer is not None:
            late_writer.close()


def test_file_mode_freeze_blocks_new_open_but_preserves_descriptor_rights(tmp_path):
    """R7 boundary semantics: file mode freeze is portable for a non-root owner."""
    claim = tmp_path / "claim"
    claim.write_bytes(b"stable bytes")
    claim.chmod(0o640)
    claim_fd = os.open(claim, os.O_RDONLY)
    try:
        os.fchmod(claim_fd, 0)

        with pytest.raises(PermissionError):
            os.open(claim, os.O_RDONLY)

        os.lseek(claim_fd, 0, os.SEEK_SET)
        assert os.read(claim_fd, 64) == b"stable bytes"
        os.chmod(claim, 0o640, follow_symlinks=False)
        assert stat.S_IMODE(claim.stat().st_mode) == 0o640
    finally:
        os.fchmod(claim_fd, 0o640)
        os.close(claim_fd)


def test_recovery_publication_failure_restores_named_claim_without_litter(
    tmp_path, monkeypatch
):
    """R7 R2a: publication failure occurs while the complete old name remains."""
    working_dir = tmp_path / "agent"
    working_dir.mkdir()
    claim_home = tmp_path / "claims"
    destination_home = tmp_path / "destination"
    source = _rollout(
        claim_home,
        "rollout-recovery-publication-failure.jsonl",
        working_dir,
    )
    original = source.read_bytes()
    claim = codex_home_mod._claim_rollout(source, log=lambda _message: None)
    assert claim is not None
    original_mode = stat.S_IMODE(claim.stat().st_mode)
    appended = b'{"type":"event_msg","payload":{"message":"new bytes"}}\n'
    current = original + appended
    claim.write_bytes(current)
    destination = _rollout(
        destination_home,
        "rollout-recovery-publication-failure.jsonl",
        working_dir,
    )
    real_link = codex_home_mod.os.link
    logs: list[str] = []

    monkeypatch.setattr(
        codex_home_mod,
        "_inode_has_open_descriptors",
        lambda _handle, _claim, _log: False,
    )

    def fail_recovery_link(source_path, destination_path, *args, **kwargs):
        if Path(source_path).name.startswith(
            f"{codex_home_mod._MIGRATION_TEMP_PREFIX}recovery-"
        ):
            raise OSError(5, "forced recovery publication failure")
        return real_link(source_path, destination_path, *args, **kwargs)

    monkeypatch.setattr(codex_home_mod.os, "link", fail_recovery_link)

    retired = codex_home_mod._retire_rollout_claim(
        claim,
        destination,
        os.path.realpath(working_dir),
        log=logs.append,
    )

    assert retired is False
    assert claim.read_bytes() == current
    assert stat.S_IMODE(claim.stat().st_mode) == original_mode
    assert destination.read_bytes() == original
    assert not list(
        claim.parent.glob(
            f"{codex_home_mod._MIGRATION_TEMP_PREFIX}recovery-*"
            f"{codex_home_mod._MIGRATION_TEMP_SUFFIX}"
        )
    )
    assert not list(claim.parent.glob("*.reserve"))
    assert any(
        "recovery publication failed before unlink" in message for message in logs
    )


@pytest.mark.parametrize(
    ("device_field", "inode_field"),
    [
        ("unparseable-device", "matching"),
        ("matching", "unparseable-inode"),
    ],
)
def test_frozen_inode_half_parse_is_loud_uncertainty(
    tmp_path, monkeypatch, device_field, inode_field
):
    """R7 R4: a matching identity half never becomes verified absence."""
    claim = tmp_path / "claim"
    claim.write_bytes(b"bytes")
    logs: list[str] = []

    with claim.open("rb", buffering=0) as claim_handle:
        claim_stat = os.fstat(claim_handle.fileno())
        device = (
            str(claim_stat.st_dev)
            if device_field == "matching"
            else device_field
        )
        inode = (
            str(claim_stat.st_ino)
            if inode_field == "matching"
            else inode_field
        )
        output = "\n".join(
            [
                f"p{os.getpid()}",
                f"f{claim_handle.fileno()}r",
                f"D{claim_stat.st_dev}",
                f"i{claim_stat.st_ino}",
                "p987654",
                "f9w",
                f"D{device}",
                f"i{inode}",
                "",
            ]
        )
        monkeypatch.setattr(
            codex_home_mod.subprocess,
            "run",
            lambda *_args, **_kwargs: SimpleNamespace(
                returncode=0,
                stdout=output,
                stderr="",
            ),
        )

        holders = codex_home_mod._inode_has_open_descriptors(
            claim_handle,
            claim,
            logs.append,
        )

    assert holders is None
    assert any(
        "could not parse a device or inode field for a potential holder" in message
        for message in logs
    )


def test_lsof_resolver_finds_macos_binary_outside_daemon_path(monkeypatch):
    """R8: the production daemon PATH still reaches macOS' system lsof."""
    daemon_path = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
    lookups: list[tuple[str, str]] = []

    def resolve(command: str) -> str | None:
        lookups.append((command, os.environ["PATH"]))
        if command == "lsof":
            return None
        if command == "/usr/sbin/lsof":
            return command
        raise AssertionError(f"unexpected lsof candidate: {command}")

    monkeypatch.setenv("PATH", daemon_path)
    monkeypatch.setattr(codex_home_mod.shutil, "which", resolve)
    codex_home_mod._resolve_lsof_binary.cache_clear()
    try:
        assert codex_home_mod._resolve_lsof_binary() == "/usr/sbin/lsof"
        assert codex_home_mod._resolve_lsof_binary() == "/usr/sbin/lsof"
    finally:
        codex_home_mod._resolve_lsof_binary.cache_clear()

    assert lookups == [
        ("lsof", daemon_path),
        ("/usr/sbin/lsof", daemon_path),
    ]


def test_both_descriptor_scans_invoke_resolved_lsof_binary(tmp_path, monkeypatch):
    """R8: neither descriptor scan falls back to subprocess PATH lookup."""
    claim = tmp_path / "claim"
    claim.write_bytes(b"bytes")
    resolved_lsof = "/resolved/system/lsof"
    commands: list[list[str]] = []
    monkeypatch.setattr(
        codex_home_mod,
        "_resolve_lsof_binary",
        lambda: resolved_lsof,
    )

    with claim.open("rb", buffering=0) as claim_handle:
        claim_stat = os.fstat(claim_handle.fileno())
        inode_output = "\n".join(
            [
                f"p{os.getpid()}",
                f"f{claim_handle.fileno()}r",
                f"D{claim_stat.st_dev}",
                f"i{claim_stat.st_ino}",
                "",
            ]
        )

        def scan(command, **_kwargs):
            commands.append(command)
            if command[1:] == ["-Fn", "--", os.fspath(claim)]:
                return SimpleNamespace(returncode=1, stdout="", stderr="")
            if command[1:] == ["-F", "pfDi"]:
                return SimpleNamespace(
                    returncode=0,
                    stdout=inode_output,
                    stderr="",
                )
            raise AssertionError(f"unexpected lsof command: {command}")

        monkeypatch.setattr(codex_home_mod.subprocess, "run", scan)

        assert codex_home_mod._claim_has_open_descriptors(
            claim,
            log=lambda _message: None,
        ) is False
        assert codex_home_mod._inode_has_open_descriptors(
            claim_handle,
            claim,
            log=lambda _message: None,
        ) is False

    assert commands == [
        [resolved_lsof, "-Fn", "--", os.fspath(claim)],
        [resolved_lsof, "-F", "pfDi"],
    ]


def test_live_mode_freeze_is_transiently_retained_without_thaw(tmp_path):
    """R7 R5: a concurrent retirer cannot repair another live freeze."""
    working_dir = tmp_path / "agent"
    working_dir.mkdir()
    claim_home = tmp_path / "claims"
    destination_home = tmp_path / "destination"
    source = _rollout(
        claim_home,
        "rollout-live-concurrent-freeze.jsonl",
        working_dir,
    )
    claim = codex_home_mod._claim_rollout(source, log=lambda _message: None)
    assert claim is not None
    destination = _rollout(
        destination_home,
        "rollout-live-concurrent-freeze.jsonl",
        working_dir,
    )
    logs: list[str] = []
    live_retirement_fd = os.open(claim, os.O_RDONLY)
    try:
        os.fchmod(live_retirement_fd, 0)

        retired = codex_home_mod._retire_rollout_claim(
            claim,
            destination,
            os.path.realpath(working_dir),
            log=logs.append,
        )

        assert retired is False
        assert stat.S_IMODE(claim.stat().st_mode) == 0
        assert any(
            "another retirement still holds the frozen claim" in message
            for message in logs
        )
    finally:
        os.fchmod(live_retirement_fd, 0o600)
        os.close(live_retirement_fd)


def test_crashed_mode_freeze_repairs_without_an_open_holder(tmp_path):
    """R7 R3: an owner can thaw a crashed mode-000 claim by pathname."""
    working_dir = tmp_path / "agent"
    working_dir.mkdir()
    claim_home = tmp_path / "claims"
    source = _rollout(
        claim_home,
        "rollout-crashed-freeze.jsonl",
        working_dir,
    )
    claim = codex_home_mod._claim_rollout(source, log=lambda _message: None)
    assert claim is not None
    claim.chmod(0)
    logs: list[str] = []

    repaired = codex_home_mod._repair_frozen_rollout_claim(claim, logs.append)

    assert repaired is True
    assert stat.S_IMODE(claim.stat().st_mode) == 0o600
    assert any("repaired crashed mode-000 rollout claim" in message for message in logs)


def test_install_open_eacces_race_is_a_loud_retryable_retain(tmp_path, monkeypatch):
    """R7 R5: a claim reader racing the freeze does not fail the migration."""
    working_dir = tmp_path / "agent"
    working_dir.mkdir()
    claim_home = tmp_path / "claims"
    destination_home = tmp_path / "destination"
    source = _rollout(
        claim_home,
        "rollout-install-eacces-race.jsonl",
        working_dir,
    )
    claim = codex_home_mod._claim_rollout(source, log=lambda _message: None)
    assert claim is not None
    destination = _rollout(
        destination_home,
        "rollout-install-eacces-race.jsonl",
        working_dir,
    )
    claim.chmod(0)
    logs: list[str] = []
    monkeypatch.setattr(
        codex_home_mod,
        "_repair_frozen_rollout_claim",
        lambda _claim, _log: True,
    )

    installed = codex_home_mod._install_rollout(
        claim,
        destination,
        os.path.realpath(working_dir),
        log=logs.append,
    )

    assert installed is False
    assert destination.exists()
    assert stat.S_IMODE(claim.stat().st_mode) == 0
    assert any(
        "claim pathname access was denied during a concurrent freeze" in message
        for message in logs
    )
    claim.chmod(0o600)


@pytest.mark.parametrize(
    "seam",
    ["after_freeze", "after_scan", "mid_publication", "before_unlink"],
)
def test_hard_exit_freeze_seams_keep_discoverable_complete_bytes(
    tmp_path, monkeypatch, seam
):
    """R7 R2b/R3: every hard-exit seam leaves a complete discoverable claim."""
    working_dir = tmp_path / "agent"
    working_dir.mkdir()
    claim_home = tmp_path / "claims"
    destination_home = tmp_path / "destination"
    source = _rollout(
        claim_home,
        f"rollout-hard-exit-{seam}.jsonl",
        working_dir,
    )
    original = source.read_bytes()
    claim = codex_home_mod._claim_rollout(source, log=lambda _message: None)
    assert claim is not None
    appended = b'{"type":"event_msg","payload":{"message":"latest"}}\n'
    current = original + appended
    claim.write_bytes(current)
    destination = _rollout(
        destination_home,
        f"rollout-hard-exit-{seam}.jsonl",
        working_dir,
    )
    hard_exit_code = 73
    monkeypatch.setattr(
        codex_home_mod,
        "_claim_has_open_descriptors",
        lambda _claim, _log: False,
    )
    monkeypatch.setattr(
        codex_home_mod,
        "_inode_has_open_descriptors",
        lambda _handle, _claim, _log: False,
    )

    child_pid = os.fork()
    if child_pid == 0:
        if seam == "after_freeze":
            codex_home_mod._inode_has_open_descriptors = (
                lambda *_args, **_kwargs: os._exit(hard_exit_code)
            )
        elif seam == "after_scan":
            codex_home_mod._file_signature_from_handle = (
                lambda *_args, **_kwargs: os._exit(hard_exit_code)
            )
        elif seam == "mid_publication":
            real_link = codex_home_mod.os.link

            def exit_during_publication(source_path, destination_path, *args, **kwargs):
                if Path(source_path).name.startswith(
                    f"{codex_home_mod._MIGRATION_TEMP_PREFIX}recovery-"
                ):
                    os._exit(hard_exit_code)
                return real_link(source_path, destination_path, *args, **kwargs)

            codex_home_mod.os.link = exit_during_publication
        else:
            real_unlink = Path.unlink

            def exit_before_claim_unlink(path, *args, **kwargs):
                if path == claim:
                    os._exit(hard_exit_code)
                return real_unlink(path, *args, **kwargs)

            Path.unlink = exit_before_claim_unlink

        codex_home_mod._retire_rollout_claim(
            claim,
            destination,
            os.path.realpath(working_dir),
            log=lambda _message: None,
        )
        os._exit(74)

    waited_pid, wait_status = os.waitpid(child_pid, 0)
    assert waited_pid == child_pid
    assert os.WIFEXITED(wait_status)
    assert os.WEXITSTATUS(wait_status) == hard_exit_code

    surviving_claims = list(
        claim.parent.glob(f"{codex_home_mod._MIGRATION_CLAIM_PREFIX}*")
    )
    assert surviving_claims
    repair_logs: list[str] = []
    for surviving_claim in surviving_claims:
        assert codex_home_mod._repair_frozen_rollout_claim(
            surviving_claim,
            repair_logs.append,
        )
    assert any(path.read_bytes() == current for path in surviving_claims)

    moved = move_matching_rollouts(
        claim_home,
        destination_home,
        working_dir,
        log=repair_logs.append,
    )

    assert moved >= 1
    assert destination.read_bytes() == current
    assert not list(
        claim.parent.glob(f"{codex_home_mod._MIGRATION_CLAIM_PREFIX}*")
    )
    assert not list(
        claim.parent.glob(
            f"{codex_home_mod._MIGRATION_TEMP_PREFIX}recovery-*"
            f"{codex_home_mod._MIGRATION_TEMP_SUFFIX}"
        )
    )
    assert not list(claim.parent.glob("*.reserve"))


def test_descriptor_scan_unavailable_keeps_claim(tmp_path, monkeypatch):
    """R4 P1-1: lsof absence fails toward claim retention, never deletion."""
    working_dir = tmp_path / "agent"
    working_dir.mkdir()
    claim_home = tmp_path / "claims"
    destination_home = tmp_path / "destination"
    claim = _rollout(claim_home, "rollout-scan-unavailable.jsonl", working_dir)
    destination = _rollout(
        destination_home,
        "rollout-scan-unavailable.jsonl",
        working_dir,
    )
    logs: list[str] = []

    def unexpected_subprocess(*_args, **_kwargs):
        raise AssertionError("subprocess must not run without a resolved lsof")

    monkeypatch.setattr(codex_home_mod, "_resolve_lsof_binary", lambda: None)
    monkeypatch.setattr(codex_home_mod.subprocess, "run", unexpected_subprocess)

    retired = codex_home_mod._retire_rollout_claim(
        claim,
        destination,
        os.path.realpath(working_dir),
        log=logs.append,
    )

    assert retired is False
    assert claim.exists()
    assert any("descriptor scan unavailable" in message for message in logs)
    assert any("lsof executable not found" in message for message in logs)


def test_claim_link_collision_retries_without_replacing_interleaved_claim(
    tmp_path, monkeypatch
):
    """R4 P2-3: link EEXIST preserves the winner and retries a fresh UUID."""
    working_dir = tmp_path / "agent"
    working_dir.mkdir()
    source_home = tmp_path / "shared"
    source = _rollout(source_home, "rollout-link-collision.jsonl", working_dir)
    source_bytes = source.read_bytes()
    collision_hex = "e" * 32
    fresh_hex = "f" * 32
    prior = source.with_name(
        f"{codex_home_mod._MIGRATION_CLAIM_PREFIX}"
        f"{collision_hex}-{source.name}"
    )
    generated = iter(
        [SimpleNamespace(hex=collision_hex), SimpleNamespace(hex=fresh_hex)]
    )
    monkeypatch.setattr(codex_home_mod.uuid, "uuid4", lambda: next(generated))
    real_link = os.link
    injected = False

    def collide_before_link(source_path, claim_path, *args, **kwargs):
        nonlocal injected
        if not injected:
            Path(claim_path).write_bytes(b"interleaved prior claim")
            injected = True
        return real_link(source_path, claim_path, *args, **kwargs)

    monkeypatch.setattr(codex_home_mod.os, "link", collide_before_link)

    claim = codex_home_mod._claim_rollout(source, log=lambda _message: None)

    assert injected is True
    assert claim is not None
    assert claim.name == (
        f"{codex_home_mod._MIGRATION_CLAIM_PREFIX}{fresh_hex}-{source.name}"
    )
    assert prior.read_bytes() == b"interleaved prior claim"
    assert claim.read_bytes() == source_bytes
    assert not source.exists()
    assert not list(source.parent.glob("*.reserve"))


def test_stale_empty_reservations_are_swept_data_safely(tmp_path, monkeypatch):
    """R7: marker fast paths sweep unlocked crash or stale empty sidecars."""
    source_home = tmp_path / "source"
    destination_home = tmp_path / "destination"
    working_dir = tmp_path / "agent"
    working_dir.mkdir()
    _auth(source_home)
    monkeypatch.setenv("CODEX_HOME", str(source_home))
    monkeypatch.setenv(PER_AGENT_CODEX_HOME_ENV, "1")
    scope = _scope(working_dir, codex_home=str(destination_home))
    prepare_agent_codex_home(scope, log=lambda _message: None)
    source_sessions = source_home / "sessions"
    destination_sessions = destination_home / "sessions"
    source_sessions.mkdir(parents=True)
    destination_sessions.mkdir(parents=True, exist_ok=True)
    stale = source_sessions / (
        f".{codex_home_mod._MIGRATION_CLAIM_PREFIX}"
        f"{'a' * 32}-rollout-stale.jsonl"
        f"{codex_home_mod._PRESERVATION_RESERVATION_SUFFIX}"
    )
    recent = destination_sessions / (
        f".{codex_home_mod._MIGRATION_QUARANTINE_PREFIX}"
        f"rollout-recent.jsonl-{'b' * 32}"
        f"{codex_home_mod._PRESERVATION_RESERVATION_SUFFIX}"
    )
    nonempty = destination_sessions / (
        f".{codex_home_mod._MIGRATION_LOCK_ASIDE_PREFIX}"
        f"lock-{'c' * 32}{codex_home_mod._PRESERVATION_RESERVATION_SUFFIX}"
    )
    stale.touch()
    recent.touch()
    nonempty.write_text("operator bytes", encoding="utf-8")
    old = time.time() - codex_home_mod._PRESERVATION_RESERVATION_STALE_SECONDS - 1
    os.utime(stale, (old, old))
    os.utime(nonempty, (old, old))
    logs: list[str] = []

    prepared = prepare_agent_codex_home(scope, log=logs.append)

    assert prepared == destination_home
    assert not stale.exists()
    assert not recent.exists()
    assert nonempty.read_text(encoding="utf-8") == "operator bytes"
    assert any("removed stale migration reservation" in message for message in logs)
    assert any("removed orphaned migration reservation" in message for message in logs)


def test_active_reservation_lock_prevents_crash_sweeper_race(tmp_path):
    """R7 R3: immediate orphan cleanup never removes a live publisher's name."""
    destination = tmp_path / ".pinkybot-rollout-claim-live-rollout-test.jsonl"
    reservation = destination.with_name(
        f".{destination.name}{codex_home_mod._PRESERVATION_RESERVATION_SUFFIX}"
    )
    logs: list[str] = []

    with codex_home_mod._reserve_exclusive_destination(
        lambda _reservation_id: destination,
        log=logs.append,
    ) as reserved:
        assert reserved == destination
        assert reservation.exists()

        codex_home_mod._sweep_stale_reservations(tmp_path, logs.append)

        assert reservation.exists()
        assert not logs

    assert not reservation.exists()


def test_reservation_swept_between_create_and_flock_retries_without_double_grant(
    tmp_path, monkeypatch
):
    """R8 R1: the exact swept-before-flock interleaving cannot double-grant."""
    destination = tmp_path / ".pinkybot-rollout-claim-race-rollout-test.jsonl"
    reservation = destination.with_name(
        f".{destination.name}{codex_home_mod._PRESERVATION_RESERVATION_SUFFIX}"
    )
    real_open = codex_home_mod.os.open
    real_flock = codex_home_mod.fcntl.flock
    creator_a_before_flock = threading.Event()
    allow_creator_a_flock = threading.Event()
    creator_a_retry_blocked = threading.Event()
    creator_a_granted = threading.Event()
    release_creator_a = threading.Event()
    creator_b_granted = threading.Event()
    release_creator_b = threading.Event()
    creator_b_exited = threading.Event()
    state_lock = threading.Lock()
    creator_a_attempts = 0
    active_grants = 0
    maximum_active_grants = 0
    grant_order: list[tuple[str, Path]] = []
    errors: list[BaseException] = []
    sweep_logs: list[str] = []

    def interleaved_open(path, flags, *args, **kwargs):
        nonlocal creator_a_attempts
        if (
            threading.current_thread().name == "reservation-creator-a"
            and flags & os.O_EXCL
            and Path(path) == reservation
        ):
            creator_a_attempts += 1
            if creator_a_attempts == 2:
                creator_a_retry_blocked.set()
                if not creator_b_exited.wait(timeout=5):
                    raise AssertionError("creator B did not release its reservation")
        return real_open(path, flags, *args, **kwargs)

    def pause_creator_a_before_flock(descriptor: int, operation: int) -> None:
        if (
            threading.current_thread().name == "reservation-creator-a"
            and operation & fcntl.LOCK_EX
            and not creator_a_before_flock.is_set()
        ):
            creator_a_before_flock.set()
            if not allow_creator_a_flock.wait(timeout=5):
                raise AssertionError("creator A was not released to flock")
        real_flock(descriptor, operation)

    def reserve(name: str, granted: threading.Event, release: threading.Event) -> None:
        nonlocal active_grants, maximum_active_grants
        try:
            with codex_home_mod._reserve_exclusive_destination(
                lambda _reservation_id: destination,
                log=lambda _message: None,
            ) as reserved:
                with state_lock:
                    active_grants += 1
                    maximum_active_grants = max(
                        maximum_active_grants,
                        active_grants,
                    )
                    grant_order.append((name, reserved))
                granted.set()
                if not release.wait(timeout=5):
                    raise AssertionError(f"creator {name} was not released")
                with state_lock:
                    active_grants -= 1
        except BaseException as exc:
            errors.append(exc)
            granted.set()
        finally:
            if name == "b":
                creator_b_exited.set()

    monkeypatch.setattr(codex_home_mod.os, "open", interleaved_open)
    monkeypatch.setattr(
        codex_home_mod.fcntl,
        "flock",
        pause_creator_a_before_flock,
    )
    creator_a = threading.Thread(
        target=reserve,
        args=("a", creator_a_granted, release_creator_a),
        name="reservation-creator-a",
    )
    creator_b = threading.Thread(
        target=reserve,
        args=("b", creator_b_granted, release_creator_b),
        name="reservation-creator-b",
    )
    try:
        creator_a.start()
        assert creator_a_before_flock.wait(timeout=5)

        codex_home_mod._sweep_stale_reservations(tmp_path, sweep_logs.append)
        assert not reservation.exists()

        creator_b.start()
        assert creator_b_granted.wait(timeout=5)
        assert not errors
        assert reservation.exists()

        allow_creator_a_flock.set()
        assert creator_a_retry_blocked.wait(timeout=5)
        assert not creator_a_granted.is_set()
        assert maximum_active_grants == 1
        assert list(tmp_path.glob("*.reserve")) == [reservation]

        release_creator_b.set()
        assert creator_b_exited.wait(timeout=5)
        assert creator_a_granted.wait(timeout=5)
        assert not errors
        assert list(tmp_path.glob("*.reserve")) == [reservation]
        assert maximum_active_grants == 1
    finally:
        allow_creator_a_flock.set()
        release_creator_b.set()
        release_creator_a.set()
        if creator_a.ident is not None:
            creator_a.join(timeout=5)
        if creator_b.ident is not None:
            creator_b.join(timeout=5)

    assert not creator_a.is_alive()
    assert not creator_b.is_alive()
    assert errors == []
    assert creator_a_attempts == 2
    assert grant_order == [("b", destination), ("a", destination)]
    assert maximum_active_grants == 1
    assert not reservation.exists()
    assert len(sweep_logs) == 1
    assert "removed orphaned migration reservation" in sweep_logs[0]


def test_creator_cleanup_does_not_unlink_replacement_live_reservation(tmp_path):
    """R8 R2: an old creator cannot unlink a new holder's sidecar name."""
    destination = tmp_path / ".pinkybot-rollout-claim-cleanup-rollout-test.jsonl"
    reservation = destination.with_name(
        f".{destination.name}{codex_home_mod._PRESERVATION_RESERVATION_SUFFIX}"
    )
    replacement_fd = -1
    try:
        with codex_home_mod._reserve_exclusive_destination(
            lambda _reservation_id: destination,
            log=lambda _message: None,
        ):
            original_stat = reservation.lstat()
            reservation.unlink()
            replacement_fd = os.open(
                reservation,
                os.O_RDWR | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            fcntl.flock(replacement_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            replacement_stat = os.fstat(replacement_fd)
            assert (replacement_stat.st_dev, replacement_stat.st_ino) != (
                original_stat.st_dev,
                original_stat.st_ino,
            )

        current_stat = reservation.lstat()
        assert (current_stat.st_dev, current_stat.st_ino) == (
            replacement_stat.st_dev,
            replacement_stat.st_ino,
        )
    finally:
        if replacement_fd >= 0:
            fcntl.flock(replacement_fd, fcntl.LOCK_UN)
            os.close(replacement_fd)
        reservation.unlink(missing_ok=True)


def test_sweeper_cleanup_does_not_unlink_replacement_live_reservation(
    tmp_path, monkeypatch
):
    """R8 R2: a sweeper cannot unlink a new holder after locking the old inode."""
    destination = tmp_path / ".pinkybot-rollout-claim-sweeper-rollout-test.jsonl"
    reservation = destination.with_name(
        f".{destination.name}{codex_home_mod._PRESERVATION_RESERVATION_SUFFIX}"
    )
    reservation.touch()
    real_unlink_locked = codex_home_mod._unlink_locked_reservation
    replacement_fd = -1
    replacement_stat = None

    def replace_before_sweeper_cleanup(path: Path, locked_fd: int) -> bool:
        nonlocal replacement_fd, replacement_stat
        original_stat = os.fstat(locked_fd)
        path.unlink()
        replacement_fd = os.open(
            path,
            os.O_RDWR | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        fcntl.flock(replacement_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        replacement_stat = os.fstat(replacement_fd)
        assert (replacement_stat.st_dev, replacement_stat.st_ino) != (
            original_stat.st_dev,
            original_stat.st_ino,
        )
        return real_unlink_locked(path, locked_fd)

    monkeypatch.setattr(
        codex_home_mod,
        "_unlink_locked_reservation",
        replace_before_sweeper_cleanup,
    )
    logs: list[str] = []
    try:
        codex_home_mod._sweep_stale_reservations(tmp_path, logs.append)

        assert replacement_stat is not None
        current_stat = reservation.lstat()
        assert (current_stat.st_dev, current_stat.st_ino) == (
            replacement_stat.st_dev,
            replacement_stat.st_ino,
        )
        assert not logs
    finally:
        if replacement_fd >= 0:
            fcntl.flock(replacement_fd, fcntl.LOCK_UN)
            os.close(replacement_fd)
        reservation.unlink(missing_ok=True)


def test_reservation_retry_exhaustion_is_loud_and_retains_source(
    tmp_path, monkeypatch
):
    """R8 R3: repeated pre-lock sweeps stop loudly without claiming bytes."""
    working_dir = tmp_path / "agent"
    working_dir.mkdir()
    source = _rollout(
        tmp_path / "source",
        "rollout-reservation-exhaustion.jsonl",
        working_dir,
    )
    source_bytes = source.read_bytes()
    real_open = codex_home_mod.os.open
    real_flock = codex_home_mod.fcntl.flock
    reservation_paths: dict[int, Path] = {}
    swept_reservations: list[Path] = []
    logs: list[str] = []

    def track_reservation_open(path, flags, *args, **kwargs):
        descriptor = real_open(path, flags, *args, **kwargs)
        if flags & os.O_EXCL and os.fspath(path).endswith(
            codex_home_mod._PRESERVATION_RESERVATION_SUFFIX
        ):
            reservation_paths[descriptor] = Path(path)
        return descriptor

    def sweep_every_reservation_before_flock(
        descriptor: int,
        operation: int,
    ) -> None:
        reservation = reservation_paths.get(descriptor)
        if operation & fcntl.LOCK_EX and reservation is not None:
            reservation.unlink()
            swept_reservations.append(reservation)
        real_flock(descriptor, operation)

    monkeypatch.setattr(codex_home_mod, "_RESERVATION_ACQUIRE_MAX_ATTEMPTS", 3)
    monkeypatch.setattr(codex_home_mod.os, "open", track_reservation_open)
    monkeypatch.setattr(
        codex_home_mod.fcntl,
        "flock",
        sweep_every_reservation_before_flock,
    )

    with pytest.raises(
        RuntimeError,
        match="exclusive migration reservation did not converge after 3 attempts",
    ):
        codex_home_mod._claim_rollout(source, log=logs.append)

    assert len(swept_reservations) == 3
    assert source.read_bytes() == source_bytes
    assert not list(source.parent.glob(f"{codex_home_mod._MIGRATION_CLAIM_PREFIX}*"))
    assert not list(source.parent.glob("*.reserve"))
    assert len(logs) == 1
    assert "refusing non-replacing publication" in logs[0]


@pytest.mark.parametrize("write_phase", ["during_copy", "post_install"])
def test_fresh_canonical_writer_path_remigrates_on_next_prepare(
    tmp_path, monkeypatch, write_phase
):
    """R3 P1-1: writes after claim become the next prepare's authority."""
    shared_home = tmp_path / "shared"
    working_dir = tmp_path / "agent"
    agent_home = working_dir / ".codex"
    working_dir.mkdir()
    _auth(shared_home)
    source = _rollout(shared_home, f"rollout-{write_phase}.jsonl", working_dir)
    original = source.read_bytes()
    appended = b'{"type":"event_msg","payload":{"message":"fresh path"}}\n'
    fresh_bytes = original + appended
    relative = source.relative_to(shared_home / "sessions")
    destination = agent_home / "sessions" / relative
    injected = False

    if write_phase == "during_copy":
        real_file_signature = codex_home_mod._file_signature

        def write_after_claim_copy(path: Path):
            nonlocal injected
            signature = real_file_signature(path)
            if (
                path.parent == source.parent
                and path.name.startswith(codex_home_mod._MIGRATION_CLAIM_PREFIX)
                and not injected
            ):
                source.write_bytes(fresh_bytes)
                injected = True
            return signature

        monkeypatch.setattr(codex_home_mod, "_file_signature", write_after_claim_copy)
    else:
        real_rollout_is_valid = codex_home_mod._rollout_is_valid

        def write_after_final_install(path, target_cwd, *, expected_signature=None):
            nonlocal injected
            valid = real_rollout_is_valid(
                path,
                target_cwd,
                expected_signature=expected_signature,
            )
            if path == destination and expected_signature is not None and valid and not injected:
                source.write_bytes(fresh_bytes)
                injected = True
            return valid

        monkeypatch.setattr(
            codex_home_mod,
            "_rollout_is_valid",
            write_after_final_install,
        )

    monkeypatch.setenv("CODEX_HOME", str(shared_home))
    monkeypatch.setenv(PER_AGENT_CODEX_HOME_ENV, "1")

    prepare_agent_codex_home(_scope(working_dir), log=lambda _message: None)

    assert injected is True
    assert source.read_bytes() == fresh_bytes
    assert destination.read_bytes() == original

    prepare_agent_codex_home(_scope(working_dir), log=lambda _message: None)

    assert not source.exists()
    assert destination.read_bytes() == fresh_bytes
    assert (agent_home / ROLLOUT_MIGRATION_MARKER).exists()


def test_linked_lock_entry_is_preserved_aside_without_touching_target(
    tmp_path, monkeypatch
):
    """R2 P2-4: classify and preserve a linked lock before opening it."""
    shared_home = tmp_path / "shared"
    destination_home = tmp_path / "agent-home"
    working_dir = tmp_path / "agent"
    working_dir.mkdir()
    destination_sessions = destination_home / "sessions"
    destination_sessions.mkdir(parents=True)
    unintended_target = tmp_path / "must-not-be-created.lock"
    lock_path = destination_sessions / codex_home_mod._MIGRATION_LOCK_NAME
    lock_path.symlink_to(unintended_target)
    logs: list[str] = []

    moved = move_matching_rollouts(
        shared_home,
        destination_home,
        working_dir,
        log=logs.append,
    )

    assert moved == 0
    assert not unintended_target.exists()
    assert lock_path.is_file()
    assert not lock_path.is_symlink()
    asides = list(
        destination_sessions.glob(f"{codex_home_mod._MIGRATION_LOCK_ASIDE_PREFIX}*")
    )
    assert len(asides) == 1
    assert asides[0].is_symlink()
    assert os.readlink(asides[0]) == str(unintended_target)
    assert any("renamed linked migration lock entry" in message for message in logs)


def test_lock_aside_uuid_collision_retains_both_artifacts(tmp_path, monkeypatch):
    """R3 P2-3: lock-aside reservation retries instead of replacing history."""
    source_home = tmp_path / "source-home"
    destination_home = tmp_path / "destination-home"
    working_dir = tmp_path / "agent"
    working_dir.mkdir()
    destination_sessions = destination_home / "sessions"
    destination_sessions.mkdir(parents=True)
    lock_path = destination_sessions / codex_home_mod._MIGRATION_LOCK_NAME
    lock_path.write_bytes(b"new linked bytes")
    peer = tmp_path / "peer.lock"
    os.link(lock_path, peer)
    collision_hex = "c" * 32
    fresh_hex = "d" * 32
    prior = destination_sessions / (
        f"{codex_home_mod._MIGRATION_LOCK_ASIDE_PREFIX}"
        f"{lock_path.name}-{collision_hex}"
    )
    prior.write_bytes(b"prior retained bytes")
    generated = iter(
        [SimpleNamespace(hex=collision_hex), SimpleNamespace(hex=fresh_hex)]
    )
    monkeypatch.setattr(codex_home_mod.uuid, "uuid4", lambda: next(generated))

    moved = move_matching_rollouts(
        source_home,
        destination_home,
        working_dir,
        log=lambda _message: None,
    )

    retained = destination_sessions / (
        f"{codex_home_mod._MIGRATION_LOCK_ASIDE_PREFIX}"
        f"{lock_path.name}-{fresh_hex}"
    )
    assert moved == 0
    assert prior.read_bytes() == b"prior retained bytes"
    assert retained.read_bytes() == b"new linked bytes"
    assert peer.read_bytes() == b"new linked bytes"
    assert lock_path.is_file()


def test_hard_linked_lock_entry_is_preserved_aside_without_mutating_peer(tmp_path):
    """A multiply-linked regular entry follows the same preserve-aside rule."""
    source_home = tmp_path / "source-home"
    destination_home = tmp_path / "destination-home"
    working_dir = tmp_path / "agent"
    working_dir.mkdir()
    destination_sessions = destination_home / "sessions"
    destination_sessions.mkdir(parents=True)
    peer = tmp_path / "operator-lock"
    peer.write_bytes(b"operator bytes")
    peer.chmod(0o640)
    lock_path = destination_sessions / codex_home_mod._MIGRATION_LOCK_NAME
    os.link(peer, lock_path)
    original_mode = peer.stat().st_mode
    logs: list[str] = []

    moved = move_matching_rollouts(
        source_home,
        destination_home,
        working_dir,
        log=logs.append,
    )

    assert moved == 0
    assert peer.read_bytes() == b"operator bytes"
    assert peer.stat().st_mode == original_mode
    assert lock_path.is_file()
    assert lock_path.stat().st_ino != peer.stat().st_ino
    asides = list(
        destination_sessions.glob(f"{codex_home_mod._MIGRATION_LOCK_ASIDE_PREFIX}*")
    )
    assert len(asides) == 1
    assert asides[0].stat().st_ino == peer.stat().st_ino
    assert any("renamed linked migration lock entry" in message for message in logs)


def test_nonregular_lock_entry_is_preserved_aside_without_blocking(tmp_path):
    """A FIFO lock entry is classified before open and cannot stall prepare."""
    source_home = tmp_path / "source-home"
    destination_home = tmp_path / "destination-home"
    working_dir = tmp_path / "agent"
    working_dir.mkdir()
    destination_sessions = destination_home / "sessions"
    destination_sessions.mkdir(parents=True)
    lock_path = destination_sessions / codex_home_mod._MIGRATION_LOCK_NAME
    os.mkfifo(lock_path)
    logs: list[str] = []

    moved = move_matching_rollouts(
        source_home,
        destination_home,
        working_dir,
        log=logs.append,
    )

    assert moved == 0
    assert lock_path.is_file()
    asides = list(
        destination_sessions.glob(f"{codex_home_mod._MIGRATION_LOCK_ASIDE_PREFIX}*")
    )
    assert len(asides) == 1
    assert stat.S_ISFIFO(asides[0].lstat().st_mode)
    assert any("renamed non-regular migration lock entry" in message for message in logs)


def test_same_source_migration_loser_converges_and_counts_result(tmp_path, monkeypatch):
    """Review P2: losing the source unlink race is successful convergence."""
    shared_home = tmp_path / "shared"
    destination_home = tmp_path / "agent-home"
    working_dir = tmp_path / "agent"
    working_dir.mkdir()
    source = _rollout(shared_home, "rollout-race.jsonl", working_dir)
    relative = source.relative_to(shared_home / "sessions")
    destination = destination_home / "sessions" / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(source.read_bytes())
    real_unlink = Path.unlink

    def winner_claimed_then_raised(path: Path, *args, **kwargs):
        if path == source:
            real_unlink(path)
            raise FileNotFoundError(path)
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", winner_claimed_then_raised)
    logs: list[str] = []

    moved = move_matching_rollouts(
        shared_home,
        destination_home,
        working_dir,
        log=logs.append,
    )

    assert moved == 1
    assert not source.exists()
    assert destination.exists()
    assert any("migration converged" in message for message in logs)


def test_concurrent_same_source_migrators_both_converge(tmp_path, monkeypatch):
    """Review P2 exact shape: two pre-lock scans, one atomic final."""
    shared_home = tmp_path / "shared"
    destination_home = tmp_path / "agent-home"
    working_dir = tmp_path / "agent"
    working_dir.mkdir()
    source = _rollout(shared_home, "rollout-concurrent.jsonl", working_dir)
    relative = source.relative_to(shared_home / "sessions")
    destination = destination_home / "sessions" / relative
    source_sessions = shared_home / "sessions"
    scan_barrier = threading.Barrier(2)
    real_glob = Path.glob

    def synchronized_glob(path: Path, pattern: str):
        found = list(real_glob(path, pattern))
        if path == source_sessions and pattern == "**/rollout-*.jsonl":
            scan_barrier.wait(timeout=5)
        return iter(found)

    monkeypatch.setattr(Path, "glob", synchronized_glob)
    logs: list[str] = []

    def migrate() -> int:
        return move_matching_rollouts(
            shared_home,
            destination_home,
            working_dir,
            log=logs.append,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(migrate), pool.submit(migrate)]
        moved = [future.result(timeout=10) for future in futures]

    assert moved == [1, 1]
    assert not source.exists()
    assert destination.exists()
    assert any("source was already claimed" in message for message in logs)


def test_concurrent_initial_migration_commits_one_accurate_marker(
    tmp_path, monkeypatch
):
    """The one-time marker closes the scan-to-gate race between spawns."""
    shared_home = tmp_path / "shared"
    destination_home = tmp_path / "agent-home"
    working_dir = tmp_path / "agent"
    working_dir.mkdir()
    source = _rollout(shared_home, "rollout-initial-race.jsonl", working_dir)
    source_sessions = shared_home / "sessions"
    destination_sessions = destination_home / "sessions"
    destination_sessions.mkdir(parents=True)
    scan_barrier = threading.Barrier(2)
    real_glob = Path.glob

    def synchronized_glob(path: Path, pattern: str):
        found = list(real_glob(path, pattern))
        if path == source_sessions and pattern == "**/rollout-*.jsonl":
            scan_barrier.wait(timeout=5)
        return iter(found)

    monkeypatch.setattr(Path, "glob", synchronized_glob)
    logs: list[str] = []

    def migrate() -> int:
        return codex_home_mod._run_initial_rollout_migration(
            shared_home,
            destination_home,
            working_dir,
            log=logs.append,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(migrate), pool.submit(migrate)]
        moved = [future.result(timeout=10) for future in futures]

    marker = destination_home / ROLLOUT_MIGRATION_MARKER
    marker_payload = json.loads(marker.read_text(encoding="utf-8"))
    destination = destination_sessions / source.relative_to(source_sessions)
    assert moved == [1, 1]
    assert marker_payload["moved_count"] == 1
    assert not source.exists()
    assert destination.exists()
    assert any("marker already committed" in message for message in logs)


def test_auth_absence_refuses_spawn_before_creating_agent_home(tmp_path, monkeypatch):
    shared_home = tmp_path / "shared"
    shared_home.mkdir()
    working_dir = tmp_path / "agent"
    working_dir.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(shared_home))
    monkeypatch.setenv(PER_AGENT_CODEX_HOME_ENV, "1")

    with pytest.raises(RuntimeError, match="shared auth file is absent or unreadable"):
        prepare_agent_codex_home(_scope(working_dir), log=lambda _message: None)

    assert not (working_dir / ".codex").exists()


def test_existing_non_symlink_auth_refuses_spawn(tmp_path, monkeypatch):
    shared_home = tmp_path / "shared"
    working_dir = tmp_path / "agent"
    agent_home = working_dir / ".codex"
    working_dir.mkdir()
    agent_home.mkdir()
    _auth(shared_home)
    (agent_home / "auth.json").write_text('{"forked": true}\n', encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(shared_home))
    monkeypatch.setenv(PER_AGENT_CODEX_HOME_ENV, "1")

    with pytest.raises(RuntimeError, match="not the managed auth symlink"):
        prepare_agent_codex_home(_scope(working_dir), log=lambda _message: None)


def test_discovery_uses_agent_home_not_shared_home(tmp_path, monkeypatch):
    shared_home = tmp_path / "shared"
    working_dir = tmp_path / "agent"
    working_dir.mkdir()
    agent_home = working_dir / ".codex"
    shared_match = _rollout(shared_home, "rollout-shared.jsonl", working_dir)
    agent_match = _rollout(agent_home, "rollout-agent.jsonl", working_dir)
    os.utime(shared_match, (agent_match.stat().st_mtime + 10,) * 2)
    monkeypatch.setenv("CODEX_HOME", str(shared_home))
    monkeypatch.setenv(PER_AGENT_CODEX_HOME_ENV, "1")

    assert _discover_codex_rollout(working_dir, agent=_scope(working_dir)) == agent_match


def test_subprocess_transport_overlays_prepared_home(tmp_path, monkeypatch):
    shared_home = tmp_path / "shared"
    working_dir = tmp_path / "agent"
    working_dir.mkdir()
    _auth(shared_home)
    monkeypatch.setenv("CODEX_HOME", str(shared_home))
    monkeypatch.setenv(PER_AGENT_CODEX_HOME_ENV, "1")
    config = StreamingSessionConfig(
        agent_name="test-agent",
        working_dir=str(working_dir),
        provider_url="codex_cli",
        system_prompt="compiled subprocess soul",
    )
    soul_store = _SoulStore()

    env = CodexSession(config, registry=soul_store)._build_codex_env()

    assert env["CODEX_HOME"] == str(working_dir / ".codex")
    assert (working_dir / ".codex" / "auth.json").is_symlink()
    assert (working_dir / ".codex" / "AGENTS.md").read_text(
        encoding="utf-8"
    ) == "compiled subprocess soul"
    assert soul_store.calls == [
        ("test-agent", "compiled subprocess soul", "spawn")
    ]
    assert not (shared_home / "AGENTS.md").exists()


def test_tmux_transport_preflight_validates_without_publishing_soul(
    tmp_path,
    monkeypatch,
):
    shared_home = tmp_path / "shared"
    working_dir = tmp_path / "agent"
    working_dir.mkdir()
    _auth(shared_home)
    monkeypatch.setenv("CODEX_HOME", str(shared_home))
    monkeypatch.setenv(PER_AGENT_CODEX_HOME_ENV, "1")
    config = StreamingSessionConfig(
        agent_name="test-agent",
        working_dir=str(working_dir),
        provider_url="codex_cli",
        system_prompt="compiled tmux soul",
    )
    soul_store = _SoulStore()
    session = CodexTmuxSession(config, registry=soul_store)

    session._preflight_transport_replacement()

    assert not (working_dir / ".codex").exists()
    assert soul_store.calls == []
    assert not (shared_home / "AGENTS.md").exists()


@pytest.mark.asyncio
async def test_tmux_replacement_publishes_and_snapshots_soul_once(
    tmp_path,
    monkeypatch,
):
    shared_home = tmp_path / "shared"
    working_dir = tmp_path / "agent"
    working_dir.mkdir()
    _auth(shared_home)
    monkeypatch.setenv("CODEX_HOME", str(shared_home))
    monkeypatch.setenv(PER_AGENT_CODEX_HOME_ENV, "1")
    config = StreamingSessionConfig(
        agent_name="test-agent",
        working_dir=str(working_dir),
        provider_url="codex_cli",
        system_prompt="compiled tmux soul",
    )
    soul_store = _SoulStore()
    session = CodexTmuxSession(config, registry=soul_store)

    async def _noop():
        return None

    async def _base_spawn(_session):
        _session._prepare_tmux_spawn()

    monkeypatch.setattr(session, "disconnect", _noop)
    monkeypatch.setattr(session, "_seed_codex_trust", lambda _cwd: None)
    monkeypatch.setattr(session, "_codex_dismiss_nux_and_ready", _noop)
    monkeypatch.setattr(
        "pinky_daemon.codex_tmux_session.TmuxSession._spawn_tmux_repl",
        _base_spawn,
    )

    await session.restart_transport(bring_up=session._spawn_tmux_repl)

    assert soul_store.calls == [("test-agent", "compiled tmux soul", "spawn")]


@pytest.mark.asyncio
async def test_tmux_child_spawn_inside_replacement_resnapshots_self_edit(
    tmp_path,
    monkeypatch,
):
    shared_home = tmp_path / "shared"
    working_dir = tmp_path / "agent"
    working_dir.mkdir()
    _auth(shared_home)
    monkeypatch.setenv("CODEX_HOME", str(shared_home))
    monkeypatch.setenv(PER_AGENT_CODEX_HOME_ENV, "1")
    config = StreamingSessionConfig(
        agent_name="test-agent",
        working_dir=str(working_dir),
        provider_url="codex_cli",
        system_prompt="compiled tmux soul",
    )
    soul_store = _SoulStore()
    session = CodexTmuxSession(config, registry=soul_store)
    prepare_agent_codex_home(
        config,
        log=lambda _message: None,
        soul_version_store=soul_store,
    )
    soul_store.calls.clear()
    agents_md = working_dir / ".codex" / "AGENTS.md"

    async def _noop():
        return None

    async def _base_spawn(_session):
        _session._prepare_tmux_spawn()

    async def _spawn_child_after_preflight():
        agents_md.write_text("self edit after preflight", encoding="utf-8")
        await asyncio.create_task(session._spawn_tmux_repl())

    monkeypatch.setattr(session, "disconnect", _noop)
    monkeypatch.setattr(session, "_seed_codex_trust", lambda _cwd: None)
    monkeypatch.setattr(session, "_codex_dismiss_nux_and_ready", _noop)
    monkeypatch.setattr(
        "pinky_daemon.codex_tmux_session.TmuxSession._spawn_tmux_repl",
        _base_spawn,
    )

    await session.restart_transport(
        configure=_spawn_child_after_preflight,
        bring_up=_noop,
    )

    assert agents_md.read_text(encoding="utf-8") == "compiled tmux soul"
    assert soul_store.calls == [
        ("test-agent", "self edit after preflight", "agent"),
        ("test-agent", "compiled tmux soul", "spawn"),
    ]


@pytest.mark.asyncio
async def test_tmux_refused_replacement_does_not_stale_next_spawn_soul(
    tmp_path,
    monkeypatch,
):
    shared_home = tmp_path / "shared"
    working_dir = tmp_path / "agent"
    working_dir.mkdir()
    _auth(shared_home)
    monkeypatch.setenv("CODEX_HOME", str(shared_home))
    monkeypatch.setenv(PER_AGENT_CODEX_HOME_ENV, "1")
    config = StreamingSessionConfig(
        agent_name="test-agent",
        working_dir=str(working_dir),
        provider_url="codex_cli",
        system_prompt="compiled tmux soul",
        restart_guard=lambda _session: {"restart_safe": False},
    )
    soul_store = _SoulStore()
    session = CodexTmuxSession(config, registry=soul_store)
    session._has_completed_turn = True
    prepare_agent_codex_home(
        config,
        log=lambda _message: None,
        soul_version_store=soul_store,
    )
    soul_store.calls.clear()
    agents_md = working_dir / ".codex" / "AGENTS.md"

    async def _noop():
        return None

    async def _base_spawn(_session):
        _session._prepare_tmux_spawn()

    monkeypatch.setattr(session, "_seed_codex_trust", lambda _cwd: None)
    monkeypatch.setattr(session, "_codex_dismiss_nux_and_ready", _noop)
    monkeypatch.setattr(
        "pinky_daemon.codex_tmux_session.TmuxSession._spawn_tmux_repl",
        _base_spawn,
    )

    assert await session.force_restart() is False
    assert soul_store.calls == []
    assert agents_md.read_text(encoding="utf-8") == "compiled tmux soul"

    agents_md.write_text("self edit after refused restart", encoding="utf-8")

    await session._spawn_tmux_repl()

    assert agents_md.read_text(encoding="utf-8") == "compiled tmux soul"
    assert soul_store.calls == [
        ("test-agent", "self edit after refused restart", "agent"),
        ("test-agent", "compiled tmux soul", "spawn"),
    ]


def test_tmux_app_server_boundary_uses_the_shared_soul_writer(
    tmp_path,
    monkeypatch,
):
    shared_home = tmp_path / "shared"
    working_dir = tmp_path / "agent"
    working_dir.mkdir()
    _auth(shared_home)
    monkeypatch.setenv("CODEX_HOME", str(shared_home))
    monkeypatch.setenv(PER_AGENT_CODEX_HOME_ENV, "1")
    config = StreamingSessionConfig(
        agent_name="test-agent",
        working_dir=str(working_dir),
        provider_url="codex_cli",
        system_prompt="compiled app-server soul",
    )
    soul_store = _SoulStore()
    supervisor = CodexAppServerSupervisor(
        "test-agent",
        working_dir=str(working_dir),
        agent_config=config,
        soul_version_store=soul_store,
    )

    env = supervisor._build_env()

    assert env["CODEX_HOME"] == str(working_dir / ".codex")
    assert (working_dir / ".codex" / "AGENTS.md").read_text(
        encoding="utf-8"
    ) == "compiled app-server soul"
    assert soul_store.calls == [
        ("test-agent", "compiled app-server soul", "spawn")
    ]
    assert not (shared_home / "AGENTS.md").exists()


@pytest.mark.asyncio
async def test_tmux_app_server_replacement_publishes_and_snapshots_soul_once(
    tmp_path,
    monkeypatch,
):
    shared_home = tmp_path / "shared"
    working_dir = tmp_path / "agent"
    working_dir.mkdir()
    _auth(shared_home)
    monkeypatch.setenv("CODEX_HOME", str(shared_home))
    monkeypatch.setenv(PER_AGENT_CODEX_HOME_ENV, "1")
    monkeypatch.setenv("PINKY_CODEX_APP_SERVER", "1")
    monkeypatch.setenv("PINKY_CODEX_TMUX_APP_SERVER", "1")
    config = StreamingSessionConfig(
        agent_name="test-agent",
        working_dir=str(working_dir),
        provider_url="codex_cli",
        system_prompt="compiled app-server soul",
    )
    soul_store = _SoulStore()
    session = CodexSession(config, registry=soul_store)
    assert session._app_supervisor is not None
    supervisor = session._app_supervisor

    class _Client:
        def __init__(self) -> None:
            self.is_closed = False
            self.initialized = False

        async def close(self) -> None:
            self.is_closed = True

        def set_transport_closed_handler(self, _handler) -> None:
            return None

        async def initialize(self, *, name: str, version: str) -> None:
            assert (name, version) == ("pinkybot", "1")
            self.initialized = True

    class _Proc:
        def __init__(self, returncode) -> None:
            self.pid = 4321
            self.returncode = returncode

        def kill(self) -> None:
            self.returncode = -9

        async def wait(self) -> int:
            return self.returncode

    stale_client = _Client()
    session._app_client = stale_client
    session._app_proc = _Proc(1)

    async def _noop_teardown(*, strict: bool = False) -> None:
        return None

    async def _start(**_kwargs):
        assert stale_client.is_closed is True
        assert soul_store.calls == []
        env = supervisor._build_env()
        assert env["CODEX_HOME"] == str(working_dir / ".codex")
        return _Client(), _Proc(None)

    monkeypatch.setattr(supervisor, "teardown", _noop_teardown)
    monkeypatch.setattr(supervisor, "start", _start)

    assert await session._ensure_app_server() is True

    assert stale_client.is_closed is True
    assert soul_store.calls == [
        ("test-agent", "compiled app-server soul", "spawn")
    ]


@pytest.mark.asyncio
async def test_tmux_app_server_first_start_with_absent_server_proceeds(
    tmp_path,
    monkeypatch,
):
    """R6: a missing tmux server is an idempotent pre-start cleanup result."""
    shared_home = tmp_path / "shared"
    working_dir = tmp_path / "agent"
    working_dir.mkdir()
    _auth(shared_home)
    monkeypatch.setenv("CODEX_HOME", str(shared_home))
    monkeypatch.setenv(PER_AGENT_CODEX_HOME_ENV, "1")
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.setenv("TMUX_TMPDIR", str(tmp_path / "absent-tmux"))
    config = StreamingSessionConfig(
        agent_name="test-agent",
        working_dir=str(working_dir),
        provider_url="codex_cli",
        system_prompt="compiled app-server soul",
    )
    soul_store = _SoulStore()
    prepare_agent_codex_home(
        config,
        log=lambda _message: None,
        soul_version_store=soul_store,
    )
    soul_store.calls.clear()
    agents_md = working_dir / ".codex" / "AGENTS.md"
    agents_md.write_text("self edit before first start", encoding="utf-8")
    supervisor = CodexAppServerSupervisor(
        "test-agent",
        working_dir=str(working_dir),
        agent_config=config,
        soul_version_store=soul_store,
    )
    tmux_calls: list[tuple[str, ...]] = []
    build_env_calls = 0
    new_session_calls = 0
    client_start_calls = 0
    absent_server = TmuxCommandResult(
        returncode=1,
        stdout="",
        stderr=("error connecting to /private/tmp/tmux-501/pinky-test (No such file or directory)"),
    )
    real_build_env = supervisor._build_env

    async def _tmux_run(*args, timeout=5.0, stdin_data=None):
        tmux_calls.append(args)
        if args[0] == "kill-session":
            return absent_server
        raise AssertionError(f"unexpected direct tmux call: {args}")

    async def _new_session(**_kwargs):
        nonlocal new_session_calls
        new_session_calls += 1
        return TmuxCommandResult(returncode=0, stdout="", stderr="")

    async def _await_accept():
        return object(), object()

    def _tracked_build_env():
        nonlocal build_env_calls
        build_env_calls += 1
        return real_build_env()

    class _Client:
        def __init__(self, *_args, **_kwargs):
            pass

        def start(self):
            nonlocal client_start_calls
            client_start_calls += 1

    monkeypatch.setattr(supervisor._tmux, "_run", _tmux_run)
    monkeypatch.setattr(supervisor._tmux, "new_session", _new_session)
    monkeypatch.setattr(supervisor, "_await_accept", _await_accept)
    monkeypatch.setattr(supervisor, "_build_env", _tracked_build_env)
    monkeypatch.setattr(
        "pinky_daemon.codex_app_server_tmux.CodexAppServerClient",
        _Client,
    )

    client, proc = await supervisor.start()

    assert isinstance(client, _Client)
    assert isinstance(proc, _TmuxAppServerProc)
    assert [call[0] for call in tmux_calls] == ["kill-session"]
    assert build_env_calls == 1
    assert new_session_calls == 1
    assert client_start_calls == 1
    assert agents_md.read_text(encoding="utf-8") == "compiled app-server soul"
    assert soul_store.calls == [
        ("test-agent", "self edit before first start", "agent"),
        ("test-agent", "compiled app-server soul", "spawn"),
    ]


@pytest.mark.asyncio
async def test_tmux_repl_absent_server_cold_start_proceeds(
    tmp_path,
    monkeypatch,
):
    """R8: a missing local socket remains positive cold-start absence."""
    config = StreamingSessionConfig(
        agent_name="test-agent",
        working_dir=str(tmp_path / "agent"),
    )
    tmux = _TmuxControl("pinky-test-agent")
    session = TmuxSession(config, tmux_control=tmux)
    socket_dir = tmp_path / "tmux-empty"
    monkeypatch.setenv("TMUX_TMPDIR", str(socket_dir))
    monkeypatch.delenv("TMUX", raising=False)
    tmux_calls: list[tuple[str, ...]] = []
    has_session_calls = 0

    async def _tmux_run(*args, timeout=5.0, stdin_data=None):
        nonlocal has_session_calls
        tmux_calls.append(args)
        if args[0] == "has-session":
            has_session_calls += 1
            if has_session_calls == 1:
                return TmuxCommandResult(
                    returncode=1,
                    stdout="",
                    stderr="no server running",
                )
            return TmuxCommandResult(returncode=0, stdout="", stderr="")
        if args[0] == "new-session":
            return TmuxCommandResult(returncode=0, stdout="", stderr="")
        raise AssertionError(f"unexpected tmux call: {args}")

    async def _noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr(tmux, "_run", _tmux_run)
    monkeypatch.setattr(
        session,
        "_container_agent",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        session,
        "_select_command_runner",
        lambda _container_agent: tmux._runner,
    )
    monkeypatch.setattr(session, "_ensure_container_started", _noop)
    monkeypatch.setattr(session, "_seed_container_trust", _noop)
    monkeypatch.setattr(session, "_seed_container_home_creds", _noop)
    monkeypatch.setattr(session, "_start_tailer", _noop)
    monkeypatch.setattr(session, "_build_claude_cmd", lambda: "claude")
    monkeypatch.setattr(
        "pinky_daemon.tmux_session._seed_claude_trust_file",
        lambda _path, _cwd: False,
    )
    monkeypatch.setattr(
        "pinky_daemon.tmux_session._POST_SPAWN_LIVENESS_DELAY_SEC",
        0,
    )

    await session._spawn_tmux_repl()

    assert [call[0] for call in tmux_calls] == [
        "has-session",
        "new-session",
        "has-session",
    ]


@pytest.mark.asyncio
async def test_tmux_repl_stale_kill_already_gone_race_proceeds(
    tmp_path,
    monkeypatch,
):
    """R6 sibling: a stale session that exits during kill may be respawned."""
    config = StreamingSessionConfig(
        agent_name="test-agent",
        working_dir=str(tmp_path / "agent"),
    )
    tmux = _TmuxControl("pinky-test-agent")
    session = TmuxSession(config, tmux_control=tmux)
    _mark_tmux_server_socket(tmp_path, monkeypatch)
    tmux_calls: list[tuple[str, ...]] = []
    failed_kill = TmuxCommandResult(
        returncode=1,
        stdout="",
        stderr="target disappeared during kill",
    )

    async def _tmux_run(*args, timeout=5.0, stdin_data=None):
        tmux_calls.append(args)
        if args[0] == "has-session":
            return TmuxCommandResult(returncode=0, stdout="", stderr="")
        if args[0] == "kill-session":
            return failed_kill
        if args[0] == "list-sessions":
            return TmuxCommandResult(
                returncode=0,
                stdout="pinky-some-other-session\n",
                stderr="",
            )
        if args[0] == "new-session":
            return TmuxCommandResult(returncode=0, stdout="", stderr="")
        raise AssertionError(f"unexpected tmux call: {args}")

    async def _noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr(tmux, "_run", _tmux_run)
    monkeypatch.setattr(
        session,
        "_container_agent",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        session,
        "_select_command_runner",
        lambda _container_agent: tmux._runner,
    )
    monkeypatch.setattr(session, "_ensure_container_started", _noop)
    monkeypatch.setattr(session, "_seed_container_trust", _noop)
    monkeypatch.setattr(session, "_seed_container_home_creds", _noop)
    monkeypatch.setattr(session, "_start_tailer", _noop)
    monkeypatch.setattr(session, "_build_claude_cmd", lambda: "claude")
    monkeypatch.setattr(
        "pinky_daemon.tmux_session._seed_claude_trust_file",
        lambda _path, _cwd: False,
    )
    monkeypatch.setattr(
        "pinky_daemon.tmux_session._POST_SPAWN_LIVENESS_DELAY_SEC",
        0,
    )

    await session._spawn_tmux_repl()

    assert [call[0] for call in tmux_calls] == [
        "has-session",
        "kill-session",
        "list-sessions",
        "new-session",
        "has-session",
    ]


@pytest.mark.asyncio
async def test_tmux_app_server_stale_kill_failure_refuses_before_publication(
    tmp_path,
    monkeypatch,
):
    """R5: a known-live stale tmux session is a strict spawn precondition."""
    shared_home = tmp_path / "shared"
    working_dir = tmp_path / "agent"
    working_dir.mkdir()
    _auth(shared_home)
    monkeypatch.setenv("CODEX_HOME", str(shared_home))
    monkeypatch.setenv(PER_AGENT_CODEX_HOME_ENV, "1")
    config = StreamingSessionConfig(
        agent_name="test-agent",
        working_dir=str(working_dir),
        provider_url="codex_cli",
        system_prompt="compiled app-server soul",
    )
    soul_store = _SoulStore()
    prepare_agent_codex_home(
        config,
        log=lambda _message: None,
        soul_version_store=soul_store,
    )
    soul_store.calls.clear()
    agents_md = working_dir / ".codex" / "AGENTS.md"
    agents_md.write_text("self edit before replacement", encoding="utf-8")
    supervisor = CodexAppServerSupervisor(
        "test-agent",
        working_dir=str(working_dir),
        agent_config=config,
        soul_version_store=soul_store,
    )
    _mark_tmux_server_socket(tmp_path, monkeypatch)
    build_env_calls = 0
    tmux_calls: list[tuple[str, ...]] = []
    real_build_env = supervisor._build_env

    async def _tmux_run(*args, timeout=5.0, stdin_data=None):
        tmux_calls.append(args)
        if args[0] == "kill-session":
            return TmuxCommandResult(
                returncode=1,
                stdout="",
                stderr="stale session still alive",
            )
        if args[0] == "list-sessions":
            return TmuxCommandResult(
                returncode=0,
                stdout=f"{supervisor.session_name}\n",
                stderr="",
            )
        raise AssertionError(f"unexpected direct tmux call: {args}")

    async def _unexpected_new_session(**_kwargs):
        raise AssertionError("new_session reached after failed stale kill")

    def _tracked_build_env():
        nonlocal build_env_calls
        build_env_calls += 1
        return real_build_env()

    monkeypatch.setattr(supervisor._tmux, "_run", _tmux_run)
    monkeypatch.setattr(supervisor._tmux, "new_session", _unexpected_new_session)
    monkeypatch.setattr(supervisor, "_build_env", _tracked_build_env)

    with pytest.raises(RuntimeError, match="kill-session failed"):
        await supervisor.start()

    assert build_env_calls == 0
    assert [call[0] for call in tmux_calls] == ["kill-session", "list-sessions"]
    assert agents_md.read_text(encoding="utf-8") == "self edit before replacement"
    assert soul_store.calls == []


@pytest.mark.asyncio
async def test_tmux_app_server_permission_probe_refuses_before_publication(
    tmp_path,
    monkeypatch,
):
    """R7: a returned verifier error is not proof that the target is absent."""
    shared_home = tmp_path / "shared"
    working_dir = tmp_path / "agent"
    working_dir.mkdir()
    _auth(shared_home)
    monkeypatch.setenv("CODEX_HOME", str(shared_home))
    monkeypatch.setenv(PER_AGENT_CODEX_HOME_ENV, "1")
    config = StreamingSessionConfig(
        agent_name="test-agent",
        working_dir=str(working_dir),
        provider_url="codex_cli",
        system_prompt="compiled app-server soul",
    )
    soul_store = _SoulStore()
    prepare_agent_codex_home(
        config,
        log=lambda _message: None,
        soul_version_store=soul_store,
    )
    soul_store.calls.clear()
    agents_md = working_dir / ".codex" / "AGENTS.md"
    agents_md.write_text("self edit before replacement", encoding="utf-8")
    supervisor = CodexAppServerSupervisor(
        "test-agent",
        working_dir=str(working_dir),
        agent_config=config,
        soul_version_store=soul_store,
    )
    _mark_tmux_server_socket(tmp_path, monkeypatch)
    tmux_calls: list[tuple[str, ...]] = []
    build_env_calls = 0
    permission_error = TmuxCommandResult(
        returncode=1,
        stdout="",
        stderr="couldn't create directory /blocked/tmux-501 (Permission denied)",
    )
    real_build_env = supervisor._build_env

    async def _tmux_run(*args, timeout=5.0, stdin_data=None):
        tmux_calls.append(args)
        if args[0] in {"kill-session", "list-sessions"}:
            return permission_error
        raise AssertionError(f"unexpected direct tmux call: {args}")

    async def _unexpected_new_session(**_kwargs):
        raise AssertionError("new_session reached after verifier error")

    def _tracked_build_env():
        nonlocal build_env_calls
        build_env_calls += 1
        return real_build_env()

    monkeypatch.setattr(supervisor._tmux, "_run", _tmux_run)
    monkeypatch.setattr(supervisor._tmux, "new_session", _unexpected_new_session)
    monkeypatch.setattr(supervisor, "_build_env", _tracked_build_env)

    with pytest.raises(RuntimeError, match="kill-session failed"):
        await supervisor.start()

    assert [call[0] for call in tmux_calls] == ["kill-session", "list-sessions"]
    assert build_env_calls == 0
    assert agents_md.read_text(encoding="utf-8") == "self edit before replacement"
    assert soul_store.calls == []


@pytest.mark.asyncio
async def test_tmux_kill_session_listing_timeout_propagates(tmp_path, monkeypatch):
    """R7: exceptions from the positive-absence verifier remain terminal."""
    control = _TmuxControl("pinky-test-agent")
    _mark_tmux_server_socket(tmp_path, monkeypatch)
    tmux_calls: list[tuple[str, ...]] = []

    async def _tmux_run(*args, timeout=5.0, stdin_data=None):
        tmux_calls.append(args)
        if args[0] == "kill-session":
            return TmuxCommandResult(
                returncode=1,
                stdout="",
                stderr="kill transport failed",
            )
        if args[0] == "list-sessions":
            raise asyncio.TimeoutError("tmux verifier timed out")
        raise AssertionError(f"unexpected direct tmux call: {args}")

    monkeypatch.setattr(control, "_run", _tmux_run)

    with pytest.raises(asyncio.TimeoutError, match="tmux verifier timed out"):
        await control.kill_session()

    assert [call[0] for call in tmux_calls] == ["kill-session", "list-sessions"]


@pytest.mark.asyncio
async def test_tmux_app_server_cached_kill_failure_blocks_replacement_publication(
    tmp_path,
    monkeypatch,
):
    """R5: the cached proc kill/wait seam must propagate strict cleanup failure."""
    shared_home = tmp_path / "shared"
    working_dir = tmp_path / "agent"
    working_dir.mkdir()
    _auth(shared_home)
    monkeypatch.setenv("CODEX_HOME", str(shared_home))
    monkeypatch.setenv(PER_AGENT_CODEX_HOME_ENV, "1")
    monkeypatch.setenv("PINKY_CODEX_APP_SERVER", "1")
    monkeypatch.setenv("PINKY_CODEX_TMUX_APP_SERVER", "1")
    config = StreamingSessionConfig(
        agent_name="test-agent",
        working_dir=str(working_dir),
        provider_url="codex_cli",
        system_prompt="compiled app-server soul",
    )
    soul_store = _SoulStore()
    prepare_agent_codex_home(
        config,
        log=lambda _message: None,
        soul_version_store=soul_store,
    )
    soul_store.calls.clear()
    agents_md = working_dir / ".codex" / "AGENTS.md"
    agents_md.write_text("self edit before replacement", encoding="utf-8")
    session = CodexSession(config, registry=soul_store)
    assert session._app_supervisor is not None
    supervisor = session._app_supervisor

    class _Client:
        is_closed = True

        async def close(self) -> None:
            return None

    async def _failed_kill():
        return TmuxCommandResult(
            returncode=1,
            stdout="",
            stderr="stale session still alive",
        )

    async def _unexpected_start(**_kwargs):
        raise AssertionError("replacement start reached after failed cached kill")

    monkeypatch.setattr(supervisor._tmux, "kill_session", _failed_kill)
    monkeypatch.setattr(supervisor, "start", _unexpected_start)
    stale_proc = _TmuxAppServerProc(supervisor, pid=4321)
    session._app_client = _Client()
    session._app_proc = stale_proc

    with pytest.raises(RuntimeError, match="kill-session failed"):
        await session._ensure_app_server()

    assert session._app_proc is stale_proc
    assert agents_md.read_text(encoding="utf-8") == "self edit before replacement"
    assert soul_store.calls == []


@pytest.mark.asyncio
async def test_direct_app_server_failed_shutdown_handle_blocks_replacement_publication(
    tmp_path,
    monkeypatch,
):
    """R5 sibling: best-effort shutdown retains proof of a failed direct kill."""
    shared_home = tmp_path / "shared"
    working_dir = tmp_path / "agent"
    working_dir.mkdir()
    _auth(shared_home)
    monkeypatch.setenv("CODEX_HOME", str(shared_home))
    monkeypatch.setenv(PER_AGENT_CODEX_HOME_ENV, "1")
    monkeypatch.setenv("PINKY_CODEX_APP_SERVER", "1")
    monkeypatch.delenv("PINKY_CODEX_TMUX_APP_SERVER", raising=False)
    config = StreamingSessionConfig(
        agent_name="test-agent",
        working_dir=str(working_dir),
        provider_url="codex_cli",
        system_prompt="compiled app-server soul",
    )
    soul_store = _SoulStore()
    prepare_agent_codex_home(
        config,
        log=lambda _message: None,
        soul_version_store=soul_store,
    )
    soul_store.calls.clear()
    agents_md = working_dir / ".codex" / "AGENTS.md"
    agents_md.write_text("self edit before replacement", encoding="utf-8")
    session = CodexSession(config, registry=soul_store)

    class _Client:
        is_closed = False

        async def close(self) -> None:
            self.is_closed = True

    class _Proc:
        returncode = None
        pid = 4321

        def kill(self) -> None:
            raise RuntimeError("direct kill failed")

        async def wait(self) -> int:
            raise AssertionError("wait reached after failed direct kill")

    async def _unexpected_spawn(**_kwargs):
        raise AssertionError("replacement spawn reached after failed direct kill")

    stale_proc = _Proc()
    session._app_client = _Client()
    session._app_proc = stale_proc
    monkeypatch.setattr(
        "pinky_daemon.codex_session.spawn_app_server",
        _unexpected_spawn,
    )

    with pytest.raises(RuntimeError, match="direct kill failed"):
        await session.restart_transport()

    assert session._app_proc is stale_proc
    assert agents_md.read_text(encoding="utf-8") == "self edit before replacement"
    assert soul_store.calls == []


@pytest.mark.asyncio
async def test_tmux_repl_stale_kill_failure_refuses_before_publication(
    tmp_path,
    monkeypatch,
):
    """R5 sibling: inherited tmux REPL cleanup gates Codex publication too."""
    shared_home = tmp_path / "shared"
    working_dir = tmp_path / "agent"
    working_dir.mkdir()
    _auth(shared_home)
    monkeypatch.setenv("CODEX_HOME", str(shared_home))
    monkeypatch.setenv(PER_AGENT_CODEX_HOME_ENV, "1")
    config = StreamingSessionConfig(
        agent_name="test-agent",
        working_dir=str(working_dir),
        provider_url="codex_cli",
        system_prompt="compiled tmux soul",
    )
    soul_store = _SoulStore()
    prepare_agent_codex_home(
        config,
        log=lambda _message: None,
        soul_version_store=soul_store,
    )
    soul_store.calls.clear()
    agents_md = working_dir / ".codex" / "AGENTS.md"
    agents_md.write_text("self edit before replacement", encoding="utf-8")
    session = CodexTmuxSession(config, registry=soul_store)
    _mark_tmux_server_socket(tmp_path, monkeypatch)
    tmux_calls: list[tuple[str, ...]] = []

    async def _noop(*_args, **_kwargs):
        return None

    async def _tmux_run(*args, timeout=5.0, stdin_data=None):
        tmux_calls.append(args)
        if args[0] == "has-session":
            return TmuxCommandResult(returncode=0, stdout="", stderr="")
        if args[0] == "kill-session":
            return TmuxCommandResult(
                returncode=1,
                stdout="",
                stderr="stale session still alive",
            )
        if args[0] == "list-sessions":
            return TmuxCommandResult(
                returncode=0,
                stdout=f"{session._session_name}\n",
                stderr="",
            )
        raise AssertionError(f"unexpected direct tmux call: {args}")

    async def _unexpected_new_session(**_kwargs):
        raise AssertionError("new_session reached after failed stale kill")

    monkeypatch.setattr(
        session,
        "_container_agent",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        session,
        "_select_command_runner",
        lambda _container_agent: session._tmux._runner,
    )
    monkeypatch.setattr(session, "_ensure_container_started", _noop)
    monkeypatch.setattr(
        "pinky_daemon.tmux_session._seed_claude_trust_file",
        lambda _path, _cwd: False,
    )
    monkeypatch.setattr(session._tmux, "_run", _tmux_run)
    monkeypatch.setattr(session._tmux, "new_session", _unexpected_new_session)

    with pytest.raises(RuntimeError, match="kill-session failed"):
        await session._spawn_tmux_repl()

    assert [call[0] for call in tmux_calls] == [
        "has-session",
        "kill-session",
        "list-sessions",
    ]
    assert agents_md.read_text(encoding="utf-8") == "self edit before replacement"
    assert soul_store.calls == []


@pytest.mark.asyncio
async def test_tmux_repl_permission_probe_refuses_before_publication(
    tmp_path,
    monkeypatch,
):
    """R7 sibling: returned verifier errors gate shared REPL publication."""
    shared_home = tmp_path / "shared"
    working_dir = tmp_path / "agent"
    working_dir.mkdir()
    _auth(shared_home)
    monkeypatch.setenv("CODEX_HOME", str(shared_home))
    monkeypatch.setenv(PER_AGENT_CODEX_HOME_ENV, "1")
    config = StreamingSessionConfig(
        agent_name="test-agent",
        working_dir=str(working_dir),
        provider_url="codex_cli",
        system_prompt="compiled tmux soul",
    )
    soul_store = _SoulStore()
    prepare_agent_codex_home(
        config,
        log=lambda _message: None,
        soul_version_store=soul_store,
    )
    soul_store.calls.clear()
    agents_md = working_dir / ".codex" / "AGENTS.md"
    agents_md.write_text("self edit before replacement", encoding="utf-8")
    session = CodexTmuxSession(config, registry=soul_store)
    _mark_tmux_server_socket(tmp_path, monkeypatch)
    tmux_calls: list[tuple[str, ...]] = []
    permission_error = TmuxCommandResult(
        returncode=1,
        stdout="",
        stderr="couldn't create directory /blocked/tmux-501 (Permission denied)",
    )

    async def _noop(*_args, **_kwargs):
        return None

    async def _tmux_run(*args, timeout=5.0, stdin_data=None):
        tmux_calls.append(args)
        if args[0] == "has-session":
            return TmuxCommandResult(returncode=0, stdout="", stderr="")
        if args[0] in {"kill-session", "list-sessions"}:
            return permission_error
        raise AssertionError(f"unexpected direct tmux call: {args}")

    async def _unexpected_new_session(**_kwargs):
        raise AssertionError("new_session reached after verifier error")

    monkeypatch.setattr(
        session,
        "_container_agent",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        session,
        "_select_command_runner",
        lambda _container_agent: session._tmux._runner,
    )
    monkeypatch.setattr(session, "_ensure_container_started", _noop)
    monkeypatch.setattr(
        "pinky_daemon.tmux_session._seed_claude_trust_file",
        lambda _path, _cwd: False,
    )
    monkeypatch.setattr(session._tmux, "_run", _tmux_run)
    monkeypatch.setattr(session._tmux, "new_session", _unexpected_new_session)

    with pytest.raises(RuntimeError, match="kill-session failed"):
        await session._spawn_tmux_repl()

    assert [call[0] for call in tmux_calls] == [
        "has-session",
        "kill-session",
        "list-sessions",
    ]
    assert agents_md.read_text(encoding="utf-8") == "self edit before replacement"
    assert soul_store.calls == []


@pytest.mark.asyncio
async def test_tmux_repl_returned_has_session_error_refuses_before_spawn_side_effects(
    tmp_path,
    monkeypatch,
):
    """R8: an ambiguous stale-session probe must fail before spawn effects."""
    shared_home = tmp_path / "shared"
    working_dir = tmp_path / "agent"
    working_dir.mkdir()
    _auth(shared_home)
    monkeypatch.setenv("CODEX_HOME", str(shared_home))
    monkeypatch.setenv(PER_AGENT_CODEX_HOME_ENV, "1")
    config = StreamingSessionConfig(
        agent_name="test-agent",
        working_dir=str(working_dir),
        provider_url="codex_cli",
        system_prompt="compiled tmux soul",
    )
    soul_store = _SoulStore()
    prepare_agent_codex_home(
        config,
        log=lambda _message: None,
        soul_version_store=soul_store,
    )
    soul_store.calls.clear()
    agents_md = working_dir / ".codex" / "AGENTS.md"
    agents_md.write_text("self edit before replacement", encoding="utf-8")
    session = CodexTmuxSession(config, registry=soul_store)
    _mark_tmux_server_socket(tmp_path, monkeypatch)
    tmux_calls: list[tuple[str, ...]] = []
    env_build_count = 0
    publication_count = 0
    new_session_count = 0
    permission_error = TmuxCommandResult(
        returncode=1,
        stdout="",
        stderr="couldn't create directory /blocked/tmux-501 (Permission denied)",
    )
    real_build_env = session._build_repl_env
    real_prepare_spawn = session._prepare_tmux_spawn

    async def _noop(*_args, **_kwargs):
        return None

    async def _tmux_run(*args, timeout=5.0, stdin_data=None):
        tmux_calls.append(args)
        if args[0] in {"has-session", "list-sessions"}:
            return permission_error
        raise AssertionError(f"unexpected direct tmux call: {args}")

    async def _tracked_new_session(**_kwargs):
        nonlocal new_session_count
        new_session_count += 1
        return TmuxCommandResult(
            returncode=1,
            stdout="",
            stderr="new-session reached after ambiguous has-session probe",
        )

    def _tracked_build_env():
        nonlocal env_build_count
        env_build_count += 1
        return real_build_env()

    def _tracked_prepare_spawn():
        nonlocal publication_count
        publication_count += 1
        return real_prepare_spawn()

    monkeypatch.setattr(
        session,
        "_container_agent",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        session,
        "_select_command_runner",
        lambda _container_agent: session._tmux._runner,
    )
    monkeypatch.setattr(session, "_ensure_container_started", _noop)
    monkeypatch.setattr(session, "_seed_container_trust", _noop)
    monkeypatch.setattr(session, "_seed_container_home_creds", _noop)
    monkeypatch.setattr(
        "pinky_daemon.tmux_session._seed_claude_trust_file",
        lambda _path, _cwd: False,
    )
    monkeypatch.setattr(session._tmux, "_run", _tmux_run)
    monkeypatch.setattr(session._tmux, "new_session", _tracked_new_session)
    monkeypatch.setattr(session, "_build_repl_env", _tracked_build_env)
    monkeypatch.setattr(session, "_prepare_tmux_spawn", _tracked_prepare_spawn)

    with pytest.raises(RuntimeError, match="has-session failed"):
        await session._spawn_tmux_repl()

    assert [call[0] for call in tmux_calls] == [
        "has-session",
        "list-sessions",
    ]
    assert env_build_count == 0
    assert publication_count == 0
    assert new_session_count == 0
    assert agents_md.read_text(encoding="utf-8") == "self edit before replacement"
    assert soul_store.calls == []


def test_reverse_migration_moves_only_matching_rollouts(tmp_path):
    shared_home = tmp_path / "shared"
    working_dir = tmp_path / "agent"
    other_dir = tmp_path / "other"
    working_dir.mkdir()
    other_dir.mkdir()
    agent_home = working_dir / ".codex"
    matching = _rollout(agent_home, "rollout-match.jsonl", working_dir)
    other = _rollout(agent_home, "rollout-other.jsonl", other_dir)
    logs: list[str] = []

    moved = rollback_agent_rollouts(
        working_dir,
        agent_home,
        shared_home=shared_home,
        log=logs.append,
    )

    restored = shared_home / "sessions" / matching.relative_to(agent_home / "sessions")
    assert moved == 1
    assert restored.exists()
    assert not matching.exists()
    assert other.exists()
    assert any("rolled back 1 cwd-matched rollout(s)" in message for message in logs)
