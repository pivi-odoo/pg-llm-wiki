---
title: "Local Buffers (Temp Table I/O)"
aliases:
  - local buffer manager
  - temp buffers
  - temp_buffers
tags:
  - theme/caching
source_files:
  - src/backend/storage/buffer/localbuf.c
symbols:
  - LocalBufferAlloc
  - LocalBufferDescriptors
  - LocalBufHash
  - LocalBufferLookupEnt
  - GetLocalVictimBuffer
  - InitLocalBuffers
  - PinLocalBuffer
  - UnpinLocalBuffer
  - DropRelationLocalBuffers
  - ExtendBufferedRelLocal
---

Temporary tables in PostgreSQL bypass the shared buffer pool entirely and instead use a per-backend buffer cache called the local buffer manager. Temp table data is private to a single session, and it never needs to survive a crash. As a result, it requires no WAL logging, no shared-memory locking, and no coordination with other backends. A dedicated, lock-free cache can therefore serve it far more cheaply than routing it through the [[subsystems/storage/buffer-manager|shared buffer manager]].

## Isolation from shared buffers

The shared buffer manager (`bufmgr.c`) exists to let every backend share the same view of on-disk data, which demands a partitioned hash table protected by [[subsystems/locking/lwlocks|LWLocks]], atomic compare-and-swap operations on buffer headers, and cooperation with the WAL machinery. None of that applies to a temp table. One backend owns its pages exclusively, the session bounds their lifetime, and crashes never need recovery.

The local buffer manager exploits all of these properties. The lookup hash table (`LocalBufHash`) is a plain process-local `HTAB` — no locking required. Buffer state updates use direct writes rather than atomic CAS loops. The local buffer manager never sets a `BM_IO_IN_PROGRESS` flag during reads, and it never generates a WAL record. The savings are significant at high concurrency precisely because they eliminate cache-line bouncing on the shared descriptor array.

Buffer numbers for local buffers are negative integers (starting at `-1`). The macro `BufferIsLocal(buffer)` tests `buffer < 0`. The `BufferDesc` for local buffer `n` lives in the backend-private `LocalBufferDescriptors` array. `InitLocalBuffers()` sets its `buf_id` to `-n - 2`, so that `BufferDescriptorGetBuffer()` recovers the correct negative buffer number (`localbuf.c`).

## Lazy initialisation and memory layout

PostgreSQL does not set up the local buffer infrastructure at backend startup. The backend calls `InitLocalBuffers()` (`localbuf.c`) on the first access to any temporary table. It allocates three parallel arrays from process memory:

| Array | Type | Purpose |
|---|---|---|
| `LocalBufferDescriptors` | `BufferDesc[]` | Metadata for each local buffer frame |
| `LocalBufferBlockPointers` | `Block[]` | Pointers to the actual 8KB page data |
| `LocalRefCount` | `int32[]` | Per-frame pin counts (process-local, no atomics) |

The `temp_buffers` GUC controls the size and defaults to 1024 buffers (8MB). Once a session accesses any temp table, `temp_buffers` becomes immutable for that session (`check_temp_buffers()`, `localbuf.c`).

The local buffer manager allocates the actual page memory lazily, one frame at a time, the first time it selects that frame as a victim. `GetLocalBufferStorage()` (`localbuf.c`) batches these allocations to reduce overhead from the memory manager: it starts with a 16-buffer chunk and doubles with each subsequent request, capped at the remaining unallocated count. All allocations are I/O-aligned and live in a dedicated `LocalBufferContext` under `TopMemoryContext`, making them easy to identify in `MemoryContextStats` output.

## Clock sweep eviction

When `LocalBufferAlloc()` needs a new frame, `GetLocalVictimBuffer()` (`localbuf.c`) selects one using the same clock sweep algorithm as the shared buffer manager's `StrategyGetBuffer()`. A single backend-local cursor (`nextFreeLocalBufId`) advances through `LocalBufferDescriptors`. For each candidate:

