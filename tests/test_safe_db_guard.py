"""Tests for scripts/_safe_db.py — the live-store in-place-open guard.

Covers holder detection for both memory-store topologies (shared-MCP pool
inside the daemon process, legacy stdio pinky_memory), fail-closed behavior
on detection failure, and the no-holder / non-candidate allow paths.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import _safe_db  # noqa: E402


@pytest.fixture()
def stores(tmp_path, monkeypatch):
    """Scratch data/ root with one main store and one agent memory store."""
    data_dir = (tmp_path / "data").resolve()
    main_db = data_dir / "conversations.db"
    mem_db = data_dir / "agents" / "tabby" / "data" / "memory.db"
    mem_db.parent.mkdir(parents=True)
    mem_db.touch()
    main_db.touch()
    monkeypatch.setattr(_safe_db, "DATA_DIR", data_dir)
    return SimpleNamespace(data=data_dir, main=main_db, memory=mem_db)


def _patch_topology(monkeypatch, *, daemon: bool, pinky_memory: bool) -> list[str]:
    """Replace _proc_running with a deterministic process topology."""
    calls: list[str] = []
    running = {"pinky_daemon": daemon, "pinky_memory": pinky_memory}

    def fake(pattern: str) -> bool:
        calls.append(pattern)
        assert pattern in running, f"unexpected pgrep pattern {pattern!r}"
        return running[pattern]

    monkeypatch.setattr(_safe_db, "_proc_running", fake)
    return calls


def _completed(returncode: int, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess(
        args=["pgrep"], returncode=returncode, stdout=stdout, stderr=stderr
    )


# ---------------------------------------------------------------- holders


def test_main_store_refused_while_daemon_runs(stores, monkeypatch):
    _patch_topology(monkeypatch, daemon=True, pinky_memory=False)
    for write in (True, False):
        with pytest.raises(SystemExit) as exc:
            _safe_db.refuse_if_live_store(stores.main, write=write)
        assert "REFUSED" in str(exc.value)
        assert "store_snapshot.py" in str(exc.value)


def test_memory_store_refused_in_shared_mcp_topology(stores, monkeypatch):
    """The production topology: no pinky_memory process, pool inside daemon."""
    _patch_topology(monkeypatch, daemon=True, pinky_memory=False)
    with pytest.raises(SystemExit) as exc:
        _safe_db.refuse_if_live_store(stores.memory, write=False)
    assert "shared-MCP memory pool" in str(exc.value)


def test_memory_store_refused_in_stdio_topology(stores, monkeypatch):
    _patch_topology(monkeypatch, daemon=False, pinky_memory=True)
    with pytest.raises(SystemExit) as exc:
        _safe_db.refuse_if_live_store(stores.memory, write=True)
    assert "pinky-memory MCP" in str(exc.value)


def test_no_holder_allows_open(stores, monkeypatch):
    _patch_topology(monkeypatch, daemon=False, pinky_memory=False)
    _safe_db.refuse_if_live_store(stores.main, write=True)
    _safe_db.refuse_if_live_store(stores.memory, write=True)


def test_non_candidate_paths_never_probe_processes(stores, tmp_path, monkeypatch):
    calls = _patch_topology(monkeypatch, daemon=True, pinky_memory=True)
    outside = tmp_path / "elsewhere.db"
    outside.touch()
    nested_non_memory = stores.data / "agents" / "tabby" / "data" / "notes.db"
    nested_non_memory.touch()
    _safe_db.refuse_if_live_store(outside, write=True)
    _safe_db.refuse_if_live_store(nested_non_memory, write=True)
    assert calls == []


def test_snapshot_pointer_resolves():
    """The refusal text points at store_snapshot.py; it must exist."""
    assert (SCRIPTS_DIR / "store_snapshot.py").is_file()


# ------------------------------------------------- fail-closed detection


@pytest.mark.parametrize(
    "failure",
    [
        FileNotFoundError("pgrep unavailable"),
        subprocess.TimeoutExpired(cmd=["pgrep"], timeout=5),
    ],
    ids=["pgrep-missing", "pgrep-timeout"],
)
def test_detection_failure_refuses_protected_paths(stores, monkeypatch, failure):
    def raising_run(*args, **kwargs):
        raise failure

    monkeypatch.setattr(_safe_db.subprocess, "run", raising_run)
    for path in (stores.main, stores.memory):
        with pytest.raises(SystemExit) as exc:
            _safe_db.refuse_if_live_store(path, write=False)
        assert "fail closed" in str(exc.value)


def test_pgrep_error_status_refuses(stores, monkeypatch):
    monkeypatch.setattr(
        _safe_db.subprocess,
        "run",
        lambda *a, **k: _completed(2, stderr="pgrep: invalid option"),
    )
    with pytest.raises(SystemExit) as exc:
        _safe_db.refuse_if_live_store(stores.main, write=True)
    assert "fail closed" in str(exc.value)


def test_pgrep_no_match_is_the_only_allow(stores, monkeypatch):
    monkeypatch.setattr(_safe_db.subprocess, "run", lambda *a, **k: _completed(1))
    _safe_db.refuse_if_live_store(stores.main, write=True)


def test_pgrep_match_refuses_even_with_empty_stdout(stores, monkeypatch):
    monkeypatch.setattr(
        _safe_db.subprocess, "run", lambda *a, **k: _completed(0, stdout="")
    )
    with pytest.raises(SystemExit):
        _safe_db.refuse_if_live_store(stores.main, write=True)


def test_proc_running_maps_statuses(monkeypatch):
    monkeypatch.setattr(
        _safe_db.subprocess, "run", lambda *a, **k: _completed(0, stdout="123\n")
    )
    assert _safe_db._proc_running("x") is True
    monkeypatch.setattr(_safe_db.subprocess, "run", lambda *a, **k: _completed(1))
    assert _safe_db._proc_running("x") is False
    monkeypatch.setattr(_safe_db.subprocess, "run", lambda *a, **k: _completed(3))
    with pytest.raises(_safe_db.HolderDetectionError):
        _safe_db._proc_running("x")
