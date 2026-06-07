---
title: Window Function Performance
aliases:
  - WindowAgg Performance
  - Window Function Tuning
tags:
  - theme/query-optimization
source_files:
  - src/backend/executor/nodeWindowAgg.c
  - src/backend/optimizer/plan/planner.c
  - src/include/nodes/plannodes.h
symbols:
  - ExecWindowAgg
  - WindowAggState
  - initialize_peragg
  - eval_windowaggregates
  - WindowAgg
  - wfuncno
---

Window functions compute results over a "window" of rows related to the current row and return one output row per input row. This differs from `GROUP BY`, which collapses groups. The executor node responsible is `WindowAgg` (`src/backend/executor/nodeWindowAgg.c`). It requires input that arrives pre-sorted. `ExecWindowAgg` iterates rows and maintains per-partition and per-frame state through transition functions stored in `WindowAggState`. The planner constructs a plan with `WindowAgg` nodes stacked above `Sort` nodes as needed.

## Sort Requirements

`WindowAgg` requires input sorted by `(PARTITION BY cols, ORDER BY cols)` in that order. If no suitable index exists, the planner inserts a `Sort` node. This is visible in `EXPLAIN`:

```sql
EXPLAIN SELECT user_id, amount,
    SUM(amount) OVER (PARTITION BY user_id ORDER BY created_at)
FROM orders;
```

```
WindowAgg
  ->  Sort
        Sort Key: user_id, created_at
        ->  Seq Scan on orders
```

### Multiple Window Functions

If two window functions have different `PARTITION BY` or `ORDER BY` clauses, the planner generates separate `WindowAgg` nodes, each with its own sort. The plan stacks them:

```sql
SELECT
    SUM(amount) OVER (PARTITION BY user_id ORDER BY created_at),
    RANK()      OVER (PARTITION BY region  ORDER BY amount DESC)
FROM orders;
```

This results in two `Sort` nodes and two `WindowAgg` nodes in the plan tree — one sort pass per distinct window specification. Reducing the number of distinct window specs is the single highest-leverage optimization for multi-window queries.

## Avoiding the Sort

An index on `(partition_col, order_col)` allows `WindowAgg` to receive rows in the required order from an `Index Scan`. This eliminates the `Sort` node entirely and uses the same mechanism as sort avoidance for `ORDER BY`.

```sql
CREATE INDEX ON orders (user_id, created_at);

EXPLAIN SELECT user_id, amount,
    SUM(amount) OVER (PARTITION BY user_id ORDER BY created_at)
FROM orders;
```

```
WindowAgg
  ->  Index Scan using orders_user_id_created_at_idx on orders
        Index Cond: ...
```

No `Sort` node appears. For large tables, eliminating an `O(n log n)` sort pass can be decisive. The same index can serve multiple window functions that share the same leading columns.

## PARTITION BY Cardinality Effects

PARTITION BY cardinality determines how frequently `WindowAgg` resets its aggregate state.

**High cardinality** (e.g., `PARTITION BY user_id` with millions of users): many small partitions. `WindowAgg` calls the transition function a few times per partition, then resets. Per-partition overhead dominates; aggregate state accumulation is minimal.

**Low cardinality** (e.g., `PARTITION BY region` with 5 regions): few large partitions. Aggregate state accumulates over many rows. For aggregate functions with expensive `finalfn` or large internal state (e.g., `array_agg`), memory pressure and finalfn cost grow.

**Pathological case — PARTITION BY on a unique key**: each partition contains exactly one row. Every window function call degenerates to a scalar computation. The partitioning overhead produces zero benefit and wastes planning and runtime cost. Replace with a plain expression or subquery.

## ROWS vs RANGE Frames

Frame mode has a large impact on per-row cost. `ROWS` boundaries are `O(1)` arithmetic on row position. `RANGE` boundaries require scanning for a value match against the `ORDER BY` column. A `RANGE` frame stays cheap only when the frame start advances monotonically and the aggregate has an inverse transition function. [[sql-features/window-frame-clauses|Window Frame Clauses]] covers the full complexity analysis — why `RANGE` without an inverse transition function degrades to `O(partition_size²)`, and how sliding-window subtraction keeps `ROWS` and well-behaved `RANGE` frames at `O(n)`. The practical guidance below (prefer `ROWS`, avoid `MAX`/`MIN` on large `RANGE` frames) follows directly from that analysis.

