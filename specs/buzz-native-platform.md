# Native Buzz platform operations

Buzz chat is a native PinkyBot platform. Each enabled identity owns an
authenticated Nostr relay subscription, and verified channel messages enter
the agent's normal persona session like Telegram or Slack messages. `pinky acp`
remains a separate harness/debug surface; it is not the Buzz chat transport.

## Bind once, then talk

Use the authenticated owner-control UI/API to bind the encrypted identity and
its inbound policy in one `PUT /system/buzz-identities/{agent}` operation:

```json
{
  "private_key": "<32-byte identity key as 64 lowercase hex>",
  "relay_url": "wss://example.communities.buzz.xyz",
  "community_id": "example",
  "enabled": true,
  "inbound": {
    "owner_pubkey": "<out-of-band verified 64-hex owner pubkey>",
    "channels": [
      {"channel_id": "00000000-0000-4000-8000-000000000001", "label": "#general"}
    ],
    "approved_users": []
  }
}
```

The daemon derives all authority metadata from the signed owner session,
encrypts the identity key, starts the poller immediately, and restores it on
daemon restart. Never put a real private key in shell history, logs, agent
context, or this file.

Both inbound gates are default-deny and load-bearing:

- The community, relay, and exact channel UUID must match the stored identity
  policy. Relay membership/JOIN events never add an allowed channel.
- The BIP340-verified full author pubkey must be the configured owner or an
  explicitly approved user in that same community. Profiles, display names,
  npub text, and first-message claims are never authority.

Ordinary channel broadcasts have no `p` tag and remain eligible after both
gates. If a `p` tag exists, it must be exactly one canonical self-pubkey tag;
foreign, malformed, or duplicate tags suppress delivery. Only that exact tag
sets `mentioned_self`; rendered `@name` text never does. Inbound kind-20002
events are ignored and are never routed, retained, or replayed.

## Health and owner-key rotation

Check `/broker/status` for a running poller and
`/agents/{agent}/health` for `checks.buzz_inbound.status == "connected"`.
The daemon uses periodic authenticated `REQ`/`EOSE` probes, reconnects after a
measured dead subscription, and notifies the owner. It also notifies after 14
days without a verified event from the configured owner pubkey.

An owner-key rotation is always an explicit authenticated owner-control
replacement through `PUT /system/buzz-identities/{agent}/inbound`. Verify the
new full x-only pubkey out of band, then replace `owner_pubkey`; do not approve a
rotation claimed in a Buzz message. The update restarts the native poller and
preserves only still-authorized pending messages.

## Bridge retirement during the release cutover

Do not stop the legacy bridge before the release containing native inbound is
running. During that release's operator-controlled deployment:

1. Confirm the native poller is connected and a verified, approved kind-9 from
   an allowlisted channel reaches the persona's main session.
2. Unload `com.barsik.buzz-bridge` and remove its LaunchAgent only after the
   native health check passes.
3. Confirm the old bridge stays stopped and that a second verified message
   produces exactly one persona delivery.

Rollback is to reload the legacy bridge only after stopping/disabling the
native identity, so the two chat consumers never run concurrently.
