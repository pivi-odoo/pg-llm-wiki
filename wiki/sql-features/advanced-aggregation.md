---
title: "Advanced Aggregation"
aliases:
  - "GROUPING SETS"
  - "ROLLUP"
  - "CUBE"
  - "ordered-set aggregates"
source_files:
  - src/backend/executor/nodeAgg.c
  - src/backend/optimizer/plan/planner.c
  - src/include/executor/nodeAgg.h
  - src/include/nodes/parsenodes.h
  - src/include/nodes/pathnodes.h
symbols:
  - AggState
  - GroupingSet
  - GroupingSetKind
  - GroupingSetData
  - RollupData
  - expand_grouping_sets
  - extract_rollup_sets
  - reorder_grouping_sets
  - initialize_aggregates
  - advance_aggregates
  - finalize_aggregate
  - ExecAgg
---

Advanced aggregation covers the set of SQL features that go beyond a plain `GROUP BY`: multi-dimensional grouping with `GROUPING SETS`, `ROLLUP`, and `CUBE`; conditional aggregation with `FILTER`; ordering and deduplication within aggregates; and the ordered-set and hypothetical-set aggregate families. Together they eliminate most of the awkward `UNION ALL` stacking and application-side pivoting that otherwise clutters reporting queries.

See [[subsystems/executor/aggregate]] for how PostgreSQL executes aggregates internally, and [[subsystems/planner/partial-aggregation]] for how the planner parallelises them.

## GROUPING SETS

`GROUPING SETS` lets a single query produce results for multiple grouping combinations simultaneously. Each set in the list is one `GROUP BY` grouping; the results are concatenated like a `UNION ALL`, but the table is scanned only once.

```sql
SELECT region, product, SUM(sales)
FROM orders
GROUP BY GROUPING SETS ((region, product), (region), ());
```

This yields three bands of rows: one grouped by `(region, product)`, one by `region` alone, and one grand total where both columns are `NULL`. Columns not part of the current grouping set appear as `NULL` in that band's rows. This creates an ambiguity when the source data itself can contain `NULL`. The `GROUPING()` function resolves this (see below).

## ROLLUP

`ROLLUP` is shorthand for a hierarchy of grouping sets from most specific to the grand total:

```sql
GROUP BY ROLLUP (year, quarter, month)
-- expands to:
GROUP BY GROUPING SETS ((year, quarter, month), (year, quarter), (year), ())
```

The canonical use case is a report that needs subtotals at each level. A sales report grouping by year, quarter, and month with `ROLLUP` returns monthly figures, quarterly subtotals, annual subtotals, and a grand total in a single result set.

## CUBE

`CUBE` generates every possible combination of the listed columns — `2^N` grouping sets for `N` columns:

```sql
GROUP BY CUBE (region, category)
-- expands to:
GROUP BY GROUPING SETS ((region, category), (region), (category), ())
```

Four sets for two columns is manageable. Six columns produce 64 sets. This volume almost always taxes both memory and patience. Keep `CUBE` to three or four columns at most. Use it for cross-tabulation reports where every marginal total is genuinely needed.

## The GROUPING() function

When `NULL` appears in a grouping column, it could mean either "this row is a subtotal where this column was aggregated away" or "the source data had a NULL in that column." `GROUPING()` distinguishes the two cases: it returns `1` for each argument that is being aggregated away in the current grouping set, and `0` when the column is a real grouping key.

```sql
SELECT
    CASE GROUPING(region) WHEN 1 THEN 'All regions' ELSE region END AS region,
    CASE GROUPING(product) WHEN 1 THEN 'All products' ELSE product END AS product,
    SUM(sales) AS total_sales
FROM orders
GROUP BY ROLLUP (region, product);
```

The `CASE` expressions replace the grouping-NULLs with human-readable labels while leaving source NULLs untouched (or handled separately). With multiple columns, `GROUPING(a, b)` returns a bitmask: bit 1 (value 2) for `a`, bit 0 (value 1) for `b`. `GROUPING(a, b) = 3` means both columns are rolled up — the grand total row.

```sql
SELECT
    region,
    product,
    SUM(sales)                                    AS sales,
    GROUPING(region, product)                     AS grp_mask
FROM orders
GROUP BY ROLLUP (region, product)
ORDER BY GROUPING(region, product), region, product;
```

Sorting by `GROUPING(...)` pushes subtotal rows to the bottom. This is the standard report layout.

## FILTER, ORDER BY, and DISTINCT modifiers

