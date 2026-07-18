# Per-agent Zoho keys — deployment guide

This deployment closes the bash escape hatch where any agent with the
shared `ZOHO_API_SECRET` could sign Zoho API requests as a different
agent. Threat model in
[olegbrok/zoho-crm-cli#10](https://github.com/olegbrok/zoho-crm-cli/issues/10).

## Hard requirement: ptrace_scope=2

The daemon-side change uses per-MCP env to deliver the derived key. That
env is set only on the zoho-mcp subprocess — **not** on the agent's
Claude CLI process. But under default Linux config, same-uid sibling
processes can read each other's `/proc/<pid>/environ`, which would let
agent A read agent B's derived key.

`kernel.yama.ptrace_scope=2` restricts ptrace operations (including
the `/proc/<pid>/environ` read check) to processes with
`CAP_SYS_PTRACE`. Agent processes don't have that capability.

**This setting is mandatory.** Without it, the env-based injection
provides identity binding (no impersonation via path 1) but doesn't
prevent cross-agent secret theft via path 2.

### Apply on Pi

```bash
# 1. Drop a sysctl.d file
sudo tee /etc/sysctl.d/10-pinkybot-ptrace.conf <<'EOF'
# PinkyBot per-agent-keys hardening — see docs/deployment-per-agent-keys.md
kernel.yama.ptrace_scope = 2
EOF

# 2. Apply without reboot
sudo sysctl --system

# 3. Verify
sysctl kernel.yama.ptrace_scope
# expected: kernel.yama.ptrace_scope = 2
```

### Verify the isolation works

After applying ptrace_scope=2, an agent with Bash should fail to read
another agent's MCP env. From a Pi shell as user `oleg`:

```bash
# Pick another running MCP child pid
ANOTHER_MCP_PID=$(pgrep -f 'zoho-mcp --agent sasha' | head -1)
sudo -u oleg cat /proc/$ANOTHER_MCP_PID/environ
# expected: cat: /proc/.../environ: Operation not permitted
```

## Migration sequence

### Phase 0 — Before any deployment

- [ ] zoho-crm-cli main has the per-agent keys server-side
      ([#11](https://github.com/olegbrok/zoho-crm-cli/pull/11)).
- [ ] This PinkyBot PR is merged.
- [ ] Both repos installed on both Mac and Pi via the existing deploy
      flow.

### Phase 1 — Mint master + roll out, agents still on legacy

- [ ] Generate the master secret once. Treat it like an SSH host key.

      ```bash
      openssl rand -base64 48 > /tmp/zoho-master.txt
      # Inspect, then move into place on each host as appropriate
      ```

- [ ] On the Mac:

      ```bash
      sudo tee /Users/oleg/zoho-crm-cli/zoho-secrets.yaml <<EOF
      master: $(cat /tmp/zoho-master.txt)
      EOF
      sudo chmod 600 /Users/oleg/zoho-crm-cli/zoho-secrets.yaml
      ```

      Update server.py config loader (separate small PR) to read this
      file into `PINKY_MASTER_SECRET` env, OR set `PINKY_MASTER_SECRET`
      in the launchd plist for the Zoho server process.

- [ ] On the Pi:

      ```bash
      install -m 600 -o oleg -g oleg /tmp/zoho-master.txt \
              /home/oleg/PinkyBot/.master-key
      ```

- [ ] Wipe the master file from disk wherever you staged it:

      ```bash
      shred -u /tmp/zoho-master.txt
      ```

- [ ] Restart Pi daemon (`sudo systemctl restart pinkybot`). The new
      `streaming_session.py:_inject_zoho_derived_key` will now derive a
      key per agent and inject it into the zoho-mcp entry's env on next
      streaming session connect. Agents that don't have a zoho-mcp
      entry get no-op.

- [ ] Confirm in `journalctl -u pinkybot`:

      ```
      streaming[lera]: injected zoho derived key (instance=abc12345...)
      ```

### Phase 2 — Drop legacy shared secret

After confirming via Mac-side audit log that no requests are coming
through with `auth_method=signed_header` (legacy) for at least a few
days:

- [ ] Remove `Environment=ZOHO_API_SECRET=...` from
      `/etc/systemd/system/pinkybot.service` on the Pi.
- [ ] Remove `ZOHO_API_SECRET` from `~/PinkyBot/.env` on the Pi.
- [ ] Remove `ZOHO_API_SECRET` from any agent-specific `.env` files.
- [ ] On the Mac side, remove `PINKY_SESSION_SECRET` /
      `PINKY_INTERNAL_SECRET` from the Zoho server config once no
      legacy clients remain.
- [ ] `sudo systemctl daemon-reload && sudo systemctl restart pinkybot`.
- [ ] After one release where the legacy path has been unused, drop
      the legacy-acceptance code in zoho-crm-cli (separate PR).

## Verifying the design holds

Three checks worth running periodically:

1. **No shared secret in agent process env.**

   ```bash
   AGENT_PID=$(pgrep -f 'streaming_session.*lera' | head -1)
   sudo cat /proc/$AGENT_PID/environ | tr '\0' '\n' | grep -i 'zoho\|secret'
   # expected: empty (post-migration)
   ```

2. **Cross-agent /proc read denied.**

   ```bash
   SASHA_MCP=$(pgrep -f 'zoho-mcp --agent sasha' | head -1)
   LERA_AGENT=$(pgrep -f 'streaming_session.*lera' | head -1)
   # As the lera agent's user (same uid, just confirms ptrace_scope works):
   sudo nsenter -t $LERA_AGENT cat /proc/$SASHA_MCP/environ
   # expected: Operation not permitted
   ```

3. **Audit log entries are `derived_key` not `signed_header`.**

   On Mac, tail the audit table or log and confirm `auth_method` field
   is `derived_key` for all production traffic.

## Emergency rotation

The instance_id is the lever for invalidating all derived keys without
generating a new master:

```bash
# On Pi
sudo systemctl stop pinkybot
rm /home/oleg/PinkyBot/data/.instance-id
sudo systemctl start pinkybot
# A fresh instance_id is generated; all previously-derived keys
# (including any an attacker might have captured) are now useless.
```

If the master itself is compromised, rotate the master file on both
Mac and Pi (Phase 1 steps) and restart both. No grace period — both
must rotate in lockstep.

## Rollback

This PR is backwards-compatible: with no master key on disk,
`_inject_zoho_derived_key` is a no-op. To roll back without losing
work:

```bash
sudo rm /home/oleg/PinkyBot/.master-key
sudo systemctl restart pinkybot
```

Agents fall back to legacy `ZOHO_API_SECRET` env inheritance (still
in place during migration). The Mac server's backcompat path
([zoho-crm-cli#11](https://github.com/olegbrok/zoho-crm-cli/pull/11))
will accept the legacy signature.
