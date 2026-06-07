---
title: Common Table Expressions (CTEs)
aliases:
  - WITH queries
  - CTE inlining
  - recursive CTEs
tags:
  - theme/query-optimization
source_files:
  - src/backend/optimizer/plan/planner.c
  - src/backend/optimizer/plan/subselect.c
  - src/backend/optimizer/prep/prepunion.c
  - src/include/nodes/parsenodes.h
  - src/include/nodes/plannodes.h
  - src/include/nodes/execnodes.h
symbols:
  - CommonTableExpr
  - CTEMaterialize
  - SS_process_ctes
  - inline_cte
  - RecursiveUnion
  - RecursiveUnionState
  - CteScan
  - CteScanState
  - WorkTableScan
  - generate_recursion_path
---

# Common Table Expressions (CTEs)

A Common Table Expression (CTE) is a named subquery introduced by a `WITH` clause. It lets a query give a name to a subquery result and reference that name one or more times within the outer query. Beyond readability, CTEs matter to the planner because they introduce a choice. The planner can execute the subquery in place as though it were an ordinary subquery (inlining), or it can execute the subquery once, store its output, and serve subsequent references from that stored result (materialisation). That choice has significant consequences for what the planner can and cannot optimise.

## Two execution strategies

When the planner encounters a CTE it must decide, for each CTE entry, whether to inline or materialise it. `SS_process_ctes()` (`src/backend/optimizer/plan/subselect.c`) makes the decision. It iterates over `root->parse->cteList` early in query planning, before join-order search begins.

**Inlining** replaces every `RTE_CTE` range-table entry that references the CTE with a copy of the CTE's subquery, turning each reference into an independent `RTE_SUBQUERY`. The outer query then sees only ordinary subqueries. It can apply all its normal optimisations — predicate pushdown, join reordering, index selection — across what was previously a CTE boundary. `inline_cte()` performs inlining, walking the query tree to substitute copies of the CTE's query node.

**Materialisation** compiles the CTE into a separate subplan, registered as an initplan (`SubPlan` with `subLinkType = CTE_SUBLINK`). The executor runs that initplan once, spooling results into a tuplestore. Each reference to the CTE in the main query becomes a `CteScan` node that reads from that shared tuplestore. Multiple `CteScan` nodes for the same CTE coordinate through a leader/follower mechanism: one `CteScanState` acts as the leader and owns the `cte_table` tuplestore; all others hold a read pointer into it.

## When inlining applies

Since PostgreSQL 12, the default behaviour is to inline a CTE whenever it is safe and likely beneficial. `SS_process_ctes()` applies the following gate:

```c
if ((cte->ctematerialized == CTEMaterializeNever ||
     (cte->ctematerialized == CTEMaterializeDefault &&
      cte->cterefcount == 1)) &&
    !cte->cterecursive &&
    cmdType == CMD_SELECT &&
    !contain_dml(cte->ctequery) &&
    (cte->cterefcount <= 1 ||
     !contain_outer_selfref(cte->ctequery)) &&
    !contain_volatile_functions(cte->ctequery))
```

Under the default policy (`CTEMaterializeDefault`), the planner inlines a CTE only if it is referenced exactly once. That covers the common case where inlining is a clear win. The planner can push the outer query's predicates into the subquery. It also sees a larger unified search space. When a CTE is referenced multiple times, inlining duplicates the subquery at each site. This may or may not be faster than running it once. The planner does not have enough information at this stage to make that call reliably, so it conservatively materialises.

Four conditions block inlining regardless of reference count:

- The CTE is recursive (`cterecursive`). Recursive CTEs have an iterative execution model that cannot be unrolled into a static subquery.
- The CTE contains DML or `SELECT FOR UPDATE/SHARE` (`contain_dml()`). Side effects must happen exactly the number of times the SQL specifies.
- The CTE contains volatile functions (`contain_volatile_functions()`). Inlining would cause each reference site to evaluate the volatile expression independently, potentially changing observable behaviour.
- The CTE is multiply-referenced and contains an outer self-reference to another recursive CTE (`contain_outer_selfref()`). That would produce multiple recursive self-references. The executor does not support this.

