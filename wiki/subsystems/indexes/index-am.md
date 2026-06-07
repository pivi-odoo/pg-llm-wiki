---
title: Index Access Method Interface
aliases:
  - index AM
  - IndexAmRoutine
  - amapi
tags:
  - theme/extensibility
source_files:
  - src/include/access/amapi.h
  - src/backend/access/index/indexam.c
  - src/backend/catalog/index.c
symbols:
  - IndexAmRoutine
  - IndexScanDesc
  - ambuild_function
  - aminsert_function
  - ambulkdelete_function
  - amvacuumcleanup_function
  - amcanreturn_function
  - amcostestimate_function
  - ambeginscan_function
  - amgettuple_function
  - amgetbitmap_function
  - amrescan_function
  - amendscan_function
  - index_beginscan
  - index_getnext_tid
  - index_getbitmap
  - index_bulk_delete
  - index_vacuum_cleanup
  - index_can_return
  - GetIndexAmRoutine
---

# Index Access Method Interface

PostgreSQL supports multiple index types — btree, hash, GIN, GiST, SP-GiST, BRIN, and bloom — each with fundamentally different internal structures. What makes this possible without rewriting the executor or planner for each one is a uniform interface: the index access method (AM) API. Every index type registers a handler function that returns an `IndexAmRoutine` struct (defined in `src/include/access/amapi.h`) filled with function pointers. The rest of the system calls those pointers uniformly, knowing nothing about the internal layout of any particular index.

This design mirrors the table access method interface (`TableAmRoutine`) introduced in PostgreSQL 12. Both follow the same principle: the core engine works against a stable contract; AM authors implement that contract.

## The IndexAmRoutine Struct

The `IndexAmRoutine` node is the AM's published interface. When PostgreSQL opens an index relation, it loads the AM handler once and caches the resulting `IndexAmRoutine` pointer in the relcache entry (`rd_indam`). All subsequent calls go through that pointer without re-invoking the handler.

The struct has two kinds of members: capability flags and function pointers.

### Capability Flags

Before calling any function, the planner and executor inspect the flags to understand what the AM can do:

| Field | Meaning |
|---|---|
| `amstrategies` | Number of strategy numbers the AM defines (0 if not fixed) |
| `amsupport` | Number of support function slots the AM uses |
| `amcanorder` | AM can return tuples in index-column order |
| `amcanorderbyop` | AM supports `ORDER BY operator(col)` (e.g. KNN distance) |
| `amcanbackward` | AM supports backward scans |
| `amcanunique` | AM can enforce uniqueness |
| `amcanmulticol` | AM supports multi-column indexes |
| `amoptionalkey` | AM does not require a constraint on the first column |
| `amsearcharray` | AM handles `ScalarArrayOpExpr` quals natively |
| `amsearchnulls` | AM handles `IS NULL` / `IS NOT NULL` |
| `amstorage` | Index storage type may differ from column type |
| `amclusterable` | Index can be used as a clustering target |
| `ampredlocks` | AM manages its own predicate locks |
| `amcanparallel` | AM supports parallel index scans |
| `amcaninclude` | AM supports `INCLUDE` columns |
| `amsummarizing` | AM stores data at block granularity (like BRIN) |

These flags are not hints — the planner relies on them to determine which index paths are legal for a given query. An AM that sets `amcanorder = true` will be considered for merge-join plans; one with `amsearchnulls = true` can satisfy `WHERE col IS NULL` via an index scan.

### Function Pointers

The function pointers divide into four groups: build/maintenance, scan, cost estimation, and introspection.

## Building and Inserting

When `CREATE INDEX` runs, the catalog layer calls `ambuild()` (via `index_build()` in `index.c`) with both the heap relation and the new index relation. The AM scans the heap, calls `FormIndexDatum()` for each tuple to extract the indexed values, and inserts them into its own structure. The returned `IndexBuildResult` reports the number of tuples indexed.

`ambuildempty()` handles the degenerate case of creating an empty index file — used for unlogged tables to write a clean empty index into the init fork.

Once the index exists, individual-tuple insertion goes through `aminsert()`. The core wrapper `index_insert()` (in `indexam.c`) handles serializable conflict checking before dispatching to the AM. The `checkUnique` parameter tells the AM whether to enforce uniqueness and whether to allow deduplication. The `indexUnchanged` flag, added in PostgreSQL 14, signals that the indexed columns did not change in an `UPDATE`, allowing HOT-aware AMs like btree to avoid creating unnecessary index entries.

## The Scan API

Index scans have a clear lifecycle enforced by the generic layer in `indexam.c`.

```
ambeginscan → amrescan → amgettuple (loop) → amendscan
                                  ↕
                            ammarkpos / amrestrpos
```