## Performance Characteristics by Pattern

| Pattern | Complexity | Notes |
|---|---|---|
| `SUM(...) OVER (ORDER BY ...)` running total | O(n) | Inverse transition function; no frame restart |
| `LAG` / `LEAD` | O(1) per row | Simple offset lookup; no aggregation |
| `RANK` / `ROW_NUMBER` | O(n) | No aggregation; cheap state update |
| `FIRST_VALUE` / `LAST_VALUE` | O(1) per row with ROWS | O(partition) per row with RANGE if no inverse fn |
| Moving avg, `ROWS` frame, SUM-based | O(n) | Fast sliding window |
| `MAX` / `MIN`, large `RANGE` frame | O(n²) worst case | No inverse transition function |
| `array_agg` over large partition | O(n) memory | Accumulates entire partition in memory |

## Practical Guidance

**Create covering indexes for window specs.** An index on `(partition_col, order_col)` eliminates a sort pass. If multiple window functions share the same leading partition column, one index can serve all of them.

**Consolidate window specifications.** Rewrite queries to use a single `OVER (...)` clause where possible, or use a CTE to compute one window result and reuse it. Each distinct window spec adds a sort pass.

**Prefer `ROWS` over `RANGE` frames when semantics allow.** `ROWS BETWEEN N PRECEDING AND CURRENT ROW` is almost always faster than an equivalent `RANGE` frame because boundary computation is O(1).

**Avoid `MAX`/`MIN` in sliding-window patterns.** These lack an inverse transition function. If you need a moving maximum, consider a self-join with a lateral, a recursive CTE, or a specialized data structure outside PostgreSQL.

**Never use `PARTITION BY` on a unique column.** Check for this in slow queries. It is a common mistake that wastes the partitioning mechanism entirely.

**Check `EXPLAIN (ANALYZE, BUFFERS)` for Sort nodes.** A `Sort` node with high actual rows and high shared hit/read counts is a candidate for index creation. Sort time appears in the `actual time` field of the Sort node.

**Use `EXPLAIN (ANALYZE)` to spot large WindowAgg nodes.** If `WindowAgg` accounts for most of the query time, profile frame type, aggregate type, and partition size. Switching from `RANGE` to `ROWS` or replacing `MAX`/`MIN` with a different approach often yields order-of-magnitude improvements.

**Prefer lateral joins or self-joins for sparse lookups.** If you only need the "most recent event before X" for a small result set, a `LATERAL` subquery with `LIMIT 1` will outperform a window function. The window function has to scan the full partition, while the lateral subquery does not.

```sql
-- Potentially slow: full window scan
SELECT DISTINCT ON (user_id) user_id,
    FIRST_VALUE(event) OVER (PARTITION BY user_id ORDER BY created_at DESC)
FROM events;

-- Faster for sparse access:
SELECT u.id, e.event
FROM users u
CROSS JOIN LATERAL (
    SELECT event FROM events
    WHERE user_id = u.id
    ORDER BY created_at DESC
    LIMIT 1
) e;
```

## Related Topics

- [[subsystems/executor/window-functions|Window Functions]] — covers the WindowAgg executor node internals, frame evaluation, and transition function mechanics that underlie the performance characteristics described here
- [[sql-features/window-functions|Window Functions (SQL)]] — SQL-level reference for window function syntax, frame specifications, and built-in window functions
- [[sql-features/window-frame-clauses|Window Frame Clauses]] — canonical complexity analysis for ROWS/RANGE/GROUPS frame modes and inverse transition functions
- [[sql-features/window-functions-builtins|Window Function Builtins]] — details on built-in window functions including LAG, LEAD, RANK, and FIRST_VALUE and their internal implementations
- [[subsystems/planner/sort-avoidance|Sort Avoidance]] — explains how the planner eliminates Sort nodes by exploiting index order, directly applicable to WindowAgg sort elimination
- [[subsystems/executor/sort|Sort]] — internals of the Sort executor node that WindowAgg depends on when no suitable index is available
- [[subsystems/executor/incremental-sort|Incremental Sort]] — an optimization that can reduce sort cost for WindowAgg when input is partially ordered on the partition or order key
- [[subsystems/planner/lateral-joins|Lateral Joins]] — describes LATERAL subqueries, which are often a faster alternative to window functions for sparse per-row lookups
