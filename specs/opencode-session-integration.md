# OpencodeSession Integration Design

Status: draft spec for review
Scope: design only, no implementation in this branch
Primary goal: add `opencode` as a third PinkyBot agent runtime alongside Claude SDK `StreamingSession` and Codex CLI `CodexSession`

## Context

PinkyBot currently has two live agent runtimes:

- `StreamingSession` in `src/pinky_daemon/streaming_session.py`, backed by the Claude Agent SDK.
- `CodexSession` in `src/pinky_daemon/codex_session.py`, backed by `codex exec --json`.

Both are registered in the broker through `broker.register_streaming(agent_name, session, label=...)` and are expected to expose the same public surface:

- `connect()`
- `send(prompt, platform="", chat_id="", message_id="", agent_hint="")`
- `disconnect()`
- `force_restart()`
- `idle_sleep()`
- `attempt_reconnect()` where practical
- `is_connected`
- `is_idle_sleeping` or equivalent
- `session_id`
- `id`
- `stats`
- `get_context_info()`

The current runtime selection is overloaded onto provider config: `_start_streaming_session()` uses `provider_url == "codex_cli"` to select `CodexSession`, otherwise it uses `StreamingSession`. That was workable for one alternate runtime, but `opencode` needs provider selection inside the runtime, so continuing to encode runtime choice in `provider_url` would make configuration ambiguous.

This spec uses the opencode architecture facts supplied by Barsik, plus a
verification pass against the official server/config docs and a short local
`opencode serve` smoke run.

Verification notes:

- `bunx opencode-ai --version` resolved v1.14.32 locally.
- `opencode serve` on `127.0.0.1:4196` with `OPENCODE_SERVER_PASSWORD` returned `/global/health` as `{"healthy":true,"version":"1.14.32"}`.
- `/global/event` emitted `server.connected` SSE events.
- The official server docs say `POST /session/:id/message` waits for completion and returns the response, while `POST /session/:id/message_async` queues a prompt and does not wait for completion.
- The generated SDK/OpenAPI types include `cost` and `tokens` on assistant message and step-finish shapes.
- The config docs support secret substitution from env/file values, and provider config supports `options.apiKey`.

## Recommendation

Add `OpencodeSession` as a polymorphic runtime backed by a shared `opencode serve` process. Add an explicit agent `runtime` field with values:

- `claude_sdk`
- `codex_cli`
- `opencode`

Keep provider/model selection separate from runtime selection. For opencode agents, PinkyBot stores the selected opencode model string, such as `deepseek/deepseek-v4-pro`, in existing model/provider fields or a normalized provider config, then renders that into generated opencode config.

Do not auto-migrate existing agents. Opencode should coexist until it proves stable under real workloads.

## 1. `OpencodeSession` Class Shape

Create:

```text
src/pinky_daemon/opencode_session.py
```

The class should mirror `StreamingSession` and `CodexSession` closely enough that `MessageBroker`, scheduler, status endpoints, restart endpoints, and web chat keep treating sessions polymorphically.

Constructor:

```python
class OpencodeSession:
    def __init__(
        self,
        config: StreamingSessionConfig,
        *,
        response_callback=None,
        conversation_store=None,
        cost_callback=None,
        stream_event_callback=None,
        analytics_store=None,
        registry=None,
        server_manager=None,
    ) -> None:
        ...
```

Public fields/properties:

- `agent_name`
- `resume_handle`: opencode resume handle persisted via the existing `_on_resume_handle` callback.
- `created_at`
- `last_active`
- `usage: SessionUsage`
- `account_info = {"apiProvider": "opencode"}`
- `is_connected`
- `is_idle_sleeping`
- `id`: keep `f"{agent_name}-{label}"`, matching existing session ids.
- `stats`: include `connected`, `idle_sleeping`, `processing`, `pending_messages`, `current_activity`, `activity_log`, `cost_usd`, `account`, `thinking_effort`.
- `get_context_info()`: best effort initially, same estimated-token fallback as `CodexSession` unless opencode exposes richer usage.

Public methods:

