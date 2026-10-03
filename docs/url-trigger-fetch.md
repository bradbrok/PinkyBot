# URL trigger destination policy

URL watchers fetch public HTTP(S) destinations by default, without ambient proxies.
All resolved addresses must pass policy before any socket opens; each redirect is
checked again. Connections use the validated sockaddr, while HTTPS certificates
and SNI retain the URL hostname. Credentials and control characters are refused.

An operator can permit internal monitors with the daemon environment variable:

```sh
PINKY_URL_TRIGGER_ALLOW="100.64.0.25:8081,10.0.0.0/8:443"
```

The default is empty. Entries are comma-separated literal IP addresses or CIDRs,
with a required numeric port from 1 through 65535. IPv6 uses `[fd00::/8]:443`.
Hostnames are not accepted as policy entries: a watcher URL may use a hostname,
but its resolved socket addresses and ports must match the numeric policy.
An HTTPS default port is 443; an HTTP default port is 80.

Loopback and link-local addresses require an exact address (or /32 or /128)
and port; broad CIDRs cannot authorize them. Multicast and unspecified addresses
are always refused. Invalid entries are ignored with a WARNING naming their
position, once per configuration; valid entries still apply.

An internal refusal records the check without changing the previous value or
firing the trigger. The scheduler emits one WARNING per trigger ID per hour,
including the refused host:port (including redirect targets), with no response
body, URL path, query or credentials. This policy also applies to previously
stored watchers. The allowlist is daemon-wide operator configuration, not a
per-trigger setting; granting an endpoint makes it available to URL watchers.

Configure required internal exceptions before deploying the public-default
policy. The five-second socket timeout, 64 KiB response limit, and fetch on a
worker thread are unchanged.
