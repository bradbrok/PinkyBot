"""Tests for the conftest guard that keeps the suite off production DBs (#355)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

REPO_DATA = Path(__file__).resolve().parent.parent / "data"


def test_relative_store_default_is_rejected():
    """The exact shape ~24 stores use as their default db_path."""
    with pytest.raises(RuntimeError, match="production database"):
        sqlite3.connect("data/tasks.db")


def test_absolute_path_into_repo_data_is_rejected():
    with pytest.raises(RuntimeError, match="production database"):
        sqlite3.connect(str(REPO_DATA / "agents" / "engineer" / "memory.db"))


def test_path_object_is_rejected():
    with pytest.raises(RuntimeError, match="production database"):
        sqlite3.connect(REPO_DATA / "tasks.db")


def test_readonly_uri_into_data_is_rejected():
    """auth.py opens its DB via a ``file:...?mode=ro`` URI."""
    with pytest.raises(RuntimeError, match="production database"):
        sqlite3.connect("file:data/agents.db?mode=ro", uri=True)


def test_tmp_path_is_allowed(tmp_path):
    conn = sqlite3.connect(tmp_path / "fine.db")
    conn.close()
    assert (tmp_path / "fine.db").exists()


def test_tmp_data_dir_is_allowed(tmp_path):
    """A ``data/`` dir that isn't a checkout's is legitimate."""
    (tmp_path / "data").mkdir()
    conn = sqlite3.connect(tmp_path / "data" / "fine.db")
    conn.close()


def test_in_memory_is_allowed():
    sqlite3.connect(":memory:").close()
    sqlite3.connect("file::memory:?cache=shared", uri=True).close()


def test_relative_data_path_rejected_even_after_chdir(tmp_path, monkeypatch):
    """cwd is re-read per call, and ``<cwd>/data`` is guarded either way.

    Chdir'ing into a tmpdir first is not an escape hatch: the rule is
    "pass an explicit path", so a relative ``data/`` default stays refused.
    """
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    with pytest.raises(RuntimeError, match="production database"):
        sqlite3.connect("data/tasks.db")
    assert not (tmp_path / "data" / "tasks.db").exists()
