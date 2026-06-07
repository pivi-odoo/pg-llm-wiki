---
title: pg_stat_activity
aliases:
  - pg_stat_activity view
  - backend activity monitoring
tags:
  - symptom/lock-wait
  - symptom/slow-query
source_files:
  - src/backend/utils/adt/pgstatfuncs.c
  - src/backend/storage/lmgr/lock.c
symbols:
  - pg_stat_get_activity
  - pgstat_report_activity
  - pgstat_report_wait_start
  - pg_blocking_pids
  - PgBackendStatus
---

# pg_stat_activity

`pg_stat_activity` is the primary real-time window into what every server process is doing. Each row corresponds to one `PgBackendStatus` entry in shared memory, populated by `pgstat_report_activity()` and `pgstat_report_wait_start()` as backends execute. `pg_stat_get_activity()` in `pgstatfuncs.c` builds the view. It reads the shared-memory stats array and applies visibility checks (non-superusers see only their own processes plus public columns for others).

## Key Columns

| Column | Type | Notes |
|---|---|---|
| `pid` | int4 | OS process ID; target for `pg_cancel_backend` / `pg_terminate_backend` |
| `usename` | name | Role name executing the session |
| `application_name` | text | Set by client via `application_name` GUC or connection string |
| `client_addr` | inet | NULL for Unix-socket connections |
| `state` | text | Current backend state (see below) |
| `query` | text | Most recent (or current) SQL text; truncated to `track_activity_query_size` bytes |
| `query_start` | timestamptz | When the current query began; NULL if not in a query |
| `state_change` | timestamptz | When `state` last changed; critical for detecting stale sessions |
| `wait_event_type` | text | Category of the current wait, or NULL if running |
| `wait_event` | text | Specific event name within the category |
| `backend_type` | text | `client backend`, `autovacuum worker`, `walsender`, `background worker`, etc. |
| `leader_pid` | int4 | For parallel workers, the PID of the leader backend |

### State Values

- `active` — query is currently executing
- `idle` — waiting for a new command from the client
- `idle in transaction` — inside an open transaction, not currently executing; holds locks and prevents VACUUM
- `idle in transaction (aborted)` — transaction aborted, waiting for ROLLBACK; same dangers as above
- `fastpath function call` — executing a fast-path function
- `disabled` — `track_activities` is off for this backend

## wait_event_type Categories

`pgstat_report_wait_start()` writes a wait event code into `PgBackendStatus.st_wait_event_info`. The view decodes this into `wait_event_type` + `wait_event`.

| Type | Performance meaning |
|---|---|
| `Lock` | Blocked on a heavyweight lock (relation, row, advisory). Always visible in `pg_locks`. Indicates lock contention — find the holder. |
| [[subsystems/locking/lwlocks|LWLock]] | Waiting on an internal lightweight lock (buffer pin, WAL insert, relation extension, etc.). High frequency signals shared-memory bottlenecks. |
| `IO` | Waiting for a kernel I/O operation (read, write, fsync). Indicates storage pressure or missing indexes causing large scans. |
| `Client` | Waiting for the client to send the next query or read the result. Normal for idle connections; large result sets can stall here. |
| `IPC` | Waiting for a message from another backend: parallel query coordination, logical replication, bgworker startup. |
| `Timeout` | Backend is sleeping in a timeout (e.g., `lock_timeout`, `statement_timeout` arm). |
| `Activity` | Backend is sleeping waiting for work ([[subsystems/background/autovacuum|autovacuum]] launcher, WAL writer idle loop). |
| `BufferPin` | Waiting to acquire a buffer pin held by another process. |

## Detecting Blocked Queries

### Using pg_blocking_pids (preferred)

`pg_blocking_pids(pid)` returns an integer array of PIDs that are blocking the given PID. It inspects `pg_locks` internally and handles both heavyweight locks and advisory locks.

