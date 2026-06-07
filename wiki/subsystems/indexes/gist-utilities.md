---
title: "GiST Utility Functions"
aliases:
  - gist utilities
  - gistutil
tags:
  - theme/storage-format
  - theme/durability
source_files:
  - src/backend/access/gist/gistutil.c
  - src/include/access/gist_private.h
symbols:
  - gistFormTuple
  - gistCompressValues
  - gistNewBuffer
  - GISTInitBuffer
  - gistinitpage
  - gistchoose
  - gistpenalty
  - gistunion
  - gistMakeUnionItVec
  - gistMakeUnionKey
  - gistgetadjusted
  - gistKeyIsEQ
  - gistdentryinit
  - gistDeCompressAtt
  - gistFetchTuple
  - gistfillbuffer
  - gistextractpage
  - gistjoinvector
  - gistfillitupvec
  - gistnospace
  - gistfitpage
  - gistcheckpage
  - gistPageRecyclable
  - gistGetFakeLSN
  - gistproperty
  - GiSTOptions
---

# GiST Utility Functions

`gistutil.c` is the shared toolkit that every other [[subsystems/indexes/gist|GiST]] source file draws on. It handles the mechanics that are needed across insertion, search, splits, and vacuuming: forming and decompressing index tuples, choosing which child to descend into during an insert, computing union keys, initialising pages and buffers, and recycling deleted pages. These routines encapsulate the repetitive but consequential work so that higher-level code in `gist.c`, `gistget.c`, and `gistsplit.c` can express algorithms at a higher level without reimplementing page layout arithmetic.

## Tuple formation and compression

`gistFormTuple` is the single place where a `Datum` array becomes an `IndexTuple` that can be written to an index page. It calls `gistCompressValues` to run each column's `compress()` support function (if one exists), then passes the results to `index_form_tuple` with either the leaf or non-leaf tuple descriptor from `GISTSTATE`. Because inner-page tuples do not correspond to heap rows, their `t_tid` offset field is set to `0xffff` (`TUPLE_IS_VALID`) — a convention that distinguishes them from the legacy "invalid" downlinks (`0xfffe`) used before PostgreSQL 9.1.

`gistCompressValues` handles the compress step column by column. For each key column it initialises a `GISTENTRY`, invokes `compressFn` if one is registered, and collects the resulting datum. For leaf tuples it also copies through any included (non-key) columns unchanged, because those are never compressed. The `isleaf` flag threads through both functions so the right tuple descriptor is selected at every step.

The mirror operation is `gistdentryinit`, which populates a `GISTENTRY` from a stored datum by calling `decompressFn`. If the opclass supplies no decompress function, the stored datum is used as-is. The decompressed entry is what `consistent()`, `penalty()`, and `equal()` operate on, so this initialisation step is called in essentially every read path.

## Descent and subtree selection

`gistchoose` implements the greedy descent rule for insertion: given an inner page and a new index tuple, it finds the child entry with the minimum penalty. It iterates over all entries on the page and calls `gistpenalty` for each column in index-definition order. Column priority is lexicographic: a strictly lower penalty on column 0 beats any combination of penalties on later columns. Later columns are only consulted when earlier ones tie.

The penalty call itself (`gistpenalty`) is a thin wrapper around the opclass `penaltyFn`. It enforces that returned penalties are non-negative and finite: NaN or negative values are clamped to zero. Mixing null and non-null entries is assigned an infinite penalty to discourage the access method from placing indexed values alongside null keys in the same subtree.

When two entries are tied across all columns, `gistchoose` breaks the tie randomly. The rationale is a cache-locality tradeoff. Repeatedly descending the same path is cache-friendly for dense inserts of similar values, but it concentrates all inserts on one subtree after a split, leaving the other half permanently underused. A probabilistic tie-break distributes load while still preferring the existing best subtree most of the time. A `keep_current_best` flag persists the random decision across multiple equal candidates so the randomness is applied at most once per "generation" of equals.

## Union key maintenance

After any structural change — an insert that widens a bounding predicate, or a split — the parent's union key must be updated to remain a valid covering predicate for its subtree. Three functions handle this at different granularities.

`gistMakeUnionItVec` computes a union over a full vector of `IndexTuple` values, one column at a time. It builds a `GistEntryVector` of the non-null datums for each column and calls `unionFn`. If only one non-null entry exists, the entry is duplicated before the call because the opclass convention guarantees at least two inputs. Columns that are entirely null produce a null union.

`gistMakeUnionKey` is the two-entry variant: it merges exactly two `GISTENTRY` values for a single column. When one entry is null and the other is not, the non-null entry is doubled to satisfy the two-input requirement. This is used during the upward fixup walk after an insert.

`gistunion` wraps `gistMakeUnionItVec` to produce a complete `IndexTuple` rather than a raw `Datum` array — it is the function called when an entire page's worth of entries needs to be summarised into a single downlink key.

