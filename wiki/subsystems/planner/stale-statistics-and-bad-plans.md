---
title: Stale Statistics and Bad Plans
aliases:
  - bad plans
  - stale stats
  - statistics staleness
tags:
  - symptom/slow-query
  - theme/query-optimization
source_files:
  - src/backend/optimizer/util/plancat.c
  - src/backend/commands/analyze.c
  - src/include/catalog/pg_statistic.h
symbols:
  - get_relation_stats_hook
  - examine_attribute
  - update_attstats
  - StatsElem
  - STATISTIC_KIND_HISTOGRAM
  - STATISTIC_KIND_MCV
---

## How the Planner Uses pg_statistic

The planner never reads table data directly to estimate selectivity. It relies entirely on statistics collected by ANALYZE and stored in `pg_statistic` (exposed via `pg_stats`). For each indexed or analyzed column, `pg_statistic` stores several statistic kinds:

- **MCV list** (`STATISTIC_KIND_MCV`, stakind=1): most-common values and their frequencies (`most_common_vals`, `most_common_freqs`). Used for equality and IN predicates. If the literal matches an MCV entry, the planner uses that frequency directly.
- **Histogram** (`STATISTIC_KIND_HISTOGRAM`, stakind=2): `histogram_bounds` divides non-MCV values into equal-frequency buckets. For range predicates, the planner interpolates within the relevant bucket.
- **Correlation** (`STATISTIC_KIND_CORRELATION`, stakind=3): Pearson correlation between physical tuple order and logical sort order. Values near ±1 mean data is physically sorted. The planner uses this to decide whether an index scan is cheaper than a sequential scan (low correlation → many random I/Os → index scan more expensive).
- **n_distinct**: Number of distinct values. Positive value = absolute count; **negative value = fraction of the table** (e.g., -0.1 means 10% of rows are distinct). The planner uses the negative form when it cannot reliably estimate absolute count from a sample.

`examine_attribute` in `analyze.c` computes these per-column statistics from a sample. `update_attstats` writes them to `pg_statistic`. The hook `get_relation_stats_hook` allows extensions (e.g., pg_hint_plan) to override what the planner sees.

`pg_class.reltuples` and `pg_class.relpages` store the coarse table-level estimates used for cost scaling. ANALYZE and VACUUM update these values, but they can lag significantly after bulk DML.

## How Stale Statistics Cause Bad Plans

After a large bulk INSERT or DELETE, two things go wrong simultaneously:

1. `pg_class.reltuples` and `relpages` remain at pre-operation values. The planner sees the old row count, mispricing every cost formula that scales with table size.
2. Column statistics in `pg_statistic` reflect the old data distribution. Histograms and MCV lists no longer match the actual data, so selectivity estimates for predicates are wrong.

Concrete failure modes:

- **Wrong join order**: The planner builds join trees bottom-up using estimated intermediate row counts. If the planner underestimates an inner table's row count by 100×, it may choose a nested-loop join with that table on the outer side, executing a full scan per outer row rather than a hash join.
- **Wrong join method**: Underestimated rows favor nested-loop. Overestimated rows favor hash join even when the data fits in a nested-loop. Both directions cause regressions.
- **Wrong index choice**: A highly selective predicate on a column with stale histogram may appear non-selective (estimated rows >> actual rows), causing the planner to skip an index and choose a sequential scan.

## Spotting Bad Estimates in EXPLAIN ANALYZE

```sql
EXPLAIN (ANALYZE, BUFFERS) SELECT ...;
```

Look for divergence between `rows=X` (planner estimate) and `actual rows=Y`:

```
Hash Join  (cost=1234.00..5678.00 rows=50 width=64)
           (actual time=12.3..45.6 rows=48200 loops=1)
  Hash Cond: (a.id = b.id)
  ->  Seq Scan on orders a  (cost=0.00..1200.00 rows=10 width=32)
                             (actual time=0.1..8.2 rows=9840 loops=1)
```

Red flags:
- Estimate vs. actual divergence of **10× or more** on any node, especially join inner sides.
- A nested-loop with `loops=N` where N is much larger than expected — the outer estimate was wrong.
- A Hash Join or Merge Join where the hash table had to be batched (`Batches: N` where N > 1) because the planner underestimated memory requirements.

The cumulative effect of bad estimates compounds through the join tree. A 10× error on one table can produce a 100× error on the join result, causing the planner to make systematically wrong decisions further up the tree.

## Correcting the Underlying Statistics

