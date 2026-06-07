---
title: "Diagnosing Lock Waits"
aliases:
  - "Lock Wait Troubleshooting"
  - "Blocking Queries"
  - "pg_locks Diagnosis"
tags:
  - symptom/lock-wait
  - symptom/deadlock
  - theme/concurrency-control
source_files:
  - src/backend/storage/lmgr/lock.c
  - src/backend/utils/adt/lockfuncs.c
  - src/backend/utils/adt/pgstatfuncs.c
  - src/backend/storage/lmgr/proc.c
  - src/include/storage/lock.h
symbols:
  - ProcSleep
  - XactLockTableWait
  - LOCKTAG
  - DeadLockCheck
  - pg_locks
  - pg_stat_activity
  - pg_blocking_pids
  - pg_cancel_backend
  - pg_terminate_backend
  - deadlock_timeout
  - lock_timeout
---

# Diagnosing Lock Waits

Lock waits are invisible to CPU profilers and query planners. A blocked backend consumes no CPU and makes no forward progress. It sleeps in the kernel inside `ProcSleep()`, on its personal latch, until a releasing backend wakes it. The only reliable way to find these waits is through `pg_stat_activity` and `pg_locks`.

For a conceptual grounding in how the lock manager works, see [[subsystems/locking/overview]]. For coverage of lock contention patterns and prevention strategies, see [[subsystems/locking/lock-contention-and-slow-queries]]. This page focuses on the hands-on diagnosis workflow: identifying what is blocked, tracing who is blocking it, understanding why, and deciding how to intervene.

## Identifying a Lock Wait

A backend waiting for a heavyweight lock shows `wait_event_type = 'Lock'` in `pg_stat_activity`. The `state` column will be `active` — PostgreSQL considers the query still running — but `query_start` will be receding into the past with no progress.

```sql
SELECT pid, state, wait_event_type, wait_event,
       now() - query_start AS wait_age,
       left(query, 120)    AS query
FROM pg_stat_activity
WHERE wait_event_type = 'Lock'
ORDER BY wait_age DESC;
```

The `wait_event` column names the specific lock tag type being waited on. The most common in OLTP workloads are:

| `wait_event` | What it means |
|---|---|
| `transactionid` | Waiting for a concurrent write transaction to commit or abort — the most frequent lock wait |
| `relation` | Waiting for a table-level lock (DDL, `LOCK TABLE`, or heavy lock escalation) |
| `tuple` | Two transactions competing to update the same row simultaneously at the heap level |
| `extend` | Competing to extend a relation file; usually short-lived |
| `advisory` | Application-level advisory lock contention |

Row-level writes produce `transactionid` waits. When a second transaction wants to update or delete a row already being modified, it calls `XactLockTableWait()` and blocks on the first transaction's XID. The wait resolves when the holder commits or rolls back. See [[subsystems/observability/wait-events]] for the full event taxonomy.

## Reading the Blocker Chain

`pg_blocking_pids(pid)` returns the PIDs immediately blocking a given backend. A single slow transaction can form a chain: A blocks B, B (still waiting) is itself the target of C's lock request. The following query exposes the full chain including the root cause:

```sql
WITH RECURSIVE lock_chain AS (
    -- Direct blockers of any waiting backend
    SELECT
        w.pid                                   AS waiting_pid,
        w.query                                 AS waiting_query,
        w.wait_event_type,
        w.wait_event,
        b.pid                                   AS blocker_pid,
        b.query                                 AS blocker_query,
        b.state                                 AS blocker_state,
        now() - w.query_start                   AS wait_age,
        1                                       AS depth
    FROM pg_stat_activity w
    JOIN LATERAL unnest(pg_blocking_pids(w.pid)) AS blocker_pid ON true
    JOIN pg_stat_activity b ON b.pid = blocker_pid
    WHERE w.wait_event_type = 'Lock'

    UNION ALL

    -- Follow the chain upward to find root blockers
    SELECT
        lc.waiting_pid,
        lc.waiting_query,
        lc.wait_event_type,
        lc.wait_event,
        b.pid,
        b.query,
        b.state,
        lc.wait_age,
        lc.depth + 1
    FROM lock_chain lc
    JOIN LATERAL unnest(pg_blocking_pids(lc.blocker_pid)) AS blocker_pid ON true
    JOIN pg_stat_activity b ON b.pid = blocker_pid
    WHERE lc.depth < 10
)
SELECT * FROM lock_chain ORDER BY wait_age DESC, depth;
```

