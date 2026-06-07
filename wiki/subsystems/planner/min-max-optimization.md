---
title: "MIN/MAX Aggregate Optimization"
aliases:
  - min/max index optimization
  - minmax agg
  - planagg
tags:
  - theme/query-optimization
source_files:
  - src/backend/optimizer/plan/planagg.c
symbols:
  - preprocess_minmax_aggregates
  - build_minmax_path
  - can_minmax_aggs
  - MinMaxAggInfo
  - MinMaxAggPath
---

When a query asks for `MIN(col)` or `MAX(col)` over an indexed column, PostgreSQL's planner can avoid scanning the entire table. Instead of aggregating every row through an Agg node, it rewrites the aggregate into a `LIMIT 1` index scan in the appropriate direction. This fetches only the one row that holds the answer. `planagg.c` implements this optimization. It fires entirely at plan time, before any executor work begins.

## The Rewrite Strategy

The central idea is straightforward: the minimum value of an indexed column is the first row when the planner reads that index in ascending order. The maximum is the first row when the planner reads it in descending order. So `SELECT MIN(col) FROM t` is semantically equivalent to:

```sql
SELECT col FROM t
WHERE col IS NOT NULL
ORDER BY col ASC
LIMIT 1
```

The planner synthesizes this subquery internally. It runs `query_planner()` on it. If a suitable index path exists, it packages the result as a `MinMaxAggPath`. This path competes in the normal path-selection process against the standard aggregate implementation. It wins whenever the index scan cost is lower, which is almost always the case for large tables.

Each MIN or MAX aggregate in the target list gets its own independent subquery. A query like `SELECT MIN(a), MAX(b) FROM t` produces two separate `LIMIT 1` scans, each potentially using a different index. The planner collects the results as initplan parameters. It substitutes them into the final output.

## Eligibility Checks

`preprocess_minmax_aggregates()` runs a gauntlet of structural checks before attempting path construction. The optimization is all-or-nothing: if the planner cannot optimize any aggregate in the query, it abandons the entire rewrite and falls back to the standard Agg path.

**Query-level requirements:**

| Condition | Reason |
|---|---|
| No `GROUP BY` (or only empty grouping sets) | Grouped aggregation must visit all rows anyway; there is no benefit |
| No window functions | Same reasoning — a full pass is required |
| No CTEs | Index scans cannot be built over CTEs |
| Exactly one base table (no joins) | Join conditions cannot be pushed into the synthesized subquery |

**Per-aggregate requirements** (`can_minmax_aggs()`):

| Condition | Reason |
|---|---|
| Aggregate has a sort operator in `pg_aggregate.aggsortop` | This is what distinguishes MIN/MAX from other aggregates |
| Exactly one argument | MIN/MAX always take a single column expression |
| No `ORDER BY` within the aggregate call | Ordered-set aggregates (e.g., `percentile_cont`) are excluded |
| No `FILTER` clause | Filter semantics are not yet pushed into the synthesized WHERE |
| Argument contains no mutable functions | Mutable expressions are not indexable in a stable way |
| Argument is not a row type | `IS NOT NULL` on composite types has surprising semantics |

The sort operator lookup via `fetch_agg_sort_op()` is the cleanest way to identify MIN/MAX: the system catalog records the comparison operator that defines the aggregate's ordering semantics. The planner will not rewrite any aggregate lacking this entry — including user-defined aggregates that happen to compute a minimum.

## Path Construction

`build_minmax_path()` constructs a cloned `PlannerInfo` representing the synthetic subquery. The clone increments the query level. This promotes any outer Var references one level up. That makes the subquery eligible to become an initplan.

The synthesized query has:
- A target list containing only the aggregate's argument expression.
- An explicit `col IS NOT NULL` added to the WHERE clause (unless already present), so the index scan stops at the first non-null value.
- An `ORDER BY col ASC` (for MIN) or `ORDER BY col DESC` (for MAX) clause, derived from the aggregate's sort operator.
- `LIMIT 1`.

The planner invokes `query_planner()` on this modified query with `tuple_fraction = 1.0` and `limit_tuples = 1.0`. This signals that only one row is needed. The planner then finds the cheapest pre-sorted path — that is, a path whose output is already ordered by the required sort key without an additional Sort node. For an index scan on a btree index covering the aggregate column, the index naturally provides this ordering at very low cost.

The planner tries the NULL-handling direction (NULLS FIRST vs NULLS LAST) both ways, stopping at the first success. Either ordering is correct — they differ only in whether null-valued index entries appear at the physical start or end of the scan.

If `get_cheapest_fractional_path_for_pathkeys()` returns nothing — meaning no pre-sorted path exists — `build_minmax_path()` returns false. The planner then abandons the optimization for the entire query.

## Cost Model and Path Competition

The planner computes the synthesized path's cost as:

```
path_cost = startup_cost + path_fraction * (total_cost - startup_cost)
```

where `path_fraction = 1.0 / rows` for tables with more than one row. This reflects the cost of fetching only the first row from the path. The planner adds the resulting `MinMaxAggPath` to the `UPPERREL_GROUP_AGG` upper relation, where it competes against the standard aggregate plan under the normal rules for cost-based selection. The index-scan path wins decisively when the table is large. On tiny tables, the margin may be negligible.

## When the Optimization Does Not Fire

Understanding the failure modes helps explain surprising `EXPLAIN` output:

- **Non-MIN/MAX aggregates in the same query.** The planner cannot optimize `SELECT MIN(a), COUNT(*) FROM t` because `COUNT(*)` has no sort operator. The entire query falls back to a sequential scan with an Agg node.
- **No btree index on the aggregate argument.** The synthetic subquery requires a pre-sorted path. Hash indexes, GIN, GiST, and BRIN indexes do not provide ordering.
- **Partial indexes that do not cover all rows.** The planner can use a partial index if its predicate subsumes the WHERE clause of the synthetic subquery, but only if the WHERE clause of the existing query implies the index predicate.
- **GROUP BY present.** Even a single-column `GROUP BY` blocks the rewrite. Each group needs its own minimum or maximum. This requires a full scan.
- **FILTER clause on the aggregate.** The planner does not yet handle `MIN(col) FILTER (WHERE ...)`. The filter would need to be spliced into the subquery's WHERE clause.
- **Expression arguments containing mutable functions.** `MIN(random())` is not indexable. The planner rejects it early.

## See also

- [[subsystems/planner/scan-selection|index scan paths]] — how btree index paths are generated and costed
- [[subsystems/executor/aggregate|the standard Agg node]] — the standard Agg node that this optimization replaces
- [[subsystems/memory/resource-owner]] — initplan parameter management