Both `reltuples`/`relpages` and the per-column entries in `pg_statistic` are snapshots taken at the last ANALYZE, so fixing bad estimates means recomputing them rather than tuning the planner around them. A fresh ANALYZE draws a new sample and rewrites every statistic kind for every column. Raising `default_statistics_target` for a specific column widens that sample into more MCV entries and histogram buckets. This trades a larger `pg_statistic` footprint and slower ANALYZE for finer resolution on skewed or high-cardinality columns.

The planner also computes per-column statistics independently of one another. This is a structural blind spot: the planner multiplies each predicate's selectivity in isolation, so two correlated columns (e.g., `city` and `zip_code`) produce a combined estimate far below the true selectivity. `CREATE STATISTICS` closes this gap: it directs ANALYZE to record the joint distribution across columns instead of treating them as independent. See [[subsystems/planner/extended-statistics|Extended Statistics]] for the dependency, ndistinct, and MCV kinds available and how the planner selects among them.

For the exact ANALYZE, `ALTER TABLE ... SET STATISTICS`, and `CREATE STATISTICS` commands, and a full diagnostic-to-remediation workflow, see [[troubleshooting/stale-statistics|Stale Statistics]].

## Per-Column Statistics: pg_stats

```sql
SELECT attname, n_distinct, correlation, most_common_vals, histogram_bounds
FROM pg_stats
WHERE tablename = 'orders' AND attname = 'status';
```

Key fields:
- `n_distinct < 0`: fraction of rows; e.g., -0.05 means each value appears on average in 5% of rows. Common for UUIDs or high-cardinality FKs.
- `correlation` near 1.0 or -1.0: data is physically sorted by this column. The planner can use an index scan with few random I/Os. Values near 0 indicate random ordering. Index scans become expensive.
- `null_frac`: fraction of NULLs; affects IS NULL/IS NOT NULL selectivity.
- `avg_width`: average byte width; used in memory cost estimates for hash joins and sort operations.

## Autovacuum's Analyze Threshold

[[subsystems/background/autovacuum|autovacuum]] decides when to reanalyze a table by comparing `n_mod_since_analyze` — rows modified since the last ANALYZE, tracked in `pg_stat_user_tables` — against a threshold:

```
n_mod_since_analyze > autovacuum_analyze_threshold + autovacuum_analyze_scale_factor * reltuples
```

With the defaults (`autovacuum_analyze_threshold = 50`, `autovacuum_analyze_scale_factor = 0.2`), a table needs roughly 20% churn before autovacuum considers its statistics due for a refresh — on a 10M-row table, that's over two million modifications. A nightly batch load of a few hundred thousand rows falls well short of that bar, so the planner keeps working from a pre-load sample for hours after the load completes.

The scale-factor design assumes churn scales with table size. This assumption breaks down for tables that grow in bursts (large periodic loads) rather than steadily — exactly the tables most likely to hand the planner a stale row count. For the detection queries, per-table `autovacuum_analyze_scale_factor` tuning, and manual-ANALYZE workflow used to close this gap, see the stale-statistics troubleshooting guide linked below.

## Related Topics

- [[subsystems/planner/statistics|Statistics]] — covers the pg_statistic catalog structure and how ANALYZE populates MCV, histogram, and correlation entries that this page explains how to interpret.
- [[subsystems/planner/selectivity-estimation|Selectivity Estimation]] — explains how the planner converts pg_statistic entries into row-count estimates, making it the direct mechanism through which stale statistics produce bad plans.
- [[subsystems/planner/extended-statistics|Extended Statistics]] — details CREATE STATISTICS and multi-column dependency, ndistinct, and MCV kinds used to fix correlated-column underestimates described here.
- [[subsystems/background/autovacuum|Autovacuum]] — describes the autovacuum worker that triggers ANALYZE, including the threshold formula and tuning parameters central to preventing statistics staleness.
- [[subsystems/planner/cost-model|Cost Model]] — documents the cost formulas that consume reltuples, relpages, and selectivity estimates, showing exactly how stale statistics propagate into wrong plan costs.
- [[code-paths/analyze|ANALYZE]] — traces the ANALYZE command code path through examine_attribute and update_attstats, the functions that collect and write the statistics this page relies on.
- [[troubleshooting/slow-queries|Slow Queries]] — practical guide for diagnosing query regressions, complementing the EXPLAIN ANALYZE interpretation advice given here.
- [[troubleshooting/stale-statistics|Stale Statistics]] — the actionable diagnostic and remediation workflow (detection queries, ANALYZE, statistics targets, extended statistics) for the staleness this page explains conceptually.