When `pg_locks` detail is needed — to see exactly which object is contested and what lock mode is held versus waited — join it directly:

```sql
SELECT
    blocked_l.pid                    AS blocked_pid,
    blocked_a.query                  AS blocked_query,
    blocked_l.locktype,
    blocked_l.relation::regclass     AS locked_relation,
    blocked_l.mode                   AS waiting_mode,
    blocking_l.pid                   AS blocker_pid,
    blocking_a.query                 AS blocker_query,
    blocking_a.state                 AS blocker_state,
    blocking_l.mode                  AS held_mode,
    now() - blocked_a.query_start    AS wait_age
FROM pg_locks              blocked_l
JOIN pg_stat_activity      blocked_a  ON blocked_a.pid = blocked_l.pid
JOIN pg_locks              blocking_l ON  blocking_l.relation IS NOT DISTINCT FROM blocked_l.relation
                                      AND blocking_l.locktype = blocked_l.locktype
                                      AND blocking_l.granted
                                      AND NOT blocked_l.granted
JOIN pg_stat_activity      blocking_a ON blocking_a.pid = blocking_l.pid
WHERE NOT blocked_l.granted
ORDER BY wait_age DESC;
```

### Interpreting `pg_locks` columns

- **`locktype`** — the kind of object being locked: `relation`, `transactionid`, `tuple`, `extend`, `advisory`, etc. This maps to the `LOCKTAG` type in `lock.h`.
- **`granted`** — `true` if the backend currently holds the lock; `false` if it is waiting. Every waiting backend has a matching row with `granted = false`.
- **`mode`** — the lock mode requested or held (`AccessShareLock`, `RowExclusiveLock`, `AccessExclusiveLock`, etc.).
- **`relation`** — OID of the relation, for `locktype = 'relation'` or `'tuple'`. Cast to `regclass` for a human-readable name.
- **`transactionid`** — the XID of the transaction being waited for, for `locktype = 'transactionid'`.
- **`pid`** — the backend process ID; join with `pg_stat_activity` for query text and state.

To find the root blocker in a chain, look for the backend whose PID appears only in the blocker position — it has no row with `granted = false` of its own.

## Common Causes

**Idle-in-transaction sessions.** A backend in `state = 'idle in transaction'` holds all locks acquired during that transaction while doing nothing — waiting for an application to send `COMMIT`, stuck on an external HTTP call, or abandoned. These are the most dangerous blockers because the duration is unbounded. Detect them with:

```sql
SELECT pid, usename, application_name,
       now() - state_change AS idle_duration, query
FROM pg_stat_activity
WHERE state IN ('idle in transaction', 'idle in transaction (aborted)')
ORDER BY idle_duration DESC;
```

**DDL operations.** `ALTER TABLE`, `CREATE INDEX` (non-concurrent), `TRUNCATE`, `DROP TABLE`, and `REINDEX` all acquire `AccessExclusiveLock`. This lock mode conflicts with every other lock mode, including plain `SELECT`. Once a DDL statement enters the lock queue, it blocks all subsequent queries on the table — even reads — until it acquires and releases the lock. A single long-running `SELECT` that started before the DDL will hold up the entire queue. Use `CREATE INDEX CONCURRENTLY` to avoid this for index builds. It uses `ShareUpdateExclusiveLock`, which does not block reads or DML.

**Explicit `LOCK TABLE`.** Application code that issues `LOCK TABLE ... IN EXCLUSIVE MODE` or higher can park a relation under a heavy lock for the lifetime of a transaction. If that transaction is long or idle, every other backend needing the table queues up.

**Foreign key cascades.** Inserting into a child table acquires `RowShareLock` on the referenced parent row to prevent concurrent deletion from violating the constraint. Under high-volume inserts into a child table, this produces contention on a small set of parent rows. A concurrent `DELETE` or `UPDATE` on the parent escalates the conflict.

