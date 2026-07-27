# Login-wall fleet fallback (#916)

**Status:** Phase 1 · **Owner:** Barsik · **Reviewer:** Murzik

## Scope

Phase 1 turns a silent shared-credential outage into an actionable owner page:

1. The session watchdog captures active `runtime=claude_sdk`,
   `transport=tmux` panes through each `TmuxSession`'s existing
   `_TmuxControl`. Codex-over-tmux is not a Claude credential-fleet member.
   The existing control preserves the daemon's normal command runner and
   socket plumbing on every host; the detector does not invoke `nsenter` or
   construct a second tmux path.
2. A pane is login-walled when its joined capture contains
   `Paste code here if prompted` or `Select login method`.
3. The wall must appear on two samples at least 30 seconds apart. One
   confirmed agent produces a per-agent warning. At least two confirmed
   agents in the shared-refresh-file cohort produce a fleet incident.
4. Before the fleet page, the watchdog renames one positive session from
   `pinky-<agent>` to `login-hold-<agent>`. The rename removes that pane from
   normal supervisor targeting and preserves the OAuth PKCE state.
5. The watchdog recaptures the held pane with `capture-pane -p -J`, extracts
   its full `https://claude.com/cai/oauth/authorize?...` URL, and sends it
   through the existing owner-notification destinations. The owner is asked
   to reply with the exact `code#state` string.

A confirmed shared wall plus an unclassifiable shared peer is treated as a
possible fleet incident and paged. This is intentionally fail-closed: a
redundant page is safer than another silent fleet outage.

Capture failures retain the agent's already-resolved credential cohort;
unknown callback failures cannot default into the shared fleet. Once every
affected active registration is definitively clear (or no longer
eligible/registered), the Phase 1 incident latch and `#902` hold exemption are
released. Phase 1 does not restart or delete the preserved pane during that
re-arm transition.

## Phase 1 operator completion

Phase 1 does not consume or paste the owner's reply. An operator must enter
the returned `code#state` string into the frozen pane without shell
interpretation (the `#` is part of the value), verify that Claude reports a
successful login, then restart the affected agents serially.

Do not restart immediately after the login completes. Current Claude Code
builds can rewrite `~/.claude.json` and remove both
`hasCompletedOnboarding` and `lastOnboardingVersion`. Restore both onboarding
markers before restarting the fleet, or valid credentials can still leave
every pane at the onboarding wizard.

Large resumed sessions can also stop at the post-boot
resume-from-summary dialog. Select the recommended option (normally Enter)
or flag the pane for operator review before considering recovery complete.

## Deferred Phase 2

Automatic owner-reply correlation, literal `send-keys -l` code injection,
success verification, orphan cleanup, onboarding repair, and serialized
fleet restart are explicitly deferred. No Phase 2 auto-paste path is enabled
by this detector. In particular, current `claude.com/cai` URLs are accepted
only by the Phase 1 notification extractor and are excluded from the legacy
flag-gated reply/paste watcher.
