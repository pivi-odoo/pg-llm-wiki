---
title: "Materialize Executor Node"
aliases:
  - Materialize node
  - nodeMaterial
  - ExecMaterial
  - MaterialState
source_files:
  - src/backend/executor/nodeMaterial.c
  - src/include/nodes/execnodes.h
symbols:
  - MaterialState
  - ExecMaterial
  - ExecInitMaterial
  - ExecEndMaterial
  - ExecReScanMaterial
  - ExecMaterialMarkPos
  - ExecMaterialRestrPos
  - make_material
  - materialize_finished_plan
---

The Materialize node buffers its child's entire output in a [[subsystems/executor/tuplestore|tuplestore]] so that the subtree can be rewound and re-scanned without re-executing. Sort and HashAgg are blocking operators; they consume all input before emitting a single row. Materialize, by contrast, is a transparent pass-through on the first scan: it forwards each tuple to its parent as soon as it arrives, storing a copy in the tuplestore at the same time. The payoff comes on the second and subsequent scans, which read cheaply from the buffer rather than re-driving the child plan.

## Planner insertion points

The [[subsystems/executor/overview|executor]] uses a pull (Volcano) model, in which a node's parent may ask it to rescan from the beginning. Not all plan nodes support this efficiently or at all. The planner therefore inserts a Materialize node whenever a subtree will be driven more than once and the subtree itself cannot cheaply restart.

The most common case is the **inner side of a NestLoop join**. A NestLoop iterates over its outer input and, for each outer row, drives the inner side from the start. If the inner plan is a SeqScan or some other node that would re-execute the full scan on each restart, wrapping it in Materialize lets the second and later iterations replay from the tuplestore at negligible cost. The planner evaluates this trade-off explicitly in `joinpath.c`: it creates a `MaterialPath` over the cheapest inner path only when `enable_material` is on and the inner path does not already materialize its output (a test performed by `ExecMaterializesOutput`, which returns true for `T_Material`, `T_Sort`, `T_CteScan`, `T_FunctionScan`, `T_WorkTableScan`, and `T_NamedTuplestoreScan`).

A second insertion point is the **inner side of a MergeJoin** when the inner input cannot support mark/restore. MergeJoin needs to back up the inner scan to a previously marked position whenever the outer scan advances past a run of equal keys. When the inner plan node cannot do this natively, the planner sets `materialize_inner` on the join path. It wraps the inner plan in a Materialize node, which provides mark/restore via `ExecMaterialMarkPos` and `ExecMaterialRestrPos`.

A third case arises with **TABLESAMPLE scans** using a method that is not repeatable across scans. Because re-executing the scan would return different rows, the planner wraps such paths in a MaterialPath to freeze the sample result.

The planner also calls `materialize_finished_plan()` in a few late-stage situations — after `create_plan()` has already run — to insert a Materialize node without a corresponding `MaterialPath`. This happens when subplan structure requires shielding a finished plan from rescan requirements that were not anticipated during path selection.

## Streaming on the first pass, replaying on subsequent passes

The key design property is that Materialize does not delay the first row. On each call to `ExecMaterial`, the node checks whether the [[subsystems/executor/tuplestore|tuplestore]] has reached its read end. If it has and the underlying child has not yet returned NULL, the node calls `ExecProcNode` on the child, copies the tuple into the tuplestore, and immediately returns that tuple to the caller. The tuplestore's read pointer automatically advances past each newly appended tuple. The node therefore stays at EOF, as long as new tuples are arriving. This means the parent sees tuples as fast as the child produces them. There is no blocking accumulation phase.

Once the child signals EOF (by returning NULL), the node sets `eof_underlying` to true. From that point on, the node satisfies all reads from the tuplestore. On a rescan, `ExecReScanMaterial` calls `tuplestore_rescan` to rewind the read pointer to the beginning. The next fetch then replays from the stored data without touching the child plan at all, provided the child's parameters have not changed. If `chgParam` is set on the child (meaning a parameter used by the child has changed), the node discards the tuplestore and re-executes the child from scratch.

## The eflags filter and deferred allocation

The tuplestore is not always necessary. If the parent signals through `eflags` that it will never rewind, scan backward, or mark/restore, there is no point accumulating tuples. `ExecInitMaterial` therefore masks `eflags` to retain only `EXEC_FLAG_REWIND`, `EXEC_FLAG_BACKWARD`, and `EXEC_FLAG_MARK`, storing the result in `MaterialState.eflags`. When all three are absent, `eflags` is zero. The node then never creates the tuplestore. `ExecMaterial` simply passes tuples straight through from the child.

When `eflags` is non-zero, the node still does not create the tuplestore immediately. It defers allocation to the first call to `ExecMaterial`, because the node might be initialised speculatively (for example, under `EXEC_FLAG_EXPLAIN_ONLY`) but never actually driven. The lazy allocation also means that a Materialize node on the inner side of a NestLoop that matches no outer rows never allocates the tuplestore at all.

