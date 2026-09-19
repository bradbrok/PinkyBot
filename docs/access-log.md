# HTTP access log

The daemon writes one private JSON line for every HTTP request handled by the main API app, including authentication denials and redirects. The optional ferry inbound listener is a separate app and is not covered; see [the listener access-log follow-up](https://github.com/bradbrok/PinkyBot/issues/1282). The separate MCP server is also outside this log. Real WebSocket scopes bypass HTTP middleware. Streaming responses record time to response headers, not stream duration. The request ID also appears in the `X-Request-ID` response header and webhook receipt messages.

The default file is `logs/access.log`, relative to the process working directory. An explicit `create_api(access_log_path=...)` takes precedence over `PINKY_ACCESS_LOG`, which takes precedence over the default. `PINKY_ACCESS_LOG=off` disables receipts and prints a startup notice. File and archive permissions are `0600`; the writer refuses a symbolic link or non-regular file at the live path.

```json
{"ts":"2026-09-19T16:41:03.217Z","rid":"19b1ec18f2474d688b7ea94579052705","crid":null,"peer":"127.0.0.1","port":8080,"xff":null,"ts_user":null,"method":"GET","path":"/agents","qk":[],"status":401,"dur_ms":0.8,"gate":"deny_protected_prefix","caller":"-","ua":"curl","upgrade":null}
```

| Field | Meaning |
|---|---|
| `ts` | UTC timestamp with milliseconds |
| `rid` | Server-generated UUID hex; never taken from a request header |
| `crid` | Client `X-Request-ID`, or null; only 1–64 characters in `[A-Za-z0-9_.-]` |
| `peer` | Socket peer address, or `-` |
| `port` | API listening port reported by ASGI, useful behind proxies and with non-default ports; null if unavailable |
| `xff` | Untrusted `X-Forwarded-For`, at most 256 characters, or null |
| `ts_user` | Untrusted `Tailscale-User-Login`, at most 256 characters, or null |
| `method` | HTTP method |
| `path` | Redacted path, at most 512 characters; no query string |
| `qk` | Sorted query parameter names; never values |
| `status` | HTTP status, or 500 if the application raised before headers |
| `dur_ms` | Time to response headers in milliseconds, rounded to one decimal |
| `gate` | Authentication decision below, or `unclassified` |
| `caller` | Authenticated internal caller (validated name), session user, or `-` |
| `ua` | User-Agent, at most 200 characters, or null |
| `upgrade` | Upgrade header, at most 32 characters, or null |
| `error` | Exception class when the application raised; otherwise absent |

Forwarded identity headers are observations, not authenticated identities. Bodies, cookies, authorization headers, signatures, query values, and raw path credentials are excluded.

## Authentication gates

| Gate | Decision |
|---|---|
| `public` | Public exact path or prefix |
| `internal_hmac` | Valid internal signature, admitted |
| `deny_isolation` | Valid signature, denied cross-resource access (403) |
| `session` | Valid signed session cookie |
| `redirect_login` | Unauthenticated protected HTML (307) |
| `deny_browser_api` | Unauthenticated browser-shaped API request (401) |
| `deny_protected_prefix` | Unauthenticated protected API prefix (401) |
| `deny_default` | Unmapped request denied in enforce mode (401) |
| `shadow_passthrough` | Unmapped request passed in shadow/off mode |

Auth-produced denials and redirects carry the standard security headers and timing header. An HTTP `Upgrade: websocket` header does not bypass authentication, timing, or security headers.

## Credential paths

| Input | Recorded path |
|---|---|
| `/hooks/{token}` | `/hooks/<redacted>` |
| `/a/{token}/...` | `/a/<redacted>/...` |
| `/p/{token}/unlock` | `/p/<redacted>/unlock` |
| `/ws/voice/{session_id}` | `/ws/voice/<redacted>` |

Redaction runs before path truncation. The console webhook access filter uses the same implementation.

## Rotation and failures

The existing log rotation loop checks every five minutes, rotating daily or at 200 MiB. Access log rotation is lossless across concurrent appends: rename the live file, switch the writer to a new private file, then gzip the closed old inode. The console `api.log` still uses copytruncate and retains its documented copy/truncate loss window. Neither mode promises durability against a power failure. A failed handoff restores the live pathname so the next rotation can retry. A raw archive left by an interrupted rotation is recovered on the next check, after reopening the live file; failed compression preserves the raw data.

| Environment variable | Default | Effect |
|---|---|---|
| `PINKY_ACCESS_LOG` | `logs/access.log` | Path, or `off` to disable |
| `PINKY_ACCESS_LOG_RETENTION_DAYS` | `90` | Archive retention, integer at least 1; invalid values fall back to 90 with a notice |
| `PINKY_LOG_ROTATION` | `on` | `off`, `0`, or `false` disables both rotation loops |

`GET /system/health` requires a session or internal signature and returns only:

```json
{"access_log":{"enabled":true,"path":"logs/access.log","write_failures":0}}
```

Write failures never fail the HTTP request. The single writer holds its lock through each append, completing partial writes before releasing it. If an append raises, it restores the file's preceding size and counts one failed receipt. If that rollback also fails, it disables the descriptor to prevent later records from joining the damaged tail. Each failed receipt increments the process-local counter, with a console warning at most once per minute. An initial open failure leaves the writer disabled and counts every missing receipt. The explicit `off` switch does not count failures. Shutdown cancels rotation, waits for active file work, and closes the descriptor.

## Queries

Denials in a UTC window:

```sh
jq 'select(.ts >= "2026-09-19T00:00:00.000Z" and .ts < "2026-09-20T00:00:00.000Z") | select(.gate | startswith("deny_"))' logs/access.log
```

Requests from one socket peer:

```sh
jq --arg peer '127.0.0.1' 'select(.peer == $peer)' logs/access.log
```

Requests carrying an Upgrade header:

```sh
jq 'select(.upgrade != null)' logs/access.log
```
