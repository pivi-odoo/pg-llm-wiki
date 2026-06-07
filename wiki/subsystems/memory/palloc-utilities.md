---
title: "palloc Utilities: Aligned Allocation, Memory Debugging, and Interruptible Sort"
aliases:
  - palloc_aligned
  - aligned allocation
  - CLOBBER_FREED_MEMORY
  - qsort_interruptible
  - memory debugging
source_files:
  - src/backend/utils/mmgr/alignedalloc.c
  - src/backend/utils/mmgr/memdebug.c
  - src/backend/utils/sort/qsort_interruptible.c
  - src/backend/utils/mmgr/mcxt.c
  - src/include/utils/memdebug.h
  - src/include/utils/memutils_internal.h
  - src/include/lib/sort_template.h
symbols:
  - palloc_aligned
  - MemoryContextAllocAligned
  - AlignedAllocFree
  - AlignedAllocRealloc
  - AlignedAllocGetChunkContext
  - AlignedAllocGetChunkSpace
  - wipe_mem
  - randomize_mem
  - set_sentinel
  - sentinel_ok
  - qsort_interruptible
  - PallocAlignedExtraBytes
---

PostgreSQL's allocator exposes three optional facilities that sit on top of the core [[subsystems/memory/contexts|memory context]] infrastructure: `palloc_aligned()` for buffers that require stricter alignment than the standard guarantee, a set of compile-time memory debugging options that expose use-after-free and uninitialised-read bugs, and `qsort_interruptible()` for sorts that must honour cancellation requests. Each is a targeted addition to the baseline allocator rather than a replacement for it.

## Over-allocation and the aligned redirect chunk

Standard `palloc()` guarantees alignment to `MAXIMUM_ALIGNOF` (8 bytes on most platforms). Code that uses SIMD intrinsics, certain cryptographic primitives, or direct I/O requires 16-, 32-, or 64-byte alignment. `palloc_aligned(size, alignto, flags)` (`mcxt.c`) satisfies this by asking the underlying context for more memory than needed and then scanning forward to the first address that satisfies the alignment constraint.

The `PallocAlignedExtraBytes(alignto)` macro (`memutils_internal.h`) computes the extra bytes requested. It expands to `(alignto) + (sizeof(MemoryChunk) - MAXIMUM_ALIGNOF)`. The subtraction of `MAXIMUM_ALIGNOF` reflects the fact that the raw allocation is already aligned to that boundary. The addition of `sizeof(MemoryChunk)` reserves space for a small *redirect chunk header* that sits immediately before the aligned pointer.

This redirect header is the key to making `pfree()` and `repalloc()` work transparently. `palloc_aligned()` places a `MemoryChunk` with type `MCTX_ALIGNED_REDIRECT_ID` just before the returned aligned address. It repurposes the block-offset field — normally used to locate the owning block — to store the pointer back to the original unaligned allocation. `AlignedAllocFree()` (`alignedalloc.c`) reads this redirect to recover the underlying chunk. It then calls `pfree()` on it. `AlignedAllocRealloc()` similarly chases the redirect. It allocates a fresh aligned chunk of the new size. It copies the data. It frees the original. `palloc_aligned()` preserves the alignment value in the redirect chunk's `value` field, so that a reallocation can honour the same boundary.

Because `palloc_aligned()` builds on `MemoryContextAllocExtended()`, it cannot work with the Slab allocator. Slab only dispenses chunks of the fixed size it was created with. All other context types (aset, generation, bump) work because they can satisfy over-sized requests.

Real callers include cache-line-aligned `CatCache` structs (`catcache.c`), BLCKSZ-aligned zero-fill buffers used by the relation extension path (`md.c`), and WAL `GenericXLogState` buffers (`generic_xlog.c`).

## Compile-time memory debugging

`memdebug.c` and `memdebug.h` provide three independent debug facilities, each controlled by a compile-time symbol. None of these are active in production builds.

**CLOBBER_FREED_MEMORY** causes `pfree()` to fill the freed region with `0x7F` bytes via `wipe_mem()` (`memdebug.h`) before returning memory to the context. Code that dereferences a freed pointer will read `0x7F7F7F7F...` rather than plausible leftover data. This makes use-after-free bugs immediately visible in test runs or under a debugger. The Valgrind annotations around `wipe_mem()` mark the region temporarily `UNDEFINED` during the fill and `NOACCESS` afterwards. So Valgrind still catches accesses even when `CLOBBER_FREED_MEMORY` is not set.

**RANDOMIZE_ALLOCATED_MEMORY** fills newly allocated memory with a deterministic pseudo-random sequence via `randomize_mem()` (`memdebug.c`). The sequence cycles through values 1–251 (a prime-length period chosen to make two same-size allocations start with different content). This catches callers that read from a palloc'd buffer before writing to it: they will observe the pseudo-random pattern rather than whatever the previous tenant left behind. The function temporarily marks the region `UNDEFINED` so that Valgrind reports reads of the randomised bytes as uninitialised-read errors.

**MEMORY_CONTEXT_CHECKING** places a sentinel byte (`0x7E`) immediately after the requested allocation region. It verifies the sentinel at `pfree()` time via `set_sentinel()` and `sentinel_ok()` (`memdebug.h`). Because most allocators round request sizes up to a power of two, there is often slack space between the end of the requested region and the true chunk boundary. A write that spills one byte past the end will hit the sentinel and trigger a `WARNING`. The aligned allocator also participates: `AlignedAllocFree()` checks the sentinel on the aligned pointer before chasing the redirect and freeing the underlying chunk (`alignedalloc.c`).

## Interruptible sort

`qsort_interruptible()` (`qsort_interruptible.c`) is a variant of `qsort_arg()` that calls `CHECK_FOR_INTERRUPTS()` at recursion boundaries during the sort. `lib/sort_template.h` generates it with the `ST_CHECK_FOR_INTERRUPTS` flag set. This flag causes the template to emit `DO_CHECK_FOR_INTERRUPTS()` calls at the start of the recursive partitioning steps (lines 311, 325, 362, 372 of `sort_template.h`). The function signature is identical to `qsort_arg()`: element array, count, element size, comparator, and a caller-supplied argument passed through to the comparator.

The primary use case is statistics gathering. `ANALYZE` and the extended-statistics machinery sort large samples in-memory — operations that can run for many seconds on wide tables. Without interrupt checks, the sort would not honour a client cancel (SIGINT) until it returned. That could be long after the user expected the query to stop. `analyze.c`, `extended_stats.c`, `mcv.c`, `mvdistinct.c`, `array_typanalyze.c`, `rangetypes_typanalyze.c`, and `ts_typanalyze.c` use `qsort_interruptible()` wherever analysis sorts data that may be large enough to matter.

The interrupt check overhead is proportional to the number of recursive calls, which is O(n log n) in the number of elements. For short arrays, the cost is negligible. For large inputs, the periodic check introduces no visible throughput penalty. `CHECK_FOR_INTERRUPTS()` is a nearly-free test of a process-local flag in the common case where no signal has arrived.

## See also

- [[subsystems/memory/contexts|Memory contexts]]
- [[subsystems/memory/palloc|palloc and the aset allocator]]
- [[subsystems/executor/work-mem-and-spill|work_mem and sort spill]]
