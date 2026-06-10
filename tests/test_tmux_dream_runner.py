"""Tests for the tmux-based dream runner (#707)."""

from __future__ import annotations

import asyncio
import os
import tempfile

import pytest

from pinky_daemon.dream_runner import DreamRunner
from pinky_daemon.tmux_dream_runner import TmuxDreamConfig, TmuxDreamRunner


class _FakeTmux:
    """Records tmux invocations; pane is 'ready' from the first capture."""

    def __init__(self, *, new_session_rc: int = 0):
        self.calls: list[tuple[str, ...]] = []
        self._new_session_rc = new_session_rc

    async def __call__(self, *args: str) -> tuple[int, str]:
        self.calls.append(args)
        if args[0] == "new-session":
            return self._new_session_rc, "" if self._new_session_rc == 0 else "no server"
        if args[0] == "capture-pane":
            return 0, "❯ try 'help'  ? for shortcuts"
        return 0, ""

    def named(self, cmd: str) -> list[tuple[str, ...]]:
        return [c for c in self.calls if c[0] == cmd]


def _runner(tmp: str, fake: _FakeTmux, **overrides) -> TmuxDreamRunner:
    cfg = TmuxDreamConfig(
        working_dir=tmp,
        model="claude-sonnet-4-6",
        system_prompt="DREAM INSTRUCTIONS",
        timeout_s=overrides.pop("timeout_s", 5.0),
        poll_interval_s=0.05,
        ready_timeout_s=2.0,
        submit_check_delay_s=0.01,
        **overrides,
    )
    runner = TmuxDreamRunner(cfg, agent_name="ivan")
    runner._tmux = fake  # type: ignore[method-assign]

    seeded: list[str] = []
    runner._seeded_dirs = seeded  # type: ignore[attr-defined]

    def _fake_seed(project_dir: str) -> bool:
        seeded.append(project_dir)
        return False

    runner._seed_trust = _fake_seed  # type: ignore[method-assign]
    return runner


class TestTmuxDreamRunner:
    @pytest.mark.asyncio
    async def test_happy_path_prompt_file_instruction_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = _FakeTmux()
            runner = _runner(tmp, fake)

            async def write_result():
                # Wait until the instruction was sent, then play the dream agent
                while not fake.named("send-keys"):
                    await asyncio.sleep(0.01)
                instruction = fake.named("send-keys")[0][-1]
                result_path = next(
                    tok for tok in instruction.split() if "result-" in tok
                )
                with open(result_path, "w") as f:
                    f.write("FINAL DREAM REPORT")

            writer = asyncio.create_task(write_result())
            result = await runner.run("consolidate the night")
            await writer

            assert result.ok
            assert result.output == "FINAL DREAM REPORT"

            # Prompt file holds system prompt + prompt; REPL never saw them
            dreams = os.listdir(os.path.join(tmp, "dreams"))
            prompt_file = next(f for f in dreams if f.startswith("prompt-"))
            content = open(os.path.join(tmp, "dreams", prompt_file)).read()
            assert "DREAM INSTRUCTIONS" in content
            assert "consolidate the night" in content
            instruction = fake.named("send-keys")[0][-1]
            assert "consolidate the night" not in instruction
            assert "prompt-" in instruction and "result-" in instruction

            # Spawn used the model + bypassPermissions; session torn down
            spawn = fake.named("new-session")[0]
            assert "--model" in spawn and "claude-sonnet-4-6" in spawn
            assert "--permission-mode" in spawn and "bypassPermissions" in spawn
            assert "--disallowedTools" in spawn
            assert fake.named("kill-session")

            # First-run trust gates were pre-seeded for the working dir
            assert runner._seeded_dirs == [str(os.path.realpath(tmp))]

    @pytest.mark.asyncio
    async def test_stale_session_killed_before_spawn(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = _FakeTmux(new_session_rc=1)
            runner = _runner(tmp, fake)
            await runner.run("x")
            assert fake.calls[0][0] == "kill-session"
            assert fake.calls[1][0] == "new-session"

    @pytest.mark.asyncio
    async def test_new_session_failure_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = _FakeTmux(new_session_rc=1)
            runner = _runner(tmp, fake)
            result = await runner.run("x")
            assert result.exit_code == 1
            assert "new-session failed" in result.error

    @pytest.mark.asyncio
    async def test_timeout_kills_session_and_reports(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = _FakeTmux()
            runner = _runner(tmp, fake, timeout_s=0.2)
            result = await runner.run("x")
            assert result.exit_code == 1
            assert "timed out" in result.error
            assert fake.named("kill-session")

    def test_session_name_is_distinct_from_main_rails(self):
        runner = TmuxDreamRunner(TmuxDreamConfig(), agent_name="ivan")
        assert runner.session_name == "pinky-dream-ivan"


class TestDreamTransportResolution:
    def _dr(self, setting_provider=None) -> DreamRunner:
        import tempfile as tf

        return DreamRunner(
            db_path=os.path.join(tf.mkdtemp(), "dream_state.db"),
            setting_provider=setting_provider,
        )

    def test_default_is_sdk(self, monkeypatch):
        monkeypatch.delenv("PINKY_DREAM_TRANSPORT", raising=False)
        assert self._dr()._resolve_dream_transport() == "sdk"

    def test_setting_provider_wins(self, monkeypatch):
        monkeypatch.delenv("PINKY_DREAM_TRANSPORT", raising=False)
        dr = self._dr(setting_provider=lambda k: "tmux" if k == "PINKY_DREAM_TRANSPORT" else "")
        assert dr._resolve_dream_transport() == "tmux"

    def test_env_fallback(self, monkeypatch):
        monkeypatch.setenv("PINKY_DREAM_TRANSPORT", "tmux")
        assert self._dr()._resolve_dream_transport() == "tmux"

    def test_unknown_value_falls_back_to_sdk(self, monkeypatch):
        monkeypatch.setenv("PINKY_DREAM_TRANSPORT", "carrier-pigeon")
        assert self._dr()._resolve_dream_transport() == "sdk"

    def test_setting_provider_exception_is_contained(self, monkeypatch):
        monkeypatch.delenv("PINKY_DREAM_TRANSPORT", raising=False)

        def boom(key):
            raise RuntimeError("db locked")

        assert self._dr(setting_provider=boom)._resolve_dream_transport() == "sdk"
