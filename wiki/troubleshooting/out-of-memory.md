---
title: "Diagnosing Out-of-Memory Kills"
aliases:
  - "OOM killer PostgreSQL"
  - "backend killed signal 9"
  - "server process terminated abnormally"
  - "shared memory OOM"
  - "work_mem OOM"
tags:
  - symptom/out-of-memory
source_files:
  - src/backend/storage/ipc/shmem.c
  - src/backend/utils/mmgr/mcxt.c
  - src/backend/postmaster/postmaster.c
symbols:
  - CreateSharedMemoryAndSemaphores
  - MemoryContextAlloc
  - HandleChildCrash
  - max_connections
  - shared_buffers
  - work_mem
  - maintenance_work_mem
---

# Diagnosing Out-of-Memory Kills

When the Linux kernel's out-of-memory (OOM) killer terminates a PostgreSQL backend, the process disappears without sending a clean exit signal. The postmaster receives `SIGCHLD`, detects the abnormal exit in `HandleChildCrash()` (postmaster.c), and sends `SIGQUIT` to all other backends to protect shared memory consistency. This causes the entire cluster to restart as if it had crashed. The resulting log entry looks like a PostgreSQL crash but the root cause is OS-level memory exhaustion. Distinguishing an OOM kill from other crash causes, finding which memory consumer was responsible, and preventing recurrence are the three phases of diagnosis.

## Recognizing an OOM Kill

An OOM kill produces entries in two separate log streams that must be correlated.

**Kernel logs** (typically `/var/log/kern.log`, `/var/log/messages`, or readable via `dmesg`) contain the canonical OOM evidence:

```
kernel: Out of memory: Kill process 48291 (postgres) score 894 or sacrifice child
kernel: Killed process 48291 (postgres) total-vm:5242880kB, anon-rss:3145728kB,
        file-rss:0kB, shmem-rss:0kB
```

The `score` field is the OOM killer's priority score for that process — higher means more likely to be killed. The kernel kills the highest-scoring process in the cgroup. `anon-rss` shows private heap memory at the time of termination; `shmem-rss` shows shared memory mapped into the process (typically zero for PostgreSQL backends since shared_buffers is allocated separately via shmget/mmap and has different accounting).

**PostgreSQL logs** show the downstream effect of the kill — not the cause:

```
LOG:  server process (PID 48291) was terminated by signal 9: Killed
DETAIL:  Failed process was running: SELECT ... FROM large_table WHERE ...
LOG:  terminating any other active server processes
WARNING:  terminating connection because of crash of another server process
DETAIL:  The postmaster has commanded this server process to roll back the
         current transaction and exit, because another server process exited
         abnormally and possibly corrupted shared memory.
LOG:  all server processes terminated; reinitializing
```

The sequence `signal 9` followed by `reinitializing` is the OOM signature. A `signal 11` (SIGSEGV) or other signal indicates a different crash type. Confirm by checking whether the kernel log shows an OOM event at the same timestamp as the `signal 9` entry.

If kernel logs are inaccessible, `/proc/$(pid)/status` captures the virtual and resident memory of a running process before the OOM killer kills it. For forensics after the fact, examine PostgreSQL's `DETAIL` line for the query that was running — memory-intensive queries (large sorts, hash joins, or queries with many `work_mem`-consuming nodes) are common culprits.

## Memory Consumers

PostgreSQL's memory footprint has two distinct parts: shared memory allocated once at startup, and private per-process memory allocated by each backend.

**Shared memory** is allocated by `CreateSharedMemoryAndSemaphores()` (shmem.c) before any backend forks. Its primary component is `shared_buffers`, plus WAL buffers, the lock table, the procarray, and other fixed structures. Total shared allocation is typically `shared_buffers + 100–300 MB` depending on `max_connections` and lock table size. This memory maps into every backend's address space, but on most Linux configurations, the kernel accounts it to the postmaster in OOM score calculations.

**Per-backend private memory** is allocated independently by each forked backend. The main consumers:

