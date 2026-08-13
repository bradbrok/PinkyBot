"""Per-agent Codex home isolation, auth, and rollout migration tests."""

from __future__ import annotations

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


def _scope(working_dir: Path, *, codex_home: str = "") -> SimpleNamespace:
    return SimpleNamespace(
        agent_name="test-agent",
        working_dir=str(working_dir),
        codex_home=codex_home,
    )


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


def test_flag_off_preserves_shared_path_and_performs_no_bootstrap(tmp_path, monkeypatch):
    shared_home = tmp_path / "shared"
    working_dir = tmp_path / "agent"
    override = tmp_path / "override"
    monkeypatch.setenv("CODEX_HOME", str(shared_home))
    monkeypatch.delenv(PER_AGENT_CODEX_HOME_ENV, raising=False)
    monkeypatch.setenv(codex_home_mod.MIGRATION_LOCK_TIMEOUT_ENV, "invalid")
    scope = _scope(working_dir, codex_home=str(override))

    assert codex_home_for(scope) == shared_home
    assert prepare_agent_codex_home(scope, log=lambda _message: None) == shared_home
    assert not working_dir.exists()
    assert not override.exists()
    assert not shared_home.exists()


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

    def unavailable(*_args, **_kwargs):
        raise FileNotFoundError("lsof")

    monkeypatch.setattr(codex_home_mod.subprocess, "run", unavailable)

    retired = codex_home_mod._retire_rollout_claim(
        claim,
        destination,
        os.path.realpath(working_dir),
        log=logs.append,
    )

    assert retired is False
    assert claim.exists()
    assert any("descriptor scan unavailable" in message for message in logs)


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

    claim = codex_home_mod._claim_rollout(source)

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
    """R4 debt: marker fast paths sweep only stale empty private sidecars."""
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
    assert recent.exists()
    assert nonempty.read_text(encoding="utf-8") == "operator bytes"
    assert any("removed stale migration reservation" in message for message in logs)


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
    )

    env = CodexSession(config)._build_codex_env()

    assert env["CODEX_HOME"] == str(working_dir / ".codex")
    assert (working_dir / ".codex" / "auth.json").is_symlink()


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
