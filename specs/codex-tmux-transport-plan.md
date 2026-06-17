# CodexTmuxSession — codex tmux transport mirroring the cc tmux transport

> Implementation plan (task #215). Goal (Brad, 2026-06-17): "codex tmux sessions
> needs to work like the cc tmux sessions." Supersedes the #791 app-server/kuzya
> approach (parked). Plan produced by a Plan subagent 2026-06-17; execute directly.

## 0. Verified facts (this box, codex-cli 0.125.0)

- `codex [PROMPT]` (no subcommand) launches the interactive TUI (alt-screen by
  default; `--no-alt-screen` for inline). `codex resume --last` / `codex resume
  <SESSION_ID>` resume a prior interactive session; `--last` is cwd-filtered.
- Sessions persist to `~/.codex/sessions/YYYY/MM/DD/rollout-<ISO-ts>-<uuid>.jsonl`,
  written incrementally, one JSON object per line (same tail model as Claude .jsonl).
- Transcript record shapes (verified):
  - Line 1: `{"type":"session_meta","payload":{"id":"<uuid>","cwd":"...","cli_version":...,"git":{...}}}` — `payload.cwd` pins the agent; `payload.id` is the resumable UUID.
  - Turn end (authoritative): `{"type":"event_msg","payload":{"type":"task_complete","turn_id":"...","last_agent_message":"<full final text>","completed_at":<epoch>,"duration_ms":N,...}}`.
  - Turn start: `event_msg`/`task_started` (`turn_id`, `started_at`, `model_context_window`).
  - Streamed chunks: `event_msg`/`agent_message` (`{"message":...,"phase":"commentary"}`); also `response_item`/`message` role assistant.
  - Usage (arrives just BEFORE task_complete): `event_msg`/`token_count` → `info.last_token_usage` {input_tokens, cached_input_tokens, output_tokens, reasoning_output_tokens, total_tokens}. `info` is sometimes null (rate-limit-only events) → null-guard.
  - User input: `event_msg`/`user_message`. Abort: `event_msg`/`turn_aborted` (second turn-end marker to handle).
- `notify` program (config `notify = ["<prog>", ...]`): codex invokes it on
  `agent-turn-complete` with JSON `{type, thread-id, turn-id, cwd, input-messages,
  last-assistant-message}` — direct analog of Claude's Stop hook → low-latency wake.
  Codex also has PreToolUse/PostToolUse hooks.
- Trust: agent cwd inherits `trust_level="trusted"` from /Users/oleg/PinkyBot in
  `~/.codex/config.toml`; `--dangerously-bypass-approvals-and-sandbox` skips
  trust/approval/sandbox uniformly (already used by CodexSession._build_codex_cmd, codex_session.py:553).
- Reusable infra: `_TmuxControl` (tmux_session.py:336) already reused by
  codex_app_server_tmux.py:48; `CodexAppServerSupervisor` shows the tmux-under-codex
  pattern. App-server path is parked — do not base on it.

## 1. Architecture

New `src/pinky_daemon/codex_tmux_session.py` → `CodexTmuxSession`, modeled on
`TmuxSession`, codex-specific launch + transcript reader. Same public interface the
broker/api consume polymorphically: connect, disconnect, send, force_restart,
idle_sleep, attempt_reconnect, notify_tail, set_transcript_path, properties state,
stats, id, resume_handle, max_tokens, context_used_pct, get_context_info, and the
`_on_resume_handle` attribute (set by api.py:2875).

Reuse verbatim from TmuxSession: `_TmuxControl`; the state-machine wiring
(connect/cold-start/warm-wake/no-op, disconnect, force_restart, idle_sleep,
attempt_reconnect, tmux_session.py:1690–1949, 2419–2510, 5196–5470); the
inflight-deque/worker/watchdog (`_QueuedTurn`, `_InflightMeta`, `_message_worker`,
`_deliver_turn`, `_finish_turn_delivery`, `_handle_turn_complete`, `_inflight_watchdog`,
tmux_session.py:609–826, 4168–4859, 3469–3687); `TurnResponse` + cost/usage/analytics helpers.

Seams that differ (codex vs claude):
| Seam | TmuxSession | CodexTmuxSession |
|---|---|---|
| in-pane cmd | `_build_claude_cmd` (2214) | `_build_codex_repl_cmd` (new) |
| repl env | `_build_repl_env` (2311) ANTHROPIC_* | new — OPENAI_API_KEY, PINKY_* |
| transcript dir | `_project_dir` (4069) ~/.claude/projects/<slug> | scan ~/.codex/sessions/** filtered by session_meta.cwd |
| discovery | `_discover_transcript_path` (4137) | newest rollout-*.jsonl whose session_meta.cwd == working_dir |
| has-prior | `_has_prior_transcript` (4117) | any matching rollout exists |
| tailer | `TmuxTranscriptTailer` | `CodexTmuxTranscriptTailer` (new) |

Recommendation: **Option B for PR1** — standalone `CodexTmuxSession` importing
`_TmuxControl`/`_QueuedTurn`/`_InflightMeta`/`_ContextLockDeferral`/state-machine and
copying worker/handle-turn, parameterizing only the seams (zero risk to the
battle-hardened claude path). Follow-up refactor to a shared `_BaseTmuxSession`
(Option A) after codex tmux soaks.

## 2. Launch (`_build_codex_repl_cmd`)
Single shlex-quoted shell string for `tmux new-session`.
- Fresh: `codex --dangerously-bypass-approvals-and-sandbox --no-alt-screen -m <model> -C <cwd> -c model_reasoning_effort="<effort>" -c notify='["<notify-prog>","<agent>"]' [-c mcp_servers.<n>.url=... ...]`
- Resume: `codex resume --last --dangerously-bypass-approvals-and-sandbox --no-alt-screen ...` (same -c flags; omit -C on resume; verify -C/resume interaction per codex_session.py:558).
Flag decisions:
- `--no-alt-screen` strongly recommended (alt-screen TUI hostile to send-keys+tail; inline behaves closer to a line REPL — biggest risk-reducer).
- `--dangerously-bypass-approvals-and-sandbox` (reuse codex_session.py:526–544 rationale; do NOT combine with --full-auto).
- `-m <model>` from config.model; `-c model_reasoning_effort` mapping thinking_effort (reuse max→high, codex_session.py:563–568).
- MCP injection: reuse `-c mcp_servers.<n>.url=` / `.http_headers.<k>=` loop (codex_session.py:570–580); config built at api.py:2774–2800.
- `notify` → new `hooks/hook_codex_notify.py` POSTing to /agents/{name}/transport/wake (low-latency wake, no polling). `-c` parses value as TOML — notify array must be valid TOML.
Env (`_build_codex_repl_env`): tmux drops parent env → pass via new_session(env=): OPENAI_API_KEY (config.provider_key or daemon env, codex_session.py:134), PATH (codex_app_server_tmux.py:108), PINKY_AGENT_NAME/PINKY_AGENT_KEY/PINKY_SESSION_SECRET (fail-closed, tmux_session.py:2375–2417, for the notify hook's signed POST). cwd: mkdir parents then new_session(cwd=).

## 3. Input (send-keys / paste)
Reuse `_deliver_turn` → `_tmux.paste_text(prompt, enter=True)` (tmux_session.py:5084;
paste_text already uses bracketed-paste + delayed-Enter and "uses 4000 for codex").
- Submit: codex inline TUI submits on Enter; delayed-Enter after bracketed-paste should submit. VERIFY Enter vs C-m, and whether paste auto-submits or parks (PR2).
- Cold-start NUX/banner: codex has first-run model-availability NUX + banner. No SessionStart-equivalent to open a readiness gate → open the gate when the tailer first sees session_meta/task_started, OR a fixed settle delay before first paste. Codex FIFO-queues pasted input, so eager pastes buffer, not lost.
- Drop the native-/effort ultracode keystroke block (tmux_session.py:5017–5068) — effort set via -c at launch.
- Interrupt (Esc/Ctrl-C) not needed v1.

## 4. Output — new `src/pinky_daemon/codex_tmux_transcript.py` (`CodexTmuxTranscriptTailer`)
Clone `TmuxTranscriptTailer` structure (offset-tracking, partial-line handling,
_MAX_READ_CHUNK_BYTES, wake()/poll hybrid, set_transcript_path, set_offset, self-heal
path_discovery, stats). Replace per-entry parsing:
- task_started → mark turn active (started_at, turn_id).
- agent_message → accumulate payload.message (streamed). (optionally reasoning → thinking.)
- token_count w/ non-null info → snapshot info.last_token_usage → TurnResponse.usage.
- task_complete → TURN END. Prefer payload.last_agent_message (fallback to accumulated chunks); duration_ms from payload (codex provides directly). Drain → TurnResponse(text, thinking, usage, model=config.model, duration_ms, assistant_entry_count).
- turn_aborted → also close the turn (empty/partial + flag), so inflight head resolves (analog handle_stop_failure tmux_session.py:3688).
- else (response_item/*, turn_context, compacted, session_meta) ignored for response; session_meta.cwd used by discovery.
Locating the session file:
- Codex does NOT slug cwd into path. Discovery: glob ~/.codex/sessions/**/rollout-*.jsonl, sort mtime desc, read line 1, pick newest whose session_meta.payload.cwd == resolve(working_dir). Bound the scan (newest-K only).
- Better primary path: notify hook receives cwd + thread-id → POST thread-id to /agents/{name}/transport/transcript-path (api.py:5617); thread-id uuid == rollout filename uuid == session_meta.id → set_transcript_path binds exactly like claude SessionStart hook. mtime+cwd glob is the self-heal fallback.
- resume --last likely forks a NEW rollout (verify PR2). If so, discovery-by-mtime+cwd + seek-to-0 correct; capture resume UUID from new rollout session_meta.id (or notify thread-id).
Tailing risk: LOW — plain append-only JSONL ending each turn on task_complete, ~identical to claude. Only nuance: usage in separate token_count line BEFORE task_complete → retain last non-null token_count.info and attach at drain.

## 5. Dispatch (api.py `_start_streaming_session`, 2663–2926; no broker.py change — duck-typed)
1. Import CodexTmuxSession (2670 region).
2. Relax transport gate (2756–2762): allow (claude_sdk,sdk),(claude_sdk,tmux),(codex_cli,sdk),(codex_cli,tmux); reject else (keep transport in {sdk,tmux} + runtime checks).
3. Flags (2767–2768): is_codex_tmux = codex_cli & tmux; is_codex = codex_cli & sdk; is_tmux unchanged.
4. MCP config (2774–2800): gate on (is_codex or is_codex_tmux).
5. SessionClass (2849–2855): if is_codex_tmux → CodexTmuxSession; elif is_tmux → TmuxSession; elif is_codex → CodexSession; else StreamingSession.
6. init_kwargs (2857–2872): same as TmuxSession; add is_codex_tmux to stream_event_callback condition (2871); NO auth_*_callback (codex has no auth-failed signal).
7. provider stamp (2926): "codex_tmux".
8. resolved_provider_url="codex_cli" for (is_codex or is_codex_tmux) (2770–2771).
9. _on_resume_handle (2875) unchanged; tailer fires _pending_resume_handle_update on session_meta.id/notify thread-id (mirror codex_session.py:348–355).
transport/wake (5488) + transport/transcript-path (5617) already duck-typed (getattr notify_tail/set_transcript_path, 5513/5684) → reused, no endpoint change.

## 6. State machine / resume / restart / idle-sleep / cost
- State machine: reuse TmuxSession StateMachine matrix verbatim (better than CodexSession derived-bool).
- Resume handle: durable = tmux session name `pinky-codex-<agent>` (distinct from claude `pinky-<agent>` and app-server `pinky-codex-as-<agent>`). Codex session UUID captured separately for diagnostics + resume --last continuity.
- Restart survival: detached tmux + resume --last survive daemon restart like claude --continue; cold-start reaps stale pinky-codex-<agent> then relaunches resume --last.
- Idle-sleep: reuse TmuxSession.idle_sleep (5311); save-prompt text reuse CodexSession (codex_session.py:1411–1421).
- Cost: subscription (not USD-metered) like claude tmux; reuse _record_turn_usage + _log_turn_cost_and_analytics (3090–3224); cost_usd 0/absent; surface token_count.rate_limits (plan + window %) in stats for dashboard.

## 7. Tests (mirror tests/test_tmux_session.py + test_tmux_transcript.py)
- test_codex_tmux_transcript.py (highest value, pure tailer): redacted real murzik rollout fixture → on_turn_complete once per task_complete, text==last_agent_message, usage from preceding token_count, duration_ms; multi-turn FIFO; partial trailing line; token_count info=null ignored; turn_aborted closes; cold-start seek-EOF vs first-bind seek-0; path_discovery cwd-filtered selection.
- test_codex_tmux_session.py (mock _TmuxControl): _build_codex_repl_cmd fresh vs resume (flags/quoting/--no-alt-screen/bypass); _build_codex_repl_env (keys/PATH/PINKY_*/fail-closed); connect cold-start BOOT→CONNECTED w/ fake tailer; _deliver_turn→paste_text; _handle_turn_complete via inflight deque; state transitions; force_restart/idle_sleep/disconnect reap tmux; discovery picks codex rollout by cwd.
- api dispatch: (codex_cli,tmux)→CodexTmuxSession; (codex_cli,sdk)→CodexSession; (claude_sdk,tmux)→TmuxSession; invalid→400; MCP injection for codex tmux; no auth callbacks.
- Integration (gated, opt-in like app-server soak): real codex --no-alt-screen under real tmux in throwaway cwd, paste trivial prompt, assert rollout task_complete tailed → TurnResponse. THE make-or-break smoke test; run before merging PR2.

## 8. Open questions / risks
1. **send-keys into codex TUI (make-or-break)** — even --no-alt-screen, composer may treat newlines/bracketed-paste/Enter differently. Mitigation: gated integration smoke (PR2); fallback send_literal + explicit send_keys Enter, or tune paste chunk/delay. Validate FIRST.
2. resume --last reopen vs fork rollout? Affects discovery+seek. Verify PR2.
3. No SessionStart-equivalent before first turn — open readiness gate on first session_meta/task_started, or fixed settle delay. OK because codex FIFO-queues input.
4. notify reliability + signing — runs as codex subprocess in pane; must reach PINKY_DAEMON_URL + sign w/ keys. Fallback poll (2s) still progresses. Confirm exact -c notify TOML quoting.
5. Rollout scan cost — ~/.codex/sessions accumulates thousands; bound scan (newest-K, line 1 only); notify-driven set_transcript_path primary.
6. Distinct tmux name `pinky-codex-<agent>` (avoid collisions).
7. Model/usage attribution: token_count has tokens not model string → use config.model; cost $0.
8. Container agents out of scope PR1 (gate codex tmux to local agents initially).

## 9. Effort & PR breakdown (~3–5 focused days; dominated by send-keys/tail validation)
- **PR1 — transcript reader + dispatch plumbing (~1d, low risk, additive).** New codex_tmux_transcript.py + unit tests vs real rollout fixtures; api.py dispatch edits (§5) behind gates; no session class yet (nothing routes to it). Lands riskiest parsing with cheapest tests.
- **PR2 — CodexTmuxSession + integration smoke (~2d, the crux).** New codex_tmux_session.py (Option B); wire into api.py SessionClass; add hooks/hook_codex_notify.py; gated real-codex integration test (paste→tail round-trip). Proves feasibility.
- **PR3 — hardening + parity (~1d).** readiness-gate-from-tailer, turn_aborted, idle-sleep save-prompt, resume --last continuity + resume-UUID capture, stats parity, restart-survival test. Optional: refactor onto shared _BaseTmuxSession (Option A) after soak.

## Critical files
- src/pinky_daemon/tmux_session.py
- src/pinky_daemon/tmux_transcript.py
- src/pinky_daemon/codex_session.py
- src/pinky_daemon/api.py
- src/pinky_daemon/codex_app_server_tmux.py (reference: tmux-under-codex + _TmuxControl reuse)
New: src/pinky_daemon/codex_tmux_session.py, src/pinky_daemon/codex_tmux_transcript.py, codex notify hook (model on data/agents/<name>/.claude/hook_tmux_session_start.py).

## 10. PR2 BUILD RECORD (2026-06-17) — Option A chosen; make-or-break validated

**Decision: Option A (subclass), NOT the tentatively-planned Option B (copy).**
Recon showed the codex seams are cleanly isolated, so `CodexTmuxSession(TmuxSession)`
overrides ONLY `_build_session_name`, `_build_claude_cmd` (codex cmd), `_build_repl_env`,
`_project_dir`/`_has_prior_transcript`/`_discover_transcript_path`, `_start_tailer`
(→ `CodexTmuxTranscriptTailer`), `_spawn_tmux_repl` (wraps super() + codex trust pre-seed
+ NUX dismissal + readiness), `_watch_for_oauth_url` (no-op), and injects a
`_CodexTmuxControl` (paste settle 4000ms vs claude's 300ms — codex composer renders
slower). ~330 lines vs ~2500 for a copy; inherits the battle-hardened state machine /
worker / inflight watchdog / delivery + readiness gate / analytics / restart-survival
verbatim (zero divergence risk). The plan's Option-B caution was right for PR1 (tailer-only)
but is superseded now that the seams are known.

**Make-or-break (send-keys into codex TUI) VALIDATED LIVE 2026-06-17:** bracketed-paste
→ Enter drives a real codex 0.125.0 TUI to run a turn; rollout written with discoverable
`session_meta.cwd`; the merged tailer parsed `task_complete` → TurnResponse(text="PONG",
usage, duration). Plain `send-keys` does NOT submit — bracketed paste is required (paste_text
already does it; `_CodexTmuxControl` just lengthens the settle to 4000ms).

**Two cold-start NUX blockers found + handled** (the plan's §3/#3 was under-specified):
1. Update-available prompt — dismissed via send-keys (Down→Enter to "Skip", never "Update now").
2. Per-directory TRUST prompt — fires even WITH `--dangerously-bypass-approvals-and-sandbox`,
   and is NOT inherited from a trusted parent → `_seed_codex_trust` pre-writes
   `[projects."<cwd>"] trust_level="trusted"` to `config.toml` before launch (idempotent);
   `_codex_dismiss_nux_and_ready` is the send-keys backstop. Readiness gate (`_session_ready_event`)
   is opened when the composer renders (codex has no SessionStart hook, and the rollout only
   appears AFTER the first paste — so readiness can't be transcript-gated; codex FIFO-queues
   input so an early paste buffers).

**Dispatch (api.py §5):** relaxed the transport gate (all 4 runtime×transport combos valid);
added `is_codex_tmux` (selects `CodexTmuxSession` + "codex_tmux" provider stamp); `is_codex`
stays True for both codex transports so MCP injection + no-auth-callback/stream-callback init
already cover codex tmux. Extended the `/transport/transcript-path` `allowed_roots` to include
`$CODEX_HOME/sessions` (else the rollout path 403s). `/transport/wake` (`notify_tail`) is
duck-typed → inherited, no change.

**Turn-done detection** rides the polling tailer (active cadence once a turn starts), so the
low-latency `notify` hook is DEFERRED to PR3 — not required for correctness.

**Tests:** `tests/test_codex_tmux_session.py` (20 unit tests on the seams) + a gated
`PINKY_CODEX_TMUX_SMOKE=1` integration smoke (real codex+tmux round-trip, the make-or-break,
codifies the manual proof). Full repo suite stays green.

**Deferred to PR3:** notify low-latency wake hook (`hooks/hook_codex_notify.py`), idle-sleep
save-prompt text, resume-UUID-capture diagnostics, resume --last reopen-vs-fork verification,
container-agent codex tmux.
