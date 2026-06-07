---
title: Connection Pooling and Query Performance
aliases:
  - connection pooling
  - pgbouncer
  - connection overhead
tags:
  - symptom/connection-exhaustion
  - theme/observability
source_files:
  - src/backend/postmaster/postmaster.c
  - src/backend/storage/lmgr/proc.c
  - src/include/storage/proc.h
symbols:
  - BackendStartup
  - InitProcess
  - MaxBackends
  - NUM_LOCK_PARTITIONS
---

# Connection Pooling and Query Performance

PostgreSQL uses a process-per-connection model: every client connection spawns a separate OS process via `BackendStartup` in `postmaster.c`. This design is robust and isolates failures. However, it means connection count directly affects memory usage, OS scheduling overhead, and shared data structure contention. A connection pooler such as PgBouncer sits in front of PostgreSQL. It multiplexes many application connections onto a much smaller set of server connections.

Each PostgreSQL backend process consumes roughly 5–10 MB of resident memory at startup, allocated through `InitProcess` in `proc.c`. With 500 connections that figure alone reaches 2.5–5 GB before any query work is done.

Beyond raw memory, several shared structures scale with `max_connections`:

- **PGPROC array** — one slot per connection, controlled by `MaxBackends` in `proc.h`. PostgreSQL allocates the array in shared memory at startup. It cannot resize the array without a restart. Every snapshot acquisition, lock check, and visibility decision walks or locks part of this array.
- **Lock table partitions** — `NUM_LOCK_PARTITIONS` (default 16) LWLocks protect the main lock hash table. More concurrent transactions increase contention on those partitions, even when most backends are idle waiting for a client query.
- **ProcArray lock** — snapshot acquisition and transaction visibility checks require a shared lock on the ProcArray. With hundreds of processes, even a brief exclusive lock (such as the one taken by `VACUUM` when updating `pg_database.datfrozenxid`) causes a spike in wait times across all backends.

OS-level context switching adds further overhead: the kernel must schedule hundreds of processes even if nearly all are sleeping in `recv()`. On modern Linux this overhead is measurable once active backends exceed a few hundred. It grows non-linearly because the scheduler must keep all those processes runnable and manage their CPU time slices.

Idle connections are not free. They occupy a PGPROC slot and hold open file descriptors. They also keep WAL sender and [[subsystems/background/autovacuum|autovacuum]] bookkeeping active for their transaction IDs. `idle` in `pg_stat_activity` does not mean zero cost.

## Optimal Connection Count

Empirical benchmarks (including the original PgBouncer paper and TPC-B-style pgbench tests) consistently show that **throughput peaks at roughly 2–4x the number of CPU cores** for CPU-bound workloads. Beyond that inflection point, adding connections increases latency without improving throughput — you are paying context-switch and lock-contention costs for no gain.

For a 16-core server the sweet spot is typically 32–64 active server connections. IO-bound workloads allow a somewhat higher ratio because processes spend more time waiting for disk, freeing CPU for other backends. Even so, the shared-memory contention limits still apply.

A practical rule: size `max_connections` to the maximum number of connections you ever expect to be *active simultaneously*, not to the number of client application threads that might connect at any moment.

## PgBouncer Pooling Modes

PgBouncer multiplexes many client connections onto a smaller pool of server connections. It offers three pooling modes with different semantics and different constraints on what the application can do.

### Session Mode

PgBouncer assigns a server connection to a client when the client connects. It releases the connection only when the client disconnects. Prepared statements, `SET` commands, advisory locks, and temporary tables all work exactly as they would with a direct connection because the same server process handles the entire session.

Session mode provides modest benefit: it reduces the number of idle server connections. Clients that have connected but are not actively querying do not necessarily hold a server connection, if the pool size is smaller than the client count. It does not help with bursts of concurrent active connections.

### Transaction Mode

PgBouncer assigns a server connection only for the duration of a transaction. It returns the connection to the pool immediately after `COMMIT` or `ROLLBACK`. This enables a high client-to-server connection ratio — thousands of client connections can share tens of server connections as long as individual transactions are short.

Transaction mode imposes important restrictions:

**Prepared statements** — protocol-level prepared statements (`Parse`/`Bind`/`Execute`) live on a specific server connection. In transaction mode the next transaction may land on a different server process, where the prepared statement does not exist. Options:

- Set `plan_cache_mode = 'force_custom_plan'` on the server so applications using SQL-level `PREPARE`/`EXECUTE` re-plan every execution and avoid caching server-side state.
- Use PgBouncer's `max_prepared_statements` setting (PgBouncer 1.21+), which makes PgBouncer track and transparently re-issue `Parse` messages on whichever server connection handles each transaction.
- Switch to simple query protocol on the connection (not always possible from the driver side).

**`SET` commands and `search_path`** — `SET` modifies server session state. PgBouncer returns a connection to the pool after each transaction. Unless something resets the session state first, the next client inherits it. Mitigations:

- Configure `server_reset_query = DISCARD ALL` in `pgbouncer.ini` to reset session state before returning a connection to the pool. `DISCARD ALL` has a small but non-zero cost. On very high-frequency short transactions, it can be measurable.
- Set `search_path` at the pool or database level in `pgbouncer.ini` under the `[databases]` section, rather than issuing `SET search_path` inside each application session.
- For role-based schemas, configure `search_path` via `ALTER ROLE ... SET search_path = ...` in PostgreSQL so it applies at login time. `server_reset_query` then reinstates it afterward.

### Statement Mode

PgBouncer assigns a server connection for a single SQL statement. It releases the connection immediately afterward. Multi-statement transactions are broken: a `BEGIN` followed by a second statement may land on a different server connection, making the transaction invisible to it.

