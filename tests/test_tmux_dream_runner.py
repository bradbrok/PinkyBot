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

            prompt_content: list[str] = []

            async def write_result():
                # Wait until the instruction was sent, then play the dream agent
                while not fake.named("send-keys"):
                    await asyncio.sleep(0.01)
                instruction = fake.named("send-keys")[0][-1]
                # Snapshot the prompt file mid-run - both files are cleaned
                # up after a successful run.
                prompt_file = next(
                    tok for tok in instruction.split() if "prompt-" in tok
                )
                prompt_content.append(open(prompt_file).read())
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
            assert "DREAM INSTRUCTIONS" in prompt_content[0]
            assert "consolidate the night" in prompt_content[0]
            instruction = fake.named("send-keys")[0][-1]
            assert "consolidate the night" not in instruction
            assert "prompt-" in instruction and "result-" in instruction

            # Successful runs leave no prompt/result artifacts behind -
            # nightly dreams must not grow dreams/ unbounded.
            assert os.listdir(os.path.join(tmp, "dreams")) == []

            # Spawn used the model + bypassPermissions; session torn down
            spawn = fake.named("new-session")[0]
            assert "--model" in spawn and "claude-sonnet-4-6" in spawn
            assert "--permission-mode" in spawn and "bypassPermissions" in spawn
            assert "--allowedTools" in spawn
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

    @pytest.mark.asyncio
    async def test_spawn_sets_remain_on_exit_for_postmortem_capture(self):
        """Without remain-on-exit tmux reaps the session the moment the
        claude process exits, so the death-diagnostic capture-pane would
        report nothing."""
        with tempfile.TemporaryDirectory() as tmp:
            fake = _FakeTmux()
            runner = _runner(tmp, fake, timeout_s=0.2)
            await runner.run("x")
            opts = fake.named("set-option")
            assert opts and "remain-on-exit" in opts[0]

    @pytest.mark.asyncio
    async def test_repl_death_fails_fast_with_pane_diagnostic(self):
        """An early claude exit must fail the run promptly with the pane
        tail (the clue about WHY it died), not burn the full timeout
        polling for a result file that can never appear."""

        class _DeadReplTmux(_FakeTmux):
            async def __call__(self, *args: str) -> tuple[int, str]:
                if args[0] == "display-message":
                    self.calls.append(args)
                    return 0, "1"  # pane_dead=1: process exited
                if args[0] == "capture-pane":
                    self.calls.append(args)
                    return 0, "Welcome to Claude Code\nerror: invalid API key"
                return await super().__call__(*args)

        with tempfile.TemporaryDirectory() as tmp:
            fake = _DeadReplTmux()
            runner = _runner(tmp, fake, timeout_s=30.0)
            import time

            start = time.time()
            result = await runner.run("x")
            elapsed = time.time() - start

            assert result.exit_code == 1
            assert "exited" in result.error
            assert "invalid API key" in result.error
            assert elapsed < 5.0, "death must be detected long before timeout"
            assert fake.named("kill-session")
            # Failure keeps the prompt file for post-mortem.
            dreams = os.listdir(os.path.join(tmp, "dreams"))
            assert any(f.startswith("prompt-") for f in dreams)

    @pytest.mark.asyncio
    async def test_tmux_subprocess_timeout_is_bounded(self, monkeypatch):
        """A hung tmux server must not hang the dream task forever: the
        subprocess is killed and a nonzero rc comes back."""
        procs = []

        class _HungProc:
            returncode = None

            def __init__(self):
                self.killed = False
                procs.append(self)

            async def communicate(self):
                await asyncio.Event().wait()  # never returns

            def kill(self):
                self.killed = True

            async def wait(self):
                return 0

        async def fake_exec(*args, **kwargs):
            return _HungProc()

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
        runner = TmuxDreamRunner(TmuxDreamConfig(), agent_name="ivan")
        rc, out = await runner._tmux("has-session", timeout=0.05)
        assert rc != 0
        assert "timed out" in out
        assert procs and procs[0].killed

    @pytest.mark.asyncio
    async def test_single_probe_timeout_does_not_abort_run(self):
        """One transiently hung liveness probe (rc=124) is indeterminate,
        not death: the run must keep waiting and still pick up the
        result."""

        class _FlakyProbeTmux(_FakeTmux):
            def __init__(self):
                super().__init__()
                self.probes = 0

            async def __call__(self, *args: str) -> tuple[int, str]:
                if args[0] == "display-message":
                    self.calls.append(args)
                    self.probes += 1
                    if self.probes == 1:
                        return 124, "tmux display-message timed out after 5.0s"
                    return 0, "0"  # alive
                return await super().__call__(*args)

        with tempfile.TemporaryDirectory() as tmp:
            fake = _FlakyProbeTmux()
            runner = _runner(tmp, fake, timeout_s=30.0)

            async def write_result():
                while not fake.named("send-keys"):
                    await asyncio.sleep(0.01)
                # Let the poller swallow the rc=124 probe before the
                # result appears.
                while fake.probes < 1:
                    await asyncio.sleep(0.01)
                instruction = fake.named("send-keys")[0][-1]
                result_path = next(
                    tok for tok in instruction.split() if "result-" in tok
                )
                with open(result_path, "w") as f:
                    f.write("REPORT AFTER FLAKY PROBE")

            writer = asyncio.create_task(write_result())
            result = await runner.run("x")
            await writer

            assert result.ok
            assert result.output == "REPORT AFTER FLAKY PROBE"
            assert fake.probes >= 1

    @pytest.mark.asyncio
    async def test_two_consecutive_dead_probes_abort_run(self):
        """A tmux server that stays hung is indistinguishable from a dead
        session; two consecutive failed probes count as death."""

        class _AlwaysDeadProbeTmux(_FakeTmux):
            async def __call__(self, *args: str) -> tuple[int, str]:
                if args[0] == "display-message":
                    self.calls.append(args)
                    return 124, "tmux display-message timed out after 5.0s"
                return await super().__call__(*args)

        with tempfile.TemporaryDirectory() as tmp:
            fake = _AlwaysDeadProbeTmux()
            runner = _runner(tmp, fake, timeout_s=30.0)
            import time

            start = time.time()
            result = await runner.run("x")
            elapsed = time.time() - start

            assert result.exit_code == 1
            assert "exited" in result.error
            assert elapsed < 5.0, "death must be detected long before timeout"
            assert len(fake.named("display-message")) == 2

    @pytest.mark.asyncio
    async def test_zero_byte_result_with_dead_repl_is_detected(self):
        """A REPL that creates an empty result file and then dies must be
        detected promptly: liveness is probed whenever the file isn't
        growing, not only while it is absent."""

        class _DeadAfterEmptyResultTmux(_FakeTmux):
            def __init__(self, dreams_dir: str):
                super().__init__()
                self.dreams_dir = dreams_dir

            async def __call__(self, *args: str) -> tuple[int, str]:
                if args[0] == "display-message":
                    self.calls.append(args)
                    results = [
                        f for f in os.listdir(self.dreams_dir)
                        if f.startswith("result-")
                    ]
                    # Alive until the 0-byte result appears, then dead.
                    return (0, "1") if results else (0, "0")
                return await super().__call__(*args)

        with tempfile.TemporaryDirectory() as tmp:
            fake = _DeadAfterEmptyResultTmux(os.path.join(tmp, "dreams"))
            runner = _runner(tmp, fake, timeout_s=30.0)

            async def touch_empty_result():
                while not fake.named("send-keys"):
                    await asyncio.sleep(0.01)
                instruction = fake.named("send-keys")[0][-1]
                result_path = next(
                    tok for tok in instruction.split() if "result-" in tok
                )
                open(result_path, "w").close()  # 0 bytes, then death

            toucher = asyncio.create_task(touch_empty_result())
            import time

            start = time.time()
            result = await runner.run("x")
            elapsed = time.time() - start
            await toucher

            assert result.exit_code == 1
            assert "exited before writing a result file" in result.error
            assert elapsed < 5.0, "death must be detected long before timeout"

    @pytest.mark.asyncio
    async def test_spawn_passes_project_mcp_config_when_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            mcp_config = os.path.join(tmp, ".mcp.json")
            with open(mcp_config, "w") as f:
                f.write("{}")
            fake = _FakeTmux(new_session_rc=1)
            runner = _runner(tmp, fake)

            await runner.run("x")

            spawn = fake.named("new-session")[0]
            idx = spawn.index("--mcp-config")
            assert spawn[idx + 1] == os.path.realpath(mcp_config)

    @pytest.mark.asyncio
    async def test_spawn_prefers_explicit_per_run_mcp_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            project_config = os.path.join(tmp, ".mcp.json")
            per_run_config = os.path.join(tmp, "dream-mcp.json")
            for path in (project_config, per_run_config):
                with open(path, "w") as file:
                    file.write("{}")
            fake = _FakeTmux(new_session_rc=1)
            runner = _runner(tmp, fake, mcp_config=per_run_config)

            await runner.run("x")

            spawn = fake.named("new-session")[0]
            idx = spawn.index("--mcp-config")
            assert spawn[idx + 1] == os.path.realpath(per_run_config)

    @pytest.mark.asyncio
    async def test_spawn_omits_project_mcp_config_when_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = _FakeTmux(new_session_rc=1)
            runner = _runner(tmp, fake)

            await runner.run("x")

            assert "--mcp-config" not in fake.named("new-session")[0]

    @pytest.mark.asyncio
    async def test_allowlist_is_the_primary_tool_boundary(self):
        """#708 review (Murzik): the dream prompt embeds raw conversation
        history, so the spawn must pin an explicit --allowedTools allowlist
        (SDK-path parity + Read/Write), not rely on broad defaults trimmed
        by a denylist.
        """
        with tempfile.TemporaryDirectory() as tmp:
            fake = _FakeTmux(new_session_rc=1)  # fail fast after spawn
            runner = _runner(tmp, fake)
            await runner.run("x")
            spawn = fake.named("new-session")[0]
            idx = spawn.index("--allowedTools")
            allowed = spawn[idx + 1].split(",")
            assert allowed == [
                "Read",
                "Write",
                "ToolSearch",
                "mcp__pinky-memory__recall",
                "mcp__pinky-memory__reflect",
            ]

    @pytest.mark.asyncio
    async def test_dream_runner_passes_sdk_parity_allowlist(self, monkeypatch):
        """The dream_runner tmux branch derives its allowlist from
        _DREAM_ALLOWED_TOOLS (single source of truth) + Read/Write.
        """
        from pinky_daemon import dream_runner as dr_mod

        captured = {}
        orig_init = dr_mod.TmuxDreamRunner.__init__

        def spy_init(self, config=None, *, agent_name=""):
            captured["allowed"] = list(config.allowed_tools)
            orig_init(self, config, agent_name=agent_name)

        monkeypatch.setattr(dr_mod.TmuxDreamRunner, "__init__", spy_init)
        monkeypatch.setenv("PINKY_DREAM_TRANSPORT", "tmux")

        class _Agent:
            dream_enabled = True
            working_dir = ""
            dream_model = ""
            model = "claude-sonnet-4-6"

        with tempfile.TemporaryDirectory() as tmp:
            dr = DreamRunner(
                db_path=os.path.join(tmp, "dream_state.db"),
                history_provider=lambda *a, **k: [
                    {"timestamp": 1.0, "role": "user", "content": "hi", "channel": "t"}
                ],
            )

            async def fake_run(self, prompt, **kw):
                from pinky_daemon.claude_runner import RunResult
                return RunResult(output="dream report", exit_code=0)

            monkeypatch.setattr(dr_mod.TmuxDreamRunner, "run", fake_run)
            await dr.run_dream("ivan", _Agent())

        assert captured["allowed"] == ["Read", "Write", *dr_mod._DREAM_ALLOWED_TOOLS]


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
