---
title: "Append Executor Node"
aliases:
  - Append Node
  - nodeAppend
tags:
  - theme/parallelism
source_files:
  - src/backend/executor/nodeAppend.c
symbols:
  - AppendState
  - ParallelAppendState
  - ExecInitAppend
  - ExecAppend
  - ExecEndAppend
  - ExecReScanAppend
  - choose_next_subplan_locally
  - choose_next_subplan_for_leader
  - choose_next_subplan_for_worker
  - ExecAsyncAppendResponse
  - classify_matching_subplans
---

The Append node fans out execution across a list of sub-plans and returns the union of their output tuples, one at a time, without sorting or deduplication. It is the execution backbone for inheritance-tree scans, partition-wise queries, and SQL UNION ALL. Because each child plan may target a different heap or use a different scan strategy, the Append node does not own a result slot of its own. It passes the child's slot through unchanged instead, avoiding any per-row copy.

## Subplan organisation

Join nodes use `lefttree` and `righttree` to hold exactly two children. An Append node, unlike those, holds an arbitrary-length list of sub-plans stored in a flat `PlanState **appendplans` array (allocated in `ExecInitAppend()`, `nodeAppend.c`). The planner's immutable `Append` plan node carries the list in `appendplans`. The executor mirrors it in `AppendState.appendplans` after calling `ExecInitNode()` on each valid entry.

The node tracks which sub-plan is currently being scanned in `as_whichplan`. When a child exhausts its tuples, `choose_next_subplan` advances `as_whichplan` to the next valid index. The node returns an empty slot only when all children are exhausted. Because the node passes child slots through directly, it sets `resultopsset = true` and `resultopsfixed = false` to tell the executor framework that its result slot type varies at runtime (`ExecInitAppend()`, `nodeAppend.c`).

## Run-time partition pruning

When the Append node was created to scan a partitioned table and `part_prune_info` is non-NULL, `ExecInitAppend()` sets up a `PartitionPruneState` via `ExecInitPartitionPruning()`. Pruning happens at two points:

- **Init-time pruning** — happens immediately inside `ExecInitPartitionPruning()`, using only constant expressions. The resulting `validsubplans` bitmapset determines which children are even initialized. Pruned partitions never get an `ExecInitNode()` call.
- **Exec-time pruning** — happens on the first call to `choose_next_subplan` (or on `ExecReScan` when `PARAM_EXEC` parameters change). `ExecFindMatchingSubPlans()` re-evaluates the pruning predicates against current parameter values and updates `as_valid_subplans`. When no exec-time pruning is needed (`!prunestate->do_exec_prune`), `as_valid_subplans` is set to all plans during initialization to avoid the later call entirely.

On rescan, if any `PARAM_EXEC` parameter used in pruning expressions has changed (`bms_overlap(chgParam, execparamids)`), `ExecReScanAppend()` resets `as_valid_subplans_identified` to false. This forces the next execution to re-evaluate the pruning conditions from scratch (`nodeAppend.c`).

## Subplan selection strategies

The node uses a function pointer `choose_next_subplan` to select the next child. Three implementations exist, chosen at initialization or when the node is attached to a parallel context:

**`choose_next_subplan_locally`** — used for single-process execution. It iterates through `as_valid_subplans` using bitmapset iteration in either forward or backward direction depending on `EState.es_direction`. Backward scan support is a notable property: by calling `bms_prev_member()` instead of `bms_next_member()`, the node correctly supports cursor movement in reverse. Async-capable plans require that the valid subplans bitmapset be known before any iteration begins. So the node forces `as_valid_subplans_identified` true before it starts async subplans.

**`choose_next_subplan_for_leader`** — used when the node is parallel-aware and the calling process is the query leader. To maximize the value of worker processes, the leader starts from the _last_ (cheapest) subplan and works backward. The planner sorts subplans in descending cost order. So the leader naturally handles cheap plans, while leaving expensive ones for workers. The leader immediately marks non-partial sub-plans (those before `first_partial_plan`) as finished in shared state after selection, preventing any worker from duplicating that scan (`choose_next_subplan_for_leader()`, `nodeAppend.c`).

**`choose_next_subplan_for_worker`** — used by parallel workers. Workers start from the lowest-index valid subplan and advance forward through `pa_next_plan`. When all non-partial plans are done, workers loop back to the first partial plan. By design, multiple workers can execute a partial plan simultaneously — each worker runs the same partial plan concurrently, with the underlying scan node (e.g., a parallel sequential scan) providing work division internally. Non-partial plans, conversely, are assigned exclusively. The first process to claim one immediately marks `pa_finished[i] = true`, so no other worker picks it up.

## Parallel coordination via shared memory

