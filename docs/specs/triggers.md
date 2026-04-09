# Pinky Triggers — Product & Technical Spec

**Status:** Draft  
**Target:** PinkyBot daemon v1.x  
**Database:** `data/pinky.db` (existing SQLite, WAL mode)

---

## 1. Overview

### What this is

Pinky Triggers adds event-driven waking to PinkyBot. Today, agents wake on cron schedules — at fixed times, regardless of what's happening in the world. Triggers make agents reactive: they wake when something happens.

Three trigger types cover the main event categories:

| Type | Direction | What fires it |
|------|-----------|---------------|
| **Webhook** | Inbound push | An external service POSTs to a secret URL |
| **URL Watcher** | Outbound poll | The daemon detects a change at a remote URL |
| **File Watcher** | Local watch | A file or glob changes on disk |

### Mental model

**Triggers point at agents, not roles.** An agent named `barsik` handles GitHub webhooks, watches status pages, and monitors config files. The same agent grows into those responsibilities over time through memory and experience. There are no throwaway specialists or disposable sub-agents — just one agent taking on more to watch over.

Every trigger is a condition → wake. When the condition becomes true, the daemon calls `wake_callback(agent_name, session_id, prompt)` — the same mechanism cron schedules use. From the agent's perspective, it just receives a message and works. The prompt carries full context about what fired.

**Agents self-assign triggers autonomously.** An agent can call `create_trigger()` via MCP during any session — "watch this URL, wake me when it changes." No human intervention needed. The agent discovers its own watch responsibilities and sets them up.

### Why it matters

- GitHub PR opens → Barsik summarizes it and drops it in Telegram
- A stock price crosses a threshold → agent files a note in long-term memory
- A config file changes on disk → agent runs validation and alerts Brad
- A deployment status page goes unhealthy → agent pings the on-call channel
- A competitor's blog RSS feed publishes → agent logs it and briefs Brad at morning wake

This is the difference between a scheduler and a reactive agent system.

---

## 2. Data Model

### 2.1 `triggers` table

Single table covering all three trigger types. Type-specific fields are nullable and only populated when relevant.

```sql
CREATE TABLE IF NOT EXISTS triggers (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_name        TEXT    NOT NULL,
    name              TEXT    NOT NULL DEFAULT '',
    trigger_type      TEXT    NOT NULL,              -- 'webhook' | 'url' | 'file'
    token             TEXT    UNIQUE,                -- webhook only, secrets.token_hex(16)
    url               TEXT    NOT NULL DEFAULT '',   -- url watcher only
    method            TEXT    NOT NULL DEFAULT 'GET',-- url watcher only
    condition         TEXT    NOT NULL DEFAULT '',   -- see §2.2
    condition_value   TEXT    NOT NULL DEFAULT '',   -- JSON blob with condition params
    file_path         TEXT    NOT NULL DEFAULT '',   -- file watcher only
    interval_seconds  INTEGER NOT NULL DEFAULT 300,
    prompt_template   TEXT    NOT NULL DEFAULT '',   -- e.g. "DNS propagated: {{body.domain}}"
    enabled           INTEGER NOT NULL DEFAULT 1,
    last_fired_at     REAL    NOT NULL DEFAULT 0,
    last_checked_at   REAL    NOT NULL DEFAULT 0,
    last_value        TEXT    NOT NULL DEFAULT '',   -- last response/hash for change detection
    fire_count        INTEGER NOT NULL DEFAULT 0,
    created_at        REAL    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_triggers_agent      ON triggers(agent_name);
CREATE INDEX IF NOT EXISTS idx_triggers_token      ON triggers(token) WHERE token IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_triggers_type_enabled ON triggers(trigger_type, enabled);
```

### 2.2 Condition types for URL Watcher

| `condition` | `condition_value` | Fires when |
|-------------|-------------------|------------|
| `status_changed` | `""` | HTTP status code differs from last check |
| `status_is` | `"200"` (integer string) | Status equals the given code |
| `body_contains` | `"deploy complete"` | Response body contains the string (case-insensitive) |
| `json_field_equals` | `{"path": "data.status", "value": "done"}` | Dot-path field equals value |
| `json_field_changed` | `{"path": "data.version"}` | Dot-path field differs from last check |

### 2.3 Migration pattern

Follows the existing `_ensure_columns` / `_migrate()` convention in `agent_registry.py`. The `TriggerStore` has its own `_migrate()` method with the same pattern. Future column additions go there.

---

## 3. API Endpoints

All management endpoints require `X-API-Key` authentication (same as all other protected routes). The webhook receiver endpoint is **public** — it authenticates via the secret token in the URL path.

### 3.1 Trigger management

#### `GET /triggers`
List all triggers across all agents. Optional `?agent_name=` filter.

