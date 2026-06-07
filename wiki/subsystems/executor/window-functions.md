---
title: Window Functions
aliases:
  - WindowAgg
  - nodeWindowAgg
tags:
  - theme/query-optimization
source_files:
  - src/backend/executor/nodeWindowAgg.c
  - src/include/executor/nodeWindowAgg.h
  - src/include/nodes/execnodes.h
  - src/backend/optimizer/plan/planner.c
symbols:
  - WindowAggState
  - WindowStatePerFuncData
  - WindowStatePerAggData
  - WindowObjectData
  - ExecWindowAgg
  - eval_windowaggregates
  - eval_windowfunction
  - begin_partition
  - release_partition
  - spool_tuples
  - are_peers
  - update_frameheadpos
  - update_frametailpos
  - advance_windowaggregate
  - advance_windowaggregate_base
  - select_active_windows
  - create_one_window_path
---

# Window Functions

Window functions compute aggregate-like results over a set of rows related to each current row. Unlike `GROUP BY` aggregates, they never collapse rows. Every input row produces exactly one output row. The window function value is computed from a potentially different subset of rows for each position. This makes window functions the right tool for running totals, rankings, moving averages, and lead/lag comparisons — calculations that need the full context of surrounding rows without losing individual row identity.

The key distinction from grouped aggregates is mechanical, not just conceptual. A `GROUP BY` aggregate collapses N rows into one. A window function leaves N rows intact and attaches a computed value to each. The [[subsystems/executor/aggregate|aggregate]] node eliminates rows as it accumulates them. The `WindowAgg` executor node emits every row it receives, augmented with the function's output.

## The Frame: Defining Which Rows Contribute

Every window function operates over a *frame* — the subset of rows within a partition that contributes to the current row's output. The frame mode (`ROWS`, `RANGE`, `GROUPS`), its boundary syntax, and `EXCLUDE` semantics are SQL-level concepts documented in [[sql-features/window-frame-clauses|Window Frame Clauses]]. What follows is how the executor tracks and evaluates that frame as `currentpos` advances through a partition.

## Planning: Sorting and Stacking WindowAgg Nodes

The planner handles window functions in `planner.c` during the final phase of query planning, after grouping and aggregation have been resolved. It calls `find_window_functions()` to locate all `WindowFunc` nodes in the target list, then `select_active_windows()` to build an ordered list of distinct window specifications that are actually referenced.

The ordering of window specifications is not arbitrary. `select_active_windows()` sorts them so that window clauses sharing a common prefix of PARTITION BY + ORDER BY keys are adjacent. This minimises the number of Sort nodes required: if window A partitions by `(dept)` and window B partitions by `(dept, salary)`, sorting for B first also satisfies A's requirement. This avoids a second sort. The planner uses `pathkeys_count_contained_in()` to check whether an existing sort already satisfies a window's requirements. It inserts an incremental sort when only a prefix is presorted.

The resulting plan is a stack: for each window specification, `create_one_window_path()` appends a `WindowAggPath` (which becomes a `WindowAgg` plan node) on top of a Sort node if one is needed. Multiple window specifications produce multiple stacked `WindowAgg` nodes, each handling the functions that share its exact specification. Intermediate nodes pass their output as input to the next.

```
WindowAgg (partition: dept, order: salary)
  Sort (dept, salary)
    WindowAgg (partition: dept)
      Sort (dept)
        SeqScan
```

## The WindowAgg Executor Node

The `WindowAgg` executor node (`nodeWindowAgg.c`) expects its input already sorted by PARTITION BY keys as the major sort key and ORDER BY keys as the minor sort key. It never re-sorts. That responsibility belongs to the Sort node beneath it.

### Partitions and the Tuple Buffer

Each partition is processed independently. When `begin_partition()` starts a new partition, the node allocates a `tuplestore` buffer that holds the rows of that partition. Rather than pulling all rows upfront, the node spools rows on demand via `spool_tuples()` — fetching from the outer plan and writing into the tuplestore only as far ahead as needed for the current frame computation. When the tuplestore spills to disk, the node falls back to spooling the entire partition at once to avoid expensive alternating reads and writes.

When the PARTITION BY key changes, `release_partition()` tears down the buffer, resets all aggregate states, and frees the partition [[subsystems/memory/contexts|memory context]] (`partcontext`). The first row of the new partition is saved in `first_part_slot` before the old partition is released. This lets it seed `begin_partition()` for the next cycle.

### Per-Row Evaluation

For each output row, `ExecWindowAgg()` advances `currentpos` within the partition. It invalidates the cached `frameheadpos` and `frametailpos`, which must be recomputed for the new position. It reads the current row back out of the tuplestore into `ss_ScanTupleSlot`. Then it dispatches to two separate evaluation paths:

- **True window functions** (functions that implement the `WindowFunc` API, such as `row_number`, `rank`, `lag`) are called via `eval_windowfunction()`. The function receives a `WindowObject` through `fcinfo->context` and uses the `WinGetFuncArgInPartition` / `WinGetFuncArgInFrame` API to fetch rows at arbitrary positions within the partition.
- **Aggregate-backed window functions** (`sum`, `avg`, `count`, and any user-defined aggregate used in a window context) are evaluated via `eval_windowaggregates()`, which manages a transition-value lifecycle per aggregate.

### Frame Position Tracking

The node maintains integer positions `frameheadpos` and `frametailpos` describing the current frame as half-open interval `[frameheadpos, frametailpos)`. These are recomputed lazily on each row via `update_frameheadpos()` and `update_frametailpos()`.

For `UNBOUNDED PRECEDING`, `frameheadpos` is always 0 — a trivial case. For `CURRENT ROW` in RANGE or GROUPS mode, the node must scan forward from the last known frame head to find the first row that is a peer of the current row (using `are_peers()`). This scan reuses a dedicated `framehead_ptr` read pointer into the tuplestore so the position persists across rows. `ROWS` mode with a numeric offset just performs arithmetic on `currentpos`.

Peer equality is evaluated with `ordEqfunction`, an `ExprState` over the ORDER BY columns. `ordEqfunction` returns true when two rows have equal ORDER BY values. It is used by `RANGE CURRENT ROW`, `GROUPS` mode, and the `are_peers()` comparison function. If there are no ORDER BY columns, all rows in the partition are peers.

## Optimised Aggregate Evaluation

Naively recomputing an aggregate from scratch for every row is O(N²) per partition. The node avoids this in two ways.

**Incremental forward accumulation.** When the frame can only grow — the common case of `UNBOUNDED PRECEDING AND CURRENT ROW` — the node tracks `aggregatedupto` (the first row not yet fed to the transition function). As `currentpos` advances, `eval_windowaggregates()` calls `advance_windowaggregate()` for each new row that enters the frame. It calls `finalize_windowaggregate()` whenever it needs the result. The transition state is never reset between rows. Each row just extends it.

**Inverse transition functions.** When the frame head can move forward (e.g. `ROWS BETWEEN 1 PRECEDING AND CURRENT ROW`), rows eventually leave the frame. If the aggregate's catalog entry (`pg_aggregate`) provides an inverse transition function (`invtransfn`), the node calls `advance_windowaggregate_base()` to subtract the departing row rather than restarting from scratch. The inverse function must not return NULL unless it cannot handle the removal. A NULL result forces a restart. The node also never calls the inverse function on the last remaining row. Instead it re-initialises the aggregate state, because the initial state may legitimately be NULL.

**Same-frame caching.** When rows share the same frame (e.g. all rows in a peer group under `RANGE UNBOUNDED PRECEDING AND CURRENT ROW` with no ORDER BY, making the whole partition one peer group), the computed aggregate result is saved in `peraggstate->resultValue` and reused for subsequent rows without re-evaluating. The check `aggregatedbase == frameheadpos && aggregatedupto > currentpos` detects this case cheaply.

If an aggregate lacks an inverse transition function and the frame head moves, the node sets `peraggstate->restart = true` and re-runs the full aggregation from the new frame head — resetting `aggcontext` to reclaim memory first.

## Peer Groups and Ranking Functions

Rows with identical ORDER BY values belong to the same *peer group*. The concept matters in three places:

- **RANGE and GROUPS frame modes** use peer boundaries rather than row counts for frame edges.
- **`RANK()`** returns the position of the first row in the peer group. Ties share a rank. The next rank skips accordingly (1, 1, 3, ...).
- **`DENSE_RANK()`** counts distinct peer groups, so ranks are contiguous (1, 1, 2, ...).
- **`ROW_NUMBER()`** assigns a unique sequential integer regardless of peers. It ignores the frame entirely.

The `WindowAggState` tracks `currentgroup` (a monotone counter of peer groups seen so far), `groupheadpos` (the row position where the current peer group began), and `grouptailpos` (one past the last row of the current peer group). These are updated in `ExecWindowAgg()` whenever `are_peers()` returns false between the previous and current row.

## Key State Structures

`WindowAggState` (in `execnodes.h`) is the top-level execution state:

| Field | Purpose |
|---|---|
| `buffer` | Tuplestore holding current partition's rows |
| `currentpos` | Zero-based position of the row being output |
| `frameheadpos` | Start of current frame (inclusive) |
| `frametailpos` | End of current frame (exclusive) |
| `aggregatedbase` | First row in current transition value |
| `aggregatedupto` | First row not yet accumulated |
| `currentgroup` | Peer group counter within partition |
| `groupheadpos` | Start of current peer group |
| `partcontext` | Memory context reset between partitions |
| `aggcontext` | Shared memory context for aggregate states |
| `status` | `WINDOWAGG_RUN`, `WINDOWAGG_PASSTHROUGH`, or `WINDOWAGG_DONE` |

`WindowStatePerFuncData` (private to `nodeWindowAgg.c`) holds per-function state including the `WindowObject` used as the function's context handle and a flag `plain_agg` indicating whether the function is an aggregate or a true window function.

`WindowStatePerAggData` holds per-aggregate state for aggregate-backed window functions:

| Field | Purpose |
|---|---|
| `transfn_oid` / `invtransfn_oid` | Transition and inverse transition function OIDs |
| `transValue` / `transValueCount` | Running transition state and row count |
| `resultValue` | Cached final value for same-frame reuse |
| `restart` | Flag set when the aggregate must recompute from scratch |
| `aggcontext` | Per-aggregate memory context (may be shared) |

`WindowObjectData` is the handle passed to true window functions via `fcinfo->context`. It exposes `markptr` and `readptr` into the tuplestore along with `seekpos` so functions can navigate to arbitrary rows within the partition without losing their place.

## The pass-through Mode

When a query has multiple `WindowAgg` nodes stacked and an upper node has a `runCondition` (an early-termination predicate, e.g. from `LIMIT` pushdown into a `RANK() < N` filter), lower nodes can enter *pass-through mode* (`WINDOWAGG_PASSTHROUGH`). In this mode the node still reads and forwards rows, so the upper node can see them. It skips the expensive window function evaluation, however, since those results will never be used. The strictest variant, `WINDOWAGG_PASSTHROUGH_STRICT`, also avoids writing rows into the tuplestore when the node is at the top level and nothing downstream needs them.

## Relationship to GROUP BY Aggregates

The `WindowAgg` node deliberately mirrors the structure of the [[subsystems/executor/aggregate|Agg node]]. The transition/finalise lifecycle, the `aggcontext` memory management, and the `initialize_windowaggregate` / `advance_windowaggregate` / `finalize_windowaggregate` functions are parallel to their counterparts in `nodeAgg.c`. The critical divergence is that `nodeAgg.c` discards input rows as it accumulates them and emits one row per group. `nodeWindowAgg.c` retains every row in the tuplestore and emits them all, one per output row. The inverse transition function concept is unique to the window case — grouped aggregates never need to remove rows from a state.

## Related Topics

- [[sql-features/window-functions|Window Functions (SQL Features)]] — SQL-level syntax and semantics for OVER clauses, frame specifications, and partition ordering that feed directly into the executor node described here.
- [[sql-features/window-frame-clauses|Window Frame Clauses]] — frame modes, boundaries, and EXCLUDE semantics at the SQL level, plus the complexity analysis behind ROWS/RANGE/GROUPS performance.
- [[sql-features/window-functions-builtins|Built-in Window Functions]] — catalog of built-in ranking and navigational functions (ROW_NUMBER, RANK, LAG, LEAD, etc.) that are dispatched through the true-window-function path in nodeWindowAgg.c.
- [[subsystems/executor/window-functions-performance|Window Function Performance]] — tuning guidance for WindowAgg execution, including sort avoidance, work_mem sizing, and the impact of inverse transition functions.
- [[subsystems/executor/sort|Sort Node]] — the Sort executor node that WindowAgg always expects beneath it to deliver rows pre-ordered by PARTITION BY and ORDER BY keys.
- [[subsystems/executor/incremental-sort|Incremental Sort]] — incremental sort variant used when input is already partially sorted on a PARTITION BY prefix, avoiding a full re-sort between stacked WindowAgg nodes.
- [[subsystems/executor/tuplestore|Tuplestore]] — the in-memory (disk-spilling) row buffer that WindowAgg uses to spool partition rows and support arbitrary read-pointer navigation for true window functions.
- [[sql-features/advanced-aggregation|Advanced Aggregation]] — GROUPING SETS, ROLLUP, and CUBE, which interact with the planner's window-function pass when both appear in the same query.
- [[subsystems/executor/overview|Executor Overview]] — the general executor node model that WindowAgg fits into.
- [[subsystems/executor/aggregate|Aggregate Node]] — the GROUP BY aggregate node whose transition/finalize lifecycle WindowAgg's aggregate evaluation path mirrors.
- [[subsystems/planner/overview|Planner Overview]] — the general planning pipeline that produces the stacked WindowAgg plan described here.
- [[subsystems/planner/cost-model|Planner Cost Model]] — cost estimation used when deciding sort placement and window path ordering.
