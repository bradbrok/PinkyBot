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
  * ``_build_repl_env``          → filtered Codex authority and compatibility
                                   payload, with the configured tmux pane PATH
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
import json
import os
import re
import shlex
import time
from collections.abc import Iterator
from pathlib import Path

from pinky_daemon import codex_launch_env, isolated_launch_env
from pinky_daemon.codex_effort import resolve_codex_effort
from pinky_daemon.codex_home import (
    MANAGED_CONFIG_SENTINEL,
    codex_home_for,
    per_agent_codex_home_enabled,
    prepare_agent_codex_home,
    shared_codex_home,
    validate_agent_codex_home,
)
from pinky_daemon.codex_mcp_env import mcp_cli_config
from pinky_daemon.codex_tmux_transcript import (
    CodexTmuxTranscriptTailer,
    _discover_codex_rollout,
    _read_owned_codex_rollout,
)
from pinky_daemon.context_window import resolve_context_window
from pinky_daemon.isolated_files import open_owned_transcript
from pinky_daemon.streaming_session import StreamingSessionConfig
from pinky_daemon.tmux_session import (
    _PLACEHOLDER_TRANSCRIPT_PATH,
    TmuxSession,
    _log,
    _regular_transcript_candidates,
    _SchedulerDeliveryCancelled,
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

# A task-close can leave accepted FIFO metadata behind when several native
# queue pastes were consumed by one Codex turn.  If an unaccepted paste is left
# instead (for example, its submit Enter was lost), require two matching live
# pane reads before reconciling it.  The literal Codex 0.146.1 production
# discriminator is ``esc to interrupt`` while busy; an agent-cwd footer proves
# a capture without that literal is rendered rather than empty/garbled.
_CODEX_IDLE_CONFIRM_SEC = 0.25
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


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

    # #860: analytics rows must not claim "anthropic" for codex turns — the
    # pricing lookup keys on (provider, model) and never crosses providers, so
    # anthropic/gpt-* priced every turn at $0. "codex_cli" matches the SDK
    # path's default (CodexSession._analytics_log_turn_usage) and
    # analytics_store._provider_alias maps it onto the openai rate rows.
    _ANALYTICS_PROVIDER = "codex_cli"
    _trace_transport_kind = "tmux_codex"
    _scrub_codex_headers = True

    def _reported_context_window(self) -> int:
        """Return the latest positive window reported by the Codex rollout."""
        tailer = getattr(self, "_tailer", None)
        tailer_value = getattr(tailer, "model_context_window", 0)
        if (
            isinstance(tailer_value, (int, float))
            and not isinstance(tailer_value, bool)
            and tailer_value > 0
        ):
            return int(tailer_value)
        usage = getattr(self, "usage", None)
        last = getattr(usage, "last_usage", {})
        if not isinstance(last, dict):
            return 0
        value = last.get("model_context_window")
        if (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and value > 0
        ):
            return int(value)
        return 0

    def _raw_max_tokens_for_model(self) -> int:
        """Resolve Codex's own window without Claude Code buffer semantics."""
        return resolve_context_window(
            self._config.model or "",
            reported_max=self._reported_context_window(),
        )

    def _max_tokens_for_model(self) -> int:
        """Codex has no Claude Code autocompact buffer to subtract."""
        return self._raw_max_tokens_for_model()

    def _current_total_tokens(self) -> int:
        """Use Codex's explicit live-window occupancy, not a schema sum."""
        usage = getattr(self, "usage", None)
        last = getattr(usage, "last_usage", {})
        if not isinstance(last, dict):
            return 0
        try:
            return max(0, int(last.get("total_tokens", 0) or 0))
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _normalize_turn_usage(u: dict) -> dict:
        """Codex usage schema → the daemon's disjoint convention (#860).

        Codex reports ``input_tokens`` INCLUSIVE of the cached prefix, with the
        cached span under ``cached_input_tokens`` — while every consumer here
        (SessionUsage accumulation, ``compute_cost_from_usage``, analytics
        ``log_turn_usage``, the context gauge's window reconstruction) follows
        the Anthropic convention: ``input_tokens`` is the uncached remainder
        and the cached span rides ``cache_read_input_tokens``. Mirrors
        ``CodexSession.uncached_input_tokens`` on the SDK path; pricing the
        inclusive number at the full input rate would overstate review-heavy
        sessions roughly 10x. The ambiguous source key is consumed, not
        forwarded. A dict without ``cached_input_tokens`` (or with malformed
        counts) passes through untouched — fail toward the visible undercount,
        never a silent double-bill.
        """
        if "cached_input_tokens" not in u:
            return u
        try:
            cached = max(0, int(u.get("cached_input_tokens") or 0))
            inclusive = max(0, int(u.get("input_tokens") or 0))
        except (TypeError, ValueError):
            return u
        out = dict(u)
        out.pop("cached_input_tokens", None)
        out["input_tokens"] = max(0, inclusive - cached)
        out["cache_read_input_tokens"] = cached
        return out

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
            from pinky_daemon.tmux_session import production_tmux_control

            self._tmux = production_tmux_control(
                self._session_name,
                tmux_binary=self._tmux.tmux_binary,
                socket_name=self._tmux.socket_name,
                socket_path=self._tmux.socket_path,
                command_runner=self._tmux._runner,
                server_config=self._tmux.server_config,
                control_type=_CodexTmuxControl,
            )
        # codex identity/config (mirrors CodexSession.__init__).
        self._codex_model = config.model or ""
        self._openai_api_key = config.provider_key or os.environ.get("OPENAI_API_KEY", "")
        self._reasoning_effort = config.thinking_effort
        self._selected_reasoning_effort: str | None = None
        self._codex_mcp_servers = config.mcp_servers or {}
        self._codex_last_scheduler_gate_signature: tuple[bool, ...] | None = None
        self._codex_user_content_warned = False
        self._codex_user_no_text_warned = False

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
        effort = resolve_codex_effort(self._reasoning_effort)
        self._selected_reasoning_effort = effort
        if effort is not None:
            parts += ["-c", f'model_reasoning_effort="{effort}"']
        # MCP injection (same -c form as CodexSession; works on fresh + resume).
        mcp_args, _ = mcp_cli_config(self._codex_mcp_servers or {})
        parts += mcp_args

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
        self.set_effort(level)
        return "pending_restart"

    def set_effort(self, level: str) -> None:
        """Stash a validated override for the next Codex launch."""
        super().set_effort(level)
        self._reasoning_effort = self._effort_override or self._config.thinking_effort

    def clear_effort_override(self) -> None:
        super().clear_effort_override()
        self._reasoning_effort = self._config.thinking_effort

    @property
    def stats(self) -> dict:
        stats = super().stats
        desired = resolve_codex_effort(self._reasoning_effort)
        pending = desired != self._selected_reasoning_effort
        stats.update(
            thinking_effort=self._selected_reasoning_effort,
            thinking_effort_pending=pending,
            pending_thinking_effort=desired if pending else None,
        )
        return stats

    async def apply_model_live(self, model: str) -> str:
        model = (model or "").strip()
        if not model:
            return "rejected"
        self._config.model = model
        self._codex_model = model
        return "pending_restart"

    # ── seam: env ───────────────────────────────────────────────────────────
    def _launch_env_policy(self) -> isolated_launch_env.LaunchPolicy:
        return codex_launch_env.capture_policy(
            agent_name=self.agent_name, registry=self._registry, log=_log,
        )

    def _wrap_launch_command(
        self, command: str, env: dict[str, str], policy: isolated_launch_env.LaunchPolicy,
    ) -> str:
        return codex_launch_env.wrap_command(command, env)

    def _build_repl_env(
        self, *, launch_policy: isolated_launch_env.LaunchPolicy | None = None,
        report_shadow: bool = True,
    ) -> dict[str, str]:
        policy = launch_policy or self._launch_env_policy()
        daemon_url = None
        if self._container_agent() is not None:
            daemon_url = os.environ.get(
                "PINKY_CONTAINER_DAEMON_URL", "http://host.containers.internal:8888",
            )
        env = codex_launch_env.build_env(
            agent_name=self.agent_name, config=self._config, api_key=self._openai_api_key,
            policy=policy, log=_log, servers=self._codex_mcp_servers,
            daemon_url=daemon_url, report=report_shadow,
        )
        from pinky_daemon.tmux_server_env import normalize_codex_path

        return normalize_codex_path(env, self._tmux)

    # ── seam: transcript discovery (codex rollout store) ────────────────────
    def _project_dir(self) -> Path:
        """Codex's rollout store root (``$CODEX_HOME/sessions`` or
        ``~/.codex/sessions``). Codex does NOT slug the cwd into the path the way
        claude does — discovery is by ``session_meta.cwd`` match, not directory
        name — so this is just the scan root."""
        return codex_home_for(self._config) / "sessions"

    def _has_prior_transcript(self) -> bool:
        """True iff a codex rollout for this agent's cwd already exists (gates
        ``codex resume --last``)."""
        return self._discover_transcript_path() is not None

    def _discover_transcript_path(self) -> Path | None:
        """Newest rollout whose ``session_meta.cwd`` == this agent's cwd, or None
        (cold start before the first turn writes a rollout)."""
        owned_root = self._owned_rollout_root()
        if owned_root is None:
            return _discover_codex_rollout(self._config.working_dir or ".", agent=self._config)
        discovered = _discover_codex_rollout(
            self._config.working_dir or ".", agent=self._config, owned_root=owned_root,
        )
        if isinstance(discovered, tuple):
            self._codex_discovered_binding = discovered
            return discovered[0]
        return discovered

    def _owned_rollout_root(self) -> Path | None:
        """Freeze the managed root without following its writable literal tail."""
        if self._isolation_status() != "isolated":
            return None
        root = getattr(self, "_codex_owned_root", None)
        if root is None:
            if per_agent_codex_home_enabled():
                override = (self._config.codex_home or "").strip()
                home = Path(override).expanduser().resolve() if override else (
                    Path(self._config.working_dir).expanduser().resolve() / ".codex"
                )
            else:
                home = shared_codex_home().expanduser().resolve()
            root = self._codex_owned_root = home / "sessions"
        return root

    def _owned_rollout_binding(self, path: Path):
        return _read_owned_codex_rollout(
            path, self._config.working_dir or ".", self._owned_rollout_root(),
        )

    def _discover_bound_rollout(self):
        path = self._discover_transcript_path()
        if self._owned_rollout_root() is None:
            return path
        binding = getattr(self, "_codex_discovered_binding", None)
        return binding if binding is not None and binding[0] == path else None

    def _transcript_predicate(self):
        root = self._owned_rollout_root()
        return (lambda opened: opened.is_relative_to(root)) if root is not None else None

    def _transcript_expected_identity(self, path: Path):
        if self._owned_rollout_root() is None:
            return None
        tailer = self._tailer
        if tailer is not None and tailer.transcript_path == path:
            return tailer._owned_identity
        binding = getattr(self, "_codex_discovered_binding", None)
        return binding[1] if binding is not None and binding[0] == path else None

    def _open_transcript(self, path: Path, identity=None):
        predicate = self._transcript_predicate()
        if predicate is None:
            return path.open("rb")
        identity = identity or self._transcript_expected_identity(path)
        if identity is None:
            return None
        handle = open_owned_transcript(path, predicate)
        if handle is not None:
            info = os.fstat(handle.fileno())
            if (info.st_dev, info.st_ino) == identity:
                return handle
            handle.close()
        return None

    def _transcript_candidates(self) -> Iterator[tuple[Path, float]]:
        """Enumerate rollout paths without opening historical session content."""
        yield from _regular_transcript_candidates(self._project_dir().glob("**/rollout-*.jsonl"))

    def _is_own_transcript(self, path: Path) -> bool | tuple[Path, tuple[int, int]] | None:
        """Inspect ownership only for a bounded set of post-launch rollouts."""
        if self._owned_rollout_root() is not None:
            return self._owned_rollout_binding(path)
        try:
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                metadata = json.loads(handle.readline())
            if metadata.get("type") != "session_meta":
                return False
            cwd = metadata.get("payload", {}).get("cwd", "")
            return os.path.realpath(cwd) == os.path.realpath(self._config.working_dir or ".")
        except (OSError, ValueError, TypeError, AttributeError):
            return False

    # ── seam: tailer class ──────────────────────────────────────────────────
    async def _start_tailer(self) -> None:
        """Construct the codex tailer (the only part that differs from claude),
        then delegate to ``super()._start_tailer()`` for the per-spawn arming /
        first-bind / readiness-gate-reset / #565 recovery scheduling (it takes
        the retained-instance branch because ``self._tailer`` is now set)."""
        if self._tailer is None:
            guessed = self._discover_transcript_path()
            path = guessed or _PLACEHOLDER_TRANSCRIPT_PATH
            binding = getattr(self, "_codex_discovered_binding", None)
            owned_root = self._owned_rollout_root()
            self._tailer = CodexTmuxTranscriptTailer(
                transcript_path=path,
                on_turn_complete=self._handle_turn_complete,
                agent_name=self.agent_name,
                model=self._codex_model,
                path_discovery=(
                    self._discover_bound_rollout if owned_root is not None
                    else self._discover_transcript_path
                ),
                # Scheduler receipts use the same exact transcript-observation
                # contract as Claude tmux.  Codex records acceptance as an
                # event_msg/user_message entry instead of Claude's user or
                # queue-operation rows.
                on_entry=self._on_transcript_entry,
                owned_root=owned_root,
                owned_identity=(binding[1] if binding is not None and binding[0] == path else None),
                owned_discovery=self._owned_rollout_binding if owned_root is not None else None,
            )
            if guessed is not None:
                # Warm-wake / resume: seek to EOF so we don't replay history.
                try:
                    self._tailer.set_offset(
                        self._tailer.bound_size if owned_root is not None else guessed.stat().st_size
                    )
                except OSError:
                    pass
        await super()._start_tailer()

    # ── seam: scheduler queued-route delivery (#1006) ──────────────────────
    def _codex_scheduler_evidence(self, candidate=None) -> tuple[bool, ...]:
        """Return the five Codex-owned scheduler gate inputs, in log order."""
        candidate_in_worker = (
            candidate is not None and self._inflight_turn is candidate
        )
        return (
            bool(self._inflight_tool_calls),
            bool(
                self._inflight_turn is not None
                and not candidate_in_worker
            ),
            bool(
                not self._message_queue.empty()
                and not candidate_in_worker
            ),
            bool(self._inflight_metas),
            bool(getattr(self._tailer, "_active", False)),
        )

    def _scheduler_pane_busy(self, candidate=None) -> bool:
        """Use Codex-local turn state instead of Claude hook status.

        The base tmux implementation deliberately requires a fresh persisted
        ``idle`` row from Claude Code's working/idle hooks before it pastes a
        scheduler turn.  Codex does not run those hooks, so that row is stale
        or absent and every trigger wake waits forever despite an idle pane.

        Keep the same conservative no-overlap rule using the evidence Codex
        does own: ordinary work in hand/queued, transcript-backed inflight
        metadata, tool activity, and the rollout tailer's active-turn flag.
        The inherited scheduler task, REPL lock, logs, exact receipt, and
        retirement paths remain unchanged.
        """
        if self._scheduler_busy_deadline_reached(candidate):
            return self._has_unresolved_pasted_acceptance()
        evidence = self._codex_scheduler_evidence(candidate)
        busy = any(evidence)
        signature = (*evidence, busy)
        if signature != self._codex_last_scheduler_gate_signature:
            self._codex_last_scheduler_gate_signature = signature
            tools, in_hand, queued, metas, tailer_active = evidence
            _log(
                f"tmux[{self.agent_name}]: CODEX_SCHEDULER_GATE "
                f"tools={tools} in_hand={in_hand} queued={queued} "
                f"metas={metas} tailer_active={tailer_active} busy={busy} "
                f"meta_depth={len(self._inflight_metas)} "
                f"queue_depth={self._message_queue.qsize()}"
            )
        return busy

    def _codex_gate_blocked_only_by_metas(self, candidate=None) -> bool:
        tools, in_hand, queued, metas, tailer_active = (
            self._codex_scheduler_evidence(candidate)
        )
        if not (metas and not any((tools, in_hand, queued, tailer_active))):
            return False
        # Exact scheduler/wake receipts remain authoritative. Pane-idle proof
        # may retire ordinary lost-submit metadata, but it must not turn a
        # pasted-without-receipt exact fire into a negative result and replay.
        return all(
            all(
                receipt is None or receipt.done()
                for receipt in (
                    meta.turn.scheduler_delivery,
                    meta.turn.submission_receipt,
                )
            )
            for meta in self._inflight_metas
        )

    @staticmethod
    def _codex_pane_is_explicitly_idle(snapshot: str, agent_name: str) -> bool:
        """Recognize the literal Codex no-busy-marker + footer pane shape.

        The composer placeholder persists while Codex is working, so it is not
        evidence either way. ``esc to interrupt`` is the observed Codex busy
        literal and always vetoes idle. The model/cwd footer is required so an
        empty, truncated, or garbled capture fails closed as unknown/busy.
        """
        if not snapshot or "esc to interrupt" in snapshot:
            return False
        plain_lines = [
            _ANSI_RE.sub("", raw).strip()
            for raw in snapshot.splitlines()
        ]
        return any(
            " · " in line and line.rstrip("/").endswith(f"/{agent_name}")
            for line in plain_lines
        )

    async def _codex_capture_explicit_idle(self) -> bool:
        try:
            result = await self._tmux.capture_pane(lines=12, escapes=True)
        except Exception:
            return False
        return bool(
            result.ok
            and self._codex_pane_is_explicitly_idle(
                result.stdout or "", self.agent_name
            )
        )

    def _reconcile_codex_phantom_metas(self, metas, *, reason: str) -> int:
        """Remove proven historical Codex FIFO entries without a restart."""
        stale = list(metas)
        if not stale:
            return 0
        stale_ids = {id(meta) for meta in stale}
        kept = [
            meta for meta in self._inflight_metas
            if id(meta) not in stale_ids
        ]
        self._inflight_metas.clear()
        self._inflight_metas.extend(kept)
        for meta in stale:
            event = meta.completion_event
            if event is not None and not event.is_set():
                event.set()
            delivery = meta.turn.scheduler_delivery
            if delivery is not None and not delivery.done():
                delivery.set_result(bool(meta.turn.transport_accepted))
        self._head_started_at = time.time() if kept else None
        self._inflight_pane_ext_anchor = None
        _log(
            f"tmux[{self.agent_name}]: CODEX_PHANTOM_META_RECONCILE "
            f"reason={reason} reconciled={len(stale)} remaining={len(kept)}"
        )
        return len(stale)

    async def _wait_for_scheduler_delivery_slot(self, turn) -> None:
        """Wait for Codex work, with a literal idle-pane stale-meta exit."""
        if not turn.scheduler_serialized:
            return
        while self._scheduler_pane_busy(turn):
            delivery = turn.scheduler_delivery
            if delivery is not None and delivery.cancelled():
                raise _SchedulerDeliveryCancelled
            if self._codex_gate_blocked_only_by_metas(turn):
                first_idle = await self._codex_capture_explicit_idle()
                if first_idle:
                    await asyncio.sleep(_CODEX_IDLE_CONFIRM_SEC)
                    if (
                        self._codex_gate_blocked_only_by_metas(turn)
                        and await self._codex_capture_explicit_idle()
                    ):
                        self._reconcile_codex_phantom_metas(
                            list(self._inflight_metas),
                            reason="explicit_idle_pane",
                        )
                        continue
            await asyncio.sleep(0.25)
        delivery = turn.scheduler_delivery
        if delivery is not None and delivery.cancelled():
            raise _SchedulerDeliveryCancelled

    async def _handle_turn_complete(self, response) -> None:
        """Retire Codex metas coalesced into the just-closed rollout turn."""
        self._defer_scheduler_idle_notify = True
        try:
            await super()._handle_turn_complete(response)
        finally:
            self._defer_scheduler_idle_notify = False
        # ``on_entry`` runs before the tailer feeds the following task_complete.
        # Therefore any remaining turn already transport-accepted at this exact
        # callback boundary was accepted inside the turn that just closed. It
        # cannot own a future task_complete; leaving it behind is the #1006
        # stale-busy recurrence.
        coalesced = [
            meta for meta in self._inflight_metas
            if meta.turn.transport_accepted
        ]
        self._reconcile_codex_phantom_metas(
            coalesced, reason="accepted_before_task_close"
        )
        # A post-paste task close proves task completion, not ownership of
        # the pasted prompt. An autonomous task can be the first task after
        # the anchor. Only the matching user_message receipt below can accept
        # the wake; without it, retain unresolved late-receipt authority.
        self._notify_scheduler_idle_if_ready()

    @staticmethod
    def _codex_paste_ticket(turn):
        return (
            turn.transcript_path_at_paste,
            turn.transcript_file_identity_at_paste,
            turn.transcript_offset_at_paste,
            turn.transcript_anchor_start_at_paste,
            turn.transcript_anchor_at_paste,
            turn.transcript_ticket_captured_at_ns,
        )

    def _on_transcript_entry(self, entry: dict) -> None:
        """Map Codex rollout acceptance onto the shared exact-receipt path."""
        payload = entry.get("payload")
        payload = payload if isinstance(payload, dict) else {}
        current_user_row = (
            entry.get("type") == "response_item"
            and payload.get("type") == "message"
            and payload.get("role") == "user"
        )
        legacy_user_row = (
            entry.get("type") == "event_msg" and payload.get("type") == "user_message"
        )
        if current_user_row:
            content = payload.get("content")
            if not isinstance(content, list) or any(
                not isinstance(item, dict)
                or (item.get("type") == "input_text" and not isinstance(item.get("text"), str))
                for item in content
            ):
                if not self._codex_user_content_warned:
                    self._codex_user_content_warned = True
                    _log("WARNING malformed Codex user-row content; receipt ignored")
                return
            prompt = "".join(
                item["text"] for item in content if item.get("type") == "input_text"
            )
            if not prompt:
                if content and not self._codex_user_no_text_warned:
                    self._codex_user_no_text_warned = True
                    item_types = sorted({str(item.get("type")) for item in content})
                    _log(
                        "WARNING Codex user-row content yielded no text; "
                        f"item types={item_types}"
                    )
                return
        elif legacy_user_row:
            prompt = payload.get("message")
        else:
            super()._on_transcript_entry(entry)
            return
        if not isinstance(prompt, str):
            return
        pointer = getattr(self._tailer, "entry_pointer", None) or {
            "path": getattr(self._tailer, "transcript_path", ""), "offset": None,
        }
        self._trace_observed_prompt(prompt, pointer=pointer)
        turn = self._match_acceptance_turn(prompt)
        if current_user_row and (
            turn is None
            or not self._transcript_entry_matches_ticket(
                entry_offset=pointer.get("offset"),
                source_identity=pointer.get("identity"),
                ticket_offset=turn.transcript_offset_at_paste,
                ticket_identity=turn.transcript_file_identity_at_paste,
            )
        ):
            if turn is not None:
                identity = turn.transcript_file_identity_at_paste
                offset = turn.transcript_offset_at_paste
                path = turn.transcript_path_at_paste
                detail = ""
                reason = "paste ticket mismatch"
                if pointer.get("identity") is None or pointer.get("offset") is None:
                    shape = "no_pointer"
                elif path is not None and identity is None and offset == 0:
                    shape = "cold-start"
                    reason = "cold_start_ticket_unverified"
                    cold_reason = (
                        "placeholder" if path == _PLACEHOLDER_TRANSCRIPT_PATH else "file_missing"
                    )
                    detail = f" cold_start_reason={cold_reason!r}"
                elif path is None:
                    shape = "unbound"
                    reason = "no paste ticket"
                elif identity is None or offset is None:
                    shape = "inaccessible"
                    reason = "ticket identity/offset missing"
                else:
                    shape = "mismatch"
                if shape not in turn.transcript_ticket_warned_shapes:
                    turn.transcript_ticket_warned_shapes.add(shape)
                    _log(
                        f"WARNING codex user-row ticket turn_id={id(turn)} "
                        f"entry_offset={pointer.get('offset')} "
                        f"source_identity={pointer.get('identity')} "
                        f"ticket_offset={offset} ticket_identity={identity} "
                        f"shape={shape} reason={reason!r}{detail}"
                    )
            return
        # A user row and task_complete can arrive in one read before paste_text
        # returns. Reserve routing metadata before resolving the receipt so that
        # completion has a head to retire; normal post-paste recording is idempotent.
        if (
            turn is not None
            and turn.scheduler_delivery is not None
            and not turn.pane_delivery_recorded
        ):
            self._finish_turn_delivery(turn)
        self._mark_transport_accepted(turn)

    # ── seam: cold-start (codex trust pre-seed + NUX dismissal + readiness) ──
    def _preflight_transport_replacement(self) -> None:
        """Validate the isolated Codex home before inherited tmux teardown."""
        validate_agent_codex_home(
            self._config,
            soul_version_store=self._registry,
        )

    def _prepare_tmux_spawn(self) -> None:
        """Snapshot and publish only after inherited strict stale cleanup."""
        prepare_agent_codex_home(
            self._config,
            log=_log,
            soul_version_store=self._registry,
        )
        cwd = str(Path(self._config.working_dir or ".").resolve())
        self._seed_codex_trust(cwd)

    async def _spawn_tmux_repl(self) -> None:
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
            codex_home = codex_home_for(self._config)
            cfg = codex_home / "config.toml"
            real = os.path.realpath(cwd)
            header = f'[projects."{real}"]'
            existing = cfg.read_text(encoding="utf-8") if cfg.exists() else ""
            if per_agent_codex_home_enabled() and not existing.startswith(
                MANAGED_CONFIG_SENTINEL
            ):
                _log(
                    f"tmux[{self.agent_name}]: managed codex config unavailable "
                    f"at {cfg}; trust seed skipped"
                )
                return
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
