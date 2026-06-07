---
title: "Free Page Manager"
aliases:
  - FreePageManager
  - FreePageManagerGet
  - FreePageManagerPut
source_files:
  - src/backend/utils/mmgr/freepage.c
  - src/include/utils/freepage.h
symbols:
  - FreePageManager
  - FreePageSpanLeader
  - FreePageBtree
  - FreePageBtreeHeader
  - FreePageBtreeLeafKey
  - FreePageBtreeInternalKey
  - FreePageManagerInitialize
  - FreePageManagerGet
  - FreePageManagerPut
  - FreePageManagerDump
---

The free page manager (`FreePageManager`) is a self-contained allocator that tracks which 4 KB pages within a fixed memory region are available for use. PostgreSQL built it specifically for [[subsystems/memory/dsa|DSA (Dynamic Shared Areas)]]. There, neither `malloc` nor `palloc` works, because all pointers must be relative to a segment base address. Standard allocators in PostgreSQL — [[subsystems/memory/contexts|memory contexts]] or `malloc` — assume a single process's virtual address space and use absolute pointers. But DSA maps segments at different virtual addresses in each backend. So any pointer stored inside the segment must be expressed as an offset from the segment's base. The free page manager is designed around this constraint. It stores relative pointers everywhere using the `relptr` infrastructure (`src/include/utils/relptr.h`). It finds its own location via `fpm_segment_base(fpm)`, which subtracts the offset stored in `fpm->self` from the manager's current address. The allocator operates entirely within the memory it manages, using free pages themselves to store its bookkeeping structures.

Allocation and deallocation operate in whole pages only — the higher-level DSA allocator is responsible for subdividing pages into smaller objects. This coarse granularity keeps the free page manager's data structures compact.

## Freelist structure

The `FreePageManager` struct contains 129 freelist heads (`FPM_NUM_FREELISTS`). Each of the first 128 lists holds spans of exactly that many pages. The 129th list (index 128) holds all spans larger than 128 pages. The allocator must search those entries linearly for best-fit, because their sizes vary. The allocator can satisfy spans of a precise size in O(1) by popping the head of the corresponding list.

A `FreePageSpanLeader` embedded in the first page of the span itself represents each free span. This header records the span's page count, a magic number for integrity checks, and doubly-linked prev/next pointers to other spans on the same freelist. Storing the header in the span itself means the manager needs no additional memory allocation — the free space holds its own catalog.

When `FreePageManagerPut` returns a span to the manager, the allocator checks whether the immediately preceding or following page range is already free. If so, the allocator merges the spans before placing the result on its freelist. This eager coalescing prevents fragmentation. Without it, fragmentation could make large contiguous allocations impossible even when sufficient free space exists.

## Adjacency tracking with an embedded B-tree

The freelist structure alone cannot efficiently locate the neighbors of an arbitrary span. To support coalescing, the manager maintains a B-tree ordered by page number. Every free span has an entry in the B-tree. The B-tree supports finding the predecessor and successor of any page range in O(log n).

The tree consists of `FreePageBtree` nodes, each exactly one page in size (4 KB). Leaf pages hold `FreePageBtreeLeafKey` entries recording `(first_page, npages)` pairs. Internal pages hold `FreePageBtreeInternalKey` entries with a low-bound key and a relative pointer to a child page. The number of entries per page is determined at compile time by how many fit in 4 KB minus the header:

- Leaf pages: `FPM_ITEMS_PER_LEAF_PAGE` entries
- Internal pages: `FPM_ITEMS_PER_INTERNAL_PAGE` entries

Because the B-tree itself consumes pages from the managed region, a bootstrapping tension exists. Recording a free span may require a new B-tree page. Allocating that page in turn changes what is free. The manager handles this carefully. When only one free span exists, the manager stores it directly in two scalar fields on the `FreePageManager` itself (`singleton_first_page`, `singleton_npages`). No B-tree is needed. The manager initializes the B-tree only when a second non-contiguous span appears.

The manager draws pages for B-tree nodes from the free spans available at that moment. If such a node is later no longer needed (because spans merged and the tree shrank), the manager places it on a separate recycle list (`btree_recycle`) rather than returning it directly to the managed pool. The cleanup phase that runs after every `FreePageManagerGet` and `FreePageManagerPut` reinserts recycled pages, but only if doing so would not itself require a B-tree split. Such a split would be an insertion that just consumes the recycled page again.

## Allocation policy

`FreePageManagerGet` uses a best-fit strategy. For a request of `n` pages, it checks freelist `n-1` first (which holds spans of exactly `n` pages). If that list is empty, it scans larger lists. For the oversized list (index 128), it walks the list to find the smallest span that satisfies the request. This avoids unnecessary waste.

When the allocator consumes a span larger than the request, it returns the remainder to the appropriate smaller freelist. It updates the span's B-tree entry in place. No new B-tree insertion is needed in this case, because the span's starting address does not change. The allocator only decrements its size field.

## Contiguous pages tracking

The `contiguous_pages` field on `FreePageManager` records the size of the largest currently available contiguous run. DSA consults this via the `fpm_largest(fpm)` macro to quickly determine whether a large allocation can possibly succeed before acquiring any lock. The manager maintains the field lazily. Operations that could reduce the maximum set `contiguous_pages_dirty`. `FreePageManagerUpdateLargest` recomputes the value by scanning the top freelists only when needed.

## Initialization and ownership

`FreePageManagerInitialize` sets up an empty manager in caller-provided memory. The caller must then use `FreePageManagerPut` to donate the managed pages before any `FreePageManagerGet` calls can succeed. In DSA's case, each DSM segment contains a `FreePageManager` near its start. When DSA creates the segment, it hands the manager all pages except those occupied by the segment header and the manager itself.

Because the `FreePageManager` must locate its own base address via `fpm->self`, the manager structure must reside within the memory region it manages, or at least at a fixed offset from it. DSA places it at the beginning of each segment so that `fpm_segment_base` correctly recovers the base address.

## Related Topics

- [[subsystems/memory/dsa|Dynamic Shared Area (DSA)]]
- [[subsystems/memory/contexts|Memory Contexts]]
- [[subsystems/storage/shared-memory|Shared Memory]]