- `connect()`: ensure the shared opencode server is healthy, create or resume the opencode session, start queue worker, attach to the global event stream through the manager, then enqueue the wake prompt.
- `send(prompt, platform="", chat_id="", message_id="", agent_hint="")`: same semantics as `CodexSession.send()`. It must accept `agent_hint`, append it only to the runtime prompt, and store the raw prompt in conversation history.
- `disconnect()`: stop local worker/subscriptions. It should not stop the shared opencode server unless this is daemon shutdown and the manager owns the process.
- `attempt_reconnect()`: reconnect to the server and event stream with the existing bounded backoff shape from `StreamingSession`.
- `force_restart()`: apply the restart guard, clear `resume_handle`, clear persisted handle via `_on_resume_handle`, create a fresh opencode session, and enqueue wake context.
- `idle_sleep()`: ask the agent to persist state, disconnect this session, preserve `resume_handle`, and mark idle sleeping.

Internal structure:

- `_message_queue: asyncio.Queue[tuple[str, str, str, str]]`
- `_worker_task`
- `_event_subscription`
- `_processing`
- `_pending_chats` or a per-turn routing map if opencode can run overlapping turns.
- `context_estimator`, using a shared helper rather than copying `CodexSession`'s private `_internal_context_texts` implementation.

The MVP should process prompts sequentially per PinkyBot session. That matches `CodexSession` and avoids ordering bugs while opencode event semantics are still new.

Add a shared context helper before or with `OpencodeSession`:

```python
class ContextTextEstimator:
    def record_internal_text(self, text: str) -> None: ...
    def estimated_tokens(self, session_id: str, conversation_store) -> int: ...
    def context_info(self, session_id: str, conversation_store, max_tokens: int) -> dict: ...
```

Then refactor `CodexSession` to use the helper instead of maintaining its own
private implementation. `OpencodeSession` should not become a third copy of the
same estimation code.

Send semantics are now resolved by docs: use `POST /session/:id/message` for
the sequential worker path because it waits for completion and returns the
response. Use `/global/event` for incremental UI activity and tool/status
streaming. Do not use `/message_async` for the MVP broker path unless we later
need fully detached turns.

## 2. Process Lifecycle

Recommendation: shared server manager, not one process per agent.

However, make it a server pool keyed by opencode runtime root, not a single hard-coded global forever:

```text
OpencodeServerManager
  key: (working_dir/config_root, port/password)
  value: running opencode serve process + HTTP client + SSE dispatcher
```

For the current PinkyBot deployment, this will normally be one `opencode serve` process shared by all opencode agents, with one opencode `session_id` per PinkyBot streaming session label.

Why not one process per agent:

- More memory and process churn.
- More port management.
- Harder daemon startup/shutdown.
- Duplicates opencode's own session persistence.

Why not one unconditional global:

- PinkyBot agents can have different `working_dir` values.
- opencode config and file access are likely scoped to the directory where `opencode serve` starts.
- A single global process could accidentally collapse separate project boundaries.

MVP lifecycle:

1. `create_api()` initializes `OpencodeServerManager`.
2. On first opencode agent start, manager checks `/global/health`.
3. If no server is reachable and PinkyBot is configured to own it, manager spawns:

   ```text
   opencode serve --hostname 127.0.0.1 --port <port>
   ```

4. Manager waits for `/global/health`.
5. Manager lazily regenerates generated opencode config if the agent/config
   fingerprint is dirty.
6. Sessions call `POST /session` or reuse a persisted session id.
7. On daemon shutdown, manager terminates owned processes.

Operational mode should be configurable:

- `managed`: PinkyBot starts/stops opencode. Default for local installs.
- `external`: PinkyBot only connects to an existing opencode server.

Generated config rewrite policy:

- Store a fingerprint alongside each generated config containing agent runtime,
  model, prompt hash, permission mode, materialized MCP config hash, provider
  secret refs, and opencode manager version.
- Rewrite lazily on next session start when the fingerprint differs.
- Force-regenerate before `force_restart()`.
- Mark configs dirty on registry writes that affect runtime, model, soul,
  boundaries, directives, permission mode, working directory, provider refs, or
  skill/materialized MCP state.
- Directive updates should mark dirty through the directive write path, even
  though the rewrite still happens lazily unless the agent is explicitly
  restarted.
- Permission changes are security-sensitive: for live opencode sessions,
  `permission_mode` writes should force-regenerate and require an immediate
  session restart to guarantee the new edit/bash policy applies. If the restart
  guard blocks, mark the session `restart_required` and surface a warning rather
  than silently continuing under stale permissions.

## 3. Auth Model

