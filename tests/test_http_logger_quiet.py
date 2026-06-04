"""httpx/httpcore loggers must be silenced so bot tokens never reach the log.

httpx logs every request line at INFO, including the full URL — and the
Telegram/Discord poll URLs embed the bot token in the path. With daemon
stdout redirected to logs/api.log, that leaked live tokens in cleartext on
every poll. ``_quiet_http_client_loggers`` raises both loggers to WARNING.
"""

import logging

import pytest

from pinky_daemon.__main__ import _quiet_http_client_loggers


@pytest.fixture(autouse=True)
def _restore_logger_levels():
    """Loggers are process-global singletons — save/restore around each test."""
    saved = {name: logging.getLogger(name).level for name in ("httpx", "httpcore")}
    try:
        yield
    finally:
        for name, level in saved.items():
            logging.getLogger(name).setLevel(level)


def test_sets_both_loggers_to_warning():
    # Simulate the noisy default that leaked tokens.
    logging.getLogger("httpx").setLevel(logging.INFO)
    logging.getLogger("httpcore").setLevel(logging.DEBUG)

    _quiet_http_client_loggers()

    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger("httpcore").level == logging.WARNING


def test_info_request_lines_are_filtered():
    _quiet_http_client_loggers()

    # The leaking record was an INFO "HTTP Request: GET https://.../bot<token>/..."
    # — it must no longer be emitted; WARNING/ERROR still are.
    httpx_logger = logging.getLogger("httpx")
    assert not httpx_logger.isEnabledFor(logging.INFO)
    assert httpx_logger.isEnabledFor(logging.WARNING)
    assert logging.getLogger("httpcore").isEnabledFor(logging.WARNING)
