---
title: Vacuum Tuning
aliases:
  - autovacuum tuning
  - vacuum configuration
  - autovacuum performance
tags:
  - theme/vacuum-and-maintenance
  - symptom/bloat
  - symptom/xid-wraparound
source_files:
  - src/backend/postmaster/autovacuum.c
  - src/backend/commands/vacuum.c
  - src/backend/access/heap/heapam_handler.c
symbols:
  - AutoVacWorkerMain
  - do_autovacuum
  - vacuum_rel
  - vac_update_relstats
---

# Vacuum Tuning

Autovacuum is one of the most operationally significant background subsystems in PostgreSQL. Mistuning it is a leading cause of table bloat, degraded query plans, and — in the worst case — a full database outage from XID wraparound. This article covers what autovacuum actually does, how to detect when it is falling behind, and the GUC knobs and per-table overrides that control its behavior.

## What Autovacuum Does

Autovacuum runs `do_autovacuum` in a dedicated worker process (`AutoVacWorkerMain`) to perform four distinct jobs on each relation it visits:

**Dead tuple reclamation.** MVCC writes new row versions on UPDATE and DELETE rather than modifying in-place. Old versions remain visible to concurrent snapshots; once all snapshots that could see them are gone, they become dead tuples. `vacuum_rel` scans the heap, marks dead item pointers as reusable, and updates the [[subsystems/storage/fsm|free space map]] so subsequent INSERTs can fill the reclaimed space. Without this, heap files grow without bound.

**Visibility map maintenance.** Vacuum marks a heap page as all-visible in the visibility map when every live tuple on the page is visible to all current and future transactions. Index-only scans rely on this bit: if a page is not all-visible, the executor must fetch the heap tuple to verify visibility. This degrades the scan to a regular index scan with heap fetches. Vacuum also marks pages all-frozen, eliminating future freeze work. `heapam_handler` sets these bits during the heap scan.

**Planner statistics refresh.** `vac_update_relstats` writes updated `relpages` and `reltuples` values to `pg_class`. The planner uses these counts for row-count estimates and cost calculations. A table that has grown significantly since its last autovacuum or ANALYZE carries stale statistics, leading to poor plan choices. Autovacuum triggers ANALYZE separately, governed by `autovacuum_analyze_scale_factor`.

**XID wraparound prevention.** Every heap tuple carries `xmin`/`xmax` transaction IDs. PostgreSQL's 32-bit XID space wraps at 2^31 transactions. When a tuple's `xmin` is sufficiently old, vacuum rewrites it as frozen using the `FrozenTransactionId` sentinel. This makes it visible to all future transactions regardless of snapshot age. If `datfrozenxid` advances too slowly, autovacuum escalates to an aggressive freeze pass that scans every heap page.

## Symptoms of Autovacuum Not Keeping Up

Growing `n_dead_tup`, rising `Heap Fetches` on index-only scans, autovacuum workers stuck in `wait_event_type = Lock`, no workers running at all despite pending work, and `age(datfrozenxid)` climbing toward `autovacuum_freeze_max_age` are all signs that autovacuum is falling behind. [[troubleshooting/autovacuum-not-keeping-up|Autovacuum Not Keeping Up]] has the diagnostic queries and a decision tree for identifying which specific failure mode — unmet thresholds, cost throttling, lock contention, or worker starvation — is active before deciding what to tune below.

## Key Autovacuum GUCs

Autovacuum decides whether to vacuum a table using:

```
n_dead_tup > autovacuum_vacuum_threshold + autovacuum_vacuum_scale_factor * pg_class.reltuples
```

**`autovacuum_vacuum_scale_factor`** (default `0.2`): Fraction of estimated live rows that must be dead before triggering. At 20%, a 10-million-row table must accumulate 2 million dead tuples before vacuum fires. For large, high-write tables this is far too permissive; values of `0.01` or `0.001` are common in production.

**`autovacuum_vacuum_threshold`** (default `50`): The absolute minimum dead tuple count. Prevents autovacuum from thrashing on tiny tables. Rarely needs adjustment.

**PostgreSQL 18: `autovacuum_vacuum_max_threshold`** (default `100,000,000`, -1 to disable): An upper cap on the dead-tuple count required to trigger autovacuum, independent of `autovacuum_vacuum_scale_factor`. On a table with one billion rows the 20% scale factor demands 200 million dead tuples before autovacuum fires — far too many for most workloads. Setting `autovacuum_vacuum_max_threshold = 10000000` means autovacuum will trigger once dead tuples exceed 10 million regardless of table size. The same cap is available as a per-table storage parameter.