Use both local bind and auth.

Default:

- Bind opencode to `127.0.0.1`.
- In managed mode, generate a new random password on every PinkyBot daemon
  restart. Do not persist it to disk or DB.
- Pass it to the child process via `OPENCODE_SERVER_PASSWORD`.
- Store it only in memory on `OpencodeServerManager`.
- Use HTTP Basic auth for all REST and SSE requests.
- Expose the current generated password through an auth-gated PinkyBot daemon
  diagnostics endpoint for admin/debug curl use cases. It should follow the
  same auth posture as existing local admin/status endpoints such as
  `/agents/{name}/streaming/status`, and should never be available without
  PinkyBot admin auth.

Do not run unauthenticated by default. Local-only without auth is convenient but brittle: localhost is still reachable by other local processes, browser extensions, and misconfigured reverse proxies. The overhead of random Basic auth is small and avoids a class of avoidable local privilege mistakes.

For external mode:

- Read password from a PinkyBot setting or environment variable, such as `PINKY_OPENCODE_SERVER_PASSWORD`.
- Never persist generated passwords to the agent DB.

Decision trail: Brad chose regenerate-on-restart for managed mode on 2026-05-02.

## 4. Streaming Protocol Mapping

opencode exposes `/global/event` as SSE. PinkyBot already exposes its own UI SSE endpoint at:

```text
GET /agents/{agent_name}/streaming/events
```

The mapping should be:

```text
opencode /global/event
  -> OpencodeServerManager single SSE reader
  -> dispatch by opencode session_id
  -> OpencodeSession._handle_event()
  -> stream_event_callback()
  -> PinkyBot /agents/{name}/streaming/events subscribers
```

The manager should own one SSE connection per server process, not one SSE connection per PinkyBot agent. It should parse events, filter/route by `session_id`, and send heartbeat/liveness updates to registered `OpencodeSession` objects.

Event categories to normalize:

- assistant text delta or completed assistant text -> `assistant_delta`
- tool started/completed -> `tool_use`
- command/file activity -> current activity/status labels
- usage/cost if available -> `StreamingTurnResult.model_usage`
- errors -> `turn_error` or `turn_failed`
- session created/resumed -> `_on_resume_handle(agent_name, resume_handle)`

For broker responses, `OpencodeSession` should produce the same final `StreamingTurnResult` used by the current response callback. If opencode's `POST /session/:id/message` returns the completed assistant message, use that as the authoritative final response and use SSE for incremental UI only. If the message endpoint is fire-and-stream, accumulate final text from events and resolve the queued turn on the matching completion event.

Heartbeat/resurrection fit:

- PR #339-style resurrection still fits if `OpencodeSession.is_connected` reflects both HTTP health and SSE reader health.
- `attempt_reconnect()` should reconnect the HTTP client/SSE subscription, then verify `/global/health`.
- The watchdog must not restart a deliberately idle-sleeping session, same as `StreamingSession.is_idle_sleeping`.
- If the server process is down and PinkyBot owns it, `attempt_reconnect()` should ask `OpencodeServerManager` to restart the server before reattaching sessions.

Important edge case:

- A healthy shared server can coexist with one wedged opencode session. Recovery should first recreate the affected opencode session, not restart the whole server. Restarting the server should be reserved for failed `/global/health` or broken global SSE.

## 5. Agent Registry Changes

Recommendation: add an explicit `runtime` column to `agents`.

Schema:

```sql
ALTER TABLE agents ADD COLUMN runtime TEXT NOT NULL DEFAULT 'claude_sdk';
```

Allowed values:

- `claude_sdk`
- `codex_cli`
- `opencode`

Migration behavior:

- Existing rows default to `claude_sdk`.
- Run a one-shot boot-time data backfill: if an existing row has
  `provider_url = 'codex_cli'`, set `runtime = 'codex_cli'`.
- After that backfill, runtime selection reads only `agents.runtime`.
- Leave `provider_url` stored for compatibility/debugging during the rollout,
  but do not continue using it as a runtime selector.
- Remove any legacy `provider_url == 'codex_cli'` runtime shim after the rollout
  window, target two weeks after deployment if no rollback is active.

Update:

- `Agent` dataclass
- `Agent.to_dict()`
- `_AGENT_COLUMNS`
- `AgentRegistry.register()`
- API create/update payloads
- frontend settings if the UI manages runtime/provider selection