**[[subsystems/background/autovacuum|Autovacuum]] vs DDL.** Autovacuum holds `ShareUpdateExclusiveLock`. This does not conflict with DML, but it does conflict with `ShareRowExclusiveLock` and `AccessExclusiveLock`. A `CREATE TRIGGER` or `ALTER TABLE` can get stuck behind autovacuum, and vice versa. Autovacuum will cancel itself when blocked long enough (unless it is running to prevent [[subsystems/transactions/xid-wraparound|XID wraparound]]), but the window can be significant on large tables.

## Killing Blockers

Two functions are available, with different consequences for the blocker's connection:

- **`pg_cancel_backend(pid)`** sends `SIGINT` to the backend. At the next `CHECK_FOR_INTERRUPTS()` checkpoint, PostgreSQL cancels the query and returns an error to the client. The connection survives; the client can retry. Use this first.
- **`pg_terminate_backend(pid)`** sends `SIGTERM`. The backend exits and the connection closes. The client must reconnect. Use this when a cancel has no effect (for example, the backend is stuck in a non-interruptible system call or is in `idle in transaction` state with no query running to interrupt).

Both functions return `true` if the signal was sent and `false` if the PID is not a PostgreSQL backend. Neither is synchronous — check `pg_stat_activity` a moment later to confirm the backend is gone.

```sql
-- Cancel a specific blocking query
SELECT pg_cancel_backend(12345);

-- Terminate all idle-in-transaction sessions older than 5 minutes
SELECT pg_terminate_backend(pid)
FROM pg_stat_activity
WHERE state IN ('idle in transaction', 'idle in transaction (aborted)')
  AND state_change < now() - interval '5 minutes';
```

Before terminating a backend, record its `query` and `state` from `pg_stat_activity`. This lets you understand why it was stuck. Avoid mass termination without investigation — the cause may immediately recur.

## Deadlocks

A deadlock occurs when a cycle forms in the wait-for graph: transaction A holds a lock that B needs, and B holds a lock that A needs. No participant can proceed. For a thorough treatment of the detection algorithm, see [[subsystems/locking/deadlock]].

PostgreSQL does not run the deadlock detector continuously because traversing all lock table partitions under exclusive [[subsystems/locking/lwlocks|LWLocks]] is expensive. Instead, a backend that has been waiting for a lock arms a `DEADLOCK_TIMEOUT` timer (default 1 second). When the timer fires, the backend wakes from `ProcSleep()` and calls `DeadLockCheck()` in `deadlock.c`. If a cycle is confirmed, PostgreSQL designates the backend that triggered the check as the victim and sets its wait status to `PROC_WAIT_STATUS_ERROR`. `ProcSleep()` then raises:

```
ERROR:  deadlock detected
DETAIL:  Process 12345 waits for ShareLock on transaction 78901; blocked by process 67890.
         Process 67890 waits for ShareLock on transaction 12345; blocked by process 12345.
HINT:   See server log for query details.
SQLSTATE: 40P01
```

The server log includes each PID's active query at the time of detection. The DETAIL lines list the full cycle; read them to understand which resources are contested and in which order each transaction acquired them.

The most common deadlock pattern is two transactions locking the same two rows in opposite order. For example, a payment flow might lock `account 1` then `account 2`, while a concurrent flow locks `account 2` then `account 1`. The fix is to impose a canonical lock order, typically `ORDER BY id FOR UPDATE` in a single query, so both transactions always acquire row locks in the same sequence. See [[subsystems/locking/row-locking-patterns]] for the full `SELECT FOR UPDATE` pattern guide.

Foreign key chains can also produce deadlocks: an `UPDATE` on a parent row may acquire a lock on the child table's FK index entry, while a concurrent `INSERT` into the child table holds a lock on the parent row for the FK check.

`deadlock_timeout` and `lock_timeout` are independent. `deadlock_timeout` controls when the cycle-detection algorithm runs. `lock_timeout` is a hard deadline. If a backend cannot acquire the lock within the configured interval, PostgreSQL cancels it with SQLSTATE `55P03`, whether or not a deadlock exists. Set both to bound wait time.

