---
title: Sorting and External Sort
aliases:
  - tuplesort
  - external sort
  - merge sort
tags:
  - theme/parallelism
source_files:
  - src/backend/utils/sort/tuplesort.c
  - src/backend/utils/sort/logtape.c
  - src/include/utils/tuplesort.h
  - src/backend/executor/nodeIncrementalSort.c
symbols:
  - Tuplesortstate
  - TuplesortPublic
  - SortTuple
  - TupSortStatus
  - LogicalTape
  - LogicalTapeSet
  - TapeBlockTrailer
  - SortCoordinateData
  - TuplesortInstrumentation
  - TuplesortMethod
  - tuplesort_begin_heap
  - tuplesort_performsort
  - tuplesort_gettupleslot
  - tuplesort_set_bound
  - mergeruns
  - dumptuples
---

# Sorting and External Sort

Sorting is one of the most pervasive operations in query execution. Every `ORDER BY`, `GROUP BY`, and `DISTINCT` clause may require it. Merge joins demand sorted inputs on both sides. `CREATE INDEX` sorts the heap's tuples before feeding them to the index build. `CLUSTER` physically rewrites the table in index order. `VACUUM` can sort freeze candidates for efficient processing. The shared infrastructure that handles all these cases lives in `src/backend/utils/sort/tuplesort.c` — a single, generalized sort engine capable of operating entirely in memory or spilling to disk through an external merge sort algorithm.

## The Two-Phase Design

The fundamental design question for a sort engine is what to do when the data does not fit in available memory. PostgreSQL answers it with a two-phase design.

During the **accumulation phase**, the engine stores incoming tuples in an in-memory array of `SortTuple` structs. The sort engine tracks how much of `work_mem` has been consumed (`availMem` in `Tuplesortstate`). As long as the input fits, the engine writes nothing to disk. When the caller eventually calls `tuplesort_performsort()`, the engine sorts the array in place with quicksort. It returns the result tuple by tuple directly from memory, with no I/O at all. The sort state machine records this as `TSS_SORTEDINMEM`.

When the in-memory array fills `work_mem`, the engine transitions to the **external sort path**. It quicksorts what is currently in memory. It writes that sorted chunk as a *run* to a temporary tape file. Then it resets the in-memory array. It continues absorbing input from there. After all input is consumed, the engine enters a merge phase that combines the on-disk runs into a single sorted stream. The relevant state transitions are:

| State | Meaning |
|---|---|
| `TSS_INITIAL` | Loading tuples; within memory limit |
| `TSS_BOUNDED` | Loading tuples into a bounded-size heap |
| `TSS_BUILDRUNS` | Spilling; writing sorted runs to tape |
| `TSS_SORTEDINMEM` | Sort completed entirely in memory |
| `TSS_SORTEDONTAPE` | Sort complete; final run is on a tape |
| `TSS_FINALMERGE` | Performing final merge on-the-fly |

## In-Memory Sort

When the full dataset fits in `work_mem`, the engine sorts the `memtuples` array using quicksort. PostgreSQL's sort template (`lib/sort_template.h`) generates specialized variants of quicksort for common leading-key types: unsigned comparisons, signed 64-bit comparisons, and int32 comparisons. Each specialization attempts to resolve comparisons from the pre-extracted `datum1` field of `SortTuple` without touching the underlying tuple at all. It falls back to the full comparator only for tiebreaks.

For `ORDER BY … LIMIT n` queries the planner may request a *bounded sort*, set via `tuplesort_set_bound()`. In that case, rather than sorting the full input, the engine maintains a max-heap of exactly `n` tuples (`TSS_BOUNDED`). The engine compares each new tuple against the heap's maximum. If the new tuple is smaller, it replaces the top. Once input is exhausted, the engine sorts the heap in place to produce the final `n` results in order. This avoids materializing the entire dataset. It also keeps memory consumption proportional to the limit, not the input size.

### MinimalTuple Storage

