# Container isolation (per-agent rootless Podman)

Phase-3 of #149 / #638: each opted-in agent runs its tmux server + `claude`
REPL inside its own rootless Podman container. The daemon orchestrates the
full lifecycle (provision on register, start on session connect, recreate on
image change, deprovision on retire) — no manual sysadmin per agent.

What the boundary buys: per-agent filesystem namespace (no sibling
`.mcp.json` / signing-key theft), private network namespace, cgroup
memory/pids caps, and no fleet-wide `PINKY_SESSION_SECRET` inside the
sandbox (per-agent key only).

## Host setup (one-time, per host)

1. Install rootless Podman (validated: Podman 5.4.2, Debian 13, aarch64).
   Docker also works (`PINKY_CONTAINER_RUNTIME=docker`).
2. Daemon env (`.env`):
   - `PINKY_CONTAINER_RUNTIME=podman` — the opt-in gate. Unset = container
     agents register but refuse to start (fail-closed).
   - `PINKY_SHARED_MCP=1` and `PINKY_SHARED_MCP_HOST=0.0.0.0` — container
     agents reach MCP via SSE at `host.containers.internal:8890`. Binding
     beyond loopback is safe because non-loopback shared-MCP requests now
     REQUIRE the per-agent bearer token (see Security below).
   - Optional: `PINKY_CONTAINER_MEMORY` (default `2g`), 
     `PINKY_CONTAINER_PIDS_LIMIT` (default `2048`) — `0` disables a cap.
   - Optional: `PINKY_CONTAINER_DAEMON_URL` (default
     `http://host.containers.internal:8888`) if the daemon API listens on a
     non-default port.
   - Optional: `PINKY_CONTAINER_SEED_CREDS=0` to disable the automatic
     Claude-credentials bootstrap (see Credentials below).
3. The daemon API must listen on a container-reachable interface (default
   `0.0.0.0:8888` — already the case).

## Bring-your-own image

Pinky pulls the image, never builds it, and bakes in no tools. Contract
(checked at cold start, clear BOOT_FAILED message if missing):

- `tmux`, `claude` (Claude Code CLI), `python3`, `sh`, `sleep`

A minimal Debian-slim + python3 + tmux + Claude Code image is enough. The
image must match the host arch (aarch64 on the Pi).

## Per-agent activation

Register (or update) with `isolation_mode="container"` and
`container_image="registry/image:tag"` (required — 422 without it).
`isolated=true` is implied/coerced for any non-local mode. Transport must
be `tmux` (enforced). The UI exposes both fields (wizard advanced block +
agent detail Runtime tab).

What the daemon creates per agent (all prefixed `pinky-<agent>`):

- container `pinky-<agent>` — created stopped, `sleep infinity` entrypoint,
  started on session connect; every tmux command is `podman exec`ed in
- volume `pinky-<agent>-home` — in-container HOME (`/home/agent`); persists
  any CLI state; preserved on retire, removed only on hard purge
- secret `pinky-<agent>-key` — the per-agent signing key (stdin-created,
  never on argv)

Key create flags: `--userns=keep-id` (claude refuses
`--dangerously-skip-permissions` as root; keeps bind-mounted files
writable), `--add-host=host.containers.internal:host-gateway`,
working_dir bind-mounted at the SAME absolute path, memory/pids caps.

## How the response pipeline works (CLAUDE_CONFIG_DIR)

The container runs with `CLAUDE_CONFIG_DIR=<working_dir>/.claude-container`.
Because the working_dir is same-path bind-mounted, transcripts, trust
flags, and credentials are visible to the host daemon at the identical
absolute path — the host-side transcript tailer, `--continue` detection,
and watchdog evidence work unchanged. Changing this breaks message
delivery; don't.

Hook scripts inside the container POST to the daemon via
`PINKY_DAEMON_URL=http://host.containers.internal:8888` (injected into the
session env automatically).

## Credentials