## Prevention Patterns

The strategies here complement the deeper analysis in [[subsystems/locking/lock-contention-and-slow-queries]].

**Keep transactions short.** The longer a transaction holds locks, the wider its blast radius. Commit or roll back immediately after the last DML statement. Never hold an open transaction across network calls, user input, or background processing.

**Lock ordering conventions.** When a transaction must lock multiple rows, always acquire them in a consistent order (by primary key is the simplest choice). Consistent ordering prevents the opposite-order cycles that cause most application-level deadlocks.

**`lock_timeout` as a circuit breaker.** Setting `lock_timeout` at the session or role level converts indefinite waits into bounded errors the application can handle with retry logic:

```sql
SET lock_timeout = '5s';
-- or at the database level:
ALTER DATABASE mydb SET lock_timeout = '5s';
```

**`idle_in_transaction_session_timeout` as a backstop.** This setting automatically terminates sessions that hold transactions open without issuing queries:

```sql
ALTER DATABASE mydb SET idle_in_transaction_session_timeout = '2min';
```

**Advisory locks for application-level serialization.** When multiple application processes need to serialize access to a resource that does not map cleanly to a row — a cron job, a schema migration, a named critical section — use transaction-scoped advisory locks rather than polling a status column or fighting over a dedicated lock row:

```sql
-- Acquire; blocks until available; released automatically at commit
SELECT pg_advisory_xact_lock(hashtext('migration-job-42'));
```

Prefer `pg_advisory_xact_lock` over `pg_advisory_lock` because a crashed client cannot leak a transaction-scoped lock. See [[subsystems/locking/overview]] for the advisory lock implementation details.

**DDL safety in migrations.** Wrap schema changes with a short `lock_timeout` so they fail fast rather than queueing and blocking all subsequent queries:

```sql
SET lock_timeout = '3s';
ALTER TABLE orders ADD COLUMN processed_at timestamptz;
```

If the `ALTER` cannot acquire the lock within 3 seconds, retry after a pause rather than letting it sit in the queue and starve all readers.

## Related Topics

- [[subsystems/locking/row-level-locking|Row-Level Locking]] — how PostgreSQL implements per-row lock modes using multixact and tuple-level state, which underlies `transactionid` and `tuple` wait events
- [[subsystems/locking/advisory-locks|Advisory Locks]] — session- and transaction-scoped application locks used to serialize external critical sections without row contention
- [[subsystems/locking/predicate-locking|Predicate Locking]] — serializable isolation's phantom-prevention locks and how they interact with the heavyweight lock manager
- [[subsystems/transactions/select-for-update|SELECT FOR UPDATE]] — the full semantics of locking reads and how they generate the `RowShareLock` and `RowExclusiveLock` modes seen in `pg_locks`
- [[subsystems/catalog/ddl-locking|DDL Locking]] — how catalog changes acquire `AccessExclusiveLock` and why DDL statements queue behind long-running reads
- [[subsystems/background/autovacuum|Autovacuum]] — autovacuum's `ShareUpdateExclusiveLock` usage and the conditions under which it cancels itself to yield to DDL
- [[troubleshooting/slow-queries|Slow Queries]] — complementary diagnosis workflow for queries that are not blocked but are consuming excessive time or resources
- [[subsystems/locking/overview|Locking Subsystem Overview]] — the heavyweight lock manager: data structures, conflict table, fast-path locking
- [[subsystems/locking/deadlock|Deadlock Detection]] — the deadlock detection algorithm, soft resolution, victim selection
- [[subsystems/locking/row-locking-patterns|Row Locking Patterns]] — `SELECT FOR UPDATE`, `SKIP LOCKED`, and deadlock avoidance in DML
- [[subsystems/locking/lock-contention-and-slow-queries|Lock Contention and Slow Queries]] — common contention scenarios and prevention strategies
- [[subsystems/observability/pg-stat-activity|pg_stat_activity]] — reading backend states, detecting idle-in-transaction, killing queries
- [[subsystems/observability/wait-events|Wait Events]] — full wait event taxonomy and diagnostic patterns
