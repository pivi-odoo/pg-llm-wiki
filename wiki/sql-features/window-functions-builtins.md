---
title: "Built-in Window Functions"
aliases:
  - row_number
  - rank
  - dense_rank
  - percent_rank
  - cume_dist
  - ntile
  - lead
  - lag
  - first_value
  - last_value
  - nth_value
source_files:
  - src/backend/utils/adt/windowfuncs.c
  - src/include/windowapi.h
symbols:
  - window_row_number
  - window_rank
  - window_dense_rank
  - window_percent_rank
  - window_cume_dist
  - window_ntile
  - window_lead
  - window_lag
  - window_first_value
  - window_last_value
  - window_nth_value
  - WinGetFuncArgInPartition
  - WinGetFuncArgInFrame
  - WinGetPartitionLocalMemory
  - WinRowsArePeers
  - rank_context
  - ntile_context
---

PostgreSQL's built-in window functions — `row_number`, `rank`, `dense_rank`, `percent_rank`, `cume_dist`, `ntile`, `lead`, `lag`, `first_value`, `last_value`, and `nth_value` — are implemented in `src/backend/utils/adt/windowfuncs.c` using the `WindowObject` API defined in `windowapi.h`. Each function accesses its surrounding partition or frame through a small set of callbacks rather than directly manipulating the executor's tuple storage, which keeps the implementations concise and insulated from execution details.

## The WindowObject API

Window functions receive a `WindowObject` handle instead of normal argument values. Window functions retrieve this handle with `PG_WINDOW_OBJECT()` from their call context. The handle serves as the entry point to all position and value queries. The API in `windowapi.h` exposes:

- `WinGetCurrentPosition` — zero-based position of the current row within its partition.
- `WinGetPartitionRowCount` — total rows in the partition, available immediately because the executor materialises the full partition before invoking functions.
- `WinRowsArePeers` — tests whether two rows are peers under the `ORDER BY` keys of the window specification.
- `WinGetPartitionLocalMemory` — allocates a per-partition scratch buffer. The buffer persists across all row invocations within a single partition. The executor automatically resets the buffer at partition boundaries.
- `WinSetMarkPosition` — tells the executor that rows before the given position can be freed from the tuplestore. Window functions call this to release rows they will never need again.
- `WinGetFuncArgInFrame` — evaluates an argument expression at a position relative to the current frame boundary (`WINDOW_SEEK_HEAD` or `WINDOW_SEEK_TAIL`) or at an offset from the frame head.
- `WinGetFuncArgInPartition` — same, but relative to the current row's position within the partition rather than the frame (`WINDOW_SEEK_CURRENT`). Used by `lead` and `lag`.
- `WinGetFuncArgCurrent` — evaluates an argument at the current row; used to read scalar arguments such as the offset in `ntile` or `nth_value`.

## Ranking Functions: Peer Detection and Shared State

`row_number`, `rank`, `dense_rank`, `percent_rank`, and `cume_dist` all rely on a shared helper `rank_up`, which uses `WinRowsArePeers` to determine whether the current row belongs to the same peer group as its predecessor.

Each ranking function allocates `rank_context` — a two-field struct holding only the current rank — once per partition via `WinGetPartitionLocalMemory`. The zero-initialised `rank` field serves as a sentinel: the first call sets it to 1. Subsequent calls detect peer-group transitions and update the rank accordingly.

**`row_number`** ignores peer groups entirely. It simply reads `WinGetCurrentPosition` and returns position + 1. No state is needed beyond the mark advance.

**`rank`** assigns the rank of the first row in a peer group to every member. When `rank_up` signals a new group, it sets the rank to the current position (+ 1). This correctly reflects the number of preceding rows. Tied rows hold the previous rank; the gap in numbers corresponds to the size of the tied group.

**`dense_rank`** also uses `rank_up` but increments a counter rather than jumping to the current position. Each new peer group adds exactly 1 to the rank, regardless of how many rows were in the previous group. The difference from `rank` is a single line: `context->rank++` versus `context->rank = WinGetCurrentPosition(winobj) + 1`.

**`percent_rank`** implements the formula `(rank - 1) / (totalrows - 1)`. It reuses the rank machinery from `rank_up` and fetches `WinGetPartitionRowCount` on every call. When the partition contains exactly one row the function returns 0.0 per the SQL standard, avoiding division by zero.

**`cume_dist`** implements `NP / NR` where `NP` is the count of rows that are peers with or precede the current row. It walks forward from the current position using `WinRowsArePeers` to determine `NP`, stopping at the first row that is not a peer. It caches the result for subsequent rows in the same peer group. This forward scan is the only ranking function that reads beyond the current position.

