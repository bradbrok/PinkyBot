"""Rollback-journal configuration for the long-lived daemon stores.

Regression cover for the 2026-08-01 orphaned-WAL incident: WAL mode plus
per-thread connections let an external process unlink the ``-wal`` out from
under the daemon, stranding committed writes in its address space.
"""

from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from pinky_daemon.activity_store import ActivityStore
from pinky_daemon.agent_comms import AgentComms
from pinky_daemon.analytics_store import AnalyticsStore
from pinky_daemon.app_store import AppStore
from pinky_daemon.conversation_store import ConversationStore
from pinky_daemon.hooks import AuditStore
from pinky_daemon.kb_store import KBStore
from pinky_daemon.librarian_runner import LibrarianRunner
from pinky_daemon.mesh_store import MeshStore
from pinky_daemon.message_context_store import MessageContextStore
from pinky_daemon.outreach_config import OutreachConfigStore
from pinky_daemon.plugin_manager import PluginManager
from pinky_daemon.presentation_store import PresentationStore
from pinky_daemon.research_store import ResearchStore
from pinky_daemon.session_store import SessionEventStore, SessionStore
from pinky_daemon.sqlite_journal import (
    SqliteJournalConfigError,
    configure_rollback_journal,
)
from pinky_daemon.task_store import TaskStore
from pinky_daemon.trigger_store import TriggerStore
from pinky_daemon.user_profile_store import UserProfileStore
from pinky_daemon.voice_store import VoiceStore


def _journal_mode(db_path: Path) -> str:
    """Journal mode seen by a *fresh* connection, i.e. what the file persists.

    SQLite only records "WAL vs rollback" in the database header — it does not
    persist *which* rollback mode. A new connection therefore reports the
    compile-time default ("delete") even when the writer set TRUNCATE, so
    callers assert on the family (``!= "wal"``), not on the exact value.
    """
    conn = sqlite3.connect(db_path)
    try:
        return str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()
    finally:
        conn.close()


def test_configure_returns_truncate_on_fresh_db(tmp_path):
    conn = sqlite3.connect(tmp_path / "fresh.db")
    assert configure_rollback_journal(conn, db_label="fresh.db") == "truncate"
    conn.close()


def test_converts_an_existing_wal_database_in_place(tmp_path):
    db_path = tmp_path / "legacy.db"
    seed = sqlite3.connect(db_path)
    seed.execute("PRAGMA journal_mode=WAL")
    seed.execute("CREATE TABLE t (v TEXT)")
    seed.execute("INSERT INTO t VALUES ('committed-under-wal')")
    seed.commit()
    seed.close()
    assert _journal_mode(db_path) == "wal"

    conn = sqlite3.connect(db_path)
    assert configure_rollback_journal(conn, db_label="legacy.db") == "truncate"

    # Content written under WAL survives the conversion — the helper checkpoints
    # before dropping the wal-index.
    assert conn.execute("SELECT v FROM t").fetchone()[0] == "committed-under-wal"
    conn.close()
    assert _journal_mode(db_path) != "wal"


def test_leaves_no_wal_or_shm_sidecars(tmp_path):
    """The whole point: no ``-wal``/``-shm`` for another process to unlink."""
    db_path = tmp_path / "sidecars.db"
    conn = sqlite3.connect(db_path)
    configure_rollback_journal(conn, db_label="sidecars.db")
    conn.execute("CREATE TABLE t (v TEXT)")
    conn.execute("INSERT INTO t VALUES ('x')")
    conn.commit()

    assert not (tmp_path / "sidecars.db-wal").exists()
    assert not (tmp_path / "sidecars.db-shm").exists()
    conn.close()


def test_committed_writes_are_visible_to_a_separate_process_handle(tmp_path):
    """The incident symptom, inverted: an outside reader must see the write.

    Under the orphaned WAL, ``sqlite3`` from outside saw data frozen at the last
    checkpoint while the daemon reported success.
    """
    db_path = tmp_path / "visible.db"
    daemon_conn = sqlite3.connect(db_path)
    configure_rollback_journal(daemon_conn, db_label="visible.db")
    daemon_conn.execute("CREATE TABLE t (v TEXT)")
    daemon_conn.execute("INSERT INTO t VALUES ('written-by-daemon')")
    daemon_conn.commit()

    outsider = sqlite3.connect(db_path)
    assert outsider.execute("SELECT v FROM t").fetchone()[0] == "written-by-daemon"
    outsider.close()
    daemon_conn.close()


class _RefusesModeSwitch(sqlite3.Connection):
    """A connection that always reports the database as locked for the switch.

    ``sqlite3.Connection`` is a C type, so its ``execute`` cannot be
    monkeypatched on an instance — subclassing via ``connect(factory=...)`` is
    the way to simulate a database that will not leave WAL.
    """

    def execute(self, sql, *args):  # noqa: D102 - inherited contract
        if "journal_mode=TRUNCATE" in sql:
            raise sqlite3.OperationalError("database is locked")
        return super().execute(sql, *args)


def test_raises_rather_than_silently_staying_on_wal(tmp_path):
    """A silent WAL fallback is the state that loses writes — it must fail loud."""
    conn = sqlite3.connect(tmp_path / "stuck.db", factory=_RefusesModeSwitch)

    with pytest.raises(SqliteJournalConfigError, match="refused to leave WAL"):
        configure_rollback_journal(conn, db_label="stuck.db", retries=2)
    conn.close()