## MATERIALIZED and NOT MATERIALIZED

SQL provides explicit control through keywords on the CTE definition. These map to the `CTEMaterialize` enum stored in `CommonTableExpr.ctematerialized`:

| Keyword | Enum value | Effect |
|---|---|---|
| *(absent)* | `CTEMaterializeDefault` | Inline if singly-referenced and safe; else materialise |
| `MATERIALIZED` | `CTEMaterializeAlways` | Always materialise; blocks inlining unconditionally |
| `NOT MATERIALIZED` | `CTEMaterializeNever` | Always inline if otherwise safe; even when multiply-referenced |

For guidance on when to reach for each keyword, see [[sql-features/ctes|CTEs (SQL Feature)]].

Before PostgreSQL 12, materialisation was always the default — every CTE was an optimisation fence that the planner could not look through. That behaviour made CTEs attractive as a workaround for forcing a particular execution shape, but it also silently blocked legitimate optimisations like predicate pushdown and index use. The PG 12 change to default-inline removed the implicit fence while preserving `MATERIALIZED` for cases where the fence is intentional.

## The optimisation fence effect

A materialised CTE creates a hard boundary in the plan tree. The planner cannot push the outer query's `WHERE` predicates down into the CTE's initplan; it compiles and costs the initplan independently. This means that if the outer query filters on a column produced by the CTE, the filter applies only after the CTE has produced all its rows. The planner cannot use an index on that column inside the CTE.

## Recursive CTEs

A `WITH RECURSIVE` query adds a self-referential term to the CTE. The planner cannot inline recursive CTEs — it always materialises them, regardless of `ctematerialized`. Their subquery has `cterecursive = true`. The planner compiles it with `subquery_planner(..., hasRecursion = true, ...)`, which allocates `root->wt_param_id` to identify the work table parameter.

The execution model is iterative:

1. The non-recursive term (the left side of `UNION` or `UNION ALL`) runs first and populates the working table.
2. The recursive term (the right side) executes with `WorkTableScan` reading the current working table contents.
3. The rows produced by the recursive term replace the working table.
4. Steps 2–3 repeat until the recursive term produces no new rows.

The plan node implementing this loop is `RecursiveUnion`. Its outer child is the non-recursive term; its inner child is the recursive term. The inner child contains a `WorkTableScan` node that reads from the `Tuplestorestate` identified by `wtParam`. At execution, `RecursiveUnionState` maintains two tuplestores: `working_table` (the current generation, read by `WorkTableScan`) and `intermediate_table` (accumulating the next generation). When the inner child is exhausted, the intermediate table becomes the new working table. The inner child then restarts.

```
RecursiveUnion (wtParam = N)
├── [outer] non-recursive term
└── [inner] recursive term
         └── WorkTableScan (wtParam = N)  ← reads working_table
```

`generate_recursion_path()` in `src/backend/optimizer/prep/prepunion.c` builds the `RecursiveUnionPath`. It plans the non-recursive left side first. It stores that path in `root->non_recursive_path` so the right side can reference it during its planning. Then it builds the `RecursiveUnionPath`, combining both sides.

## UNION vs UNION ALL in recursive CTEs

The choice between `UNION ALL` and `UNION` in a recursive CTE is not merely a duplicate-elimination preference — it changes the execution machinery inside `RecursiveUnion`.

With `UNION ALL`, no duplicate tracking is needed. The `RecursiveUnion` node simply appends each generation's output. The `numCols` field of the `RecursiveUnion` plan node is zero. The executor allocates no hash table.

With `UNION`, the node must suppress rows that have already been produced in any prior iteration. `RecursiveUnionState` allocates a `TupleHashTable` (`hashtable`) spanning all iterations. It checks each candidate row against this table before emitting. This requires all output column types to support hashing. The planner rejects `WITH RECURSIVE … UNION` if any column type is not hashable. The cost model estimates `dNumGroups` as the sum of non-recursive rows plus ten times the recursive rows, a conservative worst-case used for hash table sizing.

## Key structs

**`CommonTableExpr`** (`src/include/nodes/parsenodes.h`) — one entry per `WITH` clause item:

