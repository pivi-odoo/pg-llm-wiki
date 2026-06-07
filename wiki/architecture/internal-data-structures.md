---
title: "Internal Data Structure Library (lib/)"
aliases:
  - lib/
  - ilist
  - rbtree
  - binaryheap
  - pairingheap
  - bloomfilter
  - dshash
source_files:
  - src/backend/lib/ilist.c
  - src/backend/lib/rbtree.c
  - src/backend/lib/binaryheap.c
  - src/backend/lib/pairingheap.c
  - src/backend/lib/bloomfilter.c
  - src/backend/lib/dshash.c
symbols:
  - dlist_head
  - slist_head
  - RBTree
  - binaryheap
  - pairingheap
  - bloom_filter
  - dshash_table
  - dshash_table_control
---

PostgreSQL's `src/backend/lib/` directory provides a set of general-purpose data structure implementations that the backend uses throughout. Rather than embedding ad hoc linked lists, trees, and heaps directly into every subsystem, these centralized implementations enforce consistent invariants and support debug-mode integrity checking. They also reduce duplicated logic across the codebase.

## Intrusive Linked Lists (ilist)

The most widely used structures are the intrusive linked list types defined in `ilist.h`. Both the doubly-linked (`dlist`) and singly-linked (`slist`) variants embed their link nodes directly inside the containing struct, which eliminates a separate heap allocation per element and improves cache locality.

The doubly-linked list uses a sentinel node design: `dlist_head` contains a single `dlist_node` whose `next` and `prev` pointers always point into the list (ilist.h). As a result, the head is never NULL, and iteration never needs a branch for the empty case. This means an empty `dlist_head` initialized to all-zeros is already valid — the sentinel's `next` and `prev` both point to itself.

The singly-linked `slist` does not carry a tail pointer. `slist_delete()` (ilist.c) is therefore O(n) because it must walk from the head to locate the predecessor. The comment on that function explicitly recommends `slist_delete_current()` from within an ongoing iteration as the efficient alternative.

Both types support debug-mode integrity checking. When `ILIST_DEBUG` is defined, `dlist_check()` traverses the list both forward and backward. It verifies that every node's `prev->next` and `next->prev` back-references are self-consistent (ilist.c). `dlist_member_check()` confirms a node actually belongs to a given list before removal.

The correct way to embed these structures is via the `dlist_node` or `slist_node` member and then recover the containing struct with `dlist_container()` or `slist_container()`, which expand to `offsetof`-based casts. This pattern avoids the need for a separate pointer-to-parent field.

## Red-Black Tree (rbtree)

`rbtree.c` provides a generic balanced binary search tree for ordered data. The implementation maintains two classic red-black invariants: a red node's children are always black, and every path from root to any leaf traverses the same number of black nodes. Together, these bound the tree height to approximately 2 log₂(n), giving O(log n) worst-case for find, insert, and delete.

The caller provides all allocation and comparison logic through function pointers stored in the opaque `RBTree` struct (rbtree.c). The tree invokes `rbt_comparator` for every traversal step. It calls `rbt_combiner` when an insert finds a pre-existing key, allowing callers to merge data rather than reject duplicates. The `rbt_allocfunc` and `rbt_freefunc` control node memory, so callers can allocate from a [[subsystems/memory/contexts|memory context]] of their choice.

Nodes embed `RBTNode` as their first member. Caller data follows immediately after. `rbt_copy_data()` moves the caller's extra bytes using `memcpy(dest + 1, src + 1, node_size - sizeof(RBTNode))`, so the caller-visible fields are always contiguous after the fixed header (rbtree.c).

Leaf sentinels use a single static `RBTNIL` node rather than NULL, which eliminates special-casing for absent children throughout the rotation and fixup routines. The sentinel is permanently black with both children pointing to itself.

The tree supports two iteration orders: `LeftRightWalk` (in-order, ascending) and `RightLeftWalk` (descending). The iterator state fits in a `RBTreeIterator` struct that the caller provides. Multiple iterators over the same tree can coexist, but the tree must not be modified during traversal.

## Heap Structures

Two heap variants serve different priority-queue workloads.

### Binary Heap

`binaryheap.c` implements a fixed-capacity max-heap backed by a flat `Datum` array. The array-based layout gives O(1) random access via the standard index arithmetic: left child of node `i` is at `2*i+1`, right child at `2*i+2`, parent at `(i-1)/2` (binaryheap.c).

The primary use case is merge sorting across pre-sorted inputs (such as parallel heap scans or merge-append nodes), where the capacity is known in advance. The two-phase bulk-load path — `binaryheap_add_unordered()` followed by `binaryheap_build()` — runs in O(n) by sifting down from the last internal node rather than inserting elements one by one. `binaryheap_replace_first()` swaps the root in O(1) best case or O(log n) worst case, which is the critical hot path for k-way merge.

A `bh_has_heap_property` flag tracks whether the invariant is currently satisfied. Assertions guard operations that require it.

### Pairing Heap