| Consumer | Size | Notes |
|---|---|---|
| Backend overhead | ~5–10 MB | Stack, private metadata, catalog caches |
| `work_mem` | 0 to `work_mem` per sort/hash node | A single query with 5 hash joins can use 5× `work_mem` simultaneously |
| `temp_buffers` | 0 to `temp_buffers` | Only allocated if the session uses temporary tables |
| `maintenance_work_mem` | Up to `maintenance_work_mem` | VACUUM, CREATE INDEX, REINDEX — one slot per maintenance operation |
| Query plan memory | Variable | Execution state, tuple slots, expression contexts via `MemoryContextAlloc` (mcxt.c) |

The worst-case memory ceiling for a cluster is approximately:

```
shared_buffers
+ max_connections × (10 MB + work_mem × max_parallel_hash_joins_per_query)
+ autovacuum_max_workers × maintenance_work_mem
```

With `work_mem = 64MB`, `max_connections = 200`, and a query that runs 4 concurrent hash operations, a single backend can use `4 × 64MB = 256MB` of private memory. Two hundred such backends simultaneously would add 51 GB of private memory on top of shared_buffers.

**Parallel workers** multiply this further. Each parallel worker process is a separate forked backend with its own private memory. A query using `max_parallel_workers_per_gather = 4` can spawn 4 additional backends, each with its own `work_mem` allocation.

## Identifying the Culprit

The PostgreSQL log's `DETAIL` line naming the failed query is the starting point. Cross-reference with `pg_stat_statements` to find the query's typical buffer usage and mean execution time — memory-intensive queries tend to have high `temp_blks_written`.

For a running system that is approaching memory limits before an OOM event, track memory usage across active backends:

```sql
-- Queries with significant temp file usage (proxy for work_mem spill)
SELECT pid, query_start,
       temp_blks_written,
       round(mean_exec_time::numeric, 1) AS mean_ms,
       left(query, 100)                  AS query
FROM pg_stat_activity a
JOIN pg_stat_statements s USING (queryid)
WHERE temp_blks_written > 0
ORDER BY temp_blks_written DESC
LIMIT 20;
```

Queries with large `temp_blks_written` were close to exhausting `work_mem` and spilled to disk — they are candidates for high memory usage if `work_mem` is raised without bounds.

On Linux, the resident set size of each backend is visible in `/proc/<pid>/status`:

```bash
grep VmRSS /proc/$(pgrep -f "postgres: user") 2>/dev/null
```

Or via a one-liner that correlates PIDs with `pg_stat_activity`:

```sql
-- Get PIDs of currently active backends
SELECT pid, usename, left(query, 60) AS query
FROM pg_stat_activity
WHERE state = 'active';
```

Then read `/proc/<pid>/status` for each. A backend with `VmRSS` approaching several gigabytes is a candidate.

## Remediation

**Reduce `work_mem`.** The most common and most impactful change. Many systems run with `work_mem` that was set optimistically without accounting for concurrency. A value of 4–16 MB is appropriate for most OLTP workloads; analytical queries that genuinely need more memory benefit from setting `work_mem` at the role or session level rather than globally:

```sql
-- Global: conservative default
ALTER SYSTEM SET work_mem = '16MB';

-- Role-level: larger for analytics
ALTER ROLE analyst SET work_mem = '256MB';
SELECT pg_reload_conf();
```

**Limit `max_connections`.** Fewer connection slots means less total per-backend memory in the worst case. Combine with connection pooling to keep client connection capacity while reducing server connection count. See [[troubleshooting/connection-exhaustion]] for the connection management workflow.

**Reduce `maintenance_work_mem` or stagger maintenance operations.** If OOM kills line up with maintenance windows that run multiple `VACUUM`, `CREATE INDEX`, or `pg_dump` operations at once, each of those operations consumes its own `maintenance_work_mem`. Stagger the operations or reduce the per-operation limit:

```sql
ALTER SYSTEM SET maintenance_work_mem = '256MB';  -- default is 64MB
SELECT pg_reload_conf();
```