`gistgetadjusted` compares the union of two tuples against the existing key and returns a new tuple only if the union is strictly wider than what is already stored. The equality test uses `gistKeyIsEQ`, which invokes the opclass `equalFn`. If nothing changed, `gistgetadjusted` returns NULL, allowing callers to skip unnecessary WAL writes and page modifications when an insert falls entirely within the existing bounding predicate.

## Page and buffer initialisation

`gistinitpage` initialises a raw page for use as a GiST index page. It calls `PageInit` to lay out the standard page header and special area, then writes the `GISTPageOpaqueData` fields. `rightlink` is set to `InvalidBlockNumber` (no sibling yet), `flags` receives the caller-supplied bits (typically `F_LEAF` or `0` for inner), and `gist_page_id` is set to `GIST_PAGE_ID` (`0xFF81`) for identification by tools like `pg_filedump`. `GISTInitBuffer` is the buffer-level wrapper that reads the page from the buffer and delegates to `gistinitpage`.

`gistcheckpage` validates a freshly read page before use. It rejects all-zero pages (which `ReadBuffer` accepts but GiST cannot use) and checks that the special area is exactly `MAXALIGN(sizeof(GISTPageOpaqueData))`. Both conditions indicate on-disk corruption and trigger a `REINDEX` hint.

## Buffer allocation and page recycling

`gistNewBuffer` is the allocation path for new index pages. It first consults the Free Space Map via `GetFreeIndexPage`. Because another backend may have raced to claim the same page, it uses `ConditionalLockBuffer` rather than a blocking lock; if the lock fails, it releases and tries the next [[subsystems/storage/fsm|FSM]] entry. Once a candidate page is locked it is accepted if either the page is brand new (`PageIsNew`) or it is a deleted page old enough to be recycled (`gistPageRecyclable`). If the FSM is exhausted, the file is extended with `ExtendBufferedRel`.

`gistPageRecyclable` checks whether a deleted page is safe to reuse. A deleted page carries a `deleteXid` (a `FullTransactionId` stored in `GISTPageOpaqueData` via `GistPageGetDeleteXid`). The page can only be recycled once no running transaction could have a snapshot that observed the downlink pointing to it — tested with `GlobalVisCheckRemovableFullXid`. This is the same tombstone mechanism used by [[subsystems/indexes/btree]] for deleted pages. It ensures that concurrent scans following stale downlinks encounter a recognisable tombstone rather than repurposed data.

When a deleted page is recycled and WAL is active, `gistXLogPageReuse` emits a WAL record so that Hot Standby replicas can generate a recovery conflict against any queries holding snapshots older than `deleteXid`.

## Index-only scan support

`gistFetchTuple` reconstructs the original indexed values from a leaf tuple for an index-only scan. For each key column it checks whether a `fetchFn` is registered and calls it via `gistFetchAtt`. If no fetch function exists but the opclass also has no compress function, the stored datum is already the original value and is returned directly. If neither condition holds — meaning the column is stored in compressed form with no way to reverse the compression — the column is replaced with NULL. This is acceptable when the planner chose an index-only scan without needing that column's value.

`GIST_FETCH_PROC` (support function 9) is what makes a column index-only-scan capable; `gistproperty` checks for this function when the executor queries `AMPROP_RETURNABLE`. The same property mechanism handles `AMPROP_DISTANCE_ORDERABLE` by looking for `GIST_DISTANCE_PROC`.

## Fake LSNs for unlogged indexes

GiST uses page LSNs to detect concurrent splits during searches: a child's NSN (node sequence number) is compared against the parent's LSN recorded at descent time. For relations that are not WAL-logged — temporary tables and unlogged tables — there are no real WAL LSNs, so `gistGetFakeLSN` provides synthetic ones. Temporary relations use a backend-local counter (safe because no other backend can access the relation). Permanent unlogged relations use `GetFakeLSNForUnloggedRel`, which survives clean restarts. The function also handles the intermediate case of relations that are currently being built without WAL (during `CREATE INDEX`) by emitting a minimal dummy WAL record if the insert LSN has not advanced since the last call.

## Vector and page I/O helpers

A set of lower-level helpers move index tuples between pages and in-memory vectors. `gistextractpage` reads all items from a page into a palloc'd `IndexTuple` array. `gistfillbuffer` writes a tuple vector onto a page starting at a given offset. `gistjoinvector` concatenates two `IndexTuple` arrays with `repalloc`. `gistfillitupvec` packs a tuple vector into a contiguous flat buffer, which is used when preparing WAL records and split descriptors. `gistnospace` and `gistfitpage` compute whether a proposed set of tuples will fit on a page, accounting for an optional tuple being deleted to make room.

## Related Topics

- [[subsystems/indexes/gist|GiST Index]] — the overall framework, operator class contract, and tree structure
- [[subsystems/indexes/btree|B-tree Index]] — contrasting index architecture; shares the deleted-page tombstone approach
- [[subsystems/indexes/gin|GIN Index]] — complementary index for multi-element types
