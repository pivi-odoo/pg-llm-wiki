---
title: "GiST Index Build and WAL Recovery"
aliases:
  - GiST buffered build
  - GiST WAL records
  - gistbuildbuffers
  - gistxlog
tags:
  - theme/durability
source_files:
  - src/backend/access/gist/gistbuildbuffers.c
  - src/backend/access/gist/gistxlog.c
  - src/include/access/gist_private.h
  - src/include/access/gistxlog.h
symbols:
  - GISTBuildBuffers
  - GISTNodeBuffer
  - GISTNodeBufferPage
  - gistInitBuildBuffers
  - gistGetNodeBuffer
  - gistPushItupToNodeBuffer
  - gistPopItupFromNodeBuffer
  - gistRelocateBuildBuffersOnSplit
  - gistUnloadNodeBuffers
  - gistXLogSplit
  - gistXLogUpdate
  - gistXLogPageDelete
  - gistXLogPageReuse
  - gistXLogDelete
  - gist_redo
  - gistRedoPageSplitRecord
  - gistRedoPageUpdateRecord
---

GiST index creation and crash recovery are two distinct but interrelated problems. Building a GiST index naively — inserting each tuple top-down into the live tree — produces excessive random I/O as every insertion may touch pages at every level of a deep tree. The buffered build strategy solves this by staging tuples in per-node in-memory buffers and flushing them level-by-level in a single sweep. WAL recovery, in turn, must handle GiST's more complex split geometry. Unlike B-tree, a GiST split can scatter one page into many halves, and bounding-box adjustments propagate upward through the tree. This makes full-page images the only safe recovery strategy.

## Buffered Build

A naive GiST build issues one `gistdoinsert()` call per heap tuple. Each call traverses the full tree height using the opclass penalty function to choose which child to descend into, then writes the leaf page. For a large index this produces a random-I/O pattern proportional to the number of tuples multiplied by the tree height — the same problem that originally motivated the B-tree sort-and-build strategy. GiST cannot use a simple sorted bulk load because its opclass-defined key space has no total order that aligns with the tree structure. Buffering is therefore the practical alternative: the build accumulates enough tuples at each internal node before pushing them downward, so each page is visited only once per level sweep rather than once per tuple.

The buffered strategy is activated when the index is expected to exceed a threshold derived from `maintenance_work_mem`. The `buffering_mode` storage parameter in `GiSTOptions` can override this with `ON`, `OFF`, or `AUTO` (gist_private.h).

### GISTBuildBuffers and GISTNodeBuffer

The central state object is `GISTBuildBuffers` (gist_private.h):

| Field | Purpose |
|---|---|
| `pfile` | Temporary `BufFile` backing store for buffer pages swapped out of memory |
| `nFileBlocks` | Current size of the temporary file in BLCKSZ blocks |
| `freeBlocks` / `nFreeBlocks` | Freelist of reusable positions in the temporary file |
| `nodeBuffersTab` | Hash table mapping index `BlockNumber` to `GISTNodeBuffer` |
| `bufferEmptyingQueue` | List of buffers that have reached half-full and need draining |
| `buffersOnLevels` | Array of per-level buffer lists, used during the final emptying phase |
| `loadedBuffers` | Buffers that currently have their last page resident in memory |
| `levelStep` | Every `levelStep`-th level of the tree carries a buffer; leaf and root do not |
| `pagesPerBuffer` | Nominal maximum pages per node buffer before it is flushed |
| `rootlevel` | Height of the current root (= `maxLevel` passed at init) |

Each internal node that has a buffer is represented by a `GISTNodeBuffer`:

