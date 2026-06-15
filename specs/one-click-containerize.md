# One-Click Containerize — design for deliberation (Barsik ↔ Murzik)

**Goal (Brad):** one click in the Agents UI to **enable** (and **disable**) Podman container
isolation on an agent, with sensible defaults + clear feedback. Today it's a manual
multi-step dance. "We still haven't fully closed the containerization workflow done via
agent and front-end UI. Should basically be one click to enable."

## Current state (verified in code)

**Backend**
- `Agent.isolation_mode` ∈ {`local`,`unix_user`,`container`}, `Agent.container_image`, `Agent.isolated`.
- `PUT /agents/{name}` updates these. local→container: regenerates `.mcp.json` immediately but
  **does NOT provision** (deferred to next session start). container→local: **deprovisions**
  (container+secret removed, **home volume kept**).
- `POST /agents` (register) provisions synchronously with rollback — but only at first register.
- Runtime gated by `PINKY_CONTAINER_RUNTIME` (default OFF) → `container_runtime_enabled()` /
  `container_runtime_binary()` (podman/docker; docker not yet supported).
- `_isolation_block_reason()` blocks spawn: 501 if runtime not enabled; 400 if `container` mode
  without `transport=tmux`. **Container REQUIRES transport=tmux.**
- `ContainerProvisioner.provision()` idempotent; needs a `container_image` (**bring-your-own, NO default**).
- `_ensure_container_started()` provisions+pulls+starts at session spawn (image pull can be **minutes**).

**Frontend** (`frontend-svelte/src/pages/Agents.svelte`)
- Detail modal → **Runtime tab** already has an isolation `<select>` (LOCAL/CONTAINER) + a
  `container_image` text input + `saveAgentIsolation()` → `PUT /agents/{name}`.
- Create wizard already exposes `wizIsolation` + `wizContainerImage`.
- API client: `api('PUT', '/agents/{name}', body)` (session-cookie auth).

## Why it's not "one click" yet (the friction)
1. **No default image** — user must know/type an OCI ref.
2. **Transport coupling** — must switch transport→tmux first (separate toggle; errors otherwise).
3. **No preflight** — if the host runtime is OFF, the user only finds out at spawn (501).
4. **Deferred + invisible provisioning** — must manually stop/restart; the (minutes-long) image
   pull has no progress; provision errors surface late, at the next spawn.
5. Two coupled fields + manual restart = several steps, not one click.

## Proposed design

**B1 — Preflight endpoint.** `GET /system/container-runtime` →
`{ enabled, binary, default_image, ready }`. UI uses it to enable/disable the control, prefill the
default image, and message "container runtime not enabled on this host."

**B2 — Default image.** New env `PINKY_CONTAINER_DEFAULT_IMAGE` (fleet default; e.g. our
`pinky-agent-runtime`). One-click uses it when the user doesn't override. *(Open: ship a canonical
image, or doc bring-your-own + this env? v1 = env-configured default, bring-your-own override.)*

**B3 — Atomic action endpoints.**
- `POST /agents/{name}/containerize` body `{ image?: str }`: validate runtime (else 501 actionable);
  auto-set `transport=tmux` (handle `runtime!=claude_sdk`); set `isolation_mode=container` + image
  (default if omitted); provision (volume/secret/pull/create); restart the session so it takes effect;
  **rollback cleanly on failure**.
- `POST /agents/{name}/decontainerize`: `isolation_mode=local` + deprovision (keep volume) + restart.
- *(Alternative: orchestrate from the frontend via existing `PUT` + restart. I prefer dedicated
  endpoints — atomic, one call, encapsulated rollback + clear errors. ← deliberate.)*

**B4 — Provision timing/feedback (key decision).** Image pull can take minutes.
- (a) **Sync** + spinner + bounded timeout — simple; UI blocks; long pulls risk timeout.
- (b) **Async**: endpoint returns 202 + status; UI polls `GET /agents/{name}/container-status`
  (`pulling`|`provisioned`|`error`). Better UX, more code.
- (c) **Hybrid**: provision via `_ensure_container_started` as today, but expose a status surface the
  UI polls after the restart.
- I lean (b)/(c). ← Murzik's take?

**B5 — Restart semantics.** Apply via **stop + fresh session (cold-start)** — sidesteps the operator
restart-guard friction I hit today (`streaming/restart` 409s on save-recency + "saved via
save_my_context from this session" guards) — vs the `streaming/restart` endpoint. ← deliberate.

**B6 — UI.** Replace the manual select+image with a single **"Containerize ⚡"** button (uses default
image; an "advanced" disclosure for an image override). Disabled w/ hint when runtime not ready
(from B1). Auto-handles the transport switch. Shows provisioning progress (*pulling image…*) + result.
A **LOCAL / CONTAINERIZED** status badge on the card + detail metadata row. Symmetric
**"Return to local"** with a confirm (volume kept).

## Open questions for Murzik
1. Dedicated endpoints (B3) vs frontend-orchestrated? (I prefer dedicated.)
2. Provision feedback: sync vs async-with-status (B4)?
3. Default-image story (B2): env default OK for v1, or do we need a shipped canonical image first?
4. Restart semantics (B5): cold-start stop+create vs `streaming/restart` (guards)?
5. v1 scope vs follow-ups — what's the MINIMUM one-click that ships *well*? (My instinct: B1 + B2 +
   B3-enable + B6-button + a basic status, defer decontainerize polish + badge nicety if needed.)

