---
title: When and How to Use Table Partitioning
aliases:
  - Partitioning Strategy
  - Partition Design
tags:
  - theme/query-optimization
source_files:
  - src/backend/optimizer/path/allpaths.c
  - src/backend/partitioning/partbounds.c
  - src/backend/partitioning/partprune.c
symbols:
  - prune_append_rel_partitions
  - make_partition_pruneinfo
  - PartitionPruneContext
---

# When and How to Use Table Partitioning

PostgreSQL declarative partitioning (introduced in v10, matured in v11–v14) splits a logical table into physical child tables called *partitions*. The planner can eliminate irrelevant partitions at planning or execution time (partition pruning). Maintenance operations such as retiring old data become near-instant metadata changes. These benefits come with real trade-offs that must be understood before committing to a partitioned design.

## When Partitioning Helps

**Large tables with selective filters on the partition key.** Partition pruning lets the planner skip child tables entirely when a `WHERE` clause constrains the partition key. A 10 TB time-series table partitioned by month lets a query for a single month touch roughly 1/120th of the data without opening the other 119 files at all.

**Rolling retention workloads.** Dropping or detaching an old partition is a metadata-only operation — no bloat, no `VACUUM` needed, no `DELETE` fan-out:

```sql
-- Create a monthly partition
CREATE TABLE events_2024_01 PARTITION OF events
    FOR VALUES FROM ('2024-01-01') TO ('2024-02-01');

-- Retire it instantly with no table scan
ALTER TABLE events DETACH PARTITION events_2024_01;
DROP TABLE events_2024_01;
```

**Bulk loads into specific partitions.** Loading data directly into a child table (`COPY` or `INSERT`) bypasses parent-level routing overhead. It also allows parallel loads into different partitions while other partitions remain fully available to readers.

## When Partitioning Hurts

**Queries without a partition key filter.** A full-table scan on a partitioned table opens every child table in sequence. On a 500-partition table this is often slower than the equivalent scan on an unpartitioned table because of per-relation overhead in the executor's Append node.

**High partition counts increase planning time.** The planner must build paths for each child during `make_partition_pruneinfo`. Beyond roughly 1 000 partitions, planning latency becomes noticeable even for simple point-lookup queries. Sub-partitioned designs multiply this effect.

**Cross-partition unique constraints are impossible.** A `UNIQUE` or `PRIMARY KEY` constraint is enforced only within a single partition. If your application requires global uniqueness on a column that is not part of the partition key, you cannot enforce it declaratively in PostgreSQL.

**Foreign key complications.** A foreign key *from* an unpartitioned table *to* a partitioned table is unsupported. A foreign key *from* a partitioned table to another table works, but each partition gets its own FK constraint entry. This adds catalog overhead during inserts and updates.

## Choosing a Partition Strategy

### RANGE — time-series and sequential IDs

Use `RANGE` when data has a natural ordering and queries filter on contiguous ranges. It is the most common strategy for event logs, audit trails, and IoT data.

```sql
CREATE TABLE orders (
    id          bigint,
    created_at  timestamptz NOT NULL,
    total       numeric
) PARTITION BY RANGE (created_at);

CREATE TABLE orders_2024_q1 PARTITION OF orders
    FOR VALUES FROM ('2024-01-01') TO ('2024-04-01');
CREATE TABLE orders_2024_q2 PARTITION OF orders
    FOR VALUES FROM ('2024-04-01') TO ('2024-07-01');
-- Add a default partition to catch out-of-range inserts during transition
CREATE TABLE orders_default PARTITION OF orders DEFAULT;
```

### LIST — region, status, or tenant

Use `LIST` when the partition column has a small, known set of discrete values and queries commonly filter on exactly one of them.

