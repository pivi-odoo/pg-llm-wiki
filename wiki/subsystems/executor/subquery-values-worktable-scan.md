---
title: "SubqueryScan, ValuesScan, and WorkTableScan"
aliases:
  - subquery scan
  - values scan
  - worktable scan
  - recursive CTE execution
  - VALUES list executor
tags:
  - theme/query-optimization
source_files:
  - src/backend/executor/nodeSubqueryscan.c
  - src/backend/executor/nodeValuesscan.c
  - src/backend/executor/nodeWorktablescan.c
symbols:
  - SubqueryScanState
  - ValuesScanState
  - WorkTableScanState
  - ExecSubqueryScan
  - ExecValuesScan
  - ExecWorkTableScan
  - ValuesNext
  - WorkTableScanNext
---

Three executor scan nodes cover the cases where the "relation" being scanned is not a physical heap: a sub-SELECT treated as an opaque source, a literal `VALUES (...)` list embedded in the query tree, and the in-flight working table that drives recursive CTE iteration. Each node is a leaf from the parent's perspective. But it may wrap a full sub-tree underneath.

## SubqueryScan as an Optimization Fence

When the [[subsystems/planner/overview|planner]] cannot inline a sub-SELECT into the surrounding query — a process called subquery flattening or pull-up — it wraps the subquery's plan tree in a `SubqueryScan` node. The surrounding plan treats the result as if it were a base relation. It can apply its own qual filters above the node. But it cannot push those filters down into the subquery's plan tree. This is the optimization fence property.

The planner chooses to keep a `SubqueryScan` instead of flattening when the subquery contains certain features: `LIMIT`, `OFFSET`, `DISTINCT`, `GROUP BY` without aggregation, or volatile functions in the target list. Merging these features with the parent query would change semantics. For example, a subquery with `LIMIT 10` cannot be flattened because the parent might supply additional filters. Those filters could reduce the row count before the limit applies, producing the wrong result.

At execution time the node is nearly transparent. `SubqueryNext` calls `ExecProcNode` on the child plan. It returns whatever slot that plan produces, without copying. The executor deliberately skips the copy as an efficiency measure. It uses the node's own `ScanTupleSlot` only when EvalPlanQual needs to recheck a tuple during a concurrent update. The node sets the result and scan slot operators to match the child plan's output slot type. As a result, it adds no conversion overhead on the hot path.

In `EXPLAIN` output, the fence boundary appears as an explicit node:

```
Subquery Scan on subq
  Filter: (subq.val > 10)
  ->  Limit
        ->  Seq Scan on t
```

The `Filter` on `subq.val > 10` applies after the subquery's `Limit` has already reduced the rows. If the subquery had been flattened, the planner could have pushed that filter into the `Seq Scan` as a condition evaluated before the limit. That would have changed semantics. The presence of `Subquery Scan` in `EXPLAIN` is therefore a signal that predicate pushdown stopped at that boundary.

## ValuesScan: Expression Lists as a Relation

The planner stores `VALUES (1, 'a'), (2, 'b'), (3, 'c')` in the plan tree as a list of expression lists — no heap relation, no index, no disk I/O. The `ValuesScan` node materializes one virtual tuple at a time by evaluating each sublist in turn.

The node serves three distinct SQL constructs: the `VALUES` clause of a multi-row `INSERT`, a `VALUES` expression used as a derived table (e.g., `FROM (VALUES ...) AS v`), and the source side of an `IN (VALUES ...)` predicate that the planner converts into a join or hash table. In all three cases the executor representation is the same.

Initialization converts the planner's list-of-lists into a flat array of expression lists (`exprlists`) for O(1) index access at runtime. The executor normally builds expression state (`exprstatelists`) lazily. It initializes each row's expressions in the per-tuple [[subsystems/memory/contexts|memory context]] immediately before evaluation. It then discards them when moving to the next row. This lazy approach avoids accumulating expression state proportional to the total number of rows. That matters for large bulk inserts. The exception is rows whose expressions contain sub-plans. The executor must initialize those once at plan startup, so the sub-plan can register itself in the plan tree and appear correctly in `EXPLAIN`. The executor explicitly disables [[subsystems/executor/jit-llvm|JIT]] compilation for these transient per-row expression states. Each state is used exactly once. As a result, the compilation overhead would outweigh any benefit.

Each call to `ValuesNext` advances a zero-based `curr_idx`. It then evaluates the corresponding expression list into a virtual tuple slot and returns the tuple. The node supports backward scanning by decrementing the index. `ExecReScanValuesScan` simply resets `curr_idx` to -1.

## WorkTableScan and the Recursive CTE Loop

Recursive CTEs rely on a tight pairing between two nodes: `RecursiveUnion` and `WorkTableScan`. `RecursiveUnion` owns a [[subsystems/executor/tuplestore|tuplestore]] called the working table. `WorkTableScan` reads from that tuplestore. The two nodes communicate through an executor parameter slot (`wtParam`) that holds a pointer to the `RecursiveUnionState`. `WorkTableScan` looks up this parameter on its first execution call rather than at initialization. It does this because the `RecursiveUnion` node might not finish initializing before `WorkTableScan` does.

The iteration protocol works as follows. `RecursiveUnion` executes its non-recursive term (the part before `UNION ALL`). It loads all resulting rows into the working table. This seeds the first iteration. It then repeats two steps. First, it lets `WorkTableScan` drain the working table row by row, feeding those rows up to the rest of the query. Then it runs the recursive term against those rows to populate the working table for the next iteration. When the recursive term produces no new rows, the working table is empty at the start of the next cycle. `WorkTableScan` immediately returns nothing, ending the loop.

Only one consumer reads the working table, always in forward order. As a result, `WorkTableScan` does not acquire a private read pointer in the tuplestore or request backward-scan support. The engine configures the tuplestore for forward-only reading as a performance choice. This is safe. A worktable scan node can never appear in a scrollable cursor's plan at a point that would request backward scanning.

`ExecReScanWorkTableScan` calls `tuplestore_rescan` to reset the read position to the beginning of the current working table contents. The `RecursiveUnion` node triggers this when it reseeds the working table for a new iteration.

In `EXPLAIN`, the recursive structure surfaces as:

```
CTE Scan on search_tree
  ->  Recursive Union
        ->  Seq Scan on tree  (non-recursive term)
        ->  Hash Join
              ->  WorkTable Scan on search_tree
              ->  Seq Scan on tree
```

`WorkTable Scan` always appears inside the recursive branch of a `Recursive Union`. If that scan carries a `Filter` line, the executor applies the filter row-by-row while reading rows from the working table. There is no way to push that filter into the working table's population step, because a separate execution of the recursive term populates the working table.

## Related Topics

- [[subsystems/planner/overview|planner]] — subquery flattening / pull-up decisions that determine whether SubqueryScan is elided
- [[subsystems/executor/tuplestore|tuplestore]] — the in-memory/on-disk store that WorkTableScan reads
- [[subsystems/executor/work-mem-and-spill|work_mem]] — controls when the recursive CTE working table spills to disk
- [[subsystems/executor/jit-llvm|JIT]] — deliberately disabled for per-row ValuesScan expression states
- [[subsystems/planner/join-ordering|join ordering]] — affects how IN (VALUES ...) is executed once converted to a join
