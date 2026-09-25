"""message_text() selects text blocks by type (thinking may come first).

Uses plain fakes: the anthropic SDK is an optional extra and isn't installed in CI.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import pinky_daemon.voice_engine as voice_engine
from pinky_daemon.anthropic_text import message_text
from pinky_daemon.migration import mapper


def _thinking() -> SimpleNamespace:
    return SimpleNamespace(type="thinking", thinking="", signature="sig")


def _text(text: str) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=text)


def _response(*blocks, stop_reason: str = "end_turn") -> SimpleNamespace:
    return SimpleNamespace(content=list(blocks), stop_reason=stop_reason)


def test_thinking_block_first_then_text():
    assert message_text(_response(_thinking(), _text('{"ok": true}'))) == '{"ok": true}'


def test_joins_all_text_blocks_skipping_others():
    resp = _response(
        _text("a"), SimpleNamespace(type="tool_use", id="t1", name="x", input={}), _text("b")
    )
    assert message_text(resp) == "ab"


@pytest.mark.parametrize(
    "resp",
    [
        _response(_thinking()),
        _response(),
        _response(_thinking(), stop_reason="max_tokens"),
        SimpleNamespace(content=None),
    ],
)
def test_no_text_raises(resp):
    with pytest.raises(ValueError, match="no text block"):
        message_text(resp)


def _fake_async_client(resp):
    async def create(**kwargs):
        return resp

    return SimpleNamespace(messages=SimpleNamespace(create=create))


async def test_voice_outcome_reads_text_after_thinking(monkeypatch):
    resp = _response(_thinking(), _text('{"success": true, "summary": "booked"}'))
    monkeypatch.setattr(voice_engine, "_get_anthropic_client", lambda key="": _fake_async_client(resp))
    outcome = await voice_engine.extract_outcome_with_opus(None, [], "book a table")
    assert outcome == {"success": True, "summary": "booked"}


async def test_voice_outcome_no_text_is_failure_not_empty_success(monkeypatch):
    resp = _response(_thinking(), stop_reason="max_tokens")
    monkeypatch.setattr(voice_engine, "_get_anthropic_client", lambda key="": _fake_async_client(resp))
    outcome = await voice_engine.extract_outcome_with_opus(None, [], "book a table")
    assert outcome["success"] is False
    assert "no text block" in outcome["notes"]


def test_mapper_call_claude_reads_text_after_thinking(monkeypatch):
    resp = _response(_thinking(), _text("[]"))
    client = SimpleNamespace(messages=SimpleNamespace(create=lambda **kwargs: resp))
    monkeypatch.setattr(mapper, "_get_anthropic_client", lambda: client)
    assert mapper._call_claude("sys", "user") == "[]"
