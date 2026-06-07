---
title: Autovacuum
aliases:
  - autovacuum daemon
  - autovacuum launcher
  - autovacuum worker
tags:
  - theme/vacuum-and-maintenance
  - symptom/bloat
  - symptom/xid-wraparound
source_files:
  - src/backend/postmaster/autovacuum.c
  - src/include/postmaster/autovacuum.h
symbols:
  - AutoVacLauncherMain
  - AutoVacWorkerMain
  - do_autovacuum
  - relation_needs_vacanalyze
  - table_recheck_autovac
  - VacuumUpdateCosts
  - AutoVacuumUpdateCostLimit
  - autovac_recalculate_workers_for_balance
  - WorkerInfoData
  - AutoVacuumShmemStruct
  - autovac_table
  - avl_dbase
  - AutoVacOpts
---

# Autovacuum

PostgreSQL's [[subsystems/transactions/mvcc|MVCC]] model never overwrites tuples in place. Every UPDATE produces a new tuple version and marks the old one dead; every DELETE marks a tuple dead without reclaiming its storage. Over time, dead tuples accumulate. This bloats tables and indexes and advances the transaction ID (XID) horizon ever closer to wraparound. A database left unmaintained long enough will exhaust the 32-bit XID counter, forcing a hard shutdown.

Manual `VACUUM` can address these problems, but a busy production system with dozens of tables receiving continuous writes cannot rely on a human operator to vacuum each table at the right frequency. Autovacuum automates three distinct maintenance tasks: reclaiming dead tuples, advancing the `relfrozenxid` horizon to prevent [[subsystems/transactions/xid-wraparound|XID wraparound]], and refreshing the planner statistics that `ANALYZE` produces. Because these tasks must happen continuously and adaptively, autovacuum is built into the postmaster as a permanent background subsystem rather than as an external cron job.

## The Launcher and Worker Architecture

Autovacuum separates scheduling from execution. The **autovacuum launcher** is a long-lived postmaster child that maintains a list of databases ranked by how urgently each needs attention. It never connects to a user database itself and never performs vacuum work; its job is to decide when to start a worker and which database that worker should target.

When the launcher determines that a database needs servicing, it cannot simply `fork()` a worker itself. A process that shares memory with the postmaster and uses [[subsystems/locking/lwlocks|LWLock]]s is vulnerable to corruption. That corruption could affect the shared memory segment itself. To preserve robustness, the launcher writes the target database OID into a shared memory slot (`AutoVacuumShmem->av_startingWorker`). It then sends `PMSIGNAL_START_AUTOVAC_WORKER` to the postmaster. The postmaster performs the actual `fork()`. It hands the new child off as a fully independent process. If the fork fails, the postmaster sets an `AutoVacForkFailed` flag in shared memory and signals the launcher. The launcher retries after a brief sleep (`autovacuum.c`).

An **autovacuum worker** connects to its assigned database. It scans `pg_class` and selects tables that need vacuuming or analysis. Multiple workers can run concurrently, up to `autovacuum_max_workers`. A single database can have more than one worker active at a time. Workers advertise the table they are currently processing via `WorkerInfoData.wi_tableoid` in shared memory; other workers check this before picking a table to avoid redundant work (though a small race window exists where two workers may still select the same table).

**PostgreSQL 18:** The new `autovacuum_worker_slots` GUC controls how many background worker slots are reserved for autovacuum independently of `autovacuum_max_workers`. Previously, `autovacuum_max_workers` determined the slot count directly, so changing it required a server restart. With the two parameters separated, an operator can adjust `autovacuum_max_workers` at runtime without a restart; `autovacuum_worker_slots` still requires one, but an operator only needs to set it to the maximum value ever needed.

### Shared Memory Layout

The shared memory region, `AutoVacuumShmemStruct`, is the communication backbone between the postmaster, launcher, and workers.

| Field | Purpose |
|---|---|
| `av_signal[]` | Atomic flags workers set to request rebalancing or report fork failure |
| `av_launcherpid` | PID of the launcher; workers signal it on exit |
| `av_freeWorkers` | Linked list of available `WorkerInfoData` slots |
| `av_runningWorkers` | Linked list of active worker slots |
| `av_startingWorker` | Pointer to the slot being handed to a new worker |
| `av_workItems[]` | Up to 256 pending work items (currently BRIN summarization requests) |
| `av_nworkersForBalance` | Worker count used to divide the I/O cost limit |

