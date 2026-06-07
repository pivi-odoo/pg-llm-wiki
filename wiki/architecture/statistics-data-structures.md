---
title: "Statistics Data Structures: HyperLogLog and IntegerSet"
aliases:
  - hyperloglog
  - HyperLogLog
  - integerset
  - IntegerSet
  - n_distinct estimation
  - dead TID tracking
tags:
  - theme/query-optimization
  - theme/vacuum-and-maintenance
source_files:
  - src/backend/lib/hyperloglog.c
  - src/backend/lib/integerset.c
  - src/include/lib/hyperloglog.h
  - src/include/lib/integerset.h
symbols:
  - hyperLogLogState
  - initHyperLogLog
  - initHyperLogLogError
  - addHyperLogLog
  - estimateHyperLogLog
  - IntegerSet
  - intset_create
  - intset_add_member
  - intset_is_member
  - intset_begin_iterate
  - intset_iterate_next
---

Two specialized data structures in `src/backend/lib/` address problems where the obvious alternatives — a plain array or a hash table — are too large, too slow, or structurally ill-suited to the workload. `HyperLogLog` provides a probabilistic cardinality estimate with fixed memory cost, used during `ANALYZE` to compute the `n_distinct` column statistic. `IntegerSet` provides a compressed, sorted set of 64-bit integers, used by [[code-paths/vacuum|vacuum]] to track which heap TIDs are dead without materializing the full list in uncompressed form. This page is a companion to [[architecture/internal-data-structures|internal data structure library]], which covers the other general-purpose structures in `lib/` (`ilist`, `rbtree`, `binaryheap`, `pairingheap`, `bloomfilter`, `dshash`).

## HyperLogLog Cardinality Estimation

The planner needs to know how many distinct values a column contains in order to estimate selectivity. The exact count requires either a full sort or a hash table of every value seen — both O(n) in memory. HyperLogLog provides an approximate count in O(1) space, which is the right trade-off for `ANALYZE`. The planner already works with estimates, so a 1–2% error in `n_distinct` is acceptable and far better than the alternative of either skipping the statistic or exhausting memory on wide tables.

### The Algorithm

The core insight of HyperLogLog is that the maximum number of leading zeros observed in the hash values of a set of distinct elements is a probabilistically useful proxy for the logarithm of the cardinality. If you hash every element and the longest run of leading zeros you ever see is `k`, the set probably contains around 2^k elements.

The variance of this single-register estimator is high. HyperLogLog reduces it by partitioning the hash space into `m = 2^b` independent registers, where `b` is the register width in bits. The high-order `b` bits of each hash value select a register. The low-order bits compute the position of the first set bit (the "rho" function). Each register stores the maximum rho value seen for hashes that map to it.

HyperLogLog then derives the cardinality estimate from the harmonic mean of `2^register[i]` over all registers:

```
E = alpha * m^2 * (sum_i 2^{-register[i]})^{-1}
```

The correction factor `alpha` compensates for a systematic multiplicative bias in the raw estimator. Its value depends on `m`. `initHyperLogLog()` (`hyperloglog.c`) precomputes this value.

At small and large cardinalities, the raw estimator becomes inaccurate, so HyperLogLog applies additional corrections. When the estimate falls below `2.5 * m`, HyperLogLog applies a linear counting correction: it counts registers that are still zero and switches the estimate to `m * ln(m / zero_count)`. When the estimate exceeds `2^32 / 30`, HyperLogLog applies a large-range correction to account for hash collisions across the 32-bit hash space. `estimateHyperLogLog()` (`hyperloglog.c`) implements both adjustments.

### Structure and API

`hyperLogLogState` (`hyperloglog.h`) holds the register array (`hashesArr`), its length (`nRegisters`), the precalculated `alphaMM`, and the register width. The array is a flat `uint8` buffer allocated by `palloc0()`, so an unobserved register starts at zero rather than negative infinity. This is a deliberate initialization choice: zero is the correct initial value for the HyperLogLog algorithm.

`initHyperLogLog()` takes a bit width between 4 and 16, giving between 16 and 65 536 registers. The companion `initHyperLogLogError()` selects the smallest bit width that achieves a target error rate, using the formula `e = 1.04 / sqrt(m)`. At bit width 10 (1024 registers), the expected error is about 3.25%. At bit width 14 (16 384 registers), it falls to about 0.8%.

`addHyperLogLog()` takes a pre-computed `uint32` hash. The caller is responsible for generating the hash. The function extracts the register index from the top `b` bits, computes the rho value from the remaining bits using `pg_leftmost_one_pos32()`, and updates the register with `Max(count, current)`. The one-way update means observations can never decrease a register. As a result, the estimator is monotonically increasing as elements are added — an important property for streaming use.

`estimateHyperLogLog()` returns a `double` representing the estimated cardinality. It does not modify the state. Multiple calls therefore return consistent results, unless `addHyperLogLog()` is interleaved.

### Use in ANALYZE

