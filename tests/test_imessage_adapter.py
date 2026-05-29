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
