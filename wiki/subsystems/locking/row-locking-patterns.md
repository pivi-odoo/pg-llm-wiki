---
title: Row Locking Patterns
aliases:
  - SELECT FOR UPDATE Patterns
  - Row Lock Strengths
  - FOR UPDATE SKIP LOCKED
tags:
  - symptom/lock-wait
  - symptom/deadlock
source_files:
  - src/backend/storage/lmgr/lock.c
  - src/backend/executor/nodeModifyTable.c
  - src/backend/access/heap/heapam.c
symbols:
  - heap_lock_tuple
  - LockTupleExclusive
  - LockTupleNoKeyExclusive
  - ExecLockRows
---

# Row Locking Patterns

PostgreSQL exposes four explicit row-lock strengths through the `SELECT ... FOR` syntax. Understanding which strength to use — and combining it with `NOWAIT` or `SKIP LOCKED` — is the difference between correct, low-contention code and mysterious deadlocks or performance cliffs.

## Lock Strengths (Weakest to Strongest)

PostgreSQL defines four row lock modes, ordered from weakest to strongest:

| Mode | Constant | Blocks |
|---|---|---|
| `FOR KEY SHARE` | `LockTupleKeyShare` | `FOR UPDATE`, `FOR NO KEY UPDATE` |
| `FOR SHARE` | `LockTupleShare` | `FOR UPDATE`, `FOR NO KEY UPDATE`, `FOR SHARE` (write-side) |
| `FOR NO KEY UPDATE` | `LockTupleNoKeyExclusive` | `FOR SHARE`, `FOR NO KEY UPDATE`, `FOR UPDATE` |
| `FOR UPDATE` | `LockTupleExclusive` | Everything |

`heap_lock_tuple` in `heapam.c` is the entry point that maps these modes to the underlying flags in the tuple header (`HEAP_XMAX_KEYSHR_LOCK`, `HEAP_XMAX_SHR_LOCK`, `HEAP_XMAX_EXCL_LOCK`, etc.). `ExecLockRows` in `nodeModifyTable.c` drives the per-row locking loop for `SELECT FOR` statements.

## SELECT FOR UPDATE

`FOR UPDATE` is the strongest row lock. It is exclusive: no other transaction can hold any row lock (including `FOR KEY SHARE`) on the same row.

```sql
BEGIN;
SELECT balance FROM accounts WHERE id = 42 FOR UPDATE;
-- row is now exclusively locked
UPDATE accounts SET balance = balance - 100 WHERE id = 42;
COMMIT;
```

The lock is held until the transaction commits or rolls back. Any other session that tries to lock or modify the same row will wait. Use `FOR UPDATE` when your subsequent `UPDATE` or `DELETE` will touch a primary key or unique key column. Also use it when you need absolute exclusion.

## FOR NO KEY UPDATE

`FOR NO KEY UPDATE` is slightly weaker than `FOR UPDATE`. It does **not** block `FOR KEY SHARE`. PostgreSQL uses `FOR KEY SHARE` when it checks a foreign key on a child row. This means a concurrent `INSERT` into a child table can proceed without being blocked by your lock.

```sql
BEGIN;
SELECT * FROM orders WHERE id = 101 FOR NO KEY UPDATE;
-- FK checks on order_items(order_id) can still proceed concurrently
UPDATE orders SET status = 'processing' WHERE id = 101;
COMMIT;
```

Prefer `FOR NO KEY UPDATE` over `FOR UPDATE` whenever the `UPDATE` does not modify the primary key or a column referenced by a foreign key. Reduced lock strength directly lowers contention with child-table writers.

## FOR SHARE and FOR KEY SHARE

Shared row locks allow multiple transactions to hold the same lock strength concurrently. They block modifying transactions but let other readers at the same strength coexist.

- **`FOR KEY SHARE`** — the weakest shared lock. Only blocked by `FOR NO KEY UPDATE` and `FOR UPDATE`. Used internally by FK enforcement on the *parent* side.
- **`FOR SHARE`** — blocks writers (`FOR NO KEY UPDATE` and above) while permitting concurrent `FOR SHARE` holders.

```sql
-- Two sessions can both hold FOR SHARE at the same time:
-- Session A
BEGIN;
SELECT * FROM products WHERE id = 7 FOR SHARE;

-- Session B (concurrent, does not block)
BEGIN;
SELECT * FROM products WHERE id = 7 FOR SHARE;
```

Use shared locks when you need a stable snapshot of a row for the duration of a business-logic check without fully excluding writers — for example, reading a price before applying a discount rule.

## NOWAIT

By default, a blocked lock request waits indefinitely (or until `lock_timeout` fires). `NOWAIT` converts a timeout into an immediate error, letting the caller retry on a shorter path.

```sql
BEGIN;
SELECT * FROM tasks WHERE id = 55 FOR UPDATE NOWAIT;
-- If already locked: ERROR: could not obtain lock on row in relation "tasks"
COMMIT;
```

Use `NOWAIT` in latency-sensitive code paths where waiting is worse than failing fast. Always pair it with retry logic at the application layer.

## SKIP LOCKED