**`autovacuum_analyze_scale_factor`** (default `0.1`): Fraction of rows modified (insert + update + delete) since the last ANALYZE before triggering a new statistics pass. Lower this on tables where query plans are sensitive to row-count changes.

**`autovacuum_vacuum_cost_delay`** (default `2ms`): After each cost-unit chunk, autovacuum sleeps this long, throttling I/O impact. Setting it to `0` disables throttling entirely — useful for emergency catch-up but risks saturating I/O on shared systems.

**`autovacuum_vacuum_cost_limit`** (default `200`): Total cost units per sleep cycle. Each 8 kB page read costs 1 unit; dirty page hits cost more. Raising this to 400–800 on systems with fast NVMe storage lets workers do more work per cycle without eliminating the delay.

**`autovacuum_max_workers`** (default `3`): Maximum concurrent autovacuum workers across all databases. On instances with many active databases or very large tables, increasing this to 5–6 prevents all worker slots being consumed by a single slow table.

**PostgreSQL 18: `autovacuum_worker_slots`** (default `16`, requires restart): Separates background-worker slot reservation from the concurrency limit. Before PG 18, `autovacuum_max_workers` served both purposes — changing it required a server restart. Now `autovacuum_worker_slots` reserves the maximum number of slots at startup (needs restart) while `autovacuum_max_workers` controls how many workers run concurrently and can be adjusted at reload. Set `autovacuum_worker_slots` to the highest value `autovacuum_max_workers` will ever need; then tune `autovacuum_max_workers` at runtime without restarts.

**`autovacuum_naptime`** (default `1min`): How often the autovacuum launcher wakes up to check which tables need service. On busy systems with many tables, lowering this to `15s` reduces the lag between a table crossing its threshold and a worker being assigned. The effective per-table check interval scales with the number of databases: `naptime / N_databases`.

## Per-Table Storage Parameters

Global GUCs apply to all tables, but correct thresholds vary dramatically between a 100-row lookup table and a 500-million-row event log. `ALTER TABLE ... SET (...)` overrides the global settings for a specific relation:

```sql
-- Aggressive vacuum for a high-write table
ALTER TABLE orders SET (
    autovacuum_vacuum_scale_factor  = 0.01,
    autovacuum_vacuum_threshold     = 1000,
    autovacuum_analyze_scale_factor = 0.005,
    autovacuum_vacuum_cost_delay    = 0
);

-- Relax autovacuum on a rarely-written reference table
ALTER TABLE country_codes SET (
    autovacuum_vacuum_scale_factor  = 0.5,
    autovacuum_analyze_scale_factor = 0.3
);
```

`do_autovacuum` reads per-table parameters from `pg_class.reloptions` and checks them before applying the global fallback. They are the most effective single tuning lever for tables with atypical write patterns.

## Monitoring Vacuum

```sql
-- Active vacuum progress
SELECT relid::regclass           AS table,
       phase,
       heap_blks_total,
       heap_blks_scanned,
       heap_blks_vacuumed,
       index_vacuum_count,
       num_dead_item_ids
FROM pg_stat_progress_vacuum;

-- XID age — critical for wraparound monitoring
SELECT datname,
       age(datfrozenxid)                    AS xid_age,
       2147483647 - age(datfrozenxid)       AS xids_remaining
FROM pg_database
ORDER BY xid_age DESC;
```

The `phase` column cycles through: scanning heap, vacuuming indexes, vacuuming heap, truncating heap, performing final cleanup. A worker stuck in "vacuuming indexes" on a table with many large indexes may need cost limit adjustments or index bloat remediation.

`n_mod_since_analyze` in `pg_stat_user_tables` tracks modifications since the last ANALYZE. When this is high relative to `n_live_tup`, query plans are likely using stale statistics.

## Manual VACUUM

For immediate intervention without waiting for autovacuum's trigger threshold:

```sql
-- Standard manual vacuum with progress output
VACUUM VERBOSE orders;

-- Vacuum and refresh planner statistics in one pass
VACUUM ANALYZE orders;

-- Force freeze of old tuples (XID wraparound prevention)
VACUUM FREEZE orders;

-- Full rewrite — reclaims space to OS, requires AccessExclusiveLock
VACUUM FULL orders;
```

`VACUUM` (without `FULL`) uses `ShareUpdateExclusiveLock`, compatible with concurrent reads and writes. `VACUUM FULL` rewrites the entire table and holds an exclusive lock for its duration; use it only when reclaiming disk space to the OS is required and downtime is acceptable.

Manual VACUUM respects `vacuum_cost_delay` and `vacuum_cost_limit` from the session's GUC settings. Override them for fast manual catch-up:

