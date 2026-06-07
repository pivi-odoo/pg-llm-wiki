---
title: "GIN Entry Page Management and WAL"
aliases:
  - gin entry page
  - gin wal
  - gin page initialization
  - gin xlog
  - ginutil
  - ginentrypage
  - ginxlog
tags:
  - theme/durability
  - theme/storage-format
source_files:
  - src/backend/access/gin/ginutil.c
  - src/backend/access/gin/ginentrypage.c
  - src/backend/access/gin/ginxlog.c
symbols:
  - GinInitPage
  - GinInitBuffer
  - GinInitMetabuffer
  - GinGetStats
  - GinUpdateStats
  - GinMetaPageData
  - GinPageOpaqueData
  - ginCompareEntries
  - ginCompareAttEntries
  - GinFormTuple
  - ginEntryFillRoot
  - entrySplitPage
  - ginRedoInsert
  - ginRedoSplit
  - ginRedoVacuumPage
  - ginRedoVacuumDataLeafPage
  - ginRedoDeletePage
  - ginRedoUpdateMetapage
  - ginRedoInsertListPage
  - ginRedoDeleteListPages
  - ginRedoCreatePTree
  - gin_redo
---

The [[subsystems/indexes/gin]] access method stores all of its persistent state in heap-managed 8 kB pages. Every modification to those pages must be recoverable after a crash. Three source files — `ginutil.c`, `ginentrypage.c`, and `ginxlog.c` — cover the low-level page-management layer that makes this possible: how fresh pages are stamped with their identity, how entry-tree leaf and interior tuples are constructed and split, and how WAL records are written and replayed for every kind of GIN modification.

## Page Initialization and the Opaque Trailer

Every GIN page carries a fixed-size `GinPageOpaqueData` structure in the page's special space — the region beyond the item array that PostgreSQL's page layout reserves for access-method use. The struct holds three fields: `rightlink` (the block number of the next page in the same B-tree level, or `InvalidBlockNumber` for the rightmost page), `maxoff` (used as a count of `PostingItem`s on non-leaf data pages and as a heap-tuple count on pending-list pages), and `flags` (a bitmask that encodes the page's role).

`GinInitPage()` (`ginutil.c`) calls the generic `PageInit()` to zero and format the page header, then writes the opaque fields. The flags argument is passed in by the caller so the same function handles every page type: entry-tree pages receive no `GIN_DATA` bit, data pages set `GIN_DATA`, list pages set `GIN_LIST`, the metapage sets `GIN_META`, and deleted pages carry `GIN_DELETED`. The leaf/non-leaf distinction is controlled by the `GIN_LEAF` bit. `GIN_INCOMPLETE_SPLIT` is a transient flag set when a page split has been performed but the parent has not yet been updated. WAL replay clears it once the parent insertion record is replayed.

`GinInitBuffer()` is a thin wrapper that retrieves the page from a buffer and delegates to `GinInitPage()` with the buffer's actual page size.

`GinInitMetabuffer()` is more involved. After calling `GinInitPage()` with the `GIN_META` flag, it zeroes the `GinMetaPageData` structure at the page's content area and explicitly sets `pd_lower` to point just past the end of the metadata struct. This step is not optional: `xlog.c` uses `pd_lower` to determine which part of the page contains live data when it compresses a full-page image. Without the correct `pd_lower`, the metapage's metadata would be silently discarded during WAL compression. Replay would then restore an empty metapage. The same `pd_lower` adjustment is repeated in `GinUpdateStats()` for the same reason. The comment in the source notes that pre-v11 instances might contain the wrong value after `pg_upgrade`.

### Page Flags Reference

| Flag | Meaning |
|---|---|
| `GIN_DATA` | Posting-tree page (data tree), not an entry-tree page |
| `GIN_LEAF` | Leaf page at the bottom of a B-tree |
| `GIN_DELETED` | Page has been deleted and is available for recycling |
| `GIN_META` | The single metapage (always block 0) |
| `GIN_LIST` | Pending-list page (fast-insert buffer) |
| `GIN_LIST_FULLROW` | Last pending-list page for a heap tuple fits one full row |
| `GIN_INCOMPLETE_SPLIT` | Split occurred but parent not yet updated |
| `GIN_COMPRESSED` | Data-leaf page uses varbyte-compressed posting lists (9.4+) |

## Index Statistics and the Planner

The metapage holds four counters that the query planner reads via `gincostestimate` to estimate how many pages a GIN scan will touch: `nTotalPages`, `nEntryPages`, `nDataPages`, and `nEntries`. `nTotalPages` is the total page count of the index. `nEntryPages` counts pages in the entry B-tree. `nDataPages` counts pages in all posting trees. `nEntries` counts distinct entry-tree leaf tuples (i.e. distinct indexed keys).

`GinGetStats()` (`ginutil.c`) acquires a shared lock on the metapage and copies these counters into a `GinStatsData` struct. `GinUpdateStats()` acquires an exclusive lock and writes a new `GinStatsData` back. The counters are only as current as the last `VACUUM`. `GinGetStats()` itself notes that `nPendingPages` is always live (it is updated by fast-insert), but the four structural counters lag behind until cleanup runs. This means the planner's cost estimates for a heavily-written GIN index can be stale between vacuums, which is one reason GIN indexes benefit from regular [[subsystems/background/autovacuum]] runs.

When `GinUpdateStats()` writes to the metapage and the relation needs WAL (`RelationNeedsWAL()`), it emits an `XLOG_GIN_UPDATE_META_PAGE` record. During initial index builds (`is_build = true`) WAL emission is suppressed because the build uses a bulk WAL strategy outside the normal per-page path.

## Key Comparison and B-tree Ordering

The entry tree is a B-tree that orders all indexed keys. For a single-column GIN index the sort key is just the key datum compared through the opclass's `GIN_COMPARE_PROC` support function (or the type's default btree comparator if none is supplied). For a multi-column index a two-level ordering applies: the column number (`attnum`) is the primary sort key, and the key value is secondary.