`ambeginscan()` allocates an `IndexScanDesc` (via `RelationGetIndexScan()`) and lets the AM initialize its private state. It does not yet receive the scan keys — those arrive in the subsequent `amrescan()` call. See [[subsystems/indexes/generic-index-access|Generic Index Access]] for why the generic layer splits key binding from descriptor allocation this way and reuses descriptors across repeated `amrescan()` calls.

The `IndexScanDesc` is the shared state between the generic layer and the AM. An AM's `amgettuple()` (or `amgetbitmap()`) implementation is responsible for populating:

| Field | Purpose |
|---|---|
| `xs_heaptid` | TID of the last tuple returned by the AM |
| `xs_recheck` | AM signals that the qual must be rechecked against the heap |
| `xs_itup` / `xs_hitup` | Index tuple, available for index-only scans |

See the generic index access page for the full `IndexScanDescData` field reference, including the fields the generic layer and executor populate.

### Tuple-at-a-Time vs. Bitmap Scans

Two scan modes exist, and an AM typically supports one or both.

**Tuple-at-a-time** scanning uses `amgettuple()`. The AM finds the next matching index entry, stores its TID in `scan->xs_heaptid`, and returns true. The generic `index_getnext_tid()` wrapper returns that TID to the executor, which then calls `index_fetch_heap()` to visit the heap page. The AM controls ordering: it may return tuples in index order (if `amcanorder` is set), which the planner uses to avoid sort nodes. `ammarkpos()` and `amrestrpos()` save and restore a scan position — needed only by merge join, and only for AMs that support it.

**Bitmap scanning** uses `amgetbitmap()`, which fills a `TIDBitmap` with all matching TIDs in a single call, then returns. The executor accumulates bitmaps from multiple indexes (with AND/OR logic), then visits the heap once per page rather than once per tuple. This is more efficient when a query matches many rows. Heap pages are fetched in physical order, and each page is pinned at most once. Because the heap access is decoupled from the index scan, bitmap scans require an MVCC snapshot — there is no per-tuple interlock.

The two modes are not interchangeable. BRIN, for instance, only implements `amgetbitmap` (its block-granularity precision makes tuple-at-a-time delivery meaningless). Btree implements both.

## Index-Only Scans

An index-only scan returns column values directly from the index without visiting the heap. This is possible when every column referenced in the query is stored in the index — either as a key column or an `INCLUDE` column.

The AM advertises this capability per column via `amcanreturn()`. The generic wrapper `index_can_return()` returns false if the AM provides no `amcanreturn` function at all — an AM that never implements it (like hash) can never serve an index-only scan, regardless of query shape. See the generic index access page for how the executor uses this capability during the scan, including the visibility-map fallback that still forces a heap fetch on recently-modified pages.

## Operator Classes and Strategy Numbers

An index does not directly understand SQL operators like `<` or `=`. Instead, each AM defines a fixed set of *strategy numbers* — small integers that identify abstract operations the AM supports. Operator classes (`pg_opclass`) map concrete operators to strategy numbers for a given data type.

Btree's five strategies are the canonical example:

| Strategy | Meaning |
|---|---|
| 1 | `<` |
| 2 | `<=` |
| 3 | `=` |
| 4 | `>=` |
| 5 | `>` |

GiST has seven strategies (including containment, overlap, and equality); GIN has two (containment and equality); BRIN has four (like btree but range-based).

The AM declares how many strategies it has via `amstrategies`. When the planner matches a WHERE clause to an index scan, it looks up the operator's strategy number in the opclass and checks whether the AM can use that strategy. The AM never sees the operator OID directly — only the strategy number and the scan key value.

## Support Functions

Strategies cover the operators users write in queries. AMs also need auxiliary functions for internal operations — comparison, hashing, consistency checking — that are not query operators. These are *support functions*, stored in `pg_amproc` and retrieved at runtime via `index_getprocid()` and `index_getprocinfo()` in `indexam.c`.

Support function numbers are AM-specific. Btree uses support function 1 for the comparison function (returns negative/zero/positive like `strcmp`) and support function 2 for an optional sortsupport routine. GiST uses support function 1 for its `consistent` function (tests whether an entry could match a query), support function 2 for `union`, support function 3 for `compress`, and so on.

The generic layer caches support function lookup info (`FmgrInfo`) in the relcache alongside the index's `rd_support` and `rd_supportinfo` arrays, indexed by `(attnum, procnum)`. This avoids repeated syscache lookups during scan-heavy workloads.

The `amoptsprocnum` field identifies a special optional support function that parses per-column opclass options (e.g. `text_pattern_ops` options). When it is non-zero, `index_opclass_options()` calls it to validate and parse `WITH (...)` clauses on individual index columns.

## Cost Estimation

The planner calls `amcostestimate()` for each candidate index path to determine startup cost, total cost, selectivity, and the correlation between index order and heap physical order. The AM has access to the full `IndexPath` (which includes the scan keys already matched) and can use statistics to refine estimates.