```sql
CREATE TABLE tickets (
    id      bigint,
    region  text NOT NULL,
    body    text
) PARTITION BY LIST (region);

CREATE TABLE tickets_us   PARTITION OF tickets FOR VALUES IN ('us');
CREATE TABLE tickets_eu   PARTITION OF tickets FOR VALUES IN ('eu');
CREATE TABLE tickets_apac PARTITION OF tickets FOR VALUES IN ('apac');
CREATE TABLE tickets_rest PARTITION OF tickets DEFAULT;
```

### HASH — even distribution without a natural key

Use `HASH` when there is no meaningful range or list key but you want to spread I/O evenly across partitions. Only equality predicates on the partition key can trigger pruning; range scans always touch all partitions.

```sql
CREATE TABLE sessions (
    session_id uuid NOT NULL,
    data       jsonb
) PARTITION BY HASH (session_id);

CREATE TABLE sessions_0 PARTITION OF sessions
    FOR VALUES WITH (MODULUS 4, REMAINDER 0);
CREATE TABLE sessions_1 PARTITION OF sessions
    FOR VALUES WITH (MODULUS 4, REMAINDER 1);
CREATE TABLE sessions_2 PARTITION OF sessions
    FOR VALUES WITH (MODULUS 4, REMAINDER 2);
CREATE TABLE sessions_3 PARTITION OF sessions
    FOR VALUES WITH (MODULUS 4, REMAINDER 3);
```

## Partition Key Design

**The key must appear in most `WHERE` clauses.** Pruning via `prune_append_rel_partitions` is only possible when the planner can compare a constant (or stable expression) against the stored partition bounds. If queries rarely filter on the key, partitioning adds overhead without delivering pruning.

**Declare the key column `NOT NULL`.** PostgreSQL cannot match null values against any non-default partition bound. They fall through to the `DEFAULT` partition (if it exists) or cause an error.

**Use range predicates, not expression wrappers, over date columns.** A very common mistake is writing:

```sql
-- Does NOT prune — the function wrapper hides the column from bound comparison
SELECT * FROM events
WHERE date_trunc('month', created_at) = '2024-01-01';

-- Prunes correctly — bare column compared against constants
SELECT * FROM events
WHERE created_at >= '2024-01-01'
  AND created_at  < '2024-02-01';
```

The planner can only prune when the partition key column appears *bare* (or with an immutable cast) on one side of a comparison against a constant or stable expression.

## Constraints and Indexes

**Primary keys and unique constraints must include the partition key.** PostgreSQL enforces uniqueness partition-by-partition, so the key must be part of the constraint columns to guarantee correctness across the whole table:

```sql
-- Valid: partition key (created_at) is included in the PK
ALTER TABLE events ADD PRIMARY KEY (id, created_at);

-- Error: unique constraint does not include the partition key
ALTER TABLE events ADD UNIQUE (id);
```

**Indexes on the parent propagate automatically.** A `CREATE INDEX` on the parent table creates matching indexes on all existing child partitions and on any future partitions at `ATTACH` time:

```sql
CREATE INDEX ON events (created_at, user_id);
-- Equivalent indexes are created on every child partition automatically.
```

## Sub-partitioning

A partition can itself be partitioned, creating a two-level hierarchy. A common pattern partitions first by year, then by region:

```sql
CREATE TABLE metrics_2024 PARTITION OF metrics
    FOR VALUES FROM ('2024-01-01') TO ('2025-01-01')
    PARTITION BY LIST (region);

CREATE TABLE metrics_2024_us PARTITION OF metrics_2024 FOR VALUES IN ('us');
CREATE TABLE metrics_2024_eu PARTITION OF metrics_2024 FOR VALUES IN ('eu');
```

Planning time scales with the total number of *leaf* partitions. Two years with four regions yields 8 leaf partitions — manageable. Monthly granularity with four regions yields 96 per year. That can push planning latency into the milliseconds.

## Automated Maintenance with pg_partman

The `pg_partman` extension automates creation of future partitions and optional retention of old ones. After installation:

