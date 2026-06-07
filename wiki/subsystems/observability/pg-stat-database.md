---
title: "pg_stat_database"
aliases:
  - pg_stat_database view
  - database statistics
  - pgstat_database
  - pgstat_report_deadlock
  - pgstat_report_connect
tags:
  - symptom/corruption
source_files:
  - src/backend/utils/activity/pgstat_database.c
  - src/include/pgstat.h
symbols:
  - pgstat_report_connect
  - pgstat_report_disconnect
  - pgstat_report_deadlock
  - pgstat_report_tempfile
  - pgstat_report_recovery_conflict
  - pgstat_report_checksum_failure
  - pgstat_update_dbstats
  - pgstat_fetch_stat_dbentry
  - AtEOXact_PgStat_Database
  - PgStat_StatDBEntry
---

`pg_stat_database` is the view of aggregate statistics per database. Each row summarises cumulative activity for one database: transaction counts, cache hit rates, use of temporary files, deadlocks, recovery conflicts, and session timing. The numbers reset at cluster start or when someone calls `pg_stat_reset()` explicitly. The implementation lives in `src/backend/utils/activity/pgstat_database.c`.

## How Counters Accumulate

Most counters follow a two-phase commit model. Each backend accumulates changes in a local `PgStat_StatDBEntry` struct (the "pending" entry). At the end of each reporting cycle — triggered by `pgstat_report_stat()` — `pgstat_update_dbstats()` flushes the pending values into the stats entry in shared memory for the current database. The view reads this entry. This design avoids locking shared memory on every transaction.

Two counters bypass the pending stage and write directly to shared memory: `last_autovac_time` (written by [[subsystems/background/autovacuum|autovacuum]] on its own behalf) and `checksum_failures` / `last_checksum_failure` (written via `pgstat_report_checksum_failures_in_db()` — checksum failures are critical enough to need immediate visibility, even if the reporting backend crashes).

## Key Columns

| Column | Accumulation | Meaning |
|---|---|---|
| `xact_commit` | per transaction | Committed transactions |
| `xact_rollback` | per transaction | Rolled-back transactions |
| `blks_read` | per I/O | Blocks read from disk (buffer misses) |
| `blks_hit` | per I/O | Blocks found in shared buffers |
| `tup_returned` | per query | Rows scanned (seq scan total rows) |
| `tup_fetched` | per query | Rows fetched by index scans |
| `tup_inserted` / `_updated` / `_deleted` | per DML | Row modification counts |
| `temp_files` / `temp_bytes` | per spill | Temporary files created during query execution |
| `deadlocks` | immediate | Deadlocks detected in this database |
| `blk_read_time` / `blk_write_time` | per I/O | I/O time in milliseconds (requires `track_io_timing = on`) |
| `sessions` | per connect | Total connections to this database |
| `sessions_abandoned` | per disconnect | Connections lost due to client EOF |
| `sessions_fatal` | per disconnect | Connections terminated by fatal error |
| `sessions_killed` | per disconnect | Connections terminated by `pg_terminate_backend()` |
| `session_time` | per session | Total wall-clock time of all sessions |
| `active_time` | per session | Time sessions spent executing queries |
| `idle_in_transaction_time` | per session | Time sessions spent idle inside a transaction |

## Session Statistics

PostgreSQL reports session-level timing (`session_time`, `active_time`, `idle_in_transaction_time`, `sessions_*`) only for normal client backends. `pgstat_should_report_connstat()` excludes parallel workers and WAL sender processes; it checks `MyBackendType == B_BACKEND`. Parallel workers run inside the same wall-clock window as their leader and contribute CPU time, but attributing wall-clock session time to them would double-count. Walsenders have session characteristics too different from interactive backends to be meaningful in aggregate.

PostgreSQL measures session timing from the moment of connection (`pgstat_report_connect()`) and accumulates it in slices. Each call to `pgstat_update_dbstats()` measures elapsed time since the last report and adds it to `session_time`. The query executor and idle-in-transaction state machine maintain `pgStatActiveTime` and `pgStatTransactionIdleTime`.

## Recovery Conflicts

`pgstat_report_recovery_conflict()` increments conflict counters when a conflict with WAL replay causes PostgreSQL to cancel or terminate a hot standby process. Each conflict type has its own counter:

| `conflict_*` column | Cause |
|---|---|
| `tablespace` | A tablespace drop is being replayed |
| `lock` | A lock conflicts with a replaying lock |
| `snapshot` | A query's snapshot is too old for replay |
| `bufferpin` | A buffer pin prevents replay |
| `logicalslot` | A logical replication slot conflicts |
| `startup_deadlock` | Deadlock during startup |

PostgreSQL does not count database conflicts (`PROCSIG_RECOVERY_CONFLICT_DATABASE`), because the database is about to be dropped. The statistics entry itself will be removed.

## Temporary Files

PostgreSQL calls `pgstat_report_tempfile()` when it creates a temporary file during an on-disk spill (e.g. from a sort or hash join that exceeds [[subsystems/executor/work-mem-and-spill|work_mem]]). Each call adds the file size to `temp_bytes` and increments `temp_files`. A single query can generate multiple temporary files; PostgreSQL reports each one individually.

## Practical Use

```sql
-- Cache hit ratio per database (higher is better; aim for > 99%)
SELECT datname,
       blks_hit::float / NULLIF(blks_hit + blks_read, 0) AS hit_ratio,
       blks_read,
       blks_hit
FROM pg_stat_database
ORDER BY blks_read DESC;

-- Databases with high deadlock rates
SELECT datname, deadlocks,
       xact_commit + xact_rollback AS total_xacts,
       round(deadlocks::numeric / NULLIF(xact_commit + xact_rollback, 0) * 1000, 3)
           AS deadlocks_per_1000_xacts
FROM pg_stat_database
WHERE datname NOT IN ('template0','template1')
ORDER BY deadlocks DESC;

-- Session health by disconnect cause
SELECT datname, sessions,
       sessions_abandoned, sessions_fatal, sessions_killed,
       round(active_time / NULLIF(session_time, 0) * 100, 1) AS active_pct
FROM pg_stat_database
WHERE datname NOT IN ('template0','template1');

-- Reset stats for a specific database
SELECT pg_stat_reset();          -- current database
SELECT pg_stat_reset_shared('database');  -- all databases
```

## Related Topics

- [[subsystems/observability/overview|Statistics Collector Overview]]
- [[subsystems/observability/pgstat-shmem|Stats Shared Memory]]
- [[subsystems/observability/pgstat-xact|Stats Transactional Integration]]
- [[subsystems/observability/pg-stat-activity|pg_stat_activity]]
- [[subsystems/executor/work-mem-and-spill|work_mem and Spill to Disk]]