Correlation is especially important for btree: an index on a naturally-ordered column has correlation near 1.0, meaning sequential heap access and thus cheap fetches. An index on a random column has correlation near 0. The planner will penalize it for random I/O. The planner uses `indexCorrelation` to choose between an index scan and a bitmap index scan — see [[subsystems/planner/cost-model]] for the full calculation.

An AM can set `amcanorderbyop = true` to advertise support for distance-ordered results (KNN queries). In that case `amcostestimate()` must also estimate the order-by cost. GiST uses this for nearest-neighbor searches.

## Vacuum and Maintenance

Two callbacks handle index maintenance during VACUUM.

`ambulkdelete()` is the primary deletion pass. VACUUM calls it with a callback function that, given a TID, returns true if that heap tuple is dead. The AM walks its structure, calls the callback for each entry, and removes dead ones. This design lets the AM traverse its own pages in whatever order is efficient (b-tree leaf pages left-to-right, for instance) without the vacuum code needing to know the internal layout. The `IndexBulkDeleteResult` returned accumulates statistics (pages visited, tuples removed, etc.) that feed into `pg_stat_user_indexes`.

`amvacuumcleanup()` runs after the deletion pass. It is the AM's opportunity to reclaim internal free space — merging sparse btree pages, cleaning GIN pending lists, or updating BRIN summaries for ranges whose heap pages changed. It also updates the `IndexBulkDeleteResult` with final page-count statistics. For AMs like hash that cannot efficiently do bulk deletion in a single pass, `amvacuumcleanup` may do the real work here instead.

Parallel vacuum is also supported: `amparallelvacuumoptions` is a bitmask (from `vacuum.h`) that tells the vacuum machinery whether the AM's bulk-delete and cleanup passes can run in parallel worker processes.

## Registering an AM

A custom index AM is registered by writing a C function that returns an `IndexAmRoutine *` (allocated with `palloc`) and creating a row in `pg_am` pointing to that function. `GetIndexAmRoutine()` calls the handler and validates the returned struct. The relcache stores the result so the handler is only called once per backend session per index relation opened.

The `amvalidate()` function lets the AM check that a proposed operator class is complete and consistent — called when `CREATE OPERATOR CLASS` or `CREATE OPERATOR FAMILY` is executed, not at scan time.

## Key Relationships

The AM interface connects to several other subsystems:

- The [[subsystems/executor/overview]] drives the scan lifecycle (beginscan → gettuple loop → endscan) and handles the heap fetch after receiving a TID.
- The [[subsystems/planner/overview]] uses `amcostestimate()` and the capability flags to enumerate and price index paths.
- The [[subsystems/storage/heap]] is accessed via `table_index_fetch_tuple()` whenever the AM returns a TID that needs a heap visit.
- The [[subsystems/transactions/mvcc]] snapshot is threaded through the scan descriptor; the AM uses it indirectly (for index-only scans the visibility map check happens in the generic layer, not the AM).
- Individual AM implementations — [[subsystems/indexes/btree]], [[subsystems/indexes/gin]], [[subsystems/indexes/gist]], [[subsystems/indexes/brin]], [[subsystems/indexes/hash]], [[subsystems/indexes/spgist]] — each fill in this interface differently.
- The [[code-paths/create-index]] path calls `ambuild()` and `ambuildempty()`.
- The [[code-paths/vacuum]] path calls `ambulkdelete()` and `amvacuumcleanup()`.
- The [[code-paths/index-scan]] path exercises the full beginscan/gettuple/endscan lifecycle.

## Related Topics

- [[subsystems/indexes/generic-index-access|Generic Index Access]] — the generic `indexam.c` wrapper layer that dispatches through the `IndexAmRoutine` function pointers covered here.
- [[subsystems/extensions/custom-index-am|Custom Index AM]] — how to implement a new index type by registering an `IndexAmRoutine` handler.
- [[subsystems/indexes/index-maintenance|Index Maintenance]] — how VACUUM drives `ambulkdelete` and `amvacuumcleanup` to keep indexes clean.
- [[subsystems/indexes/index-only-scans|Index-Only Scans]] — the executor-side mechanics of reading column values from the index without a heap fetch.
- [[subsystems/storage/table-am|Table Access Method]] — the parallel `TableAmRoutine` interface that governs heap access, introduced in the same release as the formalized index AM API.
- [[subsystems/planner/index-selection|Index Selection]] — how the planner evaluates `amcostestimate` results and capability flags to choose among candidate indexes.
- [[subsystems/catalog/core-catalogs|Core Catalogs]] — `pg_am`, `pg_opclass`, `pg_opfamily`, and `pg_amproc`, which store the AM registration and operator class mappings the index AM interface depends on.
