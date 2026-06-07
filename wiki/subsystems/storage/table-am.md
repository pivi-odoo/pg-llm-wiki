---
title: Table Access Method Interface
aliases:
  - table AM
  - tableam
  - TableAmRoutine
tags:
  - theme/extensibility
source_files:
  - src/include/access/tableam.h
  - src/backend/access/table/tableam.c
  - src/backend/access/heap/heapam_handler.c
symbols:
  - TableAmRoutine
  - TableScanDesc
  - TM_Result
  - TM_FailureData
  - TU_UpdateIndexes
  - ScanOptions
  - table_beginscan
  - table_scan_getnextslot
  - table_tuple_insert
  - table_tuple_update
  - table_tuple_delete
  - table_tuple_lock
  - table_index_fetch_tuple
  - heapam_methods
  - GetTableAmRoutine
---

# Table Access Method Interface

Before PostgreSQL 12, the executor was tightly coupled to heap storage. Every node that read or wrote tuples called `heap_*` functions directly. This made it practically impossible to substitute a different storage engine without invasive changes. The table access method (table AM) interface, introduced in PG 12, breaks that coupling by placing a uniform API layer between the executor and any storage implementation. An access method registers a function pointer table. The executor calls `table_*` wrappers that dispatch through it. The executor never needs to know whether tuples live in heap pages, a columnar store, or anything else.

## Registration and the TableAmRoutine Struct

Every table AM is a catalog object with a row in `pg_am` (amtype `'t'`). `pg_am.amhandler` stores the AM's handler function. When PostgreSQL opens a relation, it calls that handler to obtain a pointer to a `TableAmRoutine` struct (`GetTableAmRoutine()`, `tableamapi.c`). The struct must be allocated in server-lifetime memory — in practice it is always a static `const` object.

`TableAmRoutine` is a flat collection of function pointers grouped by concern:

| Group | Representative callbacks |
|---|---|
| Slot | `slot_callbacks` |
| Sequential scan | `scan_begin`, `scan_end`, `scan_rescan`, `scan_getnextslot` |
| TID range scan | `scan_set_tidrange`, `scan_getnextslot_tidrange` |
| Parallel scan | `parallelscan_estimate`, `parallelscan_initialize`, `parallelscan_reinitialize` |
| Index fetch | `index_fetch_begin`, `index_fetch_reset`, `index_fetch_end`, `index_fetch_tuple` |
| Tuple read | `tuple_fetch_row_version`, `tuple_satisfies_snapshot`, `tuple_get_latest_tid` |
| Tuple write | `tuple_insert`, `tuple_insert_speculative`, `tuple_complete_speculative`, `multi_insert`, `tuple_delete`, `tuple_update`, `tuple_lock` |
| DDL / maintenance | `relation_set_new_filelocator`, `relation_vacuum`, `relation_copy_for_cluster` |
| Analyze sampling | `scan_analyze_next_block`, `scan_analyze_next_tuple` |
| Index maintenance | `index_build_range_scan`, `index_validate_scan`, `index_delete_tuples` |
| Sizing | `relation_size`, `relation_estimate_size` |
| [[subsystems/storage/toast|TOAST]] | `relation_needs_toast_table`, `relation_toast_am`, `relation_fetch_toast_slice` |
| Bitmap scan | `scan_bitmap_next_block`, `scan_bitmap_next_tuple` |
| Sample scan | `scan_sample_next_block`, `scan_sample_next_tuple` |

The `slot_callbacks` entry is notable: each AM returns the `TupleTableSlotOps` implementation appropriate for its tuple representation. This makes the slot type pluggable too. Heap returns `TTSOpsBufferHeapTuple`, which pins the buffer while the slot is live.

The `Relation` struct caches the resolved pointer at `rd_tableam`. Dispatch is therefore a single pointer dereference with no catalog lookups on the hot path.

## How the Heap Implements the Interface

The heap AM (`heapam_handler.c`) registers a static `heapam_methods` struct. It exposes the struct through the `heap_tableam_handler` SQL function. Every field maps to an existing heap routine:

```c
static const TableAmRoutine heapam_methods = {
    .type = T_TableAmRoutine,
    .slot_callbacks          = heapam_slot_callbacks,   /* TTSOpsBufferHeapTuple */
    .scan_begin              = heap_beginscan,
    .scan_getnextslot        = heap_getnextslot,
    .tuple_insert            = heapam_tuple_insert,     /* calls heap_insert() */
    .tuple_update            = heapam_tuple_update,
    .tuple_delete            = heapam_tuple_delete,
    .relation_vacuum         = heap_vacuum_rel,
    /* ... */
};
```

The thin handler functions in `heapam_handler.c` do little more than translate between the slot-based interface and the lower-level heap routines. `heapam_tuple_insert`, for instance, extracts a `HeapTuple` from the slot with `ExecFetchSlotHeapTuple()`, delegates to `heap_insert()`, then copies the resulting TID back into the slot. The handler layer is the boundary where the abstraction is paid for — inside it, all the familiar heap machinery applies unchanged.

