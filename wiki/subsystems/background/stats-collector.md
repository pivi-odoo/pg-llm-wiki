---
title: "Cumulative Statistics System"
aliases:
  - "Statistics Collector"
  - "pgstat"
  - "pg_stat_activity"
  - "pg_stat_bgwriter"
  - "pgstat_report_activity"
tags:
  - theme/observability
source_files:
  - src/backend/utils/activity/pgstat.c
  - src/backend/utils/activity/pgstat_relation.c
  - src/backend/utils/activity/pgstat_bgwriter.c
  - src/backend/utils/activity/pgstat_wal.c
  - src/backend/utils/activity/wait_event.c
  - src/include/pgstat.h
  - src/include/utils/wait_event.h
symbols:
  - PgStat_TableStatus
  - pgstat_report_activity
  - pgstat_report_wait_start
  - pgstat_report_wait_end
  - pgstat_report_stat
  - pgstat_fetch_stat_tabentry
  - PgBackendStatus
  - BackendStatusShmemSize
---

# Cumulative Statistics System

PostgreSQL's cumulative statistics system tracks per-object counters (rows inserted/updated/deleted/fetched, blocks read/hit, vacuum/analyze runs) and per-backend state (current query text, wait events, transaction start time). The data powers all `pg_stat_*` and `pg_statio_*` views.

The architecture changed significantly in PostgreSQL 15. Prior releases used a dedicated `stats collector` background process that received UDP messages and maintained counters in files. From PG 15 onward, each backend accumulates statistics directly in shared memory, eliminating the collector process and the associated message-passing latency.

## Architecture (PG 15+)

```
Backend 1 ──pgstat_report_*──► shared memory (dshash tables)
Backend 2 ──pgstat_report_*──► shared memory
Checkpointer ──────────────────►
WAL writer ─────────────────────►
                                  └── pg_stat_* views read directly
                                      via pgstat_fetch_*() functions
```

Each kind of object (table, function, WAL, [[subsystems/background/bgwriter|bgwriter]], archiver, etc.) has its own hash table in shared memory, called a `PgStat_Kind` entry, dedicated to that kind of object. The hash tables use dynamic shared memory (DSM) via `dshash`. They grow as new objects are tracked. They do not require pre-allocation.

## Per-backend status (BackendStatusArray)

Every backend has a fixed slot in `BackendStatusArray` (shared memory, `MaxBackends` entries), holding a `PgBackendStatus` struct:

| Field | Type | Purpose |
|---|---|---|
| `st_procpid` | `pid_t` | Backend PID; 0 = unused slot |
| `st_userid` | `Oid` | Authenticated role OID |
| `st_databaseid` | `Oid` | Database OID |
| `st_appname` | `char[]` | `application_name` GUC |
| `st_clientaddr` | `SockAddr` | Client IP/socket |
| `st_clienthostname` | `char[]` | Resolved client hostname |
| `st_activity_raw` | `char[]` | Current query text (up to `track_activity_query_size`) |
| `st_state` | `BackendState` | `STATE_IDLE`, `STATE_RUNNING`, `STATE_IDLEINTRANSACTION`, etc. |
| `st_wait_event_type` | `uint8` | Wait event category |
| `st_wait_event` | `uint32` | Specific wait event |
| `st_xact_start` | `TimestampTz` | Transaction start time |
| `st_activity_start` | `TimestampTz` | Current query start time |
| `st_state_start` | `TimestampTz` | Time of last state change |

Reading these slots populates `pg_stat_activity`. Reads are done under a lightweight spinlock per slot to avoid torn reads.

### pgstat_report_activity

Backends call `pgstat_report_activity(state, query)` at key lifecycle points:

| Call site | State written |
|---|---|
| Before executing a query | `STATE_RUNNING` + query text |
| After a query completes | `STATE_IDLE` + empty query |
| On `BEGIN` | `STATE_IDLEINTRANSACTION` |
| On error in transaction | `STATE_IDLEINTRANSACTION_ABORTED` |
| While waiting for a lock | unchanged (wait event is set instead) |

### Wait events

`pgstat_report_wait_start(uint32 wait_event_info)` and `pgstat_report_wait_end()` write directly to `MyProc->wait_event_info` in shared memory (no locking needed — only the owning backend writes this field). The wait event info word encodes both the event type and specific event:

```c
#define PG_WAIT_LWLOCK      0x01000000U
#define PG_WAIT_LOCK        0x03000000U
#define PG_WAIT_BUFFERPIN   0x04000000U
#define PG_WAIT_ACTIVITY    0x05000000U
#define PG_WAIT_CLIENT      0x06000000U
#define PG_WAIT_EXTENSION   0x07000000U
#define PG_WAIT_IPC         0x08000000U
#define PG_WAIT_TIMEOUT     0x09000000U
#define PG_WAIT_IO          0x0A000000U
```

