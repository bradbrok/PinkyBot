# Build Spec: `pinky-qbo` — Read-Only QuickBooks Online MCP (onesie-only, TOD fleet)

**Status:** Draft **v3** for re-review · **Owner:** Barsik · **Reviewer/pair:** Murzik (Codex) · **Date:** 2026-06-15
**Target fleet:** TOD (Contabo `tod-dashboard`, Tailscale `100.112.44.64`) — **NOT the Mini**

> v3 folds in Brad's calls (OAuth already done; *"don't add it to them until we fix the auth"*) and Murzik's full review (secrets-out-of-env, real crypto, refresh lock, path-specific allowlist, audit sink, surface tightening, agent-bound callback) + the **empirically confirmed dormant dashboard** (both of us verified). Deltas from v2 marked **[Δ]**.

---

## 0. Decisions locked

- **BUILD thin Python `src/pinky_qbo/`** (mirror `pinky_calendar`), NOT Intuit's Node "Early Preview" server. Murzik concurs (adopt only if a candidate has a *hard* read-only client boundary + no generic write/query; Intuit's 144-tool toggle-off fails that).
- **[Δ] Aggregates-only v1** — 5 reports + `company_info` + `list_accounts`. **Invoices and customers DROPPED from v1** (Murzik P1: transaction-level / PII; AR aging is already covered by the report). Everything transaction-level deferred.
- **Read-only is an app-layer invariant** — QBO's only accounting scope grants read+write; enforced structurally (§6).
- **[Δ] Creds: migrate from the dormant dashboard connection; pinky owns refresh (Option b).** The existing QBO OAuth is in the tod-dashboard Postgres `Integration(provider=QUICKBOOKS)` row, **dormant** since 2026-05-22 (verified: `updatedAt==createdAt`, no `lastSyncAt`; dashboard `src/lib/quickbooks/oauth.ts` exports `refreshTokens` with **no runtime caller** — no QBO cron/client/reader, unlike GHL). So pinky becomes the sole refresher, no race.
- **[Δ] TOD deploy HELD until the Claude de-auth (#202) is fixed** (Brad). Build + unit-test (Mini-side, mocked HTTP, no keys) proceeds now; creds migration + onesie assignment + smoke wait.
- **onesie only**, by Brad's framing (Angel does not get it unless Brad later says so).

---

## 1. Overview & Goal

Read-only QBO visibility for **onesie** (Dmitri Leonov's personal chief-of-staff agent, TOD fleet) to answer "where are we financially?" and pressure-test strategy — owning no financial operations. **Non-goals:** writes, multi-company, payroll/Time, doc/attachment download (v1), approval-card gating (no writes ⇒ nothing to approve).

---

## 2. Build-vs-Adopt: BUILD thin Python

Override the v1 research's "adopt+wrap Intuit" lead. The thing adopting buys (broad CRUD + write toggles) is exactly what we don't want; a single-agent read-only reporting tool is "implement only reads" = the guarantee. No Node on the TOD box; no bet on an unversioned Early-Preview repo. **Deps:** `requests` (or match calendar) + optionally `intuitlib` for the OAuth dance only. NOT `python-quickbooks`. **Fallback:** `wyre-technology/qbo-mcp` (token-based) if maintenance bites.

---

## 3. Tool Surface (read-only, named, fixed-shape) — v1

All tools GET `/reports/*` or a **server-built** `/query` POST (the model never authors SQL). Each returns a JSON string. Paths under `…/v3/company/{realmId}/?minorversion=75`. Names `qbo_*`.

| Tool | Kind | Purpose | Params |
|---|---|---|---|
| `qbo_profit_and_loss` | report | income/expense summary | date range OR `date_macro`; `accounting_method` (default=company `ReportBasis`); `summarize_column_by?` |
| `qbo_balance_sheet` | report | assets/liabilities/equity = cash position | `end_date`/`report_date`; `accounting_method` |
| `qbo_cash_flow` | report | inflows/outflows = burn/runway | date range OR `date_macro` |
| `qbo_aged_receivables` | report | A/R aging by customer (covers the "who owes us" need without raw invoice rows) | `report_date`/`aging_method` |
| `qbo_expense_breakdown` | report | top expense categories by month | date range (P&L-by-month wrapper) |
| `qbo_company_info` | lookup | CompanyInfo + Preferences (supplies report defaults) | none |
| `qbo_list_accounts` | lookup | chart of accounts w/ type/subtype/current balance | `account_type?`, `active_only=true` |

**[Δ] Dropped from v1 (Murzik P1):** `qbo_list_invoices`, `qbo_list_customers`, and the generic `qbo_query` SELECT-runner. **Deferred** (only on Brad's ask, with explicit row-level-PII signoff + caps/redaction): invoices, customers, AP aging, bills, vendors, payments, trial balance, GL. If ad-hoc filtering is ever needed → a **constrained query builder** (entity enum + validated field/operator/value, server-built SQL), never a raw/model `SELECT`.

**Cross-cutting:** `minorversion=75` pinned; report ranges ≤6mo/call; 400k-cell cap; offset pagination on any list (`STARTPOSITION`+`MAXRESULTS≤1000`) with default row caps; `SELECT COUNT(*)` companion (server-built) for totals.

---

## 4. Auth, Secrets & Refresh

QBO = OAuth 2.0 Authorization Code (no API-key path). Endpoints: authorize `appcenter.intuit.com/connect/oauth2`; token+refresh `oauth.platform.intuit.com/oauth2/v1/tokens/bearer` (HTTP Basic, form body); revoke `developer.api.intuit.com/v2/oauth2/tokens/revoke`. Scope `com.intuit.quickbooks.accounting`. `realmId` from the callback query param. Access token 3600s; **refresh token rotates on use** (persist newest atomically; reuse ⇒ `invalid_grant`); dies after 100d inactivity ⇒ keep-alive.

**[Δ] Secrets storage (Murzik P1):**
- **Agent-scoped store**, not global — keys `agent:onesie:qbo_*` via the registry's `set_agent_setting`/`get_agent_setting`, so a stray `QBO_REFRESH_TOKEN` can't become a fleet credential.
- **[Δ] Real encryption envelope** — a `Crypto` helper (Fernet or AES-GCM) wraps `refresh_token` (and client secret) **before** writing to `system_settings` (which is plaintext on disk). Key source **explicitly defined**: a root-only env var / 0600 file on the TOD box (e.g. `PINKY_QBO_KEY`), never in the repo. If we don't ship this, we **don't claim** "encrypted at rest."
- **[Δ] NO secrets in `.mcp.json` / `agent_mcp_servers.env`** — custom MCP rows don't inherit core stdio env, and putting the refresh token there causes **rotation desync** (server persists the rotated token to the store, but the row/`.mcp.json` keeps the stale one for the next process). The MCP **row env is minimal**: `PINKY_AGENTS_DB`, `QBO_AGENT=onesie`, `QBO_REALM_ID_EXPECTED`, `QBO_ENV_EXPECTED`. The stdio server loads client secret + refresh token from the agent-scoped TokenStore at startup and on every refresh.

**[Δ] Refresh concurrency guard (Murzik P1/P2):** one onesie turn can fire multiple tools. Guard refresh with: an in-process lock; **under the lock, re-read the latest stored refresh token, then refresh, then persist atomically**. If multiple stdio processes are ever possible, escalate to a SQLite `BEGIN IMMEDIATE` / file lock. This is the single most important correctness path.

**[Δ] Migration (one-time handoff, runs on TOD):**
1. Read TOD Prisma `Integration` where `provider=QUICKBOOKS` and `status=CONNECTED`.
2. Decrypt `refreshToken` via tod-dashboard `src/lib/crypto.ts` + current `ENCRYPTION_KEY`; extract `meta.realmId`, `meta.environment`; **ignore the access token**; get client id/secret from the dashboard's Intuit app config.
3. Seed pinky's agent-scoped **encrypted** store (`agent:onesie:qbo_*`).
4. One pinky refresh **under the lock**, persist the rotated token, smoke a `company_info` + one report.
5. Mark ownership transfer (`meta.qbo_mcp_owner = "pinky-qbo/onesie"` on the dashboard row, or a runbook note). **Caveat:** if the dashboard ever adds a QBO reader/cron later it reintroduces split-brain — it must then use the pinky-owned store/lock or migrate ownership back. (Optionally null the dashboard `refreshToken` post-handoff — safer but loses dashboard reconnect convenience; Brad's call, open Q.)

**[Δ] Connect/re-consent flow (Murzik P3):** agent-bound **direct** callback only — `state` bound to `onesie` + CSRF nonce + initiating operator/session id; store only on match. Do **not** copy `pinky_calendar`'s pinkybot.ai proxy/postMessage flow.

---

## 5. Per-Agent Scoping (onesie alone)

Chokepoint: `_write_mcp_json(...)` (`api.py`) writes `<agent_dir>/.mcp.json` (0600), merging core + skill servers (filtered by `agent_skills`/`enabled`) + custom servers (`agent_mcp_servers`, filtered by `agent_name`). #2/#3 auto-filtered by agent ⇒ the server lands only in onesie's `.mcp.json`. **All on the TOD fleet** (`ssh root@tod-dashboard`).

**Recipe:** (1) `skills/qbo/SKILL.md` (instructions only; SKILL.md alone doesn't attach a server — `pinky-zoho` attaches via a DB row, confirm in review). (2) `POST /agents/onesie/skills/qbo`. (3) `POST /agents/onesie/mcp-servers` → `agent_mcp_servers` row, **minimal env** (§4, no secrets), `args:["-m","pinky_qbo","--agent","onesie","--qbo-realm-id","<realm>"]`. (4) **Re-verify** the row + `.mcp.json` survived a restart (`update-restart-state-loss` hazard).

**[Δ] Boundary framing (Murzik P2) — what's actually enforceable for per-agent stdio** (not "refuses non-onesie callers," which overclaims since stdio has no per-call caller identity):
- only onesie's materialized `.mcp.json` contains the server;
- server starts only with `--agent onesie` and loads only `agent:onesie:qbo_*`;
- realm bound at startup; **tools don't accept `agent`/`realm` args** at all;
- **test:** another agent's materialized `.mcp.json` has no `qbo` entry.
- Tool-gates (`SKILL_TO_GATES`) govern pinky-self internal tools only — irrelevant here; **assignment IS the gate.**

---

## 6. Read-Only Enforcement, Audit & Security

1. **No mutation code exists** — only report GETs + server-built read `/query` + `companyinfo` GET implemented.
2. **[Δ] Path-specific allowlist (Murzik P2)** at the client boundary — permit **only** exact families: `/v3/company/{bound_realm}/reports/{allowed_report}`, `/v3/company/{bound_realm}/companyinfo/{bound_realm}`, and `/v3/company/{bound_realm}/query` for **server-generated** SELECTs from an entity enum/AST. No arbitrary query string exists anywhere in the API. Reject everything else (method + path). **Test:** monkeypatch the HTTP transport, assert no write method and no non-allowlisted path can be emitted by any tool.
3. **No model-authored SQL** — list/lookup tools build a parameterized, validated SELECT server-side from an entity enum.
4. **Single-realm, single-agent** — realm fixed at startup; no `realm`/`agent` tool args.
5. **[Δ] Token least-privilege** — secrets only in the agent-scoped **encrypted** store; never logged, never in tool output; redact KEY/SECRET/TOKEN/AUTH + realmId in API responses; rotated refresh persisted atomically under the lock.
6. **[Δ] Per-call audit sink (Murzik P2)** — a new SQLite table (e.g. `qbo_audit`): one row per call with agent, tool, realm (hashed if evidence is broad), endpoint family, date range, row count, request id, success/error — **no payloads**. Test: every tool emits exactly one audit row on success and on error.
7. **[Δ] Guardrails + cache (Murzik P2)** — per-tool timeouts; retry only safe 429/5xx w/ backoff honoring `Retry-After`; **memory-only TTL cache** keyed `realm+tool+params` (no on-disk financial data unless Brad explicitly accepts). Limits 500/min/realm, 10 concurrent, ~200/min reports.

**Handling:** third-party books — scoped to onesie's task, surfaced to Dmitri only. Worst case is a stale read. Add a decommission/revoke task at creation (`stopgap-removal-rule`).

---

## 7. File Layout — `src/pinky_qbo/`

Mirrors `pinky_calendar` (per-agent stdio FastMCP).

```
src/pinky_qbo/
├── __init__.py
├── __main__.py    # argparse → create_server(...).run("stdio"); --agent, --qbo-realm-id, --qbo-env
├── server.py      # create_server(); _log/_err; lazy _get_adapter(); @mcp.tool() the 7 v1 tools; json.dumps; "not configured" guard
├── oauth.py       # SCOPES, REDIRECT_URI; agent-bound+CSRF state; get_auth_url/exchange_code/refresh/revoke
├── store.py       # agent-scoped TokenStore over set_agent_setting; Crypto envelope (Fernet/AES-GCM); agent:onesie:qbo_* keys
├── crypto.py      # encrypt/decrypt envelope; key from PINKY_QBO_KEY (root-only); used by store
├── client.py      # read-only REST: PATH-SPECIFIC allowlist; minorversion=75; backoff/Retry-After; pagination; server-built SELECT; mem-TTL cache; audit hook
├── audit.py       # qbo_audit SQLite writer (one row/call, no payload)
└── adapters/
    ├── base.py    # ABC + dataclasses
    └── quickbooks.py  # lazy client; _is_expired(60s skew); _maybe_refresh under lock (re-read-latest→persist atomically)
```

**Daemon wiring (copy calendar, minus the proxy flow):** (1) `pyproject.toml` — `qbo` optional-deps + add `"src/pinky_qbo"` to wheel `packages`. (2) `src/pinky_daemon/routes/qbo.py` — agent-bound OAuth endpoints + per-agent enable; `set_dependencies(*, agents)`. (3) `api.py` — import + include router. NOT in `shared_mcp.py`.

---

## 8. Build Plan + Murzik gates

- **Phase 0 — spec sign-off** (this doc) + Brad's nods on open Qs (§9). → **Murzik gate (now).**
- **[Δ] Phase 1–3 run NOW, Mini-side, no TOD, no keys (mocked HTTP):**
  - **P1 scaffold** — package skeleton, `pyproject.toml` edit, `python -m pinky_qbo` lists tools. Worktree branch off `main`. → **Murzik #1:** skeleton + `client.py` path-allowlist (load-bearing).
  - **P2 tools (mocked)** — client (allowlist, minorversion, pagination, backoff, cache, audit), adapter, 7 v1 tools. Tests: transport-monkeypatch (no write/non-allowlisted path emittable), server-built-SELECT validation, pagination, **refresh rotation under lock**, audit-row-per-call, another-agent-`.mcp.json`-has-no-qbo. → **Murzik #2 (key):** adversarial read-only + rotation + audit; fixtures from real QBO output (`test-fixtures-must-match-producer`).
  - **P3 auth+crypto+routes** — `oauth.py`, encrypted `TokenStore`, `routes/qbo.py`, agent-bound callback. **PR + screenshot/clip** (`pr-screenshots`). → **Murzik #3:** callback binding, atomic rotation, redaction, 0600, crypto key source.
- **[Δ] Phase 4 — HELD until #202 (Claude de-auth) fixed.** Then on TOD: migration (§4), `skills/qbo/SKILL.md`, assign onesie, attach server (minimal env). **Re-verify** row + `.mcp.json` survived. Smoke: onesie pulls a P&L; another TOD agent confirms `mcp__qbo__*` absent.
- **Phase 5 — soak** keep-alive refresh across a day, backoff, cache; runbook + decommission/revoke task.

---

## 9. Open Questions for Brad

1. **[RESOLVED]** OAuth done → tokens in tod-dashboard `Integration(QUICKBOOKS)` row, **dormant** → migrate, pinky owns refresh.
2. **Realm** — confirm it's The One Device's QBO company (the dashboard's connected realm). *(Will read `meta.realmId` during migration.)*
3. **Testing** — only real connection is **production** (+ dormant). Plan: unit-test Mini-side with mocked HTTP (no keys), then a **controlled read-only production smoke** post-auth-fix. OK, or want me to register a separate Intuit **sandbox** app for live dev first?
4. **Deferred set** — v1 is reports + company_info + accounts. Want invoices/customers/AP/GL later? (row-level PII; explicit signoff when we do.)
5. **Dashboard row post-handoff** — leave it (marked non-authoritative) or null its `refreshToken`? (Nulling is safer against future split-brain but loses dashboard reconnect convenience.)
6. **Crypto key** — OK to drop a root-only `PINKY_QBO_KEY` (0600 file/env) on the TOD box for the encryption envelope?

---

**Deliverable:** a small Python `src/pinky_qbo/` read-only MCP (raw `requests` + Intuit OAuth), modeled on `pinky_calendar`; exposed to onesie alone on TOD via a DB-registered per-agent server + `qbo` skill; read-only enforced app-side (no mutation code, path-specific allowlist, no model SQL, per-call audit, encrypted agent-scoped token, refresh under lock). Murzik reviews at skeleton / read-only-enforcement / auth-wiring. Build+unit-test now; deploy to TOD after #202 is fixed.