`ginCompareEntries()` (`ginutil.c`) handles comparison within one column. It first checks the `GinNullCategory` field: pages with different null categories compare by category code before touching the datum. All values within the same non-normal category are considered equal. Only `GIN_CAT_NORM_KEY` entries invoke the comparator function. This ordering guarantees that all null keys, placeholder-for-empty-item keys, and placeholder-for-null-item keys form contiguous bands in the tree below the real keys.

`ginCompareAttEntries()` wraps `ginCompareEntries()` and adds the column-number check at the front. It is used wherever the two items being compared might come from different index columns, such as binary search on internal non-leaf pages and during leaf-page position lookups (`entryLocateEntry()`, `entryLocateLeafEntry()`).

## Entry-Tree Tuple Layout

A leaf entry-tree tuple represents one key value and carries the associated posting data inline or as a pointer to a posting tree. `GinFormTuple()` (`ginentrypage.c`) constructs this tuple in three steps.

First it builds the standard index tuple with `index_form_tuple()`. For a single-column index the tuple has one attribute: the key datum (or a null if the category is not `GIN_CAT_NORM_KEY`). For multi-column indexes the tuple has two attributes: an `int16` column number followed by the key datum. This means single-column indexes are more compact — they omit the column-number prefix that multi-column indexes must carry.

Second, if the tuple contains a null attribute (`IndexTupleHasNulls()`), `GinFormTuple()` ensures the tuple is large enough to hold a one-byte `GinNullCategory` at the position computed by `GinCategoryOffset()`. This byte lives in the alignment padding that follows the index attribute area. This is why `GinFormTuple()` takes the maximum of the standard tuple size and the minimum size needed to hold the category byte before proceeding.

Third, the posting offset is stored in the `t_tid` field's block-number half (with `GIN_ITUP_COMPRESSED` flag always set to distinguish it from an actual block pointer). The posting count is stored in the offset-number half. When the posting data is too large to fit inline, the offset-number is set to `GIN_TREE_POSTING` (0xffff). The block number then carries the root block of a separate posting tree instead. The maximum item size (`GinMaxItemSize`) is sized to guarantee that at least three tuples fit on a page — the same lower bound that B-tree uses.

Non-leaf interior tuples are simpler: they hold only the key data (no inline posting list) and use `t_tid` as a downlink to a child page (`GinSetDownlink()`). `GinFormInteriorTuple()` constructs these by copying only the key portion of a leaf or non-leaf source tuple and replacing `t_tid` with the child block number.

## Leaf Page Insertion and Splits

When inserting a new entry-tree tuple, the B-tree machinery (`ginbtree.c`) first calls `entryBeginPlaceToPage()` to decide whether the tuple fits. If `entryIsEnoughSpace()` reports that the free space on the page (plus any space freed by a pending delete) is sufficient, the function returns `GPTP_INSERT`. `entryExecPlaceToPage()` then performs the actual `PageAddItem()` call inside a critical section. After the insertion, if WAL is needed, it appends the `ginxlogInsertEntry` header and the raw tuple bytes to the buffer's WAL data via `XLogRegisterBufData()`.

When there is not enough space, `entrySplitPage()` is called instead. It collects all existing tuples plus the new one into a temporary workspace and initialises fresh left and right page copies with `GinInitPage()`. It then distributes tuples to equalise total data size (not tuple count) between the two halves. The split point (`separator`) is chosen as the last tuple index on the left page once the cumulative size exceeds half the total. The function returns two temporary page images without modifying the original buffer. The caller applies both pages atomically after acquiring the necessary locks.

After a root split, `ginEntryFillRoot()` is called to populate a newly allocated root page. It takes the rightmost tuple from each of the two child pages (left and right) and forms interior tuples from them. It then inserts both into the empty root. `ginEntryFillRoot()` is also called directly from WAL replay (`ginxlog.c`), so it must not depend on state that is unavailable during recovery.

## WAL Record Taxonomy

GIN uses a single resource manager (`RM_GIN_ID`) with one dispatch function (`gin_redo()`) that switches on the record type. The nine record types cover every modification path:

| Record type | What it captures |
|---|---|
| `XLOG_GIN_INSERT` | A single tuple insertion into an entry-tree or data-tree page, with a delta payload that either replaces an entry or adds posting data; also used for internal-page downlink updates after a child split |
| `XLOG_GIN_SPLIT` | A page split in either the entry tree or the data tree; always carries full-page images of both new pages (and the new root if the split propagated to root) |
| `XLOG_GIN_CREATE_PTREE` | Creation of the first leaf page of a new posting tree, carrying the initial compressed posting list |
| `XLOG_GIN_UPDATE_META_PAGE` | An update to the metapage stats counters (`nTotalPages`, `nEntryPages`, `nDataPages`, `nEntries`), with optional appended tuples to the pending list's tail page or a pending-list tail extension |
| `XLOG_GIN_INSERT_LISTPAGE` | Insertion of tuples into a new pending-list (fast-insert buffer) page |
| `XLOG_GIN_DELETE_LISTPAGE` | Deletion of pending-list pages after cleanup has merged them into the main index, plus a metapage update |
| `XLOG_GIN_VACUUM_PAGE` | A vacuum pass over an entry-tree page; carries a full-page image because dead entries are removed en masse |
| `XLOG_GIN_VACUUM_DATA_LEAF_PAGE` | A vacuum pass over a posting-tree leaf page; carries a recompression delta (a `ginxlogRecompressDataLeaf` action sequence) rather than a full-page image, so only the changed segments need to be in the WAL record |
| `XLOG_GIN_DELETE_PAGE` | Deletion of an empty posting-tree leaf page, updating its left sibling's rightlink and removing the downlink from the parent |

### Full-Page Images and the Split Record

GIN follows standard PostgreSQL WAL discipline: on the first modification of a page after a checkpoint, the buffer manager includes a full-page image (FPI) in the WAL record automatically when `REGBUF_STANDARD` is passed to `XLogRegisterBuffer()`. This protects against torn pages that can result from partial writes during a crash.

The split record is an exception: it mandates full-page images explicitly. The `ginRedoSplit()` redo function calls `XLogReadBufferForRedo()` and explicitly errors out if `BLK_RESTORED` is not returned for both the left and right pages. This is intentional. Split records are written atomically for both pages. During replay, both pages are always re-initialised from their stored images, rather than being patched incrementally. The root page update after a root split is handled the same way.

### Replay Logic

`gin_redo()` runs inside a private `MemoryContext` (`opCtx`) that is reset after each record. This prevents the accumulating allocations from individual replay functions — in particular `ginRedoRecompress()`'s `palloc` for the tail copy — from building up over thousands of records during recovery.

The metapage redo functions (`ginRedoUpdateMetapage()`, `ginRedoDeleteListPages()`) always use `XLogInitBufferForRedo()` rather than `XLogReadBufferForRedo()`. The distinction matters. `XLogInitBufferForRedo()` reinitialises the buffer unconditionally and does not check the page LSN. `XLogReadBufferForRedo()`, by contrast, skips the redo if the buffer's LSN is already past the record. Metapage updates use full reinitialisation because the metapage must always exactly match the WAL-recorded state — the comment in `ginRedoUpdateMetapage()` explicitly calls this out as equivalent to a full-page image.

### Data-Leaf Recompression

`XLOG_GIN_VACUUM_DATA_LEAF_PAGE` and the data-page path of `XLOG_GIN_INSERT` share the `ginRedoRecompress()` helper, which replays a sequence of segment-level actions (`GIN_SEGMENT_INSERT`, `GIN_SEGMENT_DELETE`, `GIN_SEGMENT_REPLACE`, `GIN_SEGMENT_ADDITEMS`) against the varbyte-compressed posting list on a data leaf page. To avoid in-place modifications that would corrupt unprocessed segments still being read, `ginRedoRecompress()` copies the unprocessed tail of the page into a palloc'd buffer before making any writes. The source then reads from the copy while writing the new content into the original page location.

## Observability via pg_waldump

Because GIN uses a single resource manager with well-named record types, `pg_waldump` output for a GIN-heavy workload is readable. Frequent `XLOG_GIN_INSERT` records indicate normal leaf insertions. Bursts of `XLOG_GIN_SPLIT` records indicate a phase where the entry tree is growing and splitting. `XLOG_GIN_UPDATE_META_PAGE` and `XLOG_GIN_DELETE_LISTPAGE` records indicate fast-insert pending-list flushes. High WAL volume from GIN is most commonly caused by repeated small inserts to a table with a full-text GIN index when `fastupdate` is enabled. Each pending-list flush writes a new `XLOG_GIN_INSERT_LISTPAGE` record per page of buffered entries. Disabling `fastupdate` or tuning `gin_pending_list_limit` shifts the write pattern toward more frequent but smaller `XLOG_GIN_INSERT` records.

## Related Topics

- [[subsystems/indexes/gin]] — main GIN internals article: architecture, insert path, scan path, vacuum, and the fast-insert pending list
- [[subsystems/wal/overview|WAL internals]] — WAL record structure, resource managers, and full-page images
- [[subsystems/background/autovacuum]] — triggers GIN cleanup that flushes stats counters and emits `XLOG_GIN_DELETE_LISTPAGE` records
