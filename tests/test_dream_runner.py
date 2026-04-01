"""Tests for dream transcript loading and watermarking."""

from __future__ import annotations

import os
import tempfile
from datetime import datetime

from pinky_daemon.dream_runner import DreamRunner


class TestDreamRunner:
    def test_fetch_unprocessed_history_filters_and_orders_messages(self):
        messages = [
            {"timestamp": 300.0, "role": "assistant", "content": "third"},
            {"timestamp": 100.0, "role": "user", "content": "first"},
            {"timestamp": 200.0, "role": "user", "content": "second"},
        ]

        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            runner = DreamRunner(
                db_path=path,
                history_provider=lambda agent_name, after_ts, limit, role: messages,
            )

            lines, watermark = runner._fetch_unprocessed_history("pinky", after_ts=150.0)

            expected = [
                f"[{datetime.fromtimestamp(200.0).strftime('%Y-%m-%d %H:%M')}] [user] second",
                f"[{datetime.fromtimestamp(300.0).strftime('%Y-%m-%d %H:%M')}] [assistant] third",
            ]
            assert lines == [
                expected[0],
                expected[1],
            ]
            assert watermark == 300.0
        finally:
            os.unlink(path)
