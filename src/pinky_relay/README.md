# Sealed ferry relay

The relay stores ciphertext and routing metadata. It cannot authenticate a message
as a peer or decrypt it. It can lose, delay, reorder or replay rows. A stolen relay
token can delete its fleet's queue; signed receipts and end-to-end loss detection
are not implemented. `sent` means that the relay accepted the ciphertext.

Run `pinky-relay --config /etc/ferry-relay/config.json --db /var/lib/ferry-relay/queue.db`
under a dedicated service account, with Python 3.12 or later. The listener refuses
any host other than `127.0.0.1`. The config must be an owner-owned regular 0600
file containing fleet names mapped to SHA256 hex digests of distinct bearer
tokens. Provision the clear tokens privately to their respective daemons.

The queue pins SQLite geometry and connection settings and refuses drift. Its
2 GiB ceiling includes DB, WAL and SHM, with dirty-page admission and observed
checkpoint peaks. Never edit the schema, run an external checkpoint, or open a
second writer. The source-bound derivation targets SQLite 3.45.1 and is also
checked against the local qualification version. At deployment, validate the
target build's hostile compile defaults on a newly created store: auto_vacuum 0,
page_size 4096, reserved bytes 0, secure_delete 0, temp_store MEMORY,
wal_autocheckpoint 0, and NO_CKPT_ON_CLOSE true. The target-default proof is a deploy
check; local tests do not claim to reproduce a different build's compile defaults.

## Reverse proxy

Use TLS at the proxy, strip the `/ferry-relay/` prefix, and overwrite X-Real-IP.
The ASGI app advertises `root_path=/ferry-relay`; daemon URLs retain that prefix.
The application also enforces its own 16 KiB total header limit.

```nginx
client_header_buffer_size 1k;
large_client_header_buffers 2 8k;

location /ferry-relay/ {
    client_max_body_size 400k;
    proxy_http_version 1.1;
    proxy_pass http://127.0.0.1:8898/;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header Host $host;
    proxy_read_timeout 45s;
    proxy_send_timeout 35s;
}
```

`GET /ferry-relay/health` returns only `{"ok":true}`. All other routes require a
bearer. Keep proxy/access logging free of authorization headers and request bodies.
The service disables raw uvicorn access logs and summarizes fixed refusal reasons.

```ini
[Service]
User=ferry-relay
Group=ferry-relay
WorkingDirectory=/var/lib/ferry-relay
ExecStart=/opt/ferry-relay/bin/pinky-relay --config /etc/ferry-relay/config.json --db /var/lib/ferry-relay/queue.db
Restart=on-failure
TimeoutStopSec=10
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/var/lib/ferry-relay
```

## Daemon provisioning

Generate an identity with `pinky ferry relay-keygen --fleet <fleet> --key-file <path>`.
It creates a new 0600 file exclusively and prints one public JSON line. It never
overwrites. `pinky ferry relay-fingerprint --key-file <path>` prints the same public
keys and full 32-hex fingerprint. Compare that full fingerprint with the other
operator out of band. Recomputing a configured fingerprint is a consistency check,
not identity verification. There is no discovery, TOFU or automatic rotation.

Configure the daemon with `PINKYBOT_FERRY_RELAY_URL`, `PINKYBOT_FERRY_RELAY_TOKEN`,
`PINKYBOT_FERRY_RELAY_KEY_FILE`, and `PINKYBOT_FERRY_RELAY_PEERS`. The peer map is
`{fleet: {signing_pk, encryption_pk, fingerprint}}` using the public JSON values.
Fleet names are canonical lowercase ASCII slugs. A peer is reserved for relay
routing even when its pin or local configuration is unusable. An overlapping
direct HTTP entry is ignored; relay failure never falls back to HTTP or NATS.
Non-reserved destinations retain the existing route. HTTPS is required except
for literal loopback HTTP tests. Redirects and compressed responses are refused.

The relay poller belongs to the main API lifecycle and runs independently of the
direct ferry listener. Its replay ledger is `ferry_relay_replay.db` in the daemon
data directory, cataloged as authoritative manual-recovery state. Do not delete,
rebuild or restore that ledger from a stale snapshot. Reconcile delivery history
before recovering a damaged ledger; doing otherwise can permit a second delivery.

The durable handoff marker precedes dispatch. Ambiguous results become UNKNOWN,
never an automatic second handoff. Only a specifically marked pre-dispatch failure
can retry, at most five attempts. Existing mesh audit storage is unchanged and
can contain plaintext, as it does for other ferry routes; the relay and replay
ledger never store message plaintext.

The admission floor advances only by monotonic receiver runtime after creation.
Clock rollback suspends claims, handoffs and acknowledgements. Long downtime
delays pruning safely. Inspect it with `pinky ferry relay-floor --show --store <db>`.
An informed operator may advance it with `--reset-to-now --confirm`; it never moves
backward. This command is the only wall-clock catch-up path.

SDK path: ferry secrets blanked; session secret inherited as before (legacy, out of scope).
The transport proof covers the main SDK child with the version check disabled;
it does not cover the fixed-argument version probe. Trusted helper subprocesses
are out of scope. Relay token and key-file path are daemon-only at the supported
agent launch boundaries, including retained tmux state and explicit grants.

Deployment, real token/key provisioning, out-of-band pin verification and target
startup validation remain operator actions.
