# Issue: Fix conversations_skills.db WAL deletion + operational rule for DB queries

**Date:** 2026-07-30  
**Reporter:** engineer  
**Status:** Fix ready, deployment pending maintenance window

## Problem

During 2026-07-30 investigation, 17 daemon DB files had their `-wal` and `-shm` files deleted while the daemon process held them open (fd marked `(deleted)`). This caused:

1. **Data visibility loss**: External readers (e.g. `sqlite3` from shell) saw stale data from the last checkpoint, not live daemon writes
2. **Data loss risk**: Any writes after last checkpoint would be lost on daemon crash/SIGKILL

**Status after restart (20:05 UTC):**
- 16/17 DBs recovered with clean WAL files on disk
- `conversations_skills.db` remains broken (fd deleted, no WAL on disk)
- `conversations_agents.db` was immune (already using `journal_mode=TRUNCATE` per #797/#220)

## Root Cause (Hypothesis)

**Correlational evidence**, not experimentally verified:
- External `sqlite3` shell reads may trigger checkpoint + WAL unlink when connection closes
- The only DB queried repeatedly via shell during investigation is the only one that re-broke post-restart
- Clean verification would require intentionally breaking a production DB (not done)

**Operational rule adopted regardless of exact mechanism:**
> **Zero `sqlite3` queries from shell on daemon DBs while daemon runs. Verify only via signed API.**

This rule applies to all agents (Satoshi, Fixer, Sentinel, etc.) and should be propagated fleet-wide.

## Fix Implemented (Not Yet Deployed)

**Code:** `src/pinky_daemon/skill_store.py`
- Replace `PRAGMA journal_mode=WAL` with `_configure_skills_db_connection()`
- Mirrors `_configure_agents_db_connection()` pattern: busy_timeout, WAL→TRUNCATE checkpoint if needed, `journal_mode=TRUNCATE`, bounded retry, LOUD failure
- In-place conversion of existing WAL DB on first run
- New exception type: `SkillDbConfigError`

**Tests:** `tests/test_skills_db_no_wal.py` (5 tests, green)
- No -shm created
- In-place WAL→TRUNCATE conversion
- Assignment visible to independent reader without sidecar
- LOUD failure on config error
- Retry-then-success on transient lock

**Test suite:** All skill tests green (test_skill_store.py, test_core_skill_guard.py: 42 total)

**Trade-off:** Rollback journal serializes writes (less concurrency), but on skill assignment DB (rare writes, frequent reads) robustness >>> performance.

## Deployment Plan

- [x] Fix implemented by @engineer
- [x] Tests written and passing
- [ ] **Restart postponed** to maintenance window (avoid UX disruption for new Hydra Manager agent)
- [ ] Pre-flight checklist before restart:
  ```bash
  # 1. Check for hung connections (causes 90s timeout → SIGKILL → data loss)
  ss -tnp | grep 8888
  
  # 2. Verify hydra skills present via API
  curl -s http://localhost:8888/agents/hydra/skills | jq -r '.[].name'
  
  # 3. Restart
  restart_daemon()
  ```

## Maintenance Windows (Candidates)

- Tomorrow morning < 07:00 CEST
- On explicit "not using anything" from owner
- Next planned deploy

## Current Mitigation

Until deployment:
- Hydra Manager skills live only in deleted WAL (survive while daemon lives)
- Manual reassignment via API needed after each crash/unclean restart
- Fleet observes new rule: no shell sqlite3 queries on daemon DBs

## References

- #797, #220: `conversations_agents.db` TRUNCATE mode (validated, immune to this issue)
- Garmin #52: unrelated, watchdog stable

## Action Items

- [ ] Propagate operational rule to Fixer, Sentinel (Satoshi notifies)
- [ ] Execute restart during next maintenance window
- [ ] Verify conversations_skills.db has clean WAL on disk post-restart
- [ ] Monitor for recurrence
