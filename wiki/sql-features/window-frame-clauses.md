---
title: "Window Frame Clauses"
aliases:
  - "ROWS mode"
  - "RANGE mode"
  - "GROUPS mode"
  - "EXCLUDE clause"
  - "frame specification"
  - "window frame"
source_files:
  - src/backend/executor/nodeWindowAgg.c
  - src/include/nodes/parsenodes.h
  - src/include/nodes/execnodes.h
symbols:
  - WindowDef
  - WindowClause
  - WindowAggState
  - WindowStatePerFuncData
  - WindowStatePerAggData
  - FRAMEOPTION_ROWS
  - FRAMEOPTION_RANGE
  - FRAMEOPTION_GROUPS
  - FRAMEOPTION_EXCLUSION
  - FRAMEOPTION_DEFAULTS
  - update_frameheadpos
  - update_frametailpos
  - update_grouptailpos
  - are_peers
  - startInRangeFunc
  - endInRangeFunc
---

A window frame is the subset of rows within a [[sql-features/window-functions|window function's]] partition that contributes to the result computed for the current row. It is narrower than the partition. A partition groups all rows that share the same `PARTITION BY` values. The frame further restricts that set to a neighbourhood around the current row. The frame specification in the `OVER` clause controls exactly how that neighbourhood is defined — as a count of physical rows, as a range of values, or as a count of peer groups. It also controls whether certain rows near the current position should be excluded, even if they fall within the stated boundaries.

## Frames Versus the Whole Partition

Some window functions look at the entire partition regardless of any frame specification. The ranking functions `rank()`, `dense_rank()`, `row_number()`, `percent_rank()`, and `cume_dist()` always operate over the whole partition ordering. They produce the same result whether or not a frame clause is present. The offset functions `lag()` and `lead()` use a fixed row offset within the partition. Writing a frame clause next to these functions is legal but has no effect.

Aggregate-based window functions — `sum()`, `avg()`, `count()`, `min()`, `max()`, `first_value()`, `last_value()`, `nth_value()`, and any user-defined aggregate used with `OVER` — do respect the frame. For each output row they compute their result over exactly the rows in the current frame, not over all rows in the partition. The frame therefore changes from row to row as `currentpos` advances. The executor must recompute or incrementally update the aggregate accordingly.

## Frame Modes

The frame clause begins with one of three keywords that determine the unit of measurement for the frame boundaries.

### ROWS

`ROWS` mode measures boundaries as counts of physical rows before or after the current row's position, completely independent of the values in the `ORDER BY` column. `ROWS BETWEEN 1 PRECEDING AND 1 FOLLOWING` always includes exactly three rows — the row immediately before the current row, the current row itself, and the row immediately after — regardless of whether those rows share the same `ORDER BY` value. At the start of a partition the preceding rows simply do not exist. The frame shrinks accordingly.

```sql
-- 7-row moving average: always exactly up to 7 rows, strictly by count
AVG(temperature) OVER (
    ORDER BY measured_at
    ROWS BETWEEN 6 PRECEDING AND CURRENT ROW
)
```

In the [[subsystems/executor/window-functions|WindowAgg executor node]], `ROWS` frame boundaries require only integer arithmetic on `currentpos`. The `FRAMEOPTION_ROWS` bit is set in `frameOptions` (the `WindowAggState.frameOptions` field mirrors the `WindowClause.frameOptions` bitmask from the parse tree). `update_frameheadpos()` simply computes `currentpos + offset`, clamping to the partition boundaries.

### RANGE

`RANGE` mode uses value-based distances. Instead of counting rows, the executor calls an `in_range` support function to compare the `ORDER BY` column value of a candidate row against the current row's value plus or minus the specified offset. This means that rows with equal `ORDER BY` values always land on the same side of a boundary together. For example, if the current salary is 50000 and the frame starts at `1000 PRECEDING`, the frame includes every row with salary >= 49000. This holds even if fifty rows tie at 49000.

```sql
-- Include all transactions within the past 30 days of each row's date
SUM(amount) OVER (
    ORDER BY txn_date
    RANGE BETWEEN INTERVAL '30 days' PRECEDING AND CURRENT ROW
)
```

`RANGE` with an offset (`N PRECEDING` or `N FOLLOWING`) requires a single `ORDER BY` column and a type that has a registered `in_range` support function. For numeric types the offset is of the same type as the column; for `date` and `timestamp` the offset is an `interval`. The `WindowClause` struct stores the resolved function OIDs in `startInRangeFunc` and `endInRangeFunc`. The `WindowAggState` fields `startInRangeFunc` and `endInRangeFunc` hold the resolved `FmgrInfo` at execution time. The `inRangeAsc` and `inRangeNullsFirst` flags record the sort direction so the function can be called correctly for descending orderings.

`RANGE CURRENT ROW` means "the current row's entire peer group" — all rows whose `ORDER BY` values compare equal to the current row's value. The `are_peers()` function determines this. It evaluates `ordEqfunction` (an equality expression over the `ORDER BY` columns) against two `TupleTableSlot` values. `RANGE BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW` — the default frame when `ORDER BY` is present — accumulates all rows through the current peer group. This makes it the natural choice for running totals where tied rows should be treated identically.

### GROUPS

`GROUPS` mode (available from PostgreSQL 11) uses peer groups as the counting unit. A peer group is the set of rows with identical `ORDER BY` values. `GROUPS BETWEEN 1 PRECEDING AND CURRENT ROW` includes the current peer group and the one immediately before it, however many individual rows those groups contain.

```sql
-- Rolling window of the current score tier and the two tiers before it
SUM(points) OVER (
    ORDER BY score_tier
    GROUPS BETWEEN 2 PRECEDING AND CURRENT ROW
)
```

`GROUPS` requires an `ORDER BY` clause. At execution time, `WindowAggState` maintains `currentgroup` (a monotone counter of peer groups), `frameheadgroup`, `frametailgroup`, and a `grouptailpos` position tracked via `update_grouptailpos()`. The `FRAMEOPTION_GROUPS` bit is set in `frameOptions`. For offset boundaries, `update_frameheadpos()` counts peer groups back from `currentgroup` rather than rows back from `currentpos`.

## Frame Boundaries

Both endpoints of the frame can be expressed using the same five forms:

| Syntax | Meaning |
|---|---|
| `UNBOUNDED PRECEDING` | Always the first row of the partition (position 0) |
| `N PRECEDING` | N rows / N value-units / N peer groups before the current position |
| `CURRENT ROW` | In ROWS: the current row. In RANGE/GROUPS: the current row's peer group |
| `N FOLLOWING` | N rows / N value-units / N peer groups after the current position |
| `UNBOUNDED FOLLOWING` | Always the last row of the partition |

Some combinations are forbidden: the frame start cannot be `UNBOUNDED FOLLOWING`, the frame end cannot be `UNBOUNDED PRECEDING`, and the end cannot logically precede the start. For example, `RANGE BETWEEN CURRENT ROW AND 5 PRECEDING` is rejected at parse time. By contrast, `ROWS BETWEEN 8 PRECEDING AND 7 PRECEDING` is technically valid; it simply produces an empty frame. The short form `{ ROWS | RANGE | GROUPS } frame_start` is equivalent to `BETWEEN frame_start AND CURRENT ROW`.

In the parse tree, the `WindowDef` node (before analysis) and the `WindowClause` node (after analysis, in a `Query`) both carry `startOffset` and `endOffset` expression nodes alongside the `frameOptions` bitmask. The bitmask is an OR of `FRAMEOPTION_*` constants defined in `parsenodes.h`. For example, `ROWS BETWEEN 3 PRECEDING AND CURRENT ROW` sets `FRAMEOPTION_ROWS | FRAMEOPTION_BETWEEN | FRAMEOPTION_START_OFFSET_PRECEDING | FRAMEOPTION_END_CURRENT_ROW | FRAMEOPTION_NONDEFAULT`.

## Default Frame Behaviour

When no frame clause is written, `frameOptions` is set to `FRAMEOPTION_DEFAULTS`, defined as:

```c
#define FRAMEOPTION_DEFAULTS \
    (FRAMEOPTION_RANGE | FRAMEOPTION_START_UNBOUNDED_PRECEDING | \
     FRAMEOPTION_END_CURRENT_ROW)
```

This is equivalent to `RANGE BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW`. The practical effect differs depending on whether `ORDER BY` appears in the `OVER` clause:

- **With `ORDER BY`**: the frame grows row by row, accumulating all rows through the current peer group. `SUM(amount) OVER (ORDER BY date)` produces a running total because each row's frame extends from the partition start to the end of the current row's peer group.
- **Without `ORDER BY`**: all rows in the partition are peers of each other (there is no ordering). `RANGE CURRENT ROW` therefore resolves to the entire partition. The net result is that the frame is always the full partition, equivalent to `RANGE BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING`.

This asymmetry is a common source of confusion. `SUM(amount) OVER ()` and `SUM(amount) OVER (ORDER BY date)` both use `RANGE UNBOUNDED PRECEDING AND CURRENT ROW`. But the first computes a grand total repeated on every row, while the second computes a running total.

## EXCLUDE Clause

The `EXCLUDE` clause removes specific rows from a frame that would otherwise be included by the start/end boundaries. It was introduced alongside `GROUPS` mode. The four options are:

**`EXCLUDE NO OTHERS`** — the default; nothing extra is excluded. `frameOptions` has no `FRAMEOPTION_EXCLUSION` bits set.

**`EXCLUDE CURRENT ROW`** — the current row is removed from the frame. Rows before and after it (within the boundaries) remain. Sets `FRAMEOPTION_EXCLUDE_CURRENT_ROW`.

**`EXCLUDE GROUP`** — the current row and all of its peers (rows with the same `ORDER BY` value) are excluded. Useful when you want to compute an aggregate over all other groups while seeing each row individually. Sets `FRAMEOPTION_EXCLUDE_GROUP`.

**`EXCLUDE TIES`** — the current row's peers are excluded but the current row itself is kept. In practice this is rarely needed, but it allows computing "how does this row compare to a peer-group aggregate that excludes its own duplicates". Sets `FRAMEOPTION_EXCLUDE_TIES`.

```sql
-- Each row sees the sum of all rows in the frame except itself
SUM(score) OVER (
    ORDER BY score
    ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING
    EXCLUDE CURRENT ROW
)
```

Any non-default exclusion sets at least one bit in `FRAMEOPTION_EXCLUSION` (a composite mask covering all three exclusion bits). When `FRAMEOPTION_EXCLUSION` is set, `eval_windowaggregates()` falls back to a full restart on each row rather than using inverse transition functions. This happens because exclusion means the effective frame is not a contiguous suffix of the previously accumulated state.

The `update_frameheadpos()` and `update_frametailpos()` functions deliberately compute `frameheadpos` and `frametailpos` *without* accounting for exclusion — they represent the outer frame boundaries. `row_is_in_frame()` applies the exclusion logic separately. It checks whether a candidate row position falls within the outer frame and is not filtered out by the exclusion rule. This separation avoids complicating the boundary tracking with per-row peer-group queries.

## Implementation: Frame Position Tracking

The `WindowAggState` struct (in `execnodes.h`) maintains the frame as a half-open integer interval `[frameheadpos, frametailpos)` alongside `currentpos`. Both boundary positions are computed lazily; `framehead_valid` and `frametail_valid` flags indicate whether the stored values are current for the present `currentpos`. On each output row, `ExecWindowAgg()` clears these flags and calls `update_frameheadpos()` and `update_frametailpos()` before aggregate evaluation.

For `UNBOUNDED PRECEDING` as the start, `frameheadpos` is always 0 — the function returns immediately without scanning. For value-based starts (`RANGE` with an offset or `CURRENT ROW` in `RANGE`/`GROUPS` mode), the function maintains a dedicated `framehead_ptr` read pointer into the `Tuplestorestate` buffer. It advances a cached `framehead_slot` tuple forward until `are_peers()` or `startInRangeFunc` indicates the correct boundary, without restarting from the beginning of the partition on each row. The same pattern applies to `frametail_ptr` for the frame end.

`GROUPS` mode adds `currentgroup`, `frameheadgroup`, `frametailgroup`, `groupheadpos`, and `grouptailpos` to `WindowAggState`. The `update_grouptailpos()` function finds the first row belonging to the peer group after the current one, using a `grouptail_ptr` read pointer.

## Performance Implications

The frame mode has a significant effect on aggregate computation cost.

**`UNBOUNDED PRECEDING` start (running aggregates).** When the frame head never moves backward — the common case of `RANGE UNBOUNDED PRECEDING AND CURRENT ROW` — the executor uses pure forward accumulation. `eval_windowaggregates()` tracks `aggregatedbase` and `aggregatedupto` (both in `WindowAggState`). As `currentpos` advances, only newly entered rows are passed to `advance_windowaggregate()`. The transition state is never reset between rows. Each row extends it with one call to the transition function. This is O(N) total work per partition, the best possible.

**Sliding frames with an inverse transition function.** When the frame head can advance (e.g. `ROWS BETWEEN 3 PRECEDING AND CURRENT ROW`), rows eventually leave the frame. If `pg_aggregate.aggminvtransfn` is non-null, the executor calls `advance_windowaggregate_base()` to subtract the departing row's contribution via the inverse transition function stored in `peraggstate->invtransfn_oid`. Built-in aggregates with inverses include `sum`, `avg`, and `count`. This keeps the cost O(N) even for sliding frames.

**Restart (full recomputation).** When the frame head moves and no inverse transition function exists, `peraggstate->restart` is set to `true`. The aggregate is re-initialised from scratch at `frameheadpos`. It is then reaccumulated forward to `frametailpos`. This is O(frame_width × N) — quadratic in the worst case. The same forced-restart path applies whenever `FRAMEOPTION_EXCLUSION` is set, because exclusion makes the apparent frame non-contiguous.

**RANGE with value offsets.** For each row, `update_frameheadpos()` scans forward from the last known frame head. It looks for the first row that satisfies the `in_range` constraint. Because the frame head can only move forward as `currentpos` advances, the total number of `in_range` calls across the whole partition is O(N) amortised, not O(N²). The key insight is that `framehead_slot` and `framehead_ptr` persist across rows.

**`UNBOUNDED FOLLOWING` end.** Spool the entire partition upfront. `begin_partition()` in `nodeWindowAgg.c` detects certain frame configurations (those requiring knowledge of the tail position before spooling). It sets `partition_spooled = true` immediately rather than lazily. This causes the executor to fetch all rows into the `buffer` tuplestore before it produces the first output row. For large partitions that exceed [[subsystems/executor/work-mem-and-spill|work_mem]], the tuplestore spills to disk.

## Related Topics

- [[sql-features/window-functions|Window Functions]] — SQL syntax for the full `OVER` clause, named windows, and function categories; the starting point for understanding how frames fit into the broader window function model
- [[sql-features/window-functions-builtins|Built-in Window Functions]] — reference for ranking, offset, and value functions, including which ones ignore the frame entirely
- [[subsystems/executor/window-functions|Window Functions Executor]] — the WindowAgg node in detail: partition spooling, the tuplestore buffer, reuse of aggregate state, and the pass-through optimisation
- [[subsystems/executor/window-functions-performance|Window Function Performance]] — sort requirements, work_mem sizing, and the practical performance differences between frame modes
- [[sql-features/ordered-set-aggregates|Ordered-Set Aggregates]] — a related aggregate form that operates on a sorted input set but uses a different mechanism from window frames