The daemon seeds its own `~/.claude/.credentials.json` into the agent's
config dir once (first spawn, skipped if present, 0600). **This is a
BOOTSTRAP, not a durable identity** — live-learned on the Pi (2026-06-10):
the seed is a COPY of the operator's OAuth grant, and Anthropic rotates
refresh tokens on use, so the first time any host-side claude refreshes
that grant (routine, every few hours), the container's frozen copy is
invalidated and the agent wakes up signed out
("401 Invalid authentication credentials / Please run /login"). The
local fleet never hits this because all host agents read the SAME file —
one rotation chain; a private copy forks the chain and loses the race.

**Durable path (do this once per container agent, right after first
boot):** run `/login` in the agent's pane (or
`podman exec -it pinky-<agent> claude login`) and complete the OAuth
flow. That mints the agent its OWN grant with its own rotation chain —
no collision with the host fleet, even on the same Claude account. The
credentials persist in the host-visible config dir (and are mirrored
into the home volume by the spawn-time seed), surviving container
recreates. To skip the bootstrap seed entirely:
`PINKY_CONTAINER_SEED_CREDS=0`.

## Security model

- Shared MCP (`:8890`): identity = `X-Agent-Name` header + `Authorization:
  Bearer <token>`. The token is a one-way DERIVATION of the per-agent
  signing key (`derive_mcp_bearer`) — capturing it on the wire never
  yields the signing credential itself. Every SSE agent's `.mcp.json`
  carries it (container and local alike).
- Enforcement: whenever the shared MCP is BOUND beyond loopback
  (`PINKY_SHARED_MCP_HOST`), bearer auth is required for EVERY request —
  per-request peer-address classification is deliberately not trusted
  there, because rootless Podman (pasta/slirp) can present container
  traffic with a loopback source address. On a loopback bind, loopback
  callers keep legacy header trust unless
  `PINKY_SHARED_MCP_REQUIRE_AUTH=1` (values 0/false/no = off). A bearer,
  when present, is always validated (constant-time, resolved from the
  live registry) — there is no downgrade path.
- Isolated agents never receive `PINKY_SESSION_SECRET`; they sign internal
  requests with `PINKY_AGENT_KEY` only, and the daemon refuses
  global-secret auth for isolated callers. Any non-local isolation_mode
  implies `isolated=true` (coerced at register/update and enforced again
  at the runtime secret gate).
- `.mcp.json` is 0600 and sits in the agent's own working_dir.
- The execution seam (local vs podman-exec) is re-selected from the live
  registry row on EVERY spawn, and a registry failure at spawn fails
  closed (BOOT_FAILED) — never a silent fallback to host execution.

## Failure behavior

- Container dies while idle: the next message's paste fails with a podman
  "not running" error -> the session schedules disconnect -> the next
  inbound message auto-wakes, `ensure_started` restarts the container.
- Image changed on the agent record: next cold start recreates the
  container (home volume + key secret preserved).
- Missing podman binary / gate off / bad image: clear, actionable errors
  at register or BOOT_FAILED at start — never a silent local fallback.

## Known limits / follow-ups

- macOS hosts (Mac Mini): podman runs inside a `podman machine` VM —
  same-path bind mounts and host-gateway semantics need separate
  validation. Pi/Linux first; container agents on macOS unsupported until
  validated (#638).
- No per-agent egress allowlist yet (network namespace only) — #638
  increment 4 carries the nftables/pasta prototype.
- Containers stay running while the agent idles (`sleep infinity` is
  near-free); stop-on-idle-sleep is wired in the provisioner but not yet
  called.
- Container provision/start at spawn runs under its own budget
  (`PINKY_CONTAINER_START_TIMEOUT_SEC`, default 600s) so a legitimate
  multi-minute image pull cannot trip the 60s REPL cold-start timeout.
- Docker (`PINKY_CONTAINER_RUNTIME=docker`) is wired but UNVALIDATED —
  in particular rootless Docker's in-container uid is 0 and claude
  refuses `--dangerously-skip-permissions` as root. Podman is the
  supported runtime; treat docker as experimental.
- Flipping an agent back to `local` keeps `isolated=true` (the flag also
  gates daemon API scoping, so it is never auto-cleared); clear it
  explicitly via `PUT /agents/{name} {"isolated": false}` if intended.
  The downgrade deprovisions the container + key secret; the home volume
  is kept.
