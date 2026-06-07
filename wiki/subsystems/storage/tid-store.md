---
title: "TID Store"
aliases:
  - TidStore
  - tid store
  - TID bitmap
  - vacuum TID storage
tags:
  - theme/vacuum-and-maintenance
source_files:
  - src/backend/access/common/tidstore.c
  - src/include/access/tidstore.h
symbols:
  - TidStore
  - TidStoreIter
  - TidStoreIterResult
  - BlocktableEntry
  - TidStoreCreateLocal
  - TidStoreCreateShared
  - TidStoreAttach
  - TidStoreDetach
  - TidStoreDestroy
  - TidStoreSetBlockOffsets
  - TidStoreIsMember
  - TidStoreBeginIterate
  - TidStoreIterateNext
  - TidStoreEndIterate
  - TidStoreMemoryUsage
---

The TID store is an in-memory data structure introduced in PostgreSQL 17 for storing sets of tuple identifiers (TIDs, also called `ItemPointerData`). VACUUM's heap scan phase uses it internally to record the dead tuple locations that need to be removed. It replaces the previous `TidBitmap`-based approach with a more memory-efficient structure. The TID store supports both local (single-backend) and shared (parallel worker) modes.

The underlying storage is a radix tree (`src/lib/radixtree.h`) keyed by `BlockNumber`. Each value is a `BlocktableEntry`, a variable-length struct that encodes the set of offset numbers within that block as either a short list of raw offsets (for sparse pages) or a bitmap (for dense pages). This two-level design avoids storing a full bitmap for every block when only a handful of tuples are dead.

## Local vs Shared

`TidStoreCreateLocal()` creates a process-local TID store backed by a regular `MemoryContext`. This is the common case for single-process VACUUM. PostgreSQL sizes the [[subsystems/memory/contexts|memory context]] proportionally to `max_bytes` so that allocation blocks do not exceed 1/16 of the budget. This limits wasted space from over-allocation.

`TidStoreCreateShared()` creates a TID store in a DSA area, allowing parallel vacuum workers to attach to the same store and cooperate on dead-tuple collection. PostgreSQL sizes the DSA area so that its maximum segment is no larger than 1/8 of `max_bytes`. Other backends attach with `TidStoreAttach()`, which resolves the DSA handle and finds the shared radix tree. They detach with `TidStoreDetach()`, which releases backend-local state without destroying the underlying storage.

The `TidStoreIsShared()` macro checks whether the `area` pointer is non-null. All operations on shared stores use the `shared_ts_*` radix tree functions, while local stores use `local_ts_*`.

## The BlocktableEntry Layout

Each `BlocktableEntry` has a header and an optional bitmap array:

```
header:
  flags    (uint8)  — reserved for the radix tree's tag bit
  nwords   (int8)   — number of bitmap words, or 0 if using full_offsets
  full_offsets[N]   — up to N raw OffsetNumbers (used when nwords == 0)

words[]:  — bitmap of offset numbers, one bit per offset
```

When a page has few dead tuples (≤ `NUM_FULL_OFFSETS`, typically 3), `TidStoreSetBlockOffsets()` stores the offsets directly in the header to avoid allocating any bitmap. When there are more, it uses a compact bitmap instead. `TidStoreSetBlockOffsets()` chooses between the two representations based on the number of offsets being stored, filling the bitmap word-by-word by scanning the sorted input array.

The bitmap is indexed by offset number: offset `off` maps to `words[off / BITS_PER_BITMAPWORD]`, bit `off % BITS_PER_BITMAPWORD`. `TidStoreIsMember()` tests the appropriate bit (or scans the `full_offsets` array) to check whether a given TID is present.

## Iteration

`TidStoreBeginIterate()` creates a `TidStoreIter` that wraps a radix tree iterator. Each call to `TidStoreIterateNext()` fetches the next block's entry from the radix tree and unpacks it into a `TidStoreIterResult` containing the block number and a sorted array of offset numbers. `TidStoreIterateNext()` grows the offset array on demand (starting at twice `BITS_PER_BITMAPWORD` and doubling when needed). Iteration returns blocks in ascending block-number order because the radix tree iterates keys in order.

The caller is responsible for holding any necessary locks on the TID store during iteration. For shared stores, `TidStoreLockShare()` and `TidStoreUnlock()` wrap the radix tree's built-in locking, which uses the tranche mechanism.

## Memory Budget

The `max_bytes` parameter to `TidStoreCreateLocal()` and `TidStoreCreateShared()` is advisory: the store does not enforce the limit internally. The caller must poll `TidStoreMemoryUsage()` if it wants to cap usage and take action (such as flushing to disk or splitting the work). VACUUM uses this to decide whether to process more blocks or to trigger an index cleanup pass with the TIDs collected so far.

For local stores with `insert_only = true`, the memory context is a `BumpContext`. This is faster for sequential inserts because it never needs to free individual allocations.

## Related Topics

- [[code-paths/vacuum]] — VACUUM's heap scanning phase, the primary consumer of TID stores
- [[subsystems/storage/dsm-impl]] — how DSA areas work for the shared store variant
- [[subsystems/storage/access-common-utilities]] — other access-method-layer utilities