| Field | Meaning |
|---|---|
| `ctename` | The CTE's name as written in SQL |
| `ctematerialized` | Explicit `MATERIALIZED`/`NOT MATERIALIZED` or default |
| `ctequery` | The subquery (`Query *` after parse analysis) |
| `cterecursive` | True if this CTE contains a recursive self-reference |
| `cterefcount` | Number of `RTE_CTE` range-table entries referencing this CTE |

**`RecursiveUnion`** (`src/include/nodes/plannodes.h`) — the plan node for recursive CTEs:

| Field | Meaning |
|---|---|
| `wtParam` | Param ID used to pass the working table to `WorkTableScan` |
| `numCols` | Number of columns tracked for duplicate elimination (0 for `UNION ALL`) |
| `dupColIdx` / `dupOperators` | Column indexes and equality operators for deduplication |
| `numGroups` | Estimated group count for hash table sizing |

**`RecursiveUnionState`** (`src/include/nodes/execnodes.h`) — executor state:

| Field | Meaning |
|---|---|
| `recursing` | True once the non-recursive term is exhausted |
| `intermediate_empty` | True when the current recursive iteration produced nothing |
| `working_table` | Tuplestore that `WorkTableScan` reads |
| `intermediate_table` | Accumulator for the next generation |
| `hashtable` | Tracks all rows seen, used only for `UNION` deduplication |

**`CteScan`** / **`CteScanState`** — scan node for materialised (non-recursive) CTEs. `ctePlanId` identifies the initplan subplan; `cteParam` is the shared Param slot through which multiple `CteScan` nodes coordinate access to the same tuplestore.

## Relationship to subquery planning

The planner plans materialised CTEs with a nested call to `subquery_planner()` before the outer query's join search begins. This means cost estimates for the CTE are fixed before the outer planner runs — the outer query has no ability to influence the CTE's access path or row estimates. Inlined CTEs, by contrast, become part of the outer query's `FROM` clause. They participate fully in join ordering and predicate pushdown, exactly like any other subquery.

This difference has practical consequences for [[subsystems/planner/statistics|statistics]] and cost estimation. The planner's row-count estimates for `CteScan` references are based on the CTE's own `final_rel`. This estimate cannot be refined by outer predicates, often leading to less accurate cardinality estimates than an equivalent inlined form would produce.

## Reading the query plan

The easiest way to determine what the planner did with a CTE is to look at `EXPLAIN` output. A materialised CTE appears in two places: as an `InitPlan` block near the top of the plan, and as a `CTE Scan` node wherever the CTE name is referenced in the main query body. The `InitPlan` label reflects that the planner registers the subplan as a `CTE_SUBLINK` initplan. This subplan runs before the main plan tree executes.

```
InitPlan 1 (returns $0)
  ->  Seq Scan on orders  (cost=...)
...
->  CTE Scan on recent_orders  (cost=...)
```

An inlined CTE leaves no trace under its original name. The planner folds its subquery into the outer query's range table as an `RTE_SUBQUERY` entry. As a result, the subquery appears as a `Subquery Scan`. If the subquery is simple enough, the planner merges it directly into the surrounding join without any scan node of its own. If you named your CTE `summary` and `EXPLAIN` shows no `CTE Scan on summary` and no `InitPlan`, the CTE was inlined.

## Choosing between materialised and inlined execution

The default since PostgreSQL 12 — inline when singly-referenced and safe, materialise otherwise — is correct for the vast majority of queries and needs no intervention. A singly-referenced CTE with no volatile functions will be inlined automatically, allowing outer predicates to push through, indexes inside the CTE to be used, and the planner to see the full join graph across the CTE boundary. For when explicitly forcing `MATERIALIZED` or `NOT MATERIALIZED` produces a meaningfully better plan, see the CTE usage guide referenced above.

## Pre-PostgreSQL 12 behaviour and migration

Before PostgreSQL 12, every CTE was materialised unconditionally — there was no inlining, no `MATERIALIZED` keyword, and no `NOT MATERIALIZED` keyword. The materialisation boundary was an implicit optimisation fence. Many older queries relied on this behaviour, sometimes deliberately using a CTE to prevent predicate pushdown into a subquery or to force a particular join shape.

