---
title: Planner Cost Model
aliases:
  - cost estimation
  - query cost model
  - planner costs
tags:
  - symptom/slow-query
  - theme/query-optimization
source_files:
  - src/backend/optimizer/path/costsize.c
  - src/include/optimizer/cost.h
symbols:
  - cost_seqscan
  - cost_index
  - cost_bitmap_heap_scan
  - initial_cost_nestloop
  - final_cost_nestloop
  - initial_cost_hashjoin
  - final_cost_hashjoin
  - initial_cost_mergejoin
  - final_cost_mergejoin
  - index_pages_fetched
  - cost_qual_eval
  - clamp_row_est
---

# Planner Cost Model

PostgreSQL's planner chooses execution plans by estimating the cost of every candidate path and picking the cheapest one. That estimate is a number in arbitrary units, calibrated so that the cost of reading one sequential 8 kB page equals 1.0. Every operator cost, CPU cycle, and I/O fetch is expressed as a multiple of that baseline. The resulting numbers are not wall-clock times; they are comparable only against each other within the same planning session. They do scale predictably, however, with hardware and workload characteristics that the administrator can tune.

## Two-Part Cost: Startup and Total

Every path carries two cost fields: `startup_cost` and `total_cost`. The distinction matters because not all queries consume every row a plan produces. A `LIMIT 10` clause, an `EXISTS` subquery, and a correlated subquery that stops at the first match all benefit from a plan that delivers early rows cheaply. This holds even if the plan's total cost is higher than a rival's.

The planner interpolates to estimate the cost of fetching only `k` rows from a path that would otherwise produce `N`:

```
actual_cost = startup_cost + (total_cost - startup_cost) * k / N
```

A sequential scan has zero startup cost — it begins returning rows immediately. A hash join has high startup cost, because the entire inner relation must be hashed before the first output row can appear. For a query with `LIMIT 1`, the sequential scan wins even if the hash join's total cost is lower. For a full aggregation, a hash aggregate must consume all input before emitting anything. Its entire total cost is therefore also its effective startup cost. The planner treats the two identically in that context.

## Cost Units and the GUC Parameters

The reference unit is `seq_page_cost`, defaulting to 1.0. Every other cost parameter is expressed relative to it.

| GUC | Default | What it measures |
|-----|---------|-----------------|
| `seq_page_cost` | 1.0 | One sequential 8 kB page fetch |
| `random_page_cost` | 4.0 | One random (non-sequential) page fetch |
| `cpu_tuple_cost` | 0.01 | Processing one heap tuple through a plan node |
| `cpu_index_tuple_cost` | 0.005 | Processing one index tuple during an index scan |
| `cpu_operator_cost` | 0.0025 | Evaluating one operator or simple function |
| `parallel_tuple_cost` | 0.1 | Passing one tuple from a parallel worker to the leader |
| `parallel_setup_cost` | 1000.0 | Setting up shared memory for a parallel query |

The ratio between `seq_page_cost` and `random_page_cost` encodes the assumption that spinning disks require roughly four times longer for a random seek than for a sequential continuation. On SSDs the penalty is much smaller — often 1.1 to 1.5. On a hot database that fits entirely in RAM, it is essentially 1.0. Lowering `random_page_cost` toward `seq_page_cost` tells the planner that index scans and bitmap heap scans are cheaper relative to sequential scans. This promotes their use. Per-tablespace overrides are also available, letting you set different values for tablespaces backed by fast and slow storage.

The `effective_cache_size` parameter does not consume any memory — it is a hint to the planner about how large the combined PostgreSQL shared buffer pool plus OS file cache is. A larger value tells the planner that index pages are more likely to be found in cache. This reduces the estimated I/O cost of index scans on large tables.

## Sequential Scan Cost

A sequential scan has zero startup cost and a total cost that is the sum of I/O and CPU work (implemented in `cost_seqscan()`, costsize.c):

```
disk_cost  = seq_page_cost * rel.pages
cpu_cost   = (cpu_tuple_cost + qual_eval_cost) * rel.tuples
total_cost = disk_cost + cpu_cost
```

The I/O component scales with the physical page count (`rel.pages`), not the estimated row count. The CPU component scales with the total stored tuple count (`rel.tuples`), since every stored tuple must be visited to evaluate the WHERE clause, regardless of how many pass. The cost model charges target-list evaluation separately, only for output rows, because projection happens after filtering.

## Index Scan Cost

An index scan's startup cost covers descending the B-tree from root to the first matching leaf page. The heap fetch dominates the per-tuple cost — one random I/O per matched tuple in the worst case. The planner adjusts for correlation: when an index's physical ordering closely matches the heap's ordering (as measured by `pg_stats.correlation`), successive heap fetches land on adjacent pages and behave more like sequential I/O.

