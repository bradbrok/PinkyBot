"""CodexTmuxSession — codex-CLI variant of TmuxSession (#215 PR2).

Goal (Brad, 2026-06-17): "codex tmux sessions needs to work like the cc tmux
sessions." Runs ``codex`` interactively in a detached ``pinky-codex-<agent>``
tmux pane: prompts in via bracketed-paste, responses out via the codex rollout
transcript tailer (PR1's ``CodexTmuxTranscriptTailer``), durable across daemon
restart, attachable.

**Architecture — Option A (subclass).** The design plan
(``specs/codex-tmux-transport-plan.md``) tentatively proposed copying the
worker/state-machine (Option B). Recon showed the codex-specific *seams* are
cleanly isolated, so this subclasses ``TmuxSession`` and overrides ONLY those
seams — inheriting the battle-hardened state machine, worker, inflight
watchdog, delivery/readiness gate, analytics and restart-survival verbatim
(zero divergence risk; future TmuxSession fixes auto-apply).

Overridden seams:
  * ``_build_session_name``      → ``pinky-codex-<agent>`` (distinct namespace)
  * ``_build_claude_cmd``        → the in-pane ``codex`` invocation (kept name;
                                   it's TmuxSession's spawn hook)
  * ``_build_repl_env``          → full daemon-env parity (Murzik #795 P1; mirrors
                                   the subprocess + app-server codex transports)
  * ``_project_dir`` / ``_has_prior_transcript`` / ``_discover_transcript_path``
                                 → codex rollout store (``~/.codex/sessions``)
  * ``_start_tailer``            → ``CodexTmuxTranscriptTailer``
  * ``_spawn_tmux_repl``         → wrap super() with codex trust pre-seed +
                                   first-run NUX dismissal + readiness gate
  * ``_watch_for_oauth_url``     → no-op (claude OAuth wall N/A for codex)
  * ``handle_stop_failure``      → no-op (codex turn-end = rollout tailer, not a
                                   ``.claude`` StopFailure hook; Murzik #795 P2)
  * paste settle                 → ``_CodexTmuxControl`` (4000ms, codex composer
                                   renders slower than claude's 300ms)

Validated live 2026-06-17: bracketed-paste → Enter drives a real codex 0.125.0
TUI to run a turn; the rollout is written with a discoverable ``session_meta.cwd``
and the tailer parses ``task_complete`` cleanly. Two first-run NUX prompts
(update-available; per-directory trust — the latter fires even with
``--dangerously-bypass-approvals-and-sandbox``) block cold-start and are handled
here.

Deferred to PR3: the ``notify`` low-latency wake hook (the polling tailer covers
turn-done detection without it), idle-sleep save-prompt text refinement, and the
resume-UUID-capture diagnostics.
"""
from __future__ import annotations

import asyncio
import os
import shlex
from pathlib import Path

from pinky_daemon.codex_tmux_transcript import (
    CodexTmuxTranscriptTailer,
    _discover_codex_rollout,
)
from pinky_daemon.streaming_session import StreamingSessionConfig
from pinky_daemon.tmux_session import (
    _PLACEHOLDER_TRANSCRIPT_PATH,
    TmuxSession,
    _log,
    _TmuxControl,
)

# Codex's inline composer renders slower than claude's REPL; the bracketed-paste
# needs a longer settle before the submit Enter or the Enter lands before the
# composer has captured the paste (→ text buffered, never submitted). 4000ms is
# the value Pulse v2 uses for codex; validated live 2026-06-17.
_CODEX_ENTER_DELAY_MS = 4000

# Cold-start NUX-dismissal budget (update-available + trust prompts).
_CODEX_NUX_TIMEOUT_SEC = 25.0
_CODEX_NUX_POLL_SEC = 0.5


class _CodexTmuxControl(_TmuxControl):
    """``_TmuxControl`` whose ``paste_text`` defaults to the slower codex settle
    so the inherited ``_deliver_turn`` (which calls ``paste_text(prompt,
    enter=True)`` with no explicit delay) submits codex turns reliably."""

    async def paste_text(
        self, text: str, *, enter: bool = True, enter_delay_ms: int = _CODEX_ENTER_DELAY_MS,
    ):
        return await super().paste_text(text, enter=enter, enter_delay_ms=enter_delay_ms)