## ✅ Converged decisions (Barsik ↔ Murzik, 2026-06-15)

**1. Dedicated endpoints — yes.** Frontend-orchestrated PUT+restart is the wrong boundary; too many
coupled invariants (runtime gate, image/default, claude_sdk-only, tmux transport, `.mcp.json`
rewrite, provision/probe, stop-old/start-new session class). Backend owns the choreography.
Shape:
- `POST /agents/{name}/containerize` body `{ image?, start?=true, force?=false }`
- `POST /agents/{name}/decontainerize` body `{ start?=true, force?=false }`
- `GET /agents/{name}/container-status`
- `GET /system/container-runtime`

**Key safety invariant — provision before persist.** Do NOT write `isolation_mode=container` to the
DB before the slow/risky provision. Build a *desired in-memory Agent copy* (`transport=tmux`,
`isolation_mode=container`, `isolated=True`, `container_image=image`), provision + probe it, **then**
persist. A failed image pull / binary-contract failure must leave the prior config intact (never a
DB stuck in unrunnable container mode). Rollback per-agent volume/secret/container only — the pulled
image is shared cache, don't roll it back.

**2. Real async (202 + status polling) — not sync, not hybrid.** Podman pulls take minutes and
`_ensure_container_started()` already can't cancel the underlying thread. Daemon-local op record:
`queued|validating|pulling|creating|probing|applying|restarting|ready|error` + `message`,
`started_at`, `updated_at`, `image`, `provisioned`, `runtime_ready`. On daemon restart, reconstruct
a **coarse** status from registry + `is_provisioned()` and report `unknown/not_running` — no fake
continuity. The action actively provisions/probes *now*, then restarts only after the container is
known runnable (this directly fixes today's deferred-invisible-provisioning pain).

**3. Default image — `PINKY_CONTAINER_DEFAULT_IMAGE` env for v1.** Honest preflight: `ready=false`
unless runtime enabled AND binary supported (`podman`; docker not yet) AND (default image exists OR
caller supplies override). Validate image strings: max length, no whitespace/control chars (argv
already avoids shell injection, but keep logs/UI sane). Shipping a canonical `pinky-agent-runtime`
image is the right **product follow-up**, not a v1 blocker.

**4. Cold stop + fresh start — NOT `/streaming/restart`.** The restart endpoint reconnects the
*existing* session object, so an SDK session would not become a `TmuxSession` after the transport
flip. Containerize must disconnect/unregister existing streaming sessions and call
`_start_streaming_session()` so the session class + command runner are selected from the new
registry row. Guard active work: return **409 unless `force=true`** if the agent has inflight turns /
busy status; UI shows a confirm. Stop **all** labels before applying; recreate only `main` unless
explicitly resurrecting every label.

**5. v1 includes decontainerize.** Brad asked enable + disable; without a return path a bad
image/auth problem strands the user. Polish can wait, but the backend endpoint + a simple "Return to
local" button (confirm, **keeps home volume**) ship with enable.

**Failure-mode guards (Murzik's weights, all adopted):**
- **Wake/start race:** per-agent lifecycle lock around apply/stop/start, sharing/enclosing
  `_streaming_ensure_locks`, so an inbound message / scheduler wake can't start a session mid-mutation.
- **Manual PUT race:** route isolation-touching `PUT /agents/{name}` through the same lock (or
  document the Runtime tab as legacy/advanced and let the one-click endpoint own the supported path).
- **Runtime coupling:** auto-set `transport=tmux`, but do NOT auto-convert `runtime`→`claude_sdk`;
  reject non-`claude_sdk` agents with a clear 400.
- **Multi-session:** stop/unregister all labels before applying; don't leave siblings running local
  while main is containerized.
- **Authz:** operator/UI endpoints only, same privilege class as `PUT /agents`; NOT exposed as
  self/MCP tools. Isolated agents must not decontainerize themselves via an internal route.

**Tests (fake `ContainerOps`):** no DB mutation before provision/probe success; failed provision
leaves prior config intact; active-work → 409 unless force; decontainerize keeps the volume.

### v1 ship list (minimum that ships *well*)
1. `GET /system/container-runtime` (honest preflight)
2. `PINKY_CONTAINER_DEFAULT_IMAGE` env + image validation
3. async `containerize` op + `GET /container-status` polling (provision-before-persist, per-agent lock)
4. basic `decontainerize` op (keeps volume)
5. one UI **"Containerize ⚡"** button + advanced image override + disabled/error states; symmetric
   **"Return to local"** with confirm; LOCAL/CONTAINERIZED status surface
6. cold apply via stop/unregister + `_start_streaming_session` (not `/streaming/restart`)
7. tests above

Stays gated behind `PINKY_CONTAINER_RUNTIME` (default OFF) — zero behavior change until a host opts in.