#### `GET /agents/{name}/triggers`
List all triggers for a specific agent.

#### `POST /agents/{name}/triggers`
Create a new trigger for an agent.

**Webhook:**
```json
{
  "name": "github-prs",
  "trigger_type": "webhook",
  "prompt_template": "New PR: {{body.pull_request.title}} by {{body.pull_request.user.login}}"
}
```
Response includes `token` (only time it is returned in full).

**URL Watcher:**
```json
{
  "name": "status-page",
  "trigger_type": "url",
  "url": "https://status.example.com/api/v2/status.json",
  "condition": "json_field_changed",
  "condition_value": "{\"path\": \"status.indicator\"}",
  "interval_seconds": 60,
  "prompt_template": "Status page changed: {{field_value}}"
}
```

**File Watcher:**
```json
{
  "name": "config-change",
  "trigger_type": "file",
  "file_path": "/Users/brad/projects/myapp/config/*.yaml",
  "prompt_template": "Config file changed: {{path}}"
}
```

#### `GET /agents/{name}/triggers/{id}`
Get a single trigger by ID.

#### `PUT /agents/{name}/triggers/{id}`
Update trigger fields. All fields optional. Token cannot be updated (rotate via rotate-token endpoint).

#### `DELETE /agents/{name}/triggers/{id}`
Delete trigger. For webhooks, the token is immediately invalidated.

#### `POST /agents/{name}/triggers/{id}/test`
Manually fire a trigger (for testing). Wakes the agent with the rendered prompt.

**Response:**
```json
{
  "fired": true,
  "prompt": "Webhook trigger 'github-prs' fired at ...",
  "agent_woken": true
}
```

#### `POST /agents/{name}/triggers/{id}/rotate-token`
Webhook triggers only. Generates a new secret token. Old token immediately stops working.

**Response:**
```json
{"token": "new-hex-token..."}
```

### 3.2 Webhook receiver

#### `POST /hooks/{token}`

**Public endpoint — no API key required.** Mounted before auth middleware via `_public_prefixes`.

Accepts any content type. If `Content-Type: application/json`, body is parsed as JSON and available for template interpolation. Non-JSON bodies are passed through as `body_raw`.

**Path:** `token` — 32-character hex string.

**Response `200`:**
```json
{"ok": true, "trigger_id": 7, "agent": "barsik"}
```

**Response `404`:** Token not found or trigger disabled.

**Response `413`:** Body > 1MB.

**Response `429`:** Rate limit exceeded (60/min per token).

---

## 4. Scheduler Integration

### 4.1 URL Watcher poll loop

`_check_url_watchers(now)` runs on every scheduler tick (every 30s):

1. Queries `trigger_store.list_due_url_watchers(now)` — triggers whose `last_checked_at + interval_seconds <= now`.
2. For each trigger, fetches the URL (5s timeout via `urllib`).
3. Evaluates the condition against the response and `last_value`.
4. If condition is true: renders the prompt template, calls `wake_callback`, calls `record_fire(id)`.
5. Always: calls `record_check(id, new_value)` to update `last_checked_at` and `last_value`.
6. On HTTP error: logs, calls `record_check` with unchanged value (avoids hammering on error).

`AgentScheduler.__init__` accepts a `trigger_store` keyword argument injected from `api.py`.

### 4.2 File Watcher poll loop

Phase 3 — not yet implemented. Will follow the same pattern with glob expansion, mtime/hash comparison, and `difflib.unified_diff` for text files.

### 4.3 TriggerStore

`src/pinky_daemon/trigger_store.py` — owns all DB operations for the `triggers` table:

- `create(agent_name, name, trigger_type, **kwargs) -> Trigger`
- `get(id) -> Trigger | None`
- `get_by_token(token) -> Trigger | None` — only returns enabled triggers
- `list(agent_name=None, enabled_only=False) -> list[Trigger]`
- `update(id, **kwargs) -> Trigger | None`
- `delete(id) -> bool`
- `rotate_token(id) -> str | None` — webhook only
- `record_fire(id)` — increments fire_count, sets last_fired_at
- `record_check(id, last_value)` — updates last_checked_at and last_value
- `list_due_url_watchers(now) -> list[Trigger]` — triggers ready to poll

Token is never exposed in `to_dict()` by default. Pass `include_token=True` only on create and rotate-token responses.

---

## 5. Webhook Flow

