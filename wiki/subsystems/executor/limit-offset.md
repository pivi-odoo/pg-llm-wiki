---
title: "LIMIT / OFFSET Executor Node"
aliases:
  - LIMIT node
  - OFFSET node
  - nodeLimit
  - LimitState
  - FETCH FIRST
  - WITH TIES
source_files:
  - src/backend/executor/nodeLimit.c
  - src/include/nodes/execnodes.h
  - src/include/nodes/plannodes.h
  - src/include/nodes/nodes.h
symbols:
  - LimitState
  - LimitStateCond
  - ExecLimit
  - ExecInitLimit
  - ExecEndLimit
  - ExecReScanLimit
  - recompute_limits
  - compute_tuples_needed
  - ExecSetTupleBound
---

The `Limit` executor node enforces the `LIMIT`, `OFFSET`, and `FETCH FIRST` clauses of a query by acting as a filter on the tuple stream produced by its single child plan. It does no sorting, no hashing, and no grouping — it simply counts rows and decides which ones to pass up to its caller. Despite its simplicity, the node carries non-trivial state to support backward scans, rescan after parameter changes, and the SQL standard's `WITH TIES` semantics, all of which require careful bookkeeping without materialising the full result.

## The plan node and expression evaluation

The planner emits a `Limit` plan node (`plannodes.h`) with four fields relevant to execution: `limitOffset`, `limitCount`, `limitOption`, and the tie-breaking columns `uniqColIdx`, `uniqOperators`, and `uniqCollations`. The planner stores the offset and count as `Node *` expression trees, not as integer constants. This is deliberate: both can be parameterised queries (`LIMIT $1`) or even arbitrary expressions, so `ExecInitLimit` compiles them into `ExprState` trees in the usual way.

`ExecInitLimit` does not evaluate the expressions. That would be too early because parameter values from upper nodes may not yet be set. Instead, `recompute_limits` evaluates them on the first call to `ExecLimit` and again on every `ExecReScanLimit`. `recompute_limits` treats a `NULL` offset as zero and a `NULL` count as no limit (`noCount = true`, equivalent to `LIMIT ALL`). It rejects negative values with an error. After evaluation, `recompute_limits` calls `ExecSetTupleBound(compute_tuples_needed(node), outerPlanState(node))`, which propagates the bound down to the child node. If the child is a `Sort`, the bound enables a *bounded sort* that avoids materialising more than `count + offset` rows — a significant optimisation for `ORDER BY … LIMIT n` queries.

`compute_tuples_needed` returns `count + offset`, or a negative value (meaning "unlimited") when `noCount` is true or when `WITH TIES` is active. `WITH TIES` disables the bound because the node cannot know in advance how many extra rows at the tied boundary it will need to read before finding one that breaks the tie.

## The state machine

`LimitState` drives a state machine stored in the `lstate` field. The states and their transitions reflect the node's position relative to the output window `[offset, offset + count)`:

| State | Meaning |
|---|---|
| `LIMIT_INITIAL` | Just initialised; limit expressions not yet evaluated |
| `LIMIT_RESCAN` | Expressions evaluated, positioned before first row |
| `LIMIT_EMPTY` | Window is empty (count = 0, or subplan returned too few rows) |
| `LIMIT_INWINDOW` | Within the window; `subSlot` holds the last returned tuple |
| `LIMIT_WINDOWEND_TIES` | Past the nominal count; checking for `WITH TIES` continuations |
| `LIMIT_SUBPLANEOF` | Subplan exhausted before the window ended |
| `LIMIT_WINDOWEND` | Stepped off the end of the window |
| `LIMIT_WINDOWSTART` | Stepped off the start of the window (during backward scan) |

On the first call (`LIMIT_INITIAL`), `recompute_limits` runs. Execution then falls through to `LIMIT_RESCAN`. From `LIMIT_RESCAN`, the node pulls rows from its child in a tight loop, incrementing `position` until `position > offset`. The node discards each row. Only the row at `position == offset + 1` escapes the loop and transitions the state to `LIMIT_INWINDOW`. If the subplan is exhausted before that, the state becomes `LIMIT_EMPTY` and the node returns `NULL`.

Once in `LIMIT_INWINDOW`, each forward call fetches the next child row and increments `position`. When `position - offset >= count` (and `noCount` is false), forward progress ends. With `LIMIT_OPTION_COUNT` (plain `LIMIT n`), the state becomes `LIMIT_WINDOWEND` and the node returns `NULL` immediately. With `LIMIT_OPTION_WITH_TIES`, the state becomes `LIMIT_WINDOWEND_TIES` and tie checking begins. When the subplan reports end-of-stream before the window is full, the state becomes `LIMIT_SUBPLANEOF`.

`LIMIT_WINDOWEND`, `LIMIT_SUBPLANEOF`, and `LIMIT_WINDOWSTART` act as one-step boundary markers. They return `NULL` on continued forward (or backward, for `LIMIT_WINDOWSTART`) calls, and re-enter `LIMIT_INWINDOW` when the scan reverses direction. This design means the node never needs to re-fetch a tuple just because the caller reached a boundary and then changed direction.

## Large OFFSET is not free

