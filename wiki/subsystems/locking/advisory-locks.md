---
title: "Advisory Locks"
aliases:
  - "Advisory Locks"
  - "pg_advisory_lock"
  - "Application-level Locks"
tags:
  - theme/concurrency-control
source_files:
  - src/backend/utils/adt/lockfuncs.c
  - src/backend/storage/lmgr/lock.c
  - src/include/storage/lock.h
symbols:
  - pg_advisory_lock
  - pg_advisory_unlock
  - pg_advisory_xact_lock
  - LockAcquire
  - LOCKTAG_ADVISORY
---

# Advisory Locks

Advisory locks are application-level mutual exclusion primitives managed by PostgreSQL's lock manager. Unlike relation locks or row locks, advisory locks carry no automatic semantics: PostgreSQL acquires and releases them only when explicitly instructed. They are useful for coordinating application workflows, preventing duplicate job execution, and implementing application-level critical sections.

## Key space

Advisory locks are identified by one of two key forms:

| Form | SQL functions | Key fields |
|---|---|---|
| Single 64-bit integer | `pg_advisory_lock(bigint)` | Split into two 32-bit fields internally |
| Two 32-bit integers | `pg_advisory_lock(int, int)` | Stored directly as two fields |

The two forms use different `LOCKTAG` classids and **never conflict with each other**, even if the numeric values overlap. This allows different applications to use the same integer values safely.

## Lock modes

Advisory locks support two modes:

| Mode | Functions | Behaviour |
|---|---|---|
| Exclusive | `pg_advisory_lock` / `pg_advisory_xact_lock` | Only one holder at a time |
| Shared | `pg_advisory_lock_shared` / `pg_advisory_xact_lock_shared` | Multiple shared holders coexist; exclusive blocks shared and vice versa |

## Scope: session-level vs transaction-level

### Session-level

```sql
SELECT pg_advisory_lock(12345);
-- ... do work ...
SELECT pg_advisory_unlock(12345);
```

- Held until explicitly released or the session ends.
- **Reference counted**: calling `pg_advisory_lock` twice requires two `pg_advisory_unlock` calls to release.
- `pg_advisory_unlock_all()` releases all session-level advisory locks for the current session.
- Not released at transaction `COMMIT` or `ROLLBACK`.

### Transaction-level

```sql
BEGIN;
SELECT pg_advisory_xact_lock(12345);
-- ... lock held until end of transaction ...
COMMIT;  -- lock released automatically
```

- Held until end of the current transaction.
- No explicit unlock needed (and no `pg_advisory_xact_unlock` exists).
- Released on both `COMMIT` and `ROLLBACK`.
- Cannot be stacked within a transaction.

## Non-blocking variants

| Function | Returns |
|---|---|
| `pg_try_advisory_lock(key)` | `true` if acquired, `false` if would block |
| `pg_try_advisory_lock_shared(key)` | `true` if acquired, `false` if would block |
| `pg_try_advisory_xact_lock(key)` | `true` if acquired, `false` if would block |
| `pg_try_advisory_xact_lock_shared(key)` | `true` if acquired, `false` if would block |

These return immediately rather than waiting, allowing callers to implement try-lock patterns:

```sql
IF pg_try_advisory_xact_lock(job_id) THEN
    -- We got the lock, process the job
ELSE
    -- Another worker has this job
END IF;
```

## LOCKTAG structure

Advisory locks use `LOCKTAG_ADVISORY` (`locktag_lockmethodid = USER_LOCKMETHOD`):

| Field | Value |
|---|---|
| `locktag_field1` | Database OID (scopes the lock to the current database) |
| `locktag_field2` | High 32 bits of the key (or first int) |
| `locktag_field3` | Low 32 bits of the key (or second int) |
| `locktag_field4` | `1` = single-bigint form, `2` = two-int form |

Database-scoped: advisory locks taken in one database do not interfere with the same integer keys in another database.

## pg_locks view

Advisory locks are visible in `pg_locks`:

```sql
SELECT pid, locktype, classid, objid, mode, granted
FROM pg_locks
WHERE locktype = 'advisory';
```

| Column | Meaning |
|---|---|
| `locktype` | `'advisory'` |
| `classid` | High 32 bits of the lock key |
| `objid` | Low 32 bits of the lock key |
| `objsubid` | `1` (bigint form) or `2` (two-int form) |
| `mode` | `ExclusiveLock` or `ShareLock` |
| `granted` | `true` if held, `false` if waiting |

## Deadlock detection

Advisory locks fully participate in PostgreSQL's deadlock detector. If two sessions hold advisory locks and each waits for the other's lock, the deadlock detector will abort one of them with:

```
ERROR:  deadlock detected
```

## Common patterns

### Distributed job queue

```sql
-- Worker tries to claim a job atomically
SELECT pg_try_advisory_xact_lock(job_id)
FROM jobs
WHERE status = 'pending'
ORDER BY priority
LIMIT 1
FOR UPDATE SKIP LOCKED;
```

### Preventing concurrent migrations

```sql
-- Ensure only one schema migration runs at a time
SELECT pg_advisory_lock(hashtext('schema_migration'));
-- ... run migration ...
SELECT pg_advisory_unlock(hashtext('schema_migration'));
```

### Leader election

```sql
-- Session that acquires this lock is the leader
-- Shared lock: any number of followers
-- Exclusive lock: exactly one leader
SELECT pg_try_advisory_lock(42);
```

## See also

- [[subsystems/locking/overview]] — the lock manager that implements advisory locks
- [[subsystems/locking/row-level-locking]] — row-level locks for data-driven coordination
- [[code-paths/lock-table]] — explicit relation-level locking