After the one-shot backfill has run, runtime selection in
`_start_streaming_session()` becomes:

```python
runtime = agent.runtime or "claude_sdk"
if runtime == "codex_cli":
    SessionClass = CodexSession
elif runtime == "opencode":
    SessionClass = OpencodeSession
else:
    SessionClass = StreamingSession
```

During the short rollout window only, a compatibility shim can protect DBs that
have not yet run the one-shot backfill:

```python
def runtime_from_legacy_provider(agent):
    if agent.provider_url == "codex_cli":
        return "codex_cli"
    return "claude_sdk"
```

Do not introduce opencode as another sentinel `provider_url` value.
`provider_url` should mean provider endpoint, not runtime.

Rollback path:

- UI: expose a runtime selector that can switch an agent from `opencode` back to
  `claude_sdk`, then restart the streaming session.
- API/SQL fallback:

  ```sql
  UPDATE agents SET runtime='claude_sdk', updated_at=? WHERE name=?;
  ```

- On rollback, disconnect the live opencode session, leave the opencode
  `session_id` persisted only for audit/debugging if needed, and start a fresh
  Claude SDK session with normal wake context.

## 6. Config Injection

Use PinkyBot's existing system prompt builder as the source of truth.

For an opencode agent, generate an opencode config block from PinkyBot agent state:

```json
{
  "agent": {
    "barsik": {
      "description": "Barsik - Personal AI Sidekick",
      "mode": "primary",
      "model": "deepseek/deepseek-v4-pro",
      "prompt": "<agents.build_system_prompt(...)>",
      "permission": {
        "edit": "allow",
        "bash": "allow"
      }
    }
  }
}
```

Mapping:

- `soul`, boundaries, users, active directives, skill catalog, and generated messaging instructions still flow through `agents.build_system_prompt(agent_name, skill_store=skills)`.
- `wake_context` is not baked into opencode config. It should be sent as the first prompt on `connect()`, matching `StreamingSession` and `CodexSession`.
- Permission mapping should be conservative:
  - Pinky `bypassPermissions` or `auto` -> opencode `allow` for edit/bash.
  - Pinky `default`, `acceptEdits`, or unknown -> opencode `ask` initially.
  - Pinky `plan` -> opencode `deny` for edit/bash.
- Runtime permission changes require config regeneration and session restart, as
  described in the lifecycle section. Do not assume an in-place config reload is
  sufficient for active opencode turns until verified in implementation.

MCP config:

- Prefer opencode native MCP support where possible.
- Generate MCP entries from the same `skills.materialize_for_agent()` result used today.
- In shared MCP mode, pass Pinky shared MCP HTTP endpoints with `X-Agent-Name`.
- Technical verification: generated opencode types for remote MCP config include
  `headers`, so Pinky shared MCP identity headers can be represented in config.
- Even with header support, the first implementation should prefer generated
  config roots over mutating a user's hand-written `opencode.json`.

Config file ownership:

- PinkyBot should generate runtime config into a Pinky-owned cache path, not mutate the user's hand-written `opencode.json` directly.
- Suggested path:

  ```text
  data/opencode/{server_key}/opencode.json
  ```

- Include a header/comment-equivalent in adjacent metadata that says it is generated.

Config regeneration triggers are intentionally centralized in
`OpencodeServerManager`. Individual registry write paths should mark agent config
dirty; the manager decides whether to rewrite immediately or lazily. This avoids
duplicating opencode config rendering across agent CRUD, directive writes, skill
assignment, and session startup.

## 7. Provider/Model Config Flow

User-facing model selection should stay in PinkyBot.

Recommended fields:

- `agents.runtime = 'opencode'`
- `agents.model = 'deepseek/deepseek-v4-pro'` or a selected row from `models`
- `providers` table can still store display/provider presets, but for opencode the effective output is an opencode model id string.

Do not make users edit generated `opencode.json`.

Flow:

1. User selects runtime: `opencode`.
2. UI lists opencode-compatible provider/model ids. This can initially be manually seeded in PinkyBot's `models` table, then later refreshed from opencode/models.dev if desired.
3. User selects `DeepSeek V4-Pro`.
4. Pinky stores the chosen model id in `agent.model` or `provider_model`.
5. `_start_streaming_session()` passes `effective_model` to `OpencodeSession`.
6. `OpencodeServerManager` regenerates config if agent prompt/model/permissions/MCP config changed.