The actual adjustment interpolates between two extremes. In the worst case (`csquared = 0`, perfectly uncorrelated), every heap fetch is a random access:

```
max_IO_cost = pages_fetched * random_page_cost
```

In the best case (`csquared = 1`, perfectly correlated), only the first fetch is random and the rest are sequential:

```
min_IO_cost = random_page_cost + (pages_fetched - 1) * seq_page_cost
```

The actual I/O cost blends these:

```
IO_cost = max_IO_cost + csquared * (min_IO_cost - max_IO_cost)
```

This means a freshly-CLUSTERed table nearly eliminates the random-access penalty for index scans.

### Cache Effects and the Mackert-Lohman Formula

The number of heap pages fetched is not simply `selectivity * table_pages`, because cache re-use prevents the same page from being read twice. The planner uses the Mackert-Lohman approximation (`index_pages_fetched()`, costsize.c) to estimate how many distinct pages are actually fetched, given the tuple count and a buffer estimate `b` derived by pro-rating `effective_cache_size` across all tables and indexes in the query.

When `effective_cache_size` is large relative to the table, `b` approaches the table size. Many fetches then find their page already in cache, dramatically reducing the estimated fetch count. When the table is much larger than the cache, the formula approaches a linear relationship — every fetch is a cold miss. Getting `effective_cache_size` right is therefore important for index-vs-sequential scan decisions on large tables.

### Index-Only Scan

An index-only scan avoids the heap fetch entirely for tuples whose pages are covered by the [[subsystems/storage/visibility-map|visibility map]]. The planner reduces `pages_fetched` by a factor of `(1 - allvisfrac)`. `allvisfrac` is the fraction of the heap that is all-visible. The remaining fraction still requires a heap fetch to verify tuple visibility. On tables with frequent vacuum, `allvisfrac` is high. As a result, index-only scans are very cheap.

## Bitmap Heap Scan Cost

A bitmap scan's two phases have distinct cost structures. The cost model charges the first phase — building the bitmap by walking one or more indexes — entirely to startup cost. This is the sum of the index traversal costs for all contributing indexes. The second phase — sorting the TID bitmap and fetching heap pages in physical order — is run cost.

The key advantage of the bitmap heap scan is that it converts random heap access into something closer to sequential access. Because TIDs are sorted before any heap page is touched, the number of distinct pages fetched is much smaller than for a plain index scan on the same row count. The planner approximates the cost per page with an interpolation between `random_page_cost` and `seq_page_cost` based on what fraction of the table's pages will be visited:

```
cost_per_page = random_page_cost
              - (random_page_cost - seq_page_cost) * sqrt(pages_fetched / T)
```

When `pages_fetched` approaches `T` (the full table), the cost per page approaches `seq_page_cost`. When only a few pages are fetched, it approaches `random_page_cost`. This makes bitmap scans particularly attractive when selectivity is moderate — say, 1 %–20 % of a large table.

## Join Path Costs

### Nested Loop

Nested loop's cost is proportional to the outer row count multiplied by the inner scan cost. The planner models the inner side as being re-scanned once per outer row (computed in `initial_cost_nestloop()` and `final_cost_nestloop()`, costsize.c):

```
startup = outer.startup + inner.startup
run     = outer.run
        + inner.startup_rescan * (outer_rows - 1)
        + inner.run_rescan * (outer_rows - 1)
        + cpu_per_tuple * join_tuples
```

The distinction between first-scan and rescan cost matters: if the inner side is a materialized or memoized subplan, rescans may be much cheaper than the initial scan. When the inner path is an index scan using the join clause as an index condition, unmatched outer rows pay only the cost of an empty index probe. That cost is close to just the startup cost. Without that, every unmatched outer row must scan the full inner relation.

For SEMI or ANTI joins, the executor stops at the first match per outer row. The planner accounts for this by scaling the inner scan fraction by `2 / (match_count + 1)`.

### Hash Join

A hash join pays all of its build cost upfront. The hash join fully consumes and hashes the inner relation during startup; only then can the probe phase begin. The startup cost therefore includes the inner relation's total cost plus one hash function evaluation per inner tuple (`cpu_operator_cost` per hash clause) plus one `cpu_tuple_cost` per inner row for inserting into the hash table:

```
startup = inner.total_cost
        + (cpu_operator_cost * num_hash_clauses + cpu_tuple_cost) * inner_rows
```

The run cost is the probe phase: consuming the outer relation and probing the hash table. When the hash table does not fit in `work_mem`, it is batched to disk. Batching writes the inner relation to disk during startup. It then reads both inner and outer batches sequentially during the probe phase, charged at `seq_page_cost` per page.