```sql
SELECT
    blocked.pid,
    blocked.usename,
    blocked.query                          AS blocked_query,
    blocked.state_change,
    pg_blocking_pids(blocked.pid)          AS blocked_by,
    (SELECT query FROM pg_stat_activity WHERE pid = ANY(pg_blocking_pids(blocked.pid)) LIMIT 1)
                                           AS blocker_query
FROM pg_stat_activity AS blocked
WHERE cardinality(pg_blocking_pids(blocked.pid)) > 0;
```

### Full Blocking Chain via pg_locks JOIN

For multi-level chains (A blocks B blocks C) you need a recursive query:

```sql
WITH RECURSIVE blocking_chain AS (
    SELECT
        blocked.pid,
        blocker.pid        AS blocker_pid,
        blocked.query      AS blocked_query,
        blocker.query      AS blocker_query,
        1                  AS depth
    FROM pg_stat_activity AS blocked
    JOIN pg_stat_activity AS blocker
        ON blocker.pid = ANY(pg_blocking_pids(blocked.pid))

    UNION ALL

    SELECT
        bc.pid,
        blocker.pid,
        bc.blocked_query,
        blocker.query,
        bc.depth + 1
    FROM blocking_chain bc
    JOIN pg_stat_activity AS blocker
        ON blocker.pid = ANY(pg_blocking_pids(bc.blocker_pid))
    WHERE bc.depth < 10
)
SELECT * FROM blocking_chain ORDER BY pid, depth;
```

## Detecting Long-Running Transactions

`idle in transaction` is the most dangerous state. The backend holds all locks acquired during the transaction, preventing VACUUM from cleaning up rows. This can also cause table bloat and lock queues.

```sql
-- Sessions idle in transaction for more than 5 minutes
SELECT
    pid,
    usename,
    application_name,
    client_addr,
    state,
    state_change,
    NOW() - state_change   AS idle_duration,
    query
FROM pg_stat_activity
WHERE state IN ('idle in transaction', 'idle in transaction (aborted)')
  AND state_change < NOW() - INTERVAL '5 minutes'
ORDER BY idle_duration DESC;
```

```sql
-- All queries running longer than 30 seconds
SELECT
    pid,
    usename,
    NOW() - query_start    AS running_time,
    wait_event_type,
    wait_event,
    left(query, 120)       AS query_snippet
FROM pg_stat_activity
WHERE state = 'active'
  AND query_start < NOW() - INTERVAL '30 seconds'
ORDER BY running_time DESC;
```

## Killing Queries Safely

Two functions terminate work with different severity:

- **`pg_cancel_backend(pid)`** — sends `SIGINT` to the backend. The backend's next CHECK_FOR_INTERRUPTS() point cancels the current query and returns an error to the client. The connection survives. Use this first.
- **`pg_terminate_backend(pid)`** — sends `SIGTERM`. The postmaster detects the exit and cleans up the connection. The client is disconnected. Use only if cancel is unresponsive.

Both return `bool`: `true` if the signal was sent, `false` if the PID does not belong to a PostgreSQL process. Neither is synchronous — the backend may not stop immediately.

```sql
-- Cancel query on pid 12345 (graceful)
SELECT pg_cancel_backend(12345);

-- Terminate all idle-in-transaction sessions older than 10 minutes
SELECT pg_terminate_backend(pid)
FROM pg_stat_activity
WHERE state IN ('idle in transaction', 'idle in transaction (aborted)')
  AND state_change < NOW() - INTERVAL '10 minutes';
```

## Defensive GUCs

Set these at the database or role level to prevent the conditions you would otherwise hunt for manually:

```sql
-- Kill queries that run longer than 30 seconds
ALTER DATABASE mydb SET statement_timeout = '30s';

-- Kill queries waiting more than 5 seconds to acquire a lock
ALTER DATABASE mydb SET lock_timeout = '5s';

-- Automatically terminate sessions idle in transaction for more than 2 minutes
ALTER DATABASE mydb SET idle_in_transaction_session_timeout = '2min';

-- Per-role override for ETL that legitimately needs longer transactions
ALTER ROLE etl_user SET idle_in_transaction_session_timeout = '30min';
```

