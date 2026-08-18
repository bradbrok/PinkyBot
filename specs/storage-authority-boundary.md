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
keystores are `DELETE`. Those modes do not use `-wal`/`-shm`, so an external
read cannot recreate or poison WAL sidecars and is safe from the #889
checkpoint/unlink corruption class.

Eliminating every external process file descriptor would be a stronger
isolation boundary. It would require a new authenticated secret/signing service
and materially enlarge the attack surface, so process-level FD isolation is an
explicit future option and is not part of P1/#1118.