`DEFAULT_TABLE_ACCESS_METHOD` is the string `"heap"`. The GUC `default_table_access_method` controls what AM is used when `CREATE TABLE` omits `USING`. Any table can specify a non-default AM with `CREATE TABLE ... USING <amname>`.

## The Scan API

Sequential scans follow a consistent lifecycle. The caller opens a scan with `table_beginscan()`, which dispatches to `scan_begin` and returns a `TableScanDesc`:

```c
TableScanDesc
table_beginscan(Relation rel, Snapshot snapshot, int nkeys, ScanKeyData *key)
{
    uint32 flags = SO_TYPE_SEQSCAN | SO_ALLOW_STRAT | SO_ALLOW_SYNC | SO_ALLOW_PAGEMODE;
    return rel->rd_tableam->scan_begin(rel, snapshot, nkeys, key, NULL, flags);
}
```

`ScanOptions` flags tell the AM what kind of scan the caller is requesting (`SO_TYPE_SEQSCAN`, `SO_TYPE_BITMAPSCAN`, `SO_TYPE_SAMPLESCAN`, etc.) and which optimizations to enable (`SO_ALLOW_STRAT` for a buffer access strategy ring, `SO_ALLOW_SYNC` for syncscan coordination, `SO_ALLOW_PAGEMODE` for page-at-a-time visibility). The AM may ignore flags it does not support, but must honour the scan type. The `SO_TEMP_SNAPSHOT` flag signals that the AM should unregister the snapshot when the scan ends.

`table_scan_getnextslot()` advances the scan and fills a `TupleTableSlot`. It returns `true` if it found a visible tuple, or `false` at the end of the scan. MVCC visibility is the AM's responsibility — the scan only returns tuples visible to the snapshot passed at `scan_begin`. The heap implementation checks `HeapTupleSatisfiesVisibility()` page-at-a-time when the caller sets `SO_ALLOW_PAGEMODE`. This amortizes the visibility work across all tuples on a page before releasing the buffer lock.

There are specialised entry points for different scan types: `table_beginscan_bm()` for bitmap heap scans, `table_beginscan_sampling()` for `TABLESAMPLE`, `table_beginscan_tid()` for TID scans, and `table_beginscan_tidrange()` for range scans over contiguous TID space. All return a `TableScanDesc` and feed into the same `table_scan_getnextslot()` call path.

## Executor Decoupling

The executor never calls `heap_*` functions directly. Every executor node that reads rows goes through `table_scan_getnextslot()` or `table_index_fetch_tuple()`. Every executor node that writes rows goes through `table_tuple_insert()`, `table_tuple_update()`, or `table_tuple_delete()`. This indirection is what makes alternative storage engines possible within the executor framework.

Index scans use a separate path. The executor opens an `IndexFetchTableData` handle with `table_index_fetch_begin()`, then calls `table_index_fetch_tuple()` for each TID the index returns. The heap implementation (`heapam_index_fetch_tuple`) calls `heap_hot_search_buffer()` to follow HOT chains. This is invisible to the executor: it just sees a visible tuple or a miss. An AM that does not use HOT simply returns the tuple at the exact TID.

This decoupling is why columnar storage extensions, such as Citus columnar and the earlier zedstore experiment, can integrate with PostgreSQL's executor. They register their own `TableAmRoutine` and handle the `table_*` calls in whatever way fits their storage format. The executor nodes operate identically to how they do with heap.

## Visibility and MVCC

Visibility is an AM responsibility, not a framework service. The contract is that `scan_getnextslot` and `index_fetch_tuple` only return tuples that satisfy the snapshot. The caller passes the snapshot when it opens the scan. `TableScanDesc.rs_snapshot` holds the snapshot for the rest of the scan.

For write operations, the AM returns a `TM_Result` code that tells the caller what happened:

| Code | Meaning |
|---|---|
| `TM_Ok` | Operation succeeded |
| `TM_Invisible` | Tuple was not visible to the snapshot |
| `TM_SelfModified` | Tuple was already modified by the current transaction |
| `TM_Updated` | Tuple was updated by a concurrent transaction |
| `TM_Deleted` | Tuple was deleted by a concurrent transaction |
| `TM_BeingModified` | Concurrent modification in progress (only when not waiting) |
| `TM_WouldBlock` | Lock could not be acquired without blocking |

When the result is not `TM_Ok`, the AM fills a `TM_FailureData` struct with the conflicting tuple's `ctid` (pointing to the replacement version), `xmax` (the conflicting transaction ID), and `cmax` (the command ID, valid only for `TM_SelfModified`). The executor uses this information to decide whether to retry, wait, or error out.

`TU_UpdateIndexes` is a companion enum that `table_tuple_update()` returns. It tells the executor whether index entries need updating: `TU_None` if no indexed column changed, `TU_All` for a full index update, and `TU_Summarizing` if only columns covered by summarizing indexes (like BRIN) changed. This avoids unnecessary index work on updates that do not touch indexed columns.