# Seeds a conversations.db under WAL and exits WITHOUT closing the connection,
# leaving a hot -wal behind — the state conversation_store's checkpoint has to
# survive. os._exit skips interpreter cleanup, so SQLite never checkpoints.
_HOT_WAL_SEEDER = """
import os, sys
sys.path.insert(0, {src!r})
import sqlite3
from pinky_daemon.conversation_store import ConversationStore

store = ConversationStore(db_path={db!r})
store._conn.execute("PRAGMA journal_mode=WAL")
store.append("s1", "user", "checkpointed under wal")
store.append("s1", "agent", "hot in the wal file")
assert store._conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
os._exit(0)
"""


def test_fts5_shadow_tables_survive_the_wal_conversion(tmp_path):
    """conversation_store.py warns that checkpointing with live writers corrupts
    the FTS5 shadow tables. Our checkpoint runs at connection open, before table
    init and with no concurrent writer — this pins that it is in fact safe, on a
    genuinely hot WAL rather than on the reasoning alone.
    """
    db_path = tmp_path / "conversations.db"
    src = str(Path(__file__).resolve().parents[1] / "src")
    seeded = subprocess.run(
        [sys.executable, "-c", _HOT_WAL_SEEDER.format(src=src, db=str(db_path))],
        capture_output=True,
        text=True,
    )
    assert seeded.returncode == 0, seeded.stderr
    assert db_path.with_name("conversations.db-wal").exists(), "no hot WAL to convert"

    # Opening the store checkpoints the hot WAL and converts to rollback mode.
    store = ConversationStore(db_path=str(db_path))

    integrity = store._conn.execute(
        "INSERT INTO messages_fts(messages_fts) VALUES('integrity-check')"
    )
    assert integrity is not None  # raises DatabaseError if the index is corrupt

    # Both the checkpointed and the WAL-resident row are searchable...
    assert len(store.search("checkpointed")) == 1
    assert len(store.search("hot")) == 1
    # ...and the triggers keep indexing after the conversion.
    store.append("s1", "user", "indexed after the conversion")
    assert len(store.search("conversion")) == 1
    store.close()


@pytest.mark.parametrize(
    ("factory", "filename", "conn_attr"),
    [
        (ConversationStore, "conversations.db", "_conn"),
        (TaskStore, "tasks.db", "_db"),
        (AgentComms, "agent_comms.db", "_conn"),
        (ResearchStore, "research.db", "_db"),
        (ActivityStore, "activity.db", "_db"),
        (AppStore, "apps.db", "_db"),
        (AuditStore, "audit.db", "_db"),
        (MeshStore, "mesh.db", "_db"),
        (MessageContextStore, "message_context.db", "_db"),
        (OutreachConfigStore, "outreach_config.db", "_db"),
        (PresentationStore, "presentations.db", "_db"),
        (SessionStore, "sessions.db", "_db"),
        (SessionEventStore, "session_events.db", "_db"),
        (TriggerStore, "triggers.db", "_db"),
        (UserProfileStore, "user_profiles.db", "_db"),
        (VoiceStore, "voice_calls.db", "_db"),
    ],
)
def test_stores_open_in_rollback_mode(tmp_path, factory, filename, conn_attr):
    db_path = tmp_path / filename
    store = factory(db_path=str(db_path))
    # Touch the lazy per-thread connection so the store actually opens.
    assert getattr(store, conn_attr) is not None
    assert _journal_mode(db_path) != "wal"
    assert not (tmp_path / f"{filename}-wal").exists()


def test_per_call_connection_stores_open_in_rollback_mode(tmp_path):
    """Stores that open a fresh connection per call, not one long-lived handle.

    These are just as exposed: two overlapping short connections in one process
    still share POSIX locks, so whichever closes first drops the locks the other
    is relying on.
    """
    kb = KBStore(data_dir=str(tmp_path / "kb"))
    assert _journal_mode(Path(kb.db_path)) != "wal"
    assert not Path(str(kb.db_path) + "-wal").exists()

    librarian_db = tmp_path / "librarian_state.db"
    LibrarianRunner(kb, db_path=str(librarian_db))
    assert _journal_mode(librarian_db) != "wal"

    analytics_db = tmp_path / "analytics.db"
    AnalyticsStore(db_path=str(analytics_db))
    assert _journal_mode(analytics_db) != "wal"

    plugins_db = tmp_path / "plugins.db"
    PluginManager(
        db_path=str(plugins_db),
        api_url="http://localhost:8888",
        working_dir=str(tmp_path / "wd"),
    )
    assert _journal_mode(plugins_db) != "wal"


def test_no_daemon_module_still_asks_for_wal():
    """Guard against a new store landing back on WAL by copy-paste."""
    src = Path(__file__).resolve().parents[1] / "src" / "pinky_daemon"
    offenders = [
        f"{path.relative_to(src)}:{n}"
        for path in sorted(src.rglob("*.py"))
        for n, line in enumerate(path.read_text().splitlines(), 1)
        if "journal_mode=WAL" in line
    ]
    assert offenders == [], (
        "these daemon modules still open SQLite in WAL mode — use "
        f"configure_rollback_journal() instead: {offenders}"
    )
