---
title: Aggregation Recipes for Performance
aliases:
  - aggregation performance
  - aggregate optimization
tags:
  - theme/parallelism
source_files:
  - src/backend/executor/nodeAgg.c
  - src/backend/optimizer/plan/planner.c
  - src/backend/utils/adt/orderedsetaggs.c
symbols:
  - ExecAgg
  - AggStatePerAgg
  - advance_aggregates
---

# Aggregation Recipes for Performance

Practical patterns for writing aggregation queries. These patterns minimize scans, control
memory usage, and avoid common anti-patterns that generate unnecessarily slow plans.

## FILTER Clause: Conditional Aggregates in One Pass

Any aggregate function accepts a `FILTER (WHERE ...)` clause.  PostgreSQL
evaluates all aggregates in a **single pass** over the input rows, applying
each filter independently.  This replaces multiple subqueries or `CASE`
expressions while keeping the plan to one table scan.

```sql
-- Anti-pattern: multiple subqueries, three scans
SELECT
  (SELECT COUNT(*) FROM orders WHERE status = 'active')  AS active_count,
  (SELECT SUM(amount) FROM orders WHERE region = 'EU')   AS eu_revenue,
  (SELECT AVG(amount) FROM orders WHERE channel = 'web') AS web_avg;

-- Better: single scan with FILTER
SELECT
  COUNT(*)        FILTER (WHERE status = 'active')  AS active_count,
  SUM(amount)     FILTER (WHERE region = 'EU')      AS eu_revenue,
  AVG(amount)     FILTER (WHERE channel = 'web')    AS web_avg
FROM orders;
```

Every aggregate function (`MIN`, `MAX`, `ARRAY_AGG`, `STRING_AGG`, ...) accepts
`FILTER`.  PostgreSQL evaluates the clause before the transition function of each
aggregate, so only qualifying rows advance that aggregate's state.
`advance_aggregates` in `nodeAgg.c` short-circuits the transition call when the
`FILTER` expression returns false. This keeps per-row overhead minimal. See
[[sql-features/aggregate-modifiers]] for the full modifier syntax (`FILTER`,
`ORDER BY`, `DISTINCT`) and how each is represented on the `Aggref` node.

## GROUPING SETS, ROLLUP, and CUBE

Reach for `GROUPING SETS`, `ROLLUP`, or `CUBE` whenever a report needs
subtotals or cross-tabulated totals alongside (or instead of) plain group
totals. They compute every requested grouping in a single scan, instead of
`UNION ALL`-ing separate `GROUP BY` queries together. See
[[sql-features/advanced-aggregation]] for the syntax, the expansion rules for
`ROLLUP` and `CUBE`, and how the `GROUPING()` function disambiguates a
rolled-up `NULL` from a real one in the source data.

## Ordered-Set Aggregates

Percentiles, `mode()`, and the hypothetical-set rank functions all use
`WITHIN GROUP (ORDER BY ...)`. They require a sorted input, so expect a
**Sort** node in `EXPLAIN` rather than a hash strategy. See
[[sql-features/ordered-set-aggregates]] for the syntax and internals, and
[[sql-features/advanced-aggregation]] for the direct-argument and
hypothetical-set forms. The operational point worth remembering when writing
these queries: multiple percentiles on the *same* `ORDER BY` expression share
one sort pass, while aggregates ordered by different expressions each pay for
their own sort.

```sql
-- Three percentiles on the same ORDER BY key share a single sort pass
SELECT
  percentile_cont(0.50) WITHIN GROUP (ORDER BY latency_ms) AS p50,
  percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms) AS p95,
  percentile_cont(0.99) WITHIN GROUP (ORDER BY latency_ms) AS p99
FROM request_log
WHERE recorded_at >= now() - interval '1 hour';
```

## Avoiding Correlated Subquery Per-Row Aggregation

