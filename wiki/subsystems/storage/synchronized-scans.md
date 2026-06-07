---
title: "Synchronized Sequential Scans"
aliases:
  - synchronized scans
  - syncscan
  - sync scan
  - synchronize_seqscans
tags:
  - theme/caching
  - theme/parallelism
source_files:
  - src/backend/access/common/syncscan.c
  - src/include/access/syncscan.h
  - src/backend/access/heap/heapam.c
  - src/backend/access/table/tableam.c
symbols:
  - ss_get_location
  - ss_report_location
  - ss_search
  - ss_scan_locations_t
  - ss_lru_item_t
  - SyncScanShmemInit
  - table_block_parallelscan_startblock_init
  - table_block_parallelscan_nextpage
  - ParallelBlockTableScanDesc
  - synchronize_seqscans
---

When multiple backends scan the same large table around the same time, synchronized sequential scans coordinate their starting position. This makes all of them read the table in the same order. They share the same stream of pages already loaded into shared buffers and the OS page cache. Without this coordination, two concurrent full-table scans would each read every page independently, doubling I/O. With coordination, the second backend joins the first and piggybacks on cache warmth already established. Each page then comes from storage only once. The starting position is the key variable. A sequential scan reads pages in block-number order — block 0, block 1, … block N−1. Suppose backend A is halfway through a large table when backend B starts at block 0. The two scans then read completely different pages simultaneously. They never share any cache benefit. If instead B starts at block N/2, where A currently is, both backends read the same pages at roughly the same time. The first to arrive pays the I/O cost. The second finds the page already in shared buffers. The leader must wait for I/O; the follower does not. This creates a self-reinforcing synchronization effect. The follower catches up, and the two scans advance together. B still needs to visit all blocks eventually. It wraps around from block N−1 back to block 0 and continues to block N/2−1. This way, the scan covers the full table and misses no rows. This circular traversal is the reason synchronized scans are safe. A sequential scan has no semantic requirement to start at block 0. It only requires that the scan visit every live block exactly once, within a single scan invocation.

## The shared LRU table in shared memory

The implementation is a small fixed-size doubly-linked LRU list in shared memory. `SyncScanShmemInit()` (`syncscan.c`) allocates it during postmaster startup. It contains `SYNC_SCAN_NELEM` entries (20 by default), each holding a `(RelFileLocator, BlockNumber)` pair — the identity of a relation and the last-reported scan position within it. A single `LWLock` (`SyncScanLock`) protects the list.

```c
typedef struct ss_scan_location_t {
    RelFileLocator relfilelocator;
    BlockNumber    location;
} ss_scan_location_t;
```

With only 20 slots for all concurrent scans system-wide, the design relies on the realistic observation that very few large sequential scans run simultaneously. The LRU list evicts the tail entry when a new relation needs a slot. The list moves an MRU hit — a relation that is already being scanned — to the front, so new entries never accidentally displace active scans.

A new scan calls `ss_get_location(rel, relnblocks)` (`syncscan.c`) when it begins. The function acquires `SyncScanLock` exclusively and looks up the relation's entry. It then returns the recorded block number. If the table has been VACUUM-truncated since the position was saved (i.e. the recorded block number is now out of range), the function returns 0 as a fallback. This block number becomes the scan's `rs_startblock`.

`ss_report_location(rel, location)` updates the shared entry as the scan progresses. To keep lock contention low, `ss_report_location()` batches updates: the function only writes to shared memory every `SYNC_SCAN_REPORT_INTERVAL` pages (128 KB worth of pages by default). It uses `LWLockConditionalAcquire` — if the lock is contested, it skips the update rather than blocking. Missing a few position reports is acceptable. A follower that starts slightly behind the current leader will still catch up quickly, once it is reading nearby pages.

## When synchronized scans are disabled

Not every sequential scan participates. The gate is checked in `initscan()` (`heapam.c`) during scan initialization:

- **Temp tables** use local buffers (`RelationUsesLocalBuffers()`), which are private to the backend. Sharing a position across backends for a relation that only one backend can see makes no sense.
- **Small tables** — those with fewer than `NBuffers / 4` blocks — are below the threshold where I/O sharing is worth the coordination overhead. Small tables fit in the buffer pool repeatedly anyway, so the first scan warms the cache for all subsequent ones regardless of starting position.
- **The `synchronize_seqscans` GUC** (default `on`) provides an escape hatch. Setting it to `off` forces all sequential scans to start at block 0, useful for benchmarking or regression testing when deterministic I/O ordering is required.
- **Backward scans** (e.g. a cursor using `BACKWARD`) suppress syncscan reporting because a backward traversal's position is not meaningful as a forward starting point.
- **Rescans** (restarting a scan without closing it, as a cursor rewind does) preserve the startblock from the first pass rather than re-querying the shared position, to avoid surprising result ordering for repeated fetches.

## Connection to parallel query

[[subsystems/executor/parallel|Parallel query]] introduces a different but related mechanism for coordinating scan positions across workers. In a parallel sequential scan, all workers share a single `ParallelBlockTableScanDesc` in dynamic shared memory (DSM). This descriptor contains a `phs_startblock` (the starting block chosen once for the entire parallel scan) and an atomic counter `phs_nallocated` that workers atomically increment to claim non-overlapping chunks of pages.

`table_block_parallelscan_initialize()` (`tableam.c`) initializes the parallel descriptor. It applies the same `synchronize_seqscans && !RelationUsesLocalBuffers(rel) && nblocks > NBuffers / 4` gate. When syncscan is active, `table_block_parallelscan_startblock_init()` calls `ss_get_location()` to determine `phs_startblock` — this call happens once under a spinlock. Every worker uses the result. Subsequent block allocation proceeds through the atomic counter rather than through the shared LRU table. `table_block_parallelscan_nextpage()` calls `ss_report_location()` as it claims pages, so other non-parallel scans that start later can still find a useful position hint.

The distinction is important: in a non-parallel scan, each backend independently queries and updates the shared LRU table. In a parallel scan, all workers share a single starting point, stored in DSM. Only the page-claiming path (not the workers individually) updates the LRU table. Workers never call `ss_get_location()` themselves. The parallel descriptor dictates their starting position.

## I/O and buffer strategy interaction

Synchronized scans go hand in hand with the bulk-read buffer strategy (`BAS_BULKREAD`). The same size threshold (`nblocks > NBuffers / 4`) activates both. The bulk-read strategy uses a private ring buffer within the shared buffer pool rather than competing for the general LRU pool, which prevents a single large sequential scan from displacing working-set pages used by OLTP queries. Two synchronized scans sharing a ring is precisely the design intent. They read the same pages at roughly the same time, within a ring-sized window. Each page enters shared buffers once. Both backends use it. Then the ring evicts it.

PostgreSQL deliberately sets `SYNC_SCAN_REPORT_INTERVAL` smaller than the ring size. If reporting happened less frequently than the ring size, a new scan might join at a position whose pages have already left the ring, eliminating the cache benefit. At 128 KB intervals, position reports are frequent enough that a joining scan starts within one ring revolution of the current position, maximizing the chance that pages are still resident.

## Related Topics

- [[subsystems/executor/parallel|parallel query]]
- [[subsystems/storage/buffer-manager|buffer manager]]
- [[subsystems/executor/seq-scan|sequential scan]]
