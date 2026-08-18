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
keystores are `DELETE`. Boot preflight and the fail-soft reader bind one
`O_NOFOLLOW` file descriptor, inspect the raw SQLite header through that
descriptor, and keep it as the sole physical identity through the read and
catalog lifetime. Tenant-capable preflight is descriptor-only and rejects
persistent-WAL drift before SQLite can resolve or recreate `-wal`/`-shm`.

Fleet WAL preflight is a distinct daemon-only catalog path because SQLite must
derive live WAL/SHM names from a filename. It is permitted only after every
component in the resolved data-directory chain is owner/mode checked against the
daemon trust boundary. That namespace check is the load-bearing swap defense;
an open-descriptor inventory corroborates the pinned main-file identity but is
only defense in depth because SQLite may reuse a prior same-inode descriptor.
Path comparisons are reject-only and never refresh or rebind an observation.

Standalone provisioning builds SQLite only in a daemon-owned `0700` staging
directory on the tenant home's filesystem, sets ownership and mode through file
descriptors, and publishes with an atomic dirfd-relative no-overwrite link. It
never opens SQLite or cleans up through a tenant-controlled target pathname.
These boundaries keep external reads safe from the #889 checkpoint/unlink
corruption class and keep path substitution from redirecting a signing key.

Eliminating every external process file descriptor would be a stronger
isolation boundary. It would require a new authenticated secret/signing service
and materially enlarge the attack surface, so process-level FD isolation is an
explicit future option and is not part of P1/#1118.