Each worker's slot is a `WorkerInfoData` struct:

| Field | Purpose |
|---|---|
| `wi_dboid` | Database this worker is assigned to |
| `wi_tableoid` | Table currently being vacuumed (protected by `AutovacuumScheduleLock`) |
| `wi_proc` | Pointer to the worker's `PGPROC`; NULL until the worker is running |
| `wi_launchtime` | Timestamp of launch; used to detect hung starting workers |
| `wi_dobalance` | Atomic flag: whether this worker participates in cost-limit balancing |
| `wi_sharedrel` | Whether `wi_tableoid` is a shared catalog |

## How the Launcher Schedules Work

The launcher maintains a `DatabaseList` sorted so that the database most overdue for a worker sits at the tail. At each wakeup it checks whether the tail entry's `adl_next_worker` timestamp has passed. If so, and if a free worker slot exists in `av_freeWorkers`, it launches a worker for that database.

The launcher calculates the sleep duration to wake up precisely when the next database becomes due. The minimum inter-launch interval is 100 ms; the maximum is capped at `MAX_AUTOVAC_SLEEPTIME` (300 seconds). If all worker slots are occupied the launcher sleeps for a full `autovacuum_naptime` and waits for a worker to finish.

When a worker exits, it sends `SIGUSR2` to the launcher. The launcher uses this signal to rebalance the I/O cost limits across the remaining workers and to launch a new worker immediately if the schedule demands it.

## Vacuum Thresholds

A worker connects to its assigned database. It scans `pg_class` to build a list of tables that need attention (`relation_needs_vacanalyze()`, `autovacuum.c`). The decision uses three independent conditions.

**Dead-tuple vacuum threshold.** A table is vacuumed when accumulated dead tuples exceed:

```
vacuum_threshold = autovacuum_vacuum_threshold + autovacuum_vacuum_scale_factor × reltuples
```

The fixed base (`autovacuum_vacuum_threshold`, default 50) handles tiny tables; the scale factor (`autovacuum_vacuum_scale_factor`, default 0.2) ensures autovacuum vacuums large tables proportionally. For a table with one million rows the threshold is roughly 200,050 dead tuples before autovacuum triggers.

**PostgreSQL 18:** The `autovacuum_vacuum_max_threshold` GUC adds a fixed upper cap on the dead-tuple count required to trigger vacuum. On very large tables the percentage-based formula can demand millions of dead tuples before autovacuum fires; `autovacuum_vacuum_max_threshold` bounds that count so that autovacuum triggers once dead tuples reach the cap regardless of table size. It is also available as a per-table storage parameter.

**Insert-driven vacuum threshold.** Since PostgreSQL 13, autovacuum also triggers on inserts alone, using a parallel threshold:

```
insert_threshold = autovacuum_vacuum_insert_threshold + autovacuum_vacuum_insert_scale_factor × reltuples
```

This matters for append-heavy tables, where few rows are ever updated or deleted. Even so, dead index entries from HOT updates can still cause index bloat. Page-pruning opportunities can still accumulate.

**Analyze threshold.** An `ANALYZE` pass runs when the number of modified tuples (inserts + updates + deletes) since the last analyze exceeds:

```
analyze_threshold = autovacuum_analyze_threshold + autovacuum_analyze_scale_factor × reltuples
```

The planner statistics produced by `ANALYZE` degrade as the table drifts from the state they measured. Without timely `ANALYZE` passes, join orders and index selection become increasingly wrong.

The statistics for `vactuples`, `instuples`, and `anltuples` come from the cumulative statistics system (`pgstat_fetch_stat_tabentry_ext()`). If a table has no pgstats entry, the worker skips it, unless anti-wraparound logic forces a vacuum.

## Anti-Wraparound Vacuums

PostgreSQL's XID counter is 32 bits wide. Once a table's `relfrozenxid` falls more than `autovacuum_freeze_max_age` transactions behind the current XID, autovacuum treats that table as needing a vacuum regardless of dead-tuple counts, the `autovacuum_enabled` reloption, or any other inhibition. The freeze limit is computed as:

```
xidForceLimit = recentXid - freeze_max_age
force_vacuum  = (relfrozenxid < xidForceLimit)
```

(`relation_needs_vacanalyze()`, `autovacuum.c`)

A worker processing a wraparound vacuum sets `VACOPT_SKIP_LOCKED = false` in its `VacuumParams`, meaning it will wait for locks rather than skip the table. This is intentional: a table that cannot be frozen because another session holds an incompatible lock is a table inching toward forced-shutdown territory.

