"""Submission ownership and lossless certificates are separate requirements."""

from unittest.mock import MagicMock

import pytest

from tests.test_codex_scheduler_idle_receipt import _append, _paste, _read
from tests.test_codex_scheduler_idle_receipt import harness as harness


def _event(kind, raw_id):
    return (b'{"type":"event_msg","payload":{"type":"' + kind
            + b'","turn_id":"' + raw_id + b'","last_agent_message":"done"}}\n')


@pytest.mark.asyncio
@pytest.mark.parametrize("with_user_message", [False, True])
async def test_other_task_has_no_positive_submission_ownership(harness, with_user_message):
    accepted = MagicMock(return_value=True)
    turn, receipt = await _paste(harness, on_accept=accepted)
    _append(harness, {"type": "task_started", "turn_id": "unrelated"})
    if with_user_message:
        _append(harness, {"type": "user_message", "message": "different unrelated prompt"})
    _append(harness, {"type": "task_complete", "turn_id": "unrelated",
                      "last_agent_message": "done"})
    await _read(harness)
    assert not receipt.done()
    assert not turn.transport_accepted
    accepted.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("start_id,close_id,intervening,valid", [
    (b"x\xff", b"x\xfe", b"", False),
    (b"x\xff", "x\ufffd".encode(), b"", False),
    (b"x\xff", b"x\\ufffd", b"", False),
    (b"valid", b"valid", b'{"type":"event_msg","payload":{"type":"agent_message",'
     b'"message":"\xff"}}\n', False),
    ("x\ufffd".encode(), "x\ufffd".encode(), b"", True),
    (b"x\\u03bb", "x\u03bb".encode(), b"", True),
])
async def test_certificate_utf8_validity_is_independent_of_receipt_ownership(
    harness, start_id, close_id, intervening, valid,
):
    """A receipt veto must not hide a forged certificate from the decoder test."""
    await _paste(harness)
    tailer = harness.session._tailer
    original = tailer._on_turn_complete
    certificates = []

    async def capture(response):
        certificates.append(tailer.completion)
        await original(response)

    tailer._on_turn_complete = capture
    with harness.rollout.open("ab") as stream:
        stream.write(_event(b"task_started", start_id))
        stream.write(intervening)
        stream.write(_event(b"task_complete", close_id))
    await _read(harness)
    if valid:
        assert len(certificates) == 1 and certificates[0] is not None
        assert certificates[0].turn_id == "x" + ("\ufffd" if b"\\u" not in start_id else "\u03bb")
    else:
        assert all(certificate is None for certificate in certificates)
        assert tailer.stats["parse_errors"] > 0