`statement_timeout` and `lock_timeout` use the same interrupt machinery as `pg_cancel_backend`; the session receives a query-cancel error. `idle_in_transaction_session_timeout` terminates the connection (equivalent to `pg_terminate_backend`).

## Practical Guidance

**Establish a baseline.** Sample `pg_stat_activity` every 10–30 seconds into a monitoring table. Transient locks often resolve before you notice them; a time-series lets you distinguish spikes from sustained blockage.

**Check `wait_event_type` before reacting.** A query in `Lock` wait needs a different intervention (find and cancel the holder) than one in `IO` wait (consider index coverage) or `LWLock:WALInsertLock` (possible WAL write pressure).

**`idle in transaction` is always a bug.** Either the application is not committing/rolling back, or connection pooling is misconfigured. Set `idle_in_transaction_session_timeout` as a backstop and fix the application.

**`pg_cancel_backend` before `pg_terminate_backend`.** Cancel preserves the connection and lets the application handle the error. Terminate forces a reconnect, which can itself be expensive under high connection load.

**Correlate with `pg_locks`.** `wait_event_type = 'Lock'` guarantees there is a matching row in `pg_locks` with `granted = false`. Join on `pid` to see exactly which relation or tuple is contested.

**`backend_type` filters noise.** Autovacuum workers and WAL senders appear in `pg_stat_activity`. Filter to `client backend` when investigating application workload.

```sql
-- Full dashboard query: active, blocked, and idle-in-transaction in one pass
SELECT
    pid,
    backend_type,
    state,
    wait_event_type,
    wait_event,
    NOW() - COALESCE(query_start, state_change)   AS age,
    cardinality(pg_blocking_pids(pid))             AS blocked_by_count,
    left(query, 80)                                AS query
FROM pg_stat_activity
WHERE backend_type = 'client backend'
  AND state <> 'idle'
ORDER BY age DESC NULLS LAST;
```

## Related Topics

- [[subsystems/observability/wait-events|Wait Events]] — reference for every `wait_event_type` and `wait_event` value that appears in `pg_stat_activity`
- [[subsystems/observability/signal-functions|Signal Functions]] — documents `pg_cancel_backend`, `pg_terminate_backend`, and the signal machinery they invoke
- [[subsystems/observability/pgstat-backend|pgstat Backend State]] — explains the `PgBackendStatus` shared-memory structure that backs each row in the view
- [[subsystems/locking/lock-contention-and-slow-queries|Lock Contention and Slow Queries]] — practical patterns for diagnosing the `Lock` and `LWLock` wait events surfaced by `pg_stat_activity`
- [[subsystems/background/timeouts|Timeouts]] — covers `statement_timeout`, `lock_timeout`, and `idle_in_transaction_session_timeout` used as defensive GUCs
- [[subsystems/observability/process-title-resource-usage|Process Title and Resource Usage]] — complements `pg_stat_activity` with OS-level process visibility via `ps` and `pg_stat_get_backend_*` functions
- [[troubleshooting/lock-waits|Lock Waits]] — troubleshooting guide that uses `pg_stat_activity` and `pg_blocking_pids` as primary diagnostic tools
- [[subsystems/locking/overview|Locking Subsystem Overview]] — the heavyweight lock manager whose waiters show up as `wait_event_type = 'Lock'` rows in this view
- [[subsystems/observability/pg-stat-statements|pg_stat_statements]] — aggregates historical query statistics that complement the live, per-session view pg_stat_activity provides
- [[subsystems/transactions/transaction-lifecycle|Transaction Lifecycle]] — explains the `xact_start` and `state` transitions (active, idle, idle in transaction) reported for each backend