Secrets:

- Phase 1 provider secrets must use scoped environment variables only.
- Technical verification: opencode config supports secret substitution from env
  and file values, and provider config supports `options.apiKey`.
- Generated opencode config should reference environment variables rather than
  embedding raw provider keys or file substitutions.
- The manager should launch opencode with a scoped environment containing only
  provider keys required by opencode agents attached to that server root.
- opencode also exposes `/auth/{providerID}` for setting auth credentials; keep
  that as a flagged future path, not a Phase 1 path.
- Do not write provider keys into generated config files in Phase 1.

Decision trail: Brad chose scoped env vars for Phase 1 on 2026-05-02.

## 8. Opencode Runtime Dependency

Treat opencode as an optional runtime dependency, not a hard PinkyBot install
requirement for agents that do not use the opencode runtime.

Install detection:

- `opencode --version`
- `PINKY_OPENCODE_CMD` can override the executable path for dev/test.

Mac Mini / production deploy path:

- Install with npm, pinned to a deliberate version:

  ```text
  npm install -g opencode-ai@<pinned-version>
  ```

- Pin the version in PinkyBot deploy config alongside other runtime/SDK pins.
- Bump deliberately through `update_and_restart`/deploy changes, not silently.
- Expected result: `opencode` is on `PATH`.
- `PINKY_OPENCODE_CMD` remains available for development and test overrides.

Linux/Pi/Mini:

- Use the same npm-pinned install path where Node/npm is available.
- If npm install is unavailable on a target, keep opencode disabled with a clear
  health error until Brad approves an alternate path:

  ```text
  Opencode runtime unavailable: opencode executable not found. Install opencode or configure PINKY_OPENCODE_CMD.
  ```

Non-Phase-1 alternatives:

- Homebrew is not the Mac Mini path because the available formula was stale
  during review.
- `bunx` is useful for local verification but not the production deploy path;
  Brad does not want Bun as a platform dependency when Node is already present.
- curl-bash install paths are out for Phase 1 because they fight reproducibility
  and version pinning.

Configuration:

- `PINKY_OPENCODE_CMD`: override executable path.
- `PINKY_OPENCODE_MODE=managed|external`.
- `PINKY_OPENCODE_URL`: external server URL.
- `PINKY_OPENCODE_SERVER_PASSWORD`: external password.

Rollout should not break existing Claude/Codex installs when opencode is missing.

Decision trail: Brad chose npm global install with pinned version for the Mac
Mini deployment path on 2026-05-02.

## 9. Test Plan

Unit tests:

- REST client builds Basic auth headers.
- REST client handles `/global/health` healthy/unhealthy.
- Session creation persists returned `resume_handle` through `_on_resume_handle`.
- `send()` accepts `agent_hint` and appends it only to the runtime prompt.
- `send()` uses the wait-for-completion message endpoint for MVP.
- `send()` stores raw prompt in `ConversationStore`.
- SSE dispatcher routes events by opencode `session_id`.
- Event normalization maps assistant/tool/error/completion events into PinkyBot stream events.
- Cost/tokens are extracted from assistant message or step-finish data when present.
- `force_restart()` clears persisted session id and creates a new opencode session.
- `idle_sleep()` preserves session id and sets idle sleeping.
- `attempt_reconnect()` restarts/reconnects via manager when health/SSE fails.
- Runtime selection chooses `OpencodeSession` only when `agent.runtime == "opencode"`.
- Boot-time backfill maps legacy `provider_url == "codex_cli"` to `runtime = "codex_cli"`.

Integration tests:

- Fixture starts `opencode serve` on a random localhost port with a random password.
- Test creates a temporary agent config and session.
- Test sends a trivial prompt to `/session/:id/message`.
- Test consumes `/global/event` until assistant completion or timeout.
- Test verifies Pinky `StreamingTurnResult` callback receives final response.
- Test verifies `/agents/{name}/streaming/events` emits normalized events.

Smoke test:

```text
1. Start PinkyBot with PINKY_OPENCODE_MODE=managed.
2. Create agent `deepseek-test` with runtime=opencode and model=deepseek/deepseek-v4-pro.
3. Send "Reply with exactly pong."
4. Verify Telegram/web response is "pong" or provider-equivalent.
5. Kill opencode serve.
6. Verify watchdog recovery restarts/reconnects without losing the PinkyBot process.
```

