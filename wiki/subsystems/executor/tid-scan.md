---
title: "TID Scan and TID Range Scan"
aliases:
  - TID scan
  - TID range scan
  - ctid scan
  - nodeTidscan
  - nodeTidrangescan
tags:
  - theme/parallelism
source_files:
  - src/backend/executor/nodeTidscan.c
  - src/backend/executor/nodeTidrangescan.c
  - src/backend/optimizer/path/tidpath.c
  - src/include/executor/nodeTidscan.h
symbols:
  - TidScan
  - TidScanState
  - TidRangeScan
  - TidRangeScanState
  - TidExpr
  - TidOpExpr
  - TidListEval
  - TidNext
  - TidRangeEval
  - TidRangeNext
  - ExecTidScan
  - ExecInitTidScan
  - ExecTidRangeScan
  - ExecInitTidRangeScan
  - create_tidscan_paths
  - IsTidEqualClause
  - IsTidRangeClause
---

# TID Scan and TID Range Scan

PostgreSQL exposes a row's physical location as the system column `ctid`, an `ItemPointerData` value encoding a block number and an offset within that block. Two executor scan nodes exploit ctid quals to bypass sequential page traversal. The **TID scan** fetches individual rows by their exact physical addresses. The **TID range scan** walks a contiguous page range. Both preserve full MVCC visibility semantics — bypassing the index does not bypass the snapshot check.

## The ctid system column

Every heap row has a `ctid` of type `tid` (a.k.a. `ItemPointerData`): a 32-bit block number identifying which 8 KB page the row lives on, and a 16-bit offset number identifying the row's slot within that page. Applications can read `ctid` like any other column:

```sql
SELECT ctid, * FROM orders WHERE id = 42;
```

The ctid is a physical address, not a logical one. It changes whenever a row is moved. `VACUUM` can compact pages and reassign offset numbers. HOT updates can redirect line pointers. `CLUSTER` rewrites the entire table in index order. Any ctid value captured before such an operation becomes invalid afterward. This fragility limits ctid-based queries to short-lived use cases: bulk deletion of a known set of rows within the same transaction, cursor positioning with `WHERE CURRENT OF`, or forensic inspection of live data.

## TID scan

A TID scan handles quals of the form `ctid = value`, `ctid = ANY(array)`, or `WHERE CURRENT OF cursor`. The planner generates a `TidScan` plan node whose `tidquals` list carries the recognized predicates. The executor evaluates them lazily.

### Qual recognition and list construction

`TidExprListCreate()` (`nodeTidscan.c`) walks `TidScan.tidquals` at initialization time. It compiles each qual into a `TidExpr` entry. It recognizes three forms:

- **Equality (`ctid = expr`)**: `TidExprListCreate()` compiles the right-hand expression into an `ExprState` for evaluation at scan start.
- **Array membership (`ctid = ANY(array_expr)`)**: `TidExprListCreate()` compiles the array expression. The executor extracts each element during evaluation.
- **`WHERE CURRENT OF cursor`**: `TidExprListCreate()` stores this as a `CurrentOfExpr` without compilation. At execution, `execCurrentOf()` resolves the cursor's current position.

`CURRENT OF` cannot coexist with other quals in the list. The planner enforces this through grammar restrictions. The exception is row-level security quals. The list-extraction logic handles these by preferring `CURRENT OF` when present.

### TID list evaluation and sorting

The first call to `TidNext()` triggers `TidListEval()`. `TidListEval()` evaluates all `TidExpr` entries and builds an array of `ItemPointerData` values. `TidListEval()` silently discards NULL TIDs and TIDs that fail `table_tuple_tid_valid()`. When multiple quals produce TIDs — from OR conditions or `= ANY(array)` — the array may contain duplicates. OR semantics mean each TID should be visited at most once.

After evaluation, `TidListEval()` sorts the array by (block, offset) with `qsort()`. It then removes duplicates with `qunique()`. Sorting serves two purposes. Deduplication becomes a linear pass rather than a hash lookup. Sequential order also allows heap fetches to proceed roughly in disk order, improving buffer locality.

### Row fetching and MVCC

`TidNext()` iterates the sorted TID array. For each address, it calls `table_tuple_fetch_row_version()`. This function fetches the tuple from the heap. It then tests the tuple against the executor's snapshot. A row can exist at the given ctid but have been deleted before the snapshot was taken. The scan silently skips such a row. The scan supports both forward and backward direction, simply traversing the sorted array in the corresponding order.