A correlated subquery in the SELECT list re-aggregates for every outer row.
Pull the aggregation into a subquery or CTE and JOIN once.

```sql
-- Anti-pattern: correlated subquery executed N times
SELECT
  c.customer_id,
  c.name,
  (SELECT SUM(amount) FROM orders o WHERE o.customer_id = c.customer_id) AS total
FROM customers c;

-- Better: aggregate once, join once
SELECT c.customer_id, c.name, COALESCE(agg.total, 0) AS total
FROM customers c
LEFT JOIN (
  SELECT customer_id, SUM(amount) AS total
  FROM orders
  GROUP BY customer_id
) agg USING (customer_id);
```

The planner can sometimes unnest a correlated subquery automatically, but it is
not guaranteed.  An explicit pre-aggregated subquery or CTE gives the planner a
deterministic path.

## HashAgg vs GroupAgg

`HashAgg` is the default strategy for unsorted input.  It builds an in-memory
hash table keyed on the grouping columns.  Memory consumption scales with the
number of **distinct keys** and the aggregate state size.

`GroupAgg` requires the input sorted on the grouping columns.  It streams output
one group at a time and holds only one group's state in memory.  The planner
prefers it when:

- Input arrives pre-sorted (index scan or explicit sort already in the plan).
- Cardinality is so high that HashAgg would spill to disk.

Force GroupAgg to compare plans or measure spill behaviour:

```sql
SET enable_hashagg = off;
EXPLAIN (ANALYZE, BUFFERS)
SELECT region, COUNT(*) FROM orders GROUP BY region;
SET enable_hashagg = on;
```

When `HashAgg` spills, look for `"Batches: N"` (N > 1) in `EXPLAIN ANALYZE`
output.  Increasing `work_mem` reduces batches.  Alternatively, accept the sort
cost of `GroupAgg`.

```sql
SET work_mem = '256MB';
EXPLAIN (ANALYZE, BUFFERS)
SELECT region, channel, SUM(amount) FROM orders GROUP BY region, channel;
```

## Partial Aggregation and Parallelism

When a parallel plan is chosen the planner emits a **Partial Aggregate** in
each worker and a **Finalize Aggregate** in the leader.  Workers reduce their
local share.  The leader merges the partial states.  Most built-in aggregates
support this path (`COUNT`, `SUM`, `MIN`, `MAX`, `AVG`, ...).

```
Finalize Aggregate
  ->  Gather
        Workers Planned: 4
        ->  Partial Aggregate
              ->  Parallel Seq Scan on orders
```

Custom aggregates must declare `combinefunc` (and optionally `serialfunc` /
`deserialfunc`) to participate in partial mode.  If the aggregate lacks a
`combinefunc` the planner falls back to a non-parallel plan for that aggregate.
`array_agg`, `string_agg`, and ordered-set aggregates do not support partial
mode.  They force serialization to the leader instead.

Confirm parallel aggregation is active:

```sql
EXPLAIN (ANALYZE, VERBOSE)
SELECT region, SUM(amount) FROM orders GROUP BY region;
-- Look for "Partial Aggregate" and "Finalize Aggregate" nodes
```

## Window Functions vs GROUP BY + Join-Back

Window functions preserve one output row per input row.  A common anti-pattern
groups and then joins back to recover detail columns.

```sql
-- Anti-pattern: GROUP BY then re-join for detail, two scans
SELECT o.order_id, o.amount, agg.region_total
FROM orders o
JOIN (
  SELECT region, SUM(amount) AS region_total FROM orders GROUP BY region
) agg ON agg.region = o.region;

-- Better: window function in one pass
SELECT
  order_id,
  amount,
  SUM(amount) OVER (PARTITION BY region) AS region_total
FROM orders;
```

The window version scans `orders` once.  The GROUP BY version scans it twice
(once for the subquery, once for the outer join) unless the planner can fold
them via a materialize node.  Window aggregates also support `ROWS`/`RANGE`
framing for running totals and moving averages without any self-join.