```
External service
    │
    │  POST /hooks/a3f8b2c1d4e5f6a7b8c9d0e1f2a3b4c5
    │  Content-Type: application/json
    │  Body: {"action": "opened", "pull_request": {"title": "Fix auth bug"}}
    ▼
FastAPI route handler (auth middleware skipped — /hooks/ is in _public_prefixes)
    │
    ├─ 1. Check body size ≤ 1MB
    ├─ 2. Look up trigger by token (get_by_token — only returns enabled)
    ├─ 3. Rate limit check (60/min per token, in-memory sliding window)
    ├─ 4. Parse body (JSON if Content-Type: application/json, else raw string)
    ├─ 5. Render prompt template ({{body.field}} interpolation)
    ├─ 6. Call wake_callback(agent_name, "{agent_name}-main", prompt)
    ├─ 7. record_fire(trigger.id)
    └─ 8. Return {"ok": true, "trigger_id": ..., "agent": ...}
```

### Error handling

- Token not found or disabled: `404 {"detail": "not found"}`
- Body > 1MB: `413`
- Rate limit: `429`
- `wake_callback` failure: log error, still return `200` (webhook was received; agent problem is internal)

---

## 6. Prompt Templating

Templates use `{{expression}}` mustache-style interpolation with dot-path access into nested JSON.

**Available variables (webhook):**

| Variable | Value |
|----------|-------|
| `{{body.field.subfield}}` | Dot-path into parsed JSON body |
| `{{body_raw}}` | Raw body string |
| `{{trigger_name}}` | Trigger's name |
| `{{timestamp}}` | ISO 8601 UTC timestamp |

**Available variables (url watcher):**

| Variable | Value |
|----------|-------|
| `{{url}}` | The watched URL |
| `{{status}}` | HTTP status code |
| `{{body.field}}` | Dot-path into parsed JSON response |
| `{{body_raw}}` | Raw response body |
| `{{field_value}}` | Current value of watched JSON field |
| `{{field_previous}}` | Previous value (from last_value) |
| `{{trigger_name}}` | Trigger name |
| `{{timestamp}}` | ISO 8601 UTC timestamp |

If no template is set, a sensible default is generated from the trigger name and body.

---

## 7. Security

### Token generation

`secrets.token_hex(16)` — 128 bits of entropy, 32 hex characters. Same strength as GitHub webhook secrets.

Tokens are stored in plaintext (they are bearer credentials, like API keys — hashing adds no security when the daemon needs to look them up per-request). `UNIQUE` constraint prevents collisions.

Token is returned only:
- On `POST /agents/{name}/triggers` (create) for webhook type
- On `POST /agents/{name}/triggers/{id}/rotate-token`

All other list/get responses omit the token.

### Rate limiting

Per-token in-memory sliding window: 60 requests per minute. Applied in the webhook handler, not middleware.

### Body size limit

Reject bodies > 1MB with `413`. Checked before any processing.

### URL Watcher safety

- Only the daemon owner (via API key) can create URL watchers — no SSRF concern from external callers.
- Outbound requests use a 5-second timeout via `urllib`.
- Response body is capped at 64KB when read.
- `last_value` stored in DB is capped at 1KB.

---

## 8. Frontend

### 8.1 Triggers page

Add a **Triggers** tab to the agent detail page (alongside Schedules, Directives, etc.).

**Triggers list view** — for each trigger show:
- Name, type badge (`WEBHOOK` / `URL` / `FILE`)
- Enabled toggle (inline PUT)
- Last fired: relative time or "Never"
- Fire count
- For webhooks: webhook URL (click to copy)
- Actions: Test, Edit, Delete, Rotate Token (webhooks only)

**Create trigger modal** — stepped form:
1. Choose type (3 cards with icon + description)
2. Type-specific fields (rendered conditionally)
3. Prompt template with live preview

### 8.2 Components needed

| Component | Purpose |
|-----------|---------|
| `TriggerList.svelte` | Table/list of triggers for an agent |
| `TriggerCard.svelte` | Single trigger row with status + actions |
| `CreateTriggerModal.svelte` | Multi-step creation form |
| `EditTriggerModal.svelte` | Edit existing trigger |
| `WebhookUrlCopy.svelte` | URL display + copy button + regenerate |
| `ConditionPicker.svelte` | Dropdown + dynamic sub-fields for URL condition types |

### 8.3 API calls from frontend

All through the existing authenticated fetch wrapper:
```
GET    /agents/{name}/triggers
POST   /agents/{name}/triggers
GET    /agents/{name}/triggers/{id}
PUT    /agents/{name}/triggers/{id}
DELETE /agents/{name}/triggers/{id}
POST   /agents/{name}/triggers/{id}/test
POST   /agents/{name}/triggers/{id}/rotate-token
```

---

## 9. MCP Tools (pinky-self)

Agents can create and manage their own triggers autonomously. These tools are in `src/pinky_self/server.py`.

### 9.1 `create_trigger`

Creates a webhook or url-type trigger for this agent. Webhook type returns the full webhook URL to paste into external services. URL type sets up periodic polling.

### 9.2 `list_triggers`