For `WHERE CURRENT OF`, `TidNext()` first calls `table_tuple_get_latest_tid()` to resolve the cursor's current physical position to the most recent version of the row, following `t_ctid` update chains. It then fetches that version. This handles rows updated after the cursor was opened.

When the executor rescans (for nested-loop joins), it frees the TID list. It sets `tss_TidPtr = -1`. This forces `TidListEval()` to rerun on the next call to `TidNext()`.

## TID range scan

Added in PostgreSQL 14, the TID range scan handles inequality quals on ctid: `ctid > value`, `ctid >= value`, `ctid < value`, `ctid <= value`, and AND-combinations thereof. Rather than fetching individual rows by address, it hands a contiguous TID range to the access method. The access method then walks the pages sequentially.

### Bound computation

`TidRangeEval()` (`nodeTidrangescan.c`) evaluates each `TidOpExpr` in the `TidRangeScan.tidrangequals` list to compute inclusive lower and upper bounds. The initial bounds are the widest possible range: `(0, 0)` to `(InvalidBlockNumber, UINT16_MAX)`. Each qual narrows one end:

- `TidRangeEval()` normalizes non-inclusive bounds to inclusive by incrementing or decrementing the `ItemPointerData` with `ItemPointerInc()` / `ItemPointerDec()`. The resulting pointer may not correspond to a valid heap slot. But the AM range scan handles this gracefully by stopping at the next valid row.
- If a qual expression produces a NULL bound, `TidRangeEval()` returns false. The scan then emits no rows. A NULL in a comparison makes the whole range vacuously empty.

Multiple quals ANDed together simply intersect both ends of the range. `TidRangeEval()` takes the maximum lower bound and minimum upper bound across all exprs.

### Sequential page access

Once the range is known, `TidRangeNext()` calls `table_beginscan_tidrange()` to open a range-bounded scan descriptor. It then calls `table_scan_getnextslot_tidrange()` in a loop. The access method (heap) walks pages from the lower bound's block to the upper bound's block in order, checking each tuple against the snapshot normally. This is page-sequential I/O rather than random seeks per TID, making range scans far more efficient for large continuous spans.

Rescan resets `trss_inScan = false`. On the next `TidRangeNext()` call, the executor re-evaluates `TidRangeEval()`. `table_rescan_tidrange()` repositions the scan descriptor.

### Role in parallel sequential scan

The primary consumer of TID range scans is parallel query. When PostgreSQL parallelizes a sequential scan, the parallel scan coordinator assigns each worker a disjoint page range expressed as a TID range qual. The worker executes a `TidRangeScan` node that physically reads only its assigned pages, with no overlap or coordination needed beyond the initial range assignment. The `AMFLAG_HAS_TID_RANGE` flag on the relation's access method advertises whether this capability is available. The planner checks this flag in `TidRangeQualFromRestrictInfoList()` (`tidpath.c`) before generating a `TidRangePath`.

## Planner path generation

The planner calls `create_tidscan_paths()` (`tidpath.c`) for every base relation during access path generation. It scans the relation's `baserestrictinfo` list twice: once for TID equality conditions (producing a `TidPath`) and once for TID range conditions (producing a `TidRangePath`).

For equality conditions, `IsTidEqualClause()` accepts `ctid = pseudoconstant` where "pseudoconstant" means the right side does not involve the relation's own Vars or volatile functions. `IsTidEqualAnyClause()` handles `ctid = ANY(array_expr)` similarly. The planner accepts OR conditions only when every OR arm contains a usable ctid qual.

The planner also generates parameterized `TidPath`s for join conditions of the form `t1.ctid = t2.ctid`. These appear in `rel->joininfo`, or the planner derives them from equivalence classes when the equality inference machinery has recorded `t1.ctid = t2.ctid`. A parameterized TID path is cheap for nested-loop joins where the outer side supplies the ctid value row by row.

For range conditions, the planner accepts only `ctid >/>=/</<= pseudoconstant`. It does not recognize OR-combined range conditions, because a range scan requires all bounds to apply simultaneously to form a single contiguous interval.

## Related Topics

- [[subsystems/storage/heap]] — how tuples are physically stored and fetched by TID
- [[subsystems/transactions/mvcc]] — how the snapshot check applies during TID-based fetches
- [[subsystems/storage/page-layout]] — block and offset number layout within a heap page