## Multiple Aggregations on the Same Column

PostgreSQL evaluates all aggregates over the same input in a **single pass**.
Never split aggregates across subqueries to avoid re-scanning.

```sql
-- All computed in one scan of the same rows
SELECT
  COUNT(*)                                        AS total_rows,
  COUNT(amount)                                   AS non_null_amounts,
  SUM(amount)                                     AS total,
  AVG(amount)                                     AS average,
  MIN(amount)                                     AS minimum,
  MAX(amount)                                     AS maximum,
  STDDEV(amount)                                  AS std_dev,
  PERCENTILE_CONT(0.5) WITHIN GROUP
    (ORDER BY amount)                             AS median
FROM orders
WHERE created_at >= '2024-01-01';
```

`advance_aggregates` in `nodeAgg.c` iterates all `AggStatePerAgg` slots for
each input tuple, so the number of aggregates does not increase scan cost.

## Practical Guidance

| Situation | Recommendation |
|---|---|
| Conditional counts/sums | `FILTER` clause, not `CASE`/subqueries |
| Subtotals + grand total | `ROLLUP` |
| All cross-combinations | `CUBE` (watch N) |
| Arbitrary grouping sets | `GROUPING SETS` |
| p50/p95/p99 | `percentile_cont` `WITHIN GROUP` |
| High-cardinality HashAgg spill | Raise `work_mem` or `enable_hashagg=off` |
| Aggregate + keep row detail | Window function, not GROUP BY+join |
| Correlated subquery aggregate | Pre-aggregate subquery + LEFT JOIN |
| Parallel plan not appearing | Check `max_parallel_workers_per_gather` and aggregate `combinefunc` |

Always run `EXPLAIN (ANALYZE, BUFFERS)` to confirm the chosen strategy.
Check `Batches` on HashAgg nodes and `Workers Launched` on Gather nodes.

## Related Topics

- [[subsystems/executor/aggregate|Aggregate Executor Node]] — covers the internals of `nodeAgg.c`, the HashAgg and GroupAgg strategies, and how transition functions advance aggregate state.
- [[subsystems/executor/work-mem-and-spill|work_mem and Spill]] — explains how `work_mem` controls when HashAgg batches to disk and how to read spill metrics from `EXPLAIN ANALYZE`.
- [[subsystems/planner/partial-aggregation|Partial Aggregation]] — details how the planner emits Partial and Finalize Aggregate nodes for parallel plans and what aggregate properties are required.
- [[sql-features/advanced-aggregation|Advanced Aggregation]] — SQL-level reference for `GROUPING SETS`, `ROLLUP`, `CUBE`, ordered-set aggregates, and the `GROUPING()` function.
- [[sql-features/aggregate-modifiers|Aggregate Modifiers: FILTER, ORDER BY, DISTINCT]] — full syntax and execution path for the per-aggregate modifiers introduced above.
- [[sql-features/ordered-set-aggregates|Ordered-Set Aggregates and WITHIN GROUP]] — syntax and internals for `percentile_cont`, `percentile_disc`, `mode`, and the hypothetical-set rank functions.
- [[subsystems/executor/window-functions-performance|Window Functions Performance]] — complements the window-vs-GROUP-BY patterns with framing, partition sizing, and index strategies for window aggregates.
- [[subsystems/planner/cost-model|Planner Cost Model]] — shows how the planner estimates HashAgg vs GroupAgg cost, including cardinality estimates that drive strategy selection.
- [[subsystems/executor/sort|Sort Node]] — ordered-set aggregates always introduce a Sort node; understanding sort internals helps diagnose their memory and I/O behaviour.
- [[subsystems/executor/window-functions|Window Functions]] — the WindowAgg executor node behind the "Window Functions vs GROUP BY" pattern above, which evaluates aggregate-like results without collapsing rows.