- If `LocalRefCount[id] > 0` the frame is pinned — skip it.
- If the usage count is non-zero, decrement it and continue. The scan counter resets, so the clock sweep covers the full pool again.
- If the usage count is zero and the frame is unpinned, `GetLocalVictimBuffer()` selects it.

If the victim frame is dirty (`BM_DIRTY`), `GetLocalVictimBuffer()` writes its contents to the underlying temp file via `smgrwrite()` before reusing the frame. There is no background writer for local buffers — eviction writes always happen synchronously in the foreground backend. `localbuf.c` tracks I/O statistics under `IOOBJECT_TEMP_RELATION` (`pgstat_count_io_op_time()`), which surfaces in `pg_stat_io`.

If all `NLocBuffer` frames are pinned simultaneously, `GetLocalVictimBuffer()` raises `ERROR: no empty local buffer available`. This is the local-buffer equivalent of running out of [[subsystems/executor/work-mem-and-spill|work_mem]] — a signal to increase `temp_buffers`.

## Pin and unpin

`PinLocalBuffer()` and `UnpinLocalBuffer()` (`localbuf.c`) manage the per-frame `LocalRefCount`. Because this is process-local, there is no atomic operation and no lock — a plain integer increment and decrement suffice. When a buffer transitions from unpinned to pinned (`LocalRefCount` goes from 0 to 1), `PinLocalBuffer()` optionally increments the usage count and updates `NLocalPinnedBuffers`. This lets `LimitAdditionalLocalPins()` bound bulk pin operations. Both pin and unpin register/deregister the buffer with the [[subsystems/memory/resource-owner|ResourceOwner]] so that error cleanup can detect and report leaked pins.

## Lookup path

`LocalBufferAlloc()` (`localbuf.c`) is the entry point for all local buffer lookups and mirrors the interface of `BufferAlloc()` in `bufmgr.c`. It constructs a `BufferTag` for the requested (relation, fork, block) triple and searches `LocalBufHash` with `HASH_FIND`. A hit returns the existing frame immediately after pinning it. A miss calls `GetLocalVictimBuffer()` to obtain a frame, inserts a new entry into `LocalBufHash`, and tags the frame. It then returns the frame with `*foundPtr = false`, signalling the caller that a disk read is needed.

The lookup hash table entry type is `LocalBufferLookupEnt` — identical in structure to `BufferLookupEnt` in `buf_table.c` (which serves the shared pool), except that it lives in process-local memory and requires no partition locking:

```
typedef struct {
    BufferTag  key;   /* identifies the disk page */
    int        id;    /* index into LocalBufferDescriptors */
} LocalBufferLookupEnt;
```

## Relation drop and session cleanup

When a temporary relation is dropped or truncated, `DropRelationLocalBuffers()` (or `DropRelationAllLocalBuffers()`) scans the descriptor array and discards dirty pages without writing them. It then removes entries from `LocalBufHash` and marks the frames invalid. It can discard dirty data silently because temp tables are not durable — there is no scenario in which an uncommitted write to a temp table needs to be recovered. The functions assert that no frame belonging to the dropped relation is still pinned, since a live pin would indicate a bug in the calling code.

At backend exit, `AtProcExit_LocalBuffers()` calls `CheckForLocalBufferLeaks()` to assert (in debug builds) that no pins remain, mirroring the analogous check in the shared buffer manager.

## Parallel workers cannot use local buffers

The postmaster forks parallel workers from the leader process, but they do not inherit its local buffer state. `InitLocalBuffers()` raises an error if called from a parallel worker (`IsParallelWorker()` check, `localbuf.c`). As a result, queries that access temporary tables cannot use parallelism — the executor detects temp table access and disables parallel plans. This is a fundamental architectural constraint, not a configuration limitation.

## Related Topics

- [[subsystems/storage/buffer-manager|Buffer Manager]] — the shared buffer pool that handles all non-temp relation I/O
- [[subsystems/memory/resource-owner|ResourceOwner]] — tracks buffer pins for cleanup on error
- [[subsystems/executor/work-mem-and-spill|work_mem]] — the other per-backend memory budget that limits query execution