```sql
SET vacuum_cost_delay  = 0;
SET vacuum_cost_limit  = 800;
VACUUM VERBOSE orders;
```

## VACUUM FREEZE and XID Wraparound

If `age(datfrozenxid)` approaches `autovacuum_freeze_max_age` (default 200 million transactions), PostgreSQL escalates to an aggressive freeze vacuum that scans every heap page regardless of visibility map state. At roughly 40 million transactions remaining (XID age ≈ 2,107,483,647), PostgreSQL emits warnings. At the hard limit it refuses new transactions to prevent data corruption.

```sql
-- Tables with oldest unfrozen tuples
SELECT relname,
       greatest(age(relfrozenxid), age(relminmxid)) AS freeze_age
FROM pg_class
WHERE relkind = 'r'
ORDER BY freeze_age DESC
LIMIT 10;
```

Emergency response: identify the oldest table from the query above, run `VACUUM FREEZE` on it immediately, and check `pg_stat_activity` for idle-in-transaction connections holding back the XID horizon.

**PostgreSQL 18:** Normal (non-aggressive) vacuum can now freeze all-visible pages opportunistically rather than skipping them. The `vacuum_max_eager_freeze_failure_rate` GUC (default `0.03`) limits the fraction of pages that may be unsuccessfully attempted before eager mode stops for the current run. By making incremental freeze progress during routine runs, vacuum needs aggressive passes less often. This reduces the load on instances with large, rarely-modified tables that would otherwise trigger frequent full freeze passes.

**PostgreSQL 18:** `vacuum_truncate` (which controls whether VACUUM attempts to shorten trailing empty pages) is now a session-level GUC in addition to a per-table storage parameter. To disable truncation for a single manual run without permanently altering table options: `SET vacuum_truncate = off; VACUUM orders;`.

Long-lived idle transactions prevent XID horizon advancement and block tuple freezing even when vacuum runs normally.

## Practical Guidance

**Start with per-table scale factors.** For any table over 10 million rows with significant write load, set `autovacuum_vacuum_scale_factor = 0.01` immediately. The global default of 0.2 is appropriate only for small tables.

**Eliminate cost delay for urgent tables.** On tables with persistent dead tuple accumulation, set `autovacuum_vacuum_cost_delay = 0` at the table level. Monitor host I/O to confirm it is acceptable before deploying broadly.

**Raise `autovacuum_max_workers` on busy instances.** An instance with dozens of active schemas can exhaust three workers quickly. Five or six workers with appropriate cost limits prevents starvation without causing I/O contention.

**Kill idle-in-transaction connections.** Long-running transactions are the most common cause of autovacuum being unable to remove dead tuples. Set `idle_in_transaction_session_timeout` (e.g., `30min`) to terminate them automatically.

**Lower `autovacuum_naptime` on high-throughput systems.** The default 1-minute naptime means a table can sit above its vacuum threshold for up to a minute before a worker is assigned. A value of `15s` reduces this lag with negligible overhead.

**Do not use `VACUUM FULL` as a routine operation.** It holds an exclusive lock for the full rewrite duration. Use `pg_repack` for online bloat reduction when needed.

## Related Topics

- [[subsystems/background/autovacuum|Autovacuum]] — the background subsystem that drives automatic vacuum and analyze decisions, whose worker processes and launcher this article tunes.
- [[subsystems/transactions/xid-wraparound|XID Wraparound]] — describes the transaction ID exhaustion risk that aggressive freeze vacuuming is designed to prevent.
- [[subsystems/storage/table-and-index-bloat|Table and Index Bloat]] — covers how dead tuples and page fragmentation accumulate when vacuum falls behind, and how to measure and remediate bloat.
- [[subsystems/storage/visibility-map|Visibility Map]] — explains the all-visible and all-frozen bits that vacuum maintains and that index-only scans depend on.
- [[code-paths/vacuum|VACUUM Code Path]] — traces the implementation of the VACUUM command from parse through `vacuum_rel`, complementing the tuning perspective here.
- [[subsystems/observability/pg-stat-all-tables|pg_stat_all_tables]] — the primary monitoring view for dead tuple counts, last vacuum/analyze timestamps, and modification counters referenced throughout this article.
- [[troubleshooting/bloat|Bloat Troubleshooting]] — practical diagnosis and remediation guide for table and index bloat caused by insufficient vacuuming.
- [[subsystems/indexes/index-maintenance|Index Maintenance]] — covers identifying unused indexes and measuring B-tree bloat, the index-side complement to the vacuum tuning discussed here.