The heap API (`tuplesort_begin_heap`, used for `ORDER BY`, `GROUP BY`, `DISTINCT`, merge joins) stores tuples as `MinimalTuple` rather than full `HeapTuple`. A `MinimalTuple` strips out system columns (xmin, xmax, ctid, and other per-tuple transaction visibility fields), saving typically 23 bytes per tuple. Over millions of rows this is a meaningful reduction in `work_mem` pressure. The `SortTuple` struct holds a pointer to the underlying tuple alongside the pre-extracted first key column:

```c
typedef struct {
    void   *tuple;    /* MinimalTuple or IndexTuple */
    Datum   datum1;   /* value of first key column */
    bool    isnull1;  /* is first key column NULL? */
    int     srctape;  /* source tape number (merge phase) */
} SortTuple;
```

Caching `datum1` avoids repeated `heap_getattr` calls during comparisons. For pass-by-reference types with sort support that provides *abbreviated keys* — compact, pass-by-value proxies for expensive comparisons like text — `datum1` holds the abbreviation instead of the real value. If the abbreviated keys compare equal, the engine falls back to full comparison.

## External Sort: Writing Runs

When the in-memory array exhausts `work_mem`, the engine calls `dumptuples()`. It quicksorts the current `memtuples` array. Then it writes each tuple sequentially to the current *output tape* via `WRITETUP()`. A sentinel (an all-zero unsigned int length word) separates runs on a tape. After writing, the engine clears the array. Then it allocates a new output tape for the next run.

`work_mem` determines the number of tapes: the engine wants each tape's read buffer during the merge phase to hold at least `MERGE_BUFFER_SIZE` (32 × `BLCKSZ` ≈ 256 kB) of data to maintain sequential access locality. `MINORDER = 6` and `MAXORDER = 500` bound the merge order (number of simultaneous input tapes). If the engine produces more runs than available tapes, it writes subsequent runs round-robin across the existing tapes. This requires multiple merge passes.

Historically PostgreSQL used *replacement selection* to generate longer initial runs. In this technique, the sort heap could emit tuples from the current run even while still consuming input, potentially producing runs up to twice as long as `work_mem`. PostgreSQL removed this technique in recent versions. Benchmarks showed that quicksort-generated runs combined with a balanced k-way merge outperforms polyphase/replacement-selection approaches on modern hardware. The simpler code path is also easier to reason about.

## The Tape Abstraction: logtape.c

The external sort path does not create one file per logical tape. Instead, `logtape.c` implements a virtual tape layer that multiplexes any number of logical tapes over a single underlying `BufFile`. This matters because peak disk usage from naive per-tape files would be at least twice the data volume. The last merge pass must hold both input and output simultaneously. By recycling disk blocks the moment they are read, `logtape.c` keeps total disk consumption close to the actual data size.

Each `LogicalTape` maintains a linked chain of `BLCKSZ`-sized blocks within the shared `BufFile`. Every block ends with a `TapeBlockTrailer`:

```c
typedef struct TapeBlockTrailer {
    long prev;  /* previous block on this tape, or -1 on first */
    long next;  /* next block, or negated count of valid bytes on last */
} TapeBlockTrailer;
```

The sign of `next` distinguishes interior blocks (positive block number) from the last block (negative, encodes byte count). The `LogicalTapeSet` tracks free blocks in a min-heap (`freeBlocks[]`), always reallocating the lowest-numbered available block to improve write locality.

When writing, each `LogicalTape` holds one partially-filled buffer block. After the engine rewinds the tape for the merge phase, the tape can buffer multiple blocks at once while reading. The buffer is sized to `MERGE_BUFFER_SIZE`. This size improves sequential read throughput. The `BufFile` underneath can itself span multiple physical OS files if needed to escape file size limits. `logtape.c` is unaware of those splits.

For parallel sorts, each worker produces a single `BufFile` tapeset. The leader concatenates worker `BufFile` objects into one unified file. It also adjusts block number offsets (`offsetBlockNumber`) so that logical tape block references remain valid across the join.

## The Merge Phase

Once all input is consumed, `mergeruns()` orchestrates the merge. The engine begins by setting up `inputTapes` (the tapes that hold runs) and `outputTapes` (tapes for merged output). If the number of runs fits within one merge pass (runs ≤ `maxTapes`), a single pass suffices. The engine can then perform the final merge on-the-fly as it retrieves tuples — the `TSS_FINALMERGE` state. This avoids one complete write of the sorted output to disk.