An operator cannot disable anti-wraparound vacuums. Even if `autovacuum = off` is set, the postmaster will still start a single autovacuum worker in "emergency mode" when a database approaches the wraparound horizon (`AutoVacuumingActive()` returns false but `do_start_worker()` is called before the launcher exits). Workers also force `synchronous_commit = local` to ensure they cannot be blocked waiting for synchronous standbys when performing anti-wraparound work (`autovacuum.c`).

The multixact counter has its own parallel mechanism through `autovacuum_multixact_freeze_max_age`, because MultiXactId space can also wrap around independently of XID space.

**PostgreSQL 18:** Eager freeze allows normal (non-aggressive) vacuum to freeze some all-visible pages opportunistically, controlled by `vacuum_max_eager_freeze_failure_rate`. Previously only aggressive (anti-wraparound) vacuums would freeze pages in all-visible but not all-frozen ranges; eager freeze lets routine vacuums make incremental progress on freezing, reducing how often full aggressive freezes are needed.

## Per-Table Storage Parameters

An operator can override every threshold and delay that autovacuum uses, per table, through `ALTER TABLE ... SET (autovacuum_vacuum_threshold = ...)` and its siblings. When a worker calls `extract_autovac_opts()` it reads the `AutoVacOpts` sub-struct embedded in `pg_class.reloptions` for the target table. The per-table value wins; the cluster GUC is the fallback.

[[subsystems/storage/toast|TOAST]] tables inherit the parent table's storage parameters when they lack their own. The worker builds a `table_toast_map` hash table during its initial `pg_class` scan to track this relationship.

The complete set of overridable parameters per table:

| Storage parameter | Cluster GUC equivalent |
|---|---|
| `autovacuum_enabled` | `autovacuum` |
| `autovacuum_vacuum_threshold` | `autovacuum_vacuum_threshold` |
| `autovacuum_vacuum_scale_factor` | `autovacuum_vacuum_scale_factor` |
| `autovacuum_vacuum_max_threshold` | `autovacuum_vacuum_max_threshold` (PG18+) |
| `autovacuum_vacuum_insert_threshold` | `autovacuum_vacuum_insert_threshold` |
| `autovacuum_vacuum_insert_scale_factor` | `autovacuum_vacuum_insert_scale_factor` |
| `autovacuum_analyze_threshold` | `autovacuum_analyze_threshold` |
| `autovacuum_analyze_scale_factor` | `autovacuum_analyze_scale_factor` |
| `autovacuum_vacuum_cost_delay` | `autovacuum_vacuum_cost_delay` |
| `autovacuum_vacuum_cost_limit` | `autovacuum_vacuum_cost_limit` |
| `autovacuum_freeze_min_age` | `vacuum_freeze_min_age` |
| `autovacuum_freeze_max_age` | `autovacuum_freeze_max_age` |
| `autovacuum_multixact_freeze_max_age` | `autovacuum_multixact_freeze_max_age` |
| `log_autovacuum_min_duration` | `log_autovacuum_min_duration` |

## Cost-Based Throttling

Left unthrottled, autovacuum can saturate disk I/O and compete with user queries. The vacuum cost model counts each page read, each page hit in the buffer pool, and each dirty page write as a cost, then pauses whenever the accumulated cost exceeds a limit. This is the same mechanism used by manual `VACUUM`, but autovacuum uses its own separate limit parameters:

- `autovacuum_vacuum_cost_delay` (default 2 ms) — how long the worker sleeps when the cost bucket is full.
- `autovacuum_vacuum_cost_limit` (default -1, meaning use `vacuum_cost_limit`) — the bucket capacity.

When multiple workers are active simultaneously, the launcher divides their individual limits across all participating workers to prevent aggregate I/O from exceeding the intended cap. The launcher recalculates the per-worker limit whenever a worker starts or finishes, storing the participant count in `AutoVacuumShmem->av_nworkersForBalance`. `AutoVacuumUpdateCostLimit()` (`autovacuum.c`) excludes a worker that has per-table cost parameters set (`autovacuum_vacuum_cost_limit > 0` or `autovacuum_vacuum_cost_delay >= 0` in its reloptions) from the balance pool — such a worker uses its own fixed values and does not dilute the shared budget.

During an anti-wraparound vacuum the failsafe mechanism (`VacuumFailsafeActive`) disables cost-based throttling entirely, since PostgreSQL must process a table approaching XID wraparound without artificial delays.