After upgrading to PostgreSQL 12 or later, the planner will automatically inline a previously-materialised CTE that is now singly-referenced. For most queries this is a performance improvement. For queries where the pre-12 plan was intentional — for instance, a CTE that was acting as a fence to prevent an expensive subquery from being re-evaluated at every join iteration — the silent change can produce a slower plan.

The safest migration path for performance-sensitive CTEs is to annotate them explicitly. Adding `MATERIALIZED` preserves the pre-12 fence behaviour exactly. Adding `NOT MATERIALIZED` documents that inlining is intended. Reviewing unannotated CTEs in upgraded codebases under `EXPLAIN` is worthwhile if their query plans have regressed.

## Recursive CTEs and memory

Each iteration of a recursive CTE scans the working table, which is a tuplestore. For `UNION ALL`, the tuplestore grows monotonically as each generation's output is appended. For `UNION`, the deduplication hash table (`hashtable` in `RecursiveUnionState`) also grows monotonically — every distinct row ever emitted must be retained for duplicate checking, regardless of how many iterations have passed.

Deep recursions on wide rows, or graphs with large frontier sets, can consume substantial memory. When the tuplestore or hash table exceeds `work_mem`, PostgreSQL spills to disk. The signatures in `EXPLAIN ANALYZE` output are `Sort Method: external merge` on associated sort nodes, or non-zero `temp read` and `temp written` in the I/O statistics. Both indicate that spill has occurred and that the query may benefit from a higher `work_mem` setting or a restructured query.

Choosing `UNION ALL` instead of `UNION` avoids the deduplication hash table entirely when duplicates are impossible by construction — for example, a tree traversal where the graph structure guarantees each node is reached at most once. This is often the case for hierarchical queries on parent-child tables with no cycles.

For graphs that may contain cycles, a depth-limit guard prevents runaway recursion regardless of data shape:

```sql
WITH RECURSIVE reachable AS (
  SELECT id, 1 AS depth FROM graph WHERE id = $1
  UNION ALL
  SELECT g.dest, r.depth + 1
  FROM graph g
  JOIN reachable r ON g.src = r.id
  WHERE r.depth < 50   -- hard stop
)
SELECT id FROM reachable;
```

Without a guard, a cycle in the data causes the recursive term to keep producing rows indefinitely, growing the working table until the query is cancelled or the server runs out of resources.

## Related Topics

- [[subsystems/planner/subqueries|Subqueries]] — covers how ordinary subqueries are planned and how inlined CTEs integrate into the outer query's range table as RTE_SUBQUERY entries
- [[subsystems/planner/recursive-queries|Recursive Queries]] — deeper treatment of the RecursiveUnion execution model and graph traversal strategies that complement the CTE execution overview here
- [[subsystems/planner/optimization-fences|Optimization Fences]] — explains the broader concept of planning boundaries of which materialised CTEs are the primary user-facing example
- [[subsystems/planner/predicate-pushdown|Predicate Pushdown]] — describes what the planner can and cannot push through subquery boundaries, directly relevant to CTE inlining decisions
- [[subsystems/executor/subquery-values-worktable-scan|Subquery, Values, and WorkTableScan]] — executor-level detail on WorkTableScan and how it reads the working table during recursive CTE iteration
- [[subsystems/executor/tuplestore|Tuplestore]] — the tuplestore abstraction that backs both materialised CTE result sets and the working/intermediate tables in recursive CTEs
- [[sql-features/ctes|CTEs (SQL Feature)]] — user-facing reference covering WITH query syntax, MATERIALIZED/NOT MATERIALIZED keywords, and recursive CTE usage patterns
- [[subsystems/planner/overview|Planner Overview]] — how `subquery_planner` fits into the overall planning pipeline
- [[subsystems/executor/overview|Executor Overview]] — how initplans and scan nodes execute at runtime
- [[subsystems/storage/temp-files|Temporary Files and work_mem]] — tuplestore spill-to-disk behaviour used by materialised CTEs
