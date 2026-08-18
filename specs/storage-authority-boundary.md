# Storage authority boundary

The daemon centralizes all SQLite SQL/open authority in typed store owners.
Connector code consumes narrow repository methods; it does not open live store
files or expose generic SQL. `tests/test_no_direct_db_open.py` keeps
`DIRECT_OPEN_ALLOWLIST` literally empty. Boot preflight, snapshot, and attended
restore are separately named storage-authority mechanisms rather than connector
exceptions.

The only residual cross-process read is a stdio agent's typed, `mode=ro`
signing-key lookup. Every database reachable through that reader is
rollback-journal: the fleet `_agents.db` is `TRUNCATE`, and standalone tenant
keystores are `DELETE`. Boot preflight and the fail-soft reader inspect the raw
SQLite header before any `mode=ro` open and reject persistent-WAL drift without
recreating `-wal`/`-shm`; the scoped catalog records the observed mode rather
than assuming `DELETE`. Provisioning also requires a stable, owner-only `0700`
parent and verifies the exclusive-create inode before writing the secret. Those
boundaries keep external reads safe from the #889 checkpoint/unlink corruption
class and keep path substitution from redirecting a signing key.

Eliminating every external process file descriptor would be a stronger
isolation boundary. It would require a new authenticated secret/signing service
and materially enlarge the attack surface, so process-level FD isolation is an
explicit future option and is not part of P1/#1118.