| Field | Purpose |
|---|---|
| `nodeBlocknum` | The index page this buffer represents (also the hash key) |
| `blocksCount` | Number of BLCKSZ pages currently owned by this buffer (both in-memory and on-disk) |
| `pageBlocknum` | Block number in `pfile` of the on-disk portion, or `InvalidBlockNumber` if fully in memory |
| `pageBuffer` | Pointer to the single in-memory page for this buffer, or NULL if swapped out |
| `queuedForEmptying` | Set once `BUFFER_HALF_FILLED` is true; prevents double-queueing |
| `level` | Tree level (0 = leaf) |

The `LEVEL_HAS_BUFFERS` macro (gist_private.h) defines exactly which levels carry buffers: every level that is a nonzero multiple of `levelStep` and not the root. This creates a coarse-grained set of "staging layers" in the tree.

### Buffer Page Layout

In-memory storage for each node buffer is a linked list of `GISTNodeBufferPage` structs, each exactly BLCKSZ bytes. Tuples are packed from the beginning of the `tupledata` flexible array member, growing upward; `freespace` tracks how much room remains. The `prev` field is a block number in `pfile` pointing to the previous page in the chain, forming a singly-linked list on disk. When a new tuple does not fit in the current page (`PAGE_NO_SPACE`), the in-memory page is written to a free block in `pfile`. A new in-memory page is allocated with `prev` set to that block number. `blocksCount` is then incremented (`gistPushItupToNodeBuffer()`, gistbuildbuffers.c).

Only the last page of each buffer is kept in memory at one time. `gistLoadNodeBuffer()` reads it from `pfile` into `pageBuffer`; `gistUnloadNodeBuffer()` writes it back. The `loadedBuffers` array tracks which buffers have a resident page. This lets `gistUnloadNodeBuffers()` flush them all at once when memory pressure requires it.

### Flushing: Top-Down Level Sweeps

When a node buffer is half-full it is appended to `bufferEmptyingQueue`. The build loop drains this queue by popping tuples out of the buffer with `gistPopItupFromNodeBuffer()` and re-inserting them one level lower via `gistProcessItup()`. Because `levelStep` determines which levels have buffers, a tuple may bypass several levels before landing in the next buffered level or, ultimately, in a leaf page. This top-down sweep means each internal page is locked and written only when the buffer above it is being drained, rather than once per inserted tuple.

The final phase — after all heap tuples have been pushed into the root-level buffer — iterates over `buffersOnLevels` from the topmost buffered level down to level 1, draining every buffer in sequence. New buffers created by page splits during this phase are prepended to the level list (`gistGetNodeBuffer()`, gistbuildbuffers.c). This way, freshly-split pages are flushed before pre-existing ones at the same level, while they are still likely to be in the shared buffer cache.

### Split Handling During Build

When a page splits while a buffer is being drained, the tuples already in that buffer must be redistributed across the split halves. `gistRelocateBuildBuffersOnSplit()` (gistbuildbuffers.c) handles this: it copies the old buffer into a temporary `GISTNodeBuffer` (marked `isTemp = true`), resets the original buffer for the left half, and creates new buffers for the right halves. Each tuple from the old buffer is then re-inserted into whichever new buffer produces the lowest penalty for the opclass key — exactly the same multi-column penalty logic used by `gistchoose()`. After redistribution, `gistgetadjusted()` is called to widen any downlink that now needs to cover a newly-assigned tuple, keeping the bounding boxes in the parent consistent.

## WAL Records

### Record Types

GiST WAL uses six record types, dispatched in `gist_redo()` (gistxlog.c):

| Opcode | Constant | Purpose |
|---|---|---|
| `0x00` | `XLOG_GIST_PAGE_UPDATE` | Insert and/or delete tuples on a single page; also clears `F_FOLLOW_RIGHT` on a child page when completing a split |
| `0x10` | `XLOG_GIST_DELETE` | Remove dead leaf tuples; separated from page update to carry `snapshotConflictHorizon` for Hot Standby |
| `0x20` | `XLOG_GIST_PAGE_REUSE` | Notify standbys that a recycled [[subsystems/storage/fsm|FSM]] page is about to be reused; no page content, conflict-resolution only |
| `0x30` | `XLOG_GIST_PAGE_SPLIT` | Distribute tuples across all split-product pages |
| `0x60` | `XLOG_GIST_PAGE_DELETE` | Mark a leaf page as deleted and remove its downlink from the parent |
| `0x70` | `XLOG_GIST_ASSIGN_LSN` | No-op record that advances the LSN; used by unlogged GiST indexes via `gistGetFakeLSN()` |