## Tuple-Level Locking

The AM controls how tuple-level locking works for `UPDATE` and `DELETE`. The executor calls `table_tuple_lock()` with a `LockTupleMode` (share, no-key exclusive, exclusive, key-share) and a `LockWaitPolicy`. For heap, this calls `heap_lock_tuple()`. It stamps the tuple header with the locker's XID and records the lock in the lock manager as a heavyweight lock when necessary.

Two flags govern chain-following behaviour. `TUPLE_LOCK_FLAG_LOCK_UPDATE_IN_PROGRESS` allows locking a tuple whose update is in progress if the lock modes are compatible. `TUPLE_LOCK_FLAG_FIND_LAST_VERSION` instructs the AM to follow the update chain to the latest version before acquiring the lock. An alternative AM that does not implement update chains can simply ignore these flags and lock the tuple at the given TID.

## Per-AM Relation Options

Each AM can expose storage options that users set via `CREATE TABLE ... WITH (option = value)` or `ALTER TABLE ... SET (option = value)`. PostgreSQL stores these in `pg_class.reloptions` as a serialised list. It decodes them at relation open time. The heap AM defines options like `fillfactor`, `autovacuum_vacuum_scale_factor`, and `toast_tuple_target`. A columnar AM could define its own options for compression method or stripe size. The AM framework does not dictate which options exist; it only provides the storage slot.

## Relation Sizing and Planner Integration

The planner needs estimates of row counts and page counts for cost calculations. `relation_estimate_size` provides these — the heap implementation calls `table_block_relation_estimate_size()`, a shared helper in `tableam.c` that works for any block-oriented AM. AMs with non-block-based layouts must provide their own estimate logic and map it to the `BlockNumber pages, double tuples` output parameters that the planner expects.

`relation_size` returns the actual on-disk size in bytes for a given fork. The heap delegates to `table_block_relation_size()`, also a shared helper, which sums the sizes of all segments for the fork. The planner uses `MAIN_FORKNUM` size to determine the scan range for BRIN and ANALYZE.

## Vacuum and Maintenance

VACUUM dispatches through `relation_vacuum`, which for heap calls `heap_vacuum_rel()`. The AM receives a `VacuumParams` struct specifying the cost limits and aggressiveness, and a `BufferAccessStrategy` to limit buffer cache churn. VACUUM FULL and CLUSTER take a different path through `relation_copy_for_cluster`, which lets the AM define how it extracts and rewrites tuples. An AM that stores data in a format incompatible with heap-style compaction can substitute its own CLUSTER logic here.

ANALYZE sampling uses `scan_analyze_next_block` and `scan_analyze_next_tuple` rather than the regular scan path. The distinction matters because ANALYZE uses random block sampling and needs the AM to track live and dead row counts separately.

## Relation to Other Subsystems

The table AM interface does not cover index access — that is the index AM interface (`IndexAmRoutine`, `src/include/access/amapi.h`), which is separate. See [[subsystems/indexes/btree]] for how B-tree implements the index AM side. The table AM's `index_build_range_scan` and `index_validate_scan` callbacks let the table AM control how it presents its tuples during index builds. The table AM knows best how to iterate its storage efficiently.

Tuple visibility at the table AM layer depends on the snapshot subsystem described in [[subsystems/transactions/mvcc]]. [[subsystems/storage/buffer-manager]] covers the buffer manager that the heap AM relies on. [[subsystems/storage/heap]] describes the physical heap page format.

[[subsystems/executor/overview]] describes the executor nodes that consume the table AM interface — SeqScan, IndexScan, IndexOnlyScan, BitmapHeapScan, ModifyTable. [[subsystems/executor/tuple-table-slot]] covers the `TupleTableSlot` that carries results between table AM calls and executor nodes.

[[subsystems/locking/row-level-locking]] describes row-level locking semantics, including how `table_tuple_lock()` interacts with transaction waiting.

## Related Topics

- [[subsystems/indexes/index-am|Index Access Method Interface]] — the parallel AM interface for indexes (`IndexAmRoutine`), which the table AM interacts with during index builds and index-only scans.
- [[subsystems/storage/heap|Heap Storage]] — the physical heap page format and low-level heap routines that the built-in table AM wraps.
- [[subsystems/storage/buffer-manager|Buffer Manager]] — the shared buffer infrastructure the heap AM relies on to pin and read pages during scans and writes.
- [[subsystems/transactions/mvcc|MVCC]] — the snapshot and visibility model that table AM implementations must honour when returning tuples.
- [[subsystems/executor/tuple-table-slot|Tuple Table Slot]] — the slot abstraction that table AM callbacks populate, connecting AM-specific tuple formats to executor nodes.
- [[subsystems/extensions/custom-index-am|Custom Index AM]] — shows how to register a custom access method, following the same `pg_am` pattern used by table AMs.
- [[subsystems/storage/reloptions|Relation Options]] — how per-AM storage options declared in `CREATE TABLE ... WITH (...)` are stored in `pg_class.reloptions` and decoded.