Aggregate calls also accept three optional modifiers that reshape which rows reach the aggregate and in what sequence. `FILTER (WHERE ...)` restricts a single aggregate to a subset of the group's rows. An inner `ORDER BY` controls input order for order-sensitive aggregates like `string_agg` and `array_agg`. `DISTINCT` deduplicates input before accumulation. All three compose with `GROUPING SETS`, `ROLLUP`, and `CUBE` — each modifier applies per aggregate, independent of the grouping structure. See [[sql-features/aggregate-modifiers]] for the full syntax, the `Aggref` node fields that carry each modifier, execution paths, and why `ORDER BY`/`DISTINCT` block hash aggregation and parallelism.

## Ordered-set aggregates

The `percentile_cont`, `percentile_disc`, and `mode` aggregates require the `WITHIN GROUP (ORDER BY ...)` syntax because they are inherently defined over an ordered set of values, not a bag:

```sql
SELECT
    percentile_cont(0.5)  WITHIN GROUP (ORDER BY response_ms) AS median,
    percentile_cont(0.95) WITHIN GROUP (ORDER BY response_ms) AS p95,
    percentile_disc(0.5)  WITHIN GROUP (ORDER BY response_ms) AS median_disc,
    mode()                WITHIN GROUP (ORDER BY status)       AS most_common_status
FROM requests;
```

`percentile_cont` interpolates between adjacent values to return a precise fractional position. `percentile_disc` returns the smallest value whose cumulative distribution is at least the requested fraction — always an actual value from the data. For integer or categorical data where interpolation is meaningless, `percentile_disc` is the right choice. `mode()` returns the most frequent value, with ties broken arbitrarily.

These aggregates must sort their input, so their cost scales with `O(N log N)`. There is no shortcut for arbitrary percentiles without sorting.

## Hypothetical-set aggregates

`rank`, `dense_rank`, `percent_rank`, and `cume_dist` each have a `WITHIN GROUP` form that answers the question "where would this value fall if it were added to the group?":

```sql
SELECT
    rank(500)          WITHIN GROUP (ORDER BY score DESC) AS hypothetical_rank,
    percent_rank(500)  WITHIN GROUP (ORDER BY score DESC) AS hypothetical_pct_rank
FROM leaderboard;
```

The argument to the function is the hypothetical value. `ORDER BY` defines the ranking order over the actual rows. This is useful for "what percentile is this threshold?" queries without materialising the full ranking.

## Common patterns

**Subtotals report with ROLLUP and GROUPING()**

```sql
SELECT
    COALESCE(region, 'Grand Total')   AS region,
    COALESCE(product, 'Subtotal')     AS product,
    SUM(revenue)                      AS revenue
FROM sales
GROUP BY ROLLUP (region, product)
HAVING GROUPING(region) = 0 OR SUM(revenue) > 0
ORDER BY GROUPING(region, product), region, product;
```

`GROUPING(region) = 0` in the `HAVING` clause keeps only rows where `region` is a real grouping column (or the grand total), avoiding phantom subtotals for regions that happen to have zero revenue.

**Median and percentiles**

```sql
SELECT
    department,
    percentile_cont(0.5) WITHIN GROUP (ORDER BY salary) AS median_salary,
    percentile_cont(0.9) WITHIN GROUP (ORDER BY salary) AS p90_salary
FROM employees
GROUP BY department;
```

## Related Topics

- [[subsystems/executor/aggregate|Aggregate Executor Node]] — internals of how PostgreSQL executes aggregate functions, including hash and sorted strategies that power GROUPING SETS.
- [[subsystems/planner/partial-aggregation|Partial Aggregation]] — how the planner splits aggregate work across parallel workers, including interactions with ROLLUP and CUBE.
- [[sql-features/aggregate-modifiers|Aggregate Modifiers: FILTER, ORDER BY, DISTINCT]] — the per-aggregate modifiers, their `Aggref` representation, and why they block hash aggregation and partial aggregation.
- [[sql-features/window-functions|Window Functions]] — closely related ordered and ranking functions (RANK, DENSE_RANK, PERCENT_RANK) that operate over partitions rather than groups.
- [[sql-features/window-functions-builtins|Window Function Built-ins]] — reference for the built-in window functions including the non-aggregate forms of hypothetical-set functions.
- [[subsystems/executor/group-by|GROUP BY Executor]] — the executor node that implements plain GROUP BY, on top of which GROUPING SETS is layered.
- [[subsystems/executor/aggregation-recipes|Aggregation Recipes]] — practical patterns for common aggregation problems that complement the advanced syntax covered here.
- [[subsystems/parser/aggregate-analysis|Aggregate Analysis]] — how the parser and semantic analysis phase validates aggregate expressions, WITHIN GROUP syntax, and FILTER clauses.