Parallel-aware execution requires a `ParallelAppendState` in DSM, set up by `ExecAppendInitializeDSM()`. The structure is minimal:

| Field | Purpose |
|---|---|
| `pa_lock` | [[subsystems/locking/lwlocks|LWLock]] protecting `pa_next_plan` and `pa_finished[]` |
| `pa_next_plan` | Index of the next sub-plan for workers to attempt; `INVALID_SUBPLAN_INDEX` when exhausted |
| `pa_finished[]` | Per-subplan boolean; true means no further worker should claim this plan |

Every `choose_next_subplan_for_leader` and `choose_next_subplan_for_worker` call acquires the lock exclusively. Because sub-plan selection is infrequent compared to per-tuple processing, this coarse locking does not become a bottleneck in practice.

When run-time pruning is active in a parallel context, `mark_invalid_subplans_as_finished()` pre-populates `pa_finished` for all pruned partitions. So the worker selection loop never needs to check the `as_valid_subplans` bitmapset. All invalid entries appear finished before the first worker call.

## Asynchronous subplan execution

From PostgreSQL 14, the planner may mark individual sub-plans `async_capable` on the `Plan` node, for FDW scans that support non-blocking I/O, such as `postgres_fdw`. When async sub-plans are present, `ExecInitAppend()` builds two bitmapsets: `as_asyncplans` (all async-capable children) and, after partition pruning, `as_valid_asyncplans` (the subset actually needed). `classify_matching_subplans()` splits the valid sub-plans bitmapset so that `as_valid_subplans` contains only synchronous children.

The main `ExecAppend()` loop distinguishes two interleaved streams:

- **Sync stream** — the loop drives the current `as_whichplan` child with `ExecProcNode()`, exactly as in the non-async case.
- **Async stream** — the loop sends outstanding requests to all async children via `ExecAsyncRequest()`. Results arrive through `ExecAsyncAppendResponse()`. This function stores completed slots in the `as_asyncresults` buffer and records that the corresponding child needs a new request.

When the sync stream is exhausted (`as_syncdone = true`) but async children are still outstanding, `ExecAppendAsyncEventWait()` waits on a `WaitEventSet`. Each async child has populated this set with its socket descriptor. The timeout is `-1` (indefinite block) only when all sync work is done. Otherwise it is `0` (non-blocking poll), so the node can interleave progress on the sync stream with draining async results without stalling. This design lets a query mix a local heap scan with one or more remote `postgres_fdw` scans and keep both in flight simultaneously.

The node disables async mode when executing under `EvalPlanQual` (`estate->es_epq_active != NULL`). The re-evaluation path cannot tolerate non-blocking I/O semantics.

## Key state fields

| Field in `AppendState` | Meaning |
|---|---|
| `as_whichplan` | Index into `appendplans` of the current sync child; `INVALID_SUBPLAN_INDEX` (-1) initially |
| `as_nplans` | Total number of initialized sub-plans |
| `as_first_partial_plan` | Index of the first partial plan (workers may share it); equals `as_nplans` when none exist |
| `as_valid_subplans` | Bitmapset of sync children surviving pruning |
| `as_valid_subplans_identified` | Whether `as_valid_subplans` has been computed for this scan |
| `as_prune_state` | `PartitionPruneState *`; NULL when pruning is not in use |
| `as_syncdone` | True when all sync children are exhausted; async children may still be running |
| `as_begun` | Latches first-call initialization within `ExecAppend()` |
| `as_asyncplans` | Bitmapset of all async-capable children |
| `as_nasyncremain` | Number of async children not yet exhausted |
| `as_needrequest` | Bitmapset of async children ready for their next request |
| `as_pstate` | Pointer to `ParallelAppendState` in DSM; NULL when not parallel-aware |
| `choose_next_subplan` | Function pointer selecting the next sync child |

## Interaction with the Volcano model

The Append node is a pure [[subsystems/executor/overview|executor]] pull node with no internal buffer. It calls `ExecProcNode()` on exactly one synchronous child per outer call. If that child returns a non-null slot, Append returns it immediately. If the child is exhausted, Append advances and tries the next. This means the caller above the Append node sees no difference between a single-child Append (a no-op wrapper that the planner occasionally generates) and a 64-partition Append scanning a large table.

The node does not support mark/restore cursor positioning (`EXEC_FLAG_MARK`). `ExecInitAppend()` asserts against it. The node supports backward scan only in the non-parallel, non-async path.

## Related Topics

- [[subsystems/executor/overview|Executor Overview]] — Volcano model, EState, PlanState, and the executor node lifecycle
- [[subsystems/executor/parallel|Parallel Query Framework]] — DSM setup, worker launch, and how parallel-aware nodes communicate