Lists all triggers configured for this agent with status and fire counts.

### 9.3 `delete_trigger`

Deletes a trigger by ID. For webhooks, the token is immediately invalidated.

### 9.4 `test_trigger`

Manually fires a trigger to verify the prompt template and wake path are working.

---

## 10. Implementation Phases

### Phase 1 — Webhook receiver + URL watcher (DONE)

1. `src/pinky_daemon/trigger_store.py` — `TriggerStore` class with CRUD, token management, and state update methods
2. `src/pinky_daemon/api.py` — trigger management endpoints + public `/hooks/{token}` receiver
3. `src/pinky_daemon/scheduler.py` — `_check_url_watchers()` + `_poll_url_trigger()` added to `_tick()`
4. `src/pinky_self/server.py` — `create_trigger`, `list_triggers`, `delete_trigger`, `test_trigger` MCP tools

**Deliverable:** Any external service can POST to a URL and wake an agent. Agents can self-assign webhook and URL watcher triggers via MCP.

### Phase 2 — File Watcher (future)

1. Add `_check_file_watchers(now)` to `AgentScheduler`
2. Implement glob expansion + mtime/hash diff
3. Implement `difflib.unified_diff` for text files
4. Add `file` type to create trigger form and MCP tool

**Deliverable:** Agent wakes when config files or local data changes.

### Phase 3 — Frontend (future)

1. `TriggerList.svelte`, `TriggerCard.svelte`, `CreateTriggerModal.svelte`
2. Webhook URL copy button + rotate token UI
3. Test button with result toast
4. URL watcher: last status badge + last polled time

### Phase 4 — Polish (future)

1. Audit log entries for trigger fires
2. Hourly rate limit bucket (in addition to per-minute)
3. HMAC signature verification for GitHub-style webhooks
4. Pulsing green dot for recently-fired triggers in UI

---

## 11. Example Use Cases

### 1. GitHub PR reviewer

Barsik creates a webhook trigger. Brad pastes the webhook URL into GitHub's webhook settings. Every time a PR is opened, GitHub POSTs to the URL. Barsik wakes with the PR title, author, and diff URL, writes an analysis to memory, and sends Brad a Telegram summary.

```
create_trigger(
    name="github-prs",
    trigger_type="webhook",
    prompt_template="New GitHub PR on {{body.repository.full_name}}:\n\"{{body.pull_request.title}}\" by {{body.pull_request.user.login}}\n\nURL: {{body.pull_request.html_url}}\n\nReview this and send Brad a summary."
)
```

### 2. Deployment health monitor

Barsik polls the Render status API every 60 seconds. When `status.indicator` changes from `"none"`, Barsik wakes and alerts Brad.

```
create_trigger(
    name="render-status",
    trigger_type="url",
    url="https://status.render.com/api/v2/status.json",
    condition="json_field_changed",
    condition_value='{"path": "status.indicator"}',
    interval_seconds=60,
    prompt_template="Render status changed! Indicator: {{field_value}} (was: {{field_previous}})\n\nCheck what's failing and alert Brad."
)
```

### 3. Competitor blog monitor

A URL watcher checks a competitor's RSS feed every hour. When the body changes (new post), Barsik wakes and files a memory note.

```
create_trigger(
    name="competitor-blog",
    trigger_type="url",
    url="https://competitor.com/feed.xml",
    condition="status_changed",
    interval_seconds=3600,
    prompt_template="Competitor blog may have updated at {{timestamp}}. Fetch {{url}} and summarize the latest post. Tag it 'competitor-intel' in memory."
)
```

### 4. Stripe webhook → accounting note

A Stripe webhook fires when a charge succeeds. Barsik wakes and appends a line to the revenue log.

```
create_trigger(
    name="stripe-charges",
    trigger_type="webhook",
    prompt_template="New Stripe charge: ${{body.data.object.amount_received}} from {{body.data.object.billing_details.name}} for \"{{body.data.object.description}}\"\n\nAppend this to ~/finance/revenue-log.md with today's date."
)
```

---

## Appendix: File Layout

```
src/pinky_daemon/
    trigger_store.py      # New: TriggerStore class + Trigger dataclass
    api.py                # Modified: trigger endpoints + /hooks/{token}
    scheduler.py          # Modified: _check_url_watchers, _poll_url_trigger

src/pinky_self/
    server.py             # Modified: create_trigger, list_triggers, delete_trigger, test_trigger

frontend-svelte/src/
    components/
        TriggerList.svelte       (Phase 3)
        TriggerCard.svelte       (Phase 3)
        CreateTriggerModal.svelte (Phase 3)
        WebhookUrlCopy.svelte    (Phase 3)
```

No new MCP servers, no new daemons — everything lives in the existing process.