class CodexTmuxSession(TmuxSession):
    """Interactive codex-CLI REPL in a detached tmux pane (see module docstring)."""

    def __init__(
        self,
        config: StreamingSessionConfig,
        *,
        tmux_control: _TmuxControl | None = None,
        **kwargs,
    ) -> None:
        super().__init__(config, tmux_control=tmux_control, **kwargs)
        # Respect an injected control (tests). Otherwise upgrade the default
        # control to the codex-aware one (slower paste settle).
        if tmux_control is None:
            self._tmux = _CodexTmuxControl(
                self._session_name,
                tmux_binary=self._tmux.tmux_binary,
                socket_name=self._tmux.socket_name,
                command_runner=self._tmux._runner,
            )
        # codex identity/config (mirrors CodexSession.__init__).
        self._codex_model = config.model or ""
        self._openai_api_key = config.provider_key or os.environ.get("OPENAI_API_KEY", "")
        self._reasoning_effort = config.thinking_effort or "medium"
        self._codex_mcp_servers = config.mcp_servers or {}

    # ── seam: session name ──────────────────────────────────────────────────
    def _build_session_name(self) -> str:
        """``pinky-codex-<agent>`` — distinct from claude's ``pinky-<agent>`` and
        the app-server's ``pinky-codex-as-<agent>`` so the three never collide."""
        return f"pinky-codex-{self.agent_name}"

    # ── seam: in-pane command ───────────────────────────────────────────────
    def _build_claude_cmd(self) -> str:
        """Build the in-pane ``codex`` invocation (method name kept because it's
        the hook ``_spawn_tmux_repl`` calls).

        Fresh vs resume mirrors claude's ``--continue`` gating: ``codex resume
        --last`` continues the most-recent session for this cwd, but only when a
        prior rollout exists (``_has_prior_transcript``) and a fresh context
        wasn't forced. ``-C`` (cwd) is only valid on a fresh launch — codex
        rejects it on ``resume``. Effort is set via ``-c`` at launch (no native
        keystroke block — hence ``_native_ultracode_pending = False``)."""
        force_fresh = bool(getattr(self._config, "force_fresh_context_once", False))
        has_prior = self._has_prior_transcript()
        use_resume = has_prior and not force_fresh

        # Attributes the inherited machinery reads (seek-on-first-bind, stats,
        # one-shot fresh-context reset, native-ultracode keystroke gate).
        self._last_launch_used_continue = use_resume
        self._last_launch_forced_fresh = force_fresh
        self._last_launch_had_prior_transcript = has_prior
        self._native_ultracode_pending = False

        parts = ["codex"]
        if use_resume:
            parts += ["resume", "--last"]
        # YOLO: bypass sandbox + approval uniformly (codex_session.py rationale).
        # --no-alt-screen keeps codex in an inline REPL (alt-screen TUI is hostile
        # to send-keys + transcript tail).
        parts += ["--dangerously-bypass-approvals-and-sandbox", "--no-alt-screen"]
        if self._codex_model:
            parts += ["-m", self._codex_model]
        if not use_resume:
            parts += ["-C", str(Path(self._config.working_dir or ".").resolve())]
        effort = self._reasoning_effort
        if effort and effort != "medium":
            if effort == "max":
                effort = "high"  # codex has no "max"
            parts += ["-c", f'model_reasoning_effort="{effort}"']
        # MCP injection (same -c form as CodexSession; works on fresh + resume).
        for name, cfg in (self._codex_mcp_servers or {}).items():
            if not isinstance(cfg, dict):
                continue
            url = cfg.get("url", "")
            if url:
                parts += ["-c", f'mcp_servers.{name}.url="{url}"']
                for hk, hv in (cfg.get("headers") or {}).items():
                    parts += ["-c", f'mcp_servers.{name}.http_headers.{hk}="{hv}"']

        cmd = " ".join(shlex.quote(p) for p in parts)
        _log(
            f"tmux[{self.agent_name}]: codex_cmd_built "
            f"mode={'resume' if use_resume else 'fresh'} "
            f"force_fresh={force_fresh} prior_rollout={has_prior}"
        )
        return cmd

    # ── seam: live REPL control ─────────────────────────────────────────────
    # The inherited live-apply machinery types CLAUDE slash commands
    # (/effort, /model) into the pane — the codex REPL doesn't speak them
    # (effort is a -c launch flag; /model is an interactive picker). Stash
    # only; the change lands on the next relaunch.

    async def apply_effort_live(self, level: str) -> str:
        from pinky_daemon.effort import resolve_cli_effort

        self.set_effort(level)
        # ultracode is Claude-native — resolve it so the codex launch flag
        # never sees the literal (max→high still maps at build time).
        self._reasoning_effort = resolve_cli_effort(self.effective_effort)
        return "pending_restart"

    async def apply_model_live(self, model: str) -> str:
        model = (model or "").strip()
        if not model:
            return "rejected"
        self._config.model = model
        self._codex_model = model
        return "pending_restart"

    # tmux-internal vars that must not leak into the nested REPL's children
    # (mirrors ``CodexAppServerSupervisor._ENV_DROP``).
    _ENV_DROP = frozenset({"TMUX", "TMUX_PANE"})

    # ── seam: env ───────────────────────────────────────────────────────────
    def _build_repl_env(self) -> dict[str, str]:
        """Full daemon-env parity for the codex tmux pane — NOT a small allowlist.

        tmux ``new-session`` drops the parent process env entirely; only the
        ``-e KEY=VAL`` pairs we pass survive into the pane (and its codex child).
        Both other codex transports launch codex with the daemon's FULL env:
        ``CodexSession._exec_codex`` uses ``env={**os.environ}`` (+ the configured
        key), and #792's tmux app-server (``CodexAppServerSupervisor._build_env``)
        does the same after CODEX_HOME / proxy / XDG / cert divergence proved a
        real footgun. A 4-key allowlist silently boots codex under different auth
        / network / session config: it drops ``HOME`` / ``XDG_*``, the proxy +
        TLS bundle (``HTTPS_PROXY`` / ``SSL_CERT_FILE`` / ``NODE_EXTRA_CA_CERTS``),
        ``OPENAI_BASE_URL`` / ``OPENAI_ORG`` / other ``OPENAI_*`` + ``CODEX_*``
        knobs, and any future auth/config env (Murzik #795 P1; same class as the
        #792 app-server env-parity fix).

        So we propagate the entire daemon env — including ``CODEX_HOME`` (item G:
        the child writes, and discovery scans, the SAME rollout store) and
        ``PATH`` (so the ``codex`` / ``node`` binaries resolve) — minus
        tmux-internal vars and any value tmux ``-e`` can't carry (newlines, which
        are pathological for env anyway), then overlay the configured
        ``OPENAI_API_KEY`` (item H) and this agent's ``PINKY_AGENT_NAME``. No
        ANTHROPIC_* special-casing is needed: codex ignores them, exactly as the
        subprocess transport already inherits them harmlessly.
        """
        env: dict[str, str] = {}
        for key, value in os.environ.items():
            if key in self._ENV_DROP:
                continue
            if "\n" in value or "\r" in value:
                _log(
                    f"tmux[{self.agent_name}]: dropping multiline env {key!r} "
                    f"(cannot pass via tmux -e)"
                )
                continue
            env[key] = value
        if self._openai_api_key:
            env["OPENAI_API_KEY"] = self._openai_api_key
        if self.agent_name:
            env["PINKY_AGENT_NAME"] = self.agent_name
        return env

    # ── seam: transcript discovery (codex rollout store) ────────────────────
    def _project_dir(self) -> Path:
        """Codex's rollout store root (``$CODEX_HOME/sessions`` or
        ``~/.codex/sessions``). Codex does NOT slug the cwd into the path the way
        claude does — discovery is by ``session_meta.cwd`` match, not directory
        name — so this is just the scan root."""
        codex_home = Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))
        return codex_home / "sessions"

    def _has_prior_transcript(self) -> bool:
        """True iff a codex rollout for this agent's cwd already exists (gates
        ``codex resume --last``)."""
        return _discover_codex_rollout(self._config.working_dir or ".") is not None

    def _discover_transcript_path(self) -> Path | None:
        """Newest rollout whose ``session_meta.cwd`` == this agent's cwd, or None
        (cold start before the first turn writes a rollout)."""
        return _discover_codex_rollout(self._config.working_dir or ".")

    # ── seam: tailer class ──────────────────────────────────────────────────
    async def _start_tailer(self) -> None:
        """Construct the codex tailer (the only part that differs from claude),
        then delegate to ``super()._start_tailer()`` for the per-spawn arming /
        first-bind / readiness-gate-reset / #565 recovery scheduling (it takes
        the retained-instance branch because ``self._tailer`` is now set)."""
        if self._tailer is None:
            guessed = self._discover_transcript_path()
            path = guessed or _PLACEHOLDER_TRANSCRIPT_PATH
            self._tailer = CodexTmuxTranscriptTailer(
                transcript_path=path,
                on_turn_complete=self._handle_turn_complete,
                agent_name=self.agent_name,
                model=self._codex_model,
                path_discovery=self._discover_transcript_path,
            )
            if guessed is not None:
                # Warm-wake / resume: seek to EOF so we don't replay history.
                try:
                    self._tailer.set_offset(guessed.stat().st_size)
                except OSError:
                    pass
        await super()._start_tailer()

    # ── seam: cold-start (codex trust pre-seed + NUX dismissal + readiness) ──
    async def _spawn_tmux_repl(self) -> None:
        cwd = str(Path(self._config.working_dir or ".").resolve())
        self._seed_codex_trust(cwd)
        await super()._spawn_tmux_repl()
        await self._codex_dismiss_nux_and_ready()

    async def _watch_for_oauth_url(self) -> None:
        """No-op: the claude OAuth login-wall relay (#205) doesn't apply to
        codex; overriding prevents the inherited watcher from scanning the codex
        pane for a wall that never appears."""
        return

    async def handle_stop_failure(
        self, error_type: str, message: str = "", session_id: str = ""
    ) -> bool:
        """No-op for codex (Murzik #795 P2 hardening).

        The inherited ``handle_stop_failure`` synthesizes a failed
        ``TurnResponse`` and pops the in-flight turn off ``_inflight_metas``. That
        is correct for Claude Code, whose ``.claude`` StopFailure hook (#584/#108)
        POSTs ``/transport/stop-failure`` as the authoritative turn-end marker for
        terminal API-error turns. Codex does NOT close turns that way — a codex
        turn ends via the rollout's ``task_complete`` / ``turn_aborted`` (owned by
        ``CodexTmuxTranscriptTailer``), and codex doesn't run ``.claude`` hooks. A
        StopFailure POST landing on a codex-tmux agent (stale/misrouted wire)
        would therefore falsely pop a codex turn that may still be completing
        normally. Fail safe: ignore it and let the tailer own turn-end. Returns
        ``False`` — the inherited "nothing was resolved" signal — so the
        ``/transport/stop-failure`` endpoint's response shape is unchanged. A real
        codex failure hook can replace this in a later PR."""
        if self._inflight_metas:
            _log(
                f"tmux[{self.agent_name}]: ignoring StopFailure ({error_type!r}) "
                f"for codex session — codex turn-end is owned by the rollout tailer"
            )
        return False

    def _seed_codex_trust(self, cwd: str) -> None:
        """Idempotently mark ``cwd`` trusted in ``config.toml`` so codex's
        interactive "trust this directory?" NUX doesn't block cold-start.

        ``--dangerously-bypass-approvals-and-sandbox`` does NOT skip this prompt
        in the interactive TUI (verified 2026-06-17), and trust is not inherited
        from a trusted parent dir. Best-effort; a failure here at worst leaves
        the trust NUX for ``_codex_dismiss_nux_and_ready`` to handle."""
        try:
            codex_home = Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))
            cfg = codex_home / "config.toml"
            real = os.path.realpath(cwd)
            header = f'[projects."{real}"]'
            existing = cfg.read_text(encoding="utf-8") if cfg.exists() else ""
            if header in existing:
                return
            codex_home.mkdir(parents=True, exist_ok=True)
            with cfg.open("a", encoding="utf-8") as fh:
                fh.write(f'\n{header}\ntrust_level = "trusted"\n')
            _log(f"tmux[{self.agent_name}]: seeded codex trust for {real}")
        except Exception as e:
            _log(f"tmux[{self.agent_name}]: codex trust seed failed (non-fatal): {e}")

    async def _codex_dismiss_nux_and_ready(self) -> None:
        """Navigate past codex's first-run NUX prompts (update-available; trust,
        if the pre-seed missed it) and open the readiness gate once the composer
        is live.

        Codex has no SessionStart-equivalent hook before the first turn, and the
        rollout (``session_meta``) is only written AFTER the first prompt is
        submitted — so readiness can't be gated on the transcript. Instead we
        poll the pane, dismiss known prompts via send-keys, and open
        ``_session_ready_event`` when the composer renders. Codex FIFO-queues
        input, so a paste that races this is buffered, not lost. Best-effort +
        time-bounded; the gate is opened regardless on timeout (the inherited
        delivery path also has its own gate-timeout fallback)."""
        loop = asyncio.get_event_loop()
        deadline = loop.time() + _CODEX_NUX_TIMEOUT_SEC
        composer_seen = False
        while loop.time() < deadline:
            try:
                snap = (await self._tmux.capture_pane(lines=40)).stdout
            except Exception:
                snap = ""
            low = snap.lower()
            if "update available" in low and "press enter to continue" in low:
                # Menu: 1 Update now / 2 Skip / 3 Skip until next — move off
                # "Update now" to "Skip" and confirm. NEVER pick "Update now"
                # (it would run `bun install`).
                await self._tmux.send_keys("Down", enter=False)
                await self._tmux.send_keys("Enter", enter=False)
            elif "do you trust" in low:
                # Trust prompt (pre-seed missed it) — default highlight is
                # "Yes, continue".
                await self._tmux.send_keys("Enter", enter=False)
            elif "/model to change" in low or ("directory:" in low and "permissions:" in low):
                composer_seen = True
                break
            await asyncio.sleep(_CODEX_NUX_POLL_SEC)

        if not self._session_ready_event.is_set():
            self._session_ready_event.set()
        _log(
            f"tmux[{self.agent_name}]: codex cold-start ready "
            f"(composer_seen={composer_seen})"
        )