This startup structure is why the planner avoids hash joins for LIMIT queries. It prefers them for queries that must produce all output. A hash join on a 10-million-row inner table has enormous startup cost; a nested loop with an index on the inner side may be far cheaper when only a few rows are needed.

### Merge Join

Merge join requires both inputs to arrive in sorted order on the join key. If an input is not already sorted, the planner adds an explicit sort node. That sort node has startup cost proportional to `N log N`. The merge join's run cost is then linear in the combined row count of both inputs, plus a rescan cost for duplicate key values on the outer side that require re-reading matching inner rows.

Startup cost for merge join is therefore at least as high as the cost to fully sort the smaller unsorted input. This makes the merge join relatively unattractive when neither input arrives pre-sorted. When both sides arrive ordered — for example from index scans on the join key — merge join can be competitive. It requires no additional sort work and produces output in order. This may satisfy a downstream ORDER BY for free.

## Row Count Estimation

Cost formulas are only as good as the row estimates they multiply. The planner derives the `rows` figure for each path from per-column statistics collected by ANALYZE: null fractions, most-common values (MCV) with their frequencies, and a histogram of the value distribution. Selectivity for equality predicates on indexed columns uses the MCV list directly; selectivity for range predicates uses the histogram. The planner then multiplies these estimates together under an independence assumption.

The planner then applies the product of per-column selectivities to the base table row count, producing the row estimate visible in `EXPLAIN` output. Join row counts multiply the input row counts by a join selectivity estimate, also derived from column statistics.

## Where the Model Breaks Down

The cost model is deliberate about what it does not know. Column correlations — where filtering on one column implies filtering on another — are invisible to the model. The model assumes independence. A query with `WHERE city = 'Paris' AND country = 'France'` will over-count filtering by applying both selectivities independently. The country predicate adds almost no additional filtering once the city is fixed. Extended statistics (`CREATE STATISTICS ... (dependencies)`) address this for specific column groups, but must be created explicitly.

Volatile functions and user-defined functions receive a fixed cost estimate from their catalog entry (`procost`). If a function is expensive, an inaccurate `procost` misleads the planner into treating it as cheap, potentially favoring plans that evaluate it more often than necessary.

Multi-predicate interactions and correlated subquery selectivity are similarly opaque. The planner cannot see that two predicates on different tables are semantically related. It also cannot know how many rows a correlated subquery will return for different outer values without statistics specifically targeting that pattern.

The `disable_cost` mechanism (1e10) is a blunt instrument for the enable/disable GUCs (`enable_seqscan`, `enable_hashjoin`, etc.). When a plan type is disabled, the planner adds 1e10 to its startup cost, making it effectively unreachable but not formally impossible. This allows the planner to still choose the disabled plan type if it is truly the only option.

## Related Topics

- [[subsystems/planner/selectivity-estimation|Selectivity Estimation]] — details how per-column statistics (MCVs, histograms, null fractions) are translated into the row-count estimates that feed every cost formula
- [[subsystems/planner/statistics|Extended Statistics]] — covers multi-column statistics created with `CREATE STATISTICS`, which correct the independence assumption the cost model relies on by default
- [[subsystems/planner/join-method-selection|Join Method Selection]] — explains how startup vs. total cost trade-offs drive the planner's choice between nested loop, hash join, and merge join for a given query
- [[subsystems/planner/bitmap-scans|Bitmap Scans]] — deeper treatment of the two-phase bitmap scan path whose cost structure is summarised in the cost model article
- [[subsystems/planner/index-selection|Index Selection]] — describes how the planner evaluates candidate indexes and uses correlation, `effective_cache_size`, and the Mackert-Lohman formula to pick between them
- [[subsystems/planner/stale-statistics-and-bad-plans|Stale Statistics and Bad Plans]] — practical guide to diagnosing and fixing cases where inaccurate row estimates cause the cost model to choose a poor plan
- [[troubleshooting/slow-queries|Slow Queries]] — troubleshooting workflow that starts from `EXPLAIN (ANALYZE, BUFFERS)` output and maps cost-model misestimates to concrete fixes
- [[subsystems/planner/overview|Planner Overview]] — high-level walkthrough of how a parsed query becomes an executable plan, giving context for where cost estimation fits into the overall pipeline
- [[subsystems/indexes/btree|B-tree Indexes]] — the index structure whose scan cost, including the correlation-based blending between random and sequential I/O, is modeled by the formulas described here
- [[subsystems/storage/buffer-manager|Buffer Manager]] — the shared buffer pool that `effective_cache_size` estimates the combined size of, directly shaping how many index page fetches the cost model expects to find already cached
- [[architecture/overview|Architecture Overview]] — the system-wide process and shared-memory model that the cost model's buffer and I/O assumptions are built on