The merge algorithm is a k-way merge using a **loser tree** (tournament tree) maintained as a heap over `SortTuple` entries. Each element of the heap holds the frontmost unread tuple from one input tape, tagged with `srctape` to identify its source. The engine repeatedly pops the minimum tuple. It emits that tuple to the output tape, then reads the next tuple from the same source tape to replace it. Only one tuple per input tape needs to be in memory at any time. As a result, the merge phase has very low memory overhead — just `M` tuple slots for an `M`-way merge.

To avoid per-tuple palloc/pfree overhead during merging, `tuplesort.c` switches to a fixed-size **slab allocator** at merge time. The slab is one large allocation divided into `SLAB_SLOT_SIZE` (1 kB) slots, one per tape plus one spare. The engine places tuples that fit in 1 kB into slab slots. Oversized tuples fall back to `palloc()`. When the engine releases a tuple, its slot returns to a free list instead of being freed.

When the caller does not need random access (`TUPLESORT_RANDOMACCESS` not set), the engine defers the final merge pass. `tuplesort_performsort()` returns as soon as runs are ready. Merging then happens incrementally as the caller calls `tuplesort_gettupleslot()`. This saves one full write of the sorted data.

## Incremental Sort

Introduced in PostgreSQL 13, incremental sort (`nodeIncrementalSort.c`) exploits pre-existing order in the input. Incremental sort avoids sorting the entire dataset at once when a query requires sorting on `(key1, key2, ..., keyN)` and the input is already sorted on a prefix `(key1, ..., keyM)`. This prefix order typically comes from an index scan or an earlier sort.

Instead, it scans the input to detect *prefix key group boundaries*: consecutive rows where the presorted columns change value. Within each group, all rows share the same prefix. Only the remaining `(keyM+1, ..., keyN)` columns need sorting. The engine feeds each such group to an independent `Tuplesortstate`. That instance sorts the group and emits it before the next group starts.

The practical benefit is two-fold. First, individual groups are far more likely to fit in `work_mem` than the full dataset, eliminating disk spills. Second, the node can begin returning rows as soon as the first group is sorted, rather than waiting for the entire input. This is particularly valuable for queries with `LIMIT`.

The implementation operates in two modes. It starts in a *prescan mode* that collects a small batch of tuples without checking prefix membership. This mode sorts on all keys, providing a fast path for small result sets. If it detects large groups, it switches to *full-group mode*. In this mode, it fetches all tuples for one prefix group. Then it sorts only the unsorted tail columns. The heuristic switch avoids paying the overhead of new `Tuplesortstate` creation for many tiny groups.

## Parallel Sort

When the planner chooses a parallel sort plan, the [[subsystems/executor/parallel|parallel executor]] launches worker processes, each sorting a disjoint partition of the input. Each worker runs a full `Tuplesortstate` independently, consuming tuples from a parallel heap scan. It produces a single sorted run. The coordination point is a `Sharedsort` structure in shared memory, protected by a spinlock. A `SharedFileSet` makes the workers' `BufFile` tapesets accessible to the leader.

After all workers call `tuplesort_performsort()` and freeze their result tapes, the leader calls `leader_takeover_tapes()`. This function concatenates the worker `BufFile` objects. It then constructs a logical tape for each worker's run. The leader then merges these runs exactly as it would merge runs from its own external sort. The parallel infrastructure is transparent to the merge logic. Each worker is guaranteed to produce exactly one run. As a result, the merge order equals the number of workers.

The `Gather Merge` executor node above the parallel sort continuously pulls from this merged stream. From the perspective of the query tree, a parallel sort behaves identically to a serial sort. The parallelism is internal to the sort node.

### Parallel Coordination Structures

| Field | Purpose |
|---|---|
| `SortCoordinateData.isWorker` | Distinguishes worker from leader tuplesort |
| `SortCoordinateData.nParticipants` | Number of workers launched (leader only) |
| `SortCoordinateData.sharedsort` | Pointer into shared memory |
| `Sharedsort.currentWorker` | Next worker identifier to assign |
| `Sharedsort.workersFinished` | Count of workers that have completed |
| `Sharedsort.fileset` | Shared file set for cross-process tape access |

