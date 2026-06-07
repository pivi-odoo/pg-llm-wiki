---
title: Index Maintenance
aliases:
  - index bloat
  - REINDEX CONCURRENTLY
  - unused indexes
  - index hygiene
tags:
  - symptom/bloat
  - theme/vacuum-and-maintenance
source_files:
  - src/backend/commands/indexcmds.c
  - src/backend/access/nbtree/nbtree.c
  - contrib/pgstattuple/pgstattuple.c
symbols:
  - ReindexRelationConcurrently
  - table_index_build_scan
  - pgstatindex_impl
  - bt_page_stats
---

# Index Maintenance

Indexes are not self-managing. Every index imposes write overhead on INSERT, UPDATE, and DELETE — the executor must invoke the index AM update callbacks for each affected tuple across every index on the table. An index that serves no queries extracts that cost for zero benefit. Over time, B-tree indexes accumulate internal fragmentation from deletions and updates, degrading sequential read efficiency and increasing buffer pool pressure. Systematic maintenance — identifying dead weight, measuring bloat, and rebuilding when warranted — is as important as index creation.

## Finding Unused Indexes

PostgreSQL accumulates per-index scan counts in `pg_stat_user_indexes`. An index with `idx_scan = 0` (or negligibly low counts) after a representative workload period — covering typical business cycles, batch jobs, and reporting windows — is a candidate for removal.

Unused indexes waste more than write overhead. Each index occupies shared buffer pages that could otherwise cache hot table data. On write-heavy tables, WAL volume for index maintenance is substantial — B-tree page splits generate full-page writes under `full_page_writes = on`. `IndexBuildHeapScan` in `src/backend/commands/indexcmds.c` drives initial index population; every subsequent write pays a proportional per-index cost.

```sql
SELECT
    schemaname,
    tablename,
    indexname,
    idx_scan,
    pg_size_pretty(pg_relation_size(indexrelid)) AS index_size
FROM pg_stat_user_indexes
WHERE idx_scan < 10
  AND NOT EXISTS (
      SELECT 1 FROM pg_constraint c
      WHERE c.conindid = indexrelid
  )
ORDER BY pg_relation_size(indexrelid) DESC;
```

The `pg_constraint` exclusion filters out indexes that back PRIMARY KEY, UNIQUE, or EXCLUSION constraints — those cannot be dropped independently of the constraint. Statistics reset at `pg_stat_reset()` or server restart, so verify the observation window is meaningful by checking `stats_reset` in `pg_stat_bgwriter`.

## Finding Duplicate and Redundant Indexes

An index on `(a, b)` satisfies all queries that a standalone index on `(a)` would. This is because B-tree scans can stop at any leading prefix of the key. A standalone `(a)` index is therefore redundant for most query patterns when the composite already exists.

```sql
SELECT
    i1.indexrelid::regclass AS keeping,
    i2.indexrelid::regclass AS redundant,
    i1.indkey               AS keeping_keys,
    i2.indkey               AS redundant_keys
FROM pg_index i1
JOIN pg_index i2
    ON i1.indrelid = i2.indrelid
   AND i1.indexrelid <> i2.indexrelid
WHERE i1.indisvalid AND i2.indisvalid
  -- i2's key columns are a leading prefix of i1's key columns
  AND (i1.indkey::int[])[0:array_length(i2.indkey::int[], 1) - 1]
      = i2.indkey::int[]
  AND i2.indpred IS NULL   -- partial indexes are not simply redundant
ORDER BY i1.indrelid, keeping;
```

This query compares `pg_index.indkey` integer arrays. Partial indexes (`indpred IS NOT NULL`) are excluded because their predicate scope may differ from the composite. Also check `indoption` if sort order matters. Functional indexes (`indkey` contains 0 for expression columns resolved via `indexprs`) require manual inspection.

## Index Bloat

B-tree indexes accumulate bloat when rows are updated or deleted. PostgreSQL marks deleted index tuples as dead but does not immediately reclaim the space. Vacuum removes heap dead tuples and marks corresponding index entries deletable, but page consolidation only occurs when a page becomes entirely empty or during a page split. The result is sparse leaf pages with low fill density.

