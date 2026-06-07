---
title: "Window Functions"
aliases:
  - "OVER clause"
  - "window frame"
  - "PARTITION BY"
source_files:
  - src/backend/executor/nodeWindowAgg.c
  - src/backend/utils/adt/windowfuncs.c
  - src/backend/optimizer/plan/createplan.c
  - src/include/nodes/execnodes.h
  - src/include/nodes/parsenodes.h
  - src/include/nodes/primnodes.h
symbols:
  - ExecWindowAgg
  - ExecInitWindowAgg
  - ExecEndWindowAgg
  - WindowAggState
  - WindowStatePerFunc
  - WindowStatePerAgg
  - update_frameheadpos
  - update_frametailpos
  - spool_tuples
  - create_windowagg_plan
  - make_windowagg
  - WindowClause
  - WindowFunc
  - window_row_number
  - window_rank
  - window_dense_rank
  - window_lag
  - window_lead
  - window_first_value
  - window_last_value
  - window_nth_value
---

# Window Functions

Window functions are SQL expressions using an `OVER` clause that compute a value for each output row from a related set of rows, without collapsing rows the way `GROUP BY` does. This article covers the SQL-visible constructs: the `OVER` clause, `PARTITION BY`, `ORDER BY`, frame specifications, built-in function categories, and common patterns. For how PostgreSQL executes them internally, see [[subsystems/executor/window-functions]].

## The OVER Clause

Every window function call requires an `OVER` clause, which defines the window. Its three components are independent:

```sql
function_name(...) OVER (
    PARTITION BY col1, col2
    ORDER BY col3 DESC
    ROWS BETWEEN 1 PRECEDING AND CURRENT ROW
)
```

**`PARTITION BY`** divides the result set into independent groups. The window function resets at each partition boundary, exactly as if you ran the function separately on each subset. Omitting `PARTITION BY` treats the entire result set as one partition.

**`ORDER BY`** defines row ordering within a partition. This is not the same as the query-level `ORDER BY` — it controls the ordering for the window computation, not the order in which rows appear in the result. Ranking functions and frame-sensitive aggregates require it.

**Frame specification** defines which rows relative to the current row contribute to the computation. This is explained in detail below.

A minimal window function uses none of these extras:

```sql
SELECT dept, salary,
    AVG(salary) OVER () AS company_avg
FROM employees;
```

The empty `OVER ()` computes a single average over all rows and attaches it to every row.

## Frame Specification

The frame is the subset of rows within a partition that contribute to the current row's function result. When `ORDER BY` is present in the `OVER` clause, the default frame is `RANGE BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW` — all rows from the start of the partition through the current peer group.

Frame specifications take the form:

```sql
{ ROWS | RANGE | GROUPS } BETWEEN <start> AND <end>
```

### Frame Modes

**`ROWS`** counts physical row offsets. `ROWS BETWEEN 2 PRECEDING AND CURRENT ROW` always includes exactly three rows (or fewer at the start of a partition). PostgreSQL computes boundaries by arithmetic on position, making this the cheapest mode.

```sql
-- 7-row moving average, strictly by row count
AVG(value) OVER (ORDER BY date ROWS BETWEEN 6 PRECEDING AND CURRENT ROW)
```

**`RANGE`** uses value offsets. `RANGE BETWEEN INTERVAL '7 days' PRECEDING AND CURRENT ROW` includes all rows whose `ORDER BY` value falls within 7 days of the current row's value. This handles ties naturally — rows with equal `ORDER BY` values always share a frame boundary — but requires a single `ORDER BY` column with a compatible type.

```sql
-- All rows within 7 days of the current row's date
SUM(amount) OVER (ORDER BY created_at RANGE BETWEEN INTERVAL '7 days' PRECEDING AND CURRENT ROW)
```

**`GROUPS`** counts peer groups (sets of rows with identical `ORDER BY` values) rather than individual rows. `GROUPS BETWEEN 1 PRECEDING AND CURRENT ROW` includes the current peer group plus the one immediately before it.

### Frame Boundaries

[[sql-features/window-frame-clauses|Window Frame Clauses]] covers frame boundaries (`UNBOUNDED PRECEDING`, `N PRECEDING`, `CURRENT ROW`, `N FOLLOWING`, `UNBOUNDED FOLLOWING`), the `EXCLUDE` clause, and the performance trade-offs between `ROWS`/`RANGE`/`GROUPS`. When `ORDER BY` is absent, all rows in the partition are peers. The default frame is then the entire partition.