The low 24 bits identify the specific event within its category (e.g. which [[subsystems/locking/lwlocks|LWLock]], which I/O operation). `pgstat_get_wait_event()` decodes `pg_stat_activity.wait_event_type` and `.wait_event` from this word.

## Per-table counters (PgStat_TableStatus)

Each backend tracks table-level statistics in a per-backend hash table (`tabstat_list`). The entry type is `PgStat_TableStatus`:

| Field | Counts |
|---|---|
| `t_counts.t_numscans` | Sequential and index scans started |
| `t_counts.t_tuples_returned` | Tuples returned by sequential scans |
| `t_counts.t_tuples_fetched` | Tuples fetched via index scans |
| `t_counts.t_tuples_inserted` | Rows inserted (`heap_insert`) |
| `t_counts.t_tuples_updated` | Rows updated (`heap_update`) |
| `t_counts.t_tuples_deleted` | Rows deleted (`heap_delete`) |
| `t_counts.t_tuples_hot_updated` | HOT updates (no index change needed) |
| `t_counts.t_delta_live_tuples` | Estimated change in live tuple count |
| `t_counts.t_delta_dead_tuples` | Estimated change in dead tuple count |
| `t_counts.t_changed_tuples` | Total row modifications since last analyze |
| `t_counts.t_blks_read` | Buffer misses (read from disk) |
| `t_counts.t_blks_hit` | Buffer hits |

Each backend accumulates these counters locally in `PgStat_TableStatus` during query execution. At transaction end (commit or abort), `pgstat_report_stat()` flushes them to the shared-memory dshash table under a per-object spinlock.

### Reporting on commit vs. abort

On commit, `pgstat_report_stat()` adds all accumulated counts to the global shared table. On abort, it reports only `t_delta_dead_tuples` (the aborted inserts that are now dead) and certain page-access counters; it discards insert/update/delete counts because the rows never became visible.

## Buffer manager statistics (pg_stat_bgwriter / pg_stat_io)

The background writer, checkpointer, and individual backends each report I/O statistics via `pgstat_count_buffer_*` macros and `pgstat_report_checkpointer()`. From PG 16, `pg_stat_io` provides a detailed breakdown by backend type, I/O object (relation, temp relation, WAL, etc.), and I/O operation (read, write, extend, fsync).

## WAL statistics (pg_stat_wal)

WAL writer and individual backends call `pgstat_report_wal()` when WAL is flushed. It accumulates:

| Counter | Meaning |
|---|---|
| `wal_records` | Total WAL records written |
| `wal_fpi` | Full-page images written |
| `wal_bytes` | Total WAL bytes |
| `wal_buffers_full` | Times WAL buffers were full (forced write) |
| `wal_write` | Number of WAL writes to disk |
| `wal_sync` | Number of WAL fsyncs |

## Resetting statistics

```sql
-- Reset all statistics for the current database
SELECT pg_stat_reset();

-- Reset a specific table's statistics
SELECT pg_stat_reset_single_table_counters('mytable'::regclass);

-- Reset shared-object statistics (bgwriter, WAL, archiver)
SELECT pg_stat_reset_shared('bgwriter');
```

`pg_stat_reset()` zeroes the dshash entries for all objects in the current database. Statistics are cumulative since the last reset; `stats_reset` timestamp columns record when the last reset occurred.

## Key views and their sources

| View | Source |
|---|---|
| `pg_stat_activity` | `BackendStatusArray` (shared memory, per-backend slot) |
| `pg_stat_user_tables` | dshash table for `PGSTAT_KIND_RELATION` |
| `pg_statio_user_tables` | same — block read/hit fields |
| `pg_stat_bgwriter` | `PGSTAT_KIND_BGWRITER` singleton entry |
| `pg_stat_wal` | `PGSTAT_KIND_WAL` singleton entry |
| `pg_stat_io` | `PGSTAT_KIND_IO` entries keyed by `(backend_type, io_object, io_context)` |
| `pg_stat_replication` | `WalSndCtlData` in shared memory (not pgstat) |
| `pg_stat_database` | `PGSTAT_KIND_DATABASE` entry per database |
| `pg_stat_user_functions` | `PGSTAT_KIND_FUNCTION` per function OID |

## See also

- [[architecture/process-architecture]] — background processes that report statistics
- [[subsystems/observability/overview]] — user-facing entry point: diagnostic query recipes and the `pg_stat_*` view catalog built on this subsystem
- [[subsystems/storage/buffer-manager]] — where `t_blks_read` / `t_blks_hit` counters originate
- [[subsystems/background/autovacuum]] — autovacuum uses `pg_stat_user_tables` to decide when to vacuum/analyze
- [[subsystems/wal/overview]] — WAL statistics sources