The `pgstatindex()` function from `contrib/pgstattuple` exposes this via `pgstatindex_impl` and `bt_page_stats`:

```sql
SELECT *
FROM pgstatindex('orders_customer_id_idx');
```

Key fields:

| Field | Healthy range | Concern threshold |
|---|---|---|
| `avg_leaf_density` | 70–90% | below 50% |
| `leaf_fragmentation` | near 0 | above 30% |
| `deleted_pages` | near 0 | growing steadily |

`avg_leaf_density` below 50% on a frequently-queried index means the planner is reading roughly twice the necessary pages for index scans. `leaf_fragmentation` reflects out-of-order pages — sequential index scans become random I/O. Both symptoms suggest a rebuild is warranted.

## REINDEX CONCURRENTLY (PostgreSQL 12+)

`REINDEX CONCURRENTLY` rebuilds an index without blocking concurrent reads or writes. Internally it follows the same two-pass protocol as `CREATE INDEX CONCURRENTLY`, implemented in `ReindexRelationConcurrently` in `src/backend/commands/indexcmds.c`:

1. Build a new index with `ShareUpdateExclusiveLock` — writers proceed; the new index catches inserts via the normal dual-index window.
2. Wait for all transactions that saw the old index to complete.
3. Swap the new index into place and drop the old one.

```sql
REINDEX INDEX CONCURRENTLY orders_customer_id_idx;
-- or rebuild all indexes on a table:
REINDEX TABLE CONCURRENTLY orders;
```

`ShareUpdateExclusiveLock` conflicts with `VACUUM`, `ANALYZE`, and other concurrent `REINDEX CONCURRENTLY` on the same object, but not with normal DML. The operation is safe for production but takes longer than a blocking `REINDEX` because it must wait out long-running transactions twice. If it fails partway through, an invalid index named `<original>_ccnew` is left behind and must be dropped manually before retrying (see Invalid Indexes below).

## Regular REINDEX

```sql
REINDEX INDEX orders_customer_id_idx;
REINDEX TABLE orders;
```

Regular `REINDEX` takes `AccessExclusiveLock`, blocking all reads and writes for the duration of the rebuild. It is appropriate only during scheduled maintenance windows or when `REINDEX CONCURRENTLY` has failed and left an invalid index that prevents the concurrent path from running. The blocking form is faster and simpler when downtime is acceptable.

## Invalid Indexes

`CREATE INDEX CONCURRENTLY` (and `REINDEX CONCURRENTLY`) can fail partway through and leave an index marked invalid. An invalid index is not used by the planner but still incurs write overhead on every DML operation. Detect them with:

```sql
SELECT indexrelid::regclass AS index_name, indrelid::regclass AS table_name
FROM pg_index
WHERE NOT indisvalid;
```

An invalid index must be dropped and recreated — it cannot be repaired in place:

```sql
DROP INDEX CONCURRENTLY orders_customer_id_idx_ccnew;
-- Then recreate:
CREATE INDEX CONCURRENTLY orders_customer_id_idx ON orders (customer_id);
```

The `pg_index.indisvalid` flag is the authoritative check. `\d tablename` in psql also displays `INVALID` next to affected index names.

## Monitoring Index Size and Health

Track index sizes over time to detect organic growth versus bloat growth:

```sql
-- Size of a single index:
SELECT pg_size_pretty(pg_relation_size('orders_customer_id_idx'::regclass));

-- All indexes on a table with size breakdown:
SELECT
    indexname,
    pg_size_pretty(pg_relation_size(indexname::regclass)) AS index_size,
    round(
        100.0 * pg_relation_size(indexname::regclass)
            / nullif(pg_indexes_size('orders'::regclass), 0),
        1
    ) AS pct_of_total
FROM pg_indexes
WHERE tablename = 'orders'
ORDER BY pg_relation_size(indexname::regclass) DESC;
```

`pg_indexes_size` returns the combined size of all indexes on a table. Storing these snapshots in a monitoring table and computing week-over-week deltas helps distinguish tables where index growth tracks row growth (normal) from those where index size grows faster than table size (a bloat signal).