Because the `Limit` node is a filter on a Volcano pull stream, skipping the first `offset` rows means actually fetching and discarding them from the child plan. There is no mechanism to seek into a sorted result at an arbitrary offset. The child plan produces rows one at a time. The `Limit` node counts them. A query like `SELECT … ORDER BY id OFFSET 100000 LIMIT 10` must have its child sort node produce 100,010 rows before the `Limit` node returns its first output row. This cost is linear in the offset value. It is especially visible when the child is a `Sort` that must materialise the entire input.

Keyset pagination (`WHERE id > $last_seen_id ORDER BY id LIMIT 10`) avoids this cost by pushing the skip condition into the scan predicate rather than relying on `OFFSET`. The `Limit` node then sees only the rows that should be returned. The offset is zero.

## WITH TIES and tie-breaking

`FETCH FIRST n ROWS WITH TIES` is the SQL-standard spelling of "return `n` rows, but if the `n`th row is tied on the `ORDER BY` keys with rows that follow it, include those too." The planner represents this as `LIMIT_OPTION_WITH_TIES` in the `Limit` node, and it embeds the `ORDER BY` key columns and their equality operators in `uniqColIdx`, `uniqOperators`, and `uniqCollations`.

`ExecInitLimit` compiles these into an equality function (`eqfunction`) using `execTuplesMatchPrepare`, and allocates an extra tuple slot `last_slot`. Whenever the node is in `LIMIT_INWINDOW` and `position - offset == count - 1` (the last nominal in-window tuple), it copies the current tuple into `last_slot` via `ExecCopySlot`. Once the window boundary is crossed and the state transitions to `LIMIT_WINDOWEND_TIES`, each subsequent call to `ExecLimit` pulls one more row from the child and runs the equality test (`ExecQualAndReset(node->eqfunction, econtext)`). If the new row matches `last_slot` on the `ORDER BY` columns, `ExecLimit` returns it and increments `position`. If the test fails, the state moves to `LIMIT_WINDOWEND` and `ExecLimit` returns `NULL`.

The consequence is that `WITH TIES` makes the result set size unpredictable at planning time. This is why `compute_tuples_needed` returns a negative bound. The child Sort therefore cannot use bounded sort in this case.

## Interaction with cursors and backward scans

The [[subsystems/executor/overview|executor]] propagates backward-scan capability through the `EXEC_FLAG_BACKWARD` flag set by [[code-paths/cursor|cursors]] declared with `SCROLL`. The `Limit` node does not itself check this flag during initialisation. It delegates backward scan support entirely to its child, consistent with `ExecSupportsBackwardScan` in `execAmi.c`, which recurses through `Limit` nodes transparently.

`ExecInitLimit` does assert that `EXEC_FLAG_MARK` is not set. The node does not support the mark/restore interface, because it never materialises its input and has no way to snapshot its own position independently of the child's position.

When `ExecutorRun` drives the plan in the backward direction (`es_direction == BackwardScanDirection`), the `Limit` node's state machine handles it through the `LIMIT_WINDOWSTART` and `LIMIT_SUBPLANEOF` boundary states. Stepping backward out of the window returns `NULL` and moves to `LIMIT_WINDOWSTART`. The next forward call re-returns `subSlot` (the last tuple seen before the boundary) without re-fetching from the child, then steps back into `LIMIT_INWINDOW`. Backing up from `LIMIT_SUBPLANEOF` or `LIMIT_WINDOWEND` re-fetches one row from the child in backward mode — the child must support this. This is why cursor creation checks `ExecSupportsBackwardScan` at that time.

`LIMIT_RESCAN`: both on first execution and on re-execution after parameter changes, the scan starts in this state after `recompute_limits` runs. If the scan direction is backward at rescan time, the node immediately returns `NULL` without touching the child. This matches the convention that backward scans start from EOF and work toward the beginning. A caller that wants to reposition must first drive the plan forward.

## Projection and memory

`Limit` performs no projection and allocates no tuple slots of its own beyond `last_slot` (for `WITH TIES`). It returns `subSlot` — a pointer to the child node's current output slot — directly to its caller. This is safe because the node is called in the Volcano pull model. The caller processes the returned slot before calling `ExecLimit` again. At that point, the child will have advanced, and `subSlot` may change.

`recompute_limits` evaluates the limit and offset expressions in the per-node expression context (`ps_ExprContext`), which lives in the [[subsystems/memory/contexts|memory context]] created during `ExecAssignExprContext`. Since `recompute_limits` evaluates the expressions only once per scan, there is no per-tuple allocation concern here.

## Related Topics

- [[subsystems/executor/overview|Executor overview]] — Volcano pull model, PlanState tree, and EXEC_FLAG constants
- [[subsystems/executor/sort|Sort node]] — bounded sort optimisation enabled by ExecSetTupleBound
- [[code-paths/cursor|Cursors and portals]] — how SCROLL cursors propagate EXEC_FLAG_BACKWARD and interact with backward scan
- [[sql-features/window-functions|Window functions]] — a separate mechanism for row ranking within a partition; often confused with FETCH FIRST
- [[subsystems/memory/contexts|Memory contexts]] — per-node expression context lifecycle
