"""Tmux-based dream runner — one-shot dream sessions on interactive billing (#707).

Programmatic Claude (SDK / ``-p`` print mode) bills as API usage starting
June 15, 2026, while interactive sessions draw from the Max account. Dreams
are one-shot batch jobs with ~200K-char prompts, so this runner drives a
detached tmux session running the system ``claude`` binary interactively and
passes the work through FILES: the full prompt is written to a prompt file,
the REPL receives only a one-line instruction, and the session delivers its
report by writing a result file the runner polls for. The 200K prompt never
touches the paste path (readiness-gate paste is the flaky part of the tmux
rails — see #707).

The session runs in the agent's working dir so the agent's ``.mcp.json``
(shared MCP gateway + per-agent bearer key) applies and reflect() calls
attribute to the right agent, same as the SDK path it replaces.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from pinky_daemon.claude_config import (
    migrate_claude_project_history,
    resolve_agent_claude_config_dir,
    resolve_claude_config_path,
    resolve_claude_settings_path,
    seed_claude_settings_file,
    seed_claude_trust_file,
)
from pinky_daemon.claude_runner import RunResult

_DEFAULT_CLAUDE_BIN = "/opt/homebrew/bin/claude"  # cc_autoupdate.sh manages this path


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


class _ReplExitedError(Exception):
    """The dream REPL process exited before writing a result file."""


@dataclass
class TmuxDreamConfig:
    """Configuration for the tmux-based dream runner."""

    # Working directory (the agent's dir — CLAUDE.md and .mcp.json live here)
    working_dir: str = "."

    # Model selection (CLI --model). None/empty = binary default.
    model: str | None = None

    # Claude binary. Empty = $PINKY_CLAUDE_BIN, then which(claude), then
    # the cc_autoupdate-managed homebrew path.
    claude_binary: str = ""

    # System prompt — prepended to the prompt file (interactive CC has no
    # per-session system-prompt override; the dream prompt is self-contained
    # instructions, so file-level delivery is equivalent in practice).
    system_prompt: str = ""

    # PRIMARY tool boundary (Murzik, #708 review): the dream prompt embeds raw
    # conversation history, so an injection there must not reach a broad
    # interactive tool surface. Mirror the SDK path's allowlist semantics
    # (--allowedTools + bypassPermissions), widened only by Read (prompt file)
    # and Write (result file) which the file-passing protocol requires.
    allowed_tools: list[str] = field(default_factory=lambda: [
        "Read",
        "Write",
        "ToolSearch",
        "mcp__pinky-memory__recall",
        "mcp__pinky-memory__reflect",
    ])

    # Belt-and-suspenders denylist on top of the allowlist boundary.
    disallowed_tools: list[str] = field(default_factory=lambda: [
        "Bash",
        "mcp__pinky-messaging__*",
        "mcp__pinky-outreach__*",
        "mcp__pinky-self__*",
    ])

    # Hard cap on the whole run (spawn -> result file). Dreams currently run
    # ~15 min; leave generous headroom before declaring the night lost.
    timeout_s: float = 3600.0

    # How often to poll for the result file.
    poll_interval_s: float = 5.0

    # How long to wait for the REPL to draw before sending the instruction.
    ready_timeout_s: float = 60.0

    # Delay before checking the instruction actually submitted (re-Enter guard).
    submit_check_delay_s: float = 5.0

    # Registry Agent row/config object. Used only for shared Claude config-dir
    # derivation; optional for direct tests and legacy callers.
    agent_config: object = None


class TmuxDreamRunner:
    """Runs a one-shot dream in a detached tmux session.

    Mirrors SDKRunner's ``run(prompt) -> RunResult`` surface so
    dream_runner can swap transports behind a flag.
    """

    def __init__(self, config: TmuxDreamConfig | None = None, *, agent_name: str = "") -> None:
        self._config = config or TmuxDreamConfig()
        self._agent_name = agent_name or "agent"

    @property
    def session_name(self) -> str:
        return f"pinky-dream-{self._agent_name}"

    # ── tmux plumbing ─────────────────────────────────────────

    async def _tmux(self, *args: str, timeout: float = 5.0) -> tuple[int, str]:
        """Run a tmux command; returns (returncode, combined output).

        ``timeout`` defends against a hung tmux server (mirrors the
        rails' ``_TmuxControl._run``). On expiry the subprocess is
        killed and a nonzero rc is returned so callers handle it like
        any other tmux failure instead of hanging the dream task
        forever (which would also wedge the #704 overlap guard for
        every subsequent nightly fire).
        """
        proc = await asyncio.create_subprocess_exec(
            "tmux", *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass  # process exited on its own between timeout and kill
            await proc.wait()
            return 124, f"tmux {args[0] if args else ''} timed out after {timeout}s"
        return proc.returncode or 0, out.decode(errors="replace")

    def _resolve_binary(self) -> str:
        if self._config.claude_binary:
            return self._config.claude_binary
        env_bin = os.environ.get("PINKY_CLAUDE_BIN", "").strip()
        if env_bin:
            return env_bin
        return shutil.which("claude") or _DEFAULT_CLAUDE_BIN

    def _per_agent_claude_config_dir(self) -> Path | None:
        return resolve_agent_claude_config_dir(
            self._config.agent_config,
            working_dir=self._config.working_dir,
        )

    def _per_agent_claude_env(self) -> dict[str, str]:
        cfg_dir = self._per_agent_claude_config_dir()
        if cfg_dir is None:
            return {}
        env = {"CLAUDE_CONFIG_DIR": str(cfg_dir)}
        agent = self._config.agent_config
        if not (
            getattr(agent, "provider_url", "") or getattr(agent, "provider_key", "")
        ):
            token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "").strip()
            if token:
                env["CLAUDE_CODE_OAUTH_TOKEN"] = token
        return env

    def _prepare_claude_config(self, project_dir: Path) -> None:
        cfg_dir = self._per_agent_claude_config_dir()
        if cfg_dir is None:
            return
        if migrate_claude_project_history(project_dir, cfg_dir):
            _log(f"tmux-dream: migrated claude transcript history into {cfg_dir}")
        env = {"CLAUDE_CONFIG_DIR": str(cfg_dir)}
        seed_claude_settings_file(resolve_claude_settings_path(env))

    # ── run ───────────────────────────────────────────────────

    async def run(self, prompt: str, *, system_prompt: str = "") -> RunResult:
        start = time.time()
        work_dir = Path(self._config.working_dir).resolve()
        dreams_dir = work_dir / "dreams"
        dreams_dir.mkdir(parents=True, exist_ok=True)

        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        prompt_path = dreams_dir / f"prompt-{stamp}.md"
        result_path = dreams_dir / f"result-{stamp}.md"

        sys_prompt = system_prompt or self._config.system_prompt
        body = f"{sys_prompt}\n\n---\n\n{prompt}" if sys_prompt else prompt
        prompt_path.write_text(body, encoding="utf-8")
        if result_path.exists():  # paranoia: never read a stale result
            result_path.unlink()

        # Pre-seed trust/bypass acceptance for this dir (#112 mechanism) —
        # best-effort; agent dirs are normally already trusted by their main
        # session, but a first dream on a fresh box must not wedge.
        try:
            self._prepare_claude_config(work_dir)
            if self._seed_trust(str(work_dir)):
                _log(f"tmux-dream: seeded claude trust for {work_dir}")
        except Exception as e:
            _log(f"tmux-dream: trust seed failed (continuing): {e}")

        # Defensive: a stale session from a crashed run would shadow the new
        # one (the #704 overlap guard prevents concurrent fires, not leftovers).
        await self._tmux("kill-session", "-t", self.session_name)

        cmd = [self._resolve_binary()]
        if self._config.model:
            cmd += ["--model", self._config.model]
        cmd += ["--permission-mode", "bypassPermissions"]
        if self._config.allowed_tools:
            cmd += ["--allowedTools", ",".join(self._config.allowed_tools)]
        if self._config.disallowed_tools:
            cmd += ["--disallowedTools", ",".join(self._config.disallowed_tools)]

        new_session_args = [
            "new-session", "-d", "-s", self.session_name, "-c", str(work_dir),
        ]
        for key, value in self._per_agent_claude_env().items():
            new_session_args.extend(["-e", f"{key}={value}"])
        rc, out = await self._tmux(*new_session_args, *cmd)
        if rc != 0:
            return RunResult(
                output="",
                exit_code=1,
                error=f"tmux new-session failed: {out.strip()}",
                duration_ms=int((time.time() - start) * 1000),
            )

        _log(
            f"tmux-dream: spawned {self.session_name} "
            f"(model={self._config.model or 'default'}, prompt={prompt_path.name})"
        )

        # Keep the pane around when the claude process exits (crash, bad
        # --model, expired auth) - without this tmux reaps the session on
        # command exit, so the post-mortem capture-pane below would lose
        # the one clue about WHY it died. Best-effort.
        await self._tmux(
            "set-option", "-w", "-t", self.session_name, "remain-on-exit", "on"
        )

        success = False
        try:
            if not await self._wait_ready():
                # Proceed anyway: the pty buffers typed input, and CC reads it
                # once the REPL is up. Logged so a hung binary is diagnosable.
                _log(f"tmux-dream: {self.session_name} REPL not visibly ready "
                     f"after {self._config.ready_timeout_s}s — sending anyway")

            instruction = (
                f"Read the file {prompt_path} and follow ALL instructions in it exactly. "
                f"When the work is fully complete, write your final report as plain text "
                f"to {result_path} using the Write tool. Do not create that file until "
                f"you are done — it is the completion signal."
            )
            rc, out = await self._tmux(
                "send-keys", "-t", self.session_name, "-l", instruction
            )
            if rc != 0:
                return RunResult(
                    output="",
                    exit_code=1,
                    error=f"tmux send-keys failed: {out.strip()}",
                    duration_ms=int((time.time() - start) * 1000),
                )
            await self._tmux("send-keys", "-t", self.session_name, "Enter")
            await self._ensure_submitted(instruction)

            output = await self._wait_for_result(result_path, deadline=start + self._config.timeout_s)
            elapsed_ms = int((time.time() - start) * 1000)
            _log(f"tmux-dream: {self.session_name} done in {elapsed_ms}ms, "
                 f"report={len(output)} chars")
            success = True
            return RunResult(output=output.strip(), exit_code=0, duration_ms=elapsed_ms)

        except _ReplExitedError as e:
            return RunResult(
                output="",
                exit_code=1,
                error=str(e),
                duration_ms=int((time.time() - start) * 1000),
            )
        except TimeoutError:
            _, pane = await self._tmux("capture-pane", "-p", "-t", self.session_name)
            tail = pane.strip()[-500:]
            return RunResult(
                output="",
                exit_code=1,
                error=(
                    f"dream tmux run timed out after {int(self._config.timeout_s)}s "
                    f"without a result file; pane tail: {tail}"
                ),
                duration_ms=int((time.time() - start) * 1000),
            )
        finally:
            await self._tmux("kill-session", "-t", self.session_name)
            # The report is persisted to the dream DB by the caller; the
            # prompt file carries raw conversation history. Delete both on
            # success so nightly dreams don't grow dreams/ unbounded; keep
            # them on failure for post-mortem.
            if success:
                for p in (prompt_path, result_path):
                    try:
                        p.unlink(missing_ok=True)
                    except OSError as e:
                        _log(f"tmux-dream: cleanup of {p} failed (ignored): {e}")

    def _seed_trust(self, project_dir: str) -> bool:
        """Seed first-run trust flags; seam for tests."""
        env = {**os.environ, **self._per_agent_claude_env()}
        return seed_claude_trust_file(resolve_claude_config_path(env), project_dir)

    # ── waiting ───────────────────────────────────────────────

    async def _ensure_submitted(self, instruction: str) -> None:
        """Re-send Enter once if the instruction is still parked in the input box.

        Paste-without-submit is a known tmux-rails failure mode; the dream
        turn is long, so a single cheap recheck beats losing the whole night.
        """
        await asyncio.sleep(self._config.submit_check_delay_s)
        rc, pane = await self._tmux("capture-pane", "-p", "-t", self.session_name)
        if rc != 0:
            return
        marker = instruction[:40]
        if marker in pane and "esc to interrupt" not in pane:
            _log(f"tmux-dream: {self.session_name} instruction not submitted — re-sending Enter")
            await self._tmux("send-keys", "-t", self.session_name, "Enter")

    async def _wait_ready(self) -> bool:
        """Poll the pane until the Claude REPL has drawn its input UI."""
        deadline = time.time() + self._config.ready_timeout_s
        while time.time() < deadline:
            rc, pane = await self._tmux("capture-pane", "-p", "-t", self.session_name)
            if rc == 0 and ("shortcuts" in pane or "❯" in pane or "Claude" in pane):
                return True
            await asyncio.sleep(1.0)
        return False

    async def _repl_alive(self) -> bool | None:
        """Tri-state liveness probe for the claude process in the dream pane.

        Returns True while the process is running and False on definitive
        death evidence: ``pane_dead=1`` (a dead command under
        ``remain-on-exit on``) or the session/server being gone entirely.
        Returns None when the probe itself failed (e.g. rc=124 from the
        subprocess timeout on a hung tmux server) - indeterminate, NOT
        death; one transiently hung probe must not kill the night's dream.
        """
        rc, out = await self._tmux(
            "display-message", "-p", "-t", self.session_name, "#{pane_dead}"
        )
        if rc == 0:
            return out.strip() != "1"
        low = out.lower()
        if "can't find" in low or "no server running" in low:
            return False
        return None

    async def _wait_for_result(self, path: Path, *, deadline: float) -> str:
        """Poll for the result file; require a stable non-empty size before reading.

        Also checks REPL liveness whenever the file isn't growing (absent,
        empty, or stalled): a claude process that exits early (binary
        crash, expired OAuth, bad --model, OOM) can never produce the
        result file, so waiting out the full timeout would declare the
        night lost an hour after it actually died. Declaring death needs
        definitive probe evidence (pane_dead / session gone) or two
        consecutive failed probes - a single indeterminate probe (hung
        tmux server) is not enough to abort the run.
        """
        last_size = -1
        dead_probes = 0
        while time.time() < deadline:
            size = path.stat().st_size if path.exists() else -1
            if size > 0 and size == last_size:
                return path.read_text(encoding="utf-8", errors="replace")
            growing = size > last_size
            last_size = size
            if growing:
                dead_probes = 0
            else:
                alive = await self._repl_alive()
                if alive is True:
                    dead_probes = 0
                else:
                    dead_probes += 1
                    if alive is False or dead_probes >= 2:
                        _, pane = await self._tmux(
                            "capture-pane", "-p", "-t", self.session_name
                        )
                        tail = pane.strip()[-500:]
                        raise _ReplExitedError(
                            f"dream REPL exited before writing a result file; "
                            f"pane tail: {tail}"
                        )
            await asyncio.sleep(self._config.poll_interval_s)
        raise TimeoutError(f"no result file at {path}")