## Covering Index Hygiene

`INCLUDE` columns in covering indexes add storage on every write and every index page. Columns included at creation time for a specific query pattern may no longer be referenced by any active query after schema or application changes.

```sql
-- Identify covering indexes and which columns are INCLUDE columns:
SELECT
    i.indexrelid::regclass AS index_name,
    a.attname,
    a.attnum > i.indnkeyatts AS is_included
FROM pg_index i
JOIN pg_attribute a
    ON a.attrelid = i.indrelid
   AND a.attnum = ANY(i.indkey)
WHERE i.indrelid = 'orders'::regclass
ORDER BY index_name, a.attnum;
```

`indnkeyatts` is the count of actual key columns; attributes beyond that position in `indkey` are `INCLUDE` columns. If the included columns are no longer fetched by any active index-only scan query, rebuild the index without them. Use `DROP INDEX CONCURRENTLY` to remove without blocking:

```sql
DROP INDEX CONCURRENTLY orders_covering_old_idx;
```

Always create the replacement index concurrently before dropping the old one to avoid a coverage gap.

## Practical Guidance

**Establish a statistics observation window.** Before acting on `idx_scan = 0`, confirm that `pg_stat_reset()` has not been called recently and that the window spans all workload patterns including monthly batch jobs and ad-hoc reporting. Mark candidate indexes with a comment and monitor for 2–4 weeks before dropping.

**Check bloat before query regressions occur.** Schedule a weekly query against `pgstatindex()` for your largest and most critical indexes. Alert on `avg_leaf_density < 60%` rather than waiting for users to report slowness.

**Prefer REINDEX CONCURRENTLY in production.** The performance cost of non-blocking rebuilds (longer wall time, two transaction-wait phases) is almost always preferable to the availability cost of `AccessExclusiveLock` on a busy table. Reserve regular `REINDEX` for maintenance windows or recovery scenarios.

**Rebuild triggers to consider:**
- `avg_leaf_density` below 50% on a frequently-scanned index.
- Query plans regressing to sequential scans despite accurate statistics and an apparently correct index.
- After bulk deletes removing more than 20–30% of a table's rows. Vacuum reclaims heap space, but index compaction depends on page emptying. That may not happen until subsequent writes refill pages.

**Safe removal workflow for covering index INCLUDE columns.** Build the replacement concurrently before dropping the old one:

```sql
CREATE INDEX CONCURRENTLY orders_cust_new_idx
    ON orders (customer_id, created_at);
-- Verify it appears in active query plans, then:
DROP INDEX CONCURRENTLY orders_cust_old_covering_idx;
```

**pg_repack as an alternative.** The `pg_repack` extension rebuilds tables and indexes online in a single pass, rewriting both heap and indexes compactly. It is preferable to `REINDEX CONCURRENTLY` when both table and index bloat are present simultaneously.

## Related Topics

- [[subsystems/indexes/btree|B-tree Indexes]] — the primary index type affected by bloat and fragmentation, whose internal structure determines when REINDEX is warranted
- [[subsystems/storage/table-and-index-bloat|Table and Index Bloat]] — broader treatment of bloat measurement and its impact on storage and query performance
- [[subsystems/indexes/index-only-scans|Index-Only Scans]] — covering index hygiene directly affects whether index-only scans remain viable after schema changes
- [[subsystems/indexes/multicolumn-index-strategies|Multicolumn Index Strategies]] — redundancy detection depends on understanding how leading-prefix rules make composite indexes subsume narrower ones
- [[subsystems/background/vacuum-tuning|Vacuum Tuning]] — autovacuum drives the index dead-tuple cleanup that precedes page reclamation, making its tuning inseparable from index maintenance
- [[code-paths/create-index|CREATE INDEX]] — REINDEX CONCURRENTLY reuses the same two-pass concurrent build protocol implemented here
- [[code-paths/reindex|REINDEX]] — the code path for both blocking and concurrent index rebuilds, including the invalid-index cleanup logic