### Split Record Format and Full-Page Images

A GiST split can produce more than two result pages. The `gistxlogPageSplit` record (gistxlog.h) contains `npage` — the number of split products — and embeds one full page image per product via `REGBUF_WILL_INIT`. Each page image carries the complete set of index tuples assigned to that half, the rightlink chain connecting split products, and the `F_FOLLOW_RIGHT` flag state.

Full images are required rather than logical deltas for two reasons. First, the set of pages produced by a split and the assignment of tuples to them depends on the opclass `picksplit` function, which is not deterministic from the WAL record alone. Second, bounding-box updates must propagate upward through potentially many internal levels; any intermediate state left on disk by a crash would require the recovery code to re-run the opclass union function to reconstruct valid key ranges. Storing full images makes recovery purely physical: `gistRedoPageSplitRecord()` calls `XLogInitBufferForRedo()` for each split product, wipes the page with `GISTInitBuffer()`, and repopulates it from the embedded tuple list (gistxlog.c).

The left-child page (the one that was being split) is registered separately as block 0 with `REGBUF_STANDARD` — a conditional full-page image. This lets the `F_FOLLOW_RIGHT` flag be cleared on it even if the page was already checkpointed before the split completed.

### The F_FOLLOW_RIGHT Mechanism

GiST signals an incomplete split by setting `F_FOLLOW_RIGHT` on the left half of a not-yet-linked split. Any concurrent scan that encounters this flag must follow the rightlink chain to find all tuples, even without a parent downlink. Recovery clears the flag via `gistRedoClearFollowRight()`, which sets the page's NSN to the record's LSN and calls `GistClearFollowRight()`. This is done while the lock on the parent page (or the first split product) is still held. This ensures that no Hot Standby query sees the intermediate state without the flag.

Before PostgreSQL 9.1, GiST used "invalid tuples" in parent pages to mark incomplete splits. The current mechanism — `F_FOLLOW_RIGHT` plus NSN-based detection during normal inserts — replaced that approach. It no longer requires invalid tuples on non-upgraded indexes.

### Bounding-Box Propagation in Recovery

When a `PAGE_UPDATE` record inserts a downlink after a split, the downlink tuple carries the union key for the new child page. Recovery replays this by calling `PageIndexTupleOverwrite()` or `PageAddItem()` in `gistRedoPageUpdateRecord()` with the exact tuple bytes from the WAL record. No recomputation of the union is necessary on the replica — the primary has already stored the correct bounding box in the record. If the downlink insertion itself triggered further splits, each of those splits is a separate `PAGE_SPLIT` record in the same WAL sequence with its own full images. As a result, recovery replays the entire propagation chain deterministically.

The `XLOG_GIST_DELETE` record is kept distinct from `PAGE_UPDATE` because deleting dead index tuples during an insertion sweep creates a snapshot conflict point for Hot Standby. The `snapshotConflictHorizon` field lets `gistRedoDeleteRecord()` call `ResolveRecoveryConflictWithSnapshot()` before modifying the page (gistxlog.c).

## Related Topics

- [[subsystems/indexes/gist]] — GiST index structure, insertion, and search
- [[subsystems/wal/overview]] — WAL record format, replay infrastructure, and checkpoint interaction
- [[subsystems/memory/contexts]] — memory context lifecycle used for build buffer allocation
- [[subsystems/executor/work-mem-and-spill]] — `maintenance_work_mem` budget that governs when buffered build is activated