CI gating:

- Unit tests always run.
- Main CI should include an opencode integration job rather than silently
  skipping forever. The job should install the pinned opencode version with npm
  or use a Docker fixture that contains that same pinned version, then launch
  `opencode serve` on a random localhost port.
- Local developer runs may skip integration tests if opencode or npm is missing,
  but `main` should exercise the real server path.
- If the team chooses not to run opencode integration in CI, document that as an
  accepted risk in the PR and keep a manual smoke checklist mandatory before
  enabling `PINKY_ENABLE_OPENCODE` in production.

## 10. Migration And Rollout

Phase 0: spec and review.

Phase 1: hidden backend runtime.

- Add `runtime` column.
- Add `OpencodeServerManager`.
- Add REST client.
- Add `OpencodeSession`.
- Add runtime selection behind feature flag:

  ```text
  PINKY_ENABLE_OPENCODE=1
  ```

- No UI by default.

Phase 2: local dogfood.

- Enable one test agent.
- Keep Claude/Codex agents unchanged.
- Track reliability:
  - send latency
  - turn completion rate
  - SSE reconnects
  - watchdog recoveries
  - tool-call success
  - reported cost/tokens coverage

Circuit breaker:

- Track opencode runtime errors per agent and globally:
  - failed sends
  - reconnect attempts
  - `force_restart()` calls
  - watchdog recoveries
  - global SSE disconnects
- If an opencode agent exceeds 3 forced restarts in 1 hour, disable new sends to
  that opencode session, mark it unhealthy, and alert Brad through the existing
  owner notification path.
- If all opencode agents together exceed 10 forced restarts or watchdog
  recoveries in 1 hour, automatically flip the process-level opencode feature
  gate to disabled for new session starts until manual intervention.
- Circuit breaker rollback should not rewrite agent runtime fields. It should
  stop starting new opencode sessions and tell the operator to switch affected
  agents back to `claude_sdk` or `codex_cli`.

Cost visibility:

- Verified opencode types expose cost/tokens on assistant message and
  step-finish data, so the implementation should wire those into
  `StreamingTurnResult.total_cost_usd`, `model_usage`, and analytics.
- If a provider/event path does not return cost, do not silently report `$0`.
  Mark cost as unknown in session stats/analytics and surface a warning in
  diagnostics. `$0` is acceptable only when opencode explicitly reports zero.

Phase 3: UI support.

- Add runtime selector.
- Add opencode model selector.
- Show opencode server health in diagnostics.
- Make install/runtime errors actionable.

Phase 4: optional migration.

- Do not auto-migrate agents.
- Offer manual clone/migrate:

  ```text
  Clone agent as opencode runtime
  ```

- Keep `codex_session.py` and `streaming_session.py` indefinitely until real usage shows opencode can replace them.

## Open Questions

1. What is the exact generated config root strategy for multiple PinkyBot
   `working_dir` values: one opencode server per root, or a shared server with
   per-session directory metadata? The spec assumes a manager pool keyed by
   runtime root until implementation verifies opencode's directory isolation.
2. Does opencode apply regenerated agent config to an existing session in place,
   or is a fresh opencode session always required? The spec assumes restart for
   permission changes until proven otherwise.
3. Which exact opencode event names should be treated as terminal for a
   completed assistant turn? Generated types show `session.idle` and
   message/part events, but implementation should confirm the most reliable
   completion signal from `/doc` or a fixture trace.
4. How should opencode slash commands map to PinkyBot admin actions, if at all? Initial implementation should keep them internal to opencode.

## Non-Goals

- Replacing existing Claude SDK or Codex runtime in the first implementation.
- Reworking PinkyBot's memory model.
- Moving provider secrets into generated opencode config unless unavoidable.
- UI polish beyond basic runtime/model selection during initial backend integration.
- Any implementation in this spec branch.

## References

- Official server docs: <https://opencode.ai/docs/server/>
- Official config docs: <https://opencode.ai/docs/config/>
- Official agents docs: <https://opencode.ai/docs/agents/>
- Generated SDK types checked during verification:
  <https://raw.githubusercontent.com/anomalyco/opencode/dev/packages/sdk/js/src/gen/types.gen.ts>