**Enable huge pages.** PostgreSQL can use Linux huge pages (2 MB pages instead of 4 KB) for `shared_buffers`. Huge pages reduce the kernel's page-table overhead for the shared memory mapping. This overhead becomes significant when `shared_buffers` is tens of gigabytes. More importantly, the Linux kernel typically excludes huge pages from OOM score calculations because it cannot easily reclaim them — they are effectively wired. This can reduce the OOM score for the postmaster process and make PostgreSQL less likely to be targeted:

```
# postgresql.conf
huge_pages = on   # requires nr_hugepages configured in /etc/sysctl.conf
```

```bash
# Compute required huge pages (shared_buffers / 2MB, rounded up)
# Set in /etc/sysctl.conf:
vm.nr_hugepages = 16384   # for 32 GB shared_buffers
```

**Adjust kernel overcommit settings.** Linux defaults to `vm.overcommit_memory = 0` (heuristic overcommit). This setting allows most allocations to succeed even when physical memory plus swap would not cover them all. PostgreSQL's `shared_buffers` allocation via `shmget` succeeds under any overcommit mode because it does not use virtual memory that can be overcommitted. However, per-backend `malloc` calls (underlying mcxt.c allocations) are subject to overcommit. Setting `vm.overcommit_memory = 2` disables overcommit entirely: allocations fail immediately if memory is unavailable, rather than succeeding and later triggering an OOM kill. This surfaces the problem as an error to the application rather than a crash of the database cluster. The trade-off is that some allocations that would have succeeded under normal conditions (due to memory being freed before the committed but unused pages were actually accessed) will now fail.

## Prevention

**Monitor RSS headroom.** The most reliable signal of impending OOM is the gap between total committed memory and available physical memory plus swap. Alert when available memory drops below 20% of total RAM.

**Size `shared_buffers` conservatively.** The common guideline of 25% of RAM is a starting point, not a ceiling. On a system with many concurrent connections and high `work_mem`, size `shared_buffers` to leave room for per-backend private memory at peak concurrency. On a dedicated database server with 64 GB RAM, `shared_buffers = 8–16 GB` leaves more room than 16 GB, without significantly hurting buffer pool hit rates if you set `effective_cache_size` accurately (which tells the planner about OS page cache but does not allocate memory).

**Bound concurrent maintenance.** Use a job scheduler that limits how many `VACUUM ANALYZE`, `CREATE INDEX`, or `pg_dump` jobs run simultaneously. Three jobs each using 1 GB `maintenance_work_mem` is 3 GB of additional memory that appears only during maintenance windows.

**Add swap.** PostgreSQL cannot swap shared memory, but it can swap per-backend private memory. A swap space of 8–16 GB provides a buffer that prevents OOM kills during transient memory spikes at the cost of degraded performance while swapping occurs. This buys time for investigation rather than serving as a permanent solution.

## See Also

- [[architecture/process-architecture|Process Architecture]] — how the postmaster forks backends and handles abnormal child exits via HandleChildCrash
- [[subsystems/storage/shared-memory|Shared Memory Internals]] — how shared_buffers and other shared structures are allocated at startup
- [[subsystems/memory/contexts|Memory Contexts]] — how PostgreSQL manages per-backend memory via the MemoryContext hierarchy
- [[subsystems/executor/work-mem-and-spill|work_mem and Spill]] — how work_mem is consumed by sort and hash join operations and when they spill to disk
- [[troubleshooting/connection-exhaustion|Connection Exhaustion]] — reducing max_connections to bound per-backend memory in the worst case

## Related Topics

- [[subsystems/memory/contexts|Memory Contexts]] — the MemoryContext hierarchy that governs per-backend allocation via MemoryContextAlloc
- [[subsystems/executor/work-mem-and-spill|work_mem and Spill]] — how a single query can consume multiple work_mem slots across parallel hash and sort nodes
- [[subsystems/executor/parallel|Parallel Query]] — each parallel worker is a separate backend with its own private memory budget
- [[subsystems/observability/pg-stat-statements|pg_stat_statements]] — use temp_blks_written to identify queries close to exceeding work_mem
- [[subsystems/background/autovacuum|Autovacuum]] — autovacuum workers each consume maintenance_work_mem and contribute to peak memory usage
- [[subsystems/executor/joins|Hash Join]] — the executor node most likely to drive large work_mem allocations under concurrent load