`SKIP LOCKED` silently skips rows that are already locked instead of waiting or erroring. This is the canonical pattern for job queues and task dequeuing: multiple workers can each grab a different row without ever blocking each other.

```sql
-- Worker pattern: dequeue one pending job
SELECT id, payload
FROM jobs
WHERE status = 'pending'
ORDER BY id
LIMIT 1
FOR UPDATE SKIP LOCKED;
```

Each worker atomically claims a row that no other worker holds. This lets the pattern achieve parallel fan-out without a central coordinator or explicit partitioning. Combine with `LIMIT` to bound how much work each worker picks up per iteration.

## Safe Read-Modify-Write

Lost updates occur when two transactions each read a row, then both overwrite it — the second write silently discards the first. Explicit locking eliminates this:

```sql
BEGIN;

-- Lock the row before reading its value into application logic
SELECT quantity FROM inventory WHERE sku = 'ABC-1' FOR UPDATE;
-- application decides new quantity ...
UPDATE inventory SET quantity = 9 WHERE sku = 'ABC-1';

COMMIT;
```

With `FOR UPDATE`, the second transaction blocks on the `SELECT` until the first commits. After the block lifts, it re-reads the post-commit value rather than the stale snapshot. This makes the update safe under `READ COMMITTED`.

Without the explicit lock, even `REPEATABLE READ` can produce a write skew. This happens when two transactions both read the row, then each writes a different column based on what it read.

## Deadlock Avoidance

Deadlocks occur when two transactions each hold a lock that the other needs. PostgreSQL detects the cycle. It then cancels one victim with `ERROR: deadlock detected`.

The universal mitigation is **consistent lock ordering**: always acquire multiple row locks in the same canonical order (typically by primary key).

```sql
-- UNSAFE: Transaction A locks row 1 then 2; Transaction B locks row 2 then 1
-- They deadlock.

-- SAFE: both transactions lock in ascending id order
SELECT * FROM accounts
WHERE id = ANY(ARRAY[1, 2])
ORDER BY id
FOR UPDATE;
```

`ORDER BY id FOR UPDATE` in a single query is sufficient because PostgreSQL evaluates lock requests in the result order.

## MVCC Interaction

Row locks interact with MVCC visibility in a subtle way. PostgreSQL still takes the *snapshot* that determines which rows to return at the normal point (transaction start for `REPEATABLE READ`; statement start for `READ COMMITTED`). However, locking may cause the engine to re-evaluate a row after it acquires the lock.

Under `READ COMMITTED`:

1. The row is found using the statement's snapshot.
2. If the row is locked by another transaction, the current transaction waits.
3. After acquiring the lock, PostgreSQL re-checks the row's visibility against the *updated* committed state. It may find that the row changed or was deleted. Depending on context, it then reacts accordingly (re-fetch, skip, or error).

This "fetch then recheck" behavior (implemented in `heapam.c` around `heap_lock_tuple`) means that under `READ COMMITTED`, a `SELECT FOR UPDATE` always sees the most recently committed version of the row it locks. This holds even if that version postdates the statement start.

Under `REPEATABLE READ` or `SERIALIZABLE`, the engine raises an error if a concurrent committed transaction modified the locked row, rather than silently returning the updated version.

## Practical Guidance

- Default to `FOR NO KEY UPDATE` for most read-modify-write cycles; escalate to `FOR UPDATE` only when the update touches a PK or unique key.
- Use `FOR SHARE` / `FOR KEY SHARE` when you need row stability for a check without blocking concurrent child-table inserts or other read-side operations.
- Add `NOWAIT` to any lock acquisition on a latency-critical path; wrap calls in retry loops with exponential backoff.
- Use `SKIP LOCKED` for every job-queue or task-dispatch pattern; it is both simpler and faster than advisory locks or status-column polling for this use case.
- When locking multiple rows in one transaction, always impose a deterministic order (`ORDER BY id`) to prevent deadlocks.
- Set `lock_timeout` at the session or transaction level as a safety net even when not using `NOWAIT`.

## Related Topics

- [[subsystems/locking/row-level-locking|Row-Level Locking]] — covers the underlying tuple header flags and xmax machinery that row-lock strengths map onto.
- [[subsystems/locking/deadlock|Deadlock Detection]] — explains how PostgreSQL detects and resolves lock cycles, directly relevant to the consistent-ordering advice here.
- [[subsystems/locking/lock-contention-and-slow-queries|Lock Contention and Slow Queries]] — how to diagnose sessions blocked by row locks using `pg_stat_activity` and `pg_locks`.
- [[subsystems/locking/advisory-locks|Advisory Locks]] — an alternative coordination primitive useful when row-level granularity is not a natural fit.
- [[subsystems/transactions/mvcc|MVCC]] — the visibility model that row locks interact with, including the fetch-then-recheck behavior described above.
- [[subsystems/transactions/isolation-levels|Isolation Levels]] — determines whether a blocked `SELECT FOR UPDATE` silently re-reads a newer version or raises a serialization error.
- [[subsystems/transactions/select-for-update|SELECT FOR UPDATE]] — deep dive into how the executor processes `SELECT ... FOR` statements end-to-end.
