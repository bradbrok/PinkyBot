"""Tests for claude_runner._find_claude_binary resolution order.

Regression guard for the native-install fallback: `claude install` removes
the npm-global package and drops the binary in ~/.local/bin/claude, which is
not on the daemon's launchd PATH. Without the fallback the daemon would fail
to resolve a binary and couldn't spawn agents (real near-miss, 2026-05-29).
"""

from __future__ import annotations

from pinky_daemon import claude_runner


def _make_executable(path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n")


def test_falls_back_to_native_local_bin(monkeypatch, tmp_path) -> None:
    """PATH has no claude, but the native ~/.local/bin install resolves."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(claude_runner.shutil, "which", lambda _name: None)

    native = tmp_path / ".local" / "bin" / "claude"
    _make_executable(native)

    assert claude_runner._find_claude_binary() == str(native)


def test_path_resolution_wins_over_native(monkeypatch, tmp_path) -> None:
    """A claude on PATH is preferred over the native fallback."""
    monkeypatch.setenv("HOME", str(tmp_path))

    on_path = tmp_path / "pathbin" / "claude"
    _make_executable(on_path)
    native = tmp_path / ".local" / "bin" / "claude"
    _make_executable(native)

    monkeypatch.setattr(claude_runner.shutil, "which", lambda _name: str(on_path))

    assert claude_runner._find_claude_binary() == str(on_path)


def test_raises_when_no_binary_anywhere(monkeypatch, tmp_path) -> None:
    """No PATH hit and no fallback files → FileNotFoundError (unchanged)."""
    monkeypatch.setenv("HOME", str(tmp_path))  # empty home, no ~/.local/bin
    monkeypatch.setattr(claude_runner.shutil, "which", lambda _name: None)
    # Neutralize the absolute fallback so the test doesn't depend on whether
    # /usr/local/bin/claude happens to exist on the runner.
    monkeypatch.setattr(claude_runner.os.path, "isfile", lambda _p: False)

    try:
        claude_runner._find_claude_binary()
    except FileNotFoundError:
        pass
    else:  # pragma: no cover - failure path
        raise AssertionError("expected FileNotFoundError when no binary exists")
