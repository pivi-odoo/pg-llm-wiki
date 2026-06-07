---
title: "MergeAppend Executor Node"
aliases:
  - MergeAppend Node
  - nodeMergeAppend
source_files:
  - src/backend/executor/nodeMergeAppend.c
  - src/include/executor/nodeMergeAppend.h
symbols:
  - MergeAppendState
  - ExecInitMergeAppend
  - ExecMergeAppend
  - ExecEndMergeAppend
  - ExecReScanMergeAppend
  - heap_compare_slots
---

The MergeAppend node merges multiple pre-sorted child streams into a single sorted output without performing a separate sort step. Where [[subsystems/executor/append-node|Append]] simply concatenates child outputs one after another, MergeAppend interleaves them in key order. This interleaving is only correct when every child already delivers tuples sorted by the same key. MergeAppend replaces sorting the combined result set — an O(N log N) operation over all rows — with an O(log n) heap operation per row, where n is the number of children.

## When the planner chooses MergeAppend

The planner emits a MergeAppend when it can guarantee that each child produces output in the required order and the query demands a globally sorted result. The two dominant cases are:

- **Partitioned table scans with ORDER BY on the partition key.** When partition pruning narrows the scan to multiple partitions, each partition scan is individually sorted by the partition key. MergeAppend merges those sorted streams rather than collecting all rows and sorting them.
- **UNION ALL with an outer ORDER BY** where the planner can push the sort requirement into each branch. If every branch plan satisfies the sort key, MergeAppend is preferred over Append followed by a Sort.

MergeAppend is distinct from GatherMerge, which performs the same merge but across parallel worker processes. GatherMerge reads sorted streams from workers over shared-memory queues; MergeAppend reads from ordinary executor sub-trees in the same process.

A key planner prerequisite is that the children must already be sorted. If even one child cannot satisfy the sort order without an added Sort node, the planner may find that a single Sort over the output of a plain Append is cheaper.

## Binary heap structure

MergeAppend maintains a min-heap over its children. Each entry in the heap is an integer index identifying a child slot rather than a copied tuple. The backing heap is a `binaryheap` from `lib/binaryheap.c`, a standard binary min-heap with a caller-supplied comparator.

The comparator `heap_compare_slots` receives two child indexes. It fetches the current pending tuple from each child's slot (`ms_slots[i]`) and applies the sort keys in order using `ApplySortComparator`. The heap is a min-heap, but the comparator returns a negative value when the first argument should be the minimum. MergeAppend inverts the result with `INVERT_COMPARE_RESULT` so the heap root always holds the child with the smallest pending tuple. MergeAppend disables abbreviated key conversion for two reasons. Tuples enter and leave the heap one at a time as rows are consumed, so the amortised cost of conversion would not pay off. Maintaining abbreviated keys across independent child slots is also impractical.

## Initialization and heap loading

During `ExecInitMergeAppend`, the node:

1. Optionally sets up run-time partition pruning via `ExecInitPartitionPruning`, which also returns the initial set of valid sub-plans. When pruning is not in use, all sub-plans are valid.
2. Allocates `ms_slots` — an array of `TupleTableSlot *`, one per valid child — and an empty `binaryheap` sized to the number of valid children.
3. Calls `ExecInitNode` on each valid child to initialize its sub-tree.
4. Builds the `SortSupportData` array (`ms_sortkeys`) using `PrepareSortSupportFromOrderingOp`, one entry per sort key column. The sort support structure carries the collation, null ordering, and a prepared comparison function pointer, enabling fast per-call comparison without repeated operator lookup.

MergeAppend defers heap loading until the first call to `ExecMergeAppend`. At that point the node pulls one tuple from each valid child. It adds each child index to the heap with `binaryheap_add_unordered`. After all children have contributed their first tuple, `binaryheap_build` turns the unordered array into a proper heap in O(n) time. The `ms_initialized` flag prevents this bootstrapping from repeating on subsequent calls.

## Per-row execution

On every call after initialization, the heap root holds the index of the child whose pending tuple compares smallest. The node returns that tuple's slot directly — no copy is made. Before the next call, the node advances the winning child by fetching its next tuple. It then either replaces the heap root (via `binaryheap_replace_first`, which re-sifts the heap in O(log n)) or removes the root entirely if the child is exhausted (`binaryheap_remove_first`). The node defers the fetch to the next call rather than performing it immediately after returning, so tuples are not pulled from children until they are actually needed.

When the heap becomes empty, all children are exhausted. The node then returns an empty slot. MergeAppend passes child slots through rather than copying into its own result slot, so it sets `resultopsset = true` and `resultopsfixed = false`, identical to the [[subsystems/executor/append-node|Append]] node.

## Rescan

`ExecReScanMergeAppend` handles the case where a parameter or correlated value changes. If a `PARAM_EXEC` parameter used in pruning expressions has changed, the node frees `ms_valid_subplans` and sets it to NULL. The next execution then re-evaluates partition pruning from scratch.

For each child, the node propagates changed-parameter information via `UpdateChangedParamSet`. Children whose `chgParam` is non-NULL rescan themselves on their next `ExecProcNode` call. The node immediately rescans children with no changed parameters via `ExecReScan`. Finally, `binaryheap_reset` empties the heap. The node sets `ms_initialized` to false, so the heap-loading sequence runs again on the next `ExecMergeAppend` call.

## Performance characteristics

The per-row cost of MergeAppend is O(log n) heap operations, where n is the number of active children. This compares favorably to the alternative of Append followed by Sort, which costs O(N log N) where N is the total row count. The advantage grows as N/n increases — that is, as partitions contain more rows relative to the number of partitions.

The height of the binary heap bounds the number of child plan comparisons per output row, so even a MergeAppend over hundreds of partitions adds only a small constant overhead per row. The main caveat is that MergeAppend cannot begin returning rows until it fetches the first tuple from every valid child. It also cannot be parallelized directly, because the node drives each child sequentially. For parallel sorted output across workers, GatherMerge fills that role.

## See also

- [[subsystems/executor/append-node|Append node]] — unsorted concatenation of child streams; the simpler sibling
- [[subsystems/executor/overview|Executor overview]] — Volcano model, EState, PlanState, and the executor node lifecycle
