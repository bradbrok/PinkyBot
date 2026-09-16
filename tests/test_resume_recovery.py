"""Operation budgets and bounded diagnostic retention."""
import asyncio
import time

import pytest

from pinky_daemon.resume_recovery import RecoveryOperation, ResumeEvidence, drain_diagnostic


def test_one_budget_survives_attempt_generation(monkeypatch):
    monkeypatch.setenv("PINKY_RESUME_FAILSAFE", "1")
    operation = RecoveryOperation()
    evidence = ResumeEvidence("claude_sdk", "initialize", "missing_conversation", 0)
    assert operation.claim(evidence)
    assert not operation.claim(evidence)
    assert not operation.claim(ResumeEvidence("claude_sdk", "initialize", "missing_conversation", 1))
    assert RecoveryOperation().claim(evidence)


@pytest.mark.parametrize("deadline,generation", [(0, 0), (600, 1)])
def test_expired_or_retired_evidence_cannot_claim(monkeypatch, deadline, generation):
    monkeypatch.setenv("PINKY_RESUME_FAILSAFE", "1")
    operation = RecoveryOperation(deadline=time.monotonic() + deadline)
    assert not operation.claim(ResumeEvidence("claude_sdk", "initialize", "missing_conversation", generation))
    assert not operation.fresh_used


async def test_diagnostics_drain_all_bytes_but_retain_bounded_redacted_text():
    stream = asyncio.StreamReader()
    stream.feed_data(b"Authorization: Bearer private-token\napi_key=private-key\n" + b"x" * 20000)
    stream.feed_eof()
    diagnostic = await drain_diagnostic(stream)
    assert stream.at_eof()
    assert len(diagnostic) <= 1024
    assert "private-token" not in diagnostic
    assert "private-key" not in diagnostic
    assert "[redacted]" in diagnostic


def test_possible_execution_evidence_cannot_claim(monkeypatch):
    monkeypatch.setenv("PINKY_RESUME_FAILSAFE", "1")
    operation = RecoveryOperation()
    assert not operation.claim(ResumeEvidence(
        "codex_app_server", "thread/resume", "missing_rollout", 0, execution="possible",
    ))
    assert not operation.fresh_used