`pairingheap.c` trades the fixed-capacity constraint for amortized O(1) insert and O(log n) delete-min. Unlike the binary heap's flat array, a pairing heap is a forest of heap-ordered trees using embedded `pairingheap_node` link pointers.

The node structure stores `first_child`, `next_sibling`, and a dual-purpose `prev_or_parent` pointer: before a node becomes the first sibling it points to its parent, afterwards to the previous sibling. This compact representation avoids a separate parent field while still allowing arbitrary-node removal (pairingheap.c).

Insert is a single `merge()` call — O(1). Remove-min replaces the root with a two-pass merge of the former root's children: first pair adjacent siblings left-to-right, then fold the resulting list right-to-left. Arbitrary `pairingheap_remove()` works by detaching the node and merging its children into a replacement subtree, then splicing it back into the same position.

PostgreSQL uses the pairing heap for structures where the cardinality is not known at query planning time and where decrease-key or arbitrary removal is needed, such as in the maintenance of timer lists.

## Bloom Filter (bloomfilter)

`bloomfilter.c` provides a probabilistic set-membership test: elements can be added, and a query returns either "definitely not in set" or "probably in set." False negatives are impossible. The bloom filter bounds false positives to the target rate of 1–2%.

The implementation allocates a bitset sized as a power of two, allowing modular arithmetic to reduce to a bitwise AND (mod_m(), bloomfilter.c). The implementation chooses the number of hash functions `k` to minimize the false positive rate, given the bitset size and the caller's estimate of total elements: `k = round(ln(2) * m / n)` (optimal_k(), bloomfilter.c), capped at 10.

Rather than computing `k` independent hash functions, the implementation derives all of them from two 32-bit values extracted from a single 64-bit `hash_any_extended()` call, using enhanced double hashing (k_hashes(), bloomfilter.c). This avoids the correlation issues that classic double hashing produces with power-of-two bitset sizes. The caller-supplied seed makes it possible to use different hash families across invocations, reducing the chance that the same false positives recur.

`bloom_work_mem` is expressed in kilobytes, matching the convention used by [[subsystems/executor/work-mem-and-spill|work_mem]]. The bitset is capped at 512 MB (2^32 bits), so that 32-bit hash values remain sufficient.

## Dynamic Shared Memory Hash Table (dshash)

`dshash.c` provides a hash table that lives in dynamic shared memory and is accessible from multiple backends simultaneously. Unlike `HTAB` (dynahash), which uses process-local memory, dshash stores all data through a `dsa_area`, making entries visible across backends without copying.

PostgreSQL partitions the table into 128 fixed lock partitions (matching `NUM_BUFFER_PARTITIONS`), each protected by an [[subsystems/locking/lwlocks|LWLock]] (dshash.c). The table assigns buckets to partitions by taking the high-order bits of the hash value. Ordinary find, insert, and delete operations lock a single partition. This achieves good concurrency as long as operations do not collide at partition granularity.

Resize acquires all 128 partition locks in order. It doubles the bucket count and redistributes existing items by recomputing their bucket indices for the new size. Resize releases the locks immediately after. This design means a resize is a stop-the-world event at the hash-table level. However, resizes happen only a logarithmic number of times as the table grows toward a stable size.

The per-backend `dshash_table` struct caches the current bucket pointer array and size. `ensure_valid_bucket_pointers()` refreshes this cache whenever the backend holds a partition lock and the cached size disagrees with the shared control block — a lightweight mechanism for detecting that a concurrent resize occurred (dshash.c).

Entry lookup returns a pointer to the caller-visible data while holding the partition lock, which the caller must explicitly release via `dshash_release_lock()`. This design integrates naturally with lock hierarchies: the caller holds the LWLock across the entire critical section, so it can safely read or modify the entry without additional synchronization.

| Structure | Location | Key Properties |
|---|---|---|
| `dlist_head` / `slist_head` | ilist.h | Intrusive; sentinel node; O(1) push/pop |
| `RBTree` | rbtree.h | Ordered; O(log n) find/insert/delete; caller-allocated nodes |
| `binaryheap` | binaryheap.h | Fixed capacity; flat array; O(n) bulk build |
| `pairingheap` | pairingheap.h | Unbounded; amortized O(1) insert; arbitrary remove |
| `bloom_filter` | bloomfilter.h | Probabilistic; no false negatives; 1–2% FP target |
| `dshash_table` | dshash.h | Shared memory; partitioned LWLock concurrency; dynamic resize |

## Related Topics

- [[subsystems/memory/contexts|Memory contexts]] — all lib/ structures allocate via palloc into the current context
- [[subsystems/memory/resource-owner|ResourceOwner]] — tracks cleanup of backend-local structures
- [[subsystems/locking/lwlocks|LWLocks]] — dshash partitions each carry an embedded LWLock
- [[subsystems/executor/work-mem-and-spill|work_mem]] — bloom_work_mem follows the same KB-unit convention
- [[architecture/shared-memory|Shared memory]] — dshash is one of the principal structures placed in DSM
