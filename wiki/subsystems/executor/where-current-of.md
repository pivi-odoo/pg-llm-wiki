---
title: "WHERE CURRENT OF: Positioned Update and Delete"
aliases:
  - positioned update
  - positioned delete
  - WHERE CURRENT OF
  - CURRENT OF cursor
  - execCurrentOf
tags:
  - theme/concurrency-control
source_files:
  - src/backend/executor/execCurrent.c
symbols:
  - execCurrentOf
  - search_plan_tree
  - fetch_cursor_param_value
  - CurrentOfExpr
  - ExecRowMark
---

`WHERE CURRENT OF <cursor>` in an `UPDATE` or `DELETE` statement targets the exact physical row that a cursor is currently positioned at, without re-evaluating any predicate. This is the SQL standard mechanism for *positioned updates*. It matters because it eliminates the TOCTOU gap inherent in a two-step "fetch then re-query" approach. The executor operates on the live tuple identified by the cursor's current tuple identifier (TID) rather than repeating a search that might land on a different row version.

## How the Executor Resolves the Target Row

The implementation lives in `execCurrentOf()` (`execCurrent.c`). When the [[subsystems/executor/overview|executor]] processes an `UPDATE` or `DELETE` node, it calls `execCurrentOf()` once per scan of the target table, passing the table OID and a pointer to be filled with the TID. The function returns `true` and writes the TID when the cursor is positioned on a row of the given table. It returns `false` when the cursor is scanning a different inheritance child. Any structural problem — no cursor, cursor not positioned, cursor on the wrong type of portal — raises an error immediately.

Two strategies exist for resolving the TID. The executor chooses between them automatically based on whether the cursor was declared `FOR UPDATE` or `FOR SHARE`.

### Strategy A: Row Marks (FOR UPDATE / FOR SHARE Cursors)

When a cursor carries a `FOR UPDATE` or `FOR SHARE` clause, the executor installs `ExecRowMark` entries in the query's estate during plan execution. Each row mark maintains a `curCtid` field that is updated as the cursor advances through the result set. `execCurrentOf()` locates the `ExecRowMark` entry that corresponds to the target table OID. It reads `curCtid` directly — no plan tree traversal required.

This strategy is unconditionally reliable. The executor updates the row mark at the moment the cursor fetches each row. As a result, the TID is always consistent with the cursor's current position. The cursor may carry exactly one `FOR UPDATE`/`FOR SHARE` reference to the target table. Two references would be ambiguous.

### Strategy B: Plan Tree Scan (Plain Cursors)

Cursors without locking clauses require a more invasive approach. `execCurrentOf()` walks the cursor's live `PlanState` tree via `search_plan_tree()`, looking for a scan node currently positioned on the target table OID.

`search_plan_tree()` descends through a defined set of transparent node types: `ResultState`, `LimitState`, `SubqueryScanState`, and `AppendState` (used for inheritance scans). It recognises the concrete scan types — `SeqScan`, `IndexScan`, `IndexOnlyScan`, `BitmapHeapScan`, `TidScan`, `TidRangeScan`, `ForeignScan`, and `CustomScan`. It extracts the current TID from the scan's tuple slot (`ss_ScanTupleSlot`). `IndexOnlyScan` is a special case. Virtual tuples in the slot may lack a physical ctid. In that case, the function takes the TID from the heap descriptor (`ioss_ScanDesc->xs_heaptid`) instead.

## The "Simply Updatable" Constraint

Strategy B fails when the plan contains node types that `search_plan_tree()` cannot descend through safely. The nodes that block it fall into two categories.

**Aggregation, sorting, and projection.** Nodes like `AggState`, `SortState`, or `HashState` sit between the scan and the cursor output, consuming all rows to produce their result. There is no meaningful "current scan position" in the underlying table from the cursor's perspective — the scan has already run to completion.

**Multiple scan candidates.** `AppendState` is transparent when it produces results from multiple inheritance children in sequence. At any moment, exactly one child is active. A `UNION ALL` query produces a second `AppendState` where multiple children could plausibly be scanning the same table simultaneously. `search_plan_tree()` returns `NULL` when it finds more than one matching child, causing `execCurrentOf()` to raise an error. `execCurrentOf()` rejects `MergeAppend` (used for `ORDER BY` over partitioned tables) outright. It does not fit the inheritance scan pattern, so the executor cannot treat it as transparent.

The practical consequence is that any cursor which uses aggregation, a sort, a hash, or a `UNION ALL` over the target table will produce a "cursor is not a simply updatable scan of relation" error at runtime if used with `WHERE CURRENT OF` without `FOR UPDATE`.

## Inheritance and Partitioned Tables

When the target table has inheritance children, the [[subsystems/executor/overview|executor]] scans each child in turn using an `AppendState`. `execCurrentOf()` handles this correctly: the executor calls it for each child table, with the child's OID. Strategy A returns the matching row mark for whichever child is currently active. Strategy B descends into `AppendState`. It finds the one child currently positioned on the requested OID, returning `false` for all others. The `UPDATE` or `DELETE` driver calls `execCurrentOf()` in a loop over child scans. It operates only on the child scan that returns `true`.

## The Pending Rescan Flag

`search_plan_tree()` sets a `pending_rescan` output flag to `true` whenever the node it found, or any ancestor on the path to it, has a non-null `chgParam` bitmap. A non-null `chgParam` means one of the plan node's parameters changed. The node needs to rescan before its current tuple is valid. If `pending_rescan` is set when `execCurrentOf()` returns, the TID in the slot reflects the previous fetch, not the current cursor position. `execCurrentOf()` treats this as an error — it raises "cursor is not a simply updatable scan" — because allowing a stale TID to drive a positioned update would silently modify the wrong row.

This guard is important for cursors that are parameterised (using `$N` parameters fetched via `fetch_cursor_param_value()`) or for plan nodes that re-execute when outer parameters change. Without the flag, a parameter change between `FETCH` and the DML statement could leave the scan rewound while the cursor's portal position still appears valid.

## Portal Validity Requirements

Before either strategy runs, `execCurrentOf()` validates the portal (cursor handle):

- The portal must be a `PORTAL_ONE_SELECT` portal. Utility-command cursors do not have an underlying query estate.
- The portal must not be held. A held cursor (created with `WITH HOLD`) has its query descriptor torn down after the originating transaction commits. `estate` is null, and no row marks or plan state exist.
- The cursor must be positioned: `portal->atStart` and `portal->atEnd` must both be false. A cursor at its start has never been fetched. A cursor at its end has exhausted its result set. Neither has a meaningful current row.

## Practical Notes

For bulk processing loops that fetch rows then update them, declaring the cursor `WITH FOR UPDATE` is strongly preferable. It makes `WHERE CURRENT OF` reliable regardless of plan shape, avoids the "simply updatable scan" class of errors entirely, and ensures the row is locked against concurrent modification between the fetch and the update.

Plain cursors (without `FOR UPDATE`) work for simple sequential scans. But they become fragile as queries grow. Adding an `ORDER BY`, wrapping the query in a subquery, or using a partitioned table with `ORDER BY` can all introduce plan nodes that block Strategy B. Switching to `FOR UPDATE` at that point requires no change to the `WHERE CURRENT OF` syntax — only the cursor declaration changes.

## See also

- [[code-paths/cursor|Cursors and portals]]
- [[subsystems/executor/overview|Executor overview]]
- [[subsystems/executor/tid-scan|TID scan]]
- [[subsystems/executor/junk-filter|Junk filter]]
- [[subsystems/locking/lwlocks|LWLocks]]
