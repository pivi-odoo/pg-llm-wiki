---
title: "Sequential Scan"
aliases:
  - seqscan
  - Seq Scan
  - full table scan
  - ExecSeqScan
tags:
  - theme/parallelism
source_files:
  - src/backend/executor/nodeSeqscan.c
  - src/include/executor/nodeSeqscan.h
symbols:
  - ExecSeqScan
  - ExecInitSeqScan
  - ExecEndSeqScan
  - ExecReScanSeqScan
  - SeqNext
  - SeqScanState
  - ExecSeqScanEstimate
  - ExecSeqScanInitializeDSM
  - ExecSeqScanInitializeWorker
---

A sequential scan reads every page of a relation in physical storage order, evaluating the WHERE clause on each tuple as it goes. It is the simplest possible scan strategy and the planner's unconditional fallback — the planner can execute every query with a sequential scan, even if a better plan exists.

## What a Sequential Scan Does

The executor node (`nodeSeqscan.c`) drives the scan through the Table Access Method API, which abstracts the physical storage layer. On the first call to `SeqNext()`, the node opens a scan descriptor via `table_beginscan()`, passing the relation, the current snapshot, and no index keys. Subsequent calls invoke `table_scan_getnextslot()`, which advances the scan to the next visible tuple and writes it into a `TupleTableSlot`.

For the heap AM, `table_scan_getnextslot()` reads pages through the [[subsystems/storage/buffer-manager|buffer manager]] in block number order. The [[subsystems/transactions/mvcc|MVCC]] visibility check happens inside the AM on each tuple before the AM surfaces it to the executor. The AM silently skips dead or uncommitted tuples.

After `SeqNext()` returns a tuple, `ExecScan()` evaluates the node's qualification list — the pushed-down WHERE predicates — against it. The scan discards tuples that fail the qualification and continues. `SeqRecheck()` always returns true because seq scans pass no keys to the AM and perform no AM-level filtering. All filtering happens at the executor level.

## When the Planner Chooses a Sequential Scan

The planner models the cost of a sequential scan as roughly proportional to the number of pages in the relation (`seq_page_cost` × `relpages`), plus tuple evaluation cost. It compares this against the cost of every available index scan and chooses the cheapest path.

A sequential scan wins (or is the only option) in several common situations:

- **No usable index exists** for the predicate — a sequential scan is the only choice.
- **Low selectivity** — if the predicate matches a large fraction of the relation, an index would touch nearly the same pages anyway, often with higher overhead per tuple due to random I/O and index traversal.
- **Small relation** — a table that fits in a few pages is nearly free to scan sequentially, and the index overhead would dominate.
- **TABLESAMPLE** — the `TABLESAMPLE` clause always uses a sequential scan variant regardless of predicates.

Setting `enable_seqscan = off` tells the planner to add a large cost penalty to sequential scans, effectively forcing it to prefer any available index. This is a diagnostic tool, not a production setting.

## Parallel Sequential Scan

When `max_parallel_workers_per_gather` > 0 and the relation is large enough, the planner can request a parallel sequential scan. The parallel infrastructure splits the work across a leader and one or more workers, each scanning a non-overlapping range of blocks.

The executor coordinates the split through a `ParallelTableScanDesc` structure allocated in dynamic shared memory (DSM). `ExecSeqScanInitializeDSM()` calls `table_parallelscan_initialize()` to prepare this structure, then stores it in the shared memory table of contents (TOC) under the plan node ID. Each worker's `ExecSeqScanInitializeWorker()` retrieves the same structure from the TOC via `shm_toc_lookup()` and calls `table_beginscan_parallel()` to open its own scan descriptor pointing at the shared coordinator.

The heap AM's parallel scan implementation divides the relation into block ranges and hands them out atomically to workers on demand. Each worker fetches the next unscanned block range and scans it. It then reports back for another. This dynamic partitioning adapts naturally to workers that finish at different speeds without any pre-assignment.

The result rows from all workers converge at the `Gather` or `GatherMerge` node above the seq scan in the plan tree.

## Reading EXPLAIN Output

A sequential scan appears in `EXPLAIN` as:

```
Seq Scan on accounts  (cost=0.00..18334.00 rows=1000000 width=32)
  Filter: (status = 'active')
```

The `Filter:` line shows predicates evaluated at the executor level after fetching each tuple. A large gap between `rows=` (estimate) and `actual rows=` (from `EXPLAIN ANALYZE`) indicates a stale statistics problem, not a seq scan problem per se. A high `rows removed by filter` count in `EXPLAIN ANALYZE` means the scan is reading many tuples that fail the predicate — often a signal that an index on the filter column would help.

A parallel seq scan shows a `Workers Planned:` annotation and a `Gather` or `GatherMerge` above it:

```
Gather  (cost=1000.00..94669.67 rows=1000000 width=32)
  Workers Planned: 2
  ->  Parallel Seq Scan on accounts  (cost=0.00..18334.00 rows=416667 width=32)
```

## Related Topics

- [[subsystems/storage/table-am|Table Access Method API]]
- [[subsystems/storage/heap|Heap Storage and Tuple Format]]
- [[subsystems/executor/overview|Executor Overview]]
- [[subsystems/planner/scan-selection|Scan Node Selection]]
- [[subsystems/planner/reading-explain|Reading EXPLAIN Output]]
- [[subsystems/executor/parallel|Parallel Query]]
