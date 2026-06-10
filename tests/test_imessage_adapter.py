"""Tests for iMessageAdapter chat.db init — focused on the timeout guard.

Regression for the 2026-05-28 daemon wedge: ``sqlite3.connect``'s open() on
~/Library/Messages/chat.db blocked indefinitely (macOS TCC drift) instead of
raising, and since it ran inline in the startup path it wedged the whole
daemon. The adapter now bounds the open with a daemon-thread join timeout.
"""

from __future__ import annotations

import sqlite3
import threading
import time

from pinky_outreach.imessage import iMessageAdapter


def _make_chat_db(path: str) -> None:
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE message (ROWID INTEGER PRIMARY KEY)")
    db.execute("INSERT INTO message DEFAULT VALUES")
    db.commit()
    db.close()


class TestInitDbTimeout:
    def test_blocking_open_times_out_without_wedging(self, tmp_path, monkeypatch):
        """A hung open() must bound out near init_timeout, not block forever."""
        db_path = tmp_path / "chat.db"
        db_path.write_text("")  # exists so the file check passes; connect is patched

        release = threading.Event()

        def _hanging_connect(*_a, **_k):
            release.wait()  # block past init_timeout
            raise sqlite3.OperationalError("released")  # caught in the orphaned thread

        monkeypatch.setattr(
            "pinky_outreach.imessage.sqlite3.connect", _hanging_connect
        )

        start = time.monotonic()
        adapter = iMessageAdapter(db_path=str(db_path), init_timeout=0.2)
        elapsed = time.monotonic() - start

        assert adapter.can_receive is False
        assert adapter._db is None
        assert elapsed < 2.0  # returned ~init_timeout, did NOT block on the open
        release.set()  # let the orphaned opener thread unwind cleanly

    def test_late_success_after_timeout_closes_handle(self, tmp_path, monkeypatch):
        """If the orphaned opener unblocks after timeout, it must close its own
        handle rather than leak it (the adapter never installs it)."""
        db_path = tmp_path / "chat.db"
        db_path.write_text("")

        release = threading.Event()
        closed = threading.Event()

        class _FakeConn:
            def execute(self, *_a, **_k):
                release.wait()  # block past init_timeout, then "succeed"
                return self

            def fetchone(self):
                return [7]

            def close(self):
                closed.set()

        monkeypatch.setattr(
            "pinky_outreach.imessage.sqlite3.connect",
            lambda *_a, **_k: _FakeConn(),
        )

        adapter = iMessageAdapter(db_path=str(db_path), init_timeout=0.2)
        assert adapter.can_receive is False
        assert adapter._db is None

        release.set()  # let the orphaned opener finish the open after timeout
        assert closed.wait(timeout=2.0)  # it must close the handle it opened
        assert adapter._db is None  # and never install it on the adapter

    def test_successful_open_seeds_last_rowid(self, tmp_path):
        db_path = tmp_path / "chat.db"
        _make_chat_db(str(db_path))
        adapter = iMessageAdapter(db_path=str(db_path), init_timeout=5)
        assert adapter.can_receive is True
        assert adapter._last_rowid == 1
        adapter.close()

    def test_operational_error_disables_receive(self, tmp_path, monkeypatch):
        db_path = tmp_path / "chat.db"
        db_path.write_text("")

        def _raising_connect(*_a, **_k):
            raise sqlite3.OperationalError("unable to open database file")

        monkeypatch.setattr(
            "pinky_outreach.imessage.sqlite3.connect", _raising_connect
        )
        adapter = iMessageAdapter(db_path=str(db_path), init_timeout=5)
        assert adapter.can_receive is False
        assert adapter._db is None

    def test_missing_db_file_disables_receive(self, tmp_path):
        adapter = iMessageAdapter(db_path=str(tmp_path / "nope.db"), init_timeout=5)
        assert adapter.can_receive is False
        assert adapter._db is None


def _make_full_chat_db(path: str) -> None:
    """chat.db with enough schema for get_updates' join query."""
    db = sqlite3.connect(path)
    db.execute(
        "CREATE TABLE message ("
        "ROWID INTEGER PRIMARY KEY, text TEXT, date INTEGER, "
        "is_from_me INTEGER DEFAULT 0, date_read INTEGER, handle_id INTEGER)"
    )
    db.execute("CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT)")
    db.execute(
        "CREATE TABLE chat (ROWID INTEGER PRIMARY KEY, "
        "chat_identifier TEXT, display_name TEXT, style INTEGER)"
    )
    db.execute(
        "CREATE TABLE chat_message_join (message_id INTEGER, chat_id INTEGER)"
    )
    # Seed one pre-existing message so _init_db seeds last_rowid=1.
    db.execute(
        "INSERT INTO message (text, date, is_from_me, handle_id) VALUES ('old', 0, 0, 1)"
    )
    db.execute("INSERT INTO handle (id) VALUES ('+15551234567')")
    db.commit()
    db.close()


def _insert_inbound(path: str, text: str) -> None:
    db = sqlite3.connect(path)
    db.execute(
        "INSERT INTO message (text, date, is_from_me, handle_id) VALUES (?, 0, 0, 1)",
        (text,),
    )
    db.commit()
    db.close()


class _FailingConn:
    """Stand-in connection whose queries fail like a locked chat.db."""

    def __init__(self):
        self.closed = False

    def execute(self, *_a, **_k):
        raise sqlite3.OperationalError("database is locked")

    def close(self):
        self.closed = True


class TestGetUpdatesReconnect:
    def test_transient_query_error_does_not_drop_pending_messages(self, tmp_path):
        """A locked chat.db poll must not advance the high-water mark: the
        reconnect used to reseed last_rowid to MAX(ROWID), silently skipping
        every message that arrived but was never returned."""
        db_path = str(tmp_path / "chat.db")
        _make_full_chat_db(db_path)

        adapter = iMessageAdapter(db_path=db_path, init_timeout=5)
        assert adapter.can_receive is True
        assert adapter._last_rowid == 1

        # A new inbound message arrives, then the poll hits a transient error.
        _insert_inbound(db_path, "hello while locked")
        failing = _FailingConn()
        adapter._db = failing

        assert adapter.get_updates() == []
        assert failing.closed is True  # old handle closed, not leaked
        assert adapter.can_receive is True  # reconnected
        assert adapter._last_rowid == 1  # high-water mark preserved

        # The next successful poll replays the missed message.
        messages = adapter.get_updates()
        assert [m.content for m in messages] == ["hello while locked"]
        assert adapter._last_rowid == 2
        adapter.close()

    def test_init_reseed_default_skips_preexisting_rows(self, tmp_path):
        db_path = str(tmp_path / "chat.db")
        _make_full_chat_db(db_path)
        adapter = iMessageAdapter(db_path=db_path, init_timeout=5)
        # Fresh init must not replay history.
        assert adapter.get_updates() == []
        adapter.close()