## Categories of Window Functions

### Ranking Functions

Ranking functions assign a position to each row within its partition. They require `ORDER BY` in the `OVER` clause. They ignore the frame specification.

| Function | Behavior |
|---|---|
| `row_number()` | Unique sequential integer, no ties |
| `rank()` | Tied rows share a rank; next rank skips (1, 1, 3) |
| `dense_rank()` | Tied rows share a rank; no gaps (1, 1, 2) |
| `ntile(n)` | Divides partition into n buckets |
| `percent_rank()` | Relative rank as a fraction from 0 to 1 |
| `cume_dist()` | Cumulative distribution: fraction of rows ≤ current |

```sql
SELECT dept, name, salary,
    rank()       OVER (PARTITION BY dept ORDER BY salary DESC) AS rank,
    dense_rank() OVER (PARTITION BY dept ORDER BY salary DESC) AS dense_rank,
    row_number() OVER (PARTITION BY dept ORDER BY salary DESC) AS row_num
FROM employees;
```

The distinction between `rank()` and `dense_rank()` matters when there are ties: for salaries 100, 100, 80, `rank()` gives 1, 1, 3 while `dense_rank()` gives 1, 1, 2.

### Value and Offset Functions

These functions fetch values from specific positions within the partition or frame, rather than aggregating.

**`lag(col, n, default)`** returns the value of `col` from `n` rows before the current row. **`lead(col, n, default)`** does the same for rows ahead. Both are frame-independent — they always look at physical row offsets.

```sql
SELECT date, revenue,
    lag(revenue, 1, 0) OVER (ORDER BY date) AS prev_revenue,
    revenue - lag(revenue, 1, 0) OVER (ORDER BY date) AS day_over_day
FROM daily_sales;
```

**`first_value(col)`** and **`last_value(col)`** return the first and last values within the current frame. A critical pitfall: `last_value` with the default frame (`RANGE UNBOUNDED PRECEDING AND CURRENT ROW`) returns the current row's own value, not the partition maximum, because the frame ends at the current row. To get the true last value in the partition:

```sql
-- WRONG: returns current row's value, not partition max
last_value(salary) OVER (PARTITION BY dept ORDER BY salary)

-- CORRECT: expand the frame to cover the full partition
last_value(salary) OVER (
    PARTITION BY dept
    ORDER BY salary
    ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING
)
```

**`nth_value(col, n)`** returns the value from the nth row of the frame (1-indexed). It returns NULL when the frame has fewer than n rows.

### Aggregate Functions as Window Functions

You can use any aggregate function — `sum`, `avg`, `count`, `max`, `min`, `array_agg`, and user-defined aggregates — as a window function by adding `OVER()`. The aggregate is computed over the current frame rather than collapsing rows.

```sql
SELECT user_id, order_date, amount,
    SUM(amount) OVER (PARTITION BY user_id ORDER BY order_date) AS running_total,
    COUNT(*)    OVER (PARTITION BY user_id)                     AS user_order_count
FROM orders;
```

## Common Patterns

**Running total**: accumulate a sum from the start of the partition to the current row.

```sql
SELECT user_id, order_date, amount,
    SUM(amount) OVER (PARTITION BY user_id ORDER BY order_date) AS running_total
FROM orders;
```

**Moving average**: average over a sliding window of rows.

```sql
SELECT date, temperature,
    AVG(temperature) OVER (
        ORDER BY date
        ROWS BETWEEN 6 PRECEDING AND CURRENT ROW
    ) AS rolling_7day_avg
FROM sensor_readings;
```

**Rank within group**: find the top earner per department.

```sql
SELECT * FROM (
    SELECT dept, name, salary,
        rank() OVER (PARTITION BY dept ORDER BY salary DESC) AS rnk
    FROM employees
) ranked
WHERE rnk = 1;
```

**Difference from previous row**: detect changes between consecutive readings.

```sql
SELECT date, value,
    value - LAG(value) OVER (ORDER BY date) AS delta
FROM measurements;
```

**Percentage of total within category**: express each row's amount as a share of its group.

