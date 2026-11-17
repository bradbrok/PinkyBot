"""#1521: a system prompt too big for one argv string is passed as a file.

Linux caps a single argv string at 128 KiB (MAX_ARG_STRLEN). The SDK passes a
"custom" system prompt to the CLI as one argument, so a prompt past that limit
made every spawn fail with E2BIG ("Argument list too long") and the agent
could not start. Past SYSTEM_PROMPT_ARG_MAX_BYTES the session now writes the
prompt to a private file and passes --system-prompt-file; below it nothing
changes (custom prompt, snapshot disabled). The user profile injected in the
prompt is capped too.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from pinky_daemon import streaming_session as SS
from pinky_daemon.streaming_session import StreamingSession, StreamingSessionConfig
from pinky_daemon.user_profile_store import (
    PROFILE_PROMPT_MAX_BYTES,
    ProfileEntry,
    UserProfileStore,
)


async def _options(tmp_path, monkeypatch, prompt):
    client = SimpleNamespace(connect=AsyncMock(side_effect=RuntimeError("captured")),
                             disconnect=AsyncMock())
    factory = MagicMock(return_value=client)
    monkeypatch.setattr("claude_agent_sdk.ClaudeSDKClient", factory)
    session = StreamingSession(StreamingSessionConfig(
        agent_name="sample", working_dir=str(tmp_path), system_prompt=prompt,
    ))
    with pytest.raises(RuntimeError):
        await session.connect()
    return factory.call_args.args[0]


async def test_a_small_prompt_stays_a_custom_prompt_with_snapshot_off(tmp_path, monkeypatch):
    prompt = "x" * SS.SYSTEM_PROMPT_ARG_MAX_BYTES
    options = await _options(tmp_path, monkeypatch, prompt)
    assert options.system_prompt == {"type": "custom", "prompt": prompt, "snapshot": False}
    assert not (tmp_path / ".pinky" / SS.SYSTEM_PROMPT_FILENAME).exists()


async def test_a_prompt_over_the_margin_goes_to_a_private_file(tmp_path, monkeypatch):
    prompt = "è" * (SS.SYSTEM_PROMPT_ARG_MAX_BYTES // 2 + 1)  # 2 bytes each: over the margin
    options = await _options(tmp_path, monkeypatch, prompt)
    f = tmp_path / ".pinky" / SS.SYSTEM_PROMPT_FILENAME
    assert options.system_prompt == {"type": "file", "path": str(f.resolve())}
    assert f.read_text(encoding="utf-8") == prompt
    assert f.stat().st_mode & 0o777 == 0o600
    assert (tmp_path / ".pinky").stat().st_mode & 0o777 == 0o700
    # the next start rewrites it with the prompt of that start
    nuovo = "é" * (SS.SYSTEM_PROMPT_ARG_MAX_BYTES // 2 + 7)
    await _options(tmp_path, monkeypatch, nuovo)
    assert f.read_text(encoding="utf-8") == nuovo


def test_the_cli_command_carries_the_file_not_the_text(tmp_path):
    from claude_agent_sdk import ClaudeAgentOptions
    from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport

    prompt = "y" * 200_000
    path = SS.write_system_prompt_file(str(tmp_path), prompt)
    t = SubprocessCLITransport(
        prompt="", options=ClaudeAgentOptions(system_prompt={"type": "file", "path": str(path)},
                                              cli_path="/bin/true"),
    )
    cmd = t._build_command()
    assert "--system-prompt-file" in cmd and str(path) in cmd
    assert max(len(a.encode()) for a in cmd) < 131072


def _store_with(tmp_path, n, value_len=200):
    store = UserProfileStore(db_path=str(tmp_path / "p.db"))
    for k in range(n):
        store.upsert(ProfileEntry(chat_id="u1", category="preferences", key=f"k{k:04d}",
                                  value="v" * value_len, confidence=0.5 + (k % 5) / 10,
                                  source="manual"))
    store.set_visibility("agent", "u1", True)
    return store


def test_a_small_profile_is_shown_whole(tmp_path):
    store = _store_with(tmp_path, 5)
    text = store.format_profile_for_prompt("agent", "u1", "Utente")
    assert text.count("\n- k") == 5 and "not shown" not in text


def test_a_big_profile_is_capped_keeping_the_most_confident_entries(tmp_path):
    store = _store_with(tmp_path, 400)  # ~90 KB uncapped
    text = store.format_profile_for_prompt("agent", "u1", "Utente")
    assert len(text.encode()) < PROFILE_PROMPT_MAX_BYTES + 1000
    shown = text.count("\n- k")
    assert 0 < shown < 400 and f"({400 - shown} more entries about Utente not shown" in text
    # the kept ones are the most confident (160 entries have 0.8-0.9, more than fit)
    assert text.count("(high confidence)") == shown
    uncapped = store.format_profile_for_prompt("agent", "u1", "Utente", max_bytes=10**9)
    assert uncapped.count("\n- k") == 400 and "not shown" not in uncapped