## Key Structures

### Tuplesortstate (private)

| Field | Purpose |
|---|---|
| `status` | Current `TupSortStatus` phase |
| `availMem` / `allowedMem` | Remaining / total `work_mem` budget in bytes |
| `memtuples` / `memtupcount` | In-memory sort array |
| `tapeset` | `LogicalTapeSet` for disk-based tapes |
| `inputTapes` / `outputTapes` | Tape arrays for merge phase |
| `currentRun` | Run counter during build phase |
| `slabMemoryBegin/End` | Slab allocator bounds for merge phase |
| `bounded` / `bound` | Whether a top-N limit is in effect |

### TuplesortPublic (shared interface)

| Field | Purpose |
|---|---|
| `comparetup` | Tuple comparison function pointer |
| `writetup` / `readtup` | Tape serialization function pointers |
| `removeabbrev` | Restore real datum1 from abbreviated key |
| `sortKeys` / `nKeys` | Sort key descriptors |
| `onlyKey` | Optimization for single-key sorts |
| `sortopt` | Bitmask of `TUPLESORT_*` option flags |

### Sort Method Instrumentation

`EXPLAIN ANALYZE` reports the sort method through `TuplesortInstrumentation`:

| `TuplesortMethod` | Meaning |
|---|---|
| `SORT_TYPE_QUICKSORT` | Sorted entirely in memory with quicksort |
| `SORT_TYPE_TOP_N_HEAPSORT` | Bounded sort via in-memory heap |
| `SORT_TYPE_EXTERNAL_SORT` | Spilled to disk; single merge pass |
| `SORT_TYPE_EXTERNAL_MERGE` | Spilled to disk; multiple merge passes |

## Relation to Other Subsystems

The sort engine integrates tightly with several other parts of the system. Memory context management ([[subsystems/memory/contexts]]) is critical. Tuples live in a dedicated `tuplecontext` (a sub-context of `sortcontext`) that is reset at intervals to control fragmentation. The underlying `BufFile` abstraction (used by `logtape.c`) builds on the temporary file infrastructure, not the shared [[subsystems/storage/buffer-manager|buffer manager]]. Sort temp files bypass the buffer pool entirely. This is appropriate because their access pattern offers no benefit from caching.

The planner ([[subsystems/planner/overview]]) chooses between `Sort` and `IncrementalSort` nodes based on available path orderings and cost estimates. The cost model for external sort accounts for the number of merge passes. That count depends on the ratio of estimated data size to `work_mem`. B-tree index builds ([[subsystems/indexes/btree]]) use `tuplesort_begin_index_btree()` to sort index entries before the bottom-up index build. This relies on the same external sort infrastructure. The `CLUSTER` command and `VACUUM` freeze-candidate ordering each use dedicated `tuplesort_begin_*` variants that preserve different aspects of the heap tuple.

## Related Topics

- [[subsystems/executor/sort-support|Sort Support]] — covers the `SortSupport` API and abbreviated key infrastructure that `tuplesort` uses to accelerate datum comparisons during sorting.
- [[subsystems/executor/incremental-sort|Incremental Sort]] — details the executor node that drives prefix-group-based incremental sorting built on top of `tuplesort`.
- [[subsystems/executor/work-mem-and-spill|work_mem and Spill]] — explains how `work_mem` governs the in-memory budget for sorts and other operators, and what happens when they spill to disk.
- [[subsystems/planner/sort-avoidance|Sort Avoidance]] — describes planner strategies that eliminate or reduce sorting by exploiting index order or presorted inputs.
- [[subsystems/executor/parallel|Parallel Executor]] — explains the parallel infrastructure that distributes sort work across worker processes and coordinates result merging in the leader.
- [[subsystems/storage/temp-files|Temp Files]] — documents the `BufFile` and temporary file layer that `logtape.c` uses for writing external sort runs to disk.
- [[subsystems/indexes/btree|B-tree Indexes]] — shows how B-tree index builds consume the same `tuplesort` engine to produce sorted index entries before the bottom-up build phase.