During `ANALYZE`, the statistics-gathering code in `src/backend/commands/analyze.c` feeds each sampled value to a HyperLogLog estimator (via `hash_any()` for the hash input) alongside a traditional algorithm that counts exact distinct values in the sample. The statistics-gathering code uses the HyperLogLog estimate to extrapolate the distinct count beyond the sample when the sample does not contain every distinct value. This extrapolated estimate becomes the `n_distinct` field in `pg_statistic`. The planner's selectivity functions read this field via [[subsystems/planner/statistics|planner statistics]] when planning equality predicates, joins, and grouping operations.

## IntegerSet: Compressed Sorted Integer Sets

Vacuum needs to track the TIDs of all dead heap tuples it encounters during a scan. It later passes them to each index for index tuple cleanup. For large tables this list can easily reach millions of entries. A plain 64-bit array would consume 8 bytes per TID and could grow to hundreds of megabytes. A hash set would have similar memory consumption and no meaningful ordering property. `IntegerSet` achieves much lower memory use by exploiting two facts about the workload: TIDs are added in heap-scan order (ascending), and nearby TIDs compress well.

### Structure

`IntegerSet` (`integerset.c`) is a B-tree in memory with compressed leaf nodes. Internal nodes hold up to 64 key-downlink pairs (each key is a `uint64`). Leaf nodes each hold up to 64 items. Each leaf item, however, packs up to 241 integers: a plain `uint64` for the first value, followed by a Simple-8b codeword that encodes the *differences* between up to 240 subsequent values.

Simple-8b is a fixed-width encoding scheme that fits between 1 and 240 integers into a 64-bit word. A 4-bit selector in the high bits indicates how many values are encoded and how many bits each occupies. When consecutive integers are close together, their differences are small. Many of them then fit in a single codeword: 240 consecutive integers (all with delta 1) encode in a single word using mode 0. When integers are far apart, fewer fit in the codeword. `IntegerSet` always stores the first value of each leaf item uncompressed, enabling binary search.

In memory consumption, this structure achieves approximately 0.1 bytes per integer in the best case (long runs of consecutive TIDs) and about 8 bytes per integer in the worst case (values more than 2^32 apart). In practice, heap TIDs are usually clustered within pages and blocks that vacuum scans sequentially. The average is therefore typically much closer to the low end.

### Append-Only Insertion Constraint

`intset_add_member()` requires that values be added in strictly ascending order (`integerset.c`). This constraint matches the heap-scan access pattern in vacuum. Vacuum scans the heap from block 0 forward. Dead TIDs arrive in ascending order. Because new values always go to the rightmost leaf, the B-tree never needs to split or rebalance interior nodes. The tree only ever adds new nodes to the right edge. It maintains the path from root to rightmost leaf in the `rightmost_nodes[]` array. This makes insertion amortized O(1) at the leaf level, with the only O(log n) cost being occasional upward propagation when a leaf fills and a new parent must be created.

`intset_add_member()` does not place values directly into the B-tree on each call. Instead, it accumulates new values in a flat `buffered_values[]` array. When the buffer reaches `MAX_BUFFERED_VALUES` (approximately twice the maximum values per leaf item), `intset_flush_buffered_values()` packs as many values as possible into Simple-8b codewords and appends them to the rightmost leaf. This batching allows the encoder to see enough values at once to select the most compact Simple-8b mode.

Membership testing with `intset_is_member()` first checks the unsorted buffer, then descends the B-tree to the appropriate leaf using binary search on the plain `first` field of each leaf item. Finally, it checks the packed codeword with `simple8b_contains()`. This three-level lookup is efficient because the per-leaf binary search operates on the unpacked first values, not on compressed data.

### Iteration

Iteration via `intset_begin_iterate()` and `intset_iterate_next()` walks the linked list of leaf nodes left to right, decoding each Simple-8b codeword into a temporary buffer (`iter_values_buf`) and returning values from that buffer one at a time. After exhausting the B-tree, the iterator transitions to the `buffered_values[]` array for any values that have not yet been flushed. The caller must not add new values while iteration is in progress. `intset_add_member()` enforces this with an assertion on the `iter_active` flag.

### Use in Vacuum

Vacuum uses `IntegerSet` to accumulate dead TIDs during the heap scan phase. It then passes the collected TIDs to each index's bulk-delete callback. The ordered iteration property means `IntegerSet` delivers the TIDs to indexes in ascending order. This is efficient for B-tree index cleanup because it matches the order of leaf pages in the index. The memory savings relative to a plain array matter at scale: a table with 10 million dead tuples would require 80 MB in an uncompressed array but typically a fraction of that in an `IntegerSet`.

## Related Topics

- [[architecture/internal-data-structures|Internal data structure library]] — covers the other general-purpose structures in `lib/` (ilist, rbtree, binaryheap, pairingheap, bloomfilter, dshash)
- [[subsystems/planner/statistics|Planner statistics]] — how `n_distinct` and other per-column statistics are used during query planning
- [[subsystems/planner/extended-statistics|Extended statistics]] — multi-column and functional dependency statistics that build on the per-column infrastructure
- [[code-paths/vacuum|Vacuum]] — how dead TID tracking fits into the overall vacuum algorithm