## Two-Pass Table Selection

The worker's table-selection loop runs in two passes to handle TOAST correctly. In the first pass it scans `pg_class` for ordinary tables and materialized views, building the work list and also constructing a hash map from TOAST OID to parent table OID. In the second pass it processes TOAST tables, looking up the parent's `AutoVacOpts` for any TOAST table that lacks its own reloptions.

Before actually vacuuming a table the worker performs a recheck (`table_recheck_autovac()`): it re-reads `pg_class` and pgstats to confirm the table still needs work. Another worker may have vacuumed it in the window between the first scan and the lock acquisition. The worker also records the table's OID in `wi_tableoid` under `AutovacuumScheduleLock` before releasing the lock, so that other concurrent workers can skip it.

## Version History

**PostgreSQL 17:** VACUUM's internal TidStore — the data structure used to track dead tuple TIDs during a heap scan — switched from a flat array to an Adaptive Radix Tree (ART). The old flat array imposed a silent 1 GB cap on dead-tuple tracking even when `maintenance_work_mem` was set higher; the ART store allocates memory proportional to the actual number of dead tuples found, so `maintenance_work_mem` is now the effective limit without a hidden ceiling.

**PostgreSQL 17:** VACUUM combines the prune and freeze passes into a single heap scan rather than running them separately. This reduces the volume of WAL generated during vacuum, particularly on write-heavy workloads where WAL amplification from multiple passes over the same pages was measurable.

## Consequences of Neglect

When autovacuum is disabled or consistently unable to keep up, the effects compound:

- **Table and index bloat.** Dead tuples occupy space that cannot be reused until a vacuum reclaims it. Indexes retain entries pointing to dead heap tuples, growing without bound and degrading index scans.
- **Planner degradation.** Stale statistics cause the planner to misestimate row counts, leading to bad join orders and missed index opportunities.
- **XID wraparound shutdown.** PostgreSQL warns when a database approaches the wraparound horizon. It enters read-only mode when the database gets close enough. If vacuum does not advance `datfrozenxid`, the cluster will eventually refuse all writes. It will then require a manual `VACUUM FREEZE` run.

The `pg_stat_user_tables` view exposes the signals that indicate autovacuum stress: `n_dead_tup` shows the current dead-tuple count, `n_mod_since_analyze` shows how far the table has drifted from its last statistics snapshot, and `last_autovacuum` / `last_autoanalyze` show when autovacuum last serviced the table. Active autovacuum workers appear in `pg_stat_activity` with `backend_type = 'autovacuum worker'` and the target table name in the `query` column.

## Related Topics

- [[subsystems/transactions/xid-wraparound|XID Wraparound]] — the catastrophic outcome autovacuum's freeze logic exists to prevent, including the forced-shutdown mechanism
- [[subsystems/background/vacuum-tuning|Vacuum Tuning]] — practical guidance for adjusting autovacuum thresholds, cost limits, and per-table storage parameters
- [[troubleshooting/autovacuum-not-keeping-up|Autovacuum Not Keeping Up]] — decision tree and diagnostic queries for identifying why autovacuum is falling behind on a specific table
- [[subsystems/transactions/multixact|MultiXact]] — the parallel XID-like counter with its own freeze horizon that autovacuum manages alongside regular XIDs
- [[subsystems/storage/table-and-index-bloat|Table and Index Bloat]] — how neglected autovacuum leads to unbounded growth of dead-tuple storage
- [[subsystems/observability/pg-stat-all-tables|pg_stat_all_tables]] — the view exposing n_dead_tup, last_autovacuum, and other signals used to monitor autovacuum health
- [[subsystems/background/bgworker|Background Workers]] — the background worker infrastructure that autovacuum workers participate in
- [[troubleshooting/bloat|Bloat Troubleshooting]] — diagnosing and recovering from table and index bloat caused by insufficient vacuuming
- [[code-paths/vacuum|VACUUM Code Path]] — the VACUUM execution path that autovacuum workers invoke
- [[subsystems/transactions/mvcc|MVCC]] — why dead tuples exist and why they must be reclaimed
- [[subsystems/storage/buffer-manager|Buffer Manager]] — the buffer manager's role in I/O cost accounting
- [[subsystems/indexes/btree|B-tree Indexes]] — index bloat from unvacuumed dead tuples
- [[architecture/overview|Architecture Overview]] — the postmaster process tree that hosts the launcher and workers