`EXEC_FLAG_BACKWARD` requires special handling: tuplestore does not interpret backward scanning as "reverse all the way to the start" the same way the general executor does. When backward is requested, `ExecInitMaterial` adds `EXEC_FLAG_REWIND` to the tuplestore flags in addition to `EXEC_FLAG_BACKWARD`, preventing `tuplestore_trim` from discarding tuples behind the current read position prematurely.

`ExecInitMaterial` always initialises the child plan with these three flags stripped from its own `eflags`. Materialize absorbs the rescan requirement, so that the child never needs to know about it.

## Memory and spill to disk

The tuplestore created by Materialize uses `work_mem` as its memory budget, passed to `tuplestore_begin_heap` as kilobytes. The tuplestore stores tuples in memory, as long as available memory remains. When the budget is exceeded, the tuplestore spills all accumulated tuples to a `BufFile` temporary file and continues writing new arrivals directly to disk. See [[subsystems/executor/tuplestore|tuplestore]] for the internal state machine (`TSS_INMEM` → `TSS_WRITEFILE` → `TSS_READFILE`) and [[subsystems/executor/work-mem-and-spill|work_mem and spill]] for the broader policy.

Because Materialize does not sort or aggregate, it contributes one `work_mem` grant per node instance. A plan with several NestLoop joins, each with a materialized inner side, can hold multiple tuplestores open simultaneously, each drawing from the same `work_mem` budget independently. The spill is transparent to the parent node and to the application. A materialized inner side that spills to disk can become a significant cost center, though, when the NestLoop executes many outer iterations.

`EXPLAIN ANALYZE` does not report spill information for Materialize nodes directly, but [[subsystems/storage/temp-files|temporary file]] activity is visible in [[subsystems/observability/pg-stat-statements|pg_stat_statements]] via `temp_blks_written` and in server logs via `log_temp_files`.

## Mark and restore

When the parent requests `EXEC_FLAG_MARK`, Materialize allocates a second read pointer in the tuplestore (index 1) alongside the normal active pointer (index 0). `ExecMaterialMarkPos` copies pointer 0 to pointer 1, recording the current scan position. `ExecMaterialRestrPos` copies pointer 1 back to pointer 0, restoring the saved position. After marking, Materialize calls `tuplestore_trim` to release any portion of the buffer that no active pointer needs to revisit, bounding memory use for streaming uses.

This mechanism is what allows MergeJoin to back up its inner scan. From MergeJoin's perspective, the inner node simply responds to mark/restore calls. The fact that a Materialize node is providing that capability is invisible.

## Not the same as CTE materialization

The Materialize node described here is entirely distinct from the materialization of [[sql-features/ctes|CTEs]]. When a CTE is materialized (either because it is referenced more than once, contains volatile functions, or is explicitly marked `MATERIALIZED`), the planner represents it using `CteScan` and `WorkTableScan` plan nodes. These plan nodes are backed by a tuplestore shared among all scan nodes that reference that CTE. The `RecursiveUnion` or the portal-level `InitPlan` infrastructure owns the tuplestore, not a Materialize node.

The Materialize node knows nothing about CTEs and is never inserted to implement CTE semantics. Conversely, `CteScan` nodes report `ExecMaterializesOutput` as true, precisely because they read from an already-populated tuplestore. A NestLoop with a `CteScan` on the inner side will therefore not additionally wrap it in a Materialize node.

## Backward scan

When `EXEC_FLAG_BACKWARD` is included in `eflags`, Materialize supports scanning in reverse order. The tuplestore must have been created with rewind support for this to work correctly (which `ExecInitMaterial` ensures). When `ScanDirection` in the executor's `EState` is set to backward and the tuplestore read pointer is at EOF, Materialize calls `tuplestore_advance(backward)` once, to step past the last-added tuple before beginning the reverse traversal. This avoids returning the final tuple twice, when the direction switches.

This capability supports cursors declared `WITH SCROLL`, which must support backward fetches.

## Related Topics

- [[subsystems/executor/overview|executor]] — the Volcano model and how nodes are initialised and driven
- [[subsystems/executor/tuplestore|tuplestore]] — the buffer used by Materialize and how memory and spill are managed
- [[sql-features/ctes|CTEs]] — CTE materialization, which uses CteScan/WorkTableScan rather than the Materialize node
- [[subsystems/executor/work-mem-and-spill|work_mem and spill]] — the memory budget policy shared by all spilling nodes
- [[subsystems/storage/temp-files|temporary files]] — how BufFile manages on-disk spill storage
- [[subsystems/memory/contexts|memory context]] — the allocation context in which the tuplestore and its tuples live
