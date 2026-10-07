# Isolated caller policy

`PINKY_ISOLATED_POLICY_MODE` controls the additional daemon mutation and shared
MCP boundaries. It defaults to `off`. An invalid value logs an error and uses
`enforce`.

- `off` preserves the existing route and shared-tool policy.
- `shadow` records `WOULD DENY` decisions and preserves handler behavior.
- `enforce` refuses unlisted signed isolated mutations before dispatch and
  filters/rejects disallowed shared tools.

The route grant is an exact method and registered FastAPI template. Resolution
follows the first full framework match, including mounted roots. Unknown routes,
unsupported mounts, partial matches and lookup failures cannot grant access.
Existing fleet, peer-group, body-actor and object-ownership checks still apply.
The 29 grants are defined in `isolated_policy.py`; adding a route does not
automatically grant it to isolated agents. Trigger creation remains held, and
other unreviewed mutations, including animation attachments, remain unavailable
to isolated callers in enforce mode.

Verified isolated media callers may send photos, documents and videos only from
inside their own working directory. Both the file and directory are resolved;
containment follows path components, and the resolved file is passed to the
adapter. Missing registry information fails closed. Non-isolated callers retain
their existing behavior.

Isolated transcript binding requires the resolved file's parent to equal an own
Claude project directory. Both the registered working-directory encoding and
its realpath encoding are accepted using the transcript discovery encoder;
peer directories and nested directories are refused. These resource ownership
checks apply in every policy mode, before the adapter or tailer is called.

In enabled modes, a presented internal signature is verified before a public
route shortcut. A valid signed isolated mutation gets the same policy check
even when an owner cookie is also present. Unsigned or invalidly signed public
requests retain their existing provider/anonymous authentication. Shadow keeps
the original public handler state.

Shared MCP derives identity from validated per-agent bearer credentials. In
enforce mode unsigned loopback clients have no agent-acting tools; isolated
callers cannot discover or directly invoke privileged tools. Policy is refreshed
on every list/call, and persistent sessions are bound to their opening principal.
Registry uncertainty cannot grant privileged authority. Dreamer memory access
still requires its existing role authorization. Shadow logs proposed reductions
for unsigned and isolated clients without enabling those reductions.

Malformed/ambiguous authentication is rejected in every mode. Existing
authentication, always-on admin restrictions, launch configuration restrictions
and schedule ownership checks remain active independently of this setting.
Client tool-deny lists are defense in depth; shared-server dispatch is the
authority even when a client ignores its deny list. Stdio configurations and
legacy session creation receive the same privileged-gate restrictions.

Logs identify the verified or explicitly unverified principal, canonical route
or registered tool, mode and decision. They omit credentials, request bodies and
query values. Enabling a mode is an operator rollout decision; changing source
does not change the live setting.