```sql
SELECT category, product, revenue,
    revenue / SUM(revenue) OVER (PARTITION BY category) AS pct_of_category
FROM sales;
```

**Row deduplication — keep only the latest record per group**: combine ranking with a filter in a subquery or CTE.

```sql
SELECT user_id, event_type, created_at FROM (
    SELECT *,
        row_number() OVER (PARTITION BY user_id ORDER BY created_at DESC) AS rn
    FROM events
) sub
WHERE rn = 1;
```

## Named Windows

When multiple functions share the same partition and ordering, repeating the full `OVER (...)` clause is verbose and error-prone. The `WINDOW` clause lets you define a window once and reference it by name.

```sql
SELECT dept, name, salary,
    rank()    OVER w AS dept_rank,
    avg(salary) OVER w AS dept_avg,
    max(salary) OVER w AS dept_max
FROM employees
WINDOW w AS (PARTITION BY dept ORDER BY salary DESC);
```

You can extend a named window inline: `OVER (w ROWS BETWEEN 1 PRECEDING AND CURRENT ROW)` inherits `w`'s partition and ordering and adds a frame specification. You cannot override the partition or ordering keys this way — only add or change the frame.

## Pitfalls

**`last_value()` with the default frame.** This is the most common mistake with window functions. Because the default frame ends at `CURRENT ROW`, `last_value` returns the current row's value rather than the last row in the partition. Always specify `ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING` when you want the true final value. The same applies to `nth_value` when the target row lies beyond the current position.

**Window functions cannot appear in `WHERE` or `HAVING`.** PostgreSQL evaluates window functions after `WHERE` filtering and `GROUP BY` aggregation, but before the query-level `ORDER BY`. Filtering on the result of a window function requires wrapping the query:

```sql
-- ERROR: window functions are not allowed in WHERE
SELECT * FROM orders WHERE rank() OVER (ORDER BY amount DESC) <= 5;

-- CORRECT: wrap in a subquery or CTE
SELECT * FROM (
    SELECT *, rank() OVER (ORDER BY amount DESC) AS rnk
    FROM orders
) sub
WHERE rnk <= 5;
```

**`ORDER BY` inside `OVER` is independent of the query-level `ORDER BY`.** The `OVER (ORDER BY date)` clause controls frame ordering; it does not sort the final result. If you need the output sorted, add a separate query-level `ORDER BY`.

**Frame defaults change meaning with and without `ORDER BY`.** With no `ORDER BY`, the default frame is the entire partition (`RANGE BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING`). With `ORDER BY`, it becomes `RANGE BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW`. Switching from one to the other changes the semantics of aggregate window functions.

**`RANGE` mode requires a single `ORDER BY` column with a sortable distance.** `RANGE BETWEEN 5 PRECEDING AND CURRENT ROW` will fail unless the `ORDER BY` column has a compatible subtraction operator. Use `ROWS` mode when dealing with non-numeric types or when you want strict row-count offsets regardless of value distribution.

## Related Topics

- [[sql-features/window-frame-clauses|Window Frame Clauses]] — frame modes, boundaries, EXCLUDE semantics, and the complexity analysis behind ROWS/RANGE/GROUPS performance
- [[subsystems/executor/window-functions|Window Functions Executor]] — how PostgreSQL implements the WindowAgg node internally, including frame tracking and aggregate state reuse across rows
- [[subsystems/executor/window-functions-performance|Window Functions Performance]] — cost implications of ROWS vs RANGE vs GROUPS modes, sort requirements, and index strategies for avoiding explicit sorts
- [[sql-features/window-functions-builtins|Window Function Built-ins]] — reference for all built-in ranking, value, and offset functions available in the OVER clause
- [[sql-features/advanced-aggregation|Advanced Aggregation]] — GROUPING SETS, ROLLUP, and CUBE as alternatives when collapsing rows is acceptable rather than preserving per-row detail
- [[subsystems/executor/sort|Sort]] — the sort node that window functions depend on when no suitable index ordering exists
- [[subsystems/executor/aggregate|Aggregate]] — plain aggregate execution, which shares transition-function infrastructure with aggregate window functions
- [[subsystems/planner/sort-avoidance|Sort Avoidance]] — planner strategies for satisfying OVER ORDER BY requirements without an explicit sort step