## ntile: Bucket Arithmetic on a Known Count

`ntile(n)` divides the partition into `n` approximately equal buckets and returns the bucket number (1-indexed) for each row. Because `WinGetPartitionRowCount` is available before processing the first row, `ntile` can compute all bucket sizes upfront.

The `ntile_context` struct stores the current bucket number, the count of rows placed in the current bucket, the bucket capacity (`boundary`), and a `remainder` counter for distributing leftover rows. When total rows are not evenly divisible by `n`, the first `remainder` buckets receive one extra row each. The implementation decrements `boundary` as soon as it fills those larger leading buckets. This makes subsequent buckets smaller by exactly 1.

A null argument returns NULL. A non-positive argument raises `ERRCODE_INVALID_ARGUMENT_FOR_NTILE`. Both behaviours follow the SQL standard.

## lead and lag: Partition-Relative Offsets

`lead` and `lag` are six separate C entry points (`window_lead`, `window_lead_with_offset`, `window_lead_with_offset_and_default`, and their `lag` counterparts) that all converge on `leadlag_common`. The `forward` parameter controls sign. The `withoffset` parameter controls whether the second argument is consumed. The `withdefault` parameter controls whether the third argument is used as a fallback.

The core call is `WinGetFuncArgInPartition` with `WINDOW_SEEK_CURRENT`, which evaluates the first argument expression at a position `±offset` rows from the current row within the partition. The frame specification is completely irrelevant to these functions — they always navigate by physical row offset.

When the target row falls outside the partition boundary, the API sets the `isout` flag. If a default value was provided (`withdefault`), the function fetches it with `WinGetFuncArgCurrent` for argument index 2; otherwise the result is NULL.

The `const_offset` flag, obtained from `get_fn_expr_arg_stable`, allows the executor to cache the evaluated tuple when the offset is a stable expression rather than re-evaluating it on every row. This matters when the offset expression is cheap but the target argument is expensive.

## Frame-Aware Value Functions: first_value, last_value, nth_value

These three functions use `WinGetFuncArgInFrame`, which honours the current frame boundaries as defined by the `OVER` clause.

**`first_value`** passes `WINDOW_SEEK_HEAD` with offset 0, which resolves to the very first row of the current frame.

**`last_value`** passes `WINDOW_SEEK_TAIL` with offset 0, which resolves to the last row of the current frame. Because the default frame is `RANGE BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW`, `last_value` most often returns the current row's own value — a frequent source of confusion documented in [[sql-features/window-functions|window functions]].

**`nth_value`** uses `WINDOW_SEEK_HEAD` with offset `nth - 1` (converting from 1-indexed SQL to 0-indexed C). Like `first_value`, it is frame-sensitive: the frame must contain at least `nth` rows or the function returns NULL. A non-positive `nth` raises `ERRCODE_INVALID_ARGUMENT_FOR_NTH_VALUE`.

The `set_mark` argument to `WinGetFuncArgInFrame` is `true` for `first_value` and `nth_value` (which seek from the head) and for `last_value` (tail). This instructs the executor that the tuplestore can reclaim rows before the accessed position when possible.

## Support Functions and Planner Optimisation

Every built-in window function has a corresponding `_support` function registered in `pg_proc`. These support functions handle two request types:

**`SupportRequestWFuncMonotonic`** — declares whether the function's output is monotonically non-decreasing within a partition. All ranking functions and `ntile` declare `MONOTONICFUNC_INCREASING`. The planner uses this information to optimise queries with `LIMIT` or predicates on the window function's result: once the window function's result exceeds the monotone threshold, the planner can stop processing the partition early.

**`SupportRequestOptimizeWindowClause`** — allows a function to rewrite its own frame specification before execution. Every ranking function rewrites its frame to `ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW`, overriding any user-supplied frame. Crucially, all these functions switch from `RANGE` to `ROWS` mode even though they ignore the frame entirely. This eliminates the peer-row checks that `RANGE` mode requires during frame management in the executor, saving comparison work for every row in large partitions.

The value functions (`first_value`, `last_value`, `nth_value`, `lead`, `lag`) do not have support functions because their results are not monotone and their frame handling is intentional.

## Related Topics

- [[sql-features/window-functions|Window Functions]] — SQL syntax, frame specification, and user-facing behaviour
- [[subsystems/executor/window-functions|WindowAgg Executor Node]] — how the executor materialises partitions, manages the tuplestore, and calls into these functions
- [[subsystems/executor/window-functions-performance|Window Function Performance]] — sort requirements, frame mode cost differences, and indexing strategies