```sql
SELECT partman.create_parent(
    p_parent_table => 'public.events',
    p_control      => 'created_at',
    p_type         => 'range',
    p_interval     => '1 month',
    p_premake      => 4   -- pre-create 4 future partitions
);
```

A periodic call to `partman.run_maintenance()` — typically scheduled via `pg_cron` — creates upcoming partitions. If configured, it also drops or detaches partitions older than the retention window.

## Verifying Partition Pruning

Use `EXPLAIN (ANALYZE)` to confirm that partitions are being eliminated. Pruned partitions appear with a `(never executed)` annotation on their child scans, or are absent from the plan entirely when static pruning fires at planning time.

```sql
EXPLAIN (ANALYZE, BUFFERS)
SELECT * FROM events
WHERE created_at >= '2024-03-01'
  AND created_at  < '2024-04-01';
```

Expected plan fragment showing pruning in action:

```
->  Seq Scan on events_2024_03  (cost=...) (actual rows=84123 loops=1)
->  Seq Scan on events_2024_01  (cost=...) (never executed)
->  Seq Scan on events_2024_02  (cost=...) (never executed)
```

Pruning is controlled by the `enable_partition_pruning` GUC (on by default). Dynamic pruning — run-time elimination based on parameterised values — requires PostgreSQL 12+.

```sql
SET enable_partition_pruning = on;   -- default; disable to observe the cost
```

The core pruning logic is in `src/backend/partitioning/partprune.c`. The entry point `prune_append_rel_partitions` calls `make_partition_pruneinfo` to build a `PartitionPruneContext` that holds the partition bounds used by both the planner (static pruning) and the executor (dynamic pruning).

## Practical Guidance

| Situation | Recommendation |
|---|---|
| Table > 100 GB with time-based queries | `RANGE` on the timestamp; monthly or quarterly granularity |
| Need to expire old data regularly | Partition by time; `DETACH` + `DROP` old partitions |
| Uniform distribution, no natural key | `HASH`; accept that range scans touch all partitions |
| Table < 10 GB | Skip partitioning; a plain B-tree index is cheaper |
| Global `UNIQUE` required on non-key column | Partitioning cannot enforce this; reconsider the schema |
| > 500 partitions anticipated | Benchmark planning time; consider coarser granularity or sub-partitioning |
| Automated partition creation | `pg_partman` + `pg_cron` |

## Related Topics

- [[subsystems/partitioning/partition-pruning|Partition Pruning]] — covers the planner and executor logic that eliminates irrelevant child tables, which is the primary performance benefit discussed in this article.
- [[subsystems/partitioning/partitioned-indexes|Partitioned Indexes]] — explains how indexes are created and propagated across partitions, including the constraint that primary keys must include the partition key.
- [[subsystems/partitioning/partition-wise-join|Partition-Wise Join]] — describes how the planner can join two co-partitioned tables partition-by-partition, an advanced optimization enabled by the designs outlined here.
- [[subsystems/partitioning/partition-wise-aggregate|Partition-Wise Aggregate]] — covers parallel aggregation across partitions, a companion optimization to partition-wise join relevant when choosing partition granularity.
- [[subsystems/partitioning/partition-introspection|Partition Introspection]] — shows catalog queries and system views for inspecting partition bounds, hierarchy, and statistics once a design is deployed.
- [[subsystems/planner/constraint-exclusion|Constraint Exclusion]] — the older mechanism for excluding child tables in table inheritance hierarchies; useful context for understanding how declarative pruning replaced it.
- [[subsystems/storage/table-and-index-bloat|Table and Index Bloat]] — explains the bloat dynamics that partitioning's detach-and-drop retention pattern is specifically designed to avoid.
- [[subsystems/partitioning/overview|Partitioning Overview]] — the reference article on partition strategies, catalog representation, and routing that this practical guidance builds on.
- [[code-paths/bulk-loading|Bulk Loading Performance]] — loading large datasets is one of the workloads that benefits most from partitioning, since whole partitions can be loaded and attached without touching existing data.