Statement mode is rarely appropriate. The only safe use case is fully stateless, autocommit, single-statement workloads where no transactional guarantees are required. Prefer transaction mode for the vast majority of deployments that need multiplexing.

## Sizing max_connections

With a connection pooler in front, you can and should size `max_connections` on PostgreSQL conservatively:

```
max_connections = (pool_size * pgbouncer_instances)
                + superuser_reserved_connections
                + background_worker_slots
                + replication_connections
```

For example: two PgBouncer instances each with `pool_size = 40`, `superuser_reserved_connections = 3`, a handful of background workers and one replication slot yields `max_connections = 100`, which is generous for most workloads.

Keep `max_connections` as low as safely possible. Every increment allocates more shared memory for the PGPROC array and lock table structures at server startup. With very high `max_connections` (for example 2000), you can exhaust shared memory before serving a single query, because PostgreSQL computes its shared memory requirement at startup based on `MaxBackends`. On Linux, `shmget` or `mmap` failures at startup are the result of this misconfiguration.

## Monitoring

**On PostgreSQL:**

```sql
-- Count connections by state and application
SELECT state, application_name, count(*)
FROM pg_stat_activity
GROUP BY state, application_name
ORDER BY count DESC;

-- Idle connections holding resources
SELECT count(*) FROM pg_stat_activity WHERE state = 'idle';

-- Long-running idle-in-transaction sessions (often a bug or forgotten transaction)
SELECT pid, now() - state_change AS idle_duration, left(query, 80) AS last_query
FROM pg_stat_activity
WHERE state = 'idle in transaction'
ORDER BY idle_duration DESC;
```

**On PgBouncer:**

```sql
-- Pool status: active, idle, waiting client count per pool
SHOW POOLS;

-- cl_waiting > 0 means clients are queued waiting for a server connection;
-- this is the primary alert signal for an under-provisioned pool_size.

-- Aggregate statistics per pool: requests, wait time, average query duration
SHOW STATS;

-- Per-second rates for the current interval
SHOW STATS_AVERAGES;
```

Key metrics to alert on:

| Metric | Source | Suggested threshold |
|---|---|---|
| `cl_waiting` | `SHOW POOLS` | > 0 sustained for > 5 s |
| `avg_wait_time` | `SHOW STATS` | > 5 ms (workload-dependent) |
| idle connection share | `pg_stat_activity` | > 50% of `max_connections` |
| `idle in transaction` duration | `pg_stat_activity` | > 30 s |
| `sv_idle` near 0 with `cl_waiting` > 0 | `SHOW POOLS` | increase `pool_size` |

## Practical Guidance

1. **Start small on `max_connections`.** A value of 100–200 with PgBouncer in front covers most applications. Increase only when monitoring shows sustained `cl_waiting` that cannot be resolved by shortening transaction duration.

2. **Use transaction mode by default.** Audit the application for prepared-statement and `SET` usage before enabling it. Both issues have well-understood solutions (`plan_cache_mode`, `server_reset_query`, pool-level `search_path`).

3. **Set `idle_in_transaction_session_timeout`** (for example `30s`) in `postgresql.conf` to automatically terminate sessions that linger inside an open transaction. These sessions hold locks and prevent autovacuum from advancing the horizon.

4. **Configure TCP keepalives** on both PgBouncer (`tcp_keepalive`, `tcp_keepidle`) and PostgreSQL (`tcp_keepalives_idle`, `tcp_keepalives_interval`) to detect dead clients promptly and return their server connections to the pool.

5. **Benchmark before tuning.** Use `pgbench -c N -j N` at varying concurrency levels to locate the throughput knee for your specific workload and hardware before settling on a `pool_size` value. The optimal point shifts with query complexity, IO pressure, and lock contention patterns.

6. **Separate pools by role.** If different application roles require different `search_path` or connection parameters, configure separate pool entries in `pgbouncer.ini`. Avoid relying on per-session `SET` commands that may leak across connections.

7. **Reserve superuser connections.** Always leave `superuser_reserved_connections = 3` (or more). This ensures administrative access remains possible even when the pool is saturated. A `max_connections`-exhaustion event otherwise locks out DBA intervention entirely.

## Related Topics

- [[architecture/process-architecture|Process Architecture]] — covers the process-per-connection model and how PostgreSQL forks a backend for each client, the root cause of connection overhead.
- [[architecture/postmaster-child|Postmaster and Child Processes]] — explains how the postmaster launches and manages backend processes, directly relevant to understanding connection startup cost.
- [[subsystems/locking/overview|Locking Overview]] — describes the lock table partitions and ProcArray contention that worsen under high connection counts.
- [[subsystems/locking/lwlocks|LWLocks]] — details the lightweight locks that protect shared structures such as the lock hash table and ProcArray. Pool sizing aims to keep these locks uncontended.
- [[subsystems/observability/pg-stat-activity|pg_stat_activity]] — the primary view for monitoring connection states, idle sessions, and idle-in-transaction sessions that pooling aims to eliminate.
- [[subsystems/observability/wait-events|Wait Events]] — shows contention signals (Lock, LWLock, Client) that reveal whether connection count or pool sizing is creating bottlenecks.
- [[troubleshooting/lock-waits|Lock Waits]] — practical guidance for diagnosing lock contention that is often amplified by excessive idle or idle-in-transaction connections.
- [[architecture/client-connection|Client Connection Architecture]] — describes the backend startup and authentication sequence that a pooler's persistent connections avoid repeating for every client request.
- [[subsystems/memory/contexts|Memory Contexts]] — the per-backend memory allocation model whose per-connection overhead pooling reduces by keeping fewer backends alive.
