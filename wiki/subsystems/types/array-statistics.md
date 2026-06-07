---
title: "Array Column Statistics"
aliases:
  - array typanalyze
  - array_typanalyze
  - MCELEM
  - DECHIST
  - array selectivity
  - array statistics
tags:
  - theme/query-optimization
source_files:
  - src/backend/utils/adt/array_typanalyze.c
  - src/include/utils/array.h
symbols:
  - array_typanalyze
  - compute_array_stats
  - ArrayAnalyzeExtraData
  - TrackItem
  - DECountItem
  - prune_element_hashtable
---

When ANALYZE runs on a table with array columns, it needs statistics that can answer selectivity estimates for the operators `<@` (contained by), `&&` (overlaps), and `@>` (contains). Standard scalar statistics — most-common values and histograms of the full array — are usually insufficient, because two arrays may share many elements even when the arrays themselves are distinct. `src/backend/utils/adt/array_typanalyze.c` supplies a specialised statistics-gathering function that tracks which *elements* appear most often across all arrays in the column, in addition to the standard statistics.

## How It Connects to ANALYZE

PostgreSQL registers `array_typanalyze` as the `typanalyze` function for array types in the type catalog. When ANALYZE encounters an array column, it calls this function to set up the analysis. `array_typanalyze` first invokes the standard `std_typanalyze()` to set up scalar statistics, which handle btree-style array comparison operators. It then installs `compute_array_stats` as the actual statistics computation function by replacing `stats->compute_stats`.

`compute_array_stats` skips arrays wider than `ARRAY_WIDTH_THRESHOLD` (64 KB after detoasting). This avoids disproportionate I/O and memory cost from a small number of very large outlier arrays.

## Lossy Counting for Most-Common Elements

The core algorithm in `compute_array_stats` is the *Lossy Counting* (LC) algorithm by Manku and Motwani (VLDB 2002, section 4.2). LC finds frequent items in a data stream without keeping the full stream in memory.

The algorithm maintains a set D of triples `(element, frequency, delta)`:
- `frequency` is the observed count since the element was first seen.
- `delta` is the maximum possible undercount — the number of elements seen before this element entered D.

The algorithm processes elements in batches of size `w = 1/epsilon`. After each batch, it prunes any element where `frequency + delta <= batch_number` from D. This guarantees that the algorithm never misses an element with true frequency above the threshold `s`. It also guarantees that the algorithm never overestimates any frequency.

PostgreSQL uses parameters `s = 0.07 / K` and `epsilon = s / 10` (where `K = statistics_target * 10` is the desired number of most-common elements). This gives `w ≈ K / 0.007`. It also keeps the hashtable bounded to roughly 1000 × K entries in expectation.

The algorithm deduplicates elements within an array before counting. If the same element appears multiple times in one array, it counts that element only once for the array. This reflects that `<@`, `&&`, and `@>` treat arrays as sets.

## Statistics Slots

The gathered statistics occupy two `pg_statistic` slots beyond the ones used by `std_typanalyze()`:

**`STATISTIC_KIND_MCELEM`** — Most-common elements. Stored as parallel arrays of element values and frequencies. The `stanumbers` array contains one frequency per element plus three extra entries appended at the end: `minfreq`, `maxfreq`, and `null_element_frequency`. PostgreSQL sorts the values by element value, using the element type's default comparison function. This enables binary search in selectivity estimation.

**`STATISTIC_KIND_DECHIST`** — Distinct element count histogram. It tracks how many distinct elements each analyzed array contained. Then it produces a histogram of those counts. The last entry in `stanumbers` is the average distinct-element count across all non-null arrays. This histogram lets the planner estimate the fraction of rows matched by an overlap query without knowing which specific elements are involved.

## Impact on Query Planning

The MCELEM statistics feed into `arraycontsel()` and `arraycontjoinsel()`, which estimate the selectivity of `<@`, `&&`, and `@>` expressions. The estimator looks up the query's literal elements in the MCELEM list and sums their frequencies. It adjusts the sum for the probability that multiple independent elements all appear in the same array row. Arrays with no matching element in MCELEM fall back to a default frequency derived from `minfreq`.

```sql
-- See what statistics ANALYZE collected for an array column
SELECT stakind, stanumbers, stavalues
FROM pg_statistic
WHERE starelid = 'my_table'::regclass
  AND staattnum = (SELECT attnum FROM pg_attribute
                   WHERE attrelid = 'my_table'::regclass
                     AND attname = 'tags');
```

## Related Topics

- [[code-paths/analyze|ANALYZE]] — how statistics are collected
- [[subsystems/types/array-internals|Array Internals]] — array representation and operators
- [[subsystems/planner/overview|Planner Overview]] — how statistics feed into cost estimation
